"""
02_fetch_missing_data.py — 补拉 backtest 所需的全部历史数据

执行顺序（内部自动）:
  1. 交易日历校验
  2. 指数日线 (HS300/ZZ1000/SH + SPY/QQQ/VIX)
  3. CN 日线 bars (Category A 全量 + Category B 增量)
  4. CN fundamentals_daily (daily_basic)
  5. CN 资金流 moneyflow_daily
  6. CN 融资融券 margin_daily
  7. CN 北向资金 northbound_daily
  8. CN 季报 financials_quarterly
  9. CN 市场广度 market_breadth_daily (从 bars 计算)
  10. US bars + 财报 (yfinance)
  11. turnover_rate 回填
  12. 汇总报告

依赖: .env 中 DATABASE_URL / TUSHARE_TOKEN
      Tushare pro 账号（积分 >= 2000 以支持 moneyflow / fina_indicator）

运行方式:
    cd "P10-AlphaRadar "
    python backtest/scripts/02_fetch_missing_data.py

Phase 1 完成后会打印数据质量报告，等待审核再跑 03_compute_features.py。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

import asyncpg
import pandas as pd
import numpy as np
import yaml
from dotenv import load_dotenv

# ─── 路径 ────────────────────────────────────────────────────────────────────
_SCRIPT_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPT_DIR.parents[1]   # P10-AlphaRadar/
_BACKTEST_DIR = _SCRIPT_DIR.parent    # backtest/
sys.path.insert(0, str(_REPO_ROOT))

load_dotenv(_REPO_ROOT / ".env")

# ─── 配置 ─────────────────────────────────────────────────────────────────────
FETCH_START = date(2022, 6, 1)
FETCH_END   = date(2026, 4, 17)

# Tushare 格式
TS_START = FETCH_START.strftime("%Y%m%d")
TS_END   = FETCH_END.strftime("%Y%m%d")

# ─── 日志 ────────────────────────────────────────────────────────────────────
LOG_DIR = _BACKTEST_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "02_fetch.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

_FAILED: list[dict] = []   # 失败记录

# ─── Watchlist ────────────────────────────────────────────────────────────────
def _load_watchlist() -> tuple[list[dict], list[dict]]:
    wl_path = _BACKTEST_DIR / "config" / "watchlist.yaml"
    with open(wl_path) as f:
        wl = yaml.safe_load(f)["watchlist"]
    return wl["CN"], wl["US"]

# ─── DB 连接池 ─────────────────────────────────────────────────────────────────
async def _create_pool() -> asyncpg.Pool:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL 未设置，请检查 .env")
    return await asyncpg.create_pool(dsn, min_size=2, max_size=8, command_timeout=120)

# ─── Tushare 初始化 ──────────────────────────────────────────────────────────
def _init_tushare():
    token = os.environ.get("TUSHARE_TOKEN", "").strip()
    if not token or token == "placeholder":
        raise RuntimeError(
            "TUSHARE_TOKEN 未配置。请在 .env 中填入真实 token：\n"
            "  TUSHARE_TOKEN=your_real_token\n"
            "申请地址: https://tushare.pro/register"
        )
    import tushare as ts
    ts.set_token(token)
    pro = ts.pro_api()
    log.info("Tushare 初始化完成")
    return pro

# ─── 重试装饰器 ──────────────────────────────────────────────────────────────
def _ts_call(func, symbol: str, max_retries: int = 3, sleep_base: float = 0.5, **kwargs) -> Optional[pd.DataFrame]:
    """调用 Tushare API，指数退避重试，失败返回 None 并记录。"""
    for attempt in range(max_retries):
        try:
            time.sleep(sleep_base * (2 ** attempt))   # 0.5s / 1s / 2s
            df = func(**kwargs)
            if df is not None and not df.empty:
                return df
            return df  # 空 DataFrame 也算成功（无数据）
        except Exception as e:
            log.warning(f"  [{symbol}] Tushare 调用失败 attempt={attempt+1}: {e}")
            if attempt == max_retries - 1:
                _record_failure(symbol, "tushare", str(e))
                return None
    return None

def _yf_call(func, symbol: str, max_retries: int = 3, **kwargs) -> Any:
    """调用 yfinance，指数退避重试，失败返回 None。"""
    for attempt in range(max_retries):
        try:
            time.sleep(1.0 * (2 ** attempt))   # 1s / 2s / 4s
            return func(**kwargs)
        except Exception as e:
            log.warning(f"  [{symbol}] yfinance 调用失败 attempt={attempt+1}: {e}")
            if attempt == max_retries - 1:
                _record_failure(symbol, "yfinance", str(e))
                return None
    return None

def _record_failure(symbol: str, source: str, reason: str) -> None:
    _FAILED.append({"symbol": symbol, "source": source, "reason": reason})
    with open(LOG_DIR / "failed_symbols.log", "a", encoding="utf-8") as f:
        f.write(f"{date.today()} | {symbol} | {source} | {reason}\n")

# ─── DB 批量写入 ──────────────────────────────────────────────────────────────
async def _copy_insert(
    pool: asyncpg.Pool,
    table: str,
    columns: list[str],
    records: list[tuple],
    conflict: str = "DO NOTHING",
) -> int:
    """使用 COPY 协议批量写入，ON CONFLICT DO NOTHING 或 DO UPDATE。"""
    if not records:
        return 0
    cols = ", ".join(columns)
    placeholders = ", ".join(f"${i+1}" for i in range(len(columns)))
    sql = f"INSERT INTO {table} ({cols}) VALUES ({placeholders}) ON CONFLICT {conflict}"
    async with pool.acquire() as conn:
        await conn.executemany(sql, records)
    return len(records)

def _ts_date(d: Any) -> Optional[date]:
    """Tushare 返回的日期字符串 'YYYYMMDD' 或 None → date。"""
    if pd.isna(d) or d is None:
        return None
    s = str(d).strip()
    if len(s) == 8 and s.isdigit():
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    return None

def _safe(val: Any, default=None) -> Any:
    """NaN / None / inf → default。"""
    if val is None:
        return default
    try:
        if pd.isna(val) or (isinstance(val, float) and not np.isfinite(val)):
            return default
    except Exception:
        pass
    return val

# ═════════════════════════════════════════════════════════════════════════════
# Section 2: 指数日线
# ═════════════════════════════════════════════════════════════════════════════

async def fetch_index_daily(pool: asyncpg.Pool, pro) -> None:
    """拉取 CN 和 US 指数日线，写入 index_daily。"""
    log.info("=== Section 2: 指数日线 ===")

    cn_indexes = [
        ("000300.SH", "HS300"),
        ("000852.SH", "ZZ1000"),
        ("000001.SH", "SH"),
    ]
    us_indexes = [
        ("SPY", "SPY"),
        ("QQQ", "QQQ"),
        ("^VIX", "VIX"),
    ]

    total = 0

    # CN 指数（Tushare）
    for ts_code, alias in cn_indexes:
        df = _ts_call(pro.index_daily, ts_code, ts_code=ts_code, start_date=TS_START, end_date=TS_END)
        if df is None or df.empty:
            log.warning(f"  [{alias}] 无数据")
            continue
        records = []
        for _, row in df.iterrows():
            td = _ts_date(row.get("trade_date"))
            if td is None:
                continue
            records.append((
                alias, td,
                _safe(row.get("open")), _safe(row.get("high")),
                _safe(row.get("low")), _safe(row.get("close")),
                _safe(row.get("vol")),
                td,  # available_date = trade_date
            ))
        n = await _copy_insert(pool, "index_daily",
            ["index_code","trade_date","open","high","low","close","volume","available_date"],
            records)
        total += n
        log.info(f"  [{alias}] {n} 行写入")

    # US 指数（yfinance）
    import yfinance as yf
    for symbol, alias in us_indexes:
        ticker = yf.Ticker(symbol)
        hist = _yf_call(
            ticker.history, symbol,
            start=FETCH_START.strftime("%Y-%m-%d"),
            end=(FETCH_END + timedelta(days=1)).strftime("%Y-%m-%d"),
            auto_adjust=False,
        )
        if hist is None or hist.empty:
            log.warning(f"  [{alias}] 无数据")
            continue
        records = []
        for idx, row in hist.iterrows():
            td = idx.date() if hasattr(idx, "date") else idx
            records.append((
                alias, td,
                _safe(row.get("Open")), _safe(row.get("High")),
                _safe(row.get("Low")), _safe(row.get("Close")),
                int(_safe(row.get("Volume"), 0)),
                td,
            ))
        n = await _copy_insert(pool, "index_daily",
            ["index_code","trade_date","open","high","low","close","volume","available_date"],
            records)
        total += n
        log.info(f"  [{alias}] {n} 行写入")

    log.info(f"指数日线完成，共 {total} 行")

# ═════════════════════════════════════════════════════════════════════════════
# Section 3: CN 日线 bars
# ═════════════════════════════════════════════════════════════════════════════

async def fetch_cn_bars(pool: asyncpg.Pool, pro, cn_stocks: list[dict]) -> None:
    """补拉 CN watchlist 日线（Category A 全量 + Category B 增量）。"""
    log.info("=== Section 3: CN 日线 bars ===")

    # 查每只股票的现有最大日期
    symbols = [s["symbol"] for s in cn_stocks]
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT symbol, MAX(trade_date) AS max_date FROM market_bars_daily WHERE symbol = ANY($1) GROUP BY symbol",
            symbols,
        )
    existing_max = {r["symbol"]: r["max_date"] for r in rows}

    total = 0
    for stock in cn_stocks:
        sym = stock["symbol"]
        name = stock.get("name", sym)
        ts_code = sym  # Tushare 用同一格式

        max_date = existing_max.get(sym)
        if max_date is None:
            start = TS_START
            log.info(f"  [{sym} {name}] Category A 全量 {TS_START}~{TS_END}")
        elif max_date >= FETCH_END:
            log.debug(f"  [{sym}] 数据完整，跳过")
            continue
        else:
            start = (max_date + timedelta(days=1)).strftime("%Y%m%d")
            log.info(f"  [{sym} {name}] Category B 增量 {start}~{TS_END}")

        # 拉日线
        df = _ts_call(pro.daily, sym, ts_code=ts_code, start_date=start, end_date=TS_END)
        # 拉复权因子
        df_adj = _ts_call(pro.adj_factor, sym, ts_code=ts_code, start_date=start, end_date=TS_END)

        if df is None or df.empty:
            log.warning(f"  [{sym}] daily 无数据，跳过")
            continue

        # 构造 adj_factor 映射
        adj_map: dict[str, float] = {}
        if df_adj is not None and not df_adj.empty:
            for _, r in df_adj.iterrows():
                adj_map[str(r.get("trade_date", ""))] = float(_safe(r.get("adj_factor"), 1.0))

        records = []
        for _, row in df.iterrows():
            td_str = str(row.get("trade_date", ""))
            td = _ts_date(td_str)
            if td is None:
                continue
            close = _safe(row.get("close"))
            adj_f = adj_map.get(td_str, 1.0)
            adj_close = round(close * adj_f, 4) if close is not None else None
            vol = _safe(row.get("vol"))
            volume = int(vol * 100) if vol is not None else None   # 手 → 股
            amount_k = _safe(row.get("amount"))
            amount = amount_k * 1000 if amount_k is not None else None   # 千元 → 元
            turnover = _safe(row.get("turnover_rate"))   # Tushare daily 没有此字段，daily_basic 有
            records.append((
                sym, "CN", td,
                _safe(row.get("open")), _safe(row.get("high")),
                _safe(row.get("low")), close,
                volume, amount, turnover, adj_f, adj_close,
                None,  # turnover_rate 暂 NULL，Section 4 的 daily_basic 会补
                td,    # available_date = trade_date
            ))

        n = await _copy_insert(pool, "market_bars_daily",
            ["symbol","market","trade_date","open","high","low","close",
             "volume","amount","turnover","adj_factor","adj_close","turnover_rate","available_date"],
            records)
        total += n
        log.info(f"  [{sym}] {n} 行写入")

    log.info(f"CN bars 完成，共 {total} 行")

# ═════════════════════════════════════════════════════════════════════════════
# Section 4: CN fundamentals_daily + turnover_rate 回填
# ═════════════════════════════════════════════════════════════════════════════

async def fetch_cn_fundamentals_daily(pool: asyncpg.Pool, pro, cn_stocks: list[dict]) -> None:
    """从 daily_basic 拉 PE/PB/市值，同步回填 market_bars_daily.turnover_rate。"""
    log.info("=== Section 4: CN fundamentals_daily ===")
    total_fd = 0

    for stock in cn_stocks:
        sym = stock["symbol"]
        name = stock.get("name", sym)

        df = _ts_call(pro.daily_basic, sym,
                      ts_code=sym, start_date=TS_START, end_date=TS_END,
                      fields="ts_code,trade_date,pe_ttm,pb,ps_ttm,total_mv,circ_mv,turnover_rate_f,turnover_rate")

        if df is None or df.empty:
            log.warning(f"  [{sym}] daily_basic 无数据")
            continue

        fd_records = []
        bar_updates: list[tuple] = []   # (turnover_rate, symbol, trade_date)

        for _, row in df.iterrows():
            td = _ts_date(row.get("trade_date"))
            if td is None:
                continue
            # fundamentals_daily
            total_mv = _safe(row.get("total_mv"))
            circ_mv  = _safe(row.get("circ_mv"))
            fd_records.append((
                sym, td,
                _safe(row.get("pe_ttm")),
                _safe(row.get("pb")),
                _safe(row.get("ps_ttm")),
                total_mv * 10000 if total_mv is not None else None,  # 万元→元
                circ_mv  * 10000 if circ_mv  is not None else None,
                _safe(row.get("turnover_rate_f")),
                td,   # available_date
            ))
            # market_bars_daily.turnover_rate 回填
            tr = _safe(row.get("turnover_rate"))
            if tr is not None:
                bar_updates.append((tr, sym, td))

        n_fd = await _copy_insert(pool, "fundamentals_daily",
            ["symbol","trade_date","pe_ttm","pb","ps_ttm","total_mv","circ_mv","turnover_rate_f","available_date"],
            fd_records,
            conflict="(symbol, trade_date) DO UPDATE SET pe_ttm=EXCLUDED.pe_ttm, pb=EXCLUDED.pb, "
                     "ps_ttm=EXCLUDED.ps_ttm, total_mv=EXCLUDED.total_mv, circ_mv=EXCLUDED.circ_mv, "
                     "turnover_rate_f=EXCLUDED.turnover_rate_f, available_date=EXCLUDED.available_date")
        total_fd += n_fd

        # 批量更新 turnover_rate
        if bar_updates:
            async with pool.acquire() as conn:
                await conn.executemany(
                    "UPDATE market_bars_daily SET turnover_rate=$1 WHERE symbol=$2 AND trade_date=$3",
                    bar_updates,
                )
        log.info(f"  [{sym} {name}] fd={n_fd} 行，turnover_rate 更新 {len(bar_updates)} 行")

    log.info(f"CN fundamentals_daily 完成，共 {total_fd} 行")

# ═════════════════════════════════════════════════════════════════════════════
# Section 5: CN moneyflow_daily
# ═════════════════════════════════════════════════════════════════════════════

async def fetch_cn_moneyflow(pool: asyncpg.Pool, pro, cn_stocks: list[dict]) -> None:
    """从 Tushare moneyflow 拉大/中/小单净流入，available_date = 下一个交易日。"""
    log.info("=== Section 5: CN moneyflow_daily ===")

    # 预加载交易日历（用于计算 T+1 available_date）
    async with pool.acquire() as conn:
        cal_rows = await conn.fetch(
            "SELECT trade_date FROM trade_calendar WHERE trade_date BETWEEN $1 AND $2 ORDER BY trade_date",
            FETCH_START, FETCH_END + timedelta(days=5),
        )
    trading_days = sorted([r["trade_date"] for r in cal_rows])
    next_td: dict[date, date] = {}
    for i, d in enumerate(trading_days):
        if i + 1 < len(trading_days):
            next_td[d] = trading_days[i + 1]

    total = 0
    for stock in cn_stocks:
        sym = stock["symbol"]
        df = _ts_call(pro.moneyflow, sym,
                      ts_code=sym, start_date=TS_START, end_date=TS_END)
        if df is None or df.empty:
            log.warning(f"  [{sym}] moneyflow 无数据（可能积分不足）")
            continue

        records = []
        for _, row in df.iterrows():
            td = _ts_date(row.get("trade_date"))
            if td is None:
                continue
            avail = next_td.get(td, td + timedelta(days=1))
            net_lg = _safe(row.get("buy_lg_amount")) and _safe(row.get("sell_lg_amount")) and \
                     (_safe(row.get("buy_lg_amount"), 0) - _safe(row.get("sell_lg_amount"), 0))
            net_md = _safe(row.get("buy_md_amount"), 0) - _safe(row.get("sell_md_amount"), 0)
            net_sm = _safe(row.get("buy_sm_amount"), 0) - _safe(row.get("sell_sm_amount"), 0)
            # Tushare moneyflow 直接有 net_mf_amount 字段（大单净流入），优先使用
            if "net_mf_amount" in row.index:
                net_lg = _safe(row.get("buy_elg_amount"), 0) + _safe(row.get("buy_lg_amount"), 0) \
                         - _safe(row.get("sell_elg_amount"), 0) - _safe(row.get("sell_lg_amount"), 0)
            records.append((sym, td, net_lg, net_md, net_sm, avail))

        n = await _copy_insert(pool, "moneyflow_daily",
            ["symbol","trade_date","net_lg_amount","net_md_amount","net_sm_amount","available_date"],
            records,
            conflict="(symbol, trade_date) DO NOTHING")
        total += n
        log.info(f"  [{sym}] {n} 行")

    log.info(f"CN moneyflow 完成，共 {total} 行")

# ═════════════════════════════════════════════════════════════════════════════
# Section 6: CN margin_daily
# ═════════════════════════════════════════════════════════════════════════════

async def fetch_cn_margin(pool: asyncpg.Pool, pro, cn_stocks: list[dict]) -> None:
    """拉融资融券数据，写入 margin_daily。"""
    log.info("=== Section 6: CN margin_daily ===")
    total = 0
    for stock in cn_stocks:
        sym = stock["symbol"]
        df = _ts_call(pro.margin_detail, sym,
                      ts_code=sym, start_date=TS_START, end_date=TS_END)
        if df is None or df.empty:
            log.debug(f"  [{sym}] margin 无数据")
            continue
        records = []
        for _, row in df.iterrows():
            td = _ts_date(row.get("trade_date"))
            if td is None:
                continue
            records.append((
                sym, td,
                _safe(row.get("rzye")),
                _safe(row.get("rzmre")),
                _safe(row.get("rqye")),
                td,   # available_date = trade_date（融资融券当日收盘后可查）
            ))
        n = await _copy_insert(pool, "margin_daily",
            ["symbol","trade_date","rzye","rzmre","rqye","available_date"],
            records,
            conflict="(symbol, trade_date) DO NOTHING")
        total += n
        log.info(f"  [{sym}] {n} 行")
    log.info(f"CN margin 完成，共 {total} 行")

# ═════════════════════════════════════════════════════════════════════════════
# Section 7: CN northbound_daily
# ═════════════════════════════════════════════════════════════════════════════

async def fetch_cn_northbound(pool: asyncpg.Pool, pro) -> None:
    """拉北向资金净买入数据，写入 northbound_daily。"""
    log.info("=== Section 7: CN northbound_daily ===")

    df = _ts_call(pro.moneyflow_hsgt, "northbound",
                  start_date=TS_START, end_date=TS_END)
    if df is None or df.empty:
        log.warning("  northbound 无数据（可能积分不足）")
        return

    records = []
    for _, row in df.iterrows():
        td = _ts_date(row.get("trade_date"))
        if td is None:
            continue
        records.append((
            td,
            _safe(row.get("sh_moneyflow")),    # 沪股通净流入（亿元）
            _safe(row.get("sz_moneyflow")),    # 深股通净流入
            _safe(row.get("north_moneyflow")), # 北向合计
            td,  # available_date
        ))

    n = await _copy_insert(pool, "northbound_daily",
        ["trade_date","sh_net_buy","sz_net_buy","total_net_buy","available_date"],
        records,
        conflict="(trade_date) DO NOTHING")
    log.info(f"  northbound {n} 行写入")

# ═════════════════════════════════════════════════════════════════════════════
# Section 8: CN financials_quarterly
# ═════════════════════════════════════════════════════════════════════════════

async def fetch_cn_financials(pool: asyncpg.Pool, pro, cn_stocks: list[dict]) -> None:
    """从 income + balancesheet + cashflow + fina_indicator 拼合季报，写入 financials_quarterly。"""
    log.info("=== Section 8: CN financials_quarterly ===")
    total = 0

    for stock in cn_stocks:
        sym = stock["symbol"]
        name = stock.get("name", sym)

        # 4 个 API 各拉一次
        kwargs_base = dict(ts_code=sym, start_date="20210101", end_date=TS_END, report_type="1")

        df_inc  = _ts_call(pro.income,       sym, **kwargs_base)
        df_bs   = _ts_call(pro.balancesheet, sym, **kwargs_base)
        df_cf   = _ts_call(pro.cashflow,     sym, **kwargs_base)
        df_fi   = _ts_call(pro.fina_indicator, sym, ts_code=sym, start_date="20210101", end_date=TS_END)

        # income 是主表，必须有
        if df_inc is None or df_inc.empty:
            log.warning(f"  [{sym}] income 无数据，跳过")
            continue

        # 以 (end_date, ann_date) 为 key 合并
        inc_map:  dict[str, Any] = {}
        bs_map:   dict[str, Any] = {}
        cf_map:   dict[str, Any] = {}
        fi_map:   dict[str, Any] = {}

        for _, r in df_inc.iterrows():
            k = str(r.get("end_date", ""))
            inc_map[k] = r
        if df_bs is not None:
            for _, r in df_bs.iterrows():
                bs_map[str(r.get("end_date", ""))] = r
        if df_cf is not None:
            for _, r in df_cf.iterrows():
                cf_map[str(r.get("end_date", ""))] = r
        if df_fi is not None:
            for _, r in df_fi.iterrows():
                fi_map[str(r.get("end_date", ""))] = r

        records = []
        for end_key, inc in inc_map.items():
            report_date = _ts_date(end_key)
            if report_date is None:
                continue
            ann_date = _ts_date(inc.get("ann_date"))
            # available_date = ann_date；NULL 时退回 report_date + 45 天
            avail = ann_date if ann_date else report_date + timedelta(days=45)

            bs = bs_map.get(end_key, {})
            cf = cf_map.get(end_key, {})
            fi = fi_map.get(end_key, {})

            revenue  = _safe(inc.get("total_revenue") or inc.get("revenue"))
            net_profit = _safe(inc.get("n_income_attr_p") or inc.get("net_profit"))
            # 同比增速：income 表有 n_income_attr_p_yoy（净利润同比）
            np_yoy   = _safe(inc.get("n_income_attr_p_yoy") or fi.get("netprofit_yoy"))
            # 营收同比，income 表暂无直接字段，用 fina_indicator
            rev_yoy  = _safe(fi.get("or_yoy"))

            total_assets = _safe(bs.get("total_assets"))
            total_liab   = _safe(bs.get("total_liab"))
            goodwill     = _safe(bs.get("goodwill"))
            ocf          = _safe(cf.get("n_cashflow_act"))

            gross_margin = _safe(fi.get("grossprofit_margin"))
            net_margin   = _safe(fi.get("netprofit_margin") or fi.get("profit_to_gr"))
            debt_ratio   = _safe(fi.get("debt_to_assets"))
            current_ratio= _safe(fi.get("current_ratio"))
            ocf_to_np    = _safe(fi.get("ocf_to_profit"))
            roe_ttm      = _safe(fi.get("roe_dt") or fi.get("roe_avg"))
            roa_ttm      = _safe(fi.get("roa2") or fi.get("roa"))
            dupont_npm   = net_margin
            dupont_tat   = _safe(fi.get("assets_turn"))
            # dupont_em = 1 / (1 - debt_ratio)，若 debt_ratio 有效
            dupont_em    = None
            if debt_ratio is not None and 0 < debt_ratio < 1:
                dupont_em = round(1 / (1 - debt_ratio), 4)

            # revenue_qoq 暂时不计算（需要前一季度数据，留给 03_compute）
            records.append((
                sym, report_date, ann_date,
                revenue, rev_yoy, None,   # revenue_qoq=None
                net_profit, np_yoy,
                gross_margin, net_margin,
                total_assets, total_liab,
                debt_ratio, current_ratio,
                goodwill, ocf, ocf_to_np,
                roe_ttm, roa_ttm,
                dupont_npm, dupont_tat, dupont_em,
                avail,
            ))

        n = await _copy_insert(pool, "financials_quarterly",
            ["symbol","report_date","announce_date",
             "revenue","revenue_yoy","revenue_qoq",
             "net_profit","np_yoy","gross_margin","net_margin",
             "total_assets","total_liab","debt_ratio","current_ratio",
             "goodwill","ocf","ocf_to_np","roe_ttm","roa_ttm",
             "dupont_npm","dupont_tat","dupont_em","available_date"],
            records,
            conflict="(symbol, report_date) DO UPDATE SET "
                     "announce_date=EXCLUDED.announce_date, available_date=EXCLUDED.available_date, "
                     "revenue=EXCLUDED.revenue, net_profit=EXCLUDED.net_profit, "
                     "roe_ttm=EXCLUDED.roe_ttm, roa_ttm=EXCLUDED.roa_ttm")
        total += n
        log.info(f"  [{sym} {name}] {n} 行季报")

    log.info(f"CN financials 完成，共 {total} 行")

# ═════════════════════════════════════════════════════════════════════════════
# Section 9: CN 市场广度（从 market_bars_daily 计算）
# ═════════════════════════════════════════════════════════════════════════════

async def compute_market_breadth(pool: asyncpg.Pool) -> None:
    """从 market_bars_daily 计算每日涨跌停/涨跌家数/60日新高新低，写入 market_breadth_daily。"""
    log.info("=== Section 9: 市场广度（从 bars 计算）===")

    sql = """
    INSERT INTO market_breadth_daily
        (trade_date, market, limit_up_count, limit_down_count,
         advancing_count, declining_count, new_high_count, new_low_count,
         total_stocks, available_date)
    SELECT
        b.trade_date,
        'CN' AS market,
        SUM(CASE WHEN b.pct_chg >= 9.9 THEN 1 ELSE 0 END) AS limit_up_count,
        SUM(CASE WHEN b.pct_chg <= -9.9 THEN 1 ELSE 0 END) AS limit_down_count,
        SUM(CASE WHEN b.pct_chg > 0 THEN 1 ELSE 0 END) AS advancing_count,
        SUM(CASE WHEN b.pct_chg < 0 THEN 1 ELSE 0 END) AS declining_count,
        0 AS new_high_count,
        0 AS new_low_count,
        COUNT(*) AS total_stocks,
        b.trade_date AS available_date
    FROM (
        SELECT trade_date,
               (close / NULLIF(LAG(close) OVER (PARTITION BY symbol ORDER BY trade_date), 0) - 1) * 100 AS pct_chg
        FROM market_bars_daily
        WHERE market = 'CN'
          AND trade_date BETWEEN $1 AND $2
          AND close > 0
    ) b
    GROUP BY b.trade_date
    ON CONFLICT (trade_date) DO UPDATE SET
        limit_up_count   = EXCLUDED.limit_up_count,
        limit_down_count = EXCLUDED.limit_down_count,
        advancing_count  = EXCLUDED.advancing_count,
        declining_count  = EXCLUDED.declining_count,
        total_stocks     = EXCLUDED.total_stocks
    """
    async with pool.acquire() as conn:
        await conn.execute(sql, FETCH_START, FETCH_END)

    r = await pool.fetchval("SELECT COUNT(*) FROM market_breadth_daily")
    log.info(f"  market_breadth_daily: {r} 行")

# ═════════════════════════════════════════════════════════════════════════════
# Section 10: US 数据 (yfinance)
# ═════════════════════════════════════════════════════════════════════════════

async def fetch_us_data(pool: asyncpg.Pool, us_stocks: list[dict]) -> None:
    """拉取 US watchlist 的日线 bars + 财报，写入对应表。"""
    log.info("=== Section 10: US 数据 (yfinance) ===")
    import yfinance as yf

    yf_start = FETCH_START.strftime("%Y-%m-%d")
    yf_end   = (FETCH_END + timedelta(days=1)).strftime("%Y-%m-%d")

    total_bars = 0
    total_fin  = 0

    for stock in us_stocks:
        sym = stock["symbol"]
        name = stock.get("name", sym)
        log.info(f"  [{sym} {name}] 开始拉取")

        ticker = yf.Ticker(sym)

        # ── 日线 bars ──────────────────────────────────────────────────────
        hist = _yf_call(ticker.history, sym, start=yf_start, end=yf_end, auto_adjust=False)
        if hist is not None and not hist.empty:
            records = []
            for idx, row in hist.iterrows():
                td = idx.date() if hasattr(idx, "date") else idx
                close = _safe(row.get("Close"))
                adj_close = _safe(row.get("Adj Close"))
                adj_f = round(adj_close / close, 6) if (close and close > 0 and adj_close) else 1.0
                records.append((
                    sym, "US", td,
                    _safe(row.get("Open")), _safe(row.get("High")),
                    _safe(row.get("Low")), close,
                    int(_safe(row.get("Volume"), 0)),
                    None,    # amount (USD 无此概念)
                    None,    # turnover
                    adj_f, adj_close,
                    None,    # turnover_rate
                    td,      # available_date
                ))
            n = await _copy_insert(pool, "market_bars_daily",
                ["symbol","market","trade_date","open","high","low","close",
                 "volume","amount","turnover","adj_factor","adj_close","turnover_rate","available_date"],
                records,
                conflict="(symbol, trade_date) DO NOTHING")
            total_bars += n
            log.info(f"    bars: {n} 行")
        else:
            log.warning(f"    [{sym}] bars 无数据")

        # ── 季报财报 ────────────────────────────────────────────────────────
        try:
            income_q  = ticker.quarterly_income_stmt
            balance_q = ticker.quarterly_balance_sheet
            cashflow_q = ticker.quarterly_cashflow
        except Exception as e:
            log.warning(f"    [{sym}] 财报拉取失败: {e}")
            _record_failure(sym, "yfinance_financials", str(e))
            income_q = balance_q = cashflow_q = None

        if income_q is not None and not income_q.empty:
            fin_records = []
            for col in income_q.columns:
                report_date = col.date() if hasattr(col, "date") else col
                # yfinance 财报无 announce_date，用 report_date + 45 天作为保守 available_date
                avail = report_date + timedelta(days=45)

                def _get(df, field, col=col):
                    if df is None or df.empty:
                        return None
                    return _safe(df.get(field, {}).get(col))

                revenue   = _get(income_q, "Total Revenue")
                net_profit= _get(income_q, "Net Income")
                gross_prof= _get(income_q, "Gross Profit")
                gross_margin = round(gross_prof / revenue, 4) if (gross_prof and revenue and revenue > 0) else None
                net_margin   = round(net_profit / revenue, 4) if (net_profit and revenue and revenue > 0) else None

                total_assets = _get(balance_q, "Total Assets")
                total_liab   = _get(balance_q, "Total Liabilities Net Minority Interest")
                if total_liab is None:
                    total_liab = _get(balance_q, "Total Liabilities")
                goodwill     = _get(balance_q, "Goodwill")
                equity       = _get(balance_q, "Stockholders Equity")
                debt_ratio   = round(total_liab / total_assets, 4) if (total_liab and total_assets and total_assets > 0) else None
                current_assets = _get(balance_q, "Current Assets")
                current_liab   = _get(balance_q, "Current Liabilities")
                current_ratio  = round(current_assets / current_liab, 4) if (current_assets and current_liab and current_liab > 0) else None

                ocf      = _get(cashflow_q, "Operating Cash Flow")
                ocf_to_np = round(ocf / net_profit, 4) if (ocf and net_profit and net_profit != 0) else None
                roe_ttm  = round(net_profit / equity, 4) if (net_profit and equity and equity > 0) else None
                roa_ttm  = round(net_profit / total_assets, 4) if (net_profit and total_assets and total_assets > 0) else None

                fin_records.append((
                    sym, report_date, None,   # announce_date = None (yfinance 无)
                    revenue, None, None,      # rev_yoy, rev_qoq
                    net_profit, None,         # np_yoy
                    gross_margin, net_margin,
                    total_assets, total_liab,
                    debt_ratio, current_ratio,
                    goodwill, ocf, ocf_to_np,
                    roe_ttm, roa_ttm,
                    net_margin, None, None,   # dupont_npm, tat, em
                    avail,
                ))

            nf = await _copy_insert(pool, "financials_quarterly",
                ["symbol","report_date","announce_date",
                 "revenue","revenue_yoy","revenue_qoq",
                 "net_profit","np_yoy","gross_margin","net_margin",
                 "total_assets","total_liab","debt_ratio","current_ratio",
                 "goodwill","ocf","ocf_to_np","roe_ttm","roa_ttm",
                 "dupont_npm","dupont_tat","dupont_em","available_date"],
                fin_records,
                conflict="(symbol, report_date) DO UPDATE SET "
                         "revenue=EXCLUDED.revenue, net_profit=EXCLUDED.net_profit, "
                         "available_date=EXCLUDED.available_date")
            total_fin += nf
            log.info(f"    financials: {nf} 行")

        # ── fundamentals_daily (当前快照) ──────────────────────────────────
        try:
            info = ticker.info
            if info:
                pe_ttm   = _safe(info.get("trailingPE"))
                pb       = _safe(info.get("priceToBook"))
                ps_ttm   = _safe(info.get("priceToSalesTrailing12Months"))
                total_mv = _safe(info.get("marketCap"))
                async with pool.acquire() as conn:
                    await conn.execute("""
                        INSERT INTO fundamentals_daily
                            (symbol, trade_date, pe_ttm, pb, ps_ttm, total_mv, available_date)
                        VALUES ($1, $2, $3, $4, $5, $6, $2)
                        ON CONFLICT (symbol, trade_date) DO UPDATE SET
                            pe_ttm=EXCLUDED.pe_ttm, pb=EXCLUDED.pb,
                            ps_ttm=EXCLUDED.ps_ttm, total_mv=EXCLUDED.total_mv
                    """, sym, FETCH_END, pe_ttm, pb, ps_ttm, total_mv)
        except Exception as e:
            log.debug(f"    [{sym}] info 获取失败（非关键）: {e}")

    log.info(f"US 数据完成：bars={total_bars}，financials={total_fin} 行")

# ═════════════════════════════════════════════════════════════════════════════
# Section 11: turnover_rate 最终回填（从 turnover 列）
# ═════════════════════════════════════════════════════════════════════════════

async def backfill_turnover_rate(pool: asyncpg.Pool) -> None:
    """对仍为 NULL 的 turnover_rate，从 turnover 列补回填。"""
    log.info("=== Section 11: turnover_rate 兜底回填 ===")
    async with pool.acquire() as conn:
        result = await conn.execute("""
            UPDATE market_bars_daily
            SET turnover_rate = turnover
            WHERE turnover_rate IS NULL AND turnover IS NOT NULL
        """)
    log.info(f"  turnover_rate 回填: {result}")

# ═════════════════════════════════════════════════════════════════════════════
# 汇总报告
# ═════════════════════════════════════════════════════════════════════════════

async def print_report(pool: asyncpg.Pool, cn_stocks: list[dict], us_stocks: list[dict]) -> None:
    """打印数据质量报告，供人工审核后再运行 03_compute_features.py。"""
    log.info("\n" + "=" * 60)
    log.info("数据质量报告 (Phase 1 完成)")
    log.info("=" * 60)

    checks = [
        ("market_bars_daily (CN watchlist)", f"""
            SELECT COUNT(*) AS rows, COUNT(DISTINCT symbol) AS symbols,
                   MIN(trade_date) AS min_dt, MAX(trade_date) AS max_dt,
                   SUM(CASE WHEN turnover_rate IS NULL THEN 1 ELSE 0 END) AS null_tr
            FROM market_bars_daily
            WHERE symbol = ANY($1) AND market='CN'
        """, [s["symbol"] for s in cn_stocks]),
        ("market_bars_daily (US watchlist)", f"""
            SELECT COUNT(*) AS rows, COUNT(DISTINCT symbol) AS symbols,
                   MIN(trade_date) AS min_dt, MAX(trade_date) AS max_dt
            FROM market_bars_daily
            WHERE symbol = ANY($1) AND market='US'
        """, [s["symbol"] for s in us_stocks]),
        ("fundamentals_daily (CN)", """
            SELECT COUNT(*) AS rows, COUNT(DISTINCT symbol) AS symbols,
                   MIN(trade_date), MAX(trade_date)
            FROM fundamentals_daily WHERE market IS NULL OR market='CN'
        """, []),
        ("financials_quarterly", """
            SELECT COUNT(*) AS rows, COUNT(DISTINCT symbol) AS symbols,
                   SUM(CASE WHEN announce_date IS NULL THEN 1 ELSE 0 END) AS null_ann
            FROM financials_quarterly
        """, []),
        ("moneyflow_daily", """
            SELECT COUNT(*) AS rows, COUNT(DISTINCT symbol) AS symbols
            FROM moneyflow_daily
        """, []),
        ("index_daily", """
            SELECT COUNT(*) AS rows, COUNT(DISTINCT index_code) AS codes
            FROM index_daily
        """, []),
        ("market_breadth_daily", """
            SELECT COUNT(*) AS rows, MIN(trade_date), MAX(trade_date)
            FROM market_breadth_daily
        """, []),
    ]

    async with pool.acquire() as conn:
        for label, sql, params in checks:
            try:
                if params:
                    r = await conn.fetchrow(sql, params)
                else:
                    r = await conn.fetchrow(sql)
                log.info(f"  {label}: {dict(r)}")
            except Exception as e:
                log.warning(f"  {label}: 查询失败 {e}")

    # CN watchlist 覆盖缺口
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT symbol, MAX(trade_date) AS max_dt
            FROM market_bars_daily WHERE symbol = ANY($1) AND market='CN'
            GROUP BY symbol
        """, [s["symbol"] for s in cn_stocks])
    have = {r["symbol"]: r["max_dt"] for r in rows}
    missing_sym = [s["symbol"] for s in cn_stocks if s["symbol"] not in have]
    stale_sym   = [(s["symbol"], have[s["symbol"]]) for s in cn_stocks
                   if s["symbol"] in have and have[s["symbol"]] < FETCH_END]

    if missing_sym:
        log.warning(f"  ⚠️  CN watchlist 仍完全缺失: {missing_sym}")
    if stale_sym:
        log.warning(f"  ⚠️  CN watchlist 仍有截止旧的 symbol: {stale_sym[:5]}...")

    # 失败汇总
    log.info(f"\n失败记录 ({len(_FAILED)} 条):")
    for f in _FAILED:
        log.info(f"  {f['symbol']} | {f['source']} | {f['reason'][:80]}")

    if _FAILED:
        log.warning("\n⚠️  部分 symbol 拉取失败，请检查 backtest/logs/failed_symbols.log")
        log.warning("   失败 symbol 的分析模块将降级（技术面仍可计算，基本面/资金面数据缺失）")
    else:
        log.info("\n✅ 所有 symbol 拉取成功")

    log.info("\n请人工审核以上数据质量，确认后再运行:")
    log.info("  python backtest/scripts/03_compute_features.py")

