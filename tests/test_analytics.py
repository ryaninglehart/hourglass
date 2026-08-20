"""Grain correctness, pace arithmetic, and the at-risk selection.

The first test in here is the one that matters most. Two fact tables at
different grains joined carelessly produce a number that is wrong in a way no
type checker and no error message will ever catch.
"""

from __future__ import annotations

import pandas as pd
import pytest

from hourglass import analytics


@pytest.fixture
def dim_client() -> pd.DataFrame:
    return pd.DataFrame([
        {"client_key": 1, "client_id": "C1", "valid_from": pd.Timestamp("2025-01-01"),
         "valid_to": pd.Timestamp("2026-03-31"), "is_current": False},
        {"client_key": 2, "client_id": "C1", "valid_from": pd.Timestamp("2026-04-01"),
         "valid_to": pd.Timestamp("9999-12-31"), "is_current": True},
    ])


@pytest.fixture
def dim_service() -> pd.DataFrame:
    return pd.DataFrame([{"service_key": 2, "service_code": "97153",
                          "service_name": "ABA protocol", "discipline": "ABA",
                          "minutes_per_unit": 15}])


@pytest.fixture
def dim_payer() -> pd.DataFrame:
    return pd.DataFrame([{"payer_key": 1, "payer_name": "Meridian",
                          "contract_type": "value_based"}])


@pytest.fixture
def fact_authorization() -> pd.DataFrame:
    """One authorisation: 100 units over the 100 days from 1 Mar to 8 Jun."""
    return pd.DataFrame([{
        "auth_id": "A1", "client_key": 1, "service_key": 2, "payer_key": 1,
        "period_start_key": 20260301, "period_end_key": 20260608,
        "units_authorized": 100.0, "authorized_days": 100,
    }])


@pytest.fixture
def fact_session() -> pd.DataFrame:
    """Four sessions of 10 units each, spanning the client's payer change.

    Two fall under client_key 1 and two under client_key 2. All four belong to
    the same authorisation.
    """
    rows = []
    for i, (dk, ck) in enumerate([(20260310, 1), (20260320, 1),
                                  (20260410, 2), (20260420, 2)]):
        rows.append({
            "session_id": f"S{i}", "date_key": dk, "client_key": ck,
            "provider_key": 1, "service_key": 2, "center_key": 1,
            "units_delivered": 10.0, "minutes_delivered": 150.0,
            "uom_resolved": True, "unresolved_reason": None,
            "is_completed": True, "is_cancelled": False, "is_no_show": False,
            "source_system": "ehr",
        })
    return pd.DataFrame(rows)


class TestGrain:
    def test_authorisation_is_not_fanned_out_by_its_sessions(
        self, fact_session, fact_authorization, dim_client, dim_service, dim_payer
    ):
        """The whole reason the aggregation happens before the join.

        Four sessions against one authorisation. Joined row-to-row, authorised
        units would read 400 instead of 100 and utilisation would be a quarter
        of the truth. Neither pandas nor SQL would complain.
        """
        util = analytics.build_utilization(
            fact_session, fact_authorization, dim_client, dim_service, dim_payer,
            as_of=pd.Timestamp("2026-06-30"))
        assert len(util) == 1
        assert util.iloc[0]["units_authorized"] == 100.0
        assert util.iloc[0]["session_count"] == 4

    def test_sessions_across_an_scd_change_all_count(
        self, fact_session, fact_authorization, dim_client, dim_service, dim_payer
    ):
        """The join uses the natural key, so a payer change mid-period does not
        cut the authorisation's sessions in half."""
        util = analytics.build_utilization(
            fact_session, fact_authorization, dim_client, dim_service, dim_payer,
            as_of=pd.Timestamp("2026-06-30"))
        assert util.iloc[0]["units_delivered"] == 40.0
        assert util.iloc[0]["utilization"] == pytest.approx(0.40)

    def test_sessions_outside_the_period_are_excluded(
        self, fact_session, fact_authorization, dim_client, dim_service, dim_payer
    ):
        stray = fact_session.copy()
        stray.loc[len(stray)] = {
            **fact_session.iloc[0].to_dict(), "session_id": "OUT",
            "date_key": 20261201,          # long after the period closes
        }
        util = analytics.build_utilization(
            stray, fact_authorization, dim_client, dim_service, dim_payer,
            as_of=pd.Timestamp("2026-12-31"))
        assert util.iloc[0]["units_delivered"] == 40.0


