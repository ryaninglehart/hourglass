# Data quality report

- **Verdict:** PUBLISHED
- **Evaluated:** 2026-08-20T18:19:50+00:00
- **Rule set:** v1.9.0 (`ae0bc03167c6e636`)
- **Checks:** 17 run, 7 failed

| Check | Dimension | Context | Severity | Result | Observed | Threshold | Rows |
|---|---|---|---|---|---|---|---|
| `uom_resolution_coverage` | conformance-value | verification | BLOCK | FAIL | `0.9438` | `0.99` | 2,933 |
| `session_reconciliation` | completeness | verification | BLOCK | PASS | `52160` | `52160` | 0 |
| `orphan_foreign_keys` | conformance-relational | verification | BLOCK | PASS | `0` | `0` | 0 |
| `duration_plausibility` | plausibility-atemporal | validation | BLOCK | PASS | `0` | `0` | 0 |
| `scd_type2_integrity` | conformance-relational | verification | BLOCK | PASS | `0` | `0` | 0 |
| `duplicate_session_submissions` | plausibility-uniqueness | verification | WARN | FAIL | `222` | `0` | 222 |
| `unmapped_service_codes` | conformance-value | verification | WARN | FAIL | `20` | `0` | 20 |
| `sessions_without_authorization` | conformance-relational | validation | WARN | FAIL | `124` | `0` | 124 |
| `overlapping_authorization_periods` | conformance-relational | verification | BLOCK | PASS | `0` | `0` | 0 |
| `utilization_over_ceiling` | plausibility-atemporal | validation | WARN | FAIL | `24` | `0` | 24 |
| `zero_unit_authorizations` | plausibility-atemporal | verification | WARN | PASS | `0` | `0` | 0 |
| `session_length_distribution_shift` | plausibility-temporal | verification | WARN | PASS | `0.1` | `0.4` | 0 |
| `uom_coverage_step_change` | plausibility-temporal | verification | WARN | FAIL | `0.0791` | `0.02` | 0 |
| `phi_egress` | conformance-value | verification | BLOCK | PASS | `0` | `0` | 0 |
| `phi_content_scan` | conformance-value | verification | BLOCK | PASS | `0` | `0` | 0 |
| `pseudonym_salt_configured` | conformance-computational | verification | WARN | FAIL |  |  | 0 |
| `row_counts` | completeness | verification | INFO | PASS | `52160` |  | 0 |

## Detail

### `uom_resolution_coverage` — BLOCK — FAIL

2,933 of 52,160 sessions have a duration whose unit of measure cannot be determined. Their duration is not recoverable, so they are excluded from all measures. Utilisation computed over the remaining 94.4% is correct but incomplete.

```json
[
  {
    "session_id": "SES-1E2C96158337",
    "service_code": 97153,
    "service_date": "2026-04-01",
    "duration_value": 10,
    "duration_uom": null
  },
  {
    "session_id": "SES-03B931C23F10",
    "service_code": 97156,
    "service_date": "2026-04-01",
    "duration_value": 5,
    "duration_uom": null
  },
  {
    "session_id": "SES-3E60E70F517F",
    "service_code": 97153,
    "service_date": "2026-04-01",
    "duration_value": 15,
    "duration_uom": null
  },
  {
    "session_id": "SES-4079DB0461C1",
    "service_code": 97156,
    "service_date": "2026-04-01",
    "duration_value": 5,
    "duration_uom": null
  },
  {
    "session_id": "SES-3057FF563B0A",
    "service_code": 97153,
    "service_date": "2026-04-01",
    "duration_value": 15,
    "duration_uom": null
  }
]
```

### `session_reconciliation` — BLOCK — PASS

All 52,160 deduplicated sessions reached the fact table.

### `orphan_foreign_keys` — BLOCK — PASS

Every session row resolves to a dimension member.

### `duration_plausibility` — BLOCK — PASS

All completed session durations fall within 5-480 minutes.

### `scd_type2_integrity` — BLOCK — PASS

240 clients across 265 versions: no overlapping ranges, exactly one current row each.

### `duplicate_session_submissions` — WARN — FAIL

222 duplicate session submissions removed (0.42% of raw rows). Same client, provider, service, date and duration. Left in, they inflate delivered units.

