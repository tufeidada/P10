# P10-AlphaRadar 使用手册

> 写给：自己（轩老板）。每次开机/重启后照着走一遍。
> 项目目录：`/Users/yangxuan/PycharmProjects/P10-AlphaRadar ` ⚠️ **末尾有空格**

---

## 0. 心智模型

**P10 是个股深度研究助手**，不是自动交易系统。

```
TushareClient/yfinance → 数据库（TimescaleDB）
                          ↓
              四维分析（tech/fund/flow/sentiment）
                          ↓
              CompositeAnalyzer（含 LLM 投票）
                          ↓
              判断写入 judgments 表
                          ↓
        Telegram 日报  +  Web 前端  +  Wiki 知识库
```

每天产出：
- CN 候选池每只一条 judgment（含 7 档信号：strong_buy → strong_sell）
- US 候选池每只一条 judgment
- 一份 Telegram HTML 日报
- DAILY_LOG.md 自动追加一段 YAML

---

## 1. 开机重启后启动顺序

```bash
export PROJECT_DIR="/Users/yangxuan/PycharmProjects/P10-AlphaRadar "
cd "$PROJECT_DIR"
```

### 1.1 启 Clash Verge（必须）

代理在 `127.0.0.1:7897`，没起来 Tushare/yfinance/Telegram/GitHub 全挂。

```bash
# 验证
lsof -nP -iTCP:7897 -sTCP:LISTEN | head
curl -sx http://127.0.0.1:7897 -o /dev/null -w "%{http_code}\n" --max-time 5 https://github.com
# 期望: 200
```

### 1.2 启数据库（Docker）

```bash
open -a Docker     # 等到状态栏鲸鱼图标稳定
docker compose up -d db
until docker exec alpharadar-db pg_isready -U radar -d alpharadar >/dev/null 2>&1; do sleep 2; done && echo "✅ DB ready"
```

### 1.3 启 Scheduler + Bot + API + 前端

```bash
set -a && source .env && set +a

# Scheduler（19 job 自动调度）
nohup /Users/yangxuan/miniconda3/bin/python scripts/start_scheduler.py >> logs/scheduler.log 2>&1 &

# Telegram Bot
nohup /Users/yangxuan/miniconda3/bin/python scripts/start_bot.py >> logs/bot.log 2>&1 &

# FastAPI 后端（前端依赖）
nohup /Users/yangxuan/miniconda3/bin/python -m uvicorn api.main:app --host 0.0.0.0 --port 8000 >> logs/api.log 2>&1 &

# Vite 前端
cd frontend && nohup npm run dev >> ../logs/frontend.log 2>&1 &
cd ..
```

验证：

```bash
# 4 个守护进程
ps -ef | grep -E "start_scheduler|start_bot|uvicorn|vite" | grep -v grep | awk '{print $2, $11}'

# API + 前端
curl -s http://localhost:8000/api/health | head -c 200
curl -s -o /dev/null -w "front=%{http_code}\n" http://localhost:5173
```

打开浏览器：**http://localhost:5173**

---

## 2. 日常使用场景

### 场景 A：早上看昨天的信号

- **打开 Telegram，找 `P10_leida_bot`**：每天 16:30（CN）+ 07:00（US）会自动推日报
- **或打开前端**：http://localhost:5173 → 候选池表格，按 composite_score 降序
- **或读 DAILY_LOG.md**：自动追加 yaml 段，看每天 strong_buy/buy/sell 分组

### 场景 B：临时研究某只股票

```bash
cd "$PROJECT_DIR"
set -a && source .env && set +a

# 单股分析（含 LLM 多维度叙事，~30 秒）
python scripts/run_composite_once.py --symbol 600519.SH --market CN

# 跳过 LLM 调用（仅规则评分，秒级）
python scripts/run_composite_once.py --symbol 600519.SH --market CN --dry-run

# 指定历史日期分析
python scripts/run_composite_once.py --symbol 600519.SH --market CN --date 2026-05-20
```

输出在终端 + 写入 judgments 表 + 同步更新 `wiki/stocks/<MARKET>/<SYMBOL>.md`。

### 场景 C：补漏一天数据

scheduler 滚动只追 3 天。中断超过 3 天时用：

```bash
python scripts/backfill_range.py --start 2026-05-12 --end 2026-05-25
# 自动跑：market_bars + features + regime 全链路
```

只补 CN 或 US：

```bash
python scripts/backfill_range.py --start 2026-05-12 --end 2026-05-25 --no-us
python scripts/backfill_range.py --start 2026-05-12 --end 2026-05-25 --no-cn
```

补完后 composite 仍需单独跑：

```bash
python scripts/run_composite_once.py --all-active --market CN --date 2026-05-12
```

