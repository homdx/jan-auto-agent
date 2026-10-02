# AR-61 + AR-62 — `arena profile set NAME KEY=VALUE`; one runner flag reaches the runner once

**Status:** open
**Severity:** LOW
**File:** tools/arena/profile.py
**Symbol:** set_keys, profile_flags, build_run_line
**Round:** 142
**Size:** S
**Also touches:** tools/arena/cli.py, tools/arena/rounds.py, tests/test_arena_profile_set.py

---

## Why

Round 139 was the first round started through arena. The models went into the profile with `arena model use … -p p139 -y`, but the parallelism did not: the operator had to open `contest.local.ini` and add `extra = --max-parallel 8` by hand, and then also gave `-- --max-parallel 8` on the command line. The runner got `--max-parallel 8 --max-parallel 8`. AR-61 gives the profile keys a command (a slice of AR-13), and AR-62 makes a flag that shows up twice reach the runner once.

---

## What AR-2, AR-3 and AR-59 already give you (read them before writing)

- `tools/arena/profile.py`:
  - `KNOWN_KEYS` is the one key registry, mapping key → runner flag (`max_parallel` → `--max-parallel`, `variant` → `--variant`; `None` means arena-owned data).
  - `load_profiles(repo)` reads `contest.ini` and then `contest.local.ini`; an unknown key is refused.
  - `profile_flags(profile, overrides)` gives the mapped flags in `KNOWN_KEYS` order, then the `extra` words.
- `tools/arena/models.py`:
  - `_with_models(text, name, value)` edits one key as text, keeping every other line and comment, and adds the section if it is missing.
  - `write_models(repo, name, value)` writes `contest.local.ini` atomically and keeps the file mode.
  - Both are the pattern for the one new writer: generalise them to any key. Do not add a second ini writer.
- `tools/arena/cli.py`:
  - `OBJECTS["profile"]` has `list` and `view`.
  - `split_passthrough(argv)` hands everything after `--` to `run start`.
- `tools/arena/rounds.py`:
  - `build_run_line(nn, prof, passthrough)` builds the child's argv: `… --base REF *profile_flags(prof) *passthrough`.
- The runner (`tools/contest/cli.py`):
  - `--max-parallel N` and `--variant NAME` take a value;
  - `--fresh` is a switch (no value).

---

## What to build

### AR-61 — `arena profile set NAME KEY=VALUE [KEY=VALUE …] [-y]`

- New key `fresh` in `KNOWN_KEYS`:
  - `fresh = yes|true|1` produces `--fresh` (no value);
  - `no|false|0` or empty produces nothing;
  - any other value is a refusal, both at `set` time and when the profile is read.
- Accepted keys: every key in `KNOWN_KEYS`. These are refused (exit 2, one line through `output.refuse`):
  - `models` (that is `arena model use`'s job);
  - a key not in `KNOWN_KEYS`;
  - a word without `=`;
  - `max_parallel` that is not a positive integer;
  - `base` or `ticket`.
- `KEY=` with an empty value removes that key's line from the section.
- Writes only `contest.local.ini`, `[arena.profile.NAME]`. Every other line, comment and key stays byte-for-byte as it was, including `models =` and an `api_key` in another section. A missing file or section is created.
- Without `-y` it prints the before → after flag line (`profile_flags`) and writes nothing (exit 0). With `-y` it writes and prints the new flag line.
- A refusal writes nothing.

### AR-62 — a flag once

- `build_run_line`: when a value flag (`--max-parallel`, `--variant`, `--legs`, `--backend`, `--provider`, `--models`) comes from both the profile (a key or `extra`) and the passthrough after `--`, only the passthrough one stays (the last word wins).
- The same applies within the profile: a key and the same flag in `extra` keep the `extra` one.
- `--fresh` appears at most once.
- Flags arena does not know about pass through untouched, in their order.
- Both `--flag V` and `--flag=V` count as the same flag.
- `profile view`'s flags line and the command `run start` prints are the same deduplicated list.

---

## Tests (`tests/test_arena_profile_set.py`)

Use a throw-away repo under `tmp_path` and patch `cli.REPO_ROOT`. No kilo, no model, no real round. `run start` is checked only through `build_run_line` / `profile view`, never by starting a child.

1. `profile set p max_parallel=8 -y` on a missing `contest.local.ini` creates the section, and `profile view p` shows `--max-parallel 8`.
2. A file with comments, `models =` and another section with an `api_key`: after `set p max_parallel=8 variant=high -y`, every other line is byte-for-byte the same.
3. Without `-y`: nothing is written, and stdout shows the new flags.
4. `fresh=yes` → `--fresh`, `fresh=no` → nothing, `fresh=maybe` → exit 2.
5. Refusals, each exit 2 with one line and the file unchanged: `models=x`, `nosuch=1`, `max_parallel=0`, `max_parallel=abc`, `base=x`, `max_parallel` (no `=`).
6. `max_parallel=` removes the line.
7. Profile `max_parallel = 3` with passthrough `--max-parallel 8`: the run line has `--max-parallel` once, with value `8`. The same with `extra = --max-parallel 8` and passthrough `--max-parallel=8`.
8. `extra = --fresh` plus key `fresh = yes`: `--fresh` once.
9. An unknown flag in `extra` (`--no-gate`) stays, in order.
10. The `--base` / `--ticket` refusals still hold (the AR-3 tests stay green).

Tier the file with `python3 scripts/sync_test_tiers.py`; never edit tier symlinks by hand.

---

## Acceptance

Run each command separately and in this order:

```bash
python3 scripts/sync_test_tiers.py
```

```bash
python3 scripts/sync_test_tiers.py --check
```

```bash
python3 -m pytest tests/test_arena_profile_set.py tests/test_arena_profile.py tests/test_arena_run_start.py tests/test_arena_models.py tests/test_arena_cli.py -q
```

```bash
python3 -m pytest tests -n 8 -q
```

```bash
python3 -m pytest tests_bugfix -n 8 -q
```

All cases above pass. `contest-bench/138/acceptance_138.py`, `contest-bench/140` and `contest-bench/141` still pass.

---

## Rules

- No change under `tools/contest/`.
- One ini writer: generalise `_with_models` / `write_models`; `arena model use` keeps its behaviour and its tests.
- **Never run a real round while working on this ticket** — no `python3 -m tools.contest run …` and no `arena run start …` outside a test with a stub child.
- Every refusal is one line through `output.refuse`, exit 2, and writes nothing.
- Never read or print `agents_128k.ini`. No real provider or model names in committed files. Never print an api key.
- Do not change existing tests except to add cases; `tests/test_arena_profile.py` and `tests/test_arena_run_start.py` stay green unchanged.
- Python 3.10+, four-space indent, type hints on public functions, explanatory comments in the style of `tools/arena/profile.py`.
- No unrelated repository changes.
- Commit subject starts with the round number: `142: …`.
