# KC-78 — A leg carries an agent that committed on its own branch

**Status:** landed (2026-09-29) — by hand
**Severity:** HIGH
**File:** `tools/contest/workspace.py`
**Symbol:** `_carry_workspaces`, `_reattach`
**Round:** 125
**Size:** S
**Source:** SLOW-1 round 119, `--legs 3`: the only agent to go on to leg 2 was dropped
**Depends on:** KC-43 (round 82, `--legs`)
**Also touches:** `tests/test_contest_workspace.py`

---

## What happened

Round 119 leg 1: one agent GAVE_UP (`commit_not_on_branch, commits_ne_1`). It had run
`git checkout -b fix-slowdown` in its checkout and committed there, on top of
`contest/119/<agent>`. Leg 2 then stopped the whole relay:

    warn: leg 2 was not run: …/119-<agent> is on fix-slowdown, not contest/119/<agent>
    — refusing to carry it into leg 2

`--legs 3` gave nothing: the one agent the legs exist for (GAVE_UP / STALLED) is
exactly the one most likely to have wandered off its branch.

## Fix

Before refusing, `_carry_workspaces` calls `_reattach`: when the round branch's tip is
an ancestor of HEAD (the agent's own branch or a detached HEAD built on the round's
commits), the round branch is moved to HEAD with `checkout -B`, files untouched, and a
warning is logged. A HEAD that dropped or rewrote the round's commits is still refused.

## Tests

`tests/test_contest_workspace.py`: an agent's own branch on top of the round's is
carried on the round's branch with the same HEAD and its uncommitted edit; a detached
HEAD is reattached; a HEAD branched off the base (leg 1's commit dropped) is refused.
