# KC-6 as a benchmark ticket — how the round was judged, and how to run it again

KC-6 (`epic-tasks/45-kc6-runner-prompt-wait-harvest-rework-in-the-same-session-for-n-agents.md`)
is the hardest ticket of EPIC KC: it joins four landed modules into a threaded
state machine, depends on a primitive that did not exist yet (KC-12), and has
nine Acceptance scenarios. That makes it the right ticket to hand to a live
multi-agent run once the KC epic is complete: the bench below scores any set
of worktrees black-box, in minutes, with no LLM and no `kilo` binary.

Everything here lives in `contest-bench/kc6/`:

| file | what |
|---|---|
| `setup_kc6.sh` | `*.patch` folder → one worktree per entry + `base`, outside the repo tree |
| `run_one_kc6.py` | one scenario, `cwd` = an entry worktree; builds its own sandbox + stub server |
| `run_all_kc6.py` | every scenario × every entry → `results.json` + the matrix |
| `entrants.json`, `results.json`, `RESULTS.md` | the 2026-09-19 round as it was scored |

## 1. The round as it was handed out

The tree every agent started from is commit **`f48a92c`**: KC-1…KC-5 landed,
KC-6 `open` in `epic-tasks/`, no `tools/contest/runner.py`. That commit is the
`base_ref` for any rerun — the ticket is unimplemented there, `next_task.py`
offers it, and the harvest (`tools/contest/harvest.py`) scores the worktree
against it. The bench itself lives in later commits; copy
`contest-bench/kc6/` to scratch (or check it out from `9dbb525`) when the
base tree is what the agents see.

The prompt each agent got is the runbook's Stage 1 block
(`docs/collect-epics/RUN-THE-EPIC-COMPETITION.md`), which is also what
`tools.contest.runner.round_prompt` sends in a live round.

## 2. Collect the patches

One `git format-patch <base>..HEAD` file per agent in one folder
(`kc6/` in this repo, never committed). Name them after the agent. Check
before anything else:

```bash
# same diff twice? (two KC-6 pairs were)
for a in kc6/*.patch; do for b in kc6/*.patch; do [ "$a" \< "$b" ] && cmp -s <(sed 1,3d "$a") <(sed 1,3d "$b") && echo "same: $a $b"; done; done
# how many commits, which files
grep -c '^From [0-9a-f]\{40\}' kc6/*.patch
grep -E '^ [^ ]+ *\| ' kc6/*.patch
```

## 3. Worktrees

```bash
S=/tmp/kc6-bench                      # anywhere outside the repo tree
contest-bench/kc6/setup_kc6.sh kc6/ $S/wt            # base f48a92c
contest-bench/kc6/setup_kc6.sh kc6/ $S/wt <other-sha>  # a different base
```

Prints one line per patch: the commit count (the ticket wants exactly one) or
`DOES NOT APPLY`. `$S/wt/base` is the unpatched base — the bench reads the
base tree's `tests/_kilo_fake.py` and `scripts/append_task.py` from it.

## 4. Static checks, per worktree

These are the ticket's self-check list, run by hand; every one of them cost a
KC-5 or KC-6 entry points:

```bash
for d in $S/wt/*/; do [ "$(basename $d)" = base ] && continue; ( cd $d
  echo "== $(basename $d): $(git rev-list --count f48a92c..HEAD) commit(s)"
  git diff --stat f48a92c..HEAD | tail -n +1                 # only File:/Also touches: + .smoke_tests
  python3 -c "import tools.contest.runner" && echo import-ok  # Python 3.10.12 on the judge
  python3 scripts/sync_test_tiers.py --check | tail -1
  git diff f48a92c HEAD -- tools/auto/collect_bridge.py | wc -l  # must be 0
  grep -c 'def test_' tests/test_contest_runner.py
  grep -n 'kilo serve\|localhost:[0-9]\{4\}' tests/test_contest_runner.py  # must find nothing
); done
```

## 5. Each entry's own tests (not the score — a sanity column)

```bash
for d in $S/wt/*/; do [ "$(basename $d)" = base ] && continue
  (cd $d && echo "== $(basename $d)" && timeout 600 python3 -m pytest tests/test_contest_runner.py -q --timeout=180 -p no:cacheprovider 2>&1 | tail -2)
done
```

An entry's own tests prove what its author thought of; in KC-6 all seven had
tests and none of them caught any of the defects the bench found.

## 6. The bench

```bash
python3 contest-bench/kc6/run_all_kc6.py --wt $S/wt --out $S/results.json          # everything
python3 contest-bench/kc6/run_all_kc6.py --wt $S/wt --out $S/results.json sn68v1   # one entry
python3 contest-bench/kc6/run_all_kc6.py --wt $S/wt --out $S/results.json s30_stall_with_a_chatty_neighbour  # one scenario, all entries
```

About 40 s per entry when nothing else runs; **do not run two bench
processes at once** — s14/s22/s26/s30 assert wall-clock bounds. The last
line of every run is `PASS`, `FAIL <why>` or `ERROR <traceback tail>`, and
`results.json` keeps them. To see one failure in full:

```bash
cd $S/wt/<entry> && python3 /path/to/contest-bench/kc6/run_one_kc6.py s30_stall_with_a_chatty_neighbour $S/wt/base
```

Run the bench on `base` too: every scenario must `ERROR` there (no module) —
that is the "red without the change" check for the bench itself.

