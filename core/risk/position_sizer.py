"""
仓位计算器 — 基于止损距离反算仓位大小。

公式：
    risk_per_share = |entry - stop|
    max_loss       = account_value * max_risk_pct
    raw_shares     = int(max_loss / risk_per_share)

然后按手数取整、按最大仓位占比封顶。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class PositionSizing:
    """仓位计算结果。

    Attributes:
        shares: 建议买入股数。
        position_value: 持仓市值。
        position_pct: 占账户比例（0.0-1.0）。
        risk_amount: 最大亏损金额（按止损价计算）。
        risk_pct: 最大亏损占账户比例（0.0-1.0）。
        stop_price: 止损价格。
        entry_price: 建仓价格。
        risk_reward_ratio: 风险回报比（有 target_price 时才有值）。
    """

    shares: int
    position_value: float
    position_pct: float
    risk_amount: float
    risk_pct: float
    stop_price: float
    entry_price: float
    risk_reward_ratio: float | None


class PositionSizer:
    """基于止损距离的仓位计算器。

    Args:
        account_value: 账户总市值，单位与股价相同（默认 100,000）。
    """

    def __init__(self, account_value: float = 100_000.0) -> None:
        self.account_value = account_value

    def calc_position(
        self,
        entry_price: float,
        stop_price: float,
        target_price: float | None = None,
        max_risk_pct: float = 0.02,
        max_position_pct: float = 0.80,
        lot_size: int = 100,
    ) -> PositionSizing:
        """根据止损距离反算建议仓位大小。

        Args:
            entry_price: 拟建仓价格（通常为当前收盘价）。
            stop_price: 止损价格。
            target_price: 目标价格，用于计算风险回报比；None 则不计算。
            max_risk_pct: 单笔交易最大亏损占账户的比例，默认 2%。
            max_position_pct: 仓位上限（由 regime 传入），默认 80%。
            lot_size: 每手股数，A 股为 100，US 股为 1。

        Returns:
            PositionSizing 计算结果。

        Raises:
            ValueError: entry_price 或 stop_price 不合法时。
        """
        if entry_price <= 0:
            raise ValueError(f"entry_price must be positive, got {entry_price}")
        if stop_price <= 0:
            raise ValueError(f"stop_price must be positive, got {stop_price}")
        if entry_price == stop_price:
            raise ValueError("entry_price and stop_price must differ")

        risk_per_share = abs(entry_price - stop_price)

        # 1. 按最大亏损计算原始股数
        max_loss = self.account_value * max_risk_pct
        raw_shares = int(max_loss / risk_per_share)

        # 2. 按手数向下取整
        if lot_size > 1:
            shares = math.floor(raw_shares / lot_size) * lot_size
        else:
            shares = raw_shares

        # 3. 按最大仓位占比封顶
        max_shares_by_pos = math.floor(
            (self.account_value * max_position_pct) / entry_price
        )
        if lot_size > 1:
            max_shares_by_pos = math.floor(max_shares_by_pos / lot_size) * lot_size

        shares = min(shares, max_shares_by_pos)
        shares = max(shares, 0)

        position_value = shares * entry_price
        position_pct = position_value / self.account_value if self.account_value > 0 else 0.0
        risk_amount = shares * risk_per_share
        risk_pct = risk_amount / self.account_value if self.account_value > 0 else 0.0

        # 4. 风险回报比
        risk_reward_ratio: float | None = None
        if target_price is not None and risk_per_share > 0:
            reward = abs(target_price - entry_price)
            risk_reward_ratio = round(reward / risk_per_share, 2)

        result = PositionSizing(
            shares=shares,
            position_value=round(position_value, 2),
            position_pct=round(position_pct, 4),
            risk_amount=round(risk_amount, 2),
            risk_pct=round(risk_pct, 4),
            stop_price=stop_price,
            entry_price=entry_price,
            risk_reward_ratio=risk_reward_ratio,
        )

        logger.debug(
            "position_sized",
            entry_price=entry_price,
            stop_price=stop_price,
            shares=shares,
            position_pct=round(position_pct, 4),
            risk_pct=round(risk_pct, 4),
        )
        return result

    def format_for_display(self, sizing: PositionSizing) -> str:
        """将仓位建议格式化为 Telegram HTML 字符串。

        Args:
            sizing: calc_position 的返回值。

        Returns:
            HTML 格式字符串，用于 TelegramPusher.send_html()。
        """
        rrr_line = ""
        if sizing.risk_reward_ratio is not None:
            rrr_line = f"\n风险回报比: {sizing.risk_reward_ratio:.1f}x"

        return (
            f"📐 <b>仓位建议</b> (账户 ¥{self.account_value:,.0f})\n"
            f"建议: {sizing.shares} 股 "
            f"({sizing.position_pct:.0%} 仓位 = ¥{sizing.position_value:,.0f})\n"
            f"风控: 最大亏损 ¥{sizing.risk_amount:,.0f} ({sizing.risk_pct:.1%})"
            f"{rrr_line}"
        )
