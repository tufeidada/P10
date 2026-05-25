"""
PITDataLoader 单元测试

Schema 生命周期：setup_module / teardown_module 用 asyncio.run() 同步管理，
每个测试函数创建自己的独立连接池，完全避免跨事件循环问题。

运行方式:
    cd "P10-AlphaRadar "
    pytest backtest/tests/test_pit_loader.py -v --asyncio-mode=auto
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import date
from pathlib import Path

import asyncpg
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from backtest.pit_loader import PITDataLoader, create_pool  # noqa: E402

# ─── 配置 ────────────────────────────────────────────────────────────────────

_DSN = os.environ.get(
    "DATABASE_URL",
    "postgresql://radar:alpharadar2026@localhost:5434/alpharadar",
)
_SCHEMA = f"test_pit_{os.getpid()}"

TEST_SYMBOL = "TEST.SZ"

# ─── Schema DDL ──────────────────────────────────────────────────────────────

def _ddl(schema: str) -> str:
    return f"""
CREATE SCHEMA IF NOT EXISTS {schema};

CREATE TABLE {schema}.trade_calendar (
    trade_date DATE NOT NULL PRIMARY KEY
);

CREATE TABLE {schema}.market_bars_daily (
    symbol         VARCHAR(20) NOT NULL,
    market         VARCHAR(10) NOT NULL DEFAULT 'CN',
    trade_date     DATE NOT NULL,
    open           NUMERIC(14,4),
    high           NUMERIC(14,4),
    low            NUMERIC(14,4),
    close          NUMERIC(14,4),
    volume         BIGINT,
    amount         NUMERIC(20,2),
    adj_factor     NUMERIC(12,6) DEFAULT 1,
    adj_close      NUMERIC(14,4),
    turnover       NUMERIC(8,4),
    turnover_rate  NUMERIC(8,4),
    available_date DATE NOT NULL,
    PRIMARY KEY (symbol, trade_date)
);

CREATE TABLE {schema}.features_daily (
    symbol             VARCHAR(20) NOT NULL,
    trade_date         DATE NOT NULL,
    ma5                NUMERIC(14,4),
    ma10               NUMERIC(14,4),
    ma20               NUMERIC(14,4),
    ma60               NUMERIC(14,4),
    ma150              NUMERIC(14,4),
    ma200              NUMERIC(14,4),
    ma5_slope          NUMERIC(10,6),
    ma20_slope         NUMERIC(10,6),
    ma60_slope         NUMERIC(10,6),
    rsi_14             NUMERIC(8,4),
    macd_dif           NUMERIC(12,6),
    macd_dea           NUMERIC(12,6),
    macd_hist          NUMERIC(12,6),
    adx_14             NUMERIC(8,4),
    plus_di            NUMERIC(8,4),
    minus_di           NUMERIC(8,4),
    atr_14             NUMERIC(14,4),
    hv_20              NUMERIC(10,6),
    boll_upper         NUMERIC(14,4),
    boll_lower         NUMERIC(14,4),
    boll_width         NUMERIC(10,6),
    ret_1d             NUMERIC(10,6),
    ret_5d             NUMERIC(10,6),
    ret_20d            NUMERIC(10,6),
    ret_60d            NUMERIC(10,6),
    dist_20d_high      NUMERIC(10,6),
    dist_60d_high      NUMERIC(10,6),
    pct_in_20d_range   NUMERIC(8,4),
    vol_ratio_5d       NUMERIC(10,4),
    turnover_rank_20d  NUMERIC(8,4),
    stage              SMALLINT,
    rs_rank            NUMERIC(8,4),
    available_date     DATE NOT NULL,
    PRIMARY KEY (symbol, trade_date)
);

CREATE TABLE {schema}.fundamentals_daily (
    symbol          VARCHAR(20) NOT NULL,
    trade_date      DATE NOT NULL,
    pe_ttm          NUMERIC(14,4),
    pb              NUMERIC(14,4),
    ps_ttm          NUMERIC(14,4),
    total_mv        NUMERIC(20,2),
    circ_mv         NUMERIC(20,2),
    turnover_rate_f NUMERIC(8,4),
    available_date  DATE NOT NULL,
    PRIMARY KEY (symbol, trade_date)
);

