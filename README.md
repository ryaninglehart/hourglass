# Hourglass

**An authorisation utilisation pipeline for paediatric therapy — ingest, dimensional model, quality gates, a PHI boundary, and a report somebody can act on.**

Authorised therapy hours expire. When they expire unused, a child received less
care than their plan approved, and the provider was not paid for care it could
have delivered. Nobody inside the treatment room can see it happening, because
it only becomes visible when the authorisation system and the scheduling system
are read together.

This pipeline reads them together, and then tells a person what to do about it.

> ### All data here is synthetic
> Every row is produced by a seeded generator in this repository
> (`src/hourglass/generate.py`). No real patient information was used to build
> this and none is present in it. The *shape* of the data — CPT codes,
> 15-minute unit billing, authorisation periods, the mix of disciplines —
> follows publicly documented paediatric therapy billing practice, so the
> modelling problems are the real ones.

---

## Run it

```bash
git clone https://github.com/ryaninglehart/hourglass.git
cd hourglass

# Python 3.11+
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
make all
```

About ten seconds. No time to install anything? [`samples/`](samples/) holds
one real run's reports, committed — start with
[`samples/weekly_digest.md`](samples/weekly_digest.md).

Then three files are worth opening, in this order:

| File | What it is |
|---|---|
| `data/out/reports/weekly_digest.md` | What a clinical operations coordinator receives. **Start here** — it is the point of everything else. |
| `dashboard.html` | The analyst view. Offline, no licence. |
| `data/out/reports/quality_report.md` | What the gate decided, and why. |

Two more are written on every run and are worth a glance:
`data/out/reports/metric_parity.md` — every registered metric recomputed in SQL
and compared against the pandas that produced the dashboard — and
`data/out/reports/data_diff.md` (what moved since the last published build). On
a run that publishes, `verify` appends a second section to the parity report in
which the dashboard's published headline figures are re-derived from the
warehouse; a blocked run's report stops at the first section, because `verify`
does not run when nothing was published.

**Then run `make gate`.** It runs the pipeline *without* acknowledging the
blocking data-quality failure. It refuses to publish, quarantines the warehouse
so downstream readers keep seeing the last good one, and exits non-zero. That
refusal is the part of this project worth looking at first.

```
make gate         watch it refuse to publish              (exit 2)
make run          acknowledge in writing, publish         (exit 0)
make check        lint, 461 tests, gate, publish, analytics, data dictionary
make mutation     mutation-test disclosure.py — see docs/MUTATION.md
make prove        break it on purpose; see what catches it
make benchmark    time it at 1x, 4x and 12x scale
make digest       print the weekly digest
make analytics    run the ten queries in sql/analytics.sql
make localstack-up && make run-s3      against real S3 APIs in Docker
```

`make gate` exits 2 rather than 1 because that is make's code for a failed
recipe; the pipeline process underneath it exits 1. Both are the refusal, seen
from different heights.

---

## What it does

```
  extract -> land -> read_lake -> conform -> analyse -> protect -> quality -> load
  synthetic boto3    lake         pandas     pace &     PHI        17 gates   star
  extracts  S3       only         SCD2       at-risk    boundary   3 sevs     schema

  load -> diff   ---+
       -> parity ---+-> publish -> verify
                       BI · dashboard   re-read the published
                       · digest         bytes for identifiers
```

Twelve tasks with declared dependencies, run through a small orchestrator
(`hourglass.orchestration`) that handles retries, per-task timing, failure
isolation and a JSON-lines run log. Three synthetic extracts stand in for a CRM,
a payer authorisation feed, and an EHR session log.

`diff` and `parity` both hang off `load` and both gate `publish`: the first
reports what moved since the last published build, the second recomputes every
registered metric from the warehouse and refuses to publish if it disagrees with
the pandas that produced the dashboard. `verify` runs last and does two things
neither of the others can: it reads the published files as bytes looking for raw
identifiers, and it re-derives the dashboard's headline figures from the
warehouse and compares them against what was actually written to disk.

**The pipeline only ever reads from the lake.** The generator can be deleted and
replaced with a real extract without touching anything downstream.

---

## The decisions worth arguing about

**1. Two fact tables, at deliberately different grains.**
`fact_session` is one row per delivered session. `fact_authorization` is one row
per authorisation line — one client, one service, one period. They are never
joined directly; sessions are aggregated to the authorisation grain first.
Joined row-to-row, `units_authorized` repeats once per session. On this dataset
that inflates authorised units by **55x**, and nothing errors.
`sql/analytics.sql` query 3 runs both versions with an identical row filter, so
the ratio isolates the grain error, and prints both numbers.

**2. A missing unit of measure is a blocking failure, not a default.**
When the unit is missing the number is not recoverable — "4" is four minutes or
four 15-minute units. The pipeline refuses to guess: those rows are excluded and
the coverage is published beside the number. The dashboard shows what *each* of
the two plausible guesses would have produced, so the cost of guessing is a
figure rather than an argument.

**3. Pace, not raw utilisation, while an authorisation is open.**
An authorisation a third of the way through its window should have delivered
about a third of its units. Judging it against 100% makes healthy delivery look
like a crisis. Open authorisations are measured against elapsed time; closed
ones against their final total. The split lives in the model, not in whoever
writes the query.

**4. Client history is Type 2, so a payer change does not rewrite the past.**
`dim_client` keeps a row per version with validity ranges, and facts join to the
row in effect on the event date. The trap: the session-to-authorisation join
uses the *natural* `client_id`, not the surrogate — an authorisation spanning a
payer change would otherwise match only half its own sessions.

**5. Hours are never units divided by four.**
A unit is fifteen minutes for the four ABA codes and for occupational therapy,
forty-five for a speech session, thirty for a medical visit. `dim_service`
carries `minutes_per_unit` and every hours figure — Python, SQL and DAX alike —
converts through it. A flat divisor understates speech and medical by two to
three times, and since the at-risk list is ordered by hours, it buries exactly
the rows it exists to surface. The aggregate error is 1.7%, which is what makes
it dangerous: too small to notice on a total, concentrated entirely on the two
service lines the report exists to find.

