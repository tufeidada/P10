# P10-Backtest 系统设计文档

> 版本: v1.0 | 日期: 2026-04-17
>
> 本文档为 P10-AlphaRadar 的前置回测验证系统。
> 目标：在投入完整 P10 开发之前，先用 4 周时间验证核心分析逻辑是否有效。

---

## 一、项目定位

**P10-Backtest 是一个独立的回测系统**，不是 P10 的一部分。它模拟"如果 P10 在 2025-09-01 到 2026-04-17 期间运行，表现会如何"。

**简化对比：**

| 模块 | 完整 P10 | P10-Backtest（本项目） |
|------|---------|----------------------|
| Regime 判断 | ✅ 四维度完整 | ✅ 四维度完整 |
| 技术面分析 | ✅ 多周期 + Stage + RS | ✅ 多周期 + Stage + RS |
| 基本面分析 | ✅ 行业差异化权重 | ✅ 行业差异化权重 |
| 资金面分析 | ✅ 主力+北向+融资 | ✅ 主力+北向+融资 |
| 情绪面分析 | ✅ 社交+市场情绪 | ⚠️ 仅市场级情绪（VIX/涨跌停） |
| LLM 综合叙事 | ✅ | ❌ 不做 |
| Wiki 经验库 | ✅ | ❌ 不做 |
| 盘中 15 分钟信号 | ✅ | ❌ 不做（日频为主） |
| Telegram 交互 | ✅ | ❌ 不做 |
| 前端看板 | ✅ | ⚠️ 仅输出报告（Markdown + Excel） |
| 判断追踪与回填 | ✅ 自动 | ✅ 回测自动完成 |
| 自我进化 | ✅ | ❌ 不做（回测不需要） |

**为什么砍这些：**
- LLM 叙事：回测中价值难量化，且成本高、耗时长
- Wiki：依赖长期积累，回测中没有价值
- 盘中信号：需要分钟级历史数据，成本高且复杂
- Telegram：回测只需要生成报告
- 前端：回测的产出是报告，不需要实时界面

---

## 二、核心原则：严格避免前视偏差（Look-ahead Bias）

**这是回测成败的关键**。如果违反了 PIT（Point-in-Time）原则，回测结果会虚高，上线后失败。

**严格的 PIT 规则：**

| 数据类型 | PIT 时间戳 | 陷阱 |
|---------|-----------|------|
| 日线 OHLCV | `trade_date` 收盘后可用 | T 日的判断只能用 T-1 及之前的数据 |
| 财务报表 | `announce_date`（公告日）而非 `report_date`（报告期） | 2025 Q4 财报报告期 2025-12-31，但可能 2026-03 才公告 |
| 资金流 | `trade_date` 次日可用 | 大单资金流数据 Tushare 通常 T+1 日可查 |
| 北向资金 | `trade_date` 收盘后可用 | 个股级别可能延迟到 T+1 |
| 分析师预期 | 研报发布日 | 不能用最新的一致预期去回测过去的判断 |
| 行业分类 | **当时的分类**，而非现在的 | 申万行业分类会调整，要用历史快照 |

**实施方式：**
- 所有数据查询必须通过 `PITDataLoader` 接口，自动加上 `WHERE available_date <= @current_date` 过滤
- 每张表都要有 `available_date` 字段表示该记录"何时变得可用"
- 禁止任何代码直接查原始数据，必须走 PITDataLoader

---

## 三、技术栈

- Python 3.11+
- PostgreSQL 15 + TimescaleDB（回测数据单独存一个 schema `backtest`）
- pandas / numpy / scipy
- TA-Lib（技术指标，如果安装有困难用 pandas-ta 替代）
- Tushare Pro（A 股数据）
- AkShare（辅助数据）
- yfinance（美股数据）
- matplotlib + plotly（图表）
- openpyxl（Excel 报告输出）
- pydantic（配置校验）
- structlog（日志）

---

## 四、项目结构

```
P10-Backtest/
├── README.md
├── CLAUDE.md                           # Claude Code 指令文件
├── docs/
│   └── backtest-spec.md                # 本文档
├── config/
│   ├── settings.yaml                   # 主配置
│   ├── watchlist.yaml                  # 回测候选池
│   ├── regime_params.yaml              # Regime 参数
│   └── industry_frameworks.yaml        # 行业评分框架
├── db/
│   ├── schema.sql                      # 数据库表
│   └── connection.py
├── data/
│   ├── fetchers/
│   │   ├── tushare_fetcher.py          # Tushare 历史数据拉取
│   │   ├── akshare_fetcher.py          # AkShare 辅助数据
│   │   └── yfinance_fetcher.py         # 美股数据
│   ├── pit_loader.py                   # PIT 数据加载器（核心）
│   └── data_quality.py                 # 数据质量检查
├── core/
│   ├── regime/
│   │   ├── detector.py
│   │   ├── trend.py
│   │   ├── volatility.py
│   │   ├── breadth.py
│   │   └── liquidity.py
│   ├── analysis/
│   │   ├── technical.py
│   │   ├── fundamental.py
│   │   ├── flow.py
│   │   ├── sentiment_market.py         # 仅市场级情绪
│   │   ├── composite.py
│   │   └── stage_detector.py
│   └── features.py                     # 特征计算（一次计算，多次使用）
├── backtest/
│   ├── engine.py                       # 主回测引擎
│   ├── portfolio.py                    # 模拟账户
│   ├── execution.py                    # 成交模拟（滑点、交易成本）
│   ├── benchmarks.py                   # 三个对照组
│   └── rules.py                        # 建仓/平仓规则
├── evaluation/
│   ├── metrics.py                      # 评估指标（IC/IR/Sharpe等）
│   ├── attribution.py                  # 归因分析
│   └── reporter.py                     # 报告生成
├── scripts/
│   ├── 01_init_db.py                   # 数据库初始化
│   ├── 02_fetch_data.py                # 历史数据拉取
│   ├── 03_compute_features.py          # 特征预计算
│   ├── 04_run_backtest.py              # 运行回测
│   └── 05_generate_report.py           # 生成报告
├── reports/                            # 输出目录（.gitignore）
│   ├── backtest_{timestamp}.xlsx
│   └── backtest_{timestamp}.md
├── tests/
├── docker-compose.yml
├── requirements.txt
└── pyproject.toml
```

---

## 五、数据库 Schema

所有表放在 `backtest` schema 下，与未来的 P10 生产数据隔离。

### 5.1 核心数据表

