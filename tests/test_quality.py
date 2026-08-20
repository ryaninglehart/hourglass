"""The gate: what stops a release, what merely annotates it, and what it takes
to release anyway."""

from __future__ import annotations

import json
import re

import pandas as pd
import pytest

from hourglass import model, phi, pipeline, quality, transform
from hourglass.quality import CheckResult, Severity


def _result(name: str, severity: Severity, passed: bool) -> CheckResult:
    return CheckResult(name=name, severity=severity, passed=passed, message="")


class TestGateDecision:
    def test_all_passing_publishes(self):
        results = [_result("a", Severity.BLOCK, True), _result("b", Severity.WARN, True)]
        assert quality.evaluate_gate(results).published is True

    def test_failed_warn_still_publishes(self):
        """A warning is information, not a veto. If warnings blocked releases
        nobody would raise one."""
        results = [_result("a", Severity.BLOCK, True), _result("b", Severity.WARN, False)]
        decision = quality.evaluate_gate(results)
        assert decision.published is True
        assert decision.blocking_failures == []

    def test_failed_block_halts_publication(self):
        results = [_result("bad", Severity.BLOCK, False)]
        decision = quality.evaluate_gate(results)
        assert decision.published is False
        assert decision.blocking_failures == ["bad"]

    def test_acknowledgement_releases_the_block(self):
        results = [_result("bad", Severity.BLOCK, False)]
        decision = quality.evaluate_gate(results, {"bad": "vendor ticket DE-412 open"})
        assert decision.published is True
        assert decision.acknowledged == {"bad": "vendor ticket DE-412 open"}

    def test_acknowledging_the_wrong_check_does_not_help(self):
        """You have to name the failure you are releasing."""
        results = [_result("bad", Severity.BLOCK, False)]
        decision = quality.evaluate_gate(results, {"something_else": "reason here"})
        assert decision.published is False

    def test_partial_acknowledgement_still_halts(self):
        results = [_result("a", Severity.BLOCK, False), _result("b", Severity.BLOCK, False)]
        decision = quality.evaluate_gate(results, {"a": "handled in DE-1"})
        assert decision.published is False
        assert decision.blocking_failures == ["a", "b"]

    def test_acknowledgement_is_recorded_in_the_verdict(self):
        results = [_result("bad", Severity.BLOCK, False)]
        d = quality.evaluate_gate(results, {"bad": "a written reason"}).to_dict()
        assert d["acknowledged"]["bad"] == "a written reason"
        assert d["ruleset_hash"]
        assert d["evaluated_at_utc"]


class TestRulesetHash:
    def test_hash_is_stable_across_calls(self):
        assert quality.ruleset_hash() == quality.ruleset_hash()

    def test_hash_changes_when_a_threshold_changes(self, monkeypatch):
        before = quality.ruleset_hash()
        monkeypatch.setattr(quality, "UOM_COVERAGE_FLOOR", 0.5)
        assert quality.ruleset_hash() != before


