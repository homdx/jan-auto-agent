# Running the Kilo contest — reset, run, score

One page for the operator: N models, one ticket, N patches side by side, no
human in the loop between. The contest replaces the epic round's stages 1–2
(hand-out + implement, `docs/collect-epics/RUN-THE-EPIC-COMPETITION.md`) and
hands the run over to its stages 3–5 unchanged. The plan and the module map
are in `docs/kilo-contest/EPIC-KC.md`; everything the probe established is in
`docs/kilo-contest/PROBE.md`.

All paths are from the repo root. `python` is not on PATH — use `python3`.

---

## The map

| # | stage | who | command | you take away |
|---|---|---|---|---|
| D | **draft** | a draft model + a review model | `python3 -m tools.contest draft --target REPO "brief"` | `epic-tasks/NN-<slug>.md`, committed |
| R | **reset** | you, per round | `scripts/contest_reset.sh NN [base_ref]` | one checkout per agent at the base |
| RUN | **run** | the runner, N models | `python3 -m tools.contest run --ticket NN` | `contest-out/NN/`: one patch per agent, `entrants.json`, `SUMMARY.md` |
| — | *watch* | you, mid-round | `python3 -m tools.contest status --ticket NN` | the same table, off `state.json` — touches nothing |
| — | *revive* | you, after the round | `scripts/revive_round.py NN`, then `run --ticket NN --resume` | the ended agents back at work in their own trees |
| 3 | **score** | you | the `judge_epic_round.py` line `SUMMARY.md` prints | the mechanical table |
| 4 | **judge** | you | read the diffs; the bench table per `contest-bench/README.md` | the winner |
| 5 | **merge** | you | `git cherry-pick <winning sha>` | the base for the next round |

The two new commands are `contest_reset.sh` (KC-4) and `tools.contest run`
(KC-7), which carries `--dry-run`, `--resume` and the read-only `status`
subcommand. The three old ones are the scorer (`judge_epic_round.py`), the
bench's worktree setup (`contest-bench/harness/setup_worktrees.py`) and the
merge — stages 3–5 of the epic runbook, untouched.

Rounds stay **serial on purpose**: round N+1 starts from the merged head of
round N, the way `RUN-THE-EPIC-COMPETITION.md` says it must.

---

## What you need, once

| need | where | why |
|---|---|---|
| the roster | `contest.ini` (committed) | the limits, one `[contest.agent.<name>]` per competing model, and the gate's profile name — no keys |
| the gate's key | `contest.local.ini` next to it (git-ignored) | `[contest_gate_llm] api_key`. The committed file carries `${CONTEST_GATE_API_KEY}` and the `some/model` placeholder; without the local file the gate fails closed, per ask |
| the drafter's key | `contest.local.ini` | `[contest_draft_llm] base_url / api_key / model` for `contest draft`. It must be a **different model (another account) from `[contest_gate_llm]`**: the gate reviews the draft, and a model reviewing its own draft passes its own mistakes |
| the Kilo extension | `kilo_bin = auto` | the newest VS Code extension copy; the contest spawns its own `kilo serve` and never touches the extension's own |
| the model ids | `kilo models` | one `providerID/modelID` line per model. The roster's `model =` is exactly that string, split at the FIRST `/` — `kilo/~anthropic/x` is provider `kilo`, not `~anthropic` |

Intake checks the roster against the server's own offer (KC-25): a display
name spelled as an id, a provider without credentials, or a model that is not
there, is refused with the id to use — before any worktree is built. A model
the provider serves but Kilo's list lacks is not a failure: `--register-missing`
adds it for the round alone through `KILO_CONFIG_CONTENT` (KC-35) and says so
in the plan.

No model is built into the code. The roster in `contest.ini` is an example (the
three PROBE.md models), and `--models` replaces it for one round. The one
default is the provider a bare `--models` id gets: `kenary` (`DEFAULT_PROVIDER`
in `tools/contest/cli.py`; `--provider ID` changes it). Name the provider in
every id (`sensenova123/…`, `bynara/…`) and that default never applies.

### Pick the models

Before a round, check which models answer at all and can write code.
`scripts/py_model_test.py` sends one Python task through `kilo run`, with
Kilo's own config and no keys of its own, and scores the answer on 15 checks:

```bash
python3 scripts/py_model_test.py --find-free kenary openrouter      # list the free models with tools; calls none
python3 scripts/py_model_test.py -j 4 sensenova123/sensenova-6.8-flash-lite kenary/mimo-v2-5:free
```

A model with no answer prints the reason: a timeout, Kilo's exit code, or the
tail of its stderr (401, 404, `Database is busy`, …). The exit code is 0 only
when every model got a score. Each `kilo run` writes the user's Kilo store, so
next to a live round `-j` is capped at 4 (`--force` keeps the number). The
models that score go into `--models`.

### The settings you touch

Everything sits in `[contest]` of `contest.ini`, one comment per key. An
unknown key fails at load time with its name. Put local values in
`contest.local.ini`: it is read after `contest.ini` and overrides it.

