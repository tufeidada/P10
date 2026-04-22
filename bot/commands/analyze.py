"""
Telegram /analyze 命令处理器 — 即时多维分析。

Usage:
    /analyze 600519.SH
    /analyze AAPL
"""

from __future__ import annotations

import structlog
from telegram import Update
from telegram.ext import ContextTypes

from core.analysis.composite import CompositeAnalyzer, JudgmentResult
from db.connection import db_query_one

logger = structlog.get_logger(__name__)

# 缓存 analyzer 实例（无状态，可复用）
_analyzer: CompositeAnalyzer | None = None


def _get_analyzer() -> CompositeAnalyzer:
    """获取或创建 CompositeAnalyzer 单例。"""
    global _analyzer
    if _analyzer is None:
        _analyzer = CompositeAnalyzer()
    return _analyzer


def _guess_market(symbol: str) -> str:
    """根据代码格式猜测市场。"""
    if "." in symbol and (symbol.endswith(".SH") or symbol.endswith(".SZ")):
        return "CN"
    return "US"


def _direction_emoji(direction: str) -> str:
    """方向对应的 emoji。"""
    return {"bullish": "🟢", "bearish": "🔴", "neutral": "🟡"}.get(direction, "⚪")


def _regime_emoji(regime_mode: str) -> str:
    """Regime 模式对应的 emoji + 标签。"""
    return {
        "offense": "🟢 进攻",
        "cautious_offense": "🟡 谨慎进攻",
        "defense": "🟠 防守",
        "risk_off": "🔴 避险",
    }.get(regime_mode, regime_mode)


def _confidence_bar(confidence: float, width: int = 10) -> str:
    """将置信度渲染为进度条文本。

    Args:
        confidence: 0.0-1.0 的置信度。
        width: 进度条宽度（字符数）。

    Returns:
        进度条字符串，例如 "████████░░ 80%"。
    """
    filled = int(confidence * width)
    empty = width - filled
    pct = int(confidence * 100)
    return f"{'█' * filled}{'░' * empty} {pct}%"


