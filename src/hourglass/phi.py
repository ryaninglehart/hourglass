"""PHI classification and the egress boundary.

Most projects say "HIPAA compliant" and mean that somebody was careful. This
one draws a boundary and puts a gate on it, because carefulness is a property
of people on a good day and a boundary is a property of the system.

**The boundary.** The raw lake is inside it: extracts arrive as the source
systems send them, identifiers and all. Everything the pipeline *publishes* --
BI exports, the dashboard, the digest, the quality report -- is outside it, and
nothing classified as a direct identifier is permitted to cross.

**How it is enforced.** Two layers, deliberately different in kind, because a
single mechanism fails silently when its one assumption is wrong.

1. *Classification.* Every column of every published table is declared in
   ``FIELD_CLASSIFICATION`` below. A column nobody has classified is treated as
   an identifier until somebody says otherwise -- unknown is not safe, and the
   default has to be the direction that fails loudly.

2. *Content scanning.* Declarations describe intent; values are what actually
   leave. The scanner reads the outgoing data and looks for things shaped like
   identifiers -- social security numbers, medical record and member numbers,
   phone numbers, e-mail addresses, US-format dates of birth, ZIP+4 --
   regardless of what the column claims to be. It exists to catch the case
   where a source system starts putting an identifier in a free-text field and
   nobody notices for six weeks.

   **What it cannot do, stated because a scanner people over-trust is worse
   than no scanner.** It is regular expressions over sampled rows. It will not
   find a personal name (Safe Harbor identifier (i)) -- names have no shape --
   and it will not find an ISO-format date of birth, because `1990-03-14` is
   indistinguishable from any other date. Names and free-text dates need either
   named-entity recognition or a column-level rule that free text does not
   leave at all. The classification layer is what covers that gap; this layer
   covers the case where the classification is wrong.

The classification frame is HIPAA's Safe Harbor list (45 CFR 164.514(b)(2)),
which enumerates eighteen identifier types that must be removed for a data set
to be considered de-identified under that method. Safe Harbor is cited here as
the *reference for what counts as an identifier*; this module implements the
check, not a legal determination.

**On pseudonymisation.** ``pseudonymise`` derives a surrogate from an
identifier using HMAC-SHA256 with a salt. It is *pseudonymisation*, not
anonymisation, and the strength of it is worth stating precisely rather than
described as "one-way", which it is not in any useful sense here.

HMAC is one-way over an unbounded input space. Client identifiers are not an
unbounded input space. This dataset holds 240 of them, ``CLI-00001`` through
``CLI-00240``; an attacker who knew only the shape and not the count would
enumerate the whole five-digit range, which is 10^5 candidates. Either number
is seconds of hashing. Anyone holding the salt can build the table and invert
every published surrogate by lookup. So the surrogate is
exactly as strong as the salt is secret, and no stronger. A salt committed to
this file would be a salt every reader of the repository already has, which is
why ``_salt`` refuses to ship one -- see the trade-off recorded there, and the
``pseudonym_salt_configured`` gate in :mod:`hourglass.quality` that reports
which salt a given build actually used.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import pandas as pd


class Sensitivity(str, Enum):
    """What a column is, in terms of what may be done with it."""

    DIRECT_IDENTIFIER = "direct_identifier"
    """Identifies a person on its own. Never crosses the egress boundary."""

    QUASI_IDENTIFIER = "quasi_identifier"
    """Does not identify alone, but can in combination -- age, dates, a small
    geography. Permitted, but generalised: this is why dim_client publishes an
    age band and not a birth date."""

    CLINICAL = "clinical"
    """Health information about a person. Permitted only once the identifiers
    it hangs off have been removed or pseudonymised."""

    OPERATIONAL = "operational"
    """About the business rather than the person -- counts, durations, keys."""

    SAFE = "safe"
    """Reference data with no link to any individual."""


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
# Read this as the data-protection contract for the warehouse. Anything absent
# is treated as a direct identifier by `classify`, which is the conservative
# direction: a new column added upstream blocks publication until somebody has
# looked at it and said what it is.

FIELD_CLASSIFICATION: dict[str, dict[str, Sensitivity]] = {
    "dim_client": {
        # In this synthetic data client_id is already a surrogate. In a real
        # deployment it would be an MRN, which is Safe Harbor identifier (xviii)
        # -- so it is classified as what it would be, not as what it happens to
        # be here, and it is pseudonymised on the way out.
        "client_id": Sensitivity.DIRECT_IDENTIFIER,
        "client_key": Sensitivity.OPERATIONAL,
        "version": Sensitivity.OPERATIONAL,
        "age_years": Sensitivity.QUASI_IDENTIFIER,
        "age_band": Sensitivity.QUASI_IDENTIFIER,
        "home_center_id": Sensitivity.QUASI_IDENTIFIER,
        "payer_id": Sensitivity.OPERATIONAL,
        "change_reason": Sensitivity.OPERATIONAL,
        "valid_from": Sensitivity.QUASI_IDENTIFIER,
        "valid_to": Sensitivity.QUASI_IDENTIFIER,
        "is_current": Sensitivity.OPERATIONAL,
    },
    "fact_session": {
        # Safe Harbor identifier (xviii) is "any other unique identifying
        # number, characteristic, or code" assigned by the covered entity. An
        # EHR's own encounter number qualifies: it is 1:1 with a visit, so
        # anyone holding the source extract joins it straight back to a child.
        # Publishing it raw alongside a pseudonymised client_id would make the
        # pseudonym decorative -- whatever the surrogate costs to reverse, the
        # row next to it costs nothing.
        "session_id": Sensitivity.DIRECT_IDENTIFIER,
        "date_key": Sensitivity.QUASI_IDENTIFIER,
        "client_key": Sensitivity.OPERATIONAL,
        "provider_key": Sensitivity.OPERATIONAL,
        "service_key": Sensitivity.CLINICAL,
        "center_key": Sensitivity.QUASI_IDENTIFIER,
        "units_delivered": Sensitivity.CLINICAL,
        "minutes_delivered": Sensitivity.CLINICAL,
        "uom_resolved": Sensitivity.OPERATIONAL,
        "unresolved_reason": Sensitivity.OPERATIONAL,
        "is_completed": Sensitivity.OPERATIONAL,
        "is_cancelled": Sensitivity.OPERATIONAL,
        "is_no_show": Sensitivity.OPERATIONAL,
        "source_system": Sensitivity.OPERATIONAL,
    },
    "fact_authorization": {
        # Same reasoning as session_id: an authorisation number is 1:1 with a
        # client, service and period.
        "auth_id": Sensitivity.DIRECT_IDENTIFIER,
        "client_key": Sensitivity.OPERATIONAL,
        "service_key": Sensitivity.CLINICAL,
        "payer_key": Sensitivity.OPERATIONAL,
        "period_start_key": Sensitivity.QUASI_IDENTIFIER,
        "period_end_key": Sensitivity.QUASI_IDENTIFIER,
        "units_authorized": Sensitivity.CLINICAL,
        "authorized_days": Sensitivity.OPERATIONAL,
    },
    "dim_provider": {
        # A provider is a person too. Employment data is not PHI, but a
        # provider identifier plus a session date plus a centre narrows the
        # patient population considerably, so it does not leave either.
        "provider_id": Sensitivity.DIRECT_IDENTIFIER,
        "provider_key": Sensitivity.OPERATIONAL,
        "role": Sensitivity.OPERATIONAL,
        "discipline": Sensitivity.OPERATIONAL,
        "center_id": Sensitivity.OPERATIONAL,
        "hire_date": Sensitivity.QUASI_IDENTIFIER,
        "term_date": Sensitivity.QUASI_IDENTIFIER,
        "is_active": Sensitivity.OPERATIONAL,
    },
    "dim_service": {
        "service_key": Sensitivity.SAFE, "service_code": Sensitivity.SAFE,
        "service_name": Sensitivity.SAFE, "discipline": Sensitivity.SAFE,
        "unit_basis": Sensitivity.SAFE, "minutes_per_unit": Sensitivity.SAFE,
    },
    "dim_center": {
        "center_key": Sensitivity.SAFE, "center_id": Sensitivity.SAFE,
        "center_name": Sensitivity.SAFE, "state": Sensitivity.SAFE,
    },
    "dim_payer": {
        "payer_key": Sensitivity.SAFE, "payer_id": Sensitivity.SAFE,
        "payer_name": Sensitivity.SAFE, "contract_type": Sensitivity.SAFE,
    },
    # The at-risk list is an artifact too, and for a while it was not checked.
    # The egress gate originally inspected only the eight warehouse tables, so
    # the dashboard shipped raw client identifiers past a boundary that
    # reported itself clean. A boundary that covers most of the exits is not a
    # boundary. Every frame that reaches a published file is enumerated here.
    "at_risk": {
        "auth_id": Sensitivity.DIRECT_IDENTIFIER,
        "client_id": Sensitivity.DIRECT_IDENTIFIER,
        "service_code": Sensitivity.CLINICAL,
        "service_name": Sensitivity.CLINICAL,
        "discipline": Sensitivity.CLINICAL,
        "payer_name": Sensitivity.OPERATIONAL,
        "contract_type": Sensitivity.OPERATIONAL,
        "units_authorized": Sensitivity.CLINICAL,
        "units_delivered": Sensitivity.CLINICAL,
        "units_unused": Sensitivity.CLINICAL,
        "hours_authorized": Sensitivity.CLINICAL,
        "hours_delivered": Sensitivity.CLINICAL,
        "hours_unused": Sensitivity.CLINICAL,
        "utilization": Sensitivity.CLINICAL,
        "pace": Sensitivity.CLINICAL,
        "days_to_expiry": Sensitivity.QUASI_IDENTIFIER,
        "period_end": Sensitivity.QUASI_IDENTIFIER,
        "center_name": Sensitivity.QUASI_IDENTIFIER,
    },
    "dim_date": {
        "date_key": Sensitivity.SAFE, "full_date": Sensitivity.SAFE,
        "year": Sensitivity.SAFE, "quarter": Sensitivity.SAFE,
        "month": Sensitivity.SAFE, "month_name": Sensitivity.SAFE,
        "year_month": Sensitivity.SAFE, "day_of_week": Sensitivity.SAFE,
        "day_name": Sensitivity.SAFE, "is_weekend": Sensitivity.SAFE,
    },
}


def classify(table: str, column: str) -> Sensitivity:
    """What a column is. Unknown columns are identifiers until declared."""
    return FIELD_CLASSIFICATION.get(table, {}).get(
        column, Sensitivity.DIRECT_IDENTIFIER
    )


def undeclared_columns(table: str, columns) -> list[str]:
    known = FIELD_CLASSIFICATION.get(table, {})
    return [c for c in columns if c not in known]


# ---------------------------------------------------------------------------
# Pseudonymisation
# ---------------------------------------------------------------------------

SALT_ENV = "HOURGLASS_PSEUDONYM_SALT"

# Kept as a value to *recognise*, not a value to use. An earlier version of
# this module shipped it as the default, which put the salt for every published
# surrogate in a public file; the string survives only so the gate can tell an
# operator who has re-exported it that they have configured nothing.
DEVELOPMENT_SALT = "hourglass-dev-salt-not-a-secret"

SALT_CONFIGURED = "configured"
SALT_EPHEMERAL = "ephemeral"
SALT_DEVELOPMENT_DEFAULT = "development_default"

_ephemeral_salt: bytes | None = None


def salt_source() -> str:
    """Where this process's salt comes from.

    The gate reads this rather than the salt itself, so deciding whether a
    build is safe never requires handling the secret.
    """
    configured = os.environ.get(SALT_ENV, "").strip()
    if not configured:
        return SALT_EPHEMERAL
    if configured == DEVELOPMENT_SALT:
        return SALT_DEVELOPMENT_DEFAULT
    return SALT_CONFIGURED


def _salt() -> bytes:
    """The salt, and what happens when nobody has configured one.

    A checked-in default is not a weaker option than a configured secret, it is
    no option at all: the identifier space is enumerable, so a reader of the
    repository holds everything needed to invert every published surrogate.
    When ``HOURGLASS_PSEUDONYM_SALT`` is unset this module therefore mints a
    random 32-byte salt for the life of the process and never writes it
    anywhere -- nobody can precompute against a value that does not exist until
    the run starts and does not survive it.

    What that costs, because it is a real property to give up: surrogates are
    stable *within* a run and unstable *between* runs. Two builds' exports
    cannot be joined to each other, so week-over-week tracking of a client
    outside the boundary is impossible until an operator configures a salt.
    Inside the boundary nothing changes -- the warehouse stores raw
    identifiers, so the diff and the fact tables are unaffected.

    Unlinkable-but-useless beats stable-but-reversible, so that is the default.
    ``quality.check_pseudonym_salt`` fails either way, which is what stops the
    ephemeral salt from being mistaken for a configured deployment.
    """
    global _ephemeral_salt
    source = salt_source()
    if source == SALT_EPHEMERAL:
        if _ephemeral_salt is None:
            _ephemeral_salt = secrets.token_bytes(32)
        return _ephemeral_salt
    return os.environ[SALT_ENV].strip().encode()


def pseudonymise(value: str, prefix: str = "PSN") -> str:
    """Surrogate for an identifier, stable for as long as the salt is.

    Not one-way over this input space: see the module docstring. Reversible by
    anyone holding the salt, and by nobody else.
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    digest = hmac.new(_salt(), str(value).encode(), hashlib.sha256).hexdigest()
    return f"{prefix}-{digest[:12].upper()}"


