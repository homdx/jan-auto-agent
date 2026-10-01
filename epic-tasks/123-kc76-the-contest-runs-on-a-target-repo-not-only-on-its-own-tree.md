# KC-76 — The contest runs on a target repo, not only on its own tree

**Status:** landed `dd830b9` (2026-09-29) — by hand; first written as KC-68 (round 115) on kc-2legs-runbook, renumbered: kc already had both
**Severity:** HIGH (the entire contest is currently self-referential — it can only compete
models against jan-auto-agent; running it against any other codebase requires an awkward
PYTHONPATH dance and means the operator manually sets up `epic-tasks/` inside the target
repo, which nobody does)
**File:** `tools/contest/cli.py` (`cmd_run`, `_parser`, `_roster_path`, `intake`), `docs/kilo-contest/EXTERNAL-REPO-RUNBOOK.md` (new)
**Symbol:** `cmd_run`, `_roster_path`, `intake`, `_round_out_dir`
**Round:** 123
**Size:** M
**Source:** operator, 2026-09-29: "the contest needs to run on a different, external
repository — not jan-auto-agent. Right now `repo = Path.cwd().resolve()` forces the
operator to `cd` into the target, which is awkward and couples the contest to the CWD.
A `--target REPO_PATH` flag makes the intent explicit and lets the operator run the
contest from any directory."
**Depends on:** nothing (standalone, no queued KC ticket is a prerequisite)
**Also touches:** `tests/test_contest_cli.py`,
`contest-bench/kc76/`

---

## What happens today

`cmd_run` opens with:

```python
repo = Path.cwd().resolve()
tasks_dir = repo / TASKS_DIR   # "epic-tasks"
```

So the contest's target repo is always the directory the operator ran the command
from. There is no way to say "compete models against `/home/user/my-project` while
I stand in `/home/user/jan-auto-agent`." The operator must:

1. `cd /home/user/my-project`
2. `PYTHONPATH=/home/user/jan-auto-agent python3 -m tools.contest run --ticket NN`

This is undocumented, breaks IDE debugging, and couples every `--roster` path
resolution to the CWD. It also means `epic-tasks/` must live inside the target
repo — a restriction that prevents the operator from keeping tickets and the
contest harness in one place while the codebase being improved lives elsewhere.

## What must change

1. **`--target REPO_PATH`** — new optional flag on `run`. When omitted, behaviour
   is byte-identical to today: `repo = Path.cwd().resolve()`.

   ```
   python3 -m tools.contest run --ticket 01 \
       --target /home/user/my-project \
       --roster /home/user/jan-auto-agent/contest.ini
   ```

2. **`intake(repo, …)` stays unchanged.** `repo` is already a parameter; the
   only caller (`cmd_run`) passes `Path.cwd()` today. After this ticket it
   passes `Path(args.target).resolve()`.

3. **`prepare_round(repo, …)` stays unchanged.** Same reasoning — `repo` is
   already a parameter; the fix is purely in `cmd_run`'s assignment.

4. **`_round_out_dir(repo, …)`** — `config.out_dir` is resolved relative to
   `repo` when it is a relative path. After this ticket `repo` may not be CWD,
   so a relative `out_dir` lands inside the *target* repo, which is correct
   (patches belong next to the code they patch). If the operator wants the
   output somewhere else they pass an absolute path for `out_dir` in
   `contest.ini`, which already works.

5. **`--tasks-dir` is dropped.** Tickets must stay in `<target>/epic-tasks/`,
   committed at the base: the agents read them from their own clone via
   `next_task.py --tasks epic-tasks/`, `workspace._check_base_and_epic_tasks`
   hard-codes `epic-tasks`, and `_ticket_body` reads `git show <base>:<dir
   name>/<file>` from the tasks dir's parent. A tasks dir outside the target
   would let intake pass while every agent finds no ticket.

6. **`docs/kilo-contest/EXTERNAL-REPO-RUNBOOK.md`** — the operator's page for
   the external-repo workflow: how to set up a target repo, how to write a
   ticket for it, how to read `contest-out/` when it lands inside the target,
   and the 2-leg manual workflow (KC-43 is queued; this doc covers both the
   manual process and the flag KC-43 will add).

7. **`_roster_path(repo, roster)` MUST change.** Today it is
   `path if path.is_absolute() else repo / path` (`cli.py`, "relative to the
   repo") and `cmd_run` passes `repo`. Once `repo` may differ from CWD, a
   relative `--roster contest.ini` would silently resolve inside the *target*.
   Resolve a relative roster against `Path.cwd()` (where the operator stands),
   update the `--roster` help text ("default contest.ini at the repo root")
   and document it in the runbook. Without this change the roster acceptance
   item below cannot pass.

8. **Target prerequisites.** The agent prompt runs `scripts/next_task.py` and
   `scripts/append_task.py` inside the agent's clone of the target, so both
   must be committed in the target repo. Intake should refuse, with a clear
   line, a target whose base tree lacks `scripts/next_task.py`,
   `scripts/append_task.py` or `epic-tasks/`.

## Acceptance

- [ ] `tests/test_contest_cli.py`: `run --target /tmp/fake-repo --ticket 01`
      resolves `repo` to `/tmp/fake-repo`; without `--target` it resolves to
      CWD (regression: no change in the no-flag path).
- [ ] `run --target /some/path` reads the ticket from
      `/some/path/epic-tasks/01-*.md`, and refuses (intake line) a target whose
      base lacks `scripts/next_task.py`.
- [ ] `prepare_round` is called with `repo = Path(args.target).resolve()` when
      the flag is given; the worktrees are created relative to the target repo.
- [ ] `_round_out_dir` with a relative `out_dir` lands inside the target repo,
      not CWD.
- [ ] `--roster` with a relative path still resolves against CWD, not the target.
      Test: `run --target /tmp/a --roster contest.ini` with `contest.ini` in CWD
      and no `contest.ini` in `/tmp/a` — should find the one in CWD.
- [ ] **Live round, required.** Run the contest against a small external Python
      project (not jan-auto-agent) with at least two roster models, one trivial
      ticket (e.g. "add a `__repr__` to the `Calc` class in `calc.py`"), and
      record in `contest-bench/kc76/RESULTS.md`: the command line used, the
      agents' verdicts, and the patches produced. The external repo must be a
      separate git repository in a different directory.
- [ ] `docs/kilo-contest/EXTERNAL-REPO-RUNBOOK.md` exists, covers the setup
      checklist, `--target` / `--roster` interaction, the output
      layout, and the manual 2-leg relay workflow pending KC-43.
- [ ] `python3 -m pytest tests -n 4 -q --timeout=180 && python3 -m pytest tests_bugfix -n 4 -q --timeout=180` green.

## Out of scope

- Automatic discovery of the target repo from the roster or a config key. The
  flag is explicit by design — implicit resolution is another ticket.
- Running multiple contests against different targets in parallel. That is a
  scheduling problem orthogonal to the repo path.
- Any change to `CollectBridge._shrink`. Not touched here.

## Ground rules

- Do not touch `CollectBridge._shrink`.
- No test starts a real `kilo` or calls a live provider.
- Do not edit `epic-tasks/` other than this file.
- One commit, no push; a test ships with the change and fails without it.
