"""
波段引擎：为自选股提供高抛低吸参考（v2.0 新增）

输出：
  - band_position: 0-100（区间位置）
  - band_level: "oversold" | "low" | "neutral" | "high" | "overbought"
  - valuation_score: 0-100（估值分位）
  - technical_score: 0-100（技术面超买超卖）
  - suggested_action: "低吸加仓" | "持有" | "高抛减仓" | "观望"
  - support_price: 近期支撑价位
  - resistance_price: 近期阻力价位
"""

from dataclasses import dataclass
from typing import Optional

import numpy as np

from utils.logger import logger


@dataclass
class BandResult:
    symbol: str
    name: str
    band_position: int            # 0-100
    band_level: str               # oversold | low | neutral | high | overbought
    valuation_score: int          # 0-100（估值维度，越高越贵）
    technical_score: int          # 0-100（技术面维度，越高越超买）
    suggested_action: str         # 低吸加仓 | 持有 | 高抛减仓 | 观望
    support_price: Optional[float]      # 近期支撑
    resistance_price: Optional[float]   # 近期阻力
    bollinger_upper: Optional[float]    # 布林上轨
    bollinger_lower: Optional[float]    # 布林下轨
    bollinger_mid: Optional[float]      # 布林中轨（MA20）
    pe_percentile: Optional[float]      # PE 3年分位数 (0-1)
    pb_percentile: Optional[float]      # PB 3年分位数 (0-1)
    rsi_14: Optional[float]             # RSI(14)
    details: list                       # 文字说明列表


