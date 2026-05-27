"""
APScheduler 调度器

按架构文档第七章的时间表调度所有定时任务。
每个任务函数是 async 的，由 AsyncIOScheduler 驱动。

启动方式:
    python -m scheduler.scheduler

调度表概览:
    A股 (Asia/Shanghai)
      07:30  data_quality_check        数据新鲜度+完整性检查
      08:00  pre_market_analysis       盘前多维分析
      08:30  pre_market_push           推送盘前摘要
      09:30-15:00 每15min  intraday    盘中矫正+买卖点
      09:30-15:00 每30min  regime_pulse盘中regime微调
      15:10  post_market_summary       盘后汇总
      15:30  data_pipeline_pull        拉取日线/财报/资金流
      15:45  feature_compute           计算/更新特征
      16:00  regime_update             更新regime
      16:10  backfill_judgments        回填判断实际结果
      16:20  backfill_signals          回填信号实际结果
      16:30  signal_quality_update     更新信号质量
      16:40  post_market_push          推送盘后复盘
      周六10:00  weekly_review         周度复盘
      周六10:30  scanner_weekly        技术形态扫描
      每月1日10:00  monthly_review     月度复盘

    美股 (Asia/Shanghai 时间)
      21:00  us_pre_market             美股盘前分析
      21:30-04:00 每15min  us_intraday 美股盘中监控
      04:30  us_post_market            美股盘后汇总
      05:00  us_data_pull              拉取美股日线
      05:15  us_regime_update          更新美股regime

    跨市场
      每日06:00  social_sentiment_scan 社交情绪扫描
      每周日10:00  wiki_lint           Wiki健康检查
      每周一08:00  macro_update        更新宏观指标
"""

from __future__ import annotations

import asyncio
import os

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from core.invariants import InvariantViolation
from db.connection import close_pool, db_query_val, init_pool

logger = structlog.get_logger(__name__)

_INVARIANT_LOG = "logs/invariant_violations.log"


def _handle_invariant(err: InvariantViolation) -> None:
    """记录不变量违规日志并推送 Telegram 告警（同步接口，供 job 异常处理调用）。

    Args:
        err: InvariantViolation 实例。
    """
    import asyncio
    from datetime import datetime
    from pathlib import Path

    msg = str(err)
    logger.error("invariant_violation_scheduler", error=msg)

    log_path = Path(_INVARIANT_LOG)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now().isoformat()}] [scheduler] {msg}\n")

    # 异步推送（创建独立 task，不阻塞当前协程）
    async def _push() -> None:
        try:
            from bot.telegram_bot import TelegramPusher
            pusher = TelegramPusher()
            await pusher.send(f"🚨 <b>INVARIANT VIOLATION [scheduler]</b>\n<code>{msg}</code>")
        except Exception as e:
            logger.error("invariant_alert_push_failed", error=str(e))

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(_push())
    except Exception:
        pass

TZ_CN = "Asia/Shanghai"


# ============================================================
# Job 框架：safe_run_job + 心跳
# ============================================================

async def safe_run_job(job_name: str, job_func) -> None:
    """统一 job 包装器：计时、记录 job_log、处理异常。

    捕获所有异常类型（含 CancelledError / BaseException），确保 job_log 总有记录。
    CancelledError 必须 re-raise，让 APScheduler 正确感知取消。

    Args:
        job_name: job 标识名（写入 scheduler_job_log）。
        job_func: 无参数的 async 函数。
    """
    import time as _time
    from db.job_log import log_job

    start = _time.monotonic()
    try:
        await job_func()
        duration_ms = int((_time.monotonic() - start) * 1000)
        await log_job(job_name, "success", duration_ms)
        logger.info("job_success", job=job_name, duration_ms=duration_ms)
    except InvariantViolation as e:
        duration_ms = int((_time.monotonic() - start) * 1000)
        await log_job(job_name, "invariant", duration_ms, str(e))
        _handle_invariant(e)
        raise
    except asyncio.CancelledError:
        duration_ms = int((_time.monotonic() - start) * 1000)
        await log_job(job_name, "cancelled", duration_ms, "CancelledError")
        logger.warning("job_cancelled", job=job_name, duration_ms=duration_ms)
        raise  # APScheduler / asyncio 要求 CancelledError 必须传播
    except Exception as e:
        duration_ms = int((_time.monotonic() - start) * 1000)
        err_msg = f"{type(e).__name__}: {e}"
        await log_job(job_name, "failed", duration_ms, err_msg)
        logger.error("job_failed", job=job_name, error=err_msg)
        try:
            from bot.telegram_bot import TelegramPusher
            await TelegramPusher().send(f"⚠️ <b>{job_name}</b> 失败\n<code>{err_msg}</code>")
        except Exception:
            pass
    except BaseException as e:
        # SystemExit、KeyboardInterrupt 等 — 记录后必须 re-raise
        duration_ms = int((_time.monotonic() - start) * 1000)
        err_msg = f"{type(e).__name__}: {e}"
        try:
            await log_job(job_name, "fatal", duration_ms, err_msg)
        except Exception:
            pass
        logger.critical("job_fatal", job=job_name, error=err_msg)
        raise


async def task_heartbeat() -> None:
    """每 30 分钟写心跳记录到 scheduler_heartbeat 表。"""
    try:
        from db.job_log import write_heartbeat
        jobs_count = len(_scheduler_ref.get_jobs()) if _scheduler_ref else 0
        await write_heartbeat(jobs_count)
    except Exception as e:
        logger.error("heartbeat_error", error=str(e))


