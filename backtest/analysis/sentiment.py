"""
backtest/analysis/sentiment.py — 市场级情绪评分

⚠️ 独立实现：不 import core/。通过 PITDataLoader 访问数据，PIT 安全。

返回 0-100 浮点数（越高越乐观）。市场级指标：
  - A股: 同一交易日所有 CN 股共享一个 sentiment_score
  - 美股: 同一交易日所有 US 股共享一个 sentiment_score

A股三维 (0-100):
  advancing_ratio_score (40%): 近5日 advancing/(advancing+declining) 均值
  limit_ratio_score     (30%): 近5日 (limit_up+1)/(limit_down+1) 均值，piecewise 映射
  margin_change_score   (30%): CN watchlist 融资余额 5日变化率

缺失维度按 Flow 模块同样模式：不占位，按剩余维度比例重新加权。

美股:
  VIX close → piecewise 映射 (恐慌 → 低分，平静 → 高分)

与其他模块的语义重叠见 known_issues.md SENT-01。
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Optional

import numpy as np

from backtest.pit_loader import PITDataLoader

log = logging.getLogger(__name__)

_WEIGHTS_CN = {"adv": 0.40, "limit": 0.30, "margin": 0.30}


# ═════════════════════════════════════════════════════════════════════════════
# 工具
# ═════════════════════════════════════════════════════════════════════════════

def _piecewise(x: float, bp: list[tuple[float, float]]) -> float:
    if x <= bp[0][0]:
        return float(bp[0][1])
    if x >= bp[-1][0]:
        return float(bp[-1][1])
    for i in range(len(bp) - 1):
        x0, y0 = bp[i];  x1, y1 = bp[i + 1]
        if x0 <= x <= x1:
            return float(y0 + (x - x0) / (x1 - x0) * (y1 - y0))
    return 50.0


def _reweight(scores: dict[str, tuple[float, float]]) -> tuple[float | None, bool]:
    """
    按可用维度动态重新归一化。
    scores: {key: (value, weight)}
    Returns: (weighted_score, all_present)
    """
    if not scores:
        return None, False
    total_w = sum(w for _, w in scores.values())
    score   = sum(v * w for v, w in scores.values()) / total_w
    return round(score, 2), True  # "all_present" managed by caller


# ═════════════════════════════════════════════════════════════════════════════
# A 股三维评分
# ═════════════════════════════════════════════════════════════════════════════

def _score_advancing(breadth_df, market: str = "CN") -> tuple[float | None, dict]:
    """
    近5日 advancing/(advancing+declining) 均值 → 0-100。

    advancing_count 和 declining_count 可来自全市场或 watchlist 子集，
    用比例而非绝对数，因此两种规模下都可比。
    """
    if breadth_df is None or breadth_df.empty:
        return None, {}

    sub = breadth_df[breadth_df["market"] == market].copy()
    if sub.empty:
        return None, {}

    sub = sub.sort_values("trade_date").tail(5)

    ratios = []
    for _, row in sub.iterrows():
        adv = row.get("advancing_count")
        dec = row.get("declining_count")
        if adv is None or dec is None:
            continue
        adv, dec = float(adv), float(dec)
        total = adv + dec
        if total > 0:
            ratios.append(adv / total)

    if not ratios:
        return None, {}

    avg_ratio = float(np.mean(ratios))
    score = _piecewise(avg_ratio, [
        (0.25, 10), (0.35, 25), (0.45, 40), (0.50, 50),
        (0.55, 60), (0.65, 75), (0.75, 90),
    ])

    latest = sub.iloc[-1]
    return round(score, 2), {
        "adv_ratio_5d_mean": round(avg_ratio, 4),
        "adv_latest":        int(latest.get("advancing_count", 0)),
        "dec_latest":        int(latest.get("declining_count", 0)),
        "total_stocks":      int(latest.get("total_stocks", 0)),
    }


def _score_limit(breadth_df, market: str = "CN") -> tuple[float | None, dict]:
    """
    近5日 (limit_up+1)/(limit_down+1) 均值 → piecewise 映射 → 0-100。
    +1 平滑处理防止除零，同时使"零涨停/零跌停"对应中性 1.0。
    """
    if breadth_df is None or breadth_df.empty:
        return None, {}

    sub = breadth_df[breadth_df["market"] == market].copy()
    if sub.empty:
        return None, {}

    sub = sub.sort_values("trade_date").tail(5)

    ratios = []
    for _, row in sub.iterrows():
        lu = row.get("limit_up_count")
        ld = row.get("limit_down_count")
        if lu is None or ld is None:
            continue
        ratios.append((float(lu) + 1) / (float(ld) + 1))

    if not ratios:
        return None, {}

    avg_ratio = float(np.mean(ratios))
    score = _piecewise(avg_ratio, [
        (0.25, 10), (0.50, 25), (0.75, 40), (1.00, 50),
        (1.50, 65), (3.00, 80), (6.00, 92),
    ])

    latest = sub.iloc[-1]
    return round(score, 2), {
        "limit_ratio_5d_mean": round(avg_ratio, 3),
        "limit_up_latest":     int(latest.get("limit_up_count", 0)),
        "limit_down_latest":   int(latest.get("limit_down_count", 0)),
    }


async def _score_margin_market(
    loader: PITDataLoader,
    current: date,
) -> tuple[float | None, dict]:
    """
    CN watchlist 融资余额近 6 日聚合，计算 5日变化率 → 0-100。
    直接对 margin_daily 求和（watchlist 全量），available_date <= current。
    """
    rows = await loader._fetch("""
        SELECT trade_date, SUM(rzye) AS total_rzye
        FROM margin_daily
        WHERE available_date <= $1
        GROUP BY trade_date
        ORDER BY trade_date DESC
        LIMIT 6
    """, current)

    if len(rows) < 6:
        return None, {}

    rzye_vals = [float(r["total_rzye"]) for r in rows]
    rzye_now  = rzye_vals[0]
    rzye_5ago = rzye_vals[5]

    if rzye_5ago <= 0:
        return None, {}

    chg_pct = (rzye_now - rzye_5ago) / rzye_5ago * 100.0
    score = _piecewise(chg_pct, [
        (-4, 10), (-2, 25), (-0.5, 40), (0, 50),
        (0.5, 60), (2, 75), (4, 90),
    ])

    return round(score, 2), {
        "margin_chg_5d_pct": round(chg_pct, 4),
        "margin_total_now":  round(rzye_now / 1e8, 2),    # 亿元
        "margin_total_5ago": round(rzye_5ago / 1e8, 2),
    }


# ═════════════════════════════════════════════════════════════════════════════
# 美股情绪：VIX 映射
# ═════════════════════════════════════════════════════════════════════════════

_VIX_BP = [
    (12, 85), (15, 70), (20, 55), (25, 40), (30, 25), (40, 12),
]
# 实现说明：Spec 描述的是"分段映射"，本实现采用"分段控制点的线性插值"。
# 原因：避免在控制点边界（如 VIX=15 vs 15.01）出现 ~15 分的阶跃断层。
# 这与 Spec 的业务意图一致，属于实现细节，不视为违反 Spec（见 SENT-03）。


async def _score_vix(loader: PITDataLoader) -> tuple[float | None, dict]:
    """VIX close → 0-100（低 VIX = 乐观 = 高分）。"""
    vix_df = await loader.get_index("VIX", lookback_days=5)
    if vix_df.empty or "close" not in vix_df.columns:
        return None, {}

    vix_val = vix_df["close"].dropna()
    if vix_val.empty:
        return None, {}

    latest_vix = float(vix_val.iloc[-1])

    # VIX 本身是线性降序映射：VIX 越高越恐慌 → 分数越低
    if latest_vix < _VIX_BP[0][0]:
        score = 85.0
    else:
        score = _piecewise(latest_vix, _VIX_BP)

    return round(score, 2), {"vix": round(latest_vix, 2)}


# ═════════════════════════════════════════════════════════════════════════════
# 主入口
# ═════════════════════════════════════════════════════════════════════════════

async def analyze_market_sentiment(
    loader: PITDataLoader,
    market: str,
) -> tuple[float | None, dict]:
    """
    计算市场级情绪评分。

    Args:
        loader: PITDataLoader（调用方已 set_date()）。
        market: "CN" 或 "US"。

    Returns:
        (sentiment_score 0-100 or None, detail_dict).
        sentiment_score=None 表示完全无数据。
    """
    current = loader._assert_date()

    # ── 美股: VIX ─────────────────────────────────────────────────────────────
    if market == "US":
        score, det = await _score_vix(loader)
        if score is None:
            log.warning(f"sentiment: no VIX data @ {current}")
        else:
            log.info(f"sentiment: US @ {current} VIX={det['vix']} score={score}")
        return score, {"market": "US", **det}

    # ── A 股: 三维加权 ────────────────────────────────────────────────────────
    breadth_df = await loader.get_market_breadth(lookback_days=10)

    adv_score,   adv_det   = _score_advancing(breadth_df, "CN")
    limit_score, limit_det = _score_limit(breadth_df, "CN")
    margin_score, mg_det   = await _score_margin_market(loader, current)

    # 动态加权
    pool: dict[str, tuple[float, float]] = {}
    if adv_score    is not None: pool["adv"]    = (adv_score,    _WEIGHTS_CN["adv"])
    if limit_score  is not None: pool["limit"]  = (limit_score,  _WEIGHTS_CN["limit"])
    if margin_score is not None: pool["margin"] = (margin_score, _WEIGHTS_CN["margin"])

    score, _ = _reweight(pool)

    missing = [k for k in _WEIGHTS_CN if k not in pool]
    weights_str = (
        "40/30/30" if not missing
        else f"partial ({', '.join(missing)} missing)"
    )

    det = {
        "market":          "CN",
        "weights":         weights_str,
        "adv_score":       adv_score,
        "limit_score":     limit_score,
        "margin_score":    margin_score,
        **adv_det, **limit_det, **mg_det,
    }

    log.info(
        f"sentiment: CN @ {current} "
        f"adv={adv_score} limit={limit_score} margin={margin_score} score={score}"
    )
    return score, det
