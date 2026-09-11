# V7 — `--collect` on a stale tree goes incremental; fix the docs

**Status:** landed — `25d5de8`  
**Severity:** HIGH  
**File:** `tools/collect/cli.py`  
**Symbol:** `—`  
**Round:** 11 of 27  
**Size:** M  
**Source:** `docs/collect-epics/PLAN-v2.md` § V7  
**Also touches:** `main.py`, `README.md`  

---

**Priority:** High · **Size:** M · **Files:** `tools/collect/cli.py`, `main.py`,
`README.md`, `Collect.MD` *(v1: C1 + C2, one commit)*

482 wasted LLM calls per changed file, on the default path, documented
backwards.

| command | tree | LLM calls |
|---|---|---|
| `--collect` | 1 file changed | **483** |
| `--collect --refresh` | 1 file changed | **1** |

```
main.py:855    "--refresh: unconditional full rebuild, ignoring freshness."   WRONG
README.md:67   "--refresh forces a full rebuild"                              WRONG
Collect.MD:163 "--refresh (diff-driven incremental rebuild)"                  right
```

**Do**

1. `action_collect` on a stale tree delegates to `action_refresh`. On a fresh
   tree it stays the no-op it is.
2. Add `--rebuild` (and `/collect --rebuild`) for the unconditional full
   rebuild — today's `_full_build` path.
3. `--refresh` keeps working and keeps meaning what it means.
4. The result message says which path ran: `collect: incrementally refreshed 1
   changed module(s)` vs `rebuild: 469 module(s) re-summarized`.
5. Rewrite every help string and doc line to match, including `main.py`'s
   interactive `/collect` block and `README.md:486`'s worked example.

**Migration note:** a `collector_version` mismatch must still force a full
rebuild — `manifest.is_fresh` already handles it. This ticket changes the
*stale-tree* path only, not the *schema-mismatch* path.

**Acceptance**

- [ ] One file changed + `--collect` → 1 LLM call.
- [ ] `--collect --rebuild` → one call per module.
- [ ] Fresh tree + `--collect` → still a pure no-op, zero writes.
- [ ] `grep -rn "full rebuild"` over `main.py`, `README.md`, `Collect.MD`: every
      hit describes real behaviour.
- [ ] New: `tests_bugfix/test_collect_stale_is_incremental.py`.

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