async def task_scheduler_self_check() -> None:
    """每 5 分钟 — 检查过去 35 分钟内心跳是否正常，若无则推送告警。

    APScheduler 心跳每 30 分钟写一次；超过 35 分钟无心跳则认为事件循环卡死。
    """
    try:
        from db.connection import db_query_val
        from datetime import datetime, timedelta, timezone

        cutoff = datetime.now(timezone.utc) - timedelta(minutes=35)
        latest_beat = await db_query_val(
            "SELECT MAX(beat_time) FROM scheduler_heartbeat"
        )
        if latest_beat is None or latest_beat < cutoff:
            age = (datetime.now(timezone.utc) - latest_beat).seconds // 60 if latest_beat else 999
            msg = (
                f"🚨 <b>SCHEDULER HEARTBEAT STALE</b>\n"
                f"最近心跳 {age} 分钟前，事件循环可能已停止\n"
                f"请检查 scheduler 进程"
            )
            logger.error("scheduler_self_check_stale", last_beat=str(latest_beat), age_min=age)
            try:
                from bot.telegram_bot import TelegramPusher
                await TelegramPusher().send(msg)
            except Exception:
                pass
        else:
            logger.debug("scheduler_self_check_ok", last_beat=str(latest_beat))
    except Exception as e:
        logger.warning("scheduler_self_check_error", error=str(e))


# 全局 scheduler 引用（由 _run() 写入，heartbeat 读取）
_scheduler_ref = None


# ============================================================
# 依赖检查辅助函数
# ============================================================

async def _today_data_exists(table: str, date_col: str, market: str | None = None) -> bool:
    """检查指定表今日是否已有数据（用于 job 依赖检查）。

    Args:
        table: 表名。
        date_col: 日期列名。
        market: 如果指定，附加 market 过滤。

    Returns:
        今日有数据则返回 True。
    """
    from datetime import date as _date
    from db.connection import db_query_val
    today = _date.today()
    if market:
        sql = f"SELECT MAX({date_col}) FROM {table} WHERE market = $1"
        latest = await db_query_val(sql, market)
    else:
        sql = f"SELECT MAX({date_col}) FROM {table}"
        latest = await db_query_val(sql)
    return latest is not None and latest >= today


# ============================================================
# 任务函数
# ============================================================

async def task_data_quality_check() -> None:
    """07:30 — 数据完整性检查（旧监控，保留兼容）。"""
    logger.info("task_start", task="data_quality_check")
    try:
        from data.quality.monitor import DataQualityMonitor
        monitor = DataQualityMonitor()
        results = await monitor.run_all_checks()
        await monitor.push_alerts_if_needed(results)
    except Exception as e:
        logger.error("task_error", task="data_quality_check", error=str(e))


async def _check_data_freshness() -> None:
    """数据新鲜度监控核心逻辑。"""
    from scripts.data_freshness_check import run_all_checks, push_critical_alerts
    results = await run_all_checks()
    ok = sum(1 for r in results if r["status"] == "ok")
    warn = sum(1 for r in results if r["status"] == "warn")
    critical = sum(1 for r in results if r["status"] == "critical")
    logger.info("freshness_check_done", total=len(results), ok=ok, warn=warn, critical=critical)
    await push_critical_alerts(results)


async def task_check_data_freshness() -> None:
    """08:00 — M5 数据新鲜度监控（data_source_expectations）。"""
    await safe_run_job("check_data_freshness", _check_data_freshness)


async def task_pre_market_analysis() -> None:
    """08:00 — 盘前多维分析（Phase 1 实现）。"""
    logger.info("task_start", task="pre_market_analysis")
    try:
        from core.analysis.composite import CompositeAnalyzer
        analyzer = CompositeAnalyzer()
        results = await analyzer.analyze_universe(market="CN")
        logger.info("pre_market_analysis_done", count=len(results))
    except Exception as e:
        logger.error("task_error", task="pre_market_analysis", error=str(e))


async def task_pre_market_push() -> None:
    """08:30 — 推送盘前分析摘要。"""
    logger.info("task_start", task="pre_market_push")
    # TODO: Phase 1 — 取高置信度判断，格式化推送


async def task_intraday_calibration() -> None:
    """09:30-15:00 每15分钟 — 盘中矫正 + 买卖点检测。"""
    from datetime import datetime, time
    import pytz

    # Only run during A-share trading hours
    cn_tz = pytz.timezone("Asia/Shanghai")
    now = datetime.now(cn_tz).time()
    if not (time(9, 30) <= now <= time(15, 0)):
        return

    logger.info("task_start", task="intraday_calibration")
    try:
        from data.pipeline.intraday_pull import IntradayPuller
        from core.intraday.factors import FactorCalculator
        from core.intraday.signal_detector import SignalDetector
        from core.intraday.calibrator import IntradayCalibrator
        from core.intraday.push import SignalPusher
        from db.connection import db_query_one

        puller = IntradayPuller()
        factor_calc = FactorCalculator()
        detector = SignalDetector()
        calibrator = IntradayCalibrator()
        pusher = SignalPusher()

        # Get active universe
        symbols = await puller.get_active_universe()
        if not symbols:
            logger.info("intraday_no_symbols")
            return

        # Pull data for all symbols
        pull_result = await puller.pull_and_save(symbols)

        # Process each symbol
        for symbol in symbols:
            try:
                sym_data = pull_result.get(symbol, {})
                bars_data: list = sym_data.get("bars_list", [])
                quote_data: dict | None = sym_data.get("quote")

                if not bars_data:
                    continue

                # Compute factors
                factors = await factor_calc.compute(symbol, bars_data, quote_data)

                # Check for signals
                signal = await detector.detect(symbol, "CN", factors)
                if signal:
                    signal_id = await detector.save_signal(signal)

                    # Push if strong/moderate
                    if signal.strength in ("strong", "moderate"):
                        row = await db_query_one(
                            "SELECT name FROM stock_universe WHERE symbol=$1", symbol
                        )
                        name = row["name"] if row else None

                        if signal.signal_type == "buy":
                            # Fetch full judgment dict for push formatting
                            judgment: dict | None = None
                            try:
                                j_row = await db_query_one(
                                    """
                                    SELECT id, direction, confidence, judgment_date,
                                           entry_zone_low, entry_zone_high, stop_loss
                                    FROM judgments
                                    WHERE symbol=$1
                                    ORDER BY judgment_date DESC, id DESC
                                    LIMIT 1
                                    """,
                                    symbol,
                                )
                                judgment = dict(j_row) if j_row else None
                            except Exception:
                                pass
                            await pusher.push_buy_signal(symbol, name, signal, judgment)
                        else:
                            position = await detector._get_open_position(symbol)
                            await pusher.push_sell_signal(symbol, name, signal, position)

                # Check calibration
                await calibrator.check_and_calibrate(symbol, "CN", factors)

            except Exception as e:
                logger.warning("intraday_symbol_error", symbol=symbol, error=str(e))

        logger.info("intraday_done", symbols=len(symbols))
    except Exception as e:
        logger.error("task_error", task="intraday_calibration", error=str(e))


