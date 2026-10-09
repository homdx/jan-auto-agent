# 168 — `arena model drop` refuses to remove a model Kilo no longer lists

**Status:** landed
**Origin:** `arena-bugs-sonet5` ticket 174
**Severity:** LOW
**File:** tools/arena/models.py
**Symbol:** drop (calls `resolve_names`)
**Round:** 168
**Size:** XS
**Also touches:** tests/test_arena_models.py

## The bug

`drop` first runs `resolve_names(repo, entries)` — the check that every name is a model Kilo lists — and only then compares the names with the profile. That check is right for `use` (never write a typo into a profile) and wrong for `drop`, whose whole job is to take a name **out**. A model that a provider retired (the `:free` tier models of this repo's rosters come and go) is exactly the name an operator wants to drop, and it is exactly the name Kilo no longer lists:

```
contest.local.ini:  [arena.profile.default]  models = oldmodel:free,keep:free
Kilo lists:         keep:free
>>> models.drop(repo, Namespace(names="oldmodel:free", profile=None, yes=True))
arena: 'oldmodel:free' is not a model (arena model available --search oldmodel:free)
2        # contest.local.ini unchanged
```

The operator has to edit the ini by hand — the thing `arena model` exists to avoid. A missing `kilo` binary or an unreachable provider list (`KiloError`) has the same effect: `drop` of a name that is plainly in the profile needs Kilo to answer first.

## Fix

- `drop` does not call `resolve_names`. A name is droppable when it is in the profile (the existing "profile has no X" refusal stays); the `@variant` shape check (`_VARIANT_RE`) and the empty/whitespace check stay, as they need no Kilo.
- `use` is unchanged.

## Tests

1. A profile holding a name Kilo does not list: `drop` of that name succeeds and rewrites `models =`; the cache is not refreshed and Kilo is not called (`KILO_LIST` asserts it is never reached).
2. `drop` with `KILO_LIST` raising `KiloError` still works for a name in the profile.
3. `drop` of a name not in the profile, of the last model, and of a `bad variant@!` keep their refusals.

## Acceptance

```bash
python3 -m pytest tests/test_arena_models.py -n 4 -q
```
