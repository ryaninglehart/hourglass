-- Hourglass -- analytical queries against the star schema
--
-- Every query states its grain first. "What does one row of this result mean"
-- is the question that catches a fanned-out join before it reaches a
-- dashboard, and writing the answer down costs nothing.
--
-- Run them:  sqlite3 data/out/hourglass.db < sql/analytics.sql


-- =========================================================================
-- 1. Delivered volume by month
-- GRAIN: one row per calendar month.
--
-- uom_coverage rides along with the volume deliberately. Hours delivered
-- without its coverage is a number somebody will quote in a meeting.
-- =========================================================================
SELECT
    d.year_month,
    COUNT(*)                                          AS sessions,
    SUM(f.is_completed)                               AS completed,
    ROUND(SUM(f.minutes_delivered) / 60.0, 1)         AS hours_delivered,
    -- Over completed sessions only, which is not a detail. A cancelled
    -- session has no duration to resolve, so including cancellations dilutes
    -- the rate towards whatever the cancellation rate happens to be and the
    -- number stops meaning "how much of the delivered care can we measure".
    --
    -- It also has to match `analytics.coverage_by_month`, which is what
    -- `quality.check_coverage_step_change` gates on and what the quality
    -- report prints. Without the filter this query returned 0.9209 for
    -- 2026-04 while the quality report returned 0.9204 -- same name, same
    -- month, two artifacts, two numbers, and no way for a reader to tell
    -- which one to believe.
    ROUND(AVG(CASE WHEN f.is_completed = 1 THEN f.uom_resolved END), 4)
                                                      AS uom_coverage
FROM fact_session f
JOIN dim_date d ON d.date_key = f.date_key
GROUP BY d.year_month
ORDER BY d.year_month;


-- =========================================================================
-- 2. Authorisation utilisation
-- GRAIN: one row per authorisation.
--
-- The important part is the subquery. fact_session and fact_authorization sit
-- at different grains, so sessions are aggregated to the authorisation grain
-- BEFORE the join. Joining them directly would repeat units_authorized once
-- per session and quietly divide every utilisation figure by the session
-- count. Query 3 shows exactly that failure.
--
-- Note the join is on the natural client_id, not client_key. client_key is a
-- Type 2 surrogate: an authorisation spanning a payer change would match only
-- half its own sessions if it joined on the surrogate.
--
-- The BETWEEN below assumes one client's authorisations for one service do not
-- overlap in time. If two do, every session in the intersection joins to both
-- and is counted twice, here and in every other roll-up written this way --
-- including the pandas one it is checked against, which is why metric parity
-- cannot see it. quality.check_overlapping_authorization_periods is what
-- guarantees the assumption holds before any of this is read.
-- =========================================================================
WITH delivered AS (
    SELECT
        c.client_id,
        f.service_key,
        f.date_key,
        f.units_delivered
    FROM fact_session f
    JOIN dim_client c ON c.client_key = f.client_key
    WHERE f.is_completed = 1
      AND f.uom_resolved = 1
)
SELECT
    a.auth_id,
    ac.client_id,
    s.service_code,
    s.discipline,
    p.payer_name,
    p.contract_type,
    a.units_authorized,
    COALESCE(SUM(dl.units_delivered), 0)                            AS units_delivered,
    ROUND(COALESCE(SUM(dl.units_delivered), 0) / a.units_authorized, 4)
                                                                    AS utilization
FROM fact_authorization a
JOIN dim_client  ac ON ac.client_key  = a.client_key
JOIN dim_service s  ON s.service_key  = a.service_key
JOIN dim_payer   p  ON p.payer_key    = a.payer_key
LEFT JOIN delivered dl
       ON dl.client_id   = ac.client_id
      AND dl.service_key = a.service_key
      AND dl.date_key BETWEEN a.period_start_key AND a.period_end_key
GROUP BY a.auth_id, ac.client_id, s.service_code, s.discipline,
         p.payer_name, p.contract_type, a.units_authorized
