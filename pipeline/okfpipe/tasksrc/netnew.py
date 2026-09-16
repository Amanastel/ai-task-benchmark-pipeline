"""Stage 3b -- net-new capability gaps.

A net-new task needs three things a detector cannot fake: a capability the repo
genuinely lacks, tests that define it, and a reference solution that is derived
rather than invented. The detectors below only fire when all three are
available, and the validation harness is the backstop -- a derived solution
that does not actually make the authored tests pass fails ``pass_after`` and
the task is dropped rather than shipped.

Each builder returns ``(TaskSpec, test_file_relpath, test_source)``.
"""

from __future__ import annotations

import ast
from pathlib import Path

from .. import util
from ..hygiene.detect import RepoProfile
from .model import Provenance, TaskSpec

TEST_DIR = "tests_task"


def test_node_ids(test_rel: str, test_src: str) -> list[str]:
    """pytest node ids for every test function in a generated test file.

    Without these the verifier's selection file is empty, and ``pytest`` with no
    targets runs the *entire* suite. That still produces a red-then-green
    result, so validation passes and the mistake is invisible -- but the task is
    then graded on the whole suite rather than on the behaviour it defines, and
    the manifest reports zero cases.
    """
    try:
        tree = ast.parse(test_src)
    except SyntaxError:
        return [test_rel]
    names = [n.name for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
             and n.name.startswith("test_")]
    return [f"{test_rel}::{name}" for name in names] or [test_rel]


def _module_of(prof: RepoProfile, rel: str) -> str:
    parts = Path(rel).with_suffix("").parts
    if prof.source_root and parts and parts[0] == prof.source_root:
        parts = parts[1:]
    return ".".join(p for p in parts if p != "__init__")


def _unified(before: str, after: str, path: str) -> str:
    import difflib
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}", n=3))


# --------------------------------------------------------------------------
# D1: __eq__ without __hash__
# --------------------------------------------------------------------------

def derive_hash_expression(eq_node: ast.FunctionDef) -> str | None:
    """The expression ``__hash__`` should hash, read off ``__eq__``.

    Mirroring the comparison is what keeps the two consistent. For
    ``self.path_t.__ops__ == other.path_t.__ops__`` the answer is
    ``self.path_t.__ops__`` -- deriving ``hash((self.path_t,))`` instead would
    hash a different thing than equality compares, which is the exact bug the
    task is supposed to fix.
    """
    def rooted_at_self(node: ast.AST) -> bool:
        cur = node
        while isinstance(cur, (ast.Attribute, ast.Subscript)):
            cur = cur.value
        return isinstance(cur, ast.Name) and cur.id == "self"

    exprs: list[str] = []
    for node in ast.walk(eq_node):
        if not isinstance(node, ast.Compare) or not node.ops:
            continue
        if not isinstance(node.ops[0], ast.Eq):
            continue
        for side in (node.left, *node.comparators):
            if rooted_at_self(side) and isinstance(side, (ast.Attribute, ast.Name)):
                try:
                    text = ast.unparse(side)
                except Exception:
                    continue
                if text not in exprs:
                    exprs.append(text)
    if not exprs:
        return None
    return exprs[0] if len(exprs) == 1 else "(" + ", ".join(exprs) + ")"


