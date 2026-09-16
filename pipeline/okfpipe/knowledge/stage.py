"""Stage 2 -- the knowledge layer.

The brief calls this "machine input, not documentation for humans", so the
output is a graph plus JSON Lines, and every record is designed to be checked
rather than believed:

``repo_graph.json``   nodes, edges, and the result of re-verifying every edge
``.okf/manifest.json`` schema, repo identity, counts, verification rates
``.okf/modules.jsonl`` one row per module: size, complexity, coverage, imports
``.okf/symbols.jsonl`` one row per function/class, with the tests that cover it
``.okf/edges.jsonl``   the edge list, each with file:line evidence
``.okf/claims.jsonl``  falsifiable statements about the repo, each with a
                       recorded verification method and outcome
``.okf/history.jsonl`` classified commits, the raw material for stage 3
``.okf/hotspots.json`` churn x complexity ranking used to prioritise tasks
``.okf/coverage.json`` per-file and per-test-context coverage

The per-test coverage contexts are the part stage 3 depends on most: knowing
*which tests execute a given function* is what makes an excision task possible
to validate, and what lets a history-derived task pick a verifier that actually
exercises the change.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from pathlib import Path

from .. import SCHEMA_VERSION, __version__, util
from ..hygiene import dockerenv
from ..hygiene.detect import RepoProfile, detect
from ..repo import RepoHandle, history as read_history
from . import graph as graph_mod

# Commit subjects that suggest a behavioural change worth mining.
FIX_PATTERNS = re.compile(
    r"\b(fix(e[sd])?|bug(fix)?|regression|broken|crash|incorrect|wrong|"
    r"fail(s|ed|ing)?|error|issue|defect|patch|resolve[sd]?|correct(s|ed)?)\b",
    re.I)
FEATURE_PATTERNS = re.compile(
    r"\b(add(s|ed|ing)?|implement(s|ed|ing)?|support|introduce[sd]?|"
    r"new|feature|allow(s|ed)?|enable[sd]?)\b", re.I)
NOISE_PATTERNS = re.compile(
    r"\b(typo|readme|changelog|docs?|documentation|comment|whitespace|"
    r"lint|format(ting)?|style|bump|release|version|merge branch|"
    r"rename|reorder|cleanup|refactor)\b", re.I)


# --------------------------------------------------------------------------
# coverage with per-test contexts
# --------------------------------------------------------------------------

def measure_contexts(prof: RepoProfile, repo: Path, image: str,
                     workdir: Path) -> tuple[dict, dict]:
    """Return (per-file summary, {file: {line: [test ids]}}).

    ``dynamic_context = test_function`` makes coverage record which test was
    running when each line executed. That mapping is what lets stage 3 answer
    "if I delete this function, which tests go red?" without guessing.
    """
    outdir = workdir / "contexts"
    outdir.mkdir(parents=True, exist_ok=True)
    rc = (
        "[run]\n"
        "branch = True\n"
        "dynamic_context = test_function\n"
        f"source = {','.join(prof.import_names) or '.'}\n"
        "\n[json]\n"
        "show_contexts = True\n"
    )
    util.write_text(outdir / "okf-coveragerc", rc)

    script = (
        "set -u\n"
        "cp /out/okf-coveragerc /tmp/okf-coveragerc\n"
        "python -m coverage run --rcfile=/tmp/okf-coveragerc -m pytest "
        f"{' '.join(prof.test_paths) or '.'} -p no:cacheprovider -q "
        "> /tmp/pytest.log 2>&1 || true\n"
        "python -m coverage json --rcfile=/tmp/okf-coveragerc "
        "-o /out/coverage-contexts.json > /tmp/cov.log 2>&1 || true\n"
        "tail -3 /tmp/pytest.log\n"
    )
    res = dockerenv.run_in(
        image, ["sh", "-c", script],
        mounts=[dockerenv.Mount(repo, "/app"), dockerenv.Mount(outdir, "/out")],
        workdir="/app", network="none", timeout=2400)

    data = util.read_json(outdir / "coverage-contexts.json", {}) or {}
    summary: dict = {}
    contexts: dict = {}
    for path, info in (data.get("files") or {}).items():
        rel = Path(path).as_posix().lstrip("./")
        s = info.get("summary", {})
        summary[rel] = {
            "percent": round(s.get("percent_covered", 0.0), 2),
            "num_statements": s.get("num_statements", 0),
            "missing_lines": info.get("missing_lines", []),
            "executed_lines": info.get("executed_lines", []),
        }
        per_line = {}
        for line, ctxs in (info.get("contexts") or {}).items():
            named = sorted({c for c in ctxs if c})
            if named:
                per_line[int(line)] = named
        if per_line:
            contexts[rel] = per_line

    util.info("coverage contexts measured", files=len(summary),
              with_contexts=len(contexts), exit=res.returncode)
    return summary, contexts


def tests_covering(node: graph_mod.Node, contexts: dict) -> list[str]:
    """Test ids that executed any line inside this symbol's range."""
    per_line = contexts.get(node.file) or {}
    if not per_line:
        return []
    found: set[str] = set()
    for line, ctxs in per_line.items():
        if node.line_start <= line <= node.line_end:
            found.update(ctxs)
    return sorted(found)


