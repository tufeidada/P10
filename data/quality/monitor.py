"""
数据质量监控模块

职责:
    - 新鲜度检查: 各数据源的最新数据是否在预期范围
    - 完整性检查: 候选池中每只票是否有完整数据
    - 异常检测: 价格跳变、成交量为 0、涨跌幅异常等
    - 结果写入 data_quality_checks 表
    - 严重问题通过 Telegram 推送告警

调度:
    每日 07:30 全量检查 (盘前)
    盘中每小时增量检查 (新鲜度)

Usage:
    monitor = DataQualityMonitor()
    report = await monitor.run_all_checks()
    await monitor.push_alerts_if_needed(report)
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import Any

import structlog

from db.connection import db_execute, db_query, db_query_one, db_query_val

logger = structlog.get_logger(__name__)


class CheckResult:
    """单条检查结果。"""

    def __init__(
        self,
        source_name: str,
        check_type: str,
        status: str,
        detail: dict[str, Any] | None = None,
        latest_date: date | None = None,
        expected_date: date | None = None,
    ):
        self.source_name = source_name
        self.check_type = check_type
        self.status = status  # 'ok' | 'warning' | 'critical'
        self.detail = detail or {}
        self.latest_date = latest_date
        self.expected_date = expected_date

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_name": self.source_name,
            "check_type": self.check_type,
            "status": self.status,
            "detail": self.detail,
            "latest_date": self.latest_date,
            "expected_date": self.expected_date,
        }


class DataQualityMonitor:
    """数据质量监控器。"""

    def __init__(self) -> None:
        self._results: list[CheckResult] = []

    async def run_all_checks(self) -> list[CheckResult]:
        """执行所有数据质量检查。"""
        self._results = []
        logger.info("dq_check_start")

        last_trade = await self._get_last_trade_date()

        await self.check_freshness(last_trade)
        await self.check_completeness(last_trade)
        await self.check_anomalies(last_trade)

        # 写入数据库
        await self._save_results()

        ok = sum(1 for r in self._results if r.status == "ok")
        warn = sum(1 for r in self._results if r.status == "warning")
        crit = sum(1 for r in self._results if r.status == "critical")
        logger.info("dq_check_done", ok=ok, warning=warn, critical=crit)

        return self._results

    async def check_freshness(self, last_trade: date | None) -> None:
        """检查各数据表的最新数据是否跟上交易日。"""
        if last_trade is None:
            self._results.append(CheckResult(
                source_name="trade_calendar",
                check_type="freshness",
                status="critical",
                detail={"message": "交易日历为空，无法判断预期日期"},
            ))
            return

        # 需要检查的表及其日期列
        tables_to_check = [
            ("market_bars_daily", "trade_date", 0),     # 允许 0 天延迟
            ("features_daily", "trade_date", 0),
            ("fundamentals_daily", "trade_date", 1),     # 允许 1 天延迟
            ("moneyflow_daily", "trade_date", 1),
        ]

        for table, date_col, tolerance_days in tables_to_check:
            try:
                latest = await db_query_val(
                    f"SELECT MAX({date_col}) FROM {table}"
                )
                expected = last_trade - timedelta(days=tolerance_days)

                if latest is None:
                    status = "warning"
                    msg = f"{table} 表为空"
                elif latest < expected:
                    days_behind = (expected - latest).days
                    status = "critical" if days_behind > 3 else "warning"
                    msg = f"数据滞后 {days_behind} 天"
                else:
                    status = "ok"
                    msg = "数据正常"

                self._results.append(CheckResult(
                    source_name=table,
                    check_type="freshness",
                    status=status,
                    detail={"message": msg},
                    latest_date=latest,
                    expected_date=expected,
                ))

            except Exception as e:
                logger.warning("dq_freshness_error", table=table, error=str(e))
                self._results.append(CheckResult(
                    source_name=table,
                    check_type="freshness",
                    status="warning",
                    detail={"message": f"检查失败: {e}"},
                ))

    async def check_completeness(self, last_trade: date | None) -> None:
        """检查候选池中每只票是否有当日完整数据。"""
        if last_trade is None:
            return

        try:
            # 获取活跃候选池
            universe = await db_query(
                "SELECT symbol, market FROM stock_universe WHERE active = TRUE"
            )

            if not universe:
                self._results.append(CheckResult(
                    source_name="stock_universe",
                    check_type="completeness",
                    status="ok",
                    detail={"message": "候选池为空，跳过完整性检查"},
                ))
                return

            missing_bars = []
            missing_features = []

            for row in universe:
                symbol = row["symbol"]
                market = row["market"]

                # 只检查 A 股的日线（美股数据源不同步）
                if market == "CN":
                    has_bar = await db_query_val(
                        """
                        SELECT EXISTS(
                            SELECT 1 FROM market_bars_daily
                            WHERE symbol = $1 AND trade_date = $2
                        )
                        """,
                        symbol, last_trade,
                    )
                    if not has_bar:
                        missing_bars.append(symbol)

                    has_feat = await db_query_val(
                        """
                        SELECT EXISTS(
                            SELECT 1 FROM features_daily
                            WHERE symbol = $1 AND trade_date = $2
                        )
                        """,
                        symbol, last_trade,
                    )
                    if not has_feat:
                        missing_features.append(symbol)

            total_cn = sum(1 for r in universe if r["market"] == "CN")

            if missing_bars:
                status = "critical" if len(missing_bars) > total_cn * 0.1 else "warning"
                self._results.append(CheckResult(
                    source_name="market_bars_daily",
                    check_type="completeness",
                    status=status,
                    detail={
                        "message": f"{len(missing_bars)}/{total_cn} 只票缺少日线",
                        "missing_symbols": missing_bars[:10],
                    },
                    expected_date=last_trade,
                ))
            else:
                self._results.append(CheckResult(
                    source_name="market_bars_daily",
                    check_type="completeness",
                    status="ok",
                    detail={"message": f"候选池 {total_cn} 只A股日线完整"},
                    expected_date=last_trade,
                ))

            if missing_features:
                self._results.append(CheckResult(
                    source_name="features_daily",
                    check_type="completeness",
                    status="warning",
                    detail={
                        "message": f"{len(missing_features)}/{total_cn} 只票缺少特征",
                        "missing_symbols": missing_features[:10],
                    },
                    expected_date=last_trade,
                ))

        except Exception as e:
            logger.warning("dq_completeness_error", error=str(e))
            self._results.append(CheckResult(
                source_name="stock_universe",
                check_type="completeness",
                status="warning",
                detail={"message": f"完整性检查失败: {e}"},
            ))

    async def check_anomalies(self, last_trade: date | None) -> None:
        """检查数据异常: 价格跳变、成交量为 0。"""
        if last_trade is None:
            return

        try:
            # 检查涨跌幅超过 20% 的个股（可能是数据错误或特殊情况）
            anomalies = await db_query(
                """
                WITH latest AS (
                    SELECT symbol,
                           close,
                           LAG(close) OVER (PARTITION BY symbol ORDER BY trade_date) AS prev_close
                    FROM market_bars_daily
                    WHERE trade_date >= $1::date - 5
                )
                SELECT symbol,
                       ABS(close / NULLIF(prev_close, 0) - 1) AS ret
                FROM latest
                WHERE ABS(close / NULLIF(prev_close, 0) - 1) > 0.20
                LIMIT 20
                """,
                last_trade,
            )

            if anomalies:
                symbols = [f"{r['symbol']}({r['ret']:.1%})" for r in anomalies]
                self._results.append(CheckResult(
                    source_name="market_bars_daily",
                    check_type="anomaly",
                    status="warning",
                    detail={
                        "message": f"{len(anomalies)} 只票涨跌幅超20%",
                        "symbols": symbols[:10],
                    },
                    latest_date=last_trade,
                ))
            else:
                self._results.append(CheckResult(
                    source_name="market_bars_daily",
                    check_type="anomaly",
                    status="ok",
                    detail={"message": "无价格异常"},
                    latest_date=last_trade,
                ))

            # 检查成交量为 0 的交易日数据
            zero_vol = await db_query_val(
                """
                SELECT COUNT(*) FROM market_bars_daily
                WHERE trade_date = $1 AND volume = 0
                """,
                last_trade,
            )
            if zero_vol and zero_vol > 0:
                self._results.append(CheckResult(
                    source_name="market_bars_daily",
                    check_type="anomaly",
                    status="warning" if zero_vol < 50 else "critical",
                    detail={"message": f"{zero_vol} 条记录成交量为 0"},
                    latest_date=last_trade,
                ))

        except Exception as e:
            logger.warning("dq_anomaly_error", error=str(e))

    async def push_alerts_if_needed(
        self, results: list[CheckResult] | None = None
    ) -> None:
        """如果有 warning 或 critical，通过 Telegram 推送告警。"""
        results = results or self._results
        alerts = [r for r in results if r.status in ("warning", "critical")]

        if not alerts:
            return

        from bot.telegram_bot import TelegramPusher
        from bot.formatter import format_dq_report

        pusher = TelegramPusher()
        text = format_dq_report([a.to_dict() for a in alerts])
        await pusher.send_html(text)

    # ============================================================
    # 内部方法
    # ============================================================

    async def _get_last_trade_date(self) -> date | None:
        """获取最近的交易日。"""
        row = await db_query_val(
            "SELECT MAX(trade_date) FROM trade_calendar WHERE trade_date <= CURRENT_DATE"
        )
        return row

    async def _save_results(self) -> None:
        """将检查结果写入数据库。"""
        for r in self._results:
            try:
                await db_execute(
                    """
                    INSERT INTO data_quality_checks
                        (source_name, check_type, status, detail, latest_date, expected_date)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                    r.source_name,
                    r.check_type,
                    r.status,
                    json.dumps(r.detail, ensure_ascii=False, default=str),
                    r.latest_date,
                    r.expected_date,
                )
            except Exception as e:
                logger.warning("dq_save_error", source=r.source_name, error=str(e))
