"""Telegram 消息格式化工具。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any


def escape_md(text: str) -> str:
    """转义 MarkdownV2 特殊字符。"""
    special = r"_*[]()~`>#+-=|{}.!"
    for ch in special:
        text = text.replace(ch, f"\\{ch}")
    return text


def format_help() -> str:
    """格式化 /help 命令响应。"""
    return (
        "📡 <b>AlphaRadar 命令列表</b>\n"
        "\n"
        "<b>分析</b>\n"
        "/analyze &lt;代码&gt; — 即时多维分析\n"
        "/regime — 当前 A股/美股 regime 状态\n"
        "/signal — 活跃买卖信号列表\n"
        "/macro — 宏观环境概览\n"
        "\n"
        "<b>持仓</b>\n"
        "/status — 当前持仓风险概览\n"
        "/add &lt;代码&gt; &lt;价格&gt; &lt;数量&gt; — 记录建仓\n"
        "/close &lt;代码&gt; &lt;价格&gt; — 记录平仓\n"
        "\n"
        "<b>候选池</b>\n"
        "/watchlist — 查看候选池\n"
        "/watchlist add &lt;代码&gt; [原因] — 添加到候选池\n"
        "/watchlist remove &lt;代码&gt; — 从候选池移除\n"
        "\n"
        "<b>复盘</b>\n"
        "/review — 本周判断复盘摘要\n"
        "/quality &lt;规则名&gt; — 信号历史表现\n"
        "/wiki &lt;代码&gt; — Wiki 已知信息\n"
        "\n"
        "<b>系统</b>\n"
        "/dq — 数据质量状态\n"
        "/help — 显示此帮助\n"
    )


def format_status(
    positions: list[dict[str, Any]],
    regime: dict[str, Any] | None = None,
) -> str:
    """格式化 /status 持仓概览。"""
    if not positions:
        text = "📊 <b>持仓概览</b>\n\n当前无持仓。"
        if regime:
            text += f"\n\n🌡 Regime: <b>{regime.get('regime_mode', 'N/A')}</b>"
        return text

    lines = ["📊 <b>持仓概览</b>\n"]

    total_value = 0.0
    total_pnl = 0.0

    for p in positions:
        symbol = p["symbol"]
        shares = p["shares"]
        entry_price = float(p["entry_price"])
        current_price = float(p.get("current_price", entry_price))
        pnl_pct = (current_price / entry_price - 1) * 100 if entry_price > 0 else 0
        value = current_price * shares

        emoji = "🟢" if pnl_pct >= 0 else "🔴"
        stop_text = ""
        if p.get("stop_loss"):
            stop_dist = (current_price / float(p["stop_loss"]) - 1) * 100
            stop_text = f" | 止损距离 {stop_dist:+.1f}%"

        lines.append(
            f"{emoji} <b>{symbol}</b>  {current_price:.2f}  "
            f"<b>{pnl_pct:+.1f}%</b>{stop_text}"
        )
        total_value += value
        total_pnl += (current_price - entry_price) * shares

    lines.append(f"\n💰 总市值: {total_value:,.0f}")
    lines.append(f"📈 总盈亏: {total_pnl:+,.0f}")

    if regime:
        mode = regime.get("regime_mode", "N/A")
        mode_label = {
            "offense": "进攻 🟢",
            "cautious_offense": "谨慎进攻 🟡",
            "defense": "防守 🟠",
            "risk_off": "避险 🔴",
        }.get(mode, mode)
        lines.append(f"\n🌡 Regime: <b>{mode_label}</b>")

    return "\n".join(lines)


def format_watchlist(
    stocks: list[dict[str, Any]],
    market: str | None = None,
) -> str:
    """格式化 /watchlist 候选池列表。"""
    if not stocks:
        return "📋 <b>候选池</b>\n\n候选池为空。使用 /watchlist add &lt;代码&gt; 添加。"

    lines = ["📋 <b>候选池</b>\n"]

    cn_stocks = [s for s in stocks if s.get("market") == "CN"]
    us_stocks = [s for s in stocks if s.get("market") == "US"]

    if cn_stocks and (market is None or market == "CN"):
        lines.append("<b>🇨🇳 A股</b>")
        for s in cn_stocks:
            source_tag = "📌" if s.get("source") == "manual" else "🤖"
            name = s.get("name", "")
            lines.append(f"  {source_tag} {s['symbol']}  {name}")
        lines.append("")

    if us_stocks and (market is None or market == "US"):
        lines.append("<b>🇺🇸 美股</b>")
        for s in us_stocks:
            source_tag = "📌" if s.get("source") == "manual" else "🤖"
            name = s.get("name", "")
            lines.append(f"  {source_tag} {s['symbol']}  {name}")
        lines.append("")

    lines.append(f"共 {len(stocks)} 只  (📌手动 🤖系统推荐)")
    return "\n".join(lines)


def format_dq_report(checks: list[dict[str, Any]]) -> str:
    """格式化数据质量检查结果。"""
    if not checks:
        return "🔍 <b>数据质量</b>\n\n暂无检查记录。"

    lines = ["🔍 <b>数据质量检查</b>\n"]
    status_emoji = {"ok": "✅", "warning": "⚠️", "critical": "🚨"}

    for c in checks:
        emoji = status_emoji.get(c["status"], "❓")
        lines.append(
            f"{emoji} <b>{c['source_name']}</b> ({c['check_type']}): "
            f"最新 {c.get('latest_date', 'N/A')}"
        )
        if c["status"] != "ok" and c.get("detail"):
            detail = c["detail"]
            if isinstance(detail, dict):
                msg = detail.get("message", "")
            else:
                msg = str(detail)
            if msg:
                lines.append(f"    ↳ {msg}")

    return "\n".join(lines)
