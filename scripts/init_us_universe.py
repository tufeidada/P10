#!/usr/bin/env python3
"""Initialize US stock universe with key stocks and pull initial historical data.

Inserts AAPL, NVDA, MSFT, SPY, QQQ into stock_universe, then pulls 1 year of
daily bars for all five, plus fundamentals and financials for the three stocks
(SPY/QQQ are ETFs with no meaningful fundamentals).

Usage:
    python scripts/init_us_universe.py
    python scripts/init_us_universe.py --dry-run    # skip DB writes, print plan
    python scripts/init_us_universe.py --start 2024-01-01  # custom start date
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import date, timedelta

import structlog

# Ensure project root is importable regardless of CWD
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.connection import close_pool, db_execute, init_pool
from data.pipeline.us_data_pull import USDataPuller

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="%H:%M:%S"),
        structlog.dev.ConsoleRenderer(),
    ],
)
logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Universe definition
# ---------------------------------------------------------------------------

# (symbol, name, industry, source)
# SPY/QQQ are tagged source='system' — used for regime calculation.
# Individual stocks are source='manual' — human-curated watchlist.
US_STOCKS: list[tuple[str, str, str, str]] = [
    ("AAPL",  "Apple Inc.",            "Technology",  "manual"),
    ("NVDA",  "NVIDIA Corporation",    "Technology",  "manual"),
    ("MSFT",  "Microsoft Corporation", "Technology",  "manual"),
    ("SPY",   "S&P 500 ETF",           "ETF",         "system"),
    ("QQQ",   "Nasdaq 100 ETF",        "ETF",         "system"),
]

# Symbols that have fundamentals/financials (exclude ETFs)
EQUITY_SYMBOLS: list[str] = [s for s, *_, src in US_STOCKS if src != "system"]
ALL_SYMBOLS:    list[str] = [s for s, *_ in US_STOCKS]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _upsert_universe(dry_run: bool) -> None:
    """Insert or update stock_universe rows for US stocks.

    Uses ON CONFLICT (symbol) DO UPDATE so it is safe to re-run.

    Args:
        dry_run: If True, only print what would be inserted.
    """
    today = date.today()
    for symbol, name, industry, source in US_STOCKS:
        if dry_run:
            logger.info("dry_run_universe", symbol=symbol, name=name,
                        industry=industry, source=source)
            continue
        await db_execute(
            """
            INSERT INTO stock_universe
                (symbol, market, name, industry, source, added_date, added_reason, status)
            VALUES ($1, 'US', $2, $3, $4, $5, $6, 'active')
            ON CONFLICT (symbol) DO UPDATE SET
                name        = EXCLUDED.name,
                industry    = EXCLUDED.industry,
                source      = EXCLUDED.source,
                status      = 'active',
                removed_date   = NULL,
                removed_reason = NULL
            """,
            symbol,
            name,
            industry,
            source,
            today,
            "init_us_universe script",
        )
        logger.info("universe_upserted", symbol=symbol, source=source)


# ---------------------------------------------------------------------------
# Main init flow
# ---------------------------------------------------------------------------

async def init(start: str, dry_run: bool) -> None:
    """Run the full US universe initialization.

    Steps:
        1. Upsert stock_universe rows for US_STOCKS.
        2. Pull 1 year of daily bars (all 5 symbols).
        3. Pull fundamentals for equity symbols (AAPL, NVDA, MSFT).
        4. Pull financials for equity symbols (AAPL, NVDA, MSFT).
        5. Print summary.

    Args:
        start: Start date for historical bar pull, format 'YYYY-MM-DD'.
        dry_run: If True, skip all DB writes and just print the plan.
    """
    end = date.today().strftime("%Y-%m-%d")
    logger.info("init_us_universe_start",
                symbols=ALL_SYMBOLS, start=start, end=end, dry_run=dry_run)

    # Step 1 — universe
    logger.info("step_1_upsert_universe")
    await _upsert_universe(dry_run)

    if dry_run:
        logger.info("dry_run_bars_plan",
                    symbols=ALL_SYMBOLS, start=start, end=end)
        logger.info("dry_run_fundamentals_plan", symbols=EQUITY_SYMBOLS)
        logger.info("dry_run_financials_plan", symbols=EQUITY_SYMBOLS)
        logger.info("dry_run_complete")
        return

    puller = USDataPuller()

    # Step 2 — daily bars (all symbols including SPY/QQQ)
    logger.info("step_2_pull_daily_bars", symbols=ALL_SYMBOLS)
    bars_result = await puller.pull_daily_bars(ALL_SYMBOLS, start, end)
    total_bars = sum(bars_result.values())
    logger.info("step_2_done", total_rows=total_bars, detail=bars_result)

    # Step 3 — fundamentals (equities only)
    logger.info("step_3_pull_fundamentals", symbols=EQUITY_SYMBOLS)
    fund_result = await puller.pull_fundamentals(EQUITY_SYMBOLS, date.today())
    total_fund = sum(fund_result.values())
    logger.info("step_3_done", total_rows=total_fund, detail=fund_result)

    # Step 4 — financials (equities only)
    logger.info("step_4_pull_financials", symbols=EQUITY_SYMBOLS)
    fin_result = await puller.pull_financials(EQUITY_SYMBOLS)
    total_fin = sum(fin_result.values())
    logger.info("step_4_done", total_rows=total_fin, detail=fin_result)

    # Summary
    logger.info(
        "init_us_universe_complete",
        universe_rows=len(US_STOCKS),
        bar_rows=total_bars,
        fundamental_rows=total_fund,
        financial_rows=total_fin,
    )
    print()
    print("=== Init US Universe Complete ===")
    print(f"  Universe inserted/updated : {len(US_STOCKS)}")
    print(f"  Daily bar rows written    : {total_bars}")
    print(f"  Fundamental rows written  : {total_fund}")
    print(f"  Financial rows written    : {total_fin}")
    print()
    print("Per-symbol bar rows:")
    for sym, n in sorted(bars_result.items()):
        mark = "OK" if n > 0 else "EMPTY"
        print(f"  {sym:<6} {n:>5} rows  [{mark}]")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Initialize US stock universe and pull 1 year of historical data.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    default_start = (date.today() - timedelta(days=365)).strftime("%Y-%m-%d")
    parser.add_argument(
        "--start",
        default=default_start,
        help=f"Start date for historical bars (default: 1 year ago = {default_start})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print plan without writing to DB",
    )
    return parser.parse_args()


async def _run() -> None:
    args = _parse_args()
    await init_pool()
    try:
        await init(start=args.start, dry_run=args.dry_run)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(_run())
