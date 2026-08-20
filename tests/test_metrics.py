"""The metric registry and the parity harness.

The harness is a check on other code, which makes it the kind of thing that
can rot into a green tick that means nothing. So more than half of what
follows is about the harness's own capacity to fail: if `check_parity` cannot
be made to report a disagreement, its agreement is worthless.
"""

from __future__ import annotations

import sqlite3

import pandas as pd
import pytest

from hourglass import analytics, metrics

# ---------------------------------------------------------------------------
# a miniature warehouse, small enough to verify by hand
# ---------------------------------------------------------------------------

@pytest.fixture
def frames() -> dict[str, pd.DataFrame]:
    """Two children, two services, two authorisations, four sessions.

    Deliberately hand-checkable. 97153 bills in 15-minute units and 92507 in
    45, which is the difference that makes `hours_unused` interesting.
    """
    dim_client = pd.DataFrame([
        {"client_key": 1, "client_id": "CLI-1", "is_current": 1},
        {"client_key": 2, "client_id": "CLI-1", "is_current": 0},   # SCD2 sibling
        {"client_key": 3, "client_id": "CLI-2", "is_current": 1},
    ])
    dim_service = pd.DataFrame([
        {"service_key": 1, "service_code": "97153", "service_name": "ABA",
         "discipline": "ABA", "minutes_per_unit": 15.0},
        {"service_key": 2, "service_code": "92507", "service_name": "Speech",
         "discipline": "Speech", "minutes_per_unit": 45.0},
    ])
    dim_date = pd.DataFrame([
        {"date_key": k, "full_date": d}
        for k, d in [(20260601, "2026-06-01"), (20260615, "2026-06-15"),
                     (20260701, "2026-07-01"), (20260801, "2026-08-01"),
                     (20260901, "2026-09-01")]
    ])
    fact_session = pd.DataFrame([
        # CLI-1, ABA, in period, completed and resolved
        {"session_id": "S1", "date_key": 20260615, "client_key": 1,
         "service_key": 1, "units_delivered": 8.0, "minutes_delivered": 120.0,
         "is_completed": True, "uom_resolved": True},
        # CLI-1, ABA, but recorded against the older SCD2 key -- a join on the
        # surrogate would lose this one
        {"session_id": "S2", "date_key": 20260701, "client_key": 2,
         "service_key": 1, "units_delivered": 4.0, "minutes_delivered": 60.0,
         "is_completed": True, "uom_resolved": True},
        # cancelled: consumes nothing
        {"session_id": "S3", "date_key": 20260701, "client_key": 1,
         "service_key": 1, "units_delivered": 6.0, "minutes_delivered": 90.0,
         "is_completed": False, "uom_resolved": True},
        # CLI-2, speech
        {"session_id": "S4", "date_key": 20260701, "client_key": 3,
         "service_key": 2, "units_delivered": 2.0, "minutes_delivered": 90.0,
         "is_completed": True, "uom_resolved": True},
    ])
    fact_authorization = pd.DataFrame([
        {"auth_id": "A1", "client_key": 1, "service_key": 1, "payer_key": 1,
         "period_start_key": 20260601, "period_end_key": 20260901,
         "units_authorized": 20.0, "authorized_days": 92},
        {"auth_id": "A2", "client_key": 3, "service_key": 2, "payer_key": 1,
         "period_start_key": 20260601, "period_end_key": 20260901,
         "units_authorized": 10.0, "authorized_days": 92},
    ])
    return {
        "dim_client": dim_client, "dim_service": dim_service,
        "dim_date": dim_date, "fact_session": fact_session,
        "fact_authorization": fact_authorization,
        "dim_payer": pd.DataFrame([{"payer_key": 1, "payer_id": "P1",
                                    "payer_name": "Meridian",
                                    "contract_type": "value_based"}]),
    }


@pytest.fixture
def warehouse(tmp_path, frames):
    path = tmp_path / "mini.db"
    conn = sqlite3.connect(path)
    for name, frame in frames.items():
        frame.to_sql(name, conn, index=False)
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def with_utilization(frames):
    out = dict(frames)
    out["utilization"] = analytics.build_utilization(
        frames["fact_session"], frames["fact_authorization"],
        frames["dim_client"], frames["dim_service"], frames["dim_payer"],
        pd.Timestamp("2026-08-01"),
    )
    return out


