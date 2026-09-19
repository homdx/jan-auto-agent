# KC-6 round — `tools/contest/runner.py`, scored black-box

Ticket: `epic-tasks/45-kc6-runner-prompt-wait-harvest-rework-in-the-same-session-for-n-agents.md`
(round 45), base `f48a92c`. Nine files came in for seven distinct entries
(two pairs were the same diff); Laguna S-2-1 sent nothing. The DeepSeek entry
was disqualified by the operator (it read other agents' folders during the
run); it is scored below for the record only.

Method (`contest-bench/README.md`): one worktree per entry at the base with
the patch applied, 28 scenarios written from the ticket's Acceptance list
plus the edges a round of N agents exposes, each run in a fresh interpreter
with `cwd` = the entry (`run_one_kc6.py <scenario> <base-worktree>`), against
`tests/_kilo_fake.py` of the base tree and a sandbox repo with one worktree
per agent. Only the ticket's contract is called; `state.json` is read back
as JSON and turned into a `RoundState` through the public constructors.

## Scores

| entry | model | bench 28 | ideal's pytest 28¹ | own tests | runner / tests lines | one commit | on-ticket files | Py 3.10 import |
|---|---|---:|---:|---|---|---|---|---|
| sn68v1 | Sensenova 6-8 var1 | **27** | 18 | 22 green | 1197 / 1179 | yes | yes | yes |
| deepseek | DeepSeek (DQ) | 27 | 16 | 16 green | 948 / 821 | yes | yes | yes |
| sn68v2 | Sensenova 6-8 var2 (= KC6-plus-last-fix) | 26 | 19 | 17 green | 1082 / 838 | **no (2)** | **no** — `tests/_kilo_fake.py` | yes |
| sn68v3 | Sensenova 6-8 var3 | 25 | 14 | 20 green | 1176 / 1028 | yes | yes | yes |
| sn67v3 | Sensenova 6-7 var3 | 24 | 12 | 27 green | 929 / 1404 | yes | **no** — `tools/contest/kilo_client.py` (+27, KC-12's ground) | yes |
| sn67v1 | Sensenova 6-7 var (= var2) | 18 | 10 | 16 green, **1 red** (stall test) | 837 / 1188 | yes | **no** — `tools/contest/kilo_client.py` (+8) | yes |
| sn68v4 | Sensenova 6-8 var4 | 8 | 3 | 18 green | 968 / 886 | **no (2)** | yes | yes |
| laguna | Laguna S-2-1 | — | — | no submission | — | — | — | — |
| ideal | `kc6-ideal` | **28** | 28 | 28 green | 622 / 799 | yes | yes | yes |

¹ `tests/test_contest_runner.py` of the ideal patch copied into the entry's
tree and run there — it asserts the ideal's exact shapes (turn record keys,
`last_error` wording, `from_dict` round trip), so it is stricter than the
bench, which only checks the ticket's contract. Every entry shipped tests;
"own tests" is the entry's own `tests/test_contest_runner.py` on its tree.

## Scenario matrix

```
scenario                                                  deepseek   sn67v1   sn67v3   sn68v1   sn68v2   sn68v3   sn68v4    ideal
s01_symbols_and_states                                        PASS     PASS     PASS     PASS     PASS     PASS    ERROR     PASS
s02_round_prompt                                              PASS     PASS     PASS     PASS     PASS     PASS     PASS     PASS
s03_happy_path                                                PASS     FAIL     FAIL     PASS     PASS     PASS    ERROR     PASS
s04_rework_then_ready                                         PASS     FAIL     FAIL     PASS     PASS     PASS    ERROR     PASS
s05_give_up_keeps_commit                                      PASS     PASS     PASS     PASS     PASS     PASS    ERROR     PASS
s06_permission_mechanical_once                                PASS     PASS     PASS     PASS     PASS     PASS    ERROR     PASS
s07_permission_gate_reject                                    PASS     PASS     PASS     PASS     PASS     PASS    ERROR     PASS
s08_gate_budget_from_counters                                 PASS     PASS     PASS     PASS     PASS     PASS    ERROR     PASS
s09_three_questions_stall                                     PASS     PASS     PASS     PASS     PASS     PASS    ERROR     PASS
s10_two_questions_is_not_a_stall                              PASS     PASS     PASS     PASS     PASS     PASS    ERROR     PASS
s11_questions_reset_per_turn                                  PASS     FAIL     PASS     PASS     PASS     PASS    ERROR     PASS
s12_session_error                                             PASS     PASS     PASS     PASS     PASS     FAIL    ERROR     PASS
s13_unknown_model_error_with_body                             PASS     PASS     PASS     PASS     PASS     PASS    ERROR     PASS
s14_silence_stalls_within_3s                                  PASS     FAIL     PASS     PASS     PASS     PASS    ERROR     PASS
s15_busy_events_keep_a_turn_alive                             PASS     PASS     PASS     PASS     FAIL     PASS    ERROR     PASS
s16_turn_timeout_stalls                                       PASS     PASS     PASS     PASS     PASS     PASS    ERROR     PASS
s17_server_goes_away_is_error                                 PASS     PASS     PASS     PASS     PASS     PASS    ERROR     PASS
s20_round_two_agents_ready_state_json                         PASS     PASS     PASS     PASS     PASS     PASS     PASS     PASS
s21_max_parallel_one_is_sequential                            PASS     PASS     PASS     PASS     PASS     PASS     PASS     PASS
s22_max_parallel_two_overlaps                                 PASS     FAIL     PASS     PASS     PASS     PASS     PASS     PASS
s23_unknown_model_does_not_stop_the_round                     PASS     PASS     PASS     PASS     PASS     PASS     PASS     PASS
s24_three_agents_rework_in_parallel_state_json_always_valid   PASS     FAIL     FAIL     PASS     PASS     PASS     PASS     PASS
s25_permissions_do_not_cross_agents                           PASS     PASS     PASS     PASS     PASS     PASS     PASS     PASS
s26_ctrl_c_then_resume                                        PASS     FAIL     PASS     PASS     FAIL     FAIL    ERROR     PASS
s27_resume_ready_tree_needs_no_session                        PASS    ERROR     PASS     PASS     PASS     PASS    ERROR     PASS
s28_resume_restarts_in_same_worktree_and_amends               PASS     PASS     PASS     PASS     PASS     PASS    ERROR     PASS
s29_round_state_json_roundtrip_of_runner                      PASS    ERROR     PASS     PASS     PASS     PASS    ERROR     PASS
s30_stall_with_a_chatty_neighbour                             FAIL     FAIL     FAIL     FAIL     PASS     FAIL     PASS     PASS
PASS total                                                      27       18       24       27       26       25        8       28
```

