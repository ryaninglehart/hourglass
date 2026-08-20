"""Synthetic source-system extracts.

Every row this module produces is fabricated. There is no real patient data in
this project and none was used to build it. The shape of the data -- CPT codes,
15-minute unit billing, six-month authorisation periods, the mix of disciplines
-- follows publicly documented paediatric therapy billing practice so that the
modelling problems are the real ones.

Three extracts are produced, standing in for three of the source systems named
in the role this project was written for:

    salesforce_clients.csv     CRM  -> client roster, one row per state change
    payer_authorizations.csv   API  -> authorised units per client/service/period
    ehr_sessions.csv           EHR  -> delivered sessions

The EHR extract carries a deliberate defect. See docs/ANOMALY.md.
"""

from __future__ import annotations

import csv
import random
from datetime import date, timedelta
from pathlib import Path

from .config import GEN, RAW, SERVICES, UOM_MIGRATION_DATE, GeneratorConfig, ensure_dirs

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def _weekdays(start: date, end: date) -> list[date]:
    return [d for d in _daterange(start, end) if d.weekday() < 5]


def _weighted_choice(rng: random.Random, options: list[tuple], weight_index: int):
    weights = [o[weight_index] for o in options]
    return rng.choices(options, weights=weights, k=1)[0]


def _write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# reference data
# ---------------------------------------------------------------------------


def _build_centers(cfg: GeneratorConfig) -> list[dict]:
    return [
        {"center_id": cid, "center_name": name, "state": state}
        for cid, name, state in cfg.centers
    ]


def _build_payers(cfg: GeneratorConfig) -> list[dict]:
    return [
        {"payer_id": pid, "payer_name": name, "contract_type": ctype}
        for pid, name, ctype in cfg.payers
    ]


def _build_providers(cfg: GeneratorConfig, rng: random.Random) -> list[dict]:
    providers = []
    roles = list(cfg.roles)
    for i in range(cfg.n_providers):
        role, discipline, _ = _weighted_choice(rng, roles, 2)
        center = rng.choice(cfg.centers)[0]
        # Behaviour technician turnover in this industry is famously high, so a
        # meaningful share of providers have a termination date inside the
        # window. The pipeline has to keep their historical sessions.
        hire = cfg.start_date - timedelta(days=rng.randint(30, 900))
        term = None
        if role == "RBT" and rng.random() < 0.28:
            term = cfg.start_date + timedelta(days=rng.randint(20, 210))
        providers.append(
            {
                "provider_id": f"PRV-{i + 1:04d}",
                "role": role,
                "discipline": discipline,
                "center_id": center,
                "hire_date": hire.isoformat(),
                "term_date": term.isoformat() if term else "",
            }
        )
    return providers


# ---------------------------------------------------------------------------
# clients -- emitted as a change log so the warehouse has to build SCD Type 2
# ---------------------------------------------------------------------------


def _build_client_records(cfg: GeneratorConfig, rng: random.Random) -> tuple[list[dict], list[dict]]:
    """Return (change_log_rows, client_master).

    The CRM does not hand us a tidy current-state table. It hands us one row per
    time the record changed, with an effective date. Turning that into a
    dimension you can join to history is the SCD Type 2 exercise.
    """
    change_rows: list[dict] = []
    master: list[dict] = []

    for i in range(cfg.n_clients):
        client_id = f"CLI-{i + 1:05d}"
        center = rng.choice(cfg.centers)[0]
        # Cortica's population is paediatric; ages skew young.
        age = rng.choices([2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 14, 16],
                          weights=[6, 11, 13, 13, 12, 10, 8, 7, 6, 4, 4, 3, 3], k=1)[0]
        enrolled = cfg.start_date - timedelta(days=rng.randint(0, 540))
        payer = rng.choice(cfg.payers)[0]

        change_rows.append(
            {
                "client_id": client_id,
                "effective_date": enrolled.isoformat(),
                "age_years": age,
                "home_center_id": center,
                "payer_id": payer,
                "change_reason": "enrollment",
            }
        )

        current_payer = payer
        # A payer change mid-year is the classic reason a dimension needs
        # history: sessions before the change belong to the old payer.
        if rng.random() < cfg.payer_change_rate:
            new_payer = rng.choice([p[0] for p in cfg.payers if p[0] != current_payer])
            change_date = cfg.start_date + timedelta(days=rng.randint(35, 190))
            change_rows.append(
                {
                    "client_id": client_id,
                    "effective_date": change_date.isoformat(),
                    "age_years": age,
                    "home_center_id": center,
                    "payer_id": new_payer,
                    "change_reason": "payer_change",
                }
            )
            current_payer = new_payer

        master.append(
            {
                "client_id": client_id,
                "age_years": age,
                "home_center_id": center,
                "enrolled_date": enrolled,
                "payer_history": [
                    (r["effective_date"], r["payer_id"])
                    for r in change_rows
                    if r["client_id"] == client_id
                ],
            }
        )

    change_rows.sort(key=lambda r: (r["client_id"], r["effective_date"]))
    return change_rows, master


