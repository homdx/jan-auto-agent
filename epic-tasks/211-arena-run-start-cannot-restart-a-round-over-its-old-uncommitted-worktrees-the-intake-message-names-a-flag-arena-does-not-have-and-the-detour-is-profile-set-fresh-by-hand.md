# 211 — arena cannot restart a round over its old uncommitted worktrees: the intake message names a flag `arena run start` does not have, and the way out is a hand-run `contest` call or a profile edit and its undo

**Status:** queued
**Origin:** operator session, round 198 relaunch on the new profile `p198` (2026-10-08); the operator asked "why was it started like this and not through arena — is that a bug?"
**Severity:** MEDIUM (a missing arena path; the first answer given to the operator was a bypass of arena)
**File:** tools/arena/cli.py
**Symbol:** run start (the `--fresh-ticket` option, `_cmd_run_start`), tools/arena/profile.py `KNOWN_KEYS["fresh"]`
**Round:** 211
**Size:** S
**Also touches:** tools/arena/rounds.py (the intake-refusal text relayed to the operator), docs/arena/EPIC-ARENA.md (AR-3, the "Never pass `--fresh` to `run` implicitly" line, and the `--fresh-ticket` section), tests/, tests_bugfix/

## Why

A round is started again on a new profile while the old round's worktrees `rounds/NN-<agent>` still hold uncommitted work (typical: the first run was stopped, or its entries were never committed). `tools.contest run` then stops at intake and says:

```
rounds/198-agnes-2-5-flash: M tools/contest/policy.py
pass --fresh to discard it, or --resume …
```

`arena run start` has **no way to follow that advice**:

- `--fresh-ticket` is not `--fresh`. It re-creates the *ticket* state; it does not discard the worktrees (probed: the second launch stopped at the same intake).
- `--fresh` is a **profile key** (`profile.py:44`, `"fresh": "--fresh"`, a switch in `SWITCH_FLAGS`), deliberately never an `arena run start` flag: EPIC-ARENA says "Never pass `--fresh` to `run` implicitly", because `--fresh` erases the old entries' uncommitted work.
- The rule is right; the missing part is an **explicit, one-shot, confirmed** way to say it. As it stands the operator must (a) edit the profile, (b) start, (c) edit the profile back — three commands, and a forgotten step (c) leaves a standing `fresh=yes` in the profile that wipes the work of the *next* start on it without a word. The first answer given to the operator was worse: a direct `python3 -m tools.contest run --ticket 198 --base arena-round/198 --fresh --models … --max-parallel 15`, which skips arena's profile, base choice and round record entirely.

So this is not a bug in the rule, it is a gap: the intake message advertises a flag that the arena front door cannot pass.

## What was run (the log of the session)

Round 198 had run once on profile `p151` (17 agents). Two entrants were dead (`kenary/mimo-v2-5:free` → "model not found", `bynara/space-bunny-alpha-bynara` → "The requested model is not available"), so a new profile with 13 models was made, and the old results were backed up.

```bash
# 1. a new profile with the chosen models (creates p198; "before: none")
python3 -m tools.arena -p p198 model use -y sensenova123/sensenova-6.8-flash-lite,sensenova123/sensenova-6.7-flash-lite,kenary/agnes-2-5-flash:free,kenary/step-3-7-flash:free,zai/glm-4.7-flash,publicai/aisingapore/Gemma-SEA-LION-v4-27B-IT,publicai/swiss-ai/apertus-70b-instruct,publicai/swiss-ai/apertus-v1.5-8b-thinking,publicai/aisingapore/Qwen-SEA-LION-v4-32B-IT,publicai/swiss-ai/apertus-8b-instruct,publicai/swiss-ai/apertus-v1.5-70b,publicai/swiss-ai/apertus-v1.5-70b-thinking,publicai/swiss-ai/apertus-v1.5-8b

# 2. the profile's other keys — a second command, `model use` does not take them
python3 -m tools.arena profile set -y p198 max_parallel=15 branch=arena

# 3. back up the old round's results — by hand, arena has no command for it
cp -r contest-out/198 contest-out/198.p151-backup

# 4. start — stops at intake, old worktrees hold uncommitted work
python3 -m tools.arena -p p198 run start 198 --fresh-ticket

# 5. what was suggested at that point (a BYPASS of arena — the thing this ticket removes)
python3 -m tools.contest run --ticket 198 --base arena-round/198 --fresh --models <13 models> --max-parallel 15
```

## How it has to be done through the arena CLI today

```bash
python3 -m tools.arena profile set -y p198 fresh=yes
python3 -m tools.arena -p p198 run start 198 --fresh-ticket
python3 -m tools.arena profile set -y p198 fresh=      # remove the switch again (KEY= removes the key)
```

## What is missing

1. **No one-shot discard at start.** `arena run start NN` has no `--fresh` (or `--discard-old`), so the three-command detour above is the only arena-conformant path.
2. **The refusal advertises what arena cannot do.** The intake text is `tools.contest`'s own and is relayed as it is; through arena it should say what to run *in arena*.
3. **A standing `fresh=yes` is silent.** `arena run start` on a profile with `fresh` on starts, and wipes, without naming what it is about to erase.
4. **No listing of what would be lost.** Before discarding, the operator should see which `rounds/NN-*` worktrees are dirty and how many files; today they run `git status` in each by hand.
5. **No arena command to keep the old results.** The `cp -r contest-out/198 …` backup in step 3 is manual and easy to skip.
6. `arena model use` creates a profile with only `models`; `max_parallel` and `branch` need a second command (`profile set` refuses `models`, so the two cannot be merged by the operator).

## Decisions (so the round does not have to ask)

- `arena run start NN --fresh` is added — **explicit, one start only, never stored in the profile**. Without a terminal it is refused unless `-y` is given. The EPIC rule stays: no implicit `--fresh`; the flag is a spoken word on the command line, not a default.
- Before it discards, `run start --fresh` prints the dirty worktrees (path, `M`/`??` counts) and asks `discard N worktrees? [y/N]`; `-y` skips the question, the list is still printed.
- It also copies the old `contest-out/NN` to `contest-out/NN.<profile>-<UTC stamp>` first and prints where (an existing folder of that name is never overwritten). `--no-backup` turns the copy off.
- If the profile itself carries `fresh=yes`, `run start` prints one line — "profile NAME has fresh=yes: this start discards uncommitted work" — and takes the same confirmation. (A profile key stays valid for scripted rounds; it just stops being silent.)
- The relayed intake refusal gets the arena wording: "rerun with `arena run start NN --fresh` to discard it, or `arena run rerun` / `--resume` to continue it". The contest's own text is unchanged.
- `arena model use` gains `--set KEY=VALUE` (repeatable) for the profile's other keys, so a profile is made in one command. `models` stays refused in `profile set`.

## Tests

`tests_bugfix/test_arena_run_start_fresh_211.py`: (a) `run start NN --fresh -y` against a dirty `rounds/NN-x` worktree discards it, backs up `contest-out/NN`, and passes `--fresh` to the runner exactly once; (b) without `-y` and without a terminal it refuses and touches nothing; (c) a clean start passes no `--fresh`; (d) the profile file is unchanged after a `--fresh` start; (e) a profile with `fresh=yes` prints the warning line and asks; (f) the intake refusal shown to the operator names `arena run start NN --fresh`; (g) the backup folder never overwrites an existing one; (h) `model use --set max_parallel=15 branch=arena` writes the keys in the same call.

## Acceptance

```bash
python3 -m pytest tests_bugfix -k "211" -q
python3 -m pytest tests -k "arena" -q
python3 -m pytest .smoke_tests/ -q
```