class TestPace:
    def test_pace_accounts_for_elapsed_time(
        self, fact_session, fact_authorization, dim_client, dim_service, dim_payer
    ):
        """Half way through a period, 40 of 100 units is a pace of 0.8, not 0.4.

        Reporting 0.4 here would send someone chasing a problem that does not
        exist yet.
        """
        as_of = pd.Timestamp("2026-04-19")     # day 50 of 100
        util = analytics.build_utilization(
            fact_session, fact_authorization, dim_client, dim_service, dim_payer, as_of)
        row = util.iloc[0]
        assert row["elapsed_days"] == 50
        assert row["elapsed_fraction"] == pytest.approx(0.5)
        assert row["expected_units_to_date"] == pytest.approx(50.0)
        assert row["pace"] == pytest.approx(0.8)
        assert row["utilization"] == pytest.approx(0.4)

    def test_elapsed_fraction_is_capped_at_one(
        self, fact_session, fact_authorization, dim_client, dim_service, dim_payer
    ):
        util = analytics.build_utilization(
            fact_session, fact_authorization, dim_client, dim_service, dim_payer,
            as_of=pd.Timestamp("2027-01-01"))
        assert util.iloc[0]["elapsed_fraction"] == 1.0

    def test_closed_periods_report_utilisation_not_pace(
        self, fact_session, fact_authorization, dim_client, dim_service, dim_payer
    ):
        util = analytics.build_utilization(
            fact_session, fact_authorization, dim_client, dim_service, dim_payer,
            as_of=pd.Timestamp("2026-07-01"))
        row = util.iloc[0]
        assert bool(row["is_closed"]) is True
        assert row["performance"] == pytest.approx(row["utilization"])


class TestUndefinedRatios:
    """Zero authorised units. The ratio has no value, and nor should the cell.

    An authorisation approving nothing that nevertheless received care is
    infinite over-delivery. Reported as 0.0 it reads as total non-delivery,
    which is the one direction that hides it: `check_utilization_ceiling`
    selects on `utilization > 1` and every at-risk view selects on
    under-delivery, so the zero excuses the row from both.
    """

    @pytest.fixture
    def zero_unit_auth(self, fact_authorization) -> pd.DataFrame:
        auth = fact_authorization.copy()
        auth.loc[0, "units_authorized"] = 0.0
        return auth

    def test_utilisation_is_null_not_zero(
        self, fact_session, zero_unit_auth, dim_client, dim_service, dim_payer
    ):
        util = analytics.build_utilization(
            fact_session, zero_unit_auth, dim_client, dim_service, dim_payer,
            as_of=pd.Timestamp("2026-06-30"))
        row = util.iloc[0]
        assert row["units_delivered"] == 40.0
        assert pd.isna(row["utilization"])
        assert pd.isna(row["pace"])
        assert pd.isna(row["performance"])

    def test_a_null_ratio_is_not_selected_as_under_delivery(
        self, fact_session, zero_unit_auth, dim_client, dim_service, dim_payer
    ):
        """The at-risk list is for hours that can still be delivered. There are
        none here: the authorisation approved nothing."""
        util = analytics.build_utilization(
            fact_session, zero_unit_auth, dim_client, dim_service, dim_payer,
            as_of=pd.Timestamp("2026-05-20"))
        assert util.iloc[0]["is_active"]
        assert len(analytics.at_risk_authorizations(util)) == 0

    def test_a_null_ratio_does_not_reach_the_group_totals(
        self, fact_session, zero_unit_auth, dim_client, dim_service, dim_payer
    ):
        """`utilization_by` divides sums, never per-row ratios, so a null cell
        cannot propagate into a published breakdown."""
        util = analytics.build_utilization(
            fact_session, zero_unit_auth, dim_client, dim_service, dim_payer,
            as_of=pd.Timestamp("2026-06-30"))
        grouped = analytics.utilization_by(util, "discipline")
        assert grouped.iloc[0]["units_delivered"] == 40.0
        assert grouped.iloc[0]["units_authorized"] == 0.0

    def test_delivery_before_a_period_opens_has_no_pace(
        self, fact_session, fact_authorization, dim_client, dim_service, dim_payer
    ):
        """Nothing was expected yet, so there is no rate to report -- as
        opposed to a rate of nought, which sorts to the top of every list of
        authorisations that are behind."""
        util = analytics.build_utilization(
            fact_session, fact_authorization, dim_client, dim_service, dim_payer,
            as_of=pd.Timestamp("2026-02-01"))     # a month before it opens
        row = util.iloc[0]
        assert row["elapsed_fraction"] == 0.0
        assert pd.isna(row["pace"])


