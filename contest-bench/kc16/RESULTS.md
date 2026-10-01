# KC-16 round — `python3 -m tools.contest run --ticket NN`, scored black-box

Ticket: `epic-tasks/55-kc16-run-a-ticket-on-the-real-repo-patches-out-tests-in-the-harvest.md`
(round 55), base `ae124ea`. Six files for six entries — five in `kc16/` and
`kc12/kc12-Sensenova-6-7-var3.patch`, a KC-16 submission (subject `KC16`,
`cli.py`/`__main__.py`) filed in the KC-12 folder. Two of the six are not
`git format-patch` output but a raw `git diff` of the whole branch against a
commit *before* the base (`epic-tasks/`, and for one `contest-bench/`, in the
diff): applied with those folders excluded and committed by the scorer
(`ingest_kc16.sh`). Both of those, and `kc16-Ling-30-var1.patch`, are half
a ticket: the raw diffs change `runner.py` and its tests but ship no
`cli.py`; Ling-30 var1 ships `cli.py` but leaves `runner.py` untouched, so
`run_round(run_tests=…)` is a `TypeError` on every round it starts.

Method: one worktree per entry at the base with the patch applied, the
mechanical checks of the ticket's self-check list, then 19 scenarios
(`scenarios_kc16.py`, `KC16_REPO=<worktree>`) that run **the command itself**
— `python3 -m tools.contest run …` as a subprocess with `PYTHONPATH` at the
entry, from a sandbox repo with a committed `epic-tasks/` (two `open`
tickets), a two-agent roster whose `server =` is the *base*
`tests/_kilo_fake.py` (no entry touched the fake), and the fake's `on_prompt`
playing the agent inside the worktree. The exit code, stdout/stderr and the
files left behind are the only interface. Then the entry's own tests
(`tests/test_contest_runner.py` + `tests/test_contest_cli.py`). s12 was
shown to fail on a copy of the winner's runner with the lock replaced by
`contextlib.nullcontext()`.

The machine carried eight idle `kilo serve` processes and load ≈ 3; the
existing KC-6 test `test_ctrl_c_aborts_writes_state_and_propagates_then_resume_finishes`
failed once each in sn68 and sn67-var while a scenario dry run was running
next to it, then passed 3/3 on each — a load flake, not the entries'.

## Scores

| entry | model | bench 19 | own tests | full ticket | one commit (format-patch) | on-ticket files | AGENTS.md line | runner tests unmodified | Py 3.10 |
|---|---|---:|---|---|---|---|---|---|---|
| **sn67-var3** | Sensenova 6-7 var3 | **19** | 51 green | yes | yes | yes | **no** | yes | yes |
| sn68 | Sensenova 6-8 | 18 | 46 green | yes | yes | yes | yes | yes | yes |
| sn67-var | Sensenova 6-7 var | 15 | 42 green | yes | yes | yes | yes | yes | yes |
| ling30-var1 | Ling 30 var1 | 5 | 39 green | no — no `run_tests` | yes | yes | yes | yes (untouched) | yes |
| ling30 | Ling 30 | 1 | 29 green | no — no `cli.py` | no — raw diff | no — `epic-tasks/` in the diff | no | helper `_round` edited | yes |
| sn67 | Sensenova 6-7 | 1 | 29 green | no — no `cli.py` | no — raw diff | no — `epic-tasks/`, `contest-bench/` in the diff | no | yes | yes |

"own tests" = the entry's `tests/test_contest_runner.py` + `tests/test_contest_cli.py` (29 + new).

## Scenario matrix