# --------------------------------------------------------------------------
# claims
# --------------------------------------------------------------------------

def _line(root: Path, rel: str, lineno: int, cache: dict) -> str:
    if rel not in cache:
        p = root / rel
        cache[rel] = p.read_text(encoding="utf-8", errors="replace").splitlines() \
            if p.exists() else []
    lines = cache[rel]
    return lines[lineno - 1] if 1 <= lineno <= len(lines) else ""


def build_claims(prof: RepoProfile, g: graph_mod.RepoGraph, coverage: dict,
                 contexts: dict) -> list[dict]:
    """Falsifiable statements about the repo, each verified as it is written.

    Only claim kinds a machine can re-check end up here. "This module handles
    configuration" is unverifiable prose; "``glom.core.Path.__init__`` is
    defined at glom/core.py:1465 and is executed by 41 tests" is not.
    """
    root = Path(prof.root)
    cache: dict[str, list[str]] = {}
    claims: list[dict] = []
    n = 0

    def emit(kind: str, subject: str, statement: str, evidence: dict,
             method: str, ok: bool, observed: str = "") -> None:
        nonlocal n
        n += 1
        claims.append({
            "id": f"claim-{n:05d}",
            "kind": kind,
            "subject": subject,
            "statement": statement,
            "evidence": evidence,
            "verification": {"method": method, "verified": ok,
                             **({"observed": observed[:300]} if observed else {})},
        })

    for node in sorted(g.nodes.values(), key=lambda x: x.id):
        if node.kind not in ("function", "method", "class"):
            continue

        # -- definition site -------------------------------------------
        text = _line(root, node.file, node.line_start, cache)
        keyword = "class" if node.kind == "class" else "def"
        ok = bool(re.search(rf"\b{keyword}\s+{re.escape(node.name)}\b", text)) or \
            bool(re.search(rf"\b{keyword}\s+{re.escape(node.name)}\b",
                           "\n".join(cache.get(node.file, [])
                                     [max(0, node.line_start - 1): node.line_start + 2])))
        emit("definition", node.qualname,
             f"{node.qualname} is defined as a {node.kind} at "
             f"{node.file}:{node.line_start}",
             {"file": node.file, "line": node.line_start},
             "source-line-match", ok, text.strip())

        # -- signature --------------------------------------------------
        if node.signature:
            emit("signature", node.qualname,
                 f"{node.qualname} accepts {node.signature}",
                 {"file": node.file, "line": node.line_start},
                 "ast-reparse", _verify_signature(root, node), node.signature)

        # -- test coverage ----------------------------------------------
        covering = tests_covering(node, contexts)
        if covering:
            emit("covered_by", node.qualname,
                 f"{node.qualname} is executed by {len(covering)} test(s)",
                 {"file": node.file, "line": node.line_start},
                 "coverage-context", True,
                 ", ".join(covering[:8]))
        else:
            # No test context does not mean no execution. A one-line exception
            # subclass runs at import time and shows up as covered, while no
            # test exercises it. Emitting "has no test exercising it" for those
            # ships a claim the coverage data itself contradicts, so the two
            # cases get different claim kinds and both are true as stated.
            file_cov = coverage.get(node.file, {})
            executed = set(file_cov.get("executed_lines") or [])
            body_lines = set(range(node.line_start, node.line_end + 1))
            hit = executed & body_lines
            if hit:
                emit("import_time_only", node.qualname,
                     f"{node.qualname} is executed when its module is imported, "
                     f"but no test exercises it directly",
                     {"file": node.file, "line": node.line_start},
                     "coverage-context", True,
                     f"{len(hit)} line(s) executed, 0 test contexts")
            else:
                emit("uncovered", node.qualname,
                     f"{node.qualname} has no test exercising it and none of its "
                     f"lines execute",
                     {"file": node.file, "line": node.line_start},
                     "coverage-context", True, "0 executed lines in range")

    # -- exceptions raised --------------------------------------------
    for edge in g.edges:
        if edge.kind != "raises":
            continue
        text = _line(root, edge.file, edge.line, cache)
        exc = edge.dst.split(":", 1)[-1].split(".")[-1]
        emit("raises", edge.src.split(":", 1)[-1],
             f"{edge.src.split(':', 1)[-1]} raises {exc} at "
             f"{edge.file}:{edge.line}",
             {"file": edge.file, "line": edge.line},
             "source-line-match", "raise" in text and exc in text, text.strip())

    # -- public API ----------------------------------------------------
    for edge in g.edges:
        if edge.kind != "exports":
            continue
        name = edge.dst.rsplit(".", 1)[-1]
        module = edge.src.split(":", 1)[-1]
        emit("public_api", f"{module}.{name}",
             f"{name} is declared public by {module}.__all__",
             {"file": edge.file, "line": edge.line},
             "source-line-match", True, name)

    verified = sum(1 for c in claims if c["verification"]["verified"])
    util.info("claims built", total=len(claims), verified=verified,
              rate=f"{verified / max(1, len(claims)):.1%}")
    return claims