| key | default | what it decides |
|---|---|---|
| `max_parallel` | 3 | sessions at once; `--max-parallel N` for one round |
| `legs`, `legs_by_size` | 1; `L=3` | legs per round, by the ticket's `**Size:**`; `--legs N` overrides both |
| `max_rework`, `max_continues_per_attempt` | 2, 2 | reworks after the first turn; nudges for an idle turn with edits and no commit |
| `turn_timeout_sec`, `turn_extend_sec`, `turn_max_sec` | 3600, 600, 7200 | the turn clock: a floor, extended while the tree changes, up to a ceiling |
| `idle_event_timeout_sec` | 900 | no event this long → the session is aborted |
| `first_touch_sec` | 420 | no file touched this long → a nudge, then a fresh session, then `DEAD` |
| `agent_max_sec` | 5400 | one agent's hard limit; at it the agent ends `STALLED`, its tree scored as it stands |
| `max_error_retries`, `error_retry_max_backoff_sec` | 30, 60 | retryable provider errors in a row before `ERROR` |
| `provider_retry_max_wait_sec`, `quota_patterns` | 300 | a reset further out than this, or a quota message, ends the agent `ERROR provider_quota` |
| `harvest_budget_sec`, `deadline_commit` | 900, true | the harvest's clock; uncommitted work at the deadline is committed for the agent |
| `agent_suite_slots`, `pytest_workers_*` | 1, auto | how many agents run pytest at once, and with how many workers |
| `tmp_roots`, `deny_commands`, `ask_commands` | | the policy: paths allowed outside the worktree, commands always refused, commands sent to the gate |
| `gate_llm_profile`, `draft_llm_profile` | | the profile sections for the gate and the drafter (keys in `contest.local.ini`) |
| `variant` | highest | the reasoning variant of an agent that names none; `--variant` or `model@variant` |
| `compact_at_percent`, `summary_at_percent` | 80, 90 | when a long session is compacted, and when a summary is asked for |
| `out_dir`, `rounds_dir`, `workspace_kind` | contest-out, ../rounds, clone | where the output and the agents' checkouts go |

---

## Stage D — draft the ticket

Auto mode writes the ticket; nobody edits it by hand. A ticket the round
cannot use is a bug in the drafter, fixed there, and the ticket redrafted.

```bash
python3 -m tools.contest draft --target . "add tests/test_auto_delta_validator.py covering DeltaValidator"
```

What it does, in order:

1. `--collect` Pass A over `--target` — the MODULE / TEST / RISK maps, each
   cut to `[contest] draft_map_budget` characters. A file the brief names is
   given whole, never cut; a cut says the rest was not seen.
2. The repo's own `AGENTS.md` goes into the prompt, so Acceptance and Rules
   use the repo's own commands (tiers, smoke gate, hooks).
3. One call to `[contest] draft_llm_profile` (`[contest_draft_llm]`), the
   lint, one rework on lint errors.
4. The review by `[contest] gate_llm_profile` (`[contest_gate_llm]`): one
   round of problems at a time, at most `draft_review_rounds` (3) rounds.
5. The ticket is committed in `--target` as `epic-tasks/NN-<slug>.md`, with
   `--round NN` or the next free number.

**Two accounts, two models.** Both profiles live in the git-ignored
`contest.local.ini`, never in `contest.ini` (that one carries only
`${CONTEST_DRAFT_API_KEY}` / `${CONTEST_GATE_API_KEY}`):

```ini
[contest_draft_llm]
base_url = https://<provider A>/v1
api_key  = <key of account A>
model    = <model A>

[contest_gate_llm]
base_url = https://<provider B>/v1
api_key  = <key of account B>
model    = <model B>      ; not model A
```

The same model in both sections is a self-review and is not allowed.
`--no-review` skips step 4 and says so on stderr; use it only to debug the
drafter.

**The size decides the legs.** The draft writes `**Size:** XS|S|M|L`. `run`
reads it: `[contest] legs_by_size` (default `L=3`, the rest 1) gives the
number of legs when no `--legs` is passed — see *`--legs N`* under Stage RUN.
An L ticket that comes out to one leg is refused unless `--legs 1` says so.

`--run` starts the round right after the commit with the run flags given to
`draft` (`--max-parallel`, `--legs`, `--fresh`, …). Without it, go to Stage R.

### A ticket written by hand

Most rounds of this epic ran on hand-written tickets, and intake treats them the
same way. A ticket is `epic-tasks/NN-<slug>.md`, committed before the round
(intake reads it from the base tree). Intake requires:

- `**Status:** open`. Any other first word is refused.
- `**File:**` and `**Symbol:**` lines. Without them the sessions get no code to
  fix, so intake refuses the ticket.
- Every lower-numbered ticket parked: `landed` or `queued`. A lower ticket still
  on offer is refused, and the refusal prints the `sed` + `git commit` that
  parks it.

`**Size:** XS|S|M|L` is optional; it picks the legs. Copy the header of a
recent ticket (e.g. `epic-tasks/126-*.md`) and keep the Why / What / Acceptance
sections. Read the ticket back with `cat epic-tasks/NN-*.md`. The exact prompt
the agents get is printed by `run --dry-run`.

For a task on another repository, or a task split into several tickets that
run one after another (ticket 01 → winner lands → ticket 02 on top), see
`2legs/MULTI-LEG-RUNBOOK.md`.

---

## Stage R — reset

```bash
scripts/contest_reset.sh 47                 # the roster's agents, at HEAD
scripts/contest_reset.sh 47 competition      # …at a named base
scripts/contest_reset.sh 47 --clone hy3=/path/to/an/existing/clone   # attach one
```

The script is a thin shell around `python3 -m tools.contest.workspace prepare`
(KC-4). It resolves the base and refuses a dirty `epic-tasks/` (an untracked
folder does not exist inside the checkouts, so commit the tickets first),
then builds one checkout per agent, in roster order, at
`<rounds_dir>/<NN>-<agent>` on branch `contest/<NN>/<agent>`, and empties
`runs/<agent>/` so a stale `PROGRESS.csv` never stops a round. Since KC-59
the default checkout is a **fresh local clone** — its own `refs/stash`, index
and HEAD, so one agent's `git stash` cannot pop another agent's work — and
its push URL is cut, so `git push` fails in git itself. A checkout that
holds work (commits above the base, or edits outside `runs/`) is refused
unless you pass `--fresh`; the refusal names `--resume` as the other way
out. The clone's `githooks/` pre-commit (the tier check) is copied with it,
so an agent that ships a test still has to tier it — or the commit is
aborted, which is the point.

One line per agent, the last word the state of its checkout:

```
laguna  ../rounds/47-laguna  contest/47/laguna  @ HEAD  (clone, fresh)
```

