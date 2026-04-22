# P10-AlphaRadar 系统架构文档

> 版本: v1.0 | 日期: 2026-04-16 | 作者: 轩老板 & Claude
>
> 本文档为 P10-AlphaRadar 投研判断系统的完整技术架构，供开发实施参考。

---

## 一、系统定位

P10-AlphaRadar 是一个面向 A 股和美股的 **多维投研分析 + 信号生成 + 自我进化** 系统。

**核心能力：**

- 管理候选股票池（70-80% 自选 + 20-30% 系统推荐）
- 对候选池个股进行基本面、技术面、资金面、情绪面的综合分析
- 输出短期（1-2 周）和中期（1-3 月）的方向判断及交易建议
- 盘中基于 15 分钟/小时线实时矫正短期判断，识别买卖点
- 记录所有判断并自动回填实际表现，支持复盘和归因分析
- 通过 LLM Wiki 积累投研经验，形成可检索的知识资产
- 通过 Telegram 双向通信实现随时交互

**不做的事：**

- 不做自动交易执行（信号推送给人，由人决策执行）
- 不做高频/日内超短线（最短周期为 15 分钟线，服务于波段交易）
- 不用 LLM 做选股推荐（LLM 只做分析和复盘，不做发散性推荐）
- 不做月线级别的长期方向判断（长期只做估值 + 基本面质量静态评估）

**与其他系统的关系：**

| 系统 | 定位 | 与 P10 的关系 |
|------|------|--------------|
| P6+ | A 股量化交易策略（系统化执行） | P10 的分析结论可辅助 P6+ 的 regime 参数调整 |
| P7/QlibAccel | ML 因子研究 | P7 发现的有效因子可纳入 P10 的分析框架 |
| P4-CHIGU | 持仓风险哨兵 | P10 上线后 P4 的核心功能被 P10 吸收，P4 退役 |

---

## 二、数据库选型：PostgreSQL + TimescaleDB + pgvector

### 2.1 选型评估

| 维度 | PostgreSQL + 扩展 | DuckDB（P4 现用） | SQLite（P4 现用） |
|------|-------------------|-------------------|-------------------|
| **并发读写** | ✅ 原生支持多进程并发 | ❌ 单写者锁，多进程写入会阻塞 | ❌ 文件级锁，不支持并发写 |
| **时序数据** | ✅ TimescaleDB 自动分区压缩 | ⚠️ 列存储查询快但无自动分区 | ❌ 无时序优化 |
| **JSON 支持** | ✅ JSONB 原生索引 | ⚠️ JSON 函数有限 | ⚠️ JSON 函数基础 |
| **向量搜索** | ✅ pgvector 扩展 | ❌ 不支持 | ❌ 不支持 |
| **分析查询** | ✅ 窗口函数完整 | ✅ 列存储分析极快 | ⚠️ 基础 |
| **运维复杂度** | ⚠️ 需运行 PG 服务 | ✅ 嵌入式零运维 | ✅ 嵌入式零运维 |
| **生态成熟度** | ✅ 极成熟，工具链完善 | ⚠️ 较新，工具链发展中 | ✅ 成熟但功能有限 |

### 2.2 选择 PostgreSQL 的决定性理由

P10 有多个进程需要同时读写数据库：

```
同时运行的进程:
├── 数据管道（拉取行情/财报/资金流）        → 写入 market_bars, fundamentals
├── 分析引擎（计算特征/评分/判断）           → 读取行情，写入 judgments, signals
├── 盘中监控（每 15 分钟更新）              → 读写 intraday_signals
├── Telegram Bot（接收命令、返回分析）       → 读写多张表
├── LLM 服务（生成分析、更新 wiki）          → 读写 wiki_pages, experience_store
├── 复盘引擎（回填实际表现、计算准确率）      → 读写 judgments, signals, metrics
└── FastAPI 前端（展示看板）                → 只读查询
```

DuckDB 的单写者锁在这种场景下会成为瓶颈——P4 已经因为 pipeline 和 monitor 的写冲突出过问题。PostgreSQL 的 MVCC 多版本并发控制天然解决这个问题。

此外，pgvector 直接支持 Wiki 经验库的语义检索，不需要额外部署向量数据库（Chroma/Milvus），减少运维负担。

### 2.3 PostgreSQL 扩展配置

```sql
-- 必装扩展
CREATE EXTENSION IF NOT EXISTS timescaledb;   -- 时序数据自动分区压缩
CREATE EXTENSION IF NOT EXISTS vector;        -- 向量相似度搜索（Wiki RAG）
CREATE EXTENSION IF NOT EXISTS pg_trgm;       -- 模糊文本搜索（股票名/代码）

-- TimescaleDB 配置
-- 行情表转为 hypertable，按 trade_date 自动分区
SELECT create_hypertable('market_bars_daily', 'trade_date');
SELECT create_hypertable('features_daily', 'trade_date');
SELECT create_hypertable('intraday_bars', 'bar_time');

-- 超过 90 天的分钟线数据自动压缩
ALTER TABLE intraday_bars SET (
  timescaledb.compress,
  timescaledb.compress_segmentby = 'symbol',
  timescaledb.compress_orderby = 'bar_time DESC'
);
SELECT add_compression_policy('intraday_bars', INTERVAL '90 days');
```

### 2.4 从 P6+ 导入启动数据

P6+ 的 DuckDB 中已有大量 A 股历史行情数据，通过迁移脚本导入 PostgreSQL：

```bash
# 迁移脚本 scripts/migrate_from_p6.py
# 步骤：
# 1. 连接 P6+ 的 DuckDB（只读）
# 2. 分批读取 market_bars_daily（每批 10 万行）
# 3. 写入 PostgreSQL 的对应表
# 4. 验证行数和日期范围一致
# 5. 导入 features_daily, fundamentals_daily, industry_classify 等基础表

python scripts/migrate_from_p6.py \
  --source /path/to/p6plus/data/agu.duckdb \
  --target postgresql://localhost:5432/alpharadar \
  --tables market_bars_daily,features_daily,fundamentals_daily,trade_calendar,industry_classify \
  --batch-size 100000 \
  --verify
```

迁移表清单（从 P6+/P4）：

| 源表 | 目标表 | 预估数据量 | 说明 |
|------|--------|-----------|------|
| market_bars_daily | market_bars_daily | ~5000 万行 | A 股日线 OHLCV |
| features_daily | features_daily | ~5000 万行 | 70+ 特征列 |
| fundamentals_daily | fundamentals_daily | ~500 万行 | PE/PB/PS/换手率/市值 |
| trade_calendar | trade_calendar | ~1 万行 | 交易日历 |
| industry_classify | industry_classify | ~5000 行 | 申万行业分类 |
| train_pool | — | 不迁移 | P10 不需要 ML 训练池 |

---

## 三、整体架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                          交互层                                      │
│  Telegram Bot（双向通信）  │  FastAPI REST  │  React 前端看板         │
└─────────────────────┬───────────────────────────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────────────────────────┐
│                          智能层                                      │
│  LLM 服务                                                           │
│  ├─ 综合分析师（多维度信号 → 连贯投资叙事 + 交易建议）                  │
│  ├─ 信息提取器（研报摘要/社交文本 → 结构化判断）                       │
│  ├─ 复盘教练（判断记录 + 实际表现 → 错误归因 + 改进建议）              │
│  └─ 系统诊断师（运行状态 + 数据质量 → 健康报告）                      │
│  LLM Wiki 经验库（Markdown 文件 + pgvector 索引）                    │
└─────────────────────┬───────────────────────────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────────────────────────┐
│                          分析层                                      │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐  │
│  │ Regime   │ │ 技术面   │ │ 基本面   │ │ 资金面   │ │ 情绪面   │  │
│  │ 多维检测 │ │ 多周期   │ │ 财务+    │ │ 主力+    │ │ 社交+    │  │
│  │          │ │ 趋势分析 │ │ 估值分析 │ │ 机构行为 │ │ 市场情绪 │  │
│  └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘  │
│       │            │            │            │            │         │
│       └────────────┴────────┬───┴────────────┴────────────┘         │
│                             │                                       │
│              ┌──────────────▼──────────────┐                        │
│              │  综合判断引擎                 │                        │
│              │  多维度加权 → 短/中期观点     │                        │
│              │  + 交易建议(入场/止损/目标)   │                        │
│              └──────────────┬──────────────┘                        │
│                             │                                       │
│              ┌──────────────▼──────────────┐                        │
│              │  盘中矫正引擎                │                        │
│              │  15min/1h 数据 → 短期修正    │                        │
│              │  → 买卖点信号                │                        │
│              └─────────────────────────────┘                        │
└─────────────────────┬───────────────────────────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────────────────────────┐
│                          进化层                                      │
│  判断追踪器（记录+回填） │ 周度自动复盘 │ 对照组基准 │ 信号质量评分   │
└─────────────────────┬───────────────────────────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────────────────────────┐
│                          数据层                                      │
│  A 股: Tushare + AkShare + BaoStock + pytdx                         │
│  美股: yfinance + Polygon.io (后续)                                  │
│  社交: StockTwits (美股) + 东财股吧热度 (A股)                         │
│  研报: 东方财富研报标题/评级                                          │
│                                                                     │
│  PostgreSQL 15 + TimescaleDB + pgvector                              │
│  数据质量监控层（新鲜度/完整性/异常检测）                               │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 四、数据库 Schema