class TestChecksAgainstData:
    @pytest.fixture
    def ctx(self, sessions_raw, client_changes, providers_raw, centers_raw, payers_raw):
        deduped, dupes = transform.dedupe_sessions(sessions_raw)
        resolved = transform.resolve_minutes(deduped)
        dim_client = transform.build_dim_client(client_changes)
        dim_service = transform.build_dim_service()
        fact = transform.build_fact_session(
            resolved, dim_client, transform.build_dim_provider(providers_raw),
            dim_service, transform.build_dim_center(centers_raw))
        dates = pd.to_datetime(sessions_raw["service_date"])
        return {
            "sessions_resolved": resolved,
            "sessions_deduped": deduped,
            "deduped_session_count": len(deduped),
            "duplicate_sessions": dupes,
            "fact_session": fact,
            "fact_authorization": pd.DataFrame(),
            "dim_client": dim_client,
            "dim_date": transform.build_dim_date(dates.min(), dates.max()),
            "utilization": pd.DataFrame({
                "auth_id": [], "units_authorized": [], "units_delivered": [],
                "utilization": []}),
            "unauthorized_session_count": 0,
            "unauthorized_units": 0.0,
        }

    def test_uom_coverage_fails_on_seeded_defect(self, ctx):
        r = quality.check_uom_coverage(ctx)
        assert r.severity is Severity.BLOCK
        assert r.passed is False
        assert r.affected_rows == 3
        assert r.sample                      # the failure names actual rows

    def test_scd_integrity_passes_on_a_correct_dimension(self, ctx):
        assert quality.check_scd_integrity(ctx).passed is True

    def test_scd_integrity_catches_overlapping_ranges(self, ctx):
        broken = ctx["dim_client"].copy()
        broken.loc[broken.index[0], "valid_to"] = pd.Timestamp("2099-01-01")
        r = quality.check_scd_integrity({**ctx, "dim_client": broken})
        assert r.passed is False
        assert r.severity is Severity.BLOCK

    def test_duplicate_check_is_a_warning_not_a_block(self, ctx):
        r = quality.check_duplicate_sessions(ctx)
        assert r.severity is Severity.WARN
        assert r.affected_rows == 1

    def test_orphan_keys_pass_on_clean_data(self, ctx):
        assert quality.check_orphan_keys(ctx).passed is True

    def test_unmapped_codes_warn_but_do_not_block(self, ctx):
        r = quality.check_unmapped_service_codes(ctx)
        assert r.severity is Severity.WARN
        assert r.affected_rows == 1

    def test_every_check_returns_a_result(self, ctx):
        results = quality.run_checks(ctx)
        # By name, not by count. A sabotage run removed twelve checks --
        # all three PHI gates among them -- and the count-only version of
        # this assertion, along with 457 other tests, stayed green: the
        # check functions were tested individually, their registration in
        # the gate was guarded by nothing.
        assert {r.name for r in results} == {
            "uom_resolution_coverage", "session_reconciliation",
            "orphan_foreign_keys", "duration_plausibility",
            "scd_type2_integrity", "duplicate_session_submissions",
            "unmapped_service_codes", "sessions_without_authorization",
            "overlapping_authorization_periods", "utilization_over_ceiling",
            "zero_unit_authorizations", "session_length_distribution_shift",
            "uom_coverage_step_change", "phi_egress", "phi_content_scan",
            "pseudonym_salt_configured", "row_counts",
        }
        assert len(results) == len(quality.CHECKS)
        assert all(isinstance(r, CheckResult) for r in results)
        assert all(r.message for r in results)

    def test_report_renders_without_error(self, ctx):
        decision = quality.evaluate_gate(quality.run_checks(ctx))
        report = quality.render_report(decision)
        assert "# Data quality report" in report
        assert "uom_resolution_coverage" in report


