# Run-over-run data diff

**Nothing changed.** Every modelled table holds the same rows, under the same primary keys, with the same value in every compared cell as the previous published build.

Not that the two files are identical, which they are not: `run_log` gains a row on every run and is excluded from the comparison, so the bytes differ by design. What is asserted here is a value-level match across the modelled tables — the same property the idempotency test asserts, observed on the real warehouse rather than in a fixture.

| Table | Before | After | Added | Removed | Changed | Where |
|---|---:|---:|---:|---:|---:|---|
| `dim_center` | 5 | 5 | 0 | 0 | 0 | — |
| `dim_client` | 265 | 265 | 0 | 0 | 0 | — |
| `dim_date` | 397 | 397 | 0 | 0 | 0 | — |
| `dim_payer` | 5 | 5 | 0 | 0 | 0 | — |
| `dim_provider` | 85 | 85 | 0 | 0 | 0 | — |
| `dim_service` | 8 | 8 | 0 | 0 | 0 | — |
| `fact_authorization` | 1,825 | 1,825 | 0 | 0 | 0 | — |
| `fact_session` | 52,160 | 52,160 | 0 | 0 | 0 | — |

*Compared by primary key, not by row order. Cell counts are per column, so a single-column change and a whole-table rewrite do not look alike.*
