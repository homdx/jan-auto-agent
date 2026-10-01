# KC-4 — `tools/contest/workspace.py` + `scripts/contest_reset.sh`: one worktree (or clone) per agent at the base commit, again and again

**Status:** landed `hy3`'s submission (9-entry contest, scored black-box via `contest-bench/kc4/`); two entries (agnes-2-5-flash, glm4-7) crashed on a stale branch left by an externally-removed worktree. Written against `docs/kilo-contest/PROBE.md`.  
**Severity:** HIGH  
**File:** `tools/contest/workspace.py` (new)  
**Symbol:** `Workspace`, `prepare_round`, `reset_worktree`, `attach_clone`, `WorkspaceError`  
**Round:** 43  
**Size:** M  
**Source:** `docs/collect-epics/RUN-THE-EPIC-COMPETITION.md` §Stage 1 does this by hand (`git worktree add ../round-$a -b epic-$a-r01 $BASE; mkdir -p runs/$a`) and warns twice: agents must not share a checkout, and a worktree must never be carried across rounds. The probe added the third fact: the session directory **is** the boundary, so the worktree path is what the policy (KC-3) treats as "inside". The operator also keeps separate clones for some models; those must be usable in place of a worktree.  
**Depends on:** KC-2 (`ContestConfig.rounds_dir`, agent names).  
**Also touches:** `scripts/contest_reset.sh` (new, a thin wrapper), `tests/test_contest_workspace.py` (new)

---

## What happens today

Worktrees are created by a shell loop in the runbook, once, and removed
by hand. Nothing checks that `epic-tasks/` is committed (an untracked
folder is invisible inside a worktree — the runbook's own warning), that
the base resolves, that a previous round's worktree is not silently
reused, or that `runs/<agent>/` starts empty. A rerun after a crash
means removing everything by hand first.

## What must change

1. **`Workspace`** — frozen: `agent: str`, `path: Path` (resolved),
   `branch: str`, `base_sha: str`, `kind: Literal["worktree", "clone"]`,
   `progress_csv: Path` (= `path/runs/<agent>/PROGRESS.csv`).

2. **`prepare_round(repo: Path, config: ContestConfig, round_no: int, base_ref: str, *, clones: dict[str, Path] | None = None) -> list[Workspace]`**:
   - resolve `base_ref` to a sha (`git rev-parse --verify`); error if it
     does not resolve or if `epic-tasks/` has uncommitted changes or
     untracked files at `base_ref`'s tree (`git ls-tree` vs the working
     folder) — the message repeats the runbook's reason;
   - for each agent, in roster order: if `clones` names it →
     `attach_clone`, else `reset_worktree`;
   - returns the list; never touches the repo's own checkout (no
     `checkout`, no `reset` in `repo` itself).

3. **`reset_worktree(repo, rounds_dir, round_no, agent, base_sha) -> Workspace`**
   — path `rounds_dir/<NN>-<agent>`, branch `contest/<NN>/<agent>`:
   - path absent → `git worktree add <path> -b <branch> <base_sha>`;
   - path present and is a worktree of `repo` (`git worktree list --porcelain`)
     → `git -C path checkout -B <branch> <base_sha>` then
     `git -C path clean -fdx -e runs/`; then `runs/<agent>/` is **emptied**
     (a previous attempt's `PROGRESS.csv` must not make `next_task.py`
     hand out nothing);
   - path present but not a worktree of this repo → `WorkspaceError`
     (never delete a folder we did not create);
   - a stale branch `contest/<NN>/<agent>` without a worktree → recreated
     with `-B`.
   Idempotent: calling it twice yields the same sha, an empty `runs/`, and
   `git status --porcelain` empty.

4. **`attach_clone(clone_path, agent, base_sha, branch) -> Workspace`** —
   for an existing clone: it must be a git repo whose `origin` (or any
   remote) contains `base_sha` (`git fetch --all` first, then
   `git cat-file -e`); then `checkout -B branch base_sha`, `clean -fdx -e runs/`,
   empty `runs/<agent>/`. A dirty clone is reset **only** if `--force-clone`
   was given (the wrapper's flag; the function takes `force: bool`);
   otherwise `WorkspaceError` listing the dirty files.

5. **`scripts/contest_reset.sh`** — the operator's one-liner:
   `scripts/contest_reset.sh <round> [base_ref] [--clone name=path …] [--force-clone]`
   → `python3 -m tools.contest.workspace …` and prints one line per
   agent: `laguna  ../rounds/40-laguna  contest/40/laguna  @ <base_ref>  (worktree, fresh)`.
   The last word is `fresh` | `reset` | `clone`.

6. **`remove_round(repo, config, round_no)`** — `git worktree remove --force`
   for each worktree of the round and `git branch -D` its branch; clones
   are left alone. The runbook's stage-5 cleanup, in one call.

## Acceptance

- [ ] `tests/test_contest_workspace.py` on a temp repo with two commits and
      a committed `epic-tasks/` folder: `prepare_round` creates N worktrees
      at the base sha on the right branches with empty `runs/<agent>/`;
      a second `prepare_round` after writing a file, committing, and
      dropping a `PROGRESS.csv` in one worktree brings it back to the base
      sha, empty `runs/`, clean status; a folder at the worktree path that
      is not a worktree raises `WorkspaceError` and is left untouched;
      an untracked file under `epic-tasks/` in the repo raises with the
      runbook's reason; an unresolvable base raises.
- [ ] `attach_clone` on a temp clone: attaches at the base; dirty clone
      raises without `force`, resets with it; a clone lacking the base sha
      raises after fetching.
- [ ] `remove_round` leaves `git worktree list` with only the main
      checkout and no `contest/<NN>/*` branches.
- [ ] The repo's own checkout is never modified by any call (assert `HEAD`
      and `git status` unchanged in every test).
- [ ] `scripts/contest_reset.sh 40` on this repo (manual, in the PR text)
      prints three lines for the default roster.
- [ ] `python3 -m pytest tests -n 4 -q --timeout=180 && python3 -m pytest tests_bugfix -n 4 -q --timeout=180` green.

## Out of scope

- Copying `.collect/` artifacts into worktrees — an agent that needs the
  collect model builds it; not this epic's concern.
- Sparse or shallow worktrees.

## Ground rules (same as every round)

- Do not touch `CollectBridge._shrink`.
- Tests run on temp repos only; never on this checkout.
- Do not edit `epic-tasks/`.
- One commit, no push; a test ships with the change and fails without it.
