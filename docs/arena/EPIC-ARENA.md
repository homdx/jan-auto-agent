# EPIC AR — `arena`: one short command for the whole contest flow

**Status:** draft (not started)
**Base:** `main` after `kc` is merged into it (rounds 133, 134, 135 landed first)
**Branch:** `arena`, cut from that `main`
**Tickets:** AR-1 … AR-8 are the MVP, written in full below. AR-9 … AR-58 are the
backlog, written short; each one is drafted into a full ticket (via `arena issue
create`) when its turn comes.
**Reviewed against:** branch `kc` at `9f99300` (the code checked out for the review).
The base this epic names (rounds 133-135 landed, `main` after `kc`) and the commit
`f193004` quoted in §10 were not available to the review. §10 says what was
re-checked, what was corrected, and what must be re-verified on the real base before
AR-1 runs.

---

## 1. Why

Today one round of the contest costs the operator this:

| Step | Today | Pain |
|---|---|---|
| write a ticket | `python3 -m tools.contest draft --target REPO --round NN "brief"` | it **checks out and commits on `contest-legs`**, reuses a stale `contest-legs` whatever HEAD is (ticket 135 landed on the wrong base), and the number is picked by hand to avoid clashes |
| make it runnable | commit the ticket on the round branch by hand | `run` refuses a ticket that is not committed at the base, **and refuses any untracked file in `epic-tasks/`** (`workspace._EPIC_TASKS_DIRTY_REASON`) |
| run | `python3 -m tools.contest run --ticket NN --models a,a,b,c,… --legs K --max-parallel N …` | the same long flag line retyped every round |
| watch | `python3 -m tools.contest status --ticket NN [--out contest-out/NN.K]` | a round of legs needs the right `--out` |
| recover | `scripts/revive_round.py NN [--any-leg]` then `run --ticket NN --resume [--out … --legs 1]` | two commands, the leg form is easy to get wrong |
| land | `git am`/`cherry-pick`, add the `Co-Authored-By: <model>` trailer, reset the author, flip `**Status:**` to `landed`, add the bench in the final commit | all by hand, every round, by a written procedure |

`arena` is a **thin layer** over the code that already does all of this. It
replaces none of it, it removes the retyping and the hand steps.

Helpers that already exist, and that `arena` reuses instead of re-implementing:
`scripts/ticket_status.py` (flips one ticket's `**Status:**` word and its
`epic-tasks/INDEX.md` row; `round NN` cuts a round branch with `git switch`),
`scripts/revive_round.py`, `scripts/judge_round.sh`, `scripts/sync_test_tiers.py`.

## 2. Principles (every ticket obeys these)

1. **Thin layer.** `arena` calls existing functions (`tools.contest.draft.draft_ticket`,
   `tools.contest.cli.cmd_run` / `cmd_status`, `scripts/revive_round.py`,
   `scripts/ticket_status.py`) — in process
   where the function has a clean signature, as a subprocess otherwise. No logic of
   the contest is copied into `tools/arena/`.
2. **Old commands keep working.** `python3 -m tools.contest …` and every script are
   unchanged by the MVP. Nothing in `tools/contest/` is edited by AR-1 … AR-8, except
   where a ticket says so explicitly — and only these do: AR-3 (`cli.intake` / `cmd_run`
   read a ticket that is only at the base), AR-5 (`scripts/revive_round.py`), AR-7
   (`cli.draft_callables`, `draft.draft_ticket(out_dir=)`).
3. **Pass-through.** Everything `arena` does not know yet is passed to the old
   command after `--`: `arena run start 136 -- --no-gate --max-parallel 4`. The MVP
   therefore never takes a capability away.
4. **`<object> <verb>`, always.** Like `gh` (`gh issue create`, `gh run view`). No
   bare verbs at the top level.
5. **The operator decides.** `arena` never picks a winner, never pushes, never
   merges into `main`. It prints the `git push` line instead.
6. **No secrets on screen.** No command prints an `api_key`, a token or a URL with
   a key in it, in table or JSON output or in an error. Every value whose key is, or contains as a
   whole word, `key`, `token`, `secret` or `password` is shown as `***` (`tokens`, a
   usage count, is not a token and stays readable). A URL's `user:pass@` part and any
   `key=…` / `token=…` value are scrubbed from every printed string.
7. **No checkout switch.** `arena` never runs `git checkout`/`git switch` in the
   operator's checkout. Branches are written with plumbing (`commit-tree`,
   `update-ref`) — see §4.
8. **Explicit paths in every commit** `arena` makes (`git add -- <paths>` for a
   new file, then `git commit -- <paths>`: git refuses a path it does not know yet), the
   author is the checkout's own `user.name`/`user.email`, and the message ends with
   the trailers the ticket names.
9. **Refusals are one line each, on stderr, and say how to get past them.**

## 3. Command shape (the whole epic, for orientation)

The MVP is marked **[MVP]**. Everything else is backlog (§7).

```
arena [-p PROFILE] [-o table|json] [-y] [-R REPO: AR-9] <object> <verb> … [-- <old flags>]

profile   list | view NAME                                       [MVP AR-2]
          set NAME KEY=VALUE [-y]                                AR-61 (landed, 142)

issue     create "brief" [--number NN] [--no-review]             [MVP AR-7]
          list [--state …] | view NN                             [MVP AR-6]
          land NN [-m SUBJECT] [--score TEXT] [--note TEXT] [-- PATH…]  [MVP AR-8]
          edit NN | close NN --reason T | reopen NN | queue NN   AR-14
          lint NN | review NN | validate NN                      AR-17 AR-18 AR-31
          create --blank | --after NN | --epic NAME              AR-15
          suggest | create --from FILE|--from-run|--from-suggest AR-55 AR-56

run       start NN [--branch B] [--fresh-ticket] [-- old flags]  [MVP AR-3]
          (legs/models/variant/max_parallel/fresh — profile keys, AR-61)
          list                                                   [MVP AR-3]
          view NN[.K]                                            [MVP AR-4]
          rerun NN[.K] --failed | --agent A [--dead] [--dry-run] [MVP AR-5]
          plan NN | prompt NN [AGENT]                            AR-32
          watch NN | log NN [AGENT] [-f] | wait NN               AR-35 AR-36
          gate NN [--agent A] [--rejects]                        AR-37
          resume NN | cancel NN                                  AR-38
          report NN | clean NN                                   AR-54

entry     merge NN AGENT [--uncommitted] [--squash]              [MVP AR-8]
          list NN | diff NN AGENT                                AR-47
          score NN [--on-base|--quick]                           AR-48 AR-49
          analyze NN | fix NN AGENT                              AR-50 AR-53

repo      add | list | set-default | status | refresh [--deep]   AR-9 … AR-12
bench     create NN | check NN | run NN [--tree P]               AR-22 … AR-24
base      check                                                  AR-25
model     available [PROVIDER…] [--free] [--search T] [--test]  AR-59 (landed, 140)
          use | drop NAME[,NAME…] -p P                           AR-59 (landed, 140)
          test NAME[,NAME…] [--url U]                            AR-60 (landed, 141)
          test tools|judge | stats                               AR-41 … AR-46
judge     list | test gate|reviewer                              AR-45
config    view | get | set | validate [--models …]               AR-13 AR-19 … AR-21
doctor                                                           AR-26 AR-27
check     [--stress N]                                           AR-51
epic      list | view NAME | next NAME                           AR-57
init                                                             AR-16
```

## 4. Key design decisions (read before any ticket)

### 4.1 Where a ticket lives before its round: `.arena/drafts/`

`run` reads the ticket **from the base commit** (`cli._ticket_body(..., at=base)`)
and `workspace._check_base_and_epic_tasks` **refuses any untracked or modified file
under `epic-tasks/`** in the checkout. So a draft cannot sit in `epic-tasks/`
untracked: it would block every round on the machine (the untracked 135 does that
today).

Decision:

- `arena issue create` writes the draft to **`.arena/drafts/NN-<slug>.md`**.
  `.arena/` is added to `.gitignore` (AR-1).
- `arena run start NN` puts the ticket into `epic-tasks/` **on the round branch
  only** (§4.2), never in the operator's checkout — and `run` is made to read it there
  (AR-3 step 0).
- `arena issue land` puts it into `epic-tasks/` on the integration branch, with
  `**Status:** landed`, in the **closing** commit after the winner's (§4.3).
- A ticket already in `epic-tasks/` (committed, the old way) is accepted by every
  command: `.arena/drafts/` is just where new ones wait.

### 4.2 The round branch, without a checkout switch

`arena run start NN` builds the base the agents start from:

```
integration branch (profile key `branch`, default: the current branch)
   └── arena-round/NN   = one commit: "NN: ticket for the round" — adds epic-tasks/NN-*.md
```

- Written with plumbing in a **temporary index** (`GIT_INDEX_FILE=<tmp>`):
  `read-tree <integration>` → `update-index --add --cacheinfo 100644,<blob>,epic-tasks/<name>`
  → `write-tree` → `commit-tree -p <integration>` → `update-ref refs/heads/arena-round/NN`.
  The operator's checkout, index and HEAD are untouched.
- The round runs with `--base arena-round/NN`. `run` already takes `--base` (default
  `HEAD`); the worktrees are built at the base and the ticket is read from it.
- The `arena-round/NN` commit **never reaches the integration branch**: the winner's patch
  is taken on its own (`git am` of the patch file / the branch's one commit
  above `arena-round/NN`), and the ticket lands in the merge commit.
- The ref prefix is `arena-round/`, **not** `arena/`: git cannot hold a branch
  `arena` (this epic's own branch) and a branch `arena/NN` at once (a ref cannot be
  both a file and a directory).
