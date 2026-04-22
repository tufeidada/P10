#!/usr/bin/env python3
"""
P10-AlphaRadar 数据拉取脚本

从 Tushare 拉取 Regime 计算所需的各类数据，写入 PostgreSQL。
同时修复 P6 迁移数据的 symbol 格式（添加交易所后缀）。

用法:
    # 拉取全部数据（指数+北向+情绪+修复symbol）
    python scripts/pull_data.py --all

    # 只拉指数日线
    python scripts/pull_data.py --index

    # 只修复 symbol 格式
    python scripts/pull_data.py --fix-symbols

    # 指定日期范围
    python scripts/pull_data.py --all --start 2021-03-01 --end 2026-04-16
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from datetime import date, datetime, timedelta

import pandas as pd
import structlog
import tushare as ts

# 让 import 找到项目根目录
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.connection import init_pool, close_pool, db_execute, db_execute_many, db_query, db_query_val

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="%H:%M:%S"),
        structlog.dev.ConsoleRenderer(),
    ],
)
logger = structlog.get_logger()

# Tushare 限流：每分钟约 200 次调用，保守起见每次间隔 0.3s
RATE_LIMIT_SLEEP = 0.3


def get_pro() -> ts.pro_api:
    token = os.environ.get("TUSHARE_TOKEN")
    if not token or token.startswith("your_"):
        logger.error("TUSHARE_TOKEN 未配置")
        sys.exit(1)
    return ts.pro_api(token)


def query_with_retry(pro, api_name: str, retries: int = 3, **kwargs) -> pd.DataFrame:
    """调用 Tushare API，带重试。"""
    for attempt in range(retries + 1):
        try:
            func = getattr(pro, api_name)
            df = func(**kwargs)
            if df is not None and not df.empty:
                return df
            return pd.DataFrame()
        except Exception as e:
            if attempt < retries:
                wait = 30 * (attempt + 1)
                logger.warning("tushare_retry", api=api_name, attempt=attempt + 1, error=str(e), wait=wait)
                time.sleep(wait)
            else:
                logger.error("tushare_failed", api=api_name, error=str(e))
                raise
    return pd.DataFrame()


# ============================================================
# 1. 指数日线
# ============================================================

async def pull_index_daily(pro, start: str, end: str) -> None:
    """拉取指数日线数据写入 market_bars_daily。"""
    indices = [
        ("000300.SH", "沪深300"),
        ("399852.SZ", "中证1000"),
        ("000001.SH", "上证指数"),
        ("399006.SZ", "创业板指"),
    ]

    for ts_code, name in indices:
        logger.info("pulling_index", symbol=ts_code, name=name, start=start, end=end)

        df = await asyncio.to_thread(
            query_with_retry, pro, "index_daily",
            ts_code=ts_code, start_date=start.replace("-", ""), end_date=end.replace("-", ""),
        )

        if df.empty:
            logger.warning("index_no_data", symbol=ts_code)
            continue

        logger.info("index_fetched", symbol=ts_code, rows=len(df))

        # 写入 market_bars_daily
        args = []
        for _, row in df.iterrows():
            td = datetime.strptime(str(row["trade_date"]), "%Y%m%d").date()
            args.append((
                row["ts_code"], "CN", td,
                row.get("open"), row.get("high"), row.get("low"), row.get("close"),
                int(row.get("vol", 0) or 0),  # tushare index_daily vol 单位是手
                row.get("amount"),  # 万元
                None, None,  # turnover, adj_factor
            ))

        await db_execute_many(
            """
            INSERT INTO market_bars_daily
                (symbol, market, trade_date, open, high, low, close, volume, amount, turnover, adj_factor)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
            ON CONFLICT (symbol, trade_date) DO UPDATE SET
                open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low,
                close=EXCLUDED.close, volume=EXCLUDED.volume, amount=EXCLUDED.amount
            """,
            args,
        )
        logger.info("index_saved", symbol=ts_code, rows=len(args))
        time.sleep(RATE_LIMIT_SLEEP)


# ============================================================
# 2. 北向资金
# ============================================================

async def pull_northbound(pro, start: str, end: str) -> None:
    """拉取沪深港通北向资金数据。"""
    logger.info("pulling_northbound", start=start, end=end)

    df = await asyncio.to_thread(
        query_with_retry, pro, "moneyflow_hsgt",
        start_date=start.replace("-", ""), end_date=end.replace("-", ""),
    )

    if df.empty:
        logger.warning("northbound_no_data")
        return

    logger.info("northbound_fetched", rows=len(df))

    args = []
    for _, row in df.iterrows():
        td = datetime.strptime(str(row["trade_date"]), "%Y%m%d").date()
        sh_net = row.get("north_money")  # 沪股通（百万元）
        sz_net = row.get("south_money")  # 深股通（百万元）
        # Tushare moneyflow_hsgt: north_money=沪股通净流入, south_money=深股通净流入
        # 字段映射不太一致，需查看 API
        # 实际字段: hgt=沪股通净流入, sgt=深股通净流入
        hgt = row.get("hgt")  # 沪股通（百万元）
        sgt = row.get("sgt")  # 深股通（百万元）

        sh_val = float(hgt) * 100 if hgt and pd.notna(hgt) else None  # 百万→万元
        sz_val = float(sgt) * 100 if sgt and pd.notna(sgt) else None
        total = None
        if sh_val is not None and sz_val is not None:
            total = sh_val + sz_val

        args.append((td, sh_val, sz_val, total, None, None))

    await db_execute_many(
        """
        INSERT INTO northbound_daily
            (trade_date, sh_net_buy, sz_net_buy, total_net_buy, sh_cumulative, sz_cumulative)
        VALUES ($1,$2,$3,$4,$5,$6)
        ON CONFLICT (trade_date) DO UPDATE SET
            sh_net_buy=EXCLUDED.sh_net_buy, sz_net_buy=EXCLUDED.sz_net_buy,
            total_net_buy=EXCLUDED.total_net_buy
        """,
        args,
    )
    logger.info("northbound_saved", rows=len(args))


# ============================================================
# 3. 市场情绪指标（涨跌停 / 涨跌家数）
# ============================================================

async def pull_market_sentiment(pro, start: str, end: str) -> None:
    """拉取每日涨跌停家数和市场宽度指标。"""
    logger.info("pulling_sentiment", start=start, end=end)

    # 获取交易日列表
    cal = await db_query(
        "SELECT trade_date FROM trade_calendar WHERE trade_date BETWEEN $1 AND $2 ORDER BY trade_date",
        datetime.strptime(start, "%Y-%m-%d").date(),
        datetime.strptime(end, "%Y-%m-%d").date(),
    )
    trade_dates = [r["trade_date"] for r in cal]
    logger.info("trade_dates_to_process", count=len(trade_dates))

    # 批量拉取涨跌停数据（用 limit_list 接口，比 stk_limit 更直接）
    # 同时从 market_bars_daily 计算涨跌比和新高新低
    batch_args = []
    for i, td in enumerate(trade_dates):
        td_str = td.strftime("%Y%m%d")
        try:
            # 从 Tushare limit_list 拉取涨跌停个股列表
            limit_up = 0
            limit_down = 0
            try:
                df_limit = await asyncio.to_thread(
                    query_with_retry, pro, "limit_list",
                    trade_date=td_str,
                    limit_type="U",
                )
                limit_up = len(df_limit) if not df_limit.empty else 0
                time.sleep(RATE_LIMIT_SLEEP)

                df_limit_d = await asyncio.to_thread(
                    query_with_retry, pro, "limit_list",
                    trade_date=td_str,
                    limit_type="D",
                )
                limit_down = len(df_limit_d) if not df_limit_d.empty else 0
                time.sleep(RATE_LIMIT_SLEEP)
            except Exception:
                # limit_list 不可用时，从行情数据近似计算
                # 涨幅 >= 9.5% 视为涨停, 跌幅 <= -9.5% 视为跌停
                row = await db_query_one(
                    """
                    SELECT
                        COUNT(*) FILTER (WHERE (close/NULLIF(open,0)-1) >= 0.095) AS up_cnt,
                        COUNT(*) FILTER (WHERE (close/NULLIF(open,0)-1) <= -0.095) AS dn_cnt
                    FROM market_bars_daily
                    WHERE trade_date = $1 AND market = 'CN'
                      AND symbol NOT LIKE '%.SH' OR symbol NOT LIKE '68%'
                    """,
                    td,
                )
                if row:
                    limit_up = row["up_cnt"] or 0
                    limit_down = row["dn_cnt"] or 0

            # 涨跌比
            up_down = await db_query_val(
                """
                SELECT CASE WHEN COUNT(*) FILTER (WHERE close < open) > 0
                    THEN COUNT(*) FILTER (WHERE close >= open)::float /
                         COUNT(*) FILTER (WHERE close < open)
                    ELSE 99.0 END
                FROM market_bars_daily
                WHERE trade_date = $1 AND market = 'CN'
                """,
                td,
            )
            up_down_ratio = float(up_down) if up_down else None

            # 新高新低暂不逐日计算（太慢），留空后续批量补
            batch_args.append((
                td, limit_up, limit_down, up_down_ratio,
                None, None,  # new_high_count, new_low_count
                None, None, None, None,  # margin, vix, fear_greed
            ))

        except Exception as e:
            logger.warning("sentiment_date_error", trade_date=str(td), error=str(e))
            continue

        if (i + 1) % 20 == 0:
            logger.info("sentiment_progress", done=i + 1, total=len(trade_dates))

    if batch_args:
        await db_execute_many(
            """
            INSERT INTO market_sentiment_daily
                (trade_date, limit_up_count, limit_down_count, up_down_ratio,
                 new_high_count, new_low_count,
                 margin_balance, margin_delta_5d, vix_cn, fear_greed)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
            ON CONFLICT (trade_date) DO UPDATE SET
                limit_up_count=EXCLUDED.limit_up_count,
                limit_down_count=EXCLUDED.limit_down_count,
                up_down_ratio=EXCLUDED.up_down_ratio,
                new_high_count=EXCLUDED.new_high_count,
                new_low_count=EXCLUDED.new_low_count
            """,
            batch_args,
        )
    logger.info("sentiment_saved", rows=len(batch_args))


# ============================================================
# 4. 修复 P6 迁移的 symbol 格式
# ============================================================

async def fix_symbol_format() -> None:
    """给 P6 迁移的无后缀 symbol 添加 .SH/.SZ 后缀。

    规则:
      6xxxxx → 6xxxxx.SH (上交所)
      000xxx, 001xxx, 002xxx, 003xxx, 300xxx → xxxxx.SZ (深交所)
      688xxx → 688xxx.SH (科创板)
    """
    logger.info("fix_symbols_start")

    # 查找没有后缀的 symbol
    count = await db_query_val(
        "SELECT COUNT(DISTINCT symbol) FROM market_bars_daily WHERE symbol NOT LIKE '%.%'"
    )
    if not count or count == 0:
        logger.info("fix_symbols_skip", reason="all symbols already have suffix")
        return

    logger.info("fix_symbols_found", count=count)

    # 批量更新: SH (6开头, 688开头)
    result_sh = await db_execute(
        """
        UPDATE market_bars_daily
        SET symbol = symbol || '.SH'
        WHERE symbol NOT LIKE '%.%'
          AND (symbol LIKE '6%')
        """
    )
    logger.info("fix_sh_done", result=result_sh)

    # SZ (0开头, 3开头)
    result_sz = await db_execute(
        """
        UPDATE market_bars_daily
        SET symbol = symbol || '.SZ'
        WHERE symbol NOT LIKE '%.%'
          AND (symbol LIKE '0%' OR symbol LIKE '3%')
        """
    )
    logger.info("fix_sz_done", result=result_sz)

    # 同步修复 features_daily
    for suffix, pattern in [(".SH", "6%"), (".SZ", "0%"), (".SZ", "3%")]:
        await db_execute(
            f"""
            UPDATE features_daily
            SET symbol = symbol || '{suffix}'
            WHERE symbol NOT LIKE '%.%' AND symbol LIKE '{pattern}'
            """
        )

    # 同步修复 fundamentals_daily
    for suffix, pattern in [(".SH", "6%"), (".SZ", "0%"), (".SZ", "3%")]:
        await db_execute(
            f"""
            UPDATE fundamentals_daily
            SET symbol = symbol || '{suffix}'
            WHERE symbol NOT LIKE '%.%' AND symbol LIKE '{pattern}'
            """
        )

    # 同步修复 industry_classify
    for suffix, pattern in [(".SH", "6%"), (".SZ", "0%"), (".SZ", "3%")]:
        await db_execute(
            f"""
            UPDATE industry_classify
            SET symbol = symbol || '{suffix}'
            WHERE symbol NOT LIKE '%.%' AND symbol LIKE '{pattern}'
            """
        )

    remaining = await db_query_val(
        "SELECT COUNT(DISTINCT symbol) FROM market_bars_daily WHERE symbol NOT LIKE '%.%'"
    )
    logger.info("fix_symbols_done", remaining_without_suffix=remaining)


# ============================================================
# 主入口
# ============================================================

async def main(args: argparse.Namespace) -> None:
    from dotenv import load_dotenv
    load_dotenv()

    await init_pool()
    pro = get_pro()

    start = args.start
    end = args.end

    try:
        if args.fix_symbols or args.all:
            await fix_symbol_format()

        if args.index or args.all:
            await pull_index_daily(pro, start, end)

        if args.northbound or args.all:
            await pull_northbound(pro, start, end)

        if args.sentiment or args.all:
            await pull_market_sentiment(pro, start, end)

    finally:
        await close_pool()

    logger.info("pull_data_done")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="P10-AlphaRadar 数据拉取")
    parser.add_argument("--all", action="store_true", help="拉取全部数据")
    parser.add_argument("--index", action="store_true", help="只拉指数日线")
    parser.add_argument("--northbound", action="store_true", help="只拉北向资金")
    parser.add_argument("--sentiment", action="store_true", help="只拉市场情绪")
    parser.add_argument("--fix-symbols", action="store_true", help="只修复 symbol 格式")
    parser.add_argument("--start", default="2021-03-01", help="起始日期 (default: 2021-03-01)")
    parser.add_argument("--end", default=date.today().strftime("%Y-%m-%d"), help="结束日期 (default: today)")
    parsed = parser.parse_args()

    if not any([parsed.all, parsed.index, parsed.northbound, parsed.sentiment, parsed.fix_symbols]):
        parsed.all = True

    asyncio.run(main(parsed))
