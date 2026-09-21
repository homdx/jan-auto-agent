# RUN-2 — The per-task wall-clock budget must not tick while the run is stopped

**Status:** landed — ideal patch from the round-30 competition (base: Sensenove-6-8 var1 — try/finally ledger wrap, `parse_budget_file`/`render_budget_file`, split wall/monotonic test clock; `math.isfinite` guard on ledger fields from Sensenove-6-7 var1 + its NaN/Infinity parsing test by the reviewer)  
**Severity:** MEDIUM  
**File:** `tools/auto/outer_loop.py`  
**Symbol:** `OuterLoop.run_task` (the `deadline_started_at.txt` block, ≈ 155–205)  
**Round:** 30 of 30  
**Size:** S  
**Source:** live run on `../testtext6` (2026-09-14, `log.txt`) — AUTO-T14 and AUTO-T27 were declared `BLOCKED` six seconds after `--auto` resumed, before a single LLM call, because the budget had "elapsed" while the process was not running  
**Depends on:** —  
**Preceded by:** RUN-1 (`29-run1-…`) — land it first; the two are independent in code but RUN-1 removes the failure mode that otherwise hides whether this one helps  
**Also touches:** `tools/auto/state.py` (`clear_task_deadline` docstring), `tools/auto/bug_fix_loop.py` + `tools/auto/controller.py` (they unlink the same file — keep them working), `tests_bugfix/test_bugfix_*deadline*.py`  

---

## What happens today

AUTO-CR-33 gave every task one wall-clock budget across all its rounds
(`[auto] max_task_seconds`, 1800 s in `agents.ini`). A later audit fix made
that budget survive a process restart by persisting the task's first start
time in `.agent/tasks/<id>/deadline_started_at.txt` and computing

```python
_elapsed   = time.time() - _started_at
_remaining = max(_mts - _elapsed, 0.0)
```

on every entry to `run_task`. That is *calendar* time, not *run* time. Stop
the run in the evening, resume it in the morning, and every task that had
already started is over budget before it does anything:

```
05:56:04 ♻️  Resuming existing run — 7 already done, 44 pending
05:56:10 OuterLoop: task AUTO-T14 wall-clock budget (1800s = 30.0 min) exhausted across rounds — stopping before round 10.
         … BLOCKED
05:57:33 OuterLoop: task AUTO-T27 wall-clock budget (1800s = 30.0 min) exhausted across rounds — stopping before round 4.
         … BLOCKED
```

AUTO-T27 had used 3 of 10 rounds. It was not exhausted; the clock was.

The intent of the audit fix is right — a restart must not hand a runaway
task a fresh 30 minutes — but "time the process was alive and working this
task" is the quantity to persist, not "seconds since the task was first
seen".

## What must change

1. **Persist consumed budget, not a start timestamp.** Replace
   `deadline_started_at.txt` with a small JSON (or keep the filename and
   change its content — your call, but the reader must reject the old
   float-only format cleanly and treat it as "unknown → 0 consumed"):

   ```json
   {"consumed_s": 1234.5, "session_started_at": 1757820964.1}
   ```

   - `consumed_s` — seconds this task has actually been worked, summed over
     sessions.
   - `session_started_at` — wall-clock at which the *current* session
     started working the task; present only while a session is active.

2. **Accounting.** On entry to `run_task`: read `consumed_s`; if a stale
   `session_started_at` is present (previous session died without closing),
   add `min(now - session_started_at, max_task_seconds)` to `consumed_s` —
   a crash mid-round consumes at most one budget, never a night. Then write
   `session_started_at = now`, compute `_remaining = max(mts - consumed_s, 0)`
   and derive the monotonic deadline as today. On every exit path of
   `run_task` (success, blocked, exhausted, exception — there are several
   `return OuterLoopResult(...)`; wrap them, do not duplicate) fold the
   session's elapsed time into `consumed_s` and clear `session_started_at`.

3. **Ctrl-C / process death.** A `KeyboardInterrupt` or a kill between the
   two writes leaves `session_started_at` set; rule 2's cap handles it. Do
   not try to install signal handlers.

4. **`clear_task_deadline` and the two callers** (`bug_fix_loop.py` ≈ 260–280,
   `controller.py` ≈ 1153/1201) keep their semantics: a retry / a BLOCKED
   reset starts with a full budget. Update the docstring in `state.py` to
   describe the new file content.

5. **Log line on resume.** When `consumed_s > 0` on entry, log one INFO line:
   `OuterLoop: task X resumes with 1234 s of 1800 s budget already used`.
   The existing `exhausted across rounds` WARNING stays for the real case.

## Acceptance

- [x] `tests_bugfix/`: a task worked for 600 s (mock `time.time` / monotonic),
      process "stopped" for 8 h (advance `time.time` only), resumed → the
      task gets `1800 - 600 = 1200 s`, not 0; round proceeds.
- [x] A task whose previous session crashed with `session_started_at` set
      and `now - session_started_at = 8 h` resumes with `consumed_s`
      increased by at most `max_task_seconds`, never by 8 h.
- [x] Two sessions of 1000 s each on the same task → second session's
      second round is refused with the existing `exhausted` WARNING
      (budget really does accumulate across restarts — the AUTO-CR-33 audit
      case must still hold).
- [x] Legacy file containing only a float → treated as `consumed_s = 0`,
      one WARNING, file rewritten in the new format.
- [x] `clear_task_deadline` tests (`tests_bugfix/test_bugfix_*deadline*.py`,
      5 files) still pass unchanged or with the minimal edit for the new
      file content.
- [x] `python3 -m pytest tests -n 4 -q --timeout=180 && python3 -m pytest tests_bugfix -n 4 -q --timeout=180` green (run sequentially).

## Out of scope

- The `max_tokens` truncation seen on AUTO-T30 in the same log
  (`[coder] max_tokens = 3000` is too small for a whole-file rewrite of a
  7.5 kB file) — a config change, not code.
- Any change to `InnerLoop.run_task`'s `deadline` parameter or the
  per-attempt cap.

## Ground rules (same as every round)

- Do not touch `CollectBridge._shrink`.
- Do not run anything against `agents_128k.ini` or any live provider.
- Do not edit `epic-tasks/`.
- One commit, no push.
