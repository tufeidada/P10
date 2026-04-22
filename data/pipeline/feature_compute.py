"""
特征计算管道 — 为候选池股票计算日频技术特征，写入 features_daily 表。

Usage:
    python -m data.pipeline.feature_compute --market CN --date 2026-04-15
    python -m data.pipeline.feature_compute --market US --symbols AAPL,NVDA
    python -m data.pipeline.feature_compute --all  # both markets, today
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import date
from typing import Any

import numpy as np
import structlog

from core.analysis.stage_detector import StageDetector
from db.connection import db_execute, db_query, db_query_val

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Low-level indicator helpers (pure numpy, no external dependencies)
# ---------------------------------------------------------------------------


def _compute_ema(data: np.ndarray, period: int) -> np.ndarray:
    """计算指数移动平均线 (EMA)。

    Args:
        data: 输入价格序列（时间升序）。
        period: EMA 周期。

    Returns:
        与 data 等长的 EMA 数组，前 period-1 个值为 0（未初始化）。
    """
    ema = np.zeros(len(data), dtype=np.float64)
    if len(data) < period:
        return ema

    multiplier = 2.0 / (period + 1)
    ema[period - 1] = np.mean(data[:period])
    for i in range(period, len(data)):
        ema[i] = (data[i] - ema[i - 1]) * multiplier + ema[i - 1]
    return ema


def _compute_ma(data: np.ndarray, period: int) -> np.ndarray:
    """计算简单移动平均线 (SMA)。

    Args:
        data: 输入价格序列（时间升序）。
        period: MA 周期。

    Returns:
        与 data 等长的 MA 数组，前 period-1 个值为 NaN。
    """
    if len(data) < period:
        return np.full(len(data), np.nan, dtype=np.float64)
    kernel = np.ones(period) / period
    ma = np.convolve(data, kernel, mode="full")[: len(data)]
    ma[: period - 1] = np.nan
    return ma


def _compute_rsi(closes: np.ndarray, period: int = 14) -> float:
    """计算最新 RSI 值（Wilder 平滑）。

    Args:
        closes: 收盘价序列（时间升序）。
        period: RSI 周期，默认 14。

    Returns:
        RSI 值 0-100。数据不足时返回 50.0。
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


