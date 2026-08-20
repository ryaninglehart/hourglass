"""The weekly digest.

Tested for the things that would make it harmful rather than merely ugly:
leaking an identifier, quoting a number without its caveat, or reading as
though it were about real children.
"""

from __future__ import annotations

import re
from typing import ClassVar

import pandas as pd
import pytest

from hourglass import digest, disclosure, phi


def at_risk_frame(n: int = 3) -> pd.DataFrame:
    return pd.DataFrame([{
        "auth_id": f"AUTH-{i:05d}",
        "client_id": phi.pseudonymise(f"CLI-{i:05d}", "CLI"),
        "service_code": "97153",
        "service_name": "Adaptive behavior treatment by protocol",
        "discipline": "ABA",
        "payer_name": "Meridian Health Plan",
        "contract_type": "value_based" if i % 2 else "fee_for_service",
        "units_authorized": 400.0, "units_delivered": 250.0, "units_unused": 150.0,
        "hours_authorized": 100.0, "hours_delivered": 62.5, "hours_unused": 37.5,
        "utilization": 0.625, "pace": 0.7,
        "days_to_expiry": i, "period_end": pd.Timestamp("2026-09-01"),
        "center_name": "San Diego" if i % 2 else "Temecula",
    } for i in range(n)])


def centres_frame(spec: dict[str, int]) -> pd.DataFrame:
    """One row per child, distributed across named centres."""
    rows, i = [], 0
    for centre, n_kids in spec.items():
        for k in range(n_kids):
            i += 1
            rows.append({
                "auth_id": f"AUTH-{i:05d}",
                "client_id": phi.pseudonymise(f"CLI-{i:05d}", "CLI"),
                "service_code": "97153", "service_name": "ABA",
                "discipline": "ABA", "payer_name": "Meridian Health Plan",
                "contract_type": "fee_for_service",
                "units_authorized": 400.0, "units_delivered": 250.0,
                "units_unused": 150.0, "hours_authorized": 100.0,
                "hours_delivered": 62.5, "hours_unused": 37.5,
                "utilization": 0.625, "pace": 0.7, "days_to_expiry": k + 1,
                "period_end": pd.Timestamp("2026-09-01"), "center_name": centre,
            })
    return pd.DataFrame(rows)


def headline(**over) -> dict:
    base = {"pace": 0.751, "at_risk_count": 3, "at_risk_children": 3,
            "at_risk_hours": 112.5}
    base.update(over)
    return base


def quality(coverage: float = 0.944, excluded: int = 2933) -> dict:
    return {"results": [
        {"name": "uom_resolution_coverage", "observed": coverage,
         "affected_rows": excluded},
    ]}


META = {"as_of": "2026-08-14", "run_id": "abc12345",
        "generated_at_utc": "2026-08-20T01:00:00+00:00"}


def centred_frame(sizes: dict[str, int], hours: float = 10.0) -> pd.DataFrame:
    """One child per row, spread across centres by head count.

    Hours are flattened to a round number per row so that the hours printed in
    each section can be added up and compared exactly.
    """
    frame = at_risk_frame(sum(sizes.values()))
    frame["center_name"] = [centre for centre, n in sizes.items() for _ in range(n)]
    frame["hours_unused"] = hours
    return frame


HEADING = re.compile(r"^### (.+)$")
CLIENT_REF = re.compile(r"`(CLI-[A-Z0-9]+)`")


def sections(markdown: str) -> dict[str, str]:
    """The rendered digest split into its `###` sections, as a reader reads it."""
    found: dict[str, list[str]] = {}
    body: list[str] | None = None
    for line in markdown.split("\n"):
        heading = HEADING.match(line)
        if heading:
            body = found.setdefault(heading.group(1).strip(), [])
        elif body is not None:
            body.append(line)
    return {name: "\n".join(lines) for name, lines in found.items()}


def leaking_sections(markdown: str) -> dict[str, int]:
    """Named sections whose rows give away a head count of ten or fewer.

    The attack the digest has to survive. A section may print "fewer than 11
    children" as loudly as it likes; if the rows underneath it are one per
    child, the reader counts them and has the number back. Only the pooled
    section is exempt, because it is attributable to no named centre.
    """
    leaks = {}
    for name, text in sections(markdown).items():
        if name == digest.POOLED_SECTION:
            continue
        children = len(set(CLIENT_REF.findall(text)))
        if 0 < children <= disclosure.SUPPRESSION_THRESHOLD:
            leaks[name] = children
    return leaks


