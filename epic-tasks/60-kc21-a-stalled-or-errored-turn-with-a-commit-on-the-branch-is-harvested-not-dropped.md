# KC-21 — a STALLED/ERROR turn whose worktree already has a commit is harvested and exported, not dropped

**Status:** landed `f5a9f05` — round 60 of EPIC KC (`docs/kilo-contest/EPIC-KC.md`), nine entries: six live models through `python3 -m tools.contest run --ticket 60` and four hand patches (`kc21/`). Winner **Sensenova-6-8-var1** (10/10 on the judge's acceptance suite, the round's only end-to-end test through `cli.main` and the two-commit edge); ideal = the winner plus Sensenova-6-7's `run_tests=True` lock test. The round proved itself: `agnes-2-5-flash` STALLED at 1800 s with the finished commit `4d19143` on its branch and no patch — the judge pulled it by hand and it scored 9/10. Follow-ups: KC-29 (a stall the runner asked for), KC-30 (`harvest`'s commit without a claim row), KC-31 (`<agent>.STALLED.diff`), KC-32 (reds that are the box, not the code). round 60 of EPIC KC (`docs/kilo-contest/EPIC-KC.md`); found live on 2026-09-20 in the second manager-run round (`python3 -m tools.contest run --ticket 52`, base `5ee8417`). Seen again in round 58 (2026-09-20, `--max-parallel 8`, `contest-out/58/`) one step earlier: agnes-2-5-flash, step-3-7-flash and hy3 were STALLED at `turn_timeout_sec = 1800` with the whole change edited in the worktree but **not yet committed** — each was inside its self-check `python3 -m pytest tests -q --timeout=180` (four full suites in parallel on one machine; ≈95 s alone, minutes under contention) when the clock ran out. `run.commit` null, no `.patch`; the judge took `git diff` from the three worktrees by hand (`contest-bench/kc19/RESULTS.md`). When this lands, an uncommitted STALLED worktree should be exported too — `<agent>.STALLED.diff` from `git diff <base_sha>` when `rev-list --count` is 0 and `git status --porcelain` is not empty; it is not harvestable (no commit, no claim) but it is the operator's to read. And the budget itself is a finding: 1800 s with a self-check that reruns the whole suite two or three times does not fit eight agents on one box. Same file as KC-19 (`runner.py`, the stall/error branch of `run_agent`) — whichever lands second rebases; the two changes are adjacent, not overlapping (KC-19 decides *whether* to re-prompt, this ticket decides what to do with the worktree *once the state is terminal*).
**Severity:** HIGH
**File:** `tools/contest/runner.py` (`run_agent` — the branch that turns `idle.status` into `STALLED`/`ERROR`), `tools/contest/cli.py` (`export_patches` — the file name)
**Symbol:** `run_agent`, `export_patches`
**Round:** 60
**Size:** S
**Source:** round 52 rerun, `mistral-medium-3-5:free`: at minute 28 of its first turn the worktree had commit `9f05e3c` (`tools/contest/policy.py` +69, `tests/test_contest_policy.py` +158/−3) and `runs/mistral/PROGRESS.csv` had its row — the ticket's whole deliverable. The model was then re-running `python3 -m pytest tests` as its self-check when `turn_timeout_sec = 1800` expired: `STALLED — no idle after 1800s`, `commit: null`, no patch in `contest-out/52/`, the exit row empty. The operator pulled the patch by hand (`git -C ../rounds/52-mistral format-patch --stdout 5ee8417..HEAD`) and it was a complete entry. The runner already knows how to handle exactly this shape: `_runs_for` (the `--resume` path) harvests a mid-flight worktree and calls it `READY` when the harvest says so. A turn that dies with the same worktree under it gets nothing.
**Depends on:** KC-6 (`run_agent`, landed `e8c6ad3`), KC-16 (`export_patches`, landed `1304950`), KC-18 (the transition lines, landed `9c4db8b`).
**Also touches:** `tests/test_contest_runner.py`, `tests/test_contest_cli.py`

---

## What happens today

```python
            if state is not None:
                run.turns.append(turn)
                _append_jsonl(agent_dir / "turns.jsonl", {"agent": spec.name, **turn})
                return finish(state, error)
```

`STALLED` and `ERROR` return before `HARVESTING`. `run.commit` stays
`None`, so `export_patches` skips the agent, `state.json` says nothing about
the branch, and the exit row shows `"commit": null` next to a worktree that
has the finished work on it. `GAVE_UP` already keeps its patch
(`<agent>.GAVE_UP.patch`, KC-16) because "the operator still wants to read
those" — the same is true, more so, of a stall after the commit.

## What must change

