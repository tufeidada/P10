"""
盘中信号质量追踪器。

每日 16:30 运行：
1. 找出 actual_ret_1d 为 NULL 且信号日期 < as_of_date 的记录
2. 回填 actual_ret_30m, actual_ret_1d, actual_max_favorable, actual_max_adverse
3. 计算 signal_quality 分数
4. 聚合到 signal_quality_tracker 表按规则统计
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

import numpy as np
import structlog

from db.connection import db_execute, db_execute_many, db_query, db_query_one

logger = structlog.get_logger(__name__)


class SignalQualityTracker:
    """盘中信号质量追踪器。

    每日 16:30 调度执行，回填信号实际表现并聚合规则级别统计。
    """

    async def backfill_signal_returns(
        self, as_of_date: date | None = None
    ) -> dict[str, int]:
        """回填信号的实际收益数据。

        For each signal in intraday_signals where actual_ret_1d IS NULL
        and DATE(signal_time) < as_of_date:

        1. Find close price at signal date (use market_bars_daily for the signal date)
        2. Find T+1 close from market_bars_daily (next trading day)
        3. actual_ret_1d = (close_T1 / close_signal) - 1
        4. actual_ret_30m: approximated from T+1 daily open if intraday bars unavailable
        5. actual_max_favorable: max(high) over next 1 trading day / signal price - 1
        6. actual_max_adverse: min(low) over next 1 trading day / signal price - 1
           For buy signals: favorable = upside, adverse = downside
           For sell signals: favorable = downside (price drop), adverse = upside (price rise)
        7. signal_quality:
           - buy signal: max_favorable / abs(max_adverse) if max_adverse != 0 else 5.0
             (profit factor ratio, >1 = good)
           - sell signal: mapped to 0-100 scale based on actual_ret_1d direction

        Args:
            as_of_date: 截止日期，默认今天。

        Returns:
            统计字典，如 {"total": N, "backfilled": M, "errors": K}
        """
        if as_of_date is None:
            as_of_date = date.today()

        stats: dict[str, int] = {"total": 0, "backfilled": 0, "errors": 0}

        # 查找需要回填的信号：actual_ret_1d IS NULL 且信号日期 < as_of_date
        pending = await db_query(
            """
            SELECT id, symbol, market, signal_type, signal_time,
                   price_at_signal, trigger_rule
            FROM intraday_signals
            WHERE actual_ret_1d IS NULL
              AND DATE(signal_time AT TIME ZONE 'Asia/Shanghai') < $1
            ORDER BY signal_time
            LIMIT 500
            """,
            as_of_date,
        )

        stats["total"] = len(pending)
        if not pending:
            logger.info("signal_backfill_nothing_to_do", as_of_date=str(as_of_date))
            return stats

        logger.info(
            "signal_backfill_start",
            total=len(pending),
            as_of_date=str(as_of_date),
        )

        update_batch: list[tuple[Any, ...]] = []

        for sig in pending:
            try:
                row = await self._compute_signal_returns(sig, as_of_date)
                if row is not None:
                    update_batch.append(row)
            except Exception:
                logger.exception(
                    "signal_backfill_error",
                    signal_id=sig["id"],
                    symbol=sig["symbol"],
                )
                stats["errors"] += 1

        if update_batch:
            await db_execute_many(
                """
                UPDATE intraday_signals
                SET actual_ret_30m        = COALESCE($1, actual_ret_30m),
                    actual_ret_1d         = COALESCE($2, actual_ret_1d),
                    actual_max_favorable  = COALESCE($3, actual_max_favorable),
                    actual_max_adverse    = COALESCE($4, actual_max_adverse),
                    signal_quality        = COALESCE($5, signal_quality)
                WHERE id = $6
                """,
                update_batch,
            )
            stats["backfilled"] = len(update_batch)
            logger.info("signal_backfill_done", backfilled=len(update_batch))

        return stats

    async def update_quality_tracker(
        self,
        period_end: date | None = None,
        lookback_days: int = 30,
    ) -> int:
        """Aggregate signal quality by rule and regime, write to signal_quality_tracker.

        For each distinct (trigger_rule, market) combination in intraday_signals
        where signal_quality IS NOT NULL:

        1. Group by trigger_rule, market, regime_mode (from basis_judgment_id → regime_at_time)
        2. Compute accuracy, avg_return, avg_max_dd, ic_value, ir_value
        3. Upsert into signal_quality_tracker with period_start/end

        Args:
            period_end: 统计截止日期，默认今天。
            lookback_days: 回看天数，默认 30。

        Returns:
            写入的行数。
        """
        if period_end is None:
            period_end = date.today()

        from datetime import timedelta

        period_start = period_end - timedelta(days=lookback_days)

        # 获取窗口内所有有质量评分的信号，关联 regime 信息
        rows = await db_query(
            """
            SELECT
                s.id,
                s.trigger_rule,
                s.market,
                s.signal_type,
                s.signal_quality,
                s.actual_ret_1d,
                s.actual_max_favorable,
                s.actual_max_adverse,
                j.regime_at_time
            FROM intraday_signals s
            LEFT JOIN judgments j ON j.id = s.basis_judgment_id
            WHERE s.signal_quality IS NOT NULL
              AND DATE(s.signal_time AT TIME ZONE 'Asia/Shanghai') >= $1
              AND DATE(s.signal_time AT TIME ZONE 'Asia/Shanghai') <= $2
            ORDER BY s.trigger_rule, s.market
            """,
            period_start,
            period_end,
        )

        if not rows:
            logger.info(
                "quality_tracker_no_data",
                period_start=str(period_start),
                period_end=str(period_end),
            )
            return 0

        # 按 (trigger_rule, market, regime_mode) 分组
        groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for r in rows:
            rule = r["trigger_rule"] or "unknown"
            mkt = r["market"] or "CN"
            regime_mode = _extract_regime_mode(r["regime_at_time"])
            key = (rule, mkt, regime_mode)
            groups.setdefault(key, []).append(dict(r))

        upsert_rows: list[tuple[Any, ...]] = []

        for (rule, mkt, regime_mode), signals in groups.items():
            stats_row = _aggregate_signals(
                signals, rule, mkt, regime_mode, period_start, period_end
            )
            if stats_row is not None:
                upsert_rows.append(stats_row)

        if upsert_rows:
            await db_execute_many(
                """
                INSERT INTO signal_quality_tracker
                    (rule_name, market, regime_mode, period_start, period_end,
                     total_signals, correct_signals, accuracy,
                     avg_return, avg_max_dd, ic_value, ir_value)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                ON CONFLICT (rule_name, market, regime_mode, period_end)
                DO UPDATE SET
                    period_start    = EXCLUDED.period_start,
                    total_signals   = EXCLUDED.total_signals,
                    correct_signals = EXCLUDED.correct_signals,
                    accuracy        = EXCLUDED.accuracy,
                    avg_return      = EXCLUDED.avg_return,
                    avg_max_dd      = EXCLUDED.avg_max_dd,
                    ic_value        = EXCLUDED.ic_value,
                    ir_value        = EXCLUDED.ir_value
                """,
                upsert_rows,
            )
            logger.info(
                "quality_tracker_updated",
                rows=len(upsert_rows),
                period_end=str(period_end),
            )

        return len(upsert_rows)

    async def run_all(self, as_of_date: date | None = None) -> dict[str, Any]:
        """Run both backfill and quality update. Called by scheduler.

        Args:
            as_of_date: 截止日期，默认今天。

        Returns:
            合并的结果字典，包含 backfill 和 tracker 的统计。
        """
        if as_of_date is None:
            as_of_date = date.today()

        logger.info("signal_quality_run_all_start", as_of_date=str(as_of_date))

        backfill_stats: dict[str, Any] = {}
        tracker_rows = 0

        try:
            backfill_stats = await self.backfill_signal_returns(as_of_date)
        except Exception:
            logger.exception("signal_quality_backfill_failed")
            backfill_stats = {"total": 0, "backfilled": 0, "errors": -1}

        try:
            tracker_rows = await self.update_quality_tracker(period_end=as_of_date)
        except Exception:
            logger.exception("signal_quality_tracker_failed")
            tracker_rows = -1

        result: dict[str, Any] = {
            "as_of_date": str(as_of_date),
            "backfill": backfill_stats,
            "tracker_rows_written": tracker_rows,
        }
        logger.info("signal_quality_run_all_done", result=result)
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _compute_signal_returns(
        self, sig: Any, as_of_date: date
    ) -> tuple[Any, ...] | None:
        """为单条信号计算实际收益指标。

        Args:
            sig: intraday_signals 行 (asyncpg.Record)。
            as_of_date: 截止日期。

        Returns:
            (ret_30m, ret_1d, max_favorable, max_adverse, signal_quality, id)
            未能计算时返回 None。
        """
        symbol: str = sig["symbol"]
        signal_type: str = sig["signal_type"] or "buy"
        price_at_signal: float | None = (
            float(sig["price_at_signal"]) if sig["price_at_signal"] else None
        )

        # 信号日期（使用 Asia/Shanghai 时区）
        signal_dt: datetime = sig["signal_time"]
        signal_date: date = signal_dt.date()

        if price_at_signal is None or price_at_signal == 0:
            logger.warning(
                "signal_backfill_no_price",
                signal_id=sig["id"],
                symbol=symbol,
            )
            return None

        # 获取信号日当日行情（用于 T+1 计算基准收盘价）
        base_bar = await db_query_one(
            """
            SELECT trade_date, close
            FROM market_bars_daily
            WHERE symbol = $1 AND trade_date <= $2
            ORDER BY trade_date DESC LIMIT 1
            """,
            symbol,
            signal_date,
        )
        if not base_bar:
            logger.warning(
                "signal_backfill_no_base_bar",
                signal_id=sig["id"],
                symbol=symbol,
                signal_date=str(signal_date),
            )
            return None

        base_date: date = base_bar["trade_date"]

        # 获取 T+1 行情（含 high/low 用于计算最大涨幅/回撤）
        t1_bar = await db_query_one(
            """
            SELECT trade_date, open, close, high, low
            FROM market_bars_daily
            WHERE symbol = $1 AND trade_date > $2
            ORDER BY trade_date LIMIT 1
            """,
            symbol,
            base_date,
        )
        if not t1_bar:
            return None

        close_t1 = float(t1_bar["close"])
        high_t1 = float(t1_bar["high"])
        low_t1 = float(t1_bar["low"])
        open_t1 = float(t1_bar["open"])

        ref_price = price_at_signal

        # actual_ret_1d
        actual_ret_1d = round(close_t1 / ref_price - 1, 6)

        # actual_ret_30m: 用 T+1 开盘价近似（盘中 bar 不可用时的降级处理）
        actual_ret_30m = round(open_t1 / ref_price - 1, 6)

        # actual_max_favorable / actual_max_adverse
        # 买入信号：favorable = 上涨空间, adverse = 下跌空间
        # 卖出信号：favorable = 下跌空间（避免了损失），adverse = 上涨空间
        is_buy = signal_type.lower() in ("buy", "long", "bullish")

        if is_buy:
            actual_max_favorable = round(high_t1 / ref_price - 1, 6)
            actual_max_adverse = round(low_t1 / ref_price - 1, 6)
        else:
            # 卖出方向：价格下跌是有利的
            actual_max_favorable = round(1 - low_t1 / ref_price, 6)   # 下跌幅度（正值为利）
            actual_max_adverse = round(high_t1 / ref_price - 1, 6)    # 上涨幅度（负效应）

        # signal_quality
        signal_quality = _compute_signal_quality(
            is_buy=is_buy,
            actual_ret_1d=actual_ret_1d,
            max_favorable=actual_max_favorable,
            max_adverse=actual_max_adverse,
        )

        return (
            actual_ret_30m,
            actual_ret_1d,
            actual_max_favorable,
            actual_max_adverse,
            signal_quality,
            sig["id"],
        )


# ------------------------------------------------------------------
# Module-level pure helpers (no DB access)
# ------------------------------------------------------------------


def _extract_regime_mode(regime_at_time: Any) -> str:
    """从 JSONB regime_at_time 字段中提取 regime_mode 字符串。

    Args:
        regime_at_time: JSONB 值，可为 dict 或 None。

    Returns:
        regime_mode 字符串，未知时返回 'unknown'。
    """
    if not regime_at_time:
        return "unknown"
    if isinstance(regime_at_time, dict):
        return str(regime_at_time.get("regime_mode", "unknown"))
    return "unknown"


def _compute_signal_quality(
    is_buy: bool,
    actual_ret_1d: float,
    max_favorable: float,
    max_adverse: float,
) -> float:
    """计算信号质量分数。

    买入信号：profit factor ratio = max_favorable / |max_adverse|
              max_adverse 为 0 时返回 5.0（视为极佳）
    卖出信号：根据 actual_ret_1d 方向映射到 0-100 分制
              ret < -5% → 100, ret < 0 → 50-100, ret >= 0 → 0-49

    Args:
        is_buy: 是否为买入信号。
        actual_ret_1d: T+1 实际日收益率。
        max_favorable: 最大有利变动。
        max_adverse: 最大不利变动（买入信号为负值）。

    Returns:
        质量分数。买入信号为 profit factor；卖出信号为 0-100。
    """
    if is_buy:
        adverse_abs = abs(max_adverse)
        if adverse_abs == 0:
            return 5.0
        quality = max_favorable / adverse_abs
        return round(quality, 4)
    else:
        # 卖出信号：ret_1d < 0 表示成功避免损失
        if actual_ret_1d < -0.05:
            return 100.0
        elif actual_ret_1d < 0:
            # 线性映射到 50-100
            return round(50.0 + (abs(actual_ret_1d) / 0.05) * 50.0, 2)
        elif actual_ret_1d == 0:
            return 50.0
        else:
            # ret > 0 表示卖出后价格上涨，信号质量低
            # ret = 5% 以上 → 趋近 0
            penalty = min(actual_ret_1d / 0.05, 1.0)
            return round(50.0 * (1.0 - penalty), 2)


def _aggregate_signals(
    signals: list[dict[str, Any]],
    rule: str,
    market: str,
    regime_mode: str,
    period_start: date,
    period_end: date,
) -> tuple[Any, ...] | None:
    """对一组信号做统计聚合。

    Args:
        signals: 同一 (rule, market, regime_mode) 下的信号列表。
        rule: 触发规则名称。
        market: 市场代码。
        regime_mode: 市场状态。
        period_start: 统计起始日。
        period_end: 统计截止日。

    Returns:
        适用于 db_execute_many 的参数元组，或 None（数据不足）。
    """
    total = len(signals)
    if total == 0:
        return None

    # 区分买入/卖出
    buy_signals = [
        s for s in signals
        if (s.get("signal_type") or "buy").lower() in ("buy", "long", "bullish")
    ]
    sell_signals = [s for s in signals if s not in buy_signals]

    # correct_signals
    correct = 0
    for s in buy_signals:
        q = s.get("signal_quality")
        if q is not None and float(q) >= 1.0:
            correct += 1
    for s in sell_signals:
        r = s.get("actual_ret_1d")
        if r is not None and float(r) < 0:
            correct += 1

    accuracy = round(correct / total, 4) if total > 0 else 0.0

    # avg_return（买入信号的均值）
    buy_rets = [
        float(s["actual_ret_1d"])
        for s in buy_signals
        if s.get("actual_ret_1d") is not None
    ]
    avg_return = round(float(np.mean(buy_rets)), 6) if buy_rets else None

    # avg_max_dd（所有信号的最大不利变动均值）
    adverse_vals = [
        float(s["actual_max_adverse"])
        for s in signals
        if s.get("actual_max_adverse") is not None
    ]
    avg_max_dd = round(float(np.mean(adverse_vals)), 6) if adverse_vals else None

    # IC value：signal_quality 与 actual_ret_1d 的相关系数
    ic_value = 0.0
    ir_value = 0.0
    paired = [
        (float(s["signal_quality"]), float(s["actual_ret_1d"]))
        for s in signals
        if s.get("signal_quality") is not None and s.get("actual_ret_1d") is not None
    ]
    if len(paired) >= 3:
        qualities = np.array([p[0] for p in paired])
        rets = np.array([p[1] for p in paired])
        corr_matrix = np.corrcoef(qualities, rets)
        ic_value = round(float(corr_matrix[0, 1]), 6)

        ret_mean = float(np.mean(rets))
        ret_std = float(np.std(rets, ddof=1))
        ir_value = round(ret_mean / ret_std, 6) if ret_std > 0 else 0.0

    return (
        rule,
        market,
        regime_mode,
        period_start,
        period_end,
        total,
        correct,
        accuracy,
        avg_return,
        avg_max_dd,
        ic_value,
        ir_value,
    )
