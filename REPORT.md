# REPORT

okfpipe — repo hygiene, knowledge layer, and benchmark task generation.
Target repository: `https://github.com/mahmoud/glom`.
Held-out verification repository: `https://github.com/sdispater/tomlkit` (chosen
*after* the pipeline was written, for a different build backend and a different
test layout).

> **Results tables are filled in from the delivered artifacts at the end of this
> document.** Every number in them is reproducible from `output/run-summary.json`,
> `output/glom/.okf/`, and `tasks.json`.

---

## 1. What was broken in the repo, and how the pipeline fixes each class of problem

glom is not a neglected repository — it has a real test suite, CI across eleven
interpreters, and a tox config. That mattered for scoping: the brief describes a
codebase "lacking dependency pinning, tests, linting, formatting configuration,
and containerization", and only three and a half of those are actually true
here. Saying so up front is more useful than pretending otherwise, because it
changes what the test generator has to be.

### 1.1 Runtime dependencies were never pinned

`setup.py` declares `install_requires=['boltons>=19.3.0', 'attrs', 'face>=20.1.1']`
— one dependency with no constraint at all. A fresh install on two different
days resolves to two different dependency sets.

There *is* a `requirements.txt`, but it is a developer-tooling lock, not a
package lock: its own header says it was compiled by `pip-compile` **with Python
3.7**, it pins `tox` and `virtualenv` alongside the library's real dependencies,
and it carries `coverage<=7.2.7` with the comment "can unpin when dropping
py37". It documents a Python 3.7 development environment that no longer exists.

**Fix.** `hygiene/deps.py` resolves the project's own metadata — plus exactly
the extras the test suite actually imports — inside a digest-pinned
`python:3.11-slim` container, and writes `requirements.lock` with exact versions
**and hashes**, alongside a runtime-only `requirements-runtime.lock`.

Two design points carry most of the value:

- **Resolution happens in the container, not on the host.** This build ran on
  Windows. A lock resolved there encodes that host's environment markers and
  wheel selection, and is then installed on Linux — so it is not a lock, it is a
  guess at one. Resolving inside the target image makes the platform a property
  of the artifact.
- **Build backends are pinned too.** `pip install -e .` with default build
  isolation fetches setuptools/hatchling/poetry-core fresh at image-build time.
  That is an unpinned surface *inside* an otherwise fully pinned image. The
  pipeline reads `[build-system] requires` (falling back to the PEP 517 default
  for legacy `setup.py` projects), folds it into the resolver's floor so it
  lands in the lock, and then passes `--no-build-isolation`. Either half alone
  would be worse than doing nothing.

### 1.2 No containerization at all

No Dockerfile, no compose file, no single command that builds and tests.

**Fix.** `hygiene/container.py` generates a `Dockerfile`, a `docker-compose.yml`,
a `.dockerignore` and a `run-tests.sh`. The reproducibility properties are
deliberate:

| Property | Mechanism |
|---|---|
| Base image cannot drift | `FROM python@sha256:…`, resolved by digest at pipeline run time |
| Dependencies cannot drift | `pip install --require-hashes -r requirements.lock` |
| Build backend cannot drift | pinned in the lock + `--no-build-isolation` |
| Output cannot wobble | `PYTHONHASHSEED=0`, `TZ=UTC`, `LC_ALL=C.UTF-8` |
| Tests cannot reach the network | compose service runs `network_mode: none` |
| Single documented command | `docker compose run --rm tests` builds if needed, then runs |

A compose service for a backing store is added **only** when the pinned
dependency set actually contains a client for one (postgres, redis, mongo,
mysql, elasticsearch). glom has none, so it gets none, and the test container is
network-isolated as a result.

`run-tests.sh` discovers its test roots at runtime rather than baking them in.
That is not cosmetic: the generated-tests directory does not exist on a fresh
checkout or before stage 1c runs, and naming a missing path makes pytest exit 4
— a failure with nothing to do with the code under test. This bug actually
shipped in the first draft (see §6 and `transcripts/02-verification-log.md`).

