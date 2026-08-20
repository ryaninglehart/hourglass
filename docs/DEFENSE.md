# Defending this project

This document is for you, not for a reviewer. It explains what every piece of
Hourglass does, why it was built that way, and what you will be asked about it.

Read it with the code open. Do not read it as prose — go to the file, find the
thing it describes, and say out loud what it does before you read the
explanation. The point is not that you can recite this. The point is that you
can be asked a question that is not in here and still answer it.

---

## Before anything else: what to say about how it was built

You will be asked, or it will be implied. Have this ready and say it early
rather than letting it hang:

> "I built this with heavy AI assistance — that's how I work, and it's how the
> posting says the team works. What I'd want you to look at is the verification
> around it: the gate that refuses to publish, the PHI boundary that reads the
> published bytes instead of trusting configuration, the parity check that
> recomputes every registered metric in SQL before anything ships and the
> headline check that re-derives the published dashboard tiles from the warehouse
> after, the four hundred and fifty-six tests, and the assertion that running
> it twice gives an identical warehouse. Deciding what to check is the part that
> isn't generated."

Three things make that a strong answer rather than an excuse:

1. **It is true**, and the alternative — implying you typed it all — collapses
   the moment somebody asks you to change something live.
2. **It matches the job.** The posting asks for AI-assisted development with
   verification of output. That is the actual competency, and this repository is
   an argument that you have it.
3. **It moves the conversation to your strongest ground.** The quality gate, the
   severity design, and the tests are decisions, not code, and you can defend
   decisions.

What you must **not** do is claim you could write `transform.py` from a blank
file today. If asked directly, say so, and immediately say what you *can* do:
explain every decision in it, change it, and tell them what would break.

---

## The ten-minute version

If a recruiter call starts in ten minutes, learn these eight things.

1. **What it does.** Compares authorised therapy hours against hours actually
   delivered, and flags authorisations expiring soon with hours unused — which
   means a child is about to lose approved care.

2. **Why that matters here.** Under a value-based contract, undelivered
   authorised care moves the outcome measures the contract pays on. It is a
   revenue problem and a care problem at the same time, and it is invisible from
   inside the treatment room because it only appears when two systems are read
   together.

3. **Two fact tables at different grains.** Sessions and authorisations. Never
   joined directly — sessions are aggregated to the authorisation grain first.
   Joined raw, authorised units inflate by 55× on this data and nothing errors.

4. **The defect.** The EHR changed its unit of measure on 2026-04-01 and some
   rows lost the flag. The pipeline refuses to guess, blocks publication, and
   localises the change to a single month.

5. **Pace vs utilisation.** An authorisation half way through its window should
   have delivered about half its units. Measuring open authorisations against
   100% makes healthy delivery look like a crisis.

6. **The PHI boundary.** Nothing that identifies a person crosses into a
   published file. Two layers: a classification where unknown fails closed, and
   a scanner that reads the values rather than trusting the classification.

7. **Metric parity.** The same eleven registered metrics are computed in SQL and
   in pandas on every run and compared; a disagreement blocks publication. It
   found a real bug on its first execution. The DAX is checked against a column
   contract, not executed — say that unprompted. And say what parity *cannot*
   do: it compares two implementations of a definition, so it passed for the
   whole life of a wrong number on the dashboard that nobody had registered.
   `verify` now re-derives five published headline figures from the warehouse.
   INC-005.

8. **The number.** 52,160 sessions, 1,825 authorisations, 240 children,
   461 tests, 17 quality checks, 11 registered metrics, twelve orchestrated
   tasks, eight warehouse tables plus a run log. All synthetic.

---

## Module by module

### `config.py` — every tunable value, in one place

**What it does.** Paths, S3 settings, business thresholds, the CPT service
catalogue, and the generator's shape.

**Why it exists.** Two reasons. A reviewer looking for "what would I change"
finds it in one file. And every number that carries business meaning has a
comment saying where it came from — because a threshold with no provenance is a
number somebody made up, and six months later nobody knows if 0.90 was
researched or guessed.

**Look at:** `SERVICES`. Each entry has `unit_basis` and `minutes_per_unit`.
That is the reason `dim_service` has to exist at all.

**You'll be asked:** *"Why is the service catalogue in config rather than a
database table?"* — Because it changes rarely and it is reference data the code
depends on structurally: `resolve_minutes` cannot work without it. In
production it would live in a table with the catalogue loaded from it; here,
keeping it in code makes the dependency visible.

---

### `generate.py` — synthetic source extracts

**What it does.** Produces three CSV extracts that stand in for a CRM, a payer
authorisation feed, and an EHR session log. Seeded, so the same seed gives
byte-identical output.

**Why it matters.** Determinism is load-bearing. The idempotency test asserts
that running the pipeline twice gives an identical warehouse — but that proves
nothing if the *input* changes between runs. Stable output over unstable input
is luck, not idempotency.

**Look at:** the block that writes `duration_uom`. That is the seeded defect,
and it is about ten lines.

**Also look at:** `observed_fraction`. An authorisation period can start before
the extract window opens. Without scaling the delivery target to the overlapping
slice, a period beginning two weeks before the window ends receives six months of
sessions crammed into ten weekdays, and every downstream pace figure inherits the
nonsense. This bug was in the first working version and was caught by reading the
output, not by a test — the headline pace read 111% while every per-payer figure
read 83%.

**You'll be asked:** *"Why synthetic data?"* — Because the alternative is real
PHI, and there is no version of this project worth building that involves a
candidate handling real patient records. The shape follows published billing
practice so the modelling problems are real even though the rows are not.

---

### `ingest.py` — the S3 landing zone

**What it does.** Copies extracts into an S3 data lake with partitioned keys and
writes a manifest with SHA-256 digests.

**The design decision worth defending:** partitioning by **ingest date**, not
business date.

> A corrected or late-arriving extract lands in its own partition instead of
> silently overwriting the earlier one. The lake keeps a record of what was
> received and when. Business-date partitioning belongs downstream, after the
> data has been conformed.

**Why three backends.** The same boto3 code runs against real AWS, LocalStack,
and `moto` in tests. Only `AWS_ENDPOINT_URL` changes. If the tests faked the S3
client instead, they would prove nothing about the code that ships.

**What falls back and what raises, because the distinction is the interesting
part.** No credentials at all falls back to the local mirror — that is somebody
who has never configured AWS, which is most people who will run this. Partial
credentials raise: an access key with no secret is a mistake, and a silent
fallback hides it behind a run that looks like it worked. Rejected or expired
credentials raise for the same reason. The no-credentials branch had to be
written separately from the connectivity branch because botocore signs before it
connects, and that omission is INC-007 — the one defect here that depended on
the machine it ran on. `S3Backend` also builds a fresh `boto3.session.Session()`
rather than calling `boto3.client(...)`, which would resolve credentials once
per process and cache them for every later caller.

**You'll be asked:** *"Have you used AWS?"* — Be exact. "I've built against the
S3 API with boto3 and tested it with moto and LocalStack. I have not run
production workloads on AWS — my own infrastructure is Fly.io and Cloudflare R2,
which was a monthly-cost decision. The API surface here is real; the operational
experience isn't yet."

That answer is much stronger than either overclaiming or apologising.

---

### `transform.py` — conforming, and the two hard parts

**Part one: unit resolution.** `resolve_minutes` converts a duration to minutes
using the unit flag and the service's `minutes_per_unit`. Rows it cannot resolve
get `uom_resolved = False`, zeroed measures, and a reason.

`resolve_minutes_naive` is kept deliberately — it is the bug, preserved, so the
pipeline can quantify what guessing would have cost.

**Part two: SCD Type 2.** `build_dim_client` turns a CRM change log into a
dimension with validity ranges. Each row closes the day before the next one
opens; the newest runs to 9999-12-31 and is flagged current.

**The trap, and you will be asked about it:** in `build_fact_session` the client
join is an *as-of* join — a session attaches to the client version whose validity
range contains the service date. Get this wrong by joining on `client_id` alone
and every session fans out into one row per client version. The test
`test_no_row_is_duplicated_by_the_scd_join` exists because that is the classic
failure.

**You'll be asked:** *"What is a slowly changing dimension?"*

> "A dimension whose attributes change over time. Type 1 overwrites — you lose
> history. Type 2 keeps a new row per change with validity dates, so facts stay
> attributed to what was true when they happened. Here, a client's payer can
> change mid-year. If I overwrote it, sessions delivered under the old payer
> would be re-attributed to the new one, and the payer's utilisation numbers
> would be wrong in both directions."

