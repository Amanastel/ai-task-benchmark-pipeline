"""Stage 1c -- test generation.

The bar set for this stage is that generated tests "assert real observable
behavior ... not merely that functions run without raising", and that they
catch deliberately introduced bugs. That rules out the easy win (call
everything, assert no exception) and shapes the design:

**Two generators, both producing literal expectations.**

*Doctest materialisation.* Executable examples already in the codebase are
authored expectations -- a human wrote down what the output should be. We turn
each docstring's examples into an individually-named pytest case that fails
with the real diff. High value, zero invention, and it catches any bug that
changes documented behaviour.

*Characterisation probing.* For callables with no examples we synthesise
arguments from type hints, defaults and a typed value bank, execute them in the
pinned container, and write the observed result back as a literal assertion
(``assert f(-3) == 0``) or an expected exception. These pin current behaviour
rather than validate it: a generated test encodes what the code *does*, so it
detects change, not wrongness. That distinction is stated in the generated
file's header and in REPORT.md, because a reader who mistakes one for the other
will trust these tests for something they cannot do.

**Every case is filtered before it ships.** A case survives only if its value
reprs identically under two different hash seeds, its repr is free of
addresses, paths and timestamps, and the emitted test actually passes when run.
Anything else is dropped, so the generated suite cannot be the reason the
acceptance bar fails.
"""

from __future__ import annotations

import ast
import doctest
import re
from dataclasses import dataclass, field
from pathlib import Path

from .. import util
from . import dockerenv
from .detect import RepoProfile

GENERATED_DIR = "tests_generated"

DEFAULT_DOCTEST_FLAGS = ["NORMALIZE_WHITESPACE", "ELLIPSIS", "IGNORE_EXCEPTION_DETAIL"]

# Argument values offered per annotation, ordered so index 0 is the "ordinary"
# value and later entries are edge cases.
VALUE_BANK: dict[str, list[str]] = {
    "int": ["1", "0", "-3", "255"],
    "float": ["1.5", "0.0", "-2.25"],
    "bool": ["True", "False"],
    "str": ["'abc'", "''", "'a b'", "'\\u00fc\\u00f1\\u00ee'"],
    "bytes": ["b'ab'", "b''"],
    "list": ["[1, 2, 3]", "[]", "['a', 'b']"],
    "dict": ["{'a': 1, 'b': 2}", "{}"],
    "tuple": ["(1, 2)", "()"],
    "set": ["{1, 2}", "set()"],
    "frozenset": ["frozenset({1, 2})"],
    "none": ["None"],
    "any": ["1", "'abc'", "None", "[]", "{}"],
}

_ALIASES = {
    "sequence": "list", "iterable": "list", "collection": "list",
    "mapping": "dict", "text": "str", "anystr": "str", "number": "int",
    "optional": "any", "object": "any", "typing.any": "any",
}


@dataclass
class TestGenResult:
    ok: bool
    files: list[str] = field(default_factory=list)
    cases: int = 0
    doctest_cases: int = 0
    value_cases: int = 0
    exception_cases: int = 0
    dropped: int = 0
    modules_targeted: list[str] = field(default_factory=list)
    coverage_before: dict = field(default_factory=dict)
    coverage_after: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    log: str = ""


# --------------------------------------------------------------------------
# coverage
# --------------------------------------------------------------------------

def measure_coverage(prof: RepoProfile, repo: Path, image: str,
                     workdir: Path) -> dict:
    """Per-file line coverage from the existing suite, measured in-container."""
    script = (
        "set -u\n"
        "python -m coverage run --branch "
        f"--source={','.join(prof.import_names) or '.'} -m pytest "
        f"{' '.join(prof.test_paths) or '.'} -p no:cacheprovider -q "
        "> /tmp/pytest.log 2>&1 || true\n"
        "python -m coverage json -o /out/coverage.json --pretty-print "
        "> /tmp/cov.log 2>&1 || true\n"
        "tail -5 /tmp/pytest.log\n"
    )
    outdir = workdir / "coverage"
    outdir.mkdir(parents=True, exist_ok=True)
    res = dockerenv.run_in(
        image, ["sh", "-c", script],
        mounts=[dockerenv.Mount(repo, "/app"), dockerenv.Mount(outdir, "/out")],
        workdir="/app", network="none", timeout=1800)
    data = util.read_json(outdir / "coverage.json", {}) or {}
    files = data.get("files", {})
    out = {}
    for path, info in files.items():
        summ = info.get("summary", {})
        out[Path(path).as_posix().lstrip("./")] = {
            "percent": round(summ.get("percent_covered", 0.0), 2),
            "missing_lines": info.get("missing_lines", []),
            "executed_lines": info.get("executed_lines", []),
            "num_statements": summ.get("num_statements", 0),
        }
    util.debug("coverage measured", files=len(out), exit=res.returncode)
    return out