ORDER BY utilization
LIMIT 25;


-- =========================================================================
-- 3. The grain trap, demonstrated on real rows
-- GRAIN: one row. Two numbers that should agree, and do not.
--
-- Left column: sessions aggregated to the authorisation grain first (correct).
-- Right column: the two fact tables joined row-to-row (wrong).
--
-- The wrong one throws no error. It returns a smaller, entirely plausible
-- percentage, because units_authorized has been repeated once per session.
-- Run it and read the two numbers.
-- =========================================================================
-- Both sides apply the SAME session filter (completed and resolvable). The
-- only difference between them is the grain: `correct` sums authorised units
-- once per authorisation, `wrong` sums them once per matching session. Keeping
-- the filter identical matters -- otherwise the ratio below would conflate the
-- grain error with a filtering difference and the demonstration would be
-- arguable rather than clean.
WITH correct AS (
    SELECT SUM(a.units_authorized) AS auth_units
    FROM fact_authorization a
),
wrong AS (
    SELECT SUM(a.units_authorized) AS auth_units
    FROM fact_authorization a
    JOIN dim_client ac ON ac.client_key = a.client_key
    JOIN dim_client sc ON sc.client_id  = ac.client_id
    JOIN fact_session f
      ON f.client_key   = sc.client_key
     AND f.service_key  = a.service_key
     AND f.date_key BETWEEN a.period_start_key AND a.period_end_key
    WHERE f.is_completed = 1
      AND f.uom_resolved = 1
)
SELECT
    (SELECT auth_units FROM correct)                                AS authorized_units_correct,
    (SELECT auth_units FROM wrong)                                  AS authorized_units_fanned_out,
    ROUND((SELECT auth_units FROM wrong) * 1.0
          / (SELECT auth_units FROM correct), 1)                    AS inflation_factor;


-- =========================================================================
-- 4. Where the measure is thin
-- GRAIN: one row per month per resolution outcome.
--
-- Locating a defect in time turns an open-ended cleanup into one question for
-- one vendor about one release.
-- =========================================================================
SELECT
    d.year_month,
    COALESCE(f.unresolved_reason, 'resolved')         AS resolution,
    COUNT(*)                                          AS sessions,
    ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (PARTITION BY d.year_month), 2)
                                                      AS pct_of_month
FROM fact_session f
JOIN dim_date d ON d.date_key = f.date_key
GROUP BY d.year_month, resolution
ORDER BY d.year_month, sessions DESC;


-- =========================================================================
-- 5. Utilisation by payer contract type
-- GRAIN: one row per contract type.
--
-- Under a value-based contract, authorised care that goes undelivered is not
-- only a scheduling problem -- it moves the outcome measures the contract pays
-- on. This is the cut the split exists for.
-- =========================================================================
WITH delivered AS (
    SELECT c.client_id, f.service_key, f.date_key, f.units_delivered
    FROM fact_session f
    JOIN dim_client c ON c.client_key = f.client_key
    WHERE f.is_completed = 1 AND f.uom_resolved = 1
),
per_auth AS (
    SELECT
        p.contract_type,
        a.units_authorized,
        COALESCE(SUM(dl.units_delivered), 0) AS units_delivered,
        a.units_authorized * s.minutes_per_unit / 60.0 AS hours_authorized,
        COALESCE(SUM(dl.units_delivered), 0) * s.minutes_per_unit / 60.0
                                             AS hours_delivered
    FROM fact_authorization a
    JOIN dim_client  ac ON ac.client_key = a.client_key
    JOIN dim_service s  ON s.service_key = a.service_key
    JOIN dim_payer   p  ON p.payer_key   = a.payer_key
    LEFT JOIN delivered dl
           ON dl.client_id   = ac.client_id
          AND dl.service_key = a.service_key
          AND dl.date_key BETWEEN a.period_start_key AND a.period_end_key
    GROUP BY a.auth_id, p.contract_type, a.units_authorized, s.minutes_per_unit
)
SELECT
    contract_type,
    COUNT(*)                                                AS authorizations,
    -- Hours come from each service's own minutes_per_unit, carried through
    -- from the CTE. Dividing units by 4 across the board treats a 45-minute
    -- speech session as 15 minutes.
    ROUND(SUM(hours_authorized), 0)                         AS hours_authorized,
    ROUND(SUM(hours_delivered),  0)                         AS hours_delivered,
    -- Weighted, not an average of ratios. AVG(utilization) here would give a
    -- 26-unit speech authorisation the same say as a 2,000-unit ABA one.
    ROUND(SUM(units_delivered) / SUM(units_authorized), 4)  AS utilization_weighted,
    ROUND(AVG(units_delivered / units_authorized), 4)       AS utilization_mean_of_ratios
