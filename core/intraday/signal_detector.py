"""
盘中买卖信号检测器。

架构文档 Section 5.4：对实时 IntradayFactors 进行规则匹配，
生成 buy/sell 信号，并持久化到 intraday_signals 表。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

import structlog

from db.connection import db_execute, db_query_one

logger = structlog.get_logger(__name__)


@dataclass
class IntradaySignal:
    """盘中信号数据结构。

    Attributes:
        symbol: 股票代码。
        market: 市场标识，'CN' 或 'US'。
        signal_type: 信号方向，'buy' | 'sell'。
        strength: 信号强度，'strong' | 'moderate' | 'weak'。
        trigger_rule: 触发规则的可读名称。
        trigger_detail: 触发时的因子详情。
        price: 信号触发时的当前价格。
        suggested_price: 建议成交价，可为 None。
        stop_price: 建议止损价，可为 None。
        basis_judgment_id: 关联的基础判断 ID，可为 None。
    """

    symbol: str
    market: str
    signal_type: str
    strength: str
    trigger_rule: str
    trigger_detail: dict
    price: float
    suggested_price: float | None
    stop_price: float | None
    basis_judgment_id: int | None


class SignalDetector:
    """盘中买卖信号检测引擎。

    根据 IntradayFactors 的实时因子值，结合最新的基础判断方向
    和当前持仓状态，检测买入或卖出信号。
    """

    # Buy 条件阈值
    _BUY_VWAP_DEV_MAX = 0.5
    _BUY_PRICE_RANGE_MIN = 0.3
    _BUY_VOL_RATIO_MIN = 0.8
    _BUY_RSI_MAX = 65.0
    _BUY_SUPPORT_DIST_MIN = -0.5
    _BUY_MOM_1H_MIN = -1.0

    # Strong buy 触发阈值
    _STRONG_BUY_VWAP_LOW = -0.3
    _STRONG_BUY_VWAP_HIGH = 0.2

    # Sell 触发阈值
    _SELL_BREAKDOWN_SUPPORT = -1.0
    _SELL_BREAKDOWN_VOL = 1.5
    _SELL_VWAP_PERSIST_DEV = -1.5
    _SELL_VWAP_PERSIST_MOM = -1.0
    _SELL_MOM_COLLAPSE = -2.0

    # Regime 阈值：低于此值不开仓
    _REGIME_THRESHOLD_MIN = 0.8

    async def detect(
        self,
        symbol: str,
        market: str,
        factors: "IntradayFactors",
        current_price: float | None = None,
    ) -> IntradaySignal | None:
        """检测盘中买卖信号。

        先检测卖出条件（持仓保护优先），再检测买入条件。

        Args:
            symbol: 股票代码。
            market: 市场标识，'CN' 或 'US'。
            factors: 实时盘中因子。
            current_price: 当前 bar 收盘价，优先使用；None 时降级估算。

        Returns:
            IntradaySignal 如检测到信号，否则 None。
        """
        log = logger.bind(symbol=symbol, market=market, module="signal_detector")

        # 先尝试 sell（持仓保护优先级更高）
        sell_signal = await self._check_sell(symbol, market, factors, log, current_price)
        if sell_signal is not None:
            return sell_signal

        # 再尝试 buy
        buy_signal = await self._check_buy(symbol, market, factors, log, current_price)
        return buy_signal

    # ------------------------------------------------------------------
    # 买入检测
    # ------------------------------------------------------------------

    async def _check_buy(
        self,
        symbol: str,
        market: str,
        factors: "IntradayFactors",
        log: structlog.BoundLogger,
        current_price: float | None = None,
    ) -> IntradaySignal | None:
        """检测买入信号。"""
        direction, judgment_id, stop_loss, entry_low = await self._get_latest_judgment(symbol)

        if direction != "bullish":
            log.debug("buy_skip_no_bullish", direction=direction)
            return None

        threshold = await self._get_regime_threshold()
        if threshold < self._REGIME_THRESHOLD_MIN:
            log.debug("buy_skip_regime_threshold", threshold=threshold)
            return None

        # 6 个前置条件全部必须满足
        reasons: list[str] = []
        vwap_dev = factors.vwap_deviation
        pvr = factors.price_vs_day_range
        vol_ratio = factors.volume_ratio_15m
        rsi = factors.rsi_15m
        sup_dist = factors.support_distance
        mom_1h = factors.momentum_1h

        if vwap_dev is None or abs(vwap_dev) >= self._BUY_VWAP_DEV_MAX:
            reasons.append(f"vwap_deviation={vwap_dev} not in (-0.5, 0.5)")
        if pvr is None or pvr <= self._BUY_PRICE_RANGE_MIN:
            reasons.append(f"price_vs_day_range={pvr} <= 0.3")
        if vol_ratio is None or vol_ratio <= self._BUY_VOL_RATIO_MIN:
            reasons.append(f"volume_ratio_15m={vol_ratio} <= 0.8")
        if rsi is None or rsi >= self._BUY_RSI_MAX:
            reasons.append(f"rsi_15m={rsi} >= 65")
        if sup_dist is None or sup_dist <= self._BUY_SUPPORT_DIST_MIN:
            reasons.append(f"support_distance={sup_dist} <= -0.5")
        if mom_1h is None or mom_1h <= self._BUY_MOM_1H_MIN:
            reasons.append(f"momentum_1h={mom_1h} <= -1.0")

        if reasons:
            log.debug("buy_conditions_failed", reasons=reasons)
            return None

        # 判断是否触发 strong buy
        is_strong = (
            vwap_dev is not None
            and self._STRONG_BUY_VWAP_LOW < vwap_dev < self._STRONG_BUY_VWAP_HIGH
            and factors.macd_cross_15m == "golden"
        )

        strength = "strong" if is_strong else "moderate"
        trigger_rule = "vwap_pullback_macd_golden" if is_strong else "buy_6conditions"

        trigger_detail = {
            "vwap_deviation": vwap_dev,
            "price_vs_day_range": pvr,
            "volume_ratio_15m": vol_ratio,
            "rsi_15m": rsi,
            "support_distance": sup_dist,
            "momentum_1h": mom_1h,
            "macd_cross_15m": factors.macd_cross_15m,
        }

        # current_price 由调用方传入；不可用时设为 0.0 并记录警告
        if current_price is None:
            log.warning("buy_signal_no_price", symbol=symbol)
            current_price = 0.0

        # Use entry_low as suggested price if available
        suggested = float(entry_low) if entry_low is not None else None

        log.info(
            "buy_signal_detected",
            strength=strength,
            trigger=trigger_rule,
            judgment_id=judgment_id,
            price=current_price,
        )

        return IntradaySignal(
            symbol=symbol,
            market=market,
            signal_type="buy",
            strength=strength,
            trigger_rule=trigger_rule,
            trigger_detail=trigger_detail,
            price=current_price,
            suggested_price=suggested,
            stop_price=float(stop_loss) if stop_loss is not None else None,
            basis_judgment_id=judgment_id,
        )

    # ------------------------------------------------------------------
    # 卖出检测
    # ------------------------------------------------------------------

    async def _check_sell(
        self,
        symbol: str,
        market: str,
        factors: "IntradayFactors",
        log: structlog.BoundLogger,
        current_price: float | None = None,
    ) -> IntradaySignal | None:
        """检测卖出信号。"""
        position = await self._get_open_position(symbol)
        if position is None:
            return None

        stop_loss = float(position["stop_loss"]) if position.get("stop_loss") else None
        entry_price = float(position["entry_price"]) if position.get("entry_price") else None

        # 收集触发的规则
        triggered_rules: list[str] = []
        trigger_details: dict = {
            "vwap_deviation": factors.vwap_deviation,
            "momentum_1h": factors.momentum_1h,
            "support_distance": factors.support_distance,
            "volume_ratio_15m": factors.volume_ratio_15m,
        }

        # 规则 1: 止损触发（使用调用方传入的当前价格）
        if stop_loss is not None and current_price is not None and current_price <= stop_loss:
            triggered_rules.append("stop_loss")
            trigger_details["stop_loss_level"] = stop_loss
            trigger_details["current_price"] = current_price

        # 规则 2: 跌破支撑 + 放量
        sup_dist = factors.support_distance
        vol_ratio = factors.volume_ratio_15m
        if (
            sup_dist is not None and sup_dist < self._SELL_BREAKDOWN_SUPPORT
            and vol_ratio is not None and vol_ratio > self._SELL_BREAKDOWN_VOL
        ):
            triggered_rules.append("breakdown")

        # 规则 3: VWAP 持续偏离 + 动量下行
        vwap_dev = factors.vwap_deviation
        mom_1h = factors.momentum_1h
        if (
            vwap_dev is not None and vwap_dev < self._SELL_VWAP_PERSIST_DEV
            and mom_1h is not None and mom_1h < self._SELL_VWAP_PERSIST_MOM
        ):
            triggered_rules.append("vwap_persistent")

        # 规则 4: 动量崩塌
        if mom_1h is not None and mom_1h < self._SELL_MOM_COLLAPSE:
            triggered_rules.append("momentum_collapse")

        if not triggered_rules:
            return None

        # 判断强度
        is_strong = "stop_loss" in triggered_rules or len(triggered_rules) >= 2
        strength = "strong" if is_strong else "moderate"
        trigger_rule = triggered_rules[0] if len(triggered_rules) == 1 else "+".join(triggered_rules)

        log.info(
            "sell_signal_detected",
            strength=strength,
            rules=triggered_rules,
            entry_price=entry_price,
        )

        resolved_price = current_price if current_price is not None else (entry_price or 0.0)

        return IntradaySignal(
            symbol=symbol,
            market=market,
            signal_type="sell",
            strength=strength,
            trigger_rule=trigger_rule,
            trigger_detail={**trigger_details, "triggered_rules": triggered_rules},
            price=resolved_price,
            suggested_price=resolved_price,
            stop_price=stop_loss,
            basis_judgment_id=None,
        )

    # ------------------------------------------------------------------
    # 数据库查询辅助
    # ------------------------------------------------------------------

    async def _get_latest_judgment(
        self, symbol: str
    ) -> tuple[str | None, int | None, float | None, float | None]:
        """查询最近一条 judgment 的方向和关键价位。

        Args:
            symbol: 股票代码。

        Returns:
            (direction, judgment_id, stop_loss, entry_zone_low) 四元组。
            查询失败或无记录时，direction 为 None。
        """
        try:
            row = await db_query_one(
                """
                SELECT id, direction, stop_loss, entry_zone_low
                FROM judgments
                WHERE symbol = $1
                ORDER BY judgment_date DESC, id DESC
                LIMIT 1
                """,
                symbol,
            )
            if row is None:
                return None, None, None, None
            return (
                row["direction"],
                row["id"],
                float(row["stop_loss"]) if row["stop_loss"] is not None else None,
                float(row["entry_zone_low"]) if row["entry_zone_low"] is not None else None,
            )
        except Exception as e:
            logger.error("get_latest_judgment_error", symbol=symbol, error=str(e))
            return None, None, None, None

    async def _get_regime_threshold(self) -> float:
        """获取当前 CN 市场 regime 的 signal_threshold_adj。

        Returns:
            signal_threshold_adj 数值，默认 1.0（查询失败时）。
        """
        try:
            val = await db_query_one(
                """
                SELECT signal_threshold_adj
                FROM regime_daily
                WHERE market = 'CN'
                ORDER BY trade_date DESC
                LIMIT 1
                """
            )
            if val is None:
                return 1.0
            adj = val["signal_threshold_adj"]
            return float(adj) if adj is not None else 1.0
        except Exception as e:
            logger.error("get_regime_threshold_error", error=str(e))
            return 1.0

    async def _get_open_position(self, symbol: str) -> dict | None:
        """查询 symbol 的当前持仓（状态为 open）。

        Args:
            symbol: 股票代码。

        Returns:
            持仓行字典，或 None（无持仓）。
        """
        try:
            row = await db_query_one(
                """
                SELECT id, symbol, market, entry_price, shares,
                       stop_loss, target_1, target_2, entry_date
                FROM positions
                WHERE symbol = $1 AND status = 'open'
                ORDER BY entry_date DESC
                LIMIT 1
                """,
                symbol,
            )
            return dict(row) if row is not None else None
        except Exception as e:
            logger.error("get_open_position_error", symbol=symbol, error=str(e))
            return None

    async def save_signal(self, signal: IntradaySignal) -> int:
        """将 IntradaySignal 持久化到 intraday_signals 表。

        Args:
            signal: 待写入的信号对象。

        Returns:
            新插入记录的 id。

        Raises:
            RuntimeError: 数据库写入失败时抛出。
        """
        try:
            row = await db_query_one(
                """
                INSERT INTO intraday_signals (
                    symbol, market, signal_time, signal_type, strength,
                    trigger_rule, trigger_detail, price_at_signal,
                    suggested_price, stop_price, basis_judgment_id
                ) VALUES (
                    $1, $2, NOW(), $3, $4,
                    $5, $6::jsonb, $7,
                    $8, $9, $10
                )
                RETURNING id
                """,
                signal.symbol,
                signal.market,
                signal.signal_type,
                signal.strength,
                signal.trigger_rule,
                json.dumps(signal.trigger_detail),
                signal.price,
                signal.suggested_price,
                signal.stop_price,
                signal.basis_judgment_id,
            )
            signal_id: int = row["id"]
            logger.info(
                "signal_saved",
                symbol=signal.symbol,
                signal_type=signal.signal_type,
                strength=signal.strength,
                signal_id=signal_id,
            )
            return signal_id
        except Exception as e:
            logger.error("save_signal_error", symbol=signal.symbol, error=str(e))
            raise RuntimeError(f"Failed to save signal for {signal.symbol}: {e}") from e