class TestBlockCheckFailurePaths:
    """A gate nobody has watched fail is a gate nobody knows works.

    Each BLOCK check gets a test that breaks the data and asserts the check
    notices. The passing-path tests above are not enough: a check that always
    returns PASS would satisfy every one of them.
    """

    @pytest.fixture
    def ctx(self, sessions_raw, client_changes, providers_raw, centers_raw):
        deduped, dupes = transform.dedupe_sessions(sessions_raw)
        resolved = transform.resolve_minutes(deduped)
        dim_client = transform.build_dim_client(client_changes)
        fact = transform.build_fact_session(
            resolved, dim_client, transform.build_dim_provider(providers_raw),
            transform.build_dim_service(), transform.build_dim_center(centers_raw))
        dates = pd.to_datetime(sessions_raw["service_date"])
        return {
            "sessions_resolved": resolved,
            "sessions_deduped": deduped,
            "deduped_session_count": len(deduped),
            "duplicate_sessions": dupes,
            "fact_session": fact,
            "dim_client": dim_client,
            "dim_date": transform.build_dim_date(dates.min(), dates.max()),
        }

    def test_reconciliation_passes_when_nothing_is_lost(self, ctx):
        assert quality.check_session_reconciliation(ctx).passed is True

    def test_reconciliation_catches_a_silently_dropped_session(self, ctx):
        """The failure `check_orphan_keys` structurally cannot see.

        A session whose client is not in the dimension is not an orphan key --
        it is an absence. The row never reaches the fact table, so a check that
        inspects surviving rows reports success on data it just lost.
        """
        dropped = ctx["fact_session"].iloc[:-1]
        broken = {**ctx, "fact_session": dropped}

        reconciliation = quality.check_session_reconciliation(broken)
        orphans = quality.check_orphan_keys(broken)

        assert reconciliation.passed is False
        assert reconciliation.severity is Severity.BLOCK
        assert reconciliation.affected_rows == 1
        # The point of the new check: the old one is happy.
        assert orphans.passed is True

    def test_duration_plausibility_catches_an_impossible_session(self, ctx):
        broken = ctx["fact_session"].copy()
        broken.loc[broken.index[0], "minutes_delivered"] = 9_000.0
        broken.loc[broken.index[0], "is_completed"] = True
        broken.loc[broken.index[0], "uom_resolved"] = True
        r = quality.check_duration_plausibility({**ctx, "fact_session": broken})
        assert r.passed is False
        assert r.severity is Severity.BLOCK

    def test_orphan_keys_catches_a_dangling_reference(self, ctx):
        broken = ctx["fact_session"].copy()
        broken.loc[broken.index[0], "client_key"] = None
        r = quality.check_orphan_keys({**ctx, "fact_session": broken})
        assert r.passed is False
        assert r.severity is Severity.BLOCK

    def test_scd_integrity_catches_two_current_rows(self, ctx):
        broken = ctx["dim_client"].copy()
        broken.loc[broken.index[0], "is_current"] = True   # C1 now has two
        r = quality.check_scd_integrity({**ctx, "dim_client": broken})
        assert r.passed is False

    def test_coverage_step_change_does_not_claim_persistence_it_did_not_see(self):
        """The message used to hard-code 'then stayed down'. Now it checks."""
        # Four months, ten sessions each. March drops to 50% coverage and
        # April recovers fully -- a dip, not a release.
        rows, dates = [], []
        for ym, key, resolved_count in [
            ("2026-01", 20260115, 10), ("2026-02", 20260215, 10),
            ("2026-03", 20260315, 5),  ("2026-04", 20260415, 10),
        ]:
            for n in range(10):
                rows.append({"date_key": key, "is_completed": True,
                             "uom_resolved": n < resolved_count})
            dates.append({"date_key": key, "year_month": ym})
        fact = pd.DataFrame(rows)
        dim_date = pd.DataFrame(dates)
        r = quality.check_coverage_step_change(
            {"fact_session": fact, "dim_date": dim_date})
        assert r.passed is False
        # Coverage recovers in April, so the message must not claim it persisted.
        assert "did not recover" not in r.message
        assert "recovered later" in r.message