### 4.1 行情数据（TimescaleDB hypertable）

```sql
-- A 股 + 美股日线（从 P6+ 迁移 A 股部分）
CREATE TABLE market_bars_daily (
    symbol      VARCHAR(20) NOT NULL,
    market      VARCHAR(10) NOT NULL DEFAULT 'CN',  -- 'CN' | 'US'
    trade_date  DATE NOT NULL,
    open        NUMERIC(12,4),
    high        NUMERIC(12,4),
    low         NUMERIC(12,4),
    close       NUMERIC(12,4),
    volume      BIGINT,
    amount      NUMERIC(18,2),            -- 成交额
    turnover    NUMERIC(8,4),             -- 换手率
    adj_factor  NUMERIC(10,6) DEFAULT 1,  -- 复权因子
    PRIMARY KEY (symbol, trade_date)
);
SELECT create_hypertable('market_bars_daily', 'trade_date');
CREATE INDEX idx_mbd_symbol ON market_bars_daily (symbol, trade_date DESC);

-- 分钟线（盘中监控用）
CREATE TABLE intraday_bars (
    symbol      VARCHAR(20) NOT NULL,
    market      VARCHAR(10) NOT NULL DEFAULT 'CN',
    bar_time    TIMESTAMPTZ NOT NULL,
    interval    VARCHAR(5) NOT NULL,  -- '1m' | '5m' | '15m' | '1h'
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

-- 日线特征（从 P6+ 迁移）
CREATE TABLE features_daily (
    symbol      VARCHAR(20) NOT NULL,
    trade_date  DATE NOT NULL,
    -- 技术面特征（70+ 列，此处列出核心字段）
    ma5         NUMERIC(12,4),
    ma10        NUMERIC(12,4),
    ma20        NUMERIC(12,4),
    ma60        NUMERIC(12,4),
    ma150       NUMERIC(12,4),
    ma200       NUMERIC(12,4),
    ma5_slope   NUMERIC(10,6),
    ma20_slope  NUMERIC(10,6),
    rsi_14      NUMERIC(8,4),
    macd_dif    NUMERIC(10,6),
    macd_dea    NUMERIC(10,6),
    macd_hist   NUMERIC(10,6),
    atr_14      NUMERIC(12,4),
    hv_20       NUMERIC(8,4),
    boll_upper  NUMERIC(12,4),
    boll_lower  NUMERIC(12,4),
    boll_width  NUMERIC(8,4),
    adx_14      NUMERIC(8,4),
    plus_di     NUMERIC(8,4),
    minus_di    NUMERIC(8,4),
    vol_ratio_5d    NUMERIC(8,4),
    turnover_rank_20d NUMERIC(8,4),
    ret_1d      NUMERIC(8,6),
    ret_5d      NUMERIC(8,6),
    ret_20d     NUMERIC(8,6),
    -- Weinstein Stage 相关
    stage       SMALLINT,          -- 1/2/3/4
    rs_rank     NUMERIC(8,4),      -- 相对强度排名 (0-100)
    -- 扩展 JSON（低频使用的指标放 JSONB 避免列爆炸）
    extra       JSONB,
    PRIMARY KEY (symbol, trade_date)
);
SELECT create_hypertable('features_daily', 'trade_date');
```

### 4.2 基本面数据

```sql
-- 基本面日频（PE/PB/换手/市值）
CREATE TABLE fundamentals_daily (
    symbol      VARCHAR(20) NOT NULL,
    trade_date  DATE NOT NULL,
    pe_ttm      NUMERIC(12,4),
    pb          NUMERIC(12,4),
    ps_ttm      NUMERIC(12,4),
    total_mv    NUMERIC(18,2),     -- 总市值（万元）
    circ_mv     NUMERIC(18,2),     -- 流通市值（万元）
    turnover_rate_f NUMERIC(8,4),  -- 自由流通换手率
    PRIMARY KEY (symbol, trade_date)
);

-- 财务报表（季度）
CREATE TABLE financials_quarterly (
    symbol          VARCHAR(20) NOT NULL,
    report_date     DATE NOT NULL,        -- 报告期 (e.g. 2026-03-31)
    announce_date   DATE,                 -- 公告日期
    -- 利润表
    revenue         NUMERIC(18,2),
    revenue_yoy     NUMERIC(10,4),        -- 营收同比增速 %
    revenue_qoq     NUMERIC(10,4),        -- 营收环比增速 %
    net_profit      NUMERIC(18,2),
    np_yoy          NUMERIC(10,4),        -- 净利润同比 %
    gross_margin    NUMERIC(10,4),
    net_margin      NUMERIC(10,4),
    -- 资产负债表
    total_assets    NUMERIC(18,2),
    total_liab      NUMERIC(18,2),
    debt_ratio      NUMERIC(10,4),        -- 资产负债率
    current_ratio   NUMERIC(10,4),        -- 流动比率
    goodwill        NUMERIC(18,2),
    -- 现金流
    ocf             NUMERIC(18,2),        -- 经营性现金流
    ocf_to_np       NUMERIC(10,4),        -- 经营现金流/净利润
    -- 盈利质量
    roe_ttm         NUMERIC(10,4),
    roa_ttm         NUMERIC(10,4),
    -- 杜邦分解
    dupont_npm      NUMERIC(10,4),        -- 净利率
    dupont_tat      NUMERIC(10,4),        -- 总资产周转率
    dupont_em       NUMERIC(10,4),        -- 权益乘数
    PRIMARY KEY (symbol, report_date)
);

-- 分析师一致预期（可选，有数据源时启用）
CREATE TABLE analyst_consensus (
    symbol          VARCHAR(20) NOT NULL,
    update_date     DATE NOT NULL,
    target_price    NUMERIC(12,4),
    rating          VARCHAR(20),          -- 'buy'/'hold'/'sell'
    eps_current_yr  NUMERIC(10,4),
    eps_next_yr     NUMERIC(10,4),
    num_analysts    INTEGER,
    PRIMARY KEY (symbol, update_date)
);
```

### 4.3 资金面数据

```sql
-- 资金流（A 股）
CREATE TABLE moneyflow_daily (
    symbol      VARCHAR(20) NOT NULL,
    trade_date  DATE NOT NULL,
    buy_lg_amount   NUMERIC(18,2),   -- 大单买入额
    sell_lg_amount  NUMERIC(18,2),   -- 大单卖出额
    net_lg_amount   NUMERIC(18,2),   -- 大单净流入
    buy_md_amount   NUMERIC(18,2),   -- 中单
    sell_md_amount  NUMERIC(18,2),
    net_md_amount   NUMERIC(18,2),
    buy_sm_amount   NUMERIC(18,2),   -- 小单
    sell_sm_amount  NUMERIC(18,2),
    net_sm_amount   NUMERIC(18,2),
    PRIMARY KEY (symbol, trade_date)
);

-- 北向资金（A 股）
CREATE TABLE northbound_daily (
    trade_date      DATE NOT NULL PRIMARY KEY,
    sh_net_buy      NUMERIC(18,2),   -- 沪股通净买入（万元）
    sz_net_buy      NUMERIC(18,2),   -- 深股通净买入（万元）
    total_net_buy   NUMERIC(18,2),
    sh_cumulative   NUMERIC(18,2),   -- 沪股通累计净买入
    sz_cumulative   NUMERIC(18,2)
);

-- 融资融券（A 股）
CREATE TABLE margin_daily (
    symbol      VARCHAR(20) NOT NULL,
    trade_date  DATE NOT NULL,
    rzye        NUMERIC(18,2),    -- 融资余额
    rzmre       NUMERIC(18,2),    -- 融资买入额
    rqye        NUMERIC(18,2),    -- 融券余额
    PRIMARY KEY (symbol, trade_date)
);
```

### 4.4 情绪面数据

```sql
-- 社交情绪快照
CREATE TABLE social_sentiment (
    symbol          VARCHAR(20) NOT NULL,
    market          VARCHAR(10) NOT NULL,
    snapshot_time   TIMESTAMPTZ NOT NULL,
    source          VARCHAR(20) NOT NULL,  -- 'stocktwits' | 'eastmoney' | 'xueqiu'
    bullish_pct     NUMERIC(6,2),          -- 看多比例 (0-100)
    message_count   INTEGER,               -- 讨论量
    message_delta   NUMERIC(8,2),          -- 讨论量变化率 %
    sentiment_score NUMERIC(6,4),          -- 综合情绪分 (-1 ~ +1)
    raw_data        JSONB,
    PRIMARY KEY (symbol, snapshot_time, source)
);

-- 市场情绪指标（A 股）
CREATE TABLE market_sentiment_daily (
    trade_date      DATE NOT NULL PRIMARY KEY,
    limit_up_count  INTEGER,        -- 涨停家数
    limit_down_count INTEGER,       -- 跌停家数
    up_down_ratio   NUMERIC(8,4),   -- 涨跌比
    new_high_count  INTEGER,        -- 创新高家数
    new_low_count   INTEGER,        -- 创新低家数
    margin_balance  NUMERIC(18,2),  -- 两融余额
    margin_delta_5d NUMERIC(10,4),  -- 两融余额5日变化率 %
    vix_cn          NUMERIC(8,4),   -- 50ETF期权隐含波动率（如有）
    fear_greed      NUMERIC(8,4)    -- 恐贪指数（自定义计算）
);
```

