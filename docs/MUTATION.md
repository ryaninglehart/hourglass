# Mutation testing

The suite has 461 tests. The fair question about any number like that is
whether the tests assert anything or merely execute the code. This document
answers it for the two modules where the tests are the entire safety argument.

The scores below were measured when the suite stood at 308 tests and a full run
took about 44 seconds. Neither figure changes a mutation score — each mutant was
checked against one test file, not against the suite — but both appear in the
cost arithmetic further down, and the current numbers are used there.
`disclosure.py` was also rewritten after its run, which matters more than either
count; that is set out in full at the head of its score.

## What mutation testing measures that coverage does not

Coverage records which lines ran. It cannot record whether anything checked the
result, so a test that calls a function and asserts nothing reports 100% line
coverage and catches nothing. Mutation testing changes the source instead —
flips a `<` to a `<=`, replaces a constant, deletes a keyword argument — reruns
the suite, and asks whether any test failed. A mutant that makes the suite fail
is *killed*; a mutant that leaves it green *survived*, and every survivor is a
change to production behaviour that the tests are indifferent to. The score is
killed over total. Coverage measures reach, mutation testing measures grip.

## What was run

| | |
|---|---|
| Tool | mutmut 3.7.0 (`pip install mutmut --break-system-packages`) |
| Python | 3.11 |
| Targets | `src/hourglass/disclosure.py`, `src/hourglass/transform.py` |
| Paired tests | `tests/test_disclosure.py` (28 test functions today, 26 at the time of the run; pytest collects more, several are parametrised), `tests/test_transform.py` (21) |
| Runner | `python -m pytest <paired test file>`, one invocation per mutant |
| Parallelism | `--max-children 4` |

Each mutant is checked against only the test file that covers its module, not
against the whole suite. A full run now takes about 79 seconds; multiplied by 97
mutants that is a little over two hours for one module, and the 942 `transform.py`
mutants would be roughly twenty hours. The paired file takes about a second.
The trade is stated in the limits section: a mutant that only some other test
file would have killed is scored here as survived, so these numbers are a lower
bound on the whole suite's real strength.

mutmut 3 reads its configuration solely from `pyproject.toml`, and it needs a
different target and test selection per module. Rather than commit a block that
pins the repository to one target, `scripts/mutation.py` copies the tree to a
scratch directory, writes the config there and runs mutmut against the copy:

    python scripts/mutation.py disclosure --tests tests/test_disclosure.py
    make mutation

Two configuration details cost time and are worth recording. `paths_to_mutate`
and `tests_dir` are the documented options in most of what is written about
mutmut; in 3.7.0 they are deprecated in favour of `source_paths` and
`pytest_add_cli_args_test_selection`, and passing `tests_dir` as a string raises
`TypeError: can only concatenate list (not "str") to list` before the run
starts. Separately, `source_paths` must name the whole package rather than the
single target module: mutmut runs the tests against a copied `mutants/` package,
and naming one file leaves the siblings absent, so `transform.py` fails at
`from .config import SERVICE_BY_CODE` with `ModuleNotFoundError` and every
mutant is scored as killed by an import error.

## Score

### `disclosure.py` — complete, re-run 2026-08-20

| | |
|---|---|
| Mutants generated | 80 |
| Killed | 78 |
| Survived | 2 |
| Timed out | 0 |
| Suspicious | 0 |
| Score | 78/80 = **97.5%** |
| Score excluding proven equivalents | 78/78 = **100%** |

Rate 22.5 mutants/second against `tests/test_disclosure.py` alone, about four
seconds of mutant execution plus mutmut's baseline and coverage-mapping phases.

Two survivors, and both are equivalent mutants — changes that cannot alter
observable behaviour. Neither claim is asserted from reading the code; both
were checked by running the mutated expression against every input that
reaches it.

**`needs_suppression__mutmut_1`** — `if value is None or pd.isna(value)`
becomes `and`. Under `or`, a null of any kind returns early. Under `and`, only
literal `None` returns early and everything else falls through to
`float(value)`, where `0 < nan <= 10` is `False`. Both paths return `False`.
Evaluated on `None`, `nan`, `pd.NA`, `pd.NaT`, `0`, `1`, `10`, `11`, `-3`,
`"n/a"`, `""`, `3.5`, `True` and `False`: identical on all fourteen.

