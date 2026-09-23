# KC-46 — an agent's own worktree is not forbidden ground just because the rounds folder that holds it is

**Status:** landed `adbbfd1` (2026-09-23, by hand, during round 86 run 4) — queued — found live 2026-09-23 in round 86: `step-3-7-flash` was refused `ls` and `head` on files in its own worktree, by absolute path, twice.
**Severity:** MEDIUM (a mechanical, gate-free reject of the most harmless command there is — reading your own file — — 14 of the 17 `forbidden: …/rounds/…` rejects on this box, in six rounds)
**File:** `tools/contest/runner.py`, `tools/contest/policy.py`
**Symbol:** `run_agent` (the `PolicyContext(...)` built per permission), `Policy._mechanical`, `_forbidden_match`
**Round:** 90
**Size:** S
**Source:** round 86, `contest-out/86/step-3-7-flash/decisions.jsonl`:

```json
{"command": "ls /home/renat/Project/opensource/github/agent-offline/rounds/86-step-3-7-flash/tests/test_stress_suite.py 2>/dev/null || echo \"no test_stress_suite.py yet\"",
 "layer": "mechanical", "reply": "reject",
 "reason": "forbidden: /home/renat/Project/opensource/github/agent-offline/rounds/86-step-3-7-flash/tests/test_stress_suite.py is at or under /home/renat/Project/opensource/github/agent-offline/rounds"}
```

and the same for `python3 …/86-step-3-7-flash/scripts/sync_test_tiers.py --help`.
The worktree is `rounds/86-step-3-7-flash`; the path is inside it.

The cause is one line, `runner.py:566`, there since KC-6 (`e8c6ad3`):

```python
# the other agents' worktrees are this one's siblings: never theirs to read
forbidden=tuple(HARD_DENYLIST) + (ws.path.parent,),
```

`ws.path.parent` is the whole rounds folder, and the agent's own worktree is
in it. `Policy._mechanical` checks `forbidden` **before** the worktree
(`policy.py:600-606`, deliberately — `test_forbidden_beats_the_worktree`), so
the agent's own path is claimed by the parent entry first and never reaches
`_inside_worktree_or_tmp`. The comment and the `PolicyContext` docstring both
say *the round's other worktrees*; the code forbids all of them, this one
included.

Relative paths do not hit it (they resolve against the worktree and never
match the absolute parent), which is why it is intermittent: it fires the
moment a model spells its own file absolutely — which weak models do all the
time.

Every `forbidden: …/rounds/…` reject in the rounds on this box, split by whose
path it was (`contest-out/` in qwen25 and qwen26):

| round | agent | own worktree | another worktree |
|---|---|---|---|
| 62 | hy3 | 1 | 0 |
| 64 | agnes-3-0-flash | 2 | 0 |
| 64 | step-3-7-flash | 1 | 0 |
| 74 | mimo-v2-5 | 1 | 0 |
| 85 | glm-4-7-flash | 1 | 0 |
| 85 | hy3 | 2 | 0 |
| 85 | laguna-s-2-1 | 0 | 1 |
| 85 | mimo-v2-5 | 3 | 2 |
| 85 | step-3-7-flash | 1 | 0 |
| 86 | step-3-7-flash | 2 | 0 |

14 wrong rejects, 3 right ones. The three right ones must stay rejected.

**Depends on:** KC-6 (`run_agent`'s `PolicyContext`, landed `e8c6ad3`), KC-3/KC-4 (the policy layers).
**Also touches:** `tests/test_contest_policy.py`, `tests/test_contest_runner.py`

---

## What must change

### 1. A forbidden entry that contains the worktree does not claim the worktree

In `Policy._mechanical`, a resolved path inside `ctx.worktree` is not matched
against a `forbidden` entry that is the worktree itself or one of its
ancestors. It is still matched against every other entry — an entry *inside*
the worktree (a `.git`, the `.ssh` in `test_forbidden_beats_the_worktree`)
still wins over the worktree, exactly as today.

This belongs in the policy, not only in the runner: the rounds folder also
holds worktrees of *earlier* rounds (`85-*` next to `86-*`), so replacing the
parent with a list of this round's siblings would open those. The parent
entry stays; it just stops covering the one directory the agent owns.

### 2. The runner's comment says what the entry does

`runner.py:565`: the rounds folder is forbidden except for this agent's own
worktree — its siblings in this round and every earlier round's.

## Out of scope

- The gate failing on reads it should never see — KC-37.
- `/dev/null` as a path — KC-28.
- Which paths count as the worktree for `tmp_roots` globs — unchanged.

## Acceptance

- [ ] Policy: with `worktree = <tmp>/rounds/86-a` and `forbidden = HARD_DENYLIST + (<tmp>/rounds,)`, a `bash` ask for `head -3 <tmp>/rounds/86-a/scripts/x.py` is `("once", "mechanical")`, no gate call.
- [ ] Same context, `cat <tmp>/rounds/86-b/x.py` is `("reject", "mechanical")` with `forbidden` in the reason — a sibling of this round.
- [ ] Same context, `cat <tmp>/rounds/85-a/x.py` is `("reject", "mechanical")` — an earlier round's worktree.
- [ ] A command naming both its own file and a sibling's (`diff <own>/a.py <tmp>/rounds/86-b/a.py`) is rejected.
- [ ] `test_forbidden_beats_the_worktree` and `test_a_sibling_worktree_is_forbidden_ground` pass untouched.
- [ ] Runner: an agent whose permission ask names its own worktree by absolute path gets `once` in `permission.replied`, and `decisions.jsonl` has `layer: mechanical`, reason `inside worktree/tmp_roots`.
- [ ] `tests` and `tests_bugfix` green; `CollectBridge._shrink` byte-identical.
