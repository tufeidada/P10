"""A股盘中数据拉取 — pytdx 15分钟线 + 实时盘口。

用法:
    python -m data.pipeline.intraday_pull --symbols 600519.SH,000001.SZ
    python -m data.pipeline.intraday_pull --universe
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
from typing import Any

import structlog
from pytdx.hq import TdxHq_API

from db.connection import db_execute_many, db_query, init_pool, close_pool

logger = structlog.get_logger(__name__)

# TDX 服务器池（主服务器 + 备用）
TDX_SERVERS: list[tuple[str, int]] = [
    ("124.71.187.122", 7709),
    ("60.28.23.80",    7709),
    ("120.79.159.54",  7709),
    ("115.238.90.165", 7709),
    ("119.147.212.81", 7711),
]

# pytdx 批量行情最大 symbol 数
_QUOTE_BATCH_SIZE = 80

# 每次拉取的 K 线条数
_DEFAULT_BAR_COUNT = 40


class IntradayPuller:
    """A股盘中数据拉取器，基于 pytdx TDX 协议。

    所有 pytdx 调用为同步 I/O，通过 asyncio.to_thread() 包装避免阻塞事件循环。

    Attributes:
        _api: 当前 TdxHq_API 实例（已连接）。
        _server_idx: 当前使用的服务器索引。
    """

    def __init__(self) -> None:
        self._api: TdxHq_API | None = None
        self._server_idx: int = 0

    # ------------------------------------------------------------------
    # 内部连接管理
    # ------------------------------------------------------------------

    def _get_api(self) -> TdxHq_API:
        """获取已连接的 API 实例，连接失败时自动轮换服务器。

        尝试最多 3 台服务器，每台服务器若返回 falsy 则换下一台。

        Returns:
            已成功连接的 TdxHq_API 实例。

        Raises:
            ConnectionError: 所有可用服务器均连接失败。
        """
        attempts = min(3, len(TDX_SERVERS))
        for _ in range(attempts):
            host, port = TDX_SERVERS[self._server_idx % len(TDX_SERVERS)]
            try:
                api = TdxHq_API(auto_retry=False)
                result = api.connect(host, port)
                if result:
                    self._api = api
                    logger.debug(
                        "tdx_connected",
                        host=host,
                        port=port,
                        server_idx=self._server_idx,
                    )
                    return api
                logger.warning("tdx_connect_falsy", host=host, port=port)
            except Exception as exc:
                logger.warning("tdx_connect_error", host=host, port=port, error=str(exc))
            # 换下一台服务器
            self._server_idx += 1

        raise ConnectionError(
            f"All {attempts} TDX servers failed. Last idx={self._server_idx}"
        )

    def _disconnect(self) -> None:
        """安全断开当前 API 连接。"""
        if self._api is not None:
            try:
                self._api.disconnect()
            except Exception as exc:
                logger.warning("tdx_disconnect_error", error=str(exc))
            finally:
                self._api = None

    # ------------------------------------------------------------------
    # symbol 解析
    # ------------------------------------------------------------------

    def _symbol_to_market_code(self, symbol: str) -> tuple[int, str]:
        """将带后缀的 symbol 转换为 (market_id, 6位代码)。

        Args:
            symbol: 形如 '600519.SH' 或 '000001.SZ' 的 symbol 字符串。

        Returns:
            (market_id, bare_code) 元组，market_id: 1=SH, 0=SZ。

        Raises:
            ValueError: symbol 格式不合法或市场后缀未知。
        """
        parts = symbol.upper().split(".")
        if len(parts) != 2:
            raise ValueError(f"Invalid symbol format: {symbol!r}")
        code, market = parts
        if market == "SH":
            return 1, code
        if market == "SZ":
            return 0, code
        raise ValueError(f"Unknown market suffix: {market!r} in symbol {symbol!r}")

    # ------------------------------------------------------------------
    # 15 分钟 K 线
    # ------------------------------------------------------------------

    async def pull_15m_bars(
        self, symbol: str, count: int = _DEFAULT_BAR_COUNT
    ) -> list[dict[str, Any]]:
        """拉取最近 N 根 15 分钟 K 线。

        pytdx category=1 对应 15 分钟线。连续失败时自动轮换服务器，最多重试 3 次。

        Args:
            symbol: 股票代码，如 '600519.SH'。
            count: 拉取根数，默认 40（约 10 小时）。

        Returns:
            标准化 dict 列表（按时间升序），每个 dict 包含：
            bar_time, open, high, low, close, volume (股), amount (元)。
            返回空列表表示无数据。
        """
        market_id, code = self._symbol_to_market_code(symbol)
        log = logger.bind(symbol=symbol, market=market_id, code=code)

        for attempt in range(3):
            try:
                api = await asyncio.to_thread(self._get_api)
                raw: list[dict] | None = await asyncio.to_thread(
                    api.get_security_bars,
                    1,          # category=1 → 15min
                    market_id,
                    code,
                    0,          # offset=0（最新）
                    count,
                )
                if raw is None:
                    log.warning("tdx_bars_none", attempt=attempt)
                    self._disconnect()
                    self._server_idx += 1
                    continue

                bars = [self._normalize_bar(r) for r in raw]
                # pytdx 返回从旧到新，保持升序
                bars.sort(key=lambda b: b["bar_time"])
                log.debug("tdx_bars_pulled", count=len(bars))
                return bars

            except Exception as exc:
                log.warning("tdx_bars_error", attempt=attempt, error=str(exc))
                self._disconnect()
                self._server_idx += 1

        log.error("tdx_bars_failed_all_retries")
        return []

    @staticmethod
    def _normalize_bar(raw: dict[str, Any]) -> dict[str, Any]:
        """将 pytdx 原始 bar dict 标准化为业务字段。

        Args:
            raw: pytdx get_security_bars 返回的单根 K 线 dict。

        Returns:
            标准化 dict，含 bar_time (datetime), open/high/low/close (float),
            volume (int, 股), amount (float, 元)。
        """
        # pytdx bar 包含 datetime 字符串或直接字段
        dt_raw = raw.get("datetime")
        if dt_raw:
            # 格式通常是 '2026-04-16 09:45'
            try:
                bar_time = datetime.strptime(str(dt_raw).strip(), "%Y-%m-%d %H:%M")
            except ValueError:
                bar_time = datetime.strptime(str(dt_raw).strip(), "%Y-%m-%d %H:%M:%S")
        else:
            bar_time = datetime(
                year=int(raw["year"]),
                month=int(raw["month"]),
                day=int(raw["day"]),
                hour=int(raw["hour"]),
                minute=int(raw["minute"]),
            )

        return {
            "bar_time": bar_time,
            "open":     float(raw["open"]),
            "high":     float(raw["high"]),
            "low":      float(raw["low"]),
            "close":    float(raw["close"]),
            "volume":   int(raw["vol"]),     # 单位：股（已是股数）
            "amount":   float(raw["amount"]),
        }

    # ------------------------------------------------------------------
    # 实时盘口
    # ------------------------------------------------------------------

    async def pull_quotes(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        """批量拉取多支股票的实时行情快照。

        每批最多 80 个 symbol（pytdx 限制），自动分批。

        Args:
            symbols: symbol 列表，如 ['600519.SH', '000001.SZ']。

        Returns:
            {symbol: quote_dict} 映射，quote_dict 包含：
            price, open, high, low, vol, amount, s_vol, b_vol,
            bid1..5, ask1..5, bid_vol1..5, ask_vol1..5。
        """
        if not symbols:
            return {}

        # 转换为 (market_id, code) 元组列表，保持 symbol 映射关系
        mkt_code_list: list[tuple[int, str]] = []
        idx_to_symbol: list[str] = []
        for sym in symbols:
            try:
                mid, code = self._symbol_to_market_code(sym)
                mkt_code_list.append((mid, code))
                idx_to_symbol.append(sym)
            except ValueError as exc:
                logger.warning("pull_quotes_skip", symbol=sym, error=str(exc))

        result: dict[str, dict[str, Any]] = {}

        # 分批处理
        for batch_start in range(0, len(mkt_code_list), _QUOTE_BATCH_SIZE):
            batch_mc = mkt_code_list[batch_start: batch_start + _QUOTE_BATCH_SIZE]
            batch_syms = idx_to_symbol[batch_start: batch_start + _QUOTE_BATCH_SIZE]

            try:
                api = await asyncio.to_thread(self._get_api)
                raw_quotes: list[dict] | None = await asyncio.to_thread(
                    api.get_security_quotes, batch_mc
                )
                if raw_quotes is None:
                    logger.warning(
                        "tdx_quotes_none",
                        batch_start=batch_start,
                        batch_size=len(batch_mc),
                    )
                    continue

                for sym, q in zip(batch_syms, raw_quotes):
                    result[sym] = dict(q)

            except Exception as exc:
                logger.warning(
                    "tdx_quotes_error",
                    batch_start=batch_start,
                    error=str(exc),
                )
                self._disconnect()
                self._server_idx += 1

        logger.debug("pull_quotes_done", total=len(result))
        return result

    # ------------------------------------------------------------------
    # 一键拉取并入库
    # ------------------------------------------------------------------

    async def pull_and_save(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        """拉取 15 分钟线 + 实时盘口，写入 intraday_bars 表。

        流程：
        1. 逐 symbol 拉取 15m K 线（最近 40 根）
        2. 按 bar 计算累计 VWAP
        3. Upsert 到 intraday_bars（interval='15m'）
        4. 批量拉取所有 symbol 的实时盘口

        Args:
            symbols: 要处理的 symbol 列表。

        Returns:
            {symbol: {'bars': 写入根数, 'quote': 盘口 dict}} 摘要。
        """
        summary: dict[str, dict[str, Any]] = {}

        # ---- Step 1-3: 拉取 K 线 + 写库 ----
        for symbol in symbols:
            log = logger.bind(symbol=symbol, module="intraday_pull")
            try:
                bars = await self.pull_15m_bars(symbol)
                if not bars:
                    summary[symbol] = {"bars": 0, "bars_list": [], "quote": None}
                    continue

                # 计算累计 VWAP：cumsum(amount) / cumsum(volume)
                cum_amount = 0.0
                cum_volume = 0
                for bar in bars:
                    cum_amount += bar["amount"]
                    cum_volume += bar["volume"]
                    bar["vwap"] = (
                        round(cum_amount / cum_volume, 4) if cum_volume > 0 else None
                    )

                # 组装 upsert 参数元组
                records: list[tuple] = []
                for bar in bars:
                    records.append((
                        symbol,
                        "CN",
                        bar["bar_time"],
                        "15m",
                        bar["open"],
                        bar["high"],
                        bar["low"],
                        bar["close"],
                        bar["volume"],
                        bar["amount"],
                        bar["vwap"],
                    ))

                upsert_sql = """
                    INSERT INTO intraday_bars
                        (symbol, market, bar_time, interval,
                         open, high, low, close, volume, amount, vwap)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                    ON CONFLICT (symbol, bar_time, interval)
                    DO UPDATE SET
                        open   = EXCLUDED.open,
                        high   = EXCLUDED.high,
                        low    = EXCLUDED.low,
                        close  = EXCLUDED.close,
                        volume = EXCLUDED.volume,
                        amount = EXCLUDED.amount,
                        vwap   = EXCLUDED.vwap
                """
                await db_execute_many(upsert_sql, records)
                summary[symbol] = {"bars": len(records), "bars_list": bars, "quote": None}
                log.info("intraday_bars_saved", count=len(records))

            except Exception as exc:
                log.error("pull_and_save_error", symbol=symbol, error=str(exc))
                summary[symbol] = {"bars": 0, "bars_list": [], "quote": None}

        # ---- Step 4: 批量拉取盘口 ----
        try:
            quotes = await self.pull_quotes(symbols)
            for symbol, quote in quotes.items():
                if symbol in summary:
                    summary[symbol]["quote"] = quote
        except Exception as exc:
            logger.error("pull_quotes_batch_error", error=str(exc))

        return summary

    # ------------------------------------------------------------------
    # 候选池查询
    # ------------------------------------------------------------------

    async def get_active_universe(self) -> list[str]:
        """从 stock_universe 读取活跃 CN 股票列表。

        Returns:
            symbol 字符串列表，如 ['600519.SH', '000001.SZ', ...]。
        """
        rows = await db_query(
            "SELECT symbol FROM stock_universe WHERE market = 'CN' AND active = TRUE"
        )
        return [r["symbol"] for r in rows]


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------

async def _main() -> None:
    """CLI 主函数。"""
    parser = argparse.ArgumentParser(description="A股盘中数据拉取")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--symbols",
        type=str,
        help="逗号分隔的 symbol 列表，如 600519.SH,000001.SZ",
    )
    group.add_argument(
        "--universe",
        action="store_true",
        help="拉取 stock_universe 中所有活跃 CN 股票",
    )
    args = parser.parse_args()

    await init_pool()
    puller = IntradayPuller()

    try:
        if args.symbols:
            symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
        else:
            symbols = await puller.get_active_universe()
            logger.info("universe_loaded", count=len(symbols))

        if not symbols:
            logger.warning("no_symbols_to_pull")
            return

        summary = await puller.pull_and_save(symbols)
        total_bars = sum(v["bars"] for v in summary.values())
        total_quotes = sum(1 for v in summary.values() if v.get("quote"))
        logger.info(
            "pull_complete",
            symbols=len(summary),
            total_bars=total_bars,
            total_quotes=total_quotes,
        )
    finally:
        puller._disconnect()
        await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
