"""Small-cell suppression.

The tests that matter are the ones about *recovery*: a suppression scheme that
hides a number but leaves it derivable has done nothing except make the report
harder to read.
"""

from __future__ import annotations

import pandas as pd
import pytest

from hourglass import disclosure
from hourglass.disclosure import SUPPRESSED


def table(*rows) -> pd.DataFrame:
    return pd.DataFrame(
        [{"centre": c, "service": s, "children": n, "pct": p}
         for c, s, n, p in rows]
    )


class TestThreshold:
    @pytest.mark.parametrize("value,expected", [
        (0, False),      # zero discloses nothing about anyone
        (1, True), (5, True), (10, True),
        (11, False), (250, False),
        (None, False),
    ])
    def test_boundary(self, value, expected):
        assert disclosure.needs_suppression(value) is expected

    def test_zero_is_publishable(self):
        """"No children" is not a disclosure; "three children" is."""
        assert disclosure.needs_suppression(0) is False

    def test_non_numeric_is_not_suppressed(self):
        assert disclosure.needs_suppression("n/a") is False


class TestPrimarySuppression:
    def test_small_cells_are_masked(self):
        df = table(("SD", "ABA", 3, 0.05), ("SD", "Speech", 40, 0.60),
                   ("SD", "OT", 22, 0.35))
        out, report = disclosure.suppress_counts(df, "children", "service")
        assert out.loc[0, "children"] == SUPPRESSED
        assert report.primary == ["ABA"]
        assert out.loc[1, "children"] != SUPPRESSED      # the largest survives

    def test_a_two_row_table_must_suppress_both(self):
        """Not an over-reaction -- arithmetic.

        With two cells and a published total, hiding one leaves it equal to
        total minus the other. There is no way to protect a small cell in a
        two-row table except to suppress the table.
        """
        df = table(("SD", "ABA", 3, 0.07), ("SD", "Speech", 40, 0.93))
        out, report = disclosure.suppress_counts(df, "children", "service")
        assert (out["children"] == SUPPRESSED).all()
        assert report.primary == ["ABA"]
        assert report.complementary == ["Speech"]

    def test_derived_columns_go_with_the_count(self):
        """A visible percentage recovers a suppressed count."""
        df = table(("SD", "ABA", 3, 0.05), ("SD", "Speech", 40, 0.60),
                   ("SD", "OT", 22, 0.35))
        out, _ = disclosure.suppress_counts(
            df, "children", "service", derived_columns=("pct",))
        assert out.loc[0, "pct"] == SUPPRESSED

    def test_the_mask_says_withheld_not_zero(self):
        """Blank reads as "no data" and zero reads as "none". Both are lies."""
        df = table(("SD", "ABA", 3, 0.05), ("SD", "Speech", 40, 0.60),
                   ("SD", "OT", 22, 0.35))
        out, _ = disclosure.suppress_counts(df, "children", "service")
        assert out.loc[0, "children"] == "<11"

    def test_a_clean_table_is_untouched(self):
        df = table(("SD", "ABA", 40, 0.5), ("SD", "Speech", 40, 0.5))
        out, report = disclosure.suppress_counts(df, "children", "service")
        assert report.suppressed_count == 0
        assert list(out["children"]) == [40, 40]


