"""Small, dependency-free helpers shared by every stage.

Determinism is the whole point of this file: JSON is always written with sorted
keys and a trailing newline, wall-clock timestamps never get embedded in
artifacts that get diffed, and subprocesses always get an explicit, scrubbed
environment.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from collections.abc import Iterable, Mapping, Sequence

# --------------------------------------------------------------------------
# logging
# --------------------------------------------------------------------------

_LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40}
_THRESHOLD = _LEVELS.get(os.environ.get("OKF_LOG_LEVEL", "info").lower(), 20)
_START = time.time()


def log(level: str, msg: str, **kw: Any) -> None:
    if _LEVELS.get(level, 20) < _THRESHOLD:
        return
    extra = " ".join(f"{k}={v}" for k, v in kw.items())
    prefix = f"[{time.time() - _START:7.2f}s] {level.upper():5}"
    print(f"{prefix} {msg}{(' ' + extra) if extra else ''}", file=sys.stderr, flush=True)


def debug(msg: str, **kw: Any) -> None:
    log("debug", msg, **kw)


def info(msg: str, **kw: Any) -> None:
    log("info", msg, **kw)


def warn(msg: str, **kw: Any) -> None:
    log("warn", msg, **kw)


def error(msg: str, **kw: Any) -> None:
    log("error", msg, **kw)


# --------------------------------------------------------------------------
# subprocess
# --------------------------------------------------------------------------

@dataclass
class Result:
    argv: Sequence[str]
    returncode: int
    stdout: str
    stderr: str
    duration_s: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def output(self) -> str:
        """stdout and stderr concatenated the way a human would read them."""
        return (self.stdout or "") + (self.stderr or "")

    def check(self, what: str = "") -> Result:
        if not self.ok:
            raise CommandError(what or " ".join(map(str, self.argv)), self)
        return self


class CommandError(RuntimeError):
    def __init__(self, what: str, result: Result):
        self.result = result
        tail = result.output.strip().splitlines()[-40:]
        super().__init__(f"{what} failed (exit {result.returncode})\n" + "\n".join(tail))


# Environment variables scrubbed from every subprocess so a developer's local
# shell cannot leak non-determinism (or credentials) into the pipeline.
_SCRUB = (
    "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL",
    "PIP_REQUIRE_VIRTUALENV", "VIRTUAL_ENV", "CONDA_PREFIX", "GITHUB_TOKEN", "GH_TOKEN",
)


def clean_env(**overrides: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _SCRUB}
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    env.setdefault("PYTHONUNBUFFERED", "1")
    # Stable hashing keeps set/dict iteration order reproducible across runs.
    env["PYTHONHASHSEED"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.update({k: v for k, v in overrides.items()})
    return env


def run(
    argv: Sequence[str],
    cwd: str | Path | None = None,
    timeout: int = 900,
    env: Mapping[str, str] | None = None,
    check: bool = False,
    input_text: str | None = None,
) -> Result:
    argv = [str(a) for a in argv]
    started = time.time()
    debug("run", argv=" ".join(argv), cwd=str(cwd or ""))
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            env=dict(env) if env is not None else clean_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            input=input_text,
        )
        res = Result(argv, proc.returncode, proc.stdout or "", proc.stderr or "",
                     time.time() - started)
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        res = Result(argv, 124, out, f"TIMEOUT after {timeout}s", time.time() - started)
    except FileNotFoundError as exc:
        res = Result(argv, 127, "", str(exc), time.time() - started)
    if check:
        res.check()
    return res


def have(tool: str) -> bool:
    return shutil.which(tool) is not None


# --------------------------------------------------------------------------
# filesystem / json
# --------------------------------------------------------------------------

def write_text(path: str | Path, text: str) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Always LF: these files are consumed inside Linux containers.
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return p


def read_text(path: str | Path, default: str | None = None) -> str:
    p = Path(path)
    if not p.exists():
        if default is None:
            raise FileNotFoundError(str(p))
        return default
    return p.read_text(encoding="utf-8", errors="replace")


def write_json(path: str | Path, obj: Any) -> Path:
    return write_text(path, json.dumps(obj, indent=2, sort_keys=True, default=str) + "\n")


def read_json(path: str | Path, default: Any = None) -> Any:
    p = Path(path)
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding="utf-8"))


def write_jsonl(path: str | Path, rows: Iterable[Mapping[str, Any]]) -> Path:
    body = "".join(json.dumps(r, sort_keys=True, default=str) + "\n" for r in rows)
    return write_text(path, body)


def read_jsonl(path: str | Path) -> list:
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def rmtree(path: str | Path) -> None:
    """shutil.rmtree that survives read-only files (git object stores on Windows)."""
    def _onerror(func, p, _exc):
        try:
            os.chmod(p, 0o700)
            func(p)
        except OSError:
            pass
    if Path(path).exists():
        shutil.rmtree(str(path), onerror=_onerror)


_JUNK = ("__pycache__", "*.pyc", "*.pyo", ".tox", ".pytest_cache", ".ruff_cache",
         ".coverage", ".coverage.*", "htmlcov", "*.egg-info", ".mypy_cache")


def copytree(src: str | Path, dst: str | Path, keep_git: bool = False) -> None:
    patterns = list(_JUNK) + ([] if keep_git else [".git"])
    shutil.copytree(str(src), str(dst), ignore=shutil.ignore_patterns(*patterns),
                    dirs_exist_ok=True)


def slugify(text: str, maxlen: int = 48) -> str:
    out: list[str] = []
    for ch in text.lower():
        if ch.isalnum():
            out.append(ch)
        elif out and out[-1] != "-":
            out.append("-")
    return "".join(out).strip("-")[:maxlen].strip("-") or "x"


def truncate(text: str, head: int = 6000, tail: int = 6000) -> str:
    """Keep the informative ends of a long log, drop the middle."""
    if len(text) <= head + tail:
        return text
    dropped = len(text) - head - tail
    return f"{text[:head]}\n...[{dropped} chars elided]...\n{text[-tail:]}"


@dataclass
class Stopwatch:
    label: str
    t0: float = field(default_factory=time.time)

    def __enter__(self) -> Stopwatch:
        info(f"-> {self.label}")
        return self

    def __exit__(self, *exc: Any) -> None:
        info(f"<- {self.label}", took=f"{time.time() - self.t0:.1f}s")
