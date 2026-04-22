"""
PIT（Point-in-Time）数据加载器。

所有回测数据访问的唯一入口。严格保证 look-ahead bias free。

PIT 规则：
  - T 日判断只能用 T-1 及之前的数据（get_bars/get_features 默认截至 prev_trade_date）
  - 财报过滤用 available_date（= announce_date，NULL 时 = report_date + 45天）
  - 资金流 available_date = 下一个交易日（T+1 才可查）
  - get_open_price() 是唯一允许访问"未来"数据的方法，只能在交易执行环节调用

字段映射（内部 P10 字段 → 业务层 Spec 字段）：
  - market_bars_daily.turnover → turnover_rate（COALESCE 兼容新旧列）
  - features_daily.rs_rank    → rs_rank_63d
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from typing import Any, Optional

import asyncpg
import pandas as pd
import structlog

logger = structlog.get_logger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# 连接池工厂
# ─────────────────────────────────────────────────────────────────────────────

async def create_pool(
    dsn: str | None = None,
    min_size: int = 2,
    max_size: int = 10,
) -> asyncpg.Pool:
    """创建独立的 asyncpg 连接池（不共享 P10 主项目的连接池）。

    优先使用传入的 dsn，否则读取环境变量 DATABASE_URL。
    """
    dsn = dsn or os.environ["DATABASE_URL"]
    pool = await asyncpg.create_pool(
        dsn,
        min_size=min_size,
        max_size=max_size,
        command_timeout=60,
        server_settings={"timezone": "Asia/Shanghai"},
    )
    logger.info("pit_loader_pool_created", min_size=min_size, max_size=max_size)
    return pool


# ─────────────────────────────────────────────────────────────────────────────
# PITDataLoader
# ─────────────────────────────────────────────────────────────────────────────

class PITDataLoader:
    """Point-in-Time 数据加载器。

    所有数据查询必须走这个接口，严格保证 look-ahead bias free。

    Args:
        pool: asyncpg 连接池（由调用方创建，可注入 mock 或测试池）。
        schema: 表所在的 PostgreSQL schema（生产用 'public'，测试用隔离 schema）。
    """

    def __init__(self, pool: asyncpg.Pool, schema: str = "public") -> None:
        self._pool = pool
        self._schema = schema
        self._current_date: date | None = None

    # ── 时间控制 ────────────────────────────────────────────────────────────

    def set_date(self, current_date: date) -> None:
        """回测引擎推进时间轴时调用，设置当前"模拟时间"。"""
        self._current_date = current_date
        logger.debug("pit_date_set", date=str(current_date))

    def _assert_date(self) -> date:
        if self._current_date is None:
            raise RuntimeError("call set_date() before querying data")
        return self._current_date

    async def _prev_trade_date(self) -> date:
        """从 trade_calendar 获取当前日期的前一个交易日。

        兼容 P10 实际 trade_calendar 结构（只有 trade_date 一列）。
        若表不存在或无记录，退回到日历日 - 1。
        """
        current = self._assert_date()
        sql = f"""
            SELECT MAX(trade_date)
            FROM {self._schema}.trade_calendar
            WHERE trade_date < $1
        """
        async with self._pool.acquire() as conn:
            try:
                val = await conn.fetchval(sql, current)
            except (asyncpg.UndefinedTableError, asyncpg.UndefinedColumnError):
                return current - timedelta(days=1)
        if val:
            return val
        return current - timedelta(days=1)

    # ── 行情数据 ────────────────────────────────────────────────────────────

    async def get_bars(
        self,
        symbol: str,
        lookback_days: int = 250,
        include_today: bool = False,
    ) -> pd.DataFrame:
        """获取日线 OHLCV 数据。

        默认截至 prev_trade_date（T-1），保证 T 日决策不看当日行情。
        设 include_today=True 仅用于收盘后批处理场景，禁止在生成判断时使用。

        PIT 过滤: available_date <= cutoff AND trade_date <= cutoff

        Returns:
            按 trade_date 升序排列的 DataFrame，列名遵循 Spec（turnover_rate）。
        """
        current = self._assert_date()
        cutoff = current if include_today else await self._prev_trade_date()
        sql = f"""
            SELECT
                symbol,
                market,
                trade_date,
                open,
                high,
                low,
                close,
                volume,
                amount,
                adj_factor,
                adj_close,
                COALESCE(turnover_rate, turnover) AS turnover_rate,
                available_date
            FROM {self._schema}.market_bars_daily
            WHERE symbol = $1
              AND available_date <= $2
              AND trade_date <= $2
            ORDER BY trade_date DESC
            LIMIT $3
        """
        rows = await self._fetch(sql, symbol, cutoff, lookback_days)
        df = _to_df(rows)
        return df.sort_values("trade_date").reset_index(drop=True) if not df.empty else df

    async def get_features(
        self,
        symbol: str,
        lookback_days: int = 250,
    ) -> pd.DataFrame:
        """获取预计算的技术特征（ma/rsi/stage/rs_rank 等）。

        不返回 future_ret_* 字段（它们在 backtest_features_extra 表，
        禁止在判断生成阶段访问）。

        PIT 过滤: available_date <= prev_trade_date

        Returns:
            按 trade_date 升序排列的 DataFrame，rs_rank 以 rs_rank_63d 暴露。
        """
        cutoff = await self._prev_trade_date()
        sql = f"""
            SELECT
                symbol,
                trade_date,
                ma5, ma10, ma20, ma60, ma150, ma200,
                ma5_slope,
                ma20_slope,
                ma60_slope,
                rsi_14,
                macd_dif, macd_dea, macd_hist,
                adx_14, plus_di, minus_di,
                atr_14, hv_20,
                boll_upper, boll_lower, boll_width,
                ret_1d, ret_5d, ret_20d, ret_60d,
                dist_20d_high, dist_60d_high, pct_in_20d_range,
                vol_ratio_5d,
                turnover_rank_20d,
                stage,
                rs_rank AS rs_rank_63d,
                available_date
            FROM {self._schema}.features_daily
            WHERE symbol = $1
              AND available_date <= $2
              AND trade_date <= $2
            ORDER BY trade_date DESC
            LIMIT $3
        """
        rows = await self._fetch(sql, symbol, cutoff, lookback_days)
        df = _to_df(rows)
        return df.sort_values("trade_date").reset_index(drop=True) if not df.empty else df

    # ── 基本面数据 ──────────────────────────────────────────────────────────

    async def get_fundamentals(self, symbol: str) -> Optional[dict[str, Any]]:
        """获取最新可用的每日估值指标（PE/PB/市值等）。

        PIT 过滤: available_date <= current_date

        Returns:
            单行 dict，或 None（无数据）。
        """
        current = self._assert_date()
        sql = f"""
            SELECT
                symbol,
                trade_date,
                pe_ttm,
                pb,
                ps_ttm,
                total_mv,
                circ_mv,
                turnover_rate_f,
                available_date
            FROM {self._schema}.fundamentals_daily
            WHERE symbol = $1
              AND available_date <= $2
            ORDER BY trade_date DESC
            LIMIT 1
        """
        rows = await self._fetch(sql, symbol, current)
        return dict(rows[0]) if rows else None

    async def get_latest_financials(
        self, symbol: str, n_quarters: int = 12
    ) -> pd.DataFrame:
        """获取最新可用的季度财报（最多 n_quarters 个季度）。

        ⚠️ 关键 PIT：过滤用 available_date（= announce_date），
        而非 report_date。报告期 2024-12-31 的财报若 2025-03-15 才公告，
        在 2025-03-15 之前不可见。

        Returns:
            按 report_date 降序排列的 DataFrame。
        """
        current = self._assert_date()
        sql = f"""
            SELECT
                symbol,
                report_date,
                announce_date,
                revenue, revenue_yoy, revenue_qoq,
                net_profit, np_yoy,
                gross_margin, net_margin,
                total_assets, total_liab,
                debt_ratio, current_ratio,
                goodwill,
                ocf, ocf_to_np,
                roe_ttm, roa_ttm,
                dupont_npm, dupont_tat, dupont_em,
                available_date
            FROM {self._schema}.financials_quarterly
            WHERE symbol = $1
              AND available_date <= $2
            ORDER BY report_date DESC
            LIMIT $3
        """
        rows = await self._fetch(sql, symbol, current, n_quarters)
        return _to_df(rows)

    # ── 资金面数据 ──────────────────────────────────────────────────────────

    async def get_moneyflow(
        self, symbol: str, lookback_days: int = 20
    ) -> pd.DataFrame:
        """获取个股资金流数据。

        PIT 过滤: available_date <= current_date
        （资金流 available_date = 下一个交易日，即 T 日资金流 T+1 才可查）

        Returns:
            按 trade_date 升序排列的 DataFrame。
        """
        current = self._assert_date()
        sql = f"""
            SELECT
                symbol,
                trade_date,
                net_lg_amount,
                net_md_amount,
                net_sm_amount,
                available_date
            FROM {self._schema}.moneyflow_daily
            WHERE symbol = $1
              AND available_date <= $2
            ORDER BY trade_date DESC
            LIMIT $3
        """
        rows = await self._fetch(sql, symbol, current, lookback_days)
        df = _to_df(rows)
        return df.sort_values("trade_date").reset_index(drop=True) if not df.empty else df

    async def get_northbound(self, lookback_days: int = 20) -> pd.DataFrame:
        """获取北向资金净买入数据（市场级别）。

        PIT 过滤: available_date <= current_date

        Returns:
            按 trade_date 升序排列的 DataFrame。
        """
        current = self._assert_date()
        sql = f"""
            SELECT
                trade_date,
                sh_net_buy,
                sz_net_buy,
                total_net_buy,
                available_date
            FROM {self._schema}.northbound_daily
            WHERE available_date <= $1
            ORDER BY trade_date DESC
            LIMIT $2
        """
        rows = await self._fetch(sql, current, lookback_days)
        df = _to_df(rows)
        return df.sort_values("trade_date").reset_index(drop=True) if not df.empty else df

    async def get_margin(
        self, symbol: str, lookback_days: int = 20
    ) -> pd.DataFrame:
        """获取融资融券数据。

        PIT 过滤: available_date <= current_date

        Returns:
            按 trade_date 升序排列的 DataFrame。
        """
        current = self._assert_date()
        sql = f"""
            SELECT
                symbol,
                trade_date,
                rzye,
                rzmre,
                available_date
            FROM {self._schema}.margin_daily
            WHERE symbol = $1
              AND available_date <= $2
            ORDER BY trade_date DESC
            LIMIT $3
        """
        rows = await self._fetch(sql, symbol, current, lookback_days)
        df = _to_df(rows)
        return df.sort_values("trade_date").reset_index(drop=True) if not df.empty else df

    # ── 市场宏观数据 ────────────────────────────────────────────────────────

    async def get_index(
        self, index_code: str, lookback_days: int = 250
    ) -> pd.DataFrame:
        """获取指数日线数据（用于 regime 判断和对照组）。

        PIT 过滤: trade_date <= prev_trade_date（available_date = trade_date）

        Returns:
            按 trade_date 升序排列的 DataFrame。
        """
        cutoff = await self._prev_trade_date()
        sql = f"""
            SELECT
                index_code,
                trade_date,
                open,
                high,
                low,
                close,
                volume,
                available_date
            FROM {self._schema}.index_daily
            WHERE index_code = $1
              AND available_date <= $2
            ORDER BY trade_date DESC
            LIMIT $3
        """
        rows = await self._fetch(sql, index_code, cutoff, lookback_days)
        df = _to_df(rows)
        return df.sort_values("trade_date").reset_index(drop=True) if not df.empty else df

    async def get_market_breadth(self, lookback_days: int = 20) -> pd.DataFrame:
        """获取市场广度数据（涨跌停家数、涨跌家数等）。

        PIT 过滤: available_date <= prev_trade_date

        Returns:
            按 trade_date 升序排列的 DataFrame。
        """
        cutoff = await self._prev_trade_date()
        sql = f"""
            SELECT
                trade_date,
                market,
                limit_up_count,
                limit_down_count,
                advancing_count,
                declining_count,
                new_high_count,
                new_low_count,
                total_stocks,
                available_date
            FROM {self._schema}.market_breadth_daily
            WHERE available_date <= $1
            ORDER BY trade_date DESC
            LIMIT $2
        """
        rows = await self._fetch(sql, cutoff, lookback_days)
        df = _to_df(rows)
        return df.sort_values("trade_date").reset_index(drop=True) if not df.empty else df

    # ── 行业数据 ────────────────────────────────────────────────────────────

    async def get_industry(self, symbol: str) -> str:
        """获取 symbol 在当前时间点的行业框架分类。

        查询 industry_classify 表（历史快照），若表不存在或无数据返回 'default'。

        PIT 过滤: snapshot_date <= current_date

        Returns:
            industry_framework 字符串，如 'technology'、'consumer_staples'。
        """
        current = self._assert_date()
        sql = f"""
            SELECT industry_framework
            FROM {self._schema}.industry_classify
            WHERE symbol = $1
              AND snapshot_date <= $2
            ORDER BY snapshot_date DESC
            LIMIT 1
        """
        try:
            rows = await self._fetch(sql, symbol, current)
            if rows:
                return rows[0]["industry_framework"] or "default"
        except (asyncpg.UndefinedTableError, asyncpg.UndefinedColumnError):
            logger.warning("industry_classify_table_missing", symbol=symbol)
        return "default"

    async def get_industry_pe_percentile(
        self, symbol: str, industry_framework: str | None = None
    ) -> float:
        """计算 symbol 当前 PE_TTM 在同行业中的百分位（0-100）。

        用于判断相对估值高低。无数据或计算失败时返回 50.0。

        PIT 过滤: available_date <= current_date（通过 fundamentals_daily）

        Args:
            symbol: 目标股票代码。
            industry_framework: 行业框架名，None 时自动查询。

        Returns:
            PE 百分位排名 0-100，100 表示 PE 最高（最贵）。
        """
        current = self._assert_date()
        if industry_framework is None:
            industry_framework = await self.get_industry(symbol)

        # 通过 watchlist / industry_classify 获取同行业股票
        sql_peers = f"""
            SELECT DISTINCT symbol
            FROM {self._schema}.industry_classify
            WHERE industry_framework = $1
              AND snapshot_date <= $2
        """
        try:
            peer_rows = await self._fetch(sql_peers, industry_framework, current)
        except (asyncpg.UndefinedTableError, asyncpg.UndefinedColumnError):
            return 50.0

        if not peer_rows:
            return 50.0

        peers = [r["symbol"] for r in peer_rows]

        # 获取各同行业股票最新 PE
        sql_pe = f"""
            WITH latest AS (
                SELECT DISTINCT ON (symbol)
                    symbol, pe_ttm
                FROM {self._schema}.fundamentals_daily
                WHERE symbol = ANY($1)
                  AND available_date <= $2
                  AND pe_ttm > 0
                ORDER BY symbol, trade_date DESC
            ),
            ranked AS (
                SELECT
                    symbol,
                    pe_ttm,
                    PERCENT_RANK() OVER (ORDER BY pe_ttm) * 100 AS pct_rank
                FROM latest
            )
            SELECT pct_rank
            FROM ranked
            WHERE symbol = $3
        """
        try:
            rows = await self._fetch(sql_pe, peers, current, symbol)
            if rows:
                return float(rows[0]["pct_rank"])
        except Exception as exc:
            logger.warning("pe_percentile_failed", symbol=symbol, error=str(exc))
        return 50.0

    # ── 成交执行（唯一豁免 PIT 的方法）────────────────────────────────────

    async def get_open_price(self, symbol: str, trade_date: date) -> Optional[float]:
        """获取指定日期的开盘价，用于模拟 T+1 成交。

        ⚠️ 警告：这是 PITDataLoader 中唯一允许访问"未来"数据的方法。
        只能在回测引擎的交易执行阶段（execution.py）调用，
        严禁在生成判断（analysis/）阶段调用。

        Args:
            symbol: 股票代码。
            trade_date: 要查询的开盘价日期（通常是 judgment_date + 1）。

        Returns:
            开盘价（float），或 None（停牌/无数据）。
        """
        sql = f"""
            SELECT open
            FROM {self._schema}.market_bars_daily
            WHERE symbol = $1 AND trade_date = $2
        """
        rows = await self._fetch(sql, symbol, trade_date)
        if rows and rows[0]["open"] is not None:
            return float(rows[0]["open"])
        return None

    # ── 内部工具 ─────────────────────────────────────────────────────────────

    async def _fetch(self, sql: str, *args: Any) -> list[asyncpg.Record]:
        """执行 SQL 查询，返回 Record 列表。统一入口，方便日志和 mock。"""
        async with self._pool.acquire() as conn:
            return await conn.fetch(sql, *args)


# ─────────────────────────────────────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────────────────────────────────────

def _to_df(rows: list[asyncpg.Record]) -> pd.DataFrame:
    """将 asyncpg Record 列表转换为 pandas DataFrame。"""
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame([dict(r) for r in rows])
