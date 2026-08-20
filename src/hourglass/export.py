"""Publish the warehouse for downstream consumers.

Two consumers, one source of truth.

* **Power BI** gets flat CSVs, one per table, plus ``bi/measures.dax`` and the
  relationship list. Nulls are written as empty fields rather than the string
  "NULL" or a zero, so Power BI reads BLANK. A null coerced to zero is the
  fastest way to make a rate measure lie: zero is a value and BLANK is not, and
  averaging over the difference changes the answer.

* **The HTML dashboard** gets a single JSON payload. It exists because a
  reviewer should be able to see the result by opening a file, without a
  licence, a tenant, or Windows.

Both are generated from the same frames in the same run, so they cannot drift.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd

from .config import EXPORT_DIR, UTILIZATION_CEILING, UTILIZATION_FLOOR
from .disclosure import needs_suppression
from .model import _normalise

BI_TABLES = ["dim_date", "dim_client", "dim_service", "dim_provider", "dim_center",
             "dim_payer", "fact_session", "fact_authorization"]

RELATIONSHIPS = [
    ("fact_session", "date_key", "dim_date", "date_key", "many_to_one"),
    ("fact_session", "client_key", "dim_client", "client_key", "many_to_one"),
    ("fact_session", "provider_key", "dim_provider", "provider_key", "many_to_one"),
    ("fact_session", "service_key", "dim_service", "service_key", "many_to_one"),
    ("fact_session", "center_key", "dim_center", "center_key", "many_to_one"),
    ("fact_authorization", "client_key", "dim_client", "client_key", "many_to_one"),
    ("fact_authorization", "service_key", "dim_service", "service_key", "many_to_one"),
    ("fact_authorization", "payer_key", "dim_payer", "payer_key", "many_to_one"),
    ("fact_authorization", "period_start_key", "dim_date", "date_key",
     "many_to_one (inactive - second role-playing date; activate with USERELATIONSHIP)"),
]


def export_csvs(frames: dict[str, pd.DataFrame], out_dir: Path = EXPORT_DIR) -> list[Path]:
    """Write one CSV per table, typed the way Power BI needs to read them.

    The same ``_normalise`` the SQLite loader uses is applied here, and that is
    not tidiness. Left as Python booleans, pandas writes ``True``/``False``;
    Power Query types those columns as logical, and every DAX predicate in
    ``measures.dax`` compares them to 1 -- which raises *"DAX comparison
    operations do not support comparing values of type True/False with values
    of type Number"* and takes out more than half the measures on import.

    Normalising in one place is what makes the claim above -- one source of
    truth for both consumers -- actually true rather than aspirational.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for table in BI_TABLES:
        if table not in frames:
            continue
        path = out_dir / f"{table}.csv"
        _normalise(frames[table]).to_csv(path, index=False, na_rep="")
        written.append(path)
    return written


def write_relationship_spec(out_dir: Path = EXPORT_DIR) -> Path:
    lines = [
        "# Power BI model: relationships to draw",
        "",
        "Import the eight CSVs in this folder, then create these relationships.",
        "All are single-direction, many-to-one, from the fact table to the dimension.",
        "",
        "| From table | From column | To table | To column | Cardinality |",
        "|---|---|---|---|---|",
    ]
    for f_t, f_c, t_t, t_c, card in RELATIONSHIPS:
        lines.append(f"| `{f_t}` | `{f_c}` | `{t_t}` | `{t_c}` | {card} |")
    lines += [
        "",
        "## Two things that will bite you if you skip them",
        "",
        "**Leave bidirectional filtering off.** With two fact tables hanging off the",
        "same client and service dimensions, a bidirectional relationship lets a filter",
        "travel up one fact table and back down the other, so filtering by provider",
        "would silently change authorised units. Provider has nothing to do with who",
        "authorised the care.",
        "",
        "**The second date relationship stays inactive.** `fact_authorization` has both",
        "a start and an end date pointing at `dim_date`. Power BI allows only one active",
        "path; keep `period_start_key` active and reach the other with",
        "`USERELATIONSHIP` inside a measure when you need it.",
        "",
        "## Marking the date table",
        "",
        "Mark `dim_date` as the date table on `full_date`. Without it the time",
        "intelligence functions in `measures.dax` will not behave.",
    ]
    path = out_dir / "relationships.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _suppress_small(n: int) -> int | None:
    """The org-wide people count, or None when 1-10 people would be shown."""
    return None if needs_suppression(n) else n


