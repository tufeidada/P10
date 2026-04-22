#!/usr/bin/env python3
"""Backtest schema migration script.

Extends the existing P10 alpharadar database to support PIT-correct backtesting.
This script is fully idempotent — safe to run multiple times.

Operations performed:
  C.1  Add available_date to existing tables, backfill values
  C.2  Add adj_close / turnover_rate to market_bars_daily
  C.3  Create backtest_* result tables
  C.4  Create backtest_features_extra (future_ret_* fields, isolated from main)
  C.5  Create common tables: index_daily, market_breadth_daily

Usage:
    cd /Users/yangxuan/PycharmProjects/P10-AlphaRadar
    source .venv/bin/activate
    python backtest/scripts/01_migrate_schema.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import asyncpg
import structlog
from dotenv import load_dotenv

# Load parent project .env
load_dotenv(Path(__file__).parent.parent.parent / ".env")

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ]
)
logger = structlog.get_logger()

DSN = os.environ.get("DATABASE_URL", "")


# ──────────────────────────────────────────────────────────────────
# DDL helpers
# ──────────────────────────────────────────────────────────────────

async def col_exists(conn: asyncpg.Connection, table: str, col: str) -> bool:
    """Return True if column already exists."""
    return await conn.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = $1 AND column_name = $2
        )
        """,
        table, col,
    )


