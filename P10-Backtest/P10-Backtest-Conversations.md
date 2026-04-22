# P10-Backtest Claude Code 对话模板

> 4 轮对话完成整个回测系统。每周一轮，验收通过后进入下一轮。

---

## 第 1 轮：数据层（Week 1）

### 对话 1.1：项目初始化 + 数据库

```
我要开发一个股票系统回测项目 P10-Backtest，用于在真正开发完整的 P10-AlphaRadar 之前，
先验证核心分析逻辑是否有效。

附件:
1. CLAUDE.md（项目指令，放在项目根目录，重命名为 CLAUDE.md）
2. docs/P10-Backtest-Spec.md（完整技术规格）
3. .env.template（环境变量模板）
4. config/watchlist.yaml（候选池）

请首先阅读 CLAUDE.md 和 spec，理解这是一个严格要求 PIT（Point-in-Time）的回测项目。

本轮任务：
1. 创建项目目录结构（按 spec 第四节）
2. 编写 docker-compose.yml（PostgreSQL 15 + TimescaleDB）
3. 实现 db/schema.sql（完整建表 SQL，按 spec 第五节）
4. 实现 db/connection.py（asyncpg 连接池）
5. 创建 requirements.txt 和 pyproject.toml
6. 创建 config/settings.yaml、regime_params.yaml、industry_frameworks.yaml 的完整内容
7. 创建 scripts/01_init_db.py（读 schema.sql 建库）

验收：
- docker-compose up 能启动 PostgreSQL + TimescaleDB
- python scripts/01_init_db.py 能建所有表
- psql 连接后能看到所有 backtest schema 下的表
```

### 对话 1.2：PIT 数据加载器（核心）

```
Week 1 的核心任务：实现 PITDataLoader。这是整个回测系统最关键的文件，
任何 look-ahead bias 都会让回测结果失效。

本轮任务：
1. 实现 data/pit_loader.py（按 spec 6.1 节）
2. 为 PITDataLoader 编写完整单元测试 tests/test_pit_loader.py:
   - 插入测试数据: symbol X 在 2025-01-01 有一条 bars，available_date = 2025-01-01
   - 插入 symbol X 在 2025-02-01 的 bars，available_date = 2025-02-01
   - 设置 loader.set_date(2025-01-15)
   - loader.get_bars('X') 应该只返回 2025-01-01 的数据
   - 财务数据测试: announce_date 在 current_date 之后的财报应被过滤
   - 财务数据测试: report_date 在 current_date 之前但 announce_date 在之后，应被过滤
3. 实现 data/data_quality.py（数据完整性和新鲜度检查）

关键:
- PITDataLoader 的每个方法都要经过 available_date 过滤
- 唯一例外是 get_open_price()，它允许访问未来数据用于成交模拟

验收：
- 所有单元测试通过
- 代码 review: 确认每个数据查询都有 PIT 过滤
```

### 对话 1.3：数据拉取

