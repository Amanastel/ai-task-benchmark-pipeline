"""Pull every number REPORT.md quotes out of the delivered artifacts."""
import json
import sys
from collections import Counter
from pathlib import Path

root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
repo = sys.argv[2] if len(sys.argv) > 2 else "glom"
okf = root / "output" / repo / ".okf"


def j(p, default=None):
    p = Path(p)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def jl(p):
    p = Path(p)
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] \
        if p.exists() else []


summ = j(root / "output" / "run-summary.json", {})
hyg = j(okf / "hygiene.json", {})
mut = j(okf / "mutation.json", {})
man = j(okf / "manifest.json", {})
tasks = j(root / "tasks.json", {})

print("### STAGE 1")
print("base image      :", hyg.get("base_image"))
print("packages pinned :", hyg.get("pin", {}).get("packages"),
      "| hashed:", hyg.get("pin", {}).get("hashed"),
      "| source:", hyg.get("pin", {}).get("source"),
      "| extras:", hyg.get("pin", {}).get("extras"))
print("lint mode       :", hyg.get("lint", {}).get("mode"),
      "| fixed:", hyg.get("lint", {}).get("fixed"),
      "| formatted:", hyg.get("lint", {}).get("formatted_files"),
      "| remaining:", hyg.get("lint", {}).get("remaining"),
      "| baselined:", hyg.get("lint", {}).get("baselined_rules"))
print("generated tests :", hyg.get("tests", {}).get("cases"),
      "(doctest", hyg.get("tests", {}).get("doctest_cases"),
      "/ value", hyg.get("tests", {}).get("value_cases"),
      "/ exc", hyg.get("tests", {}).get("exception_cases"),
      "/ dropped", hyg.get("tests", {}).get("dropped"), ")")
for r in hyg.get("runs", []):
    print(f"  run {r['label']:<16} exit={r['exit_code']} total={r['total']:<5} "
          f"passed={r['passed']:<5} failed={r['failed']} errors={r['errors']} "
          f"fp={r['fingerprint'][:12]} {r['duration_s']}s")
print("deterministic   :", hyg.get("deterministic"))

print("\n### MUTATION")
print("run:", mut.get("mutants_run"), "| killed:", mut.get("killed"),
      "| score:", mut.get("mutation_score"),
      "| by suite:", mut.get("killed_by_suite"))
print("killed only by generated:", len(mut.get("killed_only_by_generated_tests", [])))
print("survivors:", len(mut.get("survivors", [])))
for s in mut.get("survivors", [])[:5]:
    print("   -", s["file"], s["line"], s["operator"])

print("\n### STAGE 2")
print("counts:", man.get("counts"))
print("verification:", json.dumps(man.get("verification", {}).get("claims")),
      "| edges:", man.get("verification", {}).get("edges", {}).get("rate"),
      "of", man.get("verification", {}).get("edges", {}).get("checked"))
claims = jl(okf / "claims.jsonl")
print("claim kinds:", dict(Counter(c["kind"] for c in claims)))
print("unverified :", sum(1 for c in claims if not c["verification"]["verified"]))
hist = jl(okf / "history.jsonl")
print("commits:", len(hist), "| labels:", dict(Counter(h["label"] for h in hist)))
cov = j(okf / "coverage.json", {})
files = cov.get("files", {})
if files:
    tot = sum(f.get("num_statements", 0) for f in files.values())
    miss = sum(len(f.get("missing_lines", [])) for f in files.values())
    print(f"coverage: {tot} statements, {miss} missing -> "
          f"{100 * (tot - miss) / max(1, tot):.1f}%")

print("\n### STAGE 3")
print("counts:", tasks.get("counts"))
print("modules:", tasks.get("modules"))
print("rejected:", tasks.get("rejected_count"))
print("rejection reasons:")
for (src, why), n in Counter((r["source"], r["reason"][:70])
                             for r in tasks.get("rejected_candidates", [])).most_common(8):
    print(f"   {n:>3}  [{src}] {why}")
print("\ntasks:")
for t in tasks.get("tasks", []):
    print(f"  {t['id']:<40} {t['source_type']:<9} {t['difficulty']:<7} "
          f"cases={t['verifier_cases']:<3} {t['module']:<20} {t['validation_status']}")
