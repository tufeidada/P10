# P10-AlphaRadar · Phase 1 改进方案

> **版本**：2026-04-20
> **目的**：把 P10 主项目从"脚手架搭起、主循环未启动"推进到"每天能真实产出 watchlist 股票分析"的状态。
> **受众**：Claude Code（编码执行方）+ 轩老板（需求和验收）
> **定位**：投研效率工具（B 定位）—— 系统给数据和分析，决策权归用户

---

## 0. Phase 1 的边界和不做什么

### 0.1 Phase 1 要解决的核心问题

当前 P10 的代码完整度约 70%，但**从未完整跑通过主链路**。主生产表 `judgments` 只有 5 行、`regime_daily` 只有 3 行、候选池三方不一致、detector-engine 存在 mapping bug、数据层有静默停更。

Phase 1 不做新功能，**只做三件事**：
1. 修掉已知硬伤（detector/engine 语义对齐、候选池单一化、特征补齐）
2. 建立运行时自检机制（assertion、数据新鲜度监控）
3. 让主链路第一次端到端跑通（watchlist 全覆盖 × 连续 10 个交易日）

### 0.2 Phase 1 **不做**的事（重要）

以下功能推迟到 Phase 2+，本阶段禁止引入：

- ❌ Jump Model / HMM regime 检测（Phase 2）
- ❌ LLM 多角色分析（bull/bear/证伪）（Phase 2）
- ❌ 盘中小时线择时模块（Phase 2）
- ❌ Wiki 深度建设（Phase 3，Phase 1 只建基础 schema + 最小写入）
- ❌ 判断追踪的自我进化层（Phase 3）
- ❌ Polygon.io 等新数据源接入
- ❌ 任何 React 前端工作
- ❌ Backtest 相关改动（独立 chat 跟进）

**Claude Code 如果在 Phase 1 开发过程中发现以上模块需要改动，必须停下来向用户确认，不要自作主张扩展范围。**

---

## 1. Phase 1 总览

### 1.1 模块清单

| 编号 | 模块 | 类型 | 优先级 |
|------|------|------|--------|
| M1 | Regime detector/engine 语义对齐 | 修 bug | P0 |
| M2 | 候选池单一事实源（stock_universe 表） | 数据治理 | P0 |
| M3 | 运行时 assertion 层（invariants.py） | 新增 | P0 |
| M4 | features_daily 全覆盖补齐 | 数据补齐 | P0 |
| M5 | 数据健康度监控（data_freshness） | 新增 | P1 |
| M6 | 主生产 scheduler 启动 | 启用已有模块 | P1 |
| M7 | composite 分析链路跑通 | 启用已有模块 | P1 |
| M8 | Telegram 日报推送 | 启用已有模块 | P1 |
| M9 | Wiki 最小版本 schema + 个股页自动写入 | 新增 | P2 |
| M10 | Phase 1 验收清单 | 验证 | P2 |

### 1.2 开发顺序

严格按 M1 → M10 顺序推进。**前一个模块未通过验收，不开始下一个**。

### 1.3 Phase 1 成功标准（硬性）

Phase 1 完成当天必须满足全部 10 条：

1. ✅ `regime_daily` 表近 10 个交易日每天都有记录（CN + US 各 1 条/日）
2. ✅ `judgments` 表近 10 个交易日累计 ≥ 200 行（20-30 只股票 × 10 日）
3. ✅ `stock_universe` 表 = watchlist 唯一事实源，CN 20-30 只 + US 20-30 只
4. ✅ `features_daily` 覆盖 `stock_universe` 100% 的股票，最新日期 = 最近交易日
5. ✅ `data_freshness_check` 每日自动跑，任何数据源停更超阈值触发 Telegram 告警
6. ✅ `detect_regime` 产出的 regime_mode 严格 ∈ {offense, cautious_offense, defense, risk_off}
7. ✅ 所有消费 regime_mode 的下游模块都从配置表读取，无硬编码 fallback
8. ✅ Telegram `/daily` 命令返回当日 watchlist 所有股票的 composite 排名和关键指标
9. ✅ Wiki `stocks/CN/*.md` 和 `stocks/US/*.md` 对 watchlist 每只股票存在 1 页（可以内容简单，但必须存在）
10. ✅ 所有 invariants 在主链路中被真实触发过（assertion 失败的日志清零）

---

## 2. 模块详细规格

### M1 · Regime detector/engine 语义对齐

#### 问题背景

当前 `core/regime/detector.py` 只产出 4 种 mode（offense / cautious_offense / defense / risk_off），但 `engine.py` 的 `_REGIME_MAX_POS` 字典里配置的 key 是 7 种（offense / bull_trend / recovery / neutral / volatile / risk_off / defense），**且缺失 `cautious_offense` 这个 key**。

导致 detector 吐 cautious_offense 时，engine 静默 fallback 到 `_DEFAULT_MAX_POS = 0.15`，detector 的分级设计失效。

#### 任务清单

**Task 1.1：grep 整个代码库，找出所有消费 `regime_mode` 的地方**

```bash
grep -rn "regime_mode" --include="*.py" . | grep -v __pycache__ | grep -v test
grep -rn "REGIME_MAX_POS\|offense\|cautious_offense\|defense\|risk_off" --include="*.py" . | grep -v __pycache__
```