def _verify_signature(root: Path, node: graph_mod.Node) -> bool:
    """Re-parse the file and confirm the recorded signature still renders."""
    p = root / node.file
    if not p.exists():
        return False
    try:
        tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return False
    for child in ast.walk(tree):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and child.name == node.name and child.lineno == node.line_start:
            return graph_mod.signature_of(child) == node.signature
    return False


# --------------------------------------------------------------------------
# history classification + hotspots
# --------------------------------------------------------------------------

def classify_commits(prof: RepoProfile, commits: list) -> list[dict]:
    """Label each commit and record which first-party source files it touched."""
    src_files = {prof.rel(p) for p in prof.source_files()}
    test_files = {prof.rel(p) for p in prof.test_files()}
    rows: list[dict] = []
    for c in commits:
        touched_src = sorted(f for f in c.files if f in src_files
                             or (f.endswith(".py") and not prof.is_test_file(Path(f))))
        touched_tests = sorted(f for f in c.files
                               if f in test_files or prof.is_test_file(Path(f)))
        subject = c.subject
        if NOISE_PATTERNS.search(subject) and not FIX_PATTERNS.search(subject):
            label = "noise"
        elif FIX_PATTERNS.search(subject):
            label = "fix"
        elif FEATURE_PATTERNS.search(subject):
            label = "feature"
        else:
            label = "other"
        rows.append({
            "sha": c.sha, "parents": c.parents, "date": c.author_date,
            "subject": subject, "label": label, "is_merge": c.is_merge,
            "files": c.files, "source_files": touched_src,
            "test_files": touched_tests,
            "insertions": c.insertions, "deletions": c.deletions,
            "touches_source": bool(touched_src),
            "touches_tests": bool(touched_tests),
        })
    counts = Counter(r["label"] for r in rows)
    util.info("history classified", total=len(rows), **{k: v for k, v in counts.items()})
    return rows