class TestOverlappingAuthorizationPeriods:
    """The defect both parity engines are blind to, because they share it.

    ``analytics.build_utilization`` and ``metrics.AUTH_GRAIN_CTE`` attribute a
    session by ``client + service + date BETWEEN start AND end``. Two
    authorisations that intersect therefore both claim every session in the
    intersection, in pandas and in SQL alike -- so the two agree, and both are
    wrong. Two transcriptions of one sentence cannot check the sentence.
    """

    DIM_CLIENT = pd.DataFrame([
        {"client_key": 1, "client_id": "C1"},
        {"client_key": 2, "client_id": "C1"},      # the same child, SCD2 sibling
        {"client_key": 3, "client_id": "C2"},
    ])

    def _ctx(self, *periods) -> dict:
        """``periods`` are (client_key, service_key, start_key, end_key)."""
        auth = pd.DataFrame(
            [{"auth_id": f"A{i}", "client_key": ck, "service_key": sk,
              "payer_key": 1, "period_start_key": start, "period_end_key": end,
              "units_authorized": 100.0, "authorized_days": 90}
             for i, (ck, sk, start, end) in enumerate(periods)])
        return {"fact_authorization": auth, "dim_client": self.DIM_CLIENT}

    def test_separate_periods_pass(self):
        r = quality.check_overlapping_authorization_periods(
            self._ctx((1, 1, 20260101, 20260331), (1, 1, 20260401, 20260630)))
        assert r.passed is True
        assert r.affected_rows == 0

    def test_an_overlapping_pair_fires(self):
        r = quality.check_overlapping_authorization_periods(
            self._ctx((1, 1, 20260101, 20260430), (1, 1, 20260401, 20260630)))
        assert r.passed is False
        assert r.severity is Severity.BLOCK
        assert r.affected_rows == 1
        assert r.sample

    def test_the_severity_is_blocking_because_the_units_are_unattributable(self):
        """Not a WARN. Under an overlap the delivered units are counted twice,
        so the number is wrong rather than the situation being bad."""
        r = quality.check_overlapping_authorization_periods(
            self._ctx((1, 1, 20260101, 20260430), (1, 1, 20260401, 20260630)))
        assert r.severity is Severity.BLOCK
        assert quality.evaluate_gate([r]).published is False

    def test_a_shared_boundary_day_is_an_overlap(self):
        """One day claimed by two authorisations is one day double-counted."""
        r = quality.check_overlapping_authorization_periods(
            self._ctx((1, 1, 20260101, 20260401), (1, 1, 20260401, 20260630)))
        assert r.passed is False

    def test_consecutive_periods_are_not(self):
        r = quality.check_overlapping_authorization_periods(
            self._ctx((1, 1, 20260101, 20260331), (1, 1, 20260401, 20260630)))
        assert r.passed is True

    def test_a_period_wholly_inside_another_fires(self):
        """The case a naive scan of neighbouring rows misses. Sorted by start,
        the third row does not touch the second and is inside the first."""
        r = quality.check_overlapping_authorization_periods(
            self._ctx((1, 1, 20260101, 20261231),
                      (1, 1, 20260201, 20260228),
                      (1, 1, 20260601, 20260630)))
        assert r.passed is False
        assert r.affected_rows == 2

    def test_different_services_do_not_collide(self):
        """A child can hold ABA and speech authorisations over one window.
        Sessions carry the service, so nothing is ambiguous."""
        r = quality.check_overlapping_authorization_periods(
            self._ctx((1, 1, 20260101, 20260630), (1, 2, 20260101, 20260630)))
        assert r.passed is True

    def test_different_children_do_not_collide(self):
        r = quality.check_overlapping_authorization_periods(
            self._ctx((1, 1, 20260101, 20260630), (3, 1, 20260101, 20260630)))
        assert r.passed is True

    def test_the_same_child_under_two_surrogate_keys_does_collide(self):
        """Type 2 history is not two children. Grouping by `client_key` would
        report clean on exactly the overlap that spans a payer change."""
        r = quality.check_overlapping_authorization_periods(
            self._ctx((1, 1, 20260101, 20260630), (2, 1, 20260301, 20260930)))
        assert r.passed is False

    def test_no_authorisations_is_not_a_failure(self):
        r = quality.check_overlapping_authorization_periods(
            {"fact_authorization": pd.DataFrame(), "dim_client": self.DIM_CLIENT})
        assert r.passed is True
        assert r.message


class TestZeroUnitAuthorizations:
    def _ctx(self, authorized: float, delivered: float) -> dict:
        return {"utilization": pd.DataFrame([{
            "auth_id": "A1", "client_id": "C1",
            "units_authorized": authorized, "units_delivered": delivered,
            "utilization": float("nan") if not authorized else delivered / authorized,
        }])}

    def test_delivery_against_a_zero_unit_authorisation_fires(self):
        r = quality.check_zero_unit_authorizations(self._ctx(0.0, 12.0))
        assert r.passed is False
        assert r.affected_rows == 1
        assert r.sample

    def test_the_ceiling_check_cannot_see_it(self):
        """Why this check has to exist separately.

        Over-delivery is normally caught by `utilization > 1`. Divided by zero
        the ratio is null, and a null is not greater than one, so the row is
        absent from the only check that would otherwise report it.
        """
        ctx = self._ctx(0.0, 12.0)
        assert quality.check_utilization_ceiling(ctx).passed is True
        assert quality.check_zero_unit_authorizations(ctx).passed is False

    def test_a_zero_unit_authorisation_with_no_delivery_is_not_a_defect(self):
        """An approved-nothing line that nobody billed against is a tidy-up,
        not a number anybody has acted on."""
        assert quality.check_zero_unit_authorizations(self._ctx(0.0, 0.0)).passed

    def test_a_normal_authorisation_passes(self):
        assert quality.check_zero_unit_authorizations(self._ctx(100.0, 40.0)).passed

    def test_it_warns_rather_than_blocks(self):
        """Every total stays correct -- the delivered units are real and sum
        correctly. Only the per-authorisation ratio is unavailable."""
        r = quality.check_zero_unit_authorizations(self._ctx(0.0, 12.0))
        assert r.severity is Severity.WARN
        assert quality.evaluate_gate([r]).published is True

    def test_the_message_says_what_to_do_about_it(self):
        r = quality.check_zero_unit_authorizations(self._ctx(0.0, 12.0))
        assert "payer feed" in r.message

    def test_an_empty_utilisation_frame_is_not_a_failure(self):
        r = quality.check_zero_unit_authorizations({"utilization": pd.DataFrame()})
        assert r.passed is True
        assert r.message