### 4.5 核心业务表

```sql
-- ==================== 候选池 ====================

CREATE TABLE stock_universe (
    symbol      VARCHAR(20) NOT NULL PRIMARY KEY,
    market      VARCHAR(10) NOT NULL,
    name        VARCHAR(100),
    industry    VARCHAR(50),           -- 行业分类
    source      VARCHAR(20) NOT NULL,  -- 'manual' | 'scanner'
    added_date  DATE NOT NULL,
    added_reason TEXT,
    status      VARCHAR(20) DEFAULT 'active',  -- 'active' | 'removed' | 'archived'
    removed_date DATE,
    removed_reason TEXT
);

-- ==================== 持仓 ====================

CREATE TABLE positions (
    id              SERIAL PRIMARY KEY,
    symbol          VARCHAR(20) NOT NULL,
    market          VARCHAR(10) NOT NULL,
    entry_date      DATE NOT NULL,
    entry_price     NUMERIC(12,4) NOT NULL,
    shares          INTEGER NOT NULL,
    position_type   VARCHAR(20) DEFAULT 'swing',  -- 'swing' | 'long_term'
    stop_loss       NUMERIC(12,4),
    target_1        NUMERIC(12,4),
    target_2        NUMERIC(12,4),
    status          VARCHAR(20) DEFAULT 'open',   -- 'open' | 'closed'
    exit_date       DATE,
    exit_price      NUMERIC(12,4),
    exit_reason     TEXT,
    notes           TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ==================== 分析判断记录 ====================

CREATE TABLE judgments (
    id              SERIAL PRIMARY KEY,
    symbol          VARCHAR(20) NOT NULL,
    market          VARCHAR(10) NOT NULL,
    judgment_date   DATE NOT NULL,
    timeframe       VARCHAR(10) NOT NULL,    -- 'short' | 'mid'
    -- 各维度评分
    technical_score NUMERIC(6,2),            -- 0-100
    fundamental_score NUMERIC(6,2),          -- 0-100
    flow_score      NUMERIC(6,2),            -- 0-100
    sentiment_score NUMERIC(6,2),            -- 0-100
    composite_score NUMERIC(6,2),            -- 加权综合分
    -- 判断结论
    direction       VARCHAR(10) NOT NULL,    -- 'bullish' | 'neutral' | 'bearish'
    confidence      NUMERIC(4,2),            -- 0.0 ~ 1.0
    logic_text      TEXT,                    -- LLM 生成的判断逻辑
    -- 交易建议
    suggested_action VARCHAR(30),            -- 'buy' | 'sell' | 'hold' | 'buy_on_pullback'
    entry_zone_low  NUMERIC(12,4),
    entry_zone_high NUMERIC(12,4),
    stop_loss       NUMERIC(12,4),
    target_price    NUMERIC(12,4),
    -- 信号来源追踪
    signal_sources  JSONB,                   -- 哪些因子/规则触发了这个判断
    regime_at_time  JSONB,                   -- 判断时的 regime 状态快照
    -- 回填字段（T+N 后填入）
    actual_ret_1d   NUMERIC(8,6),
    actual_ret_5d   NUMERIC(8,6),
    actual_ret_10d  NUMERIC(8,6),
    actual_ret_20d  NUMERIC(8,6),
    actual_max_up_20d  NUMERIC(8,6),         -- 20日内最大上涨
    actual_max_dd_20d  NUMERIC(8,6),         -- 20日内最大回撤
    is_correct      BOOLEAN,                 -- 方向是否正确
    error_category  VARCHAR(30),             -- 粗分类: 'fundamental' | 'timing' | 'external_event' | 'regime_shift'
    error_detail    TEXT,                     -- LLM 生成的详细归因
    reviewed_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_judgments_symbol_date ON judgments (symbol, judgment_date DESC);
CREATE INDEX idx_judgments_correct ON judgments (is_correct) WHERE is_correct IS NOT NULL;

-- ==================== 盘中信号 ====================

CREATE TABLE intraday_signals (
    id              SERIAL PRIMARY KEY,
    symbol          VARCHAR(20) NOT NULL,
    market          VARCHAR(10) NOT NULL,
    signal_time     TIMESTAMPTZ NOT NULL,
    signal_type     VARCHAR(10) NOT NULL,     -- 'buy' | 'sell'
    strength        VARCHAR(10) NOT NULL,     -- 'strong' | 'moderate' | 'weak'
    trigger_rule    VARCHAR(50) NOT NULL,      -- 触发规则名称
    trigger_detail  JSONB,                    -- 触发细节（哪些因子、具体数值）
    price_at_signal NUMERIC(12,4),
    suggested_price NUMERIC(12,4),            -- 建议入场/出场价
    stop_price      NUMERIC(12,4),
    basis_judgment_id INTEGER REFERENCES judgments(id),  -- 关联的基础分析
    -- 回填
    actual_ret_30m  NUMERIC(8,6),             -- 信号后30分钟收益
    actual_ret_1d   NUMERIC(8,6),             -- 信号后1日收益
    actual_max_favorable NUMERIC(8,6),        -- 最大有利偏移
    actual_max_adverse   NUMERIC(8,6),        -- 最大不利偏移
    signal_quality  NUMERIC(6,2),             -- 信号质量评分（回填计算）
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ==================== 矫正记录 ====================

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
```

### 4.6 Regime 与宏观

```sql
-- Regime 状态快照（每日更新）
CREATE TABLE regime_daily (
    trade_date      DATE NOT NULL,
    market          VARCHAR(10) NOT NULL,  -- 'CN' | 'US'
    -- 四个维度评分
    trend_score     NUMERIC(6,2),          -- 趋势维度 (0-100)
    volatility_score NUMERIC(6,2),         -- 波动率维度 (0-100, 高=高波动)
    breadth_score   NUMERIC(6,2),          -- 市场宽度 (0-100)
    liquidity_score NUMERIC(6,2),          -- 资金/流动性 (0-100)
    -- 综合 regime
    regime_mode     VARCHAR(30) NOT NULL,  -- 'offense' | 'cautious_offense' | 'defense' | 'risk_off'
    trend_direction VARCHAR(10) NOT NULL,  -- 'up' | 'down' | 'sideways'
    volatility_env  VARCHAR(10) NOT NULL,  -- 'low' | 'high'
    -- 对应参数
    signal_threshold_adj NUMERIC(4,2),     -- 信号阈值调整系数
    max_position_pct     NUMERIC(4,2),     -- 建议最大仓位比例
    dimension_weights    JSONB,            -- 各分析维度权重
    -- 详细数据
    detail          JSONB,                 -- 计算用的原始指标
    PRIMARY KEY (trade_date, market)
);

-- 宏观指标（月度/周度更新）
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
-- 常见指标: 'pmi', 'm2_yoy', 'social_financing', '10y_yield', 'cpi_yoy',
--           'fed_rate', 'us_cpi', 'credit_spread', 'unemployment'
```

### 4.7 进化层

```sql
-- 信号质量追踪（每个规则/因子的历史表现）
CREATE TABLE signal_quality_tracker (
    rule_name       VARCHAR(50) NOT NULL,
    market          VARCHAR(10) NOT NULL,
    regime_mode     VARCHAR(30),
    period_start    DATE NOT NULL,
    period_end      DATE NOT NULL,
    total_signals   INTEGER,
    correct_signals INTEGER,
    accuracy        NUMERIC(6,4),
    avg_return      NUMERIC(8,6),         -- 信号后平均收益
    avg_max_dd      NUMERIC(8,6),         -- 信号后平均最大回撤
    ic_value        NUMERIC(8,6),         -- 信息系数
    ir_value        NUMERIC(8,6),         -- 信息比率
    PRIMARY KEY (rule_name, market, regime_mode, period_end)
);

-- 对照组基准
CREATE TABLE benchmark_daily (
    trade_date      DATE NOT NULL,
    market          VARCHAR(10) NOT NULL,
    benchmark_name  VARCHAR(30) NOT NULL,  -- 'buy_and_hold_hs300' | 'momentum_top20' | 'random'
    daily_return    NUMERIC(8,6),
    cumulative_return NUMERIC(12,6),
    max_drawdown    NUMERIC(8,6),
    PRIMARY KEY (trade_date, market, benchmark_name)
);

-- 周度/月度复盘报告
CREATE TABLE review_reports (
    id              SERIAL PRIMARY KEY,
    report_type     VARCHAR(10) NOT NULL,  -- 'weekly' | 'monthly'
    report_date     DATE NOT NULL,
    market          VARCHAR(10),
    -- 核心指标
    total_judgments  INTEGER,
    accuracy_short   NUMERIC(6,4),
    accuracy_mid     NUMERIC(6,4),
    alpha_vs_benchmark NUMERIC(8,6),
    -- LLM 分析
    summary_text    TEXT,
    key_findings    JSONB,
    suggested_changes JSONB,
    -- 报告全文
    full_report_md  TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
```

