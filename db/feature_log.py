"""
feature_update_log / tushare_credit_log 的读写接口。

feature_update_log: 记录每日每只股票的 feature 计算结果（成功/失败）。
tushare_credit_log: 记录 Tushare Pro 积分消耗（由数据拉取管道填写）。
"""

from __future__ import annotations

from datetime import date
from typing import Sequence

import structlog

from db.connection import db_execute, db_query

logger = structlog.get_logger(__name__)

_CONSECUTIVE_FAIL_THRESHOLD = 3  # 连续失败多少天触发 critical 告警
_TUSHARE_DAILY_BUDGET = 500      # 单日 Tushare 积分告警阈值（可调整）


# ============================================================
# feature_update_log
# ============================================================

async def log_feature_results(
    run_date: date,
    market: str,
    results: dict[str, bool],
    errors: dict[str, str] | None = None,
) -> None:
    """批量写入本次 feature 计算结果。

    Args:
        run_date: 计算目标日期。
        market: 市场代码。
        results: {symbol: success} 映射。
        errors: {symbol: error_message} 可选错误信息映射。
    """
    if not results:
        return
    errors = errors or {}
    for symbol, success in results.items():
        await db_execute(
            """
            INSERT INTO feature_update_log (run_date, market, symbol, success, error_message)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT DO NOTHING
            """,
            run_date, market, symbol, success, errors.get(symbol),
        )
    logger.info("feature_log_written", run_date=str(run_date), market=market,
                total=len(results), success=sum(results.values()))


async def get_degraded_symbols(
    market: str,
    as_of_date: date | None = None,
    threshold: int = _CONSECUTIVE_FAIL_THRESHOLD,
) -> list[str]:
    """返回连续 threshold 天 feature 更新失败的股票列表。

    供 composite 分析链路用于跳过降级股票，供日报标记"数据异常"。

    Args:
        market: 市场代码。
        as_of_date: 截止日期，默认今天。
        threshold: 连续失败天数阈值，默认 3。

    Returns:
        降级股票代码列表。
    """
    if as_of_date is None:
        as_of_date = date.today()

    rows = await db_query(
        """
        WITH recent AS (
            SELECT symbol, run_date, success,
                   ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY run_date DESC) AS rn
            FROM feature_update_log
            WHERE market = $1 AND run_date <= $2
        ),
        consecutive_fails AS (
            SELECT symbol, COUNT(*) AS fail_count
            FROM recent
            WHERE rn <= $3 AND success = FALSE
            GROUP BY symbol
            HAVING COUNT(*) = $3
        )
        SELECT symbol FROM consecutive_fails
        ORDER BY symbol
        """,
        market, as_of_date, threshold,
    )
    symbols = [r["symbol"] for r in rows]
    if symbols:
        logger.warning("degraded_symbols_detected", market=market,
                       count=len(symbols), symbols=symbols)
    return symbols


async def get_feature_run_summary(run_date: date, market: str) -> dict:
    """返回指定日期的 feature 更新摘要。

    Args:
        run_date: 查询日期。
        market: 市场代码。

    Returns:
        包含 total/success/failed/degraded 的摘要字典。
    """
    rows = await db_query(
        """
        SELECT success, COUNT(*) AS cnt
        FROM feature_update_log
        WHERE run_date = $1 AND market = $2
        GROUP BY success
        """,
        run_date, market,
    )
    success = 0
    failed = 0
    for r in rows:
        if r["success"]:
            success = r["cnt"]
        else:
            failed = r["cnt"]
    degraded = await get_degraded_symbols(market, run_date)
    return {
        "run_date": str(run_date),
        "market": market,
        "total": success + failed,
        "success": success,
        "failed": failed,
        "degraded_count": len(degraded),
        "degraded_symbols": degraded,
    }


# ============================================================
# tushare_credit_log
# ============================================================

async def log_tushare_credits(
    log_date: date,
    query_type: str,
    source: str,
    points_used: int,
    symbols_count: int | None = None,
) -> None:
    """记录 Tushare Pro 积分消耗。

    由数据拉取管道（data/pipeline/）调用，M4 创建接口，M6 填充实际调用。

    Args:
        log_date: 日期。
        query_type: 查询类型（如 'daily_bars', 'fundamentals'）。
        source: 调用来源（如 'task_data_pipeline_pull'）。
        points_used: 消耗积分数。
        symbols_count: 涉及股票数量。
    """
    await db_execute(
        """
        INSERT INTO tushare_credit_log (log_date, query_type, source, points_used, symbols_count)
        VALUES ($1, $2, $3, $4, $5)
        """,
        log_date, query_type, source, points_used, symbols_count,
    )
    logger.debug("tushare_credit_logged", date=str(log_date),
                 query_type=query_type, points=points_used)


async def get_daily_credit_total(log_date: date) -> int:
    """查询指定日期的 Tushare 积分消耗总量。

    Args:
        log_date: 查询日期。

    Returns:
        总积分消耗数。
    """
    from db.connection import db_query_val
    total = await db_query_val(
        "SELECT COALESCE(SUM(points_used), 0) FROM tushare_credit_log WHERE log_date = $1",
        log_date,
    )
    return int(total or 0)


async def check_credit_budget(log_date: date, budget: int = _TUSHARE_DAILY_BUDGET) -> bool:
    """检查今日积分消耗是否超出预算。

    Args:
        log_date: 查询日期。
        budget: 每日积分预算，默认 500。

    Returns:
        True 表示超出预算，False 表示正常。
    """
    total = await get_daily_credit_total(log_date)
    if total > budget:
        logger.warning("tushare_credit_budget_exceeded",
                       date=str(log_date), total=total, budget=budget)
        return True
    return False
