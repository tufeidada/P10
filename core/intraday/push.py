"""
盘中信号推送，带频率控制。

将 buy/sell 信号格式化为 Telegram HTML 消息推送，并通过内存状态控制
同标的同方向 30 分钟冷却、全局每小时最多 3 条。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

from .signal_detector import IntradaySignal

logger = structlog.get_logger(__name__)

# ── 频率控制常量 ──────────────────────────────────────────────────────────────
_SYMBOL_COOLDOWN_MINUTES = 30   # 同标的+方向冷却时间（分钟）
_GLOBAL_HOURLY_LIMIT = 3        # 全局每小时最大推送数


def _now() -> datetime:
    """返回带时区的当前时间（UTC）。"""
    return datetime.now(tz=timezone.utc)


class SignalPusher:
    """盘中信号推送，带频率控制。

    Rate limits:
    - Same symbol + type: 30 minutes between pushes
    - Global: max 3 per hour

    Usage:
        pusher = SignalPusher()
        pushed = await pusher.push_buy_signal("600519.SH", "贵州茅台", signal, judgment)
    """

    # 类级共享状态（单进程内全局生效）
    _last_push: dict[str, datetime] = {}     # key: f"{symbol}_{type}"
    _hourly_count: list[datetime] = []       # 最近 1 小时内推送的时间戳列表

    async def push_buy_signal(
        self,
        symbol: str,
        name: str | None,
        signal: IntradaySignal,
        judgment: dict[str, Any] | None,
    ) -> bool:
        """格式化并推送买入信号。

        Format (HTML):
            🟢 买入信号 | {symbol} {name}
            ━━━━━━━━━━━━━━━
            强度: {strong→🔴强|moderate→🟡中}  时间: {HH:MM}
            当前价: {price}
            触发: {trigger_rule}
            基础判断: 短期看多 ({confidence:.0%}) [{judgment_date}]
            建议入场: {entry_low}-{entry_high} | 止损: {stop}

        Args:
            symbol: 股票代码。
            name: 股票名称，可为 None。
            signal: 盘中买入信号。
            judgment: 关联的基础判断字典，可为 None。

        Returns:
            True 如推送成功，False 如频率限制或发送失败。
        """
        if self._is_rate_limited(symbol, "buy"):
            logger.debug("buy_signal_rate_limited", symbol=symbol)
            return False

        display = f"{symbol} {name}" if name else symbol
        strength_label = "🔴强" if signal.strength == "strong" else "🟡中"
        time_str = _signal_time_str(signal)
        price_str = _fmt_price(signal.price)

        lines: list[str] = [
            f"🟢 <b>买入信号</b> | {display}",
            "━━━━━━━━━━━━━━━",
            f"强度: {strength_label}  时间: {time_str}",
            f"当前价: <b>{price_str}</b>",
            f"触发: {signal.trigger_rule}",
        ]

        # 基础判断信息
        if judgment:
            confidence = judgment.get("confidence")
            judgment_date = judgment.get("judgment_date", "")
            confidence_text = f"{float(confidence):.0%}" if confidence else "N/A"
            lines.append(
                f"基础判断: 短期看多 ({confidence_text}) [{judgment_date}]"
            )

            # 入场区间
            entry_low = judgment.get("entry_zone_low")
            entry_high = judgment.get("entry_zone_high")
            stop_loss = signal.stop_price or judgment.get("stop_loss")
            entry_parts: list[str] = []
            if entry_low is not None and entry_high is not None:
                entry_parts.append(
                    f"建议入场: {_fmt_price(float(entry_low))}-{_fmt_price(float(entry_high))}"
                )
            if stop_loss is not None:
                entry_parts.append(f"止损: {_fmt_price(float(stop_loss))}")
            if entry_parts:
                lines.append(" | ".join(entry_parts))
        elif signal.stop_price is not None:
            lines.append(f"止损: {_fmt_price(signal.stop_price)}")

        text = "\n".join(lines)
        ok = await _send_html(text)
        if ok:
            self._record_push(symbol, "buy")
        return ok

    async def push_sell_signal(
        self,
        symbol: str,
        name: str | None,
        signal: IntradaySignal,
        position: dict[str, Any] | None,
    ) -> bool:
        """格式化并推送卖出信号。

        Format (HTML):
            🔴 卖出信号 | {symbol} {name}
            ━━━━━━━━━━━━━━━
            强度: {strong/moderate}  时间: {HH:MM}
            当前价: {price} | 止损位: {stop}
            触发: {trigger_rule}
            持仓成本: {cost} | 浮亏/盈: {pnl:.1%}
            ⚠️ 距止损仅 {dist:.1%}

        Args:
            symbol: 股票代码。
            name: 股票名称，可为 None。
            signal: 盘中卖出信号。
            position: 当前持仓字典，可为 None。

        Returns:
            True 如推送成功，False 如频率限制或发送失败。
        """
        if self._is_rate_limited(symbol, "sell"):
            logger.debug("sell_signal_rate_limited", symbol=symbol)
            return False

        display = f"{symbol} {name}" if name else symbol
        strength_label = "强" if signal.strength == "strong" else "中"
        time_str = _signal_time_str(signal)
        price_str = _fmt_price(signal.price)
        stop_str = _fmt_price(signal.stop_price)

        lines: list[str] = [
            f"🔴 <b>卖出信号</b> | {display}",
            "━━━━━━━━━━━━━━━",
            f"强度: {strength_label}  时间: {time_str}",
            f"当前价: <b>{price_str}</b> | 止损位: {stop_str}",
            f"触发: {signal.trigger_rule}",
        ]

        # 持仓信息
        if position:
            entry_price = position.get("entry_price")
            if entry_price is not None and signal.price:
                cost_f = float(entry_price)
                if cost_f > 0:
                    pnl_pct = (signal.price - cost_f) / cost_f
                    pnl_emoji = "🟢" if pnl_pct >= 0 else "🔴"
                    lines.append(
                        f"持仓成本: {_fmt_price(cost_f)} | "
                        f"浮{'盈' if pnl_pct >= 0 else '亏'}: {pnl_emoji}{pnl_pct:+.1%}"
                    )
                    # 距止损距离
                    if signal.stop_price is not None and cost_f > 0:
                        stop_dist = (signal.price - signal.stop_price) / cost_f
                        if stop_dist < 0.03:
                            lines.append(f"⚠️ 距止损仅 {abs(stop_dist):.1%}")

        text = "\n".join(lines)
        ok = await _send_html(text)
        if ok:
            self._record_push(symbol, "sell")
        return ok

    # ------------------------------------------------------------------
    # 频率控制
    # ------------------------------------------------------------------

    def _is_rate_limited(self, symbol: str, signal_type: str) -> bool:
        """检查是否触发频率限制。

        规则：
        1. 同 symbol + type 30 分钟内不重复推送。
        2. 全局每小时最多 3 条。

        Args:
            symbol: 股票代码。
            signal_type: 信号方向 'buy' | 'sell'。

        Returns:
            True 如受到限制，否则 False。
        """
        now = _now()
        key = f"{symbol}_{signal_type}"

        # 规则 1：同标的冷却
        last = self._last_push.get(key)
        if last is not None:
            cooldown = timedelta(minutes=_SYMBOL_COOLDOWN_MINUTES)
            if now - last < cooldown:
                return True

        # 规则 2：全局每小时限额
        cutoff = now - timedelta(hours=1)
        recent = [t for t in self._hourly_count if t > cutoff]
        self._hourly_count = recent  # 清理过期记录
        if len(recent) >= _GLOBAL_HOURLY_LIMIT:
            return True

        return False

    def _record_push(self, symbol: str, signal_type: str) -> None:
        """记录此次推送，用于后续频率控制。

        Args:
            symbol: 股票代码。
            signal_type: 信号方向 'buy' | 'sell'。
        """
        now = _now()
        key = f"{symbol}_{signal_type}"
        self._last_push[key] = now
        self._hourly_count.append(now)


# ------------------------------------------------------------------
# 内部工具函数
# ------------------------------------------------------------------

async def _send_html(text: str) -> bool:
    """通过 TelegramPusher 发送 HTML 消息。

    Args:
        text: HTML 格式消息内容。

    Returns:
        True 如发送成功，否则 False。
    """
    try:
        from bot.telegram_bot import TelegramPusher

        pusher = TelegramPusher()
        return await pusher.send_html(text)
    except Exception as e:
        logger.error("signal_push_send_error", error=str(e))
        return False


def _fmt_price(price: float | None) -> str:
    """格式化价格为字符串。

    Args:
        price: 价格数值，可为 None。

    Returns:
        格式化字符串，如 '1705.00' 或 'N/A'。
    """
    if price is None:
        return "N/A"
    return f"{price:.2f}"


def _signal_time_str(signal: IntradaySignal) -> str:
    """提取信号触发时间的 HH:MM 字符串。

    Args:
        signal: 信号对象。

    Returns:
        如 '10:15'，无法解析时返回 '--:--'。
    """
    try:
        # IntradaySignal 不含 signal_time 字段，
        # 使用 trigger_detail 中的 calc_time（如存在），否则用当前时间。
        calc_time_str = signal.trigger_detail.get("calc_time")
        if calc_time_str:
            dt = datetime.fromisoformat(str(calc_time_str))
            # 转为本地（Asia/Shanghai +8）
            local = dt.astimezone()
            return local.strftime("%H:%M")
    except Exception:
        pass
    return _now().astimezone().strftime("%H:%M")
