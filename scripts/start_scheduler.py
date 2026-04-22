"""
P10-AlphaRadar Scheduler 启动脚本。

启动前执行：
  1. M3 startup invariants（features 覆盖、universe 非空）
  2. M5 freshness check（critical 停更则拒绝启动）

全部通过 → 启动 APScheduler 常驻，推送 Telegram 通知。

用法：
    python scripts/start_scheduler.py
    python scripts/start_scheduler.py --dry-run          # 只跑自检，不启动
    python scripts/start_scheduler.py run_job <job>      # 单 job 冒烟测试
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import structlog

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
)

logger = structlog.get_logger(__name__)


_JOB_MAP: dict[str, str] = {
    "check_data_freshness": "task_check_data_freshness",
    "pull_cn_market_data": "task_pull_cn_market_data",
    "pull_us_market_data": "task_pull_us_market_data",
    "update_features_daily_cn": "task_update_features_daily_cn",
    "update_features_daily_us": "task_update_features_daily_us",
    "detect_regime_cn": "task_detect_regime_cn",
    "detect_regime_us": "task_detect_regime_us",
    "run_composite_analysis_cn": "task_run_composite_analysis_cn",
    "run_composite_analysis_us": "task_run_composite_analysis_us",
    "send_daily_digest": "task_send_daily_digest",
    "heartbeat": "task_heartbeat",
    "backfill_judgments": "task_backfill_judgments",
    "weekly_review": "task_weekly_review",
    "monthly_review": "task_monthly_review",
}


async def run_single_job(job_name: str) -> bool:
    """单 job 冒烟测试：初始化 DB，调用指定任务，退出。

    Args:
        job_name: job 标识，见 _JOB_MAP。

    Returns:
        成功返回 True，失败返回 False。
    """
    import importlib
    from db.connection import init_pool, close_pool

    if job_name not in _JOB_MAP:
        print(f"❌ 未知 job: {job_name}")
        print(f"可用 job: {', '.join(sorted(_JOB_MAP))}")
        return False

    await init_pool()
    try:
        func_name = _JOB_MAP[job_name]
        mod = importlib.import_module("scheduler.scheduler")
        func = getattr(mod, func_name)
        print(f"▶ 运行 {job_name} ...")
        import time
        t0 = time.monotonic()
        await func()
        elapsed = int((time.monotonic() - t0) * 1000)
        print(f"✅ {job_name} 完成 ({elapsed}ms)")
        return True
    except Exception as e:
        print(f"❌ {job_name} 失败: {type(e).__name__}: {e}")
        return False
    finally:
        await close_pool()


async def run_dry_run() -> bool:
    """只跑自检，不启动 scheduler。

    Returns:
        自检通过返回 True，失败返回 False。
    """
    from db.connection import init_pool, close_pool
    from scheduler.scheduler import _startup_checks
    from core.invariants import InvariantViolation

    await init_pool()
    try:
        await _startup_checks()
        print("\n✅ 所有启动自检通过，可以启动 scheduler。\n")
        return True
    except (InvariantViolation, RuntimeError) as e:
        print(f"\n❌ 启动自检失败：{e}\n")
        return False
    finally:
        await close_pool()


def main() -> None:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="启动 P10-AlphaRadar Scheduler")
    parser.add_argument("--dry-run", action="store_true", help="只跑自检，不启动")
    parser.add_argument("action", nargs="?", help="run_job")
    parser.add_argument("job_name", nargs="?", help="job 名称（配合 run_job 使用）")
    args = parser.parse_args()

    if args.dry_run:
        ok = asyncio.run(run_dry_run())
        sys.exit(0 if ok else 1)

    if args.action == "run_job":
        if not args.job_name:
            print(f"用法: python scripts/start_scheduler.py run_job <job_name>")
            print(f"可用 job: {', '.join(sorted(_JOB_MAP))}")
            sys.exit(1)
        ok = asyncio.run(run_single_job(args.job_name))
        sys.exit(0 if ok else 1)

    # 正式启动（调用 scheduler 的 main）
    from scheduler.scheduler import main as scheduler_main
    scheduler_main()


if __name__ == "__main__":
    main()
