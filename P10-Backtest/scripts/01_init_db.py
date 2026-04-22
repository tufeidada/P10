#!/usr/bin/env python3
"""Initialize the backtest database by executing schema.sql.

Usage:
    python scripts/01_init_db.py [--reset]

Options:
    --reset    Drop and recreate the backtest schema (DESTRUCTIVE)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import asyncpg
import structlog
from dotenv import load_dotenv

# Add project root to path so db/ is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

logger = structlog.get_logger(__name__)

SCHEMA_FILE = Path(__file__).parent.parent / "db" / "schema.sql"


def configure_logging() -> None:
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(),
    )


async def drop_schema(conn: asyncpg.Connection) -> None:
    """Drop the backtest schema and all its objects."""
    logger.warning("dropping_backtest_schema")
    await conn.execute("DROP SCHEMA IF EXISTS backtest CASCADE")
    logger.info("schema_dropped")


async def init_db(dsn: str, reset: bool = False) -> None:
    """Connect to PostgreSQL and execute schema.sql.

    Args:
        dsn: PostgreSQL connection string.
        reset: If True, drop existing backtest schema first.
    """
    logger.info("connecting", host=dsn.split("@")[-1] if "@" in dsn else dsn)

    try:
        conn = await asyncpg.connect(dsn=dsn)
    except Exception as e:
        logger.error("connection_failed", error=str(e))
        logger.info(
            "hint",
            message="Make sure Docker is running: docker compose up -d",
        )
        sys.exit(1)

    try:
        if reset:
            await drop_schema(conn)

        # Read schema SQL
        if not SCHEMA_FILE.exists():
            logger.error("schema_not_found", path=str(SCHEMA_FILE))
            sys.exit(1)

        sql = SCHEMA_FILE.read_text(encoding="utf-8")
        logger.info("executing_schema", file=str(SCHEMA_FILE))

        # Execute schema SQL (split on ; to handle multi-statement)
        # asyncpg.execute() handles multi-statement strings directly
        await conn.execute(sql)

        logger.info("schema_applied_successfully")

        # Verify tables exist
        tables = await conn.fetch(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'backtest'
            ORDER BY table_name
            """
        )
        table_names = [r["table_name"] for r in tables]
        logger.info("tables_created", count=len(table_names), tables=table_names)

        expected_tables = [
            "backtest_judgments",
            "backtest_portfolio_daily",
            "backtest_positions",
            "backtest_regime_daily",
            "backtest_runs",
            "backtest_trades",
            "features_daily",
            "financials_quarterly",
            "fundamentals_daily",
            "index_daily",
            "industry_classify",
            "margin_daily",
            "margin_market_daily",
            "market_bars_daily",
            "market_breadth_daily",
            "moneyflow_daily",
            "northbound_daily",
            "trade_calendar",
            "universe",
        ]
        missing = [t for t in expected_tables if t not in table_names]
        if missing:
            logger.warning("missing_tables", tables=missing)
        else:
            logger.info("all_expected_tables_present")

    except Exception as e:
        logger.error("schema_execution_failed", error=str(e))
        raise
    finally:
        await conn.close()


def main() -> None:
    configure_logging()
    load_dotenv()

    parser = argparse.ArgumentParser(description="Initialize P10-Backtest database")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Drop and recreate backtest schema (DESTRUCTIVE — deletes all data)",
    )
    args = parser.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        logger.error("DATABASE_URL_not_set")
        logger.info("hint", message="Copy env.template to .env and fill in values")
        sys.exit(1)

    if args.reset:
        confirm = input(
            "WARNING: This will DROP all backtest data. Type 'yes' to confirm: "
        )
        if confirm.strip().lower() != "yes":
            logger.info("aborted")
            sys.exit(0)

    asyncio.run(init_db(dsn, reset=args.reset))
    logger.info("done")


if __name__ == "__main__":
    main()
