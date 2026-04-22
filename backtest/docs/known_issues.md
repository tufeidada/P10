# Known Issues — Backtest Analysis Modules

记录已知的设计局限和待改进点。标注 **必须在 Composite 层处理** 的条目。

---

## TECH-01: MACD 过度打压强势股短期回调

**模块**: `analysis/technical.py` — `_score_macd()`
**发现日期**: 2026-04-19
**示例**: NVDA @ 2024-06-28

**现象**:
Stage=2 + RS Rank=93% 的强势股在主升浪中途出现 MACD 短暂转负时，MACD 得 0 分，
Daily score 被压至 neutral，系统在牛股回调时错失理想买点。

**根因**:
MACD 得分与 Stage/RS 得分地位相等（各 25 分），但 MACD 对短期波动更敏感，
容易与中长期趋势产生冲突。

**影响范围**:
- 主升浪中回调买点（Stage 2 + MACD 转负阶段）判断偏保守
- 周线 MACD 通常仍为正，combined_score 得以修正，但日线信号缺失

**解决方向（Composite 层处理，不改 Technical 内部）**:
识别组合 `daily_stage=2 AND weekly_stage=2 AND rs_rank>0.7 AND daily_macd_score=0`，
在 composite 中给 technical_component 加 bonus 或降低 MACD 的惩罚权重。

---

## TECH-02: Weekly Stage 在下跌转折期的滞后性

**模块**: `analysis/technical.py` — `_weekly_stage()`
**发现日期**: 2026-04-19
**示例**: 600160.SH @ 2023-06-08

**现象**:
股票日线已经 Stage=4（下跌），但周线 MA30 斜率尚未转负（前期上涨惯性），
导致 Weekly Stage=1（蓄力期边缘），而非 Stage=4。
combined_direction 靠 Daily 极低分数压到 bearish，但若 Daily 得分略高时可能误判为 neutral。

**根因**:
Weekly MA30（30 周）相比 Daily MA150（150 日）对近期走势的反应存在结构性滞后。
下跌初期（1-2 个月），日线感知到而周线尚未反映。

**影响范围**:
- 持续下跌初期（转折后 1-3 个月内）周线方向偏乐观

**解决方向（Composite 层处理，不改 Technical 内部）**:
识别组合 `weekly_stage=1 AND price_vs_ma30w < -5% AND daily_stage=4`，
在 composite 中 override 为明确 bearish，不等周线转头。

---

## TECH-03: Combined Direction 多周期矛盾时处理过于机械

**模块**: `analysis/technical.py` — `analyze_technical()`
**发现日期**: 2026-04-19

**现象**:
日线/周线方向不一致时，combined_score 为简单加权平均（60%/40%），
可能产生 daily=bullish + weekly=bearish → combined=neutral 的模糊结论。

**解决方向（Composite 层处理）**:
在 composite 中结合基本面做综合判断，当技术面存在多周期冲突时，
基本面/资金面可作为决定权，不让技术面单独定方向。

---

## DATA-01: 美股 turnover_rank_20d 全部为 NULL

**模块**: `analysis/technical.py`（上游: `scripts/03_compute_features.py`）
**发现日期**: 2026-04-19（Phase 2 阶段）

**现象**:
yfinance 不提供美股个股换手率，`market_bars_daily.turnover_rate` 全为 NULL，
导致 `features_daily.turnover_rank_20d` 对美股全部为 NULL。

**当前处理**:
Technical 模块暂未使用 `turnover_rank_20d`。Composite 模块设计时需对美股动态降低
资金流权重（Flow 模块也有此问题）。

---

## DATA-02: 美股 financials 历史季度不足

**模块**: `analysis/fundamental.py`
**发现日期**: 2026-04-19（Phase 2 阶段）

**现象**:
美股财报仅有 ~6 季度历史（2024 Q3 以后），"增速加速度"（近 4 季 vs 前 4 季）
在 2024 Q3 之前的回测点无法计算。稳定性指标（需要 8 季度）也可能缺失。

**当前处理**:
`fundamental.py` 在 n_quarters < 8 时降级用可用季度计算稳定性；
增速加速度不足时返回中性 50 分。

---

## FLOW-01: northbound_score 是市场级代理，非个股级信号

**模块**: `analysis/flow.py` — `_score_northbound()`
**发现日期**: 2026-04-19

**现象**:
同一交易日所有 A 股 `northbound_score` 完全相同（基于市场整体北向净买入），
与直觉上"个股资金面信号"不符。且与 `regime.py` 的 `liquidity_score` 存在语义重叠。