# --------------------------------------------------------------------------
# candidate discovery
# --------------------------------------------------------------------------

def _module_name(prof: RepoProfile, path: Path) -> str:
    rel = Path(prof.rel(path))
    if prof.source_root:
        rel = rel.relative_to(prof.source_root)
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _annotation_key(node: ast.AST | None) -> str:
    if node is None:
        return "any"
    try:
        text = ast.unparse(node)
    except Exception:
        return "any"
    text = text.strip().strip("'\"").lower()
    text = re.sub(r"^typing\.", "", text)
    if text.startswith("optional[") or "| none" in text or "none |" in text:
        inner = re.sub(r"^optional\[|\]$", "", text).split("|")[0].strip()
        return _ALIASES.get(inner, inner if inner in VALUE_BANK else "any")
    base = re.split(r"[\[\|]", text)[0].strip()
    base = _ALIASES.get(base, base)
    return base if base in VALUE_BANK else "any"


@dataclass
class Target:
    module: str
    qualname: str
    call_name: str          # how to reference it from the module namespace
    params: list[tuple[str, str, bool]]   # (name, annotation_key, has_default)
    source_file: str
    lineno: int
    is_method: bool
    docstring: str


def discover_targets(prof: RepoProfile) -> list[Target]:
    """Public, callable, argument-synthesisable functions across the repo."""
    targets: list[Target] = []
    for path in prof.source_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        module = _module_name(prof, path)
        if not module:
            continue

        # module/path are bound as defaults rather than captured: the closure is
        # redefined per file, and binding makes that independence explicit.
        def visit(node: ast.AST, prefix: str = "", is_method: bool = False,
                  module: str = module, path: Path = path) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.ClassDef):
                    if child.name.startswith("_"):
                        continue
                    visit(child, f"{prefix}{child.name}.", is_method=True,
                          module=module, path=path)
                elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if child.name.startswith("_") and child.name != "__init__":
                        continue
                    if isinstance(child, ast.AsyncFunctionDef):
                        continue  # needs an event loop; out of scope
                    if any(_deco_name(d) in ("property", "cached_property")
                           for d in child.decorator_list):
                        continue
                    args = child.args
                    positional = list(args.posonlyargs) + list(args.args)
                    n_defaults = len(args.defaults)
                    params: list[tuple[str, str, bool]] = []
                    for i, a in enumerate(positional):
                        if i == 0 and is_method and a.arg in ("self", "cls"):
                            continue
                        has_default = i >= len(positional) - n_defaults
                        params.append((a.arg, _annotation_key(a.annotation), has_default))
                    targets.append(Target(
                        module=module,
                        qualname=f"{prefix}{child.name}",
                        call_name=f"{prefix}{child.name}",
                        params=params,
                        source_file=prof.rel(path),
                        lineno=child.lineno,
                        is_method=is_method,
                        docstring=ast.get_docstring(child) or "",
                    ))
        visit(tree)
    return targets


def _deco_name(node: ast.AST) -> str:
    try:
        text = ast.unparse(node)
    except Exception:
        return ""
    return text.split("(")[0].split(".")[-1]


# --------------------------------------------------------------------------
# doctest materialisation
# --------------------------------------------------------------------------

