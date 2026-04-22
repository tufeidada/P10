-- ============================================================
-- P10-AlphaRadar Database Schema
-- PostgreSQL 15 + TimescaleDB + pgvector
-- ============================================================

-- Extensions
CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ============================================================
-- 4.1 行情数据 (TimescaleDB hypertable)
-- ============================================================

-- A股 + 美股日线
CREATE TABLE market_bars_daily (
    symbol      VARCHAR(20) NOT NULL,
    market      VARCHAR(10) NOT NULL DEFAULT 'CN',
    trade_date  DATE NOT NULL,
    open        NUMERIC(12,4),
    high        NUMERIC(12,4),
    low         NUMERIC(12,4),
    close       NUMERIC(12,4),
    volume      BIGINT,
    amount      NUMERIC(18,2),
    turnover    NUMERIC(8,4),
    adj_factor  NUMERIC(10,6) DEFAULT 1,
    PRIMARY KEY (symbol, trade_date)
);
SELECT create_hypertable('market_bars_daily', 'trade_date');
CREATE INDEX idx_mbd_symbol ON market_bars_daily (symbol, trade_date DESC);

-- 分钟线（盘中监控用）
CREATE TABLE intraday_bars (
    symbol      VARCHAR(20) NOT NULL,
    market      VARCHAR(10) NOT NULL DEFAULT 'CN',
    bar_time    TIMESTAMPTZ NOT NULL,
    interval    VARCHAR(5) NOT NULL,
    open        NUMERIC(12,4),
    high        NUMERIC(12,4),
    low         NUMERIC(12,4),
    close       NUMERIC(12,4),
    volume      BIGINT,
    amount      NUMERIC(18,2),
    vwap        NUMERIC(12,4),
    PRIMARY KEY (symbol, bar_time, interval)
);
SELECT create_hypertable('intraday_bars', 'bar_time');

-- 盘中自动压缩策略: 超过90天的分钟线数据自动压缩
ALTER TABLE intraday_bars SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol',
    timescaledb.compress_orderby = 'bar_time DESC'
);
SELECT add_compression_policy('intraday_bars', INTERVAL '90 days');

-- 日线特征
CREATE TABLE features_daily (
    symbol      VARCHAR(20) NOT NULL,
    trade_date  DATE NOT NULL,
    -- 均线
    ma5         NUMERIC(12,4),
    ma10        NUMERIC(12,4),
    ma20        NUMERIC(12,4),
    ma60        NUMERIC(12,4),
    ma150       NUMERIC(12,4),
    ma200       NUMERIC(12,4),
    ma5_slope   NUMERIC(10,6),
    ma20_slope  NUMERIC(10,6),
    -- 动量/振荡
    rsi_14      NUMERIC(8,4),
    macd_dif    NUMERIC(10,6),
    macd_dea    NUMERIC(10,6),
    macd_hist   NUMERIC(10,6),
    -- 波动率
    atr_14      NUMERIC(12,4),
    hv_20       NUMERIC(8,4),
    -- 布林带
    boll_upper  NUMERIC(12,4),
    boll_lower  NUMERIC(12,4),
    boll_width  NUMERIC(8,4),
    -- ADX/DMI
    adx_14      NUMERIC(8,4),
    plus_di     NUMERIC(8,4),
    minus_di    NUMERIC(8,4),
    -- 量价
    vol_ratio_5d        NUMERIC(8,4),
    turnover_rank_20d   NUMERIC(8,4),
    -- 收益率
    ret_1d      NUMERIC(8,6),
    ret_5d      NUMERIC(8,6),
    ret_20d     NUMERIC(8,6),
    -- Weinstein Stage
    stage       SMALLINT,
    rs_rank     NUMERIC(8,4),
    -- 扩展（低频指标放JSONB避免列爆炸）
    extra       JSONB,
    PRIMARY KEY (symbol, trade_date)
);
SELECT create_hypertable('features_daily', 'trade_date');

