# KC-5 contest — black-box results (13 entries, 43 scenarios)

Method: `contest-bench` (see `contest-bench/README.md`) applied to KC-5 —
each entrant's `tools/contest/gates.py` + `harvest.py` + the rewritten
`scripts/judge_epic_round.py` applied at base `4ff7d14` (KC-5 open) in its
own worktree **outside the repo tree**, and exercised against a disposable
sandbox git repo built fresh per scenario (a base commit carrying a stub
`tools/auto/collect_bridge.py` with `def _shrink`, `pkg/`, `tests/`, a
ticket, and the real `scripts/append_task.py`; then the agent branch
`contest/44/agent` with whatever commits the scenario needs). The claim
(`runs/agent/PROGRESS.csv`) is written by the **real `append_task.py`**
wherever the scenario is about the happy path, because that is the contract
the runner will meet. Entrants' own tests are not what they are scored on
(one scenario runs them, as a fact). Harness: `run_one_kc5.py` (one scenario
per process, cwd = the entry worktree) + `run_all_kc5.py`; scores in
`results.json`; provenance in `entrants.json`.

Two passes: 39 scenarios from the ticket's acceptance list and the
`append_task.py`/`judge_epic_round.py` contracts; then, after reading the
code of the tied top group, 4 more (`s40`–`s45`) that split them.

Python on the scoring machine is 3.10 — that matters below.

## Score table

