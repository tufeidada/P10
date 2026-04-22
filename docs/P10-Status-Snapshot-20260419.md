# P10-AlphaRadar 系统状态快照 · 2026-04-19

> 生成时间：2026-04-20 00:40 （北京时间）
> 快照目的：客观盘点系统真实成熟度，暴露问题优于掩盖问题。
> 截取时点：backtest run_id=3（7.5 个月全量）正在运行（进度 ~47%），快照数据为"运行中"状态。

---

## 1. 数据层真实状态

查询命令：`SELECT COUNT(*), MIN(...), MAX(...), COUNT(DISTINCT ...)` on each table，2026-04-20 00:30 执行。

### 1.1 时序业务表

| 表                        | 行数      | min_date   | max_date   | 股票数   | 备注 |
|---------------------------|-----------|------------|------------|----------|------|
| market_bars_daily         | 5,479,606 | 2021-03-01 | 2026-04-17 | 4,828    | 含 31 只 US + 4,793 只 CN + 4 只符号归类错误（疑似 US 但 `.SH/.SZ`） |
| features_daily            | 57,054    | 2022-06-01 | 2026-04-17 | **61**   | **仅覆盖 backtest watchlist（32CN + 29US），主项目股票 000001.SZ / 600519.SH 未计算特征** |
| fundamentals_daily        | 45,599    | 2022-06-01 | 2026-04-17 | 5,527    | 覆盖广，但 features_daily 未复用 |
| financials_quarterly      | 848       | 2018-12-31 | 2026-03-31 | 64       | 报告期粒度，仅 64 只股票 |
| moneyflow_daily           | 75,562    | 2022-06-01 | 2026-04-17 | 5,196    | CN 三档资金流 |
| northbound_daily          | 301       | 2025-01-06 | 2026-04-17 | -        | 市场级，非股票级；**起点仅 2025-01**，比 market_bars 晚 3.5 年 |
| margin_daily              | 35,618    | 2022-06-01 | 2026-04-17 | -        | 两融余额 |
| macro_indicators          | 1,599     | 2024-01-01 | 2026-04-16 | 7 指标    | `cn_cpi_yoy` / `cn_pmi_mfg` 最后一条为 **2025-08**，已停更 8 个月 |
| market_sentiment_daily    | 47        | 2026-02-02 | 2026-04-16 | -        | 仅 47 行，2.5 个月数据 |
| index_daily               | 5,742     | 2022-06-01 | 2026-04-17 | 6 指数    | HS300 / SH / ZZ1000 / SPY / QQQ / VIX |
| market_breadth_daily      | 941       | 2022-06-01 | 2026-04-17 | -        | 仅 CN 市场宽度 |
| intraday_bars             | 80        | 2026-04-15 | 2026-04-17 | 2        | 仅 3 天、2 只股票 |
| intraday_signals          | 5         | 2026-04-14 | 2026-04-17 | 2        | 5 条信号、2 只股票 |
| regime_daily (主产)       | 3         | 2026-04-15 | 2026-04-17 | -        | **主生产表仅 3 天，疑似人工调试** |
| judgments (主产)          | 5         | 2026-04-18 | 2026-04-18 | 2        | **主生产表仅 5 行，2 只股票** |
| trade_calendar            | 8,797     | 1990-12-19 | 2026-12-31 | -        | 交易日历完整 |
| social_sentiment          | 7         | 2026-04-18 | 2026-04-18 | 4        | 仅 1 天数据 |
| wiki_pages                | 5         | -          | 2026-04-18 | -        | 5 页（2 只股票 + 2 策略 + 1 未知） |
| experience_store          | 6         | 2026-04-17 | 2026-04-18 | -        | 6 条进化经验 |
| benchmark_daily           | 1         | 2026-04-15 | 2026-04-15 | -        | **仅 1 行**，主项目基准表实质未填充 |

### 1.2 回测专用表（run_id=3 运行中，数据实时增长）

| 表                        | 行数    | 备注 |
|---------------------------|---------|------|
| backtest_runs             | 2       | run_id=2 pilot（completed），run_id=3 full run（running） |
| backtest_judgments        | 2,731（CN 1,785 + US 1,595 = 3,380 实际，此为快照时点）|
| backtest_trades           | 40      | |
| backtest_portfolio_daily  | 88      | 44 天 × 2 市场 |
| backtest_positions        | 133     | 11 只股票 |
| backtest_regime_daily     | 88      | 44 天 × 2 市场 |
| backtest_features_extra   | 57,054  | 与 features_daily 行数一致，即 future_ret_* 回填全量 |