class TestPseudonymSalt:
    """The surrogates are only as strong as the salt, so the salt is gated.

    A reviewer rebuilt the mapping for all 240 published clients in about a
    second against a build whose salt was the constant in ``phi.py``. Nothing
    about the surrogate format changed to allow that and nothing about it would
    have revealed it; the failure lived entirely in the key, so the check has
    to look at the key.
    """

    def test_a_configured_salt_passes(self, monkeypatch):
        monkeypatch.setenv(phi.SALT_ENV, "5f3a9c2e1b7d4086af31c05e9d2b7714")
        r = quality.check_pseudonym_salt({})
        assert r.passed is True
        assert r.severity is Severity.BLOCK

    def test_the_published_default_blocks(self, monkeypatch):
        """The guard. Configure the salt everyone already has and the gate must
        stop the release -- a check that cannot fail here protects nothing."""
        monkeypatch.setenv(phi.SALT_ENV, phi.DEVELOPMENT_SALT)
        r = quality.check_pseudonym_salt({})
        assert r.passed is False
        assert r.severity is Severity.BLOCK
        assert quality.evaluate_gate([r]).published is False

    def test_the_published_default_cannot_be_acknowledged(self, monkeypatch):
        """No written reason makes a reversible pseudonym irreversible."""
        monkeypatch.setenv(phi.SALT_ENV, phi.DEVELOPMENT_SALT)
        r = quality.check_pseudonym_salt({})
        assert r.acknowledgeable is False
        decision = quality.evaluate_gate(
            [r], {"pseudonym_salt_configured": "will rotate it next sprint"})
        assert decision.published is False
        assert "pseudonym_salt_configured" in decision.refused_acknowledgements

    def test_an_unset_salt_fails_but_does_not_halt_the_build(self, monkeypatch):
        """Unlinkable beats reversible, and the reader is told either way.

        With no salt configured the module mints a random one per process, so
        nothing published can be precomputed. What is lost is comparability
        between builds, which is a WARN: a fresh clone still runs end to end
        and its report says on its face what it gave up.
        """
        monkeypatch.delenv(phi.SALT_ENV, raising=False)
        r = quality.check_pseudonym_salt({})
        assert r.passed is False
        assert r.severity is Severity.WARN
        assert quality.evaluate_gate([r]).published is True

    def test_the_failure_says_what_to_do(self, monkeypatch):
        """A gate that reports a state without a remedy is a gate people learn
        to skip."""
        for value in (None, phi.DEVELOPMENT_SALT):
            if value is None:
                monkeypatch.delenv(phi.SALT_ENV, raising=False)
            else:
                monkeypatch.setenv(phi.SALT_ENV, value)
            message = quality.check_pseudonym_salt({}).message
            assert f"export {phi.SALT_ENV}=" in message

    def test_an_unset_salt_is_not_the_published_constant(self, monkeypatch):
        """The attack the reviewer ran, re-run as a test.

        Anyone with the repository can compute the surrogate for every client
        identifier under the development salt. None of them may match what an
        unconfigured build actually emits.
        """
        import hashlib
        import hmac

        monkeypatch.delenv(phi.SALT_ENV, raising=False)
        raw = [f"CLI-{n:05d}" for n in range(200)]
        rainbow = {
            value: "CLI-" + hmac.new(
                phi.DEVELOPMENT_SALT.encode(), value.encode(),
                hashlib.sha256).hexdigest()[:12].upper()
            for value in raw
        }
        emitted = {phi.pseudonymise(raw, "CLI") for raw in rainbow}
        assert emitted.isdisjoint(set(rainbow.values()))


