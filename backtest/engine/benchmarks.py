"""
backtest/engine/benchmarks.py — 三对照组基准

Benchmark 1 (B&H 指数):
  CN=HS300, US=SPY。起始日买入，全程持有。

Benchmark 2 (等权买入持有):
  开始日对 watchlist 所有有数据的股票等权买入，全程不调仓。
  组合收益率 = 各股当日收益率的简单平均。

Benchmark 3 (周动量轮动):
  每周一重新排序，选 ret_20d 最高的 top-5 等权持有。
  调仓时先锁定上周组合在调仓日的价值，再按新价格重置基准。
  收益率通过累计价值倍数（_mom_value）链式复合。
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Optional

import asyncpg

log = logging.getLogger(__name__)

_TOP_N = 5


class Benchmarks:
    """单一市场（CN 或 US）的三对照组基准计算器。"""

    def __init__(self, market: str) -> None:
        self.market = market
        self._index_code = "HS300" if market == "CN" else "SPY"

        # B1
        self._bnh_start_price: Optional[float] = None

        # B2
        self._ew_symbols: list[str] = []
        self._ew_start_prices: dict[str, float] = {}

        # B3
        self._mom_symbols: list[str] = []
        self._mom_rebal_date: Optional[date] = None
        self._mom_rebal_prices: dict[str, float] = {}
        self._mom_value: float = 1.0   # 累计价值倍数（从 1.0 开始）

    # ─────────────────────────────────────────────────────────────────────────
    # 公开接口
    # ─────────────────────────────────────────────────────────────────────────

    async def initialize(
        self,
        pool: asyncpg.Pool,
        start_date: date,
        symbols: list[str],
    ) -> None:
        """回测首日前调用，初始化各基准的起始状态。"""
        async with pool.acquire() as conn:
            # B1: 指数起始价
            row = await conn.fetchrow(
                "SELECT close FROM index_daily "
                "WHERE index_code = $1 AND trade_date <= $2 "
                "ORDER BY trade_date DESC LIMIT 1",
                self._index_code, start_date,
            )
            if row and row["close"]:
                self._bnh_start_price = float(row["close"])
                log.info(
                    f"benchmarks[{self.market}] B1 {self._index_code} "
                    f"start_price={self._bnh_start_price:.4f} at {start_date}"
                )
            else:
                log.warning(
                    f"benchmarks[{self.market}] B1: no price for "
                    f"{self._index_code} at {start_date}"
                )

            # B2: 各股起始收盘价（取 <= start_date 最近一条）
            rows = await conn.fetch(
                """
                SELECT DISTINCT ON (symbol) symbol, close
                FROM market_bars_daily
                WHERE symbol = ANY($1)
                  AND trade_date <= $2
                  AND available_date <= $2
                  AND close IS NOT NULL AND close > 0
                ORDER BY symbol, trade_date DESC
                """,
                symbols, start_date,
            )
            self._ew_start_prices = {r["symbol"]: float(r["close"]) for r in rows}
            self._ew_symbols = list(self._ew_start_prices.keys())
            log.info(
                f"benchmarks[{self.market}] B2: "
                f"{len(self._ew_symbols)}/{len(symbols)} stocks have start prices"
            )

            # B3: 首次选股（初始化动量组合）
            await self._rebalance(conn, start_date, symbols)

    async def update(
        self,
        pool: asyncpg.Pool,
        current_date: date,
        symbols: list[str],
    ) -> tuple[float, float, float]:
        """
        返回当日三个基准的累计收益率（小数，例如 0.05 = 5%）。
        若是新的一周，先对 B3 调仓。
        """
        async with pool.acquire() as conn:
            ret1 = await self._calc_bnh(conn, current_date)
            ret2 = await self._calc_ew(conn, current_date)

            if self._is_new_week(current_date):
                # 锁定上一轮组合在今日的价值倍数，再重选
                self._mom_value = await self._mom_value_now(conn, current_date)
                await self._rebalance(conn, current_date, symbols)

            ret3 = await self._calc_mom(conn, current_date)

        return ret1, ret2, ret3

    # ─────────────────────────────────────────────────────────────────────────
    # B1 - B&H Index
    # ─────────────────────────────────────────────────────────────────────────

    async def _calc_bnh(self, conn, current_date: date) -> float:
        if not self._bnh_start_price:
            return 0.0
        row = await conn.fetchrow(
            "SELECT close FROM index_daily "
            "WHERE index_code = $1 AND trade_date <= $2 "
            "ORDER BY trade_date DESC LIMIT 1",
            self._index_code, current_date,
        )
        if not row or not row["close"]:
            return 0.0
        return float(row["close"]) / self._bnh_start_price - 1.0

    # ─────────────────────────────────────────────────────────────────────────
    # B2 - Equal Weight
    # ─────────────────────────────────────────────────────────────────────────

    async def _calc_ew(self, conn, current_date: date) -> float:
        if not self._ew_symbols:
            return 0.0
        rows = await conn.fetch(
            """
            SELECT DISTINCT ON (symbol) symbol, close
            FROM market_bars_daily
            WHERE symbol = ANY($1)
              AND trade_date <= $2
              AND available_date <= $2
              AND close IS NOT NULL AND close > 0
            ORDER BY symbol, trade_date DESC
            """,
            self._ew_symbols, current_date,
        )
        rets = []
        for r in rows:
            start_px = self._ew_start_prices.get(r["symbol"])
            if start_px and start_px > 0:
                rets.append(float(r["close"]) / start_px - 1.0)
        return sum(rets) / len(rets) if rets else 0.0

    # ─────────────────────────────────────────────────────────────────────────
    # B3 - Weekly Momentum
    # ─────────────────────────────────────────────────────────────────────────

    async def _rebalance(
        self, conn, current_date: date, symbols: list[str]
    ) -> None:
        """选 top-N ret_20d 股票，记录当前价格作为新的调仓基准。"""
        rows = await conn.fetch(
            """
            WITH ranked AS (
                SELECT symbol, close, trade_date,
                       ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY trade_date DESC) AS rn
                FROM market_bars_daily
                WHERE symbol = ANY($1)
                  AND trade_date <= $2
                  AND available_date <= $2
                  AND close IS NOT NULL AND close > 0
            ),
            latest AS (SELECT symbol, close AS c_now  FROM ranked WHERE rn = 1),
            lag20  AS (SELECT symbol, close AS c_20d  FROM ranked WHERE rn = 21)
            SELECT l.symbol, l.c_now,
                   CASE WHEN g.c_20d > 0
                        THEN (l.c_now / g.c_20d - 1)
                        ELSE NULL END AS ret_20d
            FROM latest l
            LEFT JOIN lag20 g USING (symbol)
            WHERE g.c_20d IS NOT NULL
            ORDER BY ret_20d DESC
            LIMIT $3
            """,
            symbols, current_date, _TOP_N,
        )

        if rows:
            self._mom_symbols = [r["symbol"] for r in rows]
            self._mom_rebal_prices = {r["symbol"]: float(r["c_now"]) for r in rows}
            self._mom_rebal_date = current_date
            log.debug(
                f"benchmarks[{self.market}] B3 rebal {current_date}: "
                f"{self._mom_symbols}"
            )
        else:
            log.warning(
                f"benchmarks[{self.market}] B3: no valid momentum stocks at {current_date}"
            )

    async def _mom_value_now(self, conn, current_date: date) -> float:
        """计算当前动量组合相对上次调仓的价值倍数，用于链式复合。"""
        if not self._mom_symbols:
            return self._mom_value
        rows = await conn.fetch(
            """
            SELECT DISTINCT ON (symbol) symbol, close
            FROM market_bars_daily
            WHERE symbol = ANY($1)
              AND trade_date <= $2
              AND available_date <= $2
              AND close IS NOT NULL AND close > 0
            ORDER BY symbol, trade_date DESC
            """,
            self._mom_symbols, current_date,
        )
        ratios = []
        for r in rows:
            rebal_px = self._mom_rebal_prices.get(r["symbol"])
            if rebal_px and rebal_px > 0:
                ratios.append(float(r["close"]) / rebal_px)
        if not ratios:
            return self._mom_value
        return self._mom_value * (sum(ratios) / len(ratios))

    async def _calc_mom(self, conn, current_date: date) -> float:
        """返回动量组合当日累计收益率。"""
        if not self._mom_symbols:
            return self._mom_value - 1.0
        rows = await conn.fetch(
            """
            SELECT DISTINCT ON (symbol) symbol, close
            FROM market_bars_daily
            WHERE symbol = ANY($1)
              AND trade_date <= $2
              AND available_date <= $2
              AND close IS NOT NULL AND close > 0
            ORDER BY symbol, trade_date DESC
            """,
            self._mom_symbols, current_date,
        )
        ratios = []
        for r in rows:
            rebal_px = self._mom_rebal_prices.get(r["symbol"])
            if rebal_px and rebal_px > 0:
                ratios.append(float(r["close"]) / rebal_px)
        if not ratios:
            return self._mom_value - 1.0
        current_val = self._mom_value * (sum(ratios) / len(ratios))
        return current_val - 1.0

    def _is_new_week(self, current_date: date) -> bool:
        """是否进入新的 ISO 周（用于触发 B3 调仓）。"""
        if self._mom_rebal_date is None:
            return False
        return (
            current_date.isocalendar()[:2]
            != self._mom_rebal_date.isocalendar()[:2]
        )
