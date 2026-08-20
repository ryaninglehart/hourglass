"""The orchestrator wiring.

The pipeline is expressed as tasks with declared dependencies rather than as a
sequence of statements, and run through :mod:`hourglass.orchestration`. The
work is identical either way; what changes is what the shape buys:

* the dependency graph is checked before anything runs, so a reordering
  mistake is a ``ValueError`` at construction rather than a confusing failure
  half way through;
* the one step that touches a network gets retries and the pure transforms do
  not, because retrying a deterministic function that just failed only fails
  more slowly;
* every step is timed, so "the pipeline got slower" is answerable from the
  JSON-lines log rather than from memory;
* a failure marks its dependents SKIPPED instead of running them against
  missing inputs.

The dependency graph::

    extract -> land -> read_lake -> conform -> analyse -> protect
                                                            |
                                                            v
                                            quality -> load -> diff ---+
                                                         |         |
                                                         +-> parity +-> publish
                                                                        -> verify

Run it::

    python -m hourglass.pipeline
    python -m hourglass.pipeline --acknowledge uom_resolution_coverage="ticket DE-412; \\
        vendor confirmed the field change, back-fill due Friday"
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path

import pandas as pd

from . import (
    analytics,
    diff,
    digest,
    export,
    metrics,
    model,
    phi,
    quality,
    transform,
)
from .config import (
    EXPORT_DIR,
    GEN,
    RAW,
    REPORT_DIR,
    ROOT,
    S3,
    WAREHOUSE,
    ensure_dirs,
)
from .generate import generate
from .ingest import land_extracts, make_backend
from .orchestration import Orchestrator, Task

CODE_VERSION = "0.7.0"
RUN_LOG_PATH = REPORT_DIR / "pipeline_runs.jsonl"

_RETRIES: dict[str, int] = {}

#: Append-only record of every run, published or blocked. See
#: :func:`append_run_log_row` for why the warehouse alone cannot be it.
RUN_AUDIT_PATH = WAREHOUSE.with_name("run_audit.db")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_RUN_LOG_DDL_RE = re.compile(r"CREATE TABLE run_log\b.*?;", re.S)


def _run_log_ddl() -> str:
    """The run_log DDL, read out of the schema file rather than repeated here.

    Two copies of a CREATE TABLE drift, and the copy that drifts is the one
    nobody looks at. sql/star_schema.sql stays the only place the audit
    columns are declared.
    """
    match = _RUN_LOG_DDL_RE.search(model.SCHEMA_PATH.read_text(encoding="utf-8"))
    if match is None:
        raise RuntimeError(
            f"No run_log table found in {model.SCHEMA_PATH}. The audit trail "
            f"cannot be written without it.")
    return match.group(0)


def append_run_log_row(path: Path, row: dict, create_if_missing: bool = False) -> bool:
    """Append one audit row to a run_log, touching nothing else in the file.

    ``model.atomic_build`` writes the run log for a *published* run, and it
    does so by rebuilding the entire database and swapping it in. That is the
    right shape for a warehouse and the wrong shape for an audit trail, in two
    ways. It makes the log append-only by convention rather than by
    construction -- the history is re-inserted from a read of the old file on
    every publish, so a fault in that carry-forward loses runs silently. And it
    is unavailable to a run that failed its gate: rebuilding the live warehouse
    is exactly what Write-Audit-Publish forbids, so a blocked run's row went
    only to the quarantined copy, which the next blocked run overwrote. The
    published log recorded 126 runs, all of them successes, and read as though
    the gate had never fired.

    A single INSERT is the smaller instrument that fits. It adds one row to
    run_log and leaves every fact and dimension byte where it was, and SQLite
    commits it atomically, so a concurrent reader sees the previous complete
    warehouse either with the audit row or without it. WAP survives: the run
    that failed has still published no data.

    Columns absent from the target's run_log are dropped rather than raised on.
    A warehouse built before ``refused_acknowledgements`` existed is missing
    that column, and a run whose audit row lands one field short is a better
    outcome than a run whose audit row does not land.
    """
    if not path.exists():
        if not create_if_missing:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        try:
            conn.executescript(_run_log_ddl())
            conn.commit()
        finally:
            conn.close()

    conn = sqlite3.connect(path)
    try:
        known = {c[1] for c in conn.execute("PRAGMA table_info(run_log)")}
        payload = {k: v for k, v in row.items() if k in known}
        if not payload:
            return False
        conn.execute(
            f"INSERT INTO run_log ({', '.join(payload)}) "
            f"VALUES ({', '.join('?' * len(payload))})",
            tuple(payload.values()))
        conn.commit()
    finally:
        conn.close()
    return True


def _code_revision() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return f"{CODE_VERSION}+{out.stdout.strip()}"
    except Exception:
        pass
    return CODE_VERSION


def _read_from_lake(backend, ingest_date, filename: str, source: str) -> pd.DataFrame:
    key = f"{S3.raw_prefix}/source={source}/ingest_date={ingest_date}/{filename}"
    return pd.read_csv(StringIO(backend.get_text(key)), dtype={"duration_uom": "object"})


def _log(msg: str, quiet: bool = False) -> None:
    if not quiet:
        print(msg, flush=True)


def _not_applicable(ctx: dict, task: str, reason: str) -> None:
    """Record that a task ran, decided there was nothing to do, and did nothing.

    A distinct outcome rather than the orchestrator's ``SKIPPED``, and the
    difference is a factual one. ``SKIPPED`` means the task never executed
    because something upstream failed. ``publish`` on a blocked run does
    execute: it reads the gate's verdict, correctly declines, and returns.
    Recording that as SKIPPED would put a false statement in the JSON-lines run
    log, which is the file the timing and failure analysis reads, to fix a
    cosmetic problem in the console.

    So the orchestrator's status vocabulary is left alone and the distinction
    lives here, at the console. It is written into the shared context rather
    than returned because the progress printer needs it at the moment the task
    finishes, and a returned dict is merged into a context the printer never
    sees.
    """
    ctx.setdefault("task_outcomes", {})[task] = reason


# ---------------------------------------------------------------------------
# tasks
# ---------------------------------------------------------------------------


def task_extract(ctx: dict) -> dict:
    if ctx["regenerate"] or not any(RAW.glob("*.csv")):
        generate(GEN)
    return {"extract_files": len(list(RAW.glob("*.csv")))}


def task_land(ctx: dict) -> dict:
    backend = make_backend(prefer_s3=ctx["prefer_s3"])
    manifest = land_extracts(backend=backend)
    return {"backend": backend, "manifest": manifest,
            "ingest_date": manifest["ingest_date"]}


def task_read_lake(ctx: dict) -> dict:
    backend, ingest_date = ctx["backend"], ctx["ingest_date"]

    def read(filename: str, source: str) -> pd.DataFrame:
        return _read_from_lake(backend, ingest_date, filename, source)

    return {
        "raw_sessions": read("ehr_sessions.csv", "ehr"),
        "raw_auths": read("payer_authorizations.csv", "payer_api"),
        "raw_clients": read("salesforce_clients.csv", "salesforce"),
        "raw_providers": read("reference_providers.csv", "reference"),
        "raw_centers": read("reference_centers.csv", "reference"),
        "raw_payers": read("reference_payers.csv", "reference"),
    }


def task_conform(ctx: dict) -> dict:
    deduped, duplicates = transform.dedupe_sessions(ctx["raw_sessions"])
    sessions_resolved = transform.resolve_minutes(deduped)

    dim_client = transform.build_dim_client(ctx["raw_clients"])
    dim_service = transform.build_dim_service()
    dim_provider = transform.build_dim_provider(ctx["raw_providers"])
    dim_center = transform.build_dim_center(ctx["raw_centers"])
    dim_payer = transform.build_dim_payer(ctx["raw_payers"])

    all_dates = pd.concat([
        pd.to_datetime(ctx["raw_sessions"]["service_date"]),
        pd.to_datetime(ctx["raw_auths"]["period_start"]),
        pd.to_datetime(ctx["raw_auths"]["period_end"]),
    ])
    dim_date = transform.build_dim_date(all_dates.min(), all_dates.max())

    fact_session = transform.build_fact_session(
        sessions_resolved, dim_client, dim_provider, dim_service, dim_center)
    fact_authorization = transform.build_fact_authorization(
        ctx["raw_auths"], dim_client, dim_service, dim_payer)

    return {
        "deduped": deduped, "duplicates": duplicates,
        "sessions_resolved": sessions_resolved,
        "dim_client": dim_client, "dim_service": dim_service,
        "dim_provider": dim_provider, "dim_center": dim_center,
        "dim_payer": dim_payer, "dim_date": dim_date,
        "fact_session": fact_session, "fact_authorization": fact_authorization,
        "frames": {
            "dim_date": dim_date, "dim_client": dim_client,
            "dim_service": dim_service, "dim_provider": dim_provider,
            "dim_center": dim_center, "dim_payer": dim_payer,
            "fact_session": fact_session, "fact_authorization": fact_authorization,
        },
    }


def task_analyse(ctx: dict) -> dict:
    as_of = pd.to_datetime(ctx["raw_sessions"]["service_date"]).max()
    util = analytics.build_utilization(
        ctx["fact_session"], ctx["fact_authorization"], ctx["dim_client"],
        ctx["dim_service"], ctx["dim_payer"], as_of, dim_center=ctx["dim_center"])
    at_risk = analytics.at_risk_authorizations(util)
    unmatched = analytics.unmatched_sessions(
        ctx["fact_session"], ctx["fact_authorization"], ctx["dim_client"])
    return {
        "as_of": as_of, "utilization": util, "at_risk": at_risk,
        "unauthorized_units": float(unmatched["units_delivered"].sum()),
        "unauthorized_session_count": len(unmatched),
    }


def task_protect(ctx: dict) -> dict:
    """De-identify everything bound for publication, then inspect the result.

    Separate from ``publish`` on purpose. The egress check has to run against
    the frames that will actually be written, and it has to run *before* the
    gate decides. Checking the input and publishing the output is how a
    boundary gets crossed by something nobody inspected.
    """
    publish_frames = phi.deidentify_for_export(
        {**ctx["frames"], "at_risk": ctx["at_risk"]})
    at_risk_safe = publish_frames.pop("at_risk")
    findings = phi.check_egress({**publish_frames, "at_risk": at_risk_safe})
    return {"publish_frames": publish_frames, "at_risk_safe": at_risk_safe,
            "egress_findings": findings}


def task_quality(ctx: dict) -> dict:
    check_ctx = {
        "sessions_resolved": ctx["sessions_resolved"],
        "sessions_deduped": ctx["deduped"],
        "deduped_session_count": len(ctx["deduped"]),
        "duplicate_sessions": ctx["duplicates"],
        "fact_session": ctx["fact_session"],
        "fact_authorization": ctx["fact_authorization"],
        "dim_client": ctx["dim_client"],
        "dim_date": ctx["dim_date"],
        "utilization": ctx["utilization"],
        "unauthorized_session_count": ctx["unauthorized_session_count"],
        "unauthorized_units": ctx["unauthorized_units"],
        "publish_frames": ctx["publish_frames"],
        "egress_findings": ctx["egress_findings"],
    }
    results = quality.run_checks(check_ctx)
    decision = quality.evaluate_gate(results, ctx["acknowledgements"])

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "quality_report.md").write_text(
        quality.render_report(decision), encoding="utf-8")
    (REPORT_DIR / "quality_report.json").write_text(
        json.dumps(export.json_safe(decision.to_dict()), indent=2, allow_nan=False),
        encoding="utf-8")
    return {"decision": decision, "check_results": results}


def task_load(ctx: dict) -> dict:
    """Write-Audit-Publish, with the audit already done.

    This is the *publish* step of the WAP pattern, and it is worth naming
    because the shape is not incidental. The warehouse is built into a scratch
    file, the quality gate has already run against the frames going into it,
    and only a passing run is swapped into place by an atomic rename.

    The failure WAP prevents, and that test-after-materialise does not: if the
    checks run against data that is already live, a failure means consumers
    have already read it. Here they see a slightly older warehouse instead of a
    wrong one.

    The audit row is the one thing a blocked run does write outside its own
    quarantine. Data from a failed run must not reach a reader; the fact that
    it failed must. See :func:`append_run_log_row`.
    """
    decision = ctx["decision"]
    # A blocked run builds beside the published warehouse, never over it, so
    # every downstream reader keeps seeing the last warehouse that passed.
    target = WAREHOUSE if decision.published else WAREHOUSE.with_suffix(".rejected.db")

    # Snapshot the outgoing build so the diff has something to compare against.
    # Taken before the swap, because after it the previous version is gone.
    previous = WAREHOUSE.with_suffix(".previous.db")
    if decision.published and WAREHOUSE.exists():
        shutil.copy2(WAREHOUSE, previous)
    elif not WAREHOUSE.exists():
        previous.unlink(missing_ok=True)
    finished = datetime.now(UTC)
    run_log_row = {
        "run_id": ctx["run_id"],
        "started_at_utc": ctx["started_at_utc"],
        "finished_at_utc": finished.isoformat(timespec="seconds"),
        "code_version": _code_revision(),
        "ruleset_version": decision.ruleset_version,
        "ruleset_hash": decision.ruleset_hash,
        "published": int(decision.published),
        "blocking_failures": ",".join(decision.blocking_failures),
        "acknowledgements": json.dumps(decision.acknowledged),
        # Somebody attempting to sign off a PHI failure is the most interesting
        # thing that can happen to this gate, and until now it survived only in
        # a quality report the next run overwrote.
        "refused_acknowledgements": ",".join(decision.refused_acknowledgements),
        "session_rows": len(ctx["fact_session"]),
        "auth_rows": len(ctx["fact_authorization"]),
        "lake_backend": ctx["manifest"]["backend"],
    }
    counts = model.atomic_build(target, ctx["frames"], run_log_row=run_log_row)

    # The sidecar takes every run. The published warehouse takes the blocked
    # ones it would otherwise never hear about -- a published run's row is
    # already in it, written by atomic_build above. The two are kept because
    # they fail differently: the warehouse log is convenient and gets rebuilt,
    # the sidecar is inconvenient and does not.
    audit_paths = [RUN_AUDIT_PATH]
    append_run_log_row(RUN_AUDIT_PATH, run_log_row, create_if_missing=True)
    if not decision.published and append_run_log_row(WAREHOUSE, run_log_row):
        audit_paths.append(WAREHOUSE)

    return {"counts": counts, "warehouse_path": target, "finished_at": finished,
            "previous_warehouse": previous, "audit_paths": audit_paths}


def _previous_ruleset_hash(path: Path) -> str | None:
    """The rule set the last published build ran under, or None.

    None is a legitimate answer -- a first run, or a warehouse predating the
    column -- and is treated as "cannot tell" rather than "unchanged", so a
    missing value never produces a false all-clear.
    """
    if not path or not path.exists():
        return None
    try:
        conn = sqlite3.connect(path)
        try:
            row = conn.execute(
                "SELECT ruleset_hash FROM run_log WHERE published = 1 "
                "ORDER BY finished_at_utc DESC LIMIT 1").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return row[0] if row and row[0] else None


def task_diff(ctx: dict) -> dict:
    """Compare this build against the last published one.

    Deliberately after `load` and before `publish`: the diff describes the
    warehouse that is about to be reported on, and a reader of the digest
    should be able to see what moved since last week without re-deriving it.
    """
    if not ctx["decision"].published:
        _not_applicable(ctx, "diff", "no new published build to compare against")
        return {"warehouse_diff": None}

    result = diff.diff_warehouses(ctx["previous_warehouse"], WAREHOUSE)

    # Carry the rule-set hashes alongside the row counts, so the report can say
    # whether the numbers moved or the definitions did. Both hashes are already
    # stamped on every run; nothing was comparing them, which meant a change to
    # a threshold or a unit conversion published in silence. `make prove` case
    # 7 is exactly that, and it is the reason this is here.
    result.ruleset_after = ctx["decision"].ruleset_hash
    result.ruleset_before = _previous_ruleset_hash(ctx["previous_warehouse"])

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "data_diff.md").write_text(diff.render(result), encoding="utf-8")
    return {"warehouse_diff": result}


def task_parity(ctx: dict) -> dict:
    """Compute every headline metric twice and refuse to publish if they differ.

    Once by the SQL in `metrics.py`, executed against the warehouse that was
    just built, and once by the pandas that produced the dashboard payload.
    The two are independent implementations of the same sentence, and a
    warehouse whose two consumers disagree is worse than one that is merely
    late: nobody can tell which number to act on.

    This raises rather than warns. A disagreement means the numbers about to
    be published are not reproducible from the warehouse they claim to come
    from, and the orchestrator marks `publish` SKIPPED, so the previous
    build's exports stay in place.

    It earned that severity on its first run. It found that
    `build_utilization` filtered on `is_completed` and `uom_resolved` with
    `.loc[]` -- correct for the boolean columns `transform.py` produces, and
    silently a no-op for the int64 columns the same tables come back as when
    read from SQLite, because `.loc[int_series]` returns every row without
    raising. Utilisation was being computed over cancelled sessions. No test
    saw it, because every test fed the frames in from the transform side.
    """
    warehouse = ctx["warehouse_path"]
    frames = dict(ctx["frames"])
    frames["utilization"] = ctx["utilization"]

    parity = metrics.check_parity(warehouse, frames)
    contract = metrics.check_dax_contract(ROOT / "bi" / "measures.dax")

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "metric_parity.md").write_text(
        metrics.render(parity, contract), encoding="utf-8")

    disagreements = [p for p in parity if not p.agrees]
    if disagreements:
        detail = "; ".join(
            f"{p.label}: SQL {p.sql_value:,.4f} vs pandas {p.frame_value:,.4f}"
            if p.error is None else f"{p.label}: {p.error}"
            for p in disagreements)
        raise ValueError(
            f"metric parity failed for {len(disagreements)} metric(s) -- "
            f"{detail}. Not publishing: the warehouse and the dashboard "
            f"would disagree.")

    broken = [c for c in contract if not c.holds]
    return {"parity": parity, "dax_contract": contract,
            "dax_contract_broken": broken}


def task_publish(ctx: dict) -> dict:
    decision = ctx["decision"]
    if not decision.published:
        _not_applicable(ctx, "publish", "the gate refused; nothing was written")
        return {"published_paths": [], "digest_path": None}

    published_paths = export.export_csvs(ctx["publish_frames"])
    export.write_relationship_spec()

    util, at_risk_safe = ctx["utilization"], ctx["at_risk_safe"]
    payload = export.build_dashboard_payload(
        util=util,
        at_risk=at_risk_safe,
        by_payer=analytics.utilization_by(util, "payer_name").merge(
            util[["payer_name", "contract_type"]].drop_duplicates(),
            on="payer_name", how="left"),
        by_discipline=analytics.utilization_by(util, "discipline"),
        by_center=analytics.utilization_by(util, "center_name"),
        monthly=analytics.monthly_delivery(ctx["fact_session"], ctx["dim_date"]),
        quality=decision.to_dict(),
        comparison=analytics.unit_assumption_spread(ctx["sessions_resolved"]),
        meta={
            "run_id": ctx["run_id"],
            "code_version": _code_revision(),
            "as_of": ctx["as_of"].strftime("%Y-%m-%d"),
            "generated_at_utc": ctx["finished_at"].isoformat(timespec="seconds"),
            "lake_backend": ctx["manifest"]["backend"],
            "bucket": ctx["manifest"]["bucket"],
            "synthetic": True,
        },
    )
    export.write_dashboard_payload(payload)

    # The digest is built from the same payload the dashboard uses, so the two
    # cannot disagree about what is at risk this week.
    digest_path = digest.write_digest(
        digest.build_digest(at_risk=at_risk_safe, headline=payload["headline"],
                            quality=payload["quality"], meta=payload["meta"]),
        REPORT_DIR)
    return {"published_paths": published_paths, "digest_path": digest_path}


def task_verify(ctx: dict) -> dict:
    """Re-read every published file and look for raw identifiers.

    The last task, and the only one that inspects bytes rather than frames.

    Every other protection here checks a DataFrame on its way to a file, which
    leaves a gap wherever something reaches a file without passing through a
    frame the gate was handed: a check's sample rows, an aggregate assembled
    after the gate ran, a log line. That gap was not hypothetical -- raw client
    identifiers reached `dashboard.html` through quality-check samples while
    every frame-level check reported clean.

    So this gives up on knowing the paths and reads the output instead. It is
    deliberately dumb: it does not know what any file means, only what a source
    identifier looks like and that none belongs here.

    A finding raises. It cannot be acknowledged, it is not a WARN, and it does
    not produce a report -- by the time this runs the files are already on
    disk, so the only useful response is to fail loudly and delete them.
    """
    if not ctx["decision"].published:
        _not_applicable(ctx, "verify", "nothing was published to re-read")
        return {"artifact_findings": [], "artifacts_scanned": 0}

    artifacts = [
        *EXPORT_DIR.glob("*.csv"),
        EXPORT_DIR / "dashboard_data.json",
        EXPORT_DIR / "relationships.md",
        REPORT_DIR / "quality_report.md",
        REPORT_DIR / "quality_report.json",
        REPORT_DIR / "weekly_digest.md",
        REPORT_DIR / "pipeline_runs.jsonl",
        ROOT / "dashboard.html",
    ]
    findings = phi.scan_published_artifacts(artifacts)
    scanned = sum(1 for a in artifacts if a.exists())

    # Second half of the verification, and a different question: are the
    # numbers on the published dashboard reproducible from the warehouse they
    # claim to describe?
    #
    # `task_parity` compares two implementations of a metric, which is not the
    # same thing. Its report read "All 11 metrics agree" for the entire life of
    # a defect that put a wrong number on the most prominent tile of the
    # dashboard -- truthfully, because the tile was computed in `export.py` by
    # a route nobody had registered. Comparing definitions to each other cannot
    # find that. Re-deriving the artifact can.
    payload_path = EXPORT_DIR / "dashboard_data.json"
    headline_results = []
    if payload_path.exists():
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        headline_results = metrics.check_published_headlines(WAREHOUSE, payload)
        wrong = [r for r in headline_results if not r.agrees]
        if wrong:
            detail = "; ".join(
                f"{r.label}: warehouse {r.sql_value:,.2f} vs published "
                f"{r.frame_value:,.2f}" if r.error is None
                else f"{r.label}: {r.error}" for r in wrong)
            raise ValueError(
                f"PUBLISHED FIGURE UNVERIFIABLE: {len(wrong)} headline "
                f"number(s) on the dashboard cannot be reproduced from the "
                f"warehouse ({detail}). The tile and the data disagree, and "
                f"the tile is what a reader acts on.")
        report = REPORT_DIR / "metric_parity.md"
        if report.exists():
            report.write_text(
                report.read_text(encoding="utf-8")
                + "\n" + metrics.render_published(headline_results),
                encoding="utf-8")

    if findings:
        detail = "; ".join(
            f"{f.count}x {f.pattern} in {Path(f.path).name}" for f in findings)
        for finding in findings:
            Path(finding.path).unlink(missing_ok=True)
        raise RuntimeError(
            f"PHI EGRESS: raw source identifiers found in published artifacts "
            f"after writing ({detail}). The offending files have been deleted. "
            f"This is a defect in the publication path, not a data problem -- "
            f"something reached a file without passing through "
            f"phi.deidentify_for_export."
        )
    return {"artifact_findings": [], "artifacts_scanned": scanned,
            "published_headlines": headline_results}


def build_tasks() -> list[Task]:
    """The graph. Retries are declared where the flakiness actually is."""
    return [
        Task("extract", task_extract,
             description="Produce the synthetic source extracts."),
        Task("land", task_land, depends_on=("extract",),
             # The only steps that can touch a network. If S3 is briefly
             # unavailable a second attempt is worth making; nothing else here
             # would behave differently on a retry.
             retries=2, backoff_seconds=0.5,
             description="Land extracts in the S3 data lake."),
        Task("read_lake", task_read_lake, depends_on=("land",), retries=2,
             backoff_seconds=0.5,
             description="Read the extracts back out of the lake."),
        Task("conform", task_conform, depends_on=("read_lake",),
             description="Resolve units, deduplicate, build the star schema."),
        Task("analyse", task_analyse, depends_on=("conform",),
             description="Utilisation, pace, and the at-risk list."),
        Task("protect", task_protect, depends_on=("analyse",),
             description="De-identify publishable frames and check the boundary."),
        Task("quality", task_quality, depends_on=("protect",),
             description="Run the checks and decide whether to publish."),
        Task("load", task_load, depends_on=("quality",),
             description="Build the warehouse (quarantined if blocked)."),
        Task("diff", task_diff, depends_on=("load",),
             description="Diff this build against the last published one."),
        Task("parity", task_parity, depends_on=("load",),
             description="Recompute every metric in SQL and confirm it "
                         "matches the Python."),
        Task("publish", task_publish, depends_on=("diff", "parity"),
             description="Write BI exports, dashboard payload, and the digest."),
        Task("verify", task_verify, depends_on=("publish",),
             description="Re-read the published files and confirm no raw "
                         "identifiers were written."),
    ]


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

_LABELS = {
    "extract": "source extracts", "land": "landed in lake",
    "read_lake": "read from lake", "conform": "conformed",
    "analyse": "analysed", "protect": "de-identified",
    "quality": "quality gates", "load": "loaded warehouse",
    "diff": "diffed vs previous", "parity": "metrics agree",
    "publish": "published",
    "verify": "egress verified",
}


def run(acknowledgements: dict[str, str] | None = None,
        prefer_s3: bool = True,
        quiet: bool = False,
        regenerate: bool = True,
        show_timing: bool = False) -> dict:
    ensure_dirs()
    started = datetime.now(UTC)
    run_id = str(uuid.uuid4())[:8]

    _log(f"\n  hourglass {_code_revision()}   run {run_id}", quiet)
    _log("  " + "-" * 62, quiet)

    total = len(_LABELS)
    step = {"n": 0}
    # Shared with the tasks through the context below. `Orchestrator.run` copies
    # the context dict shallowly, so this is the same object the tasks reach.
    outcomes: dict[str, str] = {}

    def on_event(event: str, record: dict) -> None:
        if quiet:
            return
        name = record.get("task", "")
        if event == "task_succeeded":
            step["n"] += 1
            # `_LABELS` is past tense -- "published", "egress verified" -- which
            # is the right thing to print for a task that did the work and the
            # opposite of the truth for one that declined to. A blocked run used
            # to report that it had published and verified.
            outcome = outcomes.get(name)
            shown = name if outcome else _LABELS.get(name, name)
            trailer = f"  not applicable — {outcome}" if outcome else ""
            # The counter is right-aligned so `[9/12]` and `[10/12]` occupy
            # the same width. Without it every line from the tenth task on
            # shifts a column and the timings stop forming a readable column.
            counter = f"[{step['n']}/{total}]"
            _log(f"  {counter:>7} {shown:<22} "
                 f"{record['duration_seconds']:>6.2f}s{trailer}")
        elif event == "task_attempt_failed":
            # A task with no retries configured emits this once and then
            # fails. Printing "retrying" there is a lie, and a small one that
            # costs somebody five minutes reading a log wondering why a
            # deterministic step was attempted twice.
            more = record.get("attempt", 1) <= _RETRIES.get(name, 0)
            verb = "retrying" if more else "attempt failed"
            _log(f"        {verb} {name}: {record['error']}")
        elif event == "task_failed":
            _log(f"  FAILED  {name}: {record['error']}")
        elif event == "task_skipped":
            _log(f"  SKIPPED {name} (upstream failed)")

    tasks = build_tasks()
    _RETRIES.update({t.name: t.retries for t in tasks})

    result = Orchestrator(
        tasks, run_id=run_id, log_path=RUN_LOG_PATH, on_event=on_event
    ).run({
        "run_id": run_id,
        "started_at_utc": started.isoformat(timespec="seconds"),
        "acknowledgements": acknowledgements or {},
        "prefer_s3": prefer_s3,
        "regenerate": regenerate,
        "task_outcomes": outcomes,
    })

    ctx = result.context
    decision = ctx.get("decision")

    if not result.succeeded:
        failed = result.failed_task
        _log(f"\n  Pipeline failed at '{failed.name}'. Nothing was published.\n", quiet)
        return {"run_id": run_id, "succeeded": False, "published": False,
                "decision": decision, "run": result, "frames": ctx.get("frames", {})}

    if not quiet:
        results = ctx["check_results"]
        n_block = sum(1 for r in results
                      if not r.passed and r.severity is quality.Severity.BLOCK)
        n_warn = sum(1 for r in results
                     if not r.passed and r.severity is quality.Severity.WARN)
        _log(f"\n  {len(results)} checks · {n_block} BLOCK · {n_warn} WARN"
             f" · ruleset {decision.ruleset_hash}")
        for r in results:
            if not r.passed and r.severity is quality.Severity.BLOCK:
                mark = "acknowledged" if r.name in decision.acknowledged else "UNRESOLVED"
                _log(f"    BLOCK  {r.name}  [{mark}]")

        if decision.published:
            _log(f"\n  Published {len(ctx['published_paths'])} CSVs, the dashboard "
                 f"payload, and the weekly digest.")
            _log(f"  Egress verified: {ctx['artifacts_scanned']} artifacts re-read, "
                 f"no raw identifiers found.")
            wd = ctx.get("warehouse_diff")
            if wd is not None and wd.tables:
                _log("  Diff vs previous build: "
                     + ("no change" if wd.is_identical
                        else f"{wd.total_changed_rows:,} rows differ"))
        else:
            _log("\n  PUBLICATION HALTED — unresolved blocking failure.")
            _log(f"  Warehouse quarantined to {ctx['warehouse_path'].name}; "
                 f"nothing downstream was refreshed.")
            _log("  Audit row written to "
                 + ", ".join(p.name for p in ctx["audit_paths"])
                 + ". A refusal is part of the record.")
        _log(f"  Report: {REPORT_DIR / 'quality_report.md'}")
        if show_timing:
            _log("\n  Task timing:")
            _log(result.timing_table())
        _log("")

    return {
        "run_id": run_id,
        "succeeded": True,
        "decision": decision,
        "frames": ctx["frames"],
        "utilization": ctx["utilization"],
        "at_risk": ctx["at_risk"],
        "published": decision.published,
        "counts": ctx["counts"],
        "manifest": ctx["manifest"],
        "run": result,
    }


def _parse_ack(values: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in values or []:
        if "=" not in item:
            raise SystemExit(
                f"--acknowledge expects CHECK=REASON, got {item!r}. "
                "A blocking failure cannot be released without a written reason."
            )
        name, reason = item.split("=", 1)
        reason = reason.strip()
        if len(reason) < 10:
            raise SystemExit(
                f"Reason for {name!r} is too short. Write the actual reason: "
                "it goes in the run log and someone will read it later."
            )
        out[name.strip()] = reason
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="hourglass",
        description="Authorisation utilisation pipeline. All data is synthetic.",
    )
    p.add_argument("--acknowledge", action="append", metavar="CHECK=REASON",
                   help="Release a blocking failure. Reason is recorded in the run log.")
    p.add_argument("--no-s3", action="store_true",
                   help="Skip S3 and use the local lake mirror.")
    p.add_argument("--no-regenerate", action="store_true",
                   help="Reuse the existing extracts instead of regenerating.")
    p.add_argument("--timing", action="store_true",
                   help="Print per-task timing after the run.")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args(argv)

    result = run(
        acknowledgements=_parse_ack(args.acknowledge),
        prefer_s3=not args.no_s3,
        quiet=args.quiet,
        regenerate=not args.no_regenerate,
        show_timing=args.timing,
    )
    # Non-zero on an unresolved block so CI fails the build, which is the whole
    # point of calling it a gate. Also non-zero if a task failed outright.
    return 0 if result.get("published") else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
