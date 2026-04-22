"""
P10-AlphaRadar Telegram Bot 主入口

启动方式:
    python -m bot.telegram_bot

功能:
    - 双向通信：接收命令、返回分析结果、推送信号
    - 命令路由：各命令分发到 commands/ 下对应处理器
    - 推送接口：供 scheduler/pipeline 调用的主动推送方法
"""

from __future__ import annotations

import os
import signal
import sys

import structlog
from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot.commands.analyze import cmd_analyze
from bot.commands.daily import cmd_daily
from bot.commands.apply_weights import cmd_apply_weights
from bot.commands.approve import cmd_approve, cmd_reject
from bot.commands.health import cmd_health
from bot.commands.help import cmd_help
from bot.commands.performance import cmd_performance
from bot.commands.quality import cmd_quality
from bot.commands.regime import cmd_regime
from bot.commands.review import cmd_review
from bot.commands.signal import cmd_signal
from bot.commands.status import cmd_status
from bot.commands.universe import cmd_universe
from bot.commands.watchlist import cmd_watchlist
from core.invariants import InvariantViolation
from db.connection import close_pool, init_pool

logger = structlog.get_logger(__name__)

_INVARIANT_LOG = "logs/invariant_violations.log"


# ============================================================
# Bot 应用构建
# ============================================================

def build_app() -> Application:
    """构建 Telegram Application 并注册所有命令。"""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("TELEGRAM_BOT_TOKEN 未设置")
        sys.exit(1)

    app = Application.builder().token(token).build()

    # Phase 0 命令
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("watchlist", cmd_watchlist))
    app.add_handler(CommandHandler("dq", cmd_dq))

    # M6 命令（/health /universe /regime 真实实装）
    app.add_handler(CommandHandler("health", cmd_health))
    app.add_handler(CommandHandler("universe", cmd_universe))

    # Phase 1 命令
    app.add_handler(CommandHandler("analyze", cmd_analyze))
    app.add_handler(CommandHandler("regime", cmd_regime))

    # M8 实装命令
    app.add_handler(CommandHandler("daily", cmd_daily))
    # M7 placeholder 命令
    app.add_handler(CommandHandler("judge", _stub_m7))

    # Phase 3 命令
    app.add_handler(CommandHandler("signal", cmd_signal))

    # Phase 5 命令
    app.add_handler(CommandHandler("review", cmd_review))
    app.add_handler(CommandHandler("quality", cmd_quality))
    app.add_handler(CommandHandler("approve", cmd_approve))
    app.add_handler(CommandHandler("reject", cmd_reject))
    app.add_handler(CommandHandler("performance", cmd_performance))
    app.add_handler(CommandHandler("apply_weights", cmd_apply_weights))

    # 未实现命令占位（后续 Phase 中实现）
    for stub_cmd in ("add", "close", "wiki", "macro"):
        app.add_handler(CommandHandler(stub_cmd, _stub_handler))

    # 未知消息
    app.add_handler(MessageHandler(filters.COMMAND, _unknown_command))

    # 不变量违规全局处理器
    app.add_error_handler(_invariant_error_handler)

    # 生命周期钩子
    app.post_init = _on_startup
    app.post_shutdown = _on_shutdown

    return app


# ============================================================
# 生命周期
# ============================================================

