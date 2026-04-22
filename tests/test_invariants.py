"""
core/invariants.py 单元测试。

覆盖 5 个 API 的 happy path + failure path。
这是 Phase 1 唯一强制要求的测试文件。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from core.invariants import (
    InvariantViolation,
    assert_fresh,
    assert_in,
    assert_not_empty,
    assert_range,
    assert_superset,
)


# ── assert_in ────────────────────────────────────────────────

def test_assert_in_happy():
    """合法值不抛出。"""
    assert_in("offense", {"offense", "defense"}, "test.ctx")


def test_assert_in_fail():
    """非法值抛出 InvariantViolation。"""
    with pytest.raises(InvariantViolation, match="bull_trend"):
        assert_in("bull_trend", {"offense", "defense"}, "regime_detector.output")


def test_assert_in_fail_message_contains_context():
    """错误信息包含 context 字段。"""
    with pytest.raises(InvariantViolation, match="my.context"):
        assert_in("x", {"a", "b"}, "my.context")


# ── assert_range ─────────────────────────────────────────────

def test_assert_range_happy_boundary():
    """边界值（0.0 和 100.0）不抛出。"""
    assert_range(0.0, 0.0, 100.0, "test.score")
    assert_range(100.0, 0.0, 100.0, "test.score")
    assert_range(50.0, 0.0, 100.0, "test.score")


def test_assert_range_fail_above():
    """超出上界抛出。"""
    with pytest.raises(InvariantViolation, match="100.1"):
        assert_range(100.1, 0.0, 100.0, "composite.final_score")


def test_assert_range_fail_below():
    """低于下界抛出。"""
    with pytest.raises(InvariantViolation, match="-0.1"):
        assert_range(-0.1, 0.0, 100.0, "composite.final_score")


# ── assert_superset ──────────────────────────────────────────

def test_assert_superset_happy():
    """actual 包含 required 不抛出。"""
    assert_superset({"A", "B", "C"}, {"A", "B"}, "startup.coverage")


def test_assert_superset_exact():
    """actual == required 不抛出。"""
    assert_superset({"A", "B"}, {"A", "B"}, "startup.coverage")


def test_assert_superset_fail():
    """actual 缺少 required 元素时抛出，错误信息包含缺失项。"""
    with pytest.raises(InvariantViolation, match="000001.SZ"):
        assert_superset({"AAPL"}, {"AAPL", "000001.SZ"}, "startup.features_coverage")


# ── assert_fresh ─────────────────────────────────────────────

def test_assert_fresh_happy():
    """今天的数据不抛出。"""
    assert_fresh(date.today(), max_age_days=3, context="data_source.test")


def test_assert_fresh_within_max():
    """在 max_age_days 以内不抛出。"""
    assert_fresh(date.today() - timedelta(days=2), max_age_days=3, context="data_source.test")


def test_assert_fresh_fail_none():
    """None 表示从未更新，必须抛出。"""
    with pytest.raises(InvariantViolation, match="None"):
        assert_fresh(None, max_age_days=3, context="data_source.features_daily")


def test_assert_fresh_fail_stale():
    """超过 max_age_days 抛出，错误信息包含 age。"""
    stale = date.today() - timedelta(days=10)
    with pytest.raises(InvariantViolation, match="age=10"):
        assert_fresh(stale, max_age_days=3, context="data_source.features_daily")


# ── assert_not_empty ─────────────────────────────────────────

def test_assert_not_empty_happy_list():
    """非空列表不抛出。"""
    assert_not_empty([1, 2, 3], "test.list")


def test_assert_not_empty_happy_set():
    """非空集合不抛出。"""
    assert_not_empty({"a"}, "test.set")


def test_assert_not_empty_fail_list():
    """空列表抛出。"""
    with pytest.raises(InvariantViolation, match="empty"):
        assert_not_empty([], "universe.symbols")


def test_assert_not_empty_fail_dict():
    """空字典抛出。"""
    with pytest.raises(InvariantViolation, match="empty"):
        assert_not_empty({}, "regime.config")