| scenario | ideal | sensenova68 | sonet5-var2 | deepseek | hy3-c2 | sensenova67v2-c3 | sonet5 | hy3 | sensenova67v2 | sonet5-var2b | sonet | glm47 | northmini | sonet4-var2 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| s01_api_symbols | ok | ok | ok | ok | ok | ok | ok | ok | ok | ok | ok | ok | **FAIL** | **ERR** |
| s02_script_is_thin_wrapper | ok | ok | ok | ok | ok | ok | ok | ok | ok | ok | ok | **FAIL** | **FAIL** | ok |
| s03_cli_golden | ok | ok | ok | ok | ok | ok | ok | **FAIL** | ok | ok | ok | **FAIL** | **FAIL** | **FAIL** |
| s04_cli_golden_with_tests | ok | ok | ok | ok | ok | ok | ok | **FAIL** | ok | ok | ok | **FAIL** | **FAIL** | **FAIL** |
| s05_cli_from_other_cwd | ok | ok | ok | ok | ok | ok | ok | **FAIL** | ok | ok | ok | **FAIL** | **FAIL** | **FAIL** |
| s06_cli_errors_identical | ok | ok | ok | ok | ok | ok | ok | **FAIL** | ok | ok | ok | **FAIL** | **FAIL** | **FAIL** |
| s07_judge_worktree_equals_old_judge | ok | ok | ok | ok | ok | ok | ok | ok | ok | ok | ok | **FAIL** | **ERR** | **ERR** |
| s08_moved_helpers_equal_old | ok | ok | ok | ok | ok | ok | ok | ok | ok | ok | ok | **FAIL** | **ERR** | **ERR** |
| s09_declared_files | ok | ok | ok | ok | ok | ok | **FAIL** | ok | ok | ok | ok | **FAIL** | **ERR** | **ERR** |
| s10_no_progress_row | ok | ok | ok | ok | ok | ok | ok | ok | ok | ok | ok | ok | **ERR** | **ERR** |
| s11_ready_via_append_task | ok | ok | ok | ok | ok | ok | ok | ok | ok | **FAIL** | **FAIL** | **FAIL** | **ERR** | **ERR** |
| s12_ready_literal_done | ok | ok | ok | ok | ok | ok | **FAIL** | ok | ok | ok | **FAIL** | **FAIL** | **ERR** | **ERR** |
| s13_short_sha_is_on_branch | ok | ok | ok | ok | ok | ok | ok | ok | ok | **FAIL** | **FAIL** | **FAIL** | **ERR** | **ERR** |
| s14_progress_not_done | ok | ok | ok | ok | ok | ok | ok | ok | ok | ok | ok | ok | **ERR** | **ERR** |
| s15_no_commit | ok | ok | ok | ok | ok | ok | ok | ok | ok | ok | ok | ok | **ERR** | **ERR** |
| s16_commit_not_on_branch | ok | ok | ok | ok | ok | ok | ok | ok | ok | ok | ok | **FAIL** | **ERR** | **ERR** |
| s17_commits_ne_1 | ok | ok | ok | ok | ok | ok | ok | ok | ok | ok | **FAIL** | ok | **ERR** | **ERR** |
| s18_no_test_file | ok | ok | ok | ok | ok | ok | ok | ok | ok | ok | ok | ok | **ERR** | **ERR** |
| s19_shrink_changed | ok | ok | ok | ok | ok | ok | ok | ok | ok | ok | **FAIL** | **FAIL** | **ERR** | **ERR** |
| s20_off_ticket_non_blocking | ok | ok | ok | ok | ok | ok | ok | ok | **FAIL** | **FAIL** | **FAIL** | **FAIL** | **ERR** | **ERR** |
| s21_tests_failed | ok | ok | ok | ok | ok | ok | ok | ok | **ERR** | ok | ok | **FAIL** | **ERR** | **ERR** |
| s22_tests_pass_with_run_tests | ok | ok | ok | **FAIL** | **FAIL** | ok | **FAIL** | **FAIL** | **ERR** | **FAIL** | **FAIL** | **FAIL** | **ERR** | **ERR** |
| s23_last_row_for_ticket_wins | ok | ok | ok | ok | ok | ok | ok | ok | ok | **FAIL** | **FAIL** | **FAIL** | **ERR** | **ERR** |
| s24_pushed | ok | ok | ok | ok | ok | ok | ok | ok | ok | ok | ok | ok | **ERR** | **ERR** |
| s25_types_and_limits | ok | ok | ok | ok | ok | ok | ok | ok | ok | ok | ok | ok | **ERR** | **ERR** |
| s26_everything_found_is_listed | ok | ok | ok | ok | ok | ok | ok | ok | **FAIL** | ok | **FAIL** | **FAIL** | **ERR** | **ERR** |
| s27_rework_message | ok | ok | ok | ok | ok | **FAIL** | ok | ok | **FAIL** | **FAIL** | **FAIL** | **FAIL** | **ERR** | **ERR** |
| s28_rework_message_only_blocking | ok | ok | ok | ok | ok | ok | ok | ok | ok | ok | ok | ok | **ERR** | **ERR** |
| s29_clone_kind | ok | ok | ok | ok | ok | ok | ok | ok | ok | ok | **FAIL** | ok | **ERR** | **ERR** |
| s30_real_git_worktree | ok | ok | ok | ok | ok | ok | ok | ok | ok | **FAIL** | **FAIL** | **FAIL** | **ERR** | **ERR** |
| s31_no_progress_still_reports_facts | ok | ok | ok | ok | ok | ok | ok | ok | ok | ok | **FAIL** | ok | **ERR** | **ERR** |
| s32_header_only_csv | ok | ok | ok | ok | ok | ok | ok | ok | ok | ok | ok | ok | **ERR** | **ERR** |
| s33_no_commits_at_all | ok | ok | ok | ok | ok | ok | ok | ok | ok | ok | ok | ok | **ERR** | **ERR** |
| s34_str_paths_accepted | ok | ok | ok | ok | ok | ok | ok | ok | ok | **FAIL** | **FAIL** | **ERR** | **ERR** | **ERR** |
| s35_declared_from_ticket_not_hardcoded | ok | ok | ok | ok | ok | ok | ok | ok | **FAIL** | **FAIL** | **FAIL** | **FAIL** | **ERR** | **ERR** |
| s36_tests_failed_tail_is_the_tail | ok | ok | ok | ok | **FAIL** | **FAIL** | ok | **FAIL** | **ERR** | ok | **FAIL** | **FAIL** | **ERR** | **ERR** |
| s37_tiers_check | ok | ok | ok | ok | ok | ok | ok | **FAIL** | **FAIL** | ok | ok | ok | ok | ok |
| s38_own_tests_pass | ok | ok | ok | ok | ok | **ERR** | ok | ok | ok | ok | ok | **FAIL** | **FAIL** | **ERR** |
| s39_stdlib_only_no_print | ok | ok | ok | ok | ok | ok | ok | ok | ok | ok | ok | ok | ok | ok |
| s40_failing_suite_runs_pytest_once | ok | ok | **FAIL** | ok | ok | ok | ok | ok | **ERR** | ok | ok | **FAIL** | **ERR** | **ERR** |
| s41_no_pytest_unless_asked | ok | ok | ok | ok | ok | ok | ok | ok | ok | **FAIL** | **FAIL** | **FAIL** | **ERR** | **ERR** |
| s42_commit_is_the_claimed_sha | ok | ok | ok | ok | ok | ok | ok | ok | ok | ok | ok | **FAIL** | **ERR** | **ERR** |
| s45_declared_files_missing_ticket_fails_loud | ok | ok | ok | **FAIL** | ok | ok | ok | ok | ok | ok | ok | **ERR** | ok | **ERR** |
| **total** | **43/43** | **43/43** | **42/43** | **41/43** | **41/43** | **40/43** | **40/43** | **36/43** | **34/43** | **33/43** | **26/43** | **15/43** | **3/43** | **3/43** |
`ok` = passed · `FAIL` = ran, wrong answer · `ERR` = crashed (import /
syntax / TypeError).

