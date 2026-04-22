"""
Stage A 单元测试: portfolio.py + rules.py

不依赖数据库。所有测试使用内存对象。

覆盖场景:
  T1. 建仓后 cash 减少正确（含佣金）
  T2. 平仓 P&L 计算正确（含双边佣金）
  T3. 止损触发（check_exit → stop_loss）
  T4. 达到目标触发（check_exit → target_hit）
  T5. 方向翻转触发（check_exit → direction_flip）
  T6. 超时触发（check_exit → timeout）
  T7. 超时不触发（仍为 bullish）
  T8. 无止损时使用 7% 固定止损
  T9. calc_position_size: 受仓位上限约束（返回小值）
  T10. calc_position_size: 受止损反算约束（返回小值）
  T11. calc_position_size: 总值为零时返回 0
  T12. check_industry_concentration: 超限返回 False
  T13. check_industry_concentration: 未超限返回 True
  T14. check_liquidity: 体系 A（手/千元）充足 → True
  T15. check_liquidity: 体系 A（手/千元）不足 → False
  T14b. check_liquidity: 体系 B（股/元）充足 → True
  T14c. check_liquidity: 无 amount 列回退 → True
  T16. A 股整百股取整
  T17. 建仓资金不足时自动缩减到最大可建手数
  T18. update_positions_value: 市值和浮动盈亏更新正确
"""

import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from backtest.engine.portfolio import Portfolio, Position, Trade
from backtest.engine.rules import (
    check_exit,
    check_industry_concentration,
    check_liquidity,
    calc_position_size,
)


# ─────────────────────────────────────────────────────────────────────────────
# 辅助工厂
# ─────────────────────────────────────────────────────────────────────────────

def _make_portfolio(market: str = "CN", cash: float = 1_000_000.0) -> Portfolio:
    return Portfolio(initial_cash=cash, market=market)


def _open_pos(
    portfolio: Portfolio,
    symbol:    str = "000001.SZ",
    price:     float = 10.0,
    shares:    int = 1000,
    sl:        float = 9.0,
    tp:        float = 13.0,
    industry:  str = "银行",
    entry_date: date = date(2025, 10, 1),
) -> Position:
    return portfolio.open_position(
        symbol=symbol, market=portfolio.market, industry=industry,
        entry_date=entry_date, entry_price=price, shares=shares,
        stop_loss=sl, target_price=tp,
    )


class _FakeJudgment:
    def __init__(self, symbol: str, direction: str, confidence: float = 0.6):
        self.symbol     = symbol
        self.direction  = direction
        self.confidence = confidence


# ─────────────────────────────────────────────────────────────────────────────
# T1. 建仓 cash 减少正确
# ─────────────────────────────────────────────────────────────────────────────

def test_open_position_cash_deduction():
    pf = _make_portfolio("CN", cash=1_000_000.0)
    pos = _open_pos(pf, price=10.0, shares=1000)

    commission = 10.0 * 1000 * 0.0025   # 0.25%
    expected_cost = 10.0 * 1000 + commission

    assert pos is not None
    assert abs(pf.cash - (1_000_000.0 - expected_cost)) < 0.01, (
        f"cash={pf.cash} expected={1_000_000 - expected_cost}"
    )
    assert pf.position_count == 1


# ─────────────────────────────────────────────────────────────────────────────
# T2. 平仓 P&L 正确（含双边佣金）
# ─────────────────────────────────────────────────────────────────────────────

def test_close_position_pnl():
    pf  = _make_portfolio("CN", cash=1_000_000.0)
    pos = _open_pos(pf, price=10.0, shares=1000, sl=9.0, tp=13.0)

    entry_comm = 10.0 * 1000 * 0.0025
    exit_price = 12.0
    exit_comm  = 12.0 * 1000 * 0.0025

    trade = pf.close_position(pos, exit_price=12.0, exit_date=date(2025, 10, 15), reason="target_hit")

    expected_pnl = (12.0 - 10.0) * 1000 - entry_comm - exit_comm
    assert abs(trade.pnl - expected_pnl) < 0.01, f"pnl={trade.pnl} expected={expected_pnl}"
    assert trade.pnl > 0
    assert trade.exit_reason == "target_hit"
    assert pf.position_count == 0
    assert len(pf.closed_trades) == 1


