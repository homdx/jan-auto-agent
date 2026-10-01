# 133 — fix four contest gates defects: pytest timeout guard, wrapped declared paths, rename prefix, flaky id parse

**Status:** landed — round 133, winner mimo-v2-5 (15/15 on contest-bench/133, base 7/15), as-is
**Severity:** MEDIUM
**File:** `tools/contest/gates.py`
**Symbol:** `_pytest, _declared_paths, judge_worktree, run_tests_detail`
**Round:** 133
**Size:** M
**Also touches:** `requirements.txt`, `tests/test_contest_harvest.py`

---

## Why

Four defects in `tools/contest/gates.py` block or mis-score contest rounds: an unconditional `--timeout=180` makes every pytest root exit 4 on machines without `pytest-timeout` (no agent can reach READY); a one-line `**Also touches:**` parse drops wrapped paths; exact `off_ticket` comparison and rename-bearing `git diff` flag declared files as off-ticket; and `PASS*` is emitted when node-id parsing loses a failure, hiding real reds as flaky.

## What to build

In `tools/contest/gates.py`:

- `_pytest` (KC-5/KC-57/KC-76): `cmd = [sys.executable, "-m", "pytest", *args, "--timeout=180"]` appends `--timeout=180` unconditionally. `importlib.util.find_spec` is imported at module top but is only called in `run_tests_detail` as `serial = ["-n0"] if importlib.util.find_spec("xdist") else []` — the brief's `pytest_timeout` lookup is never read here. Append `--timeout=180` only when `importlib.util.find_spec("pytest_timeout")` is not `None`, mirroring the `xdist` lookup.

- `_declared_paths` (KC-17/KC-34): `m = re.search(rf"^\*\*{label}:\*\*\s*(.+?)\s*$", body, re.M)` captures a single line; for `epic-tasks/123-kc76-*.md` the `**Also touches:**` line wraps and `declared_files` returns only the first line's backticked spans (`tests/test_contest_cli.py` without `contest-bench/kc76/`). Read the field from the `**Label:**` line up to the next blank line or the next `**Label:**` line (a line matching `^\*\*[^*]+:\*\*`), then run the existing backtick scan and `_looks_like_path` (which admits spans with `/` or ending in `_PATH_ENDINGS`) over that whole block.

- `judge_worktree` (KC-5): `outside = [f for f in files if f not in declared and not tests.count(f)]` compares `f` exactly against `declared`; a file under a declared directory ending in `/` (e.g. `contest-bench/kc76/x.py` vs `contest-bench/kc76/`) is counted `off_ticket`. Treat a declared entry ending in `/` as a prefix match. Also `stat = git(path, "diff", "--numstat", f"{merge_base}..HEAD")` runs without `--no-renames`, so a rename shown as `old => new` or `dir/{a => b}.py` never matches `declared` and the later `git show HEAD:<that>` fails; pass `--no-renames` to that `git` call; the deleted side of a rename (the old path) is not counted as off-ticket.

- `run_tests_detail` (KC-26): `bad, ids = _failures(lines)` where `_failures` takes `bad` from the stats line (`\d+ failed` / `\d+ error`) and `ids` from `_SUMMARY_LINE` (`^(?:FAILED|ERROR) (\S+?)(?:@[^\s\[]+)?(?: - .*)?$`); a node id with a space (`` `test_x[a b]` ``) does not match `_SUMMARY_LINE`, so `len(ids) < bad`. The branch `if rerun is not None and rerun.returncode == 0: out.append(f"{d}:PASS*{bad}")` emits `PASS*{bad}` whenever the rerun passes, marking never-rerun failures flaky. Emit `PASS*{bad}` only when `len(ids) == bad`; otherwise emit `f"{d}:{bad}✗"`.

In `requirements.txt`: the file currently carries only `tree-sitter>=0.23` and `tree-sitter-java>=0.23` under the `# COLLECT-25` comment block; add a commented `# pytest-timeout` line.

In `tests/test_contest_harvest.py`: add tests covering each fix — `_pytest` omits `--timeout=180` when `importlib.util.find_spec` is monkeypatched to return `None` and includes it when the spec is found; `_declared_paths` on a body with a wrapped `Also touches:` line returns all 4 paths and still stops at the next `**Label:**` line; `judge_worktree` `off_ticket` is `0` for a file under a declared dir and a renamed file is counted under its new path; `run_tests_detail` with 2 failures where one id is unparseable reports `2✗`, not `PASS*2`, even when the rerun passes.

## Acceptance

```bash
python3 -m pytest tests/test_contest_harvest.py -n 4 -q
python3 -m pytest tests_bugfix -n 4 -q
```

Run the two roots one after the other, never in one pytest command.

## Rules

- Commit subjects prefixed by ticket (`133: …`) describing the behavior change, one concern per commit.
- New tests in `tests/test_contest_harvest.py` are feature tests in `tests/` and must be tiered via `python3 scripts/sync_test_tiers.py` (`--check` verifies them); do not hand-edit symlinks.
- `python3 -m pytest tests_bugfix -n 4 -q` must remain green (keep `tests_bugfix` green).
- Match the module's explanatory comment style; type hints on public functions; standard library only in `tools/contest/gates.py`.
