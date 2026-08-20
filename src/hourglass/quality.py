"""Data-quality gates.

Three severities, and only one of them stops the pipeline:

    BLOCK  the number cannot be trusted. Publication halts.
    WARN   the number is usable but something needs a human. Publication proceeds.
    INFO   recorded in the run's quality report. No history is kept.

A blocking failure can be released, but not quietly. The operator has to name
the check and give a written reason, and the run log records the reason, the
rule-set hash, and the code version alongside it. The design goal is that
nobody can ship a bad number by accident, and anybody who ships one on purpose
leaves their name on it.

Each check also carries a **Kahn dimension** -- conformance, completeness or
plausibility -- and a **context** -- verification (against internal
expectations) or validation (against an external benchmark). That taxonomy is
borrowed from the framework OHDSI's Data Quality Dashboard organises its
checks by, rather than invented here, so the categories are ones somebody else
has already argued about. It also makes the gaps legible: a check set with no
`validation` context is only ever comparing the data to itself.

The severity assignments carry an opinion worth stating. A session whose unit
of measure is unknown is a BLOCK, because a utilisation percentage computed
over it is not a slightly-wrong number, it is a meaningless one. A session
delivered without an authorisation on file is only a WARN, because the number
is correct -- it is the business situation that is bad.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum

from .config import (
    AT_RISK_UNUSED_FRACTION,
    DISTRIBUTION_SHIFT_THRESHOLD,
    EXPIRY_WARNING_DAYS,
    MAX_PLAUSIBLE_MINUTES,
    MIN_PLAUSIBLE_MINUTES,
    SERVICES,
    UTILIZATION_CEILING,
    UTILIZATION_FLOOR,
)
from .disclosure import SUPPRESSION_THRESHOLD

RULESET_VERSION = "1.10.0"
UOM_COVERAGE_FLOOR = 0.99
COVERAGE_STEP_THRESHOLD = 0.02


class Severity(str, Enum):
    BLOCK = "BLOCK"
    WARN = "WARN"
    INFO = "INFO"


@dataclass
class CheckResult:
    name: str
    severity: Severity
    passed: bool
    message: str
    observed: float | int | None = None
    threshold: float | int | None = None
    affected_rows: int = 0
    sample: list[dict] = field(default_factory=list)
    acknowledgeable: bool = True
    """Whether a human may release this failure with a written reason.

    True for everything that protects a *number*, and for the one check that
    protects a person by heuristic rather than by proof --
    ``check_phi_content_scan``, whose false positives would otherwise halt a
    release with no way out. False for the checks that demonstrate a failure
    rather than guess at one: ``check_phi_egress`` and
    ``check_pseudonym_salt``. Neither states an opinion a written reason can
    change -- an identifier in a CSV is in the CSV, and a surrogate derived
    under a published salt is reversible however good the reason was."""

    dimension: str = ""
    """Kahn data-quality category -- conformance, completeness or plausibility.

    Borrowed from the framework OHDSI's Data Quality Dashboard uses, so the
    checks sit in a taxonomy somebody else already argued about rather than in
    one invented here. See docs/DEFENSE.md."""

    context: str = "verification"
    """`verification` compares data against internal expectations; `validation`
    compares it against an external benchmark. Also Kahn."""

    def __post_init__(self) -> None:
        # Samples make a failure actionable, and they are also an egress path:
        # they are serialised into the quality report and inlined into the
        # dashboard. Redacting here rather than at each call site means a new
        # check cannot introduce a leak by forgetting.
        if self.sample:
            from .phi import redact_records
            self.sample = redact_records(self.sample)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["severity"] = self.severity.value
        return d


@dataclass
class GateDecision:
    published: bool
    blocking_failures: list[str]
    acknowledged: dict[str, str]
    results: list[CheckResult]
    ruleset_hash: str
    ruleset_version: str
    evaluated_at_utc: str
    refused_acknowledgements: list[str] = field(default_factory=list)
    """Acknowledgements that were offered and rejected, recorded on purpose.

    An attempt to release a PHI failure is more interesting than a successful
    release of anything else, and it should be in the log rather than silently
    dropped."""

    def to_dict(self) -> dict:
        return {
            "published": self.published,
            "ruleset_version": self.ruleset_version,
            "ruleset_hash": self.ruleset_hash,
            "evaluated_at_utc": self.evaluated_at_utc,
            "blocking_failures": self.blocking_failures,
            "acknowledged": self.acknowledged,
            "refused_acknowledgements": self.refused_acknowledgements,
            "summary": {
                "total": len(self.results),
                "passed": sum(1 for r in self.results if r.passed),
                "failed": sum(1 for r in self.results if not r.passed),
                "block": sum(1 for r in self.results
                             if not r.passed and r.severity is Severity.BLOCK),
                "warn": sum(1 for r in self.results
                            if not r.passed and r.severity is Severity.WARN),
            },
            "results": [r.to_dict() for r in self.results],
        }


# ---------------------------------------------------------------------------
# individual checks
# ---------------------------------------------------------------------------


def check_uom_coverage(ctx: dict) -> CheckResult:
    """The seeded defect. See docs/ANOMALY.md."""
    sessions = ctx["sessions_resolved"]
    total = len(sessions)
    resolved = int(sessions["uom_resolved"].sum())
    coverage = resolved / total if total else 1.0
    bad = sessions.loc[~sessions["uom_resolved"]]
    sample = (
        bad.loc[bad["unresolved_reason"].eq("missing_uom"),
                ["session_id", "service_code", "service_date", "duration_value",
                 "duration_uom"]]
        .head(5).to_dict("records")
    )
    return CheckResult(
        name="uom_resolution_coverage",
        severity=Severity.BLOCK,
        dimension="conformance-value",
        context="verification",
        passed=coverage >= UOM_COVERAGE_FLOOR,
        message=(
            f"{total - resolved:,} of {total:,} sessions have a duration whose unit "
            f"of measure cannot be determined. Their duration is not recoverable, "
            f"so they are excluded from all measures. Utilisation computed over the "
            f"remaining {coverage:.1%} is correct but incomplete."
        ),
        observed=round(coverage, 4),
        threshold=UOM_COVERAGE_FLOOR,
        affected_rows=total - resolved,
        sample=sample,
    )


def check_session_reconciliation(ctx: dict) -> CheckResult:
    """Every deduplicated source row must reach the fact table.

    ``build_fact_session`` attaches each session to the client version in
    effect on the service date. A session whose client is missing from the
    dimension, or whose date falls outside every validity range, produces NaT
    bounds, fails the between-test, and disappears -- with no error and no
    orphan key to find, because the row is gone rather than dangling.

    ``check_orphan_keys`` cannot see this: it inspects the rows that survived.
    Counting the input against the output is the only thing that can, and a
    pipeline whose thesis is silent failure has no business shipping without
    it.
    """
    expected = int(ctx["deduped_session_count"])
    actual = len(ctx["fact_session"])
    lost = expected - actual
    return CheckResult(
        name="session_reconciliation",
        severity=Severity.BLOCK,
        dimension="completeness",
        context="verification",
        passed=lost == 0,
        message=(
            f"{lost:,} of {expected:,} deduplicated sessions did not reach the fact "
            f"table. They were dropped by the client as-of join -- either the client "
            f"is absent from the dimension or the service date falls outside every "
            f"validity range."
            if lost else
            f"All {expected:,} deduplicated sessions reached the fact table."
        ),
        observed=actual,
        threshold=expected,
        affected_rows=max(lost, 0),
    )


def check_orphan_keys(ctx: dict) -> CheckResult:
    fact = ctx["fact_session"]
    key_cols = ["date_key", "client_key", "provider_key", "center_key"]
    orphans = fact[key_cols].isna().any(axis=1)
    n = int(orphans.sum())
    return CheckResult(
        name="orphan_foreign_keys",
        severity=Severity.BLOCK,
        dimension="conformance-relational",
        context="verification",
        passed=n == 0,
        message=(f"{n:,} session rows reference a dimension member that does not exist."
                 if n else "Every session row resolves to a dimension member."),
        observed=n,
        threshold=0,
        affected_rows=n,
        sample=fact.loc[orphans, ["session_id", *key_cols]].head(5).to_dict("records"),
    )


def check_duration_plausibility(ctx: dict) -> CheckResult:
    fact = ctx["fact_session"]
    delivered = fact.loc[fact["is_completed"] & fact["uom_resolved"]]
    bad = delivered.loc[
        (delivered["minutes_delivered"] < MIN_PLAUSIBLE_MINUTES)
        | (delivered["minutes_delivered"] > MAX_PLAUSIBLE_MINUTES)
    ]
    n = len(bad)
    return CheckResult(
        name="duration_plausibility",
        severity=Severity.BLOCK,
        dimension="plausibility-atemporal",
        context="validation",
        passed=n == 0,
        message=(f"{n:,} completed sessions have a duration outside "
                 f"{MIN_PLAUSIBLE_MINUTES}-{MAX_PLAUSIBLE_MINUTES} minutes."
                 if n else
                 f"All completed session durations fall within "
                 f"{MIN_PLAUSIBLE_MINUTES}-{MAX_PLAUSIBLE_MINUTES} minutes."),
        observed=n,
        threshold=0,
        affected_rows=n,
        sample=bad[["session_id", "minutes_delivered"]].head(5).to_dict("records"),
    )


def check_scd_integrity(ctx: dict) -> CheckResult:
    """A Type 2 dimension is wrong in two specific ways, so test for both."""
    dim = ctx["dim_client"].sort_values(["client_id", "valid_from"])
    problems: list[str] = []

    current_counts = dim.groupby("client_id")["is_current"].sum()
    not_exactly_one = current_counts[current_counts != 1]
    if len(not_exactly_one):
        problems.append(f"{len(not_exactly_one)} clients without exactly one current row")

    nxt = dim.groupby("client_id")["valid_from"].shift(-1)
    overlap = (dim["valid_to"] >= nxt) & nxt.notna()
    if overlap.any():
        problems.append(f"{int(overlap.sum())} overlapping validity ranges")

    n = len(not_exactly_one) + int(overlap.sum())
    return CheckResult(
        name="scd_type2_integrity",
        severity=Severity.BLOCK,
        dimension="conformance-relational",
        context="verification",
        passed=not problems,
        message=("; ".join(problems) if problems else
                 f"{dim['client_id'].nunique():,} clients across {len(dim):,} versions: "
                 f"no overlapping ranges, exactly one current row each."),
        observed=n,
        threshold=0,
        affected_rows=n,
    )


def check_duplicate_sessions(ctx: dict) -> CheckResult:
    dupes = ctx["duplicate_sessions"]
    n = len(dupes)
    total = n + len(ctx["sessions_deduped"])
    return CheckResult(
        name="duplicate_session_submissions",
        severity=Severity.WARN,
        dimension="plausibility-uniqueness",
        context="verification",
        passed=n == 0,
        message=(f"{n:,} duplicate session submissions removed "
                 f"({n / total:.2%} of raw rows). Same client, provider, service, "
                 f"date and duration. Left in, they inflate delivered units."
                 if n else "No duplicate session submissions found."),
        observed=n,
        threshold=0,
        affected_rows=n,
        sample=dupes[["session_id", "client_id", "service_date"]].head(5).to_dict("records")
        if n else [],
    )


def check_unmapped_service_codes(ctx: dict) -> CheckResult:
    fact = ctx["fact_session"]
    n = int(fact["service_key"].eq(0).sum())
    return CheckResult(
        name="unmapped_service_codes",
        severity=Severity.WARN,
        dimension="conformance-value",
        context="verification",
        passed=n == 0,
        message=(f"{n:,} sessions carry a service code absent from the catalogue. "
                 f"They are assigned to the explicit '(unmapped)' dimension member "
                 f"rather than dropped, so they stay countable."
                 if n else "Every session maps to a known service code."),
        observed=n,
        threshold=0,
        affected_rows=n,
    )


def check_sessions_without_authorization(ctx: dict) -> CheckResult:
    n = ctx.get("unauthorized_session_count", 0)
    units = ctx.get("unauthorized_units", 0.0)
    return CheckResult(
        name="sessions_without_authorization",
        severity=Severity.WARN,
        dimension="conformance-relational",
        context="validation",
        passed=n == 0,
        message=(f"{n:,} completed sessions ({units:,.0f} units) have no matching "
                 f"authorisation for that client, service and date. The measure is "
                 f"correct; the exposure is that this care may be unbillable."
                 if n else "Every completed session falls under an authorisation."),
        observed=n,
        threshold=0,
        affected_rows=n,
    )


def check_overlapping_authorization_periods(ctx: dict) -> CheckResult:
    """Two authorisations for one child and service whose date ranges intersect.

    BLOCK, and the severity is the point. Every other roll-up in this project
    attributes a session to an authorisation by ``client_id + service_key +
    date BETWEEN period_start AND period_end``. Under an overlap that predicate
    matches two authorisations, so each session inside the intersection is
    counted in full against both: delivered units, utilisation and unused units
    are wrong for both rows, and the double count flows into every total built
    on top of them. That is delivered care becoming unattributable, not merely
    suspicious, so it fails the build rather than annotating it.

    This is also the compensating control for the parity harness's one blind
    spot. ``analytics.build_utilization`` and ``metrics.AUTH_GRAIN_CTE`` are
    deliberate transcriptions of each other, which is what makes them able to
    catch a coding error in either -- and exactly what makes them unable to
    catch an error in the specification they share. Both double-count an
    overlap, identically, so parity reports agreement while both numbers are
    wrong. Two implementations of one sentence cannot check the sentence. This
    check does.

    Nothing in the schema prevents an overlap: no SQL constraint expresses
    "these two date ranges must not intersect", and real payers amend and
    reissue authorisations. The synthetic dataset currently contains none,
    which makes this latent rather than live -- and a check that passes today
    is the only thing standing between latent and live.
    """
    auth = ctx["fact_authorization"]
    required = {"auth_id", "client_key", "service_key",
                "period_start_key", "period_end_key"}
    if auth.empty or not required.issubset(auth.columns):
        return CheckResult(
            name="overlapping_authorization_periods",
            severity=Severity.BLOCK,
            dimension="conformance-relational",
            context="verification",
            passed=True,
            message="No authorisations to compare.",
            observed=0,
            threshold=0,
        )

    df = auth[sorted(required)].copy()
    df["client_id"] = df["client_key"].map(
        ctx["dim_client"].set_index("client_key")["client_id"])
    df = df.sort_values(["client_id", "service_key",
                         "period_start_key", "period_end_key"])

    # Sorted by start date within each client-and-service group, a row overlaps
    # something earlier exactly when it starts on or before the furthest end
    # date any earlier row reached. The running maximum is what makes that a
    # single comparison per row instead of a pairwise scan -- an earlier long
    # authorisation can be overlapped by a later short one that starts before
    # it ends but after the row immediately above it ends.
    ends = df.groupby(["client_id", "service_key"], sort=False)["period_end_key"]
    prior_end = ends.cummax().groupby(
        [df["client_id"], df["service_key"]], sort=False).shift(1)
    overlapping = df["period_start_key"] <= prior_end       # NaN compares False

    n = int(overlapping.sum())
    return CheckResult(
        name="overlapping_authorization_periods",
        severity=Severity.BLOCK,
        dimension="conformance-relational",
        context="verification",
        passed=n == 0,
        message=(
            f"{n:,} authorisation(s) overlap an earlier authorisation for the same "
            f"child and service. Every session inside an overlap is counted against "
            f"both, so delivered and unused units are wrong for both rows and the "
            f"parity check cannot see it -- SQL and pandas double-count it the same "
            f"way. Reconcile the amended and reissued lines in the payer feed so that "
            f"one child, one service and one date fall under exactly one "
            f"authorisation, then re-run."
            if n else
            f"No overlapping authorisation periods across {len(df):,} authorisations: "
            f"every session attributes to exactly one authorisation."
        ),
        observed=n,
        threshold=0,
        affected_rows=n,
        sample=df.loc[overlapping,
                      ["auth_id", "client_id", "service_key",
                       "period_start_key", "period_end_key"]]
        .head(5).to_dict("records") if n else [],
    )


def check_utilization_ceiling(ctx: dict) -> CheckResult:
    util = ctx["utilization"]
    over = util.loc[util["utilization"] > UTILIZATION_CEILING]
    n = len(over)
    return CheckResult(
        name="utilization_over_ceiling",
        severity=Severity.WARN,
        dimension="plausibility-atemporal",
        context="validation",
        passed=n == 0,
        message=(f"{n:,} authorisations show more units delivered than authorised. "
                 f"This is a compliance and unbilled-revenue exposure, not a win."
                 if n else "No authorisation exceeds its authorised units."),
        observed=n,
        threshold=0,
        affected_rows=n,
        sample=over[["auth_id", "units_authorized", "units_delivered", "utilization"]]
        .head(5).round(3).to_dict("records") if n else [],
    )


def check_zero_unit_authorizations(ctx: dict) -> CheckResult:
    """Sessions delivered against an authorisation that approves no units.

    The case ``check_utilization_ceiling`` cannot see. Over-delivery is caught
    by ``utilization > 1``; over-delivery against a zero-unit authorisation has
    no utilisation at all -- the ratio is undefined, ``build_utilization``
    publishes it as null, and a null is not greater than one. Without a check
    of its own the row is simply absent from every view of the problem.

    WARN, on the same line the rest of the severities are drawn on. The totals
    stay correct: the delivered units are real and sum correctly, the
    authorised units are genuinely zero, and only the per-authorisation ratio
    is unavailable. What is broken is the authorisation record rather than the
    number, and that is the WARN case by this module's own definition.
    """
    util = ctx["utilization"]
    if util.empty or not {"units_authorized", "units_delivered"}.issubset(util.columns):
        bad = util.head(0)
    else:
        bad = util.loc[util["units_authorized"].eq(0) & util["units_delivered"].gt(0)]

    n = len(bad)
    units = float(bad["units_delivered"].sum()) if n else 0.0
    columns = [c for c in ("auth_id", "client_id", "units_authorized",
                           "units_delivered") if c in util.columns]
    return CheckResult(
        name="zero_unit_authorizations",
        severity=Severity.WARN,
        dimension="plausibility-atemporal",
        context="verification",
        passed=n == 0,
        message=(
            f"{n:,} authorisation(s) approve zero units and have {units:,.0f} units "
            f"delivered against them. Utilisation is undefined for these rows and is "
            f"published as null rather than as a zero that would read as total "
            f"non-delivery. Correct the unit count on the authorisation line in the "
            f"payer feed; if no authorisation was ever issued, remove the line so the "
            f"sessions are counted as unauthorised instead of as unattributable."
            if n else
            "No authorisation approves zero units while carrying delivered sessions."
        ),
        observed=n,
        threshold=0,
        affected_rows=n,
        sample=bad[columns].head(5).to_dict("records") if n else [],
    )


def check_distribution_shift(ctx: dict) -> CheckResult:
    """Catch a source-system change that produces plausible-looking numbers.

    A step change in median session length is far more likely to be a vendor
    changing a field than a clinic changing how it treats children.
    """
    fact = ctx["fact_session"]
    dates = ctx["dim_date"][["date_key", "year_month"]]
    df = fact.loc[fact["is_completed"] & fact["uom_resolved"]].merge(dates, on="date_key")
    med = df.groupby("year_month")["minutes_delivered"].median().sort_index()
    if len(med) < 2:
        return CheckResult("session_length_distribution_shift", Severity.WARN, True,
                           "Not enough months to evaluate.", 0, DISTRIBUTION_SHIFT_THRESHOLD)
    change = med.pct_change().abs().fillna(0)
    worst = float(change.max())
    worst_month = str(change.idxmax())
    return CheckResult(
        name="session_length_distribution_shift",
        severity=Severity.WARN,
        dimension="plausibility-temporal",
        context="verification",
        passed=worst <= DISTRIBUTION_SHIFT_THRESHOLD,
        message=(f"Median session length moved {worst:.1%} into {worst_month}. "
                 f"A step change of this size usually means a source-system change."
                 if worst > DISTRIBUTION_SHIFT_THRESHOLD else
                 f"Median session length is stable month to month "
                 f"(largest move {worst:.1%})."),
        observed=round(worst, 4),
        threshold=DISTRIBUTION_SHIFT_THRESHOLD,
    )


def check_coverage_step_change(ctx: dict) -> CheckResult:
    """Find the month the measure started losing rows, and name it.

    Knowing that 5% of sessions are unusable is worth something. Knowing that
    every one of them arrived after a particular month is worth considerably
    more, because it turns an open-ended data-cleaning task into one question
    for one vendor about one release.

    The check walks month-over-month coverage and reports the largest drop.
    """
    from .analytics import coverage_by_month

    cov = coverage_by_month(ctx["fact_session"], ctx["dim_date"])
    if len(cov) < 2:
        return CheckResult("uom_coverage_step_change", Severity.WARN, True,
                           "Not enough months to evaluate.", 0, COVERAGE_STEP_THRESHOLD)

    months = list(cov.index)
    # The first month has no predecessor, so its drop is NaN. It is dropped
    # before taking the maximum so `idxmax` cannot be handed an all-NaN series,
    # and so the index arithmetic below always has a prior month to name.
    drops = (cov.shift(1) - cov).iloc[1:]
    if drops.empty or drops.isna().all():
        return CheckResult("uom_coverage_step_change", Severity.WARN, True,
                           "No month-over-month change to evaluate.", 0,
                           COVERAGE_STEP_THRESHOLD)
    worst_month = str(drops.idxmax())
    worst_drop = float(drops.max())
    idx = months.index(worst_month)
    prior_month = months[idx - 1]
    passed = worst_drop <= COVERAGE_STEP_THRESHOLD

    # Persistence and cleanliness are TESTED, not asserted in prose. A drop
    # that recovers next month is noise; a drop that stays is a release. And
    # "everything before it is clean" is only worth saying if it is true.
    after = cov.iloc[idx:]
    before = cov.iloc[:idx]
    persisted = bool((after <= cov.iloc[idx - 1] - COVERAGE_STEP_THRESHOLD).all())
    prior_clean = bool((before >= UOM_COVERAGE_FLOOR).all())

    if passed:
        message = f"Coverage is stable month to month (largest drop {worst_drop:.1%})."
    else:
        message = (
            f"Unit-of-measure coverage fell {worst_drop:.1%} between {prior_month} "
            f"({cov[prior_month]:.1%}) and {worst_month} ({cov[worst_month]:.1%})"
        )
        message += (" and did not recover in any later month. A step that persists is "
                    "a source change, not noise"
                    if persisted else
                    ", but recovered later, which is more consistent with a one-off "
                    "extract problem than a release")
        message += (f": the defect starts in {worst_month} and every month before it "
                    f"clears the {UOM_COVERAGE_FLOOR:.0%} floor."
                    if prior_clean else
                    f": note that coverage was already below the {UOM_COVERAGE_FLOOR:.0%} "
                    f"floor before {worst_month}, so this is not the only problem.")

    return CheckResult(
        name="uom_coverage_step_change",
        severity=Severity.WARN,
        dimension="plausibility-temporal",
        context="verification",
        passed=passed,
        message=message,
        observed=round(worst_drop, 4),
        threshold=COVERAGE_STEP_THRESHOLD,
        sample=[{"year_month": m, "uom_coverage": round(float(v), 4)}
                for m, v in cov.items()],
    )


def _split_egress_findings(ctx: dict) -> tuple[list, list]:
    """Findings that demonstrate a leak, and findings that suspect one.

    ``task_protect`` runs the boundary check once and passes the result in, so
    the two gates below read the same list rather than scanning twice and
    risking two different answers.
    """
    from .phi import check_egress, is_content_match

    findings = ctx.get("egress_findings")
    if findings is None:
        findings = check_egress(ctx.get("publish_frames", {}))
    return ([f for f in findings if not is_content_match(f)],
            [f for f in findings if is_content_match(f)])


def _egress_sample(findings: list) -> list[dict]:
    return [{"table": f.table, "column": f.column, "reason": f.reason,
             "detail": f.detail} for f in findings[:5]]


def check_phi_egress(ctx: dict) -> CheckResult:
    """Nothing that identifies a person crosses the publication boundary.

    BLOCK, and not negotiable in the way the others are. Every other check in
    this module protects a number; this one protects a person. A utilisation
    figure computed over bad data is an embarrassment. An identifier in a CSV
    that gets emailed to a payer is a reportable breach, and no acknowledgement
    text makes that acceptable -- which is why the fix is to pseudonymise, not
    to release the gate.

    Scoped to the findings that *prove* a leak: a column the contract already
    calls a direct identifier still holding raw values, or a column nobody has
    classified. Both are read off the values, so neither has a false-positive
    mode, which is what makes refusing every override defensible. Regex hits go
    to ``check_phi_content_scan`` instead; see ``phi.is_content_match`` for why
    they must not be gated the same way.
    """
    findings, _ = _split_egress_findings(ctx)

    n = len(findings)
    return CheckResult(
        name="phi_egress",
        severity=Severity.BLOCK,
        acknowledgeable=False,
        dimension="conformance-value",
        passed=n == 0,
        message=(
            f"{n} column(s) would carry identifying information out of the "
            f"publication boundary."
            if n else
            "No direct identifiers and no undeclared columns in anything being "
            "published."
        ),
        observed=n,
        threshold=0,
        affected_rows=n,
        sample=_egress_sample(findings),
    )


def check_phi_content_scan(ctx: dict) -> CheckResult:
    """Identifier-shaped values in a column whose classification says otherwise.

    Still BLOCK -- a source system that has started writing phone numbers into
    a notes field is not something to publish through. But acknowledgeable,
    which ``check_phi_egress`` is not, and the difference is the strength of
    the claim rather than the severity of the consequence.

    This check is a regular expression's opinion about a string. Any pattern
    broad enough to catch a member number a vendor started appending to a free
    text field will also, sooner or later, fire on a payer legitimately named
    "Member Health Network" or on a numeric column that arrived as text.
    Un-overridable, that reading halts publication for good with no route back;
    the operator's only options would be to edit the pattern or to stop
    publishing. Acknowledgeable, the false positive costs a written reason in
    the run log and a name against it, and the true positive still cannot be
    released quietly.
    """
    _, findings = _split_egress_findings(ctx)

    n = len(findings)
    return CheckResult(
        name="phi_content_scan",
        severity=Severity.BLOCK,
        dimension="conformance-value",
        passed=n == 0,
        message=(
            f"{n} column(s) hold values shaped like identifiers that their "
            f"classification does not expect. Read the findings before acting: this "
            f"is a heuristic. If the values are identifiers, fix the source; if they "
            f"are not, release phi_content_scan with a written reason naming the "
            f"column."
            if n else
            "No identifier-shaped values found in anything being published."
        ),
        observed=n,
        threshold=0,
        affected_rows=n,
        sample=_egress_sample(findings),
    )


def check_pseudonym_salt(ctx: dict) -> CheckResult:
    """Whether the published surrogates can be reversed by the people reading them.

    ``phi.is_pseudonymised`` proves the transformation ran. It cannot prove the
    result is worth anything, because it inspects the format and the property
    that matters lives in the key. The identifier space is enumerable -- about
    10^5 client codes -- so a surrogate derived under a salt the reader already
    has is a lookup, not a protection. A reviewer demonstrated exactly that
    against a build whose salt was the constant checked into ``phi.py``:
    240 of 240 published surrogates recovered in about a second.

    So this check reads where the salt came from, never the salt itself, and
    reports one of three states. It refuses acknowledgement in every failing
    one: a written reason cannot make a reversible pseudonym irreversible.

    The severity is not constant, and that is the honest answer rather than a
    convenience. A published constant is a BLOCK, because publishing under it
    hands out re-identifiable data. An ephemeral per-run salt is a WARN,
    because what it costs is utility -- surrogates cannot be joined across
    builds -- and not anyone's privacy. A fresh clone therefore still runs end
    to end, and its report says on its face that the export it produced is not
    comparable to last week's.
    """
    from .phi import (
        SALT_CONFIGURED,
        SALT_DEVELOPMENT_DEFAULT,
        SALT_ENV,
        salt_source,
    )

    fix = f"export {SALT_ENV}=$(openssl rand -hex 32)"
    source = salt_source()

    if source == SALT_CONFIGURED:
        severity, passed = Severity.BLOCK, True
        message = (f"A pseudonym salt is configured in {SALT_ENV}. Surrogates are "
                   f"stable across runs and reversible only by a holder of that "
                   f"salt.")
    elif source == SALT_DEVELOPMENT_DEFAULT:
        severity, passed = Severity.BLOCK, False
        message = (f"{SALT_ENV} is set to the development constant, which is written "
                   f"in phi.py and therefore held by every reader of this "
                   f"repository. Every surrogate this build publishes can be "
                   f"inverted by rainbow table over the client identifier range in "
                   f"about a second. Set a real secret and re-run: {fix}")
    else:
        severity, passed = Severity.WARN, False
        message = (f"{SALT_ENV} is unset, so this run minted a random salt and "
                   f"discarded it at exit. Nothing published here can be "
                   f"precomputed, and nothing published here can be joined to "
                   f"another build's exports -- surrogates for the same client "
                   f"differ between runs. For week-to-week comparability, "
                   f"configure a salt and keep it: {fix}")

    return CheckResult(
        name="pseudonym_salt_configured",
        severity=severity,
        acknowledgeable=False,
        dimension="conformance-computational",
        context="verification",
        passed=passed,
        message=message,
        sample=[{"salt_source": source}],
    )


def check_row_counts(ctx: dict) -> CheckResult:
    return CheckResult(
        name="row_counts",
        severity=Severity.INFO,
        dimension="completeness",
        context="verification",
        passed=True,
        message=(f"sessions {len(ctx['fact_session']):,} | "
                 f"authorisations {len(ctx['fact_authorization']):,} | "
                 f"clients {ctx['dim_client']['client_id'].nunique():,} | "
                 f"client versions {len(ctx['dim_client']):,}"),
        observed=len(ctx["fact_session"]),
    )


CHECKS: list[Callable[[dict], CheckResult]] = [
    check_uom_coverage,
    check_session_reconciliation,
    check_orphan_keys,
    check_duration_plausibility,
    check_scd_integrity,
    check_duplicate_sessions,
    check_unmapped_service_codes,
    check_sessions_without_authorization,
    check_overlapping_authorization_periods,
    check_utilization_ceiling,
    check_zero_unit_authorizations,
    check_distribution_shift,
    check_coverage_step_change,
    check_phi_egress,
    check_phi_content_scan,
    check_pseudonym_salt,
    check_row_counts,
]


def _phi_ruleset_fingerprint() -> dict:
    """The PHI rules, in a form the hash can consume.

    Left out of the fingerprint, a change to the classification table or the
    scanner patterns would silently change the phi_egress verdict while the
    rule-set hash stayed the same -- which is the exact failure the hash exists
    to prevent, and which an earlier version of this function had.

    The patterns go in as their source text rather than as their names, for the
    same reason. Names alone made the fingerprint stable across a rewrite of
    what a pattern matches, so tightening ``phone_us`` from "any ten digits" to
    "a formatted phone number" -- which changes verdicts -- would have produced
    an identical hash.
    """
    from .phi import FIELD_CLASSIFICATION, IDENTIFIER_PATTERNS, SOURCE_ID_PATTERNS

    return {
        "classification": {
            table: {column: sensitivity.value for column, sensitivity in columns.items()}
            for table, columns in FIELD_CLASSIFICATION.items()
        },
        "identifier_patterns": {name: p.pattern
                                for name, p in IDENTIFIER_PATTERNS.items()},
        "source_id_patterns": {name: p.pattern
                               for name, p in SOURCE_ID_PATTERNS.items()},
    }


def ruleset_hash() -> str:
    """Identify the rule set that produced a verdict.

    Without this, a run log says a check passed but not which version of the
    check. Change a threshold and the hash changes, so old verdicts cannot be
    mistaken for statements about the current rules.
    """
    spec = json.dumps(
        {
            "version": RULESET_VERSION,
            "checks": [c.__name__ for c in CHECKS],
            # Every threshold that can change a verdict belongs here. One that
            # is left out makes the hash a partial fingerprint, which is worse
            # than none: it looks like it identifies the rules and does not.
            "thresholds": {
                "uom_coverage_floor": UOM_COVERAGE_FLOOR,
                "utilization_floor": UTILIZATION_FLOOR,
                "utilization_ceiling": UTILIZATION_CEILING,
                "distribution_shift": DISTRIBUTION_SHIFT_THRESHOLD,
                "coverage_step": COVERAGE_STEP_THRESHOLD,
                "min_minutes": MIN_PLAUSIBLE_MINUTES,
                "max_minutes": MAX_PLAUSIBLE_MINUTES,
                "expiry_warning_days": EXPIRY_WARNING_DAYS,
                "at_risk_unused_fraction": AT_RISK_UNUSED_FRACTION,
                # Absent until 1.10.0, which was the partial-fingerprint
                # failure the comment above warns about, found by sabotage:
                # weakening the small-cell threshold to 2 published
                # previously suppressed counts and the hash did not move.
                # Not a check threshold -- it changes what is published, not
                # any verdict -- and that is exactly why it belongs in the
                # fingerprint a reader uses to compare two builds.
                "suppression_threshold": SUPPRESSION_THRESHOLD,
            },
            # The unit conversion table is a definition, not a threshold, and
            # leaving it out made this a partial fingerprint of exactly the
            # kind the comment above warns against. Editing a service's
            # minutes-per-unit changes every hours figure in the warehouse by
            # up to a factor of three while every check still passes -- nothing
            # is invalid, everything is different -- and the hash did not move.
            # The sabotage harness found that: `make prove` case 5.
            "unit_conversions": {s["service_code"]: s["minutes_per_unit"]
                                 for s in SERVICES},
            "phi": _phi_ruleset_fingerprint(),
        },
        sort_keys=True,
    )
    return hashlib.sha256(spec.encode()).hexdigest()[:16]


def run_checks(ctx: dict) -> list[CheckResult]:
    return [check(ctx) for check in CHECKS]


def evaluate_gate(results: list[CheckResult],
                  acknowledgements: dict[str, str] | None = None) -> GateDecision:
    acknowledgements = acknowledgements or {}
    failed_blocks = [r.name for r in results
                     if not r.passed and r.severity is Severity.BLOCK]
    # A check marked unacknowledgeable cannot be released, whatever anyone
    # types. The alternative -- trusting that nobody would acknowledge a PHI
    # failure -- is a policy, and this project's whole argument is that a
    # boundary beats a policy.
    unacknowledgeable = {r.name for r in results if not r.acknowledgeable}
    unresolved = [
        name for name in failed_blocks
        if name not in acknowledgements or name in unacknowledgeable
    ]
    return GateDecision(
        published=not unresolved,
        blocking_failures=failed_blocks,
        acknowledged={k: v for k, v in acknowledgements.items()
                      if k in failed_blocks and k not in unacknowledgeable},
        refused_acknowledgements=sorted(
            k for k in acknowledgements if k in unacknowledgeable),
        results=results,
        ruleset_hash=ruleset_hash(),
        ruleset_version=RULESET_VERSION,
        evaluated_at_utc=datetime.now(UTC).isoformat(timespec="seconds"),
    )


def render_report(decision: GateDecision) -> str:
    """Markdown run log. Written on every run, pass or fail."""
    icon = {True: "PASS", False: "FAIL"}
    lines = [
        "# Data quality report",
        "",
        f"- **Verdict:** {'PUBLISHED' if decision.published else 'PUBLICATION HALTED'}",
        f"- **Evaluated:** {decision.evaluated_at_utc}",
        f"- **Rule set:** v{decision.ruleset_version} (`{decision.ruleset_hash}`)",
        f"- **Checks:** {len(decision.results)} run, "
        f"{sum(1 for r in decision.results if not r.passed)} failed",
        "",
        "| Check | Dimension | Context | Severity | Result | Observed | Threshold | Rows |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in decision.results:
        obs = "" if r.observed is None else f"`{r.observed}`"
        thr = "" if r.threshold is None else f"`{r.threshold}`"
        lines.append(
            f"| `{r.name}` | {r.dimension or '—'} | {r.context} | {r.severity.value} "
            f"| {icon[r.passed]} | {obs} | {thr} | {r.affected_rows:,} |"
        )
    lines += ["", "## Detail", ""]
    for r in decision.results:
        lines += [f"### `{r.name}` — {r.severity.value} — {icon[r.passed]}", "",
                  r.message, ""]
        if r.sample:
            # Fenced as ```json, so it has to be JSON. `default=str` was not
            # enough: it handles types json cannot serialise and does nothing
            # about a float NaN, which Python writes as a bare `NaN` that every
            # strict parser rejects. Sample rows come out of pandas frames and
            # carry NaN freely. That is the same defect that once rendered the
            # dashboard blank, and the same answer applies -- `json_safe` maps
            # them to null, which is what they meant, and `allow_nan=False`
            # makes anything it missed loud here rather than silent in whatever
            # reads this file. The rest of this project already writes JSON
            # that way; a report that did not was the inconsistency.
            from .export import json_safe
            lines += ["```json",
                      json.dumps(json_safe(r.sample), indent=2, allow_nan=False),
                      "```", ""]
    if decision.acknowledged:
        lines += ["## Acknowledged blocking failures", "",
                  "A human released these on purpose. The reason is part of the record.",
                  ""]
        for name, reason in decision.acknowledged.items():
            lines += [f"- **`{name}`** — {reason}"]
        lines.append("")
    if decision.refused_acknowledgements:
        # The report is overwritten every run, so this is the transient copy;
        # run_log.refused_acknowledgements is the durable one. It appears here
        # as well because this is the artifact a person actually reads.
        lines += ["## Refused acknowledgements", "",
                  "A release was attempted for these and denied. The check cannot be "
                  "acknowledged, so the attempt is recorded instead of the reason.",
                  ""]
        lines += [f"- **`{name}`**" for name in decision.refused_acknowledgements]
        lines.append("")
    return "\n".join(lines)
