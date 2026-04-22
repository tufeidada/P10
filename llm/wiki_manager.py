"""Wiki 管理器 — Markdown 页面 CRUD + RAG 检索。"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from db.connection import db_execute, db_query, db_query_one
from llm.embedder import Embedder

logger = structlog.get_logger(__name__)


class WikiManager:
    """管理 wiki/ 目录下的 Markdown 页面 + 数据库索引 + 向量检索。

    Attributes:
        _wiki_dir: Absolute path to the wiki root directory.
        _embedder: Embedder instance for generating text vectors.
    """

    def __init__(self, wiki_dir: str = "wiki") -> None:
        self._wiki_dir = Path(wiki_dir)
        self._embedder = Embedder()

    # ------------------------------------------------------------------
    # Filesystem helpers
    # ------------------------------------------------------------------

    def _page_path(self, symbol: str, market: str) -> str:
        """Get relative page path for a stock.

        Args:
            symbol: Stock symbol, e.g. ``"600519.SH"`` or ``"AAPL"``.
            market: Market code, e.g. ``"CN"`` or ``"US"``.

        Returns:
            Relative path string, e.g. ``"stocks/CN/600519_SH.md"``.
        """
        return f"stocks/{market}/{symbol.replace('.', '_')}.md"

    def _full_path(self, page_path: str) -> Path:
        """Get absolute filesystem path for a relative page path.

        Args:
            page_path: Relative path from wiki root, e.g.
                ``"stocks/CN/600519_SH.md"``.

        Returns:
            Absolute :class:`pathlib.Path`.
        """
        return self._wiki_dir / page_path

    def read_page(self, page_path: str) -> str | None:
        """Read page content from the filesystem.

        Args:
            page_path: Relative path from wiki root.

        Returns:
            Page content as a string, or ``None`` if the file does not exist.
        """
        full = self._full_path(page_path)
        if not full.exists():
            return None
        return full.read_text(encoding="utf-8")

    def write_page(self, page_path: str, content: str) -> None:
        """Write page content to the filesystem, creating parent dirs as needed.

        Args:
            page_path: Relative path from wiki root.
            content: Markdown content to write.
        """
        full = self._full_path(page_path)
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")

    # ------------------------------------------------------------------
    # Stock page creation / update
    # ------------------------------------------------------------------

    async def create_stock_page(
        self, symbol: str, market: str, analysis_result: dict[str, Any]
    ) -> str:
        """Create a new stock wiki page from an analysis result.

        Builds a structured Markdown page from the analysis data, writes it to
        the filesystem, upserts the database record, and returns the content.

        Args:
            symbol: Stock symbol, e.g. ``"600519.SH"``.
            market: Market code, e.g. ``"CN"``.
            analysis_result: Analysis dictionary containing keys such as
                ``direction``, ``composite_score``, ``technical_score``,
                ``fundamental_score``, ``flow_score``, ``logic_text``,
                ``judgment_date``, and ``signal_sources``.

        Returns:
            The written page content.
        """
        today = analysis_result.get("judgment_date", str(date.today()))
        direction = analysis_result.get("direction", "neutral")
        composite_score = analysis_result.get("composite_score", 0)
        tech_score = analysis_result.get("technical_score", 0)
        fund_score = analysis_result.get("fundamental_score", 0)
        flow_score = analysis_result.get("flow_score", 0)
        logic_text: str = analysis_result.get("logic_text", "")
        logic_excerpt = logic_text[:200] if logic_text else "暂无"

        signal_sources: dict[str, Any] = analysis_result.get("signal_sources", {})
        tech_src: dict[str, Any] = signal_sources.get("technical", {})
        fund_src: dict[str, Any] = signal_sources.get("fundamental", {})
        flow_src: dict[str, Any] = signal_sources.get("flow", {})

        # Extract nested fields with sensible defaults
        stage = tech_src.get("stage", "N/A")
        rs_rank = tech_src.get("rs_rank", "N/A")
        trend = tech_src.get("trend", "N/A")
        roe = fund_src.get("roe", "N/A")
        rev_yoy = fund_src.get("revenue_yoy", "N/A")
        flow_direction = flow_src.get("direction", "N/A")
        northbound_trend = flow_src.get("northbound_trend", "N/A")
        supports: list[Any] = tech_src.get("supports", [])
        resistances: list[Any] = tech_src.get("resistances", [])

        supports_str = ", ".join(str(s) for s in supports) if supports else "暂无"
        resistances_str = (
            ", ".join(str(r) for r in resistances) if resistances else "暂无"
        )

        content = f"""---
