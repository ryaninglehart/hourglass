"""Incremental loading.

The headline test is ``test_incremental_matches_a_full_reload``. Everything
else in this file exists to make that one trustworthy. Incremental loading that
is faster but occasionally wrong is worse than a slow full reload, because the
wrongness is silent and arrives in units of "a week of sessions nobody can
find".
"""

from __future__ import annotations

import sqlite3

import pandas as pd
import pytest

from hourglass import incremental
from hourglass.incremental import Watermark

TABLE_DDL = """
CREATE TABLE fact_session (
    session_id   TEXT PRIMARY KEY,
    service_date TEXT NOT NULL,
    client_id    TEXT NOT NULL,
    units        REAL NOT NULL
);
"""


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript(TABLE_DDL)
    incremental.ensure_watermark_table(c)
    yield c
    c.close()


def sessions(*specs) -> pd.DataFrame:
    return pd.DataFrame(
        [{"session_id": sid, "service_date": d, "client_id": cid, "units": u}
         for sid, d, cid, u in specs]
    )


DAY1 = sessions(
    ("S1", "2026-01-05", "C1", 12.0),
    ("S2", "2026-01-05", "C2", 8.0),
    ("S3", "2026-01-06", "C1", 12.0),
)
DAY2 = sessions(
    ("S4", "2026-01-20", "C1", 12.0),
    ("S5", "2026-01-21", "C2", 4.0),
)


class TestWatermark:
    def test_starts_empty(self, conn):
        wm = incremental.read_watermark(conn, "ehr", "fact_session", "service_date")
        assert wm.watermark_value is None
        assert wm.read_from is None

    def test_round_trips(self, conn):
        wm = Watermark("ehr", "fact_session", "service_date", "2026-01-06")
        incremental.write_watermark(conn, wm, rows_loaded=3)
        assert incremental.read_watermark(
            conn, "ehr", "fact_session", "service_date").watermark_value == "2026-01-06"

    def test_read_from_subtracts_the_lookback(self):
        wm = Watermark("ehr", "t", "service_date", "2026-01-20", lookback_days=7)
        assert wm.read_from == "2026-01-13"

    def test_read_from_is_not_the_bare_watermark(self):
        """The bug this module is built around.

        Reading from the watermark itself means a session entered late for a
        date below the mark is never seen again. It does not error; the row
        simply never arrives.
        """
        wm = Watermark("ehr", "t", "service_date", "2026-01-20", lookback_days=7)
        assert wm.read_from != wm.watermark_value


class TestSelection:
    def test_first_run_takes_everything(self):
        wm = Watermark("ehr", "t", "service_date", None)
        selected, high = incremental.select_incremental(DAY1, wm)
        assert len(selected) == 3
        assert high == "2026-01-06"

    def test_later_run_takes_the_window(self):
        wm = Watermark("ehr", "t", "service_date", "2026-01-20", lookback_days=7)
        combined = pd.concat([DAY1, DAY2], ignore_index=True)
        selected, high = incremental.select_incremental(combined, wm)
        # 2026-01-13 onward: S4 and S5 only.
        assert set(selected["session_id"]) == {"S4", "S5"}
        assert high == "2026-01-21"

    def test_boundary_rows_are_included(self):
        """`>=`, not `>`.

        With a date-grain watermark, `>` silently drops every session sharing
        the highest date -- and there is always more than one.
        """
        wm = Watermark("ehr", "t", "service_date", "2026-01-12", lookback_days=0)
        df = sessions(("S9", "2026-01-12", "C1", 4.0))
        selected, _ = incremental.select_incremental(df, wm)
        assert len(selected) == 1

    def test_watermark_never_moves_backwards(self):
        """A late batch of old rows must not rewind the mark.

        If it did, the next run would re-read from an earlier point and the
        window would creep backwards forever.
        """
        wm = Watermark("ehr", "t", "service_date", "2026-06-01", lookback_days=7)
        _, high = incremental.select_incremental(DAY1, wm)
        assert high == "2026-06-01"