def _payer_on(master_row: dict, when: date) -> str:
    """Which payer was responsible on a given date."""
    payer = master_row["payer_history"][0][1]
    for eff, pid in master_row["payer_history"]:
        if date.fromisoformat(eff) <= when:
            payer = pid
    return payer


# ---------------------------------------------------------------------------
# authorisations
# ---------------------------------------------------------------------------


def _build_authorizations(cfg: GeneratorConfig, master: list[dict],
                          rng: random.Random) -> list[dict]:
    """One row per client / service / authorisation period.

    This is the grain of fact_authorization and it is deliberately different
    from the grain of fact_session. That difference is the whole modelling
    point: you cannot join them directly without fanning out the measures.
    """
    rows: list[dict] = []
    auth_seq = 0

    for m in master:
        # Every client has an ABA treatment authorisation. Cortica's published
        # model is 15-20 hours per week rather than the 25-40 that is standard
        # elsewhere, so the authorised volume here reflects that.
        weekly_hours = rng.uniform(14.0, 21.0)

        services = ["97153"]
        if rng.random() < 0.85:
            services.append("97155")
        if rng.random() < 0.70:
            services.append("97156")
        if rng.random() < 0.45:
            services.append("92507")
        if rng.random() < 0.38:
            services.append("97530")
        if rng.random() < 0.30:
            services.append("99213")
        if rng.random() < 0.22:
            services.append("97151")

        # Authorisation periods are staggered but always begin inside the
        # observation window. A period that opened before the window would be
        # only partially observable, and its final utilisation would look
        # catastrophic for a reason that is an artefact of where the extract
        # starts rather than anything that happened to a child.
        period_start = cfg.start_date + timedelta(days=rng.randint(0, 60))
        while period_start <= cfg.end_date:
            # Payers issue authorisations of different lengths -- quarterly,
            # four-month and six-month are all common -- and re-authorise at a
            # different length than last time. Drawing per period rather than
            # per client matters practically: with one length per client every
            # expiry lands on the same handful of days and the expiring-soon
            # report is empty most weeks.
            period_days = rng.choice(cfg.auth_period_day_options)
            period_end = period_start + timedelta(days=period_days - 1)
            weeks = period_days / 7.0

            for code in services:
                spec = next(s for s in SERVICES if s["service_code"] == code)
                if code == "97153":
                    hours = weekly_hours * weeks
                elif code == "97155":
                    hours = weekly_hours * 0.12 * weeks
                elif code == "97156":
                    hours = 1.0 * weeks
                elif code == "97151":
                    hours = 8.0 * (weeks / 26.0)
                elif code == "92507" or code == "97530":
                    hours = 0.75 * weeks
                else:  # 99213
                    hours = 0.5 * (weeks / 6.0) * 6

                if spec["unit_basis"] == "15_min":
                    units = max(1, round(hours * 4))
                else:
                    units = max(1, round(hours * 60 / spec["minutes_per_unit"]))

                auth_seq += 1
                rows.append(
                    {
                        "auth_id": f"AUTH-{auth_seq:06d}",
                        "client_id": m["client_id"],
                        "payer_id": _payer_on(m, period_start),
                        "service_code": code,
                        "units_authorized": units,
                        "period_start": period_start.isoformat(),
                        "period_end": period_end.isoformat(),
                        "issued_date": (period_start - timedelta(days=rng.randint(5, 25))).isoformat(),
                    }
                )
            period_start = period_end + timedelta(days=1)

    return rows


# ---------------------------------------------------------------------------
# sessions -- and the defect
# ---------------------------------------------------------------------------


