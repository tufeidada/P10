"""
验证 sentiment.py 在 4 个市场时间点上的输出。
运行：python -m backtest.scripts.validate_sentiment
"""
import asyncio
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backtest.pit_loader import PITDataLoader, create_pool
from backtest.analysis.sentiment import analyze_market_sentiment

DSN = "postgresql://radar:alpharadar2026@localhost:5433/alpharadar"

CASES = [
    ("CN", date(2025, 10, 15), "A股反弹启动期，预期偏乐观 60-70"),
    ("CN", date(2026, 3, 15),  "中美贸易摩擦，预期偏悲观 30-40"),
    ("US", date(2024, 8, 5),   "美股 VIX spike，预期 <30"),
    ("US", date(2026, 1, 15),  "美股平稳期，预期 ~55"),
]


async def main() -> None:
    pool = await create_pool(DSN, min_size=1, max_size=3)
    loader = PITDataLoader(pool)

    try:
        for market, cutoff, note in CASES:
            print(f"\n{'='*60}")
            print(f"  {market}  @  {cutoff}  —  {note}")
            print(f"{'='*60}")

            loader.set_date(cutoff)
            score, det = await analyze_market_sentiment(loader, market)

            if score is None:
                print("  [ERROR] score=None — no data")
                continue

            print(f"  sentiment_score : {score:.2f}")
            print(f"  weights         : {det.get('weights', 'N/A')}")
            print()

            if market == "CN":
                print(f"  ── Sub-scores ──")
                def _fmt(v): return f"{v:.2f}" if v is not None else "N/A"
                print(f"    advancing_ratio_score : {_fmt(det.get('adv_score'))}")
                print(f"    limit_ratio_score     : {_fmt(det.get('limit_score'))}")
                print(f"    margin_change_score   : {_fmt(det.get('margin_score'))}")
                print()
                print(f"  ── Raw Data ──")
                print(f"    adv_ratio_5d_mean  : {det.get('adv_ratio_5d_mean', 'N/A')}")
                print(f"    adv_latest / dec_latest : {det.get('adv_latest', 'N/A')} / {det.get('dec_latest', 'N/A')}")
                print(f"    total_stocks       : {det.get('total_stocks', 'N/A')}")
                print(f"    limit_ratio_5d_mean: {det.get('limit_ratio_5d_mean', 'N/A')}")
                print(f"    limit_up / down    : {det.get('limit_up_latest', 'N/A')} / {det.get('limit_down_latest', 'N/A')}")
                mc = det.get('margin_chg_5d_pct')
                print(f"    margin_chg_5d_pct  : {f'{mc:.4f}%' if mc is not None else 'N/A'}")
                mn = det.get('margin_total_now')
                print(f"    margin_total_now   : {f'{mn:.2f}亿元' if mn is not None else 'N/A'}")
            else:
                print(f"  ── VIX ──")
                print(f"    VIX : {det.get('vix', 'N/A')}")
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