This decision was violated in this repository, on the dashboard's headline
"unused hours" tile, for as long as that tile existed — `export.py` computed it
as `units_unused * 0.25`, understating it by 958 hours, while the parity report
reported agreement on the whole registry. INC-005 in
[`docs/INCIDENTS.md`](docs/INCIDENTS.md) is the write-up, and it is the most
useful entry in that file: the rule is written down here and in comments in
`analytics.py`, `sql/analytics.sql` and `bi/measures.dax`, and was broken anyway,
in the one place a reader actually looks.

**6. Nulls stay null.**
`NULL` is not zero, an unmapped service code gets an explicit `(unmapped)`
dimension member rather than being dropped, and CSV exports write empty fields so
Power BI reads BLANK. A null coerced to zero is the fastest way to make a rate
measure lie.

**7. Each metric is defined once, and the engines are made to agree.**
"Hours delivered" is computed in `analytics.py`, in `sql/analytics.sql` and in
`bi/measures.dax`. Three implementations of one sentence drift, and the failure
mode is not an error — it is two reports showing different numbers for the same
week. `hourglass.metrics` declares each metric once and the pipeline checks the
implementations against each other on every build. See below for exactly what
that check does and does not cover.

---

## The PHI boundary

Most projects say "HIPAA compliant" and mean somebody was careful. This one
draws a boundary and puts a gate on it.

The raw lake is inside: extracts arrive as the sources send them. Everything
published is outside, and nothing that identifies a person crosses. Enforcement
is two layers, deliberately different in kind:

- **Classification.** Every column of every published frame is declared in
  `hourglass.phi.FIELD_CLASSIFICATION` against HIPAA's Safe Harbor identifier
  list. A column nobody has classified is treated as an identifier — unknown
  fails closed, so a new upstream column blocks publication until someone looks
  at it.
- **Content scanning.** Declarations describe intent; values are what actually
  leave. The scanner reads outgoing data for identifier *shapes* — SSNs, MRNs,
  member and phone numbers, e-mail addresses, US-format dates of birth, ZIP+4 —
  whatever the column claims to be. It is regular expressions over sampled rows,
  so it cannot find a personal name, which has no shape, and it cannot find an
  ISO-format date of birth, which is indistinguishable from any other date.
  Those two are what the classification layer covers; this layer covers the case
  where the classification is wrong.

**The two layers are gated separately, because they make claims of different
strength.** `phi_egress` reports only what it can prove by reading values — a
column the contract already calls a direct identifier still holding raw ones, or
a column nobody has classified — so it has no false-positive mode and no written
reason can release it. `phi_content_scan` reports regex hits, which have false
positives by construction: a payer legitimately named "Member Health Network"
matches the member-number pattern. Gated identically, one such name would halt
publication permanently with no route back other than editing the pattern, so
the content scan is BLOCK *and* acknowledgeable. The false positive costs a
written reason and a name in the run log; the true positive still cannot be
released quietly.

Direct identifiers are pseudonymised, not dropped, using HMAC-SHA256 with a salt
read from `HOURGLASS_PSEUDONYM_SALT`. Pseudonymisation is not anonymisation. The
surrogate is exactly as strong as the salt is secret and no stronger, because
HMAC is one-way over an *unbounded* input space and a client identifier is not
one: the identifiers here run `CLI-00001` to `CLI-00240`, and an attacker who
knows only the zero-padded five-digit shape has 10^5 candidates to try. Either
number is seconds of hashing, so anyone holding the salt can enumerate the whole
space and invert every published surrogate by lookup.
A reviewer did precisely that against an earlier build whose salt was a constant
in `phi.py` — all 240 published surrogates recovered in about a second.

So there is no default salt any more. When the variable is unset, `phi._salt`
mints a random 32-byte salt for the life of the process and never writes it
anywhere: nothing can be precomputed against a value that does not exist until
the run starts and does not survive it. **What that costs is stated rather than
hidden:** surrogates are stable *within* a run and unlinkable *between* runs, so
two builds' exports cannot be joined and week-over-week tracking of a child
outside the boundary is impossible until an operator configures a salt. Inside
the boundary nothing changes — the warehouse stores raw identifiers, so the diff
and the fact tables are unaffected.

`quality.check_pseudonym_salt` reports which of the three states a build was in,
reading the *source* of the salt and never the salt itself. A configured secret
passes. The old checked-in constant is a BLOCK, because publishing under a salt
every reader of the repository holds is handing out re-identifiable data. An
ephemeral salt is a WARN, because what it costs is utility rather than anyone's
privacy — which is why a fresh clone still runs end to end, with a report that
says on its face that its exports cannot be compared to last week's. None of the
three can be acknowledged: a written reason cannot make a reversible pseudonym
irreversible.

**And the gate verifies rather than trusts.** `phi.is_pseudonymised` does not
check that de-identification was *configured* — it reads the published values and
confirms they match the surrogate format. A new export path that skips the
transform is caught by the check rather than waved through by it.

**A third layer reads the bytes.** Everything above inspects a DataFrame on its
way to a file, which leaves a gap wherever something reaches a file without
passing through a frame the gate was handed — a check's sample rows, an aggregate
assembled after the gate ran, a log line. The `verify` task gives up on knowing
what a file means and reads it as text instead: `phi.scan_published_artifacts`
re-opens an enumerated list of paths and searches for raw source identifiers. A
finding raises, cannot be acknowledged, and withdraws the offending files,
restoring the previous published build.

That gap was not hypothetical. The gate originally inspected only the eight
warehouse tables, so the dashboard published raw client identifiers through
quality-check samples while every frame-level check reported clean. A boundary
that covers most of the exits is not a boundary. Full write-up: INC-001 in
[`docs/INCIDENTS.md`](docs/INCIDENTS.md).

**The list of paths is no longer a list**, because a scan described more
broadly than it runs is the same defect it exists to catch — and that is not
hypothetical either. The list was written out by hand, and it drifted:
`metric_parity.md` and `data_diff.md` were published, committed to `samples/`,
and never scanned, while the docstring above the scan said "every published
file" (INC-010). Scanned now: everything in the export directory — the BI CSVs,
eight of them today, `dashboard_data.json`, `relationships.md` — everything in
the report directory, including the parity report after its published-headline
section is appended, and `dashboard.html`. **Not** scanned: the SQLite files,
which hold raw identifiers by design and sit inside the boundary rather than
crossing it. `verify` is also skipped entirely on a blocked run, which is
sound — nothing was published — but it means a quarantined build's exports are
never read.

