"""Run-over-run data diff.

The diff exists to answer a question the test suite cannot: *did this run
change anything, and was it what I meant to change?* Which means the diff
itself has to be trustworthy in a specific way — it must be silent when
nothing happened. A diff that cries wolf on a deterministic re-run is worse
than no diff, because the reader stops looking at it.

Most of what follows is about that: false positives, and the one that
actually happened.
"""

from __future__ import annotations

import sqlite3

import pandas as pd

from hourglass import diff
from hourglass.diff import NULL_SENTINEL


def frame(*rows, columns=("session_id", "minutes", "reason")) -> pd.DataFrame:
    return pd.DataFrame(list(rows), columns=list(columns))


class TestIdenticalInputs:
    def test_a_frame_against_itself_reports_nothing(self):
        f = frame(("S1", 30.0, "a"), ("S2", 45.0, "b"))
        result = diff.diff_frames(f, f.copy(), ["session_id"])
        assert result.is_identical
        assert result.changed == 0
        assert result.changed_cells_by_column == {}

    def test_row_order_is_not_a_change(self):
        """A relational table has no order. A positional diff would report
        every row as moved and the report would be discarded."""
        before = frame(("S1", 30.0, "a"), ("S2", 45.0, "b"), ("S3", 60.0, "c"))
        after = before.iloc[::-1].reset_index(drop=True)
        result = diff.diff_frames(before, after, ["session_id"])
        assert result.is_identical

    def test_headline_of_an_identical_table(self):
        f = frame(("S1", 30.0, "a"))
        assert diff.diff_frames(f, f.copy(), ["session_id"]).headline() == "identical"


class TestNullHandling:
    """The regression that motivated this file.

    The first version of `diff.py` compared with `astype(str)`, which in
    pandas 3.0 leaves NaN as NaN. `NaN != NaN`, so every null in the
    warehouse counted as a changed cell. The diff announced **49,227 changed
    rows** between two runs of a pipeline that is deterministic by
    construction.

    Nothing caught it. The idempotency test passed the whole time, because
    its checksum writes CSV and both nulls render as an empty field. What
    caught it was the number being implausible, and somebody reading it.
    """

    def test_null_equals_null(self):
        before = frame(("S1", 30.0, None), ("S2", 45.0, None))
        result = diff.diff_frames(before, before.copy(), ["session_id"])
        assert result.changed == 0, (
            "nulls compared unequal to themselves — the 49,227-row bug"
        )

    def test_an_all_null_column_is_not_a_rewrite(self):
        before = frame(*[(f"S{i}", 30.0, None) for i in range(50)])
        result = diff.diff_frames(before, before.copy(), ["session_id"])
        assert result.changed == 0

    def test_null_to_value_is_a_change(self):
        """Guards the tests above from passing by comparing nothing."""
        before = frame(("S1", 30.0, None))
        after = frame(("S1", 30.0, "missing_uom"))
        result = diff.diff_frames(before, after, ["session_id"])
        assert result.changed == 1
        assert result.changed_cells_by_column == {"reason": 1}

    def test_value_to_null_is_a_change(self):
        before = frame(("S1", 30.0, "missing_uom"))
        after = frame(("S1", 30.0, None))
        result = diff.diff_frames(before, after, ["session_id"])
        assert result.changed == 1

    def test_the_sentinel_cannot_collide_with_real_data(self):
        """A printable sentinel would make the literal string equal to a null.

        This is why the sentinel contains a NUL byte rather than the word
        "null" — a column that legitimately holds "null" as text would
        otherwise compare equal to an absent value.
        """
        before = frame(("S1", 30.0, None))
        after = frame(("S1", 30.0, "null"))
        assert diff.diff_frames(before, after, ["session_id"]).changed == 1
        assert "\x00" in NULL_SENTINEL


class TestDtypeDrift:
    def test_int_versus_float_is_not_a_change(self):
        """A column becomes float64 the moment a null appears in it. That is
        a storage detail, not a data change."""
        before = pd.DataFrame({"session_id": ["S1"], "units": [4]})
        after = pd.DataFrame({"session_id": ["S1"], "units": [4.0]})
        assert diff.diff_frames(before, after, ["session_id"]).changed == 0

    def test_nulls_in_a_numeric_column_are_equal(self):
        """The numeric path needs its own null guard.

        `NaN != NaN` is true of floats, not only of strings, so fixing the
        text comparison and not this one would have moved the bug rather
        than removed it.
        """
        before = pd.DataFrame({"session_id": ["S1", "S2"],
                               "units": [4.0, float("nan")]})
        assert diff.diff_frames(before, before.copy(), ["session_id"]).changed == 0

    def test_a_datetime_column_is_compared_as_text_not_as_epoch(self):
        """`to_numeric` turns NaT into a real, comparable integer. Excluding
        datetimes from the numeric path is what stops that."""
        before = pd.DataFrame({"session_id": ["S1", "S2"],
                               "period_end": pd.to_datetime(["2026-09-01", None])})
        assert diff.diff_frames(before, before.copy(), ["session_id"]).changed == 0

    def test_a_real_numeric_change_still_registers(self):
        before = pd.DataFrame({"session_id": ["S1"], "units": [4]})
        after = pd.DataFrame({"session_id": ["S1"], "units": [4.5]})
        assert diff.diff_frames(before, after, ["session_id"]).changed == 1