### 1.3 No linter or formatter configuration

No ruff, flake8, black or pre-commit config anywhere.

**Fix.** `hygiene/lint.py` writes a `ruff.toml` (one tool, one config, one pinned
version, doing both linting and formatting), applies **safe fixes only**, then
runs the formatter.

Three judgement calls, the third of which came directly out of the held-out run
and is the most important thing in this section:

- **Only safe fixes are applied.** ruff labels a fix "unsafe" when it may change
  behaviour. Applying those unattended to an unfamiliar repository is exactly
  the silent change this pipeline exists to prevent.

- **Whatever remains is baselined, not hidden.** Violations no autofix can
  resolve move into an explicit `extend-ignore` block with a per-rule occurrence
  count and a comment marking them pre-existing, recorded in
  `.okf/hygiene.json`. The repo is genuinely lint-clean under a config that is
  honest about what it postponed. The alternative dishonesty — selecting almost
  no rules so nothing fires — is also "lint-clean" and means nothing.

- **"Safe fix" is the linter's opinion, so it is verified rather than trusted.**
  On the held-out repo, ruff's `UP017` rewrote a compatibility shim:

  ```python
  def tz_utc() -> tzinfo:
      try:
          from datetime import timezone
          return timezone.utc      # rewritten by --fix to: return UTC
      except ImportError:
          class UTC(_tzinfo):      # the only definition of UTC
              ...
  ```

  `UTC` exists only in the `except` branch, so the rewritten `try` branch raises
  `UnboundLocalError`. ruff classifies that fix as **safe**. It broke a test.

  Lint is therefore applied as a **tested ladder** — `fix+format`, then
  `fix-only`, then `config-only` — where each rung is applied, the image
  rebuilt, and the suite *run*, keeping the first rung that does not lose a
  passing test. The last rung changes no source at all, so it cannot fail: a
  repo whose code cannot be safely modified still ends up lint-clean, with
  everything baselined and its behaviour untouched. Which rung was kept is
  recorded in `.okf/hygiene.json` → `lint.mode`.

  The first version of this reverted only the *formatting* on regression and
  then re-ran the same `--fix`, reapplying the identical break after the
  comparison that would have caught it. On glom every rung passes and that bug
  would have shipped invisibly; only a repo I had not designed against exposed
  it. That is the entire argument for the held-out exercise.

### 1.4 Coverage gaps, and doctests that CI runs but a normal `pytest` does not

glom's tox config runs `pytest --doctest-modules` against the *installed*
package. A developer running plain `pytest` never executes those examples, and
the CLI and tutorial modules are the thinnest-covered parts of the codebase.

**Fix.** `hygiene/testgen.py` runs two generators, and this is the part of the
brief worth being most precise about:

**Doctest materialisation.** Every docstring containing executable examples
becomes an individually-named pytest case that runs exactly that docstring's
examples and fails with the real expected-vs-actual diff. These are *authored*
expectations — a human wrote down what the output should be — so a failure means
documented behaviour changed. This is the higher-value half.

**Characterisation probing.** For callables with no examples, arguments are
synthesised from type hints, defaults and a typed value bank; each call is
executed in the pinned container; and the observed result is written back as a
literal assertion (`assert f(-3) == 0`) or an expected exception.

> **These pin current behaviour; they do not validate it.** A generated
> assertion encodes what the code *does*. It will catch a regression or an
> injected bug — which is real and is what §5 measures — but if the current
> behaviour is already wrong, the test enshrines the bug. That distinction is
> stated in the generated files' headers, and it is why the doctest half is
> weighted first.

Every case is filtered before it ships: it must repr identically under two
different `PYTHONHASHSEED` values, its repr must be free of memory addresses,
absolute paths and timestamps, its value must round-trip through `repr()`, and
the emitted test must actually pass. Cases that fail are quarantined
function-by-function over up to three rounds; a generated file that cannot be
made green is discarded entirely rather than shipped red.

### 1.5 No way to tell whether any of the tests are worth anything

