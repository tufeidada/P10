"""
backtest/analysis/composite.py — 综合分析入口

整合 Regime（总开关）+ Technical / Fundamental / Flow / Sentiment 四维度，
生成最终 Judgment（方向 / 置信度 / 交易建议）。

⚠️ 独立实现：不 import core/。数据访问通过 asyncpg.Pool + PITDataLoader。

已知问题处理 (known_issues.md → Composite 层):
  TECH-01: daily Stage=2 + RS>80 + weekly Stage=2 + MACD 得 0 分
           → technical_score_raw += 7（补偿强势股回调误判）
  TECH-02: daily Stage=4 + weekly Stage=1 + price_vs_ma30w < -5% + ret_20d < -5%
           → 强制 direction = bearish（周线 Stage 滞后保护）
  FLOW-02: flow.data_complete=False → Flow 权重 × 0.6，释放的 40% 按 60/40 加给 Tech/Fund
  SENT-01: Sentiment 权重已由 regime_params.yaml 控制（≤ 15%），无需额外处理
  新增: 估值泡沫保护：fundamental.valuation_score < 30 + composite direction = bullish
           → 降级 neutral + confidence × 0.70

美股权重调整:
  Flow 权重减半（无北向 / 无融资），多余部分 60% → Technical，40% → Fundamental
  Sentiment 权重保持（VIX 本身是强信号）
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional

import asyncpg

from backtest.pit_loader import PITDataLoader
from backtest.analysis.regime import detect_regime, RegimeResult
from backtest.analysis.technical import analyze_technical, TechnicalResult
from backtest.analysis.fundamental import analyze_fundamental, FundamentalResult
from backtest.analysis.flow import analyze_flow, FlowAnalysis
from backtest.analysis.sentiment import analyze_market_sentiment

log = logging.getLogger(__name__)

_DIR_BULLISH_THR = 65.0
_DIR_BEARISH_THR = 40.0


# ═════════════════════════════════════════════════════════════════════════════
# 数据结构
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class Judgment:
    symbol:              str
    market:              str
    judgment_date:       date
    regime_mode:         str
    regime_snapshot:     dict
    technical_score:     Optional[float]
    fundamental_score:   Optional[float]
    flow_score:          Optional[float]
    sentiment_score:     Optional[float]
    dimension_weights:   dict[str, float]   # 经过市场/数据完整度调整后的权重
    composite_score:     Optional[float]
    direction:           str                # bullish / neutral / bearish
    confidence:          float              # 0-1
    suggested_action:    str               # buy / watch / hold / reduce / avoid
    entry_price:         Optional[float]   # 最新收盘价作为参考入场价
    entry_zone_low:      Optional[float]   # 入场区间下沿 (-0.5%)
    entry_zone_high:     Optional[float]   # 入场区间上沿 (+1.0%)
    stop_loss:           Optional[float]   # ATR 止损位
    target_price:        Optional[float]   # ATR 目标价
    suggested_size_pct:  float             # 建议仓位比例
    adjustments_applied: list[str]         # 触发的增值逻辑标签
    signal_sources:      list[dict]        # 关键信号来源（JSONB 存储）
    detail:              dict = field(default_factory=dict)

    def __str__(self) -> str:
        score_s = f"{self.composite_score:.1f}" if self.composite_score is not None else "N/A"
        return (
            f"Judgment({self.symbol} {self.judgment_date} | {self.regime_mode} | "
            f"{self.direction} score={score_s} conf={self.confidence:.2f} "
            f"action={self.suggested_action})"
        )


# ═════════════════════════════════════════════════════════════════════════════
# 权重计算
# ═════════════════════════════════════════════════════════════════════════════

def _adjust_weights_us(weights: dict[str, float]) -> dict[str, float]:
    """美股: Flow 权重减半，多余部分 60→Tech / 40→Fund。"""
    w = dict(weights)
    excess = w["flow"] * 0.5
    w["flow"]        -= excess
    w["technical"]   += excess * 0.60
    w["fundamental"] += excess * 0.40
    return w


def _adjust_weights_flow_incomplete(weights: dict[str, float]) -> dict[str, float]:
    """flow.data_complete=False: Flow 权重 × 0.6，释放的 40% 分给 Tech/Fund。"""
    w = dict(weights)
    released = w["flow"] * 0.40
    w["flow"]        -= released
    w["technical"]   += released * 0.60
    w["fundamental"] += released * 0.40
    return w


def _weighted_score(
    scores: dict[str, Optional[float]],
    weights: dict[str, float],
) -> Optional[float]:
    """按权重聚合，跳过 None 维度（按剩余权重重新归一化）。"""
    pool = {k: (v, weights[k]) for k, v in scores.items() if v is not None}
    if not pool:
        return None
    total_w = sum(w for _, w in pool.values())
    return round(sum(v * w for v, w in pool.values()) / total_w, 2)


# ═════════════════════════════════════════════════════════════════════════════
# 增值逻辑检测（Known Issues → Composite 层）
# ═════════════════════════════════════════════════════════════════════════════

def _check_tech01(tech: TechnicalResult) -> bool:
    """
    TECH-01: 强势股回调识别。
    Stage=2 + RS>80% + weekly Stage=2 + daily MACD 得 0 分（负值且扩张/平）
    → 短期 MACD 信号不应主导强势股判断，给 technical_score 加 7 分补偿。
    """
    if tech is None or tech.weekly is None:
        return False
    daily = tech.daily
    rs_rank = daily.detail.get("rs_rank")
    macd_score = daily.detail.get("macd_score", 99.0)
    return (
        daily.stage == 2
        and rs_rank is not None and rs_rank > 0.80
        and tech.weekly.stage == 2
        and macd_score == 0.0
    )


def _check_tech02(tech: TechnicalResult) -> bool:
    """
    TECH-02: 下跌转折期周线 Stage 滞后保护。
    daily Stage=4 + weekly Stage=1 + price_vs_ma30w < -5% + ret_20d < -5%
    → 强制 direction = bearish，不等周线转头。
    """
    if tech is None or tech.weekly is None:
        return False
    daily = tech.daily
    weekly = tech.weekly
    price_vs_ma30w = weekly.detail.get("price_vs_ma30")
    ret_20d = daily.detail.get("ret_20d")
    return (
        daily.stage == 4
        and weekly.stage == 1
        and price_vs_ma30w is not None and price_vs_ma30w < -0.05
        and ret_20d is not None and ret_20d < -0.05
    )


def _check_valuation_bubble(fund: FundamentalResult, direction: str) -> bool:
    """
    估值泡沫保护：好公司 + 极度高估 + 技术强 常常是阶段性顶部。
    fundamental.valuation_score < 30 + composite direction = bullish
    → 降级 neutral，confidence × 0.70
    """
    if fund is None:
        return False
    return fund.valuation_score < 30 and direction == "bullish"


# ═════════════════════════════════════════════════════════════════════════════
# 方向 / 置信度
# ═════════════════════════════════════════════════════════════════════════════

def _score_to_direction(score: float) -> str:
    if score >= _DIR_BULLISH_THR:
        return "bullish"
    if score <= _DIR_BEARISH_THR:
        return "bearish"
    return "neutral"


def _compute_confidence(
    score: float,
    direction: str,
    regime_params: dict,
    data_complete: bool = True,
) -> float:
    if direction == "neutral":
        base = 0.20
    elif direction == "bullish":
        base = (score - 50.0) / 50.0
    else:
        base = (50.0 - score) / 50.0
    conf = base * regime_params.get("confidence_factor", 1.0)
    if not data_complete:
        conf *= 0.85
    return round(min(max(conf, 0.10), 0.90), 4)


def _suggest_action(direction: str, confidence: float, min_conf: float) -> str:
    if direction == "bullish":
        return "buy" if confidence >= min_conf else "watch"
    if direction == "bearish":
        return "sell" if confidence >= min_conf else "reduce"
    return "hold"


# ═════════════════════════════════════════════════════════════════════════════
# 价格 / ATR（用于交易建议）
# ═════════════════════════════════════════════════════════════════════════════

async def _get_price_and_atr(
    loader: PITDataLoader, symbol: str
) -> tuple[Optional[float], Optional[float]]:
    bars = await loader.get_bars(symbol, lookback_days=5)
    if bars.empty or "close" not in bars.columns:
        return None, None
    price = float(bars["close"].iloc[-1])

    feats = await loader.get_features(symbol, lookback_days=3)
    atr = None
    if not feats.empty and "atr_14" in feats.columns:
        v = feats["atr_14"].dropna()
        if not v.empty:
            atr = float(v.iloc[-1])
    return price, atr


def _trade_suggestion(
    direction: str,
    confidence: float,
    price: Optional[float],
    atr: Optional[float],
    regime_params: dict,
) -> tuple[Optional[float], Optional[float], Optional[float], Optional[float], float]:
    """
    返回 (entry_zone_low, entry_zone_high, stop_loss, target_price, suggested_size_pct)。
    """
    max_pos = regime_params.get("max_position_pct", 0.10)
    size = round(min(confidence * max_pos, max_pos), 4) if direction != "bearish" else 0.0

    if price is None:
        return None, None, None, None, size

    zone_low  = round(price * 0.995, 4)
    zone_high = round(price * 1.010, 4)

    if direction != "bullish":
        return zone_low, zone_high, None, None, size

    sl_mult  = regime_params.get("stop_loss_atr_mult", 2.0)
    tgt_mult = regime_params.get("target_atr_mult", 4.0)

    if atr and atr > 0:
        stop_loss    = round(price - atr * sl_mult, 4)
        target_price = round(price + atr * tgt_mult, 4)
    else:
        stop_loss    = round(price * 0.93, 4)   # 7% fixed fallback
        target_price = round(price * 1.15, 4)   # 15% fixed fallback

    return zone_low, zone_high, stop_loss, target_price, size


# ═════════════════════════════════════════════════════════════════════════════
# 信号溯源（signal_sources JSONB）
# ═════════════════════════════════════════════════════════════════════════════

def _build_signal_sources(
    tech: Optional[TechnicalResult],
    fund: Optional[FundamentalResult],
    flow: Optional[FlowAnalysis],
    regime: RegimeResult,
    adjustments: list[str],
) -> list[dict]:
    sources: list[dict] = []

    sources.append({
        "source": "regime.mode",
        "value": regime.regime_mode,
        "weight": 0,
    })

    if tech:
        sources.append({
            "source": "technical.combined_direction",
            "value": tech.combined_direction,
            "weight": round(tech.combined_score / 100, 3),
        })
        sources.append({
            "source": "technical.stage_daily",
            "value": tech.daily.stage,
            "weight": round(tech.daily.detail.get("stage_score", 0) / 100, 3),
        })
        rs = tech.daily.detail.get("rs_rank")
        if rs is not None:
            sources.append({
                "source": "technical.rs_rank",
                "value": round(float(rs), 3),
                "weight": round(tech.daily.detail.get("rs_rank_score", 0) / 100, 3),
            })

    if fund:
        sources.append({
            "source": "fundamental.valuation_score",
            "value": round(fund.valuation_score, 1),
            "weight": 0.05,
        })

    if flow:
        sources.append({
            "source": "flow.main_flow_score",
            "value": round(flow.main_flow_score, 1),
            "weight": round(flow.score / 100, 3),
        })

    for adj in adjustments:
        sources.append({
            "source": f"adjustment.{adj.lower()}",
            "value": adj,
            "weight": 0,
        })

    return sources


# ═════════════════════════════════════════════════════════════════════════════
# 主入口
# ═════════════════════════════════════════════════════════════════════════════

async def generate_judgment(
    pool: asyncpg.Pool,
    loader: PITDataLoader,
    symbol: str,
    market: str,
) -> Optional[Judgment]:
    """
    生成个股综合判断。

    调用方须在调用前执行 loader.set_date(cutoff)，本函数从 loader 读取当前日期。

    Args:
        pool:   asyncpg 连接池（Technical / Fundamental / Regime 使用）。
        loader: PITDataLoader（Flow / Sentiment / 价格获取使用）。
        symbol: 股票代码，如 "000063.SZ" / "NVDA"。
        market: "CN" 或 "US"。

    Returns:
        Judgment，或 None（核心数据完全缺失）。
    """
    cutoff = loader._assert_date()

    # ── 1. Regime ─────────────────────────────────────────────────────────────
    regime = await detect_regime(pool, market, cutoff)

    # ── 2. 四维度并行计算 ──────────────────────────────────────────────────────
    import asyncio
    tech_task  = asyncio.create_task(analyze_technical(pool, symbol, market, cutoff))
    fund_task  = asyncio.create_task(analyze_fundamental(pool, symbol, market, cutoff))
    flow_task  = asyncio.create_task(analyze_flow(loader, symbol, market))
    sent_task  = asyncio.create_task(analyze_market_sentiment(loader, market))

    tech: Optional[TechnicalResult]   = await tech_task
    fund: Optional[FundamentalResult] = await fund_task
    flow: Optional[FlowAnalysis]      = await flow_task
    sent_score, _sent_det             = await sent_task

    if tech is None:
        log.warning(f"composite: no technical data for {symbol} @ {cutoff}, skipping")
        return None

    # ── 3. 原始分数 ────────────────────────────────────────────────────────────
    tech_score_raw  = tech.combined_score
    fund_score_raw  = fund.fundamental_score if fund else None
    flow_score_raw  = flow.score             if flow else None
    sent_score_raw  = sent_score

    # ── 4. 权重调整 ────────────────────────────────────────────────────────────
    weights = dict(regime.dimension_weights)  # offense: tech=0.40 fund=0.25 flow=0.20 sent=0.15

    if market == "US":
        weights = _adjust_weights_us(weights)

    flow_incomplete = (flow is None) or (not flow.data_complete)
    if flow_incomplete:
        weights = _adjust_weights_flow_incomplete(weights)

    # ── 5. TECH-01: 强势股回调补偿 ────────────────────────────────────────────
    adjustments: list[str] = []
    tech_score = tech_score_raw

    if _check_tech01(tech):
        tech_score = min(tech_score_raw + 7.0, 100.0)
        adjustments.append("TECH-01")
        log.info(
            f"composite: TECH-01 triggered for {symbol} @ {cutoff} "
            f"(tech_score {tech_score_raw:.1f} → {tech_score:.1f})"
        )

    # ── 6. 综合评分 ────────────────────────────────────────────────────────────
    scores = {
        "technical":   tech_score,
        "fundamental": fund_score_raw,
        "flow":        flow_score_raw,
        "sentiment":   sent_score_raw,
    }
    composite_score = _weighted_score(scores, weights)

    if composite_score is None:
        log.warning(f"composite: all scores None for {symbol} @ {cutoff}")
        return None

    # ── 7. 方向判断 ────────────────────────────────────────────────────────────
    direction = _score_to_direction(composite_score)

    # TECH-02: 下跌转折期强制 bearish
    if _check_tech02(tech):
        if direction != "bearish":
            log.info(
                f"composite: TECH-02 triggered for {symbol} @ {cutoff} "
                f"(direction {direction} → bearish)"
            )
            direction = "bearish"
        adjustments.append("TECH-02")

    # 估值泡沫保护
    if _check_valuation_bubble(fund, direction):
        direction = "neutral"
        adjustments.append("VALUATION_BUBBLE")
        log.info(f"composite: VALUATION_BUBBLE triggered for {symbol} @ {cutoff}")

    # ── 8. 置信度 ──────────────────────────────────────────────────────────────
    data_complete = (fund is not None) and (not flow_incomplete) and (sent_score is not None)
    confidence = _compute_confidence(
        composite_score, direction, regime.params, data_complete
    )

    # 估值泡沫降档
    if "VALUATION_BUBBLE" in adjustments:
        confidence = round(confidence * 0.70, 4)

    # ── 9. 交易建议 ────────────────────────────────────────────────────────────
    price, atr = await _get_price_and_atr(loader, symbol)
    min_conf = regime.params.get("min_confidence_to_enter", 0.50)
    action = _suggest_action(direction, confidence, min_conf)

    zone_low, zone_high, stop_loss, target_price, size_pct = _trade_suggestion(
        direction, confidence, price, atr, regime.params
    )

    # ── 10. 信号溯源 ───────────────────────────────────────────────────────────
    signal_sources = _build_signal_sources(tech, fund, flow, regime, adjustments)

    # ── 11. 构建 Judgment ──────────────────────────────────────────────────────
    regime_snapshot = {
        "trend_score":      regime.trend_score,
        "volatility_score": regime.volatility_score,
        "breadth_score":    regime.breadth_score,
        "liquidity_score":  regime.liquidity_score,
        "trend_direction":  regime.trend_direction,
        "volatility_env":   regime.volatility_env,
    }

    detail = {
        "tech_score_raw":   tech_score_raw,
        "tech_score_adj":   tech_score,
        "fund_score":       fund_score_raw,
        "flow_score":       flow_score_raw,
        "sent_score":       sent_score_raw,
        "weights_used":     weights,
        "flow_incomplete":  flow_incomplete,
        "data_complete":    data_complete,
        "entry_zone_low":   zone_low,
        "entry_zone_high":  zone_high,
        "atr":              atr,
        "tech_detail":      tech.daily.detail if tech else None,
        "weekly_stage":     tech.weekly.stage if tech and tech.weekly else None,
        "weekly_detail":    tech.weekly.detail if tech and tech.weekly else None,
    }

    j = Judgment(
        symbol=symbol, market=market, judgment_date=cutoff,
        regime_mode=regime.regime_mode,
        regime_snapshot=regime_snapshot,
        technical_score=round(tech_score, 2),
        fundamental_score=round(fund_score_raw, 2) if fund_score_raw is not None else None,
        flow_score=round(flow_score_raw, 2) if flow_score_raw is not None else None,
        sentiment_score=round(sent_score_raw, 2) if sent_score_raw is not None else None,
        dimension_weights={k: round(v, 4) for k, v in weights.items()},
        composite_score=composite_score,
        direction=direction,
        confidence=confidence,
        suggested_action=action,
        entry_price=round(price, 4) if price else None,
        entry_zone_low=zone_low,
        entry_zone_high=zone_high,
        stop_loss=stop_loss,
        target_price=target_price,
        suggested_size_pct=size_pct,
        adjustments_applied=adjustments,
        signal_sources=signal_sources,
        detail=detail,
    )
    log.info(str(j))
    return j


# ═════════════════════════════════════════════════════════════════════════════
# 持久化
# ═════════════════════════════════════════════════════════════════════════════

async def save_judgment(
    pool: asyncpg.Pool,
    j: Judgment,
    run_id: Optional[int] = None,
) -> int:
    """
    写入 backtest_judgments 表。

    Args:
        pool:   asyncpg 连接池。
        j:      Judgment 对象。
        run_id: 关联的回测运行 ID，单点测试传 None。

    Returns:
        新行的 id。
    """
    sql = """
        INSERT INTO backtest_judgments (
            run_id, symbol, market, judgment_date,
            technical_score, fundamental_score, flow_score, sentiment_score,
            composite_score, regime_mode, regime_snapshot, direction, confidence,
            suggested_action, entry_price, stop_loss, target_price,
            suggested_size_pct, signal_sources
        ) VALUES (
            $1, $2, $3, $4,
            $5, $6, $7, $8,
            $9, $10, $11::jsonb, $12, $13,
            $14, $15, $16, $17,
            $18, $19::jsonb
        ) RETURNING id
    """
    async with pool.acquire() as conn:
        row_id = await conn.fetchval(
            sql,
            run_id, j.symbol, j.market, j.judgment_date,
            j.technical_score, j.fundamental_score, j.flow_score, j.sentiment_score,
            j.composite_score, j.regime_mode,
            json.dumps(j.regime_snapshot, default=str),
            j.direction, j.confidence,
            j.suggested_action, j.entry_price, j.stop_loss, j.target_price,
            j.suggested_size_pct,
            json.dumps(j.signal_sources, default=str),
        )
    log.info(f"save_judgment: {j.symbol} @ {j.judgment_date} → id={row_id}")
    return row_id
