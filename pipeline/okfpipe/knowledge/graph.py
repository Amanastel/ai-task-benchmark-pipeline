"""Stage 2a -- static structure extraction.

Builds the node/edge graph that ``repo_graph.json`` and ``.okf/`` are rendered
from. Three principles:

**Every edge carries evidence.** An edge without a ``file:line`` is an
assertion nobody can check. Each one records where in the source it came from,
which is what makes the verification pass in ``verify_edges`` possible at all.

**Confidence is explicit.** Python call resolution is undecidable in general.
Rather than pretend otherwise, each call edge is labelled ``exact`` (resolved
through an import or a module-level definition) or ``heuristic`` (name matched,
target inferred). A consumer can filter on it; silently mixing the two would
make the graph's accuracy unmeasurable.

**Nothing is imported.** The repo is parsed, never executed. Importing to
introspect would run arbitrary module-level code and would also fail on exactly
the half-broken repos this pipeline exists to fix.
"""

from __future__ import annotations

import ast
import builtins
from dataclasses import dataclass, field
from pathlib import Path

from .. import util
from ..hygiene.detect import RepoProfile

NODE_KINDS = ("package", "module", "class", "function", "method", "testcase")
EDGE_KINDS = ("contains", "imports", "calls", "inherits", "decorates",
              "raises", "exports", "tests")


@dataclass
class Node:
    id: str
    kind: str
    name: str
    qualname: str
    file: str = ""
    line_start: int = 0
    line_end: int = 0
    signature: str = ""
    docstring_summary: str = ""
    is_public: bool = True
    decorators: list[str] = field(default_factory=list)
    complexity: int = 0
    loc: int = 0
    extra: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        d = {
            "id": self.id, "kind": self.kind, "name": self.name,
            "qualname": self.qualname, "file": self.file,
            "line_start": self.line_start, "line_end": self.line_end,
            "is_public": self.is_public, "loc": self.loc,
        }
        if self.signature:
            d["signature"] = self.signature
        if self.docstring_summary:
            d["docstring_summary"] = self.docstring_summary
        if self.decorators:
            d["decorators"] = self.decorators
        if self.complexity:
            d["complexity"] = self.complexity
        if self.extra:
            d.update(self.extra)
        return d


@dataclass
class Edge:
    src: str
    dst: str
    kind: str
    file: str
    line: int
    confidence: str = "exact"
    detail: str = ""
    verified: bool | None = None

    @property
    def id(self) -> str:
        return f"{self.kind}:{self.src}->{self.dst}@{self.file}:{self.line}"

    def to_json(self) -> dict:
        d = {
            "src": self.src, "dst": self.dst, "kind": self.kind,
            "evidence": {"file": self.file, "line": self.line},
            "confidence": self.confidence,
        }
        if self.detail:
            d["detail"] = self.detail
        if self.verified is not None:
            d["verified"] = self.verified
        return d


@dataclass
class RepoGraph:
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    unresolved_calls: dict[str, int] = field(default_factory=dict)

    def add_node(self, node: Node) -> Node:
        self.nodes[node.id] = node
        return node

    def add_edge(self, edge: Edge) -> None:
        self.edges.append(edge)

    def by_kind(self, kind: str) -> list[Node]:
        return [n for n in self.nodes.values() if n.kind == kind]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

_BRANCHING = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.ExceptHandler,
              ast.With, ast.AsyncWith, ast.Assert, ast.IfExp,
              ast.comprehension, ast.BoolOp, ast.Match)


def cyclomatic(node: ast.AST) -> int:
    """McCabe complexity: one plus the number of independent branch points."""
    score = 1
    for child in ast.walk(node):
        if isinstance(child, _BRANCHING):
            score += len(child.values) - 1 if isinstance(child, ast.BoolOp) else 1
        elif isinstance(child, ast.Match):
            score += len(child.cases)
    return score


def _unparse(node: ast.AST | None) -> str:
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except Exception:
        return ""


