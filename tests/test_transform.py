"""Unit resolution, deduplication, and the SCD Type 2 build."""

from __future__ import annotations

import pandas as pd
import pytest

from hourglass import transform

# ---------------------------------------------------------------------------
# unit resolution -- the defect this project exists to handle
# ---------------------------------------------------------------------------


class TestResolveMinutes:
    def test_minutes_pass_through(self, sessions_raw):
        out = transform.resolve_minutes(sessions_raw).set_index("session_id")
        assert out.loc["S1", "minutes_delivered"] == 180
        assert out.loc["S1", "units_delivered"] == 12      # 180 / 15
        assert bool(out.loc["S1", "uom_resolved"]) is True

    def test_units_convert_by_service_code(self, sessions_raw):
        out = transform.resolve_minutes(sessions_raw).set_index("session_id")
        # 12 units of a 15-minute code
        assert out.loc["S3", "minutes_delivered"] == 180
        assert out.loc["S3", "units_delivered"] == 12
        # 1 unit of a 45-minute per-session code -- the conversion factor is
        # per service, not global. This is the assertion that fails if someone
        # hard-codes 15 anywhere.
        assert out.loc["S8", "minutes_delivered"] == 45

    def test_missing_uom_is_not_guessed(self, sessions_raw):
        out = transform.resolve_minutes(sessions_raw).set_index("session_id")
        assert bool(out.loc["S5", "uom_resolved"]) is False
        assert out.loc["S5", "minutes_delivered"] == 0
        assert out.loc["S5", "units_delivered"] == 0
        assert out.loc["S5", "unresolved_reason"] == "missing_uom"

    def test_unmapped_service_code_is_unresolvable(self, sessions_raw):
        out = transform.resolve_minutes(sessions_raw).set_index("session_id")
        assert bool(out.loc["S6", "uom_resolved"]) is False
        assert out.loc["S6", "unresolved_reason"] == "unmapped_service_code"

    def test_non_numeric_duration_is_unresolvable(self, sessions_raw):
        out = transform.resolve_minutes(sessions_raw).set_index("session_id")
        assert bool(out.loc["S7", "uom_resolved"]) is False
        assert out.loc["S7", "unresolved_reason"] == "non_numeric_duration"

    def test_unresolved_rows_contribute_nothing(self, sessions_raw):
        out = transform.resolve_minutes(sessions_raw)
        unresolved = out.loc[~out["uom_resolved"]]
        assert len(unresolved) == 3                      # S5, S6, S7
        assert unresolved["minutes_delivered"].sum() == 0
        assert unresolved["units_delivered"].sum() == 0

    def test_naive_path_silently_differs(self, sessions_raw):
        """The bug, demonstrated: no error, different answer."""
        correct = transform.resolve_minutes(sessions_raw)
        naive = transform.resolve_minutes_naive(sessions_raw)
        correct_total = correct.loc[correct["uom_resolved"], "minutes_delivered"].sum()
        naive_total = naive["minutes_delivered"].sum()
        assert naive_total != correct_total
        # and neither raised


# ---------------------------------------------------------------------------
# deduplication
# ---------------------------------------------------------------------------


class TestDedupe:
    def test_exact_resubmission_removed(self, sessions_raw):
        kept, dropped = transform.dedupe_sessions(sessions_raw)
        assert len(dropped) == 1
        assert dropped.iloc[0]["session_id"] == "S9"
        assert len(kept) == len(sessions_raw) - 1

    def test_first_occurrence_survives(self, sessions_raw):
        kept, _ = transform.dedupe_sessions(sessions_raw)
        assert "S1" in set(kept["session_id"])

    def test_distinct_sessions_are_not_merged(self, sessions_raw):
        """Same client and service on different days must both survive."""
        kept, _ = transform.dedupe_sessions(sessions_raw)
        c1_97153 = kept[(kept["client_id"] == "C1") & (kept["service_code"] == "97153")]
        assert len(c1_97153) == 3      # S1, S3, S7 -- different dates