Columns, left to right: **ideal** (this branch), then the entries ranked.
`hy3` / `hy3-c2` and `sensenova67v2` / `sensenova67v2-c3` are the same
submission with one vs. all of its KC-5 commits applied (their second commit
was a KC-12 change bundled into the same patch — see `entrants.json`).
`kc5-sensenova-6-7.patch` is 0 bytes and was disqualified before scoring.
`kc5-sonet5-var2.patch` 13/13 is byte-identical to
`kc5-only-sonet5-var2-.patch` and scored once, as `sonet5-var2`.

## What each scenario checks

| # | scenario | what it checks |
|---|---|---|
| s01 | api_symbols | `gates.{judge_worktree,extract_shrink,ticket_for_round,git,run_tests,declared_files}`, `harvest.{Reason,Harvest,harvest,rework_message}` exist; both dataclasses frozen with the ticket's fields |
| s02 | script_is_thin_wrapper | the script imports `tools.contest.gates` and no longer defines the moved functions |
| s03–s06 | cli_golden / _with_tests / _from_other_cwd / errors_identical | `scripts/judge_epic_round.py --csv` stdout + CSV bytes identical to the pre-move script on a two-worktree temp round (one clean, one 2-commit/no-test/shrink-changed/off-ticket), with `--tests`, from a foreign cwd, and both exit-1 paths |
| s07–s08 | judge_worktree / moved helpers == old | the moved functions return exactly what the old module returns on the same inputs (rows, `extract_shrink` incl. `None`, `ticket_for_round`, `git` with/without `check`, `run_tests`) |
| s09 | declared_files | tuple of `**File:**` + `**Also touches:**`, `—` dropped, `str` path accepted, and **the same parse as `ticket_for_round` on all 51 real tickets** |
| s10–s24 | one scenario per reason code | `no_progress_row`, READY via the real `append_task.py --outcome DONE` (which writes `FIXED`), READY on a literal `DONE` row, a **7-char sha** in the row, `progress_not_done`, `no_commit`, `commit_not_on_branch` (a real side-branch sha and a bogus sha), `commits_ne_1` naming the number, `no_test_file`, `shrink_changed` naming `_shrink`, `off_ticket_files` non-blocking and naming the file, `tests_failed` with the tail, green suite with `run_tests=True`, last row for the ticket wins / other tickets ignored, `pushed` via a real bare remote |
| s25–s26 | types_and_limits / everything_found_is_listed | reasons is a tuple, codes from the ticket's set, text ≤ 200 chars, frozen; four defects at once → all four listed, no duplicates |
| s27–s28 | rework_message | both sentences, `Attempt 2 of 3`, `append_task.py`, `Also noted` only when there is a note, both ground rules, header, blocking before noted, no ticket text repeated |
| s29–s30 | clone_kind / real_git_worktree | a `git clone` and a `git worktree add` (`.git` is a file) both harvest |
| s31–s35 | edge cases | no CSV still reports the facts; header-only / empty CSV; 0 commits; `str` ticket path; declared list is read from the ticket, not hard-coded |
| s36 | tests_failed_tail_is_the_tail | the text carries pytest's own `1 failed` line and names the root |
| s37–s39 | mechanical | `sync_test_tiers.py --check` (the pre-commit hook), the entry's own new tests, stdlib-only / no `print` / no `sys.exit` in the library |
| s40 | failing_suite_runs_pytest_once *(pass 2)* | a sandbox `conftest.py` counts pytest sessions: a failing root must be run **once**, the tail taken from that run |
| s41 | no_pytest_unless_asked *(pass 2)* | `run_tests=False` starts no pytest session |
| s42 | commit_is_the_claimed_sha *(pass 2)* | `Harvest.commit` is the sha the row claims, verbatim |
| s45 | declared_files_missing_ticket_fails_loud *(pass 2)* | a missing ticket raises instead of returning `()` |

