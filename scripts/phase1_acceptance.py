"""
M10 — Phase 1 验收报告。

检查 10 项验收条件，输出结构化报告，失败时以退出码 1 返回。

用法：
    python scripts/phase1_acceptance.py
    python scripts/phase1_acceptance.py --verbose
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Awaitable

from dotenv import load_dotenv

load_dotenv(".env")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import structlog

# 静默 structlog（报告自己管格式）
structlog.configure(
    processors=[structlog.processors.JSONRenderer()],
    wrapper_class=structlog.make_filtering_bound_logger(40),  # ERROR only
)

# ────────────────────────────────────────────────
# 检查结果类型
# ────────────────────────────────────────────────

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"


class CheckResult:
    def __init__(self, code: str, message: str, label: str = ""):
        self.code = code      # PASS / FAIL / SKIP
        self.label = label    # e.g. "C1"
        self.message = message

    @property
    def icon(self) -> str:
        return {"PASS": "✅", "FAIL": "❌", "SKIP": "⚠️ "}.get(self.code, "❓")

    def fmt(self) -> str:
        return f"  {self.label:<4}{self.icon} {self.code:<4}  {self.message}"


# ────────────────────────────────────────────────
# 各项检查
# ────────────────────────────────────────────────

REQUIRED_TABLES = [
    "judgments",
    "regime_daily",
    "stock_universe",
    "features_daily",
    "wiki_pages",
    "experience_store",
    "llm_cost_log",
    "scheduler_heartbeat",
    "scheduler_job_log",
    "financials_quarterly",
]


async def c1_db_connect() -> CheckResult:
    """C1: DB 连接正常。"""
    try:
        from db.connection import init_pool, db_query_val
        await init_pool(min_size=1, max_size=3)
        val = await db_query_val("SELECT 1")
        assert val == 1
        return CheckResult(PASS, "DB 连接正常")
    except Exception as e:
        return CheckResult(FAIL, f"DB 连接失败: {e}")


async def c2_tables() -> CheckResult:
    """C2: 所有必要表存在。"""
    try:
        from db.connection import db_query_val
        missing = []
        for table in REQUIRED_TABLES:
            exists = await db_query_val(
                """
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_name = $1
                )
                """,
                table,
            )
            if not exists:
                missing.append(table)
        total = len(REQUIRED_TABLES)
        found = total - len(missing)
        if missing:
            return CheckResult(FAIL, f"缺少表 {missing} ({found}/{total})")
        return CheckResult(PASS, f"必要表齐全 ({found}/{total})")
    except Exception as e:
        return CheckResult(FAIL, f"表检查失败: {e}")


async def c3_universe() -> CheckResult:
    """C3: active 股票数 >= 1。"""
    try:
        from db.connection import db_query_val
        cnt = await db_query_val("SELECT COUNT(*) FROM stock_universe WHERE active = TRUE")
        if cnt < 1:
            return CheckResult(FAIL, f"Universe 为空 (active={cnt})")
        return CheckResult(PASS, f"Universe: {cnt} 只 active 股票")
    except Exception as e:
        return CheckResult(FAIL, f"Universe 检查失败: {e}")


async def c4_regime() -> CheckResult:
    """C4: regime_daily 最新数据不超过 7 天。"""
    try:
        from db.connection import db_query_val
        max_date = await db_query_val("SELECT MAX(trade_date) FROM regime_daily")
        if max_date is None:
            return CheckResult(FAIL, "regime_daily 无数据")
        lag = (date.today() - max_date).days
        if lag > 7:
            return CheckResult(FAIL, f"Regime 数据过期 (max={max_date}, lag={lag}天)")
        return CheckResult(PASS, f"Regime 数据新鲜 (max={max_date}, lag={lag}天)")
    except Exception as e:
        return CheckResult(FAIL, f"Regime 检查失败: {e}")


async def c5_features() -> CheckResult:
    """C5: features_daily 近 7 天覆盖股票数 >= 10。"""
    try:
        from db.connection import db_query_val
        cnt = await db_query_val(
            """
            SELECT COUNT(DISTINCT symbol)
            FROM features_daily
            WHERE trade_date >= CURRENT_DATE - 7
            """
        )
        if cnt < 10:
            return CheckResult(FAIL, f"Features 近 7 天覆盖仅 {cnt} 只（需 >= 10）")
        return CheckResult(PASS, f"Features 近 7 天覆盖 {cnt} 只股票")
    except Exception as e:
        return CheckResult(FAIL, f"Features 检查失败: {e}")


async def c6_judgments() -> CheckResult:
    """C6: 有效判断数（排除 bug 标记）>= 1。"""
    try:
        from db.connection import db_query_val
        cnt = await db_query_val(
            "SELECT COUNT(*) FROM judgments WHERE fundamental_bug_affected IS NOT TRUE"
        )
        if cnt < 1:
            return CheckResult(FAIL, "无有效判断数据（均被标记为 bug 或无数据）")
        return CheckResult(PASS, f"有效判断: {cnt} 条")
    except Exception as e:
        return CheckResult(FAIL, f"判断检查失败: {e}")


async def c7_llm_budget() -> CheckResult:
    """C7: 今日 LLM 费用 < 100 CNY。"""
    try:
        from db.connection import db_query_val
        cost = await db_query_val(
            """
            SELECT COALESCE(SUM(cost_cny), 0)
            FROM llm_cost_log
            WHERE DATE(call_time) = CURRENT_DATE
            """
        )
        cost = float(cost)
        if cost >= 100:
            return CheckResult(FAIL, f"今日 LLM 费用超限: ¥{cost:.2f} >= ¥100")
        return CheckResult(PASS, f"LLM 预算正常 (今日 ¥{cost:.2f} < ¥100)")
    except Exception as e:
        return CheckResult(FAIL, f"LLM 预算检查失败: {e}")


async def c8_composite_analyzer() -> CheckResult:
    """C8: 能导入并实例化 CompositeAnalyzer。"""
    try:
        from core.analysis.composite import CompositeAnalyzer
        _ = CompositeAnalyzer()
        return CheckResult(PASS, "CompositeAnalyzer 导入并实例化成功")
    except Exception as e:
        return CheckResult(FAIL, f"CompositeAnalyzer 导入失败: {e}")


async def c9_scheduler() -> CheckResult:
    """C9: 能导入 build_scheduler（进程运行检测为可选，跳过）。"""
    try:
        from scheduler.scheduler import build_scheduler  # noqa: F401
        # 仅检查可导入，不启动（进程级检测在验收范围外标记 SKIP）
        return CheckResult(SKIP, "Scheduler 暂未启动 (进程检测可选)")
    except Exception as e:
        return CheckResult(FAIL, f"build_scheduler 导入失败: {e}")


async def c10_wiki() -> CheckResult:
    """C10: wiki_pages 中股票页面数 >= 1。"""
    try:
        from db.connection import db_query_val
        cnt = await db_query_val(
            "SELECT COUNT(*) FROM wiki_pages WHERE page_type = 'stock'"
        )
        if cnt < 1:
            return CheckResult(FAIL, "Wiki 无个股页面（运行 generate_stock_pages.py）")
        return CheckResult(PASS, f"Wiki: {cnt} 个个股页面")
    except Exception as e:
        return CheckResult(FAIL, f"Wiki 检查失败: {e}")


# ────────────────────────────────────────────────
# 主流程
# ────────────────────────────────────────────────

CHECKS: list[tuple[str, Callable[[], Awaitable[CheckResult]]]] = [
    ("C1", c1_db_connect),
    ("C2", c2_tables),
    ("C3", c3_universe),
    ("C4", c4_regime),
    ("C5", c5_features),
    ("C6", c6_judgments),
    ("C7", c7_llm_budget),
    ("C8", c8_composite_analyzer),
    ("C9", c9_scheduler),
    ("C10", c10_wiki),
]


async def run(verbose: bool = False) -> int:
    """执行所有检查，返回退出码（0=全部通过，1=有失败）。"""
    today = date.today().isoformat()

    print(f"\n{'═'*58}")
    print(f"  Phase 1 验收报告  {today}")
    print(f"{'═'*58}\n")

    results: list[CheckResult] = []
    for label, fn in CHECKS:
        try:
            r = await fn()
        except Exception as e:
            r = CheckResult(FAIL, f"检查抛出未预期异常: {e}")
        r.label = label
        results.append(r)
        print(r.fmt())

    # 汇总
    passed = sum(1 for r in results if r.code == PASS)
    failed = sum(1 for r in results if r.code == FAIL)
    skipped = sum(1 for r in results if r.code == SKIP)

    print(f"\n{'═'*58}")
    print(f"  结果: {passed} 通过 / {failed} 失败 / {skipped} 跳过")
    print(f"{'═'*58}\n")

    if verbose and failed:
        print("失败项详情：")
        for r in results:
            if r.code == FAIL:
                print(f"  {r.label}: {r.message}")
        print()

    # 关闭连接池
    try:
        from db.connection import close_pool
        await close_pool()
    except Exception:
        pass

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Phase 1 验收检查")
    parser.add_argument("--verbose", action="store_true", help="失败时输出详细信息")
    args = parser.parse_args()
    sys.exit(asyncio.run(run(verbose=args.verbose)))
