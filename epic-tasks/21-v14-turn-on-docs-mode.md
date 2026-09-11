# V14 — Turn on docs mode

**Status:** open (verified against `2f9005d`, 2026-09-12)  
**Severity:** MEDIUM  
**File:** `tools/auto/context_assembler.py`  
**Symbol:** `—`  
**Round:** 21 of 27  
**Size:** S  
**Source:** `docs/collect-epics/PLAN-v2.md` § V14  
**Depends on:** V2–V5  

---

**Priority:** Medium · **Size:** S · **Files:** `agents*.ini`,
`tools/auto/context_assembler.py` **Depends on:** V2–V5 *(v1: C6)*

`make_collect_bridge` already selects the flag by `task_mode`; only the switch
is off. The one consumer where Pass B's prose is exactly the right material is
the one consumer that is disabled.

**Do**

1. A second tuple, `_PACK_ROWS_DOC`, selected by `task_mode`:
   ```python
   ("neighbours", "purpose", "entry_point", "calls_into", "callers", "tests",
    "config_read", "public_symbols")
   ```
   Note `purpose`: in docs mode the **target's own** purpose is wanted, unlike
   code mode, because a docs task often writes *about* a file whose full source
   is not in the prompt. `entry_points` (382 entries, no reader today) becomes a
   row — documentation cares which modules are roots.
2. `use_in_doc = true` in `agents_128k.ini`.
3. Check the `existence` gate's rejection rate on a docs run does not rise; if
   the writer starts citing neighbour paths as the subject, prefix the row
   `(context, not the subject of this document)`.

**Acceptance**

- [ ] A docs-mode task gets a pack; a code-mode pack is unchanged.
- [ ] The `existence` gate's docs-run rejection rate does not increase.
- [ ] New: `tests/test_collect_pack_docs_mode.py`.

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
