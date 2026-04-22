"""
backtest/engine/rules.py — 建仓 / 平仓 / 仓位 / 流动性规则

所有交易决策逻辑集中在此。engine.py 调用这里的函数，不直接嵌入规则细节。

规则汇总 (Spec 7.3):
  平仓:
    1. 止损:      current_price <= stop_loss（或低于入场价 7%）
    2. 达到目标:  current_price >= target_price（或高于入场价 15%）
    3. 方向翻转:  最新 judgment.direction='bearish' AND confidence > 0.5
    4. 超时:      持有 > 30 日 AND 当前 direction != 'bullish'

  建仓过滤:
    5. 行业集中度 ≤ 40%（同行业持仓市值占总资产比）
    6. 日流动性:   标的近 1 日成交额 > 计划建仓金额 × 10

  仓位计算:
    7. 止损反算:  单笔最大亏损 2% 总资产 → shares_by_risk
    8. 仓位上限:  portfolio.value × max_position_pct × confidence → shares_by_cap
    9. 最终取 min(shares_by_risk, shares_by_cap)，整手取整
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Optional

import pandas as pd

from backtest.engine.portfolio import Portfolio, Position

log = logging.getLogger(__name__)

# 硬编码规则参数（与 Spec 7.3 一致，不从 yaml 读以保证 PIT 无关性）
_MAX_LOSS_PCT        = 0.02   # 单笔最大亏损 2% 总资产
_MAX_INDUSTRY_PCT    = 0.40   # 同行业持仓 ≤ 40%
_LIQUIDITY_MULT      = 10.0   # 成交额 > 计划建仓金额 × 10
_TIMEOUT_DAYS        = 30     # 超时天数
_FALLBACK_STOP_PCT   = 0.07   # 无 stop_loss 时用 7% 止损
_FALLBACK_TARGET_PCT = 0.15   # 无 target_price 时用 15% 目标
_FLIP_CONF_THR       = 0.50   # 方向翻转的最低置信度


# ═════════════════════════════════════════════════════════════════════════════
# 平仓规则
# ═════════════════════════════════════════════════════════════════════════════

def check_exit(
    position:     Position,
    judgments:    list,          # list[Judgment] — 当日所有 judgment
    current_date: date,
) -> Optional[str]:
    """
    检查是否满足平仓条件。

    Args:
        position:     当前持仓（current_price 已更新为今日收盘价）。
        judgments:    当日对所有标的的判断列表（含该标的的最新 judgment）。
        current_date: 当前回测日期（用于计算持有天数）。

    Returns:
        平仓原因字符串，或 None（不平仓）。
        reason: 'stop_loss' | 'target_hit' | 'direction_flip' | 'timeout'
    """
    price = position.current_price

    # 止损阈值：优先用 judgment 给的 stop_loss，否则用固定 7%
    sl = position.stop_loss
    if sl is None:
        sl = position.entry_price * (1 - _FALLBACK_STOP_PCT)

    # 目标价：优先用 judgment 给的 target_price，否则用固定 15%
    tp = position.target_price
    if tp is None:
        tp = position.entry_price * (1 + _FALLBACK_TARGET_PCT)

    # 1. 止损
    if price <= sl:
        log.info(f"rules: stop_loss {position.symbol} price={price:.4f} sl={sl:.4f}")
        return "stop_loss"

    # 2. 达到目标
    if price >= tp:
        log.info(f"rules: target_hit {position.symbol} price={price:.4f} tp={tp:.4f}")
        return "target_hit"

    # 3. 方向翻转（bearish + 高置信度）
    latest_j = next((j for j in judgments if j.symbol == position.symbol), None)
    if latest_j and latest_j.direction == "bearish" and latest_j.confidence > _FLIP_CONF_THR:
        log.info(
            f"rules: direction_flip {position.symbol} conf={latest_j.confidence:.2f}"
        )
        return "direction_flip"

    # 4. 超时（持有 > 30 日且不再看多）
    days_held = (current_date - position.entry_date).days
    if days_held > _TIMEOUT_DAYS:
        if latest_j is None or latest_j.direction != "bullish":
            log.info(
                f"rules: timeout {position.symbol} days={days_held} "
                f"dir={latest_j.direction if latest_j else 'N/A'}"
            )
            return "timeout"

    return None


def stop_price(position: Position) -> float:
    """返回该持仓的实际止损价（用于 T+1 成交时取止损价而非开盘价）。"""
    if position.stop_loss is not None:
        return position.stop_loss
    return position.entry_price * (1 - _FALLBACK_STOP_PCT)


def target_price_of(position: Position) -> float:
    """返回该持仓的实际目标价。"""
    if position.target_price is not None:
        return position.target_price
    return position.entry_price * (1 + _FALLBACK_TARGET_PCT)


# ═════════════════════════════════════════════════════════════════════════════
# 建仓过滤
# ═════════════════════════════════════════════════════════════════════════════

def check_industry_concentration(portfolio: Portfolio, industry: str) -> bool:
    """
    检查同行业集中度。

    Returns:
        True  → 可以建仓（不超限）
        False → 超出 40% 行业上限，拒绝
    """
    current_exposure = portfolio.industry_exposure(industry)
    if current_exposure >= _MAX_INDUSTRY_PCT:
        log.debug(
            f"rules: industry_over_concentrated {industry} "
            f"exposure={current_exposure:.1%}"
        )
        return False
    return True


def check_liquidity(
    bars_df:         pd.DataFrame,
    planned_amount:  float,
) -> bool:
    """
    检查流动性是否充足。

    数据库存在两种单位体系（由 Tushare 数据源和入库时间决定）：
      体系 A: volume 单位为"手"(100股), amount 单位为"千元" → ratio(amount/cv) ≈ 0.1
              适用于 000/002/688 股（及 2026-03 之前的大部分 A 股）
      体系 B: volume 单位为"股", amount 单位为"元"          → ratio(amount/cv) ≈ 1.0
              适用于 601/300 股（及 2026-03 之后的所有 A 股）

    优先用 amount 字段加 ratio 检测推算实际成交额，比 close×volume 更可靠。
    当 amount 缺失时回退到 close×volume（可能低估体系 A 股票约 100 倍）。

    Args:
        bars_df:        近期日线数据（含 'close'、'volume'、可选 'amount' 列）。
        planned_amount: 计划建仓金额（元）。

    Returns:
        True  → 流动性充足（最近 1 日成交额 > planned_amount × 10）
        False → 流动性不足
    """
    if bars_df is None or bars_df.empty:
        log.debug("rules: liquidity check skipped (no bar data)")
        return True

    if "close" not in bars_df.columns or "volume" not in bars_df.columns:
        log.debug("rules: liquidity check skipped (missing close/volume)")
        return True

    latest = bars_df.iloc[-1]
    close  = float(latest["close"])  if latest["close"]  is not None else 0.0
    volume = float(latest["volume"]) if latest["volume"] is not None else 0.0

    if close <= 0 or volume <= 0:
        return True   # 停牌日，放行（由 get_open_price 失败处理）

    # 用 amount/cv 比值检测单位体系，推算实际日成交额（元）
    cv = close * volume
    daily_turnover: float
    if "amount" in bars_df.columns and latest.get("amount") is not None:
        amount_val = float(latest["amount"])
        if amount_val > 0 and cv > 0:
            ratio = amount_val / cv
            if ratio < 0.5:
                # 体系 A：volume 在"手"，amount 在"千元" → 实际成交 = amount × 1000
                daily_turnover = amount_val * 1000.0
            else:
                # 体系 B：volume 在"股"，amount 在"元" → 实际成交 = amount
                daily_turnover = amount_val
        else:
            daily_turnover = cv  # amount 无效，回退
    else:
        # 无 amount 列，回退到 close×volume（体系 A 下会低估 100 倍）
        daily_turnover = cv

    threshold = planned_amount * _LIQUIDITY_MULT
    ok = daily_turnover >= threshold

    if not ok:
        log.debug(
            f"rules: liquidity_fail turnover={daily_turnover/10000:.1f}万元 "
            f"threshold={threshold/10000:.1f}万元"
        )
    return ok


# ═════════════════════════════════════════════════════════════════════════════
# 仓位计算
# ═════════════════════════════════════════════════════════════════════════════

def calc_position_size(
    portfolio:        Portfolio,
    exec_price:       float,
    stop_loss_price:  Optional[float],
    confidence:       float,
    max_position_pct: float,
) -> int:
    """
    基于止损反算最优仓位。

    策略:
      shares_by_risk = (portfolio.value × 2%) / risk_per_share
      shares_by_cap  = (portfolio.value × max_position_pct × confidence) / exec_price
      shares = min(shares_by_risk, shares_by_cap)

    Args:
        portfolio:        当前投资组合（用于读取 value 和 market）。
        exec_price:       T+1 成交价（开盘价）。
        stop_loss_price:  止损价（None 时用 7% 止损反算）。
        confidence:       判断置信度（0-1），用于缩减仓位上限。
        max_position_pct: Regime 参数，单只股票最大仓位比例（0-1）。

    Returns:
        整手股数（≥ 0）。返回 0 表示风险过大或资金不足，不建仓。
    """
    if exec_price <= 0:
        return 0

    # 止损价（fallback 7%）
    sl = stop_loss_price if stop_loss_price and stop_loss_price > 0 else exec_price * (1 - _FALLBACK_STOP_PCT)

    risk_per_share = exec_price - sl
    if risk_per_share <= 0:
        log.debug("rules: stop_loss >= exec_price, skip position")
        return 0

    total_value = portfolio.value
    if total_value <= 0:
        return 0

    # 方法1: 止损反算
    max_risk    = total_value * _MAX_LOSS_PCT
    shares_risk = max_risk / risk_per_share

    # 方法2: 仓位上限 × 置信度
    max_amount   = total_value * max_position_pct * min(confidence, 1.0)
    shares_cap   = max_amount / exec_price

    raw_shares = min(shares_risk, shares_cap)

    # 整手取整
    lot = portfolio._lot
    shares = int(raw_shares // lot) * lot

    log.debug(
        f"rules: calc_size exec={exec_price:.2f} sl={sl:.2f} "
        f"risk/sh={risk_per_share:.4f} → "
        f"by_risk={shares_risk:.0f} by_cap={shares_cap:.0f} → {shares}股"
    )
    return shares