```
本轮任务：实现历史数据拉取脚本。

1. 实现 data/fetchers/tushare_fetcher.py:
   - get_daily_bars(symbols, start_date, end_date) - 批量拉日线
   - get_daily_basic(symbols, start_date, end_date) - 基本面每日指标
   - get_financials(symbols) - 最近 12 季度财报（三张表）
     * income, balancesheet, cashflow, fina_indicator 表关联
     * 关键: 必须保留 ann_date (公告日) 字段
   - get_moneyflow(symbols, start_date, end_date) - 资金流
   - get_margin_detail(symbols, start_date, end_date) - 融资融券明细

2. 实现 data/fetchers/akshare_fetcher.py:
   - get_northbound_daily(start_date, end_date) - 北向资金
   - get_margin_market_daily - 两市融资余额总计
   - get_limit_updown_count - 涨跌停家数
   - get_advance_decline_count - 涨跌家数
   - get_index_daily(index_code) - 指数日线
   - get_industry_classify - 申万行业分类

3. 实现 data/fetchers/yfinance_fetcher.py:
   - get_daily_bars_us(symbols, start_date, end_date) - 美股日线
   - get_financials_us(symbol) - 季度财报
   - get_info_us(symbol) - 基本面快照
   - get_vix(start_date, end_date) - VIX 指数

4. 实现 scripts/02_fetch_data.py:
   - 从 watchlist.yaml 读取候选池
   - 时间范围: 2024-06-01 ~ 2026-04-17（前置 3 个月给长周期指标）
   - 调用各 fetcher 拉取数据，写入数据库
   - 关键: available_date 字段必须正确填充
     * market_bars_daily: available_date = trade_date
     * financials_quarterly: available_date = announce_date
     * moneyflow_daily: available_date = trade_date + 1 (T+1 可查)
   - 进度条和失败重试
   - 最后打印数据质量报告

注意:
- Tushare 有频率限制，批量拉取时每只票之间 sleep 0.2s
- yfinance 在国内可能需要代理，环境变量 HTTPS_PROXY
- 单只股票失败不要中断整个流程，记录到 failed_symbols 列表
- 财报 ann_date 字段可能有 null，需要 fallback 逻辑

验收：
- 执行 python scripts/02_fetch_data.py 后：
  * 所有候选池股票 2024-06-01 ~ 2026-04-17 日线完整
  * 最近 12 季度财报数据存在
  * 资金流、北向、融资融券数据完整
  * 指数数据（HS300、ZZ1000、SPY、QQQ、VIX）完整
- 执行数据质量检查脚本，无高危警告
```

### 对话 1.4：特征预计算

```
本轮任务：一次性预计算所有日线特征，存入 features_daily 表。
回测时直接从此表读取，避免重复计算。

1. 实现 core/features.py:
   - compute_all_features(symbol, start_date, end_date) -> DataFrame
   - 计算字段（按 spec 5.2 节）:
     * 均线: MA5, MA10, MA20, MA60, MA150, MA200
     * 均线斜率: MA20 和 MA60 的斜率
     * RSI 14
     * MACD (DIF, DEA, HIST)
     * ADX 14, +DI, -DI
     * ATR 14
     * 20 日历史波动率（年化）
     * 布林带 (upper, lower, width)
     * 多周期收益率 (1d, 5d, 20d, 60d)
     * 距 20 日 / 60 日最高价的距离
     * 20 日收盘位置百分比
     * 量能比 (5 日)
     * 20 日换手率分位
     * Weinstein Stage (按周线计算)
     * RS Rank 63 日
   - future 字段（只用于回填评估）:
     * future_ret_5d, future_ret_10d, future_ret_20d
     * future_max_up_20d, future_max_dd_20d

2. Weinstein Stage 按 spec 6.3 节实现
3. RS Rank 计算: 在 universe 内的百分位排名

4. 实现 scripts/03_compute_features.py:
   - 遍历所有 symbol
   - 批量写入 features_daily
   - 注意: future 字段在最后 20 个交易日可能为 null，这是正常的

注意:
- 优先使用 pandas-ta 库（如果 TA-Lib 安装困难）
- 技术指标计算有 NaN 是正常的（前 N 日）
- future_ret_* 从 bars 未来数据计算，但这些字段只在评估阶段使用

验收：
- features_daily 表充分填充
- 手工抽查几只股票某个日期的 MA、RSI、MACD 值，与 TradingView 或东方财富对比
- Stage 判别合理（抽查几个明显的 Stage 2 或 Stage 4 案例）
```

**Week 1 最终验收清单：**
- [ ] Docker 环境跑通
- [ ] 数据库 schema 完整
- [ ] PITDataLoader 单元测试全通过
- [ ] 历史数据拉取完毕，覆盖整个候选池
- [ ] features_daily 预计算完成
- [ ] 数据质量检查无高危问题

---

## 第 2 轮：分析模块（Week 2）

### 对话 2.1：Regime 四维度检测

