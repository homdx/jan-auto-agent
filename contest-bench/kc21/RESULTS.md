# KC-21 — round 60 scored against the ticket's Acceptance list

Ticket: `epic-tasks/60-kc21-a-stalled-or-errored-turn-with-a-commit-on-the-branch-is-harvested-not-dropped.md`.
Base: `542244b` (`kc`, no park branch — KC-21 was the lowest open ticket).
Ideal: `f5a9f05`.

Bench: `acceptance_kc21.py` — ten scenarios written from the ticket's Acceptance list, through the
public contract only (`run_agent` against the base's `tests/_kilo_fake.py`, `cli.export_patches`
against four one-commit worktrees), using the base's own sandbox/harness helpers and no entry's new
helper. Copy it into `<worktree>/tests/` and run
`python3 -m pytest tests/test_kc21_accept.py -q --timeout=180`.
On the base: **3/10** (the three that assert today's path stays). Red where it must be.

`edge_two_commits_kc21.py` and `probe_policy_stall_kc21.py` are classification, not grading — the
ticket's prose is ambiguous on both, and they are what KC-29 and KC-30 were filed from.

## The round

`python3 -m tools.contest run --ticket 60` (six slots), 2026-09-20 18:36–19:18, `contest-out/60/`,
plus four hand patches in `kc21/` (Sensenova 6-7, 6-7 var2, 6-8 var1, 6-8 var2 — hours of work each).

| slot | outcome | why |
|---|---|---|
| agnes-2-0-flash | **READY** attempt 1 | `9c3de41` |
| mimo-v2-5 | **READY** attempt 2 | `cd3d04a` — first turn REWORK (`no_progress_row`, `commits_ne_1`, `no_test_file`) |
| step-3-7-flash | **READY** attempt 1 | `4d7f754`, and not one permission asked the whole run |
| hy3 | **READY** attempt 2 | `8530ffc` |
| agnes-2-5-flash | STALLED at 1800 s | **and its branch held the finished commit `4d19143`, claimed in `PROGRESS.csv`** — `commit: null`, no patch, the judge pulled it with `git format-patch`. The ticket's own bug, live in the round that fixes it |
| mistral-medium-3-5 | STALLED at 1800 s | four edited files, no commit — nothing exported (KC-31) |

## Entries

`acc` = the ten acceptance scenarios. `roots` = `pytest tests` then `pytest tests_bugfix`,
sequentially, in `../cb-kc21/<name>`. `own` = tests the entry ships. `policy stall` = what the entry
does when the **runner** aborts a turn (three questions) over a worktree that already holds a valid
entry. `2 commits` = what `run.commit` becomes when the branch has two.

| entry | source | acc | roots | own | policy stall | 2 commits |
|---|---|---|---|---|---|---|
| **Sensenova-6-8-var1** | hand | **10/10** | ✓ ✓ | 8 | READY | `None` |
| Sensenova-6-7-var2 | hand | 10/10 | ✓ ✓ | 6 | READY | `None` |
| Sensenova-6-7 | hand | 10/10 | ✓ ✓ | 7 | STALLED, commit dropped | `None` |
| Sensenova-6-8-var2 | hand | 10/10 | ✓ ✓ | 8 | STALLED, commit dropped | the claim's sha |
| hy3 | live | 10/10 | ✓ ✓ | 5 | READY | the claim's sha |
| agnes-2-5-flash | live, rescued from the STALLED worktree | 9/10 | ✓ ✓ | 6 | READY | the claim's sha |
| mimo-v2-5 | live | 8/10 | ✓ ✓ | 6 | READY | — |
| step-3-7-flash | live | 8/10 | ✓ ✓ | 7 | READY | — |
| agnes-2-0-flash | live | 7/10 | ✓ ✓ | 5 | READY | — |

What the four non-10s lost:

- `run.commit = verdict.commit` and nothing else (agnes-2-0-flash, agnes-2-5-flash, mimo-v2-5,
  step-3-7-flash). `harvest` resolves its commit from the `PROGRESS.csv` claim alone, so a turn that
  commits and stalls **before** `append_task.py` ends with `commit: null` and no `.patch` — the
  failure the ticket exists to end. The ticket's own prose ("the harvest sets it from the one commit
  on the branch") is what misled them → KC-30.
- The turn appended to `turns.jsonl` **before** `turn["harvest"]` was set (agnes-2-0-flash,
  agnes-2-5-flash): `run.turns` carries the verdict, the file does not. The ticket asks for one line
  with both.

## Winner

**Sensenova-6-8-var1** — 10/10, and the widest coverage of the Acceptance list: the only entry with
an end-to-end round through `cli.main` (a real silence stall: the table reads `READY, STALLED`,
`agent-a.patch` is written, `agent-b` gets nothing, exit 0 — the ticket's item 4) and the only one to
pin `run.commit is None` on a two-commit branch. Clean fallbacks (`verdict.commit or _head_sha(ws)`,
only when the branch has exactly one commit), the note built before the state flips, `last_error`
untouched.

Ideal = the winner + one test lifted from Sensenova-6-7:
`test_a_terminal_harvest_runs_the_roots_under_the_rounds_lock` — with `run_tests=True` the terminal
harvest runs the pytest roots in that worktree through `_harvest`, i.e. under the round's lock
(the ticket's item 2, which the winner never asserted).

## Timing — the entries' tests are not the slow part

`tests/test_contest_runner.py` + `tests/test_contest_cli.py`, alone, per tree: base 49 s, and every
entry 47–55 s. The 86–207 s spread of the full `tests` root across the sweep is the box's load, not
any entry's tests. Two reds that are also the box and not the code became KC-32.
