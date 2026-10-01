# KC-42 — round 81 — results

Base `866784c2`. KC-9 (`classify_idle`, `CONTINUE_PROMPT`) and KC-39
(`_diff_signature`, `max_sessions_per_attempt`, the session-reset path) were both
landed before this round, so the escalation borrows KC-39's swap rather than
inventing one.

## What landed

- `ContestConfig.first_touch_sec` (default 420, `0` = off) and
  `ContestConfig.first_touch_nudges` (default 1), in `roster.py` and `contest.ini`.
- `_FirstTouch` in `tools/contest/runner.py`: one watch per turn, armed from the
  turn's prompt, polling the worktree on its own thread. It is disarmed for good
  the first time `_dirty_tree(ws)` is non-empty or `_commits_above(ws) > 0`.
- The nudge, sent from the watch's thread into a session that is *still working* —
  which is the whole point: round 64's worst slot was never idle, so nothing else
  could have said anything to it.
- The escalation, borrowed from KC-39: `abort` + `_replace_session(turn,
  reason="first_touch")` with the same provider and model, `run.attempt`
  unchanged, subject to the same `max_sessions_per_attempt`. No room for a second
  session (`= 0` or spent) means no escalation, not an unbudgeted one.
- `AgentState.DEAD`: terminal, never harvested, no patch and no `.diff`, and a
  `**Never started:** N agents` line of its own in `SUMMARY.md` so the count is
  not folded into the stalls.
- The nudge budget and the one reset are counted on `run` (per attempt, like
  `continues`), so a fresh session is not handed a second nudge — the sequence is
  nudge, reset, `DEAD`, and it does not depend on how many turns the reset cost.

The predicate is measured on the worktree and never on the event stream, because
round 92's `nex-n2-5-pro` emitted a `session.status` heartbeat every five minutes
from a provider stuck in an offline/retry loop, and any clock on events reads that
as an agent at work.

## Fail-open, everywhere

- `first_touch_sec` absent, `0`, negative, a `bool`, or not a number: off — the
  tree before this ticket, byte for byte. `roster` also rejects a malformed ini
  value at load time.
- A worktree that cannot be read (`TreeReadError`, FL-2) is "no news": the watch
  does not declare anything, and the turn ends where the ordinary edges take it.
- A nudge whose `POST` is refused is a warning, never a reason.
- A reset whose `POST /session` is refused is `ERROR` with `resumable`, exactly
  like KC-39's.
- The watch is stopped in the same `finally` as the wait, so a turn that ends on
  its own can never be nudged after the fact.

## Tests — `tests/test_contest_runner.py`

- `test_first_touch_nudge_reset_and_dead` — one nudge, one reset, `DEAD`, all on
  `turns.jsonl`, with the second session opened on the same model.
- `test_a_touch_before_the_deadline_never_nudges` — writes, `git checkout`s its
  own edit away, stays quiet past two more deadlines: no nudge, no reset.
- `test_a_touch_after_a_nudge_ends_the_escalation` — the nudge reaches the
  session, the model does the ticket, one session, `READY`.
- `test_a_dead_agent_is_not_harvested_and_never_holds_the_round` — two agents,
  one dead: no harvest verdict, no commit, no exported file, `DEAD` in the table
  and its own count in `SUMMARY.md`, the other agent `READY`.
- `test_first_touch_sec_zero_is_the_tree_before_the_ticket` — regression guard:
  `STALLED no idle after 3s`, one session, one prompt, no nudge.
- `test_first_touch_off_when_the_key_is_missing_or_malformed`,
  `test_a_tree_that_cannot_be_read_is_not_a_dead_end`,
  `test_no_room_for_a_second_session_goes_straight_to_dead`,
  `test_the_nudge_names_the_files_the_ticket_declares`.

Nine of the ten fail on the base with the change stashed; the tenth
(`test_first_touch_sec_zero_is_the_tree_before_the_ticket`) is the regression
guard and is *meant* to hold on the base.

## Replay — `replay64.py`

`python3 contest-bench/kc42/replay64.py contest-out/64` drives the runner's own
`_FirstTouch` over each agent's recorded `events.jsonl`, offline, at the
committed 420 s / 1 nudge / 2 sessions scaled 1000×. The worktrees of round 64
are gone, so "touched yet" is the stream's first `file.edited` event — Kilo
emits one per edit/write tool call — at its real offset from the first event.

Run by the judge against the round's own bytes (exit 0):

| agent | events | first `file.edited` | turn | nudge | reset | verdict |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| `nex-n2-5-pro` | 54 695 | never | 1357 s | 1 | 1 | **DEAD** |
| `laguna-s-2-1` | 29 363 | never | 760 s | 1 | 1 | **DEAD** |
| `agnes-3-0-flash` | 411 | never | 337 s | 1 | 1 | **DEAD** |
| `mimo-v2-5` | 7 899 | 147 s | 1420 s | 0 | 0 | alive |
| `glm-4-7-flash` | 7 598 | 205 s | 4000 s | 0 | 0 | alive (disarmed) |
| `agnes-2-5-flash` | 38 392 | 294 s | 1802 s | 0 | 0 | alive |
| `step-3-7-flash` | 22 297 | 322 s | 1801 s | 0 | 0 | alive |
| `hy3` | 29 362 | 850 s | 1802 s | **1** | 0 | alive |

Two findings the table carries:

- `agnes-3-0-flash` (337 s) and `laguna-s-2-1` (760 s) ended on a provider
  error before or soon after 420 s. The clock would have DEAD-ed them only had
  the turn gone on; what cost those slots was KC-35/KC-37, as the ticket says.
- `hy3` wrote its first file at 850 s, past the 420 s deadline: it would have
  been nudged once and then written — the "touch after a nudge" path, on real
  data. A 420 s default is tight for a slow-but-working model; one nudge is
  the price, not a reset.

The first version of this script (the round's entry) read `state.json` rows by
a `name` key the file does not have and fell back to `git status` in
`contest-out/64/<agent>` — not a worktree, so git answered for the enclosing
checkout, and every agent read as touched; its `read_events` looked for `time`
instead of `t` and made up reversed timestamps. Both were fixed by the judge.

## Live probe — `live_probe.py`

`python3 contest-bench/kc42/live_probe.py --models laguna-s-2-1:free,agnes-3-0-flash:free --first-touch 90`

A ticket whose *first* instruction is a `kilo` subcommand that does not exist,
asked for before the change — so a model that follows it literally cannot reach
`pkg/thing.py` at all, which is round 64's shape produced on purpose instead of
waited for. The run confirms by hand, in this order: the nudge reaches the
session (a second `prompt_async` into the same `session_id`), the reset produces
a **second** `session_id` in `state.json`, and a `DEAD` slot stops costing wall
time (the round's wall time tracks the first-touch budget, not
`turn_timeout_sec`).

**Not run here** — it needs the Kilo VS Code extension's `bin/kilo` and the
kenary provider signed in, and it costs real free-tier calls. That is why it is
in `contest-bench/` and not in `tests/`.
