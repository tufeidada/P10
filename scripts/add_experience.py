"""
M9 — 手动添加 experience_store 经验条目的 CLI 工具。

用法：
    python scripts/add_experience.py --category error_pattern --market CN --content "..."
    python scripts/add_experience.py --category signal_tuning --market US --content "..." \\
        --evidence '{"source":"backtest","period":"2025-Q1"}' --status active
    python scripts/add_experience.py --list
    python scripts/add_experience.py --list --status active
    python scripts/add_experience.py --activate 3
    python scripts/add_experience.py --archive 5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(".env")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import structlog

structlog.configure(
    processors=[
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(20),  # INFO
)

logger = structlog.get_logger(__name__)

VALID_CATEGORIES = {"error_pattern", "signal_tuning", "market_pattern", "risk_rule"}
VALID_MARKETS = {"CN", "US", "GLOBAL"}
VALID_STATUSES = {"active", "under_review", "archived"}

CATEGORY_CN = {
    "error_pattern": "错误模式",
    "signal_tuning": "信号调优",
    "market_pattern": "市场规律",
    "risk_rule": "风控规则",
}
STATUS_CN = {
    "active": "激活",
    "under_review": "待审",
    "archived": "归档",
}


# ────────────────────────────────────────────────
# 数据库操作
# ────────────────────────────────────────────────

async def add_experience(
    category: str,
    market: str,
    content: str,
    evidence: dict | None,
    status: str,
) -> int:
    """插入新经验条目，返回新 id。"""
    from db.connection import init_pool, close_pool, db_query_val

    await init_pool(min_size=1, max_size=3)
    try:
        new_id = await db_query_val(
            """
            INSERT INTO experience_store
                (discovery_date, category, market, content_text, evidence, embedding, status,
                 applied_count, last_validated)
            VALUES ($1, $2, $3, $4, $5, NULL, $6, 0, NULL)
            RETURNING id
            """,
            date.today(),
            category,
            market,
            content,
            json.dumps(evidence) if evidence else None,
            status,
        )
        return new_id
    finally:
        await close_pool()


async def list_experiences(status_filter: str | None) -> list[dict]:
    """列出经验条目（可按状态过滤）。"""
    from db.connection import init_pool, close_pool, db_query

    await init_pool(min_size=1, max_size=3)
    try:
        if status_filter:
            rows = await db_query(
                """
                SELECT id, discovery_date, category, market, status, applied_count,
                       left(content_text, 120) AS preview
                FROM experience_store
                WHERE status = $1
                ORDER BY id DESC
                """,
                status_filter,
            )
        else:
            rows = await db_query(
                """
                SELECT id, discovery_date, category, market, status, applied_count,
                       left(content_text, 120) AS preview
                FROM experience_store
                ORDER BY id DESC
                """
            )
        return [dict(r) for r in rows]
    finally:
        await close_pool()


async def set_status(exp_id: int, new_status: str) -> bool:
    """更新经验条目状态，返回是否找到该行。"""
    from db.connection import init_pool, close_pool, db_execute

    await init_pool(min_size=1, max_size=3)
    try:
        result = await db_execute(
            "UPDATE experience_store SET status = $1 WHERE id = $2",
            new_status,
            exp_id,
        )
        # asyncpg returns "UPDATE N"
        count = int(result.split()[-1])
        return count > 0
    finally:
        await close_pool()


# ────────────────────────────────────────────────
# 输出格式
# ────────────────────────────────────────────────

def _print_list(rows: list[dict]) -> None:
    if not rows:
        print("  （无记录）")
        return

    header = f"{'ID':>4}  {'日期':10}  {'类别':10}  {'市场':6}  {'状态':8}  {'用次':4}  预览"
    print(header)
    print("-" * len(header))
    for r in rows:
        cat_cn = CATEGORY_CN.get(r["category"], r["category"])
        st_cn = STATUS_CN.get(r["status"], r["status"])
        preview = r["preview"].replace("\n", " ")
        if len(preview) >= 120:
            preview = preview[:117] + "..."
        print(
            f"{r['id']:>4}  {str(r['discovery_date']):10}  {cat_cn:10}  "
            f"{r['market'] or '-':6}  {st_cn:8}  {r['applied_count']:>4}  {preview}"
        )


# ────────────────────────────────────────────────
# 入口
# ────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="experience_store 管理工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--list", action="store_true", help="列出经验条目")
    mode.add_argument("--activate", type=int, metavar="ID", help="激活指定 ID 条目")
    mode.add_argument("--archive", type=int, metavar="ID", help="归档指定 ID 条目")
    mode.add_argument("--category", choices=sorted(VALID_CATEGORIES), help="新增时的类别")

    p.add_argument("--market", choices=sorted(VALID_MARKETS), default="GLOBAL",
                   help="市场 (CN/US/GLOBAL)")
    p.add_argument("--content", type=str, help="经验内容文本（新增时必填）")
    p.add_argument("--evidence", type=str, default=None,
                   help="证据 JSON 字符串，如 '{\"source\":\"backtest\"}'")
    p.add_argument("--status", choices=sorted(VALID_STATUSES), default="under_review",
                   help="初始状态（仅新增时有效，默认 under_review）")
    return p


async def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    # ── 列表模式 ──
    if args.list:
        status_filter = getattr(args, "status", None)
        # --list 不传 --status 时 status 默认是 under_review（因为 add_argument 有 default）
        # 用户明确没传则不过滤 —— 区别方式：检查 sys.argv
        if "--status" not in sys.argv:
            status_filter = None
        rows = await list_experiences(status_filter)
        title = f"经验库 ({len(rows)} 条)"
        if status_filter:
            title += f"  [过滤: {STATUS_CN.get(status_filter, status_filter)}]"
        print(f"\n{'═'*60}")
        print(f"  {title}")
        print(f"{'═'*60}")
        _print_list(rows)
        print(f"{'═'*60}\n")
        return 0

    # ── 激活模式 ──
    if args.activate is not None:
        found = await set_status(args.activate, "active")
        if found:
            print(f"✅ ID={args.activate} 已激活")
        else:
            print(f"❌ 未找到 ID={args.activate}")
            return 1
        return 0

    # ── 归档模式 ──
    if args.archive is not None:
        found = await set_status(args.archive, "archived")
        if found:
            print(f"✅ ID={args.archive} 已归档")
        else:
            print(f"❌ 未找到 ID={args.archive}")
            return 1
        return 0

    # ── 新增模式 ──
    if not args.content:
        print("❌ 新增经验时必须提供 --content")
        return 1

    evidence = None
    if args.evidence:
        try:
            evidence = json.loads(args.evidence)
        except json.JSONDecodeError as e:
            print(f"❌ --evidence 不是合法 JSON: {e}")
            return 1

    new_id = await add_experience(
        category=args.category,
        market=args.market,
        content=args.content,
        evidence=evidence,
        status=args.status,
    )
    print(
        f"✅ 新增经验条目 ID={new_id}\n"
        f"   类别: {CATEGORY_CN.get(args.category, args.category)}\n"
        f"   市场: {args.market}\n"
        f"   状态: {STATUS_CN.get(args.status, args.status)}\n"
        f"   内容: {args.content[:100]}{'...' if len(args.content) > 100 else ''}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
