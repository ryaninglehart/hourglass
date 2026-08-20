"""The egress boundary.

These are the tests that matter most in the repository. Every other check
protects a number; these protect a person, and a false PASS here is the only
failure in this project that would be reportable rather than embarrassing.

So they are written adversarially: each one tries to get something past the
gate.
"""

from __future__ import annotations

from typing import ClassVar

import pandas as pd
import pytest

from hourglass import phi
from hourglass.phi import Sensitivity


class TestClassification:
    def test_known_columns_classify(self):
        assert phi.classify("dim_client", "client_id") is Sensitivity.DIRECT_IDENTIFIER
        assert phi.classify("dim_client", "age_band") is Sensitivity.QUASI_IDENTIFIER
        assert phi.classify("dim_payer", "payer_name") is Sensitivity.SAFE

    def test_unknown_column_defaults_to_identifier(self):
        """Unknown must fail closed.

        If a source system adds `patient_email` tomorrow, the gate has to stop
        the publish. Defaulting to SAFE would let it through and the mistake
        would only be visible after the file had been sent.
        """
        assert phi.classify("dim_client", "patient_email") is Sensitivity.DIRECT_IDENTIFIER
        assert phi.classify("a_table_that_does_not_exist", "x") is (
            Sensitivity.DIRECT_IDENTIFIER)

    def test_undeclared_columns_are_listed(self):
        assert phi.undeclared_columns("dim_payer", ["payer_key", "surprise"]) == ["surprise"]


class TestPseudonymisation:
    def test_is_stable(self):
        assert phi.pseudonymise("CLI-00001") == phi.pseudonymise("CLI-00001")

    def test_differs_per_input(self):
        assert phi.pseudonymise("CLI-00001") != phi.pseudonymise("CLI-00002")

    def test_does_not_contain_the_input(self):
        assert "00001" not in phi.pseudonymise("CLI-00001")

    def test_salt_changes_the_output(self, monkeypatch):
        """Two deployments must not produce the same surrogates.

        If they did, a surrogate leaked from one would identify the same person
        in the other, and the whole scheme would be a rainbow table away from
        useless.
        """
        before = phi.pseudonymise("CLI-00001")
        monkeypatch.setenv("HOURGLASS_PSEUDONYM_SALT", "a-different-salt")
        assert phi.pseudonymise("CLI-00001") != before

    def test_series_matches_scalar(self):
        s = pd.Series(["CLI-00001", "CLI-00002", "CLI-00001"])
        out = phi.pseudonymise_series(s, "CLI")
        assert list(out) == [phi.pseudonymise(v, "CLI") for v in s]
        assert out.iloc[0] == out.iloc[2]

    def test_handles_nulls(self):
        out = phi.pseudonymise_series(pd.Series(["CLI-00001", None]), "CLI")
        assert out.iloc[1] == ""

    def test_recognises_its_own_output(self):
        s = phi.pseudonymise_series(pd.Series(["CLI-00001", "CLI-00002"]), "CLI")
        assert phi.is_pseudonymised(s) is True

    def test_rejects_raw_values(self):
        assert phi.is_pseudonymised(pd.Series(["CLI-00001"])) is False

    def test_rejects_a_partially_transformed_column(self):
        """One raw value among surrogates is still a leak."""
        mixed = pd.Series([phi.pseudonymise("CLI-00001", "CLI"), "CLI-00002"])
        assert phi.is_pseudonymised(mixed) is False


