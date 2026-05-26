# P10-AlphaRadar Daily Log

> **用法**：
> - **自动部分**（YAML code block）由 `composite_snapshot` job 末尾追加，**不要手改**
> - **手动部分**：你每日补 1-2 句话（看了哪些个股、对哪个信号有疑问）
> - **归档**：半年归档一次（`DAILY_LOG_2026H1.md`）
> - **AI 汇总**：Claude 读最近 N 天的本文件 + PLAN.md，生成跨项目报告

---

## 模板（每天追加一段）

````markdown
## YYYY-MM-DD (周X)

```yaml
auto:
  cron_status_cn: success | failed
  cron_status_us: success | failed
  cn_candidates_analyzed: 26
  us_candidates_analyzed: 22
  cn_signals:
    strong_buy: ["xxxxxx.SH"]
    buy: []
    weak_buy: []
    weak_sell: []
    sell: []
    strong_sell: []
  us_signals:
    strong_buy: []
    buy: []
    ...
  regime_cn: offense | cautious_offense | defense | risk_off
  regime_us: offense | cautious_offense | defense | risk_off
  llm_calls: 48
  llm_tokens_used: 87_320
  errors: []
```

### 我看了什么 / 关注什么（人工补）
- 今天对 <symbol> 做了 `run_composite_once.py`，结论：...
- 关注 <symbol> 已连续 X 天 strong_buy，但 P6 未出信号，原因？

### 反思 / 待跟进
- [ ] ...
````

---

## 2026-05-26 (周二)  ← 示例占位

```yaml
auto:
  cron_status_cn: pending       # 等今晚 16:30 之后自动填
  cron_status_us: pending       # 等明早 07:00 之后自动填
  cn_candidates_analyzed: 26
  us_candidates_analyzed: 22
  regime_cn: unknown            # 自动填
  regime_us: unknown
  note: "首条记录，待 scheduler 接入后自动写入"
```

### 我看了什么 / 关注什么
- _今天还没做按需研究，先观察 16:30 候选池信号_

### 反思 / 待跟进
- [ ] 16:30 之后查看 Telegram 日报，记录强势信号
- [ ] 本周内：让 `composite_snapshot` job 末尾追加 YAML 到本文件
- [ ] 想试一次 `python scripts/run_composite_once.py --symbol 600519.SH --market CN`，感受输出格式

---

<!-- 新的一天追加在上面，最新日期在顶部 -->