**`suppress_grouped__mutmut_3`** — `df.groupby(group_column, sort=False)`
becomes `sort=None`. pandas treats `None` as `False` here; grouping the same
frame both ways yields `['TEM', 'SD']` in both cases. The sibling mutants
`sort=True` and the omitted argument *are* killed, by
`test_group_order_follows_the_input_not_the_alphabet`, which is the evidence
that the surviving one is a property of pandas rather than a gap in the tests.

**Three real gaps this run closed.** The previous run of this module scored
79.4% against an earlier implementation, and its most interesting survivor was
`suppress_grouped__15`, which deleted the `derived_columns` argument from the
grouped path so a masked count kept a visible percentage beside it —
recoverable by multiplication, and precisely the failure the module's docstring
names as the one implementations usually miss. Every ungrouped test asserted
the rule and created the impression it held everywhere; no grouped test ever
passed a non-empty `derived_columns`.

That one is now killed, along with two others this run surfaced and the earlier
one had not:

* `suppress_counts__31` replaced the whole `suppressed` flag column with
  `None`. Every existing test asserted on the *masked values* and passed.
  `digest._plan_centres` routes on that column to decide which centres lose
  their heading, and `if hidden` is falsy for `None`, so the mutant produced a
  digest that pooled nothing and published every small centre by name. A column
  another module makes a privacy decision on needed its own assertions.
* `suppress_grouped__25/27/28` flipped `ignore_index=True`, leaving the caller
  duplicate index labels. Worth recording *how* the test for it was written:
  the first version used a frame whose centres were already contiguous, so
  grouping and re-concatenating happened to reproduce the original index and
  the assertion held whether or not the index was reset. Mutation testing said
  so — the mutant survived a test written specifically to kill it. The rows are
  now interleaved, which is the only arrangement that distinguishes the two.

Nine new tests across two classes, `TestTheFlagColumn` and `TestFrameShape`.
The score moved 79.4% → 97.5%, but the score is not the point: three of those
gaps were in behaviour another module depends on for a privacy guarantee, and
the file that guarantee lives in was fully green throughout.

## Surviving mutants — `disclosure.py`, previous run (superseded)

Kept as a record of what the module looked like before the re-run above, and
because the analysis is the part worth reading — the score is a headline, the
survivors are the finding.

> **Read this section as history.** It was measured against an implementation
> that has since changed twice. `suppress_counts`'s complementary pass was a
> `while` loop with a `passes` counter; the loop was found to be incapable of
> iterating — one published total and one hidden cell is a single equation, so
> a second suppression always ends it — and it is now a single
> `if total_is_published and suppressed.sum() == 1:`. `SuppressionReport` has
> no `passes` field and `summary()` renders no pass count. Then nine tests were
> added in response to the findings below.
>
> So: the line numbers refer to the file as it stood at that run. Eight of the
> twenty survivors — the five `passes` mutants, `suppress_counts__29`, and the
> two loop-condition equivalents — mutate code that no longer exists, and both
> timeout explanations depend on a loop that cannot now hang. The
> `suppress_grouped` findings were real, and are fixed; see the re-run above.
> The numbers are kept rather than deleted, because a dated superseded
> measurement is honest and a silently removed one is not.

Twenty survivors, falling into six groups rather than twenty independent
problems.