**Two of those files are read one run late, and the run number gives it away.**
`dashboard.html` is rendered by `scripts/build_dashboard.py` after the pipeline
exits, and `pipeline_runs.jsonl` is flushed by the orchestrator after the task
loop finishes, so `verify` opens the *previous* run's copy of each or, on a
fresh clone, no file at all. The count printed at the end of the run says so
without being asked: a cold `make clean && make run` reports **15 artifacts**,
the next `make run` reports **16** now that a run log exists, and it reaches
**17** once `dashboard.html` has been built. The JSON payload the dashboard is
built from *is* scanned in the same run, so the data path is covered and the
rendered HTML is covered on the following build; the run log is covered a run
behind throughout. That is a gap in the ordering rather than a subtlety, and it
belongs beside the claim rather than in a limitations section, because the
paragraph above would otherwise read as a stronger promise than the code makes.

**Small cells.** Pseudonymising an identifier does nothing about a count. Three
children, one centre, one service, one week is not an aggregate — it is three
families, identifiable to anyone who works there. `hourglass.disclosure`
implements CMS's cell size suppression policy: cells of 1 to 10
are withheld, zero is publishable, and a suppressed count takes its derived
columns — percentage, share, hours — with it, because publishing "12.5% of 24"
recovers the count. It also does **complementary suppression**: one hidden cell
beside a published total is recoverable by subtraction, so a second cell goes as
well — one second cell, once, rather than a pass repeated to a fixed point. The
tables published here are one-dimensional: a list of cells and one published
total, with no second margin for a suppression to propagate along. A single
equation cannot be solved for two unknowns, so once a second cell is hidden the
table is safe and hiding a third protects nothing further. The grouped path does
not change that — each group is its own one-dimensional table with its own
subtotal and no column margin joins them. The deliberate limit: this is a greedy
pass over a single margin, not linear-programming cell suppression, which is the
rigorous answer for multi-dimensional tables with margins in several directions.
It is correct for the tables published here — one grouping dimension plus a
total — and not equivalent to the general solution.

**Where it is applied, precisely: the weekly digest and the dashboard's
org-wide child count, and nowhere else.** `digest.py` and one call in
`export.py` are the only callers in `src/`. The remaining panels, the BI CSVs
and the quality report publish their counts unsuppressed, which is defensible
for the figures they actually carry — whole-population counts, 240 children and 1,825
authorisations — and not because anything checks.

That was not true of one panel until recently, and the exception is the more
useful half of the story. The dashboard's at-risk list carried `center_name` on
every row beside a pseudonymised `client_id`, so counting distinct client
references under a named centre recovered that centre's head count. The payload
publishes the top twenty-five rows, and on the current data every one of the
five centres in that slice lands inside the 1-to-10 range — one, one, two, three
and six — published verbatim in `dashboard.html`, where Ctrl-F finds them.
`export.py` now drops `center_name` from the at-risk projection, and nothing was
reading it: it was published because it was in the frame, which is how most
disclosures happen. What that
buys is exact and worth stating exactly. A pseudonymised client reference is
still stable within a run, so a reader can still count the rows and the distinct
references in them; what they can no longer do is attach any of them to a
centre.

A per-centre breakdown added to the dashboard tomorrow would still not route
through `disclosure` unless whoever added it remembered, and nothing in the
build would say so. That is a control living at a call site rather than at a
chokepoint, which is the shape of defect INC-001 was about, and it is open.

**Suppressing the count is not enough on its own, and the digest is where that
shows.** The rows under a centre heading are one per authorisation, so printing
"fewer than 11 children" above them withholds a number the reader can recover by
counting distinct client references. So a centre below the threshold loses its
*heading*, not its rows: small centres are pooled into a combined "Other
centres" section, sorted by expiry rather than by centre, with no centre column —
a pooled table ordered by centre is a centre-labelled table with the labels
taken off. What is removed is the attribution of a small count to a named
centre, which is the disclosure; what survives is the list of calls to make,
which is the point. The single-small-centre case is handled by the same
complementary pass that handles cells: if exactly one centre is below the
threshold, the pooled section *is* that centre and its name follows by
elimination from the named sections above, so the next-smallest centre is pooled
with it. Pooling has a limit and the digest prints it: a combined count that is
itself below the threshold is withheld too, and a reader can still count the
pooled references — what they cannot do is attach the figure to a centre.

**And the digest's own headline total had to go with it.** Every named centre
publishes its count, so a reader who subtracts the named counts from a published
organisation-wide total is left with the pooled figure — the one number the
pooling exists to withhold. Forty-five children, one named centre of forty, and
the pooled section is five. The earlier version of the digest named that attack
in its own closing footnote and then committed it two paragraphs above, in the
headline. `digest.CentrePlan` now decides the section layout *before* the
headline paragraph is written, and `total_is_recoverable` withholds the total
whenever the pooled count is withheld — giving the real reason in the sentence,
because a reader told only "this number is small" about a total of forty-five
will assume a bug. The named counts stay: suppressing the total is the least
that closes the hole, and taking the named counts as well would cost the report
its usefulness. `tests/test_digest.py::TestRecoveryBySubtraction` holds all of
that in place. The generalisable point is that complementary suppression is not
only a rule about cells — it applies at whatever granularity publishes both a
total and its parts.

The data is synthetic, so nothing here protects anyone. It is written this way
because the habit is the deliverable: a pipeline that only protects real data
protects nothing on the day the data becomes real.

---

## Quality gates

Seventeen checks across three severities. Only `BLOCK` stops the pipeline.

| | Meaning | Stops publication |
|---|---|---|
| `BLOCK` | The number cannot be trusted. | Yes |
| `WARN` | The number is fine; something needs a human. | No |
| `INFO` | Recorded so drift is visible over time. | No |

The severity design is an opinion worth stating. A session with an unknown unit
of measure is BLOCK — utilisation computed over it is meaningless, not slightly
wrong. A session delivered without an authorisation is only WARN — the number is
correct; it is the business situation that is bad.