| # | scenario | sn67-var3 | sn68 | sn67-var | ling30-var1 | ling30 | sn67 |
|---|---|---|---|---|---|---|---|
| s01 | `run --help` lists the nine flags; bare `-m tools.contest` → usage, exit 2 | ✓ | ✓ | ✓ | ✓ | ✗ | ✗ |
| s02 | `--ticket 2` with 01 open → exit 1, 01 named "open too", no worktree built, no request to the server | ✓ | ✓ | ✓ | ✓ | ✗ | ✗ |
| s03 | the ticket is `queued` → exit 1, named | ✓ | ✓ | ✓ | ✓ | ✗ | ✗ |
| s04 | `--base no-such-ref` → exit 1, the ref named | ✓ | ✓ | ✓ | ✓ | ✗ | ✗ |
| s05 | dirty `epic-tasks/` → exit 1, named | ✓ | ✓ | ✓ | ✓ | ✗ | ✗ |
| s06 | `--ticket 2` + bad `--base`: **both** failures in one run | ✓ | ✓ | ✗ | ✗ | ✗ | ✗ |
| s07 | unreachable `server =` → exit 1, nothing built | ✓ | ✓ | ✗ | ✗ | ✓ | ✓ |
| s08 | one READY + one GAVE_UP with a commit: exit 0, the 7 plan facts (base as a sha), table JSON, `laguna.patch` + `mistral.GAVE_UP.patch`, `git am` reproduces the tree, `state.json`, no `kilo-serve.log`/`SUMMARY.md`/`entrants.json` | ✓ | ✓ | ✗ | ✗ | ✗ | ✗ |
| s09 | both GAVE_UP → exit 2; an agent with no commit writes no patch | ✓ | ✓ | ✓ | ✗ | ✗ | ✗ |
| s10 | tests on by default: a change that breaks the pinned test → `tests_failed`, the pytest tail in both rework prompts, `turns.jsonl` says so | ✓ | ✓ | ✓ | ✗ | ✗ | ✗ |
| s11 | the same change with `--no-tests` → READY | ✓ | ✓ | ✓ | ✗ | ✗ | ✗ |
| s12 | `max_parallel=2`, both agents commit at once, the sandbox test sleeps 1.2 s and logs its window: the two pytest windows never overlap | ✓ | ✓ | ✓ | ✗ | ✗ | ✗ |
| s13 | `--models x:free,y:free --max-parallel 1`: the plan lists `kenary/x:free`, `kenary/y:free`, nothing from the roster, parallel 1; `x.patch`, `y.patch` | ✓ | ✓ | ✓ | ✗ | ✗ | ✗ |
| s14 | `--resume` after `state.json` is edited back to mid-flight for one agent: exactly one new session, for that agent | ✓ | ✓ | ✓ | ✗ | ✗ | ✗ |
| s15 | `--no-gate` with an ask outside the worktree and an unroutable gate URL: one `gate-failed` reject, reason `gate unavailable: no gate model configured` | ✓ | ✓ | ✓ | ✗ | ✗ | ✗ |
| s16 | `--out DIR` honoured; no `contest-out/` in the repo | ✓ | ✓ | ✓ | ✗ | ✗ | ✗ |
| s17 | `contest.local.ini` next to the roster overrides `max_parallel` | ✓ | ✓ | ✓ | ✗ | ✗ | ✗ |
| s18 | the committed roster (`api_key = ${CONTEST_GATE_API_KEY}`), no such variable, no local ini, `--no-gate`: the round still runs | ✓ | ✗ | ✗ | ✗ | ✗ | ✗ |
| s19 | the default out dir is `<repo>/contest-out/<NN>`; the patch starts with `From ` | ✓ | ✓ | ✓ | ✗ | ✗ | ✗ |

## Where the points went

- **sn68** — s18. `cmd_run` calls `load_roster` before anything else and the
  committed `contest.ini` says `api_key = ${CONTEST_GATE_API_KEY}`;
  `load_roster` raises on an unset reference, so on this machine (no
  `contest.local.ini`, no exported key) `python3 -m tools.contest run
  --ticket 55` — even with `--no-gate` — dies at `intake: roster: [gate]
  api_key references ${CONTEST_GATE_API_KEY} …`. The ticket: "without a
  resolvable `api_key` the round still runs … this command adds no key
  check", and `live_smoke.py` line 39 shows how (`os.environ.setdefault`).
  Everything else is the cleanest code of the round (an `IntakeError`
  carrying every problem, `prepare_round` with an empty roster as the base
  check, `logging.basicConfig` so a live round prints its transitions).
- **sn67-var** — s06: `intake` returns at the first failure, the ticket
  asks for every failure at once; s07: `intake` calls the real
  `prepare_round`, so the worktrees are built before the server check
  fails; s08: the plan prints `base=HEAD` — the ref, not the sha
  `Intake.base_sha` carries; s18 as sn68.
- **ling30-var1** — no runner change at all: `run_round` has no `run_tests`,
  so every round is a `TypeError` after intake (and the plan says `tests:
  on` for tests that would never run).
- **ling30**, **sn67** — the `run_tests` half of the ticket only, as a raw
  diff of the branch (both `epic-tasks/` diffs are the base commit's own
  content against an older parent). Their runner change is the same shape
  as the winners' — lock, `_plan(run_tests=)`, `run_round(run_tests=)` —
  and their three runner tests pass; there is no command to score.

## Winner and the ideal

**Sensenova 6-7 var3** wins on the bench (19/19) and has the strongest tests
(20 CLI + 5 runner, including the resume-with-roots case and the no-key
case); it is the only entry whose command runs on this repo out of the box.
Its one miss is mechanical — no `AGENTS.md` line (the file is not tracked
at the base, which is why the others created a three-line stub).

The ideal (`kc16-ideal`) is sn67-var3 reworked, rebased onto KC-12
(`183de9b`, whose runner no longer has `_silence_watch`/`_WATCH_POLL`):

- `intake` checks the base through the public `prepare_round` with an
  empty roster (sn68's way) instead of importing `workspace._check_base_and_epic_tasks`;
  the `out_dir=` keyword is gone — `Intake.out_dir` is the default and
  `--out` replaces it in `cmd_run`, the ticket's signature verbatim;
- one parser: `cmd_run(args)` takes the Namespace `main` parsed, the
  duplicate `_run_parser` and the list-or-Namespace branch are gone (the
  tests call `cli.main(["run", …])`);
- `main` configures logging to stderr at INFO as `live_smoke.py` does —
  the runner's transition lines are the operator's only view of a live
  round;
- `AGENTS.md` is the real repository-guidelines file with the one line
  under *Build, Test, and Development Commands* (the file was untracked at
  the base);
- the module docstring is a third the length.

Kept: the winner's runner (`_TEST_RUNS_LOCK` around the whole `harvest`
call when `run_tests` is on, `_harvest` helper, `_plan(…, run_tests=)`),
`export_patches`, the plan's seven lines, the `CONTEST_GATE_API_KEY`
placeholder, and all 25 of its tests. Verified: the five new runner tests
fail on the base runner (4 red, 1 is the `run_tests=False` control);
19/19 scenarios; `tests` and `tests_bugfix` green sequentially.
