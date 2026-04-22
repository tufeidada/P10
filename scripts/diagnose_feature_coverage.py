"""
诊断 features_daily 覆盖情况，输出 CSV 报告到 reports/feature_coverage_YYYYMMDD.csv。

用法：
    python scripts/diagnose_feature_coverage.py           # 今天
    python scripts/diagnose_feature_coverage.py --date 2026-04-20
    python scripts/diagnose_feature_coverage.py --stdout  # 只打印，不写文件

输出三个部分（写入同一个 CSV，以空行分隔）：
    A: features_daily vs stock_universe 分布汇总
    B: 不在 universe 的孤儿 features 股票
    C: active universe 中 feature 不足 250 天或无数据的股票
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import sys
from datetime import date, datetime
from pathlib import Path

import structlog
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.connection import close_pool, db_query, init_pool

logger = structlog.get_logger(__name__)


async def run_diagnostics(as_of_date: date) -> dict:
    """执行三组诊断 SQL，返回结果字典。

    Args:
        as_of_date: 诊断截止日期。

    Returns:
        包含 section_a / section_b / section_c 的结果字典。
    """
    section_a = await db_query("""
        SELECT
            CASE
                WHEN u.symbol IS NOT NULL AND u.active = TRUE  THEN 'in_universe_active'
                WHEN u.symbol IS NOT NULL AND u.active = FALSE THEN 'in_universe_inactive'
                ELSE 'not_in_universe'
            END AS status,
            COUNT(DISTINCT f.symbol) AS symbol_count
        FROM features_daily f
        LEFT JOIN stock_universe u ON f.symbol = u.symbol
        GROUP BY status
        ORDER BY status
    """)

    section_b = await db_query("""
        SELECT
            f.symbol,
            MIN(f.trade_date)  AS first_date,
            MAX(f.trade_date)  AS last_date,
            COUNT(*)           AS feature_rows
        FROM features_daily f
        LEFT JOIN stock_universe u ON f.symbol = u.symbol
        WHERE u.symbol IS NULL
        GROUP BY f.symbol
        ORDER BY f.symbol
    """)

    section_c = await db_query("""
        SELECT
            u.symbol,
            u.market,
            u.priority,
            COUNT(f.trade_date)  AS feature_days,
            MAX(f.trade_date)    AS last_feature_date,
            ($1 - MAX(f.trade_date))::INT AS days_since_last
        FROM stock_universe u
        LEFT JOIN features_daily f ON u.symbol = f.symbol
        WHERE u.active = TRUE
        GROUP BY u.symbol, u.market, u.priority
        HAVING COUNT(f.trade_date) < 250 OR MAX(f.trade_date) IS NULL
        ORDER BY u.priority, u.market, u.symbol
    """, as_of_date)

    return {
        "section_a": [dict(r) for r in section_a],
        "section_b": [dict(r) for r in section_b],
        "section_c": [dict(r) for r in section_c],
    }


def _build_csv(results: dict, as_of_date: date) -> str:
    """将三组结果拼成 CSV 字符串。

    Args:
        results: run_diagnostics 的返回值。
        as_of_date: 诊断日期（写入标题行）。

    Returns:
        CSV 格式的完整字符串。
    """
    buf = io.StringIO()
    w = csv.writer(buf)

    w.writerow([f"# Feature Coverage Diagnostics — {as_of_date}"])
    w.writerow([])

    # Section A
    w.writerow(["## A: features_daily vs universe 分布"])
    w.writerow(["status", "symbol_count"])
    for row in results["section_a"]:
        w.writerow([row["status"], row["symbol_count"]])
    w.writerow([])

    # Section B
    w.writerow(["## B: 不在 universe 的孤儿 features"])
    if results["section_b"]:
        w.writerow(["symbol", "first_date", "last_date", "feature_rows"])
        for row in results["section_b"]:
            w.writerow([row["symbol"], row["first_date"], row["last_date"], row["feature_rows"]])
    else:
        w.writerow(["(empty — no orphan features)"])
    w.writerow([])

    # Section C
    w.writerow(["## C: active universe 中 feature 不足 250 天或无数据"])
    if results["section_c"]:
        w.writerow(["symbol", "market", "priority", "feature_days", "last_feature_date", "days_since_last"])
        for row in results["section_c"]:
            w.writerow([
                row["symbol"], row["market"], row["priority"],
                row["feature_days"], row["last_feature_date"], row["days_since_last"],
            ])
    else:
        w.writerow(["(empty — all active stocks have ≥250 feature days)"])

    return buf.getvalue()


async def _main(as_of_date: date, stdout_only: bool) -> None:
    """主流程：查询 + 输出。

    Args:
        as_of_date: 诊断截止日期。
        stdout_only: True 时只打印，不写文件。
    """
    await init_pool()
    try:
        results = await run_diagnostics(as_of_date)
        csv_content = _build_csv(results, as_of_date)

        if stdout_only:
            print(csv_content)
        else:
            reports_dir = Path(__file__).resolve().parent.parent / "reports"
            reports_dir.mkdir(parents=True, exist_ok=True)
            out_path = reports_dir / f"feature_coverage_{as_of_date.strftime('%Y%m%d')}.csv"
            out_path.write_text(csv_content, encoding="utf-8")
            print(f"报告已写入: {out_path}")

            # 顺便打印摘要
            a_map = {r["status"]: r["symbol_count"] for r in results["section_a"]}
            print(f"  in_universe_active  : {a_map.get('in_universe_active', 0)}")
            print(f"  in_universe_inactive: {a_map.get('in_universe_inactive', 0)}")
            print(f"  not_in_universe     : {a_map.get('not_in_universe', 0)}")
            print(f"  section_c (需关注)  : {len(results['section_c'])} 只")
    finally:
        await close_pool()


def main() -> None:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="诊断 features_daily 覆盖情况")
    parser.add_argument("--date", help="截止日期 YYYY-MM-DD（默认今天）")
    parser.add_argument("--stdout", action="store_true", help="只打印，不写文件")
    args = parser.parse_args()

    as_of = date.today()
    if args.date:
        as_of = datetime.strptime(args.date, "%Y-%m-%d").date()

    asyncio.run(_main(as_of, args.stdout))


if __name__ == "__main__":
    main()