async def table_exists(conn: asyncpg.Connection, table: str) -> bool:
    return await conn.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = $1
        )
        """,
        table,
    )


async def index_exists(conn: asyncpg.Connection, index_name: str) -> bool:
    return await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE schemaname='public' AND indexname=$1)",
        index_name,
    )


async def add_column(conn: asyncpg.Connection, table: str, col: str, col_type: str) -> bool:
    """Add column if absent. Returns True if added.

    Uses a DO block for true idempotency on TimescaleDB hypertables,
    where information_schema may lag inside a transaction.
    """
    await conn.execute(
        f"""
        DO $$
        BEGIN
            ALTER TABLE {table} ADD COLUMN {col} {col_type};
        EXCEPTION WHEN duplicate_column THEN
            NULL;  -- already exists, skip silently
        END
        $$
        """
    )
    added = await col_exists(conn, table, col)
    if added:
        logger.debug("column_ensured", table=table, col=col)
    return added


async def ensure_index(conn: asyncpg.Connection, index_name: str, ddl: str) -> None:
    if not await index_exists(conn, index_name):
        await conn.execute(ddl)
        logger.info("index_created", name=index_name)


# ──────────────────────────────────────────────────────────────────
# C.1  available_date for existing tables
# ──────────────────────────────────────────────────────────────────

async def migrate_available_date(conn: asyncpg.Connection) -> dict[str, Any]:
    """Add available_date to all existing tables and backfill."""
    report: dict[str, Any] = {}

    # --- market_bars_daily: available_date = trade_date ---
    await add_column(conn, "market_bars_daily", "available_date", "DATE")
    res = await conn.execute(
        "UPDATE market_bars_daily SET available_date = trade_date WHERE available_date IS NULL"
    )
    report["market_bars_daily"] = {"backfilled": int(res.split()[-1])}
    await ensure_index(conn, "idx_mbd_available_date",
        "CREATE INDEX idx_mbd_available_date ON market_bars_daily (available_date)")
    logger.info("market_bars_daily.available_date done", **report["market_bars_daily"])

    # --- fundamentals_daily: available_date = trade_date ---
    await add_column(conn, "fundamentals_daily", "available_date", "DATE")
    res = await conn.execute(
        "UPDATE fundamentals_daily SET available_date = trade_date WHERE available_date IS NULL"
    )
    report["fundamentals_daily"] = {"backfilled": int(res.split()[-1])}
    await ensure_index(conn, "idx_fund_available_date",
        "CREATE INDEX idx_fund_available_date ON fundamentals_daily (symbol, available_date)")
    logger.info("fundamentals_daily.available_date done", **report["fundamentals_daily"])

    # --- features_daily: available_date = trade_date ---
    await add_column(conn, "features_daily", "available_date", "DATE")
    res = await conn.execute(
        "UPDATE features_daily SET available_date = trade_date WHERE available_date IS NULL"
    )
    report["features_daily"] = {"backfilled": int(res.split()[-1])}
    await ensure_index(conn, "idx_feat_available_date",
        "CREATE INDEX idx_feat_available_date ON features_daily (symbol, available_date)")
    logger.info("features_daily.available_date done", **report["features_daily"])

    # --- northbound_daily: available_date = trade_date ---
    await add_column(conn, "northbound_daily", "available_date", "DATE")
    res = await conn.execute(
        "UPDATE northbound_daily SET available_date = trade_date WHERE available_date IS NULL"
    )
    report["northbound_daily"] = {"backfilled": int(res.split()[-1])}
    await ensure_index(conn, "idx_nb_available_date",
        "CREATE INDEX idx_nb_available_date ON northbound_daily (available_date)")
    logger.info("northbound_daily.available_date done", **report["northbound_daily"])

    # --- moneyflow_daily: available_date = next trading day ---
    # trade_calendar contains only trading days; next trade day = MIN(t) where t > trade_date
    await add_column(conn, "moneyflow_daily", "available_date", "DATE")
    res = await conn.execute(
        """
        UPDATE moneyflow_daily m
        SET available_date = (
            SELECT MIN(t.trade_date)
            FROM trade_calendar t
            WHERE t.trade_date > m.trade_date
        )
        WHERE m.available_date IS NULL
        """
    )
    null_after = await conn.fetchval(
        "SELECT COUNT(*) FROM moneyflow_daily WHERE available_date IS NULL"
    )
    report["moneyflow_daily"] = {
        "backfilled": int(res.split()[-1]),
        "null_remaining": null_after,  # rows beyond max trade_calendar date
    }
    await ensure_index(conn, "idx_mf_available_date",
        "CREATE INDEX idx_mf_available_date ON moneyflow_daily (symbol, available_date)")
    logger.info("moneyflow_daily.available_date done", **report["moneyflow_daily"])

    # --- financials_quarterly: available_date = announce_date, fallback report_date+45 ---
    await add_column(conn, "financials_quarterly", "available_date", "DATE")
    # First pass: use announce_date where present
    res1 = await conn.execute(
        """
        UPDATE financials_quarterly
        SET available_date = announce_date
        WHERE available_date IS NULL AND announce_date IS NOT NULL
        """
    )
    # Second pass: fallback rows (announce_date IS NULL)
    fallback_rows = await conn.fetch(
        """
        SELECT symbol, report_date
        FROM financials_quarterly
        WHERE available_date IS NULL AND announce_date IS NULL
        """
    )
    if fallback_rows:
        logger.warning(
            "financials_quarterly_announce_null_fallback",
            count=len(fallback_rows),
            samples=[(r["symbol"], str(r["report_date"])) for r in fallback_rows[:5]],
        )
    res2 = await conn.execute(
        """
        UPDATE financials_quarterly
        SET available_date = report_date + INTERVAL '45 days'
        WHERE available_date IS NULL AND announce_date IS NULL
        """
    )
    report["financials_quarterly"] = {
        "backfilled_from_announce": int(res1.split()[-1]),
        "fallback_count": len(fallback_rows),
        "backfilled_from_fallback": int(res2.split()[-1]),
        "fallback_samples": [(r["symbol"], str(r["report_date"])) for r in fallback_rows[:5]],
    }
    await ensure_index(conn, "idx_fin_available_date",
        "CREATE INDEX idx_fin_available_date ON financials_quarterly (symbol, available_date)")
    logger.info("financials_quarterly.available_date done", **report["financials_quarterly"])

    return report


# ──────────────────────────────────────────────────────────────────
# C.2  market_bars_daily: adj_close + turnover_rate
# ──────────────────────────────────────────────────────────────────

async def migrate_market_bars_extra(conn: asyncpg.Connection) -> dict[str, Any]:
    """Add adj_close (backfill) and turnover_rate (leave NULL) to market_bars_daily."""
    report: dict[str, Any] = {}

    # adj_close = close * adj_factor
    await add_column(conn, "market_bars_daily", "adj_close", "NUMERIC(14,4)")
    res = await conn.execute(
        """
        UPDATE market_bars_daily
        SET adj_close = ROUND((close * COALESCE(adj_factor, 1))::NUMERIC, 4)
        WHERE adj_close IS NULL
        """
    )
    # Sanity check: adj_close should be within ±200% of close
    outlier_count = await conn.fetchval(
        """
        SELECT COUNT(*) FROM market_bars_daily
        WHERE adj_close IS NOT NULL
          AND ABS(adj_close - close) > close * 2
        """
    )
    report["adj_close"] = {
        "backfilled": int(res.split()[-1]),
        "outliers_gt_200pct": outlier_count,
    }
    logger.info("market_bars_daily.adj_close done", **report["adj_close"])

    # turnover_rate: leave NULL, will be filled from Tushare daily_basic later
    await add_column(conn, "market_bars_daily", "turnover_rate", "NUMERIC(8,4)")
    report["turnover_rate"] = {"status": "column_added_null_values"}
    logger.info("market_bars_daily.turnover_rate column added (values NULL, fill in step 02)")

    return report


# ──────────────────────────────────────────────────────────────────
# C.3  backtest_* result tables
# ──────────────────────────────────────────────────────────────────

BACKTEST_TABLES_DDL = [
    # backtest_runs
    """
    CREATE TABLE IF NOT EXISTS backtest_runs (
        run_id              SERIAL          PRIMARY KEY,
        run_timestamp       TIMESTAMPTZ     DEFAULT NOW(),
        start_date          DATE            NOT NULL,
        end_date            DATE            NOT NULL,
        initial_cash_cn     NUMERIC(18,2)   DEFAULT 1000000,
        initial_cash_us     NUMERIC(18,2)   DEFAULT 100000,
        config_snapshot     JSONB,
        status              VARCHAR(20),    -- 'running'|'completed'|'failed'
        notes               TEXT
    )
    """,
    # backtest_judgments
    """
    CREATE TABLE IF NOT EXISTS backtest_judgments (
        id                  SERIAL          PRIMARY KEY,
        run_id              INTEGER         REFERENCES backtest_runs(run_id),
        symbol              VARCHAR(20)     NOT NULL,
        market              VARCHAR(10)     NOT NULL,
        judgment_date       DATE            NOT NULL,
        technical_score     NUMERIC(6,2),
        fundamental_score   NUMERIC(6,2),
        flow_score          NUMERIC(6,2),
        sentiment_score     NUMERIC(6,2),
        composite_score     NUMERIC(6,2),
        regime_mode         VARCHAR(30),
        regime_snapshot     JSONB,
        direction           VARCHAR(10),
        confidence          NUMERIC(4,2),
        suggested_action    VARCHAR(30),
        entry_price         NUMERIC(14,4),
        stop_loss           NUMERIC(14,4),
        target_price        NUMERIC(14,4),
        suggested_size_pct  NUMERIC(6,4),
        actual_ret_5d       NUMERIC(10,6),
        actual_ret_10d      NUMERIC(10,6),
        actual_ret_20d      NUMERIC(10,6),
        actual_max_up_20d   NUMERIC(10,6),
        actual_max_dd_20d   NUMERIC(10,6),
        is_correct          BOOLEAN,
        signal_sources      JSONB
    )
    """,
    # backtest_trades
    """
    CREATE TABLE IF NOT EXISTS backtest_trades (
        id                      SERIAL      PRIMARY KEY,
        run_id                  INTEGER     REFERENCES backtest_runs(run_id),
        symbol                  VARCHAR(20) NOT NULL,
        market                  VARCHAR(10) NOT NULL,
        action                  VARCHAR(10) NOT NULL,   -- 'buy'|'sell'
        trade_date              DATE        NOT NULL,
        price                   NUMERIC(14,4),
        shares                  INTEGER,
        amount                  NUMERIC(18,2),
        commission              NUMERIC(12,2),
        trigger_judgment_id     INTEGER,
        trigger_reason          VARCHAR(50),
        portfolio_value_after   NUMERIC(18,2)
    )
    """,
    # backtest_portfolio_daily
    """
    CREATE TABLE IF NOT EXISTS backtest_portfolio_daily (
        run_id              INTEGER         REFERENCES backtest_runs(run_id),
        trade_date          DATE            NOT NULL,
        market              VARCHAR(10)     NOT NULL,
        cash                NUMERIC(18,2),
        positions_value     NUMERIC(18,2),
        total_value         NUMERIC(18,2),
        num_positions       INTEGER,
        position_pct        NUMERIC(6,4),
        daily_return        NUMERIC(10,6),
        cumulative_return   NUMERIC(12,6),
        benchmark_return_1  NUMERIC(10,6),
        benchmark_return_2  NUMERIC(10,6),
        benchmark_return_3  NUMERIC(10,6),
        PRIMARY KEY (run_id, trade_date, market)
    )
    """,
    # backtest_positions
    """
    CREATE TABLE IF NOT EXISTS backtest_positions (
        run_id              INTEGER         REFERENCES backtest_runs(run_id),
        trade_date          DATE            NOT NULL,
        symbol              VARCHAR(20)     NOT NULL,
        market              VARCHAR(10)     NOT NULL,
        shares              INTEGER,
        avg_cost            NUMERIC(14,4),
        current_price       NUMERIC(14,4),
        market_value        NUMERIC(18,2),
        unrealized_pnl      NUMERIC(18,2),
        unrealized_pnl_pct  NUMERIC(10,6),
        stop_loss           NUMERIC(14,4),
        target_price        NUMERIC(14,4),
        days_held           INTEGER,
        PRIMARY KEY (run_id, trade_date, symbol)
    )
    """,
    # backtest_regime_daily
    """
    CREATE TABLE IF NOT EXISTS backtest_regime_daily (
        run_id              INTEGER         REFERENCES backtest_runs(run_id),
        trade_date          DATE            NOT NULL,
        market              VARCHAR(10)     NOT NULL,
        trend_score         NUMERIC(6,2),
        volatility_score    NUMERIC(6,2),
        breadth_score       NUMERIC(6,2),
        liquidity_score     NUMERIC(6,2),
        regime_mode         VARCHAR(30),
        trend_direction     VARCHAR(10),
        volatility_env      VARCHAR(10),
        detail              JSONB,
        PRIMARY KEY (run_id, trade_date, market)
    )
    """,
]

BACKTEST_INDEXES_DDL = [
    ("idx_bj_run_date",   "CREATE INDEX IF NOT EXISTS idx_bj_run_date ON backtest_judgments (run_id, judgment_date)"),
    ("idx_bj_run_symbol", "CREATE INDEX IF NOT EXISTS idx_bj_run_symbol ON backtest_judgments (run_id, symbol)"),
    ("idx_bt_run_date",   "CREATE INDEX IF NOT EXISTS idx_bt_run_date ON backtest_trades (run_id, trade_date)"),
]


async def create_backtest_tables(conn: asyncpg.Connection) -> list[str]:
    created = []
    for ddl in BACKTEST_TABLES_DDL:
        table_name = ddl.strip().split("IF NOT EXISTS")[1].strip().split()[0]
        existed = await table_exists(conn, table_name)
        await conn.execute(ddl)
        if not existed:
            created.append(table_name)
            logger.info("backtest_table_created", table=table_name)
        else:
            logger.debug("backtest_table_already_exists", table=table_name)
    for idx_name, idx_ddl in BACKTEST_INDEXES_DDL:
        await conn.execute(idx_ddl)
    return created


# ──────────────────────────────────────────────────────────────────
# C.4  backtest_features_extra
# ──────────────────────────────────────────────────────────────────

FEATURES_EXTRA_DDL = """
CREATE TABLE IF NOT EXISTS backtest_features_extra (
    symbol              VARCHAR(20) NOT NULL,
    trade_date          DATE        NOT NULL,
    future_ret_5d       NUMERIC(10,6),
    future_ret_10d      NUMERIC(10,6),
    future_ret_20d      NUMERIC(10,6),
    future_max_up_20d   NUMERIC(10,6),
    future_max_dd_20d   NUMERIC(10,6),
    PRIMARY KEY (symbol, trade_date)
)
"""


async def create_features_extra(conn: asyncpg.Connection) -> bool:
    existed = await table_exists(conn, "backtest_features_extra")
    await conn.execute(FEATURES_EXTRA_DDL)
    if not existed:
        logger.info("backtest_features_extra created")
    return not existed


# ──────────────────────────────────────────────────────────────────
# C.5  Common tables: index_daily, market_breadth_daily
# ──────────────────────────────────────────────────────────────────

COMMON_TABLES_DDL = [
    (
        "index_daily",
        """
        CREATE TABLE IF NOT EXISTS index_daily (
            index_code      VARCHAR(20)     NOT NULL,
            trade_date      DATE            NOT NULL,
            open            NUMERIC(14,4),
            high            NUMERIC(14,4),
            low             NUMERIC(14,4),
            close           NUMERIC(14,4),
            volume          BIGINT,
            amount          NUMERIC(20,2),
            available_date  DATE            NOT NULL,
            PRIMARY KEY (index_code, trade_date)
        )
        """,
    ),
    (
        "market_breadth_daily",
        """
        CREATE TABLE IF NOT EXISTS market_breadth_daily (
            trade_date          DATE        NOT NULL PRIMARY KEY,
            market              VARCHAR(10) DEFAULT 'CN',
            limit_up_count      INTEGER,
            limit_down_count    INTEGER,
            advancing_count     INTEGER,
            declining_count     INTEGER,
            new_high_count      INTEGER,
            new_low_count       INTEGER,
            total_stocks        INTEGER,
            available_date      DATE        NOT NULL
        )
        """,
    ),
]

COMMON_INDEXES_DDL = [
    "CREATE INDEX IF NOT EXISTS idx_idx_daily_available ON index_daily (index_code, available_date)",
    "CREATE INDEX IF NOT EXISTS idx_breadth_available ON market_breadth_daily (available_date)",
]


async def create_common_tables(conn: asyncpg.Connection) -> list[str]:
    created = []
    for table_name, ddl in COMMON_TABLES_DDL:
        existed = await table_exists(conn, table_name)
        await conn.execute(ddl)
        if not existed:
            created.append(table_name)
            logger.info("common_table_created", table=table_name)
    for idx_ddl in COMMON_INDEXES_DDL:
        await conn.execute(idx_ddl)
    return created


# ──────────────────────────────────────────────────────────────────
# D  Verification report
# ──────────────────────────────────────────────────────────────────

async def build_verification_report(conn: asyncpg.Connection) -> None:
    logger.info("=" * 60)
    logger.info("MIGRATION VERIFICATION REPORT")
    logger.info("=" * 60)

    # Table row counts and available_date stats
    tables_with_available_date = [
        "market_bars_daily",
        "fundamentals_daily",
        "features_daily",
        "northbound_daily",
        "moneyflow_daily",
        "financials_quarterly",
    ]
    for tbl in tables_with_available_date:
        row = await conn.fetchrow(
            f"""
            SELECT
                COUNT(*)                                AS total_rows,
                SUM(CASE WHEN available_date IS NULL THEN 1 ELSE 0 END) AS null_count,
                MIN(available_date)                     AS min_date,
                MAX(available_date)                     AS max_date
            FROM {tbl}
            """
        )
        logger.info(
            f"table_stats: {tbl}",
            total=row["total_rows"],
            available_date_nulls=row["null_count"],
            date_range=f"{row['min_date']} ~ {row['max_date']}",
        )

    # adj_close check
    adj_row = await conn.fetchrow(
        """
        SELECT
            COUNT(*)                                    AS total,
            SUM(CASE WHEN adj_close IS NULL THEN 1 ELSE 0 END) AS null_adj,
            SUM(CASE WHEN ABS(adj_close - close) > close * 2 THEN 1 ELSE 0 END) AS outliers
        FROM market_bars_daily
        """
    )
    logger.info(
        "adj_close_check",
        total=adj_row["total"],
        null_count=adj_row["null_adj"],
        outliers_gt_200pct=adj_row["outliers"],
    )

    # backtest + common tables existence
    all_expected = [
        "backtest_runs", "backtest_judgments", "backtest_trades",
        "backtest_portfolio_daily", "backtest_positions", "backtest_regime_daily",
        "backtest_features_extra", "index_daily", "market_breadth_daily",
    ]
    for tbl in all_expected:
        exists = await table_exists(conn, tbl)
        rows = await conn.fetchval(f"SELECT COUNT(*) FROM {tbl}") if exists else "N/A"
        status = "✓" if exists else "✗ MISSING"
        logger.info(f"new_table: {tbl}", status=status, rows=rows)

    # financials fallback detail
    fallback = await conn.fetchrow(
        """
        SELECT
            COUNT(*) FILTER (WHERE announce_date IS NULL) AS null_announce,
            COUNT(*) FILTER (WHERE announce_date IS NULL
                             AND available_date = report_date + 45) AS used_fallback
        FROM financials_quarterly
        """
    )
    logger.info(
        "financials_quarterly_pit_check",
        null_announce_date=fallback["null_announce"],
        used_45day_fallback=fallback["used_fallback"],
    )
    logger.info("=" * 60)


# ──────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────

async def main() -> None:
    if not DSN:
        logger.error("DATABASE_URL not set — check .env in project root")
        sys.exit(1)

    logger.info("connecting", dsn=DSN.split("@")[-1])
    conn = await asyncpg.connect(dsn=DSN)

    try:
        async with conn.transaction():
            logger.info("── C.1  available_date fields ──────────────────")
            avail_report = await migrate_available_date(conn)

            logger.info("── C.2  market_bars_daily extra columns ────────")
            bars_report = await migrate_market_bars_extra(conn)

            logger.info("── C.3  backtest_* result tables ───────────────")
            created_backtest = await create_backtest_tables(conn)

            logger.info("── C.4  backtest_features_extra ────────────────")
            await create_features_extra(conn)

            logger.info("── C.5  common tables ──────────────────────────")
            created_common = await create_common_tables(conn)

        logger.info("transaction committed")

        await build_verification_report(conn)

    except Exception:
        logger.exception("migration_failed")
        raise
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
