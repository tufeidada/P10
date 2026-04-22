-- P10-Backtest Database Schema
-- All tables in 'backtest' schema, isolated from future P10 production data
-- PIT (Point-in-Time) principle: every table has available_date field

CREATE SCHEMA IF NOT EXISTS backtest;
SET search_path TO backtest, public;

-- ============================================================
-- 5.1 Core Data Tables
-- ============================================================

-- Universe (candidate pool)
CREATE TABLE IF NOT EXISTS universe (
    symbol              VARCHAR(20)  NOT NULL PRIMARY KEY,
    market              VARCHAR(10)  NOT NULL,   -- 'CN' | 'US'
    name                VARCHAR(100),
    industry            VARCHAR(50),
    industry_framework  VARCHAR(30),             -- 'consumer_staples' | 'technology' | ...
    added_date          DATE,
    notes               TEXT
);

-- Daily OHLCV (core data, must be PIT)
CREATE TABLE IF NOT EXISTS market_bars_daily (
    symbol          VARCHAR(20)     NOT NULL,
    market          VARCHAR(10)     NOT NULL,
    trade_date      DATE            NOT NULL,
    open            NUMERIC(14,4),
    high            NUMERIC(14,4),
    low             NUMERIC(14,4),
    close           NUMERIC(14,4),
    volume          BIGINT,
    amount          NUMERIC(20,2),
    adj_close       NUMERIC(14,4),              -- 复权收盘价
    adj_factor      NUMERIC(12,6)  DEFAULT 1,
    turnover_rate   NUMERIC(8,4),               -- 换手率
    available_date  DATE            NOT NULL,   -- PIT: 等于 trade_date，当日收盘后可用
    PRIMARY KEY (symbol, trade_date)
);
SELECT create_hypertable('backtest.market_bars_daily', 'trade_date', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_mbd_available ON backtest.market_bars_daily (available_date);
CREATE INDEX IF NOT EXISTS idx_mbd_symbol ON backtest.market_bars_daily (symbol, trade_date DESC);

-- Daily fundamental metrics
CREATE TABLE IF NOT EXISTS fundamentals_daily (
    symbol          VARCHAR(20)     NOT NULL,
    trade_date      DATE            NOT NULL,
    pe_ttm          NUMERIC(14,4),
    pb              NUMERIC(14,4),
    ps_ttm          NUMERIC(14,4),
    total_mv        NUMERIC(20,2),
    circ_mv         NUMERIC(20,2),
    turnover_rate_f NUMERIC(8,4),
    available_date  DATE            NOT NULL,
    PRIMARY KEY (symbol, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_fund_available ON backtest.fundamentals_daily (symbol, available_date);

-- Quarterly financials (key PIT table)
CREATE TABLE IF NOT EXISTS financials_quarterly (
    symbol          VARCHAR(20)     NOT NULL,
    report_date     DATE            NOT NULL,   -- 报告期
    announce_date   DATE            NOT NULL,   -- 公告日（PIT 关键！）
    revenue         NUMERIC(20,2),
    revenue_yoy     NUMERIC(10,4),
    revenue_qoq     NUMERIC(10,4),
    net_profit      NUMERIC(20,2),
    np_yoy          NUMERIC(10,4),
    gross_margin    NUMERIC(10,4),
    net_margin      NUMERIC(10,4),
    total_assets    NUMERIC(20,2),
    total_liab      NUMERIC(20,2),
    debt_ratio      NUMERIC(10,4),
    current_ratio   NUMERIC(10,4),
    goodwill        NUMERIC(20,2),
    ocf             NUMERIC(20,2),
    ocf_to_np       NUMERIC(10,4),
    roe_ttm         NUMERIC(10,4),
    roa_ttm         NUMERIC(10,4),
    available_date  DATE            NOT NULL,   -- 等于 announce_date
    PRIMARY KEY (symbol, report_date)
);
CREATE INDEX IF NOT EXISTS idx_fin_available ON backtest.financials_quarterly (symbol, available_date);

-- Moneyflow (大单净流入)
CREATE TABLE IF NOT EXISTS moneyflow_daily (
    symbol          VARCHAR(20)     NOT NULL,
    trade_date      DATE            NOT NULL,
    net_lg_amount   NUMERIC(20,2),             -- 大单净流入
    net_md_amount   NUMERIC(20,2),
    net_sm_amount   NUMERIC(20,2),
    available_date  DATE            NOT NULL,  -- 一般 = trade_date + 1
    PRIMARY KEY (symbol, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_mf_available ON backtest.moneyflow_daily (symbol, available_date);

-- Northbound capital (market level)
CREATE TABLE IF NOT EXISTS northbound_daily (
    trade_date      DATE            NOT NULL PRIMARY KEY,
    total_net_buy   NUMERIC(18,2),
    sh_net_buy      NUMERIC(18,2),
    sz_net_buy      NUMERIC(18,2),
    available_date  DATE            NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nb_available ON backtest.northbound_daily (available_date);

-- Margin trading (融资融券)
CREATE TABLE IF NOT EXISTS margin_daily (
    symbol          VARCHAR(20)     NOT NULL,
    trade_date      DATE            NOT NULL,
    rzye            NUMERIC(20,2),             -- 融资余额
    rzmre           NUMERIC(20,2),             -- 融资买入额
    available_date  DATE            NOT NULL,
    PRIMARY KEY (symbol, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_margin_available ON backtest.margin_daily (symbol, available_date);

-- Market-level margin (for market sentiment)
CREATE TABLE IF NOT EXISTS margin_market_daily (
    trade_date      DATE            NOT NULL PRIMARY KEY,
    total_rzye      NUMERIC(20,2),             -- 两市融资余额总计
    available_date  DATE            NOT NULL
);

-- Index daily bars
CREATE TABLE IF NOT EXISTS index_daily (
    index_code      VARCHAR(20)     NOT NULL,  -- 'HS300' | 'ZZ1000' | 'SPY' | 'QQQ' | 'VIX'
    trade_date      DATE            NOT NULL,
    open            NUMERIC(14,4),
    high            NUMERIC(14,4),
    low             NUMERIC(14,4),
    close           NUMERIC(14,4),
    volume          BIGINT,
    available_date  DATE            NOT NULL,
    PRIMARY KEY (index_code, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_idx_available ON backtest.index_daily (index_code, available_date);

-- Market breadth (A-share market sentiment)
CREATE TABLE IF NOT EXISTS market_breadth_daily (
    trade_date          DATE        NOT NULL PRIMARY KEY,
    market              VARCHAR(10) DEFAULT 'CN',
    limit_up_count      INTEGER,               -- 涨停家数
    limit_down_count    INTEGER,               -- 跌停家数
    advancing_count     INTEGER,               -- 上涨家数
    declining_count     INTEGER,               -- 下跌家数
    new_high_count      INTEGER,               -- 创 60 日新高家数
    new_low_count       INTEGER,               -- 创 60 日新低家数
    total_stocks        INTEGER,
    available_date      DATE        NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mb_available ON backtest.market_breadth_daily (available_date);

-- Industry classification (historical snapshot, PIT)
CREATE TABLE IF NOT EXISTS industry_classify (
    symbol              VARCHAR(20) NOT NULL,
    snapshot_date       DATE        NOT NULL,  -- 该分类的生效日期
    sw_l1               VARCHAR(50),           -- 申万一级行业
    sw_l2               VARCHAR(50),
    industry_framework  VARCHAR(30),           -- 映射到评分框架
    PRIMARY KEY (symbol, snapshot_date)
);

-- Trade calendar
CREATE TABLE IF NOT EXISTS trade_calendar (
    market          VARCHAR(10) NOT NULL,
    cal_date        DATE        NOT NULL,
    is_open         BOOLEAN     NOT NULL,
    pretrade_date   DATE,                      -- 上一交易日
    PRIMARY KEY (market, cal_date)
);
CREATE INDEX IF NOT EXISTS idx_cal_open ON backtest.trade_calendar (market, cal_date) WHERE is_open = TRUE;

-- ============================================================
-- 5.2 Feature Table (pre-computed)
-- ============================================================

CREATE TABLE IF NOT EXISTS features_daily (
    symbol              VARCHAR(20)     NOT NULL,
    trade_date          DATE            NOT NULL,
    -- Moving averages
    ma5                 NUMERIC(14,4),
    ma10                NUMERIC(14,4),
    ma20                NUMERIC(14,4),
    ma60                NUMERIC(14,4),
    ma150               NUMERIC(14,4),
    ma200               NUMERIC(14,4),
    ma20_slope          NUMERIC(10,6),
    ma60_slope          NUMERIC(10,6),
    -- Momentum
    rsi_14              NUMERIC(8,4),
    macd_dif            NUMERIC(12,6),
    macd_dea            NUMERIC(12,6),
    macd_hist           NUMERIC(12,6),
    adx_14              NUMERIC(8,4),
    plus_di             NUMERIC(8,4),
    minus_di            NUMERIC(8,4),
    -- Volatility
    atr_14              NUMERIC(14,4),
    hv_20               NUMERIC(10,6),
    boll_upper          NUMERIC(14,4),
    boll_lower          NUMERIC(14,4),
    boll_width          NUMERIC(10,6),
    -- Returns
    ret_1d              NUMERIC(10,6),
    ret_5d              NUMERIC(10,6),
    ret_20d             NUMERIC(10,6),
    ret_60d             NUMERIC(10,6),
    -- Price structure
    dist_20d_high       NUMERIC(10,6),
    dist_60d_high       NUMERIC(10,6),
    pct_in_20d_range    NUMERIC(8,4),
    -- Volume
    vol_ratio_5d        NUMERIC(10,4),
    turnover_rank_20d   NUMERIC(8,4),
    -- Stage and RS
    stage               SMALLINT,              -- Weinstein 1/2/3/4
    rs_rank_63d         NUMERIC(8,4),
    -- Future returns (for backfill validation ONLY, NEVER use in judgment generation)
    future_ret_5d       NUMERIC(10,6),
    future_ret_10d      NUMERIC(10,6),
    future_ret_20d      NUMERIC(10,6),
    future_max_up_20d   NUMERIC(10,6),
    future_max_dd_20d   NUMERIC(10,6),
    PRIMARY KEY (symbol, trade_date)
);
SELECT create_hypertable('backtest.features_daily', 'trade_date', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_feat_symbol ON backtest.features_daily (symbol, trade_date DESC);

-- ============================================================
-- 5.3 Backtest Result Tables
-- ============================================================

-- Backtest run record
CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id              SERIAL          PRIMARY KEY,
    run_timestamp       TIMESTAMPTZ     DEFAULT NOW(),
    start_date          DATE            NOT NULL,
    end_date            DATE            NOT NULL,
    initial_cash_cn     NUMERIC(18,2)   DEFAULT 1000000,
    initial_cash_us     NUMERIC(18,2)   DEFAULT 100000,
    config_snapshot     JSONB,                         -- full config at run time
    status              VARCHAR(20),                   -- 'running' | 'completed' | 'failed'
    notes               TEXT
);

-- Daily judgment records
CREATE TABLE IF NOT EXISTS backtest_judgments (
    id                  SERIAL          PRIMARY KEY,
    run_id              INTEGER         REFERENCES backtest_runs(run_id),
    symbol              VARCHAR(20)     NOT NULL,
    market              VARCHAR(10)     NOT NULL,
    judgment_date       DATE            NOT NULL,
    -- Dimension scores
    technical_score     NUMERIC(6,2),
    fundamental_score   NUMERIC(6,2),
    flow_score          NUMERIC(6,2),
    sentiment_score     NUMERIC(6,2),
    composite_score     NUMERIC(6,2),
    -- Regime
    regime_mode         VARCHAR(30),
    regime_snapshot     JSONB,
    -- Conclusion
    direction           VARCHAR(10),
    confidence          NUMERIC(4,2),
    -- Suggestion
    suggested_action    VARCHAR(30),
    entry_price         NUMERIC(14,4),
    stop_loss           NUMERIC(14,4),
    target_price        NUMERIC(14,4),
    suggested_size_pct  NUMERIC(6,4),
    -- Backfill (auto-filled because future data is known in backtest)
    actual_ret_5d       NUMERIC(10,6),
    actual_ret_10d      NUMERIC(10,6),
    actual_ret_20d      NUMERIC(10,6),
    actual_max_up_20d   NUMERIC(10,6),
    actual_max_dd_20d   NUMERIC(10,6),
    is_correct          BOOLEAN,
    signal_sources      JSONB
);
CREATE INDEX IF NOT EXISTS idx_bj_run ON backtest.backtest_judgments (run_id, judgment_date);
CREATE INDEX IF NOT EXISTS idx_bj_symbol ON backtest.backtest_judgments (run_id, symbol);

-- Simulated trades
CREATE TABLE IF NOT EXISTS backtest_trades (
    id                      SERIAL      PRIMARY KEY,
    run_id                  INTEGER     REFERENCES backtest_runs(run_id),
    symbol                  VARCHAR(20) NOT NULL,
    market                  VARCHAR(10) NOT NULL,
    action                  VARCHAR(10) NOT NULL,   -- 'buy' | 'sell'
    trade_date              DATE        NOT NULL,
    price                   NUMERIC(14,4),          -- 成交价（T+1 开盘）
    shares                  INTEGER,
    amount                  NUMERIC(18,2),
    commission              NUMERIC(12,2),
    trigger_judgment_id     INTEGER,
    trigger_reason          VARCHAR(50),            -- 'new_bullish' | 'stop_loss' | 'target_hit' | 'direction_flip' | 'timeout'
    portfolio_value_after   NUMERIC(18,2)
);
CREATE INDEX IF NOT EXISTS idx_bt_run ON backtest.backtest_trades (run_id, trade_date);

-- Daily portfolio snapshot
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
    benchmark_return_1  NUMERIC(10,6),             -- Buy & Hold index
    benchmark_return_2  NUMERIC(10,6),             -- Equal-weight universe
    benchmark_return_3  NUMERIC(10,6),             -- Momentum strategy
    PRIMARY KEY (run_id, trade_date, market)
);

-- Position snapshots
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
);

-- Daily regime snapshot
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
);
