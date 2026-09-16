# Design decisions where the agent was overruled

Places where the obvious or first-suggested implementation was rejected, and the
reasoning. These are the judgement calls; the code comments carry the short
version, this file carries the argument.

---

## Resolve dependencies inside a container, not on the host

**Rejected:** run `pip-compile` / `uv pip compile` on the developer's machine.

**Why.** This build ran on Windows with CPython 3.11. A lock resolved there
encodes that host's environment markers, wheel selection and interpreter
version. The Dockerfile then installs it on Linux, where a different set of
wheels and markers apply — so the lock is not a lock, it is a Windows-shaped
guess at one.

Resolving inside `python:3.11-slim` makes the platform a property of the
artifact instead of a property of whoever ran the pipeline. It costs one
container start per run and removes an entire class of "works on my machine".

---

## Pin the build backends, not just the dependencies

**Rejected:** `pip install -e .` with default build isolation.

**Why.** Build isolation fetches the build backend (setuptools, hatchling,
poetry-core) fresh at image-build time. That is an unpinned surface hiding
inside an image that is otherwise fully pinned — the lock, the base image digest
and the interpreter are all fixed, and then the thing that *builds the package*
floats.

The pipeline reads `[build-system] requires` (defaulting to the PEP 517 fallback
for legacy `setup.py` projects), adds it to the resolver's floor so it lands in
the lock, and then passes `--no-build-isolation`. Both halves are required: the
flag without the pins would be worse than the default.

---

## Baseline the linter rather than auto-fixing everything

**Rejected:** enable a broad rule set and let `--fix --unsafe-fixes` make it green.

**Why.** ruff labels a fix "unsafe" when it may change behaviour. Applying those
unattended to an unfamiliar repository is exactly the kind of silent change this
pipeline exists to prevent, and the acceptance bar would hide it — the tests
might still pass while semantics shifted.

Instead: safe fixes only, then whatever remains moves into an explicit
`extend-ignore` baseline with a per-rule occurrence count and a comment saying
these are pre-existing. The repo is genuinely lint-clean under a config that is
honest about what it postponed, and `.okf/hygiene.json` records the debt so
removing one entry is a well-defined unit of work.

The alternative dishonesty — selecting almost no rules so nothing fires — would
also be "lint-clean" and would mean nothing.

---

## Lint is applied as a tested ladder, not as a trusted operation

**Rejected:** treat ruff's "safe fixes" and `ruff format` as behaviour-preserving.

**Why.** Because on the held-out repo they were not. ruff's `UP017` rewrote
`return timezone.utc` into `return UTC` inside a `try:` block whose
`except ImportError:` branch was the only thing that defined `UTC`. ruff labels
that fix **safe**. It raises `UnboundLocalError`.

The first design took a snapshot, applied fix + format, and reverted the
*formatting* on regression — which reapplied the identical broken fix, after the
comparison that would have caught it. Both halves were wrong: trusting the
label, and reverting the wrong thing.

The replacement is a ladder — `fix+format`, `fix-only`, `config-only` — where
each rung is applied, the image rebuilt, and the suite **run**, keeping the
first rung that does not lose a passing test. The final rung changes no source,
so it always succeeds: a repo whose code cannot be safely modified still ends up
lint-clean, with every violation baselined and its behaviour untouched.

The general principle, and the reason this is in the decisions file rather than
the bug log: *the pipeline should verify its own claims rather than inherit them
from its tools.* "Safe fix" is the linter's opinion. Whether the suite still
passes is a fact, and it costs one build and one test run to have it.

The pipeline should never be the reason a repo's suite goes red, and it should
say so when it nearly was.

---

## Characterisation tests are labelled as such

**Rejected:** presenting generated snapshot assertions as correctness tests.

**Why.** A generated `assert f(-3) == 0` was produced by *running* `f(-3)` and
writing down the answer. It pins current behaviour. It will catch a regression,
an injected bug, or an accidental semantic change — genuinely useful, and
exactly what the mutation sweep measures. What it cannot do is tell you the
current behaviour is right; if `f` is already wrong, the test enshrines the bug.

That distinction is stated in the generated file's header, in the module
docstring and in `REPORT.md`. A reader who mistakes one for the other will trust
these tests for something they cannot do, and the cost of that is much higher
than the cost of saying so.

The doctest-materialisation half is different in kind and is worth more: those
expectations were written by a human, so they assert intent rather than
behaviour.

---

## Mutation targeting: covered function bodies only

**Rejected:** mutate every statement uniformly.

**Why.** See `02-verification-log.md` V4. Three exclusions, each for a distinct
reason:

- **Module level.** Runs at import, so a mutant there is caught by every test at
  once (uninformative) or by none (a constant nobody asserts on).
- **Uncovered lines.** Guaranteed to survive. That is a coverage fact already
  reported in `.okf/coverage.json`; counting it again as a mutation failure
  double-counts one problem and drowns out the real signal.
- **Display strings.** A test asserting on help text is testing the help text.

What is left answers a real question: *of the behaviours the tests actually
exercise, how many can be broken without anything going red?* The survivors are
listed individually, because a score without them cannot be checked.

---

## The verifier is restricted to tests that flip

**Rejected:** for a history-derived task, use the whole post-commit test file as
the verifier.

**Why.** A test file the commit touched is mostly tests that already passed. If
the verifier runs all of them, an agent that changes nothing scores nearly
green, and the pass/fail signal is dominated by behaviour the task is not about.

The miner runs the post-commit tests against both the parent and the commit tree
and keeps only the cases whose outcome flips red→green. Those cases *are* the
task. Everything else is covered by the separate no-collateral-breakage check,
which is where "don't break the rest" belongs.

---

## Net-new detectors decline rather than invent

**Rejected:** generating a plausible reference solution for any detected gap.

**Why.** A net-new task needs a reference solution that is *derived*, not
guessed, or the "golden answer" is just the pipeline's opinion.

The `__hash__` detector illustrates the line. The naive derivation — hash a
tuple of the attributes mentioned in `__eq__` — produces `hash((self.path_t,))`
for glom's `Path`, which hashes something different from what equality compares
and would be a subtly broken answer that still passes a "does not raise" test.
The implemented derivation mirrors `__eq__`'s actual comparison expression
(`self.path_t.__ops__`), which is the only derivation that guarantees the
invariant.

Where no derivation exists, the detector declines and the candidate is recorded
as rejected. Validation is the backstop: a derived solution that does not make
the authored tests pass fails `pass_after` and the task is dropped.

---

## Deliver fewer than ten rather than relax a gate

The brief says a smaller validated set beats ten unvalidated ones, so that is
encoded as behaviour: no gate is skipped to reach the target, every rejected
candidate is recorded with its reason in `tasks.json`, and the rejected
candidates' evidence is kept under `.okfwork/tasks/rejected/` so the selection
criteria can be audited rather than taken on trust.
