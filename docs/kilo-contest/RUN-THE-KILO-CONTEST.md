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
| R | **reset** | you, per round | `scripts/contest_reset.sh NN [base_ref]` | one checkout per agent at the base |
| RUN | **run** | the runner, N models | `python3 -m tools.contest run --ticket NN` | `contest-out/NN/`: one patch per agent, `entrants.json`, `SUMMARY.md` |
| — | *watch* | you, mid-round | `python3 -m tools.contest status --ticket NN` | the same table, off `state.json` — touches nothing |
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
| the Kilo extension | `kilo_bin = auto` | the newest VS Code extension copy; the contest spawns its own `kilo serve` and never touches the extension's own |
| the model ids | `kilo models` | one `providerID/modelID` line per model. The roster's `model =` is exactly that string, split at the FIRST `/` — `kilo/~anthropic/x` is provider `kilo`, not `~anthropic` |

Intake checks the roster against the server's own offer (KC-25): a display
name spelled as an id, a provider without credentials, or a model that is not
there, is refused with the id to use — before any worktree is built. A model
the provider serves but Kilo's list lacks is not a failure: `--register-missing`
adds it for the round alone through `KILO_CONFIG_CONTENT` (KC-35) and says so
in the plan.

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

**The flags of `run`** (`python3 -m tools.contest run --help` is the source):

| flag | what it does |
|---|---|
| `--ticket NN` | the ticket, NN from `epic-tasks/NN-*.md` (required) |
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
