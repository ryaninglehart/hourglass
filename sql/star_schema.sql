-- Hourglass warehouse: authorisation utilisation star schema
--
-- Every table states its grain in a comment. That is not documentation
-- politeness -- grain is the first thing that goes wrong in a dimensional
-- model and the first thing an interviewer asks about, so it is written down
-- where it cannot drift away from the code.
--
-- There are two fact tables and they are deliberately at different grains.
-- fact_session records what happened; fact_authorization records what was
-- permitted. They are never joined directly: sessions are aggregated to the
-- authorisation grain first. Joining them raw would repeat units_authorized
-- once per session and inflate the denominator of every utilisation figure.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- Dimensions
-- ---------------------------------------------------------------------------

-- GRAIN: one row per calendar day.
CREATE TABLE dim_date (
    date_key     INTEGER PRIMARY KEY,   -- YYYYMMDD
    full_date    TEXT    NOT NULL,
    year         INTEGER NOT NULL,
    quarter      INTEGER NOT NULL,
    month        INTEGER NOT NULL,
    month_name   TEXT    NOT NULL,
    year_month   TEXT    NOT NULL,
    day_of_week  INTEGER NOT NULL,
    day_name     TEXT    NOT NULL,
    is_weekend   INTEGER NOT NULL
);

-- GRAIN: one row per client per version of that client's record.
-- Slowly Changing Dimension, Type 2. A client whose payer changed has two
-- rows with adjacent, non-overlapping validity ranges. Facts join to the row
-- that was in effect on the event date, so history stays attributed to the
-- payer who was actually responsible at the time.
CREATE TABLE dim_client (
    client_key     INTEGER PRIMARY KEY,
    client_id      TEXT    NOT NULL,      -- natural key, repeats across versions
    version        INTEGER NOT NULL,
    age_years      INTEGER NOT NULL,
    age_band       TEXT    NOT NULL,
    home_center_id TEXT    NOT NULL,
    payer_id       TEXT    NOT NULL,
    change_reason  TEXT    NOT NULL,
    valid_from     TEXT    NOT NULL,
    valid_to       TEXT    NOT NULL,      -- 9999-12-31 for the current row
    is_current     INTEGER NOT NULL
);
CREATE INDEX ix_dim_client_natural ON dim_client (client_id, valid_from, valid_to);

-- GRAIN: one row per service code.
-- unit_basis is why this dimension has to exist. Summing a "units" column
-- across codes that measure units differently produces a number with no
-- meaning, so the conversion factor lives here rather than in a query.
CREATE TABLE dim_service (
    service_key     INTEGER PRIMARY KEY,
    service_code    TEXT    NOT NULL,
    service_name    TEXT    NOT NULL,
    discipline      TEXT    NOT NULL,
    unit_basis      TEXT    NOT NULL,     -- 15_min | per_session | unknown
    minutes_per_unit INTEGER NOT NULL
);

-- GRAIN: one row per provider. Type 1 -- corrections overwrite.
-- Terminated providers are retained: their historical sessions still happened.
CREATE TABLE dim_provider (
    provider_key INTEGER PRIMARY KEY,
    provider_id  TEXT    NOT NULL UNIQUE,
    role         TEXT    NOT NULL,
    discipline   TEXT    NOT NULL,
    center_id    TEXT    NOT NULL,
    hire_date    TEXT,
    term_date    TEXT,
    is_active    INTEGER NOT NULL
);

-- GRAIN: one row per centre.
CREATE TABLE dim_center (
    center_key  INTEGER PRIMARY KEY,
    center_id   TEXT NOT NULL UNIQUE,
    center_name TEXT NOT NULL,
    state       TEXT NOT NULL
);

-- GRAIN: one row per payer.
-- contract_type splits value-based from fee-for-service. Under a value-based
-- contract, undelivered authorised care is a quality-measure and revenue
-- problem, not just a scheduling one, so this attribute is the one the
-- utilisation report is most often sliced by.
CREATE TABLE dim_payer (
    payer_key     INTEGER PRIMARY KEY,
    payer_id      TEXT NOT NULL UNIQUE,
    payer_name    TEXT NOT NULL,
    contract_type TEXT NOT NULL           -- value_based | fee_for_service
);

-- ---------------------------------------------------------------------------
-- Facts
-- ---------------------------------------------------------------------------

