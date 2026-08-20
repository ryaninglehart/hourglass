#!/usr/bin/env python3
"""Measure the pipeline at increasing scale, and say where it breaks.

The obvious objection to this project is that 52,000 rows is not a scale
anything is hard at, and the objection is correct. This script exists so the
answer is a table rather than an assurance.

It runs the real pipeline over progressively larger generated datasets and
reports wall-clock per stage. What it is looking for is not the absolute
numbers -- they are a laptop, single-process, SQLite -- but the *shape*: which
stages scale linearly with rows, which scale worse, and therefore which one
would have to be rewritten first.

    python scripts/benchmark.py --scales 1 4 12
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hourglass import pipeline  # noqa: E402
from hourglass.config import GEN, REPORT_DIR  # noqa: E402
from hourglass.generate import generate  # noqa: E402

ACK = {"uom_resolution_coverage": "benchmark run; the defect is seeded on purpose"}


@dataclass
class Measurement:
    scale: int
    clients: int
    session_rows: int
    total_seconds: float
    per_task: dict[str, float]

    @property
    def rows_per_second(self) -> float:
        return self.session_rows / self.total_seconds if self.total_seconds else 0.0


def measure(scale: int) -> Measurement:
    cfg = replace(
        GEN,
        n_clients=GEN.n_clients * scale,
        n_providers=max(GEN.n_providers, GEN.n_providers * scale // 2),
    )
    generate(cfg)

    started = time.perf_counter()
    result = pipeline.run(acknowledgements=ACK, prefer_s3=False, quiet=True,
                          regenerate=False)
    elapsed = time.perf_counter() - started

    per_task = {t.name: round(t.duration_seconds, 3) for t in result["run"].tasks}
    return Measurement(
        scale=scale,
        clients=cfg.n_clients,
        session_rows=len(result["frames"]["fact_session"]),
        total_seconds=elapsed,
        per_task=per_task,
    )


def render(measurements: list[Measurement]) -> str:
    lines = ["# Scale benchmark", ""]
    lines.append("Wall clock over generated data of increasing size. Single process, "
                 "SQLite, full reload on every run.")
    lines.append("")
    lines.append("| Scale | Clients | Session rows | Total | Rows/sec |")
    lines.append("|---:|---:|---:|---:|---:|")
    for m in measurements:
        lines.append(f"| {m.scale}× | {m.clients:,} | {m.session_rows:,} "
                     f"| {m.total_seconds:.2f}s | {m.rows_per_second:,.0f} |")
    lines.append("")

    tasks = list(measurements[0].per_task)
    lines.append("## Seconds per stage")
    lines.append("")
    lines.append("| Stage | " + " | ".join(f"{m.scale}×" for m in measurements)
                 + " | growth |")
    lines.append("|---|" + "---:|" * (len(measurements) + 1))
    for task in tasks:
        row = [f"{m.per_task.get(task, 0):.2f}" for m in measurements]
        first = measurements[0].per_task.get(task, 0)
        last = measurements[-1].per_task.get(task, 0)
        row_scale = measurements[-1].scale / measurements[0].scale
        # Growth relative to the data: 1.0 means the stage scales linearly with
        # rows. Above 1.0 means it degrades faster than the data grows, and
        # that is the stage that decides when this design has to be replaced.
        #
        # A baseline of zero has no ratio. `diff` is 0.00s at 1x on a cold
        # start because there is no previous build to compare against, and
        # dividing by a 1e-9 floor rendered that as 466,666,666.67x -- a
        # number so obviously wrong it discredits the column it sits in. A dash
        # says "not measurable here", which is the truth.
        if first > 0:
            cell = f"{(last / first) / row_scale:.2f}×" if row_scale else "—"
        else:
            cell = "—"
        lines.append(f"| `{task}` | " + " | ".join(row) + f" | {cell} |")
    lines.append("")
    lines.append("*Growth is per-row cost at the largest scale divided by per-row cost "
                 "at the smallest. 1.00× is linear. Anything meaningfully above it "
                 "degrades faster than the data grows.*")
    lines.append("")

    # ---- the conclusion, derived from the measurements ------------------
    #
    # This paragraph used to be a hardcoded string asserting that the full
    # reload was "the design decision that expires first". It was re-emitted
    # on every run whether or not the numbers said so, and by the time twelve
    # tasks were being measured they did not: `load` came out flat at 1.01x.
    #
    # A generated report restating a conclusion that nothing re-derives is the
    # same defect as INC-005 in a different costume -- a stated number that no
    # longer describes what it names. So the worst-growing stage is now read
    # off the table it is printed under.
    lines.append("## What this means")
    lines.append("")

    scale_ratio = measurements[-1].scale / measurements[0].scale or 1
    ranked = sorted(
        (
            (task,
             (measurements[-1].per_task.get(task, 0)
              / measurements[0].per_task[task]) / scale_ratio)
            for task in tasks
            if measurements[0].per_task.get(task, 0) > 0.005
        ),
        key=lambda kv: -kv[1],
    )
    if ranked:
        worst, worst_growth = ranked[0]
        if worst_growth > 1.05:
            lines.append(
                f"- **`{worst}` degrades fastest**, at {worst_growth:.2f}x per-row "
                f"cost from the smallest scale to the largest. It is the stage that "
                f"decides when this design has to be replaced, and it is the one to "
                f"rewrite first.")
        else:
            lines.append(
                f"- **No stage degrades faster than the data grows.** The worst is "
                f"`{worst}` at {worst_growth:.2f}x, which is within measurement "
                f"noise of linear. Wall clock is therefore not what expires first "
                f"here -- memory is, and the next bullet is the real constraint.")
    lines.append("- `conform` and `analyse` hold whole tables in pandas. They scale "
                 "with available RAM rather than with anything cleverer, and the "
                 "honest ceiling is a single machine's memory. That, not time, is "
                 "the wall this design hits.")
    lines.append("- The full reload rebuilds every table on every run. Correct and "
                 "simple at this size; `hourglass.incremental` is the path out when "
                 "it stops being either -- watermark, lookback window, merge by "
                 "business key.")
    lines.append("- SQLite is single-writer. Nothing here is concurrent, so it is not "
                 "the current constraint, but it becomes one the moment two runs "
                 "overlap or the warehouse has to serve readers during a load.")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scales", type=int, nargs="+", default=[1, 4],
                    help="Multipliers on the default client count.")
    ap.add_argument("--out", type=Path, default=REPORT_DIR / "benchmark.md")
    args = ap.parse_args(argv)

    measurements = []
    for scale in sorted(args.scales):
        print(f"  running at {scale}× ...", flush=True)
        m = measure(scale)
        print(f"    {m.session_rows:,} session rows in {m.total_seconds:.2f}s "
              f"({m.rows_per_second:,.0f} rows/sec)")
        measurements.append(m)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render(measurements), encoding="utf-8")
    (args.out.with_suffix(".json")).write_text(
        json.dumps([m.__dict__ for m in measurements], indent=2), encoding="utf-8")

    print(f"\nwrote {args.out}")
    print("\nRestoring the default dataset ...")
    generate(GEN)
    pipeline.run(acknowledgements=ACK, prefer_s3=False, quiet=True, regenerate=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