## Where the entries broke

| entry | score | what went wrong |
|---|---|---|
| **sensenova68** | 43/43 | nothing found. Both `DONE` and `FIXED` accepted (it read `append_task.py`), `ws.base_sha` used, one pytest run per root with the tail kept (`run_tests_detail`), a non-worktree path handled, `declared_files` and `ticket_for_round` share one parser, 36 own tests incl. the golden one against a frozen copy in `tests/fixtures/`. Extras beyond the ticket: `commit` fell back to the branch head, `rework_message` printed a `Blocking:` label and `- (none)`. |
| sonet5-var2 | 42/43 | `s40`: a failing root is run **twice** — `run_tests` for the summary, then `_tail_for_failed_roots` re-runs the root to get the tail (up to 2×180 s per root). Otherwise the cleanest code of the round. |
| deepseek | 41/43 | `s22`: with `run_tests=True` `facts["tests_run"]` stays `—` (tests ran separately, the row never learns); `s45`: `declared_files` on a missing ticket returns `()` silently. |
| hy3-c2 | 41/43 | `s22`: a **green** suite is reported as `tests_failed` — it keys on `"absent"`/anything non-PASS in the summary; `s36`: the text is the summary token, no pytest tail. The KC-5 smoke links + golden test only exist in a commit whose subject is KC-12. |
| sensenova67v2-c3 | 40/43 | `s27`: the ground rule about a test that fails without the change is missing from the message; `s36`: the `tests_failed` text is clipped to 200 chars, which cuts off pytest's own `1 failed` summary line; `s38`: its own golden test fails (`judge_worktree` grew a `tests_run=` parameter, so the row no longer matches the pre-move `judge`). Reason/Harvest/harvest live in `gates.py`, `harvest.py` re-exports. |
| sonet5 | 40/43 | `s09`: `declared_files` is a **different parser** from `ticket_for_round` (on ticket 38, with 10+ declared files, the two disagree — the runner and the operator would score different file sets); `s12`: a literal `DONE` row is rejected (`FIXED` only); `s22`: `facts["tests_run"]` stays `—`. |
| hy3 (commit 1) | 36/43 | as `hy3-c2` plus: the script has **no `sys.path` bootstrap** — `python3 scripts/judge_epic_round.py` dies with `ModuleNotFoundError: tools` from the repo root; no `.smoke_tests/` link so the pre-commit hook rejects the commit. |
| sensenova67v2 (commit 1) | 34/43 | `harvest(..., run_tests_flag=)` — the keyword is not the ticket's `run_tests` (TypeError for the runner); `commits_ne_1` emitted twice; off-ticket text says `1 file(s) outside declared ticket scope` without naming the file; no smoke link. |
| sonet5-var2b | 33/43 | only a literal `DONE` accepted — every row the real `append_task.py` writes (`FIXED`) is `progress_not_done`, so no agent can ever be READY. |
| sonet (= `910a19c`, HEAD of `kc`) | 26/43 | `harvest(ws, ticket_path, *, run_tests=False, base="HEAD")` — **`ws.base_sha` is never used**; with the default `base="HEAD"` the merge-base is HEAD itself, so every worktree is `0 commits`, `no_test_file`, never `shrink_changed`, never READY. Also `FIXED` only, no pytest tail, no `Also noted` section. |
| glm47 | 15/43 | `split("\\t")` (a literal backslash-t) in the moved numstat parse → 0 files always; `f"…{'\\u2717'}"` style escapes inside f-strings → `SyntaxError` on Python 3.10 (the script does not even start); `declared_files` returns `()`; 3 commits; its own test fails. |
| northmini | 3/43 | raw diff, no commit, no test, edits `epic-tasks/`; keeps `judge` (no `judge_worktree`), drops `ticket_for_round`; `Reason.__post_init__` is decorated with `@property`, so the dataclass calls `None()` → every `harvest` call is a `TypeError`; no `sys.path` bootstrap in the script. |
| sonet4-var2 | 3/43 | `SyntaxError` on Python 3.10: `f'{bad}✗'` nested in an f-string expression. Nothing importable. |
| kc5-sensenova-6-7.patch | — | 0 bytes. |

