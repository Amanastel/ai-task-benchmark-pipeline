# Verification log

Every case where the agent's output looked right and was not. Each entry gives
the symptom, how it was caught, the root cause, and the fix — because the
catching mechanism is the transferable part.

---

## V1 — The baseline gate passed without running any tests

**Symptom.** Stage 1's first end-to-end run reported success:

```
INFO test run [baseline] result=exit=0 total=0 passed=0 failed=0 ... 
INFO okfpipe finished ok=True
```

**How it was caught.** `total=0`. The exit code said pass; the test count said
nothing ran. Reading the two together is the whole catch — either number alone
looks fine.

**Root cause.** Two independent bugs stacked:

1. The container command was `./run-tests.sh --junitxml=... 2>&1 | tail -80`.
   A shell pipeline's exit status is the *last* command's, so `tail` returning 0
   masked any pytest exit code.
2. The test command had `tests_generated` baked into it, but that directory does
   not exist until stage 1c runs. pytest exits 4 on a missing path — which the
   pipe then swallowed.

**Fix.** Removed the pipe entirely (`util.truncate` handles log size in Python),
and made `run-tests.sh` discover its test roots at runtime instead of baking a
path that does not exist yet.

**Why it matters.** This is the failure the whole rest of the design guards
against: a step that reports success without doing its job. After the fix the
same run reported `total=202 passed=202`, and the gate started gating.

---

## V2 — `git log` parsing silently dropped 90% of history

**Symptom.** `history read commits=104` for a repo with 1050 commits, and every
row had empty `source_files` and `test_files`.

**How it was caught.** Cross-checking against `git rev-list --count HEAD`. 104
is a plausible-looking number; it would have passed a glance.

**Root cause.** The record separator was appended at the *end* of the
`--format` string. With `--numstat`, each commit's file statistics are printed
after the formatted record, so splitting on a trailing separator put every
commit's numstat block at the head of the *next* chunk. The header parse then
failed for all but one record.

**Fix.** Moved the separator to the front of the format string. Verified with a
direct count assertion, not by eyeballing output.

**Downstream impact.** With empty file lists, the history miner found zero
eligible commits — the most important task source was silently producing
nothing, and the stage reported this as "0 candidates" rather than as an error.

---

## V3 — Three test-identifier formats, conflated

This one bug produced three distinct failures, which is why it gets its own
section.

There are three spellings of "which test" in play:

| Producer | Format | Example |
|---|---|---|
| coverage dynamic contexts | dotted, optional phase suffix | `glom.test.test_basic.test_x` |
| JUnit XML | `classname::name` | `glom.test.test_basic::test_x` |
| pytest (consumer) | path + `::` | `glom/test/test_basic.py::test_x` |

**V3a.** Excision tasks fed coverage contexts straight to pytest. Every
candidate was rejected with "excision did not produce failing covering tests" —
because pytest collected nothing from ids it could not parse.

**V3b.** After fixing V3a, all 11 probed history candidates and all 10 excision
candidates failed validation with `collects, fail_before, pass_after` all red,
while `imports` and `no_collateral` passed. The selection written into
`verifier/selection.txt` was still the JUnit form.

**How V3b was caught.** The gate pattern itself. `imports` passing while
`collects` failed says the tree is fine and the *selection* is not — a single
combined "does it fail?" check would have shown only "task invalid" and sent me
looking at the wrong layer.

**Fix.** Two explicit converters, `context_to_pytest_id` and `to_pytest_ids`,
each documented with the format it consumes, applied at exactly one place: the
point where a selection stops being internal and becomes the verifier's public
`selection.txt`.

**What made this expensive.** A verifier that collects nothing exits non-zero
and therefore *looks* like a correctly failing task. Only the explicit
`collects` gate distinguishes "red" from "did not run" — which is precisely the
distinction the brief asks for, and the reason it is worth implementing rather
than inferring from an exit code.

---

## V4 — A 60% mutation score built from unkillable mutants

**Symptom.** Mutation score 60%, but `killed_only_by_generated_tests` was 0 and
the survivor list read:

