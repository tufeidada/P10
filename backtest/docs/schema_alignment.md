# Schema 字段对照表

> 生成日期: 2026-04-18  
> 对比基准: P10-Backtest-Spec.md (v1.0) vs P10 实际 `db/schema.sql` + `01_migrate_schema.py` 执行结果  
> 标注说明: ✅ 完全匹配 | 🔄 需重命名/适配 | ❌ 缺失，需新增 | ➕ P10 多出（无害） | ⚠️ 有 PIT 风险

---

## 1. market_bars_daily

| Spec 字段 | Spec 类型 | P10 实际字段 | P10 类型 | 状态 | 备注 |
|-----------|-----------|-------------|---------|------|------|
| symbol | VARCHAR(20) | symbol | VARCHAR(20) | ✅ | |
| market | VARCHAR(10) | market | VARCHAR(10) | ✅ | P10 DEFAULT 'CN' |
| trade_date | DATE | trade_date | DATE | ✅ | 分区键（hypertable） |
| open | NUMERIC(14,4) | open | NUMERIC(12,4) | ✅ | 精度差无影响 |
| high | NUMERIC(14,4) | high | NUMERIC(12,4) | ✅ | |
| low | NUMERIC(14,4) | low | NUMERIC(12,4) | ✅ | |
| close | NUMERIC(14,4) | close | NUMERIC(12,4) | ✅ | |
| volume | BIGINT | volume | BIGINT | ✅ | |
| amount | NUMERIC(20,2) | amount | NUMERIC(18,2) | ✅ | |
| adj_factor | NUMERIC(12,6) | adj_factor | NUMERIC(10,6) | ✅ | |
| adj_close | NUMERIC(14,4) | adj_close | NUMERIC(14,4) | ✅ | 01_migrate 已 ADD：`ROUND(close*adj_factor,4)` |
| turnover_rate | NUMERIC(8,4) | turnover_rate | NUMERIC(8,4) | 🔄 | 01_migrate 已 ADD 但值为 NULL，需从 `turnover` 列或 Tushare 回填 |
| available_date | DATE | available_date | DATE | ✅ | 01_migrate 已 ADD & 回填：= trade_date |
| _(无)_ | — | turnover | NUMERIC(8,4) | ➕ | P10 原字段，含义同 turnover_rate；02_fetch 脚本回填时 `SET turnover_rate = turnover` 即可 |

**处理建议：**
- `turnover_rate` 回填：在 `scripts/02_fetch_missing_data.py` 末尾执行：
  ```sql
  UPDATE market_bars_daily
  SET turnover_rate = turnover
  WHERE turnover_rate IS NULL AND turnover IS NOT NULL;
  ```
  如果 Tushare 返回的字段名是 `turnover_rate`（而非 `turnover`），则直接写入新列；美股从 yfinance 无换手率，保持 NULL。

---

## 2. features_daily