async def task_regime_pulse() -> None:
    """09:30-15:00 每30分钟 — 盘中 regime 微调。"""
    logger.info("task_start", task="regime_pulse")
    # TODO: Phase 1 — core/regime/detector.py (partial update)


async def task_post_market_summary() -> None:
    """15:10 — 盘后汇总。"""
    logger.info("task_start", task="post_market_summary")
    # TODO: Phase 1


async def task_data_pipeline_pull() -> None:
    """15:30 — 拉取A股日线/财报/资金流。"""
    logger.info("task_start", task="data_pipeline_pull")
    # TODO: Phase 1 — data/pipeline/daily_pull.py


async def task_feature_compute() -> None:
    """15:45 — 计算/更新日线特征（CN + US）。

    Task 4.3: 对 stock_universe 所有 active 股票计算当日 features。
    Task 4.4: 连续 3 天失败的股票升级为 critical 告警 + 降级处理。
    """
    from datetime import date as _date
    from core.invariants import assert_fresh
    from db.connection import db_query_val
    from db.feature_log import (
        log_feature_results,
        get_degraded_symbols,
        get_daily_credit_total,
        check_credit_budget,
    )
    from db.universe import get_active_symbols
    from data.pipeline.feature_compute import FeatureComputer

    logger.info("task_start", task="feature_compute")
    trade_date = _date.today()
    computer = FeatureComputer()
    all_failed: dict[str, list[str]] = {}  # market -> degraded symbols

    for market in ("CN", "US"):
        symbols = await get_active_symbols(market)
        if not symbols:
            logger.warning("feature_compute_skip", market=market, reason="empty_universe")
            continue

        logger.info("feature_compute_market_start", market=market, symbols=len(symbols))

        # 逐只计算；FeatureComputer 内部已做异常隔离（互不影响）
        results = await computer.compute_for_symbols(symbols, market, trade_date)

        # 收集错误信息（FeatureComputer 返回 bool，错误在日志里；此处标记 False 为失败）
        errors = {sym: "compute_error" for sym, ok in results.items() if not ok}

        # Task 4.3: 写 feature_update_log
        await log_feature_results(trade_date, market, results, errors)

        # 失败率告警（> 20% 触发 warn）
        failed_count = sum(1 for ok in results.values() if not ok)
        fail_rate = failed_count / len(results) if results else 0
        if fail_rate > 0.2:
            msg = f"⚠️ feature_compute {market} 失败率过高: {failed_count}/{len(results)} ({fail_rate:.0%})"
            logger.warning("feature_compute_high_failure", market=market,
                           failed=failed_count, total=len(results))
            try:
                from bot.telegram_bot import TelegramPusher
                await TelegramPusher().send(msg)
            except Exception:
                pass

        # Task 4.4: 连续 3 天失败 → critical 告警 + 记录降级列表
        degraded = await get_degraded_symbols(market, trade_date)
        if degraded:
            all_failed[market] = degraded
            msg = (
                f"🚨 <b>FEATURE DEGRADED [{market}]</b>\n"
                f"以下股票连续 3 天 feature 更新失败，composite 将跳过：\n"
                f"<code>{', '.join(degraded)}</code>"
            )
            logger.error("feature_compute_degraded", market=market, symbols=degraded)
            try:
                from bot.telegram_bot import TelegramPusher
                await TelegramPusher().send(msg)
            except Exception:
                pass

    # Tushare 积分预算检查（数据由 task_data_pipeline_pull 写入；这里检查汇总）
    try:
        exceeded = await check_credit_budget(trade_date)
        if exceeded:
            total = await get_daily_credit_total(trade_date)
            msg = f"⚠️ Tushare 今日积分消耗 {total}，已超过预算 500，请检查数据拉取任务。"
            from bot.telegram_bot import TelegramPusher
            await TelegramPusher().send(msg)
    except Exception as e:
        logger.warning("credit_check_error", error=str(e))

    # M3 assert_fresh：features_daily 最新日期不超过 3 天
    try:
        latest_date = await db_query_val("SELECT MAX(trade_date) FROM features_daily")
        assert_fresh(latest_date, max_age_days=3, context="data_source.features_daily")
        logger.info("task_done", task="feature_compute", latest_date=str(latest_date),
                    degraded=all_failed)
    except InvariantViolation as e:
        _handle_invariant(e)
        raise


async def task_regime_update() -> None:
    """16:00 — 更新 A 股 regime（全维度）。"""
    logger.info("task_start", task="regime_update")
    try:
        from core.regime import detect_regime
        regime = await detect_regime(market="CN")
        logger.info("regime_updated", mode=regime.regime_mode, trend=regime.trend_score)
    except Exception as e:
        logger.error("task_error", task="regime_update", error=str(e))


async def _backfill_judgments() -> None:
    """回填判断实际结果核心逻辑。"""
    from core.evolution.judgment_tracker import JudgmentTracker
    tracker = JudgmentTracker()
    stats = await tracker.backfill_all()
    logger.info("backfill_done", **stats)


async def task_backfill_judgments() -> None:
    """16:10 — 回填 T-5/T-10/T-20 判断的实际结果。"""
    await safe_run_job("backfill_judgments", _backfill_judgments)


async def task_backfill_signals() -> None:
    """16:20 — 回填盘中信号的实际结果。"""
    logger.info("task_start", task="backfill_signals")
    # TODO: Phase 3 — core/evolution/judgment_tracker.py


async def task_signal_quality_update() -> None:
    """16:30 — 更新信号质量追踪器。"""
    logger.info("task_start", task="signal_quality_update")
    try:
        from core.evolution.signal_quality import SignalQualityTracker
        tracker = SignalQualityTracker()
        result = await tracker.run_all()
        logger.info("signal_quality_done", **result)
    except Exception as e:
        logger.error("task_error", task="signal_quality_update", error=str(e))


