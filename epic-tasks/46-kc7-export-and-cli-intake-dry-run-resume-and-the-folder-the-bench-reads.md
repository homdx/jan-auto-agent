# KC-7 — `tools/contest/export.py` + `cli.py`: `python3 -m tools.contest run` — intake, the round, and the folder `contest-bench` reads

**Status:** open — round 46 of EPIC KC (`docs/kilo-contest/EPIC-KC.md`); KC-6 (round 45) landed `e8c6ad3` and was run live on 2026-09-19 (`contest-bench/kc6/RUNBOOK.md` §10–§11 — `live_smoke.py` is the hand-written stand-in for this CLI). The last round run by hand: from here on tickets go through `python3 -m tools.contest run`. Independent of KC-12, KC-13 and KC-14 (different files); can run in parallel with any of them. Written against `docs/kilo-contest/PROBE.md`.  
**Severity:** HIGH  
**File:** `tools/contest/cli.py` (new), `tools/contest/export.py` (new), `tools/contest/__main__.py` (new)  
**Symbol:** `main`, `cmd_run`, `cmd_status`, `intake`, `export_round`, `write_summary`, `write_entrants`  
**Round:** 46  
**Size:** M  
**Source:** `contest-bench/harness/setup_worktrees.py`'s docstring is the `entrants.json` contract (`base`, `entrants.<name>.source`, optional `duplicate_of`); `scripts/judge_epic_round.py --worktree name=path` is the scorer's input; `scripts/next_task.py` refuses tickets whose `**Status:**` is `landed`/`queued`. The run must stop where those start.  
**Depends on:** KC-6 (and through it all earlier rounds).  
**Also touches:** `tests/test_contest_cli.py` (new), `.gitignore` (`contest-out/` — from KC-2), `AGENTS.md` (one line under Build/Run)

---

## What happens today

`run_round` exists but nothing invokes it, checks the inputs, or turns
the terminal states into files the existing tooling reads. The operator
would still hand-write `entrants.json` and `format-patch` each branch.

## What must change

1. **`intake(repo, config, round_no, base_ref) -> Intake`** — before any
   server starts: the ticket file for `round_no` exists (`gates.ticket_for_round`);
   its `**Status:**` first word is `open` (not `landed`, not `queued`);
   it has `**File:**` and `**Symbol:**` lines (the `next_task.py` contract);
   `base_ref` resolves; `epic-tasks/` is committed at the base
   (KC-4's check, called early so the message comes before worktrees);
   `kilo_bin` resolves (`find_kilo_binary`) or `server` is a URL that
   answers `/global/health`; the gate profile has a non-empty `api_key`
   after env expansion. Any failure → exit 1 with one line per problem.
   Everything passes → prints the plan: ticket title, base sha, N agents
   with models, gate model, out dir.

2. **`cmd_run`** — `python3 -m tools.contest run --ticket NN [--roster contest.ini] [--base REF] [--attach URL] [--clone name=path …] [--dry-run] [--resume] [--run-tests]`:
   - `--dry-run`: intake + `prepare_round` + print the plan and the
     exact prompt that would be sent; **no server, no session**;
   - otherwise: `KiloServer.spawn` (or `attach`), `run_round`,
     `export_round`, server closed in `finally`; `--resume` loads
     `out_dir/<NN>/state.json` and passes it through; `--run-tests`
     makes harvest run the four pytest roots (slow; off by default);
   - exit 0 if ≥ 1 agent `READY`, 2 if none, 1 on intake/server error.
   - `cmd_status`: `python3 -m tools.contest status --ticket NN` prints
     the table from `state.json` without touching anything.

3. **`export_round(state: RoundState, workspaces, out_dir, base_sha)`**:
   - for every agent with a commit (`READY`, and `GAVE_UP` with a commit):
     `git -C ws format-patch --stdout <base_sha>..<branch>` → `out_dir/<agent>.patch`;
     `GAVE_UP` patches are named `<agent>.GAVE_UP.patch`;
   - `write_entrants(out_dir, base_sha, agents)` → `entrants.json` in the
     bench's shape, `source` relative to `out_dir`, byte-identical patches
     marked `duplicate_of` (compare after stripping the `From <sha>` and
     `Date:` lines);
   - `write_summary(out_dir, state)` → `SUMMARY.md`: the header (ticket,
     base, gate model, started/finished, wall time), then one table row
     per agent — name, model, state, attempts, turns, asked/allowed/
     rejected/gated/gate-failed, questions, cost, tokens in/out, commit,
     last reason — then a "Decisions worth a look" list: every `gate` and
     `gate-failed` decision across agents with its command and reason
     (the operator reads this to see whether the gate blocked something
     the ticket needed), then the three commands to run next
     (`judge_epic_round.py --worktree …` with the real paths,
     `contest-bench/harness/setup_worktrees.py <out>/entrants.json --wt …`,
     `contest_reset.sh` for cleanup).

4. **`tools/contest/__main__.py`** → `cli.main()`. `AGENTS.md` gains the
   `run` line and the dry-run line.

## Acceptance

- [ ] `tests/test_contest_cli.py`: `--dry-run` on a temp repo with a
      committed open ticket prints the plan and the prompt, creates the
      worktrees, starts **no** server (assert `KiloServer.spawn` is not
      called — monkeypatch); a `queued` ticket, a missing `**File:**`
      line, an unresolved base, uncommitted `epic-tasks/`, and an empty
      gate `api_key` each fail intake with the named reason and exit 1;
      a full `run` against `tests/_kilo_fake.py` (monkeypatch
      `KiloServer.spawn` to attach to the fake) with two agents — one
      READY, one GAVE_UP with a commit — writes `laguna.patch`,
      `mistral.GAVE_UP.patch`, `entrants.json` that
      `contest-bench/harness/setup_worktrees.py` accepts (call its loader
      on it), and a `SUMMARY.md` with both rows and the gate section;
      exit 0; the same with both GAVE_UP → exit 2; two byte-identical
      patches → the second is `duplicate_of` the first.
- [ ] `git am` of `laguna.patch` onto the base in a fresh temp worktree
      applies cleanly and reproduces the branch's tree.
- [ ] `status` after the run prints the same table as `SUMMARY.md`'s.
- [ ] `python3 -m pytest tests -q --timeout=180 && python3 -m pytest tests_bugfix -q --timeout=180` green.

## Out of scope

- Merging, cherry-picking, scoring — the operator's stages 3–5.
- Uploading anything anywhere.

## Ground rules (same as every round)

- Do not touch `CollectBridge._shrink`.
- No test starts a real `kilo` or calls a live provider.
- Do not edit `epic-tasks/`.
- One commit, no push; a test ships with the change and fails without it.
