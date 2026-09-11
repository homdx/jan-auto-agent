# V16 — Pass C: a bare name in a source-file summary means the symbol defined *here*

**Severity:** HIGH  
**File:** `tools/collect/verifier.py`  
**Symbol:** `extract_claims`, `_prefer_citable_homonyms`  
**Round:** 28 of 28  
**Size:** S  
**Source:** found while validating V11 (`V11/REPORT-V11.md`, "homonyms")  
**Depends on:** V11 (merged as `9406601`)  

---

**Priority:** High · **Size:** S · **Files:** `tools/collect/verifier.py`,
`tests_bugfix/test_collect_verifier_source_homonyms.py`
**Depends on:** V11. Independent of M2 / V8 / V9 / V12 — can be the very next
round after V11 is merged, and should be: every round after this one that
measures Pass C output (M3, M4, M6) reads a number this bug distorts.

## The bug

`extract_claims` turns a bare name in a summary sentence into a `Claim` for
**every** module that defines that name — `_symbol_patterns` is keyed by full
qualname, so `load_events` mentioned in the summary of `analyze_logs.py` yields
two claims: `analyze_logs.py:load_events` (passes `citation_check`) and
`tools/auto/view_trace.py:load_events` (fails it — other module). Then
`verify_claims`'s sink-or-swim rule drops the whole sentence because one of its
claims failed. The summary named exactly the symbol defined in the file being
summarised and lost the sentence for it.

V11 fixed this for **test files only**: `_prefer_citable_homonyms` keeps, for
each short name, only the qualnames whose module is in `citable_modules` — and
`verify_repo` hands a `citable_modules` set only to test files. For a source
module `citable_modules` is `None`, the helper returns the symbols unfiltered,
and the pre-V11 behaviour stands.

Measured on `25d5de8` (clean tree, `git ls-files '*.py'`, `main` excluded):

```
source modules                                        110
  with an own public symbol that has a homonym          20
  distinct such names                                   22   (load_events, build_parser,
                                                              load_truth, list_py_files, …)
static proxy (see "Measurement" below):
  source modules given a synthetic purpose               97
  claims                                                344
  dropped — every one a homonym drop                     83  (24%)
  purposes emptied entirely                              13
```

The 13 are e.g. `analyze_logs.py`, `check_improvements.py`,
`tools/auto/view_trace.py`, `scripts/harvest_report.py`,
`scripts/sync_test_tiers.py`. These are the "9 source-file drops" V11 was told
to leave alone — V11 left them alone; this ticket is where they get fixed.

## Do

1. In `verify_repo`, give **every** module a `citable_modules` set, not only
   test files:
   - test file (`test_paths.is_test_path`): `{own path} ∪ import_edges[path]`
     — unchanged from V11;
   - source module: `{own path}`.
   
   That is the whole fix: `_prefer_citable_homonyms` then resolves
   `load_events` in `analyze_logs.py` to `analyze_logs.py:load_events` alone,
   and `citation_check` sees a same-module claim. No new rule, no new helper.
2. `citation_check` must still behave as today for a source module: the citable
   set `{own path}` is exactly what `citable_modules is None` meant. The
   "or any module it imports" wording in the drop detail must appear only when
   the set has more than the own path — keep the detail text for source files
   byte-identical to the current one (`.collect/` diffs and the M-tickets'
   reason counters read it).
3. `total_claims` in `verify_repo` is counted through `extract_claims` with the
   **same** `citable_modules` as `verify_module` uses — V11 already threads the
   set into both call sites; do not add a third path that forgets it. Invariant:
   `kept_count == total_claims − dropped_count` for every module.
