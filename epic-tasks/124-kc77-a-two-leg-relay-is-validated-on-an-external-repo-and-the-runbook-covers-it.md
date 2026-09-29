# KC-77 — A two-leg relay is validated on an external repo and the runbook covers it

**Status:** landed (2026-09-29) — by hand; first written as KC-69 (round 116) on kc-2legs-runbook, renumbered: kc already had both. Part A (two tickets, two rounds) verified live 11/11 on both machines; `--legs 2` on the external repo is not yet run live
**Severity:** MEDIUM
**File:** `2legs/` (runbook, demo tickets, `run_2legs.sh`, `check_2legs.py`), `docs/kilo-contest/EXTERNAL-REPO-RUNBOOK.md`, `contest-bench/kc77/`
**Symbol:** — (no change under `tools/`)
**Round:** 124
**Size:** S
**Source:** operator, 2026-09-29: "verify the 2-leg workflow on a different, external
repository — not jan-auto-agent — and give English instructions that show exactly
what to run and what to check."
**Depends on:** KC-76 (round 123, `--target`), KC-43 (round 82, `--legs`)
**Also touches:** `tests/test_2legs_runbook.py`

---

## What was asked

One task split in two parts, done by agents on a repo that is not jan-auto-agent,
with part 2 building on part 1 — and a page a new operator can follow.

## What landed

- `2legs/make_target.sh` builds `ext-demo-repo` next to the checkout: a `Calc`
  class, one test, the two contest scripts, tickets 01 (`__repr__`) and 02 (`__eq__`).
  It refuses to delete a directory that is not its own demo repo.
- `2legs/run_2legs.sh` runs round 1, lands the first READY patch that applies with
  green tests, runs round 2 on top, lands it, and execs `2legs/check_2legs.py`.
- `2legs/check_2legs.py` — 11 on-disk checks, no LLM: READY per leg, tickets landed,
  winners in history, `Calc` behaves, tests pass, leg 2's commit leaves `__repr__` untouched.
- `2legs/HOW-WE-RUN-2LEGS.md` — the hand version, step by step with a check after each.
- `tests/test_2legs_runbook.py` — offline: the runbook's commands parse, no machine
  paths or model ids in git, the checker passes a good history and fails a leg 2
  that rewrote leg 1.
- `contest-bench/kc77/leg_record.py` — the leg record by hand, for step 5 (manual leg 2).

## Evidence

2026-09-29, `source 2legs/env.sh && export MODELS=… && 2legs/run_2legs.sh`:

| machine | models | leg 1 READY | leg 2 READY | checks | time |
|---|---|---|---|---|---|
| 1 | 2 | 2/2 | 1/2 (the other: provider overload) | 11/11 | ~35 min |
| 2 | 5 | 5/5 | 5/5 | 11/11 | 9 min |

Model ids are per machine and are not recorded in git. Almost every READY carried
`uncommitted_files` (bytecode caches); the demo repo's `.gitignore` now lists them.

## Left open

- `--legs 2` (automatic relay) on the external repo, live — part B of the runbook.
- The intake probe comes back unparsable on both machines (warning only).