**影响范围**:
- Flow 维度对"个股相对市场"的识别能力打折
- 北向偏好 A 股时，所有 CN 股同时获得加分，无法区分个股间差异

**解决方向（Composite 层或数据层处理）**:
使用 Tushare `stk_nb_hold` 接口拉取个股港股通持股明细，
计算个股级北向持仓变化（天级）作为替代。
投入：1-2 天实现 + 补拉历史数据；对 Flow 有效性提升显著。

**当前状态**: 保留现状。`FlowAnalysis.northbound_score` docstring 已明确语义。
highlights/risks 文本加注"市场级指标"避免误读。

---

## FLOW-02: 北向数据仅从 2025-01-06 起可用

**模块**: `analysis/flow.py` — `_score_northbound()`
**发现日期**: 2026-04-19

**现象**:
`northbound_daily` 最早记录为 2025-01-06，早于此的回测点（如 2024-11、2024-08）
北向维度不可用，`northbound_score=None`，`data_complete=False`。

**当前处理**:
历史不足时返回 `None`（不用 50 占位），`compute_flow_score()` 自动排除该维度
并对 `main_flow + margin` 按比例重新加权（57.14% / 42.86%）。
综合分影响：实测 score 差值约 1 分（数值影响小，但 `data_complete` 标识语义正确）。

**解决方向**:
通过 AkShare 或东方财富补拉 2024 年北向数据。
本次回测范围（2025-09 ~ 2026-04）覆盖完整，不影响主回测结论。

---

## SENT-01: Sentiment 与 Regime 维度重叠（双重计分风险）

**模块**: `analysis/sentiment.py` vs `analysis/regime.py`
**发现日期**: 2026-04-19

**现象**:
Sentiment 的三个子项与 Regime 存在语义重叠：
- 涨跌停家数：Sentiment limit_ratio_score(30%) + Regime.breadth_score 的部分信号
- 融资余额变化：Sentiment margin_change_score(30%) + Regime.liquidity_score 的部分信号
- A 股整体情绪（Sentiment 整体）+ Regime 的"市场环境判断"有概念重叠

**影响范围**:
同一信号在 Sentiment 和 Regime 两个维度被两次使用，Composite 层如果对两者都赋予较高权重，
可能产生"系统性乐观/悲观偏置"（双重计分）。

**解决方向（Composite 层处理）**:
- 在 Composite 中降低 Sentiment 整体权重（建议 10-15%，而非 20-25%）
- 让 Regime 承担更多"市场环境"判断责任
- Sentiment 主要作为"情绪修正因子"而非独立维度

**当前状态**: 保留现状。Composite 设计时处理权重分配。

---

## SENT-02: Sentiment 权重对短期情绪恶化不敏感

**模块**: `analysis/sentiment.py`
**发现日期**: 2026-04-19

**现象**:
CN @ 2026-03-15（中美贸易摩擦）：`adv_score=26.37`（明显悲观），但综合分仍达 44.61（偏中性）。
原因：`margin_score=61.54`（融资余额小幅增加），拖高了综合分。
市场急剧恶化时，融资余额（中期信号，5日变化率）会滞后于真实情绪的恶化。

**影响范围**:
- 市场快速恶化（1-2 周内）时，Sentiment 下降比真实情绪慢
- 可能掩盖早期危险信号

**解决方向（Composite 层处理）**:
将 Sentiment 分数结合 Regime 的 `volatility_score` + `trend_score` 变化率做"市场恶化检测"，
不完全依赖 Sentiment 单独反应。
具体：当 Regime 切换为 defense/risk_off 时，回测引擎应自动将现有持仓置为 neutral 观望，
不追加做多，等下一个评估周期再重新判断。

**当前状态**: 保留 Sentiment 现有权重。Composite 权重（≤15%）已对此有所控制。

---

## SENT-03: Sentiment VIX 映射采用线性插值而非严格分段

**模块**: `analysis/sentiment.py` — `_score_vix()`
**发现日期**: 2026-04-19

**现象**:
Spec 描述的是"分段映射"（VIX 15-20 → 70-55），实现采用控制点间线性插值。
VIX=16.75 → score=64.75（线性），而严格分段下 VIX 15-20 区间的固定值约为 63。
差异约 ±2 分，不影响方向判断。

**与 Spec 的差异**: 实现细节，非设计违反。

**原因**:
避免在分段边界（如 VIX=15 vs 15.01）出现约 15 分的阶跃断层，
线性插值更平滑，与 Spec 的业务意图一致。

**文档状态**: `sentiment.py` 代码注释已说明。不视为违反 Spec。

---

*最后更新: 2026-04-19*
