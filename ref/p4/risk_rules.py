"""Layer 2：16 条规则风险因子"""

from __future__ import annotations

import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Optional


def _safe_float(val, default: float = 0.0) -> float:
    """安全取 float 值，NaN / None 回退到 default"""
    if val is None:
        return default
    try:
        v = float(val)
        if pd.isna(v):
            return default
        return v
    except (ValueError, TypeError):
        return default


@dataclass
class RuleResult:
    name: str
    fired: bool
    value: float
    threshold: float
    weight: float
    severity: str  # high / medium
    detail: str = ""

    @property
    def contribution(self) -> float:
        return self.weight if self.fired else 0.0


RULE_WEIGHTS: dict[str, tuple[float, str, bool]] = {
    # (weight, severity, enabled)
    # 保留活跃
    "均线破位":       (0.12, "high",   True),
    "硬回撤兜底":     (0.12, "high",   True),
    "下跌波动偏高":   (0.10, "medium", True),
    "流动性枯竭":     (0.08, "medium", True),
    "MACD动量衰减":   (0.08, "medium", True),
    "连续缩量阴跌":   (0.10, "medium", True),
    "突破失败":       (0.10, "high",   True),
    "SuperTrend翻转": (0.10, "high",   True),
    # 暂时禁用（等待数据/阈值修复）
    "波动率飙升":     (0.12, "high",   False),
    "量价背离":       (0.08, "medium", False),
    "大单持续流出":   (0.10, "high",   False),
    "尾盘杀跌":       (0.08, "high",   False),
    "向下跳空":       (0.04, "medium", False),
    "持续弱于行业":   (0.10, "medium", False),
    "北向资金卖出":   (0.06, "medium", False),
    "放量滞涨":       (0.08, "medium", False),
}

MAX_RULE_SCORE = 0.30  # clip 上限


