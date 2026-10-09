# 196 — arena: five inputs the parsers read wrongly (ini comment, NaN tokens, Unicode digits, revision-syntax branch, giant number)

**Status:** landed (already fixed on `arena` as bugs 183–187: `084b439`, `6bc171b`, `60896b6`; tests `tests_bugfix/*_183..187*.py`; no round needed)
**Origin:** operator review of the arena, bugs 201–205
**Severity:** HIGH (201), MEDIUM (203, 204), LOW (202), INSIGNIFICANT (205)
**File:** tools/arena/rounds.py
**Symbol:** _matches, round_alive, integration_branch, _total_tokens, _VIEW_ARG
**Round:** 196
**Size:** S
**Also touches:** tools/arena/cli.py (`_set_values`), tools/arena/tickets.py (`_ref_numbers`, ticket-number parsing), tests_bugfix/

## Bugs

**201 — `arena profile set` writes a value the ini reads back cut at a comment** (`cli.py::_set_values`).
`roster.py` reads the ini with `inline_comment_prefixes=(";", "#")`; `write_profile_keys` writes `key = value` as is; the preview is computed from the dict, not from the re-read file. `extra='--note "a ; b"'` (a `;` or `#` at the start or after whitespace) makes the profile unloadable ("No closing quotation"); `view`, `run start` and `set extra=` all refuse, only a hand edit of the file heals it.

**202 — `run view -o json` raises on NaN / Infinity tokens** (`rounds._total_tokens`).
`json.loads` accepts `NaN`/`Infinity`; `int(nan)` is `ValueError`, `int(inf)` is `OverflowError`. A traceback instead of the table.

**203 — a Unicode digit passes `isdigit()` and fails `int()`** (`rounds._matches`, `round_alive`, `tickets._ref_numbers`).
`'²'.isdigit()` is true, `int('²')` raises. Any process with `--ticket ²` on its command line (`_matches` reads every process's) or a branch `arena-round/²` breaks `run start/list/view/rerun` and `issue list`; `issue create` breaks on the look-alike branch.

**204 — `--branch main~1`, `main^`, `main@{1}`, `main^{commit}` pass the branch check** (`rounds.integration_branch`).
`rev-parse --verify` evaluates revision syntax, so the round is quietly built from the parent of the branch tip and `issue list` shows the old commit's tickets. (Reproduce first: the check asks for `refs/heads/{branch}`, confirm which of the four forms get through.)

**205 — a number of more than 4300 digits** (`run view`, `run rerun`, `issue view`; `_VIEW_ARG`, ticket-number regexes).
`\d+` has no bound and `int()` runs outside a `try` (the Python 3.11+ limit): a traceback instead of a one-line refusal.

## Fix

- 201: `_set_values` refuses a value matching `(?:^|\s)[;#]` in one line, before the preview and before `-y` writes. Also, `write_profile_keys` re-reads what it wrote through the real parser before `os.replace` and refuses (file untouched) when the profile would not load.
- 202: `_total_tokens` skips a value that is not finite (`math.isfinite`), counted as 0 like any odd value.
- 203: digits are ASCII only — `re.fullmatch(r"[0-9]+", s)` or `s.isascii() and s.isdigit()` at every place a number is taken out of a command line, a pid, a branch or a ticket name.
- 204: the branch name must be a plain branch: refuse a name `git check-ref-format --branch` rejects, or one with `~ ^ @ { :`, and resolve via the `refs/heads/` ref itself.
- 205: bound the digit runs (`{1,9}`) or catch `ValueError` around `int()`, so the answer is the existing one-line refusal.

## Tests

One file per bug in `tests_bugfix/` (`test_arena_<what>_196.py`): each fails on the old code and passes now. Cover: the reported value, `x #y`, `;z`, `#z`, no-break space for 201, and that `feature#1`, `--tag=a;b`, `'x;y'` still write and load; `nan`, `inf`, `-inf` in tokens; `²`, `٣`, `１` in a cmdline and a branch name; the four revision forms plus a real branch with `/`, `-`, `.`; a 5000-digit number.

## Acceptance

```bash
python3 -m pytest tests_bugfix -k "196" -q
python3 -m pytest .smoke_tests/ -q
```
