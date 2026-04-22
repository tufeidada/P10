"""
多时间框架技术分析模块

日线 + 周线双时间框架分析，输出 0-100 综合评分。
包含趋势判断、动量分析、形态识别、关键价位、量价确认等维度。
所有技术指标从原始K线自行计算（不依赖 features_daily 预计算）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
import structlog

from core.analysis.stage_detector import StageDetector
from db.connection import db_query

logger = structlog.get_logger(__name__)


@dataclass
class TimeframeAnalysis:
    """单时间框架分析结果。

    Attributes:
        trend: 趋势方向 'up' | 'down' | 'sideways'。
        stage: Weinstein Stage 1/2/3/4。
        strength: 趋势强度 0-100。
        ma_alignment: 均线多头/空头排列得分 0-100。
        rs_rank: O'Neil 相对强度排名 0-100。
        momentum: 动量状态 'accelerating' | 'steady' | 'decelerating'。
        key_levels: 关键支撑/阻力位 {'support': [...], 'resistance': [...]}。
        pattern: 形态 'breakout' | 'pullback' | 'consolidation' | 'breakdown' | 'none'。
    """

    trend: str = "sideways"
    stage: int = 1
    strength: float = 50.0
    ma_alignment: float = 50.0
    rs_rank: float = 50.0
    momentum: str = "steady"
    key_levels: dict[str, list[float]] = field(
        default_factory=lambda: {"support": [], "resistance": []}
    )
    pattern: str = "none"


class TechnicalAnalyzer:
    """多时间框架技术分析器。

    结合日线和周线两个时间框架进行分析，输出综合评分和详细分析结果。

    Composite Score 权重:
        - MA alignment × 0.25
        - Trend strength × 0.25
        - RS rank × 0.20
        - Momentum quality × 0.15
        - Volume confirmation × 0.15
    """

    def __init__(self) -> None:
        self._stage_detector = StageDetector()

    async def analyze(
        self,
        symbol: str,
        trade_date: date | None = None,
    ) -> dict[str, Any]:
        """执行完整技术分析。

        Steps:
            1. 加载 ~250 天日线数据
            2. 计算日线时间框架分析
            3. 聚合为周线并计算周线时间框架分析
            4. 合并双时间框架结论
            5. 计算 0-100 综合评分

        Args:
            symbol: 股票代码。
            trade_date: 分析日期，None 取最新。

        Returns:
            dict: {
                'score': float 0-100,
                'trend': str,
                'stage': int,
                'rs_rank': float,
                'confidence_adj': float 0-1,
                'key_levels': dict,
                'pattern': str,
                'details': {
                    'daily': TimeframeAnalysis,
                    'weekly': TimeframeAnalysis,
                    'volume_confirmation': float,
                    'momentum_score': float,
                }
            }
        """
        bars = await self._load_bars(symbol, trade_date, lookback=250)
        if len(bars) < 60:
            logger.warning(
                "insufficient_bars_for_analysis",
                symbol=symbol,
                bar_count=len(bars),
            )
            return self._empty_result()

        # 基础数据提取
        closes = np.array([float(b["close"]) for b in bars], dtype=np.float64)
        highs = np.array([float(b["high"]) for b in bars], dtype=np.float64)
        lows = np.array([float(b["low"]) for b in bars], dtype=np.float64)
        volumes = np.array([float(b["volume"]) for b in bars], dtype=np.float64)

        # Stage + RS Rank
        stage = await self._stage_detector.detect_stage(symbol, trade_date)
        rs_rank = await self._stage_detector.calc_rs_rank(symbol, trade_date)

        # ---- 日线分析 ----
        daily_ma_alignment = self._calc_ma_alignment(closes)
        daily_trend = self._determine_trend(closes, daily_ma_alignment)
        daily_adx, daily_plus_di, daily_minus_di = self._calc_adx(
            highs, lows, closes, period=14
        )
        daily_rsi = self._calc_rsi(closes, period=14)
        daily_strength = self._calc_trend_strength(
            daily_adx, daily_rsi, volumes, daily_trend
        )
        daily_momentum = self._assess_momentum(closes, volumes)

        # 关键价位 & 形态
        ma20 = float(np.mean(closes[-20:])) if len(closes) >= 20 else float(closes[-1])
        high_20d = float(np.max(highs[-20:])) if len(highs) >= 20 else float(highs[-1])
        daily_pattern = self._detect_pattern(bars, ma20, high_20d)
        daily_key_levels = self._find_key_levels(bars)

        daily_analysis = TimeframeAnalysis(
            trend=daily_trend,
            stage=stage,
            strength=daily_strength,
            ma_alignment=daily_ma_alignment,
            rs_rank=rs_rank,
            momentum=daily_momentum,
            key_levels=daily_key_levels,
            pattern=daily_pattern,
        )

        # ---- 周线分析 ----
        weekly_bars = self._aggregate_to_weekly(bars)
        weekly_analysis = self._analyze_weekly(weekly_bars, stage, rs_rank)

        # ---- 双时间框架合并 ----
        confidence_adj = self._combine_timeframes(daily_analysis, weekly_analysis)

        # ---- 量价确认 ----
        volume_confirmation = self._calc_volume_confirmation(
            closes, volumes, daily_trend
        )

        # ---- 动量得分 ----
        momentum_score = self._momentum_to_score(daily_momentum)

        # ---- 综合评分 ----
        composite_score = (
            daily_ma_alignment * 0.25
            + daily_strength * 0.25
            + rs_rank * 0.20
            + momentum_score * 0.15
            + volume_confirmation * 0.15
        )
        composite_score = round(
            np.clip(composite_score * confidence_adj, 0, 100), 2
        )

        result = {
            "score": composite_score,
            "trend": daily_trend,
            "stage": stage,
            "rs_rank": rs_rank,
            "confidence_adj": round(confidence_adj, 2),
            "key_levels": daily_key_levels,
            "pattern": daily_pattern,
            "details": {
                "daily": daily_analysis,
                "weekly": weekly_analysis,
                "volume_confirmation": round(volume_confirmation, 2),
                "momentum_score": round(momentum_score, 2),
            },
        }

        logger.info(
            "technical_analysis_done",
            symbol=symbol,
            trade_date=str(trade_date),
            score=composite_score,
            trend=daily_trend,
            stage=stage,
            pattern=daily_pattern,
        )
        return result

    # ------------------------------------------------------------------
    # Weekly aggregation
    # ------------------------------------------------------------------

    @staticmethod
    def _aggregate_to_weekly(daily_bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """将日线 OHLCV 聚合为周线。

        以自然周（ISO week）为分组维度，每周的 OHLCV 聚合规则:
            - open: 周内第一天的 open
            - high: 周内最高 high
            - low: 周内最低 low
            - close: 周内最后一天的 close
            - volume: 周内 volume 之和

        Args:
            daily_bars: 按日期升序的日线 bar 列表。

        Returns:
            按周升序排列的周线 bar 列表。
        """
        if not daily_bars:
            return []

        weekly: list[dict[str, Any]] = []
        current_week: list[dict[str, Any]] = []
        current_iso_week: tuple[int, int] | None = None

        for bar in daily_bars:
            td = bar["trade_date"]
            iso_week = (td.isocalendar()[0], td.isocalendar()[1])

            if current_iso_week is None:
                current_iso_week = iso_week

            if iso_week != current_iso_week:
                # 收拢前一周
                if current_week:
                    weekly.append(_merge_week(current_week))
                current_week = [bar]
                current_iso_week = iso_week
            else:
                current_week.append(bar)

        # 最后一周
        if current_week:
            weekly.append(_merge_week(current_week))

        return weekly

    # ------------------------------------------------------------------
    # MA alignment
    # ------------------------------------------------------------------

    @staticmethod
    def _calc_ma_alignment(closes: np.ndarray) -> float:
        """计算均线多头排列得分 0-100。

        检查 MA5 > MA20 > MA60 的排列关系，并考虑价格与均线的距离。

        Args:
            closes: 收盘价数组（日期升序）。

        Returns:
            均线排列得分 0-100。
        """
        n = len(closes)
        score = 50.0  # 中性起点

        ma5 = np.mean(closes[-5:]) if n >= 5 else closes[-1]
        ma20 = np.mean(closes[-20:]) if n >= 20 else closes[-1]
        ma60 = np.mean(closes[-60:]) if n >= 60 else closes[-1]

        # 多头排列: MA5 > MA20 > MA60
        if ma5 > ma20 > ma60:
            score = 75.0
            # 价格在 MA5 上方 → 更强
            if closes[-1] > ma5:
                score = 90.0
        elif ma5 > ma20:
            score = 60.0
        # 空头排列: MA5 < MA20 < MA60
        elif ma5 < ma20 < ma60:
            score = 25.0
            if closes[-1] < ma5:
                score = 10.0
        elif ma5 < ma20:
            score = 40.0

        # MA150/MA200 加分（长期趋势确认）
        if n >= 150:
            ma150 = np.mean(closes[-150:])
            if closes[-1] > ma150:
                score = min(100.0, score + 5)
            else:
                score = max(0.0, score - 5)

        return score

    # ------------------------------------------------------------------
    # Pattern detection
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_pattern(
        bars: list[dict[str, Any]],
        ma20: float,
        high_20d: float,
    ) -> str:
        """识别价格形态。

        Args:
            bars: 日线 bar 列表（日期升序）。
            ma20: 20 日均线值。
            high_20d: 近 20 日最高价。

        Returns:
            形态标识: 'breakout' | 'pullback' | 'consolidation' | 'breakdown' | 'none'。
        """
        if len(bars) < 20:
            return "none"

        latest = bars[-1]
        close = float(latest["close"])
        volume = float(latest["volume"])

        # 近 20 日平均成交量
        recent_volumes = [float(b["volume"]) for b in bars[-20:]]
        avg_vol = np.mean(recent_volumes) if recent_volumes else 1.0

        # 近 20 日最低价
        low_20d = min(float(b["low"]) for b in bars[-20:])

        # Breakout: 收盘创 20 日新高 + 成交量放大 1.5 倍以上
        if close >= high_20d * 0.998 and volume > avg_vol * 1.5:
            return "breakout"

        # Breakdown: 收盘创 20 日新低 + 放量
        if close <= low_20d * 1.002 and volume > avg_vol * 1.3:
            return "breakdown"

        # Pullback: 上升趋势中回踩 MA20 附近
        closes_arr = np.array([float(b["close"]) for b in bars[-60:]])
        if len(closes_arr) >= 60:
            ma60 = np.mean(closes_arr)
            if close > ma60 and abs(close / ma20 - 1) < 0.02:
                return "pullback"

        # Consolidation: 近 10 日波动率收窄
        if len(bars) >= 10:
            recent_closes = np.array([float(b["close"]) for b in bars[-10:]])
            volatility = np.std(recent_closes) / np.mean(recent_closes)
            if volatility < 0.02:
                return "consolidation"

        return "none"

    # ------------------------------------------------------------------
    # Key levels
    # ------------------------------------------------------------------

    @staticmethod
    def _find_key_levels(bars: list[dict[str, Any]]) -> dict[str, list[float]]:
        """寻找关键支撑和阻力位。

        使用近 20 日和 60 日的高低点作为关键价位。

        Args:
            bars: 日线 bar 列表。

        Returns:
            {'support': [价位列表], 'resistance': [价位列表]}
        """
        supports: list[float] = []
        resistances: list[float] = []

        if len(bars) < 20:
            return {"support": supports, "resistance": resistances}

        current_close = float(bars[-1]["close"])

        # 20 日高低点
        highs_20 = [float(b["high"]) for b in bars[-20:]]
        lows_20 = [float(b["low"]) for b in bars[-20:]]
        high_20 = max(highs_20)
        low_20 = min(lows_20)

        # 60 日高低点
        if len(bars) >= 60:
            highs_60 = [float(b["high"]) for b in bars[-60:]]
            lows_60 = [float(b["low"]) for b in bars[-60:]]
            high_60 = max(highs_60)
            low_60 = min(lows_60)
        else:
            high_60 = high_20
            low_60 = low_20

        # 均线作为动态支撑/阻力
        closes = np.array([float(b["close"]) for b in bars], dtype=np.float64)
        ma20 = float(np.mean(closes[-20:])) if len(closes) >= 20 else current_close
        ma60 = float(np.mean(closes[-60:])) if len(closes) >= 60 else current_close

        # 分类: 低于当前价的是支撑，高于当前价的是阻力
        candidates = [
            ("low_20", low_20),
            ("low_60", low_60),
            ("high_20", high_20),
            ("high_60", high_60),
            ("ma20", ma20),
            ("ma60", ma60),
        ]

        for _label, level in candidates:
            level_rounded = round(level, 2)
            if level < current_close * 0.995:
                supports.append(level_rounded)
            elif level > current_close * 1.005:
                resistances.append(level_rounded)

        # 去重并排序
        supports = sorted(set(supports), reverse=True)[:3]
        resistances = sorted(set(resistances))[:3]

        return {"support": supports, "resistance": resistances}

    # ------------------------------------------------------------------
    # Trend / Strength / Momentum helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _determine_trend(closes: np.ndarray, ma_alignment: float) -> str:
        """根据均线排列得分判定趋势方向。

        Args:
            closes: 收盘价数组。
            ma_alignment: 均线排列得分 0-100。

        Returns:
            'up' | 'down' | 'sideways'。
        """
        if ma_alignment >= 65:
            return "up"
        elif ma_alignment <= 35:
            return "down"
        return "sideways"

    @staticmethod
    def _calc_trend_strength(
        adx: float,
        rsi: float,
        volumes: np.ndarray,
        trend: str,
    ) -> float:
        """计算趋势强度 0-100。

        综合 ADX 值、RSI 位置和量能。

        Args:
            adx: ADX 指标值。
            rsi: RSI(14) 值。
            volumes: 成交量数组。
            trend: 当前趋势方向。

        Returns:
            趋势强度 0-100。
        """
        # ADX 贡献 (0-40): ADX > 25 表示有趋势
        adx_score = min(40.0, adx * 1.6)

        # RSI 贡献 (0-30): 上升趋势中 RSI > 50 加分
        if trend == "up":
            rsi_score = min(30.0, max(0.0, (rsi - 30) / 40 * 30))
        elif trend == "down":
            rsi_score = min(30.0, max(0.0, (70 - rsi) / 40 * 30))
        else:
            rsi_score = 15.0  # 中性

        # 量能贡献 (0-30): 近 5 日平均量 vs 近 20 日平均量
        if len(volumes) >= 20:
            vol_5 = np.mean(volumes[-5:])
            vol_20 = np.mean(volumes[-20:])
            vol_ratio = vol_5 / vol_20 if vol_20 > 0 else 1.0
            vol_score = min(30.0, max(0.0, vol_ratio * 15))
        else:
            vol_score = 15.0

        return round(min(100.0, adx_score + rsi_score + vol_score), 2)

    @staticmethod
    def _assess_momentum(closes: np.ndarray, volumes: np.ndarray) -> str:
        """评估动量状态。

        比较近期涨幅的加速/减速趋势。

        Args:
            closes: 收盘价数组。
            volumes: 成交量数组。

        Returns:
            'accelerating' | 'steady' | 'decelerating'。
        """
        if len(closes) < 20:
            return "steady"

        # 近 5 日涨幅 vs 前 5 日涨幅 (5-10 日前)
        ret_recent = closes[-1] / closes[-5] - 1 if closes[-5] != 0 else 0
        ret_prior = closes[-5] / closes[-10] - 1 if len(closes) >= 10 and closes[-10] != 0 else 0

        # MACD histogram 趋势: 用简化版
        ema12 = _ema(closes, 12)
        ema26 = _ema(closes, 26)
        if ema12 is not None and ema26 is not None:
            dif = ema12[-5:] - ema26[-5:]
            if len(dif) >= 5:
                dif_trend = dif[-1] - dif[0]
                if dif_trend > 0 and ret_recent > ret_prior:
                    return "accelerating"
                elif dif_trend < 0 and ret_recent < ret_prior:
                    return "decelerating"

        return "steady"

    @staticmethod
    def _momentum_to_score(momentum: str) -> float:
        """将动量状态转换为分数。

        Args:
            momentum: 'accelerating' | 'steady' | 'decelerating'。

        Returns:
            分数 0-100。
        """
        mapping = {
            "accelerating": 80.0,
            "steady": 50.0,
            "decelerating": 20.0,
        }
        return mapping.get(momentum, 50.0)

    # ------------------------------------------------------------------
    # Volume confirmation
    # ------------------------------------------------------------------

    @staticmethod
    def _calc_volume_confirmation(
        closes: np.ndarray,
        volumes: np.ndarray,
        trend: str,
    ) -> float:
        """计算量价确认得分 0-100。

        上升趋势中放量上涨缩量回调是健康的；下降趋势相反。

        Args:
            closes: 收盘价数组。
            volumes: 成交量数组。
            trend: 当前趋势方向。

        Returns:
            量价确认得分 0-100。
        """
        if len(closes) < 20 or len(volumes) < 20:
            return 50.0

        # 近 20 天分为上涨日和下跌日
        daily_returns = np.diff(closes[-21:])
        daily_vols = volumes[-20:]

        up_mask = daily_returns > 0
        down_mask = daily_returns < 0

        up_count = np.sum(up_mask)
        down_count = np.sum(down_mask)

        if up_count == 0 or down_count == 0:
            return 50.0

        avg_up_vol = np.mean(daily_vols[up_mask])
        avg_down_vol = np.mean(daily_vols[down_mask])

        if avg_down_vol == 0:
            return 50.0

        vol_ratio = avg_up_vol / avg_down_vol

        if trend == "up":
            # 上涨放量下跌缩量 → 好
            if vol_ratio > 1.3:
                return 85.0
            elif vol_ratio > 1.0:
                return 65.0
            else:
                return 35.0
        elif trend == "down":
            # 下跌放量上涨缩量 → 符合趋势（但得分低因为趋势本身是down）
            if vol_ratio < 0.8:
                return 30.0
            elif vol_ratio < 1.0:
                return 40.0
            else:
                return 55.0  # 下跌缩量可能企稳

        return 50.0

    # ------------------------------------------------------------------
    # Weekly analysis
    # ------------------------------------------------------------------

    def _analyze_weekly(
        self,
        weekly_bars: list[dict[str, Any]],
        stage: int,
        rs_rank: float,
    ) -> TimeframeAnalysis:
        """周线时间框架分析。

        Args:
            weekly_bars: 周线 bar 列表。
            stage: Weinstein Stage。
            rs_rank: RS 排名。

        Returns:
            TimeframeAnalysis 周线分析结果。
        """
        if len(weekly_bars) < 10:
            return TimeframeAnalysis(stage=stage, rs_rank=rs_rank)

        closes = np.array([float(b["close"]) for b in weekly_bars], dtype=np.float64)
        highs = np.array([float(b["high"]) for b in weekly_bars], dtype=np.float64)
        lows = np.array([float(b["low"]) for b in weekly_bars], dtype=np.float64)
        volumes = np.array([float(b["volume"]) for b in weekly_bars], dtype=np.float64)

        ma_alignment = self._calc_ma_alignment(closes)
        trend = self._determine_trend(closes, ma_alignment)
        adx, _, _ = self._calc_adx(highs, lows, closes, period=14)
        rsi = self._calc_rsi(closes, period=14)
        strength = self._calc_trend_strength(adx, rsi, volumes, trend)
        momentum = self._assess_momentum(closes, volumes)

        ma20_w = float(np.mean(closes[-20:])) if len(closes) >= 20 else float(closes[-1])
        high_20w = float(np.max(highs[-20:])) if len(highs) >= 20 else float(highs[-1])
        pattern = self._detect_pattern(weekly_bars, ma20_w, high_20w)
        key_levels = self._find_key_levels(weekly_bars)

        return TimeframeAnalysis(
            trend=trend,
            stage=stage,
            strength=strength,
            ma_alignment=ma_alignment,
            rs_rank=rs_rank,
            momentum=momentum,
            key_levels=key_levels,
            pattern=pattern,
        )

    # ------------------------------------------------------------------
    # Combine timeframes
    # ------------------------------------------------------------------

    @staticmethod
    def _combine_timeframes(
        daily: TimeframeAnalysis,
        weekly: TimeframeAnalysis,
    ) -> float:
        """合并日线和周线分析，返回置信度调整系数。

        规则:
            - 日线周线一致 → 1.0
            - 周线上升 + 日线横盘 → 0.7 (上升趋势中的回调)
            - 周线上升 + 日线下降 → 0.4 (信号冲突)
            - 其他不一致 → 0.5

        Args:
            daily: 日线分析结果。
            weekly: 周线分析结果。

        Returns:
            置信度系数 0-1。
        """
        if daily.trend == weekly.trend:
            return 1.0

        if weekly.trend == "up":
            if daily.trend == "sideways":
                return 0.7
            elif daily.trend == "down":
                return 0.4

        if weekly.trend == "down":
            if daily.trend == "sideways":
                return 0.6
            elif daily.trend == "up":
                return 0.4

        return 0.5

    # ------------------------------------------------------------------
    # Technical indicator calculations
    # ------------------------------------------------------------------

    @staticmethod
    def _calc_rsi(closes: np.ndarray, period: int = 14) -> float:
        """计算 RSI 指标。

        Args:
            closes: 收盘价数组。
            period: RSI 周期，默认 14。

        Returns:
            RSI 值 0-100。
        """
        if len(closes) < period + 1:
            return 50.0

        deltas = np.diff(closes)
        gains = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)

        # Wilder's smoothing (EMA-style)
        avg_gain = np.mean(gains[:period])
        avg_loss = np.mean(losses[:period])

        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period

        if avg_loss == 0:
            return 100.0

        rs = avg_gain / avg_loss
        return round(100.0 - 100.0 / (1.0 + rs), 4)

    @staticmethod
    def _calc_adx(
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        period: int = 14,
    ) -> tuple[float, float, float]:
        """计算 ADX, +DI, -DI。

        Args:
            highs: 最高价数组。
            lows: 最低价数组。
            closes: 收盘价数组。
            period: ADX 周期。

        Returns:
            (adx, plus_di, minus_di) 三元组。
        """
        n = len(closes)
        if n < period + 1:
            return (20.0, 50.0, 50.0)

        # True Range
        tr = np.maximum(
            highs[1:] - lows[1:],
            np.maximum(
                np.abs(highs[1:] - closes[:-1]),
                np.abs(lows[1:] - closes[:-1]),
            ),
        )

        # +DM / -DM
        up_move = highs[1:] - highs[:-1]
        down_move = lows[:-1] - lows[1:]

        plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
        minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

        # Wilder's smoothing
        atr = np.zeros(len(tr))
        plus_dm_smooth = np.zeros(len(tr))
        minus_dm_smooth = np.zeros(len(tr))

        atr[period - 1] = np.mean(tr[:period])
        plus_dm_smooth[period - 1] = np.mean(plus_dm[:period])
        minus_dm_smooth[period - 1] = np.mean(minus_dm[:period])

        for i in range(period, len(tr)):
            atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
            plus_dm_smooth[i] = (
                plus_dm_smooth[i - 1] * (period - 1) + plus_dm[i]
            ) / period
            minus_dm_smooth[i] = (
                minus_dm_smooth[i - 1] * (period - 1) + minus_dm[i]
            ) / period

        # +DI / -DI (use safe division to avoid RuntimeWarning)
        with np.errstate(divide="ignore", invalid="ignore"):
            plus_di_arr = np.where(
                atr > 0, plus_dm_smooth / atr * 100, 0.0
            )
            minus_di_arr = np.where(
                atr > 0, minus_dm_smooth / atr * 100, 0.0
            )

            # DX → ADX
            di_sum = plus_di_arr + minus_di_arr
            dx = np.where(di_sum > 0, np.abs(plus_di_arr - minus_di_arr) / di_sum * 100, 0.0)

        # ADX: Wilder's smoothing of DX
        adx_arr = np.zeros(len(dx))
        start_idx = 2 * period - 1
        if start_idx < len(dx):
            adx_arr[start_idx] = np.mean(dx[period:start_idx + 1])
            for i in range(start_idx + 1, len(dx)):
                adx_arr[i] = (adx_arr[i - 1] * (period - 1) + dx[i]) / period

        adx_val = float(adx_arr[-1]) if len(adx_arr) > 0 else 20.0
        plus_di_val = float(plus_di_arr[-1]) if len(plus_di_arr) > 0 else 50.0
        minus_di_val = float(minus_di_arr[-1]) if len(minus_di_arr) > 0 else 50.0

        return (round(adx_val, 4), round(plus_di_val, 4), round(minus_di_val, 4))

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    @staticmethod
    async def _load_bars(
        symbol: str,
        trade_date: date | None,
        lookback: int = 250,
    ) -> list[dict[str, Any]]:
        """从 market_bars_daily 加载日线数据。

        Args:
            symbol: 股票代码。
            trade_date: 截止日期。
            lookback: 回溯交易日数。

        Returns:
            按 trade_date 升序排列的 bar 字典列表。
        """
        if trade_date is None:
            sql = """
            SELECT trade_date, open, high, low, close, volume, amount
            FROM market_bars_daily
            WHERE symbol = $1
            ORDER BY trade_date DESC
            LIMIT $2
            """
            rows = await db_query(sql, symbol, lookback)
        else:
            sql = """
            SELECT trade_date, open, high, low, close, volume, amount
            FROM market_bars_daily
            WHERE symbol = $1 AND trade_date <= $2
            ORDER BY trade_date DESC
            LIMIT $3
            """
            rows = await db_query(sql, symbol, trade_date, lookback)

        return [dict(r) for r in reversed(rows)]

    @staticmethod
    def _empty_result() -> dict[str, Any]:
        """数据不足时返回空结果。

        Returns:
            默认分析结果字典。
        """
        return {
            "score": 0.0,
            "trend": "unknown",
            "stage": 1,
            "rs_rank": 50.0,
            "confidence_adj": 0.0,
            "key_levels": {"support": [], "resistance": []},
            "pattern": "none",
            "details": {},
        }


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------


def _ema(data: np.ndarray, period: int) -> np.ndarray | None:
    """计算指数移动平均线 (EMA)。

    Args:
        data: 输入数据数组。
        period: EMA 周期。

    Returns:
        EMA 数组，数据不足时返回 None。
    """
    if len(data) < period:
        return None

    multiplier = 2.0 / (period + 1)
    ema = np.zeros(len(data))
    ema[period - 1] = np.mean(data[:period])

    for i in range(period, len(data)):
        ema[i] = (data[i] - ema[i - 1]) * multiplier + ema[i - 1]

    return ema


def _merge_week(bars: list[dict[str, Any]]) -> dict[str, Any]:
    """将同一周的日线 bar 合并为一根周线 bar。

    Args:
        bars: 同一周内的日线 bar 列表（日期升序）。

    Returns:
        周线 bar 字典。
    """
    return {
        "trade_date": bars[-1]["trade_date"],
        "open": bars[0]["open"],
        "high": max(float(b["high"]) for b in bars),
        "low": min(float(b["low"]) for b in bars),
        "close": bars[-1]["close"],
        "volume": sum(float(b["volume"]) for b in bars),
        "amount": sum(float(b["amount"]) for b in bars) if bars[0].get("amount") else 0,
    }
