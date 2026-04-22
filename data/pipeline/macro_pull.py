"""
宏观经济数据拉取管道

A股宏观指标（通过 AkShare 拉取）：
  - cn_pmi_mfg: 制造业PMI（月度）
  - cn_m2_yoy: M2增速（月度）
  - cn_10y_yield: 10年期国债利率（日度）
  - cn_cpi_yoy: CPI同比（月度）

美股宏观指标（通过 yfinance 拉取 ETF 价格代理）：
  - us_vix: VIX 恐慌指数（^VIX）
  - us_tlt: 20年期国债ETF（TLT，代理长端利率）
  - us_hyg: 高收益债ETF（HYG，代理信用利差）

写入 macro_indicators 表。

用法:
    python -m data.pipeline.macro_pull --cn          # A股宏观
    python -m data.pipeline.macro_pull --us          # 美股宏观
    python -m data.pipeline.macro_pull --all         # 全部
    python -m data.pipeline.macro_pull --cn --start 2021-03-01 --end 2026-04-16
"""

from __future__ import annotations

import argparse
import asyncio
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pandas as pd
import structlog

from db.connection import db_execute_many, init_pool, close_pool

logger = structlog.get_logger(__name__)

# AkShare 调用间隔（秒），避免触发频率限制
_RATE_LIMIT_SLEEP: float = 0.5

# 默认拉取区间
_DEFAULT_START = "2020-01-01"
_DEFAULT_END = date.today().isoformat()


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _safe_float(val: Any) -> float | None:
    """将值安全转换为 float，NaN/None 返回 None。

    Args:
        val: 任意输入值。

    Returns:
        float 或 None。
    """
    if val is None:
        return None
    if isinstance(val, float) and pd.isna(val):
        return None
    try:
        f = float(val)
        return None if pd.isna(f) else f
    except (TypeError, ValueError):
        return None


def _parse_cn_date(raw: Any) -> date | None:
    """解析多种中文/标准日期格式为 date 对象。

    支持格式：
        - "YYYY-MM"     → 当月 1 日
        - "YYYY年MM月"  → 当月 1 日
        - "YYYYMM"      → 当月 1 日
        - "YYYY-MM-DD"  → 直接解析

    Args:
        raw: 原始日期字符串或 datetime/date。

    Returns:
        date 对象，解析失败返回 None。
    """
    if isinstance(raw, (date, datetime)):
        return raw.date() if isinstance(raw, datetime) else raw
    s = str(raw).strip()

    # YYYY-MM-DD
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        try:
            return datetime.strptime(s, "%Y-%m-%d").date()
        except ValueError:
            pass

    # YYYY年MM月 or YYYY年MM月份
    m = re.match(r"^(\d{4})年(\d{1,2})月份?$", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), 1)
        except ValueError:
            pass

    # YYYY-MM
    m = re.match(r"^(\d{4})-(\d{2})$", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), 1)
        except ValueError:
            pass

    # YYYYMM
    m = re.match(r"^(\d{4})(\d{2})$", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), 1)
        except ValueError:
            pass

    return None


def _filter_by_range(
    records: list[tuple],
    start: str,
    end: str,
    date_idx: int = 2,
) -> list[tuple]:
    """按日期区间过滤记录列表。

    Args:
        records: 数据元组列表，date_idx 位置为 report_date。
        start: 起始日期字符串 YYYY-MM-DD（含）。
        end: 结束日期字符串 YYYY-MM-DD（含）。
        date_idx: report_date 在 tuple 中的位置索引。

    Returns:
        过滤后的记录列表。
    """
    start_d = datetime.strptime(start, "%Y-%m-%d").date()
    end_d = datetime.strptime(end, "%Y-%m-%d").date()
    return [r for r in records if r[date_idx] is not None and start_d <= r[date_idx] <= end_d]


def _calc_change_pct(value: float | None, prev: float | None) -> float | None:
    """计算环比变化率（百分比）。

    Args:
        value: 当期值。
        prev: 前期值。

    Returns:
        变化率（%），或 None。
    """
    if value is None or prev is None or prev == 0:
        return None
    return round((value - prev) / abs(prev) * 100, 4)


