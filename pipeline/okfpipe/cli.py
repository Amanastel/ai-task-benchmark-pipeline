"""Command line entry point.

    python -m okfpipe <repo_url_or_path> [options]

Stages are independently runnable so a failure in task generation does not
force a 20-minute re-resolve of dependencies, and so a grader can inspect one
stage's output without running the others.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import SCHEMA_VERSION, __version__, repo as repo_mod, util

STAGES = ("hygiene", "knowledge", "tasks")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="okfpipe",
        description="Repo hygiene, knowledge layer and benchmark task generation "
                    "for an arbitrary Python repository.")
    p.add_argument("source", help="git URL or local path to the target repository")
    p.add_argument("--out", default="output",
                   help="directory for the transformed repo and artifacts "
                        "(default: output)")
    p.add_argument("--tasks-out", default="tasks",
                   help="directory for generated benchmark tasks (default: tasks)")
    p.add_argument("--work", default=".okfwork",
                   help="scratch directory for logs and intermediates")
    p.add_argument("--stages", default="all",
                   help="comma-separated subset of: " + ", ".join(STAGES)
                        + " (default: all)")
    p.add_argument("--task-count", type=int, default=10,
                   help="number of validated tasks to deliver (default: 10)")
    p.add_argument("--mutants", type=int, default=60,
                   help="mutation budget for the injected-bug sweep (default: 60)")
    p.add_argument("--validation-repeats", type=int, default=3,
                   help="times each task verifier is re-run to prove determinism")
    p.add_argument("--no-format", action="store_true",
                   help="apply the linter but not the formatter")
    p.add_argument("--skip-mutation", action="store_true",
                   help="skip the injected-bug sweep (it is the slowest step)")
    p.add_argument("--keep-work", action="store_true",
                   help="keep the scratch directory after a successful run")
    p.add_argument("--version", action="version",
                   version=f"okfpipe {__version__} (schema {SCHEMA_VERSION})")
    return p


def _selected(arg: str) -> list[str]:
    if arg.strip() in ("all", "*"):
        return list(STAGES)
    picked = [s.strip() for s in arg.split(",") if s.strip()]
    bad = [s for s in picked if s not in STAGES]
    if bad:
        raise SystemExit(f"unknown stage(s): {', '.join(bad)}. "
                         f"choose from {', '.join(STAGES)}")
    return [s for s in STAGES if s in picked]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    stages = _selected(args.stages)
    started = time.time()

    out_root = Path(args.out).resolve()
    work = Path(args.work).resolve()
    tasks_out = Path(args.tasks_out).resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)

    util.info("okfpipe starting", version=__version__, stages=",".join(stages),
              source=args.source)

    # The pristine clone stays untouched; everything else works off copies.
    handle = repo_mod.acquire(args.source, work / "source")
    out_repo = out_root / util.slugify(repo_mod.project_name(args.source))

    summary: dict = {
        "okfpipe_version": __version__,
        "schema": SCHEMA_VERSION,
        "source": args.source,
        "head_sha": handle.head_sha,
        "stages_run": stages,
        "output_repo": str(out_repo.relative_to(Path.cwd())
                           if out_repo.is_relative_to(Path.cwd()) else out_repo),
    }
    failures: list[str] = []

    # ---- stage 1 --------------------------------------------------------
    if "hygiene" in stages:
        from .hygiene import stage as hygiene_stage
        with util.Stopwatch("stage 1: hygiene"):
            hres = hygiene_stage.run(
                handle.path, out_repo, work / "hygiene",
                do_format=not args.no_format, mutants=args.mutants,
                skip_mutation=args.skip_mutation)
        okf = out_repo / ".okf"
        util.write_json(okf / "hygiene.json", {
            "ok": hres.ok, "base_image": hres.base_image,
            "image_tag": hres.image_tag, "pin": hres.pin,
            "container": hres.container, "tests": hres.tests,
            "lint": hres.lint, "runs": hres.runs,
            "deterministic": hres.deterministic,
            "failures": hres.failures, "notes": hres.notes,
        })
        util.write_json(okf / "profile.json", hres.profile)
        if hres.mutation:
            util.write_json(okf / "mutation.json", hres.mutation)
        summary["hygiene"] = {
            "ok": hres.ok, "deterministic": hres.deterministic,
            "image": hres.image_tag, "base_image": hres.base_image,
            "packages_pinned": hres.pin.get("packages", 0),
            "generated_tests": hres.tests.get("cases", 0),
            "mutation_score": hres.mutation.get("mutation_score"),
            "failures": hres.failures,
        }
        failures += hres.failures
        if hres.failures:
            util.error("stage 1 failed", detail="; ".join(hres.failures))

    # ---- stage 2 --------------------------------------------------------
    if "knowledge" in stages:
        from .knowledge import stage as knowledge_stage
        if not out_repo.exists():
            failures.append("knowledge stage needs stage 1 output; run --stages hygiene")
        else:
            with util.Stopwatch("stage 2: knowledge layer"):
                kres = knowledge_stage.run(out_repo, handle, out_root, work / "knowledge")
            summary["knowledge"] = kres
            failures += kres.get("failures", [])

    # ---- stage 3 --------------------------------------------------------
    if "tasks" in stages:
        from .tasksrc import stage as tasks_stage
        if not out_repo.exists():
            failures.append("task stage needs stage 1 output; run --stages hygiene")
        else:
            with util.Stopwatch("stage 3: task generation"):
                tres = tasks_stage.run(
                    out_repo, handle, tasks_out, work / "tasks",
                    target_count=args.task_count,
                    repeats=args.validation_repeats)
            summary["tasks"] = tres
            failures += tres.get("failures", [])

    summary["elapsed_s"] = round(time.time() - started, 1)
    summary["failures"] = failures
    summary["ok"] = not failures
    util.write_json(out_root / "run-summary.json", summary)

    print("", file=sys.stderr)
    util.info("okfpipe finished", ok=str(not failures),
              elapsed=f"{summary['elapsed_s']}s")
    for f in failures:
        util.error("FAILURE", detail=f)
    print(f"\nSummary written to {out_root / 'run-summary.json'}", file=sys.stderr)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
