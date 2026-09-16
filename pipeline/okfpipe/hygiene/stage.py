"""Stage 1 orchestration.

Order matters here, and each step exists to protect the one after it:

    detect -> pin -> containerize -> build -> BASELINE TEST RUN
           -> lint/format (rollback on regression)
           -> coverage -> generate tests
           -> rebuild -> DETERMINISM CHECK (twice, compared)
           -> mutation sweep

The baseline run is the gate: if the untouched repo's suite does not pass in
the container, nothing downstream is trustworthy and we say so rather than
generating tests against a broken environment.

Formatting precedes generation because generated assertions describe the source
as delivered -- reformatting afterwards would invalidate the expectations we
just measured. It sits behind a snapshot so a formatter that breaks a test gets
rolled back instead of shipped.

The determinism check runs the *final image* twice and compares per-test
outcomes, not just exit codes: "passed twice" and "produced the same result
twice" are different claims, and only the second one is being asked for.
"""

from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from .. import util
from . import container, deps, dockerenv, lint, mutate, testgen
from .detect import detect


@dataclass
class TestRun:
    ok: bool
    exit_code: int
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: int = 0
    total: int = 0
    fingerprint: str = ""
    duration_s: float = 0.0
    log: str = ""

    def summary(self) -> str:
        return (f"exit={self.exit_code} total={self.total} passed={self.passed} "
                f"failed={self.failed} errors={self.errors} skipped={self.skipped} "
                f"fingerprint={self.fingerprint[:16]}")


@dataclass
class HygieneResult:
    ok: bool
    profile: dict = field(default_factory=dict)
    base_image: str = ""
    image_tag: str = ""
    pin: dict = field(default_factory=dict)
    container: dict = field(default_factory=dict)
    tests: dict = field(default_factory=dict)
    lint: dict = field(default_factory=dict)
    mutation: dict = field(default_factory=dict)
    runs: list[dict] = field(default_factory=list)
    deterministic: bool = False
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# test execution + fingerprinting
# --------------------------------------------------------------------------

def _parse_junit(path: Path) -> tuple[dict, str]:
    """Counts plus a fingerprint over sorted (test id, outcome) pairs."""
    if not path.exists():
        return {}, ""
    try:
        tree = ET.parse(path)
    except ET.ParseError:
        return {}, ""
    rows: list[str] = []
    counts = {"passed": 0, "failed": 0, "skipped": 0, "errors": 0, "total": 0}
    for case in tree.iter("testcase"):
        name = f'{case.get("classname", "")}::{case.get("name", "")}'
        outcome = "passed"
        if case.find("failure") is not None:
            outcome = "failed"
        elif case.find("error") is not None:
            outcome = "error"
        elif case.find("skipped") is not None:
            outcome = "skipped"
        counts["total"] += 1
        counts["errors" if outcome == "error" else outcome] += 1
        rows.append(f"{name}={outcome}")
    fingerprint = hashlib.sha256("\n".join(sorted(rows)).encode("utf-8")).hexdigest()
    return counts, fingerprint