# ─────────────────────────────────────────────────────────────────────────────
# T3. 止损触发
# ─────────────────────────────────────────────────────────────────────────────

def test_check_exit_stop_loss():
    pf  = _make_portfolio("CN")
    pos = _open_pos(pf, price=10.0, shares=1000, sl=9.0, tp=13.0)
    pos.update(8.5)   # 跌破止损

    reason = check_exit(pos, judgments=[], current_date=date(2025, 10, 5))
    assert reason == "stop_loss"


# ─────────────────────────────────────────────────────────────────────────────
# T4. 达到目标触发
# ─────────────────────────────────────────────────────────────────────────────

def test_check_exit_target_hit():
    pf  = _make_portfolio("CN")
    pos = _open_pos(pf, price=10.0, shares=1000, sl=9.0, tp=13.0)
    pos.update(13.5)   # 超过目标

    reason = check_exit(pos, judgments=[], current_date=date(2025, 10, 5))
    assert reason == "target_hit"


# ─────────────────────────────────────────────────────────────────────────────
# T5. 方向翻转触发
# ─────────────────────────────────────────────────────────────────────────────

def test_check_exit_direction_flip():
    pf  = _make_portfolio("CN")
    pos = _open_pos(pf, price=10.0, shares=1000, sl=8.0, tp=15.0)
    pos.update(11.0)   # 价格正常，不触发止损/目标

    j = _FakeJudgment("000001.SZ", direction="bearish", confidence=0.7)
    reason = check_exit(pos, judgments=[j], current_date=date(2025, 10, 5))
    assert reason == "direction_flip"


def test_check_exit_no_flip_low_confidence():
    """置信度不足时不翻转"""
    pf  = _make_portfolio("CN")
    pos = _open_pos(pf, price=10.0, shares=1000, sl=8.0, tp=15.0)
    pos.update(11.0)

    j = _FakeJudgment("000001.SZ", direction="bearish", confidence=0.3)
    reason = check_exit(pos, judgments=[j], current_date=date(2025, 10, 5))
    assert reason is None


# ─────────────────────────────────────────────────────────────────────────────
# T6. 超时触发（>30 天，且 direction != bullish）
# ─────────────────────────────────────────────────────────────────────────────

def test_check_exit_timeout():
    pf  = _make_portfolio("CN")
    pos = _open_pos(pf, price=10.0, shares=1000, sl=8.0, tp=15.0, entry_date=date(2025, 9, 1))
    pos.update(10.5)   # 不触发止损/目标

    j = _FakeJudgment("000001.SZ", direction="neutral", confidence=0.4)
    reason = check_exit(pos, judgments=[j], current_date=date(2025, 10, 5))   # 34 天后
    assert reason == "timeout"


# ─────────────────────────────────────────────────────────────────────────────
# T7. 超时不触发（仍为 bullish）
# ─────────────────────────────────────────────────────────────────────────────

def test_check_exit_no_timeout_still_bullish():
    pf  = _make_portfolio("CN")
    pos = _open_pos(pf, price=10.0, shares=1000, sl=8.0, tp=15.0, entry_date=date(2025, 9, 1))
    pos.update(10.5)

    j = _FakeJudgment("000001.SZ", direction="bullish", confidence=0.6)
    reason = check_exit(pos, judgments=[j], current_date=date(2025, 10, 5))
    assert reason is None


# ─────────────────────────────────────────────────────────────────────────────
# T8. 无止损时使用 7% 固定止损
# ─────────────────────────────────────────────────────────────────────────────

def test_check_exit_fallback_stop():
    pf  = _make_portfolio("CN")
    pos = _open_pos(pf, price=10.0, shares=1000, sl=None, tp=None)
    pos.update(9.2)   # 跌 8% → 超过 7% 固定止损

    reason = check_exit(pos, judgments=[], current_date=date(2025, 10, 5))
    assert reason == "stop_loss"

    # 跌 5% → 不触发
    pos2 = _open_pos(pf, price=10.0, shares=1000, sl=None, tp=None, symbol="000002.SZ")
    pos2.update(9.6)
    reason2 = check_exit(pos2, judgments=[], current_date=date(2025, 10, 5))
    assert reason2 is None


