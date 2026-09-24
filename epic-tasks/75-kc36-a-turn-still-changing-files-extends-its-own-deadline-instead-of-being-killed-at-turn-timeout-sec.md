# KC-36 — a turn that is still changing files extends its own deadline instead of being killed at `turn_timeout_sec`

**Status:** queued — found live on 2026-09-21 in round 64 (`contest-out/64/`), where the 1800 s turn clock killed the four agents that were working and let the one that was stuck run on.
**Severity:** HIGH (a round loses its best entries to a constant, and the operator reads `STALLED` for an agent that had 400 lines written)
**File:** `tools/contest/kilo_client.py`, `tools/contest/runner.py`, `tools/contest/roster.py`, `contest.ini`
**Symbol:** `KiloClient.wait_idle`, `_wait_turn`, `run_agent`, `_churn`, `_turn_deadline`, `ContestConfig.turn_extend_sec`, `ContestConfig.turn_max_sec`
**Round:** 75
**Size:** M
**Source:** round 64, `--ticket 64 --max-parallel 8`, eight free models, base `9912b78`. Every turn was sent at 11:29:52; `contest-out/64/state.json` and the worktrees under `../rounds/64-*` read, together, like this:

| agent | `idle_status` | turn | files / lines in the worktree | commits |
|---|---|---|---|---|
| agnes-2-5-flash | `timeout` | **1801 s** | 4 / +273 −7 | 0 |
| hy3 | `timeout` | **1802 s** | 5 / +344 −7 | 0 |
| step-3-7-flash | `timeout` | **1800 s** | 5 / +305 −6 | 0 |
| mimo-v2-5 | `stalled` | 1419 s | 5 / +403 −3 | 0 |
| nex-n2-5-pro | `stalled` | 1357 s | 0 | 0 |
| laguna-s-2-1 | `error` | 759 s | 0 | 0 |
| agnes-3-0-flash | `error` | 336 s | 0 | 0 |
| glm-4-7-flash | `idle` ×5 | 488, 251, 62, 87, 48 s | 3 / +78 −16 | 1 |

Three agents hit `turn_timeout_sec = 1800` **to the second** — the clock, not the model, ended them — each with a worktree carrying 273–344 uncommitted lines across 4–5 files. Nothing in the round says they were productive; `state.json` records `"idle_status": "timeout"` and the KC-18 line reads `STALLED — no idle after 1800s`, the same words a dead session gets.
The silence clock already does the job the turn clock is being asked to do: `idle_event_timeout_sec = 300` caught nex-n2-5-pro at 1357 s with an empty worktree, and `session.error` caught two more inside 13 min. The truly idle agent is detected in five minutes and does not need a thirty-minute backstop.
Meanwhile glm-4-7-flash, the one agent the turn clock never touched, is the one that deserved it: after 11:56 its `decisions.jsonl` stops while `events.jsonl` keeps taking `server.heartbeat` every 10 s, and its last bash ask is corrupted output —
`'>=isAdminParameter2, provider_id, model_id: no provider \'<> ?¯7T (stricked'`.
Its worktree stopped changing; only the transport stayed alive. A clock that reads the worktree stops that agent and spares the other three — the same rule, both ways.
**Depends on:** KC-6 (`run_agent`, `run_round`, landed `e8c6ad3`), KC-12 (the silence clock `idle_event_timeout`), KC-18 (`_Heartbeat`, landed `9c4db8b`).
**Also touches:** `tests/test_contest_runner.py`, `tests/test_contest_kilo_client.py`, `tests/test_contest_roster.py`, `tests/_kilo_fake.py`

---

## What happens today

`KiloClient.wait_idle` (`kilo_client.py:819`) takes one number and never
revisits it:

```python
started = time.monotonic()
deadline = started + float(timeout)
```

and on reaching it sends `abort` and returns `IdleResult(status="timeout")`.
`_wait_turn` (`runner.py:465`) hands it `config.turn_timeout_sec` — `1800` by
default (`roster.py:178`, `contest.ini:44`) — and the runner turns that status
into `AgentState.STALLED`. Nothing between the session and the clock knows
whether the agent is writing code or printing heartbeats.