# ---------------------------------------------------------------------------
# SCD Type 2
# ---------------------------------------------------------------------------


class TestDimClientSCD2:
    def test_one_row_per_change(self, client_changes):
        dim = transform.build_dim_client(client_changes)
        assert len(dim) == 6
        assert (dim.groupby("client_id").size() == pd.Series(
            {"C1": 2, "C2": 1, "C3": 3})).all()

    def test_exactly_one_current_row_per_client(self, client_changes):
        dim = transform.build_dim_client(client_changes)
        assert (dim.groupby("client_id")["is_current"].sum() == 1).all()

    def test_ranges_do_not_overlap(self, client_changes):
        dim = transform.build_dim_client(client_changes).sort_values(
            ["client_id", "valid_from"])
        nxt = dim.groupby("client_id")["valid_from"].shift(-1)
        overlap = (dim["valid_to"] >= nxt) & nxt.notna()
        assert not overlap.any()

    def test_ranges_are_contiguous(self, client_changes):
        """No gap between one version closing and the next opening."""
        dim = transform.build_dim_client(client_changes).sort_values(
            ["client_id", "valid_from"])
        nxt = dim.groupby("client_id")["valid_from"].shift(-1)
        closed = dim.loc[nxt.notna()]
        gap = nxt.loc[closed.index] - closed["valid_to"]
        assert (gap == pd.Timedelta(days=1)).all()

    def test_current_row_carries_latest_payer(self, client_changes):
        dim = transform.build_dim_client(client_changes)
        c3 = dim[(dim["client_id"] == "C3") & dim["is_current"]].iloc[0]
        assert c3["payer_id"] == "PAY-005"
        assert c3["version"] == 3

    def test_surrogate_keys_are_unique(self, client_changes):
        dim = transform.build_dim_client(client_changes)
        assert dim["client_key"].is_unique

    def test_age_band_derived(self, client_changes):
        dim = transform.build_dim_client(client_changes)
        assert set(dim.loc[dim["client_id"] == "C3", "age_band"]) == {"0-3"}
        assert set(dim.loc[dim["client_id"] == "C1", "age_band"]) == {"4-5"}


# ---------------------------------------------------------------------------
# fact build
# ---------------------------------------------------------------------------


class TestFactSession:
    @pytest.fixture
    def built(self, sessions_raw, client_changes, providers_raw, centers_raw):
        deduped, _ = transform.dedupe_sessions(sessions_raw)
        resolved = transform.resolve_minutes(deduped)
        dim_client = transform.build_dim_client(client_changes)
        return transform.build_fact_session(
            resolved,
            dim_client,
            transform.build_dim_provider(providers_raw),
            transform.build_dim_service(),
            transform.build_dim_center(centers_raw),
        ), dim_client

    def test_session_attributed_to_version_in_effect(self, built):
        """C1's March session belongs to the pre-change record, May's to the post."""
        fact, dim_client = built
        keys = dim_client.set_index("client_key")["payer_id"]
        march = fact.loc[fact["session_id"] == "S1"].iloc[0]
        may = fact.loc[fact["session_id"] == "S3"].iloc[0]
        assert keys[march["client_key"]] == "PAY-001"
        assert keys[may["client_key"]] == "PAY-002"

    def test_no_row_is_duplicated_by_the_scd_join(self, built):
        """The classic Type 2 bug: joining on the natural key fans rows out."""
        fact, _ = built
        assert fact["session_id"].is_unique

    def test_unmapped_code_lands_on_the_unknown_member(self, built):
        fact, _ = built
        row = fact.loc[fact["session_id"] == "S6"].iloc[0]
        assert row["service_key"] == 0

    def test_every_row_has_a_dimension_key(self, built):
        fact, _ = built
        for col in ("date_key", "client_key", "provider_key", "center_key"):
            assert fact[col].notna().all()
