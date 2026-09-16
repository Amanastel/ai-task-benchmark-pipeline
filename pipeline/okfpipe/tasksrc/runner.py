"""Running a candidate tree under the pinned image, and reading the verdict.

Shared by the miner (which needs to know whether a candidate discriminates) and
the validation harness (which needs to prove it, repeatedly). Keeping one
implementation means the evidence a task ships with was produced by the same
code path that selected it.

The central idea is the **outcome map**: ``{node_id: outcome}`` parsed from
JUnit XML, plus a classification of *why* anything failed. "The verifier fails
against input/" is not enough -- a failure caused by an import error is a
broken task, not a red test, so failures are labelled at the source.
"""

from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from .. import util
from ..hygiene import dockerenv

# Failure text that means the task is broken rather than legitimately red.
_STRUCTURAL = re.compile(
    r"\b(ModuleNotFoundError|ImportError|SyntaxError|IndentationError|"
    r"CollectionError|fixture '\w+' not found|file or directory not found|"
    r"AttributeError: module|ERROR collecting)\b")

# Failure text that means a behavioural expectation was not met.
_BEHAVIOURAL = re.compile(
    r"\b(AssertionError|assert\s|Failed: DID NOT RAISE|"
    r"DID NOT RAISE|E\s+assert|pytest\.fail|"
    r"Failed: Timeout|ValueError|TypeError|KeyError|IndexError)\b")


@dataclass
class Outcome:
    exit_code: int
    outcomes: dict[str, str] = field(default_factory=dict)   # node id -> status
    messages: dict[str, str] = field(default_factory=dict)   # node id -> failure text
    collected: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    log: str = ""
    duration_s: float = 0.0

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and self.collected > 0 and not self.failed \
            and not self.errors

    @property
    def fingerprint(self) -> str:
        rows = sorted(f"{k}={v}" for k, v in self.outcomes.items())
        return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()

    def failing(self) -> list[str]:
        return sorted(k for k, v in self.outcomes.items() if v in ("failed", "error"))

    def passing(self) -> list[str]:
        return sorted(k for k, v in self.outcomes.items() if v == "passed")

    def failure_kind(self) -> str:
        """behavioural | structural | none -- why the run is red."""
        blob = "\n".join(self.messages.values()) + "\n" + self.log
        if not self.failing():
            return "none"
        if any(v == "error" for v in self.outcomes.values()):
            return "structural"
        if _STRUCTURAL.search(blob):
            return "structural"
        if _BEHAVIOURAL.search(blob):
            return "behavioural"
        return "unclassified"

    def to_json(self) -> dict:
        return {
            "exit_code": self.exit_code, "collected": self.collected,
            "passed": self.passed, "failed": self.failed,
            "errors": self.errors, "skipped": self.skipped,
            "failure_kind": self.failure_kind(),
            "fingerprint": self.fingerprint,
            "duration_s": self.duration_s,
            "failing_tests": self.failing(),
            "passing_tests": self.passing(),
        }


def parse_junit(path: Path) -> tuple[dict, dict, dict]:
    """(outcomes, messages, counts) keyed by ``file::class::name`` node ids."""
    outcomes: dict[str, str] = {}
    messages: dict[str, str] = {}
    counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0, "collected": 0}
    if not path.exists():
        return outcomes, messages, counts
    try:
        tree = ET.parse(path)
    except ET.ParseError:
        return outcomes, messages, counts

    for case in tree.iter("testcase"):
        cls = case.get("classname", "") or ""
        name = case.get("name", "") or ""
        node = f"{cls}::{name}" if cls else name
        status = "passed"
        detail = ""
        fail = case.find("failure")
        err = case.find("error")
        skip = case.find("skipped")
        if fail is not None:
            status = "failed"
            detail = (fail.get("message") or "") + "\n" + (fail.text or "")
        elif err is not None:
            status = "error"
            detail = (err.get("message") or "") + "\n" + (err.text or "")
        elif skip is not None:
            status = "skipped"
        outcomes[node] = status
        if detail.strip():
            messages[node] = detail.strip()[:4000]
        counts["collected"] += 1
        counts["errors" if status == "error" else status] += 1
    return outcomes, messages, counts


def materialise(base: Path, dest: Path, overrides: dict[str, str] | None = None,
                overlay_dir: Path | None = None) -> None:
    """Copy ``base`` to ``dest``, then apply file overrides and an overlay tree."""
    util.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    util.copytree(base, dest, keep_git=False)
    for rel, content in (overrides or {}).items():
        util.write_text(dest / rel, content)
    if overlay_dir and overlay_dir.exists():
        for src in sorted(overlay_dir.rglob("*")):
            if src.is_file():
                rel = src.relative_to(overlay_dir)
                util.write_text(dest / rel, src.read_text(encoding="utf-8",
                                                          errors="replace"))