整理成一份文档 `docs/regime_consumers.md`，列出：
- 文件路径 + 行号
- 消费形式（dict lookup / if-elif / 字符串匹配）
- 消费的字段（max_position_pct / signal_threshold_adj / weights / 其他）

**Task 1.2：建立统一的 regime 参数表**

`config/regime_params.yaml` 作为唯一事实源，schema 如下：

```yaml
thresholds:
  trend_up: 60
  trend_down: 40
  volatility_high: 60

corrections:
  breadth_low_downgrade: 30
  liquidity_high_upgrade: 70

regimes:
  offense:
    max_position_pct: 0.80
    signal_threshold_adj: 1.0
    weights:
      technical: 0.30
      fundamental: 0.25
      flow: 0.25
      sentiment: 0.20
  cautious_offense:
    max_position_pct: 0.60
    signal_threshold_adj: 0.90
    weights:
      technical: 0.25
      fundamental: 0.30
      flow: 0.25
      sentiment: 0.20
  defense:
    max_position_pct: 0.40
    signal_threshold_adj: 0.80
    weights:
      technical: 0.20
      fundamental: 0.35
      flow: 0.25
      sentiment: 0.20
  risk_off:
    max_position_pct: 0.20
    signal_threshold_adj: 0.70
    weights:
      technical: 0.20
      fundamental: 0.40
      flow: 0.25
      sentiment: 0.15
```

**具体数值需要用户最终确认**（weights 和 max_pos 数值是 placeholder，开发完后向用户 review）。

**Task 1.3：在 `core/regime/__init__.py` 或新建 `core/regime/constants.py` 定义规范**

```python
VALID_REGIME_MODES = frozenset([
    "offense",
    "cautious_offense",
    "defense",
    "risk_off",
])
```

**Task 1.4：修改所有消费方**

- 所有 `_REGIME_MAX_POS` / `_SIGNAL_THRESHOLD_ADJ` 类字典从硬编码改为**从 `regime_params.yaml` 读取**
- 删除所有永不出现的 mode key（bull_trend / recovery / neutral / volatile）
- 删除 `_DEFAULT_MAX_POS` fallback——改为 raise ValueError（见 M3）

**Task 1.5：增加迁移检查**

写一个脚本 `scripts/verify_regime_alignment.py`，执行：
```sql
SELECT DISTINCT regime_mode FROM regime_daily;
SELECT DISTINCT regime_mode FROM backtest_regime_daily;
```
断言结果集 ⊆ `VALID_REGIME_MODES`，否则报错。

#### 验收标准

- `detect_regime()` 产出的所有 mode 严格来自 `VALID_REGIME_MODES`
- 所有消费 regime_mode 的代码路径，对这 4 个 mode 都能找到对应配置，无 fallback
- `verify_regime_alignment.py` 通过
- `docs/regime_consumers.md` 完整列出所有消费方

---

### M2 · 候选池单一事实源

#### 问题背景

当前 3 个候选池源两两零交集：
- 主 `config/watchlist.yaml`：空
- `backtest/config/watchlist.yaml`：61 只
- `stock_universe` 表：7 只

下游消费混乱，`features_daily` 只覆盖 backtest 的 61 只，主项目启动会因为 `stock_universe` 里的股票没有 features 报错。

#### 任务清单

**Task 2.1：确立 `stock_universe` 表为唯一事实源**

Schema 扩展：

```sql
ALTER TABLE stock_universe ADD COLUMN IF NOT EXISTS active BOOLEAN DEFAULT TRUE;
ALTER TABLE stock_universe ADD COLUMN IF NOT EXISTS priority SMALLINT DEFAULT 1;
ALTER TABLE stock_universe ADD COLUMN IF NOT EXISTS tags JSONB DEFAULT '[]';
ALTER TABLE stock_universe ADD COLUMN IF NOT EXISTS notes TEXT;

CREATE INDEX IF NOT EXISTS idx_universe_active ON stock_universe(market, active);
```

字段说明：
- `active`：false 表示停止追踪但保留历史（退市、主动移除）
- `priority`：1=核心、2=观察、3=储备。影响 composite 计算频率和 Wiki 优先级
- `tags`：["rare_earth", "ai_infrastructure", "semiconductor"] 等标签
- `notes`：加入原因、撤出原因等人类可读说明

**Task 2.2：批量导入用户提供的 watchlist**

用户会提供一份 `inputs/watchlist_seed.csv`，格式：
```
symbol,market,industry,priority,tags,notes
600519.SH,CN,食品饮料,1,["消费白马"],核心持仓候选
000858.SZ,CN,食品饮料,2,["消费白马"],观察
...
```

写一个 `scripts/load_watchlist.py`：
- 读 csv → upsert 到 `stock_universe`
- 默认 `active=TRUE, source='manual'`
- 记录 `added_date = today`，`added_reason` 取自 `notes` 字段

**如果 csv 不存在**：Claude Code 暂停，不自创 watchlist 数据。向用户索要 csv。

**Task 2.3：改造 `config/watchlist.yaml` 为启动引导文件**