class TestContent:
    def test_leads_with_children_not_percentages(self):
        """A coordinator acts on people, not on a rate."""
        out = digest.build_digest(at_risk_frame(), headline(), quality(), META)
        first_bold = out.split("**")[1]
        assert "child" in first_bold or "children" in first_bold

    def test_states_the_data_is_synthetic(self):
        out = digest.build_digest(at_risk_frame(), headline(), quality(), META)
        assert "ynthetic" in out.split("\n")[2]

    def test_groups_by_centre(self):
        """Centres large enough to name get a section each.

        Twelve children apiece, because a centre with three is pooled rather
        than named -- see TestDisclosureControl.
        """
        out = digest.build_digest(
            centred_frame({"San Diego": 12, "Temecula": 12}),
            headline(at_risk_count=24, at_risk_children=24), quality(), META)
        assert "### San Diego" in out
        assert "### Temecula" in out

    def test_marks_value_based_contracts(self):
        frame = at_risk_frame()
        out = digest.build_digest(frame, headline(), quality(), META)
        # Counted, not merely present: marking every row regardless of
        # contract type also put "⬥" in the output and passed.
        value_based = int((frame["contract_type"] == "value_based").sum())
        marked_rows = sum("⬥" in line for line in out.splitlines()
                          if line.strip().startswith("|"))
        assert value_based > 0
        assert marked_rows == value_based

    def test_includes_an_action_list(self):
        out = digest.build_digest(at_risk_frame(), headline(), quality(), META)
        assert "What to do with this" in out

    def test_handles_an_empty_at_risk_list(self):
        empty = at_risk_frame(0)
        out = digest.build_digest(
            empty, headline(at_risk_count=0, at_risk_children=0, at_risk_hours=0.0),
            quality(), META)
        assert "Nothing is expiring unused" in out
        assert "Who to contact" not in out

    def test_a_small_headline_count_is_suppressed(self):
        """A total of one, next to a table of centres, identifies a family.

        It is tempting to exempt the organisation-wide count as "just a total".
        It is the most disclosive number on the page when it is small.
        """
        out = digest.build_digest(
            at_risk_frame(1), headline(at_risk_count=1, at_risk_children=1),
            quality(), META)
        assert "1 child has" not in out
        assert "Fewer than 11 children" in out
        assert "withheld" in out

    def test_grammar_is_correct_for_a_publishable_count(self):
        out = digest.build_digest(
            at_risk_frame(40), headline(at_risk_count=40, at_risk_children=40),
            quality(), META)
        assert "40 children have" in out

    def test_hours_survive_suppression(self):
        """Suppress counts of people, not the workload figures.

        Hours are not a count of people, and withholding them would make the
        report useless without making it safer.
        """
        out = digest.build_digest(
            at_risk_frame(1), headline(at_risk_count=1, at_risk_children=1,
                                       at_risk_hours=112.5),
            quality(), META)
        assert "112" in out or "113" in out

    def test_handles_a_missing_centre(self):
        """Rows with no centre are their own group, named once it is large
        enough to name."""
        frame = at_risk_frame(12)
        frame["center_name"] = None
        out = digest.build_digest(
            frame, headline(at_risk_count=12, at_risk_children=12), quality(), META)
        assert "centre not recorded" in out


class TestCaveats:
    def test_quality_caveat_appears_when_coverage_is_low(self):
        # The test previously asserted "2,933 sessions" -- the check's
        # affected_rows, which sweeps in cancellations, no-shows and unmapped
        # codes and was being attributed wholesale to the April field change.
        # The caveat now quotes the completed-session count from the
        # assumption-spread comparison, which is the number that actually
        # understates delivered care.
        out = digest.build_digest(at_risk_frame(), headline(), quality(0.944),
                                  META, comparison={"affected_sessions": 2607})
        assert "understated" in out
        assert "2,607 completed sessions" in out
        assert "2,933" not in out

    def test_quality_caveat_falls_back_to_affected_rows(self):
        # Without the comparison dict the caveat still appears, quoting the
        # only count it has.
        out = digest.build_digest(at_risk_frame(), headline(), quality(0.944), META)
        assert "understated" in out
        assert "2,933" in out

    def test_caveat_precedes_the_action_list(self):
        """Nobody reads footnotes before picking up the phone."""
        out = digest.build_digest(at_risk_frame(), headline(), quality(0.944), META)
        assert out.index("understated") < out.index("What to do with this")

    def test_no_caveat_when_coverage_is_complete(self):
        out = digest.build_digest(at_risk_frame(), headline(), quality(1.0, 0), META)
        assert "understated" not in out

    def test_tells_the_reader_to_verify_before_contacting_a_family(self):
        out = digest.build_digest(at_risk_frame(), headline(), quality(0.944), META)
        assert "before contacting" in out.lower() or "before you call" in out.lower()


