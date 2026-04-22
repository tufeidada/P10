"""
Migration: add llm_vote_consensus and llm_vote_total_calls to judgments table.

Run once:
    python scripts/migrate_add_vote_columns.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


async def main() -> None:
    from db.connection import db_execute, init_pool, close_pool

    await init_pool()
    try:
        await db_execute(
            """
            ALTER TABLE judgments
                ADD COLUMN IF NOT EXISTS llm_vote_consensus   NUMERIC(3,2),
                ADD COLUMN IF NOT EXISTS llm_vote_total_calls INT
            """
        )
        print("Migration complete: llm_vote_consensus, llm_vote_total_calls added.")
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