The brief says generated tests will be judged on whether they "catch
deliberately introduced bugs". So rather than assert that, the pipeline injects
the bugs and measures it — see §5.

---

## 2. Design decisions and trade-offs — what was automated, what was not, and why

### Automated

Dependency resolution, Dockerfile/compose generation, coverage measurement
(including per-test contexts), doctest materialisation, characterisation
probing, lint configuration and baselining, the mutation sweep, the entire
knowledge layer, candidate mining from git history, excision, capability-gap
detection, task materialisation, and all six validation gates. The full run is
one command.

### Deliberately *not* automated

**Rewriting non-deterministic tests.** `container.scan_nondeterminism` flags
test lines touching `datetime.now`, `random.*`, `uuid1/4`, `tempfile.mktemp` and
`socket.socket`, and reports them. It does not rewrite them. Freezing someone's
clock is a behavioural change the pipeline has no business making unattended,
and the report is the useful artifact.

**Resolving baselined lint violations.** See §1.3.

**Selecting *which* dependency-group members matter.** Poetry's `dev` group and
PEP 735 groups routinely mix test tooling with docs and static-analysis tooling.
The pipeline takes a group named for testing whole, and filters a catch-all
group against a small denylist of documentation/packaging/type-checker
packages, because pulling Sphinx and its theme into an image whose only job is
running unit tests costs build time and size for nothing. That denylist is a
heuristic and is the kind of thing that needs maintenance at fleet scale.

**Writing prose instructions from scratch.** Task instructions are assembled
from things that already exist and are checkable — the maintainer's own commit
subject, the test author's docstrings, and the *observed failure symptom* from
running the verifier against the input tree. No language model is in the
pipeline's runtime path, which keeps it deterministic and offline-capable, at
the cost of instructions that read like good bug reports rather than like
polished prose. That trade is deliberate: a hand-written instruction that cannot
be regenerated on the held-out repo is worth less than a mechanical one that can.

### Trade-offs taken

**One image for every task's validation, rather than one image per task.**
Per-task images would be more faithful for historical trees whose dependency
requirements differed. Building ~20 images would dominate runtime, and glom's
dependency surface is stable across the mined range. The consequence is real and
is listed in §7: a task mined from a commit that predates a dependency change
could validate against dependencies it never saw. The `imports` and `collects`
gates catch the cases where this actually breaks; they would not catch a subtle
behavioural difference.

**Static analysis over runtime introspection for the graph.** Importing modules
to introspect them would give perfect call resolution and would execute
arbitrary module-level code from an unfamiliar repository — and would fail on
exactly the half-broken repos this pipeline targets. Static parsing means call
resolution is imperfect, so every call edge is labelled `exact` or `heuristic`
and the unresolved targets are counted and published rather than hidden.

**Mining depth capped by a probe budget.** Each history candidate costs two
container runs to probe. The budget is an explicit constant, and every rejected
candidate is recorded with its reason in `tasks.json`.

---

## 3. How task-candidate selection works: what was mined, what was rejected, and on what grounds

### 3.1 History-derived (the brief requires ≥ 4 of 10)

**Mined from:** the full commit log with per-commit file statistics, classified
in `.okf/history.jsonl`.

**Eligibility filter.** A commit must have a parent; must not be classified as
noise (docs, typos, changelog, version bumps, formatting, merges); must touch
between one and three first-party `.py` source files; must touch at least one
test file; and must change between 2 and 400 lines.

The "must touch tests" requirement is the load-bearing one. A commit that
changed behaviour *and* changed tests comes with a verifier its own author
wrote. A commit without test changes would need one invented, and an invented
verifier is the pipeline's opinion about what the commit meant.

**Ranking.** Bug fixes outrank features; one- or two-file changes outrank
three-file ones; changes of 8–120 lines are preferred as big enough to be
interesting and small enough to scope. Candidates are then taken round-robin by
primary file, so one heavily-churned module cannot monopolise the shortlist.