### 4.8 Wiki 经验库

```sql
-- Wiki 页面索引（实际内容存 markdown 文件，PG 存索引和元数据）
CREATE TABLE wiki_pages (
    page_path       VARCHAR(200) NOT NULL PRIMARY KEY,  -- e.g. 'stocks/600519_SH.md'
    page_type       VARCHAR(20) NOT NULL,  -- 'stock' | 'industry' | 'strategy' | 'system' | 'trade'
    title           VARCHAR(200),
    summary         TEXT,                  -- 页面摘要（LLM 生成）
    tags            TEXT[],
    last_updated    TIMESTAMPTZ,
    update_count    INTEGER DEFAULT 0,
    embedding       vector(1024),          -- 文本嵌入向量（用于 RAG 检索）
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_wiki_embedding ON wiki_pages USING ivfflat (embedding vector_cosine_ops);

-- 经验条目（从复盘中提炼的规律）
CREATE TABLE experience_store (
    id              SERIAL PRIMARY KEY,
    discovery_date  DATE NOT NULL,
    category        VARCHAR(30) NOT NULL,  -- 'market_pattern' | 'stock_specific' | 'signal_tuning' | 'error_pattern'
    market          VARCHAR(10),           -- 'CN' | 'US' | 'both'
    content_text    TEXT NOT NULL,
    evidence        JSONB,                 -- 支撑数据（准确率、样本量、置信度）
    embedding       vector(1024),
    status          VARCHAR(20) DEFAULT 'under_review',  -- 'active' | 'deprecated' | 'under_review'
    applied_count   INTEGER DEFAULT 0,
    last_validated  DATE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_exp_embedding ON experience_store USING ivfflat (embedding vector_cosine_ops);
CREATE INDEX idx_exp_status ON experience_store (status) WHERE status = 'active';
```

### 4.9 系统运维

```sql
-- 数据质量监控
CREATE TABLE data_quality_checks (
    id              SERIAL PRIMARY KEY,
    check_time      TIMESTAMPTZ DEFAULT NOW(),
    source_name     VARCHAR(30) NOT NULL,   -- 'tushare' | 'akshare' | 'yfinance' | 'stocktwits'
    check_type      VARCHAR(30) NOT NULL,   -- 'freshness' | 'completeness' | 'anomaly'
    status          VARCHAR(10) NOT NULL,   -- 'ok' | 'warning' | 'critical'
    detail          JSONB,
    latest_date     DATE,                   -- 该数据源的最新数据日期
    expected_date   DATE                    -- 预期应有的最新日期
);

-- Telegram 命令日志
CREATE TABLE telegram_commands (
    id              SERIAL PRIMARY KEY,
    chat_id         BIGINT NOT NULL,
    command         VARCHAR(50) NOT NULL,
    args            TEXT,
    response_summary TEXT,
    processing_ms   INTEGER,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);
```

---

## 五、模块详细设计

### 5.1 Regime 多维检测

Regime 是整个系统的"总开关"，影响所有分析模块的参数。

```
输入 → 四维度独立评分 → 2×2 矩阵映射 → 输出 regime_mode + 参数集
```

**趋势维度（方向判断）：**

```python
def calc_trend_score(index_data) -> float:
    """
    综合评分 0-100:
    - MA 排列完整度 (40%):
      MA5 > MA20 > MA60 > MA150 > MA200，每满足一对 +8 分，共 4 对 = 32 分
      所有 MA 斜率为正额外 +8 分
    - ADX 趋势强度 (30%):
      ADX > 25 且 +DI > -DI → 按 ADX 值线性映射 0-30
    - 价格结构 (30%):
      近 20 日的 higher high + higher low 数量
      占比 × 30
    """
```

A 股用沪深 300 + 中证 1000 等权平均，美股用 SPY + QQQ 等权平均。

**波动率维度（风险环境）：**

```python
def calc_volatility_score(market) -> float:
    """
    0-100, 越高 = 波动越大 = 风险越高
    - A 股: HV20(沪深300) 在过去 250 日的百分位 × 0.6 +
            涨跌停家数比 (跌停多 → 高分) × 0.4
    - 美股: VIX 值映射 (12→20, 15→35, 20→50, 25→65, 30→80, 40→100) × 0.6 +
            VIX/VIX3M 比值 (>1 表示短期恐慌高于中期) × 0.4
    """
```

**市场宽度维度（参与度）：**

```python
def calc_breadth_score(market) -> float:
    """
    0-100, 越高 = 参与越广 = 越健康
    - A 股: 涨跌比 5 日均值 (33%)
            + 站上 MA20 的股票占比 (33%)
            + (新高家数 - 新低家数) / 总股票数 (33%)
    - 美股: NYSE A/D line 5 日趋势 (33%)
            + % above 200 MA (33%)
            + new high / new low ratio (33%)
    """
```

**资金/流动性维度：**

```python
def calc_liquidity_score(market) -> float:
    """
    0-100, 越高 = 流动性越充裕
    - A 股: 北向资金 20 日净流入趋势 (40%)
            + 融资余额 20 日变化趋势 (30%)
            + 市场成交额 vs 20 日均值 (30%)
    - 美股: 信用利差变化趋势 (50%, 收窄 = 利好)
            + 10Y-2Y 利差 (25%)
            + 市场成交额趋势 (25%)
    """
```

**Regime 映射：**

```python
def determine_regime(trend, volatility, breadth, liquidity, market):
    trend_dir = 'up' if trend > 55 else ('down' if trend < 40 else 'sideways')
    vol_env = 'high' if volatility > 60 else 'low'

    REGIME_MAP = {
        ('up', 'low'):    'offense',           # 进攻模式
        ('up', 'high'):   'cautious_offense',   # 谨慎进攻
        ('down', 'low'):  'defense',            # 防守模式
        ('down', 'high'): 'risk_off',           # 避险模式
        ('sideways', 'low'):  'defense',         # 震荡低波 → 防守
        ('sideways', 'high'): 'risk_off',        # 震荡高波 → 避险
    }

    regime_mode = REGIME_MAP[(trend_dir, vol_env)]

    # 宽度和流动性做修正
    if breadth < 30 and regime_mode == 'offense':
        regime_mode = 'cautious_offense'  # 趋势向上但参与度低，降级
    if liquidity > 70 and regime_mode == 'defense':
        regime_mode = 'cautious_offense'  # 趋势向下但流动性充裕，升级

    return regime_mode, trend_dir, vol_env
```

**Regime 参数集：**

| Regime | 信号阈值系数 | 最大仓位 | 技术面权重 | 基本面权重 | 资金面权重 | 情绪面权重 |
|--------|------------|---------|-----------|-----------|-----------|-----------|
| offense | 1.0 | 80% | 35% | 30% | 20% | 15% |
| cautious_offense | 0.9 | 60% | 30% | 35% | 20% | 15% |
| defense | 0.8 | 40% | 25% | 35% | 25% | 15% |
| risk_off | 0.7 | 20% | 20% | 30% | 30% | 20% |

### 5.2 多周期技术面分析

```
日线 (短期 5-20日)  ─┐
                     ├─→ 周期综合判断 → 趋势状态 + 方向 + 强度
周线 (中期 1-3月)   ─┤
                     │
月线 (仅做长期评估) ─┘
```

**每个周期独立输出：**

```python
@dataclass
class TimeframeAnalysis:
    trend: str             # 'up' | 'down' | 'sideways'
    stage: int             # Weinstein 1/2/3/4
    strength: float        # 0-100 趋势强度
    ma_alignment: float    # 0-100 均线排列完整度
    rs_rank: float         # 0-100 相对强度排名
    momentum: str          # 'accelerating' | 'steady' | 'decelerating'
    key_levels: dict       # {'support': [...], 'resistance': [...]}
    pattern: str           # 'breakout' | 'pullback' | 'consolidation' | 'breakdown' | 'none'
```

**Weinstein Stage 判别（周线级别）：**

```python
def detect_stage(weekly_data) -> int:
    ma30w = weekly_data['close'].rolling(30).mean()  # 30 周线 ≈ 150 日线
    ma30w_slope = (ma30w.iloc[-1] - ma30w.iloc[-4]) / ma30w.iloc[-4]
    price_vs_ma = weekly_data['close'].iloc[-1] / ma30w.iloc[-1] - 1

    if ma30w_slope < -0.02 and price_vs_ma < -0.05:
        return 4  # 下降阶段
    elif ma30w_slope < 0.005 and abs(price_vs_ma) < 0.05:
        if price_vs_ma > 0:
            return 1  # 底部蓄力（价格刚回到均线上方，均线走平）
        else:
            return 4  # 仍在下降
    elif ma30w_slope > 0.005 and price_vs_ma > 0:
        return 2  # 上升阶段
    elif ma30w_slope > -0.005 and price_vs_ma > 0.05:
        return 3  # 顶部派发（价格远高于均线，均线开始走平）
    else:
        return 1  # 默认底部
```

