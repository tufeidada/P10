"""Telegram /help 命令处理器。"""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from bot.formatter import format_help


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/help — 显示所有可用命令。"""
    await update.message.reply_text(format_help(), parse_mode="HTML")
