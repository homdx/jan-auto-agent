# KC-59 — each agent works in its own clone and its own tmp dir: a `git stash` in one worktree pops another agent's work

**Status:** landed `06104f1` + `98fc1bc` (2026-09-25) — round 106 winner sensenova-6-8-flash-lite-var2 (21/21 on the shared bench, the only full score; 8 entries, 5 of them uncommitted). Was queued: found 2026-09-25 judging round 103 (FL-9) in `/mnt-fs/jan-auto-agent`: `hy3`'s worktree holds, byte for byte, `sensenova-6-7-flash-lite-var1`'s `gate1_grounding.py`, and hy3's own implementation exists only as an unreachable stash commit.
**Severity:** HIGH (one agent's code silently lands in another's tree; the round scores the wrong author, and the real author's work is lost unless someone runs `git fsck`)
**File:** `tools/contest/workspace.py`, `tools/contest/runner.py`
**Symbol:** `prepare_round`, `_KIND_CLONE`, `attach_clone`, `PolicyContext.tmp_roots`
**Round:** 106
**Size:** S
**Source:** round 103, `contest-out/103/<agent>/events.jsonl` and the repo's object store:

Git worktrees share one `refs/stash`: a single stack for the whole repo.
Two agents ran the same idiom, "stash my change, run the new tests, pop":

| t | agent | command | stack after |
|---|---|---|---|
| +22.2 m (08:05:11) | hy3 | `git stash push -- tools/auto/gate1_grounding.py && pytest … ; git stash pop` | [hy3] |
| +23.8 m (08:06:44) | sensenova-6-7-var1 | `git stash push -q tools/auto/gate1_grounding.py && pytest … ; git stash pop` | [s67v1, hy3] |
| ≈08:06:50 | hy3's `pop` | takes the **top**, s67v1's | [hy3] |
| ≈08:06:50 | s67v1's `pop` | takes hy3's | [] |

Evidence:
- The blob in `rounds/103-hy3/tools/auto/gate1_grounding.py` is `e51c0da`. That is exactly the file in stash commit `64f5b67`, "WIP on contest/103/sensenova-6-7-flash-lite-var1", 08:06:44.
- hy3's own file is blob `7751208` in unreachable stash commit `a2080a6`, "WIP on contest/103/hy3", 08:05:11.
- Both files have mtime 08:06:49–51.
- sensenova-6-7-var1 noticed something (at +59.5 m it listed its files' mtimes). It restored its own code from a backup it had made in `/tmp/kilo/fl9_block.py`.
- hy3 never noticed. It ran its suites on the other agent's code and went `STALLED` with it.

`mimo-v2-5` ran a bare `git stash` at +11.1 m, on the same shared stack. It got
its own change back only because nobody else stashed in those seconds.

The tmp dir is shared the same way. Every agent gets `/tmp/kilo` in
`tmp_roots`, one flat directory. In round 103 it held `fl9_tests.txt`,
`fl9_bugfix.txt`, `tests_run.log`, `suite_tests.log`, `fl9.patch` and
`patch/` from four agents. No overwrite is proven this round: each of those
names had one writer. But two agents on the same ticket reach for the same
names, and one agent `tail`ing another's `fl9_tests.txt` would read someone
else's green.

**Depends on:** KC-4 (`prepare_round`, `attach_clone`).
**Also touches:** KC-48 (the reap scans the workspace path, whatever kind it is), KC-46 (the policy's own worktree allow).

---

## What must change

### 1. A fresh local clone per agent, by default

`prepare_round` makes each agent's checkout with
`git clone --local --no-checkout <repo> <rounds>/<NN>-<agent>`, then
`checkout -B contest/<NN>/<agent> <base>`. `--local` hardlinks the objects,
so a clone costs about what a worktree does. `Workspace.kind` is `"clone"`.

Each clone has its own `refs/stash`, index, `HEAD`, branches and config.
`git stash`, `git checkout` and `git branch -D` stay inside the agent.

- `ContestConfig.workspace_kind = "clone" | "worktree"`, default `"clone"`. `"worktree"` keeps today's behaviour for anyone who needs it.
- The clone's `origin` is the operator's repo. Today `git push` is refused only by the LLM gate's prompt (`policy.py`, "REFUSE, without exception … git push"), not mechanically. A worktree pushing would write the same refs, so this is no new hole, but the clone closes it for good: `prepare_round` sets `git remote set-url --push origin DISABLED` in each clone, so a push fails in git itself, whatever the policy answers.
- The harvest, the export (`git format-patch <base>..HEAD`) and the gates run in the clone exactly as in a worktree: they only use the workspace path. The operator lands with `git fetch <clone> contest/<NN>/<agent>` or `git am`, as today.
- `--resume` and `--fresh` treat a clone like a worktree: reuse it if it is at the base and clean, refuse or rebuild otherwise (KC-23's rules).

### 2. A tmp dir per agent

`tmp_roots` for an agent is `<tmp_root>/<agent>/`, created at round start.
The round prompt names it as the agent's scratch dir, and the policy allows
it and not the other agents' dirs. The shared root itself (whatever
`tmp_roots` names; `/tmp/kilo` on this box) stays readable for anything the
operator put there, but is no longer where agents write. Nothing in the
runner names a path: the per-agent dir is derived from the configured root
and the agent's name.

## Out of scope

- Hiding the other agents' checkouts from `ls` (KC-46's forbidden-path rule already covers reads into them).
- Restoring round 103's hy3. The judge rebuilt hy3's entry from `a2080a6` for scoring, by hand.

## Acceptance

- [ ] Two agents' checkouts from `prepare_round`: `git stash push` in one and `git stash pop` in the other → the second says "No stash entries found", and the first's `git stash list` still shows its entry.
- [ ] The harvest and `format-patch` of a clone produce the same verdict and patch as of a worktree with the same commit.
- [ ] `git push origin HEAD` from a clone fails (push URL `DISABLED`), and the operator's repo gains no ref.
- [ ] `workspace_kind = "worktree"`: `prepare_round` is byte for byte today's.
- [ ] Two agents get two different tmp dirs. A write by one to the other's dir is rejected by the policy.
- [ ] `tests` and `tests_bugfix` green, sequentially; `CollectBridge._shrink` byte-identical.