async def task_post_market_push() -> None:
    """16:40 — 推送盘后复盘。"""
    logger.info("task_start", task="post_market_push")
    try:
        from bot.telegram_bot import TelegramPusher
        from db.connection import db_query
        from datetime import date

        rows = await db_query(
            "SELECT symbol, direction, composite_score, confidence "
            "FROM judgments WHERE judgment_date = $1 "
            "ORDER BY composite_score DESC LIMIT 5",
            date.today(),
        )
        if not rows:
            logger.info("post_market_push_skip", reason="no_judgments_today")
            return

        lines = ["📋 <b>盘后分析摘要</b>\n"]
        for r in rows:
            emoji = {"bullish": "🟢", "bearish": "🔴", "neutral": "🟡"}.get(
                r["direction"], "⚪"
            )
            lines.append(
                f"{emoji} {r['symbol']} — {r['composite_score']:.0f}分 ({r['direction']})"
            )

        pusher = TelegramPusher()
        await pusher.send_html("\n".join(lines))
        logger.info("post_market_push_done", count=len(rows))
    except Exception as e:
        logger.error("task_error", task="post_market_push", error=str(e))


async def _weekly_review() -> None:
    """周度复盘核心逻辑。"""
    from core.evolution.reviewer import Reviewer
    reviewer = Reviewer()
    report_id = await reviewer.run_weekly_review(market="CN")
    logger.info("weekly_review_done", report_id=report_id)


async def task_weekly_review() -> None:
    """周六 10:00 — 周度复盘报告。"""
    await safe_run_job("weekly_review", _weekly_review)


async def task_scanner_weekly() -> None:
    """周六 10:30 — 技术形态扫描。"""
    logger.info("task_start", task="scanner_weekly")
    # TODO: Phase 6 — core/scanner/technical_scanner.py


async def _monthly_review() -> None:
    """月度复盘核心逻辑。"""
    from core.evolution.reviewer import Reviewer
    reviewer = Reviewer()
    report_id = await reviewer.run_monthly_review(market="CN")
    logger.info("monthly_review_done", report_id=report_id)
    result = await reviewer.validate_experiences(market="CN")
    logger.info("experience_validation_done", **result)


async def task_monthly_review() -> None:
    """每月1日 10:00 — 月度复盘 + 经验验证 + 权重建议。"""
    await safe_run_job("monthly_review", _monthly_review)


async def task_us_pre_market() -> None:
    """21:00 — 美股盘前分析。"""
    logger.info("task_start", task="us_pre_market")
    try:
        from core.analysis.composite import CompositeAnalyzer
        ca = CompositeAnalyzer()
        results = await ca.analyze_universe(market="US")
        logger.info("us_pre_market_done", count=len(results))
    except Exception as e:
        logger.error("task_error", task="us_pre_market", error=str(e))


async def task_us_intraday() -> None:
    """21:30-04:00 每15分钟 — 美股盘中监控。"""
    logger.info("task_start", task="us_intraday")
    # TODO: Phase 4 — core/intraday/ US market support


async def task_us_post_market() -> None:
    """04:30 — 美股盘后汇总。"""
    logger.info("task_start", task="us_post_market")
    # TODO: Phase 4


async def task_us_data_pull() -> None:
    """05:00 — 拉取美股日线数据。"""
    logger.info("task_start", task="us_data_pull")
    try:
        from data.pipeline.us_data_pull import USDataPuller
        from datetime import date, timedelta
        puller = USDataPuller()
        symbols = await puller.get_us_universe()
        if symbols:
            end = date.today().strftime("%Y-%m-%d")
            start = (date.today() - timedelta(days=7)).strftime("%Y-%m-%d")
            result = await puller.pull_daily_bars(symbols, start, end)
            logger.info("us_data_pull_done", symbols=len(symbols), result=result)
        else:
            logger.warning("us_data_pull_skip", reason="empty_universe")
    except Exception as e:
        logger.error("task_error", task="us_data_pull", error=str(e))


async def task_us_regime_update() -> None:
    """05:15 — 更新美股 regime。"""
    logger.info("task_start", task="us_regime_update")
    try:
        from core.regime import detect_regime
        regime = await detect_regime(market="US")
        logger.info("us_regime_updated", mode=regime.regime_mode)
    except Exception as e:
        logger.error("task_error", task="us_regime_update", error=str(e))


async def task_social_sentiment_scan() -> None:
    """每日 06:00 — 社交情绪扫描。"""
    logger.info("task_start", task="social_sentiment_scan")
    # TODO: Phase 4


async def task_wiki_lint() -> None:
    """每周日 10:00 — Wiki 健康检查。"""
    logger.info("task_start", task="wiki_lint")
    # TODO: Phase 2


async def task_macro_update() -> None:
    """每周一 08:00 — 更新宏观指标。"""
    logger.info("task_start", task="macro_update")
    # TODO: Phase 2


# ============================================================
# M6 核心 Pipeline 任务（真实实现）
# ============================================================

