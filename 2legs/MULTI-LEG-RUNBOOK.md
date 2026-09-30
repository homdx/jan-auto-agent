# Any task on any repository — a multi-leg contest round

**One page for the operator.** Point the contest at another git repo with a
plain-words brief, let every model work in relays of fresh sessions
(`--legs N`), then pick the results. Two kinds of brief:

| kind | command | ready-made brief |
|---|---|---|
| bug hunt (find, test, fix) | `prepare_task.sh REPO "brief" NN` | your one line |
| free text — the brief is the task | `prepare_task.sh --free REPO @file NN` | `coverage-brief.txt`, `docs-brief.txt` |

**Every task must add or change a file under `tests/`** — harvest blocks an
entry without one (`no_test_file`). The coverage brief does this by nature; the
docs brief asks for a test that the docs name only paths and flags that exist.
Write the same kind of requirement into your own free briefs.

Everything here runs on the current code: `--target` (KC-76), `--legs` (KC-43),
the leg record (KC-74). Checked with `--dry-run` on 2026-09-30.

---

## 0. What you need

| what | note |
|---|---|
| this checkout | `tools/contest` lives here; run the commands from here |
| target repo | a git repo with **pytest tests under `tests/`**, clean working tree |
| `contest.ini` | the roster; `--models` picks from it or names new ones |
| gate model | must not be one of the competing models, else pass `--no-gate` |

## 1. Prepare the target (once per task)

```bash
2legs/prepare_task.sh /path/to/repo "Find a real bug in the parser module" 1
2legs/prepare_task.sh --free /path/to/repo @2legs/coverage-brief.txt 2
2legs/prepare_task.sh --free /path/to/repo @2legs/docs-brief.txt 3
```

What it does, on a **new branch `contest-legs`** of the target (your branch is
untouched, `git -C /path/to/repo checkout -` goes back):

- copies `scripts/next_task.py`, `scripts/append_task.py` (the agents run them);
- writes `epic-tasks/NN-hunt.md` or `NN-free.md` — an open ticket carrying your brief, with
  `**File:** —` so no file counts as off-ticket;
- parks every other open ticket there as `queued` — intake only runs the
  lowest open one;
- commits it all. Intake refuses an uncommitted `epic-tasks/`.

The brief is free text. Narrow is better: "the date parsing in `utils/time.py`"
finds more real bugs than "the whole repo".

## 2. Dry run (no model is called)

```bash
python3 -m tools.contest run --ticket 1 --target /path/to/repo --legs 2 \
    --models provA/model-a,provB/model-b --dry-run
```

Look for `legs 2 (flag) — 01.1 … 01.2` and no `intake:` refusals.

## 3. The round

```bash
python3 -m tools.contest run --ticket 1 --target /path/to/repo --legs 2 \
    --models provA/model-a,provB/model-b,provC/model-c --fresh
```

- **Leg 1** — each model does the brief (hunt: search, failing test, fix) and commits.
- **Leg 2** — only for models not yet `READY`: a **new session** in the **same
  worktree**, handed leg 1's record (diffstat, end commit, harvest verdict,
  tests, last message) and told to continue, not restart.
- Use `--legs 3` for a large or slow repo. A `READY` leg ends that model's relay.

Watch it from another terminal:

```bash
python3 -m tools.contest status --ticket 1 --out /path/to/repo/contest-out/01.2
```

Output lands in `/path/to/repo/contest-out/01.1/`, `01.2/` — the last leg holds
the patches, `entrants.json` and `SUMMARY.md`.

## 4. Pick the results (by hand — there is no shared bench for a free task)

For a hunt every entry found **its own** bug; for coverage or docs each chose
its own modules. So the score table only says the entry is
well-formed (test present, one commit, suite green). For each `READY` patch:

1. **Hunt: is it real?** Revert the fix, keep the test: it must fail. Then read it —
   drop style fixes, "add error handling", tests that assert the new behaviour
   without a defect behind it.
2. **Duplicates** — two models often find the same bug; keep the smaller fix.
3. Land each kept patch on its own branch of the target:
   ```bash
   git -C /path/to/repo checkout -b fix/<name> <your-branch>
   git -C /path/to/repo am /path/to/repo/contest-out/01.2/<agent>.patch
   python3 -m pytest   # in the target
   ```
4. **Coverage / docs:** compare the "after" numbers in `docs/COVERAGE.md` or the
   item count in `docs/DOCS-AUDIT.md`, then read the tests — asserts on real
   behaviour beat tests that only run lines.

## 5. Next rounds — from findings to fixes

A bug found but not fixed well (red suite, a fix too big) becomes an ordinary
ticket. Write `epic-tasks/02-<slug>.md` on `contest-legs` in the same format,
with **real** `**File:**` / `**Symbol:**` lines and the failing input in the
text, commit it, and run `--ticket 2 --legs 2`. Now all models fix the **same**
bug, and you can score them with your own tests, the way `contest-bench/` does.

A second hunt: `2legs/prepare_task.sh /path/to/repo "another area" 4`.

## Known limits

- The agents' prompt still carries jan-auto-agent's own ground rules
  (`CollectBridge._shrink`, `agents_128k.ini`); on another repo they are noise
  the agents ignore.
- Harvest runs pytest; a repo without pytest tests gets `no_test_file` /
  red verdicts for every entry.
- One bug per entry by design: a hunt round is N models → up to N findings.
- A free task that needs no test (pure prose) cannot end READY; its legs run out
  and the last one is harvested as REWORK. Read its patch anyway.