### 场景 D：加新股到候选池

1. 编辑 `inputs/watchlist_<name>.csv`（参考已有 `watchlist_seed.csv` / `watchlist_ai_semi.csv`）
2. `python scripts/load_watchlist.py --csv inputs/your_file.csv`
3. 拉历史 bars（首次入池需要 ≥150 个交易日）：

```python
# 临时脚本
from data.sources.tushare_client import TushareClient   # CN
from data.pipeline.us_data_pull import USDataPuller     # US
```
或者直接等 scheduler 在 5/27 15:15（CN）/ 05:30（US）的 bootstrap 自动拉 30 天（但 features 算的不够稳）。
4. 算 features + 跑 composite，参考场景 B。

### 场景 E：手动触发 scheduler 单个 job

```bash
# CN 链路
python scripts/start_scheduler.py run_job pull_cn_market_data
python scripts/start_scheduler.py run_job update_features_daily_cn
python scripts/start_scheduler.py run_job detect_regime_cn
python scripts/start_scheduler.py run_job run_composite_analysis_cn
python scripts/start_scheduler.py run_job send_daily_digest_cn
python scripts/start_scheduler.py run_job composite_snapshot_cn

# US 链路（同样命名）
python scripts/start_scheduler.py run_job pull_us_market_data
# ...
```

### 场景 F：复盘命中率

```bash
# 周复盘（自动每周一 10:00 跑，也可手动）
python scripts/start_scheduler.py run_job weekly_review

# 月复盘（每月 1 号 10:00）
python scripts/start_scheduler.py run_job monthly_review

# composite 分布快照（看分布漂移）
python scripts/composite_distribution_snapshot.py
```

### 场景 G：Telegram 命令

`P10_leida_bot` 已实现：

```
/help          # 所有命令
/today         # 今日候选池信号（CN+US 合并）
/research SYM  # 单股深度（计划中，待实现，PLAN.md 标记 7/15 上线）
```

---

## 3. 常见故障

### 数据陈旧 / scheduler 不动

```bash
tail -50 logs/scheduler.log | grep -E "job_success|job_failed|ERROR"
docker exec alpharadar-db psql -U radar -d alpharadar -c "SELECT MAX(trade_date) FROM features_daily;"
```

如果距今 > 3 天：scheduler 启动门禁会失败，需先 `backfill_range.py` 补到最近 3 天，再启 scheduler。

### LLM 调用全失败

```bash
# 测试 LLM 端点（国内 host，需 NO_PROXY 直连）
curl -s -o /dev/null -w "%{http_code}\n" -X POST https://ark.cn-beijing.volces.com/api/v3/chat/completions
# 401 = OK 通了；000 = 网络不通
```

如果走代理后失败，确认 `.env` 里 `NO_PROXY` 包含 `ark.cn-beijing.volces.com`。

### Telegram 不推送 / `httpx.ConnectError`

代理 7897 没起。Clash Verge 没启动或节点死了。

### 前端没数据 / `/api/candidates` 报错

```bash
tail -20 logs/api.log | grep -E "ERROR|Traceback"
curl -s --max-time 5 http://localhost:8000/api/candidates?limit=3
```

### Tushare 失败 `ProxyError`

代理误把 Tushare 路由走代理了。确认 `.env`：

```
HTTP_PROXY=http://127.0.0.1:7897
NO_PROXY=localhost,127.0.0.1,api.waditu.com,ark.cn-beijing.volces.com,dashscope.aliyuncs.com
```

---

## 4. 关键文件 / 配置

| 文件 | 作用 |
|------|------|
| `.env` | API key、DB、代理；**不入 git** |
| `config/watchlist.yaml` | 候选池入口文档（实际数据在 stock_universe 表）|
| `config/regime_params.yaml` | 4 套 regime 维度权重 |
| `inputs/watchlist_seed.csv` | 初始候选池 |
| `inputs/watchlist_ai_semi.csv` | AI/半导体扩展池 |
| `PLAN.md` | 中长期计划（季度 review）|
| `DAILY_LOG.md` | 每日自动记录 + 手工补 1-2 句话 |
| `docs/reboot_recovery_checklist.md` | 重启恢复详细命令 |
| `docs/USAGE.md` | 本文件 |

---

## 5. 不要做的事

- ❌ 不要手改 wiki/* — 是 LLM 自动生成的，下次跑分析会覆盖
- ❌ 不要 push `.env` — 含 API key
- ❌ 不要在 `backtest/` 引用 `core/analysis/` — 这两边是**独立分析体系**（CLAUDE.md 强约束）
- ❌ 不要删 `external` 标记的 `pgdata` volume — 是历史数据
- ❌ 不要在 scheduler 跑 job 中途重启 docker — 等 `job_success` 再重启
