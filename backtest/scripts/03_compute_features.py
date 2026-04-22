"""
03_compute_features.py — 批量计算 features_daily + backtest_features_extra

前提：01_migrate_schema.py + 02_fetch_missing_data.py 已完成。

计算内容：
  features_daily       — 61 只 watchlist 的技术/资金面特征（全 PIT 安全）
  backtest_features_extra — future_ret_* 未来收益标注（仅用于回测标注）

PIT 铁律（三条红线）：
  1. 禁止 import P10 主项目 core/ 下任何模块
  2. rolling/shift 全部用历史数据；future_ret_* 只写 backtest_features_extra
  3. RS Rank = groupby('trade_date').rank(pct=True) 截面排名

运行方式：
    cd "P10-AlphaRadar "
    python backtest/scripts/03_compute_features.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import date
from pathlib import Path
from typing import List, Tuple

import asyncpg
import numpy as np
import pandas as pd
from dotenv import load_dotenv

# ─── 路径 ────────────────────────────────────────────────────────────────────
_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT   = _SCRIPT_DIR.parents[1]
_BACKTEST_DIR = _SCRIPT_DIR.parent
sys.path.insert(0, str(_REPO_ROOT))
load_dotenv(_REPO_ROOT / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_BACKTEST_DIR / "logs" / "compute_features.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ─── 常量 ────────────────────────────────────────────────────────────────────
COMPUTE_START = date(2022, 6, 1)
COMPUTE_END   = date(2026, 4, 17)

CN_WATCHLIST: List[str] = [
    "603683.SH", "300821.SZ", "300196.SZ", "000510.SZ", "000818.SZ", "600160.SH",
    "002378.SZ", "000962.SZ", "000960.SZ", "000657.SZ", "600392.SH", "688766.SH",
    "603986.SH", "603893.SH", "688726.SH", "688353.SH", "688599.SH", "688032.SH",
    "002050.SZ", "300757.SZ", "300818.SZ", "300502.SZ", "000063.SZ", "300002.SZ",
    "688543.SH", "600590.SH", "603716.SH", "002426.SZ", "601138.SH", "600575.SH",
    "002091.SZ", "601318.SH",
]

US_WATCHLIST: List[str] = [
    "NVDA", "AAPL", "MSFT", "GOOGL", "TSLA", "NFLX", "ADBE", "ZM", "DUOL", "MSTR",
    "MU", "INTC", "AAOI", "UNH", "SMMT", "HIMS", "FUTU", "BX", "LMND", "OXY",
    "ASTS", "BABA", "JD", "PDD", "BIDU", "NTES", "TCOM", "XPEV", "LI",
]

ALL_WATCHLIST = CN_WATCHLIST + US_WATCHLIST

# features_daily 列写入顺序（含新增列）
_FD_COLUMNS = [
    "symbol", "trade_date",
    "ma5", "ma10", "ma20", "ma60", "ma150", "ma200",
    "ma5_slope", "ma20_slope", "ma60_slope",
    "rsi_14", "macd_dif", "macd_dea", "macd_hist",
    "atr_14", "hv_20",
    "boll_upper", "boll_lower", "boll_width",
    "adx_14", "plus_di", "minus_di",
    "vol_ratio_5d",
    "turnover_rate", "turnover_rate_f", "turnover_rank_20d",
    "ret_1d", "ret_5d", "ret_20d", "ret_60d",
    "dist_20d_high", "dist_60d_high", "pct_in_20d_range",
    "stage", "rs_rank",
    "extra", "available_date",
]

_BFE_COLUMNS = [
    "symbol", "trade_date",
    "future_ret_5d", "future_ret_10d", "future_ret_20d",
    "future_max_up_20d", "future_max_dd_20d",
]


# ═════════════════════════════════════════════════════════════════════════════
# DB 连接
# ═════════════════════════════════════════════════════════════════════════════

async def _create_pool() -> asyncpg.Pool:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL 未配置")
    return await asyncpg.create_pool(
        dsn, min_size=2, max_size=10,
        server_settings={"application_name": "backtest_03_compute"},
        command_timeout=300,
    )


# ═════════════════════════════════════════════════════════════════════════════
# Section 1: DDL — 添加缺失列（幂等）
# ═════════════════════════════════════════════════════════════════════════════

async def run_ddl(pool: asyncpg.Pool) -> None:
    log.info("=== Section 1: DDL ===")
    new_cols = [
        ("ma60_slope",        "NUMERIC(14, 8)"),
        ("ret_60d",           "NUMERIC(14, 8)"),
        ("dist_20d_high",     "NUMERIC(14, 8)"),
        ("dist_60d_high",     "NUMERIC(14, 8)"),
        ("pct_in_20d_range",  "NUMERIC(14, 8)"),
        ("turnover_rate",     "NUMERIC(14, 8)"),
        ("turnover_rate_f",   "NUMERIC(14, 8)"),
    ]
    async with pool.acquire() as conn:
        for col_name, col_type in new_cols:
            try:
                await conn.execute(
                    f"ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS {col_name} {col_type}"
                )
                log.info(f"  ADD COLUMN {col_name} OK")
            except Exception as e:
                log.warning(f"  ADD COLUMN {col_name} skipped: {e}")
    log.info("DDL 完成")


# ═════════════════════════════════════════════════════════════════════════════
# Section 2: DELETE watchlist features（保留非 watchlist 数据）
# ═════════════════════════════════════════════════════════════════════════════

async def delete_existing(pool: asyncpg.Pool) -> None:
    log.info("=== Section 2: DELETE 已有 watchlist features ===")
    async with pool.acquire() as conn:
        r1 = await conn.execute(
            "DELETE FROM features_daily WHERE symbol = ANY($1) AND trade_date BETWEEN $2 AND $3",
            ALL_WATCHLIST, COMPUTE_START, COMPUTE_END,
        )
        log.info(f"  features_daily: {r1}")
        r2 = await conn.execute(
            "DELETE FROM backtest_features_extra WHERE symbol = ANY($1) AND trade_date BETWEEN $2 AND $3",
            ALL_WATCHLIST, COMPUTE_START, COMPUTE_END,
        )
        log.info(f"  backtest_features_extra: {r2}")


# ═════════════════════════════════════════════════════════════════════════════
# Section 3: 数据加载
# ═════════════════════════════════════════════════════════════════════════════

async def load_cn_all_close(pool: asyncpg.Pool) -> pd.DataFrame:
    """加载全市场 CN 收盘价（用于 RS Rank 截面计算）。"""
    log.info("  加载 CN 全市场 close（用于 RS Rank）…")
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT symbol, trade_date, close
            FROM market_bars_daily
            WHERE market = 'CN'
              AND trade_date BETWEEN $1 AND $2
              AND close IS NOT NULL
        """, COMPUTE_START, COMPUTE_END)
    df = pd.DataFrame(rows, columns=["symbol", "trade_date", "close"])
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df["close"] = df["close"].astype(float)
    log.info(f"  CN 全市场: {len(df):,} 行，{df['symbol'].nunique():,} 只")
    return df


