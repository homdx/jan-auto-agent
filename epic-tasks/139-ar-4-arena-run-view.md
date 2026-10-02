# AR-4 — `arena run view NN[.K]`: one round or one leg, without `--out`

**Status:** queued
**Severity:** LOW
**File:** tools/arena/rounds.py
**Symbol:** run_view, view_rows
**Round:** 139
**Size:** S
**Also touches:** tools/arena/cli.py, tests/test_arena_run_view.py

---

## Why

`python3 -m tools.contest status --ticket NN` needs `--out contest-out/NN.K` for a round of legs, and the operator has to know which leg is the last one. AR-4 makes it `arena run view 134`: the round's folder is found the same way `run start` / `run list` find it (AR-3), and the table is the one the operator already reads.

---

## What AR-1 … AR-3 already give you (read them before writing)

- `tools/arena/cli.py`:
  - `OBJECTS["run"].verbs["view"]` exists with `ticket="AR-4"` and no handler. AR-4 sets its `handler` and `add_arguments`; it changes no other verb.
  - `REPO_ROOT` is the one repo seam (tests patch it with `monkeypatch.setattr(cli, "REPO_ROOT", tmp_repo)`); `_run_start` / `_run_list` show how a `run` verb hands `REPO_ROOT` to `rounds`.
  - Exit codes: `EXIT_OK = 0`, `EXIT_FAILED = 1`, `EXIT_USAGE = 2`, `EXIT_NOTHING = 3`, `EXIT_NO_READY = 4`.
- `tools/arena/rounds.py` (AR-3):
  - `load_config(repo)` — the roster, for its `out_dir`; raises `RoundError` on a bad ini.
  - `round_folder(repo, config, nn, leg=None)` — `NN.K` is that leg; a bare `NN` is its highest leg when legs exist and `NN/state.json` does not, else `<out_dir>/NN`.
  - `round_alive(repo, nn, proc_root=PROC_ROOT)` — the liveness check; `PROC_ROOT` is the test seam.
  - `run_list` and `LIST_COLUMNS` — the pattern for a verb that reads round folders and emits rows.
  - `scripts.revive_round.leg_folders(folder)` — already imported there.
- `tools/arena/output.py`: `emit(rows, columns, fmt)`, `refuse(msg) -> 2`, `mask`, `scrub`. `mask` keys on whole words: `tokens` is not `token`, so a token count stays a number.
- `tools/contest/cli.py`: `cmd_status(args)` prints the SUMMARY table off `<out>/state.json` when `args.out` is set; it reads `args.ticket`, `args.out`, `args.roster`. `DEFAULT_ROSTER = "contest.ini"`.
- `state.json` holds `round_no`, `ticket`, `base_sha`, `started_at`, `agents`. Each agent has `agent`, `state`, `attempt`, `commit`, and `tokens` — a dict (`input`, `output`, `reasoning`, `cache: {read, write}`), not a number.

---

## What to build

### `arena run view NN[.K]`

- The argument is `NN` or `NN.K` (`^\d+(\.\d+)?$`, numbers as integers: `07.2` is round 7 leg 2). Anything else is a refusal, exit 2.
- The folder:
  - `NN.K` → `round_folder(repo, config, NN, leg=K)`;
  - `NN` → `round_folder(repo, config, NN)`.
- No `state.json` in that folder (or no folder): exit 1, one stderr line `arena: no round NN (looked in <path>)`, where `<path>` is the folder looked in. This is not a refusal (exit 1, not 2), but it is one scrubbed line.
- An unreadable `state.json` (broken JSON, not an object, no `agents` list): exit 1, one stderr line naming the file. Never a traceback.
- Table output (default):
  - First one header line: `round NN · leg K/N · <running|done> · base <sha7>`.
    - `leg K/N`: K is the leg shown, N the number of leg folders; a round without legs prints no `leg …` part (`round NN · done · base abc1234`).
    - `running` when `round_alive(repo, NN)`, else `done`.
    - `base` is `state.json`'s `base_sha`, first 7 characters; `?` when missing.
  - Then the table: `tools.contest.cli.cmd_status` called **in process** with `argparse.Namespace(ticket=NN, out=str(folder), roster=DEFAULT_ROSTER)`. Its exit code is `run view`'s exit code.
  - `cmd_status` reads `Path.cwd()` only when `out` is empty; `out` is always set here, so arena never depends on the current directory.
