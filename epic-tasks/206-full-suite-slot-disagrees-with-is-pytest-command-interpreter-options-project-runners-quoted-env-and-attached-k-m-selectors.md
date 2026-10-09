# 206 — `policy.is_full_suite_command` and `utils.is_pytest_command` disagree about what a pytest run is: `python3 -u -m pytest tests`, `uv run pytest tests`, `PYTEST_ADDOPTS="-q -x" pytest tests` skip the suite slot, and `pytest tests -kfoo` takes it

**Status:** landed `bbc5861` — the suite slot and is_pytest_command share one pytest recogniser; attached -k/-m selectors are targeted
**Origin:** operator review (`bugs-to-review/test_contest_suite_command_shape.py`), probed on `arena` @ `00355fd`
**Severity:** LOW (a wasted or a bypassed suite slot: an agent can run the whole suite more often than the round allows, or be refused a targeted run)
**File:** tools/contest/policy.py
**Symbol:** is_full_suite_command, _pytest_argv_start, the tokeniser it uses
**Round:** 206
**Size:** S
**Also touches:** tools/auto/utils.py (the one recogniser both should share), tests_bugfix/

## Why

Ticket 163 made `utils.is_pytest_command` skip interpreter options before `-m` (`python3 -u -m pytest`), project runners (`uv run pytest`, `poetry run pytest`) and `VAR=value` prefixes, and said the contest policy's recogniser had to answer the same question the same way. It does not. Probed on `arena` @ `00355fd` as `(policy.is_full_suite_command, utils.is_pytest_command)`:

| command | policy | utils |
| --- | --- | --- |
| `python3 -u -m pytest tests` | **False** | True |
| `uv run pytest tests` | **False** | True |
| `poetry run pytest tests` | **False** | True |
| `PYTEST_ADDOPTS="-q -x" pytest tests` | **False** | True |
| `pytest tests/test_a.py` | False | True (correct: targeted) |
| `pytest tests -kfoo` | **True** | True |
| `pytest tests -mslow` | **True** | True |
| `FOO=1 pytest tests` | True | True |

So four spellings of a whole-suite run never take the round's suite slot, and the attached selectors `-kfoo` / `-mslow` — which name a *subset* exactly like `-k foo` / `-m slow` — are counted as a whole suite.

## Bugs

**33 — the policy recogniser starts at the wrong token.** `_pytest_argv_start` still wants `-m pytest` right after the interpreter, does not know the project runners, and its tokeniser cuts `PYTEST_ADDOPTS="-q -x" pytest tests` into `PYTEST_ADDOPTS=` and `-q -x` (read as a "command word"). Fix: one function — the shared recogniser of `tools/auto/utils.py` returns the argv start (index of the first pytest argument), and `is_full_suite_command` uses it; the tokeniser is `shlex`-based so a quoted env value stays one word.

**34 — attached `-k` / `-m` selectors do not make a run targeted.** `-kfoo`, `-mslow`, `--deselect=…`, `-k=foo` are subset selectors. Fix: treat a token that starts with `-k` or `-m` (and is longer than two characters, or is exactly `-k`/`-m` followed by its value) as a selector the same way the separate form is; other options (`-q`, `-x`, `-n 4`, `--tb=short`) still do not make a run targeted.

## Tests

`tests_bugfix/test_contest_suite_command_shape_206.py`:
33 — a whole root behind an interpreter option (`python3 -u -m pytest tests`, `python -X dev -m pytest tests`), a runner (`uv run pytest tests`, `poetry run pytest tests`) and a quoted env (`PYTEST_ADDOPTS="-q -x" pytest tests`) takes a slot; the same prefixes in front of a targeted run (`pytest tests/test_a.py`, `pytest tests -k foo`) or a non-suite command (`pip install pytest`, `echo pytest tests`) do not;
34 — `-kfoo`, `-mslow`, `-k=foo` name a subset; `-q`, `-x`, `-n 4`, `--tb=short` do not;
and the **drift guard of ticket 163**, over these commands: for every command in one shared table `is_full_suite_command(c)` is true only if `is_pytest_command(c)` is, and the two agree on what the pytest argument list is.

## Review material

Review tests (held by the operator, outside the repo): `test_contest_suite_command_shape` (19 failing cases here).
The operator keeps these outside the repo on purpose: a round that can read the reviewer's fix would copy it, and the competition would measure nothing.

## Acceptance

```bash
python3 -m pytest tests_bugfix -k "206" -q
python3 -m pytest tests -k "suite_slot or full_suite or pytest_command" -q
python3 -m pytest .smoke_tests/ -q
```
