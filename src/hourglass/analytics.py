"""Authorisation utilisation, computed at the grain the question is asked at.

The one subtlety worth reading before changing anything in here.

Sessions and authorisations both reference the client, but ``client_key`` is a
Type 2 surrogate: a client whose payer changed in April has one key before the
change and a different one after. An authorisation spanning that change would
match only half its own sessions if the join used the surrogate. So the join
uses the natural ``client_id``, and the surrogate is used only to attribute an
individual session to the payer responsible on that date.

This is the kind of defect that produces a plausible number rather than an
error, which is exactly the kind worth writing down.
"""

from __future__ import annotations

import pandas as pd

from .config import (
    AT_RISK_UNUSED_FRACTION,
    EXPIRY_WARNING_DAYS,
)


def _key_to_ts(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series.astype(int).astype(str), format="%Y%m%d")


def _flag(frame: pd.DataFrame, column: str) -> pd.Series:
    """A boolean column, whatever dtype it arrived as.

    SQLite has no boolean type. A frame built by `transform.py` carries real
    ``bool`` columns; the same frame read back out of the warehouse carries
    ``int64`` 0/1 -- and reading the warehouse back is not an edge case, it is
    what a warehouse is for.

    The reason this is a function and not an inline ``.astype(bool)`` is that
    ``df.loc[int_series]`` does not raise. It is interpreted as a positional
    selection and quietly returns **every row**, so a filter written against
    integer flags does not filter. No error, no warning, and utilisation
    computed over cancelled sessions and unresolved units. That defect was
    found by the metric parity check in `metrics.py`, comparing this module
    against the equivalent SQL: the two answers differed by 300,000 units and
    only one of them could be right.
    """
    return frame[column].astype(bool)


