# KC-16 — `python3 -m tools.contest run --ticket NN`: the round on the real repo, tests in the harvest, one `.patch` per agent — nothing else

**Status:** landed `1304950` — ideal patch from a 6-entry contest (`kc16/*.patch` + `kc12/kc12-Sensenova-6-7-var3.patch`), scored black-box via `contest-bench/kc16/` (19 scenarios against the command itself; winner Sensenova 6-7 var3, 19/19). Round 55 of EPIC KC (`docs/kilo-contest/EPIC-KC.md`); KC-6 (round 45) landed `e8c6ad3` and was run live on 2026-09-19 (`contest-bench/kc6/RUNBOOK.md` §10–§11). **The last round run by hand**: from here every ticket is run through `python3 -m tools.contest run --ticket NN`. Takes the core of KC-7 (round 46, now queued after this one); what KC-7 keeps is listed there. Touches `tools/contest/runner.py` in two places only (below); KC-12 (round 51) deletes a different function of the same file — the two are landed one after the other, not merged by the entrant.
**Severity:** HIGH
**File:** `tools/contest/cli.py` (new), `tools/contest/__main__.py` (new), `tools/contest/runner.py` (`run_round`, `run_agent`, `_plan` — the `run_tests` thread only)
**Symbol:** `main`, `cmd_run`, `intake`, `export_patches`, `run_round(…, run_tests=False)`
**Round:** 55
**Size:** S
**Source:** `contest-bench/kc6/live_smoke.py` is this command written by hand for a sandbox: `load_roster` → `replace(config, …)` → `prepare_round` → `KiloServer.spawn` → `run_round` → `server.close()` → the table → `git log <base>..HEAD` per worktree. What the operator wants from a round is exactly that, on the real repo and the real ticket, plus `git format-patch` of each result — the patches then go through the same hands as every round so far (`docs/collect-epics/RUN-THE-EPIC-COMPETITION.md` stages 3–5, the ideal commit). No summary, no `entrants.json`, no scoring: the tests are the only judge in this command.
**Depends on:** KC-6 (`tools/contest/runner.py`, landed `e8c6ad3`), KC-5 (`harvest(…, run_tests=)`), KC-4 (`prepare_round`), KC-2 (`load_roster`).
**Also touches:** `tests/test_contest_cli.py` (new), `tests/test_contest_runner.py` (the `run_tests` thread — add, do not modify existing tests), `AGENTS.md` (one line under Build/Run)

---

## What happens today

`run_round` exists and works live, but only `contest-bench/kc6/live_smoke.py`
calls it, on a sandbox in `/tmp`. Nothing runs it on this repo against an
`epic-tasks/NN-*.md`, nothing writes a patch, and the harvest inside the
runner never runs the tests: `run_agent` and `_plan` call `harvest(ws,
ticket_path)` with `run_tests` at its default `False`, so an agent whose
change breaks `tests/` is READY. The operator still runs the models by hand
in the editor and collects the patches by hand.

## What must change

1. **`run_round(config, round_no, ticket_path, workspaces, *, server, out_dir, resume=None, run_tests=False)`**
   — the one new keyword, passed unchanged to both `harvest` calls
   (`run_agent` after every turn, `_plan` for a mid-flight resume).
   With `run_tests=True` the pytest roots run **one worktree at a time**:
   a module-level `threading.Lock` around the harvest's test run (the
   harvest itself may stay unlocked — only the tests are slow), because the
   judge machine takes 20 minutes for 8 parallel suites and ~105 s for one.
   `tests_failed` is already a `REASON_CODES` entry and `rework_message`
   already prints the tail — the rework prompt needs nothing new. The
   `harvest` line of `turns.jsonl` gains nothing either: `facts["tests_run"]`
   is already in the `Harvest`, and `state.json`'s shape is KC-6's contract
   (`RoundState.from_dict` round trip, unchanged).

