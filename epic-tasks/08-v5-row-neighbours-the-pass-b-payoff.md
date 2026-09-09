# V5 — Row: `neighbours` — the Pass B payoff

**Severity:** HIGH  
**File:** `tools/auto/context_assembler.py`  
**Symbol:** `—`  
**Round:** 8 of 24  
**Size:** M  
**Source:** `docs/collect-epics/PLAN-v2.md` § V5  
**Depends on:** V1, V3  

---

**Priority:** High · **Size:** M · **Files:** `tools/auto/context_assembler.py`
**Depends on:** V1, V3 *(v1: A8, unchanged in substance)*

483 LLM calls per full build produce `ModuleRecord.summary`. Its only readers
today are `render.py` (a 725 KB Markdown file for humans) and `verifier.py`
(which checks it). This is its first agent consumer.

**The design decision, stated so it is not quietly reversed:** the row carries
the purpose of the target's **neighbours**, never of the target itself. The
target's source is in the prompt; a paraphrase of it is noise. Its callers' and
callees' purposes are not in the prompt and cannot be derived from it.

**Do**

1. Up to 3 entries from `callers_of` + `calls_into`, in that order.
2. `summary.purpose`, cut at the first sentence or 110 chars, whichever is
   shorter.
3. Skip a neighbour with an empty purpose (228 modules today; V11 recovers ~219
   of them).
4. **Render `(llm)` on every line.** It is the only non-static row in the pack.
   COLLECT-1's provenance isolation is preserved by labelling, not by exclusion,
   and the label is what tells the model this line is weaker evidence than the
   ones above it.
5. It sits below `risk` in `_PACK_ROWS`: under budget pressure the static facts
   survive and the prose is what goes.

**Acceptance**

- [ ] The row renders `(llm)`; nothing else in the pack does.
- [ ] An empty purpose is skipped, not rendered blank.
- [ ] With no summaries in the artifact the row is absent and the rest of the
      pack is unaffected.
- [ ] Under a budget fitting half the pack, `neighbours` is dropped before
      `fails_open`.
- [ ] New: `tests/test_collect_pack_neighbour_purpose.py`.

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
5. **Run the suite as four separate invocations** — combining the roots in one
   `pytest` call produces ~362 false errors from a conftest collision:
   ```bash
   for d in tests tests_bugfix .smoke_tests .regression_tests; do python3 -m pytest "$d" -q --timeout=180; done
   ```
   `python` is not on PATH — use `python3`.
6. **Fail open.** A broken artifact, a malformed config key or an absent model
   degrades to "no collect data" and never raises into a run. Everything added
   here inherits that.
7. **Never widen provenance.** Static facts and LLM prose stay separately
   labelled. `is_safe()` keeps reading only `guarded_accesses` /
   `FAIL_OPEN_REGISTRY` / `CONTRACTS`.
8. **Never run anything against a live provider config.** Copy
   `agents_128k.ini` to a scratch path and point `base_url` at a stub before any
   measurement run.