class TestPrivacy:
    def test_carries_only_pseudonymised_identifiers(self):
        out = digest.build_digest(at_risk_frame(3), headline(), quality(), META)
        assert "CLI-00000" not in out
        assert "CLI-" in out                                # the surrogate form

    def test_raw_identifiers_would_be_visible_if_they_leaked(self):
        """Guards the test above from passing vacuously."""
        leaky = at_risk_frame(1)
        leaky["client_id"] = ["CLI-00001"]
        out = digest.build_digest(leaky, headline(), quality(), META)
        assert "CLI-00001" in out          # so the check is capable of failing

    def test_no_provider_names_appear(self):
        frame = at_risk_frame()
        # Give the digest a provider identifier it must ignore. Without one
        # in the frame the assertion below was true of any implementation,
        # including one that printed the provider of every row.
        frame["provider_id"] = "PRV-0042"
        frame["provider_name"] = "Dr Example"
        out = digest.build_digest(frame, headline(), quality(), META)
        assert "PRV-" not in out
        assert "Dr Example" not in out


class TestPlainLanguage:
    @pytest.mark.parametrize("jargon", [
        "utilisation", "utilization", "grain", "fact table", "SCD",
        "dimension", "star schema", "pace", "watermark",
    ])
    def test_avoids_technical_vocabulary(self, jargon):
        """The audience is a coordinator, not an analyst.

        Every one of these words has a precise meaning inside the warehouse and
        no meaning at all to the person who has to phone a family this
        afternoon.
        """
        out = digest.build_digest(at_risk_frame(), headline(), quality(), META).lower()
        assert jargon.lower() not in out

    def test_explains_what_expiry_costs(self):
        out = digest.build_digest(at_risk_frame(), headline(), quality(), META)
        assert "do not roll forward" in out


class TestFileOutput:
    def test_writes_markdown(self, tmp_path):
        path = digest.write_digest(
            digest.build_digest(at_risk_frame(), headline(), quality(), META), tmp_path)
        assert path.exists()
        assert path.suffix == ".md"
        assert path.read_text(encoding="utf-8").startswith("# ")


