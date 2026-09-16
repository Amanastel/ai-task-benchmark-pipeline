"""Stage 3a -- mining task candidates.

Three miners, in descending order of how real the resulting task is:

**History.** A commit that changed behaviour *and* changed tests is a task
somebody already wrote and already verified: ``input/`` is the parent tree,
``solution/`` is the commit tree, and the verifier is the post-commit test
file restricted to the cases that actually flipped. That restriction is the
whole game -- shipping the entire test file would mean the verifier is mostly
tests that passed before the change, so a no-op "solution" would look nearly
green.

**Excision.** Take a function the suite already covers well, delete its body,
keep the signature and docstring. The contract is real, the tests are real, and
the reference answer is the code that was there.

**Net-new.** Capability gaps detected from the knowledge layer, where a correct
implementation is mechanically derivable so the reference solution is not
invention. Detectors that cannot derive one decline rather than guess.

Every miner ends at the same place: a ``TaskSpec`` with enough information to
materialise input/, solution/ and verifier/, and nothing that assumes this
particular repository.
"""

from __future__ import annotations

import ast
import difflib
import re
from dataclasses import dataclass
from pathlib import Path

from .. import util
from ..hygiene.detect import RepoProfile
from ..repo import RepoHandle, diff as git_diff, export_tree, show_file
from . import runner
from .model import (Provenance, TaskSpec, looks_like_code, sanitise_subject,
                    titlecase)

# A commit bigger than this is a refactor, not a task.
MAX_CHANGED_LINES = 400
MIN_CHANGED_LINES = 2
MAX_SOURCE_FILES = 3


@dataclass
class Probe:
    """What a candidate looked like when we actually ran it."""
    selection: list[str]
    before: runner.Outcome | None = None
    after: runner.Outcome | None = None
    rejected: str = ""


# --------------------------------------------------------------------------
# shared
# --------------------------------------------------------------------------

def _unified(before: str, after: str, path: str) -> str:
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}", n=3))


def _module_of(prof: RepoProfile, rel: str) -> str:
    parts = Path(rel).with_suffix("").parts
    if prof.source_root and parts and parts[0] == prof.source_root:
        parts = parts[1:]
    parts = [p for p in parts if p != "__init__"]
    return ".".join(parts)


def _discriminating(before: runner.Outcome, after: runner.Outcome) -> list[str]:
    """Tests that are red before and green after -- the ones that define the task."""
    return sorted(t for t in before.failing() if after.outcomes.get(t) == "passed")


# --------------------------------------------------------------------------
# history
# --------------------------------------------------------------------------

def _score_commit(row: dict) -> float:
    changed = row["insertions"] + row["deletions"]
    score = 0.0
    score += 3.0 if row["label"] == "fix" else (2.0 if row["label"] == "feature" else 0.0)
    score += 2.0 if row["touches_tests"] else -5.0
    n_src = len(row["source_files"])
    score += 2.0 if 1 <= n_src <= 2 else (0.5 if n_src == 3 else -2.0)
    # Prefer changes big enough to be interesting, small enough to be scoped.
    if 8 <= changed <= 120:
        score += 2.0
    elif changed < MIN_CHANGED_LINES or changed > MAX_CHANGED_LINES:
        score -= 6.0
    if row["is_merge"]:
        score -= 1.0
    return score


def history_candidates(prof: RepoProfile, history_rows: list[dict],
                       limit: int = 60) -> list[dict]:
    """Commits worth probing, best first, with module diversity enforced."""
    eligible = []
    for row in history_rows:
        if not row["parents"]:
            continue
        if row["label"] == "noise":
            continue
        src = [f for f in row["source_files"] if f.endswith(".py")]
        if not src or len(src) > MAX_SOURCE_FILES:
            continue
        if not row["touches_tests"]:
            continue
        changed = row["insertions"] + row["deletions"]
        if changed < MIN_CHANGED_LINES or changed > MAX_CHANGED_LINES:
            continue
        eligible.append({**row, "score": _score_commit(row), "primary": src[0]})

    eligible.sort(key=lambda r: (-r["score"], r["date"]), reverse=False)
    eligible.sort(key=lambda r: -r["score"])

    # Round-robin over primary file so ten variants of one module cannot win.
    buckets: dict[str, list[dict]] = {}
    for row in eligible:
        buckets.setdefault(row["primary"], []).append(row)
    ordered: list[dict] = []
    idx = 0
    while len(ordered) < limit and any(idx < len(v) for v in buckets.values()):
        for key in sorted(buckets):
            if idx < len(buckets[key]) and len(ordered) < limit:
                ordered.append(buckets[key][idx])
        idx += 1
    util.info("history candidates", eligible=len(eligible), shortlisted=len(ordered),
              modules=len(buckets))
    return ordered