def signature_of(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    a = fn.args
    parts: list[str] = []
    positional = list(a.posonlyargs) + list(a.args)
    defaults = list(a.defaults)
    pad = len(positional) - len(defaults)
    for i, arg in enumerate(positional):
        text = arg.arg
        if arg.annotation is not None:
            text += f": {_unparse(arg.annotation)}"
        if i >= pad:
            text += f"={_unparse(defaults[i - pad])}"
        parts.append(text)
        if a.posonlyargs and i == len(a.posonlyargs) - 1:
            parts.append("/")
    if a.vararg:
        parts.append("*" + a.vararg.arg)
    elif a.kwonlyargs:
        parts.append("*")
    for arg, default in zip(a.kwonlyargs, a.kw_defaults, strict=True):
        text = arg.arg
        if arg.annotation is not None:
            text += f": {_unparse(arg.annotation)}"
        if default is not None:
            text += f"={_unparse(default)}"
        parts.append(text)
    if a.kwarg:
        parts.append("**" + a.kwarg.arg)
    returns = f" -> {_unparse(fn.returns)}" if fn.returns else ""
    return f"({', '.join(parts)}){returns}"


def _summary(doc: str | None) -> str:
    if not doc:
        return ""
    first = doc.strip().split("\n\n")[0].replace("\n", " ").strip()
    return first[:200]


def module_name_for(prof: RepoProfile, path: Path) -> str:
    rel = Path(prof.rel(path))
    if prof.source_root:
        try:
            rel = rel.relative_to(prof.source_root)
        except ValueError:
            pass
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


# --------------------------------------------------------------------------
# per-module extraction
# --------------------------------------------------------------------------

class _ModuleVisitor:
    """Walks one module, emitting nodes and edges."""

    def __init__(self, graph: RepoGraph, module: str, rel_path: str,
                 source: str, is_test: bool):
        self.g = graph
        self.module = module
        self.rel = rel_path
        self.source = source
        self.lines = source.splitlines()
        self.is_test = is_test
        # local name -> fully qualified target, populated from imports
        self.imports: dict[str, str] = {}
        self.module_level: set[str] = set()

    # -- entry ----------------------------------------------------------
    def run(self, tree: ast.Module) -> None:
        mod_id = f"module:{self.module}"
        self.g.add_node(Node(
            id=mod_id, kind="module", name=self.module.rsplit(".", 1)[-1],
            qualname=self.module, file=self.rel, line_start=1,
            line_end=len(self.lines), loc=len(self.lines),
            docstring_summary=_summary(ast.get_docstring(tree)),
            is_public=not self.module.rsplit(".", 1)[-1].startswith("_"),
            extra={"is_test": self.is_test},
        ))

        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.module_level.add(node.name)
            elif isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        self.module_level.add(tgt.id)

        self._collect_imports(tree, mod_id)
        self._collect_exports(tree, mod_id)
        self._walk_body(tree, parent_id=mod_id, prefix="")

    # -- imports ---------------------------------------------------------
    def _collect_imports(self, tree: ast.Module, mod_id: str) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    local = alias.asname or alias.name.split(".")[0]
                    self.imports[local] = alias.name
                    self.g.add_edge(Edge(
                        src=mod_id, dst=f"module:{alias.name}", kind="imports",
                        file=self.rel, line=node.lineno,
                        detail=f"import {alias.name}"
                        + (f" as {alias.asname}" if alias.asname else "")))
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    # Relative import: resolve against this module's package.
                    pkg = self.module.split(".")
                    pkg = pkg[: max(0, len(pkg) - node.level)]
                    base = ".".join([p for p in (".".join(pkg), base) if p])
                for alias in node.names:
                    if alias.name == "*":
                        self.g.add_edge(Edge(
                            src=mod_id, dst=f"module:{base}", kind="imports",
                            file=self.rel, line=node.lineno,
                            confidence="heuristic", detail=f"from {base} import *"))
                        continue
                    local = alias.asname or alias.name
                    self.imports[local] = f"{base}.{alias.name}" if base else alias.name
                    self.g.add_edge(Edge(
                        src=mod_id, dst=f"module:{base}", kind="imports",
                        file=self.rel, line=node.lineno,
                        detail=f"from {base} import {alias.name}"))

    def _collect_exports(self, tree: ast.Module, mod_id: str) -> None:
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "__all__":
                    try:
                        names = ast.literal_eval(node.value)
                    except (ValueError, SyntaxError):
                        continue
                    for name in names or []:
                        self.g.add_edge(Edge(
                            src=mod_id, dst=f"symbol:{self.module}.{name}",
                            kind="exports", file=self.rel, line=node.lineno,
                            detail=f"__all__ entry {name!r}"))

    # -- definitions -----------------------------------------------------
    def _walk_body(self, node: ast.AST, parent_id: str, prefix: str,
                   in_class: bool = False) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                self._class(child, parent_id, prefix)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._function(child, parent_id, prefix, in_class)

    def _class(self, node: ast.ClassDef, parent_id: str, prefix: str) -> None:
        qual = f"{self.module}.{prefix}{node.name}"
        nid = f"class:{qual}"
        self.g.add_node(Node(
            id=nid, kind="class", name=node.name, qualname=qual, file=self.rel,
            line_start=node.lineno, line_end=node.end_lineno or node.lineno,
            loc=(node.end_lineno or node.lineno) - node.lineno + 1,
            docstring_summary=_summary(ast.get_docstring(node)),
            is_public=not node.name.startswith("_"),
            decorators=[_unparse(d) for d in node.decorator_list],
            complexity=cyclomatic(node),
        ))
        self.g.add_edge(Edge(src=parent_id, dst=nid, kind="contains",
                             file=self.rel, line=node.lineno))
        for base in node.bases:
            text = _unparse(base)
            target = self.imports.get(text.split(".")[0], text)
            self.g.add_edge(Edge(
                src=nid, dst=f"symbol:{target}", kind="inherits",
                file=self.rel, line=node.lineno,
                confidence="exact" if text.split(".")[0] in self.imports else "heuristic",
                detail=f"class {node.name}({text})"))
        self._walk_body(node, parent_id=nid, prefix=f"{prefix}{node.name}.",
                        in_class=True)

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef,
                  parent_id: str, prefix: str, in_class: bool) -> None:
        qual = f"{self.module}.{prefix}{node.name}"
        kind = "method" if in_class else "function"
        if self.is_test and node.name.startswith("test"):
            kind = "testcase"
        nid = f"symbol:{qual}"
        self.g.add_node(Node(
            id=nid, kind=kind, name=node.name, qualname=qual, file=self.rel,
            line_start=node.lineno, line_end=node.end_lineno or node.lineno,
            loc=(node.end_lineno or node.lineno) - node.lineno + 1,
            signature=signature_of(node),
            docstring_summary=_summary(ast.get_docstring(node)),
            is_public=not node.name.startswith("_"),
            decorators=[_unparse(d) for d in node.decorator_list],
            complexity=cyclomatic(node),
            extra={"is_async": isinstance(node, ast.AsyncFunctionDef),
                   "param_count": len(node.args.args) + len(node.args.kwonlyargs)},
        ))
        self.g.add_edge(Edge(src=parent_id, dst=nid, kind="contains",
                             file=self.rel, line=node.lineno))
        for deco in node.decorator_list:
            text = _unparse(deco).split("(")[0]
            if text:
                target = self.imports.get(text.split(".")[0], text)
                self.g.add_edge(Edge(
                    src=nid, dst=f"symbol:{target}", kind="decorates",
                    file=self.rel, line=getattr(deco, "lineno", node.lineno),
                    confidence="exact" if text.split(".")[0] in self.imports
                    else "heuristic", detail=f"@{text}"))

        self._calls_and_raises(node, nid)
        self._walk_body(node, parent_id=nid, prefix=f"{prefix}{node.name}.",
                        in_class=False)

    # -- call / raise edges ---------------------------------------------
    def _calls_and_raises(self, fn: ast.AST, owner: str) -> None:
        seen: set[tuple[str, str, int]] = set()
        for child in ast.walk(fn):
            if isinstance(child, ast.Call):
                name = _unparse(child.func)
                if not name:
                    continue
                root = name.split(".")[0].split("(")[0]
                if root in self.imports:
                    target, confidence = self.imports[root], "exact"
                    if "." in name:
                        target = self.imports[root] + name[len(root):]
                elif root in self.module_level:
                    target, confidence = f"{self.module}.{name}", "exact"
                elif hasattr(builtins, root):
                    continue  # builtins are noise in a repo graph
                else:
                    target, confidence = name, "heuristic"
                    self.g.unresolved_calls[name] = \
                        self.g.unresolved_calls.get(name, 0) + 1
                key = ("calls", target, child.lineno)
                if key in seen:
                    continue
                seen.add(key)
                self.g.add_edge(Edge(
                    src=owner, dst=f"symbol:{target}", kind="calls",
                    file=self.rel, line=child.lineno, confidence=confidence,
                    detail=name[:120]))
            elif isinstance(child, ast.Raise) and child.exc is not None:
                text = _unparse(child.exc).split("(")[0]
                if not text:
                    continue
                root = text.split(".")[0]
                target = self.imports.get(root, text)
                if root in self.imports and "." in text:
                    target = self.imports[root] + text[len(root):]
                key = ("raises", target, child.lineno)
                if key in seen:
                    continue
                seen.add(key)
                self.g.add_edge(Edge(
                    src=owner, dst=f"exception:{target}", kind="raises",
                    file=self.rel, line=child.lineno,
                    confidence="exact" if root in self.imports
                    or root in self.module_level else "heuristic",
                    detail=f"raise {text}"))