```
Week 1 已验收。开始 Week 2: 分析模块。

本轮任务: 实现 Regime 检测。按 spec 6.7 节和 P10 架构文档 5.1 节实现。

1. 实现 core/regime/trend.py:
   - calc_trend_score(loader, market) -> float
   - A 股用 HS300 和 ZZ1000 等权
   - 美股用 SPY 和 QQQ 等权
   - 评分 = MA 排列完整度 (40%) + ADX 趋势强度 (30%) + 价格结构 (30%)

2. 实现 core/regime/volatility.py:
   - calc_volatility_score(loader, market) -> float
   - A 股: HV20(HS300) 在过去 250 日的百分位 * 0.6 + 涨跌停比 * 0.4
   - 美股: VIX 映射 * 0.6 + (如有) VIX/VIX3M 比 * 0.4

3. 实现 core/regime/breadth.py:
   - calc_breadth_score(loader, market) -> float
   - A 股: 5 日涨跌比均值 (33%) + 站上 MA20 股票占比 (33%) + 新高新低比 (33%)
   - 美股: 用简化替代（比如 SPY 相对 MA200 位置 + 成交量趋势）

4. 实现 core/regime/liquidity.py:
   - calc_liquidity_score(loader, market) -> float
   - A 股: 北向 20 日趋势 (40%) + 融资余额 20 日变化 (30%) + 市场成交额趋势 (30%)
   - 美股: VIX 作为反向代理 + SPY 成交额趋势

5. 实现 core/regime/detector.py:
   - detect_regime(loader, market) -> RegimeState
   - 四维度 → 2×2 矩阵 → regime_mode
   - 加载 regime_params.yaml 获取对应参数

6. 实现 config/regime_params.yaml:
   ```yaml
   offense:
     signal_threshold_adj: 1.0
     max_position_pct: 0.80
     confidence_factor: 1.0
     dimension_weights:
       technical: 0.35
       fundamental: 0.30
       flow: 0.20
       sentiment: 0.15
   cautious_offense:
     signal_threshold_adj: 0.9
     max_position_pct: 0.60
     ...
   defense:
     signal_threshold_adj: 0.8
     max_position_pct: 0.40
     ...
   risk_off:
     signal_threshold_adj: 0.7
     max_position_pct: 0.20
     ...
   ```

验收:
- 对 2026-01-15 这一天分别计算 CN 和 US 的 regime，输出四维度评分和 regime_mode
- 在 2025 年 10 月（A 股震荡上行）和 2026 年 3 月（贸易摩擦）这两个节点，
  regime 应该有显著不同
```

### 对话 2.2：技术面分析

```
本轮任务：技术面分析模块。

1. 实现 core/analysis/stage_detector.py:
   - detect_stage(weekly_bars) -> int (按 spec 6.3 节)
   - calc_rs_rank(loader, symbol, universe, period=63) -> float

2. 实现 core/analysis/technical.py:
   - resample_to_weekly(daily_bars) -> weekly_bars (用 pandas resample)
   - analyze_timeframe(features, bars, timeframe) -> TimeframeAnalysis
   - analyze_technical(loader, symbol, universe) -> TechnicalAnalysis

3. 关键计算:
   - MA 排列完整度: 检查 MA5 > MA20 > MA60 > MA150 > MA200 的顺序
     每满足一对 +1 分, 共 4 对 = 4 分, 归一化到 0-40
   - ADX 趋势强度: 映射 ADX 值到 0-30 分
   - 价格结构: 统计近 20 日的 higher high + higher low 数量, 归一化 0-30
   - 综合 strength = MA 分 + ADX 分 + 价格结构分

4. 多周期方向综合（spec 6.2 节）:
   - 日线和周线共振 → 高置信度
   - 矛盾 → 以周线为主 + 降低置信度

5. TimeframeAnalysis 输出:
   - trend: 'up' | 'down' | 'sideways'
   - stage: 1/2/3/4
   - strength: 0-100
   - ma_alignment, rs_rank, momentum
   - key_levels: 支撑阻力位（用近 60 日的局部高低点）

验收:
- 对贵州茅台 (600519.SH) 在 2026-01-15 运行 analyze_technical:
  * 输出 daily 和 weekly 的 TimeframeAnalysis
  * strength 评分合理（0-100 范围，不是极端值）
  * Stage 判别与肉眼看周线图一致
  * RS Rank 合理
- 对一只下跌趋势明显的股票测试, trend 应为 'down', stage 应为 4
```

