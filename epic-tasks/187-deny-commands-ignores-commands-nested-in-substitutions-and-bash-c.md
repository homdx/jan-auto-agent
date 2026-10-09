# 187 — `deny_commands` does not look inside `$(…)`, backticks, `<(…)`, `bash -c` or `eval`

**Status:** landed
**Origin:** `arena-bugs-sonet5` ticket 181 — extends 164 (the per-piece match); defence in depth, unproven against a live Kilo
**Severity:** HIGH
**File:** tools/contest/policy.py
**Symbol:** _deny_candidates, _deny_match
**Round:** 187
**Size:** S
**Also touches:** tests/test_contest_deny_commands_nested.py

## The bug

`_deny_candidates` split a command on `&&`, `||`, `;`, `|`, `&` and peeled wrappers, but never looked at a command line nested in another. With `deny_commands = git push*`:

```
echo $(git push)            -> not denied
echo `git push`             -> not denied
diff <(git push) a          -> not denied
bash -c 'git push'          -> not denied
sh -lc "cd x && git push"   -> not denied
eval 'git push'             -> not denied
```

An agent could run any denied command by wrapping it, defeating the mechanical layer that runs before the gate model.

## Fix

`_deny_candidates` now also expands the bodies of `$(…)`, `<(…)`, `>(…)` and backticks (not inside single quotes, where the shell treats them as text), the `-c` script of `sh/bash/zsh/dash/ksh/ash` (also `-lc`, behind wrappers) and `eval …`, recursively to depth 4. Malformed input (unclosed quotes/parens) is tolerated.

## Tests

`tests/test_contest_deny_commands_nested.py`: nine wrapped forms are denied; single-quoted text, harmless inner commands and malformed lines are not (and do not raise).

## Acceptance

```bash
python3 -m pytest tests/test_contest_deny_commands_nested.py -q -n 0
python3 scripts/sync_test_tiers.py --check
```

**Landed:** as written. Limits kept: a line nested deeper than four levels (`_MAX_NESTING`) is not looked into, and `base64 -d | sh`, a script file or `python -c` are not shell syntax this layer reads — it is a second line behind Kilo's own `permission.bash` deny rules, not a sandbox. Fuzzed with 20 000 random lines of `$(`, backticks, quotes and separators: no exception, the worst call 2 ms; 5 000 substitutions in one line: 0.16 s.