def pseudonymise_series(series: pd.Series, prefix: str = "PSN") -> pd.Series:
    """Vectorised over distinct values.

    A column of 52,000 sessions holds only 240 distinct clients, so hashing
    the unique values and mapping back is two orders of magnitude less work
    than hashing every row -- and HMAC is deliberately not cheap.
    """
    uniques = series.dropna().unique()
    mapping = {v: pseudonymise(v, prefix) for v in uniques}
    return series.map(mapping).fillna("")


# ---------------------------------------------------------------------------
# Content scanning
# ---------------------------------------------------------------------------
# Declarations describe what a column is meant to hold. These patterns describe
# what identifiers look like. The gap between the two is where incidents live:
# a free-text field that starts carrying a parent's phone number, an "external
# ref" column that turns out to be a member ID.

IDENTIFIER_PATTERNS: dict[str, re.Pattern] = {
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "email": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b"),
    # Punctuation is the signal, not the digits. An earlier version accepted a
    # bare ten-digit run with optional separators, which made every ten-digit
    # number a phone number: it fired six times on the published dashboard
    # payload on floats such as `106728.6875`, where the decimal point was
    # being read as a separator. Since that check is the one nobody may
    # override, a false positive there is an unhalting build, so the pattern
    # now insists on evidence that a human formatted the value as a phone
    # number -- a `+1` prefix, a parenthesised area code, or a separator in
    # *both* group positions. The cost is that `5551234567` written with no
    # punctuation at all is not detected; the classification layer, not this
    # one, is what keeps such a column from being published.
    "phone_us": re.compile(
        r"(?<![\d.])"
        r"(?:"
        r"\+1[ .-]?\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}"
        r"|(?:1[ .-])?\(\d{3}\)[ .-]?\d{3}[ .-]?\d{4}"
        r"|(?:1[ .-])?\d{3}[ .-]\d{3}[ .-]\d{4}"
        r")(?!\d)"),
    "date_of_birth": re.compile(
        r"\b(?:0?[1-9]|1[0-2])[/-](?:0?[1-9]|[12]\d|3[01])[/-](?:19|20)\d{2}\b"),
    # The optional ID / NO / NUMBER token matters: in practice these appear as
    # "Member ID 99182773" and "MRN No. 88213" far more often than as a bare
    # label followed by the value. A pattern that only matches the tidy form
    # passes its own unit test and misses the real data.
    "mrn_like": re.compile(
        r"\b(?:MRN|MR#|MEDREC|MEDICAL\s+RECORD)"
        r"(?:\s*(?:ID|NO|NUM|NUMBER|#))?[ .:#-]*\w{4,}\b", re.I),
    # The trailing token has to contain a digit. Accepting any six-character
    # word after the label meant the English phrase "member rather" was a
    # member number, and a payer legitimately named "Member Health Network"
    # would have been one too -- on a check that cannot be overridden.
    "member_id_like": re.compile(
        r"\b(?:MEMBER|SUBSCRIBER|POLICY)"
        r"(?:\s*(?:ID|NO|NUM|NUMBER|#))?[ .:#-]*"
        r"(?=[A-Z0-9-]{6,})[A-Z0-9-]*\d[A-Z0-9-]*\b", re.I),
    "us_zip_plus4": re.compile(r"\b\d{5}-\d{4}\b"),
}


