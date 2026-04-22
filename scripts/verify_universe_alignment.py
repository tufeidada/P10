"""
M2 验收验证脚本 — stock_universe 对齐检查。

检查：
  1. stock_universe 无 status 字段（已迁移为 active BOOLEAN）
  2. active=TRUE 行数 = 48
  3. features_daily 对 active 股票的覆盖天数（< 250 天的列出，这些是 M4 补齐目标）

用法：
  python scripts/verify_universe_alignment.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.connection import close_pool, get_pool, init_pool

EXPECTED_ACTIVE = 48


async def main() -> int:
    """执行所有检查，返回 0=全通过 / 1=有失败。"""
    await init_pool()
    pool = get_pool()
    failed = False

    print("=" * 65)
    print("stock_universe 对齐验证")
    print("=" * 65)

    # ── Check 1: status 列不存在 ──────────────────────────────────
    has_status = await pool.fetchval("""
        SELECT COUNT(*) FROM information_schema.columns
        WHERE table_name = 'stock_universe' AND column_name = 'status'
    """)
    if has_status:
        print("[FAIL] Check 1: status 列仍存在，迁移未完成")
        failed = True
    else:
        print("[PASS] Check 1: status 列已删除")

    # ── Check 2: active=TRUE 行数 = EXPECTED_ACTIVE ───────────────
    active_count = await pool.fetchval(
        "SELECT COUNT(*) FROM stock_universe WHERE active = TRUE"
    )
    if active_count != EXPECTED_ACTIVE:
        print(f"[FAIL] Check 2: active=TRUE 行数={active_count}，期望={EXPECTED_ACTIVE}")
        failed = True
    else:
        print(f"[PASS] Check 2: active=TRUE 行数={active_count} ✓")

    # ── Check 3: features_daily 覆盖天数 ─────────────────────────
    print()
    print("── Check 3: features_daily 覆盖天数（active 股票）──")
    rows = await pool.fetch("""
        SELECT
            su.symbol,
            su.market,
            su.priority,
            COUNT(f.trade_date)         AS days_covered,
            MAX(f.trade_date)           AS latest_date
        FROM stock_universe su
        LEFT JOIN features_daily f ON f.symbol = su.symbol
        WHERE su.active = TRUE
        GROUP BY su.symbol, su.market, su.priority
        ORDER BY days_covered ASC, su.market, su.symbol
    """)

    zero_cov = [r for r in rows if r["days_covered"] == 0]
    low_cov  = [r for r in rows if 0 < r["days_covered"] < 250]
    full_cov = [r for r in rows if r["days_covered"] >= 250]

    print(f"  全覆盖 (≥250天): {len(full_cov)} 只")
    print(f"  部分覆盖 (1-249天): {len(low_cov)} 只")
    print(f"  零覆盖 (0天): {len(zero_cov)} 只")

    if zero_cov or low_cov:
        print()
        print("  ── M4 补齐目标（覆盖不足 250 天）──")
        for r in zero_cov + low_cov:
            latest = str(r["latest_date"]) if r["latest_date"] else "无数据"
            print(f"  [{r['market']}] {r['symbol']:15} pri={r['priority']}  "
                  f"覆盖={r['days_covered']:>3}天  最新={latest}")
        print()
        if zero_cov:
            print(f"  [WARN] {len(zero_cov)} 只股票在 features_daily 完全无数据")
    else:
        print("[PASS] Check 3: 所有 active 股票 features_daily ≥ 250 天")

    # ── 汇总 ──────────────────────────────────────────────────────
    total_rows = await pool.fetchval("SELECT COUNT(*) FROM stock_universe")
    inactive   = await pool.fetchval(
        "SELECT COUNT(*) FROM stock_universe WHERE active = FALSE"
    )
    print()
    print("=" * 65)
    print(f"stock_universe 总行数: {total_rows}  (active={active_count}, inactive={inactive})")
    print("=" * 65)
    if failed:
        print("结果：FAIL")
    else:
        print("结果：PASS（M4 覆盖补齐任务见上方清单）")

    await close_pool()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
