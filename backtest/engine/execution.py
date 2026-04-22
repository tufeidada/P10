"""
backtest/engine/execution.py — T+1 成交执行器

职责：
  - execute_pending_exits: 以 T 日开盘价执行上一日队列的信号平仓（方向翻转/超时）
  - execute_pending_entries: 以 T 日开盘价执行上一日队列的建仓信号
  - check_intraday_exits: 用 T 日盘中 high/low 检测 stop_loss / target_hit，以精确价格平仓
  - queue_signal_exits: 基于当日 check_exit 结果，将 direction_flip/timeout 加入下日队列
  - queue_entries: 过滤 bullish judgments，通过行业/流动性检查后加入下日建仓队列

PIT 约束：
  - execute_*: get_open_price(symbol, exec_date) 是 PITDataLoader 唯一合法访问"未来"入口
  - check_intraday_exits: get_bars(include_today=True) 获取当日 OHLCV，不违反 PIT
    （止损/目标检查本身在收盘后批处理，允许看当日完整蜡烛）
  - queue_entries: get_bars 默认截至 prev_trade_date，流动性检查基于 T-1 成交额
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Optional

from backtest.engine.portfolio import Portfolio, Position, Trade
from backtest.engine.rules import (
    check_exit,
    check_industry_concentration,
    check_liquidity,
    calc_position_size,
    stop_price,
    target_price_of,
)
from backtest.pit_loader import PITDataLoader

log = logging.getLogger(__name__)

# (Position, exit_reason, Portfolio)
PendingExit  = tuple[Position, str, Portfolio]
# (Judgment, Portfolio, max_position_pct, industry_str)
PendingEntry = tuple[object, Portfolio, float, str]


class TradeExecutor:
    """T+1 成交执行器。每个 BacktestEngine 持有一个实例。"""

    def __init__(self, loader: PITDataLoader) -> None:
        self.loader = loader

    # ─────────────────────────────────────────────────────────────────────────
    # 执行上日队列
    # ─────────────────────────────────────────────────────────────────────────

    async def execute_pending_exits(
        self,
        pending: list[PendingExit],
        exec_date: date,
    ) -> list[Trade]:
        """以 exec_date 开盘价执行信号平仓（direction_flip / timeout）。"""
        trades: list[Trade] = []
        for pos, reason, portfolio in pending:
            if not portfolio.has_position(pos.symbol):
                continue  # 可能已被当日其他逻辑平仓
            try:
                open_px = await self.loader.get_open_price(pos.symbol, exec_date)
                if open_px is None:
                    log.warning(
                        f"execution: no open price for {pos.symbol} on {exec_date}, "
                        f"skip {reason} exit"
                    )
                    continue
                trade = portfolio.close_position(pos, open_px, exec_date, reason)
                trades.append(trade)
                log.info(
                    f"execution: exit {pos.symbol} reason={reason} "
                    f"price={open_px:.4f} pnl={trade.pnl:.2f}"
                )
            except Exception as exc:
                log.error(f"execution: exit failed {pos.symbol}: {exc}", exc_info=True)
        return trades

    async def execute_pending_entries(
        self,
        pending: list[PendingEntry],
        exec_date: date,
    ) -> list[Position]:
        """以 exec_date 开盘价建仓。"""
        positions: list[Position] = []
        for j, portfolio, max_pct, industry in pending:
            if portfolio.has_position(j.symbol):
                log.debug(f"execution: already have {j.symbol}, skip entry")
                continue
            try:
                open_px = await self.loader.get_open_price(j.symbol, exec_date)
                if open_px is None:
                    log.warning(
                        f"execution: no open price for {j.symbol} on {exec_date}, "
                        f"skip entry"
                    )
                    continue
                shares = calc_position_size(
                    portfolio, open_px, j.stop_loss, j.confidence, max_pct
                )
                if shares <= 0:
                    log.debug(f"execution: calc_size=0 for {j.symbol}, skip")
                    continue
                pos = portfolio.open_position(
                    symbol=j.symbol,
                    market=j.market,
                    industry=industry,
                    entry_date=exec_date,
                    entry_price=open_px,
                    shares=shares,
                    stop_loss=j.stop_loss,
                    target_price=j.target_price,
                    trigger_judgment_id=getattr(j, "id", None),
                )
                if pos:
                    positions.append(pos)
                    log.info(
                        f"execution: entry {j.symbol} {shares}股 @ {open_px:.4f} "
                        f"sl={j.stop_loss} tp={j.target_price}"
                    )
            except Exception as exc:
                log.error(f"execution: entry failed {j.symbol}: {exc}", exc_info=True)
        return positions

    # ─────────────────────────────────────────────────────────────────────────
    # 当日盘中止损 / 达目标检查
    # ─────────────────────────────────────────────────────────────────────────

    async def check_intraday_exits(
        self,
        portfolio: Portfolio,
        current_date: date,
    ) -> list[Trade]:
        """
        用当日完整蜡烛（high/low）检查是否触发 stop_loss 或 target_price。

        ─ 顺序：止损优先于达标（同日 low <= sl AND high >= tp 时，先止损）
        ─ 成交价：精确止损价 / 目标价（非当日开盘或收盘）
        ─ 调用 get_bars(include_today=True) 获取当日 OHLCV（收盘后批处理，不违反 PIT）
        """
        trades: list[Trade] = []
        for pos in list(portfolio.positions):  # list() 防止迭代中删除
            try:
                bars = await self.loader.get_bars(pos.symbol, lookback_days=1, include_today=True)
                if bars.empty:
                    continue

                row   = bars.iloc[-1]
                close = float(row["close"]) if row["close"] is not None else None
                low   = float(row["low"])   if row.get("low")  is not None else None
                high  = float(row["high"])  if row.get("high") is not None else None

                # 用今日收盘更新持仓市值（无论是否触发平仓）
                if close and close > 0:
                    pos.update(close)

                if low is None or high is None:
                    continue

                sl = stop_price(pos)
                tp = target_price_of(pos)

                if low <= sl:
                    trade = portfolio.close_position(pos, sl, current_date, "stop_loss")
                    trades.append(trade)
                    log.info(
                        f"execution: stop_loss {pos.symbol} sl={sl:.4f} "
                        f"day_low={low:.4f} pnl={trade.pnl:.2f}"
                    )
                elif high >= tp:
                    trade = portfolio.close_position(pos, tp, current_date, "target_hit")
                    trades.append(trade)
                    log.info(
                        f"execution: target_hit {pos.symbol} tp={tp:.4f} "
                        f"day_high={high:.4f} pnl={trade.pnl:.2f}"
                    )
            except Exception as exc:
                log.error(
                    f"execution: intraday check failed {pos.symbol}: {exc}", exc_info=True
                )
        return trades

    # ─────────────────────────────────────────────────────────────────────────
    # 构建下日队列
    # ─────────────────────────────────────────────────────────────────────────

    async def queue_signal_exits(
        self,
        portfolio: Portfolio,
        judgments: list,
        current_date: date,
    ) -> list[PendingExit]:
        """
        对当前持仓检查 direction_flip / timeout，返回需在下日开盘平仓的队列。

        注意：此时 position.current_price 已更新为当日收盘价（在 check_intraday_exits 中完成）。
        """
        queued: list[PendingExit] = []
        for pos in portfolio.positions:
            try:
                reason = check_exit(pos, judgments, current_date)
                if reason in ("direction_flip", "timeout"):
                    queued.append((pos, reason, portfolio))
                    log.info(f"execution: queue exit {pos.symbol} reason={reason}")
            except Exception as exc:
                log.error(
                    f"execution: queue_signal_exits failed {pos.symbol}: {exc}",
                    exc_info=True,
                )
        return queued

    async def queue_entries(
        self,
        judgments: list,
        portfolio: Portfolio,
        max_position_pct: float,
        industry_map: dict[str, str],
    ) -> list[PendingEntry]:
        """
        过滤 bullish judgments，通过行业集中度 + 流动性检查后加入建仓队列。

        Args:
            judgments:        当日所有 judgment。
            portfolio:        目标账户（CN 或 US）。
            max_position_pct: Regime 参数，单只最大仓位比例。
            industry_map:     {symbol: industry_str}，来自 watchlist 配置。
        """
        queued: list[PendingEntry] = []
        candidates = sorted(
            [j for j in judgments if j.direction == "bullish" and j.confidence > 0.55],
            key=lambda j: (j.composite_score or 0),
            reverse=True,
        )
        for j in candidates:
            if portfolio.has_position(j.symbol):
                log.debug(f"execution: already hold {j.symbol}, skip entry")
                continue

            industry = industry_map.get(j.symbol, "default")

            # 行业集中度检查
            if not check_industry_concentration(portfolio, industry):
                log.debug(f"execution: industry over-concentrated for {j.symbol}")
                continue

            # 流动性检查（用 T-1 成交额，get_bars 默认截至 prev_trade_date）
            try:
                bars_df = await self.loader.get_bars(j.symbol, lookback_days=1)
                planned = portfolio.value * max_position_pct * min(j.confidence, 1.0)
                if not check_liquidity(bars_df, planned):
                    log.debug(f"execution: liquidity fail {j.symbol}")
                    continue
            except Exception as exc:
                log.warning(f"execution: liquidity check error {j.symbol}: {exc}")
                continue

            queued.append((j, portfolio, max_position_pct, industry))
            log.info(
                f"execution: queue entry {j.symbol} conf={j.confidence:.2f} "
                f"score={j.composite_score}"
            )

        return queued
