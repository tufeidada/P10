#!/usr/bin/env python3
"""
P6+ DuckDB → P10 PostgreSQL 数据迁移脚本

从 P6+/P4 的 DuckDB (agu.duckdb) 迁移历史数据到 P10 的 PostgreSQL。

用法:
    python scripts/migrate_from_p6.py \
        --source /path/to/agu.duckdb \
        --target postgresql://radar:pass@localhost:5432/alpharadar \
        --tables market_bars_daily,features_daily,fundamentals_daily,trade_calendar,industry_classify \
        --batch-size 100000 \
        --verify

迁移表:
    market_bars_daily   (~544万行)  A股日线OHLCV
    features_daily      (~544万行)  70+特征列 → P10核心列 + extra JSONB
    fundamentals_daily  (~545万行)  PE/PB/PS/换手率/市值
    trade_calendar      (~8800行)   交易日历
    industry_classify   (~5200行)   申万行业分类
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import duckdb
import psycopg2
import psycopg2.extras

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("migrate")

# ============================================================
# 迁移表定义: P6 DuckDB → P10 PostgreSQL 字段映射
# ============================================================

# P10 features_daily 的核心列（直接映射）
# P6 列名带 f_ 前缀，P10 列名不带
FEATURES_CORE_MAPPING: dict[str, str] = {
    # P6 column → P10 column
    "f_rsi_14": "rsi_14",
    "f_macd_dif": "macd_dif",
    "f_macd_dea": "macd_dea",
    "f_macd_hist": "macd_hist",
    "f_atr_14": "atr_14",
    "f_hv_20": "hv_20",
    "f_boll_width": "boll_width",
    "f_vol_ratio_5d": "vol_ratio_5d",
    "f_turnover_rank_20d": "turnover_rank_20d",
    "f_ret_1d": "ret_1d",
    "f_ret_5d": "ret_5d",
    "f_ret_20d": "ret_20d",
    "f_ma5_dev": "ma5_slope",  # 近似映射：偏离度 → 斜率
}

# P6 features 中不在 P10 核心列的字段，统一放入 extra JSONB
FEATURES_SKIP_COLS = {"symbol", "trade_date", "feature_version", "created_at"}


def get_source_conn(source_path: str) -> duckdb.DuckDBPyConnection:
    """只读连接 P6+ DuckDB。"""
    if not Path(source_path).exists():
        log.error("DuckDB 文件不存在: %s", source_path)
        sys.exit(1)
    return duckdb.connect(source_path, read_only=True)


def get_target_conn(target_dsn: str):
    """连接 P10 PostgreSQL。"""
    conn = psycopg2.connect(target_dsn)
    conn.autocommit = False
    return conn


# ============================================================
# 各表迁移函数
# ============================================================

def migrate_trade_calendar(src, tgt, batch_size: int) -> dict:
    """迁移交易日历。"""
    log.info(">>> 开始迁移 trade_calendar")
    rows = src.sql("SELECT trade_date FROM trade_calendar ORDER BY trade_date").fetchall()
    total = len(rows)
    log.info("源表行数: %s", f"{total:,}")

    cur = tgt.cursor()
    cur.execute("TRUNCATE trade_calendar")

    for i in range(0, total, batch_size):
        batch = rows[i:i + batch_size]
        psycopg2.extras.execute_values(
            cur,
            "INSERT INTO trade_calendar (trade_date) VALUES %s ON CONFLICT DO NOTHING",
            [(r[0],) for r in batch],
        )
    tgt.commit()

    cur.execute("SELECT COUNT(*) FROM trade_calendar")
    target_count = cur.fetchone()[0]
    cur.close()
    log.info("目标表行数: %s", f"{target_count:,}")
    return {"source": total, "target": target_count}


def migrate_industry_classify(src, tgt, batch_size: int) -> dict:
    """迁移行业分类。"""
    log.info(">>> 开始迁移 industry_classify")
    rows = src.sql("SELECT symbol, sw1_code, sw1_name, sw2_code, sw2_name FROM industry_classify").fetchall()
    total = len(rows)
    log.info("源表行数: %s", f"{total:,}")

    cur = tgt.cursor()
    cur.execute("TRUNCATE industry_classify")

    psycopg2.extras.execute_values(
        cur,
        """INSERT INTO industry_classify (symbol, sw1_code, sw1_name, sw2_code, sw2_name)
           VALUES %s ON CONFLICT (symbol) DO UPDATE SET
             sw1_code = EXCLUDED.sw1_code, sw1_name = EXCLUDED.sw1_name,
             sw2_code = EXCLUDED.sw2_code, sw2_name = EXCLUDED.sw2_name""",
        rows,
    )
    tgt.commit()

    cur.execute("SELECT COUNT(*) FROM industry_classify")
    target_count = cur.fetchone()[0]
    cur.close()
    log.info("目标表行数: %s", f"{target_count:,}")
    return {"source": total, "target": target_count}


def migrate_market_bars_daily(src, tgt, batch_size: int) -> dict:
    """迁移日线行情（最大表，约544万行）。"""
    log.info(">>> 开始迁移 market_bars_daily")

    total = src.sql("SELECT COUNT(*) FROM market_bars_daily").fetchone()[0]
    date_range = src.sql(
        "SELECT MIN(trade_date), MAX(trade_date) FROM market_bars_daily"
    ).fetchone()
    log.info("源表: %s 行, 日期 %s ~ %s", f"{total:,}", date_range[0], date_range[1])

    cur = tgt.cursor()
    migrated = 0
    start_time = time.time()

    # 按日期分批读取，避免一次性加载全部数据到内存
    all_dates = src.sql(
        "SELECT DISTINCT trade_date FROM market_bars_daily ORDER BY trade_date"
    ).fetchall()
    date_batches = [all_dates[i:i + 30] for i in range(0, len(all_dates), 30)]

    for date_batch in date_batches:
        date_list = [d[0] for d in date_batch]
        date_str = ",".join(f"'{d}'" for d in date_list)

        rows = src.sql(f"""
            SELECT symbol, trade_date, open, high, low, close, volume, amount
            FROM market_bars_daily
            WHERE trade_date IN ({date_str})
        """).fetchall()

        if not rows:
            continue

        # 添加 market='CN' (P6+只有A股)
        values = [(r[0], "CN", r[1], r[2], r[3], r[4], r[5], r[6], r[7]) for r in rows]

        psycopg2.extras.execute_values(
            cur,
            """INSERT INTO market_bars_daily
               (symbol, market, trade_date, open, high, low, close, volume, amount)
               VALUES %s
               ON CONFLICT (symbol, trade_date) DO NOTHING""",
            values,
            page_size=10000,
        )
        tgt.commit()

        migrated += len(rows)
        elapsed = time.time() - start_time
        rate = migrated / elapsed if elapsed > 0 else 0
        log.info(
            "进度: %s/%s (%.1f%%) | %.0f rows/s",
            f"{migrated:,}", f"{total:,}", migrated / total * 100, rate,
        )

    cur.execute("SELECT COUNT(*) FROM market_bars_daily")
    target_count = cur.fetchone()[0]
    cur.close()
    log.info("迁移完成: 源 %s → 目标 %s", f"{total:,}", f"{target_count:,}")
    return {"source": total, "target": target_count}


def migrate_fundamentals_daily(src, tgt, batch_size: int) -> dict:
    """迁移基本面日频数据（约545万行）。"""
    log.info(">>> 开始迁移 fundamentals_daily")

    total = src.sql("SELECT COUNT(*) FROM fundamentals_daily").fetchone()[0]
    log.info("源表行数: %s", f"{total:,}")

    cur = tgt.cursor()
    migrated = 0
    start_time = time.time()

    all_dates = src.sql(
        "SELECT DISTINCT trade_date FROM fundamentals_daily ORDER BY trade_date"
    ).fetchall()
    date_batches = [all_dates[i:i + 30] for i in range(0, len(all_dates), 30)]

    for date_batch in date_batches:
        date_list = [d[0] for d in date_batch]
        date_str = ",".join(f"'{d}'" for d in date_list)

        rows = src.sql(f"""
            SELECT symbol, trade_date, pe_ttm, pb, ps_ttm,
                   total_mv_yi, circ_mv_yi, turnover_rate_f
            FROM fundamentals_daily
            WHERE trade_date IN ({date_str})
        """).fetchall()

        if not rows:
            continue

        # P6 市值单位是"亿"，P10 单位是"万元"，需转换: 亿 * 10000 = 万
        values = []
        for r in rows:
            total_mv = float(r[5]) * 10000 if r[5] is not None else None
            circ_mv = float(r[6]) * 10000 if r[6] is not None else None
            values.append((r[0], r[1], r[2], r[3], r[4], total_mv, circ_mv, r[7]))

        psycopg2.extras.execute_values(
            cur,
            """INSERT INTO fundamentals_daily
               (symbol, trade_date, pe_ttm, pb, ps_ttm, total_mv, circ_mv, turnover_rate_f)
               VALUES %s
               ON CONFLICT (symbol, trade_date) DO NOTHING""",
            values,
            page_size=10000,
        )
        tgt.commit()

        migrated += len(rows)
        elapsed = time.time() - start_time
        rate = migrated / elapsed if elapsed > 0 else 0
        if migrated % (batch_size * 2) < len(rows):
            log.info(
                "进度: %s/%s (%.1f%%) | %.0f rows/s",
                f"{migrated:,}", f"{total:,}", migrated / total * 100, rate,
            )

    cur.execute("SELECT COUNT(*) FROM fundamentals_daily")
    target_count = cur.fetchone()[0]
    cur.close()
    log.info("迁移完成: 源 %s → 目标 %s", f"{total:,}", f"{target_count:,}")
    return {"source": total, "target": target_count}


def migrate_features_daily(src, tgt, batch_size: int) -> dict:
    """迁移特征数据（约544万行）。

    P6 有 67 个特征列（f_ 前缀），P10 核心列约 25 个，其余放 extra JSONB。
    """
    log.info(">>> 开始迁移 features_daily")

    total = src.sql("SELECT COUNT(*) FROM features_daily").fetchone()[0]
    log.info("源表行数: %s", f"{total:,}")

    # 获取 P6 所有列名
    p6_columns = [
        col[0] for col in src.sql("DESCRIBE features_daily").fetchall()
    ]
    log.info("P6 特征列数: %d", len(p6_columns))

    cur = tgt.cursor()
    migrated = 0
    start_time = time.time()

    all_dates = src.sql(
        "SELECT DISTINCT trade_date FROM features_daily ORDER BY trade_date"
    ).fetchall()
    date_batches = [all_dates[i:i + 20] for i in range(0, len(all_dates), 20)]

    for date_batch in date_batches:
        date_list = [d[0] for d in date_batch]
        date_str = ",".join(f"'{d}'" for d in date_list)

        df = src.sql(f"""
            SELECT * FROM features_daily
            WHERE trade_date IN ({date_str})
        """).fetchdf()

        if df.empty:
            continue

        values = []
        for _, row in df.iterrows():
            # 核心列映射
            core = {
                "symbol": row["symbol"],
                "trade_date": row["trade_date"],
            }
            for p6_col, p10_col in FEATURES_CORE_MAPPING.items():
                if p6_col in row.index:
                    val = row[p6_col]
                    core[p10_col] = None if _is_nan(val) else val

            # 其余特征列放 extra JSONB
            extra = {}
            for col in p6_columns:
                if col in FEATURES_SKIP_COLS:
                    continue
                if col in FEATURES_CORE_MAPPING:
                    continue
                val = row.get(col)
                if val is not None and not _is_nan(val):
                    if isinstance(val, (bool,)):
                        extra[col] = val
                    elif isinstance(val, (Decimal, float)):
                        extra[col] = round(float(val), 6)
                    else:
                        extra[col] = val

            values.append((
                core["symbol"],
                core["trade_date"],
                core.get("rsi_14"),
                core.get("macd_dif"),
                core.get("macd_dea"),
                core.get("macd_hist"),
                core.get("atr_14"),
                core.get("hv_20"),
                core.get("boll_width"),
                core.get("vol_ratio_5d"),
                core.get("turnover_rank_20d"),
                core.get("ret_1d"),
                core.get("ret_5d"),
                core.get("ret_20d"),
                core.get("ma5_slope"),
                json.dumps(extra, ensure_ascii=False, default=str) if extra else None,
            ))

        psycopg2.extras.execute_values(
            cur,
            """INSERT INTO features_daily
               (symbol, trade_date, rsi_14, macd_dif, macd_dea, macd_hist,
                atr_14, hv_20, boll_width, vol_ratio_5d, turnover_rank_20d,
                ret_1d, ret_5d, ret_20d, ma5_slope, extra)
               VALUES %s
               ON CONFLICT (symbol, trade_date) DO NOTHING""",
            values,
            page_size=5000,
        )
        tgt.commit()

        migrated += len(df)
        elapsed = time.time() - start_time
        rate = migrated / elapsed if elapsed > 0 else 0
        if migrated % (batch_size * 2) < len(df):
            log.info(
                "进度: %s/%s (%.1f%%) | %.0f rows/s",
                f"{migrated:,}", f"{total:,}", migrated / total * 100, rate,
            )

    cur.execute("SELECT COUNT(*) FROM features_daily")
    target_count = cur.fetchone()[0]
    cur.close()
    log.info("迁移完成: 源 %s → 目标 %s", f"{total:,}", f"{target_count:,}")
    return {"source": total, "target": target_count}


def _is_nan(val) -> bool:
    """检查值是否为 NaN。"""
    try:
        import math
        return val is None or (isinstance(val, float) and math.isnan(val))
    except (TypeError, ValueError):
        return False


# ============================================================
# 验证
# ============================================================

def verify_migration(src, tgt, table: str) -> bool:
    """验证迁移结果：对比行数和日期范围。"""
    log.info("验证 %s ...", table)

    src_count = src.sql(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    cur = tgt.cursor()
    # P10 表名可能与 P6 不同，但这里迁移的表名一致
    cur.execute(f"SELECT COUNT(*) FROM {table}")
    tgt_count = cur.fetchone()[0]

    match = tgt_count >= src_count * 0.99  # 允许 1% 误差（ON CONFLICT DO NOTHING）
    status = "PASS" if match else "FAIL"
    log.info(
        "[%s] %s: 源 %s → 目标 %s (%.1f%%)",
        status, table, f"{src_count:,}", f"{tgt_count:,}",
        tgt_count / src_count * 100 if src_count > 0 else 0,
    )

    # 检查日期范围
    date_col = "trade_date"
    try:
        src_range = src.sql(
            f"SELECT MIN({date_col}), MAX({date_col}) FROM {table}"
        ).fetchone()
        cur.execute(f"SELECT MIN({date_col}), MAX({date_col}) FROM {table}")
        tgt_range = cur.fetchone()
        log.info(
            "  日期范围: 源 %s~%s | 目标 %s~%s",
            src_range[0], src_range[1], tgt_range[0], tgt_range[1],
        )
    except Exception:
        pass

    cur.close()
    return match


# ============================================================
# 主入口
# ============================================================

ALL_TABLES = [
    "trade_calendar",
    "industry_classify",
    "market_bars_daily",
    "fundamentals_daily",
    "features_daily",
]

MIGRATE_FUNCS = {
    "trade_calendar": migrate_trade_calendar,
    "industry_classify": migrate_industry_classify,
    "market_bars_daily": migrate_market_bars_daily,
    "fundamentals_daily": migrate_fundamentals_daily,
    "features_daily": migrate_features_daily,
}


def main():
    parser = argparse.ArgumentParser(
        description="P6+ DuckDB → P10 PostgreSQL 数据迁移",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--source", required=True,
        help="P6+ DuckDB 文件路径 (例: /path/to/agu.duckdb)",
    )
    parser.add_argument(
        "--target", required=True,
        help="P10 PostgreSQL 连接字符串 (例: postgresql://radar:pass@localhost:5432/alpharadar)",
    )
    parser.add_argument(
        "--tables", default=",".join(ALL_TABLES),
        help=f"要迁移的表，逗号分隔 (默认: {','.join(ALL_TABLES)})",
    )
    parser.add_argument(
        "--batch-size", type=int, default=100000,
        help="每批处理行数 (默认: 100000)",
    )
    parser.add_argument(
        "--verify", action="store_true",
        help="迁移后验证行数和日期范围",
    )
    args = parser.parse_args()

    tables = [t.strip() for t in args.tables.split(",")]
    for t in tables:
        if t not in MIGRATE_FUNCS:
            log.error("不支持的表: %s (可选: %s)", t, ", ".join(ALL_TABLES))
            sys.exit(1)

    log.info("=" * 60)
    log.info("P6+ → P10 数据迁移")
    log.info("源: %s", args.source)
    log.info("目标: %s", args.target.split("@")[-1])  # 隐藏密码
    log.info("表: %s", ", ".join(tables))
    log.info("批次大小: %s", f"{args.batch_size:,}")
    log.info("=" * 60)

    src = get_source_conn(args.source)
    tgt = get_target_conn(args.target)

    results = {}
    total_start = time.time()

    for table in tables:
        table_start = time.time()
        try:
            result = MIGRATE_FUNCS[table](src, tgt, args.batch_size)
            result["elapsed_s"] = round(time.time() - table_start, 1)
            result["status"] = "OK"
        except Exception as e:
            log.error("迁移 %s 失败: %s", table, e, exc_info=True)
            result = {"status": "FAILED", "error": str(e)}
            tgt.rollback()
        results[table] = result

    # 验证
    if args.verify:
        log.info("\n" + "=" * 60)
        log.info("迁移验证")
        log.info("=" * 60)
        for table in tables:
            if results[table].get("status") == "OK":
                verify_migration(src, tgt, table)

    # 汇总报告
    total_elapsed = time.time() - total_start
    log.info("\n" + "=" * 60)
    log.info("迁移报告")
    log.info("=" * 60)
    for table, result in results.items():
        if result["status"] == "OK":
            log.info(
                "  %-25s %10s → %10s  (%.1fs)",
                table,
                f"{result['source']:,}",
                f"{result['target']:,}",
                result["elapsed_s"],
            )
        else:
            log.info("  %-25s FAILED: %s", table, result.get("error", "unknown"))
    log.info("总耗时: %.1f 秒", total_elapsed)

    src.close()
    tgt.close()


if __name__ == "__main__":
    main()