# ---------------------------------------------------------------------------

class TestRegistry:
    def test_every_metric_has_a_definition_and_a_grain(self):
        """A metric without a stated grain is an argument waiting to happen."""
        for metric in metrics.REGISTRY:
            assert metric.definition.strip()
            assert metric.grain.strip()

    def test_keys_are_unique(self):
        keys = [m.key for m in metrics.REGISTRY]
        assert len(keys) == len(set(keys))

    def test_labels_are_unique(self):
        labels = [m.measure_name for m in metrics.REGISTRY]
        assert len(labels) == len(set(labels))

    def test_every_sql_returns_a_column_named_value(self):
        for metric in metrics.REGISTRY:
            assert "AS value" in metric.sql

    def test_catalogue_renders_every_metric(self):
        out = metrics.to_markdown_catalogue()
        for metric in metrics.REGISTRY:
            assert metric.label in out


class TestParityOnAKnownAnswer:
    """Numbers small enough to check on paper.

    A1: 20 ABA units authorised. S1 delivers 8 and S2 delivers 4 -- S2 counts
    even though it is filed under the client's older surrogate key, because
    the join is on the natural id. S3 is cancelled and delivers nothing. So 12
    delivered, 8 unused, and 8 units at 15 minutes is 2 hours.

    A2: 10 speech units authorised, 2 delivered, 8 unused -- and 8 units at 45
    minutes is 6 hours, three times the ABA figure for the same unit count.
    """

    def test_units_delivered_in_period(self, warehouse, with_utilization):
        result = self._one(warehouse, with_utilization, "auth_units_delivered")
        assert result.sql_value == 14.0
        assert result.agrees

    def test_units_unused(self, warehouse, with_utilization):
        result = self._one(warehouse, with_utilization, "units_unused")
        assert result.sql_value == 16.0
        assert result.agrees

    def test_hours_unused_uses_each_service_own_conversion(
            self, warehouse, with_utilization):
        """8 ABA units is 2 hours; 8 speech units is 6. Not 4 and 4."""
        result = self._one(warehouse, with_utilization, "hours_unused")
        assert result.sql_value == pytest.approx(8.0)
        assert result.agrees

    def test_cancelled_sessions_deliver_nothing(self, warehouse, with_utilization):
        result = self._one(warehouse, with_utilization, "units_delivered_completed")
        assert result.sql_value == 14.0            # S3's 6 units excluded

    def test_children_served_counts_people_not_surrogate_keys(
            self, warehouse, with_utilization):
        """CLI-1 has two SCD2 versions. That is one child."""
        result = self._one(warehouse, with_utilization, "children_served")
        assert result.sql_value == 2.0
        assert result.agrees

    @staticmethod
    def _one(warehouse, frames, key):
        results = metrics.check_parity(warehouse, frames)
        return next(r for r in results if r.key == key)