def _build_records_with_prev(
    rows: list[tuple[date, float]],
    indicator_name: str,
    market: str,
    source: str,
) -> list[tuple]:
    """根据日期-值对列表构建含 prev_value/change_pct 的完整记录。

    Args:
        rows: [(report_date, value), ...] 按日期升序排列。
        indicator_name: 指标名称。
        market: 市场代码（CN / US）。
        source: 数据来源。

    Returns:
        (indicator_name, market, report_date, value, prev_value, change_pct, source) 元组列表。
    """
    result: list[tuple] = []
    prev_value: float | None = None
    for report_date, value in rows:
        change_pct = _calc_change_pct(value, prev_value)
        result.append((indicator_name, market, report_date, value, prev_value, change_pct, source))
        prev_value = value
    return result


# ---------------------------------------------------------------------------
# MacroPuller
# ---------------------------------------------------------------------------

class MacroPuller:
    """宏观经济数据拉取器，封装 A 股（AkShare）和美股（yfinance）数据源。

    Attributes:
        _log: structlog 日志器，附带 module 上下文。
    """

    def __init__(self) -> None:
        self._log = logger.bind(module="macro_puller")

    # ------------------------------------------------------------------
    # A 股宏观 — AkShare
    # ------------------------------------------------------------------

    async def pull_cn_pmi(self, start: str, end: str) -> int:
        """拉取制造业PMI月度数据并写入数据库。

        数据源：AkShare macro_china_pmi_yearly（历史年度）或 macro_china_pmi（近期月度）。
        指标名：cn_pmi_mfg，市场：CN，来源：akshare。

        Args:
            start: 起始日期 YYYY-MM-DD（含）。
            end: 结束日期 YYYY-MM-DD（含）。

        Returns:
            写入记录数。
        """
        import akshare as ak

        self._log.info("pull_cn_pmi_start", start=start, end=end)
        try:
            df: pd.DataFrame = await asyncio.to_thread(ak.macro_china_pmi_yearly)
        except Exception as exc:
            self._log.warning("pmi_yearly_failed_fallback", error=str(exc))
            try:
                df = await asyncio.to_thread(ak.macro_china_pmi)
            except Exception as exc2:
                self._log.error("pull_cn_pmi_failed", error=str(exc2))
                return 0

        await asyncio.sleep(_RATE_LIMIT_SLEEP)

        # columns: ['商品', '日期', '今值', '预测值', '前值']
        # filter to 中国官方制造业PMI rows only
        rows: list[tuple[date, float]] = []
        df_pmi = df[df["商品"] == "中国官方制造业PMI"] if "商品" in df.columns else df
        for _, row in df_pmi.iterrows():
            d = _parse_cn_date(row.get("日期") or row.iloc[1])
            v = _safe_float(row.get("今值") or row.iloc[2])
            if d is not None and v is not None:
                rows.append((d, v))

        rows.sort(key=lambda x: x[0])
        records = _build_records_with_prev(rows, "cn_pmi_mfg", "CN", "akshare")
        records = _filter_by_range(records, start, end)

        count = await self._upsert_indicators(records)
        self._log.info("pull_cn_pmi_done", upserted=count)
        return count

    async def pull_cn_m2(self, start: str, end: str) -> int:
        """拉取M2货币供应量同比月度数据并写入数据库。

        数据源：AkShare macro_china_money_supply。
        指标名：cn_m2_yoy，市场：CN，来源：akshare。

        Args:
            start: 起始日期 YYYY-MM-DD（含）。
            end: 结束日期 YYYY-MM-DD（含）。

        Returns:
            写入记录数。
        """
        import akshare as ak

        self._log.info("pull_cn_m2_start", start=start, end=end)
        try:
            df: pd.DataFrame = await asyncio.to_thread(ak.macro_china_money_supply)
        except Exception as exc:
            self._log.error("pull_cn_m2_failed", error=str(exc))
            return 0

        await asyncio.sleep(_RATE_LIMIT_SLEEP)

        # 找日期列（第一列）和 M2 同比列
        date_col = df.columns[0]
        m2_col: str | None = None
        for col in df.columns:
            col_lower = str(col).lower()
            if "m2" in col_lower and ("同比" in col or "yoy" in col_lower):
                m2_col = col
                break

        if m2_col is None:
            # 回退：找包含 M2 的列
            for col in df.columns:
                if "m2" in str(col).lower() or "M2" in str(col):
                    m2_col = col
                    break

        if m2_col is None:
            self._log.error("pull_cn_m2_no_m2_col", columns=list(df.columns))
            return 0

        self._log.debug("pull_cn_m2_col_selected", col=m2_col)

        rows: list[tuple[date, float]] = []
        for _, row in df.iterrows():
            d = _parse_cn_date(row[date_col])
            v = _safe_float(row[m2_col])
            if d is not None and v is not None:
                rows.append((d, v))

        rows.sort(key=lambda x: x[0])
        records = _build_records_with_prev(rows, "cn_m2_yoy", "CN", "akshare")
        records = _filter_by_range(records, start, end)

        count = await self._upsert_indicators(records)
        self._log.info("pull_cn_m2_done", upserted=count)
        return count

    async def pull_cn_bond_yield(self, start: str, end: str) -> int:
        """拉取中国10年期国债收益率日度数据并写入数据库。

        数据源：AkShare bond_zh_us_rate（含中国10年列），日度频率。
        指标名：cn_10y_yield，市场：CN，来源：akshare。

        Args:
            start: 起始日期 YYYY-MM-DD（含）。
            end: 结束日期 YYYY-MM-DD（含）。

        Returns:
            写入记录数。
        """
        import akshare as ak

        self._log.info("pull_cn_bond_yield_start", start=start, end=end)

        # bond_zh_us_rate 不接受日期参数，返回全量数据后再过滤
        try:
            df: pd.DataFrame = await asyncio.to_thread(ak.bond_zh_us_rate)
        except Exception as exc:
            self._log.error("pull_cn_bond_yield_failed", error=str(exc))
            return 0

        await asyncio.sleep(_RATE_LIMIT_SLEEP)

        # 找日期列和中国10年列
        date_col = df.columns[0]
        cn10y_col: str | None = None
        for col in df.columns:
            if "中国" in str(col) and "10" in str(col):
                cn10y_col = col
                break

        if cn10y_col is None:
            # 回退：找第二列
            if len(df.columns) > 1:
                cn10y_col = df.columns[1]
                self._log.warning("bond_yield_fallback_col", col=cn10y_col)
            else:
                self._log.error("pull_cn_bond_yield_no_col", columns=list(df.columns))
                return 0

        self._log.debug("pull_cn_bond_yield_col_selected", col=cn10y_col)

        rows: list[tuple[date, float]] = []
        for _, row in df.iterrows():
            d = _parse_cn_date(row[date_col])
            v = _safe_float(row[cn10y_col])
            if d is not None and v is not None:
                rows.append((d, v))

        rows.sort(key=lambda x: x[0])
        records = _build_records_with_prev(rows, "cn_10y_yield", "CN", "akshare")
        # bond_zh_us_rate 已按区间过滤，仍做二次检查
        records = _filter_by_range(records, start, end)

        count = await self._upsert_indicators(records)
        self._log.info("pull_cn_bond_yield_done", upserted=count)
        return count

    async def pull_cn_cpi(self, start: str, end: str) -> int:
        """拉取CPI同比月度数据并写入数据库。

        数据源：AkShare macro_china_cpi_yearly。
        指标名：cn_cpi_yoy，市场：CN，来源：akshare。

        Args:
            start: 起始日期 YYYY-MM-DD（含）。
            end: 结束日期 YYYY-MM-DD（含）。

        Returns:
            写入记录数。
        """
        import akshare as ak

        self._log.info("pull_cn_cpi_start", start=start, end=end)
        try:
            df: pd.DataFrame = await asyncio.to_thread(ak.macro_china_cpi_yearly)
        except Exception as exc:
            self._log.error("pull_cn_cpi_failed", error=str(exc))
            return 0

        await asyncio.sleep(_RATE_LIMIT_SLEEP)

        # columns: ['商品', '日期', '今值', '预测值', '前值']
        # filter to CPI rows only
        rows: list[tuple[date, float]] = []
        df_cpi = df[df["商品"].str.contains("CPI", na=False)] if "商品" in df.columns else df
        for _, row in df_cpi.iterrows():
            d = _parse_cn_date(row.get("日期") or row.iloc[1])
            v = _safe_float(row.get("今值") or row.iloc[2])
            if d is not None and v is not None:
                rows.append((d, v))

        rows.sort(key=lambda x: x[0])
        records = _build_records_with_prev(rows, "cn_cpi_yoy", "CN", "akshare")
        records = _filter_by_range(records, start, end)

        count = await self._upsert_indicators(records)
        self._log.info("pull_cn_cpi_done", upserted=count)
        return count

    # ------------------------------------------------------------------
    # 美股宏观 — yfinance
    # ------------------------------------------------------------------

    async def pull_us_market_proxies(self, start: str, end: str) -> int:
        """拉取美股宏观代理指标（VIX / TLT / HYG）并写入数据库。

        通过 yfinance 下载日线收盘价作为宏观代理：
            - indicator_name='us_vix'  ← ^VIX 收盘
            - indicator_name='us_tlt'  ← TLT 收盘
            - indicator_name='us_hyg'  ← HYG 收盘

        Args:
            start: 起始日期 YYYY-MM-DD（含）。
            end: 结束日期 YYYY-MM-DD（含）。

        Returns:
            写入记录数。
        """
        import yfinance as yf

        self._log.info("pull_us_proxies_start", start=start, end=end)

        tickers = ["^VIX", "TLT", "HYG"]
        indicator_map = {"^VIX": "us_vix", "TLT": "us_tlt", "HYG": "us_hyg"}

        try:
            raw: pd.DataFrame = await asyncio.to_thread(
                yf.download,
                tickers,
                start=start,
                end=end,
                auto_adjust=True,
                progress=False,
            )
        except Exception as exc:
            self._log.error("pull_us_proxies_failed", error=str(exc))
            return 0

        # yfinance 多 ticker 返回 MultiIndex columns: (field, ticker)
        # 取 Close 层
        if isinstance(raw.columns, pd.MultiIndex):
            close_df = raw["Close"] if "Close" in raw.columns.get_level_values(0) else raw.xs("Close", axis=1, level=0)
        else:
            close_df = raw[["Close"]] if "Close" in raw.columns else raw

        all_records: list[tuple] = []

        for ticker in tickers:
            indicator_name = indicator_map[ticker]
            ticker_key = ticker  # ^VIX 在 MultiIndex 里就是 "^VIX"

            if ticker_key not in close_df.columns:
                self._log.warning("us_proxy_ticker_missing", ticker=ticker_key)
                continue

            series = close_df[ticker_key].dropna()
            rows: list[tuple[date, float]] = []
            for idx, val in series.items():
                d = idx.date() if hasattr(idx, "date") else idx
                v = _safe_float(val)
                if d is not None and v is not None:
                    rows.append((d, v))

            rows.sort(key=lambda x: x[0])
            records = _build_records_with_prev(rows, indicator_name, "US", "yfinance")
            all_records.extend(records)

        count = await self._upsert_indicators(all_records)
        self._log.info("pull_us_proxies_done", upserted=count)
        return count

    # ------------------------------------------------------------------
    # 组合拉取
    # ------------------------------------------------------------------

    async def pull_cn(self, start: str, end: str) -> dict[str, int]:
        """拉取全部 A 股宏观指标。

        Args:
            start: 起始日期 YYYY-MM-DD。
            end: 结束日期 YYYY-MM-DD。

        Returns:
            各指标写入记录数字典，如 {'cn_pmi_mfg': 72, ...}。
        """
        pmi = await self.pull_cn_pmi(start, end)
        m2 = await self.pull_cn_m2(start, end)
        bond = await self.pull_cn_bond_yield(start, end)
        cpi = await self.pull_cn_cpi(start, end)
        return {
            "cn_pmi_mfg": pmi,
            "cn_m2_yoy": m2,
            "cn_10y_yield": bond,
            "cn_cpi_yoy": cpi,
        }

    async def pull_us(self, start: str, end: str) -> dict[str, int]:
        """拉取全部美股宏观代理指标。

        Args:
            start: 起始日期 YYYY-MM-DD。
            end: 结束日期 YYYY-MM-DD。

        Returns:
            写入记录数字典，如 {'us_proxies': 450}。
        """
        count = await self.pull_us_market_proxies(start, end)
        return {"us_proxies_total": count}

    async def pull_all(self, start: str, end: str) -> dict[str, int]:
        """拉取所有宏观指标（A 股 + 美股）。

        Args:
            start: 起始日期 YYYY-MM-DD。
            end: 结束日期 YYYY-MM-DD。

        Returns:
            各指标写入记录数汇总字典。
        """
        self._log.info("pull_all_start", start=start, end=end)
        cn_result = await self.pull_cn(start, end)
        us_result = await self.pull_us(start, end)
        summary = {**cn_result, **us_result}
        total = sum(summary.values())
        self._log.info("pull_all_done", summary=summary, total=total)
        return summary

    # ------------------------------------------------------------------
    # 数据库写入
    # ------------------------------------------------------------------

    async def _upsert_indicators(self, records: list[tuple]) -> int:
        """将宏观指标记录 upsert 到 macro_indicators 表。

        每条记录格式：
            (indicator_name, market, report_date, value, prev_value, change_pct, source)

        冲突主键 (indicator_name, market, report_date) 时更新 value/prev_value/change_pct。

        Args:
            records: 数据元组列表。

        Returns:
            写入记录数（等于 len(records)，upsert 成功时）。
        """
        if not records:
            return 0

        sql = """
            INSERT INTO macro_indicators
                (indicator_name, market, report_date, value, prev_value, change_pct, source)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (indicator_name, market, report_date)
            DO UPDATE SET
                value       = EXCLUDED.value,
                prev_value  = EXCLUDED.prev_value,
                change_pct  = EXCLUDED.change_pct
        """
        try:
            await db_execute_many(sql, records)
            self._log.debug("upsert_indicators_done", count=len(records))
            return len(records)
        except Exception as exc:
            self._log.error("upsert_indicators_failed", error=str(exc), count=len(records))
            raise


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------

