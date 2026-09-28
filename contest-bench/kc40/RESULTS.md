# KC-40 — round 79 — results

Base `2a0132f`. Eight slots. Five came back READY: both sensenova-6-8/6-7
var1, sensenova-6-7-flash-lite-var2, agnes-2-5-flash and mimo-v2-5. Two were
STALLED at the 90-minute limit with a deadline commit: sensenova-6-8-flash-lite-var2
and agnes-2-0-flash. glm-4-7-flash was STALLED with no commit, so it has no entry.

`kilo serve` shut down cleanly at 01:32:46 (`disposing all instances`, no OOM
in the journal), and all eight turns ended `MessageAbortedError`. That was the
server going away, not the agents: every run was left `resumable`, and
`--resume` restarted all eight at 01:41:42. The agent clock restarts on resume. That is why sensenova-6-8-flash-lite-var2 and agnes-2-0-flash
both hit `time up` at the same second, 03:11:42.

Each tree was scored with `acceptance_kc40.py` (`run_all_kc40.sh <trees>
<tree>...`), through the public contract only:

- E1–E6: the edge.
  - A dirty tree past `summary_at_percent` is asked once.
  - A committed tree at the same fill is not asked.
  - A second crossing in the same attempt is not asked again.
  - At 85 % the session is only compacted.
  - `0` switches the edge off.
  - An unsized model is not asked.
- P1–P8: the prompt and the copy.
  - The ask is its own `prompt_async` in the same session, before `summarize`.
  - The reply goes to `<agent>.summary.md` under `out_dir` itself (P4), not in
    the agent's subfolder.
  - `run.summary` goes through `state.json`.
  - `summary_attempted` and `summary_captured` are written on the turn.
  - The prompt after the ask is one line, not the ticket again (§4).
  - SUMMARY shows the summary.
- F1–F6: the fallback and the carry.
  - An empty reply and a `session.error` both take KC-39's fresh session, with
    the abort first and the `git status` lines in the new opening prompt.
  - A captured summary reaches the next session's opening prompt.
  - The file accumulates across sessions and is appended to, never rewritten.
- C1–C2: `contest.ini` names the key above `compact_at_percent`, and the roster
  reads it.

| entry | state | score | misses |
| --- | --- | --- | --- |
| base | — | 0/22 | all |
| sensenova-6-8-flash-lite-var2 | STALLED | **22/22** | — |
| sensenova-6-7-flash-lite-var1 | READY | 21/22 | P7: after a compact the continue does not say the summary still applies |
| sensenova-6-8-flash-lite-var1 | READY | 21/22 | P8: no SUMMARY column |
| sensenova-6-7-flash-lite-var2 | READY | 20/22 | P4 (the file is in the agent's folder), F4 (the summary never reaches the new session) |
| mimo-v2-5 | READY | 19/22 | P7, P8, F4 |
| agnes-2-5-flash | READY | 17/22 | P4, P7, P8, F3 (a failed ask has no `summary_captured`), F4 |
| agnes-2-0-flash | STALLED | 13/22 | P2–P5, P7, P8, F3–F5 |
| glm-4-7-flash | STALLED | — | no commit |

Winner by score is sensenova-6-8-flash-lite-var2, and the ideal (`KC-40: …`) is
built on it. Its tree was a deadline commit, so the harvest scored it READY
(`off_ticket_files`) but the time-up stop kept it STALLED.

Changes made while landing it:

- **The ask armed the stop for a working turn.** The ask goes out past
  `compact_at_percent`, so a tool the model tried in its reply was refused for a
  full context (KC-69). That refusal armed the 0.5 s abort, and nothing
  cancelled it: it landed on the compact or on the continue after it. The gate
  now cancels it after the ask, and clears the refusal flag, so the next turn
  does not start forced.
- **The ask was skipped before a forced compact.** A compact forced by a
  refusal for a full context skipped the ask. Live, that is how most sessions
  reach their compact (round 79: every compact of sensenova-6-8-flash-lite-var2).
  The ask now comes first there too.
- **A stale mark after the ask.** An ask that needed no compact after it
  returned `""`, so the caller kept its old mark, and the ask's own
  `session.idle` could end the next prompt's wait (KC-63). It now returns
  `"summarized"`, and the caller takes a fresh mark.
- **A failed ask with no session to spare.** It no longer opens one past KC-39's
  `max_sessions_per_attempt` (`0` = no resets): the session is kept and
  compacted, as before KC-40.
- **The carried summary ran into the line above it.** The prompt now has a
  blank line before it.
- **The SUMMARY `summary` column is now the last column.** It no longer sits in
  front of `commit`, so every existing column keeps its place.
- **`live_probe.py` called `run_round(server=…)`.** That keyword was removed
  when `make_backend` came in, so the probe would have died with a `TypeError`.
  It now builds a `KiloBackend` per worktree.

Four tests cover these changes (`tests/test_contest_runner_summary.py`, the last
four). All four are red on the entry.

## Live probe — not run yet

The ticket's live half has not been run. It asks whether a real, nearly full
free-tier model can still write a truthful account of its own uncommitted diff:

```bash
python3 contest-bench/kc40/live_probe.py --models agnes-2-5-flash:free,glm-4-7-flash:free
```

For each model, read the summary and the diff side by side and record here:

- whether a summary was captured at all;
- whether it is truthful and useful, not only fluent;
- if the ask failed, what the fresh session did with the partial text.

A plumbing success with a useless or made-up summary does not close the ticket.
