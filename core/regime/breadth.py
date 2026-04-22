"""
市场广度评分模块 — 计算市场广度维度得分 (0-100)。

通过涨跌比、站上 MA20 比例、新高新低差值三个子维度，
评估市场参与度和健康程度。分数越高，市场越健康。
"""

from __future__ import annotations

from datetime import date

import numpy as np
import structlog

from db.connection import db_query, db_query_one, db_query_val

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# US 市场广度子维度
# ---------------------------------------------------------------------------


def _rsi_wilder(closes: np.ndarray, period: int = 14) -> float:
    """计算 Wilder RSI。

    Args:
        closes: 收盘价序列（按时间升序）。
        period: RSI 周期，默认 14。

    Returns:
        RSI 值 0-100，数据不足时返回 50.0。
    """
    if len(closes) < period + 1:
        return 50.0

    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return round(100.0 - 100.0 / (1.0 + rs), 4)


async def _us_spy_rsi_score(trade_date: date) -> float | None:
    """SPY RSI(14) 作为市场广度代理得分 (0-100)，权重 40%。

    RSI > 60 → 70+；RSI 40-60 → 45-65；RSI < 40 → 20-40。

    Args:
        trade_date: 截止交易日。

    Returns:
        得分 0-100，数据不足时返回 None。
    """
    rows = await db_query(
        """
        SELECT close
        FROM market_bars_daily
        WHERE symbol = 'SPY' AND trade_date <= $1
        ORDER BY trade_date DESC
        LIMIT 30
        """,
        trade_date,
    )

    if not rows or len(rows) < 16:
        return None

    rows = list(reversed(rows))
    closes = np.array([float(r["close"]) for r in rows], dtype=np.float64)
    rsi = _rsi_wilder(closes, period=14)

    if rsi > 60:
        # RSI 60-100 → 得分 70-100
        score = 70.0 + (rsi - 60.0) / 40.0 * 30.0
    elif rsi >= 40:
        # RSI 40-60 → 得分 45-65（中性区间线性插值，中点 RSI=50→得分55）
        score = 45.0 + (rsi - 40.0) / 20.0 * 20.0
    else:
        # RSI 0-40 → 得分 0-40（RSI=40→40分，RSI=0→0分）
        score = rsi / 40.0 * 40.0

    score = round(max(0.0, min(100.0, score)), 2)
    logger.debug("breadth_us_spy_rsi", rsi=round(rsi, 2), score=score)
    return score


