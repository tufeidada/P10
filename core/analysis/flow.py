"""
资金面分析引擎 — 主力资金、北向资金、融资融券三维评分（A股）
                + 成交量趋势、VIX 情绪、HYG 信用流代理评分（美股）。

根据近期资金流向数据综合评估个股资金面强弱，
生成 0-100 分的多维度评分。

Phase 2 模块，供 CompositeAnalyzer 调用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

import structlog

from db.connection import db_query, db_query_one

logger = structlog.get_logger(__name__)

# 中性默认分数（无数据时使用）
_NEUTRAL: float = 50.0


def _clip(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    """Clip value to [lo, hi]."""
    return max(lo, min(hi, value))


@dataclass
class FlowAnalysis:
    """资金面分析结果。"""

    symbol: str
    main_force_score: float    # 0-100, 主力资金评分
    northbound_score: float    # 0-100, 北向资金评分
    margin_score: float        # 0-100, 融资余额评分
    composite_score: float     # 0-100, 加权综合分
    detail: dict = field(default_factory=dict)


class FlowAnalyzer:
    """资金面分析引擎。

    三维度分析:
        1. 主力资金 (40%): 近 5 日大单资金净流入趋势
        2. 北向资金 (30%): 近 5/20 日北向资金流入趋势（市场级别）
        3. 融资余额 (30%): 近 5 日融资余额变化趋势

    当某维度无数据时，权重自动重新分配到有数据的维度。
    """

    # 默认权重
    _WEIGHT_MAIN_FORCE: float = 0.40
    _WEIGHT_NORTHBOUND: float = 0.30
    _WEIGHT_MARGIN: float = 0.30

    async def analyze(
        self,
        symbol: str,
        trade_date: date | None = None,
        market: str = "CN",
    ) -> FlowAnalysis:
        """分析单只股票的资金面。

        对 A 股（market='CN'）使用主力资金/北向/融资三维评分；
        对美股（market='US'）使用成交量趋势/VIX/HYG 代理评分。

        Steps (CN):
            1. 加载近 5 日 moneyflow_daily，计算主力资金评分
            2. 加载近 5/20 日 northbound_daily，计算北向资金评分
            3. 加载近 5 日 margin_daily，计算融资余额评分
            4. 加权合成（缺失维度权重重新分配）

        Steps (US):
            1. 成交量趋势（50%）: vol_5d_avg / vol_20d_avg
            2. VIX 情绪代理（30%）: 反向评分，VIX 越低越好
            3. HYG 信用流代理（20%）: 近 5 日涨跌

        Args:
            symbol: 证券代码，如 '600519.SH' 或 'AAPL'。
            trade_date: 分析日期，默认今天。
            market: 市场代码 ('CN' | 'US')，默认 'CN'。

        Returns:
            FlowAnalysis 分析结果。
        """
        if trade_date is None:
            trade_date = date.today()

        log = logger.bind(symbol=symbol, trade_date=str(trade_date), market=market)
        log.info("flow_analyze_start")

        # 美股走独立分析路径
        if market == "US":
            return await self._analyze_us(symbol, trade_date)

        # 计算三个维度
        main_score, main_detail, main_valid = await self._calc_main_force(
            symbol, trade_date
        )
        nb_score, nb_detail, nb_valid = await self._calc_northbound(trade_date)
        margin_score, margin_detail, margin_valid = await self._calc_margin(
            symbol, trade_date
        )

        # 加权合成（缺失维度重新分配权重）
        weights: dict[str, float] = {}
        scores: dict[str, float] = {}

        if main_valid:
            weights["main_force"] = self._WEIGHT_MAIN_FORCE
            scores["main_force"] = main_score
        if nb_valid:
            weights["northbound"] = self._WEIGHT_NORTHBOUND
            scores["northbound"] = nb_score
        if margin_valid:
            weights["margin"] = self._WEIGHT_MARGIN
            scores["margin"] = margin_score

        if weights:
            total_weight = sum(weights.values())
            composite = sum(
                scores[k] * (w / total_weight)
                for k, w in weights.items()
            )
            composite = _clip(round(composite, 2))
        else:
            composite = _NEUTRAL

        detail = {
            "main_force": main_detail,
            "northbound": nb_detail,
            "margin": margin_detail,
            "weights_used": {k: round(w / sum(weights.values()), 2) for k, w in weights.items()}
            if weights else {},
        }

        log.info(
            "flow_analyze_done",
            main_force=main_score,
            northbound=nb_score,
            margin=margin_score,
            composite=composite,
        )

        return FlowAnalysis(
            symbol=symbol,
            main_force_score=main_score,
            northbound_score=nb_score,
            margin_score=margin_score,
            composite_score=composite,
            detail=detail,
        )

    async def _analyze_us(
        self, symbol: str, trade_date: date
    ) -> FlowAnalysis:
        """US stock flow analysis using volume trends as proxy.

        US market has no moneyflow/northbound/margin data.
        Use volume trend as the primary proxy (50% weight).
        Use VIX momentum as risk proxy (30% weight).
        Use HYG trend as credit flow proxy (20% weight).

        Volume score:
            Load last 20 days of volume from market_bars_daily (market='US').
            vol_5d_avg / vol_20d_avg: > 1.2 → 80, 1.0-1.2 → 65,
                                      0.8-1.0 → 50, < 0.8 → 35.

        VIX score (inverted — lower VIX = better flow):
            Load latest VIX from macro_indicators.
            VIX < 15 → 80, 15-20 → 65, 20-25 → 50, 25-30 → 35, > 30 → 20.

        HYG score:
            Load last 10 days of HYG from macro_indicators.
            5d return positive → 70, negative → 35, no data → 50.

        Composite = 0.5*vol + 0.3*vix + 0.2*hyg.

        Args:
            symbol: US ticker, e.g. 'AAPL'.
            trade_date: Analysis date.

        Returns:
            FlowAnalysis with US-specific detail dict.
        """
        log = logger.bind(symbol=symbol, trade_date=str(trade_date), market="US")

        # ---- Volume score ----
        vol_score: float = _NEUTRAL
        vol_detail: dict[str, Any] = {"score": _NEUTRAL, "note": "no data"}
        try:
            start_vol = trade_date - timedelta(days=30)  # cover 20 trading days
            vol_rows = await db_query(
                """
                SELECT trade_date, volume
                FROM market_bars_daily
                WHERE symbol = $1 AND market = 'US'
                  AND trade_date BETWEEN $2 AND $3
                ORDER BY trade_date DESC
                LIMIT 20
                """,
                symbol,
                start_vol,
                trade_date,
            )
            if vol_rows and len(vol_rows) >= 5:
                volumes = [
                    float(r["volume"]) for r in vol_rows if r["volume"] is not None
                ]
                vol_5d_avg = sum(volumes[:5]) / 5.0
                vol_20d_avg = sum(volumes) / len(volumes)
                if vol_20d_avg > 0:
                    ratio = vol_5d_avg / vol_20d_avg
                    if ratio > 1.2:
                        vol_score = 80.0
                    elif ratio >= 1.0:
                        vol_score = 65.0
                    elif ratio >= 0.8:
                        vol_score = 50.0
                    else:
                        vol_score = 35.0
                    vol_detail = {
                        "score": vol_score,
                        "vol_5d_avg": round(vol_5d_avg, 0),
                        "vol_20d_avg": round(vol_20d_avg, 0),
                        "ratio": round(ratio, 3),
                        "data_points": len(volumes),
                    }
            else:
                log.info("us_vol_insufficient_data", rows=len(vol_rows) if vol_rows else 0)
        except Exception as e:
            log.warning("us_vol_query_error", error=str(e))

        # ---- VIX score ----
        vix_score: float = _NEUTRAL
        vix_detail: dict[str, Any] = {"score": _NEUTRAL, "note": "no data"}
        try:
            vix_row = await db_query_one(
                """
                SELECT report_date, value
                FROM macro_indicators
                WHERE indicator_name = 'VIX'
                  AND report_date <= $1
                ORDER BY report_date DESC
                LIMIT 1
                """,
                trade_date,
            )
            if vix_row and vix_row["value"] is not None:
                vix_val = float(vix_row["value"])
                if vix_val < 15.0:
                    vix_score = 80.0
                elif vix_val < 20.0:
                    vix_score = 65.0
                elif vix_val < 25.0:
                    vix_score = 50.0
                elif vix_val < 30.0:
                    vix_score = 35.0
                else:
                    vix_score = 20.0
                vix_detail = {
                    "score": vix_score,
                    "vix": round(vix_val, 2),
                    "as_of": str(vix_row["report_date"]),
                }
            else:
                log.info("us_vix_no_data")
        except Exception as e:
            log.warning("us_vix_query_error", error=str(e))

        # ---- HYG score ----
        hyg_score: float = _NEUTRAL
        hyg_detail: dict[str, Any] = {"score": _NEUTRAL, "note": "no data"}
        try:
            hyg_rows = await db_query(
                """
                SELECT report_date, value
                FROM macro_indicators
                WHERE indicator_name = 'HYG'
                  AND report_date <= $1
                ORDER BY report_date DESC
                LIMIT 10
                """,
                trade_date,
            )
            if hyg_rows and len(hyg_rows) >= 2:
                hyg_vals = [
                    float(r["value"]) for r in hyg_rows if r["value"] is not None
                ]
                # rows[0] is newest, rows[4] is ~5 days ago
                idx_5d = min(4, len(hyg_vals) - 1)
                ret_5d = (hyg_vals[0] - hyg_vals[idx_5d]) / hyg_vals[idx_5d] * 100.0
                hyg_score = 70.0 if ret_5d >= 0 else 35.0
                hyg_detail = {
                    "score": hyg_score,
                    "hyg_latest": round(hyg_vals[0], 4),
                    "hyg_5d_ago": round(hyg_vals[idx_5d], 4),
                    "ret_5d_pct": round(ret_5d, 3),
                }
            else:
                log.info("us_hyg_insufficient_data")
        except Exception as e:
            log.warning("us_hyg_query_error", error=str(e))

        # ---- Composite ----
        composite = _clip(round(
            0.5 * vol_score + 0.3 * vix_score + 0.2 * hyg_score, 2
        ))

        detail: dict[str, Any] = {
            "market": "US",
            "volume": vol_detail,
            "vix": vix_detail,
            "hyg": hyg_detail,
            "weights_used": {"volume": 0.5, "vix": 0.3, "hyg": 0.2},
        }

        log.info(
            "flow_analyze_us_done",
            vol_score=vol_score,
            vix_score=vix_score,
            hyg_score=hyg_score,
            composite=composite,
        )

        return FlowAnalysis(
            symbol=symbol,
            main_force_score=vol_score,   # reused field: volume proxy
            northbound_score=vix_score,   # reused field: VIX proxy
            margin_score=hyg_score,       # reused field: HYG proxy
            composite_score=composite,
            detail=detail,
        )

    async def _calc_main_force(
        self, symbol: str, trade_date: date
    ) -> tuple[float, dict[str, Any], bool]:
        """计算主力资金评分。

        加载近 5 日 moneyflow_daily，分析大单净流入金额及趋势。

        评分逻辑:
            - net_lg_5d_pct = 5日大单净流入 / 5日大单平均总额
            - > +5% -> 90, +2~5% -> 75, 0~2% -> 60, -2~0% -> 40, < -2% -> 20
            - 趋势加减分: 5天中正流入天数越多，分数越高

        Args:
            symbol: 证券代码。
            trade_date: 分析日期。

        Returns:
            (score, detail_dict, has_data) 三元组。
        """
        log = logger.bind(symbol=symbol, module="main_force")

        try:
            start = trade_date - timedelta(days=10)  # 回溯足够多天以覆盖非交易日
            rows = await db_query(
                """
                SELECT trade_date, net_lg_amount, buy_lg_amount, sell_lg_amount
                FROM moneyflow_daily
                WHERE symbol = $1 AND trade_date BETWEEN $2 AND $3
                ORDER BY trade_date DESC
                LIMIT 5
                """,
                symbol,
                start,
                trade_date,
            )
        except Exception as e:
            log.warning("main_force_query_error", error=str(e))
            return _NEUTRAL, {"score": _NEUTRAL, "note": "query error"}, False

        if not rows or len(rows) < 2:
            return _NEUTRAL, {"score": _NEUTRAL, "note": "insufficient data"}, False

        # 提取数据
        net_lg_values: list[float] = []
        buy_lg_values: list[float] = []
        sell_lg_values: list[float] = []

        for r in rows:
            net = float(r["net_lg_amount"]) if r["net_lg_amount"] is not None else 0.0
            buy = float(r["buy_lg_amount"]) if r["buy_lg_amount"] is not None else 0.0
            sell = float(r["sell_lg_amount"]) if r["sell_lg_amount"] is not None else 0.0
            net_lg_values.append(net)
            buy_lg_values.append(buy)
            sell_lg_values.append(sell)

        net_lg_5d = sum(net_lg_values)

        # 计算平均日交易额（买+卖）作为基准
        avg_daily_amount = (sum(buy_lg_values) + sum(sell_lg_values)) / len(rows)

        # 计算净流入占比
        net_lg_5d_pct: float = 0.0
        if avg_daily_amount > 0:
            net_lg_5d_pct = (net_lg_5d / avg_daily_amount) * 100.0

        # 基础评分
        if net_lg_5d_pct > 5.0:
            score = 90.0
        elif net_lg_5d_pct > 2.0:
            score = 75.0
        elif net_lg_5d_pct > 0.0:
            score = 60.0
        elif net_lg_5d_pct > -2.0:
            score = 40.0
        else:
            score = 20.0

        # 趋势加减分: 统计正流入天数
        positive_days = sum(1 for v in net_lg_values if v > 0)
        if positive_days >= 4:
            score += 5.0
        elif positive_days <= 1:
            score -= 5.0

        # 趋势方向: 从旧到新是否递增
        # rows 是降序，reverse 得到升序
        trend = "mixed"
        if len(net_lg_values) >= 3:
            chrono = list(reversed(net_lg_values))
            if all(chrono[i] < chrono[i + 1] for i in range(len(chrono) - 1)):
                trend = "improving"
                score += 5.0
            elif all(chrono[i] > chrono[i + 1] for i in range(len(chrono) - 1)):
                trend = "deteriorating"
                score -= 5.0

        score = _clip(round(score, 1))

        detail = {
            "score": score,
            "net_lg_5d": round(net_lg_5d, 2),
            "net_lg_5d_pct": round(net_lg_5d_pct, 2),
            "positive_days": positive_days,
            "total_days": len(rows),
            "trend": trend,
        }
        return score, detail, True

    async def _calc_northbound(
        self, trade_date: date
    ) -> tuple[float, dict[str, Any], bool]:
        """计算北向资金评分（市场级别，非个股）。

        加载近 5/20 日 northbound_daily，分析净流入趋势。

        评分逻辑:
            - 短期 (5日): net_5d > 0 且金额大 -> 高分
            - 长期 (20日): net_20d > 0 -> 加分
            - 组合: both positive -> 80+, 5d positive 20d negative -> 55-70,
              both negative -> 20-40

        Args:
            trade_date: 分析日期。

        Returns:
            (score, detail_dict, has_data) 三元组。
        """
        log = logger.bind(module="northbound")

        try:
            start_20d = trade_date - timedelta(days=40)  # 足够覆盖 20 个交易日
            rows = await db_query(
                """
                SELECT trade_date, total_net_buy
                FROM northbound_daily
                WHERE trade_date BETWEEN $1 AND $2
                ORDER BY trade_date DESC
                LIMIT 20
                """,
                start_20d,
                trade_date,
            )
        except Exception as e:
            log.warning("northbound_query_error", error=str(e))
            return _NEUTRAL, {"score": _NEUTRAL, "note": "query error"}, False

        if not rows or len(rows) < 3:
            return _NEUTRAL, {"score": _NEUTRAL, "note": "insufficient data"}, False

        # 提取净买入数据
        all_values = [
            float(r["total_net_buy"]) if r["total_net_buy"] is not None else 0.0
            for r in rows
        ]

        # 近 5 日
        values_5d = all_values[:min(5, len(all_values))]
        net_5d = sum(values_5d)

        # 近 20 日
        net_20d = sum(all_values)

        # 评分
        if net_5d > 0 and net_20d > 0:
            # 双正: 80-95
            if net_5d > net_20d * 0.4:
                # 短期占比大，加速流入
                score = 90.0
            else:
                score = 80.0
        elif net_5d > 0 and net_20d <= 0:
            # 短期正、长期负: 55-70，短期改善
            score = 65.0
        elif net_5d <= 0 and net_20d > 0:
            # 短期负、长期正: 45-55，短期回调
            score = 50.0
        else:
            # 双负: 20-40
            if net_5d < net_20d * 0.4:
                score = 20.0
            else:
                score = 35.0

        # 5 日内正流入天数加减分
        positive_5d = sum(1 for v in values_5d if v > 0)
        if positive_5d >= 4:
            score += 5.0
        elif positive_5d <= 1:
            score -= 5.0

        # 趋势 (从旧到新)
        trend = "mixed"
        if len(values_5d) >= 3:
            chrono = list(reversed(values_5d))
            if all(chrono[i] < chrono[i + 1] for i in range(len(chrono) - 1)):
                trend = "improving"
            elif all(chrono[i] > chrono[i + 1] for i in range(len(chrono) - 1)):
                trend = "deteriorating"

        score = _clip(round(score, 1))

        detail = {
            "score": score,
            "net_5d": round(net_5d, 2),
            "net_20d": round(net_20d, 2),
            "positive_days_5d": positive_5d,
            "trend": trend,
        }
        return score, detail, True

    async def _calc_margin(
        self, symbol: str, trade_date: date
    ) -> tuple[float, dict[str, Any], bool]:
        """计算融资余额评分。

        加载近 5 日 margin_daily，分析融资余额变化趋势。

        评分逻辑:
            - change_5d_pct = (rzye[-1] - rzye[-5]) / rzye[-5] * 100
            - > +3% -> 85, +1~3% -> 70, 0~1% -> 55, -1~0% -> 40, < -1% -> 25

        若个股无融资融券数据，尝试使用 market_sentiment_daily.margin_balance
        作为市场级别代理。若仍无数据，标记维度无效。

        Args:
            symbol: 证券代码。
            trade_date: 分析日期。

        Returns:
            (score, detail_dict, has_data) 三元组。
        """
        log = logger.bind(symbol=symbol, module="margin")

        try:
            start = trade_date - timedelta(days=10)
            rows = await db_query(
                """
                SELECT trade_date, rzye
                FROM margin_daily
                WHERE symbol = $1 AND trade_date BETWEEN $2 AND $3
                ORDER BY trade_date DESC
                LIMIT 5
                """,
                symbol,
                start,
                trade_date,
            )
        except Exception as e:
            log.warning("margin_query_error", error=str(e))
            rows = []

        if rows and len(rows) >= 2:
            return self._score_margin_rows(rows)

        # 降级: 尝试市场级别融资余额
        log.info("margin_fallback_to_market_level")
        try:
            market_rows = await db_query(
                """
                SELECT trade_date, margin_balance
                FROM market_sentiment_daily
                WHERE trade_date BETWEEN $1 AND $2
                  AND margin_balance IS NOT NULL
                ORDER BY trade_date DESC
                LIMIT 5
                """,
                trade_date - timedelta(days=10),
                trade_date,
            )
        except Exception as e:
            log.warning("margin_market_fallback_error", error=str(e))
            market_rows = []

        if market_rows and len(market_rows) >= 2:
            # 将 market_sentiment_daily.margin_balance 映射为与 rzye 相同的结构
            pseudo_rows = [
                {"trade_date": r["trade_date"], "rzye": r["margin_balance"]}
                for r in market_rows
            ]
            score, detail, valid = self._score_margin_rows(pseudo_rows)
            detail["source"] = "market_level"
            return score, detail, valid

        return _NEUTRAL, {"score": _NEUTRAL, "note": "no margin data"}, False

    @staticmethod
    def _score_margin_rows(
        rows: list[Any],
    ) -> tuple[float, dict[str, Any], bool]:
        """对融资余额数据行进行评分。

        Args:
            rows: 按 trade_date 降序排列的行（需包含 rzye 字段）。

        Returns:
            (score, detail_dict, has_data) 三元组。
        """
        # rows[0] 最新, rows[-1] 最老
        rzye_values = []
        for r in rows:
            val = r.get("rzye") if isinstance(r, dict) else r["rzye"]
            if val is not None:
                rzye_values.append(float(val))

        if len(rzye_values) < 2:
            return _NEUTRAL, {"score": _NEUTRAL, "note": "insufficient rzye data"}, False

        latest = rzye_values[0]
        oldest = rzye_values[-1]

        change_5d_pct: float = 0.0
        if oldest > 0:
            change_5d_pct = ((latest - oldest) / oldest) * 100.0

        if change_5d_pct > 3.0:
            score = 85.0
        elif change_5d_pct > 1.0:
            score = 70.0
        elif change_5d_pct > 0.0:
            score = 55.0
        elif change_5d_pct > -1.0:
            score = 40.0
        else:
            score = 25.0

        score = _clip(round(score, 1))

        detail = {
            "score": score,
            "rzye_latest": round(latest, 2),
            "change_5d_pct": round(change_5d_pct, 2),
            "data_points": len(rzye_values),
            "source": "individual",
        }
        return score, detail, True
