"""
流动性评分模块 — 计算市场流动性维度得分 (0-100)。

通过北向资金、融资余额、市场成交量三个子维度，
评估市场资金面状态。分数越高，流动性越好。
"""

from __future__ import annotations

from datetime import date

import numpy as np
import structlog

from db.connection import db_query

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# US 市场流动性子维度
# ---------------------------------------------------------------------------


async def _us_credit_spread_score(trade_date: date) -> float | None:
    """HYG/TLT 比率 20 日趋势作为信用利差代理得分 (0-100)，权重 50%。

    HYG 相对 TLT 下跌 → 信用利差扩大 → 流动性收紧 → 得分低。

    Args:
        trade_date: 截止交易日。

    Returns:
        得分 0-100，数据不足时返回 None。
    """
    hyg_rows = await db_query(
        """
        SELECT report_date, value
        FROM macro_indicators
        WHERE indicator_name = 'us_hyg' AND market = 'US' AND report_date <= $1
        ORDER BY report_date DESC
        LIMIT 25
        """,
        trade_date,
    )

    tlt_rows = await db_query(
        """
        SELECT report_date, value
        FROM macro_indicators
        WHERE indicator_name = 'us_tlt' AND market = 'US' AND report_date <= $1
        ORDER BY report_date DESC
        LIMIT 25
        """,
        trade_date,
    )

    if not hyg_rows or not tlt_rows or len(hyg_rows) < 5 or len(tlt_rows) < 5:
        return None

    hyg_map = {r["report_date"]: float(r["value"]) for r in hyg_rows}
    tlt_map = {r["report_date"]: float(r["value"]) for r in tlt_rows}

    common_dates = sorted(set(hyg_map.keys()) & set(tlt_map.keys()))
    if len(common_dates) < 5:
        return None

    ratios = np.array([hyg_map[d] / tlt_map[d] for d in common_dates], dtype=np.float64)
    score = _trend_score_from_series(ratios)

    logger.debug(
        "liquidity_us_credit_spread",
        score=score,
        common_days=len(common_dates),
        latest_ratio=round(float(ratios[-1]), 4) if len(ratios) > 0 else None,
    )
    return score


async def _us_spy_volume_score(trade_date: date) -> float | None:
    """SPY 成交量 vs 20 日均量得分 (0-100)，权重 25%。

    成交量高于均量 → 流动性活跃 → 得分高。

    Args:
        trade_date: 截止交易日。

    Returns:
        得分 0-100，数据不足时返回 None。
    """
    rows = await db_query(
        """
        SELECT trade_date, volume
        FROM market_bars_daily
        WHERE symbol = 'SPY' AND trade_date <= $1
        ORDER BY trade_date DESC
        LIMIT 25
        """,
        trade_date,
    )

    if not rows or len(rows) < 5:
        return None

    rows = list(reversed(rows))
    volumes = np.array([float(r["volume"]) for r in rows], dtype=np.float64)

    current = volumes[-1]
    avg_20 = np.mean(volumes[-21:-1]) if len(volumes) > 20 else np.mean(volumes[:-1])

    if avg_20 == 0:
        return 50.0

    ratio = current / avg_20
    # ratio: 0.5 → 冷清(0), 1.0 → 正常(50), 2.0 → 活跃(100)
    if ratio <= 0.5:
        score = 0.0
    elif ratio >= 2.0:
        score = 100.0
    elif ratio <= 1.0:
        score = (ratio - 0.5) / 0.5 * 50.0
    else:
        score = 50.0 + (ratio - 1.0) / 1.0 * 50.0

    logger.debug(
        "liquidity_us_spy_volume",
        current_volume=float(current),
        avg_20=float(avg_20),
        ratio=round(ratio, 4),
        score=round(score, 2),
    )
    return round(score, 2)


