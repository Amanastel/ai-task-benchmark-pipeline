"""Acquiring the target repository and reading its history.

Stage 3 mines git history, so the clone is deliberately *not* shallow. The
handle keeps the pristine clone separate from the working copy the pipeline
mutates, which is what lets task construction check out arbitrary historical
states without ever disturbing the tree stage 1 transformed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import util

_URL_RE = re.compile(r"^(https?://|git@|ssh://|git://)")


@dataclass
class Commit:
    sha: str
    parents: list[str]
    author_date: str
    subject: str
    body: str = ""
    files: list[str] = field(default_factory=list)
    insertions: int = 0
    deletions: int = 0

    @property
    def short(self) -> str:
        return self.sha[:12]

    @property
    def is_merge(self) -> bool:
        return len(self.parents) > 1

    @property
    def message(self) -> str:
        return (self.subject + "\n" + self.body).strip()


@dataclass
class RepoHandle:
    source: str
    path: Path            # working copy the pipeline is allowed to modify
    is_git: bool
    head_sha: str = ""
    default_branch: str = ""
    remote_url: str = ""

    def git(self, *args: str, timeout: int = 600) -> util.Result:
        return util.run(["git", *args], cwd=self.path, timeout=timeout)


def is_url(text: str) -> bool:
    return bool(_URL_RE.match(text.strip()))


def project_name(source: str) -> str:
    """The repository's own name, from a URL or a local path.

    Used to name the output tree. Deriving it from the scratch checkout path
    instead would name every output after the temp directory.
    """
    text = source.strip().rstrip("/")
    if is_url(text):
        tail = re.split(r"[/:]", text)[-1]
    else:
        tail = Path(text).expanduser().resolve().name
    return re.sub(r"\.git$", "", tail) or "repo"


def acquire(source: str, dest: Path, depth: int = 0) -> RepoHandle:
    """Clone a URL or copy a local path into ``dest``."""
    dest = Path(dest)
    util.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    if is_url(source):
        # Submodules are cloned by default because they routinely carry test
        # data. tomlkit, for one, keeps its TOML conformance corpus in a
        # submodule; without it a test module fails at *collection* with a
        # FileNotFoundError, which looks like a broken repo rather than an
        # incomplete checkout.
        args = ["git", "clone", "--quiet", "--recurse-submodules"]
        if depth:
            args += ["--depth", str(depth)]
        args += [source, str(dest)]
        util.info("cloning", url=source)
        res = util.run(args, timeout=2400)
        if not res.ok:
            util.warn("clone with submodules failed; retrying without them",
                      detail=util.truncate(res.output, 0, 300).replace("\n", " | "))
            util.rmtree(dest)
            plain = ["git", "clone", "--quiet"]
            if depth:
                plain += ["--depth", str(depth)]
            plain += [source, str(dest)]
            util.run(plain, timeout=2400).check("git clone")
    else:
        src = Path(source).expanduser().resolve()
        if not src.is_dir():
            raise FileNotFoundError(f"not a directory: {src}")
        util.info("copying local repo", path=str(src))
        util.copytree(src, dest, keep_git=True)

    handle = RepoHandle(source=source, path=dest, is_git=(dest / ".git").exists())
    if handle.is_git:
        handle.head_sha = handle.git("rev-parse", "HEAD").stdout.strip()
        branch = handle.git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        handle.default_branch = branch
        remote = handle.git("config", "--get", "remote.origin.url").stdout.strip()
        handle.remote_url = remote or (source if is_url(source) else "")
        # Detached identity so later `git commit` calls in task building work
        # without depending on the operator's global git config.
        handle.git("config", "user.email", "pipeline@okfpipe.local")
        handle.git("config", "user.name", "okfpipe")
        util.info("repo acquired", head=handle.head_sha[:12], branch=branch)
    else:
        util.warn("source has no git history; history-derived tasks unavailable")
    return handle


_LOG_SEP = "\x1e"
_FIELD_SEP = "\x1f"


def history(handle: RepoHandle, limit: int = 4000, since: str = "") -> list[Commit]:
    """Full-ish commit log with per-commit file lists."""
    if not handle.is_git:
        return []
    # The record separator leads each record. Trailing it instead would push
    # every commit's --numstat block into the *next* chunk, so the header line
    # after a split would be numstat rows rather than the format fields.
    fmt = _LOG_SEP + _FIELD_SEP.join(["%H", "%P", "%aI", "%s", "%b"])
    args = ["log", f"--max-count={limit}", f"--format={fmt}", "--numstat"]
    if since:
        args.append(f"--since={since}")
    res = handle.git(*args, timeout=900)
    if not res.ok:
        util.warn("git log failed")
        return []

    commits: list[Commit] = []
    for chunk in res.stdout.split(_LOG_SEP):
        chunk = chunk.strip("\n")
        if not chunk.strip():
            continue
        head, _, stat_block = chunk.partition("\n")
        parts = head.split(_FIELD_SEP)
        if len(parts) < 4:
            continue
        sha, parents, date, subject = parts[0], parts[1], parts[2], parts[3]
        body = parts[4] if len(parts) > 4 else ""
        # The body and the numstat block share the same trailing region; split
        # them on the first line that looks like a numstat row.
        files: list[str] = []
        ins = dels = 0
        body_lines: list[str] = []
        for line in (body + "\n" + stat_block).splitlines():
            m = re.match(r"^(\d+|-)\t(\d+|-)\t(.+)$", line)
            if m:
                a, b, path = m.groups()
                ins += int(a) if a.isdigit() else 0
                dels += int(b) if b.isdigit() else 0
                # Handle rename notation "old => new".
                if " => " in path:
                    path = re.sub(r"\{.*? => (.*?)\}", r"\1", path)
                    path = path.split(" => ")[-1].strip("{}")
                files.append(path.strip())
            elif line.strip():
                body_lines.append(line)
        commits.append(Commit(
            sha=sha.strip(), parents=parents.split() if parents.strip() else [],
            author_date=date.strip(), subject=subject.strip(),
            body="\n".join(body_lines).strip(), files=sorted(set(files)),
            insertions=ins, deletions=dels,
        ))
    util.info("history read", commits=len(commits))
    return commits


def export_tree(handle: RepoHandle, sha: str, dest: Path) -> None:
    """Materialise the full tree at ``sha`` into ``dest`` (no .git)."""
    dest = Path(dest)
    util.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    # `git archive | tar -x` is the reliable way to get a clean tree without
    # touching the working copy or creating a worktree we then have to prune.
    archive = dest.parent / f"_{sha[:12]}.tar"
    res = handle.git("archive", "--format=tar", "-o", str(archive), sha, timeout=900)
    res.check(f"git archive {sha[:12]}")
    import tarfile
    with tarfile.open(archive) as tf:
        tf.extractall(dest)  # noqa: S202 - archive is produced by git from our own clone
    archive.unlink(missing_ok=True)


def show_file(handle: RepoHandle, sha: str, path: str) -> str | None:
    res = handle.git("show", f"{sha}:{path}", timeout=300)
    return res.stdout if res.ok else None


def diff(handle: RepoHandle, base: str, head: str, paths: list[str] | None = None,
         context: int = 3) -> str:
    args = ["diff", f"--unified={context}", base, head]
    if paths:
        args += ["--", *paths]
    return handle.git(*args, timeout=600).stdout


def files_at(handle: RepoHandle, sha: str) -> list[str]:
    res = handle.git("ls-tree", "-r", "--name-only", sha, timeout=600)
    return sorted(res.stdout.splitlines()) if res.ok else []
