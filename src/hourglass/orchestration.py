"""A small task runner: dependencies, retries, timing, structured logs.

This is not Airflow and does not want to be. It is the amount of orchestration
a single-process pipeline actually needs, written out rather than left implicit
in the order of statements in a function, because four things become possible
once the steps are objects instead of lines:

* **Dependencies are declared, not implied.** A step that reads the lake
  declares that it needs the step that wrote it. Reordering the file cannot
  silently break the pipeline, and the cycle check fails at construction rather
  than at three in the morning.

* **Retries live where the flakiness is.** A network call to a payer API gets
  three attempts with backoff; a pure transform gets one, because retrying
  deterministic code that just failed is a way of failing more slowly.

* **Failure isolates.** When a task fails, everything downstream of it is
  marked SKIPPED rather than run against missing inputs, so the log says what
  went wrong once instead of cascading.

* **Every run is measurable.** Per-task duration and status go to a JSON-lines
  file. "The pipeline got slower" is a question you can answer from data
  instead of a feeling.

The honest limit: this runs in one process, in order, on one machine. It has no
scheduler, no distributed execution, no backfill window, and no cross-run
concurrency control. Those are the reasons Airflow exists, and the point at
which this should be replaced rather than extended.
"""

from __future__ import annotations

import json
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"      # an upstream task failed
    RETRIED = "retried"


@dataclass
class Task:
    """One step.

    ``fn`` takes the shared context dict and returns a dict merged back into
    it. Passing a mutable context around is simpler than threading twelve
    positional arguments through, and the merge-on-return convention keeps the
    data flow visible at the call site.
    """

    name: str
    fn: Callable[[dict], dict | None]
    depends_on: tuple[str, ...] = ()
    retries: int = 0
    backoff_seconds: float = 0.5
    description: str = ""

    # Retries are opt-in per task rather than global. A global retry policy
    # either retries pure functions pointlessly or gives up on network calls
    # too early; there is no single number that is right for both.


@dataclass
class TaskRun:
    name: str
    status: TaskStatus
    attempts: int
    duration_seconds: float
    started_at_utc: str
    error: str | None = None
    traceback: str | None = None

    def to_dict(self) -> dict:
        return {
            "task": self.name, "status": self.status.value, "attempts": self.attempts,
            "duration_seconds": round(self.duration_seconds, 3),
            "started_at_utc": self.started_at_utc, "error": self.error,
        }


@dataclass
class RunResult:
    run_id: str
    succeeded: bool
    tasks: list[TaskRun] = field(default_factory=list)
    context: dict = field(default_factory=dict)

    @property
    def duration_seconds(self) -> float:
        return sum(t.duration_seconds for t in self.tasks)

    @property
    def failed_task(self) -> TaskRun | None:
        return next((t for t in self.tasks if t.status is TaskStatus.FAILED), None)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "succeeded": self.succeeded,
            "duration_seconds": round(self.duration_seconds, 3),
            "tasks": [t.to_dict() for t in self.tasks],
        }

    def timing_table(self) -> str:
        width = max((len(t.name) for t in self.tasks), default=10)
        total = self.duration_seconds or 1.0
        lines = []
        for t in self.tasks:
            share = t.duration_seconds / total
            bar = "█" * max(0, round(share * 24))
            retry = f"  ({t.attempts} attempts)" if t.attempts > 1 else ""
            lines.append(
                f"  {t.name:<{width}}  {t.duration_seconds:7.3f}s  "
                f"{share:5.1%}  {bar}{retry}"
            )
        return "\n".join(lines)


# ---------------------------------------------------------------------------