**Two of the checks exist to cover a blind spot in another mechanism**, which is
the more interesting reason to write one.

`overlapping_authorization_periods` is BLOCK. Every roll-up here attributes a
session by `client_id + service_key + date BETWEEN period_start AND period_end`.
Two authorisations for the same child and service whose windows intersect make
that predicate match both, so each session in the intersection is counted in
full twice. The parity check cannot see it: `analytics.build_utilization` and
`metrics.AUTH_GRAIN_CTE` are deliberate transcriptions of each other, which is
what lets them catch a coding error in either and exactly what stops them
catching an error in the specification they share. Both double-count an overlap
identically, and parity reports agreement over two wrong numbers. Two
implementations of one sentence cannot check the sentence. No SQL constraint
expresses "these date ranges must not intersect", and real payers amend and
reissue lines. The synthetic data currently contains no overlap, so this is
latent rather than live — and a check that passes today is the only thing
standing between latent and live.

`zero_unit_authorizations` is WARN, and it is the case
`utilization_over_ceiling` cannot see. Over-delivery is caught by
`utilization > 1`; over-delivery against an authorisation approving zero units
has no utilisation at all — the ratio is undefined, published as null, and a
null is not greater than one, so without a check of its own the row is absent
from every view of the problem. WARN rather than BLOCK because the totals stay
correct: what is broken is the authorisation record, not the number.

A blocking failure can be released, but not quietly:

```bash
PYTHONPATH=src python -m hourglass.pipeline --acknowledge \
  "uom_resolution_coverage=Ticket DE-412. Vendor confirmed the field change \
   and is back-filling. Publishing with coverage stamped on the report."
```

The run log records the reason, the rule-set hash and the code version. Nobody
ships a bad number by accident; anybody who ships one on purpose leaves their
name on it. A reason under ten characters is rejected.

**What a blocked run does *not* do:** touch the published warehouse's data. It
builds into `hourglass.rejected.db` instead, so `sql/analytics.sql` and every
other reader keep seeing the last warehouse that passed.

**What it *does* do is leave a record, which it did not used to.** A blocked
run's audit row went only to the quarantined copy, which the next blocked run
overwrote, so the published `run_log` held 126 rows, all of them successes, and
read as though the gate had never fired. A refused acknowledgement — somebody
attempting to sign off a PHI failure, which is the most interesting event this
gate can record — survived only in a quality report the next run replaced.
`pipeline.append_run_log_row` now appends a single row to `run_log` in both the
published warehouse and a `run_audit.db` sidecar, and `refused_acknowledgements`
is a column in `sql/star_schema.sql`. Write-Audit-Publish survives that, because
WAP is about *data* reaching readers: the run that failed has still published no
fact or dimension row, the INSERT touches nothing else in the file, and SQLite
commits it atomically, so a concurrent reader sees the previous complete
warehouse either with the audit row or without it. The alternative — rebuilding
the live warehouse to record a failure — is what WAP actually forbids. Columns
absent from an older file are dropped rather than raised on: an audit row one
field short beats an audit row that does not land.

---

## Metric parity: one definition, three engines, two of them executed

`hourglass.metrics` holds a registry of eleven metrics. Each is declared
once, with the sentence it means, the grain it is computed at, a SQL expression,
a pandas implementation, and the base columns its DAX measure must reference.

On every run the `parity` task **executes** the SQL against the warehouse that
was just built and the pandas against the frames that produced the dashboard, and
compares the two to a stated tolerance (`1e-6`, not zero, because float addition
is not associative and the two engines sum 52,160 rows in different orders).

**The DAX is not executed.** There is no DAX engine in CI — running one means a
Power BI workspace, a licence and a service principal. What is checked instead is
a static column contract: each of the ten metrics that declares one must have a
measure of that exact name in `bi/measures.dax`, and that measure's body must
reference the base columns the metric declares. That catches a measure summing
`units_delivered` where the metric is defined on `minutes_delivered`, and a
measure that disappeared during an edit. It does not catch a logic error inside
otherwise-correct DAX. Two engines are checked by execution and the third by
contract; calling that three would be the error the rest of this project exists
to avoid.

**A disagreement raises rather than warns.** If SQL and pandas differ, the
`parity` task fails, the orchestrator marks `publish` SKIPPED, and the previous
build's exports stay in place. A warehouse whose two consumers disagree is worse
than one that is merely stale: nobody can tell which number to act on.

It found a real defect on its first execution — `build_utilization` filtering
with `.loc[]` on an int64 flag column. `.loc` is **label**-based, so a mask of
0s and 1s is not a mask at all: pandas reads each element as an index label and
returns duplicated copies of the rows *labelled* 0 and 1, one per element, and
it does not raise. Reproduced on pandas 3.0.2 against the current warehouse, the
filter returns 52,160 rows that are all copies of the first two sessions — and
because label `1` is a cancelled session with zero units and label `0` is a
completed one with fourteen, the selection came out exactly inverted: every
session that should have passed the filter contributed row 1's zero, and each of
the 7,626 that should have failed contributed row 0's fourteen. 7,626 × 14 =
106,764, which is the number the harness reported against 410,405 from SQL. The
filter did not merely fail to filter; it computed the headline over two rows.
Every test in the project built its frames on the transform side, where the flags
really are booleans, so it behaved correctly in every test and incorrectly in the
pipeline. INC-004 in [`docs/INCIDENTS.md`](docs/INCIDENTS.md).

**What parity does not cover, and INC-005 is the entry about it.** The registry
compares two *implementations of a definition*. It cannot see a figure computed
somewhere else, and for the life of one defect it printed "All 11 metrics agree"
over a dashboard whose most prominent tile was wrong — truthfully, because the
registry's `hours_unused` read the correct column while the tile was a separate
calculation nobody had registered. `metrics.check_published_headlines` closes
that: `verify` re-derives five headline figures in SQL from the warehouse and
compares them against what was actually written to `dashboard_data.json`, and
raises on a disagreement. A check verifies the value it names, not the value the
reader sees.

The report is written on every run: `data/out/reports/metric_parity.md`, with
the published-headline comparison appended by `verify`.

---

## The run-over-run diff