class TestParityAgreement:
    def test_all_metrics_agree_on_a_consistent_build(self, warehouse,
                                                     with_utilization):
        results = metrics.check_parity(warehouse, with_utilization)
        failures = [(r.label, r.sql_value, r.frame_value, r.error)
                    for r in results if not r.agrees]
        assert failures == []

    def test_the_check_is_capable_of_reporting_a_disagreement(
            self, warehouse, with_utilization):
        """Guards every test above from passing vacuously.

        Corrupt the pandas side only, and the harness must notice. If this
        test ever passes trivially, the green ticks elsewhere mean nothing.
        """
        tampered = dict(with_utilization)
        tampered["utilization"] = with_utilization["utilization"].copy()
        tampered["utilization"]["units_unused"] += 1.0

        results = metrics.check_parity(warehouse, tampered)
        disagreeing = {r.key for r in results if not r.agrees}
        assert "units_unused" in disagreeing
        assert "hours_unused" not in disagreeing      # only what was touched

    def test_a_broken_pandas_definition_is_reported_not_raised(
            self, warehouse, with_utilization):
        """A failure inside one metric must not abort the other ten."""
        broken = dict(with_utilization)
        broken["utilization"] = with_utilization["utilization"].drop(
            columns=["units_unused"])
        results = metrics.check_parity(warehouse, broken)
        assert len(results) == len(metrics.REGISTRY)
        failed = next(r for r in results if r.key == "units_unused")
        assert failed.error is not None
        assert not failed.agrees

    def test_a_broken_sql_definition_is_reported_not_raised(
            self, tmp_path, with_utilization):
        empty = tmp_path / "empty.db"
        sqlite3.connect(empty).close()
        results = metrics.check_parity(empty, with_utilization)
        assert all(r.error is not None for r in results)
        assert all(not r.agrees for r in results)

    def test_tolerance_absorbs_float_summation_order_but_not_a_real_change(
            self, warehouse, with_utilization):
        """52,000 floats summed in two orders differ in the last bits. A
        wrong filter differs in whole units."""
        result = metrics.ParityResult(key="x", label="X", sql_value=1000.0,
                                      frame_value=1000.0 + 1e-9,
                                      tolerance=1e-6)
        assert result.agrees
        result.frame_value = 1001.0
        assert not result.agrees


class TestTheDefectThisFound:
    """Integer flags, `.loc[]`, and a filter that silently does not filter.

    `df.loc[int_series]` is a positional selection, not a boolean mask, and it
    returns every row instead of raising. SQLite has no boolean type, so a
    frame read back out of the warehouse carries 0/1 int64 where the frame
    built by `transform.py` carried real bools -- and every test in this
    project fed the transform side.

    The parity check saw it immediately, because the SQL said 410,405 units
    delivered and the pandas said 106,764.
    """

    def test_integer_flags_filter_the_same_as_boolean_flags(self, frames):
        as_bool = analytics.build_utilization(
            frames["fact_session"], frames["fact_authorization"],
            frames["dim_client"], frames["dim_service"], frames["dim_payer"],
            pd.Timestamp("2026-08-01"))

        int_flavoured = frames["fact_session"].copy()
        for column in ("is_completed", "uom_resolved"):
            int_flavoured[column] = int_flavoured[column].astype(int)
        as_int = analytics.build_utilization(
            int_flavoured, frames["fact_authorization"],
            frames["dim_client"], frames["dim_service"], frames["dim_payer"],
            pd.Timestamp("2026-08-01"))

        assert as_bool["units_delivered"].sum() == as_int["units_delivered"].sum()

    def test_a_cancelled_session_is_still_excluded_with_integer_flags(self, frames):
        int_flavoured = frames["fact_session"].copy()
        for column in ("is_completed", "uom_resolved"):
            int_flavoured[column] = int_flavoured[column].astype(int)
        util = analytics.build_utilization(
            int_flavoured, frames["fact_authorization"], frames["dim_client"],
            frames["dim_service"], frames["dim_payer"], pd.Timestamp("2026-08-01"))
        # S3's six cancelled units must not appear.
        assert util["units_delivered"].sum() == 14.0

    def test_loc_with_an_integer_mask_really_does_not_filter(self, frames):
        """Documents the pandas behaviour the fix defends against.

        If a future pandas makes this raise, this test fails and the comment
        in `analytics._flag` can be revisited.
        """
        session = frames["fact_session"].copy()
        session["is_completed"] = session["is_completed"].astype(int)
        assert session["is_completed"].sum() == 3
        assert len(session.loc[session["is_completed"]]) == len(session)


