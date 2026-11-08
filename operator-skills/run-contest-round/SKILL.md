---
name: run-contest-round
description: Run a jan-auto-agent model contest end to end — ticket and temp branch, model roster, dry run, launch, monitoring alarms, reviving agents, a second leg, judging (black box or mutation, cross matrix, tests_bugfix, CC), assembling the ideal commit from one or several agents with credit, stress under load, a push button, then the next round. Use when the user asks to run, monitor, judge or chain contest rounds.
---

# Running a contest round, from ticket to landed commit

**Operator only.** This folder is kept out of `.claude/` and `docs/` on purpose, so that no
contest agent and no Kilo or Claude Code session loads it by itself. To use it, point a
session at this file or copy it into your own `~/.claude/skills/`.

The contest gives one ticket to several models at once. Each works in its own clone;
the operator judges the entries, builds the **ideal** commit from the best of them,
proves it does not break anything, and hands the user a push button. The tool is
`python3 -m tools.contest`; its reference is `docs/kilo-contest/RUN-THE-KILO-CONTEST.md`,
the claim check's is `docs/claim-check/RUNBOOK.md`.

Paths below: `J` is the repository checkout, `P` its parent folder (rounds, logs,
models files live there), `S` the session scratchpad.

## Standing rules (they override convenience)

- **Never push.** Give the user a one-command button, in the same message that reports
  the proof. Never refer to a button from an earlier message.
- **The ticket never goes to `main`.** It lives on the temp branch; `main` gets only the
  fix and its tests.
- **Identity:** author and committer `renat <renat@hp.local2>`. Commit with
  `git -c user.name=renat -c user.email=renat@hp.local2`. **Never `export GIT_*`** in a
  shell that later starts a round: round 345 inherited them, 8 identity tests failed for
  every agent, and agents began editing unrelated tests and even production code to cope.
- **Credit:** one `Co-Authored-By: <agent> <<agent>@round-NN.contest>` per agent whose code
  or tests went in. Claude's trailer only when Claude wrote code in that commit.
- **No new flake lands.** A flake that appears after the change is fixed before landing.
- **Free models only** unless the user names an exception.
- **Never `pgrep -f` / `pkill -f`** from your own shell: the pattern matches that shell
  (exit 144, false "in use"). Use `ps -eo pid,comm,args` and match `comm == python3`.
- `rm -rf` on a variable path must be `"${VAR:?}/${NAME:?}"`, or a literal path.
- **Fetch first.** Before any ticket work, `git fetch origin` and compare with
  `git rev-list --left-right --count origin/<branch>...HEAD`; if origin is ahead, work on
  origin, and keep a local-only commit on a `backup/<ticket>-local-duplicate` branch before
  any reset.
- **Sandbox commands fail closed:** `set -euo pipefail`, check `pwd` after every `cd`, and
  `git -C <path>` for every commit; a failed `cd` once committed into the user's branch.
  Clone into `/tmp` with `--no-hardlinks`.
- **One concern per commit.** Flakes: a flake that was not there before is fixed before
  landing. First write a ticket `.md` with the conditions it fails under, then fix every
  flake in one commit of its own. A regression found in the ticket's own change is fixed in
  that change; a bug found in `main` goes in the next commit.
- **Money and secrets.** Paid `kenary` models (no `:free`) are never run. Allowed
  exceptions: `vercel_8080/poolside/laguna-s-2.1`, every `publicai` model, `airforce` only
  where both prices are 0. Report the cost column for any paid model, ask before any other.
  Never print an API key; the LENZ key is read from the environment only.
- **Talking to the user:** each reply opens with `**Перевод:**` and an English translation
  of the user's message; the answer is in Russian. Every number gets its source.

## 1. Prepare the tickets and the temp branch

