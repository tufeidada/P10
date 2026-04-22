"""
对照组基准计算模块。

每日收盘后更新 benchmark_daily 表，提供买入持有沪深300和
动量 Top20 等权组合两个基准策略的日收益、累计收益和最大回撤。
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import structlog

from db.connection import db_execute, db_query, db_query_one

logger = structlog.get_logger(__name__)

# 沪深300 在 market_bars_daily 中的 symbol
_HS300_SYMBOL = "000300.SH"
# 标普500
_SP500_SYMBOL = "SPY"

# 动量组合每周调仓的持股数
_MOMENTUM_TOP_N = 20


class BenchmarkCalculator:
    """对照组基准计算。

    每日收盘后更新 benchmark_daily 表，支持 CN（A股）和 US（美股）市场。
    """

    async def update_benchmarks(
        self, trade_date: date | None = None, market: str = "CN"
    ) -> None:
        """计算并保存指定日期的所有基准。

        Args:
            trade_date: 交易日期，默认为最近交易日。
            market: 市场代码，'CN' 或 'US'。
        """
        if trade_date is None:
            row = await db_query_one(
                """
                SELECT trade_date FROM trade_calendar
                WHERE trade_date <= CURRENT_DATE
                ORDER BY trade_date DESC LIMIT 1
                """
            )
            if not row:
                logger.warning("benchmark_no_trade_date")
                return
            trade_date = row["trade_date"]

        logger.info(
            "benchmark_update_start",
            trade_date=str(trade_date),
            market=market,
        )

        # 买入持有基准
        try:
            bh = await self._calc_buy_and_hold(trade_date, market)
            if bh:
                await self._upsert_benchmark(trade_date, market, bh)
                logger.info(
                    "benchmark_buy_hold_done",
                    name=bh["benchmark_name"],
                    daily_return=bh["daily_return"],
                )
        except Exception:
            logger.exception("benchmark_buy_hold_error", market=market)

        # 动量 Top20 等权组合
        try:
            mom = await self._calc_momentum_top20(trade_date, market)
            if mom:
                await self._upsert_benchmark(trade_date, market, mom)
                logger.info(
                    "benchmark_momentum_done",
                    name=mom["benchmark_name"],
                    daily_return=mom["daily_return"],
                )
        except Exception:
            logger.exception("benchmark_momentum_error", market=market)

        logger.info("benchmark_update_done", trade_date=str(trade_date))

    async def _calc_buy_and_hold(
        self, trade_date: date, market: str
    ) -> dict[str, Any] | None:
        """买入持有指数基准。

        CN 市场用沪深300，US 市场用 SPY。

        Args:
            trade_date: 交易日期。
            market: 市场代码。

        Returns:
            包含 benchmark_name, daily_return, cumulative_return, max_drawdown
            的字典，无数据时返回 None。
        """
        symbol = _HS300_SYMBOL if market == "CN" else _SP500_SYMBOL
        benchmark_name = "buy_hold_hs300" if market == "CN" else "buy_hold_sp500"

        # 取今天和前一交易日的收盘价
        bars = await db_query(
            """
            SELECT trade_date, close
            FROM market_bars_daily
            WHERE symbol = $1 AND trade_date <= $2
            ORDER BY trade_date DESC
            LIMIT 2
            """,
            symbol,
            trade_date,
        )
        if len(bars) < 2 or bars[0]["trade_date"] != trade_date:
            logger.warning(
                "benchmark_bh_no_data",
                symbol=symbol,
                trade_date=str(trade_date),
            )
            return None

        close_today = float(bars[0]["close"])
        close_yesterday = float(bars[1]["close"])

        if close_yesterday == 0:
            return None

        daily_return = round(close_today / close_yesterday - 1, 6)

        # 取该指数最早可用日期的收盘价计算累计收益
        first_bar = await db_query_one(
            """
            SELECT close FROM market_bars_daily
            WHERE symbol = $1
            ORDER BY trade_date
            LIMIT 1
            """,
            symbol,
        )
        if not first_bar or float(first_bar["close"]) == 0:
            return None

        close_first = float(first_bar["close"])
        cumulative_return = round(close_today / close_first - 1, 6)

        # 最大回撤：从历史最高累计收益到当前
        max_drawdown = await self._calc_max_drawdown_index(
            symbol, trade_date, close_first
        )

        return {
            "benchmark_name": benchmark_name,
            "daily_return": daily_return,
            "cumulative_return": cumulative_return,
            "max_drawdown": max_drawdown,
        }

    async def _calc_buy_and_hold_hs300(self, trade_date: date) -> dict[str, Any] | None:
        """HS300 买入持有日收益（兼容接口）。

        Args:
            trade_date: 交易日期。

        Returns:
            基准字典或 None。
        """
        return await self._calc_buy_and_hold(trade_date, "CN")

    async def _calc_momentum_top20(
        self, trade_date: date, market: str = "CN"
    ) -> dict[str, Any] | None:
        """动量 Top20 等权组合基准。

        每周一（或当周第一个交易日）重新选股：
        - 从 features_daily 选 RS Rank 最高的 20 只股票
        - 等权持有，每日计算组合平均收益
        - 非调仓日沿用上期持仓

        Args:
            trade_date: 交易日期。
            market: 市场代码。

        Returns:
            基准字典或 None。
        """
        benchmark_name = "momentum_top20"

        # 判断是否为调仓日（当周第一个交易日）
        is_rebalance_day = await self._is_first_trading_day_of_week(trade_date)

        # 获取当期持仓组合
        portfolio: list[str] | None = None

        if is_rebalance_day:
            # 调仓：选 RS Rank 前 20
            portfolio = await self._select_momentum_stocks(trade_date, market)
            if not portfolio:
                logger.warning(
                    "benchmark_momentum_no_rs_data",
                    trade_date=str(trade_date),
                )
                # 尝试沿用上期
                portfolio = await self._get_prev_portfolio(
                    trade_date, market, benchmark_name
                )
        else:
            # 非调仓日：沿用上期
            portfolio = await self._get_prev_portfolio(
                trade_date, market, benchmark_name
            )
            if not portfolio:
                # 如果没有上期记录，尝试选股
                portfolio = await self._select_momentum_stocks(trade_date, market)

        if not portfolio:
            logger.info(
                "benchmark_momentum_skip",
                trade_date=str(trade_date),
                reason="no_portfolio",
            )
            return None

        # 计算组合今日等权平均日收益
        prev_td = await self._get_prev_trading_date(trade_date)
        if prev_td is None:
            return None

        daily_returns: list[float] = []
        for sym in portfolio:
            bars = await db_query(
                """
                SELECT trade_date, close
                FROM market_bars_daily
                WHERE symbol = $1 AND trade_date IN ($2, $3)
                ORDER BY trade_date
                """,
                sym,
                prev_td,
                trade_date,
            )
            if len(bars) == 2 and float(bars[0]["close"]) > 0:
                ret = float(bars[1]["close"]) / float(bars[0]["close"]) - 1
                daily_returns.append(ret)

        if not daily_returns:
            return None

        daily_return = round(sum(daily_returns) / len(daily_returns), 6)

        # 累计收益：从上期的 cumulative_return 累乘
        prev_bench = await db_query_one(
            """
            SELECT cumulative_return, max_drawdown
            FROM benchmark_daily
            WHERE market = $1 AND benchmark_name = $2 AND trade_date < $3
            ORDER BY trade_date DESC
            LIMIT 1
            """,
            market,
            benchmark_name,
            trade_date,
        )

        if prev_bench and prev_bench["cumulative_return"] is not None:
            prev_cum = float(prev_bench["cumulative_return"])
            cumulative_return = round(
                (1 + prev_cum) * (1 + daily_return) - 1, 6
            )
            prev_dd = float(prev_bench["max_drawdown"] or 0)
        else:
            cumulative_return = daily_return
            prev_dd = 0.0

        # 最大回撤：running peak of cumulative return
        # peak = max(historical cumulative_return)
        peak_row = await db_query_one(
            """
            SELECT MAX(cumulative_return) AS peak
            FROM benchmark_daily
            WHERE market = $1 AND benchmark_name = $2 AND trade_date < $3
            """,
            market,
            benchmark_name,
            trade_date,
        )
        peak = float(peak_row["peak"]) if peak_row and peak_row["peak"] is not None else 0.0
        # 当前累计也可能是新 peak
        current_peak = max(peak, cumulative_return)

        if current_peak > 0 and cumulative_return < current_peak:
            dd = round(
                (cumulative_return - current_peak) / (1 + current_peak), 6
            )
        else:
            dd = 0.0

        max_drawdown = min(dd, prev_dd)  # 保持历史最大回撤

        return {
            "benchmark_name": benchmark_name,
            "daily_return": daily_return,
            "cumulative_return": cumulative_return,
            "max_drawdown": max_drawdown,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _calc_max_drawdown_index(
        self, symbol: str, trade_date: date, close_first: float
    ) -> float:
        """计算指数买入持有策略的历史最大回撤。

        Args:
            symbol: 指数 symbol。
            trade_date: 截止日期。
            close_first: 起始日收盘价（用于计算累计收益）。

        Returns:
            最大回撤值（负数或零）。
        """
        rows = await db_query(
            """
            SELECT close
            FROM market_bars_daily
            WHERE symbol = $1 AND trade_date <= $2
            ORDER BY trade_date
            """,
            symbol,
            trade_date,
        )

        if not rows:
            return 0.0

        peak_cum = 0.0
        max_dd = 0.0
        for r in rows:
            cum = float(r["close"]) / close_first - 1
            if cum > peak_cum:
                peak_cum = cum
            if peak_cum > 0:
                dd = (cum - peak_cum) / (1 + peak_cum)
                if dd < max_dd:
                    max_dd = dd

        return round(max_dd, 6)

    async def _is_first_trading_day_of_week(self, trade_date: date) -> bool:
        """判断是否为当周第一个交易日。

        Args:
            trade_date: 交易日期。

        Returns:
            True 如果是当周第一个交易日。
        """
        # 获取 trade_date 所在自然周的周一
        monday = trade_date.toordinal() - trade_date.weekday()
        monday_date = date.fromordinal(monday)

        row = await db_query_one(
            """
            SELECT trade_date
            FROM trade_calendar
            WHERE trade_date >= $1 AND trade_date <= $2
            ORDER BY trade_date
            LIMIT 1
            """,
            monday_date,
            trade_date,
        )
        return row is not None and row["trade_date"] == trade_date

    async def _select_momentum_stocks(
        self, trade_date: date, market: str
    ) -> list[str] | None:
        """选取 RS Rank 最高的 Top N 只股票。

        Args:
            trade_date: 交易日期。
            market: 市场代码。

        Returns:
            symbol 列表，或 None。
        """
        # 取最近有 rs_rank 数据的交易日（可能是当天或前几天）
        rows = await db_query(
            """
            SELECT f.symbol
            FROM features_daily f
            JOIN stock_universe su ON su.symbol = f.symbol AND su.active = TRUE
            WHERE su.market = $1
              AND f.trade_date = (
                  SELECT MAX(trade_date)
                  FROM features_daily
                  WHERE trade_date <= $2 AND rs_rank IS NOT NULL
              )
              AND f.rs_rank IS NOT NULL
            ORDER BY f.rs_rank DESC
            LIMIT $3
            """,
            market,
            trade_date,
            _MOMENTUM_TOP_N,
        )

        if not rows:
            return None

        return [r["symbol"] for r in rows]

    async def _get_prev_portfolio(
        self, trade_date: date, market: str, benchmark_name: str
    ) -> list[str] | None:
        """从上期 benchmark_daily 的 JSONB 或重新从 features_daily 推导持仓。

        benchmark_daily 没有 portfolio 字段，因此回溯上一个调仓日
        （最近的周一或第一个交易日）重新推导。

        Args:
            trade_date: 当前交易日。
            market: 市场代码。
            benchmark_name: 基准名称。

        Returns:
            symbol 列表或 None。
        """
        # 找到上一个调仓日（当周之前最近的周第一交易日）
        # 简化处理：取上周一到上周五期间的第一个交易日
        monday = trade_date.toordinal() - trade_date.weekday()
        last_friday = date.fromordinal(monday - 3)
        last_monday = date.fromordinal(monday - 7)

        row = await db_query_one(
            """
            SELECT trade_date
            FROM trade_calendar
            WHERE trade_date >= $1 AND trade_date <= $2
            ORDER BY trade_date
            LIMIT 1
            """,
            last_monday,
            last_friday,
        )

        if row:
            return await self._select_momentum_stocks(row["trade_date"], market)

        # 再往前找
        return await self._select_momentum_stocks(trade_date, market)

    async def _get_prev_trading_date(self, trade_date: date) -> date | None:
        """获取前一个交易日。

        Args:
            trade_date: 当前交易日。

        Returns:
            前一交易日或 None。
        """
        row = await db_query_one(
            """
            SELECT trade_date
            FROM trade_calendar
            WHERE trade_date < $1
            ORDER BY trade_date DESC
            LIMIT 1
            """,
            trade_date,
        )
        return row["trade_date"] if row else None

    async def _upsert_benchmark(
        self, trade_date: date, market: str, data: dict[str, Any]
    ) -> None:
        """写入或更新 benchmark_daily 行。

        Args:
            trade_date: 交易日期。
            market: 市场代码。
            data: 包含 benchmark_name, daily_return, cumulative_return, max_drawdown。
        """
        await db_execute(
            """
            INSERT INTO benchmark_daily
                (trade_date, market, benchmark_name, daily_return,
                 cumulative_return, max_drawdown)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (trade_date, market, benchmark_name)
            DO UPDATE SET
                daily_return = EXCLUDED.daily_return,
                cumulative_return = EXCLUDED.cumulative_return,
                max_drawdown = EXCLUDED.max_drawdown
            """,
            trade_date,
            market,
            data["benchmark_name"],
            data["daily_return"],
            data["cumulative_return"],
            data["max_drawdown"],
        )
