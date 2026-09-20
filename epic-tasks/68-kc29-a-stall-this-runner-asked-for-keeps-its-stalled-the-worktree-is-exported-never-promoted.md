# KC-29 — a stall this runner asked for keeps its `STALLED`: the worktree is harvested and exported, never promoted to `READY`

**Status:** queued — after KC-28, last in the queue; found 2026-09-20 scoring round 60 (KC-21), where nine entries split three ways on the same turn.
**Severity:** MEDIUM (a round can exit 0 on an agent the runner aborted; or lose that agent's finished work entirely)
**File:** `tools/contest/runner.py` (`run_agent` — the KC-21 terminal harvest)
**Symbol:** `run_agent`, `stall`, `finish`
**Round:** 68
**Size:** S
**Source:** KC-21 (round 60) says "when the turn ends in `STALLED` or `ERROR` **and** the worktree has at least one commit above the base … if the verdict is `READY`, finish as `READY`". Two kinds of turn end `STALLED`, and the ticket does not separate them: the session died on its own (the turn clock, the silence window, `session.error`) and the **runner killed it** — `stall()` fires on `max_questions_per_turn` (`3 questions in one turn`), sends `POST /abort` and closes the tap. Probed against all nine round-60 entries with a worktree committed and claimed before a three-question turn: six finish **`READY`** (agnes-2-0-flash, agnes-2-5-flash, mimo-v2-5, step-3-7-flash, hy3, Sensenova-6-7-var2, Sensenova-6-8-var1), two keep **`STALLED` with `commit: null`** and no patch (Sensenova-6-7, Sensenova-6-8-var2), and the base drops it. Neither end is right: `READY` means the agent finished and claimed — an agent the round aborted for asking questions did not finish, and a round of only such agents would exit 0 — while `commit: null` throws away the very work KC-21 exists to keep.
**Depends on:** KC-21 (the terminal harvest, landed this round), KC-6 (`run_agent`), KC-16 (`export_patches`).
**Also touches:** `tests/test_contest_runner.py`

---

## What happens today

After KC-21 the terminal branch reads (shape, not text):

```python
            if state is not None:
                if state in (AgentState.STALLED, AgentState.ERROR) and commits_above_base:
                    verdict = _harvest(ws, ticket_path, run_tests)
                    turn["harvest"] = {...}
                    run.commit = ...
                    if verdict.verdict == "READY":
                        return finish(AgentState.READY, note=f"{sha12} after {error}")
                return finish(state, error)
```

`stalled` — the list `stall()` appends its reason to — is not consulted, so
`3 questions in one turn` reaches the same promotion as `no idle after 1800s`.

## What must change

1. The harvest stays for **every** terminal turn with a commit, including a
   stall the runner asked for: `turn["harvest"]` and `run.commit` are set, so
   `export_patches` writes `<agent>.STALLED.patch` and the operator reads it.
2. The **promotion** to `READY` happens only when the session ended on its own
   — `not stalled`. A turn the runner aborted finishes `STALLED` with
   `last_error` unchanged whatever the verdict, and the KC-18 line says so:
   `<agent>: STALLED — 3 questions in one turn (harvest: READY)`.
3. Exit code: unchanged code, changed outcome — an aborted agent is not a
   `READY`, so a round of nothing but aborted agents exits non-zero (KC-16's
   `EXIT_OK` needs a `READY`).

## Acceptance

- [ ] `tests/test_contest_runner.py`, against the fake:
      - a worktree committed and claimed, then a turn with `"questions": 3` →
        `STALLED`, `last_error` is the questions text, `run.commit` is the
        branch's sha, `turn["harvest"]["verdict"] == "READY"`, and the KC-18
        line names both the stall and the verdict;
      - the same turn with no commit → today's path: no harvest, `commit` is
        `None`, no `harvest` key (monkeypatch `_harvest` to raise);
      - the KC-21 silence/`session.error` promotions are unchanged — a turn
        that died on its own with a READY harvest still finishes `READY`.
- [ ] `tests/test_contest_cli.py`: a round whose only agent is an aborted
      stall with a valid commit writes `<agent>.STALLED.patch` and exits
      non-zero.
- [ ] `python3 -m pytest tests -q --timeout=180 && python3 -m pytest tests_bugfix -q --timeout=180` green (sequentially).

## Out of scope

- The questions cap itself (`max_questions_per_turn`) — an operator's
  `contest.ini` decision.
- `GAVE_UP`, which is a verdict about the work, not about the session.