class TestContentScanSeverity:
    """The scanner's findings are guesses; the classification's are proof.

    Gating both as unappealable meant an ordinary payer name -- "Member Health
    Network" -- could halt publication permanently with no route back.
    """

    def _finding(self, reason: str):
        from hourglass.phi import EgressFinding

        return EgressFinding("dim_payer", "payer_name", reason, "detail")

    def test_a_content_match_does_not_fail_the_unappealable_check(self):
        ctx = {"egress_findings": [self._finding("content_match:phone_us")]}
        assert quality.check_phi_egress(ctx).passed is True

    def test_a_content_match_still_blocks(self):
        """The guard: acknowledgeable is not the same as ignored."""
        ctx = {"egress_findings": [self._finding("content_match:phone_us")]}
        r = quality.check_phi_content_scan(ctx)
        assert r.passed is False
        assert r.severity is Severity.BLOCK
        assert quality.evaluate_gate([r]).published is False

    def test_a_content_match_can_be_released_with_a_reason(self):
        ctx = {"egress_findings": [self._finding("content_match:member_id_like")]}
        r = quality.check_phi_content_scan(ctx)
        assert r.acknowledgeable is True
        decision = quality.evaluate_gate(
            [r], {"phi_content_scan": "payer is legitimately named Member Health "
                                      "Network; confirmed against dim_payer"})
        assert decision.published is True

    def test_a_raw_identifier_is_not_releasable(self):
        """The half of the old check that had no false-positive mode keeps its
        veto."""
        ctx = {"egress_findings": [self._finding("unpseudonymised_identifier")]}
        r = quality.check_phi_egress(ctx)
        assert r.passed is False
        assert r.acknowledgeable is False
        decision = quality.evaluate_gate([r], {"phi_egress": "shipping it anyway"})
        assert decision.published is False
        assert "phi_egress" in decision.refused_acknowledgements

    def test_an_undeclared_column_is_not_releasable(self):
        ctx = {"egress_findings": [self._finding("undeclared")]}
        assert quality.check_phi_egress(ctx).passed is False

    def test_a_clean_run_passes_both(self):
        assert quality.check_phi_egress({"egress_findings": []}).passed is True
        assert quality.check_phi_content_scan({"egress_findings": []}).passed is True


class TestIdentifierPatternPrecision:
    """The patterns behind the unappealable check, tested in both directions.

    A pattern that never fires is not safe, it is decorative; a pattern that
    fires on prose halts a build nobody can restart. Both directions are
    asserted because passing only one of them is what produced the defect.
    """

    # Values taken from the published dashboard payload, where the old pattern
    # read the decimal point as a phone-number separator and fired six times.
    @pytest.mark.parametrize("value", [
        "106728.6875", "114127.1501", "126018.2145", "527110.6147",
        "125438.0861", "126926.7234",
    ])
    def test_a_rendered_float_is_not_a_phone_number(self, value):
        df = pd.DataFrame({"note": [value]})
        assert phi.scan_frame(df, "t") == []

    @pytest.mark.parametrize("value", [
        "1234567890", "7605550134", "0.1067286875", "20260401", "52000.0",
    ])
    def test_a_bare_digit_run_is_not_a_phone_number(self, value):
        assert phi.IDENTIFIER_PATTERNS["phone_us"].search(value) is None

    @pytest.mark.parametrize("value", [
        "(760) 555-0134", "(760)555-0134", "(760) 555 0134",
        "760-555-0134", "760.555.0134", "760 555 0134",
        "+1 760 555 0134", "+1-760-555-0134", "+17605550134",
        "1-760-555-0134", "call the parent on 760-555-0134 before five",
    ])
    def test_a_formatted_phone_number_is_still_caught(self, value):
        """The guard on the guard. Tightening a pattern until it stops firing
        would pass every test above."""
        df = pd.DataFrame({"note": [value]})
        assert "phone_us" in {h.pattern for h in phi.scan_frame(df, "t")}

    @pytest.mark.parametrize("value", [
        "member rather", "Member Health Network", "Member Health Plan",
        "policy before", "subscriber agreement",
    ])
    def test_a_word_after_member_is_not_a_member_number(self, value):
        assert phi.IDENTIFIER_PATTERNS["member_id_like"].search(value) is None

    @pytest.mark.parametrize("value", [
        "Member ID 99182773", "MEMBER#88213321", "Subscriber No. A1234567",
        "policy number 4471902", "Member ID: XJ4471902",
    ])
    def test_a_member_number_is_still_caught(self, value):
        df = pd.DataFrame({"note": [value]})
        assert "member_id_like" in {h.pattern for h in phi.scan_frame(df, "t")}

    def test_the_ruleset_hash_notices_a_loosened_pattern(self, monkeypatch):
        """Pattern names used to be the whole fingerprint, so rewriting what a
        pattern matched left the hash -- and every verdict it stamps --
        unchanged."""
        import re

        before = quality.ruleset_hash()
        monkeypatch.setitem(phi.IDENTIFIER_PATTERNS, "phone_us", re.compile(r"\d{10}"))
        assert quality.ruleset_hash() != before


