# AR-2 — Turn named arena profiles into the contest run flag line

**Status:** open
**Severity:** LOW
**File:** tools/arena/profile.py
**Symbol:** load_profiles, profile_flags, KNOWN_KEYS
**Round:** 137
**Size:** S
**Also touches:** tools/arena/cli.py, contest.ini, tests/test_arena_profile.py

---

## Why

The long `python3 -m tools.contest run …` line is the same every round. AR-2 keeps it once, as a named `[arena.profile.NAME]` section, and turns the selected profile into the flag list for `tools.contest run`. AR-1 (landed, round 136) already registers `arena profile list` and `arena profile view` with no handler; AR-2 gives them one. The old `tools.contest` command does not change.

---

## What AR-1 already gives you (read it before writing)

- `tools/arena/cli.py`:
  - `Verb` dataclass: `help`, `ticket`, `add_arguments(parser) | None`, `handler(args) -> int | None`.
  - `OBJECTS["profile"].verbs["list"]` and `["view"]` exist with `ticket="AR-2"` and no handler. AR-2 sets their `handler` (and `add_arguments` for `view`); it does not add new objects or change other verbs.
  - `main` parses the globals onto `args`: `args.profile` (`-p`/`--profile`, default `None`), `args.output` (`table`|`json`), `args.yes`, `args.passthrough`.
  - Exit codes: `EXIT_OK = 0`, `EXIT_FAILED = 1`, `EXIT_USAGE = 2`, `EXIT_NOTHING = 3`, `EXIT_NO_READY = 4`.
- `tools/arena/output.py`:
  - `emit(rows, columns, fmt)` masks and scrubs every row itself; pass `args.output` as `fmt`.
  - `refuse(msg) -> 2` prints one scrubbed stderr line `arena: <msg>`.
  - `mask`, `scrub`, `SECRET_WORDS`.
- `tools/contest/roster.py`: `_new_parser()` (no interpolation, inline comments after `;` and `#`) and `LOCAL_FILENAME = "contest.local.ini"`. Roster ignores sections it does not know, so `[arena]` and `[arena.profile.*]` sections do not disturb it.

---

## What to build

### `tools/arena/profile.py`

- `KNOWN_KEYS`: the one registry of accepted profile keys, mapped to their flag (or `None` when the key is arena-owned data, not a flag):
  - `models` → `--models`
  - `legs` → `--legs`
  - `max_parallel` → `--max-parallel`
  - `backend` → `--backend`
  - `provider` → `--provider`
  - `variant` → `--variant`
  - `extra` → raw arguments, `shlex.split`, appended after the mapped flags
  - `branch` → `None` (used by AR-3/AR-8)
  - `trailer` → `None` (used by AR-8)
  - `base` is not a key: a profile holding it is refused like any unknown key.
- `ProfileError(Exception)`: raised for every refusal below; its message is the one line the CLI prints.
- `load_profiles(repo: Path) -> tuple[dict[str, dict[str, str]], str]` — the profiles by name and the active profile name.
  - Read `repo / "contest.ini"`, then `repo / roster.LOCAL_FILENAME`, into one `roster._new_parser()`; the local file wins key by key, a key only in the committed file survives. A missing file is skipped.
  - Only sections named `arena.profile.<name>` are profiles.
  - The active name is `[arena] profile =`, else `default`.
  - An unknown key raises `ProfileError` naming the section and the key: `[arena.profile.default] unknown key 'colour'`.
  - Never read `agents_128k.ini` or any other file.
- `profile_flags(profile: dict[str, str], overrides: dict[str, str] | None = None) -> list[str]`:
  - `overrides` replace profile values key by key.
  - Order is fixed: `models`, `legs`, `max_parallel`, `backend`, `provider`, `variant`, then the `extra` words in their own order.
  - A key whose value is empty (after `strip()`) produces nothing.
  - `branch` and `trailer` produce nothing.
  - `--base` or `--ticket` among the `extra` words (also as `--base=X`) raises `ProfileError("--base/--ticket are set by arena, not by a profile")`.

### `tools/arena/cli.py`