`fresh` — built now; `reset` — one from an earlier round, moved back to the
base; `clone` — an attached existing clone (`--clone name=path`, discarded
only with `--force-clone`). Cleanup is the other subcommand, stage 5's:
`python3 -m tools.contest.workspace remove --round 47`.

---

## Stage RUN

`--dry-run` first, then the round, and `status` whenever you want the table:

```bash
python3 -m tools.contest run --ticket 47 --dry-run   # intake, the plan, the first prompt — stop
python3 -m tools.contest run --ticket 47              # the round
python3 -m tools.contest status --ticket 47           # the SUMMARY table off state.json; safe mid-round
```

`--dry-run` runs intake with every server check skipped (one `dry-run:
skipped …` line each), builds the round's worktrees, and prints the plan and
the exact prompt the first agent would get — no `kilo serve`, no session,
no gate call. A later `run` without `--fresh` meets those worktrees as a
round left behind; `--dry-run --fresh` discards them.

**A ready ticket, the usual round.** The models come from the command line, not
the roster. Round 126 ran like this:

```bash
time python3 -m tools.contest run --ticket 126 --max-parallel 12 --models \
sensenova123/sensenova-6.8-flash-lite,sensenova123/sensenova-6.7-flash-lite,\
sensenova123/sensenova-6.8-flash-lite,sensenova123/sensenova-6.7-flash-lite,\
agnes-2-5-flash:free,mimo-v2-5:free,step-3-7-flash:free,\
bynara/space-bunny-alpha-bynara,laguna-s-2-1:free,agnes-3-0-flash:free,glm-4-7-flash:free
```

How `--models` reads that list:
- An id split at its first `/` is `provider/model`. An id with no `/` goes to
  the default provider (`kenary`, or `--provider ID`).
- The agent's name is the model id without its `:tag`, made branch-safe.
- A name given twice is run twice, as `-var1`, `-var2`, … in list order:
  `sensenova-6-8-flash-lite-var1`, `…-var2`. Each gets its own checkout and
  branch.
- `model@high` sets that agent's reasoning variant.
- `--resume` must get the same `--models` string; the names come from it.
- `--max-parallel` at or above the number of agents starts them all at once.

Add `--dry-run` to the same line first: it shows the agents' names and the
plan without calling any model.

**What the console shows.** The plan, one line per fact — the round 47 plan
of the recorded run below, verbatim:

```
ticket   47-kc8-….md — KC-8 — `docs/kilo-contest/RUN-THE-KILO-CONTEST.md`: …
base     eb4ff6059cc9
agents   3: kenary/laguna-s-2-1:free, kenary/mistral-medium-3-5:free, kenary/hy3:free
parallel 1
workers  8 each (auto, 1 suite slot)
tmpdir   /tmp/kilo/…/contest-47
tests    off
gate     kenary/agnes-2-5-flash:free @ 127.0.0.1
out      /tmp/kilo/…/out
```

Then one line per transition, on stderr: `PROMPTED`, `WAITING`, each
permission as `permission <kind> -> <reply> (<layer>)`, `HARVESTING — tests
on/off`, and the terminal state per agent — `READY — <sha>`, `REWORK —
attempt N (reasons)`, `GAVE_UP`, `STALLED` or `ERROR`. A box with more than
`neighbour_kilo_warn` other Kilo processes sharing the server's store gets
one `kilo:` warning line, not a refusal. At the end: one JSON line per
agent, the `patch:` lines, the `entrants.json:` / `SUMMARY.md:` lines, and
the exit code — **0 when at least one agent is READY, 2 when none is, 1 on an
intake or a server failure.**

**The status line.** While the round runs, one line repeats with every agent
on it (`runner.py`, `StatusLine.line`):

```
round 120 41m: sensenova-6-8-flash-lite-var1 READY (tests 5m) · glm-4-7-flash WAITING 12m ▓▓▓░░ 60% 3f ↺1 · mimo-v2-5 HARVESTING 2m (queued 4m, 2 ahead) — 9 live · kilo neighbours 3
```

Read it left to right. Each agent is shown as follows:

- **Name and state** always come first.
- **Live agents** then show how long they have been in that state. WAITING and REWORK agents also get a progress bar, a percentage and a file count (`3f`); the percentage is relative to the median of the other working agents, not the ticket.
- **`↺N`** is the rework attempt.
- **`+Xm`** is the extra clock a long turn was granted (KC-36).
- **Harvest note, while HARVESTING.** It is read live off the round's single pytest lock (KC-57):
  - `(queued Xm, N ahead)` — the agent is waiting for the slot;
  - `(tests Xm)` — the agent holds the slot and its roots are running.
- **Harvest note, in any other state** (READY, REWORK, GAVE_UP, …). It shows the **last harvest that finished** as `(tests Xm)`. That number is the harvest's whole `elapsed`, which is measured from before the lock is taken (`harvest.py`: `start` before `with test_lock`), so it is **queue wait + the roots' run + a few seconds of git**. It is not the pytest time alone.
  - Example: `tests` + `tests_bugfix` take about 3 minutes on this box. An agent showing `(tests 15m)` therefore waited about 12 minutes for the slot, and one showing `(tests 5m)` waited about 2.
  - The split is only visible while the agent is still HARVESTING.
- **Suite note, for an agent's own pytest run** inside a WAITING turn (KC-58): `(suite queued Xm, N ahead)`, `(suite Xm)`, or `(suite Xm, over the ceiling)`, the last once it has passed `agent_suite_max_sec` and no longer blocks the next waiter.

After the agents come `N live` (the agents not yet terminal) and, when the Kilo store is shared, `kilo neighbours N` (KC-62). The same table, without the live parts, is printed any time by `python3 -m tools.contest status --ticket NN`.

**`--resume` after a Ctrl-C or a provider outage.** `state.json` is written
on every transition, so the round's own folder is the resume point:

```bash
python3 -m tools.contest run --ticket 47 --resume
```

Only the agents that were mid-flight restart; a finished one is finished.
`--resume` needs `state.json` and fails before any server starts when it is
missing or unreadable. A Ctrl-C leaves the state saved (the runner aborts
the sessions and re-raises after saving), so the resume is the continuation.
A provider outage does not need one: retryable session errors are re-prompted
in the same session within the round's own budgets (KC-19, KC-64), and a
quota — a reset time further out than `provider_retry_max_wait_sec` — ends
the agent `ERROR provider_quota` at once, with the reset time printed once
per provider at the round's end.

**Putting ended agents back to work: `scripts/revive_round.py`.** `--resume`
restarts only the agents that were mid-flight. An agent that already ended
`STALLED`, `GAVE_UP` or `ERROR` (a quota that has since reset, a deadline hit
on a loaded box) stays ended. The script sets each of them back to `WAITING`
in `state.json`, and the next `--resume` takes them up again:

```bash
scripts/revive_round.py 126 --dry-run       # what it would change, writes nothing
scripts/revive_round.py 126                 # revive; the old file is kept as state.before-revive.json
python3 -m tools.contest run --ticket 126 --resume --models <the same list>
```

The rules:
- `READY` and `DEAD` agents are kept as they are.
- A revived agent's tree is harvested first: a finished commit is taken as
  `READY`, and anything else restarts in the same tree with a fresh attempt
  budget.
- The old state, attempt and error are kept on the agent under
  `revived_from`.
- Run it only when the round is stopped. A live runner rewrites `state.json`
  and would undo it.

For a round of legs, the round's number or folder resolves to the **last** leg
(`contest-out/65.3`), the only one whose `state.json` is the whole round. The
script prints the resume line for that leg:

```bash
scripts/revive_round.py 65                  # → contest-out/65.3
python3 -m tools.contest run --ticket 65 --out contest-out/65.3 --legs 1 --resume
```

An earlier leg is refused unless you pass `--any-leg`. Even then, the legs
share one worktree per agent, so that leg restarts on the tree the later legs
left.

**`--legs N` for a ticket too large for one turn (KC-43).** A round is one shot:
one turn, the continues and reworks inside its session, then the harvest. When
the ticket cannot be finished in one turn — the context or the clock gives out
with the work half done — run it as a relay:

```bash
python3 -m tools.contest run --ticket 65 --legs 3
```

Each leg is today's round body on a **new session in the same worktree**, so the
code survives and the exhausted context does not. The legs land in
`contest-out/65.1`, `65.2`, `65.3`, and every log line and `state.json` names
its leg. An agent whose leg ended `GAVE_UP` or `STALLED` is handed on; one that
ended `READY` is finished (its state carries to the last leg unchanged), and
`DEAD` and `ERROR` are not retried — a new session would meet the same failure.
Leg *n+1*'s first prompt is a **continue**, not a rework: the leg records so far
(`<agent>.leg.md` — the files touched with a diffstat, the commit the leg ended
on, the harvest verdict, each `none` when empty; newest first) and the
instruction to go on from the files already changed. Every leg starts with a
fresh attempt counter and its own rework budget. Only the **last** leg is
scored, exported and summarised (`.patch`, `entrants.json`, `SUMMARY.md`);
intermediate legs only have to end in a captured state. `legs = 1` (the
default, `[contest] legs`) is one turn with no leg suffix, the round as it
always was. A relay cannot be `--resume`d as a whole: resume one leg's folder
with `--out <that folder> --legs 1`. Cross-agent relay — one model finishing
another's work — is deliberately not built: it removes the independence that
makes a round a comparison.

**The leg record (KC-43 + KC-74).** At the end of every leg except the last, the
runner writes `contest-out/<NN>.<leg>/<agent>.leg.md` (`runner.leg_record`). It
writes the record after that leg's `kilo serve` is closed, so everything in it
was saved while the session was still alive:
- the harvest's `tests_run` is kept on the turn as `turn["harvest"]["tests_run"]`;
- the last reply is kept as `turn["last_message"]`.

The same text is pasted into the next leg's first prompt, newest record first.
A record from round 120's bench (a leg whose `tests` root went red):

```
leg 01.1 — agent-a
files: 2 (+3 -1)
  pkg/thing.py  +1 -1
  tests/test_thing.py  +2 -0
commit: f8d9fb839a68de1a2c3d6d487dc73ff5d43eb364
harvest: REWORK tests_failed
tests:
  tests: 5✓ 1✗
  tests_bugfix: PASS
last message:
  > RED-LEG-1 agent-a: still fails
summary: none
what is left: tests/test_thing.py::test_red_marker
```

The fields, in this order:

| field | source | empty |
|---|---|---|
| `files` | the added/deleted lines `git diff` counts per file (its numstat) of the worktree against the base, committed and not, untracked marked `(new)` | `none` |
| `commit` | the branch head, when the leg left a commit above the base | `none` |
| `harvest` | the newest turn's verdict and reason codes | `none` |
| `tests` | one line per pytest root with its counts. The source is the agent's own `pytest` runs, read from the `bash` parts in `<agent>/events.jsonl` (`5✓ 1✗`, parsed from pytest's stats line). A root the agent did not run itself comes from the harvest's `tests_run` (`PASS`, `1✗`, `absent`). `skipped: …` is not a root and is dropped | `none` |
| `last message` | the agent's last assistant text, quoted with `> ` on every line so it can never pose as a field. Cut to 20 lines, ending in `… cut` | `none` |
| `summary` | KC-40's model-written summary (`run.summary`) verbatim, quoted the same way | `none` |
| `what is left` | the ticket's declared files the leg never touched, plus the failing node ids from `tests` | `none` |

Only `last message` and `summary` come from a model. Every other field comes
from git and the logs, and an empty mechanical field says `none` rather than
being filled with prose.

