"""Daily price ETL — A股日线行情（未复权）。"""
import logging
import time
from datetime import date

import pandas as pd

from etl.base import BaseETL
from etl.tushare_client import TushareClient

logger = logging.getLogger(__name__)


class DailyPriceETL(BaseETL):
    """按日期拉取全市场日线行情。"""

    COLUMNS = [
        "ts_code", "trade_date", "open", "high", "low", "close",
        "pre_close", "change", "pct_chg", "volume", "amount",
    ]
    CONFLICT_KEYS = ["ts_code", "trade_date"]

    def __init__(self, db_conn, client: TushareClient, **kwargs):
        super().__init__(table_name="daily_price", db_conn=db_conn, **kwargs)
        self.client = client

    def transform(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.rename(columns={"vol": "volume"})
        df["trade_date"] = pd.to_datetime(df["trade_date"])
        return df[self.COLUMNS]

    def fetch_date(self, trade_date: str) -> pd.DataFrame:
        """拉取单日全市场数据。trade_date 格式：YYYYMMDD。"""
        raw = self.client.query("daily", trade_date=trade_date)
        if raw.empty:
            return raw
        return self.transform(raw)

    def run(self, data_date: date | None = None):
        start_time = time.time()
        total_fetched = 0
        total_inserted = 0

        try:
            if data_date:
                dates = [data_date]
            else:
                dates = self.detect_gaps()
                if not dates:
                    dates = [date.today()]

            for d in dates:
                date_str = d.strftime("%Y%m%d")
                df = self.fetch_date(date_str)
                if not df.empty:
                    inserted = self.upsert(df, self.COLUMNS, self.CONFLICT_KEYS)
                    total_fetched += len(df)
                    total_inserted += inserted
                    logger.info("Daily price %s: %d rows", date_str, inserted)

            duration = time.time() - start_time
            self.log_result(
                data_date=data_date or (dates[-1] if dates else date.today()),
                status="SUCCESS",
                rows_fetched=total_fetched,
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
