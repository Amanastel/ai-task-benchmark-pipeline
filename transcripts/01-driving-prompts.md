# Driving prompts

The prompts that shaped the build, in order, with the intent behind each. Prose
is condensed; the substance and sequence are as they happened.

---

### 1. Framing — establish the acceptance bar before writing code

> Read the assignment PDF. The target repo is `https://github.com/mahmoud/glom`.
> Before writing anything, tell me what the repo actually lacks, and what the
> hardest acceptance criterion is.

**Intent:** stop the agent from generating a plausible pipeline against an
imagined repo. The answer — glom has tests and a stale py3.7-compiled
`requirements.txt`, but no runtime pinning, no Dockerfile and no lint config —
changed the plan: this is not a "repo with no tests", so the test generator has
to earn its place rather than fill a vacuum.

---

### 2. Constrain the architecture before implementation

> Everything that resolves dependencies, runs tests, or validates a task must
> run inside a container, not on this Windows host. No module except `detect.py`
> may contain a repo-specific string. Show me the module layout before you write
> any of it.

**Intent:** the two properties being graded — held-out generality and
determinism — are architectural, not something to retrofit. Forcing the
container boundary and the single-detection-point rule up front is what made
the held-out repo run work later without edits.

---

### 3. Probe the riskiest assumption first

> Don't write the pinning module yet. Run one throwaway container that tries
> `uv pip compile` against glom's `setup.py` with its extras, and one that runs
> glom's suite on python 3.11 under whatever pytest resolves. If either fails,
> the design changes.

**Intent:** both are cheap to test and expensive to discover late. Both passed
(pytest 9.1.1, 202 tests green), which locked in the base image and the
acceptance bar.

---

### 4. Force the test generator to be honest about what it produces

> These generated tests pin current behaviour; they do not validate it. Say that
> in the generated file header and in REPORT.md. Don't describe characterisation
> tests as if they prove correctness.

**Intent:** the rubric wants tests that "assert real observable behavior", and
the temptation is to present auto-generated snapshots as correctness proofs.
They detect *change*, including injected bugs, which is a real and useful
property — and a different one. Stating the limit is worth more than overclaiming.

---

### 5. Make the bug-catching claim falsifiable

> Don't claim the generated tests catch bugs. Inject the bugs and measure it.
> Build a mutation harness: single-edit AST mutants, run each against the
> existing suite and the generated suite separately, and report survivors
> individually.

**Intent:** turns an assertion into a number a grader can re-derive. The
per-suite split is what makes the number interpretable — it answers "what did
the generated tests add?" rather than just "how good is the suite?".

---

### 6. Correct the mutation targeting after reading the survivors

> Your survivor list is version-string constants, `__name__ == "__main__"`
> guards, and CLI help text. Those are unkillable by construction and they are
> eating the budget. Restrict mutants to statements inside function bodies, on
> lines the suite actually executes, and skip display-string keyword arguments.

**Intent:** a 60% mutation score built mostly from unkillable mutants is a
meaningless 60%. See `03-design-decisions.md`.

---

### 7. Demand that the verifier prove *why* it is red

> "Fails before" is not enough. The brief says a failure from an import error
> doesn't count. Add explicit gates: the input tree must import, pytest must
> collect the verifier successfully, and only then does a failure count — and
> classify the failure as behavioural or structural from the actual output.

**Intent:** this is the single most-cited validation requirement in the brief,
and the easy implementation (`exit != 0`) satisfies none of it.

---

### 8. Refuse to pad the task set

> If fewer than ten tasks validate, deliver fewer and list every rejected
> candidate with its reason. Do not relax a gate to reach ten.

**Intent:** the brief says explicitly that a smaller validated set beats ten
unvalidated ones. Encoding that as pipeline behaviour — rather than as an
intention — is what makes it true under time pressure.

---

### 9. Verify against an unseen repo

> Run the whole pipeline against a Python repo I have not mentioned and you
> have not special-cased. Report what broke, and fix it in `detect.py` only.

**Intent:** the held-out run is a graded dimension; the only way to know is to
do it. The "fix it in detect.py only" clause is the test of whether the
architecture rule from prompt 2 actually held.