async def _us_vix_tlt_score(trade_date: date) -> float | None:
    """VIX 低位 + TLT 稳定/上升作为利差代理得分 (0-100)，权重 25%。

    VIX < 20 且 TLT 近期稳定或上升 → 流动性宽松 → 得分高。

    Args:
        trade_date: 截止交易日。

    Returns:
        得分 0-100，数据不足时返回 None。
    """
    vix_rows = await db_query(
        """
        SELECT report_date, value
        FROM macro_indicators
        WHERE indicator_name = 'us_vix' AND market = 'US' AND report_date <= $1
        ORDER BY report_date DESC
        LIMIT 5
        """,
        trade_date,
    )

    tlt_rows = await db_query(
        """
        SELECT report_date, value
        FROM macro_indicators
        WHERE indicator_name = 'us_tlt' AND market = 'US' AND report_date <= $1
        ORDER BY report_date DESC
        LIMIT 10
        """,
        trade_date,
    )

    if not vix_rows:
        return None

    current_vix = float(vix_rows[0]["value"])

    # VIX 分数: < 20 → 高分, > 30 → 低分
    if current_vix < 20:
        vix_score = 75.0 + (20.0 - current_vix) / 20.0 * 25.0  # 75-100
    elif current_vix <= 30:
        vix_score = 50.0 - (current_vix - 20.0) / 10.0 * 25.0  # 25-50
    else:
        vix_score = max(0.0, 25.0 - (current_vix - 30.0) / 10.0 * 25.0)  # 0-25
    vix_score = max(0.0, min(100.0, vix_score))

    # TLT 趋势分数
    tlt_score = 50.0
    if tlt_rows and len(tlt_rows) >= 5:
        tlt_values = np.array([float(r["value"]) for r in reversed(tlt_rows)], dtype=np.float64)
        tlt_score = _trend_score_from_series(tlt_values)

    combined = (vix_score + tlt_score) / 2.0
    combined = round(max(0.0, min(100.0, combined)), 2)

    logger.debug(
        "liquidity_us_vix_tlt",
        current_vix=current_vix,
        vix_score=round(vix_score, 2),
        tlt_score=round(tlt_score, 2),
        combined=combined,
    )
    return combined


async def _compute_liquidity_score_us(trade_date: date) -> float:
    """计算 US 市场流动性得分。

    三个子维度:
    - HYG/TLT 信用利差趋势 (权重 50%)
    - SPY 成交量 vs 20 日均量 (权重 25%)
    - VIX 低位 + TLT 稳定性 (权重 25%)

    数据缺失时在可用维度间重新分配权重。

    Args:
        trade_date: 截止交易日。

    Returns:
        流动性得分 0-100。全部数据缺失时返回 50.0。
    """
    cs_score = await _us_credit_spread_score(trade_date)
    vol_score = await _us_spy_volume_score(trade_date)
    vix_tlt_score = await _us_vix_tlt_score(trade_date)

    components: list[tuple[str, float, float]] = []
    if cs_score is not None:
        components.append(("credit_spread", cs_score, 0.50))
    if vol_score is not None:
        components.append(("spy_volume", vol_score, 0.25))
    if vix_tlt_score is not None:
        components.append(("vix_tlt", vix_tlt_score, 0.25))

    if not components:
        logger.warning("liquidity_us_no_data", trade_date=str(trade_date))
        return 50.0

    total_weight = sum(w for _, _, w in components)
    result = sum(score * (w / total_weight) for _, score, w in components)
    result = round(max(0.0, min(100.0, result)), 2)

    logger.info(
        "liquidity_score_computed",
        market="US",
        trade_date=str(trade_date),
        score=result,
        components={name: val for name, val, _ in components},
        available_dimensions=len(components),
    )
    return result


