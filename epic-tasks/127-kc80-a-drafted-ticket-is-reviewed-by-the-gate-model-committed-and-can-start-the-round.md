# KC-80 — A drafted ticket is reviewed by the gate model, committed, and can start the round

**Status:** landed `57bd1a8` on 2026-09-30 — round 127, winner 19/19 on contest-bench/kc80 taken as-is
**Severity:** MEDIUM
**File:** `tools/contest/draft.py`
**Symbol:** `review_ticket`, `commit_ticket`
**Round:** 127
**Size:** S
**Source:** follows KC-79
**Depends on:** KC-79 (round 126)
**Also touches:** `tools/contest/cli.py`, `tests/test_contest_draft.py`

---

## Why

KC-79's lint catches a malformed ticket, not a bad one: an acceptance that
cannot fail, a speed task that "passes" by deleting or skipping tests, a task
too big for one leg. A second model should read it before agents spend an
hour on it — and the round already has one: the gate model, which by config
differs from every agent.

## What to build

`draft` gains two steps after KC-79's lint:

1. **Review.** One call to the model of `[contest] gate_llm_profile` (same
   resolution and transport settings as the gate; no model or URL in code).
   It gets the ticket, the brief and this checklist and answers JSON
   `{"ok": bool, "problems": [str]}`:
   - the acceptance can be checked by running commands, not by opinion;
   - the task cannot be met by deleting, skipping or weakening tests;
   - it fits one agent in one leg (else: split it, say how);
   - it does what the brief asked, not something nearby.
   `ok:false` → the problems go back to KC-79's drafter, then lint, then
   review again; at most 2 such rounds (config `draft_review_rounds`, default
   2). Still not ok → exit 2, problems printed, `.rejected.md` saved. A reply
   that is not that JSON counts as not ok with the raw text as the problem.
2. **Commit.** On ok: check out branch `contest-legs` in REPO (create it when
   missing — the same thing `2legs/prepare_task.sh` does), copy
   `scripts/next_task.py` and `scripts/append_task.py` in when absent, commit
   the ticket (explicit paths, no `-a`), return to nothing else. Refuse a
   REPO with uncommitted tracked changes, as `prepare_task.sh` does. Print
   `ticket NN ready: <path> — check it, then: python3 -m tools.contest run
   --ticket NN --target REPO`.
3. **`--run`.** With `--run` (and the `run` options passed through, e.g.
   `--legs`, `--models`), the round starts right after the commit instead of
   the message.

`--no-review` skips step 1 (prints that it did).

## Acceptance

    python3 -m pytest tests/test_contest_draft.py -q

With fake drafter and fake reviewer callables and a git repo in `tmp_path`:
- ok first time → one commit on `contest-legs` holding only the ticket (and
  the two scripts when they were missing); the original branch untouched;
- not ok, then ok → the drafter saw the reviewer's problems; committed;
- not ok three times → exit 2, `.rejected.md`, no commit;
- a non-JSON review → not ok, raw text among the problems;
- dirty REPO → refused before any LLM call;
- `--run` calls the round entry with `--ticket NN --target REPO` and the
  passed-through options (the entry is faked; no Kilo started);
- no `gate_llm_profile` and no `--no-review` → error naming the key.

## Rules

- No real provider, no Kilo, no network in tests.
- No model name, URL or key in code.
- Commit with explicit paths only.
- The full suite stays green: `python3 -m pytest .smoke_tests/ .regression_tests/`.