def _compute_macd(
    closes: np.ndarray,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[float, float, float]:
    """计算最新 MACD DIF / DEA / HIST。

    Args:
        closes: 收盘价序列（时间升序）。
        fast: 快线 EMA 周期，默认 12。
        slow: 慢线 EMA 周期，默认 26。
        signal: 信号线 EMA 周期，默认 9。

    Returns:
        (dif, dea, hist) 三元组，数据不足时返回 (0.0, 0.0, 0.0)。
    """
    if len(closes) < slow + signal:
        return (0.0, 0.0, 0.0)

    ema_fast = _compute_ema(closes, fast)
    ema_slow = _compute_ema(closes, slow)

    dif_series = ema_fast - ema_slow
    # 只对 slow 之后的有效部分计算 DEA
    dif_valid_start = slow - 1
    if len(dif_series) - dif_valid_start < signal:
        return (0.0, 0.0, 0.0)

    dea_series = np.zeros(len(dif_series), dtype=np.float64)
    dea_series[dif_valid_start + signal - 1] = np.mean(
        dif_series[dif_valid_start : dif_valid_start + signal]
    )
    multiplier = 2.0 / (signal + 1)
    for i in range(dif_valid_start + signal, len(dif_series)):
        dea_series[i] = (dif_series[i] - dea_series[i - 1]) * multiplier + dea_series[i - 1]

    dif = float(dif_series[-1])
    dea = float(dea_series[-1])
    hist = round((dif - dea) * 2, 6)  # 柱状图 = (DIF - DEA) × 2
    return (round(dif, 6), round(dea, 6), hist)


def _compute_atr(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    period: int = 14,
) -> float:
    """计算最新 ATR(14)（Wilder 平滑）。

    Args:
        highs: 最高价序列。
        lows: 最低价序列。
        closes: 收盘价序列。
        period: ATR 周期，默认 14。

    Returns:
        ATR 值，数据不足时返回 0.0。
    """
    n = len(closes)
    if n < period + 1:
        return 0.0

    tr = np.maximum(
        highs[1:] - lows[1:],
        np.maximum(
            np.abs(highs[1:] - closes[:-1]),
            np.abs(lows[1:] - closes[:-1]),
        ),
    )

    atr = np.mean(tr[:period])
    for i in range(period, len(tr)):
        atr = (atr * (period - 1) + tr[i]) / period

    return round(float(atr), 6)


def _compute_hv20(closes: np.ndarray) -> float:
    """计算 20 日历史波动率（年化，百分比）。

    Args:
        closes: 收盘价序列（时间升序）。

    Returns:
        HV20 年化百分比值，数据不足时返回 0.0。
    """
    if len(closes) < 21:
        return 0.0
    log_ret = np.log(closes[-20:] / closes[-21:-1])
    return round(float(np.std(log_ret, ddof=1) * np.sqrt(252) * 100.0), 4)


def _compute_bollinger(
    closes: np.ndarray,
    period: int = 20,
    num_std: float = 2.0,
) -> tuple[float, float, float]:
    """计算最新布林带上下轨及带宽。

    Args:
        closes: 收盘价序列（时间升序）。
        period: 布林带均线周期，默认 20。
        num_std: 标准差倍数，默认 2.0。

    Returns:
        (upper, lower, width) 三元组，数据不足时返回 (0.0, 0.0, 0.0)。
    """
    if len(closes) < period:
        return (0.0, 0.0, 0.0)

    window = closes[-period:]
    ma = float(np.mean(window))
    std = float(np.std(window, ddof=1))
    upper = round(ma + num_std * std, 4)
    lower = round(ma - num_std * std, 4)
    width = round((upper - lower) / ma if ma != 0 else 0.0, 6)
    return (upper, lower, width)


def _compute_adx_latest(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    period: int = 14,
) -> tuple[float, float, float]:
    """计算最新 ADX / +DI / -DI。

    Args:
        highs: 最高价序列。
        lows: 最低价序列。
        closes: 收盘价序列。
        period: ADX 周期，默认 14。

    Returns:
        (adx, plus_di, minus_di) 三元组，数据不足时返回 (0.0, 0.0, 0.0)。
    """
    n = len(closes)
    if n < period * 2 + 1:
        return (0.0, 0.0, 0.0)

    tr = np.maximum(
        highs[1:] - lows[1:],
        np.maximum(
            np.abs(highs[1:] - closes[:-1]),
            np.abs(lows[1:] - closes[:-1]),
        ),
    )

    up_move = highs[1:] - highs[:-1]
    down_move = lows[:-1] - lows[1:]

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    # Wilder's smoothing
    atr_w = np.mean(tr[:period])
    plus_w = np.mean(plus_dm[:period])
    minus_w = np.mean(minus_dm[:period])

    for i in range(period, len(tr)):
        atr_w = (atr_w * (period - 1) + tr[i]) / period
        plus_w = (plus_w * (period - 1) + plus_dm[i]) / period
        minus_w = (minus_w * (period - 1) + minus_dm[i]) / period

    if atr_w == 0:
        return (0.0, 0.0, 0.0)

    plus_di_val = plus_w / atr_w * 100.0
    minus_di_val = minus_w / atr_w * 100.0

    di_sum = plus_di_val + minus_di_val
    if di_sum == 0:
        dx = 0.0
    else:
        dx = abs(plus_di_val - minus_di_val) / di_sum * 100.0

    # ADX = smoothed DX, seeded at period*2
    start = period * 2 - 1
    if start >= len(tr):
        return (0.0, round(plus_di_val, 4), round(minus_di_val, 4))

    # Recompute full DX series for ADX seeding
    atr_arr = np.zeros(len(tr))
    plus_arr = np.zeros(len(tr))
    minus_arr = np.zeros(len(tr))
    atr_arr[period - 1] = np.mean(tr[:period])
    plus_arr[period - 1] = np.mean(plus_dm[:period])
    minus_arr[period - 1] = np.mean(minus_dm[:period])
    for i in range(period, len(tr)):
        atr_arr[i] = (atr_arr[i - 1] * (period - 1) + tr[i]) / period
        plus_arr[i] = (plus_arr[i - 1] * (period - 1) + plus_dm[i]) / period
        minus_arr[i] = (minus_arr[i - 1] * (period - 1) + minus_dm[i]) / period

    with np.errstate(divide="ignore", invalid="ignore"):
        pdi = np.where(atr_arr > 0, plus_arr / atr_arr * 100, 0.0)
        mdi = np.where(atr_arr > 0, minus_arr / atr_arr * 100, 0.0)
        di_s = pdi + mdi
        dx_arr = np.where(di_s > 0, np.abs(pdi - mdi) / di_s * 100, 0.0)

    adx_val = np.mean(dx_arr[period - 1 : period * 2])
    for i in range(period * 2, len(dx_arr)):
        adx_val = (adx_val * (period - 1) + dx_arr[i]) / period

    return (round(float(adx_val), 4), round(float(pdi[-1]), 4), round(float(mdi[-1]), 4))


def _compute_ma_slope(
    closes: np.ndarray, period: int, slope_window: int = 5
) -> float:
    """计算均线斜率（最近 slope_window 天的变化率）。

    Args:
        closes: 收盘价序列（时间升序）。
        period: 均线周期。
        slope_window: 斜率计算窗口，默认 5。

    Returns:
        归一化斜率（每天变化量 / 均线均值），数据不足时返回 0.0。
    """
    if len(closes) < period + slope_window:
        return 0.0

    ma_now = float(np.mean(closes[-period:]))
    ma_prev = float(np.mean(closes[-(period + slope_window) : -slope_window]))

    if ma_prev == 0:
        return 0.0

    return round((ma_now - ma_prev) / (ma_prev * slope_window), 6)


# ---------------------------------------------------------------------------
# FeatureComputer
# ---------------------------------------------------------------------------


class FeatureComputer:
    """日频技术特征计算器。

    为候选池股票批量计算技术指标并写入 features_daily 表。
    所有计算基于 market_bars_daily 的 OHLCV 数据，不依赖外部库。
    """

    def __init__(self) -> None:
        self._stage_detector = StageDetector()

    async def compute_for_symbol(
        self,
        symbol: str,
        market: str,
        trade_date: date,
        lookback: int = 250,
    ) -> bool:
        """计算单只股票在指定日期的所有技术特征并写入 features_daily。

        Steps:
            1. 从 market_bars_daily 加载最近 lookback 个交易日的 OHLCV 数据。
            2. 用纯 numpy 计算技术指标：MA/EMA/MACD/RSI/ATR/HV/Bollinger/ADX 等。
            3. 调用 StageDetector.detect_stage 计算 Weinstein Stage。
            4. 调用 StageDetector.calc_rs_rank 计算 O'Neil RS Rank。
            5. Upsert 到 features_daily。

        Args:
            symbol: 股票代码。
            market: 市场代码（'CN' 或 'US'）。
            trade_date: 计算特征的目标日期。
            lookback: 加载历史天数，默认 250（满足 MA200 + 余量）。

        Returns:
            成功返回 True，否则返回 False。
        """
        log = logger.bind(symbol=symbol, market=market, trade_date=str(trade_date))

        try:
            bars = await self._load_bars(symbol, trade_date, lookback)
            if len(bars) < 30:
                log.warning(
                    "feature_compute_insufficient_data",
                    bars_found=len(bars),
                    required=30,
                )
                return False

            closes = np.array([float(b["close"]) for b in bars], dtype=np.float64)
            highs = np.array([float(b["high"]) for b in bars], dtype=np.float64)
            lows = np.array([float(b["low"]) for b in bars], dtype=np.float64)
            volumes = np.array([float(b["volume"]) for b in bars], dtype=np.float64)
            amounts = np.array(
                [float(b["amount"]) if b.get("amount") else 0.0 for b in bars],
                dtype=np.float64,
            )

            n = len(closes)

            # --- 均线 ---
            ma5 = float(np.mean(closes[-5:])) if n >= 5 else None
            ma10 = float(np.mean(closes[-10:])) if n >= 10 else None
            ma20 = float(np.mean(closes[-20:])) if n >= 20 else None
            ma60 = float(np.mean(closes[-60:])) if n >= 60 else None
            ma150 = float(np.mean(closes[-150:])) if n >= 150 else None
            ma200 = float(np.mean(closes[-200:])) if n >= 200 else None

            # --- 均线斜率 ---
            ma5_slope = _compute_ma_slope(closes, 5) if n >= 10 else None
            ma20_slope = _compute_ma_slope(closes, 20) if n >= 25 else None

            # --- RSI(14) ---
            rsi_14 = _compute_rsi(closes, 14) if n >= 15 else None

            # --- MACD ---
            macd_dif, macd_dea, macd_hist = (None, None, None)
            if n >= 35:  # 26 + 9
                dif, dea, hist = _compute_macd(closes)
                if dif != 0.0 or dea != 0.0:
                    macd_dif, macd_dea, macd_hist = dif, dea, hist

            # --- ATR(14) ---
            atr_14 = _compute_atr(highs, lows, closes, 14) if n >= 15 else None

            # --- HV20 ---
            hv_20 = _compute_hv20(closes) if n >= 21 else None

            # --- Bollinger Bands (20, 2σ) ---
            boll_upper, boll_lower, boll_width = (None, None, None)
            if n >= 20:
                bu, bl, bw = _compute_bollinger(closes, 20, 2.0)
                boll_upper, boll_lower, boll_width = bu, bl, bw

            # --- ADX(14), +DI, -DI ---
            adx_14, plus_di, minus_di = (None, None, None)
            if n >= 29:  # 14*2+1
                adx, pdi, mdi = _compute_adx_latest(highs, lows, closes, 14)
                if adx > 0:
                    adx_14, plus_di, minus_di = adx, pdi, mdi

            # --- 量价 ---
            vol_ratio_5d: float | None = None
            if n >= 21:
                vol_5 = float(np.mean(volumes[-5:]))
                vol_20 = float(np.mean(volumes[-21:-1]))
                vol_ratio_5d = round(vol_5 / vol_20, 4) if vol_20 > 0 else None

            turnover_rank_20d: float | None = None
            if n >= 20 and np.any(amounts > 0):
                # 换手率排名: 当日换手率在最近 20 日中的分位数
                recent_amounts = amounts[-20:]
                current_amount = amounts[-1]
                if np.sum(recent_amounts > 0) >= 5:
                    rank = float(np.sum(recent_amounts <= current_amount) / len(recent_amounts))
                    turnover_rank_20d = round(rank * 100.0, 2)

            # --- 收益率 ---
            ret_1d = round(float(closes[-1] / closes[-2] - 1.0), 6) if n >= 2 else None
            ret_5d = round(float(closes[-1] / closes[-6] - 1.0), 6) if n >= 6 else None
            ret_20d = round(float(closes[-1] / closes[-21] - 1.0), 6) if n >= 21 else None

            # --- Weinstein Stage ---
            stage = await self._stage_detector.detect_stage(symbol, trade_date)

            # --- RS Rank ---
            rs_rank = await self._stage_detector.calc_rs_rank(symbol, trade_date)

            # --- Upsert into features_daily ---
            await self._upsert_features(
                symbol=symbol,
                trade_date=trade_date,
                ma5=ma5,
                ma10=ma10,
                ma20=ma20,
                ma60=ma60,
                ma150=ma150,
                ma200=ma200,
                ma5_slope=ma5_slope,
                ma20_slope=ma20_slope,
                rsi_14=rsi_14,
                macd_dif=macd_dif,
                macd_dea=macd_dea,
                macd_hist=macd_hist,
                atr_14=atr_14,
                hv_20=hv_20,
                boll_upper=boll_upper,
                boll_lower=boll_lower,
                boll_width=boll_width,
                adx_14=adx_14,
                plus_di=plus_di,
                minus_di=minus_di,
                vol_ratio_5d=vol_ratio_5d,
                turnover_rank_20d=turnover_rank_20d,
                ret_1d=ret_1d,
                ret_5d=ret_5d,
                ret_20d=ret_20d,
                stage=stage,
                rs_rank=rs_rank,
            )

            log.info(
                "feature_compute_success",
                stage=stage,
                rs_rank=round(rs_rank, 2),
                rsi_14=rsi_14,
            )
            return True

        except Exception:
            log.exception("feature_compute_error")
            return False

    async def compute_for_universe(
        self,
        market: str = "CN",
        trade_date: date | None = None,
    ) -> dict[str, bool]:
        """计算 stock_universe 中全部活跃股票的技术特征。

        Args:
            market: 市场代码，默认 'CN'。
            trade_date: 目标交易日，默认今天。

        Returns:
            {symbol: success} 映射字典。
        """
        if trade_date is None:
            trade_date = date.today()

        rows = await db_query(
            """
            SELECT symbol
            FROM stock_universe
            WHERE market = $1 AND active = TRUE
            ORDER BY symbol
            """,
            market,
        )

        symbols = [r["symbol"] for r in rows]
        logger.info(
            "feature_compute_universe_start",
            market=market,
            trade_date=str(trade_date),
            symbol_count=len(symbols),
        )
        return await self.compute_for_symbols(symbols, market, trade_date)

    async def compute_for_symbols(
        self,
        symbols: list[str],
        market: str,
        trade_date: date | None = None,
    ) -> dict[str, bool]:
        """计算指定股票列表的技术特征。

        按顺序逐个处理，每只股票独立捕获异常，互不影响。

        Args:
            symbols: 股票代码列表。
            market: 市场代码（'CN' 或 'US'）。
            trade_date: 目标交易日，默认今天。

        Returns:
            {symbol: success} 映射字典。
        """
        if trade_date is None:
            trade_date = date.today()

        results: dict[str, bool] = {}
        total = len(symbols)

        for idx, symbol in enumerate(symbols, start=1):
            success = await self.compute_for_symbol(symbol, market, trade_date)
            results[symbol] = success
            if idx % 50 == 0 or idx == total:
                success_count = sum(results.values())
                logger.info(
                    "feature_compute_progress",
                    market=market,
                    trade_date=str(trade_date),
                    processed=idx,
                    total=total,
                    success=success_count,
                    failed=idx - success_count,
                )

        logger.info(
            "feature_compute_done",
            market=market,
            trade_date=str(trade_date),
            total=total,
            success=sum(results.values()),
            failed=sum(1 for v in results.values() if not v),
        )
        return results

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    @staticmethod
    async def _load_bars(
        symbol: str,
        trade_date: date,
        lookback: int,
    ) -> list[dict[str, Any]]:
        """从 market_bars_daily 加载日线数据。

        Args:
            symbol: 股票代码。
            trade_date: 截止日期（含）。
            lookback: 最多返回的交易日数。

        Returns:
            按 trade_date 升序排列的 bar 字典列表。
        """
        rows = await db_query(
            """
            SELECT trade_date, open, high, low, close, volume, amount
            FROM market_bars_daily
            WHERE symbol = $1 AND trade_date <= $2
            ORDER BY trade_date DESC
            LIMIT $3
            """,
            symbol,
            trade_date,
            lookback,
        )
        return [dict(r) for r in reversed(rows)]

    @staticmethod
    async def _upsert_features(
        symbol: str,
        trade_date: date,
        **kwargs: Any,
    ) -> None:
        """将计算结果 upsert 到 features_daily。

        使用 ON CONFLICT (symbol, trade_date) DO UPDATE 保证幂等性。

        Args:
            symbol: 股票代码。
            trade_date: 交易日期。
            **kwargs: 特征字段名与值的映射。
        """
        await db_execute(
            """
            INSERT INTO features_daily (
                symbol, trade_date,
                ma5, ma10, ma20, ma60, ma150, ma200,
                ma5_slope, ma20_slope,
                rsi_14,
                macd_dif, macd_dea, macd_hist,
                atr_14, hv_20,
                boll_upper, boll_lower, boll_width,
                adx_14, plus_di, minus_di,
                vol_ratio_5d, turnover_rank_20d,
                ret_1d, ret_5d, ret_20d,
                stage, rs_rank
            ) VALUES (
                $1, $2,
                $3, $4, $5, $6, $7, $8,
                $9, $10,
                $11,
                $12, $13, $14,
                $15, $16,
                $17, $18, $19,
                $20, $21, $22,
                $23, $24,
                $25, $26, $27,
                $28, $29
            )
            ON CONFLICT (symbol, trade_date) DO UPDATE SET
                ma5            = EXCLUDED.ma5,
                ma10           = EXCLUDED.ma10,
                ma20           = EXCLUDED.ma20,
                ma60           = EXCLUDED.ma60,
                ma150          = EXCLUDED.ma150,
                ma200          = EXCLUDED.ma200,
                ma5_slope      = EXCLUDED.ma5_slope,
                ma20_slope     = EXCLUDED.ma20_slope,
                rsi_14         = EXCLUDED.rsi_14,
                macd_dif       = EXCLUDED.macd_dif,
                macd_dea       = EXCLUDED.macd_dea,
                macd_hist      = EXCLUDED.macd_hist,
                atr_14         = EXCLUDED.atr_14,
                hv_20          = EXCLUDED.hv_20,
                boll_upper     = EXCLUDED.boll_upper,
                boll_lower     = EXCLUDED.boll_lower,
                boll_width     = EXCLUDED.boll_width,
                adx_14         = EXCLUDED.adx_14,
                plus_di        = EXCLUDED.plus_di,
                minus_di       = EXCLUDED.minus_di,
                vol_ratio_5d   = EXCLUDED.vol_ratio_5d,
                turnover_rank_20d = EXCLUDED.turnover_rank_20d,
                ret_1d         = EXCLUDED.ret_1d,
                ret_5d         = EXCLUDED.ret_5d,
                ret_20d        = EXCLUDED.ret_20d,
                stage          = EXCLUDED.stage,
                rs_rank        = EXCLUDED.rs_rank
            """,
            symbol,
            trade_date,
            kwargs.get("ma5"),
            kwargs.get("ma10"),
            kwargs.get("ma20"),
            kwargs.get("ma60"),
            kwargs.get("ma150"),
            kwargs.get("ma200"),
            kwargs.get("ma5_slope"),
            kwargs.get("ma20_slope"),
            kwargs.get("rsi_14"),
            kwargs.get("macd_dif"),
            kwargs.get("macd_dea"),
            kwargs.get("macd_hist"),
            kwargs.get("atr_14"),
            kwargs.get("hv_20"),
            kwargs.get("boll_upper"),
            kwargs.get("boll_lower"),
            kwargs.get("boll_width"),
            kwargs.get("adx_14"),
            kwargs.get("plus_di"),
            kwargs.get("minus_di"),
            kwargs.get("vol_ratio_5d"),
            kwargs.get("turnover_rank_20d"),
            kwargs.get("ret_1d"),
            kwargs.get("ret_5d"),
            kwargs.get("ret_20d"),
            kwargs.get("stage"),
            kwargs.get("rs_rank"),
        )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


async def _main() -> None:
    """CLI 入口，解析参数后调用 FeatureComputer。"""
    parser = argparse.ArgumentParser(
        description="特征计算管道 — 计算日频技术特征并写入 features_daily"
    )
    parser.add_argument("--market", choices=["CN", "US"], help="市场代码")
    parser.add_argument("--date", help="目标交易日 YYYY-MM-DD（默认今天）")
    parser.add_argument("--symbols", help="逗号分隔的股票代码列表，如 AAPL,NVDA")
    parser.add_argument("--all", action="store_true", dest="all_markets", help="计算 CN+US 全市场")
    args = parser.parse_args()

    target_date: date | None = None
    if args.date:
        from datetime import datetime

        target_date = datetime.strptime(args.date, "%Y-%m-%d").date()

    computer = FeatureComputer()

    if args.all_markets:
        for mkt in ("CN", "US"):
            await computer.compute_for_universe(market=mkt, trade_date=target_date)
    elif args.symbols:
        if not args.market:
            parser.error("--symbols 必须配合 --market 使用")
        symbol_list = [s.strip() for s in args.symbols.split(",") if s.strip()]
        await computer.compute_for_symbols(symbol_list, args.market, target_date)
    elif args.market:
        await computer.compute_for_universe(market=args.market, trade_date=target_date)
    else:
        parser.print_help()


if __name__ == "__main__":
    asyncio.run(_main())
