# Phase 1 已知问题 / 技术欠债

> 本文档记录 Phase 1 开发过程中有意搁置或简化的决定，供 Phase 2 参考。

---

## DT-001 · inactive 股票历史数据未保留

**时间**：2026-04-20  
**决定**：DELETE 所有 active=FALSE 的 stock_universe 行，同步清理关联的 features_daily / fundamentals_daily / moneyflow_daily / market_bars_daily / judgments 数据。  
**影响**：5 只被删股票（000001.SZ / 600519.SH / AAPL / QQQ / SPY）的历史数据不可恢复。若未来需复活某只，需重新拉取历史数据。  
**原因**：这 5 只是 init_us_universe 脚本写入的旧数据，非 M2 watchlist 管理的范畴，保留意义低。

---

## DT-002 · 12 只孤儿 features 股票未加入 watchlist

**时间**：2026-04-20  
**决定**：直接删除 features_daily 中不在 stock_universe 的 12 只孤儿数据，不加入 watchlist。  
**涉及股票**：000818.SZ 金牛化工 / 300002.SZ 神州泰岳 / 300196.SZ 长海股份 / 300821.SZ 东岳硅材 / 600575.SH 淮河能源 / 688599.SH 天合光能 / BX Blackstone / FUTU Futu Holdings / NTES NetEase / TCOM Trip.com / XPEV XPeng / ZM Zoom  
**原因**：维持 watchlist 简洁（48 只），未来如需加入其中任何一只，重新通过 load_watchlist.py 导入并补跑 features 即可。

---

## DT-003 · db/universe.py get_stock() 仍用单 symbol 查询

**时间**：2026-04-20  
**问题**：`get_stock(symbol)` 的 WHERE 子句只用 `symbol = $1`，未包含 `market`，在 (symbol, market) 联合 PK 下语义不精确（同一 symbol 在 CN/US 均存在时会返回随机一行）。  
**当前状态**：watchlist 中无同名跨市场股票，暂时无害。  
**处理建议**：Phase 2 将 `get_stock` 签名改为 `get_stock(symbol, market)`。

---

## DT-004 · deactivate_stock() 仍用单 symbol 查询

**时间**：2026-04-20  
**问题**：同 DT-003，WHERE 子句缺 market 字段。  
**处理建议**：Phase 2 同步修复。

---

## DT-006 · market_bars_daily 从全市场缩减到 48 只 watchlist

**时间**：2026-04-20  
**决定**：清理了约 542 万行孤儿数据，market_bars_daily 从原来覆盖 4828+ 只股票缩减到仅 48 只 active watchlist 股票。  
**影响**：
- 未来新增 watchlist 股票需要重新拉原始 bars（`data/pipeline/` 全量拉取任务）
- P10-Backtest 子项目的全市场测试能力实际依赖其独立数据库，不受影响（见 P10-Backtest/CLAUDE.md）
- Phase 2 如需恢复全市场数据，需重新跑 `data/pipeline/` 的全量拉取任务，预计 Tushare 积分消耗 1000+  
**原因**：Phase 1 主链路只需 48 只 active 股票，全市场数据占用空间大且无实际消费方。

---

## DT-007 · M5 freshness check 阈值不适配交易日历（已修复）

**时间**：2026-04-21（发现于冒烟测试，本次 M6 修复）  
**问题**：`data_source_expectations` 的 `max_lag_days` 原为自然日，导致周末（2 天）+ 正常拉取延迟触发 false critical 告警，阻断 scheduler 启动。  
**根因分析**：
- 冒烟测试时间：UTC 2026-04-20T16:xx（上海时间 2026-04-21 00:xx，即周二凌晨）
- CN 市场最后数据：2026-04-17（**周五**）；CN trade_calendar 确认：4/17 = Friday ✓  
- 自然日 lag = 4（April 17 → 21），超过阈值 3 → false critical  
- 交易日 lag = 2（April 20 Monday + April 21 Tuesday 尚未拉取），≤ 阈值 2 → ok ✓  
**修复方案**：
1. `data_source_expectations` 新增 `lag_basis TEXT DEFAULT 'trading_days'` 列
2. `data_freshness_check.py` 新增 `count_trading_days_between()` 查询 `trade_calendar` 表
3. daily 数据源 → `lag_basis='trading_days'`, `max_lag_days=2`
4. monthly/quarterly 数据源 → `lag_basis='calendar_days'`，保持原有阈值  
**残留欠债**：`trade_calendar` 只有 `trade_date` 单列，不区分 CN/US 市场，使用的是 CN 交易日历。US-only 假日（如 Thanksgiving）不在 CN 日历中，导致 US 数据源的 trading day lag 可能偏低 1 天。影响微小，Phase 2 按需添加 `market` 列。

