# P10-AlphaRadar 重启恢复操作清单

> 适用场景：Mac 重启、意外断电、系统更新后恢复 P10 全套服务。  
> 目标读者：轩老板自己。所有命令可直接复制粘贴。

---

## 快速变量（每次操作前先执行）

```bash
export PROJECT_DIR="/Users/yangxuan/PycharmProjects/P10-AlphaRadar "
```

> ⚠️ 项目路径末尾有一个空格，这是实际路径，不是笔误。

---

## Part 1：重启前确认（计划性重启时）

### A. 当前是否有 job 正在跑？

```bash
tail -20 "$PROJECT_DIR/logs/scheduler.log" | grep -E "job_start|task_start|job_success|job_failed"
```

**通过标准**：最后一条 `job_success` 或无活跃 `job_start` → 可以重启  
**如果有 job 在跑**：等它出现对应的 `job_success` 再重启

### B. 关键交易时段窗口

| 时段 | 建议 |
|------|------|
| 15:15–16:30 BJT（CN 主链路） | 等 16:30 CN 日报推送完成后重启 |
| 05:30–07:00 BJT（US 主链路） | 等 07:00 US 日报推送完成后重启 |
| 其他时间 | 可随时重启 |

### C. Claude Code 任务

如果正在进行 Claude Code 对话任务，让它完成当前步骤后再重启。

---

## Part 2：启动基础服务

### Step 1：检查 PostgreSQL

```bash
/opt/homebrew/Cellar/postgresql@16/16.13/bin/pg_isready
```

**通过标准**：输出 `/tmp:5432 - accepting connections`  
**失败处理**：

```bash
brew services start postgresql@16
# 等 5 秒后再次检查
/opt/homebrew/Cellar/postgresql@16/16.13/bin/pg_isready
```

如果 brew services 报错，查看详情：
```bash
brew services info postgresql@16
cat ~/Library/Logs/Homebrew/postgresql@16/*.log 2>/dev/null | tail -20
```

---

### Step 2：检查网络 + Telegram 可达

```bash
curl -s -o /dev/null -w "%{http_code}" https://api.telegram.org
```

**通过标准**：输出 `200`  
**失败处理**：
- 检查 Wi-Fi 是否连接
- 检查 VPN（Telegram 在某些网络环境需要代理，.env 中有 `HTTP_PROXY` 配置）
- 如果有代理：`echo $HTTP_PROXY` 确认代理是否生效

---

### Step 3：检查 macOS 休眠设置（DT-011）

**操作**：  
`系统设置 → 电池（Battery）→ 电源适配器（Power Adapter）`  
确认 **"接通电源时防止自动进入睡眠"** 已勾选

**失败处理**：重新勾选该选项（scheduler 心跳中断的根因就是 Mac 睡眠）

---

### Step 4：验证 .env 文件存在

```bash
ls "$PROJECT_DIR/.env" && echo "✅ .env exists" || echo "❌ .env MISSING"
```

**通过标准**：输出 `✅ .env exists`  
**失败处理**：.env 文件丢失 → 根据 env.template 重新填写 API keys

---

### Step 5：验证 Tushare API 可达

```bash
cd "$PROJECT_DIR" && set -a && source .env && set +a && \
python3 -c "
import tushare as ts
ts.set_token('$TUSHARE_TOKEN')
pro = ts.pro_api()
df = pro.daily(ts_code='000001.SZ', start_date='20260401', end_date='20260402')
print('✅ Tushare OK, rows:', len(df))
"
```

**通过标准**：输出 `✅ Tushare OK, rows: 1` 或 `rows: 2`  
**失败处理**：
- Token 过期 → 去 tushare.pro 重新获取，更新 .env 的 `TUSHARE_TOKEN`
- 积分不足 → 查看 tushare.pro 积分余额

---

## Part 3：启动 P10 后台进程

### Step 6：清理残留进程

```bash
ps -ef | grep -E "start_scheduler|start_bot|uvicorn api.main" | grep -v grep
```

**预期（Mac 重启后）**：无输出（所有 nohup 进程已随重启消失）  
**如果有残留**：