**The probe — where most candidates die.** For each shortlisted commit the
pipeline exports the parent tree and the commit tree, overlays the *post*-commit
versions of the changed test files onto the *parent* tree, and runs them against
both. A candidate is kept only if the parent tree fails with real test failures
(not collection or import errors), the commit tree is fully green, and at least
one test flips red → green.

**The verifier is then restricted to exactly the tests that flipped.** This is
the most important decision in the whole stage. A test file that a commit
touched is mostly tests that already passed; running the whole file would mean
an agent that changes nothing scores nearly green, and the signal would be
dominated by behaviour the task is not about. Not breaking the rest is a
separate concern, and it is checked separately by the no-collateral gate.

**Rejection grounds, all recorded:** no test flips (the commit was a refactor,
or the tests were already passing); the commit tree is not green under the
current pinned dependency set (the test depended on something since changed);
the parent tree produces import errors rather than failures (the test file
imports something the parent commit does not have — a structural failure, which
the brief explicitly says does not count).

### 3.2 Excision (≤ 4 of 10)

**Mined from:** `.okf/symbols.jsonl`, filtered to public functions with ≥ 3
covering tests, ≥ 6 lines, cyclomatic complexity ≥ 3, and a docstring. Sorted
most-covered and most-complex first, because those have the sharpest contracts.

The covering-test list comes from coverage run with
`dynamic_context = test_function`, which records *which test* executed each
line. That mapping is what makes the task answerable: it identifies the tests
that define the excised function's contract, rather than guessing from names.

**Construction.** The body is replaced by `raise NotImplementedError(...)`,
keeping decorators, signature and docstring. The stub must still parse, the
package must still import, and the covering tests must actually go red — all
verified by running them before the task is built.

**Rejection grounds:** docstring-only functions (nothing to excise); stubs that
break the import; functions whose covering tests still pass after excision
(meaning the tests do not actually pin that function's behaviour — a finding in
its own right).

### 3.3 Net-new (≤ 3 of 10)

Capability gaps detected from the knowledge layer, **only where a correct
reference solution is mechanically derivable**. A detector that cannot derive
one declines rather than guess.

- **`__eq__` without `__hash__`.** A class defining equality without a hash is
  unhashable, so it cannot be a dict key or a set member. The reference solution
  mirrors `__eq__`'s own comparison expression. That derivation is the whole
  point: the naive version — hash a tuple of the attribute *names* mentioned in
  `__eq__` — produces `hash((self.path_t,))` for glom's `Path`, which hashes
  something different from what equality compares. It would pass a "does not
  raise" check and be subtly broken.
- **Missing `__all__` on a public module.** The reference list is derived from
  the symbol table: symbols the module itself defines, public, non-duplicated.

The authored tests assert *properties* (the hash/equality invariant; the
declaration's required characteristics), not a particular implementation, so any
correct answer passes. Validation is the backstop — a derived solution that does
not make its own tests pass fails `pass_after` and the task is dropped.

### 3.4 Instruction quality, checked mechanically

Before validation, every instruction goes through two automated checks whose
results ship in `evidence/instruction_quality.json`:

- **Solution leak.** The instruction is tokenised and compared against the lines
  the reference diff *adds*. Any run of six consecutive shared tokens, or any
  verbatim code line, rejects the task.
- **Implementation prescription.** Patterns that tell the agent what to type
  rather than what must be true — "change X to Y", "replace … with", "add a
  parameter", "on line N", fenced code blocks — reject the task.

Neither check proves an instruction is good. Both prove a specific instruction
is bad, which is the useful direction.

---

## 4. How to run everything

### Prerequisites

Python ≥ 3.10 on the host (the pipeline has no third-party dependencies) and a
running Docker daemon. Nothing else needs installing: every tool the pipeline
uses is fetched into a pinned container.

### Full pipeline, one command

```bash
./run.sh https://github.com/mahmoud/glom.git
```

Writes `output/glom/` (the transformed repo), `output/repo_graph.json`,
`output/glom/.okf/`, `tasks/`, `tasks.json` and `output/run-summary.json`.

### Individual stages

```bash
./run.sh <repo_url_or_path> --stages hygiene
./run.sh <repo_url_or_path> --stages knowledge
./run.sh <repo_url_or_path> --stages tasks --task-count 10 --validation-repeats 3
```

Stages 2 and 3 consume stage 1's output image, so run stage 1 first (or use
`--stages all`).

