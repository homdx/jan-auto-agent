# L6 — the budget loop lets `public_symbols` displace the fact rows

**Severity:** HIGH  
**File:** `tools/auto/context_assembler.py`  
**Symbol:** `—`  
**Round:** 27 of 27  
**Size:** S  
**Source:** `docs/collect-epics/EPIC-L-live-findings.md` § L6  
**Depends on:** V3, V5. **Must not touch `CollectBridge._shrink`** — this is the  

---

**Priority:** High · **Size:** S · **Files:** `tools/auto/context_assembler.py`
**Depends on:** V3, V5. **Must not touch `CollectBridge._shrink`** — this is the
loop *above* it, `build_collect_context_block`, and only that.

### The observation

Measured on `tools/auto/coder.py` at `b609492`:

```
budget=1717  callers calls_into tests config_read public_symbols
budget= 400  callers calls_into       public_symbols      ← tests dropped
budget= 250  callers                  public_symbols      ← calls_into dropped
```

The V2 loop skips a row whole when it does not fit and moves on. Only
`public_symbols` knows how to shrink itself, so under pressure it is the row
that survives — and it is the one row the coder can already read from the
target's own source. The plan's rule ("static facts survive, prose goes") and
V5's acceptance both assume the opposite.

A second defect in the same loop: a row's lines are added to `seen` **before**
the budget check that may skip the row, so a row that never rendered still
suppresses identical lines in later rows.

### Do

1. Rows that carry a `+N` tail (`callers`, `calls_into`, `tests`, `neighbours`)
   shrink by dropping names from the end — down to the count alone — before
   the loop gives up on them. The count is the fact; names are the courtesy.
2. `public_symbols` may only take budget that no row above it could use: it is
   rendered last, into whatever is left, never at the cost of a row that would
   have fit in a shorter form.
3. `seen` is updated only for rows that actually render.
4. `_PACK_ROWS` order is unchanged. No new rows.

### Acceptance

- [ ] For `tools/auto/coder.py` against the live artifact, for every budget in
      `(1717, 900, 600, 400, 250)`: the set of rendered rows is a prefix of the
      row order plus `public_symbols` only if it fit *after* the prefix. Rows
      shrink to their count before disappearing.
- [ ] A larger budget never renders fewer rows, nor fewer names in any row,
      than a smaller one (monotonic).
- [ ] A row skipped for budget leaves no trace in `seen`: an identical line in
      a later row still renders.
- [ ] `tests/test_collect_context_block_rows.py` and V5's tests still pass
      unchanged except where they pinned the old displacement.
- [ ] New: `tests/test_collect_block_budget_keeps_fact_rows.py`.

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