async def _pull_cn_market_data() -> None:
    """CN 市场数据拉取核心逻辑（被 safe_run_job 包装）。

    滚动追补：查 DB 最新日期，补拉缺失的交易日（最多 N=3 天）。
    首次运行（表空）自动 bootstrap 30 天。
    """
    from datetime import date as _date, timedelta
    from data.pipeline.fundamental_pull import FundamentalPuller
    from data.pipeline.flow_pull import FlowPuller
    from data.sources.tushare_client import TushareClient
    from db.feature_log import log_tushare_credits
    from db.connection import db_query_val

    _ROLLING_DAYS = 3
    _BOOTSTRAP_DAYS = 30

    today = _date.today()

    # 确定需要拉取的起始日期
    db_max = await db_query_val(
        "SELECT MAX(trade_date) FROM market_bars_daily WHERE market = $1", "CN"
    )
    if db_max is None:
        target_start = today - timedelta(days=_BOOTSTRAP_DAYS)
        logger.info("cn_bars_bootstrap", start=str(target_start))
    else:
        target_start = max(today - timedelta(days=_ROLLING_DAYS), db_max + timedelta(days=1))

    if target_start > today:
        logger.info("cn_bars_already_current", db_max=str(db_max))
        return

    start_str = target_start.strftime("%Y%m%d")
    end_str = today.strftime("%Y%m%d")

    client = TushareClient()
    fund_puller = FundamentalPuller()
    flow_puller = FlowPuller()

    # 1. 日线行情（逐只拉取，合并后批量入库）
    import pandas as pd
    from db.universe import get_active_symbols
    symbols = await get_active_symbols("CN")
    if symbols:
        frames = []
        for sym in symbols:
            try:
                df_sym = await client.fetch_daily_bars(sym, start_str, end_str)
                if df_sym is not None and not df_sym.empty:
                    frames.append(df_sym)
            except Exception as sym_e:
                logger.warning("cn_bar_fetch_skip", symbol=sym, error=str(sym_e))

        if frames:
            df_all = pd.concat(frames, ignore_index=True)
            saved = await client.save_daily_bars(df_all, market="CN")
            await log_tushare_credits(today, "daily_bars", "pull_cn_market_data",
                                      len(symbols) * 2, len(symbols))
            logger.info("cn_bars_saved", rows=saved, start=start_str, end=end_str)
        else:
            logger.warning("cn_bars_empty", start=start_str, end=end_str)

    # 2. 每日基本面 + 资金流 — 逐日补拉缺失日期
    cur = target_start
    while cur <= today:
        date_str = cur.strftime("%Y%m%d")
        saved_basic = await fund_puller.pull_daily_basic(date_str)
        if saved_basic > 0:
            await log_tushare_credits(today, "daily_basic", "pull_cn_market_data",
                                      len(symbols), len(symbols))
        logger.info("cn_daily_basic_saved", rows=saved_basic, date=date_str)

        flow_result = await flow_puller.pull_all(date_str)
        logger.info("cn_flow_saved", result=flow_result, date=date_str)
        cur += timedelta(days=1)


async def task_pull_cn_market_data() -> None:
    """15:15 — 拉取 A 股日线、基本面、资金流。"""
    await safe_run_job("pull_cn_market_data", _pull_cn_market_data)


async def _pull_us_market_data() -> None:
    """US 市场数据拉取核心逻辑。

    滚动追补：查 DB 最新日期，补拉缺失的交易日（最多 N=3 天）。
    首次运行（表空）自动 bootstrap 30 天。
    """
    from datetime import date as _date, timedelta
    from data.pipeline.us_data_pull import USDataPuller
    from db.connection import db_query_val

    _ROLLING_DAYS = 3
    _BOOTSTRAP_DAYS = 30

    today = _date.today()
    puller = USDataPuller()
    symbols = await puller.get_us_universe()
    if not symbols:
        logger.warning("us_data_pull_skip", reason="empty_universe")
        return

    db_max = await db_query_val(
        "SELECT MAX(trade_date) FROM market_bars_daily WHERE market = $1", "US"
    )
    if db_max is None:
        target_start = today - timedelta(days=_BOOTSTRAP_DAYS)
        logger.info("us_bars_bootstrap", start=str(target_start))
    else:
        target_start = max(today - timedelta(days=_ROLLING_DAYS), db_max + timedelta(days=1))

    if target_start > today:
        logger.info("us_bars_already_current", db_max=str(db_max))
        return

    start = target_start.strftime("%Y-%m-%d")
    end = today.strftime("%Y-%m-%d")
    result = await puller.pull_daily_bars(symbols, start, end)
    logger.info("us_data_pull_done", symbols=len(symbols), start=start, end=end, result=result)


async def task_pull_us_market_data() -> None:
    """05:30 — 拉取美股日线（盘后）。"""
    await safe_run_job("pull_us_market_data", _pull_us_market_data)


async def _backfill_missing_bars() -> None:
    """扫描最近 30 天 market_bars_daily 缺口并补拉核心逻辑。

    逻辑：
    1. 从 trade_calendar 取最近 30 天所有交易日
    2. 对每个 active 股票，找出在 market_bars_daily 中缺失的交易日
    3. 按日期批量补拉（CN 逐只，US 批量）
    """
    from datetime import date as _date, timedelta
    import pandas as pd
    from data.sources.tushare_client import TushareClient
    from data.pipeline.us_data_pull import USDataPuller
    from db.universe import get_active_symbols
    from db.connection import db_query, db_query_val

    today = _date.today()
    lookback_start = today - timedelta(days=30)

    # 取 trade_calendar 中 CN 交易日（用于 CN 缺口检测）
    cn_trade_days = await db_query(
        "SELECT trade_date FROM trade_calendar WHERE trade_date >= $1 AND trade_date <= $2 ORDER BY trade_date",
        lookback_start, today,
    )
    cn_trade_day_set = {r["trade_date"] for r in cn_trade_days}

    total_filled_cn = 0
    total_filled_us = 0

    # ── CN ──
    cn_symbols = await get_active_symbols("CN")
    if cn_symbols and cn_trade_day_set:
        existing_cn = await db_query(
            "SELECT DISTINCT trade_date FROM market_bars_daily WHERE market='CN' AND trade_date >= $1",
            lookback_start,
        )
        existing_cn_set = {r["trade_date"] for r in existing_cn}
        missing_cn_dates = sorted(cn_trade_day_set - existing_cn_set)

        if missing_cn_dates:
            logger.info("backfill_cn_gaps", count=len(missing_cn_dates),
                        dates=[str(d) for d in missing_cn_dates])
            client = TushareClient()
            for gap_date in missing_cn_dates:
                date_str = gap_date.strftime("%Y%m%d")
                frames = []
                for sym in cn_symbols:
                    try:
                        df_sym = await client.fetch_daily_bars(sym, date_str, date_str)
                        if df_sym is not None and not df_sym.empty:
                            frames.append(df_sym)
                    except Exception as e:
                        logger.warning("backfill_cn_skip", symbol=sym, date=date_str, error=str(e))
                if frames:
                    df_all = pd.concat(frames, ignore_index=True)
                    saved = await client.save_daily_bars(df_all, market="CN")
                    total_filled_cn += saved
                    logger.info("backfill_cn_date_done", date=date_str, rows=saved)
        else:
            logger.info("backfill_cn_no_gaps")

    # ── US ──
    us_symbols = await get_active_symbols("US")
    if us_symbols:
        us_max = await db_query_val(
            "SELECT MAX(trade_date) FROM market_bars_daily WHERE market='US'"
        )
        if us_max is not None and us_max < today - timedelta(days=1):
            start_str = (us_max + timedelta(days=1)).strftime("%Y-%m-%d")
            end_str = today.strftime("%Y-%m-%d")
            logger.info("backfill_us_gap", start=start_str, end=end_str)
            puller = USDataPuller()
            result = await puller.pull_daily_bars(us_symbols, start_str, end_str)
            total_filled_us = result.get("saved", 0) if isinstance(result, dict) else 0
            logger.info("backfill_us_done", result=result)
        else:
            logger.info("backfill_us_no_gaps", us_max=str(us_max))

    logger.info("backfill_missing_bars_done",
                cn_rows=total_filled_cn, us_rows=total_filled_us)


