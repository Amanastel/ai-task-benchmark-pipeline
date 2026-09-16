"""Discover everything repo-specific about a Python project, statically.

This module is the only place in the pipeline allowed to know *anything* about
a particular repository's layout, and even here it knows nothing in advance --
every fact is read off the checkout at runtime. If the pipeline mishandles an
unseen repo, the fix belongs here, not in a caller.

Deliberately static: we never exec a repo's ``setup.py``. Running arbitrary
code from a repo we are about to containerize is both a security problem and a
source of non-determinism, so ``setup()`` keyword arguments are lifted out of
the AST instead. When static lifting cannot resolve the dependency list,
``deps.py`` falls back to asking the packaging backend for real metadata inside
the container, which is correct for every build backend.
"""

from __future__ import annotations

import ast
import configparser
import io
import re
import tokenize
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .. import util

try:  # 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - 3.10 and older
    tomllib = None  # type: ignore[assignment]


# Directories that never contain the project's own importable source.
EXCLUDED_DIRS = {
    ".git", ".hg", ".svn", ".tox", ".nox", ".venv", "venv", "env", "__pycache__",
    "build", "dist", "docs", "doc", "examples", "example", "scripts", "bin",
    "node_modules", ".idea", ".vscode", ".eggs", ".mypy_cache", ".ruff_cache",
    ".pytest_cache", "site-packages", "htmlcov", "benchmarks", "bench",
}

# "tests_generated" is this pipeline's own output directory; listing it here
# stops a second run from mistaking the tests we wrote for first-party source.
TEST_DIR_NAMES = {"test", "tests", "testing", "unit_tests", "spec",
                  "tests_generated"}

# Ordered: the first interpreter that satisfies the project's floor is used.
SUPPORTED_PYTHONS = ("3.11", "3.12", "3.10", "3.13", "3.9")

DEFAULT_PYTHON = "3.11"


@dataclass
class Package:
    """One importable top-level package or module belonging to the project."""

    name: str            # import name, e.g. "glom"
    path: str            # repo-relative posix path, e.g. "glom"
    is_package: bool     # directory with __init__.py vs single .py module
    source_root: str     # repo-relative dir to put on sys.path ("" or "src")
    module_count: int = 0


@dataclass
class RepoProfile:
    """Everything stage 1/2/3 need to know about a checkout."""

    root: str
    name: str
    ecosystem: str = "python"

    packages: list[Package] = field(default_factory=list)
    source_root: str = ""
    test_paths: list[str] = field(default_factory=list)
    test_framework: str = "pytest"

    build_backend: str = "unknown"      # setuptools | poetry | hatchling | flit | pdm
    build_requires: list[str] = field(default_factory=list)
    manifest_files: list[str] = field(default_factory=list)
    requirement_files: list[str] = field(default_factory=list)

    runtime_requirements: list[str] = field(default_factory=list)
    extras: dict[str, list[str]] = field(default_factory=dict)
    test_extras: list[str] = field(default_factory=list)
    # Test dependencies that are NOT installable as a pip extra -- poetry
    # groups and PEP 735 dependency-groups. They are resolved alongside the
    # project instead of through --extra.
    extra_test_requirements: list[str] = field(default_factory=list)
    static_deps_resolved: bool = False   # False -> ask the backend in-container

    python_requires: str = ""
    python_version: str = DEFAULT_PYTHON
    console_scripts: dict[str, str] = field(default_factory=dict)

    has_dockerfile: bool = False
    has_lock: bool = False
    lint_configs: list[str] = field(default_factory=list)
    system_packages: list[str] = field(default_factory=list)

    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["packages"] = [asdict(p) if not isinstance(p, dict) else p for p in self.packages]
        return d

    # -- convenience ------------------------------------------------------
    @property
    def import_names(self) -> list[str]:
        return [p.name for p in self.packages]

    def source_files(self) -> list[Path]:
        """Every first-party .py file, excluding tests, sorted for determinism."""
        out: list[Path] = []
        root = Path(self.root)
        for pkg in self.packages:
            base = root / pkg.path
            if base.is_file():
                out.append(base)
                continue
            for f in base.rglob("*.py"):
                if self.is_test_file(f) or any(part in EXCLUDED_DIRS for part in f.parts):
                    continue
                out.append(f)
        return sorted(set(out))

    def test_files(self) -> list[Path]:
        root = Path(self.root)
        out: list[Path] = []
        for tp in self.test_paths:
            base = root / tp
            if base.is_dir():
                out.extend(p for p in base.rglob("*.py") if p.name != "__init__.py")
            elif base.is_file():
                out.append(base)
        return sorted(set(out))

    def is_test_file(self, path: Path) -> bool:
        parts = set(Path(path).parts)
        if parts & TEST_DIR_NAMES:
            return True
        n = Path(path).name
        return n.startswith("test_") or n.endswith("_test.py") or n == "conftest.py"

    def rel(self, path: Path | str) -> str:
        return Path(path).relative_to(self.root).as_posix()


