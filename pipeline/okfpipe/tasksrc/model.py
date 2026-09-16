"""The task record, and the guards that keep an instruction honest.

A benchmark task is only as good as its instruction. Two failure modes matter
and both are checked mechanically here rather than left to judgement:

**Solution leak.** If the instruction contains the patch, the task measures
transcription. ``leak_report`` diffs the instruction against the lines the
reference solution *adds*, and flags any run of shared tokens long enough to be
a copied code fragment, plus any identifier the solution introduces.

**Implementation prescription.** An instruction that says *how* rejects correct
alternative implementations. ``prescription_report`` looks for imperative code
directives ("change X to Y", "replace", "add a parameter called") and for
literal code spans, and reports them so the builder can rewrite or drop.

Neither check can prove an instruction is good. Both can prove a specific
instruction is bad, which is the useful direction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

SOURCE_TYPES = ("history", "excision", "net-new")
DIFFICULTIES = ("easy", "medium", "hard")


@dataclass
class Provenance:
    kind: str                      # history | excision | net-new
    commit_sha: str = ""
    parent_sha: str = ""
    merge_pr: str = ""
    excision_target: str = ""      # qualname whose body was removed
    detector: str = ""             # net-new: which capability-gap detector fired
    upstream_url: str = ""
    commit_subject: str = ""
    commit_date: str = ""

    def to_json(self) -> dict:
        return {k: v for k, v in {
            "kind": self.kind,
            "commit_sha": self.commit_sha,
            "parent_sha": self.parent_sha,
            "merge_pr": self.merge_pr,
            "excision_target": self.excision_target,
            "detector": self.detector,
            "upstream_url": self.upstream_url,
            "commit_subject": self.commit_subject,
            "commit_date": self.commit_date,
        }.items() if v}


@dataclass
class TaskSpec:
    id: str
    title: str
    instruction: str
    provenance: Provenance
    difficulty: str
    difficulty_rationale: str
    files_in_scope: list[str]
    modules: list[str]
    verifier_selection: list[str] = field(default_factory=list)   # pytest node ids
    verifier_overlay: dict[str, str] = field(default_factory=dict)  # relpath -> content
    input_files: dict[str, str] = field(default_factory=dict)     # overrides on base tree
    input_base_sha: str = ""      # "" means "the delivered output tree"
    solution_files: dict[str, str] = field(default_factory=dict)
    solution_base_sha: str = ""
    golden_diff: str = ""
    golden_rationale: str = ""
    notes: list[str] = field(default_factory=list)

    def to_task_json(self, verifier_command: str) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "instruction": self.instruction,
            "provenance": self.provenance.to_json(),
            "difficulty": self.difficulty,
            "difficulty_rationale": self.difficulty_rationale,
            "files_in_scope": sorted(self.files_in_scope),
            "modules": sorted(set(self.modules)),
            "verifier": {
                "command": verifier_command,
                "selection": self.verifier_selection,
                "working_directory": "input/ (agent edits here); the harness "
                                     "overlays verifier/overlay before running",
            },
            "notes": self.notes,
        }


# --------------------------------------------------------------------------
# leak / prescription checks
# --------------------------------------------------------------------------

_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CODEY = re.compile(r"[=<>!+\-*/%(){}\[\]:]|\bself\b|\breturn\b")

# Phrases that tell the agent what to type rather than what must be true.
_PRESCRIPTIVE = [
    (re.compile(r"\bchange\s+\S+\s+to\s+\S+", re.I), "names a specific edit"),
    (re.compile(r"\breplace\s+.{0,40}\bwith\b", re.I), "names a specific edit"),
    (re.compile(r"\b(add|insert|remove|delete)\s+(a\s+)?(line|call|import|"
                r"parameter|argument|keyword)\b", re.I), "prescribes a code edit"),
    (re.compile(r"\bon line \d+", re.I), "points at a line number"),
    (re.compile(r"```"), "contains a code block"),
    (re.compile(r"\bshould (?:now )?(?:call|invoke|use)\s+`?\w+\(", re.I),
     "names the implementation to call"),
]


def _tokens(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN.findall(text)]


def _added_lines(diff: str) -> list[str]:
    out = []
    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            body = line[1:].strip()
            if body and not body.startswith("#"):
                out.append(body)
    return out


def leak_report(instruction: str, golden_diff: str, run_length: int = 6) -> dict:
    """Detect copied code fragments and solution-only identifiers.

    ``run_length`` is in tokens: six consecutive identifiers shared with an
    added line is far past coincidence for prose, while staying tolerant of a
    task that legitimately names the function it is about.
    """
    added = _added_lines(golden_diff)
    if not added:
        return {"leaked": False, "shared_runs": [], "code_lines_quoted": []}

    instr_tokens = _tokens(instruction)
    instr_joined = " ".join(instr_tokens)

    shared_runs: list[str] = []
    for line in added:
        toks = _tokens(line)
        for i in range(0, max(0, len(toks) - run_length + 1)):
            window = " ".join(toks[i:i + run_length])
            if window and window in instr_joined:
                shared_runs.append(window)

    quoted: list[str] = []
    normalised_instruction = re.sub(r"\s+", " ", instruction)
    for line in added:
        if len(line) < 12 or not _CODEY.search(line):
            continue
        if re.sub(r"\s+", " ", line) in normalised_instruction:
            quoted.append(line[:120])

    return {
        "leaked": bool(shared_runs or quoted),
        "shared_runs": sorted(set(shared_runs))[:10],
        "code_lines_quoted": sorted(set(quoted))[:10],
    }


def prescription_report(instruction: str) -> dict:
    hits = [{"pattern": pat.pattern, "why": why, "match": m.group(0)[:100]}
            for pat, why in _PRESCRIPTIVE
            for m in [pat.search(instruction)] if m]
    return {"prescriptive": bool(hits), "hits": hits}


def instruction_quality(instruction: str, golden_diff: str) -> dict:
    """Everything an automated reviewer can say about one instruction."""
    leak = leak_report(instruction, golden_diff)
    pres = prescription_report(instruction)
    words = len(instruction.split())
    problems: list[str] = []
    if leak["leaked"]:
        problems.append("instruction overlaps the reference patch")
    if pres["prescriptive"]:
        problems.append("instruction prescribes an implementation")
    if words < 40:
        problems.append("instruction is too short to be self-contained")
    return {
        "word_count": words,
        "leak": leak,
        "prescription": pres,
        "problems": problems,
        "ok": not problems,
    }


# --------------------------------------------------------------------------
# text helpers shared by the miners
# --------------------------------------------------------------------------

def sanitise_subject(subject: str) -> str:
    """Strip conventional-commit noise and trailing issue refs from a subject."""
    text = re.sub(r"^\s*(\w+)(\([^)]*\))?!?:\s*", "", subject).strip()
    text = re.sub(r"\s*\(#\d+\)\s*$", "", text).strip()
    text = re.sub(r"\s*#\d+\b", "", text).strip()
    return text or subject.strip()


def looks_like_code(text: str) -> bool:
    """True when a phrase reads like source rather than English."""
    return bool(re.search(r"[(){}\[\]]|->|==|!=|>=|<=|\w+\.\w+\(", text))


def titlecase(text: str, limit: int = 80) -> str:
    """Capitalise and shorten, cutting on a word boundary.

    A hard slice produces titles like "...will not be operate", which reads as a
    typo rather than as a truncation.
    """
    text = " ".join(text.split()).rstrip(".")
    if not text:
        return "Untitled task"
    text = text[0].upper() + text[1:]
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return (cut or text[:limit].rstrip()) + "…"


def any_dict(obj: Any) -> dict:
    return obj if isinstance(obj, dict) else {}
