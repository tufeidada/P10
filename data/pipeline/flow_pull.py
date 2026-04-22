"""
资金流向数据拉取管道

从 Tushare Pro 拉取个股资金流向、北向资金、融资融券数据，写入 PostgreSQL。

用法:
    python -m data.pipeline.flow_pull --moneyflow --date 20260415
    python -m data.pipeline.flow_pull --moneyflow-range --symbols 600519.SH --start 20260101 --end 20260415
    python -m data.pipeline.flow_pull --margin --date 20260415
    python -m data.pipeline.flow_pull --northbound --start 20260401 --end 20260415
    python -m data.pipeline.flow_pull --all --date 20260415
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
from db.connection import db_execute_many, db_query, init_pool, close_pool

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


class FlowPuller:
    """资金流向数据拉取器。

    从 Tushare Pro 拉取个股资金流向、北向资金、融资融券数据，
    写入 PostgreSQL 的 moneyflow_daily、northbound_daily、margin_daily 表。

    Attributes:
        _ts: TushareClient 实例。
    """

    def __init__(self) -> None:
        self._ts = TushareClient()

    async def pull_moneyflow(self, trade_date: str) -> int:
        """拉取单日全市场个股资金流向，写入 moneyflow_daily。

        Tushare API: ``moneyflow``，按 trade_date 查询。
        字段映射:
            - buy_lg_amount = ts.buy_lg_amount + ts.buy_elg_amount（合并大单+特大单）
            - sell_lg_amount = ts.sell_lg_amount + ts.sell_elg_amount
            - net_lg_amount = buy_lg - sell_lg
            - buy_md_amount, sell_md_amount, net_md_amount 直接映射
            - buy_sm_amount, sell_sm_amount, net_sm_amount 直接映射

        Args:
            trade_date: 交易日期，格式 'YYYYMMDD'。

        Returns:
            成功 upsert 的行数。
        """
        log = logger.bind(trade_date=trade_date)
        log.info("pull_moneyflow_start")

        try:
            df = await asyncio.to_thread(
                self._ts._query_sync,
                "moneyflow",
                trade_date=trade_date,
                fields=(
                    "ts_code,trade_date,"
                    "buy_sm_amount,sell_sm_amount,"
                    "buy_md_amount,sell_md_amount,"
                    "buy_lg_amount,sell_lg_amount,"
                    "buy_elg_amount,sell_elg_amount"
                ),
            )
        except Exception as e:
            log.error("pull_moneyflow_fetch_error", error=str(e))
            return 0

        if df is None or df.empty:
            log.warning("pull_moneyflow_no_data")
            return 0

        args_list: list[tuple] = []
        for _, row in df.iterrows():
            td = _parse_date(row.get("trade_date"))
            if td is None:
                continue

            # 合并大单 + 特大单
            buy_lg = _safe_decimal(row.get("buy_lg_amount"))
            buy_elg = _safe_decimal(row.get("buy_elg_amount"))
            sell_lg = _safe_decimal(row.get("sell_lg_amount"))
            sell_elg = _safe_decimal(row.get("sell_elg_amount"))

            combined_buy_lg: Decimal | None = None
            combined_sell_lg: Decimal | None = None
            if buy_lg is not None or buy_elg is not None:
                combined_buy_lg = (buy_lg or Decimal("0")) + (buy_elg or Decimal("0"))
            if sell_lg is not None or sell_elg is not None:
                combined_sell_lg = (sell_lg or Decimal("0")) + (sell_elg or Decimal("0"))

            net_lg: Decimal | None = None
            if combined_buy_lg is not None and combined_sell_lg is not None:
                net_lg = combined_buy_lg - combined_sell_lg

            buy_md = _safe_decimal(row.get("buy_md_amount"))
            sell_md = _safe_decimal(row.get("sell_md_amount"))
            net_md: Decimal | None = None
            if buy_md is not None and sell_md is not None:
                net_md = buy_md - sell_md

            buy_sm = _safe_decimal(row.get("buy_sm_amount"))
            sell_sm = _safe_decimal(row.get("sell_sm_amount"))
            net_sm: Decimal | None = None
            if buy_sm is not None and sell_sm is not None:
                net_sm = buy_sm - sell_sm

            args_list.append((
                str(row["ts_code"]),   # $1  symbol
                td,                     # $2  trade_date
                combined_buy_lg,        # $3  buy_lg_amount
                combined_sell_lg,       # $4  sell_lg_amount
                net_lg,                 # $5  net_lg_amount
                buy_md,                 # $6  buy_md_amount
                sell_md,                # $7  sell_md_amount
                net_md,                 # $8  net_md_amount
                buy_sm,                 # $9  buy_sm_amount
                sell_sm,                # $10 sell_sm_amount
                net_sm,                 # $11 net_sm_amount
            ))

        if not args_list:
            log.warning("pull_moneyflow_no_rows")
            return 0

        upsert_sql = """
            INSERT INTO moneyflow_daily (
                symbol, trade_date,
                buy_lg_amount, sell_lg_amount, net_lg_amount,
                buy_md_amount, sell_md_amount, net_md_amount,
                buy_sm_amount, sell_sm_amount, net_sm_amount
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            ON CONFLICT (symbol, trade_date) DO UPDATE SET
                buy_lg_amount  = EXCLUDED.buy_lg_amount,
                sell_lg_amount = EXCLUDED.sell_lg_amount,
                net_lg_amount  = EXCLUDED.net_lg_amount,
                buy_md_amount  = EXCLUDED.buy_md_amount,
                sell_md_amount = EXCLUDED.sell_md_amount,
                net_md_amount  = EXCLUDED.net_md_amount,
                buy_sm_amount  = EXCLUDED.buy_sm_amount,
                sell_sm_amount = EXCLUDED.sell_sm_amount,
                net_sm_amount  = EXCLUDED.net_sm_amount
        """

        try:
            await db_execute_many(upsert_sql, args_list)
            log.info("pull_moneyflow_ok", trade_date=trade_date, rows=len(args_list))
            return len(args_list)
        except Exception as e:
            log.error("pull_moneyflow_upsert_error", error=str(e))
            raise

    async def pull_moneyflow_range(
        self, symbol: str, start_date: str, end_date: str
    ) -> int:
        """拉取单只股票一段时间的资金流向，写入 moneyflow_daily。

        Tushare API: ``moneyflow``，按 ts_code + 日期范围查询。

        Args:
            symbol: 证券代码，如 '600519.SH'。
            start_date: 开始日期，格式 'YYYYMMDD'。
            end_date: 结束日期，格式 'YYYYMMDD'。

        Returns:
            成功 upsert 的行数。
        """
        log = logger.bind(symbol=symbol, start_date=start_date, end_date=end_date)
        log.info("pull_moneyflow_range_start")

        try:
            df = await asyncio.to_thread(
                self._ts._query_sync,
                "moneyflow",
                ts_code=symbol,
                start_date=start_date,
                end_date=end_date,
                fields=(
                    "ts_code,trade_date,"
                    "buy_sm_amount,sell_sm_amount,"
                    "buy_md_amount,sell_md_amount,"
                    "buy_lg_amount,sell_lg_amount,"
                    "buy_elg_amount,sell_elg_amount"
                ),
            )
        except Exception as e:
            log.error("pull_moneyflow_range_fetch_error", error=str(e))
            return 0

        if df is None or df.empty:
            log.warning("pull_moneyflow_range_no_data")
            return 0

        args_list: list[tuple] = []
        for _, row in df.iterrows():
            td = _parse_date(row.get("trade_date"))
            if td is None:
                continue

            buy_lg = _safe_decimal(row.get("buy_lg_amount"))
            buy_elg = _safe_decimal(row.get("buy_elg_amount"))
            sell_lg = _safe_decimal(row.get("sell_lg_amount"))
            sell_elg = _safe_decimal(row.get("sell_elg_amount"))

            combined_buy_lg: Decimal | None = None
            combined_sell_lg: Decimal | None = None
            if buy_lg is not None or buy_elg is not None:
                combined_buy_lg = (buy_lg or Decimal("0")) + (buy_elg or Decimal("0"))
            if sell_lg is not None or sell_elg is not None:
                combined_sell_lg = (sell_lg or Decimal("0")) + (sell_elg or Decimal("0"))

            net_lg: Decimal | None = None
            if combined_buy_lg is not None and combined_sell_lg is not None:
                net_lg = combined_buy_lg - combined_sell_lg

            buy_md = _safe_decimal(row.get("buy_md_amount"))
            sell_md = _safe_decimal(row.get("sell_md_amount"))
            net_md: Decimal | None = None
            if buy_md is not None and sell_md is not None:
                net_md = buy_md - sell_md

            buy_sm = _safe_decimal(row.get("buy_sm_amount"))
            sell_sm = _safe_decimal(row.get("sell_sm_amount"))
            net_sm: Decimal | None = None
            if buy_sm is not None and sell_sm is not None:
                net_sm = buy_sm - sell_sm

            args_list.append((
                str(row["ts_code"]),
                td,
                combined_buy_lg,
                combined_sell_lg,
                net_lg,
                buy_md,
                sell_md,
                net_md,
                buy_sm,
                sell_sm,
                net_sm,
            ))

        if not args_list:
            log.warning("pull_moneyflow_range_no_rows")
            return 0

        upsert_sql = """
            INSERT INTO moneyflow_daily (
                symbol, trade_date,
                buy_lg_amount, sell_lg_amount, net_lg_amount,
                buy_md_amount, sell_md_amount, net_md_amount,
                buy_sm_amount, sell_sm_amount, net_sm_amount
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            ON CONFLICT (symbol, trade_date) DO UPDATE SET
                buy_lg_amount  = EXCLUDED.buy_lg_amount,
                sell_lg_amount = EXCLUDED.sell_lg_amount,
                net_lg_amount  = EXCLUDED.net_lg_amount,
                buy_md_amount  = EXCLUDED.buy_md_amount,
                sell_md_amount = EXCLUDED.sell_md_amount,
                net_md_amount  = EXCLUDED.net_md_amount,
                buy_sm_amount  = EXCLUDED.buy_sm_amount,
                sell_sm_amount = EXCLUDED.sell_sm_amount,
                net_sm_amount  = EXCLUDED.net_sm_amount
        """

        try:
            await db_execute_many(upsert_sql, args_list)
            log.info("pull_moneyflow_range_ok", rows=len(args_list))
            return len(args_list)
        except Exception as e:
            log.error("pull_moneyflow_range_upsert_error", error=str(e))
            raise

    async def pull_margin(self, trade_date: str) -> int:
        """拉取单日融资融券明细数据，写入 margin_daily。

        Tushare API: ``margin_detail``，按 trade_date 查询。
        字段: ts_code, trade_date, rzye (融资余额), rzmre (融资买入额), rqye (融券余额)

        Args:
            trade_date: 交易日期，格式 'YYYYMMDD'。

        Returns:
            成功 upsert 的行数。
        """
        log = logger.bind(trade_date=trade_date)
        log.info("pull_margin_start")

        try:
            df = await asyncio.to_thread(
                self._ts._query_sync,
                "margin_detail",
                trade_date=trade_date,
                fields="ts_code,trade_date,rzye,rzmre,rqye",
            )
        except Exception as e:
            log.error("pull_margin_fetch_error", error=str(e))
            return 0

        if df is None or df.empty:
            log.warning("pull_margin_no_data")
            return 0

        args_list: list[tuple] = []
        for _, row in df.iterrows():
            td = _parse_date(row.get("trade_date"))
            if td is None:
                continue

            args_list.append((
                str(row["ts_code"]),                 # $1 symbol
                td,                                   # $2 trade_date
                _safe_decimal(row.get("rzye")),       # $3 rzye
                _safe_decimal(row.get("rzmre")),      # $4 rzmre
                _safe_decimal(row.get("rqye")),       # $5 rqye
            ))

        if not args_list:
            log.warning("pull_margin_no_rows")
            return 0

        upsert_sql = """
            INSERT INTO margin_daily (
                symbol, trade_date, rzye, rzmre, rqye
            ) VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (symbol, trade_date) DO UPDATE SET
                rzye  = EXCLUDED.rzye,
                rzmre = EXCLUDED.rzmre,
                rqye  = EXCLUDED.rqye
        """

        try:
            await db_execute_many(upsert_sql, args_list)
            log.info("pull_margin_ok", trade_date=trade_date, rows=len(args_list))
            return len(args_list)
        except Exception as e:
            log.error("pull_margin_upsert_error", error=str(e))
            raise

    async def pull_northbound(self, start_date: str, end_date: str) -> int:
        """拉取北向资金流向数据，写入 northbound_daily。

        Tushare API: ``moneyflow_hsgt``
        字段: trade_date, hgt (沪股通, 百万), sgt (深股通, 百万)

        转换: 百万 -> 万（乘以 100）
        累计值: 基于已有数据计算 sh_cumulative, sz_cumulative

        Args:
            start_date: 开始日期，格式 'YYYYMMDD'。
            end_date: 结束日期，格式 'YYYYMMDD'。

        Returns:
            成功 upsert 的行数。
        """
        log = logger.bind(start_date=start_date, end_date=end_date)
        log.info("pull_northbound_start")

        try:
            df = await asyncio.to_thread(
                self._ts._query_sync,
                "moneyflow_hsgt",
                start_date=start_date,
                end_date=end_date,
                fields="trade_date,hgt,sgt",
            )
        except Exception as e:
            log.error("pull_northbound_fetch_error", error=str(e))
            return 0

        if df is None or df.empty:
            log.warning("pull_northbound_no_data")
            return 0

        # 获取已有最新累计值，用于计算后续日期的累计
        prev_sh_cum = Decimal("0")
        prev_sz_cum = Decimal("0")
        try:
            rows = await db_query(
                """
                SELECT sh_cumulative, sz_cumulative
                FROM northbound_daily
                ORDER BY trade_date DESC
                LIMIT 1
                """
            )
            if rows:
                prev_sh_cum = Decimal(str(rows[0]["sh_cumulative"] or 0))
                prev_sz_cum = Decimal(str(rows[0]["sz_cumulative"] or 0))
        except Exception as e:
            log.warning("pull_northbound_cumulative_query_error", error=str(e))

        # 按日期升序排列，方便累计计算
        df = df.sort_values("trade_date", ascending=True)

        args_list: list[tuple] = []
        for _, row in df.iterrows():
            td = _parse_date(row.get("trade_date"))
            if td is None:
                continue

            # Tushare 单位: 百万 -> 转为万（乘以 100）
            hgt_raw = _safe_decimal(row.get("hgt"))
            sgt_raw = _safe_decimal(row.get("sgt"))

            sh_net: Decimal | None = None
            sz_net: Decimal | None = None
            total_net: Decimal | None = None

            if hgt_raw is not None:
                sh_net = hgt_raw * Decimal("100")
            if sgt_raw is not None:
                sz_net = sgt_raw * Decimal("100")
            if sh_net is not None and sz_net is not None:
                total_net = sh_net + sz_net

            # 累计计算
            if sh_net is not None:
                prev_sh_cum += sh_net
            if sz_net is not None:
                prev_sz_cum += sz_net

            args_list.append((
                td,             # $1 trade_date
                sh_net,         # $2 sh_net_buy
                sz_net,         # $3 sz_net_buy
                total_net,      # $4 total_net_buy
                prev_sh_cum,    # $5 sh_cumulative
                prev_sz_cum,    # $6 sz_cumulative
            ))

        if not args_list:
            log.warning("pull_northbound_no_rows")
            return 0

        upsert_sql = """
            INSERT INTO northbound_daily (
                trade_date, sh_net_buy, sz_net_buy, total_net_buy,
                sh_cumulative, sz_cumulative
            ) VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (trade_date) DO UPDATE SET
                sh_net_buy     = EXCLUDED.sh_net_buy,
                sz_net_buy     = EXCLUDED.sz_net_buy,
                total_net_buy  = EXCLUDED.total_net_buy,
                sh_cumulative  = EXCLUDED.sh_cumulative,
                sz_cumulative  = EXCLUDED.sz_cumulative
        """

        try:
            await db_execute_many(upsert_sql, args_list)
            log.info("pull_northbound_ok", rows=len(args_list))
            return len(args_list)
        except Exception as e:
            log.error("pull_northbound_upsert_error", error=str(e))
            raise

    async def pull_all(self, trade_date: str) -> dict[str, int]:
        """拉取单日全部资金流向数据（个股资金流 + 融资融券 + 北向资金）。

        Args:
            trade_date: 交易日期，格式 'YYYYMMDD'。

        Returns:
            各类别写入行数的汇总字典。
        """
        log = logger.bind(trade_date=trade_date)
        log.info("pull_all_start")

        results: dict[str, int] = {}

        # 1) 个股资金流向
        try:
            results["moneyflow"] = await self.pull_moneyflow(trade_date)
        except Exception as e:
            log.error("pull_all_moneyflow_error", error=str(e))
            results["moneyflow"] = 0
        await asyncio.sleep(_RATE_LIMIT_SLEEP)

        # 2) 融资融券
        try:
            results["margin"] = await self.pull_margin(trade_date)
        except Exception as e:
            log.error("pull_all_margin_error", error=str(e))
            results["margin"] = 0
        await asyncio.sleep(_RATE_LIMIT_SLEEP)

        # 3) 北向资金（用当日作为 start 和 end）
        try:
            results["northbound"] = await self.pull_northbound(trade_date, trade_date)
        except Exception as e:
            log.error("pull_all_northbound_error", error=str(e))
            results["northbound"] = 0

        log.info("pull_all_done", **results)
        return results


async def _main() -> None:
    """CLI 入口，解析参数并执行对应操作。"""
    parser = argparse.ArgumentParser(
        description="P10-AlphaRadar 资金流向数据拉取管道"
    )
    parser.add_argument(
        "--moneyflow",
        action="store_true",
        help="拉取单日全市场个股资金流向",
    )
    parser.add_argument(
        "--moneyflow-range",
        action="store_true",
        help="拉取单只股票一段时间的资金流向",
    )
    parser.add_argument(
        "--margin",
        action="store_true",
        help="拉取单日融资融券明细",
    )
    parser.add_argument(
        "--northbound",
        action="store_true",
        help="拉取北向资金流向",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="拉取单日全部资金流向数据",
    )
    parser.add_argument(
        "--symbols",
        type=str,
        default="",
        help="逗号分隔的证券代码（配合 --moneyflow-range 使用）",
    )
    parser.add_argument(
        "--date",
        type=str,
        default="",
        help="交易日期，格式 YYYYMMDD",
    )
    parser.add_argument(
        "--start",
        type=str,
        default="",
        help="开始日期，格式 YYYYMMDD",
    )
    parser.add_argument(
        "--end",
        type=str,
        default="",
        help="结束日期，格式 YYYYMMDD",
    )

    args = parser.parse_args()

    # 初始化数据库连接池
    await init_pool()

    puller = FlowPuller()

    try:
        if args.all:
            if not args.date:
                parser.error("--all 需要 --date 参数")
            results = await puller.pull_all(args.date)
            logger.info("cli_all_done", date=args.date, **results)

        elif args.moneyflow:
            if not args.date:
                parser.error("--moneyflow 需要 --date 参数")
            rows = await puller.pull_moneyflow(args.date)
            logger.info("cli_moneyflow_done", date=args.date, rows=rows)

        elif args.moneyflow_range:
            symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
            if not symbols:
                parser.error("--moneyflow-range 需要 --symbols 参数")
            if not args.start or not args.end:
                parser.error("--moneyflow-range 需要 --start 和 --end 参数")
            for symbol in symbols:
                rows = await puller.pull_moneyflow_range(
                    symbol, args.start, args.end
                )
                logger.info(
                    "cli_moneyflow_range_done",
                    symbol=symbol,
                    rows=rows,
                )
                await asyncio.sleep(_RATE_LIMIT_SLEEP)

        elif args.margin:
            if not args.date:
                parser.error("--margin 需要 --date 参数")
            rows = await puller.pull_margin(args.date)
            logger.info("cli_margin_done", date=args.date, rows=rows)

        elif args.northbound:
            if not args.start or not args.end:
                parser.error("--northbound 需要 --start 和 --end 参数")
            rows = await puller.pull_northbound(args.start, args.end)
            logger.info("cli_northbound_done", start=args.start, end=args.end, rows=rows)

        else:
            parser.print_help()

    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
