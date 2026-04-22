# P10-Backtest

## 候选池独立性说明

`config/watchlist-backtest.yaml` 是本子项目**专用**的回测候选池，与主项目 `stock_universe` 表**有意保持独立**。

原因：回测需要固定的历史股票列表（PIT 原则），不应随主项目 watchlist 的动态增删而变化。
如需同步主项目 watchlist 到回测，请手动编辑本文件，并在 `docs/` 中记录原因和日期。

主项目 watchlist 的唯一事实源是 `stock_universe` 表，通过 `db.universe.get_active_symbols()` 读取。

---

## 项目概述
P10-AlphaRadar 的前置回测验证系统。目标：在投入完整 P10 开发之前（12-14 周），先用 4 周时间验证核心分析逻辑在历史数据上是否有效。

## 核心设计文档
完整技术规格见 `docs/P10-Backtest-Spec.md`，开发前必须通读。

## 技术栈
- Python 3.11+
- PostgreSQL 15 + TimescaleDB（本地 Docker 部署）
- Tushare Pro（A 股主力数据源）
- AkShare（A 股辅助数据）
- yfinance（美股数据）
- pandas, numpy, scipy
- TA-Lib 或 pandas-ta（技术指标）
- matplotlib + plotly（图表）
- openpyxl（Excel 报告）
- asyncpg + raw SQL（不用 ORM）
- pydantic + YAML（配置）
- structlog（日志）

## 最重要的开发原则

### 严格 PIT（Point-in-Time）
这是回测的生死线。任何一处违反 PIT 原则都会导致结果虚高、无法采信。

**铁律**:
1. 所有数据查询必须通过 `PITDataLoader` 类，禁止直接访问表
2. 每张数据表都要有 `available_date` 字段
3. 财报必须用 `announce_date` 过滤，不是 `report_date`
4. 生成 T 日判断时只能用 T-1 及之前的数据（特例: 当日收盘后运行的批处理可用 T 日数据）
5. 成交价必须用 T+1 日开盘价，不能用 T 日收盘价

**代码审查时必查**:
- 任何 `SELECT ... FROM market_bars_daily` 不走 PITDataLoader 的代码 = 高危
- 任何使用 `future_ret_*` 字段的代码 = 必须在评估/回填模块内
- 任何 `LIMIT 1 ORDER BY trade_date DESC` 没有 `available_date <=` 过滤 = 高危

### 数据拉取的数量控制
- A 股候选池 30-50 只（用户在 watchlist.yaml 中指定）
- 美股候选池 10-20 只
- 时间范围: 2024-06-01 ~ 2026-04-17（比回测窗口前置 3 个月，用于计算 MA150/200 等长周期指标）
- 不要盲目拉取全市场数据，太慢太贵

## 开发规范

### 代码风格
- 所有函数必须有完整 type hints 和 Google style docstring
- 异步函数用 async/await，数据库操作必须异步
- 所有外部调用（API、DB）有 try/except + 日志
- 日志用 structlog，JSON 格式，包含 symbol/date/module 上下文

### 数据库
- 连接池: asyncpg.create_pool，min=2, max=10
- 所有 SQL 用参数化查询
- 大批量写入用 COPY 协议
- schema 用 `backtest`，与未来 P10 生产数据隔离

### 配置
- 敏感信息从 .env 读取
- 业务参数（regime 阈值、维度权重）从 YAML 读取
- 每次回测运行保存 config_snapshot 到数据库

### 测试
- PITDataLoader 必须有单元测试，验证 look-ahead 防护
- 各评分函数用已知数据测试边界情况
- 回测引擎先用 1 周数据验证，再跑完整周期

