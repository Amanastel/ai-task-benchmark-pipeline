"""Docker plumbing.

Every step that resolves dependencies, runs tests, or validates a task runs
inside a Linux container so that the result does not depend on the developer's
machine. Two rules make that trustworthy:

* base images are referenced by **digest**, never by a floating tag, so
  ``python:3.11-slim`` moving under us cannot change a result; and
* containers get ``--network none`` for anything after dependency resolution,
  so a test cannot quietly reach the internet and become flaky.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Sequence

from .. import util

DEFAULT_REGISTRY = "docker.io/library"


class DockerUnavailable(RuntimeError):
    pass


def require_docker() -> str:
    res = util.run(["docker", "version", "--format", "{{.Server.Version}}"], timeout=90)
    if not res.ok:
        raise DockerUnavailable(
            "docker daemon not reachable. Start Docker Desktop / dockerd and retry.\n"
            + util.truncate(res.output, 800, 800))
    return res.stdout.strip()


def _host_path(path: str | Path) -> str:
    """A path string docker's -v flag accepts on this host."""
    p = str(Path(path).resolve())
    if os.name == "nt":
        return p
    return p


def resolve_digest(reference: str) -> str:
    """Turn ``python:3.11-slim`` into ``python@sha256:...``.

    Pulls if necessary. Falls back to the plain tag (with a loud warning) when
    the registry is unreachable, so an offline re-run of an already-built image
    still works.
    """
    name = reference.split(":")[0]
    inspect = ["docker", "image", "inspect", reference, "--format", "{{json .RepoDigests}}"]
    res = util.run(inspect, timeout=120)
    if not res.ok:
        util.info("pulling base image", image=reference)
        pull = util.run(["docker", "pull", "--quiet", reference], timeout=1800)
        if not pull.ok:
            util.warn("could not pull base image; falling back to floating tag",
                      image=reference)
            return reference
        res = util.run(inspect, timeout=120)
    m = re.search(r"([a-z0-9._/\-]+)@(sha256:[0-9a-f]{64})", res.stdout or "")
    if not m:
        util.warn("no repo digest for image; falling back to floating tag", image=reference)
        return reference
    digest = m.group(2)
    # Keep the short, canonical form: "python@sha256:..." not "docker.io/library/python@...".
    return f"{name}@{digest}"


@dataclass
class Mount:
    host: str
    container: str
    mode: str = "rw"

    def as_flag(self) -> list[str]:
        return ["-v", f"{_host_path(self.host)}:{self.container}:{self.mode}"]


def run_in(
    image: str,
    argv: Sequence[str],
    mounts: Sequence[Mount] = (),
    workdir: str = "/work",
    network: str | None = "none",
    timeout: int = 1800,
    env: dict | None = None,
    user: str | None = None,
) -> util.Result:
    """Run ``argv`` inside ``image``. Returns the Result; never raises on exit code."""
    cmd: list[str] = ["docker", "run", "--rm", "-w", workdir]
    if network is not None:
        cmd += ["--network", network]
    if user:
        cmd += ["--user", user]
    for m in mounts:
        cmd += m.as_flag()
    # A stable hash seed and a fixed locale keep container output byte-identical.
    base_env = {"PYTHONHASHSEED": "0", "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUNBUFFERED": "1", "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8",
                "TZ": "UTC", "SOURCE_DATE_EPOCH": "1700000000",
                "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
    base_env.update(env or {})
    for k, v in sorted(base_env.items()):
        cmd += ["-e", f"{k}={v}"]
    cmd.append(image)
    cmd += [str(a) for a in argv]
    return util.run(cmd, timeout=timeout)


def build_image(context: str | Path, tag: str, dockerfile: str = "Dockerfile",
                timeout: int = 3600, no_cache: bool = False) -> util.Result:
    cmd = ["docker", "build", "-t", tag, "-f", str(Path(context) / dockerfile)]
    if no_cache:
        cmd.append("--no-cache")
    cmd.append(str(Path(context).resolve()))
    return util.run(cmd, timeout=timeout)


def image_exists(tag: str) -> bool:
    return util.run(["docker", "image", "inspect", tag], timeout=60).ok


def remove_image(tag: str) -> None:
    util.run(["docker", "image", "rm", "-f", tag], timeout=300)