```
glom/_version.py:1   constant  25 -> 26
glom/__main__.py:3   comparison  == -> !=     (if __name__ == '__main__')
glom/cli.py:81       constant  '[spec [target]]' -> '[spec [target]]X'
```

**How it was caught.** Reading the survivor list rather than the score. A score
with no survivor list is unfalsifiable; printing them made the problem obvious
in seconds.

**Root cause.** Mutants were sampled from every statement in every file,
including module-level constants, entry guards and CLI help strings. None of
those can be killed by any reasonable test, so they inflate the denominator and
consume the budget.

**Fix.** Three restrictions, each with a stated rationale in `mutate.py`:
mutate only inside function bodies; only on lines coverage reports as executed
(an uncovered line is guaranteed to survive, which coverage already told us);
and skip string constants passed to display keyword arguments.

**Honest note.** The corrected score is a *different measurement*, not a better
one — it answers "of the behaviours the tests exercise, how many can be broken
undetected?" rather than "what fraction of all source tokens are pinned?". The
second question has no useful answer.

---

## V5 — Generated tests broke the suite after formatting

**Symptom.** After the first reorder, `post-lint` showed `240 passed, 1 failed`
where the baseline had been 202 passed.

**Root cause.** Test generation ran *before* `ruff format`. The generated
assertions were measured against unformatted source; reformatting then changed
a docstring the materialised doctests asserted on.

**Fix.** Reordered the stage so formatting precedes generation, and kept the
snapshot-and-rollback around formatting for the independent case where
formatting breaks the repo's *own* tests.

**Residual risk accepted.** A generated test that fails for any other reason is
quarantined (the failing function is deleted and the file re-run, up to three
rounds). If a generated file cannot be made green it is discarded entirely
rather than shipped red — the generated suite must never be the reason the
acceptance bar fails.

---

## V6 — Output tree named after a temp directory

**Symptom.** `output/source/` instead of `output/glom/`.

**Root cause.** The output directory name was derived from the *scratch
checkout* path (`.okfwork/source`) rather than from the repository identity.

**Fix.** `repo.project_name()` derives it from the URL or local path. Minor, but
it would have made every artifact path in the report wrong.

---

## V7 — The held-out repo failed the baseline gate, twice, for two real reasons

Running the pipeline against `tomlkit` (chosen after the code was written, for
its poetry-core backend and root-level `tests/`) failed immediately:

```
INFO detected repo name=tomlkit backend=poetry packages=tomlkit tests=tests py=3.11
INFO pinned dependencies count=9 hashed=True
INFO image built tag=okf-tomlkit:latest
INFO test run [baseline] result=exit=2 total=1 passed=0 failed=0 errors=1
ERROR the repository's own test suite does not pass in the container
```

Detection itself was correct — poetry backend, right package, right test
directory, nine dependencies pinned with hashes. Two things behind it were not.

**V7a — git submodules.** The one collection error was
`FileNotFoundError: /app/tests/toml-test/tests/files-toml-1.1.0`. tomlkit keeps
its TOML conformance corpus in a **git submodule**, and `git clone` without
`--recurse-submodules` produces an empty directory. The symptom is a test module
that fails at import time, which is indistinguishable from a broken repo unless
you go and read it.

*Fix:* clone with `--recurse-submodules`, falling back to a plain clone with a
warning if that fails.

**V7b — poetry dependency groups.** tomlkit's test dependencies live in
`[tool.poetry.group.dev.dependencies]`, which is not a pip extra and which the
extras logic could not see. The pins came out with 9 packages rather than the
suite's real requirements; only the tool floor happened to cover pytest.

*Fix:* read poetry groups and PEP 735 `[dependency-groups]`, translate poetry's
caret/tilde operators into PEP 440 ranges (`^7.2.0` → `>=7.2.0,<8.0.0`), and
resolve them alongside the project rather than through `--extra`. Dropping the
upper bound instead would have been easier and would have let a test-only
dependency jump a major version.

**What this validated.** Both fixes landed in `repo.py` and `detect.py` — the
two places the architecture says repo-specific knowledge is allowed. No other
module changed. That was the actual test of the "one detection point" rule, and
it held. After the fixes: `baseline … total=1058 passed=1058`.

