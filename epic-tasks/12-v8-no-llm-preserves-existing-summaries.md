# V8 — `--no-llm` preserves existing summaries

**Severity:** HIGH  
**File:** `tools/collect/cli.py`  
**Symbol:** `—`  
**Round:** 12 of 27  
**Size:** M  
**Source:** `docs/collect-epics/PLAN-v2.md` § V8  
**Also touches:** `main.py`  

---

**Priority:** High · **Size:** M · **Files:** `tools/collect/cli.py`, `main.py`
*(v1: C3)*

Documented as "a purely structural build". It is a full rebuild that writes
`summary: null` for every module — 469 summaries destroyed, silently.

**Do**

1. `--no-llm` merges: modules keep whatever `summary` the previous artifact had,
   the same way `action_refresh` carries unchanged records forward.
2. Add `--drop-summaries` for today's destructive behaviour — a legitimate thing
   to want, just not silently.
3. A carried-forward summary whose module hash changed gets
   `provenance = "llm-stale"`. Add the value to the provenance enum and to
   `field_provenance()`; do not invent it locally.
4. `verification_report.json` is currently not written by a `--no-llm` build
   (11 files, not 12). With summaries preserved there *are* claims: either verify
   and write it, or write a report saying Pass C did not run. A missing file is
   the one option to avoid.

**Acceptance**

- [ ] `--collect --no-llm` after a normal build → summaries survive.
- [ ] `--collect --no-llm --drop-summaries` → 0 summaries, and says so.
- [ ] A changed module's carried-forward summary is marked stale.
- [ ] `.collect/` file count stable across build modes, or the difference is
      explained in the result message.
- [ ] New: `tests_bugfix/test_collect_no_llm_preserves_summaries.py`.

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