```sql
CREATE SCHEMA backtest;
SET search_path TO backtest, public;

-- 候选池
CREATE TABLE universe (
    symbol          VARCHAR(20) NOT NULL PRIMARY KEY,
    market          VARCHAR(10) NOT NULL,  -- 'CN' | 'US'
    name            VARCHAR(100),
    industry        VARCHAR(50),
    industry_framework VARCHAR(30),        -- 'consumer_staples' | 'technology' | ...
    added_date      DATE,
    notes           TEXT
);

-- 日线 OHLCV（核心数据，必须 PIT）
CREATE TABLE market_bars_daily (
    symbol          VARCHAR(20) NOT NULL,
    market          VARCHAR(10) NOT NULL,
    trade_date      DATE NOT NULL,
    open            NUMERIC(14,4),
    high            NUMERIC(14,4),
    low             NUMERIC(14,4),
    close           NUMERIC(14,4),
    volume          BIGINT,
    amount          NUMERIC(20,2),
    adj_close       NUMERIC(14,4),          -- 复权收盘价
    adj_factor      NUMERIC(12,6) DEFAULT 1,
    turnover_rate   NUMERIC(8,4),           -- 换手率
    available_date  DATE NOT NULL,          -- PIT: 等于 trade_date，当日收盘后可用
    PRIMARY KEY (symbol, trade_date)
);
SELECT create_hypertable('market_bars_daily', 'trade_date', if_not_exists => TRUE);
CREATE INDEX idx_mbd_available ON market_bars_daily (available_date);

-- 基本面每日指标
CREATE TABLE fundamentals_daily (
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

-- 财务报表（PIT 关键表）
CREATE TABLE financials_quarterly (
    symbol              VARCHAR(20) NOT NULL,
    report_date         DATE NOT NULL,       -- 报告期
    announce_date       DATE NOT NULL,       -- 公告日（PIT 关键！）
    revenue             NUMERIC(20,2),
    revenue_yoy         NUMERIC(10,4),
    revenue_qoq         NUMERIC(10,4),
    net_profit          NUMERIC(20,2),
    np_yoy              NUMERIC(10,4),
    gross_margin        NUMERIC(10,4),
    net_margin          NUMERIC(10,4),
    total_assets        NUMERIC(20,2),
    total_liab          NUMERIC(20,2),
    debt_ratio          NUMERIC(10,4),
    current_ratio       NUMERIC(10,4),
    goodwill            NUMERIC(20,2),
    ocf                 NUMERIC(20,2),
    ocf_to_np           NUMERIC(10,4),
    roe_ttm             NUMERIC(10,4),
    roa_ttm             NUMERIC(10,4),
    available_date      DATE NOT NULL,       -- 等于 announce_date
    PRIMARY KEY (symbol, report_date)
);
CREATE INDEX idx_fin_available ON financials_quarterly (symbol, available_date);

-- 资金流
CREATE TABLE moneyflow_daily (
    symbol          VARCHAR(20) NOT NULL,
    trade_date      DATE NOT NULL,
    net_lg_amount   NUMERIC(20,2),           -- 大单净流入
    net_md_amount   NUMERIC(20,2),
    net_sm_amount   NUMERIC(20,2),
    available_date  DATE NOT NULL,           -- 一般 = trade_date + 1
    PRIMARY KEY (symbol, trade_date)
);

-- 北向资金（大盘级别）
CREATE TABLE northbound_daily (
    trade_date      DATE NOT NULL PRIMARY KEY,
    total_net_buy   NUMERIC(18,2),
    sh_net_buy      NUMERIC(18,2),
    sz_net_buy      NUMERIC(18,2),
    available_date  DATE NOT NULL
);

-- 融资融券
CREATE TABLE margin_daily (
    symbol          VARCHAR(20) NOT NULL,
    trade_date      DATE NOT NULL,
    rzye            NUMERIC(20,2),
    rzmre           NUMERIC(20,2),
    available_date  DATE NOT NULL,
    PRIMARY KEY (symbol, trade_date)
);

-- 融资余额总量（用于市场情绪）
CREATE TABLE margin_market_daily (
    trade_date      DATE NOT NULL PRIMARY KEY,
    total_rzye      NUMERIC(20,2),           -- 两市融资余额总计
    available_date  DATE NOT NULL
);

-- 指数日线
CREATE TABLE index_daily (
    index_code      VARCHAR(20) NOT NULL,    -- 'HS300' | 'ZZ1000' | 'SPY' | 'QQQ' | 'VIX'
    trade_date      DATE NOT NULL,
    open            NUMERIC(14,4),
    high            NUMERIC(14,4),
    low             NUMERIC(14,4),
    close           NUMERIC(14,4),
    volume          BIGINT,
    available_date  DATE NOT NULL,
    PRIMARY KEY (index_code, trade_date)
);

-- 涨跌停家数（A 股市场情绪）
CREATE TABLE market_breadth_daily (
    trade_date          DATE NOT NULL PRIMARY KEY,
    market              VARCHAR(10) DEFAULT 'CN',
    limit_up_count      INTEGER,
    limit_down_count    INTEGER,
    advancing_count     INTEGER,             -- 上涨家数
    declining_count     INTEGER,             -- 下跌家数
    new_high_count      INTEGER,             -- 创 60 日新高家数
    new_low_count       INTEGER,             -- 创 60 日新低家数
    total_stocks        INTEGER,
    available_date      DATE NOT NULL
);

-- 行业分类（历史快照）
CREATE TABLE industry_classify (
    symbol              VARCHAR(20) NOT NULL,
    snapshot_date       DATE NOT NULL,       -- 该分类的生效日期
    sw_l1               VARCHAR(50),         -- 申万一级行业
    sw_l2               VARCHAR(50),
    industry_framework  VARCHAR(30),         -- 映射到评分框架
    PRIMARY KEY (symbol, snapshot_date)
);

-- 交易日历
CREATE TABLE trade_calendar (
    market          VARCHAR(10) NOT NULL,
    cal_date        DATE NOT NULL,
    is_open         BOOLEAN NOT NULL,
    pretrade_date   DATE,                    -- 上一交易日
    PRIMARY KEY (market, cal_date)
);
```

### 5.2 特征表（预计算）

```sql
-- 日线特征（回测前一次性计算完成）
CREATE TABLE features_daily (
    symbol              VARCHAR(20) NOT NULL,
    trade_date          DATE NOT NULL,
    -- 均线
    ma5                 NUMERIC(14,4),
    ma10                NUMERIC(14,4),
    ma20                NUMERIC(14,4),
    ma60                NUMERIC(14,4),
    ma150               NUMERIC(14,4),
    ma200               NUMERIC(14,4),
    ma20_slope          NUMERIC(10,6),
    ma60_slope          NUMERIC(10,6),
    -- 动量
    rsi_14              NUMERIC(8,4),
    macd_dif            NUMERIC(12,6),
    macd_dea            NUMERIC(12,6),
    macd_hist           NUMERIC(12,6),
    adx_14              NUMERIC(8,4),
    plus_di             NUMERIC(8,4),
    minus_di            NUMERIC(8,4),
    -- 波动率
    atr_14              NUMERIC(14,4),
    hv_20               NUMERIC(10,6),
    boll_upper          NUMERIC(14,4),
    boll_lower          NUMERIC(14,4),
    boll_width          NUMERIC(10,6),
    -- 收益率
    ret_1d              NUMERIC(10,6),
    ret_5d              NUMERIC(10,6),
    ret_20d             NUMERIC(10,6),
    ret_60d             NUMERIC(10,6),
    -- 结构
    dist_20d_high       NUMERIC(10,6),
    dist_60d_high       NUMERIC(10,6),
    pct_in_20d_range    NUMERIC(8,4),
    -- 量能
    vol_ratio_5d        NUMERIC(10,4),
    turnover_rank_20d   NUMERIC(8,4),
    -- Stage 和 RS
    stage               SMALLINT,            -- Weinstein 1/2/3/4
    rs_rank_63d         NUMERIC(8,4),
    -- 未来收益（用于回填验证，禁止用于判断生成）
    future_ret_5d       NUMERIC(10,6),
    future_ret_10d      NUMERIC(10,6),
    future_ret_20d      NUMERIC(10,6),
    future_max_up_20d   NUMERIC(10,6),
    future_max_dd_20d   NUMERIC(10,6),
    PRIMARY KEY (symbol, trade_date)
);
SELECT create_hypertable('features_daily', 'trade_date', if_not_exists => TRUE);
```

### 5.3 回测结果表

