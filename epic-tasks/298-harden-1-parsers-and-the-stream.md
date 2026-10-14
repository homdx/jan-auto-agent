# HARDEN-1 — four defects in what a model's reply can contain: the verdict parser, the stream chunk, the prompt store, a dotted reference

**Status:** open — FINAL, written 2026-10-14 against `e9e5d13`; every defect below was reproduced (`run`) or read in the code (`read`) on that commit. The code is the authority, not this ticket: if a line has moved, the symbol names the place.
**Severity:** MEDIUM (wrong answers on inputs nobody tested; none loses data on a normal run)
**File:** `tools/auto/inner_loop.py`, `tools/llm_stream.py`, `tools/prompt_store.py`, `tools/block_extractor.py`, `tools/search_agent.py`
**Symbol:** `_parse_verdict_soft`, `request_completion` (the OpenAI SSE branch), `PromptStore.get_store_summary`, `extract_block` / `find_references`
**Round:** —
**Size:** S
**Depends on:** CC review fixes 34–36 (landed, `c2ef95b`): this ticket starts from their code
**Also touches:** `tests/test_auto_verdict_boundary.py` and one pinned test per defect in `tests_bugfix/`

---

## Why

Two outside reviewers listed small defects; each entry here was checked against the code and **reproduced** or **read** (marked `run` / `read` below). Most of their other entries were already fixed or wrong and are refused in HARDEN-1's table. Each defect below is a wrong answer on an input nobody tested; each needs the guard the sibling code already has. A defect is done when it has its test and the suites are green unchanged (`.smoke_tests/ .regression_tests/`, then `tests_bugfix -n 4`).

## parsers and the stream (what a model's reply can contain)

### A1. The verdict parser reads `NOT BAD` as a rejection — `run`

**Where:** `tools/auto/inner_loop.py`, `_parse_verdict_soft`, the two `for token in ("REVISE", "REJECT", "NO")` loops (the whole reply and the per-line one).
**Evidence:** `startswith("NO")` has no word boundary. On the current code:

```
_parse_verdict_soft("NOT BAD")                       -> (False, 'T BAD', False)
_parse_verdict_soft("Nothing to add, looks good")    -> (False, 'thing to add, looks good', False)
_parse_verdict_soft("NOTE: fine")                    -> (False, 'TE: fine', False)
_parse_verdict_soft("Normally fine")                 -> (False, 'rmally fine', False)
_parse_verdict_soft("NOPE")                          -> (False, 'PE', False)
```

An approval that begins with a word starting `NO` becomes a REVISE whose reason is the word's tail, and that garbage goes to the Coder as the thing to fix:
a round burned on nothing. `NO: broken` and `NO, broken` must still reject.
**Fix:** the token matches as a word (`NO` followed by a non-letter or the end), in both loops, through one helper so the two cannot drift again. `REVISE` and
`REJECT` get the same boundary (`REVISED`, `REJECTION` are not verdicts).
**Test:** the five replies above are not a bare `NO` rejection (they fall to the later rules, which is the existing behaviour for prose: fail-open, approved);
`NO: x`, `No - x`, `NO` alone still `(False, …)`; a reply with the verdict on a later line behaves the same.

### A2. A streaming chunk with `choices: [null]` kills the call — `read`

**Where:** `tools/llm_stream.py`, `request_completion`, the `data:` branch (`choices[0].get("delta")`).
**Evidence:** `choices = chunk.get("choices") or []` then `choices[0].get(...)`: for `{"choices": [null]}` that is `None.get`, an `AttributeError`; the
`except` around it names only `json.JSONDecodeError` and `KeyError`, and the transport handler below it names only network errors. The call dies with every
token already read, and the caller retries (re-billing). `_meta_from_chunk` right under it already treats this shape (`isinstance(choices[0], dict)`).
**Fix:** skip a chunk whose `choices[0]` is not a dict, like `_meta_from_chunk`; a chunk that is not a dict at all is skipped too.
**Test:** a fake SSE stream with `{"choices":[null]}` between two content chunks returns the content of the other two; `[]`, `[null]`, `[1]`, `null` as
the chunk: no exception, the same text.