### 对话 2.3：基本面分析

```
本轮任务：基本面分析模块。

1. 实现 config/industry_frameworks.yaml:
   ```yaml
   consumer_staples:   # 消费
     profitability: 0.35
     growth: 0.25
     valuation: 0.25
     health: 0.15
   technology:         # 科技
     profitability: 0.20
     growth: 0.40
     valuation: 0.25
     health: 0.15
   financials:         # 金融
     profitability: 0.30
     growth: 0.15
     valuation: 0.30
     health: 0.25
   cyclical:           # 周期
     profitability: 0.20
     growth: 0.20
     valuation: 0.35
     health: 0.25
   healthcare:         # 医药
     profitability: 0.25
     growth: 0.40
     valuation: 0.20
     health: 0.15
   default:
     profitability: 0.30
     growth: 0.25
     valuation: 0.25
     health: 0.20
   ```

2. 实现 data/industry_mapper.py:
   - 根据申万一级行业名称映射到 industry_frameworks.yaml 中的框架
   - 映射规则（写在代码里）:
     * 食品饮料/农林牧渔/家用电器 → consumer_staples
     * 电子/计算机/通信/传媒 → technology
     * 银行/非银金融/房地产 → financials
     * 有色金属/化工/钢铁/建筑材料/采掘 → cyclical
     * 医药生物 → healthcare
     * 其他 → default

3. 实现 core/analysis/fundamental.py 按 spec 6.4 节:
   - calc_profitability_score (ROE + 毛利率稳定性 + OCF/NP)
   - calc_growth_score (营收 YoY + 净利 YoY + 增速趋势)
   - calc_valuation_score (PE 行业分位 + PE 历史分位 + PB 行业分位)
     ⚠️ 注意: valuation 是越低估值越高分（100 = 最便宜）
   - calc_health_score (负债率 + 流动比率 + 商誉比)

4. 辅助函数:
   - _sigmoid_score: 把一个值映射到 0-100
   - _percentile_rank: 计算历史分位
   - _get_industry_pe_percentile: 个股 PE 在同行业中的分位

验收:
- 对贵州茅台（消费框架）在 2026-01-15 运行分析
  * 四个分维度分数都在合理范围
  * 综合 score 按消费框架加权
- 对工商银行（金融框架）和某只科技股测试，框架应用正确
- 一个财务健康差的股票, health_score 应明显低
```

### 对话 2.4：资金面 + 市场情绪

```
本轮任务：资金面和市场级情绪。

1. 实现 core/analysis/flow.py 按 spec 6.5 节:
   - analyze_flow(loader, symbol, market) -> FlowAnalysis
   - A 股:
     * main_flow_score: 近 5 日大单净流入总额 / 市值, sigmoid 映射
     * northbound_score: 大盘北向 20 日净流入趋势
     * margin_score: 个股或市场融资余额变化趋势
   - 美股: 暂用成交量趋势代理（后续可扩展）

2. 实现 core/analysis/sentiment_market.py 按 spec 6.6 节:
   - analyze_market_sentiment(loader, market) -> float
   - A 股:
     * 涨跌比 5 日均值 (40%)
     * 涨停跌停比 (30%)
     * 融资余额趋势 (30%)
   - 美股:
     * VIX 映射（VIX 越低越乐观）

3. 注意: market_sentiment 不依赖 symbol，只依赖 market
   整个市场一个情绪分数，所有股票共用

验收:
- 对 2026-01-15 的 A 股和美股分别计算市场情绪
- 2025-09 / 2025-12 / 2026-03 三个时间点的 sentiment 应有明显差异
- 资金面评分在极端资金净流入/流出的股票上有区分度
```

**Week 2 最终验收清单：**
- [ ] Regime 四维度检测能运行，输出合理
- [ ] 技术面分析对多只股票测试符合直觉
- [ ] 基本面分析按行业框架正确加权
- [ ] 资金面和市场情绪可独立运行
- [ ] 所有模块通过单独的 test case

---

## 第 3 轮：回测引擎（Week 3）

### 对话 3.1：综合判断 + 交易建议