-- ============================================================
-- 4.2 基本面数据
-- ============================================================

CREATE TABLE fundamentals_daily (
    symbol          VARCHAR(20) NOT NULL,
    trade_date      DATE NOT NULL,
    pe_ttm          NUMERIC(12,4),
    pb              NUMERIC(12,4),
    ps_ttm          NUMERIC(12,4),
    total_mv        NUMERIC(18,2),
    circ_mv         NUMERIC(18,2),
    turnover_rate_f NUMERIC(8,4),
    PRIMARY KEY (symbol, trade_date)
);

CREATE TABLE financials_quarterly (
    symbol          VARCHAR(20) NOT NULL,
    report_date     DATE NOT NULL,
    announce_date   DATE,
    revenue         NUMERIC(18,2),
    revenue_yoy     NUMERIC(10,4),
    revenue_qoq     NUMERIC(10,4),
    net_profit      NUMERIC(18,2),
    np_yoy          NUMERIC(10,4),
    gross_margin    NUMERIC(10,4),
    net_margin      NUMERIC(10,4),
    total_assets    NUMERIC(18,2),
    total_liab      NUMERIC(18,2),
    debt_ratio      NUMERIC(10,4),
    current_ratio   NUMERIC(10,4),
    goodwill        NUMERIC(18,2),
    ocf             NUMERIC(18,2),
    ocf_to_np       NUMERIC(10,4),
    roe_ttm         NUMERIC(10,4),
    roa_ttm         NUMERIC(10,4),
    dupont_npm      NUMERIC(10,4),
    dupont_tat      NUMERIC(10,4),
    dupont_em       NUMERIC(10,4),
    PRIMARY KEY (symbol, report_date)
);

CREATE TABLE analyst_consensus (
    symbol          VARCHAR(20) NOT NULL,
    update_date     DATE NOT NULL,
    target_price    NUMERIC(12,4),
    rating          VARCHAR(20),
    eps_current_yr  NUMERIC(10,4),
    eps_next_yr     NUMERIC(10,4),
    num_analysts    INTEGER,
    PRIMARY KEY (symbol, update_date)
);

-- ============================================================
-- 4.3 资金面数据
-- ============================================================

CREATE TABLE moneyflow_daily (
    symbol          VARCHAR(20) NOT NULL,
    trade_date      DATE NOT NULL,
    buy_lg_amount   NUMERIC(18,2),
    sell_lg_amount  NUMERIC(18,2),
    net_lg_amount   NUMERIC(18,2),
    buy_md_amount   NUMERIC(18,2),
    sell_md_amount  NUMERIC(18,2),
    net_md_amount   NUMERIC(18,2),
    buy_sm_amount   NUMERIC(18,2),
    sell_sm_amount  NUMERIC(18,2),
    net_sm_amount   NUMERIC(18,2),
    PRIMARY KEY (symbol, trade_date)
);

CREATE TABLE northbound_daily (
    trade_date      DATE NOT NULL PRIMARY KEY,
    sh_net_buy      NUMERIC(18,2),
    sz_net_buy      NUMERIC(18,2),
    total_net_buy   NUMERIC(18,2),
    sh_cumulative   NUMERIC(18,2),
    sz_cumulative   NUMERIC(18,2)
);

CREATE TABLE margin_daily (
    symbol      VARCHAR(20) NOT NULL,
    trade_date  DATE NOT NULL,
    rzye        NUMERIC(18,2),
    rzmre       NUMERIC(18,2),
    rqye        NUMERIC(18,2),
    PRIMARY KEY (symbol, trade_date)
);

-- ============================================================
-- 4.4 情绪面数据
-- ============================================================