async def load_watchlist_bars(pool: asyncpg.Pool) -> pd.DataFrame:
    """加载 watchlist OHLCV + turnover_rate（来自 market_bars_daily）。"""
    log.info("  加载 watchlist bars…")
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT symbol, trade_date, open, high, low, close, volume,
                   turnover_rate, market
            FROM market_bars_daily
            WHERE symbol = ANY($1)
              AND trade_date BETWEEN $2 AND $3
            ORDER BY symbol, trade_date
        """, ALL_WATCHLIST, COMPUTE_START, COMPUTE_END)
    df = pd.DataFrame(rows, columns=[
        "symbol", "trade_date", "open", "high", "low", "close",
        "volume", "turnover_rate", "market",
    ])
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    for col in ["open", "high", "low", "close", "volume", "turnover_rate"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    log.info(f"  watchlist bars: {len(df):,} 行，{df['symbol'].nunique()} 只")
    return df


async def load_cn_fundamentals(pool: asyncpg.Pool) -> pd.DataFrame:
    """加载 CN watchlist 自由流通换手率（turnover_rate_f）。"""
    log.info("  加载 CN fundamentals_daily (turnover_rate_f)…")
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT symbol, trade_date, turnover_rate_f
            FROM fundamentals_daily
            WHERE symbol = ANY($1)
              AND trade_date BETWEEN $2 AND $3
        """, CN_WATCHLIST, COMPUTE_START, COMPUTE_END)
    df = pd.DataFrame(rows, columns=["symbol", "trade_date", "turnover_rate_f"])
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df["turnover_rate_f"] = pd.to_numeric(df["turnover_rate_f"], errors="coerce")
    log.info(f"  CN fundamentals: {len(df):,} 行")
    return df


