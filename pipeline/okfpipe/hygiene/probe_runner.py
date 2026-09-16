"""The probe script that runs *inside* the container.

This file is never imported by the pipeline. It is copied into the container
and executed there, because the only environment that can tell us what a
function actually returns is the pinned environment the tests will run in.

Contract:

    python probe_runner.py <candidates.json> <results.json>

``candidates.json`` is a list of objects::

    {"id": "...", "module": "pkg.mod", "setup": "...", "expr": "f(1, 2)"}

For each candidate the probe imports ``module``, executes ``setup`` (if any),
evaluates ``expr`` twice under different hash seeds -- handled by the caller
running the probe twice -- and records an outcome::

    {"id": ..., "kind": "value"|"exception"|"error",
     "repr": "...", "type": "...", "exc_type": "...", "roundtrip": true}

Nothing here decides whether a case is good enough to become a test; that
judgement lives in testgen.py so the rules are visible in one place.
"""

from __future__ import annotations

import json
import importlib
import re
import sys
import traceback

# reprs containing any of these cannot be compared across runs
VOLATILE = re.compile(
    r"0x[0-9a-fA-F]{6,}"          # id()-derived addresses
    r"|<\w+ object at "
    r"|\bat 0x"
    r"|/tmp/|/var/folders|C:\\\\"  # absolute paths
    r"|\b\d{4}-\d{2}-\d{2}T"       # timestamps
)

MAX_REPR = 600


def _safe_repr(value: object) -> str:
    try:
        return repr(value)
    except Exception as exc:  # a repr that raises is not testable
        return f"<unreprable {type(exc).__name__}>"


def _roundtrips(value: object, text: str) -> bool:
    """True when ``eval(repr(x)) == x``, so the repr can be a test literal."""
    try:
        return bool(eval(text, {"__builtins__": {}}, {}) == value)
    except Exception:
        return False


def evaluate(cand: dict) -> dict:
    out = {"id": cand["id"]}
    try:
        mod = importlib.import_module(cand["module"])
    except Exception as exc:
        return {**out, "kind": "error", "detail": f"import failed: {exc!r}"}

    ns = {"__builtins__": __builtins__, "M": mod}
    for name in dir(mod):
        if not name.startswith("__"):
            ns[name] = getattr(mod, name)

    setup = cand.get("setup") or ""
    if setup:
        try:
            exec(setup, ns)
        except Exception as exc:
            return {**out, "kind": "error", "detail": f"setup failed: {exc!r}"}

    try:
        value = eval(cand["expr"], ns)
    except BaseException as exc:  # noqa: BLE001 - we are classifying, not handling
        if isinstance(exc, (KeyboardInterrupt, SystemExit, MemoryError, RecursionError)):
            return {**out, "kind": "error", "detail": type(exc).__name__}
        return {
            **out, "kind": "exception",
            "exc_type": type(exc).__name__,
            "exc_module": type(exc).__module__,
            "exc_str": str(exc)[:300],
        }

    text = _safe_repr(value)
    if len(text) > MAX_REPR or VOLATILE.search(text) or text.startswith("<unreprable"):
        return {**out, "kind": "error", "detail": "volatile or oversized repr"}

    return {
        **out, "kind": "value", "repr": text,
        "type": type(value).__name__,
        "type_module": type(value).__module__,
        "roundtrip": _roundtrips(value, text),
    }


def main(argv: list[str]) -> int:
    with open(argv[1], encoding="utf-8") as fh:
        candidates = json.load(fh)
    results = []
    for cand in candidates:
        try:
            results.append(evaluate(cand))
        except Exception:
            results.append({"id": cand.get("id"), "kind": "error",
                            "detail": traceback.format_exc(limit=3)[-400:]})
    with open(argv[2], "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=1, sort_keys=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