@dataclass
class ScanHit:
    table: str
    column: str
    pattern: str
    match_count: int
    example: str

    def redacted_example(self) -> str:
        """Never quote the match itself.

        A finding that reproduces the identifier it found has moved the problem
        rather than reported it -- the scan report is itself an artifact that
        gets written to disk and read by people.
        """
        return f"{self.example[:2]}…{self.example[-2:]}" if len(self.example) > 6 else "…"


def scan_frame(df: pd.DataFrame, table: str, sample_rows: int = 5_000) -> list[ScanHit]:
    """Look for identifier-shaped values in the object columns of a frame.

    Sampling is a deliberate trade. A full scan of every string cell is
    expensive and this runs on every publish; a 5,000-row sample catches
    systematic contamination -- a column that has started carrying identifiers
    -- which is the failure mode that matters. It will not reliably catch a
    single stray row, and that limitation is stated in the report rather than
    left for somebody to discover.

    Numeric columns are skipped, and that is scoping rather than an oversight.
    A float column holds no punctuation for these patterns to key on, so every
    hit in one would be a coincidence of digits. The exposure the skip leaves
    is real and worth naming: pandas gives object dtype to any column with
    mixed types, so an identifier column arriving as text from a real extract
    *is* scanned, while the same identifiers arriving as integers are not. The
    classification layer is what covers a numeric identifier column; this layer
    covers text.
    """
    hits: list[ScanHit] = []
    frame = df.head(sample_rows) if len(df) > sample_rows else df

    for column in frame.columns:
        series = frame[column]
        if not (series.dtype == object or isinstance(series.dtype, pd.StringDtype)):
            continue
        text = series.dropna().astype(str)
        if text.empty:
            continue
        for name, pattern in IDENTIFIER_PATTERNS.items():
            matched = text[text.str.contains(pattern, regex=True, na=False)]
            if len(matched):
                hits.append(ScanHit(
                    table=table, column=column, pattern=name,
                    match_count=len(matched), example=str(matched.iloc[0]),
                ))
    return hits


