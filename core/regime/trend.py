"""
趋势评分模块 — 计算市场趋势维度得分 (0-100)。

通过均线排列、ADX 趋势强度、价格结构三个子维度，
综合评估当前市场的趋势状态。
"""

from __future__ import annotations

from datetime import date
from typing import Sequence

import numpy as np
import structlog

from db.connection import db_query

logger = structlog.get_logger(__name__)

# CN 市场代表性指数
_CN_INDEX_SYMBOLS: list[str] = ["000300.SH", "399852.SZ"]

# US 市场代表性指数
_US_INDEX_SYMBOLS: list[str] = ["SPY", "QQQ"]

# 需要多少天历史数据来计算所有指标
_LOOKBACK_DAYS: int = 300  # 足够算 MA200 + 一些余量


def _compute_ma(close: np.ndarray, period: int) -> np.ndarray:
    """计算简单移动平均线。

    Args:
        close: 收盘价序列（按时间升序）。
        period: 均线周期。

    Returns:
        与 close 等长的 MA 数组，前 period-1 个值为 NaN。
    """
    if len(close) < period:
        return np.full_like(close, np.nan, dtype=np.float64)
    kernel = np.ones(period) / period
    ma = np.convolve(close, kernel, mode="full")[:len(close)]
    ma[:period - 1] = np.nan
    return ma