# --------------------------------------------------------------------------
# build + verify
# --------------------------------------------------------------------------

def build(prof: RepoProfile) -> RepoGraph:
    graph = RepoGraph()

    for pkg in prof.packages:
        pid = f"package:{pkg.name}"
        graph.add_node(Node(id=pid, kind="package", name=pkg.name,
                            qualname=pkg.name, file=pkg.path, line_start=0,
                            extra={"module_count": pkg.module_count}))

    paths = list(prof.source_files()) + list(prof.test_files())
    for path in sorted(set(paths)):
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source)
        except (SyntaxError, OSError) as exc:
            util.warn("skipping unparsable file", file=prof.rel(path), error=str(exc))
            continue
        module = module_name_for(prof, path)
        if not module:
            continue
        visitor = _ModuleVisitor(graph, module, prof.rel(path), source,
                                 is_test=prof.is_test_file(path))
        visitor.run(tree)
        top = module.split(".")[0]
        if f"package:{top}" in graph.nodes:
            graph.add_edge(Edge(src=f"package:{top}", dst=f"module:{module}",
                                kind="contains", file=prof.rel(path), line=1))

    util.info("graph built", nodes=len(graph.nodes), edges=len(graph.edges),
              unresolved=len(graph.unresolved_calls))
    return graph