```sql
-- 回测运行记录
CREATE TABLE backtest_runs (
    run_id              SERIAL PRIMARY KEY,
    run_timestamp       TIMESTAMPTZ DEFAULT NOW(),
    start_date          DATE NOT NULL,
    end_date            DATE NOT NULL,
    initial_cash_cn     NUMERIC(18,2) DEFAULT 1000000,
    initial_cash_us     NUMERIC(18,2) DEFAULT 100000,
    config_snapshot     JSONB,               -- 回测时的完整配置快照
    status              VARCHAR(20),         -- 'running' | 'completed' | 'failed'
    notes               TEXT
);

-- 每日判断记录
CREATE TABLE backtest_judgments (
    id                  SERIAL PRIMARY KEY,
    run_id              INTEGER REFERENCES backtest_runs(run_id),
    symbol              VARCHAR(20) NOT NULL,
    market              VARCHAR(10) NOT NULL,
    judgment_date       DATE NOT NULL,
    -- 各维度
    technical_score     NUMERIC(6,2),
    fundamental_score   NUMERIC(6,2),
    flow_score          NUMERIC(6,2),
    sentiment_score     NUMERIC(6,2),
    composite_score     NUMERIC(6,2),
    -- Regime
    regime_mode         VARCHAR(30),
    regime_snapshot     JSONB,
    -- 结论
    direction           VARCHAR(10),
    confidence          NUMERIC(4,2),
    -- 建议
    suggested_action    VARCHAR(30),
    entry_price         NUMERIC(14,4),
    stop_loss           NUMERIC(14,4),
    target_price        NUMERIC(14,4),
    suggested_size_pct  NUMERIC(6,4),        -- 建议仓位比例
    -- 回填（自动填充，因为回测时未来数据已知）
    actual_ret_5d       NUMERIC(10,6),
    actual_ret_10d      NUMERIC(10,6),
    actual_ret_20d      NUMERIC(10,6),
    actual_max_up_20d   NUMERIC(10,6),
    actual_max_dd_20d   NUMERIC(10,6),
    is_correct          BOOLEAN,
    signal_sources      JSONB
);
CREATE INDEX idx_bj_run ON backtest_judgments (run_id, judgment_date);
CREATE INDEX idx_bj_symbol ON backtest_judgments (run_id, symbol);

-- 模拟账户交易
CREATE TABLE backtest_trades (
    id                  SERIAL PRIMARY KEY,
    run_id              INTEGER REFERENCES backtest_runs(run_id),
    symbol              VARCHAR(20) NOT NULL,
    market              VARCHAR(10) NOT NULL,
    action              VARCHAR(10) NOT NULL,    -- 'buy' | 'sell'
    trade_date          DATE NOT NULL,
    price               NUMERIC(14,4),           -- 成交价（T+1 开盘）
    shares              INTEGER,
    amount              NUMERIC(18,2),
    commission          NUMERIC(12,2),
    trigger_judgment_id INTEGER,
    trigger_reason      VARCHAR(50),             -- 'new_bullish' | 'stop_loss' | 'target_hit' | 'direction_flip' | 'timeout'
    portfolio_value_after NUMERIC(18,2)
);

-- 每日账户快照
CREATE TABLE backtest_portfolio_daily (
    run_id              INTEGER REFERENCES backtest_runs(run_id),
    trade_date          DATE NOT NULL,
    market              VARCHAR(10) NOT NULL,
    cash                NUMERIC(18,2),
    positions_value     NUMERIC(18,2),
    total_value         NUMERIC(18,2),
    num_positions       INTEGER,
    position_pct        NUMERIC(6,4),            -- 仓位占比
    daily_return        NUMERIC(10,6),
    cumulative_return   NUMERIC(12,6),
    benchmark_return_1  NUMERIC(10,6),           -- 对照组 1: Buy & Hold 指数
    benchmark_return_2  NUMERIC(10,6),           -- 对照组 2: 等权候选池
    benchmark_return_3  NUMERIC(10,6),           -- 对照组 3: 动量策略
    PRIMARY KEY (run_id, trade_date, market)
);

-- 持仓快照
CREATE TABLE backtest_positions (
    run_id              INTEGER REFERENCES backtest_runs(run_id),
    trade_date          DATE NOT NULL,
    symbol              VARCHAR(20) NOT NULL,
    market              VARCHAR(10) NOT NULL,
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

-- Regime 每日快照
CREATE TABLE backtest_regime_daily (
    run_id              INTEGER REFERENCES backtest_runs(run_id),
    trade_date          DATE NOT NULL,
    market              VARCHAR(10) NOT NULL,
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
```

---

## 六、核心模块规格

### 6.1 PIT 数据加载器（核心组件）

```python
# data/pit_loader.py

class PITDataLoader:
    """
    Point-in-Time 数据加载器。
    所有数据查询必须走这个接口，严格保证 look-ahead bias free。
    """

    def __init__(self, db_conn, current_date: date):
        self.db = db_conn
        self._current_date = current_date

    def set_date(self, current_date: date):
        """回测引擎推进时间时调用"""
        self._current_date = current_date

    def get_bars(
        self,
        symbol: str,
        lookback_days: int = 250,
        include_today: bool = False
    ) -> pd.DataFrame:
        """
        获取 symbol 的日线数据。
        默认不包含当日（T 日的决策用 T-1 及之前的数据）。
        """
        cutoff = self._current_date if include_today else self._prev_trade_date()
        query = """
            SELECT * FROM backtest.market_bars_daily
            WHERE symbol = $1
              AND available_date <= $2
              AND trade_date <= $2
            ORDER BY trade_date DESC
            LIMIT $3
        """
        return self.db.fetch_df(query, symbol, cutoff, lookback_days)

    def get_features(
        self,
        symbol: str,
        lookback_days: int = 250
    ) -> pd.DataFrame:
        """获取特征数据（包含 ma/rsi/stage 等预计算字段）"""
        cutoff = self._prev_trade_date()
        query = """
            SELECT * FROM backtest.features_daily
            WHERE symbol = $1 AND trade_date <= $2
            ORDER BY trade_date DESC
            LIMIT $3
        """
        # 注意：features_daily 不返回 future_ret_* 字段（这是用于回填的）
        return self.db.fetch_df(query, symbol, cutoff, lookback_days)

    def get_fundamentals(self, symbol: str) -> Optional[dict]:
        """获取最新可用的基本面指标"""
        query = """
            SELECT * FROM backtest.fundamentals_daily
            WHERE symbol = $1 AND available_date <= $2
            ORDER BY trade_date DESC LIMIT 1
        """
        return self.db.fetch_one(query, symbol, self._current_date)

    def get_latest_financials(self, symbol: str, n_quarters: int = 12) -> pd.DataFrame:
        """
        获取最新可用的 N 个季度财报。
        关键: 使用 announce_date 过滤而非 report_date！
        """
        query = """
            SELECT * FROM backtest.financials_quarterly
            WHERE symbol = $1 AND announce_date <= $2
            ORDER BY report_date DESC
            LIMIT $3
        """
        return self.db.fetch_df(query, symbol, self._current_date, n_quarters)

    def get_moneyflow(self, symbol: str, lookback_days: int = 20) -> pd.DataFrame:
        """资金流数据"""
        cutoff = self._current_date
        query = """
            SELECT * FROM backtest.moneyflow_daily
            WHERE symbol = $1 AND available_date <= $2
            ORDER BY trade_date DESC LIMIT $3
        """
        return self.db.fetch_df(query, symbol, cutoff, lookback_days)

    def get_northbound(self, lookback_days: int = 20) -> pd.DataFrame:
        """北向资金"""
        cutoff = self._current_date
        query = """
            SELECT * FROM backtest.northbound_daily
            WHERE available_date <= $1
            ORDER BY trade_date DESC LIMIT $2
        """
        return self.db.fetch_df(query, cutoff, lookback_days)

    def get_index(self, index_code: str, lookback_days: int = 250) -> pd.DataFrame:
        """指数数据（用于 regime 和对照组）"""
        cutoff = self._prev_trade_date()
        query = """
            SELECT * FROM backtest.index_daily
            WHERE index_code = $1 AND trade_date <= $2
            ORDER BY trade_date DESC LIMIT $3
        """
        return self.db.fetch_df(query, index_code, cutoff, lookback_days)

    def get_market_breadth(self, lookback_days: int = 20) -> pd.DataFrame:
        """市场广度（涨跌停、涨跌家数）"""
        cutoff = self._prev_trade_date()
        query = """
            SELECT * FROM backtest.market_breadth_daily
            WHERE available_date <= $1
            ORDER BY trade_date DESC LIMIT $2
        """
        return self.db.fetch_df(query, cutoff, lookback_days)

    def get_industry(self, symbol: str) -> Optional[str]:
        """获取 symbol 当时的行业分类"""
        query = """
            SELECT industry_framework FROM backtest.industry_classify
            WHERE symbol = $1 AND snapshot_date <= $2
            ORDER BY snapshot_date DESC LIMIT 1
        """
        result = self.db.fetch_one(query, symbol, self._current_date)
        return result['industry_framework'] if result else 'default'

    def get_open_price(self, symbol: str, date: date) -> Optional[float]:
        """
        获取指定日期的开盘价（用于模拟 T+1 成交）。
        注意：这个函数允许访问未来数据，但只能在回测引擎的交易执行环节调用，
        不能在生成判断时调用。
        """
        query = """
            SELECT open FROM backtest.market_bars_daily
            WHERE symbol = $1 AND trade_date = $2
        """
        result = self.db.fetch_one(query, symbol, date)
        return result['open'] if result else None

    def _prev_trade_date(self) -> date:
        """获取当前日期之前的最近一个交易日"""
        query = """
            SELECT pretrade_date FROM backtest.trade_calendar
            WHERE cal_date = $1 AND market = 'CN'
        """
        result = self.db.fetch_one(query, self._current_date)
        return result['pretrade_date'] if result else self._current_date
```

**重要约束：**
- `get_open_price()` 明确标注"允许访问当日/未来数据"，但只能在交易执行时使用
- 所有生成判断的代码只能通过其他方法访问数据
- 在代码 review 时，任何地方直接查 `market_bars_daily` 表都要 flag

### 6.2 技术面分析

完全复用 P10-AlphaRadar 架构文档中 5.2 节的设计：

