# HARDEN-2 — four defects around a call: a closed stdout becomes a transport failure, a failed commit is counted done, the adaptive search blacklists what it scanned, a creative run's own files refuse the next run

**Status:** open — FINAL, written 2026-10-14 against `e9e5d13`; every defect below was reproduced (`run`) or read in the code (`read`) on that commit. The code is the authority, not this ticket: if a line has moved, the symbol names the place.
**Severity:** MEDIUM (B3 changes the loop's behaviour; B1 and B2 lose a received answer or leave work in the tree)
**File:** `tools/auto/coder.py`, `tools/auto/architect.py`, `tools/auto/controller.py`, `tools/auto/commit_on_success.py`, `tools/auto/git_manager.py`, `main.py`, `tools/search_agent.py`
**Symbol:** `Coder.generate` / the Architect streaming callback, `Controller.run` (after `commit_helper.commit`), `CommitOnSuccess.commit`, `_update_synopsis` / `_update_story_bible`, `GitManager.user_changes` / `_OWN_OUTPUTS`, `already_searched` in `main.py`, `SearchAgent.search`
**Round:** —
**Size:** M
**Depends on:** CC review fixes 34–36 (landed, `c2ef95b`): this ticket starts from their code
**Also touches:** `tests/test_auto_stdout_and_commit_failure.py` and one pinned test per defect in `tests_bugfix/`

---

## Why

Two outside reviewers listed small defects; each entry here was checked against the code and **reproduced** or **read** (marked `run` / `read` below). Most of their other entries were already fixed or wrong and are refused in HARDEN-1's table. Each defect below is a wrong answer on an input nobody tested; each needs the guard the sibling code already has. A defect is done when it has its test and the suites are green unchanged (`.smoke_tests/ .regression_tests/`, then `tests_bugfix -n 4`).

## output, commit and the search loop (what the run does around a call)

### B1. A closed stdout turns a received answer into a "transport failure" — `read`

**Where:** `tools/auto/coder.py`, `Coder.generate` (`_coder_on_token`, the `print("═"…)` after the call); `tools/auto/architect.py`, the streaming callback (`sys.stdout.write`).
**Evidence:** the banner before the call is already guarded (`except OSError`), but the token callback and the closing `print` sit **inside** the `try` whose
`except Exception` means *"LLM call failed"* (`error_kind="transport"`). A broken pipe (`| head`, cron with a closed stdout) discards a whole received reply, and
the retry layer pays for it again; in the Architect a batch drops out of the plan.
**Fix:** the token callback and the trailing print are guarded the way the banner is (a helper `_safe_stdout_write` already exists in `validator_agent.py` — use
one shared one). A failure to *print* is logged once and never reaches the transport `except`.
**Test:** `sys.stdout` replaced by an object whose `write` raises `BrokenPipeError`; a fake `request_completion` that streams tokens: `generate` returns the
full text, `error_kind` is not `transport`; same for the Architect callback.

### B2. A task whose commit failed is counted done and its work stays in the tree — `read`

**Where:** `tools/auto/controller.py`, `Controller.run`, after `commit_hash = commit_helper.commit(task, result)`; `tools/auto/commit_on_success.py`, `commit`.
**Evidence:** `commit` returns `None` on a `GitError` (and on "nothing staged"); the controller then does `tasks_done += 1`, logs `commit=none`, runs the
regression check on a tree that was never committed, and moves on. The task's status is **not** set to `DONE` (that happens only with a sha), so the status
file says the task is still open while the counter says it is done; the passed work stays uncommitted and the next task's commit sweeps it up under another
id, or an exhausted task's `git reset --hard HEAD` + `git clean -fd` (`GitManager`, the cleanup after a task that failed without a commit) removes it.
**Fix:** tell the two `None`s apart (a `CommitResult` or an exception kind: `nothing_to_commit` vs `git_error`). On `git_error` the task is **not** counted
done, the run logs it, the work is kept as a patch (`runs/…/uncommitted-<id>.patch`) before anything resets the tree, and the task is marked `FAILED_COMMIT`
(or the nearest existing status) so a resume sees it. "Nothing to commit" stays a quiet success.
**Test:** a fake git manager whose `commit_task` raises `GitError`: `tasks_done` does not grow, the status is not `DONE`, the patch file exists with the
task's diff; a fake with nothing staged: as today.

### B3. The adaptive search blacklists every file it scanned — `read`

**Where:** `main.py` (`already_searched.extend(search_result.get("searched_files", []))`), `tools/search_agent.py` (`searched_this_run`, `already_searched_set`).
**Evidence:** `searched_files` is every candidate file the run **looked in**, and the next iteration skips all of them. The next iteration's
`suggested_searches` are **new names**: a name the validator asks for is searched only in files that were never opened, so a definition that sits in a file
scanned for the first name is never found, and a ref found earlier regresses to missing. (If the intent was "do not re-read a file for the *same* name", the
key must be `(file, name)`, not `file`.)
**Fix:** record what was searched as `(file, ref)` pairs (or skip a file only for the refs already looked up in it); `_source_cache` already avoids re-reading.
**Test:** two iterations, ref `a` unfound in `f.py`, then ref `b` defined in `f.py`: iteration 2 finds `b`. Iteration 2 asking for `a` again does not re-scan `f.py`.
*This one changes the loop's behaviour: measure it, do not assume.* Run the existing search tests and one live validation loop before and after; the
numbers go into the commit message.

---

### B4. A creative run leaves its own `synopsis.md` / `story_bible.md` uncommitted, and the next `--auto` on the folder is refused — `run`

**Where:** `tools/auto/commit_on_success.py` (`commit`, `_update_synopsis`, `_update_story_bible`), `tools/auto/git_manager.py` (`_OWN_OUTPUTS`, `user_changes`),
`tools/auto/controller.py` (`_setup_git`). Introduced by fix 34 (the "refuses a repository with uncommitted changes" guard).
**Evidence:** `commit` calls `self._git.commit_task(...)` first and only then `_update_synopsis(task)` and `_update_story_bible(task)`, which write `synopsis.md` and
`story_bible.md` into `base_dir`. No later commit stages them, so after the last task of a run both are untracked. The next `--auto` runs `_setup_git`, which calls
`user_changes()`; that skips `.agent/`, `IMPROVEMENTS*.md`, `*.coder.bak` and the collect dir but **not** these files. On a git folder with one commit and the three
files written where the real ones are written (`synopsis.md`, `story_bible.md`, `story_bible.md.pending.json`):

```
user_changes() -> ['story_bible.md', 'story_bible.md.pending.json', 'synopsis.md']
```

so the run raises `DirtyTreeError` ("--auto refused", exit 2), blaming the user for changes the agent made. In creative mode the second run on a folder is blocked as soon
as one run has finished; code-mode tasks are not affected. The reviewer's own repro (`cr_repro.py`, an empty `cr/` folder) gave the same three names.
**Fix (pick one, state the choice in the commit):**
1. Add the synopsis, the story bible and its `.pending.json` to the agent's own outputs in `user_changes()`, with the names **taken from config/the objects** (`SummaryMemory`'s
   path, `StoryBible._path` and its `.pending.json` suffix, as `story_bible.py:562` builds it), not hard-coded.