# ═════════════════════════════════════════════════════════════════════════════
# main
# ═════════════════════════════════════════════════════════════════════════════

async def main() -> None:
    log.info("02_fetch_missing_data.py 启动")
    log.info(f"补拉范围: {FETCH_START} ~ {FETCH_END}")

    cn_stocks, us_stocks = _load_watchlist()
    log.info(f"Watchlist: CN={len(cn_stocks)} 只，US={len(us_stocks)} 只")

    pool = await _create_pool()
    pro  = _init_tushare()

    try:
        await fetch_index_daily(pool, pro)
        await fetch_cn_bars(pool, pro, cn_stocks)
        await fetch_cn_fundamentals_daily(pool, pro, cn_stocks)
        await fetch_cn_moneyflow(pool, pro, cn_stocks)
        await fetch_cn_margin(pool, pro, cn_stocks)
        await fetch_cn_northbound(pool, pro)
        await fetch_cn_financials(pool, pro, cn_stocks)
        await compute_market_breadth(pool)
        await fetch_us_data(pool, us_stocks)
        await backfill_turnover_rate(pool)
        await print_report(pool, cn_stocks, us_stocks)
    finally:
        await pool.close()
        log.info("连接池已关闭")


if __name__ == "__main__":
    asyncio.run(main())
