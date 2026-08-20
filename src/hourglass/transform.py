"""Conform raw extracts into dimension and fact frames.

Two things in here carry most of the weight.

**Unit resolution.** A session's duration arrives as a bare number plus a unit
of measure. When the unit of measure is missing the number is not recoverable:
"4" could be four minutes or four 15-minute units, and those differ by a factor
of fifteen. This module refuses to guess. Rows with an unresolvable unit are
flagged, excluded from the measures, and reported. ``resolve_minutes_naive``
exists purely so the pipeline can quantify what the guess would have cost --
see analytics and docs/ANOMALY.md.

**Slowly changing dimensions.** The CRM emits one row per time a client record
changed. A client whose payer changed in April has sessions that belong to the
old payer and sessions that belong to the new one. Overwriting the dimension
would silently re-attribute the earlier sessions, so ``build_dim_client``
builds Type 2 history with validity ranges and the fact loader joins on the
row that was in effect on the service date.
"""

from __future__ import annotations

import pandas as pd

from .config import SERVICE_BY_CODE, SERVICES

FAR_FUTURE = pd.Timestamp("9999-12-31")
UNRESOLVED = -1  # sentinel for "unit of measure unknown"; never treated as a value


# ---------------------------------------------------------------------------
# unit resolution
# ---------------------------------------------------------------------------


def _minutes_per_unit(code: str) -> int | None:
    spec = SERVICE_BY_CODE.get(str(code))
    return spec["minutes_per_unit"] if spec else None


def resolve_minutes(df: pd.DataFrame) -> pd.DataFrame:
    """Add ``minutes_delivered``, ``units_delivered`` and ``uom_resolved``.

    Unresolvable rows keep their raw value, get ``uom_resolved = False`` and
    contribute nothing to the measures. That is the whole point: a measure
    built on a guess is worse than a measure that admits a hole, because the
    hole is visible and the guess is not.
    """
    out = df.copy()
    mpu = out["service_code"].astype(str).map(_minutes_per_unit)
    uom = out["duration_uom"].fillna("").astype(str).str.strip().str.lower()
    value = pd.to_numeric(out["duration_value"], errors="coerce")

    is_minutes = uom.eq("minutes")
    is_units = uom.eq("units")
    resolvable = (is_minutes | is_units) & value.notna() & mpu.notna()

    minutes = pd.Series(pd.NA, index=out.index, dtype="Float64")
    minutes[is_minutes & resolvable] = value[is_minutes & resolvable]
    minutes[is_units & resolvable] = (value[is_units & resolvable]
                                      * mpu[is_units & resolvable])

    units = pd.Series(pd.NA, index=out.index, dtype="Float64")
    units[resolvable] = minutes[resolvable] / mpu[resolvable]

    out["uom_resolved"] = resolvable
    out["minutes_delivered"] = minutes.fillna(0).astype(float).where(resolvable, 0.0)
    out["units_delivered"] = units.fillna(0).astype(float).where(resolvable, 0.0)
    # Every refused row says why, and the order matters: later assignments
    # win, so the reasons run from least to most specific. A row with both an
    # unreadable duration and an unmapped service code is reported as the
    # unmapped code, because that is the one somebody can act on.
    #
    # The catch-all at the end is not defensive padding. A refused row with a
    # null reason is invisible to `analytics.unit_assumption_spread`, which
    # selects on the reason, so it would be counted neither in delivered hours
    # nor in the quantified cost of guessing -- the quietest possible version
    # of the exact failure this module exists to catch. A property test found
    # that gap: `duration_uom = "hours"` is present, is not a value this
    # pipeline understands, and previously fell through every branch.
    out["unresolved_reason"] = pd.Series(pd.NA, index=out.index, dtype="object")
    out.loc[~resolvable, "unresolved_reason"] = "unresolvable"
    out.loc[~resolvable & ~uom.eq("") & ~(is_minutes | is_units),
            "unresolved_reason"] = "unrecognised_uom"
    out.loc[~resolvable & uom.eq(""), "unresolved_reason"] = "missing_uom"
    out.loc[~resolvable & mpu.isna(), "unresolved_reason"] = "unmapped_service_code"
    out.loc[~resolvable & value.isna(), "unresolved_reason"] = "non_numeric_duration"
    return out