async def task_backfill_missing_bars() -> None:
    """每周一 10:30 — 扫描并补拉最近 30 天缺失的日线数据。"""
    await safe_run_job("backfill_missing_bars", _backfill_missing_bars)


async def _update_features_daily_cn() -> None:
    """CN features 增量更新核心逻辑。"""
    from datetime import date as _date
    from data.pipeline.feature_compute import FeatureComputer
    from db.universe import get_active_symbols
    from db.feature_log import log_feature_results, get_degraded_symbols

    today = _date.today()

    # 依赖检查：今日 CN bars 是否已拉取
    if not await _today_data_exists("market_bars_daily", "trade_date", "CN"):
        logger.warning("features_cn_skip", reason="market_bars not ready for today")
        from db.job_log import log_job
        await log_job("update_features_daily_cn", "skipped",
                      error_message="market_bars_daily CN not ready")
        return

    symbols = await get_active_symbols("CN")
    computer = FeatureComputer()
    results = await computer.compute_for_symbols(symbols, "CN", today)

    errors = {s: "compute_error" for s, ok in results.items() if not ok}
    await log_feature_results(today, "CN", results, errors)

    # 连续失败检测
    degraded = await get_degraded_symbols("CN", today)
    if degraded:
        from bot.telegram_bot import TelegramPusher
        await TelegramPusher().send(
            f"🚨 <b>FEATURE DEGRADED [CN]</b>\n连续 3 天失败: <code>{', '.join(degraded)}</code>"
        )

    failed = sum(1 for ok in results.values() if not ok)
    logger.info("features_cn_done", total=len(results), failed=failed)
    if failed / max(len(results), 1) > 0.2:
        raise RuntimeError(f"features_cn 失败率过高: {failed}/{len(results)}")


async def task_update_features_daily_cn() -> None:
    """15:30 — 计算 CN features。"""
    await safe_run_job("update_features_daily_cn", _update_features_daily_cn)


async def _update_features_daily_us() -> None:
    """US features 增量更新核心逻辑。"""
    from datetime import date as _date
    from data.pipeline.feature_compute import FeatureComputer
    from db.universe import get_active_symbols
    from db.feature_log import log_feature_results, get_degraded_symbols

    today = _date.today()

    if not await _today_data_exists("market_bars_daily", "trade_date", "US"):
        logger.warning("features_us_skip", reason="market_bars not ready for today")
        from db.job_log import log_job
        await log_job("update_features_daily_us", "skipped",
                      error_message="market_bars_daily US not ready")
        return

    symbols = await get_active_symbols("US")
    computer = FeatureComputer()
    results = await computer.compute_for_symbols(symbols, "US", today)

    errors = {s: "compute_error" for s, ok in results.items() if not ok}
    await log_feature_results(today, "US", results, errors)

    degraded = await get_degraded_symbols("US", today)
    if degraded:
        from bot.telegram_bot import TelegramPusher
        await TelegramPusher().send(
            f"🚨 <b>FEATURE DEGRADED [US]</b>\n连续 3 天失败: <code>{', '.join(degraded)}</code>"
        )

    failed = sum(1 for ok in results.values() if not ok)
    logger.info("features_us_done", total=len(results), failed=failed)


async def task_update_features_daily_us() -> None:
    """05:45 — 计算 US features。"""
    await safe_run_job("update_features_daily_us", _update_features_daily_us)


async def _detect_regime_cn() -> None:
    """CN regime 检测核心逻辑。"""
    from core.regime.detector import detect_regime

    # 依赖检查：今日 CN features 是否已计算
    if not await _today_data_exists("features_daily", "trade_date"):
        logger.warning("regime_cn_skip", reason="features_daily not ready for today")
        from db.job_log import log_job
        await log_job("detect_regime_cn", "skipped",
                      error_message="features_daily not ready")
        return

    result = await detect_regime(market="CN")
    logger.info("regime_cn_done", mode=result.regime_mode, trend=result.trend_score)


async def task_detect_regime_cn() -> None:
    """15:40 — CN regime 检测。"""
    await safe_run_job("detect_regime_cn", _detect_regime_cn)


async def _detect_regime_us() -> None:
    """US regime 检测核心逻辑。"""
    from core.regime.detector import detect_regime
    result = await detect_regime(market="US")
    logger.info("regime_us_done", mode=result.regime_mode)


async def task_detect_regime_us() -> None:
    """06:00 — US regime 检测。"""
    await safe_run_job("detect_regime_us", _detect_regime_us)