Tests answer *is this data valid*. The diff answers a question nothing else here
asks: *did this run change anything, and was it what I meant to change?* Both can
pass and the second can still be a disaster — every check green, every row
plausible, and 40,000 sessions quietly fifteen minutes longer because a service's
`minutes_per_unit` was edited. Nothing is invalid. Everything is different.

`hourglass.diff` compares this build against the last published one by primary
key, not by row order, and reports rows added, removed and changed plus a
per-column changed-cell count — because "412 rows changed" is a shrug and "412
rows changed, all in `minutes_delivered`" is a diagnosis. Output:
`data/out/reports/data_diff.md`.

The limit is size. Loading both sides into memory and merging stops being viable
at a hundred million rows; the technique that replaces it is column checksums per
key range. At this size, in-memory is correct and simpler.

Its first version reported 49,227 changed rows between two runs of a
deterministic pipeline, because it compared cells with `astype(str)` and
`NaN != NaN`. A monitoring tool that cries wolf is worse than no monitoring tool:
the damage is done to the reader's attention. INC-002 and INC-003 in
[`docs/INCIDENTS.md`](docs/INCIDENTS.md).

---

## What the pipeline found

The EHR's unit-of-measure column changed meaning on **2026-04-01** — minutes
before, 15-minute units after — and a slice of the post-change rows lost the flag
entirely. Nothing errored. No test failed. Delivered hours stayed plausible.

| Check | What it says |
|---|---|
| `uom_resolution_coverage` | 2,933 of 52,160 sessions cannot be interpreted. **BLOCK.** |
| `uom_coverage_step_change` | Coverage fell 7.9 percentage points between 2026-03 and 2026-04 and did not recover. |

Knowing 5.6% of rows are unusable is worth something. Knowing they all start in
one month turns an open-ended cleanup into one question for one vendor about one
release. Full write-up: **[`docs/ANOMALY.md`](docs/ANOMALY.md)**.

---

## Cloud, orchestration, and scale

**Cloud.** The S3 layer is real boto3 against the real S3 API. One environment
variable changes between environments: LocalStack (`make run-s3`), real AWS
(unset `AWS_ENDPOINT_URL`), `moto` in tests, or a filesystem mirror with the
identical key layout when there is no Docker. Objects land at
`raw/source=<system>/ingest_date=<date>/`, partitioned by *ingest* date so a
corrected re-extract lands beside the original instead of overwriting it.

**Which failures fall back, and which do not.** A connectivity failure falls
back to local, and so does the absence of any credentials at all — no `AWS_*`
variables, no `~/.aws`, no instance role. Somebody who has never configured AWS
is not misconfigured, they are not using it, and that is how most readers of
this repository will run it. *Partial* credentials raise instead: an access key
with no secret means somebody meant to use S3 and got it wrong, and a silent
fallback would hide the mistake behind a run that looks like it worked. An
**auth** failure — a rejected or expired key — also raises, because silently
writing to disk and reporting success is the exact class of failure this project
argues against.

The no-credentials case is there because it was missing. botocore signs a
request before it opens a socket, so a machine with nothing to sign with raised
at signing and never reached the connection error the fallback was written to
catch — the fallback that exists for a reviewer with no Docker and no AWS failed
for exactly that reviewer, and for nobody with credentials of any kind. It was
found by running `make check` on a laptop that had never been told about AWS.
INC-007 in [`docs/INCIDENTS.md`](docs/INCIDENTS.md), and it is the entry to read
first.

**Orchestration.** `hourglass.orchestration` is about two hundred and seventy
lines: a
topological sort with cycle detection at construction, per-task retries with
backoff, failure isolation, and a JSON-lines run log. Retries are declared where
the flakiness actually is — `land` and `read_lake` get three attempts, every pure
transform gets one, because retrying deterministic code that just failed is a way
of failing more slowly. A failed task marks its dependents SKIPPED rather than
running them against missing inputs. The honest limit is in the docstring and in
the limitations section below: one process, one machine, no scheduler, no
backfill, no cross-run concurrency control.

**API ingestion, and it is a demonstration rather than a live component.**
`hourglass.sources` has no caller in `src/` or `scripts/` — the pipeline's
authorisations arrive as a file drop, so nothing in a `make run` executes a line
of it. Say that first, because the first question it invites is "show me where
that runs." What it is, is a payer API client written against the failure modes
a file drop does not have: cursor pagination — offsets shift under inserts and
silently skip or duplicate rows — retry with exponential backoff on 429 and 5xx
only, `Retry-After` honoured when the server sends it, a page budget so a server
returning a cursor pointing at itself errors instead of filling the disk, and —
the one that matters — completeness checked against the total the server
declared, because a short fetch returns a plausible number of rows and no error.
`FakePayerAPI` is a deliberately awkward in-process server that paginates,
rate-limits and fails intermittently, so the client's error handling is
exercised by something that actually misbehaves rather than by a mock that
agrees with it. No network is involved anywhere in it, and `tests/test_sources.py`
is the only thing that calls it.

**Incremental loading.** `hourglass.incremental` implements the watermark, the
lookback window, and merge-by-business-key. The lookback is the part people leave
out: a strict watermark never sees a session entered late for a date below the
mark, so each run re-reads seven days behind the mark and merges what it finds.
The merge is delete-then-insert on the key set inside one transaction, and the
insert goes through `executemany` rather than `DataFrame.to_sql` — pandas commits
its own transaction, which would end the one wrapping the delete, and a delete
that commits without its insert is data loss. The property that makes the whole
thing trustworthy is asserted —
`test_incremental_matches_a_full_reload` runs both and compares the tables.

**Scale.** `make benchmark` runs the real pipeline at increasing size across all
twelve tasks and writes `data/out/reports/benchmark.md`. **That file is the
result; this paragraph is not.** No table is reproduced here, deliberately — an
earlier version of this section quoted one, the numbers moved on the next run,
and the README then contradicted the artifact it was pointing at. A document
restating a measurement it does not compute will eventually be wrong, and this
project has an incident about exactly that (INC-005). Read the report.

What the report has shown consistently across runs, and what is therefore worth
saying:

- **Nothing degrades sharply inside the range measured** — 52,160 to 617,851
  session rows, a twelvefold increase. Most stages come out below 1.00× per-row
  cost and the top of the column has never exceeded 1.15×.