| # | Line | Mutation | Verdict |
|---|---|---|---|
| `needs_suppression__1` | 81 | `value is None or pd.isna(value)` → `and` | Equivalent |
| `suppress_counts__9` | 113 | `pd.to_numeric(out[c], errors="coerce")` → drops `errors` | **Real gap** |
| `suppress_counts__17` | 120 | `passes = 1` → `passes = 2` | **Real gap** (minor) |
| `suppress_counts__35` | 129 | `passes += 1` → `passes = 1` | **Real gap** (minor) |
| `suppress_counts__36` | 129 | `passes += 1` → `passes -= 1` | **Real gap** (minor) |
| `suppress_counts__37` | 129 | `passes += 1` → `passes += 2` | **Real gap** (minor) |
| `suppress_grouped__23` | 162 | `combined.passes = max(...)` → `= None` | **Real gap** (minor) |
| `suppress_counts__21` | 122 | `(~suppressed).sum() > 0` → `suppressed.sum() > 0` | Equivalent |
| `suppress_counts__22` | 122 | `(~suppressed).sum() > 0` → `>= 0` | Equivalent |
| `suppress_counts__29` | 125 | `break` → `return` | **Real gap** |
| `suppress_counts__43` | 137 | `out["suppressed"] = suppressed.to_numpy()` → `= None` | **Real gap** |
| `suppress_counts__44` | 137 | column renamed to `XXsuppressedXX` | **Real gap** |
| `suppress_counts__45` | 137 | column renamed to `SUPPRESSED` | **Real gap** |
| `suppress_grouped__3` | 155 | `groupby(..., sort=False)` → `sort=None` | **Real gap** (minor) |
| `suppress_grouped__5` | 155 | `sort=False` argument deleted | **Real gap** (minor) |
| `suppress_grouped__6` | 155 | `sort=False` → `sort=True` | **Real gap** (minor) |
| `suppress_grouped__15` | 156 | `derived_columns` argument dropped from the inner call | **Real gap** |
| `suppress_grouped__30` | 165 | `pd.concat(..., ignore_index=True)` → `None` | **Real gap** (minor) |
| `suppress_grouped__32` | 165 | `ignore_index=True` argument deleted | **Real gap** (minor) |
| `suppress_grouped__33` | 165 | `ignore_index=True` → `False` | **Real gap** (minor) |

### The one that matters: `suppress_grouped__15`

The mutant deletes `derived_columns` from `suppress_grouped`'s call into
`suppress_counts`, so the grouped path masks the count and leaves every derived
column — the percentage, the share, the hours — untouched next to it. That is
precisely the failure the module's own docstring calls "the most common way an
implementation defeats itself": a visible `12.5%` beside a withheld count
recovers the count by multiplication.

The suite does test this. `test_derived_columns_go_with_the_count` asserts it,
and it kills the equivalent mutation in the ungrouped path. It gives no
protection at all to the grouped path, because no grouped test passes
`derived_columns` — and that is the shape of the problem worth noticing. A
passing test about a privacy rule creates the impression that the rule is
enforced everywhere, when it is enforced on the one code path the test happened
to take. Coverage is silent here: `suppress_grouped` is well covered, the line
executes, the argument is simply never non-empty.

The distinguishing input is a two-centre table with a `pct` column where one
centre has a small count, passed with `derived_columns=("pct",)`. The test that
should exist:

    def test_grouped_derived_columns_go_with_the_count(self):
        df = table(("SD", "ABA", 3, 0.05), ("SD", "Speech", 40, 0.60),
                   ("SD", "OT", 22, 0.35), ("TEM", "ABA", 55, 0.5),
                   ("TEM", "Speech", 60, 0.5))
        out, _ = disclosure.suppress_grouped(
            df, "centre", "children", "service", derived_columns=("pct",))
        assert (out.loc[out["children"] == SUPPRESSED, "pct"] == SUPPRESSED).all()

One qualification, stated because it bounds the severity: `suppress_grouped` has
no caller in `src/` today. The digest calls `suppress_counts` directly, in
`_plan_centres` at `digest.py:145`, and passes no derived columns. This is an
untested public function, not a live leak. It is also the one part of this
analysis that the rewrite did not touch, so it is the part still worth acting
on.

### The other real gaps

**`suppress_counts__9` — the coercion is unasserted.** Removing
`errors="coerce"` restores pandas' default of `errors="raise"`, so a count
column containing a non-numeric value raises `ValueError` out of
`suppress_counts` instead of coercing to `NaN` and treating the cell as
publishable. `test_non_numeric_is_not_suppressed` covers this at the level of
`needs_suppression("n/a")`, but never through the frame API. Needed:
`test_a_non_numeric_count_does_not_raise`, passing a table with `"n/a"` in the
count column and asserting the call returns.

**`suppress_counts__29` — `break` becomes `return`.** *This mutant no longer
exists: the `break` went with the loop.* The bare `return` yielded `None` from a
function whose contract is a two-tuple, so every caller failed at unpacking. It
survived because no test reached the branch, and the branch was reachable: a
table with one small count and one `NaN` count entered the complementary loop
(one cell suppressed, one unsuppressed) and then found no non-null candidate to
sacrifice. The input is still worth a test —
`table(("SD", "ABA", 3, 0.05), ("SD", "Speech", None, 0.6))`, asserting the call
returns a frame with the small cell suppressed — because the guarded branch
survives the rewrite as `if candidates.notna().sum():` and is still unreached.

