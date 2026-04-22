"""
P6+ 分钟级数据拉取器 — 多源轮转版
====================================
目标：拉取锁定板块（计算机/电子/通信/有色金属）候选池股票的最近2年分钟线数据
数据源：tushare / baostock / akshare / efinance，轮流拉取，规避单源限流
存储：本地 parquet 文件（按股票分文件），可选写入 PostgreSQL/DuckDB

使用方法：
1. pip install tushare baostock akshare efinance pandas pyarrow
2. 配置下方 CONFIG 区域（tushare token 等）
3. python fetch_minute_data.py

作者：轩老板 x Claude | 2026-04
"""

import os
import time
import datetime
import logging
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field

import pandas as pd

# ============================================================
# CONFIG — 按需修改
# ============================================================

TUSHARE_TOKEN = os.environ.get("TUSHARE_TOKEN", "693a34158e1f79f41fb1593ba7fdcd21c65f9bb965d332f88319407c")

# 锁定板块的申万行业代码（用于 tushare 拉候选池）
# 计算机 801750 / 电子 801080 / 通信 801770 / 有色金属 801050
SECTOR_CODES = {
    "计算机": "801750",
    "电子": "801080",
    "通信": "801770",
    "有色金属": "801050",
}

# 候选池基本面门槛（与 P6+ Layer6 一致）
MIN_MARKET_CAP = 20e8       # 市值 > 20亿
MIN_ROE = 5.0               # ROE > 5%
RANK_PCT_THRESHOLD = 0.88   # 行业内前12%

# 数据时间范围
START_DATE = "2025-01-01"
END_DATE = "2026-04-10"

# 分钟级别：1min / 5min / 15min / 30min / 60min
FREQ = "5min"  # 推荐先拉5分钟线，数据量适中且够用

# 存储路径
DATA_DIR = Path("./minute_data")
DATA_DIR.mkdir(exist_ok=True)

