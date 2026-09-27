# KC-50 — a harvest that is already `REWORK` on its mechanical facts runs no pytest root and does not hold the round's test lock

> ## Ticket audit — 2026-09-27 (branch head `b5257cf`)
> **Verdict: still needed, but partially implemented; the original no-commit example is stale.**
>
> - `harvest.py` now skips roots when `facts["commits"] == 0`, so round 86's specific base-tree pytest run no longer occurs. It does not record a `tests_run` skip reason.
> - A commit-bearing harvest still runs roots even if a blocking mechanical reason is already present (for example, no progress row or a commit count other than one).
> - `_harvest` still enters `_TEST_RUNS_LOCK` and the suite slot before calling `harvest`, so even a roots-skipped mechanical harvest queues behind tests and keeps the lock around git/judgment work. The intended improvement is narrower: skip roots for any pre-existing blocking reason and hold serialization only while roots execute.
>
> **The corrected status and current behavior below supersede the original round-86 description where it says the zero-commit case still runs pytest. No cancellation is warranted.**

**Status:** queued, partially implemented — the no-commit case now skips test roots (`harvest.py`, `facts["commits"] == 0`); remaining work is to skip roots for every pre-existing blocking reason, record the skip, and move the lock to the test call.
**Severity:** MEDIUM (a commit-bearing harvest that is already mechanically `REWORK` can still spend minutes running roots that cannot change its verdict, while every mechanical harvest can wait behind the round-wide test lock)
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

The original round-86 no-commit case is partly fixed: `harvest.py` now guards the test branch with `facts.get("commits") != 0`, so a base tree with zero commits does not run roots. The skip is not recorded in `facts["tests_run"]`. Other blocking facts can still waste test time: a commit-bearing tree with no progress row, multiple commits, or another blocking reason passes that guard and runs pytest even though the verdict is already `REWORK`.

`_harvest` still enters `_TEST_RUNS_LOCK` (and, when enabled, the suite slot) before `harvest` performs its mechanical git/judgment work. Thus the original multi-minute no-commit test run is gone, but a mechanically doomed commit-bearing harvest still runs or waits for roots and all mechanical-only harvests still queue behind that lock.
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
