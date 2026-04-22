"""美股数据拉取管道。

从 yfinance 拉取日线、分钟线、基本面、财务三表，写入 PostgreSQL。

Usage:
    python -m data.pipeline.us_data_pull --symbols AAPL,NVDA,MSFT --bars --financials
    python -m data.pipeline.us_data_pull --symbols AAPL --intraday
    python -m data.pipeline.us_data_pull --symbols AAPL --all --start 2024-01-01
    python -m data.pipeline.us_data_pull --universe --bars
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import date as date_type
from datetime import datetime
from decimal import Decimal
from typing import Any

import pandas as pd
import structlog

from data.sources.yfinance_client import YFinanceClient
from db.connection import db_execute_many, db_query, init_pool, close_pool

logger = structlog.get_logger(__name__)

# yfinance 调用间隔（秒）
_RATE_LIMIT_SLEEP: float = 0.5


# ============================================================
# 类型转换工具
# ============================================================

def _safe_decimal(val: Any) -> Decimal | None:
    """将值安全转换为 Decimal，NaN/None 返回 None。

    Args:
        val: 任意值。

    Returns:
        Decimal 或 None。
    """
    if val is None:
        return None
    if isinstance(val, float) and (pd.isna(val) or not pd.api.types.is_float(val)):
        return None
    try:
        f = float(val)
        if pd.isna(f):
            return None
        return Decimal(str(f))
    except (TypeError, ValueError):
        return None


def _safe_float(val: Any) -> float | None:
    """将值安全转换为 float，NaN/None 返回 None.

    Args:
        val: 任意值。

    Returns:
        float 或 None。
    """
    if val is None:
        return None
    try:
        f = float(val)
        return None if pd.isna(f) else f
    except (TypeError, ValueError):
        return None


def _safe_int(val: Any) -> int | None:
    """将值安全转换为 int，NaN/None 返回 None.

    Args:
        val: 任意值。

    Returns:
        int 或 None。
    """
    f = _safe_float(val)
    return int(f) if f is not None else None


def _get_row_value(df: pd.DataFrame, row_key: str, col_idx: int = 0) -> Any:
    """从财务 DataFrame 中按行名、列索引取值。

    Args:
        df: 财务 DataFrame（行=科目，列=日期，newest first）。
        row_key: 行名（科目名称）。
        col_idx: 列索引，0 表示最新一期。

    Returns:
        对应单元格的原始值，未找到返回 None。
    """
    if df is None or df.empty:
        return None
    if row_key not in df.index:
        return None
    if col_idx >= len(df.columns):
        return None
    return df.loc[row_key, df.columns[col_idx]]


def _compute_yoy(current: float | None, prev: float | None) -> Decimal | None:
    """计算同比增长率 (current - prev) / abs(prev)。

    Args:
        current: 当期值。
        prev: 上年同期值。

    Returns:
        同比增长率 Decimal，或 None（无法计算时）。
    """
    if current is None or prev is None or prev == 0:
        return None
    return _safe_decimal((current - prev) / abs(prev))


# ============================================================
# 主拉取器
# ============================================================

class USDataPuller:
    """美股数据拉取器。

    封装 YFinanceClient，提供日线、分钟线、基本面、财务三表的
    批量拉取与数据库写入功能。

    Attributes:
        _yf: YFinanceClient 实例。
    """

    def __init__(self) -> None:
        self._yf = YFinanceClient()

    # ------------------------------------------------------------------
    # 日线 OHLCV
    # ------------------------------------------------------------------

    async def pull_daily_bars(
        self,
        symbols: list[str],
        start: str,
        end: str,
    ) -> dict[str, int]:
        """拉取美股日线 OHLCV 并写入 market_bars_daily。

        数据已前复权（auto_adjust=True），adj_factor 固定写 1.0。

        Args:
            symbols: ticker 列表，如 ['AAPL', 'NVDA']。
            start: 开始日期，'YYYY-MM-DD'。
            end: 结束日期，'YYYY-MM-DD'（exclusive）。

        Returns:
            {symbol: rows_saved} 字典。
        """
        log = logger.bind(market="US", module="us_data_pull")
        log.info("pull_daily_bars_start", symbols=len(symbols), start=start, end=end)

        result: dict[str, int] = {}

        for symbol in symbols:
            sym_log = log.bind(symbol=symbol)
            try:
                df = await self._yf.get_daily_bars(symbol, start, end)
                if df.empty:
                    sym_log.warning("pull_daily_bars_empty")
                    result[symbol] = 0
                    await asyncio.sleep(_RATE_LIMIT_SLEEP)
                    continue

                args_list: list[tuple] = []
                for idx, row in df.iterrows():
                    trade_date = idx.date() if hasattr(idx, "date") else idx
                    args_list.append((
                        symbol,                                    # $1  symbol
                        "US",                                      # $2  market
                        trade_date,                                # $3  trade_date
                        _safe_decimal(row.get("Open")),            # $4  open
                        _safe_decimal(row.get("High")),            # $5  high
                        _safe_decimal(row.get("Low")),             # $6  low
                        _safe_decimal(row.get("Close")),           # $7  close
                        _safe_int(row.get("Volume")),              # $8  volume
                        None,                                      # $9  amount (yfinance无)
                        None,                                      # $10 turnover
                        Decimal("1.0"),                            # $11 adj_factor
                    ))

                upsert_sql = """
                    INSERT INTO market_bars_daily
                        (symbol, market, trade_date, open, high, low, close,
                         volume, amount, turnover, adj_factor)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                    ON CONFLICT (symbol, trade_date) DO UPDATE SET
                        open       = EXCLUDED.open,
                        high       = EXCLUDED.high,
                        low        = EXCLUDED.low,
                        close      = EXCLUDED.close,
                        volume     = EXCLUDED.volume,
                        amount     = EXCLUDED.amount,
                        adj_factor = EXCLUDED.adj_factor
                """
                await db_execute_many(upsert_sql, args_list)
                result[symbol] = len(args_list)
                sym_log.info("pull_daily_bars_ok", rows=len(args_list))

            except Exception as e:
                sym_log.error("pull_daily_bars_error", error=str(e))
                result[symbol] = 0

            await asyncio.sleep(_RATE_LIMIT_SLEEP)

        log.info("pull_daily_bars_done", summary=result)
        return result

    # ------------------------------------------------------------------
    # 基本面（每日 PE/PB/市值）
    # ------------------------------------------------------------------

    async def pull_fundamentals(
        self,
        symbols: list[str],
        trade_date: date_type,
    ) -> dict[str, int]:
        """拉取 PE/PB/市值并写入 fundamentals_daily。

        Args:
            symbols: ticker 列表。
            trade_date: 数据对应的交易日。

        Returns:
            {symbol: rows_saved} 字典（0 = 失败或无数据）。
        """
        log = logger.bind(market="US", trade_date=str(trade_date), module="us_data_pull")
        log.info("pull_fundamentals_start", symbols=len(symbols))

        result: dict[str, int] = {}

        for symbol in symbols:
            sym_log = log.bind(symbol=symbol)
            try:
                info = await self._yf.get_info(symbol)
                if not info:
                    sym_log.warning("pull_fundamentals_empty_info")
                    result[symbol] = 0
                    await asyncio.sleep(_RATE_LIMIT_SLEEP)
                    continue

                market_cap_raw = info.get("market_cap")
                # 将 USD 市值转换为 万元（1 USD ≈ 7.2 CNY，这里存原始 USD/10000 近似）
                total_mv: Decimal | None = None
                if market_cap_raw is not None:
                    mc_f = _safe_float(market_cap_raw)
                    if mc_f is not None:
                        total_mv = _safe_decimal(mc_f / 10000.0)

                upsert_sql = """
                    INSERT INTO fundamentals_daily
                        (symbol, trade_date, pe_ttm, pb, ps_ttm, total_mv, circ_mv, turnover_rate_f)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                    ON CONFLICT (symbol, trade_date) DO UPDATE SET
                        pe_ttm          = EXCLUDED.pe_ttm,
                        pb              = EXCLUDED.pb,
                        ps_ttm          = EXCLUDED.ps_ttm,
                        total_mv        = EXCLUDED.total_mv,
                        circ_mv         = EXCLUDED.circ_mv,
                        turnover_rate_f = EXCLUDED.turnover_rate_f
                """
                await db_execute_many(upsert_sql, [(
                    symbol,                              # $1 symbol
                    trade_date,                          # $2 trade_date
                    _safe_decimal(info.get("pe_ttm")),   # $3 pe_ttm
                    _safe_decimal(info.get("pb")),        # $4 pb
                    _safe_decimal(info.get("ps_ttm")),    # $5 ps_ttm
                    total_mv,                            # $6 total_mv
                    None,                                # $7 circ_mv（yfinance 无流通市值）
                    None,                                # $8 turnover_rate_f（yfinance 无换手率）
                )])
                result[symbol] = 1
                sym_log.info("pull_fundamentals_ok")

            except Exception as e:
                sym_log.error("pull_fundamentals_error", error=str(e))
                result[symbol] = 0

            await asyncio.sleep(_RATE_LIMIT_SLEEP)

        log.info("pull_fundamentals_done", summary=result)
        return result

    # ------------------------------------------------------------------
    # 财务三表
    # ------------------------------------------------------------------

    async def pull_financials(self, symbols: list[str]) -> dict[str, int]:
        """拉取季报财务数据并写入 financials_quarterly。

        yfinance DataFrame 结构：行=财务科目，列=季度日期（newest first）。
        最多处理最近 4 个季度。

        字段映射：
        - 利润表: Total Revenue → revenue, Net Income → net_profit,
          Gross Profit / Total Revenue → gross_margin
        - 资产负债表: Total Assets → total_assets,
          Total Liabilities Net Minority Interest → total_liab,
          Current Assets / Current Liabilities → current_ratio,
          total_liab / total_assets → debt_ratio
        - 现金流量表: Operating Cash Flow → ocf,
          ocf / net_profit → ocf_to_np
        - 同比: (current - prev) / abs(prev)

        Args:
            symbols: ticker 列表。

        Returns:
            {symbol: rows_upserted} 字典。
        """
        log = logger.bind(market="US", module="us_data_pull")
        log.info("pull_financials_start", symbols=len(symbols))

        result: dict[str, int] = {}

        for symbol in symbols:
            sym_log = log.bind(symbol=symbol)
            try:
                financials = await self._yf.get_financials(symbol)
                income_stmt   = financials.get("income_stmt",   pd.DataFrame())
                balance_sheet = financials.get("balance_sheet", pd.DataFrame())
                cashflow      = financials.get("cashflow",      pd.DataFrame())

                if income_stmt.empty and balance_sheet.empty and cashflow.empty:
                    sym_log.warning("pull_financials_all_empty")
                    result[symbol] = 0
                    await asyncio.sleep(_RATE_LIMIT_SLEEP)
                    continue

                # 以利润表季度列为主，最多取 4 期
                if not income_stmt.empty:
                    n_quarters = min(4, len(income_stmt.columns))
                elif not balance_sheet.empty:
                    n_quarters = min(4, len(balance_sheet.columns))
                else:
                    n_quarters = min(4, len(cashflow.columns))

                args_list: list[tuple] = []

                for col_idx in range(n_quarters):
                    # --- 取各表当期/上期值 ---
                    # 利润表
                    revenue_f    = _safe_float(_get_row_value(income_stmt, "Total Revenue", col_idx))
                    net_profit_f = _safe_float(_get_row_value(income_stmt, "Net Income", col_idx))
                    gross_profit_f = _safe_float(_get_row_value(income_stmt, "Gross Profit", col_idx))

                    # 同比：上期 = col_idx + 4（季度同比）
                    revenue_prev_f    = _safe_float(_get_row_value(income_stmt, "Total Revenue",    col_idx + 4))
                    net_profit_prev_f = _safe_float(_get_row_value(income_stmt, "Net Income",       col_idx + 4))

                    # 资产负债表
                    total_assets_f = _safe_float(_get_row_value(balance_sheet, "Total Assets",                            col_idx))
                    total_liab_f   = _safe_float(_get_row_value(balance_sheet, "Total Liabilities Net Minority Interest", col_idx))
                    cur_assets_f   = _safe_float(_get_row_value(balance_sheet, "Current Assets",      col_idx))
                    cur_liab_f     = _safe_float(_get_row_value(balance_sheet, "Current Liabilities", col_idx))

                    # 现金流量表
                    ocf_f = _safe_float(_get_row_value(cashflow, "Operating Cash Flow", col_idx))

                    # --- 衍生指标 ---
                    gross_margin: Decimal | None = None
                    if gross_profit_f is not None and revenue_f and revenue_f != 0:
                        gross_margin = _safe_decimal(gross_profit_f / revenue_f)

                    net_margin: Decimal | None = None
                    if net_profit_f is not None and revenue_f and revenue_f != 0:
                        net_margin = _safe_decimal(net_profit_f / revenue_f)

                    debt_ratio: Decimal | None = None
                    if total_liab_f is not None and total_assets_f and total_assets_f != 0:
                        debt_ratio = _safe_decimal(total_liab_f / total_assets_f)

                    current_ratio: Decimal | None = None
                    if cur_assets_f is not None and cur_liab_f and cur_liab_f != 0:
                        current_ratio = _safe_decimal(cur_assets_f / cur_liab_f)

                    ocf_to_np: Decimal | None = None
                    if ocf_f is not None and net_profit_f and net_profit_f != 0:
                        ocf_to_np = _safe_decimal(ocf_f / net_profit_f)

                    revenue_yoy = _compute_yoy(revenue_f, revenue_prev_f)
                    np_yoy      = _compute_yoy(net_profit_f, net_profit_prev_f)

                    # --- 确定报告期 ---
                    report_date: date_type | None = None
                    for df_source in [income_stmt, balance_sheet, cashflow]:
                        if not df_source.empty and col_idx < len(df_source.columns):
                            col_val = df_source.columns[col_idx]
                            if hasattr(col_val, "date"):
                                report_date = col_val.date()
                            elif isinstance(col_val, str):
                                try:
                                    report_date = datetime.strptime(col_val[:10], "%Y-%m-%d").date()
                                except ValueError:
                                    pass
                            break

                    if report_date is None:
                        sym_log.warning("pull_financials_no_report_date", col_idx=col_idx)
                        continue

                    args_list.append((
                        symbol,                          # $1  symbol
                        report_date,                     # $2  report_date
                        None,                            # $3  announce_date（yfinance 无）
                        _safe_decimal(revenue_f),        # $4  revenue
                        revenue_yoy,                     # $5  revenue_yoy
                        _safe_decimal(net_profit_f),     # $6  net_profit
                        np_yoy,                          # $7  np_yoy
                        gross_margin,                    # $8  gross_margin
                        net_margin,                      # $9  net_margin
                        _safe_decimal(total_assets_f),   # $10 total_assets
                        _safe_decimal(total_liab_f),     # $11 total_liab
                        debt_ratio,                      # $12 debt_ratio
                        current_ratio,                   # $13 current_ratio
                        _safe_decimal(ocf_f),            # $14 ocf
                        ocf_to_np,                       # $15 ocf_to_np
                        None,                            # $16 roe_ttm（需另行计算）
                        None,                            # $17 roa_ttm（需另行计算）
                    ))

                if not args_list:
                    sym_log.warning("pull_financials_no_rows")
                    result[symbol] = 0
                    await asyncio.sleep(_RATE_LIMIT_SLEEP)
                    continue

                upsert_sql = """
                    INSERT INTO financials_quarterly (
                        symbol, report_date, announce_date,
                        revenue, revenue_yoy, net_profit, np_yoy,
                        gross_margin, net_margin,
                        total_assets, total_liab, debt_ratio, current_ratio,
                        ocf, ocf_to_np, roe_ttm, roa_ttm
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8, $9,
                        $10, $11, $12, $13, $14, $15, $16, $17
                    )
                    ON CONFLICT (symbol, report_date) DO UPDATE SET
                        announce_date = EXCLUDED.announce_date,
                        revenue       = EXCLUDED.revenue,
                        revenue_yoy   = EXCLUDED.revenue_yoy,
                        net_profit    = EXCLUDED.net_profit,
                        np_yoy        = EXCLUDED.np_yoy,
                        gross_margin  = EXCLUDED.gross_margin,
                        net_margin    = EXCLUDED.net_margin,
                        total_assets  = EXCLUDED.total_assets,
                        total_liab    = EXCLUDED.total_liab,
                        debt_ratio    = EXCLUDED.debt_ratio,
                        current_ratio = EXCLUDED.current_ratio,
                        ocf           = EXCLUDED.ocf,
                        ocf_to_np     = EXCLUDED.ocf_to_np,
                        roe_ttm       = EXCLUDED.roe_ttm,
                        roa_ttm       = EXCLUDED.roa_ttm
                """
                await db_execute_many(upsert_sql, args_list)
                result[symbol] = len(args_list)
                sym_log.info("pull_financials_ok", rows=len(args_list))

            except Exception as e:
                sym_log.error("pull_financials_error", error=str(e))
                result[symbol] = 0

            await asyncio.sleep(_RATE_LIMIT_SLEEP)

        log.info("pull_financials_done", summary=result)
        return result

    # ------------------------------------------------------------------
    # 分钟线
    # ------------------------------------------------------------------

    async def pull_intraday(self, symbols: list[str]) -> dict[str, int]:
        """拉取 15 分钟线并写入 intraday_bars（market='US'）。

        Args:
            symbols: ticker 列表。

        Returns:
            {symbol: rows_saved} 字典。
        """
        log = logger.bind(market="US", module="us_data_pull")
        log.info("pull_intraday_start", symbols=len(symbols))

        result: dict[str, int] = {}

        for symbol in symbols:
            sym_log = log.bind(symbol=symbol)
            try:
                df = await self._yf.get_intraday_bars(symbol, interval="15m", period="7d")
                if df.empty:
                    sym_log.warning("pull_intraday_empty")
                    result[symbol] = 0
                    await asyncio.sleep(_RATE_LIMIT_SLEEP)
                    continue

                args_list: list[tuple] = []
                for idx, row in df.iterrows():
                    # bar_time 带时区，直接存 TIMESTAMPTZ
                    bar_time = idx if hasattr(idx, "tzinfo") else pd.Timestamp(idx)
                    args_list.append((
                        symbol,
                        "US",
                        bar_time,
                        "15m",
                        _safe_decimal(row.get("Open")),
                        _safe_decimal(row.get("High")),
                        _safe_decimal(row.get("Low")),
                        _safe_decimal(row.get("Close")),
                        _safe_int(row.get("Volume")),
                        None,   # amount（yfinance 无）
                        None,   # vwap
                    ))

                upsert_sql = """
                    INSERT INTO intraday_bars
                        (symbol, market, bar_time, interval,
                         open, high, low, close, volume, amount, vwap)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                    ON CONFLICT (symbol, bar_time, interval) DO UPDATE SET
                        open   = EXCLUDED.open,
                        high   = EXCLUDED.high,
                        low    = EXCLUDED.low,
                        close  = EXCLUDED.close,
                        volume = EXCLUDED.volume
                """
                await db_execute_many(upsert_sql, args_list)
                result[symbol] = len(args_list)
                sym_log.info("pull_intraday_ok", rows=len(args_list))

            except Exception as e:
                sym_log.error("pull_intraday_error", error=str(e))
                result[symbol] = 0

            await asyncio.sleep(_RATE_LIMIT_SLEEP)

        log.info("pull_intraday_done", summary=result)
        return result

    # ------------------------------------------------------------------
    # 候选池查询
    # ------------------------------------------------------------------

    async def get_us_universe(self) -> list[str]:
        """从 stock_universe 表读取市场为 US 的活跃股票列表。

        Returns:
            symbol 字符串列表，如 ['AAPL', 'NVDA', 'MSFT']。
        """
        rows = await db_query(
            "SELECT symbol FROM stock_universe WHERE market = 'US' AND active = TRUE"
        )
        return [r["symbol"] for r in rows]


# ============================================================
# CLI 入口
# ============================================================

async def _main() -> None:
    """CLI 主函数，解析参数并执行对应拉取操作。"""
    parser = argparse.ArgumentParser(
        description="P10-AlphaRadar 美股数据拉取管道",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--symbols",
        type=str,
        default="",
        help="逗号分隔的 ticker，如 AAPL,NVDA,MSFT",
    )
    parser.add_argument(
        "--universe",
        action="store_true",
        help="使用 stock_universe 表中市场为 US 的股票",
    )
    parser.add_argument(
        "--bars",
        action="store_true",
        help="拉取日线 OHLCV",
    )
    parser.add_argument(
        "--financials",
        action="store_true",
        help="拉取季报财务数据",
    )
    parser.add_argument(
        "--fundamentals",
        action="store_true",
        help="拉取当日 PE/PB/市值",
    )
    parser.add_argument(
        "--intraday",
        action="store_true",
        help="拉取 15 分钟线（最近 7 天）",
    )
    parser.add_argument(
        "--all",
        dest="pull_all",
        action="store_true",
        help="拉取所有类型（bars + financials + fundamentals）",
    )
    parser.add_argument(
        "--start",
        type=str,
        default="2020-01-01",
        help="日线开始日期，格式 YYYY-MM-DD（默认 2020-01-01）",
    )
    parser.add_argument(
        "--end",
        type=str,
        default=str(date_type.today()),
        help="日线结束日期，格式 YYYY-MM-DD（默认今天）",
    )

    args = parser.parse_args()

    # 初始化数据库连接池
    await init_pool()

    puller = USDataPuller()

    try:
        # 解析 symbol 列表
        if args.universe:
            symbols = await puller.get_us_universe()
            logger.info("us_universe_loaded", count=len(symbols))
        elif args.symbols:
            symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
        else:
            parser.error("需要 --symbols 或 --universe 参数")
            return  # unreachable，使 type checker 满意

        if not symbols:
            logger.warning("no_symbols_resolved")
            return

        logger.info("us_data_pull_start", symbols=symbols, args=vars(args))

        do_bars         = args.bars         or args.pull_all
        do_financials   = args.financials   or args.pull_all
        do_fundamentals = args.fundamentals or args.pull_all
        do_intraday     = args.intraday

        if do_bars:
            bar_result = await puller.pull_daily_bars(symbols, args.start, args.end)
            total_bar_rows = sum(bar_result.values())
            logger.info("cli_bars_done", total_rows=total_bar_rows, detail=bar_result)

        if do_financials:
            fin_result = await puller.pull_financials(symbols)
            total_fin_rows = sum(fin_result.values())
            logger.info("cli_financials_done", total_rows=total_fin_rows, detail=fin_result)

        if do_fundamentals:
            fund_result = await puller.pull_fundamentals(symbols, date_type.today())
            total_fund_rows = sum(fund_result.values())
            logger.info("cli_fundamentals_done", total_rows=total_fund_rows, detail=fund_result)

        if do_intraday:
            intra_result = await puller.pull_intraday(symbols)
            total_intra_rows = sum(intra_result.values())
            logger.info("cli_intraday_done", total_rows=total_intra_rows, detail=intra_result)

        if not any([do_bars, do_financials, do_fundamentals, do_intraday]):
            parser.print_help()

    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