def _trend_score_from_series(values: np.ndarray) -> float:
    """根据时间序列计算趋势得分 (0-100)。

    使用线性回归斜率 + 近期 vs 远期均值 综合判断趋势方向和强度。

    Args:
        values: 时间序列数组（按时间升序），至少 5 个非 NaN 值。

    Returns:
        趋势得分 0-100。50 为中性。
    """
    valid = values[~np.isnan(values)]
    if len(valid) < 5:
        return 50.0

    # 线性回归斜率（归一化）
    x = np.arange(len(valid), dtype=np.float64)
    mean_x = np.mean(x)
    mean_y = np.mean(valid)
    denom = np.sum((x - mean_x) ** 2)
    if denom == 0:
        return 50.0
    slope = np.sum((x - mean_x) * (valid - mean_y)) / denom

    # 归一化斜率: 用序列标准差衡量
    std = np.std(valid)
    if std == 0:
        return 50.0
    normalized_slope = slope / std * len(valid)

    # 近期 vs 远期: 后半段均值 vs 前半段均值
    mid = len(valid) // 2
    recent_mean = np.mean(valid[mid:])
    early_mean = np.mean(valid[:mid])
    if early_mean != 0:
        momentum = (recent_mean - early_mean) / abs(early_mean)
    else:
        momentum = 0.0

    # 综合: 斜率 60% + 动量 40%
    # normalized_slope 典型范围 -3 ~ +3 → 映射到 0-100
    slope_score = 50.0 + normalized_slope / 3.0 * 50.0
    slope_score = max(0.0, min(100.0, slope_score))

    # momentum 典型范围 -0.2 ~ +0.2 → 映射到 0-100
    momentum_score = 50.0 + momentum / 0.2 * 50.0
    momentum_score = max(0.0, min(100.0, momentum_score))

    return round(slope_score * 0.6 + momentum_score * 0.4, 2)


async def _northbound_score(trade_date: date) -> float | None:
    """计算北向资金 20 日净流入趋势得分。

    Args:
        trade_date: 截止交易日。

    Returns:
        得分 0-100，数据不足时返回 None。
    """
    rows = await db_query(
        """
        SELECT trade_date, total_net_buy
        FROM northbound_daily
        WHERE trade_date <= $1
        ORDER BY trade_date DESC
        LIMIT 20
        """,
        trade_date,
    )

    if not rows or len(rows) < 5:
        return None

    # 反转为时间升序
    rows = list(reversed(rows))
    values = np.array(
        [float(r["total_net_buy"]) for r in rows if r["total_net_buy"] is not None],
        dtype=np.float64,
    )

    if len(values) < 5:
        return None

    score = _trend_score_from_series(values)
    logger.debug("liquidity_northbound", score=score, days=len(values))
    return score


async def _margin_score(trade_date: date) -> float | None:
    """计算融资余额 20 日变化趋势得分。

    优先从 market_sentiment_daily 的 margin_balance 读取，
    降级用 margin_daily 汇总。

    Args:
        trade_date: 截止交易日。

    Returns:
        得分 0-100，数据不足时返回 None。
    """
    # 方案1: 从 market_sentiment_daily 读取
    rows = await db_query(
        """
        SELECT trade_date, margin_balance
        FROM market_sentiment_daily
        WHERE trade_date <= $1 AND margin_balance IS NOT NULL
        ORDER BY trade_date DESC
        LIMIT 20
        """,
        trade_date,
    )

    if rows and len(rows) >= 5:
        rows = list(reversed(rows))
        values = np.array(
            [float(r["margin_balance"]) for r in rows],
            dtype=np.float64,
        )
        score = _trend_score_from_series(values)
        logger.debug("liquidity_margin_from_sentiment", score=score, days=len(values))
        return score

    # 方案2: 从 margin_daily 汇总（全市场融资余额）
    rows = await db_query(
        """
        SELECT trade_date, SUM(rzye) AS total_rzye
        FROM margin_daily
        WHERE trade_date <= $1
        GROUP BY trade_date
        ORDER BY trade_date DESC
        LIMIT 20
        """,
        trade_date,
    )

    if not rows or len(rows) < 5:
        return None

    rows = list(reversed(rows))
    values = np.array(
        [float(r["total_rzye"]) for r in rows if r["total_rzye"] is not None],
        dtype=np.float64,
    )

    if len(values) < 5:
        return None

    score = _trend_score_from_series(values)
    logger.debug("liquidity_margin_from_margin_daily", score=score, days=len(values))
    return score