## What must change

### 1. `wait_idle` asks before it kills

`wait_idle` gains one keyword-only argument:

```python
on_deadline: "Callable[[float], float | None] | None" = None
```

When the overall deadline is reached and `on_deadline` is set, it is called
once with the elapsed seconds **before** `abort` is sent. A positive return
value is added to the deadline and the loop continues; `None`, `0`, a negative
number, or any exception (logged, never raised) aborts exactly as today.
Omitted — every existing caller — the behaviour is byte-for-byte today's.
The silence clock is untouched and keeps racing independently: an extension
never resurrects a session that has gone quiet.

### 2. The runner decides from the worktree

```python
def _churn(ws: Workspace) -> tuple[int, int]:
    """(files touched, lines changed) in *ws*, committed and not. Never raises."""
```

the union of

- `git -C <path> --no-optional-locks status --porcelain --untracked-files=all`
  → the paths, and for each untracked file its line count;
- `git -C <path> --no-optional-locks diff --numstat` → added + deleted per path;
- `git -C <path> --no-optional-locks diff --numstat <base_sha>..HEAD` → what is
  already committed;

minus everything under `.smoke_tests/` (`sync_test_tiers.py` links). Any git
failure, a missing worktree, a binary `-` in `--numstat` → that path counts `0`;
the function returns `(0, 0)` rather than raising. `--no-optional-locks` so a
sample never takes `index.lock` from the agent's own `git commit`.

If KC-27 has landed, `_worktree_files` is the files half of this — share it,
do not write a second walker.

### 3. The extension rule

`run_agent` builds one closure per turn and passes it to `_wait_turn`:

- a sample is `(files, lines)` from `_churn`, taken when the turn is sent and
  again on every deadline;
- **progress** means the current sample is strictly greater than the previous
  one in either number. Files alone is too coarse — an agent that writes a file
  in its first minute and then loops shows the same count forever — so `lines`
  is what usually moves;
- on progress the deadline is pushed by `turn_extend_sec` and the sample is
  kept as the new previous;
- on no progress the turn ends as it does today: `abort`, `status="timeout"`,
  `STALLED`;
- total wall time for one turn never exceeds `turn_max_sec` — the last
  extension is clipped to it, and at the cap the turn ends whatever the churn
  says. A model in an infinite edit loop is bounded.

### 4. Config and defaults

`roster.py` gains two limits next to `turn_timeout_sec`, both read by
`limit()` and validated `>= 0`:

| key | default | meaning |
|---|---|---|
| `turn_extend_sec` | `600` | seconds granted per extension |
| `turn_max_sec` | `7200` | hard ceiling for one turn, whatever the churn |

`turn_extend_sec = 0` disables extension entirely, which is today's behaviour
and what a test that wants the old clock sets. `turn_max_sec` below
`turn_timeout_sec` is a `RosterError` naming both keys. `contest.ini` gains both
with a comment saying the turn clock is now a floor, not a limit, and that the
silence clock (`idle_event_timeout_sec`) is what actually catches a dead agent.

### 5. The operator can see it happen

- `turn["extensions"]` — a list, one entry per grant:
  `{"at": <elapsed s, 1 dp>, "files": int, "lines": int, "granted": int}`. Absent
  when nothing was granted, so a reader of the old keys is unaffected.
- A KC-18 INFO line per grant:
  `<agent>: WAITING — +10m at 30m (5 files, 344 lines)`.
- The turn that finally times out names the sample it refused:
  `STALLED — no idle after 70m (5 files, 344 lines, unchanged for 10m)`.
- The heartbeat's `(attempt N)` suffix gains `+Nm` once an extension was
  granted, so an agent running past the nominal clock is not mistaken for a
  hung round.

### 6. Agent names, not model ids