### The container test run — the acceptance bar

```bash
cd output/glom
docker compose run --rm tests
docker compose run --rm tests     # twice; identical results
```

Equivalent without compose:

```bash
cd output/glom
docker build -t okf-glom:latest .
docker run --rm --network none okf-glom:latest ./run-tests.sh
```

### Validating a task by hand

```bash
cd tasks/<task_id>
./verifier/run.sh input       # must FAIL, on a behavioural assertion
./verifier/run.sh solution    # must PASS
cat evidence/verdict.json
```

`verifier/run.sh` copies the tree into a scratch directory first, so `input/`
and `solution/` are never modified and both commands are repeatable.

### Re-running the whole validation harness over the delivered tasks

```bash
./run.sh https://github.com/mahmoud/glom.git --stages tasks --validation-repeats 5
```

### Useful flags

| Flag | Effect |
|---|---|
| `--task-count N` | target number of validated tasks (default 10) |
| `--mutants N` | mutation budget for the injected-bug sweep (default 60) |
| `--validation-repeats N` | determinism repeat count per task (default 3) |
| `--no-format` | apply the linter but not the formatter |
| `--skip-mutation` | skip the slowest stage-1 step |
| `--out`, `--tasks-out`, `--work` | artifact locations |

`OKF_LOG_LEVEL=debug ./run.sh …` prints every container invocation.

---

## 5. Results

All figures below are read out of the delivered artifacts by
`transcripts/results.py`; §8 maps each claim to the file it comes from.

### 5.1 Stage 1 — environment

| | |
|---|---|
| Base image | `python@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534` |
| Dependencies pinned | **14**, exact versions **with hashes**, resolved from `setup.py` + the `test` extra |
| Build backends pinned | `setuptools>=68`, `wheel` → `--no-build-isolation` is safe |
| Compose services | none (no backing-store client in the dependency set → test container runs `network_mode: none`) |
| Lint outcome | ladder rung **`fix+format`** kept: 66 violations autofixed, 26 files formatted, **0 remaining**, **0 baselined** |
| System packages | none (git omitted: no VCS build backend, no test shells out to git) |

**Acceptance bar — build, then run the suite twice:**

| Run | Exit | Tests | Passed | Failed | Outcome fingerprint |
|---|---|---|---|---|---|
| baseline (untouched repo) | 0 | 202 | 202 | 0 | `743c4344dc5a` |
| final-1 | 0 | 268 | 268 | 0 | `0d1f3c033362` |
| final-2 | 0 | 268 | 268 | 0 | `0d1f3c033362` |

`deterministic: true`. The fingerprint is a SHA-256 over the sorted
`(test id, outcome)` pairs from the JUnit report, so the two runs agree
test-by-test — not merely in exit code. Both runs execute the image's own baked
`/app`, with only an output directory mounted, which is exactly what
`docker compose run --rm tests` does.

### 5.2 Stage 1 — generated tests

66 test functions across 8 files, added to the repo's existing 202:

| Source | Kept | Notes |
|---|---|---|
| Doctest materialisation | 65 docstrings | authored expectations, one test per docstring |
| Characterisation — values | 6 | `assert f(x) == <literal>` |
| Characterisation — exceptions | 2 | `pytest.raises(...)` |
| Dropped | 38 candidates | non-deterministic across hash seeds, unreprable, uniform-exception-only targets, or failed on first run |

The "uniform-exception-only" filter is why the count fell from an earlier 96:
a target where every synthesised input raised the *same* builtin exception was
dropped whole, because "passing a string where an object is expected raises
AttributeError" pins no behaviour anyone relies on. The mutation score was
unchanged by removing them (81.7% before and after), which is the evidence that
they were filler.

