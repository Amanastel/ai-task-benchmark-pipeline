# Transcripts — how this was built with an agent

The brief asks for the key prompts and session logs, and says AI-tool leverage is
scored on "effective agent use **with verification** — not blind acceptance of
agent output." So this directory is organised around that second half.

| File | What it contains |
|---|---|
| `01-driving-prompts.md` | The prompts that actually steered the build, with what each one was trying to achieve |
| `02-verification-log.md` | Every place the agent's first answer was wrong, how it was caught, and what the fix was |
| `03-design-decisions.md` | Decisions where I overrode or redirected the agent, and why |
| `results.py` | Reads every number quoted in `REPORT.md` back out of the delivered artifacts: `python transcripts/results.py . glom` |

## The working method

The build ran as a tight loop rather than a single large generation:

1. **Probe before building.** Before writing the pinning module, one throwaway
   container run confirmed `uv pip compile` could resolve glom's `setup.py`
   with extras. Before committing to the acceptance bar, another confirmed
   glom's suite passes on Python 3.11 under a modern pytest. Two minutes of
   probing removed the two largest unknowns.
2. **Run the stage, read the numbers, distrust the green.** Stage 1's first
   "successful" run reported `total=0 passed=0 exit=0`. It was green because
   `pytest | tail -80` returns `tail`'s exit code. The gate was not gating.
   That single bug is the clearest argument for the rule below.
3. **Never accept a metric without reproducing the mechanism.** Every headline
   number in `REPORT.md` — mutation score, edge-verification rate, task
   validation — is produced by code that a grader can re-run, and each was
   spot-checked by hand at least once against the underlying artifact.

## What the agent was good and bad at

**Good:** scaffolding modules with consistent structure, AST manipulation
(the mutation operators and the excision transformer were close to right first
time), and grinding through Docker/pytest plumbing.

**Bad, and caught only by running it:**

- Shell exit-code semantics through a pipe (see above).
- Two different test-identifier formats — coverage's dynamic contexts emit
  `glom.test.test_basic.test_x` while JUnit emits `glom.test.test_basic::test_x`
  and pytest wants `glom/test/test_basic.py::test_x`. Conflating them produced
  verifiers that silently collected nothing, which looks identical to a broken
  task. Three of the bugs in `02-verification-log.md` are this one mistake.
- `git log --format=... --numstat` record framing: putting the record separator
  at the *end* meant every commit's numstat block landed in the next chunk. The
  symptom was a plausible-looking 104 commits instead of 1050 — plausible enough
  that it would have shipped if the count had not been checked against
  `git rev-list --count`.

The pattern in all three: the failure mode was *quiet and plausible*, not loud.
That is the argument for making the pipeline assert its own invariants — the
baseline-run gate, the failure-kind classifier, the edge re-verification pass —
rather than trusting that a step worked because it exited zero.
