# Mini-epic — what round 64 proved, and the six tickets that answer it

Written 2026-09-22. This is the overview; the tickets carry the detail and are
the thing to review. Nothing here starts until the current open set
(72–77 / KC-33…KC-38) is closed.

## The evidence

Round 64 (`contest-out/64/`, ticket `64-kc25-…`, base `9912b78`, eight agents,
2026-09-21) reported **zero harvested entries**. It was not a zero round.

How the eight ended their single turn: three `timeout` at
`turn_timeout_sec = 1800`, one `stalled`, two `error`, one reached harvest and
got `REWORK`, one `error` at 336 s. On 2026-09-22 the five non-empty worktrees
were squashed to one commit each, given a `PROGRESS.csv` row by hand, and
harvested sequentially with `run_tests=True`:

| agent | commit | verdict | four roots | files | ± | test funcs |
|---|---|---|---|---|---|---|
| `mimo-v2-5` | `24d3e8a` | **READY** | PASS / PASS / PASS / PASS | 5 | +403/−3 | 80 |
| `agnes-2-5-flash` | `f01e2c5` | **READY** | PASS / PASS / PASS / PASS | 4 | +273/−7 | 34 |
| `step-3-7-flash` | `f91efba` | REWORK | `tests:1✗` | 5 | +305/−6 | 71 |
| `hy3` | `cf160da` | REWORK | `tests:3✗` | 5 | +344/−7 | 73 |
| `glm-4-7-flash` | `86046dc` | REWORK | `tests:34✗` | 3 | +290/−10 | 42 |

All five: `shrink: same`, `off_ticket: 0`, `gate: ok`, one commit. Both READY
entries had independently written the same two symbols as the ideal patch that
had already landed as `215a740` — they were finished work that never got to
say so.

Three conclusions, none of them about model quality:

1. **The harvester only sees commits.** ~1300 lines of mostly-green code were
   invisible because four agents never reached `git commit`. → **KC-41**.
2. **The clock kills the working and spares the idle.** Three agents were cut at
   exactly 1800 s *while editing*; `glm-4-7-flash` sat flat for 15 minutes
   behind a live heartbeat and was never touched. → **KC-36** (already filed).
3. **A dead slot costs what a working one costs.** Three agents never opened a
   file. `nex-n2-5-pro` spent 54 695 events inside one failed `task` call —
   never silent, never idle, never dirty, so no existing edge sees it.
   → **KC-42**.

And the operator's standing workaround — at the third turn, open a fresh chat
on the same worktree and make the model continue from the files it already
changed — is the missing outer loop. → **KC-43**, **KC-44**.

## The tickets

| # | id | what it does | size | depends on |
|---|---|---|---|---|
| 80 | **KC-41** | a turn that ends with uncommitted work gets a deadline commit, a runner-written `PROGRESS.csv` row, and a real harvest | M | KC-21, KC-22, KC-31 |
| 81 | **KC-42** | first-touch deadline → nudge → session reset → `DEAD`, for the agent that never starts | M | KC-9, KC-39 |
| 82 | **KC-43** | numbered legs (`65.1`, `65.2`): same worktree and branch, fresh session each leg, a mechanically-derived leg record handed between them, scored once at the end | L | KC-36, 39, 40, 41, 42 |
| 83 | **KC-44** | the ticket's own `**Size:**` header picks the leg count; `L` is refused in one leg | S | KC-43 |

Already filed and unchanged by this pass, but part of the same picture:
**KC-9** (48, `classify_idle`), **KC-10** (49, context fill), **KC-31** (70,
now scoped to the clean-worktree case), **KC-36** (75, the clock),
**KC-39** (78, a repeating diff starts a fresh session), **KC-40** (79, the
model's own summary copied out before a compact).

```
KC-41 ──┐
KC-42 ──┤
KC-36 ──┼──> KC-43 ──> KC-44
KC-39 ──┤
KC-40 ──┘
```

KC-41 and KC-42 are independent of everything and of each other; either can
land first, and both are worth landing even if the relay is never built.

## The correction worth keeping in view

The idea this started from was: when an agent leaves no changed files, summarise
its *chat answers* and feed several such summaries into a new round.

The summarising instinct is right, the input is wrong. Text is what the model
**said**; what matters is what it **did**. A handoff that carries only prose
makes the next leg re-derive the code — exactly what the manual workaround
avoids by reusing the worktree. So a handoff is always a pair: the branch
itself, carried forward, plus a record whose fields are derived from git and
the logs, with model prose as one field among many (KC-43 §3, KC-40).

The one case where prose is nearly all there is — an agent that ended with an
empty worktree — is real, rare, and low-value: all three in round 64 failed for
infrastructural reasons. Those slots get reclaimed *during* the round (KC-42),
not summarised after it.

## Open questions for review

1. May a deadline commit **win** a round, or only qualify as an entry? KC-41
   files it as `deadline_commit: true` and leaves the judge's rule open.
   Evidence for allowing it: both round-64 READY entries were complete work.
2. Is a reclaimed slot closed or re-filled mid-round? KC-42 closes it —
   re-filling makes a round's cost and timing unreproducible.
3. Does a relay leg get the previous record only, or all of them? KC-43 says
   all, newest first, since they are short by construction.
4. Are relay legs always the same model? KC-43 says yes and puts cross-agent
   relay out of scope — it would destroy the comparability that makes a round a
   round.
5. The relay and the existing `rework` path are two answers to "try again" and
   probably should become one. Not in the ticket that introduces the second.

## Outside this mini-epic

`glm-4-7-flash`'s round-64 entry made the `tests` root take **2609 s** against
~260 s for every other entry in the same replay — a tenfold slowdown from its
own code, not the known KC-38 flake. No ticket: the code never landed, so there
is nothing to fix until it is reproduced against code that does.
