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

    # 追加 YAML 段到 DAILY_LOG.md（自动部分）— 见 AI_PLAYBOOK 约定
    try:
        await _append_daily_log(stats_list, today)
    except Exception as e:
        logger.warning("daily_log_append_failed", error=str(e))


async def _append_daily_log(stats_list: list[dict], today: date) -> None:
    """把 snapshot 结果以 YAML block 追加到 DAILY_LOG.md。"""
    from db.connection import db_query

    daily_log = Path(__file__).resolve().parent.parent / "DAILY_LOG.md"
    if not daily_log.exists():
        logger.info("daily_log_not_found", path=str(daily_log))
        return

    # 每天最多写一次（同日重复运行不重复追加）
    existing = daily_log.read_text(encoding="utf-8")
    date_header = f"## {today.isoformat()}"
    if f"\n{date_header} " in existing or f"\n{date_header}\n" in existing:
        logger.info("daily_log_already_has_today", date=today.isoformat())
        return

    # 收集每个市场 top 多/空方
    by_market: dict[str, dict] = {}
    for s in stats_list:
        m = s["market"]
        rows = await db_query(
            "SELECT symbol, composite_score, rule_signal_strength FROM judgments "
            "WHERE judgment_date=$1 AND market=$2 ORDER BY composite_score DESC",
            today, m,
        )
        by_market[m] = {
            "regime": s.get("regime", "unknown"),
            "count": s.get("count", 0),
            "signals": {
                "strong_buy": [r["symbol"] for r in rows if r["rule_signal_strength"] == "strong_buy"],
                "buy": [r["symbol"] for r in rows if r["rule_signal_strength"] == "buy"],
                "weak_buy": [r["symbol"] for r in rows if r["rule_signal_strength"] == "weak_buy"],
                "weak_sell": [r["symbol"] for r in rows if r["rule_signal_strength"] == "weak_sell"],
                "sell": [r["symbol"] for r in rows if r["rule_signal_strength"] == "sell"],
                "strong_sell": [r["symbol"] for r in rows if r["rule_signal_strength"] == "strong_sell"],
            },
        }

    weekday_cn = ["一", "二", "三", "四", "五", "六", "日"][today.weekday()]
    block_lines = [
        "",
        f"{date_header} (周{weekday_cn})",
        "",
        "```yaml",
        "auto:",
    ]
    for mkt in ("CN", "US"):
        if mkt not in by_market:
            continue
        d = by_market[mkt]
        block_lines.append(f"  {mkt.lower()}_candidates_analyzed: {d['count']}")
        block_lines.append(f"  regime_{mkt.lower()}: {d['regime']}")
        block_lines.append(f"  {mkt.lower()}_signals:")
        for sig in ("strong_buy", "buy", "weak_buy", "weak_sell", "sell", "strong_sell"):
            symbols = d["signals"][sig]
            block_lines.append(f"    {sig}: {symbols if symbols else '[]'}")
    block_lines += [
        "```",
        "",
        "### 我看了什么 / 关注什么（人工补）",
        "- _待补_",
        "",
        "### 反思 / 待跟进",
        "- [ ] _待补_",
        "",
        "---",
    ]
    new_block = "\n".join(block_lines)

    # 插入到 "<!-- 新的一天追加在上面" 标记前；如果找不到就追加末尾
    marker = "<!-- 新的一天追加在上面"
    if marker in existing:
        # 在 marker 前插入新块（新日期排在顶部）
        idx = existing.rfind("---\n\n" + marker)
        if idx == -1:
            idx = existing.rfind(marker)
        new_content = existing[:idx] + new_block + "\n" + existing[idx:]
    else:
        new_content = existing.rstrip() + "\n\n" + new_block + "\n"

    daily_log.write_text(new_content, encoding="utf-8")
    logger.info("daily_log_appended", date=today.isoformat(), markets=list(by_market.keys()))


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
