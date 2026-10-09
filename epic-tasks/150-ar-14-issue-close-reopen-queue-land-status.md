# AR-14 — `arena issue queue / open / close / reopen / edit`: the only writers of `**Status:**`, on any branch; `run start` names the ticket that blocks intake

**Status:** landed
**Severity:** MEDIUM
**File:** tools/arena/tickets.py
**Symbol:** set_status, cmd_issue_queue, cmd_issue_open, cmd_issue_close, cmd_issue_reopen, cmd_issue_edit, blocking_tickets
**Round:** 150
**Size:** M
**Also touches:** tools/arena/cli.py, tools/arena/rounds.py, tools/arena/gitref.py, scripts/next_task.py, tests/test_arena_issue_status.py, docs/arena/EPIC-ARENA.md

**Depends on:** AR-6 (round 144, `issue list / view`, the computed state) and AR-7 (round 145). Run it after AR-63 (round 146).

---

## Why — what the operator had to do by hand on 2026-10-03

Rounds 148 and 149 are fix rounds on the branch `ctx-overflow-fix`, not on `arena`. This is what happened:

1. `./arena -p p144 run start 148 --branch ctx-overflow-fix` built `arena-round/148`.
2. Intake then refused. Tickets 145 (AR-7, whose round already ran and is judged on `arena`) and 147 (landed on that branch) still said `**Status:** open` on the fix branch, and `scripts/next_task.py` offers the lowest open ticket first.
3. The fix was a `sed` of two `**Status:**` lines in a second worktree checked out on `ctx-overflow-fix`, plus a hand commit.
4. Then a second `run start … --fresh-ticket`: `arena-round/148` held the old tip. That refusal already exists and names `--fresh-ticket` (`rounds.py`), so it is not part of this ticket; §5 below makes the first start refuse *before* it builds `arena-round/NN`, so the second start needs no `--fresh-ticket` at all.
5. Ticket 147 was set `landed` by the same `sed`. Setting `landed` stays `issue land`'s (AR-8); this ticket does not cover it.

EPIC-ARENA §4.4 says `**Status:**` is written by `arena` only. Today nothing in `arena` writes it except, later, `issue land` (AR-8). And `closed` (§4.4) is not a parked status: a closed ticket would block every higher round at intake.

---

## What to build

### 1. `arena issue queue | open | close | reopen NN [--branch B] [--reason TEXT] [--note TEXT]`

| verb | writes | when it is allowed |
|---|---|---|
| `queue` | `**Status:** queued` | from `open` |
| `open` | `**Status:** open` | from `queued` |
| `close` | `**Status:** closed`, and a `**Closed:** <reason>` line under it | from `open` or `queued`; `--reason` is required |
| `reopen` | `**Status:** open`, the `**Closed:**` line removed | from `closed` only |

- `--note TEXT` keeps a short note after the word, e.g. `queued (judged on arena)`. `_status` in `next_task.py` and `tickets.py` reads only the first word, so a note never changes the state.
- `landed` is **never** set here. That stays `issue land`'s (AR-8). A `landed` ticket refuses every verb with one line naming `issue land`.
- A running round's ticket (`running` in §4.4) refuses every verb.
- Exit codes and output follow AR-6: one line on success, `-o json` gives `{"ticket", "branch", "from", "to", "commit"}`, `-y` skips the before → after prompt.

### 2. On any branch, without checking it out

- `--branch B` defaults to the profile's branch, then HEAD, as `integration_branch` does (`rounds.py`).
- If B is the checked-out branch of the current checkout, write the file and commit it, with explicit paths only.
- Otherwise, do not touch any worktree. Commit on B through plumbing:
  - read the blob with `git show B:epic-tasks/NN-….md`;
  - write the new blob with `hash-object -w`;
  - build the tree in a temporary `GIT_INDEX_FILE` from `B^{tree}`;
  - `commit-tree -p B`, then `update-ref refs/heads/B <new> <old>`.

  The old value makes it a compare-and-swap, so a moved branch is refused.

  A branch that is checked out in *another* worktree is refused with the path of that worktree, because its working tree would go stale.
- The commit subject is `NN: status queued → …` (§ commit rules: prefixed by the round number, describing the change). The author is the configured git user. No co-author line.
- The commit is never pushed. The output prints the push command as one line.

