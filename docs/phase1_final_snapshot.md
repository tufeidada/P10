# Phase 1 最终运行时状态快照

**生成时间**：2026-04-21T12:22 UTC（北京时间 20:22）  
**快照范围**：接线后第一天（2026-04-21）

---

## 1. 判断追踪机制状态

### judgments 表近 7 天每日新增

| judgment_date | 新增行数 |
|---------------|---------|
| 2026-04-21    | 13      |
| 2026-04-15 至 2026-04-20 | 0（接线前无自动写入）|

### 回填字段存在性及填充情况

| 字段 | 表中存在 | 非 NULL 行数 |
|------|---------|------------|
| actual_ret_1d | ✅ | 0 |
| actual_ret_5d | ✅ | 0 |
| actual_ret_10d | ✅ | 0 |
| actual_ret_20d | ✅ | 0 |
| actual_max_up_20d | ✅ | 0 |
| actual_max_dd_20d | ✅ | 0 |
| is_correct | ✅ | 0 |

所有回填字段已建列，无任何行已回填（接线第一天，无到期判断）。

### 自动回填 Job

| Job 名 | 在 scheduler 中 | 函数 | 最后运行时间 |
|--------|----------------|------|------------|
| backfill_judgments | ✅ 已注册（16:10 CN）| `core/evolution/judgment_tracker.JudgmentTracker.backfill_all()` | 无记录（从未触发）|
| backfill_signals | ✅ 已注册（16:20 CN）| `core/evolution/` 模块 | 无记录（从未触发）|

---

## 2. 数据实际产出量（2026-04-21）

### 核心表行数

| 表 | 日期 | 行数 |
|----|------|------|
| regime_daily | 2026-04-21 | 1 |
| judgments (CN) | 2026-04-21 | 7 |
| judgments (US) | 2026-04-21 | 6 |
| judgments (合计) | 2026-04-21 | 13 |
| features_daily | 2026-04-21 | 0（当日 job 已运行，更新对象为上一交易日）|
| features_daily | 2026-04-20 | 1 |

### judgments 总量分布

| 类别 | 行数 |
|------|------|
| 总行数 | 13 |
| fundamental_bug_affected = TRUE | 3 |
| 有效行（bug 修复后）| 10 |

### LLM 调用统计（2026-04-21）

| 指标 | 值 |
|------|---|
| 总调用次数（success）| 13 |
| 使用模型 | deepseek-v3-2-251201（唯一）|
| 总 tokens（in+out）| 9,528 |
| 总成本 | ¥0.0139 |
| 日预算利用率 | 0.014% （上限 ¥100）|
| failed / skipped 调用 | 0 |

### Scheduler Job 24h 统计

| Job 名 | success | failed | skipped | 备注 |
|--------|---------|--------|---------|------|
| detect_regime_cn | 1 | 0 | 1 | 1次跳过=数据未就绪 |
| detect_regime_us | 2 | 0 | 0 | |
| pull_cn_market_data | 1 | 1 | 0 | 1次失败 |
| pull_us_market_data | 2 | 0 | 0 | |
| update_features_daily_cn | 1 | 0 | 1 | |
| update_features_daily_us | 2 | 0 | 2 | 2次跳过=非US交易日 |
| run_composite_analysis_us | 0 | 0 | 1 | 接线前旧 placeholder 触发 |
| send_daily_digest | 0 | 0 | 1 | 接线前旧 placeholder 触发 |
| run_composite_analysis_cn | — | — | — | 接线后首次触发时间：今日 16:00 CN |
| send_daily_digest_cn/us | — | — | — | 接线后首次触发：16:30 / 明日 07:00 |

> 注：`run_composite_analysis_cn` 和 `task_send_daily_digest_cn/us` 的新接线版本在快照生成时尚未触发（接线于 10:04 UTC 完成，16:00 CN = 08:00 UTC 已过，首次真实触发为明日）。

### feature_update_log

| 字段 | 值 |
|------|---|
| 近 2 天记录行数 | 0（表存在但无近期记录）|
| 表结构 | id, run_date, market, symbol, success, error_message, computed_at |

---

## 3. Wiki 和经验条目积累状态

### wiki_pages

| page_type | 行数 |
|-----------|------|
| stock | 51 |
| strategy | 2 |
| **合计** | **53** |

股票页覆盖：51/48（active 股票）+ 3 条来自测试运行的额外页面

### experience_store

| status | 行数 |
|--------|------|
| active | 5 |
| under_review | 1 |
| **合计** | **6** |

### generate_stock_pages.py 自动执行

| 项目 | 值 |
|------|---|
| 是否接入 scheduler | 否（仅手动执行）|
| 最后一次执行时间 | 2026-04-21（开发期手动执行，无自动记录）|
| wiki 页自动更新触发 | composite analyze() 调用时自动写入（通过 WikiManager）|

---

## 4. 已知欠债分布