def _doctest_flags(prof: RepoProfile) -> list[str]:
    ini = util.read_text(Path(prof.root) / "pytest.ini", "") \
        or util.read_text(Path(prof.root) / "setup.cfg", "") \
        or util.read_text(Path(prof.root) / "tox.ini", "")
    m = re.search(r"doctest_optionflags\s*=\s*(.+)", ini)
    if m:
        flags = [f.strip() for f in re.split(r"[\s,]+", m.group(1)) if f.strip()]
        if flags:
            return flags
    return list(DEFAULT_DOCTEST_FLAGS)


def harvest_doctests(prof: RepoProfile) -> list[tuple[str, str, int]]:
    """(module, object qualname, number of examples) for every docstring with examples."""
    parser = doctest.DocTestParser()
    found: list[tuple[str, str, int]] = []
    for path in prof.source_files():
        try:
            src = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(src)
        except SyntaxError:
            continue
        module = _module_name(prof, path)
        if not module:
            continue

        def walk(node: ast.AST, prefix: str = "", module: str = module) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    doc = ast.get_docstring(child) or ""
                    name = f"{prefix}{child.name}"
                    if doc:
                        try:
                            examples = [e for e in parser.parse(doc)
                                        if isinstance(e, doctest.Example) and e.want]
                        except ValueError:
                            examples = []
                        if examples:
                            found.append((module, name, len(examples)))
                    if isinstance(child, ast.ClassDef):
                        walk(child, name + ".", module=module)

        mod_doc = ast.get_docstring(tree) or ""
        if mod_doc:
            try:
                ex = [e for e in parser.parse(mod_doc)
                      if isinstance(e, doctest.Example) and e.want]
            except ValueError:
                ex = []
            if ex:
                found.append((module, "", len(ex)))
        walk(tree)
    return found


DOCTEST_TEMPLATE = '''\
"""Generated by okfpipe (stage 1) -- doctest materialisation.

Each test below runs the executable examples from one docstring and fails with
the real expected-vs-actual diff. These are *authored* expectations lifted out
of the source, so a failure here means documented behaviour changed.

Regenerate with:  ./run.sh <repo> --stages hygiene
Do not edit by hand -- edits are overwritten.
"""

import doctest
import importlib

import pytest

OPTIONFLAGS = {flags}


def _run(module_name, object_name):
    module = importlib.import_module(module_name)
    target = module
    for part in object_name.split("."):
        if part:
            target = getattr(target, part)
    wanted = f"{{module_name}}.{{object_name}}" if object_name else module_name

    # recurse=False keeps each generated test scoped to exactly one docstring,
    # so a failure names the function whose documented behaviour changed rather
    # than the whole module.
    finder = doctest.DocTestFinder(exclude_empty=True, recurse=False)
    tests = [t for t in finder.find(target, wanted, module=module) if t.examples]
    if not tests:
        pytest.skip(f"no doctest examples found for {{wanted}}")

    runner = doctest.DocTestRunner(optionflags=OPTIONFLAGS, verbose=False)
    output = []
    for test in tests:
        runner.run(test, out=output.append, clear_globs=True)
    if runner.failures:
        raise AssertionError(
            f"{{runner.failures}} of {{runner.tries}} doctest example(s) failed "
            f"for {{wanted}}:\\n" + "".join(output))


{bodies}
'''


# --------------------------------------------------------------------------
# characterisation cases
# --------------------------------------------------------------------------

MAX_CASES_PER_TARGET = 4


def build_call_candidates(targets: list[Target], skip: set[tuple[str, str]],
                          limit: int) -> list[dict]:
    """Synthesise concrete calls, one varied argument at a time."""
    cands: list[dict] = []
    for t in targets:
        if (t.module, t.qualname) in skip:
            continue
        if t.is_method:
            continue  # constructing a receiver generically is unreliable
        required = [p for p in t.params if not p[2]]
        if len(required) > 3:
            continue
        if any(p[0] in ("args", "kwargs") for p in t.params):
            pass  # *args/**kwargs are fine; they just take the defaults

        base: list[str] = []
        for _name, key, _has_default in required:
            bank = VALUE_BANK.get(key) or VALUE_BANK["any"]
            base.append(bank[0])

        variants: list[list[str]] = [list(base)]
        for i, (_n, key, _d) in enumerate(required):
            bank = VALUE_BANK.get(key) or VALUE_BANK["any"]
            for alt in bank[1:3]:
                v = list(base)
                v[i] = alt
                variants.append(v)

        for n, argv in enumerate(variants[:MAX_CASES_PER_TARGET]):
            expr = f"{t.call_name}({', '.join(argv)})"
            cands.append({
                "id": f"{t.module}:{t.qualname}:{n}",
                "module": t.module,
                "expr": expr,
                "setup": "",
                "_target": t.qualname,
                "_file": t.source_file,
            })
        if len(cands) >= limit:
            break
    return cands[:limit]


