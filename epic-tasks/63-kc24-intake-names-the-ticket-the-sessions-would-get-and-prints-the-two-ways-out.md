# KC-24 — `run --ticket NN` refused for a lower ticket on offer: intake names the ticket the sessions *would* get and prints the two ways out

**Status:** open — KC-15 landed `2ed3ee1` on 2026-09-20; found 2026-09-20 starting round 54 while round 53 was in flight on a second machine: `intake: KC-14 (53) is open too — set it to queued or run it first`, and nothing more. The operator had to read `cli.py` to learn *why* (the session prompt never names a ticket; `scripts/next_task.py` hands out the lowest ticket on offer), then guess the status word — and the first guess, `running`, passes intake but is still on offer to `next_task.py`, so round 54's sessions would have done KC-14. Found again 2026-09-20 starting round 58 beside round 54 (in flight on hp-uz): the operator parked KC-15 in a commit on a branch `kc-58-base` and ran `run --ticket 58 --base kc-58-base` — refused with the same line, because `intake` reads `epic-tasks/` from the **checkout**, while the sessions read it from the worktree built at `--base`. The two only agree when the base is HEAD. And the way out this ticket was going to print — a bare `sed` on the checkout — would not have started the round either: `prepare_round` refuses a dirty `epic-tasks/` (`workspace._check_base_and_epic_tasks`), so the park is a `sed` **and a commit**. The operator had to check out the parked branch to start round 58.
**Severity:** LOW
**File:** `tools/contest/cli.py` (`intake`, `_tickets`, `_status`)
**Symbol:** `intake`, `_tickets`, `_status`
**Round:** 63
**Size:** S
**Source:** `intake` refuses when a lower-numbered ticket's status is exactly `open`; `scripts/next_task.py` (`SKIP_STATUS = ("landed", "queued")`) offers every ticket whose status is **not** `landed`/`queued`. The two rules disagree on every other word (`running`, `wip`, a missing line), which is exactly the gap an operator falls into when a round runs elsewhere. The refusal also says nothing about what would happen (the sessions get the lower ticket) nor how to get past it: which command runs the planned ticket, which one-liner parks it so this round can start beside it. `_tickets` and `_status` open the files under `tasks_dir` — the checkout — while the runner's prompt (`runner.py`, `python3 scripts/next_task.py --tasks epic-tasks/`) runs inside `rounds/NN-<agent>`, a worktree at the base sha `intake` itself resolves two lines later: `git show <base>:epic-tasks/<name>` is the copy the sessions see.
**Depends on:** KC-16 (`cli.py run`, landed `1304950`).
**Also touches:** `tests/test_contest_cli.py`

---

## What must change

1. The rule matches `next_task.py`: a lower-numbered ticket blocks when its
   status word is **not** in `("landed", "queued")` — the same tuple, kept
   in `cli.py` as `PARKED = ("landed", "queued")` with a comment naming
   `scripts/next_task.py`'s `SKIP_STATUS` (the runner does not import
   scripts, the way `_STATUS_RE` is already duplicated). `open` for the
   requested ticket itself stays as is.
2. Both status reads — the requested ticket's and every lower ticket's —
   come from the **base tree**, not the checkout: `git ls-tree --name-only
   <base> epic-tasks/` for the file list and `git show <base>:epic-tasks/<name>`
   for each body (`gates.git`, one call per file — a dozen files, well
   under a second). The base sha is the one `intake` already resolves;
   resolve it first, and when it does not resolve the existing
   `WorkspaceError` line is the only failure. `_tickets(tasks_dir)` keeps
   its signature for the tests and gains a keyword `at=None` (a sha, or
   `None` for the working tree); `intake` passes the sha. The checkout's
   copy stays what `prepare_round` guards for dirtiness — nothing else
   reads it.
