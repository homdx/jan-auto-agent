# KC-32 — the contest tests stop failing on the operator's own box: no wall-clock bound under load, and `contest.local.ini` does not redden the roster test

**Status:** queued — after KC-31, last in the queue; found 2026-09-20 scoring round 60 (KC-21) on the judge's own checkout.
**Severity:** MEDIUM (a red `pytest tests` that is nobody's bug reaches every self-check and every harvest that runs the roots)
**File:** `tests/test_contest_roster.py` (`test_gate_profile_reads_the_committed_section`), `tests/test_contest_runner.py` (the `elapsed <` assertions)
**Symbol:** `test_gate_profile_reads_the_committed_section`, `test_ctrl_c_aborts_writes_state_and_propagates_then_resume_finishes`, `test_ctrl_c_during_retry_backoff_ends_the_round`
**Round:** 71
**Size:** S
**Source:** two reds that say nothing about the code.
(1) `test_gate_profile_reads_the_committed_section` loads `REPO_ROOT / "contest.ini"` through `load_roster`, which reads `contest.local.ini` next to it — git-ignored, where the operator's real gate `base_url`/`model`/`api_key` live (`contest.local.ini` here since 14:31 on 2026-09-20). The test then asserts the committed placeholders `https://example/v1` and `some/model` and fails on exactly the machines that are configured to run rounds; a fresh worktree (no local file) is green, which is why every agent's self-check passed and the judge's own `pytest tests` did not.
(2) The round's turn tests bound wall-clock: `assert elapsed < 2.0` in `test_ctrl_c_during_retry_backoff_ends_the_round`, `< 3.0`, `< 5`, `< 6`, `< 10` elsewhere. Under the load a round actually makes — eight agents, a harvest holding `_TEST_RUNS_LOCK`, another root running beside it — they fail: `test_ctrl_c_aborts_writes_state_and_propagates_then_resume_finishes` went red twice on 2026-09-20 (once on agnes-2-5-flash's tree mid-sweep, once on `542244b` + this round's tests) and passed on a quiet box three times in a row, same code.
**Depends on:** KC-6, KC-12 (the timing edges these tests cover).
**Also touches:** nothing in `tools/`

---

## What must change

1. `test_gate_profile_reads_the_committed_section` reads the **committed**
   file, not the resolved pair: parse `git show HEAD:contest.ini` into a temp
   file (or call `load_roster` with the local override suppressed) and assert
   the placeholders there. What the operator put in `contest.local.ini` is
   theirs; the test must not care.
2. The wall-clock bounds become bounds on the **behaviour**, not the box: keep
   an upper limit only where the test would otherwise hang (and set it to the
   config value it is proving, e.g. `idle_event_timeout_sec + a margin of 10 s`
   rather than a hard `2.0`), and assert the state machine — `aborted`,
   `state`, `last_error` — for the rest.
3. No test grows a `sleep`; the fake already drives every edge.

## Acceptance

- [ ] With a `contest.local.ini` that overrides `base_url`, `model` and
      `api_key`, `python3 -m pytest tests/test_contest_roster.py -q` is green;
      without one, it is green too.
- [ ] The three timing tests pass with a `nice -n 19` CPU hog on every core
      (run them once under load and once quiet).
- [ ] `python3 -m pytest tests -q --timeout=180 && python3 -m pytest tests_bugfix -q --timeout=180` green (sequentially).

## Out of scope

- `turn_timeout_sec` itself and the round's own budget — an operator's
  `contest.ini` decision (KC-21's Out of scope).