- The repo is the checkout `arena` runs from: the directory that holds `tools/` (the same root the launcher puts on `sys.path`). Tests pass a `tmp_path` repo through a module-level seam (for example `cli.REPO_ROOT`, patched with `monkeypatch`); do not read the current working directory.
- `arena profile list`:
  - columns `NAME ACTIVE MODELS LEGS BRANCH`, one row per profile, sorted by name;
  - `ACTIVE` is `*` for the active profile (or the one `-p` names), empty otherwise;
  - `MODELS` is a count, not the list: `5 agents (3 models)` for five comma-separated entries with three distinct values; empty when the profile has no `models`;
  - no profile at all: one stderr line `arena: no [arena.profile.*] section in contest.ini or contest.local.ini` and exit `EXIT_NOTHING` (3). This is not a refusal, so it does not go through `refuse`'s exit 2.
- `arena profile view [NAME]`:
  - `NAME` defaults to `-p`, then the active profile;
  - rows `KEY VALUE`, one per key the profile sets, then one row `flags` holding `shlex.join(profile_flags(profile))`;
  - goes through `emit`, so a key containing a secret word shows `***`.
- `-p NAME` naming no profile is a refusal (exit 2) that lists the known names: `unknown profile 'x' (known: default, fast)`.
- Every `ProfileError` reaches the operator as `refuse(str(err))`, exit 2.
- AR-1 behaviour does not change: usage errors, passthrough, masking, the other verbs' "not implemented yet (AR-N)".

### `contest.ini`

- Append a commented-out example block, every line starting with `#`:

  ```ini
  # [arena]
  # profile = default
  #
  # [arena.profile.default]
  # models = provider/model-a,provider/model-a,provider/model-b
  # legs = 1              ; empty = no --legs
  # max_parallel = 4
  # branch = arena
  # extra = --fresh
  # trailer = Co-Authored-By: ...
  ```

- Placeholder model ids only. Do not touch the existing sections or their values.

### `tests/test_arena_profile.py`

One-line docstring. Throw-away `contest.ini` / `contest.local.ini` under `tmp_path`; no `kilo`, no model, no real `contest-out/`. The cases:

1. A local key overrides the same committed key; a key only in the committed file survives.
2. `profile_flags` order is exactly `models, legs, max_parallel, backend, provider, variant`, then `extra`.
3. `legs =` (empty) gives no `--legs`.
4. Override `{"legs": "3"}` replaces `legs = 1`.
5. `extra = --base X` and `extra = --ticket=5` are refused.
6. `colour = red` in `[arena.profile.default]` is refused, and the message names both the section and the key.
7. `arena profile view` on a profile with `extra = --x api_key=sekrit` and `trailer = token=sekrit2` never prints `sekrit` or `sekrit2` (both in `table` and `-o json`).
8. No profile sections: `arena profile list` exits 3 with one stderr line naming `[arena.profile.*]`.
9. `legs = 2   ; two legs` reads as `2`, and a value holding `%` does not raise.
10. `-p nosuch` is a refusal listing the known profile names.
11. `profile list` shows `5 agents (3 models)` and marks the active profile.

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
python3 -m pytest tests/test_arena_profile.py tests/test_arena_cli.py -q
```

```bash
python3 -m pytest tests -n 8 -q
```

```bash
python3 -m pytest tests_bugfix -n 8 -q
```

All eleven cases above pass, `tests/test_arena_cli.py` (AR-1) stays green unchanged, and `contest-bench/136/acceptance_136.py` still passes.

---

## Rules

- No change under `tools/contest/`; import `roster._new_parser` and `roster.LOCAL_FILENAME`, do not copy them.
- `KNOWN_KEYS` is the single registry of profile keys.
- `base` is never a profile key; `--base` and `--ticket` are never produced, and refused when `extra` holds them.
- Never read or print `agents_128k.ini`.
- No real provider or model names in `contest.ini`; placeholders only.
- Keep AR-1's one-line refusals, masking, passthrough and exit codes.
- Do not change `tests/test_arena_cli.py`.
- Python 3.10+, four-space indent, type hints on public functions, explanatory comments in the style of `tools/arena/cli.py`.
- Tests wait on events, never on sleeps or tight timeouts.
- No unrelated repository changes.
- Commit subject starts with the round number: `137: …`.
