# KC-39 — round 78 — results

Base `5b9362c`. Eight slots: three READY (agnes-2-5-flash, mimo-v2-5,
sensenova-6-7-flash-lite-var2), four STALLED at the 90-minute limit with a
deadline commit, and glm-4-7-flash stopped on a context overflow with a commit
that does not import. Scored tree by tree with `acceptance_kc39.py`
(`run_all_kc39.sh ../rounds <tree>...`), which runs through the public contract:

- S1–S8: `_diff_signature`. A clean tree is `""`. Content counts, and an
  untracked file counts. Committed work does not. The gitignored `runs/`, which
  every live worktree holds once the agent writes its progress row, neither
  blanks the signature nor changes it.
- R1–R17: the runner end to end over the fake Kilo.
  - A repeat on the last continue: abort, a second `POST /session` for the same
    model, the `git status` lines in the new session's prompt, the attempt
    unchanged, `new_session` and `diff_signature` in `turns.jsonl`, then READY.
  - Ceilings 0, 1 and 2: 2 resets once and harvests in place.
  - Changing content never resets.
  - Only the last continue resets.
  - Cost and tokens are summed.
  - Both transcripts are kept.
  - `state.json` and SUMMARY show the sessions.
- C1–C2: `contest.ini` and the default of 2.

§7, the 90 % rework rule, was retired by the ticket's audit before scoring and
is not scored.

| entry | state | score | misses |
| --- | --- | --- | --- |
| base | — | 0/27 | all |
| agnes-2-5-flash | READY | **26/27** | R16 (no SUMMARY column) |
| sensenova-6-7-flash-lite-var2 | READY | 25/27 | S6, S8: `git add -A -N -- ':!runs'` exits 1 whenever the ignored `runs/` exists, so live it never resets |
| sensenova-6-8-flash-lite-var1 | STALLED | 25/27 | R8, R9 (the ceiling: a reset at 1, or a second one at 2) |
| sensenova-6-7-flash-lite-var1 | STALLED | 24/27 | R8, R9, R15 |
| agnes-2-0-flash | STALLED | 24/27 | R5, R9, R16 |
| sensenova-6-8-flash-lite-var2 | STALLED | 23/27 | R8, R9, R15, R16 |
| mimo-v2-5 | READY | 21/27 | R1, R3 (no abort), R8, R9, R15, R16 |
| glm-4-7-flash | STALLED | 0/27 | the suite does not import |

Winner by score: agnes-2-5-flash. The ideal (`KC-39: a continue that repeats its
diff …`, 27/27) is built on sensenova-6-7-flash-lite-var2 instead:

- It has the per-attempt ceiling with a run-wide count beside it.
- `_record_once` records each session exactly once, so a refused swap does not
  count cost twice.
- A refused reset is left `resumable`.
- It has the SUMMARY column.

Changes on the way in:

- §7 was dropped.
- The add takes no pathspec (the S6/S8 bug).
- All swaps (KC-54 overflow, KC-69 context swap, the KC-39 reset) go through one
  `_replace_session`. It records the outgoing session and retires it with
  KC-73's abort.
- Its two §7 tests were rewritten as repeat-based ones.

On kc: `tests` 6009 passed / 2 skipped, `tests_bugfix` 2878.