def build_hash_task(prof: RepoProfile, hit: dict, task_id: str,
                    ctor: str, other_ctor: str
                    ) -> tuple[TaskSpec, str, str] | None:
    rel = hit["file"]
    path = Path(prof.root) / rel
    source = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    cls = next((n for n in ast.walk(tree)
                if isinstance(n, ast.ClassDef) and n.name == hit["class"]
                and n.lineno == hit["line"]), None)
    if cls is None:
        return None
    eq = next((m for m in cls.body
               if isinstance(m, ast.FunctionDef) and m.name == "__eq__"), None)
    if eq is None:
        return None
    expr = derive_hash_expression(eq)
    if not expr:
        return None

    # Insert __hash__ immediately after __eq__, at the same indentation.
    lines = source.splitlines(keepends=True)
    insert_at = eq.end_lineno or eq.lineno
    indent = " " * eq.col_offset
    method = (
        f"\n"
        f"{indent}def __hash__(self):\n"
        f"{indent}    return hash({expr})\n"
    )
    patched = "".join(lines[:insert_at]) + method + "".join(lines[insert_at:])
    try:
        ast.parse(patched)
    except SyntaxError:
        return None

    module = _module_of(prof, rel)
    qual = f"{module}.{hit['class']}"
    test_rel = f"{TEST_DIR}/test_{util.slugify(qual).replace('-', '_')}_hashable.py"
    test_src = _hash_test_source(module, hit["class"], ctor, other_ctor)

    instruction = (
        f"`{qual}` defines equality but instances of it cannot be hashed, so "
        f"they cannot be used as dictionary keys, stored in a set, or "
        f"deduplicated. In Python, defining `__eq__` without `__hash__` makes a "
        f"class unhashable.\n"
        f"\n"
        f"Make `{hit['class']}` hashable, with a hash that is consistent with its "
        f"existing equality semantics. Specifically, the following must hold:\n"
        f"\n"
        f"  - hashing an instance succeeds rather than raising `TypeError`;\n"
        f"  - two instances that compare equal produce the same hash;\n"
        f"  - an instance can be used as a dict key and recovered with an equal "
        f"but distinct instance;\n"
        f"  - hashing the same instance twice returns the same value.\n"
        f"\n"
        f"Do not change what equality means -- the existing comparison behaviour "
        f"must be preserved exactly. Any hash function satisfying the equality "
        f"contract is accepted.\n"
        f"\n"
        f"Scope: `{rel}`.\n"
    )

    spec = TaskSpec(
        id=task_id,
        title=f"Make {hit['class']} hashable, consistently with its equality",
        instruction=instruction,
        provenance=Provenance(kind="net-new", detector="eq_without_hash",
                              excision_target=qual),
        difficulty="medium",
        difficulty_rationale=(
            "The symptom (a TypeError on hash) is easy to reach, but a correct "
            "answer requires noticing what the existing __eq__ actually compares "
            "and hashing the same thing. A plausible wrong answer -- hashing id(), "
            "or a different attribute than __eq__ uses -- passes the "
            "does-not-raise check and fails the equal-objects-hash-equally check, "
            "so the agent has to reason about the invariant rather than the error."),
        files_in_scope=[rel], modules=[module],
        input_files={}, solution_files={rel: patched},
        verifier_overlay={test_rel: test_src},
        verifier_selection=test_node_ids(test_rel, test_src),
        golden_diff=_unified(source, patched, rel),
        golden_rationale=(
            f"The reference solution adds `__hash__` to `{hit['class']}` returning "
            f"`hash({expr})`.\n\n"
            f"That expression is not arbitrary: it is the same expression the "
            f"class's existing `__eq__` compares. Deriving the hash from whatever "
            f"equality already uses is what guarantees the invariant "
            f"`a == b implies hash(a) == hash(b)`; hashing any other attribute "
            f"would produce a class that is hashable but broken as a dict key.\n\n"
            f"The verifier does not require this particular expression -- it tests "
            f"the invariant, so any consistent hash passes."),
    )
    return spec, test_rel, test_src


def _hash_test_source(module: str, cls: str, ctor: str, other_ctor: str) -> str:
    """Tests for the hash/equality invariant, around a verified constructor.

    ``ctor`` and ``other_ctor`` are expressions the pipeline already executed in
    the pinned image and confirmed produce, respectively, equal instances and a
    non-equal instance. Baking in verified literals keeps the test file free of
    fixtures and readable as a specification.
    """
    return f'''"""Net-new capability test generated by okfpipe (stage 3).

Specification for making {module}.{cls} hashable. These assertions describe the
hash/equality invariant, not any particular hash function, so any consistent
implementation passes.
"""

import pytest

from {module} import {cls}  # noqa: F401  (referenced by the constructor exprs)


def _equal_pair():
    """Two separately constructed instances that compare equal."""
    return {ctor}, {ctor}


def _different():
    return {other_ctor}


def test_instances_are_hashable():
    a, _b = _equal_pair()
    try:
        hash(a)
    except TypeError as exc:
        pytest.fail(
            "{cls} instances must be hashable so they can be used as dict keys "
            f"and set members, but hash() raised: {{exc}}"
        )


def test_equal_instances_hash_equally():
    a, b = _equal_pair()
    assert a == b, "precondition: the two constructed instances compare equal"
    assert hash(a) == hash(b), (
        "objects that compare equal must have equal hashes"
    )


def test_usable_as_dict_key():
    a, b = _equal_pair()
    mapping = {{a: "value"}}
    assert mapping[b] == "value", (
        "an equal instance must retrieve the same dict entry"
    )


def test_hash_is_stable_across_calls():
    a, _b = _equal_pair()
    assert hash(a) == hash(a), "hashing the same object twice must agree"


def test_set_deduplicates_equal_instances():
    a, b = _equal_pair()
    assert len({{a, b}}) == 1, "equal instances must collapse to one set element"


def test_unequal_instances_remain_distinct():
    a, _b = _equal_pair()
    other = _different()
    assert a != other, "precondition: the instances differ"
    assert len({{a, other}}) == 2, (
        "instances that are not equal must not be collapsed by the set"
    )
'''


# --------------------------------------------------------------------------
# D2: module without __all__
# --------------------------------------------------------------------------

