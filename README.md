# COLLECT-25 — Language dispatch + tree-sitter-java parser (Java 17+)

Implements the first task of Epic H (Java support) on top of the existing
`jan-auto-agent` `feature-collect` branch.

## What's in here

- `COLLECT-25-java-support.patch` — full unified diff, apply with
  `git apply COLLECT-25-java-support.patch` from the repo root (branch
  `feature-collect`), or `git am` if you want it as a commit.
- `tools/collect/lang.py` — new: extension -> language dispatch
  (`detect_language`, `Language.PYTHON` / `Language.JAVA`).
- `tools/collect/java_parser.py` — new: `tree-sitter` + `tree-sitter-java`
  wrapper (`parse_java`), same "recorded, not raised" failure contract as
  the Python `ast.parse` path. Chosen over `javalang` specifically because
  the grammar needs to cover Java 17+ (records, sealed types,
  pattern-matching `switch`).
- `tools/collect/model.py` — modified: `ModuleRecord` gets a
  `language: str = "python"` field (default keeps old artifacts/tests
  working unchanged).
- `tools/collect/scanner.py` — modified: `scan_repo` now walks both `.py`
  and `.java` files via the dispatcher instead of hardcoding `.py`; adds
  `scan_java_module` (parses Java files into empty-but-valid
  `ModuleRecord`s — symbol/import/except extraction is COLLECT-26/27, not
  in scope here).
- `tests/test_collect_lang_dispatch.py`, `tests/test_collect_java_parser.py`
  — new tests covering dispatch and parser behavior, including Java 17+
  constructs and tree-sitter's error-tolerant parsing of broken files.
- `requirements.txt` — new: `tree-sitter>=0.23`, `tree-sitter-java>=0.23`
  (optional — `.java` files degrade to a recorded `parse_error` if not
  installed; nothing about the Python-only path changes).

Note: applying the patch also updates
`tests/fixtures/collect_mini_repo_golden.json` — adding the `language`
field to `ModuleRecord.to_dict()` changes the canonical JSON byte output
that COLLECT-3's determinism test checks against, so the golden fixture had
to be regenerated. This is expected and intentional, not a hidden change.

## Verified

- `pytest tests/ -k collect` → 360 passed
- `pytest tests/` (full suite) → 2730 passed

## Install the optional dependency

```
pip install tree-sitter tree-sitter-java
```



Example run I just did in your clone
bash$ python main.py --collect --check
collect check: no manifest at .../\.collect/collect_manifest.json — collect has never run

$ python main.py --collect
collect collect: built 11 file(s) in .../.collect

$ python main.py --collect          # run again, nothing changed
collect collect: already up to date — nothing to do

$ echo "# comment" >> tools/collect/model.py   # simulate an edit
$ python main.py --collect --check
collect check: stale — a tracked file changed since the last collect run

$ python main.py --collect --refresh
collect refresh: tree unchanged — recomputed derived artifacts only, wrote 11 file(s)

$ python main.py --collect --module tools/collect/model.py
collect module: patched tools/collect/model.py and refreshed 11 file(s)
What gets written to .collect/
artifact.json            raw structural data (1.1MB in this repo)
collect_manifest.json    file hashes + git sha, used for freshness checks
MODULE_MAP.md            per-file symbol tables (signature, private?, docstring)
ARCHITECTURE.md          derived overview
CONFIG_MAP.md            where each config key is read
CONTRACTS.md             cross-module invariants (hand-seeded or derived)
GATES.md                 the pipeline's quality gates
FAIL_OPEN_REGISTRY.md    silently-swallowed exceptions found in the code
RISK_INDEX.md            per-module risk score (LOC, blast radius, unguarded access, etc.)
TEST_MAP.md              zero/thin test coverage per module
GLOSSARY.md              term definitions used across the other files
Example, from MODULE_MAP.md:
## `analyze_logs.py`
Imports: `argparse`, `collections`, `datetime`, ...
| symbol | signature | private | docstring |
| analyze_logs.py:_chapter_num | _chapter_num(...) | yes | Extract chapter number... |
Configuring it (agents.ini, [collect] section)
ini[collect]
enabled         = true      # master switch
dir             = .collect  # output dir (relative to project root)
use_in_auto     = false     # wire artifact into /auto (no-op until built)
use_in_doc      = false
use_in_bughunt  = false
staleness       = warn      # warn | refresh | ignore, on stale reads
llm_summaries   = true      # false = purely structural, no Pass B LLM prose
