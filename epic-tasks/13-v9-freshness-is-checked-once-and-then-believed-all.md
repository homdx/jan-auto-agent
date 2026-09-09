# V9 — Freshness is checked once and then believed all run

**Severity:** HIGH  
**File:** `tools/auto/collect_bridge.py`  
**Symbol:** `—`  
**Round:** 13 of 24  
**Size:** M  
**Source:** `docs/collect-epics/PLAN-v2.md` § V9  
**Also touches:** `tools/auto/controller.py`  

---

**Priority:** High · **Size:** M · **Files:** `tools/auto/collect_bridge.py`,
`tools/auto/controller.py` *(v1: C8)*

`collect_bridge.py`'s own docstring promises that a stale model is treated
exactly like an absent one. `status` is computed once inside `load()` and the
bridge is then cached for the lifetime of the `Controller`. `--auto` edits and
commits source files. Reproduced against live code:

```
at run start:                      usable=True  status=fresh
after a task edits the tree:       usable=True  status=fresh
does the block know the new symbol?  False
header still reads:                "COLLECT MODEL (static facts, do not contradict):"
```

After Stage 1 the pack asserts *more* about a file than today's block does, so
the cost of believing it grows with every ticket above. This one adds no rows
and is still among the highest-value tickets in the plan.

**Do**

1. **Invalidate on write, not on a timer.** The controller already knows which
   files each task touched — it commits them. After a successful commit:
   ```python
   bridge.invalidate(paths)     # NEW
   ```
   `context_for` / `pull_symbol` / `module_symbols` return `""` for a dirty path.
   Clean paths are untouched: one edited file must not blind the pack for the
   other 468.
2. **Then, optionally, repair.** `[collect] auto_refresh_between_tasks`
   (default `false`): instead of blinding a dirty path, run the existing
   `action_module` for it — one module, one LLM call — and fold the fresh record
   back into the in-memory model.
3. Keep the build-once contract for the model *object*: patch records in place,
   never call `load()` twice. `test_collect_model_loaded_once_per_run` must keep
   passing unchanged — if it cannot, the design is wrong, not the test.
4. Emit `collect_miss(reason="dirty")` (M4) so a run reports how many blocks this
   suppressed.

**Acceptance**

- [ ] The reproduction above inverts: after an edit, `context_for` on that path
      returns `""` (or, with the flag on, post-edit facts).
- [ ] An unedited path keeps its block for the whole run.
- [ ] `tools.collect.loader.load` still called exactly once per run.
- [ ] With the flag on, a 5-task run where tasks 1 and 3 touch the same file
      gives task 3 the post-task-1 facts.
- [ ] New: `tests_bugfix/test_collect_bridge_stale_after_task_commit.py`.

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
