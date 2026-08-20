"""The atomic swap, and what it does when a second build turns up.

Every other test in this project runs one pipeline at a time, which is how the
guarantee in ``atomic_build``'s docstring survived being untrue for so long.
Two runs against one output directory used to share a scratch filename and read
each other's ``run_log`` -- and the loser's audit row disappeared with no error
anywhere, which is the failure this project exists to argue against.
"""

from __future__ import annotations

import sqlite3

import pandas as pd
import pytest

from hourglass import model


def run_row(run_id: str) -> dict:
    """A complete audit row. Every column in the DDL is NOT NULL for a reason."""
    return {
        "run_id": run_id,
        "started_at_utc": "2026-08-20T09:00:00+00:00",
        "finished_at_utc": "2026-08-20T09:00:03+00:00",
        "code_version": "0.7.0",
        "ruleset_version": "1.8.0",
        "ruleset_hash": "0123456789abcdef",
        "published": 1,
        "blocking_failures": "",
        "acknowledgements": "{}",
        "refused_acknowledgements": "",
        "session_rows": 0,
        "auth_rows": 0,
        "lake_backend": "local",
    }


def run_log(path) -> pd.DataFrame:
    conn = sqlite3.connect(path)
    try:
        return pd.read_sql_query("SELECT * FROM run_log ORDER BY run_id", conn)
    finally:
        conn.close()


class TestScratchFile:
    def test_the_scratch_file_is_named_for_the_run(self, tmp_path, monkeypatch):
        """Shared, it is a shared file: the second build unlinks the first
        build's half-written database and both rename onto the same target."""
        during: list[str] = []
        real = model.load_star

        def spy(conn, frames):
            during.extend(p.name for p in tmp_path.iterdir())
            return real(conn, frames)

        monkeypatch.setattr(model, "load_star", spy)
        model.atomic_build(tmp_path / "hourglass.db", {},
                           run_log_row=run_row("abc12345"))

        assert "hourglass.building-abc12345.db" in during

    def test_nothing_is_left_behind_on_success(self, tmp_path):
        model.atomic_build(tmp_path / "hourglass.db", {},
                           run_log_row=run_row("abc12345"))
        assert [p.name for p in tmp_path.iterdir()] == ["hourglass.db"]

    def test_a_failed_build_leaves_the_previous_warehouse_in_place(
            self, tmp_path, monkeypatch):
        """The whole point of building beside the target rather than into it."""
        target = tmp_path / "hourglass.db"
        model.atomic_build(target, {}, run_log_row=run_row("first"))
        before = target.read_bytes()

        def explode(conn, frames):
            raise ValueError("load failed half way through")

        monkeypatch.setattr(model, "load_star", explode)
        with pytest.raises(ValueError):
            model.atomic_build(target, {}, run_log_row=run_row("second"))

        assert target.read_bytes() == before
        assert [p.name for p in tmp_path.iterdir()] == ["hourglass.db"]


class TestConcurrentBuilds:
    """The guarantee the docstring makes, tested from the outside.

    ``flock`` is per open file description, so a second `atomic_build` inside
    the first one contends for the lock exactly as a second process would.
    That is what makes this testable in one process without threads.
    """

    def test_a_second_build_is_refused_while_one_is_running(
            self, tmp_path, monkeypatch):
        target = tmp_path / "hourglass.db"
        refused: list[str] = []
        real = model.load_star

        def spy(conn, frames):
            # Mid-build, holding the lock. A second build must not get in.
            with pytest.raises(RuntimeError) as exc:
                model.atomic_build(target, {}, run_log_row=run_row("second"))
            refused.append(str(exc.value))
            return real(conn, frames)

        monkeypatch.setattr(model, "load_star", spy)
        model.atomic_build(target, {}, run_log_row=run_row("first"))

        assert len(refused) == 1
        assert "Another warehouse build" in refused[0]
        assert str(tmp_path) in refused[0]      # names the directory to look at

    def test_the_refusal_protects_the_first_run_audit_row(
            self, tmp_path, monkeypatch):
        """The harm a silent second build does, stated as the reason to refuse.

        Both builds read `run_log` before either swaps, so whichever renames
        second carries forward a history taken before the first one's row
        existed. The row is gone, nothing raises, and the log reads as though
        that run never happened.
        """
        target = tmp_path / "hourglass.db"
        model.atomic_build(target, {}, run_log_row=run_row("first"))
        real = model.load_star

        def spy(conn, frames):
            with pytest.raises(RuntimeError):
                model.atomic_build(target, {}, run_log_row=run_row("second"))
            return real(conn, frames)

        monkeypatch.setattr(model, "load_star", spy)
        model.atomic_build(target, {}, run_log_row=run_row("third"))

        assert list(run_log(target)["run_id"]) == ["first", "third"]

    def test_the_lock_is_released_when_the_build_finishes(self, tmp_path):
        """Sequential builds are the normal case and must not deadlock."""
        target = tmp_path / "hourglass.db"
        for run_id in ("first", "second", "third"):
            model.atomic_build(target, {}, run_log_row=run_row(run_id))
        assert list(run_log(target)["run_id"]) == ["first", "second", "third"]

    def test_the_lock_is_released_when_the_build_fails(self, tmp_path, monkeypatch):
        """A crashed build must not lock the directory for everything after it."""
        target = tmp_path / "hourglass.db"

        def explode(conn, frames):
            raise ValueError("load failed")

        monkeypatch.setattr(model, "load_star", explode)
        with pytest.raises(ValueError):
            model.atomic_build(target, {}, run_log_row=run_row("first"))

        monkeypatch.undo()
        model.atomic_build(target, {}, run_log_row=run_row("second"))
        assert list(run_log(target)["run_id"]) == ["second"]
