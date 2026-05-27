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


## 2026-05-26 (周二)

```yaml
auto:
  cn_candidates_analyzed: 52
  regime_cn: cautious_offense
  cn_signals:
    strong_buy: []
    buy: ['300502.SZ', '002050.SZ', '603893.SH', '000962.SZ', '600590.SH', '600160.SH', '603986.SH', '603683.SH', '000960.SZ', '000657.SZ', '601138.SH']
    weak_buy: ['600392.SH', '000510.SZ']
    weak_sell: ['603716.SH']
    sell: ['002426.SZ']
    strong_sell: []
  us_candidates_analyzed: 0
  regime_us: cautious_offense
  us_signals:
    strong_buy: []
    buy: []
    weak_buy: []
    weak_sell: []
    sell: []
    strong_sell: []
```

### 我看了什么 / 关注什么（人工补）
- _待补_

### 反思 / 待跟进
- [ ] _待补_

---

## 2026-05-27 (周三)

```yaml
auto:
  cn_candidates_analyzed: 34
  regime_cn: cautious_offense
  cn_signals:
    strong_buy: []
    buy: []
    weak_buy: []
    weak_sell: []
    sell: []
    strong_sell: []
  us_candidates_analyzed: 31
  regime_us: cautious_offense
  us_signals:
    strong_buy: []
    buy: []
    weak_buy: ['NVDA', 'ADBE']
    weak_sell: []
    sell: ['AMD', 'MRVL', 'AVGO', 'ASML', 'ARM', 'PLTR', 'QCOM', 'SMCI', 'TSM']
    strong_sell: []
```

### 我看了什么 / 关注什么（人工补）
- _待补_

### 反思 / 待跟进
- [ ] _待补_

---
---

<!-- 新的一天追加在上面，最新日期在顶部 -->
