"""Tushare Pro API client with retry and rate limiting."""
import time
import logging
import tushare as ts
import pandas as pd

logger = logging.getLogger(__name__)


class TushareClient:
    """Tushare API wrapper with automatic retry."""

    def __init__(self, token: str):
        self._pro = ts.pro_api(token)

    def query(
        self,
        api_name: str,
        retry_wait: list[int] | None = None,
        **kwargs,
    ) -> pd.DataFrame:
        """调用 Tushare 接口，失败时自动重试。

        Args:
            api_name: Tushare 接口名（如 'daily', 'trade_cal'）
            retry_wait: 每次重试前等待秒数列表，长度即最大重试次数
            **kwargs: 传给 Tushare 接口的参数
        """
        if retry_wait is None:
            retry_wait = [30, 60, 120]

        api_func = getattr(self._pro, api_name)
        last_error = None

        for attempt in range(len(retry_wait) + 1):
            try:
                result = api_func(**kwargs)
                if result is not None and not result.empty:
                    logger.info(
                        "Tushare %s: %d rows fetched", api_name, len(result)
                    )
                return result if result is not None else pd.DataFrame()
            except Exception as e:
                last_error = e
                if attempt < len(retry_wait):
                    wait = retry_wait[attempt]
                    logger.warning(
                        "Tushare %s failed (attempt %d/%d): %s. Retry in %ds",
                        api_name, attempt + 1, len(retry_wait) + 1, e, wait,
                    )
                    time.sleep(wait)

        raise last_error
