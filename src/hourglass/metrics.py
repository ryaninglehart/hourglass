"""One definition per metric, and a harness that proves the engines agree.

The problem this exists to solve is mundane and expensive. "Hours delivered"
is computed in three places in this project — in `analytics.py` for the
dashboard, in `sql/analytics.sql` for whoever queries the warehouse directly,
and in `bi/measures.dax` for the Power BI report. Three implementations of one
sentence. Nothing stops them from drifting, and when they do, the failure mode
is not an error: it is two dashboards showing different numbers for the same
week, and an afternoon spent finding out which one lied.

Every organisation that has run a warehouse for more than a year has this
scar. It is why dbt has metrics, why Cube and Malloy and the whole "semantic
layer" category exist, and why "the numbers do not tie out" is the most common
reason a BI project loses its audience.

So: each metric is declared once here, in prose, with the exact expression each
engine is supposed to use. Then the definitions are *checked against each
other*, and the check runs in the pipeline on every build.

**What is actually verified, and what is not.**

* SQL and pandas are both *executed*, over the same warehouse, and their
  results compared to a stated tolerance. This is a real test. It fails if
  someone changes the pandas aggregation and not the query, or writes a join
  in one that fans out and in the other that does not.

* DAX is *not* executed. There is no DAX engine in CI — running one means a
  Power BI workspace, a licence, and a service principal, and this project
  does not have those. What is checked instead is weaker than a contract, and
  is named here for what it is: an **existence-and-reference check**. Every
  registered metric must have a measure of that exact name in
  `bi/measures.dax`, and that measure's body must mention the base columns the
  metric declares. That catches the two things an edit actually does — a
  measure deleted or renamed, and a measure whose column reference has moved
  onto the wrong column, `units_delivered` where the metric is defined on
  `minutes_delivered`.

  It catches nothing else, and the demonstration is worth stating rather than
  leaving to the reader's imagination. A reviewer generated a `.dax` file in
  which every checked measure read `VAR X = 42 RETURN X + 0 * SUM ( <declared
  column> )`, and all ten passed. Mentioning a column is not using it, using it
  is not aggregating it correctly, and none of that is visible to a substring
  search.

  The honest statement is: two of the three engines are verified by execution,
  and the third is checked for the presence of a name and some column
  references. Claiming more than that would be the same species of error the
  rest of this project is built to avoid.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

# ---------------------------------------------------------------------------
# shared SQL
# ---------------------------------------------------------------------------

# The authorisation-grain roll-up, written once.
#
# This is the query an analyst would write by hand to answer "how much of each
# authorisation was used", and it is deliberately a transcription of what
# `analytics.build_utilization` does in pandas -- same filters, same join keys,
# same aggregation order. That correspondence is the whole point: if the two
# ever stop describing the same thing, the parity check says so.
#
# Note the join is on the natural `client_id`, not on `client_key`. The
# surrogate is Type 2, so a client whose payer changed mid-year has two keys,
# and an authorisation spanning that change would match only the sessions on
# one side of it. That defect produces a plausible number rather than an error.
AUTH_GRAIN_CTE = """
WITH sess AS (
    SELECT s.session_id,
           c.client_id,
           s.service_key,
           d.full_date       AS service_date,
           s.units_delivered,
           s.minutes_delivered
    FROM fact_session s
    JOIN dim_client c ON c.client_key = s.client_key
    JOIN dim_date   d ON d.date_key   = s.date_key
    WHERE s.is_completed = 1
      AND s.uom_resolved = 1
),
auth AS (
    SELECT a.auth_id,
           c.client_id,
           a.service_key,
           a.units_authorized,
           ds.full_date        AS period_start,
           de.full_date        AS period_end,
           sv.minutes_per_unit
    FROM fact_authorization a
    JOIN dim_client  c  ON c.client_key  = a.client_key
    JOIN dim_date    ds ON ds.date_key   = a.period_start_key
    JOIN dim_date    de ON de.date_key   = a.period_end_key
    JOIN dim_service sv ON sv.service_key = a.service_key
),
rolled AS (
    SELECT auth.auth_id,
           auth.units_authorized,
           auth.minutes_per_unit,
           COALESCE(SUM(sess.units_delivered), 0)   AS units_delivered,
           COALESCE(SUM(sess.minutes_delivered), 0) AS minutes_delivered,
           COUNT(DISTINCT sess.session_id)          AS session_count
    FROM auth
    LEFT JOIN sess
           ON sess.client_id    = auth.client_id
          AND sess.service_key  = auth.service_key
          AND sess.service_date BETWEEN auth.period_start AND auth.period_end
    GROUP BY auth.auth_id, auth.units_authorized, auth.minutes_per_unit
)
"""


# ---------------------------------------------------------------------------
# the registry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Metric:
    """One business number, defined once, expressed for three engines."""

    key: str
    """Machine name, used in reports and as the parity-check identifier."""

    label: str
    """What a human calls it. Must match the measure name in measures.dax."""

    definition: str
    """The sentence the number means. If this is ambiguous, the metric is."""

    grain: str
    """What one row of the underlying calculation represents."""

    sql: str
    """A query returning exactly one column, named `value`."""

    frame: Callable[[dict[str, pd.DataFrame]], float]
    """The pandas computation, over the same tables."""

    dax_columns: tuple[str, ...] = ()
    """Base columns the DAX measure must reference, filter columns included.

    Filter columns included, which they were not. A metric defined only over
    completed sessions with a resolvable unit depends on `is_completed` and
    `uom_resolved` exactly as much as it depends on the column it sums, and a
    measure that mentions neither is not expressing this definition. Several
    shipped measures do not: they return the right number because
    `transform.py` zeroes the rows those filters would have removed. That is a
    real invariant, asserted in another module, and it is what the agreement
    rests on -- so it is declared here and reported as unreferenced rather than
    quietly left out of the check."""

    dax_measure: str | None = None
    """Measure name in measures.dax, if it differs from `label`."""

    tolerance: float = 1e-6
    """Absolute agreement required between SQL and pandas.

    Not zero, because float addition is not associative and the two engines
    sum 52,160 rows in different orders. A tolerance this tight still catches
    every real disagreement -- a wrong filter or a fanned-out join moves the
    number by whole units, not by 1e-9."""

    caveat: str = ""
    """A known, accepted reason the number is not the whole truth."""

    @property
    def measure_name(self) -> str:
        return self.dax_measure or self.label


def _sum(frames: dict[str, pd.DataFrame], table: str, column: str,
         mask: Callable[[pd.DataFrame], pd.Series] | None = None) -> float:
    df = frames[table]
    if mask is not None:
        df = df.loc[mask(df)]
    return float(df[column].sum())


REGISTRY: list[Metric] = [
    Metric(
        key="units_authorized",
        label="Units Authorized",
        definition="Total units approved by payers across all authorisations "
                   "in scope.",
        grain="one authorisation",
        sql="SELECT SUM(units_authorized) AS value FROM fact_authorization",
        frame=lambda f: _sum(f, "fact_authorization", "units_authorized"),
        dax_columns=("fact_authorization[units_authorized]",),
    ),
    Metric(
        key="units_delivered_completed",
        label="Units Delivered (Completed)",
        definition="Units consumed by sessions that actually happened. A "
                   "cancellation is not delivery.",
        grain="one session",
        sql="SELECT SUM(units_delivered) AS value FROM fact_session "
            "WHERE is_completed = 1",
        frame=lambda f: _sum(f, "fact_session", "units_delivered",
                             lambda d: d["is_completed"].astype(bool)),
        dax_columns=("fact_session[units_delivered]", "fact_session[is_completed]"),
    ),
    Metric(
        key="hours_delivered",
        label="Hours Delivered",
        definition="Delivered therapy time, in hours, summed from the minutes "
                   "recorded on each session.",
        grain="one session",
        sql="SELECT SUM(minutes_delivered) / 60.0 AS value FROM fact_session "
            "WHERE is_completed = 1 AND uom_resolved = 1",
        frame=lambda f: _sum(
            f, "fact_session", "minutes_delivered",
            lambda d: d["is_completed"].astype(bool) & d["uom_resolved"].astype(bool),
        ) / 60.0,
        dax_columns=("fact_session[minutes_delivered]",
                     "fact_session[is_completed]", "fact_session[uom_resolved]"),
        caveat="Excludes sessions whose unit of measure could not be resolved, "
               "so it is a floor. See docs/ANOMALY.md.",
    ),
    Metric(
        key="session_count",
        label="Session Count",
        definition="Distinct sessions recorded, of any status.",
        grain="one session",
        sql="SELECT COUNT(DISTINCT session_id) AS value FROM fact_session",
        frame=lambda f: float(f["fact_session"]["session_id"].nunique()),
        dax_columns=("fact_session[session_id]",),
    ),
    Metric(
        key="authorization_count",
        label="Authorization Count",
        definition="Distinct authorisations in scope.",
        grain="one authorisation",
        sql="SELECT COUNT(DISTINCT auth_id) AS value FROM fact_authorization",
        frame=lambda f: float(f["fact_authorization"]["auth_id"].nunique()),
        dax_columns=("fact_authorization[auth_id]",),
    ),
    Metric(
        key="children_served",
        label="Children Served",
        definition="Distinct children with at least one recorded session.",
        grain="one child",
        # DISTINCT on the natural id, not the surrogate: a client with two SCD2
        # versions is still one child, and counting keys would double them.
        sql="SELECT COUNT(DISTINCT c.client_id) AS value FROM fact_session s "
            "JOIN dim_client c ON c.client_key = s.client_key",
        frame=lambda f: float(
            f["fact_session"]["client_key"]
            .map(f["dim_client"].set_index("client_key")["client_id"])
            .nunique()
        ),
        dax_columns=("dim_client[client_id]",),
    ),
    Metric(
        key="auth_units_delivered",
        label="Units Delivered In Period",
        definition="Units delivered against an authorisation, counting only "
                   "sessions for that child and service inside the "
                   "authorisation's own date window.",
        grain="one authorisation",
        sql=AUTH_GRAIN_CTE + "SELECT SUM(units_delivered) AS value FROM rolled",
        frame=lambda f: _sum(f, "utilization", "units_delivered"),
        dax_columns=(),
        dax_measure=None,
        caveat="Sessions outside every authorisation window are excluded here "
               "and included in Units Delivered (Completed). The two are "
               "different questions.",
    ),
    Metric(
        key="units_unused",
        label="Units Unused",
        definition="Authorised units not delivered, clamped at zero per "
                   "authorisation before summing.",
        grain="one authorisation",
        # MAX(x, 0) per row, then SUM -- not MAX(SUM(a) - SUM(d), 0). With any
        # over-delivered authorisation in scope the two differ, and the second
        # lets an overrun on one child hide an unused balance on another.
        sql=AUTH_GRAIN_CTE + "SELECT SUM(MAX(units_authorized - units_delivered, 0)) "
                             "AS value FROM rolled",
        frame=lambda f: _sum(f, "utilization", "units_unused"),
        dax_columns=("fact_authorization[units_authorized]",
                     "fact_session[is_completed]", "fact_session[uom_resolved]"),
    ),
    Metric(
        key="hours_unused",
        label="Hours Unused",
        definition="Unused authorised units converted to hours using each "
                   "service's own minutes-per-unit.",
        grain="one authorisation",
        # The conversion factor comes from dim_service, per row, before the
        # sum. A unit is 15 minutes for 97153 and 45 for a speech session;
        # dividing units by 4 across the board understates speech and medical
        # authorisations by two to three times, and because the at-risk list
        # sorts by hours, it buries exactly the children it should surface.
        sql=AUTH_GRAIN_CTE + "SELECT SUM(MAX(units_authorized - units_delivered, 0) "
                             "* minutes_per_unit / 60.0) AS value FROM rolled",
        frame=lambda f: _sum(f, "utilization", "hours_unused"),
        dax_columns=("dim_service[minutes_per_unit]",
                     "fact_authorization[units_authorized]",
                     "fact_session[is_completed]", "fact_session[uom_resolved]"),
    ),
    Metric(
        key="hours_authorized",
        label="Hours Authorized",
        definition="Authorised units converted to hours at each service's own "
                   "minutes-per-unit.",
        grain="one authorisation",
        sql=AUTH_GRAIN_CTE + "SELECT SUM(units_authorized * minutes_per_unit / 60.0) "
                             "AS value FROM rolled",
        frame=lambda f: _sum(f, "utilization", "hours_authorized"),
        dax_columns=("dim_service[minutes_per_unit]",
                     "fact_authorization[units_authorized]"),
    ),
    Metric(
        key="authorization_utilization",
        label="Authorization Utilization",
        definition="Delivered units divided by authorised units, summed on "
                   "both sides before dividing.",
        grain="one authorisation",
        # Summed then divided, deliberately. Averaging a per-row ratio gives a
        # 1-unit authorisation the same weight as a 2,000-unit one, which is
        # how a dashboard ends up disagreeing with the warehouse.
        sql=AUTH_GRAIN_CTE + "SELECT SUM(units_delivered) * 1.0 / "
                             "NULLIF(SUM(units_authorized), 0) AS value FROM rolled",
        frame=lambda f: (
            _sum(f, "utilization", "units_delivered")
            / _sum(f, "utilization", "units_authorized")
        ),
        dax_columns=("fact_authorization[units_authorized]",
                     "fact_session[is_completed]", "fact_session[uom_resolved]"),
    ),
]

BY_KEY: dict[str, Metric] = {m.key: m for m in REGISTRY}


# ---------------------------------------------------------------------------
# execution parity: SQL vs pandas
# ---------------------------------------------------------------------------

@dataclass
class ParityResult:
    key: str
    label: str
    sql_value: float | None = None
    frame_value: float | None = None
    tolerance: float = 1e-6
    error: str | None = None

    @property
    def difference(self) -> float:
        if self.sql_value is None or self.frame_value is None:
            return float("nan")
        return self.sql_value - self.frame_value

    @property
    def agrees(self) -> bool:
        if self.error is not None:
            return False
        if self.sql_value is None or self.frame_value is None:
            return False
        return abs(self.difference) <= self.tolerance

    @property
    def relative(self) -> float:
        if not self.sql_value:
            return 0.0
        return abs(self.difference) / abs(self.sql_value)


def check_parity(warehouse: Path,
                 frames: dict[str, pd.DataFrame]) -> list[ParityResult]:
    """Execute every metric both ways and compare.

    `frames` must contain the fact and dimension tables plus `utilization`,
    the authorisation-grain frame `analytics.build_utilization` produces.
    """
    results: list[ParityResult] = []
    conn = sqlite3.connect(warehouse)
    try:
        for metric in REGISTRY:
            result = ParityResult(key=metric.key, label=metric.label,
                                  tolerance=metric.tolerance)
            try:
                row = conn.execute(metric.sql).fetchone()
                result.sql_value = float(row[0]) if row and row[0] is not None else 0.0
            except sqlite3.Error as exc:
                result.error = f"SQL failed: {exc}"
                results.append(result)
                continue
            try:
                result.frame_value = float(metric.frame(frames))
            except (KeyError, ValueError, TypeError, ZeroDivisionError) as exc:
                result.error = f"pandas failed: {type(exc).__name__}: {exc}"
            results.append(result)
    finally:
        conn.close()
    return results


# ---------------------------------------------------------------------------
# static contract: DAX
# ---------------------------------------------------------------------------

MEASURE_RE = re.compile(r"^([A-Z][A-Za-z0-9 ()%/'&+-]*?)\s*=\s*$", re.MULTILINE)


def parse_measures(dax_text: str) -> dict[str, str]:
    """Split a .dax file into `{measure name: body}`.

    Crude on purpose. A DAX parser is a real piece of work and the contract
    being enforced here does not need one: it needs to know which measures
    exist and which columns each mentions.
    """
    lines = dax_text.splitlines()
    measures: dict[str, list[str]] = {}
    current: str | None = None
    for line in lines:
        stripped = line.strip()
        header = MEASURE_RE.match(line) or MEASURE_RE.match(stripped)
        # A measure header is `Name =` at the start of a line, not indented,
        # and not a comment.
        if (header and not line.startswith((" ", "\t", "//"))
                and "(" not in header.group(1)[:1]):
            current = header.group(1).strip()
            measures[current] = []
            continue
        if stripped.startswith("//") and current is None:
            continue
        if current is not None:
            if stripped == "" and measures[current] and measures[current][-1] == "":
                current = None
                continue
            measures[current].append(stripped)
    return {name: "\n".join(body).strip() for name, body in measures.items()}


@dataclass
class ContractResult:
    key: str
    measure: str
    present: bool = False
    missing_columns: list[str] = field(default_factory=list)

    @property
    def holds(self) -> bool:
        return self.present and not self.missing_columns


def check_dax_contract(dax_path: Path) -> list[ContractResult]:
    """Every registered metric has a measure of that name that mentions its columns.

    An existence-and-reference check, and the name of the function is more
    confident than the check is. ``missing_columns`` is a substring test over
    the measure body: it proves the column name appears somewhere between the
    measure's header and the blank line that ends it. It cannot see whether the
    column is aggregated, filtered, multiplied by zero, or referenced inside a
    comment.

    So it detects deletion, renaming, and a reference that moved to the wrong
    column -- the three things an edit does -- and nothing about whether the
    measure computes the metric. A generated file whose every measure read
    ``VAR X = 42 RETURN X + 0 * SUM ( <declared column> )`` passed it in full.
    Executing the DAX is the only thing that would close that gap, and there is
    no engine here to execute it with.
    """
    text = dax_path.read_text(encoding="utf-8") if dax_path.exists() else ""
    measures = parse_measures(text)
    results = []
    for metric in REGISTRY:
        if not metric.dax_columns:
            continue                      # not surfaced in the report
        name = metric.measure_name
        result = ContractResult(key=metric.key, measure=name,
                                present=name in measures)
        if result.present:
            body = measures[name]
            # Follow one level of measure reference: `Units Unused` is defined
            # in terms of `[Units Delivered (Completed)]`, and the column
            # contract has to see through that or it reports false failures.
            seen = set()
            for referenced in re.findall(r"\[([^\]]+)\]", body):
                if referenced in measures and referenced not in seen:
                    seen.add(referenced)
                    body += "\n" + measures[referenced]
            result.missing_columns = [c for c in metric.dax_columns if c not in body]
        results.append(result)
    return results


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------

def render(parity: list[ParityResult],
           contract: list[ContractResult]) -> str:
    disagreements = [p for p in parity if not p.agrees]
    broken = [c for c in contract if not c.holds]

    # The executed result and the static one are stated separately, always.
    # Folded into a single verdict, an unreferenced column in a measure nobody
    # runs in CI would suppress the one claim here that is backed by execution.
    lines = ["# Metric parity", ""]
    if disagreements:
        lines.append(f"**{len(disagreements)} metric(s) disagree between "
                     "SQL and pandas.** Two consumers of this warehouse "
                     "would show different numbers.")
    else:
        lines.append(
            f"**All {len(parity)} metrics agree.** Each one was computed twice "
            "over this build — once by the SQL in this registry and once by the "
            "pandas that produced the dashboard — and the two results matched.")
    lines.append("")
    if broken:
        lines.append(
            f"{len(broken)} of {len(contract)} DAX measures do not reference "
            "every base column their metric is defined on. That is a statement "
            "about the text of `bi/measures.dax`, not about a number — read the "
            "section below before treating it as a defect.")
    else:
        lines.append(
            f"All {len(contract)} DAX measures with declared columns are present "
            "and reference each of them.")
    lines.append("")

    lines += ["## SQL vs pandas", "",
              "| Metric | SQL | pandas | Difference | |",
              "|---|---:|---:|---:|:--|"]
    for p in parity:
        mark = "✓" if p.agrees else "✗"
        if p.error:
            lines.append(f"| {p.label} | — | — | — | ✗ {p.error} |")
            continue
        lines.append(
            f"| {p.label} | {p.sql_value:,.4f} | {p.frame_value:,.4f} | "
            f"{p.difference:,.6f} | {mark} |")
    lines.append("")

    lines += ["## DAX: existence and column references", "",
              "DAX is not executed here — there is no DAX engine in CI. What is "
              "checked is that each measure of the declared name exists and that "
              "its body mentions the base columns the metric is defined on. It is "
              "a substring test, so it catches a measure that was deleted, renamed, "
              "or pointed at the wrong column, and it catches nothing else: a "
              "measure that names a column and then ignores it passes.", "",
              "Where the unreferenced column is a filter — `is_completed`, "
              "`uom_resolved` — the measure still returns the right number today, "
              "because `transform.py` zeroes the rows those filters would remove. "
              "The number is correct and the dependency is on an invariant in "
              "another module rather than in the measure. That is why it is listed "
              "here rather than assumed.", "",
              "| Measure | Present | Columns |", "|---|:--:|:--|"]
    for c in contract:
        cols = "✓" if not c.missing_columns else "not referenced: " + ", ".join(
            f"`{m}`" for m in c.missing_columns)
        lines.append(f"| {c.measure} | {'✓' if c.present else '✗'} | {cols} |")
    lines.append("")

    caveated = [m for m in REGISTRY if m.caveat]
    if caveated:
        lines += ["## Stated caveats", ""]
        for m in caveated:
            lines.append(f"* **{m.label}** — {m.caveat}")
        lines.append("")
    return "\n".join(lines)


def to_markdown_catalogue() -> str:
    """The registry as documentation, so the definitions have one home."""
    lines = ["# Metric definitions", "",
             "Each metric is declared once in `src/hourglass/metrics.py` and "
             "checked on every pipeline run: SQL and pandas are executed and "
             "compared, and the DAX measure is checked for existence and for "
             "references to the base columns listed here. The DAX is not "
             "executed.", ""]
    for m in REGISTRY:
        lines += [f"## {m.label}", "",
                  f"{m.definition}", "",
                  f"* **Grain** — {m.grain}",
                  f"* **Key** — `{m.key}`"]
        if m.measure_name and m.dax_columns:
            lines.append(f"* **Power BI measure** — `{m.measure_name}`")
        if m.caveat:
            lines.append(f"* **Caveat** — {m.caveat}")
        lines += ["", "```sql", m.sql.strip(), "```", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# published-artifact parity: the dashboard payload vs the warehouse
# ---------------------------------------------------------------------------

# `check_parity` compares two implementations of a metric. It does not check
# that either one is what the reader is shown, and that gap was not
# hypothetical.
#
# The dashboard's headline "unused hours" tile was computed in `export.py` as
# `units_unused * 0.25` -- a flat quarter-hour per unit, which is the exact
# error this project was built to argue against, on its most prominent number.
# `metric_parity.md` reported "All 11 metrics agree" throughout, truthfully:
# the registry's `hours_unused` reads the correct per-row column, and the
# published tile was a separate calculation nobody had registered. The check
# was attesting to a number the reader never saw.
#
# So the parity harness now has a second half that starts from the artifact. It
# re-derives each headline figure from the warehouse in SQL and compares it to
# what was actually written to `dashboard_data.json`. Same doctrine as the PHI
# egress scan two modules over: check the artifact, not the intention.

HEADLINE_SQL: dict[str, str] = {
    # Hours are summed through each service's own minutes_per_unit. The whole
    # point of the check is that a flat divisor here would not match.
    "hours_unused": AUTH_GRAIN_CTE + """
        SELECT SUM(MAX(rolled.units_authorized - rolled.units_delivered, 0)
                   * rolled.minutes_per_unit / 60.0) AS value
        FROM rolled
        JOIN auth USING (auth_id)
        WHERE auth.period_start <= :as_of AND auth.period_end >= :as_of
    """,
    "units_authorized": AUTH_GRAIN_CTE + """
        SELECT SUM(rolled.units_authorized) AS value FROM rolled
        JOIN auth USING (auth_id)
        WHERE auth.period_start <= :as_of AND auth.period_end >= :as_of
    """,
    "units_delivered": AUTH_GRAIN_CTE + """
        SELECT SUM(rolled.units_delivered) AS value FROM rolled
        JOIN auth USING (auth_id)
        WHERE auth.period_start <= :as_of AND auth.period_end >= :as_of
    """,
    "active_authorizations": AUTH_GRAIN_CTE + """
        SELECT COUNT(*) AS value FROM rolled
        JOIN auth USING (auth_id)
        WHERE auth.period_start <= :as_of AND auth.period_end >= :as_of
    """,
    "closed_authorizations": AUTH_GRAIN_CTE + """
        SELECT COUNT(*) AS value FROM rolled
        JOIN auth USING (auth_id)
        WHERE auth.period_end < :as_of
    """,
    # The figure a reader sees first, and the one this check did not cover.
    # A sabotage that recomputed pace over every authorisation instead of
    # the open ones published 76.0% where the true figure was 75.1%, and
    # nothing objected. The elapsed-fraction arithmetic mirrors
    # `analytics.build_utilization` exactly: inclusive day counts, capped at
    # the as-of date, clamped to [0, 1].
    "expected_units_to_date": AUTH_GRAIN_CTE + """
        SELECT SUM(rolled.units_authorized *
                   MIN(MAX(MIN(julianday(auth.period_end), julianday(:as_of))
                           - julianday(auth.period_start) + 1, 0)
                       / (julianday(auth.period_end)
                          - julianday(auth.period_start) + 1), 1)) AS value
        FROM rolled
        JOIN auth USING (auth_id)
        WHERE auth.period_start <= :as_of AND auth.period_end >= :as_of
    """,
    "pace": AUTH_GRAIN_CTE + """
        SELECT SUM(rolled.units_delivered)
               / SUM(rolled.units_authorized *
                     MIN(MAX(MIN(julianday(auth.period_end), julianday(:as_of))
                             - julianday(auth.period_start) + 1, 0)
                         / (julianday(auth.period_end)
                            - julianday(auth.period_start) + 1), 1)) AS value
        FROM rolled
        JOIN auth USING (auth_id)
        WHERE auth.period_start <= :as_of AND auth.period_end >= :as_of
    """,
}

HEADLINE_SCOPE: dict[str, str] = {
    "hours_unused": "open authorisations only",
    "units_authorized": "open authorisations only",
    "units_delivered": "open authorisations only",
    "active_authorizations": "open on the as-of date",
    "closed_authorizations": "period ended before the as-of date",
    "expected_units_to_date": "open authorisations only",
    "pace": "units delivered over units expected to date, open authorisations only",
}
"""What each published figure counts.

