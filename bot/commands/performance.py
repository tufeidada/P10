"""
Telegram /performance 命令处理器 — 系统整体表现 vs 基准。

Usage:
    /performance
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import structlog
from telegram import Update
from telegram.ext import ContextTypes

from db.connection import db_query, db_query_one, db_query_val

logger = structlog.get_logger(__name__)


async def cmd_performance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/performance — 系统整体表现 vs 基准。

    汇总 judgments 表准确率、benchmark_daily 的 HS300 近30日累计收益、
    最近一期 alpha_vs_benchmark，以及 signal_quality_tracker 最佳/最差规则。
    数据不足时以 N/A 补位。

    Args:
        update: Telegram Update 对象。
        context: 命令上下文（本命令无参数）。
    """
    msg = await update.message.reply_text("⏳ 正在统计系统表现...")

    try:
        stats = await _gather_stats()
        text = _format_performance(stats)
        await msg.edit_text(text, parse_mode="HTML")
    except Exception as e:
        logger.error("cmd_performance_error", error=str(e))
        await msg.edit_text(f"⚠️ 统计失败: {e}")


# ──────────────────────────────────────────────────────────────
# 数据采集
# ──────────────────────────────────────────────────────────────


async def _gather_stats() -> dict[str, Any]:
    """从多个数据源汇总表现统计数据。

    Returns:
        包含各维度统计数据的字典，缺失字段为 None。
    """
    stats: dict[str, Any] = {}

    # 1. judgments 表整体准确率（已验证 = is_correct IS NOT NULL）
    try:
        j_row = await db_query_one(
            """
            SELECT
                COUNT(*)                                          AS total,
                COUNT(*) FILTER (WHERE is_correct IS NOT NULL)   AS verified,
                COUNT(*) FILTER (WHERE is_correct = TRUE)        AS correct
            FROM judgments
            """
        )
        if j_row:
            stats["total_judgments"] = int(j_row["total"] or 0)
            stats["verified"] = int(j_row["verified"] or 0)
            stats["correct"] = int(j_row["correct"] or 0)
            verified = stats["verified"]
            stats["total_acc"] = (
                stats["correct"] / verified if verified > 0 else None
            )
    except Exception as e:
        logger.warning("performance_judgments_error", error=str(e))

    # 2. 短期 / 中期准确率（来自 review_reports 最新一条）
    try:
        rr_row = await db_query_one(
            "SELECT accuracy_short, accuracy_mid, alpha_vs_benchmark "
            "FROM review_reports ORDER BY created_at DESC LIMIT 1"
        )
        if rr_row:
            stats["short_acc"] = rr_row["accuracy_short"]
            stats["mid_acc"] = rr_row["accuracy_mid"]
            stats["alpha"] = rr_row["alpha_vs_benchmark"]
    except Exception as e:
        logger.warning("performance_review_reports_error", error=str(e))

    # 3. benchmark_daily: HS300 近30日累计收益
    try:
        cutoff = date.today() - timedelta(days=30)
        bm_rows = await db_query(
            """
            SELECT cumulative_return
            FROM benchmark_daily
            WHERE market = 'CN'
              AND benchmark_name ILIKE '%hs300%'
              AND trade_date >= $1
            ORDER BY trade_date DESC
            LIMIT 1
            """,
            cutoff,
        )
        if bm_rows:
            stats["hs300_ret"] = float(bm_rows[0]["cumulative_return"])
    except Exception as e:
        logger.warning("performance_benchmark_error", error=str(e))

    # 4. signal_quality_tracker 最佳 / 最差规则
    try:
        sq_rows = await db_query(
            """
            SELECT rule_name, accuracy
            FROM (
                SELECT rule_name, AVG(accuracy) AS accuracy
                FROM signal_quality_tracker
                GROUP BY rule_name
            ) t
            WHERE accuracy IS NOT NULL
            ORDER BY accuracy DESC
            """
        )
        if sq_rows:
            stats["best_rule"] = sq_rows[0]["rule_name"]
            stats["best_acc"] = float(sq_rows[0]["accuracy"])
            stats["worst_rule"] = sq_rows[-1]["rule_name"]
            stats["worst_acc"] = float(sq_rows[-1]["accuracy"])
    except Exception as e:
        logger.warning("performance_signal_quality_error", error=str(e))

    return stats


# ──────────────────────────────────────────────────────────────
# 格式化
# ──────────────────────────────────────────────────────────────


def _format_performance(stats: dict[str, Any]) -> str:
    """将汇总统计格式化为 Telegram HTML 消息。

    Args:
        stats: _gather_stats() 返回的统计字典。

    Returns:
        HTML 格式消息字符串。
    """
    sep = "━━━━━━━━━━━━━━━"
    today_str = date.today().strftime("%Y-%m-%d")

    header = f"📊 <b>系统表现概览</b>"

    # 判断计数行
    total = stats.get("total_judgments", 0)
    verified = stats.get("verified", 0)
    correct = stats.get("correct", 0)
    counts_line = f"总判断 {total} / 已验证 {verified} / 正确 {correct}"

    # 准确率行
    total_acc = stats.get("total_acc")
    short_acc = stats.get("short_acc")
    mid_acc = stats.get("mid_acc")

    total_acc_str = f"{total_acc:.0%}" if total_acc is not None else "N/A"
    short_acc_str = f"{short_acc:.0%}" if short_acc is not None else "N/A"
    mid_acc_str = f"{mid_acc:.0%}" if mid_acc is not None else "N/A"

    acc_header = "判断准确率:"
    overall_line = f"  总体: <b>{total_acc_str}</b>  ({correct}/{verified} 次)"
    period_line = f"  短期: {short_acc_str}  中期: {mid_acc_str}"

    # vs 基准
    hs300_ret = stats.get("hs300_ret")
    alpha = stats.get("alpha")

    hs300_str = f"{hs300_ret:+.1%}" if hs300_ret is not None else "N/A"
    alpha_str = f"{alpha:+.1%}" if alpha is not None else "N/A"

    # 系统估算收益：HS300 + Alpha（如有）
    if hs300_ret is not None and alpha is not None:
        our_ret = float(hs300_ret) + float(alpha)
        our_ret_str = f"{our_ret:+.1%}"
    else:
        our_ret_str = "N/A"

    bm_header = "vs 基准 (近30日):"
    hs300_line = f"  HS300:   {hs300_str} 累计"
    our_line = f"  系统:    {our_ret_str} 估算"
    alpha_line = f"  Alpha:   {alpha_str}"

    # 信号质量
    best_rule = stats.get("best_rule")
    best_acc = stats.get("best_acc")
    worst_rule = stats.get("worst_rule")
    worst_acc = stats.get("worst_acc")

    sq_header = "信号质量 (已回填):"
    if best_rule and best_acc is not None:
        best_str = f"  最佳规则: {best_rule} ({best_acc:.0%})"
    else:
        best_str = "  最佳规则: N/A"

    if worst_rule and worst_acc is not None:
        worst_str = f"  最差规则: {worst_rule} ({worst_acc:.0%})"
    else:
        worst_str = "  最差规则: N/A"

    parts: list[str] = [
        header,
        sep,
        counts_line,
        "",
        acc_header,
        overall_line,
        period_line,
        "",
        bm_header,
        hs300_line,
        our_line,
        alpha_line,
        "",
        sq_header,
        best_str,
        worst_str,
        "",
        f"🕐 {today_str}",
    ]

    return "\n".join(parts)