1. In `run_agent`, when the turn ends in `STALLED` or `ERROR` **and the
   worktree has at least one commit above the base**
   (`git rev-list --count <base_sha>..HEAD` > 0 — the same count
   `judge_worktree` reports as `commits`), run `_harvest(ws, ticket_path, run_tests)`
   before finishing, exactly as the `HARVESTING` step and `_runs_for`'s
   resume branch do:
   - record `turn["harvest"] = {"verdict": …, "reasons": […]}` on the turn
     that stalled (the turn is still appended to `run.turns` and
     `turns.jsonl` once, with both `idle_status` and `harvest` on it);
   - set `run.commit = verdict.commit` whatever the verdict (the harvest
     sets it from the one commit on the branch; `None` when the branch has
     two or none);
   - if the verdict is `READY`, finish as `READY` — the same call
     `_runs_for` makes for a resumed worktree — with the transition note
     `after <error>` (KC-18's one line reads
     `mistral: READY — 9f05e3c0e2a1 after no idle after 1800s`);
   - otherwise finish in the terminal state the turn earned (`STALLED` /
     `ERROR`), `last_error` unchanged, no rework: the session is gone.
   A worktree with **no** commit keeps today's path byte-for-byte — no
   harvest, no pytest, `commit: null`.
2. `_harvest` runs under `_TEST_RUNS_LOCK` as it does today; a stalled
   agent's harvest queues behind the round's other pytest runs like any
   other.
3. `export_patches` writes the patch for every agent with `run.commit`,
   naming it by the terminal state when it is not `READY`:
   `<agent>.patch` for `READY`, `<agent>.GAVE_UP.patch`,
   `<agent>.STALLED.patch`, `<agent>.ERROR.patch` — i.e.
   `suffix = ".patch" if run.state is AgentState.READY else f".{run.state.value}.patch"`.
   `test_run_exports_a_patch_per_agent_and_exits_zero` and
   `test_run_exits_two_when_every_agent_gave_up` stay green as they are.
4. The exit code is unchanged: `EXIT_OK` needs a `READY`; a stall whose
   harvest is `READY` counts, a stall whose harvest is `REWORK` does not.
5. `AgentRun`'s JSON shape (KC-6's contract) is unchanged — the harvest
   lands in the existing `turns[i]["harvest"]` slot and `commit`.

## Acceptance

- [ ] `tests/test_contest_runner.py`, against the fake (`tests/_kilo_fake.py`):
      - a turn that commits one valid entry into the worktree and then goes
        silent past `idle_event_timeout_sec` → the run ends `READY`,
        `run.commit` is that sha, the single turn has
        `idle_status == "stalled"` **and** a `harvest` with
        `verdict == "READY"`, and the KC-18 line for it is
        `<agent>: READY — <sha12> after no event for …s`;
      - the same shape with a commit the harvest rejects (no `PROGRESS.csv`
        row) → `STALLED`, `run.commit` is the sha, `turn["harvest"]["verdict"] == "REWORK"`,
        `last_error` is the stall text, `attempt` unchanged, no second prompt
        to the fake (`len(fake.prompts) == 1`);
      - a `session.error` turn with one commit on the branch → the same two
        outcomes with `ERROR` in place of `STALLED`;
      - a stall with **no** commit → `STALLED`, `commit is None`, no
        `harvest` key on the turn, and the harvest was not called
        (monkeypatch `_harvest` to raise).
- [ ] `tests/test_contest_cli.py`: `export_patches` on a `RoundState` with a
      `STALLED` agent whose `commit` is set writes `<agent>.STALLED.patch`
      with `git format-patch` content; an `ERROR` one writes
      `<agent>.ERROR.patch`; `READY` and `GAVE_UP` names unchanged.
- [ ] Every existing test in both files unmodified and green — in
      particular `test_idle_event_timeout_stalls_a_silent_session_within_3s`
      (its worktree has no commit, so nothing changes for it).
- [ ] `python3 -m pytest tests -n 4 -q --timeout=180 && python3 -m pytest tests_bugfix -n 4 -q --timeout=180` green (sequentially).

## Out of scope

- Re-prompting after a retryable `session.error` — KC-19.
- Raising `turn_timeout_sec` so self-checks fit — an operator's
  `contest.ini` decision, not code.
- A fourth patch-file name for `REWORK`-in-flight worktrees on `--resume` —
  `_runs_for` already turns those into `READY` or a fresh attempt.

## Self-check before `append_task.py` (required — every item, in the worktree you submit)

- [ ] `python3 --version` on the judge is **3.10.12**;
      `python3 -c "import tools.contest.runner, tools.contest.cli"` from the
      repo root. No backslash and no nested same-quote inside an f-string
      expression.
- [ ] Exactly **one** commit on top of the base; only this ticket's work.
- [ ] `git diff --stat <base>..HEAD` names only `tools/contest/runner.py`,
      `tools/contest/cli.py` and the two test files (plus `.smoke_tests/`
      links). Never `epic-tasks/`.
- [ ] `python3 scripts/sync_test_tiers.py --check` is clean.
- [ ] The new tests are red without the change.
- [ ] `python3 -m pytest tests -n 4 -q --timeout=180` then
      `python3 -m pytest tests_bugfix -n 4 -q --timeout=180`, **sequentially**,
      both green.
- [ ] `CollectBridge._shrink` byte-identical.
- [ ] `scripts/append_task.py` from the worktree with the **sha** of the
      one commit — not `HEAD`; hand in `git format-patch <base>..HEAD`.

## Ground rules (same as every round)

- Do not touch `CollectBridge._shrink`.
- No test starts a real `kilo` or calls a live provider.
- Do not edit `epic-tasks/`.
- One commit, no push; a test ships with the change and fails without it.