class TestContentScanner:
    @pytest.mark.parametrize("value,pattern", [
        ("123-45-6789", "ssn"),
        ("parent@example.com", "email"),
        ("(760) 555-0134", "phone_us"),
        ("03/14/2019", "date_of_birth"),
        ("MRN: 88213", "mrn_like"),
        ("Member ID 99182773", "member_id_like"),
        ("92028-1234", "us_zip_plus4"),
    ])
    def test_detects_identifier_shapes(self, value, pattern):
        df = pd.DataFrame({"notes": [value]})
        hits = phi.scan_frame(df, "some_table")
        assert pattern in {h.pattern for h in hits}

    def test_clean_data_produces_no_hits(self):
        df = pd.DataFrame({"discipline": ["ABA", "Speech"], "code": ["97153", "92507"]})
        assert phi.scan_frame(df, "dim_service") == []

    def test_numeric_columns_are_not_scanned(self):
        """Session counts are not phone numbers."""
        df = pd.DataFrame({"units": [7605550134, 12]})
        assert phi.scan_frame(df, "fact_session") == []

    def test_findings_do_not_reproduce_the_identifier(self):
        """The report is itself a file on disk.

        A finding that quotes the value it found has copied the identifier into
        a second artifact rather than protecting the first.
        """
        df = pd.DataFrame({"notes": ["123-45-6789"]})
        hit = phi.scan_frame(df, "t")[0]
        assert "123-45-6789" not in hit.redacted_example()


class TestEgressGate:
    def _clean(self):
        return {"dim_payer": pd.DataFrame({
            "payer_key": [1], "payer_id": ["PAY-001"],
            "payer_name": ["Meridian"], "contract_type": ["value_based"]})}

    def test_clean_frames_pass(self):
        assert phi.check_egress(self._clean()) == []

    def test_raw_identifier_is_caught(self):
        frames = {"dim_client": pd.DataFrame({"client_id": ["CLI-00001"]})}
        findings = phi.check_egress(frames)
        assert any(f.reason == "unpseudonymised_identifier" for f in findings)

    def test_pseudonymised_identifier_passes(self):
        frames = {"dim_client": pd.DataFrame({
            "client_id": phi.pseudonymise_series(pd.Series(["CLI-00001"]), "CLI")})}
        assert [f for f in phi.check_egress(frames)
                if f.reason == "unpseudonymised_identifier"] == []

    def test_undeclared_column_is_caught(self):
        frames = {"dim_payer": pd.DataFrame({"payer_key": [1], "guardian_name": ["A B"]})}
        findings = phi.check_egress(frames)
        assert any(f.reason == "undeclared" and f.column == "guardian_name"
                   for f in findings)

    def test_content_match_is_caught_even_in_a_declared_safe_column(self):
        """The scanner is the layer that does not trust the classification.

        `payer_name` is declared SAFE. If a source starts writing an e-mail
        address into it, the declaration is now wrong, and only reading the
        values can tell.
        """
        frames = {"dim_payer": pd.DataFrame({
            "payer_key": [1], "payer_id": ["PAY-001"],
            "payer_name": ["billing@meridian.example.com"],
            "contract_type": ["value_based"]})}
        findings = phi.check_egress(frames)
        assert any(f.reason.startswith("content_match") for f in findings)

    def test_deidentify_makes_a_dirty_frame_publishable(self):
        dirty = {"dim_client": pd.DataFrame({
            "client_key": [1, 2], "client_id": ["CLI-00001", "CLI-00002"],
            "version": [1, 1], "age_years": [5, 9], "age_band": ["4-5", "9-12"],
            "home_center_id": ["CTR-SD", "CTR-SD"], "payer_id": ["PAY-001", "PAY-002"],
            "change_reason": ["enrollment", "enrollment"],
            "valid_from": ["2025-01-01", "2025-01-01"],
            "valid_to": ["9999-12-31", "9999-12-31"], "is_current": [1, 1]})}
        assert phi.check_egress(dirty)                       # dirty as given
        assert phi.check_egress(phi.deidentify_for_export(dirty)) == []

    def test_deidentify_preserves_joinability(self):
        """A de-identified export nobody can join is not an export."""
        frames = {
            "dim_client": pd.DataFrame({"client_id": ["CLI-00001", "CLI-00002"]}),
            "at_risk": pd.DataFrame({"client_id": ["CLI-00001"]}),
        }
        out = phi.deidentify_for_export(frames)
        assert out["at_risk"]["client_id"].iloc[0] == out["dim_client"]["client_id"].iloc[0]

    def test_deidentify_leaves_non_identifiers_untouched(self):
        frames = {"dim_client": pd.DataFrame({
            "client_id": ["CLI-00001"], "age_band": ["4-5"]})}
        out = phi.deidentify_for_export(frames)
        assert out["dim_client"]["age_band"].iloc[0] == "4-5"