CREATE TABLE {schema}.financials_quarterly (
    symbol         VARCHAR(20) NOT NULL,
    report_date    DATE NOT NULL,
    announce_date  DATE,
    revenue        NUMERIC(20,2),
    revenue_yoy    NUMERIC(10,4),
    revenue_qoq    NUMERIC(10,4),
    net_profit     NUMERIC(20,2),
    np_yoy         NUMERIC(10,4),
    gross_margin   NUMERIC(10,4),
    net_margin     NUMERIC(10,4),
    total_assets   NUMERIC(20,2),
    total_liab     NUMERIC(20,2),
    debt_ratio     NUMERIC(10,4),
    current_ratio  NUMERIC(10,4),
    goodwill       NUMERIC(20,2),
    ocf            NUMERIC(20,2),
    ocf_to_np      NUMERIC(10,4),
    roe_ttm        NUMERIC(10,4),
    roa_ttm        NUMERIC(10,4),
    dupont_npm     NUMERIC(10,4),
    dupont_tat     NUMERIC(10,4),
    dupont_em      NUMERIC(10,4),
    available_date DATE NOT NULL,
    PRIMARY KEY (symbol, report_date)
);

CREATE TABLE {schema}.moneyflow_daily (
    symbol         VARCHAR(20) NOT NULL,
    trade_date     DATE NOT NULL,
    net_lg_amount  NUMERIC(20,2),
    net_md_amount  NUMERIC(20,2),
    net_sm_amount  NUMERIC(20,2),
    available_date DATE NOT NULL,
    PRIMARY KEY (symbol, trade_date)
);

CREATE TABLE {schema}.northbound_daily (
    trade_date     DATE NOT NULL PRIMARY KEY,
    sh_net_buy     NUMERIC(18,2),
    sz_net_buy     NUMERIC(18,2),
    total_net_buy  NUMERIC(18,2),
    available_date DATE NOT NULL
);

CREATE TABLE {schema}.index_daily (
    index_code     VARCHAR(20) NOT NULL,
    trade_date     DATE NOT NULL,
    open           NUMERIC(14,4),
    high           NUMERIC(14,4),
    low            NUMERIC(14,4),
    close          NUMERIC(14,4),
    volume         BIGINT,
    available_date DATE NOT NULL,
    PRIMARY KEY (index_code, trade_date)
);

CREATE TABLE {schema}.market_breadth_daily (
    trade_date       DATE NOT NULL PRIMARY KEY,
    market           VARCHAR(10) DEFAULT 'CN',
    limit_up_count   INTEGER,
    limit_down_count INTEGER,
    advancing_count  INTEGER,
    declining_count  INTEGER,
    new_high_count   INTEGER,
    new_low_count    INTEGER,
    total_stocks     INTEGER,
    available_date   DATE NOT NULL
);