```python
# core/analysis/technical.py

@dataclass
class TimeframeAnalysis:
    timeframe: str              # 'daily' | 'weekly'
    trend: str                  # 'up' | 'down' | 'sideways'
    stage: int                  # Weinstein 1/2/3/4
    strength: float             # 0-100
    ma_alignment: float         # 0-100
    rs_rank: float              # 0-100
    momentum: str               # 'accelerating' | 'steady' | 'decelerating'

@dataclass
class TechnicalAnalysis:
    symbol: str
    daily: TimeframeAnalysis
    weekly: TimeframeAnalysis
    score: float                # 0-100 综合评分
    combined_direction: str
    confidence_adj: float       # 多周期一致性调整

def analyze_technical(loader: PITDataLoader, symbol: str) -> TechnicalAnalysis:
    # 1. 读取日线和周线数据（周线从日线聚合）
    daily_bars = loader.get_bars(symbol, lookback_days=250)
    daily_features = loader.get_features(symbol, lookback_days=250)
    weekly_bars = resample_to_weekly(daily_bars)

    # 2. 各周期独立分析
    daily_analysis = analyze_timeframe(daily_features, daily_bars, 'daily')
    weekly_analysis = analyze_timeframe_weekly(weekly_bars, 'weekly')

    # 3. 综合评分（权重: 日线 40%, 周线 60%, 周线更重要）
    score = daily_analysis.strength * 0.4 + weekly_analysis.strength * 0.6

    # 4. 方向综合
    combined = combine_timeframes(daily_analysis, weekly_analysis)

    return TechnicalAnalysis(
        symbol=symbol,
        daily=daily_analysis,
        weekly=weekly_analysis,
        score=score,
        combined_direction=combined['direction'],
        confidence_adj=combined['confidence_adj']
    )
```

**评分公式（每个 timeframe 的 strength）：**

```python
def calc_strength(ma_alignment, adx, plus_di, minus_di, price_structure):
    """
    0-100 综合强度评分（方向无关，纯强度）
    """
    # MA 排列完整度（0-40）
    # 检查 MA5 > MA20 > MA60 > MA150 > MA200 的排列数
    ma_score = ma_alignment * 0.4

    # ADX 趋势强度（0-30）
    if adx < 20:
        adx_score = adx  # 弱趋势
    else:
        adx_score = min(30, 15 + (adx - 20) * 0.5)

    # 价格结构（higher highs & higher lows）（0-30）
    struct_score = price_structure * 0.3

    return ma_score + adx_score + struct_score
```

**多周期方向综合：**

```python
def combine_timeframes(daily, weekly):
    """
    日线和周线方向综合：
    - 共振（方向相同）-> 高置信度
    - 矛盾 -> 以周线为主，降低置信度
    """
    if daily.trend == weekly.trend:
        return {'direction': daily.trend, 'confidence_adj': 1.0}

    # 周线上升 + 日线震荡 = 回调中，仍看多
    if weekly.trend == 'up' and daily.trend == 'sideways':
        return {'direction': 'up', 'confidence_adj': 0.7}

    # 周线上升 + 日线下跌 = 可能反转，观望
    if weekly.trend == 'up' and daily.trend == 'down':
        return {'direction': 'neutral', 'confidence_adj': 0.4}

    # 周线下降 + 日线上升 = 反弹，不追
    if weekly.trend == 'down' and daily.trend == 'up':
        return {'direction': 'neutral', 'confidence_adj': 0.4}

    # 周线下降 + 日线震荡 = 下跌中整理
    if weekly.trend == 'down' and daily.trend == 'sideways':
        return {'direction': 'down', 'confidence_adj': 0.7}

    return {'direction': 'neutral', 'confidence_adj': 0.5}
```

### 6.3 Weinstein Stage + RS Rank

```python
# core/analysis/stage_detector.py

def detect_stage(weekly_bars: pd.DataFrame) -> int:
    """
    Weinstein Stage Analysis (weekly):
    Stage 1: 底部蓄力 - MA30W 走平，价格在其附近
    Stage 2: 上升阶段 - MA30W 向上，价格在其上方
    Stage 3: 顶部派发 - MA30W 走平或开始下降，价格在高位
    Stage 4: 下降阶段 - MA30W 向下，价格在其下方
    """
    if len(weekly_bars) < 30:
        return 1  # 数据不足，默认 Stage 1

    ma30w = weekly_bars['close'].rolling(30).mean()
    recent_slope = (ma30w.iloc[-1] - ma30w.iloc[-5]) / ma30w.iloc[-5]
    price_vs_ma = weekly_bars['close'].iloc[-1] / ma30w.iloc[-1] - 1
    ma30w_rising = recent_slope > 0.005
    ma30w_falling = recent_slope < -0.005

    if ma30w_rising and price_vs_ma > 0:
        return 2  # 上升阶段
    if ma30w_falling and price_vs_ma < 0:
        return 4  # 下降阶段
    if not ma30w_rising and not ma30w_falling:
        # MA 走平
        if price_vs_ma > 0.05:
            return 3  # 高位走平 = 派发
        else:
            return 1  # 低位走平 = 蓄力
    # 边缘情况
    if ma30w_rising and price_vs_ma <= 0:
        return 1  # MA 刚开始转好，价格还没完全跟上
    if ma30w_falling and price_vs_ma >= 0:
        return 3  # MA 开始转差，价格还在高位
    return 2

def calc_rs_rank(loader: PITDataLoader, symbol: str, universe: list, period: int = 63) -> float:
    """
    相对强度排名 (O'Neil RS Rating)
    计算个股过去 63 日收益率在 universe 中的百分位排名
    """
    target_ret = _get_period_return(loader, symbol, period)
    if target_ret is None:
        return 50.0  # 默认中性

    all_rets = []
    for sym in universe:
        ret = _get_period_return(loader, sym, period)
        if ret is not None:
            all_rets.append(ret)

    if not all_rets:
        return 50.0

    rank = sum(1 for r in all_rets if r < target_ret) / len(all_rets) * 100
    return rank
```

### 6.4 基本面分析

```python
# core/analysis/fundamental.py

@dataclass
class FundamentalAnalysis:
    symbol: str
    profitability_score: float    # 0-100
    growth_score: float           # 0-100
    valuation_score: float        # 0-100 (越低越便宜)
    health_score: float           # 0-100
    score: float                  # 综合（按行业权重）
    framework_used: str           # 使用的行业框架
    highlights: list              # 亮点
    risks: list                   # 风险

def analyze_fundamental(loader: PITDataLoader, symbol: str) -> FundamentalAnalysis:
    # 1. 拉取最近 12 季度财报（PIT）
    financials = loader.get_latest_financials(symbol, n_quarters=12)
    if financials.empty:
        return _default_fundamental(symbol)

    fundamentals = loader.get_fundamentals(symbol)
    industry_fw = loader.get_industry(symbol)

    # 2. 各维度评分
    profit = calc_profitability_score(financials)
    growth = calc_growth_score(financials)
    valuation = calc_valuation_score(symbol, fundamentals, loader)
    health = calc_health_score(financials)

    # 3. 按行业框架加权
    weights = load_industry_weights(industry_fw)
    score = (
        profit * weights['profitability'] +
        growth * weights['growth'] +
        valuation * weights['valuation'] +
        health * weights['health']
    )

    return FundamentalAnalysis(
        symbol=symbol,
        profitability_score=profit,
        growth_score=growth,
        valuation_score=valuation,
        health_score=health,
        score=score,
        framework_used=industry_fw,
        highlights=_extract_highlights(financials, fundamentals),
        risks=_extract_risks(financials, fundamentals)
    )

def calc_profitability_score(fin: pd.DataFrame) -> float:
    """
    盈利质量 0-100:
    - ROE_TTM 水平和稳定性 (50%)
    - 毛利率稳定性 (25%)
    - 经营性现金流/净利润 (25%)
    """
    roe_latest = fin['roe_ttm'].iloc[0]
    roe_stability = 100 - min(100, fin['roe_ttm'].head(8).std() * 10)
    roe_level = min(100, roe_latest * 4)  # ROE 25% = 100 分
    roe_score = (roe_level + roe_stability) / 2

    gross_stability = 100 - min(100, fin['gross_margin'].head(8).std() * 10)

    ocf_ratio = fin['ocf_to_np'].iloc[0] if pd.notna(fin['ocf_to_np'].iloc[0]) else 0.5
    ocf_score = min(100, ocf_ratio * 80)  # OCF/NP = 1.25 → 100 分

    return roe_score * 0.5 + gross_stability * 0.25 + ocf_score * 0.25

def calc_growth_score(fin: pd.DataFrame) -> float:
    """
    成长性 0-100:
    - 营收 YoY 增速 (40%)
    - 净利润 YoY 增速 (40%)
    - 增速趋势（最近 4 季度 vs 之前 4 季度）(20%)
    """
    rev_yoy = fin['revenue_yoy'].iloc[0] if pd.notna(fin['revenue_yoy'].iloc[0]) else 0
    np_yoy = fin['np_yoy'].iloc[0] if pd.notna(fin['np_yoy'].iloc[0]) else 0

    rev_score = _sigmoid_score(rev_yoy, center=15, scale=10)  # 15% 增速 = 50 分
    np_score = _sigmoid_score(np_yoy, center=20, scale=15)    # 20% 增速 = 50 分

    # 趋势
    recent_rev = fin['revenue_yoy'].head(4).mean()
    prev_rev = fin['revenue_yoy'].iloc[4:8].mean() if len(fin) >= 8 else recent_rev
    trend_score = 50 + min(50, max(-50, (recent_rev - prev_rev) * 3))

    return rev_score * 0.4 + np_score * 0.4 + trend_score * 0.2

def calc_valuation_score(symbol, fund: dict, loader: PITDataLoader) -> float:
    """
    估值 0-100 (越低 = 估值越便宜 = 分数越高):
    - PE_TTM 行业内分位 (50%)
    - PE_TTM 自身历史 3 年分位 (30%)
    - PB 行业内分位 (20%)
    """
    pe = fund['pe_ttm']
    if pe is None or pe <= 0:
        return 50  # 亏损股，默认中性

    # 自身历史分位
    pe_history = _get_pe_history(loader, symbol, years=3)
    pe_history_pct = _percentile_rank(pe_history, pe)
    # 越低越便宜，所以反转: 0 分位 = 100 分
    pe_history_score = 100 - pe_history_pct

    # 行业分位（需要读取同行业其他股票的 PE）
    pe_industry_pct = _get_industry_pe_percentile(loader, symbol, pe)
    pe_industry_score = 100 - pe_industry_pct

    # PB
    pb_industry_score = 100 - _get_industry_pb_percentile(loader, symbol, fund['pb'])

    return pe_industry_score * 0.5 + pe_history_score * 0.3 + pb_industry_score * 0.2

def calc_health_score(fin: pd.DataFrame) -> float:
    """
    财务健康度 0-100:
    - 资产负债率 (40%)，低为好
    - 流动比率 (30%)，高为好
    - 商誉/净资产 (30%)，低为好
    """
    debt_ratio = fin['debt_ratio'].iloc[0]
    debt_score = max(0, 100 - debt_ratio * 1.5)  # 66% 负债率 = 0 分

    cr = fin['current_ratio'].iloc[0]
    cr_score = min(100, cr * 40)  # 流动比率 2.5 = 100 分

    gw_ratio = fin['goodwill'].iloc[0] / max(fin['total_assets'].iloc[0] - fin['total_liab'].iloc[0], 1)
    gw_score = max(0, 100 - gw_ratio * 200)  # 商誉/净资产 50% = 0 分

    return debt_score * 0.4 + cr_score * 0.3 + gw_score * 0.3
```