```
Week 2 已验收。开始 Week 3: 回测引擎。

本轮任务：综合判断和交易建议。

1. 实现 core/analysis/composite.py 按 spec 6.8 节:
   - generate_judgment(loader, symbol, market, universe) -> Judgment
   - 按 regime 权重加权四维度
   - 方向判断: composite > 65 → bullish, < 40 → bearish, 其他 → neutral
   - 置信度 = base_conf * tech.confidence_adj * regime.confidence_factor

2. 实现 generate_trade_suggestion:
   - 基于 ATR 计算止损（2 倍 ATR）和目标（4 倍 ATR，2:1 盈亏比）
   - 入场区间: 当前价 ±1%
   - 仓位建议: min(max_pos_pct, confidence * max_pos_pct)

3. Judgment 数据类:
   ```python
   @dataclass
   class Judgment:
       symbol, market, judgment_date
       technical_score, fundamental_score, flow_score, sentiment_score
       composite_score
       direction, confidence
       regime_mode, regime_snapshot
       suggested_action, entry_price
       entry_zone_low, entry_zone_high
       stop_loss, target_price
       suggested_size_pct
       signal_sources: dict  # 详细的触发来源
   ```

4. 实现 save_judgment(judgment, run_id, db) - 写入数据库

验收:
- 对候选池中每只股票在 2026-01-15 生成 judgment
- 输出格式正确，各字段合理
- 同一只股票在 regime='offense' 和 'risk_off' 下建议仓位应明显不同
```

### 对话 3.2：Portfolio + 交易规则

```
本轮任务：模拟账户。

1. 实现 backtest/portfolio.py 按 spec 7.2 节:
   - Position 类: 持仓对象
   - Trade 类: 已平仓交易记录
   - Portfolio 类:
     * open_position: 建仓（含佣金计算）
     * close_position: 平仓（含佣金计算）
     * update_positions_value: 每日末更新持仓市值
     * value: 总市值（cash + 持仓）
     * position_pct: 仓位占比

2. 佣金规则:
   - A 股: 双边 0.2%（含印花税、佣金、过户费）
   - 美股: 双边 0.1%
   - 整股规则: A 股整百股, 美股整 1 股

3. 实现 backtest/rules.py 按 spec 7.3 节:
   - check_exit(position, judgments, current_date) -> Optional[str]
     * 返回值: 'stop_loss' | 'target_hit' | 'direction_flip' | 'timeout' | None
   - calc_position_size: 基于止损反算（单笔最大亏损 2%）
   - check_industry_concentration: 同行业持仓不超过 40%
   - check_liquidity: 单日成交额 > 建仓金额 10 倍

4. 关键原则:
   - 所有成交价用 T+1 开盘价（通过 loader.get_open_price）
   - 止损和目标用日内最低/最高判断是否触发，但成交价取止损位/目标位
   - 平仓优先于建仓（先检查所有现有持仓是否需要平仓，再考虑新建仓）

验收:
- 单元测试:
  * 建仓一只股票，cash 减少正确
  * 平仓计算 P&L 正确
  * 触发止损成交价为止损位
  * 触发目标成交价为目标位
  * 仓位超限时建仓被拒绝
```

### 对话 3.3：回测引擎主循环

```
本轮任务：回测引擎主逻辑。

1. 实现 backtest/engine.py 按 spec 7.1 节:
   - BacktestEngine.__init__: 初始化 loader, portfolio, benchmarks
   - BacktestEngine.run: 主循环
   - BacktestEngine._process_day(date): 单日处理
     * 步骤 1: 更新 regime
     * 步骤 2: 对 universe 每只票生成 judgment
     * 步骤 3: 执行交易（T+1 开盘价）
     * 步骤 4: 记录账户快照
     * 步骤 5: 更新对照组

2. 实现 backtest/execution.py:
   - TradeExecutor.execute_exits: 平仓执行
   - TradeExecutor.execute_entries: 建仓执行
   - 按 composite_score 排序，从高到低建仓
   - 处理各种异常情况（停牌、流动性不足、仓位上限）

3. 数据持久化:
   - 每个 judgment 写入 backtest_judgments
   - 每笔 trade 写入 backtest_trades
   - 每日持仓快照写入 backtest_positions
   - 每日账户市值写入 backtest_portfolio_daily
   - 每日 regime 写入 backtest_regime_daily

4. 实现 scripts/04_run_backtest.py:
   - 从命令行接收 config 参数（可选）
   - 创建 backtest_run 记录
   - 调用 engine.run()
   - 最后打印汇总（总收益、交易次数、关键指标）

5. 进度监控:
   - 每处理一个月打印一次进度和关键指标
   - 使用 tqdm 显示进度条

验收:
- python scripts/04_run_backtest.py 能跑完 2025-09-01 ~ 2026-04-17 不报错
- 期间 regime 有切换
- 交易记录合理（不会每天都买卖）
- 账户总市值变化符合预期
```

