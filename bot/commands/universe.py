"""
Telegram /universe 命令 — 显示 active watchlist 列表。
"""

from __future__ import annotations

import structlog
from telegram import Update
from telegram.ext import ContextTypes

from db.universe import get_active_stocks

logger = structlog.get_logger(__name__)


async def cmd_universe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/universe — 返回 active watchlist 列表（CN + US）。"""
    try:
        stocks = await get_active_stocks()
        if not stocks:
            await update.message.reply_text("⚠️ Watchlist 为空。")
            return

        cn = [s for s in stocks if s.market == "CN"]
        us = [s for s in stocks if s.market == "US"]

        lines = [f"📋 <b>Active Watchlist</b> ({len(stocks)} 只)\n"]

        if cn:
            lines.append(f"🇨🇳 <b>A 股 ({len(cn)} 只)</b>")
            # 按 priority 分组
            for pri, label in [(1, "核心"), (2, "观察"), (3, "储备")]:
                group = [s for s in cn if s.priority == pri]
                if group:
                    syms = "  ".join(
                        f"{s.symbol}({s.name or ''})" for s in group
                    )
                    lines.append(f"  P{pri} {label}: {syms}")

        if us:
            lines.append(f"\n🇺🇸 <b>美股 ({len(us)} 只)</b>")
            for pri, label in [(1, "核心"), (2, "观察"), (3, "储备")]:
                group = [s for s in us if s.priority == pri]
                if group:
                    syms = "  ".join(
                        f"{s.symbol}({s.name or ''})" for s in group
                    )
                    lines.append(f"  P{pri} {label}: {syms}")

        text = "\n".join(lines)
        # Telegram 消息上限 4096 字符，截断保护
        if len(text) > 4000:
            text = text[:3990] + "\n…（已截断）"

        await update.message.reply_text(text, parse_mode="HTML")

    except Exception as e:
        logger.error("cmd_universe_error", error=str(e))
        await update.message.reply_text(f"⚠️ universe 查询失败: {e}")
