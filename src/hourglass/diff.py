"""Value-level diff between two warehouse builds.

Tests answer "is this data valid". A diff answers a different question that
nobody else in the pipeline asks: **"did this run change anything, and was it
what I expected to change?"**

Both can be true at once and one of them can still be a disaster. Every check
passes, every row is plausible, and 40,000 sessions quietly gained fifteen
minutes because a service's `minutes_per_unit` was edited. Nothing is invalid.
Everything is different. A test suite has no opinion about that; a diff does.

What it reports, per table:

* rows **added** and **removed**, by primary key;
* rows **changed**, and *which columns* changed in them, because "412 rows
  changed" is a shrug and "412 rows changed, all in `minutes_delivered`" is a
  diagnosis;
* a per-column changed-cell count, so a one-column change and a
  whole-table rewrite do not look the same.

The comparison is key-based, not positional. Row order is not meaningful in a
relational table, and a diff that reports every row as changed because the sort
order moved is a diff nobody reads twice.

**What it is not.** This is not Datafold, and at a hundred million rows the
approach here -- load both sides into memory and merge -- stops being viable.
The technique that replaces it is column checksums per key-range, comparing
ranges rather than rows. At this size, in-memory is correct and simpler.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
from pandas.api.types import is_datetime64_any_dtype, is_timedelta64_dtype

# Primary key per table. Without these there is no diff, only a row count --
# which is the difference between "something changed" and "this changed".
PRIMARY_KEYS: dict[str, list[str]] = {
    "dim_date": ["date_key"],
    "dim_client": ["client_key"],
    "dim_service": ["service_key"],
    "dim_provider": ["provider_key"],
    "dim_center": ["center_key"],
    "dim_payer": ["payer_key"],
    "fact_session": ["session_id"],
    "fact_authorization": ["auth_id"],
}

# Excluded from comparison: append-only operational tables whose whole purpose
# is to differ between runs. Diffing them would report noise on every run and
# train the reader to skip the report.
IGNORED_TABLES = {"run_log", "etl_watermark"}


@dataclass
class TableDiff:
    table: str
    rows_before: int = 0
    rows_after: int = 0
    added: int = 0
    removed: int = 0
    changed: int = 0
    changed_cells_by_column: dict[str, int] = field(default_factory=dict)
    error: str | None = None

    @property
    def unchanged(self) -> int:
        return max(self.rows_after - self.added - self.changed, 0)

    @property
    def is_identical(self) -> bool:
        return not (self.added or self.removed or self.changed) and not self.error

    def headline(self) -> str:
        if self.error:
            return f"could not compare ({self.error})"
        if self.is_identical:
            return "identical"
        parts = []
        if self.added:
            parts.append(f"+{self.added:,}")
        if self.removed:
            parts.append(f"-{self.removed:,}")
        if self.changed:
            top = sorted(self.changed_cells_by_column.items(),
                         key=lambda kv: -kv[1])[:2]
            columns = ", ".join(f"{c}" for c, _ in top)
            parts.append(f"~{self.changed:,} ({columns})")
        return " ".join(parts)


@dataclass
class WarehouseDiff:
    before: str
    after: str
    tables: list[TableDiff] = field(default_factory=list)
    ruleset_before: str | None = None
    ruleset_after: str | None = None

    @property
    def definitions_changed(self) -> bool:
        """Did the rules and definitions move, as well as the data?

        This is the difference between "the numbers changed" and "the numbers
        changed because we changed what they mean", and a reader cannot tell
        them apart from row counts alone.

        The hash covers every threshold that can change a verdict plus the
        unit-conversion table, so it moves when somebody edits the at-risk
        window from thirty days to ninety, or corrects a service's
        minutes-per-unit. Neither is a data error -- no check can object,
        because no rule was broken and the pipeline has no way to know which
        value was intended. What it can do is say so.
        """
        return bool(self.ruleset_before and self.ruleset_after
                    and self.ruleset_before != self.ruleset_after)

    @property
    def is_identical(self) -> bool:
        return all(t.is_identical for t in self.tables)

    @property
    def total_changed_rows(self) -> int:
        return sum(t.added + t.removed + t.changed for t in self.tables)


NULL_SENTINEL = "\x00<null>"
"""Stands in for a null during comparison.

