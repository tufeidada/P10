# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> **协作规范**：本项目的日志/计划/周报体系遵循 [`~/PycharmProjects/AI_PLAYBOOK.md`](../AI_PLAYBOOK.md)。任何会话开始时，请先读该文件了解 PLAN.md / DAILY_LOG.md 的格式约定与用户提示词响应规则。

## 项目概述

A股+美股多维投研分析系统（CN 约 26 只 + US 约 22 只候选池）。已完整实现 Phase 0–4（技术面/基本面/资金面/情绪面 + 美股），目前处于 M5 观察期——调优 composite 评分与信号强度阈值。

完整技术架构见 `docs/P10-AlphaRadar-Architecture.md`。

---

## 基础设施

**数据库**：TimescaleDB（PG16）运行在 Docker 容器，**host 端口 5434**（容器内 5432）。volume `pgdata` 是 external，docker-compose 重建时必须保留。

**代理**：`.env` 配 `HTTP_PROXY=http://127.0.0.1:4780` + `NO_PROXY` 排除 Tushare/Ark/Dashscope；Telegram 走代理。

```bash
# 重启后必须先拉起 DB（docker-compose.yml 中 service 名为 db）
docker compose up -d db
until docker exec alpharadar-db pg_isready -U radar -d alpharadar; do sleep 2; done

# 启动 Scheduler（后台）
cd "/Users/yangxuan/PycharmProjects/P10-AlphaRadar "
set -a && source .env && set +a
nohup python scripts/start_scheduler.py >> logs/scheduler.log 2>&1 &

# 启动 Telegram Bot（独立进程）
nohup python scripts/start_bot.py >> logs/bot.log 2>&1 &
```

⚠️ **路径末尾有空格**：项目目录是 `/Users/yangxuan/PycharmProjects/P10-AlphaRadar `（带空格），引用时需引号。

---

## 常用操作命令

```bash
# 调度器自检（不启动，只验证启动条件）
python scripts/start_scheduler.py --dry-run

# 手动触发单个 job（绕过 startup_checks，安全用于手动补跑）
python scripts/start_scheduler.py run_job <job_name>
# 可用 job: pull_cn_market_data, update_features_daily_cn, detect_regime_cn,
#           run_composite_analysis_cn, send_daily_digest_cn,
#           pull_us_market_data, update_features_daily_us, detect_regime_us,
#           run_composite_analysis_us, send_daily_digest_us, backfill_judgments

# 手动跑 composite 分析（不经 scheduler）
python scripts/run_composite_once.py --symbol 600519.SH --market CN
python scripts/run_composite_once.py --all-active --market CN
python scripts/run_composite_once.py --all-active --market CN --dry-run    # 跳过 LLM
python scripts/run_composite_once.py --all-active --date 2026-04-23        # 指定日期

# 诊断 features 覆盖
python scripts/diagnose_feature_coverage.py

# 语法自检（改完核心文件后）
python3 -c "import ast; ast.parse(open('core/analysis/composite.py').read())"
python3 -c "import ast; ast.parse(open('core/analysis/sentiment.py').read())"
python3 -c "import ast; ast.parse(open('bot/commands/daily.py').read())"

# 回归测试
cd backtest && pytest tests/ -q

# 查 scheduler 日志
tail -f logs/scheduler.log | grep -E "job_success|job_failed|ERROR"
```

---

## 核心数据流水线

每个交易日（以北京时间为准）：

```
CN 链路（15:15–16:35）
  pull_cn_market_data (15:15)
    └─ TushareClient → market_bars_daily, features_daily, fund_flow_daily
  update_features_daily_cn (15:30)
    └─ 计算技术指标，写入 features_daily
  detect_regime_cn (15:40)
    └─ core/regime/ → regime_daily（offense/cautious_offense/defense/risk_off）
  run_composite_analysis_cn (16:00)
    └─ CompositeAnalyzer.analyze() → judgments 表
  send_daily_digest_cn (16:30)
    └─ DailyPusher → Telegram HTML 报告
  composite_snapshot (16:35)
    └─ 写入 reports/composite_snapshot_YYYYMMDD.csv（3 天观察期数据）

US 链路（05:30–07:05 BJT 次日）
  pull_us_market_data (05:30) → YFinanceClient
  update_features_daily_us (05:45)
  detect_regime_us (06:00)
  run_composite_analysis_us (06:30)
  send_daily_digest_us (07:00)
  composite_snapshot (07:05)
```