| 编号 | 分类 | 摘要 | 修复状态 |
|------|------|------|---------|
| DT-001 | 数据管理 | inactive 股票历史数据已删除，不可恢复 | 已知，可接受 |
| DT-002 | 数据管理 | 全市场数据未拉取，回测受限 | Phase 2 按需 |
| DT-003 | 数据管理 | features_daily 无 US 基本面字段 | Phase 4 扩展 |
| DT-004 | 数据管理 | trade_calendar 无 market 列，US 假日 lag 偏低 1 天 | Phase 2 按需 |
| DT-005 | 数据管理 | market_bars_daily 删除了 5.4M 行孤儿数据 | 已知，不可恢复 |
| DT-006 | 数据管理 | non-universe 股票数据清理 | 已知，可接受 |
| DT-007 | 监控 | M5 freshness check 自然日阈值 → 交易日阈值 | ✅ 已修复 |
| DT-008 | 分析质量 | roe_ttm 非严格 4 季滚动 TTM（Tushare 季化年化口径）| Phase 2 评估 |
| DT-009 | 分析质量 | confidence 公式在 defense regime 下结构性偏低（×0.80）| Phase 2 重设计 |
| DT-010 | 数据管理 | CN/US 财务字段存储格式不一致（CN=%, US=小数）| ✅ 已修复（分支处理）|

---

## 5. Scheduler 健康度

### 进程存活时间

| 进程 | PID | 启动时间（UTC）| 快照时运行时长 |
|------|-----|----------------|--------------|
| start_scheduler.py | 65694 | 2026-04-21 00:33:42 | ~11h 48min |
| start_bot.py | 2799 | 2026-04-21 00:38:50 | ~11h 43min |

### Heartbeat（过去 24h）

| 指标 | 值 |
|------|---|
| 心跳记录行数 | 23 |
| 预期行数（每 30min）| ~24（11.8h × 2）|
| 实际心跳间隔 | ~30min（正常）|
| 最后心跳时间 | 2026-04-21 04:00:00 UTC（快照时 8h 前）|

> 注：最后心跳 04:00 UTC，快照 12:22 UTC，间隔 8h。Scheduler 进程仍在 ps 中存活（PID 65694），但 beat_time 停止更新 — 心跳任务可能在某次运行中静默失败。

### Job 成功率汇总（24h）

| Job | 触发次数 | 成功次数 | 成功率 |
|-----|---------|---------|-------|
| detect_regime_cn | 2 | 1 | 50% |
| detect_regime_us | 2 | 2 | 100% |
| pull_cn_market_data | 2 | 1 | 50% |
| pull_us_market_data | 2 | 2 | 100% |
| update_features_daily_cn | 2 | 1 | 50% |
| update_features_daily_us | 4 | 2 | 50%（2 次跳过非失败）|

---

## 6. Phase 2 候选方向准备度

### 候选 1：Regime 升级

| 项目 | 状态 |
|------|------|
| jumpmodels 库是否安装 | ❌ 未安装 |
| 当前 detector.py 实现 | 规则引擎（阈值比较 + 市场广度修正）|
| detector.py 入口 | `core/regime/detector.py`，`detect_regime(market)` |
| regime_params.yaml 可热重载 | ✅（`_load_regime_params()` 每次调用重读）|
| 所需安装 | `pip install jumpmodels`（PyPI 上有）|

### 候选 2：LLM 对抗验证

| 项目 | 状态 |
|------|------|
| prompt 模板位置 | `llm/prompts.py`（301 行，6 个函数）|
| 函数列表 | `build_analysis_prompt`, `_summarize_tech`, `_summarize_fundamental`, `_summarize_flow`, `_summarize_sentiment`, `_build_key_data` |
| 是否模块化 | ✅ 各维度独立 summarize 函数，可单独替换 |
| 反驳模式所需改动 | 新增 `build_devil_advocate_prompt()` 函数 + composite.py 中第二次 LLM 调用 |

### 候选 3：盘中择时信号

| 项目 | 状态 |
|------|------|
| `core/intraday/` 目录 | ✅ 存在，4 个文件，1714 行 |
| 文件列表 | `signal_detector.py`（435 行）、`factors.py`（658 行）、`calibrator.py`（303 行）、`push.py`（296 行）|
| 代码状态 | 有完整实现：`SignalDetector.detect()`、`FactorCalculator.compute()`、`IntradaySignal` dataclass |
| 依赖表 | `intraday_signals`（signal_detector 中有 INSERT SQL，表是否存在未验证）|
| 接入 scheduler | ❌ 未接入（scheduler.py 中标注 `# TODO: Phase 3`）|
| 是否死代码 | 疑似可用代码，Phase 3 前需验证 `intraday_signals` 表存在性及 5min bar 数据可用性 |

### 判断追踪自动回填

| 项目 | 状态 |
|------|------|
| 回填字段已建列 | ✅ actual_ret_1d/5d/10d/20d, actual_max_up/dd_20d, is_correct |
| 回填 Job 已注册 | ✅ backfill_judgments（16:10）、backfill_signals（16:20）|
| JudgmentTracker 实现 | ✅ `core/evolution/judgment_tracker.py` |
| 当前回填行数 | 0（接线第一天，无到期判断）|
| 首次回填时间 | T+1 日（actual_ret_1d），T+5 日（actual_ret_5d），以此类推 |
