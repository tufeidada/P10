"""
Telegram /signal 命令处理器 — 查询今日活跃盘中买卖信号。

Usage:
    /signal              — 显示今日全部强/中等信号（最多 20 条）
    /signal 600519.SH    — 显示指定股票的今日信号
    /signal buy          — 只看今日买入信号
    /signal sell         — 只看今日卖出信号
"""

from __future__ import annotations

from datetime import date
from typing import Any

import structlog
from telegram import Update
from telegram.ext import ContextTypes

from db.connection import db_query

logger = structlog.get_logger(__name__)

# 每次最多显示的信号条数
_MAX_SIGNALS = 20


async def cmd_signal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/signal — 显示今日活跃盘中信号。

    无参数时显示当日全部 strong/moderate 信号，倒序排列，最多 20 条。
    支持可选的代码或方向过滤参数。

    Args:
        update: Telegram Update 对象。
        context: 命令上下文，context.args 含可选过滤参数。
    """
    args = context.args or []

    # 解析可选过滤参数
    symbol_filter: str | None = None
    type_filter: str | None = None

    for arg in args:
        arg_lower = arg.lower()
        if arg_lower in ("buy", "sell"):
            type_filter = arg_lower
        else:
            symbol_filter = arg.upper()

    try:
        signals = await _query_signals(symbol_filter, type_filter)
        text = _format_signals(signals, symbol_filter, type_filter)
        await update.message.reply_text(text, parse_mode="HTML")

    except Exception as e:
        logger.error("cmd_signal_error", error=str(e))
        await update.message.reply_text(f"⚠️ 查询信号失败: {e}")


# ──────────────────────────────────────────────────────────────
# 数据查询
# ──────────────────────────────────────────────────────────────


async def _query_signals(
    symbol: str | None,
    signal_type: str | None,
) -> list[dict[str, Any]]:
    """从 intraday_signals 表查询今日信号。

    只返回 strong/moderate 强度的信号，按触发时间倒序。

    Args:
        symbol: 股票代码过滤，可为 None。
        signal_type: 信号类型过滤 'buy'/'sell'，可为 None。

    Returns:
        信号记录字典列表。
    """
    conditions: list[str] = [
        "DATE(s.signal_time) = CURRENT_DATE",
        "s.strength IN ('strong', 'moderate')",
    ]
    params: list[Any] = []
    idx = 1

    if symbol:
        conditions.append(f"s.symbol = ${idx}")
        params.append(symbol)
        idx += 1

    if signal_type:
        conditions.append(f"s.signal_type = ${idx}")
        params.append(signal_type)
        idx += 1

    where = " AND ".join(conditions)

    rows = await db_query(
        f"""
        SELECT s.symbol, s.market, s.signal_type, s.strength,
               s.trigger_rule, s.price_at_signal, s.signal_time,
               u.name
        FROM intraday_signals s
        LEFT JOIN stock_universe u ON u.symbol = s.symbol
        WHERE {where}
        ORDER BY s.signal_time DESC
        LIMIT {_MAX_SIGNALS}
        """,
        *params,
    )
    return [dict(r) for r in rows]


# ──────────────────────────────────────────────────────────────
# 格式化
# ──────────────────────────────────────────────────────────────


def _format_signals(
    signals: list[dict[str, Any]],
    symbol_filter: str | None,
    type_filter: str | None,
) -> str:
    """将信号列表格式化为 Telegram HTML 消息。

    目标格式：
        📡 盘中信号 (2026-04-17)
        ━━━━━━━━━━━━━━━
        🟢 BUY 600519.SH strong 10:15 ¥1705 — VWAP回踩+MACD金叉
        🔴 SELL 000001.SZ moderate 11:30 ¥11.05 — 跌破支撑
        ━━━━━━━━━━━━━━━
        共 2 条信号 (今日)

    Args:
        signals: 信号记录列表。
        symbol_filter: 代码过滤（用于标题显示）。
        type_filter: 类型过滤（用于标题显示）。

    Returns:
        HTML 格式消息字符串。
    """
    today_str = date.today().strftime("%Y-%m-%d")

    # 标题行
    title_parts = ["📡 <b>盘中信号</b>"]
    if symbol_filter:
        title_parts.append(f"· {symbol_filter}")
    if type_filter:
        type_cn = {"buy": "买入", "sell": "卖出"}.get(type_filter, type_filter)
        title_parts.append(f"· {type_cn}")
    title_parts.append(f"({today_str})")
    header = " ".join(title_parts)

    if not signals:
        return f"{header}\n\n今日暂无盘中信号"

    sep = "━━━━━━━━━━━━━━━"
    lines: list[str] = [header, sep]

    for sig in signals:
        sig_type: str = sig.get("signal_type", "")
        strength: str = sig.get("strength", "")
        symbol: str = sig.get("symbol", "")
        price = sig.get("price_at_signal")
        rule: str = sig.get("trigger_rule") or ""
        sig_time = sig.get("signal_time")
        market: str = sig.get("market", "")

        # 方向 emoji
        type_emoji = "🟢" if sig_type == "buy" else "🔴"
        type_upper = sig_type.upper()

        # 强度文字（保留英文与规范格式一致）
        strength_str = strength  # 'strong' | 'moderate'

        # 时间格式化 HH:MM
        time_str = _parse_time_str(sig_time)

        # 价格：A 股用 ¥，美股用 $
        currency = "¥" if market == "CN" else "$"
        price_str = f"{currency}{float(price):.2f}" if price is not None else "N/A"

        # 规则简短描述（最多 20 字，截断）
        rule_display = _shorten_rule(rule)

        line = (
            f"{type_emoji} {type_upper} {symbol} {strength_str} "
            f"{time_str} {price_str}"
        )
        if rule_display:
            line += f" — {rule_display}"

        lines.append(line)

    lines.append(sep)
    lines.append(f"共 {len(signals)} 条信号 (今日)")

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────
# 内部工具
# ──────────────────────────────────────────────────────────────


def _parse_time_str(sig_time: Any) -> str:
    """将信号时间解析为 HH:MM 字符串。

    Args:
        sig_time: datetime 对象或字符串。

    Returns:
        'HH:MM' 格式字符串，解析失败时返回 '--:--'。
    """
    try:
        if sig_time is None:
            return "--:--"
        if hasattr(sig_time, "strftime"):
            # 转为本地时间再格式化
            local = sig_time.astimezone()
            return local.strftime("%H:%M")
        # 字符串形式 '2026-04-17T10:15:00+00:00'
        return str(sig_time)[11:16]
    except Exception:
        return "--:--"


_RULE_DISPLAY_MAP: dict[str, str] = {
    "vwap_pullback_macd_golden": "VWAP回踩+MACD金叉",
    "buy_6conditions": "多因子买入",
    "stop_loss": "触达止损位",
    "breakdown": "跌破支撑放量",
    "vwap_persistent": "VWAP持续偏离",
    "momentum_collapse": "动量崩塌",
}


def _shorten_rule(rule: str) -> str:
    """将 trigger_rule 转换为可读中文标签。

    Args:
        rule: trigger_rule 字符串，可能含多个规则用 '+' 连接。

    Returns:
        可读标签，最多 20 个字符，超出则截断加省略号。
    """
    if not rule:
        return ""

    # 复合规则（stop_loss+breakdown 等）
    parts = rule.split("+")
    labels = [_RULE_DISPLAY_MAP.get(p.strip(), p.strip()) for p in parts]
    result = "+".join(labels)

    if len(result) > 20:
        result = result[:19] + "…"
    return result