def probe_history(handle: RepoHandle, prof: RepoProfile, row: dict, image: str,
                  workdir: Path) -> tuple[Probe, dict]:
    """Materialise parent and commit trees, run the changed tests against both."""
    sha, parent = row["sha"], row["parents"][0]
    tag = sha[:10]
    parent_tree = workdir / f"{tag}-input"
    commit_tree = workdir / f"{tag}-solution"

    try:
        export_tree(handle, parent, parent_tree)
        export_tree(handle, sha, commit_tree)
    except Exception as exc:
        return Probe([], rejected=f"could not export trees: {exc}"), {}

    # The verifier is the post-commit version of each test file the commit
    # touched. Overlay them onto the parent tree so the old code meets the new
    # expectations -- that is the fail-before state.
    overlay: dict[str, str] = {}
    for rel in row["test_files"]:
        if not rel.endswith(".py"):
            continue
        content = show_file(handle, sha, rel)
        if content is not None:
            overlay[rel] = content
    if not overlay:
        return Probe([], rejected="commit touched no readable test file"), {}

    for rel, content in overlay.items():
        util.write_text(parent_tree / rel, content)

    targets = sorted(overlay)
    before = runner.run_pytest(image, parent_tree, workdir, targets,
                               label=f"{tag}-before")
    after = runner.run_pytest(image, commit_tree, workdir, targets,
                              label=f"{tag}-after")

    probe = Probe(selection=[], before=before, after=after)
    if before.collected == 0:
        probe.rejected = "verifier collected nothing against the parent tree"
        return probe, overlay
    if before.errors and not before.failed:
        probe.rejected = ("parent tree produces collection/import errors, not "
                          "behavioural failures")
        return probe, overlay
    if not after.ok:
        probe.rejected = (f"verifier is not green on the commit tree "
                          f"({after.failed} failed, {after.errors} errors)")
        return probe, overlay

    selection = _discriminating(before, after)
    if not selection:
        probe.rejected = "no test flips from red to green across the commit"
        return probe, overlay
    probe.selection = selection
    return probe, overlay


def build_history_task(handle: RepoHandle, prof: RepoProfile, row: dict,
                       probe: Probe, overlay: dict[str, str],
                       task_id: str) -> TaskSpec:
    src_files = [f for f in row["source_files"] if f.endswith(".py")]
    golden = git_diff(handle, row["parents"][0], row["sha"], src_files)
    modules = [_module_of(prof, f) for f in src_files]
    subject = sanitise_subject(row["subject"])

    difficulty, rationale = _rate_history(row, probe, src_files)
    selection = runner.to_pytest_ids(probe.selection)
    symptoms = _symptoms(probe.before.messages if probe.before else {}, selection)
    docstrings = _test_docstrings(overlay, selection)

    instruction = _history_instruction(
        subject=subject, modules=modules, files=src_files,
        label=row["label"], symptoms=symptoms, docstrings=docstrings,
        selection_count=len(selection))

    pr = re.search(r"#(\d+)", row["subject"])
    prov = Provenance(
        kind="history", commit_sha=row["sha"], parent_sha=row["parents"][0],
        merge_pr=pr.group(0) if pr else "",
        commit_subject=row["subject"].strip(), commit_date=row["date"],
        upstream_url=(handle.remote_url.replace(".git", "") + "/commit/" + row["sha"])
        if handle.remote_url.startswith("http") else "")

    return TaskSpec(
        id=task_id, title=_title_for(modules[0] if modules else "", subject),
        instruction=instruction,
        provenance=prov, difficulty=difficulty, difficulty_rationale=rationale,
        files_in_scope=src_files, modules=modules,
        verifier_selection=selection,
        verifier_overlay=overlay,
        input_base_sha=row["parents"][0], solution_base_sha=row["sha"],
        golden_diff=golden,
        golden_rationale=_history_rationale(row, probe, src_files),
    )