CREATE TABLE social_sentiment (
    symbol          VARCHAR(20) NOT NULL,
    market          VARCHAR(10) NOT NULL,
    snapshot_time   TIMESTAMPTZ NOT NULL,
    source          VARCHAR(20) NOT NULL,
    bullish_pct     NUMERIC(6,2),
    message_count   INTEGER,
    message_delta   NUMERIC(8,2),
    sentiment_score NUMERIC(6,4),
    raw_data        JSONB,
    PRIMARY KEY (symbol, snapshot_time, source)
);

CREATE TABLE market_sentiment_daily (
    trade_date      DATE NOT NULL PRIMARY KEY,
    limit_up_count  INTEGER,
    limit_down_count INTEGER,
    up_down_ratio   NUMERIC(8,4),
    new_high_count  INTEGER,
    new_low_count   INTEGER,
    margin_balance  NUMERIC(18,2),
    margin_delta_5d NUMERIC(10,4),
    vix_cn          NUMERIC(8,4),
    fear_greed      NUMERIC(8,4)
);

-- ============================================================
-- 4.5 核心业务表
-- ============================================================

-- 候选池
CREATE TABLE stock_universe (
    symbol        VARCHAR(20)  NOT NULL,
    market        VARCHAR(10)  NOT NULL,
    name          VARCHAR(100),
    industry      VARCHAR(50),
    source        VARCHAR(20)  NOT NULL,
    added_date    DATE         NOT NULL,
    added_reason  TEXT,
    active        BOOLEAN      DEFAULT TRUE,
    priority      SMALLINT     DEFAULT 1,
    tags          JSONB        DEFAULT '[]',
    notes         TEXT,
    removed_date  DATE,
    removed_reason TEXT,
    PRIMARY KEY (symbol, market)
);
CREATE INDEX idx_universe_active ON stock_universe (market, active);
CREATE INDEX idx_su_name_trgm ON stock_universe USING gin (name gin_trgm_ops);