**`suppress_counts__43/44/45` — the `suppressed` column is never read.** The
function writes a boolean audit column and no test asserts its name, dtype or
contents, so setting it to `None` or renaming it to `SUPPRESSED` passes. The
column is the machine-readable record of which cells were withheld. Needed: one
assertion in the existing `test_small_cells_are_masked` —
`assert list(out["suppressed"]) == [True, False, True]` — which kills all three.

**The `passes` cluster — five survivors, one missing assertion.** *This cluster
is moot: `SuppressionReport` no longer has a `passes` field and `summary()` no
longer renders a pass count.* Mutants 17, 35, 36, 37 and `suppress_grouped__23`
all corrupted the pass counter: the initial value, the increment, and the
cross-group `max`. Nothing asserted `report.passes`, even though it was
published — `SuppressionReport.summary()` rendered "(resolved in N
pass/passes)" into the quality report, so `combined.passes = None` produced
"resolved in None passes" in an audit artifact and the suite stayed green,
because `test_summary_states_both_kinds` checked two substrings and not the
number. The counter was removed with the loop rather than asserted, which
closes the gap by deleting the thing that had it. The transferable half is the
observation, and it still applies to the `suppressed` column above: a value that
reaches an audit artifact needs an assertion on the value, not on the sentence
around it.

**`suppress_grouped__3/5/6` — group ordering.** `sort=False` preserves the input
order of groups; the mutants let pandas sort them alphabetically. The fixtures
happen to be in alphabetical order already (`SD` before `TEM`), so nothing
moves, and the grouped tests select rows by boolean mask rather than position,
so nothing would notice if it did. Needed: a fixture with centres in
non-alphabetical order and an assertion on `list(out["centre"])`.

**`suppress_grouped__30/32/33` — index reset.** Without `ignore_index=True` the
concatenated frame carries duplicate index labels from the per-group frames.
Every test filters by mask, so the duplication is invisible. It is not harmless:
duplicate labels break positional `.loc` lookups in any consumer, and
`is_disclosive` returns *positions* from `enumerate`, which would then no longer
agree with the caller's index. Needed:
`assert list(out.index) == list(range(len(out)))`, which kills all three.

### The equivalent mutants, and what they reveal

**`needs_suppression__1`, `or` → `and`.** With `and`, a `NaN` input no longer
returns early at the guard; it falls through to `float(nan)`, and
`0 < nan <= 10` is `False`, so the function still returns `False`. `pd.NA`
raises `TypeError` inside `float()` and is caught. Every value for which
`pd.isna` is true either converts to `nan` or raises, and both paths already
return `False`. No input distinguishes the two versions, so no test can kill it.
The mutant is a proof that the `pd.isna` guard is redundant given the
`try/except` and the range check — a simplification, not a gap.

**`suppress_counts__21` and `__22`, the loop's second condition.** *Both mutants
described a condition that has since been deleted.* Inside the loop
`suppressed.sum() == 1` already held, so the mutated conditions
(`suppressed.sum() > 0` and `(~suppressed).sum() >= 0`) were both permanently
true and the guard stopped guarding. The behaviour was unchanged anyway, because
the loop body's own `if candidates.notna().sum() == 0: break` caught exactly the
case the outer condition was there to prevent: a single-row table produces an
all-`NaN` candidate set and broke on the first iteration. Two guards, one
condition. The mutants could not be killed because the second guard made the
first unnecessary — which was worth knowing about the code, and was not a test
defect. It is also, read back, the first evidence that the loop could not
iterate: a loop whose body always leaves after one pass is an `if` written the
long way, and the mutation run said so before anyone read it that way.


## Surviving mutants — `transform.py`

### Score

| | |
|---|---|
| Mutants generated for `transform.py` | 942 |
| Killed | 482 |
| Survived | 155 |
| Not exercised by the paired test file ("no tests") | 305 |
| Timed out | 0 |
| Suspicious | 0 |
| Score over mutants the paired file actually reaches | 482/637 = **75.7%** |
| Score over every mutant generated | 482/942 = 51.2% |

Both numbers are given because neither alone is honest. The first flatters the
suite by silently discarding 305 mutants; the second penalises it for mutants
that `tests/test_transform.py` was never the right file to catch. The gap
between them is the finding.