*"Why is provider Type 1 then?"* — A judgement call. Client attribution feeds
payer reporting, so history is load-bearing. Provider history did not earn the
complexity for the questions this model answers. It is in the README's
limitations section because it is a trade-off, not an oversight.

---

### `model.py` — loading, and atomicity

**The decision to defend:** the load is **not** wrapped in a transaction, and
the docstring says why.

> pandas commits per table when writing through `to_sql`, so an enclosing
> transaction would look like protection without providing any. Atomicity comes
> from building the warehouse in a scratch file and swapping it in with a single
> `os.replace`. A reader sees either the previous complete warehouse or the new
> one, never a half-loaded one.

This is worth knowing cold, because it is the kind of thing where the *obvious*
answer — "wrap it in BEGIN/COMMIT" — is wrong, and the first version of this code
did exactly that and crashed with `cannot commit - no transaction is active`.

**You'll be asked:** *"Is your pipeline idempotent?"* — "Yes, and it's asserted
rather than assumed. `test_second_run_produces_identical_tables` runs the whole
thing twice and compares a checksum of every table. The run log is the deliberate
exception — it's append-only, because the record of what ran shouldn't be erased
by a re-run."

---

### `quality.py` — the gate

**Three severities, one of which stops the pipeline.**

| | Meaning | Stops publication |
|---|---|---|
| `BLOCK` | The number cannot be trusted. | Yes |
| `WARN` | The number is fine; something needs a human. | No |
| `INFO` | Recorded so drift is visible over time. | No |

**The severity design is the interesting part, and it is an opinion you should
be ready to defend:**

- A session with an unknown unit of measure is **BLOCK** — utilisation computed
  over it is meaningless, not slightly wrong.
- A session delivered without an authorisation is **WARN** — the number is
  correct; it is the business situation that is bad.

If you can articulate that distinction you have said something most candidates
cannot: that severity should track *whether the number can be trusted*, not *how
upsetting the finding is*.

**You'll be asked, and you should raise it first:** *"You have a parity check
that compares two engines. Why do you also need a check about overlapping
authorisation periods?"* — Because the parity check structurally cannot see it,
and this is the strongest thing to say about knowing what your own controls are
worth.

> "`check_overlapping_authorization_periods` is BLOCK. Every roll-up in the
> project attributes a session to an authorisation by `client_id +
> service_key + date BETWEEN period_start AND period_end`. If two
> authorisations for the same child and service have windows that intersect,
> that predicate matches both, and every session inside the intersection is
> counted in full against each — delivered units, utilisation and unused units
> wrong for both rows, and the double count flows into every total built on
> them.
>
> Now the part that matters. `analytics.build_utilization` and
> `metrics.AUTH_GRAIN_CTE` are deliberate transcriptions of each other — same
> filters, same join keys, same aggregation order. That correspondence is what
> makes them able to catch a coding error in either one, and it is exactly what
> makes them unable to catch an error in the specification they share. Both
> double-count an overlap, identically, so `metric_parity.md` prints agreement
> over two numbers that are both wrong. Two implementations of one sentence can
> check the transcription; they cannot check the sentence. This check is the
> compensating control for that blind spot, and I would rather name the blind
> spot than let the parity report imply it does not have one.
>
> Nothing in the schema prevents an overlap — no SQL constraint expresses 'these
> two date ranges must not intersect' — and real payers amend and reissue
> authorisations, which is how overlaps appear. The synthetic data contains
> none today, so the check is latent rather than live. That is the point of
> writing it before the data needs it."

Mechanically it is a running maximum rather than a pairwise scan: sort by start
date within each client-and-service group, and a row overlaps something earlier
exactly when it starts on or before the furthest end date any earlier row
reached. The `cummax` is there because an earlier long authorisation can be
overlapped by a later short one that starts before the long one ends but after
the row immediately above it does.

**One more check worth knowing, for the same reason.**
`check_zero_unit_authorizations` is the case `check_utilization_ceiling` cannot
see. Over-delivery is caught by `utilization > 1`; over-delivery against an
authorisation approving zero units has no utilisation at all — the ratio is
undefined, `build_utilization` publishes it as null, and a null is not greater
than one, so the row is absent from every view of the problem. It is WARN on
this module's own definition of the line: the delivered units are real, the
authorised units are genuinely zero, and only the per-authorisation ratio is
unavailable. What is broken is the authorisation record, not the number.

**And the PHI checks are split into two, which is a severity question rather
than a scoping one.** `phi_egress` reports only findings read off the values —
a declared identifier still raw, or an undeclared column — so it has no
false-positive mode and is `acknowledgeable=False`. `phi_content_scan` reports
regular-expression hits, which have false positives by construction: a payer
legitimately named "Member Health Network" matches the member-number pattern.
Un-overridable, that reading halts publication for good with no route back but
editing the pattern; acknowledgeable, the false positive costs a written reason
and a name in the run log and the true positive still cannot be released
quietly. Gating a proof and a heuristic identically gets one of the two wrong.

**The acknowledgement mechanism.** A BLOCK can be released, but the operator must
name the check and write a reason, and the run log records the reason, the
rule-set hash, and the code version. A reason under ten characters is rejected.

> "Nobody can ship a bad number by accident, and anybody who ships one on
> purpose leaves their name on it."

**The rule-set hash.** Without it the log records that checks passed but not
*which* checks. Change a threshold — or a service's minutes-per-unit, which is a
definition rather than a threshold and was missing from the hash until
INC-006 — and the hash changes, so an old verdict can't be mistaken for a
statement about the current rules. `task_diff` now compares it against the last
published run, because a hash that is written and never read is not a control.

**The audit row survives a blocked run, and this is the best story in the gate.**

> "For a while the log was a lie by omission. A blocked run built into the
> quarantined warehouse, so its audit row went there too — and the next blocked
> run overwrote it. The published `run_log` had 126 rows, every one of them a
> success, and read as though the gate had never fired. Refused acknowledgements
> were worse: somebody attempting to sign off a PHI failure is the single most
> interesting event this gate can record, and it survived only in a quality
> report the next run replaced.
>
> The reason it was built that way is that `model.atomic_build` writes the run log
> by rebuilding the whole database and swapping it in. That's the right shape for
> a warehouse and the wrong shape for an audit trail — it makes append-only a
> convention rather than a construction, since the history is re-inserted from a
> read of the old file every publish, and it's unavailable to a run that failed
> its gate, because rebuilding the live warehouse is exactly what
> Write-Audit-Publish forbids.
>
> So there's a smaller instrument: `pipeline.append_run_log_row` does a single
> INSERT into `run_log`, into both the published warehouse and a `run_audit.db`
> sidecar. Two copies because they fail differently — the warehouse log is
> convenient and gets rebuilt; the sidecar is inconvenient and does not."

**You'll be asked:** *"Doesn't writing to the published warehouse from a failed
run break Write-Audit-Publish?"* — This is the question to want.

> "No, and the distinction is what WAP is actually about. WAP stops *data* from a
> failed run reaching a reader. The blocked run has still published no fact and no
> dimension row — those went to `hourglass.rejected.db`. What crosses is one row
> in `run_log` recording that the run happened and was refused. The INSERT
> touches nothing else in the file, SQLite commits it atomically, so a concurrent
> reader sees the previous complete warehouse either with the audit row or
> without it — never a partial one. The alternative, rebuilding the live
> warehouse to record a failure, is the thing WAP forbids, and it's what the old
> code was doing.
>
> Data from a failed run must not reach a reader. The fact that it failed must."

One defensive detail worth pointing at: columns absent from the target's
`run_log` are dropped rather than raised on, because a warehouse built before
`refused_acknowledgements` existed is missing that column, and an audit row that
lands one field short beats an audit row that does not land.

**You'll be asked:** *"What happens when a check fails?"* — Walk them through
`make gate`: seventeen checks run, one blocks, publication halts, `make` exits 2
(the pipeline process itself exits 1), the report is still written. Then
`make run` with the acknowledgement publishes and records why. Offer to run it.

---

### `analytics.py` — the measures

**The grain fix.** `build_utilization` aggregates sessions to the authorisation
grain *before* joining. The comment above it explains why, and query 3 in
`analytics.sql` demonstrates the failure on real rows: 710,734 authorised units
becomes 39,409,848 — a 55× inflation, with no error. Both sides of that
comparison apply the same row filter, so the ratio is the grain error alone.

**The natural-key subtlety.** The session-to-authorisation join uses `client_id`,
not `client_key`. An authorisation spanning a payer change would match only half
its own sessions if it used the surrogate. This is the single most likely thing
for an interviewer to probe, because it is where two correct-looking decisions
(use surrogate keys; keep Type 2 history) interact badly.