- **Which stage comes top is unstable, and that is the more useful finding.**
  One run put `load` at 1.15×, another put it at 1.01× and not at the top at
  all, a third put `parity` top with `load` a hundredth behind. This is a shared
  machine, each scale is measured once, and a stage taking four tenths of a
  second at 1× is timed close to the noise floor. A stage that changes places
  between runs is not degrading; it is a single sample on contended hardware.
  Repeated trials and a median are what this would need before anyone made a
  decision on it, and that is a fair thing to be asked about.
- **`diff`, `parity` and `verify` are consistently 44–45% of wall clock, at
  every scale.** Nearly half the run is spent checking the work rather than
  doing it, because each of the three re-reads the whole build. That ratio is
  stable where the growth column is not, which is why it is the number to
  quote. It is what the guarantees cost.

The report's own closing paragraph is **derived from its table** rather than
asserted. Above 1.05× it names the worst stage and says it is the one to
rewrite first; at or below, it says no stage degrades faster than the data
grows and points at memory instead. It used to be a fixed string in
`scripts/benchmark.py` claiming the full reload was "the design decision that
expires first", re-emitted whatever the timings came out at — and for several
runs the measurements disagreed with it.

The thing that expires first therefore probably expires on **memory, not
time**. `conform`, `analyse` and `protect` hold whole frames, and `diff` holds
two builds at once; none of that appears in a timing table until the machine
runs out of RAM, at which point the curve is not gradual. Say "probably", and
say why: there is no memory profile in this repository. That claim is an
argument from what the code holds, not a measurement, and the difference matters
in a document where the other claims are measured.

Two caveats on the measurement itself, both artefacts of how the script works.
The benchmark runs each scale against the build the previous scale published,
so the `diff` timings at 4× and 12× compare warehouses of different sizes and
are not like-for-like. And a run started against an empty data directory has no
previous build at 1×, which makes `diff` 0.00s and its growth ratio undefined;
the report prints a dash there rather than dividing by a floor, which an earlier
version did — and rendered as 466,666,666.67×, a number wrong enough to
discredit the column it sat in.

---

## Reporting

Three consumers, one source of truth, generated in the same run so they cannot
drift.

- **The weekly digest** (`weekly_digest.md`) — for a clinical operations
  coordinator. Plain language, grouped by centre because that is who acts, with
  the data-quality caveat next to the numbers rather than in a footnote, and
  every head count passed through small-cell suppression before it is printed,
  and centres below the threshold pooled rather than named (the hours and the
  authorisation count stay: they are not counts of people).
  A dashboard answers questions somebody already thought to ask and requires them
  to open it; most weeks nobody does, and the authorisation still expires. The
  digest arrives. It is tested for the things that would make it harmful:
  leaking an identifier, quoting a number without its caveat, or using a word
  like "utilisation".
- **Power BI** — eight CSVs, `bi/measures.dax` (27 measures), and
  `relationships.md` (nine relationships, and the two mistakes that will bite
  you). Rates are measures never calculated columns; every division goes through
  `DIVIDE()`; booleans export as integers because DAX will not compare a logical
  to a number. Twenty-seven counts *named measure definitions*, and two other
  counts are easy to arrive at, so it is worth saying which one this is.
  `metrics.parse_measures` reports 29: its header pattern accepts any
  unindented line ending in a bare `=`, and `VAR LastSessionKey =` and
  `VAR LastSessionDate =` inside `As Of Date` are written that way. A naive
  `grep -cE '^\w.* ='` reports 32, catching three further top-level `VAR` lines
  whose value sits on the same line. Subtract the five `VAR` lines and 27 is
  what is left. Ten of the twenty-seven are under the column contract; the rest
  are unchecked by anything.
- **`dashboard.html`** — self-contained, offline, no licence. Its five headline
  figures are re-derived from the warehouse by `verify` on every run.

---

## Layout

```
src/hourglass/
  config.py         every tunable value, with provenance on the ones that matter
  generate.py       synthetic source extracts, seeded and deterministic
  ingest.py         S3 landing zone (boto3 · LocalStack · moto · local fallback)
  sources.py        paginated payer API client; tests only, not on the pipeline path
  transform.py      unit resolution, deduplication, SCD Type 2, fact builds
  model.py          star schema load; atomic rebuild via scratch file + rename
  incremental.py    watermark, lookback window, merge by business key
  phi.py            PHI classification, content scanning, the egress gate
  disclosure.py     CMS small-cell suppression, including complementary suppression
  quality.py        17 checks, 3 severities, the acknowledgement mechanism
  analytics.py      utilisation, pace, at-risk selection, the assumption spread
  metrics.py        one definition per metric; SQL vs pandas parity, DAX contract
  diff.py           run-over-run value diff by primary key, per-column attribution
  export.py         Power BI CSVs, DAX, dashboard payload
  digest.py         the weekly digest, written for a non-technical reader
  orchestration.py  task graph, retries, timing, structured logs
  pipeline.py       the twelve tasks and the CLI
sql/                star_schema.sql (grain on every table) · analytics.sql (10 queries)
bi/                 measures.dax (27 measures)
tests/              461 tests across 15 files, including property-based tests
scripts/            run_analytics · build_dashboard · build_data_dictionary
                    · benchmark · mutation
docs/               ANOMALY.md · INCIDENTS.md · MUTATION.md · DEFENSE.md
                    · DATA_DICTIONARY.md (generated)
```

---

## How it is tested

461 tests across 15 files, plus three mechanisms that answer questions a test
count cannot.

**Idempotency is asserted, not assumed.** `tests/test_pipeline.py` runs the whole
pipeline twice and compares a checksum of every table. The run log is the
deliberate exception — it is append-only, because the record of what ran should
not be erased by a re-run.

**Defence in depth is proven, not asserted.**
`tests/test_phi.py::TestDefenceInDepth` disables the innermost protection —
sample redaction — runs the real pipeline, and asserts that the outermost one,
the byte-level artifact scan, still catches the leak and fails the `verify` task.
Two layers are only two layers if you have shown one works without the other.

