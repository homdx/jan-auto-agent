# AR-3 — `arena run start NN` builds the round's base ref and starts the old runner; `arena run list`

**Status:** open
**Severity:** LOW
**File:** tools/arena/rounds.py
**Symbol:** round_folder, round_alive, commit_file_on, ticket_file
**Round:** 138
**Size:** M
**Also touches:** tools/arena/gitref.py, tools/arena/cli.py, tools/contest/cli.py, tests/test_arena_run_start.py, tests/test_arena_run_list.py

---

## Why

Every round today is the same long `python3 -m tools.contest run --ticket NN …` line typed by hand, and the ticket must already be committed in the operator's checkout. AR-3 makes it one command: `arena run start NN` finds the ticket, builds a one-commit base ref `arena-round/NN` that holds it (without touching the operator's checkout), and starts the old runner as a child with the profile's flags (AR-2). `arena run list` lists the rounds in the output folder. The old `tools.contest` command keeps working; its only change is step 0.

---

## What AR-1 and AR-2 already give you (read them before writing)

- `tools/arena/cli.py`:
  - `Verb(help, ticket, add_arguments=None, handler=None)`; `OBJECTS["run"].verbs["start"]` and `["list"]` exist with `ticket="AR-3"` and no handler. AR-3 sets their `handler` / `add_arguments`; other verbs keep printing `… is not implemented yet (AR-N)`.
  - `REPO_ROOT`: the checkout `arena` runs from (the directory that holds `tools/`). It is the one repo seam; tests patch it with `monkeypatch.setattr(cli, "REPO_ROOT", tmp_repo)`. Never read the current working directory.
  - `_load(args) -> (profiles, selected_name)`: `-p` wins over `[arena] profile`, an unknown `-p` raises `ProfileError` with the known names. Reuse it; do not re-implement profile selection.
  - `args.profile`, `args.output`, `args.yes`, `args.passthrough` (everything after the first standalone `--`, verbatim).
  - Exit codes: `EXIT_OK = 0`, `EXIT_FAILED = 1`, `EXIT_USAGE = 2`, `EXIT_NOTHING = 3`, `EXIT_NO_READY = 4`.
- `tools/arena/profile.py`: `load_profiles(repo)`, `profile_flags(profile, overrides) -> list[str]` (already refuses `--base`/`--ticket` in `extra`), `ProfileError`. A missing profile file means no profiles; `run start` with no profile at all runs with no profile flags.
- `tools/arena/output.py`: `emit(rows, columns, fmt)` (masks and scrubs every row), `refuse(msg) -> 2` (one scrubbed stderr line), `mask`, `scrub`.
  - Lesson from round 137: `emit` masks any column whose name is or contains the word `key`, so never call a column `KEY`.
- `.arena/` is already in `.gitignore`.
- Old runner facts (checked at the base of this round):
  - `gates.ticket_for_round(tasks_dir, n)` lists only the checkout's `epic-tasks/` and returns `(None, None, [])` when nothing matches.
  - `cli.intake` (`tools/contest/cli.py`) then fails with `no ticket numbered NN in …`, and `cmd_run` sizes the round with `ticket_size(tasks_dir / ticket_name)` from the checkout.
  - `intake` already reads the ticket body and the lower tickets' `**Status:**` at the base (`_ticket_body(..., at=base)`).
  - The runner reads `declared_files(Intake.ticket_path)` from disk.
- Round folders:
  - `cli._round_out_dir(repo, config, round_no)` and `cli._leg_out_dir(out_dir, leg)` (`tools/contest/cli.py`) give `<out_dir>/NN` and `<out_dir>/NN.K`, zero-padded to two digits.
  - `scripts/revive_round.py` has `leg_folders(folder)` and `state_path(target, any_leg)`; import them (`from scripts.revive_round import leg_folders` works: `scripts/` is a namespace package off the repo root), do not copy.
  - `<out_dir>` is the roster's `out_dir` (`roster.load_roster`), never a literal `contest-out`.

---

## What to build

### Step 0 — `tools/contest/cli.py`: a ticket that is only at the base (the one `tools/contest/` edit)

- Add `ticket_file(repo, tasks_dir, round_no, at) -> tuple[str, Path | None]`:
  - When the checkout has the ticket file: `(name, tasks_dir / name)`, exactly today's path.
  - Otherwise, when the base `at` has the ticket: write `git show <at>:<rel>/<name>` to a file **with the same name** in a fresh folder under the system temp dir and return its path. The ticket is found with `git ls-tree --name-only <at> <rel>/`, where `<rel>` is `tasks_dir`'s path relative to `repo`, and the same `^0*(\d+)-.*\.md$` match as `ticket_for_round`.
  - Otherwise `("", None)`.