### 3. `edit NN [--branch B]`

- Opens `$EDITOR` on the ticket text. With a `--branch` that is not checked out, it opens a temporary copy.
- On save, lint with `draft.HEADER_FIELDS`, the same check intake uses, then commit as in §2.
- `**Status:**` changed by hand in the editor is refused: "use issue queue/open/close/reopen".

### 4. `closed` is parked

- Add `closed` to `cli.PARKED` and to `scripts/next_task.py` `SKIP_STATUS`, as §4.4 requires.
- `issue list` shows `closed` with its reason.

### 5. `run start NN` names what blocks it, before it builds anything

Before it creates `arena-round/NN`, `run start` lists the tickets on the round's branch that intake would offer ahead of NN:
- same `epic-tasks/` order as `next_task.py`;
- not `landed`, `queued` or `closed`;
- a lower number.

If there are any, it builds nothing and exits non-zero. For each ticket it prints the error line, then the choices (§7):

```
arena: 145 (AR-7) is open on ctx-overflow-fix and comes before 148 — intake would hand the sessions 145
  → its round is done elsewhere, park it:  arena issue queue 145 --branch ctx-overflow-fix --note "judged on arena"
  → it is no longer wanted:               arena issue close 145 --branch ctx-overflow-fix --reason "…"
  → its code is on this branch:           arena issue land 145 --branch ctx-overflow-fix
```

The `issue land` line is printed only once AR-8 has landed (the verb exists in the CLI); before that it is left out.

The check reuses `next_task.py`'s own reading (`_status`, `SKIP_STATUS`, order), imported rather than copied, so the two never disagree.

### 6. The situations — each one decided here, so the round does not guess

**Where the ticket and the branch are**

| situation | what the verb does |
|---|---|
| B is checked out in this checkout | write the file, `git commit --only -- epic-tasks/NN-….md`. Other staged or modified files are left out of the commit and untouched. The repo's pre-commit hook (`githooks`) runs as for any commit. |
| B is checked out here, and `epic-tasks/NN-….md` has uncommitted edits | refuse, naming the file. The operator's edit is never overwritten or swept into the commit. |
| B exists locally and is not checked out anywhere | plumbing commit (§2). No hook runs, so the same lint as `issue edit` (§3) is run in code. |
| B is checked out in another worktree (`git worktree list --porcelain`) | refuse, naming that worktree's path and the command to run there: `arena issue queue NN` from that checkout. 2026-10-03: `ctx-overflow-fix` was checked out in `qwen25-ctxfix`. |
| B exists only as `origin/B` | refuse: `branch B is only on origin — git branch B origin/B`. A remote ref is never written. |
| no `--branch`, no profile branch, detached HEAD | refuse, as `integration_branch` already does. |
| B does not exist | refuse, as `integration_branch` already does. |

**What the ticket is**

| situation | what the verb does |
|---|---|
| the ticket is only in `.arena/drafts/` | refuse: a draft has no `**Status:**` yet. It is published by `issue create`'s own flow, not here. |
| the ticket is in the checkout's `epic-tasks/` but not committed on B | refuse: the status is written on the branch, and the file must be there first. |
| two files with the same NN on B | refuse, naming both (`find_ticket` already does). `*.rejected.md` is ignored. |
| no `**Status:**` line, or an unreadable one | refuse. `next_task.py` treats a missing line as on offer, so a status verb must not invent where the line goes. The operator adds it with `issue edit`. |
| a status word that is none of `open`, `queued`, `closed`, `landed` (e.g. `fixed`) | refuse every verb but `edit`, naming the word. `next_task.py` hands such a ticket out as open, and §5 reports it as a blocker. |
| the verb's target is already the status (e.g. `queue` on `queued`) | no commit, exit 0, one line: `already queued`. A `--note` that differs is written. |
| `landed` | refuse every verb, naming `issue land` (AR-8). |

**The round**

| situation | what the verb does |
|---|---|
| the round is `running` (§4.6: state.json and a live runner) | refuse. |
| the round is `done` (state.json, no live runner) | allowed. 145's round was done on `arena` and was parked on `ctx-overflow-fix`; that is the case this ticket exists for. |
| `arena-round/NN` exists for a round the verb changes | nothing to do here: the next `run start` already refuses a stale `arena-round/NN` and names `--fresh-ticket` (`rounds.py`). |
| two `arena` calls race on one branch | the compare-and-swap in §2 refuses the second; nothing is lost. |
| the branch is used on a second machine | the commit is local. The output prints `git push origin B` and never pushes. |