### 5.3 Stage 1 — do the tests catch injected bugs?

60 single-edit mutants, restricted to statements inside function bodies on lines
the suite actually executes:

| | |
|---|---|
| Mutants run | 60 (0 discarded as unparsable) |
| **Killed** | **49 → mutation score 81.7%** |
| Killed by the repo's existing suite | 49 |
| Killed by the generated suite alone | 14 |
| Killed *only* by the generated suite | **0** |
| Survivors | 11, each listed individually in `.okf/mutation.json` |

**The honest reading.** On glom the generated tests add **no unique kill
power** — every bug they catch, the existing suite already caught. That is the
expected result for a well-tested library and it is stated rather than buried.
The number that would matter on a repo without tests is the 14: a suite
generated from nothing detects 14 of 60 injected bugs where the repo would
otherwise score zero.

The 11 survivors are the actionable output — real holes where a behaviour
change goes undetected, concentrated in `glom/cli.py` (error-message constants)
and `glom/core.py`.

### 5.4 Stage 2 — knowledge layer

| | |
|---|---|
| Modules / symbols | 37 / 704 |
| Edges | 3,759 (`calls` 2,476, `contains` 765, `imports` 424, `raises` 136, `inherits` 38, `decorates` 12) |
| Edge confidence | 3,107 `exact` / 744 `heuristic` (labelled, never mixed silently) |
| **Edges re-verified against source** | **99.95%** (3,757 / 3,759) |
| Claims | 1,411 |
| **Claims verified** | **100%** (1,411 / 1,411) |
| Claim kinds | `definition` 456, `covered_by` 427, `signature` 363, `raises` 136, `import_time_only` 19, `uncovered` 10 |
| Commits mined | 1,050 (`fix` 144, `feature` 142, `other` 522, `noise` 242) |
| Coverage | 98.0% of 4,423 statements, with per-test contexts on 24 files |

Edge verification re-reads the raw source line and looks for the token the edge
names — a code path that shares no logic with the AST walk that produced it, so
agreement means something.

The 100% claim rate is a property of the *extractor*, not a free pass: it is
100% because claims that could not be substantiated are not emitted. An earlier
run sat at 98.65% because 19 classes were claimed "has no test exercising it"
while line coverage showed their bodies executing at import time. Those now emit
a distinct `import_time_only` claim that is true as stated.

### 5.5 Stage 3 — delivered tasks

**10 validated tasks, all six gates passing, spanning 6 distinct modules**
(the brief requires ≥ 4), using all three sources:

| Task | Source | Difficulty | Verifier cases | Module |
|---|---|---|---|---|
| `hist-fdafeea7` | history | medium | 2 | `glom.core` |
| `hist-0da761ff` | history | medium | 2 | `glom.reduction` |
| `hist-93c33eb8` | history | medium | 2 | `glom.flat` |
| `hist-70c3d9cc` | history | medium | 1 | `glom.control_flow` |
| `hist-a7f5a4c2` | history | easy | 2 | `glom.mutation` |
| `exc-glom-core-format-target-spec-trace` | excision | hard | 28 | `glom.core` |
| `exc-glom-core-format-invocation` | excision | hard | 26 | `glom.core` |
| `exc-glom-grouping-group` | excision | hard | 12 | `glom.grouping` |
| `new-path-hashable` | net-new | medium | 6 | `glom.core` |
| `new-glom-core-public-api` | net-new | easy | 5 | `glom.core` |

Composition: 5 history / 3 excision / 2 net-new (brief: ≥ 4 history, ≤ 4
excision, ≤ 3 net-new). Difficulty: 2 easy, 5 medium, 3 hard.

Source quotas are **reserved**, not filled greedily. An earlier run let history
and excision take all ten slots and delivered zero net-new tasks — legal under
the brief's table, but it hides whether that miner works at all and costs
task-type diversity.

**12 candidates rejected**, every one recorded in `tasks.json` with its reason:

