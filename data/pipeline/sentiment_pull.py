"""情绪数据拉取管道

从 StockTwits（美股）和东方财富股吧（A 股）拉取社交情绪数据，
同时计算 A 股市场整体情绪指标写入 market_sentiment_daily。

Usage:
    python -m data.pipeline.sentiment_pull --stocktwits --symbols AAPL,NVDA
    python -m data.pipeline.sentiment_pull --eastmoney --symbols 600519.SH,000001.SZ
    python -m data.pipeline.sentiment_pull --market-sentiment --date 2026-04-17
    python -m data.pipeline.sentiment_pull --all  # runs all of the above for universe
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

import structlog

from data.sources.eastmoney_client import EastMoneyClient
from data.sources.stocktwits_client import StockTwitsClient
from db.connection import (
    close_pool,
    db_execute,
    db_query,
    db_query_one,
    db_query_val,
    init_pool,
)

logger = structlog.get_logger(__name__)

_RATE_LIMIT_SLEEP: float = 0.3


def _safe_float(val: Any) -> float | None:
    """安全转换为 float，None/NaN 返回 None。"""
    if val is None:
        return None
    try:
        f = float(val)
        import math
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (ValueError, TypeError):
        return None


def _safe_decimal(val: Any) -> Decimal | None:
    """安全转换为 Decimal，None/NaN 返回 None。"""
    if val is None:
        return None
    try:
        import math
        f = float(val)
        if math.isnan(f) or math.isinf(f):
            return None
        return Decimal(str(val))
    except (ValueError, TypeError):
        return None


class SentimentPuller:
    """情绪数据拉取器。

    整合 StockTwits（美股）和东方财富（A 股）情绪源，
    并计算 A 股大盘整体情绪指标。

    Attributes:
        _st_client: StockTwitsClient 实例。
        _em_client: EastMoneyClient 实例。
    """

    def __init__(self) -> None:
        self._st_client = StockTwitsClient()
        self._em_client = EastMoneyClient()

    # ================================================================
    # 候选池查询
    # ================================================================

    async def _get_us_universe(self) -> list[str]:
        """从 stock_universe 获取活跃美股列表。

        Returns:
            美股代码列表，如 ['AAPL', 'NVDA']。
        """
        try:
            rows = await db_query(
                """
                SELECT symbol FROM stock_universe
                WHERE market = 'US' AND status = 'active'
                ORDER BY symbol
                """
            )
            return [str(row["symbol"]) for row in rows]
        except Exception as e:
            logger.error("get_us_universe_error", error=str(e))
            return []

    async def _get_cn_universe(self) -> list[str]:
        """从 stock_universe 获取活跃 A 股列表。

        Returns:
            A 股代码列表，如 ['600519.SH', '000001.SZ']。
        """
        try:
            rows = await db_query(
                """
                SELECT symbol FROM stock_universe
                WHERE market = 'CN' AND status = 'active'
                ORDER BY symbol
                """
            )
            return [str(row["symbol"]) for row in rows]
        except Exception as e:
            logger.error("get_cn_universe_error", error=str(e))
            return []

    # ================================================================
    # StockTwits 拉取
    # ================================================================

    async def pull_stocktwits_universe(self) -> dict[str, Any]:
        """拉取候选池所有美股的 StockTwits 情绪。

        Returns:
            汇总统计 dict：
            - total (int): 处理的 symbol 总数
            - success (int): 成功数
            - failed (int): 失败数
            - results (dict[str, bool]): 各 symbol 成功状态
        """
        log = logger.bind(module="sentiment_pull")
        log.info("pull_stocktwits_universe_start")

        symbols = await self._get_us_universe()
        if not symbols:
            log.warning("pull_stocktwits_universe_no_symbols")
            return {"total": 0, "success": 0, "failed": 0, "results": {}}

        log.info("pull_stocktwits_universe_symbols", count=len(symbols))
        results = await self._st_client.batch_fetch_save(symbols)

        success = sum(1 for v in results.values() if v)
        summary = {
            "total": len(symbols),
            "success": success,
            "failed": len(symbols) - success,
            "results": results,
        }
        log.info("pull_stocktwits_universe_done", **{k: v for k, v in summary.items() if k != "results"})
        return summary

    async def pull_stocktwits_symbols(self, symbols: list[str]) -> dict[str, Any]:
        """拉取指定美股 symbol 列表的 StockTwits 情绪。

        Args:
            symbols: 美股代码列表，如 ['AAPL', 'NVDA']。

        Returns:
            汇总统计 dict。
        """
        log = logger.bind(module="sentiment_pull", symbols=symbols)
        log.info("pull_stocktwits_symbols_start")

        results = await self._st_client.batch_fetch_save(symbols)
        success = sum(1 for v in results.values() if v)
        return {
            "total": len(symbols),
            "success": success,
            "failed": len(symbols) - success,
            "results": results,
        }

    # ================================================================
    # EastMoney 拉取
    # ================================================================

    async def pull_eastmoney_universe(self) -> dict[str, Any]:
        """拉取候选池所有 A 股的东方财富股吧情绪。

        Returns:
            汇总统计 dict：
            - total (int): 处理的 symbol 总数
            - success (int): 成功数
            - failed (int): 失败数
            - results (dict[str, bool]): 各 symbol 成功状态
        """
        log = logger.bind(module="sentiment_pull")
        log.info("pull_eastmoney_universe_start")

        symbols = await self._get_cn_universe()
        if not symbols:
            log.warning("pull_eastmoney_universe_no_symbols")
            return {"total": 0, "success": 0, "failed": 0, "results": {}}

        log.info("pull_eastmoney_universe_symbols", count=len(symbols))
        results = await self._em_client.batch_fetch_save(symbols)

        success = sum(1 for v in results.values() if v)
        summary = {
            "total": len(symbols),
            "success": success,
            "failed": len(symbols) - success,
            "results": results,
        }
        log.info("pull_eastmoney_universe_done", **{k: v for k, v in summary.items() if k != "results"})
        return summary

    async def pull_eastmoney_symbols(self, symbols: list[str]) -> dict[str, Any]:
        """拉取指定 A 股 symbol 列表的东方财富股吧情绪。

        Args:
            symbols: A 股代码列表，如 ['600519.SH', '000001.SZ']。

        Returns:
            汇总统计 dict。
        """
        log = logger.bind(module="sentiment_pull", symbols=symbols)
        log.info("pull_eastmoney_symbols_start")

        results = await self._em_client.batch_fetch_save(symbols)
        success = sum(1 for v in results.values() if v)
        return {
            "total": len(symbols),
            "success": success,
            "failed": len(symbols) - success,
            "results": results,
        }

    # ================================================================
    # 大盘市场情绪
    # ================================================================

    async def pull_market_sentiment(self, trade_date: date | None = None) -> bool:
        """拉取并计算 A 股大盘整体情绪指标，写入 market_sentiment_daily。

        计算步骤：
        1. 涨停/跌停数量 — AkShare stock_zt_pool_em / stock_zt_pool_dtgc_em
        2. 涨跌比 — 从 market_bars_daily 统计
        3. 融资余额 — 从 margin_daily 聚合
        4. 融资余额5日变化 — 对比5个交易日前
        5. 恐慌贪婪指数 — 综合加权计算

        Args:
            trade_date: 交易日期，默认为今天（UTC）。

        Returns:
            成功返回 True，失败返回 False。
        """
        if trade_date is None:
            trade_date = datetime.now(timezone.utc).date()

        log = logger.bind(trade_date=str(trade_date), module="sentiment_pull")
        log.info("pull_market_sentiment_start")

        # ---- 1. 涨停/跌停数量 ----
        limit_up_count, limit_down_count = await self._fetch_limit_counts(trade_date)

        # ---- 2. 涨跌比 ----
        up_down_ratio = await self._compute_up_down_ratio(trade_date)

        # ---- 3 & 4. 融资余额 + 5日变化 ----
        margin_balance, margin_delta_5d = await self._compute_margin_stats(trade_date)

        # ---- 5. 恐慌贪婪指数 ----
        fear_greed = self._compute_fear_greed(
            up_down_ratio=up_down_ratio,
            margin_delta_5d=margin_delta_5d,
            limit_up_count=limit_up_count,
            limit_down_count=limit_down_count,
        )

        log.info(
            "pull_market_sentiment_computed",
            limit_up=limit_up_count,
            limit_down=limit_down_count,
            up_down_ratio=up_down_ratio,
            margin_balance=str(margin_balance) if margin_balance else None,
            margin_delta_5d=margin_delta_5d,
            fear_greed=fear_greed,
        )

        # ---- 6. Upsert ----
        upsert_sql = """
            INSERT INTO market_sentiment_daily (
                trade_date, limit_up_count, limit_down_count, up_down_ratio,
                margin_balance, margin_delta_5d, vix_cn, fear_greed
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (trade_date) DO UPDATE SET
                limit_up_count   = EXCLUDED.limit_up_count,
                limit_down_count = EXCLUDED.limit_down_count,
                up_down_ratio    = EXCLUDED.up_down_ratio,
                margin_balance   = EXCLUDED.margin_balance,
                margin_delta_5d  = EXCLUDED.margin_delta_5d,
                vix_cn           = EXCLUDED.vix_cn,
                fear_greed       = EXCLUDED.fear_greed
        """

        try:
            await db_execute(
                upsert_sql,
                trade_date,                           # $1
                limit_up_count,                       # $2
                limit_down_count,                     # $3
                _safe_decimal(up_down_ratio),         # $4
                margin_balance,                       # $5
                _safe_decimal(margin_delta_5d),       # $6
                Decimal("50"),                        # $7 vix_cn 占位符（无 A 股 VIX 数据）
                _safe_decimal(fear_greed),            # $8
            )
            log.info("pull_market_sentiment_saved", trade_date=str(trade_date))
            return True
        except Exception as e:
            log.error("pull_market_sentiment_upsert_error", error=str(e))
            return False

    async def _fetch_limit_counts(
        self, trade_date: date
    ) -> tuple[int | None, int | None]:
        """获取涨停/跌停数量。

        优先通过 AkShare 获取，失败则从 market_bars_daily 近似统计。

        Args:
            trade_date: 交易日期。

        Returns:
            (limit_up_count, limit_down_count) 元组，获取失败时对应值为 None。
        """
        log = logger.bind(trade_date=str(trade_date), module="sentiment_pull")

        # 尝试 AkShare
        date_str = trade_date.strftime("%Y%m%d")
        try:
            limit_up, limit_down = await asyncio.to_thread(
                self._fetch_limit_counts_akshare, date_str
            )
            if limit_up is not None:
                log.info("fetch_limit_counts_akshare_ok", limit_up=limit_up, limit_down=limit_down)
                return limit_up, limit_down
        except Exception as e:
            log.warning("fetch_limit_counts_akshare_error", error=str(e))

        # 降级：从 market_bars_daily 近似统计（涨幅 ≥ 9.9% 视为涨停）
        try:
            limit_up_count = await db_query_val(
                """
                SELECT COUNT(*)
                FROM market_bars_daily
                WHERE trade_date = $1 AND market = 'CN'
                  AND open > 0
                  AND (close - open) / open >= 0.099
                """,
                trade_date,
            )
            limit_down_count = await db_query_val(
                """
                SELECT COUNT(*)
                FROM market_bars_daily
                WHERE trade_date = $1 AND market = 'CN'
                  AND open > 0
                  AND (close - open) / open <= -0.099
                """,
                trade_date,
            )
            log.info(
                "fetch_limit_counts_db_fallback_ok",
                limit_up=limit_up_count,
                limit_down=limit_down_count,
            )
            return (
                int(limit_up_count) if limit_up_count is not None else None,
                int(limit_down_count) if limit_down_count is not None else None,
            )
        except Exception as e:
            log.error("fetch_limit_counts_db_fallback_error", error=str(e))
            return None, None

    def _fetch_limit_counts_akshare(
        self, date_str: str
    ) -> tuple[int | None, int | None]:
        """同步方法：通过 AkShare 获取涨停/跌停池数量（供 asyncio.to_thread 调用）。

        Args:
            date_str: 日期字符串，格式 'YYYYMMDD'。

        Returns:
            (limit_up_count, limit_down_count) 元组。
        """
        import akshare as ak

        limit_up_count: int | None = None
        limit_down_count: int | None = None

        try:
            df_zt = ak.stock_zt_pool_em(date=date_str)
            if df_zt is not None and not df_zt.empty:
                limit_up_count = len(df_zt)
        except Exception:
            pass

        try:
            df_dt = ak.stock_zt_pool_dtgc_em(date=date_str)
            if df_dt is not None and not df_dt.empty:
                limit_down_count = len(df_dt)
        except Exception:
            pass

        return limit_up_count, limit_down_count

    async def _compute_up_down_ratio(self, trade_date: date) -> float | None:
        """从 market_bars_daily 计算当日涨跌比。

        Args:
            trade_date: 交易日期。

        Returns:
            涨跌比（上涨数 / 下跌数），下跌数为 0 时返回上涨数，查询失败返回 None。
        """
        log = logger.bind(trade_date=str(trade_date), module="sentiment_pull")
        try:
            row = await db_query_one(
                """
                SELECT
                    COUNT(*) FILTER (WHERE close >= open) AS up_count,
                    COUNT(*) FILTER (WHERE close < open)  AS down_count
                FROM market_bars_daily
                WHERE trade_date = $1 AND market = 'CN'
                """,
                trade_date,
            )
            if row is None:
                return None

            up_count = int(row["up_count"] or 0)
            down_count = int(row["down_count"] or 0)

            if down_count == 0:
                ratio = float(up_count) if up_count > 0 else None
            else:
                ratio = up_count / down_count

            log.info("compute_up_down_ratio_ok", up=up_count, down=down_count, ratio=ratio)
            return ratio
        except Exception as e:
            log.error("compute_up_down_ratio_error", error=str(e))
            return None

    async def _compute_margin_stats(
        self, trade_date: date
    ) -> tuple[Decimal | None, float | None]:
        """计算融资余额及 5 日变化率。

        Args:
            trade_date: 交易日期。

        Returns:
            (margin_balance, margin_delta_5d) 元组。
            margin_balance: 当日全市场融资余额（万元）。
            margin_delta_5d: 相对 5 个交易日前的变化率（百分比）。
        """
        log = logger.bind(trade_date=str(trade_date), module="sentiment_pull")

        # 当日融资余额
        margin_balance: Decimal | None = None
        try:
            val = await db_query_val(
                """
                SELECT SUM(rzye)
                FROM margin_daily
                WHERE trade_date = $1
                """,
                trade_date,
            )
            if val is not None:
                margin_balance = Decimal(str(val))
                log.info("compute_margin_balance_ok", balance=str(margin_balance))
        except Exception as e:
            log.warning("compute_margin_balance_error", error=str(e))

        # 5 个交易日前的融资余额（使用已有数据最近5条中最早的那条）
        margin_delta_5d: float | None = None
        if margin_balance is not None:
            try:
                prev_val = await db_query_val(
                    """
                    SELECT SUM(rzye)
                    FROM margin_daily
                    WHERE trade_date = (
                        SELECT trade_date FROM margin_daily
                        WHERE trade_date < $1
                        GROUP BY trade_date
                        ORDER BY trade_date DESC
                        LIMIT 1 OFFSET 4
                    )
                    """,
                    trade_date,
                )
                if prev_val is not None and float(prev_val) > 0:
                    prev_balance = float(prev_val)
                    curr_balance = float(margin_balance)
                    margin_delta_5d = (curr_balance - prev_balance) / prev_balance * 100.0
                    log.info(
                        "compute_margin_delta_5d_ok",
                        delta_pct=margin_delta_5d,
                    )
            except Exception as e:
                log.warning("compute_margin_delta_5d_error", error=str(e))

        return margin_balance, margin_delta_5d

    def _compute_fear_greed(
        self,
        up_down_ratio: float | None,
        margin_delta_5d: float | None,
        limit_up_count: int | None,
        limit_down_count: int | None,
    ) -> float | None:
        """计算恐慌贪婪综合指数（0-100）。

        各分项归一化后等权平均：
        - 涨跌比分 (25%)：ratio=0.5→0, 1.0→50, 2.0→80, 3.0→100
        - 融资余额5日变化分 (25%)：-5%→0, 0%→50, +5%→100
        - VIX占位分 (25%)：固定50（暂无A股VIX）
        - 涨停/跌停比分 (25%)：0→0, 0.5→50, 1→100

        Args:
            up_down_ratio: 当日涨跌比（上涨数 / 下跌数）。
            margin_delta_5d: 融资余额5日变化率（百分比）。
            limit_up_count: 涨停数量。
            limit_down_count: 跌停数量。

        Returns:
            0-100 的恐慌贪婪指数，分项均缺失时返回 None。
        """
        components: list[float] = []

        # a. 涨跌比分
        if up_down_ratio is not None:
            r = up_down_ratio
            if r <= 0.5:
                score_a = 0.0
            elif r <= 1.0:
                score_a = (r - 0.5) / 0.5 * 50.0
            elif r <= 2.0:
                score_a = 50.0 + (r - 1.0) / 1.0 * 30.0
            elif r <= 3.0:
                score_a = 80.0 + (r - 2.0) / 1.0 * 20.0
            else:
                score_a = 100.0
            components.append(score_a)
        else:
            components.append(50.0)  # 数据缺失时中性

        # b. 融资余额5日变化分
        if margin_delta_5d is not None:
            delta = margin_delta_5d
            if delta <= -5.0:
                score_b = 0.0
            elif delta <= 0.0:
                score_b = (delta + 5.0) / 5.0 * 50.0
            elif delta <= 5.0:
                score_b = 50.0 + delta / 5.0 * 50.0
            else:
                score_b = 100.0
            components.append(score_b)
        else:
            components.append(50.0)  # 数据缺失时中性

        # c. VIX 占位分（暂无 A 股 VIX 数据，固定 50）
        components.append(50.0)

        # d. 涨停/跌停比分
        lu = limit_up_count or 0
        ld = limit_down_count or 0
        if lu + ld > 0:
            ld_ratio = lu / (lu + ld)
            score_d = ld_ratio * 100.0
            components.append(score_d)
        else:
            components.append(50.0)  # 数据缺失时中性

        if not components:
            return None

        fear_greed = sum(components) / len(components)
        return round(fear_greed, 4)

    # ================================================================
    # 全量拉取
    # ================================================================

    async def pull_all(self, trade_date: date | None = None) -> dict[str, Any]:
        """拉取所有情绪数据（StockTwits + 东方财富 + 大盘情绪）。

        Args:
            trade_date: 大盘情绪计算日期，默认今天。

        Returns:
            各模块汇总结果 dict。
        """
        log = logger.bind(module="sentiment_pull")
        log.info("pull_all_start")

        results: dict[str, Any] = {}

        # StockTwits
        try:
            results["stocktwits"] = await self.pull_stocktwits_universe()
        except Exception as e:
            log.error("pull_all_stocktwits_error", error=str(e))
            results["stocktwits"] = {"error": str(e)}

        await asyncio.sleep(_RATE_LIMIT_SLEEP)

        # EastMoney
        try:
            results["eastmoney"] = await self.pull_eastmoney_universe()
        except Exception as e:
            log.error("pull_all_eastmoney_error", error=str(e))
            results["eastmoney"] = {"error": str(e)}

        await asyncio.sleep(_RATE_LIMIT_SLEEP)

        # 大盘情绪
        try:
            results["market_sentiment"] = await self.pull_market_sentiment(trade_date)
        except Exception as e:
            log.error("pull_all_market_sentiment_error", error=str(e))
            results["market_sentiment"] = False

        log.info(
            "pull_all_done",
            stocktwits_success=results.get("stocktwits", {}).get("success"),
            eastmoney_success=results.get("eastmoney", {}).get("success"),
            market_sentiment=results.get("market_sentiment"),
        )
        return results


# ================================================================
# CLI 入口
# ================================================================

async def _main() -> None:
    """CLI 入口，解析参数并执行对应操作。"""
    parser = argparse.ArgumentParser(
        description="P10-AlphaRadar 情绪数据拉取管道"
    )
    parser.add_argument(
        "--stocktwits",
        action="store_true",
        help="拉取 StockTwits 美股情绪（需配合 --symbols 或自动使用候选池）",
    )
    parser.add_argument(
        "--eastmoney",
        action="store_true",
        help="拉取东方财富股吧 A 股情绪（需配合 --symbols 或自动使用候选池）",
    )
    parser.add_argument(
        "--market-sentiment",
        action="store_true",
        help="拉取并计算 A 股大盘整体情绪指标",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="拉取所有情绪数据（StockTwits + 东方财富 + 大盘情绪）",
    )
    parser.add_argument(
        "--symbols",
        type=str,
        default="",
        help="逗号分隔的股票代码，如 AAPL,NVDA 或 600519.SH,000001.SZ",
    )
    parser.add_argument(
        "--date",
        type=str,
        default="",
        help="交易日期，格式 YYYY-MM-DD（用于 --market-sentiment）",
    )

    args = parser.parse_args()

    # 初始化数据库连接池
    await init_pool()
    puller = SentimentPuller()

    try:
        # 解析 symbols
        symbols: list[str] = (
            [s.strip() for s in args.symbols.split(",") if s.strip()]
            if args.symbols
            else []
        )

        # 解析日期
        trade_date: date | None = None
        if args.date:
            try:
                trade_date = date.fromisoformat(args.date)
            except ValueError:
                parser.error(f"日期格式无效：{args.date}，请使用 YYYY-MM-DD")

        if args.all:
            result = await puller.pull_all(trade_date)
            logger.info("cli_all_done", result=str(result))

        elif args.stocktwits:
            if symbols:
                result = await puller.pull_stocktwits_symbols(symbols)
            else:
                result = await puller.pull_stocktwits_universe()
            logger.info("cli_stocktwits_done", **{k: v for k, v in result.items() if k != "results"})

        elif args.eastmoney:
            if symbols:
                result = await puller.pull_eastmoney_symbols(symbols)
            else:
                result = await puller.pull_eastmoney_universe()
            logger.info("cli_eastmoney_done", **{k: v for k, v in result.items() if k != "results"})

        elif args.market_sentiment:
            success = await puller.pull_market_sentiment(trade_date)
            logger.info("cli_market_sentiment_done", success=success)

        else:
            parser.print_help()

    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