```bash
# 获取 PID 后 kill
kill $(ps -ef | grep -E "start_scheduler|start_bot|uvicorn api.main" | grep -v grep | awk '{print $2}')
```

---

### Step 7：启动 Scheduler

```bash
cd "$PROJECT_DIR" && set -a && source .env && set +a && \
nohup python3 scripts/start_scheduler.py >> logs/scheduler.log 2>&1 & \
echo "Scheduler PID: $!"
```

**通过标准**：
1. 命令立即打印出 PID（如 `Scheduler PID: 12345`）
2. **30 秒内** Telegram 收到：`✅ Scheduler started [时间]`，内含 17 个 job 列表
3. 检查日志无错误：

```bash
sleep 20 && tail -30 "$PROJECT_DIR/logs/scheduler.log" | grep -E "startup_check|scheduler_started|ERROR|failed"
```

**失败处理**：

| 日志关键词 | 含义 | 处理 |
|-----------|------|------|
| `startup_check_failed` + `market_bars_cn` | CN bars 数据超过 2 个交易日未更新 | 先手动补拉（见 Part 5） |
| `db_pool_error` / `Connection refused` | PostgreSQL 未运行 | 回 Step 1 |
| `telegram_push_error` | Telegram 不可达 | 回 Step 2 |
| `InvariantViolation` | features 覆盖不足 48 只 | 手动跑 `python3 scripts/diagnose_feature_coverage.py` |

---

### Step 8：启动 Bot

```bash
cd "$PROJECT_DIR" && set -a && source .env && set +a && \
nohup python3 scripts/start_bot.py >> logs/bot.log 2>&1 & \
echo "Bot PID: $!"
```

**通过标准**：
1. 命令立即打印出 PID
2. **30 秒内** Telegram 收到：`✅ Bot started [时间] 命令菜单已更新，共 X 个命令。`
3. 检查日志：

```bash
sleep 10 && tail -10 "$PROJECT_DIR/logs/bot.log" | grep -v "^{"
```

**失败处理**：
- 无 PID 输出 → 看 bot.log 具体报错
- `TELEGRAM_BOT_TOKEN` 相关错误 → 检查 .env 中 `TELEGRAM_BOT_TOKEN`

---

### Step 9：启动 API 服务（Dashboard 依赖）

```bash
cd "$PROJECT_DIR" && set -a && source .env && set +a && \
nohup python3 -m uvicorn api.main:app --port 8000 >> logs/api.log 2>&1 & \
echo "API PID: $!"
```

**通过标准**：

```bash
sleep 5 && curl -s http://localhost:8000/api/regime/latest | python3 -c "import sys,json; d=json.load(sys.stdin); print('✅ API OK, keys:', list(d.keys()))"
```

输出 `✅ API OK, keys: [...]`  
**失败处理**：看 `logs/api.log`，通常是 DB 连接失败或端口占用（`lsof -ti:8000` 查占用进程）

---

### Step 10：启动前端 Dashboard（可选，需要看 UI 时）

```bash
cd "$PROJECT_DIR/frontend" && npm run dev
```

**通过标准**：终端输出 `Local: http://localhost:5173/`，浏览器访问该地址  
**注意**：这会占用一个终端窗口，关闭终端则 Dashboard 停止。如需后台运行：

```bash
cd "$PROJECT_DIR/frontend" && nohup npm run dev >> "$PROJECT_DIR/logs/frontend.log" 2>&1 &
echo "Frontend PID: $!"
```

---

## Part 4：验证系统健康

### Step 11：进程全家桶检查

```bash
ps -ef | grep -E "start_scheduler|start_bot|uvicorn api.main" | grep -v grep
```

**通过标准**：显示 3 行（scheduler + bot + uvicorn）  
**失败处理**：缺哪个 → 回对应 Step 补启

---

### Step 12：Telegram /status 验证

在 Telegram 向 Bot 发送 `/status`

**通过标准（30 秒内收到回复）**，回复含：
- ✅ 最新 CN + US Regime 状态
- ✅ 最后心跳时间 < 35 分钟
- ✅ LLM 今日成本（¥ 数字）
- ✅ 下次日报时间

**失败处理**：
- 无响应 → `tail -20 "$PROJECT_DIR/logs/bot.log"` 看报错
- 心跳超时 → scheduler 进程是否存活？ `ps -ef | grep start_scheduler | grep -v grep`