def _format_result(result: JudgmentResult, stock_info: dict | None = None) -> str:
    """将分析结果格式化为 Telegram HTML 消息。

    Args:
        result: 分析结果。
        stock_info: 股票基本信息（name, industry 等）。

    Returns:
        HTML 格式消息文本。
    """
    name = stock_info.get("name", "") if stock_info else ""

    # Header
    header = f"📊 <b>{result.symbol}</b>"
    if name:
        header += f" {name}"

    separator = "━━━━━━━━━━━━━━━"

    # Regime
    regime_mode = result.regime_snapshot.get("regime_mode", "N/A")
    regime_label = {
        "offense": "进攻模式 🟢",
        "cautious_offense": "谨慎进攻 🟡",
        "defense": "防守模式 🟠",
        "risk_off": "避险模式 🔴",
    }.get(regime_mode, regime_mode)
    regime_line = f"🌡 Regime: <b>{regime_label}</b>"

    # 技术面详情
    tech_detail = result.signal_sources.get("technical", {})
    trend = tech_detail.get("trend", "")
    stage = tech_detail.get("stage", "")
    rs_rank = tech_detail.get("rs_rank")
    pattern = tech_detail.get("pattern", "")
    key_levels = tech_detail.get("key_levels", {})

    tech_header = f"📈 技术面 <b>{result.technical_score:.0f}</b>/100"

    # 趋势映射
    trend_cn = {"up": "上升趋势", "down": "下降趋势", "sideways": "横盘整理"}.get(
        trend, trend if trend else "N/A"
    )
    stage_str = f"Stage {stage}" if stage else "N/A"

    tech_lines = [
        f"├ 日线: {trend_cn} | {stage_str}",
        f"├ 周线: {trend_cn} | {stage_str}",
    ]
    if rs_rank is not None:
        tech_lines.append(f"├ RS Rank: {rs_rank}/100")
    tech_lines.append(f"├ 动量: {pattern if pattern else '无特殊形态'}")

    # 关键位
    supports = key_levels.get("support", [])
    resistances = key_levels.get("resistance", [])
    level_parts = []
    if supports:
        level_parts.append(f"支撑 {supports[0]:.0f}")
    if resistances:
        level_parts.append(f"阻力 {resistances[0]:.0f}")
    if level_parts:
        tech_lines.append(f"└ 关键位: {' | '.join(level_parts)}")
    else:
        tech_lines.append("└ 关键位: N/A")

    # 基本面详情 (Phase 2)
    fund_detail = result.signal_sources.get("fundamental", {})
    fund_lines: list[str] = []
    if result.fundamental_score is not None and fund_detail:
        fund_header = f"📋 基本面 <b>{result.fundamental_score:.0f}</b>/100"
        fund_lines.append(fund_header)

        # 盈利质量
        prof = fund_detail.get("profitability", {})
        prof_score = prof.get("score", 0)
        roe = prof.get("roe_ttm")
        roe_str = f"ROE {roe:.1f}%" if roe is not None else ""
        roe_trend_map = {
            "improving": "趋势向好",
            "declining": "趋势下行",
            "stable": "稳定",
        }
        roe_trend = roe_trend_map.get(prof.get("roe_trend", ""), "")
        fund_lines.append(f"├ 盈利质量: {prof_score:.0f} ({roe_str} {roe_trend})".rstrip())

        # 成长性
        grw = fund_detail.get("growth", {})
        grw_score = grw.get("score", 0)
        rev_yoy = grw.get("revenue_yoy")
        rev_str = f"营收增速 {rev_yoy:.1f}%" if rev_yoy is not None else ""
        grw_trend_map = {
            "accelerating": "加速",
            "decelerating": "减速",
            "mixed": "波动",
        }
        grw_trend = grw_trend_map.get(grw.get("trend", ""), "")
        fund_lines.append(f"├ 成长性: {grw_score:.0f} ({rev_str} {grw_trend})".rstrip())

        # 估值
        val = fund_detail.get("valuation", {})
        val_score = val.get("score", 0)
        pe = val.get("pe_ttm")
        pe_pctile = val.get("pe_hist_pctile")
        val_desc = ""
        if pe is not None:
            val_desc = f"PE {pe:.1f}x"
            if pe_pctile is not None:
                val_desc += f" 历史{pe_pctile:.0f}分位"
            if val_score < 35:
                val_desc += " 偏低"
            elif val_score > 65:
                val_desc += " 偏贵"
        fund_lines.append(f"├ 估值: {val_score:.0f} ({val_desc})")

        # 财务健康
        hlt = fund_detail.get("health", {})
        hlt_score = hlt.get("score", 0)
        debt = hlt.get("debt_ratio")
        debt_str = f"负债率 {debt:.0f}%" if debt is not None else ""
        fund_lines.append(f"└ 财务健康: {hlt_score:.0f} ({debt_str})")

    # 情绪面详情 (Phase 4)
    sent_detail = result.signal_sources.get("sentiment", {})
    sent_lines: list[str] = []
    if result.sentiment_score is not None:
        sent_header = f"😊 情绪面 <b>{result.sentiment_score:.0f}</b>/100"
        sent_lines.append(sent_header)

        heat = sent_detail.get("social_heat", {})
        heat_score = heat.get("score", 50)
        msg_delta = heat.get("message_delta")
        delta_str = (
            f"讨论量{'↑' if (msg_delta or 0) > 0 else '↓'} {abs(msg_delta or 0):.0f}%"
            if msg_delta is not None
            else ""
        )
        sent_lines.append(f"├ 社交热度: {heat_score:.0f} ({delta_str})")

        direction_d = sent_detail.get("social_direction", {})
        dir_score = direction_d.get("score", 50)
        bull_pct = direction_d.get("bullish_pct")
        bull_str = f"看多 {bull_pct:.0f}%" if bull_pct is not None else "暂无数据"
        sent_lines.append(f"├ 看多比例: {bull_str}")

        mood = sent_detail.get("market_mood", {})
        mood_score = mood.get("score", 50)
        mood_label = "恐慌" if mood_score < 30 else ("乐观" if mood_score > 65 else "中性")
        sent_lines.append(f"└ 市场情绪: {mood_score:.0f} ({mood_label})")

    # 资金面详情 (Phase 2)
    flow_detail = result.signal_sources.get("flow", {})
    flow_lines: list[str] = []
    if result.flow_score is not None and flow_detail:
        flow_header = f"💰 资金面 <b>{result.flow_score:.0f}</b>/100"
        flow_lines.append(flow_header)

        # 主力资金
        mf = flow_detail.get("main_force", {})
        net_lg_5d = mf.get("net_lg_5d")
        if net_lg_5d is not None:
            # net_lg_5d 单位为万，转换为亿
            net_yi = net_lg_5d / 10000.0
            direction = "净流入" if net_yi >= 0 else "净流出"
            flow_lines.append(f"├ 主力: 5日{direction} {abs(net_yi):+.1f}亿")
        else:
            pos_days = mf.get("positive_days")
            if pos_days is not None:
                flow_lines.append(f"├ 主力: 净流入天数 {pos_days}/5")

        # 北向资金
        nb = flow_detail.get("northbound", {})
        nb_net_5d = nb.get("net_5d")
        nb_trend = nb.get("trend", "")
        if nb_net_5d is not None:
            nb_dir = "净买入" if nb_net_5d > 0 else "净卖出"
            flow_lines.append(f"├ 北向: 5日{nb_dir}")
        elif nb_trend:
            flow_lines.append(f"├ 北向: {nb_trend}")

        # 融资
        margin = flow_detail.get("margin", {})
        margin_chg = margin.get("change_5d_pct")
        if margin_chg is not None:
            arrow = "↑" if margin_chg >= 0 else "↓"
            flow_lines.append(f"└ 融资: 余额{arrow} {abs(margin_chg):.1f}%")
        else:
            # 关闭最后一项的树状符号
            if len(flow_lines) > 1:
                last = flow_lines[-1]
                flow_lines[-1] = "└" + last[1:]

    elif result.flow_score is not None:
        flow_lines.append(f"💰 资金面 <b>{result.flow_score:.0f}</b>/100")

    # 其余维度（情绪面已单独展示，此处只保留资金面简要行备用）
    dim_lines: list[str] = []
    # flow_score only (sentiment now has its own block above)
    if result.flow_score is not None and not flow_lines:
        dim_lines.append(f"💰 资金面: <b>{result.flow_score:.0f}</b>/100")
    # sentiment removed from dim_lines since it has its own block

    # 短期判断
    dir_label = {"bullish": "看多", "bearish": "看空", "neutral": "中性"}.get(
        result.direction, result.direction
    )
    dir_emoji = _direction_emoji(result.direction)
    confidence_pct = int(result.confidence * 100)
    judgment_line = f"🎯 短期判断: <b>{dir_label}</b> (置信度 {confidence_pct}%)"

    # 逻辑/LLM叙事
    logic_line = ""
    if result.logic_text:
        # LLM 叙事可能较长，Telegram 消息截断到 300 字符
        narrative = result.logic_text
        if len(narrative) > 300:
            narrative = narrative[:297] + "..."
        logic_line = f"📝 {narrative}"
    elif result.suggested_action:
        logic_line = f"📝 逻辑: {result.suggested_action}"

    # 建议 — 从 entry_zone 构建
    advice_line = ""
    if result.entry_zone:
        advice_line = f"💡 建议: 回调至 {result.entry_zone[0]:.0f} 附近可买入"

    # 止损/目标
    sl_tp_parts: list[str] = []
    if result.stop_loss is not None:
        sl_tp_parts.append(f"⛔ 止损: {result.stop_loss:.0f}")
    if result.target_price is not None:
        sl_tp_parts.append(f"🎯 目标: {result.target_price:.0f}")
    sl_tp_line = " | ".join(sl_tp_parts) if sl_tp_parts else ""

    # 组装
    parts = [header, separator, regime_line, tech_header]
    parts.extend(tech_lines)
    if fund_lines:
        parts.extend(fund_lines)
    if flow_lines:
        parts.extend(flow_lines)
    if sent_lines:
        parts.extend(sent_lines)
    if dim_lines:
        parts.extend(dim_lines)
    parts.append(judgment_line)
    if logic_line:
        parts.append(logic_line)
    if advice_line:
        parts.append(advice_line)
    if sl_tp_line:
        parts.append(sl_tp_line)

    parts.append(f"\n🕐 {result.judgment_date}")

    return "\n".join(parts)