**RS Rank（相对强度排名）：**

```python
def calc_rs_rank(symbol, universe_returns, period=63) -> float:
    """
    计算个股相对全市场的强度排名 (0-100)
    方法: 过去 63 个交易日(一季度) 的收益率，
          在全市场中的百分位排名
    参考: O'Neil RS Rating
    """
    stock_return = universe_returns[symbol].iloc[-period:].sum()
    all_returns = universe_returns.iloc[-period:].sum()
    rank_pct = (all_returns < stock_return).mean() * 100
    return rank_pct
```

**多周期综合：**

```python
def combine_timeframes(daily, weekly) -> dict:
    """
    日线和周线方向一致时，置信度高；
    日线和周线方向矛盾时，以周线为准但降低置信度
    """
    if daily.trend == weekly.trend:
        combined_direction = daily.trend
        confidence_adj = 1.0
    elif weekly.trend == 'up' and daily.trend == 'sideways':
        combined_direction = 'up'  # 周线上升+日线震荡 = 回调中，仍看多
        confidence_adj = 0.7
    elif weekly.trend == 'up' and daily.trend == 'down':
        combined_direction = 'neutral'  # 周线上升+日线下跌 = 可能反转，观望
        confidence_adj = 0.4
    # ... 其他组合
    return {'direction': combined_direction, 'confidence_adj': confidence_adj}
```

### 5.3 基本面分析

**行业差异化评分框架：**

不同行业使用不同的核心指标和权重。初始框架如下，后续通过 wiki 持续优化：

| 行业 | 核心指标 | 权重分配 |
|------|---------|---------|
| 消费（白酒/食品） | ROE, 营收增速, 毛利率, 现金流/净利润 | 盈利质量 40%, 成长性 30%, 估值 30% |
| 科技（半导体/软件） | 营收增速, 研发投入/营收, 毛利率趋势 | 成长性 45%, 盈利趋势 30%, 估值 25% |
| 金融（银行/保险） | 不良率, 息差, 资本充足率, ROA | 资产质量 40%, 盈利能力 30%, 估值 30% |
| 周期（有色/化工） | PB, 产品价格趋势, 产能利用率 | 周期位置 40%, 估值 35%, 盈利弹性 25% |
| 医药 | 研发管线, 营收增速, 毛利率 | 成长性 40%, 盈利质量 30%, 估值 30% |
| 默认 | ROE, 营收增速, 估值百分位 | 盈利 35%, 成长 30%, 估值 35% |

**估值评分标准化：**

```python
def calc_valuation_score(symbol, industry) -> float:
    """
    估值评分 0-100 (越低越便宜)
    - PE_TTM 在行业内分位 (40%)
    - PE_TTM 在自身历史 3 年分位 (30%)
    - PB 在行业内分位 (15%)
    - PEG（如有预期数据）(15%)
    """
```

### 5.4 盘中矫正引擎

**监控频率：** 每 15 分钟，盘中时段（A 股 9:30-15:00，美股 9:30-16:00 ET）

**核心因子（15-20 个，均以 ATR 标准化）：**

| 因子 | 计算 | 信号含义 |
|------|------|---------|
| vwap_deviation | (close - VWAP) / ATR | 偏离 VWAP 程度 |
| intraday_momentum_15m | 15分钟收益 / ATR | 短期动量 |
| intraday_momentum_1h | 1小时收益 / ATR | 中期日内动量 |
| volume_ratio_15m | 最近15min量 / 同时段5日均量 | 量能异常 |
| bid_ask_imbalance | (ask_vol - bid_vol) / (ask_vol + bid_vol) | 盘口失衡 |
| price_vs_day_range | (close - day_low) / (day_high - day_low) | 日内位置 |
| support_distance | (close - nearest_support) / ATR | 距支撑距离 |
| resistance_distance | (nearest_resistance - close) / ATR | 距阻力距离 |
| 15m_rsi | RSI(14) on 15min bars | 短期超买超卖 |
| 15m_macd_cross | MACD 金叉/死叉状态 | 短期动量转折 |

**买入信号触发条件（需同时满足）：**

```python
def check_buy_signal(stock, basis_judgment):
    """前提: basis_judgment.direction in ['bullish'] 且 regime 允许"""
    conditions = [
        abs(stock.vwap_deviation) < 0.5,       # 接近 VWAP
        stock.price_vs_day_range > 0.3,         # 不在日内最低区域
        stock.volume_ratio_15m > 0.8,           # 量能不枯竭
        stock['15m_rsi'] < 65,                  # 没有严重超买
        stock.support_distance > -0.5,          # 没有跌破支撑
        stock.intraday_momentum_1h > -1.0,      # 小时级别没有持续下跌
    ]
    # 强信号: 回踩 VWAP 后 15 分钟 MACD 金叉
    strong_trigger = (
        stock.vwap_deviation > -0.3 and
        stock.vwap_deviation < 0.2 and
        stock['15m_macd_cross'] == 'golden'
    )
    return all(conditions) and strong_trigger
```

**卖出信号触发条件：**

```python
def check_sell_signal(stock, position):
    """任一条件满足即触发"""
    triggers = {
        'stop_loss': stock.close <= position.stop_loss,
        'breakdown': stock.support_distance < -1.0 and stock.volume_ratio_15m > 1.5,
        'vwap_persistent': stock.vwap_deviation < -1.5 and stock.vwap_below_minutes > 30,
        'momentum_collapse': stock.intraday_momentum_1h < -2.0,
    }
    return {k: v for k, v in triggers.items() if v}
```

### 5.5 综合判断引擎

将各维度评分加权合成最终判断：

```python
def generate_judgment(symbol, market, regime):
    tech = technical_analyzer.analyze(symbol)      # 0-100
    fund = fundamental_analyzer.analyze(symbol)    # 0-100
    flow = flow_analyzer.analyze(symbol)           # 0-100
    sent = sentiment_analyzer.analyze(symbol)      # 0-100

    # 权重由 regime 决定
    weights = regime.dimension_weights
    composite = (
        tech * weights['technical'] +
        fund * weights['fundamental'] +
        flow * weights['flow'] +
        sent * weights['sentiment']
    )

    # 方向判断
    if composite > 65:
        direction = 'bullish'
    elif composite < 40:
        direction = 'bearish'
    else:
        direction = 'neutral'

    # 置信度 = 基础置信度 × 多周期一致性调整 × regime 调整
    confidence = calc_base_confidence(composite) * tech.confidence_adj * regime.confidence_factor

    # 检索 wiki 相关经验
    relevant_exp = wiki.search_experience(symbol, direction, regime.mode)

    # LLM 生成逻辑叙事
    logic = llm.generate_analysis(
        symbol=symbol,
        scores={'tech': tech, 'fund': fund, 'flow': flow, 'sent': sent},
        regime=regime,
        wiki_context=relevant_exp,
        output='logic_narrative'
    )

    return Judgment(
        symbol=symbol, direction=direction, confidence=confidence,
        composite_score=composite, logic_text=logic,
        signal_sources={...}, regime_at_time=regime.snapshot()
    )
```

### 5.6 LLM 集成

**模型选择：**

| 任务 | 模型 | 原因 |
|------|------|------|
| 综合分析叙事 | DeepSeek V3 / Qwen3-Coder-Plus | 需要强推理能力 |
| 信息提取（研报/社交） | Qwen3-Mini / Doubao Lite | 轻量任务，控制成本 |
| 文本嵌入（Wiki RAG） | text-embedding-v4 | P7 已验证的嵌入模型 |
| 复盘教练 | DeepSeek V3 | 需要深度分析能力 |

**Prompt 框架（综合分析师）：**

```python
ANALYSIS_PROMPT = """
你是一位经验丰富的投资分析师。请基于以下多维度数据，为 {symbol} 生成投资分析。

## 当前市场环境 (Regime)
{regime_summary}

## 各维度评分
- 技术面: {tech_score}/100 - {tech_summary}
- 基本面: {fund_score}/100 - {fund_summary}
- 资金面: {flow_score}/100 - {flow_summary}
- 情绪面: {sent_score}/100 - {sent_summary}

## 相关历史经验（来自 Wiki）
{wiki_context}

## 要求
1. 识别各维度之间的一致性和矛盾
2. 给出短期(1-2周)和中期(1-3月)的方向判断和置信度
3. 如果建议买入/卖出，给出具体价位区间和止损位
4. 说明"如果判断错了，最可能是因为什么"
5. 用 200-400 字完成，语言简洁直接，避免废话
"""
```

### 5.7 Telegram 双向通信

**命令路由：**