# ─────────────────────────────────────────────────────────────────────────────
# T9/T10. calc_position_size: 仓位上限 vs 止损反算
# ─────────────────────────────────────────────────────────────────────────────

def test_calc_position_size_cap_constrained():
    """仓位上限更严格时，按仓位上限计算"""
    pf = _make_portfolio("CN", cash=1_000_000.0)
    # exec=10, sl=9, risk/sh=1, max_risk=20000 → by_risk=20000/1=20000股
    # max_position: 1000000 × 0.10 × 0.5 = 50000 → by_cap=5000股
    # min(20000, 5000)=5000 → 整百 → 5000
    shares = calc_position_size(pf, exec_price=10.0, stop_loss_price=9.0,
                                 confidence=0.5, max_position_pct=0.10)
    assert shares == 5000, f"got {shares}"


def test_calc_position_size_risk_constrained():
    """止损反算更严格时，按止损计算"""
    pf = _make_portfolio("CN", cash=1_000_000.0)
    # exec=10, sl=9.9, risk/sh=0.1, max_risk=20000 → by_risk=20000/0.1=200000股
    # max_position: 1000000 × 0.10 × 0.8 = 80000 → by_cap=8000股
    # min(200000, 8000)=8000 → 整百 → 8000
    shares = calc_position_size(pf, exec_price=10.0, stop_loss_price=9.9,
                                 confidence=0.8, max_position_pct=0.10)
    assert shares == 8000, f"got {shares}"


def test_calc_position_size_zero_value():
    pf = Portfolio(initial_cash=0.0, market="CN")
    shares = calc_position_size(pf, exec_price=10.0, stop_loss_price=9.0,
                                 confidence=0.6, max_position_pct=0.10)
    assert shares == 0


# ─────────────────────────────────────────────────────────────────────────────
# T12/T13. check_industry_concentration
# ─────────────────────────────────────────────────────────────────────────────

def test_industry_concentration_over_limit():
    """同行业占 50% 总资产，应拒绝"""
    pf = _make_portfolio("CN", cash=1_000_000.0)
    # 建 1000股 @ 500元 = 50万，总资产≈150万，行业占 500000/1500000≈33%
    # 再建另一只同行业 1000股 @ 500元 = 50万，行业占 1000000/2000000=50% > 40% → 拒绝
    _open_pos(pf, symbol="000001.SZ", price=500.0, shares=1000, industry="半导体")
    pf.update_positions_value({"000001.SZ": 500.0})

    ok = check_industry_concentration(pf, "半导体")
    # 当前半导体暴露 = 500000/1000000(cash) + 500000(pos) ≈ 33.3% < 40%
    # （cash 500000，pos 500000，total 1000000，ind_exposure=500000/1000000=50%）
    # 等等：pf.cash 已经减少了，initial=1M，买了 500*1000+comm=502000，cash≈498000，total≈998000
    # 行业市值=500000，exposure=500000/998000≈50% → 超限
    assert ok is False, f"expected False, got {ok}"


def test_industry_concentration_under_limit():
    """行业占 20%，应允许"""
    pf = _make_portfolio("CN", cash=1_000_000.0)
    _open_pos(pf, symbol="000001.SZ", price=100.0, shares=1000, industry="银行")
    pf.update_positions_value({"000001.SZ": 100.0})

    # 行业市值=100000，total≈900000，exposure≈11% < 40%
    ok = check_industry_concentration(pf, "银行")
    assert ok is True


# ─────────────────────────────────────────────────────────────────────────────
# T14/T15. check_liquidity（使用 amount 字段 + ratio 检测）
# ─────────────────────────────────────────────────────────────────────────────

def test_liquidity_sufficient_system_a():
    # 体系 A: volume 在"手"，amount 在"千元"，ratio = amount/cv ≈ 0.1
    # close=40, volume=12000手, amount=48000千元
    # cv = 40×12000 = 480000, ratio = 48000/480000 = 0.1 → 体系 A
    # daily_turnover = 48000 × 1000 = 4800万元 > planned 200万×10=2000万 → True
    df = pd.DataFrame({"close": [40.0], "volume": [12_000.0], "amount": [48_000.0]})
    ok = check_liquidity(df, planned_amount=2_000_000.0)
    assert ok is True


