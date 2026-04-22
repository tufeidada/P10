"""
财务数据拉取管道

从 Tushare Pro 拉取财务报表数据、每日基本面数据，写入 PostgreSQL。

用法:
    python -m data.pipeline.fundamental_pull --symbols 600519.SH,601398.SH
    python -m data.pipeline.fundamental_pull --universe   # 拉取候选池所有票
    python -m data.pipeline.fundamental_pull --daily-basic --date 20260415
    python -m data.pipeline.fundamental_pull --daily-basic-range --symbols 600519.SH --start 20260101 --end 20260415
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import date as date_type
from decimal import Decimal
from typing import Any

import pandas as pd
import structlog

from data.sources.tushare_client import TushareClient
from db.connection import db_execute_many, db_query, db_query_val, init_pool, close_pool

logger = structlog.get_logger(__name__)

# Tushare API 调用间隔（秒），避免触发频率限制
_RATE_LIMIT_SLEEP: float = 0.3


def _safe_decimal(val: Any) -> Decimal | None:
    """将值安全转换为 Decimal，NaN/None 返回 None。"""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    try:
        return Decimal(str(val))
    except Exception:
        return None


def _safe_float(val: Any) -> float | None:
    """将值安全转换为 float，NaN/None 返回 None。"""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    try:
        return float(val)
    except Exception:
        return None


def _parse_date(date_str: Any) -> date_type | None:
    """将 YYYYMMDD 字符串转为 date 对象。"""
    if date_str is None or (isinstance(date_str, float) and pd.isna(date_str)):
        return None
    s = str(date_str).strip()
    if len(s) < 8:
        return None
    try:
        return date_type(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except (ValueError, TypeError):
        return None


class FundamentalPuller:
    """财务基本面数据拉取器。

    从 Tushare Pro 拉取财报数据（利润表、资产负债表、现金流量表、财务指标）
    和每日估值指标，写入 PostgreSQL。

    Attributes:
        _ts: TushareClient 实例。
    """

    def __init__(self) -> None:
        self._ts = TushareClient()

    async def pull_financials(self, symbol: str, quarters: int = 12) -> int:
        """拉取单只股票最近 N 季度的财务数据，写入 financials_quarterly。

        Steps:
            1. Query tushare ``fina_indicator`` for ROE/ROA/YoY growth etc.
            2. Query tushare ``income`` for revenue, net profit, margins.
            3. Query tushare ``balancesheet`` for assets, liabilities, goodwill.
            4. Query tushare ``cashflow`` for OCF.
            5. Merge by (ts_code, end_date) and compute derived fields.
            6. Upsert into financials_quarterly via ON CONFLICT DO UPDATE.

        Args:
            symbol: 证券代码，如 '600519.SH'。
            quarters: 拉取最近几个季度的数据，默认 12。

        Returns:
            成功 upsert 的行数。
        """
        log = logger.bind(symbol=symbol, quarters=quarters)
        log.info("pull_financials_start")

        try:
            # 1) fina_indicator — 财务指标
            df_fina = await asyncio.to_thread(
                self._ts._query_sync,
                "fina_indicator",
                ts_code=symbol,
                fields=(
                    "ts_code,end_date,ann_date,roe_dt,roa,"
                    "or_yoy,netprofit_yoy,grossprofit_margin,"
                    "debt_to_assets,q_revenue_yoy,q_netprofit_yoy,"
                    "dupont_roe,dupont_npm,dupont_tat,dupont_em"
                ),
            )
            await asyncio.sleep(_RATE_LIMIT_SLEEP)

            # 2) income — 利润表
            df_income = await asyncio.to_thread(
                self._ts._query_sync,
                "income",
                ts_code=symbol,
                fields="ts_code,end_date,ann_date,revenue,n_income",
            )
            await asyncio.sleep(_RATE_LIMIT_SLEEP)

            # 3) balancesheet — 资产负债表
            df_bs = await asyncio.to_thread(
                self._ts._query_sync,
                "balancesheet",
                ts_code=symbol,
                fields=(
                    "ts_code,end_date,total_assets,total_liab,"
                    "total_cur_assets,total_cur_liab,goodwill"
                ),
            )
            await asyncio.sleep(_RATE_LIMIT_SLEEP)

            # 4) cashflow — 现金流量表
            df_cf = await asyncio.to_thread(
                self._ts._query_sync,
                "cashflow",
                ts_code=symbol,
                fields="ts_code,end_date,n_cashflow_act",
            )

        except Exception as e:
            log.error("pull_financials_fetch_error", error=str(e))
            return 0

        # --- 合并数据 ---
        # 对每张表，取最近 N 季度，按 (ts_code, end_date) 去重（保留最新公告）
        dfs: dict[str, pd.DataFrame] = {
            "fina": df_fina,
            "income": df_income,
            "bs": df_bs,
            "cf": df_cf,
        }
        for key, df in dfs.items():
            if df is None or df.empty:
                log.warning("pull_financials_empty_table", table=key)
                dfs[key] = pd.DataFrame()
                continue
            df = df.sort_values("end_date", ascending=False)
            df = df.drop_duplicates(subset=["ts_code", "end_date"], keep="first")
            df = df.head(quarters)
            dfs[key] = df

        df_fina = dfs["fina"]
        df_income = dfs["income"]
        df_bs = dfs["bs"]
        df_cf = dfs["cf"]

        # 收集所有出现过的 end_date
        all_dates: set[str] = set()
        for df in [df_fina, df_income, df_bs, df_cf]:
            if not df.empty and "end_date" in df.columns:
                all_dates.update(df["end_date"].dropna().astype(str).tolist())

        if not all_dates:
            log.warning("pull_financials_no_data")
            return 0

        # 建立索引便于查找
        def _to_dict(df: pd.DataFrame) -> dict[str, dict[str, Any]]:
            """将 DataFrame 按 end_date 转为 dict[end_date, row_dict]。"""
            if df.empty:
                return {}
            result: dict[str, dict[str, Any]] = {}
            for _, row in df.iterrows():
                ed = str(row.get("end_date", ""))
                if ed and ed not in result:
                    result[ed] = row.to_dict()
            return result

        fina_map = _to_dict(df_fina)
        income_map = _to_dict(df_income)
        bs_map = _to_dict(df_bs)
        cf_map = _to_dict(df_cf)

        # --- 构建 upsert 参数 ---
        args_list: list[tuple] = []
        for end_date_str in sorted(all_dates, reverse=True)[:quarters]:
            report_date = _parse_date(end_date_str)
            if report_date is None:
                continue

            fina_row = fina_map.get(end_date_str, {})
            inc_row = income_map.get(end_date_str, {})
            bs_row = bs_map.get(end_date_str, {})
            cf_row = cf_map.get(end_date_str, {})

            # announce_date: 优先 fina 的 ann_date，其次 income 的
            ann_str = fina_row.get("ann_date") or inc_row.get("ann_date")
            announce_date = _parse_date(ann_str)

            # revenue & net_profit from income
            revenue = _safe_decimal(inc_row.get("revenue"))
            net_profit = _safe_decimal(inc_row.get("n_income"))

            # net_margin = net_profit / revenue
            net_margin: Decimal | None = None
            if revenue and net_profit and revenue > 0:
                net_margin = _safe_decimal(float(net_profit) / float(revenue))

            # balancesheet fields
            total_assets = _safe_decimal(bs_row.get("total_assets"))
            total_liab = _safe_decimal(bs_row.get("total_liab"))
            goodwill = _safe_decimal(bs_row.get("goodwill"))

            # debt_ratio: balancesheet 优先，fina fallback
            debt_ratio: Decimal | None = None
            ta_f = _safe_float(bs_row.get("total_assets"))
            tl_f = _safe_float(bs_row.get("total_liab"))
            if ta_f and tl_f and ta_f > 0:
                debt_ratio = _safe_decimal(tl_f / ta_f)
            if debt_ratio is None:
                debt_ratio = _safe_decimal(fina_row.get("debt_to_assets"))
                if debt_ratio is not None:
                    # fina 里 debt_to_assets 是百分比形式，转为小数
                    debt_ratio = _safe_decimal(float(debt_ratio) / 100.0)

            # current_ratio
            current_ratio: Decimal | None = None
            tca = _safe_float(bs_row.get("total_cur_assets"))
            tcl = _safe_float(bs_row.get("total_cur_liab"))
            if tca and tcl and tcl > 0:
                current_ratio = _safe_decimal(tca / tcl)

            # cashflow
            ocf = _safe_decimal(cf_row.get("n_cashflow_act"))
            ocf_to_np: Decimal | None = None
            np_f = _safe_float(inc_row.get("n_income"))
            ocf_f = _safe_float(cf_row.get("n_cashflow_act"))
            if ocf_f is not None and np_f and np_f != 0:
                ocf_to_np = _safe_decimal(ocf_f / np_f)

            # fina_indicator fields
            revenue_yoy = _safe_decimal(fina_row.get("or_yoy"))
            if revenue_yoy is not None:
                revenue_yoy = _safe_decimal(float(revenue_yoy) / 100.0)
            np_yoy = _safe_decimal(fina_row.get("netprofit_yoy"))
            if np_yoy is not None:
                np_yoy = _safe_decimal(float(np_yoy) / 100.0)
            gross_margin = _safe_decimal(fina_row.get("grossprofit_margin"))
            if gross_margin is not None:
                gross_margin = _safe_decimal(float(gross_margin) / 100.0)
            roe_ttm = _safe_decimal(fina_row.get("roe_dt"))
            if roe_ttm is not None:
                roe_ttm = _safe_decimal(float(roe_ttm) / 100.0)
            roa_ttm = _safe_decimal(fina_row.get("roa"))
            if roa_ttm is not None:
                roa_ttm = _safe_decimal(float(roa_ttm) / 100.0)

            # revenue_qoq — tushare q_revenue_yoy 是单季度同比，不是环比
            # 环比需要自行计算，这里暂不填
            revenue_qoq: Decimal | None = None

            # DuPont decomposition
            dupont_npm = _safe_decimal(fina_row.get("dupont_npm"))
            if dupont_npm is not None:
                dupont_npm = _safe_decimal(float(dupont_npm) / 100.0)
            dupont_tat = _safe_decimal(fina_row.get("dupont_tat"))
            dupont_em = _safe_decimal(fina_row.get("dupont_em"))

            args_list.append((
                symbol,          # $1  symbol
                report_date,     # $2  report_date
                announce_date,   # $3  announce_date
                revenue,         # $4  revenue
                revenue_yoy,     # $5  revenue_yoy
                revenue_qoq,     # $6  revenue_qoq
                net_profit,      # $7  net_profit
                np_yoy,          # $8  np_yoy
                gross_margin,    # $9  gross_margin
                net_margin,      # $10 net_margin
                total_assets,    # $11 total_assets
                total_liab,      # $12 total_liab
                debt_ratio,      # $13 debt_ratio
                current_ratio,   # $14 current_ratio
                goodwill,        # $15 goodwill
                ocf,             # $16 ocf
                ocf_to_np,       # $17 ocf_to_np
                roe_ttm,         # $18 roe_ttm
                roa_ttm,         # $19 roa_ttm
                dupont_npm,      # $20 dupont_npm
                dupont_tat,      # $21 dupont_tat
                dupont_em,       # $22 dupont_em
            ))

        if not args_list:
            log.warning("pull_financials_no_rows_to_upsert")
            return 0

        upsert_sql = """
            INSERT INTO financials_quarterly (
                symbol, report_date, announce_date,
                revenue, revenue_yoy, revenue_qoq,
                net_profit, np_yoy, gross_margin, net_margin,
                total_assets, total_liab, debt_ratio, current_ratio, goodwill,
                ocf, ocf_to_np, roe_ttm, roa_ttm,
                dupont_npm, dupont_tat, dupont_em
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                $11, $12, $13, $14, $15, $16, $17, $18, $19,
                $20, $21, $22
            )
            ON CONFLICT (symbol, report_date) DO UPDATE SET
                announce_date  = EXCLUDED.announce_date,
                revenue        = EXCLUDED.revenue,
                revenue_yoy    = EXCLUDED.revenue_yoy,
                revenue_qoq    = EXCLUDED.revenue_qoq,
                net_profit     = EXCLUDED.net_profit,
                np_yoy         = EXCLUDED.np_yoy,
                gross_margin   = EXCLUDED.gross_margin,
                net_margin     = EXCLUDED.net_margin,
                total_assets   = EXCLUDED.total_assets,
                total_liab     = EXCLUDED.total_liab,
                debt_ratio     = EXCLUDED.debt_ratio,
                current_ratio  = EXCLUDED.current_ratio,
                goodwill       = EXCLUDED.goodwill,
                ocf            = EXCLUDED.ocf,
                ocf_to_np      = EXCLUDED.ocf_to_np,
                roe_ttm        = EXCLUDED.roe_ttm,
                roa_ttm        = EXCLUDED.roa_ttm,
                dupont_npm     = EXCLUDED.dupont_npm,
                dupont_tat     = EXCLUDED.dupont_tat,
                dupont_em      = EXCLUDED.dupont_em
        """

        try:
            await db_execute_many(upsert_sql, args_list)
            log.info("pull_financials_ok", rows=len(args_list))
            return len(args_list)
        except Exception as e:
            log.error("pull_financials_upsert_error", error=str(e))
            raise

    async def pull_daily_basic(self, trade_date: str) -> int:
        """拉取单日的 daily_basic 数据（PE/PB/市值等），写入 fundamentals_daily。

        Tushare ``daily_basic`` 字段映射:
            - pe_ttm, pb, ps_ttm, total_mv (万元), circ_mv (万元), turnover_rate_f

        Args:
            trade_date: 交易日期，格式 'YYYYMMDD'。

        Returns:
            成功 upsert 的行数。
        """
        log = logger.bind(trade_date=trade_date)
        log.info("pull_daily_basic_start")

        try:
            df = await asyncio.to_thread(
                self._ts._query_sync,
                "daily_basic",
                trade_date=trade_date,
                fields="ts_code,trade_date,pe_ttm,pb,ps_ttm,total_mv,circ_mv,turnover_rate_f",
            )
        except Exception as e:
            log.error("pull_daily_basic_fetch_error", error=str(e))
            return 0

        if df is None or df.empty:
            log.warning("pull_daily_basic_no_data")
            return 0

        args_list: list[tuple] = []
        for _, row in df.iterrows():
            td = _parse_date(row.get("trade_date"))
            if td is None:
                continue
            args_list.append((
                str(row["ts_code"]),                     # $1 symbol
                td,                                       # $2 trade_date
                _safe_decimal(row.get("pe_ttm")),         # $3 pe_ttm
                _safe_decimal(row.get("pb")),             # $4 pb
                _safe_decimal(row.get("ps_ttm")),         # $5 ps_ttm
                _safe_decimal(row.get("total_mv")),       # $6 total_mv (万元)
                _safe_decimal(row.get("circ_mv")),        # $7 circ_mv (万元)
                _safe_decimal(row.get("turnover_rate_f")),  # $8 turnover_rate_f
            ))

        if not args_list:
            log.warning("pull_daily_basic_no_rows")
            return 0

        upsert_sql = """
            INSERT INTO fundamentals_daily (
                symbol, trade_date, pe_ttm, pb, ps_ttm,
                total_mv, circ_mv, turnover_rate_f
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (symbol, trade_date) DO UPDATE SET
                pe_ttm          = EXCLUDED.pe_ttm,
                pb              = EXCLUDED.pb,
                ps_ttm          = EXCLUDED.ps_ttm,
                total_mv        = EXCLUDED.total_mv,
                circ_mv         = EXCLUDED.circ_mv,
                turnover_rate_f = EXCLUDED.turnover_rate_f
        """

        try:
            await db_execute_many(upsert_sql, args_list)
            log.info("pull_daily_basic_ok", trade_date=trade_date, rows=len(args_list))
            return len(args_list)
        except Exception as e:
            log.error("pull_daily_basic_upsert_error", error=str(e))
            raise

    async def pull_daily_basic_range(
        self, symbol: str, start_date: str, end_date: str
    ) -> int:
        """拉取单只股票一段时间的 daily_basic，写入 fundamentals_daily。

        Args:
            symbol: 证券代码，如 '600519.SH'。
            start_date: 开始日期，格式 'YYYYMMDD'。
            end_date: 结束日期，格式 'YYYYMMDD'。

        Returns:
            成功 upsert 的行数。
        """
        log = logger.bind(symbol=symbol, start_date=start_date, end_date=end_date)
        log.info("pull_daily_basic_range_start")

        try:
            df = await asyncio.to_thread(
                self._ts._query_sync,
                "daily_basic",
                ts_code=symbol,
                start_date=start_date,
                end_date=end_date,
                fields="ts_code,trade_date,pe_ttm,pb,ps_ttm,total_mv,circ_mv,turnover_rate_f",
            )
        except Exception as e:
            log.error("pull_daily_basic_range_fetch_error", error=str(e))
            return 0

        if df is None or df.empty:
            log.warning("pull_daily_basic_range_no_data")
            return 0

        args_list: list[tuple] = []
        for _, row in df.iterrows():
            td = _parse_date(row.get("trade_date"))
            if td is None:
                continue
            args_list.append((
                str(row["ts_code"]),
                td,
                _safe_decimal(row.get("pe_ttm")),
                _safe_decimal(row.get("pb")),
                _safe_decimal(row.get("ps_ttm")),
                _safe_decimal(row.get("total_mv")),
                _safe_decimal(row.get("circ_mv")),
                _safe_decimal(row.get("turnover_rate_f")),
            ))

        if not args_list:
            log.warning("pull_daily_basic_range_no_rows")
            return 0

        upsert_sql = """
            INSERT INTO fundamentals_daily (
                symbol, trade_date, pe_ttm, pb, ps_ttm,
                total_mv, circ_mv, turnover_rate_f
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (symbol, trade_date) DO UPDATE SET
                pe_ttm          = EXCLUDED.pe_ttm,
                pb              = EXCLUDED.pb,
                ps_ttm          = EXCLUDED.ps_ttm,
                total_mv        = EXCLUDED.total_mv,
                circ_mv         = EXCLUDED.circ_mv,
                turnover_rate_f = EXCLUDED.turnover_rate_f
        """

        try:
            await db_execute_many(upsert_sql, args_list)
            log.info("pull_daily_basic_range_ok", rows=len(args_list))
            return len(args_list)
        except Exception as e:
            log.error("pull_daily_basic_range_upsert_error", error=str(e))
            raise

    async def pull_universe_financials(self, quarters: int = 12) -> dict[str, Any]:
        """拉取候选池所有股票的财报数据。

        从 watchlist.yaml 和 stock_universe 表获取候选池，
        逐只拉取财务数据。

        Args:
            quarters: 拉取最近几个季度的数据。

        Returns:
            汇总字典，包含 total_symbols, success, failed, total_rows 等。
        """
        log = logger.bind(quarters=quarters)
        log.info("pull_universe_financials_start")

        # 从 stock_universe 表获取候选池
        symbols: list[str] = []
        try:
            rows = await db_query(
                "SELECT symbol FROM stock_universe WHERE market = 'CN' AND is_active = true"
            )
            symbols = [r["symbol"] for r in rows]
        except Exception as e:
            log.warning("pull_universe_query_error", error=str(e))

        # 也尝试从 watchlist 表获取
        try:
            rows = await db_query(
                "SELECT DISTINCT symbol FROM watchlist WHERE market = 'CN'"
            )
            wl_symbols = {r["symbol"] for r in rows}
            for s in wl_symbols:
                if s not in symbols:
                    symbols.append(s)
        except Exception:
            pass  # watchlist 表可能不存在

        if not symbols:
            log.warning("pull_universe_financials_no_symbols")
            return {"total_symbols": 0, "success": 0, "failed": 0, "total_rows": 0}

        log.info("pull_universe_financials_symbols", count=len(symbols))

        success = 0
        failed = 0
        total_rows = 0
        failed_symbols: list[str] = []

        for symbol in symbols:
            try:
                rows_upserted = await self.pull_financials(symbol, quarters=quarters)
                total_rows += rows_upserted
                success += 1
                await asyncio.sleep(_RATE_LIMIT_SLEEP)
            except Exception as e:
                failed += 1
                failed_symbols.append(symbol)
                log.error(
                    "pull_universe_financials_symbol_error",
                    symbol=symbol,
                    error=str(e),
                )

        summary = {
            "total_symbols": len(symbols),
            "success": success,
            "failed": failed,
            "total_rows": total_rows,
            "failed_symbols": failed_symbols,
        }
        log.info("pull_universe_financials_done", **summary)
        return summary


async def _main() -> None:
    """CLI 入口，解析参数并执行对应操作。"""
    parser = argparse.ArgumentParser(
        description="P10-AlphaRadar 财务数据拉取管道"
    )
    parser.add_argument(
        "--symbols",
        type=str,
        default="",
        help="逗号分隔的证券代码，如 600519.SH,601398.SH",
    )
    parser.add_argument(
        "--universe",
        action="store_true",
        help="拉取候选池所有股票的财报数据",
    )
    parser.add_argument(
        "--daily-basic",
        action="store_true",
        help="拉取单日 daily_basic 数据",
    )
    parser.add_argument(
        "--daily-basic-range",
        action="store_true",
        help="拉取单只股票一段时间的 daily_basic",
    )
    parser.add_argument(
        "--date",
        type=str,
        default="",
        help="交易日期，格式 YYYYMMDD（配合 --daily-basic 使用）",
    )
    parser.add_argument(
        "--start",
        type=str,
        default="",
        help="开始日期，格式 YYYYMMDD（配合 --daily-basic-range 使用）",
    )
    parser.add_argument(
        "--end",
        type=str,
        default="",
        help="结束日期，格式 YYYYMMDD（配合 --daily-basic-range 使用）",
    )
    parser.add_argument(
        "--quarters",
        type=int,
        default=12,
        help="拉取最近几个季度的财报数据（默认 12）",
    )

    args = parser.parse_args()

    # 初始化数据库连接池
    await init_pool()

    puller = FundamentalPuller()

    try:
        if args.universe:
            summary = await puller.pull_universe_financials(quarters=args.quarters)
            logger.info("cli_universe_done", **summary)

        elif args.daily_basic:
            if not args.date:
                parser.error("--daily-basic 需要 --date 参数")
            rows = await puller.pull_daily_basic(args.date)
            logger.info("cli_daily_basic_done", date=args.date, rows=rows)

        elif args.daily_basic_range:
            symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
            if not symbols:
                parser.error("--daily-basic-range 需要 --symbols 参数")
            if not args.start or not args.end:
                parser.error("--daily-basic-range 需要 --start 和 --end 参数")
            for symbol in symbols:
                rows = await puller.pull_daily_basic_range(
                    symbol, args.start, args.end
                )
                logger.info(
                    "cli_daily_basic_range_done",
                    symbol=symbol,
                    rows=rows,
                )
                await asyncio.sleep(_RATE_LIMIT_SLEEP)

        elif args.symbols:
            symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
            for symbol in symbols:
                rows = await puller.pull_financials(
                    symbol, quarters=args.quarters
                )
                logger.info(
                    "cli_financials_done",
                    symbol=symbol,
                    rows=rows,
                )
                await asyncio.sleep(_RATE_LIMIT_SLEEP)

        else:
            parser.print_help()

    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
