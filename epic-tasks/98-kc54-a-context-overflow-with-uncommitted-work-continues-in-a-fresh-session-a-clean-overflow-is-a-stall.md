# KC-54 — A context overflow with uncommitted work continues in a fresh session; a clean overflow is a stall

**Status:** landed `f383335` (2026-09-24) — round 98, winner `sensenova-6-8-flash-lite-var1` (its uncommitted tree, cut off by the 3600 s turn clock before it committed; the fullest tests of the round. `step-3-7-flash` READY `61a067d` had the same runner change and thinner tests), plus `_is_overflow` reading the top-level `message` too. Found live 2026-09-24 in round 91 (base `58e20e5`). Five of the round's twelve agents ended in `ContextOverflowError`, and the runner marked each one `ERROR` and stopped. Three still had uncommitted work: `step-3-7-flash` with 356 lines on its rework attempt (`kilo_client.py`, `runner.py`, `tests/test_contest_kilo_client.py`), `glm-4-7-flash` with 31 lines and `hy3` with 18. Two of them, `laguna-s-2-1` and `nex-n2-5-pro`, had clean worktrees. The work was not lost, but it was not recovered either. A `ContextOverflowError` on a clean worktree means the model spent its whole context reading and produced nothing. That is a stall, not a crash.
**Severity:** HIGH (a model that fills its context mid-turn loses all its work, with no recovery path; five of twelve entries in round 91)
**File:** `tools/contest/runner.py`
**Symbol:** `run_agent` (the `idle.status == "error"` branch, ~line 667), `_is_overflow` (new)
**Round:** 98
**Size:** S
**Source:** round 91, `contest-out/91/state.json` (`last_error` of the five agents) and `git status` in `rounds/91-*`.
**Depends on:** KC-22 (`continue_message`, `round_prompt(..., dirty=)`, `max_continues_per_attempt`), KC-21 (the harvest of a commit under a dead turn). `_dirty_tree`, `_commits_above`, `_brief` and `TreeReadError` are already in the tree.
**Also touches:** `tests/test_contest_runner.py`

---

## What happens today

`run_agent` opens **one** session at `CREATED` and sends every later turn
(`retry`, `rework`, `continue`) into that same session with
`backend.prompt(session, text)`. When `wait_idle` returns
`status == "error"` and `_retryable` returns `False`, the runner does this:

```python
error = f"session.error: {_brief(idle.error)}"
state = AgentState.ERROR
```

A `ContextOverflowError` reaches this branch. It is not retryable, not a stall
and not a closed stream. The agent is marked `ERROR`. The KC-21 block below it
harvests only a commit above the base, so uncommitted changes are ignored.

A `continue` sent into the **same** session cannot help. That session's context
is already full, so the next prompt overflows again.

## What must change

### 1. `_is_overflow(error) -> bool`

This is a module-level function, shaped like `_retryable`. It returns `True`
when either of these holds:
- the payload's `name` is `"ContextOverflowError"`;
- its `data.message` (or top-level `message`) contains `"maximum context length"`
  or `"context_length_exceeded"`.

A plain string matches when it contains any of those three. `None`, `{}` and
other shapes are `False`.

### 2. In the `status == "error"` branch, before the `_retryable` check

The overflow check goes **first**. An overflow must never be sent to
`RETRY_PROMPT` in the same full session, even when a provider flags it as
retryable.

```python
if _is_overflow(idle.error):
    budget = int(config.max_continues_per_attempt)
    dirty = ""
    if _commits_above(ws) == 0:
        try:
            dirty = _dirty_tree(ws)
        except TreeReadError as exc:
            _log.warning("%s: tree unreadable — %s", spec.name, _brief(str(exc)))
    if dirty and 0 < budget and continue_used < budget:
        run.turns.append(turn)
        _append_jsonl(agent_dir / "turns.jsonl", {"agent": spec.name, **turn})
        # a fresh session: the old one's context is full
        try:
            session = backend.create_session(
                spec.provider_id, spec.model_id, rules=config.session_rules(),
                title=ws.branch, agent=spec.kilo_agent, variant=spec.variant)
        except (ContestBackendError, ValueError) as exc:
            return finish(AgentState.ERROR, f"POST /session failed: {_brief(str(exc))}")
        run.session_id = session.id
        continue_text = round_prompt(spec.name, ticket_path, ws.base_sha, dirty=dirty)
        continue_used += 1
        continue
    error = "context overflow" + ("" if dirty else " with no uncommitted work")
    state = AgentState.STALLED
```