def resolve_minutes_naive(df: pd.DataFrame) -> pd.DataFrame:
    """The bug this project was built to catch, preserved deliberately.

    Treats a missing unit of measure as minutes -- the natural assumption,
    because that is what the column meant before the vendor changed it. It
    throws no error and produces a number that looks entirely reasonable.
    """
    out = df.copy()
    mpu = out["service_code"].astype(str).map(_minutes_per_unit)
    uom = out["duration_uom"].fillna("").astype(str).str.strip().str.lower()
    value = pd.to_numeric(out["duration_value"], errors="coerce").fillna(0)

    minutes = value.where(~uom.eq("units"), value * mpu.fillna(15))
    out["minutes_delivered"] = minutes.astype(float)
    out["units_delivered"] = (minutes / mpu.fillna(15)).astype(float)
    return out


# ---------------------------------------------------------------------------
# dimensions
# ---------------------------------------------------------------------------


def build_dim_date(start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    days = pd.date_range(start=start, end=end, freq="D")
    df = pd.DataFrame({"full_date": days})
    df["date_key"] = df["full_date"].dt.strftime("%Y%m%d").astype(int)
    df["year"] = df["full_date"].dt.year
    df["quarter"] = df["full_date"].dt.quarter
    df["month"] = df["full_date"].dt.month
    df["month_name"] = df["full_date"].dt.strftime("%b")
    df["year_month"] = df["full_date"].dt.strftime("%Y-%m")
    df["day_of_week"] = df["full_date"].dt.dayofweek
    df["day_name"] = df["full_date"].dt.strftime("%a")
    df["is_weekend"] = df["day_of_week"] >= 5
    return df[["date_key", "full_date", "year", "quarter", "month", "month_name",
               "year_month", "day_of_week", "day_name", "is_weekend"]]


def _age_band(age: int) -> str:
    if age <= 3:
        return "0-3"
    if age <= 5:
        return "4-5"
    if age <= 8:
        return "6-8"
    if age <= 12:
        return "9-12"
    return "13+"


def build_dim_client(changes: pd.DataFrame) -> pd.DataFrame:
    """Type 2 dimension from the CRM change log.

    Each row gets a validity range. The range closes the day before the next
    change for that client; the newest row runs to 9999-12-31 and is flagged
    current. Surrogate keys are assigned after ordering so they are stable for
    a given input.
    """
    df = changes.copy()
    df["effective_date"] = pd.to_datetime(df["effective_date"])
    df = df.sort_values(["client_id", "effective_date"]).reset_index(drop=True)

    df["valid_from"] = df["effective_date"]
    df["valid_to"] = (
        df.groupby("client_id")["effective_date"].shift(-1) - pd.Timedelta(days=1)
    )
    df["valid_to"] = df["valid_to"].fillna(FAR_FUTURE)
    df["is_current"] = df["valid_to"].eq(FAR_FUTURE)
    df["version"] = df.groupby("client_id").cumcount() + 1
    df["age_band"] = df["age_years"].map(_age_band)
    df.insert(0, "client_key", range(1, len(df) + 1))

    return df[["client_key", "client_id", "version", "age_years", "age_band",
               "home_center_id", "payer_id", "change_reason",
               "valid_from", "valid_to", "is_current"]]


def build_dim_service() -> pd.DataFrame:
    df = pd.DataFrame(SERVICES)
    df.insert(0, "service_key", range(1, len(df) + 1))
    # An explicit unknown member. A session with an unmapped code still has to
    # land somewhere countable; dropping it would hide the problem, and a null
    # foreign key would break the join.
    unknown = pd.DataFrame([{
        "service_key": 0, "service_code": "(unmapped)",
        "service_name": "Unmapped service code", "discipline": "(unknown)",
        "unit_basis": "unknown", "minutes_per_unit": 0,
    }])
    return pd.concat([unknown, df], ignore_index=True)


def build_dim_provider(providers: pd.DataFrame) -> pd.DataFrame:
    df = providers.copy()
    df["hire_date"] = pd.to_datetime(df["hire_date"])
    df["term_date"] = pd.to_datetime(df["term_date"], errors="coerce")
    df["is_active"] = df["term_date"].isna()
    df = df.sort_values("provider_id").reset_index(drop=True)
    df.insert(0, "provider_key", range(1, len(df) + 1))
    return df[["provider_key", "provider_id", "role", "discipline", "center_id",
               "hire_date", "term_date", "is_active"]]


def build_dim_center(centers: pd.DataFrame) -> pd.DataFrame:
    df = centers.copy().sort_values("center_id").reset_index(drop=True)
    df.insert(0, "center_key", range(1, len(df) + 1))
    return df[["center_key", "center_id", "center_name", "state"]]


def build_dim_payer(payers: pd.DataFrame) -> pd.DataFrame:
    df = payers.copy().sort_values("payer_id").reset_index(drop=True)
    df.insert(0, "payer_key", range(1, len(df) + 1))
    return df[["payer_key", "payer_id", "payer_name", "contract_type"]]


# ---------------------------------------------------------------------------
# facts
# ---------------------------------------------------------------------------


def dedupe_sessions(sessions: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Drop exact re-submissions of the same visit.

    Business key is client + provider + service + date + duration + status.
    Two rows matching on all six are treated as one visit entered twice.

    **This rule is a heuristic and it is worth being honest about its limits.**
    A client can legitimately see the same provider twice in one day for the
    same service -- a morning and an afternoon block -- and if both blocks
    happen to run the same length, this collapses them into one and undercounts
    delivered care.

    Two things make that acceptable here rather than merely convenient. The
    source has no session start time, so there is nothing available to
    distinguish the two cases; and `duplicate_session_submissions` reports the
    count as a WARN, so the number is visible rather than silently applied. If
    the EHR exposed a timestamp, that would belong in the key and this
    docstring would be shorter.
    """
    key = ["client_id", "provider_id", "service_code", "service_date",
           "duration_value", "duration_uom", "status"]
    dupe_mask = sessions.duplicated(subset=key, keep="first")
    return sessions.loc[~dupe_mask].copy(), sessions.loc[dupe_mask].copy()


def build_fact_session(
    sessions: pd.DataFrame,
    dim_client: pd.DataFrame,
    dim_provider: pd.DataFrame,
    dim_service: pd.DataFrame,
    dim_center: pd.DataFrame,
) -> pd.DataFrame:
    """Grain: one row per delivered session.

    The client join is an as-of join against the Type 2 dimension: a session is
    attributed to the client record that was in effect on the service date, not
    to whatever the record says today.
    """
    df = sessions.copy()
    df["service_date"] = pd.to_datetime(df["service_date"])
    df["date_key"] = df["service_date"].dt.strftime("%Y%m%d").astype(int)

    cl = dim_client[["client_key", "client_id", "valid_from", "valid_to", "payer_id"]]
    df = df.merge(cl, on="client_id", how="left", suffixes=("", "_dim"))
    in_window = df["service_date"].between(df["valid_from"], df["valid_to"])
    df = df.loc[in_window].copy()

    # validate: a duplicated dimension row in any of these merges would
    # silently multiply fact rows. The dimensions are built unique today;
    # validate makes "today" a loud assertion instead of an assumption.
    df = df.merge(dim_provider[["provider_key", "provider_id"]],
                  on="provider_id", how="left", validate="many_to_one")
    df = df.merge(dim_service[["service_key", "service_code"]].astype({"service_code": str}),
                  left_on=df["service_code"].astype(str), right_on="service_code",
                  how="left", suffixes=("", "_svc"), validate="many_to_one")
    df["service_key"] = df["service_key"].fillna(0).astype(int)
    df = df.merge(dim_center[["center_key", "center_id"]], on="center_id",
                  how="left", validate="many_to_one")

    df["is_completed"] = df["status"].eq("completed")
    df["is_cancelled"] = df["status"].eq("cancelled")
    df["is_no_show"] = df["status"].eq("no_show")

    # Only completed sessions consume authorised units.
    df.loc[~df["is_completed"], ["minutes_delivered", "units_delivered"]] = 0.0

    fact = df[[
        "session_id", "date_key", "client_key", "provider_key", "service_key",
        "center_key", "units_delivered", "minutes_delivered", "uom_resolved",
        "unresolved_reason", "is_completed", "is_cancelled", "is_no_show",
        "source_system",
    ]].copy()
    return fact.sort_values("session_id").reset_index(drop=True)


def build_fact_authorization(
    auths: pd.DataFrame,
    dim_client: pd.DataFrame,
    dim_service: pd.DataFrame,
    dim_payer: pd.DataFrame,
) -> pd.DataFrame:
    """Grain: one row per authorisation line -- client x service x period.

    Deliberately a different grain from fact_session. Joining the two directly
    on client would multiply every authorisation by its session count and
    inflate authorised units; the analytics aggregate sessions to this grain
    first. That is the standard fix for fact tables of different grain and it
    is worth knowing rather than discovering.
    """
    df = auths.copy()
    df["period_start"] = pd.to_datetime(df["period_start"])
    df["period_end"] = pd.to_datetime(df["period_end"])
    df["issued_date"] = pd.to_datetime(df["issued_date"])
    df["period_start_key"] = df["period_start"].dt.strftime("%Y%m%d").astype(int)
    df["period_end_key"] = df["period_end"].dt.strftime("%Y%m%d").astype(int)

    cl = dim_client[["client_key", "client_id", "valid_from", "valid_to"]]
    df = df.merge(cl, on="client_id", how="left")

    # Pick the client version in effect when the period opened. If an
    # authorisation predates the client's first record -- possible if the CRM
    # extract starts later than the payer feed -- fall back to the earliest
    # version rather than dropping the row.
    #
    # The ordering does the work: sort each auth_id's candidate rows so a real
    # match comes first and the earliest version is the tie-breaker, then keep
    # one row per auth_id. An earlier version of this used
    # `~duplicated(keep="first")` as the fallback, which evaluated across the
    # whole merged frame rather than per authorisation and could attach an
    # authorisation to the wrong client version whenever a client had more than
    # one. It was invisible on this dataset and wrong in general.
    df["_in_window"] = df["period_start"].between(df["valid_from"], df["valid_to"])
    df = (df.sort_values(["auth_id", "_in_window", "valid_from"],
                         ascending=[True, False, True])
            .drop_duplicates(subset=["auth_id"], keep="first")
            .drop(columns=["_in_window"])
            .copy())

    df = df.merge(dim_service[["service_key", "service_code"]].astype({"service_code": str}),
                  left_on=df["service_code"].astype(str), right_on="service_code",
                  how="left", suffixes=("", "_svc"), validate="many_to_one")
    df["service_key"] = df["service_key"].fillna(0).astype(int)
    df = df.merge(dim_payer[["payer_key", "payer_id"]], on="payer_id",
                  how="left", validate="many_to_one")

    df["authorized_days"] = (df["period_end"] - df["period_start"]).dt.days + 1

    fact = df[[
        "auth_id", "client_key", "service_key", "payer_key",
        "period_start_key", "period_end_key", "units_authorized", "authorized_days",
    ]].copy()
    return fact.sort_values("auth_id").reset_index(drop=True)