**Pace.** `expected_units_to_date = units_authorized × elapsed_fraction`, and
`pace = delivered / expected`. Open authorisations are judged on pace; closed
ones on final utilisation. The `performance` column picks the right one.

**Weighted, not averaged.** `utilization_by` sums numerator and denominator
before dividing. A mean of per-authorisation ratios gives a 26-unit speech
authorisation the same weight as a 2,000-unit ABA one. Query 5 prints both so the
difference is visible.

**You'll be asked:** *"Why not just delivered ÷ authorised?"* — "That's in there
as `utilization`, and it's the right number once a period closes. While it's
open it's misleading: an authorisation a third of the way through its window
shows 33% and looks like a crisis. So open authorisations are measured against
elapsed time. The split is in the model rather than left to whoever writes the
query, because that's the kind of thing that gets forgotten."

---

### `metrics.py` — one definition, three engines

**What it does.** A registry of eleven metrics. Each is declared once —
the sentence it means, the grain it is computed at, a SQL expression, a pandas
implementation, the base columns its DAX measure must reference, and the
tolerance the two executed engines must agree within. On every run the `parity`
task executes SQL against the warehouse and pandas against the frames that
produced the dashboard, compares them, and writes
`data/out/reports/metric_parity.md`.

**Why it exists.** "Hours delivered" is computed in three places here:
`analytics.py` for the dashboard, `sql/analytics.sql` for anyone querying the
warehouse, and `bi/measures.dax` for Power BI. Three implementations of one
sentence. Nothing stops them drifting, and the failure mode is not an error — it
is two reports showing different numbers for the same week and an afternoon
spent finding out which one lied. That is why dbt has metrics and why the whole
semantic-layer category exists.

**You'll be asked:** *"Isn't a metrics registry over-engineering for one
dashboard?"*

> "It would be if the number were computed once. It's computed in three engines,
> and it drifted — the check caught a real defect on its first execution, before
> it had ever caught the thing it was written for. `build_utilization` filtered
> with `.loc[]` on a flag column that comes back from SQLite as int64 rather than
> bool. `.loc` is label-based, so a mask of noughts and ones isn't a mask — pandas
> looks each element up as an index label and hands back duplicated copies of the
> rows labelled 0 and 1, one per element, without raising. The row labelled 1 was
> a cancelled session with zero units and the row labelled 0 was a completed one
> with fourteen, so the selection came out inverted: 7,626 × 14 = 106,764 against
> 410,405 from SQL. The headline was being computed over two rows repeated fifty
> thousand times. Every test in the project built its frames on the transform side
> where the flags really are booleans, so it passed everywhere and was wrong in
> the pipeline."

Be precise about the mechanism if they know pandas, because the sloppy version of
this story is self-refuting. "It returned every row" would make the pandas figure
*larger* than the SQL one; the recorded figures are the other way round, and an
interviewer who spots that has caught you not understanding your own incident.
`.loc` is label-based, `.iloc` is positional, and an integer Series passed to
`.loc` is a list of labels.

**You'll be asked:** *"You say you verify the DAX. Do you?"* — This is a question
about intellectual honesty and the right answer is the limitation.

> "No. There is no DAX engine in CI — that means a Power BI workspace, a licence
> and a service principal, and I don't have those. SQL and pandas are both
> executed and compared to a tolerance; that is a real test. The DAX is checked
> against a static column contract: the measure must exist under the exact name
> the metric declares, and its body must reference the base columns the metric
> declares. That catches a measure summing `units_delivered` where the metric is
> defined on `minutes_delivered`, and a measure that vanished during an edit. It
> does not catch a logic error inside otherwise-correct DAX. Two engines by
> execution, the third by contract — and calling that three would be the same
> species of error the rest of the project is built to avoid."

**You'll be asked:** *"Why does a parity failure block publication instead of
warning?"*

> "Because the failure means the published numbers are not reproducible from the
> warehouse they claim to come from. A warning ships two artifacts that disagree
> and leaves a person to work out which one to act on — and they can't, because
> nothing in either one says it's the wrong one. Blocking leaves last week's
> exports in place, which are stale and consistent. Stale and consistent is
> recoverable; confidently contradictory is not. Mechanically, `task_parity`
> raises, so the orchestrator marks `publish` SKIPPED and nothing is
> overwritten."

**The tolerance is `1e-6`, not zero,** and be ready for that: float addition is
not associative and the two engines sum 52,160 rows in different orders. A real
disagreement — a wrong filter, a fanned-out join — moves the number by whole
units, not by 1e-9.

**You'll be asked, and you should raise it first:** *"Your parity check passed
while the dashboard was wrong. What does it actually guarantee?"*

This is the best question anyone can ask about this project, and the answer is
the interesting part rather than a defence.

> "It guarantees that two implementations of a definition I registered agree
> with each other. That is all it can guarantee, and for a while I was reading it
> as more than that. The headline 'unused hours' tile on the dashboard was
> computed in `export.py` as `units_unused * 0.25` — a flat quarter-hour per
> unit, which is the exact error the project has a numbered design decision
> against. It understated the figure by 958 hours, and it understated speech by a
> factor of three and medical by two, which are the rows the at-risk list exists
> to surface. The parity report said 'All 11 metrics agree' the entire time, and
> it was telling the truth: the registry computes `hours_unused` from the correct
> per-row column in both engines, and both were right. The tile was a separate
> calculation nobody had registered. The check was attesting to a number nobody
> displayed.
>
> So the lesson isn't 'add a test'. It's that a check verifies the value it
> names, not the value the reader sees, and comparing two definitions to each
> other can never close that gap however many engines you add. What closes it is
> re-deriving the artifact. `metrics.check_published_headlines` reads
> `dashboard_data.json` as published and recomputes five headline figures in SQL
> straight from the warehouse, in `task_verify`, after the files are on disk, and
> raises on a disagreement. Same principle as the byte-level PHI scan: check the
> artifact, not the plan.
>
> And it covers seven figures — pace itself only since a sabotage published
> 76.0% for 75.1% unchallenged. The at-risk table, the per-payer and per-discipline
> breakdowns and the assumption spread are still computed in `export.py` by
> routes nothing re-derives, so the class of defect is open below the tiles. I'd
> rather say that than imply the hole is closed."

**A second illustration of the same boundary, smaller and cleaner.**
`sql/analytics.sql` query 1 computed `uom_coverage` as `AVG(f.uom_resolved)`
over every session, and returned **0.9209** for 2026-04.
`analytics.coverage_by_month` — which is what `check_coverage_step_change` gates
on and what the quality report prints — filters to completed sessions and
returned **0.9204**. Same name, same month, two published artifacts, two
numbers, and nothing anywhere saying which one a reader should act on. The
filter belongs in both: a cancelled session has no duration to resolve, so
including cancellations dilutes the rate towards the cancellation rate and the
number stops meaning "how much of the delivered care can we measure". The SQL
carries the filter now and both return 0.9204.

The honest observation is not that it was fixed. It is that `uom_coverage` is
**not in the metric registry**, so the parity harness never looked at it — the
registry protects the eleven metrics it contains and is silent about every other
number the project publishes. That is the same lesson as INC-005 arriving from a
different direction: the guarantee covers what it names.

Two details worth having ready if they push. The headline tolerance is 0.5
absolute rather than 1e-6, because the payload rounds to one decimal before it is
written and anything tighter fails on the rounding — it was 0.05 for one run and
57,763.75 against a published 57,763.8 tripped it. And the check runs in `verify`
rather than in `parity`, which means it cannot block the publish that produced
the file; it raises after, so the run fails and the wrong artifact is on disk
with a failed build attached to it. Blocking earlier would mean building the
payload twice.

---

### `export.py` and `bi/measures.dax` — reporting

**The null rule.** CSVs write empty fields, not `NULL` or `0`, so Power BI reads
BLANK. A null coerced to zero makes a rate measure lie: zero is a value and BLANK
is not, and averaging over the difference changes the answer.

**`json_safe`.** Python writes a bare `NaN` for `float('nan')`, which is invalid
JSON and which the browser rejects. This was a real bug — the dashboard rendered
blank until it was fixed. `allow_nan=False` now makes it fail loudly at the
boundary rather than silently three steps later.

**In the DAX, two things to be able to explain:**

1. **Rates are measures, never calculated columns.** A calculated column
   evaluates per row and then gets averaged by the visual, giving every
   authorisation equal weight. A measure evaluates over what the visual is
   showing, so numerator and denominator are summed first and divided once.
2. **`DIVIDE()` not `/`.** The operator returns Infinity on a zero denominator
   and that renders as a number. `DIVIDE` returns BLANK.