# ---------------------------------------------------------------------------
# The egress gate
# ---------------------------------------------------------------------------


@dataclass
class EgressFinding:
    table: str
    column: str
    reason: str
    detail: str


CONTENT_MATCH_PREFIX = "content_match:"


def is_content_match(finding: EgressFinding) -> bool:
    """Whether a finding is a regex guess rather than a demonstrated leak.

    The two kinds of finding in this module make claims of different strength
    and the gate has to treat them differently.

    A classification finding -- ``unpseudonymised_identifier`` or
    ``undeclared`` -- is a statement about a column the contract already calls
    an identifier, verified by reading the values. It has no false-positive
    mode, so no written reason can excuse it.

    A content match is a heuristic firing on a value whose column says it
    should be something else. It has false positives by construction: any
    pattern that catches an identifier a source system started writing into a
    free-text field will sometimes catch prose or a number that looks like one.
    Treating it as unappealable meant an ordinary payer name could halt
    publication permanently with no way out, which is a worse failure than the
    one the check exists to prevent.
    """
    return finding.reason.startswith(CONTENT_MATCH_PREFIX)


PSEUDONYM_RE = re.compile(r"^[A-Z]{3}-[0-9A-F]{12}$")

# The shapes the *source systems* use for their own record numbers. Declaring
# them is what makes the final artifact scan possible: after everything is
# written, the published files are re-read as text and searched for these, so
# the boundary is verified against the bytes on disk rather than against the
# frames somebody remembered to pass through the gate.
#
# Note the deliberate near-collision: a raw id is `CLI-00234` and a surrogate is
# `CLI-6A2F91C4D0E8`. They share a prefix, so a human skimming a file cannot
# tell them apart -- which is exactly why this has to be a machine check.
SOURCE_ID_PATTERNS: dict[str, re.Pattern] = {
    "raw_client_id": re.compile(r"\bCLI-\d{4,6}\b"),
    "raw_provider_id": re.compile(r"\bPRV-\d{3,5}\b"),
    "raw_session_id": re.compile(r"\bSES-\d{6,8}\b"),
    "raw_auth_id": re.compile(r"\bAUTH-\d{5,7}\b"),
}