def _behaviour_phrases(selection: list[str]) -> list[str]:
    """Readable behaviour names from test ids, without quoting test code."""
    out: list[str] = []
    for node in selection[:8]:
        name = node.split("::")[-1]
        name = re.sub(r"^test_", "", name)
        name = re.sub(r"\[.*\]$", "", name)
        out.append(name.replace("_", " ").strip())
    return out


# Lines of a pytest failure that state the observed symptom, rather than
# echoing the assertion's source or framing the traceback.
_SYMPTOM = re.compile(
    r"^(?:E\s+)?((?:[\w.]*(?:Error|Exception|Warning)\b.*)"
    r"|(?:Failed:.*)|(?:DID NOT RAISE.*)|(?:assert .*))$")


# Content that differs between two runs of the same failing test. It has to go
# before a symptom is written into an instruction, or the *task description*
# becomes non-deterministic -- two pipeline runs would produce byte-different
# task.json files for the same commit, which undermines every reproducibility
# claim the delivery makes.
_VOLATILE_SUBS = (
    (re.compile(r"0x[0-9a-fA-F]{4,}"), "0x<addr>"),
    (re.compile(r"\b[0-9a-f]{10,}\b"), "<addr>"),
    (re.compile(r"(/tmp|/var/folders)/\S+"), "<tmpdir>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}\S*"), "<timestamp>"),
    (re.compile(r"\bin \d+\.\d+s\b"), "in <duration>"),
)


def _scrub(text: str) -> str:
    for pattern, replacement in _VOLATILE_SUBS:
        text = pattern.sub(replacement, text)
    return text


def _symptoms(messages: dict[str, str], selection: list[str],
              limit: int = 4) -> list[tuple[str, str]]:
    """(test id, one-line observed symptom) from the fail-before run.

    This is the part that makes a history-derived instruction self-contained.
    A commit subject says what the maintainer was thinking; the failure says
    what an engineer would actually see, which is the same thing a bug report
    would carry -- and it describes the symptom, never the patch.
    """
    out: list[tuple[str, str]] = []
    wanted = {runner.node_to_pytest_id(k): k for k in messages}
    for node in selection[:limit]:
        raw = messages.get(wanted.get(node, ""), "") or messages.get(node, "")
        line = ""
        for candidate in reversed(raw.strip().splitlines()):
            m = _SYMPTOM.match(candidate.strip())
            if m:
                line = _scrub(re.sub(r"\s+", " ", m.group(1)).strip())[:220]
                break
        out.append((node, line or "the expected behaviour was not observed"))
    return out


def _test_docstrings(overlay: dict[str, str],
                     selection: list[str]) -> dict[str, str]:
    """First docstring line of each graded test, when its author wrote one."""
    wanted: dict[str, str] = {}
    by_file: dict[str, set[str]] = {}
    for node in selection:
        path, _, rest = node.partition("::")
        by_file.setdefault(path, set()).add(rest.split("::")[-1].split("[")[0])
    for path, names in by_file.items():
        source = overlay.get(path)
        if not source:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and fn.name in names:
                doc = (ast.get_docstring(fn) or "").strip()
                if doc:
                    wanted[fn.name] = re.sub(r"\s+", " ", doc.split("\n\n")[0])[:240]
    return wanted


_TEST_NOISE = re.compile(
    r"\b(more|add(ing)?|extra|additional)?\s*(unit\s+)?tests?\b[,;:]?\s*", re.I)


def _title_for(module: str, subject: str) -> str:
    """A task title that names the behaviour, not the commit's housekeeping."""
    cleaned = _TEST_NOISE.sub("", subject).strip(" ,;:-")
    cleaned = re.sub(r"^(and|for|in|of)\b\s*", "", cleaned, flags=re.I).strip()
    if not cleaned or looks_like_code(cleaned) and len(cleaned) < 8:
        cleaned = subject.strip()
    body = titlecase(cleaned, limit=max(24, 88 - len(module) - 2))
    return f"{module}: {body}" if module else body