def build_dashboard_payload(
    util: pd.DataFrame,
    at_risk: pd.DataFrame,
    by_payer: pd.DataFrame,
    by_discipline: pd.DataFrame,
    by_center: pd.DataFrame,
    monthly: pd.DataFrame,
    quality: dict,
    comparison: dict,
    meta: dict,
) -> dict:
    active = util.loc[util["is_active"]]
    closed = util.loc[util["is_closed"]]
    exp = float(active["expected_units_to_date"].sum())
    total_auth = float(active["units_authorized"].sum())
    total_del = float(active["units_delivered"].sum())
    closed_auth = float(closed["units_authorized"].sum())
    closed_del = float(closed["units_delivered"].sum())

    return {
        "meta": meta,
        "headline": {
            # Pace for open authorisations, final utilisation for closed ones.
            # Quoting raw utilisation on an authorisation that is half way
            # through its window makes healthy delivery look like a crisis.
            "pace": round(total_del / exp, 4) if exp else 0.0,
            "closed_utilization": round(closed_del / closed_auth, 4) if closed_auth else 0.0,
            "floor": UTILIZATION_FLOOR,
            "ceiling": UTILIZATION_CEILING,
            "active_authorizations": len(active),
            "closed_authorizations": len(closed),
            "units_authorized": round(total_auth, 1),
            "units_delivered": round(total_del, 1),
            "expected_units_to_date": round(exp, 1),
            # Summed from the per-row `hours_unused`, which `build_utilization`
            # computed against each service's own `minutes_per_unit`. NOT
            # `units_unused * 0.25`.
            #
            # It was `* 0.25` until an adversarial review found it, which is
            # worth recording rather than quietly correcting: this is the exact
            # error the project was built to argue against, on the headline
            # tile of the dashboard, in a file whose sibling modules carry
            # three separate comments forbidding it. A unit is fifteen minutes
            # for the ABA codes and forty-five for a speech session, so the flat
            # divisor understated the figure by 958 hours -- 1.7%, small enough
            # to look right, concentrated entirely on the disciplines the
            # at-risk list is meant to surface.
            #
            # The parity check did not catch it either. `metrics.py` registers
            # `hours_unused` against `utilization["hours_unused"]`, so it was
            # attesting to a correct number that nothing displayed. The metric
            # registry now covers the published headline figures too, and the
            # lesson is the one in docs/INCIDENTS.md: a check verifies the
            # value it names, not the value the reader sees.
            "hours_unused": round(float(active["hours_unused"].sum()), 1),
            "at_risk_count": len(at_risk),
            "at_risk_hours": round(float(at_risk["hours_unused"].sum()), 1),
            # Suppressed at the source, not at render. dashboard_data.json
            # is itself a published artifact, so a small count hidden only
            # by the page would still sit raw in the payload underneath it.
            # The digest applies this rule to this number; the dashboard did
            # not, which was two policies for one figure.
            "at_risk_children": _suppress_small(
                int(at_risk["client_id"].nunique()) if len(at_risk) else 0),
        },
        "by_payer": by_payer.round(4).to_dict("records"),
        "by_discipline": by_discipline.round(4).to_dict("records"),
        "by_center": by_center.round(4).to_dict("records"),
        "monthly": monthly.round(4).to_dict("records"),
        # `center_name` is dropped, and the reason is the same one `digest.py`
        # pools small centres for. A pseudonymised client reference is stable
        # within a run, so counting distinct references under a named centre
        # recovers that centre's head count -- and on the current data five
        # centres in this list sit inside the 1-to-10 suppression range. The
        # digest protects against exactly that and the payload was handing it
        # over, embedded verbatim in `dashboard.html` where Ctrl-F finds it.
        #
        # Nothing reads the column: `scripts/build_dashboard.py` never
        # references it. It was published because it was in the frame, which is
        # how most disclosures happen.
        "at_risk": at_risk.head(25).drop(
            columns=[c for c in ("center_name", "center_id", "home_center_id")
                     if c in at_risk.columns]
        ).assign(
            period_end=lambda d: d["period_end"].dt.strftime("%Y-%m-%d")
        ).round(3).to_dict("records"),
        "quality": quality,
        "comparison": comparison,
    }


def json_safe(obj):
    """Recursively replace values that json.dumps writes as invalid JSON.

    Python emits a bare ``NaN`` for float('nan'), which every strict JSON
    parser -- including the browser's -- rejects. pandas produces NaN and NaT
    freely, so anything crossing the boundary into a JSON file has to be
    cleaned first. They become null, which is what they meant.
    """
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    if obj is None or isinstance(obj, (str, bool, int)):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if obj is pd.NaT or (obj is not None and obj is pd.NA):
        return None
    try:
        if pd.isna(obj):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(obj, pd.Timestamp):
        return obj.strftime("%Y-%m-%d")
    if hasattr(obj, "item"):          # numpy scalar
        return json_safe(obj.item())
    return str(obj)


def write_dashboard_payload(payload: dict, out_dir: Path = EXPORT_DIR) -> Path:
    path = out_dir / "dashboard_data.json"
    # allow_nan=False makes an escaped NaN a loud failure here rather than a
    # silent one in the browser three steps later.
    path.write_text(
        json.dumps(json_safe(payload), indent=2, allow_nan=False), encoding="utf-8"
    )
    return path
