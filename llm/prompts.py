"""
LLM Prompt 模板 — 多维分析叙事生成。

提供结构化 prompt 构建函数，将量化数据转化为
OpenAI-compatible messages 格式供 LLMClient 使用。
"""

from __future__ import annotations

from typing import Any

ANALYSIS_SYSTEM_PROMPT = """你是一位经验丰富的投资分析师。你的输出必须是严格合法的 JSON，不包含任何 markdown 代码块或额外文字。"""

ANALYSIS_PROMPT = """请基于以下多维度数据，为 {symbol} 生成投资分析。

## 当前市场环境 (Regime)
模式: {regime_mode} ({regime_label})
趋势: {trend_score}/100 | 波动率: {volatility_score}/100
宽度: {breadth_score}/100 | 流动性: {liquidity_score}/100

## 各维度评分
- 技术面: {tech_score}/100 — {tech_summary}
- 基本面: {fund_score}/100 — {fund_summary}
- 资金面: {flow_score}/100 — {flow_summary}
- 情绪面: {sent_score}/100 — {sent_summary}

## 关键数据
{key_data}

## 相关历史经验（来自 Wiki）
{wiki_context}

## 输出格式
严格返回以下 JSON，不要任何额外文字或 markdown：
{{
  "direction": "bullish" | "bearish" | "neutral" | "unknown",
  "signal_strength": "strong_buy" | "buy" | "hold" | "sell" | "strong_sell" | "unknown",
  "reasoning": "1-2句话核心理由，中文",
  "risks": "1-2句话主要风险，中文",
  "extra_advice": "1-2句话额外建议，中文",
  "narrative": "200-300字综合叙事，中文，识别各维度一致性与矛盾，说明如果判断错误最可能的原因"
}}

## 约束
- direction 和 signal_strength 必须严格使用上述枚举值之一
- extra_advice 不得包含任何具体价格数字（不得写止损位、目标价、买入区间等）
- narrative 不得包含任何具体价格数字
- 语言简洁直接"""

# Regime 模式中文标签
_REGIME_LABELS: dict[str, str] = {
    "offense": "进攻模式",
    "cautious_offense": "谨慎进攻",
    "defense": "防守模式",
    "risk_off": "避险模式",
}


def _summarize_tech(score: float, detail: dict[str, Any]) -> str:
    """从技术面详情构建简短摘要。

    Args:
        score: 技术面评分 0-100。
        detail: 技术分析详情字典。

    Returns:
        一行中文摘要。
    """
    parts: list[str] = []

    trend = detail.get("trend", "")
    trend_cn = {"up": "上升趋势", "down": "下降趋势", "sideways": "横盘"}.get(
        trend, trend
    )
    if trend_cn:
        parts.append(trend_cn)

    stage = detail.get("stage")
    if stage:
        parts.append(f"Stage {stage}")

    rs_rank = detail.get("rs_rank")
    if rs_rank is not None:
        parts.append(f"RS={rs_rank}")

    pattern = detail.get("pattern", "")
    if pattern:
        parts.append(pattern)

    return "，".join(parts) if parts else "无详细数据"


def _summarize_fundamental(score: float | None, detail: dict[str, Any]) -> str:
    """从基本面详情构建简短摘要。

    Args:
        score: 基本面评分，可能为 None。
        detail: 基本面详情字典。

    Returns:
        一行中文摘要。
    """
    if score is None or not detail:
        return "暂无数据"

    parts: list[str] = []

    prof = detail.get("profitability", {})
    roe = prof.get("roe_ttm")
    if roe is not None:
        parts.append(f"ROE {roe:.1f}%")

    grw = detail.get("growth", {})
    rev_yoy = grw.get("revenue_yoy")
    if rev_yoy is not None:
        parts.append(f"营收增速 {rev_yoy:.1f}%")

    val = detail.get("valuation", {})
    pe = val.get("pe_ttm")
    if pe is not None:
        parts.append(f"PE {pe:.1f}x")

    framework = detail.get("framework", "")
    if framework:
        parts.append(f"框架:{framework}")

    return "，".join(parts) if parts else "暂无数据"


def _summarize_flow(score: float | None, detail: dict[str, Any]) -> str:
    """从资金面详情构建简短摘要。

    Args:
        score: 资金面评分，可能为 None。
        detail: 资金面详情字典。

    Returns:
        一行中文摘要。
    """
    if score is None or not detail:
        return "暂无数据"

    parts: list[str] = []

    mf = detail.get("main_force", {})
    net_5d = mf.get("net_lg_5d")
    if net_5d is not None:
        direction = "净流入" if net_5d > 0 else "净流出"
        parts.append(f"主力5日{direction} {abs(net_5d)/10000:.1f}亿")

    nb = detail.get("northbound", {})
    nb_trend = nb.get("trend", "")
    if nb_trend:
        parts.append(f"北向{nb_trend}")

    margin = detail.get("margin", {})
    margin_chg = margin.get("change_5d_pct")
    if margin_chg is not None:
        arrow = "↑" if margin_chg > 0 else "↓"
        parts.append(f"融资余额{arrow}{abs(margin_chg):.1f}%")

    return "，".join(parts) if parts else "暂无数据"