### 对话 3.4：对照组

```
本轮任务：三个对照组。

1. 实现 backtest/benchmarks.py 按 spec 7.4 节:
   - Benchmarks 类管理三个对照组

2. 对照组 1 - Buy & Hold 指数:
   - 2025-09-01 用全部初始资金买入 HS300（CN）和 SPY（US）
   - 持有到 2026-04-17
   - 每日更新市值

3. 对照组 2 - 等权候选池:
   - 2025-09-01 把资金等权分配买入 universe 所有股票
   - 不做任何调仓
   - 每日更新市值

4. 对照组 3 - 简单动量策略:
   - 每周一调仓（如果周一是非交易日则顺延）
   - 选过去 20 个交易日涨幅最大的 5 只票等权持有
   - 下次调仓时全部卖出重新买入
   - 考虑交易成本

5. 所有对照组的逻辑必须用 PITDataLoader 数据（符合 PIT 原则）

6. 在 BacktestEngine._process_day 最后调用 benchmarks.update(current_date)
   同步更新三个对照组的每日市值

7. 三个对照组的市值存入 backtest_portfolio_daily 的对应字段

验收:
- 三个对照组的累计收益曲线都能生成
- Buy & Hold 沪深300 的收益曲线和公开的沪深300 指数走势一致
- 动量策略能看到周度换股痕迹
```

**Week 3 最终验收清单：**
- [ ] 能从头到尾跑完整个回测周期
- [ ] 生成完整的判断记录、交易记录、持仓快照
- [ ] 三个对照组数据完整
- [ ] 账户市值变化合理，无明显 bug（如某日市值突然翻倍）
- [ ] 初步看交易次数合理（不是每天都交易，也不是一笔都没有）

---

## 第 4 轮：评估与报告（Week 4）

### 对话 4.1：评估指标

```
Week 3 已验收。开始 Week 4: 评估与报告。

本轮任务：完整评估指标。

1. 实现 evaluation/metrics.py 按 spec 8.1 节:
   - calc_metrics(run_id, db) -> BacktestMetrics
   - 所有字段按 BacktestMetrics dataclass 计算

2. 关键指标实现:
   - 累计收益: (final - initial) / initial
   - 年化收益: (1 + total)^(252/days) - 1
   - 年化波动: daily_return.std() * sqrt(252)
   - 最大回撤: 用 cummax - value 计算
   - 最大回撤持续天数: 从 peak 到 recover 的天数
   - Sharpe: (年化收益 - 3%) / 年化波动 (无风险利率用 3%)
   - Sortino: (年化收益 - 3%) / 下行波动
   - Calmar: 年化收益 / 最大回撤

3. 交易统计:
   - 胜率 = 盈利交易数 / 总交易数
   - 盈亏比 = 平均盈利 / 平均亏损（都取绝对值）
   - 平均持仓天数

4. vs 基准:
   - Alpha = 系统收益 - 基准收益
   - Beta = cov(系统日收益, 基准日收益) / var(基准日收益)

5. IC 分析（按 spec 8.2 节）:
   - 对每个维度（tech/fund/flow/sent/composite）:
     * 取所有 judgments 的 score 和 future_ret_5d/10d/20d
     * 计算 Spearman rank correlation
   - 计算 IR = IC 均值 / IC 标准差（按月分组后）

6. 按 regime 分层:
   - 分别在 offense/cautious_offense/defense/risk_off 四种 regime 下
     计算累计收益和天数占比

7. 按月分层:
   - 每月累计收益

验收:
- 对现有回测结果运行 calc_metrics，打印所有指标
- IC 数值在合理范围（不应该 > 0.3 或 < -0.3，除非有 bug）
- Sharpe 在合理范围（0-3）
- 按 regime 分层的收益之和约等于总收益
```

