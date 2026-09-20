# KC-18 — The round narrates itself: every transition is a log line, and once a minute a heartbeat says who is doing what

**Status:** landed `9c4db8b` — by hand, no contest (observability only: no verdict, no policy, no state changes). Round 57 of EPIC KC (`docs/kilo-contest/EPIC-KC.md`); found on 2026-09-20 in the first manager-run round (`python3 -m tools.contest run --ticket 52`, three kenary free models).
**Severity:** HIGH
**File:** `tools/contest/runner.py` (`run_agent` → `transition`, `run_round`)
**Symbol:** `run_agent`, `run_round`, `ContestConfig.progress_every_sec`
**Round:** 57
**Size:** S
**Source:** the operator saw the seven plan lines KC-16 prints and then **nothing for 20 minutes** while three sessions ran — no line when a prompt went out, none when two agents died at minute 3, none when the harvest started running 7 600 tests in the third one's worktree. `runner.py` logs at INFO exactly once, on a permission decision; every other line is a `warning` on a failed write. `state.json` and `turns.jsonl` carry all of it, but a round is watched from a terminal, and `cli.main` already wires `logging.basicConfig(INFO, stderr)` for lines that do not exist.
**Depends on:** KC-16 (`cli.py`, landed `1304950`) — the logging config lives there.
**Also touches:** `tools/contest/roster.py` (`CONTEST_KEYS`, `ContestConfig`: `progress_every_sec`), `contest.ini` (the key, commented), `tests/test_contest_runner.py`, `tests/test_contest_roster.py`

---

## What happens today

`run_agent` calls `transition(state, error)` on every edge, and `run_round`'s
`on_transition` writes `state.json` — nothing goes to the logger. The only
INFO line is `<agent>: permission … -> once (mechanical)`. A round with no
permission asked is silent from the plan until the JSON rows at the end.

## What must change

1. `transition()` in `run_agent` logs one INFO line per edge through
   `_log` (`tools.contest.runner`), the agent's name first:
   `laguna: PROMPTED attempt 0 (initial)` / `laguna: WAITING` /
   `laguna: HARVESTING (tests on)` / `mistral: REWORK attempt 1 — no_progress_row, commits_ne_1`
   (the harvest's codes, comma-joined) / `laguna: ERROR — session.error: {…}`
   (the `error` argument, already `_brief`ed) / `hy3: READY 2fa4e1d39958`
   (`run.commit[:12]`). The format is free; what is fixed: **every**
   transition produces exactly one line, the line starts with `<name>: <STATE>`,
   and a terminal line carries the error text or the commit.
2. `run_round` starts a daemon thread that, every `config.progress_every_sec`
   seconds (new `[contest]` key, default `60`, `0` = off), logs one INFO
   heartbeat: the round's elapsed time and, per agent, its state and how long
   it has been in that state — `round 52 12m: mistral WAITING 3m (attempt 1) · laguna ERROR · hy3 ERROR — 1 live`.
   Time in state is measured in `run_round`'s `on_transition` (a dict
   `name → time.monotonic()`), **not** as a new `AgentRun` field:
   `AgentRun.to_dict`/`from_dict` and `state.json`'s shape are KC-6's contract
   and stay byte-for-byte (`test_agent_run_and_round_state_round_trip_through_json`).
   The thread stops with the round — `finally`, before `pool.shutdown` —
   and never outlives `run_round` (a `threading.Event` it waits on; no `sleep`).
3. Nothing else changes: no new `print`, no new stream — the `cli` prints its
   JSON rows to stdout as before, and every new line is `_log.info`, so a
   test or a library caller that never configured logging sees nothing.
4. `roster.py`: `progress_every_sec` joins `CONTEST_KEYS` (after
   `max_questions_per_turn`), `ContestConfig` (`int = 60`), `load_roster`
   (`limit("progress_every_sec", 60)`); `contest.ini` gets the key with a
   one-line comment like its neighbours.

## Acceptance

- [ ] `tests/test_contest_runner.py` (with `caplog` at INFO on
      `tools.contest.runner`):
      - the happy path (`work_ready`, one turn) logs, in order, lines starting
        `agent-a: PROMPTED`, `agent-a: WAITING`, `agent-a: HARVESTING`,
        `agent-a: READY` and the READY line contains `run.commit[:12]`;
      - the rework path logs `agent-a: REWORK` with `no_test_file` in it;
      - a `session.error` turn logs `agent-a: ERROR` with the payload's text;
      - `run_round` with `progress_every_sec=0.2` and a turn that pauses
        (`pause_before_idle_sec=1`, then `idle`) logs at least one heartbeat
        line containing `WAITING` and the agent's name;
      - `progress_every_sec=0` → no heartbeat line at all, transitions still logged;
      - after `run_round` returns, `threading.enumerate()` holds no thread
        whose name starts with the heartbeat's prefix (`contest-progress`).
- [ ] `tests/test_contest_roster.py`: `progress_every_sec` parses from
      `[contest]`, defaults to 60, and is in `CONTEST_KEYS`; the existing
      "every key" tests pick it up unmodified.
- [ ] Every existing `tests/test_contest_runner.py` test unmodified and
      green; `test_contest_cli.py` unmodified and green.
- [ ] `python3 -m pytest tests -q --timeout=180 && python3 -m pytest tests_bugfix -q --timeout=180` green.

## Out of scope

- A progress bar, colours, or a curses table — one plain line, greppable.
- Logging inside `wait_idle` or the tap (`kilo_client.py`) — the runner is
  the narrator; the primitive stays quiet.
- Writing the heartbeat to a file — `state.json` is that file.

## Ground rules (same as every round)

- Do not touch `CollectBridge._shrink`.
- No test starts a real `kilo` or calls a live provider.
- Do not edit `epic-tasks/`.
- One commit, no push; a test ships with the change and fails without it.
