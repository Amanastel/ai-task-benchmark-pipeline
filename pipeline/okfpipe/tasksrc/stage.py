"""Stage 3 -- benchmark task generation.

Mines candidates, materialises task folders, validates every one, and keeps only
what survives. The composition rules from the brief are enforced here rather
than assumed: at least four history-derived tasks, at most four excision, at
most three net-new, and at least four distinct modules across the set.

The order is deliberate. History-derived tasks are tried first and given the
largest budget because they are the only source where provenance is a real
merged change rather than something the pipeline constructed. Excision and
net-new backfill toward the target count. If the target cannot be met with
*validated* tasks, the stage delivers fewer and says so -- a smaller validated
set is explicitly worth more than a padded one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .. import SCHEMA_VERSION, __version__, util
from ..hygiene import dockerenv
from ..hygiene.detect import RepoProfile, detect
from ..repo import RepoHandle, export_tree
from . import mine, netnew, runner, validate as validate_mod
from .model import TaskSpec, instruction_quality

# How many candidates to probe per source before giving up. Probing is the
# expensive part (two container runs each), so the budget is explicit.
HISTORY_PROBE_BUDGET = 26
EXCISION_PROBE_BUDGET = 14

MIN_HISTORY = 4
MAX_EXCISION = 4
MAX_NETNEW = 3
MIN_DISTINCT_MODULES = 4


@dataclass
class Built:
    spec: TaskSpec
    directory: Path
    verdict: dict = field(default_factory=dict)
    quality: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# materialisation
# --------------------------------------------------------------------------

def materialise_task(spec: TaskSpec, task_dir: Path, out_repo: Path,
                     handle: RepoHandle, workdir: Path) -> None:
    """Write input/, solution/, verifier/, task.json and goldenSolution.md."""
    util.rmtree(task_dir)
    task_dir.mkdir(parents=True, exist_ok=True)

    # -- input / solution trees ------------------------------------------
    if spec.input_base_sha:
        export_tree(handle, spec.input_base_sha, task_dir / "input")
    else:
        util.copytree(out_repo, task_dir / "input", keep_git=False)
    for rel, content in spec.input_files.items():
        util.write_text(task_dir / "input" / rel, content)

    if spec.solution_base_sha:
        export_tree(handle, spec.solution_base_sha, task_dir / "solution")
    else:
        util.copytree(out_repo, task_dir / "solution", keep_git=False)
    for rel, content in spec.solution_files.items():
        util.write_text(task_dir / "solution" / rel, content)

    # -- verifier ---------------------------------------------------------
    validate_mod.write_verifier(task_dir, spec.verifier_selection,
                                spec.verifier_overlay)

    command = "verifier/run.sh input    (and: verifier/run.sh solution)"
    util.write_json(task_dir / "task.json", spec.to_task_json(command))

    # -- golden solution --------------------------------------------------
    util.write_text(task_dir / "goldenSolution.md", _golden_md(spec))


def _golden_md(spec: TaskSpec) -> str:
    diff = spec.golden_diff.strip() or "(no source diff: see solution/ tree)"
    sel = "\n".join(f"  - `{s}`" for s in spec.verifier_selection[:25])
    prov = spec.provenance
    lines = [
        f"# Golden solution -- {spec.id}",
        "",
        f"**Task:** {spec.title}",
        f"**Source:** {prov.kind}",
    ]
    if prov.commit_sha:
        lines.append(f"**Commit:** `{prov.commit_sha}` "
                     f"(parent `{prov.parent_sha[:12]}`)")
    if prov.upstream_url:
        lines.append(f"**Upstream:** {prov.upstream_url}")
    if prov.excision_target:
        lines.append(f"**Target symbol:** `{prov.excision_target}`")
    if prov.detector:
        lines.append(f"**Detector:** `{prov.detector}`")
    lines += [
        "",
        "## Why this is the correct fix",
        "",
        spec.golden_rationale.strip(),
        "",
        "## Verified behaviours",
        "",
        f"The verifier grades {len(spec.verifier_selection)} case(s):",
        "",
        sel or "  (none)",
        "",
        "## Diff",
        "",
        "```diff",
        diff,
        "```",
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# excision probing
# --------------------------------------------------------------------------

def _probe_excision(prof: RepoProfile, out_repo: Path, sym: dict, image: str,
                    workdir: Path) -> tuple[list[str], str, str, str] | None:
    """Excise, run the covering tests, keep the ones that go red."""
    rel = sym["file"]
    source = (Path(prof.root) / rel).read_text(encoding="utf-8", errors="replace")
    made = mine.excise_function(source, sym["qualname"], sym["name"],
                                sym["line_start"])
    if not made:
        return None
    excised, removed = made

    covering = [runner.context_to_pytest_id(t) for t in sym.get("tests_covering", [])]
    covering = sorted({c for c in covering if c})
    if not covering:
        return None

    tree = workdir / f"exc-{util.slugify(sym['qualname'])}"
    runner.materialise(out_repo, tree, overrides={rel: excised})

    ok, _ = runner.import_check(image, tree, prof.import_names)
    if not ok:
        util.rmtree(tree)
        return None

    before = runner.run_pytest(image, tree, workdir, covering,
                               label=f"exc-before-{sym['name']}")
    util.rmtree(tree)
    if before.collected == 0 or before.errors:
        return None
    selection = runner.to_pytest_ids(before.failing())
    if not selection:
        return None
    return selection, excised, removed, source


# --------------------------------------------------------------------------
# net-new probing
# --------------------------------------------------------------------------

_CTOR_ARGS = ["'a', 'b'", "'a'", "1", "'a', 1", "", "'x', 'y', 'z'"]


def _probe_constructor(image: str, out_repo: Path, workdir: Path, module: str,
                       cls: str) -> tuple[str, str] | None:
    """Find a constructor call that yields equal instances, plus a differing one.

    Runs in the pinned image because only execution can confirm that
    ``C(a) == C(a)`` and ``C(a) != C(b)`` actually hold for this class.
    """
    probe = []
    for i, args in enumerate(_CTOR_ARGS):
        probe.append({"id": f"eq-{i}", "module": module,
                      "expr": f"({cls}({args}) == {cls}({args}), "
                              f"repr({cls}({args})))"})
    src = Path(__file__).resolve().parents[1] / "hygiene" / "probe_runner.py"
    pdir = workdir / "ctor-probe"
    pdir.mkdir(parents=True, exist_ok=True)
    util.write_text(pdir / "probe_runner.py", src.read_text(encoding="utf-8"))
    util.write_json(pdir / "candidates.json", probe)
    dockerenv.run_in(
        image,
        ["python", "/probe/probe_runner.py", "/probe/candidates.json",
         "/probe/results.json"],
        mounts=[dockerenv.Mount(out_repo, "/app", "ro"),
                dockerenv.Mount(pdir, "/probe")],
        workdir="/app", network="none", timeout=600,
        env={"PYTHONPATH": "/app"})
    rows = {r["id"]: r for r in (util.read_json(pdir / "results.json", []) or [])}

    working: list[str] = []
    for i, args in enumerate(_CTOR_ARGS):
        row = rows.get(f"eq-{i}")
        if row and row.get("kind") == "value" and row.get("repr", "").startswith("(True,"):
            working.append(f"{cls}({args})")
    if len(working) < 2:
        return None

    # Confirm the second expression is genuinely not equal to the first.
    check = [{"id": "ne", "module": module,
              "expr": f"({working[0]} != {working[1]})"}]
    util.write_json(pdir / "candidates.json", check)
    dockerenv.run_in(
        image,
        ["python", "/probe/probe_runner.py", "/probe/candidates.json",
         "/probe/results2.json"],
        mounts=[dockerenv.Mount(out_repo, "/app", "ro"),
                dockerenv.Mount(pdir, "/probe")],
        workdir="/app", network="none", timeout=600,
        env={"PYTHONPATH": "/app"})
    rows2 = {r["id"]: r for r in (util.read_json(pdir / "results2.json", []) or [])}
    if rows2.get("ne", {}).get("repr") != "True":
        return None
    return working[0], working[1]


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------

def run(out_repo: Path, handle: RepoHandle, tasks_out: Path, workdir: Path,
        target_count: int = 10, repeats: int = 3) -> dict:
    workdir.mkdir(parents=True, exist_ok=True)
    prof = detect(out_repo)
    okf = out_repo / ".okf"
    failures: list[str] = []
    rejected: list[dict] = []

    hygiene = util.read_json(okf / "hygiene.json", {}) or {}
    image = hygiene.get("image_tag") or f"okf-{util.slugify(prof.name)}:latest"
    if not dockerenv.image_exists(image):
        return {"ok": False, "failures": [f"image {image} missing; run stage 1 first"]}

    symbols = util.read_jsonl(okf / "symbols.jsonl")
    history_rows = util.read_jsonl(okf / "history.jsonl")
    coverage = (util.read_json(okf / "coverage.json", {}) or {}).get("files", {})
    if not history_rows:
        failures.append("no history available; history-derived tasks impossible")

    util.rmtree(tasks_out)
    tasks_out.mkdir(parents=True, exist_ok=True)

    built: list[Built] = []
    counters = {"history": 0, "excision": 0, "net-new": 0}
    used_modules: set[str] = set()

    def accept(spec: TaskSpec) -> bool:
        """Materialise, validate, and keep only if every gate passes."""
        task_dir = tasks_out / spec.id
        materialise_task(spec, task_dir, out_repo, handle, workdir)
        quality = instruction_quality(spec.instruction, spec.golden_diff)
        if not quality["ok"]:
            rejected.append({"id": spec.id, "source": spec.provenance.kind,
                             "reason": "instruction quality: "
                                       + "; ".join(quality["problems"]),
                             "detail": quality})
            util.rmtree(task_dir)
            return False
        verdict = validate_mod.validate(
            task_dir, image, prof.import_names, prof.test_paths,
            workdir / "validate" / spec.id, repeats=repeats)
        util.write_json(task_dir / "evidence" / "instruction_quality.json", quality)
        if not verdict.valid:
            rejected.append({"id": spec.id, "source": spec.provenance.kind,
                             "reason": "validation: " + "; ".join(verdict.notes),
                             "gates": {k: v.get("passed")
                                       for k, v in verdict.gates.items()}})
            # tasks/ ships only validated work, but the evidence that rejected a
            # candidate is worth keeping -- it is how the selection criteria get
            # audited, and how a near-miss gets diagnosed without a re-run.
            keep = workdir / "rejected" / spec.id
            util.rmtree(keep)
            keep.mkdir(parents=True, exist_ok=True)
            util.copytree(task_dir / "evidence", keep / "evidence")
            util.write_json(keep / "task.json",
                            spec.to_task_json("verifier/run.sh input"))
            util.rmtree(task_dir)
            return False
        built.append(Built(spec=spec, directory=task_dir,
                           verdict=verdict.to_json(), quality=quality))
        counters[spec.provenance.kind] += 1
        used_modules.update(spec.modules)
        util.info("task accepted", id=spec.id, kind=spec.provenance.kind,
                  difficulty=spec.difficulty)
        return True

    # ---- source fills, each resumable ----------------------------------
    # Every source keeps a cursor, so the quota pass and the backfill pass
    # continue through the same candidate list instead of re-probing work
    # already done -- probing is two container runs per candidate.
    history_pool = mine.history_candidates(prof, history_rows,
                                           limit=HISTORY_PROBE_BUDGET)
    excision_pool = mine.excision_candidates(symbols, coverage)
    detector_pool: list[tuple[str, dict | None]] = [
        ("eq_without_hash", mine.detect_eq_without_hash(prof)),
        ("missing_dunder_all", mine.detect_missing_dunder_all(prof, symbols)),
    ]
    cursors = {"history": 0, "excision": 0, "net-new": 0}

    def fill_history(want: int) -> None:
        while counters["history"] < want and cursors["history"] < len(history_pool) \
                and cursors["history"] < HISTORY_PROBE_BUDGET:
            row = history_pool[cursors["history"]]
            cursors["history"] += 1
            probe, overlay = mine.probe_history(handle, prof, row, image,
                                               workdir / "history")
            if probe.rejected:
                rejected.append({"id": f"history-{row['sha'][:10]}",
                                 "source": "history", "reason": probe.rejected,
                                 "subject": row["subject"][:120]})
                continue
            accept(mine.build_history_task(handle, prof, row, probe, overlay,
                                           task_id=f"hist-{row['sha'][:8]}"))

    def fill_excision(want: int) -> None:
        while counters["excision"] < min(want, MAX_EXCISION) \
                and cursors["excision"] < len(excision_pool) \
                and cursors["excision"] < EXCISION_PROBE_BUDGET:
            sym = excision_pool[cursors["excision"]]
            cursors["excision"] += 1
            found = _probe_excision(prof, out_repo, sym, image,
                                    workdir / "excision")
            if not found:
                rejected.append({"id": f"exc-{sym['qualname']}",
                                 "source": "excision",
                                 "reason": "excision did not produce failing "
                                           "covering tests, or the stub broke "
                                           "the import"})
                continue
            selection, excised, removed, _original = found
            accept(mine.build_excision_task(
                prof, sym, excised, removed, selection,
                task_id=f"exc-{util.slugify(sym['qualname'], 40)}",
                docstring=sym.get("docstring_summary", "")))

    def fill_netnew(want: int) -> None:
        while counters["net-new"] < min(want, MAX_NETNEW) \
                and cursors["net-new"] < len(detector_pool):
            name, hit = detector_pool[cursors["net-new"]]
            cursors["net-new"] += 1
            if not hit:
                rejected.append({"id": f"netnew-{name}", "source": "net-new",
                                 "reason": "detector found no gap in this "
                                           "repository"})
                continue
            result = None
            if name == "eq_without_hash":
                ctors = _probe_constructor(image, out_repo, workdir / "netnew",
                                           hit["module"], hit["class"])
                if not ctors:
                    rejected.append({"id": f"netnew-{name}", "source": "net-new",
                                     "reason": "could not construct two equal "
                                               "instances to specify the "
                                               "invariant"})
                    continue
                result = netnew.build_hash_task(
                    prof, hit, f"new-{util.slugify(hit['class'], 30)}-hashable",
                    ctors[0], ctors[1])
            elif name == "missing_dunder_all":
                result = netnew.build_dunder_all_task(
                    prof, hit, f"new-{util.slugify(hit['module'], 30)}-public-api")
            if not result:
                rejected.append({"id": f"netnew-{name}", "source": "net-new",
                                 "reason": "no mechanically derivable reference "
                                           "solution; declined rather than "
                                           "invent one"})
                continue
            accept(result[0])

    # Phase 1 -- reserved quotas, so a productive source is always represented.
    # Filling purely in priority order would let history and excision take all
    # ten slots and silently drop net-new, losing task-type diversity that the
    # brief's source table is asking for.
    netnew_quota = min(MAX_NETNEW, target_count // 5)
    excision_quota = min(MAX_EXCISION, target_count // 3)
    history_quota = max(MIN_HISTORY, target_count - netnew_quota - excision_quota)

    fill_history(history_quota)
    fill_excision(excision_quota)
    fill_netnew(netnew_quota)

    # Phase 2 -- backfill any shortfall, most-real source first.
    for filler, cap in ((fill_history, target_count),
                        (fill_excision, MAX_EXCISION),
                        (fill_netnew, MAX_NETNEW)):
        if len(built) >= target_count:
            break
        kind = {id(fill_history): "history", id(fill_excision): "excision",
                id(fill_netnew): "net-new"}[id(filler)]
        room = target_count - len(built)
        filler(min(cap, counters[kind] + room))

    if counters["history"] < MIN_HISTORY:
        failures.append(f"only {counters['history']} history-derived task(s) "
                        f"validated; the brief requires at least {MIN_HISTORY}")

    # ---- 4. compose the manifest ---------------------------------------
    built.sort(key=lambda b: ({"history": 0, "excision": 1, "net-new": 2}
                              [b.spec.provenance.kind], b.spec.id))
    if len(built) > target_count:
        # Trim the lowest-value surplus, never a history task.
        keep: list[Built] = [b for b in built if b.spec.provenance.kind == "history"]
        kept_ids = {id(b) for b in keep}
        for b in built:
            if id(b) not in kept_ids and len(keep) < target_count:
                keep.append(b)
                kept_ids.add(id(b))
        for b in built:
            if id(b) not in kept_ids:
                util.rmtree(b.directory)
        built = sorted(keep, key=lambda b: b.spec.id)

    modules = sorted({m for b in built for m in b.spec.modules})
    if len(modules) < MIN_DISTINCT_MODULES:
        failures.append(f"tasks span only {len(modules)} module(s); "
                        f"the brief requires at least {MIN_DISTINCT_MODULES}")
    if len(built) < target_count:
        failures.append(f"delivered {len(built)} validated task(s), "
                        f"target was {target_count}")

    manifest = {
        "schema": SCHEMA_VERSION,
        "generator": f"okfpipe {__version__}",
        "repo": {"name": prof.name, "remote": handle.remote_url,
                 "head_sha": handle.head_sha},
        "verifier_runner": "tasks/<id>/verifier/run.sh <input|solution>",
        "determinism_repeats": repeats,
        "counts": {
            "delivered": len(built),
            "by_source": {k: v for k, v in counters.items()},
            "by_difficulty": {
                d: sum(1 for b in built if b.spec.difficulty == d)
                for d in ("easy", "medium", "hard")},
            "distinct_modules": len(modules),
        },
        "modules": modules,
        "tasks": [
            {
                "id": b.spec.id,
                "title": b.spec.title,
                "source_type": b.spec.provenance.kind,
                "module": b.spec.modules[0] if b.spec.modules else "",
                "modules": sorted(set(b.spec.modules)),
                "difficulty": b.spec.difficulty,
                "provenance": b.spec.provenance.to_json(),
                "files_in_scope": sorted(b.spec.files_in_scope),
                "verifier_command": f"tasks/{b.spec.id}/verifier/run.sh input",
                "verifier_cases": len(b.spec.verifier_selection),
                "validation_status": "validated" if b.verdict.get("valid")
                                     else "failed",
                "validation_gates": {k: v.get("passed")
                                     for k, v in b.verdict.get("gates", {}).items()},
                "evidence": f"tasks/{b.spec.id}/evidence/",
            }
            for b in built
        ],
        "rejected_candidates": rejected[:120],
        "rejected_count": len(rejected),
        "failures": failures,
    }
    # tasks.json lives beside tasks/, at the delivery root, as the brief specifies.
    util.write_json(tasks_out.parent / "tasks.json", manifest)

    util.info("task generation done", delivered=len(built),
              history=counters["history"], excision=counters["excision"],
              netnew=counters["net-new"], modules=len(modules),
              rejected=len(rejected))

    return {
        "ok": not failures,
        "delivered": len(built),
        "by_source": counters,
        "distinct_modules": len(modules),
        "rejected": len(rejected),
        "failures": failures,
    }
