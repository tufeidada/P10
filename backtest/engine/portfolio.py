"""
backtest/engine/portfolio.py — 虚拟账户

管理现金 + 持仓的生命周期。纯内存对象，不直接访问数据库。
engine.py 在每日结束时将快照写入 DB（backtest_portfolio_daily / backtest_positions）。

成本模型（单边，买入和卖出各收一次）:
  A 股 (CN): 0.2%  = 佣金 0.03% + 印花税 0.1% + 过户费+滑点折算 ~0.07%
  美股 (US): 0.1%  = 佣金+滑点折算

整手规则:
  A 股: 整百股 (100 股/手)
  美股: 整 1 股
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional

log = logging.getLogger(__name__)

# 单边佣金率（含滑点折算，单边各收一次）
# CN 0.25% = 佣金 0.03% + 印花税 0.1% + 过户费+滑点折算 ~0.12%
# US 0.15% = 佣金+滑点折算（中小盘冲击成本高于大盘）
_COMM_CN = 0.0025   # 0.25%
_COMM_US = 0.0015   # 0.15%

# 整手大小
_LOT_CN = 100
_LOT_US = 1


# ═════════════════════════════════════════════════════════════════════════════
# 持仓记录
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class Position:
    symbol:               str
    market:               str
    entry_date:           date
    entry_price:          float
    shares:               int
    stop_loss:            Optional[float]   # None 时 rules 用固定 7% 止损
    target_price:         Optional[float]   # None 时 rules 用固定 15% 目标
    industry:             str              # 用于行业集中度检查
    trigger_judgment_id:  Optional[int]    # backtest_judgments.id，DB 写入后回填
    entry_commission:     float            # 建仓佣金（供平仓时计算完整 PnL）
    current_price:        float = 0.0      # 每日收盘更新
    market_value:         float = 0.0
    unrealized_pnl:       float = 0.0
    unrealized_pnl_pct:   float = 0.0

    def __post_init__(self):
        self.current_price = self.entry_price
        self.market_value  = self.entry_price * self.shares

    @property
    def entry_cost(self) -> float:
        return self.entry_price * self.shares + self.entry_commission

    @property
    def days_held(self) -> int:
        from datetime import date as _date
        return 0  # 由 engine 传入 current_date 计算，或 Trade 在平仓时记录

    def update(self, price: float) -> None:
        self.current_price    = price
        self.market_value     = price * self.shares
        self.unrealized_pnl   = self.market_value - self.entry_cost
        self.unrealized_pnl_pct = self.unrealized_pnl / self.entry_cost if self.entry_cost else 0.0

    def __str__(self) -> str:
        return (
            f"Position({self.symbol} | {self.shares}股 @ {self.entry_price:.2f} "
            f"→ {self.current_price:.2f} | pnl={self.unrealized_pnl_pct*100:.1f}%)"
        )


# ═════════════════════════════════════════════════════════════════════════════
# 已平仓交易记录
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class Trade:
    symbol:               str
    market:               str
    entry_date:           date
    entry_price:          float
    exit_date:            date
    exit_price:           float
    shares:               int
    pnl:                  float        # 含双边佣金
    pnl_pct:              float        # pnl / entry_cost
    exit_reason:          str          # stop_loss | target_hit | direction_flip | timeout | manual
    days_held:            int
    industry:             str
    trigger_judgment_id:  Optional[int] = None

    def __str__(self) -> str:
        return (
            f"Trade({self.symbol} | {self.days_held}d | "
            f"{self.exit_reason} | pnl={self.pnl_pct*100:.1f}%)"
        )


# ═════════════════════════════════════════════════════════════════════════════
# 投资组合
# ═════════════════════════════════════════════════════════════════════════════

class Portfolio:
    """
    虚拟投资账户。

    Args:
        initial_cash: 初始资金（元）。
        market:       "CN" 或 "US"。
    """

    def __init__(self, initial_cash: float, market: str) -> None:
        self.market:        str           = market
        self.cash:          float         = initial_cash
        self.initial_cash:  float         = initial_cash
        self.positions:     list[Position] = []
        self.closed_trades: list[Trade]   = []
        self._comm_rate:    float         = _COMM_CN if market == "CN" else _COMM_US
        self._lot:          int           = _LOT_CN  if market == "CN" else _LOT_US

    # ── 属性 ──────────────────────────────────────────────────────────────────

    @property
    def positions_value(self) -> float:
        return sum(p.market_value for p in self.positions)

    @property
    def value(self) -> float:
        return self.cash + self.positions_value

    @property
    def position_pct(self) -> float:
        """持仓市值占总资产比例（0-1）。"""
        v = self.value
        return self.positions_value / v if v > 0 else 0.0

    @property
    def position_count(self) -> int:
        return len(self.positions)

    # ── 查询 ──────────────────────────────────────────────────────────────────

    def has_position(self, symbol: str) -> bool:
        return any(p.symbol == symbol for p in self.positions)

    def get_position(self, symbol: str) -> Optional[Position]:
        return next((p for p in self.positions if p.symbol == symbol), None)

    def industry_exposure(self, industry: str) -> float:
        """某行业当前市值占总资产比例（0-1）。"""
        ind_val = sum(p.market_value for p in self.positions if p.industry == industry)
        v = self.value
        return ind_val / v if v > 0 else 0.0

    # ── 建仓 ──────────────────────────────────────────────────────────────────

    def open_position(
        self,
        symbol:              str,
        market:              str,
        industry:            str,
        entry_date:          date,
        entry_price:         float,
        shares:              int,
        stop_loss:           Optional[float],
        target_price:        Optional[float],
        trigger_judgment_id: Optional[int] = None,
    ) -> Optional[Position]:
        """
        建仓。cash 不足时按实际可用资金向下取整到整手，再尝试建仓。
        shares <= 0 或 cash 不够 1 手时返回 None。

        Returns:
            新建的 Position，或 None（资金不足 / 无效参数）。
        """
        if shares <= 0:
            return None

        commission    = round(entry_price * shares * self._comm_rate, 2)
        total_cost    = entry_price * shares + commission

        # cash 不足时缩减到最大可建手数
        if total_cost > self.cash:
            max_shares = int(self.cash / (entry_price * (1 + self._comm_rate)))
            shares     = (max_shares // self._lot) * self._lot
            if shares <= 0:
                log.warning(f"portfolio: 资金不足以建仓 {symbol}，跳过")
                return None
            commission = round(entry_price * shares * self._comm_rate, 2)
            total_cost = entry_price * shares + commission

        self.cash = round(self.cash - total_cost, 4)

        pos = Position(
            symbol=symbol, market=market, industry=industry,
            entry_date=entry_date, entry_price=entry_price, shares=shares,
            stop_loss=stop_loss, target_price=target_price,
            trigger_judgment_id=trigger_judgment_id,
            entry_commission=commission,
        )
        self.positions.append(pos)
        log.info(
            f"portfolio.open  {symbol} {shares}股 @ {entry_price:.4f} "
            f"comm={commission:.2f} cost={total_cost:.2f} cash_after={self.cash:.2f}"
        )
        return pos

    # ── 平仓 ──────────────────────────────────────────────────────────────────

    def close_position(
        self,
        position:   Position,
        exit_price: float,
        exit_date:  date,
        reason:     str,
    ) -> Trade:
        """
        平仓。计算完整 PnL（含建仓 + 平仓双边佣金）。

        Returns:
            Trade 记录（同时添加到 closed_trades，Position 从 positions 移除）。
        """
        exit_commission = round(exit_price * position.shares * self._comm_rate, 2)
        proceeds        = exit_price * position.shares - exit_commission

        self.cash = round(self.cash + proceeds, 4)

        pnl     = round(proceeds - (position.entry_price * position.shares + position.entry_commission), 4)
        pnl_pct = round(pnl / position.entry_cost, 6) if position.entry_cost else 0.0
        days    = (exit_date - position.entry_date).days

        trade = Trade(
            symbol=position.symbol, market=position.market,
            entry_date=position.entry_date, entry_price=position.entry_price,
            exit_date=exit_date, exit_price=exit_price,
            shares=position.shares,
            pnl=pnl, pnl_pct=pnl_pct,
            exit_reason=reason, days_held=days,
            industry=position.industry,
            trigger_judgment_id=position.trigger_judgment_id,
        )
        self.closed_trades.append(trade)
        self.positions.remove(position)

        log.info(
            f"portfolio.close {position.symbol} {position.shares}股 @ {exit_price:.4f} "
            f"reason={reason} pnl={pnl:.2f} ({pnl_pct*100:.2f}%) cash_after={self.cash:.2f}"
        )
        return trade

    # ── 每日更新 ──────────────────────────────────────────────────────────────

    def update_positions_value(self, price_map: dict[str, float]) -> None:
        """
        每日收盘后用最新价格更新所有持仓的市值和浮动盈亏。
        price_map: {symbol: adj_close_price}
        """
        for p in self.positions:
            price = price_map.get(p.symbol)
            if price and price > 0:
                p.update(price)

    # ── 调试 ─────────────────────────────────────────────────────────────────

    def summary(self) -> dict[str, Any]:
        return {
            "market":          self.market,
            "cash":            round(self.cash, 2),
            "positions_value": round(self.positions_value, 2),
            "total_value":     round(self.value, 2),
            "position_count":  self.position_count,
            "position_pct":    round(self.position_pct, 4),
            "return_pct":      round((self.value / self.initial_cash - 1) * 100, 4),
            "closed_trades":   len(self.closed_trades),
        }

    def __str__(self) -> str:
        s = self.summary()
        return (
            f"Portfolio({self.market} | cash={s['cash']:,.0f} "
            f"pos={s['positions_value']:,.0f} total={s['total_value']:,.0f} "
            f"({s['return_pct']:+.2f}%) | {s['position_count']}持仓 "
            f"{s['closed_trades']}已平)"
        )