class TestAggregation:
    def test_group_utilisation_is_weighted_not_averaged(self):
        """A one-unit authorisation must not outvote a thousand-unit one."""
        util = pd.DataFrame([
            {"auth_id": "A", "grp": "g", "units_authorized": 1000.0,
             "units_delivered": 500.0, "units_unused": 500.0,
             "expected_units_to_date": 1000.0, "hours_unused": 125.0,
             "hours_authorized": 250.0},
            {"auth_id": "B", "grp": "g", "units_authorized": 1.0,
             "units_delivered": 1.0, "units_unused": 0.0,
             "expected_units_to_date": 1.0, "hours_unused": 0.0,
             "hours_authorized": 0.75},
        ])
        out = analytics.utilization_by(util, "grp")
        # Weighted: 501 / 1001 = 0.5005. A mean of ratios would give 0.75.
        assert out.iloc[0]["utilization"] == pytest.approx(501 / 1001)


class TestAtRisk:
    def _util(self, **over):
        base = {
            "auth_id": "A1", "client_id": "C1", "service_code": "97153",
            "service_name": "ABA", "discipline": "ABA", "payer_name": "Meridian",
            "contract_type": "value_based", "units_authorized": 100.0,
            "units_delivered": 50.0, "units_unused": 50.0,
            "hours_authorized": 25.0, "hours_delivered": 12.5, "hours_unused": 12.5,
            "utilization": 0.5, "pace": 0.6, "days_to_expiry": 15,
            "is_active": True, "period_end": pd.Timestamp("2026-08-30"),
        }
        base.update(over)
        return pd.DataFrame([base])

    def test_selects_expiring_and_underused(self):
        assert len(analytics.at_risk_authorizations(self._util())) == 1

    def test_ignores_authorisations_expiring_far_out(self):
        assert len(analytics.at_risk_authorizations(self._util(days_to_expiry=200))) == 0

    def test_ignores_already_expired(self):
        assert len(analytics.at_risk_authorizations(self._util(days_to_expiry=-5))) == 0

    def test_ignores_well_used_authorisations(self):
        out = analytics.at_risk_authorizations(
            self._util(units_delivered=95.0, units_unused=5.0, hours_unused=1.25,
                       utilization=0.95))
        assert len(out) == 0

    def test_ignores_inactive(self):
        assert len(analytics.at_risk_authorizations(self._util(is_active=False))) == 0


class TestUnitAssumptionSpread:
    def test_quantifies_both_guesses(self, sessions_raw):
        from hourglass import transform
        resolved = transform.resolve_minutes(sessions_raw)
        out = analytics.unit_assumption_spread(resolved)
        assert out["affected_sessions"] == 1                 # S5 only
        # S5 is 10 of a 15-minute code: 10 minutes vs 150 minutes.
        assert out["if_assumed_units_hours"] > out["if_assumed_minutes_hours"]
        assert out["spread_hours"] == pytest.approx((10 * 15 - 10) / 60, abs=0.05)