def _history_instruction(subject: str, modules: list[str], files: list[str],
                         label: str, symptoms: list[tuple[str, str]],
                         docstrings: dict[str, str], selection_count: int) -> str:
    """A bug report, not a patch description.

    Built from three sources, none of which is the diff: the maintainer's own
    summary of intent, the docstrings the test author wrote, and the symptom
    actually observed when the verifier runs against this tree. That is what
    an engineer picking up the ticket would have.
    """
    what = "a defect" if label == "fix" else "an incomplete behaviour"
    mod_list = ", ".join(f"`{m}`" for m in modules) or "the package"
    file_list = ", ".join(f"`{f}`" for f in files)

    lines: list[str] = []
    for node, symptom in symptoms:
        name = node.split("::")[-1].split("[")[0]
        doc = docstrings.get(name, "")
        label_text = name.replace("test_", "").replace("_", " ").strip()
        lines.append(f"  - **{label_text}** - observed: `{symptom}`")
        if doc:
            lines.append(f"    Intended behaviour: {doc}")
    observed = "\n".join(lines)

    more = ""
    if selection_count > len(symptoms):
        more = (f"\n  - …and {selection_count - len(symptoms)} further case(s) "
                f"failing in the same area.\n")

    return (
        f"This repository has {what} in {mod_list}.\n"
        f"\n"
        f"Summary of the problem, as reported: {subject}.\n"
        f"\n"
        f"Running the graded checks against this tree currently produces "
        f"{selection_count} failure(s):\n"
        f"\n"
        f"{observed}\n{more}"
        f"\n"
        f"Change the implementation so that these behaviours hold. Work out the "
        f"intended semantics from the surrounding code and from how the affected "
        f"functions are used elsewhere in the package.\n"
        f"\n"
        f"Scope: the change belongs in {file_list}. You may read anything in the "
        f"repository, but the graded test files are replaced with a reference "
        f"copy before grading, so editing them has no effect.\n"
        f"\n"
        f"Success is measured only by observable behaviour: any implementation "
        f"that makes the failing cases pass, without breaking any test that "
        f"currently passes, is accepted. No particular structure, helper name or "
        f"code shape is required.\n"
    )


def _rate_history(row: dict, probe: Probe, src_files: list[str]) -> tuple[str, str]:
    changed = row["insertions"] + row["deletions"]
    n_files = len(src_files)
    n_tests = len(probe.selection)

    if n_files >= 2 and changed >= 40:
        return "hard", (
            f"Coordinated edit across {n_files} files totalling {changed} changed "
            f"lines, with {n_tests} behaviours to satisfy at once. An agent has to "
            f"hold the call path in mind rather than patch one function, and a fix "
            f"that satisfies one file's tests can regress the other's.")
    if changed >= 25 or n_tests >= 3:
        return "medium", (
            f"A single-module change of {changed} lines constrained by {n_tests} "
            f"distinct behaviours. The contract is discoverable from the "
            f"surrounding code, but the agent must infer which of several "
            f"plausible readings the tests encode rather than guessing from the "
            f"symptom alone.")
    return "easy", (
        f"A localised change of {changed} lines in one file with {n_tests} "
        f"behaviour(s) to satisfy. The failing case points almost directly at the "
        f"responsible branch, so the difficulty is reading comprehension rather "
        f"than design.")


def _history_rationale(row: dict, probe: Probe, src_files: list[str]) -> str:
    return (
        f"The reference solution is the upstream change {row['sha'][:12]} "
        f"(\"{row['subject'].strip()}\"), authored on {row['date']}.\n\n"
        f"It is correct because the project's own maintainers shipped it as the "
        f"fix for this behaviour, and because the {len(probe.selection)} "
        f"verifier case(s) that fail against the parent commit all pass against "
        f"it. Those cases were selected mechanically: the post-commit test files "
        f"were run against both trees and only the tests whose outcome flipped "
        f"from failing to passing were kept, so the verifier measures this change "
        f"and not the pre-existing behaviour around it.\n\n"
        f"Files changed: {', '.join(src_files)}."
    )


# --------------------------------------------------------------------------
# excision
# --------------------------------------------------------------------------