async def _run_composite_analysis(market: str) -> None:
    """Composite 分析核心逻辑（CN/US 共用）。

    使用 features_daily 中该 market 的最新可用日期作为 trade_date，
    而不是 date.today()——避免在交易日尚未产生 EOD features 时
    生成 fallback 假数据（symbol 维度全部 _NEUTRAL_SCORE）。

    US 链路在 BJT 06:30 跑时，date.today() 是 BJT 当天但 US EOD 数据
    日期是 BJT 昨天。CN 链路也可能在 features 尚未更新时被触发。
    """
    from core.analysis.composite import CompositeAnalyzer

    analyzer = CompositeAnalyzer()

    # 用 universe 范围内的 features 最新日期：避免被全市场 features 拉偏。
    trade_date = await db_query_val(
        """
        SELECT MAX(f.trade_date)
        FROM features_daily f
        JOIN stock_universe u
          ON u.symbol = f.symbol
        WHERE u.market = $1 AND u.active = TRUE
        """,
        market,
    )
    if trade_date is None:
        logger.warning("composite_skip_no_features", market=market)
        from db.job_log import log_job
        await log_job(f"run_composite_analysis_{market.lower()}", "skipped",
                      error_message="no features_daily for active universe")
        return

    logger.info("composite_using_trade_date", market=market, trade_date=str(trade_date))
    results = await analyzer.analyze_universe(market=market, trade_date=trade_date, dry_run=False)
    saved = 0
    for r in results:
        try:
            await analyzer.save_judgment(r)
            saved += 1
        except Exception as e:
            logger.warning("composite_save_error", symbol=r.symbol, error=str(e))
    logger.info("composite_analysis_done", market=market,
                analyzed=len(results), saved=saved, date=str(trade_date))


async def task_run_composite_analysis_cn() -> None:
    """16:00 — CN composite 分析。"""
    await safe_run_job("run_composite_analysis_cn",
                       lambda: _run_composite_analysis("CN"))


async def task_run_composite_analysis_us() -> None:
    """06:30 — US composite 分析。"""
    await safe_run_job("run_composite_analysis_us",
                       lambda: _run_composite_analysis("US"))


async def _send_daily_digest(market: str) -> None:
    """日报推送核心逻辑（CN/US 分别调用）。"""
    from bot.commands.daily import DailyPusher
    pusher = DailyPusher()
    ok = await pusher.push(market=market, dry_run=False)
    logger.info("daily_digest_pushed", market=market, ok=ok)


async def task_send_daily_digest_cn() -> None:
    """16:30 — CN 日报推送。"""
    await safe_run_job("send_daily_digest_cn",
                       lambda: _send_daily_digest("CN"))


async def task_send_daily_digest_us() -> None:
    """07:00 — US 日报推送。"""
    await safe_run_job("send_daily_digest_us",
                       lambda: _send_daily_digest("US"))


async def _composite_distribution_snapshot() -> None:
    """Composite 分布快照核心逻辑（CN+US 合并）。"""
    from scripts.composite_distribution_snapshot import run_snapshot
    await run_snapshot()


async def task_composite_snapshot() -> None:
    """16:35 / 07:05 — Composite 分布快照（3 天观察期）。"""
    await safe_run_job("composite_snapshot", _composite_distribution_snapshot)


# ============================================================
# 调度器构建
# ============================================================

def build_scheduler() -> AsyncIOScheduler:
    """构建并配置所有定时任务（M6 版本）。

    M6 核心 10 个 job（严格按 Phase 1 方案 Task 6.2）：
    ┌─────────────────────────────┬───────┬─────────────────────────┐
    │ Job                         │ 时间  │ 状态                    │
    ├─────────────────────────────┼───────┼─────────────────────────┤
    │ check_data_freshness        │ 08:00 │ M5 实装                 │
    │ pull_cn_market_data         │ 15:15 │ M6 实装                 │
    │ pull_us_market_data         │ 05:30 │ M6 实装                 │
    │ update_features_daily_cn    │ 15:30 │ M6 实装                 │
    │ update_features_daily_us    │ 05:45 │ M6 实装                 │
    │ detect_regime_cn            │ 15:40 │ M6 实装                 │
    │ detect_regime_us            │ 06:00 │ M6 实装                 │
    │ run_composite_analysis_cn   │ 16:00 │ M7 实装 (2026-04-21)    │
    │ run_composite_analysis_us   │ 06:30 │ M7 实装 (2026-04-21)    │
    │ send_daily_digest_cn        │ 16:30 │ M8 实装 (2026-04-21)    │
    │ send_daily_digest_us        │ 07:00 │ M8 实装 (2026-04-21)    │
    │ composite_snapshot (CN)     │ 16:35 │ 观察期快照 (2026-04-22) │
    │ composite_snapshot (US)     │ 07:05 │ 观察期快照 (2026-04-22) │
    └─────────────────────────────┴───────┴─────────────────────────┘
    """
    scheduler = AsyncIOScheduler(timezone=TZ_CN)

    # ---- 系统维护 ----
    scheduler.add_job(task_heartbeat,
                      CronTrigger(minute="*/30", timezone=TZ_CN),
                      id="heartbeat", name="心跳")

    scheduler.add_job(task_scheduler_self_check,
                      CronTrigger(minute="*/5", timezone=TZ_CN),
                      id="scheduler_self_check", name="调度器自检")

    # ---- M5: 数据新鲜度 ----
    scheduler.add_job(task_check_data_freshness,
                      CronTrigger(hour=8, minute=0, timezone=TZ_CN),
                      id="check_data_freshness", name="数据新鲜度监控")

    # ---- CN 主链路 ----
    scheduler.add_job(task_pull_cn_market_data,
                      CronTrigger(hour=15, minute=15, timezone=TZ_CN),
                      id="pull_cn_market_data", name="CN数据拉取")

    scheduler.add_job(task_update_features_daily_cn,
                      CronTrigger(hour=15, minute=30, timezone=TZ_CN),
                      id="update_features_daily_cn", name="CN Features更新")

    scheduler.add_job(task_detect_regime_cn,
                      CronTrigger(hour=15, minute=40, timezone=TZ_CN),
                      id="detect_regime_cn", name="CN Regime检测")

    scheduler.add_job(task_run_composite_analysis_cn,
                      CronTrigger(hour=16, minute=0, timezone=TZ_CN),
                      id="run_composite_analysis_cn", name="CN综合分析(M7)")

    scheduler.add_job(task_send_daily_digest_cn,
                      CronTrigger(hour=16, minute=30, timezone=TZ_CN),
                      id="send_daily_digest_cn", name="CN日报推送")

    # ---- US 主链路 ----
    scheduler.add_job(task_pull_us_market_data,
                      CronTrigger(hour=5, minute=30, timezone=TZ_CN),
                      id="pull_us_market_data", name="US数据拉取")

    scheduler.add_job(task_update_features_daily_us,
                      CronTrigger(hour=5, minute=45, timezone=TZ_CN),
                      id="update_features_daily_us", name="US Features更新")

    scheduler.add_job(task_detect_regime_us,
                      CronTrigger(hour=6, minute=0, timezone=TZ_CN),
                      id="detect_regime_us", name="US Regime检测")

    scheduler.add_job(task_run_composite_analysis_us,
                      CronTrigger(hour=6, minute=30, timezone=TZ_CN),
                      id="run_composite_analysis_us", name="US综合分析(M7)")

    scheduler.add_job(task_send_daily_digest_us,
                      CronTrigger(hour=7, minute=0, timezone=TZ_CN),
                      id="send_daily_digest_us", name="US日报推送")

    # ---- 观察期快照（3 天，2026-04-22 起）----
    scheduler.add_job(task_composite_snapshot,
                      CronTrigger(hour=16, minute=35, timezone=TZ_CN),
                      id="composite_snapshot_cn", name="Composite分布快照(CN)")

    scheduler.add_job(task_composite_snapshot,
                      CronTrigger(hour=7, minute=5, timezone=TZ_CN),
                      id="composite_snapshot_us", name="Composite分布快照(US)")

    # ---- 数据补拉（周一 10:30）----
    scheduler.add_job(task_backfill_missing_bars,
                      CronTrigger(day_of_week="mon", hour=10, minute=30, timezone=TZ_CN),
                      id="backfill_missing_bars", name="缺口补拉")

    # ---- 进化引擎（Phase 5，保留框架）----
    scheduler.add_job(task_backfill_judgments,
                      CronTrigger(hour=16, minute=10, timezone=TZ_CN),
                      id="backfill_judgments", name="判断回填")

    scheduler.add_job(task_weekly_review,
                      CronTrigger(day_of_week="sat", hour=10, minute=0, timezone=TZ_CN),
                      id="weekly_review", name="周度复盘")

    scheduler.add_job(task_monthly_review,
                      CronTrigger(day=1, hour=10, minute=0, timezone=TZ_CN),
                      id="monthly_review", name="月度复盘")

    return scheduler