| 命令 | 功能 | 响应时间目标 |
|------|------|------------|
| `/analyze {symbol}` | 触发即时多维分析 | < 30s（含 LLM） |
| `/status` | 当前持仓风险概览 | < 3s |
| `/signal` | 活跃买卖信号列表 | < 3s |
| `/regime` | 当前 A 股/美股 regime 状态 | < 3s |
| `/wiki {symbol}` | Wiki 中关于该股的已知信息 | < 5s |
| `/add {symbol} {price} {shares}` | 记录建仓 | < 2s |
| `/close {symbol} {price}` | 记录平仓 | < 2s |
| `/watchlist add/remove {symbol}` | 管理候选池 | < 2s |
| `/review` | 本周判断复盘摘要 | < 15s |
| `/quality {rule_name}` | 查看特定信号的历史表现 | < 5s |
| `/macro` | 宏观环境 dashboard | < 5s |
| `/help` | 命令列表 | < 1s |

**实现架构：**

```python
# Telegram webhook → FastAPI → 命令路由 → 对应处理器 → 格式化响应 → 发回

from telegram import Update, Bot
from telegram.ext import Application, CommandHandler

async def cmd_analyze(update: Update, context):
    symbol = context.args[0]
    # 发送"分析中..."先回复
    msg = await update.message.reply_text(f"正在分析 {symbol}，请稍候...")
    # 异步执行分析
    result = await analysis_engine.full_analysis(symbol)
    # 格式化
    text = format_analysis_for_telegram(result)
    await msg.edit_text(text, parse_mode='Markdown')

app = Application.builder().token(BOT_TOKEN).build()
app.add_handler(CommandHandler("analyze", cmd_analyze))
app.add_handler(CommandHandler("status", cmd_status))
# ... 其他命令
```

### 5.8 数据质量监控层

```python
class DataQualityMonitor:
    """每日 08:00 运行，盘中每小时检查一次"""

    def check_freshness(self):
        """检查每个数据源的最新数据是否在预期范围内"""
        checks = {
            'market_bars_daily': ('CN', self.last_trade_date),
            'features_daily': ('CN', self.last_trade_date),
            'fundamentals_daily': ('CN', self.last_trade_date),
            'northbound_daily': ('CN', self.last_trade_date),
            'intraday_bars': ('CN', datetime.now() - timedelta(minutes=20)),
        }
        for table, (market, expected) in checks.items():
            actual = db.query(f"SELECT MAX(trade_date) FROM {table} WHERE market='{market}'")
            if actual < expected:
                self.alert('warning', f'{table} 数据滞后: 最新={actual}, 预期={expected}')

    def check_completeness(self):
        """检查候选池中每只票是否有完整数据"""
        universe = db.query("SELECT symbol FROM stock_universe WHERE active = TRUE")
        for symbol in universe:
            missing = []
            if not has_recent_bars(symbol): missing.append('bars')
            if not has_recent_features(symbol): missing.append('features')
            if not has_recent_fundamentals(symbol): missing.append('fundamentals')
            if missing:
                self.alert('warning', f'{symbol} 缺少数据: {missing}')

    def check_anomaly(self):
        """检查数据异常（价格跳变、成交量为 0 等）"""
        anomalies = db.query("""
            SELECT symbol, trade_date,
                   ABS(close/LAG(close) OVER (PARTITION BY symbol ORDER BY trade_date) - 1) as ret
            FROM market_bars_daily
            WHERE trade_date = current_date AND ABS(ret) > 0.20
        """)
        for row in anomalies:
            self.alert('info', f'{row.symbol} 价格异动: {row.ret:.1%}')
```

---

## 六、候选池管理

### 6.1 自选股（70-80%）

通过 Telegram 命令或 YAML 配置文件管理：

```yaml
# config/watchlist.yaml
manual:
  CN:
    - symbol: "600519.SH"
      name: "贵州茅台"
      added: "2026-04-16"
      reason: "白酒龙头，长期关注"
    - symbol: "002475.SZ"
      name: "立讯精密"
  US:
    - symbol: "AAPL"
      name: "Apple"
    - symbol: "NVDA"
      name: "NVIDIA"
```

### 6.2 系统推荐（20-30%）

每周末运行一次自动扫描，筛选逻辑：

**第一层：热度异动扫描（每日）**

```python
def scan_hot_stocks():
    """扫描市场异动，发现候选池未覆盖的热门票"""
    # A 股
    cn_candidates = []
    # 涨幅榜前 20 中不在候选池的
    cn_candidates += get_top_gainers(market='CN', top_n=20, exclude=universe)
    # 成交额异动（今日成交额 > 5 日均值 × 3）
    cn_candidates += get_volume_surge(market='CN', ratio=3.0, exclude=universe)
    # 龙虎榜（机构净买入前 10）
    cn_candidates += get_lhb_institutional_buy(top_n=10, exclude=universe)

    # 美股
    us_candidates = []
    us_candidates += get_top_gainers(market='US', top_n=20, exclude=universe)
    us_candidates += get_volume_surge(market='US', ratio=3.0, exclude=universe)

    return cn_candidates + us_candidates
```

**第二层：技术形态筛选（每周末）**

```python
def scan_technical_setups():
    """在全市场扫描符合条件的技术形态"""
    candidates = []
    # 条件: Stage 2 + RS Rank > 80 + 近期波动率收缩（VCP 初筛）
    for symbol in full_market_universe:
        if (detect_stage(symbol) == 2 and
            calc_rs_rank(symbol) > 80 and
            is_volatility_contracting(symbol)):
            candidates.append(symbol)
    return candidates
```

推荐的票进入 `stock_universe` 后，自动触发基础分析（模块 5.5）。

---

## 七、调度时间表

### 7.1 A 股（时区 Asia/Shanghai）

| 时间 | 任务 | 说明 |
|------|------|------|
| 07:30 | data_quality_check | 数据新鲜度 + 完整性检查 |
| 08:00 | pre_market_analysis | 对候选池所有 A 股执行多维分析 + 生成判断 |
| 08:30 | pre_market_push | Telegram 推送盘前分析摘要（只推高置信度） |
| 09:30-15:00 每15分钟 | intraday_calibration | 盘中矫正 + 买卖点检测 |
| 09:30-15:00 每30分钟 | regime_pulse | 盘中 regime 微调（仅波动率和宽度维度） |
| 15:10 | post_market_summary | 盘后汇总：当日表现 + 信号回顾 |
| 15:30 | data_pipeline_pull | 拉取 A 股日线/财报/资金流（超时 900s） |
| 15:45 | feature_compute | 计算/更新特征 |
| 16:00 | regime_update | 更新 A 股 regime（全维度） |
| 16:10 | backfill_judgments | 回填 T-5/T-10/T-20 判断的实际结果 |
| 16:20 | backfill_signals | 回填盘中信号的实际结果 |
| 16:30 | signal_quality_update | 更新信号质量追踪器 |
| 16:40 | post_market_push | Telegram 推送盘后复盘 |
| 周六 10:00 | weekly_review | 生成周度复盘报告 |
| 周六 10:30 | scanner_weekly | 执行技术形态扫描（第二层推荐） |
| 每月1日 10:00 | monthly_review | 生成月度复盘 + 经验提炼 |

### 7.2 美股（时区 US/Eastern，需转换为 Asia/Shanghai）

| 上海时间 | 任务 | 说明 |
|---------|------|------|
| 21:00 | us_pre_market | 美股盘前分析（美东 9:00） |
| 21:30-04:00 每15分钟 | us_intraday | 美股盘中监控（美东 9:30-16:00） |
| 04:30 | us_post_market | 美股盘后汇总 |
| 05:00 | us_data_pull | 拉取美股日线数据 |
| 05:15 | us_regime_update | 更新美股 regime |

### 7.3 跨市场

| 时间 | 任务 | 说明 |
|------|------|------|
| 每周日 10:00 | wiki_lint | Wiki 健康检查（矛盾/过期/孤立页面） |
| 每日 06:00 | social_sentiment_scan | 社交情绪每日扫描 |
| 每周一 08:00 | macro_update | 更新宏观指标 |

---

## 八、LLM Wiki 架构

### 8.1 目录结构

```
wiki/
├── index.md                      # 全局索引（LLM 自动维护）
├── log.md                        # 操作日志（append-only）
├── schema.md                     # Wiki 约定和 LLM 指令
│
├── stocks/                       # 个股页面
│   ├── CN/
│   │   ├── 600519_SH.md          # 贵州茅台
│   │   ├── 002475_SZ.md          # 立讯精密
│   │   └── ...
│   └── US/
│       ├── AAPL.md
│       ├── NVDA.md
│       └── ...
│
├── industries/                   # 行业分析框架
│   ├── CN/
│   │   ├── liquor.md             # 白酒行业
│   │   ├── semiconductor.md      # 半导体
│   │   └── ...
│   └── US/
│       ├── big_tech.md
│       └── ...
│
├── strategies/                   # 策略经验
│   ├── vcp_in_a_shares.md        # VCP 形态在 A 股的统计
│   ├── regime_playbook.md        # 不同 regime 下的操作手册
│   ├── signal_reliability.md     # 各信号在不同环境下的可靠性
│   ├── earnings_patterns.md      # 财报季的价格模式
│   └── behavioral_traps.md       # 交易行为陷阱（来自你的历史复盘）
│
├── system/                       # 系统改进记录
│   ├── changelog.md              # 参数/规则/因子变更历史
│   ├── factor_performance.md     # 因子表现追踪
│   └── architecture_decisions.md # 架构决策记录
│
├── macro/                        # 宏观环境
│   ├── cn_macro_outlook.md       # A 股宏观展望
│   ├── us_macro_outlook.md       # 美股宏观展望
│   └── global_events.md          # 重大事件追踪
│
└── trades/                       # 交易记录
    ├── active_positions.md       # 当前持仓及逻辑
    └── trade_journal.md          # 已平仓交易复盘精选
```

