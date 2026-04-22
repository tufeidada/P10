"""
Telegram /health 命令 — 显示系统数据新鲜度 + scheduler job 状态。
"""

from __future__ import annotations

from datetime import datetime, timezone

import structlog
from telegram import Update
from telegram.ext import ContextTypes

from db.connection import db_query, db_query_val

logger = structlog.get_logger(__name__)


async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/health — 数据健康度 + scheduler 各 job 最近状态。"""
    try:
        lines = ["🩺 <b>系统健康状态</b>\n"]

        # ── Scheduler 心跳 ──
        last_hb = await db_query_val(
            "SELECT MAX(beat_time) FROM scheduler_heartbeat"
        )
        if last_hb:
            now_utc = datetime.now(timezone.utc)
            if last_hb.tzinfo is None:
                from datetime import timezone as tz
                last_hb = last_hb.replace(tzinfo=tz.utc)
            lag_min = int((now_utc - last_hb).total_seconds() / 60)
            hb_emoji = "✅" if lag_min < 35 else "⚠️"
            lines.append(f"{hb_emoji} <b>Scheduler</b>: 最近心跳 {lag_min} 分钟前")
        else:
            lines.append("❌ <b>Scheduler</b>: 无心跳记录（未启动？）")

        # ── Job 最近状态（最近 24h）──
        lines.append("\n<b>Jobs（最近24h）</b>")
        job_rows = await db_query(
            """
            SELECT
                job_name,
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE status = 'success') AS success,
                COUNT(*) FILTER (WHERE status = 'failed')  AS failed,
                MAX(trigger_time) AS last_run,
                (ARRAY_AGG(status ORDER BY trigger_time DESC))[1] AS last_status
            FROM scheduler_job_log
            WHERE trigger_time > NOW() - INTERVAL '24 hours'
            GROUP BY job_name
            ORDER BY job_name
            """
        )
        if job_rows:
            for r in job_rows:
                emoji = {"success": "✅", "failed": "❌", "skipped": "⏭️",
                         "invariant": "🚨"}.get(r["last_status"], "❓")
                last = r["last_run"].strftime("%H:%M") if r["last_run"] else "N/A"
                lines.append(
                    f"{emoji} {r['job_name']}: {r['success']}/{r['total']} ok  @{last}"
                )
        else:
            lines.append("  (暂无记录，scheduler 可能刚启动)")

        # ── 数据新鲜度摘要 ──
        lines.append("\n<b>数据新鲜度</b>")
        fresh_rows = await db_query(
            """
            SELECT source_name, status, lag_days, max_date
            FROM data_freshness_log
            WHERE check_time = (
                SELECT MAX(check_time) FROM data_freshness_log
            )
            AND status != 'ok'
            ORDER BY status DESC, source_name
            LIMIT 10
            """
        )
        if fresh_rows:
            for r in fresh_rows:
                emoji = "🚨" if r["status"] == "critical" else "⚠️"
                lines.append(
                    f"{emoji} {r['source_name']}: lag={r['lag_days']}d  ({r['max_date']})"
                )
        else:
            lines.append("  ✅ 所有数据源正常")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    except Exception as e:
        logger.error("cmd_health_error", error=str(e))
        await update.message.reply_text(f"⚠️ health 查询失败: {e}")
