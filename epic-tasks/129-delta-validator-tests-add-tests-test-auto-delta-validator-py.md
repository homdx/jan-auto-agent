# 129-DELTA-VALIDATOR-TESTS — add tests/test_auto_delta_validator.py covering DeltaValidator

**Status:** open
**Severity:** LOW
**File:** tests/test_auto_delta_validator.py
**Symbol:** DeltaValidator, DeltaVerdict, check, _read_at_head, make_delta_validator, feedback
**Round:** 129
**Size:** S
**Also touches:** tools/auto/delta_validator.py

---

## Why

`tools/auto/delta_validator.py` has only thin bugfix coverage (`tests_bugfix/test_gates_3_delta.py`) and no feature test in `tests/`. The brief requires dedicated tests for `DeltaValidator.check` verdicts, HEAD reads (tracked, untracked, not a git repo), and `make_delta_validator` per `task_mode`, with no production changes unless a bug is exposed.

## What to build

Cases drawn directly from `tools/auto/delta_validator.py`:

- `DeltaValidator.check` branches (all return `DeltaVerdict`):
  - `base_dir` not a directory → `DeltaVerdict(approved=True, reason="base_dir unavailable")`
  - `rel_path` empty → `DeltaVerdict(approved=True, reason="no rel_path given")`
  - `_read_at_head` returns `None` → `DeltaVerdict(approved=True, reason="no prior version to compare")`
  - `text.strip() != prior.strip()` → `DeltaVerdict(approved=True, reason="content differs from HEAD")`
  - `text.strip() == prior.strip()` → `DeltaVerdict(approved=False, rel_path=rel_path, reason="unchanged from the version already committed at HEAD")`; `DeltaVerdict.feedback()` returns message containing `` `rel_path` ``
  - any exception → `DeltaVerdict(approved=True, reason=f"error: {exc}")`
- `_read_at_head` (called by `check`):
  - invokes `run_git(["git","-C",str(base_dir),"show",f"HEAD:./{rel_path}"], timeout=10)`
  - on `OSError`, `subprocess.TimeoutExpired`, `UnicodeDecodeError` → returns `None` (logged)
  - on `result.returncode != 0` → returns `None`
  - on success → returns `result.stdout`
  - tracked file at HEAD: returns baseline content; untracked file (no HEAD entry) → `returncode != 0` → `None`; not a git repo → `OSError`/`returncode != 0` → `None`
- `make_delta_validator` config keys and fallbacks:
  - `config.getboolean("validator_agent", "delta_check", fallback=True)` → if false returns `None`; on `ValueError` logs warning and continues (enabled)
  - `config.getint("validator_agent", "max_delta_revisions", fallback=1)` → on `ValueError` sets `max_rev = 1`
  - returns `DeltaValidator(max_delta_revisions=max_rev)` when enabled
  - parameter `task_mode` (named in the brief) is **never read** inside `make_delta_validator`; tests must call it with `task_mode="creative"`, `"code"`, etc. and assert identical behaviour regardless of value.

## Acceptance

```bash
python3 scripts/sync_test_tiers.py --check
python3 -m pytest tests/test_auto_delta_validator.py -n auto
python3 -m pytest .smoke_tests/
```

## Rules

- Add `tests/test_auto_delta_validator.py` with a one-line docstring; file name matches `test_<area>_<feature>.py`.
- Use pytest with `pytest.ini` defaults (`-n auto --dist=loadgroup`); no port-bound HTTP markers needed.
- Regenerate tiers via `python3 scripts/sync_test_tiers.py` (never by hand); verify with `--check`.
- Enable hook with `git config core.hooksPath githooks` if not already set.
- No production code changes in `tools/auto/delta_validator.py` unless a test exposes a real bug; if so, separate commit with ticket prefix.
- Run `python3 -m pytest .smoke_tests/` as the fast pre-commit gate.