async def cmd_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/analyze <symbol> — 运行多维分析。

    解析命令参数中的证券代码，执行综合分析流程，
    将结果格式化为 HTML 消息并回复。
    """
    args = context.args or []

    if not args:
        await update.message.reply_text(
            "用法: /analyze <代码>\n"
            "例如: /analyze 600519.SH\n"
            "例如: /analyze AAPL"
        )
        return

    symbol = args[0].upper()
    market = _guess_market(symbol)

    # 发送"正在分析"提示
    msg = await update.message.reply_text(f"⏳ 正在分析 {symbol}...")

    try:
        # 获取股票基本信息
        stock_info: dict | None = None
        try:
            row = await db_query_one(
                "SELECT name, industry FROM stock_universe WHERE symbol = $1",
                symbol,
            )
            if row:
                stock_info = dict(row)
        except Exception:
            pass  # 查不到不影响分析

        # 执行分析
        analyzer = _get_analyzer()
        result = await analyzer.analyze(symbol, market)

        # 保存判断到数据库
        try:
            judgment_id = await analyzer.save_judgment(result)
            logger.info("analyze_cmd_saved", symbol=symbol, judgment_id=judgment_id)
        except Exception as e:
            logger.warning("analyze_cmd_save_failed", symbol=symbol, error=str(e))

        # 格式化并回复
        text = _format_result(result, stock_info)
        await msg.edit_text(text, parse_mode="HTML")

    except Exception as e:
        logger.error("cmd_analyze_error", symbol=symbol, error=str(e))
        await msg.edit_text(f"⚠️ 分析 {symbol} 失败: {e}")