# ═════════════════════════════════════════════════════════════════════════════
# Section 4: 指标计算函数
# ═════════════════════════════════════════════════════════════════════════════

def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder RSI（用 EWM 近似，与大多数平台一致）。"""
    delta = close.diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period, adjust=False).mean()
    rs  = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def _macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9
          ) -> Tuple[pd.Series, pd.Series, pd.Series]:
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    dif  = ema_fast - ema_slow
    dea  = dif.ewm(span=signal, adjust=False).mean()
    hist = 2 * (dif - dea)
    return dif, dea, hist


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low  - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(com=period - 1, min_periods=period, adjust=False).mean()


def _adx_dmi(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
             ) -> Tuple[pd.Series, pd.Series, pd.Series]:
    up   = high.diff()
    down = (-low.diff())
    dm_plus  = np.where((up > down) & (up > 0), up, 0.0)
    dm_minus = np.where((down > up) & (down > 0), down, 0.0)
    dm_plus  = pd.Series(dm_plus,  index=close.index)
    dm_minus = pd.Series(dm_minus, index=close.index)

    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low  - close.shift(1)).abs(),
    ], axis=1).max(axis=1)

    alpha = 1.0 / period
    atr_w    = tr.ewm(       alpha=alpha, min_periods=period, adjust=False).mean()
    dm_plus_w  = dm_plus.ewm( alpha=alpha, min_periods=period, adjust=False).mean()
    dm_minus_w = dm_minus.ewm(alpha=alpha, min_periods=period, adjust=False).mean()

    safe_atr = atr_w.replace(0, np.nan)
    di_plus  = 100 * dm_plus_w  / safe_atr
    di_minus = 100 * dm_minus_w / safe_atr

    denom = (di_plus + di_minus).replace(0, np.nan)
    dx    = 100 * (di_plus - di_minus).abs() / denom
    adx   = dx.ewm(alpha=alpha, min_periods=period, adjust=False).mean()

    return di_plus, di_minus, adx


def _forward_max_min(close_arr: np.ndarray, window: int = 20
                     ) -> Tuple[np.ndarray, np.ndarray]:
    """future_max_up / future_max_dd：T 日之后 window 天的最高/最低收盘价。"""
    n = len(close_arr)
    fmax = np.full(n, np.nan)
    fmin = np.full(n, np.nan)
    if n > window:
        from numpy.lib.stride_tricks import sliding_window_view
        wins = sliding_window_view(close_arr, window_shape=window)
        # wins[i] = close_arr[i : i+window]
        # 对应 T=i 时，未来窗口是 close_arr[i+1 : i+1+window]
        # 所以 wins[1:] 对应 T=0..n-window-1
        length = len(wins) - 1
        fmax[:length] = wins[1:].max(axis=1)
        fmin[:length] = wins[1:].min(axis=1)
    return fmax, fmin


def compute_symbol_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    给单只股票的时序数据计算全部特征（按 trade_date 升序）。
    输入列：trade_date, open, high, low, close, volume, turnover_rate, turnover_rate_f（可 NULL）
    输出：原列 + 所有特征列
    """
    df = df.sort_values("trade_date").copy()
    close  = df["close"]
    high   = df["high"]
    low    = df["low"]
    volume = df["volume"]

    # ── Moving Averages ──────────────────────────────────────────────────────
    df["ma5"]   = close.rolling(5,   min_periods=1).mean()
    df["ma10"]  = close.rolling(10,  min_periods=1).mean()
    df["ma20"]  = close.rolling(20,  min_periods=2).mean()
    df["ma60"]  = close.rolling(60,  min_periods=5).mean()
    df["ma150"] = close.rolling(150, min_periods=10).mean()
    df["ma200"] = close.rolling(200, min_periods=20).mean()

    # MA 斜率（相对于 5 天前 MA 的涨幅）
    df["ma5_slope"]  = df["ma5"].pct_change(5)
    df["ma20_slope"] = df["ma20"].pct_change(5)
    df["ma60_slope"] = df["ma60"].pct_change(5)

    # ── Returns ──────────────────────────────────────────────────────────────
    df["ret_1d"]  = close.pct_change(1)
    df["ret_5d"]  = close.pct_change(5)
    df["ret_20d"] = close.pct_change(20)
    df["ret_60d"] = close.pct_change(60)

    # ── 距离高点 / 区间位置 ─────────────────────────────────────────────────
    roll20_max = close.rolling(20,  min_periods=5).max()
    roll60_max = close.rolling(60,  min_periods=10).max()
    roll20_min = close.rolling(20,  min_periods=5).min()
    df["dist_20d_high"]    = close / roll20_max - 1
    df["dist_60d_high"]    = close / roll60_max - 1
    range_width = roll20_max - roll20_min
    df["pct_in_20d_range"] = (close - roll20_min) / range_width.replace(0, np.nan)

    # ── RSI / MACD ───────────────────────────────────────────────────────────
    df["rsi_14"] = _rsi(close, 14)
    df["macd_dif"], df["macd_dea"], df["macd_hist"] = _macd(close)

    # ── ATR / HV ─────────────────────────────────────────────────────────────
    df["atr_14"] = _atr(high, low, close, 14)
    log_ret      = np.log(close / close.shift(1))
    df["hv_20"]  = log_ret.rolling(20, min_periods=10).std() * np.sqrt(252)

    # ── Bollinger Bands (20, 2) ───────────────────────────────────────────────
    boll_ma  = close.rolling(20, min_periods=10).mean()
    boll_std = close.rolling(20, min_periods=10).std()
    df["boll_upper"] = boll_ma + 2 * boll_std
    df["boll_lower"] = boll_ma - 2 * boll_std
    df["boll_width"] = (df["boll_upper"] - df["boll_lower"]) / boll_ma.replace(0, np.nan)

    # ── ADX / DMI (14) ───────────────────────────────────────────────────────
    df["plus_di"], df["minus_di"], df["adx_14"] = _adx_dmi(high, low, close, 14)

    # ── Volume ratio (5d avg / 20d avg) ─────────────────────────────────────
    vol20 = volume.rolling(20, min_periods=5).mean().replace(0, np.nan)
    df["vol_ratio_5d"] = volume.rolling(5, min_periods=1).mean() / vol20

    # ── Stage（Weinstein/Minervini 风格，4 级）──────────────────────────────
    ma200_rising = df["ma200"] > df["ma200"].shift(63)
    s2 = (close > df["ma20"]) & (df["ma20"] > df["ma60"]) & \
         (df["ma60"] > df["ma200"]) & ma200_rising & df["ma200"].notna()
    s4 = (close < df["ma200"]) & df["ma200"].notna()
    s3 = (close >= df["ma200"]) & ma200_rising & ~s2 & df["ma200"].notna()
    s1 = (close >= df["ma200"]) & ~ma200_rising & ~s2 & df["ma200"].notna()

    stage = pd.Series(0, index=df.index, dtype=int)
    stage[s1] = 1
    stage[s2] = 2
    stage[s3] = 3
    stage[s4] = 4
    df["stage"] = stage

    # ── Future returns（仅用于 backtest_features_extra，不进 features_daily）
    close_arr = close.values
    fmax, fmin = _forward_max_min(close_arr, 20)
    df["future_ret_5d"]    = close.shift(-5)  / close - 1
    df["future_ret_10d"]   = close.shift(-10) / close - 1
    df["future_ret_20d"]   = close.shift(-20) / close - 1
    df["future_max_up_20d"]  = pd.Series(fmax, index=df.index) / close - 1
    df["future_max_dd_20d"]  = pd.Series(fmin, index=df.index) / close - 1

    return df