-- GRAIN: one row per delivered therapy session.
-- uom_resolved is a measure-quality flag, not a business attribute. Rows where
-- it is 0 have a duration whose unit of measure could not be determined; their
-- measures are zero and they are excluded from utilisation. Keeping the rows
-- rather than dropping them means the size of the hole is queryable.
CREATE TABLE fact_session (
    session_id        TEXT PRIMARY KEY,
    date_key          INTEGER NOT NULL REFERENCES dim_date (date_key),
    client_key        INTEGER NOT NULL REFERENCES dim_client (client_key),
    provider_key      INTEGER NOT NULL REFERENCES dim_provider (provider_key),
    service_key       INTEGER NOT NULL REFERENCES dim_service (service_key),
    center_key        INTEGER NOT NULL REFERENCES dim_center (center_key),
    units_delivered   REAL    NOT NULL DEFAULT 0,
    minutes_delivered REAL    NOT NULL DEFAULT 0,
    uom_resolved      INTEGER NOT NULL,
    unresolved_reason TEXT,
    is_completed      INTEGER NOT NULL,
    is_cancelled      INTEGER NOT NULL,
    is_no_show        INTEGER NOT NULL,
    source_system     TEXT    NOT NULL
);
CREATE INDEX ix_fact_session_date   ON fact_session (date_key);
CREATE INDEX ix_fact_session_client ON fact_session (client_key);
CREATE INDEX ix_fact_session_svc    ON fact_session (service_key);

-- GRAIN: one row per authorisation line -- one client, one service, one period.
-- Note the two date keys. An authorisation is not an event on a day, it is a
-- permission spanning a range, so it carries a start and an end rather than a
-- single date_key. This is why it cannot share a grain with fact_session.
--
-- The grain carries an assumption the schema cannot enforce: for one client and
-- one service, the periods must not intersect. Every roll-up in this project
-- attributes a session by client, service and date BETWEEN the two keys, so a
-- session inside an overlap is counted in full against both authorisations. No
-- constraint here can express "these two ranges must not intersect", and real
-- payers amend and reissue, so the rule is enforced where it can be:
-- quality.check_overlapping_authorization_periods, at BLOCK.
CREATE TABLE fact_authorization (
    auth_id          TEXT PRIMARY KEY,
    client_key       INTEGER NOT NULL REFERENCES dim_client (client_key),
    service_key      INTEGER NOT NULL REFERENCES dim_service (service_key),
    payer_key        INTEGER NOT NULL REFERENCES dim_payer (payer_key),
    period_start_key INTEGER NOT NULL REFERENCES dim_date (date_key),
    period_end_key   INTEGER NOT NULL REFERENCES dim_date (date_key),
    units_authorized REAL    NOT NULL,
    authorized_days  INTEGER NOT NULL
);
CREATE INDEX ix_fact_auth_client ON fact_authorization (client_key);
CREATE INDEX ix_fact_auth_period ON fact_authorization (period_start_key, period_end_key);

-- ---------------------------------------------------------------------------
-- Run log
-- ---------------------------------------------------------------------------

-- GRAIN: one row per pipeline run, published or blocked. Append-only.
-- The rule-set hash matters: without it the log records that checks passed but
-- not which version of the checks, and an old verdict could be mistaken for a
-- statement about the current rules.
--
-- Blocked runs belong here as much as published ones. For a while they did not:
-- a blocked run's row went only to the quarantined warehouse, which the next
-- blocked run overwrote, so the log held nothing but successes and read as if
-- the gate had never fired. `pipeline.append_run_log_row` is what puts them
-- back; the columns below are the same either way, with `published = 0`.
--
-- refused_acknowledgements records the releases that were attempted and denied.
-- Somebody trying to sign off a PHI failure is a more interesting event than
-- any successful release, and it used to survive only in a quality report the
-- next run overwrote.
CREATE TABLE run_log (
    run_id           TEXT PRIMARY KEY,
    started_at_utc   TEXT    NOT NULL,
    finished_at_utc  TEXT    NOT NULL,
    code_version     TEXT    NOT NULL,
    ruleset_version  TEXT    NOT NULL,
    ruleset_hash     TEXT    NOT NULL,
    published        INTEGER NOT NULL,
    blocking_failures TEXT,
    acknowledgements TEXT,
    refused_acknowledgements TEXT,
    session_rows     INTEGER NOT NULL,
    auth_rows        INTEGER NOT NULL,
    lake_backend     TEXT    NOT NULL
);
