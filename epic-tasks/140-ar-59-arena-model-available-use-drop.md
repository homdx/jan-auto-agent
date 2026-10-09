# AR-59 — `arena model available|use|drop`: the model list from Kilo, by name

**Status:** landed
**Severity:** LOW
**File:** tools/arena/models.py
**Symbol:** list_models, resolve_names, model_cache
**Round:** 140
**Size:** M
**Depends on:** AR-2 (profiles)
**Also touches:** tools/arena/cli.py, tests/test_arena_models.py

---

## Why

The model list of a round is typed by hand into `--models` or a profile, copied
from `kilo models` output, and a typo is found only when the round's intake fails.
AR-59 reads the list from Kilo, shows free models, and writes the chosen names into
the profile by name. No test is needed: models already checked are used as they are.
Keys and tests are AR-60.

---

## Where the list comes from

- Kilo only: `kilo models PROVIDER --verbose`, through the function
  `scripts/py_model_test.py` already has (`kilo_models`); import it, do not copy.
- Several providers in one call: `arena model available p1 p2 …`. No provider:
  every provider Kilo knows.
- Free = price 0 from the verbose metadata **or** a name ending in `:free` / `-free`.
  Reuse `free_from_kilo`. A price 0 without the suffix is shown `maybe`
  (Kilo's 0 can mean "unknown").
- Kilo failing (missing binary, error exit): one refusal line, exit 2, no traceback.
- The kilo binary: `[contest] kilo_bin` when an ini sets it, else `kilo` on `PATH`,
  else the newest `~/.vscode/extensions/kilocode.kilo-code-*/bin/kilo`.
- `kilo models` (no provider) lists every model of every provider Kilo knows
  (checked on Kilo 7.6.2: 1121 rows); the provider is the part before the first `/`.

## Without any ini file

`available` must work in a fresh checkout with **no `contest.ini` and no
`contest.local.ini`**:

- the model list comes from Kilo alone; nothing in the list needs an ini;
- `model_cache_days` falls back to 7, the `.arena/` folder is created on first write;
- `IN-PROFILE` is empty (no profiles); no warning about the missing files;
- the judge check of `use` has no roster to read, so it refuses nothing.

`use -p NAME` with no ini creates `contest.local.ini` holding only
`[arena.profile.NAME]` and its `models =` line (the file is git-ignored); without
`-p` and with no `[arena] profile =` the profile is `default`. `drop` with no ini
(no profile) is a refusal naming the missing profile.

## The cache — the same pattern as `tools/contest/context_memory.py` (KC-67)

- One file `.arena/models-cache.json`: a JSON list of records
  `{provider, model, free, ctx, at}` (`at` = epoch seconds when Kilo listed it).
- Records older than `model_cache_days` are dropped **on every read and every write**,
  as `context_memory.load` / `add` do. Default 7 days, key
  `[arena] model_cache_days` in `contest.local.ini`; 0 or junk = no cache.
- Written atomically: `tempfile.mkstemp` next to the file + `os.replace`.
- Fail-open: a missing, unreadable or non-list file is an empty cache, never an error.
- `available` refreshes the listed providers from Kilo and merges into the cache;
  `use` / `drop` check names against the cache and call Kilo only when the cache
  has no record for a name.

## Commands

### `arena model available [PROVIDER…] [--free] [--search TEXT]`

- Columns `NAME PROVIDER FREE CTX IN-PROFILE LAST-TEST`, sorted by provider, name.
  `FREE` is `yes` / `maybe` / `no`. `LAST-TEST` stays empty until AR-60.
- `--free`: `yes` and `maybe` rows only. `--search`: case-insensitive substring.
- Nothing matches: one stderr line, exit 3. `-o json` through `emit`.

### `arena model use NAME[,NAME…] -p PROFILE [-y]`

- Comma list, same shape as `--models`; a repeated name is one more agent
  (`-var1`, `-var2`). The order is kept.
- Writes the profile's `models =` in `contest.local.ini` only — never `contest.ini`,
  never `agents_128k.ini`.
- **Every name is checked before anything is written.** Any name that is not an exact
  model name is a refusal, exit 2, file unchanged, with a hint:
  - a missing suffix: `arena: 'hy3' is not a model — did you mean: hy3:free`;
  - a typo: `arena: 'devstral2' is not a model — did you mean: devstral-2:free`
    (up to 3 names, `difflib.get_close_matches`);
  - nothing close: `arena: 'xyz' is not a model (arena model available --search xyz)`.
  No name is ever substituted on its own, also not with `-y`.
- A model with no test on record is fine: no warning, no refusal.
- A judge model (gate / reviewer / writer in the roster) is refused.
- Prints `models =` before → after and asks; `-y` applies without asking.

### `arena model drop NAME[,NAME…] -p PROFILE [-y]`

- Removes every copy of each name from the profile.
- An unknown name: the same refusal and hint as `use`.
- A name not in the profile: refusal naming the profile's models.
- Dropping every model: refusal `arena: profile 'default' would be empty — 1 model
  must stay`, file unchanged.

---

## Tests (`tests/test_arena_models.py`)

Kilo is faked through a module seam (`models.KILO_LIST`); no network, no kilo binary.

1. `available` lists all; `--free` keeps `x:free`, `y-free` and a price-0 `z` (as `maybe`),
   drops a paid `w`; two providers in one call.
2. `--search` filters; nothing found → exit 3.
3. `use a:free,a:free,b-free` writes `models = a:free,a:free,b-free` into
   `contest.local.ini`; `contest.ini` bytes unchanged.
4. `use hy3` (only `hy3:free` exists) → exit 2, hint names `hy3:free`, file unchanged,
   also with `-y`.
5. A typo → exit 2 with the close name; nothing close → the `--search` hint.
6. One bad name among good ones → nothing written.
7. A judge model is refused.
8. `drop` removes all copies; unknown name → hint; dropping the last model → refusal.
9. Cache: a record 8 days old is gone after a read and after a write; the write is
   atomic (no `.tmp` left); a broken cache file is an empty cache.
10. Kilo failing → one refusal line, exit 2.
11. `-o json` parses.
12. No `contest.ini`, no `contest.local.ini`: `available` lists, `--free` filters, exit 0,
    nothing on stderr; `use a:free -p p1` then creates `contest.local.ini` with
    `[arena.profile.p1]` / `models = a:free` and nothing else; `contest.ini` is not created.

## Rules

- No real provider/model names in committed files; tests use placeholders.
- Never read or print `agents_128k.ini`; write only `contest.local.ini`.
- Every refusal is one line through `output.refuse`; broken input never a traceback.
- Commit subject starts with the round number.
