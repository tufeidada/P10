"""
Telegram /regime 命令处理器 — 显示当前市场 regime 状态。

Usage:
    /regime        — 默认显示 A 股 regime
    /regime cn     — A 股 regime
    /regime us     — 美股 regime
    /regime all    — 全部市场
"""

from __future__ import annotations

import structlog
from telegram import Update
from telegram.ext import ContextTypes

from db.connection import db_query_one

logger = structlog.get_logger(__name__)

# Regime 模式标签和 emoji
_REGIME_LABELS: dict[str, str] = {
    "offense": "🟢 进攻模式",
    "cautious_offense": "🟡 谨慎进攻",
    "defense": "🟠 防守模式",
    "risk_off": "🔴 避险模式",
}

# 维度中文名
_DIMENSION_NAMES: dict[str, str] = {
    "trend": "趋势",
    "volatility": "波动",
    "breadth": "宽度",
    "liquidity": "资金",
}


def _progress_bar(score: float, width: int = 10) -> str:
    """将 0-100 的分数渲染为进度条。

    Args:
        score: 分数 0-100。
        width: 进度条宽度（字符数）。

    Returns:
        进度条字符串，例如 "████████░░ 78/100"。
    """
    clamped = max(0.0, min(100.0, score))
    filled = int(clamped / 100.0 * width)
    empty = width - filled
    return f"{'█' * filled}{'░' * empty} {clamped:.0f}/100"


def _format_regime(regime: dict, market_label: str) -> str:
    """格式化单个市场的 regime 信息为 HTML。

    Args:
        regime: 数据库查询结果字典。
        market_label: 市场显示名称。

    Returns:
        HTML 格式消息文本。
    """
    mode = regime.get("regime_mode", "N/A")
    mode_label = _REGIME_LABELS.get(mode, mode)

    lines = [
        f"🌡 <b>{market_label} Regime</b>",
        f"模式: <b>{mode_label}</b>",
        "",
    ]

    # 四维度进度条
    dimensions = [
        ("trend", regime.get("trend_score", 0)),
        ("volatility", regime.get("volatility_score", 0)),
        ("breadth", regime.get("breadth_score", 0)),
        ("liquidity", regime.get("liquidity_score", 0)),
    ]

    for dim_key, score in dimensions:
        dim_name = _DIMENSION_NAMES.get(dim_key, dim_key)
        bar = _progress_bar(float(score) if score is not None else 0)
        lines.append(f"{dim_name} {bar}")

    # 权重配置
    weights = regime.get("dimension_weights")
    if weights and isinstance(weights, dict):
        lines.append("")
        weight_parts = []
        label_map = {"technical": "技术", "fundamental": "基本", "flow": "资金", "sentiment": "情绪"}
        for k, v in weights.items():
            label = label_map.get(k, k)
            weight_parts.append(f"{label}={v:.0%}" if isinstance(v, (int, float)) else f"{label}={v}")
        lines.append(f"📊 权重: {' | '.join(weight_parts)}")

    # 仓位上限
    max_pos = regime.get("max_position_pct")
    if max_pos is not None:
        lines.append(f"📏 仓位上限: <b>{float(max_pos):.0%}</b>")

    # 信号阈值调整
    threshold_adj = regime.get("signal_threshold_adj")
    if threshold_adj is not None:
        lines.append(f"🔧 信号阈值系数: {float(threshold_adj):.2f}")

    # 日期
    trade_date = regime.get("trade_date")
    if trade_date:
        lines.append(f"\n🕐 数据日期: {trade_date}")

    return "\n".join(lines)


async def _fetch_regime(market: str) -> dict | None:
    """从数据库获取最新 regime 数据。

    Args:
        market: 市场代码 ('CN' | 'US')。

    Returns:
        Regime 记录字典，或 None。
    """
    try:
        row = await db_query_one(
            """
            SELECT trade_date, market, regime_mode,
                   trend_score, volatility_score,
                   breadth_score, liquidity_score,
                   signal_threshold_adj, max_position_pct,
                   dimension_weights, detail
            FROM regime_daily
            WHERE market = $1
            ORDER BY trade_date DESC
            LIMIT 1
            """,
            market,
        )
        return dict(row) if row else None
    except Exception as e:
        logger.error("fetch_regime_error", market=market, error=str(e))
        return None


async def cmd_regime(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/regime [cn|us|all] — 显示当前 regime 状态。

    默认显示 A 股 regime。指定 us 显示美股，all 显示全部。
    """
    args = context.args or []
    target = args[0].lower() if args else "cn"

    if target not in ("cn", "us", "all"):
        await update.message.reply_text(
            "用法: /regime [cn|us|all]\n"
            "  /regime — A股 regime\n"
            "  /regime us — 美股 regime\n"
            "  /regime all — 全部市场"
        )
        return

    try:
        parts: list[str] = []

        if target in ("cn", "all"):
            cn_regime = await _fetch_regime("CN")
            if cn_regime:
                parts.append(_format_regime(cn_regime, "A股"))
            else:
                parts.append("🌡 <b>A股 Regime</b>\n暂无数据。请先运行 regime 计算。")

        if target in ("us", "all"):
            us_regime = await _fetch_regime("US")
            if us_regime:
                if parts:
                    parts.append("\n" + "─" * 20 + "\n")
                parts.append(_format_regime(us_regime, "美股"))
            else:
                if parts:
                    parts.append("\n" + "─" * 20 + "\n")
                parts.append("🌡 <b>美股 Regime</b>\n暂无数据。")

        text = "\n".join(parts)
        await update.message.reply_text(text, parse_mode="HTML")

    except Exception as e:
        logger.error("cmd_regime_error", error=str(e))
        await update.message.reply_text(f"⚠️ 查询 regime 失败: {e}")
