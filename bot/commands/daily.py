"""
Telegram /daily 命令处理器 — 今日综合日报。

Usage:
    /daily        — 全市场日报
    /daily CN     — 仅 A 股
    /daily US     — 仅美股
"""

from __future__ import annotations

from datetime import date
from typing import Any

import structlog
from telegram import Update
from telegram.ext import ContextTypes

from db.connection import db_query, db_query_one

logger = structlog.get_logger(__name__)

# ============================================================
# Regime 标签
# ============================================================

_REGIME_LABELS: dict[str, str] = {
    "offense": "进攻模式 🟢",
    "cautious_offense": "谨慎进攻 🟡",
    "defense": "防守模式 🟠",
    "risk_off": "避险模式 🔴",
}

_DIRECTION_EMOJI: dict[str, str] = {
    "bullish": "🟢",
    "bearish": "🔴",
    "neutral": "🟡",
}

_DIRECTION_CN: dict[str, str] = {
    "bullish": "看多",
    "bearish": "看空",
    "neutral": "中性",
}

_ACTION_CN: dict[str, str] = {
    "buy": "建仓",
    "hold": "持有",
    "sell": "减仓",
    "watch": "观察",
    "avoid": "回避",
}


# ============================================================
# 辅助函数
# ============================================================

def _confidence_bar(confidence: float, width: int = 6) -> str:
    """将置信度渲染为进度条。

    Args:
        confidence: 0.0-1.0 的置信度。
        width: 进度条宽度。

    Returns:
        进度条字符串，例如 "████░░ 67%"。
    """
    clamped = max(0.0, min(1.0, float(confidence)))
    filled = int(clamped * width)
    empty = width - filled
    pct = int(clamped * 100)
    return f"{'█' * filled}{'░' * empty} {pct}%"


def _regime_summary(regime: dict) -> str:
    """格式化 regime 单行摘要。

    Args:
        regime: regime_daily 查询结果字典。

    Returns:
        例如 "防守模式 🟠  trend=45 breadth=40"
    """
    mode = regime.get("regime_mode", "N/A")
    label = _REGIME_LABELS.get(mode, mode)
    trend = regime.get("trend_score")
    breadth = regime.get("breadth_score")

    parts = [f"<b>{label}</b>"]
    scores: list[str] = []
    if trend is not None:
        scores.append(f"trend={float(trend):.0f}")
    if breadth is not None:
        scores.append(f"breadth={float(breadth):.0f}")
    if scores:
        parts.append("  " + " ".join(scores))

    return "".join(parts)


_SIG_DISPLAY: dict[str, str] = {
    "strong_buy":  "STRONG_BUY",
    "buy":         "BUY",
    "weak_buy":    "WEAK_BUY",
    "hold":        "HOLD",
    "weak_sell":   "WEAK_SELL",
    "sell":        "SELL",
    "strong_sell": "STRONG_SELL",
}

_BULL_STRENGTHS = ("strong_buy", "buy", "weak_buy")
_BEAR_STRENGTHS = ("strong_sell", "sell", "weak_sell")
_STRENGTH_RANK: dict[str, int] = {
    "strong_buy": 0, "buy": 1, "weak_buy": 2,
    "strong_sell": 0, "sell": 1, "weak_sell": 2,
}


def _group_signals(
    judgments: list[dict],
    strengths: tuple[str, ...],
) -> tuple[list[dict], dict[str, int]]:
    """按 rule_signal_strength 分组并排序（强度优先，同档内 composite 降序）。"""
    subset = [r for r in judgments if r.get("rule_signal_strength") in strengths]
    subset.sort(key=lambda r: (
        _STRENGTH_RANK.get(r.get("rule_signal_strength", ""), 99),
        -float(r.get("composite_score") or 0),
    ))
    counts = {s: sum(1 for r in subset if r.get("rule_signal_strength") == s) for s in strengths}
    return subset, counts


def _format_judgment_line(row: dict, show_name: bool = True) -> str:
    """格式化单只股票的判断摘要行（双信号格式）。

    Args:
        row: judgments 查询结果字典。
        show_name: 是否显示股票名称。

    Returns:
        例如 "🟢 000960.SZ 锡业股份  71/100  规则:BUY · 置信度 65% | LLM:HOLD  ⚠️"
    """
    symbol = row.get("symbol", "")
    direction = row.get("direction", "neutral")
    score = row.get("composite_score")
    confidence = row.get("confidence") or 0.0
    name = row.get("name", "") or ""
    rule_sig = row.get("rule_signal_strength") or ""
    llm_sig = row.get("llm_signal_strength") or ""
    llm_dir = row.get("llm_direction") or ""

    emoji = _DIRECTION_EMOJI.get(direction, "⚪")
    score_str = f"{float(score):.0f}/100" if score is not None else "N/A"
    conf_pct = f"{int(float(confidence) * 100)}%"
    name_part = f" {name}" if show_name and name else ""

    rule_label = _SIG_DISPLAY.get(rule_sig, rule_sig.upper()) if rule_sig else "?"
    if not llm_sig or llm_sig == "unknown":
        llm_part = "LLM: -"
    else:
        llm_label = _SIG_DISPLAY.get(llm_sig, llm_sig.upper())
        llm_part = f"LLM:{llm_label}"

    divergent = (
        llm_dir and llm_dir not in ("unknown", "")
        and direction and direction != llm_dir
    )
    warn = "  ⚠️" if divergent else ""

    return (
        f"  {emoji} <code>{symbol}</code>{name_part}  {score_str}"
        f"  规则:{rule_label} · 置信度 {conf_pct} | {llm_part}{warn}"
    )


