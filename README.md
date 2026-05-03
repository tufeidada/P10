# P10-AlphaRadar

A股 + 美股多维投研分析系统：候选池管理、多维分析（技术面/基本面/资金面/情绪面）、综合评分与信号生成、判断追踪与自我进化。

> 当前状态：Phase 0–4 完成（CN 约 26 只 + US 约 22 只候选池），处于 M5 观察期，调优 composite 评分与信号强度阈值。

---

## 核心能力

- **候选池管理**：自选为主（70–80%）+ 系统推荐补充（20–30%）
- **多维分析**：技术面 / 基本面 / 资金面 / 情绪面四维并行打分
- **Regime 自适应**：根据市场状态（offense / cautious_offense / defense / risk_off）动态调整维度权重
- **综合判断**：composite_score（0–100）+ 7 档信号强度（strong_buy → strong_sell）
- **判断追踪**：所有 judgment 入库，自动回填表现并复盘
- **Telegram 推送**：每日报告（CN 16:30 / US 07:00 BJT），分多方/中性/空方三组
- **LLM Wiki**：DeepSeek V3 / Qwen3-Mini 沉淀投研经验，可检索

**不做的事**：自动交易执行 / 高频日内 / LLM 选股发散推荐 / 月线长期方向判断。

---

## 技术栈

- **语言**：Python 3.11+
- **数据库**：PostgreSQL 15 + TimescaleDB + pgvector（host 端口 **5433**）
- **数据源**：Tushare（A股）、yfinance（美股）、StockTwits（情绪）
- **LLM**：DeepSeek V3（主力分析）、Qwen3-Mini（轻量任务）、text-embedding-v4
- **Web**：FastAPI + uvicorn
- **调度**：APScheduler AsyncIOScheduler
- **Bot**：python-telegram-bot v20+
- **DB 访问**：asyncpg + raw SQL（无 ORM）

---

## 快速开始

### 1. 准备环境

```bash
# 克隆并进入项目（注意目录末尾有空格）
cd "/Users/yangxuan/PycharmProjects/P10-AlphaRadar "

# Python 虚拟环境
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 环境变量
cp env.template .env
# 编辑 .env，填入 TUSHARE_TOKEN / TELEGRAM_BOT_TOKEN / DEEPSEEK_API_KEY 等
```

### 2. 启动数据库（Docker）

```bash
docker compose up -d db
until docker exec alpharadar-db pg_isready -U radar -d alpharadar; do sleep 2; done
```

### 3. 启动 Scheduler 与 Bot

```bash
set -a && source .env && set +a

# 调度器自检（不启动，验证启动条件）
python scripts/start_scheduler.py --dry-run

# 后台启动
nohup python scripts/start_scheduler.py >> logs/scheduler.log 2>&1 &
nohup python scripts/start_bot.py       >> logs/bot.log       2>&1 &
```

---

## 每日数据流水线

时间均为北京时间。

| 时间 | Job | 说明 |
|------|-----|------|
| 15:15 | `pull_cn_market_data` | Tushare 拉 A 股日 K + 资金流 |
| 15:30 | `update_features_daily_cn` | 计算技术指标 |
| 15:40 | `detect_regime_cn` | 判定 regime |
| 16:00 | `run_composite_analysis_cn` | 综合分析，写 `judgments` 表 |
| 16:30 | `send_daily_digest_cn` | Telegram HTML 日报 |
| 16:35 | `composite_snapshot` | 写 `reports/composite_snapshot_YYYYMMDD.csv` |
| 05:30 BJT 次日 | `pull_us_market_data` | yfinance 拉美股 |
| 05:45 | `update_features_daily_us` | 美股技术指标 |
| 06:00 | `detect_regime_us` | 美股 regime |
| 06:30 | `run_composite_analysis_us` | 美股综合分析 |
| 07:00 | `send_daily_digest_us` | 美股日报 |
| 07:05 | `composite_snapshot` | 美股快照 |

手动触发任意 job：

```bash
python scripts/start_scheduler.py run_job <job_name>
```

---

## 综合评分架构

详见 [`core/analysis/composite.py`](core/analysis/composite.py)。

**方向判定**：
- `composite_score >= 65` → **bullish**
- `composite_score <= 40` → **bearish**
- 否则 → **neutral**

**维度权重**（随 regime 变化，配置见 [`config/regime_params.yaml`](config/regime_params.yaml)）：

| Regime | tech | fund | flow | sentiment |
|--------|------|------|------|-----------|
| offense | 0.30 | 0.25 | 0.25 | 0.20 |
| cautious_offense | 0.25 | 0.30 | 0.25 | 0.20 |
| defense | 0.20 | 0.35 | 0.25 | 0.20 |
| risk_off | 0.20 | 0.40 | 0.25 | 0.15 |

**置信度**：

```
agree_ratio = count(维度 > 55 或 < 45 且方向一致) / N
confidence  = agree_ratio * 0.7 + distance * 0.3
```