### The stub server

`run_one_kc6.py::bench_fake` subclasses `tests/_kilo_fake.py::FakeKiloServer`
(the scripted `kilo serve` from KC-1). A scenario is one dict shared by every
session; `on_prompt(directory, text)` runs in the fake before the turn's
events and is where the "agent's work" happens — the bench commits into the
worktree and writes `runs/<agent>/PROGRESS.csv` through the real
`scripts/append_task.py`. Knobs, per turn:

| key | what the session does |
|---|---|
| `events: ["busy", "file.edited", "idle"]` | the recorded event sequence; `idle` is held until permission/question are answered |
| `permission: {...}` | one `permission.asked`; the turn blocks until the runner replies |
| `questions: N` | *(bench)* N `question.asked` in a row, each waits for `reject` |
| `delay: 1.0` | seconds before `session.idle` |
| `idle: false` | never goes idle (a stall; the turn timeout or the silence clock must end it) |
| `error: {...}` | `session.error` instead of idle |
| `tool_parts`, `assistant`, `info`, `diff` | what `GET /session/{id}/message`, `/session/{id}`, `/diff` return |
| top-level `bad_models: {"m:free"}` | *(bench)* `POST /session` answers 400 for that model |
| top-level `session: {"cost": …, "tokens": {…}}` | what `session_info` reports |

The fake **broadcasts every event to every open `/event` stream**, whatever
`directory` the tap asked for. That is the property behind s30: a runner
whose silence clock counts any tap event never stalls a silent agent while a
neighbour is chatty. A real server with several directories on one stream
behaves the same way; count the session's own events.

Ad-hoc shapes are done by patching the instance (see s30: `fake._run_turn`,
`fake._permission_event`, `fake._record_request` are swapped per test); the
same subclass exists as `_BenchFake` in `tests/test_contest_runner.py` with
a `pulse()` helper for "busy every N ms".

### Adding a scenario

Add a `@scenario` function to `run_one_kc6.py` (they are discovered by name,
`sNN_` prefix keeps the order), build a `Sandbox(tmp, agents)`, script the
fake, call `AgentHarness(...).go()` for `run_agent` or `_round(...)` for
`run_round`, and `check(...)` the contract: the returned `AgentRun`/
`RoundState`, `out_dir/state.json`, `out_dir/<agent>/turns.jsonl`,
`decisions.jsonl`, `<agent>.session.json`, and the fake's request/event log
(`fake.calls(...)`, `fake.events_of(...)`, `prompts(fake)`). Only the
ticket's public names — the bench must not know how an entry is built.
Re-run the base (must `ERROR`) and the ideal (must `PASS`), then everyone.

## 7. One test file on every entry

The ideal's `tests/test_contest_runner.py` is the bench in pytest form with
exact shapes. To run it inside each entry (a second, stricter column):

```bash
for d in $S/wt/*/; do e=$(basename $d); [ $e = base ] && continue
  cp tests/test_contest_runner.py $d/tests/test_kc6_final.py
  (cd $d && timeout 900 python3 -m pytest tests/test_kc6_final.py -q --timeout=120 -p no:cacheprovider 2>&1 | tail -1 | sed "s/^/$e: /")
  rm $d/tests/test_kc6_final.py
done
```

(Copy under a new name: the file's `REPO_ROOT` is its own parent's parent,
so it imports the entry's runner, not the ideal's.)

## 8. From the matrix to the ideal

1. Rank by the bench, not by the entries' tests or line counts.
2. Read the code of the top group only, and only after the matrix — what
   they did differently on the scenarios that split them.
3. Write the ideal from the ticket with the bench as the spec (in KC-6 the
   result was a fresh write closest to the winner's structure, 622 lines
   against 837–1197), run it through the bench (must be 28/28) and through
   the ticket's own self-check list.
4. Branch `kc<N>-ideal` off the round base; commits in this order:
   the ideal (one commit, `File:` + `Also touches:` + `.smoke_tests/` link
   only), the bench record (`contest-bench/kc<N>/`), the round flip
   (`epic-tasks/`: this ticket `landed <sha>` with the one-paragraph verdict,
   the next ticket's `Status:` updated with what it must now know).
5. `python3 scripts/next_task.py --tasks epic-tasks --status` must offer only
   the next round. `kc` fast-forwards to the ideal branch; the operator pushes.

## 9. Using KC-6 to test a live multi-agent run

Once KC-7 (`python3 -m tools.contest run`) and KC-9 exist:

```bash
python3 -m tools.contest run --ticket 45 --base f48a92c --config contest.ini --out contest-out/kc6-live
```

hands the very same ticket to the roster in one session each, harvests,
reworks and exports one patch per agent. Score them with this folder:

```bash
contest-bench/kc6/setup_kc6.sh contest-out/kc6-live/patches $S/wt f48a92c
python3 contest-bench/kc6/run_all_kc6.py --wt $S/wt --out $S/live-results.json
```

Compare against `results.json` here: the same models, the same ticket, the
same 28 scenarios — the difference between the hand-run round of 2026-09-19
and the runner's round is the runner's own score. What to look at first:
`s30` (the concurrency edge nobody's own tests caught), `s26` (Ctrl-C), and
the static checks in §4 — a live agent that stacks commits or edits
`kilo_client.py` fails the harvest before the bench sees it.