def run_probe(image: str, repo: Path, workdir: Path, candidates: list[dict],
              hash_seed: str) -> dict[str, dict]:
    probe_src = Path(__file__).with_name("probe_runner.py")
    util.write_text(workdir / "probe_runner.py", probe_src.read_text(encoding="utf-8"))
    util.write_json(workdir / "candidates.json",
                    [{k: v for k, v in c.items() if not k.startswith("_")}
                     for c in candidates])
    res = dockerenv.run_in(
        image,
        ["python", "/probe/probe_runner.py", "/probe/candidates.json",
         f"/probe/results-{hash_seed}.json"],
        mounts=[dockerenv.Mount(repo, "/app", "ro"),
                dockerenv.Mount(workdir, "/probe")],
        workdir="/app", network="none", timeout=1200,
        env={"PYTHONHASHSEED": hash_seed, "PYTHONPATH": "/app"})
    if not res.ok:
        util.warn("probe run failed", seed=hash_seed,
                  tail=util.truncate(res.output, 0, 400).replace("\n", " | "))
    rows = util.read_json(workdir / f"results-{hash_seed}.json", []) or []
    return {r["id"]: r for r in rows}


def stable_results(a: dict[str, dict], b: dict[str, dict]) -> dict[str, dict]:
    """Keep only outcomes identical under two different hash seeds."""
    out = {}
    for key, ra in a.items():
        rb = b.get(key)
        if not rb or ra.get("kind") != rb.get("kind"):
            continue
        if ra.get("kind") == "value" and ra.get("repr") != rb.get("repr"):
            continue
        if ra.get("kind") == "exception" and ra.get("exc_type") != rb.get("exc_type"):
            continue
        out[key] = ra
    return out


CHARACTERISATION_HEADER = '''\
"""Generated by okfpipe (stage 1) -- characterisation tests.

WHAT THESE ARE: each assertion below was produced by calling the function in
the pinned container and writing down what it returned. They pin *current*
behaviour, so they detect change -- including a deliberately injected bug --
but they do not claim the current behaviour is correct. Treat a failure here as
"something changed, go look", not as "the new code is wrong".

Only cases that reproduced identically under two different PYTHONHASHSEEDs and
whose values round-trip through repr() were kept; everything else was dropped.

Regenerate with:  ./run.sh <repo> --stages hygiene
Do not edit by hand -- edits are overwritten.
"""

import pytest

{imports}

'''


def _py_name(text: str) -> str:
    return re.sub(r"\W+", "_", text).strip("_")


def emit_characterisation(module: str, rows: list[tuple[dict, dict]]) -> str:
    imports = f"import {module}"
    body: list[str] = []
    seen: set[str] = set()
    for cand, result in rows:
        target = cand["_target"]
        name = _py_name(f"test_{target}_{len(seen)}")
        while name in seen:
            name += "x"
        seen.add(name)
        expr = cand["expr"].replace(cand["_target"], f"{module}.{cand['_target']}", 1)
        body.append(f"def {name}():")
        body.append(f'    """{target}: {cand["expr"]}"""')
        if result["kind"] == "value":
            body.append(f"    assert {expr} == {result['repr']}")
        else:
            exc = result["exc_type"]
            body.append(f"    with pytest.raises({exc}):")
            body.append(f"        {expr}")
        body.append("")
        body.append("")
    return CHARACTERISATION_HEADER.format(imports=imports) + "\n".join(body)