**And the one that shows depth:** `Authorizations Expiring In Period` uses
`USERELATIONSHIP`. `fact_authorization` points at `dim_date` twice — period start
and period end. Power BI allows one active path. Without `USERELATIONSHIP` the
measure silently answers a different question (authorisations *starting* in the
period) with a plausible-looking number.

---

### `phi.py` — the egress boundary

**What it does.** Classifies every column of every published frame, scans
outgoing values for identifier shapes, pseudonymises direct identifiers, and
blocks publication if anything identifying would cross.

**The four things to be able to say:**

1. **Unknown fails closed.** A column nobody has classified is treated as a
   direct identifier. If a source system adds `guardian_email` tomorrow, the
   publish stops until somebody looks at it. Defaulting to safe would let it
   through and the mistake would only be visible after the file was sent.

2. **The gate verifies, it does not trust.** It does not check that
   de-identification was configured; it reads the published values and confirms
   they match the surrogate format. A new export path that skips the transform
   is caught rather than waved through. Same principle as everything else here:
   check the artifact, not the plan.

3. **Two layers, deliberately different in kind.** The classification says what
   a column is *meant* to hold. The scanner reads what it *actually* holds.
   The gap between those two is where incidents live — a notes field that
   starts carrying a parent's phone number.

4. **A third layer reads the bytes.** Both layers above inspect a DataFrame on
   its way to a file, which leaves a gap wherever something reaches a file
   without passing through a frame the gate was handed. `scan_published_artifacts`
   gives up on knowing what a file *means* and reads it as text:
   the eight BI CSVs, `dashboard_data.json`, `relationships.md`, the quality
   report in both formats, the digest, the JSON-lines run log, and
   `dashboard.html`. A finding raises, cannot be acknowledged, and deletes the
   offending files.

   **Know the list and its two holes, because "every published file" is the
   claim and it is not quite what the code does.** `metric_parity.md`,
   `data_diff.md` and `benchmark.md` are written to the same directory and are
   not on the list. And two files on the list are read one run late.
   `dashboard.html` is rendered by `scripts/build_dashboard.py` after the
   pipeline process exits; `pipeline_runs.jsonl` is flushed by the orchestrator
   after the task loop ends, which is after `verify` has already read it. So
   what `verify` opens in each case is the previous run's copy, and on a fresh
   clone there is nothing to open — the run prints the consequence, reporting
   15 artifacts on a cold `make clean && make run`, 16 on the next `make run`,
   and 17 once `dashboard.html` exists. The JSON payload the dashboard is built
   from *is* scanned in the same run, so the data path is covered and the
   rendered HTML is covered on the next build. If they ask how you'd fix it:
   either move the render inside the pipeline and flush the log before `verify`
   so both are current, or make `verify` fail on a `dashboard.html` older than
   the payload. The second is smaller and catches the fresh-clone case too.

**You'll be asked:** *"How do you know your PHI boundary works?"* — Do not answer
with the design. Answer with the test.

> "Because a test disables it and checks the other layer still catches the leak.
> `tests/test_phi.py::TestDefenceInDepth` monkeypatches out the sample
> redaction — the innermost protection — runs the real pipeline, and asserts the
> `verify` task fails with a PHI egress error. If that test ever passes quietly,
> the two layers have collapsed into one with a fallback and the redundancy is
> imaginary. Defence in depth is only defence if you've proven the layers are
> independent, and the way you prove it is to break one on purpose."

**On pseudonymisation, if they push — and volunteer this, because the naive
version of it is wrong.** HMAC-SHA256 with a salt from
`HOURGLASS_PSEUDONYM_SALT`. Do **not** say "one-way, so the surrogate is useless
without the salt". HMAC is one-way over an unbounded input space, and a client
identifier is not one:

> "The identifiers here run `CLI-00001` to `CLI-00240`, and someone who knows
> only the zero-padded five-digit shape has 10^5 candidates. Either way it is
> seconds of hashing: anyone with the salt can enumerate the space and invert
> every published surrogate by lookup. So the surrogate is exactly as strong as the salt is secret and no
> stronger — it is pseudonymisation, not anonymisation, and I'd rather say that
> than let the crypto word do work it can't do. This isn't hypothetical here: an
> earlier version shipped a constant as the default salt, and a reviewer
> recovered all 240 published surrogates with a rainbow table over the identifier
> space."

The fix, and the trade it makes, is the part worth defending:

> "There's no default salt now. If the variable is unset, `phi._salt` mints a
> random 32-byte salt for the life of the process and never writes it anywhere —
> nothing can be precomputed against a value that doesn't exist until the run
> starts and doesn't survive it. What that costs is real: surrogates are stable
> within a run and unlinkable between runs, so two builds' exports can't be
> joined and you can't track a child week over week outside the boundary until
> somebody configures a salt. Inside the boundary nothing changes, because the
> warehouse stores the raw identifiers. Unlinkable-but-useless beats
> stable-but-reversible, so that's the default."

`quality.check_pseudonym_salt` reports which of three states a build was in, and
it reads the *source* of the salt rather than the salt, so deciding whether a
build is safe never involves handling the secret. A configured secret passes. The
old constant is a BLOCK. An ephemeral salt is a WARN — the severity is not
constant, deliberately, because what an ephemeral salt costs is utility and not
anyone's privacy, and a fresh clone should still run end to end with a report
saying on its face that its exports are not comparable to last week's. None of
the three is acknowledgeable: a written reason cannot make a reversible pseudonym
irreversible.

Direct identifiers are pseudonymised rather than dropped because an at-risk list
nobody can trace back to a child is not an actionable list, it is a statistic.

**The story worth telling.** The gate originally inspected only the eight
warehouse tables. The dashboard published raw client identifiers and the gate
reported clean, because the at-risk frame was not enumerated. A boundary that
covers most of the exits is not a boundary. The fix was one line of
classification and one line of wiring; the lesson was that "we check the
exports" has to mean *all* of them, and there is now a regression test that
would fail if the dashboard ever leaked again.

**You'll be asked:** *"Is this HIPAA compliant?"* — Be careful here, and the
careful answer is the strong one. "It implements the Safe Harbor identifier
list as a check, and it enforces a boundary rather than relying on discipline.
Whether a deployment is compliant is a determination about an organisation, its
BAAs and its whole environment — not about one repository. What I can say is
that the design makes the common failure structurally hard rather than
prohibited."

---

### `disclosure.py` — small-cell suppression

**What it does.** Implements CMS's cell size suppression policy: no cell with a
value of **1 to 10** may be reported, zero is fine, and no cell may be published
that lets a 1-to-10 value be *derived*.

**Where it is applied, and say this exactly rather than "every count".**
`digest.py` and the org-wide child count in `export.py` are the only callers
in `src/`. The remaining panels, the BI CSVs and the quality report publish
their counts unsuppressed. That is defensible at the
grain they use — 240 children and 1,825 authorisations for the whole population
are not disclosive — and it is defensible by argument, not by enforcement. Add a
per-centre panel to the dashboard tomorrow and nothing routes it through
`disclosure` unless whoever adds it remembers. It is a control at a call site
rather than at a chokepoint, which is the shape of INC-001, and it is open.

**The second clause is what makes it harder than a filter,** and it has three
consequences you should be able to state without notes:

1. **Derived columns go with their numerator.** Publishing "12.5% of 24" recovers
   the count. A suppressed count suppresses its own percentage, and the
   denominator too where it is small enough to invert.
2. **Complementary suppression.** One hidden cell beside a published row total is
   recoverable by subtraction, so a second cell has to go — conventionally the
   next smallest, because it costs the least information. This is the part
   usually missing from implementations that claim to do suppression.
3. **One complementary suppression is enough, and no more are needed.** Say this
   explicitly, because the opposite is widely claimed and is not true of these
   tables. They are one-dimensional — a list of cells and one published total,
   with no second margin for a suppression to propagate along — and a single
   equation cannot be solved for two unknowns, so once a second cell is hidden
   the table is safe and hiding a third protects nothing further. The grouped
   path does not change it either: each group is its own one-dimensional table
   with its own subtotal, resolved independently. The implementation therefore
   does not iterate. Volunteer why the docstring says so at that length: an
   earlier version of the module claimed it did iterate, while its loop always
   exited after the first pass.

**You'll be asked:** *"Why suppress small cells if the data is synthetic?"*