| Spec 字段 | Spec 类型 | P10 实际字段 | P10 类型 | 状态 | 备注 |
|-----------|-----------|-------------|---------|------|------|
| symbol | VARCHAR(20) | symbol | VARCHAR(20) | ✅ | |
| trade_date | DATE | trade_date | DATE | ✅ | 分区键 |
| **均线** | | | | | |
| ma5 | NUMERIC(14,4) | ma5 | NUMERIC(12,4) | ✅ | |
| ma10 | NUMERIC(14,4) | ma10 | NUMERIC(12,4) | ✅ | |
| ma20 | NUMERIC(14,4) | ma20 | NUMERIC(12,4) | ✅ | |
| ma60 | NUMERIC(14,4) | ma60 | NUMERIC(12,4) | ✅ | |
| ma150 | NUMERIC(14,4) | ma150 | NUMERIC(12,4) | ✅ | |
| ma200 | NUMERIC(14,4) | ma200 | NUMERIC(12,4) | ✅ | |
| ma20_slope | NUMERIC(10,6) | ma20_slope | NUMERIC(10,6) | ✅ | |
| ma60_slope | NUMERIC(10,6) | _(无)_ | — | ❌ | P10 只有 `ma5_slope`；需在 `03_compute_features.py` 新增计算 |
| **动量** | | | | | |
| rsi_14 | NUMERIC(8,4) | rsi_14 | NUMERIC(8,4) | ✅ | |
| macd_dif | NUMERIC(12,6) | macd_dif | NUMERIC(10,6) | ✅ | |
| macd_dea | NUMERIC(12,6) | macd_dea | NUMERIC(10,6) | ✅ | Spec 写 `macd_dea`，P10 同名 |
| macd_hist | NUMERIC(12,6) | macd_hist | NUMERIC(10,6) | ✅ | |
| adx_14 | NUMERIC(8,4) | adx_14 | NUMERIC(8,4) | ✅ | |
| plus_di | NUMERIC(8,4) | plus_di | NUMERIC(8,4) | ✅ | |
| minus_di | NUMERIC(8,4) | minus_di | NUMERIC(8,4) | ✅ | |
| **波动率** | | | | | |
| atr_14 | NUMERIC(14,4) | atr_14 | NUMERIC(12,4) | ✅ | |
| hv_20 | NUMERIC(10,6) | hv_20 | NUMERIC(8,4) | ✅ | |
| boll_upper | NUMERIC(14,4) | boll_upper | NUMERIC(12,4) | ✅ | |
| boll_lower | NUMERIC(14,4) | boll_lower | NUMERIC(12,4) | ✅ | |
| boll_width | NUMERIC(10,6) | boll_width | NUMERIC(8,4) | ✅ | |
| **收益率** | | | | | |
| ret_1d | NUMERIC(10,6) | ret_1d | NUMERIC(8,6) | ✅ | |
| ret_5d | NUMERIC(10,6) | ret_5d | NUMERIC(8,6) | ✅ | |
| ret_20d | NUMERIC(10,6) | ret_20d | NUMERIC(8,6) | ✅ | |
| ret_60d | NUMERIC(10,6) | _(无)_ | — | ❌ | 需在 `03_compute_features.py` 新增：`(close/close_63d_ago - 1)` |
| **结构** | | | | | |
| dist_20d_high | NUMERIC(10,6) | _(无)_ | — | ❌ | `(close/max_high_20d - 1)`，负值；需新增 |
| dist_60d_high | NUMERIC(10,6) | _(无)_ | — | ❌ | `(close/max_high_60d - 1)`；需新增 |
| pct_in_20d_range | NUMERIC(8,4) | _(无)_ | — | ❌ | `(close-min_20d)/(max_20d-min_20d)`，0~1；需新增 |
| **量能** | | | | | |
| vol_ratio_5d | NUMERIC(10,4) | vol_ratio_5d | NUMERIC(8,4) | ✅ | |
| turnover_rank_20d | NUMERIC(8,4) | turnover_rank_20d | NUMERIC(8,4) | ✅ | |
| **Stage / RS** | | | | | |
| stage | SMALLINT | stage | SMALLINT | ✅ | Weinstein 1-4 |
| rs_rank_63d | NUMERIC(8,4) | rs_rank | NUMERIC(8,4) | 🔄 | P10 列名为 `rs_rank`；pit_loader 查询时 `AS rs_rank_63d` 别名即可，无需 ALTER |
| **PIT 控制** | | | | | |
| available_date | DATE | available_date | DATE | ✅ | 01_migrate 已 ADD & 回填：= trade_date |
| **P10 额外列** | | | | | |
| _(无)_ | — | ma5_slope | NUMERIC(10,6) | ➕ | 可保留供 pit_loader 读取（不影响 Spec） |
| _(无)_ | — | extra | JSONB | ➕ | 低频指标扩展字段；回测中可将 `ma60_slope` 等缺失字段暂存此处，待 ALTER TABLE 后迁出 |
| **未来收益（Spec 表）** | | | | | |
| future_ret_5d | NUMERIC(10,6) | _(在 backtest_features_extra)_ | — | ✅ | 按设计隔离，不污染 features_daily |
| future_ret_10d | NUMERIC(10,6) | _(在 backtest_features_extra)_ | — | ✅ | |
| future_ret_20d | NUMERIC(10,6) | _(在 backtest_features_extra)_ | — | ✅ | |
| future_max_up_20d | NUMERIC(10,6) | _(在 backtest_features_extra)_ | — | ✅ | |
| future_max_dd_20d | NUMERIC(10,6) | _(在 backtest_features_extra)_ | — | ✅ | |

