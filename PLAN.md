# P10-AlphaRadar 中长期计划

> 创建：2026-05-26
> 最近更新：2026-05-26
> 下次全面复盘：2026-08-31（季度末）
> 文档定位：个股深度研究助手的目标和演化方向

---

## 一、项目定位（演化中）

A 股 + 美股**个股深度研究助手**（不是自动选股、不是自动交易）。

- **CN 候选池**：26 只
- **US 候选池**：22 只
- **数据库**：alpharadar（Docker, host port 5433），**独立于 P6 的 stock_hub**
- **调度**：APScheduler，CN 15:15-16:35 + US 05:30-07:05 BJT
- **LLM**：DeepSeek V3（主力）+ Qwen3-Mini（轻量）+ text-embedding-v4
- **存储特色**：TimescaleDB + pgvector（为未来个股 wiki 检索准备）
- **核心引擎**：composite_score 0-100，7 档信号（strong_buy → strong_sell）

### 定位演化方向
- **当前（5 月）**：自动扫候选池，每日 Telegram 日报
- **目标（Q3）**：转为"按需深度研究 + 候选池监控"——你扔一只票，5 分钟出多维报告

---

## 二、本季度目标（2026 Q2）

### 主目标
- [ ] **完成定位转向**：从"自动扫描"转为"按需研究 + 候选池监控"
- [ ] **composite 评分质量观察**：strong_buy/buy 信号 T+5 命中率 ≥ 65%
- [ ] **与 P6 建立联动**：P6 买入信号自动触发 P10 报告（含 LLM 总结）
- [ ] **按需研究命令**：Telegram `/research <ticker>` 5 分钟出报告

### 关键里程碑

| 时间 | 事项 | 状态 |
|---|---|---|
| Phase 0-4 | A 股 + 美股四维分析体系完成 | ✅ |
| 5/26 | 启用 DAILY_LOG 记录日常信号质量（composite_snapshot job 自动追加 YAML 段） | ✅ |
| 5/26 | composite 评分修复：has_social=False 时 sentiment 权重重分配，避免市场情绪广播污染 | ✅ |
| 5/26 | flow 维度从同质化（uniq=1）改善到 differentiated（uniq=20+），靠 backfill 5 天 moneyflow | ✅ |
| 5/26 | scripts/backfill_range.py：补漏脚本，弥补 scheduler 3 天滚动窗口的不足 | ✅ |
| 6/15 | M5 观察期结束，评估信号阈值是否需调整 | ⏳ |
| 7/15 | Telegram `/research <ticker>` 命令上线 | ⏳ |
| 8/15 | P6 → P10 自动联动（P6 信号触发 P10 报告） | ⏳ |
| 8/31 | 季度复盘 | ⏳ |

### 关键指标基线
- **composite_score 权重**：随 regime 变化（offense/cautious_offense/defense/risk_off 4 套）
- **信号 7 档**：strong_sell → sell → weak_sell → hold → weak_buy → buy → strong_buy
- **维度**：tech / fund / flow / sentiment 4 维
- **sentiment 数据状态**：social_sentiment 表稀疏，常态下不参与 agree_ratio（仅 3 维生效）
- **LLM 日均消耗**：~88K tokens（DeepSeek V3）

---

## 三、当前阻塞 / 风险

- ⚠️ **social_sentiment 数据稀疏**：5/26 改用 has_social=False 时 sentiment 退出加权，缓解但未根治；要根治需要拉个股 StockTwits / 雪球数据。
- ⚠️ **数据源停更**：`tushare.index_daily` / `tushare.market_breadth` / `yfinance.us_vix` 在 4/15-4/17 后停更 20+ 天，scheduler 启动自检显示 warn 但不阻塞。这些是 regime 检测的辅助数据源，需要单独写 backfill 路径。
- ⚠️ **LLM 调用成本累积**：每日 ~88K tokens × 30 天 ≈ 2.6M tokens/月，需监控
- ⚠️ **信号未经过完整 backtest 验证**，命中率只能事后累积观察
- ⚠️ **候选池小（48 只）**：信号样本有限，统计意义弱
- ⚠️ **路径有空格**（`P10-AlphaRadar `），运维脚本需引号包裹
- ⚠️ **本地 Clash 代理端口在 7897/4780 间漂移**：.env 默认写 7897，启动失败时检查 `lsof -i :7897` 确认监听

---

## 四、不做的事（边界）

- ❌ **不做自动交易执行**
- ❌ **不做高频日内**
- ❌ **不让 LLM 发散选股**（LLM 仅做评分辅助，不出推荐池）
- ❌ **不做月线长期方向判断**（不是 P10 擅长的）
- ❌ **Backtest 和 Production 代码不互相引用**（已有禁区约束）
- ❌ **不在 LLM 失败时阻塞主流程**（保持 fallback 到规则判断）

---

## 五、联动关系

- **P6**：本项目作为 P6 信号的"个股研究补充"。P6 出买入信号 → P10 提供多维研究 + LLM 总结 + 风险点
- **P11**：暂无强联动（P11 是 A 股因子实验室，P10 偏个股研究）

---

## 六、复盘节奏

| 频率 | 内容 | 文档 |
|---|---|---|
| 每日 | scheduler 跑完后自动写入候选池信号 + 人补 1-2 句话 | `DAILY_LOG.md` |
| 每周日 | 跨项目周报 | `../REPORTS/weekly/` |
| 每月末 | 命中率累积统计 + 阈值调整建议 | `../REPORTS/monthly/` |
| 季度末（8/31） | 本计划全面修订 | 修改本文件 |

---

## 七、变更记录

| 日期 | 变更 |
|---|---|
| 2026-05-26 | 初版创建（Claude 起草，待 Yangxuan 修订） |
