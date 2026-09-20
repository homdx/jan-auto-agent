# KC-14 — `harvest`: the claimed commit must be a hex sha that resolves — `HEAD`, a branch or a tag is `commit_not_on_branch`

**Status:** landed `720e4d9` — 3-entry contest (hand-run Sensenova 6-8 / 6-8 var2 / 6-7), winner Sensenova 6-8, scored black-box in `contest-bench/kc14/`; the manager round on hp-uz (five kenary free models) produced no entry because the judge was wrong (KC-17, KC-26 — both landed by hand the same day). Round 53 of EPIC KC (`docs/kilo-contest/EPIC-KC.md`); found live on 2026-09-19 (`contest-bench/kc6/RUNBOOK.md` §11).
**Severity:** MEDIUM
**File:** `tools/contest/harvest.py` (`_is_ancestor`, `harvest`)
**Symbol:** `_is_ancestor`, `harvest`, `Harvest.commit`
**Round:** 53
**Size:** S
**Source:** live round 1 of `contest-bench/kc6/live_smoke.py --models …` (12 kenary free models). `agnes-2-0-flash` ran `scripts/append_task.py … --outcome DONE --commit HEAD` — the literal word `HEAD`, not the sha the prompt asks for. The row landed in `runs/agnes-2-0-flash/PROGRESS.csv` as `…,FIXED,HEAD,"…"`, `harvest` ran `git merge-base --is-ancestor HEAD HEAD` (true by construction), reported `READY`, and `Harvest.commit == "HEAD"` went into `state.json` and the round table. Any name git resolves passes the same way: `main`, `@`, `HEAD~0`, `contest/01/agnes-2-0-flash`, a tag. The claim is supposed to pin *which* commit the agent hands in; a symbolic name pins nothing — it resolves to something else on every branch it is read from, and KC-7's `SUMMARY.md` would print `HEAD` as the commit.
**Depends on:** KC-5 (`tools/contest/harvest.py`, landed `0d91dd6`).
**Also touches:** `tests/test_contest_harvest.py`

---

## What happens today

`harvest` reads the claim's `commit` column and calls
`_is_ancestor(ws.path, claimed_commit)` — `git merge-base --is-ancestor
<claimed> HEAD`. git resolves the argument like any revision: `HEAD`,
`@`, a branch, a tag, `HEAD^{}`, even an abbreviated sha of any length.
Only an unresolvable string or a commit off the branch fails. `Harvest.commit`
is the string as written.

## What must change

1. The claimed commit is accepted only when it is a **hex sha of 7–40
   characters** (`[0-9a-f]{7,40}`, case-insensitive) **and** `git rev-parse
   --verify --quiet <claimed>^{commit}` resolves it in the worktree **and**
   it is an ancestor of HEAD. Anything else — `HEAD`, `@`, `main`, a tag, a
   6-char prefix, an ambiguous prefix — is `commit_not_on_branch`, with a
   text that says what was written and what to do:
   `commit HEAD is not a sha — commit once, then append_task.py --commit $(git rev-parse HEAD)`
   (one sentence, ≤ 200 chars, like every other reason; the empty case stays
   `no_commit`).
