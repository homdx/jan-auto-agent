# KC-51 — a relative path in a command resolves against the agent's worktree, not the runner's cwd

**Status:** queued — found live 2026-09-23 in round 86 (run 4, base `27adb12`).
**Severity:** HIGH (harmless commands go to a gate that fails, and a sibling's worktree reached by `../` is judged "outside" instead of "forbidden", so a gate `allow` lets an agent read another entry)
**File:** `tools/contest/policy.py`
**Symbol:** `_extract_paths`, `_resolve`
**Round:** 95
**Size:** S
**Source:** round 86, `contest-out/86/hy3/decisions.jsonl`:
`ln -sf ../tests/test_stress_suite.py .smoke_tests/test_stress_suite.py` →
`gate-failed` / `empty reply` → `reject`. Checked by hand, with the runner's cwd
at the orchestrating checkout (`qwen25`):

```
_extract_paths(bash "ln -sf ../tests/x.py …; ls ./a")
  ../tests/x.py → /…/agent-offline/tests/x.py      (the agent meant /…/rounds/86-hy3/tests/x.py)
  ./a           → /…/agent-offline/qwen25/a
```

`_command_paths` keeps `./…` and `../…` tokens (KC-13). `_extract_paths` then
calls `Path(target).resolve()`, which resolves against the **runner process's**
cwd. Kilo runs the agent's `bash` in the agent's worktree. Two consequences:

- `./x` and `../x` inside the worktree are "outside", so they go to the gate and
  are refused whenever the gate fails (KC-37).
- `cat ../86-b/x.py` from `rounds/86-a` names the sibling worktree `rounds/86-b`,
  which is forbidden (KC-46 keeps the rounds folder forbidden). Resolved from
  `qwen25` it becomes `/…/agent-offline/86-b/x.py`: not forbidden, just
  outside. It goes to the gate, and an `allow` hands over another entry.

**Depends on:** KC-13 (the command scan, landed `3e4b41b`), KC-46 (the rounds-folder rule).
**Also touches:** `tests/test_contest_policy.py`

---

## What must change

1. `_extract_paths(props, base=None)`: a target that is not absolute after `~`
   expansion is joined to *base* before it is resolved. `_mechanical` passes
   `ctx.worktree`. With no *base* (a direct caller, an old test), behaviour
   is today's.
2. `cd` inside the command is **not** followed: the path is judged against the
   worktree, the directory the command starts in. A `cd /elsewhere && cat ./x`
   still has `/elsewhere` as a path of its own and is judged by it.
3. `external_directory` patterns are absolute in Kilo's events and are not
   affected.
4. Out of scope: `ln -s <target> <link>`, whose target is relative to the
   link's folder, not to the cwd. It is judged against the worktree like any
   other token. The round 86 `ln -sf ../tests/x.py .smoke_tests/x.py` then
   names `rounds/tests/x.py`, which is under the forbidden rounds folder. An
   agent can write `ln -sf` with an absolute target, or use
   `scripts/sync_test_tiers.py`, which makes the link itself.

## Acceptance

- [ ] With `worktree = <tmp>/rounds/86-a`, the runner's cwd elsewhere (`monkeypatch.chdir`):
  - `ls ./a` → the path is `<tmp>/rounds/86-a/a`, `("once", "mechanical")`;
  - `cat ../86-b/x.py` with `forbidden = HARD_DENYLIST + (<tmp>/rounds,)` → `("reject", "mechanical")`, reason `forbidden`, no gate call;
  - `cat ./scripts/x.py` → `("once", "mechanical")`, no gate call.
- [ ] `_extract_paths` without *base* returns exactly today's pairs.
- [ ] Every existing policy test green; `tests` and `tests_bugfix` green; `CollectBridge._shrink` byte-identical.
