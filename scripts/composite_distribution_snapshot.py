"""
Composite 分布快照 — 每天 16:35 BJT (CN) + 07:05 BJT (US) 各跑一次。

统计昨天+今天的 judgments，输出到 reports/composite_snapshot_YYYYMMDD.csv
并推送 Telegram 简短摘要。用于 3 天观察期判断是否需要调整阈值/公式。

用法（手动触发）：
    python scripts/composite_distribution_snapshot.py
    python scripts/composite_distribution_snapshot.py --market CN
"""

from __future__ import annotations

import asyncio
import csv
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import structlog
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
)

logger = structlog.get_logger(__name__)

REPORTS_DIR = Path(__file__).resolve().parent.parent / "reports"


async def _snapshot_one_market(market: str, today: date, yesterday: date) -> dict:
    """统计单个市场的 composite 分布。"""
    from db.connection import db_query

    rows = await db_query(
        """
        SELECT
            composite_score,
            rule_signal_strength,
            direction,
            confidence,
            judgment_date
        FROM judgments
        WHERE market = $1
          AND judgment_date >= $2
          AND judgment_date <= $3
          AND composite_score IS NOT NULL
        ORDER BY judgment_date DESC, composite_score DESC
        """,
        market, yesterday, today,
    )

    if not rows:
        return {"market": market, "count": 0}

    scores = sorted(float(r["composite_score"]) for r in rows)
    n = len(scores)

    def pct(p: float) -> float:
        idx = (n - 1) * p / 100
        lo, hi = int(idx), min(int(idx) + 1, n - 1)
        return round(scores[lo] + (scores[hi] - scores[lo]) * (idx - lo), 2)

    # signal_strength counts
    sig_counts: dict[str, int] = {}
    for r in rows:
        k = r["rule_signal_strength"] or "null"
        sig_counts[k] = sig_counts.get(k, 0) + 1

    # direction counts
    dir_counts: dict[str, int] = {}
    for r in rows:
        k = r["direction"] or "null"
        dir_counts[k] = dir_counts.get(k, 0) + 1

    return {
        "market": market,
        "count": n,
        "composite_min": scores[0],
        "composite_p25": pct(25),
        "composite_p50": pct(50),
        "composite_p75": pct(75),
        "composite_max": scores[-1],
        "strong_buy": sig_counts.get("strong_buy", 0),
        "buy": sig_counts.get("buy", 0),
        "hold": sig_counts.get("hold", 0),
        "sell": sig_counts.get("sell", 0),
        "strong_sell": sig_counts.get("strong_sell", 0),
        "bullish": dir_counts.get("bullish", 0),
        "neutral": dir_counts.get("neutral", 0),
        "bearish": dir_counts.get("bearish", 0),
    }


async def _get_regime(market: str, today: date) -> str:
    """取最近一条 regime_mode。"""
    from db.connection import db_query_one

    row = await db_query_one(
        """
        SELECT regime_mode FROM regime_daily
        WHERE market = $1 AND trade_date <= $2
        ORDER BY trade_date DESC LIMIT 1
        """,
        market, today,
    )
    return row["regime_mode"] if row else "unknown"


async def run_snapshot(market: str | None = None) -> None:
    """拉取分布数据、写 CSV、推 Telegram。"""
    from bot.telegram_bot import TelegramPusher

    today = date.today()
    yesterday = today - timedelta(days=1)
    markets = [market] if market else ["CN", "US"]

    stats_list = []
    for mkt in markets:
        stat = await _snapshot_one_market(mkt, today, yesterday)
        stat["regime"] = await _get_regime(mkt, today)
        stat["snapshot_date"] = today.isoformat()
        stats_list.append(stat)
        logger.info("snapshot_done", **{k: v for k, v in stat.items() if k != "snapshot_date"})

    # 写 CSV
    REPORTS_DIR.mkdir(exist_ok=True)
    csv_path = REPORTS_DIR / f"composite_snapshot_{today.strftime('%Y%m%d')}.csv"
    fieldnames = [
        "snapshot_date", "market", "count", "regime",
        "composite_min", "composite_p25", "composite_p50", "composite_p75", "composite_max",
        "strong_buy", "buy", "hold", "sell", "strong_sell",
        "bullish", "neutral", "bearish",
    ]

    file_exists = csv_path.exists()
    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        writer.writerows(stats_list)

    logger.info("snapshot_csv_written", path=str(csv_path), rows=len(stats_list))

    # Telegram 推送
    lines = [f"📊 <b>Composite 分布快照</b>  {today.isoformat()}\n"]
    for s in stats_list:
        if s.get("count", 0) == 0:
            lines.append(f"<b>{s['market']}</b>  暂无数据")
            continue
        lines.append(
            f"<b>{s['market']}</b>  Regime: <code>{s['regime']}</code>  N={s['count']}\n"
            f"  Composite: {s['composite_min']:.1f} / {s['composite_p25']:.1f} / "
            f"{s['composite_p50']:.1f} / {s['composite_p75']:.1f} / {s['composite_max']:.1f}"
            f"  <i>(min/p25/p50/p75/max)</i>\n"
            f"  信号: 强买={s.get('strong_buy',0)} 买={s.get('buy',0)} "
            f"持={s.get('hold',0)} 卖={s.get('sell',0)} 强卖={s.get('strong_sell',0)}\n"
            f"  方向: 多={s.get('bullish',0)} 中={s.get('neutral',0)} 空={s.get('bearish',0)}"
        )
    msg = "\n".join(lines)

    try:
        await TelegramPusher().send(msg)
    except Exception as e:
        logger.warning("snapshot_telegram_failed", error=str(e))


async def _main() -> None:
    import argparse
    from db.connection import init_pool, close_pool

    parser = argparse.ArgumentParser(description="Composite 分布快照")
    parser.add_argument("--market", choices=["CN", "US"], help="只统计指定市场（默认 CN+US）")
    args = parser.parse_args()

    await init_pool()
    try:
        await run_snapshot(market=args.market)
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(_main())
