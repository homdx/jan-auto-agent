# RUN-3 — Exec feedback shows the head of pytest output; the cause is at the tail

**Status:** landed — RUN-3 commit on top of `d4ffb12` (SHA stamped in the next commit, as `7135844` → `5788423`). Competition (12 entrants, `run3/`; reviewer probe = the 4 ACs + a >1 500-char box + 5 real-subprocess workspace runs against a `pytest.ini` with `addopts = -n auto -q --dist=loadgroup`): **DeepSeek-V4-1-Flash — winner, base of the ideal** (probe 10/10; `-n 0` gated on the project's own addopts, shared `is_pytest_command` in `utils.py`, ERRORS/FAILURES-only anchor because the summary is printed *after* the box, header + tail when over budget, README + `agents.ini`; not taken: its edits to `epic-tasks/`, `INDEX.md`, `STATUS.md`, `NEXT-ROUNDS.md` — ground rule — and the flag only reached the task-loop executor, not AUTO-G5's); Sensenova-68-var2 — 10/10, `-p no:xdist` made safe by rewriting the workspace `addopts` with `-o addopts=` (270 lines incl. a pyproject parser; strips pytest-core `--lf/--ff` too) — taken: `[executor]` placement after `[auto]`, the real-subprocess proof that bare `-p no:xdist` exits 4; Sensenova-67-var1 — 9/10 (box head-cut over budget), `-n 0` gated + cached + `PYTEST_ADDOPTS`, config wired to the AUTO-G5 executor — taken: the cache, the env read, the single injection site in `run()`; Sensenova-68-var1 — 8/10: `-n 0` unconditional (a project without xdist exits 4), stdout preferred over stderr — taken: keep the box header and its last lines when trimming, `safe_getboolean` in `controller.py`; agness2-5 — 7/10: `-p no:xdist` only on the bare `pytest` fallback (exits 4 there; the real `python -m pytest …` check is never serialised), breaks the fail-open line pin — taken: the fake-loop test shape; HY3 6/10, Laguna-S-2-1 6/10, Sensenova-67-var2 6/10 — `-p no:xdist` on every pytest command: with this repo's addopts every workspace run exits 4 before collecting (Laguna and 67-var2 also put stdout before stderr; 67-var2 9 failures — smoke tiers not synced); MistralMedium-3.5 5/10, Nemotron3-ultra 5/10 — anchor on the LAST header, which is `short test summary info`: the `E   ModuleNotFoundError` line is dropped — AC1 fails; Nemotron has no config key and no tests; GLM-47 3/10 — defines `_is_pytest_cmd` as an `Executor` staticmethod and imports it as a module function: `ImportError` on every failure, head behaviour unchanged, no tests — disqualified; GLM67 — a RUN-6 patch (`collect_bridge.py`), does not apply — not a RUN-3 entrant. Late entrants (four Sonnet patches, filed under `run4/` as `run3-*`/`RUN-3-sonet5`, reviewed against `d4ffb12` with the same probe after `26b5d7f` had landed): Sonnet-5-var2 (`run3-pytest-sonet5-var2-tail-feedback`) — 6/10, green in `tests`/`tests_bugfix`, 33 tests of its own, `[executor] pytest_serial` wired to both executors incl. AUTO-G5's; Sonnet-4.6-var1 (`run3-sonet-4-6-var1-exec-feedback`) — 6/10, no `.smoke_tests/` mirror (4 tier/pre-commit tests fail); Sonnet-5-var1 (`RUN-3-sonet5`) — 5/10, green, shared `is_pytest_command` in `utils.py` like the winner's, but anchors on the *last* of `ERRORS`/`FAILURES`/`short test summary info` — the summary is printed after the box, so the `E   ModuleNotFoundError` line is dropped (AC1 fails); Sonnet-4.6-var2 (`run31-exec-sonet-4-6-var2-feedback`) — 5/10, green, same last-header anchor (AC1 fails). All four append a bare `-p no:xdist`: with this repo's `addopts = -n auto -q --dist=loadgroup` inherited by the workspace, every real workspace run exits 4 (`unrecognized arguments: -n`) before collecting — the 3 real-subprocess probes fail, the same trap as HY3/Laguna/67-var2; and all four cut the *head* of a box longer than 1 500 chars (the `E` line at its end is lost). Nothing to fold into `26b5d7f`. Competition closed.  
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