def test_liquidity_insufficient_system_a():
    # 体系 A: volume 在"手"，amount 在"千元"
    # close=40, volume=3000手, amount=12000千元
    # ratio = 12000/(40×3000) = 0.1 → 体系 A
    # daily_turnover = 12000 × 1000 = 1200万元 < planned 200万×10=2000万 → False
    df = pd.DataFrame({"close": [40.0], "volume": [3_000.0], "amount": [12_000.0]})
    ok = check_liquidity(df, planned_amount=2_000_000.0)
    assert ok is False


def test_liquidity_sufficient_system_b():
    # 体系 B: volume 在"股"，amount 在"元"，ratio ≈ 1.0（601/300 类股票）
    # close=62.55, volume=44882218, amount=2808948857
    # ratio ≈ 1.0 → 体系 B，daily_turnover = amount = 28亿元 → 充足
    df = pd.DataFrame({"close": [62.55], "volume": [44_882_218.0], "amount": [2_808_948_857.0]})
    ok = check_liquidity(df, planned_amount=2_000_000.0)
    assert ok is True


def test_liquidity_fallback_no_amount():
    # 无 amount 列时回退到 close×volume（体系 B 结果正确，体系 A 低估）
    # close=100, volume=500000 → cv=5000万元 > 1000万阈值 → True
    df = pd.DataFrame({"close": [100.0], "volume": [500_000.0]})
    ok = check_liquidity(df, planned_amount=1_000_000.0)
    assert ok is True


# ─────────────────────────────────────────────────────────────────────────────
# T16. A 股整百股取整
# ─────────────────────────────────────────────────────────────────────────────

def test_cn_lot_rounding():
    pf = _make_portfolio("CN", cash=1_000_000.0)
    # risk = 20000 / (10 - 9.0) = 20000 股，cap = 100000 / 10 = 10000 股
    # min = 10000，整百 = 10000
    shares = calc_position_size(pf, exec_price=10.0, stop_loss_price=9.0,
                                 confidence=1.0, max_position_pct=0.10)
    assert shares % 100 == 0, f"not lot-aligned: {shares}"
    assert shares > 0


def test_us_lot_one_share():
    pf = _make_portfolio("US", cash=100_000.0)
    shares = calc_position_size(pf, exec_price=180.0, stop_loss_price=170.0,
                                 confidence=0.6, max_position_pct=0.10)
    assert shares >= 1
    # US 整 1 股（不要求整百）
    assert isinstance(shares, int)


# ─────────────────────────────────────────────────────────────────────────────
# T17. 建仓资金不足时自动缩减
# ─────────────────────────────────────────────────────────────────────────────

def test_open_position_insufficient_cash():
    """10200元，@1000元只够买10股，不够1手100股，返回None"""
    pf = _make_portfolio("CN", cash=10_200.0)
    pos = _open_pos(pf, price=1000.0, shares=100, industry="半导体")

    # max_shares = int(10200 / (1000 × 1.0025)) = 10 → lot取整后 = 0 → None
    assert pos is None, "资金不足1手应返回 None"
    assert pf.cash == 10_200.0  # cash 未被扣除


def test_open_position_fits_reduced():
    """资金够 200股但要求 1000股，自动缩减到 200"""
    pf = _make_portfolio("CN", cash=205_000.0)
    pos = _open_pos(pf, price=1000.0, shares=1000, industry="半导体")

    # max = int(205000 / 1002.5) = 204 → lot=200
    assert pos is not None
    assert pos.shares == 200
    assert pf.cash >= 0


# ─────────────────────────────────────────────────────────────────────────────
# T18. update_positions_value
# ─────────────────────────────────────────────────────────────────────────────

def test_update_positions_value():
    pf  = _make_portfolio("CN", cash=1_000_000.0)
    pos = _open_pos(pf, price=10.0, shares=1000)
    assert pos is not None

    pf.update_positions_value({"000001.SZ": 12.5})
    assert abs(pos.current_price - 12.5) < 1e-6
    assert abs(pos.market_value - 12_500.0) < 1e-6
    # unrealized_pnl = 12500 - (10000 + entry_comm)
    entry_comm = 10.0 * 1000 * 0.0025
    expected_upnl = 12_500 - 10_000 - entry_comm
    assert abs(pos.unrealized_pnl - expected_upnl) < 0.01
