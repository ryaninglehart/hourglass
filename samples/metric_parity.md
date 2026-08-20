# Metric parity

**All 11 metrics agree.** Each one was computed twice over this build — once by the SQL in this registry and once by the pandas that produced the dashboard — and the two results matched.

All 10 DAX measures with declared columns are present and reference each of them.

## SQL vs pandas

| Metric | SQL | pandas | Difference | |
|---|---:|---:|---:|:--|
| Units Authorized | 710,734.0000 | 710,734.0000 | 0.000000 | ✓ |
| Units Delivered (Completed) | 411,030.0000 | 411,030.0000 | 0.000000 | ✓ |
| Hours Delivered | 104,480.5000 | 104,480.5000 | 0.000000 | ✓ |
| Session Count | 52,160.0000 | 52,160.0000 | 0.000000 | ✓ |
| Authorization Count | 1,825.0000 | 1,825.0000 | 0.000000 | ✓ |
| Children Served | 240.0000 | 240.0000 | 0.000000 | ✓ |
| Units Delivered In Period | 410,405.0000 | 410,405.0000 | 0.000000 | ✓ |
| Units Unused | 300,396.0000 | 300,396.0000 | 0.000000 | ✓ |
| Hours Unused | 76,362.5000 | 76,362.5000 | 0.000000 | ✓ |
| Hours Authorized | 180,587.5000 | 180,587.5000 | 0.000000 | ✓ |
| Authorization Utilization | 0.5774 | 0.5774 | 0.000000 | ✓ |

## DAX: existence and column references

DAX is not executed here — there is no DAX engine in CI. What is checked is that each measure of the declared name exists and that its body mentions the base columns the metric is defined on. It is a substring test, so it catches a measure that was deleted, renamed, or pointed at the wrong column, and it catches nothing else: a measure that names a column and then ignores it passes.

Where the unreferenced column is a filter — `is_completed`, `uom_resolved` — the measure still returns the right number today, because `transform.py` zeroes the rows those filters would remove. The number is correct and the dependency is on an invariant in another module rather than in the measure. That is why it is listed here rather than assumed.

| Measure | Present | Columns |
|---|:--:|:--|
| Units Authorized | ✓ | ✓ |
| Units Delivered (Completed) | ✓ | ✓ |
| Hours Delivered | ✓ | ✓ |
| Session Count | ✓ | ✓ |
| Authorization Count | ✓ | ✓ |
| Children Served | ✓ | ✓ |
| Units Unused | ✓ | ✓ |
| Hours Unused | ✓ | ✓ |
| Hours Authorized | ✓ | ✓ |
| Authorization Utilization | ✓ | ✓ |

## Stated caveats

* **Hours Delivered** — Excludes sessions whose unit of measure could not be resolved, so it is a floor. See docs/ANOMALY.md.
* **Units Delivered In Period** — Sessions outside every authorisation window are excluded here and included in Units Delivered (Completed). The two are different questions.

## Published headline figures

Re-derived from the warehouse and compared against what was actually written to `dashboard_data.json`. The section above compares two implementations of a metric; this one compares the number a reader sees against the data behind it, which is not the same question.

| Figure | Warehouse | Published | Difference | |
|---|---:|---:|---:|:--|
| hours_unused — open authorisations only | 57,763.7500 | 57,763.8000 | -0.0500 | ✓ |
| units_authorized — open authorisations only | 397,477.0000 | 397,477.0000 | 0.0000 | ✓ |
| units_delivered — open authorisations only | 170,263.0000 | 170,263.0000 | 0.0000 | ✓ |
| active_authorizations — open on the as-of date | 959.0000 | 959.0000 | 0.0000 | ✓ |
| closed_authorizations — period ended before the as-of date | 866.0000 | 866.0000 | 0.0000 | ✓ |