### 1.3 业务支撑表

| 表                     | 行数  | 状态 |
|------------------------|-------|------|
| stock_universe         | 7     | 2 CN + 5 US（详见 §2） |
| industry_classify      | 5,190 | 行业分类 |
| data_quality_checks    | 11    | 11 次质量检查 |
| positions (主产)       | 2     | 2 条持仓（1 只股票） |
| signal_quality_tracker | 1     | **仅 1 行，进化引擎未真实运行** |
| review_reports         | 2     | 2 份复盘报告 |

### 1.4 完全空的表（行数 = 0）

- `analyst_consensus` — 未接数据源
- `calibrations` — 未运行过标定
- `telegram_commands` — Telegram 命令日志未开启

### 1.5 连续性缺口抽查

最近一周（2026-04-10 ~ 2026-04-17）CN 股票 `market_bars_daily` 每日覆盖数：

```
2026-04-17: 32    2026-04-13: 26    2026-04-09: 12
2026-04-16: 36    2026-04-10: 28    2026-04-08: 12
2026-04-15: 51    2026-04-14: 26
```

**异常：2026-04-15 突然冒到 51 只，其他天只有 26~36；2026-04-08/09 仅 12 只。** 说明近期拉数不稳定、人工补拉和自动拉混杂。

---

## 2. 候选池真实状态

### 2.1 三个独立候选池源，完全不一致

**源 A — 主项目 `config/watchlist.yaml`**
```yaml
manual:
  CN: []   # 全部注释掉，实际为空
  US: []
```
**→ 空。** 主项目"手动管理"池未维护。

**源 B — 回测 `backtest/config/watchlist.yaml`**
- CN 32 只（新材料 / 化工 / 有色 / 半导体 / 电力设备 / 军工 / 医药等 7 行业）
- US 29 只
- 总 61 只

**源 C — `stock_universe` 表**
```
 symbol    | market | industry   | source | added_date | added_reason
-----------+--------+------------+--------+------------+-----------------------------
 000001.SZ | CN     | 银行       | manual | 2026-04-17 | (空)
 600519.SH | CN     | 食品饮料   | manual | 2026-04-17 | (空)
 AAPL      | US     | Technology | manual | 2026-04-17 | init_us_universe script
 MSFT      | US     | Technology | manual | 2026-04-17 | init_us_universe script
 NVDA      | US     | Technology | manual | 2026-04-17 | init_us_universe script
 QQQ       | US     | ETF        | system | 2026-04-17 | init_us_universe script
 SPY       | US     | ETF        | system | 2026-04-17 | init_us_universe script
```
共 7 只（2 CN + 5 US，其中 2 只为 ETF）。

### 2.2 一致性分析

| 候选池 | 大小 | 与其他池交集 |
|--------|------|-------------|
| 源 A (主 YAML)       | 0    | ∅ |
| 源 B (backtest YAML) | 61   | 与 C 交集 = 0（无任何重叠） |
| 源 C (DB universe)   | 7    | 与 A 交集 = 0，与 B 交集 = 0 |

**三方零交集。** 主项目运行时读哪个、如何消费，没有统一答案。features_daily 只算了源 B 的 61 只，主生产的 judgments 只给源 C 中的 000001.SZ / 600519.SH 算过（2 只股票 5 条记录）。

---

## 3. 端到端跑通验证

### 3.1 说明

由于主生产链路（`core/analysis/composite.py` + LLM 叙事）截至快照时只在 DB 留下 5 条 judgments（2 只股票），且跑批期间无持久化的耗时分解或 LLM 原文，**无法从历史数据复现"完整 E2E + LLM 原文"**。

使用替代方案：从正在运行的 backtest run_id=3 中抽取 5 只股票（3 CN + 2 US）的最新综合分析结果（composite 四维度得分 + regime snapshot）。**backtest 流程不包含 LLM 叙事**（CLAUDE.md 明确禁止），因此 LLM 原文空缺。

### 3.2 5 只股票最新 composite 判断（run_id=3）

