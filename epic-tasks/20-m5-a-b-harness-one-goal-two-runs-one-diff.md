# M5 — A/B harness: one goal, two runs, one diff

**Status:** open (verified against `2f9005d`, 2026-09-12)  
**Severity:** HIGH  
**File:** `scripts/collect_ab.sh`  
**Symbol:** `—`  
**Round:** 20 of 27  
**Size:** M  
**Source:** `docs/collect-epics/EPIC-M-metrics.md` § M5  
**Depends on:** M4  
**Also touches:** `scripts/collect_ab.py`  

---

**Priority:** High · **Size:** M · **Files:** `scripts/collect_ab.sh` or
`scripts/collect_ab.py`
**Depends on:** M4

The only way to claim "the agent works better" is to run the same thing twice.

### Verified against `2f9005d` (2026-09-12) — two corrections

1. **Not `--dry-run`.** `MEASURE-BEFORE-AFTER.md` §"`--dry-run` skips the
   coder" is right: the collect block is built only in `Coder._build_prompt`,
   so a dry-run A/B measures nothing the pack does. Both runs must execute
   tasks. Do it against a local stub that answers every role (copy `proxy2/`
   to `proxy-stub/` and log requests to JSON, or the harness's own stub) —
   never a live provider. The switch is `[collect] use_in_auto` (exists today);
   `pack_enabled` is a finer switch that only exists once V6 lands, so this
   ticket depends on **M4** and optionally V6, not on V6 unconditionally.
2. "fixed seed": the run has no seed flag. Determinism comes from the stub —
   a stub that replays recorded answers keyed by prompt hash gives
   "two runs of A produce the same counters" for free; a live model never will.

### Do

1. One goal, one base tree, fixed seed, `--dry-run`, against the local stub —
   run A with `pack_enabled = false`, run B with `pack_enabled = true`.
   Everything else identical, including the config file, which the script
   copies and patches rather than editing in place.
2. Diff the Tier-2 counters:

```
                          pack off   pack on   delta
probe requests                  ___       ___
probe misses                    ___       ___     ← want down
context re-requests             ___       ___     ← want down
gate1 rejected: existence       ___       ___     ← want down (fewer phantom paths)
gate1 rejected: presence        ___       ___
gate2 attempts per task         ___       ___     ← want down
tasks blocked                   ___       ___
prompt chars per coder call     ___       ___     ← want up only a little
LLM calls, whole run            ___       ___
```

3. The pass condition is deliberately weak and stated up front: **probe misses
   and Gate-2 attempts do not increase, and prompt size grows by less than the
   pack's own budget.** A pack that makes the model ask *more* questions is a
   pack that added noise, and that is the failure this harness exists to catch.

### Acceptance

- [ ] Reproducible: two runs of A produce the same counters.
- [ ] Never touches `agents_128k.ini` — copies to a scratch path and patches
      the copy. (Do not point a measurement run at a live provider config.)
- [ ] Output committed alongside the baseline JSON.

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
