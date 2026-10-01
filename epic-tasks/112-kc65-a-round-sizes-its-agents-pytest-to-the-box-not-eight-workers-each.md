# KC-65 — a round sizes its agents' pytest to the box, not eight workers each

**Status:** landed `23db0b3` (2026-09-25) — round 112, ideal patch from the second machine; found 2026-09-25 in round 107 (`--fresh` re-run): load average 68 on an 8-core box while the CPU sat 36–64 % idle. The box was not attacked and no log was flooding: the agents' own test runs had put 55 processes in uninterruptible disk wait.
**Severity:** MEDIUM (every agent's test run slows to a crawl; `--timeout=300` fails fire on healthy code and an agent "fixes" what is not broken; the operator's box is unusable for the round)
**File:** `tools/contest/cli.py`, `tools/contest/runner.py`, `tools/contest/backend.py`, `tools/contest/roster.py`, `contest.ini`, new `tools/contest/pytest_plugin/contest_pytest_workers.py`
**Symbol:** `cmd_run` (the `env` it hands `_make_backends`), `run_round.save`, `OpenRouterBackend` agent spawn, `CONTEST_KEYS`
**Round:** 112
**Size:** S
**Source:** round 107, measured live at 14:29 local:

    load average: 68,58, 66,86, 58,72        nproc = 8
    vmstat: us 25–48  sy 3–8  id 36–64  wa 8–10
    process states: 55 D, 2 R
    D-state wait channels: 27 git f2fs_create, 15 python3 f2fs_do_truncate_blocks,
                           7 f2fs_mkdir, 3 f2fs_issue_checkpoint, …
    iostat: the disk under the root file system at 94.8 % util, 710 w/s
    /proc/pressure/io: some avg60=41.97  full avg60=37.65

58 `pytest-xdist` workers were alive, from 9 pytest runs in 7 worktrees:

    107-step-3-7-flash                 pytest tests/ --timeout=300 -q      8 workers
    107-sensenova-6-8-flash-lite-var2  pytest tests/test_contest_runner.py 8
    107-sensenova-6-8-flash-lite-var2  pytest tests_bugfix -n 4            4
    107-agnes-2-5-flash                pytest .smoke_tests/ -n auto        8
    107-mimo-v2-5                      pytest .smoke_tests/ -x -q          8
    107-sensenova-6-7-flash-lite-var1  pytest tests/test_contest_cli.py -n 2   2
    107-agnes-2-0-flash                pytest tests_bugfix -n 4            4
    107-agnes-2-0-flash                pytest .regression_tests/ -q        8
    107-sensenova-6-8-flash-lite-var1  pytest tests/test_contest_*.py      8

The D-state `git` processes were all under
`$TMPDIR/pytest-of-<user>/pytest-NNN/popen-gwN/<test>/repo` — the suite's
`tmp_path` git fixtures, created by the thousand.

**Why it happens.** Two multipliers stack:

1. `pytest.ini` carries `addopts = -n auto`. Every pytest an agent types —
   even one test file — starts one worker per core. N live agents × 8
   workers on 8 cores is N-fold oversubscription before a single test runs.
   Agents cannot know their neighbours, so they cannot size it themselves;
   one agent even ran `uptime; nproc` to find out why its run was slow.
2. On this box the temp dir was the root file system: f2fs on a USB card
   reader, no `tmpfs` on `/tmp`. The suite's git-in-`tmp_path` fixtures
   became tens of thousands of small synchronous writes to that device.
   The operator is moving `/tmp` to `tmpfs` in `/etc/fstab`; that is a
   property of the box, **not** something the round may assume or change.

Load average counts `D` state, so the number looked like a runaway; the
real limit was the disk queue, then the CPU once the disk was fast.

