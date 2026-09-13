# M5 — A/B harness: one goal, two runs, one diff

**Status:** open (verified against `84a24b2`, 2026-09-13 — V6 landed, `pack_enabled` exists)  
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

### Verified against `84a24b2` (2026-09-13) — four corrections

1. **Not `--dry-run`.** `MEASURE-BEFORE-AFTER.md` §"`--dry-run` skips the
   coder" is right: the collect block is built only in `Coder._build_prompt`,
   so a dry-run A/B measures nothing the pack does. Both runs must execute
   tasks against a local stub that answers every role (copy `proxy2/` to
   `proxy-stub/` and log requests to JSON, or the harness's own stub) —
   never a live provider.
2. **The switch is `[collect] pack_enabled`** (V6, `76fd4bd`): `false` keeps
   the pre-V3 rows, `true` the full pack. Do **not** flip `use_in_auto` — that
   also removes the architect probe, so the two runs would differ in more
   than the pack.
3. **"fixed seed": the run has no seed flag.** Determinism comes from the stub
   replaying recorded answers. But a replay keyed by the *full prompt hash*
   cannot serve run B from run A's recordings — the coder prompt differs by
   exactly the block under test. Key coder replies by `(role, task_id, round)`
   or hash the prompt with the `COLLECT MODEL (static facts` … block cut out.
4. **Same plan in both runs.** The architect + gate-1 phase is upstream of the
   pack; on a live model it is non-deterministic (testtext5 vs testtext6
   differed by 52 candidates on one tree). Run the plan phase **once**, then
   seed run B's `.agent/` from run A's (`plan.json`, `progress.json`,
   `tickets/`) before its coding phase — the harness copies, it does not
   re-plan. Consequently `gate1 rejected: existence / presence` rows below are a
   **sanity check** (must be equal), not a measurement.
5. Non-`.py` tasks (`.ini`, `.md`, `.json` targets) get no block by
   construction and the coder loops on them (live: testtext6 T4 on four
   `skills/*.skill.ini`, three failed rounds). Until L1 lands, the harness
   drops them from the seeded plan the same way in both runs
   (`target_files` all non-`.py` → remove; verify no `dependencies` point at
   them) and reports how many it dropped.

### Do

1. One goal, one base tree, one plan, executing tasks against the local
   replay stub — run A with `[collect] pack_enabled = false`, run B with
   `pack_enabled = true`. Everything else identical, including the config
   file, which the script copies and patches rather than editing in place.
   Counters come from M4's `collect_*` events and the gate-1 split line, read
   from **every** `trace_*.jsonl` of the tree.
2. Diff the Tier-2 counters:

```
                          pack off   pack on   delta
probe requests                  ___       ___
probe misses                    ___       ___     ← want down
context re-requests             ___       ___     ← want down
gate1 rejected: existence       ___       ___     ← must be equal (same seeded plan)
gate1 rejected: presence        ___       ___     ← must be equal
gate2 attempts per task         ___       ___     ← want down
tasks blocked                   ___       ___
coder rounds per task           ___       ___     ← want down
tasks done / blocked            ___       ___
prompt chars per coder call     ___       ___     ← want up only a little (≤ max_context_chars_auto)
collect blocks / coder calls    ___       ___     ← A must be 0 rows-from-pack, B > 0
LLM calls, whole run            ___       ___
```

3. The pass condition is deliberately weak and stated up front: **probe misses
   and Gate-2 attempts do not increase, and prompt size grows by less than the
   pack's own budget.** A pack that makes the model ask *more* questions is a
   pack that added noise, and that is the failure this harness exists to catch.

### Acceptance

- [ ] Reproducible: two runs of A produce the same counters (replay stub, seeded plan).
- [ ] Depends on M4: uses its events; fails loudly if a trace has none.
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
