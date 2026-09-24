# KC-54 — A context overflow with uncommitted work continues in a fresh session; a clean overflow is a stall

**Status:** open — found live 2026-09-24 in round 91: `step-3-7-flash` filled its context on the rework attempt (`ContextOverflowError`), leaving 356 lines of uncommitted changes in `kilo_client.py`, `runner.py` and `tests/test_contest_kilo_client.py`. The runner marked it `ERROR` and stopped. The work was not lost but was also not recovered. A `ContextOverflowError` on a clean worktree means the model spent the whole context reading without producing anything — that is a stall, not a crash.
**Severity:** HIGH — a model that fills context mid-rework loses all its work with no recovery path.
**File:** `tools/contest/runner.py`
**Symbol:** the `session.error` branch (~line 698), `_overflow_continue_message` (new), `_is_overflow` (new)
**Round:** 98
**Size:** S
**Source:** round 91 live observation (base `58e20e5`, `step-3-7-flash`, `ContextOverflowError` on rework attempt 1).
**Depends on:** nothing — `_dirty_tree`, `_commits_above`, `_brief` are already present.
**Also touches:** `tests/test_contest_runner.py`

---

## What happens today

In `run_agent`, when `wait_idle` returns `status == "error"` and `_retryable`
returns `False`, the runner does:

```python
error = f"session.error: {_brief(idle.error)}"
state = AgentState.ERROR
```

A `ContextOverflowError` reaches this branch — it is not retryable, it is not
a stall, it is not a closed stream. The agent is marked `ERROR` and the loop
exits. Any uncommitted changes in the worktree are ignored (the KC-21 harvest
below only runs when there is a commit above the base).

## What must change

Add one guard before the generic `session.error` assignment, still inside the
`status == "error"` branch:

1. **`_is_overflow(error) -> bool`** — returns `True` when the error payload's
   `name` is `"ContextOverflowError"` or its `data.message` / `message` field
   contains `"context length"` or `"context_length_exceeded"`. Mirrors the
   shape of `_retryable`.

2. **`_overflow_continue_message(dirty: str) -> str`** — a module-level string
   function parallel to `continue_message`. Text:

   > Your session ran out of context before you could commit. Your changes are
   > still in the worktree:
   > `<indented dirty lines, same cap as continue_message>`
   > Start fresh: run the tests against what is already there, fix any
   > failures, then `git add` and commit exactly one commit. Do not discard
   > these files and do not start over from scratch.

3. **In the `status == "error"` branch**, before the generic assignment:

   ```python
   if _is_overflow(idle.error):
       dirty = ""
       if _commits_above(ws) == 0:
           try:
               dirty = _dirty_tree(ws)
           except TreeReadError:
               pass
       if dirty:
           # Real work exists — continue in a new session with the diff.
           continue_text = _overflow_continue_message(dirty)
           continue_used += 1
           run.turns.append(turn)
           _append_jsonl(agent_dir / "turns.jsonl",
                         {"agent": spec.name, **turn})
           continue  # top of loop: new session, carry continue_text
       else:
           # Nothing was produced — treat as a stall, not a crash.
           run.turns.append(turn)
           _append_jsonl(agent_dir / "turns.jsonl",
                         {"agent": spec.name, **turn})
           return finish(AgentState.STALLED,
                         "context overflow with no uncommitted work")
   ```

No other changes. The existing `session.error` path handles every non-overflow
error exactly as before.

## Acceptance

- [ ] `tests/test_contest_runner.py` — use the existing fake infrastructure:

  - **`test_overflow_with_dirty_tree`**: fake emits `session.error` with
    `name: "ContextOverflowError"`, worktree has an uncommitted file → the
    runner records the turn, increments `continue_used`, and loops (does not
    return `ERROR` or `STALLED`); the next prompt contains the dirty-tree
    lines from `_overflow_continue_message`.

  - **`test_overflow_with_clean_tree`**: same error, worktree is clean (no
    diff) → runner returns `AgentState.STALLED`, not `ERROR`.

  - **`test_non_overflow_error_unchanged`**: a non-overflow `session.error`
    (e.g. `name: "SomeOtherError"`) → runner returns `AgentState.ERROR` as
    before (existing behaviour, confirm it is not broken).

- [ ] `_is_overflow` returns `True` for:
  - `{"name": "ContextOverflowError", "data": {"message": "..."}}`
  - `{"name": "Other", "data": {"message": "the request exceeds the model's maximum context length"}}`
  - a raw string `"ContextOverflowError"` (for forward compat with `_brief`-truncated payloads)

  and `False` for `None`, `{}`, a retryable network error.

- [ ] Every existing test in `tests/test_contest_runner.py` unmodified and green.
- [ ] `python3 -m pytest tests -n 4 -q --timeout=180` then
  `python3 -m pytest tests_bugfix -n 4 -q --timeout=180`, sequentially, both green.
- [ ] `python3 scripts/sync_test_tiers.py --check` is clean.

## Out of scope

- KC-40's summary prompt (asking the model to describe its work before overflow).
  That fires *before* the overflow; this ticket handles *after*.
- Any change to `_retryable`, `RETRY_PROMPT`, or the retry counter.
- Harvesting a partial commit (already handled by KC-21).

## Self-check before `append_task.py`

- [ ] `python3 --version` is **3.10.12**; `python3 -c "import tools.contest.runner"` clean.
- [ ] Exactly **one** commit above the base; only this ticket's work.
- [ ] `git diff --stat <base>..HEAD` names only `tools/contest/runner.py` and
  `tests/test_contest_runner.py` (plus `.smoke_tests/` links). Never `epic-tasks/`.
- [ ] `python3 scripts/sync_test_tiers.py --check` is clean.
- [ ] New tests are red without the change.
- [ ] `CollectBridge._shrink` byte-identical.
- [ ] `scripts/append_task.py` from the worktree with the sha of the one commit.

## Ground rules

- Do not touch `CollectBridge._shrink`.
- No test starts a real `kilo` or calls a live provider.
- Do not edit `epic-tasks/`.
- One commit, no push; a test ships with the change and fails without it.