**缺失字段处理建议：**

| 字段 | 计算公式 | 处理方案 |
|------|---------|---------|
| `ma60_slope` | 对过去20天的 MA60 序列做线性回归，斜率 / MA60均值 | 在 `03_compute_features.py` 中计算后 `UPDATE features_daily SET ma60_slope=... WHERE symbol=... AND trade_date=...`；先执行 `ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS ma60_slope NUMERIC(10,6)` |
| `ret_60d` | `close_t / close_{t-63} - 1`（取前63个交易日） | 同上，ADD COLUMN + UPDATE |
| `dist_20d_high` | `close_t / MAX(high, 20d) - 1`（负值，越接近0越强） | 同上 |
| `dist_60d_high` | `close_t / MAX(high, 60d) - 1` | 同上 |
| `pct_in_20d_range` | `(close_t - MIN(low,20d)) / (MAX(high,20d) - MIN(low,20d))`，范围 [0,1] | 同上；注意分母为0的边界处理（range=0则填 0.5） |

**推荐执行顺序**（在 `03_compute_features.py` 开头）：
```python
ALTER_SQLS = [
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS ma60_slope NUMERIC(10,6)",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS ret_60d NUMERIC(10,6)",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS dist_20d_high NUMERIC(10,6)",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS dist_60d_high NUMERIC(10,6)",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS pct_in_20d_range NUMERIC(8,4)",
]
```

---

## 3. fundamentals_daily

| Spec 字段 | Spec 类型 | P10 实际字段 | P10 类型 | 状态 | 备注 |
|-----------|-----------|-------------|---------|------|------|
| symbol | VARCHAR(20) | symbol | VARCHAR(20) | ✅ | |
| trade_date | DATE | trade_date | DATE | ✅ | |
| pe_ttm | NUMERIC(14,4) | pe_ttm | NUMERIC(12,4) | ✅ | |
| pb | NUMERIC(14,4) | pb | NUMERIC(12,4) | ✅ | |
| ps_ttm | NUMERIC(14,4) | ps_ttm | NUMERIC(12,4) | ✅ | |
| total_mv | NUMERIC(20,2) | total_mv | NUMERIC(18,2) | ✅ | |
| circ_mv | NUMERIC(20,2) | circ_mv | NUMERIC(18,2) | ✅ | |
| turnover_rate_f | NUMERIC(8,4) | turnover_rate_f | NUMERIC(8,4) | ✅ | |
| available_date | DATE | available_date | DATE | ✅ | 01_migrate 已 ADD & 回填：= trade_date |

**结论：全部字段匹配。无缺失，无需额外处理。**

---

## 4. financials_quarterly

