# KC-41 replay — round 64, through the code

Ticket: `epic-tasks/80-kc41-a-turn-that-ends-with-uncommitted-work-gets-a-deadline-commit-and-is-harvested.md`.
Base of the replay: round 64's own, `9912b78`. Ticket of the replay: `64-kc25-...`.

Bench: `replay64.py` — the ticket's seventh Acceptance item, "Replay against real
data, required". It takes round 64's five abandoned worktrees and puts them
through KC-41's steps 1–4 (`_deadline_commit`, the progress row, `_harvest`)
instead of through the hand, and asserts the ticket's *Source* table comes back.

```bash
python3 contest-bench/kc41/replay64.py --rounds ../rounds --out contest-out/64
```

`--no-tests` skips the four roots (a smoke, not the ticket's item), `--only NAME`
does one worktree, `--table` prints and stops without asserting, `--timeout SEC`
bounds each worktree's roots. Default scratch is `/tmp/kilo/kc41-replay64`; the
roots are the same four `pytest` invocations `harvest` runs, one worktree at a
time.

## Not run here — and why

**This replay was not executed against round 64's real data.** The round-64
worktrees (`../rounds/64-*`) and `contest-out/64/` are not in this round's
worktree, and reaching outside the worktree for them is a reviewer decision this
agent cannot make. So the numbers in the table below are the ticket's own 2026-09-22
hand pass, quoted as the *expectation* — not a measurement this entry made. The
script is the deliverable; running it is the operator's step.

`RESULTS.md` §2 below is what the script was verified against: a synthetic
round-64-shaped fixture in `/tmp`, five agents, built and thrown away.

## 1. What the ticket's table says (2026-09-22, by hand)

The operator squashed the five non-empty worktrees to one commit each
(`git reset --soft <base> && git commit`), gave each a one-row
`runs/<agent>/PROGRESS.csv` by hand, and ran `harvest(ws, ticket, run_tests=True)`
on each, sequentially:

| agent | deadline commit | verdict | four roots | files | ± |
|---|---|---|---|---|---|
| `mimo-v2-5` | `24d3e8a` | **READY** | PASS / PASS / PASS / PASS | 5 | +403/−3 |
| `agnes-2-5-flash` | `f01e2c5` | **READY** | PASS / PASS / PASS / PASS | 4 | +273/−7 |
| `step-3-7-flash` | `f91efba` | REWORK | `tests:1✗` | 5 | +305/−6 |
| `hy3` | `cf160da` | REWORK | `tests:3✗` | 5 | +344/−7 |
| `glm-4-7-flash` | `86046dc` | REWORK | `tests:34✗` | 3 | +290/−10 |

All five: `shrink: same`, `off_ticket: 0`, `gate: ok`, one commit. That is the
round the operator never saw — the round reported **zero** harvested entries
while holding two that pass all four roots. `replay64.py` asserts exactly this
table, on `EXPECTED`: two `READY`, three `REWORK` each carrying `tests_failed`,
one commit each, `off_ticket: 0` and `shrink: same` throughout. The shas are
deliberately not compared — the replay's own deadline commits carry new ones,
which is the point.

## 2. What the script was verified against

A synthetic fixture, because the real round is not on disk here: a repo with a
base commit, two worktrees on `contest/64/<agent>` holding an uncommitted change
and an uncommitted test, and a `contest-out/64/state.json` naming the base and
the ticket. Run twice, with and without the four roots:

```
| agent | deadline commit | verdict | four roots | off_ticket | shrink | commits |
|---|---|---|---|---|---|---|
| mimo-v2-5 | `af1c629` | **READY** | tests:PASS tests_bugfix:absent .smoke_tests:absent .regression_tests:absent | 0 | same | 1 |
```

`READY`, one commit, `off_ticket: 0`, `shrink: same` — the shape the real replay
has to produce. The roots really ran; they are not stubbed.

The assertion path was exercised too: with two of the five agents on disk the
script reports `1 READY, the ticket's table has 2`, names each mismatching agent,
and exits 1. It does not pass a partial round.

## 3. The bug this fixture found

The first version of `replay64.py` rebuilt each worktree with
`shutil.copytree`. A worktree's `.git` is a *file* whose `gitdir:` points back at
the shared repository — so the copy was a second worktree of the same repo, and
the replay's deadline commit landed on round 64's own branch, mutating the
evidence it was scoring. The fixture's `mimo-v2-5` branch had a
`WIP (deadline commit, …)` commit on it afterwards.

It is now a worktree of a **clone** (`git clone --no-hardlinks` into the scratch
dir, then `git worktree add -B` off the source's branch), with the source's
uncommitted files copied in from `git status --porcelain -uall`. Verified: after
two full runs, both source worktrees still show 2 dirty files and `HEAD` at the
base, and each run is idempotent (the scratch worktree is removed and re-added
rather than reused).

This is worth recording as a property of the *data*, not just the script: any
replay that touches a round's worktrees has to clone. `contest-out/64/` on disk
is the round's own record and must be read, never written.