class TestRecoveryBySubtraction:
    """The pooled count must not be derivable from the published total.

    Pooling removes a small centre's name. It does not, on its own, remove the
    number: every named centre still publishes its count, so subtracting them
    from a published organisation-wide total leaves exactly the pooled figure.
    Forty-five children, one named centre of forty, and the pooled section is
    five.

    The first version of this digest named that attack in its own closing
    footnote — "a combined section holding a single centre could be recovered
    by subtracting the named centres from the total" — and then committed it
    two paragraphs above, in the headline. Complementary suppression is not
    only a rule about cells; it applies to whatever granularity publishes both
    a total and its parts.
    """

    SPEC: ClassVar[dict[str, int]] = {"Big A": 40, "Small One": 2,
                                     "Small Two": 3}

    def _digest(self, spec: dict[str, int]) -> str:
        frame = centres_frame(spec)
        return digest.build_digest(
            frame,
            headline(at_risk_count=len(frame),
                     at_risk_children=int(frame["client_id"].nunique()),
                     at_risk_hours=float(frame["hours_unused"].sum())),
            quality(), META)

    def test_the_total_is_withheld_when_it_would_reveal_the_pooled_count(self):
        out = self._digest(self.SPEC)
        assert "45 children" not in out
        assert "**45" not in out
        assert "withheld" in out

    def test_the_reason_given_is_the_real_one(self):
        """A reader told only "this is small" will not understand why a total
        of forty-five is being withheld, and will assume a bug."""
        out = self._digest(self.SPEC)
        assert "subtraction" in out

    def test_named_centres_still_publish_their_counts(self):
        """Suppressing the total is the least that closes the hole. Taking the
        named counts as well would cost the report its usefulness."""
        out = self._digest(self.SPEC)
        assert "40 children" in out

    def test_the_pooled_section_is_still_withheld(self):
        out = self._digest(self.SPEC)
        pooled = sections(out).get(digest.POOLED_SECTION, "")
        assert "exact number withheld" in pooled

    def test_the_pooled_rows_survive_both_suppressions(self):
        """Nothing here may be bought by dropping work from the list.

        Asserted over the pooled centres rather than the whole frame, because
        a section longer than the display cap is truncated with a "N more"
        line — a rendering limit that predates suppression and applies to
        named sections equally. What must not happen is a row disappearing
        *because* its centre was pooled.
        """
        pooled_only = {k: v for k, v in self.SPEC.items() if k.startswith("Small")}
        frame = centres_frame(self.SPEC)
        expected = frame.loc[
            frame["center_name"].isin(pooled_only), "client_id"]
        out = self._digest(self.SPEC)
        pooled_section = sections(out).get(digest.POOLED_SECTION, "")
        # Five rows, well inside the cap, so every one must be present.
        assert len(expected) == 5
        for client_id in expected:
            assert client_id in pooled_section

    def test_truncation_is_disclosed_rather_than_silent(self):
        """A section cut to the display cap says how many rows it dropped.

        Otherwise the cap is indistinguishable from suppression, and a reader
        cannot tell whether the missing work is hidden or merely off-screen.
        """
        out = self._digest(self.SPEC)
        assert re.search(r"\d+ more", out)

    def test_a_publishable_total_is_still_published(self):
        """Guards the tests above from passing by suppressing everything.

        With no centre below the threshold there is nothing to pool and
        nothing to recover, so the total must appear as a number.
        """
        out = self._digest({"Big A": 40, "Big B": 30})
        assert "70 children" in out
        assert "subtraction" not in out

    def test_the_subtraction_is_arithmetically_impossible_not_merely_unstated(self):
        """Parse the published numbers and try the attack directly.

        Asserting the string "45" is absent is weaker than it looks — the
        number could return in a different form. This does the sum a reader
        would do: take every count the document publishes, and check that no
        combination of them yields the pooled figure.
        """
        out = self._digest(self.SPEC)
        published = [int(n) for n in re.findall(r"\b(\d+) (?:child|children)\b", out)]
        # 40 is publishable and appears; 45 and 5 must not, in any position.
        assert 45 not in published
        assert 5 not in published
        assert 40 in published