def hotspots(prof: RepoProfile, g: graph_mod.RepoGraph, commit_rows: list[dict],
             coverage: dict) -> list[dict]:
    """Rank files by churn x complexity x coverage gap.

    Stage 3 uses this to pick where tasks are worth mining: a file that changes
    often, is complex, and is thinly covered is exactly where a real bug lived.
    """
    churn = Counter()
    for row in commit_rows:
        if row["label"] == "noise":
            continue
        for f in row["source_files"]:
            churn[f] += 1

    by_file: dict[str, dict] = {}
    for node in g.nodes.values():
        if node.kind not in ("function", "method", "class"):
            continue
        entry = by_file.setdefault(node.file, {"complexity": 0, "symbols": 0})
        entry["complexity"] += node.complexity
        entry["symbols"] += 1

    rows: list[dict] = []
    for rel, entry in by_file.items():
        cov = coverage.get(rel, {})
        pct = float(cov.get("percent", 0.0))
        c = churn.get(rel, 0)
        # Uncovered complexity in a file that keeps changing is the risk signal.
        score = round(c * entry["complexity"] * (1.0 + (100.0 - pct) / 100.0), 2)
        rows.append({
            "file": rel, "churn_commits": c, "total_complexity": entry["complexity"],
            "symbols": entry["symbols"], "coverage_percent": pct,
            "risk_score": score,
        })
    rows.sort(key=lambda r: (-r["risk_score"], r["file"]))
    return rows


# --------------------------------------------------------------------------
# emission
# --------------------------------------------------------------------------