**§5's blocking check counts exactly what `next_task.py` would hand out**

- a lower NN on B's `epic-tasks/` whose status is not in `SKIP_STATUS`, including a missing `**Status:**` line and an unknown word;
- excluding tickets already recorded in the progress file `next_task.py` reads;
- a *higher* open NN never blocks. 149 stays open while 148 runs.

### 7. Every error is followed by what to do — no bare refusal

**The flow block — "you are here, the error is here":** before the hints, every refusal prints where the operator stands and which step failed:

```
arena: 145 (AR-7) is open on ctx-overflow-fix and comes before 148 — intake would hand the sessions 145
  where:  checkout /home/renat/…/qwen25 on arena (clean) · target branch ctx-overflow-fix @ e508db4,
          checked out in /home/renat/…/qwen25-ctxfix · round 148: not started
  ticket: 145 (AR-7) epic-tasks/145-….md on ctx-overflow-fix · Status: open · its round 145: done (on arena)
  flow:   [✓] branch found → [✓] ticket 148 found → [✗] intake: lower open tickets 145, 147 → [ ] build arena-round/148 → [ ] start sessions
  → its round is done elsewhere, park it:  arena issue queue 145 --branch ctx-overflow-fix --note "judged on arena"
  → it is no longer wanted:               arena issue close 145 --branch ctx-overflow-fix --reason "…"
```

- `where:` — this checkout's path, its branch (or `detached`) and whether it is clean; the target branch and its short sha; where it is checked out (here, another worktree's path, nowhere, origin only, missing); the round's state (not started / running / done).
- `ticket:` — NN and AR id, its file on the branch (or `draft only` / `not on B` / two files), its current `**Status:**` with the note, and its round's state.
- `flow:` — the command's steps in order: `[✓]` done, `[✗]` the step that failed with the short reason, `[ ]` not reached. The steps are fixed per command:
  - status verbs: branch resolved → where B is checked out → ticket found on B → status readable → transition allowed → round not running → write → commit;
  - `run start`: branch found → ticket found → intake (§5) → build `arena-round/NN` → start sessions;
  - `edit`: as the status verbs, then editor → status unchanged → lint → commit.
- A field that is not known yet at the failing step (e.g. the ticket when the branch is missing) prints `?`, never a guess.

**The rule:**
- Every refusal in this ticket (§1–§6), and §5's blocking report, prints the error line first, then the flow block above.
- Then one or more `  → ` lines, each with a short reason and a command the operator can paste as it is. The real NN, branch and path are filled in; there are no `<placeholders>`, except a `--reason "…"` the operator must write.
- When there is more than one sensible way out, every one is listed, the most likely first.
- The error and the hints go to stderr, and the exit code is non-zero.
- With `-o json`, the error object carries `"where"`, `"ticket"`, `"flow"` and `"hints": [{"why": "...", "command": "..."}]`.
- A refusal with no hint is a bug, and test 10 fails it.

**The hints, per refusal:**

