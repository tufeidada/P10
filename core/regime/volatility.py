"""
波动率评分模块 — 计算市场波动率维度得分 (0-100)。

分数越高表示波动率越大（越危险）。
通过 HV20 历史分位 + 跌停占比两个子维度评估。
"""

from __future__ import annotations

from datetime import date

import numpy as np
import structlog

from db.connection import db_query, db_query_one

logger = structlog.get_logger(__name__)

_HS300_SYMBOL: str = "000300.SH"
_HV_LOOKBACK: int = 270  # 250 交易日 + 20 天计算窗口

# VIX 分档映射表 (vix_level, volatility_score)
_VIX_BREAKPOINTS: list[tuple[float, float]] = [
    (0.0, 0.0),
    (12.0, 20.0),
    (15.0, 35.0),
    (20.0, 50.0),
    (25.0, 65.0),
    (30.0, 80.0),
    (40.0, 100.0),
]


def _vix_to_score(vix: float) -> float:
    """将 VIX 值线性插值映射到 0-100 的波动率得分。

    Args:
        vix: 当前 VIX 收盘值。

    Returns:
        波动率得分 0-100。
    """
    if vix <= _VIX_BREAKPOINTS[0][0]:
        return _VIX_BREAKPOINTS[0][1]
    if vix >= _VIX_BREAKPOINTS[-1][0]:
        return _VIX_BREAKPOINTS[-1][1]

    for i in range(len(_VIX_BREAKPOINTS) - 1):
        x0, y0 = _VIX_BREAKPOINTS[i]
        x1, y1 = _VIX_BREAKPOINTS[i + 1]
        if x0 <= vix <= x1:
            t = (vix - x0) / (x1 - x0)
            return round(y0 + t * (y1 - y0), 2)

    return 50.0


def _compute_hv20(close: np.ndarray) -> np.ndarray:
    """计算 20 日历史波动率序列。

    Args:
        close: 收盘价序列（按时间升序）。

    Returns:
        与 close 等长的 HV20 数组（年化百分比），前 20 个值为 NaN。
    """
    n = len(close)
    hv = np.full(n, np.nan, dtype=np.float64)
    if n < 21:
        return hv

    log_ret = np.log(close[1:] / close[:-1])

    for i in range(20, n):
        window = log_ret[i - 20:i]
        hv[i] = np.std(window, ddof=1) * np.sqrt(252) * 100.0

    return hv


def _hv_percentile_score(hv_series: np.ndarray, lookback: int = 250) -> float:
    """计算最新 HV20 在过去 lookback 天中的分位数。

    Args:
        hv_series: HV20 时间序列。
        lookback: 分位计算回看天数，默认 250。

    Returns:
        分位数 0-100。数据不足时返回 50。
    """
    valid = hv_series[~np.isnan(hv_series)]
    if len(valid) < 30:
        return 50.0

    recent = valid[-lookback:] if len(valid) > lookback else valid
    current = valid[-1]

    percentile = float(np.sum(recent <= current) / len(recent) * 100.0)
    return round(percentile, 2)


async def _compute_volatility_score_us(trade_date: date) -> float:
    """计算 US 市场波动率得分 (VIX-based)。

    子维度:
    - VIX 分位映射得分 (权重 0.6): 按分档线性插值映射到 0-100。
    - VIX 5 日动量代理 (权重 0.4): 若 VIX 5 日涨幅 > 30% 表示短期恐慌加剧，加 10 分。

    Args:
        trade_date: 截止交易日。

    Returns:
        US 波动率得分 0-100。数据不足时返回 50.0。
    """
    rows = await db_query(
        """
        SELECT report_date, value
        FROM macro_indicators
        WHERE indicator_name = 'us_vix' AND market = 'US' AND report_date <= $1
        ORDER BY report_date DESC
        LIMIT 30
        """,
        trade_date,
    )

    if not rows or len(rows) < 5:
        logger.warning(
            "volatility_us_vix_insufficient_data",
            rows_found=len(rows) if rows else 0,
            trade_date=str(trade_date),
        )
        return 50.0

    # 按时间升序
    rows = list(reversed(rows))
    vix_values = np.array([float(r["value"]) for r in rows], dtype=np.float64)

    current_vix = vix_values[-1]

    # --- VIX 插值得分 ---
    hv_score = _vix_to_score(current_vix)

    # --- 5 日动量代理（VIX 快速上升 = 短期恐慌加剧）---
    momentum_add = 0.0
    if len(vix_values) >= 6:
        vix_5d_ago = vix_values[-6]
        if vix_5d_ago > 0:
            vix_5d_change = (current_vix - vix_5d_ago) / vix_5d_ago
            if vix_5d_change > 0.30:
                momentum_add = 10.0
            logger.debug(
                "volatility_us_vix_momentum",
                vix_5d_change=round(vix_5d_change, 4),
                momentum_add=momentum_add,
            )

    # 加权合成: HV-based 60%, momentum proxy 40%
    # momentum_add 体现为在动量子维度上加分
    # 将基础分当作动量分的基准，动量加分直接叠加
    result = hv_score * 0.6 + (hv_score + momentum_add) * 0.4
    result = round(max(0.0, min(100.0, result)), 2)

    logger.info(
        "volatility_score_computed",
        market="US",
        trade_date=str(trade_date),
        score=result,
        current_vix=current_vix,
        hv_score=hv_score,
        momentum_add=momentum_add,
    )
    return result


