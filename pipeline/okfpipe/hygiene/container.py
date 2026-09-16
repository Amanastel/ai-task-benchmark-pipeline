"""Stage 1b -- containerization.

Generates a Dockerfile, a compose file, a .dockerignore and a ``run-tests.sh``
entry point for an arbitrary Python repo. Design constraints, in priority order:

1. **Reproducible.** Base image by digest, dependencies installed from a hashed
   lock with ``--require-hashes``, build backends pinned so build isolation
   cannot pull a different setuptools next week, and a fixed ``PYTHONHASHSEED``
   and ``TZ`` so test output does not wobble.
2. **Single documented command.** ``docker compose run --rm tests`` builds the
   image if needed and runs the suite, so a grader types one line.
3. **Honest about services.** A compose service is only added for a backing
   store the repo's pinned dependencies actually talk to.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .. import util
from .detect import RepoProfile

# distribution name -> (compose service name, image, env, healthcheck cmd)
SERVICE_MAP = {
    "psycopg2": ("postgres", "postgres:16-alpine"),
    "psycopg2-binary": ("postgres", "postgres:16-alpine"),
    "psycopg": ("postgres", "postgres:16-alpine"),
    "asyncpg": ("postgres", "postgres:16-alpine"),
    "redis": ("redis", "redis:7-alpine"),
    "pymongo": ("mongo", "mongo:7"),
    "motor": ("mongo", "mongo:7"),
    "mysqlclient": ("mysql", "mysql:8"),
    "pymysql": ("mysql", "mysql:8"),
    "elasticsearch": ("elasticsearch", "elasticsearch:8.15.0"),
}

SERVICE_BLOCKS = {
    "postgres": """  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_PASSWORD: postgres
      POSTGRES_DB: app
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 3s
      retries: 20
""",
    "redis": """  redis:
    image: redis:7-alpine
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 3s
      retries: 20
""",
    "mongo": """  mongo:
    image: mongo:7
    healthcheck:
      test: ["CMD", "mongosh", "--eval", "db.adminCommand('ping')"]
      interval: 3s
      retries: 20
""",
    "mysql":  """  mysql:
    image: mysql:8
    environment:
      MYSQL_ROOT_PASSWORD: mysql
      MYSQL_DATABASE: app
    healthcheck:
      test: ["CMD", "mysqladmin", "ping", "-h", "localhost"]
      interval: 3s
      retries: 20
""",
    "elasticsearch": """  elasticsearch:
    image: elasticsearch:8.15.0
    environment:
      discovery.type: single-node
      xpack.security.enabled: "false"
