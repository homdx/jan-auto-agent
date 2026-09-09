# L2 — `probe_config` must say `stale`, not `no_artifact`

**Severity:** HIGH  
**File:** `tools/auto/architect.py`  
**Symbol:** `—`  
**Round:** 1 of 24  
**Size:** XS  
**Source:** `docs/collect-epics/EPIC-L-live-findings.md` § L2  
**Also touches:** `tools/auto/collect_bridge.py`  

---

**Priority:** High · **Size:** XS · **Files:** `tools/auto/architect.py`,
`tools/auto/collect_bridge.py`

Do this one first. It is the smallest ticket in the whole set and it protects
every measurement that comes after it.

### The defect

`architect.py:975` traces one reason string for two different situations:

```python
if bridge is None or not getattr(bridge, "usable", False):
    logger.warning(
        "architect: probe_enabled=true but no fresh collect artifact is "
        "available ([collect] use_in_auto) — planning without probes this run.",
    )
    self._trace_probe_config(usable=False, reason="no_artifact")
    return None
```

`CollectBridge.usable` is `False` whenever `status != "fresh"`
(`collect_bridge.py:139-142`) — which covers `"stale"` **and** `"absent"`. The
log line says "no fresh collect artifact", which is accurate; the traced
`reason` says `no_artifact`, which is not.

### What it cost, live

`../testtext7` ran the whole plan phase with `usable=False reason=no_artifact`
while `testtext7/.collect/artifact.json` sat on disk at 2.28 MB. It was stale —
artifact `Sep 9 00:11`, newest tracked source `Sep 9 23:13` — and one
`--collect --refresh` would have fixed it. Instead the run produced 459
candidates (the most of the five) with **zero** probe operations, against four
sibling runs that made 763 between them. Nothing in `run.log`, `progress.json`
or the trace says the data was recoverable.

Any A/B comparison between those runs is invalid, and the invalidation is
invisible.

### Do

1. Give `CollectBridge` a public read-only `status` property returning
   `"fresh"` / `"stale"` / `"absent"` — it already reads
   `getattr(self._model, "status", "absent")` inside `usable`; lift that to a
   property and have `usable` call it. No behaviour change.
2. In `_build_probe`, branch the reason:
   ```python
   reason = "stale_artifact" if bridge is not None and bridge.status == "stale" else "no_artifact"
   ```
   and make the `logger.warning` say which, naming
   `--collect --refresh` when it is stale.
3. `reason="bridge_error"` at `architect.py:967` is unchanged.

### Acceptance

- [ ] A stale model traces `reason="stale_artifact"`; an absent one still
      traces `reason="no_artifact"`. Both still trace `usable=False`.
- [ ] The stale warning names `--collect --refresh`; the absent one does not.
- [ ] `CollectBridge.status` on an absent model returns `"absent"` and raises
      nothing. `usable` is unchanged for all three values.
- [ ] `analyze_logs.py` still parses `probe_config` events with the new reason
      (it must not key off the exact string, or it is fixed in the same commit).
- [ ] New: `tests_bugfix/test_probe_config_stale_vs_absent.py`.

### Out of scope

Making a stale artifact usable, refreshing it automatically, or changing
`staleness = warn`. The stance that stale is treated exactly like absent is
deliberate and documented in `collect_bridge.py`'s module docstring — this
ticket changes only what the operator is told.

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
