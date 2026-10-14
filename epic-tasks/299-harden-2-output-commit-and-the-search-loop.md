# HARDEN-2 — three defects around a call: a closed stdout becomes a transport failure, a failed commit is counted done, the adaptive search blacklists what it scanned

**Status:** open — FINAL, written 2026-10-14 against `e9e5d13`; every defect below was reproduced (`run`) or read in the code (`read`) on that commit. The code is the authority, not this ticket: if a line has moved, the symbol names the place.
**Severity:** MEDIUM (B3 changes the loop's behaviour; B1 and B2 lose a received answer or leave work in the tree)
**File:** `tools/auto/coder.py`, `tools/auto/architect.py`, `tools/auto/controller.py`, `tools/auto/commit_on_success.py`, `main.py`, `tools/search_agent.py`
**Symbol:** `Coder.generate` / the Architect streaming callback, `Controller.run` (after `commit_helper.commit`), `CommitOnSuccess.commit`, `already_searched` in `main.py`, `SearchAgent.search`
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
id, or its exhaustion reset removes it.
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

## Contest

One commit. Score by the shared tests in `contest-bench/299/` (each defect's reproduction as a script that exits non-zero on the bug), not by the entries' own tests; cross-test every entry's tests against every other's code. B3 changes behaviour: the judge runs the existing search tests **and** a live validation loop on the winner before accepting it. B2 needs the winner's status for a failed commit checked against the vocabulary of `state.py`. Strong models only; B2 touches the controller's accounting.

## Not in scope

Parts A, C, D are HARDEN-1 (298), HARDEN-3 (300), HARDEN-4 (301). A new status vocabulary beyond what B2 needs; changing what the validator asks the search for.
