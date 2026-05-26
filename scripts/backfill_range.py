"""Backfill market data + features + regime + composite for an explicit date range.

Workaround for the rolling 3-day window built into the scheduler jobs.
Usage:
    python scripts/backfill_range.py --start 2026-05-12 --end 2026-05-25
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path

# Ensure project root on path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import structlog

from db.connection import init_pool, close_pool, db_query, db_query_val
from db.universe import get_active_symbols

logger = structlog.get_logger(__name__)


async def _trading_days(start: date, end: date, market: str) -> list[date]:
    """Return trading days between start..end inclusive.

    For CN we use trade_calendar (only CN dates). For US we approximate by
    weekday (Mon-Fri) since the calendar table only contains CN dates.
    """
    if market == "CN":
        rows = await db_query(
            "SELECT trade_date FROM trade_calendar "
            "WHERE trade_date BETWEEN $1 AND $2 ORDER BY trade_date",
            start, end,
        )
        return [r["trade_date"] for r in rows]
    days = []
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            days.append(cur)
        cur += timedelta(days=1)
    return days


async def backfill_cn_bars(start: date, end: date) -> int:
    from data.sources.tushare_client import TushareClient
    import pandas as pd

    client = TushareClient()
    symbols = await get_active_symbols("CN")
    if not symbols:
        return 0
    start_str = start.strftime("%Y%m%d")
    end_str = end.strftime("%Y%m%d")
    frames = []
    for sym in symbols:
        try:
            df = await client.fetch_daily_bars(sym, start_str, end_str)
            if df is not None and not df.empty:
                frames.append(df)
        except Exception as e:
            logger.warning("cn_bar_fetch_skip", symbol=sym, error=str(e))
    if not frames:
        return 0
    df_all = pd.concat(frames, ignore_index=True)
    return await client.save_daily_bars(df_all, market="CN")


async def backfill_cn_basic_flow(days: list[date]) -> tuple[int, int]:
    from data.pipeline.fundamental_pull import FundamentalPuller
    from data.pipeline.flow_pull import FlowPuller

    fund = FundamentalPuller()
    flow = FlowPuller()
    basic_total = 0
    flow_total = 0
    for d in days:
        ds = d.strftime("%Y%m%d")
        b = await fund.pull_daily_basic(ds)
        basic_total += b or 0
        f = await flow.pull_all(ds)
        flow_total += sum(f.values()) if isinstance(f, dict) else 0
    return basic_total, flow_total


async def backfill_us_bars(start: date, end: date) -> dict:
    from data.pipeline.us_data_pull import USDataPuller

    puller = USDataPuller()
    symbols = await puller.get_us_universe()
    if not symbols:
        return {}
    # USDataPuller's pull_daily_bars: (symbols, start: str, end: str, end exclusive)
    return await puller.pull_daily_bars(
        symbols,
        start.isoformat(),
        (end + timedelta(days=1)).isoformat(),
    )


async def backfill_features(days: list[date], market: str) -> None:
    from data.pipeline.feature_compute import FeatureComputer
    comp = FeatureComputer()
    for d in days:
        await comp.compute_for_universe(market=market, trade_date=d)


async def backfill_regime(days: list[date], market: str) -> None:
    from core.regime.detector import detect_regime
    for d in days:
        r = await detect_regime(market=market, trade_date=d)
        logger.info("regime_backfilled", market=market, date=str(d), mode=r.regime_mode)


async def main(args: argparse.Namespace) -> None:
    await init_pool()
    try:
        start = date.fromisoformat(args.start)
        end = date.fromisoformat(args.end)

        cn_days = await _trading_days(start, end, "CN")
        us_days = await _trading_days(start, end, "US")
        logger.info("backfill_plan",
                    cn_days=[str(d) for d in cn_days],
                    us_days=[str(d) for d in us_days])

        if args.cn and cn_days:
            logger.info("backfill_cn_bars_start")
            saved = await backfill_cn_bars(cn_days[0], cn_days[-1])
            logger.info("backfill_cn_bars_done", rows=saved)

            logger.info("backfill_cn_basic_flow_start")
            b, f = await backfill_cn_basic_flow(cn_days)
            logger.info("backfill_cn_basic_flow_done", basic=b, flow=f)

            logger.info("backfill_cn_features_start")
            await backfill_features(cn_days, "CN")

            logger.info("backfill_cn_regime_start")
            await backfill_regime(cn_days, "CN")

        if args.us and us_days:
            logger.info("backfill_us_bars_start")
            res = await backfill_us_bars(us_days[0], us_days[-1])
            logger.info("backfill_us_bars_done", result=res)

            logger.info("backfill_us_features_start")
            await backfill_features(us_days, "US")

            logger.info("backfill_us_regime_start")
            await backfill_regime(us_days, "US")

        logger.info("backfill_all_done")
    finally:
        await close_pool()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    parser.add_argument("--cn", action="store_true", default=True)
    parser.add_argument("--us", action="store_true", default=True)
    parser.add_argument("--no-cn", dest="cn", action="store_false")
    parser.add_argument("--no-us", dest="us", action="store_false")
    args = parser.parse_args()
    asyncio.run(main(args))