---

## DT-005 · market_bars_daily 删除了 5.4M 行历史数据

**时间**：2026-04-20  
**决定**：清理 12 只孤儿 + 5 只 inactive 股票对应的全部 market_bars_daily 数据（5,428,860 行）。  
**影响**：这些股票的历史价格数据不可恢复，回测若需要这些股票需重新拉取。  
**注意**：P10-Backtest 子项目有独立数据库，不受影响。

---

## DT-008 · roe_ttm 非严格4季滚动TTM口径

**时间**：2026-04-21（F1 fundamental bug 调查时发现）  
**问题**：Tushare `fina_indicator` 的 `roe_ttm` 字段文档描述为"净资产收益率(TTM)"，但实际极可能是"单季净利润×4/净资产均值"（季化年化），而非严格的滚动4季度净利润之和除以净资产。对季节性明显的行业（如零售、农业）偏差最大。  
**影响**：盈利质量评分对季节性公司可能偏高或偏低，但整体偏差对大多数制造/金融类公司 < 5%。  
**处置**：接受为 Phase 1 已知限制，记入 known_issues。  
**Phase 2 候选修复**：从 `income`（归母净利润）+ `balancesheet`（归母净资产）表手动计算真实4季滚动ROE，替换 Tushare 字段。

---

## DT-009 · confidence 公式在 defense/risk_off regime 下结构性偏低

**时间**：2026-04-21（M7 10只样本统计时发现）  
**问题**：`confidence = |composite - 52.5| / 50 × regime.signal_threshold_adj`。在 defense regime（adj=0.80）下，即使 composite=70，confidence = (70-52.5)/50 × 0.80 = 0.28，永远低于 0.35。在 risk_off（adj=0.70）下更低。  
**影响**：confidence 分布 p50 约 0.13，p75 约 0.26，无法体现高质量信号的区分度。  
**处置**：接受为 Phase 1 已知限制，不阻塞接线。  
**Phase 2 候选修复**：重新设计 confidence 公式，将 regime_factor 改为对高信号的放大因子，而非对所有信号的压制因子；或将 regime 因子单独输出为 `regime_adjusted_confidence`，保留原始 confidence 作参考。

---

## DT-012 · Scheduler 手动触发与定时触发共用 save_judgment 路径，无类型标注

**时间**：2026-04-21（数据复盘时发现重复 judgment 问题）  
**问题**：`scheduler.py` 的定时触发和手动测试/重跑均调用同一 `CompositeAnalyzer.save_judgment()`，无法在 `judgments` 表中区分来源。当 scheduler 同日触发两次（如进程重启导致任务补跑），会产生重复记录。  
**临时修复**：已加 `UNIQUE (symbol, market, judgment_date)` 约束 + `ON CONFLICT DO UPDATE` upsert，防止物理重复。  
**Phase 2 完整修复**：为 `judgments` 表增加 `judgment_type TEXT`（`scheduled`/`manual`/`backfill`），在 `save_judgment()` 接口传入 `judgment_type` 参数；对同 symbol+market+date 的多次写入，仅允许 `scheduled` 覆盖 `scheduled`，`manual` 不覆盖已有 `scheduled`。

---

## DT-013 · M4 LLM 方向判断引入幻觉风险，打破 Phase 1 原则

**时间**：2026-04-21（Phase 1.5 设计时确认）  
**问题**：Phase 1 原则是"LLM 只写叙事，方向决策由规则引擎完成"。Phase 1.5 的 M4 引入 `llm_direction` 和 `llm_signal_strength`，让 LLM 独立输出方向判断，打破了该原则。LLM 对定量信号的解读可能产生幻觉，尤其在数据稀疏或 prompt 复杂时。  
**风险管理**：
1. LLM 信号仅作为参考，规则信号（`direction`/`confidence`）仍为主信号，Telegram 日报和 Dashboard 列表均优先展示规则信号
2. 前两周每周统计 LLM vs 规则方向一致率；若一致率 < 50% 或 LLM 准确率低于规则，考虑回退
3. `llm_direction = 'unknown'` 比例超 20% 时推送告警
4. 不允许 LLM 输出具体价格（写入 prompt hard constraint）  
**相关验证**：M4.3 成本预估（月 > ¥300 暂停）、M4.4 unknown 比例监控均为配套措施。

---

## DT-011 · Scheduler 心跳中断根因定位为 Mac 休眠

