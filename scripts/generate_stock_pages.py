"""
M9 — 为 stock_universe 中的股票生成/刷新 wiki_pages 条目。

根据 judgments 表最新判断数据生成结构化 Markdown 页面，写入 wiki_pages 表。
不依赖 LLM，纯 DB 数据格式化。

用法：
    python scripts/generate_stock_pages.py --market CN   # 仅处理 A 股
    python scripts/generate_stock_pages.py --market US   # 仅处理美股
    python scripts/generate_stock_pages.py --all         # 所有 active 股票
    python scripts/generate_stock_pages.py --symbol 002050.SZ  # 指定股票
    python scripts/generate_stock_pages.py --market CN --limit 3  # 前 N 只（测试用）
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv(".env")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import structlog

structlog.configure(
    processors=[
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(20),
)

logger = structlog.get_logger(__name__)

# ────────────────────────────────────────────────
# Markdown 生成
# ────────────────────────────────────────────────

DIRECTION_CN = {
    "bullish": "看多 📈",
    "bearish": "看空 📉",
    "neutral": "中性 ➡️",
}

ACTION_CN = {
    "buy": "买入",
    "sell": "卖出",
    "hold": "持有",
    "watch": "观察",
    "avoid": "回避",
    "reduce": "减仓",
    "add": "加仓",
}


def _score_bar(score: float | None, width: int = 20) -> str:
    """将 0-100 分数渲染为文本进度条。"""
    if score is None:
        return "N/A"
    filled = round((score / 100) * width)
    bar = "█" * filled + "░" * (width - filled)
    return f"{bar} {score:.1f}"


def _fmt_regime(regime: dict | str | None) -> str:
    if not regime:
        return "暂无"
    # asyncpg may return jsonb as a raw string; parse if needed
    if isinstance(regime, str):
        try:
            import json as _json
            regime = _json.loads(regime)
        except Exception:
            return regime[:200]
    parts = []
    for k, v in regime.items():
        if isinstance(v, (str, int, float)) and k not in ("detail", "dimension_weights"):
            parts.append(f"{k}={v}")
    return "  ".join(parts[:6]) if parts else str(regime)[:200]


def generate_page_markdown(
    symbol: str,
    market: str,
    name: str | None,
    industry: str | None,
    judgment: dict[str, Any] | None,
) -> str:
    """根据 DB 数据生成股票 wiki 页面 Markdown。"""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    display_name = name or symbol
    industry_str = industry or "未分类"

    lines: list[str] = []
    lines.append(f"# {display_name} ({symbol})")
    lines.append("")
    lines.append(f"**市场**: {market}  |  **行业**: {industry_str}  |  **更新时间**: {now_str}")
    lines.append("")
    lines.append("---")
    lines.append("")

    if judgment is None:
        lines.append("## 最新判断")
        lines.append("")
        lines.append("> ⚠️ 暂无判断数据。请运行 `run_composite_once.py` 生成分析。")
        lines.append("")
    else:
        jdate = str(judgment.get("judgment_date", "N/A"))
        direction = judgment.get("direction", "neutral")
        direction_cn = DIRECTION_CN.get(direction, direction)
        composite = judgment.get("composite_score")
        technical = judgment.get("technical_score")
        fundamental = judgment.get("fundamental_score")
        flow = judgment.get("flow_score")
        sentiment = judgment.get("sentiment_score")
        confidence = judgment.get("confidence")
        suggested = judgment.get("suggested_action")
        logic = judgment.get("logic_text")
        regime = judgment.get("regime_at_time")
        timeframe = judgment.get("timeframe", "daily")

        lines.append("## 最新判断")
        lines.append("")
        lines.append(f"| 字段 | 值 |")
        lines.append(f"|------|-----|")
        lines.append(f"| 判断日期 | {jdate} |")
        lines.append(f"| 时间框架 | {timeframe} |")
        lines.append(f"| 方向 | {direction_cn} |")
        lines.append(f"| 综合评分 | {composite:.1f}/100 |" if composite is not None else "| 综合评分 | N/A |")
        lines.append(f"| 置信度 | {confidence:.2f} |" if confidence is not None else "| 置信度 | N/A |")
        action_cn = ACTION_CN.get(suggested or "", suggested or "—")
        lines.append(f"| 建议操作 | {action_cn} |")
        lines.append("")

        lines.append("## 多维评分")
        lines.append("")
        lines.append("```")
        lines.append(f"综合面: {_score_bar(composite)}")
        lines.append(f"技术面: {_score_bar(technical)}")
        lines.append(f"基本面: {_score_bar(fundamental)}")
        lines.append(f"资金面: {_score_bar(flow)}")
        lines.append(f"情绪面: {_score_bar(sentiment)}")
        lines.append("```")
        lines.append("")

        lines.append("## 市场环境 (Regime)")
        lines.append("")
        lines.append(f"```\n{_fmt_regime(regime)}\n```")
        lines.append("")

        if logic:
            lines.append("## 分析摘要")
            lines.append("")
            # Truncate very long logic text
            preview = logic[:800] + ("..." if len(logic) > 800 else "")
            lines.append(preview)
            lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(f"*此页面由 generate_stock_pages.py 自动生成 · {now_str}*")

    return "\n".join(lines)


def build_page_path(symbol: str, market: str) -> str:
    """构建 wiki_pages 的 page_path 主键，如 stocks/CN/002050_SZ.md。"""
    safe = symbol.replace(".", "_")
    return f"stocks/{market}/{safe}.md"


def build_title(symbol: str, name: str | None, market: str) -> str:
    if name:
        return f"{name} ({symbol}) - {market}"
    return f"{symbol} - {market}"


def build_tags(market: str, direction: str | None, industry: str | None) -> list[str]:
    tags = [f"market:{market}", "type:stock"]
    if direction:
        tags.append(f"direction:{direction}")
    if industry:
        tags.append(f"industry:{industry}")
    return tags


# ────────────────────────────────────────────────
# DB 操作
# ────────────────────────────────────────────────

async def fetch_symbols(
    market: str | None,
    symbol: str | None,
) -> list[dict]:
    """从 stock_universe 获取目标股票列表。"""
    from db.connection import db_query

    if symbol:
        rows = await db_query(
            "SELECT symbol, market, name, industry FROM stock_universe WHERE symbol = $1",
            symbol,
        )
    elif market:
        rows = await db_query(
            """
            SELECT symbol, market, name, industry
            FROM stock_universe
            WHERE active = TRUE AND market = $1
            ORDER BY symbol
            """,
            market,
        )
    else:
        rows = await db_query(
            """
            SELECT symbol, market, name, industry
            FROM stock_universe
            WHERE active = TRUE
            ORDER BY market, symbol
            """
        )
    return [dict(r) for r in rows]


async def fetch_latest_judgment(symbol: str) -> dict | None:
    """获取该股票最新一条 judgment，不含 fundamental_bug_affected 条目。"""
    from db.connection import db_query_one

    row = await db_query_one(
        """
        SELECT symbol, market, judgment_date, timeframe,
               technical_score, fundamental_score, flow_score,
               sentiment_score, composite_score,
               direction, confidence, logic_text,
               suggested_action, regime_at_time
        FROM judgments
        WHERE symbol = $1
          AND (fundamental_bug_affected IS NOT TRUE)
        ORDER BY judgment_date DESC, id DESC
        LIMIT 1
        """,
        symbol,
    )
    return dict(row) if row else None


async def upsert_wiki_page(
    page_path: str,
    page_type: str,
    title: str,
    summary: str,
    tags: list[str],
) -> str:
    """Upsert wiki_pages，返回操作状态字符串。"""
    from db.connection import db_execute

    result = await db_execute(
        """
        INSERT INTO wiki_pages (page_path, page_type, title, summary, tags,
                                last_updated, update_count, embedding, created_at)
        VALUES ($1, $2, $3, $4, $5, NOW(), 1, NULL, NOW())
        ON CONFLICT (page_path) DO UPDATE SET
            title        = EXCLUDED.title,
            summary      = EXCLUDED.summary,
            tags         = EXCLUDED.tags,
            last_updated = NOW(),
            update_count = wiki_pages.update_count + 1
        """,
        page_path,
        page_type,
        title,
        summary,
        tags,
    )
    return result


# ────────────────────────────────────────────────
# 核心处理
# ────────────────────────────────────────────────

async def process_symbol(sym_info: dict, verbose: bool = True) -> bool:
    """处理单只股票，生成并写入 wiki 页面。返回是否成功。"""
    symbol = sym_info["symbol"]
    market = sym_info["market"]
    name = sym_info.get("name")
    industry = sym_info.get("industry")

    try:
        judgment = await fetch_latest_judgment(symbol)
        md = generate_page_markdown(symbol, market, name, industry, judgment)
        page_path = build_page_path(symbol, market)
        title = build_title(symbol, name, market)
        direction = judgment.get("direction") if judgment else None
        tags = build_tags(market, direction, industry)

        result = await upsert_wiki_page(page_path, "stock", title, md, tags)
        action = "新增" if result.startswith("INSERT") else "更新"
        if verbose:
            j_date = str(judgment["judgment_date"]) if judgment else "无判断"
            print(f"  ✅ {action}  {page_path}  (判断日期: {j_date})")
        return True
    except Exception as e:
        logger.error("生成页面失败", symbol=symbol, error=str(e))
        if verbose:
            print(f"  ❌ {symbol}  错误: {e}")
        return False


async def run(
    market: str | None,
    symbol: str | None,
    all_active: bool,
    limit: int | None,
) -> None:
    from db.connection import init_pool, close_pool

    await init_pool(min_size=2, max_size=10)
    try:
        target_market = None if all_active else market
        symbols = await fetch_symbols(target_market, symbol)

        if not symbols:
            print("⚠️  未找到符合条件的股票（stock_universe 为空或无匹配）")
            return

        if limit:
            symbols = symbols[:limit]

        total = len(symbols)
        print(f"\n{'═'*60}")
        print(f"  Wiki 页面生成  共 {total} 只股票")
        print(f"{'═'*60}")

        ok = 0
        for sym_info in symbols:
            if await process_symbol(sym_info):
                ok += 1

        fail = total - ok
        print(f"{'═'*60}")
        print(f"  完成: {ok} 成功 / {fail} 失败 / {total} 合计")
        print(f"{'═'*60}\n")

    finally:
        await close_pool()


# ────────────────────────────────────────────────
# 入口
# ────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="为 stock_universe 生成 wiki_pages",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--market", choices=["CN", "US"], help="只处理指定市场")
    mode.add_argument("--all", dest="all_active", action="store_true",
                      help="处理所有 active 股票")
    mode.add_argument("--symbol", type=str, help="处理指定股票（如 002050.SZ）")
    p.add_argument("--limit", type=int, default=None,
                   help="最多处理 N 只（测试用）")
    return p


if __name__ == "__main__":
    parser = build_parser()
    args = parser.parse_args()
    asyncio.run(run(
        market=getattr(args, "market", None),
        symbol=getattr(args, "symbol", None),
        all_active=getattr(args, "all_active", False),
        limit=args.limit,
    ))
