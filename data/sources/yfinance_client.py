"""
yfinance 封装 — 美股数据采集客户端。

基于 yfinance 的异步包装器，通过 asyncio.to_thread 桥接同步 API，
供 USDataPuller 等上层管道调用。

Usage:
    client = YFinanceClient()
    df = await client.get_daily_bars("AAPL", "2024-01-01", "2024-12-31")
    info = await client.get_info("NVDA")
    financials = await client.get_financials("MSFT")
"""

from __future__ import annotations

import asyncio
from typing import Any

import pandas as pd
import structlog
import yfinance as yf

logger = structlog.get_logger(__name__)

# yfinance 调用间隔（秒），避免被限流
_RATE_LIMIT_SLEEP: float = 0.5


class YFinanceClient:
    """yfinance 封装 — 美股数据采集客户端。

    所有方法均为 async，通过 asyncio.to_thread 桥接同步 API。
    无状态，可在并发场景中安全复用同一实例。
    """

    # ------------------------------------------------------------------
    # 日线 OHLCV
    # ------------------------------------------------------------------

    async def get_daily_bars(
        self,
        symbol: str,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame:
        """拉取日线 OHLCV（复权）。

        Args:
            symbol: 美股 ticker，如 'AAPL'。
            start_date: 开始日期，格式 'YYYY-MM-DD'。
            end_date: 结束日期，格式 'YYYY-MM-DD'（exclusive，yfinance 约定）。

        Returns:
            DataFrame，列：Open, High, Low, Close, Volume；
            Index：DatetimeIndex（日期）。
            数据已前复权（auto_adjust=True）。
            拉取失败返回空 DataFrame。
        """
        log = logger.bind(symbol=symbol, start=start_date, end=end_date, module="yfinance_client")
        log.info("get_daily_bars_start")

        def _download() -> pd.DataFrame:
            df = yf.download(
                symbol,
                start=start_date,
                end=end_date,
                auto_adjust=True,
                progress=False,
            )
            # yfinance >= 1.x returns MultiIndex columns for single ticker
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            return df

        try:
            df = await asyncio.to_thread(_download)
            if df is None or df.empty:
                log.warning("get_daily_bars_empty")
                return pd.DataFrame()
            log.info("get_daily_bars_ok", rows=len(df))
            return df
        except Exception as e:
            log.error("get_daily_bars_error", error=str(e))
            return pd.DataFrame()

    # ------------------------------------------------------------------
    # 分钟线
    # ------------------------------------------------------------------

    async def get_intraday_bars(
        self,
        symbol: str,
        interval: str = "15m",
        period: str = "7d",
    ) -> pd.DataFrame:
        """拉取分钟线（yfinance 只保留最近 7-60 天）。

        Args:
            symbol: 美股 ticker，如 'AAPL'。
            interval: K 线周期，如 '15m', '5m', '1h'。
            period: 回溯时间窗口，如 '7d', '30d', '60d'。

        Returns:
            DataFrame，列：Open, High, Low, Close, Volume；
            Index：DatetimeIndex（带时区）。
            拉取失败返回空 DataFrame。
        """
        log = logger.bind(symbol=symbol, interval=interval, period=period, module="yfinance_client")
        log.info("get_intraday_bars_start")

        def _fetch() -> pd.DataFrame:
            ticker = yf.Ticker(symbol)
            df = ticker.history(period=period, interval=interval)
            return df

        try:
            df = await asyncio.to_thread(_fetch)
            if df is None or df.empty:
                log.warning("get_intraday_bars_empty")
                return pd.DataFrame()
            log.info("get_intraday_bars_ok", rows=len(df))
            return df
        except Exception as e:
            log.error("get_intraday_bars_error", error=str(e))
            return pd.DataFrame()

    # ------------------------------------------------------------------
    # 财务三表
    # ------------------------------------------------------------------

    async def get_financials(self, symbol: str) -> dict[str, pd.DataFrame]:
        """拉取财务三表（季报）。

        Args:
            symbol: 美股 ticker，如 'MSFT'。

        Returns:
            dict，包含：
            - 'income_stmt': 利润表 DataFrame（最近 4 季度）
            - 'balance_sheet': 资产负债表 DataFrame
            - 'cashflow': 现金流量表 DataFrame
            列为日期（newest first），行为财务科目。
            任一表拉取失败返回空 DataFrame。
        """
        log = logger.bind(symbol=symbol, module="yfinance_client")
        log.info("get_financials_start")

        def _fetch() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
            ticker = yf.Ticker(symbol)
            income = ticker.quarterly_income_stmt
            balance = ticker.quarterly_balance_sheet
            cashflow = ticker.quarterly_cashflow
            return income, balance, cashflow

        try:
            income_stmt, balance_sheet, cashflow = await asyncio.to_thread(_fetch)
            result: dict[str, pd.DataFrame] = {
                "income_stmt":   income_stmt   if income_stmt   is not None else pd.DataFrame(),
                "balance_sheet": balance_sheet if balance_sheet is not None else pd.DataFrame(),
                "cashflow":      cashflow      if cashflow      is not None else pd.DataFrame(),
            }
            log.info(
                "get_financials_ok",
                income_cols=len(result["income_stmt"].columns) if not result["income_stmt"].empty else 0,
                balance_cols=len(result["balance_sheet"].columns) if not result["balance_sheet"].empty else 0,
                cashflow_cols=len(result["cashflow"].columns) if not result["cashflow"].empty else 0,
            )
            return result
        except Exception as e:
            log.error("get_financials_error", error=str(e))
            return {
                "income_stmt":   pd.DataFrame(),
                "balance_sheet": pd.DataFrame(),
                "cashflow":      pd.DataFrame(),
            }

    # ------------------------------------------------------------------
    # 股票基本信息
    # ------------------------------------------------------------------

    async def get_info(self, symbol: str) -> dict[str, Any]:
        """拉取股票基本信息（PE/PB/市值/行业等）。

        Args:
            symbol: 美股 ticker，如 'NVDA'。

        Returns:
            标准化 dict，字段：
            - pe_ttm:      滚动12个月市盈率
            - pb:          市净率
            - ps_ttm:      滚动12个月市销率
            - market_cap:  总市值（原始值，USD）
            - sector:      行业大类
            - industry:    细分行业
            - long_name:   公司全称
            - raw:         完整原始 info dict（供调用方按需取用）
            拉取失败返回空 dict。
        """
        log = logger.bind(symbol=symbol, module="yfinance_client")
        log.info("get_info_start")

        def _fetch() -> dict[str, Any]:
            ticker = yf.Ticker(symbol)
            return ticker.info

        try:
            raw_info = await asyncio.to_thread(_fetch)
            if not raw_info:
                log.warning("get_info_empty")
                return {}

            mapped: dict[str, Any] = {
                "pe_ttm":     raw_info.get("trailingPE"),
                "pb":         raw_info.get("priceToBook"),
                "ps_ttm":     raw_info.get("priceToSalesTrailing12Months"),
                "market_cap": raw_info.get("marketCap"),
                "sector":     raw_info.get("sector"),
                "industry":   raw_info.get("industry"),
                "long_name":  raw_info.get("longName"),
                "raw":        raw_info,
            }
            log.info("get_info_ok", sector=mapped.get("sector"))
            return mapped
        except Exception as e:
            log.error("get_info_error", error=str(e))
            return {}

    # ------------------------------------------------------------------
    # S&P 500 成分股
    # ------------------------------------------------------------------

    async def get_sp500_components(self) -> list[str]:
        """获取 S&P 500 成分股列表（来自 Wikipedia）。

        Returns:
            ticker symbol 字符串列表，格式已清理（如 'BRK.B' → 'BRK-B'，
            符合 yfinance 约定）。
            拉取失败返回空列表。
        """
        log = logger.bind(module="yfinance_client")
        log.info("get_sp500_components_start")

        def _fetch() -> list[str]:
            url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
            tables = pd.read_html(url)
            df = tables[0]
            # 列名可能是 'Symbol' 或 'Ticker symbol'
            col = "Symbol" if "Symbol" in df.columns else df.columns[0]
            symbols: list[str] = df[col].astype(str).str.strip().tolist()
            # Wikipedia 用 '.' 分隔（如 BRK.B），yfinance 要求 '-'（如 BRK-B）
            symbols = [s.replace(".", "-") for s in symbols if s]
            return symbols

        try:
            symbols = await asyncio.to_thread(_fetch)
            log.info("get_sp500_components_ok", count=len(symbols))
            return symbols
        except Exception as e:
            log.error("get_sp500_components_error", error=str(e))
            return []