The record is capped at `LEG_RECORD_MAX_LINES = 80`. Over the cap, the summary
gives ground first, then the message, each ending in `… cut`. The mechanical
fields are never cut.

With `legs = 1`, the default, no record is written, and the prompts are byte
for byte what they were before.

How to read one when judging a relay:
- `harvest: REWORK tests_failed` with `what is left` naming node ids means the
  next leg's job is exactly those tests.
- `files: none` plus `last message: none` is a leg that did nothing. That is
  usually a provider problem, so check `turns.jsonl` before blaming the model.

The acceptance suite is `contest-bench/kc74/acceptance_kc74.py`. It has 31
checks and runs end to end on the fake Kilo, which emits
`message.part.updated` the way live Kilo 7.6.2 does.

**The flags of `run`** (`python3 -m tools.contest run --help` is the source):

| flag | what it does |
|---|---|
| `--ticket NN` | the ticket, NN from `epic-tasks/NN-*.md` (required) |
| `--target REPO_PATH` | the git repo the round runs on (default: the current one); see `2legs/MULTI-LEG-RUNBOOK.md` (KC-76) |
| `--roster PATH` | the roster ini (default `contest.ini`; `contest.local.ini` beside it overrides it) |
| `--base REF` | the ref the worktrees start from (default `HEAD`) |
| `--models a:free,b:free` | these models run instead of the roster's agents; `model@variant` names a variant |
| `--backend kilo\|openrouter` | override the roster's backend |
| `--provider ID` | the provider behind a `--models` id that names none (default `kenary` / `openrouter`) |
| `--variant NAME` | the reasoning variant of every agent that names none: `highest` (default), `default`, or a name |
| `--register-missing` | register a model Kilo does not list, for this round only (needs `server = spawn`) |
| `--reprobe` / `--allow-unprobed` | re-run the variant probe / start even when a probe failed or is missing (KC-11) |
| `--max-parallel N` | override `[contest] max_parallel` |
| `--legs N` | run the round as N numbered legs over one worktree per agent (KC-43); overrides `[contest] legs` (default 1) |
| `--no-tests` | no pytest roots in the harvest |
| `--no-gate` | no gate model: the mechanical layer decides, the rest is `gate-failed` |
| `--resume` | continue from `state.json`; only the mid-flight agents restart |
| `--dry-run` | intake, the worktrees, the plan and the first prompt, then stop |
| `--fresh` | reset the round's worktrees even when they hold uncommitted work or commits |
| `--out DIR` | the round's output directory (default `<out_dir>/<NN>`) |

**Where every file lands** (`EPIC-KC.md` §3, with the live defaults):

```
contest.ini                              roster + limits + gate profile (committed, no keys)
contest-out/<NN>/                        one folder per round (git-ignored); with `--legs N`, `<NN>.1` … `<NN>.N`, one per leg
  state.json                             every agent's state after every transition — `--resume` reads it
  <agent>/events.jsonl                   every SSE event of that session, wall-clocked
  <agent>/decisions.jsonl                every permission decision: layer, verdict, reason, elapsed, gate model
  <agent>/turns.jsonl                    prompt sent, idle reached, harvest verdict, per turn
  <agent>.patch                          git format-patch base..branch (READY; GAVE_UP/STALLED/ERROR keep it when a commit exists)
  <agent>.<STATE>.diff                   a STALLED/ERROR tree with edits and no commit (KC-31)
  <agent>.session.json                    the session's own message dump
  <agent>.leg.md                         what a leg left, handed to the next one (`--legs` only)
  entrants.json                          contest-bench's input, names = agents
  SUMMARY.md                             the table + the gate's own decisions + the next commands
../rounds/<NN>-<agent>/                  the checkouts (outside the repo tree), branch contest/<NN>/<agent>
  runs/<agent>/PROGRESS.csv              written by the agent via append_task.py (git-ignored, as today)
```

---

## The gate

Every ask the session rules did not settle on their own goes to a **second
model**, never the agent's own: `[contest] gate_llm_profile` names an LLM
profile (`contest_gate_llm` by default), resolved like Gate 1's and called
through `tools.llm_stream.request_completion` with `response_format` on.
Intake refuses a gate model the roster runs (KC-37), probes it once before
the round (KC-55), and says when it shares the agents' own endpoint — a 429
there hits the gate too.

What the gate sees is one labelled user message — the permission, its
`patterns`, the `command` and `description` from its metadata, the worktree
path, the round's `tmp_roots`, the ticket's title and `**File:**` list, and
the last five tool parts of the session (what the agent was doing) — and it
answers one JSON object: `{"verdict": "allow"|"reject", "reason": "<one
line>"}`. The mechanical layer (geometry: inside the worktree or a
`tmp_roots` glob, or the hard denylist) settles most asks first, so a gate
call is the exception, not the rule; the per-session budget is
`gate_max_calls_per_session = 20`, and a 429 or a dropped connection is
waited out within `gate_deadline_sec`, the wait granted back to the turn
(KC-55, KC-66). **Fail-closed, always:** a transport error, a timeout, a
non-JSON reply, a verdict outside the enum, or an exhausted budget is a
`reject` whose reason the agent can read — a gate that cannot be reached
never turns into an allow.

**How to read the record.** One line per decision in `<agent>/decisions.jsonl`
(all three layers, so a `mechanical` line is the majority of a healthy
round): `t`, `sessionID`, `permission_id`, `permission`, `patterns`,
`command`, `layer` (`mechanical` | `gate` | `gate-failed` | `budget` |
`context`), `reply` (`once` | `reject`), `reason`, `gate_elapsed`,
`gate_model`. `SUMMARY.md` counts them per agent (asked / allowed /
rejected / gated / gate-failed) and prints, under **"Decisions worth a
look"**, every `gate` and `gate-failed` line of every agent — the section to
read after a round to see whether the gate blocked what the ticket needed.
The command and the reason ride on the line; the layer says whether the gate
spoke or could not.