This is where the time budget ran out. The run was launched against the whole
package so that intra-package imports resolve, and mutmut ignored the
`hourglass.transform.*` mutant filter and began a 7,515-mutant sweep. It
finished all 942 `transform.py` mutants and was partway into the next module
when the run was stopped at roughly the eight-minute mark. `transform.py` is
therefore complete; nothing else in that sweep is reported here. `diff.py` was
the third target and was not run at all.

### The 305 unreached mutants

Three functions have no test in `tests/test_transform.py` that touches them:

| Function | Mutants with no covering test |
|---|---|
| `build_fact_authorization` | 180 |
| `build_dim_date` | 95 |
| `build_dim_payer` | 30 |

`build_fact_authorization` is the largest single hole. Its docstring documents a
bug that was found and fixed — an earlier `~duplicated(keep="first")` fallback
that evaluated across the whole merged frame instead of per authorisation, and
so could attach an authorisation to the wrong client version. That fix has no
test in the module's own test file. It may be exercised indirectly through
`tests/test_pipeline.py`; a fix that was worth writing a paragraph about is
worth a named regression test next to the function it protects, and the
mutation run is what makes the absence visible.

### Survivors by function

| Function | Survived |
|---|---|
| `build_fact_session` | 40 |
| `build_dim_service` | 34 |
| `resolve_minutes` | 28 |
| `resolve_minutes_naive` | 24 |
| `build_dim_provider` | 13 |
| `_age_band` | 9 |
| `build_dim_client` | 3 |
| `build_dim_center` | 3 |
| `dedupe_sessions` | 1 |

The 155 survivors were not individually adjudicated — that is the honest limit
of what fitted in the time — so no verdict is claimed for the group as a whole.
One cluster was read in full, because it is small enough to be conclusive.

### `_age_band` — nine survivors, every boundary unverified

| Mutant | Mutation |
|---|---|
| `_age_band__2` | `if age <= 3` → `<= 4` |
| `_age_band__5` | `if age <= 5` → `<= 6` |
| `_age_band__7` | `if age <= 8` → `< 8` |
| `_age_band__8` | `if age <= 8` → `<= 9` |
| `_age_band__10` | `if age <= 12` → `< 12` |
| `_age_band__11` | `if age <= 12` → `<= 13` |
| `_age_band__9` | `return "6-8"` → `"XX6-8XX"` |
| `_age_band__12` | `return "9-12"` → `"XX9-12XX"` |
| `_age_band__13` | `return "13+"` → `"XX13+XX"` |

All nine are real gaps, and together they say something stronger than nine
separate defects: `_age_band` has no test at all that pins a boundary. Every
band edge can be moved by one year, and the labels the report prints can be
replaced with nonsense, without a single assertion failing. A child of 4 can be
filed under `0-3`; a child of 9 can be filed under `6-8`. Age band is a
reporting dimension in a paediatric programme, so a shifted edge silently
misstates who was served — and, because band membership changes cell counts, it
interacts with the small-cell suppression above.

One parametrised test kills all nine:

    @pytest.mark.parametrize("age,band", [
        (0, "0-3"), (3, "0-3"), (4, "4-5"), (5, "4-5"),
        (6, "6-8"), (8, "6-8"), (9, "9-12"), (12, "9-12"), (13, "13+"),
    ])
    def test_age_band_edges(age, band):
        assert transform._age_band(age) == band

Every case sits on an edge or one step past it, which is what makes the
boundary mutants unkillable-free rather than merely covered.

## What to fix, in order

Ordered by consequence if the mutated behaviour were real, not by how many
mutants each fixes.

1. **`test_grouped_derived_columns_go_with_the_count`** — the only survivor that
   describes a privacy failure. `suppress_grouped` currently has no caller, so
   fixing the test now is what makes it safe to acquire one.
2. **`test_age_band_edges`** — nine mutants, one parametrised test, and a
   dimension every report groups by.
3. **`build_fact_authorization` regression tests** — 180 mutants with no
   covering test in the module's own test file, guarding a fix the code comments
   describe as "invisible on this dataset and wrong in general". At minimum: a
   client with two versions, an authorisation whose period opens inside the
   second, and an assertion on the resulting `client_key`.
