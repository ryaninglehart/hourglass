#!/usr/bin/env python3
"""Break the pipeline on purpose, and see whether it notices.

Every project claims its safety checks work. This runs the experiment.

Each sabotage below is a real, single-line change to the source -- the kind of
thing a tired person makes on a Friday, or that an AI assistant produces
confidently while every test stays green. The script applies one, runs the
whole pipeline, records what happened, puts the source back, and moves to the
next. Nothing in the working tree is modified: it all happens in a throwaway
copy under /tmp.

**The point is not that everything is caught.** Some of these are caught
loudly and some are only reported quietly, and the difference matters: a
release that is stopped and a release that ships with a note attached are
different promises.

The expected outcome is declared per sabotage and asserted, in both
directions. A safety net that stops working is a failure; so is a documented
blind spot that has quietly started being caught, because then the
documentation is lying in the flattering direction. Case 7 was exactly that --
it was an honest ``expect="nothing"`` until the rule-set hash began being
compared run over run, and leaving the old claim in place would have understated
the project while making its own report untrue.

What this cannot do is find a blind spot nobody has thought of. Every case here
had to be imagined and written down first, so a clean sweep says these seven are
covered and says nothing at all about the eighth.

    python scripts/prove.py            # run them all
    python scripts/prove.py --list     # show them without running
    python scripts/prove.py --only 3   # run one

Read the output rather than the exit code. The exit code only tells you whether
each safety net behaved as documented; the table tells you what each one is
actually for.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

ACK = ("uom_resolution_coverage=Ticket DE-412. Vendor confirmed the "
       "2026-04-01 unit-of-measure change; back-fill scheduled.")


@dataclass
class Sabotage:
    name: str
    file: str
    find: str
    replace: str
    simulates: str
    """What real-world mistake this stands in for."""
    expect: str
    """'blocks' -- the pipeline refuses to publish.
       'reports' -- it publishes, but an artifact says the numbers moved.
       'nothing' -- an honest blind spot."""
    caught_by: str


SABOTAGES: list[Sabotage] = [
    Sabotage(
        name="Hours computed with a flat quarter-hour per unit",
        file="src/hourglass/export.py",
        find='"hours_unused": round(float(active["hours_unused"].sum()), 1),',
        replace='"hours_unused": round(float(active["units_unused"].sum()) * 0.25, 1),',
        simulates="The real INC-005 defect, restored. A unit is 15 minutes for "
                  "ABA but 45 for speech, so a flat divisor understates the "
                  "headline by about 958 hours and understates it most on the "
                  "cases the at-risk list exists to surface.",
        expect="blocks",
        caught_by="the published-headline check in `verify`, which re-derives "
                  "every dashboard figure from the warehouse in SQL",
    ),
    Sabotage(
        name="A filter that silently stops filtering",
        file="src/hourglass/analytics.py",
        find="    return frame[column].astype(bool)",
        replace="    return frame[column].astype(int)",
        simulates="The real INC-004 defect. Cancelled and unmeasurable sessions "
                  "start counting as delivered care. No error is raised -- the "
                  "number simply becomes wrong.",
        expect="blocks",
        caught_by="the metric parity check, which computes every figure twice "
                  "-- once in SQL, once in pandas -- and refuses to publish "
                  "when they disagree",
    ),
    Sabotage(
        name="De-identification switched off",
        file="src/hourglass/phi.py",
        find="def redact_records(records: list[dict]) -> list[dict]:",
        replace=("def redact_records(records: list[dict]) -> list[dict]:\n"
                 "    return records  # sabotage: redaction disabled"),
        simulates="Raw client identifiers reach the quality report and the "
                  "dashboard -- the real INC-001 leak.",
        expect="blocks",
        caught_by="the egress scan, which re-reads every published file as "
                  "bytes and looks for identifier patterns. It does not trust "
                  "the redaction step; it checks the artifact",
    ),
    Sabotage(
        name="A metric's SQL quietly changed",
        file="src/hourglass/metrics.py",
        find='sql="SELECT SUM(units_authorized) AS value FROM fact_authorization",',
        replace=('sql="SELECT SUM(units_authorized) * 1.01 AS value '
                 'FROM fact_authorization",'),
        simulates="Somebody edits a query and not the Python beside it. A one "
                  "per cent drift -- small enough that nobody eyeballing a "
                  "dashboard would see it.",
        expect="blocks",
        caught_by="the metric parity check. Its tolerance is 1e-6, so a "
                  "disagreement of one per cent is not close",
    ),
    Sabotage(
        name="A service's minutes-per-unit edited",
        file="src/hourglass/config.py",
        find='"discipline": "Speech", "unit_basis": "per_session", "minutes_per_unit": 45',
        replace='"discipline": "Speech", "unit_basis": "per_session", "minutes_per_unit": 15',
        simulates="A conversion factor is corrected, or mistyped. Every speech "
                  "figure in the warehouse changes by a factor of three. "
                  "Nothing is *invalid* -- everything is different.",
        expect="reports",
        caught_by="the run-over-run diff, which compares this build against "
                  "the last published one and names the columns that moved. "
                  "No gate fires, because no rule was broken -- which is "
                  "exactly why the diff exists",
    ),
    Sabotage(
        name="Deduplication removed",
        file="src/hourglass/transform.py",
        find="def dedupe_sessions(sessions: pd.DataFrame)",
        replace=("def dedupe_sessions(sessions: pd.DataFrame):  # noqa: ANN201\n"
                 "    return sessions, sessions.head(0)\n\n\n"
                 "def _unused_dedupe_sessions(sessions: pd.DataFrame)"),
        simulates="Duplicate session records from two overlapping source "
                  "extracts stop being collapsed. Delivered care is "
                  "double-counted.",
        expect="reports",
        caught_by="the run-over-run diff, exactly: 222 duplicates are seeded "
                  "and 222 rows differ. The `duplicate_session_submissions` "
                  "check reports the same number as a WARN. It is a warning "
                  "rather than a block because duplicate submissions are a "
                  "normal fact of two overlapping extracts -- what matters is "
                  "that they were removed, and the report says how many",
    ),
    Sabotage(
        name="The at-risk window changed from 30 days to 90",
        file="src/hourglass/config.py",
        find="EXPIRY_WARNING_DAYS = 30",
        replace="EXPIRY_WARNING_DAYS = 90",
        simulates="Somebody widens the definition of 'expiring soon'. The "
                  "weekly list triples in length and every headline changes.",
        expect="reports",
        caught_by="the rule-set hash, compared run over run and reported at "
                  "the top of the diff. No check objects and none should -- "
                  "this is a policy constant, not a data error, and the "
                  "pipeline has no way to know which value was intended. What "
                  "it can do is tell a reader that the definitions moved, so "
                  "they do not compare this week's list with last week's as "
                  "though the two meant the same thing. This was a genuine "
                  "blind spot until 20 Aug: the hash was already stamped on "
                  "every run and nothing compared it against the previous one",
    ),
]


# ---------------------------------------------------------------------------


def _clip(text: str, limit: int = 200) -> str:
    """Trim on a word boundary. A figure cut mid-digits reads as a figure."""
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + " ..."


def run_pipeline(work: Path) -> tuple[int, str]:
    """One full pipeline run in the sandbox copy. Returns (exit code, output)."""
    proc = subprocess.run(
        [sys.executable, "-m", "hourglass.pipeline", "--no-s3", "--acknowledge", ACK],
        cwd=work, capture_output=True, text=True, timeout=600,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin",
             "PYTHONPATH": str(work / "src"),
             "HOME": str(work)},
    )
    return proc.returncode, proc.stdout + proc.stderr


def apply(work: Path, s: Sabotage) -> bool:
    path = work / s.file
    text = path.read_text(encoding="utf-8")
    if s.find not in text:
        return False
    path.write_text(text.replace(s.find, s.replace, 1), encoding="utf-8")
    return True


def revert(work: Path, s: Sabotage) -> None:
    shutil.copy2(ROOT / s.file, work / s.file)


def evidence(output: str, work: Path) -> str:
    """The single most informative line from the run."""
    for marker in ("PUBLISHED FIGURE UNVERIFIABLE", "metric parity failed",
                   "PHI EGRESS", "PUBLICATION HALTED", "FAILED"):
        for line in output.splitlines():
            if marker in line:
                return _clip(line.strip())
    diff_report = work / "data" / "out" / "reports" / "data_diff.md"
    if diff_report.exists():
        text = diff_report.read_text(encoding="utf-8")
        for line in text.splitlines():
            if "rows differ" in line:
                return _clip(line.strip().replace("**", ""))
        # A definition change can move every published figure while leaving
        # every warehouse row identical, so "rows differ" never appears. The
        # diff says so in a banner instead, and a harness that only counted
        # rows reported this as caught by nothing -- which stopped being true
        # the moment the banner was added. A blind-spot report that has itself
        # gone stale is worse than no report.
        for line in text.splitlines():
            if "definitions changed" in line.lower():
                return _clip(line.strip().replace("**", "").lstrip("> "))
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list", action="store_true", help="show them, run nothing")
    ap.add_argument("--only", type=int, help="run one, by number")
    args = ap.parse_args()

    if args.list:
        for i, s in enumerate(SABOTAGES, 1):
            print(f"{i}. {s.name}\n   expect: {s.expect}\n")
        return 0

    chosen = ([SABOTAGES[args.only - 1]] if args.only else SABOTAGES)

    work = Path(tempfile.mkdtemp(prefix="hourglass-prove-"))
    print(f"  sandbox: {work}\n  copying the project ...", flush=True)
    shutil.copytree(ROOT, work / "hourglass", dirs_exist_ok=False,
                    ignore=shutil.ignore_patterns(
                        ".git", "__pycache__", ".pytest_cache", ".ruff_cache",
                        ".hypothesis", "mutants", "data"))
    work = work / "hourglass"

    print("  establishing a clean baseline (two runs, so the diff has "
          "something to compare against) ...", flush=True)
    for _ in range(2):
        code, out = run_pipeline(work)
        if code != 0:
            print("  BASELINE FAILED -- the unmodified pipeline did not "
                  "publish. Nothing below is meaningful.\n")
            print(out[-3000:])
            return 2
    print("  baseline publishes cleanly.\n")

    results = []
    for i, s in enumerate(chosen, 1):
        print(f"  [{i}/{len(chosen)}] {s.name} ...", end=" ", flush=True)
        if not apply(work, s):
            print("SKIPPED (source has moved on; update scripts/prove.py)")
            results.append((s, "skipped", ""))
            continue
        code, out = run_pipeline(work)
        note = evidence(out, work)
        revert(work, s)
        run_pipeline(work)          # restore a clean published state

        if code != 0:
            actual = "blocks"
        elif note:
            actual = "reports"
        else:
            actual = "nothing"

        ok = actual == s.expect
        print(f"{actual.upper()}{'' if ok else '  <-- NOT AS DOCUMENTED'}")
        results.append((s, actual, note))

    print("\n" + "=" * 78)
    print("  WHAT HAPPENED")
    print("=" * 78 + "\n")

    for i, (s, actual, note) in enumerate(results, 1):
        verdict = {
            "blocks": "STOPPED THE RELEASE",
            "reports": "published, but reported the change",
            "nothing": "published silently",
            "skipped": "not run",
        }[actual]
        print(f"{i}. {s.name}\n")
        print(f"   Simulates      {s.simulates}\n")
        print(f"   Result         {verdict}")
        if note:
            print(f"   Evidence       {note}")
        print(f"   Caught by      {s.caught_by}")
        if actual != s.expect and actual != "skipped":
            print(f"   *** documented as '{s.expect}', actually '{actual}'. "
                  f"The documentation is now wrong. ***")
        print()

    mismatches = [r for r in results if r[1] not in (r[0].expect, "skipped")]
    blind = [r for r in results if r[1] == "nothing"]

    plural = "" if len(results) == 1 else "s"
    print("=" * 78)
    print(f"  {len(results)} sabotage{plural}. "
          f"{sum(1 for r in results if r[1] == 'blocks')} stopped the release, "
          f"{sum(1 for r in results if r[1] == 'reports')} were reported, "
          f"{len(blind)} passed silently.")
    if blind:
        print("  The silent ones are the honest part -- see what they simulate.")
    elif len(results) == len(SABOTAGES):
        # Nothing here is silent any more. That is a good result and a
        # misleading one if left to speak for itself: it says these seven are
        # covered, not that the pipeline has no blind spots. The ones nobody
        # has thought to write down are the ones still open, and a harness
        # cannot report those by construction.
        print("  Nothing passed silently -- of these seven. That is a")
        print("  statement about the seven, not about the pipeline: the blind")
        print("  spots still open are the ones nobody has thought to sabotage.")
    if mismatches:
        print(f"  {len(mismatches)} did not behave as documented. Fix the code "
              f"or fix the docs.")
    print("=" * 78)

    shutil.rmtree(work.parent, ignore_errors=True)
    return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