class TestDisclosureControl:
    """Small head counts are not published, and the rows do not publish them.

    This class replaces one built around `test_authorisation_rows_are_never
    _suppressed`, whose docstring argued that "suppression protects the head
    count, not the work". Half of that is right and worth keeping: the rows
    must survive, because a digest nobody can act on does not stop an
    authorisation expiring, and a control that works by emptying the document
    is not a control anybody should accept.

    The other half was wrong, and it made the suppression cosmetic. A table of
    one row per authorisation *underneath a centre heading* is the head count.
    Three rows under "### San Diego", each with a distinct client reference,
    publish the three that the line above them withheld -- to a reader who can
    count, which is every reader. The rows survive here by moving into a pooled
    section rather than by staying under a heading that discloses.
    """

    def test_a_small_centre_is_neither_named_nor_counted_by_its_rows(self):
        """The regression test for the defect, written as the attack.

        Not "is the count suppressed" -- the broken version suppressed the
        count too. Parse the rendered digest, take each named section, count
        the distinct client references under it, and check that no named
        section is sitting on a count of ten or fewer.
        """
        frame = centred_frame({"San Diego": 3, "Temecula": 14, "Murrieta": 15})
        out = digest.build_digest(
            frame, headline(at_risk_count=32, at_risk_children=32), quality(), META)
        assert "San Diego" not in out
        assert leaking_sections(out) == {}

    def test_the_attack_finds_a_leak_when_there_is_one(self):
        """Guards the test above from passing vacuously.

        The literal below is the shape the digest had before this fix: a named
        heading, a withheld count, and three rows that give it straight back.
        """
        leak = "\n".join([
            "### San Diego",
            "",
            "fewer than 11 children (exact number withheld) · 33 hours at risk",
            "",
            "| Client | Service | Hours left |",
            "|---|---|---:|",
            "| `CLI-AAAAAAAAAAAA` | ABA | **10.0** |",
            "| `CLI-BBBBBBBBBBBB` | Speech | **11.0** |",
            "| `CLI-CCCCCCCCCCCC` | OT | **12.0** |",
        ])
        assert leaking_sections(leak) == {"San Diego": 3}

    def test_every_authorisation_survives_pooling(self):
        """Safe by being empty is not safe, it is useless.

        Pooling moves rows; it must not drop them, and the pooled section's own
        row limit has to hold the centres it absorbed.
        """
        frame = centred_frame({"San Diego": 3, "Temecula": 4, "Murrieta": 15})
        out = digest.build_digest(
            frame, headline(at_risk_count=22, at_risk_children=22), quality(), META)
        pooled = sections(out)[digest.POOLED_SECTION]
        for centre in ("San Diego", "Temecula"):
            for client_id in frame.loc[frame["center_name"] == centre, "client_id"]:
                assert client_id in pooled

    def test_a_single_small_centre_does_not_stand_alone_when_pooled(self):
        """Pooling one centre pools nothing.

        If the combined section holds exactly one centre, its name is whichever
        centre is missing from the headings above it, and the count is the rows
        underneath. `disclosure.suppress_counts` sacrifices the next-smallest
        centre for the same reason it sacrifices a second cell.
        """
        frame = centred_frame({"San Diego": 3, "Temecula": 14, "Murrieta": 15})
        out = digest.build_digest(
            frame, headline(at_risk_count=32, at_risk_children=32), quality(), META)
        pooled = set(CLIENT_REF.findall(sections(out)[digest.POOLED_SECTION]))
        for centre in ("San Diego", "Temecula"):
            clients = set(frame.loc[frame["center_name"] == centre, "client_id"])
            assert clients <= pooled

    def test_centres_above_the_threshold_are_named_and_publish_their_counts(self):
        frame = centred_frame({"San Diego": 14, "Temecula": 15})
        out = digest.build_digest(
            frame, headline(at_risk_count=29, at_risk_children=29), quality(), META)
        assert "### San Diego" in out
        assert "### Temecula" in out
        assert "14 children" in out
        assert "15 children" in out
        assert digest.POOLED_SECTION not in out

    def test_pooling_does_not_change_the_hours_at_risk(self):
        """The privacy control may move the work about. It may not lose any."""
        frame = centred_frame({"San Diego": 3, "Temecula": 14, "Murrieta": 15})
        out = digest.build_digest(
            frame, headline(at_risk_count=32, at_risk_children=32), quality(), META)
        stated = [int(value.replace(",", ""))
                  for value in re.findall(r"· ([\d,]+) hours at risk", out)]
        assert sum(stated) == frame["hours_unused"].sum()

    def test_the_pooling_is_explained_where_it_happens(self):
        """In the section itself, not in a footnote nobody reaches."""
        frame = centred_frame({"San Diego": 3, "Temecula": 14, "Murrieta": 15})
        out = digest.build_digest(
            frame, headline(at_risk_count=32, at_risk_children=32), quality(), META)
        pooled = sections(out)[digest.POOLED_SECTION]
        assert "identify the families" in pooled
        assert "still needs the same call" in pooled

    def test_the_second_pooled_centre_is_explained(self):
        """A centre of fourteen disappearing from the headings needs a reason."""
        frame = centred_frame({"San Diego": 3, "Temecula": 14, "Murrieta": 15})
        out = digest.build_digest(
            frame, headline(at_risk_count=32, at_risk_children=32), quality(), META)
        assert "recovered by subtracting" in out

    def test_a_pooled_section_below_the_threshold_withholds_its_own_count(self):
        """Pooling small centres can still leave a small count.

        The rows stay -- the reader can count them, and that is a stated limit
        of pooling -- but the number is not published, and it is attached to no
        centre.
        """
        frame = centred_frame({"San Diego": 3, "Temecula": 4})
        out = digest.build_digest(
            frame, headline(at_risk_count=7, at_risk_children=7), quality(), META)
        assert "San Diego" not in out
        assert "Temecula" not in out
        assert "fewer than 11 children (exact number withheld)" in (
            sections(out)[digest.POOLED_SECTION])
        assert len(set(CLIENT_REF.findall(out))) == 7

    def test_large_counts_are_published_normally(self):
        frame = centred_frame({"San Diego": 40})
        out = digest.build_digest(
            frame, headline(at_risk_count=40, at_risk_children=40), quality(), META)
        assert "40 children" in out
        assert "withheld" not in out
        assert digest.POOLED_SECTION not in out