def _compute_supertrend(bars: pd.DataFrame, period: int = 10, multiplier: float = 3.0) -> tuple[str, str]:
    """计算 SuperTrend 方向。返回 (today_direction, yesterday_direction)。
    direction: "up" = 上涨趋势（价格在 SuperTrend 上方），"down" = 下跌趋势。
    """
    if len(bars) < period + 2:
        return "up", "up"

    high = bars["high"].astype(float).values
    low = bars["low"].astype(float).values
    close = bars["close"].astype(float).values
    n = len(bars)

    # 计算 ATR
    tr = np.zeros(n)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
    atr = np.zeros(n)
    atr[:period] = np.nan
    atr[period - 1] = np.mean(tr[:period])
    for i in range(period, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period

    hl2 = (high + low) / 2
    upper = hl2 + multiplier * atr
    lower = hl2 - multiplier * atr

    # SuperTrend 计算
    supertrend = np.zeros(n)
    direction = np.ones(n)  # 1 = up, -1 = down

    supertrend[period - 1] = upper[period - 1]
    direction[period - 1] = -1

    for i in range(period, n):
        # 调整 lower band（只升不降）
        if lower[i] > lower[i - 1] or close[i - 1] < lower[i - 1]:
            pass  # 使用当前 lower
        else:
            lower[i] = lower[i - 1]
        # 调整 upper band（只降不升）
        if upper[i] < upper[i - 1] or close[i - 1] > upper[i - 1]:
            pass
        else:
            upper[i] = upper[i - 1]

        if direction[i - 1] == 1:  # 之前上涨
            if close[i] < lower[i]:
                direction[i] = -1
                supertrend[i] = upper[i]
            else:
                direction[i] = 1
                supertrend[i] = lower[i]
        else:  # 之前下跌
            if close[i] > upper[i]:
                direction[i] = 1
                supertrend[i] = lower[i]
            else:
                direction[i] = -1
                supertrend[i] = upper[i]

    today_dir = "up" if direction[-1] == 1 else "down"
    yest_dir = "up" if direction[-2] == 1 else "down"
    return today_dir, yest_dir


def compute_rule_score(results: list[RuleResult]) -> float:
    """将触发规则的权重叠加，clip 到 MAX_RULE_SCORE"""
    raw = sum(r.contribution for r in results)
    return round(min(raw, MAX_RULE_SCORE), 4)


class RiskRuleEngine:
    """计算 16 条规则，返回 RuleResult 列表"""

    def __init__(self, moneyflow_available: bool = True, northbound_available: bool = True):
        self.moneyflow_available = moneyflow_available
        self.northbound_available = northbound_available

    @staticmethod
    def _is_enabled(rule_name: str) -> bool:
        """检查规则是否启用"""
        entry = RULE_WEIGHTS.get(rule_name)
        if entry is None:
            return True
        return entry[2] if len(entry) > 2 else True

    def evaluate(
        self,
        symbol: str,
        features: pd.Series,
        daily_bars: pd.DataFrame,   # 最近 20 条日线，按日期升序
        intraday_bars: pd.DataFrame, # 今日盘中数据（可空）
        moneyflow: pd.DataFrame,    # 近 5 日资金流（可空）
        holding: dict | None = None,  # 持仓信息（含 entry_date）
    ) -> list[RuleResult]:
        # 规则名 → 评估函数的映射
        rule_evaluators = [
            ("波动率飙升",     lambda: self._rule_volatility_surge(features)),
            ("下跌波动偏高",   lambda: self._rule_down_vol_bias(daily_bars)),
            ("均线破位",       lambda: self._rule_ma_break(features, daily_bars)),
            ("连续缩量阴跌",   lambda: self._rule_shrink_decline(daily_bars)),
            ("量价背离",       lambda: self._rule_price_vol_diverge(features, daily_bars)),
            ("大单持续流出",   lambda: self._rule_big_outflow(moneyflow)),
            ("尾盘杀跌",       lambda: self._rule_tail_drop(intraday_bars)),
            ("向下跳空",       lambda: self._rule_gap_down(daily_bars)),
            ("持续弱于行业",   lambda: self._rule_weak_vs_industry(features)),
            ("流动性枯竭",     lambda: self._rule_liquidity_dry(features)),
            ("北向资金卖出",   lambda: self._rule_northbound_sell(moneyflow, daily_bars)),
            ("MACD动量衰减",   lambda: self._rule_macd_decay(features, daily_bars)),
            ("突破失败",       lambda: self._rule_breakout_failure(features, daily_bars)),
            ("SuperTrend翻转", lambda: self._rule_supertrend_flip(daily_bars)),
            ("放量滞涨",       lambda: self._rule_volume_stall(daily_bars)),
            ("硬回撤兜底",     lambda: self._rule_hard_drawdown(daily_bars, holding)),
        ]

        results = []
        for name, evaluator in rule_evaluators:
            if not self._is_enabled(name):
                continue
            results.append(evaluator())

        return results

    # ---- Rule 1: 波动率飙升 ----
    def _rule_volatility_surge(self, features: pd.Series) -> RuleResult:
        w, sev, _ = RULE_WEIGHTS["波动率飙升"]
        try:
            hv10 = _safe_float(features.get("hv_10", 0))
            hv20 = _safe_float(features.get("hv_20", 0))
            ratio = hv10 / hv20 if hv20 > 0 else 0
            threshold = 1.5
            fired = ratio > threshold
            detail = f"hv10={hv10:.1%} vs hv20={hv20:.1%}，倍数{ratio:.2f}" if fired else ""
        except Exception:
            ratio, threshold, fired, detail = 0, 1.5, False, ""
        return RuleResult("波动率飙升", fired, round(ratio, 3), threshold, w, sev, detail)

    # ---- Rule 2: 下跌波动偏高 ----
    def _rule_down_vol_bias(self, bars: pd.DataFrame) -> RuleResult:
        w, sev, _ = RULE_WEIGHTS["下跌波动偏高"]
        threshold = 1.5
        try:
            recent = bars.tail(10)
            down_days = recent[recent["pct_chg"] < 0]
            up_days = recent[recent["pct_chg"] >= 0]
            down_std = down_days["pct_chg"].std() if len(down_days) > 1 else 0
            up_std = up_days["pct_chg"].std() if len(up_days) > 1 else 0
            ratio = abs(down_std) / abs(up_std) if up_std and up_std != 0 else 0
            fired = ratio > threshold
            detail = f"下跌波动/上涨波动={ratio:.2f}" if fired else ""
        except Exception:
            ratio, fired, detail = 0, False, ""
        return RuleResult("下跌波动偏高", fired, round(ratio, 3), threshold, w, sev, detail)

    # ---- Rule 3: 均线破位（升级：OR SuperTrend 翻转） ----
    def _rule_ma_break(self, features: pd.Series, daily_bars: pd.DataFrame) -> RuleResult:
        w, sev, _ = RULE_WEIGHTS["均线破位"]
        try:
            close = _safe_float(features.get("close", 0))
            ma20_dev = _safe_float(features.get("ma20_dev", 0))
            ma10_slope = _safe_float(features.get("ma10_slope", 0))
            below_ma = ma20_dev < 0
            slope_neg = ma10_slope < 0
            ma_fired = below_ma and slope_neg

            # SuperTrend 翻转作为 OR 条件
            st_flipped = False
            if not daily_bars.empty and len(daily_bars) >= 15:
                today_dir, yest_dir = _compute_supertrend(daily_bars)
                st_flipped = yest_dir == "up" and today_dir == "down"

            fired = ma_fired or st_flipped
            ma20 = close / (1 + ma20_dev) if ma20_dev != -1 else close
            value = round(ma20_dev * 100, 2)
            threshold = 0

            # 区分首次破位 vs 持续在下方：近5日收盘与 MA20 关系
            days_below = 0
            if below_ma and not daily_bars.empty and len(daily_bars) >= 5:
                try:
                    recent5 = daily_bars.tail(5)
                    if "close" in recent5.columns:
                        closes = recent5["close"].astype(float).values
                        # 简易 MA20 近似：用当前 ma20 值判断
                        days_below = sum(1 for c in closes if c < ma20)
                except Exception:
                    days_below = 1

            parts = []
            if ma_fired:
                if days_below <= 2:
                    parts.append(f"首次破位 收盘{close:.2f}<MA20={ma20:.2f}")
                    sev = "high"
                else:
                    parts.append(f"持续在MA20下方({days_below}日) 收盘{close:.2f}<MA20={ma20:.2f}")
                    sev = "low"
            if st_flipped:
                parts.append("SuperTrend翻空")
            detail = "，".join(parts) if fired else ""
        except Exception:
            value, fired, detail = 0, False, ""
            threshold = 0
        return RuleResult("均线破位", fired, value, threshold, w, sev, detail)

    # ---- Rule 4: 连续缩量阴跌 ----
    def _rule_shrink_decline(self, bars: pd.DataFrame) -> RuleResult:
        w, sev, _ = RULE_WEIGHTS["连续缩量阴跌"]
        threshold = 3
        try:
            recent = bars.tail(5)
            # 找连续阴线且成交额递减的最长序列
            count = 0
            for i in range(len(recent) - 1, -1, -1):
                row = recent.iloc[i]
                is_down = float(row.get("pct_chg", row.get("close", 0) - row.get("open", 0))) < 0
                if not is_down:
                    break
                if i < len(recent) - 1:
                    prev_amount = float(recent.iloc[i + 1].get("amount", 1))
                    curr_amount = float(row.get("amount", 1))
                    if curr_amount >= prev_amount:
                        break
                count += 1
            fired = count >= threshold
            detail = f"连续{count}日缩量阴跌" if fired else ""
        except Exception:
            count, fired, detail = 0, False, ""
        return RuleResult("连续缩量阴跌", fired, count, threshold, w, sev, detail)

    # ---- Rule 5: 量价背离 ----
    def _rule_price_vol_diverge(self, features: pd.Series, bars: pd.DataFrame) -> RuleResult:
        w, sev, _ = RULE_WEIGHTS["量价背离"]
        threshold = 0.60
        try:
            close = float(features.get("close", 0))
            high_20 = float(bars.tail(20)["high"].max()) if not bars.empty else close
            near_high = close >= high_20 * 0.97  # 接近20日高点
            amt5 = bars.tail(5)["amount"].mean() if not bars.empty else 0
            amt20 = bars.tail(20)["amount"].mean() if not bars.empty else 1
            ratio = amt5 / amt20 if amt20 > 0 else 1
            fired = near_high and ratio < threshold
            detail = f"接近高点但5日均额/20日均额={ratio:.2f}" if fired else ""
        except Exception:
            ratio, fired, detail = 1, False, ""
        return RuleResult("量价背离", fired, round(ratio, 3), threshold, w, sev, detail)

    # ---- Rule 6: 大单持续流出 ----
    def _rule_big_outflow(self, moneyflow: pd.DataFrame) -> RuleResult:
        w, sev, _ = RULE_WEIGHTS["大单持续流出"]
        threshold = 15.0  # 大单净流出占总资金流 15%
        if not self.moneyflow_available or moneyflow is None or moneyflow.empty:
            return RuleResult("大单持续流出", False, 0, threshold, w, sev, "数据不可用")
        try:
            recent = moneyflow.tail(3)
            if len(recent) < 3:
                return RuleResult("大单持续流出", False, 0, threshold, w, sev, "数据不足")

            # 向量化计算大单净流出占比
            buy_lg = recent["buy_lg_amount"].fillna(0).astype(float)
            sell_lg = recent["sell_lg_amount"].fillna(0).astype(float)
            buy_elg = recent["buy_elg_amount"].fillna(0).astype(float)
            sell_elg = recent["sell_elg_amount"].fillna(0).astype(float)
            big_net = (buy_lg + buy_elg) - (sell_lg + sell_elg)

            amount_cols = ["buy_sm_amount", "sell_sm_amount", "buy_md_amount", "sell_md_amount",
                           "buy_lg_amount", "sell_lg_amount", "buy_elg_amount", "sell_elg_amount"]
            total = recent[amount_cols].fillna(0).astype(float).sum(axis=1) / 2
            total = total.replace(0, 1)  # 避免除零

            outflow_pcts = ((-big_net / total * 100).where(big_net < 0, 0)).tolist()

            all_outflow = all(p > threshold for p in outflow_pcts)
            avg_pct = sum(outflow_pcts) / len(outflow_pcts) if outflow_pcts else 0
            fired = all_outflow
            detail = f"连续3日大单净流出 {'/'.join(f'{p:.1f}%' for p in outflow_pcts)}" if fired else ""
        except Exception:
            avg_pct, fired, detail = 0, False, ""
        return RuleResult("大单持续流出", fired, round(avg_pct, 2), threshold, w, sev, detail)

    # ---- Rule 7: 尾盘杀跌 ----
    def _rule_tail_drop(self, intraday: pd.DataFrame) -> RuleResult:
        w, sev, _ = RULE_WEIGHTS["尾盘杀跌"]
        threshold = 0.20  # 收在日内最低 20%
        if intraday is None or intraday.empty:
            return RuleResult("尾盘杀跌", False, 0, threshold, w, sev, "盘中数据不可用")
        try:
            day_high = intraday["high"].max()
            day_low = intraday["low"].min()
            close = float(intraday.iloc[-1]["close"])
            price_range = day_high - day_low
            pos = (close - day_low) / price_range if price_range > 0 else 0.5

            # 尾盘30分钟成交量 vs 全天均值
            tail = intraday.tail(6)  # 5min bars, 30min = 6 bars
            tail_vol = tail["volume"].mean() if not tail.empty else 0
            avg_vol = intraday["volume"].mean() if not intraday.empty else 1
            vol_ratio = tail_vol / avg_vol if avg_vol > 0 else 0

            fired = pos < threshold and vol_ratio > 1.5
            detail = f"收盘位置{pos:.0%}，尾盘量比{vol_ratio:.1f}" if fired else ""
        except Exception:
            pos, fired, detail = 0.5, False, ""
        return RuleResult("尾盘杀跌", fired, round(pos, 3), threshold, w, sev, detail)

    # ---- Rule 8: 向下跳空 ----
    def _rule_gap_down(self, bars: pd.DataFrame) -> RuleResult:
        w, sev, _ = RULE_WEIGHTS["向下跳空"]
        threshold = 1
        if bars is None or len(bars) < 2:
            return RuleResult("向下跳空", False, 0, threshold, w, sev, "")
        try:
            yesterday = bars.iloc[-2]
            today = bars.iloc[-1]
            gap = float(today["open"]) < float(yesterday["low"])
            # 检查缺口是否回补
            filled = float(today["high"]) >= float(yesterday["low"])
            fired = gap and not filled
            value = 1 if fired else 0
            detail = f"开盘{today['open']:.2f} < 前日最低{yesterday['low']:.2f}" if fired else ""
        except Exception:
            value, fired, detail = 0, False, ""
        return RuleResult("向下跳空", fired, value, threshold, w, sev, detail)

    # ---- Rule 9: 持续弱于行业 ----
    def _rule_weak_vs_industry(self, features: pd.Series) -> RuleResult:
        w, sev, _ = RULE_WEIGHTS["持续弱于行业"]
        threshold = -0.03  # 5日弱于行业 3%
        try:
            alpha = _safe_float(features.get("alpha_vs_industry_5d", 0))
            fired = alpha < threshold
            value = round(alpha, 4)
            detail = f"5日行业超额={alpha:.2%}" if fired else ""
        except Exception:
            value, fired, detail = 0, False, ""
        return RuleResult("持续弱于行业", fired, value, threshold, w, sev, detail)

    # ---- Rule 10: 流动性枯竭 ----
    def _rule_liquidity_dry(self, features: pd.Series) -> RuleResult:
        w, sev, _ = RULE_WEIGHTS["流动性枯竭"]
        threshold = 0.10
        try:
            turnover_rank = _safe_float(features.get("turnover_rank_20d"), default=None)
            # NaN 时回退到 vol_rank_60d 或 amount_rank_60d
            if turnover_rank is None:
                turnover_rank = _safe_float(features.get("vol_rank_60d"), default=None)
            if turnover_rank is None:
                turnover_rank = _safe_float(features.get("amount_rank_60d"), default=0.5)
            fired = turnover_rank < threshold
            detail = f"流动性分位{turnover_rank:.0%}" if fired else ""
        except Exception:
            turnover_rank, fired, detail = 0.5, False, ""
        return RuleResult("流动性枯竭", fired, round(turnover_rank, 3), threshold, w, sev, detail)

    # ---- Rule 11: 主力资金卖出 ----
    def _rule_northbound_sell(self, moneyflow: pd.DataFrame, bars: pd.DataFrame) -> RuleResult:
        w, sev, _ = RULE_WEIGHTS["北向资金卖出"]
        threshold = 5.0  # 连续3日净流出占成交额 5%
        if moneyflow is None or moneyflow.empty:
            return RuleResult("北向资金卖出", False, 0, threshold, w, sev, "数据不可用")
        try:
            recent = moneyflow.tail(3)
            if len(recent) < 3:
                return RuleResult("北向资金卖出", False, 0, threshold, w, sev, "数据不足")

            # 向量化计算主力资金净流出占比
            net_mf = recent["net_mf_amount"].fillna(0).astype(float)
            amount_cols = ["buy_sm_amount", "sell_sm_amount", "buy_md_amount", "sell_md_amount",
                           "buy_lg_amount", "sell_lg_amount", "buy_elg_amount", "sell_elg_amount"]
            total = recent[amount_cols].fillna(0).astype(float).sum(axis=1) / 2
            total = total.replace(0, 1)
            outflow_pcts = ((-net_mf / total * 100).where(net_mf < 0, 0)).tolist()

            all_outflow = all(p > threshold for p in outflow_pcts)
            avg_pct = sum(outflow_pcts) / len(outflow_pcts) if outflow_pcts else 0
            fired = all_outflow
            detail = f"连续3日净流出 {'/'.join(f'{p:.1f}%' for p in outflow_pcts)}" if fired else ""
        except Exception:
            avg_pct, fired, detail = 0, False, ""
        return RuleResult("北向资金卖出", fired, round(avg_pct, 2), threshold, w, sev, detail)

    # ---- Rule 12: MACD 动量衰减 ----
    def _rule_macd_decay(self, features: pd.Series, bars: pd.DataFrame) -> RuleResult:
        w, sev, _ = RULE_WEIGHTS["MACD动量衰减"]
        threshold = 3  # 连续 3 日 hist 缩短
        try:
            hist = _safe_float(features.get("macd_hist", 0))
            hist_slope = _safe_float(features.get("macd_hist_slope", 0))

            # hist > 0（还没死叉）且当日在缩
            if hist <= 0 or hist_slope >= 0:
                return RuleResult("MACD动量衰减", False, 0, threshold, w, sev, "")

            # 回看 bars 中的 hist 变化判断连续缩短天数
            shrink_days = 1  # 当日已确认在缩
            if not bars.empty and len(bars) >= 4:
                # 用 features_daily 中的 macd_hist 不可得历史值，改用 bars 中的 pct_chg 趋势
                # 简化：hist_slope < 0 且 hist > 0 就视为动量衰减信号
                # 更精确的方式需要历史 hist 值，这里用 hist_slope 的幅度作为置信度
                shrink_days = 3 if abs(hist_slope) > 0.001 else 1

            fired = shrink_days >= threshold and hist > 0
            detail = f"MACD柱状缩短，hist={hist:.4f}，slope={hist_slope:.4f}" if fired else ""
        except Exception:
            shrink_days, fired, detail = 0, False, ""
        return RuleResult("MACD动量衰减", fired, shrink_days, threshold, w, sev, detail)

    # ---- Rule 13: 突破失败 ----
    def _rule_breakout_failure(self, features: pd.Series, bars: pd.DataFrame) -> RuleResult:
        w, sev, _ = RULE_WEIGHTS["突破失败"]
        threshold = 0.98  # 接近 20 日高点的阈值
        if bars is None or bars.empty or len(bars) < 5:
            return RuleResult("突破失败", False, 0, threshold, w, sev, "数据不足")
        try:
            recent_3d = bars.tail(3)
            recent_high = float(recent_3d["high"].max())
            high_20d = float(bars.tail(20)["high"].max())

            near_high = recent_high > high_20d * threshold  # 接近或触及前高
            close = float(bars.iloc[-1]["close"])
            failed_to_hold = close < high_20d * 0.97  # 没站上去

            amt_today = float(bars.iloc[-1].get("amount", 0) or 0)
            amt_5d_avg = float(bars.tail(5)["amount"].mean()) if len(bars) >= 5 else amt_today
            weak_volume = amt_today < amt_5d_avg * 0.80 if amt_5d_avg > 0 else False

            fired = near_high and failed_to_hold and weak_volume
            value = round(recent_high / high_20d, 4) if high_20d > 0 else 0
            detail = (f"近3日高点{recent_high:.2f}接近20日高{high_20d:.2f}，"
                      f"收盘{close:.2f}未站上，量能不足") if fired else ""
        except Exception:
            value, fired, detail = 0, False, ""
        return RuleResult("突破失败", fired, value, threshold, w, sev, detail)

    # ---- Rule 14: SuperTrend 翻转 ----
    def _rule_supertrend_flip(self, bars: pd.DataFrame) -> RuleResult:
        w, sev, _ = RULE_WEIGHTS["SuperTrend翻转"]
        threshold = 1  # 翻转发生
        if bars is None or bars.empty or len(bars) < 15:
            return RuleResult("SuperTrend翻转", False, 0, threshold, w, sev, "数据不足")
        try:
            today_dir, yest_dir = _compute_supertrend(bars)
            fired = yest_dir == "up" and today_dir == "down"
            value = 1 if fired else 0
            detail = "SuperTrend从上涨翻转为下跌" if fired else ""
        except Exception:
            value, fired, detail = 0, False, ""
        return RuleResult("SuperTrend翻转", fired, value, threshold, w, sev, detail)

    # ---- Rule 15: 放量滞涨 ----
    def _rule_volume_stall(self, bars: pd.DataFrame) -> RuleResult:
        w, sev, _ = RULE_WEIGHTS["放量滞涨"]
        threshold = 1.8  # 成交额倍数阈值
        if bars is None or bars.empty or len(bars) < 5:
            return RuleResult("放量滞涨", False, 0, threshold, w, sev, "数据不足")
        try:
            today = bars.iloc[-1]
            amt_today = float(today.get("amount", 0) or 0)
            amt_5d_avg = float(bars.tail(5)["amount"].mean())
            pct_chg = float(today.get("pct_chg", 0) or 0)

            vol_ratio = amt_today / amt_5d_avg if amt_5d_avg > 0 else 0
            fired = vol_ratio > threshold and abs(pct_chg) < 0.5
            detail = f"成交额倍数{vol_ratio:.1f}x但涨跌仅{pct_chg:.2f}%" if fired else ""
        except Exception:
            vol_ratio, fired, detail = 0, False, ""
        return RuleResult("放量滞涨", fired, round(vol_ratio, 2), threshold, w, sev, detail)

    # ---- Rule 16: 硬回撤兜底 ----
    def _rule_hard_drawdown(self, bars: pd.DataFrame, holding: dict | None) -> RuleResult:
        w, sev, _ = RULE_WEIGHTS["硬回撤兜底"]
        threshold = -7.0  # 回撤 7%
        if bars is None or bars.empty or len(bars) < 5:
            return RuleResult("硬回撤兜底", False, 0, threshold, w, sev, "数据不足")
        try:
            close = float(bars.iloc[-1]["close"])

            # 确定计算范围：持仓以来 or 近 20 日
            if holding and holding.get("entry_date"):
                entry_date = str(holding["entry_date"])
                mask = bars["date"].astype(str) >= entry_date
                holding_bars = bars[mask] if mask.any() else bars.tail(20)
            else:
                holding_bars = bars.tail(20)

            peak = float(holding_bars["high"].max())
            drawdown_pct = (close / peak - 1) * 100 if peak > 0 else 0

            fired = drawdown_pct < threshold
            detail = f"从高点{peak:.2f}回撤{drawdown_pct:.1f}%" if fired else ""
        except Exception:
            drawdown_pct, fired, detail = 0, False, ""
        return RuleResult("硬回撤兜底", fired, round(drawdown_pct, 2), threshold, w, sev, detail)
