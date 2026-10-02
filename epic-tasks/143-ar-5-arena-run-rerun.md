# AR-5 — `arena run rerun NN[.K] --failed | --agent NAME [--dead] [--dry-run]`

**Status:** landed
**Severity:** LOW
**File:** tools/arena/rounds.py
**Symbol:** run_rerun, revive_agents
**Round:** 143
**Size:** S
**Also touches:** tools/arena/cli.py, scripts/revive_round.py, tests/test_arena_run_rerun.py

---

## Why

Bringing a stopped round's agents back is two commands today — `scripts/revive_round.py NN`, then `python3 -m tools.contest run --ticket NN --base … --resume` with the round's flags typed again — and a round of legs needs `--out contest-out/NN.K --legs 1` on top. `DEAD` agents (no edit in the first-touch window; round 141: three of them) cannot be brought back at all. AR-5 makes it `arena run rerun 141 --failed --dead`.

---

## What AR-1 … AR-4, AR-61 and AR-62 already give you (read them before writing)

- `tools/arena/cli.py`:
  - `OBJECTS["run"].verbs["rerun"]` exists with `ticket="AR-5"` and no handler. AR-5 sets its `handler` and `add_arguments`; no other verb changes.
  - `REPO_ROOT` is the one repo seam; `_run_start` shows how a `run` verb loads the profile and hands `REPO_ROOT` to `rounds`.
  - `-y` is a global flag (`_late_globals(p, "y")` lets it come after the verb too).
  - Exit codes: `EXIT_OK = 0`, `EXIT_FAILED = 1`, `EXIT_USAGE = 2`, `EXIT_NOTHING = 3`, `EXIT_NO_READY = 4`.
- `tools/arena/rounds.py`:
  - `load_config(repo)`, `round_folder(repo, config, nn, leg=None)` (a bare `NN` is its last leg), `round_alive(repo, nn, PROC_ROOT)`.
  - `REF_PREFIX` (`arena-round/`), `_rev(repo, spec)`.
  - `build_run_line(nn, prof, passthrough)` and `_map_exit(code, state, started)`; `SPAWN` is the child seam (tests replace it); `run_start` shows the lock file and the Ctrl-C wait.
  - `_VIEW_ARG`, the `NN[.K]` parsing `run_view` uses.
- `tools/arena/profile.py`: `profile_flags(profile, overrides)`, `dedupe_flags(words)` (AR-62: the last value of a flag wins), `fresh_on(value)`.
- `scripts/revive_round.py`: `REVIVE`, `REVIVED_STATE`, `BACKUP_NAME`, `leg_folders`, `state_path`; the revive loop is inline in `main`.
- The runner (`tools/contest/cli.py`): `--resume` skips terminal agents and takes `WAITING` ones up again; it refuses `--resume` with more than one leg.

---

## What to build

### `scripts/revive_round.py`

- Extract the loop from `main` into `revive_agents(agents: list[dict], only: str | None = None, dead: bool = False) -> list[str]`. It changes the agents in place and returns the revived names; the printed lines stay in `main`.
- `dead=True` adds `DEAD` to the revived states. Without it `DEAD` is kept, as today.
- `only=NAME` revives that one agent only.
- `main` gets `--dead` and `--agent NAME`. Without them its output, its `state.before-revive.json` and its exit codes are exactly as before.
- Nothing else under `scripts/` changes.

### `arena run rerun NN[.K] (--failed | --agent NAME) [--dead] [--dry-run] [-y] [-- PASSTHROUGH]`

- `--failed` or `--agent NAME` is required (one of them, not both); neither → refusal.
- The round: `NN` or `NN.K` as in `run view`; `round_folder` finds it. No `state.json` there → exit 1, one line naming the path.
- Live round (`round_alive`) → refusal, exit 2, `state.json` untouched.
- An earlier leg `NN.K` that is not the last one → refusal saying later legs already changed the trees, unless `-y`.
- `--agent NAME`:
  - an unknown name → refusal listing the names;
  - `READY` / `WAITING` / any state not revivable → refusal naming that state;
  - `DEAD` without `--dead` → refusal `NAME is DEAD — add --dead to bring it back`.
