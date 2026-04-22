#!/usr/bin/env python3
"""
Wiki 冷启动脚本

预写入已知的交易经验和操作手册到 Wiki，
为 LLM RAG 检索提供初始知识库。

用法:
    python scripts/init_wiki.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import structlog

# ─── 项目根加载 ────────────────────────────────────────────────────────────────
# 将项目根添加到 sys.path，使 `from llm.wiki_manager import WikiManager` 可用
_PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(dotenv_path=_PROJECT_ROOT / ".env")

# 加载 .env 后再导入依赖环境变量的模块
from db.connection import init_pool, close_pool  # noqa: E402
from llm.wiki_manager import WikiManager  # noqa: E402

logger = structlog.get_logger(__name__)

# ─── 冷启动内容 ────────────────────────────────────────────────────────────────

BEHAVIORAL_TRAPS_CONTENT = """---
title: 交易行为陷阱
type: strategy
last_updated: 2026-04-17
---

# 交易行为陷阱（来自 14 个月交易记录分析）

## 已识别的陷阱

### 1. 处置效应
盈利时急于止盈，亏损时死扛。
历史数据显示：平均盈利持仓 5 天，亏损持仓 15 天。
**规则**: 系统止盈/止损优先于情绪。

### 2. FOMO 早盘买入
10:00 前买入的交易胜率仅 38%，10:00 后买入胜率 52%。
**规则**: 系统在 10:00 前的买入信号上标注"早盘警告"，降级处理。

### 3. 卖后追回
"卖出后又买回更高价"导致的净亏损占总亏损的 23%。
**规则**: 卖出后 3 个交易日内对同一只票的买入信号自动降级为"观察"。

### 4. 行业集中
盈利几乎全部来自 1-2 个行业（稀土/有色/半导体），其他行业整体亏损。
**规则**: 单行业持仓不超过总仓位 40%。
"""

REGIME_PLAYBOOK_CONTENT = """---
title: 不同市场环境下的操作手册
type: strategy
last_updated: 2026-04-17
---

# 不同市场环境下的操作手册（Regime Playbook）

## 四种模式的操作原则

### 进攻模式 (offense)
- 正常仓位，积极跟随趋势
- 止损可放宽至 10%
- 优先选 Stage 2 + RS > 80 的强势股

### 谨慎进攻 (cautious_offense)
- 只做 Stage 2 + RS > 80 的强势股
- 止损 8%
- 仓位不超过 60%

### 防守模式 (defense)
- 仓位不超过 40%
- 只做高确定性机会（突破+放量）
- 止损 6%

### 避险模式 (risk_off)
- 仓位不超过 20%
- 持有现金等待，不追涨
- 以保本为首要目标

## 关键经验