### A3. A stored prompt version without `version` takes `/prompts` down — `run`

**Where:** `tools/prompt_store.py`, `PromptStore.get_store_summary` (`stack[-1]["version"]`, `item["version"]`).
**Evidence:** `{"validator_agent": {"stack": [{"prompt": "x", "score": 0.5}]}}` → `KeyError: 'version'`; the REPL's `/prompts` has no handler, so one hand-edited
or older-format `prompts.json` ends the shell. A missing or `null` `score` is already shown as `(no score)` (fix 36); `version` was not given the same.
**Fix:** read `version` with a placeholder (`v?`) for a record that has none; the summary still lists the other records.
**Test:** a store with a record lacking `version` (first, middle, last) prints a table with `v?` and does not raise; a normal store prints as before.

### A4. A dotted reference can never be resolved — `run`

**Where:** `tools/block_extractor.py`, `extract_block` (and what `find_references` hands it, `tools/search_agent.py`).
**Evidence:** for `src = "def func(a): …"`, `extract_block(src, "func", ".py")` finds the definition and `extract_block(src, "module.func", ".py")` returns `''`.
A call through an imported module is the commonest Python reference, so the search reports it missing on every run and the validator is fed that as noise.
**Fix:** look a dotted name up by its **last segment** when the whole name is not defined (`module.func` → `func`; `Class.method` → the method inside the class
if the extractor can see it, else the name). The whole name still wins when it exists. Check what `find_references` emits first: if it already strips the
prefix for some languages, make the extractor and the emitter agree rather than fixing both.
**Test:** `module.func` finds `func`; `a.b.c` finds `c`; a name that is a real definition with a dot (a string key, not code) is unchanged; no match returns `''` as now.

---

## Contest

One commit. Score by the shared tests in `contest-bench/298/` (each defect's reproduction as a script that exits non-zero on the bug), not by the entries' own tests; cross-test every entry's tests against every other's code. A1 is the one that costs rounds: an approval that begins with `NO…` goes to the Coder as a defect. Judge A1 by hand; A2–A4 by the pinned tests. Free models are enough for A2–A4; A1 wants a strong one.

## Refused (shared by HARDEN-1 … HARDEN-4, so they are not opened again)

| from the lists | verdict | why |
|---|---|---|
| executor timeout kills only the shell | **already fixed** | `executor.py` starts the command in a session of its own and `os.killpg(SIGKILL)` on timeout |
| `/edit` ignores symlinks | **already fixed** | `_resolve_within_base` uses `realpath` + `commonpath` (fix 34) |
| `/edit` re-encodes non-UTF-8 files | **already fixed** | bytes and encoding kept, backup by `copy2` (fix 34) |
| `strip_json_fence` breaks a `` ``` `` in a string | **already fixed** | `_before_closing_fence` (fix 36) |
| root commit's changed files are `[]` | **false** | `git_manager.py` already passes `--root` to `diff-tree` |
| `ticket_status` reads without `encoding` | **false as stated** | `LANG=C … show 144` passes: Python's UTF-8 mode covers the C locale; only a non-UTF-8 locale would fail (add `encoding="utf-8"` if touched anyway) |
| URL/host ports read as line numbers | **not reproduced** | `app.py:8080` is a line citation by design; a host without a file suffix is not an anchor |
| malformed `noqa` in `gate1_grounding` | **false** | the comments are valid; ruff's `RUF100` says the rule `BLE001` is not enabled here |
| `append_finding` lock / `utf-8-sig` | **not worth it** | one reviewer, one file, written in turn; the tool writes no BOM |
| dead `Coder._is_truthy_delete` drops `"1"` | **cosmetic** | no caller; delete the dead copy if a part touches `coder.py`, no test |
| unreachable `HTTPError` handler in the validator | **not shown** | `request_completion` may well raise `RuntimeError`; not proved either way |

## Not in scope

Parts B, C, D of the same list are HARDEN-2 (299), HARDEN-3 (300), HARDEN-4 (301). The retry budget of `request_completion`; what the validator asks the search for.