**When the gate blocked something the ticket needed:** add a `tmp_roots`
entry to `contest.ini` (the mechanical layer then settles it for the next
round, gate calls and all) or let the agent write inside its own worktree,
and rerun. Never `always` — under `external_directory` it whitelists the
pattern for the rest of the session (PROBE.md, fact 4), and the policy's
replies are exactly `once` and `reject`.

---

## Hand-over

The run stops when every agent is in a terminal state and the folder is
written. `SUMMARY.md` ends with the commands to run next, built from the
real paths — the two the round-47 summary printed (a round with no patch
prints the judge line alone):

```bash
python3 contest-bench/harness/setup_worktrees.py <out>/entrants.json --wt <out>/wt
python3 scripts/judge_epic_round.py --round 47 --base <sha> --worktree laguna=<path> --worktree mistral=<path> --worktree hy3=<path>
```

`contest_reset.sh` is deliberately not among them: it prepares a round's
checkouts and cleans nothing up — the cleanup is `python3 -m
tools.contest.workspace remove --round NN`, stage 5's. From there the epic
runbook's stages 3–5 are unchanged — the mechanical table, the judgement
read against the four questions, the merge onto `competition` — and the
scoring side of the record is `contest-bench/README.md`: the same input
data fed to every entry, ranked by what came out, against a fake provider,
never a live one.

### Judging a finished round: the score table, then the cross phase

Round 158. When every agent is in a terminal state and the models have written
their tests, the judge is one command with two stages, in this order.

```bash
python3 contest-bench/harness/setup_worktrees.py <out>/entrants.json --wt <out>/wt --ideal <ref>
```

```bash
python3 scripts/judge_epic_round.py --round NN --base <sha> --runs <out>/wt --cross --ideal <ref>
```

1. **The score table**, as before: one row per worktree (gate, commits, files,
   `+/-`, test files and functions, off-ticket files, pushed, sha). It is printed
   first, so a round with a failing hard gate is read before anything else runs.
2. **The cross phase**, after it, only with `--cross`. Rows are *whose tests*: every
   entry that added or changed a file under `tests/` (the names `git diff` reports
   for `tests/` against the base; an entry that touched nothing there is a column
   and no row).
   Columns are *whose code*: every entry, the base, and with `--ideal REF` the
   candidate ideal. A cell is `passed/total` of that entry's own changed test
   files run on that code, and under the matrix every failing test is named with
   its first `E ` line and one class:

   - `api` — an ImportError, AttributeError or a signature `TypeError`: the test
     names something only its author's code has. A different implementation, not
     a bug (a test module that cannot be imported at all is this, too).
   - `base` — the same test also fails on the base: the author asked for
     something nobody was asked for. Not a finding.
   - `behaviour` — everything else. A **lead, not a verdict**: reproduce it by
     hand on the code, and a reproduced bug gets its own ticket.

   A cell that could not run says why instead of `0/0` (`n/a`, with a note: a
   path that is not a repo, a ref git cannot archive, a test that outlived
   `--cell-timeout`).

   Between the matrix and the full list the judge prints **Leads**, the two
   things worth reading first (`cross.md` has them under `## Leads`, `cross.json`
   under `leads`): every `behaviour` failure, and every *discriminating* test — one
   the base fails, some code passes and other code fails. The class of that test is
   `base`, so on its own it reads as "nobody was asked for this"; the lead says who
   did what the test asks and who did not (round 157: a test of the lock written
   before the child starts, which the two entries without the fix fail and the rest pass).

How a cell runs: the implementation is a `git archive` of the worktree's `HEAD`
(or of the ref) in a scratch directory, the author's test files are copied in as
`tests/_xcross_<entry>_<file>` — never into a worktree, which is only read —
and `pytest -n 4` runs them against a private short basetemp. The scratch
copy and the basetemp are deleted with the cell, and a cell that outlives
`--cell-timeout` (300 s) has its whole process group, xdist workers included,
ended; round 151 ran the box out of inodes before that cleanup existed. Cells
run one at a time; `--jobs N` runs N at once when the box can take it.

`cross.json` (the raw cells: counts, classes, nodes, `E ` lines) and `cross.md`
(the table and the failing cells) are written to `contest-out/NN/` next to
`SUMMARY.md`; `--cross-out DIR` moves both. A worktree named `base` or `ideal`
(`setup_worktrees.py` makes both) is that column; without one the column is the
`--base` / `--ideal` ref. The phase never takes the round down: if it cannot
start, the score table is already on the screen and the judge says why.

What the matrix is for, and what it is not. An entry's tests are bound to its
author's own helper names, so most off-diagonal cells of an API-heavy ticket are
`api` and say little; the diagonal says whether each entry's own tests are green
on its own code, and a `behaviour` cell is the only place another entry's test
found something the bench did not. Round 151's two real bugs (a cache that
outlived the drop, a linked worktree left with Kilo's own `.gitignore`) came out
of exactly such cells.

---

## Known limits

- **No port discovery for the extension's server.** The contest runs its own
  `kilo serve`; to attach to one you name the port in the roster
  (`server = http://127.0.0.1:PORT`). The VS Code extension's own
  `kilo serve` children — each on its own ephemeral port — are not findable.
- **Serial rounds.** One ticket per round, N agents on it; round N+1 starts
  from round N's merged head. Parallel rounds would produce N conflicting
  versions of the same file.
- **Mechanical harvest only.** READY is decided by `gates.py`'s checks — the
  `PROGRESS.csv` row, the one commit, nothing pushed, a test file in the
  diff, `_shrink` byte-identical — never by a model reading the patch. The
  scoring side reads code; that is its job.
- **Nothing is merged, nothing is pushed, nothing is judged inside the
  run.** That is the operator's stage 3–5.

## When a landed ticket makes the suite slower or flaky

