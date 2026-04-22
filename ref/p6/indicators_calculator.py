"""衍生指标计算引擎。

基于 raw 数据计算衍生指标，写入 derived.indicators_daily。
所有价格类指标使用前复权价格。
"""
import uuid
import logging
import time
from datetime import date

import pandas as pd
import psycopg2
from psycopg2.extras import execute_values

logger = logging.getLogger(__name__)


class IndicatorCalculator:
    """批量计算衍生指标。"""

    def __init__(self, conn: psycopg2.extensions.connection):
        self.conn = conn
        self.batch_id = str(uuid.uuid4())

    def calc_all(self, trade_date: date | None = None):
        """计算指定日期（或最新日）的所有指标。"""
        if trade_date is None:
            with self.conn.cursor() as cur:
                cur.execute("SELECT MAX(trade_date) FROM raw.daily_price")
                trade_date = cur.fetchone()[0]

        logger.info("Calculating indicators for %s (batch %s)", trade_date, self.batch_id[:8])
        start = time.time()

        # 加载所需数据
        df = self._load_price_data(trade_date)
        if df.empty:
            logger.warning("No price data for %s", trade_date)
            return

        basic = self._load_basic_data(trade_date)
        bench = self._load_benchmark(trade_date)
        self._fin_cache = self._load_financial_indicators(trade_date)

        results = []
        for ts_code, group in df.groupby("ts_code"):
            group = group.sort_values("trade_date")
            if len(group) < 5:
                continue

            row = {"ts_code": ts_code, "trade_date": trade_date}

            # 收益率
            close = group["close"].values
            row["return_5d"] = self._return_nd(close, 5)
            row["return_10d"] = self._return_nd(close, 10)
            row["return_20d"] = self._return_nd(close, 20)
            row["return_60d"] = self._return_nd(close, 60)

            # 均线偏离
            row["ma5_deviation"] = self._ma_deviation(close, 5)
            row["ma20_deviation"] = self._ma_deviation(close, 20)
            row["ma60_deviation"] = self._ma_deviation(close, 60)

            # 量比
            vol = group["volume"].values
            row["volume_ratio_5d"] = self._volume_ratio(vol, 5)
            row["volume_ratio_20d"] = self._volume_ratio(vol, 20)

            # 相对强度（vs 沪深300）
            if bench is not None and len(bench) >= 60:
                row["relative_strength_20d"] = self._relative_strength(close, bench, 20)
                row["relative_strength_60d"] = self._relative_strength(close, bench, 60)

            # 新高新低
            row["new_high_20d"] = self._is_new_high(close, 20)
            row["new_high_60d"] = self._is_new_high(close, 60)
            row["new_low_20d"] = self._is_new_low(close, 20)
            row["new_low_60d"] = self._is_new_low(close, 60)

            # 成交额变化（近5日均值 vs 前20日均值）
            if "amount" in group.columns:
                amt = group["amount"].values
                row["amount_change_5v20"] = self._amount_change(amt, 5, 20)

            # 基本面指标（从财报数据）
            if ts_code in self._fin_cache:
                fin = self._fin_cache[ts_code]
                row["roe_ttm"] = fin.get("roe_ttm")
                row["earnings_yield"] = fin.get("earnings_yield")
                row["cashflow_quality"] = fin.get("cashflow_quality")

            # 估值分位（需要 daily_basic 数据）
            if ts_code in basic:
                pe_series = basic[ts_code]["pe_ttm"]
                pb_series = basic[ts_code]["pb"]
                row["pe_percentile_3y"] = self._percentile(pe_series, 3 * 250)
                row["pe_percentile_5y"] = self._percentile(pe_series, 5 * 250)
                row["pb_percentile_3y"] = self._percentile(pb_series, 3 * 250)
                row["pb_percentile_5y"] = self._percentile(pb_series, 5 * 250)

            row["calc_batch_id"] = self.batch_id
            results.append(row)

        if results:
            self._write_results(results)

        elapsed = time.time() - start
        logger.info("Calculated %d stocks in %.1fs", len(results), elapsed)

    def _load_price_data(self, trade_date: date) -> pd.DataFrame:
        """加载最近180天的前复权价格+成交额。"""
        sql = """
            SELECT p.ts_code, p.trade_date,
                   p.close * a.adj_factor / latest.adj_factor as close,
                   p.volume, p.amount
            FROM raw.daily_price p
            JOIN raw.adj_factor a ON p.ts_code = a.ts_code AND p.trade_date = a.trade_date
            JOIN (
                SELECT ts_code, adj_factor
                FROM raw.adj_factor
                WHERE trade_date = %s
            ) latest ON p.ts_code = latest.ts_code
            WHERE p.trade_date BETWEEN %s - INTERVAL '180 days' AND %s
            ORDER BY p.ts_code, p.trade_date
        """
        return pd.read_sql(sql, self.conn, params=[trade_date, trade_date, trade_date])

    def _load_financial_indicators(self, trade_date: date) -> dict:
        """从最新财报计算 ROE_TTM / earnings_yield / cashflow_quality。"""
        # 最新一期利润表 + 现金流
        sql = """
            SELECT DISTINCT ON (i.ts_code)
                i.ts_code,
                i.n_income,
                i.revenue,
                c.n_cashflow_act,
                b2.total_hldr_eqy_exc_min_int as equity,
                b2.total_assets,
                b2.total_liab
            FROM raw.income_statement i
            LEFT JOIN raw.cashflow c ON i.ts_code = c.ts_code AND i.end_date = c.end_date
            LEFT JOIN raw.balance_sheet b2 ON i.ts_code = b2.ts_code AND i.end_date = b2.end_date
            WHERE i.ann_date <= %s
            ORDER BY i.ts_code, i.end_date DESC
        """
        df = pd.read_sql(sql, self.conn, params=[trade_date])

        result = {}
        for _, row in df.iterrows():
            ts = row["ts_code"]
            fin = {}

            # ROE_TTM (简化：用最新期净利润/净资产，TTM需4期累加但这里先用单期)
            equity = row.get("equity")
            n_income = row.get("n_income")
            if equity and n_income and float(equity) > 0:
                fin["roe_ttm"] = round(float(n_income) / float(equity) * 100, 2)

            # Earnings yield = EBIT / (总市值+总负债)
            # 简化：用净利润 / 总资产
            total_assets = row.get("total_assets")
            if n_income and total_assets and float(total_assets) > 0:
                fin["earnings_yield"] = round(float(n_income) / float(total_assets), 4)

            # Cashflow quality = 经营现金流 / 净利润
            cf = row.get("n_cashflow_act")
            if cf and n_income and float(n_income) != 0:
                fin["cashflow_quality"] = round(float(cf) / float(n_income), 4)

            if fin:
                result[ts] = fin

        return result

    def _load_benchmark(self, trade_date: date):
        """加载沪深300指数收盘价序列。"""
        sql = """
            SELECT close FROM raw.index_daily
            WHERE ts_code = '000300.SH'
              AND trade_date BETWEEN %s - INTERVAL '180 days' AND %s
            ORDER BY trade_date
        """
        df = pd.read_sql(sql, self.conn, params=[trade_date, trade_date])
        return df["close"].values if not df.empty else None

    def _load_basic_data(self, trade_date: date) -> dict:
        """加载最近5年的 PE/PB 数据。"""
        sql = """
            SELECT ts_code, trade_date, pe_ttm, pb
            FROM raw.daily_basic
            WHERE trade_date BETWEEN %s - INTERVAL '5 years' AND %s
              AND pe_ttm IS NOT NULL AND pe_ttm > 0
            ORDER BY ts_code, trade_date
        """
        df = pd.read_sql(sql, self.conn, params=[trade_date, trade_date])
        result = {}
        for ts_code, group in df.groupby("ts_code"):
            result[ts_code] = {
                "pe_ttm": group["pe_ttm"].values,
                "pb": group["pb"].values,
            }
        return result

    def _write_results(self, results: list[dict]):
        """写入 derived.indicators_daily。"""
        cols = [
            "ts_code", "trade_date",
            "return_5d", "return_10d", "return_20d", "return_60d",
            "ma5_deviation", "ma20_deviation", "ma60_deviation",
            "volume_ratio_5d", "volume_ratio_20d",
            "relative_strength_20d", "relative_strength_60d",
            "new_high_20d", "new_high_60d", "new_low_20d", "new_low_60d",
            "amount_change_5v20",
            "roe_ttm", "earnings_yield", "cashflow_quality",
            "pe_percentile_3y", "pe_percentile_5y",
            "pb_percentile_3y", "pb_percentile_5y",
            "calc_batch_id",
        ]

        values = []
        for r in results:
            values.append(tuple(r.get(c) for c in cols))

        update = ", ".join(
            f"{c} = EXCLUDED.{c}" for c in cols if c not in ("ts_code", "trade_date")
        )
        sql = f"""
            INSERT INTO derived.indicators_daily ({", ".join(cols)})
            VALUES %s
            ON CONFLICT (ts_code, trade_date) DO UPDATE SET {update}
        """

        with self.conn.cursor() as cur:
            execute_values(cur, sql, values, page_size=2000)
        self.conn.commit()

    @staticmethod
    def _return_nd(close, n):
        if len(close) < n + 1:
            return None
        return float((close[-1] / close[-n - 1]) - 1)

    @staticmethod
    def _ma_deviation(close, n):
        if len(close) < n:
            return None
        ma = float(close[-n:].mean())
        if ma == 0:
            return None
        return float((close[-1] - ma) / ma)

    @staticmethod
    def _volume_ratio(vol, n):
        if len(vol) < n + 1:
            return None
        avg = float(vol[-n - 1:-1].mean())
        if avg == 0:
            return None
        return float(vol[-1] / avg)

    @staticmethod
    def _relative_strength(close, bench, n):
        """个股N日收益 vs 基准N日收益。"""
        if len(close) < n + 1 or len(bench) < n + 1:
            return None
        stock_ret = (close[-1] / close[-n - 1]) - 1
        bench_ret = (float(bench[-1]) / float(bench[-n - 1])) - 1
        return float(stock_ret - bench_ret)

    @staticmethod
    def _is_new_high(close, n):
        if len(close) < n:
            return None
        return bool(close[-1] >= max(close[-n:]))

    @staticmethod
    def _is_new_low(close, n):
        if len(close) < n:
            return None
        return bool(close[-1] <= min(close[-n:]))

    @staticmethod
    def _amount_change(amount, short, long):
        """近short日均成交额 vs 前long日均成交额。"""
        if len(amount) < short + long + 1:
            return None
        recent = float(amount[-short:].mean())
        prev = float(amount[-short - long:-short].mean())
        if prev == 0:
            return None
        return float((recent - prev) / prev)

    @staticmethod
    def _percentile(series, window):
        if len(series) < 10:
            return None
        data = series[-window:] if len(series) >= window else series
        current = data[-1]
        rank = (data < current).sum()
        return float(rank / len(data))