# ═════════════════════════════════════════════════════════════════════════════
# Section 5: 截面排名（RS Rank / turnover_rank_20d）
# ═════════════════════════════════════════════════════════════════════════════

def compute_rs_rank_cn(df_all_close: pd.DataFrame, df_watch: pd.DataFrame) -> pd.DataFrame:
    """
    用全市场 4797 只 CN 股的截面 63 日收益率排名。
    返回 DataFrame：symbol, trade_date, rs_rank (0~1)
    """
    log.info("  计算 CN RS Rank（全市场 4797 只截面）…")
    df = df_all_close.sort_values(["symbol", "trade_date"]).copy()
    df["ret_63d"] = df.groupby("symbol")["close"].pct_change(63)
    # 截面排名
    df["rs_rank"] = df.groupby("trade_date")["ret_63d"].rank(pct=True, na_option="keep")
    # 只保留 watchlist
    result = df[df["symbol"].isin(df_watch["symbol"].unique())][
        ["symbol", "trade_date", "rs_rank"]
    ]
    log.info(f"  CN RS Rank 计算完成：{len(result):,} 行")
    return result


def compute_rs_rank_us(df_watch_us: pd.DataFrame) -> pd.DataFrame:
    """
    US watchlist 内部截面 63 日收益率排名（29 只互相比较）。
    """
    log.info("  计算 US RS Rank（watchlist 内截面）…")
    df = df_watch_us.sort_values(["symbol", "trade_date"]).copy()
    df["ret_63d"] = df.groupby("symbol")["close"].pct_change(63)
    df["rs_rank"] = df.groupby("trade_date")["ret_63d"].rank(pct=True, na_option="keep")
    return df[["symbol", "trade_date", "rs_rank"]]


