"""Stage 1e -- deliberate bug injection (mutation testing).

"We will evaluate whether your tests catch deliberately introduced bugs." So
rather than claim the generated tests are good, the pipeline injects the bugs
itself and reports the score.

Each mutant is a single-edit change to one statement -- a flipped comparison, a
swapped operator, a perturbed constant, a suppressed return. The suites are run
separately so the report can answer the question that actually matters: *how
many bugs does the repo catch that it would have missed without the generated
tests?*

Two honesty guards, both learned from mutation testing's usual failure modes:

* A mutant that does not parse is discarded, not counted as killed -- otherwise
  syntax damage inflates the score.
* Surviving mutants are reported individually with file, line and the exact
  edit, because the survivors are the finding. A score with no survivor list is
  unfalsifiable.
"""

from __future__ import annotations

import ast
import random
from dataclasses import dataclass, field
from pathlib import Path

from .. import util
from . import dockerenv
from .detect import RepoProfile

# Mutation is deterministic: same repo in, same mutants out.
SEED = 20240917

# Files whose contents cannot produce a meaningful mutant: version tuples,
# module entry guards and packaging scripts. Mutating these manufactures
# survivors that say nothing about test quality.
SKIP_FILES = ("_version.py", "__main__.py", "setup.py", "conftest.py", "_meta.py")

# Keyword arguments whose string values are human-facing display text. A test
# asserting on a help string is testing the help string, not the behaviour.
DISPLAY_KWARGS = {
    "doc", "help", "label", "description", "desc", "title", "metavar",
    "prog", "usage", "epilog", "name", "display", "message", "msg",
}


@dataclass
class Mutant:
    id: str
    file: str
    operator: str
    start_line: int
    end_line: int
    replacement: str
    before: str
    after: str
    target: str = ""

    def to_json(self) -> dict:
        return {
            "id": self.id, "file": self.file, "operator": self.operator,
            "start_line": self.start_line, "end_line": self.end_line,
            "replacement": self.replacement, "before": self.before,
            "after": self.after, "target": self.target,
        }


@dataclass
class MutationReport:
    total: int = 0
    run: int = 0
    invalid: int = 0
    killed_any: int = 0
    killed_by: dict[str, int] = field(default_factory=dict)
    only_generated: list[dict] = field(default_factory=list)
    survivors: list[dict] = field(default_factory=list)
    score: float = 0.0
    log: str = ""
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "total_candidates": self.total,
            "mutants_run": self.run,
            "discarded_unparsable": self.invalid,
            "killed": self.killed_any,
            "mutation_score": self.score,
            "killed_by_suite": self.killed_by,
            "killed_only_by_generated_tests": self.only_generated,
            "survivors": self.survivors,
            "notes": self.notes,
        }


# --------------------------------------------------------------------------
# mutation operators
# --------------------------------------------------------------------------

_CMP_SWAP = {
    ast.Lt: "<=", ast.LtE: "<", ast.Gt: ">=", ast.GtE: ">",
    ast.Eq: "!=", ast.NotEq: "==", ast.Is: "is not", ast.IsNot: "is",
    ast.In: "not in", ast.NotIn: "in",
}

_BINOP_SWAP = {
    ast.Add: "-", ast.Sub: "+", ast.Mult: "//", ast.Div: "*",
    ast.FloorDiv: "/", ast.Mod: "*", ast.BitAnd: "|", ast.BitOr: "&",
}

_CMP_TEXT = {
    ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=",
    ast.Eq: "==", ast.NotEq: "!=", ast.Is: "is", ast.IsNot: "is not",
    ast.In: "in", ast.NotIn: "not in",
}

_BINOP_TEXT = {
    ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/",
    ast.FloorDiv: "//", ast.Mod: "%", ast.BitAnd: "&", ast.BitOr: "|",
}


