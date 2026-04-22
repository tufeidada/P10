"""
scheduler_job_log / scheduler_heartbeat 的读写接口。
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Any

import structlog

from db.connection import db_execute, db_query, db_query_val

logger = structlog.get_logger(__name__)


async def log_job(
    job_name: str,
    status: str,
    duration_ms: int | None = None,
    error_message: str | None = None,
) -> None:
    """写入 job 执行结果到 scheduler_job_log。

    Args:
        job_name: job 标识名。
        status: success / failed / skipped / invariant。
        duration_ms: 执行耗时（毫秒）。
        error_message: 失败时的错误信息。
    """
    await db_execute(
        """
        INSERT INTO scheduler_job_log (job_name, status, duration_ms, error_message)
        VALUES ($1, $2, $3, $4)
        """,
        job_name, status, duration_ms, error_message,
    )


async def write_heartbeat(jobs_count: int) -> None:
    """写入心跳记录到 scheduler_heartbeat。

    Args:
        jobs_count: 当前注册的 job 数量。
    """
    try:
        import psutil
        mem_mb = round(psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024, 1)
    except Exception:
        mem_mb = None

    await db_execute(
        """
        INSERT INTO scheduler_heartbeat (jobs_count, pid, memory_mb)
        VALUES ($1, $2, $3)
        """,
        jobs_count, os.getpid(), mem_mb,
    )
    logger.debug("heartbeat_written", jobs=jobs_count, pid=os.getpid(), mem_mb=mem_mb)


async def get_job_last_success(job_name: str) -> datetime | None:
    """返回指定 job 最近一次成功的时间。

    Args:
        job_name: job 标识名。

    Returns:
        最近成功时间，从未成功时返回 None。
    """
    row = await db_query_val(
        """
        SELECT MAX(trigger_time) FROM scheduler_job_log
        WHERE job_name = $1 AND status = 'success'
        """,
        job_name,
    )
    return row


async def get_recent_job_stats(hours: int = 24) -> list[dict]:
    """返回最近 N 小时内各 job 的执行统计。

    Args:
        hours: 统计窗口（小时数）。

    Returns:
        包含 job_name / total / success / failed / last_run / last_status 的列表。
    """
    rows = await db_query(
        """
        SELECT
            job_name,
            COUNT(*)                                        AS total,
            COUNT(*) FILTER (WHERE status = 'success')     AS success,
            COUNT(*) FILTER (WHERE status = 'failed')      AS failed,
            COUNT(*) FILTER (WHERE status = 'skipped')     AS skipped,
            MAX(trigger_time)                               AS last_run,
            (ARRAY_AGG(status ORDER BY trigger_time DESC))[1] AS last_status
        FROM scheduler_job_log
        WHERE trigger_time > NOW() - ($1 || ' hours')::INTERVAL
        GROUP BY job_name
        ORDER BY job_name
        """,
        str(hours),
    )
    return [dict(r) for r in rows]
