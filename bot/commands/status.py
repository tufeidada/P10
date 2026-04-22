"""Telegram /status 命令处理器 — 系统状态概览。"""

from __future__ import annotations

from datetime import date, datetime, timezone

import structlog
from telegram import Update
from telegram.ext import ContextTypes

from db.connection import db_query, db_query_one, db_query_val

logger = structlog.get_logger(__name__)

_REGIME_LABELS: dict[str, str] = {
    "offense": "进攻 🟢",
    "cautious_offense": "谨慎进攻 🟡",
    "defense": "防守 🟠",
    "risk_off": "避险 🔴",
}


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/status — 系统状态概览（regime / judgments / scheduler / 成本）。"""
    try:
        lines = await _build_status()
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    except Exception as e:
        logger.error("cmd_status_error", error=str(e))
        await update.message.reply_text(f"⚠️ 查询失败: {e}")


async def _build_status() -> list[str]:
    """构建状态报告行列表。"""
    lines: list[str] = []
    now_cn = datetime.now(tz=timezone.utc).astimezone(
        __import__("zoneinfo").ZoneInfo("Asia/Shanghai")
    ).strftime("%Y-%m-%d %H:%M")
    lines.append(f"🖥 <b>系统状态</b>  <code>{now_cn}</code>")
    lines.append("")

    # ── Regime ──
    lines.append("🌡 <b>Regime</b>")
    for mkt in ("CN", "US"):
        row = await db_query_one(
            "SELECT regime_mode, trade_date FROM regime_daily WHERE market=$1 ORDER BY trade_date DESC LIMIT 1",
            mkt,
        )
        if row:
            label = _REGIME_LABELS.get(row["regime_mode"], row["regime_mode"])
            lines.append(f"  {mkt}: {label}  ({row['trade_date']})")
        else:
            lines.append(f"  {mkt}: 暂无数据")
    lines.append("")

    # ── Judgments 覆盖 ──
    lines.append("📊 <b>今日判断</b>")
    today = date.today()
    total_active = await db_query_val("SELECT COUNT(*) FROM stock_universe WHERE active=TRUE")
    cnt_cn = await db_query_val(
        "SELECT COUNT(DISTINCT symbol) FROM judgments WHERE judgment_date=$1 AND market='CN'", today
    )
    cnt_us = await db_query_val(
        "SELECT COUNT(DISTINCT symbol) FROM judgments WHERE judgment_date=$1 AND market='US'", today
    )
    cnt_today = (cnt_cn or 0) + (cnt_us or 0)
    lines.append(f"  今日覆盖: {cnt_today}/{total_active}  (CN {cnt_cn or 0} / US {cnt_us or 0})")

    # latest judgment date if today is empty
    if cnt_today == 0:
        latest_date = await db_query_val(
            "SELECT MAX(judgment_date) FROM judgments WHERE fundamental_bug_affected IS NOT TRUE"
        )
        if latest_date:
            lines.append(f"  最近判断日: {latest_date}")
    lines.append("")

    # ── Scheduler 心跳 ──
    lines.append("⚙️ <b>Scheduler</b>")
    heartbeat = await db_query_one(
        "SELECT beat_time, pid FROM scheduler_heartbeat ORDER BY beat_time DESC LIMIT 1"
    )
    if heartbeat:
        hb_time = heartbeat["beat_time"]
        if hb_time:
            now_utc = datetime.now(tz=timezone.utc)
            hb_utc = hb_time if hb_time.tzinfo else hb_time.replace(tzinfo=timezone.utc)
            lag_min = int((now_utc - hb_utc).total_seconds() / 60)
            status_icon = "✅" if lag_min < 35 else "⚠️"
            lines.append(f"  {status_icon} 最后心跳: {hb_utc.strftime('%H:%M')} UTC ({lag_min}min ago)")
        else:
            lines.append("  ⚠️ 心跳时间未记录")
    else:
        lines.append("  ⚠️ Scheduler 未启动或心跳表为空")

    # last composite job
    last_composite = await db_query_one(
        "SELECT job_name, status, trigger_time FROM scheduler_job_log "
        "WHERE job_name LIKE 'run_composite%' ORDER BY trigger_time DESC LIMIT 1"
    )
    if last_composite:
        icon = "✅" if last_composite["status"] == "success" else "⚠️"
        lines.append(f"  {icon} 最近 composite: {last_composite['job_name']} [{last_composite['status']}]")
    lines.append("")

    # ── Fundamental bug 状态 ──
    lines.append("🔧 <b>已知修复</b>")
    lines.append("  ✅ Fundamental ×100 bug 已修复 (2026-04-21)")
    lines.append("  ✅ CN/US 存储格式分支处理")
    lines.append("")

    # ── 数据新鲜度摘要（快查：用 data_quality_checks 记录）──
    lines.append("📡 <b>数据新鲜度</b>")
    try:
        dq_rows = await db_query(
            """
            SELECT status, COUNT(*) AS cnt
            FROM data_quality_checks
            WHERE check_time > NOW() - INTERVAL '25 hours'
            GROUP BY status
            """
        )
        counts = {r["status"]: int(r["cnt"]) for r in dq_rows}
        critical = counts.get("critical", 0)
        warn = counts.get("warn", 0)
        ok = counts.get("ok", 0)
        if critical > 0:
            lines.append(f"  🔴 {critical} critical / {warn} warn / {ok} ok")
        elif warn > 0:
            lines.append(f"  🟡 {warn} warn / {ok} ok  (宏观月频数据停更属正常)")
        elif ok > 0:
            lines.append(f"  ✅ 所有数据源正常 ({ok} ok)")
        else:
            lines.append("  ⚪ 暂无新鲜度检查记录（scheduler 尚未运行）")
    except Exception as e:
        lines.append(f"  ⚠️ 无法查询 ({e})")
    lines.append("")

    # ── LLM 成本 ──
    lines.append("💰 <b>LLM 成本</b>")
    cost_today = await db_query_val(
        "SELECT COALESCE(SUM(cost_cny),0) FROM llm_cost_log WHERE DATE(call_time)=$1", today
    )
    cost_7d_avg = await db_query_val(
        """
        SELECT COALESCE(SUM(cost_cny),0) / GREATEST(COUNT(DISTINCT DATE(call_time)),1)
        FROM llm_cost_log WHERE call_time >= NOW() - INTERVAL '7 days'
        """
    )
    budget = 100.0
    cost_f = float(cost_today or 0)
    avg_f = float(cost_7d_avg or 0)
    monthly_est = avg_f * 30
    pct = cost_f / budget * 100
    icon = "✅" if pct < 50 else ("⚠️" if pct < 80 else "🔴")
    lines.append(f"  {icon} 今日: ¥{cost_f:.4f}  ({pct:.1f}% / ¥{budget:.0f})")
    lines.append(f"  📈 近7天日均: ¥{avg_f:.4f} | 月预估: ¥{monthly_est:.2f}")

    # LLM direction unknown ratio today
    llm_q = await db_query_one(
        """
        SELECT COUNT(*) AS total,
               COUNT(*) FILTER (WHERE llm_direction = 'unknown' OR llm_direction IS NULL) AS unknown_cnt
        FROM judgments WHERE judgment_date = $1
        """,
        today,
    )
    if llm_q and int(llm_q["total"]) > 0:
        total_j = int(llm_q["total"])
        unknown_j = int(llm_q["unknown_cnt"])
        ratio = unknown_j / total_j
        q_icon = "✅" if ratio <= 0.1 else ("⚠️" if ratio <= 0.3 else "🔴")
        lines.append(f"  {q_icon} LLM方向未知: {unknown_j}/{total_j} ({ratio:.0%})")
    lines.append("")

    # ── Backfill 状态 ──
    lines.append("🔄 <b>回填状态</b>")
    backfill_row = await db_query_one(
        "SELECT status, trigger_time FROM scheduler_job_log "
        "WHERE job_name LIKE '%backfill%' ORDER BY trigger_time DESC LIMIT 1"
    )
    if backfill_row:
        b_icon = "✅" if backfill_row["status"] == "success" else "⚠️"
        bt = backfill_row["trigger_time"]
        bt_str = bt.strftime("%m-%d %H:%M") if bt else "--"
        lines.append(f"  {b_icon} 最近回填: {bt_str} [{backfill_row['status']}]")
    else:
        lines.append("  ⚪ 尚未运行过回填任务")
    lines.append("")

    # ── 下次日报时间 ──
    lines.append("📅 <b>下次日报</b>")
    lines.append("  CN: 今日 16:30 北京时间")
    lines.append("  US: 明日 07:00 北京时间")

    return lines