## What sank whom (`results.json` has every reason)

- **s30, the concurrency edge — five of seven.** With N agents on one
  server the fake (like any shared stream) delivers every agent's events to
  every tap. deepseek, sn67v1, sn67v3, sn68v1 and sn68v3 clock silence as
  "any event on my tap", so a silent agent next to a chatty one is not
  aborted for `idle_event_timeout_sec` but only when the neighbour goes
  quiet (7 s instead of 1 s here; a whole turn in a real round). sn68v2 and
  the ideal count only the session's own events, as KC-12 specifies.
- **Ctrl-C (s26).** sn67v1 and sn68v3 run the pool inside
  `with ThreadPoolExecutor(...)`: the `with` exit joins every worker before
  the `except KeyboardInterrupt` can send `abort`, so the round waits out
  the turn timeout (20 s here, 30 min live) and then marks the agent
  STALLED — a terminal state, which `--resume` would skip. sn68v2 aborts
  correctly but a worker woken by the closed tap still transitions to ERROR
  after the state was reset to CREATED.
- **One line per transition, not per turn (s03/s04/s24).** sn67v1 and
  sn67v3 write four `turns.jsonl` lines per turn; the Acceptance says one.
- **Windowed wait (s15).** sn68v2 runs `wait_idle` in windows of
  `idle_event_timeout_sec`; KC-1's `wait_idle` sends `abort` on every
  timeout, so a live session that keeps emitting is aborted at the first
  window edge.
- **Clock mix (s14).** sn67v1 stores `time.time()` in `last_event_at` and
  subtracts it from `time.monotonic()`: the gap is never positive, the
  monitor never fires, silence is caught only by the turn timeout (30 s).
- **Questions never reset (s11).** sn67v1 counts questions per run, so two
  questions in each of two turns stall the second turn.
- **Contract (s01…, sn68v4).** `AgentState` is a `typing.Literal`, not an
  enum — `AgentState.READY` raises; `AgentRun` needs `state=` and is
  replaced immutably. KC-7 written to the ticket would not run against it.
- **Resume drops agents (s27/s29, sn67v1).** READY agents are removed from
  `RoundState.agents` for the duration of the round and appended back only
  at the end, so `state.json` mid-round lacks them and the returned state has
  no entry for a skipped agent.
- **`last_error` without the payload (s12, sn68v3).** `session.error from
  the server` — the operator cannot tell a provider 429 from a crash.

## DeepSeek and the other entries

Textual overlap was measured (lines ≥ 25 chars, minus the runbook prompt,
the ticket's own text and imports). deepseek shares 85 such lines with
sn67v3, 53–56 with the other Sensenova entries, 37 with sn68v4; the
Sensenova entries share 29–72 among themselves. The design differs
(`_Env` class in tests, `_new_client` seam, a dangling wait thread on stall),
so the patch alone does not prove copying — the operator's observation of
the session (reads outside its worktree) is the evidence, and that is what
the runner now closes mechanically: the ideal puts the rounds folder (the
siblings of the agent's worktree) on the policy's forbidden list, so such a
read is a mechanical `reject` recorded in `decisions.jsonl`.

## The ideal (`kc6-ideal`)

Written from the ticket with the bench as the spec; structure closest to
sn68v1 (linear state machine, counters on the run, `inspect.signature` for
KC-12's keyword), with: per-session silence clock (sn68v2's idea, without
the windowed wait), Ctrl-C = `stop.set()` → save → abort + close taps →
re-raise, one `turns.jsonl` line per turn, `RoundState.from_dict` for KC-7,
the sibling-worktree rule, 622 lines. 28/28 here, 28 tests of its own.

Re-run: `RUNBOOK.md` (`setup_kc6.sh` builds the worktrees, `run_all_kc6.py --wt`).