class TestComplementarySuppression:
    def test_a_lone_suppression_is_recoverable_so_a_second_is_added(self):
        """The clause implementations usually miss.

        One suppressed cell plus a published total is subtraction, not privacy.
        """
        df = table(("SD", "ABA", 3, 0.05), ("SD", "Speech", 40, 0.60),
                   ("SD", "OT", 22, 0.35))
        out, report = disclosure.suppress_counts(df, "children", "service")
        assert len(report.primary) == 1
        assert len(report.complementary) == 1
        assert (out["children"] == SUPPRESSED).sum() == 2

    def test_the_second_victim_is_the_smallest_available(self):
        """Withhold the least information that closes the hole."""
        df = table(("SD", "ABA", 3, 0.05), ("SD", "Speech", 90, 0.60),
                   ("SD", "OT", 22, 0.35))
        out, report = disclosure.suppress_counts(df, "children", "service")
        assert report.complementary == ["OT"]
        assert out.loc[1, "children"] == 90          # the largest survives

    def test_two_primary_suppressions_need_no_complement(self):
        """Two unknowns and one equation cannot be solved."""
        df = table(("SD", "ABA", 3, 0.05), ("SD", "Speech", 4, 0.06),
                   ("SD", "OT", 60, 0.89))
        out, report = disclosure.suppress_counts(df, "children", "service")
        assert len(report.complementary) == 0
        assert (out["children"] == SUPPRESSED).sum() == 2

    def test_no_complement_when_no_total_is_published(self):
        """Without a total there is nothing to subtract from."""
        df = table(("SD", "ABA", 3, 0.05), ("SD", "Speech", 40, 0.60))
        out, report = disclosure.suppress_counts(
            df, "children", "service", total_is_published=False)
        assert report.complementary == []
        assert (out["children"] == SUPPRESSED).sum() == 1

    def test_one_complementary_pass_reaches_the_fixed_point(self):
        """The claim the module used to make, tested instead of asserted.

        An earlier docstring said suppression "repeats to a fixed point"; the
        loop it described always exited after one pass. Both cannot be right,
        and the docstring was the one that was wrong: a table with two hidden
        cells and one published total is two unknowns against one equation, so
        re-running the pass finds nothing left to do. Feeding the output back
        in and getting the same output is what "sufficient" means here.
        """
        df = table(("SD", "ABA", 3, 0.05), ("SD", "Speech", 40, 0.60),
                   ("SD", "OT", 22, 0.35))
        once, first = disclosure.suppress_counts(df, "children", "service")
        twice, second = disclosure.suppress_counts(
            once.drop(columns=["suppressed"]), "children", "service")
        assert (once["children"] == SUPPRESSED).sum() == 2
        assert (twice["children"] == SUPPRESSED).sum() == 2
        assert second.complementary == []
        assert first.suppressed_count == 2

    def test_a_single_row_table_cannot_be_protected_by_a_complement(self):
        """Honest limit: one row, one suppression, nothing left to sacrifice."""
        df = table(("SD", "ABA", 3, 1.0))
        out, report = disclosure.suppress_counts(df, "children", "service")
        assert out.loc[0, "children"] == SUPPRESSED
        assert report.complementary == []


class TestGrouped:
    def test_each_group_is_protected_independently(self):
        df = table(("SD", "ABA", 3, 0.1), ("SD", "Speech", 40, 0.6),
                   ("SD", "OT", 22, 0.3),
                   ("TEM", "ABA", 55, 0.5), ("TEM", "Speech", 60, 0.5))
        out, _ = disclosure.suppress_grouped(df, "centre", "children", "service")
        sd = out[out["centre"] == "SD"]
        tem = out[out["centre"] == "TEM"]
        assert (sd["children"] == SUPPRESSED).sum() == 2      # primary + complement
        assert (tem["children"] == SUPPRESSED).sum() == 0

    def test_derived_columns_are_suppressed_in_the_grouped_path_too(self):
        """A gap found by mutation testing, not by reading the code.

        Deleting the `derived_columns` argument from the inner call left every
        example test passing: the ungrouped path asserted the rule and created
        the impression it held everywhere, while no grouped test ever passed a
        non-empty `derived_columns`. A masked count beside a visible percentage
        is recovered by multiplication, so the mutant produced a suppression
        scheme that suppresses nothing.
        """
        df = table(("SD", "ABA", 3, 0.05), ("SD", "Speech", 40, 0.60),
                   ("SD", "OT", 22, 0.35),
                   ("TEM", "ABA", 55, 0.5), ("TEM", "Speech", 60, 0.5))
        out, _ = disclosure.suppress_grouped(
            df, "centre", "children", "service", derived_columns=("pct",))
        masked = out["children"] == SUPPRESSED
        assert masked.sum() == 2
        assert (out.loc[masked, "pct"] == SUPPRESSED).all()
        assert (out.loc[~masked, "pct"] != SUPPRESSED).all()

    def test_report_aggregates_across_groups(self):
        df = table(("SD", "ABA", 3, 0.1), ("SD", "Speech", 40, 0.6),
                   ("SD", "OT", 22, 0.3),
                   ("TEM", "ABA", 2, 0.1), ("TEM", "Speech", 90, 0.6),
                   ("TEM", "OT", 30, 0.3))
        _, report = disclosure.suppress_grouped(df, "centre", "children", "service")
        assert len(report.primary) == 2
        assert len(report.complementary) == 2
        assert report.total_cells == 6


