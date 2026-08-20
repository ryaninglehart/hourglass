"""End-to-end behaviour: idempotency, the gate's effect on publication, and
determinism of the generator.

These are slower than the unit tests because they run the real pipeline over
the real generated dataset. They are the ones that would have caught every
integration bug found while building this.

Every test here starts from an empty data tree -- see ``_clean_workspace`` in
conftest.py. Nothing below may assert against state an earlier test left
behind, because in a randomised order there is no earlier test.
"""

from __future__ import annotations

import json
import re

import pandas as pd
import pytest

from hourglass import model, pipeline
from hourglass.generate import generate


@pytest.fixture(scope="module")
def rerun_in_place(workspace, acknowledgement):
    """Two runs of the pipeline over one warehouse, each one snapshotted.

    The idempotency and append-only properties are both about what a *second*
    run does to the artifacts of the first, so the two runs have to share a
    directory. Everything the tests need is read out here and returned as
    values, so the tests themselves are readers and the shared directory can
    be emptied the moment this fixture returns.
    """
    def once() -> dict:
        pipeline.run(acknowledgements=acknowledgement, prefer_s3=False, quiet=True)
        conn = model.connect(workspace.warehouse)
        try:
            return {
                "checksums": model.warehouse_checksums(conn),
                "run_log": pd.read_sql_query(
                    "SELECT * FROM run_log ORDER BY run_id", conn),
            }
        finally:
            conn.close()

    workspace.reset()
    return once(), once()


class TestGenerator:
    def test_is_deterministic(self, tmp_path):
        """Same seed, same bytes. Without this the idempotency test proves
        nothing -- a stable warehouse over unstable input is luck."""
        a = generate(out_dir=tmp_path / "a")
        b = generate(out_dir=tmp_path / "b")
        for key in a:
            assert a[key].read_bytes() == b[key].read_bytes(), key

    def test_seeds_the_defect_only_after_the_migration_date(self, tmp_path):
        paths = generate(out_dir=tmp_path / "g")
        df = pd.read_csv(paths["sessions"], dtype={"duration_uom": "object"})
        df["service_date"] = pd.to_datetime(df["service_date"])
        missing = df.loc[df["duration_uom"].isna()]
        assert len(missing) > 0
        assert (missing["service_date"] >= pd.Timestamp("2026-04-01")).all()


class TestIdempotency:
    def test_second_run_produces_identical_tables(self, rerun_in_place):
        """Run it twice, get the same warehouse.

        This is the property that makes a pipeline safe to re-run after a
        failure, and it is the question worth asking of any loader. Asserting
        it is cheap; assuming it is how you end up with double-counted facts.
        """
        first, second = rerun_in_place
        assert second["checksums"] == first["checksums"]

    def test_run_log_is_append_only(self, rerun_in_place):
        """The warehouse is rebuilt on every run; the audit trail is not.

        Asserted as a property of the two runs -- one row added, the first
        run's row still there and unaltered -- rather than as a row count.
        A count only means anything if you know how many runs preceded it,
        and in a randomised order you do not.
        """
        first, second = rerun_in_place
        before, after = first["run_log"], second["run_log"]

        assert len(after) == len(before) + 1
        carried = after[after["run_id"].isin(before["run_id"])]
        pd.testing.assert_frame_equal(carried.reset_index(drop=True), before)


class TestVerifyRollback:
    def test_a_failed_verify_restores_the_previous_published_build(
            self, workspace, acknowledgement, monkeypatch):
        """A run that dies after writing must not leave its artifacts serving.

        The warehouse has had this guarantee from the start -- quarantine,
        previous build untouched. The exports did not: a verify failure left
        the failed run's payload, digest and CSVs on disk while the console
        said nothing was published. Two runs: one good, one that fails at
        verify, and the artifacts on disk afterwards must be the first run's.
        """
        from hourglass import metrics

        workspace.reset()
        first = pipeline.run(acknowledgements=acknowledgement,
                             prefer_s3=False, quiet=True)
        assert first["published"] is True
        payload_path = pipeline.EXPORT_DIR / "dashboard_data.json"
        digest_path = pipeline.REPORT_DIR / "weekly_digest.md"
        good_payload = payload_path.read_bytes()
        good_digest = digest_path.read_bytes()

        def disagreeing_headlines(warehouse, payload):
            return [metrics.ParityResult(
                key="published.hours_unused", label="hours_unused (forced)",
                sql_value=1.0, frame_value=99999.0, tolerance=0.5)]

        monkeypatch.setattr(metrics, "check_published_headlines",
                            disagreeing_headlines)
        second = pipeline.run(acknowledgements=acknowledgement,
                              prefer_s3=False, quiet=True)
        assert second["succeeded"] is False
        assert second["run"].failed_task.name == "verify"
        # The artifacts a reader would consume are the first run's, bytes
        # for bytes -- not the failed run's, and not missing.
        assert payload_path.read_bytes() == good_payload
        assert digest_path.read_bytes() == good_digest