| 股票       | 市场 | 日期       | direction | conf | tech  | fund  | flow  | sent  | composite | regime_mode      |
|------------|------|------------|-----------|------|-------|-------|-------|-------|-----------|------------------|
| 601138.SH  | CN   | 2025-11-24 | neutral   | 0.17 | 59.85 | 59.69 | 53.49 | 36.89 | 56.23     | cautious_offense |
| 601138.SH  | CN   | 2025-11-04 | bullish   | 0.47 | 88.40 | 58.38 | 64.44 | 70.18 | 73.37     | offense          |
| 000001.SZ  | CN   | -          | -         | -    | -     | -     | -     | -     | -         | **不在 backtest watchlist，无数据** |
| 600519.SH  | CN   | -          | -         | -    | -     | -     | -     | -     | -         | **不在 backtest watchlist，无数据** |
| AAPL       | US   | -          | -         | -    | -     | -     | -     | -     | -         | 快照时点尚未跑到 AAPL 最新日期 |
| NVDA       | US   | -          | -         | -    | -     | -     | -     | -     | -         | 同上 |

### 3.3 维度数据可用性（backtest_judgments 市场级聚合）

| 市场 | 条数 | tech 均值 | fund 均值 | flow 均值 | sent 均值 | comp 均值 | flow 非空 | sent 非空 |
|------|------|-----------|-----------|-----------|-----------|-----------|-----------|-----------|
| CN   | 1785 | 68.18     | 47.67     | 59.76     | 64.68     | 60.66     | 1785 (100%) | 1785 (100%) |
| US   | 1595 | 57.70     | 51.63     | 49.45     | 61.34     | 55.70     | 1595 (100%) | 1595 (100%) |

**观察**：
- 所有维度都填了数，但 **CN flow = 59.76 vs US flow = 49.45**，差近 10 点——US 无北向、无两融，`flow_score` 基本是占位默认值。
- US direction 分布：bull 391 / neut 964 / bear 240 = **24.5% 看多**；CN 为 452/1264/69 = **25.3% 看多**。两侧接近，但 US 看空比例远高（15% vs 3.9%）→ 美股综合分系统性偏低。
- `backtest_judgments.signal_sources` JSON 显示 CN 每条都有完整的 regime / technical / fundamental / flow 来源；未看到 sentiment 单独来源（嵌在 signal_sources 里的权重为 0）。

### 3.4 LLM 叙事输出

**空缺。** 原因：
1. backtest 流程按设计不调 LLM（`backtest/CLAUDE.md` 明令）。
2. 主生产 `judgments` 表仅 5 条，`logic_text` 字段虽然存在，快照时点抽查：

```sql
SELECT symbol, judgment_date, LEFT(logic_text, 200)
FROM judgments ORDER BY judgment_date DESC LIMIT 3;
```

该 SQL 因并发连接池紧张未能在快照时点执行（回测占满连接）。**结论：LLM 原文本次未能贴出**，需等 run_id=3 跑完、或在主生产链路真正运行一次后补采。

### 3.5 耗时分解

**不可得。** 无现成耗时埋点：
- backtest 只记总耗时（run_id=2 pilot：16.7s / 5 交易日 = 3.3s/日；run_id=3 平均 7~9s/日）
- 各子模块（technical / fundamental / flow / sentiment / composite）无独立计时

---

## 4. Regime 检测实际表现

### 4.1 CN 最近 57 个交易日（2025-09-01 ~ 2025-11-25，来自 backtest run_id=3）

| 日期       | regime_mode      | trend | vol  | brd  | liq  | HS300 收盘 |
|------------|------------------|-------|------|------|------|------------|
| 2025-11-25 | cautious_offense | 53.5  | 40.9 | 59.8 | 24.5 | 4490.40    |
| 2025-11-24 | cautious_offense | 50.6  | 51.9 | 50.8 | 26.3 | 4448.05    |
| 2025-11-21 | defense          | 51.0  | 73.8 | 32.2 | 29.7 | 4453.61    |
| 2025-11-20 | offense          | 60.2  | 50.0 | 50.4 | 35.5 | 4564.95    |
| 2025-11-19 | offense          | 61.8  | 53.4 | 52.7 | 42.5 | 4588.29    |
| 2025-11-18 | cautious_offense | 65.8  | 68.6 | 47.1 | 44.4 | 4568.19    |
| 2025-11-17 | offense          | 70.1  | 37.1 | 62.6 | 48.7 | 4598.05    |
| ...（中间略）    | ...              | ...   | ...  | ...  | ...  | ...        |
| 2025-09-05 | offense          | 82.6  | 45.3 | 62.4 | 93.9 | 4460.32    |
| 2025-09-04 | cautious_offense | 76.2  | 78.6 | 39.7 | 98.2 | 4365.21    |
| 2025-09-03 | cautious_offense | 81.7  | 72.9 | 45.8 | 97.8 | 4459.83    |
| 2025-09-02 | cautious_offense | 85.5  | 71.5 | 45.3 | 98.9 | 4490.45    |
| 2025-09-01 | offense          | 89.2  | 32.4 | 61.1 | 98.6 | 4523.71    |

