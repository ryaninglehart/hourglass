"""Small-cell suppression.

A count of three children in one centre, for one service, in one week is not an
aggregate. It is three families, and in a paediatric autism programme where a
centre might serve twenty children, "3" plus a discipline plus a date is often
enough to identify them to anyone who works there.

Pseudonymising the identifier does not help. The disclosure is in the *count*.

CMS's cell size suppression policy is the standard applied here: **no cell with
a value of 1 to 10 may be reported**, zero is fine, and — the part people miss —
no cell may be published that lets a 1-to-10 value be *derived*. That second
clause is what makes this harder than a filter, and it has three consequences:

**1. Rates go with their numerators.** Publishing "12.5% of 24" recovers the
count. A suppressed count suppresses its own percentage and, where the
denominator is small enough to invert, the denominator too.

**2. Complementary suppression.** If one cell in a row is suppressed and the
row total is published, subtraction recovers it. So a second cell must go —
conventionally the next smallest, because it costs the least information. This
module implements that, and it is the part that is usually missing from
implementations that claim to do suppression.

**3. One complementary suppression is enough, and no more are needed.** Said
explicitly because the opposite is widely claimed and is not true of the tables
published here. These are one-dimensional: a list of cells and one published
total, with no second margin for a suppression to propagate along. A single
equation cannot be solved for two unknowns, so once a second cell is hidden the
table is safe, and hiding a third protects nothing further. The grouped path
does not change that -- each group is its own one-dimensional table with its
own subtotal, resolved independently, with no column margin joining them. There
is no fixed point to iterate towards, so the implementation does not iterate;
an earlier version of this module claimed it did, while its loop always exited
after the first pass. Cascading is real for tables with margins in more than one
direction, which is the case below.

Deliberately not implemented: full linear-programming cell suppression, which
is the rigorous solution for multi-dimensional tables with margins in several
directions. That is a genuinely hard optimisation problem and a greedy pass is
not equivalent to it. What is here is correct for the tables this project
publishes — one grouping dimension plus a total, one group at a time — and the
limitation is stated rather than glossed. Point a table with row totals *and*
column totals at this module and it will suppress each row and leave the
columns disclosive.

Reference: CMS cell size suppression policy; see docs/DEFENSE.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

SUPPRESSION_THRESHOLD = 10
"""Cells with a count from 1 to this value inclusive are suppressed. Zero is
publishable: "no children" discloses nothing about any child."""

SUPPRESSED = "<11"
"""What a suppressed cell shows. Not blank, and not zero.

Blank reads as "no data" and zero reads as "none", and both are lies that a
reader will act on. `<11` says a real value exists and has been withheld, which
is the truth and is also what makes the suppression auditable."""


@dataclass
class SuppressionReport:
    total_cells: int = 0
    primary: list[str] = field(default_factory=list)
    complementary: list[str] = field(default_factory=list)

    @property
    def suppressed_count(self) -> int:
        return len(self.primary) + len(self.complementary)

    def summary(self) -> str:
        if not self.suppressed_count:
            return f"No cells suppressed ({self.total_cells} published)."
        return (
            f"{self.suppressed_count} of {self.total_cells} cells suppressed: "
            f"{len(self.primary)} below the disclosure threshold, "
            f"{len(self.complementary)} to prevent recovery by subtraction."
        )


def needs_suppression(value) -> bool:
    """A count from 1 to the threshold. Zero and null are publishable."""
    if value is None or pd.isna(value):
        return False
    try:
        n = float(value)
    except (TypeError, ValueError):
        return False
    return 0 < n <= SUPPRESSION_THRESHOLD


def suppress_counts(
    df: pd.DataFrame,
    count_column: str,
    label_column: str,
    derived_columns: tuple[str, ...] = (),
    total_is_published: bool = True,
) -> tuple[pd.DataFrame, SuppressionReport]:
    """Suppress small cells in a one-dimensional table, plus one complement.

    ``derived_columns`` are values computed from the count -- a rate, a share,
    an hours figure -- which have to be suppressed alongside it. Leaving a
    percentage visible next to a suppressed count is the most common way an
    implementation defeats itself.

    ``total_is_published`` is the whole reason complementary suppression is
    needed. If the reader cannot see a total, one suppressed cell is safe. If
    they can, one suppressed cell is arithmetic.
    """
    out = df.copy()
    report = SuppressionReport(total_cells=len(out))
    if out.empty:
        return out, report

    counts = pd.to_numeric(out[count_column], errors="coerce")
    suppressed = counts.map(needs_suppression)
    report.primary = out.loc[suppressed, label_column].astype(str).tolist()

    # Complementary suppression. With the total visible, exactly one suppressed
    # cell is recoverable by subtraction; a second cell is sacrificed to protect
    # it. The next-smallest is chosen because it withholds the least.
    #
    # Once. Not until stable: the condition that made the table unsafe was
    # "exactly one hidden cell against one published total", and hiding a second
    # ends it permanently -- two unknowns, one equation. There is no state this
    # table can be left in that a further pass would improve, which is why the
    # loop that used to be here could only ever execute its body once.
    if total_is_published and suppressed.sum() == 1:
        candidates = counts.where(~suppressed)
        if candidates.notna().sum():
            victim = candidates.idxmin()
            suppressed.loc[victim] = True
            report.complementary.append(str(out.loc[victim, label_column]))

    for column in (count_column, *derived_columns):
        if column in out.columns:
            out[column] = out[column].astype(object)
            out.loc[suppressed, column] = SUPPRESSED

    out["suppressed"] = suppressed.to_numpy()
    return out, report


def suppress_grouped(
    df: pd.DataFrame,
    group_column: str,
    count_column: str,
    label_column: str,
    derived_columns: tuple[str, ...] = (),
) -> tuple[pd.DataFrame, SuppressionReport]:
    """Apply suppression within each group of a two-level table.

    Each group is treated as its own table with its own published subtotal,
    which is how the digest presents them: a section per centre, each with a
    stated total. Groups are resolved independently and nothing propagates
    between them, because no column margin is published across groups -- that
    is what would make this genuinely two-dimensional, and what would make a
    suppression in one group leave a hole in another.
    """
    frames, combined = [], SuppressionReport()
    for _, group in df.groupby(group_column, sort=False):
        done, report = suppress_counts(
            group, count_column, label_column, derived_columns)
        frames.append(done)
        combined.total_cells += report.total_cells
        combined.primary += report.primary
        combined.complementary += report.complementary
    if not frames:
        return df.copy(), combined
    return pd.concat(frames, ignore_index=True), combined


def is_disclosive(df: pd.DataFrame, count_column: str) -> list[int]:
    """Row positions still carrying a publishable small count.

    A verification, not a transformation. `suppress_counts` promises the table
    is safe; this reads the table back and checks. Same principle as the PHI
    egress scan: check the artifact, not the intention.
    """
    counts = pd.to_numeric(df[count_column], errors="coerce")
    return [i for i, value in enumerate(counts) if needs_suppression(value)]