1. From review reports (e.g. `VERDICTS-*.md`), verify the claims against the current code
   with your own probes before trusting them. A report's FIX can be unreachable; its
   FALSE_POSITIVE can be right. Drop items that would break working behaviour, items on
   seeded fixtures (`contest-bench/cc/fixture`, SHAs pinned) and items on scripts no test
   runs.
2. Group the rest by area into tickets sized for one agent in one leg (`**Size:** L`
   means `--legs 1` must be passed, or intake refuses it). Write each with the header
   fields and sections the lint wants (`Status: open`, `Severity`, `File`, `Symbol`,
   `Round`, `Size`, `Depends on`, `Also touches`; `## Why`, `## What to build`,
   `## Acceptance` with a command, `## Rules`). Restate the corrected scope, not the
   report's wording, and list what must NOT be done.
3. Lint: `python3 -c` with `tools.contest.draft.lint_ticket(text, None, repo=Path("."), round_no=N)`.
4. Branch: `git checkout -b <name> origin/main` (or a worktree), commit the tickets there.
   Intake refuses an untracked `epic-tasks/`, and refuses a ticket while a lower-numbered
   one is still `open`: park each judged ticket as `**Status:** queued`.

An **external repository** (GYRO, a llama.cpp fork): run a smoke ticket whose whole
solution is in the ticket first; start the real ticket only when agents build, finish
`READY` and the cross phase runs. Fix the contest tool itself on its own branch off `main`,
never on `main` or in the target repository, one commit per tool bug with a test in
`tests_bugfix/`.

## 2. Pick and check the models

- Probe a candidate with `python3 scripts/py_model_test.py MODEL…` (one coding task,
  15 checks). Keep a roster file, e.g. `$P/roundNN.models`: comma-separated ids,
  `provider/model`, `|` for a failover chain inside one agent
  (`moonmath/deepseek-v4.1-flash|moonmath/zro/deepseek-v4.1-flash|tokenharbor/…|apinex/…`),
  a repeated id runs twice as `-var1`, `-var2`.
- Drop a model that keeps failing (two `GAVE_UP` in a row, nothing harvested) and say so.
- Quotas: a chain moves on by itself when a provider answers with a quota error; it does
  not move back after the reset.

## 3. Dry run, then launch

```bash
cd "$J" && python3 -m tools.contest run --ticket NN --legs 1 --max-parallel 8 --models "$(cat ../roundNN.models)" --dry-run
```

Check the ticket line, `base`, the agent list and that the dry run prints no refusal.
Then launch **alone, in its own Bash call, with no exports**:

```bash
cd "$J" && setsid nohup python3 -m tools.contest run --ticket NN --legs 1 --max-parallel 8 --models "$(cat ../roundNN.models)" > ../roundNN.log 2>&1 < /dev/null &
```

Confirm it is really up, not just alive: `contest-out/NN/state.json` exists, every agent
is past `CREATED` with one session, and `/proc/<pid>/environ` of the runner and of
`kilo serve` holds no `GIT_AUTHOR_*` / `GIT_COMMITTER_*`.

**`kilo serve did not report healthy … within 30.0s`:** the health check is 30 s. A large
`~/.local/share/kilo/kilo.db` (4 GB once) made start-up take 27.7 s, so any load broke it.
Stop every `kilo` process, delete `kilo.db`, `kilo.db-shm`, `kilo.db-wal` (keep
`auth.json`), relaunch: start-up fell to 0.6 s.

## 4. Alarms: what they check

Alarms live only in the session (CronCreate). If the GUI session dies, they and the round
die with it; check on return and resume by hand.

- **Monitor, every 15 minutes, recurring:** `python3 -m tools.contest status --ticket NN`,
  the tail of `../roundNN.log`, each agent's state, the deepseek chain's index in
  `state.json`, `STALLED` / `GAVE_UP` / `ERROR`, quota lines, `REWORK` reasons, `free -h`
  and `df -h /tmp` (`/tmp` is RAM, no swap). Never revive, restart or stop without the
  user. When the round ends, say so and delete the monitor.