async def _main() -> None:
    """命令行入口，解析参数并运行对应拉取任务。"""
    parser = argparse.ArgumentParser(
        description="宏观经济数据拉取管道",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--cn", action="store_true", help="拉取 A 股宏观指标")
    parser.add_argument("--us", action="store_true", help="拉取美股宏观代理指标")
    parser.add_argument("--all", dest="all_markets", action="store_true", help="拉取全部指标")
    parser.add_argument(
        "--start",
        default=_DEFAULT_START,
        help=f"起始日期 YYYY-MM-DD（默认 {_DEFAULT_START}）",
    )
    parser.add_argument(
        "--end",
        default=_DEFAULT_END,
        help=f"结束日期 YYYY-MM-DD（默认 {_DEFAULT_END}）",
    )

    args = parser.parse_args()

    if not any([args.cn, args.us, args.all_markets]):
        parser.print_help()
        return

    await init_pool()
    puller = MacroPuller()

    try:
        if args.all_markets:
            summary = await puller.pull_all(args.start, args.end)
        elif args.cn:
            summary = await puller.pull_cn(args.start, args.end)
        elif args.us:
            summary = await puller.pull_us(args.start, args.end)
        else:
            summary = {}

        print("\n=== 宏观数据拉取完成 ===")
        for key, count in summary.items():
            print(f"  {key}: {count} 条")
        print(f"  合计: {sum(summary.values())} 条")
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
