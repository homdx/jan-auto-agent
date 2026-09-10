# V13 — One safety note for the candidates V12 does *not* suppress

**Severity:** LOW  
**File:** `tools/auto/gate1_grounding.py`  
**Symbol:** `—`  
**Round:** 18 of 27  
**Size:** S  
**Source:** `docs/collect-epics/PLAN-v2.md` § V13  
**Depends on:** V12  

---

**Priority:** Low · **Size:** S · **Files:** `tools/auto/gate1_grounding.py`
**Depends on:** V12 *(v1: B3, two functions merged into one)*

Gate 1 already builds `collect_contract_note` and `existing_test_coverage_note`
for the Stage-B prompt. Add one more, for the ambiguous candidates V12
deliberately leaves alone:

```python
def already_handled_note(collect_bridge, cited) -> Optional[str]:
    """'Static analysis records this site as already guarded by <guard>.' or
    'This site fails open deliberately: <exception> — <rationale>.'

    The fail-open half renders only when a rationale exists — 22 of 117 sites
    have one, and an undocumented fail-open is not evidence of intent and must
    not be presented as if it were."""
```

**Acceptance**

- [ ] Undocumented fail-open produces no note.
- [ ] The note appears in the Stage-B prompt and is absent when the model is
      absent or stale.
- [ ] New: `tests/test_gate1_grounding_safety_notes.py`.

---

### Stage 4 — finish

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
