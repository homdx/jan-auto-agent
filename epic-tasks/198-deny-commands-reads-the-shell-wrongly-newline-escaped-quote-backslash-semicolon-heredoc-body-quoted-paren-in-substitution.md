# 198 — `deny_commands` reads the shell wrongly: a newline, an escaped quote, `\;`, a heredoc body and a quoted `)` inside `$( … )`

**Status:** landed — round 198, winner sensenova-6-7-flash-lite, 67b8311 as-is
**Origin:** operator review of the contest policy, bugs 8–11; extends 164 (the per-piece match) and 187 (the substitutions) — read both and `131` before touching `_shell_pieces`
**Severity:** HIGH (8, 9, 11: a denied command passes), LOW (10: a harmless line is refused)
**File:** tools/contest/policy.py
**Symbol:** _shell_pieces, _substitutions, _deny_candidates, _deny_match
**Round:** 198
**Size:** M
**Also touches:** tests_bugfix/

## Why

`_deny_candidates` splits a command into pieces and matches each with `fnmatch`; a denied command (`git push*`) that the splitter cannot see is not denied. `_shell_pieces` and `_substitutions` each carry their own small shell reader, and each reader gets a different part of the shell wrong. All probes below were run on `HEAD` with `deny_commands = ["git push*"]`.

## Bugs

**8 — a newline does not separate commands** (`_shell_pieces`: separators are only `|&;`).
`echo hi⏎git push` → `None`: the denied command passes. A newline is a command separator in every shell, and agents write multi-line commands all the time.

**9 — an escaped quote opens a quote that is never closed** (`_shell_pieces`, `_substitutions`).
`echo \"; git push` → `None`. The backslash is not read in `_shell_pieces`, so `\"` starts a quote that runs to the end of the line, and the `;` after it is "inside quotes". (`_substitutions` does read `\`, the two readers disagree.)

**10 — `\;` and a heredoc body are read as shell** (`_shell_pieces`).
`echo \; git push` → matches `git push*`: an escaped `;` is an argument, not a separator, and the line is refused although it only prints text. `cat > f <<'EOF'⏎x; git push⏎EOF` → matches: the body of a heredoc is data for `cat`, and a `;` in it must not make a command. (`cat <<EOF⏎hi⏎EOF` is already right: `None`.) The two are one defect — the reader does not know what is data — but they fail the other way from 8/9: a refusal where there should be an allow. A heredoc whose delimiter is unquoted still runs `$( … )` and backticks inside its body, so those must still be searched.

**11 — a parenthesis inside quotes ends a `$( … )` early** (`_substitutions`).
`echo "$(printf ')' ; git push)"` → `None`. The depth count (`:970`) counts every `(` and `)` including the one inside `')'`, closes the body at the quoted `)` and loses `; git push)`.

## Fix

One small shell reader used by both functions, not two: it walks the text once and knows single quotes, double quotes, `\` escapes (outside single quotes), `$( … )`/backticks nesting with quotes inside, and heredocs (`<<`, `<<-`, quoted or not delimiter). From it:
- the separators are `|`, `&`, `;`, **newline** (and `\r\n`), outside quotes and not escaped;
- a heredoc body is not a command line; an unquoted heredoc's `$(…)`/backtick bodies still are;
- the substitution body ends at its own closing parenthesis, quotes and escapes counted.
Keep `_MAX_NESTING`, the behaviour of 164/187 and the public names. A reader that cannot make sense of the text (an unclosed quote) fails closed: the rest of the line is still one piece, never skipped.

## Tests

One file per bug in `tests_bugfix/` (`test_policy_<what>_198.py`), failing on the old code. Cases: `echo hi⏎git push`, the same with `\r\n`, a blank line between, a tab-indented second line; `echo \"; git push`, `echo \\"; git push` (the escaped backslash then a real quote), `echo '\"'; git push`; `echo \; git push` (allowed), `find . -exec ls {} \; ` (allowed), `echo a \; b; git push` (denied); the two heredoc forms (body with `;`, `<<-`, quoted and unquoted delimiter, a heredoc followed by `&& git push` on the delimiter's line, two heredocs on one line, a heredoc with no terminator — the rest of the text is then still searched, never skipped); lines that only look like a heredoc and stay commands (`cat <<< 'x'; git push`, `echo $((1 << 2)); git push`); an apostrophe in a heredoc body (`don't`) must not hide a `$(git push)` that follows the heredoc; an unquoted heredoc's body with `$(git push)` is a push, a quoted one (`<<'EOF'`) may be either, say which in the commit; `echo "$(printf ')' ; git push)"`, a `'`-quoted `(` and an escaped `\)`, a nested depth 3. Plus the whole of 164's and 187's existing tests, unchanged and green. A table-driven test of ~40 lines (command, expected pattern or `None`) is welcome: a model tends to fix the four named lines and break a fifth.

## Review material

Review report `bugs-to-review/6.txt`, bugs 8–11 (its regression test file is `test_contest_deny_pieces_escapes_and_newlines`, plus `test_contest_deny_newline_and_keywords` for the newline and keyword cases) — held by the operator, outside the repo. The keyword cases (`then git push`) are ticket 205, bug 32.
The operator keeps these outside the repo on purpose: a round that can read the reviewer's fix would copy it, and the competition would measure nothing.

## Acceptance

```bash
python3 -m pytest tests_bugfix -k "198 or deny or policy" -q
python3 -m pytest .smoke_tests/ -q
```
