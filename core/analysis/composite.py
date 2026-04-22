"""
综合分析引擎 — 多维分析判断与记录。

将技术面、基本面、资金面、情绪面评分加权合成，
生成综合判断并持久化到 judgments 表。

Phase 1: 技术面为主，其余维度使用中性默认值（50 分）。
Phase 2+: 接入基本面/资金面/情绪面模块。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

import structlog
import yaml

from core.invariants import assert_in, assert_range
from db.connection import db_query, db_query_one, db_query_val, db_execute

logger = structlog.get_logger(__name__)

# 中性分数：Phase 1 中未实现的维度使用此值
_NEUTRAL_SCORE: float = 50.0

# 方向判定阈值
_BULLISH_THRESHOLD: float = 65.0
_BEARISH_THRESHOLD: float = 40.0


@dataclass
class JudgmentResult:
    """综合分析结果数据类。"""

    symbol: str
    market: str
    judgment_date: date
    timeframe: str  # 'short' | 'mid'
    technical_score: float
    fundamental_score: float | None
    flow_score: float | None
    sentiment_score: float | None
    composite_score: float
    direction: str  # 'bullish' | 'neutral' | 'bearish'
    confidence: float  # 0.0-1.0
    rule_signal_strength: str  # strong_buy | buy | hold | sell | strong_sell
    logic_text: str | None
    suggested_action: str | None
    entry_zone: tuple[float, float] | None
    stop_loss: float | None
    target_price: float | None
    signal_sources: dict = field(default_factory=dict)
    regime_snapshot: dict = field(default_factory=dict)
    position_sizing: Any | None = None  # core.risk.position_sizer.PositionSizing
    llm_vote_consensus: float | None = None   # 0.33-1.0, 众数票数/总调用次数
    llm_vote_total_calls: int | None = None   # 实际完成的 LLM 调用次数


class CompositeAnalyzer:
    """综合分析器 — 组合多维度评分并生成判断。"""

    def __init__(self, regime_params_path: str = "config/regime_params.yaml") -> None:
        self._regime_params = self._load_regime_params(regime_params_path)

    @staticmethod
    def _load_regime_params(path: str) -> dict[str, Any]:
        """加载 regime 参数配置。

        Args:
            path: YAML 配置文件路径。

        Returns:
            参数字典。

        Raises:
            FileNotFoundError: 配置文件不存在。
            ValueError: 配置文件格式错误。
        """
        with open(path, "r", encoding="utf-8") as f:
            params = yaml.safe_load(f)
        if not params or "regimes" not in params:
            raise ValueError(f"regime_params.yaml 缺少 regimes 字段：{path}")
        return params

    async def _get_latest_regime(self, market: str, trade_date: date) -> dict[str, Any] | None:
        """获取最新 regime 状态。

        Args:
            market: 市场代码 ('CN' | 'US')。
            trade_date: 交易日期。

        Returns:
            Regime 信息字典（含 regime_mode 等字段），无数据时返回 None。

        Raises:
            asyncpg.PostgresError: 数据库查询失败时向上传播。
        """
        row = await db_query_one(
            """
            SELECT regime_mode, trend_score, volatility_score,
                   breadth_score, liquidity_score,
                   signal_threshold_adj, max_position_pct,
                   dimension_weights, detail
            FROM regime_daily
            WHERE market = $1 AND trade_date <= $2
            ORDER BY trade_date DESC
            LIMIT 1
            """,
            market,
            trade_date,
        )
        return dict(row) if row else None

    def _get_weights(self, regime_mode: str) -> dict[str, float]:
        """根据 regime 模式获取各维度权重。

        Args:
            regime_mode: Regime 模式名称，必须属于 VALID_REGIME_MODES。

        Returns:
            权重字典 {technical, fundamental, flow, sentiment}。

        Raises:
            ValueError: regime_mode 不在配置中。
        """
        regimes = self._regime_params.get("regimes", {})
        assert_in(regime_mode, set(regimes.keys()), "composite.regime_consumer")
        return regimes[regime_mode]["weights"]

    async def _get_technical_score(
        self, symbol: str, market: str, trade_date: date,
    ) -> tuple[float, dict[str, Any]]:
        """获取技术面评分。

        尝试从 core.analysis.technical 导入（如可用），否则从 features_daily 表读取。

        Args:
            symbol: 证券代码。
            market: 市场代码。
            trade_date: 交易日期。

        Returns:
            (技术分数 0-100, 技术分析详情字典)。
        """
        detail: dict[str, Any] = {}

        # 使用 TechnicalAnalyzer（从原始行情计算，不依赖 features_daily）
        try:
            from core.analysis.technical import TechnicalAnalyzer
            ta = TechnicalAnalyzer()
            result = await ta.analyze(symbol, trade_date)
            score = float(result.get("score", _NEUTRAL_SCORE))
            detail = {
                "source": "technical_analyzer",
                "trend": result.get("trend"),
                "stage": result.get("stage"),
                "rs_rank": result.get("rs_rank"),
                "pattern": result.get("pattern"),
                "confidence_adj": result.get("confidence_adj"),
                "key_levels": result.get("key_levels"),
            }
            return round(score, 2), detail

        except Exception as e:
            logger.error("technical_score_fallback_error", symbol=symbol, error=str(e))
            return _NEUTRAL_SCORE, {"source": "error", "error": str(e)}

    async def _get_key_levels(
        self, symbol: str, trade_date: date,
        tech_detail: dict[str, Any] | None = None,
    ) -> dict[str, float | None]:
        """获取关键价位（支撑/阻力/止损/目标）。

        优先从技术分析结果中提取，回退到 market_bars_daily 直接查询。

        Args:
            symbol: 证券代码。
            trade_date: 交易日期。
            tech_detail: 技术分析详情（含 key_levels）。

        Returns:
            包含 close, support, resistance, stop_loss, target 的字典。
        """
        default = {"close": None, "support": None, "resistance": None,
                    "stop_loss": None, "target": None}

        # 优先从技术分析结果中提取
        if tech_detail and tech_detail.get("key_levels"):
            kl = tech_detail["key_levels"]
            supports = kl.get("support", [])
            resistances = kl.get("resistance", [])
            support = supports[0] if supports else None
            resistance = resistances[0] if resistances else None
        else:
            support = None
            resistance = None

        try:
            row = await db_query_one(
                """
                SELECT close FROM market_bars_daily
                WHERE symbol = $1 AND trade_date <= $2
                ORDER BY trade_date DESC LIMIT 1
                """,
                symbol, trade_date,
            )
            close = float(row["close"]) if row else None
        except Exception:
            close = None

        stop_loss = None
        target = None
        if close and support:
            stop_loss = round(support * 0.97, 2)
        if close and resistance:
            target = round(resistance * 1.02, 2)

        return {
            "close": close,
            "support": support,
            "resistance": resistance,
            "stop_loss": stop_loss,
            "target": target,
        }

    @staticmethod
    def _determine_direction(composite_score: float) -> str:
        """根据综合得分判定方向。

        Args:
            composite_score: 综合得分 (0-100)。

        Returns:
            方向字符串: 'bullish' | 'neutral' | 'bearish'。
        """
        if composite_score >= _BULLISH_THRESHOLD:
            return "bullish"
        elif composite_score <= _BEARISH_THRESHOLD:
            return "bearish"
        return "neutral"

    def _compute_confidence(
        self,
        composite_score: float,
        regime_mode: str,
        dim_scores: list[float | None] | None = None,
    ) -> float:
        """计算置信度（Phase 1.5 新公式）。

        基于"4维度一致性"(70%)和"分数距离中性区"(30%)，不再乘 regime_factor。

        Args:
            composite_score: 综合得分。
            regime_mode: 当前 regime 模式（保留参数，用于 ValueError 校验）。
            dim_scores: [tech, fund, flow, sent] 四维度分数列表，可含 None。

        Returns:
            置信度 0.0-1.0。

        Raises:
            ValueError: regime_mode 不在配置中。
        """
        # 校验 regime_mode 合法性（保持 M1 原则）
        regimes = self._regime_params.get("regimes", {})
        if regime_mode not in regimes:
            raise ValueError(
                f"Unknown regime_mode: {regime_mode!r}，"
                f"合法值: {sorted(regimes.keys())}"
            )

        # 有效维度分数（过滤 None，用中性分 50 替代缺失维度）
        scores = [s if s is not None else 50.0 for s in (dim_scores or [])]
        # 补齐到 4 个维度
        while len(scores) < 4:
            scores.append(50.0)
        scores = scores[:4]

        # 1. 维度一致性
        if composite_score > 55:
            agree_count = sum(1 for s in scores if s > 55)
        elif composite_score < 45:
            agree_count = sum(1 for s in scores if s < 45)
        else:
            agree_count = 0

        agree_ratio = agree_count / 4.0 if composite_score != 50 else 0.3

        # 2. 分数距离归一化
        distance = abs(composite_score - 50.0) / 50.0  # 0-1

        # 3. 合并：一致性主导，距离辅助
        confidence = agree_ratio * 0.7 + distance * 0.3
        return round(max(0.0, min(1.0, confidence)), 3)

    @staticmethod
    def _compute_rule_signal_strength(direction: str, confidence: float) -> str:
        """根据方向和置信度计算规则信号强度。

        Args:
            direction: 方向判定（bullish/bearish/neutral）。
            confidence: 置信度 0-1。

        Returns:
            信号强度字符串。
        """
        if direction == "bullish":
            if confidence > 0.4:
                return "strong_buy"
            elif confidence > 0.25:
                return "buy"
            else:
                return "hold"
        elif direction == "bearish":
            if confidence > 0.4:
                return "strong_sell"
            elif confidence > 0.25:
                return "sell"
            else:
                return "hold"
        else:
            return "hold"

    @staticmethod
    def _suggest_action(
        direction: str,
        confidence: float,
        close: float | None,
        support: float | None,
        resistance: float | None,
    ) -> str | None:
        """生成建议操作文本。

        Args:
            direction: 方向判定。
            confidence: 置信度。
            close: 当前收盘价。
            support: 支撑位。
            resistance: 阻力位。

        Returns:
            建议操作文本，或 None。
        """
        if confidence < 0.3:
            return "观望 — 信号不够明确，等待确认"

        if direction == "bullish":
            if confidence >= 0.6:
                if close and support:
                    return f"关注回调买入机会，支撑 {support:.2f} 附近"
                return "偏多，关注回调买入机会"
            return "偏多但信号不强，轻仓试探或等待"

        if direction == "bearish":
            if confidence >= 0.6:
                return "回避 — 趋势偏空，控制仓位"
            return "偏空，减仓或观望"

        return "中性震荡，等待方向明确"

    @staticmethod
    def _parse_llm_json(raw: str) -> dict[str, Any] | None:
        """从 LLM 原始响应中解析结构化 JSON。

        Returns:
            解析后的 dict，或 None（解析失败时）。
        """
        _valid_dirs = {"bullish", "bearish", "neutral", "unknown"}
        _valid_strengths = {"strong_buy", "buy", "hold", "sell", "strong_sell", "unknown"}
        try:
            clean = raw.strip()
            if clean.startswith("```"):
                clean = clean.split("```")[1]
                if clean.startswith("json"):
                    clean = clean[4:]
            parsed = json.loads(clean)
            direction = parsed.get("direction", "unknown")
            if direction not in _valid_dirs:
                direction = "unknown"
            signal_strength = parsed.get("signal_strength", "unknown")
            if signal_strength not in _valid_strengths:
                signal_strength = "unknown"
            return {
                "direction": direction,
                "signal_strength": signal_strength,
                "reasoning": parsed.get("reasoning") or None,
                "risks": parsed.get("risks") or None,
                "extra_advice": parsed.get("extra_advice") or None,
                "narrative": parsed.get("narrative") or raw,
            }
        except (json.JSONDecodeError, AttributeError):
            return None

    async def _llm_analyze_with_vote(
        self,
        prompt_msgs: list[dict[str, str]],
        symbol: str,
        market: str,
        n_calls: int = 3,
    ) -> dict[str, Any]:
        """3-call majority vote LLM 分析。

        同一 prompt 调用 LLM n_calls 次，对 direction / signal_strength 取众数，
        选取方向与众数一致的第一次调用的完整文本（narrative / reasoning 等）。

        Returns:
            包含 direction, signal_strength, reasoning, risks, extra_advice, narrative,
            vote_consensus, vote_total_calls 的 dict。
        """
        from collections import Counter
        from llm.client import LLMClient
        llm = LLMClient()

        raw_results: list[dict[str, Any]] = []
        for i in range(n_calls):
            try:
                raw = await llm.chat(
                    prompt_msgs, model="deepseek", max_tokens=900,
                    temperature=0.0,
                    symbol=symbol, market=market,
                )
                parsed = self._parse_llm_json(raw)
                if parsed:
                    raw_results.append(parsed)
            except Exception as e:
                logger.warning("llm_vote_call_failed", call_index=i, symbol=symbol, error=str(e))

        if not raw_results:
            return {
                "direction": "unknown", "signal_strength": "unknown",
                "reasoning": None, "risks": None, "extra_advice": None,
                "narrative": None, "vote_consensus": 0.0,
                "vote_total_calls": 0,
            }

        directions = [r["direction"] for r in raw_results if r.get("direction")]
        strengths = [r["signal_strength"] for r in raw_results if r.get("signal_strength")]

        voted_direction = Counter(directions).most_common(1)[0][0] if directions else "unknown"
        voted_strength = Counter(strengths).most_common(1)[0][0] if strengths else "unknown"
        vote_count = Counter(directions).most_common(1)[0][1] if directions else 0
        consensus = round(vote_count / len(raw_results), 2) if raw_results else 0.0

        logger.debug(
            "llm_vote_result",
            symbol=symbol,
            voted_direction=voted_direction,
            voted_strength=voted_strength,
            consensus=consensus,
            calls_done=len(raw_results),
            calls_requested=n_calls,
        )

        # 取方向与众数一致的第一次调用的文本内容
        for r in raw_results:
            if r.get("direction") == voted_direction:
                return {
                    "direction": voted_direction,
                    "signal_strength": voted_strength,
                    "reasoning": r.get("reasoning"),
                    "risks": r.get("risks"),
                    "extra_advice": r.get("extra_advice"),
                    "narrative": r.get("narrative"),
                    "vote_consensus": consensus,
                    "vote_total_calls": len(raw_results),
                }

        # Fallback（理论上不可达）
        return {
            "direction": voted_direction, "signal_strength": voted_strength,
            "reasoning": None, "risks": None, "extra_advice": None,
            "narrative": None, "vote_consensus": consensus,
            "vote_total_calls": len(raw_results),
        }

    async def analyze(
        self,
        symbol: str,
        market: str = "CN",
        trade_date: date | None = None,
        timeframe: str = "short",
        dry_run: bool = False,
    ) -> JudgmentResult:
        """运行完整多维分析流程。

        1. 获取最新 regime
        2. 计算技术面得分
        3. Phase 1: 基本面/资金面/情绪面使用中性默认值
        4. 加权合成综合分数
        5. 判定方向和置信度
        6. 生成建议和关键价位

        Args:
            symbol: 证券代码。
            market: 市场代码 ('CN' | 'US')。
            trade_date: 分析日期，默认今天。
            timeframe: 时间框架 ('short' | 'mid')。
            dry_run: True 时跳过 LLM 调用，返回占位数据（用于开发/测试）。

        Returns:
            JudgmentResult 分析结果。
        """
        if trade_date is None:
            trade_date = date.today()

        logger.info(
            "composite_analyze_start",
            symbol=symbol,
            market=market,
            trade_date=str(trade_date),
        )

        # 1. 获取 regime（无数据时直接抛出，要求调用方先运行 detect_regime）
        regime = await self._get_latest_regime(market, trade_date)
        if regime is None:
            raise RuntimeError(
                f"无 regime_daily 数据：market={market}, date={trade_date}。"
                "请先运行 detect_regime() 或 scheduler 的 regime 任务。"
            )
        regime_mode = regime["regime_mode"]
        weights = self._get_weights(regime_mode)

        # 2. 技术面评分
        tech_score, tech_detail = await self._get_technical_score(symbol, market, trade_date)

        # 3. Phase 2: 基本面评分
        fundamental_score: float | None = None
        fund_detail: dict = {}
        try:
            from core.analysis.fundamental import FundamentalAnalyzer
            fa = FundamentalAnalyzer()
            fund_result = await fa.analyze(symbol, trade_date)
            fundamental_score = fund_result.composite_score
            fund_detail = fund_result.detail
        except Exception as e:
            logger.warning("fundamental_score_error", symbol=symbol, error=str(e))

        # Phase 2: 资金面评分（CN + US 均支持）
        flow_score: float | None = None
        flow_detail: dict = {}
        try:
            from core.analysis.flow import FlowAnalyzer
            fl = FlowAnalyzer()
            flow_result = await fl.analyze(symbol, trade_date, market=market)
            flow_score = flow_result.composite_score
            flow_detail = flow_result.detail
        except Exception as e:
            logger.warning("flow_score_error", symbol=symbol, market=market, error=str(e))

        # Phase 4: 情绪面评分
        sentiment_score: float | None = None
        sent_detail: dict = {}
        try:
            from core.analysis.sentiment import SentimentAnalyzer
            sa = SentimentAnalyzer()
            sent_result = await sa.analyze(symbol, market, trade_date)
            sentiment_score = sent_result.composite
            sent_detail = sent_result.detail
        except Exception as e:
            logger.warning("sentiment_score_error", symbol=symbol, error=str(e))

        # 4. 计算加权综合分数
        eff_fundamental = fundamental_score if fundamental_score is not None else _NEUTRAL_SCORE
        eff_flow = flow_score if flow_score is not None else _NEUTRAL_SCORE
        eff_sentiment = sentiment_score if sentiment_score is not None else _NEUTRAL_SCORE

        composite = (
            tech_score * weights.get("technical", 0.30)
            + eff_fundamental * weights.get("fundamental", 0.35)
            + eff_flow * weights.get("flow", 0.20)
            + eff_sentiment * weights.get("sentiment", 0.15)
        )
        composite = round(max(0.0, min(100.0, composite)), 2)
        assert_range(composite, 0.0, 100.0, "composite.final_score")

        # 5. 方向与置信度
        direction = self._determine_direction(composite)
        assert_in(direction, {"bullish", "bearish", "neutral"}, "composite.direction")
        confidence = self._compute_confidence(
            composite, regime_mode,
            dim_scores=[tech_score, fundamental_score, flow_score, sentiment_score],
        )
        rule_signal_strength = self._compute_rule_signal_strength(direction, confidence)

        # 6. 关键价位与建议
        levels = await self._get_key_levels(symbol, trade_date, tech_detail)
        close_price = levels["close"]
        support = levels["support"]
        resistance = levels["resistance"]
        stop_loss = levels["stop_loss"]
        target_price = levels["target"]

        entry_zone: tuple[float, float] | None = None
        if close_price and support and direction == "bullish":
            entry_low = round(support, 2)
            entry_high = round(close_price, 2)
            entry_zone = (entry_low, entry_high)

        # 计算仓位建议（需要用户配置账户规模）
        position_sizing = None
        try:
            from core.risk.position_sizer import PositionSizer
            account_value = float(os.environ.get("ACCOUNT_VALUE", "100000"))
            if close_price and stop_loss:
                sizer = PositionSizer(account_value=account_value)
                position_sizing = sizer.calc_position(
                    entry_price=close_price,
                    stop_price=stop_loss,
                    target_price=target_price,
                    max_position_pct=regime.get("max_position_pct", 0.4),
                )
        except Exception:
            pass

        suggested_action = self._suggest_action(
            direction, confidence, close_price, support, resistance,
        )

        # 检索 Wiki 经验库（失败不阻塞）
        wiki_context: list[dict] = []
        try:
            from llm.wiki_manager import WikiManager
            wm = WikiManager()
            query = f"{symbol} {direction} {regime_mode}"
            wiki_context = await wm.search_experience(query, top_k=3)
        except Exception as e:
            logger.debug("wiki_search_error", symbol=symbol, error=str(e))

        # LLM 分析叙事（失败不阻塞；dry_run 跳过）
        logic_text: str | None = None
        llm_direction: str = "unknown"
        llm_signal_strength: str = "unknown"
        llm_reasoning: str | None = None
        llm_risks: str | None = None
        llm_extra_advice: str | None = None
        llm_vote_consensus: float | None = None
        llm_vote_total_calls: int | None = None
        if dry_run:
            logic_text = (
                f"[DRY RUN] tech={tech_score:.0f} "
                f"fund={fundamental_score or _NEUTRAL_SCORE:.0f} "
                f"flow={flow_score or _NEUTRAL_SCORE:.0f} "
                f"sent={sentiment_score or _NEUTRAL_SCORE:.0f} "
                f"regime={regime_mode}"
            )
        else:
            try:
                from llm.client import LLMClient
                from llm.prompts import build_analysis_prompt
                llm = LLMClient()
                if llm.is_configured():
                    try:
                        prompt_msgs = build_analysis_prompt(
                            symbol, regime,
                            tech_score, tech_detail,
                            fundamental_score, fund_detail,
                            flow_score, flow_detail,
                            wiki_context=wiki_context,
                            sent_score=sentiment_score,
                            sent_detail=sent_detail,
                        )
                    except TypeError:
                        prompt_msgs = build_analysis_prompt(
                            symbol, regime,
                            tech_score, tech_detail,
                            fundamental_score, fund_detail,
                            flow_score, flow_detail,
                            wiki_context=wiki_context,
                        )
                    vote_result = await self._llm_analyze_with_vote(
                        prompt_msgs, symbol=symbol, market=market, n_calls=3,
                    )
                    llm_direction = vote_result["direction"]
                    llm_signal_strength = vote_result["signal_strength"]
                    llm_reasoning = vote_result["reasoning"]
                    llm_risks = vote_result["risks"]
                    llm_extra_advice = vote_result["extra_advice"]
                    logic_text = vote_result["narrative"]
                    llm_vote_consensus = vote_result["vote_consensus"]
                    llm_vote_total_calls = vote_result["vote_total_calls"]
            except Exception as e:
                logger.warning("llm_narrative_error", symbol=symbol, error=str(e))

        if not logic_text:
            # 降级：纯定量摘要
            logic_parts = [f"技术面 {tech_score:.0f}/100"]
            if fundamental_score is not None:
                fw_name = fund_detail.get("framework", "")
                logic_parts.append(f"基本面 {fundamental_score:.0f}/100({fw_name})")
            if flow_score is not None:
                logic_parts.append(f"资金面 {flow_score:.0f}/100")
            if sentiment_score is not None:
                logic_parts.append(f"情绪面 {sentiment_score:.0f}/100")
            logic_parts.append(f"Regime={regime_mode}")
            logic_text = "；".join(logic_parts)

        # 7. Wiki 更新（失败不阻塞）
        try:
            from llm.wiki_manager import WikiManager
            wm = WikiManager()
            analysis_for_wiki = {
                "direction": direction,
                "composite_score": composite,
                "technical_score": tech_score,
                "fundamental_score": fundamental_score,
                "flow_score": flow_score,
                "sentiment_score": sentiment_score,
                "logic_text": logic_text,
                "judgment_date": trade_date,
                "signal_sources": {
                    "technical": tech_detail,
                    "fundamental": fund_detail,
                    "flow": flow_detail,
                    "sentiment": sent_detail,
                },
            }
            page_path = wm._page_path(symbol, market)
            if wm.read_page(page_path):
                await wm.update_stock_page(symbol, market, analysis_for_wiki)
            else:
                await wm.create_stock_page(symbol, market, analysis_for_wiki)
        except Exception as e:
            logger.warning("wiki_update_error", symbol=symbol, error=str(e))

        signal_sources = {
            "technical": tech_detail,
            "fundamental": fund_detail,
            "flow": flow_detail,
            "sentiment": sent_detail,
            "regime_mode": regime_mode,
            "weights": weights,
            "rule_signal_strength": rule_signal_strength,
            "llm_direction": llm_direction,
            "llm_signal_strength": llm_signal_strength,
            "llm_reasoning": llm_reasoning,
            "llm_risks": llm_risks,
            "llm_extra_advice": llm_extra_advice,
            "llm_vote_consensus": llm_vote_consensus,
            "llm_vote_total_calls": llm_vote_total_calls,
            "position_sizing": (
                {
                    "shares": position_sizing.shares,
                    "position_value": position_sizing.position_value,
                    "position_pct": position_sizing.position_pct,
                    "risk_amount": position_sizing.risk_amount,
                    "risk_pct": position_sizing.risk_pct,
                    "risk_reward_ratio": position_sizing.risk_reward_ratio,
                }
                if position_sizing is not None
                else None
            ),
        }

        # 序列化 regime_snapshot（去除不可序列化的字段）
        regime_snapshot = {
            k: (v if not isinstance(v, (date,)) else str(v))
            for k, v in regime.items()
        }

        result = JudgmentResult(
            symbol=symbol,
            market=market,
            judgment_date=trade_date,
            timeframe=timeframe,
            technical_score=tech_score,
            fundamental_score=fundamental_score,
            flow_score=flow_score,
            sentiment_score=sentiment_score,
            composite_score=composite,
            direction=direction,
            confidence=confidence,
            rule_signal_strength=rule_signal_strength,
            logic_text=logic_text,
            suggested_action=suggested_action,
            entry_zone=entry_zone,
            stop_loss=stop_loss,
            target_price=target_price,
            signal_sources=signal_sources,
            regime_snapshot=regime_snapshot,
            position_sizing=position_sizing,
            llm_vote_consensus=llm_vote_consensus,
            llm_vote_total_calls=llm_vote_total_calls,
        )

        logger.info(
            "composite_analyze_done",
            symbol=symbol,
            composite=composite,
            direction=direction,
            confidence=confidence,
        )
        return result

    async def save_judgment(self, result: JudgmentResult) -> int:
        """将判断结果写入 judgments 表。

        Args:
            result: JudgmentResult 分析结果。

        Returns:
            新建记录的 ID。
        """
        try:
            entry_low = result.entry_zone[0] if result.entry_zone else None
            entry_high = result.entry_zone[1] if result.entry_zone else None

            ss = result.signal_sources or {}
            judgment_id = await db_query_val(
                """
                INSERT INTO judgments (
                    symbol, market, judgment_date, timeframe,
                    technical_score, fundamental_score, flow_score, sentiment_score,
                    composite_score, direction, confidence,
                    logic_text, suggested_action,
                    entry_zone_low, entry_zone_high, stop_loss, target_price,
                    signal_sources, regime_at_time,
                    rule_signal_strength, llm_direction, llm_signal_strength,
                    llm_reasoning, llm_risks, llm_extra_advice,
                    llm_vote_consensus, llm_vote_total_calls,
                    created_at
                ) VALUES (
                    $1, $2, $3, $4,
                    $5, $6, $7, $8,
                    $9, $10, $11,
                    $12, $13,
                    $14, $15, $16, $17,
                    $18, $19,
                    $20, $21, $22,
                    $23, $24, $25,
                    $26, $27,
                    NOW()
                )
                ON CONFLICT (symbol, market, judgment_date) DO UPDATE SET
                    timeframe             = EXCLUDED.timeframe,
                    technical_score       = EXCLUDED.technical_score,
                    fundamental_score     = EXCLUDED.fundamental_score,
                    flow_score            = EXCLUDED.flow_score,
                    sentiment_score       = EXCLUDED.sentiment_score,
                    composite_score       = EXCLUDED.composite_score,
                    direction             = EXCLUDED.direction,
                    confidence            = EXCLUDED.confidence,
                    logic_text            = EXCLUDED.logic_text,
                    suggested_action      = EXCLUDED.suggested_action,
                    entry_zone_low        = EXCLUDED.entry_zone_low,
                    entry_zone_high       = EXCLUDED.entry_zone_high,
                    stop_loss             = EXCLUDED.stop_loss,
                    target_price          = EXCLUDED.target_price,
                    signal_sources        = EXCLUDED.signal_sources,
                    regime_at_time        = EXCLUDED.regime_at_time,
                    rule_signal_strength  = EXCLUDED.rule_signal_strength,
                    llm_direction         = EXCLUDED.llm_direction,
                    llm_signal_strength   = EXCLUDED.llm_signal_strength,
                    llm_reasoning         = EXCLUDED.llm_reasoning,
                    llm_risks             = EXCLUDED.llm_risks,
                    llm_extra_advice      = EXCLUDED.llm_extra_advice,
                    llm_vote_consensus    = EXCLUDED.llm_vote_consensus,
                    llm_vote_total_calls  = EXCLUDED.llm_vote_total_calls,
                    created_at            = NOW()
                RETURNING id
                """,
                result.symbol,
                result.market,
                result.judgment_date,
                result.timeframe,
                result.technical_score,
                result.fundamental_score,
                result.flow_score,
                result.sentiment_score,
                result.composite_score,
                result.direction,
                result.confidence,
                result.logic_text,
                result.suggested_action,
                entry_low,
                entry_high,
                result.stop_loss,
                result.target_price,
                json.dumps(result.signal_sources, ensure_ascii=False, default=str),
                json.dumps(result.regime_snapshot, ensure_ascii=False, default=str),
                result.rule_signal_strength,
                ss.get("llm_direction") or "unknown",
                ss.get("llm_signal_strength") or "unknown",
                ss.get("llm_reasoning"),
                ss.get("llm_risks"),
                ss.get("llm_extra_advice"),
                result.llm_vote_consensus,
                result.llm_vote_total_calls,
            )

            logger.info(
                "judgment_saved",
                judgment_id=judgment_id,
                symbol=result.symbol,
                direction=result.direction,
            )
            return judgment_id

        except Exception as e:
            logger.error(
                "judgment_save_error",
                symbol=result.symbol,
                error=str(e),
            )
            raise

    async def analyze_universe(
        self,
        market: str = "CN",
        trade_date: date | None = None,
        dry_run: bool = False,
    ) -> list[JudgmentResult]:
        """分析候选池中所有活跃股票。

        Args:
            market: 市场代码。
            trade_date: 分析日期，默认今天。
            dry_run: True 时跳过 LLM 调用（开发/测试用）。

        Returns:
            JudgmentResult 列表。
        """
        if trade_date is None:
            trade_date = date.today()

        try:
            rows = await db_query(
                "SELECT symbol FROM stock_universe WHERE market = $1 AND active = TRUE ORDER BY symbol",
                market,
            )
        except Exception as e:
            logger.error("universe_query_error", market=market, error=str(e))
            return []

        if not rows:
            logger.warning("universe_empty", market=market)
            return []

        results: list[JudgmentResult] = []
        llm_failures: int = 0
        total = len(rows)

        for row in rows:
            symbol = row["symbol"]
            try:
                result = await self.analyze(symbol, market, trade_date, dry_run=dry_run)
                # 检测 LLM 是否降级到定量摘要（非 dry_run 时 logic_text 包含 [DRY RUN] 即异常）
                if not dry_run and result.logic_text and result.logic_text.startswith("技术面"):
                    llm_failures += 1
                results.append(result)
            except Exception as e:
                logger.error("universe_analyze_error", symbol=symbol, error=str(e))
                llm_failures += 1
                continue

        # LLM 失败率 > 20% → Telegram warn
        if total > 0 and not dry_run:
            fail_rate = llm_failures / total
            if fail_rate > 0.2:
                msg = (
                    f"⚠️ composite_analyze LLM 失败率过高: "
                    f"{llm_failures}/{total} ({fail_rate:.0%}) [{market}]"
                )
                logger.warning("composite_llm_high_failure", market=market,
                               failed=llm_failures, total=total)
                try:
                    from bot.telegram_bot import TelegramPusher
                    await TelegramPusher().send(msg)
                except Exception:
                    pass

        logger.info(
            "universe_analyze_done",
            market=market,
            total=total,
            analyzed=len(results),
            llm_failures=llm_failures,
            dry_run=dry_run,
        )
        return results