| Spec 字段 | Spec 类型 | P10 实际字段 | P10 类型 | 状态 | 备注 |
|-----------|-----------|-------------|---------|------|------|
| symbol | VARCHAR(20) | symbol | VARCHAR(20) | ✅ | |
| report_date | DATE | report_date | DATE | ✅ | 主键之一 |
| announce_date | DATE NOT NULL | announce_date | DATE (nullable) | ⚠️ | Spec 要求 NOT NULL，P10 允许 NULL；01_migrate 用 `report_date+45天` 作为 NULL 的 available_date 兜底，实际查询 PIT 安全；无需 ALTER（强制 NOT NULL 有 12 行历史数据 NULL 无法通过） |
| revenue | NUMERIC(20,2) | revenue | NUMERIC(18,2) | ✅ | |
| revenue_yoy | NUMERIC(10,4) | revenue_yoy | NUMERIC(10,4) | ✅ | |
| revenue_qoq | NUMERIC(10,4) | revenue_qoq | NUMERIC(10,4) | ✅ | |
| net_profit | NUMERIC(20,2) | net_profit | NUMERIC(18,2) | ✅ | |
| np_yoy | NUMERIC(10,4) | np_yoy | NUMERIC(10,4) | ✅ | |
| gross_margin | NUMERIC(10,4) | gross_margin | NUMERIC(10,4) | ✅ | |
| net_margin | NUMERIC(10,4) | net_margin | NUMERIC(10,4) | ✅ | |
| total_assets | NUMERIC(20,2) | total_assets | NUMERIC(18,2) | ✅ | |
| total_liab | NUMERIC(20,2) | total_liab | NUMERIC(18,2) | ✅ | |
| debt_ratio | NUMERIC(10,4) | debt_ratio | NUMERIC(10,4) | ✅ | |
| current_ratio | NUMERIC(10,4) | current_ratio | NUMERIC(10,4) | ✅ | |
| goodwill | NUMERIC(20,2) | goodwill | NUMERIC(18,2) | ✅ | |
| ocf | NUMERIC(20,2) | ocf | NUMERIC(18,2) | ✅ | |
| ocf_to_np | NUMERIC(10,4) | ocf_to_np | NUMERIC(10,4) | ✅ | |
| roe_ttm | NUMERIC(10,4) | roe_ttm | NUMERIC(10,4) | ✅ | |
| roa_ttm | NUMERIC(10,4) | roa_ttm | NUMERIC(10,4) | ✅ | |
| available_date | DATE | available_date | DATE | ✅ | 01_migrate 已 ADD & 回填：= announce_date，NULL 时 = report_date + 45天 |
| _(无)_ | — | dupont_npm | NUMERIC(10,4) | ➕ | 杜邦净利率；可在 fundamental.py 中直接使用，提升分析质量 |
| _(无)_ | — | dupont_tat | NUMERIC(10,4) | ➕ | 杜邦总资产周转率 |
| _(无)_ | — | dupont_em | NUMERIC(10,4) | ➕ | 杜邦权益乘数 |

**结论：所有 Spec 字段均存在。P10 额外的三个杜邦字段可直接利用，无需处理。**

**announce_date NULL 处理说明：**
- 当前有约 12/48 行 announce_date 为 NULL（01_migrate 执行日志确认）
- `available_date` 已通过 `report_date + 45天` 安全兜底
- `pit_loader` 查询财报时，过滤条件用 `available_date <= :as_of_date` 即可，无需再判断 announce_date NULL

---

## 5. feature_compute.py 评估

**文件路径**: `data/pipeline/feature_compute.py`（P10 主项目，770行）

### 5.1 能否直接批量跑 2024-06-01 ~ 2026-04-17？

**结论：不能直接用，需要包装层。**

现有设计是 **单日单标的** 计算：
```python
# 当前接口签名
async def compute_for_symbol(
    self, symbol: str, market: str, trade_date: date, lookback: int = 250
) -> dict
```

要回填全量历史特征（~490个交易日 × 61只票 ≈ 30,000次调用），需要：

1. **方案A（推荐）**：在 `backtest/scripts/03_compute_features.py` 中写批量向量化版本
   - 一次性加载所有 symbol 的全量 bars（1次 SQL）
   - 用 pandas rolling 向量化计算所有指标（比逐行调用快 50x+）
   - 批量 COPY 写入 features_daily（比逐行 INSERT 快 20x+）
   - 单独计算 rs_rank_63d 和 stage（这两个需要跨标的查询）

