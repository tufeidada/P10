"""
从 CSV 批量导入 watchlist 到 stock_universe 表。

用法：
  python scripts/load_watchlist.py                          # 默认读 inputs/watchlist_seed.csv
  python scripts/load_watchlist.py --csv path/to/file.csv  # 指定路径
  python scripts/load_watchlist.py --dry-run               # 只打印，不写库

CSV 格式（UTF-8，逗号分隔）：
  symbol,market,name,industry,priority,tags,notes
  600519.SH,CN,贵州茅台,食品饮料,1,"[""消费白马""]",核心持仓候选
  AAPL,US,Apple,Technology,1,"[""mega_cap""]",科技龙头

字段说明：
  - symbol, market：必填
  - name, industry：可选，留空则保留已有值
  - priority：1=核心 2=观察 3=储备，默认 1
  - tags：JSON 数组字符串，默认 []
  - notes：人类可读备注，可为空

幂等性：多次执行结果一致（ON CONFLICT DO UPDATE）。
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from pathlib import Path

import structlog
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.connection import close_pool, init_pool
from db.universe import upsert_stock

logger = structlog.get_logger(__name__)

_DEFAULT_CSV = Path("inputs/watchlist_seed.csv")


def _parse_row(row: dict[str, str], lineno: int) -> dict | None:
    """解析 CSV 一行，返回 upsert_stock 的 kwargs，错误时返回 None。

    Args:
        row: csv.DictReader 的一行。
        lineno: 行号（用于错误提示）。

    Returns:
        kwargs dict 或 None（行无效时）。
    """
    symbol = row.get("symbol", "").strip().upper()
    market = row.get("market", "").strip().upper()

    if not symbol or not market:
        logger.warning("load_watchlist_skip_row", lineno=lineno, reason="symbol 或 market 为空")
        return None

    if market not in ("CN", "US"):
        logger.warning("load_watchlist_skip_row", lineno=lineno, symbol=symbol,
                       reason=f"market={market!r} 不合法，只接受 CN/US")
        return None

    priority_raw = row.get("priority", "1").strip()
    try:
        priority = int(priority_raw) if priority_raw else 1
    except ValueError:
        logger.warning("load_watchlist_invalid_priority", lineno=lineno, symbol=symbol,
                       priority_raw=priority_raw)
        priority = 1

    tags_raw = row.get("tags", "[]").strip()
    try:
        tags: list[str] = json.loads(tags_raw) if tags_raw else []
        if not isinstance(tags, list):
            tags = []
    except (json.JSONDecodeError, ValueError):
        logger.warning("load_watchlist_invalid_tags", lineno=lineno, symbol=symbol,
                       tags_raw=tags_raw)
        tags = []

    return {
        "symbol": symbol,
        "market": market,
        "name": row.get("name", "").strip() or None,
        "industry": row.get("industry", "").strip() or None,
        "source": "manual",
        "added_reason": row.get("notes", "").strip() or None,
        "priority": priority,
        "tags": tags,
        "notes": row.get("notes", "").strip() or None,
    }


async def load_csv(csv_path: Path, dry_run: bool = False) -> None:
    """读取 CSV 并 upsert 到 stock_universe。

    Args:
        csv_path: CSV 文件路径。
        dry_run: True 时只打印，不写库。
    """
    if not csv_path.exists():
        print(f"[waiting for csv] {csv_path} 不存在，跳过导入。")
        print("请将 watchlist 整理好后放到该路径，再重新运行本脚本。")
        return

    rows_parsed: list[dict] = []
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for lineno, row in enumerate(reader, start=2):
            parsed = _parse_row(row, lineno)
            if parsed:
                rows_parsed.append(parsed)

    if not rows_parsed:
        print(f"[warn] CSV 解析后无有效行（共 {lineno - 1} 原始行），退出。")
        return

    print(f"解析完成：{len(rows_parsed)} 只股票待导入（dry_run={dry_run}）")

    if dry_run:
        for r in rows_parsed:
            print(f"  [dry] {r['symbol']:15} {r['market']} priority={r['priority']} tags={r['tags']}")
        return

    await init_pool()
    success = 0
    failed = 0
    for r in rows_parsed:
        try:
            await upsert_stock(**r)
            print(f"  [ok] {r['symbol']:15} {r['market']}")
            success += 1
        except Exception as e:
            print(f"  [err] {r['symbol']}: {e}")
            logger.error("load_watchlist_upsert_error", symbol=r["symbol"], error=str(e))
            failed += 1

    await close_pool()
    print(f"\n完成：成功 {success}，失败 {failed}，共 {success + failed} 只")


def main() -> None:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="从 CSV 导入 watchlist 到 stock_universe 表")
    parser.add_argument("--csv", type=Path, default=_DEFAULT_CSV, help="CSV 文件路径")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不写库")
    args = parser.parse_args()

    asyncio.run(load_csv(args.csv, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
