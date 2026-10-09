# 201 — `deny_commands` reads the shell wrongly, second pass: `$'…'` quotes, a quoted `)` in a heredoc's `$( … )`, `# <<EOF` in a comment, `<<\EOF` and `<<E"O"F`

**Status:** landed
**Origin:** judging round 198 (black box `contest-bench/198`, then probes of the best entry), bugs 12–15; extends 198 — read it, 164, 187 and `131` before touching `_scan` / `_line_pieces`
**Severity:** HIGH (12, 13, 14: a denied command passes), LOW (15: a harmless line is refused)
**File:** tools/contest/policy.py
**Symbol:** _shell_pieces, _substitutions, _deny_candidates, _bash_c_bodies (the `bash -c` / `eval` unquoting), and — once 198 has landed — `_scan`, `_line_pieces`, `_subs_in_line`, `_heredoc_at`
**Round:** 201
**Size:** M
**Also touches:** tests_bugfix/

## Why

Round 198 asked for one shell reader that knows quotes, escapes, `$( … )` and heredocs. Six entries tried; the black box (43 cases, written from the ticket alone) separated them, and the entry that passed all 43 still failed four lines nobody listed. Three of the four are holes of the same kind as 8–11 (a denied command the reader cannot see); one is a false refusal. Bug 12 is on `HEAD` today, in every entry and in the base: the ticket never named it. Bugs 13–15 are in the best entry of round 198 (`sensenova-6-7-flash-lite-var1`, commit `ab0f7ef`); a fix for 198 has to be re-probed with them.

All probes use `deny_commands = ["git push*"]` and `tools.contest.policy._deny_match(command, deny)`.

## Bugs

**12 — `$'…'` (ANSI-C quoting) is read as a plain `'` quote** (`HEAD`; every entry of 198; `_shell_pieces`, `_substitutions` and the `bash -c` / `eval` unquoting).
Inside `$'…'` a backslash escapes, so `\'` is a quote *character*, not the end of the string. The reader ends the string at `\'`.
- `echo $'a\'b'; git push` → `None` (denied command passes). The reader ends the string at `\'`, reads `b`, then opens a quote at the next `'` that runs to the end of the line and hides `; git push`.
- `echo $'\''; git push` → `None` (same).
- `bash -c $'git push'` → `None` and `eval $'git push'` → `None`: the `bash -c` argument is unquoted as if `$'…'` were not there, so the body is `$'git push'`, which does not start with `git`.
- Controls that are right today: `echo $'x'; git push` → denied, `echo $'a;b' ; git push` → denied, `echo $"a"; git push` → denied. They stay.
`$"…"` needs nothing (it reads as `"…"`). Agents write `$'…\n…'` for multi-line strings, so this is not exotic.

**13 — a quoted `)` inside the `$( … )` of an *unquoted heredoc body* ends it early** (`_subs_in_line`, best entry of 198).
`cat <<EOF⏎$(echo ')'; git push)⏎EOF` → `None`; the same with `")"` → `None`. Top-level `echo "$(echo ')'; git push)"` is right (denied; this is bug 11, fixed). The cause: `_subs_in_line` reads a heredoc body line "where quotes are plain characters" — right for the body's own text (`don't`), wrong *inside* a `$(…)` opened in it, where the shell reads quotes again. The paren counter closes at the quoted `)` and the rest is lost. 198's text says an unquoted heredoc's `$(…)` bodies "must still be searched"; this is the same sentence with a `)` in it.

**14 — a `#` comment that contains `<<EOF` declares a heredoc** (`sensenova-6-7-flash-lite-var2` and `sensenova-6-8-flash-lite-var1` of round 198; the best entry is right).
`echo a # <<EOF⏎git push` → `None`. A `#` that starts a word (after whitespace or `;|&(`) opens a comment that runs to the end of the line: nothing in it is a heredoc, a quote or a substitution. These readers see `<<EOF` in the comment, take the next line (`git push`) as the heredoc body, and never find the delimiter; the push is read as data. Also check the converse: `echo a#b <<EOF`, `echo "#" <<EOF` and `echo $#` — a `#` that is not a comment start — must NOT start a comment (`cat <<EOF⏎x; git push⏎EOF` after any of them is still a heredoc, i.e. allowed).

**15 — a heredoc whose delimiter is quoted by a backslash or in parts is read as unquoted** (`_heredoc_at`, best entry of 198; LOW: a refusal where there should be an allow).
In the shell the delimiter word is quote-removed, and the body is expanded only if *no part* of the word was quoted. So `<<\EOF`, `<<E"O"F`, `<<'E'OF` all have the delimiter `EOF` and a body that is *data*.
- `cat <<\EOF⏎x; git push⏎EOF` → refused (should be `None`)
- `cat <<E"O"F⏎x; git push⏎EOF` → refused (should be `None`)
The reader takes the word up to `_HEREDOC_WORD_STOP` and compares the raw word (`E"O"F`, `\EOF`) with the delimiter line, so the body is never terminated; it falls back to "unterminated: search the rest as commands" — fail-closed, hence only LOW.