| refusal | `→` hints (in this order) |
|---|---|
| a lower open ticket blocks `run start` (§5) | `issue queue NN --branch B --note "…"`; `issue close NN --branch B --reason "…"`; `issue land NN --branch B` (once AR-8 exists) |
| an unknown status word, as a §5 blocker | `issue edit NN --branch B` (fix the word); then the §5 hints |
| B is checked out in another worktree | `cd PATH && arena issue VERB NN …` (the same verb and flags) |
| B is checked out here, the ticket file has uncommitted edits | `git -C ROOT diff -- FILE` (see them); `git -C ROOT commit --only -m "NN: …" -- FILE` (keep them); `git -C ROOT restore -- FILE` (drop them); then the same command again |
| B is only on origin | `git branch B origin/B`; then the same command again |
| no branch: detached HEAD, no profile branch | `arena issue VERB NN --branch B` with B = the profile's branch if it has one; `git switch BRANCH` |
| B does not exist | `git branch --list '*PART*'` with the closest local names (difflib) printed as `--branch NAME` lines |
| the ticket is only in `.arena/drafts/` | `arena issue create --file .arena/drafts/FILE` (AR-7's publish flow) |
| the ticket is in the checkout but not committed on B | `git commit --only -m "NN: ticket …" -- epic-tasks/FILE` on B; then the same command again |
| two files with the same NN on B | each file's path with `git log -1 --format=%h\ %s B -- FILE`; `git rm` of the one to drop |
| no `**Status:**` line | `arena issue edit NN --branch B` (add `**Status:** open` under the title) |
| an unknown status word | `arena issue edit NN --branch B`; the four words that are allowed |
| `landed` | `arena issue view NN` (where it landed); `arena issue land NN` (AR-8) |
| the round is `running` | `arena status NN` (watch it); `arena run stop NN` (stop it); then the same command again |
| `close` without `--reason` | the same command with `--reason "…"` |
| `reopen` of a ticket that is not closed | the verb for its real status, e.g. `issue open NN` from `queued` |
| `queue` from `closed`, `open` from `closed` | `arena issue reopen NN` first |
| the compare-and-swap lost a race | `git log -3 --oneline B` (what moved it); the same command again |
| `edit` changed `**Status:**` | `arena issue queue/open/close/reopen NN …` for the wanted status; the rest of the edit is kept in a temp file, and its path is printed |
| `edit` failed the lint | the lint messages, and the temp file's path; `arena issue edit NN --branch B` again |
| an unknown NN | `arena issue list --branch B` |

---

## Tests (`tests/test_arena_issue_status.py`, new; a temp git repo, no network, no `kilo`)

1. **Each verb on the checked-out branch:**
   - one commit, the right `**Status:**` line;
   - the `**Closed:**` line is added on `close` and removed on `reopen`;
   - the rest of the file is byte for byte the same.
2. **Each verb with `--branch` on a branch not checked out:**
   - the working tree, the index and HEAD are untouched (`git status --porcelain` is empty, HEAD's sha is the same);
   - the branch has moved by exactly one commit.
3. **A branch checked out in another worktree:** refused, naming that worktree's path, with nothing written.
4. **A moved branch:** between the read and the update the ref moves (a hook in the test). The update is refused and nothing is lost.
5. **Refusals:**
   - `landed` → any verb;
   - `running` → any verb;
   - `close` without `--reason`;
   - `reopen` of a ticket that is not closed;
   - an unknown NN.
6. **`next_task.py`:** a `closed` ticket is not on offer, and the next open one is. `queued (judged on arena)` with a note still parses as `queued`.
7. **`run start`, the blocking check:** open tickets 145 and 147 before 148 on the branch:
   - exit non-zero, nothing built (no `arena-round/148`);
   - for each ticket, the error line, then the `→ issue queue` and `→ issue close` hints, with the real NN and branch.

   After `issue queue 145 --branch …` and `issue queue 147 --branch …`, the check passes.
8. **The situation tables (§6), one test per row:**
   - uncommitted edit refused;
   - origin-only branch refused;
   - draft-only refused;
   - missing status refused;
   - unknown word refused and reported by §5;
   - `queue` on `queued` → no commit, exit 0;
   - `done` round allowed;
   - other staged files are not in the commit.
9. **`issue edit`:** run with `EDITOR` set to a script that appends a line. The new text is committed. An edit that changes `**Status:**` is refused.

10. **Every error has its hints (§7):** parametrised over every row of §7's table.
    - stderr has the error line, then `where:`, `ticket:` and `flow:` with exactly one `[✗]` on the step that failed (the row's step), `[✓]` before it and `[ ]` after it, then at least one `  → ` line;
    - no `<…>` placeholder is left in it, and the real NN and branch are in it;
    - the first hint's command, run as printed in the temp repo (after the row's precondition is fixed where the hint says "then the same command again"), succeeds or moves to the next step. Checked for: the other-worktree `cd`, `git branch B origin/B`, `issue edit`, `issue reopen`, and §5's `issue queue`;
    - `-o json` gives the same hints under `"hints"` and the flow under `"where"`, `"ticket"`, `"flow": [{"step", "state"}]`.

## Acceptance

```bash
python3 scripts/sync_test_tiers.py --check
```

```bash
python3 -m pytest tests -n 8 -q
```

```bash
python3 -m pytest tests_bugfix -n 8 -q
```