### 对话 4.2：归因分析

```
本轮任务：归因分析（为什么对/为什么错）。

1. 实现 evaluation/attribution.py:
   - analyze_winning_trades(run_id, db) -> list[TradeAnalysis]
     * 选出盈利最多的 10 笔交易
     * 对每笔分析: 触发时的 regime、四维度评分、主要触发因素
   - analyze_losing_trades(run_id, db) -> list[TradeAnalysis]
     * 选出亏损最多的 10 笔交易
     * 自动归因分类:
       - 基本面出错: 如果 fund_score > 65 但实际跌
       - 时机问题: 如果 tech_score 高但 regime 已切换
       - 外部事件: 交易期内有单日跌幅 > 5%（推测突发事件）
       - Regime 切换: 持仓期间 regime 从 offense 切到 defense

2. 维度有效性分析:
   - analyze_dimension_effectiveness(run_id, db)
   - 对每个维度:
     * 将所有 judgment 按该维度 score 分成 5 层（quintile）
     * 统计每层的平均未来 10 日收益
     * 如果从低到高单调递增，说明该维度有效
     * 如果各层收益差异不显著，说明该维度无效

3. 规则有效性分析:
   - analyze_exit_reasons(run_id, db)
   - 统计不同平仓原因下的平均收益:
     * stop_loss: 平均亏损多少
     * target_hit: 平均盈利多少
     * direction_flip: 若不翻转是否能盈利更多
     * timeout: 超时平仓的收益分布

验收:
- 运行归因分析，输出典型成功/失败案例
- 维度分层分析能区分出有效/无效维度
```

### 对话 4.3：报告生成

```
本轮任务：生成可读的报告。

1. 实现 evaluation/reporter.py:
   - generate_markdown_report(run_id, db) -> str
   - generate_excel_report(run_id, db, output_path)
   - generate_charts(run_id, db, output_dir)

2. Markdown 报告结构（按 spec 8.3.1）:
   - 回测周期 + 配置概要
   - 核心指标表格（系统 vs 三个对照组）
   - 判断准确率汇总
   - 交易统计
   - 各维度 IC 分析表
   - 按 regime 分层表现
   - 按月表现折线图
   - 典型成功/失败案例分析（3 个成功 + 3 个失败）
   - 结论与建议

3. Excel 报告（按 spec 8.3.2）:
   - Sheet 1 - Summary: 所有核心指标
   - Sheet 2 - Daily Returns: 每日收益明细
   - Sheet 3 - All Judgments: 所有判断
   - Sheet 4 - All Trades: 所有交易
   - Sheet 5 - Regime Timeline: regime 变化
   - Sheet 6 - IC by Dimension: IC 详细
   - Sheet 7 - Monthly Breakdown: 月度分解

4. 图表:
   - 累计净值曲线（系统 vs 三个对照组，一张图）
   - 回撤曲线
   - 月度收益热力图
   - 各维度评分分布直方图
   - Regime 时间线 + 叠加账户净值

5. 实现 scripts/05_generate_report.py:
   - 接收 run_id 参数（默认最新）
   - 生成所有报告到 reports/{timestamp}/

验收:
- 生成 reports/{ts}/backtest.md（Markdown 报告）
- 生成 reports/{ts}/backtest.xlsx（Excel 详细）
- 生成 reports/{ts}/charts/ 下的 5 张图
- 报告内容完整、图表清晰
```

### 对话 4.4：首次完整运行 + 迭代