---

### Step 13：确认 Scheduler 17 个 job 注册

Telegram 发 `/status`，在 Scheduler 区块确认以下 17 个 job 全部出现：

```
scheduler_self_check    heartbeat
pull_cn_market_data     pull_us_market_data
update_features_daily_cn  update_features_daily_us
detect_regime_cn        detect_regime_us
run_composite_analysis_cn  run_composite_analysis_us
send_daily_digest_cn    send_daily_digest_us
check_data_freshness    backfill_judgments
backfill_missing_bars   weekly_review    monthly_review
```

**失败处理**：job 数量不对 → `tail -50 "$PROJECT_DIR/logs/scheduler.log" | grep job_registered`

---

### Step 14：首个心跳确认

等 5 分钟后执行（scheduler 每 30 分钟写心跳）：

```bash
cd "$PROJECT_DIR" && set -a && source .env && set +a && \
python3 -c "
import asyncio, sys
sys.path.insert(0, '.')
async def main():
    from db.connection import init_pool, close_pool
    await init_pool()
    import asyncpg, os
    conn = await asyncpg.connect(os.environ['DATABASE_URL'])
    row = await conn.fetchrow('SELECT MAX(beat_time) AS last FROM scheduler_heartbeat')
    print('Last heartbeat:', row['last'])
    await conn.close()
    await close_pool()
asyncio.run(main())
"
```

**通过标准**：输出时间在最近 35 分钟内  
**失败处理**：心跳未写入 → scheduler 启动失败，检查 scheduler.log

---

## Part 5：补跑 miss 的 Job（重启跨过了关键时间窗口）

### 先判断是否需要补跑

```
当前 BJT 时间          可能 miss 的 job
─────────────────────────────────────────────────
05:30 ~ 07:00 之间重启  US: pull → features → regime → composite → digest
15:15 ~ 16:30 之间重启  CN: pull → features → regime → composite → digest
16:10 之间重启          backfill_judgments（可忽略，明天自然补跑）
其他时间重启            无需补跑
```

### 补跑单个 job（通用命令格式）

```bash
cd "$PROJECT_DIR" && set -a && source .env && set +a && \
python3 scripts/start_scheduler.py run_job <job_name>
```

可用 `<job_name>`：
```
pull_cn_market_data    pull_us_market_data
update_features_daily_cn   update_features_daily_us
detect_regime_cn       detect_regime_us
run_composite_analysis_cn  run_composite_analysis_us
check_data_freshness   backfill_judgments
```

> ⚠️ **日报不通过 run_job 补发**，直接在 Telegram 发 `/daily CN` 或 `/daily US`

---

### 补跑 CN 全链路（15:15–16:30 之间重启时）

> 必须严格按顺序，每步等完成再下一步（数据依赖）

```bash
cd "$PROJECT_DIR" && set -a && source .env && set +a

python3 scripts/start_scheduler.py run_job pull_cn_market_data && \
python3 scripts/start_scheduler.py run_job update_features_daily_cn && \
python3 scripts/start_scheduler.py run_job detect_regime_cn && \
python3 scripts/start_scheduler.py run_job run_composite_analysis_cn
```

补跑完成后，Telegram 发 `/daily CN` 手动触发日报。

---

### 补跑 US 全链路（05:30–07:00 之间重启时）

```bash
cd "$PROJECT_DIR" && set -a && source .env && set +a

python3 scripts/start_scheduler.py run_job pull_us_market_data && \
python3 scripts/start_scheduler.py run_job update_features_daily_us && \
python3 scripts/start_scheduler.py run_job detect_regime_us && \
python3 scripts/start_scheduler.py run_job run_composite_analysis_us
```

补跑完成后，Telegram 发 `/daily US` 手动触发日报。

---

### Scheduler 启动被 freshness check 拦截时

**症状**：`logs/scheduler.log` 出现 `startup_check_failed` + `market_bars_cn` 或 `market_bars_us`

**处理（以 CN 为例）**：

```bash
cd "$PROJECT_DIR" && set -a && source .env && set +a && \
python3 scripts/start_scheduler.py run_job pull_cn_market_data
```

