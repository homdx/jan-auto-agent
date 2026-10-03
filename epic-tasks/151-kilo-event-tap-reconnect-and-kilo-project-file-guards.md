# 151 — the event-tap reconnect after `PATCH /config` never leaves a dead tap; the `.kilo` project-file guards agree, fail closed and run before the server spawns

**Status:** queued
**Severity:** HIGH
**File:** tools/contest/backend.py
**Symbol:** KiloBackend.__init__, KiloBackend._reconnect_tap, KiloBackend.interrupt, KiloBackend.set_model_limit, tracked_kilo_files, drop_stale_kilo_file, _exclude_kilo_dir
**Round:** 151
**Size:** M
**Also touches:** tools/contest/cli.py (the stale-file drop before `KiloServer.spawn`), tools/contest/runner.py (the `set_model_limit` caller: `pushed_limit` marking, the log line), tests/test_contest_kilo_tap_reconnect.py, tests/test_contest_overflow_wording.py (the `.kilo` tests), tests/_kilo_fake.py

**Depends on:** tickets 147–149 (branch `ctx-overflow-fix`, merged on arena at 6e5daf8).

Found by three independent reviews of 147–149 at 6e5daf8; every item below was confirmed in the code. **Not in this ticket:** the overflow wording gaps (ticket 152), the three environmental test failures (no `kilo` binary, root `chmod`).

---

## A. `_reconnect_tap` (HIGH)

Kilo 7.6.2 answers `PATCH /config` by reloading the workspace and closing its `/event` stream; `_reconnect_tap` then replaces the tap. Today it stops and joins the old tap, starts a new `EventTap`, and assigns `self._tap` last, with no `try`.

1. **A failing start leaves a dead tap.** If `EventTap(...).start()` raises (`os.makedirs`, `open(log)`), `self._tap` is still the stopped old tap. Every later `wait_idle` sees `tap.closed` at once and the agent hears nothing for the rest of its run (live: a 34 s compact waited 600 s). The exception is not a `ContestBackendError`, so the runner logs "not handed to Kilo" although the PATCH returned 200; `pushed_limit` stays unset and every prompt PATCHes — and reloads Kilo — again.
2. **`interrupt()` has no lock.** A Ctrl-C between `old = self._tap` and `self._tap = tap` stops only the old tap; the new one keeps running and the next wait is not woken.
3. **`_tap_args` is set whenever `events_log` is given** (`__init__`), even when the caller passed `tap=` — contradicting its own comment — and `__init__` reads `server.base_url` unconditionally in that case.
4. **Silent, unbounded wait.** The method waits up to `settle` for `tap.closed` and again for `server.connected` plus `join(settle)` (about 15 s) with no log line; if `old.join()` times out, two taps write to the same `events.jsonl`.

### Fix

- Do all the work inside one `try`. Start the **new** tap first (it needs the old one stopped only for the shared log, so join the old tap before the new tap writes its first line, or tag lines with the writer), swap it in only once it is running. On any failure keep a live tap (restart a tap on the same log, retried once) or raise `ContestBackendError`. `self._tap` never points at a stopped tap after the method returns.
- One lock shared by `_reconnect_tap` and `interrupt()`. After the swap, if `_interrupted` is set, stop the new tap too.
- `_tap_args` only when this backend built the tap (no `tap=` passed and the server has a `base_url`).
- Log when either wait runs into its timeout; bound the whole reconnect (one deadline, not 3 × `settle`).
- Runner: mark `pushed_limit` as soon as the PATCH returns 200. A failed reconnect gets its own log line and is not reported as "not handed to Kilo".

## B. The `.kilo/kilo.jsonc` guards

5. **Too broad.** `tracked_kilo_files` refuses the push if *anything* under `.kilo/` is tracked (e.g. `.kilo/rules/x.md`), although only a tracked `.kilo/kilo.jsonc` / `.kilo/kilo.json` is rewritten by the patch; `info/exclude` already keeps the untracked file out of the diff. `drop_stale_kilo_file` checks just `kilo.jsonc`, so the two disagree.
6. **Fails open.** A git failure or timeout returns `[]` and the push reads it as a green light, so a tracked file can be rewritten into the agent's diff.
7. **Too late.** `drop_stale_kilo_file` runs only inside `run_agent` and `finish` (runner.py), after `kilo serve` is up and the backend has subscribed to the workspace. Ticket 148 asked for the deletion "before a resumed agent's server is spawned"; `cli.py` has no call.
8. **Shared exclude.** For a linked worktree `git rev-parse --git-path info/exclude` is the repository's *common* file, so `.kilo/` lands in the operator's real repository's exclude; the docstring calls that harmless.
9. **Agent's own file.** `drop_stale_kilo_file` deletes any untracked `.kilo/kilo.jsonc`, including one the agent wrote on purpose (`policy._kilo_config` sends the edit to the gate, but the harvest then never sees the file).

### Fix

- One shared helper returns the tracked project files among `.kilo/kilo.jsonc` and `.kilo/kilo.json`, and a distinct "git did not answer" result. `tracked_kilo_files` and `drop_stale_kilo_file` both use it; a tracked `.kilo/rules/…` no longer blocks the push; "git did not answer" (error or timeout) raises `KiloLimitRefused` (or retries once, then raises) — never `[]`.
- `cli.py`: drop the stale untracked project file for every agent workspace **before** the round's `KiloServer.spawn`, on a normal start and on `--resume` / `scripts/revive_round.py`; keep the cleanup in `finish`.
- Delete only a file that is exactly what the runner wrote for this model's limit (compare with the limit it pushed) or that was pushed in this run; an agent-authored file survives to the harvest.
- Worktrees: write `.kilo/` to the worktree's own exclude (`--git-path` of a per-worktree path, or `extensions.worktreeConfig`), or skip the exclude edit and delete the file at the end of the run. The operator's main exclude file is unchanged after a push from a worktree workspace.
- **Check live on Kilo 7.6.2** which wins, `.kilo/kilo.jsonc` or `KILO_CONFIG_CONTENT`, and whether a workspace's project file is read on first open. Record the answer in this ticket; if the file cannot override, say so and keep the deletion anyway.

---

## Tests (offline, `tests/_kilo_fake.py`; each fails on 6e5daf8)

1. `EventTap.start` raising inside `_reconnect_tap`: the backend still has a working tap or raises `ContestBackendError`; the next turn's events are heard; one PATCH per size; `pushed_limit` is set.
2. `interrupt()` racing `_reconnect_tap` (a barrier): no tap thread is left running and `wait_idle` returns promptly.
3. A `KiloBackend` built with an injected `tap=` and `events_log` never rebuilds that tap.
4. A reload that does not end the stream: the reconnect is bounded and logs the timeout.
5. A repo tracking `.kilo/rules/x.md` but not `kilo.jsonc`: the push goes through. A repo tracking `.kilo/kilo.jsonc`: `KiloLimitRefused`.
6. A monkeypatched failing / timing-out `git`: `set_model_limit` raises `KiloLimitRefused`, no PATCH is sent.
7. `--resume` with a stale untracked project file: gone before `KiloServer.spawn` (assert the call order).
8. An agent-authored untracked `.kilo/kilo.jsonc` survives to the harvest.
9. A push from a linked-worktree workspace: the main repository's `info/exclude` is byte-identical afterwards.

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
