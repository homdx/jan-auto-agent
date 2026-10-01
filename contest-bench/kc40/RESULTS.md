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

## Live probe

The ticket's live half asks whether a real, nearly full free-tier model can
still write a truthful account of its own uncommitted diff. Two runs, on
agnes-2-5-flash and glm-4-7-flash, both over `kilo serve`.

### Run 1 — `--budget 4000 --ask 45 --compact 60`: no summary

No ask ever went out. The ask is checked when a turn ends, and one Kilo step
is already 12–16 k tokens, so the first turn ended past 100 % of the window and
the edge skipped it (`fill < 100`). Both agents then left the ticket and ended
GAVE_UP. The probe's old defaults could not answer the question, so they are
now 20 000 / 55 / 85.

### Run 2 — `--budget 20000 --ask 55 --compact 85`: 4497 s

```bash
python3 contest-bench/kc40/live_probe.py --round 2 --models agnes-2-5-flash:free,glm-4-7-flash:free
```

| entry | state | asks | captured | fill at the ask | commit |
| --- | --- | --- | --- | --- | --- |
| agnes-2-5-flash | STALLED (no event for 180 s) | 2 | 1 | 76.6 %, 69.3 % | `8b61a85`, against the ticket |
| glm-4-7-flash | GAVE_UP (`no_progress_row`, `no_test_file`) | 3 | 3 | 96.4 %, 92.7 %, 92.3 % | `6ea342e`, against the ticket |

**Captured at all: yes, for both.** The plumbing held on every path it met:

- The ask went out before any `summarize`.
- The file got one section per session and was appended to.
- agnes' first ask ended `error`. Its partial text (1177 chars, including an
  `aaaa` block) was kept in the file, and the work went on in a new session,
  which wrote a clean summary on its first ask.
- A `summarize` of agnes hung for 900 s, and the runner went on in a new
  session.

**Truthful: what changed and why — yes.** Every section names the real change:
`sum(values, 0)` in `pkg/thing.py`, `TypeError` for a non-numeric element, and
the diff left uncommitted. glm named the test file it had written against the
ticket; it did not hide it. After a `git reset` to base, glm's last section
says so. No section mentions the docstring, which also changed; that is minor.

**Useful: what is left — no.** That part cannot be trusted:

- Only the first section, agnes' failed ask of attempt 0, got it right: keep
  replying with the `a` blocks. Every section written after a REWORK repeats
  the runner's rework prompt instead (add a test, one commit, `append_task`),
  and the probe's ticket forbids all three. In a real round that list would be
  right, so this is the probe's ticket against the round's rules, not a
  runner bug.
- glm's last section lists tickets 02, 03 and 04 in `epic-tasks/`. Only 01
  exists. A later session that trusts it would go looking for work that is not
  there.

**Verdict.** The edge works live and the account of the diff is truthful.
The "left to do" part is not, and a carried summary should be read for what
changed, not for what to do next.

Two things were seen but not changed:

- The ask comes at the end of a turn, so the fill it sees overshoots `--ask` by
  a whole step (glm: 92–96 % against 55 %). On a real 32 768 window a step is a
  smaller share of the window.
- The ask prompt could say "only what this ticket still needs" to cut both the
  rework echo and the made-up tickets. That is a change to the prompt, not to
  KC-40's edge.
