"""
Tushare Pro API 客户端 — 适配 P10-AlphaRadar 异步架构。

基于 P6 的 TushareClient，增加：
- async 包装器（通过 asyncio.to_thread 桥接同步 API）
- 写入 PostgreSQL 的便捷方法
- structlog 结构化日志

Usage:
    client = TushareClient()
    df = await client.fetch_daily_bars("600519.SH", "20240101", "20240131")
    saved = await client.save_daily_bars(df)
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import pandas as pd
import structlog
import tushare as ts

from db.connection import db_copy, db_execute_many, db_query_val

logger = structlog.get_logger(__name__)


class TushareClient:
    """Tushare Pro 异步客户端，封装常用数据接口。

    Token 从环境变量 TUSHARE_TOKEN 读取。
    """

    def __init__(self, token: str | None = None) -> None:
        self._token = token or os.environ.get("TUSHARE_TOKEN", "")
        if not self._token:
            raise ValueError("TUSHARE_TOKEN 未设置，请设置环境变量或传入 token 参数。")
        self._pro = ts.pro_api(self._token)

    # ================================================================
    # 底层同步查询（带重试）
    # ================================================================

    def _query_sync(
        self,
        api_name: str,
        retry_wait: list[int] | None = None,
        **kwargs: Any,
    ) -> pd.DataFrame:
        """调用 Tushare 接口，失败时自动重试。

        Args:
            api_name: Tushare 接口名（如 'daily', 'trade_cal'）。
            retry_wait: 每次重试前等待秒数列表，长度即最大重试次数。
            **kwargs: 传给 Tushare 接口的参数。

        Returns:
            查询结果 DataFrame。

        Raises:
            Exception: 重试用尽后抛出最后一个错误。
        """
        if retry_wait is None:
            retry_wait = [30, 60, 120]

        api_func = getattr(self._pro, api_name)
        last_error: Exception | None = None

        for attempt in range(len(retry_wait) + 1):
            try:
                result = api_func(**kwargs)
                if result is not None and not result.empty:
                    logger.info(
                        "tushare_query_ok",
                        api=api_name,
                        rows=len(result),
                        params=kwargs,
                    )
                return result if result is not None else pd.DataFrame()
            except Exception as e:
                last_error = e
                if attempt < len(retry_wait):
                    wait = retry_wait[attempt]
                    logger.warning(
                        "tushare_query_retry",
                        api=api_name,
                        attempt=attempt + 1,
                        max_attempts=len(retry_wait) + 1,
                        error=str(e),
                        wait_seconds=wait,
                    )
                    time.sleep(wait)

        raise last_error  # type: ignore[misc]

    # ================================================================
    # 异步数据获取方法
    # ================================================================

    async def fetch_daily_bars(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame:
        """获取个股/指数日线行情。

        Args:
            symbol: 证券代码，如 '600519.SH'。
            start_date: 开始日期，格式 'YYYYMMDD'。
            end_date: 结束日期，格式 'YYYYMMDD'。

        Returns:
            包含 ts_code, trade_date, open, high, low, close, vol, amount 的 DataFrame。
        """
        try:
            df = await asyncio.to_thread(
                self._query_sync,
                "daily",
                ts_code=symbol,
                start_date=start_date,
                end_date=end_date,
            )
            logger.info(
                "fetch_daily_bars_ok",
                symbol=symbol,
                start=start_date,
                end=end_date,
                rows=len(df),
            )
            return df
        except Exception as e:
            logger.error(
                "fetch_daily_bars_error",
                symbol=symbol,
                start=start_date,
                end=end_date,
                error=str(e),
            )
            raise

    async def fetch_index_daily(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame:
        """获取指数日线行情。

        Args:
            symbol: 指数代码，如 '000300.SH'。
            start_date: 开始日期，格式 'YYYYMMDD'。
            end_date: 结束日期，格式 'YYYYMMDD'。

        Returns:
            包含 ts_code, trade_date, open, high, low, close, vol, amount 的 DataFrame。
        """
        try:
            df = await asyncio.to_thread(
                self._query_sync,
                "index_daily",
                ts_code=symbol,
                start_date=start_date,
                end_date=end_date,
            )
            logger.info(
                "fetch_index_daily_ok",
                symbol=symbol,
                start=start_date,
                end=end_date,
                rows=len(df),
            )
            return df
        except Exception as e:
            logger.error(
                "fetch_index_daily_error",
                symbol=symbol,
                error=str(e),
            )
            raise

    async def fetch_daily_basic(
        self,
        symbol: str,
        trade_date: str,
    ) -> pd.DataFrame:
        """获取每日基本面指标（PE/PB/换手率等）。

        Args:
            symbol: 证券代码，如 '600519.SH'。
            trade_date: 交易日期，格式 'YYYYMMDD'。

        Returns:
            包含 ts_code, trade_date, turnover_rate, pe, pb, ps 等的 DataFrame。
        """
        try:
            df = await asyncio.to_thread(
                self._query_sync,
                "daily_basic",
                ts_code=symbol,
                trade_date=trade_date,
            )
            logger.info(
                "fetch_daily_basic_ok",
                symbol=symbol,
                trade_date=trade_date,
                rows=len(df),
            )
            return df
        except Exception as e:
            logger.error(
                "fetch_daily_basic_error",
                symbol=symbol,
                trade_date=trade_date,
                error=str(e),
            )
            raise

    async def fetch_moneyflow(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame:
        """获取个股资金流向。

        Args:
            symbol: 证券代码。
            start_date: 开始日期，格式 'YYYYMMDD'。
            end_date: 结束日期，格式 'YYYYMMDD'。

        Returns:
            资金流向 DataFrame。
        """
        try:
            df = await asyncio.to_thread(
                self._query_sync,
                "moneyflow",
                ts_code=symbol,
                start_date=start_date,
                end_date=end_date,
            )
            logger.info(
                "fetch_moneyflow_ok",
                symbol=symbol,
                rows=len(df),
            )
            return df
        except Exception as e:
            logger.error(
                "fetch_moneyflow_error",
                symbol=symbol,
                error=str(e),
            )
            raise

    async def fetch_adj_factor(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame:
        """获取复权因子。

        Args:
            symbol: 证券代码。
            start_date: 开始日期，格式 'YYYYMMDD'。
            end_date: 结束日期，格式 'YYYYMMDD'。

        Returns:
            复权因子 DataFrame。
        """
        try:
            df = await asyncio.to_thread(
                self._query_sync,
                "adj_factor",
                ts_code=symbol,
                start_date=start_date,
                end_date=end_date,
            )
            return df
        except Exception as e:
            logger.error(
                "fetch_adj_factor_error",
                symbol=symbol,
                error=str(e),
            )
            raise

    # ================================================================
    # 数据写入 PostgreSQL
    # ================================================================

    async def save_daily_bars(self, df: pd.DataFrame, market: str = "CN") -> int:
        """将日线数据写入 market_bars_daily 表。

        使用 COPY 协议批量写入，性能最优。重复数据通过 ON CONFLICT 跳过。

        Args:
            df: 包含 ts_code, trade_date, open, high, low, close, vol, amount 的 DataFrame。
            market: 市场标识，默认 'CN'。

        Returns:
            成功写入的行数。
        """
        if df is None or df.empty:
            logger.warning("save_daily_bars_skip", reason="empty_dataframe")
            return 0

        required_cols = {"ts_code", "trade_date", "open", "high", "low", "close", "vol"}
        missing = required_cols - set(df.columns)
        if missing:
            logger.error("save_daily_bars_missing_cols", missing=list(missing))
            return 0

        try:
            # 转换为参数列表
            args_list: list[tuple] = []
            for _, row in df.iterrows():
                trade_date_str = str(row["trade_date"])
                # Tushare 日期格式 YYYYMMDD → date 对象
                from datetime import date as date_type
                td = date_type(
                    int(trade_date_str[:4]),
                    int(trade_date_str[4:6]),
                    int(trade_date_str[6:8]),
                )
                args_list.append((
                    str(row["ts_code"]),
                    market,
                    td,
                    float(row["open"]),
                    float(row["high"]),
                    float(row["low"]),
                    float(row["close"]),
                    float(row["vol"]) if pd.notna(row.get("vol")) else 0.0,
                    float(row["amount"]) if pd.notna(row.get("amount")) else 0.0,
                ))

            sql = """
                INSERT INTO market_bars_daily
                    (symbol, market, trade_date, open, high, low, close, volume, amount)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                ON CONFLICT (symbol, trade_date) DO NOTHING
            """
            await db_execute_many(sql, args_list)

            logger.info(
                "save_daily_bars_ok",
                market=market,
                rows=len(args_list),
            )
            return len(args_list)

        except Exception as e:
            logger.error("save_daily_bars_error", error=str(e))
            raise

    async def save_index_daily(self, df: pd.DataFrame, market: str = "CN") -> int:
        """将指数日线数据写入 market_bars_daily 表。

        复用 save_daily_bars，指数数据结构一致。

        Args:
            df: 指数日线 DataFrame。
            market: 市场标识。

        Returns:
            成功写入的行数。
        """
        return await self.save_daily_bars(df, market=market)