内容：
```yaml
# 本文件仅用于首次 bootstrap seed，主项目运行时从 stock_universe 表读取。
# 修改 watchlist 请通过：
#   1. 编辑 inputs/watchlist_seed.csv 然后跑 scripts/load_watchlist.py
#   2. 或通过 Telegram /watchlist_add SYMBOL 命令（Phase 2 实现）

source: stock_universe_table
seed_csv: inputs/watchlist_seed.csv
```

所有主项目代码**禁止**从 `config/watchlist.yaml` 读股票列表。读取逻辑统一走 `db.universe.get_active_symbols(market: str) -> list[str]`。

**Task 2.4：`backtest/config/watchlist.yaml` 保持独立**

回测子项目继续使用自己的 yaml，不和主项目耦合。在 `backtest/CLAUDE.md` 里加一段注释说明这是有意的独立。

#### 验收标准

- `stock_universe` 表 = 所有下游模块读取 watchlist 的唯一来源
- 代码库 grep `watchlist.yaml` 只在 2 处出现：bootstrap 脚本 + backtest 独立子项目
- `scripts/load_watchlist.py` 能从 csv 幂等导入（多次跑结果一致）
- `db.universe.get_active_symbols('CN')` 和 `get_active_symbols('US')` 返回正确股票列表

---

### M3 · 运行时 Assertion 层（`core/invariants.py`）

#### 问题背景

当前系统没有运行时不变量检查。Mapping bug 跑了一整个回测（22 分钟）才被发现，如果有 assertion，1 行代码就会当场抛错。

这个模块是 Phase 1 的核心治理基础设施，**所有后续模块的开发必须遵循 invariants 规范**。

#### 任务清单

**Task 3.1：新建 `core/invariants.py`**

核心 API：

```python
"""
运行时不变量断言 —— 让静默失败立即显性化。

哲学：
- 宁可当场 crash，不要 silent fallback
- 生产环境 assertion 失败 → 立即 Telegram 告警 + 停止后续处理
- 开发环境 assertion 失败 → raise，让测试/CI 捕获
"""

from typing import Any, Iterable

class InvariantViolation(Exception):
    """不变量违规，表示系统处于不应该出现的状态。"""
    pass


def assert_in(value: Any, allowed: Iterable, context: str) -> None:
    """断言 value 在 allowed 集合中。"""
    if value not in allowed:
        raise InvariantViolation(
            f"[{context}] value={value!r} not in allowed={set(allowed)}"
        )


def assert_range(value: float, lo: float, hi: float, context: str) -> None:
    """断言 value ∈ [lo, hi]。"""
    if not (lo <= value <= hi):
        raise InvariantViolation(
            f"[{context}] value={value} out of range [{lo}, {hi}]"
        )


def assert_superset(actual: set, required: set, context: str) -> None:
    """断言 actual ⊇ required。"""
    missing = required - actual
    if missing:
        raise InvariantViolation(
            f"[{context}] missing required elements: {missing}"
        )


def assert_fresh(last_update_date, max_age_days: int, context: str) -> None:
    """断言数据新鲜度。"""
    from datetime import date, timedelta
    if last_update_date is None:
        raise InvariantViolation(f"[{context}] last_update_date is None")
    age = (date.today() - last_update_date).days
    if age > max_age_days:
        raise InvariantViolation(
            f"[{context}] data stale: last_update={last_update_date}, age={age}d, max={max_age_days}d"
        )


def assert_not_empty(seq, context: str) -> None:
    """断言序列非空。"""
    if len(seq) == 0:
        raise InvariantViolation(f"[{context}] sequence is empty")
```

**Task 3.2：在关键链路上布点**

Claude Code 需要识别以下位置并加入 assertion（**具体位置由 Claude Code 基于代码结构判断**，但必须覆盖下面的语义）：

| 断言位置 | 语义 |
|---------|------|
| `detect_regime()` 返回前 | `assert_in(regime_mode, VALID_REGIME_MODES, "regime_detector.output")` |
| 任何 engine 消费 regime_mode 前 | `assert_in(mode, config['regimes'].keys(), "engine.regime_consumer")` |
| `composite.calculate()` 返回前 | `assert_range(score, 0, 100, "composite.final_score")` |
| 启动 scheduler 前 | `assert_superset(features_symbols, universe_symbols, "startup.features_coverage")` |
| 每个数据源拉取完成后 | `assert_fresh(max_date, expected_max_age, f"data_source.{source_name}")` |
| LLM 输出解析后 | `assert_in(direction, {"bullish","bearish","neutral"}, "llm.direction")` |

**Task 3.3：InvariantViolation 的全局处理**

在 `bot/telegram_bot.py` 和 `scheduler/scheduler.py` 的顶层异常处理器中：
- 捕获 `InvariantViolation`
- 写入 `logs/invariant_violations.log`
- 推送 Telegram 红色告警：`🚨 INVARIANT VIOLATION: {context} - {details}`
- **停止当前周期的后续处理**（不要 swallow 后继续）

**Task 3.4：测试**

在 `tests/test_invariants.py` 写基础用例，覆盖 5 个 API 的 happy path + failure path。**这是 Phase 1 唯一强制要求的测试文件**。其他模块不强求测试覆盖，但 invariants 必须有。

#### 验收标准

- `core/invariants.py` 实现 5 个 API
- 关键链路 6 个位置都有 assertion
- `InvariantViolation` 在顶层被捕获并推送 Telegram
- `tests/test_invariants.py` 全部通过