# 各数据源每分钟可请求次数（保守估计，避免被封）
RATE_LIMITS = {
    "tushare":  {"calls_per_min": 80,  "sleep": 0.8},   # 5000积分约80次/分
    "baostock": {"calls_per_min": 50,  "sleep": 1.2},   # 无官方限制但保守
    "akshare":  {"calls_per_min": 30,  "sleep": 2.0},   # 底层爬虫，需慢
    "efinance": {"calls_per_min": 30,  "sleep": 2.0},   # 同上
}

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(DATA_DIR / "fetch.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ============================================================
# DATA SOURCE ADAPTERS
# ============================================================

@dataclass
class FetchResult:
    success: bool
    df: Optional[pd.DataFrame] = None
    error: str = ""
    rows: int = 0


class TushareSource:
    """Tushare Pro — 最稳定，但有积分限制"""

    def __init__(self, token: str):
        import tushare as ts
        ts.set_token(token)
        self.pro = ts.pro_api()
        self.name = "tushare"

    def get_stock_list(self, sector_code: str) -> list[str]:
        """获取板块成分股"""
        try:
            df = self.pro.index_member(index_code=sector_code)
            if df is not None and not df.empty:
                return df["con_code"].tolist()
        except Exception as e:
            log.warning(f"tushare index_member failed for {sector_code}: {e}")
        return []

    def fetch_minutes(self, ts_code: str, start: str, end: str, freq: str) -> FetchResult:
        """
        tushare 分钟线接口：stk_mins (需5000+积分)
        freq: 1min/5min/15min/30min/60min
        注意：tushare 每次最多返回8000条，需要分段拉取
        """
        try:
            freq_map = {"1min": "1min", "5min": "5min", "15min": "15min",
                        "30min": "30min", "60min": "60min"}
            ts_freq = freq_map.get(freq, "5min")

            all_dfs = []
            # tushare 分钟线用 start_date/end_date 格式 "2024-04-10 09:30:00"
            seg_start = start
            while seg_start < end:
                # 每次拉30天（5min线约30天=~5800条，安全在8000内）
                seg_end_dt = min(
                    pd.Timestamp(seg_start) + pd.Timedelta(days=30),
                    pd.Timestamp(end)
                )
                seg_end = seg_end_dt.strftime("%Y-%m-%d")

                df = self.pro.stk_mins(
                    ts_code=ts_code,
                    freq=ts_freq,
                    start_date=seg_start,
                    end_date=seg_end,
                )
                if df is not None and not df.empty:
                    all_dfs.append(df)

                seg_start = (seg_end_dt + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
                time.sleep(RATE_LIMITS["tushare"]["sleep"])

            if all_dfs:
                result = pd.concat(all_dfs, ignore_index=True)
                result = self._normalize(result, ts_code)
                return FetchResult(True, result, rows=len(result))
            return FetchResult(False, error="tushare returned empty")
        except Exception as e:
            return FetchResult(False, error=f"tushare error: {e}")

    def _normalize(self, df: pd.DataFrame, ts_code: str) -> pd.DataFrame:
        """统一列名"""
        col_map = {
            "trade_time": "datetime", "ts_code": "code",
            "open": "open", "high": "high", "low": "low",
            "close": "close", "vol": "volume", "amount": "amount",
        }
        df = df.rename(columns=col_map)
        for c in ["datetime", "code", "open", "high", "low", "close", "volume"]:
            if c not in df.columns:
                if c == "code":
                    df[c] = ts_code
                elif c == "volume":
                    df[c] = 0
        df["source"] = "tushare"
        return df[["datetime", "code", "open", "high", "low", "close", "volume", "source"]]


class BaostockSource:
    """Baostock — 免费，无token，但速度慢"""

    def __init__(self):
        import baostock as bs
        self.bs = bs
        result = bs.login()
        log.info(f"baostock login: {result.error_msg}")
        self.name = "baostock"

    def fetch_minutes(self, ts_code: str, start: str, end: str, freq: str) -> FetchResult:
        try:
            # tushare代码 000001.SZ → baostock代码 sz.000001
            code = self._convert_code(ts_code)
            freq_map = {"1min": "1", "5min": "5", "15min": "15",
                        "30min": "30", "60min": "60"}
            bs_freq = freq_map.get(freq, "5")

            rs = self.bs.query_history_k_data_plus(
                code,
                "date,time,open,high,low,close,volume,amount",
                start_date=start, end_date=end,
                frequency=bs_freq, adjustflag="2",  # 前复权
            )
            rows = []
            while rs.error_code == "0" and rs.next():
                rows.append(rs.get_row_data())
            if rows:
                df = pd.DataFrame(rows, columns=rs.fields)
                df = self._normalize(df, ts_code)
                return FetchResult(True, df, rows=len(df))
            return FetchResult(False, error="baostock returned empty")
        except Exception as e:
            return FetchResult(False, error=f"baostock error: {e}")

    def _convert_code(self, ts_code: str) -> str:
        """000001.SZ → sz.000001"""
        parts = ts_code.split(".")
        if len(parts) == 2:
            return f"{parts[1].lower()}.{parts[0]}"
        return ts_code

    def _normalize(self, df: pd.DataFrame, ts_code: str) -> pd.DataFrame:
        df = df.rename(columns={"time": "datetime"})
        # baostock time格式: 20240410093500000 → 需要转换
        if "date" in df.columns and "datetime" in df.columns:
            df["datetime"] = df["datetime"].apply(
                lambda x: f"{x[:4]}-{x[4:6]}-{x[6:8]} {x[8:10]}:{x[10:12]}:{x[12:14]}"
                if len(str(x)) >= 14 else x
            )
        df["code"] = ts_code
        for c in ["open", "high", "low", "close", "volume"]:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        df["source"] = "baostock"
        return df[["datetime", "code", "open", "high", "low", "close", "volume", "source"]]

    def __del__(self):
        try:
            self.bs.logout()
        except Exception:
            pass


class AkshareSource:
    """Akshare — 免费，底层爬东财/新浪，需控速"""

    def __init__(self):
        import akshare as ak
        self.ak = ak
        self.name = "akshare"

    def fetch_minutes(self, ts_code: str, start: str, end: str, freq: str) -> FetchResult:
        try:
            # tushare代码 000001.SZ → akshare代码 sz000001 或纯数字
            symbol = self._convert_code(ts_code)
            # akshare的分钟线接口：stock_zh_a_hist_min_em (东财源)
            period_map = {"1min": "1", "5min": "5", "15min": "15",
                          "30min": "30", "60min": "60"}
            period = period_map.get(freq, "5")

            df = self.ak.stock_zh_a_hist_min_em(
                symbol=symbol,
                period=period,
                start_date=start.replace("-", "") + " 09:30:00",
                end_date=end.replace("-", "") + " 15:00:00",
                adjust="qfq",  # 前复权
            )
            if df is not None and not df.empty:
                df = self._normalize(df, ts_code)
                return FetchResult(True, df, rows=len(df))
            return FetchResult(False, error="akshare returned empty")
        except Exception as e:
            return FetchResult(False, error=f"akshare error: {e}")

    def _convert_code(self, ts_code: str) -> str:
        """000001.SZ → 000001"""
        return ts_code.split(".")[0]

    def _normalize(self, df: pd.DataFrame, ts_code: str) -> pd.DataFrame:
        col_map = {"时间": "datetime", "开盘": "open", "最高": "high",
                    "最低": "low", "收盘": "close", "成交量": "volume"}
        df = df.rename(columns=col_map)
        df["code"] = ts_code
        df["source"] = "akshare"
        cols = ["datetime", "code", "open", "high", "low", "close", "volume", "source"]
        for c in cols:
            if c not in df.columns:
                df[c] = 0 if c == "volume" else ""
        return df[cols]


class EfinanceSource:
    """efinance — 免费，东财数据源，接口简洁"""

    def __init__(self):
        import efinance as ef
        self.ef = ef
        self.name = "efinance"

    def fetch_minutes(self, ts_code: str, start: str, end: str, freq: str) -> FetchResult:
        try:
            code = ts_code.split(".")[0]
            # efinance 的 kfq=1 前复权
            freq_map = {"1min": 1, "5min": 5, "15min": 15,
                        "30min": 30, "60min": 60}
            ef_freq = freq_map.get(freq, 5)

            df = self.ef.stock.get_quote_history(
                code, klt=ef_freq, beg=start.replace("-", ""),
                end=end.replace("-", ""),
            )
            if df is not None and not df.empty:
                df = self._normalize(df, ts_code)
                return FetchResult(True, df, rows=len(df))
            return FetchResult(False, error="efinance returned empty")
        except Exception as e:
            return FetchResult(False, error=f"efinance error: {e}")

    def _normalize(self, df: pd.DataFrame, ts_code: str) -> pd.DataFrame:
        col_map = {"日期": "datetime", "开盘": "open", "最高": "high",
                    "最低": "low", "收盘": "close", "成交量": "volume"}
        df = df.rename(columns=col_map)
        df["code"] = ts_code
        df["source"] = "efinance"
        cols = ["datetime", "code", "open", "high", "low", "close", "volume", "source"]
        for c in cols:
            if c not in df.columns:
                df[c] = 0 if c == "volume" else ""
        return df[cols]


# ============================================================
# CANDIDATE POOL — 获取候选池股票列表
# ============================================================

def get_candidate_pool(tushare_src: TushareSource) -> list[str]:
    """直接使用 2025 年候选池并集（142只精选股）。"""
    log.info(f"使用 2025 候选池并集: {len(FALLBACK_STOCK_LIST)} 只")
    return FALLBACK_STOCK_LIST


# 2025年候选池并集（计算机/电子/通信/有色金属，共142只）
FALLBACK_STOCK_LIST = [
    "000426.SZ", "000506.SZ", "000612.SZ", "000688.SZ", "000737.SZ",
    "000807.SZ", "000933.SZ", "000975.SZ", "000977.SZ", "001309.SZ",
    "001337.SZ", "001389.SZ", "002049.SZ", "002134.SZ", "002180.SZ",
    "002222.SZ", "002273.SZ", "002371.SZ", "002463.SZ", "002716.SZ",
    "002729.SZ", "002916.SZ", "002947.SZ", "002970.SZ", "002978.SZ",
    "003019.SZ", "300033.SZ", "300054.SZ", "300115.SZ", "300249.SZ",
    "300264.SZ", "300308.SZ", "300322.SZ", "300394.SZ", "300408.SZ",
    "300442.SZ", "300475.SZ", "300476.SZ", "300502.SZ", "300508.SZ",
    "300531.SZ", "300548.SZ", "300570.SZ", "300576.SZ", "300604.SZ",
    "300627.SZ", "300628.SZ", "300679.SZ", "300752.SZ", "300787.SZ",
    "300803.SZ", "300811.SZ", "300831.SZ", "300835.SZ", "300857.SZ",
    "300866.SZ", "300916.SZ", "300940.SZ", "300976.SZ", "301031.SZ",
    "301099.SZ", "301162.SZ", "301183.SZ", "301308.SZ", "301382.SZ",
    "301396.SZ", "301458.SZ", "301479.SZ", "301489.SZ", "301491.SZ",
    "301556.SZ", "301566.SZ", "301589.SZ", "301606.SZ", "301611.SZ",
    "600105.SH", "600111.SH", "600183.SH", "600219.SH", "600281.SH",
    "600301.SH", "600338.SH", "600392.SH", "600455.SH", "600563.SH",
    "600589.SH", "600666.SH", "600845.SH", "600941.SH", "600988.SH",
    "601020.SH", "601168.SH", "601702.SH", "601899.SH", "603061.SH",
    "603236.SH", "603296.SH", "603322.SH", "603341.SH", "603496.SH",
    "603508.SH", "603629.SH", "603893.SH", "603979.SH", "605118.SH",
    "605376.SH", "688008.SH", "688018.SH", "688019.SH", "688041.SH",
    "688049.SH", "688080.SH", "688082.SH", "688088.SH", "688093.SH",
    "688109.SH", "688111.SH", "688123.SH", "688127.SH", "688150.SH",
    "688159.SH", "688183.SH", "688188.SH", "688195.SH", "688200.SH",
    "688208.SH", "688213.SH", "688256.SH", "688258.SH", "688279.SH",
    "688288.SH", "688313.SH", "688486.SH", "688525.SH", "688550.SH",
    "688582.SH", "688588.SH", "688615.SH", "688692.SH", "688766.SH",
    "688775.SH", "688800.SH",
]


# ============================================================
# MULTI-SOURCE ROTATION ENGINE
# ============================================================

class RotationFetcher:
    """
    多源轮转拉取器
    策略：
    1. 优先用 tushare（最稳定，数据质量最高）
    2. tushare 失败或触发限流 → 切换 baostock
    3. baostock 失败 → 切换 akshare
    4. akshare 失败 → 切换 efinance
    5. 全部失败 → 记录到失败列表，最后重试
    """

    def __init__(self):
        self.sources: list = []
        self.source_names: list[str] = []
        self._init_sources()

        self.stats = {name: {"success": 0, "fail": 0, "rows": 0}
                      for name in self.source_names}
        self.failed_stocks: list[str] = []

    def _init_sources(self):
        """按优先级初始化数据源，失败的跳过"""
        # 1. Tushare
        try:
            src = TushareSource(TUSHARE_TOKEN)
            self.sources.append(src)
            self.source_names.append("tushare")
            log.info("✅ tushare 初始化成功")
        except Exception as e:
            log.warning(f"❌ tushare 初始化失败: {e}")

        # 2. Baostock
        try:
            src = BaostockSource()
            self.sources.append(src)
            self.source_names.append("baostock")
            log.info("✅ baostock 初始化成功")
        except Exception as e:
            log.warning(f"❌ baostock 初始化失败: {e}")

        # 3. Akshare
        try:
            src = AkshareSource()
            self.sources.append(src)
            self.source_names.append("akshare")
            log.info("✅ akshare 初始化成功")
        except Exception as e:
            log.warning(f"❌ akshare 初始化失败: {e}")

        # 4. Efinance
        try:
            src = EfinanceSource()
            self.sources.append(src)
            self.source_names.append("efinance")
            log.info("✅ efinance 初始化成功")
        except Exception as e:
            log.warning(f"❌ efinance 初始化失败: {e}")

        if not self.sources:
            raise RuntimeError("所有数据源初始化失败！请检查依赖安装")

    def fetch_one_stock(self, ts_code: str) -> Optional[pd.DataFrame]:
        """
        轮转拉取单只股票，任一源成功即返回
        """
        parquet_path = DATA_DIR / f"{ts_code.replace('.', '_')}_{FREQ}.parquet"

        # 断点续传：如果已有数据，检查最新日期，只拉增量
        existing_end = None
        if parquet_path.exists():
            try:
                existing = pd.read_parquet(parquet_path)
                if not existing.empty:
                    existing_end = pd.Timestamp(existing["datetime"].max())
                    if existing_end >= pd.Timestamp(END_DATE) - pd.Timedelta(days=1):
                        log.info(f"⏭️  {ts_code} 已完整，跳过")
                        return existing
                    log.info(f"📎 {ts_code} 断点续传，从 {existing_end.date()} 开始")
            except Exception:
                existing_end = None

        fetch_start = (existing_end + pd.Timedelta(days=1)).strftime("%Y-%m-%d") \
            if existing_end else START_DATE

        for src in self.sources:
            src_name = src.name
            sleep_time = RATE_LIMITS[src_name]["sleep"]

            log.info(f"🔄 {ts_code} ← {src_name}")
            result = src.fetch_minutes(ts_code, fetch_start, END_DATE, FREQ)

            if result.success and result.df is not None and len(result.df) > 0:
                self.stats[src_name]["success"] += 1
                self.stats[src_name]["rows"] += result.rows
                log.info(f"✅ {ts_code} ← {src_name}: {result.rows} 行")

                # 合并已有数据
                final_df = result.df
                if existing_end and parquet_path.exists():
                    existing = pd.read_parquet(parquet_path)
                    final_df = pd.concat([existing, result.df], ignore_index=True)
                    final_df = final_df.drop_duplicates(subset=["datetime", "code"])
                    final_df = final_df.sort_values("datetime").reset_index(drop=True)

                # 保存
                final_df.to_parquet(parquet_path, index=False)
                time.sleep(sleep_time)
                return final_df
            else:
                self.stats[src_name]["fail"] += 1
                log.warning(f"❌ {ts_code} ← {src_name}: {result.error}")
                time.sleep(sleep_time)

        # 全部失败
        self.failed_stocks.append(ts_code)
        log.error(f"💀 {ts_code} 所有数据源均失败")
        return None

    def fetch_all(self, stock_list: list[str]):
        """批量拉取所有股票"""
        total = len(stock_list)
        log.info(f"\n{'='*60}")
        log.info(f"开始拉取 {total} 只股票的 {FREQ} 分钟线")
        log.info(f"时间范围: {START_DATE} ~ {END_DATE}")
        log.info(f"可用数据源: {self.source_names}")
        log.info(f"存储目录: {DATA_DIR.absolute()}")
        log.info(f"{'='*60}\n")

        start_time = time.time()

        for i, code in enumerate(stock_list, 1):
            log.info(f"\n--- [{i}/{total}] {code} ---")
            self.fetch_one_stock(code)

            # 每20只股票打印一次进度
            if i % 20 == 0:
                elapsed = time.time() - start_time
                rate = i / elapsed * 60  # 股票/分钟
                eta = (total - i) / (rate / 60) if rate > 0 else 0
                log.info(f"\n📊 进度: {i}/{total} ({i/total*100:.0f}%) "
                         f"| 速度: {rate:.1f}只/分 | ETA: {eta/60:.0f}分钟")

        # 重试失败的
        if self.failed_stocks:
            log.info(f"\n🔁 重试 {len(self.failed_stocks)} 只失败股票...")
            retry_list = self.failed_stocks.copy()
            self.failed_stocks.clear()
            for code in retry_list:
                time.sleep(5)  # 重试前多等一会
                self.fetch_one_stock(code)

        self._print_summary(total, time.time() - start_time)

    def _print_summary(self, total: int, elapsed: float):
        """打印汇总"""
        log.info(f"\n{'='*60}")
        log.info(f"拉取完成！耗时: {elapsed/60:.1f} 分钟")
        log.info(f"{'='*60}")

        for name, s in self.stats.items():
            log.info(f"  {name}: 成功 {s['success']} | 失败 {s['fail']} | 总行数 {s['rows']:,}")

        if self.failed_stocks:
            log.warning(f"\n⚠️  最终失败 {len(self.failed_stocks)} 只:")
            for code in self.failed_stocks:
                log.warning(f"    {code}")

        # 统计已保存数据
        parquet_files = list(DATA_DIR.glob("*.parquet"))
        total_size = sum(f.stat().st_size for f in parquet_files)
        log.info(f"\n📁 已保存 {len(parquet_files)} 个文件, 总大小 {total_size/1024/1024:.1f} MB")


# ============================================================
# UTILITY — 数据质量检查
# ============================================================

def check_data_quality():
    """检查已拉取数据的完整性"""
    parquet_files = sorted(DATA_DIR.glob("*.parquet"))
    if not parquet_files:
        print("没有找到任何数据文件")
        return

    print(f"\n{'股票代码':<15} {'数据源':<10} {'起始日期':<12} {'结束日期':<12} "
          f"{'总行数':>8} {'交易日数':>8} {'状态'}")
    print("-" * 80)

    expected_days = pd.bdate_range(START_DATE, END_DATE).shape[0]

    for f in parquet_files:
        df = pd.read_parquet(f)
        if df.empty:
            continue
        code = df["code"].iloc[0] if "code" in df.columns else f.stem
        source = df["source"].iloc[0] if "source" in df.columns else "unknown"
        df["datetime"] = pd.to_datetime(df["datetime"])
        min_dt = df["datetime"].min().strftime("%Y-%m-%d")
        max_dt = df["datetime"].max().strftime("%Y-%m-%d")
        n_days = df["datetime"].dt.date.nunique()
        # 5min线每天48根（9:30-15:00），估算完整度
        completeness = n_days / expected_days
        status = "✅" if completeness > 0.85 else "⚠️" if completeness > 0.5 else "❌"

        print(f"{code:<15} {source:<10} {min_dt:<12} {max_dt:<12} "
              f"{len(df):>8,} {n_days:>8} {status}")


# ============================================================
# MAIN
# ============================================================

def main():
    log.info("P6+ 分钟级数据拉取器启动")

    # Step 1: 获取候选池
    try:
        ts_src = TushareSource(TUSHARE_TOKEN)
        stock_list = get_candidate_pool(ts_src)
    except Exception:
        log.warning("tushare不可用，使用备用股票列表")
        stock_list = FALLBACK_STOCK_LIST

    if not stock_list:
        log.error("候选池为空，退出")
        return

    # Step 2: 轮转拉取
    fetcher = RotationFetcher()
    fetcher.fetch_all(stock_list)

    # Step 3: 数据质量检查
    log.info("\n数据质量检查:")
    check_data_quality()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="P6+ 分钟级数据拉取器")
    parser.add_argument("--check", action="store_true", help="只检查已有数据质量")
    parser.add_argument("--freq", default="5min", help="分钟级别: 1min/5min/15min/30min/60min")
    parser.add_argument("--start", default=START_DATE, help="起始日期 YYYY-MM-DD")
    parser.add_argument("--end", default=END_DATE, help="结束日期 YYYY-MM-DD")
    args = parser.parse_args()

    if args.check:
        check_data_quality()
    else:
        FREQ = args.freq
        START_DATE = args.start
        END_DATE = args.end
        main()
