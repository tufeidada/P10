"""
M7 独立验证入口 — 手动运行 composite 分析并写入 judgments 表。

用法：
    python scripts/run_composite_once.py --symbol 600519.SH --market CN
    python scripts/run_composite_once.py --symbol AAPL --market US
    python scripts/run_composite_once.py --symbol 600519.SH --market CN --dry-run
    python scripts/run_composite_once.py --all-active --market CN
    python scripts/run_composite_once.py --all-active --dry-run          # 跑全部 48 只，不调 LLM
    python scripts/run_composite_once.py --all-active --date 2026-04-21  # 指定日期

不接入 scheduler，仅用于验证和手动触发。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date
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


def _fmt_result(r, save_id: int | None) -> str:
    """格式化单条分析结果为可读文本。"""
    emoji = {"bullish": "🟢", "bearish": "🔴", "neutral": "🟡"}.get(r.direction, "⚪")
    dry = " [DRY]" if r.logic_text and r.logic_text.startswith("[DRY RUN]") else ""
    ss = r.signal_sources or {}
    llm_dir = ss.get("llm_direction", "unknown")
    llm_sig = ss.get("llm_signal_strength", "unknown")
    llm_reasoning = ss.get("llm_reasoning", "")
    lines = [
        f"{emoji} {r.symbol} ({r.market}){dry}",
        f"   综合: {r.composite_score:.1f}/100  规则: {r.direction}/{r.rule_signal_strength}  置信度: {r.confidence:.2f}",
        f"   技术: {r.technical_score:.1f}  基本面: {r.fundamental_score or 50.0:.1f}"
        f"  资金: {r.flow_score or 50.0:.1f}  情绪: {r.sentiment_score or 50.0:.1f}",
        f"   LLM方向: {llm_dir}  LLM信号: {llm_sig}",
    ]
    if llm_reasoning:
        lines.append(f"   LLM理由: {llm_reasoning[:120]}")
    if r.logic_text and not r.logic_text.startswith("[DRY RUN]"):
        preview = r.logic_text[:120].replace("\n", " ")
        lines.append(f"   叙事: {preview}…" if len(r.logic_text) > 120 else f"   叙事: {r.logic_text}")
    if save_id:
        lines.append(f"   ✅ 写入 judgments id={save_id}")
    return "\n".join(lines)


async def run_single(
    symbol: str,
    market: str,
    trade_date: date,
    dry_run: bool,
    save: bool,
) -> bool:
    """分析单只股票并可选写入 DB。

    Returns:
        成功返回 True。
    """
    from core.analysis.composite import CompositeAnalyzer

    analyzer = CompositeAnalyzer()
    try:
        result = await analyzer.analyze(symbol, market, trade_date, dry_run=dry_run)
        save_id = None
        if save:
            save_id = await analyzer.save_judgment(result)
        print(_fmt_result(result, save_id))
        return True
    except Exception as e:
        print(f"❌ {symbol} ({market}) 失败: {type(e).__name__}: {e}")
        return False


async def run_all_active(
    market: str | None,
    trade_date: date,
    dry_run: bool,
    save: bool,
) -> None:
    """分析全部 active 股票。"""
    from db.connection import db_query
    from core.analysis.composite import CompositeAnalyzer

    where_market = "AND market = $1" if market else ""
    args = [market] if market else []
    rows = await db_query(
        f"SELECT symbol, market FROM stock_universe WHERE active = TRUE {where_market} ORDER BY market, symbol",
        *args,
    )
    if not rows:
        print("⚠️ 没有 active 股票")
        return

    print(f"\n{'='*60}")
    print(f"  composite_once —— {len(rows)} 只  date={trade_date}  dry_run={dry_run}")
    print(f"{'='*60}\n")

    analyzer = CompositeAnalyzer()
    success = failed = 0
    for row in rows:
        sym, mkt = row["symbol"], row["market"]
        try:
            result = await analyzer.analyze(sym, mkt, trade_date, dry_run=dry_run)
            save_id = None
            if save:
                save_id = await analyzer.save_judgment(result)
            print(_fmt_result(result, save_id))
            success += 1
        except Exception as e:
            print(f"❌ {sym} ({mkt}) 失败: {type(e).__name__}: {e}")
            failed += 1
        print()

    print(f"{'='*60}")
    print(f"  结果: {success} 成功 / {failed} 失败 / 共 {len(rows)} 只")
    if not dry_run:
        # 显示当日 LLM 成本汇总
        try:
            from db.connection import db_query_val
            total_cost = await db_query_val(
                "SELECT COALESCE(SUM(cost_cny), 0) FROM llm_cost_log WHERE DATE(call_time) = $1",
                trade_date,
            )
            print(f"  LLM 当日成本: ¥{float(total_cost):.4f}")
        except Exception:
            pass
    print()


async def _main(args: argparse.Namespace) -> int:
    from db.connection import init_pool, close_pool

    trade_date = (
        date.fromisoformat(args.date) if args.date else date.today()
    )
    dry_run: bool = args.dry_run
    save: bool = not args.no_save

    await init_pool()
    try:
        if args.all_active:
            await run_all_active(
                market=args.market if args.market else None,
                trade_date=trade_date,
                dry_run=dry_run,
                save=save,
            )
            return 0
        else:
            if not args.symbol or not args.market:
                print("错误：--symbol 和 --market 必须同时指定，或使用 --all-active")
                return 1
            ok = await run_single(args.symbol, args.market, trade_date, dry_run, save)
            return 0 if ok else 1
    finally:
        await close_pool()


def main() -> None:
    parser = argparse.ArgumentParser(description="运行一次 composite 分析")
    parser.add_argument("--symbol", help="证券代码，如 600519.SH")
    parser.add_argument("--market", help="市场: CN 或 US")
    parser.add_argument("--all-active", action="store_true", help="跑全部 active 股票")
    parser.add_argument("--date", help="分析日期 YYYY-MM-DD，默认今天")
    parser.add_argument("--dry-run", action="store_true", help="跳过 LLM，不烧钱")
    parser.add_argument("--no-save", action="store_true", help="不写入 judgments 表")
    args = parser.parse_args()
    sys.exit(asyncio.run(_main(args)))


if __name__ == "__main__":
    main()