async def _us_credit_breadth_score(trade_date: date) -> float | None:
    """HYG/TLT 比率趋势作为信用市场广度代理得分 (0-100)，权重 30%。

    HYG 相对 TLT 上升 → 信用环境改善 → 得分高。

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

    # 按时间升序并对齐日期
    hyg_map = {r["report_date"]: float(r["value"]) for r in hyg_rows}
    tlt_map = {r["report_date"]: float(r["value"]) for r in tlt_rows}

    common_dates = sorted(set(hyg_map.keys()) & set(tlt_map.keys()))
    if len(common_dates) < 5:
        return None

    ratios = np.array([hyg_map[d] / tlt_map[d] for d in common_dates], dtype=np.float64)

    # 计算比率趋势: 近期 vs 早期均值
    mid = len(ratios) // 2
    recent_mean = np.mean(ratios[mid:])
    early_mean = np.mean(ratios[:mid])

    if early_mean <= 0:
        return None

    momentum = (recent_mean - early_mean) / early_mean
    # momentum 典型范围: -0.05 ~ +0.05
    # 映射: -0.05 → 0, 0 → 50, +0.05 → 100
    score = 50.0 + momentum / 0.05 * 50.0
    score = round(max(0.0, min(100.0, score)), 2)

    logger.debug(
        "breadth_us_credit",
        hyg_tlt_momentum=round(momentum, 5),
        score=score,
        common_days=len(common_dates),
    )
    return score


async def _us_spy_momentum_score(trade_date: date) -> float | None:
    """SPY 5 日收益率 vs 20 日收益率作为短期动量广度代理得分 (0-100)，权重 30%。

    两者均为正且短期 > 长期 → 高分；两者均为负 → 低分。

    Args:
        trade_date: 截止交易日。

    Returns:
        得分 0-100，数据不足时返回 None。
    """
    rows = await db_query(
        """
        SELECT close
        FROM market_bars_daily
        WHERE symbol = 'SPY' AND trade_date <= $1
        ORDER BY trade_date DESC
        LIMIT 25
        """,
        trade_date,
    )

    if not rows or len(rows) < 22:
        return None

    rows = list(reversed(rows))
    closes = np.array([float(r["close"]) for r in rows], dtype=np.float64)

    ret_5d = (closes[-1] / closes[-6] - 1.0) if closes[-6] != 0 else 0.0
    ret_20d = (closes[-1] / closes[-21] - 1.0) if len(closes) >= 21 and closes[-21] != 0 else 0.0

    # 评分规则:
    # 两者均正且 5d > 20d → 动量加速 → 高分
    # 两者均正但 5d < 20d → 动量减速 → 中高分
    # 一正一负 → 中性
    # 两者均负 → 低分
    if ret_5d > 0 and ret_20d > 0:
        if ret_5d >= ret_20d:
            score = 75.0 + min(25.0, ret_5d / 0.05 * 25.0)  # 75-100
        else:
            score = 55.0 + min(20.0, ret_20d / 0.05 * 20.0)  # 55-75
    elif ret_5d > 0 or ret_20d > 0:
        score = 50.0  # 中性
    else:
        # 两者均负
        neg_avg = (abs(ret_5d) + abs(ret_20d)) / 2
        score = 50.0 - min(50.0, neg_avg / 0.05 * 50.0)  # 0-50

    score = round(max(0.0, min(100.0, score)), 2)
    logger.debug(
        "breadth_us_spy_momentum",
        ret_5d=round(ret_5d, 4),
        ret_20d=round(ret_20d, 4),
        score=score,
    )
    return score


async def _compute_breadth_score_us(trade_date: date) -> float:
    """计算 US 市场广度得分。

    三个子维度:
    - SPY RSI(14) (权重 40%)
    - HYG/TLT 比率趋势 (权重 30%)
    - SPY 5 日 vs 20 日收益率 (权重 30%)

    数据缺失时在可用维度间重新分配权重。

    Args:
        trade_date: 截止交易日。

    Returns:
        广度得分 0-100。全部数据缺失时返回 50.0。
    """
    rsi_score = await _us_spy_rsi_score(trade_date)
    credit_score = await _us_credit_breadth_score(trade_date)
    momentum_score = await _us_spy_momentum_score(trade_date)

    components: list[tuple[str, float, float]] = []
    if rsi_score is not None:
        components.append(("spy_rsi", rsi_score, 0.40))
    if credit_score is not None:
        components.append(("hyg_tlt_credit", credit_score, 0.30))
    if momentum_score is not None:
        components.append(("spy_momentum", momentum_score, 0.30))

    if not components:
        logger.warning("breadth_us_no_data", trade_date=str(trade_date))
        return 50.0

    total_weight = sum(w for _, _, w in components)
    result = sum(score * (w / total_weight) for _, score, w in components)
    result = round(max(0.0, min(100.0, result)), 2)

    logger.info(
        "breadth_score_computed",
        market="US",
        trade_date=str(trade_date),
        score=result,
        components={name: val for name, val, _ in components},
        available_dimensions=len(components),
    )
    return result


async def _up_down_ratio_score(trade_date: date) -> float | None:
    """计算涨跌比 5 日均值得分 (0-100)。

    从 market_sentiment_daily 获取最近 5 天的 up_down_ratio，
    取均值后映射到 0-100。

    Args:
        trade_date: 截止交易日。

    Returns:
        得分 0-100，数据不足时返回 None。
    """
    rows = await db_query(
        """
        SELECT up_down_ratio
        FROM market_sentiment_daily
        WHERE trade_date <= $1
        ORDER BY trade_date DESC
        LIMIT 5
        """,
        trade_date,
    )

    if not rows:
        return None

    ratios = [float(r["up_down_ratio"]) for r in rows if r["up_down_ratio"] is not None]
    if not ratios:
        return None

    avg_ratio = np.mean(ratios)
    # up_down_ratio 一般范围: 0.3（极度弱势） ~ 3.0（极度强势）
    # 映射: 0.3 → 0, 1.0 → 50, 3.0 → 100
    if avg_ratio <= 0.3:
        score = 0.0
    elif avg_ratio >= 3.0:
        score = 100.0
    elif avg_ratio <= 1.0:
        # 0.3-1.0 → 0-50
        score = (avg_ratio - 0.3) / 0.7 * 50.0
    else:
        # 1.0-3.0 → 50-100
        score = 50.0 + (avg_ratio - 1.0) / 2.0 * 50.0

    return round(score, 2)


async def _above_ma20_pct_score(trade_date: date, market: str = "CN") -> float | None:
    """计算站上 MA20 的个股占比得分 (0-100)。

    从 features_daily 查询最近交易日有 ma20 数据的股票，
    计算 close > ma20 的比例。

    Args:
        trade_date: 截止交易日。
        market: 市场代码。

    Returns:
        得分 0-100，数据不足时返回 None。
    """
    # 先找到最近有数据的交易日
    latest_date = await db_query_val(
        """
        SELECT MAX(trade_date)
        FROM features_daily
        WHERE trade_date <= $1 AND ma20 IS NOT NULL
        """,
        trade_date,
    )

    if latest_date is None:
        return None

    # 查询该日所有有 ma20 的股票
    row = await db_query_one(
        """
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE b.close > f.ma20) AS above_ma20
        FROM features_daily f
        JOIN market_bars_daily b ON f.symbol = b.symbol AND f.trade_date = b.trade_date
        WHERE f.trade_date = $1
          AND b.market = $2
          AND f.ma20 IS NOT NULL
          AND b.close IS NOT NULL
        """,
        latest_date,
        market,
    )

    if row is None or row["total"] == 0:
        return None

    pct = row["above_ma20"] / row["total"] * 100.0
    return round(pct, 2)


async def _new_high_low_score(trade_date: date) -> float | None:
    """计算新高新低差值得分 (0-100)。

    (new_high_count - new_low_count) / total_stocks 映射到 0-100。

    Args:
        trade_date: 截止交易日。

    Returns:
        得分 0-100，数据不足时返回 None。
    """
    row = await db_query_one(
        """
        SELECT new_high_count, new_low_count
        FROM market_sentiment_daily
        WHERE trade_date <= $1
        ORDER BY trade_date DESC
        LIMIT 1
        """,
        trade_date,
    )

    if row is None:
        return None

    new_high = row["new_high_count"]
    new_low = row["new_low_count"]

    if new_high is None or new_low is None:
        return None

    # 估算总股票数（A股约 5000+）
    total_stocks = await db_query_val(
        """
        SELECT COUNT(DISTINCT symbol)
        FROM market_bars_daily
        WHERE trade_date = (
            SELECT MAX(trade_date) FROM market_bars_daily WHERE trade_date <= $1 AND market = 'CN'
        )
        AND market = 'CN'
        """,
        trade_date,
    )

    if not total_stocks or total_stocks == 0:
        total_stocks = 5000  # fallback

    diff_ratio = (new_high - new_low) / total_stocks
    # diff_ratio 典型范围: -0.05 ~ +0.05
    # 映射: -0.05 → 0, 0 → 50, +0.05 → 100
    score = 50.0 + diff_ratio / 0.05 * 50.0
    score = max(0.0, min(100.0, score))
    return round(score, 2)


async def compute_breadth_score(
    market: str = "CN",
    trade_date: date | None = None,
) -> float:
    """计算市场广度维度得分。

    三个子维度等权 (各 33.3%):
    1. 涨跌比 5 日均值
    2. 站上 MA20 个股占比
    3. 新高新低差值比

    数据缺失时在可用维度间重新分配权重。

    Args:
        market: 市场代码，默认 "CN"。
        trade_date: 截止交易日，默认今天。

    Returns:
        广度得分 0-100。全部数据缺失时返回 50（中性）。
    """
    if trade_date is None:
        trade_date = date.today()

    if market == "US":
        return await _compute_breadth_score_us(trade_date)

    if market != "CN":
        logger.warning("breadth_unsupported_market", market=market)
        return 50.0

    # 并行计算三个子维度
    ud_score = await _up_down_ratio_score(trade_date)
    ma20_score = await _above_ma20_pct_score(trade_date, market)
    nhl_score = await _new_high_low_score(trade_date)

    components: list[tuple[str, float]] = []
    if ud_score is not None:
        components.append(("up_down_ratio", ud_score))
    if ma20_score is not None:
        components.append(("above_ma20_pct", ma20_score))
    if nhl_score is not None:
        components.append(("new_high_low", nhl_score))

    if not components:
        logger.warning(
            "breadth_no_data",
            market=market,
            trade_date=str(trade_date),
        )
        return 50.0

    # 等权重取平均
    result = round(float(np.mean([s for _, s in components])), 2)
    result = max(0.0, min(100.0, result))

    logger.info(
        "breadth_score_computed",
        market=market,
        trade_date=str(trade_date),
        score=result,
        components={name: val for name, val in components},
        available_dimensions=len(components),
    )
    return result
