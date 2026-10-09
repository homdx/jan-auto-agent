# 172 — an abbreviated `--tick`/`--ou`/`--bas`/`--targ` after `--` slips past arena's owned-flag refusal

**Status:** landed
**Origin:** `arena-bugs-opus5` ticket 161 — merged with sonet5 ticket 168 (the exact spelling of --out/--target in a profile's extra)
**Severity:** HIGH
**File:** tools/arena/rounds.py
**Symbol:** _owned, build_run_line, build_rerun_line
**Round:** 172
**Size:** S
**Also touches:** tools/arena/profile.py (`OWNED_FLAGS`, `owned_flag`, `profile_flags`), tests/test_arena_profile.py, tests/test_arena_run_start.py, tests_bugfix/test_arena_owned_flag_abbrev_172.py

## The bug

`_owned(word)` refuses a passthrough word only when its flag part is *exactly* one of `_OWNED_FLAGS` (`--ticket`, `--base`, `--target`, `--out`). The child is `tools.contest run`, whose `argparse.ArgumentParser` keeps the default `allow_abbrev=True`, so any unambiguous prefix is the same flag, and the last occurrence wins.

Reproduced:

```
>>> rounds.build_run_line(7, {}, ['--tick', '9', '--ou', '/tmp/x'])
[..., 'run', '--ticket', '7', '--base', 'arena-round/7', '--tick', '9', '--ou', '/tmp/x']
```

The runner parses that as `--ticket 9 --out /tmp/x`: round 9's ticket runs on the base built for round 7, writes into a folder `run list`/`run view` never look at, and the lock file `.arena/locks/7.pid` names the wrong round. `profile.profile_flags` has the same hole for `extra = --tick 9` (its own comment admits "so would an abbreviation argparse accepts").

## Fix

- Treat a word as owned when it is `--x` / `--x=V` and `--x` is a prefix (length ≥ 3) of an owned flag that argparse would resolve to it — simplest: compare against the runner parser itself (`contest_cli._parser()` subparser `run`, `_option_string_actions` + `_get_option_tuples`), or refuse any prefix of an owned flag.
- Same check in `profile_flags` for `extra`.

## Tests

1. `build_run_line(7, {}, ['--tick', '9'])` raises `RoundError("--ticket is set by arena, not after --")`; same for `--ou`, `--bas=x`, `--targ`.
2. `build_rerun_line` refuses the same words.
3. A profile `extra = --tic 9` is a `ProfileError`.
4. `--backend x` and `--ba`-free words still pass (no false positive on `--backend`, `--models`).

## Review note

Re-verified against the real runner parser: `python3 -m tools.contest run --tick 9 --ba x` answers `ambiguous option: --ba could match --base, --backend`, so argparse prefix matching is on in `run`, and an unambiguous prefix (`--tick`, `--ou`, `--targ`, `--bas`) is accepted as the owned flag. Not a false positive.

## Merged with sonet5's 168 (the exact-spelling half)

`arena-bugs-sonet5` found the same hole from the other side: `profile._FORBIDDEN_FLAGS` was
`("--base", "--ticket")`, two shorter than `rounds._OWNED_FLAGS`, so a profile's `extra = --out DIR`
or `--target REPO` (exact spelling) reached the runner. The round then wrote `state.json`,
patches and `SUMMARY.md` into `DIR` while `run list`, `run view`, `run rerun`, `round_alive` and
`issue list` looked in `contest-out/NN` and reported "no round"; `--target` ran the round on another
repository than the one whose `epic-tasks/` and `arena-round/NN` ref arena had built. Reproduced on
the base: `profile_flags({"extra": "--out /tmp/elsewhere --target /other"})` returned the four words.

**Landed:** one tuple `profile.OWNED_FLAGS` (sonet5; `rounds._OWNED_FLAGS` is that same object, a test pins it),
and `profile.owned_flag(word, owned)` (opus5; it asks the runner's own parser, so an exact runner option
such as `--backend` is never taken for an abbreviation) used by the passthrough check and by `profile_flags`
for `extra`. A profile gets `--base/--ticket are set by arena, not by a profile` as before, any other owned
flag `<flag> is set by arena, not by a profile`.