class TestMerge:
    def test_inserts_new_rows(self, conn):
        counts = incremental.merge_rows(conn, "fact_session", DAY1, ["session_id"])
        assert counts == {"deleted": 0, "inserted": 3}

    def test_replaces_rather_than_duplicating(self, conn):
        incremental.merge_rows(conn, "fact_session", DAY1, ["session_id"])
        corrected = sessions(("S1", "2026-01-05", "C1", 20.0))
        incremental.merge_rows(conn, "fact_session", corrected, ["session_id"])

        rows = pd.read_sql_query("SELECT * FROM fact_session", conn)
        assert len(rows) == 3
        assert rows.loc[rows["session_id"] == "S1", "units"].iloc[0] == 20.0

    def test_reloading_the_same_batch_is_a_no_op(self, conn):
        incremental.merge_rows(conn, "fact_session", DAY1, ["session_id"])
        incremental.merge_rows(conn, "fact_session", DAY1, ["session_id"])
        assert pd.read_sql_query(
            "SELECT COUNT(*) c FROM fact_session", conn).iloc[0]["c"] == 3

    def test_rolls_back_on_failure(self, conn):
        """A delete that commits without its insert is data loss."""
        incremental.merge_rows(conn, "fact_session", DAY1, ["session_id"])
        broken = DAY1.copy()
        broken["units"] = None            # NOT NULL violation on insert

        with pytest.raises(sqlite3.IntegrityError):
            incremental.merge_rows(conn, "fact_session", broken, ["session_id"])

        rows = pd.read_sql_query("SELECT * FROM fact_session", conn)
        assert len(rows) == 3
        assert rows["units"].notna().all()


class TestEquivalenceToFullReload:
    """The property that makes incremental loading trustworthy."""

    def test_incremental_matches_a_full_reload(self, conn):
        full_conn = sqlite3.connect(":memory:")
        full_conn.executescript(TABLE_DDL)
        try:
            everything = pd.concat([DAY1, DAY2], ignore_index=True)
            everything.to_sql("fact_session", full_conn, if_exists="append", index=False)
            expected = pd.read_sql_query(
                "SELECT * FROM fact_session ORDER BY session_id", full_conn)

            # Incrementally: day one, then everything (the second run sees the
            # day-one rows again inside its lookback window and must not
            # duplicate them).
            incremental.load_incrementally(
                conn, "fact_session", DAY1, ["session_id"], "ehr", "service_date")
            incremental.load_incrementally(
                conn, "fact_session", everything, ["session_id"], "ehr", "service_date")

            actual = pd.read_sql_query(
                "SELECT * FROM fact_session ORDER BY session_id", conn)
            pd.testing.assert_frame_equal(actual, expected)
        finally:
            full_conn.close()

    def test_a_late_correction_is_picked_up(self, conn):
        """The reason the lookback window exists.

        S1 happened on 5 January. The watermark has moved to 21 January. On the
        22nd, somebody corrects S1's duration. A strict watermark never sees
        it; the lookback window does -- provided the correction lands inside
        it.
        """
        incremental.load_incrementally(
            conn, "fact_session", DAY1, ["session_id"], "ehr", "service_date",
            lookback_days=30)
        incremental.load_incrementally(
            conn, "fact_session", pd.concat([DAY1, DAY2], ignore_index=True),
            ["session_id"], "ehr", "service_date", lookback_days=30)

        corrected = pd.concat([DAY1, DAY2], ignore_index=True)
        corrected.loc[corrected["session_id"] == "S1", "units"] = 99.0

        incremental.load_incrementally(
            conn, "fact_session", corrected, ["session_id"], "ehr", "service_date",
            lookback_days=30)

        rows = pd.read_sql_query("SELECT * FROM fact_session", conn)
        assert rows.loc[rows["session_id"] == "S1", "units"].iloc[0] == 99.0
        assert len(rows) == 5

    def test_a_correction_outside_the_window_is_missed_and_that_is_documented(self, conn):
        """The honest limitation, asserted so it cannot be forgotten.

        A one-day lookback cannot see a correction to a session three weeks
        old. This is not a defect to fix in code -- it is the trade the window
        represents, and the only defence is choosing the window deliberately
        and knowing what it costs.
        """
        everything = pd.concat([DAY1, DAY2], ignore_index=True)
        incremental.load_incrementally(
            conn, "fact_session", everything, ["session_id"], "ehr", "service_date",
            lookback_days=1)

        corrected = everything.copy()
        corrected.loc[corrected["session_id"] == "S1", "units"] = 99.0
        incremental.load_incrementally(
            conn, "fact_session", corrected, ["session_id"], "ehr", "service_date",
            lookback_days=1)

        rows = pd.read_sql_query("SELECT * FROM fact_session", conn)
        assert rows.loc[rows["session_id"] == "S1", "units"].iloc[0] == 12.0


class TestReporting:
    def test_load_reports_what_it_did(self, conn):
        out = incremental.load_incrementally(
            conn, "fact_session", DAY1, ["session_id"], "ehr", "service_date")
        assert out["source_rows"] == 3
        assert out["selected_rows"] == 3
        assert out["inserted"] == 3
        assert out["watermark"] == "2026-01-06"
        assert out["lookback_days"] == incremental.DEFAULT_LOOKBACK_DAYS
