"""东方财富股吧客户端 — A 股社交情绪。

方案:
1. 先尝试 AkShare stock_comment_em (股票评论/情绪)
2. 若不可用，爬取东方财富股吧帖子数:
   https://guba.eastmoney.com/list,{6位代码}.html
   用 regex 提取帖子总数和今日帖子数
3. 写入 social_sentiment 表 (source='eastmoney')
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog

from db.connection import db_execute, db_query_val

logger = structlog.get_logger(__name__)

_RATE_LIMIT_SLEEP: float = 1.0
_REQUEST_TIMEOUT: float = 20.0
_SCRAPE_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": "https://guba.eastmoney.com/",
}


class EastMoneyClient:
    """东方财富股吧 A 股情绪客户端。

    多级降级策略：
    1. AkShare stock_comment_em — 综合评分/排名
    2. AkShare stock_hot_rank_em — 热度排名
    3. 直接爬取股吧页面 — 帖子总数/今日帖子数
    """

    def _strip_suffix(self, symbol: str) -> str:
        """去除 A 股后缀。

        Args:
            symbol: 如 '600519.SH' 或 '000001.SZ'。

        Returns:
            纯数字代码，如 '600519'。
        """
        return symbol.split(".")[0]

    async def get_stock_sentiment(self, symbol: str) -> dict[str, Any]:
        """获取 A 股股吧情绪数据（多级降级策略）。

        依次尝试：
        1. AkShare stock_comment_em — 综合评分
        2. AkShare stock_hot_rank_em — 热度排名
        3. 爬取东方财富股吧页面 — 帖子数量

        Args:
            symbol: A 股代码，如 '600519.SH'，支持带后缀或不带后缀。

        Returns:
            情绪数据 dict：
            - post_count (int | None): 帖子总数
            - post_count_today (int | None): 今日帖子数
            - sentiment_score (float | None): 综合情绪分（AkShare 提供时）
            - bullish_pct (float | None): 看多占比（AkShare 提供时）
            - source_method (str): 实际使用的数据获取方式
        """
        bare_code = self._strip_suffix(symbol)
        log = logger.bind(symbol=symbol, bare_code=bare_code, module="eastmoney_client")
        log.info("get_stock_sentiment_start")

        # --- 方法 1: AkShare stock_comment_em ---
        try:
            result = await self._try_akshare_comment(bare_code)
            if result is not None:
                log.info("get_stock_sentiment_akshare_comment_ok", method=result.get("source_method"))
                return result
        except Exception as e:
            log.warning("get_stock_sentiment_akshare_comment_error", error=str(e))

        # --- 方法 2: AkShare stock_hot_rank_em ---
        try:
            result = await self._try_akshare_hot_rank(bare_code)
            if result is not None:
                log.info("get_stock_sentiment_akshare_hot_ok", method=result.get("source_method"))
                return result
        except Exception as e:
            log.warning("get_stock_sentiment_akshare_hot_error", error=str(e))

        # --- 方法 3: 爬取股吧页面 ---
        try:
            result = await self._scrape_guba(bare_code)
            log.info("get_stock_sentiment_scrape_ok", method=result.get("source_method"))
            return result
        except Exception as e:
            log.error("get_stock_sentiment_scrape_error", error=str(e))

        # 全部失败，返回空结果
        log.error("get_stock_sentiment_all_methods_failed", symbol=symbol)
        return {
            "post_count": None,
            "post_count_today": None,
            "sentiment_score": None,
            "bullish_pct": None,
            "source_method": "failed",
        }

    async def _try_akshare_comment(self, bare_code: str) -> dict[str, Any] | None:
        """尝试通过 AkShare stock_comment_em 获取情绪。

        Args:
            bare_code: 纯数字股票代码，如 '600519'。

        Returns:
            情绪 dict 或 None（接口不可用/数据为空时）。
        """
        import pandas as pd

        def _fetch() -> pd.DataFrame:
            import akshare as ak
            return ak.stock_comment_em(symbol=bare_code)

        df: pd.DataFrame = await asyncio.to_thread(_fetch)

        if df is None or df.empty:
            return None

        # stock_comment_em 返回字段因版本而异，尽量提取有用信息
        row = df.iloc[0] if len(df) > 0 else None
        if row is None:
            return None

        sentiment_score: float | None = None
        bullish_pct: float | None = None

        # 尝试常见列名
        for col in ["综合评分", "评分", "score"]:
            if col in df.columns:
                try:
                    sentiment_score = float(row[col]) / 100.0  # 归一化到 0-1
                    bullish_pct = float(row[col])  # 直接作为看多占比
                except (ValueError, TypeError):
                    pass
                break

        return {
            "post_count": None,
            "post_count_today": None,
            "sentiment_score": sentiment_score,
            "bullish_pct": bullish_pct,
            "source_method": "akshare_comment_em",
            "_raw_df_cols": list(df.columns),
        }

    async def _try_akshare_hot_rank(self, bare_code: str) -> dict[str, Any] | None:
        """尝试通过 AkShare stock_hot_rank_em 获取热度排名。

        Args:
            bare_code: 纯数字股票代码，如 '600519'。

        Returns:
            情绪 dict 或 None（接口不可用/数据为空/股票不在热榜时）。
        """
        import pandas as pd

        def _fetch() -> pd.DataFrame:
            import akshare as ak
            return ak.stock_hot_rank_em()

        df: pd.DataFrame = await asyncio.to_thread(_fetch)

        if df is None or df.empty:
            return None

        # 查找当前股票在热榜中的位置
        code_col = None
        for col in ["代码", "股票代码", "code"]:
            if col in df.columns:
                code_col = col
                break

        if code_col is None:
            return None

        mask = df[code_col].astype(str).str.strip() == bare_code
        target_rows = df[mask]

        if target_rows.empty:
            return None

        row = target_rows.iloc[0]
        rank: int | None = None
        for col in ["排名", "rank"]:
            if col in df.columns:
                try:
                    rank = int(row[col])
                except (ValueError, TypeError):
                    pass
                break

        # 用排名估算情绪分：排名越靠前越热，归一化为 0-1
        sentiment_score: float | None = None
        if rank is not None:
            total_stocks = len(df)
            sentiment_score = max(0.0, 1.0 - (rank - 1) / max(total_stocks, 1))

        return {
            "post_count": None,
            "post_count_today": None,
            "sentiment_score": sentiment_score,
            "bullish_pct": None,
            "source_method": "akshare_hot_rank_em",
            "hot_rank": rank,
        }

    async def _scrape_guba(self, bare_code: str) -> dict[str, Any]:
        """爬取东方财富股吧页面提取帖子数量。

        Args:
            bare_code: 纯数字股票代码，如 '600519'。

        Returns:
            包含 post_count / post_count_today 的情绪 dict。
        """
        url = f"https://guba.eastmoney.com/list,{bare_code}.html"
        log = logger.bind(bare_code=bare_code, url=url, module="eastmoney_client")
        log.info("scrape_guba_start")

        import os
        proxies = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or None
        async with httpx.AsyncClient(
            timeout=_REQUEST_TIMEOUT,
            follow_redirects=True,
            proxy=proxies,
        ) as client:
            resp = await client.get(url, headers=_SCRAPE_HEADERS)
            resp.raise_for_status()
            html = resp.text

        post_count: int | None = None
        post_count_today: int | None = None

        # 匹配帖子总数 — 页面中 "count": 932593 是最可靠的来源
        patterns_total = [
            r'"count"\s*:\s*(\d{4,})',          # JSON-embedded count (most reliable)
            r"共\s*([\d,]+)\s*篇",
            r"([\d,]+)\s*篇帖子",
            r"总帖子数[：:]\s*([\d,]+)",
            r"articleNum\s*[=:]\s*['\"]?([\d,]+)",
            r'"articleNum"\s*:\s*"?([\d,]+)',
        ]
        for pat in patterns_total:
            m = re.search(pat, html)
            if m:
                try:
                    post_count = int(m.group(1).replace(",", ""))
                    break
                except (ValueError, IndexError):
                    continue

        # 匹配今日帖子数
        patterns_today = [
            r'"todayNum"\s*:\s*"?(\d+)',
            r"todayNum\s*[=:]\s*['\"]?([\d,]+)",
            r"今日\s*([\d,]+)\s*篇",
            r"今日发帖[：:]\s*([\d,]+)",
        ]
        for pat in patterns_today:
            m = re.search(pat, html)
            if m:
                try:
                    post_count_today = int(m.group(1).replace(",", ""))
                    break
                except (ValueError, IndexError):
                    continue

        log.info(
            "scrape_guba_ok",
            post_count=post_count,
            post_count_today=post_count_today,
        )

        return {
            "post_count": post_count,
            "post_count_today": post_count_today,
            "sentiment_score": None,
            "bullish_pct": None,
            "source_method": "scrape_guba",
        }

    async def save_to_db(
        self,
        symbol: str,
        data: dict[str, Any],
        snapshot_time: datetime | None = None,
    ) -> bool:
        """将情绪数据保存到 social_sentiment 表（source='eastmoney'）。

        Args:
            symbol: A 股代码，如 '600519.SH'。
            data: get_stock_sentiment() 返回的情绪 dict。
            snapshot_time: 快照时间，默认为当前 UTC 时间。

        Returns:
            成功返回 True，异常返回 False。
        """
        if snapshot_time is None:
            snapshot_time = datetime.now(timezone.utc)

        log = logger.bind(symbol=symbol, module="eastmoney_client")

        # message_count 优先使用 post_count，次选 post_count_today
        message_count: int | None = (
            data.get("post_count")
            or data.get("post_count_today")
        )

        # 计算 message_delta：与历史最近一次保存的 message_count 对比
        message_delta: float | None = None
        if message_count is not None:
            try:
                prev_count = await db_query_val(
                    """
                    SELECT message_count
                    FROM social_sentiment
                    WHERE symbol = $1 AND source = 'eastmoney'
                    ORDER BY snapshot_time DESC
                    LIMIT 1
                    """,
                    symbol,
                )
                if prev_count is not None and int(prev_count) > 0:
                    message_delta = (
                        (message_count - int(prev_count)) / int(prev_count) * 100.0
                    )
                else:
                    message_delta = 0.0
            except Exception as e:
                log.warning("save_to_db_prev_count_error", error=str(e))
                message_delta = 0.0

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

        # 排除内部字段，只保存有用的 raw_data
        raw_data = {
            k: v for k, v in data.items()
            if k not in ("bullish_pct", "sentiment_score")
        }

        try:
            await db_execute(
                upsert_sql,
                symbol,                          # $1
                "CN",                            # $2 market
                snapshot_time,                   # $3
                "eastmoney",                     # $4 source
                data.get("bullish_pct"),         # $5
                message_count,                   # $6
                message_delta,                   # $7
                data.get("sentiment_score"),     # $8
                json.dumps(raw_data, default=str),  # $9
            )
            log.info(
                "save_to_db_ok",
                symbol=symbol,
                message_count=message_count,
                source_method=data.get("source_method"),
            )
            return True
        except Exception as e:
            log.error("save_to_db_error", symbol=symbol, error=str(e))
            return False

    async def fetch_and_save(self, symbol: str) -> bool:
        """便捷方法：拉取单只股票情绪并保存。"""
        try:
            data = await self.get_stock_sentiment(symbol)
            return await self.save_to_db(symbol, data)
        except Exception as e:
            logger.warning("fetch_and_save_error", symbol=symbol, error=str(e))
            return False

    async def batch_fetch_save(self, symbols: list[str]) -> dict[str, bool]:
        """批量拉取多个 A 股 symbol 情绪数据，调用间隔 1s（爬虫礼貌延迟）。

        Args:
            symbols: A 股代码列表，如 ['600519.SH', '000001.SZ']。

        Returns:
            以 symbol 为 key、成功状态为 value 的 dict。
        """
        log = logger.bind(module="eastmoney_client", total=len(symbols))
        log.info("batch_fetch_save_start")

        results: dict[str, bool] = {}
        for i, symbol in enumerate(symbols):
            log.info("batch_fetch_save_processing", symbol=symbol, index=i + 1, total=len(symbols))
            try:
                data = await self.get_stock_sentiment(symbol)
                results[symbol] = await self.save_to_db(symbol, data)
            except Exception as e:
                log.error("batch_fetch_save_symbol_error", symbol=symbol, error=str(e))
                results[symbol] = False

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
