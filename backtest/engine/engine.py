"""
backtest/engine/engine.py — 回测主循环

BacktestEngine 统筹：
  1. 每日推进时间轴（CN 交易日历）
  2. 执行上日队列的 T+1 开盘成交（exits + entries）
  3. 检查当日盘中止损 / 达目标（high/low 触发）
  4. 检测 Regime
  5. 生成全池 Judgment（支持并发，Semaphore=5 限制 DB 并发）
  6. 组建下日执行队列（signal exits + new entries）
  7. 批量写入快照（backtest_portfolio_daily / backtest_positions / backtest_regime_daily）
  8. 进度监控（每月末打印摘要，tqdm 日级进度条）

PIT 约束：
  - 所有数据读取走 PITDataLoader
  - get_open_price 仅在 execute_pending_* 中调用（合法"未来"入口）
  - 判断生成用 loader.get_bars() 默认截至 prev_trade_date

数据库写入：
  - 每日一个 async transaction（事务失败不中断循环，仅 log error）
  - asyncpg executemany 批量写 backtest_positions
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import date
from typing import Any, Optional

import asyncpg
import yaml
from tqdm import tqdm

from backtest.analysis.composite import generate_judgment, save_judgment
from backtest.analysis.regime import detect_regime
from backtest.engine.benchmarks import Benchmarks
from backtest.engine.execution import TradeExecutor
from backtest.engine.portfolio import Portfolio, Position, Trade
from backtest.pit_loader import PITDataLoader

log = logging.getLogger(__name__)

# 判断并发数（受 DB 连接池限制）
_JUDGMENT_CONCURRENCY = 5

# Regime mode → max_position_pct 映射
_REGIME_MAX_POS: dict[str, float] = {
    "offense":       0.20,   # 进攻模式（牛市明确）
    "bull_trend":    0.20,
    "recovery":      0.15,
    "neutral":       0.15,
    "volatile":      0.10,
    "risk_off":      0.05,
    "defense":       0.0,    # 防御模式不建仓
}
_DEFAULT_MAX_POS = 0.15


def _load_watchlist(config_path: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """从 watchlist.yaml 读取 (symbol, industry) 列表。返回 (cn_list, us_list)。"""
    with open(config_path) as f:
        cfg = yaml.safe_load(f)
    wl = cfg.get("watchlist", {})
    cn = [(s["symbol"], s.get("industry", "default")) for s in wl.get("CN", [])]
    us = [(s["symbol"], s.get("industry", "default")) for s in wl.get("US", [])]
    return cn, us


def _regime_max_pct(regime_result) -> float:
    if regime_result is None:
        return _DEFAULT_MAX_POS
    return _REGIME_MAX_POS.get(regime_result.regime_mode, _DEFAULT_MAX_POS)


class BacktestEngine:
    """
    回测引擎主类。

    Args:
        pool:             asyncpg 连接池（由调用方创建）。
        watchlist_path:   watchlist.yaml 路径。
        cn_initial_cash:  CN 账户初始资金（元）。
        us_initial_cash:  US 账户初始资金（USD）。
        config_snapshot:  任意额外配置（存入 backtest_runs.config_snapshot）。
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        watchlist_path: str,
        cn_initial_cash: float = 10_000_000.0,
        us_initial_cash: float = 1_000_000.0,
        config_snapshot: Optional[dict] = None,
    ) -> None:
        self.pool = pool
        self.loader = PITDataLoader(pool)
        self.cn_portfolio = Portfolio(cn_initial_cash, "CN")
        self.us_portfolio = Portfolio(us_initial_cash, "US")
        self.executor = TradeExecutor(self.loader)
        self.cn_benchmarks = Benchmarks("CN")
        self.us_benchmarks = Benchmarks("US")

        cn_wl, us_wl = _load_watchlist(watchlist_path)
        self.cn_watchlist: list[tuple[str, str]] = cn_wl   # (symbol, industry)
        self.us_watchlist: list[tuple[str, str]] = us_wl
        self.cn_industry_map: dict[str, str] = dict(cn_wl)
        self.us_industry_map: dict[str, str] = dict(us_wl)

        self.config_snapshot = config_snapshot or {
            "cn_initial_cash": cn_initial_cash,
            "us_initial_cash": us_initial_cash,
            "comm_cn": 0.0025,
            "comm_us": 0.0015,
            "max_loss_pct": 0.02,
            "max_industry_pct": 0.40,
            "liquidity_mult": 10.0,
            "timeout_days": 30,
            "entry_confidence_thr": 0.55,
            "flip_confidence_thr": 0.50,
        }

        # 跨日队列状态
        self._pending_exits: list = []
        self._pending_entries: list = []

        # 统计
        self._stats: dict[str, int] = {
            "judgments_total": 0,
            "bullish": 0,
            "neutral": 0,
            "bearish": 0,
            "entries": 0,
            "exits": 0,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # 主入口
    # ─────────────────────────────────────────────────────────────────────────

    async def run(self, start_date: date, end_date: date) -> int:
        """
        运行回测，返回 run_id。

        Args:
            start_date: 回测开始日（含）。
            end_date:   回测结束日（含）。
        """
        t0 = time.monotonic()
        run_id = await self._create_run(start_date, end_date)
        log.info(f"engine: run_id={run_id} start={start_date} end={end_date}")

        trading_days = await self._get_cn_trading_days(start_date, end_date)
        log.info(f"engine: {len(trading_days)} trading days to process")

        # 初始化 benchmarks（在第一个交易日之前）
        if trading_days:
            cn_syms = [s for s, _ in self.cn_watchlist]
            us_syms = [s for s, _ in self.us_watchlist]
            await self.cn_benchmarks.initialize(self.pool, trading_days[0], cn_syms)
            await self.us_benchmarks.initialize(self.pool, trading_days[0], us_syms)

        prev_month = None
        for current_date in tqdm(trading_days, desc="Backtest", unit="day"):
            try:
                await self._process_day(current_date, run_id)
            except Exception as exc:
                log.error(
                    f"engine: day {current_date} FAILED: {exc}", exc_info=True
                )
                continue  # 不中断全局循环

            # 月末进度报告
            if prev_month and current_date.month != prev_month:
                self._print_progress(current_date)
            prev_month = current_date.month

        # 最后一天也打印
        if trading_days:
            self._print_progress(trading_days[-1])

        elapsed = time.monotonic() - t0
        await self._finalize_run(run_id, elapsed)
        log.info(
            f"engine: DONE run_id={run_id} elapsed={elapsed:.1f}s "
            f"judgments={self._stats['judgments_total']} "
            f"entries={self._stats['entries']} exits={self._stats['exits']}"
        )
        return run_id

    # ─────────────────────────────────────────────────────────────────────────
    # 单日处理
    # ─────────────────────────────────────────────────────────────────────────

    async def _process_day(self, T: date, run_id: int) -> None:
        self.loader.set_date(T)

        # ── 1. 执行上日队列（T 日开盘价成交）─────────────────────────────────
        exit_trades = await self.executor.execute_pending_exits(
            self._pending_exits, exec_date=T
        )
        self._pending_exits.clear()
        self._stats["exits"] += len(exit_trades)

        new_positions = await self.executor.execute_pending_entries(
            self._pending_entries, exec_date=T
        )
        self._pending_entries.clear()
        self._stats["entries"] += len(new_positions)

        # ── 2. 盘中止损 / 达目标（T 日 high/low 触发，精确价格）─────────────
        cn_intraday = await self.executor.check_intraday_exits(self.cn_portfolio, T)
        us_intraday = await self.executor.check_intraday_exits(self.us_portfolio, T)
        intraday_trades = cn_intraday + us_intraday
        self._stats["exits"] += len(intraday_trades)

        # ── 3. 未触及止损/目标的持仓已在 check_intraday_exits 中更新到收盘价 ──
        #    需要更新没有任何当日 bar 的持仓（停牌等）保持原价

        # ── 4. 检测 Regime ────────────────────────────────────────────────────
        cn_regime = await self._detect_regime("CN")
        us_regime = await self._detect_regime("US")

        # ── 5. 生成 Judgments（并发，Semaphore 限制）──────────────────────────
        cn_judgments = await self._gen_judgments("CN", run_id)
        us_judgments = await self._gen_judgments("US", run_id)

        # ── 6. 组建下日执行队列 ───────────────────────────────────────────────
        cn_sig_exits = await self.executor.queue_signal_exits(
            self.cn_portfolio, cn_judgments, T
        )
        us_sig_exits = await self.executor.queue_signal_exits(
            self.us_portfolio, us_judgments, T
        )
        self._pending_exits = cn_sig_exits + us_sig_exits

        cn_max_pct = _regime_max_pct(cn_regime)
        us_max_pct = _regime_max_pct(us_regime)

        # 防御模式不建仓
        if cn_max_pct > 0:
            cn_entries = await self.executor.queue_entries(
                cn_judgments, self.cn_portfolio, cn_max_pct, self.cn_industry_map
            )
        else:
            cn_entries = []

        if us_max_pct > 0:
            us_entries = await self.executor.queue_entries(
                us_judgments, self.us_portfolio, us_max_pct, self.us_industry_map
            )
        else:
            us_entries = []

        self._pending_entries = cn_entries + us_entries

        # ── 7. 基准收益率 ────────────────────────────────────────────────────
        cn_syms = [s for s, _ in self.cn_watchlist]
        us_syms = [s for s, _ in self.us_watchlist]
        cn_b1, cn_b2, cn_b3 = await self.cn_benchmarks.update(self.pool, T, cn_syms)
        us_b1, us_b2, us_b3 = await self.us_benchmarks.update(self.pool, T, us_syms)

        # ── 8. 持久化快照 ────────────────────────────────────────────────────
        all_judgments = cn_judgments + us_judgments
        all_exit_trades = exit_trades + intraday_trades
        all_entry_positions = new_positions

        await self._persist_day(
            run_id, T,
            cn_regime, us_regime,
            all_judgments,
            all_exit_trades,
            all_entry_positions,
            cn_benchmarks=(cn_b1, cn_b2, cn_b3),
            us_benchmarks=(us_b1, us_b2, us_b3),
        )

    # ─────────────────────────────────────────────────────────────────────────
    # 工具：Regime + Judgment 生成
    # ─────────────────────────────────────────────────────────────────────────

    async def _detect_regime(self, market: str):
        # detect_regime(pool, market, cutoff) — cutoff 取 loader 当前日期
        cutoff = self.loader._current_date
        try:
            return await detect_regime(self.pool, market, cutoff)
        except Exception as exc:
            log.error(f"engine: regime failed market={market}: {exc}", exc_info=True)
            return None

    async def _gen_judgments(self, market: str, run_id: int) -> list:
        watchlist = self.cn_watchlist if market == "CN" else self.us_watchlist
        sem = asyncio.Semaphore(_JUDGMENT_CONCURRENCY)

        async def _one(symbol: str, industry: str):
            async with sem:
                try:
                    j = await generate_judgment(self.pool, self.loader, symbol, market)
                    if j is None:
                        return None
                    # 保存到 DB，回填 id
                    j_id = await save_judgment(self.pool, j, run_id)
                    j.id = j_id  # type: ignore[attr-defined]

                    self._stats["judgments_total"] += 1
                    self._stats[j.direction] = self._stats.get(j.direction, 0) + 1
                    return j
                except Exception as exc:
                    log.error(
                        f"engine: judgment failed {symbol}: {exc}", exc_info=True
                    )
                    return None

        tasks = [_one(sym, ind) for sym, ind in watchlist]
        results = await asyncio.gather(*tasks)
        return [j for j in results if j is not None]

    # ─────────────────────────────────────────────────────────────────────────
    # 工具：交易日历
    # ─────────────────────────────────────────────────────────────────────────

    async def _get_cn_trading_days(
        self, start: date, end: date
    ) -> list[date]:
        """从 trade_calendar 获取 CN 交易日列表（升序）。"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT trade_date FROM trade_calendar
                WHERE trade_date BETWEEN $1 AND $2
                ORDER BY trade_date
                """,
                start, end,
            )
        return [r["trade_date"] for r in rows]

    # ─────────────────────────────────────────────────────────────────────────
    # 工具：数据库操作
    # ─────────────────────────────────────────────────────────────────────────

    async def _create_run(self, start: date, end: date) -> int:
        async with self.pool.acquire() as conn:
            run_id = await conn.fetchval(
                """
                INSERT INTO backtest_runs
                    (start_date, end_date, initial_cash_cn, initial_cash_us,
                     config_snapshot, status)
                VALUES ($1, $2, $3, $4, $5, 'running')
                RETURNING run_id
                """,
                start, end,
                self.cn_portfolio.initial_cash,
                self.us_portfolio.initial_cash,
                json.dumps(self.config_snapshot),
            )
        return run_id

    async def _finalize_run(self, run_id: int, elapsed_seconds: float) -> None:
        notes = (
            f"elapsed={elapsed_seconds:.1f}s | "
            f"judgments={self._stats['judgments_total']} | "
            f"bullish={self._stats.get('bullish',0)} "
            f"neutral={self._stats.get('neutral',0)} "
            f"bearish={self._stats.get('bearish',0)} | "
            f"entries={self._stats['entries']} exits={self._stats['exits']}"
        )
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE backtest_runs
                SET status = 'completed', notes = $1
                WHERE run_id = $2
                """,
                notes, run_id,
            )

    async def _persist_day(
        self,
        run_id:          int,
        T:               date,
        cn_regime,
        us_regime,
        judgments:       list,
        exit_trades:     list[Trade],
        entry_positions: list[Position],
        cn_benchmarks:   tuple[float, float, float] = (0.0, 0.0, 0.0),
        us_benchmarks:   tuple[float, float, float] = (0.0, 0.0, 0.0),
    ) -> None:
        """一个事务写入当日所有快照。事务失败只 log，不中断引擎。"""
        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    # regime_daily
                    for regime, market in [(cn_regime, "CN"), (us_regime, "US")]:
                        if regime is None:
                            continue
                        await conn.execute(
                            """
                            INSERT INTO backtest_regime_daily
                                (run_id, trade_date, market,
                                 trend_score, volatility_score, breadth_score,
                                 liquidity_score, regime_mode, trend_direction,
                                 volatility_env, detail)
                            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                            ON CONFLICT (run_id, trade_date, market) DO NOTHING
                            """,
                            run_id, T, market,
                            getattr(regime, "trend_score", None),
                            getattr(regime, "volatility_score", None),
                            getattr(regime, "breadth_score", None),
                            getattr(regime, "liquidity_score", None),
                            getattr(regime, "regime_mode", None),
                            getattr(regime, "trend_direction", None),
                            getattr(regime, "volatility_env", None),
                            json.dumps(getattr(regime, "detail", {})),
                        )

                    # portfolio_daily
                    bench_map = {"CN": cn_benchmarks, "US": us_benchmarks}
                    for pf, market in [
                        (self.cn_portfolio, "CN"),
                        (self.us_portfolio, "US"),
                    ]:
                        prev_value = getattr(self, f"_prev_{market.lower()}_value",
                                             pf.initial_cash)
                        daily_ret = (pf.value / prev_value - 1.0) if prev_value else 0.0
                        cum_ret   = (pf.value / pf.initial_cash - 1.0)
                        b1, b2, b3 = bench_map[market]
                        await conn.execute(
                            """
                            INSERT INTO backtest_portfolio_daily
                                (run_id, trade_date, market, cash, positions_value,
                                 total_value, num_positions, position_pct,
                                 daily_return, cumulative_return,
                                 benchmark_return_1, benchmark_return_2, benchmark_return_3)
                            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
                            ON CONFLICT (run_id, trade_date, market) DO NOTHING
                            """,
                            run_id, T, market,
                            round(pf.cash, 2),
                            round(pf.positions_value, 2),
                            round(pf.value, 2),
                            pf.position_count,
                            round(pf.position_pct, 4),
                            round(daily_ret, 6),
                            round(cum_ret, 6),
                            round(b1, 6),
                            round(b2, 6),
                            round(b3, 6),
                        )
                        # 更新上日市值（用于明日 daily_return 计算）
                        setattr(self, f"_prev_{market.lower()}_value", pf.value)

                    # positions (snapshot)
                    pos_records = []
                    for pf, market in [
                        (self.cn_portfolio, "CN"),
                        (self.us_portfolio, "US"),
                    ]:
                        for pos in pf.positions:
                            days_held = (T - pos.entry_date).days
                            pos_records.append((
                                run_id, T, pos.symbol, market,
                                pos.shares,
                                round(pos.entry_price, 4),
                                round(pos.current_price, 4),
                                round(pos.market_value, 2),
                                round(pos.unrealized_pnl, 2),
                                round(pos.unrealized_pnl_pct, 6),
                                pos.stop_loss,
                                pos.target_price,
                                days_held,
                            ))
                    if pos_records:
                        await conn.executemany(
                            """
                            INSERT INTO backtest_positions
                                (run_id, trade_date, symbol, market,
                                 shares, avg_cost, current_price, market_value,
                                 unrealized_pnl, unrealized_pnl_pct,
                                 stop_loss, target_price, days_held)
                            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
                            ON CONFLICT (run_id, trade_date, symbol) DO NOTHING
                            """,
                            pos_records,
                        )

                    # trades（平仓记录）
                    for trade in exit_trades:
                        pf = (
                            self.cn_portfolio
                            if trade.market == "CN"
                            else self.us_portfolio
                        )
                        await conn.execute(
                            """
                            INSERT INTO backtest_trades
                                (run_id, symbol, market, action, trade_date,
                                 price, shares, amount, commission,
                                 trigger_judgment_id, trigger_reason,
                                 portfolio_value_after)
                            VALUES ($1,$2,$3,'sell',$4,$5,$6,$7,$8,$9,$10,$11)
                            """,
                            run_id, trade.symbol, trade.market, T,
                            round(trade.exit_price, 4),
                            trade.shares,
                            round(trade.exit_price * trade.shares, 2),
                            None,   # exit_commission 未单独暴露，pnl 含双边
                            trade.trigger_judgment_id,
                            trade.exit_reason,
                            round(pf.value, 2),
                        )

                    # trades（建仓记录）
                    for pos in entry_positions:
                        pf = (
                            self.cn_portfolio
                            if pos.market == "CN"
                            else self.us_portfolio
                        )
                        await conn.execute(
                            """
                            INSERT INTO backtest_trades
                                (run_id, symbol, market, action, trade_date,
                                 price, shares, amount, commission,
                                 trigger_judgment_id, trigger_reason,
                                 portfolio_value_after)
                            VALUES ($1,$2,$3,'buy',$4,$5,$6,$7,$8,$9,'new_bullish',$10)
                            """,
                            run_id, pos.symbol, pos.market, T,
                            round(pos.entry_price, 4),
                            pos.shares,
                            round(pos.entry_price * pos.shares, 2),
                            round(pos.entry_commission, 2),
                            pos.trigger_judgment_id,
                            round(pf.value, 2),
                        )

        except Exception as exc:
            log.error(f"engine: persist failed on {T}: {exc}", exc_info=True)

    # ─────────────────────────────────────────────────────────────────────────
    # 进度监控
    # ─────────────────────────────────────────────────────────────────────────

    def _print_progress(self, current_date: date) -> None:
        cn = self.cn_portfolio
        us = self.us_portfolio
        cn_ret = (cn.value / cn.initial_cash - 1) * 100
        us_ret = (us.value / us.initial_cash - 1) * 100
        print(
            f"\n[{current_date}] "
            f"CN: {cn.value/1e4:.1f}万元 ({cn_ret:+.2f}%) {cn.position_count}持仓 | "
            f"US: {us.value/1e4:.1f}万元 ({us_ret:+.2f}%) {us.position_count}持仓 | "
            f"J={self._stats['judgments_total']} "
            f"B={self._stats.get('bullish',0)} "
            f"entries={self._stats['entries']} exits={self._stats['exits']}"
        )