class TestDaxContract:
    def test_every_shipped_measure_exists(self):
        from hourglass.config import ROOT
        results = metrics.check_dax_contract(ROOT / "bi" / "measures.dax")
        assert [c.measure for c in results if not c.present] == []

    def test_every_measure_references_every_column_its_metric_declares(self):
        """No measure may be correct only because of what another module does.

        This began as a pinned list of four known exceptions. `Hours Delivered`
        referenced neither `is_completed` nor `uom_resolved`, and three others
        omitted `uom_resolved`. All four returned the right totals anyway,
        because `transform.py` zeroes `minutes_delivered` and `units_delivered`
        on exactly the rows those filters would remove — so the measures were
        correct by an invariant in a module they do not import, do not
        reference, and could not detect the loss of.

        That is the same shape as the defect in INC-005: a number that is right
        for a reason other than the one stated. The filters are now written
        into `bi/measures.dax`, both executed engines filter on both flags, and
        the exception list is empty. Keeping it empty is the point of the
        assertion.
        """
        from hourglass.config import ROOT
        results = metrics.check_dax_contract(ROOT / "bi" / "measures.dax")
        assert {c.measure: c.missing_columns
                for c in results if c.missing_columns} == {}

    def test_a_measure_that_names_its_columns_and_ignores_them_passes(self, tmp_path):
        """The limit that makes this a reference check rather than a contract.

        Every declared measure, regenerated as `VAR X = 42 RETURN X + 0 * SUM(
        <column> )`: each names its columns, none computes anything, and all of
        them pass. Asserted rather than described, because a claim about a
        check's weakness drifts as easily as a claim about its strength, and
        the wording in `metrics.py` now rests on exactly this.
        """
        path = tmp_path / "measures.dax"
        path.write_text("\n\n".join(
            f"{m.measure_name} =\nVAR X = 42\nRETURN X"
            + "".join(f" + 0 * SUM ( {c} )" for c in m.dax_columns) + "\n"
            for m in metrics.REGISTRY if m.dax_columns))
        results = metrics.check_dax_contract(path)
        assert results
        assert all(c.holds for c in results)

    def test_every_contracted_metric_is_checked(self):
        from hourglass.config import ROOT
        results = metrics.check_dax_contract(ROOT / "bi" / "measures.dax")
        expected = {m.key for m in metrics.REGISTRY if m.dax_columns}
        assert {c.key for c in results} == expected

    def test_a_missing_measure_is_caught(self, tmp_path):
        path = tmp_path / "measures.dax"
        path.write_text("Session Count =\nDISTINCTCOUNT ( fact_session[session_id] )\n")
        results = metrics.check_dax_contract(path)
        assert any(not c.present for c in results)

    def test_a_measure_summing_the_wrong_column_is_caught(self, tmp_path):
        """The realistic drift: the measure still exists, still returns a
        number, and is now about something else."""
        path = tmp_path / "measures.dax"
        path.write_text(
            "Hours Delivered =\nDIVIDE ( SUM ( fact_session[units_delivered] ), 60 )\n")
        results = metrics.check_dax_contract(path)
        hours = next(c for c in results if c.key == "hours_delivered")
        assert hours.present
        assert "fact_session[minutes_delivered]" in hours.missing_columns

    def test_an_absent_file_fails_rather_than_passing_silently(self, tmp_path):
        results = metrics.check_dax_contract(tmp_path / "nothing.dax")
        assert results
        assert all(not c.present for c in results)

    def test_measures_are_parsed_with_their_bodies(self):
        text = ("// a comment\n\nAlpha =\nSUM ( t[a] )\n\n\nBeta =\nSUM ( t[b] )\n")
        parsed = metrics.parse_measures(text)
        assert set(parsed) == {"Alpha", "Beta"}
        assert "t[a]" in parsed["Alpha"]
        assert "t[b]" not in parsed["Alpha"]

    def test_a_referenced_measure_contributes_its_columns(self):
        """`Units Unused` is defined via `[Units Delivered (Completed)]`. A
        contract that could not follow the reference would report a false
        failure and teach the reader to ignore it."""
        text = ("Units Delivered (Completed) =\nSUM ( fact_session[units_delivered] )\n"
                "\n\nDerived =\nSUMX ( t, [Units Delivered (Completed)] )\n")
        parsed = metrics.parse_measures(text)
        assert "Derived" in parsed
        assert "Units Delivered (Completed)" in parsed["Derived"]