### 6.5 资金面分析

```python
# core/analysis/flow.py

@dataclass
class FlowAnalysis:
    symbol: str
    main_flow_score: float        # 主力资金
    northbound_score: float       # 北向资金
    margin_score: float           # 融资余额
    score: float                  # 综合
    notes: list

def analyze_flow(loader: PITDataLoader, symbol: str, market: str) -> FlowAnalysis:
    if market != 'CN':
        # 美股暂时用成交量趋势代理
        return _us_flow_proxy(loader, symbol)

    # 主力资金（大单净流入）
    mf = loader.get_moneyflow(symbol, lookback_days=5)
    main_score = _score_main_flow(mf)

    # 北向（大盘级别）
    nb = loader.get_northbound(lookback_days=20)
    nb_score = _score_northbound(nb)

    # 融资融券
    margin = _get_margin_trend(loader, symbol, lookback_days=5)
    margin_score = _score_margin(margin)

    score = main_score * 0.4 + nb_score * 0.3 + margin_score * 0.3

    return FlowAnalysis(
        symbol=symbol,
        main_flow_score=main_score,
        northbound_score=nb_score,
        margin_score=margin_score,
        score=score,
        notes=[]
    )
```

### 6.6 市场级情绪（简化版）

```python
# core/analysis/sentiment_market.py

def analyze_market_sentiment(loader: PITDataLoader, market: str) -> float:
    """
    仅市场级情绪（不含个股社交数据）
    A 股: 涨跌比 + 融资余额变化 + 涨停跌停比 + Fear & Greed
    美股: VIX + VIX 期限结构（如有）
    返回 0-100，越高越乐观
    """
    if market == 'CN':
        breadth = loader.get_market_breadth(lookback_days=5)
        margin_trend = _get_margin_market_trend(loader, days=5)

        ad_ratio = (breadth['advancing_count'] / breadth['declining_count']).mean()
        ad_score = _sigmoid_score(ad_ratio, center=1.0, scale=0.3) * 100

        limit_ratio = (breadth['limit_up_count'] / (breadth['limit_down_count'] + 1)).mean()
        limit_score = _sigmoid_score(limit_ratio, center=1.5, scale=1.0) * 100

        margin_score = _sigmoid_score(margin_trend, center=0, scale=0.005) * 100

        return ad_score * 0.4 + limit_score * 0.3 + margin_score * 0.3

    elif market == 'US':
        vix = loader.get_index('VIX', lookback_days=5)
        vix_latest = vix['close'].iloc[0]
        # VIX 映射: 12 → 乐观 80, 20 → 中性 50, 30 → 恐慌 20
        if vix_latest < 12:
            return 85
        elif vix_latest < 15:
            return 70
        elif vix_latest < 20:
            return 55
        elif vix_latest < 25:
            return 40
        elif vix_latest < 30:
            return 25
        else:
            return 15

    return 50
```

### 6.7 Regime 检测

完全按照 P10-AlphaRadar 架构文档 5.1 节实现。核心四维度 → 2×2 矩阵 → regime_mode + 参数集。

关键是 Regime 的所有输入数据必须通过 PITDataLoader 获取。

```python
# core/regime/detector.py

def detect_regime(loader: PITDataLoader, market: str) -> RegimeState:
    trend = calc_trend_score(loader, market)
    vol = calc_volatility_score(loader, market)
    breadth = calc_breadth_score(loader, market)
    liquidity = calc_liquidity_score(loader, market)

    trend_dir = 'up' if trend > 55 else ('down' if trend < 40 else 'sideways')
    vol_env = 'high' if vol > 60 else 'low'

    mode = REGIME_MAP[(trend_dir, vol_env)]

    # 修正
    if breadth < 30 and mode == 'offense':
        mode = 'cautious_offense'
    if liquidity > 70 and mode == 'defense':
        mode = 'cautious_offense'

    params = load_regime_params(mode)
    return RegimeState(
        market=market,
        trend_score=trend,
        volatility_score=vol,
        breadth_score=breadth,
        liquidity_score=liquidity,
        mode=mode,
        trend_direction=trend_dir,
        volatility_env=vol_env,
        params=params
    )
```

### 6.8 综合判断

```python
# core/analysis/composite.py

def generate_judgment(
    loader: PITDataLoader,
    symbol: str,
    market: str,
    universe: list
) -> Judgment:
    # 1. Regime
    regime = detect_regime(loader, market)

    # 2. 四维度分析
    tech = analyze_technical(loader, symbol, universe)
    fund = analyze_fundamental(loader, symbol)
    flow = analyze_flow(loader, symbol, market)
    sent = analyze_market_sentiment(loader, market)  # 注意：市场级，不依赖 symbol

    # 3. 按 regime 权重加权
    weights = regime.params['dimension_weights']
    composite = (
        tech.score * weights['technical'] +
        fund.score * weights['fundamental'] +
        flow.score * weights['flow'] +
        sent * weights['sentiment']
    )

    # 4. 方向 + 置信度
    if composite > 65:
        direction = 'bullish'
    elif composite < 40:
        direction = 'bearish'
    else:
        direction = 'neutral'

    base_conf = abs(composite - 50) / 50  # 50 → 0, 100/0 → 1
    confidence = base_conf * tech.confidence_adj * regime.params.get('confidence_factor', 1.0)

    # 5. 交易建议
    suggestion = generate_trade_suggestion(loader, symbol, direction, confidence, regime)

    return Judgment(
        symbol=symbol,
        market=market,
        judgment_date=loader._current_date,
        technical_score=tech.score,
        fundamental_score=fund.score,
        flow_score=flow.score,
        sentiment_score=sent,
        composite_score=composite,
        direction=direction,
        confidence=confidence,
        regime_mode=regime.mode,
        regime_snapshot=regime.to_dict(),
        **suggestion
    )

def generate_trade_suggestion(loader, symbol, direction, confidence, regime):
    """生成入场区间、止损、目标"""
    bars = loader.get_bars(symbol, lookback_days=20)
    current_price = bars['close'].iloc[0]
    atr = _calc_atr(bars, period=14)

    if direction == 'bullish' and confidence > 0.5:
        # 买入建议
        entry_low = current_price * 0.99
        entry_high = current_price * 1.01
        stop_loss = current_price - 2.0 * atr  # 2 ATR 止损
        target_price = current_price + 4.0 * atr  # 2:1 盈亏比
        action = 'buy'
    elif direction == 'bearish' and confidence > 0.5:
        action = 'sell'
        entry_low = entry_high = stop_loss = target_price = None
    else:
        action = 'hold'
        entry_low = entry_high = stop_loss = target_price = None

    # 仓位建议
    max_pos = regime.params['max_position_pct']
    size_pct = min(max_pos, confidence * max_pos)

    return {
        'suggested_action': action,
        'entry_price': current_price,
        'entry_zone_low': entry_low,
        'entry_zone_high': entry_high,
        'stop_loss': stop_loss,
        'target_price': target_price,
        'suggested_size_pct': size_pct
    }
```