class TestRunLogAudit:
    """A log that records only successes is a log of the wrong thing.

    Every blocked run used to write its row to a quarantined database the next
    blocked run overwrote, so the published warehouse held 126 rows and all of
    them said ``published = 1``.
    """

    def _row(self, run_id: str, published: int = 0, refused: str = "") -> dict:
        return {
            "run_id": run_id, "started_at_utc": "2026-08-20T00:00:00+00:00",
            "finished_at_utc": "2026-08-20T00:00:04+00:00",
            "code_version": "0.7.0", "ruleset_version": quality.RULESET_VERSION,
            "ruleset_hash": quality.ruleset_hash(), "published": published,
            "blocking_failures": "uom_resolution_coverage", "acknowledgements": "{}",
            "refused_acknowledgements": refused, "session_rows": 10,
            "auth_rows": 2, "lake_backend": "local",
        }

    def test_the_schema_declares_the_refusal_column(self):
        assert "refused_acknowledgements" in pipeline._run_log_ddl()

    def test_a_refused_acknowledgement_is_kept_in_the_verdict(self):
        results = [CheckResult(name="phi_egress", severity=Severity.BLOCK,
                               passed=False, message="", acknowledgeable=False)]
        decision = quality.evaluate_gate(results, {"phi_egress": "signing this off"})
        assert decision.refused_acknowledgements == ["phi_egress"]
        assert decision.acknowledged == {}

    def test_appending_twice_keeps_the_first_row(self, tmp_path):
        db = tmp_path / "run_audit.db"
        assert pipeline.append_run_log_row(db, self._row("aaa"), create_if_missing=True)
        assert pipeline.append_run_log_row(db, self._row("bbb"), create_if_missing=True)
        conn = model.connect(db)
        try:
            rows = pd.read_sql_query("SELECT * FROM run_log ORDER BY run_id", conn)
        finally:
            conn.close()
        assert list(rows["run_id"]) == ["aaa", "bbb"]
        assert set(rows["published"]) == {0}

    def test_a_refusal_survives_the_round_trip(self, tmp_path):
        db = tmp_path / "run_audit.db"
        pipeline.append_run_log_row(
            db, self._row("ccc", refused="phi_egress"), create_if_missing=True)
        conn = model.connect(db)
        try:
            row = pd.read_sql_query("SELECT * FROM run_log", conn).iloc[0]
        finally:
            conn.close()
        assert row["refused_acknowledgements"] == "phi_egress"

    def test_it_will_not_conjure_a_warehouse_that_does_not_exist(self, tmp_path):
        """The guard on the WAP property.

        A blocked run may append to a published warehouse. It may not create
        one: an empty database where a reader expected the last good build is
        worse than the absence of a file.
        """
        missing = tmp_path / "hourglass.db"
        assert pipeline.append_run_log_row(missing, self._row("ddd")) is False
        assert not missing.exists()

    def test_a_blocked_run_reaches_the_published_warehouse(self, workspace,
                                                           acknowledgement):
        """End to end: publish, then fail, then read the published log.

        Also the WAP assertion. The blocked run must add its audit row and
        change nothing else, so the fact and dimension checksums are compared
        either side of it.
        """
        workspace.reset()
        pipeline.run(acknowledgements=acknowledgement, prefer_s3=False, quiet=True)
        conn = model.connect(workspace.warehouse)
        try:
            before = model.warehouse_checksums(conn)
        finally:
            conn.close()

        blocked = pipeline.run(
            acknowledgements={"phi_egress": "releasing this one on my authority"},
            prefer_s3=False, quiet=True, regenerate=False)
        assert blocked["published"] is False

        conn = model.connect(workspace.warehouse)
        try:
            after = model.warehouse_checksums(conn)
            log = pd.read_sql_query("SELECT * FROM run_log", conn)
        finally:
            conn.close()

        assert after == before, "a blocked run rewrote the published warehouse"
        row = log.loc[log["run_id"] == blocked["run_id"]]
        assert len(row) == 1, "the blocked run left no audit row"
        assert int(row.iloc[0]["published"]) == 0
        assert "phi_egress" in row.iloc[0]["refused_acknowledgements"]

    def test_every_run_reaches_the_sidecar(self, workspace, acknowledgement):
        """The sidecar is the copy that is append-only by construction rather
        than by a carry-forward the warehouse rebuild has to get right."""
        workspace.reset()
        first = pipeline.run(acknowledgements=acknowledgement, prefer_s3=False,
                             quiet=True)
        second = pipeline.run(acknowledgements={}, prefer_s3=False, quiet=True,
                              regenerate=False)
        conn = model.connect(pipeline.RUN_AUDIT_PATH)
        try:
            log = pd.read_sql_query("SELECT * FROM run_log", conn)
        finally:
            conn.close()
        assert set(log["run_id"]) == {first["run_id"], second["run_id"]}
        assert sorted(log["published"]) == [0, 1]


