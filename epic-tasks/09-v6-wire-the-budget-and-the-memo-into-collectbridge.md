# V6 — Wire the budget and the memo into `CollectBridge`

**Status:** landed — see `git log --grep 'V6:'` (budget passed to the assembler, per-run memo dropped by `invalidate()`, `[collect] pack_enabled`; the key is documented in README and `agents_256k.ini` — add `pack_enabled = true` to your own `agents_128k.ini` under `max_context_chars_auto`, unindented)  
**Severity:** HIGH  
**File:** `tools/auto/collect_bridge.py`  
**Symbol:** `—`  
**Round:** 9 of 27  
**Size:** S  
**Source:** `docs/collect-epics/PLAN-v2.md` § V6  

---

**Priority:** High · **Size:** S · **Files:** `tools/auto/collect_bridge.py`,
`agents*.ini` *(v1: A9 + A11, one boolean instead of seven keys)*

### Verified against `2f9005d` (2026-09-12) — read this first

- **This is now the ticket that makes L6 real.** `build_collect_context_block`
  takes `budget=` and L6's row-shrinking loop is in place, but
  `CollectBridge.context_for` (`tools/auto/collect_bridge.py`) still calls it
  **without** `budget` — so in a live run every block is assembled unbudgeted
  and any overshoot goes straight to `_shrink` (an LLM call that may cut fact
  rows). PLAN-v2 §6 says V6 "can be dropped"; that is wrong for step 1 and
  right only for the memo.
- V9 landed first: `context_for` now checks `_is_dirty(target_file)` before
  the budget. Keep that order, and the memo from step 2 must be **dropped for a
  path when `invalidate()` marks it dirty** — a memoised pre-edit block is
  exactly the bug V9 fixed. A test for that belongs in the ticket.
- "`pack_enabled = false` restores the V2 block (today's three rows)" is
  stale: the three-row block no longer exists. Redefine: `false` renders only
  the rows that carry no neighbourhood facts — `contract`, `config_read`,
  `public_symbols` — i.e. the pre-V3 shape. `use_in_auto` stays the master
  switch; `pack_enabled` is the finer one and M5 may use either.
- Row caps named below already live as module constants in
  `context_assembler.py`; do not re-introduce them.

**Do**

1. Pass `budget=self._max_context_chars` into `build_collect_context_block`.
   `context_for` keeps its exact three-line shape; **do not** touch `_shrink`,
   its overshoot tolerance, its logging, its truncation notice or `shrink_calls`.
2. Add a per-run memo keyed by `(target_file, budget)`. The same file is a target
   in many tasks; today each one re-assembles and, when over budget, re-pays an
   LLM shrink call. The bridge is already build-once-per-run, so the memo is
   safe and bounded by the number of distinct target files.
3. One config key, in `[collect]`:
   ```ini
   # COLLECT-USE: per-task fact pack. Rows are emitted highest-value-first until
   # max_context_chars_auto is spent; public_symbols ranks last because the coder
   # already has the target file's full source in the same prompt.
   pack_enabled = true
   ```
   `false` restores the V2 block (today's three rows). Caps (5 callers, 5
   callees, 3 tests, 4 fail-open sites, 3 neighbours, 110 purpose chars) are
   module constants with a comment, not ini keys — no one is going to tune them,
   and each one would be a config-guard test.
4. `context_for_many` keeps budgeting each file independently.

**Characterization test — the guard on `_shrink`**

`tests/test_collect_bridge_shrink_contract.py`, pinning today's behaviour:
summarizer within `budget * 1.15` → returned verbatim; beyond it → hard
truncation with the `[+N chars truncated by CollectBridge]` notice, overshoot
logged; summarizer raises → hard truncation, failure logged;
`summarizer_call is None` → hard truncation, `shrink_calls` stays 0;
`shrink_calls` increments once per attempt including failed ones.

**Acceptance**

- [ ] `git diff` touches no line inside `_shrink`.
- [ ] The characterization test passes before and after this ticket.
- [ ] A second `context_for("x.py")` in one run makes zero additional LLM calls.
- [ ] `pack_enabled = false` reproduces the V2 block exactly.
- [ ] A malformed value warns once and uses the default.
- [ ] Blocks over budget: record the new figure against the M1 baseline (50/477).

---

### Stage 2 — producer: cost and honesty

Independent of Stage 1. Each is a small, self-contained win.

---

## Ground rules — these are the scoring criteria

1. **Verify before implementing.** Grep the live source for what this ticket
   claims. It was written against `competition` at `68b78a0`; if the code has
   moved, the ticket is the stale one — say so and adapt rather than forcing it.
2. **`CollectBridge._shrink` is not to be modified, reordered, or replaced.**
   New work runs *before* it, never instead of it. A diff that touches a line
   inside `_shrink` fails this round regardless of what else it does.
3. **One local commit for this ticket alone.** Never `git push`. No
   opportunistic refactors bundled in.
4. **Ships a test.** New behaviour → `tests/`; a fix to something that used to
   be wrong → `tests_bugfix/`. If you add a `tests/test_*.py`, run
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