class TestAddedAndRemoved:
    def test_counts_added_rows(self):
        before = frame(("S1", 30.0, "a"))
        after = frame(("S1", 30.0, "a"), ("S2", 45.0, "b"))
        result = diff.diff_frames(before, after, ["session_id"])
        assert (result.added, result.removed, result.changed) == (1, 0, 0)

    def test_counts_removed_rows(self):
        before = frame(("S1", 30.0, "a"), ("S2", 45.0, "b"))
        after = frame(("S1", 30.0, "a"))
        result = diff.diff_frames(before, after, ["session_id"])
        assert (result.added, result.removed, result.changed) == (0, 1, 0)

    def test_a_replaced_row_is_an_add_and_a_remove_not_a_change(self):
        """Different key, different row. Calling it a change would imply the
        two are the same entity."""
        before = frame(("S1", 30.0, "a"))
        after = frame(("S2", 30.0, "a"))
        result = diff.diff_frames(before, after, ["session_id"])
        assert (result.added, result.removed, result.changed) == (1, 1, 0)

    def test_unchanged_never_goes_negative(self):
        before = frame(("S1", 30.0, "a"), ("S2", 45.0, "b"))
        after = frame(("S3", 60.0, "c"))
        assert diff.diff_frames(before, after, ["session_id"]).unchanged == 0


class TestPerColumnAttribution:
    def test_names_the_column_that_moved(self):
        """"412 rows changed" is a shrug. "412 rows changed, all in
        minutes_delivered" is a diagnosis."""
        before = frame(*[(f"S{i}", 30.0, "a") for i in range(10)])
        after = frame(*[(f"S{i}", 45.0, "a") for i in range(10)])
        result = diff.diff_frames(before, after, ["session_id"])
        assert result.changed == 10
        assert result.changed_cells_by_column == {"minutes": 10}

    def test_counts_cells_not_just_rows(self):
        before = frame(("S1", 30.0, "a"), ("S2", 45.0, "b"))
        after = frame(("S1", 31.0, "z"), ("S2", 45.0, "b"))
        result = diff.diff_frames(before, after, ["session_id"])
        assert result.changed == 1
        assert result.changed_cells_by_column == {"minutes": 1, "reason": 1}

    def test_headline_names_the_top_columns(self):
        before = frame(("S1", 30.0, "a"))
        after = frame(("S1", 45.0, "a"))
        assert "minutes" in diff.diff_frames(before, after, ["session_id"]).headline()


class TestSchemaChanges:
    def test_a_missing_key_column_is_an_error_not_a_crash(self):
        before = frame(("S1", 30.0, "a"))
        after = pd.DataFrame({"other_id": ["S1"], "minutes": [30.0]})
        result = diff.diff_frames(before, after, ["session_id"])
        assert result.error is not None
        assert "session_id" in result.error
        assert not result.is_identical

    def test_a_new_column_is_not_reported_as_a_change(self):
        """Only shared columns are compared. A schema change is a different
        kind of event and pretending it is a value change would bury it."""
        before = frame(("S1", 30.0, "a"))
        after = pd.DataFrame({"session_id": ["S1"], "minutes": [30.0],
                              "reason": ["a"], "new_column": ["x"]})
        assert diff.diff_frames(before, after, ["session_id"]).changed == 0

    def test_a_table_with_only_key_columns_compares_cleanly(self):
        before = pd.DataFrame({"session_id": ["S1", "S2"]})
        after = pd.DataFrame({"session_id": ["S1", "S2"]})
        assert diff.diff_frames(before, after, ["session_id"]).is_identical


class TestEmptyInputs:
    def test_two_empty_frames(self):
        empty = frame()
        assert diff.diff_frames(empty, empty.copy(), ["session_id"]).is_identical

    def test_everything_added(self):
        result = diff.diff_frames(frame(), frame(("S1", 30.0, "a")), ["session_id"])
        assert result.added == 1

    def test_everything_removed(self):
        result = diff.diff_frames(frame(("S1", 30.0, "a")), frame(), ["session_id"])
        assert result.removed == 1


class TestCompositeKeys:
    def test_compares_on_more_than_one_column(self):
        before = pd.DataFrame({"client_key": [1, 1], "valid_from": ["2026-01-01",
                               "2026-04-01"], "tier": ["A", "B"]})
        after = before.copy()
        after.loc[1, "tier"] = "C"
        result = diff.diff_frames(before, after, ["client_key", "valid_from"])
        assert result.changed == 1
        assert result.added == 0