```json
[
  {
    "session_id": "SES-1FD07DCF0CAE",
    "client_id": "CLI-5FA9D8E92722",
    "service_date": "2026-01-14"
  },
  {
    "session_id": "SES-B2D92641AB61",
    "client_id": "CLI-C270C071B8A9",
    "service_date": "2026-01-15"
  },
  {
    "session_id": "SES-0E5A72AFC6D0",
    "client_id": "CLI-FF19679A664C",
    "service_date": "2026-01-20"
  },
  {
    "session_id": "SES-20B044D719C7",
    "client_id": "CLI-BA19C3E544D6",
    "service_date": "2026-01-21"
  },
  {
    "session_id": "SES-BCE25BFE48D9",
    "client_id": "CLI-DB30EF34BF8C",
    "service_date": "2026-01-22"
  }
]
```

### `unmapped_service_codes` — WARN — FAIL

20 sessions carry a service code absent from the catalogue. They are assigned to the explicit '(unmapped)' dimension member rather than dropped, so they stay countable.

### `sessions_without_authorization` — WARN — FAIL

124 completed sessions (625 units) have no matching authorisation for that client, service and date. The measure is correct; the exposure is that this care may be unbillable.

### `overlapping_authorization_periods` — BLOCK — PASS

No overlapping authorisation periods across 1,825 authorisations: every session attributes to exactly one authorisation.

### `utilization_over_ceiling` — WARN — FAIL

24 authorisations show more units delivered than authorised. This is a compliance and unbilled-revenue exposure, not a win.

```json
[
  {
    "auth_id": "ATH-16937EB510B3",
    "units_authorized": 52,
    "units_delivered": 57.0,
    "utilization": 1.096
  },
  {
    "auth_id": "ATH-86B489388238",
    "units_authorized": 16,
    "units_delivered": 19.0,
    "utilization": 1.188
  },
  {
    "auth_id": "ATH-F64395A45E7F",
    "units_authorized": 52,
    "units_delivered": 59.0,
    "utilization": 1.135
  },
  {
    "auth_id": "ATH-3B9BABBC0DC1",
    "units_authorized": 16,
    "units_delivered": 18.0,
    "utilization": 1.125
  },
  {
    "auth_id": "ATH-9305C8920A4B",
    "units_authorized": 52,
    "units_delivered": 53.0,
    "utilization": 1.019
  }
]
```

### `zero_unit_authorizations` — WARN — PASS

No authorisation approves zero units while carrying delivered sessions.

### `session_length_distribution_shift` — WARN — PASS

Median session length is stable month to month (largest move 10.0%).

### `uom_coverage_step_change` — WARN — FAIL

Unit-of-measure coverage fell 7.9% between 2026-03 (99.9%) and 2026-04 (92.0%) and did not recover in any later month. A step that persists is a source change, not noise: the defect starts in 2026-04 and every month before it clears the 99% floor.

```json
[
  {
    "year_month": "2026-01",
    "uom_coverage": 0.9973
  },
  {
    "year_month": "2026-02",
    "uom_coverage": 0.9998
  },
  {
    "year_month": "2026-03",
    "uom_coverage": 0.9995
  },
  {
    "year_month": "2026-04",
    "uom_coverage": 0.9204
  },
  {
    "year_month": "2026-05",
    "uom_coverage": 0.9177
  },
  {
    "year_month": "2026-06",
    "uom_coverage": 0.924
  },
  {
    "year_month": "2026-07",
    "uom_coverage": 0.9237
  },
  {
    "year_month": "2026-08",
    "uom_coverage": 0.9263
  }
]
```

### `phi_egress` — BLOCK — PASS

No direct identifiers and no undeclared columns in anything being published.

### `phi_content_scan` — BLOCK — PASS

No identifier-shaped values found in anything being published.

### `pseudonym_salt_configured` — WARN — FAIL

HOURGLASS_PSEUDONYM_SALT is unset, so this run minted a random salt and discarded it at exit. Nothing published here can be precomputed, and nothing published here can be joined to another build's exports -- surrogates for the same client differ between runs. For week-to-week comparability, configure a salt and keep it: export HOURGLASS_PSEUDONYM_SALT=$(openssl rand -hex 32)

```json
[
  {
    "salt_source": "ephemeral"
  }
]
```

### `row_counts` — INFO — PASS

sessions 52,160 | authorisations 1,825 | clients 240 | client versions 265

## Acknowledged blocking failures

A human released these on purpose. The reason is part of the record.

- **`uom_resolution_coverage`** — Ticket DE-412. Vendor confirmed the 2026-04-01 unit-of-measure change and is back-filling the flag. Publishing with coverage stamped on the report.