class TestGateBehaviour:
    def test_unacknowledged_block_halts_publication(self):
        result = pipeline.run(acknowledgements={}, prefer_s3=False, quiet=True)
        assert result["published"] is False
        assert "uom_resolution_coverage" in result["decision"].blocking_failures

    def test_a_blocked_run_does_not_report_publishing_and_verifying(self, capsys):
        """`make gate` is the first thing the README sends a reader to look at.

        `diff`, `publish` and `verify` all run on a blocked run and all three
        correctly do nothing. The progress printer took a successful return as
        a completed task and printed the past-tense label anyway, so the
        demonstration of the gate refusing to publish reported that it had
        published and verified. The orchestrator's own status stays SUCCEEDED,
        because the tasks did run and did the right thing -- what changes is
        what the console claims they did.
        """
        result = pipeline.run(acknowledgements={}, prefer_s3=False, quiet=False)
        out = capsys.readouterr().out

        assert result["published"] is False
        for task in ("diff", "publish", "verify"):
            assert re.search(rf"\[\d+/12\] {task}\s+[\d.]+s\s+not applicable", out), task
        assert "] published " not in out
        assert "] egress verified " not in out
        # The tasks that did do their work still say so.
        assert "loaded warehouse" in out
        assert "quality gates" in out

    def test_report_is_written_even_when_publication_halts(self, workspace):
        pipeline.run(acknowledgements={}, prefer_s3=False, quiet=True)
        assert (workspace.report_dir / "quality_report.md").exists()
        assert (workspace.report_dir / "quality_report.json").exists()

    def test_acknowledgement_publishes(self, published_run):
        assert published_run.result["published"] is True
        assert (published_run.export_dir / "dashboard_data.json").exists()
        assert (published_run.export_dir / "fact_session.csv").exists()

    def test_acknowledgement_reason_reaches_the_run_log(self, published_run,
                                                        acknowledgement):
        conn = model.connect(published_run.warehouse)
        try:
            row = pd.read_sql_query(
                "SELECT * FROM run_log WHERE run_id = ?", conn,
                params=(published_run.result["run_id"],)).iloc[0]
        finally:
            conn.close()
        acknowledged = json.loads(row["acknowledgements"])
        assert "uom_resolution_coverage" in acknowledged
        assert acknowledged["uom_resolution_coverage"] == (
            acknowledgement["uom_resolution_coverage"])
        assert row["ruleset_hash"]
        assert row["published"] == 1

    def test_cli_exit_code_reflects_the_verdict(self):
        """CI has to be able to fail the build on a blocked release."""
        assert pipeline.main(["--no-s3", "--quiet", "--no-regenerate"]) == 1
        assert pipeline.main([
            "--no-s3", "--quiet", "--no-regenerate",
            "--acknowledge", "uom_resolution_coverage=seeded defect, expected in tests",
        ]) == 0

    def test_short_acknowledgement_reason_is_rejected(self):
        with pytest.raises(SystemExit):
            pipeline._parse_ack(["uom_resolution_coverage=ok"])

    def test_acknowledgement_without_a_reason_is_rejected(self):
        with pytest.raises(SystemExit):
            pipeline._parse_ack(["uom_resolution_coverage"])


class TestWarehouseIntegrity:
    def test_foreign_keys_resolve(self, published_run):
        conn = model.connect(published_run.warehouse)
        try:
            orphans = pd.read_sql_query("""
                SELECT COUNT(*) AS n
                FROM fact_session f
                LEFT JOIN dim_client  c ON c.client_key  = f.client_key
                LEFT JOIN dim_date    d ON d.date_key    = f.date_key
                LEFT JOIN dim_service s ON s.service_key = f.service_key
                WHERE c.client_key IS NULL OR d.date_key IS NULL OR s.service_key IS NULL
            """, conn).iloc[0]["n"]
        finally:
            conn.close()
        assert orphans == 0

    def test_no_duplicate_session_ids(self, published_run):
        conn = model.connect(published_run.warehouse)
        try:
            n = pd.read_sql_query(
                "SELECT COUNT(*) - COUNT(DISTINCT session_id) AS n FROM fact_session",
                conn).iloc[0]["n"]
        finally:
            conn.close()
        assert n == 0

    def test_unresolved_rows_carry_no_measure(self, published_run):
        conn = model.connect(published_run.warehouse)
        try:
            leaked = pd.read_sql_query(
                "SELECT COUNT(*) AS n FROM fact_session "
                "WHERE uom_resolved = 0 AND minutes_delivered > 0", conn).iloc[0]["n"]
        finally:
            conn.close()
        assert leaked == 0

    def test_every_client_has_exactly_one_current_row(self, published_run):
        conn = model.connect(published_run.warehouse)
        try:
            bad = pd.read_sql_query("""
                SELECT COUNT(*) AS n FROM (
                    SELECT client_id, SUM(is_current) AS c
                    FROM dim_client GROUP BY client_id HAVING c != 1
                )
            """, conn).iloc[0]["n"]
        finally:
            conn.close()
        assert bad == 0


class TestPublishedOutputs:
    def test_dashboard_payload_is_internally_consistent(self, published_run):
        payload = json.loads(
            (published_run.export_dir / "dashboard_data.json").read_text())
        h = payload["headline"]
        assert 0 < h["pace"] < 2
        assert h["active_authorizations"] > 0
        # An exact identity. The previous disjunct (== len or >= 25) was
        # always satisfied by its right half on generated data, so a payload
        # shipping a headline count over an emptied worklist passed.
        assert len(payload["at_risk"]) == min(h["at_risk_count"], 25)
        assert payload["meta"]["synthetic"] is True

    def test_every_bi_table_is_exported(self, published_run):
        from hourglass.export import BI_TABLES
        for table in BI_TABLES:
            assert (published_run.export_dir / f"{table}.csv").exists(), table

    def test_nulls_export_as_empty_not_zero(self, published_run):
        """Power BI must read BLANK, not 0. A rate measure averaged over
        zeros-that-should-be-blank is quietly wrong."""
        text = (published_run.export_dir / "fact_session.csv").read_text()
        assert ",NULL," not in text
        assert "nan" not in text.lower()
