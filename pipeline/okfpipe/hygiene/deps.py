"""Stage 1a -- dependency pinning.

Resolution happens **inside a Linux container** rather than on the developer's
machine. That is the whole trick: environment markers, wheel selection and the
interpreter version all become properties of the pinned artifact instead of
properties of whoever ran the pipeline. A lock produced on Windows against
CPython 3.13 is not the lock the Dockerfile will install.

Outputs, written into the transformed repo:

    requirements.lock          runtime + test deps, exact versions + hashes
    requirements-runtime.lock  runtime deps only, exact versions + hashes

Both carry a header recording the resolution source, the interpreter and
the base image digest they were resolved against, so a lock can never be
read without knowing what produced it.

We resolve with ``uv`` (itself pinned) because it accepts ``setup.py``,
``setup.cfg``, ``pyproject.toml`` and ``requirements.in`` as inputs, which
covers every layout the detector can hand us.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .. import util
from . import dockerenv
from .detect import RepoProfile

# Pinned so the resolver itself cannot drift between runs.
UV_VERSION = "0.9.29"

# Tooling the transformed repo needs regardless of what the project declares.
# Floors only -- the resolver pins them to exact versions in the lock.
TOOL_FLOOR = [
    "pytest>=7.4",
    "pytest-timeout>=2.2",
    "coverage>=7.3",
]

LINT_FLOOR = ["ruff>=0.6"]


@dataclass
class PinResult:
    ok: bool
    lock_path: str = ""
    runtime_lock_path: str = ""
    packages: dict[str, str] = field(default_factory=dict)
    hashed: bool = True
    extras_used: list[str] = field(default_factory=list)
    source: str = ""
    base_image: str = ""
    log: str = ""
    notes: list[str] = field(default_factory=list)


def _compile_source(prof: RepoProfile) -> tuple[str, str]:
    """Pick what uv should resolve from. Returns (filename, kind)."""
    root = Path(prof.root)
    for name in ("pyproject.toml", "setup.py", "setup.cfg"):
        if (root / name).exists() and name in prof.manifest_files:
            # setup.cfg alone still needs a setup.py shim for uv to build metadata.
            if name == "setup.cfg" and not (root / "setup.py").exists():
                continue
            return name, "manifest"
    for cand in prof.requirement_files:
        if cand.endswith(".in"):
            return cand, "requirements"
    for cand in prof.requirement_files:
        if re.search(r"requirements(-|_)?(dev|test)?\.txt$", cand):
            return cand, "requirements"
    return "", "none"


def _extras_needed_by_tests(prof: RepoProfile) -> list[str]:
    """Extras whose distributions are actually imported by the test suite.

    Keeps an optional-dependency group out of the lock unless the tests really
    need it, which stops us pulling a heavy extra (say, a GPU stack) into an
    image whose only job is running unit tests.
    """
    imported: set[str] = set()
    for tf in prof.test_files():
        try:
            text = tf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in re.finditer(r"^\s*(?:import|from)\s+([A-Za-z_][\w.]*)", text, re.M):
            imported.add(m.group(1).split(".")[0].lower())
    imported -= {p.name.lower() for p in prof.packages}

    wanted: list[str] = []
    for extra, reqs in prof.extras.items():
        for req in reqs:
            dist = re.split(r"[<>=!~;\[\s]", req.strip(), maxsplit=1)[0].lower()
            # PyYAML -> yaml, python-dateutil -> dateutil: compare loosely.
            aliases = {dist, dist.replace("-", "_"), dist.replace("python-", "")}
            if aliases & imported:
                wanted.append(extra)
                break
    return sorted(set(wanted))


def _parse_lock(text: str) -> dict[str, str]:
    pkgs: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("--"):
            continue
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*([^\s;\\]+)", line)
        if m:
            pkgs[m.group(1).lower().replace("_", "-")] = m.group(2)
    return pkgs


_SCRIPT = r"""
set -eu
pip install --quiet --disable-pip-version-check "uv==__UV__" >/dev/null 2>&1
cp -a /src/. /build/
cd /build
__BODY__
"""


def _floor_requirements(prof: RepoProfile) -> list[str]:
    """Test/lint tooling plus the project's own build backends.

    Pinning the build backends is what lets the Dockerfile pass
    ``--no-build-isolation``: without it, ``pip install -e .`` would fetch
    whatever setuptools/hatchling happens to be current at image build time,
    which is an unpinned surface hiding inside an otherwise pinned image.
    """
    return (TOOL_FLOOR + LINT_FLOOR + list(prof.build_requires)
            + list(prof.extra_test_requirements))


def pin(prof: RepoProfile, out_repo: Path, base_image: str,
        workdir: Path) -> PinResult:
    """Resolve and write lockfiles into ``out_repo``."""
    source, kind = _compile_source(prof)
    if kind == "none":
        return PinResult(ok=False, source="", log="",
                         notes=["no manifest or requirements file to resolve from"])

    extras = sorted(set(prof.test_extras) | set(_extras_needed_by_tests(prof)))
    py = prof.python_version
    logs: list[str] = []
    notes: list[str] = []

    # An extra floor file lets us add pytest/coverage/ruff without editing the
    # repo's own manifest -- uv resolves both together so the pins stay consistent.
    tool_req = workdir / "tool-floor.in"
    util.write_text(tool_req, "\n".join(_floor_requirements(prof)) + "\n")

    def attempt(use_extras: list[str], hashes: bool) -> util.Result:
        ex = " ".join(f"--extra {e}" for e in use_extras)
        body = (
            f"uv pip compile --no-header --no-annotate --python-version {py} "
            f"{'--generate-hashes ' if hashes else ''}{ex} {source} /floor/tool-floor.in "
            f"-o /out/requirements.lock\n"
            f"uv pip compile --no-header --no-annotate --python-version {py} "
            f"{'--generate-hashes ' if hashes else ''}{source} "
            f"-o /out/requirements-runtime.lock\n"
        )
        script = _SCRIPT.replace("__UV__", UV_VERSION).replace("__BODY__", body)
        return dockerenv.run_in(
            base_image, ["sh", "-c", script],
            mounts=[dockerenv.Mount(prof.root, "/src", "ro"),
                    dockerenv.Mount(workdir / "build", "/build"),
                    dockerenv.Mount(workdir, "/floor", "ro"),
                    dockerenv.Mount(workdir / "out", "/out")],
            workdir="/build", network="bridge", timeout=1800)

    (workdir / "build").mkdir(parents=True, exist_ok=True)
    (workdir / "out").mkdir(parents=True, exist_ok=True)

    ladder: list[tuple[list[str], bool]] = [(extras, True)]
    if extras:
        ladder.append((extras, False))
        ladder.append(([], True))
    else:
        ladder.append(([], False))

    res = None
    used_extras: list[str] = []
    hashed = True
    for use_extras, hashes in ladder:
        util.info("resolving dependencies", source=source,
                  extras=",".join(use_extras) or "-", hashes=str(hashes))
        res = attempt(use_extras, hashes)
        logs.append(f"$ uv pip compile ({source}, extras={use_extras}, hashes={hashes})\n"
                    + util.truncate(res.output, 3000, 3000))
        if res.ok and (workdir / "out" / "requirements.lock").exists():
            used_extras, hashed = use_extras, hashes
            if use_extras != extras:
                notes.append(f"dropped extras {sorted(set(extras) - set(use_extras))}: "
                             "they did not resolve together")
            if not hashes:
                notes.append("hash pinning unavailable for this dependency set; "
                             "versions are still exact")
            break
    else:
        return PinResult(ok=False, source=source, log="\n\n".join(logs),
                         notes=notes + ["dependency resolution failed"])

    lock_text = util.read_text(workdir / "out" / "requirements.lock", "")
    runtime_text = util.read_text(workdir / "out" / "requirements-runtime.lock", "")

    header = (
        "# Generated by okfpipe (stage 1) -- do not edit by hand.\n"
        f"# source: {source}   extras: {','.join(used_extras) or 'none'}\n"
        f"# interpreter: python {py}   resolver: uv=={UV_VERSION}\n"
        f"# base image: {base_image}\n"
        "# Regenerate with: ./run.sh <repo> --stages hygiene\n"
    )
    util.write_text(out_repo / "requirements.lock", header + lock_text)
    if runtime_text.strip():
        util.write_text(out_repo / "requirements-runtime.lock", header + runtime_text)

    pkgs = _parse_lock(lock_text)
    util.info("pinned dependencies", count=len(pkgs), hashed=str(hashed))
    return PinResult(
        ok=True,
        lock_path="requirements.lock",
        runtime_lock_path="requirements-runtime.lock" if runtime_text.strip() else "",
        packages=pkgs, hashed=hashed, extras_used=used_extras, source=source,
        base_image=base_image, log="\n\n".join(logs), notes=notes,
    )