**Property-based tests** (`tests/test_properties.py`, Hypothesis) state
invariants and let the generator choose the inputs: a suppressed table is never
disclosive, a frame diffed against itself is identical, SCD validity ranges never
overlap, a surrogate never contains its identifier, unused units are clamped per
authorisation. Both of this project's most expensive defects were inputs nobody
would have written down as a fixture.

Two of those properties were written as the invariant that *ought* to hold rather
than weakened into one that did, and both failed on the day they were written.
They were marked `xfail(strict=True)` while they failed, the source was then
fixed, and they are ordinary passing regression tests now — each carrying the
shrunk failing case in its docstring, so the reason the assertion exists survives
the fix. A present-but-unrecognised unit of measure (`'hours'`, `'each'`) was
refused with a null `unresolved_reason` and so fell out of the assumption spread
entirely; `resolve_minutes` now assigns `unrecognised_uom` with a catch-all
beneath it. And a null primary key made a frame differ from itself in `diff.py`,
because INC-002's sentinel had been applied to cell values and not to the key
set; it is applied to key columns now. There are no `xfail` markers left in the
suite — `grep -n xfail tests/` finds only those two docstrings.

**Hermeticity is enforced, not hoped for.** `make check` used to fail
intermittently because the suite wrote to the repository's real `data/out/`,
racing the atomic rename with whatever else was running. `tests/conftest.py` now
points `HOURGLASS_DATA_DIR` at a throwaway tree, and it does so at *conftest
import time* — before pytest imports any test module and therefore before
anything imports `hourglass`. The ordering is the mechanism, not tidiness:
`config.DATA` is read once at import, and `pipeline.py`, `export.py` and
`ingest.py` bind paths from it at import and again as frozen default arguments.
A fixture that monkeypatched afterwards would have redirected some call sites
and silently missed the rest, which is worse than not redirecting at all. The
`workspace` fixture asserts `config.DATA` is the temporary path and fails with
that explanation if anything imported the package too early.

**Sabotage** answers the question underneath all of the above: *how would you
know if this were wrong?* `make prove` copies the project to a scratch
directory, makes one real single-line change to the source — the kind a tired
person makes on a Friday, or that an assistant produces confidently while every
test stays green — runs the whole pipeline, records what happened, puts the
source back, and moves on to the next.

Seven sabotages, and the results are not uniform, which is the point:

| Sabotage | Outcome |
|---|---|
| Hours computed with a flat quarter-hour per unit (INC-005) | **stopped the release** — published-headline check |
| A filter that silently stops filtering (INC-004) | **stopped the release** — metric parity |
| De-identification switched off (INC-001) | **stopped the release** — egress scan |
| A metric's SQL edited by 1% | **stopped the release** — metric parity |
| A service's minutes-per-unit changed | published; diff reported 56,223 rows differ, under a banner saying the definitions moved |
| Deduplication removed | published; diff reported 222 rows differ — exactly the 222 seeded |
| The at-risk window changed from 30 days to 90 | published; diff reported the rule set changed |

**Run 20 August 2026: four stopped the release, three were reported, none passed
silently.** Read that as a statement about the seven cases
and not about the pipeline — every one of them had to be imagined and written
down first, so a clean sweep says these seven are covered and says nothing about
the eighth. The blind spots still open are the ones nobody has thought to
sabotage, and a harness cannot report those by construction.

The last two rows publish, and should. `EXPIRY_WARNING_DAYS` is a policy
constant and a `minutes_per_unit` correction is a definition change; neither is
a data error, and the pipeline has no way to know which value was intended. What
it can do is say so, which until 20 August it could not: the rule-set hash
omitted the unit-conversion table, and nothing compared the hash against the
previous published run, so a definition change moved every number in silence.
The hash now covers the conversions and `task_diff` compares it run over run,
with a banner at the top of `data_diff.md` warning that a difference below it
may be a change in what the numbers *mean* rather than in the care delivered.
INC-006 in [`docs/INCIDENTS.md`](docs/INCIDENTS.md).

The script asserts each declared outcome in both directions: a safety net that
stops working is a failure, and so is a documented blind spot that has quietly
started being caught, because then the documentation is lying in the flattering
direction. That second assertion is not theoretical here. The harness had
itself gone stale for one run — its detector looked only for "rows differ", and
a definition change leaves every warehouse row identical, so it kept reporting
case 7 as caught by nothing after the banner already existed. A blind-spot
report that has gone stale is worse than no report.

**Mutation testing** answers whether the tests assert anything or merely execute
the code. Two of eighteen modules have been scored (`docs/MUTATION.md`):
`disclosure.py` at **97.5%** (78/80), with both survivors demonstrated to be
equivalent mutants — changes that cannot alter behaviour — which puts the
effective score at 100%; and `transform.py` at **75.7%** over the mutants its
paired test file reaches (482/637), or 51.2% over all 942 generated. Both
`transform.py` figures are given because neither alone is honest.

The `disclosure.py` figure was 79.4% on the previous run and the improvement is
not the interesting part. Three of the mutants that survived that run were real
gaps in behaviour another module depends on for a privacy guarantee: a masked
count keeping a visible percentage beside it in the grouped path, a `suppressed`
flag column that could be replaced wholesale with `None` while every test passed,
and an index reset whose first regression test could not distinguish the two
cases. `digest.py` routes its centre-pooling decision on that flag column, and
`tests/test_disclosure.py` was fully green throughout. Nine tests were added in
response. The survivors are the finding, not the score.

It is a periodic audit rather than a CI gate. `disclosure.py` takes about four
seconds of mutant execution; the package-wide sweep was stopped at the
eight-minute mark, by which point `transform.py`'s own 942 mutants had
completed — `docs/MUTATION.md` reports completed figures only.

---

## What this does not do

Stated plainly, because a project that claims no limitations is not being read
carefully by its author.

- **The default path is a full reload.** `hourglass.incremental` exists and is
  tested, but the pipeline itself still rebuilds. Wiring it into the main path
  needs a source that can be queried by watermark; the file drop here cannot.
- **SQLite, not a warehouse.** The dimensional model transfers to Redshift or
  Snowflake unchanged; the loader would not.
- **One process, in order, on one machine.** The orchestrator has no scheduler,
  no distributed execution, no backfill window and no cross-run concurrency
  control. Those are the reasons Airflow exists, and the point at which this
  should be replaced rather than extended.