class TestPublishedHeadlines:
    """The half of the harness that starts from the artifact.

    `check_parity` compares two implementations of a metric to each other,
    which says nothing about whether either one is the number on the tile. It
    reported "All 11 metrics agree" for the whole life of a defect that put
    `units_unused * 0.25` on the dashboard's most prominent figure. So the
    tests that matter here are the ones that prove this check can fail: a
    happy path on a harness incapable of failing is a green tick that means
    nothing, and this is the second time that lesson has been paid for.

    Against the miniature warehouse, as of 2026-08-01: both authorisations are
    open, 30 units authorised, 14 delivered, 16 unused -- 8 ABA units at 15
    minutes is 2 hours and 8 speech units at 45 is 6, so 8 hours unused.
    """

    @pytest.fixture
    def payload(self) -> dict:
        """What a faithful `dashboard_data.json` would hold for `frames`.

        Written out by hand rather than produced by `export.py`. A payload
        built by the code under comparison would make the check compare a
        number with itself, which is the failure it exists to prevent.
        """
        return {
            "meta": {"as_of": "2026-08-01"},
            "headline": {
                "hours_unused": 8.0,
                "units_authorized": 30.0,
                "units_delivered": 14.0,
                "active_authorizations": 2,
                "closed_authorizations": 0,
                # By hand, from the dates: both periods run 2026-06-01 to
                # 2026-09-01 inclusive (93 days), 62 days elapsed at the
                # as-of date, so each authorisation expects 62/93 of its
                # units: (20 + 10) x 62/93 = 20 exactly. Pace is 14/20.
                "expected_units_to_date": 20.0,
                "pace": 0.7,
            },
        }

    def test_a_faithful_payload_reproduces_from_the_warehouse(self, warehouse, payload):
        results = metrics.check_published_headlines(warehouse, payload)
        assert len(results) == len(metrics.HEADLINE_SQL)
        assert [r.label for r in results if not r.agrees] == []

    def test_the_flat_quarter_hour_divisor_is_caught(self, warehouse, payload):
        """The guard test, and the defect it is named after.

        16 unused units at a flat quarter hour each is 4.0. Through each
        service's own minutes-per-unit it is 8.0 -- the ABA half is right and
        the speech half is understated threefold. Publish the 4.0 and this
        check must say the tile cannot be reproduced from the data behind it.
        """
        payload["headline"]["hours_unused"] = 16.0 * 0.25

        results = metrics.check_published_headlines(warehouse, payload)
        wrong = [r for r in results if not r.agrees]
        assert [r.key for r in wrong] == ["published.hours_unused"]
        assert wrong[0].sql_value == pytest.approx(8.0)
        assert wrong[0].frame_value == pytest.approx(4.0)

    def test_any_tampered_figure_is_caught(self, warehouse, payload):
        """Not only the one that went wrong once."""
        for key in metrics.HEADLINE_SQL:
            tampered = {**payload, "headline": {**payload["headline"]}}
            tampered["headline"][key] = float(payload["headline"][key]) + 5.0
            wrong = {r.key for r in metrics.check_published_headlines(
                warehouse, tampered) if not r.agrees}
            assert wrong == {f"published.{key}"}, key

    def test_a_figure_missing_from_the_payload_is_reported_not_skipped(
            self, warehouse, payload):
        """A tile that vanished is not a tile that agrees."""
        del payload["headline"]["hours_unused"]
        results = metrics.check_published_headlines(warehouse, payload)
        missing = next(r for r in results if r.key == "published.hours_unused")
        assert missing.error is not None
        assert not missing.agrees

    def test_a_payload_with_nothing_comparable_fails_rather_than_returning_nothing(
            self, warehouse):
        """An empty result is indistinguishable from a passing one.

        This returned `[]` in its first version, which made the whole section
        vanish from the report and left `task_verify` with nothing to raise on
        — a malformed payload sailed through looking exactly like a verified
        one. The rule this project applies everywhere else is that a check
        which cannot run has not passed, and it applies to the checks as much
        as to the data.
        """
        for payload, expected in (
            ({}, "`headline` is empty or absent"),
            ({"meta": {"as_of": "2026-08-01"}}, "`headline` is empty or absent"),
            ({"headline": {"hours_unused": 8.0}}, "`meta.as_of` is missing"),
        ):
            results = metrics.check_published_headlines(warehouse, payload)
            assert results, "a malformed payload must produce a finding"
            assert not any(r.agrees for r in results)
            assert expected in results[0].error

    def test_a_malformed_payload_is_stated_in_the_report(self, warehouse):
        """And it has to be legible, not merely present."""
        out = metrics.render_published(
            metrics.check_published_headlines(warehouse, {}))
        assert "dashboard payload" in out
        assert "cannot be reproduced" in out

    def test_the_tolerance_absorbs_the_published_rounding_and_nothing_more(
            self, warehouse, payload):
        """The payload rounds to one decimal place, so the comparison has to
        allow half a rounding step and no more. An hour is a defect."""
        payload["headline"]["hours_unused"] = 8.0 + metrics.HEADLINE_TOLERANCE / 2
        assert all(r.agrees for r in metrics.check_published_headlines(
            warehouse, payload))

        payload["headline"]["hours_unused"] = 9.0
        assert [r.key for r in metrics.check_published_headlines(warehouse, payload)
                if not r.agrees] == ["published.hours_unused"]

    def test_a_broken_warehouse_is_reported_not_raised(self, tmp_path, payload):
        empty = tmp_path / "empty.db"
        sqlite3.connect(empty).close()
        results = metrics.check_published_headlines(empty, payload)
        assert results
        assert all(r.error is not None and not r.agrees for r in results)