### 8.2 Wiki Schema（LLM 指令文件）

```markdown
# wiki/schema.md — P10-AlphaRadar Wiki 约定

## 页面类型及模板

### 个股页面 (stocks/)
每只票一个文件，首次分析时创建，后续分析时更新（不新建）。

模板:
---
symbol: {symbol}
market: {CN|US}
name: {名称}
industry: {行业}
last_updated: {日期}
current_stage: {Weinstein Stage}
---

## 公司概况
[主营业务、竞争力、核心风险 — 首次写入后较少更新]

## 当前状态
[最近一次分析的结论摘要 — 每次分析后覆盖更新]
- 技术面: ...
- 基本面: ...
- 资金面: ...
- 综合判断: ...

## 关键价位
[历史重要支撑/阻力位 — 累积更新]

## 行为模式
[已观察到的该股特有规律 — 累积更新]
例: "财报后首日高开低走概率 68% (n=8)"

## 历史判断摘要
[最近 5 次判断的简要记录 — 滚动更新，只保留最近 5 条]
| 日期 | 方向 | 结果 | 简评 |
|------|------|------|------|

## 操作规则

1. **更新优先于新建**: 分析已有票时，更新现有页面，不创建新页面
2. **Actuel/Archive 模式**: "当前状态"章节直接覆盖；"行为模式"和"历史判断"追加
3. **长度控制**: 每个页面不超过 300 行；超过时压缩历史部分
4. **交叉引用**: 用 [[链接]] 语法关联行业页面和策略页面
5. **每次更新后**: 更新 index.md 中该页面的摘要行
```

### 8.3 Wiki 与系统的集成

```python
class WikiManager:
    def __init__(self, wiki_dir, db_conn, embedding_model):
        self.wiki_dir = Path(wiki_dir)
        self.db = db_conn
        self.embedder = embedding_model

    def update_stock_page(self, symbol, analysis_result):
        """分析完成后更新个股 wiki 页面"""
        page_path = f"stocks/{analysis_result.market}/{symbol.replace('.', '_')}.md"
        existing = self.read_page(page_path)
        if existing:
            updated = self.llm_update_page(existing, analysis_result)
        else:
            updated = self.llm_create_page(symbol, analysis_result)
        self.write_page(page_path, updated)
        self.update_index(page_path, analysis_result.summary)
        self.update_embedding(page_path, updated)

    def search_experience(self, symbol, direction, regime_mode, top_k=3):
        """RAG 检索相关经验"""
        query = f"{symbol} {direction} {regime_mode}"
        query_vec = self.embedder.encode(query)
        results = self.db.query("""
            SELECT content_text, evidence
            FROM experience_store
            WHERE status = 'active'
            ORDER BY embedding <=> %s::vector
            LIMIT %s
        """, [query_vec, top_k])
        return results

    def lint(self):
        """定期健康检查"""
        # 检查孤立页面（无入链接）
        # 检查过期内容（last_updated > 30 天）
        # 检查矛盾（同一票在不同页面的判断不一致）
        pass
```

---

## 九、数据源与 API 清单

| 数据类型 | A 股 | 美股 | 获取频率 |
|---------|------|------|---------|
| 日线 OHLCV | Tushare Pro (主) → AkShare (备) | yfinance (免费) → Polygon.io (付费备选) | 日更 |
| 分钟线 | pytdx (免费, 通达信协议) | yfinance (1m 最近7天) → Polygon (更长) | 盘中 15min |
| 财务报表 | Tushare Pro (income/balancesheet/cashflow) | yfinance financials / SimFin | 季更 |
| 基本面指标 | Tushare Pro (daily_basic) | yfinance info | 日更 |
| 资金流 | Tushare Pro (moneyflow) | — | 日更 |
| 北向资金 | AkShare (stock_hsgt_north_net_flow) | — | 日更 |
| 融资融券 | Tushare Pro (margin_detail) | — | 日更 |
| 龙虎榜 | Tushare Pro (top_inst) | — | 日更 |
| 社交情绪 | 东方财富股吧帖子数 (AkShare) | StockTwits API (免费) | 日更/盘中 |
| 研报评级 | 东方财富研报 (AkShare) | — | 周更 |
| 指数/宏观 | AkShare (各类宏观指标) | yfinance (VIX, 利率ETF) / FRED API | 日更/月更 |
| 行业分类 | Tushare Pro (industry_classify) | yfinance sector | 静态 |

**API 成本估算（月度）：**

| 服务 | 费用 | 说明 |
|------|------|------|
| Tushare Pro | ¥500/年 | 已有，足够覆盖 A 股需求 |
| yfinance | 免费 | 美股基础数据，有频率限制 |
| StockTwits | 免费 | 基础 API |
| LLM (DeepSeek V3) | ~¥200-500/月 | 取决于分析频率 |
| LLM (Qwen3-Mini) | ~¥50-100/月 | 轻量任务 |
| Embedding (text-embedding-v4) | ~¥50/月 | Wiki RAG |
| PostgreSQL (云) | ~¥100-300/月 | 或本地部署免费 |
| **总计** | **~¥400-1000/月** | 不含 Polygon 等付费美股源 |

---

## 十、目录结构

```
P10-AlphaRadar/
├── config/
│   ├── settings.yaml              # 主配置（数据库连接、API 密钥引用、调度参数）
│   ├── watchlist.yaml             # 候选池（手动管理部分）
│   ├── regime_params.yaml         # Regime 参数集（四种模式的参数）
│   └── industry_frameworks.yaml   # 行业差异化评分框架
│
├── core/
│   ├── regime/
│   │   ├── detector.py            # 四维度 Regime 检测
│   │   ├── trend.py               # 趋势维度计算
│   │   ├── volatility.py          # 波动率维度
│   │   ├── breadth.py             # 市场宽度维度
│   │   └── liquidity.py           # 资金/流动性维度
│   ├── analysis/
│   │   ├── technical.py           # 多周期技术面分析
│   │   ├── fundamental.py         # 基本面分析
│   │   ├── flow.py                # 资金面分析
│   │   ├── sentiment.py           # 情绪面分析
│   │   ├── composite.py           # 综合判断引擎
│   │   └── stage_detector.py      # Weinstein Stage 判别
│   ├── intraday/
│   │   ├── calibrator.py          # 盘中矫正引擎
│   │   ├── signal_detector.py     # 买卖点检测
│   │   └── factors.py             # 盘中因子计算
│   ├── scanner/
│   │   ├── hot_scanner.py         # 热度异动扫描
│   │   └── technical_scanner.py   # 技术形态扫描
│   ├── risk/
│   │   ├── position_sizer.py      # 仓位计算器
│   │   └── portfolio_check.py     # 组合风险检查
│   └── evolution/
│       ├── judgment_tracker.py    # 判断追踪 + 回填
│       ├── signal_quality.py      # 信号质量评分
│       ├── reviewer.py            # 周度/月度复盘引擎
│       └── benchmark.py           # 对照组基准计算
│
├── data/
│   ├── sources/
│   │   ├── tushare_client.py      # Tushare 数据采集（复用 P4/P6+）
│   │   ├── akshare_client.py      # AkShare 采集
│   │   ├── yfinance_client.py     # 美股数据采集
│   │   ├── pytdx_client.py        # A 股盘中实时
│   │   ├── stocktwits_client.py   # StockTwits 情绪
│   │   └── eastmoney_client.py    # 东财股吧/研报
│   ├── pipeline/
│   │   ├── daily_pull.py          # 日线数据拉取
│   │   ├── intraday_pull.py       # 分钟线拉取
│   │   ├── fundamental_pull.py    # 财报数据拉取
│   │   └── feature_compute.py     # 特征计算
│   ├── quality/
│   │   └── monitor.py             # 数据质量监控
│   └── migration/
│       └── migrate_from_p6.py     # P6+ 数据迁移脚本
│
├── db/
│   ├── connection.py              # PostgreSQL 连接池管理
│   ├── schema.sql                 # 完整建表 SQL
│   └── migrations/                # 数据库迁移脚本 (Alembic)
│
├── llm/
│   ├── client.py                  # LLM API 客户端（支持 DeepSeek/Qwen/Doubao）
│   ├── prompts.py                 # Prompt 模板
│   ├── embedder.py                # 文本嵌入
│   └── wiki_manager.py            # Wiki 管理器（CRUD + RAG + Lint）
│
├── bot/
│   ├── telegram_bot.py            # Telegram Bot 主逻辑
│   ├── commands/                   # 各命令处理器
│   │   ├── analyze.py
│   │   ├── status.py
│   │   ├── signal.py
│   │   ├── watchlist.py
│   │   ├── position.py
│   │   ├── review.py
│   │   └── regime.py
│   └── formatter.py               # Telegram 消息格式化
│
├── api/
│   ├── main.py                    # FastAPI 入口
│   └── routes/                    # API 路由
│
├── scheduler/
│   └── scheduler.py               # APScheduler 调度器
│
├── wiki/                          # LLM Wiki（Markdown 文件）
│   ├── index.md
│   ├── log.md
│   ├── schema.md
│   ├── stocks/
│   ├── industries/
│   ├── strategies/
│   ├── system/
│   ├── macro/
│   └── trades/
│
├── frontend/                      # React 前端看板
│
├── scripts/
│   ├── migrate_from_p6.py         # P6+ 数据迁移
│   ├── init_wiki.py               # Wiki 冷启动（导入已知经验）
│   ├── setup_db.py                # 数据库初始化
│   └── backtest_signals.py        # 历史信号回测
│
├── tests/
│
├── docker-compose.yml             # PostgreSQL + TimescaleDB + 应用
├── requirements.txt
├── pyproject.toml
└── README.md
```