- `intake` and `cmd_run`'s `ticket_size` call it **only when `gates.ticket_for_round` found nothing**, so the old path is byte for byte unchanged. `Intake.ticket_path` carries the result, so `declared_files` reads the temp copy.
- The temp folder is removed when `cmd_run` returns (`try/finally`), also on a failure.
- `tests/test_contest_cli.py` and `tests/test_contest_cli_export.py` stay green **unedited**.

### `tools/arena/gitref.py`

- `commit_file_on(repo, parent, path_in_repo, content, message, ref) -> str` (the new sha), with plumbing in a **temporary index**, `GIT_INDEX_FILE=<tmp>`:
  1. `read-tree <parent>`
  2. `hash-object -w --stdin`
  3. `update-index --add --cacheinfo 100644,<blob>,<path>`
  4. `write-tree`
  5. `commit-tree -p <parent> -m <message>`
  6. `update-ref <ref> <sha>`
- The operator's `HEAD`, index and working tree are never touched.
- No `git checkout`, `switch`, `stash`, `reset` or `add` anywhere in arena.
- Every git failure is a `GitRefError` (one line: the git command and its first stderr line). It is never a traceback.

### `tools/arena/rounds.py`

- `round_folder(repo, config, nn, leg=None) -> Path`:
  - `NN.K` is that leg's folder.
  - A bare `NN` is the highest `NN.K` when legs exist and `<out_dir>/NN/state.json` does not; otherwise it is `<out_dir>/NN`.
  - Built over `cli._round_out_dir` / `cli._leg_out_dir` and `revive_round.leg_folders`.
- `round_alive(repo, nn, proc_root="/proc") -> bool`:
  - Alive when a process exists whose `cmdline` holds `tools.contest`, `run` and `--ticket NN` (or `--ticket=NN`, and `07` equals `7`), **and** whose `cwd` resolves to `repo`.
  - Also alive when `.arena/locks/NN.pid` names a live pid whose cmdline still matches.
  - A pid whose `cwd` or `cmdline` cannot be read does not match. `proc_root` is the test seam (a fake `/proc` tree under `tmp_path`).

### `arena run start NN [--branch B] [--fresh-ticket] [-- old flags]`

1. **Find the ticket NN**, in this order:
   - `.arena/drafts/NN-*.md`, never `*.rejected.md`;
   - else `epic-tasks/NN-*.md` in the checkout;
   - else `epic-tasks/` on the integration branch (`git ls-tree`).

   Numbers compare as integers (`07-x.md` is round 7). No ticket is a refusal. More than one file for NN in the first place that has any is a refusal listing them.
2. **Integration branch**:
   - `--branch`, else the profile's `branch`, else `git symbolic-ref --short HEAD`.
   - A detached HEAD with neither flag nor key is a refusal.
   - A branch that does not exist is a refusal.
3. **Refuse before anything is written** when:
   - a live round NN exists (`round_alive`);
   - `epic-tasks/` in the checkout has untracked or modified files. The refusal lists them and adds the hint "move drafts to `.arena/drafts/`". It is the runner's own check, reported early.
4. **Build `arena-round/NN`** (unpadded: `arena-round/7`) with `commit_file_on`:
   - Parent: the integration tip.
   - Path: `epic-tasks/<the ticket's own file name>`.
   - Content: the ticket with its `**Status:**` line forced to `open`.
   - Message: `NN: ticket for the round`.
   - When the integration tip already holds that exact file with that exact content, `arena-round/NN` points at the tip itself (no empty commit).
   - An existing `arena-round/NN` with the same parent and the same tree is reused (same sha).
   - Otherwise (different text, or a moved tip) it is a refusal naming `--fresh-ticket`.
   - `--fresh-ticket` rewrites the ref only when no `NN` / `NN.K` round folder holds a `state.json`, or with `-y`.
   - Then write `.arena/rounds/NN.json` with `branch`, `base_ref`, `base_sha`, `ticket_sha256` and `started_at` (ISO-8601 UTC).
