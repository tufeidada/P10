"""
数据新鲜度检查 — 对 data_source_expectations 里的每个数据源执行 MAX(date) 检查。

用法：
    python scripts/data_freshness_check.py           # 检查所有数据源
    python scripts/data_freshness_check.py --dry-run # 只打印结果，不写 DB 不推送
    python scripts/data_freshness_check.py --source tushare.cn_cpi_yoy  # 只检查指定源

结果写入 data_freshness_log 表。
status = critical → 立即推送 Telegram 🚨
status = warn     → 写入日志（由 daily digest 汇总推送，M8 实现）
status = ok/info  → 仅写日志
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, datetime
from pathlib import Path

import structlog
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.connection import close_pool, db_execute, db_query, db_query_val, init_pool

logger = structlog.get_logger(__name__)


async def count_trading_days_between(start_date: date, end_date: date) -> int:
    """返回 (start_date, end_date] 之间的交易日数（查询 trade_calendar 表）。

    Args:
        start_date: 区间起点（不含）。
        end_date: 区间终点（含）。

    Returns:
        交易日数；若 trade_calendar 为空或查询失败则返回日历天数作为保守值。
    """
    if end_date <= start_date:
        return 0
    try:
        count = await db_query_val(
            "SELECT COUNT(*) FROM trade_calendar WHERE trade_date > $1 AND trade_date <= $2",
            start_date,
            end_date,
        )
        return int(count) if count is not None else (end_date - start_date).days
    except Exception as e:
        logger.warning("trading_day_count_error", error=str(e))
        return (end_date - start_date).days


async def _get_max_date(table_name: str, date_column: str, filter_clause: str | None) -> date | None:
    """查询指定表的最新数据日期。

    Args:
        table_name: 表名。
        date_column: 日期列名。
        filter_clause: 可选 WHERE 子句（不含 WHERE 关键字）。

    Returns:
        最新日期，表为空时返回 None。
    """
    where = f"WHERE {filter_clause}" if filter_clause else ""
    sql = f"SELECT MAX({date_column}) FROM {table_name} {where}"
    try:
        result = await db_query_val(sql)
        return result
    except Exception as e:
        logger.error("freshness_check_query_error", table=table_name, error=str(e))
        return None


async def check_single_source(
    source: dict,
    today: date,
    dry_run: bool = False,
) -> dict:
    """检查单个数据源的新鲜度。

    Args:
        source: data_source_expectations 的一行。
        today: 检查日期（通常是今天）。
        dry_run: True 时不写 DB 不推送。

    Returns:
        包含 source_name / max_date / lag_days / status / message 的结果字典。
    """
    source_name = source["source_name"]
    max_date = await _get_max_date(
        source["table_name"],
        source["date_column"],
        source["filter_clause"],
    )

    lag_basis = source.get("lag_basis", "trading_days")

    if max_date is None:
        lag_days = None
        status = "critical"
        message = f"[{source_name}] max_date=None（从未有数据或查询失败）"
    else:
        if lag_basis == "trading_days":
            lag_days = await count_trading_days_between(max_date, today)
            lag_unit = "交易日"
        else:
            lag_days = (today - max_date).days
            lag_unit = "自然日"

        if lag_days > source["max_lag_days"]:
            status = source["severity"]
            message = (
                f"[{source_name}] 数据停更 {lag_days} {lag_unit}，"
                f"超出阈值 {source['max_lag_days']} {lag_unit}（{source['frequency']}）"
                f"，最新 = {max_date}"
            )
        else:
            status = "ok"
            message = f"[{source_name}] 正常，最新 = {max_date}，lag = {lag_days}{lag_unit}"

    result = {
        "source_name": source_name,
        "max_date": max_date,
        "lag_days": lag_days,
        "status": status,
        "message": message,
    }

    if not dry_run:
        await db_execute(
            """
            INSERT INTO data_freshness_log
                (source_name, max_date, lag_days, status, message)
            VALUES ($1, $2, $3, $4, $5)
            """,
            source_name, max_date, lag_days, status, message,
        )

    return result


async def run_all_checks(
    source_filter: str | None = None,
    dry_run: bool = False,
) -> list[dict]:
    """对所有（或指定）数据源执行新鲜度检查。

    Args:
        source_filter: 若指定，只检查该 source_name。
        dry_run: True 时不写 DB 不推送。

    Returns:
        所有检查结果列表。
    """
    today = date.today()

    if source_filter:
        sources = await db_query(
            "SELECT * FROM data_source_expectations WHERE source_name = $1",
            source_filter,
        )
    else:
        sources = await db_query(
            "SELECT * FROM data_source_expectations ORDER BY severity DESC, source_name"
        )

    if not sources:
        logger.warning("freshness_check_no_sources", filter=source_filter)
        return []

    results = []
    for src in sources:
        r = await check_single_source(dict(src), today, dry_run)
        results.append(r)
        log_fn = logger.error if r["status"] == "critical" else (
            logger.warning if r["status"] == "warn" else logger.info
        )
        log_fn("freshness_check_result", **{k: str(v) if v else v for k, v in r.items()})

    return results


async def push_critical_alerts(results: list[dict], dry_run: bool = False) -> None:
    """对 critical 状态的数据源立即推送 Telegram 告警。

    Args:
        results: run_all_checks 的返回值。
        dry_run: True 时只打印，不实际推送。
    """
    critical = [r for r in results if r["status"] == "critical"]
    if not critical:
        return

    lines = ["🚨 <b>DATA FRESHNESS CRITICAL</b>\n"]
    for r in critical:
        lines.append(f"• {r['message']}")
    msg = "\n".join(lines)

    if dry_run:
        print(f"[dry-run] WOULD PUSH:\n{msg}")
        return

    try:
        from bot.telegram_bot import TelegramPusher
        pusher = TelegramPusher()
        ok = await pusher.send(msg)
        if ok:
            logger.info("freshness_critical_alert_sent", count=len(critical))
        else:
            logger.warning("freshness_critical_alert_not_sent")
    except Exception as e:
        logger.error("freshness_critical_alert_error", error=str(e))


def print_summary(results: list[dict]) -> None:
    """打印检查摘要到 stdout。

    Args:
        results: 检查结果列表。
    """
    by_status: dict[str, list] = {}
    for r in results:
        by_status.setdefault(r["status"], []).append(r)

    print(f"\n{'='*60}")
    print(f"Data Freshness Check — {date.today()}")
    print(f"{'='*60}")
    total = len(results)
    ok = len(by_status.get("ok", []))
    warn = len(by_status.get("warn", []))
    critical = len(by_status.get("critical", []))
    info = len(by_status.get("info", []))
    print(f"Total: {total}  |  ok: {ok}  warn: {warn}  critical: {critical}  info: {info}")
    print()

    for status, emoji in [("critical", "🚨"), ("warn", "⚠️"), ("info", "ℹ️"), ("ok", "✅")]:
        items = by_status.get(status, [])
        if items:
            print(f"{emoji} {status.upper()} ({len(items)}):")
            for r in items:
                lag = f"lag={r['lag_days']}d" if r["lag_days"] is not None else "lag=N/A"
                print(f"   {r['source_name']:35} {lag:12} max={r['max_date']}")
    print()


async def _main(source_filter: str | None, dry_run: bool) -> None:
    """主流程。

    Args:
        source_filter: 只检查指定源（None = 全部）。
        dry_run: True 时不写 DB 不推送。
    """
    await init_pool()
    try:
        results = await run_all_checks(source_filter, dry_run)
        print_summary(results)
        await push_critical_alerts(results, dry_run)
    finally:
        await close_pool()


def main() -> None:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="数据新鲜度检查")
    parser.add_argument("--source", help="只检查指定 source_name")
    parser.add_argument("--dry-run", action="store_true", help="不写 DB，不推送")
    args = parser.parse_args()
    asyncio.run(_main(args.source, args.dry_run))


if __name__ == "__main__":
    main()