---

## 十一、Docker 部署

```yaml
# docker-compose.yml
version: '3.8'

services:
  db:
    image: timescale/timescaledb:latest-pg15
    environment:
      POSTGRES_DB: alpharadar
      POSTGRES_USER: radar
      POSTGRES_PASSWORD: ${DB_PASSWORD}
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ./db/schema.sql:/docker-entrypoint-initdb.d/01-schema.sql
    ports:
      - "5432:5432"
    command: >
      postgres
        -c shared_preload_libraries='timescaledb,vector'
        -c max_connections=100
        -c shared_buffers=2GB
        -c work_mem=256MB

  app:
    build: .
    environment:
      DATABASE_URL: postgresql://radar:${DB_PASSWORD}@db:5432/alpharadar
      TUSHARE_TOKEN: ${TUSHARE_TOKEN}
      TELEGRAM_BOT_TOKEN: ${TELEGRAM_BOT_TOKEN}
      DEEPSEEK_API_KEY: ${DEEPSEEK_API_KEY}
    volumes:
      - ./wiki:/app/wiki
      - ./config:/app/config
    depends_on:
      - db
    ports:
      - "8000:8000"

volumes:
  pgdata:
```

---

## 十二、实施路线图

### Phase 0: 基础设施（1 周）

- [ ] PostgreSQL + TimescaleDB + pgvector 部署
- [ ] 建表 + 索引
- [ ] P6+ 数据迁移脚本开发 + 执行
- [ ] Telegram Bot 基础框架 + `/help` `/status` 命令
- [ ] 数据质量监控框架

### Phase 1: 核心分析（2-3 周）

- [ ] Regime 四维检测（A 股先行）
- [ ] 多周期技术面分析（日线 + 周线）
- [ ] Weinstein Stage + RS Rank
- [ ] 候选池管理（YAML + Telegram 命令）
- [ ] 判断记录 + 回填框架
- [ ] Telegram `/analyze` `/regime` `/watchlist` 命令
- [ ] 对照组基准计算

### Phase 2: 分析维度扩展（2-3 周）

- [ ] 基本面分析模块（财报拉取 + 行业差异化评分）
- [ ] 资金面分析（北向/融资融券/主力资金）
- [ ] LLM 综合分析师集成
- [ ] LLM Wiki 初始化 + 冷启动（导入已知经验）
- [ ] Wiki RAG 检索集成
- [ ] 宏观/行业预处理层

### Phase 3: 盘中 + 信号（2 周）

- [ ] 盘中 15 分钟矫正引擎
- [ ] 买卖点信号检测
- [ ] 盘中 Telegram 信号推送
- [ ] 矫正记录追踪
- [ ] Telegram `/signal` 命令

### Phase 4: 美股 + 情绪面（2 周）

- [ ] 美股数据接入（yfinance）
- [ ] 美股 regime 独立检测
- [ ] StockTwits 情绪接入
- [ ] 东财股吧热度接入
- [ ] 情绪面评分集成到综合判断

### Phase 5: 进化引擎（2 周）

- [ ] 信号质量追踪器
- [ ] 周度自动复盘报告
- [ ] LLM 复盘教练
- [ ] 经验提炼 → Wiki 写入
- [ ] 月度复盘 + 参数审查
- [ ] 风控/仓位计算器

### Phase 6: 前端 + 优化（2 周）

- [ ] React 看板（个股分析卡片 + 信号仪表盘 + 复盘看板）
- [ ] 技术形态扫描器（每周推荐）
- [ ] Wiki lint 自动化
- [ ] 系统健康 dashboard

**总工期预估：12-14 周（Phase 1 完成后约 4 周即有可用的最小系统）**

---

## 十三、关键风险与缓解

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| 数据管道超时（P4 前车之鉴） | 分析基于过期数据 | 数据质量监控层 + 超时熔断 + 分析结果标注数据时效 |
| LLM 幻觉导致错误分析 | 误导交易决策 | LLM 结论必须有数据支撑，不独立做判断；所有分析带置信度 |
| 过拟合近期市场特征 | regime 切换后系统失效 | 前 6 个月用固定权重，不自动调参；对照组基准持续比对 |
| 信息过载（推送太多） | 用户开始忽略系统 | 信号强度阈值，每日推送上限 5 条核心信息 |
| 美股数据源不稳定 | 美股分析断档 | yfinance 免费兜底，关键时刻可手动触发 |
| Wiki 知识腐化 | 过期经验误导分析 | 定期 lint + 经验条目设有效期 + 定期验证 |

---

## 附录 A: P6+ 数据迁移脚本规格

```python
"""
scripts/migrate_from_p6.py

从 P6+ 的 DuckDB 迁移历史数据到 P10 的 PostgreSQL

用法:
  python scripts/migrate_from_p6.py \
    --source /path/to/p6plus/data/agu.duckdb \
    --target postgresql://radar:pass@localhost:5432/alpharadar \
    --tables market_bars_daily,features_daily,fundamentals_daily \
    --batch-size 100000 \
    --verify

流程:
  1. 只读连接 P6+ DuckDB
  2. 对每张表:
     a. 查询总行数和日期范围
     b. 分批读取（pandas DataFrame, batch_size 行/批）
     c. 添加 market='CN' 列（P6+ 只有 A 股）
     d. 写入 PostgreSQL（pandas to_sql + psycopg2）
     e. 验证: 对比源和目标的行数、日期范围、随机抽样校验
  3. 输出迁移报告

字段映射（P6+ → P10）:
  market_bars_daily:
    ts_code → symbol (格式: 600519.SH 不变)
    trade_date → trade_date (已是 DATE)
    其余字段名一致

  features_daily:
    需检查 P6+ 的特征列名是否与 P10 schema 一致
    不一致的列放入 extra JSONB

注意:
  - P6+ 的 DuckDB 可能有 ~20GB，预计迁移时间 30-60 分钟
  - 迁移期间不要启动 P10 的数据管道，避免主键冲突
  - 建议在非交易日执行
"""
```

---

## 附录 B: Wiki 冷启动内容

系统启动时，手动导入以下已知经验到 Wiki：

```markdown
# wiki/strategies/behavioral_traps.md
# 交易行为陷阱（来自 14 个月交易记录分析）

## 当前
- 处置效应: 盈利时急于止盈，亏损时死扛。历史数据显示平均盈利持仓 5 天，亏损持仓 15 天
- FOMO 早盘买入: 10:00 前买入的交易胜率仅 38%，10:00 后买入胜率 52%
- 卖后追回: "卖出后又买回更高价"导致的净亏损占总亏损的 23%
- 行业集中: 盈利几乎全部来自 1-2 个行业（稀土/有色/半导体），其他行业整体亏损

## 规则
- 系统应在 10:00 前的买入信号上标注 "早盘警告"
- 卖出后 3 个交易日内对同一只票的买入信号自动降级为 "观察"
- 单行业持仓不超过总仓位 40%
```

```markdown
# wiki/strategies/regime_playbook.md
# 不同市场环境下的操作手册

## 当前
- 进攻模式 (offense): 正常仓位，积极跟随趋势，止损可放宽至 10%
- 谨慎进攻 (cautious_offense): 只做 Stage 2 + RS>80 的强势股，止损 8%
- 防守模式 (defense): 仓位不超过 40%，只做高确定性机会（突破+放量）
- 避险模式 (risk_off): 仓位不超过 20%，持有现金等待，不追涨
- 从历史来看，A 股 regime 从 offense 切换到 defense 时，如果不减仓，平均后续 20 天亏损 6-8%

## 关键经验
- Regime 切换的第一天就应该行动，不要等确认
- 高波动环境下技术面信号的准确率下降约 15-20%
```
