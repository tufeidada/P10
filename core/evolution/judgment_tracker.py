"""
判断记录回填引擎。

每日 16:10 调度执行，回填历史判断的实际表现（收益率、最大涨幅/回撤），
并判定方向正确性。依赖 trade_calendar 表计算交易日偏移。
"""

from __future__ import annotations

from datetime import date
from typing import Any

import structlog

from db.connection import db_execute_many, db_query, db_query_one, db_query_val

logger = structlog.get_logger(__name__)

# 需要回填的收益率窗口（交易日数）
_RET_WINDOWS: list[int] = [1, 5, 10, 20]


class JudgmentTracker:
    """判断记录回填引擎。

    每日 16:10 调度执行，回填历史判断的实际表现。
    """

    async def backfill_all(self, as_of_date: date | None = None) -> dict[str, int]:
        """回填所有未填的判断。

        For each judgment where actual_ret_Xd is NULL and enough trading days
        have passed since judgment_date, compute actual returns and max
        up/drawdown from market_bars_daily, then UPDATE the row.

        Uses trade_calendar to count trading days (not calendar days).

        Args:
            as_of_date: 截止日期，默认今天。

        Returns:
            统计字典，如 {"total_checked": 12, "updated_1d": 8, ...}
        """
        if as_of_date is None:
            as_of_date = date.today()

        stats: dict[str, int] = {
            "total_checked": 0,
            "updated_1d": 0,
            "updated_5d": 0,
            "updated_10d": 0,
            "updated_20d": 0,
            "updated_is_correct": 0,
            "updated_error_category": 0,
        }

        # Step 1: 查找需要回填的判断
        # 先找出 as_of_date 相对于各窗口的最晚 judgment_date 阈值
        # 通过 trade_calendar 找到 as_of_date 之前 N 个交易日的日期
        thresholds = await self._get_backfill_thresholds(as_of_date)
        if not thresholds:
            logger.info("backfill_no_thresholds", as_of_date=str(as_of_date))
            return stats

        # 查询需要回填任何字段的判断（含 error_category 分类所需字段）
        judgments = await db_query(
            """
            SELECT id, symbol, market, judgment_date, direction,
                   actual_ret_1d, actual_ret_5d, actual_ret_10d, actual_ret_20d,
                   actual_max_up_20d, actual_max_dd_20d, is_correct, error_category,
                   technical_score, fundamental_score
            FROM judgments
            WHERE (actual_ret_1d IS NULL AND judgment_date <= $1)
               OR (actual_ret_5d IS NULL AND judgment_date <= $2)
               OR (actual_ret_10d IS NULL AND judgment_date <= $3)
               OR (actual_ret_20d IS NULL AND judgment_date <= $4)
            ORDER BY judgment_date
            """,
            thresholds[1],
            thresholds[5],
            thresholds[10],
            thresholds[20],
        )

        stats["total_checked"] = len(judgments)
        if not judgments:
            logger.info("backfill_nothing_to_do", as_of_date=str(as_of_date))
            return stats

        logger.info("backfill_start", total=len(judgments), as_of_date=str(as_of_date))

        # Step 2: 逐条回填
        update_batch: list[tuple[Any, ...]] = []

        for j in judgments:
            try:
                updates = await self._compute_returns_for_judgment(
                    j, thresholds, as_of_date
                )
                if updates:
                    update_batch.append(updates)
                    # 统计
                    if updates[0] is not None and j["actual_ret_1d"] is None:
                        stats["updated_1d"] += 1
                    if updates[1] is not None and j["actual_ret_5d"] is None:
                        stats["updated_5d"] += 1
                    if updates[2] is not None and j["actual_ret_10d"] is None:
                        stats["updated_10d"] += 1
                    if updates[3] is not None and j["actual_ret_20d"] is None:
                        stats["updated_20d"] += 1
                    if updates[6] is not None and j["is_correct"] is None:
                        stats["updated_is_correct"] += 1
                    # updates[7] = error_category (index after is_correct)
                    if updates[7] is not None and j["error_category"] is None:
                        stats["updated_error_category"] += 1
            except Exception:
                logger.exception(
                    "backfill_judgment_error",
                    judgment_id=j["id"],
                    symbol=j["symbol"],
                )

        # Step 3: 批量更新
        if update_batch:
            await db_execute_many(
                """
                UPDATE judgments
                SET actual_ret_1d    = COALESCE($1, actual_ret_1d),
                    actual_ret_5d    = COALESCE($2, actual_ret_5d),
                    actual_ret_10d   = COALESCE($3, actual_ret_10d),
                    actual_ret_20d   = COALESCE($4, actual_ret_20d),
                    actual_max_up_20d = COALESCE($5, actual_max_up_20d),
                    actual_max_dd_20d = COALESCE($6, actual_max_dd_20d),
                    is_correct       = COALESCE($7, is_correct),
                    error_category   = COALESCE($8, error_category),
                    reviewed_at      = COALESCE($9, reviewed_at)
                WHERE id = $10
                """,
                update_batch,
            )
            logger.info("backfill_updated", count=len(update_batch))

        logger.info("backfill_done", stats=stats)
        return stats

    async def get_accuracy_stats(
        self, market: str = "CN", days: int = 30
    ) -> dict[str, Any]:
        """获取近期判断准确率统计。

        Args:
            market: 市场代码，默认 'CN'。
            days: 回看天数，默认 30。

        Returns:
            统计字典，包含 total, correct, accuracy, by_direction。
        """
        row = await db_query_one(
            """
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE is_correct = TRUE) AS correct,
                COUNT(*) FILTER (WHERE is_correct = FALSE) AS incorrect
            FROM judgments
            WHERE market = $1
              AND is_correct IS NOT NULL
              AND judgment_date >= CURRENT_DATE - $2 * INTERVAL '1 day'
            """,
            market,
            days,
        )

        total = row["total"] if row else 0
        correct = row["correct"] if row else 0
        accuracy = round(correct / total, 4) if total > 0 else 0.0

        # 按方向分组
        direction_rows = await db_query(
            """
            SELECT
                direction,
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE is_correct = TRUE) AS correct
            FROM judgments
            WHERE market = $1
              AND is_correct IS NOT NULL
              AND judgment_date >= CURRENT_DATE - $2 * INTERVAL '1 day'
            GROUP BY direction
            """,
            market,
            days,
        )

        by_direction: dict[str, dict[str, Any]] = {}
        for dr in direction_rows:
            d_total = dr["total"]
            d_correct = dr["correct"]
            by_direction[dr["direction"]] = {
                "total": d_total,
                "correct": d_correct,
                "accuracy": round(d_correct / d_total, 4) if d_total > 0 else 0.0,
            }

        return {
            "total": total,
            "correct": correct,
            "accuracy": accuracy,
            "by_direction": by_direction,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _classify_error(
        self,
        judgment: Any,
        bar_list: list[dict[str, Any]],
        as_of_date: date,
    ) -> str | None:
        """Classify why a judgment was wrong.

        Checks in order:
        1. 'external_event': any bar in first 3 days has |daily_ret| > 5%
        2. 'regime_shift': regime_mode changed within 5 trading days after judgment_date
        3. 'timing': technical_score > 60 (tech said go) but judgment failed
        4. 'fundamental': fundamental_score < 40 for a bullish judgment
        5. None: unclassified (for LLM to analyze later)

        Args:
            judgment: 判断记录，需含 market, judgment_date, direction,
                      technical_score, fundamental_score 字段。
            bar_list: T+1 起的后续行情列表，每项含 close/high/low。
            as_of_date: 截止日期（未使用，保留供扩展）。

        Returns:
            错误分类字符串，或 None（无法分类）。
        """
        direction: str = judgment["direction"]
        judgment_date: date = judgment["judgment_date"]
        market: str = judgment["market"]

        # ----------------------------------------------------------------
        # 1. external_event: 前 3 日内出现单日 |涨跌幅| > 5%
        # ----------------------------------------------------------------
        # bar_list[0] = T+1, bar_list[1] = T+2, ...
        # 需要计算单日收益率，需相邻两根 bar 的收盘价
        # 以信号当日收盘价作为 T+0 基准
        base_bar = await db_query_one(
            """
            SELECT close FROM market_bars_daily
            WHERE symbol = $1 AND trade_date <= $2
            ORDER BY trade_date DESC LIMIT 1
            """,
            judgment["symbol"],
            judgment_date,
        )
        if base_bar:
            prev_close = float(base_bar["close"])
            for i, bar in enumerate(bar_list[:3]):
                curr_close = bar["close"]
                if prev_close > 0:
                    daily_ret = abs(curr_close / prev_close - 1)
                    if daily_ret > 0.05:
                        logger.debug(
                            "classify_external_event",
                            judgment_id=judgment["id"],
                            day=i + 1,
                            daily_ret=round(daily_ret, 4),
                        )
                        return "external_event"
                prev_close = curr_close

        # ----------------------------------------------------------------
        # 2. regime_shift: regime_mode 在 judgment_date 后 5 个交易日内发生变化
        # ----------------------------------------------------------------
        regime_on_day = await db_query_one(
            """
            SELECT regime_mode FROM regime_daily
            WHERE market = $1 AND trade_date <= $2
            ORDER BY trade_date DESC LIMIT 1
            """,
            market,
            judgment_date,
        )
        regime_5d_later = await db_query_one(
            """
            SELECT regime_mode FROM regime_daily
            WHERE market = $1 AND trade_date > $2
            ORDER BY trade_date LIMIT 1
            OFFSET 4
            """,
            market,
            judgment_date,
        )
        if (
            regime_on_day
            and regime_5d_later
            and regime_on_day["regime_mode"] != regime_5d_later["regime_mode"]
        ):
            logger.debug(
                "classify_regime_shift",
                judgment_id=judgment["id"],
                from_regime=regime_on_day["regime_mode"],
                to_regime=regime_5d_later["regime_mode"],
            )
            return "regime_shift"

        # ----------------------------------------------------------------
        # 3. timing: 技术面看多（technical_score > 60）但判断失败
        #    意味着技术信号出现了，但时机偏早或基本面未跟上
        # ----------------------------------------------------------------
        tech_score = judgment["technical_score"]
        if tech_score is not None:
            tech_score_f = float(tech_score)
            if direction == "bullish" and tech_score_f > 60:
                logger.debug(
                    "classify_timing",
                    judgment_id=judgment["id"],
                    technical_score=tech_score_f,
                )
                return "timing"

        # ----------------------------------------------------------------
        # 4. fundamental: 基本面弱（fundamental_score < 40）但判断看多
        # ----------------------------------------------------------------
        fund_score = judgment["fundamental_score"]
        if fund_score is not None:
            fund_score_f = float(fund_score)
            if direction == "bullish" and fund_score_f < 40:
                logger.debug(
                    "classify_fundamental",
                    judgment_id=judgment["id"],
                    fundamental_score=fund_score_f,
                )
                return "fundamental"

        # ----------------------------------------------------------------
        # 5. 无法分类
        # ----------------------------------------------------------------
        return None

    async def _get_backfill_thresholds(
        self, as_of_date: date
    ) -> dict[int, date] | None:
        """计算各窗口的 judgment_date 阈值。

        返回 {1: date_T-1, 5: date_T-5, 10: date_T-10, 20: date_T-20}，
        其中 date_T-N 是 as_of_date 往前第 N 个交易日。
        如果交易日历不足则返回 None。

        Args:
            as_of_date: 截止日期。

        Returns:
            窗口到日期的映射，或 None。
        """
        # 取 as_of_date 之前（含）最近 21 个交易日，按降序
        rows = await db_query(
            """
            SELECT trade_date
            FROM trade_calendar
            WHERE trade_date <= $1
            ORDER BY trade_date DESC
            LIMIT 21
            """,
            as_of_date,
        )
        if len(rows) < 2:
            return None

        # rows[0] = as_of_date 或之前最近交易日
        # rows[N] = 倒推第 N 个交易日
        thresholds: dict[int, date] = {}
        for w in _RET_WINDOWS:
            if w < len(rows):
                thresholds[w] = rows[w]["trade_date"]
            else:
                # 不够天数的窗口用最早可用日期
                thresholds[w] = rows[-1]["trade_date"]

        return thresholds

    async def _compute_returns_for_judgment(
        self,
        j: Any,
        thresholds: dict[int, date],
        as_of_date: date,
    ) -> tuple[Any, ...] | None:
        """为单条判断计算收益率及误差分类。

        Args:
            j: 判断记录 (asyncpg.Record)，需含 technical_score, fundamental_score,
               error_category 字段。
            thresholds: 各窗口的日期阈值。
            as_of_date: 截止日期。

        Returns:
            更新参数元组 (ret_1d, ret_5d, ret_10d, ret_20d, max_up_20d,
            max_dd_20d, is_correct, error_category, reviewed_at, id)，
            未改变的字段为 None。
        """
        symbol: str = j["symbol"]
        jd: date = j["judgment_date"]

        # 获取 T 日收盘价: judgment_date 当天或之前最近的实际行情
        base_bar = await db_query_one(
            """
            SELECT trade_date, close FROM market_bars_daily
            WHERE symbol = $1 AND trade_date <= $2
            ORDER BY trade_date DESC LIMIT 1
            """,
            symbol, jd,
        )
        if not base_bar or float(base_bar["close"]) == 0:
            logger.warning("backfill_missing_base_bar", symbol=symbol, judgment_date=str(jd))
            return None

        base_date = base_bar["trade_date"]
        close_t = float(base_bar["close"])

        # 获取 base_date 之后 20 个交易日的行情
        future_bars = await db_query(
            """
            SELECT trade_date, close, high, low
            FROM market_bars_daily
            WHERE symbol = $1 AND trade_date > $2
            ORDER BY trade_date LIMIT 20
            """,
            symbol, base_date,
        )

        if not future_bars:
            return None

        # 构建有序列表
        bar_list = [{"close": float(b["close"]), "high": float(b["high"]), "low": float(b["low"])} for b in future_bars]
        if close_t == 0:
            return None

        # 计算各窗口收益率 (bar_list[0] = T+1, bar_list[1] = T+2, ...)
        ret_1d = None
        ret_5d = None
        ret_10d = None
        ret_20d = None
        max_up_20d = None
        max_dd_20d = None

        available_days = len(bar_list)  # T 之后的交易日数

        for w in _RET_WINDOWS:
            if j[f"actual_ret_{w}d"] is not None:
                continue
            if w > available_days:
                continue
            if jd > thresholds[w]:
                continue

            # bar_list[w-1] = T+w 的数据
            ret_val = round(bar_list[w - 1]["close"] / close_t - 1, 6)
            if w == 1:
                ret_1d = ret_val
            elif w == 5:
                ret_5d = ret_val
            elif w == 10:
                ret_10d = ret_val
            elif w == 20:
                ret_20d = ret_val

        # 计算区间内的最大涨幅和最大回撤
        if j["actual_max_up_20d"] is None or j["actual_max_dd_20d"] is None:
            max_high = max((b["high"] for b in bar_list), default=None)
            min_low = min((b["low"] for b in bar_list), default=None)

            if available_days >= 20 and jd <= thresholds[20]:
                if max_high is not None and j["actual_max_up_20d"] is None:
                    max_up_20d = round(max_high / close_t - 1, 6)
                if min_low is not None and j["actual_max_dd_20d"] is None:
                    max_dd_20d = round(min_low / close_t - 1, 6)

        # 判定 is_correct（需要 ret_10d 有值）
        is_correct = None
        error_category: str | None = None
        reviewed_at = None
        if j["is_correct"] is None:
            # 优先使用新计算的 ret_10d，否则使用已有的
            r10 = ret_10d if ret_10d is not None else (
                float(j["actual_ret_10d"]) if j["actual_ret_10d"] is not None else None
            )
            if r10 is not None:
                direction: str = j["direction"]
                if direction == "bullish":
                    is_correct = r10 > 0
                elif direction == "bearish":
                    is_correct = r10 < 0
                elif direction == "neutral":
                    is_correct = abs(r10) < 0.03
                else:
                    is_correct = None

                if is_correct is not None:
                    from datetime import datetime, timezone

                    reviewed_at = datetime.now(timezone.utc)

                # 仅在判断错误且尚未分类时执行误差分类
                if is_correct is False and j["error_category"] is None:
                    try:
                        error_category = await self._classify_error(
                            j, bar_list, as_of_date
                        )
                    except Exception:
                        logger.exception(
                            "classify_error_failed",
                            judgment_id=j["id"],
                            symbol=j["symbol"],
                        )

        # 如果什么都没更新，跳过
        has_update = any(
            v is not None
            for v in [
                ret_1d, ret_5d, ret_10d, ret_20d,
                max_up_20d, max_dd_20d, is_correct, error_category,
            ]
        )
        if not has_update:
            return None

        return (
            ret_1d,
            ret_5d,
            ret_10d,
            ret_20d,
            max_up_20d,
            max_dd_20d,
            is_correct,
            error_category,
            reviewed_at,
            j["id"],
        )