# Column names that carry a direct identifier wherever they appear, in any
# frame, sample, or nested payload. Derived from FIELD_CLASSIFICATION so the
# two cannot drift.
IDENTIFIER_COLUMNS: frozenset[str] = frozenset(
    column
    for columns in FIELD_CLASSIFICATION.values()
    for column, sensitivity in columns.items()
    if sensitivity is Sensitivity.DIRECT_IDENTIFIER
)

_PREFIXES = {"client_id": "CLI", "provider_id": "PRV",
             "session_id": "SES", "auth_id": "ATH"}


def redact_records(records: list[dict]) -> list[dict]:
    """Make a list of sample rows safe to publish.

    Quality checks attach example rows to their findings, which is what makes a
    failure actionable instead of merely alarming -- "3,100 rows are bad" is a
    statistic, "here are five of them" is a starting point. But those samples
    are serialised into the quality report and inlined into the dashboard, so
    they are an egress path like any other.

    This is applied inside ``CheckResult`` at construction rather than at the
    point each sample is built. There are five places that build samples today
    and there will be more; a chokepoint that cannot be forgotten is worth more
    than five call sites that currently remember.
    """
    out: list[dict] = []
    for record in records:
        clean = {}
        for key, value in record.items():
            if key in IDENTIFIER_COLUMNS:
                clean[key] = pseudonymise(value, _PREFIXES.get(key, "PSN")) if (
                    value is not None and str(value) != "") else value
                continue
            text = str(value)
            for pattern in SOURCE_ID_PATTERNS.values():
                text = pattern.sub("[redacted]", text)
            clean[key] = text if text != str(value) else value
        out.append(clean)
    return out


