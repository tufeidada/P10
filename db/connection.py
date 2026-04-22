"""
PostgreSQL 连接池管理 (asyncpg)

使用方式:
    from db.connection import get_pool, db_query, db_execute, db_copy

    # 初始化（应用启动时调用一次）
    await init_pool()

    # 查询
    rows = await db_query("SELECT * FROM trade_calendar WHERE trade_date > $1", date(2024, 1, 1))

    # 写入
    await db_execute("INSERT INTO stock_universe (symbol, market, ...) VALUES ($1, $2, ...)", ...)

    # 批量写入（COPY 协议，性能最优）
    await db_copy("market_bars_daily", columns, records)

    # 关闭（应用退出时调用）
    await close_pool()
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
import structlog

logger = structlog.get_logger(__name__)

_pool: asyncpg.Pool | None = None


async def init_pool(
    dsn: str | None = None,
    min_size: int = 5,
    max_size: int = 20,
) -> asyncpg.Pool:
    """初始化连接池，应用启动时调用一次。

    Args:
        dsn: PostgreSQL 连接字符串，默认从 DATABASE_URL 环境变量读取。
        min_size: 最小连接数。
        max_size: 最大连接数。

    Returns:
        asyncpg.Pool 连接池实例。
    """
    global _pool
    if _pool is not None:
        return _pool

    dsn = dsn or os.environ["DATABASE_URL"]
    _pool = await asyncpg.create_pool(
        dsn,
        min_size=min_size,
        max_size=max_size,
        command_timeout=60,
        server_settings={"timezone": "Asia/Shanghai"},
    )
    logger.info("db_pool_initialized", min_size=min_size, max_size=max_size)
    return _pool


def get_pool() -> asyncpg.Pool:
    """获取已初始化的连接池，未初始化时抛异常。"""
    if _pool is None:
        raise RuntimeError("Database pool not initialized. Call init_pool() first.")
    return _pool


@asynccontextmanager
async def acquire():
    """从连接池获取一个连接的上下文管理器。"""
    pool = get_pool()
    async with pool.acquire() as conn:
        yield conn


async def db_query(sql: str, *args: Any) -> list[asyncpg.Record]:
    """执行查询，返回所有行。

    Args:
        sql: 参数化 SQL（使用 $1, $2 占位符）。
        *args: SQL 参数。

    Returns:
        asyncpg.Record 列表。
    """
    async with acquire() as conn:
        return await conn.fetch(sql, *args)


async def db_query_one(sql: str, *args: Any) -> asyncpg.Record | None:
    """执行查询，返回单行或 None。"""
    async with acquire() as conn:
        return await conn.fetchrow(sql, *args)


async def db_query_val(sql: str, *args: Any) -> Any:
    """执行查询，返回单个值。"""
    async with acquire() as conn:
        return await conn.fetchval(sql, *args)


async def db_execute(sql: str, *args: Any) -> str:
    """执行写操作（INSERT/UPDATE/DELETE），返回状态字符串。"""
    async with acquire() as conn:
        return await conn.execute(sql, *args)


async def db_execute_many(sql: str, args_list: list[tuple]) -> None:
    """批量执行写操作。"""
    async with acquire() as conn:
        await conn.executemany(sql, args_list)


async def db_copy(
    table: str,
    columns: list[str],
    records: list[tuple],
) -> str:
    """使用 COPY 协议批量写入（高性能，适用于大批量数据导入）。

    Args:
        table: 目标表名。
        columns: 列名列表。
        records: 数据元组列表。

    Returns:
        COPY 操作状态字符串。
    """
    async with acquire() as conn:
        result = await conn.copy_records_to_table(
            table,
            columns=columns,
            records=records,
        )
        logger.info("db_copy_done", table=table, result=result)
        return result


@asynccontextmanager
async def transaction():
    """事务上下文管理器。

    Usage:
        async with transaction() as conn:
            await conn.execute("INSERT ...")
            await conn.execute("UPDATE ...")
    """
    async with acquire() as conn:
        async with conn.transaction():
            yield conn


async def close_pool() -> None:
    """关闭连接池，应用退出时调用。"""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("db_pool_closed")