def _neutral_summary(neutrals: list[dict], max_show: int = 5) -> str:
    """格式化中性股票的简短摘要。

    Args:
        neutrals: 中性方向的判断列表。
        max_show: 最多展示几只。

    Returns:
        例如 "前5只: 002050.SZ 60  000063.SZ 54  ..."
    """
    if not neutrals:
        return "  — 无"

    top = neutrals[:max_show]
    parts: list[str] = []
    for r in top:
        symbol = r.get("symbol", "")
        score = r.get("composite_score")
        score_str = f"{float(score):.0f}" if score is not None else "?"
        parts.append(f"<code>{symbol}</code> {score_str}")

    prefix = f"前{max_show}只: " if len(neutrals) > max_show else ""
    return f"  {prefix}{('  '.join(parts))}"


def _build_digest(
    report_date: date,
    market_filter: str | None,
    regime: dict | None,
    judgments: list[dict],
) -> str:
    """构建完整的日报 HTML 消息。

    Args:
        report_date: 日报日期。
        market_filter: 'CN' | 'US' | None（全部）。
        regime: regime_daily 查询结果字典，可为 None。
        judgments: judgments 查询结果字典列表。

    Returns:
        HTML 格式消息文本。
    """
    # 标题行
    market_tag = ""
    if market_filter == "CN":
        market_tag = "  [A股]"
    elif market_filter == "US":
        market_tag = "  [美股]"

    header = f"📅 <b>日报 {report_date.strftime('%Y-%m-%d')}</b>{market_tag}"

    lines: list[str] = [header, ""]

    # Regime 行
    if regime:
        lines.append(f"🌡 Regime: {_regime_summary(regime)}")
    else:
        lines.append("🌡 Regime: 暂无数据")
    lines.append("")

    # 分组（按 rule_signal_strength，不再依赖 direction）
    bullish, bull_counts = _group_signals(judgments, _BULL_STRENGTHS)
    bearish, bear_counts = _group_signals(judgments, _BEAR_STRENGTHS)
    neutral = [r for r in judgments if r.get("rule_signal_strength") == "hold"]

    # 多方
    bull_breakdown = (
        f"强买 {bull_counts['strong_buy']} / 买 {bull_counts['buy']} / 弱买 {bull_counts['weak_buy']}"
    )
    lines.append(f"📈 <b>多方信号 ({len(bullish)})</b>  {bull_breakdown}")
    if bullish:
        for r in bullish[:10]:
            lines.append(_format_judgment_line(r))
    else:
        lines.append("  — 无")
    lines.append("")

    # 空方
    bear_breakdown = (
        f"强卖 {bear_counts['strong_sell']} / 卖 {bear_counts['sell']} / 弱卖 {bear_counts['weak_sell']}"
    )
    lines.append(f"📉 <b>空方信号 ({len(bearish)})</b>  {bear_breakdown}")
    if bearish:
        for r in bearish[:10]:
            lines.append(_format_judgment_line(r))
    else:
        lines.append("  — 无")
    lines.append("")

    # 中性
    lines.append(f"🟡 <b>中性 ({len(neutral)})</b>")
    lines.append(_neutral_summary(neutral))
    lines.append("")

    # 分布统计
    total = len(judgments)
    if total > 0:
        scores = [float(r["composite_score"]) for r in judgments if r.get("composite_score") is not None]
        avg_score = sum(scores) / len(scores) if scores else 0.0
        lines.append("📊 <b>分布</b>")
        lines.append(
            f"  多: {len(bullish)}  中: {len(neutral)}  空: {len(bearish)}"
            f"  平均分: {avg_score:.1f}"
        )
    else:
        lines.append("📊 <b>暂无判断数据</b>")

    return "\n".join(lines)


# ============================================================
# 数据查询
# ============================================================

async def _fetch_latest_regime(market: str | None) -> dict | None:
    """获取最新 regime（单市场或全局第一行）。

    Args:
        market: 'CN' | 'US' | None。None 时取 CN 作为代表。

    Returns:
        regime 字典或 None。
    """
    target_market = market if market else "CN"
    try:
        row = await db_query_one(
            """
            SELECT trade_date, market, regime_mode,
                   trend_score, breadth_score, liquidity_score, volatility_score
            FROM regime_daily
            WHERE market = $1
            ORDER BY trade_date DESC
            LIMIT 1
            """,
            target_market,
        )
        return dict(row) if row else None
    except Exception as e:
        logger.error("daily_fetch_regime_error", market=target_market, error=str(e))
        return None