def run_pytest(image: str, tree: Path, workdir: Path, selection: list[str],
               label: str = "run", timeout: int = 900,
               extra: list[str] | None = None) -> Outcome:
    """Run a selection of tests against ``tree`` inside the pinned image."""
    outdir = workdir / f"junit-{util.slugify(label)}"
    util.rmtree(outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    targets = " ".join(f"'{s}'" for s in selection) if selection else ""
    cmd = (f"python -m pytest {targets} -p no:cacheprovider -q "
           f"--timeout=180 --junitxml=/out/junit.xml "
           f"{' '.join(extra or [])}")
    res = dockerenv.run_in(
        image, ["sh", "-c", cmd],
        mounts=[dockerenv.Mount(tree, "/app"), dockerenv.Mount(outdir, "/out")],
        workdir="/app", network="none", timeout=timeout,
        # The tree under test is mounted over /app, so the editable install
        # baked into the image resolves imports to this tree, not to the
        # version the image was built from.
        env={"PYTHONPATH": "/app"})

    outcomes, messages, counts = parse_junit(outdir / "junit.xml")
    return Outcome(
        exit_code=res.returncode, outcomes=outcomes, messages=messages,
        collected=counts["collected"], passed=counts["passed"],
        failed=counts["failed"], errors=counts["errors"],
        skipped=counts["skipped"], duration_s=round(res.duration_s, 2),
        log=util.truncate(res.output, 2500, 4000),
    )


def import_check(image: str, tree: Path, packages: list[str]) -> tuple[bool, str]:
    """Confirm the tree still imports. A task whose input does not import is broken."""
    if not packages:
        return True, "no package to import"
    stmt = "; ".join(f"import {p}" for p in packages)
    res = dockerenv.run_in(
        image, ["python", "-c", stmt],
        mounts=[dockerenv.Mount(tree, "/app", "ro")],
        workdir="/app", network="none", timeout=300,
        env={"PYTHONPATH": "/app"})
    return res.ok, util.truncate(res.output, 500, 1500)


def collect_check(image: str, tree: Path, workdir: Path,
                  selection: list[str]) -> tuple[bool, str]:
    """Confirm pytest can collect the verifier. Separates 'red' from 'broken'."""
    targets = " ".join(f"'{s}'" for s in selection) if selection else ""
    res = dockerenv.run_in(
        image, ["sh", "-c", f"python -m pytest {targets} --collect-only -q "
                            "-p no:cacheprovider"],
        mounts=[dockerenv.Mount(tree, "/app", "ro")],
        workdir="/app", network="none", timeout=600,
        env={"PYTHONPATH": "/app"})
    return res.ok, util.truncate(res.output, 500, 2500)


def context_to_pytest_id(context: str) -> str:
    """``glom.test.test_basic.test_x`` -> ``glom/test/test_basic.py::test_x``.

    Coverage's ``dynamic_context = test_function`` records a fully dotted path
    with no ``::`` separator, and may append a phase suffix such as ``|setup``.
    That is a different shape from the JUnit ``classname::name`` handled by
    ``node_to_pytest_id``, and confusing the two yields ids pytest silently
    collects nothing for.
    """
    ctx = context.split("|")[0].strip()
    parts = [p for p in ctx.split(".") if p]
    if len(parts) < 2:
        return ""
    func = parts[-1]
    rest = parts[:-1]
    # Trailing capitalised segments are test classes, not package components.
    classes: list[str] = []
    while len(rest) > 1 and rest[-1][:1].isupper():
        classes.insert(0, rest.pop())
    if not rest:
        return ""
    return "::".join(["/".join(rest) + ".py", *classes, func])


def to_pytest_ids(node_ids: list[str]) -> list[str]:
    """Convert JUnit node ids to ids pytest will actually collect.

    JUnit reports ``glom.test.test_core::test_x``; pytest needs
    ``glom/test/test_core.py::test_x``. Writing the JUnit form into a verifier
    selection makes pytest collect nothing and exit 4, which looks exactly like
    a task whose verifier is broken.
    """
    out = [node_to_pytest_id(n) for n in node_ids]
    return sorted({n for n in out if n})


def node_to_path(node_id: str) -> str:
    """``glom.test.test_core::test_x`` -> ``glom/test/test_core.py``."""
    cls = node_id.split("::")[0]
    return cls.replace(".", "/") + ".py"


def node_to_pytest_id(node_id: str) -> str:
    """JUnit classname::name -> a pytest node id pytest will accept."""
    parts = node_id.split("::")
    if len(parts) < 2:
        return node_id
    dotted, name = parts[0], parts[-1]
    segments = dotted.split(".")
    # Trailing capitalised segments are test classes, not package parts.
    file_parts: list[str] = []
    class_parts: list[str] = []
    for seg in segments:
        if class_parts or (seg[:1].isupper() and file_parts):
            class_parts.append(seg)
        else:
            file_parts.append(seg)
    path = "/".join(file_parts) + ".py"
    return "::".join([path, *class_parts, name])