class BandScorer:
    """波段评分器"""

    def __init__(self, duckdb_reader, settings: dict = None):
        self.db = duckdb_reader
        self.settings = settings or {}

    def score(self, symbol: str, name: str, hold_type: str = "swing") -> BandResult:
        """计算单只股票的区间位置"""
        # 1. 获取数据
        features = self._get_latest_features(symbol)
        daily_bars = self._get_daily_bars(symbol, days=250)
        fundamentals = self._get_fundamentals_history(symbol, days=750)

        # 2. 估值维度（权重 40%）
        valuation_score = self._calc_valuation_score(fundamentals, symbol)

        # 3. 技术面维度（权重 60%）
        technical_score, bollinger, rsi_14, support, resistance = \
            self._calc_technical_score(features, daily_bars)

        # 4. 合成区间位置
        v_weight = self.settings.get("valuation_weight", 0.40)
        t_weight = self.settings.get("technical_weight", 0.60)
        band_position = int(valuation_score * v_weight + technical_score * t_weight)
        band_position = max(0, min(100, band_position))

        # 5. 分级
        band_level = self._classify_band(band_position)

        # 6. 操作建议（结合 hold_type）
        suggested_action = self._suggest_action(band_position, band_level, hold_type)

        # 7. 生成文字说明
        details = self._generate_details(
            valuation_score, technical_score, band_position,
            bollinger, rsi_14, fundamentals
        )

        return BandResult(
            symbol=symbol,
            name=name,
            band_position=band_position,
            band_level=band_level,
            valuation_score=valuation_score,
            technical_score=technical_score,
            suggested_action=suggested_action,
            support_price=support,
            resistance_price=resistance,
            bollinger_upper=bollinger.get("upper") if bollinger else None,
            bollinger_lower=bollinger.get("lower") if bollinger else None,
            bollinger_mid=bollinger.get("mid") if bollinger else None,
            pe_percentile=fundamentals.get("pe_percentile") if fundamentals else None,
            pb_percentile=fundamentals.get("pb_percentile") if fundamentals else None,
            rsi_14=rsi_14,
            details=details,
        )

    def _calc_valuation_score(self, fundamentals: dict, symbol: str) -> int:
        """
        估值分位 → 0-100 分

        ETF（如 512400）跳过估值，返回 50（中性）
        """
        if symbol.startswith("51") or symbol.startswith("15"):
            return 50

        if not fundamentals or fundamentals.get("pe_percentile") is None:
            return 50

        pe_pct = fundamentals["pe_percentile"]  # 0-1
        return int(pe_pct * 90 + 5)

    def _calc_technical_score(self, features: dict, daily_bars: list) -> tuple:
        """
        技术面 → 0-100 分

        三个子指标等权：
        1. 布林带位置（当前价在布林通道中的百分比位置）
        2. RSI(14)（直接作为 0-100 分数）
        3. MA20 偏离度（正偏离越大越超买）

        返回：(technical_score, bollinger_dict, rsi_14, support, resistance)
        """
        if not features or not daily_bars or len(daily_bars) < 20:
            return 50, None, None, None, None

        closes = [bar["close"] for bar in daily_bars]
        current = closes[-1]

        # 布林带（20日，2倍标准差）
        boll_period = self.settings.get("bollinger_period", 20)
        boll_std = self.settings.get("bollinger_std", 2.0)
        ma20 = float(np.mean(closes[-boll_period:]))
        std20 = float(np.std(closes[-boll_period:]))
        upper = ma20 + boll_std * std20
        lower = ma20 - boll_std * std20
        bollinger = {"upper": round(upper, 2), "lower": round(lower, 2), "mid": round(ma20, 2)}

        # 布林位置：0%=下轨，100%=上轨
        if upper > lower:
            boll_position = (current - lower) / (upper - lower) * 100
        else:
            boll_position = 50
        boll_position = max(0, min(100, boll_position))

        # RSI(14)
        rsi_14 = features.get("f_rsi_14", features.get("rsi_14", 50))
        if rsi_14 is None:
            rsi_14 = 50
        rsi_14 = float(rsi_14)

        # MA20 偏离度 → 映射到 0-100
        ma20_dev = features.get("f_ma20_dev", features.get("ma20_dev", 0))
        if ma20_dev is None:
            ma20_dev = 0
        ma20_dev = float(ma20_dev)
        # 偏离度 -10% → 分数 0，偏离度 +10% → 分数 100
        ma_score = (ma20_dev + 0.10) / 0.20 * 100
        ma_score = max(0, min(100, ma_score))

        technical_score = int((boll_position + rsi_14 + ma_score) / 3)

        # 支撑/阻力（近 20 日低点/高点）
        support = round(min(closes[-20:]), 2) if len(closes) >= 20 else None
        resistance = round(max(closes[-20:]), 2) if len(closes) >= 20 else None

        return technical_score, bollinger, rsi_14, support, resistance

    def _classify_band(self, position: int) -> str:
        if position <= 15:
            return "oversold"
        elif position <= 35:
            return "low"
        elif position <= 65:
            return "neutral"
        elif position <= 85:
            return "high"
        else:
            return "overbought"

    def _suggest_action(self, position: int, level: str, hold_type: str) -> str:
        if hold_type == "watch":
            if level in ("oversold", "low"):
                return "进入关注买入区"
            elif level in ("high", "overbought"):
                return "暂不建议介入"
            return "观望"

        if hold_type == "long_term":
            if level == "oversold":
                return "低吸加仓"
            elif level == "low":
                return "可小幅加仓"
            elif level == "high":
                return "可高抛部分仓位"
            elif level == "overbought":
                return "建议高抛减仓"
            return "持有"

        # swing
        if level == "oversold":
            return "低吸加仓"
        elif level == "low":
            return "可小幅加仓"
        elif level == "high":
            return "高抛减仓"
        elif level == "overbought":
            return "建议大幅减仓"
        return "持有"

    def _generate_details(self, v_score, t_score, position,
                          bollinger, rsi, fundamentals) -> list:
        details = []
        if fundamentals and fundamentals.get("pe_percentile") is not None:
            pct = fundamentals["pe_percentile"]
            label = "（低估区）" if pct < 0.2 else "（高估区）" if pct > 0.8 else ""
            details.append(f"PE 3年分位 {pct:.0%}{label}")
        if rsi is not None:
            if rsi < 30:
                details.append(f"RSI(14)={rsi:.0f}，超卖区间")
            elif rsi > 70:
                details.append(f"RSI(14)={rsi:.0f}，超买区间")
            else:
                details.append(f"RSI(14)={rsi:.0f}")
        if bollinger:
            details.append(f"布林带 {bollinger['lower']:.2f} ~ {bollinger['upper']:.2f}")
        return details

    # --- 数据查询方法（复用 DuckDBReader 已有方法 + _get_conn）---

    def _exec(self, sql: str, params: list):
        """通过 DuckDBReader 的已有连接执行查询，避免锁冲突"""
        conn = self.db._get_conn()
        return conn.execute(sql, params)

    def _get_latest_features(self, symbol: str) -> dict:
        """从 features_daily 取最新一行"""
        try:
            df = self._exec(
                "SELECT * FROM features_daily WHERE symbol = ? ORDER BY trade_date DESC LIMIT 1",
                [symbol],
            ).df()
            if df.empty:
                return {}
            return df.iloc[0].to_dict()
        except Exception as e:
            logger.warning(f"BandScorer 获取 features 失败 ({symbol}): {e}")
            return {}

    def _get_daily_bars(self, symbol: str, days: int = 250) -> list:
        """从 market_bars_daily 取近 N 日日线"""
        try:
            df = self._exec(
                """SELECT trade_date, open, high, low, close, volume
                   FROM market_bars_daily
                   WHERE symbol = ?
                   ORDER BY trade_date DESC
                   LIMIT ?""",
                [symbol, days],
            ).df()
            if df.empty:
                return []
            df = df.sort_values("trade_date")
            return df.to_dict("records")
        except Exception as e:
            logger.warning(f"BandScorer 获取 daily_bars 失败 ({symbol}): {e}")
            return []

    def _get_fundamentals_history(self, symbol: str, days: int = 750) -> dict:
        """从 fundamentals_daily 取 3 年 PE/PB 数据，计算分位数"""
        try:
            df = self._exec(
                """SELECT pe_ttm, pb FROM fundamentals_daily
                   WHERE symbol = ? AND pe_ttm IS NOT NULL AND pe_ttm > 0
                   ORDER BY trade_date DESC
                   LIMIT ?""",
                [symbol, days],
            ).df()
            if df.empty or len(df) < 20:
                return {}

            pe_values = [v for v in df["pe_ttm"].tolist() if v and v > 0]
            pb_values = [v for v in df["pb"].tolist() if v and v > 0]
            current_pe = pe_values[0] if pe_values else None
            current_pb = pb_values[0] if pb_values else None

            pe_percentile = None
            pb_percentile = None
            if current_pe and len(pe_values) >= 20:
                pe_percentile = sum(1 for v in pe_values if v <= current_pe) / len(pe_values)
            if current_pb and len(pb_values) >= 20:
                pb_percentile = sum(1 for v in pb_values if v <= current_pb) / len(pb_values)

            return {
                "pe_ttm": current_pe,
                "pb": current_pb,
                "pe_percentile": pe_percentile,
                "pb_percentile": pb_percentile,
            }
        except Exception as e:
            logger.warning(f"BandScorer 获取 fundamentals 失败 ({symbol}): {e}")
            return {}
