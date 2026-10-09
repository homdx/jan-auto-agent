# 162 — `Executor._serialise_pytest` appends `-n 0` to the end of the whole command line, onto `tail`, `tee`, `echo`

**Status:** landed
**Origin:** `arena-bugs-sonet5` ticket 175 — found while widening 172; fixed in the same change
**Severity:** MEDIUM
**File:** tools/auto/executor.py
**Symbol:** Executor._serialise_pytest (with utils.pytest_piece_spans)
**Round:** 162
**Size:** S
**Also touches:** tools/auto/utils.py, tests/test_run3_exec_feedback.py

Found on the second look at ticket 163: widening `is_pytest_command` made this older bug reach more commands, so it was fixed in the same change.

## The bug

RUN-3's `_serialise_pytest` returns `f"{command.rstrip()} -n 0"`: the flag goes on the very end of the line. For a plain `pytest tests` that is the pytest run; for anything piped or chained it is **another command's** argument list. Reproduced on a project whose `pytest.ini` has `addopts = -n auto`:

```
'pytest tests | tail -5'               -> 'pytest tests | tail -5 -n 0'                 # tail -n 0: prints nothing
'python -m pytest -q 2>&1 | tee run.log' -> '... | tee run.log -n 0'                    # tee writes files "-n" and "0"
'pytest a && echo done'                -> 'pytest a && echo done -n 0'
'cd x && pytest -q; echo $?'           -> 'cd x && pytest -q; echo $? -n 0'
```

`pytest … | tail` is how an agent keeps a long run short, and it is exactly the line where the tail of the output (the failure) is thrown away by `tail -n 0`, and the pool is *not* suppressed, so the in-process guarantee RUN-3 exists for is lost too.

## Fix

- `utils.pytest_piece_spans(command)`: the character spans of every pytest run, pieces cut at unquoted `|`, `&`, `;`, newline; the `&` of `2>&1` / `&>` is not a cut; a piece whose quoting does not parse is not a run.
- `_serialise_pytest` inserts ` -n 0` at the end of each span (last first so offsets hold), skipping a piece that already carries an xdist flag. No pytest span, no pool in the config, `pytest_serial` off: the command is returned unchanged, as before.

## Tests

`tests/test_run3_exec_feedback.py`: the flag lands on the run in the four lines above and in `pytest a; pytest b` (both); `pytest -n 4 | tail`, `--dist=…`, a non-pytest line and `ls | tail -n 2` are unchanged; `2>&1` and `&>` stay whole.

## Acceptance

```bash
python3 -m pytest tests/test_run3_exec_feedback.py tests/test_pytest_ini_quiet_summary.py -n 4 -q
```
