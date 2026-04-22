"""
验证 flow.py 在 3 只样本股上的输出。
运行：python -m backtest.scripts.validate_flow
"""
import asyncio
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import asyncpg

from backtest.pit_loader import PITDataLoader, create_pool
from backtest.analysis.flow import analyze_flow

DSN = "postgresql://radar:alpharadar2026@localhost:5433/alpharadar"

CASES = [
    ("000063.SZ", "CN", date(2026, 1, 15)),  # 中兴通讯，弱势期（北向有历史）
    ("601318.SH", "CN", date(2026, 1, 15)),  # 中国平安，蓝筹（北向有历史）
    ("300502.SZ", "CN", date(2024, 11, 15)), # 新易盛，光模块启动期（北向无历史）
    ("000063.SZ", "CN", date(2024, 8, 15)),  # 中兴通讯，回测区间前（北向无历史，score 差值对比）
]


def _fmt(v, unit="") -> str:
    if v is None:
        return "N/A"
    if isinstance(v, float):
        return f"{v:.4f}{unit}"
    return f"{v}{unit}"


async def main() -> None:
    pool = await create_pool(DSN, min_size=1, max_size=3)
    loader = PITDataLoader(pool)

    try:
        for symbol, market, cutoff in CASES:
            print(f"\n{'='*60}")
            print(f"  {symbol}  @  {cutoff}  [{market}]")
            print(f"{'='*60}")

            loader.set_date(cutoff)
            r = await analyze_flow(loader, symbol, market)

            if r is None:
                print("  [ERROR] returned None")
                continue

            print(f"  data_complete  : {r.data_complete}")
            nb_disp = f"{r.northbound_score:.2f}" if r.northbound_score is not None else "None"
            mg_disp = f"{r.margin_score:.2f}"     if r.margin_score     is not None else "None"
            print(f"  northbound_score raw : {nb_disp}")
            print(f"  margin_score raw     : {mg_disp}")
            print(f"  weights        : {r.detail.get('weights', 'N/A')}")
            print()
            print(f"  ── Scores ──")
            print(f"    main_flow_score  : {r.main_flow_score:.2f}")
            nb_s = f"{r.northbound_score:.2f}" if r.northbound_score is not None else "None (历史不足)"
            mg_s = f"{r.margin_score:.2f}"     if r.margin_score     is not None else "None (非两融)"
            print(f"    northbound_score : {nb_s}")
            print(f"    margin_score     : {mg_s}")
            print(f"    FINAL score      : {r.score:.2f}")
            print()
            print(f"  ── Raw Data ──")
            fp = r.detail.get('flow_pct_5d')
            print(f"    5日大单净流入占流通市值 : {_fmt(fp, '%')}")
            nb = r.detail.get('nb_20d_pct')
            nb_str = f"{nb*100:.1f}%分位" if nb is not None else "N/A (无历史数据)"
            print(f"    20日北向净买入历史分位  : {nb_str}")
            mc = r.detail.get('margin_chg_5d')
            print(f"    融资余额5日变化率        : {_fmt(mc, '%')}")
            cmv = r.detail.get('circ_mv_bn')
            print(f"    流通市值                : {_fmt(cmv, '亿元')}")
            print()
            if r.highlights:
                print(f"  ── Highlights ──")
                for h in r.highlights:
                    print(f"    + {h}")
            if r.risks:
                print(f"  ── Risks ──")
                for rk in r.risks:
                    print(f"    ! {rk}")
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