def _compute_adx(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    period: int = 14,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """计算 ADX、+DI、-DI。

    Args:
        high: 最高价序列。
        low: 最低价序列。
        close: 收盘价序列。
        period: ADX 周期，默认 14。

    Returns:
        (adx, plus_di, minus_di) 三个等长数组。
    """
    n = len(close)
    if n < period + 1:
        nan_arr = np.full(n, np.nan, dtype=np.float64)
        return nan_arr, nan_arr, nan_arr

    # True Range
    tr = np.zeros(n, dtype=np.float64)
    plus_dm = np.zeros(n, dtype=np.float64)
    minus_dm = np.zeros(n, dtype=np.float64)

    for i in range(1, n):
        h_l = high[i] - low[i]
        h_pc = abs(high[i] - close[i - 1])
        l_pc = abs(low[i] - close[i - 1])
        tr[i] = max(h_l, h_pc, l_pc)

        up_move = high[i] - high[i - 1]
        down_move = low[i - 1] - low[i]

        plus_dm[i] = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm[i] = down_move if (down_move > up_move and down_move > 0) else 0.0

    # Wilder smoothing
    atr = np.full(n, np.nan, dtype=np.float64)
    smooth_plus = np.full(n, np.nan, dtype=np.float64)
    smooth_minus = np.full(n, np.nan, dtype=np.float64)

    atr[period] = np.sum(tr[1:period + 1])
    smooth_plus[period] = np.sum(plus_dm[1:period + 1])
    smooth_minus[period] = np.sum(minus_dm[1:period + 1])

    for i in range(period + 1, n):
        atr[i] = atr[i - 1] - atr[i - 1] / period + tr[i]
        smooth_plus[i] = smooth_plus[i - 1] - smooth_plus[i - 1] / period + plus_dm[i]
        smooth_minus[i] = smooth_minus[i - 1] - smooth_minus[i - 1] / period + minus_dm[i]

    plus_di = np.full(n, np.nan, dtype=np.float64)
    minus_di = np.full(n, np.nan, dtype=np.float64)
    dx = np.full(n, np.nan, dtype=np.float64)

    valid = ~np.isnan(atr) & (atr > 0)
    plus_di[valid] = 100.0 * smooth_plus[valid] / atr[valid]
    minus_di[valid] = 100.0 * smooth_minus[valid] / atr[valid]

    di_sum = plus_di + minus_di
    di_diff = np.abs(plus_di - minus_di)
    nonzero_sum = valid & (di_sum > 0)
    dx[nonzero_sum] = 100.0 * di_diff[nonzero_sum] / di_sum[nonzero_sum]

    # ADX = Wilder smoothed DX
    adx = np.full(n, np.nan, dtype=np.float64)
    first_adx_idx = period * 2
    if first_adx_idx < n:
        valid_dx = dx[period:first_adx_idx + 1]
        valid_dx = valid_dx[~np.isnan(valid_dx)]
        if len(valid_dx) > 0:
            adx[first_adx_idx] = np.mean(valid_dx)
            for i in range(first_adx_idx + 1, n):
                if not np.isnan(dx[i]) and not np.isnan(adx[i - 1]):
                    adx[i] = (adx[i - 1] * (period - 1) + dx[i]) / period

    return adx, plus_di, minus_di


def _ma_alignment_score(close: np.ndarray) -> float:
    """计算均线排列完整度得分 (0-40)。

    规则:
    - MA5>MA20>MA60>MA150>MA200, 每对正确排列 +8 分 (4 对 = 32 分)
    - 所有 MA 斜率为正 +8 分

    Args:
        close: 收盘价序列（按时间升序，至少 200+ 根）。

    Returns:
        均线排列得分 0-40。
    """
    periods = [5, 20, 60, 150, 200]
    mas: dict[int, np.ndarray] = {}
    for p in periods:
        mas[p] = _compute_ma(close, p)

    # 取最新值
    latest: dict[int, float] = {}
    for p in periods:
        val = mas[p][-1]
        if np.isnan(val):
            return 20.0  # 数据不足，返回中性
        latest[p] = val

    # 均线排列：4 对比较
    pairs = [(5, 20), (20, 60), (60, 150), (150, 200)]
    pair_score = sum(8.0 for short, long in pairs if latest[short] > latest[long])

    # 斜率判断：最近 5 天的 MA 变化
    slope_positive_count = 0
    for p in periods:
        ma_arr = mas[p]
        if len(ma_arr) >= 6 and not np.isnan(ma_arr[-6]):
            if ma_arr[-1] > ma_arr[-6]:
                slope_positive_count += 1

    slope_score = 8.0 if slope_positive_count == len(periods) else 0.0

    return pair_score + slope_score


def _adx_strength_score(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
) -> float:
    """计算 ADX 趋势强度得分 (0-30)。

    规则: ADX > 25 且 +DI > -DI 时，将 ADX 线性映射到 0-30。

    Args:
        high: 最高价序列。
        low: 最低价序列。
        close: 收盘价序列。

    Returns:
        ADX 趋势强度得分 0-30。
    """
    adx, plus_di, minus_di = _compute_adx(high, low, close)

    latest_adx = adx[-1]
    latest_plus = plus_di[-1]
    latest_minus = minus_di[-1]

    if np.isnan(latest_adx):
        return 15.0  # 数据不足，返回中性

    if latest_adx <= 25 or latest_plus <= latest_minus:
        # ADX 弱或下跌趋势 → 线性映射 ADX 0-25 → 0-15
        return min(latest_adx / 25.0 * 15.0, 15.0)

    # ADX > 25 且上涨趋势 → 线性映射 ADX 25-50 → 15-30
    score = 15.0 + (latest_adx - 25.0) / 25.0 * 15.0
    return min(score, 30.0)


def _price_structure_score(high: np.ndarray, low: np.ndarray, window: int = 20) -> float:
    """计算价格结构得分 (0-30)。

    统计最近 window 根 K 线中 higher-high 和 higher-low 的数量。

    Args:
        high: 最高价序列。
        low: 最低价序列。
        window: 回看窗口，默认 20。

    Returns:
        价格结构得分 0-30。
    """
    if len(high) < window + 1:
        return 15.0

    recent_high = high[-window:]
    recent_low = low[-window:]

    hh_count = 0
    hl_count = 0
    total = window - 1

    for i in range(1, window):
        if recent_high[i] > recent_high[i - 1]:
            hh_count += 1
        if recent_low[i] > recent_low[i - 1]:
            hl_count += 1

    if total == 0:
        return 15.0

    ratio = (hh_count + hl_count) / (2 * total)
    return ratio * 30.0


async def _fetch_index_bars(
    symbol: str,
    trade_date: date,
    lookback: int = _LOOKBACK_DAYS,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray] | None:
    """从数据库获取指数的 OHLCV 数据。

    Args:
        symbol: 指数代码。
        trade_date: 截止日期（包含）。
        lookback: 向前取多少个交易日。

    Returns:
        (open, high, low, close) numpy 数组，按时间升序排列。
        数据不足时返回 None。
    """
    rows = await db_query(
        """
        SELECT trade_date, open, high, low, close
        FROM market_bars_daily
        WHERE symbol = $1 AND trade_date <= $2
        ORDER BY trade_date DESC
        LIMIT $3
        """,
        symbol,
        trade_date,
        lookback,
    )

    if not rows or len(rows) < 30:
        logger.warning(
            "trend_insufficient_data",
            symbol=symbol,
            rows_found=len(rows) if rows else 0,
            required=30,
        )
        return None

    # 反转为时间升序
    rows = list(reversed(rows))
    open_ = np.array([float(r["open"]) for r in rows], dtype=np.float64)
    high_ = np.array([float(r["high"]) for r in rows], dtype=np.float64)
    low_ = np.array([float(r["low"]) for r in rows], dtype=np.float64)
    close_ = np.array([float(r["close"]) for r in rows], dtype=np.float64)

    return open_, high_, low_, close_


def _compute_single_index_trend(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
) -> float:
    """为单个指数计算趋势得分 (0-100)。

    Args:
        high: 最高价序列。
        low: 最低价序列。
        close: 收盘价序列。

    Returns:
        趋势综合得分 0-100。
    """
    ma_score = _ma_alignment_score(close)             # 0-40
    adx_score = _adx_strength_score(high, low, close)  # 0-30
    ps_score = _price_structure_score(high, low)        # 0-30

    total = ma_score + adx_score + ps_score
    return round(max(0.0, min(100.0, total)), 2)


async def compute_trend_score(
    market: str = "CN",
    trade_date: date | None = None,
) -> float:
    """计算市场趋势维度得分。

    对于 CN 市场，取沪深300(000300.SH)和中证1000(399852.SZ)的均值。

    Args:
        market: 市场代码，默认 "CN"。
        trade_date: 截止交易日，默认今天。

    Returns:
        趋势得分 0-100。数据不足时返回 50（中性）。
    """
    if trade_date is None:
        trade_date = date.today()

    if market == "CN":
        symbols = _CN_INDEX_SYMBOLS
    elif market == "US":
        symbols = _US_INDEX_SYMBOLS
    else:
        logger.warning("trend_unsupported_market", market=market)
        return 50.0

    scores: list[float] = []
    for symbol in symbols:
        data = await _fetch_index_bars(symbol, trade_date)
        if data is None:
            logger.warning("trend_skip_index", symbol=symbol, reason="insufficient_data")
            continue

        _, high, low, close = data
        score = _compute_single_index_trend(high, low, close)
        scores.append(score)
        logger.debug(
            "trend_index_score",
            symbol=symbol,
            score=score,
            trade_date=str(trade_date),
        )

    if not scores:
        logger.warning("trend_no_valid_index", market=market, trade_date=str(trade_date))
        return 50.0

    result = round(float(np.mean(scores)), 2)
    logger.info("trend_score_computed", market=market, trade_date=str(trade_date), score=result)
    return result