class _Mutator(ast.NodeTransformer):
    """Applies exactly one edit, identified by node position."""

    def __init__(self, target_pos: tuple[int, int], operator: str):
        self.target_pos = target_pos
        self.operator = operator
        self.applied = False

    def _at(self, node: ast.AST) -> bool:
        return (getattr(node, "lineno", -1), getattr(node, "col_offset", -1)) \
            == self.target_pos

    def visit_Compare(self, node: ast.Compare) -> ast.AST:
        self.generic_visit(node)
        if self.operator == "comparison" and self._at(node) and node.ops:
            swap = {ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE,
                    ast.GtE: ast.Gt, ast.Eq: ast.NotEq, ast.NotEq: ast.Eq,
                    ast.Is: ast.IsNot, ast.IsNot: ast.Is,
                    ast.In: ast.NotIn, ast.NotIn: ast.In}
            new = swap.get(type(node.ops[0]))
            if new:
                node.ops[0] = new()
                self.applied = True
        return node

    def visit_BinOp(self, node: ast.BinOp) -> ast.AST:
        self.generic_visit(node)
        if self.operator == "arithmetic" and self._at(node):
            swap = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.FloorDiv,
                    ast.Div: ast.Mult, ast.FloorDiv: ast.Div, ast.Mod: ast.Mult,
                    ast.BitAnd: ast.BitOr, ast.BitOr: ast.BitAnd}
            new = swap.get(type(node.op))
            if new:
                node.op = new()
                self.applied = True
        return node

    def visit_BoolOp(self, node: ast.BoolOp) -> ast.AST:
        self.generic_visit(node)
        if self.operator == "logic" and self._at(node):
            node.op = ast.Or() if isinstance(node.op, ast.And) else ast.And()
            self.applied = True
        return node

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        if self.operator == "constant" and self._at(node):
            v = node.value
            if isinstance(v, bool):
                node.value = not v
            elif isinstance(v, int):
                node.value = v + 1
            elif isinstance(v, float):
                node.value = v + 1.0
            elif isinstance(v, str):
                node.value = v + "X" if v else "X"
            else:
                return node
            self.applied = True
        return node

    def visit_Return(self, node: ast.Return) -> ast.AST:
        self.generic_visit(node)
        if self.operator == "return" and self._at(node) and node.value is not None:
            node.value = ast.Constant(value=None)
            self.applied = True
        return node


def _display_constants(tree: ast.AST) -> set[int]:
    """ids of string constants that are human-facing text, not behaviour."""
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg in DISPLAY_KWARGS:
                    for sub in ast.walk(kw.value):
                        out.add(id(sub))
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if isinstance(key, ast.Constant) and key.value in DISPLAY_KWARGS:
                    for sub in ast.walk(value):
                        out.add(id(sub))
    return out


def _mutable_regions(tree: ast.AST) -> list[ast.AST]:
    """Function and method bodies only.

    Module-level code runs at import time, so a mutant there is either caught by
    every test at once (uninformative) or by none (a constant nobody asserts on).
    Behaviour worth testing lives in function bodies.
    """
    regions: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            regions.extend(node.body)
    return regions


def _iter_candidates(tree: ast.AST, covered: set[int] | None = None
                     ) -> list[tuple[str, ast.AST, str, str]]:
    """(operator, node, before_text, after_text) for every viable single edit."""
    out: list[tuple[str, ast.AST, str, str]] = []
    skip_ids = _display_constants(tree)
    nodes: list[ast.AST] = []
    for region in _mutable_regions(tree):
        nodes.extend(ast.walk(region))

    for node in nodes:
        if id(node) in skip_ids:
            continue
        line = getattr(node, "lineno", None)
        # An uncovered line is guaranteed to survive; that is a coverage fact we
        # already report, not a statement about assertion quality.
        if covered is not None and (line is None or line not in covered):
            continue
        if isinstance(node, ast.Compare) and node.ops:
            t = type(node.ops[0])
            if t in _CMP_SWAP:
                out.append(("comparison", node, _CMP_TEXT[t], _CMP_SWAP[t]))
        elif isinstance(node, ast.BinOp):
            t = type(node.op)
            if t in _BINOP_SWAP:
                out.append(("arithmetic", node, _BINOP_TEXT[t], _BINOP_SWAP[t]))
        elif isinstance(node, ast.BoolOp):
            is_and = isinstance(node.op, ast.And)
            out.append(("logic", node, "and" if is_and else "or",
                        "or" if is_and else "and"))
        elif isinstance(node, ast.Constant):
            v = node.value
            if isinstance(v, bool):
                out.append(("constant", node, repr(v), repr(not v)))
            elif isinstance(v, int):
                out.append(("constant", node, repr(v), repr(v + 1)))
            elif isinstance(v, float):
                out.append(("constant", node, repr(v), repr(v + 1.0)))
            elif isinstance(v, str) and len(v) < 60 and "\n" not in v:
                out.append(("constant", node, repr(v), repr(v + "X")))
        elif isinstance(node, ast.Return) and node.value is not None:
            if not (isinstance(node.value, ast.Constant) and node.value.value is None):
                out.append(("return", node, "return <expr>", "return None"))
    return out