## Fix

- 12: in the one reader, an unquoted `$'` opens an ANSI-C string: `\` escapes the next character (so `\'` and `\\` do not close it), and the string ends at the first unescaped `'`. The `bash -c` / `eval` unquoting does the same (the body of `$'git push'` is `git push`; decode the common escapes `\n`, `\t`, `\\`, `\'`, `\"`, `\xHH`, `\NNN` at least well enough that a command word is not hidden — a body that cannot be decoded falls back to the raw text, fail closed).
- 13: a heredoc body is read as text *except* inside a `$(…)`/backtick opened in it; inside, the body is the normal shell reader (quotes, escapes, nesting). One reader, as 198 said, entered in two modes — "text" and "command".
- 14: in the main reader, `#` at the start of a word skips to the end of the line, outside quotes and not escaped, before the heredoc / quote / substitution tests. `#` inside a word (`a#b`), `$#`, `${#x}` are not comments.
- 15: read the delimiter word through quote removal (`\x` → `x`, `'…'`, `"…"` pieces joined), and mark the heredoc quoted when any piece was quoted.
Keep `_MAX_NESTING`, the public names, and the behaviour of 164, 187 and 198.

## Tests

One file per bug in `tests_bugfix/` (`test_policy_<what>_201.py`), each failing on `HEAD`'s reader (12) or on the 198 landing (13–15). Table-driven is welcome: (command, expected pattern or `None`).
- 12: the four lines above (denied); `echo $'a\\'` followed by `; git push` (the `\\` is a backslash, the `'` closes the string — denied); `echo $'x'; git push`, `echo $'a;b'; git push` (denied, the controls); `echo $'a; git push'` (allowed: all inside the string); `echo $'a\'; git push'` (allowed: `\'` is a quote character, the string ends at the last `'`); `bash -c $'git push'` and `eval $'git push'` (denied); `bash -c $'echo hi'` (allowed).
- 13: `cat <<EOF⏎$(echo ')'; git push)⏎EOF`, with `")"`, with a `\)` and with a backtick body `` `echo ')'; git push` `` (all denied); the same with `<<'EOF'` (data: allowed) and with a body that has `echo ')'` and no push (allowed).
- 14: `echo a # <<EOF⏎git push` (denied), `echo a #<<EOF⏎git push` (denied), `echo a;# x⏎git push` (denied), `echo a # ; git push` (allowed: the `;` is in the comment), `echo a#b; git push` (denied), `echo $#; git push` (denied), `echo "#" <<EOF⏎x; git push⏎EOF` (allowed).
- 15: `cat <<\EOF`, `cat <<E"O"F`, `cat <<'E'OF`, `cat <<"EOF"` each with `⏎x; git push⏎EOF` (allowed) and with `⏎x⏎EOF⏎git push` (denied).
- Plus 164's, 187's and 198's tests unchanged and green, and the black box: `python3 -m pytest contest-bench/198/acceptance_198.py -n 0 -q` on the new tree (43 passed).

## Review material

Review report `bugs-to-review/6.txt` has no bug 12–15 of this ticket: they were found by judging round 198 (`contest-bench/198`); the review's own deny tests are listed in ticket 205.
The operator keeps these outside the repo on purpose: a round that can read the reviewer's fix would copy it, and the competition would measure nothing.

## Acceptance

```bash
python3 -m pytest tests_bugfix -k "201 or 198 or deny or policy" -q
python3 -m pytest contest-bench/198/acceptance_198.py -n 0 -q
python3 -m pytest .smoke_tests/ -q
```

## Found while judging round 198

Round 198 itself: 6 entries committed, all with `tests_bugfix/` tests; the black box scored them 43/43 (`sensenova-6-7-flash-lite-var1`), 42/43 (`-6-7-var2`, `-6-8-var1`, `-6-8-var2`), 41/43 (`step-3-7-flash`), 34/43 (`agnes-2-5-flash`); the base 25/43. The cross matrix of the judge was empty for this round: see 202.
Four agents stopped without a commit: `mimo-v2-5` (a stale model id, the provider answered `model not found`) and three `bynara` models ("the model service connection was interrupted", all within 12 s): provider errors, not an arena bug — no ticket.
Evidence of the old reader at work: `agnes-2-5-flash` had 12 and `-6-7-var2` 2 of their `bash` calls refused with `deny_commands match: git push*` while they only *printed* `git push` inside a `python3 -c "…"` string to test the ticket's lines — bugs 9/10 in practice; both agents carried on, so this is the false-refusal side of 198, not a stop.
