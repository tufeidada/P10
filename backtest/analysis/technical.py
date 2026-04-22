"""
backtest/analysis/technical.py — PIT 版个股技术面分析

⚠️ 独立实现：禁止 import P10 主项目 core/ 下任何模块。
   数据通过 asyncpg pool 直接查询，严格遵守 PIT 约束（只用 <= cutoff 数据）。

日线评分 (0-100):
  MA 排列 (0-15)  — 4 对均线多头排列，每对 3.75 分
  RSI      (0-25) — RSI-14 标准化
  MACD     (0-25) — 柱状图方向 + 动量
  Stage    (0-20) — Weinstein Stage 1/2/3/4
  RS Rank  (0-15) — 全市场 63 日收益百分位

周线评分 (0-100):  [基于 lookback=1200 日 → ~240 周]
  Stage    (0-40) — 周线 MA30 + 30 周斜率（同日线 MA150 概念）
  RSI      (0-35) — 周线 RSI-14
  MACD     (0-25) — 周线 MACD 柱状图

stage_confidence 降权:
  slope 在阈值边界 ±0.5% 内 → 0.5
  price 在 MA150  ±3%  以内 → 0.7
  其余                      → 1.0
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import asyncpg
import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

_SLOPE_FLAT = 0.01
_PRICE_HIGH = 0.10
_DIR_BULLISH = 65.0
_DIR_BEARISH = 35.0


# ═════════════════════════════════════════════════════════════════════════════
# 数据结构
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class TimeframeResult:
    timeframe:    str           # "daily" / "weekly"
    direction:    str           # "bullish" / "neutral" / "bearish"
    score:        float         # 0-100
    rsi:          float | None
    macd_bullish: bool | None
    stage:        int | None    # Weinstein Stage (weekly 基于周线 MA30)
    above_ma150:  bool | None   # daily: 收盘>MA150; weekly: 收盘>MA30
    detail:       dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        rsi_s = f"{self.rsi:.1f}" if self.rsi is not None else "N/A"
        return (
            f"[{self.timeframe}] dir={self.direction} score={self.score:.1f} "
            f"stage={self.stage} rsi={rsi_s} macd_bull={self.macd_bullish}"
        )


@dataclass
class TechnicalResult:
    symbol:             str
    trade_date:         date
    market:             str
    daily:              TimeframeResult
    weekly:             TimeframeResult | None
    combined_direction: str
    combined_score:     float
    confidence_adj:     float
    stage_confidence:   float
    detail:             dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return (
            f"Technical({self.symbol} {self.trade_date} | {self.combined_direction} "
            f"score={self.combined_score:.1f} conf={self.confidence_adj:.2f} "
            f"stage_conf={self.stage_confidence})"
        )


# ═════════════════════════════════════════════════════════════════════════════
# 指标计算
# ═════════════════════════════════════════════════════════════════════════════

def _rsi(close: np.ndarray, period: int = 14) -> float | None:
    if len(close) < period + 1:
        return None
    delta = np.diff(close.astype(np.float64))
    gain  = np.where(delta > 0, delta, 0.0)
    loss  = np.where(delta < 0, -delta, 0.0)
    alpha = 1.0 / period
    ag, al = gain[0], loss[0]
    for g, l in zip(gain[1:], loss[1:]):
        ag = ag * (1 - alpha) + g * alpha
        al = al * (1 - alpha) + l * alpha
    if al == 0:
        return 100.0
    return round(float(100.0 - 100.0 / (1.0 + ag / al)), 4)


def _macd(close: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9
          ) -> tuple[float | None, float | None, float | None]:
    if len(close) < slow + signal:
        return None, None, None
    c = close.astype(np.float64)
    ef = pd.Series(c).ewm(span=fast, adjust=False).mean().values
    es = pd.Series(c).ewm(span=slow, adjust=False).mean().values
    dif = ef - es
    dea = pd.Series(dif).ewm(span=signal, adjust=False).mean().values
    return float(dif[-1]), float(dea[-1]), float((dif - dea)[-1] * 2)


def _ma(close: np.ndarray, period: int) -> float | None:
    if len(close) < period:
        return None
    return float(np.mean(close[-period:]))


# ═════════════════════════════════════════════════════════════════════════════
# 周线 Stage 判定（MA30 = 周线版 Weinstein）
# ═════════════════════════════════════════════════════════════════════════════

def _weekly_stage(close_arr: np.ndarray) -> int | None:
    """
    周线 Weinstein Stage，基于周线 MA30（≈日线 MA150 概念）。
    需要至少 60 周（30周MA + 30周斜率）。
    阈值与日线相同: SLOPE_FLAT=1%, PRICE_HIGH=10%。
    """
    if len(close_arr) < 60:
        return None
    ma30_now    = float(np.mean(close_arr[-30:]))
    ma30_30w_ago = float(np.mean(close_arr[-60:-30]))
    if ma30_30w_ago == 0:
        return None
    slope = (ma30_now - ma30_30w_ago) / ma30_30w_ago
    price_vs = close_arr[-1] / ma30_now - 1

    ma_up   = slope >  _SLOPE_FLAT
    ma_down = slope < -_SLOPE_FLAT
    ma_flat = not ma_up and not ma_down
    above   = price_vs > 0
    high    = price_vs > _PRICE_HIGH

    if ma_up and above:        return 2
    if ma_down and not above:  return 4
    if ma_flat and high:       return 3
    if ma_flat and not above:  return 1
    if ma_up and not above:    return 1   # 边缘：MA向上但价格未跟上
    if ma_down and above:      return 3   # 边缘：MA向下但价格仍在上方
    return 0


# ═════════════════════════════════════════════════════════════════════════════
# 评分函数
# ═════════════════════════════════════════════════════════════════════════════

def _score_ma_alignment(
    ma5: float | None, ma20: float | None, ma60: float | None,
    ma150: float | None, ma200: float | None,
) -> float:
    """MA 多头排列得分 (0-15)。4 对，每对 3.75 分。"""
    pairs = [(ma5, ma20), (ma20, ma60), (ma60, ma150), (ma150, ma200)]
    score = 0.0
    for short, long_ in pairs:
        if short is not None and long_ is not None and short > long_:
            score += 3.75
    return score


def _score_rsi(rsi: float | None, max_score: float = 25.0) -> float:
    """RSI → 0-max_score。RSI 30→0, RSI 70→max_score。"""
    if rsi is None:
        return max_score / 2
    return float(np.clip((rsi - 30.0) / 40.0 * max_score, 0.0, max_score))


def _score_macd(hist: float | None, prev_hist: float | None,
                max_score: float = 25.0) -> float:
    """MACD 柱状 → 0-max_score。"""
    if hist is None:
        return max_score / 2
    if hist > 0:
        if prev_hist is not None and hist > prev_hist:
            return max_score            # 正值且扩张（最强）
        return max_score * 0.72         # 正值但收缩
    else:
        if prev_hist is not None and hist > prev_hist:
            return max_score * 0.32     # 负值但收窄（底背离迹象）
        return 0.0


def _score_stage(stage: int | None, stage_conf: float = 1.0, max_score: float = 20.0) -> float:
    """
    Weinstein Stage → 0-max_score，应用 stage_confidence 向中性靠拢。

    stage_confidence=1.0: 完全按 Stage 映射
    stage_confidence=0.5: 得分向中性 (max/2) 靠拢一半
    base 比例: Stage2=100%, Stage1=25%, Stage3=25%, Stage4=0%, Stage0=50%
    中性基准 = max_score * 0.5
    """
    _BASE_RATIO = {2: 1.0, 1: 0.25, 3: 0.25, 4: 0.0, 0: 0.5}
    ratio   = _BASE_RATIO.get(stage, 0.5) if stage is not None else 0.5
    base    = ratio * max_score
    neutral = 0.5 * max_score
    return round(neutral + (base - neutral) * stage_conf, 2)


def _score_rs_rank(rs_rank: float | None) -> float:
    """RS Rank (0-1 百分位) → 0-15 分。"""
    if rs_rank is None:
        return 7.5
    return float(np.clip(rs_rank * 15.0, 0.0, 15.0))


def _direction(score: float) -> str:
    if score >= _DIR_BULLISH:
        return "bullish"
    if score <= _DIR_BEARISH:
        return "bearish"
    return "neutral"


# ═════════════════════════════════════════════════════════════════════════════
# stage_confidence
# ═════════════════════════════════════════════════════════════════════════════

def _compute_stage_confidence(
    ma150_slope: float | None,
    price_vs_ma150: float | None,
) -> float:
    if ma150_slope is not None:
        if abs(abs(ma150_slope) - _SLOPE_FLAT) < 0.005:
            return 0.5
    if price_vs_ma150 is not None:
        if abs(price_vs_ma150) < 0.03:
            return 0.7
    return 1.0


# ═════════════════════════════════════════════════════════════════════════════
# 数据查询
# ═════════════════════════════════════════════════════════════════════════════

async def _fetch_features(
    conn: asyncpg.Connection,
    symbol: str,
    cutoff: date,
    lookback: int = 5,
) -> list[asyncpg.Record]:
    return await conn.fetch("""
        SELECT trade_date, ma5, ma10, ma20, ma60, ma150, ma200,
               ma20_slope, ma60_slope,
               rsi_14, macd_dif, macd_dea, macd_hist,
               atr_14, adx_14, plus_di, minus_di,
               stage, rs_rank,
               ret_5d, ret_20d, ret_60d,
               dist_20d_high, pct_in_20d_range, vol_ratio_5d
        FROM features_daily
        WHERE symbol = $1
          AND trade_date <= $2
        ORDER BY trade_date DESC
        LIMIT $3
    """, symbol, cutoff, lookback)


async def _fetch_bars(
    conn: asyncpg.Connection,
    symbol: str,
    cutoff: date,
    lookback: int = 1200,
) -> list[asyncpg.Record]:
    """加载日线 OHLCV。默认 1200 日（≈240 周），兼顾日线 MA150 slope 和周线 MA30。"""
    return await conn.fetch("""
        SELECT trade_date, open, high, low, close, volume, amount
        FROM market_bars_daily
        WHERE symbol = $1
          AND trade_date <= $2
        ORDER BY trade_date DESC
        LIMIT $3
    """, symbol, cutoff, lookback)


# ═════════════════════════════════════════════════════════════════════════════
# 周线评分
# ═════════════════════════════════════════════════════════════════════════════

def _compute_weekly_result(bar_rows: list[asyncpg.Record]) -> TimeframeResult | None:
    """
    日线 OHLCV → 周线聚合 → 周线技术评分。
    需要 bar_rows 来自 lookback=1200 的查询，保证有足够周数计算 MA30 和 Stage。

    周线评分权重:
      Stage (0-40): 周线 MA30 Weinstein Stage
      RSI   (0-35): 周线 RSI-14
      MACD  (0-25): 周线 MACD 柱状图
    """
    if len(bar_rows) < 70:   # 至少 70 日 = 14 周，周线 RSI 最低需求
        return None

    df = pd.DataFrame(bar_rows, columns=["trade_date","open","high","low","close","volume","amount"])
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df = df.sort_values("trade_date").copy()
    for col in ["open","high","low","close","volume","amount"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["close"])
    df.set_index("trade_date", inplace=True)

    weekly = df.resample("W").agg({
        "open":   "first",
        "high":   "max",
        "low":    "min",
        "close":  "last",
        "volume": "sum",
    }).dropna(subset=["close"])

    if len(weekly) < 15:
        return None

    close_arr = weekly["close"].values.astype(np.float64)

    # ── 周线 RSI ────────────────────────────────────────────────────────
    w_rsi = _rsi(close_arr, period=14)

    # ── 周线 MACD ───────────────────────────────────────────────────────
    w_dif, w_dea, w_hist = _macd(close_arr, fast=12, slow=26, signal=9)
    if len(close_arr) >= 2:
        _, _, prev_w_hist = _macd(close_arr[:-1], fast=12, slow=26, signal=9)
    else:
        prev_w_hist = None

    # ── 周线 Stage（基于 MA30）───────────────────────────────────────────
    w_stage = _weekly_stage(close_arr)

    # 周线 MA30 用于 above_ma150 字段（语义复用）
    w_ma30 = _ma(close_arr, 30)
    above_ma30 = None if w_ma30 is None else bool(close_arr[-1] > w_ma30)

    # 周线 MA30 斜率（30 周，用于 detail）
    w_ma30_slope: float | None = None
    if len(close_arr) >= 60:
        ma30_now    = float(np.mean(close_arr[-30:]))
        ma30_30w_ago = float(np.mean(close_arr[-60:-30]))
        if ma30_30w_ago != 0:
            w_ma30_slope = (ma30_now - ma30_30w_ago) / ma30_30w_ago

    # ── 评分 ────────────────────────────────────────────────────────────
    stage_s = _score_stage(w_stage, max_score=40.0)
    rsi_s   = _score_rsi(w_rsi, max_score=35.0)
    macd_s  = _score_macd(w_hist, prev_w_hist, max_score=25.0)
    w_score = round(stage_s + rsi_s + macd_s, 2)

    w_dir = _direction(w_score)
    macd_bull = None if w_hist is None else bool(w_hist > 0)

    return TimeframeResult(
        timeframe="weekly",
        direction=w_dir,
        score=w_score,
        rsi=w_rsi,
        macd_bullish=macd_bull,
        stage=w_stage,
        above_ma150=above_ma30,   # 字段复用：weekly 语义为 above_ma30
        detail={
            "weekly_bars":  len(weekly),
            "stage_score":  round(stage_s, 2),
            "rsi_score":    round(rsi_s, 2),
            "macd_score":   round(macd_s, 2),
            "ma30":         round(w_ma30, 4) if w_ma30 else None,
            "ma30_slope":   round(w_ma30_slope, 4) if w_ma30_slope is not None else None,
            "price_vs_ma30": round(float(close_arr[-1] / w_ma30 - 1), 4) if w_ma30 else None,
            "macd_dif":     round(w_dif, 4) if w_dif is not None else None,
            "macd_dea":     round(w_dea, 4) if w_dea is not None else None,
            "macd_hist":    round(w_hist, 4) if w_hist is not None else None,
        },
    )


# ═════════════════════════════════════════════════════════════════════════════
# 主入口
# ═════════════════════════════════════════════════════════════════════════════

async def analyze_technical(
    pool: asyncpg.Pool,
    symbol: str,
    market: str,
    cutoff: date | None = None,
) -> TechnicalResult | None:
    """
    计算个股在 cutoff 日期的双周期技术面评分。

    Args:
        pool:   asyncpg 连接池（由调用方管理）。
        symbol: 股票代码，如 "000063.SZ" 或 "NVDA"。
        market: "CN" 或 "US"。
        cutoff: 截止交易日（PIT 安全，只用 <= cutoff 的数据）。

    Returns:
        TechnicalResult，或 None（数据不足）。
    """
    if cutoff is None:
        cutoff = date.today()

    async with pool.acquire() as conn:
        feat_rows = await _fetch_features(conn, symbol, cutoff, lookback=5)
        bar_rows  = await _fetch_bars(conn, symbol, cutoff, lookback=1200)

    if not feat_rows:
        log.warning(f"technical: no features for {symbol} <= {cutoff}")
        return None

    f  = feat_rows[0]
    f2 = feat_rows[1] if len(feat_rows) >= 2 else None

    def _fv(rec, col):
        v = rec[col] if rec else None
        return float(v) if v is not None else None

    # ── 从 features_daily 读取当日值 ─────────────────────────────────────
    rsi       = _fv(f, "rsi_14")
    macd_hist = _fv(f, "macd_hist")
    macd_dif  = _fv(f, "macd_dif")
    macd_dea  = _fv(f, "macd_dea")
    prev_hist = _fv(f2, "macd_hist") if f2 else None
    stage     = int(f["stage"]) if f["stage"] is not None else None
    ma5       = _fv(f, "ma5")
    ma20      = _fv(f, "ma20")
    ma60      = _fv(f, "ma60")
    ma150     = _fv(f, "ma150")
    ma200     = _fv(f, "ma200")
    rs_rank   = _fv(f, "rs_rank")    # 0-1 百分位（CN=全市场4797股，US=watchlist内）

    # ── MA150 slope + price_vs_ma150（从日线 bars 计算）─────────────────
    ma150_slope_val: float | None = None
    price_vs_ma150:  float | None = None

    if bar_rows:
        bar_df = pd.DataFrame(bar_rows, columns=["trade_date","open","high","low","close","volume","amount"])
        bar_df = bar_df.sort_values("trade_date").copy()
        close_arr = pd.to_numeric(bar_df["close"], errors="coerce").values.astype(np.float64)
        if len(close_arr) >= 180:
            ma150_now    = float(np.mean(close_arr[-150:]))
            ma150_30d    = float(np.mean(close_arr[-180:-30]))
            if ma150_30d != 0:
                ma150_slope_val = (ma150_now - ma150_30d) / ma150_30d
            if ma150_now != 0:
                price_vs_ma150 = close_arr[-1] / ma150_now - 1
        elif len(close_arr) >= 150:
            ma150_now = float(np.mean(close_arr[-150:]))
            if ma150_now != 0:
                price_vs_ma150 = close_arr[-1] / ma150_now - 1
        # fallback: features_daily ma150
        if price_vs_ma150 is None and ma150 is not None and ma150 != 0 and bar_rows:
            cur_close = float(bar_rows[0]["close"]) if bar_rows[0]["close"] else None
            if cur_close:
                price_vs_ma150 = cur_close / ma150 - 1

    above_ma150 = None if price_vs_ma150 is None else bool(price_vs_ma150 > 0)

    # ── stage_confidence ─────────────────────────────────────────────────
    stage_conf = _compute_stage_confidence(ma150_slope_val, price_vs_ma150)

    # ── 日线评分 ─────────────────────────────────────────────────────────
    ma_s    = _score_ma_alignment(ma5, ma20, ma60, ma150, ma200)
    rsi_s   = _score_rsi(rsi, max_score=25.0)
    macd_s  = _score_macd(macd_hist, prev_hist, max_score=25.0)
    stage_s = _score_stage(stage, stage_conf=stage_conf, max_score=20.0)
    rs_s    = _score_rs_rank(rs_rank)

    daily_score = round(ma_s + rsi_s + macd_s + stage_s + rs_s, 2)
    daily_dir   = _direction(daily_score)
    macd_bull   = None if macd_hist is None else bool(macd_hist > 0)

    daily = TimeframeResult(
        timeframe="daily",
        direction=daily_dir,
        score=daily_score,
        rsi=rsi,
        macd_bullish=macd_bull,
        stage=stage,
        above_ma150=above_ma150,
        detail={
            "ma_alignment_score": round(ma_s, 2),
            "rsi_score":          round(rsi_s, 2),
            "macd_score":         round(macd_s, 2),
            "stage_score":        round(stage_s, 2),
            "rs_rank_score":      round(rs_s, 4),
            "ma5": ma5, "ma20": ma20, "ma60": ma60, "ma150": ma150, "ma200": ma200,
            "ma150_slope":     round(ma150_slope_val, 4) if ma150_slope_val is not None else None,
            "price_vs_ma150":  round(price_vs_ma150, 4) if price_vs_ma150 is not None else None,
            "macd_dif":        round(macd_dif, 4) if macd_dif is not None else None,
            "macd_dea":        round(macd_dea, 4) if macd_dea is not None else None,
            "macd_hist":       round(macd_hist, 4) if macd_hist is not None else None,
            "rs_rank":         rs_rank,   # 保留原始精度，不 round
            "adx":             _fv(f, "adx_14"),
            "plus_di":         _fv(f, "plus_di"),
            "minus_di":        _fv(f, "minus_di"),
            "ret_5d":          _fv(f, "ret_5d"),
            "ret_20d":         _fv(f, "ret_20d"),
            "ret_60d":         _fv(f, "ret_60d"),
            "vol_ratio_5d":    _fv(f, "vol_ratio_5d"),
        },
    )

    # ── 周线评分 ──────────────────────────────────────────────────────────
    weekly = _compute_weekly_result(bar_rows)

    # ── 综合 ─────────────────────────────────────────────────────────────
    if weekly is not None:
        combined_score = round(daily_score * 0.6 + weekly.score * 0.4, 2)
    else:
        combined_score = daily_score
    combined_dir = _direction(combined_score)

    base_conf      = float(np.clip(0.3 + combined_score / 100.0 * 0.6, 0.3, 0.9))
    confidence_adj = round(base_conf * stage_conf, 4)

    stage_conf_reason = (
        "slope_borderline"
        if ma150_slope_val is not None and abs(abs(ma150_slope_val) - _SLOPE_FLAT) < 0.005
        else "price_near_ma150"
        if price_vs_ma150 is not None and abs(price_vs_ma150) < 0.03
        else "clear"
    )

    result = TechnicalResult(
        symbol=symbol,
        trade_date=cutoff,
        market=market,
        daily=daily,
        weekly=weekly,
        combined_direction=combined_dir,
        combined_score=combined_score,
        confidence_adj=confidence_adj,
        stage_confidence=stage_conf,
        detail={"stage_conf_reason": stage_conf_reason},
    )
    log.info(str(result))
    return result
