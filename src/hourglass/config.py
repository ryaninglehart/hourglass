"""Central configuration.

Everything that a reviewer might want to change lives here rather than being
scattered through the modules. The values that carry business meaning are
annotated with where they came from, because a number without a provenance is
just a number someone made up.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[2]

# HOURGLASS_DATA_DIR moves the whole data tree somewhere private. Without it a
# pytest run and a `make run` in the same checkout write the same warehouse,
# race on the same atomic rename, and delete each other's published files.
#
# It is read here, at import, rather than at each use, because every module
# below binds these paths at import -- and `export.py` and `ingest.py` bind
# them again as default arguments, which freeze at definition time. Patching
# the attributes afterwards would redirect some call sites and silently miss
# the rest, which is worse than not redirecting at all.
DATA = Path(os.environ.get("HOURGLASS_DATA_DIR") or ROOT / "data")
RAW = DATA / "raw"            # generator output, stands in for source extracts
LAKE = DATA / "lake"          # local mirror of the S3 data lake
OUT = DATA / "out"            # warehouse, exports, reports
WAREHOUSE = OUT / "hourglass.db"
EXPORT_DIR = OUT / "bi"
REPORT_DIR = OUT / "reports"


# --------------------------------------------------------------------------
# S3 / data lake
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class S3Config:
    """Where the lake lives.

    ``endpoint_url`` is what makes this work against LocalStack without any
    code changes: boto3 talks the real S3 API, it just points at a container on
    localhost instead of at AWS. Unset the env var and the same code path goes
    to real AWS.
    """

    bucket: str = os.environ.get("HOURGLASS_BUCKET", "cortica-datalake-dev")
    region: str = os.environ.get("AWS_DEFAULT_REGION", "us-west-2")
    endpoint_url: str | None = os.environ.get("AWS_ENDPOINT_URL") or None
    raw_prefix: str = "raw"

    @property
    def uses_localstack(self) -> bool:
        return bool(self.endpoint_url)


S3 = S3Config()


# --------------------------------------------------------------------------
# Business rules
# --------------------------------------------------------------------------

# The date the EHR vendor changed how it reports session length. Sessions
# before this date report minutes; sessions on or after report 15-minute
# units. This is the seeded defect the pipeline exists to catch: the size of
# the hole is quality.check_uom_coverage and the month it opens in is
# quality.check_coverage_step_change. See docs/ANOMALY.md.
UOM_MIGRATION_DATE = date(2026, 4, 1)

# Authorization utilization target band.
# Source: industry operating benchmark, 90-100% of authorized hours delivered.
# Below the floor means authorized care is going undelivered; above 100% means
# care was delivered that was not authorized, which is an unbilled-revenue and
# compliance exposure rather than a win.
UTILIZATION_FLOOR = 0.90
UTILIZATION_CEILING = 1.00

# An authorization inside this window with materially unused units is the
# actionable output of the whole pipeline: there is still time to schedule.
EXPIRY_WARNING_DAYS = 30
AT_RISK_UNUSED_FRACTION = 0.25

# Sessions whose length falls outside this range are implausible for the
# service codes modelled here and indicate a data defect, not a short visit.
MIN_PLAUSIBLE_MINUTES = 5
MAX_PLAUSIBLE_MINUTES = 480

# How far behind the watermark an incremental run re-reads, to pick up records
# that arrived or were corrected after their business date. Seven days covers
# the correction latency of a clinical documentation workflow. Too short and
# late corrections are lost for good; too long and every run reprocesses data
# that has not changed. It lives here rather than in incremental.py because it
# is a business decision about how late data is allowed to be, not a technical
# constant.
INCREMENTAL_LOOKBACK_DAYS = 7

# Month-over-month change in median session minutes large enough to suggest a
# source-system change rather than a change in clinical practice.
DISTRIBUTION_SHIFT_THRESHOLD = 0.40


# --------------------------------------------------------------------------
# Service catalogue
# --------------------------------------------------------------------------
# CPT codes used in ABA and paediatric therapy billing. `unit_basis` is the
# reason dim_service has to exist: you cannot sum a column of "units" across
# these codes without knowing what a unit means for each one.

SERVICES: list[dict] = [
    {"service_code": "97151", "service_name": "Behavior identification assessment",
     "discipline": "ABA", "unit_basis": "15_min", "minutes_per_unit": 15},
    {"service_code": "97153", "service_name": "Adaptive behavior treatment by protocol",
     "discipline": "ABA", "unit_basis": "15_min", "minutes_per_unit": 15},
    {"service_code": "97155", "service_name": "Adaptive behavior treatment w/ protocol modification",
     "discipline": "ABA", "unit_basis": "15_min", "minutes_per_unit": 15},
    {"service_code": "97156", "service_name": "Family adaptive behavior treatment guidance",
     "discipline": "ABA", "unit_basis": "15_min", "minutes_per_unit": 15},
    {"service_code": "92507", "service_name": "Speech/language treatment",
     "discipline": "Speech", "unit_basis": "per_session", "minutes_per_unit": 45},
    {"service_code": "97530", "service_name": "Therapeutic activities (OT)",
     "discipline": "Occupational", "unit_basis": "15_min", "minutes_per_unit": 15},
    {"service_code": "99213", "service_name": "Office visit, established patient",
     "discipline": "Medical", "unit_basis": "per_session", "minutes_per_unit": 30},
]

SERVICE_BY_CODE = {s["service_code"]: s for s in SERVICES}


# --------------------------------------------------------------------------
# Generator shape
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class GeneratorConfig:
    """Controls the synthetic dataset.

    All data produced by this project is synthetic. No real patient
    information is present anywhere in this repository. See README.md.
    """

    seed: int = 20260818
    n_clients: int = 240
    n_providers: int = 85
    start_date: date = date(2026, 1, 6)     # a Monday
    end_date: date = date(2026, 8, 14)
    auth_period_days: int = 182             # nominal six-month period
    # Payers issue quarterly, four-month and six-month authorisations.
    auth_period_day_options: tuple[int, ...] = (91, 121, 182)

    # Fraction of post-migration EHR rows that arrive with a null unit of
    # measure. This is the defect. 8% is small enough to be missed by eyeball
    # and large enough to move the headline metric.
    null_uom_rate: float = 0.08

    # Realistic operational noise the pipeline must tolerate without blocking.
    cancel_rate: float = 0.06
    no_show_rate: float = 0.04
    duplicate_rate: float = 0.004
    unauthorized_session_rate: float = 0.02
    unmapped_code_rate: float = 0.06        # a real CPT the catalogue lacks
    payer_change_rate: float = 0.10         # drives SCD Type 2 on dim_client

    centers: tuple[tuple[str, str, str], ...] = (
        ("CTR-SD", "San Diego", "CA"),
        ("CTR-TEM", "Temecula", "CA"),
        ("CTR-IRV", "Irvine", "CA"),
        ("CTR-PHX", "Phoenix", "AZ"),
        ("CTR-PLA", "Plano", "TX"),
    )

    payers: tuple[tuple[str, str, str], ...] = (
        ("PAY-001", "Meridian Health Plan", "value_based"),
        ("PAY-002", "Pacific Care Network", "value_based"),
        ("PAY-003", "Statewide Medicaid MCO", "fee_for_service"),
        ("PAY-004", "Cascade Benefits", "fee_for_service"),
        ("PAY-005", "Unified Employer Trust", "value_based"),
    )

    roles: tuple[tuple[str, str, float], ...] = (
        # role, discipline, share of workforce
        ("RBT", "ABA", 0.62),
        ("BCBA", "ABA", 0.18),
        ("SLP", "Speech", 0.09),
        ("OT", "Occupational", 0.07),
        ("MD", "Medical", 0.04),
    )


GEN = GeneratorConfig()


def ensure_dirs() -> None:
    for p in (DATA, RAW, LAKE, OUT, EXPORT_DIR, REPORT_DIR):
        p.mkdir(parents=True, exist_ok=True)
