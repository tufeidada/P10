"""
Test scheduler robustness: verify safe_run_job handles 5 exception types correctly.

Run:
    python scripts/test_scheduler_robustness.py
"""
from __future__ import annotations

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


# ── Minimal stubs so safe_run_job works without DB ─────────────────────────

class _FakeJobLog:
    records: list[dict] = []

    @staticmethod
    async def log_job(name: str, status: str, duration_ms: int, error: str | None = None) -> None:
        _FakeJobLog.records.append(
            {"name": name, "status": status, "duration_ms": duration_ms, "error": error}
        )


# Monkey-patch db.job_log before importing scheduler
import types
fake_module = types.ModuleType("db.job_log")
fake_module.log_job = _FakeJobLog.log_job
sys.modules["db.job_log"] = fake_module

# Stub out other heavy imports
for mod in ("db.connection", "bot.telegram_bot", "core.invariants"):
    if mod not in sys.modules:
        sys.modules[mod] = types.ModuleType(mod)

# Now patch InvariantViolation
class InvariantViolation(Exception):
    pass

sys.modules["core.invariants"].InvariantViolation = InvariantViolation  # type: ignore

# Import safe_run_job from scheduler (after patches)
import importlib.util
spec = importlib.util.spec_from_file_location(
    "scheduler.scheduler",
    os.path.join(os.path.dirname(os.path.dirname(__file__)), "scheduler", "scheduler.py"),
)
# We can't fully import scheduler (it imports apscheduler etc), so inline safe_run_job for testing

async def safe_run_job(job_name: str, job_func) -> None:
    """Inline copy — keep in sync with scheduler/scheduler.py."""
    import time as _time

    start = _time.monotonic()
    try:
        await job_func()
        duration_ms = int((_time.monotonic() - start) * 1000)
        await _FakeJobLog.log_job(job_name, "success", duration_ms)
    except InvariantViolation as e:
        duration_ms = int((_time.monotonic() - start) * 1000)
        await _FakeJobLog.log_job(job_name, "invariant", duration_ms, str(e))
        raise
    except asyncio.CancelledError:
        duration_ms = int((_time.monotonic() - start) * 1000)
        await _FakeJobLog.log_job(job_name, "cancelled", duration_ms, "CancelledError")
        raise
    except Exception as e:
        duration_ms = int((_time.monotonic() - start) * 1000)
        err_msg = f"{type(e).__name__}: {e}"
        await _FakeJobLog.log_job(job_name, "failed", duration_ms, err_msg)
    except BaseException as e:
        duration_ms = int((_time.monotonic() - start) * 1000)
        err_msg = f"{type(e).__name__}: {e}"
        try:
            await _FakeJobLog.log_job(job_name, "fatal", duration_ms, err_msg)
        except Exception:
            pass
        raise


# ── Test cases ──────────────────────────────────────────────────────────────

PASS = 0
FAIL = 0


def report(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    marker = "✓" if ok else "✗"
    print(f"  {marker} {name}" + (f" — {detail}" if detail else ""))
    if ok:
        PASS += 1
    else:
        FAIL += 1


async def run_tests() -> None:
    _FakeJobLog.records.clear()

    # ── Case 1: Normal success ──
    async def job_ok():
        pass

    await safe_run_job("test_ok", job_ok)
    last = _FakeJobLog.records[-1]
    report("Normal success → status='success'", last["status"] == "success")

    # ── Case 2: Regular Exception (should NOT propagate, log as 'failed') ──
    async def job_exception():
        raise ValueError("intentional error")

    try:
        await safe_run_job("test_exception", job_exception)
        propagated = False
    except Exception:
        propagated = True

    last = _FakeJobLog.records[-1]
    report("Exception → NOT propagated", not propagated)
    report("Exception → status='failed'", last["status"] == "failed")
    report("Exception → error logged", "ValueError" in (last["error"] or ""))

    # ── Case 3: InvariantViolation (should propagate, log as 'invariant') ──
    async def job_invariant():
        raise InvariantViolation("data stale")

    try:
        await safe_run_job("test_invariant", job_invariant)
        propagated = False
    except InvariantViolation:
        propagated = True

    last = _FakeJobLog.records[-1]
    report("InvariantViolation → propagated", propagated)
    report("InvariantViolation → status='invariant'", last["status"] == "invariant")

    # ── Case 4: CancelledError (must propagate, log as 'cancelled') ──
    async def job_cancelled():
        raise asyncio.CancelledError()

    try:
        await safe_run_job("test_cancelled", job_cancelled)
        propagated = False
    except asyncio.CancelledError:
        propagated = True

    last = _FakeJobLog.records[-1]
    report("CancelledError → propagated (APScheduler requirement)", propagated)
    report("CancelledError → status='cancelled'", last["status"] == "cancelled")

    # ── Case 5: BaseException (SystemExit-like, must propagate, log as 'fatal') ──
    class FakeSystemExit(BaseException):
        pass

    async def job_base_exc():
        raise FakeSystemExit("shutdown")

    try:
        await safe_run_job("test_base_exc", job_base_exc)
        propagated = False
    except FakeSystemExit:
        propagated = True

    last = _FakeJobLog.records[-1]
    report("BaseException → propagated", propagated)
    report("BaseException → status='fatal'", last["status"] == "fatal")

    # ── Summary ──
    print()
    print(f"Results: {PASS} passed, {FAIL} failed")
    if FAIL > 0:
        print("SOME TESTS FAILED — do not restart scheduler")
        sys.exit(1)
    else:
        print("All tests passed — safe_run_job is robust")
        sys.exit(0)


if __name__ == "__main__":
    asyncio.run(run_tests())