| Count | Source | Reason |
|---|---|---|
| 4 | excision | verifier did not collect / fail / pass cleanly |
| 3 | history | verifier not green on the commit tree under current pins |
| 2 | history | no test flips red → green (the commit was a refactor) |
| 2 | history | parent tree gives collection errors, not behavioural failures |
| 1 | excision | stub broke the import, or covering tests still passed |

That last history category is the brief's rule enforced mechanically: a failure
caused by an import error does not count, so those candidates are dropped rather
than dressed up.

### 5.6 Held-out repository

See §1.3 and `transcripts/02-verification-log.md` for what it found.

---

## 6. Scale answer: what breaks at 100 repos

**What holds.** The container boundary, the digest-pinned base image, the
single-detection-point rule, the evidence format, and the validation gates are
all per-repo and independent. Running 100 repos is 100 independent invocations.

**What breaks first, in order:**

1. **Wall-clock, dominated by container starts.** A single run makes dozens of
   `docker run` calls, and history probing is two runs per candidate. At 100
   repos with a 26-candidate budget that is on the order of 10⁴ container
   starts. *Fix:* keep one long-lived container per repo and drive it over exec
   instead of starting a fresh one per step; parallelise across repos with a
   worker pool sized to available cores; make the probe phase batch many
   candidates into one container invocation the way the mutation sweep already
   does.

2. **The baseline gate becomes the dominant failure mode, correctly.** Most
   real-world repos will not have a green suite on first contact — missing
   system libraries, network-dependent tests, pinned-to-ancient-Python code. The
   pipeline currently stops and says so, which is right for one repo and useless
   for a fleet. *Fix:* a triage layer that classifies *why* the baseline failed
   into actionable buckets (missing apt package, network access needed,
   interpreter too new, flaky), auto-retries the mechanical ones, and routes the
   rest to a human queue. The value at scale is in that classifier, not in the
   happy path.

3. **Test-collection variance.** pytest plugins in a repo's own config
   (`-p randomly`, `xdist`, custom fixtures) change collection and ordering. The
   pipeline neutralises `PYTEST_ADDOPTS` and disables the cache provider, which
   is not enough in general. *Fix:* run collection twice with different seeds as
   a pre-flight and quarantine repos whose collection is unstable before mining
   from them.

4. **Dependency resolution against the real index.** 100 repos × several
   resolution attempts is meaningful PyPI traffic, and a yanked release or an
   index outage makes runs non-reproducible over time. *Fix:* an internal index
   mirror or a shared wheel cache, and store the resolved lock as the
   reproducibility anchor so a re-run never re-resolves.

5. **Storage.** Each task ships a full `input/` and `solution/` tree. For glom
   that is small; for a large repo, ten tasks is ten pairs of full checkouts.
   *Fix:* store tasks as a base commit plus a patch, and materialise trees on
   demand. The current format was chosen for a grader who wants to read the
   files, and that trade stops making sense at fleet scale.

6. **Task quality becomes the bottleneck, not task quantity.** At 100 repos the
   mining is cheap and the judgement is expensive. The per-task signals already
   collected — mutation score of the verifier, instruction leak report, number
   of discriminating tests, cross-module reach — should become a *ranking
   model*, and the pipeline should deliver the top N by predicted quality rather
   than the first N that validate.

**What I would build differently, knowing this.** I would invert the
orchestration: instead of a CLI that runs three stages for one repo, a work
queue where each stage is an idempotent, independently-retryable job keyed by
`(repo, commit, stage)`, writing to content-addressed storage. Everything in
this codebase that returns a dataclass and writes JSON is already shaped for
that; the CLI is the part that would go.

---

## 7. Honest gaps

**Known-weak, in rough order of how much it would bother me in review:**

1. **Characterisation tests pin behaviour rather than validate it.** Covered at
   length in §1.4. They are labelled as such in every file they appear in.

2. **One image validates every task, including historical trees.** See §2. A
   task mined from a commit predating a dependency change is validated against
   dependencies it never saw. The `imports` and `collects` gates catch the loud
   version of this; a subtle behavioural difference would pass unnoticed.