def scan_text_for_source_ids(text: str) -> dict[str, int]:
    """Count raw source identifiers in a blob of text."""
    return {
        name: len(pattern.findall(text))
        for name, pattern in SOURCE_ID_PATTERNS.items()
        if pattern.search(text)
    }


@dataclass
class ArtifactFinding:
    path: str
    pattern: str
    count: int


def scan_published_artifacts(paths) -> list[ArtifactFinding]:
    """Re-read every published file and look for raw identifiers.

    The last line of defence, and the only one that inspects what was actually
    written rather than what was intended to be written.

    Every other layer here checks a *frame* on its way to a file. That leaves a
    gap wherever something reaches a file without passing through a frame --
    a check's sample rows, an aggregate assembled after the gate ran, a log
    line. This closes it by giving up on knowing the paths and reading the
    bytes instead.

    It is deliberately dumb. It does not know what the file means; it knows
    what a source identifier looks like and that none should be here.
    """
    findings: list[ArtifactFinding] = []
    for path in paths:
        path = Path(path)
        if not path.exists() or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except (OSError, UnicodeDecodeError):
            continue
        for name, count in scan_text_for_source_ids(text).items():
            findings.append(ArtifactFinding(path=str(path), pattern=name, count=count))
    return findings


def is_pseudonymised(series: pd.Series) -> bool:
    """Whether a column's values have actually been through ``pseudonymise``.

    This is the difference between a gate that works and a gate that documents
    an intention. Marking a column "de-identified" in a config file asserts
    that a transformation ran; reading the values proves it. If someone adds a
    new export path that skips ``deidentify_for_export``, a declaration-based
    check waves it through and this one does not.

    The same principle as the rest of the pipeline: verify the artifact, not
    the plan.

    What it does not do: it recognises the *shape* ``pseudonymise`` emits, and
    a shape is not a strength. A surrogate derived under a published salt
    passes this function and is still reversible by anyone who has the salt.
    ``quality.check_pseudonym_salt`` is the check for that property; this one
    only proves the transformation ran.
    """
    values = series.dropna().astype(str)
    values = values[values.ne("")]
    if values.empty:
        return True
    return bool(values.str.match(PSEUDONYM_RE).all())


