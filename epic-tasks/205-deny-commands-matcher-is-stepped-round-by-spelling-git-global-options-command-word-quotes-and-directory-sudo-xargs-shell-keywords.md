# 205 — `deny_commands` matches the spelling, not the command: `git -C . push`, `/usr/bin/git push`, `"git" push`, `\git push`, `sudo git push`, `xargs git push` and `then git push` all pass `git push*`

**Status:** queued
**Origin:** operator review (`bugs-to-review/`: the "left unchanged" High finding and `test_contest_deny_git_options_and_wrappers.py`, `test_contest_deny_newline_and_keywords.py`), probed on `arena` @ `00355fd`
**Severity:** HIGH (a denied command runs; `git push*` is the contest's first and most important deny pattern)
**File:** tools/contest/policy.py
**Symbol:** _deny_match, _shell_pieces, COMMAND_WRAPPERS (+ the command-word normaliser this ticket adds)
**Round:** 205
**Size:** M
**Also touches:** tests_bugfix/, tests/test_contest_policy_deny*.py (the existing deny tests), docs/kilo-contest/RUN-THE-KILO-CONTEST.md (the deny-list paragraph)

## Why

`deny_commands` is the mechanical backstop behind Kilo's own rule: a command that reaches layer 1 with no path outside the worktree is allowed. Tickets 198 and 201 fix how the *shell text* is cut into commands (newline, quotes, heredocs). This one is the layer after: once a piece is a single command, the pattern is matched against **its spelling**, and the same command has many spellings. Probed with `_deny_match(cmd, ["git push*"])` on `arena` @ `00355fd` (True = denied):

| command | denied? |
| --- | --- |
| `git push` | True |
| `git -C . push` | **False** |
| `git -c x=y push` | **False** |
| `git --no-pager push` | **False** |
| `/usr/bin/git push` | **False** |
| `"git" push` | **False** |
| `\git push` | **False** |
| `sudo git push` | **False** |
| `xargs git push` | **False** |
| `if true; then git push; fi` | **False** |
| `while true; do git push; done` | **False** |
| `env git push`, `command git push`, `nohup git push`, `time git push`, `exec git push`, `! git push`, `{ git push; }`, `( git push )` | True (already handled) |

`git -C <dir>` is how an agent runs git on a worktree it is not standing in — the *usual* spelling, not a trick.

## Bugs

**29 — `git`'s global options in front of the subcommand.** `-C <dir>`, `-c k=v`, `--no-pager`, `--git-dir=…`, `--work-tree=…`, `--namespace=…`, `-P`/`-p`, `--exec-path[=…]`, `--bare`, `--no-replace-objects`, `--literal-pathspecs` (and the `--opt value` forms of those that take one) sit between `git` and `push`; the pattern `git push*` is anchored at the start of the piece. Fix: before matching, rewrite a piece whose command word is `git` into `git <subcommand> <rest>` by skipping the global options (a small table of which take a separate value: `-C`, `-c`, `--git-dir`, `--work-tree`, `--namespace`, `--exec-path` without `=`, `--config-env`), and match the pattern against *both* the original and the normalised spelling.

**30 — the command word spelled with a directory, a quote or a backslash.** `/usr/bin/git`, `./git`, `"git"`, `'git'`, `\git`, `g"i"t`. A shell strips these before it looks the command up. Fix: normalise the command word of a piece (`os.path.basename` of the unquoted word; the quotes and backslashes removed the way the shell removes them — the 198 reader already knows how) before matching, again against both spellings; a pattern that itself holds a path (`/usr/bin/git push*`) still matches as written.

**31 — wrapper words that run their argument.** `sudo`, `doas`, `xargs`, `nice`, `ionice`, `setsid`, `stdbuf`, `chroot`… `COMMAND_WRAPPERS` (shared with `is_pytest_command`) knows `env`, `command`, `nohup`, `time`, `exec`, `timeout` but not `sudo` or `xargs`. Fix: a **separate** deny-only wrapper list (so the pytest recogniser, which reads the shared list, is not changed) with the same unwrapping, each wrapper's own options skipped (`sudo -u root`, `xargs -n1`, `nice -n 5`).

**32 — the command position after a shell keyword.** In `if true; then git push; fi` the piece after `;` is `then git push`; `do`, `else`, `elif`, `if`, `while`, `until` and `!` before the command word do the same. The piece starts with `then`, so the anchored pattern never sees `git`. Fix: drop leading reserved words (`if then elif else fi do done while until time ! { }`, and a leading `(`) from a piece's start before matching; `fi` / `done` / `}` alone are not commands.

## Not in scope (cannot be decided statically; stays a documented limit)

`echo 'git push' | sh`, `sh -c "$(…)"`, here-strings, `python -c "import os; os.system('git push')"`: the text comes from stdin or from a computed value. Say it in the deny-list paragraph of the runbook: the matcher is a pattern filter on the command's *spelling*, not a sandbox.

## Tests

`tests_bugfix/test_contest_deny_spellings_205.py`, parametrised, each case failing on the old code:
29 — the five option spellings above plus `git --git-dir=x --work-tree=y push`, `git -C a -C b push`, `git -c a=b -c c=d push`; 30 — the five command-word spellings and a pattern with a path still matching as written; 31 — `sudo git push`, `sudo -u root git push`, `xargs git push`, `xargs -n1 git push`, `nice -n 5 git push`; 32 — `then`/`do`/`else`/`elif`/`if`/`while`/`until` lines.
**The false-positive side is the same size:** `git -C wt status`, `git -c x=y log`, `git --no-pager diff`, `git pushd` (not a git subcommand), `echo git push`, `"git push"` as a quoted argument, `sudo ls`, `xargs echo`, `echo then git push` are all *not* denied; the other default patterns (`git reset --hard*`, …) see through a directory and a quote too.

## Review material

Review tests (held by the operator, outside the repo): `test_contest_deny_git_options_and_wrappers` (29 failing cases here), `test_contest_deny_newline_and_keywords`.
The operator keeps these outside the repo on purpose: a round that can read the reviewer's fix would copy it, and the competition would measure nothing.

## Acceptance

```bash
python3 -m pytest tests_bugfix -k "205" -q
python3 -m pytest tests -k "policy and deny" -q
python3 -m pytest .smoke_tests/ -q
```