def toposort(tasks: list[Task]) -> list[Task]:
    """Order tasks so every dependency precedes its dependents.

    Kahn's algorithm, with ties broken by declaration order so the resulting
    sequence is stable across runs. A stable order matters more than it looks:
    without it, two runs of the same pipeline produce differently-ordered logs
    and diffing them becomes useless.
    """
    by_name = {t.name: t for t in tasks}
    for task in tasks:
        for dep in task.depends_on:
            if dep not in by_name:
                raise ValueError(
                    f"Task {task.name!r} depends on {dep!r}, which is not defined."
                )

    remaining = {t.name: set(t.depends_on) for t in tasks}
    ordered: list[Task] = []
    while remaining:
        ready = [name for t in tasks
                 if (name := t.name) in remaining and not remaining[name]]
        if not ready:
            raise ValueError(
                "Dependency cycle among: " + ", ".join(sorted(remaining))
            )
        for name in ready:
            ordered.append(by_name[name])
            del remaining[name]
            for deps in remaining.values():
                deps.discard(name)
    return ordered


class Orchestrator:
    """Runs tasks in dependency order, with retries and a structured log."""

    def __init__(
        self,
        tasks: list[Task],
        run_id: str,
        log_path: Path | None = None,
        on_event: Callable[[str, dict], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.tasks = toposort(tasks)
        self.run_id = run_id
        self.log_path = log_path
        self.on_event = on_event
        # Injectable so tests exercise the retry logic without waiting for it.
        # A test that really sleeps is a test somebody eventually deletes.
        self._sleep = sleep
        self._events: list[dict] = []

    def _emit(self, event: str, **payload) -> None:
        record = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "run_id": self.run_id, "event": event, **payload,
        }
        self._events.append(record)
        if self.on_event:
            self.on_event(event, record)

    def _flush(self) -> None:
        if not self.log_path:
            return
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as fh:
            for record in self._events:
                fh.write(json.dumps(record, default=str) + "\n")

    def run(self, context: dict | None = None) -> RunResult:
        context = dict(context or {})
        result = RunResult(run_id=self.run_id, succeeded=True, context=context)
        failed: set[str] = set()

        self._emit("run_started", task_count=len(self.tasks))

        for task in self.tasks:
            blocked = sorted(set(task.depends_on) & failed)
            if blocked:
                result.tasks.append(TaskRun(
                    name=task.name, status=TaskStatus.SKIPPED, attempts=0,
                    duration_seconds=0.0,
                    started_at_utc=datetime.now(UTC).isoformat(timespec="seconds"),
                    error=f"upstream failed: {', '.join(blocked)}"))
                failed.add(task.name)
                self._emit("task_skipped", task=task.name, blocked_by=blocked)
                continue

            started = datetime.now(UTC)
            begin = time.perf_counter()
            attempts, error, tb = 0, None, None

            for attempt in range(1, task.retries + 2):
                attempts = attempt
                try:
                    self._emit("task_started", task=task.name, attempt=attempt)
                    produced = task.fn(context)
                    if produced:
                        context.update(produced)
                    error = None
                    break
                except Exception as exc:
                    error, tb = f"{type(exc).__name__}: {exc}", traceback.format_exc()
                    self._emit("task_attempt_failed", task=task.name,
                               attempt=attempt, error=error)
                    if attempt <= task.retries:
                        # Exponential, deterministic. No jitter: this pipeline
                        # is single-writer, so there is no thundering herd to
                        # spread out, and determinism is worth more here than
                        # collision avoidance.
                        self._sleep(task.backoff_seconds * (2 ** (attempt - 1)))

            duration = time.perf_counter() - begin
            status = TaskStatus.FAILED if error else TaskStatus.SUCCEEDED
            result.tasks.append(TaskRun(
                name=task.name, status=status, attempts=attempts,
                duration_seconds=duration,
                started_at_utc=started.isoformat(timespec="seconds"),
                error=error, traceback=tb))

            if error:
                failed.add(task.name)
                result.succeeded = False
                self._emit("task_failed", task=task.name, attempts=attempts,
                           duration_seconds=round(duration, 3), error=error)
            else:
                self._emit("task_succeeded", task=task.name, attempts=attempts,
                           duration_seconds=round(duration, 3))

        self._emit("run_finished", succeeded=result.succeeded,
                   duration_seconds=round(result.duration_seconds, 3))
        self._flush()
        result.context = context
        return result