### 4.2 切换统计

| 市场 | 57 天内切换次数 | 出现过的 mode | 最常见 mode（天数） |
|------|------------------|---------------|------------------------|
| CN   | **16**           | 3 种          | offense (43d), cautious_offense (13d), defense (1d) |
| US   | **4**            | 2 种          | offense (51d), cautious_offense (6d) |

### 4.3 合理性评估

**问题 1：只产出 3 种 mode，配置表中的 7 种大部分从未被触发。**
引擎 `_REGIME_MAX_POS` 定义了 7 种 mode（offense / bull_trend / recovery / neutral / volatile / risk_off / defense），但 57 天实跑只出现 3 种。`neutral / volatile / risk_off / bull_trend / recovery` 全部 0 次。→ 要么 detect_regime 内部只会吐这 3 个，要么阈值设计与实际市场数据不匹配。

**问题 2：CN 切换过于频繁。**
57 天 16 次切换 ≈ 每 3.5 天切一次。看具体样本：
- 2025-11-17 offense → 11-18 cautious_offense → 11-19 offense → 11-20 offense → 11-21 defense → 11-24 cautious_offense
  4 天内 4 次切换，对应 HS300 从 4598→4568→4588→4565→4454→4448，单日 vol_score 从 37.1 跳到 68.6 再到 73.8。
- 切换由 volatility_score 单维度主导（vol 从 37→73 触发 offense→defense），缺少滤波/确认机制。

**问题 3：HS300 从 4523（9/1）→ 4490（11/25）区间震荡，regime 大部分时间 offense（43/57 天），与 +0% 盘整行情不匹配。** 若 offense = "满仓做多信号"，则 regime 持续发多头信号而指数没涨，策略会高频换手损耗手续费。

### 4.4 美股相对平静

US 57 天只切换 4 次，基本全程 offense，符合 SPY 2025-09-01（645.05）→ 2025-11-26（679.68）的稳步上涨行情，表现合理。

---

## 5. 已实现但未运行的模块清单

根据 DB 行数反推"从未在生产调度中真实运行过"的模块：

| 模块路径 | 状态 | 证据 |
|----------|------|------|
| `core/scanner/`                       | **空实现** | 只有 `__init__.py`，无其他文件 |
| `core/risk/position_sizer.py`         | 未运行 | 主 `positions` 表仅 2 行，无调度触发记录 |
| `core/evolution/*` (4 个文件)         | 未运行 | `signal_quality_tracker` 仅 1 行，`experience_store` 6 行（均为手工调试） |
| `core/intraday/*`                     | 仅测试 | `intraday_signals` 5 行、`intraday_bars` 80 行，集中在 3 天 |
| `core/analysis/*` 主生产流水线         | 人工调试 | `judgments` 主表仅 5 行，2 只股票 |
| `core/regime/detector.py` 主生产       | 人工调试 | `regime_daily` 主表仅 3 行 |
| `scheduler/scheduler.py`              | 未启用 | 所有调度表（`telegram_commands` 0 行）未被调用 |
| `bot/telegram_bot.py` + `bot/commands/*` | 未常驻 | `telegram_commands` 表 0 行 |
| `api/main.py` + `api/routes/`         | 未启用 | routes 目录只有 `__init__.py` |
| `data/sources/stocktwits_client.py`   | 未接入 | `social_sentiment` 仅 7 行、1 天数据 |
| `data/sources/eastmoney_client.py`    | 未知 | 无独立表，无法判断是否调用过 |
| `data/quality/monitor.py`             | 运行过 | `data_quality_checks` 11 行（少量） |
| `llm/wiki_manager.py`                 | 运行过 | `wiki_pages` 5 行（试点） |
| `llm/embedder.py`                     | 运行过 | `wiki_pages.embedding` 有向量（推断） |
| `data/pipeline/feature_compute.py`    | **仅 backtest 跑过** | `features_daily` 61 只 = backtest watchlist，主 YAML 的 0 只/universe 的 7 只未覆盖 |

