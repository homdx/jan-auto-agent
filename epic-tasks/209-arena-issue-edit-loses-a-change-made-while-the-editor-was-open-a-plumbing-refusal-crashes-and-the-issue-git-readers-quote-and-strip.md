# 209 — `arena issue edit` overwrites a ticket that changed while the editor was open, its plumbing refusal crashes, and AR-14's git readers repeat bugs 206 and 209

**Status:** queued
**Origin:** operator review (`bugs-to-review/test_arena_issue_edit_concurrent.py`, bug 211 `test_arena_issue_git_reads_211.py`), probed on `arena` @ `00355fd`
**Severity:** MEDIUM (42: another change to the ticket is silently gone), LOW (43: a traceback instead of a refusal; 44: a wrong name in a refusal, a "not found" for a ticket that exists)
**File:** tools/arena/tickets.py
**Symbol:** issue_edit, _commit_plumbed, _on_branch, _uncommitted, _modified
**Round:** 209
**Size:** M
**Also touches:** tools/arena/output.py (only if the hint shape is tightened there), tests_bugfix/

## Why

AR-14's `arena issue edit NN` reads the ticket from the branch, opens `$EDITOR`, then lints and commits the edited text. The window between the read and the write is as long as the operator keeps the editor open — minutes — and the code does not defend it; and three small git readers next to it repeat two bugs that ticket 197 fixes in `rounds.py`. Probed on `arena` @ `00355fd`.

## Bugs

**42 — the compare-and-swap base is taken after the editor closes** (`issue_edit`, `tickets.py:1240–1380`; `_commit_plumbed`, `tickets.py:969`).
`text = _body_on_branch(...)` is read **before** `$EDITOR`; `_commit_plumbed` reads `expect = _rev(repo, "refs/heads/<branch>")` **after** it. A commit that changed the same ticket during the edit (another `arena issue …` call, a push, the operator in a second terminal) is therefore not a "moved branch" any more: the new commit is built on the *new* tip with the editor's text, which was made from the *old* one, and the other change is gone. The doc comment of `_commit_plumbed` says "a branch that moved between the read and the write is refused, and nothing is lost" — true only between its own two lines. The checked-out path (`git commit --only -- <rel>`) does the same: it commits the editor's text over whatever the working tree now holds. Fix:
- take the tip sha (`base_sha`) **when the ticket is read**, and pass it to the writer as `expect`; the plumbed path refuses if `refs/heads/<branch>` is no longer `base_sha` *and the ticket's blob changed* (`git rev-parse <tip>:<rel>` differs from the blob read) — a branch that moved for **another file** during a long edit does not stop the edit (the commit is built on the new tip, which is safe because this ticket's blob is the one that was read);
- the checked-out path compares the file's blob/bytes with what was read and refuses on a change;
- the refusal is the usual one (`failed step: the commit`), names `git log -3 --oneline <branch>` first, and keeps the edited text (`the edited text, kept here`).

**43 — a plumbing failure that is not a moved branch crashes** (`issue_edit`, `tickets.py:1389–1398`).
The `hints` expression is `[ {…} if "moved from" in message else [ {…} ] ] + [...]`: for any other `GitRefError` (a corrupt index, `Author identity unknown`, a ref that vanished) the `else` branch is a **list inside the list**, and the refusal printer calls `.get` on it: `AttributeError: 'list' object has no attribute 'get'` at `output.py:171` / `:204`. The operator sees a stack trace on top of a refusal that was already decided, and the kept-text hint is lost. Fix: the conditional picks the *dict* (`{…} if … else {…}`), and a test pins that every hint is a `{why, command}` pair with a non-empty command, in text and `-o json` output, for each of: moved branch, `git read-tree` failure, `commit-tree` identity failure, a missing branch.

**44 — (already fixed by ticket 197, `fedc539`: `ls_tree_names` / `status_paths` in `gitref.py`; do not redo — the round covers 42 and 43 only) `_on_branch`, `_uncommitted` and `_modified` read git the way `rounds.py` did before bugs 206 and 209** (`tickets.py:763`, `:774`, `:798`).
- `_on_branch` lists a branch's tickets with `ls-tree --name-only`, which **quotes** a name holding a non-ASCII letter: `arena issue queue 2 --branch B` on `02-баг.md` says "no ticket 2 … on B". Fix: `ls-tree -z --name-only` (NUL-separated, unquoted), decoded `utf-8`/`surrogateescape`.
- `_uncommitted` and `_modified` read `git status --porcelain` through `git()`, which **strips the whole output**: an unstaged change is ` M path`, its first space is eaten, and `line[3:]` cuts a letter — the refusal reads `uncommitted: pic-tasks/07-x.md`, a file that does not exist. A modified *tracked* file under `.arena/` (excluded by name) is then counted as the operator's work because its name no longer starts with `.arena/`, so a clean checkout shows `(1 modified)`. Fix: `status --porcelain -z --untracked-files=all` read raw (not stripped), parse the two-character status and the path from NUL-separated records (a rename has two paths), and exclude `.arena/` on the whole path.
- Share one reader with `rounds._dirty_tasks` (ticket 197, bug 209) so the two cannot drift again.

## Tests

`tests_bugfix/test_arena_issue_edit_concurrent_209.py` and `test_arena_issue_git_reads_209.py`, each failing on the old code (the editor is a script that, mid-edit, makes the competing commit or edit):
42 — a ticket changed on the checked-out branch while editing is not overwritten (exit 2, the other change still there, the edited text kept); the same on *another* branch (plumbed path); the refusal is the usual one; a branch that moved for **another file** does not stop the edit; an untouched ticket is still edited on both paths;
43 — `GitRefError("git read-tree: cannot read the index")`, `"git commit-tree: Author identity unknown"`, `"branch 'x' does not exist"`, injected: exit 2, three hints, all `{why, command}`, text and json;
44 — a ticket named `02-баг.md` is found on a branch; an unstaged ` M epic-tasks/07-x.md` is named whole; a modified tracked `.arena/…` file is not counted; renames and spaces in names.

## Review material

Review tests (held by the operator, outside the repo): `test_arena_issue_edit_concurrent` (9 failing cases here), `test_arena_issue_git_reads_211`, `test_arena_dirty_tasks_path`.
The operator keeps these outside the repo on purpose: a round that can read the reviewer's fix would copy it, and the competition would measure nothing.

## Acceptance

```bash
python3 -m pytest tests_bugfix -k "209" -q
python3 -m pytest tests -k "arena and (issue or ticket)" -q
python3 -m pytest .smoke_tests/ -q
```