class TestTheFlagColumn:
    """`suppressed` is not decoration -- another module routes on it.

    `digest._plan_centres` reads this column to decide which centres lose
    their heading and get pooled. Every test above asserts on the *masked
    values*, and mutation testing found the consequence: replacing the whole
    column with `None` left all of them passing while the digest silently
    pooled nothing and published every small centre by name.

    A column another module makes a privacy decision on needs its own
    assertions, not assertions about the thing it is derived from.
    """

    def test_the_flag_marks_exactly_the_masked_rows(self):
        df = table(("SD", "ABA", 3, 0.05), ("SD", "Speech", 40, 0.60),
                   ("SD", "OT", 22, 0.35))
        out, _ = disclosure.suppress_counts(df, "children", "service")
        assert list(out["suppressed"]) == [True, False, True]
        assert list(out["children"] == SUPPRESSED) == list(out["suppressed"])

    def test_the_flag_is_boolean_not_none(self):
        """`if hidden` is falsy for None, so a null column pools nothing and
        raises nothing."""
        df = table(("SD", "ABA", 3, 0.05), ("SD", "Speech", 40, 0.60),
                   ("SD", "OT", 22, 0.35))
        out, _ = disclosure.suppress_counts(df, "children", "service")
        assert out["suppressed"].dtype == bool
        assert out["suppressed"].notna().all()

    def test_a_clean_table_flags_nothing(self):
        df = table(("SD", "ABA", 40, 0.5), ("SD", "Speech", 40, 0.5))
        out, _ = disclosure.suppress_counts(df, "children", "service")
        assert not out["suppressed"].any()

    def test_the_grouped_path_carries_the_flag_too(self):
        df = table(("SD", "ABA", 3, 0.1), ("SD", "Speech", 40, 0.6),
                   ("SD", "OT", 22, 0.3),
                   ("TEM", "ABA", 55, 0.5), ("TEM", "Speech", 60, 0.5))
        out, _ = disclosure.suppress_grouped(df, "centre", "children", "service")
        assert out["suppressed"].sum() == 2
        assert list(out.loc[out["centre"] == "TEM", "suppressed"]) == [False, False]


class TestFrameShape:
    """Row order and index, which callers depend on and no assertion covered.

    Both found by mutation testing rather than by review: `sort=False` and
    `ignore_index=True` could each be flipped with the whole file still green.
    """

    def test_group_order_follows_the_input_not_the_alphabet(self):
        """`suppress_grouped` preserves first-appearance order deliberately.

        The digest orders centres by hours at risk and expects that order
        back. Sorting alphabetically would silently reorder the worklist a
        coordinator reads top-down.
        """
        df = table(("TEM", "ABA", 55, 0.5), ("TEM", "Speech", 60, 0.5),
                   ("SD", "ABA", 40, 0.5), ("SD", "Speech", 44, 0.5))
        out, _ = disclosure.suppress_grouped(df, "centre", "children", "service")
        assert list(out["centre"]) == ["TEM", "TEM", "SD", "SD"]

    def test_the_grouped_result_has_a_clean_index(self):
        """Concatenated groups keep their original labels unless reset.

        The rows are interleaved on purpose. With the centres already
        contiguous, grouping and re-concatenating happens to reproduce the
        original index and the assertion holds whether or not the index was
        reset -- a test that cannot distinguish the two. Interleaved, the
        groups come back as 0, 2, 1, 3 unless the index is rebuilt, so the
        assertion has something to fail on.
        """
        df = table(("TEM", "ABA", 55, 0.5), ("SD", "ABA", 40, 0.5),
                   ("TEM", "Speech", 60, 0.5), ("SD", "Speech", 44, 0.5))
        out, _ = disclosure.suppress_grouped(df, "centre", "children", "service")
        assert list(out.index) == [0, 1, 2, 3]
        assert list(out["service"]) == ["ABA", "Speech", "ABA", "Speech"]


class TestVerification:
    def test_a_suppressed_table_reports_clean(self):
        df = table(("SD", "ABA", 3, 0.1), ("SD", "Speech", 40, 0.6),
                   ("SD", "OT", 22, 0.3))
        out, _ = disclosure.suppress_counts(df, "children", "service")
        assert disclosure.is_disclosive(out, "children") == []

    def test_the_verifier_is_capable_of_failing(self):
        """Guards the test above from passing vacuously."""
        df = table(("SD", "ABA", 3, 0.1), ("SD", "Speech", 40, 0.9))
        assert disclosure.is_disclosive(df, "children") == [0]

    def test_a_two_row_table_is_fully_suppressed_and_verifies_clean(self):
        df = table(("SD", "ABA", 3, 0.07), ("SD", "Speech", 40, 0.93))
        out, _ = disclosure.suppress_counts(df, "children", "service")
        assert disclosure.is_disclosive(out, "children") == []


class TestReporting:
    def test_summary_states_both_kinds(self):
        df = table(("SD", "ABA", 3, 0.05), ("SD", "Speech", 40, 0.6),
                   ("SD", "OT", 22, 0.35))
        _, report = disclosure.suppress_counts(df, "children", "service")
        summary = report.summary()
        assert "below the disclosure threshold" in summary
        assert "recovery by subtraction" in summary

    def test_summary_when_nothing_is_suppressed(self):
        df = table(("SD", "ABA", 40, 0.5), ("SD", "Speech", 40, 0.5))
        _, report = disclosure.suppress_counts(df, "children", "service")
        assert "No cells suppressed" in report.summary()