class TestReportIsMachineReadable:
    """The report is read by people, and its code fences are read by parsers."""

    def _samples(self, report: str) -> list[str]:
        return re.findall(r"```json\n(.*?)\n```", report, re.S)

    def test_sample_blocks_parse_as_json(self):
        """A fence labelled ```json has to contain JSON.

        `json.dumps(..., default=str)` covers the types json cannot serialise
        and does nothing about a float NaN, which Python writes as a bare
        `NaN` -- rejected by every strict parser, including the browser's.
        Sample rows come out of pandas frames, where an absent number is
        exactly that, and the seeded unit-of-measure defect puts one in the
        first sample block of every run.
        """
        result = CheckResult(
            name="uom_resolution_coverage", severity=Severity.BLOCK, passed=False,
            message="a missing unit of measure",
            sample=[{"session_id": "S1", "duration_value": 10,
                     "duration_uom": float("nan")}])
        blocks = self._samples(quality.render_report(quality.evaluate_gate([result])))

        assert blocks
        parsed = json.loads(blocks[0])
        assert parsed[0]["duration_uom"] is None

    def test_a_timestamp_in_a_sample_survives_as_a_string(self):
        """The other half of what `default=str` was doing. Dropping it without
        a replacement would raise on the first date in a sample."""
        result = CheckResult(
            name="x", severity=Severity.WARN, passed=False, message="m",
            sample=[{"service_date": pd.Timestamp("2026-05-06")}])
        parsed = json.loads(
            self._samples(quality.render_report(quality.evaluate_gate([result])))[0])
        assert parsed[0]["service_date"] == "2026-05-06"

    def test_the_published_report_parses_end_to_end(self, published_run):
        """Asserted on the artifact rather than on a fixture, because the
        fixture is not what anybody opens."""
        report = (published_run.report_dir / "quality_report.md").read_text()
        blocks = self._samples(report)
        assert blocks
        for block in blocks:
            json.loads(block)