class TestRenderingPublishedHeadlines:
    def test_nothing_checked_renders_nothing(self):
        """An empty section would read as a section that found no problems."""
        assert metrics.render_published([]) == ""

    def test_every_figure_appears_with_both_numbers(self, warehouse):
        payload = {"meta": {"as_of": "2026-08-01"},
                   "headline": dict.fromkeys(metrics.HEADLINE_SQL, 0.0)}
        out = metrics.render_published(
            metrics.check_published_headlines(warehouse, payload))
        for key in metrics.HEADLINE_SQL:
            # Each row is labelled with its scope, because the registry above
            # publishes `Hours Unused` over every authorisation and this
            # section publishes it over the open ones -- 76,362.5 against
            # 57,763.75, two rows in one report, both correct.
            assert f"{key} — {metrics.HEADLINE_SCOPE[key]}" in out

    def test_an_unreproducible_figure_is_stated_above_the_table(self, warehouse):
        payload = {"meta": {"as_of": "2026-08-01"},
                   "headline": dict.fromkeys(metrics.HEADLINE_SQL, 0.0)}
        out = metrics.render_published(
            metrics.check_published_headlines(warehouse, payload))
        assert "cannot be reproduced" in out
        assert out.index("cannot be reproduced") < out.index("| Figure |")

    def test_agreement_does_not_claim_more_than_it_checked(self, warehouse):
        payload = {"meta": {"as_of": "2026-08-01"},
                   "headline": {"hours_unused": 8.0, "units_authorized": 30.0,
                                "units_delivered": 14.0,
                                "active_authorizations": 2,
                                "closed_authorizations": 0,
                                "expected_units_to_date": 20.0,
                                "pace": 0.7}}
        out = metrics.render_published(
            metrics.check_published_headlines(warehouse, payload))
        assert "cannot be reproduced" not in out
        assert "not the same question" in out


class TestRendering:
    def test_agreement_renders_as_agreement(self, warehouse, with_utilization):
        out = metrics.render(
            metrics.check_parity(warehouse, with_utilization), [])
        assert "All 11 metrics agree" in out

    def test_disagreement_is_stated_at_the_top(self, warehouse, with_utilization):
        tampered = dict(with_utilization)
        tampered["utilization"] = with_utilization["utilization"].copy()
        tampered["utilization"]["units_unused"] += 5.0
        out = metrics.render(metrics.check_parity(warehouse, tampered), [])
        assert "disagree between" in out
        assert out.index("disagree") < out.index("| Metric |")

    def test_caveats_are_printed_not_buried(self, warehouse, with_utilization):
        out = metrics.render(
            metrics.check_parity(warehouse, with_utilization), [])
        assert "Stated caveats" in out
        assert "floor" in out

    def test_the_dax_limitation_is_stated_in_the_report(self, warehouse,
                                                       with_utilization):
        """Claiming DAX is 'verified' would be the same species of overclaim
        this project exists to avoid."""
        out = metrics.render(metrics.check_parity(warehouse, with_utilization), [])
        assert "DAX is not executed" in out
