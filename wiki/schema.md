# wiki/schema.md — P10-AlphaRadar Wiki 约定

## 页面类型及模板

### 个股页面 (stocks/)

每只票一个文件，首次分析时创建，后续分析时更新（不新建）。

模板:

```
---
symbol: {symbol}
market: {CN|US}
name: {名称}
industry: {行业}
last_updated: {日期}
current_stage: {Weinstein Stage}
---

## 公司概况
[主营业务、竞争力、核心风险 — 首次写入后较少更新]

## 当前状态 ({日期})
[最近一次分析的结论摘要 — 每次分析后覆盖更新]
- 技术面: {tech_score}/100 — {trend}, Stage {stage}, RS Rank {rs_rank}
- 基本面: {fund_score}/100 — ROE {roe}%, 营收增速 {rev_yoy}%
- 资金面: {flow_score}/100 — 主力5日{方向}, 北向{trend}
- 综合判断: {direction} ({composite_score}/100)
- 分析叙事: {logic_text 前200字}

## 关键价位
[历史重要支撑/阻力位 — 累积更新]
- 支撑: {supports}
- 阻力: {resistances}

## 行为模式
[已观察到的该股特有规律 — 累积更新]
例: "财报后首日高开低走概率 68% (n=8)"

## 历史判断摘要
[最近 5 次判断的简要记录 — 滚动更新，只保留最近 5 条]
| 日期 | 方向 | 综合分 | 简评 |
|------|------|-------|------|
```

### 行业页面 (industries/)

每个行业/板块一个文件，分析框架 + 当前观察。

```
---
industry: {行业名}
market: {CN|US}
last_updated: {日期}
---

## 行业特征
[核心驱动因子、周期特征、典型估值范围]

## 当前环境
[最近更新的行业级别判断]

## 代表个股
[候选池中属于该行业的股票列表]

## 分析框架
[该行业的差异化评分规则 — 参考 config/industry_frameworks.yaml]
```

### 策略页面 (strategies/)

经验总结和操作规则。格式自由，但需包含：
- 结论（一句话）
- 支撑数据（样本量、准确率、置信度）
- 适用条件（market、regime、时间段）
- 反例或失效条件

### 系统页面 (system/)

系统演进记录。格式自由。建议按时间顺序追加，不覆盖。

### 宏观页面 (macro/)

宏观环境综述。包含：
- 当前判断（更新日期）
- 关键指标值（PMI、利率、通胀等）
- 对分析的影响（对 regime 参数的调整建议）

### 交易记录 (trades/)

- `active_positions.md`：当前持仓列表，每次建仓/平仓后更新
- `trade_journal.md`：已平仓交易的复盘精选（只记录有学习价值的）

---

## 操作规则

1. **更新优先于新建**: 分析已有票时，更新现有页面，不创建新页面
2. **Actual/Archive 模式**: "当前状态"章节直接覆盖；"行为模式"和"历史判断"追加
3. **长度控制**: 每个页面不超过 300 行；超过时压缩历史部分
4. **交叉引用**: 用 `[[链接]]` 语法关联行业页面和策略页面
5. **每次更新后**: 更新 index.md 中该页面的摘要行
6. **数据新鲜度**: 所有分析结论必须标注日期；超过 30 天未更新的页面在 lint 时标记为 stale
7. **置信度标注**: 涉及统计规律时必须标注样本量 (n=X) 和适用条件

---

## 与数据库的关系

- Markdown 文件是内容的 source of truth
- PostgreSQL `wiki_pages` 表存储元数据（摘要、标签、向量嵌入）用于快速检索
- 每次写入文件后必须同步更新数据库（WikiManager.upsert_page_db）
- 向量嵌入用于 RAG 检索（search_experience），维度为 1024

---

## 目录结构说明

```
wiki/
├── index.md          # 全局索引（自动维护）
├── log.md            # 操作日志（append-only）
├── schema.md         # 本文件 — Wiki 约定和 LLM 指令
├── stocks/
│   ├── CN/           # A 股个股页面
│   └── US/           # 美股个股页面
├── industries/
│   ├── CN/
│   └── US/
├── strategies/       # 策略经验
├── system/           # 系统改进记录
├── macro/            # 宏观环境
└── trades/           # 交易记录
```