# ============================================================
# 主入口
# ============================================================

async def _startup_checks() -> None:
    """启动前自检：M3 invariants + M5 freshness check。

    Raises:
        InvariantViolation: features 覆盖不满足或数据源有 critical 停更。
        RuntimeError: data_source_expectations 未配置。
    """
    from core.invariants import assert_superset, assert_not_empty
    from db.connection import db_query

    # M3: features_daily 覆盖 universe
    universe_rows = await db_query(
        "SELECT symbol FROM stock_universe WHERE active = TRUE"
    )
    universe_symbols = {r["symbol"] for r in universe_rows}
    assert_not_empty(universe_symbols, "startup.universe")

    feature_rows = await db_query(
        "SELECT DISTINCT symbol FROM features_daily"
    )
    feature_symbols = {r["symbol"] for r in feature_rows}

    assert_superset(feature_symbols, universe_symbols, "startup.features_coverage")
    logger.info("startup_check_pass", check="features_coverage",
                universe=len(universe_symbols), features=len(feature_symbols))

    # M5: 检查 data_source_expectations 是否有 critical 停更
    expectations = await db_query("SELECT COUNT(*) AS cnt FROM data_source_expectations")
    if expectations[0]["cnt"] == 0:
        logger.warning("startup_no_expectations", msg="data_source_expectations 为空，跳过 freshness 检查")
    else:
        from scripts.data_freshness_check import run_all_checks, push_critical_alerts
        results = await run_all_checks()
        critical = [r for r in results if r["status"] == "critical"]
        if critical:
            names = [r["source_name"] for r in critical]
            logger.error("startup_freshness_critical", sources=names)
            await push_critical_alerts(results)
            raise RuntimeError(
                f"启动检查失败：{len(critical)} 个数据源处于 critical 停更状态: {names}\n"
                "请先修复数据源再启动 scheduler。"
            )
        logger.info("startup_check_pass", check="freshness",
                    total=len(results), warn=sum(1 for r in results if r["status"] == "warn"))


async def _run() -> None:
    """初始化数据库并启动调度器（含自检 + Telegram 通知）。"""
    global _scheduler_ref
    from dotenv import load_dotenv
    load_dotenv()

    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
    )

    await init_pool()

    # 启动自检（失败则拒绝启动）
    try:
        await _startup_checks()
    except (InvariantViolation, RuntimeError) as e:
        logger.error("startup_check_failed", error=str(e))
        try:
            from bot.telegram_bot import TelegramPusher
            await TelegramPusher().send(f"🚨 <b>Scheduler 启动失败</b>\n<code>{e}</code>")
        except Exception:
            pass
        await close_pool()
        raise SystemExit(1)

    scheduler = build_scheduler()
    _scheduler_ref = scheduler
    scheduler.start()

    jobs = scheduler.get_jobs()
    logger.info("scheduler_started", jobs=len(jobs))
    for job in jobs:
        logger.info("job_registered", id=job.id, name=job.name,
                    next_run=str(job.next_run_time))

    # 推送启动成功通知
    try:
        from bot.telegram_bot import TelegramPusher
        from datetime import datetime
        import pytz
        now_cn = datetime.now(pytz.timezone(TZ_CN)).strftime("%Y-%m-%d %H:%M:%S")
        job_list = "\n".join(f"  • {j.id}" for j in jobs)
        await TelegramPusher().send(
            f"✅ <b>Scheduler started</b> [{now_cn}]\n"
            f"Jobs ({len(jobs)}):\n{job_list}"
        )
    except Exception as e:
        logger.warning("startup_notify_failed", error=str(e))

    # 保持运行
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        _scheduler_ref = None
        await close_pool()
        logger.info("scheduler_stopped")


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