---

### M4 · features_daily 全覆盖补齐

#### 问题背景

`features_daily` 当前只覆盖 backtest watchlist 的 61 只，`stock_universe` 里的 000001.SZ / 600519.SH 等股票没有 features。启动 scheduler 会因为 features 缺失报错或降级到占位分。

#### 任务清单

**Task 4.1：诊断 feature 缺失现状**

写 `scripts/diagnose_feature_coverage.py`：

```sql
SELECT 
    u.symbol, u.market, u.priority,
    MAX(f.trade_date) AS last_feature_date,
    COUNT(f.trade_date) AS feature_days,
    (CURRENT_DATE - MAX(f.trade_date))::INT AS days_since_last
FROM stock_universe u
LEFT JOIN features_daily f ON u.symbol = f.symbol
WHERE u.active = TRUE
GROUP BY u.symbol, u.market, u.priority
ORDER BY u.market, u.priority, u.symbol;
```

输出到 `reports/feature_coverage_YYYYMMDD.csv`。

**Task 4.2：补齐历史 features**

- 对 `stock_universe` 里 `active=TRUE` 的每只股票，回溯至少 **250 个交易日**（约 1 年）的历史 features
- 调用已有的 `data/pipeline/feature_compute.py`，参数是 `symbols=universe_symbols, start_date=today-300d`
- 写入前用 invariants 检查：features 结果的 symbol 集合必须 ⊇ universe 集合

**数据源调用预算**：
- Tushare Pro 5200 分：单次 `daily` 接口 1-2 分/股票·年 ≈ 60 只股 × 2 分 = 120 分，足够
- yfinance：免费无限

**Task 4.3：增量更新任务**

在 `scheduler/scheduler.py` 增加每日定时任务 `update_features_daily`：
- 执行时间：CN 15:30 / US 盘后 16:30 ET
- 对 `stock_universe` 所有 active 股票，拉取**当日**的 features
- 失败重试 3 次，仍失败则写入 `data_quality_checks` 并告警

#### 验收标准

- `features_daily` 覆盖 `stock_universe` 100% 的 active 股票
- 每只股票至少有 250 天历史 feature
- 每日增量更新任务在 scheduler 中注册

---

### M5 · 数据健康度监控

#### 问题背景

`cn_cpi_yoy` 和 `cn_pmi_mfg` 在 Tushare 源头停更 8 个月，系统没有任何告警。需要建立主动监控。

#### 任务清单

**Task 5.1：定义数据源的预期更新频率**

新建表 `data_source_expectations`：

```sql
CREATE TABLE IF NOT EXISTS data_source_expectations (
    source_name     TEXT PRIMARY KEY,      -- e.g. "tushare.cn_cpi_yoy"
    table_name      TEXT NOT NULL,         -- e.g. "macro_indicators"
    filter_clause   TEXT,                  -- e.g. "indicator_name = 'cn_cpi_yoy'"
    date_column     TEXT NOT NULL,         -- e.g. "data_date"
    frequency       TEXT NOT NULL,         -- daily / weekly / monthly / quarterly
    max_lag_days    INT NOT NULL,          -- 超过这个天数告警
    severity        TEXT NOT NULL,         -- info / warn / critical
    created_at      TIMESTAMP DEFAULT NOW()
);
```

初始化条目（Claude Code 基于 macro_indicators 和其他表的实际字段填充）：

```sql
INSERT INTO data_source_expectations VALUES
    ('tushare.market_bars_daily', 'market_bars_daily', 'market = ''CN''', 'trade_date', 'daily', 3, 'critical'),
    ('tushare.fundamentals_daily', 'fundamentals_daily', NULL, 'trade_date', 'daily', 5, 'warn'),
    ('tushare.moneyflow_daily', 'moneyflow_daily', NULL, 'trade_date', 'daily', 5, 'warn'),
    ('tushare.northbound_daily', 'northbound_daily', NULL, 'trade_date', 'daily', 5, 'warn'),
    ('tushare.cn_cpi_yoy', 'macro_indicators', 'indicator_name = ''cn_cpi_yoy''', 'data_date', 'monthly', 45, 'warn'),
    ('tushare.cn_pmi_mfg', 'macro_indicators', 'indicator_name = ''cn_pmi_mfg''', 'data_date', 'monthly', 45, 'warn'),
    ('tushare.cn_m2_yoy', 'macro_indicators', 'indicator_name = ''cn_m2_yoy''', 'data_date', 'monthly', 45, 'warn'),
    ('yfinance.us_market_bars', 'market_bars_daily', 'market = ''US''', 'trade_date', 'daily', 3, 'critical'),
    -- ... Claude Code 需要根据实际数据源补齐
```

**Task 5.2：每日新鲜度检查脚本**

`scripts/data_freshness_check.py`：

```python
"""
对 data_source_expectations 里的每一项：
1. 跑 SQL: SELECT MAX({date_column}) FROM {table_name} WHERE {filter_clause}
2. 计算 lag_days = (today - max_date).days
3. 若 lag_days > max_lag_days → 按 severity 推送
4. 写入 data_freshness_log 表（记录本次检查结果）
"""
```

结果表：