> "Because nothing here protects anyone, and that is exactly why it has to be
> written now. The suppression is a habit in the code, not a reaction to a
> particular dataset — a pipeline that only protects real data protects nothing
> on the day the data becomes real, because that is the day somebody is under
> deadline and nobody remembers the report was built without it. It also costs
> almost nothing to have: it is under two hundred lines and one call in the
> digest. And the shape of the disclosure is genuinely there in the synthetic
> data — a centre serving twenty children, sliced by service and week, produces
> cells of two and three. Pseudonymising the identifier does not help; the
> disclosure is in the count."

**State the limit before they find it.** This is a greedy pass over a single
margin: one grouping dimension plus a total. Full linear-programming cell
suppression is the rigorous solution for multi-dimensional tables with margins
in several directions, it is a genuinely hard optimisation problem, and what is
here is not equivalent to it.

**The mutation-testing finding, if the conversation gets that far.**
`suppress_grouped` passes `derived_columns` through to `suppress_counts`, and
`docs/MUTATION.md` records that deleting that argument leaves the test suite
green — the grouped path has no test that passes derived columns, so the privacy
rule is enforced on the one code path a test happened to take. `suppress_grouped`
has no caller in `src/` today, so it is an untested public function rather than a
live leak. That is the most useful thing mutation testing turned up here, and it
is worth volunteering: a passing test about a privacy rule creates the impression
the rule is enforced everywhere.

---

### `orchestration.py` — the task runner

**What it does.** Topological ordering with cycle detection, per-task retries
with exponential backoff, failure isolation, per-task timing, and a JSON-lines
run log.

**Why not just call the functions in order.** Four things the shape buys:
dependencies are declared rather than implied, so reordering cannot silently
break it; retries live where the flakiness is; a failure marks its dependents
SKIPPED instead of running them against missing inputs; and every step is timed,
so "the pipeline got slower" is answerable from data.

**The detail worth volunteering:** only `land` and `read_lake` have retries.
Retrying a pure transform that just failed only fails more slowly, and a global
retry policy is either wrong for the network calls or wrong for the transforms.

**The graph is twelve tasks:** extract → land → read_lake → conform → analyse →
protect → quality → load → diff and parity in parallel → publish → verify. The
cycle check runs at construction, so a reordering mistake is a `ValueError`
before anything executes rather than a confusing failure half way through.

**You'll be asked:** *"Why write your own orchestrator instead of using
Airflow?"* — Answer it honestly and then name the switch point, because the
second half is what makes the first half credible.

> "Because this runs in one process, in order, on one machine, and everything
> Airflow is actually worth its operational cost for — a scheduler, distributed
> execution, backfill windows, cross-run concurrency control, a UI other people
> depend on — I don't have. What I wanted is the part I do need at this size:
> declared dependencies so reordering the file can't silently break it, retries
> placed where the flakiness is, failure isolation, and per-task timing. That is
> about two hundred and seventy lines.
>
> I'd switch the first time any one of four things is true: the pipeline needs to
> run on a schedule somebody other than me depends on; a backfill has to be
> re-run over a date range; two runs can overlap and need locking; or a task
> needs to execute somewhere other than this process. Any one of those, and I'm
> reimplementing Airflow badly. None of them is true here, so extending this
> would be the mistake, not replacing it."

**The detail that shows you mean it:** the honest limit is written in the
module's own docstring, not just in an interview answer.

---

### `sources.py` — the payer API client, and nothing calls it

**Say this before anything else, because it is the first question.** Nothing in
`src/` or `scripts/` imports `hourglass.sources`. `grep -rn "sources" src/
scripts/ --include=*.py` returns its own definition and nothing more. The
pipeline's authorisations arrive as a file drop through `ingest.py`, so a
`make run` executes not one line of this module; `tests/test_sources.py` is the
only caller, and it runs the client against an in-process fake server. It is a
demonstrated capability, not a live component, and offering it as part of the
running system is the kind of claim an interviewer checks in thirty seconds.

**What it does.** Cursor pagination, retry with backoff, `Retry-After`
honoured, a page budget, and a completeness check against the total the server
declared.

**Why it exists at all, given that.** A file either arrives or does not. An API
can be *partially* successful — four pages, then a rate limit, then a different
total because somebody wrote a record mid-read. That difference is the whole
engineering problem, and it is the one the file drop upstream of this project
does not pose. Writing the client is how the failure modes get thought about
before there is a real endpoint to think about them against.

**The check to point at:** comparing rows collected against the declared total.
A pagination bug does not raise; it returns a plausible number of rows. A
silently-short authorisation extract makes every utilisation figure downstream
*too high*, which is the direction nobody investigates.

**Also:** cursor rather than offset. Offsets shift under inserts and silently
skip or duplicate rows.

---

### `incremental.py` — watermarks and the lookback window

**What it does.** The other loading mode: read the high-water mark, select rows
above it minus a lookback window, merge by business key, advance the mark.

**The idea people leave out is the lookback.** A strict watermark is wrong and
quietly wrong: records arrive late and get corrected after the fact — a session
entered Friday for Tuesday, a duration fixed on Monday. Their business date is
below the mark, so a strict watermark never sees them again. So each run
re-reads a window *behind* the mark.

**The trade has no clean answer.** Too short and late corrections are lost for
good; too long and every run re-processes unchanged data. Seven days is in
config rather than buried, so changing it is a decision somebody makes on
purpose. There is a test that asserts a correction *outside* the window is
missed — the limitation is documented in an assertion rather than in a comment.

**The property that makes it trustworthy:**
`test_incremental_matches_a_full_reload` loads incrementally, loads fully, and
compares the tables. Without it, incremental loading is an optimisation you hope
is also correct.

**You'll be asked:** *"Why `>=` and not `>` on the watermark?"* — With a
date-grain watermark, `>` drops every row sharing the highest date, and there is
always more than one session on the last day. The merge replaces by key, so the
overlap is harmless; without the `=`, the loss is silent.

---

### `diff.py` — did this run change anything I did not intend

**What it does.** Compares this build against the last published one, per table,
by primary key. Reports rows added, removed and changed, and for the changed
rows, *which columns* changed and how many cells in each.

**Why it exists, and this is the sentence to have ready.** Tests answer "is this
data valid". The diff answers "did this run change anything, and was it what I
meant to change". Both can pass and the second can still be a disaster: every
check green, every row plausible, and 40,000 sessions quietly fifteen minutes
longer because somebody edited a service's `minutes_per_unit`. Nothing is
invalid. Everything is different. A test suite has no opinion about that.

**Two design details worth volunteering:**

- **Key-based, not positional.** Row order is not meaningful in a relational
  table, and a diff that reports every row as changed because the sort order
  moved is a diff nobody opens twice.
- **Per-column attribution.** "412 rows changed" is a shrug. "412 rows changed,
  all in `minutes_delivered`" is a diagnosis.
- **The rule-set hash is compared, not just carried.** The report opens with a
  banner when this run's rules differ from the last published run's, because
  "the numbers changed" and "we changed what the numbers mean" are different
  events that look identical in a row count. Nothing blocks: a policy constant
  is not a data error and the pipeline cannot know which value was intended.
  The banner appears above the table on purpose — a reader who works it out
  three tables down has already formed a wrong theory. INC-006.

**The story.** Its first version reported 49,227 changed rows between two runs of
a pipeline that is deterministic by construction and asserted to be so. Under
pandas 3.0, `astype(str)` leaves `NaN` as `NaN` and `NaN != NaN`, so every null
cell compared unequal to itself — and `fact_session.unresolved_reason` is null on
49,227 of 52,160 rows. The diff was reporting the *healthy* rows as changed. The
fix is a `NULL_SENTINEL` containing a NUL byte, so no real value can collide with
it; a printable sentinel like `"null"` would trade a false positive for a false
negative. INC-002 in `docs/INCIDENTS.md`, and INC-003 is the dtype variant found
while writing the tests for the fix.

**Say why that class matters:** a false positive in a monitoring tool is worse
than no tool, because the damage is done to the reader's attention and fixing the
tool later does not recover it.

**The limit.** Both sides are loaded into memory and merged. Fine at 52,000 rows,
not viable at a hundred million, where the technique is column checksums per key
range — compare ranges, then descend only into the ranges that differ.

---

### `digest.py` — the output for a person

**What it does.** Renders the weekly at-risk list as plain-language Markdown for
a clinical operations coordinator.

**Why it is the most important output.** A dashboard answers questions somebody
already thought to ask, and requires them to open it. Most weeks nobody does,
and the authorisation still expires. The digest arrives.

**The constraints, and they are real:** no jargon (there is a test that fails if
the word "utilisation" appears); grouped by centre because a coordinator owns a
centre, not a payer; numbers attached to decisions; and the data-quality caveat
*next to* the numbers rather than in a footnote — if hours-delivered is
understated, the person about to phone a family needs to know before they imply
somebody missed appointments.