- **Judge, one-shot, ~100 minutes after launch:** the whole procedure below, written into
  the alarm's prompt with the lessons of earlier rounds. If the round is still working,
  it sets itself again (rounds took 75–150 min); a straggler alone may be skipped.
- If several agents fail the **same unrelated tests**, suspect the environment first.

## 5. Revive an agent, give a second leg

- **Revive** (round stopped first):
  `scripts/revive_round.py NN [--agent NAME] [--dead] [--any-leg] [--dry-run]` (`--agent`
  revives one agent, `--dead` brings `DEAD` agents back too, `--any-leg` allows a leg
  folder that is not the last), then
  `python3 -m tools.contest run --ticket NN --resume --models "<the same list>"`.
  `STALLED` / `GAVE_UP` / `ERROR` go back to `WAITING`; a finished commit is taken as
  `READY`; an `ERROR` agent restarts on the first model of its chain.
- **Second leg** for a ticket too large for one turn: run it as a relay with
  `--legs 2` (or `3`); each leg is a new session in the same worktree, the next leg's
  prompt carries `<agent>.leg.md`. Resume one leg with
  `--out contest-out/NN.K --legs 1 --resume`.
- **Salvage instead of restart** when a round was poisoned but every agent committed
  (round 345): judge in a clean environment on the ticket-named files only, drop every
  edit that answers the poison, and do not count a lost rework against the agent.

## 6. Judge the entries

Copy each entry with `git -C rounds/NN-<agent> archive HEAD` into `/tmp/rNN-entries/<agent>`,
plus the base (`git archive <base_sha>`). Never test in the agents' clones.