```sql
CREATE TABLE IF NOT EXISTS data_freshness_log (
    check_id        SERIAL PRIMARY KEY,
    check_time      TIMESTAMP DEFAULT NOW(),
    source_name     TEXT NOT NULL,
    max_date        DATE,
    lag_days        INT,
    status          TEXT,    -- ok / warn / critical
    message         TEXT
);

CREATE INDEX IF NOT EXISTS idx_freshness_log_time ON data_freshness_log(check_time DESC);
```

**Task 5.3：Telegram 告警**

- `status = critical` → 立即推送，红色 🚨
- `status = warn` → 累积到每日 08:30 的 daily digest 推送，黄色 ⚠️
- `status = ok` → 不推送，只记日志

**Task 5.4：Scheduler 集成**

在 `scheduler/scheduler.py` 注册每日 08:00 任务 `check_data_freshness`。

#### 验收标准

- `data_source_expectations` 覆盖所有主要数据源（至少 10 个条目）
- 每日 08:00 自动跑 freshness check
- 手工模拟一次停更（改某个 expectation 的 max_lag_days = 1）能触发 Telegram 告警
- `data_freshness_log` 每天有新记录

---

### M6 · 主生产 Scheduler 启动

#### 问题背景

`scheduler/scheduler.py` 代码已有，但从未启用。主生产表 `regime_daily` / `judgments` 稀疏就是因为 scheduler 没跑。

#### 任务清单

**Task 6.1：Review 现有 scheduler 代码**

Claude Code 先读 `scheduler/scheduler.py`，输出一份 `docs/scheduler_audit.md`，包含：
- 现有注册的 job 列表
- 每个 job 的功能、触发时间、依赖模块
- 发现的问题（硬编码、缺少错误处理、等）

**Task 6.2：确立 Phase 1 的 job 清单**

基于 audit 结果，Phase 1 必须有以下 job（缺失则新增）：

| Job 名称 | 频率 | 时间（北京时间）| 功能 |
|---------|------|----------|------|
| `check_data_freshness` | 日 | 08:00 | M5 数据新鲜度 |
| `pull_cn_market_data` | 日 | 15:15 | 拉 A 股日线、资金流、基本面 |
| `pull_us_market_data` | 日 | 05:30 | 拉美股日线（盘后）|
| `update_features_daily_cn` | 日 | 15:30 | 计算 CN features |
| `update_features_daily_us` | 日 | 05:45 | 计算 US features |
| `detect_regime_cn` | 日 | 15:40 | CN regime 检测 |
| `detect_regime_us` | 日 | 06:00 | US regime 检测 |
| `run_composite_analysis_cn` | 日 | 16:00 | CN 所有 watchlist 股票综合分析 |
| `run_composite_analysis_us` | 日 | 06:30 | US 所有 watchlist 股票综合分析 |
| `send_daily_digest` | 日 | 16:30 + 07:00 | Telegram 日报（CN 收盘后 + US 收盘后）|

**Task 6.3：错误处理规范**

每个 job 的 wrapper 遵循：
```python
async def safe_run_job(job_name, job_func):
    try:
        await job_func()
    except InvariantViolation as e:
        # 立即 Telegram 红色告警 + 停止本次周期
        await alert_telegram(f"🚨 {job_name} INVARIANT: {e}")
        raise
    except Exception as e:
        # 其他错误记日志 + 告警（但不中断调度）
        logger.exception(f"{job_name} failed")
        await alert_telegram(f"⚠️ {job_name} failed: {type(e).__name__}: {e}")
```

**Task 6.4：启动脚本**

`scripts/start_scheduler.py`：
- 启动前跑一遍 M3 的 startup invariants（features 覆盖、universe 非空、regime config 可读等）
- 任何一个失败 → 拒绝启动 + 推送告警
- 全部通过 → 启动 scheduler，推送"✅ Scheduler started at {time}"

**Task 6.5：supervisor / systemd 脚本**

提供 `deploy/alpharadar.service`（systemd unit 文件模板），方便部署。**不强制 Claude Code 部署**，只提供模板。

#### 验收标准

- `docs/scheduler_audit.md` 完成
- 所有 10 个 job 注册到 scheduler
- `scripts/start_scheduler.py` 能成功启动
- 启动前自检 invariants 全部通过
- 手工运行 24 小时，所有 job 都至少跑过一次

---

### M7 · Composite 分析链路跑通

#### 问题背景

`core/analysis/composite.py` 代码存在，但主 `judgments` 表只有 5 行。Phase 1 目标：watchlist 每只股票每天产生 1 条 judgment。

#### 任务清单

**Task 7.1：Review 现有 composite 链路**

读 `core/analysis/{technical,fundamental,flow,sentiment,composite,stage_detector}.py` + `llm/client.py`，输出 `docs/composite_audit.md`：
- 数据流图（哪些输入、哪些输出）
- 已知问题和硬编码
- LLM 调用点和预期 prompt

**Task 7.2：确保 Phase 1 的 LLM 使用是轻量的**

Phase 1 **不做**多角色辩论、不做 bull/bear 分析。LLM 只做：
- 读取四维度打分
- 生成一段 200-400 字的中文叙事
- 输出 direction (bullish/bearish/neutral) + confidence (0-1)
- 返回 JSON 结构：

