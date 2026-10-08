# 213 — `deny_commands` misses a `$(…)` in an unquoted heredoc body when the body has an apostrophe and the heredoc is never closed

**Status:** queued
**Origin:** round 198 judging (cross matrix: agnes-2-5-flash's `test_table_driven` on the landed ideal `67b8311`), reproduced by hand on the ideal
**Severity:** MEDIUM (a denied command passes: fail-open)
**File:** tools/contest/policy.py
**Symbol:** _shell_pieces
**Round:** 213
**Size:** XS

## Why

Ticket 198 made the shell reader treat a heredoc body as data, with one exception: in an unquoted heredoc the `$(…)` and backtick bodies are still commands. That exception is lost when the body holds an apostrophe and the heredoc has no closing line:

```
_deny_match("cat <<EOF\ndon't\nEOF$(git push)", ["git push*"])   → None        (expected "git push*")
_deny_match("cat <<EOF\ndon\nEOF$(git push)",   ["git push*"])   → "git push*"  (no apostrophe: right)
_deny_match("cat <<EOF\n$(git push)",           ["git push*"])   → "git push*"  (no apostrophe: right)
_deny_match("cat <<EOF\ndon't\nEOF\ngit push",  ["git push*"])   → "git push*"  (closed heredoc: right)
```

`EOF$(git push)` is not a terminator line (a terminator is the delimiter alone on its line), so the heredoc is still open there and the shell expands `$(git push)` in its body. The apostrophe in `don't` opens a single quote in the reader's scan of the body, and the rest of the text is swallowed — `git push` is never seen. The command is not denied; with the gate model's `once` it would run.

## What to do

The body of an **unquoted** heredoc is scanned for `$(…)` and backticks only; an apostrophe or a double quote in it is a plain character there, never a quote. A quoted heredoc's body is data and is not scanned at all. An unterminated heredoc is read to the end of the text, as the shell does.

## Tests

`tests_bugfix/test_policy_deny_heredoc_apostrophe_213.py`, failing on the old code: the four lines above with their expected results; the same with a double quote in place of the apostrophe; a quoted heredoc (`<<'EOF'`) with an apostrophe and `$(git push)` in the body is not denied only when it is closed; an `<<-EOF` form.

## Acceptance

```bash
python3 -m pytest tests_bugfix -k "213" -q
python3 -m pytest tests -k "policy or deny" -q
```
