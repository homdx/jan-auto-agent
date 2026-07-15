#!/usr/bin/env python3
"""Self-play: run the real collect-mode pipeline against jan-auto-agent's
own repo with a deliberately hallucinating Pass B (`llm_call`) stub, to see
what the Pass C verification gate does and does not catch.

This is NOT a mock of the pipeline -- it calls the actual
tools.collect.cli.action_collect(), which runs the actual scanner (Pass A),
the actual summarize_repo() (Pass B, fed by our stub llm_call instead of a
real network call to Ollama), and the actual verify_repo() (Pass C), then
writes real .collect/ artifacts.

The stub plays five distinct hallucination patterns per module, one
sentence each, mixed into `purpose`:

  1. HONEST      - a plain, ungrounded but *harmless* generic description
                    (kind="generic", no citation at all -> always survives;
                    this is the documented, accepted gap: unfalsifiable
                    prose is not what COLLECT-17 claims to catch).
  2. FAKE-SYMBOL - invents a symbol name that exists nowhere in the repo.
  3. FAKE-LINE   - cites a real file but a wildly out-of-range line number.
  4. CROSS-MODULE- cites a real symbol/location that belongs to a
                    DIFFERENT module than the one being summarized (the
                    bug fixed in this session).
  5. FALSE-CRASH - claims a real, indexed access in this module crashes,
                    when Pass A's own dataflow proved it's guarded.
"""
import configparser
import json
import sys
from pathlib import Path

sys.path.insert(0, ".")

from tools.collect.cli import action_collect
from tools.collect.scanner import scan_repo

ROOT = Path(".").resolve()
TARGET_SUBDIR = "tools/collect"  # keep the self-play run fast and legible

# ── figure out real, in-repo facts to build "true-sounding" lies from ──────
all_modules = scan_repo(ROOT, config=None)
by_path = {m.path: m for m in all_modules}
target_modules = [m for m in all_modules if m.path.startswith(TARGET_SUBDIR + "/")]
extra = by_path.get("tools/prompt_store.py")  # has a real GUARDED site to test FALSE-CRASH against
if extra:
    target_modules.append(extra)
print(f"Pass A scanned {len(all_modules)} module(s) repo-wide, "
      f"{len(target_modules)} under {TARGET_SUBDIR}/ will get (fake) Pass B calls.\n")

# Pick one real symbol from a totally unrelated module, for the CROSS-MODULE lie.
foreign_module = by_path.get("tools/backoff.py") or next(
    m for m in all_modules if m.path != "" and m.public_symbols and not m.path.startswith(TARGET_SUBDIR)
)
foreign_symbol = foreign_module.public_symbols[0].qualname if foreign_module.public_symbols else None
print(f"Cross-module lie will borrow: {foreign_symbol} (really defined in {foreign_module.path})\n")

CALL_LOG = []


def hallucinating_llm_call(system: str, user: str) -> str:
    """Stands in for the real Ollama call `summarizer._make_llm_call` would
    make. `user` is the actual Pass B prompt (`build_summary_prompt`), so we
    can read the real facts block back out of it to target our lies at a
    real module -- exactly what a genuinely confused/creative local model
    would have in front of it when it started inventing things."""
    # Recover which module this call is for from the facts block's
    # "path: ..." line (first line of `facts` block, not first line of the
    # whole prompt -- the prompt starts with a fixed intro sentence first).
    module_path = None
    for line in user.splitlines():
        if line.startswith("path: "):
            module_path = line[len("path: "):].strip()
            break
    module = by_path.get(module_path)

    sentences = [
        # 1. HONEST-BUT-UNFALSIFIABLE generic sentence — no citation, so
        #    Pass C has nothing to check; documents the accepted gap.
        "This module is part of the collect-mode ground-truth pipeline.",
        # 2. FAKE-SYMBOL — invented, exists nowhere.
        f"It relies on {module_path}:_totally_invented_helper_fn for caching.",
        # 3. FAKE-LINE — real file, absurd out-of-range line.
        f"See {module_path}:999999 for the retry policy.",
    ]
    if foreign_symbol:
        # 4. CROSS-MODULE — real symbol, wrong module.
        sentences.append(
            f"This module implements {foreign_symbol}, defined at "
            f"{foreign_module.path}:1."
        )
    if module and module.guarded_accesses:
        ga = next((g for g in module.guarded_accesses if g.status == "GUARDED"), None)
        if ga:
            # 5. FALSE-CRASH — Pass A already proved this access is guarded.
            sentences.append(
                f"{ga.access} will crash with an unguarded IndexError at "
                f"{ga.location}."
            )

    purpose = " ".join(sentences)
    reply = json.dumps({"purpose": purpose, "notes": ""})
    CALL_LOG.append((module_path, purpose))
    return reply


# ── run the real pipeline, scoped to TARGET_SUBDIR via a throwaway root ────
# action_collect scans the whole `root` tree, so to keep this self-play run
# fast we point it at a tmp copy containing only tools/collect/*.py plus the
# handful of modules the cross-module lie needs to resolve against.
import shutil
import tempfile

tmp_root = Path(tempfile.mkdtemp(prefix="collect_selfplay_"))
for m in target_modules + ([foreign_module] if foreign_module else []):
    dst = tmp_root / m.path
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / m.path, dst)
(tmp_root / "tools" / "__init__.py").touch()
(tmp_root / "tools" / "collect" / "__init__.py").touch()

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
print("=== surviving `purpose` text per module (what actually reached the artifact) ===")
for rec in modules_json["modules"]:
    if rec.get("summary"):
        print(f"- {rec['path']}: {rec['summary']['purpose']!r}")

print(f"\n(artifact written under {collect_dir})")
