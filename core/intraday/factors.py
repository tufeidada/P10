"""盘中因子计算 — 10个ATR归一化因子。

用于从 15 分钟 K 线 + 实时盘口数据中提取标准化盘中因子，
供盘中信号检测模块（core/intraday/signal_detector.py）调用。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import structlog

from db.connection import db_query_one, db_query_val

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class IntradayFactors:
    """盘中 10 因子计算结果（全部 ATR 归一化或有界比例）。

    Attributes:
        symbol: 股票代码，如 '600519.SH'。
        calc_time: 因子计算时刻。
        vwap_deviation: (close - VWAP_today) / ATR_daily。负值=低于VWAP。
        momentum_15m: 最新一根 15m bar 涨跌幅 / ATR_daily。
        momentum_1h: 最近 4 根 15m bar 合计涨跌幅 / ATR_daily。
        volume_ratio_15m: 当前 bar 成交量 / 最近 5 根历史 bar 均量。
        bid_ask_imbalance: (s_vol - b_vol)/(s_vol + b_vol)，+1=纯卖，-1=纯买。
        price_vs_day_range: (close - 日内最低) / (日内最高 - 日内最低)，[0,1]。
        support_distance: (close - 最近支撑位) / ATR_daily。
        resistance_distance: (最近阻力位 - close) / ATR_daily。
        rsi_15m: RSI(14) on 15min bars，Wilder 平滑法，[0,100]。
        macd_cross_15m: 'golden' | 'dead' | 'none'，最近两根 bar 是否发生交叉。
    """

    symbol: str
    calc_time: datetime
    vwap_deviation: float | None
    momentum_15m: float | None
    momentum_1h: float | None
    volume_ratio_15m: float | None
    bid_ask_imbalance: float | None
    price_vs_day_range: float | None
    support_distance: float | None
    resistance_distance: float | None
    rsi_15m: float | None
    macd_cross_15m: str | None


# ---------------------------------------------------------------------------
# 纯函数计算工具
# ---------------------------------------------------------------------------


def _ema_series(values: list[float], period: int) -> list[float]:
    """计算 EMA 序列（标准指数平滑）。

    Args:
        values: 原始数值序列（升序，index 0 最旧）。
        period: EMA 周期。

    Returns:
        与输入等长的 EMA 列表；前 period-1 个元素填 NaN，第 period-1 个用 SMA 初始化。
    """
    if not values or period <= 0:
        return []
    k = 2.0 / (period + 1)
    ema = [float("nan")] * len(values)
    start = period - 1
    if start >= len(values):
        return ema
    ema[start] = sum(values[:period]) / period
    for i in range(start + 1, len(values)):
        ema[i] = values[i] * k + ema[i - 1] * (1 - k)
    return ema


def _rsi_wilder(closes: list[float], period: int = 14) -> float | None:
    """计算最新一根 RSI，使用 Wilder 平滑法。

    Args:
        closes: 收盘价序列（升序，index 0 最旧），至少需要 period+1 个值。
        period: RSI 周期，默认 14。

    Returns:
        RSI 值 [0, 100]，数据不足时返回 None。
    """
    if len(closes) < period + 1:
        return None

    # 只取末尾 period*3 个数据，避免超长序列性能问题
    closes = closes[-(period * 3):]

    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))

    if len(gains) < period:
        return None

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - 100.0 / (1.0 + rs), 4)


def _macd_cross(
    closes: list[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> str:
    """检测最近两根 bar 是否发生 MACD 金叉/死叉。

    Args:
        closes: 15m 收盘价序列（升序），至少需要 slow+signal+2 个值。
        fast: 快线 EMA 周期，默认 12。
        slow: 慢线 EMA 周期，默认 26。
        signal: 信号线 EMA 周期，默认 9。

    Returns:
        'golden' — 前一根 DIF < DEA，最新 DIF > DEA（金叉）。
        'dead'   — 前一根 DIF > DEA，最新 DIF < DEA（死叉）。
        'none'   — 无交叉或数据不足。
    """
    min_len = slow + signal + 2
    if len(closes) < min_len:
        return "none"

    fast_ema = _ema_series(closes, fast)
    slow_ema = _ema_series(closes, slow)

    # DIF = EMA(fast) - EMA(slow)，从 slow-1 起有效
    dif: list[float] = []
    for i in range(slow - 1, len(closes)):
        fe = fast_ema[i]
        se = slow_ema[i]
        if math.isnan(fe) or math.isnan(se):
            dif.append(float("nan"))
        else:
            dif.append(fe - se)

    valid_dif = [d for d in dif if not math.isnan(d)]
    if len(valid_dif) < signal + 2:
        return "none"

    dea_series = _ema_series(valid_dif, signal)
    prev_dif = valid_dif[-2]
    curr_dif = valid_dif[-1]
    prev_dea = dea_series[-2]
    curr_dea = dea_series[-1]

    if math.isnan(prev_dea) or math.isnan(curr_dea):
        return "none"

    if prev_dif <= prev_dea and curr_dif > curr_dea:
        return "golden"
    if prev_dif >= prev_dea and curr_dif < curr_dea:
        return "dead"
    return "none"


# ---------------------------------------------------------------------------
# 主计算类
# ---------------------------------------------------------------------------


class FactorCalculator:
    """盘中 10 因子计算器。

    从 15m K 线数据和实时盘口计算全部盘中因子，所有价格类因子均除以
    ATR_daily 进行归一化，使因子具有跨股票可比性。
    """

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    async def compute(
        self,
        symbol: str,
        bars: list[dict[str, Any]],
        quote: dict[str, Any] | None = None,
    ) -> IntradayFactors:
        """计算所有 10 个盘中因子。

        Args:
            symbol: 股票代码，如 '600519.SH'。
            bars: 15m K 线列表（升序，最新在末尾），来自 intraday_bars 表。
                  每个 dict 至少含: bar_time, open, high, low, close, volume, amount, vwap。
            quote: 实时盘口 dict（可选），含 price, open, high, low, vol, amount,
                   s_vol, b_vol 等字段。

        Returns:
            IntradayFactors 实例，计算失败的单个因子设为 None。
        """
        log = logger.bind(symbol=symbol, module="factors")

        if not bars:
            log.warning("compute_no_bars")
            return IntradayFactors(
                symbol=symbol,
                calc_time=datetime.now(),
                vwap_deviation=None,
                momentum_15m=None,
                momentum_1h=None,
                volume_ratio_15m=None,
                bid_ask_imbalance=None,
                price_vs_day_range=None,
                support_distance=None,
                resistance_distance=None,
                rsi_15m=None,
                macd_cross_15m=None,
            )

        # 加载 ATR_daily
        atr = await self._load_atr_daily(symbol)
        log.debug("atr_loaded", atr=atr)

        # 当前价：优先用盘口 price，否则用最新 bar close
        last_bar = bars[-1]
        current_price: float = (
            float(quote["price"])
            if quote and quote.get("price")
            else float(last_bar["close"])
        )

        closes = [float(b["close"]) for b in bars]

        # 逐因子计算（各自独立，单因子失败不影响其他）
        vwap_deviation = self._calc_vwap_deviation(bars, current_price, atr)
        momentum_15m = self._calc_momentum_15m(bars, atr)
        momentum_1h = self._calc_momentum_1h(bars, atr)
        volume_ratio_15m = self._calc_volume_ratio(bars)
        bid_ask_imbalance = self._calc_bid_ask_imbalance(quote)
        price_vs_day_range = self._calc_price_vs_day_range(bars, quote, current_price)
        support_distance, resistance_distance = await self._calc_level_distances(
            symbol, current_price, atr, log
        )
        rsi_15m = self._calc_rsi(closes)
        macd_cross_15m = self._calc_macd_cross(closes)

        factors = IntradayFactors(
            symbol=symbol,
            calc_time=datetime.now(),
            vwap_deviation=vwap_deviation,
            momentum_15m=momentum_15m,
            momentum_1h=momentum_1h,
            volume_ratio_15m=volume_ratio_15m,
            bid_ask_imbalance=bid_ask_imbalance,
            price_vs_day_range=price_vs_day_range,
            support_distance=support_distance,
            resistance_distance=resistance_distance,
            rsi_15m=rsi_15m,
            macd_cross_15m=macd_cross_15m,
        )
        log.debug(
            "factors_computed",
            **{k: v for k, v in vars(factors).items() if k not in ("symbol", "calc_time")},
        )
        return factors

    # ------------------------------------------------------------------
    # ATR 加载
    # ------------------------------------------------------------------

    async def _load_atr_daily(self, symbol: str) -> float | None:
        """从 features_daily 加载最新 ATR_14，或回退到原始日线计算。

        优先读取 features_daily.atr_14（预计算值）；若为空则从
        market_bars_daily 最近 30 个交易日手工计算 Wilder ATR(14)。

        Args:
            symbol: 股票代码。

        Returns:
            ATR 值（元），数据缺失时返回 None。
        """
        try:
            atr_val = await db_query_val(
                """
                SELECT atr_14
                FROM   features_daily
                WHERE  symbol = $1
                  AND  atr_14 IS NOT NULL
                ORDER  BY trade_date DESC
                LIMIT  1
                """,
                symbol,
            )
            if atr_val is not None and float(atr_val) > 0:
                return float(atr_val)

            # 回退：从原始日线计算 ATR
            row = await db_query_one(
                """
                SELECT array_agg(high  ORDER BY trade_date ASC) AS highs,
                       array_agg(low   ORDER BY trade_date ASC) AS lows,
                       array_agg(close ORDER BY trade_date ASC) AS closes
                FROM (
                    SELECT trade_date, high, low, close
                    FROM   market_bars_daily
                    WHERE  symbol = $1
                    ORDER  BY trade_date DESC
                    LIMIT  30
                ) sub
                """,
                symbol,
            )
            if not row or not row["closes"]:
                return None

            highs = [float(v) for v in row["highs"]]
            lows = [float(v) for v in row["lows"]]
            closes_d = [float(v) for v in row["closes"]]

            if len(closes_d) < 15:
                return None

            trs: list[float] = []
            for i in range(1, len(closes_d)):
                tr = max(
                    highs[i] - lows[i],
                    abs(highs[i] - closes_d[i - 1]),
                    abs(lows[i] - closes_d[i - 1]),
                )
                trs.append(tr)

            period = 14
            if len(trs) < period:
                return None

            # Wilder 平滑
            atr_calc = sum(trs[:period]) / period
            for tr in trs[period:]:
                atr_calc = (atr_calc * (period - 1) + tr) / period

            return round(atr_calc, 4) if atr_calc > 0 else None

        except Exception as exc:
            logger.warning("atr_load_error", symbol=symbol, error=str(exc))
            return None

    # ------------------------------------------------------------------
    # 各因子计算
    # ------------------------------------------------------------------

    @staticmethod
    def _calc_vwap_deviation(
        bars: list[dict[str, Any]],
        current_price: float,
        atr: float | None,
    ) -> float | None:
        """计算 (close - VWAP_today) / ATR_daily。

        VWAP 取当日所有 bars 的累计成交额 / 累计成交量。

        Args:
            bars: 15m K 线列表（升序）。
            current_price: 当前价格。
            atr: 日 ATR，为 None 或 0 时返回 None。

        Returns:
            ATR 归一化后的 VWAP 偏差，正值=高于VWAP，负值=低于VWAP。
        """
        if not atr or atr <= 0:
            return None
        try:
            today = bars[-1]["bar_time"].date()
            today_bars = [b for b in bars if b["bar_time"].date() == today]
            if not today_bars:
                return None

            cum_amount = sum(float(b["amount"]) for b in today_bars)
            cum_volume = sum(int(b["volume"]) for b in today_bars)
            if cum_volume <= 0:
                return None

            vwap = cum_amount / cum_volume
            return round((current_price - vwap) / atr, 4)
        except Exception as exc:
            logger.warning("vwap_deviation_error", error=str(exc))
            return None

    @staticmethod
    def _calc_momentum_15m(
        bars: list[dict[str, Any]],
        atr: float | None,
    ) -> float | None:
        """计算最新一根 15m bar 收盘价变动 / ATR_daily。

        Args:
            bars: 15m K 线列表（升序，最新在末尾），至少 2 根。
            atr: 日 ATR。

        Returns:
            单根 bar 动量（ATR 归一化）。
        """
        if not atr or atr <= 0 or len(bars) < 2:
            return None
        try:
            ret = float(bars[-1]["close"]) - float(bars[-2]["close"])
            return round(ret / atr, 4)
        except Exception as exc:
            logger.warning("momentum_15m_error", error=str(exc))
            return None

    @staticmethod
    def _calc_momentum_1h(
        bars: list[dict[str, Any]],
        atr: float | None,
    ) -> float | None:
        """计算最近 4 根 15m bar（约 1 小时）的价格变动 / ATR_daily。

        Args:
            bars: 15m K 线列表（升序，最新在末尾），至少 5 根。
            atr: 日 ATR。

        Returns:
            1 小时动量（ATR 归一化）。
        """
        if not atr or atr <= 0 or len(bars) < 5:
            return None
        try:
            ret = float(bars[-1]["close"]) - float(bars[-5]["close"])
            return round(ret / atr, 4)
        except Exception as exc:
            logger.warning("momentum_1h_error", error=str(exc))
            return None

    @staticmethod
    def _calc_volume_ratio(bars: list[dict[str, Any]]) -> float | None:
        """计算当前 bar 成交量与最近 5 根历史 bar 均量之比（简化量比）。

        简化实现：取倒数第 2~6 根 bar 的均量作为参照均量。

        Args:
            bars: 15m K 线列表（升序，最新在末尾），至少 6 根。

        Returns:
            成交量比率（>1 表示放量，<1 表示缩量），参照量为 0 时返回 None。
        """
        if len(bars) < 6:
            return None
        try:
            current_vol = int(bars[-1]["volume"])
            ref_vols = [int(b["volume"]) for b in bars[-6:-1]]
            avg_ref = sum(ref_vols) / len(ref_vols)
            if avg_ref <= 0:
                return None
            return round(current_vol / avg_ref, 4)
        except Exception as exc:
            logger.warning("volume_ratio_error", error=str(exc))
            return None

    @staticmethod
    def _calc_bid_ask_imbalance(
        quote: dict[str, Any] | None,
    ) -> float | None:
        """计算买卖盘口失衡度 (s_vol - b_vol) / (s_vol + b_vol)。

        Args:
            quote: 实时盘口 dict，含 s_vol（委托卖量）和 b_vol（委托买量）字段。

        Returns:
            [-1, +1] 范围内的失衡度，+1=纯卖压，-1=纯买盘；无盘口数据返回 None。
        """
        if not quote:
            return None
        try:
            s_vol = float(quote.get("s_vol") or 0)
            b_vol = float(quote.get("b_vol") or 0)
            total = s_vol + b_vol
            if total <= 0:
                return None
            return round((s_vol - b_vol) / total, 4)
        except Exception as exc:
            logger.warning("bid_ask_imbalance_error", error=str(exc))
            return None

    @staticmethod
    def _calc_price_vs_day_range(
        bars: list[dict[str, Any]],
        quote: dict[str, Any] | None,
        current_price: float,
    ) -> float | None:
        """计算当前价在日内高低区间的相对位置。

        结果 = (close - day_low) / (day_high - day_low)，范围 [0, 1]。
        0 = 日内最低，1 = 日内最高。

        Args:
            bars: 15m K 线列表，用于从今日 bars 推算日内高低（盘口缺失时兜底）。
            quote: 实时盘口（优先使用其 high/low 字段）。
            current_price: 当前价格。

        Returns:
            [0, 1] 范围内的位置，区间为 0 时返回 None。
        """
        try:
            if quote and quote.get("high") and quote.get("low"):
                day_high = float(quote["high"])
                day_low = float(quote["low"])
            else:
                today = bars[-1]["bar_time"].date()
                today_bars = [b for b in bars if b["bar_time"].date() == today]
                if not today_bars:
                    return None
                day_high = max(float(b["high"]) for b in today_bars)
                day_low = min(float(b["low"]) for b in today_bars)

            rng = day_high - day_low
            if rng <= 0:
                return None
            return round((current_price - day_low) / rng, 4)
        except Exception as exc:
            logger.warning("price_vs_day_range_error", error=str(exc))
            return None

    async def _calc_level_distances(
        self,
        symbol: str,
        current_price: float,
        atr: float | None,
        log: Any,
    ) -> tuple[float | None, float | None]:
        """计算距最近支撑/阻力位的 ATR 归一化距离。

        数据来源优先级：
        1. judgments 表最新记录的 signal_sources->technical->key_levels
        2. features_daily.extra->key_levels（无 judgments 时兜底）

        Args:
            symbol: 股票代码。
            current_price: 当前价格。
            atr: 日 ATR，为 None 或 0 时两项均返回 None。
            log: 绑定了上下文信息的 structlog logger 实例。

        Returns:
            (support_distance, resistance_distance) 元组，单位为 ATR 倍数。
            support_distance = (close - 最近支撑) / ATR，正值=价格在支撑上方。
            resistance_distance = (最近阻力 - close) / ATR，正值=价格在阻力下方。
        """
        if not atr or atr <= 0:
            return None, None

        supports: list[float] = []
        resistances: list[float] = []

        try:
            # 方式 1：从最新 judgment 的 signal_sources 读取
            row = await db_query_one(
                """
                SELECT signal_sources
                FROM   judgments
                WHERE  symbol = $1
                  AND  signal_sources IS NOT NULL
                ORDER  BY judgment_date DESC
                LIMIT  1
                """,
                symbol,
            )
            if row and row["signal_sources"]:
                ss = row["signal_sources"]
                technical: dict = ss.get("technical") or {}
                kl: dict = technical.get("key_levels") or {}
                supports = [float(v) for v in (kl.get("support") or []) if v]
                resistances = [float(v) for v in (kl.get("resistance") or []) if v]

            # 方式 2：从 features_daily.extra 读取（兜底）
            if not supports and not resistances:
                extra_row = await db_query_one(
                    """
                    SELECT extra
                    FROM   features_daily
                    WHERE  symbol = $1
                      AND  extra IS NOT NULL
                    ORDER  BY trade_date DESC
                    LIMIT  1
                    """,
                    symbol,
                )
                if extra_row and extra_row["extra"]:
                    kl = extra_row["extra"].get("key_levels") or {}
                    supports = [float(v) for v in (kl.get("support") or []) if v]
                    resistances = [float(v) for v in (kl.get("resistance") or []) if v]

        except Exception as exc:
            log.warning("level_load_error", error=str(exc))
            return None, None

        support_dist: float | None = None
        resistance_dist: float | None = None

        # 最近支撑：当前价以下最高的支撑位
        below = [s for s in supports if s <= current_price]
        if below:
            nearest_support = max(below)
            support_dist = round((current_price - nearest_support) / atr, 4)

        # 最近阻力：当前价以上最低的阻力位
        above = [r for r in resistances if r >= current_price]
        if above:
            nearest_resistance = min(above)
            resistance_dist = round((nearest_resistance - current_price) / atr, 4)

        return support_dist, resistance_dist

    @staticmethod
    def _calc_rsi(closes: list[float]) -> float | None:
        """计算基于 15m 收盘价序列的 RSI(14)，使用 Wilder 平滑法。

        Args:
            closes: 收盘价列表（升序），至少 15 个值。

        Returns:
            RSI 值 [0, 100]，数据不足时返回 None。
        """
        try:
            return _rsi_wilder(closes, period=14)
        except Exception as exc:
            logger.warning("rsi_calc_error", error=str(exc))
            return None

    @staticmethod
    def _calc_macd_cross(closes: list[float]) -> str | None:
        """检测 15m K 线上 MACD 是否在最近两根 bar 发生金叉/死叉。

        Args:
            closes: 15m 收盘价列表（升序）。

        Returns:
            'golden' | 'dead' | 'none'；数据不足时返回 None。
        """
        try:
            return _macd_cross(closes)
        except Exception as exc:
            logger.warning("macd_cross_error", error=str(exc))
            return None
