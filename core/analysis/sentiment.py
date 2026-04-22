"""
情绪面分析引擎 — 社交热度、看多方向与市场情绪综合评分。

Phase 4 实现：
- 社交媒体情绪（StockTwits / 雪球）
- A股市场情绪（涨跌停比、融资余额、fear_greed 指数）
- 美股市场情绪（VIX 指数）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

import structlog

from db.connection import db_query, db_query_one

logger = structlog.get_logger(__name__)


@dataclass
class SentimentAnalysis:
    """情绪面分析结果数据类。"""

    symbol: str
    market: str
    social_heat: float       # 0-100，讨论量活跃度
    social_direction: float  # 0-100，看多比例归一化（50=中性）
    market_mood: float       # 0-100，市场情绪指标
    composite: float         # 0-100，加权综合
    detail: dict = field(default_factory=dict)  # 子维度详情，用于展示


class SentimentAnalyzer:
    """情绪面分析器。

    综合社交媒体情绪信号和市场宏观情绪数据，
    为个股生成标准化的情绪评分（0-100）。
    """

    async def analyze(
        self,
        symbol: str,
        market: str,
        trade_date: date | None = None,
    ) -> SentimentAnalysis:
        """分析个股情绪面评分。

        Args:
            symbol: 证券代码。
            market: 市场代码（'CN' | 'US'）。
            trade_date: 分析基准日期，默认今天（仅影响市场情绪查询范围）。

        Returns:
            SentimentAnalysis 情绪分析结果。
        """
        if trade_date is None:
            trade_date = date.today()

        logger.info(
            "sentiment_analyze_start",
            symbol=symbol,
            market=market,
            trade_date=str(trade_date),
        )

        # 1. 加载过去 48 小时的社交情绪数据
        social_rows = await self._load_social_sentiment(symbol)

        # 2. 社交热度评分（0-100）
        social_heat, heat_detail = self._calc_social_heat(social_rows)

        # 3. 社交方向评分（0-100）
        social_direction, direction_detail = self._calc_social_direction(social_rows)

        # 4. 市场情绪评分（0-100）
        market_mood, mood_detail = await self._calc_market_mood(market, trade_date)

        # 5. 综合评分
        has_social = social_heat != 50.0 or social_direction != 50.0
        if has_social:
            composite = (
                0.30 * social_heat
                + 0.30 * social_direction
                + 0.40 * market_mood
            )
        else:
            # 无社交数据时，只用市场级别数据
            composite = market_mood

        composite = round(max(0.0, min(100.0, composite)), 2)

        detail = {
            "social_heat": heat_detail,
            "social_direction": direction_detail,
            "market_mood": mood_detail,
        }

        logger.info(
            "sentiment_analyze_done",
            symbol=symbol,
            social_heat=social_heat,
            social_direction=social_direction,
            market_mood=market_mood,
            composite=composite,
        )

        return SentimentAnalysis(
            symbol=symbol,
            market=market,
            social_heat=social_heat,
            social_direction=social_direction,
            market_mood=market_mood,
            composite=composite,
            detail=detail,
        )

    async def _load_social_sentiment(self, symbol: str) -> list[dict[str, Any]]:
        """从 social_sentiment 表加载过去 48 小时的数据。

        Args:
            symbol: 证券代码。

        Returns:
            记录列表（最多 10 条，按时间降序）。
        """
        try:
            rows = await db_query(
                """
                SELECT symbol, market, snapshot_time, source,
                       bullish_pct, message_count, message_delta,
                       sentiment_score, raw_data
                FROM social_sentiment
                WHERE symbol = $1
                  AND snapshot_time > NOW() - INTERVAL '48 hours'
                ORDER BY snapshot_time DESC
                LIMIT 10
                """,
                symbol,
            )
            return [dict(r) for r in rows] if rows else []
        except Exception as e:
            logger.warning(
                "social_sentiment_load_error",
                symbol=symbol,
                error=str(e),
            )
            return []

    @staticmethod
    def _calc_social_heat(rows: list[dict[str, Any]]) -> tuple[float, dict[str, Any]]:
        """计算社交热度评分。

        根据 message_delta（讨论量环比变化）判断社交活跃度。

        Args:
            rows: social_sentiment 记录列表。

        Returns:
            (热度评分 0-100, 详情字典)。
        """
        if not rows:
            return 50.0, {
                "score": 50.0,
                "message_delta": None,
                "source": "no_data",
            }

        # 取最近一条有效的 message_delta
        latest = rows[0]
        msg_delta = None
        source = latest.get("source", "unknown")

        for row in rows:
            if row.get("message_delta") is not None:
                try:
                    msg_delta = float(row["message_delta"])
                    source = row.get("source", "unknown")
                    break
                except (TypeError, ValueError):
                    continue

        if msg_delta is None:
            # 无 delta 数据，但有记录 → 轻微活跃
            score = 55.0
        elif msg_delta > 20.0:
            # 明显增量 → 高热度
            score = 80.0 + min(msg_delta / 100.0 * 10.0, 10.0)  # 80-90
        elif msg_delta > 0.0:
            # 小幅增量 → 适度活跃 (55-75)
            score = 55.0 + (msg_delta / 20.0) * 20.0
        else:
            # 负增量 → 热度下降 (20-45)
            score = 45.0 + (msg_delta / 100.0) * 25.0  # 越负越低

        score = round(max(0.0, min(100.0, score)), 2)

        return score, {
            "score": score,
            "message_delta": msg_delta,
            "source": source,
        }

    @staticmethod
    def _calc_social_direction(rows: list[dict[str, Any]]) -> tuple[float, dict[str, Any]]:
        """计算社交方向（看多比例）评分。

        综合 bullish_pct 和 sentiment_score 计算标准化的看多得分。

        Args:
            rows: social_sentiment 记录列表。

        Returns:
            (方向评分 0-100, 详情字典)。
        """
        if not rows:
            return 50.0, {
                "score": 50.0,
                "bullish_pct": None,
                "sentiment_score": None,
            }

        # 收集有效 bullish_pct 和 sentiment_score
        bullish_values: list[float] = []
        sentiment_values: list[float] = []

        for row in rows:
            if row.get("bullish_pct") is not None:
                try:
                    bullish_values.append(float(row["bullish_pct"]))
                except (TypeError, ValueError):
                    pass
            if row.get("sentiment_score") is not None:
                try:
                    sentiment_values.append(float(row["sentiment_score"]))
                except (TypeError, ValueError):
                    pass

        avg_bullish: float | None = None
        avg_sentiment: float | None = None
        score_from_bullish: float | None = None
        score_from_sentiment: float | None = None

        if bullish_values:
            avg_bullish = sum(bullish_values) / len(bullish_values)
            # 分段映射: 0%→10, 30%→40, 50%→50, 70%→65, 90%→85
            b = avg_bullish
            if b <= 30.0:
                score_from_bullish = 10.0 + (b / 30.0) * 30.0
            elif b <= 50.0:
                score_from_bullish = 40.0 + ((b - 30.0) / 20.0) * 10.0
            elif b <= 70.0:
                score_from_bullish = 50.0 + ((b - 50.0) / 20.0) * 15.0
            else:
                score_from_bullish = 65.0 + ((b - 70.0) / 20.0) * 20.0

        if sentiment_values:
            avg_sentiment = sum(sentiment_values) / len(sentiment_values)
            # sentiment_score: -1→10, 0→50, +1→90（线性映射）
            score_from_sentiment = 50.0 + avg_sentiment * 40.0

        # 融合两个来源
        if score_from_bullish is not None and score_from_sentiment is not None:
            score = (score_from_bullish + score_from_sentiment) / 2.0
        elif score_from_bullish is not None:
            score = score_from_bullish
        elif score_from_sentiment is not None:
            score = score_from_sentiment
        else:
            score = 50.0  # 无有效数据

        score = round(max(0.0, min(100.0, score)), 2)

        return score, {
            "score": score,
            "bullish_pct": round(avg_bullish, 2) if avg_bullish is not None else None,
            "sentiment_score": round(avg_sentiment, 4) if avg_sentiment is not None else None,
        }

    async def _calc_market_mood(
        self,
        market: str,
        trade_date: date,
    ) -> tuple[float, dict[str, Any]]:
        """计算市场整体情绪评分。

        CN: 优先使用 fear_greed 指数，降级用涨跌停比 + 量化指标合成。
        US: 使用 VIX 指数（反向映射）。

        Args:
            market: 市场代码。
            trade_date: 基准日期。

        Returns:
            (市场情绪评分 0-100, 详情字典)。
        """
        if market == "CN":
            return await self._market_mood_cn(trade_date)
        else:
            return await self._market_mood_us(trade_date)

    async def _market_mood_cn(self, trade_date: date) -> tuple[float, dict[str, Any]]:
        """A股市场情绪评分。

        Args:
            trade_date: 基准日期。

        Returns:
            (评分 0-100, 详情字典)。
        """
        try:
            row = await db_query_one(
                """
                SELECT trade_date, fear_greed, up_down_ratio,
                       limit_up_count, limit_down_count,
                       margin_balance, margin_delta_5d
                FROM market_sentiment_daily
                WHERE trade_date <= $1
                ORDER BY trade_date DESC
                LIMIT 1
                """,
                trade_date,
            )
        except Exception as e:
            logger.warning("cn_market_mood_load_error", error=str(e))
            return 50.0, {"score": 50.0, "source": "fallback", "value": None}

        if not row:
            return 50.0, {"score": 50.0, "source": "fallback", "value": None}

        row_dict = dict(row)

        # 优先使用 fear_greed（已归一化 0-100）
        fear_greed = row_dict.get("fear_greed")
        if fear_greed is not None:
            try:
                score = float(fear_greed)
                score = round(max(0.0, min(100.0, score)), 2)
                return score, {
                    "score": score,
                    "source": "fear_greed",
                    "value": score,
                }
            except (TypeError, ValueError):
                pass

        # 降级：从 up_down_ratio 和涨跌停数量合成
        sub_scores: list[float] = []

        up_down_ratio = row_dict.get("up_down_ratio")
        if up_down_ratio is not None:
            try:
                ratio = float(up_down_ratio)
                # up_down_ratio: 2.0 → 70, 1.0 → 50, 0.5 → 35
                ratio_score = min(100.0, max(0.0, 50.0 + (ratio - 1.0) * 20.0))
                sub_scores.append(ratio_score)
            except (TypeError, ValueError):
                pass

        limit_up = row_dict.get("limit_up_count")
        limit_down = row_dict.get("limit_down_count")
        if limit_up is not None and limit_down is not None:
            try:
                lu = float(limit_up)
                ld = float(limit_down)
                total = lu + ld
                if total > 0:
                    lu_ratio = lu / total  # 涨停占比
                    # 0→10, 0.5→50, 1.0→90
                    limit_score = 10.0 + lu_ratio * 80.0
                    sub_scores.append(limit_score)
            except (TypeError, ValueError):
                pass

        if sub_scores:
            score = round(sum(sub_scores) / len(sub_scores), 2)
        else:
            score = 50.0

        return score, {
            "score": score,
            "source": "composite_cn",
            "value": score,
            "up_down_ratio": row_dict.get("up_down_ratio"),
            "limit_up_count": row_dict.get("limit_up_count"),
            "limit_down_count": row_dict.get("limit_down_count"),
        }

    async def _market_mood_us(self, trade_date: date) -> tuple[float, dict[str, Any]]:
        """美股市场情绪评分（基于 VIX）。

        VIX 与情绪分呈反向关系：VIX 越低，市场越乐观。

        Args:
            trade_date: 基准日期。

        Returns:
            (评分 0-100, 详情字典)。
        """
        try:
            row = await db_query_one(
                """
                SELECT value, report_date
                FROM macro_indicators
                WHERE indicator_name = 'us_vix'
                  AND market = 'US'
                  AND report_date <= $1
                ORDER BY report_date DESC
                LIMIT 1
                """,
                trade_date,
            )
        except Exception as e:
            logger.warning("us_vix_load_error", error=str(e))
            return 50.0, {"score": 50.0, "source": "fallback", "value": None}

        if not row:
            return 50.0, {"score": 50.0, "source": "vix", "value": None}

        try:
            vix = float(row["value"])
        except (TypeError, ValueError):
            return 50.0, {"score": 50.0, "source": "vix", "value": None}

        # VIX 反向映射：低 VIX = 乐观 = 高分
        if vix < 15.0:
            score = 80.0
        elif vix < 20.0:
            # 15-20 → 65-80 线性插值
            score = 65.0 + (20.0 - vix) / 5.0 * 15.0
        elif vix < 25.0:
            # 20-25 → 50-65
            score = 50.0 + (25.0 - vix) / 5.0 * 15.0
        elif vix < 30.0:
            # 25-30 → 35-50
            score = 35.0 + (30.0 - vix) / 5.0 * 15.0
        else:
            score = 20.0

        score = round(max(0.0, min(100.0, score)), 2)

        return score, {
            "score": score,
            "source": "vix",
            "value": round(vix, 2),
        }