`social_sentiment` 表无个股数据时（当前常态），只用 tech/fund/flow 三维计算 agree_ratio，避免 market-wide Fear & Greed 污染 confidence。

**信号强度（7 档）**：

| 距离 + 置信度 | 信号 |
|---------------|------|
| `distance > 12` & `conf > 0.65` | strong_buy / strong_sell |
| `distance > 12` & `conf > 0.40` | buy / sell |
| `distance > 8`  & `conf > 0.25` | buy / sell |
| `distance > 4`  & `conf > 0.15` | weak_buy / weak_sell |
| 否则 | hold |

---

## 目录结构

```
P10-AlphaRadar/
├── README.md                       # 本文件
├── CLAUDE.md                       # Claude Code 工作指南
├── docs/                           # 详细文档
│   ├── P10-AlphaRadar-Architecture.md
│   ├── reboot_recovery_checklist.md
│   └── phase*_completion.md
├── config/
│   ├── settings.yaml
│   ├── watchlist.yaml              # 候选池
│   ├── regime_params.yaml          # 维度权重
│   └── industry_frameworks.yaml
├── core/                           # 核心业务
│   ├── regime/                     # 市场状态判定
│   ├── analysis/                   # 四维分析 + composite
│   ├── intraday/                   # 盘中信号（Phase 3）
│   ├── scanner/
│   ├── risk/
│   └── evolution/                  # 自我进化（Phase 5）
├── data/                           # 采集与管道
│   ├── sources/                    # tushare / yfinance / stocktwits
│   ├── pipeline/
│   └── quality/
├── db/                             # asyncpg + raw SQL
│   ├── connection.py
│   ├── schema.sql
│   └── migrations/
├── llm/                            # DeepSeek / Qwen 客户端
├── bot/                            # Telegram bot
├── api/                            # FastAPI
├── scheduler/                      # APScheduler
├── wiki/                           # 个股知识库（自动维护）
│   ├── stocks/CN/
│   └── stocks/US/
├── scripts/                        # 一次性 / 手动脚本
├── backtest/                       # ⚠️ 独立分析实现，禁止与 core/analysis 互相引用
├── tests/
└── docker-compose.yml
```

---

## 关键约束

- **Backtest 禁区**：[`backtest/`](backtest/) 下有独立的分析实现（`backtest/analysis/composite.py` 等），与 [`core/analysis/`](core/analysis/) **相互独立**，禁止互相引用或同步修改。
- **LLM 不阻塞主流程**：composite 分析中 LLM 调用失败时降级为纯规则判断，`llm_direction` / `llm_signal_strength` 留 NULL，不抛异常。
- **数据拉取超时**：统一 900 秒，超时后写入 `data_source_expectations.last_error` 状态，不崩溃。
- **每日推送**：日报全量推送所有股票，按 `rule_signal_strength` 分多方/中性/空方三组；多方内按 strong_buy → buy → weak_buy 排序。
- **不变量断言**：使用 [`core/invariants.py`](core/invariants.py) 的 `assert_in` / `assert_range` / `assert_fresh`，失败立即抛 `InvariantViolation`，不 silent fallback。

---

## 启动门禁（M3 / M5）

[`scheduler/scheduler.py`](scheduler/scheduler.py) 在 `_startup_checks()` 里执行两道门禁：

- **M3**：`features_daily` 最新日期距今 ≤ 3 天
- **M5**：`data_source_expectations` 表中 severity=`critical` 的数据源不超过 `max_lag_days` 滞后

M5 失败会阻塞启动。临时解法：把问题数据源 severity 改为 `warn`，问题修复后改回 `critical`。

---

## 常用命令

```bash
# 手动跑一次 composite 分析（不经 scheduler）
python scripts/run_composite_once.py --symbol 600519.SH --market CN
python scripts/run_composite_once.py --all-active --market CN
python scripts/run_composite_once.py --all-active --market CN --dry-run    # 跳过 LLM
python scripts/run_composite_once.py --all-active --date 2026-04-23

# 诊断 features 覆盖
python scripts/diagnose_feature_coverage.py

# 语法自检（改完核心文件后）
python3 -c "import ast; ast.parse(open('core/analysis/composite.py').read())"

# 回归测试
cd backtest && pytest tests/ -q

# 看日志
tail -f logs/scheduler.log | grep -E "job_success|job_failed|ERROR"
```

---

## 文档导航

- 完整架构：[docs/P10-AlphaRadar-Architecture.md](docs/P10-AlphaRadar-Architecture.md)
- 重启恢复清单：[docs/reboot_recovery_checklist.md](docs/reboot_recovery_checklist.md)
- Phase 1 收尾：[docs/phase1_completion.md](docs/phase1_completion.md)
- 状态快照：[docs/P10-Status-Snapshot-20260419.md](docs/P10-Status-Snapshot-20260419.md)
- Claude Code 指南：[CLAUDE.md](CLAUDE.md)

---

## 许可证

私有项目，未公开授权。
