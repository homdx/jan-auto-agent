# RUN-1 — An extra file outside `target_files` must not fail an attempt whose target files all landed

**Status:** landed — ideal patch from the round-29 competition (base: Sensenova-6-8 var2b/var3; skip-message helper, all-skipped error naming every path, plural feedback grammar and REJECTED-trace `skipped` from Sensenova-6-7 var1; uniform Guard 2 for deletes by the reviewer)  
**Severity:** HIGH  
**File:** `tools/auto/coder.py`  
**Symbol:** `Coder._write_files`, `Coder.generate`, `CoderResult.succeeded`  
**Round:** 29 of 30  
**Size:** S  
**Source:** live run on `../testtext6` (2026-09-14, `log.txt`, mistral-medium-3-5, pre-M4 code) — AUTO-T29 burned ~40 attempts across 10 rounds and AUTO-T30 every attempt of its last 4 rounds on this one path  
**Depends on:** —  
**Also touches:** `tools/auto/inner_loop.py` (feedback text only), `tests/test_coder_safety_domain.py`  
**Followed by:** RUN-2 (`30-run2-…`) — independent code, but land RUN-1 first so the RUN-2 live re-run on testtext6 is not dominated by this failure mode  

---

## What happens today

`Coder._write_files` (`tools/auto/coder.py` ≈ 1918–1931, "Guard 2") skips
any path the LLM returned that is not in the task's `target_files`, logs

```
[SAFETY] LLM tried to write 'tests/test_next_task.py' which is not in target_files — skipped to protect unrelated files
```

and — this is the defect — records that message as `first_error`. Back in
`Coder.generate` (≈ 631–647) the result becomes
`CoderResult(files_written=[...target...], error="[SAFETY] …")`.
`CoderResult.succeeded` is `files_written and not error`, so the attempt is
`CODER FAIL` and `InnerLoop` (≈ 1478) rejects it **before the executor or
the validator ever look at the code that was actually written.**

Observed sequence in `log.txt` (AUTO-T29, "Add schema validation to
`next_task.py` PROGRESS.csv reader"):

```
08:57:48 coder._write_files [AUTO-T29]: wrote scripts/next_task.py (5070 chars)
08:57:48 coder._write_files [AUTO-T29]: [SAFETY] LLM tried to write 'tests/test_next_task.py' … skipped
08:57:48 coder.generate: [AUTO-T29] CODER FAIL — [SAFETY] LLM tried to write 'tests/test_next_task.py' …
         coder → inner_loop [decision] REJECTED
```

The target file was written, the skipped file was *not* written (the guard
did its job), and yet the attempt failed. The model then received
`attempt N: coder failed — [SAFETY] LLM tried to write …` as feedback and,
being a model that likes to ship tests with code, did exactly the same thing
on the next attempt — 5 attempts × 10 rounds, 30 minutes of wall-clock, task
BLOCKED, and the perfectly good `next_task.py` never once reached the
executor. AUTO-T30 shows the same loop interleaved with `max_tokens`
truncation (that part is a config matter, not this ticket).

The skip is *protection*; it was never meant to be a *verdict*. Nothing
unrelated was touched, so there is nothing to reject.

## What must change

1. **Split "skipped" from "failed".** `_write_files` returns
   `(written, first_error)`. Add a third channel for Guard-2 skips (a list of
   skipped relative paths, or a `skipped` field on the result — pick one and
   keep the old two-tuple callers working). Guard 2 appends to it and **no
   longer sets `first_error`**. Guard 1 (path escapes `base_dir`), the
   non-ASCII-identifier gate, and real I/O errors keep setting `first_error`
   exactly as now.

2. **Success rule.** In `Coder.generate`:
   - every declared `target_files` entry that the model returned was written
     and no `first_error` → `succeeded` is `True`, even if extra paths were
     skipped;
   - **nothing** was written because *every* returned path was outside
     `target_files` → still a failure (there is no code to validate), with
     the existing `[SAFETY]` message as the error. This is the case
     `if write_error and not written` already covers — keep it.
   - `CoderResult` carries the skipped paths (`files_skipped: list[str]`) so
     the trace and the feedback can name them.

3. **Feedback still tells the model.** Even on a successful attempt the model
   must learn that its extra files were dropped, otherwise a test file it
   "wrote" will be missing when it reasons about the next round. Put one line
   into the feedback the inner loop hands to the next attempt / round
   (`inner_loop.py`, the block that builds `fb` after the coder call):

   ```
   note: 1 file outside target_files was not written (tests/test_next_task.py) — only the listed target files are editable in this task
   ```

   Wording is yours; it must not start with `coder failed`, because
   `_write_round_feedback` and the prompt-optimizer summariser key on that
   prefix.

4. **Trace.** Emit the skip through the existing coder trace stage
   (`_trace_stage(task_id, attempt, "coder", …)`) with `skipped=[…]` in
   params so `scripts/trace_round_snapshot.py` / M5's harness can count it.
   Do not add a new event kind.

5. **Log level stays WARNING** for the skip line — it is still worth seeing.

## Acceptance

- [x] `tests/test_coder_safety_domain.py`: target file written + one extra
      path → `CoderResult.succeeded is True`, `files_written == [target]`,
      `files_skipped == [extra]`, `error == ""`; the extra file does not
      exist on disk.
- [x] Same test file: *only* extra paths, no target → `succeeded is False`,
      nothing on disk, error mentions `[SAFETY]` (unchanged behaviour).
- [x] Guard 1 (`../escape.py`) still fails the attempt (unchanged).
- [x] Inner-loop test: an attempt with a skipped extra file proceeds to the
      executor (`_trace_stage("executor", …)` is recorded for that attempt),
      and the feedback for the next attempt contains the skipped path and
      does not contain `coder failed`.
- [x] Nothing in the SAFETY guard's *blocking* semantics changes: the file
      outside `target_files` is never written, not even partially.
- [x] `python3 -m pytest tests -q --timeout=180 && python3 -m pytest tests_bugfix -q --timeout=180` green (run sequentially).

## Ground rules (same as every round)

- Do not touch `CollectBridge._shrink`.
- Do not run anything against `agents_128k.ini` or any live provider; use a
  stub config / replay proxy if a live check is wanted.
- Do not edit `epic-tasks/`.
- One commit, no push.