Every sample is keyed by `run.agent.name` and read from `run.workspace.path` —
never from `model_id`. With KC-34 §7 a round can hold `hy3-var1` and `hy3-var2`
on the same model in different worktrees, and each must extend on its own churn.
No new structure may be keyed by the model.

## Acceptance

- [ ] `tests/test_contest_kilo_client.py`:
  - `wait_idle` without `on_deadline` — the existing timeout tests unchanged
    and green (`abort` sent, `status="timeout"`, `elapsed ≈ timeout`);
  - `on_deadline` returning `30` twice then `None`: the fake session stays
    open past the original deadline, the callback is called at
    `timeout`, `timeout + 30` and `timeout + 60`, `abort` is sent once, and
    `elapsed >= timeout + 60`;
  - `on_deadline` returning `0` / a negative number / raising `RuntimeError`
    → abort at the original deadline, no exception out of `wait_idle`, the
    raise logged;
  - a session that goes silent mid-extension still ends on the silence clock,
    not on the extended deadline.
- [ ] `tests/test_contest_runner.py`, against the fake:
  - a worktree whose line count grows between samples → the turn is extended,
    `turn["extensions"]` has one entry with the right `files`/`lines`, and the
    KC-18 line says `+10m at 30m`;
  - a worktree that does not change → no extension, `STALLED`, no
    `extensions` key, and the error text names the sample;
  - growth every time → extensions stop at `turn_max_sec`, the last grant is
    clipped so the total is exactly `turn_max_sec`, and the turn ends `STALLED`;
  - `turn_extend_sec = 0` → `on_deadline` is not passed at all and the turn is
    today's path, byte for byte;
  - `_churn` on: a worktree with an untracked file; one with a tracked edit;
    one with a commit above `base_sha`; one where `git` fails (a deleted
    directory) → `(0, 0)`, no raise; `.smoke_tests/` links excluded from both
    numbers;
  - two runs named `hy3-var1` and `hy3-var2` on the same `model_id` in
    different worktrees: each extends on its own churn, and the one that is
    idle stalls while the other runs on.
- [ ] `tests/test_contest_roster.py`: the two new keys parse, default
      `600` / `7200`, are inherited by `--models` rounds, and
      `turn_max_sec < turn_timeout_sec` is a `RosterError` naming both.
- [ ] Every existing test unmodified and green.
- [ ] `python3 -m pytest tests -n 4 -q --timeout=180` then
      `python3 -m pytest tests_bugfix -n 4 -q --timeout=180`, sequentially, both green.
- [ ] Replaying round 64's numbers: agnes-2-5-flash, hy3 and step-3-7-flash
      (churn growing at 30 min) are extended; glm-4-7-flash (churn flat since
      11:56, only `server.heartbeat` on the wire) is not.

## Out of scope

- `idle_event_timeout_sec` and the silence clock — they already work and are
  the reason the turn clock can stop being a hard kill.
- The harvest and the verdict: an extended turn is an ordinary turn, and KC-21
  / KC-29 decide what a `STALLED` worktree is worth.
- Raising `turn_timeout_sec` itself. A bigger constant makes a dead agent cost
  more and still cuts a live one off; the point of this ticket is that the
  number stops being the thing that decides.
- Per-model timeouts in the roster.
- `contest-bench/kc6/live_smoke.py`'s own copy of the loop.

## Self-check before `append_task.py`

- [ ] `python3 --version` on the judge is **3.10.12**.
- [ ] Exactly **one** commit; only the files listed in `**File:**` and the test files touched.
- [ ] `git diff --stat <base>..HEAD` names only `tools/contest/kilo_client.py`,
      `tools/contest/runner.py`, `tools/contest/roster.py`, `contest.ini`,
      `tests/test_contest_runner.py`, `tests/test_contest_kilo_client.py`,
      `tests/test_contest_roster.py`, `tests/_kilo_fake.py`
      (plus `.smoke_tests/` links). Never `epic-tasks/`.
- [ ] `python3 scripts/sync_test_tiers.py --check` clean.
- [ ] New tests red without the change.
- [ ] `CollectBridge._shrink` byte-identical.