def run_tests(repo: Path, image: str, workdir: Path, label: str,
              extra_args: list[str] | None = None) -> TestRun:
    """Run the repo's documented test entry point inside the built image.

    Deliberately mounts *only* the output directory. The source under test is
    the copy baked into the image, which is what a grader running
    ``docker compose run --rm tests`` will execute -- mounting the host tree
    over ``/app`` would test something the acceptance bar never runs.

    The command must not be piped: a pipeline's exit status is the last
    command's, so ``pytest | tail`` reports success no matter what pytest did.
    """
    outdir = workdir / f"run-{label}"
    util.rmtree(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    args = " ".join(extra_args or [])
    res = dockerenv.run_in(
        image,
        ["sh", "-c", f"./run-tests.sh --junitxml=/out/junit.xml {args}"],
        mounts=[dockerenv.Mount(outdir, "/out")],
        workdir="/app", network="none", timeout=2400)
    counts, fingerprint = _parse_junit(outdir / "junit.xml")
    run = TestRun(
        ok=res.ok, exit_code=res.returncode,
        passed=counts.get("passed", 0), failed=counts.get("failed", 0),
        skipped=counts.get("skipped", 0), errors=counts.get("errors", 0),
        total=counts.get("total", 0), fingerprint=fingerprint,
        duration_s=round(res.duration_s, 2), log=util.truncate(res.output, 2000, 4000),
    )
    util.info(f"test run [{label}]", result=run.summary())
    return run


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def run(source_repo: Path, out_repo: Path, workdir: Path,
        do_format: bool = True, mutants: int = 60,
        skip_mutation: bool = False) -> HygieneResult:
    dockerenv.require_docker()
    workdir.mkdir(parents=True, exist_ok=True)
    result = HygieneResult(ok=False)

    # ---- detect ---------------------------------------------------------
    prof = detect(source_repo)
    result.profile = prof.to_dict()
    if not prof.packages:
        result.failures.append("no importable package detected; cannot proceed")
        return result

    base_image = dockerenv.resolve_digest(f"python:{prof.python_version}-slim")
    result.base_image = base_image

    # ---- materialise the output tree ------------------------------------
    util.rmtree(out_repo)
    out_repo.mkdir(parents=True, exist_ok=True)
    util.copytree(source_repo, out_repo, keep_git=False)
    out_prof = detect(out_repo)

    # ---- pin ------------------------------------------------------------
    with util.Stopwatch("pin dependencies"):
        pin = deps.pin(out_prof, out_repo, base_image, workdir / "pin")
    result.pin = {
        "ok": pin.ok, "source": pin.source, "packages": len(pin.packages),
        "hashed": pin.hashed, "extras": pin.extras_used, "notes": pin.notes,
        "lock": pin.lock_path, "runtime_lock": pin.runtime_lock_path,
        "resolved": pin.packages,
    }
    if not pin.ok:
        result.failures.append("dependency resolution failed: " + "; ".join(pin.notes))
        util.write_text(workdir / "pin.log", pin.log)
        return result

    # ---- containerize ---------------------------------------------------
    cont = container.containerize(
        out_prof, out_repo, base_image, pin.lock_path, pin.hashed,
        pin.packages, out_prof.build_requires,
        generated_tests_dir=testgen.GENERATED_DIR)
    result.container = {
        "image": cont.image_tag, "services": cont.services,
        "test_command": cont.test_command, "notes": cont.notes,
    }
    result.image_tag = cont.image_tag

    build = dockerenv.build_image(out_repo, cont.image_tag)
    util.write_text(workdir / "docker-build.log", build.output)
    if not build.ok:
        result.failures.append("docker build failed; see docker-build.log")
        return result
    util.info("image built", tag=cont.image_tag)

    # ---- baseline test run (the gate) -----------------------------------
    baseline = run_tests(out_repo, cont.image_tag, workdir, "baseline")
    result.runs.append({"label": "baseline", **_run_json(baseline)})
    if not baseline.ok:
        result.failures.append(
            "the repository's own test suite does not pass in the container; "
            "refusing to generate tests against a broken baseline")
        util.write_text(workdir / "baseline-tests.log", baseline.log)
        return result

    # ---- lint + format, behind a rollback snapshot ----------------------
    # Formatting runs *before* test generation: generated assertions describe
    # the source as delivered, so reformatting afterwards would invalidate the
    # very expectations we just measured.
    snapshot = workdir / "pre-lint"
    util.rmtree(snapshot)
    util.copytree(out_repo, snapshot, keep_git=False)

    # A ladder, least invasive last. ruff's "safe" fixes are ruff's judgement,
    # not a proof: on the held-out repo one rewrote an import inside a
    # compatibility shim and broke a test. So each rung is applied, built and
    # *tested*, and we keep the first one that does not lose a passing test.
    # The final rung touches no source at all, so it cannot fail.
    ladder = [("fix+format", True, do_format), ("fix-only", True, False),
              ("config-only", False, False)]
    if not do_format:
        ladder = [r for r in ladder if r[0] != "fix+format"]

    lint_res = None
    post_lint = None
    for i, (mode, want_fix, want_format) in enumerate(ladder):
        if i:
            util.rmtree(out_repo)
            util.copytree(snapshot, out_repo, keep_git=False)
        lint_res = lint.apply(out_prof, out_repo, cont.image_tag,
                              do_format=want_format, do_fix=want_fix)
        lint_res.mode = mode
        util.write_text(workdir / f"lint-{mode}.log", lint_res.log)

        rebuild = dockerenv.build_image(out_repo, cont.image_tag)
        post_lint = run_tests(out_repo, cont.image_tag, workdir, f"post-lint-{mode}") \
            if rebuild.ok else TestRun(ok=False, exit_code=1, log=rebuild.output)
        if post_lint.ok and post_lint.passed >= baseline.passed:
            if i:
                lint_res.format_reverted = True
                lint_res.notes.append(
                    f"fell back to '{mode}': the more aggressive pass lost a "
                    f"passing test ({baseline.passed} -> {post_lint.passed}), so "
                    "the repo's behaviour was preserved over its tidiness")
            break
        util.warn("lint pass regressed the suite; trying a less invasive one",
                  mode=mode, before=baseline.passed, after=post_lint.passed)
        result.notes.append(
            f"lint mode '{mode}' changed the test outcome "
            f"({baseline.passed} passing -> {post_lint.passed}); not used")

    dockerenv.build_image(out_repo, cont.image_tag).check("rebuild after lint")
    result.lint = {
        "ok": bool(lint_res and lint_res.ok), "config": lint_res.config_path,
        "mode": lint_res.mode,
        "formatted_files": lint_res.formatted_files,
        "fixed": lint_res.fixed_violations,
        "baselined_rules": lint_res.baselined,
        "remaining": lint_res.remaining,
        "fell_back": lint_res.format_reverted,
        "notes": lint_res.notes,
    }
    util.rmtree(snapshot)

    # ---- coverage + test generation -------------------------------------
    with util.Stopwatch("measure coverage"):
        coverage = testgen.measure_coverage(out_prof, out_repo, cont.image_tag,
                                            workdir / "coverage")
    with util.Stopwatch("generate tests"):
        gen = testgen.generate(out_prof, out_repo, cont.image_tag,
                               workdir / "testgen", coverage)
    result.tests = {
        "ok": gen.ok, "files": gen.files, "cases": gen.cases,
        "doctest_cases": gen.doctest_cases, "value_cases": gen.value_cases,
        "exception_cases": gen.exception_cases, "dropped": gen.dropped,
        "notes": gen.notes,
    }
    util.write_text(workdir / "testgen.log", gen.log)

    # Generated files are new source; lint them too so the repo stays clean.
    # Formatting stays off if it was rolled back above -- re-running it here
    # would silently reapply the very pass that broke the suite, and the second
    # application happens after the comparison that would have caught it.
    # Reuse the rung that survived: re-running a more aggressive mode here would
    # reapply the very pass the ladder rejected, after the comparison that
    # caught it.
    if gen.files:
        kept_mode = result.lint.get("mode", "config-only")
        again = lint.apply(out_prof, out_repo, cont.image_tag,
                           do_format=kept_mode == "fix+format",
                           do_fix=kept_mode != "config-only")
        result.lint["remaining"] = again.remaining
        result.lint["baselined_rules"] = again.baselined

    # ---- determinism: run the final tree twice and compare --------------
    dockerenv.build_image(out_repo, cont.image_tag).check("rebuild final image")
    first = run_tests(out_repo, cont.image_tag, workdir, "final-1")
    second = run_tests(out_repo, cont.image_tag, workdir, "final-2")
    result.runs += [{"label": "final-1", **_run_json(first)},
                    {"label": "final-2", **_run_json(second)}]
    result.deterministic = (
        first.ok and second.ok
        and first.fingerprint == second.fingerprint
        and (first.passed, first.failed, first.errors)
        == (second.passed, second.failed, second.errors)
    )
    if not first.ok or not second.ok:
        result.failures.append("final test run did not pass in the container")
    elif not result.deterministic:
        result.failures.append(
            "two identical runs produced different results; "
            f"{first.fingerprint[:12]} vs {second.fingerprint[:12]}")

    nondet = container.scan_nondeterminism(out_prof)
    if nondet:
        result.notes.append(
            f"{len(nondet)} test line(s) use clock/random/network APIs that can "
            "make a verdict wobble; reported, not rewritten")

    # ---- mutation sweep -------------------------------------------------
    if not skip_mutation and gen.ok:
        suites: dict[str, list[str]] = {}
        if out_prof.test_paths:
            suites["existing"] = list(out_prof.test_paths)
        if gen.files:
            suites["generated"] = [testgen.GENERATED_DIR]
        if suites:
            # Scale the per-mutant timeout to how long the suite actually takes.
            # A fixed ceiling is wrong in both directions: too tight for a slow
            # suite, and ruinous for a fast one, because a mutant that inverts a
            # parser's loop bound hangs and then burns the whole ceiling. On the
            # held-out repo a 300s constant turned a 6-minute sweep into hours.
            per_mutant = max(60, int((first.duration_s or 10) * 5))
            util.info("mutation timeout chosen", seconds=per_mutant,
                      suite_duration=f"{first.duration_s:.1f}s")
            with util.Stopwatch("mutation sweep"):
                mut = mutate.run(out_prof, out_repo, cont.image_tag,
                                 workdir / "mutate", suites, limit=mutants,
                                 coverage=coverage, timeout=per_mutant)
            result.mutation = mut.to_json()
            util.write_text(workdir / "mutation.log", mut.log)

    tidy(out_repo)
    result.ok = not result.failures
    result.notes.append(f"nondeterminism scan: {len(nondet)} flagged line(s)")
    return result


# Caches and coverage data the in-container steps write into the tree. They are
# created *after* the tree is copied, so the copy's ignore list never sees them,
# and shipping them would mean the delivered repo contains a `.coverage` file
# describing a run the recipient did not make.
_TIDY_GLOBS = ("__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache",
               "htmlcov", "*.egg-info", ".coverage", ".coverage.*", "*.pyc")


def tidy(repo: Path) -> int:
    removed = 0
    for pattern in _TIDY_GLOBS:
        for path in sorted(repo.rglob(pattern)):
            if path.is_dir():
                util.rmtree(path)
            else:
                try:
                    path.unlink()
                except OSError:
                    continue
            removed += 1
    if removed:
        util.debug("tidied build artefacts from the output tree", count=removed)
    return removed


def _run_json(run: TestRun) -> dict:
    return {
        "exit_code": run.exit_code, "passed": run.passed, "failed": run.failed,
        "errors": run.errors, "skipped": run.skipped, "total": run.total,
        "fingerprint": run.fingerprint, "duration_s": run.duration_s,
    }