FROM per_auth
GROUP BY contract_type;


-- =========================================================================
-- 6. Slowly changing dimension, read back
-- GRAIN: one row per client version, for clients that changed payer.
--
-- The point of Type 2: sessions before the change still belong to the payer
-- who was responsible then. Overwriting would have re-attributed them.
-- =========================================================================
SELECT
    c.client_id,
    c.version,
    c.payer_id,
    c.change_reason,
    c.valid_from,
    c.valid_to,
    c.is_current,
    COUNT(f.session_id)                                     AS sessions_in_version
FROM dim_client c
LEFT JOIN fact_session f ON f.client_key = c.client_key
WHERE c.client_id IN (
    SELECT client_id FROM dim_client GROUP BY client_id HAVING COUNT(*) > 1
)
GROUP BY c.client_key
ORDER BY c.client_id, c.version
LIMIT 30;


-- =========================================================================
-- 7. Expiring authorisations with unused hours
-- GRAIN: one row per authorisation expiring within 30 days of the last
--        observed session date.
--
-- This is the query the whole pipeline exists to make possible. Each row is a
-- child with approved therapy hours that are about to expire unused. Nobody
-- inside the treatment room can see it, because it only appears when the
-- authorisation system and the scheduling system are read together.
-- =========================================================================
WITH as_of AS (
    SELECT MAX(date_key) AS date_key FROM fact_session
),
delivered AS (
    SELECT c.client_id, f.service_key, f.date_key, f.units_delivered
    FROM fact_session f
    JOIN dim_client c ON c.client_key = f.client_key
    WHERE f.is_completed = 1 AND f.uom_resolved = 1
)
SELECT
    ac.client_id,
    s.service_code,
    s.service_name,
    p.payer_name,
    p.contract_type,
    de.full_date                                            AS expires_on,
    JULIANDAY(de.full_date)
        - JULIANDAY((SELECT full_date FROM dim_date
                     WHERE date_key = (SELECT date_key FROM as_of)))
                                                            AS days_to_expiry,
    -- Each service converts with its own minutes_per_unit. A flat /4 here
    -- would understate speech and medical authorisations by two to three
    -- times -- and since this list is ordered by hours at risk, it would push
    -- them off the bottom of the page.
    ROUND(a.units_authorized * s.minutes_per_unit / 60.0, 1)      AS hours_authorized,
    ROUND(COALESCE(SUM(dl.units_delivered), 0) * s.minutes_per_unit / 60.0, 1)
                                                                  AS hours_delivered,
    ROUND((a.units_authorized - COALESCE(SUM(dl.units_delivered), 0))
          * s.minutes_per_unit / 60.0, 1)                         AS hours_at_risk
FROM fact_authorization a
JOIN dim_client  ac ON ac.client_key  = a.client_key
JOIN dim_service s  ON s.service_key  = a.service_key
JOIN dim_payer   p  ON p.payer_key    = a.payer_key
JOIN dim_date    de ON de.date_key    = a.period_end_key
LEFT JOIN delivered dl
       ON dl.client_id   = ac.client_id
      AND dl.service_key = a.service_key
      AND dl.date_key BETWEEN a.period_start_key AND a.period_end_key
