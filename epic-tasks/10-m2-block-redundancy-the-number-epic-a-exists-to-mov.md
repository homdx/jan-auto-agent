# M2 — Block redundancy: the number EPIC A exists to move

**Severity:** CRITICAL  
**File:** `scripts/collect_metrics.py`  
**Symbol:** `—`  
**Round:** 10 of 27  
**Size:** S  
**Source:** `docs/collect-epics/EPIC-M-metrics.md` § M2  
**Depends on:** M1  

---

**Priority:** Highest · **Size:** S · **Files:** `scripts/collect_metrics.py`
**Depends on:** M1

The claim driving EPIC A is *"the block describes the file the coder already
has"*. Right now that is an argument. This makes it a number.

### Do

For every module, split the rendered block into lines and classify each:

* **redundant** — the line's content is derivable from the target file's own
  source (`public_symbols`, the `module:` line, `config_read` — every one of
  these is visible by reading the file in the prompt);
* **new** — it is not (`callers`, `calls_into`, `tests`, `risk`, `owns_config`,
  `fails_open`, `neighbours`, `contract` — a contract lives in a registry, not
  in the file).

Report:

```
redundant chars / total chars     ___ %      (baseline: ~100%)
blocks with >=1 new row           ___ / 477  (baseline: 4 — the contract rows)
mean new rows per block           ___        (baseline: 0.008)
```

The classification is a static dict of `kind -> redundant|new` in the script.
It does not have to be clever; it has to be fixed, so before and after are
comparable.

### Acceptance

- [ ] Baseline recorded: redundant share, blocks with a new row, mean new rows.
- [ ] After EPIC A the same three numbers are recorded in the same table.
- [ ] The kind→class mapping lives in one dict with a comment saying why each
      kind is classified the way it is.

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