3. When that rule fires, the failure is one message of four lines, the
   first three as today's shape and each further line indented two
   spaces (every line still lands under `intake:` through the existing
   loop):

   ```
   intake: KC-14 (53) is on offer ahead of KC-15 (54) — the session prompt names no ticket; scripts/next_task.py would hand the sessions KC-14 (53)
   intake:   planned for the sessions: epic-tasks/53-kc14-….md (**Status:** open)
   intake:   run that one instead:    python3 -m tools.contest run --ticket 53 <the other argv words verbatim>
   intake:   or park it, then re-run:  sed -i '3s/^\*\*Status:\*\* open/**Status:** queued/' epic-tasks/53-kc14-….md && git commit -m 'epic-tasks: KC-14 queued — round 53 runs elsewhere' -- epic-tasks/53-kc14-….md
   ```

   The park line is `sed` **and** `git commit` on that one file, because
   an uncommitted `epic-tasks/` is refused by `prepare_round` and an
   uncommitted edit is invisible to the sessions anyway (item 2). When
   `--base` is not the checkout's HEAD, the park line is replaced by one
   sentence: `  or park it in a commit reachable from --base <ref> (the
   sessions read epic-tasks/ from there, not from this checkout)`.

   The `run` line is `sys.argv[1:]` with the `--ticket` value (either
   `--ticket NN` or `--ticket=NN`) replaced — nothing else is
   re-derived. The `sed` line targets the ticket's `**Status:**` line by
   its **number** (found once, `_STATUS_RE` gives the offset → line
   count) in the **base** copy, not a hard-coded `3`. One block per lower
   ticket on offer, lowest first.
4. Nothing else in `intake` changes: the other failures keep their
   one-line shape, and `EXIT_FAILED` / no server started stay as they are.

## Acceptance

- [ ] `tests/test_contest_cli.py`: a sandbox with tickets 53 (`open`) and
      54 (`open`), `run --ticket 54 --models x --max-parallel 2` → stderr
      has the four `intake:` lines, the `run` line is exactly
      `python3 -m tools.contest run --ticket 53 --models x --max-parallel 2`,
      the `sed` line names ticket 53's file and its real status line
      number; exit `EXIT_FAILED`, no `kilo serve` spawned.
- [ ] Same sandbox with 53 as `running` → still refused (the word is not
      in `PARKED`); with 53 as `queued` → passes intake (today's
      behaviour, unchanged).
- [ ] `--ticket=54` spelling → the `run` line carries `--ticket=53`.
- [ ] The base wins over the checkout, both ways: a sandbox repo whose
      HEAD has 53 `open` and whose branch `parked` carries one commit
      with 53 `queued` — `run --ticket 54 --base parked` passes intake
      (and the `intake:` refusal is not printed), while `run --ticket 54`
      (base HEAD) is refused; the reverse layout (53 `queued` at HEAD,
      `open` on `parked`) is refused with `--base parked` and passes
      without. The refusal's park line for `--base parked` is the
      one-sentence form, not the `sed`.
- [ ] The park line for base = HEAD ends in `&& git commit -m '…' --
      epic-tasks/<file>`; running the printed `sed && git commit` in the
      sandbox and re-running intake passes.
- [ ] Every existing intake test unmodified and green.
- [ ] `python3 -m pytest tests -n 4 -q --timeout=180 && python3 -m pytest tests_bugfix -n 4 -q --timeout=180` green (sequentially).

## Out of scope

- A `park`/`queue` subcommand that rewrites the status for the operator —
  the printed `sed` is the whole tool; a subcommand can come once two
  rounds have wanted it.
- Teaching `next_task.py` a `running` status — the two-word rule is
  shared as is.
- Running two tickets from one checkout at once.
- Reading the ticket the **harvest** judges (`gates.ticket_for_round` →
  `judge_worktree`) from the base: it reads the checkout today and the
  round's own ticket is identical in both trees; a separate ticket if a
  round ever needs it.

## Self-check before `append_task.py` (required — every item, in the worktree you submit)

- [ ] `python3 --version` on the judge is **3.10.12**;
      `python3 -c "import tools.contest.cli"` from the repo root. No
      backslash and no nested same-quote inside an f-string expression —
      build the `sed` line with `str.replace`/concatenation, not an
      f-string holding `\*`.
- [ ] Exactly **one** commit on top of the base; only this ticket's work.
- [ ] `git diff --stat <base>..HEAD` names only `tools/contest/cli.py` and
      `tests/test_contest_cli.py` (plus `.smoke_tests/` links). Never
      `epic-tasks/`.
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