def build_dunder_all_task(prof: RepoProfile, hit: dict, task_id: str
                          ) -> tuple[TaskSpec, str, str] | None:
    rel = hit["file"]
    path = Path(prof.root) / rel
    source = path.read_text(encoding="utf-8", errors="replace")
    names = sorted(hit["names"])
    module = hit["module"]

    entries = "\n".join(f'    "{n}",' for n in names)
    block = f'\n__all__ = [\n{entries}\n]\n'

    # Place __all__ after the imports so it reads like hand-written code.
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    last_import = 0
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            last_import = node.end_lineno or node.lineno
        elif last_import:
            break
    lines = source.splitlines(keepends=True)
    patched = "".join(lines[:last_import]) + block + "".join(lines[last_import:])
    try:
        ast.parse(patched)
    except SyntaxError:
        return None

    test_rel = f"{TEST_DIR}/test_{util.slugify(module).replace('-', '_')}_public_api.py"
    test_src = _dunder_all_test_source(module, names)

    instruction = (
        f"The module `{module}` exposes a substantial public API but never "
        f"declares it. Without an explicit export list, `from {module} import *` "
        f"pulls in every non-underscore name including incidental imports, and "
        f"tooling has no way to tell the supported surface from internals.\n"
        f"\n"
        f"Declare the module's public API explicitly. The declaration must:\n"
        f"\n"
        f"  - exist as a module-level list of strings;\n"
        f"  - name only objects the module actually provides;\n"
        f"  - contain no private names (nothing beginning with an underscore);\n"
        f"  - contain no duplicates;\n"
        f"  - include every public class and function the module itself defines.\n"
        f"\n"
        f"Scope: `{rel}`. Do not rename, move or delete anything -- this is an "
        f"additive declaration of what is already there.\n"
    )

    spec = TaskSpec(
        id=task_id,
        title=f"Declare the public API of {module}",
        instruction=instruction,
        provenance=Provenance(kind="net-new", detector="missing_dunder_all"),
        difficulty="easy",
        difficulty_rationale=(
            "Mechanically simple and the requirement is fully stated, but it is "
            "not free: the agent has to distinguish names the module defines from "
            "names it merely imports, which is the one place a careless answer "
            "goes wrong."),
        files_in_scope=[rel], modules=[module],
        input_files={}, solution_files={rel: patched},
        verifier_overlay={test_rel: test_src},
        verifier_selection=test_node_ids(test_rel, test_src),
        golden_diff=_unified(source, patched, rel),
        golden_rationale=(
            f"The reference solution adds a module-level `__all__` listing the "
            f"{len(names)} public classes and functions defined in `{rel}`.\n\n"
            f"The list was derived from the knowledge layer's symbol table: every "
            f"symbol whose `kind` is class or function, whose `qualname` belongs "
            f"to this module rather than an import, and whose name does not start "
            f"with an underscore.\n\n"
            f"The verifier checks the properties the declaration must have, not "
            f"the exact list ordering, so any correct superset-free declaration "
            f"passes."),
    )
    return spec, test_rel, test_src


def _dunder_all_test_source(module: str, names: list[str]) -> str:
    required = "\n".join(f'    "{n}",' for n in names)
    return f'''"""Net-new capability test generated by okfpipe (stage 3).

Defines the required behaviour for an explicit public-API declaration on
{module}. The assertions describe properties the declaration must have, so any
correct export list passes regardless of ordering or formatting.
"""

import importlib

MODULE_NAME = "{module}"

# Public classes and functions this module itself defines.
REQUIRED_NAMES = [
{required}
]


def _module():
    return importlib.import_module(MODULE_NAME)


def test_declares_an_export_list():
    module = _module()
    assert hasattr(module, "__all__"), (
        f"{{MODULE_NAME}} must declare __all__ to state its public API"
    )


def test_export_list_is_a_list_of_strings():
    module = _module()
    exported = getattr(module, "__all__", None)
    assert isinstance(exported, list), "__all__ must be a list"
    assert all(isinstance(name, str) for name in exported), (
        "every entry in __all__ must be a string"
    )


def test_every_exported_name_resolves():
    module = _module()
    exported = getattr(module, "__all__", []) or []
    missing = [name for name in exported if not hasattr(module, name)]
    assert not missing, f"__all__ names objects the module does not provide: {{missing}}"


def test_no_private_or_duplicate_names():
    module = _module()
    exported = getattr(module, "__all__", []) or []
    private = [name for name in exported if name.startswith("_")]
    assert not private, f"__all__ must not export private names: {{private}}"
    duplicates = sorted({{n for n in exported if exported.count(n) > 1}})
    assert not duplicates, f"__all__ contains duplicates: {{duplicates}}"


def test_covers_the_modules_own_public_api():
    module = _module()
    exported = set(getattr(module, "__all__", []) or [])
    missing = sorted(name for name in REQUIRED_NAMES if name not in exported)
    assert not missing, (
        f"__all__ omits public names defined in {{MODULE_NAME}}: {{missing}}"
    )
'''