```json
{
  "direction": "bullish",
  "confidence": 0.65,
  "key_drivers": ["technical score 78 due to strong momentum", "flow turning positive"],
  "risks": ["fundamental score below 50", "sentiment neutral"],
  "narrative": "300字叙事..."
}
```

Prompt 模板放在 `prompts/composite_narrative.md`。

**Task 7.3：写入 judgments 表**

每次 composite 完成后写入，字段：
- symbol, market, judgment_date
- direction, confidence
- 四维度分 + composite 总分
- regime_snapshot (JSONB, 当时的 regime_daily 快照)
- signal_sources (JSONB, 每个维度的关键信号)
- logic_text (LLM 叙事)
- llm_model (e.g. "deepseek-v3")
- llm_tokens_used

**Task 7.4：失败处理**

- 某一只股票 LLM 调用失败 → 记录 direction=null, confidence=null, logic_text=null，其他字段照常写入（允许部分成功）
- 整批失败率 > 20% → 触发告警

**Task 7.5：成本监控**

每日 digest 里带一行 LLM 成本："今日 LLM 调用 XX 次，tokens YY，估算成本 ZZ 元"

#### 验收标准

- `judgments` 表每日新增行数 = watchlist 活跃股票数（允许 5% 失败）
- LLM 叙事输出符合 JSON schema
- Direction 字段值严格 ∈ {bullish, bearish, neutral}
- 日均 LLM 成本 < 10 元

---

### M8 · Telegram 日报推送

#### 问题背景

`bot/telegram_bot.py` 存在，但 `telegram_commands` 表 0 行——bot 没在常驻。Phase 1 让 bot 真实常驻 + 日报自动推送。

#### 任务清单

**Task 8.1：日报内容规范**

每天 2 次推送：
- **CN 盘后**（16:30 北京时间）：CN watchlist 今日分析摘要
- **US 盘后**（07:00 北京时间，次日）：US watchlist 最新分析摘要

日报内容模板：

```
📊 P10 日报 · CN · 2026-04-21

🌐 市场环境
Regime: offense (趋势↑ + 波动低)
HS300: +0.35% | ZZ1000: +0.52% | 宽度: 62 | 北向: 净流入 12.3亿

🎯 Watchlist 综合排名 (24 只活跃)

🟢 Top 5 看多:
1. 600519.SH 贵州茅台  | comp 78 | tech 85 | fund 72 | 信心 0.72
2. 002371.SZ 北方华创  | comp 75 | tech 80 | fund 68 | 信心 0.68
3. ...

🔴 Top 3 看空:
1. 600030.SH 中信证券  | comp 35 | tech 28 | fund 45 | 信心 0.55
...

⚠️ 需关注:
• 600519.SH regime 由 cautious_offense 升至 offense
• 002371.SZ 突破 20日新高
• 数据健康: 所有数据源正常

💰 今日 LLM 成本: ¥6.8 (DeepSeek V3)
```

具体字段由 Claude Code 决定，但必须包含：regime / 指数表现 / Top N 看多 / Top N 看空 / 异动提醒 / 成本。

**Task 8.2：交互命令（Phase 1 最小集）**

| 命令 | 功能 |
|------|------|
| `/daily` | 手动触发最新日报 |
| `/judge SYMBOL` | 返回单股最新 composite 分析详细版 |
| `/regime` | 返回当前 CN + US regime 状态 |
| `/universe` | 返回 active watchlist 列表 |
| `/health` | 数据健康度 + scheduler 各 job 最后成功时间 |

**禁止 Phase 1 引入**：
- 交易命令（/buy, /sell 等，Phase 3+ 才考虑，且仍然只是提醒，不下单）
- 编辑 watchlist 的命令（先用脚本）
- 复杂查询（/history, /compare 等）

**Task 8.3：常驻运行**

`scripts/start_bot.py` + `deploy/alpharadar-bot.service` 模板。

**Task 8.4：命令日志**

每次命令调用写入 `telegram_commands` 表：
- timestamp, user_id, command, args, response_preview, duration_ms, error

#### 验收标准

- Bot 常驻运行
- 每日 2 次日报自动推送，内容完整
- 5 个命令全部可用
- `telegram_commands` 表有记录

---

### M9 · Wiki 最小版本

#### 范围（Q7 回答：选 ii）

Phase 1 只做：
- 建全 schema（预留后续扩展字段）
- 个股页自动生成 + 写入
- 经验条目（experience_store）schema + 手动写入能力

**不做**：
- 行业页、策略页、宏观页、trades 页
- RAG 检索、embedding 搜索
- Wiki 的自动经验提炼（Phase 3）

#### 任务清单

**Task 9.1：Review 现有 Wiki 代码**

`llm/wiki_manager.py` + `wiki_pages` 表。输出 `docs/wiki_audit.md`。

**Task 9.2：Schema 完整化**