**A bug the tests caught here:** the action list hard-coded "the figures are
understated while the session-length issue is open" even when coverage was
complete. Telling people to distrust numbers after the numbers are fixed trains
them to distrust the numbers. It is conditional now.

**The suppression call is here, not in a config flag.** Per-centre head counts go
through `disclosure.suppress_counts` before they are printed; the hours and the
authorisation count stay, because they are not counts of people.

**And the interesting part is that suppressing the count was not enough.** Have
this ready, because it is a collision between two correct decisions rather than
a bug:

> "Grouping by centre is right — a coordinator owns a centre, so that's who acts
> on the row. Small-cell suppression is right. Put them together and printing
> 'fewer than 11 children' above a table with one row per authorisation withholds
> nothing: the reader counts the distinct client references under '### Temecula'
> and recovers the number the line above just withheld. Suppressing the rows
> instead would protect the families by making the document useless, and a
> document nobody can act on doesn't stop an authorisation expiring.
>
> So a centre below the threshold loses its heading, not its rows. Small centres
> are pooled into one 'Other centres' section, sorted by expiry rather than by
> centre and with no centre column — a pooled table ordered by centre is a
> centre-labelled table with the labels taken off. What's removed is the
> attribution of a small count to a named centre, which is the disclosure. What
> survives is the list of calls to make, which is the point."

Two details that show it was thought through rather than patched. The
single-small-centre case is handled by the same complementary pass that handles
cells: if exactly one centre is below the threshold, the pooled section *is* that
centre and its name follows by elimination from the named sections above it, so
the next-smallest centre is pooled with it — which is why the digest calls
`suppress_counts` on the centre table rather than filtering it by hand. And the
limit is printed rather than hidden: if the pooled count is itself below the
threshold it is withheld too, and the digest says so, because a reader can still
count the pooled references. What they cannot do is attach the figure to a
centre. The section carries a sentence explaining why it exists, because a reader
who meets an unfamiliar heading with no explanation assumes a system fault and
goes looking for the missing sections.

**The headline total had to be suppressed too, and this is the entry to lead
with if they want to see you find your own bugs.** Every named centre publishes
its count. Subtract the named counts from a published organisation-wide total
and what is left is the pooled figure — precisely the number the pooling exists
to withhold. Forty-five children, one named centre of forty, and the pooled
section is five.

> "The first version of this function named the attack in its own closing
> footnote — 'a combined section holding a single centre could be recovered by
> subtracting the named centres from the total' — and then committed it two
> paragraphs above, in the headline. The generalisable point is that
> complementary suppression is not only a rule about cells; it applies at
> whatever granularity publishes both a total and its parts. I had implemented
> it at cell granularity, written the sentence that describes it at section
> granularity, and not noticed they were the same sentence.
>
> The fix is structural rather than a check bolted on the end. `CentrePlan`
> decides the section layout before a word of the digest is written, because the
> headline is not independent of it — deciding what to pool after the headline
> is on the page is how the leak happened. `total_is_recoverable` withholds the
> total whenever the pooled count is withheld, and the sentence gives the real
> reason: a reader told only 'this number is small' about a total of forty-five
> will assume a bug and go looking. The named counts stay, because suppressing
> the total is the least that closes the hole and taking the named counts as
> well would cost the report its usefulness. `TestRecoveryBySubtraction` in
> `tests/test_digest.py` holds each of those separately, including that no row
> disappears from the work list to buy any of it."

---

## The tests, and what a test count does not tell you

461 tests across 15 files. Be able to say what that number is worth, because a
good interviewer will ask, and "461" on its own is a claim about volume.

**Three tests carry more weight than the rest**, and these are the ones to name:

- **Idempotency.** `tests/test_pipeline.py` runs the whole pipeline twice and
  compares a checksum of every table. The run log is the deliberate exception —
  append-only, because the record of what ran should not be erased by a re-run.
- **Incremental equals full reload.** `test_incremental_matches_a_full_reload`.
  Without it, incremental loading is an optimisation you hope is also correct.
- **Defence in depth.** `tests/test_phi.py::TestDefenceInDepth` disables sample
  redaction, runs the real pipeline, and asserts the byte-level `verify` scan
  still fails the build.

**Property-based tests** (`tests/test_properties.py`, Hypothesis). The rest of
the suite encodes cases somebody sat down and thought of; the two most expensive
defects in this project were on nobody's list, and both were *inputs* rather than
logic. A property test states the invariant once — a suppressed table is never
disclosive, a frame diffed against itself is identical, SCD validity ranges never
overlap, exactly one client version is in effect on any date, a surrogate never
contains its identifier — and lets Hypothesis choose the inputs and shrink any
failure to its smallest form.

Two of them were written as the invariant that *ought* to hold rather than
weakened into one that did, and both failed the day they were written. They were
marked `xfail(strict=True)` while they failed, the source was fixed, and they are
ordinary passing regression tests now. There are no `xfail` markers left in the
suite. Volunteer this rather than waiting to be asked, and volunteer it in the
past tense — a reviewer who runs `pytest -rx` looking for two admitted defects and
finds nothing starts doubting everything else you claimed:

- A unit of measure that was present but unrecognised — `'hours'`, `'each'` — was
  correctly refused and then left with a null `unresolved_reason`, because the
  three assignments in `resolve_minutes` covered an empty value, an unmapped
  service code and a non-numeric duration and nothing covered a word the pipeline
  simply did not know. Such a row fell out of `unit_assumption_spread`, which
  selects on `unresolved_reason == "missing_uom"`, so it was counted neither in
  the delivered hours nor in the quantified cost of guessing — the quietest
  possible failure, and the same shape as the defect the module exists to catch.
  Hypothesis shrank it to `service_code="97153", duration_value=10,
  duration_uom="hours"`. `resolve_minutes` now assigns `unrecognised_uom`, with a
  catch-all beneath it so the invariant holds for any input rather than for the
  cases somebody enumerated.
- A row whose primary key was null was reported as both added and removed on
  every run against an identical frame. INC-002's null sentinel had been applied
  to cell *values* and never to the key set, and `set(frame.set_index(keys)
  .index)` on a NaN key is a set of a float that is not equal to itself. Shrunk
  case: a one-row frame keyed on NaN, diffed against a copy of itself, giving
  added=1 and removed=1. The sentinel covers key columns now. A warehouse primary
  key should never be null, which is why it had not bitten — but `diff.py` reads
  whatever `read_sql_query` returns and SQLite does not enforce that, so the
  guarantee was living in a different module from the code depending on it.

**The shape of that is the point, and it is what to say about it.** Both
docstrings still carry the failing case that motivated them, so the reason the
assertion exists survives the fix. A regression test with no record of what it
regressed is a test the next person deletes as redundant.

**You'll be asked:** *"How do you know your tests are telling you the truth?"* —
Answer with hermeticity, because it is the unglamorous one and most candidates
have not thought about it.

> "`make check` used to fail intermittently, and the reason was that the suite
> wrote to the repository's real `data/out/` — the same warehouse a `make run` in
> the same checkout writes, racing on the same atomic rename. A test that is
> reading whatever the last run left is not testing anything.
>
> `tests/conftest.py` points `HOURGLASS_DATA_DIR` at a temporary tree, and it
> does it at conftest *import* time, before pytest imports any test module and
> therefore before anything imports `hourglass`. That ordering is the whole
> mechanism rather than a tidiness preference. `config.DATA` reads the variable
> once at import, and `pipeline.py`, `export.py` and `ingest.py` bind paths from
> it at import and again as frozen default arguments — which cannot be
> monkeypatched afterwards at all. A fixture doing this in setup would have
> redirected some call sites and silently missed the rest, which is worse than
> not redirecting, because then you think you're isolated.
>
> The `workspace` fixture asserts `config.DATA` is the temporary path and fails
> with that explanation if anything imported the package too early. Each test
> gets the tree reset at setup rather than teardown, so a failing test's output
> is still on disk to look at, and the shared published run is snapshotted out of
> the reset area so twelve tests can read the same artifacts without one of them
> being able to write to them."

**State the limit of property testing too:** Hypothesis only explores the input
space it is told about, so a property's blind spots are its strategies' blind
spots, and a property holding over a few hundred generated examples is evidence,
not proof.

**Mutation testing** (`docs/MUTATION.md`) answers the fair question about any
test count: do the tests assert anything, or do they merely execute the code?
Coverage measures reach; mutation testing measures grip. Two of eighteen modules
have been scored:

