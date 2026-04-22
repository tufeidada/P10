"""
运行时不变量断言 —— 让静默失败立即显性化。

哲学：
- 宁可当场 crash，不要 silent fallback
- 生产环境 assertion 失败 → 立即 Telegram 告警 + 停止后续处理
- 开发环境 assertion 失败 → raise，让测试/CI 捕获
"""

from __future__ import annotations

from datetime import date
from typing import Any, Iterable, Sized


class InvariantViolation(Exception):
    """不变量违规，表示系统处于不应该出现的状态。"""

    pass


def assert_in(value: Any, allowed: Iterable, context: str) -> None:
    """断言 value 在 allowed 集合中。

    Args:
        value: 被检查的值。
        allowed: 合法值的集合或可迭代对象。
        context: 断言位置描述，用于错误信息。

    Raises:
        InvariantViolation: value 不在 allowed 中。
    """
    allowed_set = set(allowed)
    if value not in allowed_set:
        raise InvariantViolation(
            f"[{context}] value={value!r} not in allowed={sorted(str(x) for x in allowed_set)}"
        )


def assert_range(value: float, lo: float, hi: float, context: str) -> None:
    """断言 value ∈ [lo, hi]。

    Args:
        value: 被检查的数值。
        lo: 区间下界（含）。
        hi: 区间上界（含）。
        context: 断言位置描述。

    Raises:
        InvariantViolation: value 不在 [lo, hi] 范围内。
    """
    if not (lo <= value <= hi):
        raise InvariantViolation(
            f"[{context}] value={value} out of range [{lo}, {hi}]"
        )


def assert_superset(actual: set, required: set, context: str) -> None:
    """断言 actual ⊇ required（actual 包含 required 的全部元素）。

    Args:
        actual: 实际集合。
        required: 要求必须存在的元素集合。
        context: 断言位置描述。

    Raises:
        InvariantViolation: actual 缺少 required 中的某些元素。
    """
    missing = required - actual
    if missing:
        raise InvariantViolation(
            f"[{context}] missing required elements: {sorted(str(x) for x in missing)}"
        )


def assert_fresh(last_update_date: date | None, max_age_days: int, context: str) -> None:
    """断言数据新鲜度：last_update_date 距今不超过 max_age_days 天。

    Args:
        last_update_date: 最后更新日期，None 表示从未更新。
        max_age_days: 允许的最大过期天数。
        context: 断言位置描述。

    Raises:
        InvariantViolation: 数据为 None 或已过期超过 max_age_days 天。
    """
    if last_update_date is None:
        raise InvariantViolation(f"[{context}] last_update_date is None（数据从未更新）")
    age = (date.today() - last_update_date).days
    if age > max_age_days:
        raise InvariantViolation(
            f"[{context}] data stale: last_update={last_update_date}, age={age}d, max_allowed={max_age_days}d"
        )


def assert_not_empty(seq: Sized, context: str) -> None:
    """断言序列/集合非空。

    Args:
        seq: 任何实现 __len__ 的对象（list、set、dict 等）。
        context: 断言位置描述。

    Raises:
        InvariantViolation: seq 长度为 0。
    """
    if len(seq) == 0:
        raise InvariantViolation(f"[{context}] sequence is empty")