WHERE a.period_end_key   >= (SELECT date_key FROM as_of)
  AND a.period_start_key <= (SELECT date_key FROM as_of)
GROUP BY a.auth_id
HAVING days_to_expiry <= 30
   AND (a.units_authorized - COALESCE(SUM(dl.units_delivered), 0))
       / a.units_authorized >= 0.25
ORDER BY days_to_expiry, hours_at_risk DESC
LIMIT 30;


-- =========================================================================
-- 8. Provider workload, with a window function
-- GRAIN: one row per provider.
--
-- Rank within discipline rather than overall: an SLP and an RBT do not carry
-- comparable session loads and ranking them together says nothing.
-- =========================================================================
SELECT
    pr.provider_id,
    pr.role,
    pr.discipline,
    pr.is_active,
    COUNT(*)                                                AS sessions,
    ROUND(SUM(f.minutes_delivered) / 60.0, 1)               AS hours,
    RANK() OVER (PARTITION BY pr.discipline ORDER BY COUNT(*) DESC)
                                                            AS rank_in_discipline,
    ROUND(100.0 * COUNT(*)
          / SUM(COUNT(*)) OVER (PARTITION BY pr.discipline), 2)
                                                            AS pct_of_discipline
FROM fact_session f
JOIN dim_provider pr ON pr.provider_key = f.provider_key
WHERE f.is_completed = 1
GROUP BY pr.provider_key
ORDER BY pr.discipline, rank_in_discipline
LIMIT 30;


-- =========================================================================
-- 9. WHERE versus HAVING, on the same question
-- GRAIN: one row per centre.
--
-- WHERE filters rows before grouping; HAVING filters groups after. The two are
-- not interchangeable and the difference is not stylistic: it changes what the
-- aggregate counted.
--
-- Both are computed side by side below. `no_show_pct_all_rows` divides by every
-- session at the centre. `no_show_pct_completed_only` restricts the denominator
-- to completed sessions -- which is what you get if you push the status filter
-- into a WHERE clause -- and it is systematically higher, because the rows it
-- removed were in the denominator and never in the numerator.
--
-- Same table, same question in English, two different answers.
-- =========================================================================
SELECT
    ct.center_name,
    COUNT(*)                                                AS all_sessions,
    SUM(CASE WHEN f.is_completed = 1 THEN 1 ELSE 0 END)     AS completed_sessions,
    SUM(CASE WHEN f.is_no_show   = 1 THEN 1 ELSE 0 END)     AS no_shows,
    -- denominator: every session
    ROUND(100.0 * SUM(CASE WHEN f.is_no_show = 1 THEN 1 ELSE 0 END)
          / COUNT(*), 2)                                    AS no_show_pct_all_rows,
    -- denominator: completed sessions only, i.e. the filter moved to WHERE
    ROUND(100.0 * SUM(CASE WHEN f.is_no_show = 1 THEN 1 ELSE 0 END)
          / NULLIF(SUM(CASE WHEN f.is_completed = 1 THEN 1 ELSE 0 END), 0), 2)
                                                            AS no_show_pct_completed_only
FROM fact_session f
JOIN dim_center ct ON ct.center_key = f.center_key
GROUP BY ct.center_key
HAVING COUNT(*) > 100        -- filters centres, AFTER counting every session
ORDER BY no_show_pct_all_rows DESC;


-- =========================================================================
-- 10. The run log
-- GRAIN: one row per pipeline run.
--
-- Which rule set produced which verdict, and who released a blocking failure
-- and why. Without the rule-set hash this table records that checks passed but
-- not which checks.
-- =========================================================================
SELECT
    run_id,
    finished_at_utc,
    code_version,
    ruleset_version,
    ruleset_hash,
    published,
    blocking_failures,
    acknowledgements,
    session_rows,
    lake_backend
FROM run_log
ORDER BY finished_at_utc DESC
LIMIT 10;
