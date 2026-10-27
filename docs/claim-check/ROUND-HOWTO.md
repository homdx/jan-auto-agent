# Running a claim-check ticket as a round — the flow used for CC-10 (292)

One ticket, one round. Free models compete through the arena, cloud models (Opus, Sonnet, Haiku) bring patches made
by hand, one black box scores them all, the ideal commit is assembled from the best one. This page is the order of
steps and the traps met on the way; the commands of the arena itself are in `arena-fast-start.txt` (Russian, local).

## 1. The ticket must be open for intake

`scripts/next_task.py` reads the **first word** of the `**Status:**` line of `epic-tasks/NNN-*.md`. `open` is taken;
`final` and `landed` close the ticket for the round. A ticket whose source in `docs/claim-check/tickets/` says
`FINAL, open — …` is cut into `epic-tasks/` with the first word `open`, as its own commit (`CC-10 (292): the ticket cut
into epic-tasks for the round`). `python3 -m tools.contest run --ticket NNN --dry-run` shows the intake, the worktrees and
the plan and touches nothing: run it first.

## 2. A profile with every model, started at once

```bash
python3 -m tools.arena model use sensenova123/sensenova-6.8-flash-lite,sensenova123/sensenova-6.8-flash-lite,\
sensenova123/sensenova-6.8-flash-lite,kenary/step-5-preview:free,kenary/agnes-2-5-flash:free,zai/glm-4.7-flash,\
kenary/nemotron-3-super-120b-a12b:free -p p292 -y --set max_parallel=7 branch=cc-epic-docs
python3 -m tools.arena -p p292 run start 292
```

* A name given twice (or three times) is two (three) agents, `var1`, `var2`, `var3`.
* `max_parallel` is at least the number of agents, otherwise the rest queue. All agents start together.
* `branch=` is the branch the round forks from (here the epic's, not `main`): the round is on `arena-round/NNN`.
* `models` is written by `model use` only; `profile set` refuses it. The profile lives in `contest.local.ini`, never in git.
* Pick the models with `python3 -m tools.arena model available <providers…> --free` and the voters' table in
  `docs/claim-check/RUNBOOK.md`; a model that answers 429 all the time only burns the round's time.

## 3. Watching it

```bash
python3 -m tools.contest status --ticket 292 2>&1 | tail -10 | cut -c1-200
python3 -m tools.arena run view 292
```

States: `WAITING` (working, or waiting out a provider), `HARVESTING` (the harvest is checking the commit), `READY`,
`STALLED` (no activity for an hour or so), `GAVE_UP`, `ERROR`. `REWORK …` in *last reason* is the harvest sending the agent
back (`no_test_file`, `off_ticket_files`, `tests_failed`, `commits_ne_1`). 429s are not in the table: look in
`contest-out/NNN/<agent>/events.jsonl` or the console. Free-provider limits that bit this round: kenary 15 requests a minute
**shared by all its models**, sensenova answers 429 often, and the contest gate (one more kenari.id model) shares kenary's limit
with the agents. A round of seven free agents takes one to two hours.

**Where the code is:** the entries are clones in `../rounds/NNN-<agent>` (the path is in `contest-out/NNN/state.json`,
`workspace.path`), on branches `contest/NNN/<agent>`. The `/tmp/NNN-*` directories are scratch, not the tree.

## 4. The cloud patches

The operator makes the Opus, Sonnet and Haiku patches by hand and leaves them in `/tmp` (`Sonet5-0073-….patch`, a series:
`0072` was the already-landed docs commit, `0073` the work). Apply each to a worktree at the round's base commit
(`git worktree add --detach /tmp/j/Sonet5 <base>`; `git apply <patch>`). They are judged in the same round with the same box.
Authors in their patches are not the project's: the ideal commit is made again by the operator's own git identity (`git config user.name/user.email`) with the model's
`Co-Authored-By` trailer.

## 5. Judging

1. **The black box** `contest-bench/NNN/blackbox_NNN.py --code DIR` (written before the round, committed or not): a fake model
   server, the real process, a table of cases in the reply shapes seen live. It is the yardstick when the entries have different
   internals. Run it on a **clean worktree copy, not on the main checkout**: with the operator's `contest.local.ini` around it
   once hung on real network calls.
2. **The existing suites**, one root after the other: `python3 -m pytest .smoke_tests/ .regression_tests/`, then
   `python3 -m pytest tests_bugfix -n 4 -q`. A candidate that edits other tests to pass (agnes in round 292) is disqualified
   on that alone.
3. **Cross tests** (each entry's tests against the other entries' code) only mean something when the entries share an API.
   In 292 every entry tested its own internals and everything failed everywhere: no information, the box decided.
4. **Size of the change** to the sensitive file: the same pass rate with 51 lines changed against 296–342 is the better patch.

## 6. The ideal commit

Take the best entry as it is when it passes everything (here Sonnet 5.5). `git apply` its patch on the epic's branch (leave
out files the branch already has, such as the `epic-tasks` copy of the ticket, and take the entry's version of that one file),
check that the files equal the judged ones (`cmp`), commit **by explicit paths** in groups (code and tests; then ticket, runbook,
epic table), author the operator's own git identity (`git config user.name/user.email`), the model's trailer kept. The operator pushes; nobody else does.

## 7. After the round: the live measurement

The ticket's layer 2 (does the new switch pay off on real data) is `docs/claim-check/GATE1-VOTE-LAYER2.md`: one command,
launch and forget. The flag stays `off` until it passes.
