# KC-50 — a harvest that is already `REWORK` on its mechanical facts runs no pytest root and does not hold the round's test lock

**Status:** queued — found live 2026-09-23 in round 86 (run 3, base `4634507`): `hy3` ended its first turn after 41 s with a clean tree and no commit, and its harvest ran the four pytest roots on the untouched base under `_TEST_RUNS_LOCK` for more than five minutes at load 22, while `agnes-2-5-flash`, also clean and with no commit, waited in `HARVESTING` behind it.
**Severity:** MEDIUM (every model that stops early, and every one whose turn a gate reject ends, holds up the one lock that every finished entry needs, and does it to learn nothing: the verdict was `REWORK` before the first test ran)
**File:** `tools/contest/harvest.py`, `tools/contest/runner.py`
**Symbol:** `harvest` (the `if run_tests:` block), `_harvest`, `_TEST_RUNS_LOCK`
**Round:** 94
**Size:** S
**Source:** round 86, `contest-out/86/state.json` and `ps` at 21:17:

| agent | how its turn ended | commits above base | `git status` | state |
|---|---|---|---|---|
| `hy3` | `session.idle` at +41 s, `finish: stop` after one `bash` call | 0 | clean | `HARVESTING`, `pytest .smoke_tests` running in `rounds/86-hy3` |
| `agnes-2-5-flash` | `session.idle` at +92 s, after 3 of 4 `bash` calls came back `gate-failed` / `empty reply` (KC-37) | 0 | clean | `HARVESTING`, waiting on the lock |
| `mimo-v2-5` | `session.idle` at +212 s | **1** | clean | `HARVESTING` at 21:18, a real entry waiting on the lock behind both |

`harvest` builds its reasons in order. `no_progress_row`, `commits_ne_1`
(0 commits) and `no_test_file` are already on the list, all `blocking`, when
`if run_tests:` calls `run_tests_detail`: `tests`, `tests_bugfix`,
`.smoke_tests` and `.regression_tests` on the base tree, about 9 min on this
box under load (`tests` 345 s, `bugfix` 199 s idle-ish). Their result cannot
change the verdict. The only thing it can add is a `tests_failed` tail about
the *base*, which then goes into the rework prompt as if it were the agent's
fault. Meanwhile `_harvest` holds `_TEST_RUNS_LOCK` for the whole call, so the
round's other harvests queue behind it. The runner's own docstring already
says the zero-commit case needs "no harvest and no pytest" (`_commits_above`,
KC-21), but only the STALLED/ERROR edge follows that. The idle edge to
`HARVESTING` does not.
**Depends on:** KC-16 (`run_tests=True`, the four roots, landed `1304950`), KC-21 (`_commits_above`, landed `f5a9f05`).
**Also touches:** `tests/test_contest_harvest.py`, `tests/test_contest_runner.py`

---

## What must change

### 1. The roots run only for a verdict they can decide

In `harvest`, when `run_tests` is set and at least one **blocking** reason is
already on the list, the roots are not run. `facts["tests_run"]` becomes
`"skipped: <first blocking code>"`, for example `"skipped: commits_ne_1"`. No
`tests_failed` reason is added. The verdict is `REWORK` exactly as before, with
the same reasons except the skipped `tests_failed`.

When no blocking reason is on the list, the roots run exactly as today.

### 2. The lock is taken only around the roots

`_harvest` no longer wraps the whole `harvest` call in `_TEST_RUNS_LOCK`. The
lock moves to the one place that needs it: `harvest` takes a `test_lock`
argument (a context manager, default `contextlib.nullcontext()`), and holds it
only around `run_tests_detail`. The runner passes `_TEST_RUNS_LOCK`. The
mechanical part (the `git` calls and `judge_worktree`) runs outside the lock,
so a mechanical-only harvest never waits behind another agent's roots.

### 3. The rework prompt says why no tests ran

When the roots were skipped, `rework_message` adds one line under the blocking
bullets: `The test roots were not run: fix the items above first.` That way an
agent is not told its tests passed or failed when neither happened.

## Out of scope

- A clean tree after an idle getting a `continue` instead of a rework: that is KC-9 and KC-42.
- The gate rejects that ended `agnes-2-5-flash`'s turn: that is KC-37.
- How long the roots take under load (`--max-parallel`): that is the operator's.

## Acceptance

- [ ] `tests/test_contest_harvest.py`, with `run_tests_detail` patched to record its calls:
  - a worktree at the base, no commit and no PROGRESS row, `run_tests=True` → `run_tests_detail` never called, verdict `REWORK`, `facts["tests_run"] == "skipped: no_progress_row"` (the first blocking code), no `tests_failed`;
  - one commit, a DONE row, a test file and `_shrink` the same → `run_tests_detail` called once, exactly as today;
  - only a non-blocking `off_ticket_files` reason → the roots run;
  - `test_lock` is entered only around `run_tests_detail`: a lock that records enter/exit sees one pair when the roots run and none when they are skipped.
- [ ] `tests/test_contest_runner.py`: two agents harvesting at once, one at the base with no commit and one with a READY-shaped commit. The first one's harvest finishes without waiting on the lock while the second holds it (a fake `run_tests_detail` that blocks on an event).
- [ ] `rework_message` for a skipped harvest carries the "not run" line. For a harvest that ran, the text is unchanged.
- [ ] Every existing harvest and runner test passes unmodified.
- [ ] `python3 -m pytest tests -n 4 -q --timeout=180` then `python3 -m pytest tests_bugfix -n 4 -q --timeout=180`, sequentially, both green; `CollectBridge._shrink` byte-identical.
