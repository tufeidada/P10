"""Database connection pool management using asyncpg."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Optional

import asyncpg
import structlog

logger = structlog.get_logger(__name__)

_pool: Optional[asyncpg.Pool] = None


async def create_pool() -> asyncpg.Pool:
    """Create and return the global asyncpg connection pool.

    Args:
        None — reads DATABASE_URL from environment.

    Returns:
        The initialized asyncpg Pool.
    """
    global _pool
    if _pool is not None:
        return _pool

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL environment variable is not set")

    logger.info("creating_db_pool", dsn=dsn.split("@")[-1])  # hide credentials
    _pool = await asyncpg.create_pool(
        dsn=dsn,
        min_size=2,
        max_size=10,
        command_timeout=60,
        server_settings={"search_path": "backtest,public"},
    )
    logger.info("db_pool_ready")
    return _pool


async def get_pool() -> asyncpg.Pool:
    """Return the existing pool, creating it if necessary."""
    if _pool is None:
        return await create_pool()
    return _pool


async def close_pool() -> None:
    """Close the global connection pool."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("db_pool_closed")


@asynccontextmanager
async def acquire() -> AsyncGenerator[asyncpg.Connection, None]:
    """Context manager that acquires a connection from the pool.

    Usage:
        async with acquire() as conn:
            await conn.fetch(...)
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        yield conn


class Database:
    """Thin wrapper around asyncpg pool with helper methods.

    All methods set search_path to backtest schema automatically via the pool.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    @classmethod
    async def connect(cls) -> "Database":
        """Create Database instance using the global pool."""
        pool = await get_pool()
        return cls(pool)

    async def fetch(self, query: str, *args: Any) -> list[asyncpg.Record]:
        """Execute a SELECT query and return all rows.

        Args:
            query: Parameterized SQL query.
            *args: Query parameters.

        Returns:
            List of asyncpg Record objects.
        """
        async with self._pool.acquire() as conn:
            try:
                return await conn.fetch(query, *args)
            except Exception as e:
                logger.error("db_fetch_error", query=query[:100], error=str(e))
                raise

    async def fetch_df(self, query: str, *args: Any):
        """Execute SELECT and return pandas DataFrame.

        Args:
            query: Parameterized SQL query.
            *args: Query parameters.

        Returns:
            pandas DataFrame with query results.
        """
        import pandas as pd

        rows = await self.fetch(query, *args)
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame([dict(r) for r in rows])

    async def fetch_one(self, query: str, *args: Any) -> Optional[asyncpg.Record]:
        """Execute SELECT and return single row or None.

        Args:
            query: Parameterized SQL query.
            *args: Query parameters.

        Returns:
            Single asyncpg Record or None.
        """
        async with self._pool.acquire() as conn:
            try:
                return await conn.fetchrow(query, *args)
            except Exception as e:
                logger.error("db_fetchrow_error", query=query[:100], error=str(e))
                raise

    async def execute(self, query: str, *args: Any) -> str:
        """Execute a non-SELECT statement.

        Args:
            query: Parameterized SQL query.
            *args: Query parameters.

        Returns:
            Status string from asyncpg.
        """
        async with self._pool.acquire() as conn:
            try:
                return await conn.execute(query, *args)
            except Exception as e:
                logger.error("db_execute_error", query=query[:100], error=str(e))
                raise

    async def executemany(self, query: str, args_list: list[tuple]) -> None:
        """Execute a statement for multiple parameter sets.

        Args:
            query: Parameterized SQL query.
            args_list: List of parameter tuples.
        """
        async with self._pool.acquire() as conn:
            try:
                await conn.executemany(query, args_list)
            except Exception as e:
                logger.error("db_executemany_error", query=query[:100], error=str(e))
                raise

    async def copy_records(
        self,
        table_name: str,
        records: list[tuple],
        columns: list[str],
    ) -> None:
        """Bulk insert using COPY protocol (fastest for large datasets).

        Args:
            table_name: Fully qualified table name (schema.table).
            records: List of tuples matching column order.
            columns: Column names in the same order as records.
        """
        if not records:
            return
        async with self._pool.acquire() as conn:
            try:
                await conn.copy_records_to_table(
                    table_name,
                    records=records,
                    columns=columns,
                    schema_name="backtest",
                )
                logger.info(
                    "copy_records_done",
                    table=table_name,
                    count=len(records),
                )
            except Exception as e:
                logger.error(
                    "db_copy_error",
                    table=table_name,
                    count=len(records),
                    error=str(e),
                )
                raise
