# KC-20 — `judge_worktree`'s `pushed` gate fires on a worktree with **no** commit, because the base itself is on `origin`

**Status:** open — round 59 of EPIC KC (`docs/kilo-contest/EPIC-KC.md`); found live on 2026-09-20 in the first manager-run round (`python3 -m tools.contest run --ticket 52`). Independent of every other open ticket except KC-17 (same file, different function — disjoint lines).
**Severity:** MEDIUM
**File:** `tools/contest/gates.py` (`judge_worktree`, hard gate 2)
**Symbol:** `judge_worktree`
**Round:** 59
**Size:** S
**Source:** round 52, `mistral-medium-3-5:free`, first turn: 895 s of work, `tools/contest/policy.py` modified but **never committed**. The harvest said `REWORK` with `no_progress_row, commits_ne_1, pushed, no_test_file, tests_failed` (`contest-out/52/mistral/turns.jsonl`). `pushed` is false: nothing left the machine. The gate runs `git branch -r --contains HEAD`; with zero commits `HEAD` **is** the base `70a708a`, and the base is on `origin/kc` — the operator pushed `kc` before starting the round, as every round will be started. The rework prompt then carried KC-5's sentence `HEAD 70a708af491c is reachable from a remote — delete the remote ref before the round is scored` — to a model whose session rules deny `git push*` and which was being told to delete the repository's own branch on GitHub. `mistral` did not try; the next model may. The same false positive hits every agent that reworks from zero commits, i.e. exactly the ones that most need a clean rework prompt. Both live rounds before this one (`contest-bench/kc6/RUNBOOK.md` §10–§11) ran in sandboxes whose base had no remote, so it never showed.
**Depends on:** KC-5 (`tools/contest/gates.py`, landed `0d91dd6`).
**Also touches:** `tests/test_contest_harvest.py` (the gate is tested through `harvest` — `test_harvest_flags_a_pushed_commit` — and `judge_worktree` directly)

---

## What happens today

```python
remotes = git(path, "branch", "-r", "--contains", "HEAD")
row["pushed"] = "yes" if remotes.strip() else "no"
```

`HEAD` is checked whether or not it is above the base. A worktree with no
commit, or one whose only commits are the base's own history, is "pushed"
whenever the base branch has ever been pushed — which on the real repo is
always.

## What must change

1. The gate asks whether **the agent's commits** reached a remote, not
   whether `HEAD` did: when `row["commits"] == 0` the answer is `no` — there
   is nothing that could have been pushed. When there are commits, keep
   `git branch -r --contains HEAD` (a remote that contains the tip contains
   the whole range).
2. `row["pushed"]` keeps its two values `yes`/`no`; `harvest`'s `pushed`
   reason, its text and `REASON_CODES` are unchanged (KC-5's contract,
   `test_reason_codes_are_the_tickets_list`); `commits_ne_1` still reports
   the zero-commit case on its own.
3. `scripts/judge_epic_round.py` stays byte-for-byte; its `--csv` golden
   test uses a fixture whose base is not on any remote, so it is unaffected.

## Acceptance

- [ ] `tests/test_contest_harvest.py`:
      - the live case: a sandbox whose base branch is fetched into a
        `origin` remote (`git remote add origin <bare>` + `git push origin main`
        in the sandbox — a bare repo under `tmp_path`, never the network),
        a worktree with **no** commit → `harvest` gives `REWORK`, codes contain
        `commits_ne_1` and **not** `pushed`;
      - the same sandbox, one commit **not** pushed → codes do not contain
        `pushed` (the commit above a pushed base is fine);
      - the same sandbox, the one commit pushed to `origin` → `pushed` is
        in the codes, text unchanged (`test_harvest_flags_a_pushed_commit`
        stays green as it is);
      - `judge_worktree` directly: `commits == 0` → `pushed == "no"` on the
        remote-backed sandbox.
- [ ] Every existing `tests/test_contest_harvest.py`
      test unmodified and green.
- [ ] `python3 -m pytest tests -q --timeout=180 && python3 -m pytest tests_bugfix -q --timeout=180` green.

## Out of scope

- The wording of the `pushed` reason — KC-5's text is correct when the gate
  is right.
- Detecting a push of a *different* branch from the worktree (`git push
  origin HEAD:scratch`) — `--contains HEAD` already catches it, since the
  tip is then on a remote ref.

## Self-check before `append_task.py` (required — every item, in the worktree you submit)

- [ ] `python3 --version` on the judge is **3.10.12**;
      `python3 -c "import tools.contest.gates"` from the repo root. No
      backslash and no nested same-quote inside an f-string expression.
- [ ] Exactly **one** commit on top of the base; only this ticket's work.
- [ ] `git diff --stat <base>..HEAD` names only `tools/contest/gates.py`
      and the test files above (plus `.smoke_tests/` links). Never `epic-tasks/`.
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