""",
}


@dataclass
class ContainerResult:
    image_tag: str
    dockerfile: str = "Dockerfile"
    compose: str = "docker-compose.yml"
    services: list[str] = field(default_factory=list)
    test_command: str = ""
    notes: list[str] = field(default_factory=list)


def detect_services(packages: dict[str, str]) -> list[str]:
    found = {SERVICE_MAP[p][0] for p in packages if p in SERVICE_MAP}
    return sorted(found)


_VCS_BUILD_BACKENDS = ("setuptools_scm", "setuptools-scm", "hatch-vcs",
                       "versioneer", "dunamai", "pdm-backend")


def _needs_git(prof: RepoProfile) -> bool:
    """Whether the image has to carry git.

    Installing it unconditionally is a wasted layer on most repos, but omitting
    it breaks the two cases that genuinely need it: a build backend that derives
    the version from VCS metadata, and a test suite that shells out to git.
    """
    blob = " ".join(prof.build_requires).lower()
    if any(name in blob for name in _VCS_BUILD_BACKENDS):
        return True
    for tf in prof.test_files():
        try:
            text = tf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if re.search(r"""["']git["']|\bgit \w|GitPython|\bdulwich\b""", text):
            return True
    return False


def _apt_packages(prof: RepoProfile) -> list[str]:
    pkgs = set(prof.system_packages)
    if _needs_git(prof):
        pkgs.add("git")
    return sorted(pkgs)


def test_command(prof: RepoProfile, generated_tests_dir: str = "") -> str:
    """The pytest invocation used everywhere -- container, CI and validation.

    Test paths are resolved by run-tests.sh at runtime rather than baked in
    here, so the same script works before and after stage 1c has written the
    generated tests. Naming a directory that does not exist yet makes pytest
    exit 4, which would fail the baseline run for a reason that has nothing to
    do with the repo.
    """
    # -p no:cacheprovider keeps the source tree clean so a second run starts
    # from the same state as the first -- that is what makes reruns identical.
    return 'python -m pytest "$@" $PATHS -p no:cacheprovider --timeout=300 -q'


def render_dockerfile(prof: RepoProfile, base_image: str, lock: str,
                      has_lock_hashes: bool, build_requires: list[str],
                      install_editable: bool) -> str:
    apt = _apt_packages(prof)
    lines: list[str] = []
    A = lines.append

    A("# syntax=docker/dockerfile:1")
    A("#")
    A("# Generated by okfpipe (stage 1). Reproducibility notes:")
    A("#   * base image pinned by digest, not by a moving tag")
    A("#   * dependencies installed from a fully pinned lock" +
      (" with --require-hashes" if has_lock_hashes else ""))
    if build_requires:
        A("#   * build backends pinned too ("
          + ", ".join(build_requires)
          + "), so --no-build-isolation is safe")
    A("#")
    A(f"FROM {base_image}")
    A("")
    A(f'LABEL org.opencontainers.image.title="{prof.name}"')
    A('LABEL org.opencontainers.image.description="Hygiene-transformed by okfpipe"')
    A("")
    A("ENV PYTHONDONTWRITEBYTECODE=1 \\")
    A("    PYTHONUNBUFFERED=1 \\")
    A("    PYTHONHASHSEED=0 \\")
    A("    PIP_DISABLE_PIP_VERSION_CHECK=1 \\")
    A("    PIP_NO_CACHE_DIR=1 \\")
    A("    LC_ALL=C.UTF-8 \\")
    A("    LANG=C.UTF-8 \\")
    A("    TZ=UTC")
    A("")
    if apt:
        A("RUN apt-get update \\")
        A(" && apt-get install -y --no-install-recommends \\")
        A("      " + " ".join(apt) + " \\")
        A(" && rm -rf /var/lib/apt/lists/*")
        A("")
    A("WORKDIR /app")
    A("")
    A("# Dependency layer first: it only re-runs when the lock actually changes.")
    A(f"COPY {lock} ./{lock}")
    flags = "--require-hashes " if has_lock_hashes else ""
    A(f"RUN pip install {flags}-r {lock}")
    A("")
    A("COPY . /app")
    if install_editable:
        A("")
        A("# --no-deps: the lock is the single source of truth for versions.")
        A("# --no-build-isolation: build backends are already pinned above.")
        A("RUN pip install --no-deps --no-build-isolation -e .")
    A("")
    A("# Run as a non-root user; tests that need to write get an owned tree.")
    A("RUN useradd --create-home --uid 1000 app && chown -R app:app /app")
    A("USER app")
    A("")
    A('CMD ["./run-tests.sh"]')
    A("")
    return "\n".join(lines)


def render_compose(prof: RepoProfile, services: list[str], image_tag: str) -> str:
    out: list[str] = []
    A = out.append
    A("# Generated by okfpipe (stage 1).")
    A("#")
    A("#   docker compose run --rm tests     # build if needed, then run the suite")
    A("#")
    A("services:")
    A("  tests:")
    A("    build:")
    A("      context: .")
    A("      dockerfile: Dockerfile")
    A(f"    image: {image_tag}")
    A("    environment:")
    A("      PYTHONHASHSEED: \"0\"")
    A("      TZ: UTC")
    if services:
        A("    depends_on:")
        for s in services:
            A(f"      {s}:")
            A("        condition: service_healthy")
    else:
        # No backing services -> no reason to let a unit test reach the network.
        A("    network_mode: none")
    A('    command: ["./run-tests.sh"]')
    for s in services:
        A(SERVICE_BLOCKS.get(s, f"  {s}:\n    image: {s}\n").rstrip("\n"))
    A("")
    return "\n".join(out)


DOCKERIGNORE = """\
# Generated by okfpipe (stage 1).
.git
.github
.tox
.nox
.venv
venv
__pycache__
*.pyc
*.pyo
*.egg-info
.pytest_cache
.ruff_cache
.mypy_cache
htmlcov
.coverage
.coverage.*
build
dist
docs/_build
"""


def render_run_tests(prof: RepoProfile, cmd: str, generated_dir: str) -> str:
    """The single test entry point, shared by the container, CI and stage 3."""
    declared = " ".join(prof.test_paths)
    return f"""#!/bin/sh
# Generated by okfpipe (stage 1). One entry point so the container, the
# determinism check and the task validation harness cannot drift apart.
set -eu

# Unset so an inherited PYTEST_ADDOPTS cannot silently change the verdict.
unset PYTEST_ADDOPTS 2>/dev/null || true
export PYTHONHASHSEED=0

# Collect test roots that actually exist. The generated directory is absent on
# a fresh checkout and before stage 1c has run, and naming a missing path makes
# pytest exit 4 for a reason unrelated to the code under test.
PATHS=""
for candidate in {declared or "."} {generated_dir}; do
    found=$(find "$candidate" -name 'test_*.py' -print -quit 2>/dev/null || true)
    if [ -d "$candidate" ] && [ -n "$found" ]; then
        PATHS="$PATHS $candidate"
    elif [ -f "$candidate" ]; then
        PATHS="$PATHS $candidate"
    fi
done

if [ -z "$PATHS" ]; then
    echo "run-tests.sh: no test files found" >&2
    exit 1
fi

exec {cmd}
"""


def containerize(prof: RepoProfile, out_repo: Path, base_image: str,
                 lock_name: str, hashed: bool, packages: dict[str, str],
                 build_requires: list[str], generated_tests_dir: str = "") -> ContainerResult:
    services = detect_services(packages)
    install_editable = bool(prof.manifest_files)
    tag = f"okf-{util.slugify(prof.name)}:latest"
    cmd = test_command(prof, generated_tests_dir)

    util.write_text(out_repo / "Dockerfile",
                    render_dockerfile(prof, base_image, lock_name, hashed,
                                      build_requires, install_editable))
    util.write_text(out_repo / "docker-compose.yml",
                    render_compose(prof, services, tag))
    util.write_text(out_repo / ".dockerignore", DOCKERIGNORE)
    p = util.write_text(out_repo / "run-tests.sh",
                        render_run_tests(prof, cmd, generated_tests_dir))
    try:
        p.chmod(0o755)
    except OSError:
        pass

    notes = []
    if not install_editable:
        notes.append("no packaging manifest; the package is imported from the "
                     "working directory rather than installed")
    if services:
        notes.append(f"compose services added for detected clients: {', '.join(services)}")
    else:
        notes.append("no backing services detected; the test container runs with "
                     "no network")

    util.info("containerized", image=tag, services=",".join(services) or "-")
    return ContainerResult(image_tag=tag, services=services, test_command=cmd, notes=notes)


def mark_executable_in_git(repo: Path, relpath: str) -> None:
    """Record the exec bit in the index so a Windows checkout still works."""
    util.run(["git", "update-index", "--chmod=+x", relpath], cwd=repo)


_RE_NONDET = re.compile(
    r"\b(datetime\.now|time\.time|random\.(random|randint|choice|shuffle)|uuid[14]|"
    r"tempfile\.mktemp|socket\.socket)\b")


def scan_nondeterminism(prof: RepoProfile) -> list[dict]:
    """Flag test code whose verdict can change between two identical runs.

    Reported, never auto-fixed: rewriting someone's test to use a frozen clock
    is a behavioural change the pipeline has no business making silently.
    """
    hits: list[dict] = []
    for tf in prof.test_files():
        try:
            text = tf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            m = _RE_NONDET.search(line)
            if m and not line.strip().startswith("#"):
                hits.append({"file": prof.rel(tf), "line": i,
                             "symbol": m.group(1), "text": line.strip()[:160]})
    return hits
