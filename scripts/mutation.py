#!/usr/bin/env python3
"""Run mutmut against one module, paired with the test file that covers it.

Why this exists rather than a `[tool.mutmut]` block in pyproject.toml:

mutmut 3 takes its configuration only from pyproject.toml, and it needs a
different `source_paths` and a different test selection for every module you
want to score. Committing one such block would pin the repository to a single
target and make `mutmut run` mean whatever the last edit left behind. This
script copies the tree to a scratch directory, writes the config it needs there,
and runs mutmut against the copy, so the working tree is never modified and each
target is reproducible from its command line alone.

The pairing matters for a second reason. The full suite takes about 44 seconds;
a mutation run executes it once per mutant. Running only the test file that
exercises the mutated module takes about a second, which is the difference
between a run that finishes and a run that does not. The cost is that a mutant
killed only by some *other* test file is scored as survived here -- the score is
therefore a lower bound on what the whole suite would achieve.

    python scripts/mutation.py disclosure --tests tests/test_disclosure.py

`source_paths` is the whole package, not the single module, because mutmut runs
the tests against a copied `mutants/` package: naming one file there would leave
the rest of the package missing and every import of a sibling module would fail.
The mutant-name filter is what restricts the run to the target.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CONFIG = """
[tool.mutmut]
source_paths = ["src/hourglass"]
pytest_add_cli_args_test_selection = [{tests!r}]
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("module", help="module under src/hourglass, without .py")
    ap.add_argument("--tests", required=True, help="test file to run per mutant")
    ap.add_argument("--max-children", type=int, default=4)
    ap.add_argument("--keep", action="store_true",
                    help="keep the scratch directory for `mutmut show`")
    args = ap.parse_args()

    if not (ROOT / "src" / "hourglass" / f"{args.module}.py").exists():
        print(f"no such module: {args.module}", file=sys.stderr)
        return 2

    work = Path(tempfile.mkdtemp(prefix=f"mutation-{args.module}-"))
    target = work / "hourglass"
    shutil.copytree(
        ROOT, target,
        ignore=shutil.ignore_patterns(
            ".git", "__pycache__", ".pytest_cache", ".ruff_cache", "data",
            "mutants", "*.egg-info"),
    )

    pyproject = target / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text().split("[tool.mutmut]")[0]
        + CONFIG.format(tests=args.tests)
    )

    cmd = ["mutmut", "run", "--max-children", str(args.max_children),
           f"hourglass.{args.module}.*"]
    print(f"$ {' '.join(cmd)}   (in {target})")
    run = subprocess.run(cmd, cwd=target)

    subprocess.run(["mutmut", "results"], cwd=target)
    if args.keep:
        print(f"\nscratch kept at {target}")
        print(f"  cd {target} && mutmut show <mutant-name>")
    else:
        shutil.rmtree(work, ignore_errors=True)
    return run.returncode


if __name__ == "__main__":
    raise SystemExit(main())