2. Commit them: update the synopsis and bible **before** the task commit so they go in with it, or make a second small commit after (as `ensure_gitignore_committed` does for
   `.gitignore`). Note the order after the commit is deliberate (a failing synopsis update must never block the commit): if the update goes first, it must stay guarded.
3. Record the paths the agent wrote in `.agent/` state and have `user_changes()` exclude exactly those.
The reviewer's pick is 1 or 2; 3 is the most exact but the most code.
**Test:** a creative-mode run of two tasks on a fresh git folder, then `user_changes()`: empty (or the user's own file only); a user's own untracked `notes.md` next to them is still
reported; the second `--auto` start does not raise `DirtyTreeError`; code mode is unchanged.

## Contest

One commit. The judge's own checks are not published and are written after the round, from the entries, so nothing here can be written to pass them: the entries' own tests are cross-tested against every other entry's code, and the judge reads every diff. B3 changes behaviour: the judge runs the existing search tests **and** a live validation loop on the winner before accepting it. B2 needs the winner's status for a failed commit checked against the vocabulary of `state.py`. Strong models only; B2 touches the controller's accounting.

## Not in scope

Parts A, C, D are HARDEN-1 (298), HARDEN-3 (300), HARDEN-4 (301). A new status vocabulary beyond what B2 needs; changing what the validator asks the search for.
