# okfpipe — repo hygiene, knowledge layer, and benchmark task generation

A repo-agnostic pipeline that takes a URL or path to any Python repository and
produces (1) a pinned, containerized, tested, lint-clean version of it, (2) a
machine-readable knowledge layer, and (3) validated benchmark tasks for AI
coding agents, each with machine-generated proof that it is a real task.

Built for the *Founding Software Engineer — AI Task Benchmark & Evaluation
Infrastructure* take-home. Target repository: `https://github.com/mahmoud/glom`.

---

## Quick start

```bash
./run.sh https://github.com/mahmoud/glom.git
```

Requirements: **Python ≥ 3.10** on the host (the pipeline itself has no
third-party dependencies) and a **running Docker daemon**. Everything that
resolves dependencies, runs tests or validates a task happens inside a
container, so the host's Python version cannot affect any result.

Run one stage at a time:

```bash
./run.sh <repo> --stages hygiene     # pin, containerize, generate tests, lint
./run.sh <repo> --stages knowledge   # repo_graph.json + .okf/
./run.sh <repo> --stages tasks       # mine, build and validate tasks
```

Useful flags: `--task-count N`, `--mutants N`, `--validation-repeats N`,
`--no-format`, `--skip-mutation`. Full list: `./run.sh --help`.

### Verify the container acceptance bar yourself

```bash
cd output/glom
docker compose run --rm tests        # builds if needed, then runs the suite
docker compose run --rm tests        # run it twice; results are identical
```

### Verify any single task yourself

```bash
cd tasks/<task_id>
./verifier/run.sh input              # must FAIL, on a behavioural assertion
./verifier/run.sh solution           # must PASS
cat evidence/verdict.json            # what the pipeline recorded
```

`verifier/run.sh` copies the tree into a scratch directory before running, so
`input/` and `solution/` are never modified and the commands are re-runnable.

---

## What is where

```
run.sh                  single entry point for all three stages
pipeline/okfpipe/       pipeline source
  repo.py                 clone/copy, git history, tree export
  hygiene/                stage 1
    detect.py               the ONLY module that learns anything repo-specific
    deps.py                 dependency pinning (in-container resolution)
    container.py            Dockerfile / compose / run-tests.sh generation
    testgen.py              doctest materialisation + characterisation probing
    mutate.py               deliberate bug injection, to measure test quality
    lint.py                 ruff config, autofix, adoption baseline
    stage.py                orchestration + the determinism check
  knowledge/              stage 2
    graph.py                AST -> nodes/edges, plus edge re-verification
    stage.py                coverage contexts, claims, .okf/ emission
  tasksrc/                stage 3
    mine.py                 history + excision candidate mining
    netnew.py               capability-gap detectors
    model.py                task record, solution-leak and prescription checks
    runner.py               running a tree, reading the verdict
    validate.py             the six validation gates
    stage.py                orchestration + tasks.json

output/glom/            the transformed repository
output/glom/.okf/       the knowledge layer
output/repo_graph.json  the consolidated graph
tasks/<id>/             validated tasks with evidence
tasks.json              task manifest
REPORT.md               design, trade-offs, scale answer, honest gaps
transcripts/            agent prompts, verification log, decisions
```

---

## The short version of the design

**Determinism comes from the container boundary.** Dependencies are resolved
*inside* `python:<ver>-slim` pinned by digest, not on the developer's machine,
so environment markers and wheel selection become properties of the artifact
rather than of whoever ran it. Build backends are pinned too, so
`--no-build-isolation` closes the last unpinned surface inside an otherwise
pinned image.

**Generality comes from one detection point.** `hygiene/detect.py` is the only
module permitted to learn anything about a specific repo, and it learns it at
runtime by reading the checkout. Everything else consumes a `RepoProfile`.

**Claims are measured, not asserted.** The pipeline injects bugs and reports how
many its tests catch; it re-verifies every graph edge against the source with a
code path that does not share logic with the one that produced it; and it
validates every task through six separate gates, keeping the rejected
candidates' evidence so the selection criteria can be audited.

`REPORT.md` has the full argument, the numbers, and the honest gaps.