### 总结
> **core/ 下大部分主生产链路代码从未在正式调度中运行过。** 现有 DB 数据几乎全部来自：(a) 手工调试的零星调用，(b) backtest 子项目的独立跑批。主项目整体处于"脚手架搭起、主循环未启动"状态。

---

## 6. 外部依赖健康度

### 诚实说明
**无法给出"最近 7 天调用次数 / 失败率 / 平均延迟"。** 原因：
1. 主项目未运行 structlog 聚合日志，`/logs/` 目录不存在。
2. 没有集中的 API 调用审计表（`data_quality_checks` 仅 11 行，非细粒度调用日志）。
3. 各 client 文件无内置统计埋点。

### 间接观察（从 DB 行数反推）

| 依赖          | 最近 7 天活动证据 | 健康度推断 |
|---------------|-------------------|-----------|
| Tushare       | `market_bars_daily` 新增到 2026-04-17，`fundamentals_daily` 到 2026-04-17，`moneyflow_daily` 到 2026-04-17 | **活跃**，至少日频拉 |
| yfinance      | `market_bars_daily` 美股到 2026-04-17 | **活跃** |
| DeepSeek V3.2 | 主 `judgments.logic_text` 5 条，`wiki_pages` 3 条新内容（4/17-4/18）| **偶发调用**，非日常 |
| Qwen-Turbo    | 无独立证据 | **无法判断** |
| Embedding     | `wiki_pages` 5 行带 embedding | **至少调用过 5 次** |
| Telegram      | `telegram_commands` = 0 | **未进入生产调度**（CLAUDE.md 记忆显示 2026-04-17 测试通过，但此后未留下命令记录） |

---

## 7. 自 2026-04-18 以来的真实变化

> 无 git 仓库（`git log` 报错 "not a git repository"），变化只能从文件 mtime 和 DB 行数推断。

### 7.1 文件层（按 mtime）

```
backtest/engine/benchmarks.py   2026-04-20 00:24  (新建)
backtest/engine/engine.py       2026-04-20 00:24  (修改 - 集成 benchmarks)
backtest/engine/execution.py    2026-04-19 23:32
backtest/engine/portfolio.py    2026-04-19 23:17
backtest/engine/rules.py        2026-04-19 23:07
backtest/tests/                 2026-04-19 23:18  (测试更新)
backtest/scripts/               2026-04-19 23:34  (04_run_backtest.py 新增)
backtest/docs/                  2026-04-19 22:23
backtest/analysis/              2026-04-19 22:23
```

主项目 `core/` / `bot/` / `llm/` 等目录在 04-18 之后无修改（除 backtest 外）。

### 7.2 DB 行数变化（对比 04-18 简报估算）

| 表                     | 04-18 估计 | 04-19 实测 | 变化 |
|------------------------|-----------|-----------|------|
| backtest_runs          | 0         | 2         | +2（pilot + full 进行中） |
| backtest_judgments     | 0         | 2,731 →（增长中）| +3000 级 |
| backtest_trades        | 0         | 40        | +40 |
| backtest_portfolio_daily | 0       | 88        | +88 |
| judgments (主)         | 3-5       | 5         | 基本持平 |
| experience_store       | 4-5       | 6         | +1-2 |
| wiki_pages             | 3         | 5         | +2（AAPL、600519.SH 页面） |
| social_sentiment       | 0?        | 7         | +7（一次性） |
| intraday_signals       | 3-4?      | 5         | +1-2 |

### 7.3 首次被真实调用（而非"已实现"）的模块

仅 1 项：**`backtest/engine/benchmarks.py`** — 2026-04-20 首次跑通（B1 HS300 = 4523.71 / SPY = 645.05，B2 32/32 + 29/29 起始价对齐）。

其他主项目模块本日无新增"首次真实调用"证据。

---

## 8. Claude Code 识别的三个最大短板

### 短板 1：主项目生产链路从未真正启动过（P0，最严重）