def _summarize_sentiment(score: float | None, detail: dict[str, Any]) -> str:
    """从情绪面详情构建简短摘要。

    Args:
        score: 情绪面综合评分，可能为 None。
        detail: 情绪面详情字典（含 social_heat, social_direction, market_mood）。

    Returns:
        一行中文摘要。
    """
    if score is None or not detail:
        return "暂无数据"

    parts: list[str] = []

    heat = detail.get("social_heat", {})
    heat_score = heat.get("score")
    msg_delta = heat.get("message_delta")
    if heat_score is not None and heat_score != 50.0:
        if msg_delta is not None:
            direction = "↑" if msg_delta > 0 else "↓"
            parts.append(f"社交热度{direction}{abs(msg_delta):.0f}%")
        else:
            parts.append(f"社交热度{heat_score:.0f}")

    direction_d = detail.get("social_direction", {})
    bull_pct = direction_d.get("bullish_pct")
    if bull_pct is not None:
        parts.append(f"看多{bull_pct:.0f}%")

    mood = detail.get("market_mood", {})
    mood_score = mood.get("score", 50)
    mood_src = mood.get("source", "")
    mood_val = mood.get("value")
    if mood_src == "vix" and mood_val is not None:
        parts.append(f"VIX={mood_val:.1f}")
    elif mood_src == "fear_greed" and mood_val is not None:
        mood_label = "恐慌" if mood_score < 30 else ("乐观" if mood_score > 65 else "中性")
        parts.append(f"FG指数{mood_label}")

    return "，".join(parts) if parts else "中性"


def _build_key_data(
    tech_detail: dict[str, Any],
    fund_detail: dict[str, Any],
    flow_detail: dict[str, Any],
) -> str:
    """构建关键数据段落。

    Args:
        tech_detail: 技术面详情。
        fund_detail: 基本面详情。
        flow_detail: 资金面详情。

    Returns:
        格式化的关键数据文本。
    """
    lines: list[str] = []

    # 关键技术位
    kl = tech_detail.get("key_levels", {})
    supports = kl.get("support", [])
    resistances = kl.get("resistance", [])
    if supports:
        lines.append(f"- 支撑位: {', '.join(f'{s:.2f}' for s in supports[:3])}")
    if resistances:
        lines.append(f"- 阻力位: {', '.join(f'{r:.2f}' for r in resistances[:3])}")

    # 估值分位
    val = fund_detail.get("valuation", {})
    pe_pctile = val.get("pe_hist_pctile")
    if pe_pctile is not None:
        lines.append(f"- PE历史分位: {pe_pctile:.0f}%")

    # 资金面关键指标
    mf = flow_detail.get("main_force", {})
    pos_days = mf.get("positive_days")
    if pos_days is not None:
        lines.append(f"- 主力净流入天数(5日): {pos_days}/5")

    return "\n".join(lines) if lines else "无额外关键数据"


def build_analysis_prompt(
    symbol: str,
    regime: dict[str, Any],
    tech_score: float,
    tech_detail: dict[str, Any],
    fund_score: float | None,
    fund_detail: dict[str, Any],
    flow_score: float | None,
    flow_detail: dict[str, Any],
    wiki_context: list[dict[str, Any]] | None = None,
    sent_score: float | None = None,
    sent_detail: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Construct the analysis prompt messages.

    Extracts key metrics from each detail dict to build human-readable summaries,
    then formats them into the ANALYSIS_PROMPT template.

    Args:
        symbol: 证券代码。
        regime: Regime 信息字典（含 regime_mode, trend_score 等）。
        tech_score: 技术面评分。
        tech_detail: 技术面详情。
        fund_score: 基本面评分（可能为 None）。
        fund_detail: 基本面详情。
        flow_score: 资金面评分（可能为 None）。
        flow_detail: 资金面详情。
        wiki_context: Wiki RAG 检索结果列表，每项含 content_text 等字段。
        sent_score: 情绪面评分（可能为 None）。
        sent_detail: 情绪面详情字典（可能为 None）。

    Returns:
        OpenAI-format messages list。
    """
    regime_mode = regime.get("regime_mode", "cautious_offense")

    if wiki_context:
        lines: list[str] = []
        for exp in wiki_context[:3]:
            lines.append(f"- {exp.get('content_text', '')[:200]}")
        wiki_context_text = "\n".join(lines)
    else:
        wiki_context_text = "暂无相关历史经验"

    prompt_text = ANALYSIS_PROMPT.format(
        symbol=symbol,
        regime_mode=regime_mode,
        regime_label=_REGIME_LABELS.get(regime_mode, regime_mode),
        trend_score=regime.get("trend_score", "N/A"),
        volatility_score=regime.get("volatility_score", "N/A"),
        breadth_score=regime.get("breadth_score", "N/A"),
        liquidity_score=regime.get("liquidity_score", "N/A"),
        tech_score=f"{tech_score:.0f}",
        tech_summary=_summarize_tech(tech_score, tech_detail),
        fund_score=f"{fund_score:.0f}" if fund_score is not None else "N/A",
        fund_summary=_summarize_fundamental(fund_score, fund_detail),
        flow_score=f"{flow_score:.0f}" if flow_score is not None else "N/A",
        flow_summary=_summarize_flow(flow_score, flow_detail),
        sent_score=f"{sent_score:.0f}" if sent_score is not None else "N/A",
        sent_summary=_summarize_sentiment(sent_score, sent_detail or {}),
        key_data=_build_key_data(tech_detail, fund_detail, flow_detail),
        wiki_context=wiki_context_text,
    )

    return [
        {"role": "system", "content": ANALYSIS_SYSTEM_PROMPT},
        {"role": "user", "content": prompt_text},
    ]
