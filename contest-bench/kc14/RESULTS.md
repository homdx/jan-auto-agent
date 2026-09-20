# KC-14 round — the claimed commit must be a sha, scored black-box

Ticket: `epic-tasks/53-kc14-harvest-accepts-only-a-hex-sha-as-the-claimed-commit-not-head-or-a-ref.md`
(round 53), base `0f4eb02`. Three entries, all hand-run Sensenova sessions
(`kc14/*.patch`); every one applied cleanly with `git am`, one commit, the
two declared files only, `_shrink` untouched, tiers clean, Python 3.10.

The manager round on hp-uz (`python3 -m tools.contest run --ticket 53`,
five kenary free models, `kc14/contest-out/53/`) produced **no entry** — and
not because of the models. Two of five reached a harvest with a commit on
the branch (agnes-2-0-flash `fb71b69`, step-3-7-flash `e1f67da`) and both
were sent back twice with `tests_failed` + `off_ticket_files`:

- `tests:0✗ … .smoke_tests:0✗` — the ini's `-q` plus the judge's own `-q`
  is `-qq`, at which pytest prints no stats line, so the count was 0 for
  any failure; the 20-line tail was `.smoke_tests`' only, so the `tests`
  failure was invisible; and that failure was two timing assertions of
  `test_contest_runner.py` (`elapsed < 3.0` at 20 s, `< 10` at 10.1 s)
  under five sessions and `-n auto` on one box — a load flake, not the tree.
  → **KC-26**, landed by hand `6d10def`: count from the short summary, a
  tail per root, the failed tests rerun alone before the tree is called
  broken (`PASS*N`). On this machine the same call now reports
  `tests:PASS*1 tests_bugfix:PASS .smoke_tests:PASS .regression_tests:PASS`
  with `test_ctrl_c_aborts_writes_state_and_propagates_then_resume_finishes`
  as the flake.
- `touched 1 file(s) outside the ticket's declared list: tools/contest/harvest.py`
  — the ticket's own file, because `_declared_paths` could not read a
  `**File:**` line with symbols after the path. → **KC-17**, landed by hand
  `6d038f9`.

The other three: agnes-2-5-flash and mimo-v2-5 hit the 1800 s turn timeout
with no commit (mimo after one rejected `external_directory` ask), hy3
went silent for 300 s at minute 12. The two commits on hp-uz were never
exported (KC-21) — `git -C rounds/53-<name> format-patch 0f4eb02..HEAD`
would still produce them.

Method: one worktree per entry at the base with the patch applied
(`ingest_kc14.sh`, worktrees in `../cb-kc14/`), the ticket's mechanical
checks, then 23 scenarios (`scenarios_kc14.py`, `KC14_REPO=<worktree>`)
through the public `harvest(ws, ticket_path)` on temp worktrees whose
`PROGRESS.csv` claims every shape the ticket names. Then the entry's own
`tests/test_contest_harvest.py`, the unmodified `tests/test_contest_runner.py`,
and the ideal's six KC-14 tests on the entry's `harvest.py`.

## Scores

| entry | model | bench 23 | own tests | ideal's 6 | runner tests | harvest tests removed | one commit | on-ticket files | Py 3.10 |
|---|---|---:|---|---:|---|---|---|---|---|
| **sn68** | Sensenova 6-8 | **23** | 39 green | 5¹ | 35 green | 0 | yes | yes | yes |
| sn68-var2 | Sensenova 6-8 var2 | 20 (s9, s11, s15) | 47 green | 4 | 35 green | 0 | yes | yes | yes |
| sn67 | Sensenova 6-7 | 20 (s9, s11, s15) | 48 green | 4 | 35 green | 0 | yes | yes | yes |
| ideal | `720e4d9` | **23** | 51 green | 6 | 35 green | 0 | yes | yes | yes |

¹ the ideal's off-branch test quotes the *resolved* sha in the sentence;
sn68 quotes the claim as written (`5D7DA334687C` for an upper-case claim) —
cosmetic, same verdict, same `commit is None`.

What the two losses are — the same two bugs in both var2 and 6-7:

- **s9 / s15 — `Harvest.commit` is the sha of a rejected claim.** Both
  resolve the claim first and only then test ancestry, and hand the
  resolved sha to `Harvest(commit=…)` whether or not the ancestry check
  passed. A real sha off the branch — the KC-5 case, `REWORK` with
  `commit_not_on_branch` — comes back with `commit='c38ae93…'`, and the
  ticket's second point is exactly that a rejected claim carries `None`
  (`state.json`, `table_rows()`, the export would otherwise show a commit
  for a worktree that failed). sn68 folds the ancestry into
  `_resolve_claim`, so the sha never leaks.
- **s11 — a 386-char reason.** Both build the "not a sha" sentence with
  `{claimed_commit}` uncut; a 300-char row value gives a 386-char `Reason`
  against the 200-char budget every other reason keeps
  (`test_blocking_reasons_stay_within_the_sentence_budget` only tries the
  usual shapes, so their own suites stay green). sn68 slices `[:40]`.

Everyone got the ticket's list right: `HEAD`, `@`, the branch, a tag,
`HEAD~0`, `HEAD^{}`, a 6-char prefix → `commit_not_on_branch` with the
`rev-parse` sentence and `commit is None`; short / full / upper-case sha →
`READY` with the 40-char lower-case sha; 40 zeros still rejected; the empty
commit still `no_commit`; `REASON_CODES`, `_is_ancestor`, the one
`subprocess.run` and the judge script untouched.

## Scenario matrix

```
scenario                                          sn68  sn68-var2  sn67  ideal
s0  signature, codes, fields, _is_ancestor kept    ok     ok        ok    ok
s1  HEAD → not a sha, HEAD + rev-parse in text     ok     ok        ok    ok
s2  branch name                                    ok     ok        ok    ok
s3  @                                              ok     ok        ok    ok
s4  short sha → READY, 40-char commit              ok     ok        ok    ok
s5  full sha → READY                               ok     ok        ok    ok
s6  6-char prefix refused (git would resolve)      ok     ok        ok    ok
s7  upper-case sha → READY, lower-case commit      ok     ok        ok    ok
s8  40 zeros → commit_not_on_branch, None          ok     ok        ok    ok
s9  real sha off the branch: rebase text, None     ok     FAIL      FAIL  ok
s10 empty commit stays no_commit                   ok     ok        ok    ok
s11 300-char claim: sentence ≤ 200                 ok     FAIL      FAIL  ok
s12 HEAD~0, HEAD^{}, HEAD^{commit}                 ok     ok        ok    ok
s13 tag                                            ok     ok        ok    ok
s15 short sha off the branch → None                ok     FAIL      FAIL  ok
s16 40 non-hex chars → not a sha                   ok     ok        ok    ok
s19 12-char prefix → READY                         ok     ok        ok    ok
s25 HEAD + SKIPPED → both codes                    ok     ok        ok    ok
s26 no row → no_progress_row only                  ok     ok        ok    ok
s27 facts.sha stays the 7-char head                ok     ok        ok    ok
s28 last row wins (sha after HEAD)                 ok     ok        ok    ok
s29 whitespace around the sha                      ok     ok        ok    ok
s30 gates.git only, one subprocess.run             ok     ok        ok    ok
```

## Winner and ideal

Winner **Sensenova 6-8** (`kc14-Sensenova-6-8.patch`): 23/23, the only
entry that keeps `Harvest.commit` at `None` for every rejection and the
sentence inside the budget; four tests that cover the whole symbolic list
in one loop.

The ideal commit `720e4d9` is the winner reworked:

- `_resolve_claim` is resolution only (shape + `rev-parse --verify`), as the
  ticket words it — "the shape check and the resolution go in front of"
  `_is_ancestor` — and `harvest` calls `_is_ancestor` itself on the
  resolved sha, so the three steps read in the ticket's order and the
  shape check is not written twice;
- the rebase sentence names the resolved sha (`(sha or claimed)[:12]`), so
  an upper-case or 7-char claim is reported as the commit git found;
- two tests from the bench — the off-branch sha with `commit is None`
  (full, short and upper-case), and the 300-char claim under `TEXT_LIMIT`.

The ideal sits on `kc` above KC-17 and KC-26, the two judge fixes this
round forced; its own 51 harvest tests and the unmodified runner tests are
green, and `tests` / `tests_bugfix` sequentially.