def compute_turnover_rank(df: pd.DataFrame, col: str = "turnover_rate_f") -> pd.Series:
    """
    截面换手率排名：在同一 trade_date，当只的 turnover_rate_f 在 watchlist 中的百分位。
    仅在 col 不全为 NULL 时计算，否则返回 NaN。
    """
    return df.groupby("trade_date")[col].rank(pct=True, na_option="keep")


# ═════════════════════════════════════════════════════════════════════════════
# Section 6: 组装最终 DataFrame
# ═════════════════════════════════════════════════════════════════════════════

def _nan_to_none(val):
    if val is None:
        return None
    if isinstance(val, float) and np.isnan(val):
        return None
    return val


def _df_to_records(df: pd.DataFrame, columns: List[str]) -> List[tuple]:
    """DataFrame → list of tuples，NaN 转 None（asyncpg COPY 需要）。"""
    sub = df[columns]
    result = []
    for row in sub.itertuples(index=False):
        result.append(tuple(_nan_to_none(v) for v in row))
    return result


def build_features_df(
    df_bars:   pd.DataFrame,
    df_fund:   pd.DataFrame,       # CN fundamentals (turnover_rate_f)
    rs_cn:     pd.DataFrame,       # CN rs_rank
    rs_us:     pd.DataFrame,       # US rs_rank
) -> pd.DataFrame:
    """
    把每只 symbol 的计算结果合并为一张宽表（features_daily 格式）。
    """
    log.info("  按 symbol 逐只计算技术指标…")
    all_parts = []

    symbols = df_bars["symbol"].unique()
    for i, sym in enumerate(symbols):
        sub = df_bars[df_bars["symbol"] == sym].copy()

        # 合并 turnover_rate_f（仅 CN）
        if sym in CN_WATCHLIST and len(df_fund) > 0:
            fund_sub = df_fund[df_fund["symbol"] == sym][["trade_date", "turnover_rate_f"]]
            sub = sub.merge(fund_sub, on="trade_date", how="left")
        else:
            sub["turnover_rate_f"] = np.nan

        sub = compute_symbol_features(sub)
        all_parts.append(sub)

        if (i + 1) % 10 == 0:
            log.info(f"    {i+1}/{len(symbols)} symbols 完成")

    df = pd.concat(all_parts, ignore_index=True)
    log.info(f"  技术指标计算完成：{len(df):,} 行")

    # ── 合并 RS Rank ─────────────────────────────────────────────────────────
    rs_all = pd.concat([rs_cn, rs_us], ignore_index=True)
    df = df.merge(rs_all, on=["symbol", "trade_date"], how="left", suffixes=("_drop", ""))
    if "rs_rank_drop" in df.columns:
        df.drop(columns=["rs_rank_drop"], inplace=True)

    # ── 截面换手率排名（CN 用 turnover_rate_f，US 用 turnover_rate）──────────
    cn_mask = df["symbol"].isin(CN_WATCHLIST)
    us_mask = ~cn_mask
    df.loc[cn_mask, "turnover_rank_20d"] = compute_turnover_rank(
        df[cn_mask], col="turnover_rate_f"
    )
    df.loc[us_mask, "turnover_rank_20d"] = compute_turnover_rank(
        df[us_mask], col="turnover_rate"
    )

    # ── available_date = trade_date（features_daily PIT 规则）───────────────
    df["available_date"] = df["trade_date"]
    df["extra"] = None

    log.info("  合并 RS Rank + turnover_rank_20d 完成")
    return df