2. **`intake(repo, tasks_dir, round_no, base_ref, config) -> Intake`** in
   `cli.py`, before anything else, all checks run and every failure reported
   (one line each, exit 1):
   - `gates.ticket_for_round(tasks_dir, round_no)` finds the ticket, and its
     `**Status:**` first word is `open` (`scripts/next_task.py`'s rule);
   - the ticket is **the lowest-numbered `open` ticket** in `tasks_dir` —
     it is what `next_task.py` will hand the session (the prompt does not
     name the ticket; `runner.round_prompt` says why). The message names
     the tickets in the way: `KC-7 (46) is open too — set it to queued
     or run it first`;
   - `base_ref` resolves (`git rev-parse --verify`) and `epic-tasks/` is
     clean at it — `workspace.prepare_round` raises for both; catch
     `WorkspaceError` and report, do not re-implement;
   - `find_kilo_binary(config.kilo_bin)` resolves when `config.server ==
     "spawn"`; otherwise `KiloServer.attach(config.server)` answers.
   Everything passes → `Intake(ticket_path, title, base_sha, out_dir)` and
   one printed plan line per fact: ticket, base, N agents with models,
   `max_parallel`, tests on/off, gate on/off, out dir.

3. **`cmd_run`** — `python3 -m tools.contest run --ticket NN [--roster contest.ini] [--base HEAD] [--models a:free,b:free] [--max-parallel N] [--no-tests] [--no-gate] [--resume] [--out DIR]`:
   - `--roster` → `load_roster` (default `contest.ini` at the repo root,
     overlay `contest.local.ini` when present — KC-2's rule);
   - `--models` replaces the roster's agents exactly as
     `contest-bench/kc6/live_smoke.py::agents_from_models` does (copy the
     function into `cli.py`; provider `kenary` unless the id has one);
     `--max-parallel` overrides `config.max_parallel`;
   - `--no-tests` → `run_tests=False`; the default is **on**;
   - `--no-gate` → `replace(config, gate_settings=None)`: the policy's
     mechanical layer still decides, and every ask it cannot decide is the
     existing `gate-failed` reject (`gate unavailable: no gate model
     configured`), recorded in `decisions.jsonl` like any decision. Without
     `--no-gate` and without a resolvable `api_key` the round still runs —
     the gate profile is loaded by `load_roster` as today; this command
     adds no key check (KC-7's intake does);
   - `--out DIR` default `<config.out_dir>/<NN>` relative to the repo
     (`contest-out/55`); `--resume` loads `<out>/state.json` through
     `RoundState.from_dict` and passes it as `resume`, with the workspaces
     taken from it (`run.workspace` per agent), exactly `live_smoke.py`'s
     branch;
   - then: `prepare_round(repo, config, NN, base)`, `KiloServer.spawn(bin,
     log_path=<out>/kilo-serve.log)` or `attach`, `run_round(…,
     run_tests=…)`, `server.close()` in `finally` (Ctrl-C: `run_round`
     already saves and re-raises — let it propagate after the close);
   - then `export_patches`, then one line per agent from
     `state.table_rows()` (JSON, as `live_smoke.py` prints) and one line per
     patch written;
   - exit **0** when ≥ 1 agent is READY, **2** when none, **1** on intake or
     server failure.

4. **`export_patches(state, workspaces, out_dir) -> list[Path]`** — for
   every agent whose `run.commit` is set: `git -C <ws.path> format-patch
   --stdout <base_sha>..HEAD` → `<out>/<agent>.patch`, or
   `<out>/<agent>.GAVE_UP.patch` when the state is `GAVE_UP` (the operator
   still wants to read those). Empty output → no file, one warning line.
   READY without a commit cannot happen (harvest sets it); do not guard for
   it with a fake sha.

5. **`tools/contest/__main__.py`** → `from .cli import main; sys.exit(main())`.
   `main(argv=None)` uses `argparse` subcommands so KC-7 can add `status`
   and `--dry-run` without moving anything. `AGENTS.md` gains one line:
   `python3 -m tools.contest run --ticket NN` — the round; patches in `contest-out/NN/`.

## Acceptance

- [ ] `tests/test_contest_runner.py` (new tests only): with
      `tests/_kilo_fake.py` and a worktree whose fake agent commits a change
      that makes a tiny `tests/test_x.py` fail, `run_round(…, run_tests=True)`
      → the first turn's `harvest.reasons` contains `tests_failed`, the
      rework prompt sent to the session contains the pytest tail, and the
      same run with `run_tests=False` is READY after one turn; two agents
      harvested at once with `run_tests=True` never run pytest concurrently
      (monkeypatch `gates.run_tests_detail` with a function that asserts a
      counter is 0 on entry, sleeps 0.2 s, and decrements).
- [ ] `tests/test_contest_cli.py`, all on a temp git repo with a committed
      `epic-tasks/` (two tickets: `01-…md` `open`, `02-…md` `open`) and a
      roster of two agents, `KiloServer.spawn` monkeypatched to attach to
      the fake:
      - `run --ticket 2` → exit 1, the message names `01` as open and in the
        way; `--ticket 1` with `02` set to `queued` in a second commit passes
        intake;
      - `--ticket 1` where the ticket's status is `queued` → exit 1, named;
      - an unresolvable `--base` → exit 1, named; a dirty `epic-tasks/` →
        exit 1, named (both through `WorkspaceError`);
      - a full `run --ticket 1 --no-gate --no-tests` with the fake making
        one agent READY and one GAVE_UP with a commit → exit 0,
        `<out>/laguna.patch` and `<out>/mistral.GAVE_UP.patch` exist,
        `git am` of `laguna.patch` onto the base in a fresh temp worktree
        applies cleanly and reproduces the branch's tree, `state.json` has
        both agents, the table lines are printed; both GAVE_UP → exit 2;
      - `--models x:free,y:free` → the plan lists `kenary/x:free` and
        `kenary/y:free` and nothing from the roster; `--max-parallel 1` →
        the plan says 1;
      - `--resume` after a first run that ended with one agent mid-flight
        (`state.json` written by hand from a `RoundState`) restarts only
        that agent (the fake counts sessions);
      - `--no-gate` → `Policy` is built with no gate settings (a
        `permission.asked` for a path outside the worktree in the fake
        ends as `gate-failed` in `decisions.jsonl`, and no HTTP call is
        attempted — monkeypatch `request_completion` to raise).
- [ ] `python3 -m tools.contest run --help` prints the flags above;
      `python3 -m tools.contest` with no subcommand → usage, exit 2.
- [ ] `tests/test_contest_runner.py`'s existing tests **unmodified** and
      green (the default `run_tests=False` keeps every one of them as is).
- [ ] `python3 -m pytest tests -n 4 -q --timeout=180 && python3 -m pytest tests_bugfix -n 4 -q --timeout=180` green.

## Out of scope

- `SUMMARY.md`, `entrants.json`, `duplicate_of`, `status`, `--dry-run`, the
  gate `api_key` check at intake — KC-7, on top of this.
- Any scoring, ranking or judging of the patches: the operator does that
  (`contest-bench/`, the ideal commit), as in every round so far.
- Flipping ticket statuses: an `open` ticket in the way is reported, never
  edited (`epic-tasks/` is the orchestrator's).
- Running the two pytest roots inside the *model's* session — the ticket's
  self-check already tells it to; this command's tests are the harvest's.

## Self-check before `append_task.py` (required — every item, in the worktree you submit)

Every line below is a way a KC-5/KC-6 entry lost points on the round bench;
the scorer checks all of them mechanically, so check them yourself first.

- [ ] `python3 --version` on the judge is **3.10.12**. Every changed module
      imports there: `python3 -c "import tools.contest.cli, tools.contest.runner"`
      from the repo root; `python3 -m tools.contest --help` runs. No
      backslash and no nested same-quote inside an f-string expression (a
      3.12-only `f"{x.split("\t")}"` is a `SyntaxError` here and scores 0).
- [ ] Exactly **one** commit on top of the base: `git log --oneline <base>..HEAD`
      prints one line. Only this ticket's work is in it — no KC-12, no
      KC-7 extras, no "while I was here" fixes; amend, do not stack.
- [ ] `git diff --stat <base>..HEAD` names only the files under **File:**
      and **Also touches:** (plus `.smoke_tests/` links). Never `epic-tasks/`,
      never `contest-bench/`.
- [ ] `run_round`'s existing positional and keyword parameters are unchanged
      and `run_tests` is keyword-only with default `False`; `harvest`,
      `prepare_round`, `KiloServer`, `Policy`, `RoundState` are called, not
      re-implemented — read `tools/contest/runner.py`, `harvest.py`,
      `workspace.py`, `kilo_client.py` and `contest-bench/kc6/live_smoke.py`
      before writing `cli.py`.
- [ ] No test starts a real `kilo`, calls a provider, or runs the repo's own
      `tests/` root from inside a test (the fake pytest root is a temp dir).
- [ ] `python3 scripts/sync_test_tiers.py --check` is clean.
- [ ] The new tests are red without the change: check the test files alone
      out onto the base, run them, see them fail; restore.
- [ ] `python3 -m pytest tests -n 4 -q --timeout=180` then
      `python3 -m pytest tests_bugfix -n 4 -q --timeout=180`, **sequentially**,
      both green.
- [ ] `CollectBridge._shrink` byte-identical:
      `git diff <base> HEAD -- tools/auto/collect_bridge.py` is empty.
- [ ] Then, and only then, `scripts/append_task.py` from the worktree;
      open `runs/<you>/PROGRESS.csv` and see your row with the **sha** of
      the one commit — not `HEAD`.
- [ ] What you hand in is `git format-patch <base>..HEAD` of that one
      commit — not a raw `git diff`, not the whole branch, not an empty file.

## Ground rules (same as every round)

- Do not touch `CollectBridge._shrink`.
- No test starts a real `kilo` or calls a live provider.
- Do not edit `epic-tasks/`.
- One commit, no push; a test ships with the change and fails without it.