Not decoration. The registry above publishes `Hours Unused` over *every*
authorisation, 76,362.5, and the dashboard tile publishes it over the open
ones, 57,763.75 -- a difference of 18,599 hours between two rows in the same
report, both correct, both previously labelled "hours unused". A report whose
stated purpose is that a reader can reproduce the number they are shown is the
worst possible place to print two values under one name."""


HEADLINE_TOLERANCES: dict[str, float] = {"pace": 0.0005}
"""Per-figure overrides of the absolute tolerance below.

Pace is a ratio rounded to four decimal places; against a value near 0.75
the half-unit tolerance that suits the summed figures would accept literally
any pace, which is a check in name only."""

HEADLINE_TOLERANCE = 0.5
"""Absolute agreement required on a published headline.

Much looser than the 1e-6 used between engines, and the reason is stated
rather than tuned: the payload rounds to one decimal place before it is
written, so a comparison tighter than half a rounding step fails on the
rounding rather than on a defect. It was 0.05 for one run, which is exactly
half a step, and 57,763.75 against a published 57,763.8 tripped it on float
representation alone.

Half an hour is three orders of magnitude below the 958-hour error this exists
to catch, and any defect worth this check moves a headline by percent, not by
a rounding step. A tolerance chosen to make a failing test pass would be worth
nothing; this one is chosen from the precision of the artifact."""


def check_published_headlines(warehouse: Path,
                              payload: dict) -> list[ParityResult]:
    """Re-derive the dashboard's headline figures from the warehouse.

    Reads `dashboard_data.json` as published and recomputes each figure in SQL
    against the warehouse it claims to describe. A disagreement means the
    number on the tile is not reproducible from the data behind it.
    """
    headline = payload.get("headline", {})
    as_of = payload.get("meta", {}).get("as_of")
    results: list[ParityResult] = []

    # A payload that exists but carries nothing comparable is a failure, not a
    # pass. Returning an empty list here made the whole section vanish from the
    # report and left `task_verify` with nothing to raise on -- a check that
    # silently disappears when its input is malformed is indistinguishable
    # from a check that passed, which is the failure mode this module exists
    # to argue against.
    if not headline or not as_of:
        results.append(ParityResult(
            key="published.payload",
            label="dashboard payload",
            error=("no comparable figures: "
                   + ("`meta.as_of` is missing" if headline
                      else "`headline` is empty or absent")),
        ))
        return results

    conn = sqlite3.connect(warehouse)
    try:
        for key, sql in HEADLINE_SQL.items():
            result = ParityResult(key=f"published.{key}",
                                  label=f"{key} — {HEADLINE_SCOPE[key]}",
                                  tolerance=HEADLINE_TOLERANCES.get(
                                      key, HEADLINE_TOLERANCE))
            if key not in headline:
                result.error = "not present in the published payload"
                results.append(result)
                continue
            try:
                row = conn.execute(sql, {"as_of": as_of}).fetchone()
                result.sql_value = float(row[0]) if row and row[0] is not None else 0.0
                result.frame_value = float(headline[key])
            except (sqlite3.Error, TypeError, ValueError) as exc:
                result.error = f"{type(exc).__name__}: {exc}"
            results.append(result)
    finally:
        conn.close()
    return results


def render_published(results: list[ParityResult]) -> str:
    if not results:
        return ""
    bad = [r for r in results if not r.agrees]
    lines = ["## Published headline figures", "",
             "Re-derived from the warehouse and compared against what was "
             "actually written to `dashboard_data.json`. The section above "
             "compares two implementations of a metric; this one compares the "
             "number a reader sees against the data behind it, which is not "
             "the same question.", ""]
    if bad:
        lines.append(f"**{len(bad)} published figure(s) cannot be reproduced "
                     "from the warehouse.**")
        lines.append("")
    lines += ["| Figure | Warehouse | Published | Difference | |",
              "|---|---:|---:|---:|:--|"]
    for r in results:
        if r.error:
            lines.append(f"| {r.label} | — | — | — | ✗ {r.error} |")
            continue
        lines.append(f"| {r.label} | {r.sql_value:,.4f} | {r.frame_value:,.4f} "
                     f"| {r.difference:,.4f} | {'✓' if r.agrees else '✗'} |")
    lines.append("")
    return "\n".join(lines)