symbol: {symbol}
market: {market}
last_updated: {today}
current_stage: {stage}
---

## 公司概况
[首次分析，暂无历史数据]

## 当前状态 ({today})
- 技术面: {tech_score}/100 — {trend}, Stage {stage}, RS Rank {rs_rank}
- 基本面: {fund_score}/100 — ROE {roe}%, 营收增速 {rev_yoy}%
- 资金面: {flow_score}/100 — 主力5日{flow_direction}, 北向{northbound_trend}
- 综合判断: {direction} ({composite_score}/100)
- 分析叙事: {logic_excerpt}

## 关键价位
- 支撑: {supports_str}
- 阻力: {resistances_str}

## 行为模式
暂无记录。

## 历史判断摘要
| 日期 | 方向 | 综合分 | 简评 |
|------|------|-------|------|
| {today} | {direction} | {composite_score} | 技术{tech_score}/基本面{fund_score}/资金{flow_score} |
"""

        page_path = self._page_path(symbol, market)
        self.write_page(page_path, content)

        title = f"{symbol} ({market})"
        summary = (
            f"{symbol} — {direction} — 综合分 {composite_score}/100 ({today})"
        )
        tags = [market, "stock", direction]
        await self.upsert_page_db(page_path, "stock", title, summary, tags, content)
        await self.update_index(page_path, summary)

        logger.info(
            "wiki_page_created",
            page_path=page_path,
            symbol=symbol,
            market=market,
        )
        return content

    async def update_stock_page(
        self, symbol: str, market: str, analysis_result: dict[str, Any]
    ) -> str:
        """Update an existing stock wiki page with new analysis.

        Updates the front-matter ``last_updated`` field, replaces the
        ``## 当前状态`` section with the new analysis, appends a row to the
        ``## 历史判断摘要`` table (keeping only the last 5 rows), and refreshes
        key levels when they differ significantly. If no existing page is found,
        delegates to :meth:`create_stock_page`.

        Args:
            symbol: Stock symbol.
            market: Market code.
            analysis_result: Same structure as in :meth:`create_stock_page`.

        Returns:
            The updated page content.
        """
        page_path = self._page_path(symbol, market)
        existing = self.read_page(page_path)
        if not existing:
            logger.info(
                "wiki_page_not_found_creating",
                page_path=page_path,
            )
            return await self.create_stock_page(symbol, market, analysis_result)

        today = analysis_result.get("judgment_date", str(date.today()))
        direction = analysis_result.get("direction", "neutral")
        composite_score = analysis_result.get("composite_score", 0)
        tech_score = analysis_result.get("technical_score", 0)
        fund_score = analysis_result.get("fundamental_score", 0)
        flow_score = analysis_result.get("flow_score", 0)
        logic_text: str = analysis_result.get("logic_text", "")
        logic_excerpt = logic_text[:200] if logic_text else "暂无"

        signal_sources: dict[str, Any] = analysis_result.get("signal_sources", {})
        tech_src: dict[str, Any] = signal_sources.get("technical", {})
        fund_src: dict[str, Any] = signal_sources.get("fundamental", {})
        flow_src: dict[str, Any] = signal_sources.get("flow", {})

        stage = tech_src.get("stage", "N/A")
        rs_rank = tech_src.get("rs_rank", "N/A")
        trend = tech_src.get("trend", "N/A")
        roe = fund_src.get("roe", "N/A")
        rev_yoy = fund_src.get("revenue_yoy", "N/A")
        flow_direction = flow_src.get("direction", "N/A")
        northbound_trend = flow_src.get("northbound_trend", "N/A")
        new_supports: list[Any] = tech_src.get("supports", [])
        new_resistances: list[Any] = tech_src.get("resistances", [])

        content = existing

        # 1. Update front-matter last_updated
        content = re.sub(
            r"(last_updated:\s*).*",
            f"\\g<1>{today}",
            content,
        )
        # Also update current_stage if present
        content = re.sub(
            r"(current_stage:\s*).*",
            f"\\g<1>{stage}",
            content,
        )

        # 2. Replace "## 当前状态" section
        new_status_section = (
            f"## 当前状态 ({today})\n"
            f"- 技术面: {tech_score}/100 — {trend}, Stage {stage}, RS Rank {rs_rank}\n"
            f"- 基本面: {fund_score}/100 — ROE {roe}%, 营收增速 {rev_yoy}%\n"
            f"- 资金面: {flow_score}/100 — 主力5日{flow_direction}, 北向{northbound_trend}\n"
            f"- 综合判断: {direction} ({composite_score}/100)\n"
            f"- 分析叙事: {logic_excerpt}\n"
        )
        content = _replace_section(content, "当前状态", new_status_section)

        # 3. Update key levels if new data is provided
        if new_supports or new_resistances:
            supports_str = (
                ", ".join(str(s) for s in new_supports) if new_supports else "暂无"
            )
            resistances_str = (
                ", ".join(str(r) for r in new_resistances)
                if new_resistances
                else "暂无"
            )
            new_levels_section = (
                "## 关键价位\n"
                f"- 支撑: {supports_str}\n"
                f"- 阻力: {resistances_str}\n"
            )
            content = _replace_section(content, "关键价位", new_levels_section)

        # 4. Append row to "## 历史判断摘要" table, keep last 5 rows
        new_row = (
            f"| {today} | {direction} | {composite_score} "
            f"| 技术{tech_score}/基本面{fund_score}/资金{flow_score} |"
        )
        content = _append_table_row(content, "历史判断摘要", new_row, max_rows=5)

        self.write_page(page_path, content)

        title = f"{symbol} ({market})"
        summary = (
            f"{symbol} — {direction} — 综合分 {composite_score}/100 ({today})"
        )
        tags = [market, "stock", direction]
        await self.upsert_page_db(page_path, "stock", title, summary, tags, content)
        await self.update_index(page_path, summary)

        logger.info(
            "wiki_page_updated",
            page_path=page_path,
            symbol=symbol,
            market=market,
        )
        return content

    # ------------------------------------------------------------------
    # Database operations
    # ------------------------------------------------------------------

    async def upsert_page_db(
        self,
        page_path: str,
        page_type: str,
        title: str,
        summary: str,
        tags: list[str],
        content: str,
    ) -> None:
        """Upsert a wiki_pages DB record and regenerate its embedding.

        Generates the embedding from the concatenation of *summary* and the
        first 500 characters of *content*, then performs an INSERT … ON CONFLICT
        DO UPDATE.

        Args:
            page_path: Primary key for wiki_pages, relative path.
            page_type: One of ``'stock'``, ``'industry'``, ``'strategy'``,
                ``'system'``, ``'trade'``.
            title: Human-readable page title.
            summary: Short summary text (LLM-generated or auto-built).
            tags: List of tag strings.
            content: Full page Markdown content.
        """
        embed_text = summary + " " + content[:500]
        embedding: list[float] | None = None
        if self._embedder.is_configured():
            try:
                embedding = await self._embedder.embed(embed_text)
            except Exception as exc:
                logger.warning(
                    "wiki_embed_failed", page_path=page_path, error=str(exc)
                )

        now = datetime.now(tz=timezone.utc)

        if embedding is not None:
            await db_execute(
                """
                INSERT INTO wiki_pages
                    (page_path, page_type, title, summary, tags,
                     last_updated, update_count, embedding)
                VALUES ($1, $2, $3, $4, $5, $6, 1, $7::vector)
                ON CONFLICT (page_path) DO UPDATE SET
                    page_type    = EXCLUDED.page_type,
                    title        = EXCLUDED.title,
                    summary      = EXCLUDED.summary,
                    tags         = EXCLUDED.tags,
                    last_updated = EXCLUDED.last_updated,
                    update_count = wiki_pages.update_count + 1,
                    embedding    = EXCLUDED.embedding
                """,
                page_path,
                page_type,
                title,
                summary,
                tags,
                now,
                str(embedding),
            )
        else:
            await db_execute(
                """
                INSERT INTO wiki_pages
                    (page_path, page_type, title, summary, tags,
                     last_updated, update_count)
                VALUES ($1, $2, $3, $4, $5, $6, 1)
                ON CONFLICT (page_path) DO UPDATE SET
                    page_type    = EXCLUDED.page_type,
                    title        = EXCLUDED.title,
                    summary      = EXCLUDED.summary,
                    tags         = EXCLUDED.tags,
                    last_updated = EXCLUDED.last_updated,
                    update_count = wiki_pages.update_count + 1
                """,
                page_path,
                page_type,
                title,
                summary,
                tags,
                now,
            )

        logger.info("wiki_db_upserted", page_path=page_path)

    # ------------------------------------------------------------------
    # RAG / experience store
    # ------------------------------------------------------------------

    async def search_experience(
        self, query: str, top_k: int = 3
    ) -> list[dict[str, Any]]:
        """RAG search in the experience_store table.

        Embeds *query* and performs a cosine-similarity search against all
        ``status='active'`` rows in ``experience_store``.

        Args:
            query: Free-text search query.
            top_k: Maximum number of results to return.

        Returns:
            List of dicts with keys ``content_text``, ``evidence``, and
            ``category``. Returns an empty list when the embedder is not
            configured or no results are found.
        """
        if not self._embedder.is_configured():
            logger.debug("search_experience_skipped_no_embedder")
            return []

        try:
            vec = await self._embedder.embed(query)
        except Exception as exc:
            logger.warning("search_experience_embed_failed", error=str(exc))
            return []

        if vec is None:
            return []

        rows = await db_query(
            """
            SELECT content_text, evidence, category
            FROM experience_store
            WHERE status = 'active'
            ORDER BY embedding <=> $1::vector
            LIMIT $2
            """,
            str(vec),
            top_k,
        )
        return [
            {
                "content_text": row["content_text"],
                "evidence": row["evidence"],
                "category": row["category"],
            }
            for row in rows
        ]

    async def add_experience(
        self,
        content_text: str,
        category: str,
        market: str,
        evidence: dict[str, Any] | None = None,
        status: str = "under_review",
    ) -> int:
        """Add a new experience entry with an embedding vector.

        Args:
            content_text: Human-readable experience description.
            category: One of ``'market_pattern'``, ``'stock_specific'``,
                ``'signal_tuning'``, ``'error_pattern'``.
            market: Market context, e.g. ``'CN'``, ``'US'``, or ``'both'``.
            evidence: Optional supporting data (accuracy, sample count, etc.).
            status: Initial review status; defaults to ``'under_review'``.

        Returns:
            The ``id`` of the inserted row.
        """
        import json as _json

        embedding: list[float] | None = None
        if self._embedder.is_configured():
            try:
                embedding = await self._embedder.embed(content_text)
            except Exception as exc:
                logger.warning("add_experience_embed_failed", error=str(exc))

        today = date.today()
        evidence_json = _json.dumps(evidence, ensure_ascii=False) if evidence else None

        if embedding is not None:
            row = await db_query_one(
                """
                INSERT INTO experience_store
                    (discovery_date, category, market, content_text,
                     evidence, embedding, status)
                VALUES ($1, $2, $3, $4, $5, $6::vector, $7)
                RETURNING id
                """,
                today,
                category,
                market,
                content_text,
                evidence_json,
                str(embedding),
                status,
            )
        else:
            row = await db_query_one(
                """
                INSERT INTO experience_store
                    (discovery_date, category, market, content_text,
                     evidence, status)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING id
                """,
                today,
                category,
                market,
                content_text,
                evidence_json,
                status,
            )

        inserted_id: int = row["id"] if row else -1
        logger.info(
            "experience_added",
            id=inserted_id,
            category=category,
            market=market,
        )
        return inserted_id

    # ------------------------------------------------------------------
    # Index maintenance
    # ------------------------------------------------------------------

    async def update_index(self, page_path: str, summary: str) -> None:
        """Update wiki/index.md to include or refresh this page's entry.

        Writes a line of the form
        ``- [title](page_path) — summary (updated: YYYY-MM-DD)``
        under the appropriate section heading.  If an entry for *page_path*
        already exists it is replaced; otherwise it is appended under the
        matching section.

        Args:
            page_path: Relative page path, also used as the link target.
            summary: Short description to include in the index line.
        """
        index_path = "index.md"
        index_full = self._full_path(index_path)
        if not index_full.exists():
            # Create a minimal index file if it doesn't exist yet
            index_full.parent.mkdir(parents=True, exist_ok=True)
            index_full.write_text(
                "# P10-AlphaRadar Wiki 索引\n"
                "> 自动维护 — 每次分析后更新\n\n"
                "## 个股页面 (stocks/)\n\n"
                "## 行业页面 (industries/)\n\n"
                "## 策略经验 (strategies/)\n\n"
                "## 系统记录 (system/)\n\n"
                "## 宏观 (macro/)\n",
                encoding="utf-8",
            )

        content = index_full.read_text(encoding="utf-8")

        # Derive a display title from the path, e.g. "stocks/CN/600519_SH.md"
        stem = Path(page_path).stem  # "600519_SH"
        title = stem.replace("_", ".")

        today = str(date.today())
        new_line = f"- [{title}]({page_path}) — {summary} (updated: {today})"

        # Check if the entry already exists and replace it
        escaped = re.escape(page_path)
        pattern = re.compile(rf"^- \[.*?\]\({escaped}\).*$", re.MULTILINE)
        if pattern.search(content):
            content = pattern.sub(new_line, content)
        else:
            # Append under the matching section
            section = _section_for_path(page_path)
            section_pattern = re.compile(
                rf"(## {re.escape(section)}[^\n]*\n)", re.MULTILINE
            )
            match = section_pattern.search(content)
            if match:
                insert_pos = match.end()
                content = content[:insert_pos] + new_line + "\n" + content[insert_pos:]
            else:
                # Fallback: append at the end
                content = content.rstrip("\n") + "\n" + new_line + "\n"

        index_full.write_text(content, encoding="utf-8")
        logger.debug("wiki_index_updated", page_path=page_path)

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    async def lint(self) -> dict[str, list[str]]:
        """Perform a basic wiki health check.

        Checks for three categories of issues:

        1. Pages present in the DB but whose Markdown file is missing on disk.
        2. Stock pages present on disk (under ``stocks/``) but absent from the DB.
        3. DB pages whose ``last_updated`` is 30+ days in the past.

        Returns:
            Dictionary with keys ``'missing_files'``, ``'missing_db'``, and
            ``'stale'``, each mapping to a list of page path strings.
        """
        from datetime import timedelta

        result: dict[str, list[str]] = {
            "missing_files": [],
            "missing_db": [],
            "stale": [],
        }

        # 1. Pages in DB but not on filesystem
        db_rows = await db_query(
            "SELECT page_path, last_updated FROM wiki_pages"
        )
        db_paths: set[str] = set()
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=30)
        for row in db_rows:
            path = row["page_path"]
            db_paths.add(path)
            if not self._full_path(path).exists():
                result["missing_files"].append(path)
            last_up = row["last_updated"]
            if last_up is not None and last_up < cutoff:
                result["stale"].append(path)

        # 2. Stock markdown files on disk not in DB
        stocks_root = self._wiki_dir / "stocks"
        if stocks_root.exists():
            for md_file in stocks_root.rglob("*.md"):
                rel = md_file.relative_to(self._wiki_dir).as_posix()
                if rel not in db_paths:
                    result["missing_db"].append(rel)

        logger.info(
            "wiki_lint_done",
            missing_files=len(result["missing_files"]),
            missing_db=len(result["missing_db"]),
            stale=len(result["stale"]),
        )
        return result


# ---------------------------------------------------------------------------
# Module-level helpers (private)
# ---------------------------------------------------------------------------


def _find_section_bounds(content: str, section_title: str) -> tuple[int, int] | None:
    """Find the character start/end of a Markdown section (## heading).

    Args:
        content: Full page content.
        section_title: Section heading text to search for (without ``## ``
            prefix and without a date suffix).

    Returns:
        Tuple of (section_start, section_end) character offsets, where
        *section_start* is the position of the ``##`` marker and *section_end*
        is either the start of the next ``##`` heading or end-of-string.
        Returns ``None`` if the section is not found.
    """
    # Match "## Section Title" with an optional " (date)" suffix
    pattern = re.compile(
        rf"^(## {re.escape(section_title)}[^\n]*\n)", re.MULTILINE
    )
    m = pattern.search(content)
    if not m:
        return None

    section_start = m.start()
    # Find the next ## heading after this section
    next_section = re.search(r"^## ", content[m.end():], re.MULTILINE)
    if next_section:
        section_end = m.end() + next_section.start()
    else:
        section_end = len(content)

    return section_start, section_end


def _replace_section(content: str, section_title: str, new_section: str) -> str:
    """Replace the content of a Markdown ``##`` section.

    If the section is not found, the content is returned unchanged.

    Args:
        content: Full page content.
        section_title: Heading text to locate (without ``## `` prefix).
        new_section: Replacement text including the heading line.

    Returns:
        Updated page content.
    """
    bounds = _find_section_bounds(content, section_title)
    if bounds is None:
        return content
    start, end = bounds
    return content[:start] + new_section + content[end:]


def _append_table_row(
    content: str, section_title: str, new_row: str, max_rows: int = 5
) -> str:
    """Append a row to a Markdown table inside a section, keeping at most max_rows data rows.

    Args:
        content: Full page content.
        section_title: Heading text of the section containing the table.
        new_row: New table row string (pipe-delimited Markdown).
        max_rows: Maximum number of data rows to keep (header rows excluded).

    Returns:
        Updated page content, or the original content if the section or table
        header is not found.
    """
    bounds = _find_section_bounds(content, section_title)
    if bounds is None:
        return content

    start, end = bounds
    section_text = content[start:end]

    # Find all table rows (lines starting with |)
    lines = section_text.split("\n")
    header_idx: int | None = None
    separator_idx: int | None = None
    data_row_indices: list[int] = []

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        if header_idx is None:
            header_idx = i
        elif separator_idx is None and re.match(r"^\|[\s|:-]+\|$", stripped):
            separator_idx = i
        elif separator_idx is not None:
            data_row_indices.append(i)

    if header_idx is None or separator_idx is None:
        # No table found — append the row as plain text
        section_text = section_text.rstrip("\n") + "\n" + new_row + "\n"
    else:
        # Append new row and trim to max_rows
        data_row_indices_to_keep = (data_row_indices + [None])[-max_rows:]  # type: ignore[list-item]
        # Rebuild: everything up to and including separator, then kept data rows, then new row
        new_lines = lines[: separator_idx + 1]
        for idx in data_row_indices:
            if idx in data_row_indices_to_keep[: max_rows - 1]:
                new_lines.append(lines[idx])
        new_lines.append(new_row)
        # Preserve any trailing non-table content
        last_data_idx = data_row_indices[-1] if data_row_indices else separator_idx
        new_lines.extend(lines[last_data_idx + 1 :])
        section_text = "\n".join(new_lines)
        if not section_text.endswith("\n"):
            section_text += "\n"

    return content[:start] + section_text + content[end:]


def _section_for_path(page_path: str) -> str:
    """Map a page path prefix to the index.md section heading fragment.

    Args:
        page_path: Relative page path from wiki root.

    Returns:
        Section heading fragment used in ``index.md``.
    """
    if page_path.startswith("stocks/"):
        return "个股页面 (stocks/)"
    if page_path.startswith("industries/"):
        return "行业页面 (industries/)"
    if page_path.startswith("strategies/"):
        return "策略经验 (strategies/)"
    if page_path.startswith("system/"):
        return "系统记录 (system/)"
    if page_path.startswith("macro/"):
        return "宏观 (macro/)"
    return "系统记录 (system/)"
