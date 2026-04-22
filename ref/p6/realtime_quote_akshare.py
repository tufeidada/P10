"""实时行情获取 — 优先Tushare，备选akshare。"""
import os, logging

log = logging.getLogger(__name__)


def get_realtime_quotes(codes: list) -> dict:
    """
    获取实时行情。返回 {ts_code: {price, change, pct_change, high, low, volume, name, time}}
    非交易时段返回最近收盘价。
    """
    result = {}

    # 方案1: Tushare
    try:
        result = _tushare_realtime(codes)
        if result:
            return result
    except Exception as e:
        log.warning(f"Tushare实时行情失败: {e}")

    # 方案2: akshare
    try:
        result = _akshare_realtime(codes)
        if result:
            return result
    except Exception as e:
        log.warning(f"akshare实时行情失败: {e}")

    return result


def _tushare_realtime(codes: list) -> dict:
    """Tushare realtime_quote 逐只查询。"""
    import tushare as ts
    from dotenv import load_dotenv
    load_dotenv()
    token = os.getenv("TUSHARE_TOKEN")
    if not token:
        return {}
    ts.set_token(token)

    result = {}
    for code in codes:
        try:
            df = ts.realtime_quote(ts_code=code)
            if df is not None and not df.empty:
                row = df.iloc[0]
                price = float(row.get("PRICE", 0))
                pre_close = float(row.get("PRE_CLOSE", 0))
                change = price - pre_close if pre_close > 0 else 0
                pct = change / pre_close if pre_close > 0 else 0
                result[code] = {
                    "price": price,
                    "change": round(change, 2),
                    "pct_change": round(pct * 100, 2),
                    "high": float(row.get("HIGH", 0)),
                    "low": float(row.get("LOW", 0)),
                    "open": float(row.get("OPEN", 0)),
                    "pre_close": pre_close,
                    "volume": float(row.get("VOLUME", 0)),
                    "amount": float(row.get("AMOUNT", 0)),
                    "name": str(row.get("NAME", "")),
                    "time": str(row.get("TIME", "")),
                    "source": "tushare",
                }
        except Exception as e:
            log.debug(f"Tushare {code} 失败: {e}")
    return result


def _akshare_realtime(codes: list) -> dict:
    """akshare 东方财富全市场实时行情。"""
    import akshare as ak
    df = ak.stock_zh_a_spot_em()
    if df is None or df.empty:
        return {}

    # 转换代码格式: akshare用纯数字, 我们用 XXXXXX.SZ/SH
    code_set = set(codes)
    result = {}
    for _, row in df.iterrows():
        ak_code = str(row.get("代码", ""))
        # 转换为 ts_code 格式
        if ak_code.startswith("6") or ak_code.startswith("9"):
            ts_code = ak_code + ".SH"
        else:
            ts_code = ak_code + ".SZ"

        if ts_code in code_set:
            price = float(row.get("最新价", 0))
            pre = float(row.get("昨收", 0))
            result[ts_code] = {
                "price": price,
                "change": round(float(row.get("涨跌额", 0)), 2),
                "pct_change": round(float(row.get("涨跌幅", 0)), 2),
                "high": float(row.get("最高", 0)),
                "low": float(row.get("最低", 0)),
                "open": float(row.get("今开", 0)),
                "pre_close": pre,
                "volume": float(row.get("成交量", 0)),
                "amount": float(row.get("成交额", 0)),
                "name": str(row.get("名称", "")),
                "time": "",
                "source": "akshare",
            }
    return result