The harvest runs `tests` and `tests_bugfix` for every agent, one agent at a time
behind a single lock. A suite that is 2 minutes slower is therefore 2 minutes
times N agents of queue in every round: see `(tests Xm)` on the status line. Some
winning patches are correct but slow the suite down, or leave a test that
flakes under load. That is fixed **after** the ideal commit, in **separate
commits of its own**, never folded into the ticket's commit. The ideal commit
stays the winner's code, so the bench and the ticket still describe it.

**An exercise on record (2026-09-27).** This part is written as a task on
purpose. It names no slow test and no fix; finding them is the job.

- **Start from `b5257cf`** (`epic-tasks: KC-10 (49) landed`). At `-n 8`,
  `python3 -m pytest tests` there is roughly **365 s**. Before the slowdown it
  was under 300 s. After a good fix it is about 130 s, so the slowdown is only
  part of the story.
- `b5257cf` is **not** the commit that made it slow. The cause came in
  **several commits earlier**, and the commits in between are unrelated
  landings. Walk the history back yourself (`git log`, `git bisect run`
  with a timing script, timing a single file across commits) and name the
  commit that brought the slowdown in, with numbers.
- **How many causes?** As many as you find. There may be one, there may be
  several, and the reference answer is not guaranteed to have found them
  all. Your score is how many you find, prove with numbers and fix, and
  how far below the reference timing you get. Look at the tests, and also at
  everything around them that decides when and where they run.
- **The reference answer** is on `kc`, somewhere within the **ten commits
  after `b5257cf`**. Which of them, and how many, is for you to find:
  look for the point where the suite got fast again, by timing the commits
  (not by reading their messages first). Do that only after you have your
  own cause, your own fix and your own numbers, then compare the approach,
  not only the timings.

**The method**, for this exercise and for any landing after which the round's
`(tests Xm)` or a local `python3 -m pytest tests -n 8` jumps:

1. **Measure on the slow head against a commit where the suite was fast.**
   That commit is usually not the immediate parent, so bisect. Run `tests`,
   then `tests_bugfix`, one after the other with `-n 8`, never in parallel.
   Name the base commit in every fix commit's message.
2. **Find the real sleeps**: pytest's own slow-test report (its `durations`
   limit, 25 by this exercise), then ask why each slow test
   waits. A roster default such as a backoff or a silence window that reaches
   a test not about it is fixed in the test config, not in the product
   default.
3. **Look at the scheduling, not only the tests.** Compare the sum of the
   durations with the wall clock. A large gap means something serialises the
   run, for example an `xdist_group` or a fixture pinning a whole file to one
   worker. Check whether the reason for that pinning is still true.
4. **Stress the result**: the changed file many times next to a full run at a
   higher `-n`. A test that turns flaky because of your fix gets its own
   commit, with the failure rate before and after.
5. **One concern per commit.** In the message: the base sha, the times before
   and after, and the stress numbers.

---

## First round on record

Round 47 — this page's own ticket — run live on 2026-09-27 from base
`eb4ff60`: seven agents (six `kenary` free models and
`bynara/space-bunny-alpha-bynara`), `--max-parallel 8`, and `kenary/hy3:free`
as the gate. It is the baseline the next roster is compared against.

### `SUMMARY.md`, verbatim

````markdown
# Round 47: 47-kc8-the-runbook-reset-run-score-and-the-first-live-round.md

- ticket: 47-kc8-the-runbook-reset-run-score-and-the-first-live-round.md
- base: eb4ff6059cc98cd4ebd67d4ac34012706193e009
- gate: hy3:free @ kenari.id
- started: 2026-09-27 15:22:43 UTC
- ended: 2026-09-27 16:58:52 UTC
- wall: 5770 s

## Agents

| name | model | state | attempts | turns | asked | allowed | rejected | gated | gate-failed | questions | cost | tokens in | tokens out | commit | last reason | file |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| agnes-2-5-flash | kenary/agnes-2-5-flash:free | READY | 1 | 2 | 45 | 44 | 1 | 1 | 0 | 0 | 0 | 5381087 | 32447 | 70c54b2dd139 | READY | agnes-2-5-flash.patch |
| mimo-v2-5 | kenary/mimo-v2-5:free | READY | 0 | 1 | 6 | 6 | 0 | 0 | 0 | 0 | 0 | 3025024 | 16917 | 79f6b42cc332 | READY uncommitted_files | mimo-v2-5.patch |
| step-3-7-flash | kenary/step-3-7-flash:free | READY | 1 | 2 | 13 | 10 | 3 | 2 | 0 | 0 | 0 | 4054641 | 17462 | 3716d7c78e0a | READY | step-3-7-flash.patch |
| space-bunny-alpha-bynara | bynara/space-bunny-alpha-bynara | STALLED | 0 | 1 | 77 | 73 | 4 | 3 | 0 | 0 | 0 | 13702528 | 35580 | 1825e2df56e1 | time up: 90m for the agent | space-bunny-alpha-bynara.STALLED.patch |
| laguna-s-2-1 | kenary/laguna-s-2-1:free | READY | 1 | 2 | 27 | 26 | 1 | 2 | 0 | 0 | 0 | 6966603 | 61328 | 38a22b6d552d | READY | laguna-s-2-1.patch |
| agnes-3-0-flash | kenary/agnes-3-0-flash:free | READY | 0 | 1 | 50 | 48 | 2 | 1 | 0 | 0 | 0 | 18787520 | 83618 | 6a80f8b3845b | READY | agnes-3-0-flash.patch |
| glm-4-7-flash | kenary/glm-4-7-flash:free | GAVE_UP | 2 | 3 | 33 | 30 | 3 | 3 | 0 | 0 | 0 | 6368516 | 26235 | - | REWORK after the last attempt: commit_not_on_branch | - |

## Decisions worth a look