def verify_edges(prof: RepoProfile, graph: RepoGraph) -> dict:
    """Re-check every edge against the source file it claims to come from.

    Verification deliberately does not reuse the AST walk that produced the
    edges -- it re-reads the raw line and looks for the token the edge names.
    A check that shares its logic with the thing it checks proves nothing.
    """
    cache: dict[str, list[str]] = {}
    root = Path(prof.root)

    def lines_of(rel: str) -> list[str]:
        if rel not in cache:
            p = root / rel
            cache[rel] = p.read_text(encoding="utf-8", errors="replace").splitlines() \
                if p.exists() else []
        return cache[rel]

    checked = verified = 0
    failures: list[dict] = []
    for edge in graph.edges:
        lines = lines_of(edge.file)
        if not lines or edge.line < 1 or edge.line > len(lines):
            edge.verified = False
            failures.append({"edge": edge.id, "reason": "evidence line out of range"})
            checked += 1
            continue
        # Look in a small window: a call's lineno is the start of the call
        # expression, which for a wrapped argument list can sit a line above
        # the token we are looking for.
        window = "\n".join(lines[max(0, edge.line - 1): edge.line + 2])
        token = _expected_token(edge)
        ok = (token in window) if token else True
        edge.verified = ok
        checked += 1
        verified += int(ok)
        if not ok:
            failures.append({"edge": edge.id, "reason": f"token {token!r} not near "
                                                        f"{edge.file}:{edge.line}"})

    rate = round(verified / checked, 4) if checked else 1.0
    util.info("edges verified", checked=checked, verified=verified,
              rate=f"{rate:.2%}")
    return {"checked": checked, "verified": verified, "rate": rate,
            "failures": failures[:50], "failure_count": len(failures)}


def _expected_token(edge: Edge) -> str:
    """A literal string that must appear near the evidence line if the edge is real."""
    if edge.kind == "contains":
        return ""
    if edge.kind in ("imports", "exports"):
        return "import" if edge.kind == "imports" else "__all__"
    if edge.kind == "calls":
        return edge.detail.split(".")[-1].split("(")[0] if edge.detail else ""
    if edge.kind == "raises":
        return "raise"
    if edge.kind == "inherits":
        return "class "
    if edge.kind == "decorates":
        return "@"
    return ""