```sql
-- 确保 wiki_pages 表字段完整
CREATE TABLE IF NOT EXISTS wiki_pages (
    page_id         SERIAL PRIMARY KEY,
    page_type       TEXT NOT NULL,      -- stock / industry / strategy / macro / trade
    page_key        TEXT NOT NULL,      -- e.g. "CN/600519.SH" for stocks
    title           TEXT NOT NULL,
    content         TEXT,               -- Markdown
    metadata        JSONB DEFAULT '{}',
    embedding       VECTOR(1024),       -- pgvector, Phase 3 启用 RAG 时再用
    version         INT DEFAULT 1,
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW(),
    UNIQUE (page_type, page_key)
);

CREATE INDEX IF NOT EXISTS idx_wiki_pages_type ON wiki_pages(page_type);

-- experience_store 
CREATE TABLE IF NOT EXISTS experience_store (
    exp_id          SERIAL PRIMARY KEY,
    title           TEXT NOT NULL,
    content         TEXT NOT NULL,
    category        TEXT,               -- e.g. "regime_behavior" / "stock_specific" / "bias_pattern"
    status          TEXT DEFAULT 'under_review',  -- under_review / active / deprecated
    source          TEXT,               -- manual / auto_review / llm_extracted
    evidence_count  INT DEFAULT 0,      -- 支撑该经验的观察次数
    created_at      TIMESTAMP DEFAULT NOW(),
    reviewed_at     TIMESTAMP,
    deprecated_at   TIMESTAMP,
    metadata        JSONB DEFAULT '{}'
);
```

**Task 9.3：个股页自动生成**

每次 `run_composite_analysis` 成功后，追加/更新个股 Wiki 页：

- `page_type = 'stock'`
- `page_key = '{market}/{symbol}'`，e.g. "CN/600519.SH"
- 内容结构（Markdown）：

```markdown
# 600519.SH 贵州茅台

## 基本信息
- 市场: CN
- 行业: 食品饮料
- 标签: 消费白马, 核心持仓候选
- 加入 watchlist: 2026-04-20
- 最近更新: 2026-04-21 16:00

## 最新综合判断 (2026-04-21)
- 方向: bullish | 信心: 0.72
- Composite: 78 (tech 85 / fund 72 / flow 76 / sent 72)
- Regime: offense
- 叙事: [LLM 300字]

## 历史判断追踪 (最近 10 条)
| 日期 | 方向 | 信心 | Comp | T+5 收益 | T+10 收益 | 是否正确 |
|------|------|------|------|---------|----------|---------|
| 2026-04-21 | bullish | 0.72 | 78 | - | - | - |
| 2026-04-20 | bullish | 0.68 | 75 | - | - | - |
...

## 维度趋势 (最近 20 日)
<!-- 文字描述 tech/fund/flow/sent 各维度的滚动趋势 -->

## 关键事件日志
<!-- 手动添加 or 自动填入：公告、研报、财报等 -->
```

每日更新，保留历史版本（`version` 字段递增）。

**Task 9.4：经验条目的初始化**

Phase 1 不做自动提炼，但提供：
- `scripts/add_experience.py SYMBOL CATEGORY TITLE CONTENT` 命令行工具
- Telegram `/exp_add` 命令（Phase 2 才引入，Phase 1 不做）

由用户手动添加初始经验条目。Phase 1 目标：积累 10+ 条 active 状态的经验。

#### 验收标准

- `wiki_pages` 表对 watchlist 每只股票都有一页（内容可自动生成）
- 每日综合分析后个股页自动更新
- `experience_store` schema 就绪
- 有至少 1 条 `add_experience.py` 手动写入的样本

---

### M10 · Phase 1 验收清单

Claude Code 完成 M1-M9 后，跑一遍以下验收：

**Task 10.1：端到端自检脚本**

写 `scripts/phase1_acceptance.py`，跑完输出一份 markdown 报告：

```
## Phase 1 验收报告 (生成时间: YYYY-MM-DD HH:MM)

### 1. Regime 产出一致性
- [ ] regime_daily CN 最近 10 日覆盖率: X/10
- [ ] regime_daily US 最近 10 日覆盖率: X/10
- [ ] 所有 regime_mode ∈ VALID_REGIME_MODES: PASS/FAIL

### 2. Judgments 产出
- [ ] 最近 10 日累计 judgment 行数: X (目标 ≥ 200)
- [ ] 每日 watchlist 覆盖率均值: X% (目标 ≥ 95%)
- [ ] direction 分布: bullish X / neutral Y / bearish Z

### 3. Universe 单一事实源
- [ ] stock_universe active 股票数: CN X, US Y
- [ ] watchlist.yaml 是否已降级为 seed: PASS/FAIL
- [ ] features_daily 覆盖率: X% (目标 100%)

### 4. 数据新鲜度
- [ ] data_source_expectations 条目数: X
- [ ] 当前 critical 告警数: X
- [ ] 当前 warn 告警数: X
- [ ] data_freshness_log 最近 7 日记录数: X

### 5. Invariants
- [ ] core/invariants.py 已实现 5 个 API
- [ ] 测试通过率: X/Y
- [ ] 最近 7 日生产环境 InvariantViolation 次数: X (理想 0)

### 6. Scheduler
- [ ] 注册的 job 数: X (目标 ≥ 10)
- [ ] 最近 24 小时内每个 job 都成功跑过: PASS/FAIL

### 7. Telegram Bot
- [ ] Bot 在线: PASS/FAIL
- [ ] 日报推送记录: 最近 7 日 X 次
- [ ] 5 个命令可用性: X/5

### 8. Wiki
- [ ] wiki_pages 中 stock 类型页数: X (目标 = watchlist size)
- [ ] experience_store 行数: X

### 9. LLM 成本
- [ ] 最近 7 日总成本: ¥X
- [ ] 日均成本: ¥X

### 10. Overall
- [ ] Phase 1 成功标准 10 条满足数: X/10
- [ ] 建议进入 Phase 2: PASS/FAIL
```