2. `Harvest.commit` is the **full 40-char sha** the claim resolves to, or
   `None` when the claim is rejected or empty. Downstream (`state.json`,
   `table_rows()`, KC-7's export) then carries one shape.
3. No new reason code: `REASON_CODES` and their order are KC-5's contract
   (`test_reason_codes_are_the_tickets_list`), and a symbolic name *is* a
   commit that is not pinned to the branch. The `commit_not_on_branch`
   text for a real sha off the branch is unchanged.
4. Keep `_is_ancestor` as the ancestry check; the shape check and the
   resolution go in front of it, in `harvest`, or in a small helper next to
   it (`_resolve_claim(path, claimed) -> str | None`, say). `git` is
   `tools.contest.gates.git` — no new subprocess wrapper.

## Acceptance

- [ ] `tests/test_contest_harvest.py`:
      - the live case: a DONE row with `commit=HEAD` on a clean one-commit
        worktree → `REWORK`, codes contain `commit_not_on_branch`, the text
        contains `HEAD` and `rev-parse`; `Harvest.commit is None`;
      - the same with the branch name (`ws.branch`) and with `@` → the same;
      - a DONE row with the **short** sha (`git rev-parse --short HEAD`) →
        `READY`, `Harvest.commit == git rev-parse HEAD` (40 chars);
      - a DONE row with the full sha → `READY`, `Harvest.commit` is that sha;
      - a 6-char prefix (`sha[:6]`) → `commit_not_on_branch` even though git
        would resolve it;
      - an uppercase full sha is accepted (git does), `Harvest.commit` is
        lower-case;
      - `test_harvest_flags_a_bogus_commit_sha` (40 zeros) and
        `test_harvest_flags_a_commit_off_the_branch` unchanged and green.
- [ ] Every existing `tests/test_contest_harvest.py` test and
      `tests/test_contest_runner.py` unmodified and green — the runner's
      fakes claim with real shas already.
- [ ] `python3 -m pytest tests -q --timeout=180 && python3 -m pytest tests_bugfix -q --timeout=180` green.

## Out of scope

- Teaching `scripts/append_task.py` to refuse `--commit HEAD`: the script
  is shared with the non-contest loop and its columns are the contract;
  the runner side must not trust the row anyway (a model can write the
  CSV by hand).
- Resolving the sha *for* the model (`--commit HEAD` → sha at write time):
  same reason — the claim is the model's, the check is ours.
- `scripts/judge_epic_round.py` stays byte-for-byte (`--csv` golden test).

## Self-check before `append_task.py` (required — every item, in the worktree you submit)

Every line below is a way a KC-5/KC-6 entry lost points on the round bench;
the scorer checks all of them mechanically, so check them yourself first.

- [ ] `python3 --version` on the judge is **3.10.12**. Every changed module
      imports there: `python3 -c "import tools.contest.harvest"` from the
      repo root. No backslash and no nested same-quote inside an f-string
      expression (a 3.12-only `f"{x.split("\t")}"` is a `SyntaxError` here
      and scores 0).
- [ ] Exactly **one** commit on top of the base: `git log --oneline <base>..HEAD`
      prints one line. Only this ticket's work is in it — no other KC
      ticket, no "while I was here" fixes; amend, do not stack.
- [ ] `git diff --stat <base>..HEAD` names only the files under **File:**
      and **Also touches:** (plus `.smoke_tests/` links). Never `epic-tasks/`.
- [ ] Names and signatures are the ticket's, verbatim — `harvest(ws, ticket_path, *, run_tests=False) -> Harvest`,
      `Harvest`/`Reason` frozen with the same fields, `REASON_CODES` unchanged;
      read `tools/contest/harvest.py` and `tools/contest/gates.py` (`git`)
      before adding to them.
- [ ] `python3 scripts/sync_test_tiers.py --check` is clean.
- [ ] The new tests are red without the change: check the test file alone
      out onto the base, run it, see them fail; restore.
- [ ] `python3 -m pytest tests -q --timeout=180` then
      `python3 -m pytest tests_bugfix -q --timeout=180`, **sequentially**,
      both green.
- [ ] `CollectBridge._shrink` byte-identical:
      `git diff <base> HEAD -- tools/auto/collect_bridge.py` is empty.
- [ ] Then, and only then, `scripts/append_task.py` from the worktree;
      open `runs/<you>/PROGRESS.csv` and see your row with the **sha** of
      the one commit — not `HEAD` (the script stores `--outcome DONE` as
      `FIXED`; that is fine).
- [ ] What you hand in is `git format-patch <base>..HEAD` of that one
      commit — not a raw `git diff`, not the whole branch, not an empty file.

## Ground rules (same as every round)

- Do not touch `CollectBridge._shrink`.
- No test starts a real `kilo` or calls a live provider.
- Do not edit `epic-tasks/`.
- One commit, no push; a test ships with the change and fails without it.
