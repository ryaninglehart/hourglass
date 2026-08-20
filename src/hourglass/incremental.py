"""Incremental loading: watermarks, a lookback window, and merge.

The full reload elsewhere in this project is the right call at 52,000 rows and
the wrong one at fifty million. This module is the other mode, and it exists
because "we would do it incrementally at scale" is a sentence anybody can say.

Three ideas, and the second is the one people leave out.

**1. A watermark.** The highest source timestamp successfully loaded. The next
run asks the source only for rows above it. Stored in the warehouse next to the
data it describes, so it cannot drift away from the thing it is a fact about.

**2. A lookback window.** The watermark alone is wrong, and quietly. Records
arrive late and get corrected after the fact: a session entered on Friday for
Tuesday, a cancellation backdated, a duration fixed on Monday. A strict
watermark never sees any of them, because their business date is below the high
mark. So each run re-reads a window *behind* the watermark and merges what it
finds.

The window is a trade with no clean answer. Too short and late corrections are
missed for good. Too long and every run re-processes data that has not changed.
Seven days is the default here because a week covers the correction latency of
a clinical documentation workflow, and it is stated in config rather than
buried so that changing it is a decision somebody makes on purpose.

**3. Merge, not append.** Re-reading a window means seeing rows already loaded.
Appending them double-counts; deleting the window and re-inserting it is
correct and, at window scale, fast. The merge is keyed on the business key, so
a corrected row replaces its earlier version rather than joining it.

The property that makes all of this trustworthy is asserted in
``tests/test_incremental.py``: **an incremental load and a full reload of the
same source data produce identical tables.** Without that test, incremental
loading is an optimisation you hope is also correct.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pandas as pd

from .config import INCREMENTAL_LOOKBACK_DAYS

WATERMARK_DDL = """
-- GRAIN: one row per (source, table) pair being tracked.
CREATE TABLE IF NOT EXISTS etl_watermark (
    source           TEXT NOT NULL,
    target_table     TEXT NOT NULL,
    watermark_column TEXT NOT NULL,
    watermark_value  TEXT,
    lookback_days    INTEGER NOT NULL,
    rows_loaded      INTEGER NOT NULL DEFAULT 0,
    updated_at_utc   TEXT NOT NULL,
    PRIMARY KEY (source, target_table)
);
"""

DEFAULT_LOOKBACK_DAYS = INCREMENTAL_LOOKBACK_DAYS


@dataclass
class Watermark:
    source: str
    target_table: str
    watermark_column: str
    watermark_value: str | None
    lookback_days: int = DEFAULT_LOOKBACK_DAYS

    @property
    def read_from(self) -> str | None:
        """The lower bound the next extract should actually request.

        Watermark minus the lookback window. Returning the bare watermark here
        is the bug this whole module is built around, and it is invisible until
        somebody notices a month later that late-entered sessions never made it
        into the warehouse.
        """
        if self.watermark_value is None:
            return None
        value = pd.to_datetime(self.watermark_value)
        return (value - timedelta(days=self.lookback_days)).strftime("%Y-%m-%d")


def ensure_watermark_table(conn: sqlite3.Connection) -> None:
    conn.executescript(WATERMARK_DDL)
    conn.commit()


def read_watermark(
    conn: sqlite3.Connection,
    source: str,
    target_table: str,
    watermark_column: str,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> Watermark:
    ensure_watermark_table(conn)
    row = conn.execute(
        "SELECT watermark_value, lookback_days FROM etl_watermark "
        "WHERE source = ? AND target_table = ?",
        (source, target_table),
    ).fetchone()
    # The caller's lookback wins over the stored one. Storing it is useful as
    # a record of what the last run applied; letting it override the argument
    # would mean a caller could ask for a 90-day window, get 7, and be told in
    # the returned report that it got 90.
    return Watermark(
        source=source,
        target_table=target_table,
        watermark_column=watermark_column,
        watermark_value=row[0] if row else None,
        lookback_days=lookback_days,
    )


def write_watermark(conn: sqlite3.Connection, wm: Watermark, rows_loaded: int) -> None:
    ensure_watermark_table(conn)
    conn.execute(
        "INSERT INTO etl_watermark (source, target_table, watermark_column, "
        "watermark_value, lookback_days, rows_loaded, updated_at_utc) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(source, target_table) DO UPDATE SET "
        "  watermark_value = excluded.watermark_value, "
        "  lookback_days   = excluded.lookback_days, "
        "  rows_loaded     = excluded.rows_loaded, "
        "  updated_at_utc  = excluded.updated_at_utc",
        (wm.source, wm.target_table, wm.watermark_column, wm.watermark_value,
         wm.lookback_days, rows_loaded,
         datetime.now(UTC).isoformat(timespec="seconds")),
    )
    conn.commit()


def select_incremental(
    df: pd.DataFrame, watermark: Watermark
) -> tuple[pd.DataFrame, str | None]:
    """Rows at or after (watermark - lookback), plus the new high-water mark.

    The comparison is ``>=`` rather than ``>`` on purpose. With a date-grain
    watermark, ``>`` drops every row sharing the highest date -- and there is
    always more than one session on the last day. Combined with a merge that
    replaces by key, the overlap is harmless; without the ``=``, the loss is
    silent.
    """
    if df.empty:
        return df, watermark.watermark_value

    column = watermark.watermark_column
    values = pd.to_datetime(df[column])
    new_high = values.max().strftime("%Y-%m-%d")

    lower = watermark.read_from
    if lower is None:
        return df.copy(), new_high

    selected = df.loc[values >= pd.Timestamp(lower)].copy()
    high = max(new_high, watermark.watermark_value or "")
    return selected, high


def merge_rows(
    conn: sqlite3.Connection,
    table: str,
    df: pd.DataFrame,
    key_columns: list[str],
) -> dict[str, int]:
    """Delete-then-insert by business key, inside one transaction.

    SQLite has ``INSERT ... ON CONFLICT``, but it needs a unique index on the
    conflict target and it updates column by column. Delete-then-insert on the
    key set is simpler to reason about, handles a changed column set, and at
    window scale the cost difference is not measurable.

    The insert deliberately does **not** go through ``DataFrame.to_sql``.
    pandas commits its own transaction, which would end the one wrapping the
    delete -- and a delete that commits without its insert is data loss. That
    is the same trap documented in ``model.py``; there the answer was to build
    beside the target and rename, because the whole file is rewritten. Here the
    delete and the insert genuinely have to be atomic with respect to each
    other, so the rows go in through ``executemany`` and the transaction is
    real.
    """
    if df.empty:
        return {"deleted": 0, "inserted": 0}

    columns = list(df.columns)
    key_placeholders = " AND ".join(f"{c} = ?" for c in key_columns)
    insert_sql = (
        f"INSERT INTO {table} ({', '.join(columns)}) "
        f"VALUES ({', '.join('?' for _ in columns)})"
    )
    # NaN must become NULL: sqlite3 stores float('nan') as a float, and a NaN
    # in a numeric column silently poisons every aggregate over it.
    records = [
        tuple(None if pd.isna(v) else v for v in row)
        for row in df[columns].itertuples(index=False, name=None)
    ]
    key_tuples = [
        tuple(row) for row in
        df[key_columns].drop_duplicates().itertuples(index=False, name=None)
    ]

    conn.execute("BEGIN")
    try:
        deleted = 0
        for key in key_tuples:
            deleted += conn.execute(
                f"DELETE FROM {table} WHERE {key_placeholders}", key).rowcount
        conn.executemany(insert_sql, records)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return {"deleted": deleted, "inserted": len(df)}


def load_incrementally(
    conn: sqlite3.Connection,
    table: str,
    df: pd.DataFrame,
    key_columns: list[str],
    source: str,
    watermark_column: str,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> dict:
    """One incremental cycle: read the mark, select, merge, advance the mark."""
    wm = read_watermark(conn, source, table, watermark_column, lookback_days)

    # Captured BEFORE the watermark advances. `read_from` is a live property of
    # the watermark, so reading it afterwards reports the window the *next* run
    # will use and labels it as the one this run applied -- an audit record
    # that describes a different run than the one it is attached to.
    read_from = wm.read_from
    previous_watermark = wm.watermark_value

    selected, new_high = select_incremental(df, wm)
    counts = merge_rows(conn, table, selected, key_columns)

    wm.watermark_value = new_high
    write_watermark(conn, wm, rows_loaded=len(selected))

    return {
        "table": table,
        "source_rows": len(df),
        "selected_rows": len(selected),
        "previous_watermark": previous_watermark,
        "read_from": read_from,
        "watermark": new_high,
        "lookback_days": wm.lookback_days,
        **counts,
    }
