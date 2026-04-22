"""
验证 fundamental.py 在 3 只样本股上的输出。
运行：python -m backtest.scripts.validate_fundamental
"""
import asyncio
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import asyncpg

from backtest.analysis.fundamental import analyze_fundamental


DSN = "postgresql://radar:alpharadar2026@localhost:5433/alpharadar"

CASES = [
    ("000063.SZ", "CN", date(2026, 1, 15)),   # 通信 / technology framework
    ("601318.SH", "CN", date(2026, 1, 15)),   # 非银金融 / financial framework
    ("603986.SH", "CN", date(2026, 1, 15)),   # 半导体 / semiconductor framework
]


def _fmt(v) -> str:
    if v is None:
        return "N/A"
    if isinstance(v, float):
        return f"{v:.4f}"
    return str(v)


async def main() -> None:
    pool = await asyncpg.create_pool(DSN, min_size=1, max_size=3)
    try:
        for symbol, market, cutoff in CASES:
            print(f"\n{'='*60}")
            print(f"  {symbol}  @  {cutoff}  [{market}]")
            print(f"{'='*60}")

            r = await analyze_fundamental(pool, symbol, market, cutoff)
            if r is None:
                print("  [ERROR] returned None — no financials found")
                continue

            fund_cnt = await pool.fetchval(
                "SELECT count(*) FROM fundamentals_daily WHERE symbol=$1 AND available_date<=$2",
                symbol, cutoff
            )
            print(f"  framework      : {r.framework_used}")
            print(f"  data_quarters  : {r.data_quarters}")
            print(f"  fund_daily_rows: {fund_cnt}  (available_date <= {cutoff})")
            print(f"  fundamental_score : {r.fundamental_score:.2f}")
            print()
            print(f"  ── Sub-scores ──")
            print(f"    profitability : {r.profitability_score:.2f}")
            print(f"    growth        : {r.growth_score:.2f}")
            print(f"    valuation     : {r.valuation_score:.2f}")
            print(f"    health        : {r.health_score:.2f}")
            print()
            print(f"  ── Weights ──")
            for k, v in r.detail.get("weights", {}).items():
                print(f"    {k:<15}: {v}")
            print()
            print(f"  ── Profitability Detail ──")
            pd_ = r.detail.get("profitability_detail", {})
            print(f"    roe_ttm_latest      : {_fmt(pd_.get('roe_ttm_latest'))}")
            print(f"    gross_margin_latest : {_fmt(pd_.get('gross_margin_latest'))}")
            print()
            print(f"  ── Growth Detail ──")
            gd = r.detail.get("growth_detail", {})
            print(f"    revenue_yoy_recent4 : {_fmt(gd.get('revenue_yoy_recent4'))}")
            print(f"    np_yoy_recent4      : {_fmt(gd.get('np_yoy_recent4'))}")
            print()
            print(f"  ── Valuation Detail ──")
            vd = r.detail.get("valuation_detail", {})
            print(f"    pe_ttm : {_fmt(vd.get('pe_ttm'))}")
            print(f"    pb     : {_fmt(vd.get('pb'))}")
            print()
            print(f"  ── Health Detail ──")
            hd = r.detail.get("health_detail", {})
            print(f"    debt_ratio    : {_fmt(hd.get('debt_ratio'))}")
            print(f"    current_ratio : {_fmt(hd.get('current_ratio'))}")
            print()
            if r.highlights:
                print(f"  ── Highlights ──")
                for h in r.highlights:
                    print(f"    + {h}")
            if r.risks:
                print(f"  ── Risks ──")
                for rr in r.risks:
                    print(f"    ! {rr}")
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
