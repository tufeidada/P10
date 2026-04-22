"""
Telegram /review 命令处理器 — 最近一期周报摘要。

Usage:
    /review
"""

from __future__ import annotations

import json
from typing import Any

import structlog
from telegram import Update
from telegram.ext import ContextTypes

from db.connection import db_query_one

logger = structlog.get_logger(__name__)


async def cmd_review(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/review — 返回最近一期周报摘要。

    从 review_reports 表取最新一条记录，格式化关键指标、摘要、
    发现要点及系统建议后回复。

    Args:
        update: Telegram Update 对象。
        context: 命令上下文（本命令无参数）。
    """
    try:
        row = await db_query_one(
            "SELECT * FROM review_reports ORDER BY created_at DESC LIMIT 1"
        )
        if not row:
            await update.message.reply_text(
                "暂无复盘报告，将在每周六自动生成"
            )
            return

        report = dict(row)
        text = _format_review(report)
        await update.message.reply_text(text, parse_mode="HTML")

    except Exception as e:
        logger.error("cmd_review_error", error=str(e))
        await update.message.reply_text(f"⚠️ 查询复盘报告失败: {e}")


# ──────────────────────────────────────────────────────────────
# 格式化
# ──────────────────────────────────────────────────────────────


def _format_review(report: dict[str, Any]) -> str:
    """将复盘报告记录格式化为 Telegram HTML 消息。

    Args:
        report: review_reports 表的一行记录字典。

    Returns:
        HTML 格式消息字符串。
    """
    report_date = report.get("report_date")
    created_at = report.get("created_at")

    # 标题行：使用 report_date 推算周期
    if report_date:
        date_str = str(report_date)
        # report_date 是周末日期，本周起始 = report_date - 6天（近似）
        try:
            from datetime import datetime, timedelta
            rd = (
                report_date
                if hasattr(report_date, "strftime")
                else datetime.fromisoformat(str(report_date)).date()
            )
            start_str = (rd - timedelta(days=6)).strftime("%Y-%m-%d")
            end_str = rd.strftime("%Y-%m-%d")
            period_header = f"📊 <b>周度复盘</b> {start_str} ~ {end_str}"
        except Exception:
            period_header = f"📊 <b>周度复盘</b> {report_date}"
    else:
        period_header = "📊 <b>周度复盘</b>"

    sep = "━━━━━━━━━━━━━━━"

    # 核心指标行
    total = report.get("total_judgments") or 0
    acc_short = report.get("accuracy_short")
    alpha = report.get("alpha_vs_benchmark")

    acc_str = f"{acc_short:.0%}" if acc_short is not None else "N/A"
    alpha_str = f"{alpha:+.1%}" if alpha is not None else "N/A"
    metrics_line = f"判断: {total} | 准确率: {acc_str} | Alpha: {alpha_str}"

    # 摘要文本（前 300 字）
    summary_text: str = report.get("summary_text") or ""
    summary_display = summary_text[:300]
    if len(summary_text) > 300:
        summary_display += "..."

    # key_findings — 可能是 JSON 数组或纯文本
    key_findings = report.get("key_findings")
    findings_lines: list[str] = []
    if key_findings:
        parsed = _parse_json_field(key_findings)
        if isinstance(parsed, list):
            for item in parsed[:5]:  # 最多 5 条
                findings_lines.append(f"• {item}")
        elif isinstance(parsed, str) and parsed.strip():
            for line in parsed.strip().splitlines()[:5]:
                if line.strip():
                    findings_lines.append(f"• {line.strip()}")

    # 系统建议（取第一条）
    suggested_changes = report.get("suggested_changes")
    first_suggestion = ""
    if suggested_changes:
        parsed_suggestions = _parse_json_field(suggested_changes)
        if isinstance(parsed_suggestions, list) and parsed_suggestions:
            first_suggestion = str(parsed_suggestions[0])
        elif isinstance(parsed_suggestions, str) and parsed_suggestions.strip():
            first_suggestion = parsed_suggestions.strip().splitlines()[0]

    # 生成时间
    time_str = ""
    if created_at:
        try:
            ts = (
                created_at
                if hasattr(created_at, "strftime")
                else __import__("datetime").datetime.fromisoformat(str(created_at))
            )
            time_str = ts.strftime("%m-%d %H:%M")
        except Exception:
            time_str = str(created_at)[:16]

    # 组装消息
    parts: list[str] = [period_header, sep, metrics_line]

    if summary_display:
        parts.append("")
        parts.append(summary_display)

    if findings_lines:
        parts.append("")
        parts.extend(findings_lines)

    if first_suggestion:
        parts.append("")
        parts.append(f"💡 系统建议: {first_suggestion}")

    if time_str:
        parts.append("")
        parts.append(f"🕐 生成时间: {time_str}")

    return "\n".join(parts)


def _parse_json_field(value: Any) -> Any:
    """尝试将字段解析为 JSON，失败则原样返回字符串。

    Args:
        value: 字段值，可能是字符串或已解析对象。

    Returns:
        解析结果（list/dict/str）。
    """
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return value
    return str(value)
