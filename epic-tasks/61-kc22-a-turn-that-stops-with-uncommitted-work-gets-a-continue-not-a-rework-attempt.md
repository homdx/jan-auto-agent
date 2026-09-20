# KC-22 — a turn that stops with uncommitted work in the tree gets a `continue`, not a harvest and a rework attempt

**Status:** open — round 61 of EPIC KC (`docs/kilo-contest/EPIC-KC.md`); found live on 2026-09-20 in the third manager-run round (`python3 -m tools.contest run --ticket 52 --models …`, base `5ee8417`). Same file as KC-19 and KC-21 (`runner.py`, the end of a turn in `run_agent`) — adjacent, not overlapping: KC-19 is `session.error`, KC-21 is a terminal state with a commit, this ticket is `idle` with **no** commit and a dirty tree. Whichever lands later rebases.
**Severity:** HIGH
**File:** `tools/contest/runner.py` (`run_agent` — after `_wait_turn` returns `idle`, before `HARVESTING`; `round_prompt` for the resumed case), `tools/contest/roster.py` (one `[contest]` key), `contest.ini`
**Symbol:** `run_agent`, `continue_message`, `round_prompt`, `ContestConfig.max_continues_per_attempt`
**Round:** 61
**Size:** S
**Source:** round 52 run 3, `mimo-v2-5:free`, turn 0: 2 minutes of work — `policy.py` and `test_contest_policy.py` edited, own tests run — then the stream ended on the text `Let me first check the base commit and understand the file state:` and the session went `idle`. Nothing was committed. The runner did what KC-6 says: `HARVESTING` (8 minutes of both pytest roots on a tree with **zero** commits, under `_TEST_RUNS_LOCK`, while two other agents queued), `REWORK — attempt 1 — no_progress_row, commits_ne_1, pushed, no_test_file`, and a rework prompt whose first bullet reads `0 commits on contest/52/mimo-v2-5 for … — amend them into exactly one commit`. The model then finished in one more turn and was `READY 8f19de7` — the rework had served as a plain "go on", at the price of one of the two rework attempts and eight minutes of pytest that could not have scored anything. The same shape ended `mistral-medium-3-5:free` in run 1 (`GAVE_UP` after three turns, never committed). A model that stops mid-work — a provider cutting the stream, a model that ends its message to "check in", a model that writes *Shall I continue?* — is not a model that handed in a wrong entry; it is a model that has not handed in yet.
**Depends on:** KC-6 (`run_agent`, landed `e8c6ad3`), KC-16 (`--resume`, landed `1304950`), KC-18 (transition lines, landed `9c4db8b`).
**Also touches:** `tests/test_contest_runner.py`, `tests/test_contest_roster.py`

---

## What happens today

```python
            idle = _wait_turn(client, tap, session, config, ...)
            ...
            # ── HARVESTING ─────────────────────────────────────────────────
            transition(AgentState.HARVESTING, note="tests on" if run_tests else "tests off")
            verdict = _harvest(ws, ticket_path, run_tests)
```

Every `idle` is harvested, and every harvest that is not `READY` costs an
attempt (`max_rework = 2`). A turn that ends with edits in the tree and no
commit fails every hard gate by construction (`no_commit`/`commits_ne_1`,
`no_progress_row`, `no_test_file`), so the harvest's answer is known before
it runs — and the pytest roots it runs measure an unfinished tree.

Under `--resume`, `_runs_for` keeps a dirty non-READY worktree and starts a
**fresh session** on it with the unchanged `round_prompt`, which says
nothing about the work already there: the new session may start over, or
`git checkout -- .` it away.

## What must change

1. After `_wait_turn` returns `idle` — and only then; `STALLED`/`ERROR` keep
   their branches (KC-19, KC-21) — the runner looks at the tree **before**
   harvesting: `commits = git rev-list --count <base_sha>..HEAD` and
   `dirty = git status --porcelain --untracked-files=all` (excluding
   `runs/`). When `commits == 0` **and** `dirty` is non-empty **and** the
   turn's continue counter is below `config.max_continues_per_attempt`,
   the runner does **not** harvest: it sends `continue_message(dirty)` into
   the same session, records the turn with `kind = "continue"` and
   `idle_status = "idle"`, leaves `run.attempt` unchanged, and loops back to
   `PROMPTED`. The KC-18 line reads
   `mimo-v2-5: PROMPTED — attempt 0 (continue 1 of 2)`.
2. `continue_message(dirty: str) -> str` (module-level, next to
   `round_prompt`): a short text — "Your turn ended before anything was
   committed. The worktree still holds your uncommitted work:" + the
   `git status --porcelain` lines indented + "Finish the ticket in this same
   worktree: one commit, the test, `append_task.py` with the commit's sha.
   Do not start over and do not discard these files." No ticket text
   repeated (the session has it).