async def compute_volatility_score(
    market: str = "CN",
    trade_date: date | None = None,
) -> float:
    """计算市场波动率维度得分。

    子维度:
    - HV20 历史分位 (权重 0.6): 沪深300 最近 HV20 在过去 250 天中的分位。
    - 跌停占比 (权重 0.4): 跌停数 / (涨停数 + 跌停数)，放大到 0-100。

    Args:
        market: 市场代码，默认 "CN"。
        trade_date: 截止交易日，默认今天。

    Returns:
        波动率得分 0-100。数据不足时返回 50（中性）。
    """
    if trade_date is None:
        trade_date = date.today()

    if market == "US":
        return await _compute_volatility_score_us(trade_date)

    if market != "CN":
        logger.warning("volatility_unsupported_market", market=market)
        return 50.0

    # --- HV20 分位 ---
    rows = await db_query(
        """
        SELECT trade_date, close
        FROM market_bars_daily
        WHERE symbol = $1 AND trade_date <= $2
        ORDER BY trade_date DESC
        LIMIT $3
        """,
        _HS300_SYMBOL,
        trade_date,
        _HV_LOOKBACK,
    )

    hv_percentile = 50.0
    if rows and len(rows) >= 30:
        rows = list(reversed(rows))
        close = np.array([float(r["close"]) for r in rows], dtype=np.float64)
        hv_series = _compute_hv20(close)
        hv_percentile = _hv_percentile_score(hv_series)
        logger.debug(
            "volatility_hv_computed",
            symbol=_HS300_SYMBOL,
            latest_hv=float(hv_series[~np.isnan(hv_series)][-1]) if np.any(~np.isnan(hv_series)) else None,
            percentile=hv_percentile,
        )
    else:
        logger.warning(
            "volatility_hv_insufficient_data",
            rows_found=len(rows) if rows else 0,
        )

    # --- 跌停占比 ---
    sentiment_row = await db_query_one(
        """
        SELECT limit_up_count, limit_down_count
        FROM market_sentiment_daily
        WHERE trade_date <= $1
        ORDER BY trade_date DESC
        LIMIT 1
        """,
        trade_date,
    )

    limit_down_ratio_score: float | None = None
    if sentiment_row is not None:
        up = sentiment_row["limit_up_count"] or 0
        down = sentiment_row["limit_down_count"] or 0
        total = up + down
        if total > 0:
            # 跌停占比 → 映射到 0-100
            # 正常市场跌停占比约 0-20%，极端恐慌时可能 30-50%
            raw_ratio = down / total
            # 将 0-0.5 的比率映射到 0-100（0.5 以上截断为 100）
            limit_down_ratio_score = round(min(raw_ratio / 0.5 * 100.0, 100.0), 2)
            logger.debug(
                "volatility_limit_down",
                limit_up=up,
                limit_down=down,
                raw_ratio=round(raw_ratio, 4),
                score=limit_down_ratio_score,
            )

    # --- 加权合成 ---
    if limit_down_ratio_score is not None:
        result = hv_percentile * 0.6 + limit_down_ratio_score * 0.4
    else:
        # 情绪数据缺失，仅用 HV20
        result = hv_percentile
        logger.info(
            "volatility_degraded",
            reason="market_sentiment_daily_empty",
            using="hv_only",
        )

    result = round(max(0.0, min(100.0, result)), 2)
    logger.info(
        "volatility_score_computed",
        market=market,
        trade_date=str(trade_date),
        score=result,
        hv_percentile=hv_percentile,
        limit_down_score=limit_down_ratio_score,
    )
    return result