**时间**：2026-04-21（接线后第一天），2026-04-22（根因重定位）  
**原始诊断**：推测为 asyncpg idle timeout 或连接池耗尽导致事件循环静默停止。  
**真实根因（2026-04-22 修正）**：日志显示 3 次 missed heartbeat 均发生在夜间（21:00/23:00/00:00 CST），为 **Mac 休眠**导致事件循环暂停，非 APScheduler 或 asyncpg 的 bug。  
**解决方案**：
- 系统层（根治）：用户已在 macOS System Preferences → Battery/电池 → 接通电源时关闭"自动进入睡眠模式"，一次设置永久生效
- 防御层（保留）：`safe_run_job` 新增 `CancelledError`/`BaseException` 捕获 + `scheduler_self_check`（每 5 分钟检测心跳），作为未来部署新环境时的保障
**Phase 2 终极方案**：部署到 Linux 服务器后，Mac 休眠问题自然消失，DT-011 可关闭。

---

## DT-010 · CN/US 财务数据存储格式不一致

**时间**：2026-04-21（F1 修复时发现）  
**问题**：`financials_quarterly` 中，CN 股票（来自 Tushare）的百分比字段以百分比形式存储（`revenue_yoy=10.97` = 10.97%）；US 股票（来自 yfinance）以小数形式存储（`revenue_yoy=0.7321` = 73.21%）。  
**影响**：如果不加区分处理，US 股票评分会严重错误（原 bug 下 CN 被错误 ×100，修复后 US 保留 ×100）。  
**处置**：已通过 symbol 后缀判断分支处理（`.SZ/.SH/.BJ` = CN = 不转换，其他 = US = ×100）。当前逻辑正确，抽检通过。  
**Phase 2 候选优化**：在数据入库时统一归一化为同一格式（推荐统一为百分比形式），消除运行时分支判断的维护负担。

---

## DT-014 · LLM 边界案例浮点不确定性

**时间**：2026-04-21（M5 验收后发现）  
**问题**：000960.SZ（tech=53/fund=64/flow=91/sent=75/composite=70.6，defense regime）以 temperature=0 + top_p=1.0 连续 5 次分析结果不一致：3次 neutral/hold，2次 bullish/buy。根因为神经网络浮点非确定性，在 logit 差异极小的边界案例无法通过参数消除。  
**影响范围**：仅影响维度高度矛盾的"边界股票"（估计 10-20% 的股票），典型特征：flow/sent 强但 tech 弱 + defense regime。  
**已完成修复**：`composite.py` LLM 调用改 temperature=0.0（原 0.3），`client.py` 默认 temperature=0.0 + top_p=1.0（温度为0时）。  
**为什么对质量追踪仍有效**：噪声无偏（非系统偏向），质量追踪依赖大样本统计（N>100），单次随机噪声均值趋零。真正危险的系统性偏差（LLM 总是比规则激进）可通过质量追踪页分歧区块检测。  
**Phase 2 候选方案**：3-call majority vote，月增成本 ¥6.46（总月估 ¥9.69，仍可接受），需衡量额外延迟（3×12s=36s/stock）。

---

## DT-016 · Scheduler 运行在系统 Python 3.9，非独立虚拟环境

**时间**：2026-04-22（scheduler 重启审计时发现）  
**问题**：当前 scheduler 进程使用 `/Applications/Xcode.app/.../Python3.9`（macOS 系统 python3），非项目 venv 或 conda 环境。项目文档要求 Python 3.11+。  
**当前影响**：Phase 1 代码在 Python 3.9 下运行正常（无 3.10+/3.11+ 专有语法），暂无功能影响。  
**潜在风险**：依赖版本不可控；跨机部署时环境不一致；Xcode 更新可能改变 Python 版本。  
**Phase 2 修复**：迁移到独立 conda 环境（`conda activate p10` + `conda env export`），启动命令改为 `conda run -n p10 python scripts/start_scheduler.py`，便于依赖管理和跨机部署。

---

## DT-015 · 历史判断 llm_direction 列 cold start

**时间**：2026-04-21（M5.1 schema 迁移时）  
**问题**：M5.1 之前保存的判断（约 31 条 2026-04-21 数据）仅在 `signal_sources` JSONB 中有 llm_direction，新 `llm_direction` 列为 NULL。质量追踪分歧统计（`/api/quality-tracking`）仅统计列中有值的行，cold start 期样本量极少。  
**不修复原因**：历史数据重新分析成本高（需重新调用 LLM），且历史叙事已基于旧 prompt（无 direction 字段）。接受自然积累。  
**Phase 2 候选**：可写脚本从 `signal_sources->>'llm_direction'` 回填 `llm_direction` 列（仅限已有 JSON 字段的行），一次性操作，成本 ¥0。
