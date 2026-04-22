"""
Telegram /approve 和 /reject 命令处理器 — 管理经验条目状态。

Usage:
    /approve <id>   — 将经验条目状态改为 active
    /reject  <id>   — 将经验条目状态改为 deprecated
"""

from __future__ import annotations

from datetime import date

import structlog
from telegram import Update
from telegram.ext import ContextTypes

from db.connection import db_execute, db_query_one

logger = structlog.get_logger(__name__)


async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/approve <id> — 将经验条目状态改为 active。

    查找指定 ID 的经验条目，若存在且非 active 则激活，
    同时更新 last_validated 为今日。

    Args:
        update: Telegram Update 对象。
        context: 命令上下文，context.args[0] 为条目 ID。
    """
    entry_id = _parse_id(context.args)
    if entry_id is None:
        await update.message.reply_text("用法: /approve <id>\n例如: /approve 42")
        return

    try:
        row = await db_query_one(
            "SELECT id, status, content_text, category, market "
            "FROM experience_store WHERE id = $1",
            entry_id,
        )

        if not row:
            await update.message.reply_text(f"未找到 ID {entry_id} 的经验条目")
            return

        entry = dict(row)
        if entry["status"] == "active":
            await update.message.reply_text(f"ID {entry_id} 已是活跃状态")
            return

        await db_execute(
            "UPDATE experience_store "
            "SET status = 'active', last_validated = $1 "
            "WHERE id = $2",
            date.today(),
            entry_id,
        )

        content_preview = (entry.get("content_text") or "")[:100]
        category: str = entry.get("category") or "N/A"
        market: str = entry.get("market") or "N/A"

        text = (
            f'✅ <b>经验条目已激活</b> (ID {entry_id})\n'
            f'"{content_preview}"\n'
            f"类别: {category} | 市场: {market}\n"
            f"已可用于 RAG 检索"
        )
        await update.message.reply_text(text, parse_mode="HTML")
        logger.info("experience_approved", entry_id=entry_id, category=category)

    except Exception as e:
        logger.error("cmd_approve_error", entry_id=entry_id, error=str(e))
        await update.message.reply_text(f"⚠️ 操作失败: {e}")


async def cmd_reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/reject <id> — 将经验条目状态改为 deprecated。

    查找指定 ID 的经验条目，若存在且非 deprecated 则废弃，
    同时更新 last_validated 为今日。

    Args:
        update: Telegram Update 对象。
        context: 命令上下文，context.args[0] 为条目 ID。
    """
    entry_id = _parse_id(context.args)
    if entry_id is None:
        await update.message.reply_text("用法: /reject <id>\n例如: /reject 42")
        return

    try:
        row = await db_query_one(
            "SELECT id, status, content_text, category, market "
            "FROM experience_store WHERE id = $1",
            entry_id,
        )

        if not row:
            await update.message.reply_text(f"未找到 ID {entry_id} 的经验条目")
            return

        entry = dict(row)
        if entry["status"] == "deprecated":
            await update.message.reply_text(f"ID {entry_id} 已是废弃状态")
            return

        await db_execute(
            "UPDATE experience_store "
            "SET status = 'deprecated', last_validated = $1 "
            "WHERE id = $2",
            date.today(),
            entry_id,
        )

        content_preview = (entry.get("content_text") or "")[:100]
        category: str = entry.get("category") or "N/A"
        market: str = entry.get("market") or "N/A"

        text = (
            f'❌ <b>经验条目已废弃</b> (ID {entry_id})\n'
            f'"{content_preview}"\n'
            f"类别: {category} | 市场: {market}"
        )
        await update.message.reply_text(text, parse_mode="HTML")
        logger.info("experience_rejected", entry_id=entry_id, category=category)

    except Exception as e:
        logger.error("cmd_reject_error", entry_id=entry_id, error=str(e))
        await update.message.reply_text(f"⚠️ 操作失败: {e}")


# ──────────────────────────────────────────────────────────────
# 内部工具
# ──────────────────────────────────────────────────────────────


def _parse_id(args: list[str] | None) -> int | None:
    """从命令参数中解析整数 ID。

    Args:
        args: context.args 列表，可为 None 或空列表。

    Returns:
        整数 ID，解析失败时返回 None。
    """
    if not args:
        return None
    try:
        return int(args[0])
    except (ValueError, IndexError):
        return None
