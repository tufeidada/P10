"""
候选池查询接口 — stock_universe 表的唯一读取入口。

所有主项目代码必须通过此模块读取 watchlist，禁止直接拼 SQL 或读 config/watchlist.yaml。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import structlog

from db.connection import db_execute, db_query, db_query_one

logger = structlog.get_logger(__name__)


@dataclass
class UniverseStock:
    """候选池股票信息。

    Attributes:
        symbol: 证券代码。
        market: 市场代码 ('CN' | 'US')。
        name: 股票名称。
        industry: 行业。
        source: 来源 ('manual' | 'system')。
        added_date: 加入日期。
        added_reason: 加入原因。
        active: 是否活跃追踪。
        priority: 优先级 (1=核心 2=观察 3=储备)。
        tags: 标签列表。
        notes: 备注。
    """

    symbol: str
    market: str
    name: str | None
    industry: str | None
    source: str
    added_date: date
    added_reason: str | None
    active: bool
    priority: int
    tags: list[str]
    notes: str | None


def _row_to_stock(row: Any) -> UniverseStock:
    """将 asyncpg Record 转换为 UniverseStock。

    Args:
        row: asyncpg 查询结果行。

    Returns:
        UniverseStock 实例。
    """
    tags = row["tags"] or []
    if isinstance(tags, str):
        import json
        tags = json.loads(tags)
    return UniverseStock(
        symbol=row["symbol"],
        market=row["market"],
        name=row["name"],
        industry=row["industry"],
        source=row["source"],
        added_date=row["added_date"],
        added_reason=row["added_reason"],
        active=row["active"],
        priority=row["priority"],
        tags=tags,
        notes=row["notes"],
    )


async def get_active_symbols(market: str) -> list[str]:
    """获取指定市场的活跃股票代码列表。

    Args:
        market: 市场代码，'CN' 或 'US'。

    Returns:
        股票代码列表，按 priority ASC, symbol ASC 排序。
    """
    rows = await db_query(
        """
        SELECT symbol
        FROM stock_universe
        WHERE active = TRUE AND market = $1
        ORDER BY priority ASC, symbol ASC
        """,
        market,
    )
    symbols = [r["symbol"] for r in rows]
    logger.debug("get_active_symbols", market=market, count=len(symbols))
    return symbols


async def get_active_stocks(market: str | None = None) -> list[UniverseStock]:
    """获取活跃股票完整信息列表。

    Args:
        market: 市场代码 ('CN' | 'US')，None 则返回所有市场。

    Returns:
        UniverseStock 列表，按 market, priority, symbol 排序。
    """
    if market:
        rows = await db_query(
            """
            SELECT symbol, market, name, industry, source, added_date,
                   added_reason, active, priority, tags, notes
            FROM stock_universe
            WHERE active = TRUE AND market = $1
            ORDER BY priority ASC, symbol ASC
            """,
            market,
        )
    else:
        rows = await db_query(
            """
            SELECT symbol, market, name, industry, source, added_date,
                   added_reason, active, priority, tags, notes
            FROM stock_universe
            WHERE active = TRUE
            ORDER BY market ASC, priority ASC, symbol ASC
            """
        )
    return [_row_to_stock(r) for r in rows]


async def get_stock(symbol: str) -> UniverseStock | None:
    """获取单只股票信息（含非活跃）。

    Args:
        symbol: 证券代码。

    Returns:
        UniverseStock 或 None（不存在时）。
    """
    row = await db_query_one(
        """
        SELECT symbol, market, name, industry, source, added_date,
               added_reason, active, priority, tags, notes
        FROM stock_universe
        WHERE symbol = $1
        """,
        symbol,
    )
    return _row_to_stock(row) if row else None


async def upsert_stock(
    symbol: str,
    market: str,
    name: str | None = None,
    industry: str | None = None,
    source: str = "manual",
    added_reason: str | None = None,
    priority: int = 1,
    tags: list[str] | None = None,
    notes: str | None = None,
) -> None:
    """插入或更新候选池股票。

    已存在时更新 name/industry/priority/tags/notes，激活 active=TRUE。
    新增时写入完整字段。

    Args:
        symbol: 证券代码。
        market: 市场代码。
        name: 股票名称。
        industry: 行业。
        source: 来源。
        added_reason: 加入原因。
        priority: 优先级。
        tags: 标签列表。
        notes: 备注。
    """
    import json
    from datetime import date as _date
    tags_json = json.dumps(tags or [])
    await db_execute(
        """
        INSERT INTO stock_universe
            (symbol, market, name, industry, source, added_date, added_reason,
             active, priority, tags, notes)
        VALUES ($1, $2, $3, $4, $5, $6, $7, TRUE, $8, $9::jsonb, $10)
        ON CONFLICT (symbol, market) DO UPDATE SET
            name         = COALESCE(EXCLUDED.name, stock_universe.name),
            industry     = COALESCE(EXCLUDED.industry, stock_universe.industry),
            active       = TRUE,
            priority     = EXCLUDED.priority,
            tags         = EXCLUDED.tags,
            notes        = COALESCE(EXCLUDED.notes, stock_universe.notes),
            added_reason = COALESCE(EXCLUDED.added_reason, stock_universe.added_reason),
            removed_date = NULL,
            removed_reason = NULL
        """,
        symbol, market, name, industry, source, _date.today(),
        added_reason, priority, tags_json, notes,
    )
    logger.info("upsert_stock", symbol=symbol, market=market)


async def deactivate_stock(symbol: str, reason: str = "manual_remove") -> bool:
    """停止追踪股票（active=FALSE），保留历史数据。

    Args:
        symbol: 证券代码。
        reason: 移除原因。

    Returns:
        True 如果找到并更新，False 如果股票不存在或已非活跃。
    """
    from datetime import date as _date
    result = await db_execute(
        """
        UPDATE stock_universe
        SET active = FALSE, removed_date = $1, removed_reason = $2
        WHERE symbol = $3 AND active = TRUE
        """,
        _date.today(), reason, symbol,
    )
    updated = result != "UPDATE 0"
    if updated:
        logger.info("deactivate_stock", symbol=symbol, reason=reason)
    return updated
