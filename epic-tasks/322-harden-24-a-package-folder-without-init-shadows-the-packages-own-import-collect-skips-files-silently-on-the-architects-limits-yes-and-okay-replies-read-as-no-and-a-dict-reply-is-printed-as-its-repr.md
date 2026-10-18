# HARDEN-24 — an own-root `__init__.py` shadows `src/<pkg>/<folder>/` imports; `--collect` skips files silently on the Architect's depth and size limits; `reply_is_yes` reads `Yes.`, `okay`, `Да` as no; `reply_text` prints a dict as its repr and a `None` as an empty line

**Status:** open
**Severity:** MEDIUM (A: a wrong edge in the import graph; B: files missing from the model with no word of it; C: an approval read as a rejection by the gate); LOW (D: a log line)
**File:** `tools/collect/graph.py` (A), `main.py` and the collect settings (B), `tools/llm_stream.py` (C, D)
**Symbol:** A: `resolve_import`. B: the collect limits read from `[search]`. C: `reply_is_yes`. D: `reply_text`
**Round:** (set when the round starts)
**Size:** S
**Depends on:** HARDEN-7 (the two readers this extends)
**Also touches:** `tests_bugfix/` pins of HARDEN-7 A, `tests/`

---

## Why

Outside reviews of the collect stage and of Gate 2 reported four more bugs; each was re-run on the current head and reproduced.

---

## Part A — `src/<pkg>/<folder without __init__.py>/` is resolved to the package's `__init__.py`

### What happens today

`src/c/__init__.py` exists, `src/c/core.py` too, and `src/c/tests/test_c.py` imports `c.core` from a folder `tests/` that has no `__init__.py` of its own. `resolve_import("c.core", index, "src/c/tests/test_c.py")` returns `src/c/__init__.py`: the importer's own root is looked up first, the folder without an `__init__` is treated as the root of the package, and `c.core` collapses to the package file. The edge the graph draws is the wrong file, so a change to `core.py` does not select the test that imports it.

### What it does

An importer's own-root rule applies only to a folder that really is a package root (it holds the `__init__.py` the import names, or the dotted name's first part is a folder next to the importer). A dotted name that is found whole in the index (`src/c/core.py`) wins over the shortened one.

### Tests

The repro above returns `src/c/core.py`; `import c` still returns `src/c/__init__.py`; a flat layout and a `src/` layout keep every edge they had; a folder with its own `__init__` is unchanged.

---

## Part B — `--collect` shares the Architect's `max_depth` and `max_file_kb` and skips files without a word

### What happens today

The collect stage reads `max_depth = 2` and `max_file_kb = 500` from `[search]`, which are the Architect's retrieval limits. A repo whose sources sit three folders deep, or a 600 KB generated file, is missing from the structural model, the log says nothing, and the manifest does not name the file.

### What it does

* `--collect` reads its own `[collect]` limits (defaults at least as wide as today's); the Architect's keep theirs.
* Every skipped file is logged once with the reason (too deep, too large) and the manifest lists the skipped files.

### Tests

A tree three folders deep is collected with the collect default; the same tree under `[search] max_depth = 2` still limits the Architect; a skipped file is logged and listed.

---

## Part C — `reply_is_yes` reads `Yes.`, `yes!`, `okay`, `Да`, `ok, approved`, `Approved.` as no

### What happens today

HARDEN-7 A made the reader fail-closed: only a known set of words is a yes. The set is matched against the whole stripped string, so a trailing full stop or an exclamation mark, the word `okay`, the Russian `Да` and `ok, approved` fall outside it and the gate treats an approval as a rejection (a retry loop that never ends).

### What it does

Trailing `.`, `!`, quotes and spaces are removed before the match; `okay`, `ok`, `да`, `approved` and their upper-case forms are yes; `ok, approved` is yes; `Yes, but …` and `No` stay no (fail-closed is kept).

### Tests

The six replies above are yes; `no`, `Not approved`, `Yes, but the date is wrong`, `""`, `None`, `0` stay no; the HARDEN-7 A pins stay green.

---

## Part D — `reply_text` renders a dict as a repr and a `None` as an empty line

### What happens today

`reply_text({"a": 1})` returns the Python repr `{'a': 1}` and `reply_text(None)` returns an empty string, so a log line or a prompt that quotes a model's field shows the repr or a blank where the reply held nothing.

### What it does

A dict or a list is rendered as compact JSON; `None` is rendered as an empty string that the caller can tell from `""` only through a marker the callers already use (`(none)`); a string is unchanged.

### Tests

The dict, the list, `None`, a number and a string.

---

## Contest

One commit. The judge's checks are written after the round, from the entries.

## Acceptance

**Layer 1 — offline.** The tests above; `.smoke_tests/ .regression_tests/` and `tests_bugfix -n 4` green unchanged.
**Layer 2 — the operator.** `--collect` on a repo with three folders of depth lists every skipped file; Gate 2 on `Yes.` approves.

## Not in scope

Rust, the verdict parser (HARDEN-17, 22), the key mask.