# --------------------------------------------------------------------------
# static setup.py lifting
# --------------------------------------------------------------------------

def _literal(node: ast.AST) -> Any:
    """ast.literal_eval that tolerates the parts it cannot resolve."""
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError, TypeError):
        return None


def _setup_kwargs(source: str) -> tuple[dict[str, Any], bool]:
    """Lift the keyword arguments of the ``setup(...)`` call out of a setup.py.

    Returns (kwargs, fully_static). ``fully_static`` is False when any keyword
    we care about was a name/call we refused to evaluate, which tells deps.py to
    fall back to real backend metadata instead of trusting a partial answer.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}, False

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = getattr(fn, "id", None) or getattr(fn, "attr", None)
        if name != "setup":
            continue
        kwargs: dict[str, Any] = {}
        static = True
        for kw in node.keywords:
            if kw.arg is None:          # setup(**config) -- unresolvable
                static = False
                continue
            value = _literal(kw.value)
            if value is None and not isinstance(kw.value, ast.Constant):
                if kw.arg in ("install_requires", "extras_require", "entry_points",
                              "python_requires", "packages"):
                    static = False
                continue
            kwargs[kw.arg] = value
        return kwargs, static
    return {}, False


def _parse_entry_points(raw: Any) -> dict[str, str]:
    """Normalise the several shapes setuptools accepts for console_scripts."""
    out: dict[str, str] = {}
    if isinstance(raw, dict):
        scripts = raw.get("console_scripts") or []
    elif isinstance(raw, str):
        cp = configparser.ConfigParser()
        try:
            cp.read_string(raw)
        except configparser.Error:
            return out
        scripts = [f"{k} = {v}" for k, v in cp.items("console_scripts")] \
            if cp.has_section("console_scripts") else []
    else:
        return out
    if isinstance(scripts, str):
        scripts = [scripts]
    for item in scripts:
        if "=" in item:
            k, _, v = item.partition("=")
            out[k.strip()] = v.strip()
    return out


# --------------------------------------------------------------------------
# manifest readers
# --------------------------------------------------------------------------

def _pep440(name: str, spec: Any) -> str:
    """Render one poetry dependency as a PEP 440 requirement.

    Poetry's caret and tilde operators are not PEP 440, so a resolver handed
    ``pytest = "^7.2.0"`` verbatim either errors or silently ignores the bound.
    Dropping the bound instead would let a test-only dependency jump a major
    version and break the suite for reasons that have nothing to do with the
    repo, so the ranges are translated properly.
    """
    if isinstance(spec, dict):
        spec = spec.get("version", "*")
    if not isinstance(spec, str) or spec.strip() in ("*", ""):
        return name

    text = spec.strip()
    m = re.match(r"^\^(\d+)(?:\.(\d+))?(?:\.(\d+))?$", text)
    if m:
        major, minor, patch = (int(g) if g else 0 for g in m.groups())
        lower = f"{major}.{minor}.{patch}"
        if major > 0:
            upper = f"{major + 1}.0.0"
        elif minor > 0:
            upper = f"0.{minor + 1}.0"
        else:
            upper = f"0.0.{patch + 1}"
        return f"{name}>={lower},<{upper}"

    m = re.match(r"^~(\d+)(?:\.(\d+))?(?:\.(\d+))?$", text)
    if m:
        parts = [g for g in m.groups() if g is not None]
        nums = [int(p) for p in parts]
        lower = ".".join(str(n) for n in nums)
        if len(nums) >= 2:
            upper = f"{nums[0]}.{nums[1] + 1}.0"
        else:
            upper = f"{nums[0] + 1}.0.0"
        return f"{name}>={lower},<{upper}"

    if text[0].isdigit():
        return f"{name}=={text}"
    return f"{name}{text}"


# Tooling that belongs to docs, packaging or static analysis. A broad "dev"
# group routinely carries all three, and none of it is needed to run a test
# suite -- pulling Sphinx and its theme into the test image costs build time and
# image size for nothing. Groups named for testing are taken whole; only the
# catch-all groups are filtered.
_NON_TEST_TOOLING = {
    "sphinx", "furo", "mkdocs", "mkdocs-material", "sphinx-rtd-theme",
    "myst-parser", "sphinx-autodoc-typehints", "pre-commit", "mypy", "black",
    "flake8", "isort", "pylint", "ruff", "twine", "build", "bump2version",
    "commitizen", "towncrier", "pyright", "types-setuptools", "setuptools-scm",
}

_TEST_GROUP_NAMES = ("test", "tests", "testing")


def _read_dependency_groups(data: dict, prof: RepoProfile) -> None:
    """Collect test dependencies that live outside ``optional-dependencies``.

    Two conventions put test tooling somewhere a pip extra cannot reach:
    poetry's ``[tool.poetry.group.<name>.dependencies]`` and PEP 735's
    ``[dependency-groups]``. Missing them means the container is built without
    pytest plugins the suite imports, and the baseline run fails for a reason
    that looks like a broken repo.
    """
    wanted = _TEST_GROUP_NAMES + ("dev", "develop", "check", "lint")
    collected: list[str] = []

    def keep(dist: str, group: str) -> bool:
        if group.lower() in _TEST_GROUP_NAMES:
            return True     # a group named for testing is taken whole
        return dist.lower() not in _NON_TEST_TOOLING

    groups = (((data.get("tool") or {}).get("poetry") or {}).get("group") or {})
    for gname, gbody in groups.items():
        if gname.lower() not in wanted:
            continue
        for dep, spec in ((gbody or {}).get("dependencies") or {}).items():
            if dep.lower() == "python" or not keep(dep, gname):
                continue
            collected.append(_pep440(dep, spec))

    for gname, entries in (data.get("dependency-groups") or {}).items():
        if gname.lower() not in wanted:
            continue
        for entry in entries or []:
            if not isinstance(entry, str):
                continue
            dist = re.split(r"[<>=!~;\[\s]", entry.strip(), maxsplit=1)[0]
            if keep(dist, gname):
                collected.append(entry)

    if collected:
        prof.extra_test_requirements.extend(sorted(set(collected)))
        prof.notes.append(
            f"{len(collected)} test dependency/ies found in dependency groups "
            "rather than extras; resolved alongside the project")


def _read_pyproject(path: Path, prof: RepoProfile) -> None:
    if tomllib is None:
        prof.notes.append("tomllib unavailable; pyproject.toml not parsed")
        return
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # malformed TOML in the wild
        prof.notes.append(f"pyproject.toml unparsable: {exc}")
        return

    build_system = data.get("build-system", {}) or {}
    backend = build_system.get("build-backend", "") or ""
    prof.build_requires = [str(r) for r in (build_system.get("requires") or [])]
    for key, label in (("poetry", "poetry"), ("hatchling", "hatchling"),
                       ("flit", "flit"), ("pdm", "pdm"), ("setuptools", "setuptools"),
                       ("maturin", "maturin"), ("scikit_build", "scikit-build")):
        if key in backend:
            prof.build_backend = label
            break

    project = data.get("project") or {}
    if project:
        prof.build_backend = prof.build_backend if prof.build_backend != "unknown" else "setuptools"
        prof.name = project.get("name") or prof.name
        prof.runtime_requirements = list(project.get("dependencies") or [])
        prof.extras = {k: list(v) for k, v in (project.get("optional-dependencies") or {}).items()}
        prof.python_requires = project.get("requires-python", "") or prof.python_requires
        prof.console_scripts.update(dict(project.get("scripts") or {}))
        prof.static_deps_resolved = True
        # PEP 621 dynamic fields are resolved by the backend, not by us.
        if "dependencies" in (project.get("dynamic") or []):
            prof.static_deps_resolved = False
            prof.notes.append("project.dependencies is dynamic; using backend metadata")

    poetry = ((data.get("tool") or {}).get("poetry") or {})
    if poetry:
        prof.build_backend = "poetry"
        prof.name = poetry.get("name") or prof.name
        deps = poetry.get("dependencies") or {}
        prof.runtime_requirements = [
            f"{k}{v if isinstance(v, str) and v[0:1] in '<>=!~^' else ''}"
            for k, v in deps.items() if k.lower() != "python"
        ]
        prof.python_requires = deps.get("python", "") or prof.python_requires
        # Poetry carets/tildes are not PEP 440; let the backend resolve them.
        prof.static_deps_resolved = False
        prof.notes.append("poetry constraints are not PEP 440; using backend metadata")

    _read_dependency_groups(data, prof)

    tool = data.get("tool") or {}
    for cfg in ("ruff", "black", "flake8", "isort"):
        if cfg in tool:
            prof.lint_configs.append(f"pyproject.toml[tool.{cfg}]")
    if "pytest" in tool:
        prof.notes.append("pytest configured in pyproject.toml")


def _read_setup_cfg(path: Path, prof: RepoProfile) -> None:
    cp = configparser.ConfigParser()
    try:
        cp.read(path, encoding="utf-8")
    except configparser.Error as exc:
        prof.notes.append(f"setup.cfg unparsable: {exc}")
        return

    def _lines(section: str, option: str) -> list[str]:
        if not cp.has_option(section, option):
            return []
        return [ln.strip() for ln in cp.get(section, option).splitlines() if ln.strip()]

    if cp.has_section("metadata"):
        prof.name = cp.get("metadata", "name", fallback="") or prof.name
    if cp.has_section("options"):
        reqs = _lines("options", "install_requires")
        if reqs:
            prof.runtime_requirements = reqs
            prof.static_deps_resolved = True
        prof.python_requires = cp.get("options", "python_requires", fallback="") \
            or prof.python_requires
        if prof.build_backend == "unknown":
            prof.build_backend = "setuptools"
    if cp.has_section("options.extras_require"):
        for key, _ in cp.items("options.extras_require"):
            prof.extras[key] = _lines("options.extras_require", key)
    if cp.has_section("options.entry_points"):
        prof.console_scripts.update(
            _parse_entry_points("[console_scripts]\n"
                                + cp.get("options.entry_points", "console_scripts", fallback="")))
    for sect in ("flake8", "isort", "tool:pytest"):
        if cp.has_section(sect):
            prof.lint_configs.append(f"setup.cfg[{sect}]")


def _read_setup_py(path: Path, prof: RepoProfile) -> None:
    src = path.read_text(encoding="utf-8", errors="replace")
    kwargs, static = _setup_kwargs(src)
    if prof.build_backend == "unknown":
        prof.build_backend = "setuptools"
    prof.name = kwargs.get("name") or prof.name
    if isinstance(kwargs.get("install_requires"), list):
        prof.runtime_requirements = [str(r) for r in kwargs["install_requires"]]
        prof.static_deps_resolved = prof.static_deps_resolved or static
    if isinstance(kwargs.get("extras_require"), dict):
        prof.extras.update({
            str(k): [str(x) for x in v] if isinstance(v, list) else [str(v)]
            for k, v in kwargs["extras_require"].items()
        })
    prof.python_requires = kwargs.get("python_requires") or prof.python_requires
    prof.console_scripts.update(_parse_entry_points(kwargs.get("entry_points")))
    if not static:
        prof.notes.append("setup.py has non-literal arguments; using backend metadata")


# --------------------------------------------------------------------------
# layout discovery
# --------------------------------------------------------------------------

def _discover_packages(root: Path, prof: RepoProfile) -> list[Package]:
    """Find first-party importable packages without trusting any manifest."""
    candidates: list[Package] = []
    search_roots: list[tuple[Path, str]] = [(root, "")]
    src = root / "src"
    if src.is_dir():
        search_roots.insert(0, (src, "src"))

    for base, source_root in search_roots:
        for child in sorted(base.iterdir()):
            if child.name in EXCLUDED_DIRS or child.name.startswith("."):
                continue
            if child.is_dir():
                if not (child / "__init__.py").exists():
                    continue
                if child.name in TEST_DIR_NAMES:
                    continue
                n_mods = sum(1 for _ in child.rglob("*.py"))
                candidates.append(Package(
                    name=child.name, path=child.relative_to(root).as_posix(),
                    is_package=True, source_root=source_root, module_count=n_mods))
            elif child.suffix == ".py" and child.name not in (
                    "setup.py", "conftest.py", "noxfile.py", "tasks.py"):
                if child.stem.startswith("test_"):
                    continue
                candidates.append(Package(
                    name=child.stem, path=child.relative_to(root).as_posix(),
                    is_package=False, source_root=source_root, module_count=1))
        if candidates and source_root == "src":
            break  # a src/ layout is authoritative; do not also scan the root

    # A distribution name like "my-lib" maps to an import name "my_lib".
    if prof.name:
        want = prof.name.replace("-", "_").lower()
        candidates.sort(key=lambda p: (p.name.lower() != want, -p.module_count, p.name))
    else:
        candidates.sort(key=lambda p: (-p.module_count, p.name))
    return candidates


def _discover_tests(root: Path, prof: RepoProfile) -> list[str]:
    found: list[str] = []
    for name in sorted(TEST_DIR_NAMES):
        p = root / name
        if p.is_dir() and any(p.rglob("test_*.py")):
            found.append(name)
    for pkg in prof.packages:
        base = root / pkg.path
        if not base.is_dir():
            continue
        for sub in sorted(base.iterdir()):
            if sub.is_dir() and sub.name in TEST_DIR_NAMES and any(sub.rglob("test_*.py")):
                found.append(sub.relative_to(root).as_posix())
    if not found:
        loose = sorted(p.relative_to(root).as_posix() for p in root.glob("test_*.py"))
        found.extend(loose)
    return found


def _min_python(python_requires: str) -> str:
    """Pick a concrete interpreter satisfying a requires-python specifier."""
    if not python_requires:
        return DEFAULT_PYTHON
    floors = [tuple(int(x) for x in m.split(".")[:2])
              for m in re.findall(r">=\s*(\d+\.\d+)", python_requires)]
    ceils = [tuple(int(x) for x in m.split(".")[:2])
             for m in re.findall(r"<\s*(\d+\.\d+)", python_requires)]
    floor = max(floors) if floors else (3, 0)
    ceil = min(ceils) if ceils else (9, 99)
    for cand in SUPPORTED_PYTHONS:
        cv = tuple(int(x) for x in cand.split("."))
        if floor <= cv < ceil:
            return cand
    return DEFAULT_PYTHON


# Import name -> Debian package needed to build/run it. Only consulted when the
# import actually appears in the resolved dependency set.
_SYSTEM_HINTS = {
    "psycopg2": "libpq-dev", "mysqlclient": "default-libmysqlclient-dev",
    "lxml": "libxml2-dev libxslt1-dev", "cryptography": "libssl-dev libffi-dev",
    "pillow": "libjpeg-dev zlib1g-dev", "numpy": "", "scipy": "gfortran libopenblas-dev",
    "pycurl": "libcurl4-openssl-dev", "cffi": "libffi-dev",
}


def detect(root: str | Path) -> RepoProfile:
    """Build a RepoProfile for the checkout at ``root``."""
    root = Path(root).resolve()
    prof = RepoProfile(root=str(root), name=root.name)

    # Manifests, least to most authoritative.
    for fname, reader in (("setup.py", _read_setup_py),
                          ("setup.cfg", _read_setup_cfg),
                          ("pyproject.toml", _read_pyproject)):
        p = root / fname
        if p.exists():
            prof.manifest_files.append(fname)
            reader(p, prof)

    if not prof.manifest_files:
        prof.notes.append("no packaging manifest; treating as a plain source tree")

    for pat in ("requirements*.txt", "requirements*.in", "requirements/*.txt",
                "constraints*.txt"):
        for p in sorted(root.glob(pat)):
            prof.requirement_files.append(p.relative_to(root).as_posix())

    prof.packages = _discover_packages(root, prof)
    if prof.packages:
        prof.source_root = prof.packages[0].source_root
    else:
        prof.notes.append("no importable first-party package found")

    prof.test_paths = _discover_tests(root, prof)
    if not prof.test_paths:
        prof.notes.append("no existing test suite detected")

    # Which extras does the test suite need? Prefer conventionally-named ones,
    # and include any extra that pulls in a known test dependency.
    for name, reqs in prof.extras.items():
        low = name.lower()
        joined = " ".join(reqs).lower()
        if low in ("test", "tests", "testing", "dev", "develop", "all") \
                or "pytest" in joined:
            prof.test_extras.append(name)
    prof.test_extras = sorted(set(prof.test_extras))

    if not prof.build_requires:
        # PEP 517 default for a project with no [build-system] table.
        prof.build_requires = ["setuptools>=68", "wheel"]

    prof.python_version = _min_python(prof.python_requires)
    prof.has_dockerfile = (root / "Dockerfile").exists()
    prof.has_lock = any((root / n).exists() for n in
                        ("poetry.lock", "uv.lock", "Pipfile.lock", "pdm.lock",
                         "requirements.lock"))

    for cfg in (".flake8", ".ruff.toml", "ruff.toml", ".pylintrc", ".isort.cfg",
                ".pre-commit-config.yaml"):
        if (root / cfg).exists():
            prof.lint_configs.append(cfg)

    blob = " ".join(prof.runtime_requirements + sum(prof.extras.values(), [])).lower()
    for mod, sys_pkgs in _SYSTEM_HINTS.items():
        if mod in blob and sys_pkgs:
            prof.system_packages.extend(sys_pkgs.split())
    prof.system_packages = sorted(set(prof.system_packages))

    if (root / "pytest.ini").exists() or (root / "tox.ini").exists():
        prof.test_framework = "pytest"
    elif prof.test_paths and not any("pytest" in r.lower()
                                     for r in sum(prof.extras.values(), [])):
        prof.test_framework = "pytest"  # pytest runs unittest suites fine

    util.info("detected repo",
              name=prof.name, backend=prof.build_backend,
              packages=",".join(prof.import_names) or "-",
              tests=",".join(prof.test_paths) or "-",
              py=prof.python_version)
    return prof


def strip_comments(source: str) -> str:
    """Remove comments from python source, preserving layout. Used by testgen."""
    out = io.StringIO()
    last_row, last_col = 1, 0
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            ttype, text, (srow, scol), (erow, ecol), _ = tok
            if srow > last_row:
                last_col = 0
                out.write("\n" * (srow - last_row))
            if scol > last_col:
                out.write(" " * (scol - last_col))
            out.write("" if ttype == tokenize.COMMENT else text)
            last_row, last_col = erow, ecol
    except (tokenize.TokenError, IndentationError):
        return source
    return out.getvalue()