成功后重新启动 scheduler（Step 7）。

---

## Part 6：异常情况速查

### Telegram 无响应

```bash
# 检查 bot 进程
ps -ef | grep start_bot | grep -v grep

# 检查 bot 日志
tail -30 "$PROJECT_DIR/logs/bot.log"

# 验证 token
cd "$PROJECT_DIR" && set -a && source .env && set +a && \
curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe" | python3 -c "import sys,json; d=json.load(sys.stdin); print('✅ Token OK:', d.get('result',{}).get('username',''))" 
```

---

### Scheduler 心跳中断

```bash
# 1. 确认 Mac 没睡眠
# 系统设置 → 电池 → 接通电源时防止自动进入睡眠 → 确认已勾选

# 2. 检查进程是否存活
ps -ef | grep start_scheduler | grep -v grep

# 3. 查最近日志
tail -50 "$PROJECT_DIR/logs/scheduler.log" | grep -E "ERROR|fatal|missed|heartbeat"

# 4. 如果进程不见了，重启
cd "$PROJECT_DIR" && set -a && source .env && set +a && \
nohup python3 scripts/start_scheduler.py >> logs/scheduler.log 2>&1 & echo "New PID: $!"
```

---

### Dashboard 打不开（http://localhost:5173）

```bash
# 检查 API 是否运行
curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/api/regime/latest
# 通过标准: 200

# 如果 API 未运行，重启 API（Step 9）

# 检查前端是否运行
ps -ef | grep "npm run dev\|vite" | grep -v grep
# 如果没有，重启前端（Step 10）
```

---

### Scheduler 启动报 InvariantViolation（features 覆盖不足）

```bash
cd "$PROJECT_DIR" && set -a && source .env && set +a && \
python3 scripts/diagnose_feature_coverage.py
```

根据输出确认哪些 symbol 缺 features，然后手动跑 `run_job update_features_daily_cn` 或 `update_features_daily_us`。

---

### 日报显示 0 多 0 空（正常情况确认）

查今日 composite_score 分布：

```bash
cd "$PROJECT_DIR" && set -a && source .env && set +a && \
python3 -c "
import asyncio, sys, os
sys.path.insert(0, '.')
async def main():
    from db.connection import init_pool, close_pool
    await init_pool()
    import asyncpg
    conn = await asyncpg.connect(os.environ['DATABASE_URL'])
    from datetime import date
    rows = await conn.fetch('''
        SELECT market,
               COUNT(*) total,
               COUNT(*) FILTER (WHERE direction=\\'bullish\\') bull,
               COUNT(*) FILTER (WHERE direction=\\'bearish\\') bear,
               ROUND(MIN(composite_score)::numeric,1) min_comp,
               ROUND(MAX(composite_score)::numeric,1) max_comp,
               ROUND(AVG(confidence)::numeric,3) avg_conf
        FROM judgments WHERE judgment_date = \$1
        GROUP BY market
    ''', date.today())
    for r in rows: print(dict(r))
    await conn.close()
    await close_pool()
asyncio.run(main())
"
```

- `max_comp < 65` → 正常（市场无方向信号），不是 bug
- `llm_direction` 全 NULL → 检查 composite.py 是否用新代码（重启 scheduler 后第一次跑才生效）

---

## Part 7：长期改进（当前暂不做）

- **自动开机启动**：配置 launchd plist，Mac 重启后自动拉起 scheduler/bot/api（彻底消灭本清单的使用场景）
- **迁移 conda 环境**：将 Python 3.9 系统环境迁移到独立 conda env `p10`（DT-016），解决版本不可控问题
- **部署到云服务器**：Mac 休眠、重启问题根治方案，本清单归档

---

## 总耗时参考

| 场景 | 预计时间 |
|------|---------|
| 正常重启后完整恢复（Part 2-4） | 5–10 分钟 |
| 需要补跑 CN 全链路 | 额外 15–20 分钟 |
| 需要补跑 US 全链路 | 额外 20–30 分钟 |
| freshness check 拦截 + 补数据 | 额外 5 分钟 |

---

*最后更新：2026-04-22 · 对应 scheduler PID 体系（17 jobs）*
