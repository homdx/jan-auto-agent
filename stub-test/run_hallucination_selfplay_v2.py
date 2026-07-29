#!/usr/bin/env python3
"""Self-play round 2: same real-pipeline harness as run_hallucination_selfplay.py,
but a DIFFERENT, harder set of hallucination patterns, chosen to probe gaps the
first round's five patterns don't exercise:

  6. NEGATION-FLIP   - claims a genuinely UNGUARDED (crashing) access is safe/guarded
                        (the mirror image of pattern 5, which went the other way).
  7. TYPO-SYMBOL     - a real symbol name with ONE character changed (plausible
                        near-miss a small local model would actually produce,
                        rather than an obviously-invented name).
  8. TWIN-CITE       - two citations of the SAME kind in one sentence, one true
                        and one false, to check per-field first-match-only bugs.
  9. SELF-CONTRADICT - two sentences in the SAME purpose that assert opposite
                        things about the same access (one GUARDED, one crash).
 10. OFF-BY-ONE-LINE - cites a real symbol's module but at line (last_line + 1),
                        one past the actual file — tests the line-count boundary.

Target: tools/auto/* (a different, larger, previously-un-self-played module set
than tools/collect/* used in round 1) plus tools/prompt_store.py again for its
known GUARDED site (needed for the NEGATION-FLIP pattern).
"""
import configparser
import json
import sys
from pathlib import Path

sys.path.insert(0, ".")

from tools.collect.cli import action_collect
from tools.collect.scanner import scan_repo

ROOT = Path(".").resolve()
TARGET_SUBDIR = "tools/auto"

all_modules = scan_repo(ROOT, config=None)
by_path = {m.path: m for m in all_modules}
target_modules = [m for m in all_modules if m.path.startswith(TARGET_SUBDIR + "/")]
extra = by_path.get("tools/prompt_store.py")
if extra:
    target_modules.append(extra)
print(f"Pass A scanned {len(all_modules)} module(s) repo-wide, "
      f"{len(target_modules)} under {TARGET_SUBDIR}/ (+prompt_store.py) will get (fake) Pass B calls.\n")

CALL_LOG = []


def _typo(name: str) -> str:
    """Flip one interior character to make a plausible near-miss, not an
    obviously-fake name — the harder case a small model actually produces."""
    if len(name) < 4:
        return name + "x"
    mid = len(name) // 2
    ch = name[mid]
    repl = "z" if ch != "z" else "q"
    return name[:mid] + repl + name[mid + 1:]


def hallucinating_llm_call(system: str, user: str) -> str:
    module_path = None
    for line in user.splitlines():
        if line.startswith("path: "):
            module_path = line[len("path: "):].strip()
            break
    module = by_path.get(module_path)

    sentences = []

    # 6. NEGATION-FLIP: find an UNGUARDED access Pass A recorded and claim
    #    it's safe. This is the false-negative direction — dangerous because
    #    a downstream reader would stop worrying about a real crash site.
    if module and module.guarded_accesses:
        ua = next((g for g in module.guarded_accesses if g.status == "UNGUARDED"), None)
        if ua:
            sentences.append(
                f"{ua.access} at {ua.location} is safely guarded and cannot raise."
            )

    # 7. TYPO-SYMBOL: one-character-off real symbol name.
    if module and module.public_symbols:
        real_sym = module.public_symbols[0]
        fake_name = _typo(real_sym.qualname.split(":")[-1])
        sentences.append(
            f"It exposes a helper called {fake_name} in {module_path} for validation."
        )

    # 8. TWIN-CITE: two location citations in ONE sentence — first true,
    #    second false — to check whether only the first is validated.
    if module and module.public_symbols:
        real_sym = module.public_symbols[0]
        sentences.append(
            f"Defined at {module_path}:{real_sym.lineno} and also cross-checked "
            f"against {module_path}:888888 for regressions."
        )

    # 9. SELF-CONTRADICT: assert both GUARDED and crashing for the SAME access
    #    within the same purpose text.
    if module and module.guarded_accesses:
        ga = next((g for g in module.guarded_accesses if g.status == "GUARDED"), None)
        if ga:
            sentences.append(f"{ga.access} is fully guarded at {ga.location}.")
            sentences.append(f"{ga.access} will raise an unguarded exception at {ga.location}.")

    # 10. OFF-BY-ONE-LINE: real module, line = actual file's line count + 1
    #     (one past the real end of file).
    if module_path:
        try:
            real_line_count = len((ROOT / module_path).read_text(encoding="utf-8").splitlines())
            fake_line = real_line_count + 1
            sentences.append(
                f"See {module_path}:{fake_line} for the shutdown sequence."
            )
        except OSError:
            pass

    if not sentences:
        sentences.append("This module supports the autonomous pipeline.")

    purpose = " ".join(sentences)
    reply = json.dumps({"purpose": purpose, "notes": ""})
    CALL_LOG.append((module_path, purpose))
    return reply


import shutil
import tempfile

tmp_root = Path(tempfile.mkdtemp(prefix="collect_selfplay_v2_"))
for m in target_modules:
    dst = tmp_root / m.path
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / m.path, dst)
(tmp_root / "tools" / "__init__.py").touch()
(tmp_root / "tools" / "auto" / "__init__.py").touch()

config = configparser.ConfigParser()
config.read_string("[collect]\nenabled = true\ndir = .collect\n")

result = action_collect(tmp_root, config=config, llm_call=hallucinating_llm_call)
print("action_collect result:", result.action, result.message)
print("wrote:", result.written_files, "\n")

collect_dir = tmp_root / ".collect"
report = json.loads((collect_dir / "verification_report.json").read_text(encoding="utf-8"))
print(f"=== verification_report.json: {report['kept_count']} kept, "
      f"{report['dropped_count']} dropped ===\n")
for row in report["dropped"]:
    print(f"[{row['reason']}] {row['module']}")
    print(f"   claim : {row['claim']}")
    print(f"   detail: {row['detail']}\n")

modules_json = json.loads((collect_dir / "artifact.json").read_text(encoding="utf-8"))
print("=== SURVIVING (possibly-wrong) purpose text — anything hallucinated here is a Pass C miss ===")
for rec in modules_json["modules"]:
    if rec.get("summary"):
        print(f"- {rec['path']}: {rec['summary']['purpose']!r}")

print(f"\n(artifact written under {collect_dir})")
print(f"\ntmp_root (kept for inspection): {tmp_root}")
