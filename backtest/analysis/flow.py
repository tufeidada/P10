"""
backtest/analysis/flow.py — PIT 版个股资金面分析

⚠️ 独立实现：不 import core/。
   通过 PITDataLoader 访问所有数据，PIT 安全。

三维评分 (A股, 0-100):
  main_flow_score  (40%): 近5日大单净流入累计 / 流通市值 → piecewise 映射
  northbound_score (30%): 近20日北向净买入累计 vs 历史分位；历史不足时为 None
  margin_score     (30%): 融资余额5日变化率 → piecewise 映射；非两融标的为 None

数据缺失时按实际可用维度重新加权（compute_flow_score）。
data_complete=True 仅当三维全部可用。Composite 层据此决定 Flow 整体置信度。

美股退化为:
  main_flow_score  (70%): 5日均量 / 20日均量（volume_trend）
  margin_score     (30%): 5日均成交额 / 20日均成交额（turnover_proxy）
  northbound_score : 0.0 占位（美股无北向）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import numpy as np

from backtest.pit_loader import PITDataLoader

log = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════════════════════
# 数据结构
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class FlowAnalysis:
    symbol:              str
    trade_date:          date
    market:              str
    main_flow_score:     float               # 0-100, A股大单 / 美股成交量代理
    northbound_score:    Optional[float]     # 0-100, 市场级北向资金代理（大盘级，非个股级）。
                                             # 同一交易日所有 A 股此值相同。
                                             # 北向历史数据仅从 2025-01-06 起，早于此返回 None。
                                             # 未来升级方向：使用 stk_nb_hold 获取个股北向持仓变化。
                                             # 美股恒为 0.0 占位。
    margin_score:        Optional[float]     # 0-100, A股融资余额变化 / 美股成交额代理; None=无数据
    score:               float               # 按实际可用维度重新加权的综合分
    highlights:          list[str]           = field(default_factory=list)
    risks:               list[str]           = field(default_factory=list)
    data_complete:       bool                = True   # False 表示有维度缺失
    detail:              dict                = field(default_factory=dict)

    def __str__(self) -> str:
        nb = f"{self.northbound_score:.1f}" if self.northbound_score is not None else "N/A"
        mg = f"{self.margin_score:.1f}"     if self.margin_score     is not None else "N/A"
        return (
            f"Flow({self.symbol} {self.trade_date} | "
            f"score={self.score:.1f} "
            f"mf={self.main_flow_score:.1f} nb={nb} mg={mg} "
            f"complete={self.data_complete})"
        )


# ═════════════════════════════════════════════════════════════════════════════
# 加权合并（可用维度动态重新归一化）
# ═════════════════════════════════════════════════════════════════════════════

_WEIGHTS = {"main": 0.40, "nb": 0.30, "margin": 0.30}


def compute_flow_score(
    main:       float | None,
    northbound: float | None,
    margin:     float | None,
    weights:    dict[str, float] = _WEIGHTS,
) -> tuple[float | None, bool]:
    """
    按可用维度动态加权，缺失维度的权重按比例分配给剩余维度。

    Returns:
        (score, data_complete).
        score=None 当所有维度均缺失。
        data_complete=True 仅当三维全部有效。
    """
    pool: dict[str, tuple[float, float]] = {}   # key → (value, weight)
    if main       is not None: pool["main"]   = (main,       weights["main"])
    if northbound is not None: pool["nb"]     = (northbound, weights["nb"])
    if margin     is not None: pool["margin"] = (margin,     weights["margin"])

    if not pool:
        return None, False

    total_w = sum(w for _, w in pool.values())
    score   = sum(v * w for v, w in pool.values()) / total_w
    return round(score, 2), len(pool) == 3


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


def _flt(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# ═════════════════════════════════════════════════════════════════════════════
# A 股三维评分
# ═════════════════════════════════════════════════════════════════════════════

def _score_main_flow(
    mf_df,
    circ_mv_yuan: float | None,
) -> tuple[float, float | None, list[str], list[str]]:
    highlights: list[str] = []
    risks:      list[str] = []

    if mf_df is None or mf_df.empty or "net_lg_amount" not in mf_df.columns:
        return 50.0, None, highlights, risks

    recent5 = mf_df.tail(5)["net_lg_amount"].dropna()
    if recent5.empty:
        return 50.0, None, highlights, risks

    sum_lg_wan = float(recent5.sum())

    if circ_mv_yuan and circ_mv_yuan > 0:
        flow_pct = sum_lg_wan * 10_000 / circ_mv_yuan * 100.0
    else:
        flow_pct = None

    if flow_pct is not None:
        score = _piecewise(flow_pct, [(-4, 5), (-2, 20), (-0.5, 40), (0, 50),
                                       (0.5, 60), (2, 80), (4, 95)])
        if flow_pct >= 2.0:
            highlights.append(f"近5日大单净流入占流通市值 +{flow_pct:.2f}%，主力积极买入")
        elif flow_pct <= -2.0:
            risks.append(f"近5日大单净流出占流通市值 {flow_pct:.2f}%，主力持续离场")
    else:
        score = _piecewise(sum_lg_wan, [(-200_000, 5), (-50_000, 20), (-5_000, 40),
                                         (0, 50), (5_000, 60), (50_000, 80), (200_000, 95)])
        if sum_lg_wan >= 50_000:
            highlights.append(f"近5日大单净流入 {sum_lg_wan/10000:.1f}亿元")
        elif sum_lg_wan <= -50_000:
            risks.append(f"近5日大单净流出 {sum_lg_wan/10000:.1f}亿元")

    return round(score, 2), flow_pct, highlights, risks


def _score_northbound(nb_df) -> tuple[float | None, float | None, list[str], list[str]]:
    """
    利用近20日北向净流入 vs 可用历史的滚动20日分位计算得分。

    北向数据仅从 2025-01-06 起。历史少于 40 个交易日时无法建立对比基准，
    返回 (None, None, ...) 而非占位 50，避免给 Composite 层虚假信号。

    Returns:
        (score, percentile_0_to_1, highlights, risks).
        score=None 表示数据不足，Composite 层应排除该维度。
    """
    highlights: list[str] = []
    risks:      list[str] = []

    if nb_df is None or nb_df.empty or "total_net_buy" not in nb_df.columns:
        return None, None, highlights, risks

    nb_vals = nb_df["total_net_buy"].dropna()
    if len(nb_vals) < 20:
        return None, None, highlights, risks

    arr = nb_vals.values.astype(np.float64)
    current_20d = float(arr[-20:].sum())

    if len(arr) < 40:
        # 有近20日数据但无足够历史对比 → 也返回 None
        return None, None, highlights, risks

    windows = np.array([arr[i:i+20].sum() for i in range(len(arr) - 19)])
    pct = float(np.sum(windows <= current_20d) / len(windows))
    score = pct * 100.0

    # highlights/risks：明确写"市场级"，避免误以为是个股级信号
    if pct >= 0.80:
        highlights.append(f"市场北向强势（近20日处历史{pct*100:.0f}%分位，市场级指标）")
    elif pct <= 0.20:
        risks.append(f"市场北向偏弱（近20日处历史{pct*100:.0f}%分位，市场级指标）")

    return round(score, 2), pct, highlights, risks


def _score_margin(margin_df) -> tuple[float | None, float | None, list[str], list[str]]:
    highlights: list[str] = []
    risks:      list[str] = []

    if margin_df is None or margin_df.empty or "rzye" not in margin_df.columns:
        return None, None, highlights, risks

    rzye = margin_df["rzye"].dropna()
    if len(rzye) < 6:
        return None, None, highlights, risks

    rzye_now  = float(rzye.iloc[-1])
    rzye_5ago = float(rzye.iloc[-6])

    if rzye_5ago <= 0:
        return None, None, highlights, risks

    chg_pct = (rzye_now - rzye_5ago) / rzye_5ago * 100.0
    score = _piecewise(chg_pct, [(-20, 5), (-10, 20), (-3, 40), (0, 50),
                                   (3, 60), (10, 80), (20, 95)])

    if chg_pct >= 10.0:
        highlights.append(f"融资余额5日增加 +{chg_pct:.1f}%，杠杆买入加速")
    elif chg_pct <= -10.0:
        risks.append(f"融资余额5日减少 {chg_pct:.1f}%，去杠杆压力")

    return round(score, 2), chg_pct, highlights, risks


# ═════════════════════════════════════════════════════════════════════════════
# 美股代理评分
# ═════════════════════════════════════════════════════════════════════════════

_VOL_BP = [(0.4, 10), (0.7, 30), (0.9, 45), (1.0, 50),
           (1.2, 60), (1.5, 75), (2.0, 90), (3.0, 98)]


def _score_us_flow(bars_df) -> tuple[float, float, float, list[str], list[str]]:
    highlights: list[str] = []
    risks:      list[str] = []

    if bars_df is None or bars_df.empty or len(bars_df) < 20:
        return 50.0, 50.0, 50.0, highlights, risks

    vol = bars_df["volume"].dropna().values.astype(np.float64)
    amt = bars_df["amount"].dropna().values.astype(np.float64)

    def _ratio_score(arr: np.ndarray) -> tuple[float, float]:
        if len(arr) < 20:
            return 50.0, 1.0
        vol20 = float(np.mean(arr[-20:]))
        vol5  = float(np.mean(arr[-5:]))
        ratio = vol5 / vol20 if vol20 > 0 else 1.0
        return _piecewise(ratio, _VOL_BP), ratio

    vt_s, vt_ratio = _ratio_score(vol)
    tp_s, _        = _ratio_score(amt)

    score = round(vt_s * 0.70 + tp_s * 0.30, 2)

    if vt_ratio >= 1.5:
        highlights.append(f"5日均量/20日均量 {vt_ratio:.2f}x，成交显著放量")
    elif vt_ratio <= 0.7:
        risks.append(f"5日均量/20日均量 {vt_ratio:.2f}x，成交萎缩")

    return round(vt_s, 2), round(tp_s, 2), score, highlights, risks


# ═════════════════════════════════════════════════════════════════════════════
# 主入口
# ═════════════════════════════════════════════════════════════════════════════

async def analyze_flow(
    loader: PITDataLoader,
    symbol: str,
    market: str,
) -> FlowAnalysis | None:
    """
    计算个股在 loader.current_date 的资金面评分。

    Args:
        loader: PITDataLoader（调用方已 set_date()）。
        symbol: 股票代码。
        market: "CN" 或 "US"。

    Returns:
        FlowAnalysis，或 None（完全无数据）。
    """
    current = loader._assert_date()

    # ── 美股分支 ──────────────────────────────────────────────────────────────
    if market == "US":
        bars_df = await loader.get_bars(symbol, lookback_days=30)
        if bars_df.empty:
            log.warning(f"flow: no bars for {symbol}")
            return None

        vt_s, tp_s, score, hl, rk = _score_us_flow(bars_df)

        return FlowAnalysis(
            symbol=symbol,
            trade_date=current,
            market=market,
            main_flow_score=vt_s,
            northbound_score=0.0,
            margin_score=tp_s,
            score=score,
            highlights=hl,
            risks=rk,
            data_complete=True,
            detail={
                "volume_trend_score":   vt_s,
                "turnover_proxy_score": tp_s,
                "note": "US: volume_trend(70%) + turnover_proxy(30%)",
            },
        )

    # ── A 股分支 ──────────────────────────────────────────────────────────────
    all_highlights: list[str] = []
    all_risks:      list[str] = []

    # 1. 主力资金流
    mf_df    = await loader.get_moneyflow(symbol, lookback_days=10)
    fund_row = await loader.get_fundamentals(symbol)
    circ_mv  = _flt(fund_row.get("circ_mv")) if fund_row else None

    mf_score, flow_pct, mf_hl, mf_rk = _score_main_flow(mf_df, circ_mv)
    all_highlights.extend(mf_hl)
    all_risks.extend(mf_rk)

    # 2. 北向资金（市场级；历史不足时为 None）
    nb_df    = await loader.get_northbound(lookback_days=260)
    nb_score, nb_pct, nb_hl, nb_rk = _score_northbound(nb_df)
    all_highlights.extend(nb_hl)
    all_risks.extend(nb_rk)

    # 3. 融资融券
    mg_df    = await loader.get_margin(symbol, lookback_days=10)
    mg_score, mg_chg_pct, mg_hl, mg_rk = _score_margin(mg_df)
    all_highlights.extend(mg_hl)
    all_risks.extend(mg_rk)

    # ── 动态加权合并 ──────────────────────────────────────────────────────────
    score, data_complete = compute_flow_score(mf_score, nb_score, mg_score)
    if score is None:
        log.warning(f"flow: all dimensions missing for {symbol} @ {current}")
        return None

    # 重新加权信息写入 detail
    parts = []
    if nb_score is not None:   parts.append("40/30/30")
    elif mg_score is not None: parts.append("main×57.1%+margin×42.9% (no northbound)")
    else:                      parts.append("main×100% (only main_flow available)")

    highlights = all_highlights[:3]
    risks      = all_risks[:3]

    log.info(
        f"flow: {symbol} @ {current} "
        f"mf={mf_score:.1f} nb={nb_score} mg={mg_score} "
        f"score={score:.1f} complete={data_complete}"
    )

    return FlowAnalysis(
        symbol=symbol,
        trade_date=current,
        market=market,
        main_flow_score=mf_score,
        northbound_score=nb_score,
        margin_score=mg_score,
        score=score,
        highlights=highlights,
        risks=risks,
        data_complete=data_complete,
        detail={
            "flow_pct_5d":   flow_pct,
            "nb_20d_pct":    nb_pct,        # 百分位 0-1；None 表示无历史数据
            "margin_chg_5d": mg_chg_pct,
            "circ_mv_bn":    round(circ_mv / 1e9, 2) if circ_mv else None,
            "weights":       parts[0],
        },
    )