- `--failed` with nothing to revive → exit 3 (`EXIT_NOTHING`), one line, nothing written, no child.
- `--dry-run` prints one line per agent (`name: STATE -> WAITING` or `(kept)`) and stops: no write, no child.
- Otherwise: copy `state.json` to `state.before-revive.json`, write the revived file, then start the runner through `SPAWN` exactly as `run start` does (print the line, lock file, wait, `_map_exit`).
- The base: `arena-round/NN` when that ref exists, else `base_sha` from `state.json`; neither → refusal.
- The run line: `… run --ticket NN --base <base> <profile flags> <passthrough> --resume`, through `dedupe_flags`, with two profile keys dropped:
  - `fresh` / `--fresh` never reaches a rerun (it would wipe the trees being resumed) — from the profile key, `extra` or the passthrough;
  - a round of legs (the folder is `NN.K`): `--out <out_dir>/NN.K --legs 1`, whatever the profile's `legs` says; `<out_dir>` from the roster, never a literal `contest-out`.
- A round without legs gets no `--out` and no `--legs`.

---

## Tests (`tests/test_arena_run_rerun.py`)

Throw-away repo under `tmp_path` with `contest.ini` (`out_dir`, one `[contest.agent.a]`), patched `cli.REPO_ROOT`, `rounds.PROC_ROOT` (fake `/proc`) and `rounds.SPAWN` (a stub that records argv and returns an exit code). No kilo, no model, no real round.

1. A live round → exit 2, `state.json` byte-for-byte unchanged, the stub never called.
2. `--failed --dry-run` → lines per agent, no write, no child.
3. `--failed` revives `STALLED`/`GAVE_UP`/`ERROR`, keeps `READY` and `DEAD`; `state.before-revive.json` holds the old file; `revived_from` holds the old state.
4. `--failed --dead` revives the `DEAD` one too (`revived_from.state == "DEAD"`).
5. `--agent x` revives only `x`; `--agent` on a `READY` agent → refusal naming `READY`; on `DEAD` without `--dead` → refusal naming `--dead`; an unknown name → refusal listing names.
6. Nothing to revive → exit 3, no write, no child.
7. The run line carries `--resume` and `--base arena-round/NN` when the ref exists, else `--base <base_sha>`.
8. Profile `fresh = yes`, `extra = --fresh` and passthrough `--fresh`: no `--fresh` in the run line.
9. Legs round (`out/65.1`, `out/65.2`, roster `out_dir = out`), profile `legs = 3`: `rerun 65` → `--out <repo>/out/65.2 --legs 1`, one `--legs`; `rerun 65.1` without `-y` → refusal, with `-y` → `--out …/65.1`.
10. The stub's exit code 2 with `state.json` rewritten after the start → 4; without the rewrite → 1.
11. `scripts/revive_round.py N --dry-run` (no `--dead`, no `--agent`) prints what it printed before; `--dead --dry-run` lists the `DEAD` agents.

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
python3 -m pytest tests/test_arena_run_rerun.py tests/test_arena_run_start.py tests/test_arena_run_view.py tests/test_arena_profile_set.py tests/test_arena_cli.py tests/test_kilo_contest_runbook.py -q
```

```bash
python3 -m pytest tests -n 8 -q
```

```bash
python3 -m pytest tests_bugfix -n 8 -q
```

All cases above pass. `contest-bench/138`, `139` and `142` still pass.

---

## Rules

- No change under `tools/contest/`. Under `scripts/` only `revive_round.py`.
- `REPO_ROOT` is the only repo seam; never read the current working directory.
- **Never run a real round while working on this ticket** — no `python3 -m tools.contest run …`, no `arena run start …`, no `arena run rerun …` and no `scripts/revive_round.py` on a real `contest-out/` folder. Every check goes through a test with a stub `SPAWN` on a fake `out/`.
- Every refusal is one line through `output.refuse`, exit 2, and writes nothing.
- Never read or print `agents_128k.ini`. No real provider or model names in committed files. Never print an api key.
- Do not change existing tests except to add cases; `tests/test_arena_cli.py` may move its one "not implemented" example off `run rerun` to another unimplemented verb.
- Python 3.10+, four-space indent, type hints on public functions, explanatory comments in the style of `tools/arena/rounds.py`.
- Tests wait on events, never on sleeps or tight timeouts.
- No unrelated repository changes.
- Commit subject starts with the round number: `143: …`.