- **Provider is Type 1.** Corrections overwrite. Client history matters for
  attribution; provider history did not earn the complexity, and that is a
  judgement rather than an oversight.
- **Deduplication is a heuristic.** The source has no session start time, so the
  business key cannot distinguish a genuine second visit on the same day from a
  double entry. The count is reported as a WARN so the number is visible.
- **Authorisation amendments are not modelled.** Authorisations are as issued,
  not as revised. Real payers amend them.
- **The at-risk list is a signal, not a work queue.** It does not know who is on
  leave or where there is no clinician available. It says where to look.
- **The PHI content scanner samples, and is blind to two identifier types.**
  5,000 rows per frame, which catches systematic contamination and will not
  reliably catch a single stray row. It is regular expressions, so a personal
  name and an ISO-format date of birth both pass it — those are covered by the
  classification layer, which is a declaration rather than an inspection.
- **The DAX is not executed.** Metric parity runs SQL and pandas against the same
  warehouse and compares the results. The DAX measures are checked against a
  static column contract — the measure exists and references the declared base
  columns — which does not catch a logic error inside otherwise-correct DAX.
- **Cell suppression is a greedy pass over a single margin.** Correct for the
  tables published here, one grouping dimension plus a total. Not equivalent to
  linear-programming cell suppression, which is what a multi-dimensional table
  with margins in several directions needs.
- **Cell suppression is applied at two call sites and no chokepoint.**
  `digest.py` and the org-wide child count in `export.py` are `disclosure`'s
  only callers in `src/`. The remaining panels and the BI exports publish their
  counts unsuppressed, which is defensible at the population grain they
  currently use and is not enforced by anything. The dashboard's at-risk list
  reached that grain by having `center_name` removed from it in `export.py` —
  fixes at call sites, not a chokepoint.
- **`hourglass.sources` is not on the pipeline's path.** The payer API client is
  exercised only by `tests/test_sources.py` against an in-process fake server;
  nothing in `src/` or `scripts/` imports it. Authorisations arrive here as a
  file drop, so it demonstrates a capability rather than serving one.
- **The diff loads both sides into memory.** Fine at 52,000 rows and not viable
  at a hundred million, where the replacement is column checksums per key range.
- **Pseudonyms are unlinkable between runs by default.** With no salt configured
  the process mints an ephemeral one, so this week's exports cannot be joined to
  last week's outside the boundary. That is the deliberate trade against a
  checked-in constant, and it is a real loss of utility, not a free win.
- **`verify` reads `dashboard.html` and `pipeline_runs.jsonl` one run late.**
  The rendered HTML is built after the pipeline exits and the run log is
  flushed after the task loop ends, so both are the previous run's copy. The
  hand-written scan list that skipped `metric_parity.md` and `data_diff.md`
  entirely is now a glob over both output directories — INC-010 has the
  write-up.
- **The published-headline check covers seven figures**, not the whole payload.
  `hours_unused`, `units_authorized`, `units_delivered`,
  `expected_units_to_date`, `pace`, `active_authorizations` and
  `closed_authorizations` are re-derived from the warehouse — pace joined the
  list only after a sabotage published 76.0% for 75.1% unchallenged (INC-009).
  The at-risk table, the per-payer and per-discipline breakdowns and the
  assumption spread are not, so the class of defect INC-005 records is still
  possible in every panel below the tiles.
- **Mutation testing covers two of eighteen modules.** `phi.py` and `quality.py`
  carry guarantees at least as load-bearing as the two that were scored, and
  nothing is known about the grip of their tests.
- **Patient identity is assumed clean.** The generator issues one `client_id`
  per child across all three source systems, so identity resolution — the first
  thing a real EHR or payer integration breaks — has no work to do here.
  `check_session_reconciliation` catches hard mismatches loudly; the same child
  under two identifiers is invisible by construction, and no matching, whether
  deterministic or probabilistic against an MPI, exists.
- **A stale build looks like a fresh one.** A blocked run leaves the previous
  build serving — deliberately — but nothing measures how old the serving build
  is. After a week of blocked runs the digest still presents last-good numbers
  with only the `as_of` date to give it away: no freshness check, no age
  warning, no alerting.
- **The entrance has no contract.** Unknown columns fail closed on the way out.
  On the way in, a renamed source column dies as a bare `KeyError` in
  `conform`, and an added one is silently dropped by the explicit column
  selections in `transform.py` before the fail-closed layer can see it.
- **No history of check results is kept.** Each run's quality report overwrites
  the last, and `run_log` keeps blocking failures only, so drift in WARN-level
  results across weeks is invisible unless somebody diffs reports by hand.
- **Nothing pins the environment a test runs in, beyond AWS credentials.**
  INC-007 was a test whose result was decided by the host rather than by the
  code, and the fix declares the credential environment inside the tests that
  depend on it. `HOME`, `TZ`, `LANG`, locale-dependent number formatting, an
  installed binary and the clock are all still ambient, and no test enumerates
  them. CI runs two Python versions on one Linux image; running the suite
  somewhere else entirely is the only thing that has ever found a defect of
  this shape.

---

## Why it exists

Written in August 2026 as a worked example for a junior AI data engineering role
at a paediatric autism provider — the thing a first sprint would plausibly ask
for: a well-scoped pipeline, a Kimball model, quality checks, an anomaly to
investigate, and a report. Built with AI coding assistance throughout, and
verified at every step. Developed in a private repository and squashed for
publication, which is why the incident log runs longer than the commit log.

The verification is the part worth reading. The gate that refuses to publish, the
boundary that reads the published bytes instead of trusting configuration, the
parity check that recomputes every registered metric in SQL before anything is
published and the headline check that re-derives the published tiles after,
the 461 tests, and the assertion that running it twice
produces an identical warehouse are what turn generated code into something you
can put a number in front of a payer with.

`docs/DEFENSE.md` explains every module, why each decision was made, and what an
interviewer would ask about it. `docs/INCIDENTS.md` is the seven defects that got
through, and what now catches each class without anyone having to remember. The
last of them was found by somebody running `make check` on a machine that was
not the one it was built on, which is the cheapest check in this document and
the only one nothing here automates.
