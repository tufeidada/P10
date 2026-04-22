"""StockTwits API 客户端 — 美股社交情绪。

API: https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json
免费 API，无 token，返回最近 30 条消息及情绪标注。
Rate limit: ~200 calls/hour → sleep 0.5s between calls.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog

from db.connection import db_execute, db_query_val

logger = structlog.get_logger(__name__)

_RATE_LIMIT_SLEEP: float = 0.5
_REQUEST_TIMEOUT: float = 15.0


class StockTwitsClient:
    """StockTwits 社交情绪客户端。

    通过 StockTwits 公开 API 拉取美股社交媒体情绪数据，
    解析看多/看空比例并写入 social_sentiment 表。
    """

    BASE_URL = "https://api.stocktwits.com/api/2"

    async def get_symbol_stream(self, symbol: str) -> dict[str, Any]:
        """拉取指定 symbol 最新 30 条消息流。

        Args:
            symbol: 股票代码，如 'AAPL' 或 'NASDAQ:AAPL'。
                    会自动去除 'NASDAQ:' 等交易所前缀。

        Returns:
            StockTwits API 原始响应 dict，包含：
            - messages: 消息对象列表
            - symbol: {id, symbol, title, ...}
            网络错误时返回 {}（优雅降级）。

        Raises:
            httpx.HTTPStatusError: HTTP 4xx/5xx 错误时抛出。
        """
        # 去除交易所前缀（如 NASDAQ:AAPL → AAPL）
        clean_symbol = symbol.split(":")[-1] if ":" in symbol else symbol

        log = logger.bind(symbol=clean_symbol, module="stocktwits_client")
        log.info("get_symbol_stream_start")

        url = f"{self.BASE_URL}/streams/symbol/{clean_symbol}.json"

        import os
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or None
        transport = httpx.AsyncHTTPTransport(proxy=proxy) if proxy else None
        mounts = {"https://": transport, "http://": transport} if transport else None

        # Browser-like headers required by StockTwits
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://stocktwits.com/",
        }

        try:
            client_kwargs: dict[str, Any] = {"timeout": _REQUEST_TIMEOUT, "headers": headers}
            if mounts:
                client_kwargs["mounts"] = mounts
            async with httpx.AsyncClient(**client_kwargs) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data: dict[str, Any] = resp.json()
                log.info(
                    "get_symbol_stream_ok",
                    message_count=len(data.get("messages", [])),
                )
                return data
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status in (403, 429):
                # IP blocked or rate-limited — return empty gracefully
                log.warning("get_symbol_stream_blocked", status_code=status, symbol=symbol)
                return {}
            log.error(
                "get_symbol_stream_http_error",
                status_code=status,
                error=str(e),
            )
            return {}
        except Exception as e:
            log.error("get_symbol_stream_network_error", error=str(e))
            return {}

    def parse_sentiment(self, response: dict[str, Any]) -> dict[str, Any]:
        """从 API 响应中提取情绪指标。

        Args:
            response: get_symbol_stream() 返回的原始响应 dict。

        Returns:
            情绪指标 dict：
            - message_count (int): 流中的消息总数
            - bullish_count (int): 看多消息数
            - bearish_count (int): 看空消息数
            - bullish_pct (float): 看多占比 0-100
            - bearish_pct (float): 看空占比 0-100
            - sentiment_score (float): -1 到 +1，(bull-bear)/(bull+bear)
            - message_delta (float): 占位符，save_to_db 步骤填充
            - oldest_time (str | None): 最早消息的 ISO 时间
            - newest_time (str | None): 最新消息的 ISO 时间
        """
        messages: list[dict[str, Any]] = response.get("messages", [])
        message_count = len(messages)

        bullish_count = 0
        bearish_count = 0
        oldest_time: str | None = None
        newest_time: str | None = None

        for msg in messages:
            entities = msg.get("entities", {}) or {}
            sentiment = entities.get("sentiment")
            if sentiment is not None:
                basic = sentiment.get("basic", "")
                if basic == "Bullish":
                    bullish_count += 1
                elif basic == "Bearish":
                    bearish_count += 1

            # 收集时间范围
            created_at = msg.get("created_at")
            if created_at:
                if oldest_time is None or created_at < oldest_time:
                    oldest_time = created_at
                if newest_time is None or created_at > newest_time:
                    newest_time = created_at

        # 计算比例
        total_with_sentiment = bullish_count + bearish_count
        if total_with_sentiment > 0:
            bullish_pct = bullish_count / total_with_sentiment * 100.0
            bearish_pct = bearish_count / total_with_sentiment * 100.0
            sentiment_score = (bullish_count - bearish_count) / total_with_sentiment
        else:
            bullish_pct = 0.0
            bearish_pct = 0.0
            sentiment_score = 0.0

        return {
            "message_count": message_count,
            "bullish_count": bullish_count,
            "bearish_count": bearish_count,
            "bullish_pct": bullish_pct,
            "bearish_pct": bearish_pct,
            "sentiment_score": sentiment_score,
            "message_delta": 0.0,  # 在 save_to_db 中填充
            "oldest_time": oldest_time,
            "newest_time": newest_time,
        }

    async def save_to_db(
        self,
        symbol: str,
        parsed: dict[str, Any],
        snapshot_time: datetime | None = None,
    ) -> bool:
        """将情绪数据 upsert 到 social_sentiment 表。

        Args:
            symbol: 股票代码，如 'AAPL'。
            parsed: parse_sentiment() 返回的情绪指标 dict。
            snapshot_time: 快照时间，默认为当前 UTC 时间。

        Returns:
            成功返回 True，异常返回 False。
        """
        if snapshot_time is None:
            snapshot_time = datetime.now(timezone.utc)

        log = logger.bind(symbol=symbol, module="stocktwits_client")

        # 计算 message_delta：与历史最近一次保存的 message_count 对比
        message_delta = 0.0
        try:
            prev_count = await db_query_val(
                """
                SELECT message_count
                FROM social_sentiment
                WHERE symbol = $1 AND source = 'stocktwits'
                ORDER BY snapshot_time DESC
                LIMIT 1
                """,
                symbol,
            )
            if prev_count is not None and int(prev_count) > 0:
                current_count = parsed.get("message_count", 0) or 0
                message_delta = (
                    (current_count - int(prev_count)) / int(prev_count) * 100.0
                )
        except Exception as e:
            log.warning("save_to_db_prev_count_error", error=str(e))

        upsert_sql = """
            INSERT INTO social_sentiment (
                symbol, market, snapshot_time, source,
                bullish_pct, message_count, message_delta,
                sentiment_score, raw_data
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (symbol, snapshot_time, source) DO UPDATE SET
                bullish_pct     = EXCLUDED.bullish_pct,
                message_count   = EXCLUDED.message_count,
                message_delta   = EXCLUDED.message_delta,
                sentiment_score = EXCLUDED.sentiment_score,
                raw_data        = EXCLUDED.raw_data
        """

        raw_data = {
            "bullish_count": parsed.get("bullish_count"),
            "bearish_count": parsed.get("bearish_count"),
            "bearish_pct": parsed.get("bearish_pct"),
            "oldest_time": parsed.get("oldest_time"),
            "newest_time": parsed.get("newest_time"),
        }

        try:
            await db_execute(
                upsert_sql,
                symbol,                                       # $1
                "US",                                         # $2 market
                snapshot_time,                                # $3
                "stocktwits",                                 # $4 source
                parsed.get("bullish_pct"),                    # $5
                parsed.get("message_count"),                  # $6
                message_delta,                                # $7
                parsed.get("sentiment_score"),                # $8
                json.dumps(raw_data),                         # $9
            )
            log.info(
                "save_to_db_ok",
                symbol=symbol,
                message_count=parsed.get("message_count"),
                bullish_pct=parsed.get("bullish_pct"),
                sentiment_score=parsed.get("sentiment_score"),
            )
            return True
        except Exception as e:
            log.error("save_to_db_error", symbol=symbol, error=str(e))
            return False

    async def fetch_and_save(self, symbol: str) -> bool:
        """拉取并保存情绪数据（一步完成）。

        Args:
            symbol: 股票代码，如 'AAPL'。

        Returns:
            成功返回 True，任一步骤失败返回 False。
        """
        log = logger.bind(symbol=symbol, module="stocktwits_client")
        log.info("fetch_and_save_start")

        try:
            response = await self.get_symbol_stream(symbol)
        except Exception as e:
            log.error("fetch_and_save_stream_error", error=str(e))
            return False

        if not response:
            log.warning("fetch_and_save_empty_response", symbol=symbol)
            return False

        parsed = self.parse_sentiment(response)
        return await self.save_to_db(symbol, parsed)

    async def batch_fetch_save(self, symbols: list[str]) -> dict[str, bool]:
        """批量拉取多个 symbol 的情绪数据，调用间隔 0.5s。

        Args:
            symbols: 股票代码列表，如 ['AAPL', 'NVDA', 'MSFT']。

        Returns:
            以 symbol 为 key、成功状态为 value 的 dict。
        """
        log = logger.bind(module="stocktwits_client", total=len(symbols))
        log.info("batch_fetch_save_start")

        results: dict[str, bool] = {}
        for i, symbol in enumerate(symbols):
            results[symbol] = await self.fetch_and_save(symbol)
            if i < len(symbols) - 1:
                await asyncio.sleep(_RATE_LIMIT_SLEEP)

        success_count = sum(1 for v in results.values() if v)
        log.info(
            "batch_fetch_save_done",
            total=len(symbols),
            success=success_count,
            failed=len(symbols) - success_count,
        )
        return results