**Depends on:** KC-35 (the `env` the round's `kilo serve` is spawned with).
**Also touches:** KC-62 (the other shared-box resource — Kilo's SQLite — that a crowd of processes chokes), FL-7 (`pytest.ini` comments on how agents invoke the suite).

---

## What must change — file by file

Line numbers are at `97e8fd5`. Find by the quoted anchor if they drifted.

Every Kilo agent's bash runs as a child of the one `kilo serve` the round
shares, so an environment variable is set **once**, when the server spawns,
and cannot change afterwards. A fixed number would be wrong for most of the
round: 10 agents on 8 cores need 2 workers each, but when only 2 are still
running they could have 4, and the last one 8. So the environment carries
**where to read** the number, not the number:

- the runner keeps one file, `<out_dir>/pytest-workers`, holding the
  count for the number of `live` agents — those that hold a pool slot now
  (not `CREATED`, not terminal):

      live 1      → cores                  (the last one gets the box)
      live 2..4   → pytest_workers_few  = 4
      live 5+     → pytest_workers_min  = 2

  always capped at `cores`, never below 1;
- a tiny pytest plugin, shipped with the runner and loaded through the
  environment, reads that file when each pytest starts and gives xdist the
  number for `-n auto`.

A pytest already running keeps its workers; the next one an agent starts
gets the new number. An agent that stopped and is sent a `continue`, a
`REWORK` or a `--resume` prompt reads the file on its next pytest like
everyone else, so it always runs with the count of that moment.

The floor is **2**, never 1: with one worker the full `tests` root runs for
over half an hour, and an agent's own `--timeout` fires on healthy code. At
10 live agents on 8 cores that is 20 workers — 2.5× the cores instead of
today's 10×; at 4 live agents, 16.

The plugin is what holds the number; the prompt (§5) also names it, so an
agent does not type `-n 8` by habit and understands a slow run.

### 1. `tools/contest/roster.py` + `contest.ini` — the keys

**Where:** `CONTEST_KEYS` (≈ line 91, next to `"progress_every_sec"`),
`ContestConfig` (≈ line 235), `_build` (≈ line 533).

**Add:**

    #: KC-65: pytest-xdist workers per agent. 0 = auto by live agents:
    #: 1 → cpu_count, up to pytest_workers_few_agents → pytest_workers_few,
    #: more → pytest_workers_min; always at most cpu_count.
    pytest_workers_per_agent: int = 0
    #: KC-65: workers each while at most `pytest_workers_few_agents` are live.
    pytest_workers_few: int = 4
    pytest_workers_few_agents: int = 4
    #: KC-65: workers each above that; 1 is allowed, 0 is rejected.
    pytest_workers_min: int = 2
    #: KC-65: temp dir for agents' commands, `${VAR}` expanded; "" = inherit TMPDIR.
    agent_tmpdir: str = ""

`contest.ini`, after `progress_every_sec`, each with a one-line comment as
the file does:

    # pytest-xdist workers each agent gets; 0 = by live agents (below), re-read by every pytest.
    pytest_workers_per_agent     = 0
    # Up to this many live agents each gets pytest_workers_few; one alone gets every core.
    pytest_workers_few_agents    = 4
    pytest_workers_few           = 4
    # More live agents than that: this many each. Below 2 a full suite runs over half an hour.
    pytest_workers_min           = 2
    # Temp dir for the agents' commands (a tmpfs is best); empty = inherit TMPDIR.
    agent_tmpdir                 =

No path is written into the committed `contest.ini`; an operator who wants
one puts it in `contest.local.ini`.

### 2. `tools/contest/cli.py` — the round's environment

**Where:** `cmd_run`, the block that builds `env` (≈ line 1365, anchor
`# KC-35: the overlay intake proved in its throwaway`).

**How:** build `env` as a dict always (`{}` instead of `None`), keep the
`KILO_CONFIG_CONTENT` entry exactly as now, then add:

- `CONTEST_PYTEST_WORKERS_FILE` = `<out_dir>/pytest-workers` (absolute).
  Write the file **before** the server spawns, with the start value
  `workers(min(max_parallel, live agents))` (the count below), so the first
  prompts already read a sane number.
- `PYTEST_PLUGINS` = `contest_pytest_workers` (§3) and `PYTHONPATH` =
  `<runner>/tools/contest/pytest_plugin` **prepended** to the inherited
  `PYTHONPATH` (kept, `os.pathsep`-joined). Only that one directory, never
  the runner's repo root: the agents' repo is this repo, and a root on
  `PYTHONPATH` would make an agent's pytest import the runner's `tools`
  instead of the code in its own clone.
- `PYTEST_XDIST_AUTO_NUM_WORKERS` = the same start value, as the fallback
  xdist ≥ 3.2 reads by itself (installed: 3.8.0) if the plugin cannot load.
- Count the **round's agents**, not `config.agents`: on `--resume` the
  agents come from `state.json` and may be more than the command line names
  (round 110 resumed 10 agents, two of them not on the command line). A
  `Workspace` carries no state, so the count is taken from the runs:
  `resume.agents` minus those whose `state` is terminal (a resumed round
  whose 8 agents already ended has 2 live, not 10); without `--resume`
  it is `len(config.agents)`, one workspace per agent.
- Order in `cmd_run`: today `_print_plan` (≈ line 1364) runs **before** the
  `if args.resume:` block (≈ line 1373), so the plan cannot know the count.
  Move the `state.json` read (the `if args.resume:` half only — it only
  reads a file) above `_print_plan`; `prepare_round` stays where it is,
  after the plan. A `--resume` whose `state.json` is missing or unreadable
  then fails before the plan is printed, as it already fails before any
  server starts.

An explicit `-n 4` typed by an agent still wins — that is fine; `-n auto`
from `pytest.ini` is what multiplies.
- if `agent_tmpdir` is set: `TMPDIR`, `TEMP`, `TMP` = the resolved path
  `<agent_tmpdir>/contest-<ticket>`; create it (`mkdir -p`, mode 0700)
  before the server spawns. If it cannot be created, intake fails with
  the path in the message — the round does not silently fall back.

Pass `env or None` on, so an empty dict still spawns "exactly as before"
per KC-35's contract. Print the start worker count, its rule (`auto, min 2`
or `fixed N`) and the temp dir in `_print_plan` (≈ line 1299,
next to `("parallel", …)`) so the operator sees them before the round.

`--resume` must rebuild the same env (it goes through the same block).

The harvest's own roots are **not** under this env: `run_tests_detail` runs
in the runner process, not under `kilo serve`, and keeps `-n auto`. Say so
in a comment, so nobody "fixes" it here (the harvest's budget is KC-57's).

### 3. `tools/contest/pytest_plugin/contest_pytest_workers.py` — new, the reader

One module, alone in its directory (see §2), standard library plus `pytest`:

    @pytest.hookimpl(tryfirst=True, optionalhook=True)
    def pytest_xdist_auto_num_workers(config):
        # read CONTEST_PYTEST_WORKERS_FILE; an int >= 1 is returned,
        # anything else (unset, missing, empty, not a number) returns None

- `tryfirst`: xdist's own implementation of this `firstresult` hook reads
  `PYTEST_XDIST_AUTO_NUM_WORKERS`; ours must answer before it.
- `optionalhook`: without it, a pytest run with `-p no:xdist` or without
  xdist installed fails with "unknown hook" — the plugin must never break a
  run it has nothing to say to.
- `None` hands the question on to xdist, i.e. to the fallback variable and
  then to the core count. Never raises.

### 4. `tools/contest/runner.py` — the file follows the round

**Where:** `run_round`, `save()` (≈ line 1858) — it already runs after every
transition of any agent, under the round's `lock`.

**How:** after `state.json`, rewrite `pytest-workers` from the same `state`:
`live` = agents whose state is neither `CREATED` nor terminal (a queued
agent holds no slot; a `HARVESTING` one does, it may go back to work).
Atomic (temp file + `os.replace`), only when the number changed, and only
when `pytest_workers_per_agent` is 0 — a fixed count is written once by
the CLI and never touched. A write that fails is a log line, never a stop.

### 5. `tools/contest/runner.py` — every prompt names the count

**Where:** `run_agent`, the one send site (≈ line 1505, anchor
`backend.prompt(session, text)`); it carries the first prompt, `continue`,
`REWORK` and the `--resume` prompt alike.

**How:** read the current `pytest-workers` value and append one line to
*text* just before the send:

    The box is shared: right now `-n auto` gives your pytest N workers.
    Do not pass a larger `-n`; a run is slower when many agents test at once.

Not when the file cannot be read (no line, no guess), and not when
`pytest_workers_per_agent` is fixed and equal to the core count. The line
is the last one of the message, so KC-22's "only the first prompt carries
it" and every existing prompt test that checks the start of a message stay
as they are.

### 6. `tools/contest/policy.py` — the new temp dir is scratch

**Where:** wherever `tmp_roots` reaches `PolicyContext` (`policy.py`
≈ line 352, anchor `tmp_roots: tuple = ()`).

**How:** when `agent_tmpdir` is set, append `<resolved>/*` to the round's
effective `tmp_roots`. Otherwise an agent that writes to `$TMPDIR` gets a
policy ask/deny it did not get under `/tmp`.

### 7. `tools/contest/backend.py` — the OpenRouter agent gets the same env

**Where:** the agent spawn (≈ line 656, anchor
`env[_AGENT_BASH_TIMEOUT_ENV] =`).

**How:** the backend takes an `extra_env: dict | None` at construction
(from `_make_backends`) and applies it after `dict(os.environ)`. No other
change there.

### 8. `tools/contest/runner.py` — clean up

At round end (the same place the round closes its server), remove
`<agent_tmpdir>/contest-<ticket>` if the runner created it. Never remove
the parent `agent_tmpdir`. `pytest-workers` stays in `out_dir` with the
round's other records.

---

## Tests — where they go

`tests/test_contest_cli.py` (or a new `tests/test_contest_agent_env.py`):

- 8 cores (monkeypatch `os.cpu_count`), `max_parallel = 6` → the start
  file holds `2` and the env handed to `_make_backends` carries
  `CONTEST_PYTEST_WORKERS_FILE`, `PYTEST_PLUGINS`, the plugin directory
  first on `PYTHONPATH` (the inherited value kept after it) and
  `PYTEST_XDIST_AUTO_NUM_WORKERS == "2"`.
- `max_parallel = 16`, 8 cores → `2`, not `0` or `1`;
  `pytest_workers_min = 1` → `1`.
- 8 cores, live 1 / 2 / 4 / 5 → `8` / `4` / `4` / `2`; 2 cores, live 3 →
  `2` (capped at the cores, never above).
- `pytest_workers_per_agent = 3` → `3` whatever the cores, and `run_round`
  never rewrites it.
- 2 agents, `max_parallel = 5` → counts 2, not 5 → `4`.
- `--resume` with a `state.json` of 6 non-terminal agents, command line of
  2, `max_parallel = 8`, 8 cores → `2` (counts the resumed 6).
- `PYTHONPATH` does not contain the runner's repo root.

`tests/test_contest_runner.py` (fake backend, 8 cores, defaults):

- 6 agents, `max_parallel = 6`: file is `2`; two go `READY` → `4`; one
  more → `4`; down to one live → `8`; a queued `CREATED` agent does not
  count as live.
- every prompt sent (first, `continue`, `REWORK`, resume) ends with the
  line naming the file's value of that moment; a `continue` sent after
  others finished names the new, larger number; no line when the file is
  unreadable.
- 10 agents, `max_parallel = 8` → `2` while 8 run; the file never holds
  more than `cores`.
- an unwritable `out_dir` for the file → the round goes on, one log line.

`tests/test_contest_pytest_workers_plugin.py` — a `pytester` run each:

- the file says `3` → `-n auto` starts 3 workers;
- file missing, empty or `abc` → the hook returns `None` and xdist's own
  answer stands (the fallback variable is honoured);
- `-p no:xdist` with the plugin loaded → the run passes (no "unknown hook").
- `agent_tmpdir = ${X}` with `X=tmp_path` → `TMPDIR` = `tmp_path/contest-NN`,
  the dir exists, and `tmp_roots` gains its glob.
- `agent_tmpdir` unset → no `TMPDIR` key in env; `KILO_CONFIG_CONTENT`
  handling is byte-for-byte as before (existing KC-35 tests stay green).
- unwritable `agent_tmpdir` → intake fails naming the path.

`tests/test_contest_roster.py`: the keys parse, reject negatives (and `pytest_workers_min = 0`, `pytest_workers_few = 0`), and
`${VAR}` expands in `agent_tmpdir`.

**Every new test is hermetic against the round's own env.** Agents run this
suite *inside* a round, where `CONTEST_PYTEST_WORKERS_FILE`,
`PYTEST_PLUGINS`, `PYTEST_XDIST_AUTO_NUM_WORKERS`, the plugin's
`PYTHONPATH` entry and (with `agent_tmpdir`) `TMPDIR` are already set. A
test that checks "file missing → xdist's own answer", "no `TMPDIR` key" or
"`PYTHONPATH` starts with the plugin dir" must `monkeypatch.delenv` /
`setenv` each of those first, or it passes on the operator's box and fails
in every agent's run (or the reverse). Add one test that runs the plugin
tests with those variables set to junk and expects them green.

- `--resume` with a `state.json` of 10 agents, 8 of them terminal,
  `max_parallel = 8`, 8 cores → `4` (2 live), not `2`.
- `--resume` with no `state.json` → exit 1, and the plan is not printed.

Tier with `python3 scripts/sync_test_tiers.py`.

## Out of scope

- Editing `/etc/fstab` or mounting anything — the box is the operator's.
- Changing `pytest.ini`'s `-n auto` for people running the suite by hand.
- Throttling how often agents run the suite.

## Acceptance

- A round with 7 live agents on 8 cores gives each `-n auto` run 2 workers
  (`ps` count of `exec(eval(sys.stdin` children per pytest); when 4 or fewer
  are left, a newly started run gets 4; the last one alone gets 8.
- An agent's next prompt names the count of that moment.
- `_print_plan` shows the worker count and the temp dir.
- With `agent_tmpdir` unset, the round's `kilo serve` environment differs
  from today's only by `CONTEST_PYTEST_WORKERS_FILE`, `PYTEST_PLUGINS`,
  the one `PYTHONPATH` entry and `PYTEST_XDIST_AUTO_NUM_WORKERS`.
- `python3 -m pytest .smoke_tests/ .regression_tests/` and
  `python3 -m pytest tests_bugfix -n 4 -q` pass;
  `CollectBridge._shrink` byte-identical.