def check_egress(frames: dict[str, pd.DataFrame]) -> list[EgressFinding]:
    """Everything that must not cross the boundary, found before it does.

    Three ways a publish can be unsafe, and all three are checked because they
    fail independently:

    * a column classified as a direct identifier still holds raw values;
    * a column nobody has classified is present (unknown defaults to unsafe);
    * a value *looks* like an identifier whatever its column claims to be.

    Note what the first one checks. A direct identifier is not forbidden from
    the export -- it is forbidden from the export *in its raw form*. The
    pipeline pseudonymises it, and this check confirms that actually happened
    by inspecting the values rather than taking the transformation's word for
    it.

    The findings are returned together and separated by ``is_content_match``
    where the gate reads them, because the first two are proof and the third is
    a guess.
    """
    findings: list[EgressFinding] = []

    for table, df in frames.items():
        undeclared = set(undeclared_columns(table, df.columns))
        for column in df.columns:
            if column in undeclared:
                findings.append(EgressFinding(
                    table, column, "undeclared",
                    "Column is not in FIELD_CLASSIFICATION. Unclassified columns are "
                    "treated as direct identifiers: declare it before publishing."))
                continue
            if (classify(table, column) is Sensitivity.DIRECT_IDENTIFIER
                    and not is_pseudonymised(df[column])):
                findings.append(EgressFinding(
                    table, column, "unpseudonymised_identifier",
                    "Classified as a direct identifier and the values are still raw. "
                    "Route this frame through deidentify_for_export, or drop the "
                    "column."))

        for hit in scan_frame(df, table):
            findings.append(EgressFinding(
                hit.table, hit.column, f"{CONTENT_MATCH_PREFIX}{hit.pattern}",
                f"{hit.match_count} value(s) match the {hit.pattern} pattern "
                f"(e.g. {hit.redacted_example()}). This is a heuristic and it has "
                f"false positives: confirm the values, then either fix the source "
                f"or release phi_content_scan with a written reason."))

    return findings


def deidentify_for_export(frames: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """Prepare frames to cross the boundary.

    Direct identifiers are pseudonymised rather than dropped. Dropping them
    would break the join between the BI exports and anything a person needs to
    act on -- an at-risk list nobody can trace back to a child is not an
    actionable list, it is a statistic. A surrogate keeps the export usable
    while making it useless to anyone without the salt -- and useful to anyone
    with it, which is why the salt's handling is gated rather than assumed.

    The surrogate is stable across tables within a run, which is what makes the
    exports joinable to each other. It is stable across runs only when a salt
    is configured; under the ephemeral default described in ``_salt`` it is
    not, and week-to-week comparison of the exports is unavailable.
    """
    out: dict[str, pd.DataFrame] = {}

    for table, df in frames.items():
        copy = df.copy()
        for column in copy.columns:
            if classify(table, column) is Sensitivity.DIRECT_IDENTIFIER:
                copy[column] = pseudonymise_series(
                    copy[column], _PREFIXES.get(column, "PSN"))
        out[table] = copy
    return out


def classification_summary() -> pd.DataFrame:
    """The contract, as a table. Rendered into docs/DATA_DICTIONARY.md."""
    rows = [
        {"table": table, "column": column, "sensitivity": sensitivity.value}
        for table, columns in FIELD_CLASSIFICATION.items()
        for column, sensitivity in columns.items()
    ]
    return pd.DataFrame(rows).sort_values(["table", "column"]).reset_index(drop=True)