- agnes-2-5-flash: gate — reject — `find /home/renat/.vscode/extensions -name "kilo" -type f 2>/dev/null | head -5` — gate: Read-only find targets /home/renat/.vscode/extensions outside worktree, not in tmp_roots and not named by ticket.
- step-3-7-flash: gate — reject — `ls -la /home/renat/.vscode/extensions/kilocode.kilo-code-7.6.2-linux-x64/ 2>/dev/null | head -10` — gate: Read-only but targets /home/renat/.vscode outside worktree, not in tmp_roots and not named by the ticket.
- step-3-7-flash: gate — reject — `mv docs/kilo-contest/RUN-THE-KILO-CONTEST.md /tmp/kilo-test-backup.md && python3 -m pytest tests/test_kc8_runbook.py -v 2>&1 | tail -20; mv /tmp/kilo-test-backup.md docs/kilo-contest/RUN-THE-KILO-CONTEST.md` — gate: The path /tmp/kilo-test-backup.md is not within the allowed tmp_roots (/tmp/kilo/*, /tmp/contest/*, etc.).
- space-bunny-alpha-bynara: gate — reject — `cat .gitignore | head -60; echo ===; which kilo; ls ~/.vscode/extensions 2>/dev/null | grep -i kilo` — gate: Command reads ~/.vscode/extensions and queries PATH outside worktree, which is not a ticket-named path nor an allowed scratch location.
- space-bunny-alpha-bynara: gate — reject — `ls ~/.vscode/extensions 2>/dev/null | grep -i kilo` — gate: Reads ~/.vscode/extensions outside worktree, not in allowed tmp_roots, and path not named in ticket
- space-bunny-alpha-bynara: gate — reject — `ls -la /tmp/kilo-offer-* 2>/dev/null | tail -3; for f in /tmp/kilo-offer-*; do echo "== $f"; tail -6 "$f"; done 2>/dev/null | tail -30` — gate: Paths /tmp/kilo-offer-* are outside the worktree, not in allowed tmp_roots (/tmp/kilo/*), and not named by the ticket.
- laguna-s-2-1: gate — reject — `cd /home/renat/Project/opensource/github/agent-offline/rounds/47-laguna-s-2-1 && ls contest.ini contest.local.ini 2>/dev/null; echo "---contest-out---"; ls -la contest-out/ 2>/dev/null; echo "---kilo bin---"; which kilo 2>/dev/null; ls ~/.vscode/extensions/kilocode.kilo-code-*/bin/kilo 2>/dev/null; echo "---contest-bench---"; ls contest-bench/ 2>/dev/null` — gate: Command reads ~/.vscode home dotfile outside worktree, not in tmp_roots and not a path named by the ticket.
- laguna-s-2-1: gate — once — `df -P /home/renat/Project/opensource/github/agent-offline/rounds/47-laguna-s-2-1 /tmp/kilo 2>&1 | awk '{print $1, $NF}'` — gate: Read-only df on the worktree and allowed scratch location /tmp/kilo, no writes or destruction
- agnes-3-0-flash: gate — reject — `ls ~/.vscode/extensions/ 2>/dev/null | grep -i kilo; which kilo 2>/dev/null; echo ---; grep -i "contest" .gitignore; ls contest-out 2>/dev/null | head` — gate: Reads ~/.vscode/extensions outside worktree, not in tmp_roots and not a ticket-named path.
- glm-4-7-flash: gate — once — `cd /home/renat/Project/opensource/github/agent-offline && git status` — gate: Read-only git status on the operator-allowed external repo directory that contains the ticket-named docs files.
- glm-4-7-flash: gate — once — `cd ../.. && pwd && git status` — gate: Target /home/renat/Project/opensource/github/agent-offline is the operator-permitted external_directory and git status is read-only.
- glm-4-7-flash: gate — reject — `/home/renat/Project/opensource/github/agent-offline/docs/collect-epics/*` — gate: Edit writes outside the worktree to docs/collect-epics, which is not a tmp_root and the call is not read-only.

## Next

```bash
python3 contest-bench/harness/setup_worktrees.py contest-out/47/entrants.json --wt contest-out/47/wt
python3 scripts/judge_epic_round.py --round 47 --base eb4ff6059cc98cd4ebd67d4ac34012706193e009 --worktree agnes-2-5-flash=/home/renat/Project/opensource/github/agent-offline/rounds/47-agnes-2-5-flash --worktree mimo-v2-5=/home/renat/Project/opensource/github/agent-offline/rounds/47-mimo-v2-5 --worktree step-3-7-flash=/home/renat/Project/opensource/github/agent-offline/rounds/47-step-3-7-flash --worktree space-bunny-alpha-bynara=/home/renat/Project/opensource/github/agent-offline/rounds/47-space-bunny-alpha-bynara --worktree laguna-s-2-1=/home/renat/Project/opensource/github/agent-offline/rounds/47-laguna-s-2-1 --worktree agnes-3-0-flash=/home/renat/Project/opensource/github/agent-offline/rounds/47-agnes-3-0-flash --worktree glm-4-7-flash=/home/renat/Project/opensource/github/agent-offline/rounds/47-glm-4-7-flash
```
````

### The decisions list

Twelve asks reached the gate and none failed: nine `reject`, three `once`.
Every one was a path outside the worktree and outside `tmp_roots`; the other
~250 asks were settled mechanically.

### What the gate did

It kept every agent inside its worktree. The rejects are the pattern worth
knowing: five agents independently went looking for the Kilo binary under
`~/.vscode/extensions`, and the gate refused each time — reading a home
dotfile is not something the ticket names. glm's `Edit` into the operator's
checkout (`../../docs/collect-epics/`) was rejected too; its two `once`
answers were read-only `git status` in that directory. Nothing the ticket
needed was blocked. Five entries ended `READY` (mimo with an uncommitted
test), space-bunny hit the 90 min wall with a deadline commit, and glm gave up
after its progress row kept naming a commit it had since reset away.