## 目录结构
```
P10-Backtest/
├── CLAUDE.md                      # 本文件
├── docs/
│   └── P10-Backtest-Spec.md
├── config/
│   ├── settings.yaml
│   ├── watchlist.yaml             # 用户提供
│   ├── regime_params.yaml
│   └── industry_frameworks.yaml
├── db/
│   ├── schema.sql
│   └── connection.py
├── data/
│   ├── fetchers/
│   ├── pit_loader.py              # ⚠️ 核心
│   └── data_quality.py
├── core/
│   ├── regime/
│   ├── analysis/
│   └── features.py
├── backtest/
│   ├── engine.py
│   ├── portfolio.py
│   ├── execution.py
│   ├── benchmarks.py
│   └── rules.py
├── evaluation/
│   ├── metrics.py
│   ├── attribution.py
│   └── reporter.py
├── scripts/
│   ├── 01_init_db.py
│   ├── 02_fetch_data.py
│   ├── 03_compute_features.py
│   ├── 04_run_backtest.py
│   └── 05_generate_report.py
├── reports/                       # gitignored
├── tests/
├── docker-compose.yml
├── requirements.txt
└── pyproject.toml
```

## 开发顺序（严格按 Week 执行）

### Week 1: 数据层
1. Docker 环境 + schema.sql
2. PITDataLoader 实现（核心，必须有测试）
3. TushareFetcher（拉 A 股日线、财报、资金流、基本面）
4. AkShareFetcher（拉北向、融资、涨跌停、指数）
5. YFinanceFetcher（美股）
6. features_daily 预计算

验收标准:
- [ ] 候选池所有股票都有 2024-06-01 ~ 2026-04-17 的完整日线
- [ ] 财务数据 announce_date 字段准确
- [ ] features_daily 表充分填充，包括 future_ret_* 字段
- [ ] PITDataLoader 单元测试通过（验证不会泄露未来数据）

### Week 2: 分析模块
1. Regime 四维度 + 映射
2. 技术面（多周期 + Stage + RS Rank）
3. 基本面（行业差异化）
4. 资金面 + 市场级情绪

验收标准:
- [ ] 对任意一只股票某个日期，所有模块能正常输出评分
- [ ] 手工抽查 3-5 只票，评分结果符合直觉（不离谱）
- [ ] 各模块独立测试用例通过

### Week 3: 回测引擎
1. 综合判断（composite）
2. Portfolio + 交易规则
3. BacktestEngine 主循环
4. 三个对照组

验收标准:
- [ ] 能跑完整个 7.5 个月周期不崩溃
- [ ] judgments、trades、portfolio_daily 三张表有完整数据
- [ ] 对照组数据同步更新

### Week 4: 评估与报告
1. 全套评估指标
2. Markdown 报告
3. Excel 详细报告
4. 图表
5. 首次完整运行 + 迭代

验收标准:
- [ ] Markdown 报告信息完整，图表清晰
- [ ] IC 计算方法正确（Spearman rank correlation）
- [ ] 能识别并报告"IC < 0.02 的无效维度"
- [ ] 典型案例分析有意义

## 不要做什么

- **不要实现 LLM 集成**（回测不需要）
- **不要实现 Telegram**（回测不需要）
- **不要实现 Wiki**（回测不需要）
- **不要实现前端看板**（报告是 Markdown + Excel 即可）
- **不要做盘中 15 分钟信号**（本回测是日频）
- **不要做社交情绪**（StockTwits/股吧 历史数据不可靠）
- **不要过度优化参数**（容易过拟合）
- **不要为了好看的数字修改评分逻辑**（如果结果不好，要找真实原因）

## 遇到问题时

如果数据拉取失败：
- 检查 token 是否配置
- 检查是否超出 Tushare 的频率限制
- 对单个股票重试 3 次，失败则记录并跳过

如果 PIT 检查发现潜在 look-ahead：
- 立即停止开发，修复后再继续
- 不要用"下次再改"的心态放过

如果回测结果明显不合理（alpha 超过 20% 或低于 -20%）：
- 第一反应是"系统有 bug"，而不是"系统很强"
- 逐一检查: PIT 过滤、成交价逻辑、佣金计算、复权处理

## 关键文件提醒

- `data/pit_loader.py` - 必须最小心对待的文件，改动要慎重
- `backtest/execution.py` - 成交逻辑，决定回测可信度
- `evaluation/metrics.py` - 指标计算，错了会误导决策
