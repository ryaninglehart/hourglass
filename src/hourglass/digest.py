"""The weekly digest: the pipeline's output for a person, not an analyst.

A dashboard answers questions somebody already thought to ask. It requires a
person to open it, know which panel matters, and remember to do that every
week. Most weeks nobody does, and the authorisation still expires.

This is the other kind of output. It arrives, it says what happened, and it
says what to do about it. Everything else in this project exists to make the
list in here correct.

Written for a clinical operations coordinator, which imposes real constraints:

* **No jargon.** Not "authorisation utilisation is 75.1% against a 90% floor"
  but "we are delivering about three quarters of the therapy hours that have
  been approved." Anyone can read the second one and nobody has to.
* **Grouped by who acts.** Sorted by centre, because a coordinator owns a
  centre. A list sorted by hours at risk is more impressive and less useful.
* **A number attached to a decision.** "34 hours" means nothing on its own.
  "34 hours across 3 children, expiring in 8 days" is a scheduling problem
  with a deadline.
* **Data-quality caveats in the same breath as the numbers**, not in a
  footnote. If 5.6% of sessions could not be counted, the person acting on
  this needs to know the delivered figure is understated before they call a
  family and imply they have missed appointments.

Grouping by centre collides with small-cell suppression, and the collision is
the interesting part. Printing "fewer than 11 children" above a table with one
row per child withholds nothing: the reader counts the rows. Suppressing the
rows instead would protect the families by making the document useless, and a
document nobody can act on does not stop an authorisation expiring. So the
centres below the threshold keep every row and lose their heading: their rows
are pooled into one combined section. What is removed is the attribution of a
small count to a named centre, which is the disclosure; what survives is the
list of calls to make, which is the point.

The digest carries pseudonymised identifiers like every other published
artifact. In a real deployment the last step before sending would re-attach
real names inside the clinical system, to a named recipient, over an
authenticated channel -- which is a different boundary from this one and
deliberately not this module's job.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from . import disclosure
from .config import EXPIRY_WARNING_DAYS, UTILIZATION_FLOOR

ROW_LIMIT = 12
"""Rows shown per centre before the rest are counted rather than listed.

The pooled section multiplies this by the number of centres it holds, so that
pooling never shows fewer rows than the separate sections would have."""

POOLED_SECTION = "Other centres"
"""Heading for the pooled rows. Named as a constant because the tests attack
this section by name, and because nothing else in the digest may reuse it."""

POOLED_NOTE = (
    "These rows come from centres with too few children on this week's list to "
    "name the centre: ten or fewer children at one named centre, in one week, "
    "is often enough to identify the families. Combining the rows removes the "
    "centre, not the work — every row below still needs the same call, and the "
    "client reference finds the family, and their centre, in the clinical "
    "system."
)
"""Why the rows are pooled, said where they are pooled.