- If `arena-round/NN` exists: same ticket text **and** the same parent (the integration
  tip) → reused (a re-run). Different text, or a moved tip → refusal naming
  `--fresh-ticket`, which rewrites the ref — only when no `contest-out/NN` or
  `contest-out/NN.K` folder holds a `state.json`, or with `-y` (a `--resume` needs the
  same base, so a started round's ref is never rewritten silently).
- `arena run start` records the round in `.arena/rounds/NN.json` (`branch`, `base_ref`,
  `base_sha`, `ticket_sha256`, `started_at`). `entry merge` and `issue land` read it
  (AR-8).

**Checked against `kc` @ `9f99300` — and not enough on its own.** `run` takes a base
that is not `HEAD`: `intake` reads every `**Status:**` and the lower-numbered tickets
at the base (`_ticket_body(..., at=base)`, `_tickets(..., at=base)`), and `_on_offer`'s
`base_is_head=False` branch only changes the hint it prints. **But `intake` finds the
ticket itself in the checkout:** `gates.ticket_for_round(tasks_dir, NN)` lists the
checkout's `epic-tasks/`, `intake` fails with `no ticket numbered NN` when
`tasks_dir/<name>` is not a file there, `cmd_run` reads `ticket_size(...)` from it, and
the runner's `declared_files(ticket_path)` reads it from disk. A ticket that lives only
on `arena-round/NN` and in `.arena/drafts/` is invisible to the runner, and the
checkout cannot hold it (the dirty check, §4.1). AR-3 therefore changes
`tools/contest/cli.py` in one place (AR-3 step 0): a ticket that is not in the
checkout but is at the base is read from the base. AR-3 pins it with a real
`--dry-run` intake on `--base arena-round/NN` with the ticket absent from the checkout.

**Known effect:** intake refuses a ticket while a **lower-numbered** ticket is still
`open` at the base. `arena-round/NN` is cut from the integration branch, so an older
ticket committed there as `open` blocks it; intake's own message says how to park
it. `arena` does not park tickets silently.

### 4.3 Landing: the winner's commit, then the closing commit

The operator's practice (e.g. `8c58a1d` + `9f99300` for round 132, `9bd4871` +
`dafbd20` for 131), made mechanical. **Two kinds of commit, never mixed:**

**1. The winner, as-is** — `arena entry merge NN AGENT`:

```
<the winner's own subject, "NN: " prepended when it has no NN: / AR-N: prefix>

<the winner's own body, as-is>

Co-Authored-By: <model> <<model>@round-NN.contest>
```

- Only the winner's code. Paths under `epic-tasks/` and `runs/` are **dropped**
  from the patch (the agent's own ticket/progress files; the ticket file does not
  even exist on the integration branch) and the dropped paths are printed.