async def _turnover_score(trade_date: date, market: str = "CN") -> float | None:
    """计算市场成交量 vs 20 日均量得分。

    Args:
        trade_date: 截止交易日。
        market: 市场代码。

    Returns:
        得分 0-100，数据不足时返回 None。
    """
    rows = await db_query(
        """
        SELECT trade_date, SUM(amount) AS total_amount
        FROM market_bars_daily
        WHERE market = $1 AND trade_date <= $2
        GROUP BY trade_date
        ORDER BY trade_date DESC
        LIMIT 25
        """,
        market,
        trade_date,
    )

    if not rows or len(rows) < 5:
        return None

    rows = list(reversed(rows))
    amounts = np.array(
        [float(r["total_amount"]) for r in rows if r["total_amount"] is not None],
        dtype=np.float64,
    )

    if len(amounts) < 5:
        return None

    # 最近一天 vs 20 日均量
    current = amounts[-1]
    avg_20 = np.mean(amounts[-21:-1]) if len(amounts) > 20 else np.mean(amounts[:-1])

    if avg_20 == 0:
        return 50.0

    ratio = current / avg_20
    # ratio: 0.5 → 很冷清, 1.0 → 正常, 2.0 → 很活跃
    # 映射: 0.5 → 0, 1.0 → 50, 2.0 → 100
    if ratio <= 0.5:
        score = 0.0
    elif ratio >= 2.0:
        score = 100.0
    elif ratio <= 1.0:
        score = (ratio - 0.5) / 0.5 * 50.0
    else:
        score = 50.0 + (ratio - 1.0) / 1.0 * 50.0

    logger.debug(
        "liquidity_turnover",
        current_amount=float(current),
        avg_20=float(avg_20),
        ratio=round(ratio, 4),
        score=round(score, 2),
    )
    return round(score, 2)


async def compute_liquidity_score(
    market: str = "CN",
    trade_date: date | None = None,
) -> float:
    """计算市场流动性维度得分。

    三个子维度（权重可因数据缺失而重新分配）:
    - 北向资金 20 日趋势 (40%)
    - 融资余额 20 日趋势 (30%)
    - 市场成交量 vs 20 日均量 (30%)

    Args:
        market: 市场代码，默认 "CN"。
        trade_date: 截止交易日，默认今天。

    Returns:
        流动性得分 0-100。全部数据缺失时返回 50（中性）。
    """
    if trade_date is None:
        trade_date = date.today()

    if market == "US":
        return await _compute_liquidity_score_us(trade_date)

    if market != "CN":
        logger.warning("liquidity_unsupported_market", market=market)
        return 50.0

    nb_score = await _northbound_score(trade_date)
    mg_score = await _margin_score(trade_date)
    tv_score = await _turnover_score(trade_date, market)

    # 动态权重分配
    components: list[tuple[str, float, float]] = []  # (name, score, base_weight)
    if nb_score is not None:
        components.append(("northbound", nb_score, 0.40))
    if mg_score is not None:
        components.append(("margin", mg_score, 0.30))
    if tv_score is not None:
        components.append(("turnover", tv_score, 0.30))

    if not components:
        logger.warning(
            "liquidity_no_data",
            market=market,
            trade_date=str(trade_date),
        )
        return 50.0

    # 按原始权重比例重新归一化
    total_weight = sum(w for _, _, w in components)
    result = sum(score * (w / total_weight) for _, score, w in components)
    result = round(max(0.0, min(100.0, result)), 2)

    logger.info(
        "liquidity_score_computed",
        market=market,
        trade_date=str(trade_date),
        score=result,
        components={name: val for name, val, _ in components},
        available_dimensions=len(components),
    )
    return result