3. The counter is per attempt: a rework resets it. When it is exhausted the
   turn falls through to today's path (`HARVESTING` → `REWORK`/`GAVE_UP`) —
   a model that never commits still ends in bounded time
   (`max_rework × (1 + max_continues_per_attempt)` turns at most).
4. A clean tree with no commit (the model did nothing) is **not** a
   continue: it harvests and reworks as today — the rework text is the right
   answer for "you did nothing", and `test_run_exits_two_when_every_agent_gave_up`
   depends on it.
5. `--resume`: in `_runs_for`, a mid-flight worktree whose harvest is not
   `READY` and whose tree is dirty (same `dirty` check) is restarted with the
   `round_prompt` **plus** the `continue_message(dirty)` paragraph appended —
   the fresh session learns on its first prompt that the work is there.
   `round_prompt(agent_name, ticket_path, base_sha, *, dirty: str = "")`
   keeps its shape for every existing caller.
6. `[contest] max_continues_per_attempt = 2` in `contest.ini` (comment: "a
   turn that ends idle with edits but no commit is nudged on this many
   times per attempt before it is harvested; 0 = off"), read by
   `load_roster` like `max_questions_per_turn`, default `2`, `0` disables
   the whole mechanism (today's behaviour byte-for-byte).
7. `AgentRun`'s JSON shape (KC-6) is unchanged: the continue turns are
   ordinary entries of `turns` with `kind = "continue"`; `state.json` and
   `turns.jsonl` carry them.

## Acceptance

- [ ] `tests/test_contest_runner.py`, against the fake (`tests/_kilo_fake.py`
      `on_prompt` playing the agent inside the worktree):
      - turn 0 edits a file and goes idle without committing; turn 1 commits
        the entry and writes the row → `READY`, `run.attempt == 0`,
        `[t["kind"] for t in run.turns] == ["initial", "continue"]`, the
        second prompt the fake received contains the edited file's name and
        the words `uncommitted`, and `_harvest` ran **once** (monkeypatch a
        counter);
      - a model that edits and never commits, `max_continues_per_attempt = 1`,
        `max_rework = 1` → turns are `initial, continue, rework, continue`
        and the run ends `GAVE_UP`; the harvest ran twice;
      - a turn that goes idle on a **clean** tree → no continue: harvested at
        once, `REWORK` (`test_run_exits_two_when_every_agent_gave_up` stays
        green as it is);
      - `max_continues_per_attempt = 0` → a dirty idle turn is harvested at
        once, as today;
      - `--resume` with a dirty non-READY worktree in `state.json` → the new
        session's first prompt contains the `round_prompt` text **and** the
        dirty file's name; a clean one gets the unchanged `round_prompt`.
- [ ] `tests/test_contest_roster.py`: the default is `2`, the key parses,
      `0` is allowed, a negative or non-integer value is rejected like the
      other `[contest]` ints.
- [ ] Every existing test in both files unmodified and green.
- [ ] `python3 -m pytest tests -q --timeout=180 && python3 -m pytest tests_bugfix -q --timeout=180` green (sequentially).

## Out of scope

- Answering a `question.asked` event — KC-1 rejects every question by
  design and counts it (`max_questions_per_turn`); none of the twelve live
  sessions of round 52 asked one.
- Refusing to reset a worktree that carries work when the operator re-runs
  the round without `--resume` — KC-23.
- The wording of `commits_ne_1` for zero commits ("amend them into exactly
  one") — `harvest.py`, cosmetic.

## Self-check before `append_task.py` (required — every item, in the worktree you submit)

- [ ] `python3 --version` on the judge is **3.10.12**;
      `python3 -c "import tools.contest.runner, tools.contest.roster"` from
      the repo root. No backslash and no nested same-quote inside an
      f-string expression.
- [ ] Exactly **one** commit on top of the base; only this ticket's work.
- [ ] `git diff --stat <base>..HEAD` names only `tools/contest/runner.py`,
      `tools/contest/roster.py`, `contest.ini` and the two test files (plus
      `.smoke_tests/` links). Never `epic-tasks/`.
- [ ] `python3 scripts/sync_test_tiers.py --check` is clean.
- [ ] The new tests are red without the change.
- [ ] `python3 -m pytest tests -q --timeout=180` then
      `python3 -m pytest tests_bugfix -q --timeout=180`, **sequentially**,
      both green.
- [ ] `CollectBridge._shrink` byte-identical.
- [ ] `scripts/append_task.py` from the worktree with the **sha** of the
      one commit — not `HEAD`; hand in `git format-patch <base>..HEAD`.

## Ground rules (same as every round)

- Do not touch `CollectBridge._shrink`.
- No test starts a real `kilo` or calls a live provider.
- Do not edit `epic-tasks/`.
- One commit, no push; a test ships with the change and fails without it.
