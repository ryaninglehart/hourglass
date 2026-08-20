"""Property-based tests.

The 308 example tests in this suite encode cases somebody sat down and thought
of. That is their strength and it is also the whole of their coverage. The two
most expensive defects in this project were neither of them on that list.

The first was a null comparing unequal to itself: ``diff.py`` compared cells
with ``astype(str)``, ``NaN != NaN``, and the diff announced 49,227 changed
rows between two runs of a deterministic pipeline. Nobody had written a fixture
containing a null in a column that also contained values, because there was no
reason to think that mattered. The second was an integer flag column silently
defeating a ``.loc`` filter — ``df.loc[int_series]`` is a positional selection,
not a mask, so it returns every row without raising, and utilisation was
computed over cancelled sessions. Nobody had written a fixture where the flags
came back from SQLite as 0/1 instead of ``True``/``False``, because in the
frames the tests built they never did.

Both were inputs, not logic. A property test attacks exactly that: state the
invariant once — a suppressed table is never disclosive, a frame diffed against
itself is identical — and let Hypothesis choose the inputs, including the ones
nobody would think to write down, and shrink any failure to its smallest form.

The honest limit, because a property test people over-trust is worse than none.
Hypothesis only explores the input space it is told about, so these properties'
blind spots are the strategies' blind spots. The second defect above is a fair
illustration of both halves of that. No strategy here produces a frame whose
flags come back from SQLite as int64, so nothing here reproduces the *trigger*;
what is caught instead is the *consequence*, by
``test_only_completed_and_resolved_sessions_consume_authorised_units``, which
states filtering as an equivalence — removing the ineligible rows must give the
same answer as filtering them — and fails if ``_flag`` is made to return
integers. A property stated over outcomes survives a change of trigger; one
stated over a dtype would not. And a property holding over a few hundred
generated examples is evidence, not proof. This file does not replace the
example tests. It covers a different failure mode, and the two are worth
keeping side by side.

Two properties here failed the first time they ran. Both were real defects --
a refused row left without a reason, and a null primary key making a frame
differ from itself -- and both were written as the property that ought to hold
rather than weakened into one that already did. The source was then fixed and
they pass; each carries the original failure and its shrunk input in its
docstring, because a regression test that does not say what it is guarding
against decays into a test nobody dares delete and nobody understands.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from hourglass import analytics, diff, disclosure, phi, transform
from hourglass.config import SERVICE_BY_CODE, SERVICES
from hourglass.disclosure import SUPPRESSED, SUPPRESSION_THRESHOLD

SERVICE_CODES = [s["service_code"] for s in SERVICES]


# ---------------------------------------------------------------------------
# strategies
# ---------------------------------------------------------------------------
# Hand-built rather than `hypothesis.extra.pandas`, because the domain objects
# here are a table of counts, a session and an authorisation -- not an
# arbitrary dataframe. A strategy that generates the domain object states what
# the input actually looks like, and a shrunk counterexample reads as a row
# somebody could have received from a source system.


@st.composite
def count_tables(draw, min_rows: int = 1, max_rows: int = 8) -> pd.DataFrame:
    """A published one-dimensional count table: label, count, derived rate.

    Counts are bounded at 60 rather than being unbounded because the
    interesting region is the boundary at the suppression threshold, and
    Hypothesis spends its budget better near it.
    """
    n = draw(st.integers(min_value=min_rows, max_value=max_rows))
    counts = draw(st.lists(st.integers(min_value=0, max_value=60),
                           min_size=n, max_size=n))
    total = sum(counts) or 1
    return pd.DataFrame({
        "service": [f"svc-{i}" for i in range(n)],
        "children": counts,
        "pct": [c / total for c in counts],
    })


@st.composite
def grouped_count_tables(draw) -> pd.DataFrame:
    """Several count tables stacked, each with its own published subtotal."""
    n_groups = draw(st.integers(min_value=1, max_value=3))
    frames = []
    for g in range(n_groups):
        group = draw(count_tables())
        group["centre"] = f"CTR-{g}"
        group["service"] = [f"CTR-{g}-{s}" for s in group["service"]]
        frames.append(group)
    return pd.concat(frames, ignore_index=True)


durations = st.floats(min_value=0.25, max_value=2_000.0,
                      allow_nan=False, allow_infinity=False)

absent_uoms = st.sampled_from([None, "", "   ", "\t", float("nan")])
"""Every way the source can fail to say what the number means."""

unrecognised_uoms = st.sampled_from(["hours", "hrs", "min", "unit",
                                     "sessions", "15min", "each"])
"""Present but not one of the two known values. Distinct from absent."""


@st.composite
def session_rows(draw, uom_strategy, n: int = 4) -> pd.DataFrame:
    """Raw EHR session rows, before unit resolution."""
    rows = []
    for i in range(n):
        rows.append({
            "session_id": f"S{i}",
            "service_code": draw(st.sampled_from(SERVICE_CODES)),
            "duration_value": draw(durations),
            "duration_uom": draw(uom_strategy),
        })
    return pd.DataFrame(rows)


# The client dimension carries two versions of C1, so a session and an
# authorisation for the same child can sit under different surrogate keys.
# That is the case the natural-key join in analytics.py exists for, and a
# scenario that never produces it would test the easy half of the code.
DIM_CLIENT = pd.DataFrame([
    {"client_key": 1, "client_id": "C1", "is_current": False,
     "home_center_id": "CTR-SD"},
    {"client_key": 2, "client_id": "C2", "is_current": True,
     "home_center_id": "CTR-TEM"},
    {"client_key": 3, "client_id": "C1", "is_current": True,
     "home_center_id": "CTR-SD"},
])
DIM_SERVICE = transform.build_dim_service()
DIM_PAYER = pd.DataFrame([
    {"payer_key": 1, "payer_name": "Meridian", "contract_type": "value_based"},
    {"payer_key": 2, "payer_name": "Cascade", "contract_type": "fee_for_service"},
])
AS_OF = pd.Timestamp("2026-04-01")
PERIOD_START_KEY, PERIOD_END_KEY = 20260101, 20260630
AUTHORIZED_DAYS = 181

# Service keys spanning both unit bases: 15-minute codes and per-session codes
# of 45 and 30 minutes. Holding the pool to four keeps the join dense enough
# that authorisations actually match sessions.
SERVICE_KEY_POOL = [1, 2, 5, 7]
MINUTES_PER_UNIT = dict(zip(DIM_SERVICE["service_key"],
                            DIM_SERVICE["minutes_per_unit"]))

# Two dates outside the authorisation period, deliberately, so the period
# filter is exercised rather than assumed.
SESSION_DATE_KEYS = [20251201, 20260115, 20260220, 20260315, 20260610, 20260901]


@st.composite
def utilisation_scenarios(draw) -> tuple[pd.DataFrame, pd.DataFrame]:
    """A fact_session and a fact_authorization that reference each other.

    Delivery is drawn up to 300 units against authorisations of at most 200,
    so over-delivery arises on its own rather than having to be arranged.
    """
    n_auth = draw(st.integers(min_value=1, max_value=4))
    auths = []
    for i in range(n_auth):
        auths.append({
            "auth_id": f"A{i}",
            "client_key": draw(st.sampled_from([1, 2, 3])),
            "service_key": draw(st.sampled_from(SERVICE_KEY_POOL)),
            "payer_key": draw(st.sampled_from([1, 2])),
            "period_start_key": PERIOD_START_KEY,
            "period_end_key": PERIOD_END_KEY,
            "units_authorized": float(draw(st.integers(1, 200))),
            "authorized_days": AUTHORIZED_DAYS,
        })

    n_sess = draw(st.integers(min_value=0, max_value=8))
    sessions = []
    for i in range(n_sess):
        service_key = draw(st.sampled_from(SERVICE_KEY_POOL))
        units = float(draw(st.integers(0, 300)))
        sessions.append({
            "session_id": f"S{i}",
            "date_key": draw(st.sampled_from(SESSION_DATE_KEYS)),
            "client_key": draw(st.sampled_from([1, 2, 3])),
            "provider_key": 1,
            "service_key": service_key,
            "center_key": 1,
            "units_delivered": units,
            "minutes_delivered": units * MINUTES_PER_UNIT[service_key],
            "uom_resolved": draw(st.booleans()),
            "unresolved_reason": None,
            "is_completed": draw(st.booleans()),
            "is_cancelled": False,
            "is_no_show": False,
            "source_system": "ehr",
        })

    session_columns = ["session_id", "date_key", "client_key", "provider_key",
                       "service_key", "center_key", "units_delivered",
                       "minutes_delivered", "uom_resolved", "unresolved_reason",
                       "is_completed", "is_cancelled", "is_no_show",
                       "source_system"]
    fact_session = pd.DataFrame(sessions, columns=session_columns)
    return fact_session, pd.DataFrame(auths)


raw_identifiers = st.one_of(
    st.integers(min_value=1, max_value=999_999).map(lambda n: f"CLI-{n:05d}"),
    st.integers(min_value=1, max_value=99_999).map(lambda n: f"PRV-{n:04d}"),
    st.integers(min_value=1, max_value=9_999_999).map(lambda n: f"SES-{n:07d}"),
    st.integers(min_value=1, max_value=999_999).map(lambda n: f"AUTH-{n:06d}"),
)


@st.composite
def diffable_frames(draw, keys: list[str] | None = None) -> pd.DataFrame:
    """A warehouse-shaped table: a key, a float column, a label, a flag, a date.

    Every non-key column is nullable, because the failure this shape exists to
    reproduce is a null in a column that also holds values.
    """
    if keys is None:
        n = draw(st.integers(min_value=1, max_value=6))
        keys = [f"K{i}" for i in range(n)]
    n = len(keys)

    def column(values):
        return draw(st.lists(values, min_size=n, max_size=n))

    frame = pd.DataFrame({
        "session_id": keys,
        "minutes": column(st.one_of(
            st.none(),
            st.floats(min_value=0, max_value=500, allow_nan=False))),
        "reason": column(st.one_of(
            st.none(),
            st.sampled_from(["missing_uom", "unmapped_service_code", ""]))),
        "is_completed": column(st.booleans()),
        # A genuinely mixed object column: pandas cannot read it as numbers, so
        # the comparison falls to the text path and the null sentinel.
        "note": column(st.one_of(st.none(), st.integers(0, 9),
                                 st.sampled_from(["a", "0", "9"]))),
        "service_date": pd.to_datetime(column(st.one_of(
            st.none(), st.sampled_from(["2026-01-01", "2026-05-05"])))),
    })
    return frame


@st.composite
def change_logs(draw) -> pd.DataFrame:
    """A CRM change log: one row per time a client record changed.

    Effective dates within a client are strictly increasing by construction.
    Two changes on the same day are a separate case and are noted in
    ``TestSlowlyChangingDimension``.
    """
    base = pd.Timestamp("2025-01-01")
    rows = []
    for c in range(draw(st.integers(min_value=1, max_value=3))):
        gaps = draw(st.lists(st.integers(min_value=1, max_value=400),
                             min_size=1, max_size=4))
        day = 0
        for version, gap in enumerate(gaps):
            day += gap
            rows.append({
                "client_id": f"C{c}",
                "effective_date": base + pd.Timedelta(days=day),
                "age_years": draw(st.integers(min_value=0, max_value=17)),
                "home_center_id": "CTR-SD",
                "payer_id": f"PAY-{version:03d}",
                "change_reason": "enrollment" if version == 0 else "payer_change",
            })
    # Shuffled on the way in: the CRM extract arrives in whatever order the
    # source produced, and the dimension build is responsible for ordering it.
    return pd.DataFrame(draw(st.permutations(rows)))


# ---------------------------------------------------------------------------
# disclosure control
# ---------------------------------------------------------------------------


class TestSuppressionIsSafeForEveryTable:
    """The safety invariant, asserted over the input space rather than six cases.

    Suppression is the only privacy control in this pipeline that operates on
    published aggregates, and it is the one that cannot be verified by reading
    the output — a table with one recoverable cell looks exactly like a table
    with none.
    """

    @given(count_tables())
    @settings(max_examples=100, deadline=None)
    def test_a_suppressed_table_is_never_disclosive(self, df):
        """The guarantee the digest is published on.

        If this stops holding, a report goes out with a cell reading "3" for a
        service at a centre, and in a programme where a centre serves twenty
        children that is three identifiable families. There is no recall for a
        published number.
        """
        out, _ = disclosure.suppress_counts(df, "children", "service")
        assert disclosure.is_disclosive(out, "children") == []

    @given(count_tables())
    @settings(max_examples=100, deadline=None)
    def test_every_small_count_is_masked_and_no_other_value_is_invented(self, df):
        """Safety and fidelity at once.

        Verifying only that the output is non-disclosive would be satisfied by
        blanking the whole table, which is safe and useless. Every cell that
        survives must still carry its original value, or the report is safe and
        wrong — and a wrong report is acted on the same as a right one.
        """
        out, _ = disclosure.suppress_counts(df, "children", "service")
        for original, published in zip(df["children"], out["children"]):
            if 0 < original <= SUPPRESSION_THRESHOLD:
                assert published == SUPPRESSED
            elif published != SUPPRESSED:
                assert published == original

    @given(count_tables(min_rows=2))
    @settings(max_examples=100, deadline=None)
    def test_a_lone_suppression_never_survives_a_published_total(self, df):
        """The clause most implementations miss.

        One hidden cell next to a published total is not withheld, it is
        subtraction. Any table of two or more rows must therefore end with
        zero suppressions or at least two.
        """
        out, _ = disclosure.suppress_counts(df, "children", "service")
        assert int((out["children"] == SUPPRESSED).sum()) != 1

    @given(count_tables())
    @settings(max_examples=100, deadline=None)
    def test_zero_is_never_a_primary_suppression(self, df):
        """Zero discloses nothing about anyone, and hiding it costs information.

        "No children received speech therapy at this centre" is an operational
        fact somebody needs; suppressing it as though it were a small count
        would make the report less useful for no privacy gain. Note the
        asymmetry deliberately encoded here: a zero *can* still be taken as the
        complementary victim, because the cheapest cell to sacrifice is the
        smallest one and the sacrifice is about protecting a different cell.
        """
        _, report = disclosure.suppress_counts(df, "children", "service")
        original = dict(zip(df["service"], df["children"]))
        small = {label for label, n in original.items()
                 if 0 < n <= SUPPRESSION_THRESHOLD}
        assert set(report.primary) == small
        for label in report.complementary:
            assert not (0 < original[label] <= SUPPRESSION_THRESHOLD)
        # Withhold the least that closes the hole: one sacrifice, never two.
        assert len(report.complementary) <= 1

    @given(grouped_count_tables())
    @settings(max_examples=80, deadline=None)
    def test_every_group_is_independently_safe(self, df):
        """A per-centre digest section is its own table with its own total.

        Protecting the combined table would be no protection at all: a reader
        looking at the San Diego section subtracts within that section, not
        across the report.
        """
        out, _ = disclosure.suppress_grouped(df, "centre", "children", "service")
        assert disclosure.is_disclosive(out, "children") == []
        for _, group in out.groupby("centre"):
            assert disclosure.is_disclosive(group, "children") == []


# ---------------------------------------------------------------------------
# unit of measure
# ---------------------------------------------------------------------------


class TestUnitResolution:
    """The conversion the whole project exists because of.

    A duration of "4" is four minutes or four 15-minute units depending on a
    column the vendor started leaving null in April. The two readings differ by
    a factor of fifteen and neither raises.
    """

    @given(st.sampled_from(SERVICE_CODES), durations)
    @settings(max_examples=70, deadline=None)
    def test_units_and_minutes_round_trip_for_every_service(self, code, value):
        """The conversion factor belongs to the service, not to the pipeline.

        97153 bills in 15-minute units, 92507 is a 45-minute session and 99213
        is 30. A flat factor understates speech and medical authorisations by
        two to three times, and because the at-risk list sorts by hours unused,
        it buries exactly the children with the most unused care.
        """
        mpu = SERVICE_BY_CODE[code]["minutes_per_unit"]
        df = pd.DataFrame([{"service_code": code, "duration_value": value,
                            "duration_uom": "units"}])
        out = transform.resolve_minutes(df)
        assert bool(out.loc[0, "uom_resolved"]) is True
        assert out.loc[0, "minutes_delivered"] == pytest.approx(value * mpu)
        assert out.loc[0, "units_delivered"] == pytest.approx(value, rel=1e-9)

        df = pd.DataFrame([{"service_code": code, "duration_value": value,
                            "duration_uom": "minutes"}])
        out = transform.resolve_minutes(df)
        assert out.loc[0, "minutes_delivered"] == pytest.approx(value)
        assert out.loc[0, "units_delivered"] * mpu == pytest.approx(value)

    @given(session_rows(absent_uoms))
    @settings(max_examples=100, deadline=None)
    def test_an_absent_unit_of_measure_is_never_guessed(self, df):
        """The central claim of docs/ANOMALY.md, as a property.

        A measure built on a guess is worse than a measure that admits a hole,
        because the hole is visible and the guess is not. Every one of these
        rows must refuse: no number, a flag, and a stated reason.
        """
        out = transform.resolve_minutes(df)
        assert not out["uom_resolved"].any()
        assert (out["minutes_delivered"] == 0.0).all()
        assert (out["units_delivered"] == 0.0).all()
        assert (out["unresolved_reason"] == "missing_uom").all()

    @given(st.sampled_from(SERVICE_CODES), durations,
           st.sampled_from(["minutes", "units"]),
           st.sampled_from(["", " ", "   ", "\t"]),
           st.sampled_from([str.upper, str.lower, str.title]))
    @settings(max_examples=80, deadline=None)
    def test_case_and_padding_do_not_change_the_answer(self, code, value, uom,
                                                       pad, case):
        """Source systems are not consistent about this and never will be.

        "UNITS", " units " and "Units" are the same statement about the same
        number. If casing decided resolvability, a vendor changing an export
        template would silently move rows into the unresolved bucket and the
        coverage metric would fall for a reason nobody could find.
        """
        clean = pd.DataFrame([{"service_code": code, "duration_value": value,
                               "duration_uom": uom}])
        messy = pd.DataFrame([{"service_code": code, "duration_value": value,
                               "duration_uom": pad + case(uom) + pad}])
        assert (transform.resolve_minutes(messy).loc[0, "minutes_delivered"]
                == transform.resolve_minutes(clean).loc[0, "minutes_delivered"])

    @given(session_rows(st.sampled_from(["minutes", "units"])))
    @settings(max_examples=80, deadline=None)
    def test_the_naive_path_agrees_exactly_when_the_unit_is_present(self, df):
        """Isolates the defect to the one input that causes it.

        The naive function is kept in the codebase to quantify what guessing
        would have cost. That number is only meaningful if the two functions
        are otherwise identical — if they disagreed on well-formed rows too,
        the spread in the anomaly report would be measuring the wrong thing.
        """
        correct = transform.resolve_minutes(df)
        naive = transform.resolve_minutes_naive(df)
        assert list(correct["minutes_delivered"]) == list(naive["minutes_delivered"])
        assert list(correct["units_delivered"]) == list(naive["units_delivered"])

    @given(session_rows(absent_uoms))
    @settings(max_examples=80, deadline=None)
    def test_the_naive_path_fabricates_a_number_when_the_unit_is_absent(self, df):
        """Guards the pair of tests above from agreeing about nothing.

        This is the difference the whole anomaly report is built on, and if it
        ever stopped being demonstrable the report would be describing a
        problem the code no longer has.
        """
        correct = transform.resolve_minutes(df)
        naive = transform.resolve_minutes_naive(df)
        assert (correct["minutes_delivered"] == 0.0).all()
        assert (naive["minutes_delivered"] > 0).all()

    @given(session_rows(unrecognised_uoms))
    @settings(max_examples=30, deadline=None)
    def test_every_refused_row_says_why(self, df):
        """Refusing is half the job; the other half is being auditable.

        The module's promise is that unresolvable rows are "flagged, excluded
        from the measures, and reported". A row flagged with a null reason is
        excluded from the measures and absent from the report — the quietest
        possible failure, and the same shape as the defect the module was
        written to catch.

        **This began as a strict xfail.** When it was first written it failed:
        a unit of measure that was present but unrecognised — `hours`, `hrs`,
        `each` — was correctly refused and then left with a null reason,
        because the three assignments in `resolve_minutes` covered an empty
        value, an unmapped service code and a non-numeric duration, and
        nothing covered a word the pipeline simply did not know. Such a row
        fell out of `analytics.unit_assumption_spread`, which selects on
        `unresolved_reason == "missing_uom"`, so it was counted in neither the
        delivered hours nor the quantified cost of guessing. Shrunk case:
        `service_code="97153", duration_value=10, duration_uom="hours"`.
        `resolve_minutes` now assigns `unrecognised_uom`, with a catch-all
        beneath it so the invariant holds for any input rather than for the
        cases enumerated.
        """
        out = transform.resolve_minutes(df)
        refused = out.loc[~out["uom_resolved"]]
        assert refused["unresolved_reason"].notna().all()


# ---------------------------------------------------------------------------
# aggregation
# ---------------------------------------------------------------------------


class TestUtilisationAggregation:
    """Arithmetic that produces a plausible number when it is wrong.

    None of these failures raise. They all publish.
    """

    @given(utilisation_scenarios(), st.randoms(use_true_random=False))
    @settings(max_examples=20, deadline=None)
    def test_row_order_does_not_change_any_total(self, scenario, rng):
        """A warehouse table has no order and neither does an extract.

        Sessions arrive in whatever order the source produced and an
        incremental run appends them in a different one. If any total moved
        with row order, the run-over-run diff would report changes on a
        deterministic pipeline and the report would stop being read — which is
        precisely how the null-comparison defect stayed invisible.
        """
        fact_session, fact_auth = scenario
        measures = ["auth_id", "units_delivered", "units_unused", "hours_unused",
                    "utilization", "pace", "session_count"]

        def utilisation(sessions, auths):
            out = analytics.build_utilization(
                sessions, auths, DIM_CLIENT, DIM_SERVICE, DIM_PAYER, AS_OF)
            return out.sort_values("auth_id").reset_index(drop=True)[measures]

        shuffled_sessions = fact_session.sample(
            frac=1, random_state=rng.randrange(10_000)).reset_index(drop=True)
        shuffled_auths = fact_auth.iloc[::-1].reset_index(drop=True)

        pd.testing.assert_frame_equal(
            utilisation(fact_session, fact_auth),
            utilisation(shuffled_sessions, shuffled_auths),
            check_exact=False, rtol=1e-9)

    @given(utilisation_scenarios())
    @settings(max_examples=20, deadline=None)
    def test_hours_unused_decomposes_across_any_partition(self, scenario):
        """The headline number and the breakdown under it must be the same number.

        A digest states total unused hours and then lists them by discipline
        and by payer. If the parts do not sum to the whole, somebody in an
        operations meeting finds it, and from then on they do not trust either
        figure. Hours are summed from per-authorisation values, each already
        converted with its own service's minutes_per_unit — deriving hours
        from summed units instead would reintroduce the flat-15-minute error.
        """
        fact_session, fact_auth = scenario
        util = analytics.build_utilization(
            fact_session, fact_auth, DIM_CLIENT, DIM_SERVICE, DIM_PAYER, AS_OF)
        total = util["hours_unused"].sum()
        for dimension in ("discipline", "payer_name", "contract_type"):
            parts = analytics.utilization_by(util, dimension)
            assert parts["hours_unused"].sum() == pytest.approx(total)
            assert parts["units_authorized"].sum() == pytest.approx(
                util["units_authorized"].sum())

    @given(utilisation_scenarios())
    @settings(max_examples=30, deadline=None)
    def test_unused_units_are_clamped_per_authorisation(self, scenario):
        """Clamp before summing, not after, or over-delivery pays for shortfalls.

        An over-delivered authorisation is not spare capacity for a different
        child — those hours were delivered to somebody and are a billing
        exposure, not a credit. Clamping after the sum lets one over-delivered
        authorisation cancel another child's unused hours and drop them off the
        at-risk list, which is the one output of this pipeline a person acts
        on. The identity below states the size of the gap exactly: it is the
        over-delivered units, and it is not zero.
        """
        fact_session, fact_auth = scenario
        util = analytics.build_utilization(
            fact_session, fact_auth, DIM_CLIENT, DIM_SERVICE, DIM_PAYER, AS_OF)

        shortfall = (util["units_authorized"] - util["units_delivered"]).clip(lower=0)
        overshoot = (util["units_delivered"] - util["units_authorized"]).clip(lower=0)

        assert (util["units_unused"] >= 0).all()
        assert list(util["units_unused"]) == pytest.approx(list(shortfall))

        naive = max(util["units_authorized"].sum() - util["units_delivered"].sum(), 0)
        assert util["units_unused"].sum() >= naive
        assert (util["units_unused"].sum() - overshoot.sum()
                == pytest.approx(util["units_authorized"].sum()
                                 - util["units_delivered"].sum()))

    @given(utilisation_scenarios())
    @settings(max_examples=30, deadline=None)
    def test_only_completed_and_resolved_sessions_consume_authorised_units(
            self, scenario):
        """The filter that a `.loc` on an integer column silently defeats.

        A cancelled session delivered no care and an unresolved session has no
        trustworthy duration. Counting either inflates utilisation, and an
        authorisation that looks fully used is an authorisation nobody calls
        about. Stated as an equivalence rather than a bound: filtering the
        ineligible rows out must give the same answer as deleting them from the
        input. A `.loc` that silently selected positionally instead of masking
        would break this on the first scenario containing a cancelled session.
        """
        fact_session, fact_auth = scenario
        eligible = fact_session.loc[
            fact_session["is_completed"].astype(bool)
            & fact_session["uom_resolved"].astype(bool)
        ].reset_index(drop=True)

        measures = ["auth_id", "units_delivered", "minutes_delivered",
                    "session_count", "units_unused", "utilization", "pace"]

        def utilisation(sessions):
            out = analytics.build_utilization(
                sessions, fact_auth, DIM_CLIENT, DIM_SERVICE, DIM_PAYER, AS_OF)
            return out.sort_values("auth_id").reset_index(drop=True)[measures]

        pd.testing.assert_frame_equal(
            utilisation(fact_session), utilisation(eligible),
            check_exact=False, rtol=1e-9)


# ---------------------------------------------------------------------------
# pseudonymisation
# ---------------------------------------------------------------------------


class TestPseudonymisation:
    """The surrogate is what makes an export both joinable and useless.

    Joinable, because a stable surrogate lets a BI user follow one child across
    tables and across weeks. Useless to anyone without the salt, because the
    mapping is one-way.
    """

    @given(raw_identifiers)
    @settings(max_examples=100, deadline=None)
    def test_the_same_identifier_always_gives_the_same_surrogate(self, value):
        """Stability is the whole reason to pseudonymise rather than drop.

        If the surrogate moved between runs, week-over-week comparison would
        be impossible and the exports would not join to each other — the
        at-risk list and the session export would describe different people.
        """
        assert phi.pseudonymise(value) == phi.pseudonymise(value)
        assert phi.pseudonymise(value, "CLI") == phi.pseudonymise(value, "CLI")

    @given(st.lists(raw_identifiers, min_size=2, max_size=40, unique=True))
    @settings(max_examples=60, deadline=None)
    def test_distinct_identifiers_give_distinct_surrogates(self, values):
        """A collision silently merges two children into one row.

        Counts drop, utilisation is computed against the wrong authorisations,
        and nothing anywhere raises.

        Honest scope: the surrogate is HMAC-SHA256 truncated to 12 hex
        characters, so collisions are possible in principle -- 48 bits gives an
        even chance of one somewhere around 2 * 10^7 distinct identifiers. This
        asserts injectivity over the identifiers generated here, which is
        evidence at the scale this pipeline runs at. It is not a proof, and the
        truncation is the thing to revisit if this ever ran at that scale.
        """
        surrogates = [phi.pseudonymise(v) for v in values]
        assert len(set(surrogates)) == len(set(values))

    @given(raw_identifiers)
    @settings(max_examples=100, deadline=None)
    def test_the_surrogate_never_contains_the_identifier(self, value):
        """A surrogate that embeds its input has published its input.

        The deliberate near-collision in phi.py -- a raw id is CLI-00234 and a
        surrogate is CLI-6A2F91C4D0E8 -- means a human skimming an export
        cannot tell them apart. Nothing but a machine check will catch a
        surrogate that quietly carried the original along.
        """
        for prefix in ("PSN", "CLI", "PRV", "SES", "ATH"):
            surrogate = phi.pseudonymise(value, prefix)
            assert value not in surrogate
            assert surrogate != value

    @given(st.lists(raw_identifiers, min_size=1, max_size=20, unique=True))
    @settings(max_examples=60, deadline=None)
    def test_the_gate_recognises_surrogates_and_rejects_raw_identifiers(self, values):
        """The egress gate reads values, not declarations.

        Marking a column de-identified asserts that a transformation ran;
        is_pseudonymised proves it. If it accepted raw identifiers, a new
        export path that skipped deidentify_for_export would be waved through
        and the boundary would report itself clean while leaking.
        """
        for prefix in ("PSN", "CLI", "ATH"):
            surrogates = pd.Series([phi.pseudonymise(v, prefix) for v in values])
            assert phi.is_pseudonymised(surrogates) is True
        assert phi.is_pseudonymised(pd.Series(values)) is False


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------


class TestDiffReflexivity:
    """INC-002 as a property rather than as the six fixtures it left behind.

    The diff claimed 49,227 changed rows between two runs of a deterministic
    pipeline. Every one was a null comparing unequal to itself. The example
    tests written afterwards cover the shapes somebody thought of at the time;
    this covers the shape space.
    """

    @given(diffable_frames())
    @settings(max_examples=70, deadline=None)
    def test_a_frame_against_itself_is_always_identical(self, frame):
        """A diff that cries wolf on an unchanged run is a diff nobody reads.

        And a diff nobody reads is why a real change — 40,000 sessions each
        gaining fifteen minutes because a minutes_per_unit was edited — would
        go out unnoticed. The value of the diff is entirely in its silence when
        nothing happened.
        """
        result = diff.diff_frames(frame, frame.copy(), ["session_id"])
        assert result.is_identical
        assert result.changed_cells_by_column == {}

    @given(diffable_frames(), diffable_frames())
    @settings(max_examples=50, deadline=None)
    def test_swapping_the_arguments_swaps_added_and_removed(self, before, after):
        """Added and removed are one fact told from two ends.

        A diff that is not antisymmetric is counting something other than the
        set difference of the keys, and the row counts in the report would not
        reconcile against the table row counts printed beside them.
        """
        forward = diff.diff_frames(before, after, ["session_id"])
        backward = diff.diff_frames(after, before, ["session_id"])
        assert forward.added == backward.removed
        assert forward.removed == backward.added
        assert forward.changed == backward.changed
        assert forward.changed_cells_by_column == backward.changed_cells_by_column

    @given(diffable_frames(), st.data())
    @settings(max_examples=60, deadline=None)
    def test_one_edited_cell_is_one_changed_row_and_one_changed_cell(
            self, before, data):
        """Attribution is the difference between a shrug and a diagnosis.

        "412 rows changed" tells an on-call engineer nothing. "412 rows
        changed, all in minutes_delivered" points at the service dimension. If
        the per-column counts over-reported, every change would look like a
        whole-table rewrite and the attribution would be worthless.
        """
        row = data.draw(st.integers(min_value=0, max_value=len(before) - 1))
        after = before.copy()
        original = after.loc[row, "minutes"]
        after.loc[row, "minutes"] = 1.0 if pd.isna(original) else float(original) + 1.0

        result = diff.diff_frames(before, after, ["session_id"])
        assert result.added == 0
        assert result.removed == 0
        assert result.changed == 1
        assert result.changed_cells_by_column == {"minutes": 1}

    @given(st.lists(st.floats(min_value=0, max_value=100, allow_nan=False),
                    min_size=0, max_size=4, unique=True))
    @settings(max_examples=15, deadline=None)
    def test_a_null_primary_key_does_not_make_a_frame_differ_from_itself(
            self, other_keys):
        """The same defect as INC-002, relocated to the key set.

        Reflexivity is the property the whole diff rests on, and it is asserted
        above over frames whose keys are well formed. This states it over the
        one case that is not.

        **This began as a strict xfail.** `diff.py` mapped nulls to a sentinel
        when comparing cell *values*, but built its key set with
        `set(frame.set_index(keys).index)`, and a null key is a float NaN,
        which is not equal to itself. A row keyed on one was reported as both
        added and removed against an identical frame, on every run — INC-002
        surviving in the one column nobody thought to check. Shrunk case:
        `pd.DataFrame({"session_id": [float("nan")], "minutes": [1.0]})`
        against a copy of itself, giving added=1, removed=1. The sentinel is
        now applied to key columns as well.

        A warehouse primary key should never be null, which is why this had
        not bitten. But SQLite does not enforce that and `diff.py` reads
        whatever `read_sql_query` returns, so the guarantee was living in a
        different module from the code depending on it.
        """
        frame = pd.DataFrame({
            "session_id": [*other_keys, np.nan],
            "minutes": [float(i) for i in range(len(other_keys) + 1)],
        })
        result = diff.diff_frames(frame, frame.copy(), ["session_id"])
        assert result.is_identical


# ---------------------------------------------------------------------------
# slowly changing dimension
# ---------------------------------------------------------------------------


class TestSlowlyChangingDimension:
    """Type 2 history is only history if the validity ranges are a partition.

    Every property here is a precondition of the as-of join in
    build_fact_session. If they hold, each session attributes to exactly one
    client version. If any fails, sessions are duplicated, dropped, or
    attributed to the payer who was not responsible.

    Not covered: two changes for one client on the same effective date. That
    produces a version whose valid_to is the day before its valid_from — an
    empty window that no session can match. The generator strategy uses
    strictly increasing dates, so this file does not exercise it; it is a
    plausible CRM input and it is stated here rather than left implied.
    """

    @given(change_logs())
    @settings(max_examples=80, deadline=None)
    def test_validity_ranges_are_contiguous_and_never_overlap(self, changes):
        """A gap loses sessions; an overlap duplicates them.

        The join in build_fact_session filters on
        service_date.between(valid_from, valid_to). A gap silently drops every
        session in it — delivered care that no longer exists in the warehouse.
        An overlap multiplies sessions by the number of matching versions and
        inflates delivered units, which reads as an authorisation fully used.
        """
        dim = transform.build_dim_client(changes)
        for _, versions in dim.groupby("client_id"):
            versions = versions.sort_values("valid_from")
            assert (versions["valid_to"] >= versions["valid_from"]).all()
            ends = versions["valid_to"].iloc[:-1]
            starts = versions["valid_from"].iloc[1:]
            assert list(ends + pd.Timedelta(days=1)) == list(starts)

    @given(change_logs())
    @settings(max_examples=80, deadline=None)
    def test_exactly_one_version_of_each_client_is_current(self, changes):
        """Anything that asks "who is this child now" reads is_current.

        The at-risk list attaches a home centre that way, and a centre is who
        gets called. Two current versions duplicate a child across two centres'
        worklists; none drops them from every worklist.
        """
        dim = transform.build_dim_client(changes)
        current = dim.groupby("client_id")["is_current"].sum()
        assert (current == 1).all()
        for _, versions in dim.groupby("client_id"):
            latest = versions.sort_values("valid_from").iloc[-1]
            assert bool(latest["is_current"]) is True
            assert latest["valid_to"] == transform.FAR_FUTURE

    @given(change_logs(), st.integers(min_value=0, max_value=2_000))
    @settings(max_examples=80, deadline=None)
    def test_exactly_one_version_is_in_effect_on_any_date(self, changes, offset):
        """The as-of join stated directly, at the grain it is performed at.

        Contiguity implies this, but the join does not read the contiguity —
        it reads `between`, inclusive at both ends. An off-by-one at a
        boundary is exactly the defect that would re-attribute a session to the
        payer who stopped covering the child the day before.
        """
        dim = transform.build_dim_client(changes)
        for _, versions in dim.groupby("client_id"):
            as_of = versions["valid_from"].min() + pd.Timedelta(days=offset)
            in_effect = versions["valid_from"].le(as_of) & versions["valid_to"].ge(as_of)
            assert int(in_effect.sum()) == 1

    @given(change_logs())
    @settings(max_examples=60, deadline=None)
    def test_the_dimension_is_not_sensitive_to_extract_order(self, changes):
        """A CRM extract arrives in whatever order the source produced.

        Ordering is the dimension build's job. If the version numbering or the
        validity ranges depended on the order rows happened to arrive in, two
        runs over the same data would produce different history and every
        run-over-run diff would be noise.
        """
        first = transform.build_dim_client(changes)
        second = transform.build_dim_client(changes.iloc[::-1].reset_index(drop=True))
        pd.testing.assert_frame_equal(first, second)