5. **The run line**:

   ```text
   [sys.executable, "-m", "tools.contest", "run", "--ticket", str(NN),
    "--base", "arena-round/NN", *profile_flags(profile), *passthrough]
   ```

   - `--ticket`, `--base`, `--target` and `--out` in the passthrough are refused, as `--x V` and as `--x=V`.
   - The line is printed once, scrubbed, before the child starts.
   - The child's argv is built in one function, `build_run_line(nn, profile, passthrough) -> list[str]`, so tests can check it without a child.
6. **Run it**:
   - Start it as a child process in the foreground with `cwd=REPO_ROOT`, with stdout and stderr inherited.
   - The command that starts the child is a module-level seam (for example `rounds.SPAWN = subprocess.Popen`), so tests run a stub child that records its argv.
   - Write `.arena/locks/NN.pid` with the child's pid and remove it in a `finally`.
   - Exit codes:

     | Child exits | `arena run start` exits | When |
     |---|---|---|
     | 0 | 0 | always |
     | 1 | 1 | always |
     | 2 | 4 (`EXIT_NO_READY`) | the round ran: `round_folder`'s `state.json` exists and its mtime is not older than the child's start |
     | 2 | 1 | otherwise (argparse refusing a typo after `--`; the child's own stderr line is already on screen) |
     | anything else | 1 | always |

   - Ctrl-C reaches the child (same process group). arena waits for it and does not print a traceback.
7. Never pass `--fresh`, `--resume` or `--no-gate` on its own. Only the profile's `extra` and the passthrough add flags.

### `arena run list`

- One row per round in `<out_dir>`. Only directories named `^\d+(\.\d+)?$` count. Files such as `probe-memory.json` and other directories are ignored.
- Legs are collapsed: `06.1/` and `06.2/` are one row `6` with `LEGS 2/2`. A round without legs shows `LEGS` empty.
- Columns are `RUN LEGS STATE AGE READY/TOTAL`, newest first (by the last leg's `state.json` mtime).
- `STATE`:
  - `running` when `round_alive`;
  - `done` when not alive and `state.json` reads;
  - `?` when `state.json` is missing or unreadable. That is a row, never a crash.
- `READY/TOTAL` comes from the last leg's `state.json` `agents[].state`: count of `READY` / number of agents. It is empty for a `?` row.
- `AGE` is the time since that mtime, compact (`45s`, `12m`, `3h`, `2d`).
- No round folder at all: one stderr line and `EXIT_NOTHING` (3).
- `-o json` works through `emit`.

---

## Tests

Use a throw-away git repo under `tmp_path`, with `git init`, one commit, a `contest.ini` holding `out_dir` and the identity set via `-c user.name=… -c user.email=…` or env. Patch `cli.REPO_ROOT`. No `kilo`, no model, no network, no real `contest-out/`. Wait on events (marker files, `proc.wait()`), never on sleeps.

`tests/test_arena_run_start.py`:

1. `commit_file_on` creates `arena-round/7` whose parent is the integration tip and whose tree is the tip's tree plus `epic-tasks/07-x.md`. `git status --porcelain`, `git rev-parse HEAD` and the index file's bytes are identical before and after.
2. The same ticket twice gives the same sha (reuse). A ticket already committed with the same text on the tip makes `arena-round/7` the tip's own sha.
3. Changed ticket text is a refusal naming `--fresh-ticket`.
   - With `--fresh-ticket` and no `state.json`: a new sha.
   - With a `state.json` in `07/` and no `-y`: a refusal, ref unchanged.
   - The same text on a moved integration tip: the same refusal as for changed text.
4. `build_run_line(7, {"models": "a,a,b", "legs": "2"}, ["--no-gate"])` is exactly `[sys.executable, "-m", "tools.contest", "run", "--ticket", "7", "--base", "arena-round/7", "--models", "a,a,b", "--legs", "2", "--no-gate"]`. `-- --base X`, `-- --ticket=3`, `-- --out d` and `-- --target r` are each refused with exit 2, and no ref is created.
5. A dirty `epic-tasks/` (an untracked `epic-tasks/99-draft.md`) gives exit 2, one stderr line naming the file and `.arena/drafts/`. Nothing is created: no `arena-round/7` ref, no `.arena/rounds/7.json`.
6. Step 0, a real `python3 -m tools.contest run --ticket 7 --base arena-round/7 --dry-run` in the tmp repo:
   - The ticket is **absent from the checkout's `epic-tasks/`**: it exists only on `arena-round/7` and in `.arena/drafts/`.
   - Intake passes, the round is sized from the ticket's `**Size:**`, and no temp ticket folder is left behind.
   - A ticket present in the checkout behaves exactly as before.
   - Patch intake's server and provider checks the way `tests/test_contest_cli_export.py` (`test_dry_run_prints_the_plan_and_the_first_prompt_without_a_server`) already does. Reuse that approach, do not invent a new one.
7. The lock file `.arena/locks/7.pid` exists while the stub child runs (the stub writes a marker file the test waits for) and is gone after, also when the child exits 1.
8. Exit mapping with a stub child:
   - writes `state.json` and exits 2 → 4;
   - exits 2 without one → 1;
   - exits 1 → 1;
   - exits 0 → 0.
9. `round_alive`, on a fake `/proc` under `tmp_path`:
   - a matching cmdline with this repo's cwd → alive;
   - the same cmdline with another repo's cwd → not alive;
   - `--ticket=07` for 7 → alive;
   - a stale `.arena/locks/7.pid` naming a pid whose cmdline no longer matches → not alive.
10. A live round 7 (fake `/proc`) makes `run start 7` exit 2 before any ref is written.
11. The ticket search order: `.arena/drafts/` beats `epic-tasks/`, and `07-x.rejected.md` is ignored. Two drafts for 7 are a refusal listing both names. A detached HEAD with no `--branch` and no profile `branch` is a refusal.

`tests/test_arena_run_list.py`:

1. A fake out folder with:
   - `05/state.json` (2 of 3 READY);
   - `06.1/` and `06.2/` (each with `state.json`);
   - `08/` with a broken `state.json`;
   - a stray `probe-memory.json` and a `notes/` directory.

   It gives exactly three rows: `6` with `LEGS 2/2`, `8` with `STATE ?`, and `5` with `2/3`. Newest is first. The file and `notes/` are ignored.
2. `-o json` parses, and its row for `6` names `2/2` legs.
3. No round folder: exit 3, one stderr line.
4. `out_dir` set in `contest.ini` to another name is the folder listed, not `contest-out`.

Tier both files with `python3 scripts/sync_test_tiers.py`; never edit tier symlinks by hand.

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
python3 -m pytest tests/test_arena_run_start.py tests/test_arena_run_list.py tests/test_arena_profile.py tests/test_arena_cli.py -q
```

```bash
python3 -m pytest tests -n 8 -q
```

```bash
python3 -m pytest tests_bugfix -n 8 -q
```

All cases above pass. `tests/test_arena_cli.py`, `tests/test_arena_profile.py`, `tests/test_contest_cli.py` and `tests/test_contest_cli_export.py` stay green and unchanged. `contest-bench/136/acceptance_136.py` and `contest-bench/137/acceptance_137.py` still pass.

**Verify first, and write the result into the commit message:** read `cli.intake`'s `gates.ticket_for_round` call, `cmd_run`'s `ticket_size` call and `cli._on_offer`'s `base_is_head` branch, and say whether step 0 is all `run` needs for a base that is not `HEAD`. Test 6 is the proof.

---

## Rules

- Step 0 is the only change under `tools/contest/`. Today's path, with the ticket in the checkout, is byte for byte unchanged.
- No `git checkout`, `switch`, `stash`, `reset`, `add` or `commit` (porcelain) in arena code. Use plumbing only, in a temporary index.
- The ref prefix is `arena-round/`, never `arena/`: this repo has a branch `arena`, and a ref cannot be both a file and a directory.
- `REPO_ROOT` is the only repo seam, and it must point at the checkout root.
  - Lesson from round 137: two entries set it one directory off, and their own tests never noticed, because every test patched it.
  - Keep one test that checks its real, unpatched value.
- Every refusal is one line through `output.refuse`, exit 2. A broken input (unreadable `state.json`, failing git command, bad ini) is a refusal or a `?` row, never a traceback.
- Never pass `--fresh`, `--resume` or `--no-gate` implicitly.
- Never read or print `agents_128k.ini`. No real provider or model names in committed files.
- Do not change `tests/test_arena_cli.py`, `tests/test_arena_profile.py` or `tests/test_contest_cli*.py`.
- Python 3.10+, four-space indent, type hints on public functions, explanatory comments in the style of `tools/arena/cli.py`.
- Tests wait on events, never on sleeps or tight timeouts. Any test that spawns a child process has a `timeout=` on its wait.
- No unrelated repository changes.
- Commit subject starts with the round number: `138: …`.