---

## 综合评分架构（core/analysis/composite.py）

**方向判定**：`composite_score >= 65` → bullish，`<= 40` → bearish，否则 neutral。

**维度权重**（随 regime 变化，见 `config/regime_params.yaml`）：

| Regime | tech | fund | flow | sentiment |
|--------|------|------|------|-----------|
| offense | 0.30 | 0.25 | 0.25 | 0.20 |
| cautious_offense | 0.25 | 0.30 | 0.25 | 0.20 |
| defense | 0.20 | 0.35 | 0.25 | 0.20 |
| risk_off | 0.20 | 0.40 | 0.25 | 0.15 |

**置信度公式**（`_compute_confidence`）：

```
agree_ratio = count(维度 > 55 或 < 45 且方向一致) / N
confidence = agree_ratio * 0.7 + distance * 0.3
```

`has_social=False`（当前常态，`social_sentiment` 表无个股数据）时只用 tech/fund/flow **3 维**计算 agree_ratio，避免 market-wide Fear & Greed 广播污染 confidence。

**信号强度（7 档，`_compute_rule_signal_strength`）**：

```python
distance = composite_score - 50.0
distance > 12 and conf > 0.65  → strong_buy
distance > 12 and conf > 0.40  → buy
distance >  8 and conf > 0.25  → buy
distance >  4 and conf > 0.15  → weak_buy
# 镜像对称为空方
else                           → hold
```

---

## 数据库访问层（db/connection.py）

所有数据库操作通过模块级函数，不直接使用连接池对象：

```python
from db.connection import db_query, db_query_one, db_query_val, db_execute, init_pool, close_pool

rows = await db_query("SELECT * FROM judgments WHERE symbol = $1", symbol)
row  = await db_query_one("SELECT ...", arg)
val  = await db_query_val("SELECT COUNT(*) FROM ...", )
await db_execute("UPDATE ... SET x = $1 WHERE id = $2", val, id_)
```

脚本入口须显式初始化/关闭连接池；scheduler/bot 在启动时统一完成。

---

## 启动检查机制

`scheduler.py` 在 `_startup_checks()` 中执行两道门禁：

- **M3**：`features_daily` 最新日期距今 ≤ 3 天（assert_fresh）
- **M5**：`data_source_expectations` 表中 severity=`critical` 的数据源不超过 `max_lag_days` 滞后

M5 失败会阻塞启动。临时解法：把问题数据源的 severity 改为 `warn`，问题修复后改回 `critical`。

---

## 关键约束

**Backtest 禁区**：`backtest/` 目录下有独立的分析实现（`backtest/analysis/composite.py` 等），与 `core/analysis/` **相互独立，禁止互相引用或同步修改**。回测代码改动须单独评估，不能随生产端一起改。

**LLM 不阻塞主流程**：composite 分析中 LLM 调用失败时降级为纯规则判断，`llm_direction`/`llm_signal_strength` 字段留 NULL，不抛异常。

**数据拉取超时**：统一 900 秒，超时后标注数据状态（`data_source_expectations.last_error`），不崩溃。

**每日推送**：日报全量推送所有股票，按 `rule_signal_strength` 分 3 组（多方/中性/空方），多方内按 strong_buy → buy → weak_buy 强度排序。

---

## 开发规范

- 异步优先：DB 操作、API 调用、Telegram 交互全部用 `async/await`
- 日志：`structlog` JSON 格式，字段包含 `symbol`/`market`/`event`
- 不变量断言：用 `core/invariants.py` 中的 `assert_in` / `assert_range` / `assert_fresh`，失败立即抛 `InvariantViolation`，不 silent fallback
- 技术栈：Python 3.11+，asyncpg raw SQL（无 ORM），APScheduler AsyncIOScheduler，OpenAI-compatible SDK 调用 DeepSeek/Qwen