-- 行业分类（从P6+迁移）
CREATE TABLE industry_classify (
    symbol      VARCHAR(20) NOT NULL PRIMARY KEY,
    sw1_code    VARCHAR(20),
    sw1_name    VARCHAR(50),
    sw2_code    VARCHAR(20),
    sw2_name    VARCHAR(50),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

-- 交易日历（从P6+迁移）
CREATE TABLE trade_calendar (
    trade_date  DATE NOT NULL PRIMARY KEY
);

-- 持仓
CREATE TABLE positions (
    id              SERIAL PRIMARY KEY,
    symbol          VARCHAR(20) NOT NULL,
    market          VARCHAR(10) NOT NULL,
    entry_date      DATE NOT NULL,
    entry_price     NUMERIC(12,4) NOT NULL,
    shares          INTEGER NOT NULL,
    position_type   VARCHAR(20) DEFAULT 'swing',
    stop_loss       NUMERIC(12,4),
    target_1        NUMERIC(12,4),
    target_2        NUMERIC(12,4),
    status          VARCHAR(20) DEFAULT 'open',
    exit_date       DATE,
    exit_price      NUMERIC(12,4),
    exit_reason     TEXT,
    notes           TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_positions_open ON positions (symbol) WHERE status = 'open';

-- 分析判断记录
CREATE TABLE judgments (
    id              SERIAL PRIMARY KEY,
    symbol          VARCHAR(20) NOT NULL,
    market          VARCHAR(10) NOT NULL,
    judgment_date   DATE NOT NULL,
    timeframe       VARCHAR(10) NOT NULL,
    -- 各维度评分
    technical_score NUMERIC(6,2),
    fundamental_score NUMERIC(6,2),
    flow_score      NUMERIC(6,2),
    sentiment_score NUMERIC(6,2),
    composite_score NUMERIC(6,2),
    -- 判断结论
    direction       VARCHAR(10) NOT NULL,
    confidence      NUMERIC(4,2),
    logic_text      TEXT,
    -- 交易建议
    suggested_action VARCHAR(30),
    entry_zone_low  NUMERIC(12,4),
    entry_zone_high NUMERIC(12,4),
    stop_loss       NUMERIC(12,4),
    target_price    NUMERIC(12,4),
    -- 信号来源追踪
    signal_sources  JSONB,
    regime_at_time  JSONB,
    -- 双信号体系（M5）
    rule_signal_strength VARCHAR(20),
    llm_direction        VARCHAR(20),
    llm_signal_strength  VARCHAR(20),
    llm_reasoning        TEXT,
    llm_risks            TEXT,
    llm_extra_advice     TEXT,
    -- Vote 机制（Phase 2）
    llm_vote_consensus   NUMERIC(3,2),
    llm_vote_total_calls INT,
    -- 回填字段（T+N 后填入）
    actual_ret_1d   NUMERIC(8,6),
    actual_ret_5d   NUMERIC(8,6),
    actual_ret_10d  NUMERIC(8,6),
    actual_ret_20d  NUMERIC(8,6),
    actual_max_up_20d  NUMERIC(8,6),
    actual_max_dd_20d  NUMERIC(8,6),
    is_correct      BOOLEAN,
    error_category  VARCHAR(30),
    error_detail    TEXT,
    reviewed_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_judgments_symbol_date ON judgments (symbol, judgment_date DESC);
CREATE INDEX idx_judgments_correct ON judgments (is_correct) WHERE is_correct IS NOT NULL;

-- 盘中信号
CREATE TABLE intraday_signals (
    id              SERIAL PRIMARY KEY,
    symbol          VARCHAR(20) NOT NULL,
    market          VARCHAR(10) NOT NULL,
    signal_time     TIMESTAMPTZ NOT NULL,
    signal_type     VARCHAR(10) NOT NULL,
    strength        VARCHAR(10) NOT NULL,
    trigger_rule    VARCHAR(50) NOT NULL,
    trigger_detail  JSONB,
    price_at_signal NUMERIC(12,4),
    suggested_price NUMERIC(12,4),
    stop_price      NUMERIC(12,4),
    basis_judgment_id INTEGER REFERENCES judgments(id),
    -- 回填
    actual_ret_30m  NUMERIC(8,6),
    actual_ret_1d   NUMERIC(8,6),
    actual_max_favorable NUMERIC(8,6),
    actual_max_adverse   NUMERIC(8,6),
    signal_quality  NUMERIC(6,2),
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_intraday_signals_time ON intraday_signals (signal_time DESC);

-- 矫正记录
CREATE TABLE calibrations (
    id              SERIAL PRIMARY KEY,
    judgment_id     INTEGER REFERENCES judgments(id),
    calibration_time TIMESTAMPTZ NOT NULL,
    original_direction VARCHAR(10),
    new_direction   VARCHAR(10),
    reason          TEXT,
    trigger_factors JSONB,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- 4.6 Regime 与宏观
-- ============================================================

CREATE TABLE regime_daily (
    trade_date      DATE NOT NULL,
    market          VARCHAR(10) NOT NULL,
    trend_score     NUMERIC(6,2),
    volatility_score NUMERIC(6,2),
    breadth_score   NUMERIC(6,2),
    liquidity_score NUMERIC(6,2),
    regime_mode     VARCHAR(30) NOT NULL,
    trend_direction VARCHAR(10) NOT NULL,
    volatility_env  VARCHAR(10) NOT NULL,
    signal_threshold_adj NUMERIC(4,2),
    max_position_pct     NUMERIC(4,2),
    dimension_weights    JSONB,
    detail          JSONB,
    PRIMARY KEY (trade_date, market)
);

CREATE TABLE macro_indicators (
    indicator_name  VARCHAR(50) NOT NULL,
    market          VARCHAR(10) NOT NULL,
    report_date     DATE NOT NULL,
    value           NUMERIC(18,6),
    prev_value      NUMERIC(18,6),
    change_pct      NUMERIC(10,4),
    source          VARCHAR(50),
    PRIMARY KEY (indicator_name, market, report_date)
);

-- ============================================================
-- 4.7 进化层
-- ============================================================

CREATE TABLE signal_quality_tracker (
    rule_name       VARCHAR(50) NOT NULL,
    market          VARCHAR(10) NOT NULL,
    regime_mode     VARCHAR(30),
    period_start    DATE NOT NULL,
    period_end      DATE NOT NULL,
    total_signals   INTEGER,
    correct_signals INTEGER,
    accuracy        NUMERIC(6,4),
    avg_return      NUMERIC(8,6),
    avg_max_dd      NUMERIC(8,6),
    ic_value        NUMERIC(8,6),
    ir_value        NUMERIC(8,6),
    PRIMARY KEY (rule_name, market, regime_mode, period_end)
);

CREATE TABLE benchmark_daily (
    trade_date      DATE NOT NULL,
    market          VARCHAR(10) NOT NULL,
    benchmark_name  VARCHAR(30) NOT NULL,
    daily_return    NUMERIC(8,6),
    cumulative_return NUMERIC(12,6),
    max_drawdown    NUMERIC(8,6),
    PRIMARY KEY (trade_date, market, benchmark_name)
);

CREATE TABLE review_reports (
    id              SERIAL PRIMARY KEY,
    report_type     VARCHAR(10) NOT NULL,
    report_date     DATE NOT NULL,
    market          VARCHAR(10),
    total_judgments  INTEGER,
    accuracy_short   NUMERIC(6,4),
    accuracy_mid     NUMERIC(6,4),
    alpha_vs_benchmark NUMERIC(8,6),
    summary_text    TEXT,
    key_findings    JSONB,
    suggested_changes JSONB,
    full_report_md  TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- 4.8 Wiki 经验库
-- ============================================================

CREATE TABLE wiki_pages (
    page_path       VARCHAR(200) NOT NULL PRIMARY KEY,
    page_type       VARCHAR(20) NOT NULL,
    title           VARCHAR(200),
    summary         TEXT,
    tags            TEXT[],
    last_updated    TIMESTAMPTZ,
    update_count    INTEGER DEFAULT 0,
    embedding       vector(1024),
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_wiki_embedding ON wiki_pages USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

CREATE TABLE experience_store (
    id              SERIAL PRIMARY KEY,
    discovery_date  DATE NOT NULL,
    category        VARCHAR(30) NOT NULL,
    market          VARCHAR(10),
    content_text    TEXT NOT NULL,
    evidence        JSONB,
    embedding       vector(1024),
    status          VARCHAR(20) DEFAULT 'under_review',
    applied_count   INTEGER DEFAULT 0,
    last_validated  DATE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_exp_embedding ON experience_store USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);
CREATE INDEX idx_exp_status ON experience_store (status) WHERE status = 'active';

-- ============================================================
-- 4.9 系统运维
-- ============================================================

CREATE TABLE data_quality_checks (
    id              SERIAL PRIMARY KEY,
    check_time      TIMESTAMPTZ DEFAULT NOW(),
    source_name     VARCHAR(30) NOT NULL,
    check_type      VARCHAR(30) NOT NULL,
    status          VARCHAR(10) NOT NULL,
    detail          JSONB,
    latest_date     DATE,
    expected_date   DATE
);
CREATE INDEX idx_dqc_time ON data_quality_checks (check_time DESC);

CREATE TABLE telegram_commands (
    id              SERIAL PRIMARY KEY,
    chat_id         BIGINT NOT NULL,
    command         VARCHAR(50) NOT NULL,
    args            TEXT,
    response_summary TEXT,
    processing_ms   INTEGER,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- M4: Feature 更新追踪
-- ============================================================

CREATE TABLE IF NOT EXISTS feature_update_log (
    id              SERIAL PRIMARY KEY,
    run_date        DATE NOT NULL,
    market          VARCHAR(10) NOT NULL,
    symbol          VARCHAR(20) NOT NULL,
    success         BOOLEAN NOT NULL,
    error_message   TEXT,
    computed_at     TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_ful_symbol_date ON feature_update_log(symbol, run_date DESC);
CREATE INDEX IF NOT EXISTS idx_ful_run_date    ON feature_update_log(run_date DESC, market);

-- Tushare Pro 积分消耗追踪
CREATE TABLE IF NOT EXISTS tushare_credit_log (
    id              SERIAL PRIMARY KEY,
    log_date        DATE NOT NULL,
    query_type      TEXT NOT NULL,       -- 'daily_bars' / 'fundamentals' / 'moneyflow' 等
    source          TEXT NOT NULL,       -- 调用任务名
    points_used     INT NOT NULL DEFAULT 0,
    symbols_count   INT,
    logged_at       TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_tcl_date ON tushare_credit_log(log_date DESC);

-- ============================================================
-- M5: 数据健康度监控
-- ============================================================

CREATE TABLE IF NOT EXISTS data_source_expectations (
    source_name     TEXT PRIMARY KEY,
    table_name      TEXT NOT NULL,
    filter_clause   TEXT,
    date_column     TEXT NOT NULL,
    frequency       TEXT NOT NULL,       -- daily / weekly / monthly / quarterly
    max_lag_days    INT NOT NULL,
    severity        TEXT NOT NULL,       -- info / warn / critical
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS data_freshness_log (
    check_id    SERIAL PRIMARY KEY,
    check_time  TIMESTAMPTZ DEFAULT NOW(),
    source_name TEXT NOT NULL,
    max_date    DATE,
    lag_days    INT,
    status      TEXT,                    -- ok / warn / critical / info
    message     TEXT
);
CREATE INDEX IF NOT EXISTS idx_freshness_log_time   ON data_freshness_log(check_time DESC);
CREATE INDEX IF NOT EXISTS idx_freshness_log_source ON data_freshness_log(source_name, check_time DESC);

-- M6: Scheduler observability
CREATE TABLE IF NOT EXISTS scheduler_heartbeat (
    id          SERIAL PRIMARY KEY,
    beat_time   TIMESTAMPTZ DEFAULT NOW(),
    jobs_count  INT,
    memory_mb   NUMERIC(8,1)
);
CREATE INDEX IF NOT EXISTS idx_scheduler_hb_time ON scheduler_heartbeat(beat_time DESC);

CREATE TABLE IF NOT EXISTS scheduler_job_log (
    id           SERIAL PRIMARY KEY,
    trigger_time TIMESTAMPTZ DEFAULT NOW(),
    job_name     TEXT NOT NULL,
    status       TEXT NOT NULL,   -- success / failed / skipped / invariant
    duration_ms  INT,
    error_message TEXT
);
CREATE INDEX IF NOT EXISTS idx_job_log_time ON scheduler_job_log(trigger_time DESC);
CREATE INDEX IF NOT EXISTS idx_job_log_name ON scheduler_job_log(job_name, trigger_time DESC);

-- M6: data_source_expectations lag_basis column (added via migration)
ALTER TABLE data_source_expectations ADD COLUMN IF NOT EXISTS lag_basis TEXT NOT NULL DEFAULT 'trading_days';

-- M7: LLM cost tracking
CREATE TABLE IF NOT EXISTS llm_cost_log (
    id          SERIAL PRIMARY KEY,
    call_time   TIMESTAMP DEFAULT NOW(),
    model       TEXT,
    symbol      TEXT,
    market      TEXT,
    tokens_in   INT,
    tokens_out  INT,
    cost_cny    NUMERIC(10,4),
    status      TEXT,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_llm_cost_time ON llm_cost_log(call_time DESC);
CREATE INDEX IF NOT EXISTS idx_llm_cost_date ON llm_cost_log(DATE(call_time), status);