async def _on_startup(app: Application) -> None:
    """Bot 启动时初始化数据库连接池 + 设置命令菜单。"""
    await init_pool()

    commands = [
        BotCommand("help", "显示命令列表"),
        BotCommand("status", "持仓风险概览"),
        BotCommand("daily", "今日综合日报"),
        BotCommand("analyze", "多维分析 (代码)"),
        BotCommand("regime", "当前 regime 状态"),
        BotCommand("signal", "活跃买卖信号"),
        BotCommand("watchlist", "管理候选池"),
        BotCommand("add", "记录建仓"),
        BotCommand("close", "记录平仓"),
        BotCommand("review", "本周复盘摘要"),
        BotCommand("quality", "信号规则历史表现"),
        BotCommand("approve", "激活经验条目"),
        BotCommand("reject", "废弃经验条目"),
        BotCommand("performance", "系统整体表现"),
        BotCommand("apply_weights", "应用月报建议权重"),
        BotCommand("dq", "数据质量状态"),
    ]
    await app.bot.set_my_commands(commands)
    logger.info("telegram_bot_started")

    # 推送启动通知
    try:
        from datetime import datetime
        import pytz
        now_cn = datetime.now(pytz.timezone("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
        pusher = TelegramPusher()
        await pusher.send(f"✅ <b>Bot started</b> [{now_cn}]\n命令菜单已更新，共 {len(commands)} 个命令。")
    except Exception as notify_err:
        logger.warning("bot_startup_notify_failed", error=str(notify_err))


async def _on_shutdown(app: Application) -> None:
    """Bot 关闭时清理资源。"""
    await close_pool()
    logger.info("telegram_bot_stopped")


# ============================================================
# 占位/工具命令
# ============================================================

async def _stub_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """尚未实现的命令占位。"""
    cmd = update.message.text.split()[0] if update.message.text else "unknown"
    await update.message.reply_text(f"🚧 {cmd} 命令将在后续 Phase 中实现。")


async def _stub_m7(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """M7/M8 尚未实装的命令。"""
    cmd = update.message.text.split()[0] if update.message.text else "unknown"
    await update.message.reply_text(
        f"🚧 <b>{cmd}</b> 将在 M7/M8 中实装，当前返回 coming soon。",
        parse_mode="HTML",
    )


async def _unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """未识别的命令。"""
    await update.message.reply_text("❓ 未知命令。输入 /help 查看可用命令。")


async def _invariant_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """捕获 InvariantViolation，写日志并推送 Telegram 红色告警。"""
    if not isinstance(context.error, InvariantViolation):
        raise context.error  # 非不变量错误继续向上传播

    import os
    from pathlib import Path
    from datetime import datetime

    msg = str(context.error)
    logger.error("invariant_violation", error=msg)

    # 写文件日志
    log_path = Path(_INVARIANT_LOG)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now().isoformat()}] {msg}\n")

    # 推送 Telegram 告警（尽力，失败不影响主流程）
    try:
        pusher = TelegramPusher()
        await pusher.send(f"🚨 <b>INVARIANT VIOLATION</b>\n<code>{msg}</code>")
    except Exception as push_err:
        logger.error("invariant_alert_push_failed", error=str(push_err))


async def cmd_dq(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/dq — 数据质量检查状态。"""
    from bot.formatter import format_dq_report
    from db.connection import db_query

    try:
        rows = await db_query(
            """
            SELECT source_name, check_type, status, detail,
                   latest_date, expected_date, check_time
            FROM data_quality_checks
            WHERE check_time > NOW() - INTERVAL '24 hours'
            ORDER BY check_time DESC
            LIMIT 20
            """
        )
        checks = [dict(r) for r in rows]
        text = format_dq_report(checks)
        await update.message.reply_text(text, parse_mode="HTML")

    except Exception as e:
        logger.error("cmd_dq_error", error=str(e))
        await update.message.reply_text(f"⚠️ 查询失败: {e}")


# ============================================================
# 推送接口（供 scheduler/pipeline 外部调用）
# ============================================================

class TelegramPusher:
    """主动推送消息到 Telegram chat。

    Usage:
        pusher = TelegramPusher()
        await pusher.send("分析完成: AAPL bullish")
        await pusher.send_html("<b>信号</b>: 600519.SH 买入")
    """

    def __init__(self, bot_token: str | None = None, chat_id: str | None = None):
        self.bot_token = bot_token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")

    async def send(self, text: str, parse_mode: str = "HTML") -> bool:
        """发送消息到配置的 chat。"""
        if not self.bot_token or not self.chat_id:
            logger.warning("telegram_pusher_not_configured")
            return False

        try:
            from telegram import Bot
            bot = Bot(token=self.bot_token)
            await bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode=parse_mode,
            )
            return True
        except Exception as e:
            logger.error("telegram_push_error", error=str(e))
            return False

    async def send_html(self, html: str) -> bool:
        """发送 HTML 格式消息。"""
        return await self.send(html, parse_mode="HTML")


# ============================================================
# 主入口
# ============================================================

def main() -> None:
    """启动 Telegram Bot（polling 模式）。"""
    from dotenv import load_dotenv
    load_dotenv()

    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
    )

    app = build_app()
    logger.info("starting_polling")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
