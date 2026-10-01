# KC-36 — round 75 scored against the ticket's Acceptance list

Ticket: `epic-tasks/75-kc36-a-turn-still-changing-files-extends-its-own-deadline-instead-of-being-killed-at-turn-timeout-sec.md`.
Base: `2b4342e`. Ideal: `5840147` (sensenova-6-7-flash-lite-var1, as is).

Bench: `acceptance_kc36.py`, 24 scenarios through the public contract
(`wait_idle` on the base's scripted tap and fake clock, `runner._churn`,
`run_agent` / `run_round` on `tests/_kilo_fake.py`, `load_roster`).
`run_all_kc36.sh <cb-dir>` copies it into each tree and runs one tree at a time.
On the base: 1/24 (K1, today's timeout). R7 is the round-2 splitter: an extended
turn that then goes quiet has to be named a silence stall, which needs the
extended deadline in the runner's `quiet` test.

Entry trees are the slot worktrees' `git diff 2b4342e` (committed or not),
in `contest-out/75/diffs/` (not committed).

| entry | round | acc /24 | misses | roots |
|---|---|---|---|---|
| **sensenova-6-7-flash-lite-var1** | STALLED 3600 s, uncommitted | **24** | — | 5149 ✓ / 2878 ✓ |
| sensenova-6-8-flash-lite-var1 | STALLED 3600 s, uncommitted | 24 | — (keeps a second walker next to `_churn`) | 5144 ✓ / 2878 ✓ |
| sensenova-6-7-flash-lite-var2 | READY 0a23ad9 | 23 | R5b (passes `on_deadline=None`) | — |
| agnes-3-0-flash | STALLED 3600 s, uncommitted | 23 | R7 | 1 failed / 2878 ✓ |
| sensenova-6-8-flash-lite-var2 | GAVE_UP (206b568) | 21 | R5, R7-ok; R4 flaky: records a `granted: 0` extension at the cap | — |
| laguna-s-2-1 | STALLED 3600 s, uncommitted | 22 | R3 (stall does not name the sample), R7 | — |
| mimo-v2-5 | READY 5495441 | 22 | R3, R7 | — |
| step-3-7-flash | STALLED 3600 s, uncommitted | 20 | R5, R5b (callback armed at extend 0), S4 (negative accepted), R7 | — |

R4 runs on real time; a single miss under load for mimo / sn68-var1 did not repeat in 3 reruns.