A value that cannot occur in the data, so a real cell can never collide with
it. Anything printable could: a column that legitimately contains the string
"null" would then compare equal to an actual null."""


def _stringify(series: pd.Series) -> pd.Series:
    """Render a column as text, with nulls made comparable."""
    return series.astype(object).where(series.notna(), NULL_SENTINEL).astype(str)


def _as_number(series: pd.Series) -> pd.Series | None:
    """The column as floats, or ``None`` if it is not a numeric column.

    Numbers are compared as numbers, not as text, because the two sides can
    legitimately disagree on storage. The moment a single null appears in an
    integer column pandas widens it to float64, and SQLite will hand back
    ``4`` from one build and ``4.0`` from the next for the same unchanged
    value. Compared as strings those differ; as numbers they do not, and it
    is the number that is the data.

    Datetimes are excluded deliberately. ``to_numeric`` will happily turn
    them into epoch nanoseconds, but it renders ``NaT`` as
    ``-9223372036854775808`` -- a real, comparable integer -- which would
    quietly reintroduce the null bug in a different disguise.
    """
    if is_datetime64_any_dtype(series) or is_timedelta64_dtype(series):
        return None
    try:
        return pd.to_numeric(series, errors="raise").astype(float)
    except (TypeError, ValueError):
        return None


def _column_differs(before: pd.Series, after: pd.Series) -> pd.Series:
    """Element-wise inequality, with null equal to null.

    Two nulls mean the same thing -- this value is absent -- and a diff that
    calls that a change is not describing the data.
    """
    b_num, a_num = _as_number(before), _as_number(after)
    if b_num is not None and a_num is not None:
        both_null = b_num.isna() & a_num.isna()
        return b_num.ne(a_num) & ~both_null
    return _stringify(before).ne(_stringify(after))


def _read(conn: sqlite3.Connection, table: str) -> pd.DataFrame:
    return pd.read_sql_query(f"SELECT * FROM {table}", conn)


def diff_frames(before: pd.DataFrame, after: pd.DataFrame,
                keys: list[str], table: str = "") -> TableDiff:
    """Key-based comparison of two versions of the same table."""
    result = TableDiff(table=table, rows_before=len(before), rows_after=len(after))

    missing = [k for k in keys if k not in before.columns or k not in after.columns]
    if missing:
        result.error = f"missing key column(s): {', '.join(missing)}"
        return result

    shared = [c for c in after.columns if c in before.columns and c not in keys]

    # The sentinel goes on the *keys* as well as the values, and for the same
    # reason. A null key is not equal to itself, so a row keyed on one would be
    # reported as both added and removed against an identical frame, on every
    # run. That is INC-002 relocated: the first fix put the sentinel on the
    # cells and left the index alone, and a property test found the gap by
    # generating a frame with a null primary key.
    #
    # A warehouse primary key should never be null, and in this pipeline none
    # is. But SQLite does not enforce that and this module reads whatever
    # `read_sql_query` returns, so the guarantee would be living in a different
    # module from the code depending on it.
    b = before.copy()
    a = after.copy()
    for frame in (b, a):
        for key in keys:
            if frame[key].isna().any():
                frame[key] = _stringify(frame[key])

    b = b.set_index(keys, drop=False)
    a = a.set_index(keys, drop=False)
    before_keys, after_keys = set(b.index), set(a.index)

    result.added = len(after_keys - before_keys)
    result.removed = len(before_keys - after_keys)

    common = sorted(before_keys & after_keys, key=str)
    if not common or not shared:
        return result

    b_common = b.loc[list(common), shared]
    a_common = a.loc[list(common), shared]

    # Compare column by column, numerically where the column is numeric and
    # as text otherwise, with nulls mapped to a sentinel on the text path.
    #
    # Every part of that sentence is load-bearing. Numerically, because an
    # int64 column becomes float64 the moment a null appears in it and `4` is
    # not the string `4.0`. As text otherwise, because a category label has no
    # numeric reading. And the sentinel, because **NaN does not equal NaN**.
    # Without it, every
    # null compares as a difference and the diff reports a table as completely
    # rewritten when it is byte-identical. The first version of this module did
    # exactly that: it claimed 49,227 changed rows between two runs of a
    # deterministic pipeline, and every one of them was a null comparing
    # unequal to itself.
    #
    # It is worth noticing what caught it. Not a test -- the idempotency test
    # passed throughout, because its checksum renders both nulls as an empty
    # CSV field and never sees the difference. The number was simply
    # implausible, and reading the output is what found it. That is the same
    # way the unit-of-measure defect in docs/ANOMALY.md was found.
    differs = pd.DataFrame(
        {column: _column_differs(b_common[column], a_common[column])
         for column in shared},
        index=b_common.index,
    )

    changed_rows = differs.any(axis=1)
    result.changed = int(changed_rows.sum())
    result.changed_cells_by_column = {
        column: int(count)
        for column, count in differs.sum().items() if count
    }
    return result


def diff_warehouses(before_path: Path, after_path: Path) -> WarehouseDiff:
    """Compare every modelled table across two warehouse files."""
    out = WarehouseDiff(before=str(before_path), after=str(after_path))
    if not before_path.exists() or not after_path.exists():
        return out

    before_conn = sqlite3.connect(before_path)
    after_conn = sqlite3.connect(after_path)
    try:
        names = {r[0] for r in after_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for table in sorted(names - IGNORED_TABLES):
            keys = PRIMARY_KEYS.get(table)
            if not keys:
                continue
            try:
                out.tables.append(diff_frames(
                    _read(before_conn, table), _read(after_conn, table), keys, table))
            except (sqlite3.Error, KeyError) as exc:
                out.tables.append(TableDiff(table=table, error=str(exc)))
    finally:
        before_conn.close()
        after_conn.close()
    return out


def render(diff: WarehouseDiff) -> str:
    lines = ["# Run-over-run data diff", ""]

    # Before anything about rows. If the definitions moved, that is the
    # explanation for whatever follows, and a reader who works it out three
    # tables later has already formed a wrong theory.
    if diff.definitions_changed:
        lines += [
            f"> **The definitions changed in this run.** Rule set "
            f"`{diff.ruleset_before}` → `{diff.ruleset_after}`.",
            ">",
            "> Some threshold, window or unit conversion is not what it was "
            "last time, so any difference below may be a change in what the "
            "numbers *mean* rather than in the underlying care. No check "
            "objected because no rule was broken — the pipeline cannot know "
            "which value was intended. Confirm the change was deliberate "
            "before acting on the figures, and before comparing them with "
            "anything published earlier.",
            "",
        ]

    if not diff.tables:
        lines.append("No previous warehouse to compare against — this is the "
                     "first run, or the last one was quarantined.")
        return "\n".join(lines) + "\n"

    if diff.is_identical and not diff.definitions_changed:
        lines.append("**Nothing changed.** Every modelled table holds the same "
                     "rows, under the same primary keys, with the same value in "
                     "every compared cell as the previous published build.")
        lines.append("")
        lines.append("Not that the two files are identical, which they are not: "
                     "`run_log` gains a row on every run and is excluded from the "
                     "comparison, so the bytes differ by design. What is asserted "
                     "here is a value-level match across the modelled tables — the "
                     "same property the idempotency test asserts, observed on the "
                     "real warehouse rather than in a fixture.")
        lines.append("")
    elif diff.is_identical:
        lines.append("**No row changed**, but the definitions did — see above. "
                     "Identical data under different rules is still worth "
                     "knowing about, because the next run's numbers will move.")
        lines.append("")
    else:
        lines.append(f"**{diff.total_changed_rows:,} rows differ** from the "
                     f"previous published build.")
        lines.append("")

    lines += ["| Table | Before | After | Added | Removed | Changed | Where |",
              "|---|---:|---:|---:|---:|---:|---|"]
    for t in sorted(diff.tables, key=lambda t: -(t.added + t.removed + t.changed)):
        where = ", ".join(
            f"`{c}` ({n:,})" for c, n in
            sorted(t.changed_cells_by_column.items(), key=lambda kv: -kv[1])[:3]
        ) or "—"
        lines.append(
            f"| `{t.table}` | {t.rows_before:,} | {t.rows_after:,} | {t.added:,} "
            f"| {t.removed:,} | {t.changed:,} | {where} |"
        )
    lines.append("")
    lines.append("*Compared by primary key, not by row order. Cell counts are per "
                 "column, so a single-column change and a whole-table rewrite do "
                 "not look alike.*")
    lines.append("")
    return "\n".join(lines)