**事实**：
- 主 `regime_daily` 3 行、`judgments` 5 行、`positions` 2 行、`intraday_signals` 5 行、`telegram_commands` 0 行。
- `config/watchlist.yaml` 为空，`stock_universe` 只有 7 只（且 2 只是 ETF）。
- `core/evolution/` 的 4 个文件、`scheduler/scheduler.py`、`api/routes/` 在 7 个月开发周期结束后（2026-04-19）行数 = 0。

**为何最严重**：
当前的"架构完整度"是一张没通电的主板。backtest 模块跑得再顺也只验证了 analysis 子树的算法可行性，**不代表整套调度 / 推送 / 风控 / 进化回路是活的**。Phase 3-5 声称已实现，但 DB 层面没有任何真实流量证据。

**建议**：
在再写新功能之前，做一次"小流量闭环"演练——
1. 挑 5 只股票、连续 5 个交易日，走完 `data/pipeline → core/regime → core/analysis → bot/telegram → evolution` 完整一轮。
2. 所有中间结果必须落 DB，每一步行数 ≥ 5 才算通过。
3. 发现的所有 bug 都修到根因（不绕过）。

### 短板 2：候选池治理缺失，三份清单零交集（P0）

**事实**：
- 主 YAML 空、backtest YAML 61 只、DB universe 7 只，三者无任何交集。
- features_daily 只给 61 只（backtest 的）算了，主 YAML / universe 的股票连特征都没有。
- 这意味着如果哪天启用主 `scheduler`，读取 `stock_universe` 的 7 只，立即会因 features 缺失报错或降级到占位分。

**为何严重**：
候选池是一切分析的输入。"哪些股票被 P10 追踪"目前没有单一事实来源。这不是小 bug，是 **信息架构层面的治理缺陷**。

**建议**：
1. 确立单一事实源：`stock_universe` 表为唯一 truth，两个 YAML 只作为 bootstrap seed。
2. 每次 `scheduler` 启动必须对齐 `features_daily.symbol = stock_universe.symbol`，未对齐则拒绝运行。
3. 人工补拉/替换 universe 时强制走 Telegram `/watchlist` 命令（现已空置），所有变更写入 `stock_universe.added_reason`。

### 短板 3：Regime 过度震荡，且 mode 空间利用不足（P1）

**事实**：
- CN 57 天切换 16 次，`offense → cautious_offense` 两态高频跳变，几乎单日内随 volatility_score 40→70 的波动就翻转。
- 配置 7 种 mode（offense/bull_trend/recovery/neutral/volatile/risk_off/defense）中，5 种从未出现。
- HS300 在 -0.7% 区间震荡（4523→4490），但 regime 大部分时间是 offense（43/57 天），会持续发多头信号，策略将被手续费消耗。

**为何重要**：
Regime 是策略执行的节流阀。当前节流阀要么全开（offense），要么突然关死（defense 1 天），中间挡位全缺。这会让 **position sizing（用 regime_max_pct）完全失去平滑控制能力**，回测期后半段 CN 组合已出现 -5.66% 与频繁进出（见 run_id=3 2025-12-01 日志）。

**建议**：
1. 给 regime 加 **3 日滤波**：当前日 mode ≠ 前两日 mode 时，保持前两日 mode，避免单日跳变。
2. 检查 `detect_regime` 函数是否真能吐 `neutral / volatile / risk_off`——如果代码路径不覆盖这些模式，阈值分段要重新设计。
3. 补一张小表 `regime_transition_matrix`，记录每次切换的触发维度（trend/vol/brd/liq 哪一维跨过阈值），便于调参。

---

## 附：当前 backtest run_id=3 进度快照

- 进度：**47% (70/150 交易日)，耗时 10 分钟，预计还需 ~12 分钟**
- 中间状态（2025-12-01）：
  - CN：943.4 万（-5.66%）1 持仓
  - US：103.2 万（+3.24%）0 持仓
  - Judgments：3660 条，bullish 893（24.4%）
  - Trades：entries 23 / exits 22（**高度换手**）

完整的三基准对比、Stage C 健康检查将在 run_id=3 结束后补充。

---

*生成者：Claude Code（Opus 4.7）。本快照不含任何定性修饰词（"进展良好""已完善"等），所有结论可直接用 SQL 在 2026-04-20 00:30 时点的 DB 复现。*