STUB_MESSAGE = ("This function's body was removed for a benchmark task. "
                "Implement it so that it satisfies the contract in its "
                "signature and docstring.")


def excise_function(source: str, qualname: str, name: str,
                    line_start: int) -> tuple[str, str] | None:
    """Return (excised source, removed body) or None if it cannot be done safely."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    target = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == name and node.lineno == line_start:
            target = node
            break
    if target is None or not target.body:
        return None

    body = list(target.body)
    doc_offset = 0
    if isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        doc_offset = 1
    real_body = body[doc_offset:]
    if not real_body:
        return None            # docstring-only function; nothing to excise

    lines = source.splitlines(keepends=True)
    start = real_body[0].lineno - 1
    end = max((s.end_lineno or s.lineno) for s in real_body)
    removed = "".join(lines[start:end])

    indent = " " * real_body[0].col_offset
    stub = (f'{indent}raise NotImplementedError(\n'
            f'{indent}    "{qualname}: {STUB_MESSAGE}"\n'
            f'{indent})\n')
    excised = "".join(lines[:start]) + stub + "".join(lines[end:])

    try:                       # the stub must still parse
        ast.parse(excised)
    except SyntaxError:
        return None
    return excised, removed


def excision_candidates(symbols: list[dict], coverage: dict,
                        min_tests: int = 3, min_loc: int = 6,
                        min_complexity: int = 3) -> list[dict]:
    """Well-covered, non-trivial, documented public functions."""
    out = []
    for sym in symbols:
        if sym["kind"] not in ("function", "method"):
            continue
        if not sym.get("is_public") or sym["name"].startswith("_"):
            continue
        if sym.get("test_count", 0) < min_tests:
            continue
        if sym.get("loc", 0) < min_loc or sym.get("complexity", 0) < min_complexity:
            continue
        if not sym.get("docstring_summary"):
            continue
        if any(d in ("property", "cached_property", "staticmethod")
               for d in sym.get("decorators", [])):
            continue
        out.append(sym)
    # Most-covered and most-complex first: those have the sharpest contracts.
    out.sort(key=lambda s: (-s["test_count"], -s["complexity"], s["qualname"]))
    util.info("excision candidates", count=len(out))
    return out


def build_excision_task(prof: RepoProfile, sym: dict, excised: str, removed: str,
                        selection: list[str], task_id: str,
                        docstring: str) -> TaskSpec:
    rel = sym["file"]
    original = (Path(prof.root) / rel).read_text(encoding="utf-8", errors="replace")
    golden = _unified(excised, original, rel)
    module = _module_of(prof, rel)

    body_lines = len([ln for ln in removed.splitlines() if ln.strip()])
    complexity = sym.get("complexity", 1)
    if complexity >= 8 or body_lines >= 25:
        difficulty = "hard"
        rationale = (
            f"The removed body is {body_lines} lines with cyclomatic complexity "
            f"{complexity}, so the contract has many branches the docstring only "
            f"summarises. The agent must recover edge-case behaviour -- error "
            f"paths and boundary handling -- that no single sentence states.")
    elif complexity >= 5 or body_lines >= 12:
        difficulty = "medium"
        rationale = (
            f"A {body_lines}-line body with complexity {complexity}. The happy "
            f"path follows from the signature, but the {len(selection)} covering "
            f"tests pin down branch behaviour that has to be inferred from how "
            f"the rest of the module uses this function.")
    else:
        difficulty = "easy"
        rationale = (
            f"A short body ({body_lines} lines, complexity {complexity}) with a "
            f"docstring that states the contract directly and {len(selection)} "
            f"tests confirming it.")

    instruction = (
        f"The body of `{sym['qualname']}` in `{rel}` has been removed and "
        f"replaced with a stub that raises `NotImplementedError`. Its signature "
        f"and docstring are intact and are the authoritative statement of what it "
        f"must do.\n"
        f"\n"
        f"Contract, as documented:\n"
        f"    {docstring.strip().splitlines()[0] if docstring.strip() else '(see docstring)'}\n"
        f"\n"
        f"Implement the function so that it fulfils that contract. The rest of "
        f"the module is unchanged and shows how the function is called and what "
        f"its callers expect back.\n"
        f"\n"
        f"Scope: only `{rel}` needs to change. The verifier runs "
        f"{len(selection)} existing test(s) that already exercise this function; "
        f"they were written against the original implementation and are unmodified. "
        f"Any implementation satisfying them is accepted -- matching the original "
        f"line for line is not required.\n"
    )

    return TaskSpec(
        id=task_id, title=titlecase(f"Implement {sym['qualname']}"),
        instruction=instruction,
        provenance=Provenance(kind="excision", excision_target=sym["qualname"],
                              detector=f"{rel}:{sym['line_start']}"),
        difficulty=difficulty, difficulty_rationale=rationale,
        files_in_scope=[rel], modules=[module],
        verifier_selection=selection,
        input_files={rel: excised},
        solution_files={},
        golden_diff=golden,
        golden_rationale=(
            f"The reference answer is the implementation that was removed: the "
            f"code shipping in {rel} at the delivered commit. It is correct by "
            f"construction -- it is the implementation the repository's own "
            f"{len(selection)} covering tests were written against, and those "
            f"tests pass against it and fail against the stub.\n\n"
            f"Note that the verifier accepts any behaviourally equivalent "
            f"implementation; the diff below shows the original only as the "
            f"reference point."),
    )


# --------------------------------------------------------------------------
# net-new capability gaps
# --------------------------------------------------------------------------

def detect_missing_version_export(prof: RepoProfile) -> dict | None:
    """A package that ships a version but does not expose ``pkg.__version__``."""
    for pkg in prof.packages:
        if not pkg.is_package:
            continue
        init = Path(prof.root) / pkg.path / "__init__.py"
        if not init.exists():
            continue
        text = init.read_text(encoding="utf-8", errors="replace")
        if re.search(r"^\s*__version__\s*=", text, re.M):
            continue
        # Is a version available anywhere to re-export?
        for cand in ("_version.py", "version.py", "__about__.py"):
            vp = Path(prof.root) / pkg.path / cand
            if vp.exists() and re.search(r"^\s*__version__\s*=",
                                         vp.read_text(encoding="utf-8",
                                                      errors="replace"), re.M):
                return {"package": pkg.name, "init": prof.rel(init),
                        "version_module": f"{pkg.name}.{cand[:-3]}",
                        "source": "module"}
    return None


def detect_missing_dunder_all(prof: RepoProfile, symbols: list[dict]) -> dict | None:
    """A public module with a real public surface but no ``__all__``."""
    by_module: dict[str, list[dict]] = {}
    for sym in symbols:
        if sym["kind"] in ("function", "class") and sym.get("is_public"):
            module = sym["qualname"].rsplit(".", 1)[0]
            by_module.setdefault(module, []).append(sym)

    for module, syms in sorted(by_module.items(), key=lambda kv: -len(kv[1])):
        if len(syms) < 5:
            continue
        rel = syms[0]["file"]
        text = (Path(prof.root) / rel).read_text(encoding="utf-8", errors="replace")
        if re.search(r"^\s*__all__\s*=", text, re.M):
            continue
        names = sorted({s["name"] for s in syms
                        if "." not in s["qualname"].split(module + ".")[-1]})
        if len(names) < 5:
            continue
        return {"module": module, "file": rel, "names": names}
    return None


def detect_eq_without_hash(prof: RepoProfile) -> dict | None:
    """A class defining ``__eq__`` but not ``__hash__``, so instances are unhashable.

    Only reported when the attributes compared by ``__eq__`` can be read off the
    AST, because otherwise there is no mechanically derivable reference answer
    and the "reference solution" would be invention.
    """
    for path in prof.source_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or node.name.startswith("_"):
                continue
            methods = {m.name for m in node.body
                       if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))}
            if "__eq__" not in methods or "__hash__" in methods:
                continue
            eq = next(m for m in node.body
                      if isinstance(m, ast.FunctionDef) and m.name == "__eq__")
            attrs = sorted({
                n.attr for n in ast.walk(eq)
                if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                and n.value.id in ("self", "other") and not n.attr.startswith("__")
            })
            if not attrs:
                continue
            return {"class": node.name, "file": prof.rel(path),
                    "line": node.lineno, "attrs": attrs,
                    "module": _module_of(prof, prof.rel(path))}
    return None