**Task 10.2：交付文档**

在 `docs/phase1_completion.md` 记录：
- Phase 1 实际完成的内容
- 与原方案的偏离（如果有）
- 发现的新问题，建议在 Phase 2 处理
- 已知 bug / 暂时绕过的问题

#### 验收标准

10 项全部 PASS 或用户明确接受妥协。

---

## 3. Phase 1 开发规约

### 3.1 工作方式

Claude Code 按模块顺序开发，每个模块完成后：
1. 跑模块自己的验收标准
2. 在 `docs/phase1_progress.md` 追加一条进度
3. 等待用户确认再进入下一个模块

**禁止跳步骤。** M3 invariants 未完成，M6 scheduler 不能启动。

### 3.2 代码质量规范

- 所有新写的函数必须有中文 docstring
- 所有跨模块的调用必须走已定义的接口（不跨模块访问私有字段）
- 所有配置值必须从 yaml / db 读，禁止硬编码
- 所有 DB 操作必须在事务里（async with conn.transaction())
- 所有 LLM 调用必须带 timeout + retry（timeout=60s, retry=2）

### 3.3 禁止行为

- 禁止修改 `backtest/` 目录下的任何文件
- 禁止新增 React 前端代码
- 禁止引入 Phase 2+ 的功能（Jump Model、多 agent、盘中模块、RAG 等）
- 禁止删除现有数据（未经用户确认）
- 禁止"静默 fallback"——任何异常路径必须显式处理

### 3.4 文档要求

Phase 1 必须产出以下文档（都放在 `docs/`）：
- `regime_consumers.md` (M1)
- `scheduler_audit.md` (M6)
- `composite_audit.md` (M7)
- `wiki_audit.md` (M9)
- `phase1_progress.md` (持续更新)
- `phase1_completion.md` (Phase 1 结束时)

---

## 4. 不在 Phase 1 但需要用户提前准备的

### 4.1 用户需提供的输入文件

**inputs/watchlist_seed.csv**（必须）
用户提供 CN 20-30 只 + US 20-30 只的初始 watchlist。格式见 M2 Task 2.2。

### 4.2 用户需确认的数值

M1 Task 1.2 中 `regime_params.yaml` 的 weights 和 max_position_pct 具体数值，Claude Code 开发完后用户 review 并给定最终值。

### 4.3 用户需做的部署

- 启动 scheduler 常驻（systemd / supervisor / screen）
- 启动 telegram bot 常驻
- 确保 postgres 服务稳定

---

## 5. Phase 2 预告（Phase 1 完成后讨论，不在本方案范围）

Phase 2 将在 Phase 1 稳定运行 **至少 2 周**后启动。届时讨论：
- Regime 从规则升级到 Jump Model（带 fallback 到 HMM）
- LLM 从单调叙事升级到 bull/bear/证伪三段式
- 盘中小时线择时模块（严格受日线 composite 约束）
- Composite 维度权重按 regime 分层 IC 动态调整
- Wiki 扩展到行业页

Phase 2 的具体方案基于 Phase 1 运行 2 周的真实数据再定。

---

## 附录 A · 常见陷阱提醒

**陷阱 1：把"已实现"等同于"已运行"。**
代码文件存在不等于功能可用。Phase 1 每个模块的验收都基于 DB 行数和真实调用日志，不基于代码行数。

**陷阱 2：LLM 幻觉接口。**
Claude Code 开发中可能调用不存在的函数或字段。**每个模块完成后必须跑通一次真实调用**，不能只靠 type check。

**陷阱 3：Silent fallback 的陷阱。**
任何 `try: ... except: pass` 或 dict.get(key, default) 类型的代码，都要审视是否应该改为 assertion + 报警。Phase 1 的哲学是"宁可 crash 不要静默"。

**陷阱 4：测试覆盖过度。**
Phase 1 只强制要求 `tests/test_invariants.py`。其他模块不写测试。优先把流程跑通，测试放 Phase 2+。

---

## 附录 B · 与上次快照（2026-04-19）的对比目标

| 指标 | 快照当前 | Phase 1 目标 |
|------|---------|-------------|
| judgments 行数（主表）| 5 | ≥ 200（近 10 日）|
| regime_daily 行数（主表）| 3 | 20 |
| stock_universe active | 7 | 40-60 |
| features_daily 覆盖率 | 61/7 = N/A | 100% of universe |
| telegram_commands 行数 | 0 | ≥ 30/周 |
| 数据新鲜度监控 | 无 | 自动运行 |
| InvariantViolation 机制 | 无 | 已实现并在生产中运行 |

---

**文档结束。开发前请确认：**
1. 你已提供 `inputs/watchlist_seed.csv`
2. 你理解 Phase 1 的边界（不引入 Phase 2+ 的新功能）
3. 你理解每个模块的验收标准
4. 你接受"宁可 crash 不要静默 fallback"的设计哲学