3. **Static call resolution is imperfect.** Dynamic dispatch, `getattr`, and
   decorators that rewrite call targets are not resolved. Every call edge is
   labelled `exact` or `heuristic` and the unresolved-target histogram is
   published in `.okf/manifest.json`, so the graph's accuracy is measurable —
   but "measurable" is not "complete".

4. **Excision fail-before is a `NotImplementedError` from the function under
   test.** That is a genuine behavioural failure — the function does not do what
   its contract says — and the pipeline proves the tree still imports and the
   verifier still collects before counting it. It is nonetheless a weaker signal
   than an assertion about a wrong *value*, and I would rather have both. A
   stronger version would excise to a contract-shaped wrong answer, which needs
   return-type inference the pipeline does not do.

5. **Net-new detectors are shallow.** Two detectors, both structural. They find
   real gaps and produce validated tasks, but neither requires the cross-module
   reasoning that makes a benchmark task interesting. This is the area I would
   spend the next day on: net-new tasks derived from *invariants* in the
   knowledge layer (a function documented as idempotent that is not; a pair of
   functions documented as inverses that do not round-trip) would be
   substantially better and are mechanically detectable.

6. **The mutation sweep measures the suite, not each task's verifier.** The
   per-task verifiers would benefit from the same treatment — mutate the task's
   `files_in_scope` and confirm the verifier's selection kills the mutants — to
   prove a verifier is not trivially satisfiable. The machinery exists
   (`hygiene/mutate.py` takes a `files` argument and a suite map); wiring it
   into stage 3 is a small job I did not get to.

7. **Only one held-out repo was tested.** `tomlkit` exercises a different build
   backend and a different test layout, which is the highest-value single
   choice, but one repo is one data point. A `src/` layout, a namespace package,
   and a repo needing system libraries are the next three I would try.

8. **Determinism is demonstrated, not proven.** Repeated runs on one machine on
   one day is evidence, not a guarantee. Genuine determinism would need the same
   check across machines and across time, with the lock and image digest as the
   anchors. Both anchors exist; the cross-machine run does not.

9. **No cleanup of Docker artifacts.** The pipeline leaves its built image
   behind, deliberately, so stages 2 and 3 can reuse it. On a fleet that needs a
   retention policy.

---

## 8. Reproducing the claims in this report

**A note on where `.okf/` lives.** The brief lists `output/` as containing the
transformed repo "plus `repo_graph.json` and `.okf/`". `repo_graph.json` is at
`output/repo_graph.json`; the `.okf/` directory is written *inside* the
transformed repo, at `output/glom/.okf/`. That is deliberate: the knowledge
layer describes one specific commit of one specific tree, so keeping it beside
that tree means it is copied, versioned and shipped with the thing it describes
rather than drifting from it. Every path in the tables below reflects this.

Everything above is derived from artifacts in this delivery:

| Claim | Where to check it |
|---|---|
| Pins, image digest, lock contents | `output/glom/requirements.lock`, `output/glom/Dockerfile` |
| Build succeeds and tests pass twice identically | `output/glom/.okf/hygiene.json` → `runs[]`, `deterministic` |
| Generated test counts and what was dropped | `output/glom/.okf/hygiene.json` → `tests` |
| Injected bugs caught, and every survivor | `output/glom/.okf/mutation.json` |
| Lint state and any baselined rules | `output/glom/.okf/hygiene.json` → `lint`, and `output/glom/ruff.toml` |
| Graph edges re-verified against source | `output/repo_graph.json` → `verification.edges` |
| Knowledge claims re-verified against code | `output/glom/.okf/manifest.json` → `verification.claims` |
| Per-task validation evidence | `tasks/<id>/evidence/verdict.json` and siblings |
| Every rejected candidate and why | `tasks.json` → `rejected_candidates` |

The pipeline's own source is lint-clean under the same rule set it applies to
its targets (`python -m ruff check pipeline/okfpipe --select E,F,W,B,UP
--line-length 100`).
