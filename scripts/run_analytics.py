#!/usr/bin/env python3
"""Run sql/analytics.sql against the warehouse and print each result.

Exists because splitting a SQL file on semicolons is wrong -- a semicolon can
appear inside a string or a comment -- and because a reviewer without the
sqlite3 CLI installed should still be able to see the queries run.
``sqlite3.complete_statement`` does the splitting properly.

    python scripts/run_analytics.py [--db PATH] [--only 3]
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "out" / "hourglass.db"
SQL_PATH = ROOT / "sql" / "analytics.sql"

TITLE_RE = re.compile(r"^--\s*(\d+)\.\s*(.+)$")


def split_statements(sql: str) -> list[str]:
    statements, buffer = [], ""
    for line in sql.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            if buffer.strip():
                statements.append(buffer)
            buffer = ""
    if buffer.strip():
        statements.append(buffer)
    return statements


def title_of(statement: str, fallback: str) -> str:
    for line in statement.splitlines():
        m = TITLE_RE.match(line.strip())
        if m:
            return f"{m.group(1)}. {m.group(2)}"
    return fallback


def render(cols: list[str], rows: list[tuple], limit: int = 8) -> str:
    if not rows:
        return "    (no rows)"
    widths = [
        max(len(str(c)), max((len(str(r[i])) for r in rows[:limit]), default=0))
        for i, c in enumerate(cols)
    ]
    widths = [min(w, 34) for w in widths]

    def fmt(values) -> str:
        cells = []
        for v, w in zip(values, widths):
            s = str(v)
            cells.append((s[: w - 1] + "…") if len(s) > w else s.ljust(w))
        return "    " + "  ".join(cells)

    out = [fmt(cols), "    " + "  ".join("-" * w for w in widths)]
    out += [fmt(r) for r in rows[:limit]]
    if len(rows) > limit:
        out.append(f"    ... {len(rows) - limit:,} more rows")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--only", type=int, help="Run a single numbered query.")
    ap.add_argument("--rows", type=int, default=8)
    args = ap.parse_args(argv)

    if not args.db.exists():
        print(f"No warehouse at {args.db}. Run `make run` first.", file=sys.stderr)
        return 2

    conn = sqlite3.connect(args.db)
    statements = split_statements(SQL_PATH.read_text(encoding="utf-8"))

    failures = 0
    for i, stmt in enumerate(statements, 1):
        name = title_of(stmt, f"statement {i}")
        if args.only and not name.startswith(f"{args.only}."):
            continue
        print(f"\n── {name} " + "─" * max(0, 66 - len(name)))
        try:
            cur = conn.execute(stmt)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description] if cur.description else []
            print(render(cols, rows, args.rows))
        except sqlite3.Error as exc:
            failures += 1
            print(f"    ERROR: {exc}")

    conn.close()
    print()
    if failures:
        print(f"{failures} statement(s) failed.", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