4. **`test_a_non_numeric_count_does_not_raise`** — the `errors="coerce"`
   survivor. A single non-numeric cell in a count column currently has no test
   standing between it and an exception.
5. **`suppressed`-column assertions** — three survivors, fixable by adding an
   assertion to a test that already exists rather than by writing a new one. The
   column is published in an audit artifact. The five `report.passes` survivors
   listed with it originally were resolved by the field's removal, not by a
   test.
6. **`build_dim_date` and `build_dim_payer`** — 125 unreached mutants. Lower
   priority because both are mechanical derivations, but "mechanical" is a claim
   the suite does not currently check.
7. **Index and group-order assertions in `suppress_grouped`** — six survivors,
   least consequence, fixed by two one-line assertions.

Nothing above proposed deleting the equivalent mutants' code. The `pd.isna`
guard in `needs_suppression` is redundant rather than wrong, and removing
redundancy from a privacy function to improve a metric is the wrong trade. The
doubled loop condition in `suppress_counts` was named here on the same footing
and did go, but for the opposite reason: it was not redundancy worth keeping,
it was a loop that could not iterate, and it was removed to make the code say
what it does rather than to move a score.

## Limits

Stated here rather than left implied, because a mutation score is easy to quote
out of context.

**It is slow, and getting slower.** 97 mutants took about 18 seconds only
because each was checked against one test file. Against the full suite as it
stood at the time of the run -- 308 tests, 44 seconds -- the same 97 mutants
would have taken roughly 70 minutes. The suite is now 461 tests and 79 seconds,
which puts the same run at a little over two hours and the 942 `transform.py`
mutants at about twenty. The cost grows with the suite, which is why this run is
scoped and why mutation testing is a periodic audit here rather than a CI gate.

**The pairing understates the suite.** Each mutant was run against one test
file. A mutant that `tests/test_pipeline.py` would have caught is scored as
survived. Every number in this document is a lower bound on what the whole suite
achieves, and the 305 "no tests" mutants in particular may well be covered
elsewhere.

**A high score does not mean the tests assert the right things.** Mutation
testing measures sensitivity to changes in the code as written. A suite can kill
every mutant of a function that implements the wrong requirement, and score
100%. If the CMS threshold in this module were wrong — if the rule were 1-to-20
rather than 1-to-10 — the tests would pin the wrong constant just as firmly, and
mutation testing would applaud. It checks that the tests are watching; it cannot
check that they are watching the right thing.

**Equivalent mutants put the ceiling below 100%, and the ceiling is not
knowable in advance.** Three of the twenty survivors on the superseded
97-mutant run could not be killed by any test, because no input distinguishes
them from the original — an attainable ceiling of 94/97, about 96.9%, for that
run. On the current run both survivors are proven equivalent, so the recorded
97.5% (78/80) is that run's ceiling, already met. The equivalent fraction
depends entirely on how much redundancy and defensive coding a given
codebase contains. Deciding which survivors are equivalent is a manual judgement
with no algorithm behind it — the general problem is undecidable. Comparing a
mutation score across projects, or treating a target percentage as a standard,
is therefore meaningless.

**This run covers two of eighteen modules** (the eighteenth is the empty
`__init__.py`). Scored: `disclosure.py` and `transform.py`. Not scored, and
this document says nothing about them:
`analytics.py`, `config.py`, `diff.py`, `digest.py`, `export.py`, `generate.py`,
`incremental.py`, `ingest.py`, `metrics.py`, `model.py`, `orchestration.py`,
`phi.py`, `pipeline.py`, `quality.py`, `sources.py`. `phi.py` and `quality.py`
carry guarantees at least as load-bearing as `disclosure.py` and are the
obvious next targets. `diff.py` was planned for this run and was not reached.

## Reproducing

    python scripts/mutation.py disclosure --tests tests/test_disclosure.py --keep
    python scripts/mutation.py transform  --tests tests/test_transform.py  --keep

`--keep` retains the scratch directory so individual survivors can be inspected:

    cd <scratch>/hourglass && mutmut show hourglass.disclosure.x_suppress_grouped__mutmut_15

mutmut's mutant-name filter is accepted but not honoured in 3.7.0; a run started
this way sweeps the whole package in module order and must be stopped once the
target module's mutants are done. `mutmut results` lists survivors, timeouts and
uncovered mutants but not killed ones, so the killed count is read from the
progress counter.
