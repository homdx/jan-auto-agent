# 173 — `arena profile set` stores `legs=abc`, `backend=zzz`, crashes on `max_parallel=²`, and a newline in a value injects ini lines

**Status:** landed
**Origin:** `arena-bugs-sonet5` ticket 176
**Severity:** MEDIUM
**File:** tools/arena/cli.py
**Symbol:** _set_values, _profile_set
**Round:** 173
**Size:** S
**Also touches:** tests/test_arena_profile_set.py (+ a guard that `_BACKENDS` is the runner's own `--backend` choices)

## The bug

`_set_values` validates `max_parallel` (positive integer) and `fresh` and nothing else of what the runner parses. Reproduced with `arena profile set p1 KEY=VALUE -y`, every one exit 0 and written to `contest.local.ini`:

```
legs=abc   legs=0   legs=-1        # runner: --legs is type=int, intake refuses < 1
backend=zzz                        # runner: choices=["kilo", "openrouter"]
variant=a b                        # the model commands' own rule is [A-Za-z0-9_.-]+
extra=--a\n[evil]\nk=v             # a NEWLINE in the value
max_parallel=²                     # ValueError: invalid literal for int() — a traceback
```

- The first three kinds fail only later, at `arena run start`, with the runner's argparse error mapped to a plain failure; the operator has to find the bad key by hand.
- The newline case is worse: `models.write_profile_keys` edits the ini **as text**, so the value's second line becomes a new line of the file — `extra = --a` followed by a real `[evil]` section and `k=v`. `contest.local.ini` is the file that also holds the gate model's key.
- `"²".isdigit()` is True and `int("²")` raises: the "positive integer" check itself crashes instead of refusing.

## Fix

- Refuse a value with a control character (`ord < 32` or 127) — one line, nothing written.
- `legs` and `max_parallel`: ASCII `[1-9][0-9]*` only (that also removes the `²` crash); `backend` one of `kilo`/`openrouter`; `variant` matches `models._VARIANT_RE`.
- Everything the runner takes (`legs=3`, `backend=openrouter`, `variant=high`, `provider=my prov`) is stored as before.

## Tests

`tests/test_arena_profile_set.py`: each bad pair above exits 2 with one stderr line and leaves the file byte for byte; the good pairs still show their flags in `profile view`.

## Acceptance

```bash
python3 -m pytest tests/test_arena_profile_set.py tests/test_arena_profile.py -n 4 -q
```