CREATE TABLE {schema}.margin_daily (
    symbol         VARCHAR(20) NOT NULL,
    trade_date     DATE NOT NULL,
    rzye           NUMERIC(20,2),
    rzmre          NUMERIC(20,2),
    available_date DATE NOT NULL,
    PRIMARY KEY (symbol, trade_date)
);
"""


# ─── 同步 Schema 生命周期（setup_module / teardown_module）──────────────────

async def _create_schema_async() -> None:
    conn = await asyncpg.connect(_DSN)
    try:
        await conn.execute(_ddl(_SCHEMA))
        # 与 P10 实际 trade_calendar 对齐：只有 trade_date 一列
        calendar_dates = [
            (date(2025, 1, 7),),  (date(2025, 1, 8),),  (date(2025, 1, 9),),
            (date(2025, 1, 10),), (date(2025, 1, 11),), (date(2025, 1, 12),),
            (date(2025, 1, 13),), (date(2025, 1, 14),), (date(2025, 1, 15),),
            (date(2025, 1, 16),), (date(2025, 1, 30),), (date(2025, 1, 31),),
            (date(2025, 2, 1),),  (date(2025, 3, 28),), (date(2025, 3, 31),),
            (date(2025, 4, 1),),
        ]
        await conn.executemany(
            f"INSERT INTO {_SCHEMA}.trade_calendar (trade_date) VALUES($1) ON CONFLICT DO NOTHING",
            calendar_dates,
        )
    finally:
        await conn.close()


async def _drop_schema_async() -> None:
    conn = await asyncpg.connect(_DSN)
    try:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE;")
    finally:
        await conn.close()


def setup_module(_module=None) -> None:
    asyncio.run(_create_schema_async())


def teardown_module(_module=None) -> None:
    asyncio.run(_drop_schema_async())


# ─── 函数级 Fixtures（每个测试有独立连接池）──────────────────────────────────

@pytest.fixture
async def pool():
    """每个测试函数独立创建/关闭连接池，完全避免跨事件循环。"""
    p = await create_pool(_DSN, min_size=1, max_size=3)
    yield p
    await p.close()


@pytest.fixture
def loader(pool):
    return PITDataLoader(pool, schema=_SCHEMA)


# ─── 辅助 ────────────────────────────────────────────────────────────────────

async def _insert_bars(pool: asyncpg.Pool, symbol: str, rows: list[tuple]) -> None:
    """rows: (trade_date, open, high, low, close, available_date)"""
    async with pool.acquire() as conn:
        await conn.executemany(
            f"""INSERT INTO {_SCHEMA}.market_bars_daily
                (symbol, market, trade_date, open, high, low, close,
                 volume, amount, adj_factor, adj_close, turnover, available_date)
                VALUES ($1,'CN',$2,$3,$4,$5,$6,100000,1000000.0,1.0,$6,0.01,$7)
                ON CONFLICT DO NOTHING""",
            [(symbol, *r) for r in rows],
        )


# ═════════════════════════════════════════════════════════════════════════════
# 测试 1 — 基础日线 PIT
# ═════════════════════════════════════════════════════════════════════════════

async def test_get_bars_pit_cutoff(pool, loader):
    """set_date(2025-01-15)：只能看到 2025-01-01，不能看 2025-02-01。"""
    await _insert_bars(pool, TEST_SYMBOL, [
        (date(2025, 1, 1),  100.0, 105.0, 99.0,  102.0, date(2025, 1, 1)),
        (date(2025, 2, 1),  110.0, 115.0, 109.0, 112.0, date(2025, 2, 1)),
    ])

    loader.set_date(date(2025, 1, 15))
    df = await loader.get_bars(TEST_SYMBOL)

    assert not df.empty, "应有数据"
    dates = set(df["trade_date"].tolist())
    assert date(2025, 1, 1) in dates, "2025-01-01 应可见"
    assert date(2025, 2, 1) not in dates, "2025-02-01 是未来数据，不应可见"


# ═════════════════════════════════════════════════════════════════════════════
# 测试 2 — 财报 PIT（announce_date 过滤）
# ═════════════════════════════════════════════════════════════════════════════

async def test_get_latest_financials_pit(pool, loader):
    """2024 Q4 财报 announce_date=2025-03-15：2025-02-01 不可见，2025-04-01 可见。"""
    async with pool.acquire() as conn:
        await conn.execute(
            f"""INSERT INTO {_SCHEMA}.financials_quarterly
                (symbol, report_date, announce_date,
                 revenue, revenue_yoy, revenue_qoq,
                 net_profit, np_yoy, gross_margin, net_margin,
                 total_assets, total_liab, debt_ratio, current_ratio, goodwill,
                 ocf, ocf_to_np, roe_ttm, roa_ttm,
                 dupont_npm, dupont_tat, dupont_em, available_date)
                VALUES ($1,$2,$3,
                        1e9,0.1,0.05,
                        1e8,0.2,0.35,0.1,
                        5e9,2e9,0.4,2.0,1e8,
                        8e7,0.8,0.15,0.05,
                        0.1,0.8,2.0,$3)
                ON CONFLICT DO NOTHING""",
            TEST_SYMBOL, date(2024, 12, 31), date(2025, 3, 15),
        )

    loader.set_date(date(2025, 2, 1))
    df_before = await loader.get_latest_financials(TEST_SYMBOL)
    assert df_before.empty, \
        f"announce_date=2025-03-15，在 2025-02-01 时不应可见（实际 {len(df_before)} 行）"

    loader.set_date(date(2025, 4, 1))
    df_after = await loader.get_latest_financials(TEST_SYMBOL)
    assert len(df_after) == 1, \
        f"2025-04-01 应看到 1 行财报（实际 {len(df_after)} 行）"
    assert df_after.iloc[0]["report_date"] == date(2024, 12, 31)


# ═════════════════════════════════════════════════════════════════════════════
# 测试 3 — 资金流 T+1 规则
# ═════════════════════════════════════════════════════════════════════════════

async def test_get_moneyflow_t_plus_1(pool, loader):
    """trade_date=2025-01-10，available_date=2025-01-11：T 日不可查，T+2 可查。"""
    async with pool.acquire() as conn:
        await conn.execute(
            f"""INSERT INTO {_SCHEMA}.moneyflow_daily
                (symbol, trade_date, net_lg_amount, net_md_amount, net_sm_amount, available_date)
                VALUES ($1,$2,1e6,-5e5,-5e5,$3) ON CONFLICT DO NOTHING""",
            TEST_SYMBOL, date(2025, 1, 10), date(2025, 1, 11),
        )

    loader.set_date(date(2025, 1, 10))
    df_t = await loader.get_moneyflow(TEST_SYMBOL)
    t10 = df_t[df_t["trade_date"] == date(2025, 1, 10)] if not df_t.empty else df_t
    assert t10.empty, "T 日（2025-01-10）不应能查到当日资金流（available=T+1）"

    loader.set_date(date(2025, 1, 12))
    df_t2 = await loader.get_moneyflow(TEST_SYMBOL)
    t10_visible = df_t2[df_t2["trade_date"] == date(2025, 1, 10)]
    assert not t10_visible.empty, "2025-01-12 应能查到 2025-01-10 的资金流"


# ═════════════════════════════════════════════════════════════════════════════
# 测试 4 — available_date 边界严格性
# ═════════════════════════════════════════════════════════════════════════════

async def test_available_date_boundary(pool, loader):
    """available_date=2025-01-15 的指数记录：
    - set_date(2025-01-14) → prev=2025-01-13 → 不可见
    - set_date(2025-01-15) → prev=2025-01-14 → 不可见（14 < 15）
    - set_date(2025-01-16) → prev=2025-01-15 → 可见（15 <= 15）
    """
    async with pool.acquire() as conn:
        await conn.execute(
            f"""INSERT INTO {_SCHEMA}.index_daily
                (index_code, trade_date, open, high, low, close, volume, available_date)
                VALUES ('BDY_TEST',$1,10.0,10.5,9.8,10.2,100000,$2) ON CONFLICT DO NOTHING""",
            date(2025, 1, 14), date(2025, 1, 15),
        )

    loader.set_date(date(2025, 1, 14))
    assert (await loader.get_index("BDY_TEST")).empty, \
        "prev=2025-01-13，available_date=15 不可见"

    loader.set_date(date(2025, 1, 15))
    assert (await loader.get_index("BDY_TEST")).empty, \
        "prev=2025-01-14，available_date=15 仍不可见"

    loader.set_date(date(2025, 1, 16))
    df = await loader.get_index("BDY_TEST")
    assert not df.empty, "prev=2025-01-15，available_date=15 应可见"


# ═════════════════════════════════════════════════════════════════════════════
# 测试 5 — 字段映射（业务层看 Spec 名称）
# ═════════════════════════════════════════════════════════════════════════════

async def test_field_mapping_turnover_and_rs_rank(pool, loader):
    """get_bars 暴露 turnover_rate（COALESCE），get_features 暴露 rs_rank_63d。"""
    async with pool.acquire() as conn:
        await conn.execute(
            f"""INSERT INTO {_SCHEMA}.market_bars_daily
                (symbol, market, trade_date, open, high, low, close,
                 volume, amount, adj_factor, adj_close,
                 turnover, turnover_rate, available_date)
                VALUES ('MAP.SZ','CN',$1,50,55,49,52,
                        200000,10000000,1.0,52,
                        0.025,NULL,$1)
                ON CONFLICT DO NOTHING""",
            date(2025, 1, 1),
        )
        await conn.execute(
            f"""INSERT INTO {_SCHEMA}.features_daily
                (symbol, trade_date, ma20, rs_rank, available_date)
                VALUES ('MAP.SZ',$1,50.0,78.5,$1) ON CONFLICT DO NOTHING""",
            date(2025, 1, 1),
        )

    loader.set_date(date(2025, 1, 15))

    bars = await loader.get_bars("MAP.SZ")
    assert not bars.empty
    assert "turnover_rate" in bars.columns, "应暴露 turnover_rate"
    assert "turnover" not in bars.columns, "不应暴露内部 turnover 列"
    assert abs(float(bars.iloc[0]["turnover_rate"]) - 0.025) < 1e-6, \
        f"COALESCE(NULL, 0.025) 应=0.025，实际={bars.iloc[0]['turnover_rate']}"

    feats = await loader.get_features("MAP.SZ")
    assert not feats.empty
    assert "rs_rank_63d" in feats.columns, "应暴露 rs_rank_63d"
    assert "rs_rank" not in feats.columns, "不应暴露内部 rs_rank 列"
    assert abs(float(feats.iloc[0]["rs_rank_63d"]) - 78.5) < 1e-4, \
        f"rs_rank_63d 应=78.5，实际={feats.iloc[0]['rs_rank_63d']}"


# ═════════════════════════════════════════════════════════════════════════════
# 测试 6 — get_open_price 唯一豁免 PIT 的方法
# ═════════════════════════════════════════════════════════════════════════════

async def test_get_open_price_future_exempt(pool, loader):
    """set_date(2025-01-10)，get_open_price 可以查询 2025-01-15（未来）的开盘价。

    这是 PITDataLoader 唯一允许访问未来数据的方法，用于模拟 T+1 成交。
    """
    await _insert_bars(pool, "EXEC.SZ", [
        (date(2025, 1, 15), 88.0, 92.0, 87.5, 90.0, date(2025, 1, 15)),
    ])

    loader.set_date(date(2025, 1, 10))

    open_price = await loader.get_open_price("EXEC.SZ", date(2025, 1, 15))
    assert open_price is not None, "get_open_price 应能查到未来日期的开盘价"
    assert abs(open_price - 88.0) < 1e-4, f"开盘价应为 88.0，实际={open_price}"

    assert await loader.get_open_price("EXEC.SZ", date(2030, 1, 1)) is None, \
        "无数据时应返回 None"
