# KC-26 — `gates.run_tests_detail`: the failure count is read from a stats line `-qq` never prints, the tail is the last root's only, and a timing test that flakes under round load fails every harvest

**Status:** landed `6d10def` — by hand, no contest (the judge itself was wrong; a round cannot be scored by it until it is fixed). Round 65 of EPIC KC (`docs/kilo-contest/EPIC-KC.md`); found on 2026-09-20 in round 53 (KC-14 on hp-uz: five kenary free models, `python3 -m tools.contest run --ticket 53`).
**Severity:** HIGH
**File:** `tools/contest/gates.py` (`run_tests_detail`, `run_tests`)
**Symbol:** `run_tests_detail`, `_failures`, `_pytest`, `_SUMMARY_LINE`
**Round:** 65
**Size:** S
**Source:** `contest-out/53/state.json` — both agents that reached a harvest (agnes-2-0-flash `fb71b69`, step-3-7-flash `e1f67da`) got `REWORK: off_ticket_files, tests_failed`, twice each. The rework prompt in `agnes-2-0-flash.session.json` carries `the tests do not pass: tests:0✗ tests_bugfix:PASS .smoke_tests:0✗ .regression_tests:PASS` and a 20-line tail that is `.smoke_tests` only. The `tests` failures are visible in the agent's own pytest run inside the session: `test_three_questions_in_one_turn_stall_and_abort` (`assert elapsed < 10`, took 10.1 s) and `test_idle_event_timeout_stalls_a_silent_session_within_3s` (`assert elapsed < 3.0`, took 20.1 s) — timing assertions under the load of five sessions, their own pytest runs and `-n auto` on one box. No tree could pass; both rework attempts were spent on a failure that was not the tree's. `0✗` is the ini's `addopts = -q` plus the command's own `-q`: at verbosity `-2` pytest prints no `N failed in Xs` line (`TerminalReporter.summary_stats` returns early), the last stdout line is a `FAILED …` line, and both regexes miss — on this repo the count has been 0 since KC-5.
**Depends on:** KC-5 (`tools/contest/gates.py`, landed `0d91dd6`), KC-16 (`run_tests=True` in the runner, landed `1304950`).
**Also touches:** `tests/test_contest_harvest.py`

---

## What happens today

`run_tests_detail` runs `pytest <root> -q --timeout=180` per root, reads
the failure count from the last stdout line, and appends the whole stdout
of every failing root to one list it cuts to the last 20 lines at the end.
So: (1) with an ini `-q` the count is always 0 (`0✗`); (2) with two
failing roots only the last one's tail survives — in round 53 the root
that mattered (`tests`) was invisible; (3) a test that fails under load and
passes alone is a failure of the tree, and the agent is told to fix it.

## What must change

1. No `-q` from the command; the count is the stats line's when pytest
   printed one, else the number of `FAILED` / `ERROR` lines of the short
   test summary (`-rfE`, the default, prints them at every verbosity).
   Those lines also yield the node ids, with xdist's `@group` suffix and
   the ` - message` stripped. A non-zero exit with no failed test (usage
   error, collection error, interrupt) is `rc<N>✗` — never a silent `PASS`.
2. Each failing root contributes a `--- <root>` line and its **own** last
   `FAIL_TAIL_LINES` lines (stderr when stdout is empty).
3. A failing root is rerun once, only the failed node ids, serially
   (`-n0` when `xdist` is importable): if the rerun passes the token is
   `PASS*N` and the tail carries one line naming the flaky tests; a real
   failure stays `N✗`. `harvest` and the runner are unchanged — `"✗" in
   summary` still decides `tests_failed`.

## Acceptance

- [x] `tests/test_contest_harvest.py`: a temp repo with `addopts = -qq` and
      two failing tests → `tests:2✗`, tail begins `--- tests` and carries
      the marker; `tests` and `tests_bugfix` both failing → both tails, in
      order, under their own headers; a test that fails once and passes on
      rerun → `tests:PASS*1`, named in the tail, `READY` through `harvest`
      with `run_tests=True` and no `tests_failed`; a collection error →
      `✗` with the exit code, never `PASS`.
- [x] `test_run_tests_summary_and_tail_agree` (KC-5's golden: the passing
      summary is token-for-token the pre-KC5 script's) unmodified and green.
- [x] Landed `6d10def`; `python3 -m pytest tests` / `tests_bugfix` green
      sequentially; `run_tests_detail('.')` on the repo itself reports the
      four roots.

## Out of scope

- Making the timing tests of `tests/test_contest_runner.py` load-proof —
  they are the base's, not the judge's; a rerun alone is the judge's answer.
- A baseline run of the tests on the base commit before the round (so a
  base that is already red is not blamed on the agent) — a later ticket if
  a round ever needs it; `PASS*N` covers the case seen.
- KC-17 (`_declared_paths`, the `off_ticket_files` half of the same rework
  prompt) — landed by hand the same day, `6d038f9`.

## Self-check before `append_task.py` (required — every item, in the worktree you submit)

- [x] `python3 --version` on the judge is **3.10.12**; `python3 -c "import tools.contest.gates"`.
- [x] Exactly **one** commit on top of the base; only this ticket's work.
- [x] `git diff --stat <base>..HEAD` names only `tools/contest/gates.py` and `tests/test_contest_harvest.py`. Never `epic-tasks/`.
- [x] `python3 scripts/sync_test_tiers.py --check` is clean.
- [x] The new tests are red without the change (all four).
- [x] `python3 -m pytest tests -q --timeout=180` then `python3 -m pytest tests_bugfix -q --timeout=180`, sequentially, both green.
- [x] `CollectBridge._shrink` byte-identical; `scripts/judge_epic_round.py` untouched.

## Ground rules (same as every round)

- Do not touch `CollectBridge._shrink`.
- No test starts a real `kilo` or calls a live provider.
- Do not edit `epic-tasks/`.
- One commit, no push; a test ships with the change and fails without it.