4. A bare name that is defined **nowhere in the module being summarised** and
   has homonyms elsewhere keeps *all* its claims (the helper's "no citable
   sibling → keep all" branch) — so it still drops, as a wrong citation should.
   Do not weaken that branch.
5. `extract_claims(..., citable_modules=None)` — the public default — keeps
   returning the unfiltered list. Callers outside `verify_repo` (tests, ad-hoc
   scripts) that never pass the set see no change.

**Not in scope:** the `path:line` location check stays same-module;
`is_safe()` / provenance untouched; no change to what a test file may cite;
`CollectBridge._shrink` untouched.

## Acceptance

- [ ] Static proxy on the merged tree: **0** source purposes emptied (from 13),
      **0** claims dropped (from 83), kept 316 — record the numbers in the commit message.
- [ ] A source summary naming a symbol from another module it does not define
      still drops (`REASON_NO_CITATION`), with the *same* detail string as
      before this ticket.
- [ ] A test file's behaviour is bit-identical to V11: the V11 proxy still
      reports 0 empty purposes; `tests_bugfix/test_collect_verifier_test_file_citations.py`
      passes unmodified.
- [ ] `kept == total − dropped` holds per module in `verify_repo`'s report.
- [ ] New: `tests_bugfix/test_collect_verifier_source_homonyms.py`, at least:
      1. `analyze_logs.py`-shaped case — two modules define `load_events`; the
         summary of module A says "`load_events` reads the trace"; kept, not
         dropped. **Must fail on the parent commit.**
      2. same fixture, module A's summary names a symbol only B defines → drops
         with `REASON_NO_CITATION` and the pre-ticket detail text.
      3. three modules define `load`; A's summary names it → exactly one claim
         extracted for A (assert `total_claims == 1`), `kept_count == 1`.
      4. a test file importing A, naming `load_events` → still kept (V11 path
         unchanged), and a test file importing neither A nor B → still dropped.
      5. `extract_claims(text, path)` with no `citable_modules` still returns
         every homonym qualname (public default unchanged).
- [ ] Both real roots GREEN, run one after the other; tiers in sync
      (`python3 scripts/sync_test_tiers.py` if you add anything under `tests/`).

## Measurement — the static proxy

The acceptance number does not need an LLM run. Give every source module a
synthetic Pass B purpose naming up to five of its own multi-word public symbols
(what a real summary does), run `verify_repo`, and count. On the parent commit
every drop this reports is a homonym drop, because the purpose names nothing
but symbols defined in the file.

```python
# v16proxy.py — run as: PYTHONPATH=. python3 v16proxy.py <repo-root>
# Use a clean worktree (git worktree add --detach), never a tree with
# untracked V*/ candidate dirs — scan_repo picks them up.
import sys
from pathlib import Path
from tools.collect.scanner import scan_repo
from tools.collect.model import LLMSummary
from tools.collect.test_paths import is_test_path
from tools.collect import verifier

root = Path(sys.argv[1]); ms = scan_repo(root)
summ = []; n = 0
for m in ms:
    if is_test_path(m.path):
        summ.append(m); continue
    names = []
    for s in m.public_symbols:
        short = s.qualname.split(":")[-1]
        if s.is_private or "." in short: continue
        if ("_" in short or short != short.lower()) and short not in names:
            names.append(short)
    if not names:
        summ.append(m); continue
    n += 1
    summ.append(m.with_llm_summary(LLMSummary(purpose="Provides " + ", ".join(names[:5]) + ".", notes="")))
sources = {m.path: (root / m.path).read_text(errors="replace") for m in ms}
v, rep = verifier.verify_repo(summ, sources, root=root)
empty = [m.path for m in v if m.summary is not None and not m.summary.purpose]
print(f"source modules with a synthetic purpose: {n}; empty after Pass C: {len(empty)}; "
      f"kept {rep['kept_count']} dropped {rep['dropped_count']}")
print("examples:", empty[:8])
```

Parent (`25d5de8` / `9406601`): `97; empty 13; kept 261 dropped 83`.
Target: `97; empty 0; kept 316 dropped 0` — `kept` is below 344 because the
duplicate homonym claims are no longer *counted*, not because anything drops
(verified on a scratch worktree with the one-line change: exactly these numbers).

## How the final patch is validated (the review protocol for this round)

Every candidate — including the one that gets merged — goes through the same
sequence, one candidate at a time, never two in parallel:

1. **Shape.** `git am` onto `9406601` in a detached worktree; exactly one
   commit; touches `tools/collect/verifier.py` and the new test file (plus
   `cli.py` only if it must — it should not). No edits to `epic-tasks/`.
2. **`_shrink` gate.** AST of `CollectBridge._shrink` identical to parent.
3. **Tiers.** `python3 scripts/sync_test_tiers.py --check` (or the hook)
   reports nothing to do.
4. **New test fails on parent.** Check out the new test file alone onto
   `9406601`; case 1 must fail, the rest may pass.
5. **Suites**, sequentially, in the candidate's worktree:
   ```bash
   python3 -m pytest tests -q --timeout=180 && python3 -m pytest tests_bugfix -q --timeout=180
   ```
6. **Proxy, twice.** `v16proxy.py` on the candidate tree → must print
   `empty 0 … dropped 0`. Then the V11 proxy (`V11/` report, test-file
   version) → must still print `empty after Pass C: 0`, proving the test-file
   path did not move.
7. **Detail-string diff.** For one source module citing an unimported homonym,
   print `report["dropped"][…]["detail"]` on parent and on candidate — they
   must be equal.
8. **Optional end-to-end**, only if a stub provider is at hand: copy
   `agents_128k.ini` to a scratch path, point `base_url` at the stub
   (`proxy2/` → `proxy-stub/` with full JSON logging, or any local stub),
   `--rebuild` into a scratch `.collect/` (never over the user's own), and
   confirm `analyze_logs.py` / `tools/auto/view_trace.py` come out with a
   non-empty purpose. This is confirmation, not the acceptance — the proxy is.

Winner = passes 1–7; ties broken by the smallest diff to `verifier.py`
(the fix is a one-line change of what `verify_repo` builds for source modules
plus the detail-text guard — a candidate that adds a new helper or a filename
check has misread the ticket).

---

## Ground rules — these are the scoring criteria

1. **Verify before implementing.** Grep the live source for what this ticket
   claims. It was written against `tickets` at `9406601`; if the code has
   moved, the ticket is the stale one — say so and adapt rather than forcing it.
2. **`CollectBridge._shrink` is not to be modified, reordered, or replaced.**
   New work runs *before* it, never instead of it. A diff that touches a line
   inside `_shrink` fails this round regardless of what else it does.
3. **One local commit for this ticket alone.** Never `git push`. No
   opportunistic refactors bundled in.
4. **Ships a test.** A fix to something that used to be wrong →
   `tests_bugfix/`. If you add a `tests/test_*.py`, run
   `python3 scripts/sync_test_tiers.py` so the `.smoke_tests/` mirror exists
   (a pre-commit hook enforces it).
5. **Run the two real roots, one after the other, never combined** —
   `.smoke_tests/` and `.regression_tests/` are symlink views onto `tests/`
   and run nothing extra; combining roots in one `pytest` call produces ~362
   false errors from a conftest collision:
   ```bash
   python3 -m pytest tests -q --timeout=180 && python3 -m pytest tests_bugfix -q --timeout=180
   ```
   `python` is not on PATH — use `python3`. One suite at a time — the machine
   is shared with the other round entrants.
6. **Fail open.** A broken artifact, a malformed config key or an absent model
   degrades to "no collect data" and never raises into a run. Everything added
   here inherits that.
7. **Never widen provenance.** Static facts and LLM prose stay separately
   labelled. `is_safe()` keeps reading only `guarded_accesses` /
   `FAIL_OPEN_REGISTRY` / `CONTRACTS`.
8. **Never run anything against a live provider config.** Copy
   `agents_128k.ini` to a scratch path and point `base_url` at a stub before any
   measurement run.
