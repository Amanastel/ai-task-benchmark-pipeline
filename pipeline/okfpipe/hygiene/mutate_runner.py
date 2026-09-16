"""In-container mutation driver.

Copied into the container and run there. It applies one mutant at a time,
runs the chosen test selection, records whether the mutant was killed, and
always restores the original file -- including on crash, so a failed run cannot
leave a mutated source tree behind.

    python mutate_runner.py <mutants.json> <plan.json> <results.json>

``plan.json``::

    {"suites": {"existing": ["glom/test"], "generated": ["tests_generated"]},
     "timeout": 300, "pytest_args": ["-x", "-q", "-p", "no:cacheprovider"]}

A mutant is "killed" by a suite when that suite exits non-zero on the mutated
tree. A mutant that no suite kills is a real gap: some behaviour change is
invisible to the tests.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path


def apply_mutant(root: Path, mutant: dict) -> str:
    """Overwrite the target file with the mutated source. Returns the original."""
    path = root / mutant["file"]
    original = path.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)
    start = mutant["start_line"] - 1
    end = mutant["end_line"]
    replacement = mutant["replacement"]
    if not replacement.endswith("\n"):
        replacement += "\n"
    mutated = "".join(lines[:start]) + replacement + "".join(lines[end:])
    path.write_text(mutated, encoding="utf-8", newline="\n")
    return original


def run_suite(root: Path, paths: list[str], args: list[str], timeout: int) -> dict:
    started = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", *paths, *args],
            cwd=str(root), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
        code, out = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        # A mutant that hangs (say, an inverted loop bound) counts as killed:
        # the suite did detect it, just by not terminating.
        code, out = 124, "TIMEOUT"
    return {"exit": code, "killed": code != 0,
            "seconds": round(time.time() - started, 2),
            "tail": out[-600:]}


def main(argv: list[str]) -> int:
    mutants = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    plan = json.loads(Path(argv[2]).read_text(encoding="utf-8"))
    out_path = Path(argv[3])
    root = Path(plan.get("root", "/app"))
    args = plan.get("pytest_args", ["-x", "-q", "-p", "no:cacheprovider"])
    timeout = int(plan.get("timeout", 300))
    suites: dict[str, list[str]] = plan["suites"]

    results = []
    for i, mutant in enumerate(mutants, 1):
        path = root / mutant["file"]
        if not path.exists():
            results.append({"id": mutant["id"], "status": "skipped",
                            "detail": "file missing"})
            continue
        original = None
        try:
            original = apply_mutant(root, mutant)
            # A mutant that does not even import is not a behaviour test; it
            # would be "killed" by every suite for the wrong reason.
            check = subprocess.run(
                [sys.executable, "-c",
                 f"import ast,pathlib;ast.parse(pathlib.Path(r'{path}')"
                 ".read_text(encoding='utf-8'))"],
                cwd=str(root), capture_output=True, text=True, timeout=60)
            if check.returncode != 0:
                results.append({"id": mutant["id"], "status": "invalid",
                                "detail": "mutant does not parse"})
                continue
            per_suite = {name: run_suite(root, paths, args, timeout)
                         for name, paths in suites.items()}
            results.append({
                "id": mutant["id"], "status": "run",
                "file": mutant["file"], "operator": mutant["operator"],
                "line": mutant["start_line"], "target": mutant.get("target", ""),
                "before": mutant.get("before", "")[:200],
                "after": mutant.get("after", "")[:200],
                "suites": per_suite,
                "killed_by": sorted(n for n, r in per_suite.items() if r["killed"]),
            })
        except Exception as exc:  # noqa: BLE001 - report, never abort the sweep
            results.append({"id": mutant["id"], "status": "error",
                            "detail": f"{type(exc).__name__}: {exc}"[:300]})
        finally:
            if original is not None:
                path.write_text(original, encoding="utf-8", newline="\n")
        print(f"[{i}/{len(mutants)}] {mutant['id']}", flush=True)

    out_path.write_text(json.dumps(results, indent=1, sort_keys=True),
                        encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
