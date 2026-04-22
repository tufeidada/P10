#!/usr/bin/env python3
"""
P6+ DuckDB Schema 导出工具
在 P6+ 项目目录下运行此脚本，生成 p6_schema_export.txt
将该文件一并发送给 Claude Code 用于编写数据迁移脚本

用法:
  python export_p6_schema.py --db /path/to/agu.duckdb
  python export_p6_schema.py --db /path/to/agu.duckdb --sample 5
"""

import argparse
import duckdb
from pathlib import Path


def export_schema(db_path: str, sample_rows: int = 3):
    output_file = "p6_schema_export.txt"
    db = duckdb.connect(db_path, read_only=True)
    tables = db.sql("SHOW TABLES").fetchall()

    with open(output_file, "w", encoding="utf-8") as f:
        f.write(f"# P6+ DuckDB Schema Export\n")
        f.write(f"# Source: {db_path}\n")
        f.write(f"# Tables: {len(tables)}\n\n")

        for (table_name,) in sorted(tables):
            f.write(f"{'='*60}\n")
            f.write(f"TABLE: {table_name}\n")
            f.write(f"{'='*60}\n\n")

            # Column definitions
            desc = db.sql(f"DESCRIBE {table_name}").fetchall()
            f.write("Columns:\n")
            for col in desc:
                col_name = col[0]
                col_type = col[1]
                nullable = col[2] if len(col) > 2 else ""
                f.write(f"  {col_name:35s} {col_type:20s} {nullable}\n")

            # Row count
            count = db.sql(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
            f.write(f"\nRow count: {count:,}\n")

            # Date range for date columns
            date_cols = [c[0] for c in desc if "date" in c[0].lower()]
            for dc in date_cols:
                try:
                    min_max = db.sql(
                        f"SELECT MIN({dc}), MAX({dc}) FROM {table_name}"
                    ).fetchone()
                    f.write(f"Date range ({dc}): {min_max[0]} ~ {min_max[1]}\n")
                except Exception:
                    pass

            # Sample rows
            if sample_rows > 0 and count > 0:
                f.write(f"\nSample ({sample_rows} rows):\n")
                try:
                    sample = db.sql(
                        f"SELECT * FROM {table_name} LIMIT {sample_rows}"
                    ).fetchdf()
                    f.write(sample.to_string(index=False))
                    f.write("\n")
                except Exception as e:
                    f.write(f"  (Could not fetch sample: {e})\n")

            f.write("\n\n")

    db.close()
    print(f"Schema exported to {output_file}")
    print(f"Tables exported: {len(tables)}")
    print(f"Please send this file to Claude Code along with CLAUDE.md and the architecture doc.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export P6+ DuckDB schema for P10 migration")
    parser.add_argument("--db", required=True, help="Path to P6+ DuckDB file (e.g. data/agu.duckdb)")
    parser.add_argument("--sample", type=int, default=3, help="Number of sample rows per table (default: 3)")
    args = parser.parse_args()

    if not Path(args.db).exists():
        print(f"Error: {args.db} not found")
        exit(1)

    export_schema(args.db, args.sample)