---

## 七、回测引擎

### 7.1 主引擎

```python
# backtest/engine.py

class BacktestEngine:
    def __init__(self, config: BacktestConfig):
        self.config = config
        self.db = connect_db()
        self.loader = PITDataLoader(self.db, current_date=config.start_date)
        self.portfolio_cn = Portfolio(initial_cash=config.initial_cash_cn, market='CN')
        self.portfolio_us = Portfolio(initial_cash=config.initial_cash_us, market='US')
        self.benchmarks = Benchmarks(config)
        self.run_id = None

    def run(self):
        # 1. 创建 run 记录
        self.run_id = self._create_run()

        # 2. 获取交易日列表
        trade_days = self._get_trade_days()

        # 3. 按日循环
        for current_date in trade_days:
            try:
                self._process_day(current_date)
            except Exception as e:
                logger.error(f"Error on {current_date}: {e}")
                raise

            if current_date.day == 1:  # 每月进度汇报
                logger.info(f"Progress: {current_date}, portfolio value: CN={self.portfolio_cn.value}")

        # 4. 完成
        self._finalize_run()
        logger.info(f"Backtest completed. Run ID: {self.run_id}")
        return self.run_id

    def _process_day(self, current_date: date):
        """处理单个交易日"""
        self.loader.set_date(current_date)

        # 1. 更新 regime（每日）
        regime_cn = detect_regime(self.loader, 'CN')
        regime_us = detect_regime(self.loader, 'US')
        self._save_regime(regime_cn, regime_us, current_date)

        # 2. 对每个市场的 universe 生成判断
        for market, portfolio in [('CN', self.portfolio_cn), ('US', self.portfolio_us)]:
            universe = self._get_universe(market)
            regime = regime_cn if market == 'CN' else regime_us

            judgments = []
            for symbol in universe:
                try:
                    j = generate_judgment(self.loader, symbol, market, universe)
                    judgments.append(j)
                    self._save_judgment(j)
                except DataInsufficientError:
                    continue  # 数据不足，跳过

            # 3. 执行交易规则（T+1 开盘成交）
            next_trade_date = self._get_next_trade_date(current_date, market)
            if next_trade_date:
                self._execute_trades(portfolio, judgments, next_trade_date, regime)

            # 4. 记录账户状态
            self._snapshot_portfolio(portfolio, current_date)

        # 5. 更新对照组
        self.benchmarks.update(current_date, self.loader)

    def _execute_trades(
        self,
        portfolio: Portfolio,
        judgments: list,
        exec_date: date,
        regime: RegimeState
    ):
        """
        按 T+1 开盘价执行交易
        优先级:
        1. 检查现有持仓的平仓信号（止损、目标、方向翻转、超时）
        2. 检查新建仓机会
        """
        # 1. 平仓检查
        for position in portfolio.positions[:]:
            exit_reason = self._check_exit(position, judgments, exec_date)
            if exit_reason:
                exec_price = self.loader.get_open_price(position.symbol, exec_date)
                if exec_price:
                    portfolio.close_position(position, exec_price, exec_date, exit_reason)

        # 2. 建仓检查
        bullish_judgments = [j for j in judgments if j.direction == 'bullish' and j.confidence > 0.55]
        bullish_judgments.sort(key=lambda j: j.composite_score, reverse=True)

        for j in bullish_judgments:
            if portfolio.has_position(j.symbol):
                continue  # 已持有，不重复建仓

            # 检查仓位限制
            if portfolio.position_pct >= regime.params['max_position_pct']:
                break

            # 检查行业集中度
            if self._industry_over_concentrated(portfolio, j.symbol):
                continue

            # 检查流动性
            if not self._has_enough_liquidity(j.symbol, exec_date):
                continue

            # 计算仓位大小
            exec_price = self.loader.get_open_price(j.symbol, exec_date)
            if not exec_price:
                continue

            shares = self._calc_position_size(
                portfolio, j, exec_price, regime.params['max_position_pct']
            )
            if shares > 0:
                portfolio.open_position(j, exec_price, shares, exec_date)
```

### 7.2 模拟账户

```python
# backtest/portfolio.py

class Portfolio:
    def __init__(self, initial_cash: float, market: str):
        self.market = market
        self.cash = initial_cash
        self.initial_cash = initial_cash
        self.positions: list[Position] = []
        self.closed_trades: list[Trade] = []
        self.daily_values = []

    def open_position(self, judgment, price, shares, date):
        # 计算佣金
        amount = price * shares
        commission = amount * (0.002 if self.market == 'CN' else 0.001)  # 双边 0.2% / 0.1%
        total_cost = amount + commission

        if total_cost > self.cash:
            shares = int((self.cash / (1 + 0.002)) / price / 100) * 100  # A 股整百
            if shares <= 0:
                return
            amount = price * shares
            commission = amount * 0.002
            total_cost = amount + commission

        self.cash -= total_cost

        position = Position(
            symbol=judgment.symbol,
            entry_date=date,
            entry_price=price,
            shares=shares,
            stop_loss=judgment.stop_loss,
            target_price=judgment.target_price,
            trigger_judgment_id=judgment.id,
            market=self.market
        )
        self.positions.append(position)

    def close_position(self, position, price, date, reason):
        amount = price * position.shares
        commission = amount * (0.002 if self.market == 'CN' else 0.001)
        self.cash += amount - commission

        pnl = (price - position.entry_price) * position.shares - commission
        pnl_pct = pnl / (position.entry_price * position.shares)

        trade = Trade(
            symbol=position.symbol,
            entry_date=position.entry_date,
            entry_price=position.entry_price,
            exit_date=date,
            exit_price=price,
            shares=position.shares,
            pnl=pnl,
            pnl_pct=pnl_pct,
            exit_reason=reason,
            days_held=(date - position.entry_date).days
        )
        self.closed_trades.append(trade)
        self.positions.remove(position)

    @property
    def value(self) -> float:
        # 需要引用当前价格（由回测引擎在每日末调用 update_value）
        return self.cash + sum(p.market_value for p in self.positions)

    def update_positions_value(self, price_map: dict):
        """每日末更新持仓市值"""
        for p in self.positions:
            p.current_price = price_map.get(p.symbol, p.current_price)
            p.market_value = p.current_price * p.shares
            p.unrealized_pnl_pct = (p.current_price - p.entry_price) / p.entry_price
```

### 7.3 建仓/平仓规则

```python
# backtest/rules.py

def check_exit(position: Position, judgments: list, current_date: date) -> Optional[str]:
    """
    检查是否应该平仓。返回平仓原因或 None。
    """
    # 1. 止损
    if position.current_price <= position.stop_loss:
        return 'stop_loss'

    # 2. 达到目标
    if position.current_price >= position.target_price:
        return 'target_hit'

    # 3. 方向翻转
    latest_j = next((j for j in judgments if j.symbol == position.symbol), None)
    if latest_j and latest_j.direction == 'bearish' and latest_j.confidence > 0.5:
        return 'direction_flip'

    # 4. 超时（持有 > 30 天且仍为 neutral/bearish）
    days_held = (current_date - position.entry_date).days
    if days_held > 30:
        if not latest_j or latest_j.direction != 'bullish':
            return 'timeout'

    return None

def calc_position_size(
    portfolio: Portfolio,
    judgment: Judgment,
    exec_price: float,
    max_position_pct: float
) -> int:
    """
    基于止损反算仓位：单笔最大亏损 2% 总资产
    """
    risk_per_share = exec_price - judgment.stop_loss
    if risk_per_share <= 0:
        return 0

    max_risk_amount = portfolio.value * 0.02  # 单笔最大亏损 2%
    shares_by_risk = max_risk_amount / risk_per_share

    # 不超过最大仓位限制
    max_position_amount = portfolio.value * max_position_pct * judgment.confidence
    shares_by_position = max_position_amount / exec_price

    shares = min(shares_by_risk, shares_by_position)

    # A 股整百，美股整股
    if portfolio.market == 'CN':
        shares = int(shares / 100) * 100
    else:
        shares = int(shares)

    return shares
```