# ═════════════════════════════════════════════════════════════════════════════
# Section 7: 写入 DB
# ═════════════════════════════════════════════════════════════════════════════

async def write_features_daily(pool: asyncpg.Pool, df: pd.DataFrame) -> None:
    log.info("=== Section 7a: 写入 features_daily ===")
    records = _df_to_records(df, _FD_COLUMNS)
    batch_size = 5000
    total = 0
    async with pool.acquire() as conn:
        for i in range(0, len(records), batch_size):
            batch = records[i: i + batch_size]
            await conn.copy_records_to_table(
                "features_daily", records=batch, columns=_FD_COLUMNS
            )
            total += len(batch)
            log.info(f"  features_daily: {total:,}/{len(records):,} 行已写入")
    log.info(f"features_daily 写入完成：{total:,} 行")


async def write_backtest_extra(pool: asyncpg.Pool, df: pd.DataFrame) -> None:
    log.info("=== Section 7b: 写入 backtest_features_extra ===")
    # 过滤掉 trade_date 在回测终点前 20 天（future_ret_20d 必为 NULL）
    cutoff = pd.Timestamp(COMPUTE_END)
    df_bfe = df[df["trade_date"] <= cutoff].copy()
    records = _df_to_records(df_bfe, _BFE_COLUMNS)
    async with pool.acquire() as conn:
        await conn.copy_records_to_table(
            "backtest_features_extra", records=records, columns=_BFE_COLUMNS
        )
    log.info(f"backtest_features_extra 写入完成：{len(records):,} 行")


# ═════════════════════════════════════════════════════════════════════════════
# Section 8: 验证报告
# ═════════════════════════════════════════════════════════════════════════════