def emit_doctest_module(module: str, entries: list[tuple[str, int]],
                        flags: list[str]) -> str:
    flag_expr = " | ".join(f"doctest.{f}" for f in flags) or "0"
    bodies: list[str] = []
    seen: set[str] = set()
    for obj, count in entries:
        label = _py_name(obj) or "module"
        name = f"test_doctest_{label}"
        while name in seen:
            name += "_x"
        seen.add(name)
        bodies.append(f"def {name}():")
        bodies.append(f'    """{count} documented example(s) for '
                      f'{module}{("." + obj) if obj else ""}."""')
        bodies.append(f'    _run("{module}", "{obj}")')
        bodies.append("")
        bodies.append("")
    return DOCTEST_TEMPLATE.format(flags=flag_expr, bodies="\n".join(bodies))


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------

def _run_generated(image: str, repo: Path, paths: list[str]) -> util.Result:
    return dockerenv.run_in(
        image,
        ["python", "-m", "pytest", *paths, "-p", "no:cacheprovider", "-q",
         "--timeout=120", "-rf"],
        mounts=[dockerenv.Mount(repo, "/app")],
        workdir="/app", network="none", timeout=1800)


_FAILED_RE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+?)::(\w+)", re.M)


def _quarantine(repo: Path, gen_dir: Path, output: str) -> int:
    """Delete generated test functions that did not pass. Returns how many."""
    failures: dict[str, set[str]] = {}
    for rel, func in _FAILED_RE.findall(output):
        failures.setdefault(Path(rel).name, set()).add(func)
    removed = 0
    for fname, funcs in failures.items():
        path = gen_dir / fname
        if not path.exists():
            continue
        src = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        lines = src.splitlines(keepends=True)
        drop: list[tuple[int, int]] = []
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in funcs:
                drop.append((node.lineno - 1, (node.end_lineno or node.lineno)))
                removed += 1
        for start, end in sorted(drop, reverse=True):
            del lines[start:end]
        util.write_text(path, "".join(lines))
    return removed


