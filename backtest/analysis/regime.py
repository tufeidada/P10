"""
backtest/analysis/regime.py — PIT 版市场 Regime 检测

⚠️ 独立实现：禁止 import P10 主项目 core/ 下任何模块。
   所有数据访问通过 pit_loader.py 的 asyncpg pool 直接查询。

四维度评分 (各 0-100):
  trend_score      — 指数均线排列 + 价格结构
  volatility_score — HV20 历史分位 + 跌停占比(CN) / VIX(US)
  breadth_score    — 涨跌比 + 新高新低 + 成交量广度
  liquidity_score  — 北向资金(CN) + 融资余额(CN) + 市场成交量

Regime 模式 (来自 regime_params.yaml):
  offense / cautious_offense / defense / risk_off
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any

import asyncpg
import numpy as np
import yaml

log = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).resolve().parents[2] / "backtest" / "config"
_PARAMS_PATH = _CONFIG_DIR / "regime_params.yaml"


# ═════════════════════════════════════════════════════════════════════════════
# 配置加载
# ═════════════════════════════════════════════════════════════════════════════

@lru_cache(maxsize=1)
def _load_params() -> dict[str, Any]:
    with open(_PARAMS_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ═════════════════════════════════════════════════════════════════════════════
# 数据结构
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class RegimeResult:
    trade_date:       date
    market:           str
    trend_score:      float
    volatility_score: float
    breadth_score:    float
    liquidity_score:  float
    regime_mode:      str          # offense / cautious_offense / defense / risk_off
    trend_direction:  str          # up / neutral / down
    volatility_env:   str          # low / high
    dimension_weights: dict[str, float]
    params:           dict[str, Any]   # 该 mode 的完整参数
    detail:           dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return (
            f"Regime({self.market} {self.trade_date} | {self.regime_mode} | "
            f"trend={self.trend_score:.1f} vol={self.volatility_score:.1f} "
            f"breadth={self.breadth_score:.1f} liq={self.liquidity_score:.1f})"
        )


# ═════════════════════════════════════════════════════════════════════════════
# 工具函数
# ═════════════════════════════════════════════════════════════════════════════

def _trend_score_from_series(values: np.ndarray) -> float:
    """线性回归斜率 + 近远期均值动量综合趋势得分 (0-100, 50 为中性)。"""
    valid = values[~np.isnan(values)]
    if len(valid) < 5:
        return 50.0
    x = np.arange(len(valid), dtype=np.float64)
    mx, my = np.mean(x), np.mean(valid)
    denom = np.sum((x - mx) ** 2)
    slope = 0.0 if denom == 0 else np.sum((x - mx) * (valid - my)) / denom
    std = np.std(valid)
    norm_slope = 0.0 if std == 0 else slope / std * len(valid)

    mid = len(valid) // 2
    early_m = np.mean(valid[:mid])
    momentum = 0.0 if early_m == 0 else (np.mean(valid[mid:]) - early_m) / abs(early_m)

    slope_s = np.clip(50.0 + norm_slope / 3.0 * 50.0, 0.0, 100.0)
    mom_s   = np.clip(50.0 + momentum   / 0.2  * 50.0, 0.0, 100.0)
    return round(float(slope_s * 0.6 + mom_s * 0.4), 2)


# ═════════════════════════════════════════════════════════════════════════════
# 趋势评分
# ═════════════════════════════════════════════════════════════════════════════

async def _fetch_index_close(
    conn: asyncpg.Connection,
    index_code: str,
    cutoff: date,
    lookback: int = 300,
) -> np.ndarray | None:
    rows = await conn.fetch("""
        SELECT close FROM index_daily
        WHERE index_code = $1 AND trade_date <= $2
        ORDER BY trade_date DESC LIMIT $3
    """, index_code, cutoff, lookback)
    if not rows or len(rows) < 30:
        return None
    return np.array([float(r["close"]) for r in reversed(rows)], dtype=np.float64)


def _ma_alignment_score(close: np.ndarray) -> float:
    """均线多头排列得分 (0-40)。MA5>MA20>MA60>MA150>MA200，每对正确 +8。"""
    periods = [5, 20, 60, 150, 200]
    if len(close) < 200:
        return 20.0  # 数据不足→中性
    mas = {}
    for p in periods:
        kernel = np.ones(p) / p
        ma = np.convolve(close, kernel, "full")[:len(close)]
        ma[:p - 1] = np.nan
        mas[p] = ma[-1]
    if any(np.isnan(v) for v in mas.values()):
        return 20.0
    pairs = [(5, 20), (20, 60), (60, 150), (150, 200)]
    return sum(8.0 for s, l in pairs if mas[s] > mas[l])


def _price_vs_ma150_score(close: np.ndarray) -> float:
    """价格相对 MA150 位置得分 (0-30)。"""
    if len(close) < 150:
        return 15.0
    kernel = np.ones(150) / 150
    ma150 = np.convolve(close, kernel, "full")[:len(close)]
    ma150_val = ma150[-1]
    if np.isnan(ma150_val) or ma150_val == 0:
        return 15.0
    ratio = close[-1] / ma150_val - 1
    # ratio: -0.2→0, 0→15, +0.2→30
    return float(np.clip(15.0 + ratio / 0.2 * 15.0, 0.0, 30.0))


def _momentum_score(close: np.ndarray) -> float:
    """近期 20 日涨跌幅得分 (0-30)。"""
    if len(close) < 21:
        return 15.0
    ret_20d = close[-1] / close[-21] - 1
    # ret: -0.10→0, 0→15, +0.10→30
    return float(np.clip(15.0 + ret_20d / 0.10 * 15.0, 0.0, 30.0))


async def _compute_trend_score_cn(conn: asyncpg.Connection, cutoff: date) -> float:
    scores = []
    for code in ("HS300", "ZZ1000"):
        close = await _fetch_index_close(conn, code, cutoff)
        if close is None:
            continue
        s = _ma_alignment_score(close) + _price_vs_ma150_score(close) + _momentum_score(close)
        scores.append(min(100.0, max(0.0, s)))
    return round(float(np.mean(scores)), 2) if scores else 50.0


async def _compute_trend_score_us(conn: asyncpg.Connection, cutoff: date) -> float:
    scores = []
    for code in ("SPY", "QQQ"):
        close = await _fetch_index_close(conn, code, cutoff)
        if close is None:
            continue
        s = _ma_alignment_score(close) + _price_vs_ma150_score(close) + _momentum_score(close)
        scores.append(min(100.0, max(0.0, s)))
    return round(float(np.mean(scores)), 2) if scores else 50.0


# ═════════════════════════════════════════════════════════════════════════════
# 波动率评分
# ═════════════════════════════════════════════════════════════════════════════

def _hv20_percentile(close: np.ndarray, lookback: int = 250) -> float:
    """HV20 在过去 lookback 天中的百分位 (0-100)。"""
    if len(close) < 22:
        return 50.0
    log_ret = np.log(close[1:] / close[:-1])
    hv_vals = []
    for i in range(20, len(log_ret) + 1):
        hv_vals.append(np.std(log_ret[i - 20:i], ddof=1) * np.sqrt(252) * 100)
    hv_arr = np.array(hv_vals)
    recent = hv_arr[-lookback:] if len(hv_arr) > lookback else hv_arr
    current = hv_arr[-1]
    return round(float(np.sum(recent <= current) / len(recent) * 100), 2)


async def _compute_volatility_score_cn(conn: asyncpg.Connection, cutoff: date) -> float:
    # ── HV20 分位 (60%) ──
    rows = await conn.fetch("""
        SELECT close FROM index_daily
        WHERE index_code = 'HS300' AND trade_date <= $1
        ORDER BY trade_date DESC LIMIT 280
    """, cutoff)
    hv_score = 50.0
    if rows and len(rows) >= 30:
        close = np.array([float(r["close"]) for r in reversed(rows)], dtype=np.float64)
        hv_score = _hv20_percentile(close)

    # ── 跌停占比 (40%) ——来自 market_breadth_daily ──
    row = await conn.fetchrow("""
        SELECT limit_up_count, limit_down_count
        FROM market_breadth_daily
        WHERE trade_date <= $1
        ORDER BY trade_date DESC LIMIT 1
    """, cutoff)
    ld_score = 50.0
    if row and row["limit_up_count"] is not None and row["limit_down_count"] is not None:
        up, down = int(row["limit_up_count"]), int(row["limit_down_count"])
        total = up + down
        if total > 0:
            ld_score = min(down / total / 0.5 * 100.0, 100.0)

    return round(hv_score * 0.6 + ld_score * 0.4, 2)


async def _compute_volatility_score_us(conn: asyncpg.Connection, cutoff: date) -> float:
    """US 波动率：VIX 映射 (0-100)。"""
    _VIX_BP = [(0,0),(12,20),(15,35),(20,50),(25,65),(30,80),(40,100)]
    rows = await conn.fetch("""
        SELECT close FROM index_daily
        WHERE index_code = 'VIX' AND trade_date <= $1
        ORDER BY trade_date DESC LIMIT 10
    """, cutoff)
    if not rows:
        return 50.0
    vix = float(rows[0]["close"])
    for i in range(len(_VIX_BP) - 1):
        x0, y0 = _VIX_BP[i]; x1, y1 = _VIX_BP[i + 1]
        if x0 <= vix <= x1:
            return round(y0 + (vix - x0) / (x1 - x0) * (y1 - y0), 2)
    return 100.0 if vix >= 40 else 0.0


# ═════════════════════════════════════════════════════════════════════════════
# 广度评分
# ═════════════════════════════════════════════════════════════════════════════

async def _compute_breadth_score_cn(conn: asyncpg.Connection, cutoff: date) -> float:
    scores: list[tuple[float, float]] = []  # (score, weight)

    # ── 涨跌比 5日均值 (40%) ──
    rows = await conn.fetch("""
        SELECT advancing_count, declining_count
        FROM market_breadth_daily
        WHERE trade_date <= $1
        ORDER BY trade_date DESC LIMIT 5
    """, cutoff)
    if rows:
        ratios = []
        for r in rows:
            adv, dec = r["advancing_count"] or 0, r["declining_count"] or 0
            if adv + dec > 0:
                ratios.append(adv / (adv + dec))
        if ratios:
            avg = np.mean(ratios)   # 0~1，0.5 是均衡
            s = np.clip(avg / 0.5 * 50.0, 0.0, 100.0)
            scores.append((float(s), 0.40))

    # ── 新高新低差值 (30%) ──
    row = await conn.fetchrow("""
        SELECT new_high_count, new_low_count
        FROM market_breadth_daily
        WHERE trade_date <= $1
        ORDER BY trade_date DESC LIMIT 1
    """, cutoff)
    if row and row["new_high_count"] is not None and row["new_low_count"] is not None:
        diff_ratio = (row["new_high_count"] - row["new_low_count"]) / 5000.0
        s = np.clip(50.0 + diff_ratio / 0.05 * 50.0, 0.0, 100.0)
        scores.append((float(s), 0.30))

    # ── 涨停/跌停比 (30%) ──
    row2 = await conn.fetchrow("""
        SELECT limit_up_count, limit_down_count
        FROM market_breadth_daily
        WHERE trade_date <= $1
        ORDER BY trade_date DESC LIMIT 1
    """, cutoff)
    if row2 and row2["limit_up_count"] is not None and row2["limit_down_count"] is not None:
        lu, ld = int(row2["limit_up_count"]), int(row2["limit_down_count"])
        if lu + ld > 0:
            s = np.clip(lu / (lu + ld) / 0.5 * 50.0, 0.0, 100.0)
            scores.append((float(s), 0.30))

    if not scores:
        return 50.0
    total_w = sum(w for _, w in scores)
    return round(sum(s * w / total_w for s, w in scores), 2)


async def _compute_breadth_score_us(conn: asyncpg.Connection, cutoff: date) -> float:
    """US 广度：SPY 近期收益率 + QQQ/SPY 相对强度。"""
    scores: list[tuple[float, float]] = []

    for code, w in (("SPY", 0.6), ("QQQ", 0.4)):
        rows = await conn.fetch("""
            SELECT close FROM index_daily
            WHERE index_code = $1 AND trade_date <= $2
            ORDER BY trade_date DESC LIMIT 25
        """, code, cutoff)
        if not rows or len(rows) < 6:
            continue
        close = np.array([float(r["close"]) for r in reversed(rows)], dtype=np.float64)
        ret5  = close[-1] / close[-6]  - 1
        ret20 = close[-1] / close[-21] - 1 if len(close) >= 21 else ret5
        s = np.clip(50.0 + (ret5 + ret20) / 2 / 0.05 * 50.0, 0.0, 100.0)
        scores.append((float(s), w))

    if not scores:
        return 50.0
    total_w = sum(w for _, w in scores)
    return round(sum(s * w / total_w for s, w in scores), 2)


# ═════════════════════════════════════════════════════════════════════════════
# 流动性评分
# ═════════════════════════════════════════════════════════════════════════════

async def _compute_liquidity_score_cn(conn: asyncpg.Connection, cutoff: date) -> float:
    scores: list[tuple[float, float]] = []

    # ── 北向资金 20日趋势 (40%) ──
    rows = await conn.fetch("""
        SELECT total_net_buy FROM northbound_daily
        WHERE trade_date <= $1
        ORDER BY trade_date DESC LIMIT 20
    """, cutoff)
    if rows and len(rows) >= 5:
        vals = np.array([float(r["total_net_buy"]) for r in reversed(rows)
                         if r["total_net_buy"] is not None], dtype=np.float64)
        if len(vals) >= 5:
            scores.append((_trend_score_from_series(vals), 0.40))

    # ── 融资余额 20日趋势 (35%) ——从 margin_daily 全市场汇总 ──
    rows2 = await conn.fetch("""
        SELECT trade_date, SUM(rzye) AS total
        FROM margin_daily
        WHERE trade_date <= $1
        GROUP BY trade_date
        ORDER BY trade_date DESC LIMIT 20
    """, cutoff)
    if rows2 and len(rows2) >= 5:
        vals2 = np.array([float(r["total"]) for r in reversed(rows2)
                          if r["total"] is not None], dtype=np.float64)
        if len(vals2) >= 5:
            scores.append((_trend_score_from_series(vals2), 0.35))

    # ── 市场成交量 vs 20日均量 (25%) ──
    rows3 = await conn.fetch("""
        SELECT trade_date, SUM(amount) AS total_amount
        FROM market_bars_daily
        WHERE market = 'CN' AND trade_date <= $1
        GROUP BY trade_date
        ORDER BY trade_date DESC LIMIT 25
    """, cutoff)
    if rows3 and len(rows3) >= 5:
        amounts = np.array([float(r["total_amount"]) for r in reversed(rows3)
                            if r["total_amount"] is not None], dtype=np.float64)
        if len(amounts) >= 2:
            cur = amounts[-1]
            avg = np.mean(amounts[-21:-1]) if len(amounts) > 20 else np.mean(amounts[:-1])
            ratio = cur / avg if avg > 0 else 1.0
            s = float(np.clip(50.0 + (ratio - 1.0) / 1.0 * 50.0, 0.0, 100.0))
            scores.append((s, 0.25))

    if not scores:
        return 50.0
    total_w = sum(w for _, w in scores)
    return round(sum(s * w / total_w for s, w in scores), 2)


async def _compute_liquidity_score_us(conn: asyncpg.Connection, cutoff: date) -> float:
    """US 流动性：SPY 成交量 vs 均量 + VIX 低位加分。"""
    scores: list[tuple[float, float]] = []

    rows = await conn.fetch("""
        SELECT volume FROM index_daily
        WHERE index_code = 'SPY' AND trade_date <= $1
        ORDER BY trade_date DESC LIMIT 25
    """, cutoff)
    if rows and len(rows) >= 5:
        vols = np.array([float(r["volume"]) for r in reversed(rows)], dtype=np.float64)
        cur = vols[-1]; avg = np.mean(vols[-21:-1]) if len(vols) > 20 else np.mean(vols[:-1])
        ratio = cur / avg if avg > 0 else 1.0
        s = float(np.clip(50.0 + (ratio - 1.0) / 1.0 * 50.0, 0.0, 100.0))
        scores.append((s, 0.50))

    vix_rows = await conn.fetch("""
        SELECT close FROM index_daily
        WHERE index_code = 'VIX' AND trade_date <= $1
        ORDER BY trade_date DESC LIMIT 1
    """, cutoff)
    if vix_rows:
        vix = float(vix_rows[0]["close"])
        # VIX < 15 → 高流动性, VIX > 30 → 低流动性
        s = float(np.clip(100.0 - (vix - 15.0) / 15.0 * 50.0, 0.0, 100.0))
        scores.append((s, 0.50))

    if not scores:
        return 50.0
    total_w = sum(w for _, w in scores)
    return round(sum(s * w / total_w for s, w in scores), 2)


# ═════════════════════════════════════════════════════════════════════════════
# 主入口：detect_regime
# ═════════════════════════════════════════════════════════════════════════════

async def detect_regime(
    pool: asyncpg.Pool,
    market: str = "CN",
    cutoff: date | None = None,
) -> RegimeResult:
    """
    检测指定日期的市场 Regime。

    Args:
        pool:   asyncpg 连接池（必须由调用方管理生命周期）。
        market: "CN" 或 "US"。
        cutoff: 截止交易日（PIT 安全，只用 <= cutoff 的数据）。

    Returns:
        RegimeResult 数据类。
    """
    if cutoff is None:
        cutoff = date.today()

    params = _load_params()

    async with pool.acquire() as conn:
        if market == "CN":
            trend_score      = await _compute_trend_score_cn(conn, cutoff)
            volatility_score = await _compute_volatility_score_cn(conn, cutoff)
            breadth_score    = await _compute_breadth_score_cn(conn, cutoff)
            liquidity_score  = await _compute_liquidity_score_cn(conn, cutoff)
        else:
            trend_score      = await _compute_trend_score_us(conn, cutoff)
            volatility_score = await _compute_volatility_score_us(conn, cutoff)
            breadth_score    = await _compute_breadth_score_us(conn, cutoff)
            liquidity_score  = await _compute_liquidity_score_us(conn, cutoff)

    # ── 分类 ──────────────────────────────────────────────────────────────
    thr = params["thresholds"]
    trend_dir = ("up"   if trend_score      > thr["trend"]["up"]
                 else "down" if trend_score < thr["trend"]["down"]
                 else "neutral")
    vol_env   = "high" if volatility_score  > thr["volatility"]["high"] else "low"

    # ── 2×2 矩阵映射 ────────────────────────────────────────────────────
    key = f"{trend_dir if trend_dir == 'up' else 'sideways' if trend_dir == 'neutral' else 'down'}_{vol_env}"
    # normalize: neutral → sideways in yaml key
    key = key.replace("neutral", "sideways")
    mode = params["regime_map"][key]

    # ── 修正规则 ─────────────────────────────────────────────────────────
    corr = thr.get("corrections", {})
    if mode == "offense" and breadth_score < corr.get("breadth_weak", 30):
        mode = "cautious_offense"
        log.info(f"Regime 修正: offense→cautious_offense (breadth={breadth_score:.1f})")
    if mode == "defense" and liquidity_score > corr.get("liquidity_strong", 70):
        mode = "cautious_offense"
        log.info(f"Regime 修正: defense→cautious_offense (liquidity={liquidity_score:.1f})")

    # ── 读取该模式参数 ──────────────────────────────────────────────────
    mode_params = params["params"][mode]
    weights     = mode_params["dimension_weights"]

    detail = {
        "trend_direction": trend_dir,
        "volatility_env": vol_env,
        "sub_scores": {
            "trend":      trend_score,
            "volatility": volatility_score,
            "breadth":    breadth_score,
            "liquidity":  liquidity_score,
        },
    }

    result = RegimeResult(
        trade_date=cutoff, market=market,
        trend_score=trend_score, volatility_score=volatility_score,
        breadth_score=breadth_score, liquidity_score=liquidity_score,
        regime_mode=mode, trend_direction=trend_dir, volatility_env=vol_env,
        dimension_weights=weights, params=mode_params, detail=detail,
    )
    log.info(str(result))
    return result
