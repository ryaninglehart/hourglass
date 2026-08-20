# The defect, and how the pipeline finds it

A silent data-quality failure is seeded into the generated EHR extract on
purpose. It throws no exception, fails no test, and leaves the headline number
looking entirely reasonable. This is a write-up of what it is, how the pipeline
catches it, and why it is handled the way it is.

---

## What happens

The EHR reports how long a session lasted in a column called `duration_value`,
with a companion column `duration_uom` saying what the number means.

Before **2026-04-01** the value is **minutes**:

```
session_id,service_code,service_date,duration_value,duration_uom
SES-0012938,97153,2026-03-11,180,minutes
```

On and after that date the vendor changed it to **15-minute units**, which is
how ABA services are actually billed:

```
SES-0044102,97153,2026-05-04,12,units
```

Both are fine. `180 minutes` and `12 units` are the same three-hour session --
for this service code. Note the qualifier: 97153 bills in 15-minute units, but
92507 is a 45-minute session and 99213 is 30. The conversion factor lives in
`dim_service`, which is why the resolver takes the service code as well as the
unit flag.

The defect is that roughly 8% of the post-change rows arrive with the unit flag
**empty**:

```
SES-0044117,97153,2026-05-06,10,
```

That row is not slightly wrong. It is unrecoverable. `10` is either ten minutes
or ten 15-minute units — ten minutes or two and a half hours. Nothing else in
the row distinguishes them.

---

## Why it is dangerous rather than merely annoying

There are exactly two reasonable things to assume, and **both are defensible in
isolation**:

- *minutes*, because that is what the column meant for the first three months;
- *units*, because that is what it means now.

They differ by a factor of fifteen on every affected row. Neither raises an
error. Neither produces an obviously silly dashboard. On this dataset:

| Approach | Hours delivered | What you would conclude |
|---|---|---|
| Assume **minutes** | 104,891 | Delivery is down. Start chasing schedulers. |
| Assume **units** | 110,740 | Delivery is fine. Nothing to do. |
| **Exclude, publish coverage** | **104,480 over 94.4% of sessions** | Delivery is down *and* 5.6% of the data is unusable. Two problems, both real. |

The spread between the two guesses is **5,849 hours**, about 5.6% of delivered
volume. That is the cost of picking one and not writing it down.

This is the shape of failure worth designing against: not the one that crashes,
but the one that returns a plausible number and lets somebody act on it.

---

## How the pipeline catches it

Two checks fire, and they do different jobs.

### `uom_resolution_coverage` — severity BLOCK

Counts rows whose unit cannot be determined and blocks publication when coverage
falls below 99%.

```
2,933 of 52,160 sessions have a duration whose unit of measure cannot be
determined. Their duration is not recoverable, so they are excluded from all
measures. Utilisation computed over the remaining 94.4% is correct but
incomplete.
```

BLOCK is the right severity because a utilisation percentage computed over
unknown units is not a slightly-wrong number — it is a meaningless one. Compare
`sessions_without_authorization`, which is only a WARN: there the number is
correct and it is the business situation that is bad.

### `uom_coverage_step_change` — severity WARN

Walks coverage month over month and reports the largest drop.

```
Unit-of-measure coverage fell 7.9% between 2026-03 (99.9%) and 2026-04 (92.0%)
and did not recover in any later month. A step that persists is a source
change, not noise: the defect starts in 2026-04 and every month before it
clears the 99% floor.
```

One thing about that sentence is wrong and it is the check's own wording, quoted
here as it is emitted rather than tidied. The fall from 99.9% to 92.0% is 7.9
**percentage points**, not 7.9%; `check_coverage_step_change` formats a
difference of two rates with a percent sign. It happens to read correctly here
only by coincidence — 7.91 points off a 99.95% base is also a 7.91% relative
fall — and the coincidence will not survive the first month whose base is not
close to 100%.

**This is the check that earns its keep.** The first one tells you 5.6% of your
data is unusable, which is an open-ended cleanup of unknown size. The second
localises it to a single month, which turns it into one question for one vendor
about one release — and tells you the three months before it are clean and can
be trusted.

A step change in a data-quality metric that does not recover is almost never
noise and almost always a release.

---

## Why the rows are kept rather than dropped

Unresolvable sessions stay in `fact_session` with `uom_resolved = 0` and zeroed
measures. Dropping them would have been tidier and worse:

- the size of the hole stays queryable, so `SELECT ... WHERE uom_resolved = 0`
  answers "how bad is it, and where";
- session **counts** remain correct even though session **durations** are not,
  and those are different questions;
- when the vendor back-fills, the rows are already there to update.

The general principle: a measure that admits a hole beats a measure that fills
one with a guess, because the hole is visible and the guess is not.

---

## What happens next in real life

The blocking failure is released with a written reason that goes into the run
log alongside the rule-set hash:

```bash
python -m hourglass.pipeline --acknowledge \
  "uom_resolution_coverage=Ticket DE-412. Vendor confirmed the 2026-04-01 field \
   change and is back-filling the missing flag. Publishing with coverage stamped \
   on the report; 2,933 sessions quarantined."
```

The dashboard then carries the coverage figure next to the utilisation figure,
so nobody quotes one without the other. The `Utilization Label` measure in
`bi/measures.dax` does the same thing in Power BI — it appends
`(over 94.4% of sessions)` to the percentage automatically whenever coverage
drops below 99%, so the caveat travels with the number instead of living in a
footnote somebody deletes.

And the fix is a source fix, not a pipeline fix. The pipeline's job was to
refuse to publish a number it could not stand behind, say precisely how much
data was affected, and point at the month it started.

---

## Reproducing it

The generator is deterministic — same seed, same bytes, asserted in
`tests/test_pipeline.py::TestGenerator::test_is_deterministic`.

```bash
make gate                              # blocks, exits 1
python scripts/run_analytics.py --only 4   # where the measure is thin, by month
```

The defect rate is `null_uom_rate` in `config.GeneratorConfig`, currently `0.08`
— small enough to be missed by eyeball, large enough to move the headline
metric. Set it to `0.0` and the gate passes.
