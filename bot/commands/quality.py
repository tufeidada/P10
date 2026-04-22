"""
Telegram /quality 命令处理器 — 查看信号规则历史表现。

Usage:
    /quality                        — 显示准确率前 5 的规则
    /quality vwap_pullback_macd_golden — 显示指定规则详细统计
"""

from __future__ import annotations

from typing import Any

import structlog
from telegram import Update
from telegram.ext import ContextTypes

from db.connection import db_query, db_query_one

logger = structlog.get_logger(__name__)


async def cmd_quality(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/quality [rule_name] — 查看信号规则历史表现。

    无参数时展示按准确率排序的前 5 条规则总览；
    传入规则名称时展示该规则的详细统计及按 Regime 分组数据。

    Args:
        update: Telegram Update 对象。
        context: 命令上下文，context.args 含可选规则名称。
    """
    args = context.args or []
    rule_name = args[0] if args else None

    try:
        if rule_name:
            await _show_rule_detail(update, rule_name)
        else:
            await _show_top_rules(update)
    except Exception as e:
        logger.error("cmd_quality_error", error=str(e))
        await update.message.reply_text(f"⚠️ 查询信号质量失败: {e}")


# ──────────────────────────────────────────────────────────────
# 无参数：Top 5 规则排行
# ──────────────────────────────────────────────────────────────


async def _show_top_rules(update: Any) -> None:
    """展示按准确率排序的前 5 条规则。

    Args:
        update: Telegram Update 对象。
    """
    rows = await db_query(
        """
        SELECT rule_name, accuracy, total_signals, correct_signals, avg_return
        FROM signal_quality_tracker
        ORDER BY accuracy DESC NULLS LAST
        LIMIT 5
        """
    )

    if not rows:
        await update.message.reply_text("暂无信号质量数据")
        return

    records = [dict(r) for r in rows]
    text = _format_top_rules(records)
    await update.message.reply_text(text, parse_mode="HTML")


def _format_top_rules(records: list[dict[str, Any]]) -> str:
    """将 Top 5 规则格式化为表格式 HTML 消息。

    Args:
        records: signal_quality_tracker 行记录列表。

    Returns:
        HTML 格式消息字符串。
    """
    header = "📈 <b>信号质量排行</b>"
    sep = "━━━━━━━━━━━━━━━"
    col_header = (
        f"<code>{'规则':<22} {'准确率':>6}  {'触发':>4}  {'均收益':>6}</code>"
    )

    lines: list[str] = [header, sep, col_header]

    for rec in records:
        rule: str = rec.get("rule_name") or ""
        accuracy = rec.get("accuracy")
        total = rec.get("total_signals") or 0
        avg_ret = rec.get("avg_return")

        # 截断规则名至 20 字符
        rule_display = rule[:20] + "…" if len(rule) > 20 else rule

        acc_str = f"{accuracy:.0%}" if accuracy is not None else " N/A"
        ret_str = f"{avg_ret:+.1%}" if avg_ret is not None else "  N/A"

        lines.append(
            f"<code>{rule_display:<21} {acc_str:>6}  {total:>4}  {ret_str:>6}</code>"
        )

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────
# 带参数：单规则详情
# ──────────────────────────────────────────────────────────────


async def _show_rule_detail(update: Any, rule_name: str) -> None:
    """展示单条规则的详细统计数据及 Regime 分组。

    Args:
        update: Telegram Update 对象。
        rule_name: 规则名称。
    """
    # 汇总行（跨所有市场/Regime 的聚合）
    row = await db_query_one(
        """
        SELECT
            rule_name,
            SUM(total_signals)   AS total_signals,
            SUM(correct_signals) AS correct_signals,
            AVG(accuracy)        AS accuracy,
            AVG(avg_return)      AS avg_return,
            AVG(avg_max_dd)      AS avg_max_dd,
            AVG(ic_value)        AS ic_value,
            AVG(ir_value)        AS ir_value
        FROM signal_quality_tracker
        WHERE rule_name = $1
        GROUP BY rule_name
        """,
        rule_name,
    )

    if not row:
        await update.message.reply_text(f"暂无 {rule_name} 的历史数据")
        return

    summary = dict(row)

    # 按 Regime 分组
    regime_rows = await db_query(
        """
        SELECT regime_mode, accuracy, total_signals
        FROM signal_quality_tracker
        WHERE rule_name = $1
        ORDER BY accuracy DESC NULLS LAST
        """,
        rule_name,
    )
    regime_data = [dict(r) for r in regime_rows]

    text = _format_rule_detail(summary, regime_data)
    await update.message.reply_text(text, parse_mode="HTML")


def _format_rule_detail(
    summary: dict[str, Any],
    regime_data: list[dict[str, Any]],
) -> str:
    """将单规则详情格式化为 HTML 消息。

    Args:
        summary: 跨市场/Regime 的聚合统计字典。
        regime_data: 按 Regime 分组的统计列表。

    Returns:
        HTML 格式消息字符串。
    """
    rule_name: str = summary.get("rule_name") or ""
    total = int(summary.get("total_signals") or 0)
    correct = int(summary.get("correct_signals") or 0)
    accuracy = summary.get("accuracy")
    avg_ret = summary.get("avg_return")
    avg_dd = summary.get("avg_max_dd")
    ic = summary.get("ic_value")
    ir = summary.get("ir_value")

    header = f"📈 <b>规则: {rule_name}</b>"
    sep = "━━━━━━━━━━━━━━━"

    acc_str = f"{accuracy:.0%}" if accuracy is not None else "N/A"
    correct_str = f"({correct}/{total} 次正确)"
    accuracy_line = f"准确率: <b>{acc_str}</b> {correct_str}"

    ret_str = f"{avg_ret:+.1%}" if avg_ret is not None else "N/A"
    dd_str = f"{avg_dd:+.1%}" if avg_dd is not None else "N/A"
    ret_line = f"平均收益: {ret_str} | 平均最大回撤: {dd_str}"

    ic_str = f"{ic:.2f}" if ic is not None else "N/A"
    ir_str = f"{ir:.2f}" if ir is not None else "N/A"
    ic_line = f"IC 值: {ic_str} | IR 值: {ir_str}"

    parts: list[str] = [header, sep, accuracy_line, ret_line, ic_line]

    # Regime 分组
    if regime_data:
        parts.append("")
        parts.append("按 Regime 分组:")
        regime_parts: list[str] = []
        for rec in regime_data:
            mode: str = rec.get("regime_mode") or "unknown"
            acc = rec.get("accuracy")
            a_str = f"{acc:.0%}" if acc is not None else "N/A"
            regime_parts.append(f"{mode} {a_str}")
        parts.append("  |  ".join(regime_parts))

    return "\n".join(parts)
