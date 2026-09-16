#!/usr/bin/env bash
#
# okfpipe -- single entry point for all three pipeline stages.
#
#   ./run.sh <repo_url_or_path> [options]
#
# Examples
#   ./run.sh https://github.com/mahmoud/glom.git
#   ./run.sh ../some-local-repo --stages hygiene
#   ./run.sh <url> --stages tasks --task-count 10
#
# Requirements: python >= 3.10 on the host (the pipeline itself needs no
# third-party packages) and a running Docker daemon. Everything that resolves
# dependencies, runs tests or validates a task happens inside a container, so
# the host's Python version does not affect any result.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "$#" -lt 1 ]; then
    sed -n '2,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 2
fi

# Git Bash on Windows rewrites container-side paths like /app into Windows
# paths before docker sees them. Turning that off here keeps one code path.
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'

PY="${OKF_PYTHON:-}"
if [ -z "$PY" ]; then
    for cand in python3 python py; do
        if command -v "$cand" >/dev/null 2>&1; then
            if "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
                PY="$cand"
                break
            fi
        fi
    done
fi

if [ -z "$PY" ]; then
    echo "error: need python >= 3.10 on PATH (or set OKF_PYTHON)" >&2
    exit 1
fi

if ! docker version >/dev/null 2>&1; then
    echo "error: the Docker daemon is not reachable. Start Docker and retry." >&2
    exit 1
fi

export PYTHONPATH="${HERE}/pipeline${PYTHONPATH:+:${PYTHONPATH}}"
cd "$HERE"
exec "$PY" -m okfpipe "$@"