### 7.4 对照组

```python
# backtest/benchmarks.py

class Benchmarks:
    def __init__(self, config):
        self.config = config
        self.bh_values = {'CN': [], 'US': []}          # 对照组 1: Buy & Hold 指数
        self.eq_values = {'CN': [], 'US': []}          # 对照组 2: 等权候选池
        self.mom_values = {'CN': [], 'US': []}         # 对照组 3: 动量策略

    def initialize(self, loader: PITDataLoader):
        """在回测开始时初始化三个组合"""
        # 对照组 1: 买入指数
        self.bh_positions = {
            'CN': self._buy_index('HS300', self.config.initial_cash_cn, loader),
            'US': self._buy_index('SPY', self.config.initial_cash_us, loader)
        }

        # 对照组 2: 等权买入 universe
        self.eq_positions = {
            'CN': self._buy_equal_weight('CN', self.config.initial_cash_cn, loader),
            'US': self._buy_equal_weight('US', self.config.initial_cash_us, loader)
        }

        # 对照组 3: 动量策略，每周调仓
        self.mom_cash = {'CN': self.config.initial_cash_cn, 'US': self.config.initial_cash_us}
        self.mom_positions = {'CN': [], 'US': []}

    def update(self, current_date: date, loader: PITDataLoader):
        """每日更新对照组市值"""
        for market in ['CN', 'US']:
            # 1. Buy & Hold: 只需更新市值
            self._update_bh(market, current_date, loader)

            # 2. 等权: 同上
            self._update_eq(market, current_date, loader)

            # 3. 动量: 每周一调仓
            if self._is_rebalance_day(current_date):
                self._rebalance_momentum(market, current_date, loader)
            self._update_mom(market, current_date, loader)

    def _rebalance_momentum(self, market, date, loader):
        """动量策略: 选过去 20 日涨幅最大的 5 只票等权持有"""
        universe = self._get_universe(market)
        returns = []
        for symbol in universe:
            bars = loader.get_bars(symbol, lookback_days=20)
            if len(bars) >= 20:
                ret = (bars['close'].iloc[0] / bars['close'].iloc[-1]) - 1
                returns.append((symbol, ret))

        returns.sort(key=lambda x: x[1], reverse=True)
        top5 = [s for s, _ in returns[:5]]

        # 清空现有持仓（用当前价格）
        total_value = self.mom_cash[market] + sum(
            p['shares'] * self._get_price(p['symbol'], date, loader)
            for p in self.mom_positions[market]
        )

        # 等权建仓
        per_position = total_value / 5 * 0.998  # 留 0.2% 作为交易成本
        new_positions = []
        for symbol in top5:
            price = self._get_price(symbol, date, loader)
            if price:
                shares = per_position / price
                if market == 'CN':
                    shares = int(shares / 100) * 100
                else:
                    shares = int(shares)
                new_positions.append({'symbol': symbol, 'shares': shares, 'entry_price': price})

        self.mom_positions[market] = new_positions
        invested = sum(p['shares'] * p['entry_price'] for p in new_positions)
        self.mom_cash[market] = total_value - invested
```

---

## 八、评估与报告

### 8.1 评估指标

```python
# evaluation/metrics.py

@dataclass
class BacktestMetrics:
    # 收益
    total_return: float
    annualized_return: float
    daily_returns: pd.Series

    # 风险
    annualized_volatility: float
    max_drawdown: float
    max_drawdown_duration_days: int

    # 风险调整后
    sharpe_ratio: float
    sortino_ratio: float
    calmar_ratio: float

    # 交易统计
    total_trades: int
    win_rate: float
    profit_factor: float
    avg_win_pct: float
    avg_loss_pct: float
    avg_holding_days: float

    # vs 基准
    alpha_vs_bh: float
    alpha_vs_eq: float
    alpha_vs_mom: float
    beta_vs_bh: float

    # IC 分析
    ic_composite: float
    ic_technical: float
    ic_fundamental: float
    ic_flow: float
    ic_sentiment: float

    # 按 regime 分层
    returns_by_regime: dict  # {'offense': 0.12, 'defense': -0.03, ...}

    # 按月分层
    monthly_returns: pd.Series

def calc_metrics(run_id: int, db) -> BacktestMetrics:
    daily = db.query(f"""
        SELECT trade_date, total_value, daily_return, benchmark_return_1, benchmark_return_2, benchmark_return_3
        FROM backtest.backtest_portfolio_daily
        WHERE run_id = {run_id} AND market = 'CN'
        ORDER BY trade_date
    """)
    # ... 计算所有指标
    return metrics
```

### 8.2 IC 分析

```python
def calc_ic(run_id: int, db, dimension: str, lookback_days: int = 20) -> float:
    """
    计算某个维度评分与未来 N 日收益的 Rank IC (Spearman)

    IC > 0.05 表示该维度有明显预测力
    IC > 0.10 表示预测力强
    IC < 0.02 表示基本无效
    """
    data = db.query(f"""
        SELECT
            {dimension}_score AS score,
            actual_ret_{lookback_days}d AS future_ret
        FROM backtest.backtest_judgments
        WHERE run_id = {run_id}
          AND actual_ret_{lookback_days}d IS NOT NULL
    """)
    return spearmanr(data['score'], data['future_ret']).correlation
```

### 8.3 报告生成

**8.3.1 Markdown 摘要报告**

```markdown
# P10-Backtest 报告 - Run #123

**回测周期**: 2025-09-01 ~ 2026-04-17 (约 7.5 个月)
**初始资金**: A 股 100 万 RMB + 美股 10 万 USD

## 核心指标

### A 股账户
| 指标 | P10 系统 | 沪深300 B&H | 等权候选池 | 动量策略 |
|------|---------|------------|-----------|---------|
| 累计收益 | +8.3% | +3.1% | +5.2% | +6.8% |
| 年化收益 | +13.5% | +5.0% | +8.4% | +11.0% |
| 年化波动 | 18.2% | 16.5% | 19.0% | 22.1% |
| 最大回撤 | -9.5% | -13.2% | -14.1% | -18.5% |
| 夏普比率 | 0.74 | 0.30 | 0.44 | 0.50 |
| Alpha vs 沪深300 | +8.5% | - | - | - |

### 判断准确率
- 总判断数: 1,240
- 短期判断准确率 (T+10): 57.3%
- 中期判断准确率 (T+20): 54.8%

### 交易统计
- 总交易: 42 笔
- 胜率: 57.1%
- 盈亏比: 1.8
- 平均持仓: 12 天

## 各维度 IC 分析

| 维度 | IC (T+10) | IC (T+20) | IR | 评估 |
|------|-----------|-----------|-----|------|
| 综合评分 | 0.048 | 0.051 | 0.62 | 有效 |
| 技术面 | 0.042 | 0.038 | 0.55 | 弱有效 |
| 基本面 | 0.031 | 0.055 | 0.48 | 中期更强 |
| 资金面 | 0.028 | 0.021 | 0.35 | 短期偏弱 |
| 市场情绪 | 0.012 | 0.009 | 0.18 | 基本无效 |

## 按 Regime 分层表现

| Regime | 天数占比 | 系统累计收益 | 沪深300 同期收益 | Alpha |
|--------|---------|-------------|-----------------|-------|
| offense | 25% | +5.2% | +4.1% | +1.1% |
| cautious_offense | 30% | +3.5% | +1.2% | +2.3% |
| defense | 32% | +0.8% | -1.5% | +2.3% |
| risk_off | 13% | -1.2% | -0.7% | -0.5% |

## 典型案例分析

### 成功案例 1: 600519.SH (贵州茅台)
- 2025-11-15 给出 bullish 判断 (综合分 72)
- 逻辑: 周线 Stage 2 + 基本面 ROE 稳定 + 北向连续买入
- T+10 实际收益: +4.2%
- T+20: +6.8%

### 失败案例 1: 000858.SZ (五粮液)
- 2026-02-10 给出 bullish 判断 (综合分 68)
- 未预料到政策风险事件
- T+10 实际收益: -5.3%
- 归因: external_event

## 结论

1. **有效性初步验证**: 综合评分 IC = 0.048，达到"弱有效"标准。系统有 alpha 但不夸张，符合合理预期。

2. **强项**: Regime 自适应机制有效——在 cautious_offense 和 defense 环境下跑赢基准最多。

3. **弱项**:
   - 情绪维度几乎无效 (IC 0.012)，建议降权或改用其他代理指标
   - 在 risk_off 环境下跑输基准，说明极端下跌时系统反应不够快

4. **下一步建议**:
   - 情绪维度权重从当前 15% 降至 5%
   - 增加"急跌止损"规则，在 risk_off 触发时强制降仓
   - 可以启动完整 P10 开发
```