def generate(prof: RepoProfile, repo: Path, image: str, workdir: Path,
             coverage: dict, max_candidates: int = 220) -> TestGenResult:
    """Write generated tests into ``repo/tests_generated`` and verify they pass."""
    gen_dir = repo / GENERATED_DIR
    gen_dir.mkdir(parents=True, exist_ok=True)
    util.write_text(gen_dir / "__init__.py", "")
    logs: list[str] = []
    notes: list[str] = []

    # ---- 1. doctest materialisation ------------------------------------
    flags = _doctest_flags(prof)
    harvested = harvest_doctests(prof)
    by_module: dict[str, list[tuple[str, int]]] = {}
    for module, obj, count in harvested:
        by_module.setdefault(module, []).append((obj, count))

    doc_files: list[str] = []
    doc_cases = 0
    for module, entries in sorted(by_module.items()):
        entries.sort()
        fname = f"test_doctests_{_py_name(module)}.py"
        util.write_text(gen_dir / fname, emit_doctest_module(module, entries, flags))
        doc_files.append(f"{GENERATED_DIR}/{fname}")
        doc_cases += len(entries)
    util.info("doctests materialised", modules=len(by_module), cases=doc_cases)

    # ---- 2. characterisation probing ------------------------------------
    # Prefer targets in the least-covered files: that is where new assertions
    # buy the most, and it is this stage's stated job.
    targets = discover_targets(prof)

    def cov_of(rel: str) -> float:
        for key, info in coverage.items():
            if key.endswith(rel) or rel.endswith(key):
                return float(info.get("percent", 0.0))
        return 0.0

    targets.sort(key=lambda t: (cov_of(t.source_file), t.source_file, t.lineno))
    documented = {(m, o) for m, o, _ in harvested}
    candidates = build_call_candidates(targets, skip=documented, limit=max_candidates)
    util.info("probing call candidates", count=len(candidates),
              targets=len({c["_target"] for c in candidates}))

    char_files: list[str] = []
    value_cases = 0
    exc_cases = 0
    dropped = 0
    if candidates:
        a = run_probe(image, repo, workdir / "probe_a", candidates, "0")
        b = run_probe(image, repo, workdir / "probe_b", candidates, "12345")
        stable = stable_results(a, b)
        dropped += len(candidates) - len(stable)

        # Collect per target first, so a target can be judged as a whole.
        per_target: dict[tuple[str, str], list[tuple[dict, dict]]] = {}
        for cand in candidates:
            res = stable.get(cand["id"])
            if not res or res["kind"] == "error":
                continue
            if res["kind"] == "value" and not res.get("roundtrip"):
                dropped += 1
                continue
            if res["kind"] == "exception" and res.get("exc_module") != "builtins":
                # A project-defined exception needs an import we have not made;
                # builtins keep the generated file dependency-free.
                dropped += 1
                continue
            per_target.setdefault((cand["module"], cand["_target"]), []).append(
                (cand, res))

        per_module: dict[str, list[tuple[dict, dict]]] = {}
        for (module, target), rows in sorted(per_target.items()):
            kinds = {r["kind"] for _c, r in rows}
            excs = {r.get("exc_type") for _c, r in rows if r["kind"] == "exception"}

            # A target where every synthesised input raised the *same* builtin
            # exception tells us the value bank never produced a shape this
            # function accepts. "Passing a string where an object is expected
            # raises AttributeError" is true, and is not worth a test: it pins
            # no behaviour anyone relies on, and it is the kind of filler the
            # brief means by coverage theater.
            if kinds == {"exception"} and len(excs) == 1:
                dropped += len(rows)
                util.debug("dropped target: only uniform exceptions",
                           target=f"{module}.{target}", exc=next(iter(excs)))
                continue

            kept_exc = 0
            for cand, res in rows:
                if res["kind"] == "exception":
                    # Cap exception cases so a target's assertions are mostly
                    # about what it returns, not about how it rejects junk.
                    if kept_exc >= 2:
                        dropped += 1
                        continue
                    kept_exc += 1
                    exc_cases += 1
                else:
                    value_cases += 1
                per_module.setdefault(module, []).append((cand, res))

        for module, rows in sorted(per_module.items()):
            fname = f"test_behaviour_{_py_name(module)}.py"
            util.write_text(gen_dir / fname, emit_characterisation(module, rows))
            char_files.append(f"{GENERATED_DIR}/{fname}")

    all_files = sorted(doc_files + char_files)
    if not all_files:
        util.rmtree(gen_dir)
        return TestGenResult(ok=True, coverage_before=coverage,
                             notes=["no generated tests: no doctests and no "
                                    "synthesisable call targets"])

    # ---- 3. verify, quarantining anything that does not pass -------------
    green = False
    for attempt in range(3):
        res = _run_generated(image, repo, [GENERATED_DIR])
        logs.append(f"$ pytest {GENERATED_DIR} (attempt {attempt + 1})\n"
                    + util.truncate(res.output, 2500, 3500))
        if res.ok:
            green = True
            break
        removed = _quarantine(repo, gen_dir, res.output)
        dropped += removed
        util.warn("quarantined failing generated tests", count=removed,
                  attempt=attempt + 1)
        if removed == 0:
            break

    if not green:
        notes.append("generated tests could not be made green; the generated "
                     "directory was discarded rather than ship a red suite")
        util.rmtree(gen_dir)
        return TestGenResult(ok=False, notes=notes, log="\n\n".join(logs),
                             coverage_before=coverage, dropped=dropped)

    # Drop files left empty by quarantining.
    kept: list[str] = []
    total = 0
    for rel in all_files:
        p = repo / rel
        if not p.exists():
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError:
            p.unlink()
            continue
        n = sum(1 for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"))
        if not n:
            p.unlink()
            continue
        kept.append(rel)
        total += n

    notes.append(f"{doc_cases} doctest case(s) materialised from existing docstrings")
    notes.append(f"{value_cases} value and {exc_cases} exception characterisation "
                 "case(s) probed in-container")
    if dropped:
        notes.append(f"{dropped} candidate(s) dropped as non-deterministic, "
                     "unreprable or failing")

    util.info("test generation done", files=len(kept), cases=total, dropped=dropped)
    return TestGenResult(
        ok=True, files=kept, cases=total, doctest_cases=doc_cases,
        value_cases=value_cases, exception_cases=exc_cases, dropped=dropped,
        modules_targeted=sorted({m for m, _o, _c in harvested}
                                | {c["module"] for c in candidates}),
        coverage_before=coverage, notes=notes, log="\n\n".join(logs),
    )
