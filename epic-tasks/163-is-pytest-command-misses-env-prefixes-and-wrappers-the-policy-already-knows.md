# 163 — `is_pytest_command` is False for `FOO=1 pytest`, `timeout 600 pytest`, `uv run pytest` and `a&&pytest`

**Status:** landed
**Origin:** `arena-bugs-sonet5` ticket 172
**Severity:** LOW
**File:** tools/auto/utils.py
**Symbol:** is_pytest_command, _tokens_run_pytest
**Round:** 163
**Size:** S
**Also touches:** tests/test_run3_exec_feedback.py

## The bug

Ticket RUN-3 made `is_pytest_command` the single decision for "read the **tail** of this run's output" (`inner_loop._build_exec_detail`) and "append the in-process flag" (`Executor._serialise_pytest`). It recognises a pytest run only at the very first token or right after a *spaced* separator, and only when the first word is `pytest`/`py.test`/`python -m pytest`:

```
>>> from tools.auto.utils import is_pytest_command as p
>>> [p(c) for c in ["FOO=1 pytest -q", "cd sub&&pytest", "timeout 600 pytest",
...                 "python -X dev -m pytest", "uv run pytest"]]
[False, False, False, False, False]
```

For each of those the coder gets the **head** of a long failed run (the collection banner) instead of the tail with the failure — the exact symptom RUN-3 fixed — and the executor's pytest serialisation does not apply. `tools/contest/policy._pytest_argv_start` / `_past_wrappers` already handle `VAR=value` and the `timeout N` wrapper for the same question ("is this command a suite run"), so the two modules disagree about what a pytest run is, which is the thing RUN-3's own comment says must never happen.

## Review (second pass)

Kept at LOW. The commands come from Architect-written acceptance checks and the Coder's own `run_tests`; an env prefix or `timeout` wrapper is plausible but not the common case, which is why this is a consistency gap with `policy._past_wrappers` (same question, wider answer) rather than a frequent failure.

## Fix

- Share one recogniser: move the `VAR=value` / wrapper skipping (`timeout`, `env`, `nice`, `time`, `uv run`, `poetry run`, `pipenv run`, `xvfb-run`) next to `PYTEST_TOKENS` in `tools/auto/utils.py` and have both `_tokens_run_pytest` and `policy._past_wrappers` use it (or have utils import the policy's list), so the two cannot drift.
- Split `cd sub&&pytest` / `a;pytest` with `shlex.shlex(punctuation_chars=True)` so unspaced separators count as command boundaries; `echo pytest` and `bash pytest_runner.sh` stay script runs.
- `python` options before `-m` (`-X dev`, `-u`, `-W ignore`) are skipped.

## Tests

1. The five commands above → `True`; `echo pytest`, `bash pytest_runner.sh`, `grep pytest x` → `False`.
2. A drift guard: for a table of commands `is_pytest_command(c) == (policy._pytest_argv_start(shlex.split(c)) is not None)`.
3. The RUN-3 cases unchanged.

## Acceptance

```bash
python3 -m pytest tests/test_run3_exec_feedback.py tests/test_contest_policy.py -n 4 -q
```
