# KC-47 — round 91 — results

Base `58e20e5`. Twelve slots, ten with work (laguna-s-2-1 and nex-n2-5-pro left the
tree untouched). Every entry was captured as `git diff --binary 58e20e5` (committed and
uncommitted alike, `contest-out/91/diffs/`) and scored tree by tree with
`acceptance_kc47.py` (`run_all_kc47.sh`, one tree at a time):

- K1–K10 — the ticket's Acceptance list, `wait_idle` over a timed scripted stream on a fake clock;
- D1–D8 — Kilo's real `bash` default (7.6.2: `bashDefaultTimeoutMs ?? 120000`), the bound
  measured from the part's own `running` event, `pending` open, `error` closed, the command
  clipped at 120, a non-`bash` tool with a `timeout` input, `running_for`, round 86 mimo's race;
- R1–R3 — the runner end to end over the fake Kilo and with the backend's result stubbed;
- P1–P4 — `AGENT_TEST_TIMEOUT_MS`, the prompt sentence, the runbook, the committed `contest.ini`;
- D9–D10 (round 2) — "the largest of": an open short `bash` never cuts a live session earlier
  than KC-12, and with two open parts the widest bound applies.

The base passes 10 of 28 (the ones that say "unchanged").

| entry | state | score | misses |
|---|---|---|---|
| **sensenova-6-7-flash-lite-var1** | READY `9aafbf2` | **27/28** | D2 (bound runs from the last event, not the call's `running`) |
| mimo-v2-5 | READY `05c0889` | 27/28 | D1 (default 300 s) |
| sensenova-6-7-flash-lite-var2 | READY `2e91567` | 27/28 | D1 |
| step-3-7-flash | uncommitted | 27/28 | D1 |
| agnes-3-0-flash | uncommitted | 26/28 | D9, D10 — and ships no test |
| sensenova-6-8-flash-lite-var1 | READY `7a63649` | 26/28 | D1, D2 |
| agnes-2-5-flash | READY `9c6b2ed` | 25/28 | D1, D9, D10 |
| sensenova-6-8-flash-lite-var2 | READY `583eb87` | 25/28 | R1 (`no event for <elapsed>s`), D9, D10 (nearest deadline, not widest) |
| hy3 | uncommitted | 13/28 | runner, prompt, most of the bound |
| glm-4-7-flash | uncommitted | 1/28 | — |

sensenova-6-8-flash-lite-var2 and sensenova-6-7-flash-lite-var1 on their own base: both roots
ran with no failure in the progress output, tiers OK, `collect_bridge.py` untouched.

## Winner and the ideal

sensenova-6-7-flash-lite-var1. Of the four at 27 it is the only one with Kilo's real default
(120 000 ms, read off the build), and its miss is on the safe side: `_silence_bound` is the
*widest* of KC-12's window and each open call's, so opening a part never gives a turn less
room — the property sn68-var2 and both agnes entries break (D9). It also ships the most tests
(12, `wait_idle` and runner).

Changes on the way into `kc` (one commit, with KC-36's `on_deadline` in the same loop):

- `_silence_bound` → `_silence_left(silence, last_seen, open_parts, now)`: the latest of
  `last_seen + window` and each open call's `running + timeout + window` (D2 fixed; D9/D10 kept).
- An open call's `at` is its first `running` event; a later update of the same running call does
  not restart it (a `pending` → `running` does).
- KC-36's `quiet` reads the widened `silence_left`, so a `bash` that holds the silence open does
  not also block a churn extension at the turn deadline.
- `_open_tool_report` names the call with the widest bound — the one the turn was waiting on.
- The entry's `test_two_bash_parts_only_the_still_running_one_sets_the_bound` asserted the
  last-event reading; rewritten to the ticket's (3.5, `running_for` 2.4), and
  `test_the_calls_bound_runs_from_its_running_event_not_from_later_updates` added.
