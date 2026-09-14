# RUN-3 — Exec feedback shows the head of pytest output; the cause is at the tail

**Status:** open  
**Severity:** HIGH  
**File:** `tools/auto/inner_loop.py`  
**Symbol:** `InnerLoop.run_task` — the executor-rejected branch (≈ 1556–1578, `detail = f"stdout:\n{out[:400]}"`)  
**Round:** 31  
**Size:** XS  
**Source:** live runs on `../testtext` and `../testtext6` (2026-09-13/14, all `trace_*.jsonl`) — 438 executor rejections (testtext: exit 1 ×200, exit 5 ×67, exit 2 ×18; testtext6: exit 5 ×77, exit 1 ×70, exit 4 ×5). In every `exit 1` collection error and every `exit 5` the coder's feedback ends before the line that says what went wrong  
**Depends on:** —  
**Also touches:** `tools/auto/executor.py` (optional second item — see "What must change" 3), `tests/test_inner_loop*.py`  

---

## What happens today

When the executor fails, the feedback the coder gets on its next attempt is
built from the *first* 400 characters of stdout (traceback and stderr are
preferred, but pytest writes collection errors to stdout). The workspace
inherits the project's `pytest.ini` (`addopts = -n auto -q --dist=loadgroup`),
so those 400 characters are spent on xdist's banner and the frame of the
error box. What the coder actually saw, 25 times in a row on AUTO-T11
(testtext6), 15 times on AUTO-T23, and on every one of the 7 tasks that were
finally BLOCKED with an `exit 1`/`exit 5`:

```
attempt 5: exec failed (exit 1)  cmd='/usr/bin/python3 -m pytest tests/test_state.py -q'
stdout:
bringing up nodes...
bringing up nodes...


==================================== ERRORS ====================================
_____________________ ERROR collecting tests/test_state.py _____________________
ImportError while importing test module '/home/…/.agent/workspace/AUTO-T11/tests/test_state.py'.
Hint: make sure your te
```

The next line — `E   ModuleNotFoundError: No module named 'x'` or
`E   ImportError: cannot import name 'y' from 'tools.state'` — is the only
diagnostic line in the whole output, and it is cut. For `exit 5` (no tests
collected: the `-k` expression matched nothing, or the file the coder wrote
defines no `test_*`) the 400 characters are two banner lines and blank
space; the coder is told nothing at all and re-emits the same file until the
attempt cap.

`[:400]` was chosen when the executor ran a script whose first lines were
the interesting ones. For pytest the interesting lines are the last ones.

## What must change

1. **Tail, not head, for test runners.** When the command is a pytest
   invocation (`executor.py` already knows — it builds the command and
   `_is_pytest_cmd`-style checks exist around ≈ 599–673), the detail is the
   *last* N characters of stdout, with the error box kept whole: find the
   last `ERRORS`/`FAILURES`/`short test summary info` header and start
   there; if none, take the tail. Non-pytest commands keep the head
   (script output is head-interesting).

2. **A budget that fits one error.** 400 characters do not hold one
   collection error. Raise the per-detail budget to 1 500 characters for the
   tail path (the round-6 feedback path at ≈ 641 already allows 2 000 for
   the same stdout — two limits for one stream is the bug). Strip xdist's
   `bringing up nodes...` lines and runs of blank lines before applying the
   budget; they carry nothing.

3. **`exit 5` gets a sentence.** pytest's exit code 5 means "no tests were
   collected". Prefix the detail with
   `no tests collected — the -k expression matched nothing or the file defines no test_* function`
   so a coder without the pytest exit-code table learns what to fix.

4. *(optional, executor)* Run workspace pytest with `-p no:xdist` (or
   `-n 0`): one file, one worker. Every workspace run today spawns N
   workers on a machine the user describes as "not super powerful", pays
   the "bringing up nodes" startup twice per attempt, and interleaves
   output. This is a one-flag change in the command builder; keep it
   behind `[executor] pytest_serial = true` (default true) so a project
   that needs xdist can turn it back on.

## Acceptance

- [ ] `tests/`: an `ExecResult` with exit 1 whose stdout is a 900-char pytest
      collection error → the feedback contains the `E   ModuleNotFoundError`
      line verbatim.
- [ ] exit 5 with banner-only stdout → feedback starts with the
      `no tests collected` sentence.
- [ ] A non-pytest command (`python3 script.py`) with long stdout → head
      behaviour unchanged (existing tests still pass).
- [ ] `bringing up nodes...` never appears in feedback.
- [ ] If item 4 is taken: the workspace pytest command contains `-p no:xdist`
      by default and does not when `pytest_serial = false`.
- [ ] `python3 -m pytest tests -q --timeout=180 && python3 -m pytest tests_bugfix -q --timeout=180` green (run sequentially).

## Out of scope

- Why the import fails in the workspace (the coder's file imports a name
  that does not exist) — that is the coder's job once it can see the line.
- Any change to the validator's feedback.

## Ground rules (same as every round)

- Do not touch `CollectBridge._shrink`.
- Do not run anything against `agents_128k.ini` or any live provider.
- Do not edit `epic-tasks/`.
- One commit, no push.