| Module | Score | Basis |
|---|---|---|
| `disclosure.py` | **97.5%** | 78/80. Both survivors proven equivalent by execution, so 100% of behaviour-changing mutants are killed. Was 79.4% before nine tests were added in response to the previous run |
| `transform.py` | **75.7%** | 482/637 over the mutants the paired test file reaches; 51.2% over all 942 generated |

Give both `transform.py` numbers, because neither alone is honest: the first
discards 305 mutants no test in the paired file touches, the second penalises the
file for mutants it was never the right file to catch. The gap between them is
the finding.

**The improvement is the answer, not the score.** `disclosure.py` was 79.4% on
the first run. Three of the mutants that survived it were real gaps, and all
three were in behaviour a *different* module depends on for a privacy
guarantee — `digest.py` routes its centre-pooling decision on the `suppressed`
flag column that one mutant replaced wholesale with `None`, and every test in
`tests/test_disclosure.py` passed. The tests asserted the masked *values*;
nothing asserted the column another module reads.

That is the whole case for mutation testing in one example, and it is the thing
to say if asked. Coverage was 100% on those lines. The suite was green. A
reviewer reading the file would have seen assertions everywhere. The only thing
that found it was changing the code and watching whether anything objected.

Nine tests later the score is 97.5%, and the two remaining survivors were each
checked by execution rather than by argument: `value is None or pd.isna(value)`
becoming `and` returns the same answer for all fourteen inputs that reach it,
and `groupby(sort=False)` becoming `sort=None` is a pandas synonym — its
`sort=True` sibling *is* killed, which is the evidence that the survivor is a
property of the library and not a hole in the tests.

**The two findings worth reciting:** `_age_band` has no test pinning a single
boundary, so every band edge can move by a year and every label can be replaced
with nonsense without a failure — nine survivors, one parametrised test kills all
nine. And `suppress_grouped` drops `derived_columns` with the suite green, which
is a privacy rule enforced on one code path and absent on the other.

**And the limits, which are in that document rather than glossed:** a high score
does not mean the tests assert the right things — a suite can pin the wrong
constant just as firmly and score 100%. Equivalent mutants put the attainable
ceiling below 100% — on the current `disclosure.py` run both survivors are
proven equivalent, so the recorded 97.5% is that run's ceiling, already met —
and deciding which survivors are equivalent is a manual judgement with no
algorithm behind it. Comparing mutation
scores across projects is therefore meaningless. It is a periodic audit, not a CI
gate, because the full suite against 97 mutants would now take a little over two
hours.

---

## Vocabulary, defined against this repo

| Term | Meaning | Where it is |
|---|---|---|
| **Grain** | What exactly one row represents | Stated in a comment on every table in `star_schema.sql` |
| **Fact / dimension** | Facts are measured; dimensions are what you slice by | `fact_session` vs `dim_client` |
| **Conformed dimension** | One dimension shared by more than one fact table | `dim_client`, `dim_service` serve both facts |
| **Surrogate key** | A meaningless integer PK, so the natural key can change | `client_key` vs `client_id` |
| **SCD Type 2** | New row per change, with validity dates | `build_dim_client` |
| **As-of join** | Join to the version in effect on an event date | `build_fact_session` |
| **Fan-out** | Rows multiplied by a careless join | `analytics.sql` query 3 |
| **Idempotent** | Running twice = running once | `atomic_build`, asserted in tests |
| **Partition** | Physical split of data by a key | `source=` / `ingest_date=` in the lake |
| **Data quality gate** | A check that can stop a release | `quality.py` |
| **Silent failure** | Wrong data, no error | The whole of `ANOMALY.md` |
| **Watermark** | Highest source value already loaded | `incremental.Watermark` |
| **Lookback window** | Re-reading behind the watermark for late data | `Watermark.read_from` |
| **Merge / upsert** | Replace by key rather than append | `incremental.merge_rows` |
| **Pseudonymisation** | Surrogate for an identifier, reversible by a salt holder and by nobody else | `phi.pseudonymise` |
| **Egress boundary** | The line published data crosses | `phi.check_egress` |
| **Quasi-identifier** | Not identifying alone; identifying combined | `phi.Sensitivity` |
| **Topological sort** | Ordering a graph so dependencies come first | `orchestration.toposort` |
| **Cursor pagination** | Following the server's pointer, not an offset | `sources.fetch_all` |
| **Cell suppression** | Withholding counts small enough to identify someone | `disclosure.suppress_counts` |
| **Complementary suppression** | A second cell hidden so the first is not recoverable by subtraction | `disclosure.suppress_counts` |
| **Semantic layer** | One definition of a metric, shared by every engine | `metrics.REGISTRY` |
| **Metric parity** | The same number computed two ways and compared | `metrics.check_parity` |
| **Column contract** | A static check that a measure exists and reads the right columns | `metrics.check_dax_contract` |
| **Value-level diff** | Run-over-run comparison by key, not by row order | `diff.diff_warehouses` |
| **Property-based test** | State the invariant, let the tool choose inputs | `tests/test_properties.py` |
| **Mutation testing** | Change the code, see whether a test notices | `docs/MUTATION.md` |

---

## Questions you will get, ranked by likelihood

**1. "Walk me through what this does."**
Ninety seconds, and lead with the problem, not the architecture: *"Authorised
therapy hours expire. When they expire unused a child got less care than their
plan approved and the provider wasn't paid for care it could have delivered.
Nobody in the treatment room can see it because it only shows up when the
authorisation system and the scheduling system are read together. So the
pipeline reads them together and produces a weekly list of authorisations
expiring in the next thirty days with hours still on them."*

**2. "Why two fact tables?"**
Different grains. Sessions are events; authorisations are permissions spanning a
window — which is why the authorisation has two date keys and not one. Then the
55× fan-out demonstration.

**3. "How do you know the data is right?"**
You don't, entirely — so you check, and you publish what you checked. Seventeen
gates, three severities, blocking failures halt publication, releases are
recorded with a written reason and a rule-set hash. Then the second half, which
is the part most answers miss: the gates check whether the data is *valid*, and
three other mechanisms check whether the *run* did what it was supposed to. The
diff says what moved since the last published build. The parity check recomputes
every registered metric in SQL and refuses to publish if it disagrees with the
pandas that produced the dashboard. And the headline check re-derives what was
actually written to `dashboard_data.json` from the warehouse, because the first
two compare definitions to each other and neither can see a figure computed by a
route nobody registered.

**4. "Tell me about a bug you found."**
The unit-of-measure change. Say the shape: no error, no failing test, plausible
number, caught by a coverage check, localised to one month by a step-change
check. Then the punchline — *both* plausible guesses were defensible and they
differed by a factor of fifteen, so the fix was to refuse to guess and publish
the coverage instead.

**5. "What would you do differently at 100× the volume?"**
Lead with what the benchmark actually shows, and resist the temptation to name a
stage as the bottleneck, because the numbers do not support one. Across 1×, 4×
and 12× — 52,160 to 617,851 session rows — most stages come out below 1.00×
per-row cost and the top of the column has never exceeded 1.15×. Nothing in that
timing table predicts a wall inside the range.

**Open `benchmark.md` rather than quoting a remembered figure**, and say why you
are doing that: the report derives its own conclusion from its own table —
above 1.05× it names the worst stage and calls it the one to rewrite first, at
or below it says no stage degrades faster than the data grows — and which
branch it prints depends on the run. The README deliberately reproduces no
table for the same reason. A document restating a measurement it does not
compute will eventually be wrong, which is the whole of INC-005.

Then undercut your own number before they do: **the top of that column moves
between runs.** One run of the same script on the same data put `load` at the
top at 1.15×; another put it at 1.01×, flat and not at the top at all; a third
put `parity` top with `load` a hundredth behind. A stage that changes places is
not a stage that is degrading. This is a shared machine, each scale is measured
once, and a stage costing four tenths of a second at 1× is timed near the noise
floor. Single-sample timings on contended hardware are
evidence about shape, not measurements of a constant. Saying that first is worth
more than defending a figure that will not reproduce, and the fix — repeated
trials and a median — is a sentence long.

What does hold across the range is that `diff`, `parity` and `verify` are 44% of
wall clock at 1× and 45% at 12×: nearly half the run is spent checking the work,
because each re-reads the whole build. That ratio is stable where the per-stage
growth column is not, which is itself the reason to quote it instead. It is the
cost of the guarantees, and it is worth stating as a figure rather than being
caught by it.

