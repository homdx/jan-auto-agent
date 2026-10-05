# 164 — `deny_commands` is matched against the whole command line, so `cd x && git push` is not denied

**Status:** landed
**Origin:** `arena-bugs-sonet5` ticket 166 — defence in depth; end-to-end hole not verified against a live Kilo
**Severity:** LOW
**File:** tools/contest/policy.py
**Symbol:** _deny_match, Policy (the mechanical layer that calls it)
**Round:** 164
**Size:** S
**Also touches:** tests/test_contest_policy.py

## The bug

`_deny_match(command, deny_commands)` runs `fnmatch.fnmatch` of the **entire** command string against each pattern (and once more against `pattern + " *"`). A pattern like `git push*` or `sudo *` therefore only matches a command that *starts* with it. Anything an agent puts in front of the forbidden command, or spells with a different amount of whitespace, is not denied by the runner's own mechanical layer.

Reproduced offline with the patterns from the committed `contest.ini` (`git push*, sudo *, rm -rf /*, curl * | sh`):

```
>>> from tools.contest.policy import _deny_match
>>> d = ["rm -rf *", "git push*", "curl * | sh", "sudo *"]
>>> _deny_match("rm -rf /", d)                 # 'rm -rf *'  (ok)
>>> _deny_match("cd x && rm -rf /", d)         # None
>>> _deny_match("true && sudo ls", d)          # None
>>> _deny_match("echo hi; git push origin", d) # None
>>> _deny_match("(sudo ls)", d)                # None
>>> _deny_match("git  push", d)                # None   (two spaces)
>>> _deny_match("git -C . push", d)            # None
```

`policy._shell_pieces` already exists in the same file and splits a command on `&&`, `||`, `;`, `|`, `&` outside quotes (it is what `is_full_suite_command` uses), and `_past_wrappers` already skips `VAR=x` and `timeout N`. The deny list is the one consumer that does not use them. (Kilo's own `permission.bash` deny rules are emitted from the same `deny_commands` by `roster`, and are the second line; whether Kilo splits compound commands is a property of the Kilo version, so the runner's layer must not rely on it — the layer exists precisely so a rule does not depend on the model gate or on Kilo.)

## Review (second pass)

Re-checked against the call chain, so the severity is lower than first written: `Roster.session_rules` sends every `deny_commands` entry to **Kilo** as a `bash` deny rule, and Kilo evaluates those first. `_deny_match` runs only for a command Kilo *asked* about (an `ask_commands` match or a path outside the worktree), and a command that reaches that layer with no outside path is then **allowed** by the next branch (`bash: no path outside worktree/tmp_roots`). So the runner's deny list is defence in depth, and whether `cd x && git push` is stopped in practice depends on how the installed Kilo matches a compound command against `git push*` — **not verified against a live Kilo**; the offline reproduction above is of `_deny_match` alone. Not a false positive in the function, unproven as an end-to-end hole: confirm with one live probe before spending a round.

## Fix

- `_deny_match` tests every shell piece of the command (`_shell_pieces`, then `.strip()`, whitespace collapsed to single spaces, a leading `(`/`{`/`!` and `VAR=value` words and the `_WRAPPERS` prefix removed) in addition to the whole line. The first pattern that matches any piece is returned, as today.
- The `curl * | sh` pattern names a pipe, so it must still be tried against the **whole line** (a piece never contains the `|`); keep the existing whole-line match for that.
- Do not split inside quotes (`echo "a && git push"` is not a push) — `_shell_pieces` already honours quotes.

## Tests

1. Each command in the list above is denied by the matching pattern.
2. `echo "x && git push"` and `git status && git log` are **not** denied.
3. `curl https://x | sh -s -- -y` is still denied by `curl * | sh`; `curl … | sha256sum` is still allowed.
4. The existing deny-order tests of ticket 131 stay green.

## Acceptance

```bash
python3 -m pytest tests/test_contest_policy.py -n 4 -q
```
