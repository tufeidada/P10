"""Telegram /watchlist 命令处理器。"""

from __future__ import annotations

from datetime import date

import structlog
from telegram import Update
from telegram.ext import ContextTypes

from bot.formatter import format_watchlist
from db.connection import db_execute, db_query, db_query_one

logger = structlog.get_logger(__name__)


def _guess_market(symbol: str) -> str:
    """根据代码格式猜测市场。

    A股: 600xxx.SH, 000xxx.SZ, 300xxx.SZ, 688xxx.SH
    美股: 纯字母
    """
    if "." in symbol and (symbol.endswith(".SH") or symbol.endswith(".SZ")):
        return "CN"
    return "US"


async def cmd_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/watchlist [add|remove] [symbol] [reason]"""
    args = context.args or []

    # /watchlist — 查看列表
    if not args:
        await _show_watchlist(update)
        return

    action = args[0].lower()

    if action == "add" and len(args) >= 2:
        symbol = args[1].upper()
        reason = " ".join(args[2:]) if len(args) > 2 else None
        await _add_to_watchlist(update, symbol, reason)

    elif action == "remove" and len(args) >= 2:
        symbol = args[1].upper()
        await _remove_from_watchlist(update, symbol)

    elif action in ("cn", "us"):
        await _show_watchlist(update, market=action.upper())

    else:
        await update.message.reply_text(
            "用法:\n"
            "/watchlist — 查看候选池\n"
            "/watchlist add 600519.SH 白酒龙头 — 添加\n"
            "/watchlist remove 600519.SH — 移除\n"
            "/watchlist cn — 只看A股\n"
            "/watchlist us — 只看美股"
        )


async def _show_watchlist(update: Update, market: str | None = None) -> None:
    """查询并展示候选池。"""
    try:
        if market:
            rows = await db_query(
                """
                SELECT symbol, market, name, source, industry
                FROM stock_universe
                WHERE active = TRUE AND market = $1
                ORDER BY added_date DESC
                """,
                market,
            )
        else:
            rows = await db_query(
                """
                SELECT symbol, market, name, source, industry
                FROM stock_universe
                WHERE active = TRUE
                ORDER BY market, added_date DESC
                """
            )

        stocks = [dict(r) for r in rows]
        text = format_watchlist(stocks, market)
        await update.message.reply_text(text, parse_mode="HTML")

    except Exception as e:
        logger.error("watchlist_show_error", error=str(e))
        await update.message.reply_text(f"⚠️ 查询失败: {e}")


async def _add_to_watchlist(
    update: Update, symbol: str, reason: str | None
) -> None:
    """添加股票到候选池。"""
    try:
        # 检查是否已存在
        existing = await db_query_one(
            "SELECT symbol, active FROM stock_universe WHERE symbol = $1",
            symbol,
        )

        market = _guess_market(symbol)

        if existing and existing["active"]:
            await update.message.reply_text(f"ℹ️ {symbol} 已在候选池中。")
            return

        if existing:
            # 重新激活
            await db_execute(
                """
                UPDATE stock_universe
                SET active = TRUE, added_date = $1, added_reason = $2,
                    source = 'manual', removed_date = NULL, removed_reason = NULL
                WHERE symbol = $3
                """,
                date.today(), reason, symbol,
            )
        else:
            await db_execute(
                """
                INSERT INTO stock_universe (symbol, market, source, added_date, added_reason, active)
                VALUES ($1, $2, 'manual', $3, $4, TRUE)
                """,
                symbol, market, date.today(), reason,
            )

        await update.message.reply_text(f"✅ 已添加 {symbol} 到候选池。")
        logger.info("watchlist_add", symbol=symbol, market=market, reason=reason)

    except Exception as e:
        logger.error("watchlist_add_error", symbol=symbol, error=str(e))
        await update.message.reply_text(f"⚠️ 添加失败: {e}")


async def _remove_from_watchlist(update: Update, symbol: str) -> None:
    """从候选池移除（标记为 removed，不物理删除）。"""
    try:
        existing = await db_query_one(
            "SELECT symbol FROM stock_universe WHERE symbol = $1 AND active = TRUE",
            symbol,
        )

        if not existing:
            await update.message.reply_text(f"ℹ️ {symbol} 不在候选池中。")
            return

        await db_execute(
            """
            UPDATE stock_universe
            SET active = FALSE, removed_date = $1, removed_reason = 'manual_remove'
            WHERE symbol = $2
            """,
            date.today(), symbol,
        )

        await update.message.reply_text(f"✅ 已从候选池移除 {symbol}。")
        logger.info("watchlist_remove", symbol=symbol)

    except Exception as e:
        logger.error("watchlist_remove_error", symbol=symbol, error=str(e))
        await update.message.reply_text(f"⚠️ 移除失败: {e}")