2. **方案B（快速可用）**：包装 FeatureComputer 的单日接口，加双重循环
   - `for date in trade_dates: for symbol in watchlist: await compute_for_symbol(...)`
   - 无需重写计算逻辑，但性能差：rs_rank 每次都做全表扫描，30,000次调用约需数小时
   - 仅建议用于验证少量数据（如 10只票 × 30天）

**推荐方案A**，在 `backtest/scripts/03_compute_features.py` 中实现，与 P10 的 `feature_compute.py` 完全独立。

### 5.2 现有计算逻辑的 PIT 安全性

| 模块/函数 | 查询方式 | PIT 状态 | 说明 |
|----------|---------|---------|------|
| `_load_bars()` (含 trade_date) | `WHERE symbol=$1 AND trade_date <= $2` | ✅ 安全 | 正确截止日期过滤 |
| `_load_bars()` (trade_date=None) | `ORDER BY trade_date DESC LIMIT N` | ⚠️ 仅限生产 | 无日期限制，回测时**禁止**传 None |
| `StageDetector._load_bars()` | 同上两种情况 | ✅/⚠️ 同上 | |
| `StageDetector.calc_rs_rank()` (sql_precise) | `WHERE trade_date <= $1 ... LIMIT $2` | ✅ 安全 | target_dates 用 DESC LIMIT 正确找第N个交易日 |
| `calc_rs_rank()` end_prices | `WHERE trade_date = $1` | ✅ 安全 | 精确等于截止日 |
| `calc_rs_rank()` start_prices | `WHERE trade_date = (SELECT MIN(dt) FROM target_dates)` | ✅ 安全 | 等于第63个交易日，无未来数据 |
| 写回 `features_daily` | `INSERT ... ON CONFLICT DO UPDATE` | ✅ 安全 | 不涉及读取未来 |
| `calc_rs_rank()` 全市场百分位 | 扫描 `market_bars_daily` 全部 symbol | ⚠️ 设计差异 | 在回测中，这等于"用当时全市场已上市股票排名"——实际上没问题，因为未来新上市的股票在 T 日还不存在于 market_bars_daily |

**关键结论：**

1. **`available_date` 未参与过滤**：`stage_detector.py` 和 `feature_compute.py` 直接用 `trade_date <= X` 过滤 `market_bars_daily`。由于该表的 `available_date = trade_date`，结果等价于 `available_date <= X`，**PIT 安全**。但 `pit_loader.py` 实现时应统一用 `available_date <= :as_of_date` 以保证一致性。

2. **`_load_bars(trade_date=None)` 是 PIT 高危路径**：回测的 `backtest/analysis/` 模块禁止在调用 StageDetector 时传 `trade_date=None`。

3. **P10 的 StageDetector 和 feature_compute.py 不可在回测中直接 import**（CLAUDE.md 铁律：禁止 import core/）。`backtest/scripts/03_compute_features.py` 必须独立实现，可参考其逻辑但不 import。

---

## 6. 汇总：待处理事项

| 优先级 | 任务 | 负责脚本 | 状态 |
|--------|------|---------|------|
| P0 | `turnover_rate` 从 `turnover` 回填 | `02_fetch_missing_data.py` | ⏳ |
| P0 | ADD COLUMN: `ma60_slope`, `ret_60d`, `dist_20d_high`, `dist_60d_high`, `pct_in_20d_range` | `03_compute_features.py` 开头 | ⏳ |
| P0 | 批量计算上述 5 个缺失特征 + 全量已有特征（2024-06-01 起） | `03_compute_features.py` | ⏳ |
| P1 | `pit_loader.py` 将 `rs_rank` 以别名 `rs_rank_63d` 暴露 | `pit_loader.py` | ⏳ |
| P2 | `backtest/analysis/` 内的 StageDetector 重写版本，确保不 import core/ | `backtest/analysis/stage_detector.py` | ⏳ |
