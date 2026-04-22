"""
backtest/analysis/fundamental.py — PIT 版个股基本面分析

⚠️ 独立实现：禁止 import P10 主项目 core/ 下任何模块。
   使用 asyncpg pool + cutoff 直接查询（与 regime.py / technical.py 保持一致），
   所有 financials 过滤用 available_date <= cutoff（PIT 安全）。

四个子维度评分 (0-100):
  profitability — ROE 水平/稳定性 + 毛利率稳定性 + OCF/NP
  growth        — 营收 YoY + 净利润 YoY + 增速加速度
  valuation     — PE/PB 同行业分位 + PE 自身 3 年历史分位（低估值 = 高分）
  health        — 资产负债率 + 流动比率 + 商誉/净资产

行业框架权重从 config/industry_frameworks.yaml 加载，
industry 来源于 config/watchlist.yaml（watchlist 内股票）或退化为 default。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

import asyncpg
import numpy as np
import pandas as pd
import yaml

log = logging.getLogger(__name__)

_CONFIG_DIR    = Path(__file__).resolve().parents[2] / "backtest" / "config"
_WATCHLIST_PATH = _CONFIG_DIR / "watchlist.yaml"
_INDUSTRY_PATH  = _CONFIG_DIR / "industry_frameworks.yaml"


# ═════════════════════════════════════════════════════════════════════════════
# 配置加载
# ═════════════════════════════════════════════════════════════════════════════

@lru_cache(maxsize=1)
def _load_watchlist_industry() -> dict[str, str]:
    """symbol → raw industry string (从 watchlist.yaml)。"""
    with open(_WATCHLIST_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    result: dict[str, str] = {}
    for market_group in data.get("watchlist", {}).values():
        for item in market_group:
            result[item["symbol"]] = item.get("industry", "")
    return result


@lru_cache(maxsize=1)
def _load_industry_config() -> dict[str, Any]:
    with open(_INDUSTRY_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _get_framework(symbol: str) -> tuple[str, dict[str, float]]:
    """返回 (framework_name, weights_dict)。"""
    wl_industry = _load_watchlist_industry()
    cfg = _load_industry_config()
    raw_industry = wl_industry.get(symbol, "")
    i2f = cfg.get("industry_to_framework", {})
    fw_name = i2f.get(raw_industry, "default")
    weights = cfg["frameworks"].get(fw_name, cfg["frameworks"]["default"])
    return fw_name, weights


def _get_watchlist_by_framework(fw_name: str) -> list[str]:
    """返回同一 framework 的所有 watchlist symbol。"""
    wl_industry = _load_watchlist_industry()
    cfg = _load_industry_config()
    i2f = cfg.get("industry_to_framework", {})
    result = []
    for sym, ind in wl_industry.items():
        if i2f.get(ind, "default") == fw_name:
            result.append(sym)
    return result


def _load_thresholds() -> dict[str, Any]:
    cfg = _load_industry_config()
    return {
        "profit": cfg.get("profitability_thresholds", {}),
        "growth": cfg.get("growth_thresholds", {}),
        "valuation": cfg.get("valuation_thresholds", {}),
        "health": cfg.get("health_thresholds", {}),
    }


# ═════════════════════════════════════════════════════════════════════════════
# 数据结构
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class FundamentalResult:
    symbol:              str
    trade_date:          date
    market:              str
    profitability_score: float         # 0-100
    growth_score:        float         # 0-100
    valuation_score:     float         # 0-100
    health_score:        float         # 0-100
    fundamental_score:   float         # 0-100 按行业框架加权
    framework_used:      str
    data_quarters:       int           # 可用财报季度数（PIT 过滤后）
    highlights:          list[str]     # 最多 3 条亮点
    risks:               list[str]     # 最多 3 条风险
    detail:              dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return (
            f"Fundamental({self.symbol} {self.trade_date} | {self.framework_used} | "
            f"score={self.fundamental_score:.1f} "
            f"P={self.profitability_score:.1f} G={self.growth_score:.1f} "
            f"V={self.valuation_score:.1f} H={self.health_score:.1f} "
            f"qtrs={self.data_quarters})"
        )


# ═════════════════════════════════════════════════════════════════════════════
# 工具函数
# ═════════════════════════════════════════════════════════════════════════════

def _flt(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _piecewise(x: float, breakpoints: list[tuple[float, float]]) -> float:
    """分段线性插值，breakpoints 为 [(x, y), ...]，x 升序。"""
    if x <= breakpoints[0][0]:
        return float(breakpoints[0][1])
    if x >= breakpoints[-1][0]:
        return float(breakpoints[-1][1])
    for i in range(len(breakpoints) - 1):
        x0, y0 = breakpoints[i]
        x1, y1 = breakpoints[i + 1]
        if x0 <= x <= x1:
            return float(y0 + (x - x0) / (x1 - x0) * (y1 - y0))
    return 50.0


def _series_stability_score(values: list[float], max_bad_cv: float = 0.5) -> float:
    """稳定性得分 0-100。变异系数 CV = std/|mean|, CV 越小越稳定。"""
    arr = np.array([v for v in values if v is not None], dtype=np.float64)
    if len(arr) < 2:
        return 50.0
    mean = np.mean(arr)
    if abs(mean) < 1e-6:
        return 50.0
    cv = np.std(arr, ddof=1) / abs(mean)
    return float(np.clip(100.0 - cv / max_bad_cv * 100.0, 0.0, 100.0))


def _percentile_of(value: float, arr: np.ndarray) -> float:
    """value 在 arr 中的百分位 (0-1)。"""
    if len(arr) == 0:
        return 0.5
    return float(np.sum(arr <= value) / len(arr))


# ═════════════════════════════════════════════════════════════════════════════
# 数据查询
# ═════════════════════════════════════════════════════════════════════════════

async def _fetch_financials(
    conn: asyncpg.Connection,
    symbol: str,
    cutoff: date,
    n_quarters: int = 12,
) -> pd.DataFrame:
    """PIT 安全的财报查询，用 available_date <= cutoff 过滤。"""
    rows = await conn.fetch("""
        SELECT report_date, revenue, revenue_yoy, revenue_qoq,
               net_profit, np_yoy,
               gross_margin, net_margin,
               total_assets, total_liab, debt_ratio,
               current_ratio, goodwill,
               ocf, ocf_to_np,
               roe_ttm, roa_ttm,
               available_date
        FROM financials_quarterly
        WHERE symbol = $1
          AND available_date <= $2
        ORDER BY report_date DESC
        LIMIT $3
    """, symbol, cutoff, n_quarters)

    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([dict(r) for r in rows])
    for col in df.select_dtypes(include="object").columns:
        if col not in ("report_date", "available_date"):
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Normalize ratio columns to percentage scale.
    # Detect: if gross_margin max < 2, data is stored as decimal (0-1) → multiply by 100.
    # Columns that should be in percentage: yoy/qoq growth rates, margins, ROE/ROA, debt_ratio.
    # current_ratio is a real ratio (e.g. 6.62x) — leave it as is.
    # ocf_to_np, current_ratio are multipliers — excluded from percentage normalization.
    _pct_cols = ["revenue_yoy", "revenue_qoq", "np_yoy",
                 "gross_margin", "net_margin",
                 "roe_ttm", "roa_ttm", "debt_ratio"]
    _pct_cols = [c for c in _pct_cols if c in df.columns]
    if _pct_cols:
        gm = df["gross_margin"].dropna() if "gross_margin" in df.columns else pd.Series(dtype=float)
        if len(gm) > 0 and gm.abs().max() < 2.0:
            df[_pct_cols] = df[_pct_cols] * 100.0

    return df


async def _fetch_valuation_current(
    conn: asyncpg.Connection,
    symbol: str,
    cutoff: date,
) -> dict[str, float | None]:
    """获取截止日期最新估值指标（pe_ttm, pb）。"""
    row = await conn.fetchrow("""
        SELECT pe_ttm, pb
        FROM fundamentals_daily
        WHERE symbol = $1 AND available_date <= $2
        ORDER BY trade_date DESC
        LIMIT 1
    """, symbol, cutoff)
    if not row:
        return {"pe_ttm": None, "pb": None}
    return {"pe_ttm": _flt(row["pe_ttm"]), "pb": _flt(row["pb"])}


async def _fetch_valuation_history(
    conn: asyncpg.Connection,
    symbol: str,
    cutoff: date,
    years: int = 3,
) -> np.ndarray:
    """获取历史 pe_ttm 序列（用于自身分位计算）。"""
    rows = await conn.fetch("""
        SELECT pe_ttm FROM fundamentals_daily
        WHERE symbol = $1
          AND available_date <= $2
          AND trade_date >= ($2 - INTERVAL '1 year' * $3)
          AND pe_ttm IS NOT NULL AND pe_ttm > 0
        ORDER BY trade_date DESC
        LIMIT 750
    """, symbol, cutoff, years)
    if not rows:
        return np.array([])
    return np.array([float(r["pe_ttm"]) for r in rows], dtype=np.float64)


async def _fetch_peer_valuations(
    conn: asyncpg.Connection,
    peers: list[str],
    cutoff: date,
) -> pd.DataFrame:
    """获取同行业 peers 的最新 pe_ttm、pb（用于横截面分位）。"""
    if not peers:
        return pd.DataFrame()
    rows = await conn.fetch("""
        SELECT DISTINCT ON (symbol) symbol, pe_ttm, pb
        FROM fundamentals_daily
        WHERE symbol = ANY($1)
          AND available_date <= $2
          AND trade_date >= ($2 - INTERVAL '5 days')
        ORDER BY symbol, trade_date DESC
    """, peers, cutoff)
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame([dict(r) for r in rows])
    df["pe_ttm"] = pd.to_numeric(df["pe_ttm"], errors="coerce")
    df["pb"]     = pd.to_numeric(df["pb"],     errors="coerce")
    return df


# ═════════════════════════════════════════════════════════════════════════════
# 盈利质量评分
# ═════════════════════════════════════════════════════════════════════════════

def _score_profitability(
    df: pd.DataFrame,
    thr: dict,
    market: str,
) -> tuple[float, list[str], list[str]]:
    highlights: list[str] = []
    risks:      list[str] = []

    # ── ROE (50%) ──────────────────────────────────────────────────────
    roe_vals = df["roe_ttm"].dropna().tolist()
    roe_latest = roe_vals[0] if roe_vals else None

    roe_level_s = 50.0
    if roe_latest is not None:
        roe_level_s = _piecewise(roe_latest, [
            (-20, 0), (0, 5), (8, 40), (15, 70), (25, 90), (40, 100)
        ])
        if roe_latest >= thr.get("roe_excellent", 25):
            highlights.append(f"ROE TTM {roe_latest:.1f}% 高位")
        elif roe_latest < thr.get("roe_ok", 8):
            risks.append(f"ROE TTM {roe_latest:.1f}% 偏低")

    roe_stab_s = _series_stability_score(roe_vals[:8]) if len(roe_vals) >= 2 else 50.0
    roe_s = roe_level_s * 0.7 + roe_stab_s * 0.3

    # ── 毛利率稳定性 (25%) ──────────────────────────────────────────────
    gm_vals = df["gross_margin"].dropna().tolist()
    gm_latest = gm_vals[0] if gm_vals else None

    gm_level_s = 50.0
    if gm_latest is not None:
        gm_level_s = _piecewise(gm_latest, [
            (-10, 0), (0, 10), (15, 40), (30, 65), (50, 85), (70, 100)
        ])

    gm_stab_s = _series_stability_score(gm_vals[:8]) if len(gm_vals) >= 2 else 50.0
    gm_s = gm_level_s * 0.6 + gm_stab_s * 0.4

    # ── OCF/NP (25%) ───────────────────────────────────────────────────
    ocf_np_vals = df["ocf_to_np"].dropna().tolist()
    # 如果 ocf_to_np 为空，尝试用 ocf / net_profit 计算
    if not ocf_np_vals and "ocf" in df.columns and "net_profit" in df.columns:
        for _, row in df.iterrows():
            ocf = _flt(row.get("ocf"))
            np_ = _flt(row.get("net_profit"))
            if ocf is not None and np_ is not None and abs(np_) > 1:
                ocf_np_vals.append(ocf / np_)

    ocf_np_latest = np.mean(ocf_np_vals[:4]) if ocf_np_vals else None

    ocf_s = 50.0
    if ocf_np_latest is not None:
        ocf_s = _piecewise(ocf_np_latest, [
            (-2, 0), (0, 10), (0.5, 30), (1.0, 65), (1.25, 85), (2.0, 100)
        ])
        if ocf_np_latest >= thr.get("ocf_np_excellent", 1.25):
            highlights.append(f"OCF/NP {ocf_np_latest:.2f} 现金流质量优秀")
        elif ocf_np_latest < 0:
            risks.append(f"OCF/NP {ocf_np_latest:.2f} 现金流为负")

    score = round(roe_s * 0.50 + gm_s * 0.25 + ocf_s * 0.25, 2)
    return score, highlights, risks


# ═════════════════════════════════════════════════════════════════════════════
# 成长性评分
# ═════════════════════════════════════════════════════════════════════════════

def _score_growth(
    df: pd.DataFrame,
    thr: dict,
    market: str,
    n_quarters: int,
) -> tuple[float, list[str], list[str]]:
    highlights: list[str] = []
    risks:      list[str] = []

    rv_center = thr.get("revenue_yoy_center", 15.0)
    np_center = thr.get("np_yoy_center", 20.0)

    # ── 营收 YoY (40%) ─────────────────────────────────────────────────
    rev_yoy = df["revenue_yoy"].dropna().tolist()
    if rev_yoy:
        recent_rev = float(np.mean(rev_yoy[:4]))
        rev_s = _piecewise(recent_rev, [
            (-rv_center, 0), (0, 33), (rv_center, 67), (3 * rv_center, 100)
        ])
        if recent_rev >= rv_center * 2:
            highlights.append(f"营收 YoY 均 {recent_rev:.1f}% 高增")
        elif recent_rev < 0:
            risks.append(f"营收 YoY 均 {recent_rev:.1f}% 负增长")
    else:
        rev_s = 50.0

    # ── 净利润 YoY (40%) ───────────────────────────────────────────────
    np_yoy = df["np_yoy"].dropna().tolist()
    if np_yoy:
        recent_np = float(np.mean(np_yoy[:4]))
        np_s = _piecewise(recent_np, [
            (-np_center, 0), (0, 33), (np_center, 67), (3 * np_center, 100)
        ])
        if recent_np >= np_center * 2:
            highlights.append(f"净利润 YoY 均 {recent_np:.1f}% 高增")
        elif recent_np < 0:
            risks.append(f"净利润 YoY 均 {recent_np:.1f}% 负增长")
    else:
        np_s = 50.0

    # ── 增速加速度 (20%) ───────────────────────────────────────────────
    # 近 4 季 vs 前 4 季平均 YoY 对比
    acc_s = 50.0   # 默认中性（数据不足时）
    if n_quarters >= 8 and len(rev_yoy) >= 8:
        recent4  = float(np.mean(rev_yoy[:4]))
        prior4   = float(np.mean(rev_yoy[4:8]))
        accel    = recent4 - prior4
        acc_s = _piecewise(accel, [
            (-30, 0), (-10, 30), (0, 50), (10, 70), (30, 100)
        ])
        if accel > 10:
            highlights.append(f"营收增速加速 Δ{accel:.1f}pct")
        elif accel < -10:
            risks.append(f"营收增速减速 Δ{accel:.1f}pct")

    score = round(rev_s * 0.40 + np_s * 0.40 + acc_s * 0.20, 2)
    return score, highlights, risks


# ═════════════════════════════════════════════════════════════════════════════
# 估值评分（低估值 = 高分）
# ═════════════════════════════════════════════════════════════════════════════

async def _score_valuation(
    conn: asyncpg.Connection,
    symbol: str,
    cutoff: date,
    fw_name: str,
    market: str,
) -> tuple[float, list[str], list[str]]:
    highlights: list[str] = []
    risks:      list[str] = []

    # 当前 PE/PB
    curr = await _fetch_valuation_current(conn, symbol, cutoff)
    pe  = curr["pe_ttm"]
    pb  = curr["pb"]

    if pe is None and pb is None:
        return 50.0, highlights, risks

    # 同行业 peers
    peers = _get_watchlist_by_framework(fw_name)
    peer_df = await _fetch_peer_valuations(conn, peers, cutoff)

    # ── PE 同行业分位 (50%) ──────────────────────────────────────────────
    pe_peer_s = 50.0
    if pe is not None and pe > 0:
        peer_pe = peer_df["pe_ttm"].dropna().values
        peer_pe = peer_pe[(peer_pe > 0) & (peer_pe < 500)]  # 剔除异常
        if len(peer_pe) >= 2:
            pct = _percentile_of(pe, peer_pe)
            pe_peer_s = (1.0 - pct) * 100.0   # 低PE → 高分
            if pct >= 0.80:
                risks.append(f"PE {pe:.1f}x 处于同行业 {pct*100:.0f}% 分位，偏贵")
            elif pct <= 0.20:
                highlights.append(f"PE {pe:.1f}x 处于同行业 {pct*100:.0f}% 分位，低估")
        elif len(peer_pe) == 1:
            # 只有自身或一个同行，降级用自身历史
            pe_peer_s = 50.0

    # ── PE 自身历史分位 (30%) ──────────────────────────────────────────
    pe_hist_s = 50.0
    if pe is not None and pe > 0:
        hist_arr = await _fetch_valuation_history(conn, symbol, cutoff, years=3)
        if len(hist_arr) >= 30:
            pct_h = _percentile_of(pe, hist_arr)
            pe_hist_s = (1.0 - pct_h) * 100.0
            if pct_h >= 0.80:
                risks.append(f"PE 处于自身 3 年历史 {pct_h*100:.0f}% 分位，历史偏高")
            elif pct_h <= 0.20:
                highlights.append(f"PE 处于自身 3 年历史 {pct_h*100:.0f}% 分位，历史低位")

    # ── PB 同行业分位 (20%) ──────────────────────────────────────────────
    pb_peer_s = 50.0
    if pb is not None and pb > 0:
        peer_pb = peer_df["pb"].dropna().values
        peer_pb = peer_pb[(peer_pb > 0) & (peer_pb < 100)]
        if len(peer_pb) >= 2:
            pct_pb = _percentile_of(pb, peer_pb)
            pb_peer_s = (1.0 - pct_pb) * 100.0

    score = round(pe_peer_s * 0.50 + pe_hist_s * 0.30 + pb_peer_s * 0.20, 2)
    return score, highlights, risks


# ═════════════════════════════════════════════════════════════════════════════
# 财务健康度评分
# ═════════════════════════════════════════════════════════════════════════════

def _score_health(
    df: pd.DataFrame,
    thr: dict,
) -> tuple[float, list[str], list[str]]:
    highlights: list[str] = []
    risks:      list[str] = []

    # ── 资产负债率 (40%) ────────────────────────────────────────────────
    dr_vals = df["debt_ratio"].dropna().tolist()
    dr_latest = dr_vals[0] if dr_vals else None

    dr_s = 50.0
    if dr_latest is not None:
        max_dr = thr.get("debt_ratio_max", 66.0)
        dr_s = _piecewise(dr_latest, [
            (0, 100), (30, 85), (50, 55), (max_dr, 10), (85, 0)
        ])
        if dr_latest > max_dr:
            risks.append(f"资产负债率 {dr_latest:.1f}% 偏高")
        elif dr_latest < 30:
            highlights.append(f"资产负债率 {dr_latest:.1f}% 财务稳健")

    # ── 流动比率 (30%) ─────────────────────────────────────────────────
    cr_vals = df["current_ratio"].dropna().tolist()
    cr_latest = cr_vals[0] if cr_vals else None

    cr_s = 50.0
    if cr_latest is not None:
        full_cr = thr.get("current_ratio_full", 2.5)
        cr_s = _piecewise(cr_latest, [
            (0, 0), (0.8, 10), (1.0, 30), (1.5, 65), (full_cr, 100)
        ])
        if cr_latest < 1.0:
            risks.append(f"流动比率 {cr_latest:.2f} < 1，短期偿债风险")
        elif cr_latest >= full_cr:
            highlights.append(f"流动比率 {cr_latest:.2f} 充裕")

    # ── 商誉/净资产 (30%) ────────────────────────────────────────────
    gw_vals = df["goodwill"].dropna().tolist()
    eq_col  = (df["total_assets"] - df["total_liab"]).dropna().tolist()

    gw_s = 80.0  # 无商誉默认良好
    if gw_vals and eq_col:
        gw = gw_vals[0]
        eq = eq_col[0] if eq_col else None
        if eq and abs(eq) > 1:
            max_gw = thr.get("goodwill_ratio_max", 0.50)
            gw_ratio = abs(gw) / abs(eq)
            gw_s = _piecewise(gw_ratio, [
                (0, 100), (0.10, 80), (0.30, 50), (max_gw, 10), (0.80, 0)
            ])
            if gw_ratio > max_gw:
                risks.append(f"商誉/净资产 {gw_ratio*100:.0f}% 较高，减值风险")

    score = round(dr_s * 0.40 + cr_s * 0.30 + gw_s * 0.30, 2)
    return score, highlights, risks


# ═════════════════════════════════════════════════════════════════════════════
# 主入口
# ═════════════════════════════════════════════════════════════════════════════

async def analyze_fundamental(
    pool: asyncpg.Pool,
    symbol: str,
    market: str,
    cutoff: date | None = None,
) -> FundamentalResult | None:
    """
    计算个股在 cutoff 日期的基本面四维评分。

    Args:
        pool:   asyncpg 连接池（由调用方管理）。
        symbol: 股票代码。
        market: "CN" 或 "US"。
        cutoff: 截止日期（PIT 安全，available_date <= cutoff）。

    Returns:
        FundamentalResult，或 None（无财报数据）。
    """
    if cutoff is None:
        cutoff = date.today()

    fw_name, weights = _get_framework(symbol)
    thr = _load_thresholds()

    async with pool.acquire() as conn:
        df = await _fetch_financials(conn, symbol, cutoff, n_quarters=12)
        if df.empty:
            log.warning(f"fundamental: no financials for {symbol} <= {cutoff}")
            return None

        n_q = len(df)

        # ── 各维度评分 ───────────────────────────────────────────────────
        profit_s, ph, pr = _score_profitability(df, thr["profit"], market)
        growth_s,  gh, gr = _score_growth(df, thr["growth"], market, n_q)
        health_s,  hh, hr = _score_health(df, thr["health"])
        val_s, vh, vr = await _score_valuation(conn, symbol, cutoff, fw_name, market)
        curr_val = await _fetch_valuation_current(conn, symbol, cutoff)

    # ── 加权综合 ─────────────────────────────────────────────────────────
    w = weights
    fund_score = round(
        profit_s * w["profitability"] +
        growth_s * w["growth"]        +
        val_s    * w["valuation"]     +
        health_s * w["health"],
        2,
    )

    # ── 汇总 highlights / risks（各维度最多 1 条，共最多 3 条）──────────
    all_highlights = (ph + gh + hh + vh)[:3]
    all_risks      = (pr + gr + hr + vr)[:3]

    result = FundamentalResult(
        symbol=symbol,
        trade_date=cutoff,
        market=market,
        profitability_score=profit_s,
        growth_score=growth_s,
        valuation_score=val_s,
        health_score=health_s,
        fundamental_score=fund_score,
        framework_used=fw_name,
        data_quarters=n_q,
        highlights=all_highlights,
        risks=all_risks,
        detail={
            "weights": dict(w),
            "profitability_detail": {
                "roe_ttm_latest": _flt(df["roe_ttm"].iloc[0]) if not df.empty else None,
                "gross_margin_latest": _flt(df["gross_margin"].iloc[0]) if not df.empty else None,
            },
            "growth_detail": {
                "revenue_yoy_recent4": float(np.mean(df["revenue_yoy"].dropna().tolist()[:4])) if not df["revenue_yoy"].dropna().empty else None,
                "np_yoy_recent4": float(np.mean(df["np_yoy"].dropna().tolist()[:4])) if not df["np_yoy"].dropna().empty else None,
            },
            "valuation_detail": {
                "pe_ttm": curr_val["pe_ttm"],
                "pb":     curr_val["pb"],
            },
            "health_detail": {
                "debt_ratio": _flt(df["debt_ratio"].iloc[0]) if not df.empty else None,
                "current_ratio": _flt(df["current_ratio"].iloc[0]) if not df.empty else None,
            },
        },
    )
    log.info(str(result))
    return result