## The ideal patch (this branch)

Skeleton: **sensenova68** as-is (`gates.py`, `judge_epic_round.py`, the test
file and its `tests/fixtures/judge_epic_round_pre_kc5.py` golden copy).
`harvest.py` trimmed toward what the ticket asks and what `sonet5-var2`
showed was enough:

| change | from | why |
|---|---|---|
| `Harvest.commit` is the claimed sha or `None` — no fallback to the branch head | sonet5-var2 | the ticket defines `commit` as the claim; the head sha is already `facts["sha"]`, and a row without a commit is REWORK anyway, so the fallback (plus an extra `git rev-parse`) bought nothing |
| `rework_message`: header, one bullet per blocking reason, `Also noted:` only when there is a note, the two ground rules | sonet5-var2 | the ticket's exact shape; the `Blocking:` label and `- (none)` were extra — a REWORK message with no blocking reason cannot happen |
| `_rel()` helper replaced by the literal `runs/<agent>/PROGRESS.csv` | — | `Workspace.progress_csv` is defined as exactly that path |
| kept: `DONE` **and** `FIXED` mean done | sensenova68, sonet5-var2 | `append_task.py` rewrites `DONE` → `FIXED` before it reaches the CSV; the ticket says `DONE` |
| kept: `run_tests_detail()` — one pytest run per root, summary + last 20 lines | sensenova68 | `s40`: the tail must come from the run that failed, not a second run |
| kept: `declared_files` and `ticket_for_round` share `_declared_paths` | sensenova68 (sonet5-var2 had the same shape) | `s09`: one parse, so the runner and the operator score the same file set |
| kept: the non-worktree guard (`"commits" not in facts`) | sensenova68 | `judge_worktree` returns a short row for a non-git path; the naive `facts.get("commits") != 1` would print `None commits` |

Three test expectations changed with those edits (the `no_commit` case now
asserts `commit is None` and `facts["sha"]`; the `Also noted` order is checked
against the first bullet and the ground rules; the `- (none)` test became "no
`Also noted` without a note"). 36 tests, all green; `43/43` on this bench;
`tests` + `tests_bugfix` green (see the landing commit).
