"""The task runner: ordering, retries, failure isolation, logging."""

from __future__ import annotations

import json

import pytest

from hourglass.orchestration import Orchestrator, Task, TaskStatus, toposort

NO_SLEEP = lambda _: None


def task(name, fn=None, **kwargs):
    return Task(name=name, fn=fn or (lambda ctx: None), **kwargs)


class TestToposort:
    def test_orders_dependencies_first(self):
        order = [t.name for t in toposort([
            task("c", depends_on=("b",)), task("a"), task("b", depends_on=("a",))])]
        assert order == ["a", "b", "c"]

    def test_is_stable_for_independent_tasks(self):
        """Same input, same order, every run.

        Without this two runs of the same pipeline produce differently-ordered
        logs and diffing them stops being useful.
        """
        tasks = [task("x"), task("y"), task("z")]
        assert [t.name for t in toposort(tasks)] == ["x", "y", "z"]
        assert [t.name for t in toposort(tasks)] == ["x", "y", "z"]

    def test_detects_a_cycle(self):
        with pytest.raises(ValueError, match="cycle"):
            toposort([task("a", depends_on=("b",)), task("b", depends_on=("a",))])

    def test_detects_a_missing_dependency(self):
        with pytest.raises(ValueError, match="not defined"):
            toposort([task("a", depends_on=("nope",))])

    def test_handles_a_diamond(self):
        order = [t.name for t in toposort([
            task("d", depends_on=("b", "c")), task("b", depends_on=("a",)),
            task("c", depends_on=("a",)), task("a")])]
        assert order.index("a") < order.index("b") < order.index("d")
        assert order.index("c") < order.index("d")


class TestExecution:
    def test_runs_in_order_and_threads_context(self):
        seen = []

        def first(ctx):
            seen.append("first")
            return {"value": 1}

        def second(ctx):
            seen.append("second")
            return {"value": ctx["value"] + 1}

        result = Orchestrator(
            [task("second", second, depends_on=("first",)), task("first", first)],
            run_id="r1", sleep=NO_SLEEP).run()

        assert seen == ["first", "second"]
        assert result.context["value"] == 2
        assert result.succeeded is True

    def test_records_duration_per_task(self):
        result = Orchestrator([task("a")], run_id="r1", sleep=NO_SLEEP).run()
        assert result.tasks[0].duration_seconds >= 0
        assert result.tasks[0].status is TaskStatus.SUCCEEDED

    def test_seed_context_is_available(self):
        captured = {}
        Orchestrator([task("a", lambda ctx: captured.update(ctx))],
                     run_id="r1", sleep=NO_SLEEP).run({"seed": 42})
        assert captured["seed"] == 42


class TestRetries:
    def test_retries_until_it_succeeds(self):
        attempts = {"n": 0}

        def flaky(ctx):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RuntimeError("transient")
            return {"ok": True}

        result = Orchestrator([task("flaky", flaky, retries=3)],
                              run_id="r1", sleep=NO_SLEEP).run()
        assert result.succeeded is True
        assert result.tasks[0].attempts == 3

    def test_gives_up_after_the_limit(self):
        def always(ctx):
            raise RuntimeError("permanent")

        result = Orchestrator([task("always", always, retries=2)],
                              run_id="r1", sleep=NO_SLEEP).run()
        assert result.succeeded is False
        assert result.tasks[0].attempts == 3          # 1 initial + 2 retries
        assert "permanent" in result.tasks[0].error

    def test_no_retries_means_one_attempt(self):
        """Retrying a deterministic transform that just failed only fails slower."""
        def boom(ctx):
            raise ValueError("deterministic")

        result = Orchestrator([task("boom", boom)], run_id="r1", sleep=NO_SLEEP).run()
        assert result.tasks[0].attempts == 1

    def test_backoff_is_exponential(self):
        waits = []

        def always(ctx):
            raise RuntimeError("nope")

        Orchestrator([task("always", always, retries=3, backoff_seconds=1.0)],
                     run_id="r1", sleep=waits.append).run()
        assert waits == [1.0, 2.0, 4.0]


class TestFailureIsolation:
    def test_downstream_tasks_are_skipped_not_run(self):
        ran = []

        def boom(ctx):
            raise RuntimeError("upstream broke")

        result = Orchestrator([
            task("extract", boom),
            task("transform", lambda ctx: ran.append("transform"),
                 depends_on=("extract",)),
            task("load", lambda ctx: ran.append("load"), depends_on=("transform",)),
        ], run_id="r1", sleep=NO_SLEEP).run()

        assert ran == []
        statuses = {t.name: t.status for t in result.tasks}
        assert statuses["extract"] is TaskStatus.FAILED
        assert statuses["transform"] is TaskStatus.SKIPPED
        assert statuses["load"] is TaskStatus.SKIPPED

    def test_independent_branches_still_run(self):
        """A failure should not stop work that does not depend on it."""
        ran = []

        result = Orchestrator([
            task("bad", lambda ctx: (_ for _ in ()).throw(RuntimeError("x"))),
            task("good", lambda ctx: ran.append("good")),
        ], run_id="r1", sleep=NO_SLEEP).run()

        assert ran == ["good"]
        assert result.succeeded is False

    def test_failed_task_is_reported(self):
        result = Orchestrator(
            [task("bad", lambda ctx: (_ for _ in ()).throw(RuntimeError("x")))],
            run_id="r1", sleep=NO_SLEEP).run()
        assert result.failed_task is not None
        assert result.failed_task.name == "bad"
        assert result.failed_task.traceback


class TestLogging:
    def test_writes_json_lines(self, tmp_path):
        log = tmp_path / "run.jsonl"
        Orchestrator([task("a"), task("b", depends_on=("a",))],
                     run_id="r1", log_path=log, sleep=NO_SLEEP).run()

        records = [json.loads(line) for line in log.read_text().splitlines()]
        events = [r["event"] for r in records]
        assert events[0] == "run_started"
        assert events[-1] == "run_finished"
        assert "task_succeeded" in events
        assert all(r["run_id"] == "r1" for r in records)

    def test_every_line_is_valid_json(self, tmp_path):
        """A log a machine cannot parse is a log nobody queries."""
        log = tmp_path / "run.jsonl"
        Orchestrator(
            [task("bad", lambda ctx: (_ for _ in ()).throw(RuntimeError("x")))],
            run_id="r1", log_path=log, sleep=NO_SLEEP).run()
        for line in log.read_text().splitlines():
            json.loads(line)

    def test_appends_across_runs(self, tmp_path):
        log = tmp_path / "run.jsonl"
        for run_id in ("r1", "r2"):
            Orchestrator([task("a")], run_id=run_id, log_path=log,
                         sleep=NO_SLEEP).run()
        ids = {json.loads(line)["run_id"] for line in log.read_text().splitlines()}
        assert ids == {"r1", "r2"}

    def test_timing_table_renders(self):
        result = Orchestrator([task("a"), task("b")], run_id="r1",
                              sleep=NO_SLEEP).run()
        table = result.timing_table()
        assert "a" in table and "b" in table
