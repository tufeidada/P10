"""大盘实时健康度 Market Pulse

评分维度（满分 100）：
  日内涨跌幅(30) + 近1小时趋势(30) + 日内波动率(20) + 距日内低点(20)

level 分级：healthy(>75) / neutral(50-75) / weak(25-50) / danger(<25)
"""

from __future__ import annotations

import pandas as pd
from utils.logger import logger


# 沪深300 代码
INDEX_SYMBOL = "000300"

_LEVEL_THRESHOLDS = [
    (75, "healthy"),
    (50, "neutral"),
    (25, "weak"),
    (0, "danger"),
]


class MarketPulse:
    """从盘中 K 线计算大盘健康度"""

    def __init__(self, market_feed=None):
        self._feed = market_feed
        self._last_score: int = 50
        self._last_level: str = "neutral"

    def compute(self, bars_5m: pd.DataFrame | None = None) -> dict:
        """计算大盘健康度。

        Args:
            bars_5m: 5 分钟 K 线 DataFrame，需有 open/high/low/close/volume 列。
                     如果不传则尝试从 market_feed 获取。

        Returns:
            {"score": 0-100, "level": str, "details": {...}}
        """
        if bars_5m is None and self._feed:
            bars_map = self._feed.get_realtime_bars([INDEX_SYMBOL], freq=0)  # 5min
            bars_5m = bars_map.get(INDEX_SYMBOL, pd.DataFrame())

        if bars_5m is None or bars_5m.empty or len(bars_5m) < 2:
            return {
                "score": self._last_score,
                "level": self._last_level,
                "details": {"error": "无数据"},
            }

        try:
            bars = bars_5m.copy()
            for col in ["open", "high", "low", "close", "volume"]:
                bars[col] = pd.to_numeric(bars[col], errors="coerce")

            day_open = float(bars.iloc[0]["open"])
            current = float(bars.iloc[-1]["close"])
            day_high = float(bars["high"].max())
            day_low = float(bars["low"].min())

            # 维度 1：日内涨跌幅 (满分 30)
            day_chg = (current / day_open - 1) * 100 if day_open > 0 else 0
            # -2% → 0分, 0% → 15分, +2% → 30分
            s1 = max(0, min(30, int(15 + day_chg * 7.5)))

            # 维度 2：近 1 小时趋势 (满分 30)
            recent = bars.tail(12)  # 12 根 5min = 60min
            if len(recent) >= 2:
                hour_start = float(recent.iloc[0]["open"])
                hour_chg = (current / hour_start - 1) * 100 if hour_start > 0 else 0
                s2 = max(0, min(30, int(15 + hour_chg * 10)))
            else:
                s2 = 15

            # 维度 3：日内波动率 (满分 20，波动越小越好)
            intraday_range = (day_high - day_low) / day_open * 100 if day_open > 0 else 0
            # 0% → 20分, 1% → 15分, 2% → 10分, 3%+ → 0分
            s3 = max(0, min(20, int(20 - intraday_range * 6.7)))

            # 维度 4：距日内低点 (满分 20)
            if day_high > day_low:
                position = (current - day_low) / (day_high - day_low)
                s4 = int(position * 20)
            else:
                s4 = 10

            total = min(s1 + s2 + s3 + s4, 100)

            level = "danger"
            for threshold, lv in _LEVEL_THRESHOLDS:
                if total >= threshold:
                    level = lv
                    break

            self._last_score = total
            self._last_level = level

            return {
                "score": total,
                "level": level,
                "details": {
                    "day_chg_pct": round(day_chg, 2),
                    "hour_chg_pct": round(hour_chg if len(recent) >= 2 else 0, 2),
                    "intraday_range_pct": round(intraday_range, 2),
                    "day_position": round(position if day_high > day_low else 0.5, 2),
                    "sub_scores": {"day_chg": s1, "hour_trend": s2, "volatility": s3, "position": s4},
                },
            }
        except Exception as e:
            logger.warning(f"[MarketPulse] 计算异常: {e}")
            return {
                "score": self._last_score,
                "level": self._last_level,
                "details": {"error": str(e)},
            }

    @property
    def score(self) -> int:
        return self._last_score

    @property
    def level(self) -> str:
        return self._last_level

    def get_sensitivity_factor(self) -> float:
        """返回盘中告警灵敏度因子。

        danger → 0.8（阈值收紧 20%，更容易触发）
        healthy → 1.2（阈值放宽 20%，更不容易触发）
        """
        if self._last_level == "danger":
            return 0.8
        elif self._last_level == "weak":
            return 0.9
        elif self._last_level == "healthy":
            return 1.2
        return 1.0
