# KC-48 — an agent that has ended leaves no process running in its worktree: the runner reaps what the agent's commands left behind

**Status:** queued — found live 2026-09-23 in round 86: 36 python processes (4 × `pytest -n=8 … tests` and their 32 xdist workers) ran on in `rounds/86-step-3-7-flash`, reparented to `systemd --user`, after the `bash` call that started them had returned, and past the agent's own `STALLED`.
**Severity:** MEDIUM (a stray 32-worker suite on an 8-core box that is already carrying twelve agents — load average 72 at 19:01 — is what turns other agents' test runs into KC-47's silence stalls)
**File:** `tools/contest/runner.py`
**Symbol:** `run_agent` (`finally`, the terminal-state path), the round's end in `run_round`
**Round:** 92
**Size:** S
**Source:** round 86, `ps` at 19:01:

```
3482802  ppid=2076 (systemd --user)  pgid=sid=3412522  cwd=rounds/86-step-3-7-flash
         /usr/bin/python3 -m pytest -n=8 --timeout=180 --tb=long -rf tests
3482807, 3485054, 3485056 — the same; 8 workers under each
```

36 python processes had their cwd in that worktree; the session leader
3412522 (the Kilo `bash` that spawned them) no longer existed. Started ≈18:53.
The agent's calls in `contest-out/86/step-3-7-flash/events.jsonl` around then
were its own `tests/test_stress_suite.py` runs (18:54:18, `1 failed in 1.24s`)
and `scripts/stress_suite.py --passes 2` (18:55:32 → 19:00:32, `User aborted
the command`). Each returned, and the runner put the agent in `STALLED` at
19:00:32, but the suites kept running until ≈19:02. Nothing in the runner looks
at processes: `finish()` transitions the state and returns.

This round's ticket (FL-3, a script that launches four parallel suites) makes
it likely, but any ticket can: a test that spawns `pytest` and is killed by
`--timeout`, a server started with `&`, a `timeout` that kills a shell but not
its grandchildren.

**Depends on:** KC-6 (`run_agent`, landed `e8c6ad3`).
**Also touches:** `tests/test_contest_runner.py`

---

## What must change

### 1. On every terminal state, reap the worktree's processes

When `run_agent` reaches a terminal state (`READY`, `STALLED`, `ERROR`,
`GAVE_UP`, …), after the session is aborted/closed and before the harvest
reads the tree: every process whose cwd (`/proc/<pid>/cwd`, resolved) is the
agent's worktree or under it, and whose uid is ours, gets `SIGTERM`; those
still alive after a short grace (`REAP_GRACE_SEC = 5`, a module constant,
patched in tests) get `SIGKILL`. The runner's own process and its ancestors
are never candidates.

- A process whose `/proc` entry vanishes or cannot be read mid-scan is skipped,
  not an error.
- Where `/proc` does not exist, the reap is a single `WARNING`, never a crash.

### 2. It is recorded

`state.json`'s agent record gains `"reaped": [{"pid": int, "cmd": <first 120 chars>}]`
(absent when none), and the runner logs one line:
`step-3-7-flash: reaped 36 processes left in the worktree (pytest -n=8 … ×4, …)`.
A scorer reading `state.json` sees that the agent left work running.

### 3. The round's end does it once more for every worktree

After the last agent ends and before the round exits — including on Ctrl-C —
the same reap runs over every agent's worktree, so a process started by a
tool call that returned after the agent's own reap is not left behind.

## Out of scope

- Killing a process while its agent is still running: the runner cannot tell
  an agent's deliberate background server from a stray one, so it does not try.
- Kilo's own `bash` kill semantics.
- Load from the operator's own runs outside `rounds/`.

## Acceptance

- [ ] A test spawns, with its cwd in a fake worktree, a child that ignores nothing and one that ignores `SIGTERM`; after the agent reaches `STALLED` both are gone, the second by `SIGKILL`, and `state.json` lists both under `reaped`.
- [ ] A process with its cwd **outside** the worktree (a sibling worktree, `tmp_path`) is untouched.
- [ ] A process started in `<worktree>/sub/dir` is reaped.
- [ ] The test's own process, with cwd set to the worktree, is never signalled.
- [ ] With `/proc` unavailable (patched), the agent still reaches its terminal state and one `WARNING` is logged.
- [ ] The round-end reap runs on `KeyboardInterrupt`.
- [ ] No test sleeps real time for the grace.
- [ ] `tests` and `tests_bugfix` green; `CollectBridge._shrink` byte-identical.
