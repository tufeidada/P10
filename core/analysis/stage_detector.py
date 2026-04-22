"""
Weinstein Stage 检测 + O'Neil RS Rank 计算

Weinstein 四阶段模型:
  - Stage 1 (底部积累): MA150走平, 股价在MA150附近波动
  - Stage 2 (上升阶段): MA150上升, 股价在MA150上方
  - Stage 3 (顶部派发): MA150走平但股价仍在高位
  - Stage 4 (下降阶段): MA150下降, 股价在MA150下方

RS Rank (相对强度排名):
  - O'Neil风格, 计算个股区间涨幅在全市场的百分位排名 0-100
"""

from __future__ import annotations

from datetime import date
from typing import Any

import numpy as np
import structlog

from db.connection import db_execute, db_query, db_query_one

logger = structlog.get_logger(__name__)


class StageDetector:
    """Weinstein Stage 检测与 RS Rank 计算。"""

    async def detect_stage(
        self,
        symbol: str,
        trade_date: date | None = None,
    ) -> int:
        """检测 Weinstein Stage (1-4)。

        使用 150 日均线（约等于 30 周均线）及其斜率判定当前阶段。

        Args:
            symbol: 股票代码。
            trade_date: 分析日期，默认为最新交易日。

        Returns:
            Stage 编号 1-4。

        Logic:
            - 加载最近 ~200 个交易日的日线数据
            - 计算 MA150 及最近 20 日的斜率
            - price_vs_ma = close / ma150 - 1
            - Stage 4: slope < -0.02 且 price_vs_ma < -0.05
            - Stage 1: slope < 0.005 且 |price_vs_ma| < 0.05 且 price > ma（否则归为 Stage 4）
            - Stage 2: slope > 0.005 且 price > ma
            - Stage 3: slope > -0.005 且 price_vs_ma > 0.05（股价远高于走平的均线）
            - 默认: Stage 1
        """
        bars = await self._load_bars(symbol, trade_date, lookback=200)
        if len(bars) < 150:
            logger.warning(
                "insufficient_bars_for_stage",
                symbol=symbol,
                trade_date=trade_date,
                bar_count=len(bars),
            )
            return 1  # 数据不足默认 Stage 1

        closes = np.array([float(b["close"]) for b in bars], dtype=np.float64)

        # MA150
        ma150 = np.mean(closes[-150:])
        if ma150 <= 0:
            return 1

        # MA150 斜率: 用最近 20 天的 MA150 值做线性回归斜率（归一化）
        if len(closes) >= 170:
            ma150_series = np.array([
                np.mean(closes[i - 150 : i])
                for i in range(len(closes) - 20, len(closes) + 1)
            ])
        else:
            # 数据不足 170 根，用可用范围计算
            n_points = min(21, len(closes) - 149)
            ma150_series = np.array([
                np.mean(closes[i - 150 : i])
                for i in range(len(closes) - n_points + 1, len(closes) + 1)
            ])

        # 归一化斜率 = 线性回归斜率 / MA150 均值
        x = np.arange(len(ma150_series), dtype=np.float64)
        if len(x) >= 2:
            slope_raw = np.polyfit(x, ma150_series, 1)[0]
            slope = slope_raw / np.mean(ma150_series)
        else:
            slope = 0.0

        current_close = closes[-1]
        price_vs_ma = current_close / ma150 - 1.0

        # Stage 判定
        stage = self._classify_stage(slope, price_vs_ma, current_close, ma150)

        logger.info(
            "stage_detected",
            symbol=symbol,
            trade_date=str(trade_date),
            stage=stage,
            slope=round(slope, 5),
            price_vs_ma=round(price_vs_ma, 4),
        )
        return stage

    @staticmethod
    def _classify_stage(
        slope: float,
        price_vs_ma: float,
        close: float,
        ma150: float,
    ) -> int:
        """根据斜率和价格位置判定 Weinstein Stage。

        Args:
            slope: MA150 归一化斜率。
            price_vs_ma: (close / ma150) - 1。
            close: 当前收盘价。
            ma150: 150日均线值。

        Returns:
            Stage 1-4。
        """
        price_above_ma = close > ma150

        # Stage 4: 明确下降趋势
        if slope < -0.02 and price_vs_ma < -0.05:
            return 4

        # Stage 2: 明确上升趋势
        if slope > 0.005 and price_above_ma:
            return 2

        # Stage 3: 顶部派发（均线走平但价格仍远高于均线）
        if slope > -0.005 and price_vs_ma > 0.05:
            return 3

        # Stage 1: 底部积累（均线走平，价格在均线附近）
        if slope < 0.005 and abs(price_vs_ma) < 0.05:
            if price_above_ma:
                return 1
            else:
                return 4  # 均线走平但价格在下方，偏向 Stage 4

        # 默认
        return 1

    async def calc_rs_rank(
        self,
        symbol: str,
        trade_date: date | None = None,
        period: int = 63,
    ) -> float:
        """计算 O'Neil RS Rating (0-100)。

        用单条 SQL 计算目标股票在全市场同期涨幅中的百分位排名。

        Args:
            symbol: 股票代码。
            trade_date: 分析日期，默认取该股票最新交易日。
            period: 回溯交易日数（默认 63 ≈ 3 个月）。

        Returns:
            RS 排名 0-100，100 表示最强。如果数据不足返回 50.0。
        """
        if trade_date is None:
            trade_date = await self._latest_trade_date(symbol)
            if trade_date is None:
                logger.warning("no_data_for_rs_rank", symbol=symbol)
                return 50.0

        # 使用 PERCENT_RANK 窗口函数一次性计算所有股票的区间收益排名
        sql = """
        WITH end_prices AS (
            SELECT symbol, close
            FROM market_bars_daily
            WHERE trade_date = $1
        ),
        start_prices AS (
            SELECT DISTINCT ON (symbol) symbol, close
            FROM market_bars_daily
            WHERE trade_date <= $1
              AND trade_date >= $1 - INTERVAL '%s days'
            ORDER BY symbol, trade_date ASC
        ),
        returns AS (
            SELECT
                e.symbol,
                (e.close / NULLIF(s.close, 0) - 1) AS ret
            FROM end_prices e
            JOIN start_prices s ON e.symbol = s.symbol
            WHERE s.close > 0
        ),
        ranked AS (
            SELECT
                symbol,
                ret,
                PERCENT_RANK() OVER (ORDER BY ret) * 100 AS pct_rank
            FROM returns
        )
        SELECT pct_rank
        FROM ranked
        WHERE symbol = $2
        """ % (period * 2)  # 用 period*2 日历天数确保覆盖足够交易日

        # 注意: DISTINCT ON 配合 ORDER BY symbol, trade_date ASC 会取每个 symbol 最早一天
        # 这里需要更精确的方法: 取距离 trade_date 往前第 period 个交易日
        # 改用子查询精确匹配

        sql_precise = """
        WITH target_dates AS (
            SELECT DISTINCT trade_date
            FROM market_bars_daily
            WHERE trade_date <= $1
            ORDER BY trade_date DESC
            LIMIT $2
        ),
        start_date AS (
            SELECT MIN(trade_date) AS dt FROM target_dates
        ),
        end_prices AS (
            SELECT symbol, close
            FROM market_bars_daily
            WHERE trade_date = $1
        ),
        start_prices AS (
            SELECT symbol, close
            FROM market_bars_daily
            WHERE trade_date = (SELECT dt FROM start_date)
        ),
        returns AS (
            SELECT
                e.symbol,
                (e.close / NULLIF(s.close, 0) - 1) AS ret
            FROM end_prices e
            JOIN start_prices s ON e.symbol = s.symbol
            WHERE s.close > 0
        ),
        ranked AS (
            SELECT
                symbol,
                PERCENT_RANK() OVER (ORDER BY ret) * 100 AS pct_rank
            FROM returns
        )
        SELECT pct_rank
        FROM ranked
        WHERE symbol = $3
        """

        row = await db_query_one(sql_precise, trade_date, period + 1, symbol)
        if row is None:
            logger.warning(
                "rs_rank_no_data",
                symbol=symbol,
                trade_date=str(trade_date),
                period=period,
            )
            return 50.0

        rs_rank = float(row["pct_rank"])
        logger.info(
            "rs_rank_calculated",
            symbol=symbol,
            trade_date=str(trade_date),
            rs_rank=round(rs_rank, 2),
        )
        return round(rs_rank, 2)

    async def update_features(
        self,
        symbol: str,
        trade_date: date,
    ) -> None:
        """将 stage 和 rs_rank 写回 features_daily 表。

        如果 features_daily 中已有该行则 UPDATE，否则 INSERT。

        Args:
            symbol: 股票代码。
            trade_date: 交易日期。
        """
        stage = await self.detect_stage(symbol, trade_date)
        rs_rank = await self.calc_rs_rank(symbol, trade_date)

        sql = """
        INSERT INTO features_daily (symbol, trade_date, stage, rs_rank)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (symbol, trade_date)
        DO UPDATE SET stage = $3, rs_rank = $4
        """
        await db_execute(sql, symbol, trade_date, stage, rs_rank)

        logger.info(
            "features_updated",
            symbol=symbol,
            trade_date=str(trade_date),
            stage=stage,
            rs_rank=rs_rank,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _load_bars(
        self,
        symbol: str,
        trade_date: date | None,
        lookback: int = 200,
    ) -> list[dict[str, Any]]:
        """从 market_bars_daily 加载日线数据。

        Args:
            symbol: 股票代码。
            trade_date: 截止日期，None 则取最新。
            lookback: 需要的交易日数。

        Returns:
            按 trade_date 升序排列的 bar 列表。
        """
        if trade_date is None:
            sql = """
            SELECT trade_date, open, high, low, close, volume, amount
            FROM market_bars_daily
            WHERE symbol = $1
            ORDER BY trade_date DESC
            LIMIT $2
            """
            rows = await db_query(sql, symbol, lookback)
        else:
            sql = """
            SELECT trade_date, open, high, low, close, volume, amount
            FROM market_bars_daily
            WHERE symbol = $1 AND trade_date <= $2
            ORDER BY trade_date DESC
            LIMIT $3
            """
            rows = await db_query(sql, symbol, trade_date, lookback)

        # 转为 dict 列表，按日期升序
        bars = [dict(r) for r in reversed(rows)]
        return bars

    async def _latest_trade_date(self, symbol: str) -> date | None:
        """获取某只股票最新交易日期。

        Args:
            symbol: 股票代码。

        Returns:
            最新交易日或 None。
        """
        sql = """
        SELECT MAX(trade_date) FROM market_bars_daily WHERE symbol = $1
        """
        val = await db_query_one(sql, symbol)
        if val is None:
            return None
        return val["max"]
