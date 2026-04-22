# Backtest Module - 严格 PIT 规范

## 定位
P10-AlphaRadar 下的回测子模块。目标：在完整 P10 开发前（还需 12-14 周），
用 4 周时间验证核心分析逻辑在 2025-09 ~ 2026-04 数据上的有效性。

## ⚠️ 代码隔离铁律

本模块处于 P10 主项目内部，共用数据库。因此必须靠代码约定保证回测严谨性：

1. **禁止 import P10 主项目的 core/ 模块**
   - backtest/analysis/ 下的分析模块必须独立重写
   - 可以参考 P10 的 core/analysis/ 实现作为业务逻辑蓝图
   - 但所有数据访问必须改为 PIT 模式（通过 pit_loader.py）

2. **禁止直接 SQL 查询业务数据**
   - 所有业务数据访问必须通过 backtest/pit_loader.py
   - 唯一例外：backtest/scripts/ 下的 DDL 和 ETL 脚本

3. **禁止在 features_daily 等共享表中暴露未来字段**
   - future_ret_* 字段只存在 backtest_features_extra 表
   - 不要污染主 features_daily（它是 P10 主项目共用的）

## 数据库
- 复用 P10 的 alpharadar 数据库（localhost:5433）
- 现有表扩展：加 available_date 字段
- 新建 backtest_ 前缀表：backtest_runs / backtest_judgments / backtest_trades /
  backtest_portfolio_daily / backtest_positions / backtest_regime_daily /
  backtest_features_extra
- 新建通用表：index_daily / market_breadth_daily（P10 主项目后续也需要）
- 所有表在 public schema（不建独立 schema）

## PIT 约束（复述 spec）

PIT（Point-in-Time）是回测的生死线。任何违反都会让结果虚高。

1. T 日的判断只能用 T-1 及之前数据（日线、features、资金流、基本面）
2. 财报必须用 announce_date 过滤，不是 report_date
3. 成交价必须用 T+1 开盘价（通过 pit_loader.get_open_price）
4. 各表 available_date 填充规则：
   - market_bars_daily：available_date = trade_date
   - fundamentals_daily：available_date = trade_date
   - features_daily：available_date = trade_date
   - financials_quarterly：available_date = announce_date（NULL 则 report_date + 45天）
   - moneyflow_daily：available_date = 下一个交易日（T+1）
   - northbound_daily：available_date = trade_date

**代码审查时必查：**
- 任何直接查 market_bars_daily 不走 PITDataLoader 的代码 = 高危
- 任何使用 future_ret_* 字段的代码 = 必须在 backtest_features_extra 的 ETL 内
- 任何 `ORDER BY trade_date DESC LIMIT 1` 没有 `available_date <=` 过滤 = 高危

**架构合理性原则（2026-04-19 新增）：**
审查不只核实数字，也必须核实架构的合理性。
具体规则：看到"N/A / 数据不足"类结论时，不能直接标注 acceptable，
必须先追问"我们明明有 N 年数据，为什么只拿到这么少？"
典型案例：Weekly 65 周 → MA30 无法判 Stage，根因是 lookback=300 不够，
不是数据本身不足。发现此类架构问题立即修复，不绕过。

## 目录结构

```
backtest/
├── CLAUDE.md                   # 本文件
├── pit_loader.py               # ⚠️ 核心：所有业务数据访问入口
├── analysis/                   # PIT 版分析模块（不 import core/）
│   ├── regime.py
│   ├── technical.py
│   ├── fundamental.py
│   ├── flow.py
│   ├── sentiment.py
│   ├── stage_detector.py
│   └── composite.py
├── engine/                     # 回测引擎
│   ├── engine.py
│   ├── portfolio.py
│   ├── execution.py
│   ├── benchmarks.py
│   └── rules.py
├── evaluation/                 # 评估与报告
│   ├── metrics.py
│   ├── attribution.py
│   └── reporter.py
├── scripts/                    # ETL 和运行脚本（唯一可直接写 SQL 的地方）
│   ├── 01_migrate_schema.py    # Schema 扩展（幂等）
│   ├── 02_fetch_missing_data.py
│   ├── 03_compute_features.py
│   ├── 04_run_backtest.py
│   └── 05_generate_report.py
├── config/
│   ├── watchlist.yaml          # 61 只候选股（32 CN + 29 US）
│   ├── settings.yaml
│   ├── regime_params.yaml
│   └── industry_frameworks.yaml
├── docs/
│   └── P10-Backtest-Spec.md
└── tests/                      # PITDataLoader 单元测试必须覆盖
```

## 开发顺序（4 周）

**Week 1: 数据层**
1. scripts/01_migrate_schema.py（Schema 扩展 + available_date 回填）
2. pit_loader.py（核心，必须有单元测试）
3. scripts/02_fetch_missing_data.py（Tushare 补拉 CN 财报/资金流/指数；yfinance 补 US 日线）
4. scripts/03_compute_features.py（features_daily 批量计算；backtest_features_extra 未来收益回填）

**Week 2: 分析模块**
- regime.py、technical.py、fundamental.py、flow.py、sentiment.py、composite.py
- 参考 P10 core/analysis/ 的业务逻辑，但所有数据访问改走 pit_loader

**Week 3: 回测引擎**
- engine.py 主循环、portfolio.py 虚拟账户、execution.py T+1 成交、benchmarks.py 三对照组、rules.py 建平仓规则

**Week 4: 评估报告**
- metrics.py（IC/IR/Sharpe/Sortino/MDD）、attribution.py、reporter.py（Markdown + Excel + 图表）

## 不做什么
- 不做 LLM 集成
- 不做 Telegram
- 不做 Wiki
- 不做前端看板
- 不做盘中 15 分钟信号
- 不做社交情绪（StockTwits/股吧历史数据不可靠）
- 不过度优化参数（容易过拟合）

## 遇到问题时
- 数据拉取失败：重试 3 次，失败则记录并跳过，不崩溃
- PIT 检查发现 look-ahead：立即停止开发，修复后再继续，不放过
- 回测结果明显异常（alpha > 20% 或 < -20%）：先假设有 bug，逐一排查 PIT / 成交价 / 佣金 / 复权