```
本轮任务：跑完整个回测，诊断问题，迭代。

1. 完整运行:
   python scripts/01_init_db.py   # 如果需要重建
   python scripts/02_fetch_data.py
   python scripts/03_compute_features.py
   python scripts/04_run_backtest.py
   python scripts/05_generate_report.py

2. 检查结果合理性:
   检查点 A - 数据完整性:
   - [ ] 所有候选池股票都有判断记录
   - [ ] 没有长期（> 3 天）未更新的数据
   - [ ] regime 在整个周期内有 2+ 次切换

   检查点 B - 交易合理性:
   - [ ] 交易总数在合理范围（20-100 笔，太少说明系统不敏感，太多说明过度交易）
   - [ ] 没有明显 bug（如某日突然全部买入或全部卖出）
   - [ ] 止损和目标触发比例合理（不应该 90% 都止损）

   检查点 C - 收益合理性:
   - [ ] 系统累计收益在合理范围（绝对值 < 50%，这 7.5 个月不应该暴涨暴跌）
   - [ ] 如果远超基准（alpha > 20%），先怀疑有 look-ahead bias
   - [ ] 最大回撤 < 25%

   检查点 D - IC 合理性:
   - [ ] 综合 IC 在 -0.1 ~ +0.1 范围
   - [ ] 至少 1-2 个维度 IC > 0.03
   - [ ] 不应出现 |IC| > 0.2（太高说明可能有问题）

3. 发现问题后的处理:
   - 如果发现 look-ahead bias: 立即停止, 定位问题, 修复后重跑
   - 如果发现某个维度 IC = 0: 检查该维度的评分逻辑
   - 如果发现所有判断都是 bullish 或都是 bearish: 检查 regime 参数和阈值
   - 如果交易过于频繁: 调整 direction_flip 阈值
   - 如果收益过高: 严肃怀疑, 重点检查 execution.py 的 PIT 处理

4. 输出最终报告并解读

验收:
- 完整回测报告生成
- 报告中的结论清晰
- 任何明显异常已修复或已记录
- 有明确的"下一步建议"（哪些应该保留, 哪些应该调整）
```

**Week 4 最终验收清单：**
- [ ] 完整报告生成（Markdown + Excel + 图表）
- [ ] IC 分析清晰呈现各维度有效性
- [ ] 归因分析有代表性案例
- [ ] 明确结论：P10 的核心逻辑是否有效
- [ ] 有基于数据的下一步建议

---

## 整体交付包

发给 Claude Code 的完整文件:
1. `CLAUDE.md`（项目指令，改名自 CLAUDE-Backtest.md）
2. `docs/P10-Backtest-Spec.md`（完整规格）
3. `.env.template`
4. `config/watchlist.yaml`（你填入具体股票）
5. 本文档（对话模板，供你自己使用）

各周对话按顺序发送，每周验收通过再进入下一周。

---

## 结果解读指南

回测完成后，重点看以下指标并按下面的标准判断：

### 综合评分 IC
- `IC < 0.02`: 系统整体无效，需要大改
- `IC 0.02-0.05`: 弱有效，可以继续开发但要持续优化
- `IC 0.05-0.10`: 有效，可以继续完整 P10 开发
- `IC > 0.10`: 看起来很好，但要怀疑过拟合

### Alpha vs 三个对照组
理想情况: 对所有三个对照组都有正 alpha
合理情况: 至少对 2 个对照组有正 alpha
警告情况: 只对 Buy & Hold 有正 alpha（说明只是简单择股，没有真正的 alpha）
失败情况: 对所有对照组都跑输

### 维度有效性
如果某个维度的 IC < 0.02，可以考虑在完整 P10 中降低其权重或重新设计
如果情绪面 IC 很低，可能是预期之中（情绪指标本身噪音大）
如果技术面 IC 很低，需要严肃重新审视

### 按 Regime 表现
理想: 所有 regime 下 alpha 都为正
合理: 至少在 defense/risk_off 下 alpha 为正（说明风控有价值）
警告: 只在 offense 下 alpha 为正（说明只是牛市跟风）

### 最终决策
- 如果核心指标全部达标 → 继续 P10 开发，投入 12-14 周
- 如果部分达标 → 继续开发但针对无效模块做大改
- 如果全部不达标 → 重新设计，或者考虑简化目标
