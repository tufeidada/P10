"""/apply_weights — 将月报建议的维度权重写入 regime_params.yaml。"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import structlog
import yaml
from telegram import Update
from telegram.ext import ContextTypes

logger = structlog.get_logger(__name__)

# regime_params.yaml 的绝对路径（从本文件向上三级到项目根）
_YAML_PATH = Path(__file__).parent.parent.parent / "config" / "regime_params.yaml"

# 权重维度的中文标签
_DIM_LABELS: dict[str, str] = {
    "technical": "技术面",
    "fundamental": "基本面",
    "flow": "资金面",
    "sentiment": "情绪面",
}

# 四种 regime 模式
_REGIME_KEYS = ("offense", "cautious_offense", "defense", "risk_off")


async def cmd_apply_weights(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/apply_weights — 应用月报建议的维度权重到所有 regime 模式。

    用法:
        /apply_weights          — 预览月报建议权重 vs 当前权重
        /apply_weights confirm  — 确认写入 regime_params.yaml

    Args:
        update: Telegram Update 对象。
        context: ContextTypes.DEFAULT_TYPE，context.args 含可选的 "confirm"。
    """
    from db.connection import db_query_one

    # ------------------------------------------------------------------
    # 1. 查询最新月报
    # ------------------------------------------------------------------
    try:
        row = await db_query_one(
            """
            SELECT key_findings
            FROM review_reports
            WHERE report_type = 'monthly'
            ORDER BY created_at DESC
            LIMIT 1
            """
        )
    except Exception as e:
        logger.error("apply_weights_db_error", error=str(e))
        await update.message.reply_text(f"⚠️ 数据库查询失败: {e}")
        return

    if not row:
        await update.message.reply_text("暂无月报建议权重，请先运行月度复盘")
        return

    key_findings: dict[str, Any] = dict(row).get("key_findings") or {}
    suggested: dict[str, float] | None = key_findings.get("suggested_weights")

    if not suggested:
        await update.message.reply_text("暂无月报建议权重，请先运行月度复盘")
        return

    # 验证建议权重包含所有维度且和为 1.0（容许浮点误差）
    dims = list(_DIM_LABELS.keys())
    if set(suggested.keys()) != set(dims):
        await update.message.reply_text(
            f"⚠️ 月报权重维度不完整，期望: {dims}，实际: {list(suggested.keys())}"
        )
        return

    weight_sum = sum(suggested.values())
    if abs(weight_sum - 1.0) > 0.01:
        await update.message.reply_text(
            f"⚠️ 月报权重之和为 {weight_sum:.4f}，必须等于 1.0"
        )
        return

    # ------------------------------------------------------------------
    # 2. 读取当前 YAML，提取 offense 当前权重（用于展示"当前值"）
    # ------------------------------------------------------------------
    try:
        with open(_YAML_PATH, encoding="utf-8") as f:
            data: dict[str, Any] = yaml.safe_load(f)
    except Exception as e:
        logger.error("apply_weights_yaml_read_error", error=str(e))
        await update.message.reply_text(f"⚠️ 读取配置文件失败: {e}")
        return

    regimes: dict[str, Any] = data.get("regimes", {})
    # 取 offense 权重作为"当前"展示基准
    current_weights: dict[str, float] = (
        regimes.get("offense", {}).get("weights", {})
    )

    # ------------------------------------------------------------------
    # 3. 如果没有 confirm 参数 → 预览模式
    # ------------------------------------------------------------------
    args: list[str] = context.args or []
    if "confirm" not in args:
        lines = [
            "📊 <b>权重调整确认</b>",
            "━━━━━━━━━━━━━━━",
            f"{'维度':<6}{'当前':>6}{'':>4}{'建议':>6}",
        ]
        for dim, label in _DIM_LABELS.items():
            cur_pct = current_weights.get(dim, 0.0) * 100
            sug_pct = suggested[dim] * 100
            lines.append(f"{label:<5}  {cur_pct:>4.0f}%  →  {sug_pct:>4.0f}%")

        lines += [
            "",
            "该权重将应用到所有 4 种 Regime 模式（按比例缩放）。",
            "发送 /apply_weights confirm 确认执行",
        ]
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
        return

    # ------------------------------------------------------------------
    # 4. confirm 分支 → 写入 YAML
    # ------------------------------------------------------------------
    # 4a. 备份原文件
    try:
        backup_path = str(_YAML_PATH) + ".bak"
        shutil.copy(_YAML_PATH, backup_path)
        logger.info("apply_weights_backup", path=backup_path)
    except Exception as e:
        logger.error("apply_weights_backup_error", error=str(e))
        await update.message.reply_text(f"⚠️ 备份配置文件失败: {e}")
        return

    # 4b. 将建议权重直接写入每种 regime（sum 已验证为 1.0，直接替换）
    for regime_key in _REGIME_KEYS:
        if regime_key not in regimes:
            logger.warning("apply_weights_missing_regime", regime=regime_key)
            continue
        regimes[regime_key]["weights"] = dict(suggested)

    # 4c. 写回 YAML
    try:
        with open(_YAML_PATH, "w", encoding="utf-8") as f:
            yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        logger.info("apply_weights_written", path=str(_YAML_PATH))
    except Exception as e:
        logger.error("apply_weights_yaml_write_error", error=str(e))
        await update.message.reply_text(f"⚠️ 写入配置文件失败: {e}")
        return

    # 4d. 重载 regime 参数缓存
    try:
        from core.regime.detector import reload_params
        reload_params()
        logger.info("apply_weights_cache_reloaded")
    except Exception as e:
        logger.warning("apply_weights_reload_warning", error=str(e))
        # 不阻塞，仅警告

    # 4e. 回复确认消息
    lines = [
        "✅ <b>权重已更新</b>",
        "━━━━━━━━━━━━━━━",
    ]
    for dim, label in _DIM_LABELS.items():
        cur_pct = current_weights.get(dim, 0.0) * 100
        sug_pct = suggested[dim] * 100
        lines.append(f"{label}: {cur_pct:.0f}% → {sug_pct:.0f}%")

    lines += [
        "",
        "已更新 4 种 Regime 模式的权重配置。",
        "下次分析将使用新权重。",
        "",
        f"备份文件: config/regime_params.yaml.bak",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    logger.info(
        "apply_weights_done",
        suggested=suggested,
        regimes_updated=list(_REGIME_KEYS),
    )
