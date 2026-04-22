"""
盘中判断矫正引擎。

对比最近的 judgment 方向和盘中因子状态，
发现强烈信号矛盾时写入 calibrations 表并推送 Telegram 通知。
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from db.connection import db_execute, db_query_one
from .factors import IntradayFactors

logger = structlog.get_logger(__name__)

# ── 矫正触发阈值 ───────────────────────────────────────────────────────────────
# bullish 判断的反向警戒线
_BULLISH_MOM_THRESHOLD: float = -1.5      # momentum_1h 低于此值 → 强烈下行动量
_BULLISH_SUPPORT_THRESHOLD: float = -0.8  # support_distance 低于此值 → 跌破支撑
_BULLISH_VOL_THRESHOLD: float = 2.0       # volume_ratio_15m 高于此值 → 高卖量放大

# bearish 判断的反向警戒线
_BEARISH_MOM_THRESHOLD: float = 1.5       # momentum_1h 高于此值 → 强烈上行动量
_BEARISH_RANGE_THRESHOLD: float = 0.8     # price_vs_day_range 高于此值 → 日内高位
_BEARISH_VOL_THRESHOLD: float = 2.0       # volume_ratio_15m 高于此值 → 高买量放大


class IntradayCalibrator:
    """盘中判断矫正引擎。

    对比最近的 judgment 方向和盘中因子状态，
    发现强烈信号矛盾时写入 calibrations 表并推送通知。
    """

    async def check_and_calibrate(
        self,
        symbol: str,
        market: str,
        factors: IntradayFactors,
    ) -> bool:
        """检查是否需要矫正，并在条件满足时执行矫正。

        矫正触发条件：
        - bullish 判断但出现以下 3 条件中任意 2 条：
            1. momentum_1h < -1.5（强烈下行动量）
            2. support_distance < -0.8（跌破支撑）
            3. volume_ratio_15m > 2.0（高卖量放大）
        - bearish 判断但出现以下 3 条件中任意 2 条：
            1. momentum_1h > 1.5（强烈上行动量）
            2. price_vs_day_range > 0.8（日内高位）
            3. volume_ratio_15m > 2.0（高买量放大）

        Args:
            symbol: 股票代码。
            market: 市场标识，'CN' 或 'US'。
            factors: 实时盘中因子。

        Returns:
            True 如写入了矫正记录，否则 False。
        """
        log = logger.bind(symbol=symbol, market=market, module="calibrator")

        judgment = await self._get_latest_judgment(symbol)
        if judgment is None:
            log.debug("calibrate_skip_no_judgment")
            return False

        direction: str = judgment.get("direction", "neutral")
        judgment_id: int | None = judgment.get("id")

        if direction == "bullish":
            triggered, reason = _check_bullish_contradiction(factors)
        elif direction == "bearish":
            triggered, reason = _check_bearish_contradiction(factors)
        else:
            # neutral 方向无需矫正
            return False

        if not triggered:
            return False

        log.info(
            "calibration_triggered",
            judgment_id=judgment_id,
            original=direction,
            reason=reason,
        )

        await self._write_calibration(
            judgment_id=judgment_id,
            original=direction,
            new="neutral",
            reason=reason,
            factors=factors,
        )
        await self._push_calibration_alert(symbol, judgment, reason)
        return True

    # ------------------------------------------------------------------
    # 数据库辅助
    # ------------------------------------------------------------------

    async def _get_latest_judgment(self, symbol: str) -> dict[str, Any] | None:
        """查询最近一条 judgment 记录。

        Args:
            symbol: 股票代码。

        Returns:
            包含 id、direction、confidence、judgment_date 等字段的字典，
            或 None（无记录或查询失败）。
        """
        try:
            row = await db_query_one(
                """
                SELECT id, symbol, direction, confidence,
                       judgment_date, entry_zone_low, entry_zone_high,
                       stop_loss, logic_text
                FROM judgments
                WHERE symbol = $1
                ORDER BY judgment_date DESC, id DESC
                LIMIT 1
                """,
                symbol,
            )
            return dict(row) if row is not None else None
        except Exception as e:
            logger.error("get_latest_judgment_error", symbol=symbol, error=str(e))
            return None

    async def _write_calibration(
        self,
        judgment_id: int | None,
        original: str,
        new: str,
        reason: str,
        factors: IntradayFactors,
    ) -> None:
        """写入矫正记录到 calibrations 表。

        new_direction 固定为 'neutral'（当前设计：仅降级，不升级）。

        Args:
            judgment_id: 被矫正的 judgment id。
            original: 原始方向，如 'bullish'。
            new: 矫正后方向，如 'neutral'。
            reason: 矫正原因文本。
            factors: 触发矫正的盘中因子（序列化为 JSONB）。
        """
        trigger_factors: dict[str, Any] = {
            "vwap_deviation": factors.vwap_deviation,
            "momentum_1h": factors.momentum_1h,
            "momentum_15m": factors.momentum_15m,
            "volume_ratio_15m": factors.volume_ratio_15m,
            "support_distance": factors.support_distance,
            "price_vs_day_range": factors.price_vs_day_range,
            "rsi_15m": factors.rsi_15m,
            "macd_cross_15m": factors.macd_cross_15m,
            "calc_time": factors.calc_time.isoformat(),
        }

        try:
            await db_execute(
                """
                INSERT INTO calibrations (
                    judgment_id, calibration_time,
                    original_direction, new_direction,
                    reason, trigger_factors, created_at
                ) VALUES (
                    $1, NOW(),
                    $2, $3,
                    $4, $5::jsonb, NOW()
                )
                """,
                judgment_id,
                original,
                new,
                reason,
                json.dumps(trigger_factors),
            )
            logger.info(
                "calibration_written",
                judgment_id=judgment_id,
                original=original,
                new=new,
            )
        except Exception as e:
            logger.error(
                "write_calibration_error",
                judgment_id=judgment_id,
                error=str(e),
            )

    # ------------------------------------------------------------------
    # 推送辅助
    # ------------------------------------------------------------------

    async def _push_calibration_alert(
        self,
        symbol: str,
        judgment: dict[str, Any],
        reason: str,
    ) -> None:
        """通过 TelegramPusher 发送矫正提醒（非阻塞，失败不抛异常）。

        Args:
            symbol: 股票代码。
            judgment: judgment 记录字典，含 direction、confidence 等。
            reason: 矫正触发原因。
        """
        try:
            from bot.telegram_bot import TelegramPusher

            original: str = judgment.get("direction", "unknown")
            confidence = judgment.get("confidence")
            judgment_date = judgment.get("judgment_date", "")
            confidence_text = f"{float(confidence):.0%}" if confidence else "N/A"

            direction_label = {
                "bullish": "看多",
                "bearish": "看空",
                "neutral": "中性",
            }.get(original, original)

            text = (
                f"⚠️ <b>盘中矫正提醒</b>\n"
                f"━━━━━━━━━━━━━━━\n"
                f"标的: <b>{symbol}</b>\n"
                f"原判断: {direction_label}（置信度 {confidence_text}）[{judgment_date}]\n"
                f"矫正方向: 中性\n"
                f"触发原因: {reason}\n"
                f"━━━━━━━━━━━━━━━\n"
                f"⚡ 建议重新评估持仓或观望。"
            )

            pusher = TelegramPusher()
            await pusher.send_html(text)

        except Exception as e:
            # 推送失败不阻塞主流程
            logger.warning(
                "calibration_push_failed",
                symbol=symbol,
                error=str(e),
            )


# ------------------------------------------------------------------
# 模块级纯函数（方便单元测试）
# ------------------------------------------------------------------

def _check_bullish_contradiction(
    factors: IntradayFactors,
) -> tuple[bool, str]:
    """检测 bullish 判断与盘中因子的矛盾（需 2/3 条件触发）。

    Args:
        factors: 实时盘中因子。

    Returns:
        (triggered, reason) 元组。triggered=True 表示需要矫正。
    """
    fired: list[str] = []

    if factors.momentum_1h is not None and factors.momentum_1h < _BULLISH_MOM_THRESHOLD:
        fired.append(f"强烈下行动量 momentum_1h={factors.momentum_1h:.2f}")
    if factors.support_distance is not None and factors.support_distance < _BULLISH_SUPPORT_THRESHOLD:
        fired.append(f"跌破支撑 support_distance={factors.support_distance:.2f}")
    if factors.volume_ratio_15m is not None and factors.volume_ratio_15m > _BULLISH_VOL_THRESHOLD:
        fired.append(f"高卖量放大 volume_ratio_15m={factors.volume_ratio_15m:.2f}")

    triggered = len(fired) >= 2
    reason = "；".join(fired) if fired else ""
    return triggered, reason


def _check_bearish_contradiction(
    factors: IntradayFactors,
) -> tuple[bool, str]:
    """检测 bearish 判断与盘中因子的矛盾（需 2/3 条件触发）。

    Args:
        factors: 实时盘中因子。

    Returns:
        (triggered, reason) 元组。triggered=True 表示需要矫正。
    """
    fired: list[str] = []

    if factors.momentum_1h is not None and factors.momentum_1h > _BEARISH_MOM_THRESHOLD:
        fired.append(f"强烈上行动量 momentum_1h={factors.momentum_1h:.2f}")
    if factors.price_vs_day_range is not None and factors.price_vs_day_range > _BEARISH_RANGE_THRESHOLD:
        fired.append(f"处于日内高位 price_vs_day_range={factors.price_vs_day_range:.2f}")
    if factors.volume_ratio_15m is not None and factors.volume_ratio_15m > _BEARISH_VOL_THRESHOLD:
        fired.append(f"高买量放大 volume_ratio_15m={factors.volume_ratio_15m:.2f}")

    triggered = len(fired) >= 2
    reason = "；".join(fired) if fired else ""
    return triggered, reason
