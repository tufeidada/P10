# Regime Mode 消费方清单

> 生成时间：2026-04-20
> 范围：主项目（backtest/ 目录不纳入，独立 chat 跟进）
> 目的：M1 验收依据 — 所有消费方对 4 种合法 mode 均有对应配置，无静默 fallback

---

## 合法 regime_mode 集合

```
VALID_REGIME_MODES = {"offense", "cautious_offense", "defense", "risk_off"}
```

定义在 `core/regime/constants.py`

---

## 消费方详情

### 1. `core/regime/detector.py`

| 行号 | 消费形式 | 消费字段 | 有无 fallback |
|------|----------|----------|---------------|
| 124-142 | 2×2 矩阵映射 | 产出 regime_mode | 无（完整枚举） |
| 153-182 | 修正规则 | 调整 mode 字符串 | 无 |
| 229-235 | dict lookup `params["regimes"][regime_mode]` | signal_threshold_adj, max_position_pct, weights | 无（KeyError if missing — 正确） |

**状态**：✅ 已正确，无需改动

---

### 2. `core/analysis/composite.py`

| 行号 | 消费形式 | 消费字段 | 有无 fallback | 改动状态 |
|------|----------|----------|---------------|----------|
| 76-87 | `_load_regime_params()` except 块 | 全量权重 | ❌ 硬编码 fallback dict | ✅ M1 已删除，改为 raise |
| 99-130 | `_get_latest_regime()` DB miss | regime_mode 等全量 | ❌ 硬编码 cautious_offense 默认值 | ✅ M1 已改为返回 None |
| 132-148 | `_get_weights()` `.get(regime_mode, cautious_offense)` | weights | ❌ 静默 fallback 到 cautious_offense | ✅ M1 已改为 raise ValueError |
| 262-293 | `_compute_confidence()` 硬编码 dict | signal_threshold_adj | ❌ 硬编码 {offense:1.0, ...}.get(mode, 0.8) | ✅ M1 已改为从 self._regime_params 读取 |
| 369 | `regime.get("regime_mode", "cautious_offense")` | regime_mode | ❌ None 时静默 fallback | ✅ M1 已改为 None 时 raise RuntimeError |

---

### 3. `llm/prompts.py`

| 行号 | 消费形式 | 消费字段 | 有无 fallback | 说明 |
|------|----------|----------|---------------|------|
| 271 | `regime.get("regime_mode", "cautious_offense")` | 显示用 regime_label | 有（显示 fallback） | 纯展示函数，不影响交易逻辑 |
| 40-43 | `_REGIME_LABELS` dict（4 个 key） | 中文标签 | `.get(mode, mode)` 兜底 | 显示兜底合理 |

**状态**：⚪ 显示层，不改动

---

### 4. `bot/commands/analyze.py`

| 行号 | 消费形式 | 消费字段 | 有无 fallback | 说明 |
|------|----------|----------|---------------|------|
| 44-51 | dict with 4 keys + `.get(mode, mode)` | emoji 标签 | 有（显示 fallback） | 纯展示 |
| 90-96 | dict with 4 keys + `.get(mode, mode)` | 中文标签 | 有（显示 fallback） | 纯展示 |

**状态**：✅ 4 种 mode 均覆盖，显示兜底合理，无需改动

---

### 5. `bot/commands/regime.py`

| 行号 | 消费形式 | 消费字段 | 有无 fallback | 说明 |
|------|----------|----------|---------------|------|
| 23-26 | dict with 4 keys | 中文标签显示 | 无兜底（未用 .get） | 纯展示 |
| 64 | `regime.get("regime_mode", "N/A")` | 显示 | N/A 兜底 | 纯展示 |

**状态**：✅ 无需改动

---

### 6. `bot/formatter.py`

| 行号 | 消费形式 | 消费字段 | 有无 fallback | 说明 |
|------|----------|----------|---------------|------|
| 57 | `regime.get('regime_mode', 'N/A')` | 显示 | N/A 兜底 | 纯展示 |
| 90-95 | dict with 4 keys + `.get(mode, mode)` | 中文标签 | 有（显示 fallback） | 纯展示 |

**状态**：✅ 无需改动

---

### 7. `bot/commands/apply_weights.py`

| 行号 | 消费形式 | 消费字段 | 有无 fallback | 说明 |
|------|----------|----------|---------------|------|
| 28 | `_REGIME_KEYS = ("offense", "cautious_offense", "defense", "risk_off")` | 遍历 4 种 mode | 无 | 完整枚举，从 YAML 读取 |

**状态**：✅ 无需改动

---

### 8. `scheduler/scheduler.py`

| 行号 | 消费形式 | 消费字段 | 有无 fallback | 说明 |
|------|----------|----------|---------------|------|
| 216 | `regime.regime_mode` | 日志记录 | 无 | 读属性，非参数消费 |
| 371 | `regime.regime_mode` | 日志记录 | 无 | 同上 |

**状态**：✅ 无需改动

---

### 9. `core/evolution/judgment_tracker.py`

| 行号 | 消费形式 | 消费字段 | 有无 fallback | 说明 |
|------|----------|----------|---------------|------|
| 279-305 | SQL 查询后读取 `regime_mode` 字段 | 比较两个时间点的 mode | 无 fallback | DB 读取比较，无交易逻辑 |

**状态**：✅ 无需改动

---

### 10. `core/evolution/reviewer.py`

| 行号 | 消费形式 | 消费字段 | 有无 fallback | 说明 |
|------|----------|----------|---------------|------|
| 1395-1396 | `regime_cfg.get("regimes", {}).get("offense", {}).get("weights", {})` | 展示基准权重 | 空 dict | 报告展示用 |
| 1460-1479 | SQL 查询读取 `regime_mode` | 历史分析展示 | 无 | 只读展示 |

**状态**：✅ 无需改动

---

### 11. `core/evolution/signal_quality.py`

| 行号 | 消费形式 | 消费字段 | 有无 fallback | 说明 |
|------|----------|----------|---------------|------|
| 384-396 | `_extract_regime_mode()` 从 JSONB 提取 | 分组 key | 返回 "unknown" | 提取辅助函数，unknown 用于分组统计，不影响交易 |

**状态**：✅ 无需改动

---

### 12. `api/main.py`

| 行号 | 消费形式 | 消费字段 | 有无 fallback | 说明 |
|------|----------|----------|---------------|------|
| 698 | 函数参数传递 `regime_mode` | 传递给其他逻辑 | 不明（需确认）| 需在 M1 验收前确认 |

**状态**：⚠️ 待确认（api 层，读取 DB 后传递）

---

## 变更汇总

| 文件 | 改动类型 | 涉及行 |
|------|----------|--------|
| `core/analysis/composite.py` | 删除 4 处硬编码 fallback，改为 raise | 76-87, 99-130, 132-148, 262-293, 369 |
| `core/regime/constants.py` | 新建，定义 VALID_REGIME_MODES | — |
| `core/regime/__init__.py` | 导出 VALID_REGIME_MODES | — |
| `config/regime_params.yaml` | 更新 schema，差异化 weights | 全文 |
| `scripts/verify_regime_alignment.py` | 新建验证脚本 | — |
