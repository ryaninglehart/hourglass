"""Shared fixtures.

Most tests run against small hand-built frames rather than the generated
dataset. A test that fails should point at one behaviour, and a 97,000-row
fixture cannot do that -- it tells you something is wrong somewhere.

The end-to-end test is the exception and is marked ``slow``.

The other job this file does is isolation. Everything that runs the pipeline
writes to a throwaway data tree created below, never to the checkout's own
``data/``. See ``workspace`` for why that matters.
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------
# This runs at conftest import, which is before pytest imports any test module
# and therefore before anything imports `hourglass`. That ordering is the whole
# mechanism: `config.DATA` reads this variable once, at import, and the modules
# that bind paths from it -- including the ones that bind them as default
# arguments, which cannot be monkeypatched afterwards -- then bind the
# redirected paths. Setting it from a fixture would be too late and would only
# move some of them.
# An already-set value is honoured rather than replaced, and that is not
# politeness -- it is what makes `make mutation` work. mutmut copies the tree
# and imports this conftest a second time in the same process, so minting a
# fresh directory unconditionally would leave `config.DATA` bound to the first
# one while the environment pointed at the second. The `workspace` fixture
# below would then fail its own assertion, mutmut would report the baseline as
# broken, and every mutant would come back `not checked` -- a green-looking
# target that had measured nothing.
_existing = os.environ.get("HOURGLASS_DATA_DIR")
if _existing:
    _SESSION_BASE = Path(_existing).parent
else:
    _SESSION_BASE = Path(tempfile.mkdtemp(prefix="hourglass-tests-"))
    os.environ["HOURGLASS_DATA_DIR"] = str(_SESSION_BASE / "data")

    # Registered here rather than as a teardown fixture so that an interrupted
    # run, or a `--collect-only` that never starts a session, still cleans up
    # after itself. A published warehouse is 25MB and nobody goes looking in
    # /tmp. Only the process that created the directory removes it.
    atexit.register(shutil.rmtree, _SESSION_BASE, ignore_errors=True)


class Workspace:
    """The throwaway checkout the pipeline runs against.

    ``data`` is what ``config.DATA`` points at. ``root`` stands in for the
    repository root: the pipeline reads ``bi/measures.dax`` from there and its
    egress verifier scans -- and on a finding, deletes -- ``dashboard.html``
    there. Pointing it at a copy is what stops a test from deleting the
    dashboard a developer just built.
    """

    #: Repository files the pipeline reads or scans through ``pipeline.ROOT``.
    MIRRORED = ("bi/measures.dax", "dashboard.html")

    def __init__(self, base: Path) -> None:
        self.base = base
        self.data = base / "data"
        self.root = base / "repo"

    def reset(self) -> None:
        """Return the workspace to the state a fresh clone would be in.

        Called before every test. A test that publishes leaves a warehouse, a
        run log and a set of exports behind; the next test must not be able to
        read them, append to them, or notice that a previous test deleted
        them. Cleaning at setup rather than teardown also leaves the last
        failing test's output on disk to look at.
        """
        dirty = self.data.exists() and any(self.data.iterdir())
        if dirty:
            shutil.rmtree(self.data)
        self.data.mkdir(parents=True, exist_ok=True)
        if dirty or not self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)
            self.root.mkdir(parents=True, exist_ok=True)
            for name in self.MIRRORED:
                source = ROOT / name
                if source.exists():
                    target = self.root / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)

    def snapshot(self, name: str) -> Workspace:
        """Copy the current workspace somewhere ``reset`` will not touch.

        A shared pipeline run is only safe to share if nothing can write to it
        afterwards. Taking a copy outside the reset area gives the readers a
        fixed set of artifacts rather than a directory the next test empties.
        """
        kept = Workspace(self.base / "snapshots" / name)
        shutil.rmtree(kept.base, ignore_errors=True)
        kept.base.mkdir(parents=True)
        for source, target in ((self.data, kept.data), (self.root, kept.root)):
            if source.exists():
                shutil.copytree(source, target)
        return kept

    # Convenience accessors, mirroring the names in `config`.
    @property
    def out(self) -> Path:
        return self.data / "out"

    @property
    def warehouse(self) -> Path:
        return self.data / "out" / "hourglass.db"

    @property
    def export_dir(self) -> Path:
        return self.data / "out" / "bi"

    @property
    def report_dir(self) -> Path:
        return self.data / "out" / "reports"


@pytest.fixture(scope="session")
def workspace() -> Workspace:
    from hourglass import config

    space = Workspace(_SESSION_BASE)
    assert space.data == config.DATA, (
        f"config.DATA is {config.DATA}, not {space.data}. Something imported "
        "hourglass before this conftest ran, so the pipeline would write to "
        "the checkout's own data/ and the suite would not be hermetic."
    )
    return space


@pytest.fixture(scope="session", autouse=True)
def _redirect_repo_root(workspace: Workspace):
    """Point the pipeline's idea of the repository root at the mirror.

    ``pipeline.ROOT`` is read inside task bodies rather than captured as a
    default argument, so setting the attribute is enough here. The path is
    constant for the session, so one substitution covers every test.
    """
    from hourglass import pipeline

    original = pipeline.ROOT
    pipeline.ROOT = workspace.root
    yield
    pipeline.ROOT = original


@pytest.fixture(autouse=True)
def _clean_workspace(workspace: Workspace):
    """Give every test the empty data tree it thinks it has.

    Without this, ``run_log`` row counts depend on how many earlier tests ran
    the pipeline, and the egress test in test_phi.py -- which deliberately
    provokes a failure that deletes published files -- decides whether the
    tests after it find anything to assert against.
    """
    workspace.reset()


class PublishedRun:
    """The result dict of a published run, plus the paths it wrote to."""

    def __init__(self, result: dict, kept: Workspace) -> None:
        self.result = result
        self.workspace = kept
        self.warehouse = kept.warehouse
        self.export_dir = kept.export_dir
        self.report_dir = kept.report_dir
        self.root = kept.root


@pytest.fixture(scope="session")
def acknowledgement() -> dict[str, str]:
    """The written reason the tests hand the gate.

    Long enough to clear the minimum the parser enforces, and honest about why
    the block is being waived here.
    """
    return {"uom_resolution_coverage": "test run; defect is seeded on purpose"}


@pytest.fixture(scope="session")
def published_run(workspace: Workspace, acknowledgement) -> PublishedRun:
    """One acknowledged, published pipeline run, snapshotted and shared.

    Read-only by construction: the run happens in the live workspace, the
    result is copied out, and every consumer reads the copy. Sharing the
    directory instead would put these tests back where they started, each one
    asserting against whatever the previous one left.

    Session-scoped because a full run costs seconds and nine tests want the
    same artifacts. Because it resets the workspace first, it produces the
    same snapshot whichever test happens to request it first.
    """
    from hourglass import pipeline

    workspace.reset()
    result = pipeline.run(acknowledgements=acknowledgement, prefer_s3=False,
                          quiet=True)
    return PublishedRun(result, workspace.snapshot("published"))


@pytest.fixture
def sessions_raw() -> pd.DataFrame:
    """Nine sessions covering every unit-of-measure case that exists.

    Rows 1-2 pre-migration (minutes), 3-4 post-migration (units), 5 missing the
    unit flag, 6 an unmapped service code, 7 a non-numeric duration, 8 a
    per-session code, 9 an exact duplicate of row 1.
    """
    return pd.DataFrame([
        {"session_id": "S1", "client_id": "C1", "provider_id": "P1",
         "service_code": "97153", "service_date": "2026-03-02", "center_id": "CTR-SD",
         "duration_value": 180, "duration_uom": "minutes", "status": "completed",
         "source_system": "ehr"},
        {"session_id": "S2", "client_id": "C1", "provider_id": "P1",
         "service_code": "97155", "service_date": "2026-03-03", "center_id": "CTR-SD",
         "duration_value": 60, "duration_uom": "minutes", "status": "completed",
         "source_system": "ehr"},
        {"session_id": "S3", "client_id": "C1", "provider_id": "P1",
         "service_code": "97153", "service_date": "2026-05-04", "center_id": "CTR-SD",
         "duration_value": 12, "duration_uom": "units", "status": "completed",
         "source_system": "ehr"},
        {"session_id": "S4", "client_id": "C2", "provider_id": "P2",
         "service_code": "97153", "service_date": "2026-05-05", "center_id": "CTR-SD",
         "duration_value": 8, "duration_uom": "units", "status": "completed",
         "source_system": "ehr"},
        {"session_id": "S5", "client_id": "C2", "provider_id": "P2",
         "service_code": "97153", "service_date": "2026-05-06", "center_id": "CTR-SD",
         "duration_value": 10, "duration_uom": None, "status": "completed",
         "source_system": "ehr"},
        {"session_id": "S6", "client_id": "C2", "provider_id": "P2",
         "service_code": "99999", "service_date": "2026-05-07", "center_id": "CTR-SD",
         "duration_value": 4, "duration_uom": "units", "status": "completed",
         "source_system": "ehr"},
        {"session_id": "S7", "client_id": "C1", "provider_id": "P1",
         "service_code": "97153", "service_date": "2026-05-08", "center_id": "CTR-SD",
         "duration_value": "n/a", "duration_uom": "units", "status": "completed",
         "source_system": "ehr"},
        {"session_id": "S8", "client_id": "C1", "provider_id": "P3",
         "service_code": "92507", "service_date": "2026-05-11", "center_id": "CTR-SD",
         "duration_value": 1, "duration_uom": "units", "status": "completed",
         "source_system": "ehr"},
        {"session_id": "S9", "client_id": "C1", "provider_id": "P1",
         "service_code": "97153", "service_date": "2026-03-02", "center_id": "CTR-SD",
         "duration_value": 180, "duration_uom": "minutes", "status": "completed",
         "source_system": "ehr"},
    ])


@pytest.fixture
def client_changes() -> pd.DataFrame:
    """C1 changes payer once; C2 never does; C3 changes twice."""
    return pd.DataFrame([
        {"client_id": "C1", "effective_date": "2025-06-01", "age_years": 5,
         "home_center_id": "CTR-SD", "payer_id": "PAY-001", "change_reason": "enrollment"},
        {"client_id": "C1", "effective_date": "2026-04-01", "age_years": 5,
         "home_center_id": "CTR-SD", "payer_id": "PAY-002", "change_reason": "payer_change"},
        {"client_id": "C2", "effective_date": "2025-09-15", "age_years": 9,
         "home_center_id": "CTR-SD", "payer_id": "PAY-003", "change_reason": "enrollment"},
        {"client_id": "C3", "effective_date": "2025-01-10", "age_years": 3,
         "home_center_id": "CTR-TEM", "payer_id": "PAY-001", "change_reason": "enrollment"},
        {"client_id": "C3", "effective_date": "2025-08-01", "age_years": 3,
         "home_center_id": "CTR-TEM", "payer_id": "PAY-004", "change_reason": "payer_change"},
        {"client_id": "C3", "effective_date": "2026-02-01", "age_years": 3,
         "home_center_id": "CTR-TEM", "payer_id": "PAY-005", "change_reason": "payer_change"},
    ])


@pytest.fixture
def providers_raw() -> pd.DataFrame:
    return pd.DataFrame([
        {"provider_id": "P1", "role": "RBT", "discipline": "ABA",
         "center_id": "CTR-SD", "hire_date": "2024-01-05", "term_date": ""},
        {"provider_id": "P2", "role": "BCBA", "discipline": "ABA",
         "center_id": "CTR-SD", "hire_date": "2023-03-01", "term_date": "2026-06-30"},
        {"provider_id": "P3", "role": "SLP", "discipline": "Speech",
         "center_id": "CTR-SD", "hire_date": "2025-02-01", "term_date": ""},
    ])


@pytest.fixture
def centers_raw() -> pd.DataFrame:
    return pd.DataFrame([
        {"center_id": "CTR-SD", "center_name": "San Diego", "state": "CA"},
        {"center_id": "CTR-TEM", "center_name": "Temecula", "state": "CA"},
    ])


@pytest.fixture
def payers_raw() -> pd.DataFrame:
    return pd.DataFrame([
        {"payer_id": "PAY-001", "payer_name": "Meridian", "contract_type": "value_based"},
        {"payer_id": "PAY-002", "payer_name": "Pacific", "contract_type": "value_based"},
        {"payer_id": "PAY-003", "payer_name": "Statewide", "contract_type": "fee_for_service"},
        {"payer_id": "PAY-004", "payer_name": "Cascade", "contract_type": "fee_for_service"},
        {"payer_id": "PAY-005", "payer_name": "Unified", "contract_type": "value_based"},
    ])