- `-o json` (through `emit`, so it is masked and scrubbed): one row per agent in `state.json` order, keys `agent`, `state`, `attempt`, `tokens`, `commit`.
  - `tokens` is the agent's total: `input + output + reasoning` as an integer (0 when missing). It stays a number in the JSON, never `***`.
  - `commit` is the 12-character prefix or empty.
  - No header line on stdout in JSON mode: stdout must parse with `json.loads`.
- `run view` never writes a file and never starts a process.

### `tools/arena/cli.py`

- `run view`'s handler loads the config through `rounds.load_config(REPO_ROOT)` (a `RoundError` is a refusal, exit 2) and calls `rounds.run_view(REPO_ROOT, args)`.
- Every other verb keeps its behaviour. `tests/test_arena_cli.py` uses `run view` as its "not implemented yet" example since round 138: change that one example to another verb that is still unimplemented (for example `run rerun`, AR-5), and nothing else in that file.

---

## Tests (`tests/test_arena_run_view.py`)

Throw-away repo under `tmp_path` with a `contest.ini` holding `out_dir` and one `[contest.agent.a]` section (the roster refuses an empty one). Patch `cli.REPO_ROOT` and `rounds.PROC_ROOT` (a fake `/proc` under `tmp_path`). No `kilo`, no model, no network, no git commit needed, no real `contest-out/`. Never run a real round: no `tools.contest run` and no `arena run start` without a stub, also not "just to try it".

1. `out/134.1/` and `out/134.2/` with `state.json` each: `run view 134` shows leg 2 (`leg 2/2` in the header, leg 2's agents in the table).
2. `run view 134.1` shows leg 1 (`leg 1/2`).
3. A round without legs (`out/07/state.json`): no `leg` part in the header; `run view 7` and `run view 07` are the same.
4. No folder: exit 1, one stderr line naming the path looked in.
5. A broken `state.json`: exit 1, one stderr line, no traceback.
6. `-o json` parses, has one entry per agent in order, `tokens` is an integer equal to input + output + reasoning, and the word `***` is nowhere in it; an agent field named `api_key` added to `state.json` does not reach the output unmasked.
7. A fake `/proc` with a live `tools.contest run --ticket 134` whose cwd is the repo → `running`; without it → `done`.
8. `run view abc` and `run view 1.2.3` are refusals, exit 2.
9. `REPO_ROOT`'s real, unpatched value is the checkout root (keep the AR-3 check green).

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
python3 -m pytest tests/test_arena_run_view.py tests/test_arena_run_start.py tests/test_arena_run_list.py tests/test_arena_profile.py tests/test_arena_cli.py -q
```

```bash
python3 -m pytest tests -n 8 -q
```

```bash
python3 -m pytest tests_bugfix -n 8 -q
```

All cases above pass; `contest-bench/138/acceptance_138.py` still passes.

---

## Rules

- No change under `tools/contest/`: call `cmd_status` as it is.
- `REPO_ROOT` is the only repo seam; never read the current working directory.
- **Never run a real round while working on this ticket** — no `python3 -m tools.contest run …` and no `arena run start …` outside a test with a stub child. Round 138: two entries ran their own `arena run start 5` "to check it", which built a ref and started a detached round with its own `kilo serve` beside the real one. Check by hand only with `arena run view`/`run list` on a fake `out/` folder.
- Every refusal is one line through `output.refuse`, exit 2. A broken `state.json` is exit 1 with one line, never a traceback.
- Never read or print `agents_128k.ini`. No real provider or model names in committed files.
- Do not change `tests/test_arena_run_start.py`, `tests/test_arena_run_list.py`, `tests/test_arena_profile.py` or `tests/test_contest_cli*.py`; in `tests/test_arena_cli.py` only the one "not implemented" example moves off `run view`.
- Python 3.10+, four-space indent, type hints on public functions, explanatory comments in the style of `tools/arena/rounds.py`.
- Tests wait on events, never on sleeps or tight timeouts.
- No unrelated repository changes.
- Commit subject starts with the round number: `139: …`.