def build_utilization(
    fact_session: pd.DataFrame,
    fact_authorization: pd.DataFrame,
    dim_client: pd.DataFrame,
    dim_service: pd.DataFrame,
    dim_payer: pd.DataFrame,
    as_of: pd.Timestamp,
    dim_center: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """One row per authorisation, with delivered units rolled up to that grain."""
    key_to_id = dim_client.set_index("client_key")["client_id"]

    sess = fact_session.loc[
        _flag(fact_session, "is_completed") & _flag(fact_session, "uom_resolved")
    ].copy()
    sess["client_id"] = sess["client_key"].map(key_to_id)
    sess["service_date"] = _key_to_ts(sess["date_key"])

    auth = fact_authorization.copy()
    auth["client_id"] = auth["client_key"].map(key_to_id)
    auth["period_start"] = _key_to_ts(auth["period_start_key"])
    auth["period_end"] = _key_to_ts(auth["period_end_key"])

    # Aggregate sessions to the authorisation grain BEFORE joining. Joining the
    # two fact tables row-to-row would repeat units_authorized once per session.
    pairs = auth[["auth_id", "client_id", "service_key", "period_start", "period_end"]].merge(
        sess[["client_id", "service_key", "service_date", "units_delivered",
              "minutes_delivered", "session_id"]],
        on=["client_id", "service_key"], how="left",
    )
    in_period = pairs["service_date"].between(pairs["period_start"], pairs["period_end"])
    pairs = pairs.loc[in_period]

    delivered = pairs.groupby("auth_id", as_index=False).agg(
        units_delivered=("units_delivered", "sum"),
        minutes_delivered=("minutes_delivered", "sum"),
        session_count=("session_id", "nunique"),
    )

    out = auth.merge(delivered, on="auth_id", how="left")
    out[["units_delivered", "minutes_delivered", "session_count"]] = (
        out[["units_delivered", "minutes_delivered", "session_count"]].fillna(0)
    )

    # The service dimension is joined HERE, before any hours are computed,
    # because a unit is not a fixed quantity of time. 97153 bills in 15-minute
    # units; 92507 is a 45-minute session and 99213 is 30. Multiplying units by
    # a hard-coded 0.25 understates speech and medical authorisations by two to
    # three times -- and because the at-risk list sorts by hours, it buries
    # them. star_schema.sql says the conversion factor lives in the dimension;
    # this is the code honouring that.
    out = out.merge(
        dim_service[["service_key", "service_code", "service_name", "discipline",
                     "minutes_per_unit"]],
        on="service_key", how="left",
    )
    out["minutes_per_unit"] = out["minutes_per_unit"].fillna(0)
    hours_per_unit = out["minutes_per_unit"] / 60.0

    # A ratio with a zero denominator is undefined, and undefined has to survive
    # as null. An authorisation approving no units that nevertheless received a
    # session is infinite over-delivery; published as 0.0 it reads as total
    # non-delivery, which is the worst available direction to be wrong in --
    # `check_utilization_ceiling` tests `utilization > 1` and every at-risk view
    # selects on under-delivery, so a zero moves the row out of all of them.
    # `quality.check_zero_unit_authorizations` names that case instead of
    # letting the arithmetic bury it.
    authorized = out["units_authorized"].where(out["units_authorized"] != 0)
    out["utilization"] = out["units_delivered"] / authorized
    out["units_unused"] = (out["units_authorized"] - out["units_delivered"]).clip(lower=0)
    out["hours_authorized"] = out["units_authorized"] * hours_per_unit
    out["hours_delivered"] = out["units_delivered"] * hours_per_unit
    out["hours_unused"] = out["units_unused"] * hours_per_unit
    out["days_to_expiry"] = (out["period_end"] - as_of).dt.days
    out["is_active"] = (out["period_start"] <= as_of) & (out["period_end"] >= as_of)
    out["is_closed"] = out["period_end"] < as_of

    # ---- pace ------------------------------------------------------------
    # Raw utilisation on an authorisation that is only a third of the way
    # through its period reads as a catastrophe when nothing is wrong. An
    # authorisation is a budget spread over a window, so what an operations
    # team needs mid-period is delivery against the run rate: how much should
    # have been delivered by today, and how much was.
    #
    # Reporting raw utilisation on open authorisations is the single easiest
    # way to make this dashboard wrong, which is why the split is in the model
    # rather than left to whoever writes the query.
    elapsed_days = (
        (out[["period_end"]].assign(cap=as_of).min(axis=1) - out["period_start"]).dt.days + 1
    ).clip(lower=0)
    out["elapsed_days"] = elapsed_days
    out["elapsed_fraction"] = (elapsed_days / out["authorized_days"]).clip(0, 1)
    out["expected_units_to_date"] = out["units_authorized"] * out["elapsed_fraction"]
    # Same rule as utilisation above. Nothing expected yet -- a zero-unit
    # authorisation, or a period that has not started -- makes the delivery rate
    # undefined rather than nil, and a nil pace sorts to the top of every
    # "behind" list it has no business being on.
    expected = out["expected_units_to_date"].where(out["expected_units_to_date"] != 0)
    out["pace"] = out["units_delivered"] / expected

    # The number to judge an authorisation by: pace while it is open, final
    # utilisation once it has closed and no further delivery is possible.
    out["performance"] = out["pace"].where(~out["is_closed"], out["utilization"])

    out = out.merge(
        dim_payer[["payer_key", "payer_name", "contract_type"]],
        on="payer_key", how="left",
    )

    # The centre a child attends is the unit an operations team is organised
    # around: a list of at-risk authorisations nobody owns is a list nobody
    # works. Attached from the client's CURRENT home centre, because the
    # question the digest answers is "who should call them this week", not
    # "where were they in March".
    if dim_center is not None:
        home = (dim_client.loc[_flag(dim_client, "is_current"),
                               ["client_id", "home_center_id"]]
                .drop_duplicates("client_id"))
        out = out.merge(home, on="client_id", how="left")
        out = out.merge(dim_center[["center_id", "center_name"]],
                        left_on="home_center_id", right_on="center_id", how="left")
        out = out.drop(columns=[c for c in ("center_id",) if c in out.columns])
    return out


def unmatched_sessions(
    fact_session: pd.DataFrame,
    fact_authorization: pd.DataFrame,
    dim_client: pd.DataFrame,
) -> pd.DataFrame:
    """Completed sessions that no authorisation covers.

    Computed by matching rather than by subtracting one total from another. A
    subtraction gives a number that is right only when everything else is, and
    quietly returns zero or a negative when it is not -- which is exactly when
    you wanted to know.
    """
    key_to_id = dim_client.set_index("client_key")["client_id"]

    sess = fact_session.loc[
        _flag(fact_session, "is_completed") & _flag(fact_session, "uom_resolved")
    ].copy()
    if sess.empty or fact_authorization.empty:
        return sess

    sess["client_id"] = sess["client_key"].map(key_to_id)
    sess["service_date"] = _key_to_ts(sess["date_key"])

    auth = fact_authorization.copy()
    auth["client_id"] = auth["client_key"].map(key_to_id)
    auth["period_start"] = _key_to_ts(auth["period_start_key"])
    auth["period_end"] = _key_to_ts(auth["period_end_key"])

    joined = sess.merge(
        auth[["auth_id", "client_id", "service_key", "period_start", "period_end"]],
        on=["client_id", "service_key"], how="left",
    )
    covered = joined["service_date"].between(joined["period_start"], joined["period_end"])
    matched_ids = set(joined.loc[covered, "session_id"])
    return sess.loc[~sess["session_id"].isin(matched_ids)]


def at_risk_authorizations(util: pd.DataFrame) -> pd.DataFrame:
    """The output a person can act on this week.

    An authorisation that is still open, expiring soon, and materially unused
    means a child has approved therapy hours that are about to disappear. It is
    a scheduling failure that nobody inside the treatment room can see, because
    it is only visible by comparing two systems.
    """
    mask = (
        util["is_active"]
        & util["days_to_expiry"].between(0, EXPIRY_WARNING_DAYS)
        & ((util["units_unused"] / util["units_authorized"]) >= AT_RISK_UNUSED_FRACTION)
        & (util["units_authorized"] > 0)
    )
    cols = ["auth_id", "client_id", "service_code", "service_name", "discipline",
            "payer_name", "contract_type", "units_authorized", "units_delivered",
            "units_unused", "hours_authorized", "hours_delivered", "hours_unused",
            "utilization", "pace", "days_to_expiry", "period_end"]
    cols = [c for c in [*cols, "center_name"] if c in util.columns]
    return (util.loc[mask, cols]
            .sort_values(["days_to_expiry", "hours_unused"], ascending=[True, False])
            .reset_index(drop=True))


def utilization_by(util: pd.DataFrame, dimension: str) -> pd.DataFrame:
    """Weighted utilisation by a dimension.

    Weighted, not an average of ratios. The mean of per-authorisation
    percentages gives a one-unit authorisation the same weight as a
    two-thousand-unit one, which is how a report ends up disagreeing with the
    number underneath it.
    """
    # dropna=False: an authorisation with an unknown payer or centre must
    # surface as an unknown group, not vanish -- the default silently
    # removes its units from every rollup, with no error and no residual.
    grouped = util.groupby(dimension, as_index=False, dropna=False).agg(
        authorizations=("auth_id", "nunique"),
        units_authorized=("units_authorized", "sum"),
        units_delivered=("units_delivered", "sum"),
        units_unused=("units_unused", "sum"),
        expected_units_to_date=("expected_units_to_date", "sum"),
        # Summed from the per-authorisation figures, each already converted
        # with its own service's minutes_per_unit. Deriving hours from the
        # summed units here would reintroduce the flat-15-minutes error.
        hours_unused=("hours_unused", "sum"),
        hours_authorized=("hours_authorized", "sum"),
    )
    grouped["utilization"] = grouped["units_delivered"] / grouped["units_authorized"]
    # Same rule as the per-row pace in `build_utilization`: nothing expected
    # yet is undefined, not nil -- and not infinite, which is what delivery
    # against a zero denominator produced here, straight past fillna(0).
    expected = grouped["expected_units_to_date"].where(
        grouped["expected_units_to_date"] != 0)
    grouped["pace"] = grouped["units_delivered"] / expected
    return grouped.sort_values("pace").reset_index(drop=True)


def monthly_delivery(fact_session: pd.DataFrame, dim_date: pd.DataFrame) -> pd.DataFrame:
    """Delivered volume by month, with the measure's coverage alongside it.

    ``uom_coverage`` travels with the volume on purpose. A month where coverage
    drops is a month whose delivered hours are understated, and showing the two
    numbers apart invites someone to quote the first without the second.
    """
    completed = (
        fact_session.loc[_flag(fact_session, "is_completed")]
        .merge(dim_date[["date_key", "year_month"]], on="date_key", how="left")
    )
    out = completed.groupby("year_month", as_index=False).agg(
        sessions=("session_id", "nunique"),
        units_delivered=("units_delivered", "sum"),
        minutes_delivered=("minutes_delivered", "sum"),
        resolved_sessions=("uom_resolved", "sum"),
    )
    out["hours_delivered"] = out["minutes_delivered"] / 60.0
    out["uom_coverage"] = out["resolved_sessions"] / out["sessions"]

    # Median is computed over resolved rows only; including zeroed-out
    # unresolved rows would drag it down and disguise the very shift the
    # distribution check is looking for.
    median = (
        completed.loc[_flag(completed, "uom_resolved")]
        .groupby("year_month")["minutes_delivered"].median()
    )
    out["median_minutes"] = out["year_month"].map(median)
    return out.sort_values("year_month").reset_index(drop=True)


def unit_assumption_spread(sessions_resolved: pd.DataFrame) -> dict:
    """What the two plausible guesses would each have produced.

    When a row's unit of measure is missing there are exactly two reasonable
    assumptions, and both are defensible in isolation:

      * *minutes* -- because that is what the column meant before April;
      * *units*   -- because that is what it means now.

    They differ by a factor of fifteen on every affected row. Neither raises an
    error. Neither looks obviously wrong on a dashboard. This function computes
    both alongside the honest answer, which is to exclude the rows and publish
    the coverage, so the cost of guessing is a number rather than an argument.
    """
    df = sessions_resolved.loc[sessions_resolved["status"].eq("completed")].copy()
    unresolved = df.loc[~_flag(df, "uom_resolved")
                        & df["unresolved_reason"].eq("missing_uom")]

    resolved_hours = float(
        df.loc[_flag(df, "uom_resolved"), "minutes_delivered"].sum()) / 60.0
    values = pd.to_numeric(unresolved["duration_value"], errors="coerce").fillna(0)
    mpu = unresolved["service_code"].astype(str).map(_minutes_per_unit_safe).fillna(15)

    as_minutes = float(values.sum()) / 60.0
    as_units = float((values * mpu).sum()) / 60.0

    spread = as_units - as_minutes
    return {
        "resolved_hours": round(resolved_hours, 1),
        "if_assumed_minutes_hours": round(resolved_hours + as_minutes, 1),
        "if_assumed_units_hours": round(resolved_hours + as_units, 1),
        "spread_hours": round(spread, 1),
        "spread_relative": round(spread / resolved_hours, 4) if resolved_hours else 0.0,
        "affected_sessions": len(unresolved),
        "affected_clients": int(unresolved["client_id"].nunique()) if len(unresolved) else 0,
    }


def _minutes_per_unit_safe(code) -> float | None:
    from .config import SERVICE_BY_CODE
    spec = SERVICE_BY_CODE.get(str(code))
    return float(spec["minutes_per_unit"]) if spec else None


def coverage_by_month(fact_session: pd.DataFrame, dim_date: pd.DataFrame) -> pd.Series:
    df = (fact_session.loc[_flag(fact_session, "is_completed")]
          .merge(dim_date[["date_key", "year_month"]], on="date_key", how="left"))
    return df.groupby("year_month")["uom_resolved"].mean().sort_index()