So the thing that expires first probably expires on **memory, not time**.
`conform`, `analyse` and `protect` hold whole frames and `diff` holds two builds
at once, and none of that shows up in a timing table until the machine runs out
of RAM, at which point the curve is not gradual. Say "probably", and say why:
there is no memory profile in this repository. That claim is an argument from
what the code holds, not a measurement, and the difference matters in a room
where the other claims are measured. Full reload becomes a merge with a
watermark — `hourglass.incremental` already implements it — for footprint
reasons rather than because anything was measured slowing down. SQLite becomes a
real warehouse: the dimensional model transfers unchanged, the loader doesn't.
`pipeline.py` becomes Airflow tasks so retries and backfills are real. The diff
stops loading both sides into memory and becomes column checksums per key range.
And the quality checks run on a sample before the full load, because at that size
you want to fail before you spend the compute.

If they press on the benchmark's own limits: it runs each scale against the build
the previous scale published, so the `diff` timings at 4× and 12× compare
warehouses of different sizes and are not like-for-like, and a run started
against an empty data directory has no previous build at 1×, which makes `diff`
0.00s and its growth column a division by nearly zero.

**Worth volunteering.** The "What this means" paragraph at the foot of
`data/out/reports/benchmark.md` used to be a fixed string in
`scripts/benchmark.py` asserting that the full reload was the decision that
expires first — re-emitted on every run whatever the timings came out at, and
for several runs the measurements disagreed with it. It now reads the
worst-growing stage off the table it is printed under, and says so explicitly
when nothing degrades.

It is a small fix and a good thing to raise unprompted, because it is the same
class INC-005 is about: a generated artifact restating a conclusion nothing
re-derived. The general form is that any sentence in a report which is not a
function of the data in that report will eventually be false, and the only
question is whether anyone notices before a reader acts on it.

**6. "What would you do differently with a team?"**
This is the honest one, and it is the reason for wanting a junior role. Three
things change and none of them are technical:

- **Every convention here is mine and nothing has been through review.** The
  severity taxonomy, the acknowledgement mechanism, the decision that unknown
  fails closed — all defensible, none argued with. The first thing a team gives
  you is somebody who disagrees before the code ships rather than after.
- **The incident log and the mutation report would be shared work.** Both exist
  here because one person read the output and disbelieved it. On a team that is a
  rota and a review, not a mood.
- **Metric definitions would move out of a Python file.** The registry is the
  right idea at the wrong altitude: with more than one analyst, the definitions
  belong somewhere non-engineers can read and propose changes to — dbt metrics or
  an equivalent semantic layer — with the parity check as the enforcement
  underneath it.

Say plainly that you have never written code inside a codebase someone else
designed, or taken review from a senior engineer. Then say the adjacent thing you
have done: thirteen tagged releases on your own deployment, with CI gates you
wrote after three releases broke in ways you should have caught.

**7. "What's wrong with it?"**
Have three ready, from the README's limitations. Volunteering them is stronger
than being caught by them. The strongest three to lead with: the DAX is checked
by contract and not executed; cell suppression is a greedy pass over a single
margin and not the linear-programming solution; mutation testing has scored two
of eighteen modules and says nothing about `phi.py` or `quality.py`. A fourth
if the conversation reaches the module list: `sources.py` has no caller outside
its own tests.

**8. "How do you know it works on a machine that isn't yours?"**
I didn't, until somebody ran it on one and it broke. Say that first. It is a
better answer than any claim of thoroughness, because the claim of thoroughness
is the thing that turned out to be wrong.

> "`make check` on a clean MacBook — no `~/.aws`, no `AWS_*` variables — came
> back 455 passed, 1 failed. The S3 layer falls back to a local filesystem
> mirror when S3 isn't reachable, so a reviewer with no Docker and no AWS
> account can still run the whole thing. On a machine with no credentials at
> all it raised instead, because botocore signs a request before it opens a
> socket: with nothing to sign with it raises at signing and never reaches the
> connection error the fallback was written to catch. Any credentials at all,
> including rejected ones, sign fine, fail to connect and fall back correctly.
> So the fallback that exists for a reviewer without AWS failed for exactly
> that reviewer and for nobody else."

**Then the part that makes it worth telling.** That defect had survived three
adversarial reads of the source, the full test suite and a mutation-testing run,
every one of which had executed in a single environment that happened to have
`AWS_ACCESS_KEY_ID` set. The test was there, it was aimed at the right property,
and it asserted the right thing — its outcome was simply decided by the host
rather than by the code. The fix pins the credential environment inside the
tests, so the answer is a function of the code. Name the limit in the same
breath: that closes one class of environment variable, and `HOME`, `TZ`, `LANG`,
locale formatting, an installed binary and the clock are all still ambient.

**Connect it to the rest of the document rather than leaving it as an anecdote.**
Everything here argues for checking the artifact instead of the intention — the
egress scan reads published bytes, the parity check recomputes from the
warehouse, the headline check re-derives the tile. The one thing nobody checked
was the environment the checks themselves ran in. A green tick is a statement
about one machine, and this suite was telling the truth about the only machine
it had ever been on.

The honest framing, if they push: the underlying question is not whether this
project is correct. It is what you do when you are the only person checking, and
the answer that survived contact was to stop being the only person — get it onto
a machine you do not control and run the checks there. INC-007 in
`docs/INCIDENTS.md`.

**9. "How much of this did the AI write?"**
Answered at the top of this document.

---

## What you cannot defend yet, and should not pretend to

Say these plainly if they come up. Each is followed by the nearest thing you
*have* done, which is the pattern that reads as honest rather than evasive.

- **Running this in production on AWS.** You have built against the S3 API and
  tested it with moto and LocalStack. You have not operated it. What you have
  operated is your own deployment on Fly.io and Cloudflare R2 with GitHub
  Actions and thirteen tagged releases.
- **dbt, Airflow, Snowflake, Spark.** None of them are in here. What is in here
  is the modelling those tools orchestrate, done by hand, which means you know
  what they would be doing for you.
- **Working in a codebase someone else designed.** This is your design, start to
  finish. It is the honest reason you want a junior role, and this posting says
  "under the guidance of senior engineers" — which is the thing you are actually
  looking for. Say that.
- **Power BI at depth.** Twenty-seven measures are written — that is named
  measure definitions, and be ready to say how you counted, because two other
  numbers are easy to reach. `metrics.parse_measures` returns 29: its header
  pattern takes any unindented line ending in a bare `=`, which `VAR
  LastSessionKey =` and `VAR LastSessionDate =` inside `As Of Date` both are. A
  naive `grep -cE '^\w.* ='` returns 32, picking up three more top-level `VAR`
  lines whose value is on the same line. Five `VAR` lines, twenty-seven
  measures — and the model is
  specified down to the nine relationships and which one stays inactive, but
  Power BI Desktop is Windows-only and you are on a Mac. No measure in that file
  has ever been evaluated by a DAX engine. The parity check confirms each metric
  has a measure of the right name referencing the right base columns, and that is
  a contract, not a verification. You have built the semantic layer, not the
  report. Do not imply otherwise, and say it before they ask.
- **Running the pipeline for anyone but yourself.** No scheduler, no on-call, no
  consumer who noticed when it broke. The seven entries in `docs/INCIDENTS.md`
  borrow the practice of writing incidents up; they do not claim to have run an
  incident. That distinction is in the document and it should be in your answer.
  INC-007 is the closest thing to an exception and it cuts the other way: the
  first time this ran on hardware that was not yours, it failed — which is what
  a second person is for, and why you are not claiming to have had one.

---

## Before you send it

- [ ] Run `make gate` and watch it fail. Understand the exit code.
- [ ] Run `make run` and watch it publish. Read the acknowledgement in the log.
- [ ] Run `make test` and know roughly what the 461 tests cover.
- [ ] Run `python scripts/run_analytics.py --only 3` and be able to explain the
      55× number without notes.
- [ ] Open `dashboard.html` and be able to say what each panel is for.
- [ ] Read `data/out/reports/metric_parity.md` and be able to say, in one
      sentence, which two engines were executed and which one was not.
- [ ] Read `docs/ANOMALY.md` twice. It is the story you will tell most often.
- [ ] Read `docs/INCIDENTS.md` once. INC-004 is the second story, and it is the
      one that shows a check finding something its author was not looking for.
      INC-007 is the third, and it is the one you did not find yourself.
- [ ] Run `make check` on a machine you did not build this on — a borrowed
      laptop, a fresh container, anything with no `~/.aws`. That is how INC-007
      was found, it costs ten minutes, and it is the only check here that no
      part of this repository can perform for you.
- [ ] Pick three limitations from the README and be able to raise them yourself.

If you can do those ten things, you can defend this. If you cannot do them
yet, do not send it — a project you cannot discuss is worse than no project,
because it converts an interview from a conversation into an exam.