- Author: the checkout's `user.name`/`user.email`, never the patch's `From:` (a
  patch made on another machine carries that machine's address).
- `<model>` is the agent's `model_id` from `state.json` put through the exact rule
  `cli.agents_from_models` names an agent by, before any `-varN`: the part after the
  last `/`, cut before the first `:`, lower-cased, every character that is not
  alphanumeric, `_` or `-` turned into `-`, leading `_`/`-` stripped. So `GLM-4.7` is
  `glm-4-7`, a model `…-6.8-…` is `…-6-8-…`, and the agent's `-var2` is not in it,
  exactly as in `8c58a1d` (agent `sensenova-6-8-flash-lite-var2`, trailer
  `sensenova-6-8-flash-lite`). The `-varN` is never stripped from the agent name (a
  model may itself end in `-var1`); the id is recomputed from `model_id`, and AR-8's
  test pins it equal to `cli.agents_from_models(<model_id>)[0].name` for a model that
  appears once.
- No `**Status:**` change, no bench. If the winner needs fixing, the operator
  fixes it now, in the working tree.

**2. The closing commit** — `arena issue land NN [-- extra paths]`:

```
NN: ticket landed, and contest-bench/NN holds the round's acceptance bench
    (or, with follow-up edits: "NN: <what the follow-up fixes>; ticket landed …")

Round NN: <READY>/<TOTAL> READY; winner <agent> taken as-is in <sha7>.
[--note TEXT, appended as given: the bench score, the test totals]

Co-Authored-By: <profile trailer>          ← only when configured
```

- `epic-tasks/NN-*.md` (taken from `arena-round/NN`) with its status line in the form
  every landed ticket in `epic-tasks/` carries (131, 132):
  `**Status:** landed — round NN, winner <agent> (<score>), <sha7> as-is`, or
  `<sha7> + follow-up` instead of `as-is` when paths are named after `--` (the
  follow-up fix rides in this commit). `<score>` is `--score TEXT` as given — the bench
  result, e.g. `28/28 on contest-bench/131`; without `--score` the parenthesis is left
  out (READY/TOTAL is not the score: 131's line says `28/28 on contest-bench/131`).
  Winner and sha come from `.arena/merged.json`; without a record: `landed — round NN`;
  that ticket's `epic-tasks/INDEX.md` row, when the row exists, gets the same first
  word — the edit `scripts/ticket_status.py` already makes, so `arena` imports its
  `STATUS_RE` / `INDEX_ROW` instead of writing a second one;
  `contest-bench/NN/` when present, plus every path the operator names after `--`
  (the follow-up fix). **Explicit paths only** — never `-a`. A new file is staged with
  `git add -- <path>` first: `git commit -- <path>` refuses a path git does not know.
- The ticket + bench therefore go into the **final** commit, whether it is a pure
  "ticket landed" commit or the follow-up that fixed the winner. No separate
  status commit ever exists.
- A larger follow-up (more than a few lines) is its own commit by hand first, then
  `issue land` closes. Regressions found later: separate commits, as always.

### 4.4 Ticket state is computed, `**Status:**` is written by `arena` only

`scripts/next_task.py` and `intake` read `**Status:**` (`open`, `queued`, `landed`),
so the field stays. The operator stops editing it; `arena` derives a richer state:

| State | Rule (first match wins) |
|---|---|
| `landed` | `**Status:** landed` in `epic-tasks/NN-*.md` on the integration branch |
| `closed` | `**Status:** closed` (AR-14, which must also add `closed` to `cli.PARKED` and to `scripts/next_task.py` `SKIP_STATUS` — today a `closed` ticket would count as still on offer and block every higher round at intake) |
| `running` | `contest-out/NN[.K]/state.json` exists and the round's runner process is alive (§4.6) |
| `done` | `state.json` exists, no live runner |
| `queued` | `**Status:** queued` |
| `draft` | only in `.arena/drafts/` |
| `open` | in `epic-tasks/`, `**Status:** open`, no `state.json` |

Mismatches are flagged in `issue list` (`!`), e.g. a commit subject `NN: …` on the
integration branch while the md still says `open`.

### 4.5 Numbering

The next number is **max + 1** over: `epic-tasks/*.md` (checkout and integration
branch), `.arena/drafts/*.md` (not `*.rejected.md`), `contest-out/<N>` and
`contest-out/<N>.<K>` folder names (zero-padded by `cli._round_out_dir`, read as
integers, §4.8), and `refs/heads/arena-round/*`. Not the first gap: `draft.next_round` returns the
first free number from 1, which would hand out an old, already-used round number
whose `contest-out/` still exists. `--number NN` overrides, and is refused when
any of the sources above already holds NN.

### 4.6 "Is the round alive?"

`state.json` carries no pid. A round is alive when a process exists whose
`/proc/<pid>/cmdline` contains `tools.contest` and `run` and `--ticket` `NN`
(any form, `--ticket=NN` too) **and whose `/proc/<pid>/cwd` is this repo** (two
checkouts on one machine may both run a round NN), **or** `.arena/locks/NN.pid`
names a live pid whose cmdline still matches (a pid reused by an unrelated
process after a crash is not a live round). Written by `arena run start`. Used by `run rerun` (refuse when alive), `entry merge`
and `issue list` (`running` vs `done`). The helper lives in `tools/arena/rounds.py`
(AR-3) with the round-folder resolver (§4.8); AR-4, AR-5, AR-6 and AR-8 import it.
`/proc` is read through `cli._PROC_ROOT`; a process whose `cwd` cannot be read is not
matched. A round started by `draft --run` has no separate `run` argument and is not
seen — start rounds with `arena run start`.

### 4.7 Profiles

In `contest.local.ini` (never committed; `roster.load_roster` ignores unknown
sections, so the runner is not affected):

```ini
[arena]
profile = default                 ; the active one

[arena.profile.default]
branch     = kc                   ; integration branch (default: current branch)
models     = provider/a,provider/a,provider/b      ; placeholders here, real ids in the local file
legs       = 1
max_parallel =
extra      = --no-tests           ; raw old flags appended to `run`
trailer    = Claude Opus 5.5 <noreply@anthropic.com>   ; optional second Co-Authored-By
```

Each key maps to exactly one `run` flag (`models`→`--models`, `legs`→`--legs`,
`max_parallel`→`--max-parallel`, `backend`→`--backend`, `provider`→`--provider`,
`variant`→`--variant`); `extra` is raw text, `branch` and `trailer` are arena's own.
`base` is never a key — it is always `arena-round/NN`. An unknown key is a refusal, so
the keys live in one registry, `profile.KNOWN_KEYS`, which a later ticket extends when it
adds a key (`min_tools_score` in AR-21, `on_finish` in AR-36). An empty value means "the
flag is not passed" (so the roster's own default applies). A command-line flag beats the
profile; `--` flags are appended last. The files are read with the roster's own parser
shape (`roster._new_parser()`: `interpolation=None`, inline comments after `;` / `#`);
with a plain `ConfigParser` the `; …` notes in the block above would become part of the
values.

### 4.8 Names, numbers and the round's folder

- `NN` is an integer. What the old code makes is zero-padded to two digits —
  `epic-tasks/07-x.md` (`draft_ticket`), `contest-out/07` and `contest-out/07.2`
  (`cli._round_out_dir`, `cli._leg_out_dir`), branches `contest/07/<agent>`
  (`workspace`) — and every `arena` reader parses those names back to an integer. A ref
  `arena` makes is not padded: `arena-round/7`. Tests use the padded names wherever the
  old code would make them.
- One resolver, `rounds.round_folder(repo, config, NN[, K]) -> Path`, in
  `tools/arena/rounds.py` (AR-3): `NN.K` is that leg's folder; a bare `NN` is the highest
  `NN.K` when legs exist and `<out_dir>/NN/state.json` does not (the rule of
  `revive_round.state_path`), else `<out_dir>/NN`. `<out_dir>` comes from the roster,
  never a literal `contest-out`; the leg listing is `revive_round.leg_folders`, imported.
  `run view`, `run rerun`, `entry merge` and `issue land` all use it.

---

## 5. Flow

```
              ┌───────────── ticket ─────────────┐
 brief ─▶ arena issue create ─▶ collect (Pass A) ─▶ writer ─▶ lint ─▶ reviewer
              │                                                 │ ✗ ≤3 reworks
              ▼                                                 ▼
        .arena/drafts/NN-*.md  ◀─────────────────────── ok ─────┘
              │   (operator reads / edits the md)
              ▼
 ┌──────────── round ───────────────────────────────────────────────────┐
 │ arena run start NN                                                   │
 │   profile → flags;  arena-round/NN = integration + ticket  (plumbing)      │
 │   python3 -m tools.contest run --ticket NN --base arena-round/NN …         │
 │   legs NN.1 … NN.K, agents ⇄ gate, harvest → READY/STALLED/…         │
 │                                                                      │
 │ arena run list / arena run view NN[.K]      (watch)                  │
 │ arena run rerun NN --failed                 (revive + --resume)      │
 └──────────────────────────────┬───────────────────────────────────────┘
                                ▼
 ┌──────────── judge (by hand in the MVP) ──────────────────────────────┐
 │ contest-bench/NN in each worktree, read the diffs, pick AGENT        │
 └──────────────────────────────┬───────────────────────────────────────┘
                                ▼
 arena entry merge NN AGENT     → commit 1: the winner as-is (model trailer, own author)
   (operator fixes the winner in the tree if needed)
 arena issue land NN [-- paths] → commit 2: ticket landed + bench (+ the fix)
   prints: run tests, then tests_bugfix, then `git push …`  (never runs push)
```

---

## 6. MVP tickets (full)

Order and dependencies:

```
AR-1 ── AR-2 ── AR-3 ─┬─ AR-4
                      ├─ AR-5
                      └─ AR-6 ─┬─ AR-7
                               └─ AR-8
```

All are size **S** except AR-3 and AR-8 (**M** — AR-3 writes a git ref and starts
the runner, AR-8 makes the two landing commits; both need careful tests). Each ticket is one round.

Common rules for every AR ticket:

- Code under `tools/arena/`, tests in `tests/test_arena_<area>.py` (one-line
  docstring each), tiered with `python3 scripts/sync_test_tiers.py`, then
  `python3 scripts/sync_test_tiers.py --check`.
- Tests use a throw-away git repo in `tmp_path` and **never** start `kilo`, call a
  model, or touch the real `contest-out/`. The old command is faked by
  monkeypatching the function `arena` calls (or by a stub on `PATH` for a
  subprocess).
- A test that waits for a child process or a file waits on an event the child creates (a
  marker file, a pipe), never on a sleep or a tight timeout: `scripts/check_test_clocks.py`
  (FL-4) and the parallel runs hold every test to that.
- Acceptance is always these two, **run one after the other, never in one
  command**:

  ```bash
  python3 -m pytest tests -n 8 -q
  ```
  ```bash
  python3 -m pytest tests_bugfix -n 8 -q
  ```
- Landed like every round (§4.3): the winner's commit as-is with its model
  trailer, then the closing commit (ticket landed + bench). Subjects start with
  the round number `NN:` (§9), explicit paths.

---

### AR-1 — skeleton: `arena <object> <verb>`, global flags, exit codes, masking

**Size:** S · **Depends on:** — · **Files:** `tools/arena/__init__.py`,
`tools/arena/__main__.py`, `tools/arena/cli.py`, `tools/arena/output.py`,
`arena` (repo-root launcher), `.gitignore`, `tests/test_arena_cli.py`

**Why.** Every later ticket adds one `<object> <verb>`. Without one parser, one
output helper and one masking rule, each ticket would invent its own and the
`-o json` / secret rules would drift.

**What to build.**

- `python3 -m tools.arena …` and an executable `./arena` at the repo root (mode
  `100755`; a short Python launcher that puts its own directory first on `sys.path` — so
  it works from any working directory and the `scripts/` imports of later tickets
  resolve — and runs `tools.arena.cli.main`). `githooks/pre-commit` only rejects a new
  root `*.py` file; `arena` has no extension, so the hook passes it.
- `argparse` with two levels of sub-parsers: `<object>` then `<verb>`. Objects are
  registered from a table (`OBJECTS = {"profile": …, "issue": …, "run": …, "entry": …}`),
  so a later ticket adds one entry. In AR-1 every object has only `--help`; a verb
  that is not implemented yet prints `arena: <object> <verb> is not implemented yet
  (AR-N)` and exits 2.
- Global flags, before the object: `-p/--profile NAME`, `-o/--output table|json`
  (default `table`), `-y/--yes`.
- `--` handling: `main` splits `argv` at the first standalone `--` **before** argparse
  sees it (argparse swallows a `--` on its own), parses the left part, and keeps the
  right part verbatim in `args.passthrough` (a list) — `arena` never parses it. No `--`
  gives `[]`.
- Usage errors: `ArgumentParser.error` is overridden to print one line, `arena:
  <message>`, and exit 2 (argparse's default adds a usage block; principle 9 says one
  line).
- Exit codes, one module-level table: `0` ok, `1` the action failed, `2` usage or a
  refusal before anything was done, `3` "nothing to do" (e.g. no rounds to list), `4`
  "the round ran and ended with no READY agent" (the old `run` command's own `2`,
  mapped in AR-3 so that it cannot be taken for a refusal). The old `run` also exits `2`
  on an argparse error — a mistyped flag after `--` — so a child's `2` is `4` only when
  the round ran (AR-3 step 5); otherwise it is `1`.
- `output.py`:
  - `emit(rows, columns, fmt)` — a plain left-aligned table, or `json.dumps(rows)`.
  - `mask(mapping)` — returns a copy where the value of every key that is `key`,
    `apikey`, `token`, `secret`, `password` or `passwd`, or contains one of them as a
    whole word (words split on `_`, `-`, `.` and camelCase; case-insensitive), is
    replaced by `***`; recursive over dicts/lists. `api_key`, `API_KEY`, `gate_token`,
    `apiKey`, `client-secret` are masked; `tokens` (a usage count — `run view -o json`
    prints it), `max_tokens`, `monkey` and `base_url` are not. `emit` always passes rows
    through `mask`.
  - `scrub(text)` — returns *text* with a URL's `user:pass@` part and every
    `api_key|key|token|secret|password=<value>` replaced by `***`. `emit` applies it to
    every string value, `refuse` to its message (principle 6: a URL with a key in it
    never reaches the screen).
  - `refuse(msg)` — prints `arena: <scrubbed msg>` to stderr and returns 2.
- `.gitignore`: add `.arena/`.

**Tests** (`tests/test_arena_cli.py`):

1. `arena --help` lists the four objects; `arena issue --help` exits 0.
2. An unknown object exits 2 with exactly one stderr line (`arena: …`, no usage block).
3. `arena run start 5 -- --no-gate --max-parallel 4` → `passthrough ==
   ["--no-gate", "--max-parallel", "4"]` and `5` parsed as the ticket (use a fake
   verb handler registered in the test); the same call without `--` has
   `passthrough == []`.
4. `mask` hides `api_key`, `API_KEY`, `apiKey`, `gate_token`, nested values, values in
   lists; leaves `model`, `base_url` (without a key in it) and `tokens` (a number) alone.
5. `emit(..., "json")` output parses back with `json.loads` and contains `***`
   where a key was.
6. `scrub` / `refuse`: `https://u:p@host/v1?api_key=abc&x=1` and `token=xyz` come out
   with `***` in the stderr line.
7. `./arena` is executable and `./arena --help` exits 0 when started from another
   working directory (subprocess).

**Acceptance.** The two commands in §6.

**Rules.** No `tools/contest/` change. Heavy explanatory comments, like
`tools/contest/cli.py`.

---

### AR-2 — profiles: `[arena.profile.NAME]` → the `run` flag line

**Size:** S · **Depends on:** AR-1 · **Files:** `tools/arena/profile.py`,
`tools/arena/cli.py`, `contest.ini` (comment block only, placeholders),
`tests/test_arena_profile.py`

**Why.** The long `run` line is the same every round. A named profile holds it once.

**What to build.**

- `load_profiles(repo) -> dict[name, dict]`: reads `contest.ini` then
  `contest.local.ini` (the local file wins key by key) with the roster's own parser
  shape, `roster._new_parser()` (no interpolation, inline comments after `;` / `#` —
  the §4.7 example needs both; import it, do not build a second `ConfigParser`),
  sections `[arena.profile.<n>]`, and `[arena] profile = <n>` for the active
  one (default `default`). Uses `roster.LOCAL_FILENAME` for the local name — no
  second constant.
- Known keys live in one registry, `KNOWN_KEYS` (later tickets add to it, §4.7), and
  their flag: `models`→`--models`, `legs`→`--legs`, `max_parallel`→`--max-parallel`,
  `backend`→`--backend`, `provider`→`--provider`, `variant`→`--variant`, `extra`
  (split with `shlex.split`, appended raw), `branch` (not a flag — used by AR-3/AR-8),
  `trailer` (AR-8). An unknown key is a refusal naming the section and the key.
- `profile_flags(profile, overrides) -> list[str]`: the flag list for `run`. Empty
  values are skipped. `overrides` (from the command line) replace profile values.
  `--base` and `--ticket` are never produced here (refusal if `extra` holds them:
  "`--base`/`--ticket` are set by arena").
- `arena profile list` — `NAME ACTIVE MODELS LEGS BRANCH` (models shortened to a
  count: `5 agents (3 models)`); `arena profile view NAME` — every key, masked.
- `-p NAME` picks the profile for one command; an unknown name is a refusal that
  lists the known ones.
- `contest.ini`: a commented example `[arena.profile.default]` block with
  **placeholder** model ids only (`provider/model-a`). The roster sections already in
  that file name real models and are not touched.

**Tests.**

1. Local file overrides a key of the committed file; a key only in the committed
   file survives.
2. `profile_flags` order is stable: models, legs, max_parallel, backend, provider,
   variant, then `extra`.
3. Empty `legs =` → no `--legs` in the list.
4. An override `legs=3` beats the profile's `legs = 1`.
5. `extra = --base X` → refusal.
6. Unknown key → refusal naming `[arena.profile.default] colour`.
7. `profile view` output never contains a value of a `*key*` field (mask).
8. No profile at all → `profile list` exits 3 with "no `[arena.profile.*]`".
9. `legs = 2   ; two legs` reads as `2` (the inline comment is not part of the value),
   and a `%` in a value does not raise.

**Rules.** Never read or print `agents_128k.ini`. No real names in the new block.

---

### AR-3 — `arena run start NN` and `arena run list`

**Size:** M · **Depends on:** AR-2 · **Files:** `tools/arena/rounds.py`,
`tools/arena/gitref.py`, `tools/arena/cli.py`, `tools/contest/cli.py` (step 0 only),
`tests/test_arena_run_start.py`, `tests/test_arena_run_list.py`

**Why.** This is the main simplification: one short command builds the round's
base and starts the old runner with the profile's flags. It also owns the helpers the
other MVP tickets import: liveness (§4.6) and the round-folder resolver (§4.8).

**What to build.**

0. **The runner reads a ticket that is only at the base** — the one `tools/contest/`
   edit of this ticket (§4.2, "Checked"). Add `cli.ticket_file(repo, tasks_dir,
   round_no, at) -> (name, path)`: the checkout's file when it exists (today's path,
   byte for byte), else the ticket `git show <at>:epic-tasks/<name>` written to a file
   **with the same name `<name>`** in a fresh folder under the system temp dir (callers
   read `path.name`), removed when `cmd_run` returns, else `("", None)` (which
   keeps today's `no ticket numbered NN` line). `intake` and `cmd_run`'s `ticket_size`
   read ask it **only when `gates.ticket_for_round` found nothing**, so the old path is
   untouched; the result is what `Intake.ticket_path` carries. `tests/test_contest_cli*.py`
   stay green unedited. If this step makes the ticket larger than M while drafting, split
   it off as its own round first and let AR-3 depend on it.
1. **Find the ticket** NN: `.arena/drafts/NN-*.md` (not `*.rejected.md`), else
   `epic-tasks/NN-*.md` in the checkout, else on the integration branch (`git ls-tree`).
   None → refusal. More than one file for NN → refusal listing them.
   A ticket already committed on the integration branch with the same text →
   `arena-round/NN` is pointed at the integration tip itself (no empty commit).
2. **Integration branch**: `--branch` flag, else profile `branch`, else the
   current branch (`git symbolic-ref --short HEAD`; detached HEAD → refusal).
3. **Build `arena-round/NN`** with plumbing in a temporary index (§4.2), in
   `gitref.commit_file_on(repo, parent, path_in_repo, content, message, ref)`.
   - The ticket file name in `epic-tasks/` is the draft's own name.
   - `**Status:**` is forced to `open` in the committed copy (a draft written by
     hand may say anything).
   - Subject: `NN: ticket for the round`, author = the checkout's identity.
   - Existing `arena-round/NN` with the same ticket text **and** the same parent →
     reused; otherwise refusal with `--fresh-ticket` named; `--fresh-ticket` rewrites it
     only when no `contest-out/NN` / `NN.K` folder holds a `state.json`, or with `-y`.
   - Write `.arena/rounds/NN.json` (§4.2).
4. **The run line**: `[sys.executable, "-m", "tools.contest", "run", "--ticket", NN,
   "--base", "arena-round/NN", *profile_flags(...), *passthrough]`. `--ticket`,
   `--base` and `--target` in the passthrough are refused (arena sets them), and so is
   `--out` (the default folder is the one `run list` / `run view` look in; `run rerun`
   sets `--out` itself for a leg). Printed in full before it starts (masked — it never
   contains a key, but `mask` and `scrub` run anyway).
5. **Run it** as a child process in the foreground, stdout/stderr inherited, so the
   operator sees the runner's own lines exactly as today. Write
   `.arena/locks/NN.pid` with the child's pid, remove it when the child exits.
   Exit code: the child's `0` → `0`, `1` → `1`, `2` → `4` (AR-1's table) **only when the
   round ran** — the round folder's `state.json` (`rounds.round_folder`) exists and was
   written after the child started; a `2` without it is argparse refusing a flag (e.g. a
   typo after `--`) and becomes `1`, with the child's own stderr line already on screen.
   So arena's own refusals (`2`) stay distinguishable, and a typo is never "no READY".
6. **Refuse before anything is written** when: a live round NN exists (§4.6);
   `epic-tasks/` in the checkout is dirty (the same check the runner makes,
   reported early with the file list, plus the hint "move drafts to
   `.arena/drafts/`").
7. `arena run list`: one row per `contest-out/<N>` or `contest-out/<N>.<K>` group
   (legs collapsed: `N` with `LEGS k/K`), columns `RUN LEGS STATE AGE READY/TOTAL`,
   newest first. Only directories named `^\d+(\.\d+)?$` count (`<out_dir>` also holds
   files such as `probe-memory.json`). `STATE` is `running`/`done` (§4.6); READY and
   TOTAL from the last leg's `state.json` (`agents[].state`). An unreadable
   `state.json` is a row with `STATE = ?`, never a crash.
   The output folder comes from the roster's `out_dir` (`rounds.round_folder`, §4.8,
   over `cli._round_out_dir`), not a hard-coded `contest-out`.

**Verify first (and write the result into the commit message).** Read `cli.intake`'s
`gates.ticket_for_round` call and `cli._on_offer`'s `base_is_head` branch, and check
that step 0 is all `run` needs for a base that is not `HEAD`. Test 6 proves it with a
real intake in `--dry-run` (no kilo: intake's server and provider checks are
monkeypatched to pass, exactly as `tests/test_contest_cli*.py` already do — reuse their
fixtures' approach).

**Tests.**

1. `commit_file_on` creates `arena-round/7` whose parent is the integration tip, whose
   tree = the tip's tree + `epic-tasks/07-x.md`; the checkout's `HEAD`, index and
   working tree are byte-identical before/after (`git status --porcelain` empty,
   `git rev-parse HEAD` unchanged).
2. Same ticket twice → the same commit sha (reuse).
3. Changed ticket text → refusal; with `--fresh-ticket` and no `state.json` → new sha;
   with a `state.json` and no `-y` → refusal. The same text on a moved integration tip →
   the same refusal.
4. The built run line for a profile `models=a,a,b legs=2` + `-- --no-gate` is
   exactly `[..., "run", "--ticket", "7", "--base", "arena-round/7", "--models", "a,a,b",
   "--legs", "2", "--no-gate"]` (the child is a stub that records argv).
5. Dirty `epic-tasks/` → exit 2, nothing created (no `arena-round/7` ref, no
   `.arena/rounds/7.json`).
6. Real `tools.contest run --ticket 7 --base arena-round/7 --dry-run` in the tmp repo,
   **with the ticket absent from the checkout's `epic-tasks/`** (it exists only on
   `arena-round/7` and in `.arena/drafts/`), passes intake and sizes the round from the
   ticket's `**Size:**`; a ticket present in the checkout behaves as before.
7. `run list` on a fake `contest-out/` with `05/`, `06.1/`, `06.2/`, a broken
   `state.json` and a stray `probe-memory.json` → rows sane, `6` shown once with
   `LEGS 2/2`, the broken row `?`, the file ignored.
8. Lock file exists while the stub child runs (the stub creates a marker file the test
   waits for — no sleeps) and is gone after.
9. A stub child that writes `state.json` and exits `2` → arena exits `4`; a stub that
   exits `2` without writing one (an argparse error) → `1`; `1` → `1`; `0` → `0`.

**Rules.** No `git checkout`/`switch` anywhere. Never pass `--fresh` to `run`
implicitly.

---

### AR-4 — `arena run view NN[.K]`

**Size:** S · **Depends on:** AR-3 (its `rounds.round_folder` and liveness) · **Files:**
`tools/arena/rounds.py`, `tools/arena/cli.py`, `tests/test_arena_run_view.py`

**Why.** `status` needs `--out contest-out/NN.K` for a round of legs; the operator
should type `arena run view 134` and see the round.

**What to build.**

- `NN.K` → that leg exactly; a bare `NN` → `rounds.round_folder` (§4.8): the **last**
  leg folder when legs exist (the rule of `revive_round.state_path`, via the imported
  `leg_folders`), else `<out_dir>/NN`.
- Calls `tools.contest.cli.cmd_status` with a namespace
  `(ticket=NN, out=<folder>, roster=DEFAULT_ROSTER)` — in process, its output is the
  table the operator already knows.
- Above the table, one line: `round NN · leg K/K · <running|done> · base <sha7>`.
- `-o json`: the raw `state.json` agents list (`agent, state, attempt, tokens,
  commit`), masked — `tokens` stays a number (AR-1's `mask` keys on whole words).
- No folder → exit 1, `no round NN (looked in <path>)`.

**Tests.** legs → last leg chosen; `134.1` → leg 1; no legs → plain folder;
missing → exit 1; JSON parses, has one entry per agent, and `tokens` is not `***`.

---

### AR-5 — `arena run rerun NN[.K] --failed | --agent NAME [--dead] [--dry-run]`

**Size:** S · **Depends on:** AR-3 · **Files:** `tools/arena/rounds.py`,
`tools/arena/cli.py`, `scripts/revive_round.py`, `tests/test_arena_run_rerun.py`

**Why.** Recovery is two commands today and the leg form needs four flags.

**What to build.**

- Refuse when the round is alive (§4.6) — the revive script's own docstring warns a
  live runner undoes it; `arena` enforces it.
- `scripts/revive_round.py` has no function that revives: the loop is inline in `main`,
  and `leg_folders` / `state_path` are all it offers. This ticket extracts the loop into
  `revive_agents(agents, only=None) -> list[str]`, called by `main` with its output, its
  `state.before-revive.json` and its exit codes unchanged, and adds `--agent NAME` to
  `main` — the only `scripts/` change in the MVP. `arena` imports the module: `scripts/`
  has no `__init__.py`, so it imports as a namespace package, because the repo root is on
  `sys.path` for `python3 -m tools.arena`, for the launcher and for the tests'
  `conftest.py`.
- `--failed`: `revive_agents` over the resolved `state.json` (`--dry-run` passes
  through and stops there).
- `--agent NAME`: revive only that agent — same states (`STALLED`, `GAVE_UP`, `ERROR`);
  an agent in another state → refusal naming its state; an unknown name → refusal
  listing the names.
- `--dead`: `DEAD` agents come back too (round 141: hy3, longcat and laguna were
  `DEAD` — "nothing modified in 2 rounds of the 420s first-touch clock" — and the
  revive script could not bring them back). `DEAD` is never revived without the flag:
  the agent already failed the first-touch rule, so bringing it back is the
  operator's explicit choice.
  - `scripts/revive_round.py` gets the same `--dead` flag; `revive_agents(agents,
    only=None, dead=False)` adds `DEAD` to `REVIVE` when `dead` is true. Without the
    flag its output and exit codes are unchanged.
  - With `--agent NAME`: a `DEAD` agent without `--dead` → refusal naming `DEAD` and
    the flag (`arena: hy3 is DEAD — add --dead to bring it back`).
  - A revived `DEAD` agent restarts in its own worktree with a fresh attempt budget
    and a fresh first-touch clock, exactly like a revived `STALLED` one; its old
    state is kept under `revived_from` (`state: DEAD`, the reason).
  - `--dry-run` lists the `DEAD` agents it would revive only when `--dead` is given.
- Then start the round again through AR-3's path with `--resume`:
  - no legs (`<out_dir>/NN/` only): `run --ticket NN --base <base> <profile
    flags> --resume`;
  - a round of legs: the revive docstring's own form, **for the last leg too** —
    `run --ticket NN --base <base> --out <out_dir>/NN.K --legs 1 --resume`
    (`<out_dir>` from the roster, `rounds.round_folder`, §4.8 — never a literal
    `contest-out`)
    (`--legs` from the profile must not leak in: the profile's `legs` is dropped
    and `--legs 1` set; `cmd_run` refuses `--resume` with more than one leg);
  - an earlier leg `NN.K` that is not the last: refuse unless `-y`, printing
    "later legs already changed the trees" (the revive docstring's warning); with
    `-y`: revive with `--any-leg`, then the same line with that `NN.K`.
- The base: `arena-round/NN` when that ref exists, else the `base_sha` from `state.json`
  (a round started by the old command), passed as `--base <sha>`.
- The child's exit code is mapped as in AR-3 (`2` → `4` only when `state.json` was
  written after the child started, else `1`).

**Tests.** live round → exit 2, `state.json` unchanged; `--failed --dry-run` → no
write; `--agent x` revives one and leaves the others; `--agent` on a READY agent →
refusal naming `READY`; `--failed` leaves a `DEAD` agent as it is, `--failed --dead`
revives it (`revived_from.state == "DEAD"`); `--agent x` on a `DEAD` agent without
`--dead` → refusal naming the flag; `scripts/revive_round.py 141 --dead --dry-run`
lists the `DEAD` agents; earlier leg without `-y` →
refusal; the run line carries `--resume`; for a legs round (last leg) it carries
`--out <out_dir>/NN.K --legs 1` even when the profile says `legs = 3`, and the
roster's own `out_dir` when it is not `contest-out`;
`state.before-revive.json` written (the script's own behaviour, asserted once);
`scripts/revive_round.py 112 --dry-run` prints what it printed before the extraction
(`tests/test_kilo_contest_runbook.py` stays green).

---

### AR-6 — computed ticket state, `arena issue list` / `arena issue view NN`

**Size:** S · **Depends on:** AR-3 (its liveness helper, §4.6) · **Files:** `tools/arena/tickets.py`,
`tools/arena/cli.py`, `tests/test_arena_issue_list.py`

**Why.** "Which tickets are open, running, landed" is answered today by reading
md files, `contest-out/` and `git log` by hand.

**What to build.**

- `tickets.scan(repo, branch) -> list[Ticket]` over `.arena/drafts/` (`*.rejected.md` skipped),
  `epic-tasks/` (checkout) and `epic-tasks/` on the integration branch
  (`git ls-tree`), one entry per number (drafts lose to `epic-tasks/` when both
  exist — and that is flagged).
- `Ticket(number, title, path, where, status_field, state, flags)`; `state` per
  §4.4; `title` from the first `# ` line (reuse `draft.title_of`).
- Flags (`!` column): commit `NN:` on the integration branch but status not
  `landed`; status `landed` but no such commit; draft and epic-tasks file both
  present; two files for one number.
- `issue list [--state S]`: `NN STATE TITLE !`, numeric order; `issue view NN`:
  the md text, then one line per flag.
- Uses `cli._status_of` for `**Status:**` (import, not copy).

**Tests.** one ticket per state (draft, open, queued, running with a fake live pid
via the lock file, done, landed, closed); each mismatch flag; `--state open`
filters; `view` of a missing number → exit 1.

---

### AR-7 — `arena issue create "brief"`: the writer, no branch, no commit

**Size:** S · **Depends on:** AR-6 · **Files:** `tools/arena/tickets.py`,
`tools/arena/cli.py`, `tools/contest/cli.py` (the one extraction below),
`tools/contest/draft.py` (the one keyword below), `tests/test_arena_issue_create.py`

**Why.** `contest draft` checks out and commits on `contest-legs`, and that branch
can be stale (ticket 135). `draft.draft_ticket` already supports `commit=False` —
the branch logic is not needed at all.

**What to build.**

- Number: §4.5 (max + 1 over every source), or `--number NN` (refused if taken).
- Call `draft.draft_ticket(brief, repo=repo, config=config, round_no=NN,
  out_dir=<repo>/.arena/drafts, llm_call=…, review_call=…, commit=False)` —
  `out_dir` is **not** today's `out` but a new keyword this ticket adds (below),
  building `config`, `llm_call` and `review_call` exactly as `cli.cmd_draft` does
  (same refusals: no draft profile, no gate profile without `--no-review`, writer
  model == reviewer model). Extract that preparation from `cmd_draft` into one
  function `cli.draft_callables(config, no_review) -> (llm_call, review_call)` and
  call it from both — **the first allowed `tools/contest/cli.py` edit of this ticket**,
  behaviour of `cmd_draft` unchanged (its tests must stay green untouched).
- **`draft.draft_ticket(..., out_dir=None)`** — the second allowed edit, one keyword:
  the folder the default file name `<NN>-<slug>.md` (and a refused draft's
  `.rejected.md`) is written into instead of `epic-tasks/`; `out`, when given, still
  wins; `commit=True` behaves as today. Why not the route "call with `write=False` and
  write the text yourself": the slug comes from the ticket's own title, known only
  after the model answered, so `out` cannot be passed in advance; and with
  `write=False` a refused draft writes no `.rejected.md` at all and the text is not
  returned. `lint_ticket`'s "round is taken" check keeps reading `epic-tasks/`;
  uniqueness against drafts and `contest-out/` is §4.5's job.
- Collect: `draft_ticket` runs Pass A itself; nothing to add in the MVP.
- Output: `ticket NN drafted: .arena/drafts/NN-slug.md`, then
  `next: arena run start NN`. A lint/review refusal prints the problems and the
  `.rejected.md` path (it sits in `.arena/drafts/` and every scan skips it, §4.5 and
  AR-6) and exits 2. Every refusal is `2` in arena (AR-1's table), including the ones
  `cmd_draft` itself returns `1` for (no profile, same model): they happen before any
  model call.
- `.arena/drafts/` is created by `draft_ticket`'s own `_write`.

**Tests.** (the LLM is a fake callable everywhere)

1. Draft lands in `.arena/drafts/NN-<slug>.md`; `git status --porcelain` in the
   repo shows **nothing under `epic-tasks/`**; no branch `contest-legs` exists; no
   new commit.
2. Number = max + 1 when `contest-out/40/` exists and the highest md is 12 → 41.
3. `--number 12` when 12 exists → exit 2.
4. Lint refusal → `.arena/drafts/NN-<slug>.rejected.md`, exit 2, no draft file; the next
   `issue create` is offered the same NN again.
5. Same model for writer and reviewer → exit 2, no model called.
6. `tests/test_contest_draft*.py` unchanged and green; one new `draft_ticket(out_dir=…)`
   test: the file and a `.rejected.md` land there and `epic-tasks/` is untouched.

---

### AR-8 — `arena entry merge NN AGENT` and `arena issue land NN`

**Size:** M · **Depends on:** AR-3, AR-6 · **Files:** `tools/arena/merge.py`,
`tools/arena/cli.py`, `tests/test_arena_entry_merge.py`, `tests/test_arena_issue_land.py`

**Why.** Landing is the most error-prone hand step: the author (a patch made on
another machine carries that machine's address), the model trailer, explicit
paths, the ticket's status and the bench in the final commit. §4.3 is the rule;
this ticket makes it two commands.

**What to build — `entry merge NN AGENT [--uncommitted] [--squash]` (commit 1, §4.3).**

- Inputs: the round's out folder (`rounds.round_folder`, last leg) and the
  `entrants.json` there: `{"base": sha, "entrants": {agent: {"source": <path from the
  repo root>, "state"?: S} | {"duplicate_of": agent}}}` — `state` only for an entry
  whose file name carries one (`GAVE_UP`, `STALLED`, `ERROR`); a READY entry has
  `source` alone (`export.write_entrants`). A `duplicate_of` entry has no
  `source`: it is followed to the agent it names (the same patch text); the commit is
  still the asked-for agent's, with its own trailer.
- Refusals, before anything is touched: round alive (§4.6); no such agent; the current
  branch is not the one `.arena/rounds/NN.json` records (**the operator checks it out;
  arena never switches** — a round started by the old command has no record: then the
  profile's `branch`, else no branch check); the working tree has any tracked change
  (`git diff --quiet` and `git diff --cached --quiet`). A warning, not a refusal: the
  integration tip is no longer the round's base (`git rev-list --count
  <base_sha>..HEAD`, printed).
- The entry, by the exporter's own file names (`cli.export_patches`):

  | File | What it holds | Taken |
  |---|---|---|
  | `<agent>.patch` | `format-patch` output of a READY agent | yes |
  | `<agent>.<STATE>.patch` (`GAVE_UP`, `STALLED`, `ERROR`) | `format-patch` output of an agent that has a commit but did not end READY | refused unless `-y`, the state named |
  | `<agent>.<STATE>.diff` (`STALLED`, `ERROR`) | `git diff <base>` of an uncommitted tree; tracked files only, the untracked ones are only named in a trailing comment | refused unless `--uncommitted`; the untracked names are printed as "not in the diff — copy them by hand" |

- The patch:
  - `git apply --index --exclude='epic-tasks/*' --exclude='runs/*' <file>` — staged,
    not committed (`git apply` skips a `format-patch` file's mail headers). **No
    `--3way`:** on a conflict it leaves conflict markers in the tree and unmerged index
    entries (checked in a scratch repo), which breaks "tree unchanged"; plain `--index`
    applies all or nothing;
  - before applying, print the dropped paths: those of `git apply --numstat -z` that the
    excludes match;
  - a patch that does not apply: print git's error, exit 1, tree and index unchanged,
    with the hint "the integration branch moved since the round — apply by hand with
    `git am -3 <file>`";
  - the paths the commit takes are `git diff --cached --name-only -z --no-renames`
    after the apply (`--numstat` names only the destination of a rename, so a source's
    deletion would be left out).
- The message comes from `git mailsplit` + `git mailinfo` on the file (they decode a
  wrapped or RFC 2047 `Subject:` and drop `[PATCH n/m]`; hand parsing would not). A file
  with more than one message is refused, naming the count, unless `--squash`: then one
  commit, the first message's subject and body plus the line `(squashed from N commits)`.
- The commit: `git commit -F <msg> -- <the paths above>`:
  - subject: the patch's subject, with `NN: ` prepended unless it already starts with
    `NN:` or `AR-<n>:`; an uncommitted entry: `NN: <agent>'s uncommitted tree, taken
    as-is`;
  - body: the patch's message body as-is;
  - trailer: `Co-Authored-By: <model> <<model>@round-NN.contest>` — `<model>` as in §4.3,
    from `agents[].agent.model_id` in the last leg's `state.json` (the agent name when
    missing);
  - author: the checkout's identity — `git commit` without `--author`.
- Record `.arena/merged.json` (`NN` → agent, sha, subject).
- Print `merged <sha7> — fix it now if needed, then: arena issue land NN`.

**What to build — `issue land NN [-m SUBJECT] [--score TEXT] [--note TEXT] [-- PATH…]` (commit 2, §4.3).**

- The ticket: from `arena-round/NN` (`git show arena-round/NN:epic-tasks/<n>`), else
  `epic-tasks/` / `.arena/drafts/`; only its `**Status:**` line is rewritten, to the form
  of §4.3 (`landed — round NN, winner <agent> (<--score>), <sha7> as-is` or `+ follow-up`
  when paths follow `--`), and its `INDEX.md` row's status
  word when the row exists (via `scripts/ticket_status.py`'s `STATUS_RE` / `INDEX_ROW`);
  written to `epic-tasks/<n>`.
- The bench: `contest-bench/NN/` when present, added with `git add -- contest-bench/NN`
  (so `.gitignore`d files such as `__pycache__` stay out).
- Extra paths after `--`: the operator's follow-up edits. Each must exist and be changed
  or new; a path with no change → refusal.
- Refusal when any tracked change exists that is **not** in that list (so nothing
  is left out by accident, and nothing is swept in).
- Subject: `-m`, else `NN: ticket landed, and contest-bench/NN holds the round's
  acceptance bench` (without the bench clause when there is no bench). Body:
  `Round NN: <READY>/<TOTAL> READY; winner <agent> taken as-is in <sha7>.` — the winner
  and sha from the last `entry merge NN` (recorded in `.arena/merged.json`); when none is
  recorded, the line is left out — then `--note TEXT` as given (the bench score, the test
  totals: the landed commits of rounds 131 and 132 carry them). Trailer: the profile's
  `trailer`, if set.
- `git add -- <ticket> <INDEX row file> <bench files> <extra paths>`, then
  `git commit -F <msg> -- <the same paths>`.
- Then print, do not run:

  ```
  next: python3 -m pytest tests -n 8 -q
  then: python3 -m pytest tests_bugfix -n 8 -q
  push: git push <remote> <branch>
  ```

  `<remote>` is `git config branch.<branch>.remote`, else `origin`.

**Tests.**

1. `entry merge`: a format-patch whose `From:` is another address lands as **one**
   commit; author = the repo's `user.email`; the trailer line exact; the subject
   gets `NN: ` when missing and is unchanged when present; a wrapped / non-ASCII
   `Subject:` comes out whole.
2. A patch touching `epic-tasks/NN-x.md` and `runs/a/PROGRESS.csv` → those paths
   are not in the commit and are printed as dropped.
3. A non-applying patch (one that would conflict under `--3way`) → exit 1,
   `git status --porcelain` identical, no conflict markers, no commit.
4. A `.STALLED.diff` without `--uncommitted` → exit 2; with it → one commit and the
   untracked names printed. A `.STALLED.patch` without `-y` → exit 2; with it → one commit.
5. Alive round / dirty tree / wrong branch → exit 2, nothing staged.
6. `issue land --score "19/19 on contest-bench/NN"`: one commit with the ticket
   (`**Status:** landed — round NN, winner <agent> (19/19 on contest-bench/NN), <sha7> as-is`,
   every other line byte-identical; without `--score` no parenthesis), the INDEX row's word when a row exists, and
   `contest-bench/NN/*` — new, untracked files that were staged by the `git add` step;
   the body names the merged winner.
7. `issue land NN -- tools/x.py` with `tools/x.py` changed → in the same commit, and the
   status line ends `<sha7> + follow-up`; another changed tracked file not listed → exit 2.
8. Neither commit contains `arena-round/NN`'s ticket commit (`git log <branch>` has
   no `NN: ticket for the round`), and no `contest-legs` exists.
9. A patch with a rename: the source's deletion and the destination are both in the
   commit.
10. Trailer id: a model `x.y-z:free` gives `x-y-z`, `GLM-4.7` gives `glm-4-7`; the agent's
    `-var2` is not in it; for every model in the test it equals
    `cli.agents_from_models(<model_id>)[0].name`.
11. A `duplicate_of` entrant resolves to the named agent's patch.
12. A patch file with two messages → refused naming `2`; with `--squash` → one commit.

**Rules.** Never `git push`. Never `git commit -a`. Never touch `agents_128k.ini`.

---

## 7. After the MVP (backlog)

From here on every ticket is **drafted with `arena issue create`, run with
`arena run start`, landed with `arena entry merge`**. Each line below becomes a full
ticket when its turn comes. Sizes: **S** = one function + tests, one round;
**M** = one command with git or process side effects. Anything that grows past M
while drafting is split before it runs. A ticket runs only after everything in its
**Depends** column has landed; the phases group tickets by topic, and an `AR-N` id is
a label, not the run order (AR-21 sits in Phase 6, AR-46 in Phase 7, for that reason).

### Phase 2 — repos and collect

| # | Ticket | Size | Depends |
|---|---|---|---|
| AR-9  | `repo add NAME PATH [--test CMD] [--branch B]`, `repo list`, `repo set-default`; `-R NAME` global flag; registry in `contest.local.ini` `[arena.repo.NAME]` | S | AR-1 |
| AR-10 | Collect freshness: HEAD + dirty tracked files vs the `.collect/` stamp; auto-refresh (Pass A) before any command that reads the maps; **fail-closed** (a collect that raises is an error, not "no maps"); `--no-refresh` global flag skips it with a warning | S | AR-9 |
| AR-11 | `repo refresh [--deep]` (manual, forced; `--deep` = Pass B with the model), `repo status` | S | AR-10 |
| AR-12 | Wire AR-10 into `issue create` (instead of `draft_ticket`'s fail-open collect) and into `run start` | S | AR-10 |

### Phase 3 — tickets and config

| # | Ticket | Size | Depends |
|---|---|---|---|
| AR-13 | `config view/get/set` (masked) — `profile set` already landed as AR-61 (round 142); `config set` writes through the same `models._with_key` / `write_profile_keys`, no second ini writer | S | AR-2, AR-61 |
| AR-14 | `issue edit` ($EDITOR), `issue close --reason`, `reopen`, `queue` — the only writers of `**Status:**` besides `issue land`; adds `closed` to `cli.PARKED` and `scripts/next_task.py` `SKIP_STATUS` (§4.4) | S | AR-6 |
| AR-15 | `issue create --blank "title"` (template with every `draft.HEADER_FIELDS` line — Status, Severity, File, Symbol, Round, Size, Also touches — because lint and `intake` refuse a ticket without them; no LLM), `--after NN` (`**After:**` field), `--epic NAME` (`**Epic:**` field) | S | AR-7 |
| AR-16 | `arena init`: `contest.local.ini` from a template with placeholders, an empty `default` profile, `.arena/` — models are not chosen here: `init` only points at `arena model available` / `model use -p default` (AR-59) | S | AR-2, AR-59 |
| AR-17 | `issue lint NN`: `draft.lint_ticket` on demand, against a fresh collect | S | AR-10 |
| AR-18 | `issue review NN`: the reviewer alone on an edited draft | S | AR-7 |

### Phase 4 — validation before a round (`run start` calls all of it)

| # | Ticket | Size | Depends |
|---|---|---|---|
| AR-19 | `config validate` V1–V3: no agent name twice (exists), **no `provider/model` pair in two agent sections of the ini**, every name in the round's model list exists in the roster or carries `provider/`. A repeat **in the round's list** is allowed — it is how `-var1`, `-var2` are made (`cli.agents_from_models`) — and is printed as info `hy3 ×2 → hy3-var1, hy3-var2` | S | AR-2 |
| AR-20 | `config validate` V4–V7: **a judge model (gate, reviewer, writer) is never in the round** — always an error, no override; writer ≠ reviewer; no real provider/model name in a committed `[arena.profile.*]` section and no `api_key` that is not a `${ENV}` reference anywhere in the committed `contest.ini` (the roster sections there already name real `kenary/…` models and are not touched); profile keys point at existing sections | S | AR-19 |
| AR-22 | `bench create NN`: `contest-bench/NN/` + a template `test_bench_NN.py` (self-contained header like `contest-bench/132`) | S | AR-1 |
| AR-23 | `bench check NN`: run the bench on the base — it must have at least one **failing** test (else the bug is not there) | S | AR-22 |
| AR-24 | `bench run NN [--tree PATH]` | S | AR-22 |
| AR-25 | `base check`: `tests` then `tests_bugfix` (sequential), `sync_test_tiers --check`, `check_test_clocks`; cached per base sha in `.arena/base-check.json` | S | AR-1 |
| AR-26 | `doctor` part 1: kilo binary (through `models.find_kilo_bin`, the one lookup arena already uses — not a second one) + version, provider auth (`kilo auth list`, never printing keys), free port, orphan `kilo serve` processes | S | AR-1 |
| AR-27 | `doctor` part 2: `vm.max_map_count`, `ulimit -n`, free disk for `rounds_dir`, provider quota probe | S | AR-26 |
| AR-28 | `**After:** NN` respected: `run start` refuses until NN is `landed` | S | AR-15 |
| AR-29 | Overlap warning: a ticket whose `**File:**`/`**Also touches:**` intersects a running round's | S | AR-6 |
| AR-30 | Overlap by collect symbols (refines AR-29: same file, different symbols = no warning) | S | AR-29, AR-10 |
| AR-31 | `issue validate NN` = lint → review → bench check → `run plan`; `run start` runs lint + config validate + bench check + base check (cached) + doctor, refuses on error; `--skip-validate` | S | AR-17…AR-20, AR-22…AR-27 |

### Phase 5 — round, more views

| # | Ticket | Size | Depends |
|---|---|---|---|
| AR-32 | `run plan NN` (= `run --dry-run` on `arena-round/NN`), `run prompt NN [AGENT]` (the first prompt) | S | AR-3 |
| AR-33 | One lock per repo: a second `run start` on the same repo refuses (separate from AR-3's per-round pid) | S | AR-3 |
| AR-34 | `run view` columns: tokens, context %, the tests-queue time | S | AR-4 |
| AR-35 | `run watch NN` (re-render every N s), `run log NN [AGENT] [-f]` | S | AR-4 |
| AR-36 | `run wait NN` (blocks until not alive; exit 0 if ≥1 READY, else 4 — AR-1's code for a finished round with no READY) and `on_finish = <command>` in the profile (a new `profile.KNOWN_KEYS` entry) | S | AR-3 |
| AR-37 | `run gate NN [--agent A] [--rejects] [-f]`: per-agent allow/reject/LLM-calls off `decisions.jsonl` | S | AR-4 |
| AR-38 | `run resume NN` (plain `--resume`, no revive), `run cancel NN` (SIGINT to the runner, then wait) | S | AR-5 |

### Phase 6 — models and judges

| # | Ticket | Size | Depends |
|---|---|---|---|
| AR-39 | ~~`model find` / `model list`~~ — covered by AR-59 `model available` (LAST-TEST column); left: nothing unless a roster-only listing is wanted | S | AR-1 |
| AR-40 | ~~`model test code`~~ — landed as AR-60 `model test` (imports `py_model_test.py`; its `run_one` is a closure inside `main`, so AR-60 runs the task through the module's checker, not `run_one` — keep it so, don't extract it in another ticket); scores in `.arena/model-scores.json` | S | AR-60 |
| AR-41 | `model test tools` part 1, T1–T4 on `kilo serve` in a temp worktree: first token < N s, `read` called, `edit` with valid args, `bash` → pytest and the output read | M | AR-40 |
| AR-42 | `model test tools` part 2, T5–T7: answers a rejected permission instead of hanging, makes a commit and ends the turn, no fake tool call in plain text | S | AR-41 |
| AR-43 | The judge exam's data: ~30 permission requests with known answers (safe-in-worktree, dangerous, borderline per ticket rules, prompt injection) | S | — |
| AR-44 | `model test judge MODEL`: J1 valid JSON, J2 zero allow on dangerous, J3 ≥ 90 % allow on safe, J4 injection refused, J5 under `gate_timeout`; V8: `run start` refuses a gate model without a passed exam | S | AR-43 |
| AR-45 | `judge list` (writer / reviewer / gate / scorer and their models, masked), `judge test gate|reviewer` | S | AR-44 |
| AR-21 | Warnings W1–W3: one model under two providers (normalised id, warning only); no tools/code test on record; tools score below the profile's `min_tools_score` (a new `profile.KNOWN_KEYS` entry) — sits in Phase 6 because W2–W3 read the test results; `run start` (AR-31) prints these warnings from the moment it lands | S | AR-19, AR-40, AR-41 |

### Phase 7 — judging and landing

| # | Ticket | Size | Depends |
|---|---|---|---|
| AR-47 | `entry list NN` (agent, state, files, gate rejects), `entry diff NN AGENT` | S | AR-4 |
| AR-48 | `entry score NN`: copy `contest-bench/NN` into each entry's worktree, run it, write `contest-out/NN/score.json`, `BENCH` column | S | AR-24 |
| AR-49 | `entry score NN --on-base` / `--quick` (= `scripts/judge_round.sh`) | S | AR-48 |
| AR-50 | `entry analyze NN`: the table + a recommendation by the reviewer model; it never chooses | S | AR-48 |
| AR-46 | `model stats` (in Phase 7: it reads `score.json` from AR-48): per model over every `contest-out/*/state.json` + `score.json` — rounds, READY, wins, STALLED, average bench | S | AR-48 |
| AR-51 | `check [--stress N]`: `tests` then `tests_bugfix`, timing vs the base-check timing, a warning past +X % | S | AR-25 |
| AR-52 | `entry merge` runs `check` when asked (`--check`), still as a separate step after the commit | S | AR-8, AR-51 |
| AR-53 | `entry fix NN AGENT [--model M]`: a one-agent round on the merged head with the failing tests as the prompt; result = a separate follow-up commit | M | AR-8, AR-3 |
| AR-54 | `run report NN` (markdown for the commit message / notes), `run clean NN` (worktrees, `contest/NN/*` branches, `arena-round/NN`; only `landed`/`closed`; shows the list, asks unless `-y`) | S | AR-8 |

### Phase 8 — ticket sources, epics, docs

| # | Ticket | Size | Depends |
|---|---|---|---|
| AR-55 | `issue suggest`: `main.py --dry-run` on the repo → candidates (list only; known to be mostly false positives, never auto-ticketed) | S | AR-9 |
| AR-56 | `issue create --from-suggest K` / `--from FILE` (one ticket per confirmed finding, y/N each) / `--from-run NN AGENT` (follow-up) | M | AR-55, AR-7 |
| AR-57 | `**Epic:**` field, `epic list / view NAME / next NAME`, a generated `INDEX.md` | S | AR-15 |
| AR-58 | `docs/arena/RUNBOOK.md`, `--help` examples on every verb, the old runbook's map points to it | S | — |

### Phase 9 — from the first `arena run start` (round 139, 2026-10-02)

Found while running round 139 through arena for the first time. AR-61 + AR-62 are one ticket: `epic-tasks/142-ar-61-arena-profile-set.md` (round 142). Not committed; the operator picks the order.

| # | Ticket | Size | Depends |
|---|---|---|---|
| AR-61 | `profile set NAME KEY=VALUE` for the keys a round really needs — `max_parallel`, `fresh`, `variant` as real `profile.KNOWN_KEYS` (each becomes its runner flag), so the long run line is `arena -p P run start NN` with no hand-edited `extra =`; writes through AR-59's `write_models` / `_with_models` text editing, not a second ini writer (a slice of AR-13) | S | AR-2, AR-59 |
| AR-62 | `run start`: one runner flag never reaches the runner twice — a flag given both in the profile (`extra` or a key) and after `--` keeps the `--` one (the last word), and the printed command shows it once (round 139 printed `--max-parallel 8 --max-parallel 8`) | S | AR-3 |

**Totals:** 58 tickets (+ AR-59/AR-60 landed, AR-61…AR-62 from round 139) — MVP 8 (6 S, 2 M); backlog 50 (47 S, 3 M). No L.

---

## 8. Out of scope (on purpose)

- **Several machines** (remote hosts, `--host`, shared numbering, shared quotas).
  Later, a separate epic. Until then the second machine runs its own `arena` on its
  own clone and results come over by `git fetch` + `arena entry merge` — which
  already resets the author, so another machine's address never lands.
- Picking the winner automatically, pushing, merging into `main`.
- `harvest_report.py`, `merge_validations.py`, `truth_consensus.py` (the reviewer-CSV
  flow), `split_epic_tickets.py`, `trace_round_snapshot.py` (collect epics).
- `main.py --validate-plan` (the Architect's plan, not contest tickets).

## 9. How the epic itself is run

1. Rounds 133 and 134 judged and landed, then 135; `kc` merged into `main` by the
   operator.
2. Branch `arena` from that `main`.
3. **Numbers.** The runner only knows numbered tickets (`epic-tasks/NN-*.md`,
   `run --ticket NN`). Each AR ticket therefore gets the next free **round
   number** NN (136, 137, …); `AR-N` is its title id (`# NN — AR-N: …`), the bench
   is `contest-bench/NN/`, the commit subjects start with `NN:` like every round.
4. AR-1 … AR-8 run **the old way**, one round each, in the dependency order of §6.
   `contest draft` cannot write them: `cmd_draft` always passes `commit=True`, so it
   checks out and commits on `contest-legs` — the very behaviour this epic removes. Each
   ticket is written by hand (the runbook's "hand-written tickets"; every
   `draft.HEADER_FIELDS` line, and `**File:**` / `**Symbol:**` above all, because
   `intake` refuses a ticket without them) or drafted once into a scratch checkout,
   checked by hand and copied over. The round branch is cut from `arena` with
   `scripts/ticket_status.py round NN --base arena` (see its docstring: it commits the
   ticket as `open` on its own branch), and the round is run from that branch, checked
   out, with `run --ticket NN` — until AR-3 lands the old `run` needs the ticket in the
   checkout, as for rounds 131 and 132. Never leave the ticket on `contest-legs`.
5. From AR-9 on: `arena issue create` → `arena run start` → judge →
   `arena entry merge` → `arena issue land`. Every bug found in `arena` while
   using it is fixed at once in a separate commit (or a ticket, if larger than a
   few lines).

## 10. Checks (re-run against `kc` @ `9f99300`; the epic quoted `f193004`, which is not in the repository the review cloned)

- `draft.draft_ticket(..., commit=False)` exists — AR-7 needs no branch logic.
- `draft.next_round` returns the **first gap** from 1, not max + 1 — AR-7 must not
  use it as is (§4.5).
- `cli.intake` reads every `**Status:**` and the lower-numbered tickets **at the base**
  (`_ticket_body(..., at=base)`) and requires `**Status:** open`, no lower-numbered ticket
  still on offer, and `**File:**` / `**Symbol:**` (KC-7). It finds the ticket **itself in
  the checkout** (`gates.ticket_for_round`, `ticket_path.is_file()`) — AR-3 step 0 (§4.2).
- `workspace._check_base_and_epic_tasks` refuses **untracked** files under
  `epic-tasks/` in the checkout → drafts go to `.arena/drafts/` (§4.1). Note: the
  untracked `epic-tasks/135-*.md` that blocks the next `run` on the author's checkout is
  not in the review's clone (no ticket above 132 exists on any remote branch).
- `run` takes `--base` (default `HEAD`), `--legs`, `--resume`, `--out`, `--fresh`,
  `--dry-run`; `status` takes `--ticket`, `--out`, `--roster`.
- `roster.load_roster` ignores unknown sections → `[arena.*]` in
  `contest.local.ini` does not disturb the runner.
- `state.json` has `round_no, ticket, base_sha, started_at, agents[]` and **no pid**
  → liveness by `/proc` + a lock file (§4.6).
- A READY patch is `format-patch` output whose `From:` is the machine it was made
  on (e.g. another host's address) → AR-8 must not keep the patch author.
- `entrants.json` is `{"base", "entrants": {agent: {"source", "state"?} | {"duplicate_of"}}}`;
  `<agent>.patch` is READY, `<agent>.<STATE>.patch` a committed non-READY entry,
  `<agent>.<STATE>.diff` an uncommitted tree (`export_patches`).
- `revive_round.py` resolves a bare number to the last leg and refuses an earlier
  leg without `--any-leg`; it has no per-agent mode and no function that revives — the loop is inline in `main`
  (AR-5 extracts one and adds `--agent`).
- `cli.agents_from_models` makes `-varN` from a repeated name in `--models`
  (a feature, not a duplicate).

Found by the review and fixed above:

- `intake` needs the ticket file in the checkout, which the design forbids (§4.2, AR-3
  step 0, test 6).
- `git commit -- <path>` refuses an untracked path: `git add -- <path>` first (checked in
  a scratch repo; principle 8, AR-8).
- `git apply --3way` leaves conflict markers and unmerged entries on a conflict (checked):
  AR-8 applies without it.
- `export_patches` names: `.<STATE>.patch` is committed work, `.<STATE>.diff` the
  uncommitted tree, and the `.diff` does not inline untracked files (AR-8's table).
- `cli._round_out_dir` / `_leg_out_dir` / `workspace` zero-pad the round to two digits
  (`contest-out/07`, `07.2`, `contest/07/<agent>`): §4.8.
- `draft_ticket(write=False)` writes no `.rejected.md` and returns no text: AR-7 uses
  `out_dir`.
- `scripts/ticket_status.py` already flips `**Status:**` and the INDEX row; landed
  tickets carry `landed — round NN, winner …, <sha> as-is` (§4.3, AR-8).
- The trailer id on `kc` is the model name squeezed to `[a-z0-9_-]`, without `-varN`
  (§4.3).
- `tokens` is a usage count: AR-1's `mask` keys on whole words.
- The roster's `ConfigParser` shape (`_new_parser`) is needed to read the §4.7 example
  (AR-2).
- The old `run` exits `2` when no agent is READY: AR-1 / AR-3 map it to `4`.
- Dependencies: AR-4 and AR-6 depend on AR-3 (liveness, round-folder resolver), §6.
- `closed` is not a parked status for `intake` / `next_task.py` (§4.4, AR-14).

Corrected after the review (checked on `kc` @ `9f99300`):

- The old `run` exits `2` on an argparse error too (a mistyped flag after `--` exits
  `2`, checked): a child's `2` is `4` only when the round's `state.json` was written,
  else `1` (AR-1, AR-3 step 5, AR-5).
- The landed status line's parenthesis is the bench score (`28/28 on contest-bench/131`,
  `19/19 on contest-bench/132`), not READY/TOTAL, and 131 ends `+ follow-up`, not
  `as-is`: `issue land --score`, and `+ follow-up` when paths follow `--` (§4.3, AR-8).
- The trailer id also lower-cases and strips leading `_`/`-` (`agents_from_models`); the
  rule is spelled out in §4.3 and AR-8's test pins it to `agents_from_models` itself.
- AR-3 step 0's temporary ticket keeps its file name, because callers read `path.name`.
- `contest.ini` already names real models, so AR-2's and AR-20's "no real names" rules
  are scoped to the new `[arena.*]` blocks.
- AR-7's call names `out_dir=`, which is a new keyword the ticket adds; today's
  `draft_ticket` only has `out`.
- Two backlog tickets depended on later phases: AR-21 (Phase 4) on AR-41, and AR-46
  (Phase 6) on AR-48. AR-21 moved to Phase 6 and AR-46 to Phase 7, AR-31 no longer
  waits for AR-21, and §7 states that a ticket runs once its **Depends** have
  landed. The counts are unchanged.
- AR-5's rerun line used a literal `contest-out/NN.K`; it is `<out_dir>/NN.K` from the
  roster (§4.8). AR-8's `entrants.json` shape marks `state` optional: a READY entry
  has `source` alone. The §6 graph keeps AR-8 under AR-6 only: AR-8's AR-3 edge is
  implied through AR-6, and its header names both.

Not verifiable from the review's clone, to be re-checked on the real base before AR-1:
commit `f193004`; rounds 133-135 and the "ticket 134 bug" of the earlier §9; every
statement about `main` after `kc` is merged.