class TestSampleRedaction:
    """Check samples are an egress path, and used not to be treated as one."""

    def test_identifier_columns_are_pseudonymised(self):
        out = phi.redact_records([{"client_id": "CLI-00234", "units": 12}])
        assert out[0]["client_id"] == phi.pseudonymise("CLI-00234", "CLI")
        assert out[0]["units"] == 12

    def test_identifiers_embedded_in_free_text_are_redacted(self):
        out = phi.redact_records([{"note": "duplicate of CLI-00234 on Tuesday"}])
        assert "CLI-00234" not in out[0]["note"]
        assert "[redacted]" in out[0]["note"]

    def test_provider_ids_too(self):
        out = phi.redact_records([{"provider_id": "PRV-0042"}])
        assert out[0]["provider_id"] != "PRV-0042"

    def test_check_result_redacts_at_construction(self):
        """The chokepoint. A new check cannot leak by forgetting."""
        from hourglass.quality import CheckResult, Severity
        result = CheckResult(
            name="anything", severity=Severity.WARN, passed=False, message="m",
            sample=[{"client_id": "CLI-00234", "session_id": "SES-0000001"}])
        assert result.sample[0]["client_id"] != "CLI-00234"

    def test_serialised_check_result_carries_no_raw_id(self):
        import json as _json

        from hourglass.quality import CheckResult, Severity
        result = CheckResult("c", Severity.BLOCK, False, "m",
                             sample=[{"client_id": "CLI-00234"}])
        assert "CLI-00234" not in _json.dumps(result.to_dict())


class TestUnacknowledgeableBlock:
    """A boundary that can be waived by typing a sentence is a policy."""

    def test_phi_egress_cannot_be_acknowledged(self):
        from hourglass import quality
        from hourglass.quality import CheckResult, Severity
        failed = CheckResult("phi_egress", Severity.BLOCK, False, "leak",
                             acknowledgeable=False)
        decision = quality.evaluate_gate(
            [failed], {"phi_egress": "signed off by nobody in particular"})
        assert decision.published is False
        assert decision.refused_acknowledgements == ["phi_egress"]
        assert "phi_egress" not in decision.acknowledged

    def test_other_blocks_remain_acknowledgeable(self):
        from hourglass import quality
        from hourglass.quality import CheckResult, Severity
        failed = CheckResult("uom_resolution_coverage", Severity.BLOCK, False, "m")
        decision = quality.evaluate_gate(
            [failed], {"uom_resolution_coverage": "vendor ticket DE-412 is open"})
        assert decision.published is True

    def test_the_real_check_declares_itself_unacknowledgeable(self):
        from hourglass import quality
        result = quality.check_phi_egress({"egress_findings": []})
        assert result.acknowledgeable is False


class TestArtifactScanning:
    def test_finds_a_raw_identifier_in_a_file(self, tmp_path):
        path = tmp_path / "leaky.json"
        path.write_text('{"client_id": "CLI-00234"}')
        findings = phi.scan_published_artifacts([path])
        assert findings and findings[0].pattern == "raw_client_id"

    def test_passes_a_pseudonymised_file(self, tmp_path):
        path = tmp_path / "clean.json"
        surrogate = phi.pseudonymise("CLI-00234", "CLI")
        path.write_text('{"client_id": "' + surrogate + '"}')
        assert phi.scan_published_artifacts([path]) == []

    def test_missing_files_are_skipped_not_errors(self, tmp_path):
        assert phi.scan_published_artifacts([tmp_path / "nope.csv"]) == []

    def test_surrogates_and_raw_ids_are_distinguished(self):
        """They share a prefix, which is why this must be a machine check."""
        assert phi.scan_text_for_source_ids("CLI-00234")
        assert not phi.scan_text_for_source_ids(phi.pseudonymise("CLI-00234", "CLI"))