def _enclosing_statement(tree: ast.AST, node: ast.AST) -> ast.stmt | None:
    """The top-level statement containing ``node``, so we can re-render just it."""
    best: ast.stmt | None = None
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            if child is node or node in set(ast.walk(child)):
                if isinstance(child, ast.stmt):
                    if best is None or (child.lineno >= best.lineno
                                        and (child.end_lineno or 0)
                                        <= (best.end_lineno or 10 ** 9)):
                        best = child
    return best


def _covered_lines(coverage: dict, rel: str) -> set[int] | None:
    """Executed lines for one file, from the coverage report keyed loosely."""
    if not coverage:
        return None
    for key, info in coverage.items():
        norm = key.replace("\\", "/").lstrip("./")
        if norm == rel or norm.endswith("/" + rel) or rel.endswith("/" + norm):
            return set(info.get("executed_lines") or [])
    return set()


def build_mutants(prof: RepoProfile, repo: Path, limit: int = 60,
                  files: list[str] | None = None,
                  coverage: dict | None = None) -> list[Mutant]:
    """Generate up to ``limit`` single-edit mutants, spread across files."""
    rng = random.Random(SEED)
    sources = [repo / f for f in files] if files else \
        [repo / prof.rel(p) for p in prof.source_files()]

    per_file: dict[str, list[Mutant]] = {}
    for path in sorted(sources):
        if not path.exists() or path.name in SKIP_FILES:
            continue
        rel = path.relative_to(repo).as_posix()
        try:
            src = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(src)
        except SyntaxError:
            continue
        lines = src.splitlines(keepends=True)
        covered = _covered_lines(coverage or {}, rel)
        cands = _iter_candidates(tree, covered)
        rng.shuffle(cands)

        made: list[Mutant] = []
        used_positions: set[tuple[int, int]] = set()
        for operator, node, before, after in cands:
            pos = (getattr(node, "lineno", -1), getattr(node, "col_offset", -1))
            if pos in used_positions or pos[0] < 0:
                continue
            stmt = _enclosing_statement(tree, node)
            if stmt is None or stmt.lineno is None:
                continue
            # Re-parse per mutant so each edit starts from clean source.
            try:
                fresh = ast.parse(src)
            except SyntaxError:
                break
            fresh_stmt = None
            for n in ast.walk(fresh):
                if isinstance(n, ast.stmt) and n.lineno == stmt.lineno \
                        and n.col_offset == stmt.col_offset \
                        and type(n) is type(stmt):
                    fresh_stmt = n
                    break
            if fresh_stmt is None:
                continue
            mut = _Mutator(pos, operator)
            new_stmt = mut.visit(fresh_stmt)
            if not mut.applied:
                continue
            ast.fix_missing_locations(new_stmt)
            try:
                rendered = ast.unparse(new_stmt)
            except Exception:
                continue
            indent = " " * (stmt.col_offset or 0)
            replacement = "\n".join(indent + ln for ln in rendered.splitlines())
            original_text = "".join(
                lines[stmt.lineno - 1: (stmt.end_lineno or stmt.lineno)])
            if replacement.strip() == original_text.strip():
                continue
            used_positions.add(pos)
            made.append(Mutant(
                id=f"{rel}:{pos[0]}:{operator}",
                file=rel, operator=operator,
                start_line=stmt.lineno,
                end_line=stmt.end_lineno or stmt.lineno,
                replacement=replacement,
                before=f"{before}  |  {original_text.strip()[:120]}",
                after=f"{after}  |  {replacement.strip()[:120]}",
                target=rel,
            ))
            if len(made) >= max(4, limit // max(1, len(sources))) * 3:
                break
        if made:
            per_file[rel] = made

    # Round-robin across files so one big module cannot eat the whole budget.
    chosen: list[Mutant] = []
    idx = 0
    while len(chosen) < limit and per_file:
        progressed = False
        for rel in sorted(per_file):
            bucket = per_file[rel]
            if idx < len(bucket):
                chosen.append(bucket[idx])
                progressed = True
                if len(chosen) >= limit:
                    break
        if not progressed:
            break
        idx += 1
    chosen.sort(key=lambda m: m.id)
    util.info("mutants built", count=len(chosen), files=len(per_file))
    return chosen


# --------------------------------------------------------------------------
# execution
# --------------------------------------------------------------------------

def run(prof: RepoProfile, repo: Path, image: str, workdir: Path,
        suites: dict[str, list[str]], limit: int = 60,
        files: list[str] | None = None, timeout: int = 300,
        coverage: dict | None = None) -> MutationReport:
    mutants = build_mutants(prof, repo, limit=limit, files=files, coverage=coverage)
    if not mutants:
        return MutationReport(notes=["no mutable statements found"])

    workdir.mkdir(parents=True, exist_ok=True)
    util.write_json(workdir / "mutants.json", [m.to_json() for m in mutants])
    util.write_json(workdir / "plan.json", {
        "root": "/app", "suites": suites, "timeout": timeout,
        # Two nested limits. pytest-timeout kills an individual hanging test
        # (a mutant that inverts a loop bound is the common cause); the outer
        # subprocess timeout in the runner is the backstop for a hang that
        # pytest-timeout cannot interrupt. Both derive from the real suite
        # duration rather than from a constant.
        "pytest_args": ["-x", "-q", "-p", "no:cacheprovider",
                        f"--timeout={max(15, timeout // 3)}"],
    })
    runner = Path(__file__).with_name("mutate_runner.py")
    util.write_text(workdir / "mutate_runner.py", runner.read_text(encoding="utf-8"))

    # The tree is mounted read-write because each mutant is applied in place and
    # restored; the copy is throwaway, never the delivered output.
    res = dockerenv.run_in(
        image,
        ["python", "/mut/mutate_runner.py", "/mut/mutants.json", "/mut/plan.json",
         "/mut/results.json"],
        mounts=[dockerenv.Mount(repo, "/app"), dockerenv.Mount(workdir, "/mut")],
        workdir="/app", network="none",
        timeout=max(1800, timeout * len(mutants) * len(suites) // 2 + 600))

    rows = util.read_json(workdir / "results.json", []) or []
    report = MutationReport(total=len(mutants), log=util.truncate(res.output, 1500, 2500))
    killed_by: dict[str, int] = {name: 0 for name in suites}
    for row in rows:
        if row.get("status") == "invalid":
            report.invalid += 1
            continue
        if row.get("status") != "run":
            continue
        report.run += 1
        killers = row.get("killed_by") or []
        for name in killers:
            killed_by[name] = killed_by.get(name, 0) + 1
        if killers:
            report.killed_any += 1
            if "generated" in killers and "existing" not in killers:
                report.only_generated.append({
                    "id": row["id"], "file": row["file"], "line": row["line"],
                    "operator": row["operator"], "change": f'{row["before"]} -> {row["after"]}',
                })
        else:
            report.survivors.append({
                "id": row["id"], "file": row["file"], "line": row["line"],
                "operator": row["operator"],
                "change": f'{row["before"]} -> {row["after"]}',
            })

    report.killed_by = killed_by
    report.score = round(report.killed_any / report.run, 4) if report.run else 0.0
    report.notes.append(
        f"{report.killed_any}/{report.run} injected bugs detected "
        f"(mutation score {report.score:.1%})")
    if report.only_generated:
        report.notes.append(
            f"{len(report.only_generated)} bug(s) caught only by the generated "
            "tests -- these would have shipped undetected before this stage")
    if report.survivors:
        report.notes.append(
            f"{len(report.survivors)} mutant(s) survived; each is a real hole in "
            "the suite and is listed individually in .okf/mutation.json")
    util.info("mutation sweep done", run=report.run, killed=report.killed_any,
              score=f"{report.score:.1%}")
    return report
