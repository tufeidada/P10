# Phase 1.5 改造完成记录

完成日期: 2026-04-21  
状态: **已完成，进入实战观察期**

---

## Phase 1.5 改造清单

### M1 — 紧急 Bug 修复
- **重复 judgment**：`judgments` 表加 `UNIQUE (symbol, market, judgment_date)` 约束，`save_judgment` 改为 `ON CONFLICT DO UPDATE` upsert，删除存量重复行
- **北向资金渲染**：`extractEvidence()` 修复 `[object Object]` bug，从 `northbound.net_5d` 提取数值
- **Sidebar 日期错误**：改为显示 `max(cn.trade_date, us.trade_date)`，数据超 1 天时显示 ⚠️

### M2 — UI 优化
- react-markdown 渲染 LLM 叙事（`.markdown-content` CSS）
- 股价列新增涨跌幅（A股惯例：涨红跌绿），过时行情显示"X天前"
- 字体 14→16px，表格行高 50px→40px，score pill 色阈调整（>70 绿 / 50-70 黄 / <50 红）

### M3 — Confidence 公式重设计
- **旧公式**：`base_confidence × regime_factor`（regime_factor 在 defense/risk_off 模式将置信度压到 0.1 以下，导致信号消失）
- **新公式**：`agree_ratio × 0.7 + distance × 0.3`，`agree_ratio` = 4 维度与 composite 方向一致的比例
- 48 股 dry-run：median confidence 0.13 → 0.389，strong_buy 5/48，hold 43/48
- `_compute_rule_signal_strength()`：新增静态方法，direction + confidence → 5 档信号（strong_buy / buy / hold / sell / strong_sell）

### M4 — LLM 结构化方向判断
- `llm/prompts.py`：新增 system prompt，`ANALYSIS_PROMPT` 改为要求 JSON `{direction, signal_strength, reasoning, risks, extra_advice, narrative}`，明确禁止价格数字
- `composite.py`：解析 LLM JSON，6 字段写入 `signal_sources` JSONB
- 成本测试：¥0.00112/call，月预估 **¥3.23**（远低于 ¥300 阈值）
- `temperature` 修复：0.3 → 0.0，同时 `top_p=1.0`（仍存在边界案例浮点不确定性，见 DT-014）

### M5 — 双信号体系 + 质量追踪页
- **M5.1 Schema**：`judgments` 表新增 6 列（rule_signal_strength, llm_direction, llm_signal_strength, llm_reasoning, llm_risks, llm_extra_advice）
- **M5.2 详情页**：`TradeSuggestionCard` → `DualSignalCard`（规则/LLM 信号并排，分歧 ⚠️，三行建议文字），移除全部价格目标
- **M5.3 首页列表**：增加"规则信号"+"LLM 信号"两列，分歧标 ⚠️
- **M5.4 质量追踪页** `/quality`（API: `/api/quality-tracking`）：规则胜率表 / LLM 胜率表 / 规则 vs LLM 分歧区块（3 卡片 + 14 天柱状图 + 解读）/ Alpha 区块
- **M5.5**：backfill_judgments 确认在 scheduler（每日 16:10 CN），`/status` 新增回填状态行

### Phase 1.5 附加改动（含 1.1）
- **日报格式**（bot/commands/daily.py）：每行新格式 `🟢 000960.SZ 锡业股份  71/100  规则:BUY · 置信度 65% | LLM:HOLD  ⚠️`
- **LLM 成本常态感知**：健康页 + `/status` 显示"近 7 天日均 ¥X | 月预估 ¥Y"，`llm_quality.unknown_ratio` 监控

---

## Known Issues 更新

| ID | 问题 | 状态 |
|----|------|------|
| DT-011 | APScheduler 事件循环静默停止（已加 try/except 兜底，未从架构解决） | Phase 2 修复 |
| DT-012 | Scheduler manual/scheduled 触发 save_judgment 共用路径，缺 `judgment_type` 字段区分 | Phase 2 修复 |
| DT-013 | LLM 方向判断幻觉风险（2-4 周实战验证期） | 观察中 |
| DT-014 | LLM 浮点不确定性（温度=0 仍 ~40% 边界案例方差，temperature=0 + top_p=1.0 已实施，效果不足） | Phase 2 修复（3-call vote） |

**DT-014 影响的 3 个场景：**
1. 用户基于单次判断决策 → 40% 噪声直接影响个别决定
2. 累计胜率被噪声稀释 → 难以区分"LLM 真的准确"和"随机噪声"
3. 分歧统计失真 → 难以分辨真分歧和噪声分歧

| DT-015 | 历史判断 llm_direction 列 cold start（旧数据仅在 signal_sources JSONB 中有字段） | 接受，自然积累 |

---

## Phase 2 候选方向（按优先级）

1. **3-call majority vote 缓解 LLM 随机性** ← 本批次启动
2. **Scheduler 健壮性审计**（safe_wrapper 统一、BaseException 捕获、自监控 job）← 本批次启动
3. **Regime 升级到 HMM / Jump Model**（待 2 周实战数据）
4. **日报移动端布局优化**（待用户反馈）
5. **Judgment 回填统计 + Alpha 计算**（待 T+5 数据积累）

---

## 数据基准（2026-04-21）

| 指标 | 值 |
|------|-----|
| 今日判断 CN | 31 条 |
| 今日判断 US | 6 条 |
| 今日 LLM unknown 比例 | 0% |
| LLM 月预估成本（单次） | ¥3.23 |
| LLM 月预估成本（3-call vote） | ~¥9.69 |
| 分歧统计样本（llm_direction 列） | 1 条（cold start） |
| 规则信号分布 | strong_buy 5/48，hold 43/48 |
| median confidence | 0.389 |

---

Phase 1.5 宣告完成。进入 Phase 2 开发阶段。
