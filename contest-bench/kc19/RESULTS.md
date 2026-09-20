# KC-19 — round 58 scored black-box

Ticket: `epic-tasks/58-kc19-a-retryable-session-error-is-re-prompted-into-the-same-session-not-error.md`.
Base: `c27bc32` (= `d0d8a45` + KC-15 parked `queued` so the round could start beside round 54 on hp-uz — see KC-24).
Bench: `scenarios_kc19.py` — 49 scenarios on `run_agent` against the base's `tests/_kilo_fake.py`
(no entry changed the fake) through the base's sandbox/harness; `ingest_kc19.sh <name> <file>` runs the
mechanical checks, the entry's own tests and the bench in `../cb-kc19/<name>`.

## The round

`python3 -m tools.contest run --ticket 58 --models agnes-2-5-flash:free,mimo-v2-5:free,step-3-7-flash:free,hy3:free,agnes-3-0-flash:free,muse-spark-1-3,north-mini-code:free,nex-n2-5-pro:free --max-parallel 8`,
2026-09-20 14:36–15:07 local, this machine, `contest-out/58/`.

| slot | outcome | when | why |
|---|---|---|---|
| agnes-3-0-flash, north-mini-code, nex-n2-5-pro | ERROR | t+17 s | `Model not found: kenary/<id>` — ids that do not exist (KC-25) |
| muse-spark-1-3 | ERROR | t+225 s | `the model's provider interrupted the response stream` — the very shape this ticket retries (not in the ticket's message list: `isRetryable` absent, no code, no status word) |
| mimo-v2-5 | **READY** | t+1296 s | commit `7929e4e`, harvest READY, `mimo-v2-5.patch` |
| agnes-2-5-flash, step-3-7-flash, hy3 | STALLED | t+1800 s | `no idle after 1800s` — every one of them was inside its self-check `python3 -m pytest tests -q --timeout=180` (four full suites in parallel on one box; ~95 s alone) when `turn_timeout_sec` ran out; the whole change edited, **not committed** (KC-21's shape one step earlier — `run.commit` null and no `.patch`; the judge took `git diff` from the worktrees) |

## Entries

| entry | model | commits over base | files | fake | removed base tests | own roster | own runner | cli | bench |
|---|---|---|---|---|---|---|---|---|---|
| mimo | mimo-v2-5:free | 1 (`7929e4e`) | 5, +292/−5 | untouched | 0 | 42 ✓ | 44 ✓ | 20 ✓ | **49/49** |
| step | step-3-7-flash:free | 0 (worktree diff) | 5, +175/−2 | untouched | 0 | 43 ✓ | 42 ✓ | 20 ✓ | **49/49** |
| agnes | agnes-2-5-flash:free | 0 (worktree diff) | 5, +242/−11 | untouched | **1** (`test_unknown_model_is_error_with_the_body` deleted) | 44 ✓ | 41 ✓ | 20 ✓ | 48/49 |
| hy3 | hy3:free | 0 (worktree diff) | 5, +260/−7 | untouched | 0 | 41 ✓ 1 ✗ | **aborted at 19** | 20 ✓ | 48/49 |

All four: `RETRY_PROMPT` a module constant, `_retryable(error) -> bool`, `run_agent` signature unchanged,
both roster keys in `CONTEST_KEYS` with the ticket's defaults, `contest.ini` commented, no `time.sleep`,
tiers clean, `_shrink` untouched, py3.10 import ok.

### What separated them

- **agnes** — `s12` (a reset *after* a rework): the retry is labelled `rework` and re-sends the rework
  sentence, not `RETRY_PROMPT` — `rework_text` wins over `retry_text` in its `kind`/`text` selection, so
  the model is told to redo the rework instead of "continue from where you were". And it deleted a base
  test (`test_unknown_model_is_error_with_the_body`) while inserting its own — "every existing test
  unmodified" broken.
- **hy3** — `s10` (Ctrl-C inside the backoff): `except KeyboardInterrupt: return finish(ERROR, "retry interrupted…")`
  swallows the interrupt — the round would not stop, the agent is marked ERROR, `state.json` is no longer
  "mid-flight, resumable". Its `backoff_event` is created and waited on but never set by anyone. Its own
  Ctrl-C test lets the interrupt escape and aborts the whole `test_contest_runner.py` run at test 19; its
  roster test loads the repo's `contest.ini` and needs `${CONTEST_GATE_API_KEY}` in the environment.
- **step** — green everywhere, but: the backoff waits on `tap._stop` (the tap's private event) and on a
  set tap `return run` leaves the run un-finished (no `finish`, no terminal state); the retry prompt's
  parenthesis is `_brief(payload)` — the whole JSON dict — where the ticket wants `data.message`; the
  retry reuses `rework_text` with an `is_retry` flag rather than its own slot.
- **mimo** — separate `retry_text` consumed on send (a rework's text survives a retry), `_retry_reason`
  picks `data.message` for the prompt, `_RETRYABLE_MSG_RE` / `_RETRYABLE_CODES` as constants, guards every
  shape (`data` not a dict, `metadata` not a dict), docstring diagram updated, the stalled-during-backoff
  case records a `retry` turn with `idle_status = "stalled"`. Ten tests named after the Acceptance
  bullets. Only nit: the INFO line logged `_brief(idle.error)` (the JSON) instead of the message.

## Winner: mimo-v2-5 — the only harvested entry and the cleanest one

Ideal = mimo's `7929e4e` cherry-picked onto `kc` (`a17ef98`) + one polish: the INFO line uses
`_retry_reason(idle.error)` (`agent-a: retry 1/2 in 0s — Connection reset by server`), asserted in
`test_retryable_error_reprompts_same_session_and_recovers` via caplog.

## Findings for the epic

- **KC-21** gets a sibling case: three STALLED worktrees with the finished change *uncommitted* — the
  self-check ate the clock. Nothing exports them today; the judge ran `git diff` by hand.
- **KC-25** confirmed (three dead ids, half the slots gone at t+17 s).
- `turn_timeout_sec = 1800` against `max_parallel = 8` on one machine: the self-check is
  `pytest tests` (≈95 s alone, minutes under 4-way contention) run two or three times per agent.
  Either the budget or the prompt's self-check line has to change for big rosters — a ticket.
- `the model's provider interrupted the response stream` (muse-spark-1-3) is not in the ticket's
  message rule and carries no `isRetryable`/code — it stays ERROR under the landed change. Worth adding
  `interrupted the response stream` to the rule when it recurs.
