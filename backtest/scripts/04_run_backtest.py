"""
scripts/04_run_backtest.py — 回测主入口

用法:
  python -m backtest.scripts.04_run_backtest [--start YYYY-MM-DD] [--end YYYY-MM-DD]

默认参数:
  --start 2025-10-06  (1周 pilot)
  --end   2025-10-10

完整回测:
  python -m backtest.scripts.04_run_backtest --start 2025-09-01 --end 2026-04-17
"""

import argparse
import asyncio
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from backtest.engine.engine import BacktestEngine
from backtest.pit_loader import create_pool

DSN = "postgresql://radar:alpharadar2026@localhost:5434/alpharadar"
WATCHLIST_PATH = str(Path(__file__).resolve().parents[1] / "config" / "watchlist.yaml")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
# 静默噪声日志
logging.getLogger("asyncio").setLevel(logging.WARNING)
logging.getLogger("backtest.analysis").setLevel(logging.WARNING)
logging.getLogger("backtest.engine.portfolio").setLevel(logging.WARNING)


async def main(start: date, end: date) -> None:
    pool = await create_pool(DSN, min_size=2, max_size=10)
    try:
        engine = BacktestEngine(
            pool=pool,
            watchlist_path=WATCHLIST_PATH,
            cn_initial_cash=10_000_000.0,
            us_initial_cash=1_000_000.0,
        )
        run_id = await engine.run(start, end)
        print(f"\n{'='*60}")
        print(f"  Run ID: {run_id}")
        print(f"  CN 最终: {engine.cn_portfolio.summary()}")
        print(f"  US 最终: {engine.us_portfolio.summary()}")
        stats = engine._stats
        print(f"\n  Judgments: {stats['judgments_total']} total | "
              f"bullish={stats.get('bullish',0)} "
              f"neutral={stats.get('neutral',0)} "
              f"bearish={stats.get('bearish',0)}")
        print(f"  Trades: entries={stats['entries']} exits={stats['exits']}")
        print(f"{'='*60}")

        # 从 DB 查询本次 run 统计
        async with pool.acquire() as conn:
            r = await conn.fetchrow(
                "SELECT status, notes FROM backtest_runs WHERE run_id = $1", run_id
            )
            print(f"\n  DB Status: {r['status']}")
            print(f"  Notes: {r['notes']}")

            j_cnt = await conn.fetchval(
                "SELECT COUNT(*) FROM backtest_judgments WHERE run_id=$1", run_id
            )
            t_cnt = await conn.fetchval(
                "SELECT COUNT(*) FROM backtest_trades WHERE run_id=$1", run_id
            )
            pd_cnt = await conn.fetchval(
                "SELECT COUNT(*) FROM backtest_portfolio_daily WHERE run_id=$1", run_id
            )
            pos_cnt = await conn.fetchval(
                "SELECT COUNT(*) FROM backtest_positions WHERE run_id=$1", run_id
            )
            reg_cnt = await conn.fetchval(
                "SELECT COUNT(*) FROM backtest_regime_daily WHERE run_id=$1", run_id
            )
        print(f"\n  DB行数验证:")
        print(f"    backtest_judgments:      {j_cnt}")
        print(f"    backtest_trades:         {t_cnt}")
        print(f"    backtest_portfolio_daily:{pd_cnt}")
        print(f"    backtest_positions:      {pos_cnt}")
        print(f"    backtest_regime_daily:   {reg_cnt}")

    finally:
        await pool.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2025-10-06")
    parser.add_argument("--end",   default="2025-10-10")
    args = parser.parse_args()

    asyncio.run(main(
        start=date.fromisoformat(args.start),
        end=date.fromisoformat(args.end),
    ))