---

## V8 — A "safe" autofix broke a test, and the rollback re-applied it

**Symptom.** After V7, the held-out run got further and then reported:

```
INFO lint applied fixed=43 formatted=10
INFO test run [post-lint] result=exit=1 total=1058 passed=1057 errors=1
WARN formatting regressed the suite; rolling back the format pass
INFO lint applied fixed=43 formatted=0
INFO test run [final-1] result=exit=1 passed=1115 errors=1     <- still broken
```

**Root cause, in two layers.**

The break was not the formatter. ruff's `UP017` ("use the `datetime.UTC`
alias") rewrote a compatibility shim:

```python
@pytest.fixture()
def tz_utc() -> tzinfo:
    try:
        from datetime import timezone
        return timezone.utc          # <- rewritten to: return UTC
    except ImportError:
        from datetime import tzinfo as _tzinfo
        class UTC(_tzinfo):          # <- the only place UTC is defined
            ...
```

`UTC` exists only in the `except` branch, so the rewritten `try` branch raises
`UnboundLocalError`. **ruff classifies this fix as safe.** It is not, on this
pattern — and the pipeline had been treating "safe fix" as a guarantee rather
than as the linter's opinion.

The second layer is mine: the rollback reverted *formatting* and then re-ran
`ruff check --fix`, which reapplied the identical break. The rollback ran after
the comparison that would have caught it, so nothing noticed.

**Fix.** Replaced the single revert with a ladder — `fix+format`, then
`fix-only`, then `config-only` — where each rung is applied, built and **tested**,
and the first that does not lose a passing test is kept. The last rung touches
no source at all, so it cannot fail: a repo whose code cannot be safely modified
still ends up lint-clean, with everything baselined and its behaviour intact.
The post-generation lint pass now reuses the rung that survived instead of
starting again from the most aggressive one.

**Why this is the most valuable thing the held-out run produced.** It is a
correctness bug in a claim the pipeline makes about itself — "safe fixes only,
so we cannot change behaviour" — and only a repo I had not designed against
exposed it. On glom, all three rungs would have passed and the bug would have
shipped invisible.

---

## V9 — A constant timeout turned a 6-minute sweep into hours

**Symptom.** On the held-out repo the mutation sweep sat at `mutants built
count=30` for 25 minutes with no further output, while the same sweep on glom
finished in about five.

**How it was caught.** Arithmetic, not observation. tomlkit's full suite runs in
4.4 seconds, so 30 mutants across two suites should cost roughly six minutes.
Twenty-five minutes with no progress meant something was waiting, not working.

**Root cause.** The per-mutant timeout was a constant 300 seconds. tomlkit is a
parser: inverting a comparison in a tokenising loop produces a mutant that does
not fail, it *hangs*. Each such mutant burned the full ceiling twice, once per
suite. Worst case for 30 mutants was five hours.

**Fix.** Both limits now derive from the measured suite duration rather than
from a constant — the subprocess ceiling is five times the real run time
(floor 60s), and pytest-timeout gets a third of that to kill an individual
hanging test first. A hanging mutant still counts as killed, which is correct:
the suite did detect it, just by not terminating.

**Why it is worth recording.** Nothing was *wrong* on the target repo — glom's
suite is fast and nothing hung, so the constant was invisible. The held-out repo
did not expose a logic bug here, it exposed a hard-coded assumption about scale.
Those are the ones that survive review and then fail in production.

---

## Standing checks that came out of this

These are now assertions in the pipeline rather than things to remember:

- A test run's verdict is `exit_code` **and** collected count **and** a
  per-test outcome fingerprint — never the exit code alone.
- Determinism compares fingerprints over sorted `(node_id, outcome)` pairs, so
  "passed twice" cannot be mistaken for "produced the same result twice".
- Every graph edge is re-verified by re-reading the source line, using a code
  path that does not share logic with the one that produced it.
- Task validation separates `imports` / `collects` / `fail_before` so a broken
  task cannot masquerade as a hard one.