**8.3.2 Excel 详细报告**

包含多个 sheet：
- `Summary`: 核心指标汇总
- `Daily Returns`: 每日收益明细
- `All Judgments`: 所有判断记录
- `All Trades`: 所有交易记录
- `Regime Timeline`: regime 变化时间线
- `IC by Dimension`: 各维度 IC 详细分析
- `Monthly Breakdown`: 按月分解

**8.3.3 图表（matplotlib/plotly）**

- 累计净值曲线（系统 vs 三个对照组）
- 回撤曲线
- 月度收益热力图
- 各维度评分分布
- Regime 时间线 + 对应收益
- 买卖点标注在候选池主要个股的 K 线上

---

## 九、开发路线图

### Week 1: 基础设施 + 数据准备

**Day 1-2: 环境搭建**
- PostgreSQL + TimescaleDB Docker 配置
- 项目结构初始化
- schema.sql 建表
- 配置文件模板

**Day 3-5: 数据拉取**
- TushareFetcher（A 股日线、财报、资金流、基本面）
- AkShareFetcher（北向、融资融券、涨跌停、指数）
- YFinanceFetcher（美股日线、基本面、VIX）
- 严格 PIT 处理（announce_date 准确记录）

**Day 6-7: 特征预计算**
- features_daily 表一次性计算所有技术指标
- future_ret_* 字段（用于回填验证）

### Week 2: 核心分析模块

**Day 1-2: Regime**
- 四维度计算
- Regime 映射和参数加载

**Day 3-4: 技术面 + Stage**
- 多周期趋势
- Weinstein Stage
- RS Rank
- TechnicalAnalysis 完整流程

**Day 5-6: 基本面**
- 行业映射
- 四个子维度评分
- industry_frameworks.yaml

**Day 7: 资金面 + 情绪**
- 主力 + 北向 + 融资
- 市场级情绪（A 股 + 美股）

### Week 3: 回测引擎

**Day 1-2: 综合判断**
- composite.py 整合四维度
- 交易建议生成
- generate_judgment 主函数

**Day 3-4: 模拟账户**
- Portfolio 类
- 建仓/平仓规则
- 交易成本模拟

**Day 5-6: 回测引擎主循环**
- BacktestEngine
- PIT 数据访问控制
- 结果持久化

**Day 7: 对照组**
- Buy & Hold
- 等权候选池
- 动量策略

### Week 4: 评估与报告

**Day 1-2: 指标计算**
- 全套指标（Sharpe, Sortino, Calmar, MDD, IC, IR）
- 按 regime / 按月分层

**Day 3-4: 报告生成**
- Markdown 摘要
- Excel 详细
- 图表可视化

**Day 5: 首次完整运行**
- 跑完 7.5 个月数据
- 生成报告
- 诊断明显问题

**Day 6-7: 迭代与验证**
- 检查 look-ahead bias
- 修复发现的问题
- 生成最终报告

---

## 十、做法 A 框架设计（完整重跑）

做法 A 是在 P10-AlphaRadar 完整系统基础上，增加"时光机"能力，支持在历史数据上重跑。

### 10.1 改造点

**改造 1: 所有数据访问走 `VirtualDateContext`**

```python
# core/virtual_date.py

class VirtualDateContext:
    """
    全局的虚拟日期上下文管理器。
    系统所有数据访问都要通过这个上下文，自动加 PIT 过滤。
    """
    _current_date: Optional[date] = None

    @classmethod
    def set(cls, d: date):
        cls._current_date = d

    @classmethod
    def clear(cls):
        cls._current_date = None

    @classmethod
    def is_backtest(cls) -> bool:
        return cls._current_date is not None

    @classmethod
    def get_cutoff(cls) -> date:
        return cls._current_date or date.today()
```

所有数据查询函数检查 `VirtualDateContext.is_backtest()`，如果是就加 `available_date <= cutoff` 过滤。

**改造 2: LLM 调用模式切换**

- 生产模式: 正常调用 LLM API
- 回测模式: 可选跳过 LLM（加速）或用 LLM 缓存（基于 prompt hash）

```python
class LLMClient:
    def __init__(self, cache_mode='normal'):
        self.cache_mode = cache_mode  # 'normal' | 'backtest_skip' | 'backtest_cache'

    async def chat(self, messages, **kwargs):
        if VirtualDateContext.is_backtest():
            if self.cache_mode == 'backtest_skip':
                return self._placeholder_response(messages)
            elif self.cache_mode == 'backtest_cache':
                cached = self._try_cache(messages)
                if cached:
                    return cached
        return await self._real_call(messages, **kwargs)
```

**改造 3: Wiki 在回测模式下只读**

回测期间 Wiki 不更新（因为回测是用现在的经验去测试过去，允许 Wiki 更新会引入 look-ahead）。

**改造 4: Telegram / 调度器不启动**

回测引擎直接调用业务逻辑，不经过 Telegram 和调度器。

**改造 5: 交易执行模拟化**

`TradeExecutor` 有两种实现：
- 生产模式: 通知用户 + 记录信号
- 回测模式: 模拟成交 + 更新虚拟账户

### 10.2 做法 A 的触发方式

```python
# 生产模式（不变）
async def daily_analysis():
    for symbol in universe:
        await analyze(symbol)

# 回测模式
async def run_backtest(start_date, end_date):
    engine = BacktestEngine(start_date, end_date)
    for current_date in trade_days:
        with VirtualDateContext(current_date):
            for symbol in universe:
                judgment = await analyze(symbol)  # 同一个 analyze 函数
                engine.record(judgment)
            engine.execute_day(current_date)
    return engine.report()
```

**好处：** 业务逻辑完全共享，回测即是"在虚拟时间里跑生产代码"。

### 10.3 做法 A 的实施时机

- P10 Phase 1 完成后 → 改造 1（最重要）
- P10 Phase 2 完成后 → 改造 2（LLM 缓存）
- P10 Phase 5 完成后 → 改造 3, 4, 5（完整回测能力）

预计做法 A 的改造工作量约 2-3 周，可以在 P10 Phase 6 之后作为独立项目做。

### 10.4 做法 A vs 做法 B 对比

| 维度 | 做法 B（本项目） | 做法 A（完整重跑） |
|------|----------------|-------------------|
| 工程量 | 4 周 | 2-3 周（基于 P10） |
| 何时可做 | 现在 | P10 基本完成后 |
| 回测范围 | 简化版核心逻辑 | 完整 P10 |
| LLM 评估 | 无 | 可评估（需缓存） |
| 代码复用 | 独立项目 | 与生产完全共享 |
| 验证目的 | 快速筛选系统是否有效 | 精确验证完整系统 |

**推荐策略：**
1. 现在做法 B，4 周拿到初步结论
2. 如果结论正面 → 继续完整 P10 开发
3. P10 完成后做改造 1-5，得到完整的回测能力
4. 每次 P10 重大更新都用做法 A 重跑历史数据，验证更新是否真的带来改进

---

## 十一、交付清单

发给 Claude Code 的内容：

1. 本文档 `P10-Backtest-Spec.md`
2. CLAUDE.md（项目指令）
3. `.env.template`（数据源密钥）
4. `watchlist.yaml`（回测候选池，你填入 30-50 只 A 股 + 10-20 只美股）
5. 初始的 `industry_frameworks.yaml`（行业评分框架）

Claude Code 需要自己创建的内容：
- 所有代码文件
- Docker 配置
- 数据库 schema
- 配置文件
- 报告模板

每周验收标准：
- **Week 1 验收**: 数据库初始化完成，历史数据全部拉取完毕（A 股 30 只、美股 20 只、全部辅助数据），features_daily 计算完毕
- **Week 2 验收**: 四个分析模块 + Regime 都能独立运行，单只票分析输出合理结果
- **Week 3 验收**: 回测引擎能从 2025-09-01 跑到 2026-04-17，生成判断和交易记录
- **Week 4 验收**: 完整报告生成（Markdown + Excel + 图表），核心指标计算正确