def _build_sessions(cfg: GeneratorConfig, master: list[dict], providers: list[dict],
                    auths: list[dict], rng: random.Random) -> list[dict]:
    by_client: dict[str, list[dict]] = {}
    for a in auths:
        by_client.setdefault(a["client_id"], []).append(a)

    providers_by_discipline: dict[str, list[dict]] = {}
    for p in providers:
        providers_by_discipline.setdefault(p["discipline"], []).append(p)

    rows: list[dict] = []
    seq = 0
    days = _weekdays(cfg.start_date, cfg.end_date)

    for m in master:
        client_auths = by_client.get(m["client_id"], [])
        if not client_auths:
            continue

        # Delivery intensity varies by client. Most land in the target band;
        # some genuinely under-deliver, which is the business problem this
        # pipeline exists to surface -- not a data defect.
        intensity = rng.choices(
            [rng.uniform(0.55, 0.78), rng.uniform(0.86, 0.99), rng.uniform(0.99, 1.06)],
            weights=[0.24, 0.62, 0.14], k=1,
        )[0]

        for auth in client_auths:
            spec = next(s for s in SERVICES if s["service_code"] == auth["service_code"])
            p_start = date.fromisoformat(auth["period_start"])
            p_end = date.fromisoformat(auth["period_end"])
            window = [d for d in days if p_start <= d <= p_end]
            if not window:
                continue

            # An authorisation period routinely extends past the end of the
            # observation window. Only the overlapping slice can contain
            # sessions, so the delivery target is scaled to that slice. Without
            # this, a period that opens two weeks before the extract ends
            # receives a full period of sessions crammed into ten weekdays, and
            # every downstream pace figure inherits the nonsense. That bug was
            # in the first working version: headline pace read 111% while every
            # per-payer figure read 83%.
            period_weekdays = sum(1 for _ in _weekdays(p_start, p_end)) or 1
            observed_fraction = min(1.0, len(window) / period_weekdays)

            target_units = auth["units_authorized"] * intensity * observed_fraction
            if spec["unit_basis"] == "15_min":
                units_per_session = {"97153": 12, "97155": 4, "97156": 4,
                                     "97151": 8, "97530": 4}.get(auth["service_code"], 4)
            else:
                units_per_session = 1

            n_sessions = max(0, round(target_units / units_per_session))
            if n_sessions == 0:
                continue

            # A client cannot be seen more than once a day by the same
            # provider for the same service, so the number of sessions is
            # capped at the number of available weekdays. When the cap binds,
            # the same authorised volume is delivered in fewer, longer visits
            # rather than being silently lost -- which is what a clinic
            # actually does, and it keeps delivered units tracking the
            # intensity the client was assigned instead of an artefact of the
            # calendar.
            if n_sessions > len(window):
                units_per_session *= n_sessions / len(window)
                n_sessions = len(window)

            pool = providers_by_discipline.get(spec["discipline"]) or providers
            # Sample days WITHOUT replacement so a client is not scheduled with
            # the same provider for the same service twice on one day. That is
            # what makes the deduplication business key in transform.py sound:
            # if the generator produced same-day repeats, every one of them
            # would be discarded as a false duplicate.
            chosen_days = (rng.sample(window, n_sessions) if n_sessions <= len(window)
                           else list(window))

            for d in sorted(chosen_days):
                seq += 1
                provider = rng.choice(pool)

                roll = rng.random()
                if roll < cfg.no_show_rate:
                    status = "no_show"
                elif roll < cfg.no_show_rate + cfg.cancel_rate:
                    status = "cancelled"
                else:
                    status = "completed"

                if status == "completed":
                    units = max(1, round(
                        rng.gauss(units_per_session, units_per_session * 0.18)))
                else:
                    units = 0

                minutes = units * spec["minutes_per_unit"]

                # ---- the defect -------------------------------------------
                # Before the migration the EHR reported minutes. On and after
                # it, the same column reports 15-minute units. A slice of the
                # post-migration rows lost the unit-of-measure flag entirely.
                if d < UOM_MIGRATION_DATE:
                    duration_value, duration_uom = minutes, "minutes"
                else:
                    duration_value = units if spec["unit_basis"] == "15_min" else 1
                    duration_uom = "units"
                    if rng.random() < cfg.null_uom_rate:
                        duration_uom = ""     # <-- unknown unit. Not recoverable.
                # -----------------------------------------------------------

                row = {
                    "session_id": f"SES-{seq:07d}",
                    "client_id": m["client_id"],
                    "provider_id": provider["provider_id"],
                    "service_code": auth["service_code"],
                    "service_date": d.isoformat(),
                    "center_id": provider["center_id"],
                    "duration_value": duration_value,
                    "duration_uom": duration_uom,
                    "status": status,
                    "source_system": "ehr",
                }
                rows.append(row)

                # Double-entry: the same visit submitted twice. Real, common,
                # and it inflates delivered units if you do not deduplicate.
                if rng.random() < cfg.duplicate_rate:
                    seq += 1
                    dup = dict(row)
                    dup["session_id"] = f"SES-{seq:07d}"
                    rows.append(dup)

        # Care delivered with no authorisation covering it. Two flavours, and
        # they are different problems that a single "bad rows" bucket would
        # merge into one useless number:
        #
        #   1. a real, billable service the client has no authorisation for --
        #      the measure is correct, the revenue is at risk;
        #   2. a service code that is not in the catalogue at all -- the
        #      measure cannot be computed, and the code needs mapping.
        authorized_codes = {a["service_code"] for a in client_auths}
        unauthorized_codes = [s["service_code"] for s in SERVICES
                              if s["service_code"] not in authorized_codes]

        if unauthorized_codes and rng.random() < cfg.unauthorized_session_rate * 10:
            for _ in range(rng.randint(1, 4)):
                seq += 1
                d = rng.choice(days)
                code = rng.choice(unauthorized_codes)
                spec = next(s for s in SERVICES if s["service_code"] == code)
                provider = rng.choice(
                    providers_by_discipline.get(spec["discipline"], providers))
                units = rng.randint(2, 8)
                if d < UOM_MIGRATION_DATE:
                    dv, uom = units * spec["minutes_per_unit"], "minutes"
                else:
                    dv, uom = units, "units"
                rows.append({
                    "session_id": f"SES-{seq:07d}",
                    "client_id": m["client_id"],
                    "provider_id": provider["provider_id"],
                    "service_code": code,
                    "service_date": d.isoformat(),
                    "center_id": provider["center_id"],
                    "duration_value": dv,
                    "duration_uom": uom,
                    "status": "completed",
                    "source_system": "ehr",
                })

        if rng.random() < cfg.unmapped_code_rate:
            seq += 1
            d = rng.choice(days)
            provider = rng.choice(providers_by_discipline.get("ABA", providers))
            rows.append({
                "session_id": f"SES-{seq:07d}",
                "client_id": m["client_id"],
                "provider_id": provider["provider_id"],
                "service_code": "97158",   # real CPT, absent from the catalogue
                "service_date": d.isoformat(),
                "center_id": provider["center_id"],
                "duration_value": rng.randint(2, 6),
                "duration_uom": "units" if d >= UOM_MIGRATION_DATE else "minutes",
                "status": "completed",
                "source_system": "ehr",
            })

    rows.sort(key=lambda r: (r["service_date"], r["session_id"]))
    return rows


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def generate(cfg: GeneratorConfig = GEN, out_dir: Path | None = None) -> dict[str, Path]:
    """Write all source extracts. Deterministic for a given seed."""
    ensure_dirs()
    out_dir = out_dir or RAW
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(cfg.seed)

    centers = _build_centers(cfg)
    payers = _build_payers(cfg)
    providers = _build_providers(cfg, rng)
    client_changes, master = _build_client_records(cfg, rng)
    auths = _build_authorizations(cfg, master, rng)
    sessions = _build_sessions(cfg, master, providers, auths, rng)

    paths = {
        "centers": out_dir / "reference_centers.csv",
        "payers": out_dir / "reference_payers.csv",
        "providers": out_dir / "reference_providers.csv",
        "clients": out_dir / "salesforce_clients.csv",
        "authorizations": out_dir / "payer_authorizations.csv",
        "sessions": out_dir / "ehr_sessions.csv",
    }

    _write_csv(paths["centers"], centers, ["center_id", "center_name", "state"])
    _write_csv(paths["payers"], payers, ["payer_id", "payer_name", "contract_type"])
    _write_csv(paths["providers"], providers,
               ["provider_id", "role", "discipline", "center_id", "hire_date", "term_date"])
    _write_csv(paths["clients"], client_changes,
               ["client_id", "effective_date", "age_years", "home_center_id",
                "payer_id", "change_reason"])
    _write_csv(paths["authorizations"], auths,
               ["auth_id", "client_id", "payer_id", "service_code", "units_authorized",
                "period_start", "period_end", "issued_date"])
    _write_csv(paths["sessions"], sessions,
               ["session_id", "client_id", "provider_id", "service_code", "service_date",
                "center_id", "duration_value", "duration_uom", "status", "source_system"])

    return paths


if __name__ == "__main__":  # pragma: no cover
    written = generate()
    for name, path in written.items():
        with path.open(encoding="utf-8") as fh:
            n = sum(1 for _ in fh) - 1
        print(f"{name:>16}: {n:>7,} rows  -> {path}")