1. **Rules per entry:** files touched against the ticket's list, `git diff --exit-code
   base -- tools main.py …` for test-only tickets, no assertion removed or loosened (a
   replaced stricter one is fine), no real `sleep` over 1 s, no fixed port, tiers
   (`python3 scripts/sync_test_tiers.py --check`).
2. **Code tickets — black box with traps** (`contest-bench/rNN/_scene.py`, `run_rNN.py`,
   run with `python3 -I`, retries off, an alarm per check). Score the ticket's cases;
   add `*_guard` checks that hold on the base, so a failing guard is a regression. The
   best traps came from shipped config and real usage: a JSX apostrophe that darkened a
   whole file, `contest.ini`'s deliberately blank OpenRouter `model`, a blank gate model,
   a second corruption overwriting the first, `def range` defined in the source. A
   uniform result across entries means the bench is wrong; check it on one case first.
3. **Test-only tickets — mutation** (`contest-bench/r344` … `r346/run_rNN.py`): one
   mutation per ticket item, breaking the production behaviour that item pins, on a fresh
   copy of the base's `tools/` (and `conftest.py`, `scripts/`, `contest-bench` when the
   item lives there). **Copy `conftest.py`, never symlink it:** a linked conftest resolved
   back to the entry and every mutation survived (0/11 for all). Credit only kills the
   base's own tests do not make. When nobody kills a mutation, read the test: in 345 a
   test could not fail because its fixture file was missing; in 346 a barrier test passed
   with every `Barrier.wait` forced to fail. Mark equivalent mutations.
4. **Differential on real behaviour:** run the changed functions over real inputs (other
   repositories' sources, real config files) on base and entry; any change must be the
   ticket's intent.
5. **Cross matrix:** each entry's tests against each entry's code, base row included, at
   most 3 parallel jobs with a `--basetemp` inside each cell (6 parallel jobs plus a round
   once ran the machine out of memory). The official form is
   `scripts/judge_epic_round.py --round NN --base <sha> --runs <out>/wt --cross --ideal <ref>`
   after `contest-bench/harness/setup_worktrees.py`. Classify failures: `api` (another
   implementation's names), `base` (asked of nobody), `behaviour` (a lead to reproduce).
6. **tests_bugfix**, and the tests an entry missed that reveal real bugs: write them, run
   them on base and on every entry.
7. **CC, the claim check** on the ideal: `scripts/claim_diff.py claims.json --target .
   --base main --head <ideal> --profiles <three voters of three families> --runs 1` with the
   ticket's claims and `expect` (`fixed` / `still` / `new`). Only a unanimous verdict with a
   quote the code verified counts; a unanimous TRUE on "add a test" claims was wrong 75–88 %
   of the time, so CC supports the black box, it does not replace it.

8. **Commit the bench.** The black box or mutation bench (`contest-bench/rNN/`, or
   `contest-bench/kcNN/` through `contest-bench/harness/`) goes into its own commit on the
   ticket branch, separate from the ideal; like the ticket, it is not pushed to `main`.

## 7. Assemble the ideal commit

- Compare **item by item**, not by total score. Take the best code or tests per item from
  any agent, add your own where nobody got an item right, and run the combined result
  through the same bench, mutations included.
- Build on a temp branch from `origin/main` in its own worktree (`jan-NN-ideal`), so each
  push is a fast-forward. If the previous ideal is not in `main` yet, stack on its branch
  and say that one push carries both.
- The commit carries only the fix and its tests, never the ticket. Subject
  `AGNES-k (NN): <behaviour change>`; body lists what changed, what was dropped and why,
  and the trailers of every agent used.
- Then on the ticket branch: cherry-pick the ideal, set the ticket's `**Status:** queued`,
  commit locally.

## 8. Prove it, give the button

- `tests_bugfix`, then the full suites. The full stress is 3 runs at once in one checkout
  (shared cache) and 3 runs in fresh checkouts one at a time (like contest worktrees). In a
  chain of rounds the user cut it to **3 full suites under load**, alongside the next round,
  and insists on those 3. Each run gets its own `--basetemp`, removed after it; stop if `/tmp`
  falls under 1 GB. For a test-only commit, also 20 repeats of the changed tests.
- A change that claims test speed is measured as an A/B on scratch clones: 60 s pause
  before each run, cold scan cache, an exclusive `-n 8` run, then the stress, old/new
  alternating, medians and failures in a table. Claude Code's shell sets
  `PYTHONDONTWRITEBYTECODE=1` and the user's terminal does not: say which one a number is.
- Report the numbers, then the button in the same message:

  ```bash
  git -C "$P/jan-NN-ideal" push origin tmp/NN-ideal:main
  ```

## 9. Next round and housekeeping

- Start the next round right after judging (the user asked for the chain), after checking
  `df -h /tmp /`: under 3 GB on `/tmp` or 5 GB on the disk, stop and give the cleanup
  button, pressed **only between rounds** (agents' tests live in `/tmp/pytest-of-*`):
  `rm -rf /tmp/pytest-of-renat /tmp/NN-*; find /tmp/commit/tmp -mindepth 1 -maxdepth 1 -mmin +30 -exec rm -rf {} +`.
  `/tmp/commit` grows by ~1 GB a day: `conftest.py` sets `PYTHONPYCACHEPREFIX` to
  `/tmp/<folder name>`, and harvests run in random `kc60-commit-*/commit` paths.
- `rounds/NN-*` are standalone clones. Before deleting old ones, check their patches are
  in `contest-out/NN/` and archive any that are not, plus any uncommitted work.
- Delete only local branches that `git merge-base --is-ancestor <b> origin/main` confirms.

A branch merge into `main` gets an announcement in the voice of the previous merge
commit's body: an English stand-up with the pitch, the cast, "Previously, on…", the proof
with sourced numbers, a real conclusion, and the credits.

## Report to the user

Each judging message: the agents' states, a score table, what went into the ideal and
from whom, what was dropped, any of my mistakes plainly, the proof numbers, the button,
the next round's start time and the next alarm.
