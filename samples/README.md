# Sample output

`data/` is not tracked, so a clone of this repository contains the code but none
of the artifacts it produces. These files are one real run, committed so the
output can be read without installing anything.

They are a snapshot, not a live report. Regenerate them with `make all`; the
numbers below will match unless the code or the rule set changed.

| File | What it is |
|---|---|
| `quality_report.md` | All 17 checks, the severity of each, and what the gate decided |
| `metric_parity.md` | Every headline number computed twice — SQL and pandas — and compared |
| `data_diff.md` | What changed against the previous published build, data and definitions |
| `weekly_digest.md` | The plain-language list a care coordinator receives |

Two things in `quality_report.md` are the point of the project:

`uom_resolution_coverage` fails at **BLOCK**. Partway through the year the source
extract stops recording whether a duration is minutes or units, and a unit is 15
minutes for ABA, 45 for speech, 30 for a medical visit. Guessing is wrong by up
to 3× and produces a number that looks reasonable. Those rows are excluded, the
coverage rate is published beside every figure they touch, and the run will not
publish until someone acknowledges it in writing. `docs/ANOMALY.md` has the
full account.

Seven checks fail in this run and the build published anyway. That is the design:
six of them are WARN, which means reported rather than fatal, and the BLOCK was
acknowledged with a reason recorded in the audit log. Run `make gate` to see the
same run refuse to publish when it is not.