class TestPublishedArtifacts:
    """Against the real pipeline output, because that is what ships."""

    ARTIFACTS: ClassVar[tuple[str, ...]] = (
        "bi/dim_client.csv", "bi/fact_session.csv", "bi/dashboard_data.json",
        "reports/quality_report.json", "reports/quality_report.md",
        "reports/weekly_digest.md",
    )

    def test_no_published_artifact_contains_a_raw_identifier(self, published_run):
        """The test that would have caught the real leak, and did not.

        Its predecessor loaded the dashboard payload and inspected only
        ``payload["at_risk"]``. It passed while the same file carried five raw
        identifiers forty lines away, inside a quality check's sample rows.

        The lesson is in the shape, not the fix: a test that checks the place
        you were already thinking about cannot find the leak you were not.
        This one reads whole files as text and looks for the pattern anywhere.

        It reads the artifacts of the run in ``published_run`` rather than
        whatever is in ``data/out``. The old version skipped when that
        directory was empty, which made it silent on a fresh clone and, worse,
        silent whenever an earlier test had deleted the files.
        """
        paths = [published_run.workspace.out / name for name in self.ARTIFACTS]
        for path in paths:
            assert path.exists(), f"the published run did not write {path.name}"

        # dashboard.html is built by scripts/build_dashboard.py, not by the
        # pipeline, so it is only present if someone has run it.
        dashboard = published_run.root / "dashboard.html"
        existing = paths + ([dashboard] if dashboard.exists() else [])

        findings = phi.scan_published_artifacts(existing)
        assert findings == [], (
            "raw source identifiers found in published artifacts: "
            + "; ".join(f"{f.count}x {f.pattern} in {f.path}" for f in findings)
        )

    def test_the_scan_is_capable_of_failing(self, tmp_path):
        """Guards the test above from passing vacuously."""
        planted = tmp_path / "planted.csv"
        planted.write_text("client_id\nCLI-00234\n")
        assert phi.scan_published_artifacts([planted])

    def test_exported_csvs_carry_pseudonymised_keys(self, published_run):
        path = published_run.export_dir / "dim_client.csv"
        assert path.exists(), "the published run did not export dim_client"
        df = pd.read_csv(path, dtype={"client_id": "string"})
        assert phi.is_pseudonymised(df["client_id"])


class TestDefenceInDepth:
    """The layers have to be independent, or they are one layer with a fallback.

    This test disables the innermost protection -- sample redaction -- runs the
    real pipeline, and asserts that the outermost one still catches the leak.
    If it ever passes without the `verify` task failing, the layers have
    collapsed into each other and the redundancy is imaginary.

    The failure it provokes deletes published files, so it runs against the
    empty data tree ``_clean_workspace`` hands every test rather than against
    ``data/out``. Sharing that directory used to mean this test decided, by
    collection order, whether the tests after it had any artifacts to read.
    """

    def test_the_artifact_scan_catches_what_redaction_would_have(self, monkeypatch):
        from hourglass import pipeline, quality

        monkeypatch.setattr(quality.CheckResult, "__post_init__",
                            lambda self: None, raising=False)

        result = pipeline.run(
            acknowledgements={"uom_resolution_coverage": "test of the egress verifier"},
            prefer_s3=False, quiet=True)

        assert result["succeeded"] is False
        assert result["published"] is False
        failed = result["run"].failed_task
        assert failed is not None and failed.name == "verify"
        assert "PHI EGRESS" in failed.error