async def print_validation_report(pool: asyncpg.Pool, df: pd.DataFrame) -> None:
    log.info("\n" + "=" * 60)
    log.info("数据验证报告 (Phase 2 完成)")
    log.info("=" * 60)

    # 1. 每只股票行数
    log.info("\n── 每只 symbol 行数（期望 900~1000 for 正常股票）──")
    counts = df.groupby("symbol").size().sort_values()
    for sym, cnt in counts.items():
        flag = "⚠️LOW" if cnt < 300 else ("⚠️HIGH" if cnt > 1100 else "")
        log.info(f"  {sym}: {cnt} 行 {flag}")

    # 2. NULL 比例（关键列）
    log.info("\n── NULL 比例（关键列）──")
    key_cols = ["ma20", "ma200", "rsi_14", "macd_dif", "adx_14",
                "rs_rank", "turnover_rank_20d", "stage", "ret_60d"]
    total = len(df)
    for col in key_cols:
        if col in df.columns:
            null_cnt = df[col].isna().sum()
            pct = null_cnt / total * 100
            flag = ""
            if col in ("ma20", "rsi_14", "macd_dif") and pct > 5:
                flag = "⚠️"
            log.info(f"  {col}: {null_cnt:,}/{total:,} NULL ({pct:.1f}%) {flag}")

    # 3. 随机抽 3 只 × 3 个日期的关键值（供人工对比 TradingView）
    log.info("\n── 随机抽样（请对比 TradingView/东方财富）──")
    sample_symbols = ["000063.SZ", "NVDA", "601318.SH"]
    sample_dates   = ["2024-01-15", "2024-06-28", "2025-09-30"]
    check_cols = ["close", "ma20", "rsi_14", "macd_dif", "stage", "rs_rank"]
    for sym in sample_symbols:
        for dt in sample_dates:
            mask = (df["symbol"] == sym) & (df["trade_date"] == pd.Timestamp(dt))
            row = df[mask]
            if len(row) == 0:
                log.info(f"  {sym} {dt}: 无数据（可能是非交易日或数据范围外）")
                continue
            vals = {c: f"{row[c].iloc[0]:.4f}" if pd.notna(row[c].iloc[0]) else "NULL"
                    for c in check_cols if c in row.columns}
            log.info(f"  {sym} {dt}: {vals}")

    # 4. backtest_features_extra 统计
    async with pool.acquire() as conn:
        bfe = await conn.fetchrow(
            "SELECT COUNT(*) as rows, SUM(CASE WHEN future_ret_20d IS NULL THEN 1 ELSE 0 END) as null_20d "
            "FROM backtest_features_extra WHERE symbol = ANY($1)",
            ALL_WATCHLIST,
        )
    log.info(f"\n── backtest_features_extra ──")
    log.info(f"  总行数: {bfe['rows']:,}，future_ret_20d NULL: {bfe['null_20d']:,} "
             f"（末尾 20 天正常）")

    log.info("\n请人工核对以上抽样值后，再进入 Phase 3（回测引擎）。")
    log.info("=" * 60)


# ═════════════════════════════════════════════════════════════════════════════
# main
# ═════════════════════════════════════════════════════════════════════════════

async def main() -> None:
    log.info("03_compute_features.py 启动")
    log.info(f"计算范围: {COMPUTE_START} ~ {COMPUTE_END}")
    log.info(f"Watchlist: CN={len(CN_WATCHLIST)} 只，US={len(US_WATCHLIST)} 只")

    pool = await _create_pool()
    try:
        # ── DDL ──────────────────────────────────────────────────────────────
        await run_ddl(pool)

        # ── DELETE ───────────────────────────────────────────────────────────
        await delete_existing(pool)

        # ── 加载数据 ─────────────────────────────────────────────────────────
        log.info("=== Section 3: 数据加载 ===")
        df_cn_all  = await load_cn_all_close(pool)
        df_bars    = await load_watchlist_bars(pool)
        df_fund    = await load_cn_fundamentals(pool)

        # ── RS Rank ──────────────────────────────────────────────────────────
        log.info("=== Section 4: RS Rank ===")
        df_bars_cn = df_bars[df_bars["market"] == "CN"]
        df_bars_us = df_bars[df_bars["market"] == "US"]
        rs_cn = compute_rs_rank_cn(df_cn_all, df_bars_cn)
        rs_us = compute_rs_rank_us(df_bars_us)

        # ── 特征计算 ─────────────────────────────────────────────────────────
        log.info("=== Section 5 & 6: 特征计算 + 合并 ===")
        df_features = build_features_df(df_bars, df_fund, rs_cn, rs_us)

        # ── 写入 DB ──────────────────────────────────────────────────────────
        await write_features_daily(pool, df_features)
        await write_backtest_extra(pool, df_features)

        # ── 验证报告 ─────────────────────────────────────────────────────────
        await print_validation_report(pool, df_features)

    finally:
        await pool.close()
        log.info("连接池已关闭")


if __name__ == "__main__":
    asyncio.run(main())