def run(out_repo: Path, handle: RepoHandle, out_root: Path,
        workdir: Path) -> dict:
    workdir.mkdir(parents=True, exist_ok=True)
    prof = detect(out_repo)
    okf = out_repo / ".okf"
    failures: list[str] = []

    hygiene = util.read_json(okf / "hygiene.json", {}) or {}
    image = hygiene.get("image_tag") or f"okf-{util.slugify(prof.name)}:latest"

    # ---- structure ------------------------------------------------------
    g = graph_mod.build(prof)
    edge_report = graph_mod.verify_edges(prof, g)
    if edge_report["rate"] < 0.9:
        failures.append(f"only {edge_report['rate']:.1%} of graph edges verified "
                        "against source")

    # ---- coverage with contexts -----------------------------------------
    if dockerenv.image_exists(image):
        coverage, contexts = measure_contexts(prof, out_repo, image, workdir)
    else:
        coverage, contexts = {}, {}
        failures.append(f"image {image} not found; run --stages hygiene first")

    # ---- history --------------------------------------------------------
    commits = read_history(handle)
    commit_rows = classify_commits(prof, commits)

    # ---- claims ---------------------------------------------------------
    claims = build_claims(prof, g, coverage, contexts)
    verified_claims = sum(1 for c in claims if c["verification"]["verified"])
    claim_rate = round(verified_claims / max(1, len(claims)), 4)

    # ---- emit -----------------------------------------------------------
    modules = []
    for node in sorted(g.by_kind("module"), key=lambda n: n.qualname):
        cov = coverage.get(node.file, {})
        imports = sorted({e.dst.split(":", 1)[-1] for e in g.edges
                          if e.kind == "imports" and e.src == node.id})
        symbols = [e.dst for e in g.edges if e.kind == "contains" and e.src == node.id]
        modules.append({
            "module": node.qualname, "file": node.file, "loc": node.loc,
            "is_test": bool(node.extra.get("is_test")),
            "docstring_summary": node.docstring_summary,
            "imports": imports, "symbol_ids": sorted(symbols),
            "symbol_count": len(symbols),
            "coverage_percent": cov.get("percent"),
            "missing_lines": cov.get("missing_lines", []),
            "num_statements": cov.get("num_statements"),
        })

    symbols_rows = []
    for node in sorted(g.nodes.values(), key=lambda n: n.id):
        if node.kind not in ("function", "method", "class", "testcase"):
            continue
        covering = tests_covering(node, contexts)
        calls = sorted({e.dst.split(":", 1)[-1] for e in g.edges
                        if e.kind == "calls" and e.src == node.id})
        called_by = sorted({e.src.split(":", 1)[-1] for e in g.edges
                            if e.kind == "calls" and e.dst == node.id})
        raises = sorted({e.dst.split(":", 1)[-1] for e in g.edges
                         if e.kind == "raises" and e.src == node.id})
        symbols_rows.append({
            "id": node.id, "kind": node.kind, "qualname": node.qualname,
            "name": node.name, "file": node.file,
            "line_start": node.line_start, "line_end": node.line_end,
            "loc": node.loc, "signature": node.signature,
            "complexity": node.complexity, "is_public": node.is_public,
            "decorators": node.decorators,
            "docstring_summary": node.docstring_summary,
            "calls": calls, "called_by": called_by, "raises": raises,
            "tests_covering": covering, "test_count": len(covering),
        })

    util.write_jsonl(okf / "modules.jsonl", modules)
    util.write_jsonl(okf / "symbols.jsonl", symbols_rows)
    util.write_jsonl(okf / "edges.jsonl", [e.to_json() for e in g.edges])
    util.write_jsonl(okf / "claims.jsonl", claims)
    util.write_jsonl(okf / "history.jsonl", commit_rows)
    util.write_json(okf / "coverage.json",
                    {"files": coverage, "contexts_available": bool(contexts),
                     "context_files": sorted(contexts)})
    hot = hotspots(prof, g, commit_rows, coverage)
    util.write_json(okf / "hotspots.json", hot)

    manifest = {
        "schema": SCHEMA_VERSION,
        "generator": f"okfpipe {__version__}",
        "repo": {
            "name": prof.name,
            "remote": handle.remote_url,
            "head_sha": handle.head_sha,
            "branch": handle.default_branch,
            "packages": prof.import_names,
            "python_version": prof.python_version,
        },
        "counts": {
            "modules": len(modules),
            "symbols": len(symbols_rows),
            "edges": len(g.edges),
            "claims": len(claims),
            "commits": len(commit_rows),
        },
        "verification": {
            "edges": edge_report,
            "claims": {"checked": len(claims), "verified": verified_claims,
                       "rate": claim_rate},
        },
        "files": {
            "graph": "../repo_graph.json",
            "modules": "modules.jsonl", "symbols": "symbols.jsonl",
            "edges": "edges.jsonl", "claims": "claims.jsonl",
            "history": "history.jsonl", "coverage": "coverage.json",
            "hotspots": "hotspots.json",
        },
        "unresolved_call_targets": dict(
            sorted(g.unresolved_calls.items(), key=lambda kv: -kv[1])[:40]),
    }
    util.write_json(okf / "manifest.json", manifest)

    util.write_json(out_root / "repo_graph.json", {
        "schema": SCHEMA_VERSION,
        "generator": f"okfpipe {__version__}",
        "repo": manifest["repo"],
        "node_kinds": list(graph_mod.NODE_KINDS),
        "edge_kinds": list(graph_mod.EDGE_KINDS),
        "nodes": [n.to_json() for n in sorted(g.nodes.values(), key=lambda x: x.id)],
        "edges": [e.to_json() for e in g.edges],
        "verification": manifest["verification"],
        "stats": manifest["counts"],
    })

    util.info("knowledge layer written", modules=len(modules),
              symbols=len(symbols_rows), claims=len(claims),
              edge_verified=f"{edge_report['rate']:.1%}",
              claim_verified=f"{claim_rate:.1%}")

    return {
        "ok": not failures,
        "modules": len(modules), "symbols": len(symbols_rows),
        "edges": len(g.edges), "claims": len(claims),
        "commits": len(commit_rows),
        "edge_verification_rate": edge_report["rate"],
        "claim_verification_rate": claim_rate,
        "coverage_contexts": bool(contexts),
        "failures": failures,
    }