class TestWarehouseLevel:
    def _warehouse(self, path, minutes: float = 30.0, reason=None):
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE fact_session "
                     "(session_id TEXT, minutes_delivered REAL, "
                     "unresolved_reason TEXT)")
        conn.executemany(
            "INSERT INTO fact_session VALUES (?, ?, ?)",
            [(f"S{i}", minutes, reason) for i in range(20)])
        conn.execute("CREATE TABLE run_log (run_id TEXT)")
        conn.execute("INSERT INTO run_log VALUES ('differs-every-run')")
        conn.commit()
        conn.close()

    def test_two_identical_warehouses(self, tmp_path):
        a, b = tmp_path / "a.db", tmp_path / "b.db"
        self._warehouse(a)
        self._warehouse(b)
        result = diff.diff_warehouses(a, b)
        assert result.is_identical
        assert result.total_changed_rows == 0

    def test_identical_warehouses_full_of_nulls(self, tmp_path):
        """The shape of the real bug, at the level it actually appeared."""
        a, b = tmp_path / "a.db", tmp_path / "b.db"
        self._warehouse(a, reason=None)
        self._warehouse(b, reason=None)
        assert diff.diff_warehouses(a, b).total_changed_rows == 0

    def test_a_real_change_is_found(self, tmp_path):
        a, b = tmp_path / "a.db", tmp_path / "b.db"
        self._warehouse(a, minutes=30.0)
        self._warehouse(b, minutes=45.0)
        result = diff.diff_warehouses(a, b)
        assert result.total_changed_rows == 20
        table = next(t for t in result.tables if t.table == "fact_session")
        assert table.changed_cells_by_column == {"minutes_delivered": 20}

    def test_operational_tables_are_ignored(self, tmp_path):
        """`run_log` differs on every run by design. Diffing it would put
        noise at the top of every report."""
        a, b = tmp_path / "a.db", tmp_path / "b.db"
        self._warehouse(a)
        self._warehouse(b)
        assert "run_log" not in {t.table for t in diff.diff_warehouses(a, b).tables}
        assert "run_log" in diff.IGNORED_TABLES

    def test_a_missing_previous_build_is_not_an_error(self, tmp_path):
        """First run, or the last build was quarantined. Neither is a fault."""
        b = tmp_path / "b.db"
        self._warehouse(b)
        result = diff.diff_warehouses(tmp_path / "absent.db", b)
        assert result.tables == []

    def test_tables_without_a_declared_key_are_skipped(self, tmp_path):
        """No key means no diff, only a row count. Skipping is honest;
        guessing a key is not."""
        a, b = tmp_path / "a.db", tmp_path / "b.db"
        for path in (a, b):
            self._warehouse(path)
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE staging_scratch (x TEXT)")
            conn.commit()
            conn.close()
        names = {t.table for t in diff.diff_warehouses(a, b).tables}
        assert names == {"fact_session"}


class TestRendering:
    def test_identical_renders_as_nothing_changed(self):
        f = frame(("S1", 30.0, "a"))
        result = diff.WarehouseDiff(before="a", after="b", tables=[
            diff.diff_frames(f, f.copy(), ["session_id"], "fact_session")])
        out = diff.render(result)
        assert "Nothing changed" in out

    def test_an_identical_result_does_not_claim_the_files_match(self):
        """It is a value diff by primary key, and `run_log` is excluded.

        Two builds of a deterministic pipeline produce identical modelled
        tables and different files -- the run log gains a row every time. The
        report used to say "byte-for-byte identical", which is a claim about
        the files and is false on every run this section is printed.
        """
        f = frame(("S1", 30.0, "a"))
        out = diff.render(diff.WarehouseDiff(before="a", after="b", tables=[
            diff.diff_frames(f, f.copy(), ["session_id"], "fact_session")]))
        assert "byte-for-byte" not in out.lower()
        assert "run_log" in out
        assert "primary key" in out

    def test_changes_render_with_a_row_count_and_a_table(self):
        before = frame(("S1", 30.0, "a"))
        after = frame(("S1", 45.0, "a"))
        result = diff.WarehouseDiff(before="a", after="b", tables=[
            diff.diff_frames(before, after, ["session_id"], "fact_session")])
        out = diff.render(result)
        assert "1 rows differ" in out
        assert "`minutes`" in out
        assert "| `fact_session` |" in out

    def test_no_previous_build_says_so(self):
        out = diff.render(diff.WarehouseDiff(before="a", after="b"))
        assert "first run" in out

    def test_biggest_change_is_listed_first(self):
        """The reader scans the top of the table and stops."""
        small_b, small_a = frame(("S1", 30.0, "a")), frame(("S1", 45.0, "a"))
        big_b = frame(*[(f"S{i}", 30.0, "a") for i in range(10)])
        big_a = frame(*[(f"S{i}", 45.0, "a") for i in range(10)])
        result = diff.WarehouseDiff(before="a", after="b", tables=[
            diff.diff_frames(small_b, small_a, ["session_id"], "dim_client"),
            diff.diff_frames(big_b, big_a, ["session_id"], "fact_session"),
        ])
        out = diff.render(result)
        assert out.index("`fact_session`") < out.index("`dim_client`")

    def test_an_errored_table_is_reported_rather_than_hidden(self):
        result = diff.WarehouseDiff(before="a", after="b", tables=[
            diff.TableDiff(table="fact_session", error="missing key column(s): x")])
        assert not result.is_identical
        assert "could not compare" in result.tables[0].headline()