The rules for this branch:
- **The new session gets `round_prompt(..., dirty=dirty)`, not `continue_message`.**
  A new session has not seen the ticket or the round prompt.
  `round_prompt(dirty=)` already exists for exactly this case (KC-22,
  `--resume`), and it appends the `continue_message` paragraph. Write no new
  message function.
- **Budget.** The continue budget of the attempt caps the fresh sessions, as
  it caps the KC-22 continues. A model that overflows again after the budget
  is spent ends as `STALLED`, and the loop cannot run forever.
- **Do not `return` on the stall.** Set `error` and `state` and fall through to
  the existing `if state is not None:` block. A commit above the base, where
  the model committed and then overflowed, is harvested there by KC-21 and
  can still end `READY`. A direct `return finish(...)` would skip that harvest.
- **Record the old session.** The `finally` block records only the last
  `session`. Before it is replaced, add `turn["session_id"] = run.session_id`,
  the old id, to the turn that overflowed. That keeps the old session
  findable in `turns.jsonl`.

No other change. Every non-overflow `session.error` takes today's path
unchanged.

## Acceptance

- [ ] `tests/test_contest_runner.py`, on the existing fake backend:
  - **overflow, dirty tree:** the fake emits `session.error` with `name: "ContextOverflowError"`, and the worktree holds an uncommitted file. Expect:
    - `create_session` is called a **second** time;
    - the next prompt goes to the **new** session and contains the dirty-tree lines;
    - the overflowed turn in `turns.jsonl` carries the old `session_id`;
    - `run.session_id` is the new one.
  - **overflow, clean tree, no commit:** → `STALLED`, `error == "context overflow with no uncommitted work"`, one `create_session` call.
  - **overflow with a commit above the base:** → the KC-21 harvest runs. A READY-shaped commit ends `READY`, not `STALLED`.
  - **overflow, dirty tree, budget spent** (`max_continues_per_attempt = 1`, overflow twice) → `STALLED` after the second overflow, and `create_session` is called twice in total.
  - **`max_continues_per_attempt = 0`**, dirty tree → `STALLED`, no second session.
  - **non-overflow `session.error`** (`name: "SomeOtherError"`) → `ERROR`, as before.
- [ ] `_is_overflow` returns `True` for each of these:
  - `{"name": "ContextOverflowError", "data": {"message": "..."}}`;
  - `{"name": "Other", "data": {"message": "the request exceeds the model's maximum context length"}}`;
  - the string `"ContextOverflowError"`.

  It returns `False` for `None`, `{}` and a retryable network error.
- [ ] Every existing test in `tests/test_contest_runner.py` passes unmodified.
- [ ] `python3 -m pytest tests -n 4 -q --timeout=180` then
  `python3 -m pytest tests_bugfix -n 4 -q --timeout=180`, run one after the other; both green.
- [ ] `python3 scripts/sync_test_tiers.py --check` is clean.

## Out of scope

- KC-40's summary prompt (asking the model to describe its work before overflow).
  That fires *before* the overflow; this ticket handles *after*.
- Any change to `_retryable`, `RETRY_PROMPT` or the retry counter.
- Carrying the rework reasons into the fresh session. The next harvest reports
  them again.

## Self-check before `append_task.py`

- [ ] `python3 --version` is **3.10.12**, and `python3 -c "import tools.contest.runner"` runs clean.
- [ ] Exactly **one** commit above the base, holding only this ticket's work.
- [ ] `git diff --stat <base>..HEAD` names only `tools/contest/runner.py` and
  `tests/test_contest_runner.py` (plus `.smoke_tests/` links). Never `epic-tasks/`.
- [ ] `python3 scripts/sync_test_tiers.py --check` is clean.
- [ ] The new tests are red without the change.
- [ ] `CollectBridge._shrink` byte-identical.
- [ ] `scripts/append_task.py` from the worktree with the sha of the one commit.

## Ground rules

- Do not touch `CollectBridge._shrink`.
- No test starts a real `kilo` or calls a live provider.
- Do not edit `epic-tasks/`.
- One commit, no push; a test ships with the change and fails without it.
