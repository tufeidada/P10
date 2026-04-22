"""Daily basic ETL — 每日指标（PE/PB/市值等）。"""
import logging
import time
from datetime import date

import pandas as pd

from etl.base import BaseETL
from etl.tushare_client import TushareClient

logger = logging.getLogger(__name__)


class DailyBasicETL(BaseETL):
    """按日期拉取全市场每日指标。"""

    COLUMNS = [
        "ts_code", "trade_date",
        "turnover_rate", "turnover_rate_f", "volume_ratio",
        "pe", "pe_ttm", "pb", "ps", "ps_ttm",
        "dv_ratio", "dv_ttm",
        "total_share", "float_share", "free_share",
        "total_mv", "circ_mv",
    ]
    CONFLICT_KEYS = ["ts_code", "trade_date"]

    def __init__(self, db_conn, client: TushareClient, **kwargs):
        super().__init__(table_name="daily_basic", db_conn=db_conn, **kwargs)
        self.client = client

    def transform(self, df: pd.DataFrame) -> pd.DataFrame:
        # 只保留目标列，忽略 Tushare 返回的额外列
        available = [c for c in self.COLUMNS if c in df.columns]
        df = df[available].copy()
        df["trade_date"] = pd.to_datetime(df["trade_date"])
        # 补齐缺失列
        for c in self.COLUMNS:
            if c not in df.columns:
                df[c] = None
        return df[self.COLUMNS]

    def run(self, data_date: date | None = None):
        start_time = time.time()

        try:
            if data_date:
                dates = [data_date]
            else:
                dates = self.detect_gaps()
                if not dates:
                    dates = [date.today()]

            total_inserted = 0
            for d in dates:
                date_str = d.strftime("%Y%m%d")
                raw = self.client.query("daily_basic", trade_date=date_str)
                if not raw.empty:
                    df = self.transform(raw)
                    inserted = self.upsert(df, self.COLUMNS, self.CONFLICT_KEYS)
                    total_inserted += inserted
                    logger.info("Daily basic %s: %d rows", date_str, inserted)

            duration = time.time() - start_time
            self.log_result(
                data_date=data_date or (dates[-1] if dates else date.today()),
                status="SUCCESS",
                rows_fetched=total_inserted,
                rows_inserted=total_inserted,
                duration=duration,
            )

        except Exception as e:
            duration = time.time() - start_time
            self.log_result(
                data_date=data_date,
                status="FAILED",
                duration=duration,
                error_message=str(e),
            )
            raise