async def _fetch_judgments(market: str | None) -> tuple[date, list[dict]]:
    """获取最近日期的判断记录，附带股票名称。

    Args:
        market: 'CN' | 'US' | None（全部）。

    Returns:
        (判断日期, 判断列表)。列表为空时日期为今日。
    """
    try:
        # 先找最新的 judgment_date
        if market:
            latest_date = await db_query_one(
                """
                SELECT MAX(judgment_date) AS max_date
                FROM judgments
                WHERE market = $1
                """,
                market,
            )
        else:
            latest_date = await db_query_one(
                "SELECT MAX(judgment_date) AS max_date FROM judgments"
            )

        if not latest_date or latest_date["max_date"] is None:
            return date.today(), []

        j_date: date = latest_date["max_date"]

        # 取该日期的最新一条判断（每只股票）并 JOIN 股票名称
        rows = await db_query(
            """
            SELECT DISTINCT ON (j.symbol)
                j.symbol,
                j.market,
                j.composite_score,
                j.direction,
                j.confidence,
                j.technical_score,
                j.fundamental_score,
                j.flow_score,
                j.sentiment_score,
                j.suggested_action,
                j.regime_at_time,
                j.rule_signal_strength,
                j.llm_direction,
                j.llm_signal_strength,
                COALESCE(u.name, ic.sw1_name, '') AS name
            FROM judgments j
            LEFT JOIN stock_universe u ON u.symbol = j.symbol
            LEFT JOIN industry_classify ic ON ic.symbol = j.symbol
            WHERE j.judgment_date = $1
              AND ($2::text IS NULL OR j.market = $2)
            ORDER BY j.symbol, j.id DESC
            """,
            j_date,
            market,
        )
        result = [dict(r) for r in rows]
        # 按 composite_score 降序排列
        result.sort(key=lambda r: float(r.get("composite_score") or 0), reverse=True)
        return j_date, result

    except Exception as e:
        logger.error("daily_fetch_judgments_error", market=market, error=str(e))
        return date.today(), []


# ============================================================
# 命令处理器
# ============================================================

async def cmd_daily(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/daily [CN|US] — 生成并发送今日综合日报。

    可选参数 CN 或 US 过滤市场。无参数时展示全市场数据。
    """
    args = context.args or []
    market_filter: str | None = None

    if args:
        raw = args[0].upper()
        if raw in ("CN", "US"):
            market_filter = raw
        else:
            await update.message.reply_text(
                "用法: /daily [CN|US]\n"
                "  /daily     — 全市场日报\n"
                "  /daily CN  — 仅 A 股\n"
                "  /daily US  — 仅美股"
            )
            return

    # 发送等待提示
    msg = await update.message.reply_text("⏳ 生成日报中...")

    try:
        regime = await _fetch_latest_regime(market_filter)
        j_date, judgments = await _fetch_judgments(market_filter)

        text = _build_digest(j_date, market_filter, regime, judgments)
        await msg.edit_text(text, parse_mode="HTML")

        logger.info(
            "cmd_daily_ok",
            market=market_filter,
            report_date=str(j_date),
            judgment_count=len(judgments),
        )

    except Exception as e:
        logger.error("cmd_daily_error", market=market_filter, error=str(e))
        await msg.edit_text(f"⚠️ 生成日报失败: {e}")


# ============================================================
# DailyPusher — 供外部（scheduler / 手动脚本）调用
# ============================================================

class DailyPusher:
    """生成并主动推送日报到 Telegram。

    Usage:
        pusher = DailyPusher()
        success = await pusher.push(market="CN")
        success = await pusher.push(dry_run=True)  # 只返回文本，不实际推送
    """

    async def push(self, market: str | None = None, dry_run: bool = False) -> bool:
        """生成日报并推送到 Telegram。

        Args:
            market: 'CN' | 'US' | None（全部）。
            dry_run: True 时只生成文本、打印日志，不推送。

        Returns:
            True 表示推送成功（或 dry_run 时生成成功）。
        """
        try:
            regime = await _fetch_latest_regime(market)
            j_date, judgments = await _fetch_judgments(market)
            text = _build_digest(j_date, market, regime, judgments)

            if dry_run:
                logger.info(
                    "daily_pusher_dry_run",
                    market=market,
                    report_date=str(j_date),
                    char_count=len(text),
                )
                return True

            from bot.telegram_bot import TelegramPusher
            pusher = TelegramPusher()
            ok = await pusher.send_html(text)
            logger.info(
                "daily_pusher_pushed",
                market=market,
                report_date=str(j_date),
                ok=ok,
            )
            return ok

        except Exception as e:
            logger.error("daily_pusher_error", market=market, error=str(e))
            return False
