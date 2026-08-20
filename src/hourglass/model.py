"""Load the star schema into SQLite.

The load is a full replace. At this volume that is both simpler and safer than
an incremental merge, and it buys the property the pipeline most needs: running
it twice produces the same rows with the same values, which is what
:func:`table_checksum` and the idempotency test compare. The run_log is the one
exception -- it is append-only, because the history of what ran is not
something a re-run should erase, and it is why the *file* differs between two
runs that produced identical tables.

Atomicity is handled by building into a scratch file and swapping it into place
with a single rename, rather than by wrapping the inserts in a transaction.
pandas commits per table when writing through ``to_sql``, so an enclosing
transaction would look like protection without providing any.

Idempotency is asserted, not assumed.
tests/test_pipeline.py::TestIdempotency runs the whole pipeline twice and
compares a checksum of every table.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

import pandas as pd

try:
    import fcntl
except ImportError:                       # pragma: no cover - Windows
    # There is no portable advisory lock. Rather than fail to import on a
    # platform this project has never been run on, the lock below becomes a
    # no-op and :func:`atomic_build` says so in its docstring: the
    # never-a-partial-load guarantee holds everywhere, the
    # one-build-at-a-time guarantee is POSIX only.
    fcntl = None

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "sql" / "star_schema.sql"

DIMENSION_ORDER = ["dim_date", "dim_client", "dim_service", "dim_provider",
                   "dim_center", "dim_payer"]
FACT_ORDER = ["fact_session", "fact_authorization"]


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _normalise(df: pd.DataFrame) -> pd.DataFrame:
    """SQLite has no boolean or timestamp type; convert once, here."""
    out = df.copy()
    for col in out.columns:
        s = out[col]
        if pd.api.types.is_bool_dtype(s):
            out[col] = s.astype(int)
        elif pd.api.types.is_datetime64_any_dtype(s):
            out[col] = s.dt.strftime("%Y-%m-%d")
        elif s.dtype.name in ("boolean", "Boolean"):
            out[col] = s.astype("Int64")
    return out


def load_star(conn: sqlite3.Connection, frames: dict[str, pd.DataFrame]) -> dict[str, int]:
    """Load dimensions then facts.

    Dimensions first is not stylistic: foreign keys are on, so a fact row
    inserted before its dimension member would be rejected.

    This function does not manage a transaction. pandas commits per table, so
    wrapping it in one would not give the atomicity it appears to. Atomicity is
    provided a level up by :func:`atomic_build`, which writes a fresh database
    file and swaps it into place only once every table has loaded.
    """
    counts: dict[str, int] = {}
    for table in DIMENSION_ORDER + FACT_ORDER:
        if table not in frames:
            continue
        df = _normalise(frames[table])
        df.to_sql(table, conn, if_exists="append", index=False)
        counts[table] = len(df)
    conn.commit()
    return counts


@contextmanager
def _exclusive_build_lock(directory: Path):
    """Hold an exclusive advisory lock on an output directory while building.

    ``flock`` on the directory itself, so nothing is left behind to clean up,
    and non-blocking: a second concurrent build fails immediately, naming the
    directory, rather than queueing behind a build it cannot see.

    Refusing is the better of the two available outcomes. Two builds that both
    proceed each read ``run_log`` from the current warehouse before either
    swaps, so whichever swaps second carries forward a history taken before the
    first one's row existed, and that row is gone with no error anywhere. A run
    that refuses to start has cost somebody a re-run; a run that silently drops
    another run's audit row has cost them the audit trail.

    POSIX only. ``fcntl`` does not exist on Windows, where this yields without
    a lock and concurrent builds are not serialised.
    """
    if fcntl is None:                     # pragma: no cover - Windows
        yield
        return
    fd = os.open(directory, os.O_RDONLY)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError(
                f"Another warehouse build is already running against "
                f"{directory}. Wait for it to finish and re-run: two builds "
                f"swapping the same target lose one of their audit rows."
            ) from exc
        yield
    finally:
        os.close(fd)                      # releases the lock


def atomic_build(
    target: Path,
    frames: dict[str, pd.DataFrame],
    run_log_row: dict | None = None,
) -> dict[str, int]:
    """Build the warehouse in a scratch file, then swap it in.

    A half-loaded warehouse is worse than no warehouse: it answers queries, and
    the answers are wrong. Building beside the target and replacing it with a
    single atomic rename means a reader either sees the previous complete
    warehouse or the new complete one, never a partial load.

    That guarantee holds against readers unconditionally. Against a second
    *writer* it holds on POSIX and rests on two things, both of which used to
    be missing. The scratch file is named for the run, so two builds cannot
    delete each other's half-written database; and the whole operation --
    reading the existing run_log, loading, and the rename -- runs under an
    exclusive advisory lock on the output directory, so the history one run
    carries forward cannot predate a row another run has already written. A
    second run that cannot take the lock raises rather than proceeding. See
    :func:`_exclusive_build_lock`; on Windows there is no lock and concurrent
    builds are not serialised.

    Existing run_log rows are read from the current warehouse first and carried
    across, because the record of what ran should survive a rebuild.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    with _exclusive_build_lock(target.parent):
        history = None
        if target.exists():
            # sqlite3's context manager is transactional, not a closer -- `with
            # connect(...)` leaves the handle open. On Windows that open handle
            # makes the os.replace below raise PermissionError, so close it
            # explicitly rather than waiting for garbage collection.
            existing = connect(target)
            try:
                history = pd.read_sql_query("SELECT * FROM run_log", existing)
            except Exception:
                history = None
            finally:
                existing.close()

        # Run-scoped, because a shared scratch name is a shared file: two
        # builds under one name means the second unlinks the first's in-flight
        # database and both rename onto the same target.
        run_id = (run_log_row or {}).get("run_id") or uuid.uuid4().hex[:8]
        scratch = target.with_suffix(f".building-{run_id}.db")
        scratch.unlink(missing_ok=True)

        conn = connect(scratch)
        try:
            conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            conn.commit()
            counts = load_star(conn, frames)
            if history is not None and len(history):
                history.to_sql("run_log", conn, if_exists="append", index=False)
            if run_log_row is not None:
                pd.DataFrame([run_log_row]).to_sql("run_log", conn, if_exists="append",
                                                   index=False)
            conn.commit()
        except Exception:
            conn.close()
            scratch.unlink(missing_ok=True)
            raise
        conn.close()

        os.replace(scratch, target)  # atomic on POSIX and on Windows same-volume
    return counts


def table_checksum(conn: sqlite3.Connection, table: str) -> str:
    """Order-independent digest of a table's contents.

    Used by the idempotency test. Rows are sorted before hashing so a stable
    result does not depend on insertion order.
    """
    df = pd.read_sql_query(f"SELECT * FROM {table}", conn)
    if df.empty:
        return hashlib.sha256(b"").hexdigest()[:16]
    df = df.sort_values(list(df.columns)).reset_index(drop=True)
    payload = df.to_csv(index=False).encode()
    return hashlib.sha256(payload).hexdigest()[:16]


def warehouse_checksums(conn: sqlite3.Connection) -> dict[str, str]:
    return {t: table_checksum(conn, t) for t in DIMENSION_ORDER + FACT_ORDER}