- Regime 切换的第一天就应该行动，不要等确认
- 高波动环境下技术面信号的准确率下降约 15-20%
- A 股 regime 从 offense 切换到 defense 时，如果不减仓，平均后续 20 天亏损 6-8%
"""

# ─── 精炼经验条目 ───────────────────────────────────────────────────────────────

# (content_text, category, market)
EXPERIENCE_ENTRIES: list[tuple[str, str, str]] = [
    (
        "FOMO 早盘买入胜率仅38%，建议10:00后入场",
        "error_pattern",
        "CN",
    ),
    (
        "卖后追回亏损占总亏损23%，卖出后3日内同票买入信号降级",
        "error_pattern",
        "CN",
    ),
    (
        "高波动环境技术面信号准确率下降15-20%",
        "signal_tuning",
        "CN",
    ),
    (
        "Regime切换第一天就应行动",
        "market_pattern",
        "CN",
    ),
]

# ─── 页面元数据映射 ─────────────────────────────────────────────────────────────

_PAGES: list[dict] = [
    {
        "page_path": "strategies/behavioral_traps.md",
        "page_type": "strategy",
        "title": "交易行为陷阱",
        "summary": "来自14个月交易记录分析：处置效应、FOMO早盘、卖后追回、行业集中等陷阱及规则",
        "tags": ["strategy", "CN", "behavior", "risk"],
        "content": BEHAVIORAL_TRAPS_CONTENT,
    },
    {
        "page_path": "strategies/regime_playbook.md",
        "page_type": "strategy",
        "title": "不同市场环境下的操作手册",
        "summary": "四种Regime模式（进攻/谨慎进攻/防守/避险）下的仓位、止损和选股规则",
        "tags": ["strategy", "CN", "regime", "playbook"],
        "content": REGIME_PLAYBOOK_CONTENT,
    },
]


# ─── 主逻辑 ────────────────────────────────────────────────────────────────────


async def _write_page_files(wm: WikiManager) -> list[str]:
    """将策略页面内容写入文件系统。

    Args:
        wm: WikiManager 实例（提供 write_page）。

    Returns:
        成功写入的页面路径列表。
    """
    written: list[str] = []
    for page in _PAGES:
        try:
            wm.write_page(page["page_path"], page["content"])
            logger.info("page_written", page_path=page["page_path"])
            written.append(page["page_path"])
        except Exception as e:
            logger.warning("page_write_failed", page_path=page["page_path"], error=str(e))
    return written


async def _upsert_pages_db(wm: WikiManager, written_paths: list[str]) -> list[str]:
    """将已写入的页面同步到 wiki_pages DB 索引。

    Args:
        wm: WikiManager 实例。
        written_paths: 成功写入文件的页面路径集合。

    Returns:
        成功写入 DB 的页面路径列表。
    """
    upserted: list[str] = []
    for page in _PAGES:
        if page["page_path"] not in written_paths:
            continue
        try:
            await wm.upsert_page_db(
                page_path=page["page_path"],
                page_type=page["page_type"],
                title=page["title"],
                summary=page["summary"],
                tags=page["tags"],
                content=page["content"],
            )
            logger.info("page_db_upserted", page_path=page["page_path"])
            upserted.append(page["page_path"])
        except Exception as e:
            logger.warning("page_db_upsert_failed", page_path=page["page_path"], error=str(e))
    return upserted


async def _add_experiences(wm: WikiManager) -> list[int]:
    """批量写入精炼经验条目到 experience_store。

    Args:
        wm: WikiManager 实例。

    Returns:
        成功写入的经验条目 ID 列表（-1 表示失败）。
    """
    ids: list[int] = []
    for content_text, category, market in EXPERIENCE_ENTRIES:
        exp_id = await wm.add_experience(
            content_text=content_text,
            category=category,
            market=market,
            evidence={"source": "init_wiki_cold_start", "date": "2026-04-17"},
            status="active",
        )
        if exp_id is not None and exp_id != -1:
            logger.info(
                "experience_inserted",
                exp_id=exp_id,
                category=category,
                content_preview=content_text[:50],
            )
            ids.append(exp_id)
        else:
            logger.warning(
                "experience_insert_failed",
                category=category,
                content_preview=content_text[:50],
            )
    return ids


async def main() -> None:
    """Wiki 冷启动主流程。

    1. 初始化 DB 连接池
    2. 写入策略页面到文件系统
    3. 将页面索引 upsert 到 wiki_pages 表
    4. 写入精炼经验条目到 experience_store
    5. 打印汇总报告
    """
    print("=" * 60)
    print("P10-AlphaRadar Wiki 冷启动")
    print("=" * 60)

    # 初始化连接池
    try:
        await init_pool()
        logger.info("db_pool_initialized")
    except Exception as e:
        logger.error("db_pool_init_failed", error=str(e))
        print(f"\n[ERROR] 数据库连接失败: {e}")
        print("请确认 PostgreSQL 已启动（localhost:5433，用户=radar）并已建表。")
        sys.exit(1)

    wm = WikiManager(wiki_dir=str(_PROJECT_ROOT / "wiki"))

    # Step 1: 写入文件
    print("\n[1/3] 写入 Wiki 页面文件...")
    written_paths = await _write_page_files(wm)
    print(f"      写入: {len(written_paths)}/{len(_PAGES)} 个页面")
    for p in written_paths:
        print(f"      ✓ wiki/{p}")

    # Step 2: 同步 DB 索引
    print("\n[2/3] 同步 wiki_pages 数据库索引...")
    upserted_paths = await _upsert_pages_db(wm, written_paths)
    print(f"      DB upsert: {len(upserted_paths)}/{len(written_paths)} 个页面")

    # Step 3: 写入经验条目
    print("\n[3/3] 写入 experience_store 经验条目...")
    exp_ids = await _add_experiences(wm)
    print(f"      写入: {len(exp_ids)}/{len(EXPERIENCE_ENTRIES)} 条经验")
    for i, (content_text, category, market) in enumerate(EXPERIENCE_ENTRIES):
        status = "✓" if i < len(exp_ids) else "✗"
        print(f"      {status} [{category}/{market}] {content_text[:60]}")

    # 汇总
    print("\n" + "=" * 60)
    print("Wiki 冷启动完成")
    print(f"  策略页面: {len(written_paths)} 个文件写入，{len(upserted_paths)} 个 DB 索引")
    print(f"  经验条目: {len(exp_ids)} 条写入 experience_store")
    if len(exp_ids) < len(EXPERIENCE_ENTRIES):
        failed = len(EXPERIENCE_ENTRIES) - len(exp_ids)
        print(f"  [警告] {failed} 条经验写入失败（可能是向量服务未配置，不影响后续使用）")
    print("=" * 60)

    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