A reader who meets an unfamiliar heading and no explanation assumes a system
fault and starts looking for the missing sections."""


def _plural(n: int, singular: str, plural: str | None = None) -> str:
    return singular if n == 1 else (plural or singular + "s")


def _urgency(days: int) -> str:
    if days <= 0:
        return "expires today"
    if days == 1:
        return "1 day left"
    if days <= 7:
        return f"{days} days left"
    return f"{days} days"


@dataclass
class CentrePlan:
    """Which centres are named, which are pooled, and what may be printed.

    Computed once, before the headline paragraph is written, because the
    headline depends on it. A published total, minus the published named
    counts, is the pooled count -- so if the pooled count has to be withheld,
    the total has to go with it. Deciding that after the headline was already
    on the page is how the first version of this leaked.
    """

    named: list[str]
    pooled: list[str]
    published: dict
    pooled_children: int
    order: list[str]
    complementary: list[str]

    @property
    def pooled_is_withheld(self) -> bool:
        return bool(self.pooled) and disclosure.needs_suppression(self.pooled_children)

    @property
    def total_is_recoverable(self) -> bool:
        """The org-wide total would give the pooled count away by subtraction.

        Every named centre publishes its count. Subtract them all from a
        published total and what remains is the pooled section -- the one
        number the pooling exists to withhold. This is ordinary complementary
        suppression, applied at section granularity instead of cell
        granularity, and it is the clause implementations usually miss.
        """
        return self.pooled_is_withheld


def _plan_centres(working: pd.DataFrame) -> CentrePlan:
    """Decide the section layout and what each section may publish."""
    order = list(
        working.groupby("center_name")["hours_unused"].sum()
        .sort_values(ascending=False).index
    )
    counts = pd.DataFrame(
        [{"centre": centre,
          "children": working.loc[working["center_name"] == centre,
                                  "client_id"].nunique()}
         for centre in order],
        columns=["centre", "children"])

    # The complementary pass inside suppress_counts is what stops a lone small
    # centre being recovered by elimination: if exactly one centre is below the
    # threshold, the pooled section would *be* that centre and its name would
    # follow from the list of named sections above it, so the next-smallest
    # centre is pooled with it. Same reasoning the module applies to cells, and
    # the reason not to reimplement it here.
    assessed, report = disclosure.suppress_counts(counts, "children", "centre")
    published = dict(zip(assessed["centre"], assessed["children"], strict=False))
    # A table with no rows never reaches the suppression pass, so the flag
    # column can be absent; nothing to pool is the right reading of that.
    flags = assessed.get("suppressed", [])
    pooled = [centre for centre, hidden
              in zip(assessed["centre"], flags, strict=False) if hidden]
    named = [centre for centre in order if centre not in pooled]
    pooled_children = int(
        working.loc[working["center_name"].isin(pooled), "client_id"].nunique()
    ) if pooled else 0
    return CentrePlan(named=named, pooled=pooled, published=published,
                      pooled_children=pooled_children, order=order,
                      complementary=list(report.complementary))


def build_digest(
    at_risk: pd.DataFrame,
    headline: dict,
    quality: dict,
    meta: dict,
) -> str:
    """Render the digest as Markdown.

    Markdown because it pastes into an email, renders in Slack, opens in any
    editor, and survives being forwarded. A PDF would look better and be harder
    to act on.
    """
    as_of = meta.get("as_of", "")
    coverage = next(
        (r["observed"] for r in quality["results"]
         if r["name"] == "uom_resolution_coverage"), 1.0)
    excluded = next(
        (r["affected_rows"] for r in quality["results"]
         if r["name"] == "uom_resolution_coverage"), 0)

    n_auth = int(headline["at_risk_count"])
    n_kids = int(headline["at_risk_children"])
    hours = float(headline["at_risk_hours"])
    pace = float(headline["pace"])

    # The section layout is decided before a word is written, because the
    # headline total is not independent of it. See CentrePlan.
    working = at_risk.copy()
    if len(working):
        if "center_name" not in working.columns:
            working["center_name"] = "(centre not recorded)"
        working["center_name"] = working["center_name"].fillna(
            "(centre not recorded)")
        plan = _plan_centres(working)
    else:
        plan = CentrePlan(named=[], pooled=[], published={}, pooled_children=0,
                          order=[], complementary=[])

    lines: list[str] = []
    add = lines.append

    add(f"# Authorised hours at risk — week of {as_of}")
    add("")
    add("*Synthetic data. This is a worked example, not a report about real "
        "children.*")
    add("")

    # ---- the one paragraph that matters ----------------------------------
    if n_auth == 0:
        add("**Nothing is expiring unused this week.** Every open authorisation "
            "with time left on it is being delivered at or near the expected "
            "pace.")
    elif disclosure.needs_suppression(n_kids) or plan.total_is_recoverable:
        # Two reasons to withhold the organisation-wide head count, and the
        # second is the one that is easy to miss.
        #
        # The first: the total is itself small. It is tempting to exempt a
        # total as "just a total", but three children across five centres,
        # printed above a table of centres, is more disclosive than any single
        # cell in it.
        #
        # The second: the total is large but every named centre publishes its
        # own count, so total minus the named counts is the pooled count --
        # exactly the number the pooling withheld. Forty-five children, one
        # named centre of forty, and the pooled section is five. That is
        # complementary suppression at section granularity, and the earlier
        # version of this function named the attack in its own footnote while
        # committing it two paragraphs above.
        reason = ("small enough to identify the families involved"
                  if disclosure.needs_suppression(n_kids)
                  else "it would otherwise reveal, by subtraction from the "
                       "named centres below, a group small enough to identify "
                       "the families involved")
        add(f"**Fewer than {disclosure.SUPPRESSION_THRESHOLD + 1} children have "
            f"approved therapy hours that will expire in the next "
            f"{EXPIRY_WARNING_DAYS} days without being used.** The exact number is "
            f"withheld because {reason}."
            if disclosure.needs_suppression(n_kids) else
            f"**Children across several centres have approved therapy hours "
            f"that will expire in the next {EXPIRY_WARNING_DAYS} days without "
            f"being used.** The total is withheld because {reason}.")
        add("")
        add(f"That is **{hours:,.0f} hours** across {n_auth} "
            f"{_plural(n_auth, 'authorisation')}. Once the authorisation period "
            f"ends, the unused hours are gone — they do not roll forward, and "
            f"re-authorising takes time the family does not get back.")
    else:
        add(f"**{n_kids} {_plural(n_kids, 'child', 'children')} "
            f"{'has' if n_kids == 1 else 'have'} approved therapy hours that will "
            f"expire in the next {EXPIRY_WARNING_DAYS} days without being used.**")
        add("")
        add(f"That is **{hours:,.0f} hours** across {n_auth} "
            f"{_plural(n_auth, 'authorisation')}. Once the authorisation period "
            f"ends, the unused hours are gone — they do not roll forward, and "
            f"re-authorising takes time the family does not get back.")
    add("")

    # ---- the overall picture, in plain language --------------------------
    add("### How we are doing overall")
    add("")
    add(f"Across every open authorisation we are delivering about "
        f"**{pace:.0%}** of the hours we would expect to have delivered by now. "
        f"The target is {UTILIZATION_FLOOR:.0%}.")
    add("")
    if pace < UTILIZATION_FLOOR:
        gap = UTILIZATION_FLOOR - pace
        add(f"That is {gap:.0%} below target. Some of that is cancellations and "
            f"no-shows, which are normal. The list below is the part that is "
            f"still fixable this week.")
    else:
        add("That is at or above target.")
    add("")

    # ---- the caveat, next to the number, not in a footnote ---------------
    if coverage < 0.99:
        add("> **Before you use these numbers.** Our therapy system changed how it "
            f"records session length, and **{excluded:,} sessions** since April "
            f"cannot be read reliably. They are excluded rather than guessed at, "
            f"so the hours-delivered figures here are **understated** — a family "
            f"may have attended more than this shows. Confirm in the clinical "
            f"record before contacting anyone about missed appointments. "
            f"Coverage is currently {coverage:.1%}; the vendor fix is tracked "
            f"under DE-412.")
        add("")

    # ---- the work ---------------------------------------------------------
    if n_auth:
        add("---")
        add("")
        add("## Who to contact, by centre")
        add("")
        add("Ordered by how soon the authorisation expires. The client reference "
            "is the de-identified key — look the family up in the clinical system "
            "with it.")
        add("")

        # Per-centre child counts are the disclosive part of this report. A
        # centre serving twenty children, sliced by service and week, produces
        # cells of two and three -- and "3 children, ABA, San Diego" identifies
        # them to anyone who works there, whatever the client reference says.
        # CMS's cell size suppression policy is applied before any count is
        # printed. See hourglass.disclosure.
        #
        # Printing the count as "<11" is not enough on its own, and that is the
        # defect this shape exists to close: the rows beneath a centre heading
        # are one per authorisation, so counting the distinct client references
        # recovers the number that was withheld. A centre below the threshold
        # therefore loses its heading rather than its rows.
        #
        # The plan was computed at the top of this function, before the
        # headline, because the headline total depends on it. See CentrePlan.
        published, pooled, named = plan.published, plan.pooled, plan.named

        def add_section(title: str, centres: list[str], count_text: str,
                        note: str = "") -> None:
            rows = (working.loc[working["center_name"].isin(centres)]
                    .sort_values(["days_to_expiry", "hours_unused"],
                                 ascending=[True, False]))
            add(f"### {title}")
            add("")
            # Hours and authorisation counts are not counts of people, so they
            # are published for a pooled section as they are for a named one.
            add(f"{count_text} · {rows['hours_unused'].sum():,.0f} hours at risk · "
                f"{len(rows)} {_plural(len(rows), 'authorisation')}")
            add("")
            if note:
                add(note)
                add("")
            # No centre column, by design. Sorting by expiry rather than by
            # centre matters for the same reason: a pooled table ordered by
            # centre is a centre-labelled table with the labels taken off.
            add("| Client | Service | Hours left | Used so far | Expires | Payer |")
            add("|---|---|---:|---:|---|---|")
            limit = ROW_LIMIT * len(centres)
            for _, r in rows.head(limit).iterrows():
                used = r["utilization"]
                vb = " ⬥" if r.get("contract_type") == "value_based" else ""
                add(f"| `{r['client_id']}` | {r['service_name']} "
                    f"| **{r['hours_unused']:,.1f}** | {used:.0%} "
                    f"| {_urgency(int(r['days_to_expiry']))} "
                    f"| {r['payer_name']}{vb} |")
            if len(rows) > limit:
                where = ("at this centre" if len(centres) == 1
                         else "across these centres")
                add(f"| … | *{len(rows) - limit} more {where}* | | | | |")
            add("")

        for centre in named:
            kids = int(published[centre])
            add_section(centre, [centre],
                        f"{kids} {_plural(kids, 'child', 'children')}")

        if pooled:
            kids = plan.pooled_children
            if plan.pooled_is_withheld:
                # Pooling small centres together can still leave a combined
                # count inside the withheld range. The rows stay; the number
                # does not. This is a real limit of pooling: a reader can count
                # the references and arrive at the combined figure. What they
                # cannot do is attach it to a centre.
                count_text = (f"fewer than {disclosure.SUPPRESSION_THRESHOLD + 1} "
                              f"children (exact number withheld)")
            else:
                count_text = f"{kids} {_plural(kids, 'child', 'children')}"
            add_section(POOLED_SECTION, pooled, count_text, note=POOLED_NOTE)

        add("⬥ marks a value-based contract, where undelivered authorised care "
            "also affects the outcome measures the contract is paid on.")
        add("")
        if pooled:
            small = len(pooled) - len(plan.complementary)
            explanation = (
                f"> **Why some centres are not named.** {small} "
                f"{_plural(small, 'centre')} had ten or fewer children on this "
                f"list, which is few enough to identify the families involved, "
                f"so those rows are listed under \"{POOLED_SECTION}\" rather "
                f"than under a centre name.")
            if plan.complementary:
                explanation += (
                    " One further centre was combined with them, because a "
                    "combined section holding a single centre could be "
                    "recovered by subtracting the named centres from the total.")
            explanation += (
                " The authorisations themselves are unaffected — nothing has "
                "been withheld from the list, and every row can still be acted "
                "on.")
            add(explanation)
            add("")

    # ---- what to do -------------------------------------------------------
    add("---")
    add("")
    add("## What to do with this")
    add("")
    steps = ["**Start with the rows expiring inside a week.** Those are the only "
             "ones where scheduling can still change the outcome."]
    if coverage < 0.99:
        # Conditional, because this instruction is only true while the source
        # issue is open. An action list that tells people to distrust the
        # numbers after the numbers have been fixed trains them to distrust
        # the numbers.
        steps.append("**Check the clinical record before calling.** The "
                     "hours-delivered figures come from the scheduling system and "
                     "are understated while the session-length issue is open.")
    steps += [
        "**If the family cannot use the hours, say so early.** A request to amend "
        "or re-authorise takes longer than the time remaining on most of these.",
        "**Tell us if a row looks wrong.** A row that looks wrong usually is, and "
        "it usually means something upstream needs fixing rather than this one "
        "family.",
    ]
    for i, step in enumerate(steps, 1):
        add(f"{i}. {step}")
    add("")

    add("---")
    add("")
    add(f"*Generated {meta.get('generated_at_utc', '')} from run "
        f"`{meta.get('run_id', '')}` · data as of {as_of} · "
        f"measure coverage {coverage:.1%} · all figures synthetic.*")

    return "\n".join(lines)


def write_digest(markdown: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "weekly_digest.md"
    path.write_text(markdown, encoding="utf-8")
    return path
