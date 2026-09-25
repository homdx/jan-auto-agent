# KC-62 — Kilo's own database error is a retry, not the end of an agent

**Status:** queued — found 2026-09-25 in round 107 (KC-60): 4 of the round's 5 live agents went `ERROR` within 2 minutes on `Failed to execute statement`. That is Kilo's local SQLite refusing a write, not the model or the provider. All four had uncommitted work, and the round exports none of it.
**Severity:** HIGH (a local, transient error ends an agent for good and throws away its work; one noisy neighbour on the box can empty a round)
**File:** `tools/contest/runner.py`, `tools/contest/cli.py`, `scripts/py_model_test.py`
**Symbol:** `_retryable`, `_RETRYABLE_MSG_RE`, the runner's `session.error` block, intake
**Round:** 109
**Size:** S
**Source:** round 107, `contest-out/107/`:

`kilo-serve.log`, one line per agent. Each is the round's own `kilo serve`
failing a write while it processed the agent's turn:

    09:49:20.324Z message=process session.id=ses_f28202540ffe… error="Failed to execute statement"   space-bunny-alpha-bynara
    09:50:43.825Z message=process session.id=ses_f282027e3ffe… error="Failed to execute statement"   sensenova-6-*
    09:50:53.846Z message=process session.id=ses_f282027f3ffe… error="Failed to execute statement"   sensenova-6-*
    09:51:03.863Z message=process session.id=ses_f282027deffe… error="Failed to execute statement"   sensenova-6-*

    stack: at runLoop (/$bunfs/root/src/index.js:103:21352) … processTicksAndRejections

`state.json` → `ERROR`, `last_error: session.error: {"name": "UnknownError",
"data": {"message": "Failed to execute statement"}}`. `turns.jsonl`: the
agents were still on their `initial` turn, after ~28 min of work. Their
worktrees at the time: `sensenova-6-7-var2` 3 files, `sensenova-6-8-var1`
4 files, `sensenova-6-8-var2` 3 files, `space-bunny-alpha-bynara` 3 files
(+202 lines in `harvest.py` / `gates.py` / a test), all uncommitted, 0
commits. KC-21 harvests a `STALLED`/`ERROR` turn only when it has a commit,
so the round exports nothing for any of them.

**Why the database failed.** Every Kilo process of one user writes the same
database under `~/.local/share/kilo`. The round's `kilo serve` runs with the
operator's `HOME` (checked through `/proc/<pid>/environ`). While the round
ran, the operator's model check (`scripts/py_model_test.py -j 12` for bynara
and `-j 14` for kenary) had about 26 `kilo run` processes going. Each one boots
its own server and writes the same file. Those checks hit the same class of
error themselves:
`Error: Unexpected error | Failed query: insert into "project" (…)`,
`Failed to execute statement`, `Database is busy`. The earlier 12-model run
with no round alive showed the same errors in `kilo run`. The error appears
under concurrent writers; nothing in the agent's session caused it.

**Why the runner ends the agent.** `_retryable` knows provider signals only:
`data.isRetryable`, socket codes, and `_RETRYABLE_MSG_RE` =
`429|502|503|504|overloaded|rate limit|timeout|interrupted the response|upstream unavailable`.
`Failed to execute statement` matches none of them, so it goes to the
fallback `ERROR` path. `max_error_retries = 2` and
`error_retry_backoff_sec = 15` are never used for it.

**Depends on:** KC-19/KC-45 (the retry path and `RETRY_PROMPT`), KC-21 (harvest of an `ERROR` with a commit).
**Also touches:** KC-41 (committing uncommitted work at a deadline), KC-61 (the other `session.error` that ends an agent for a reason outside the model).

---

## What must change — file by file

Line numbers are at `de8843d`. Find by the quoted anchor if they drifted.

### 1. `tools/contest/runner.py` — a Kilo-local error class next to the provider one

**Where:** right under `_RETRYABLE_MSG_RE` (≈ line 188, anchor
`r"|interrupted the response|upstream unavailable"`).

**Add:**

```python
#: KC-62: Kilo's own store refusing a write — the round server's SQLite, shared
#: with every other Kilo process of the same user. Not the provider, not the
#: model: the session is intact and the same turn can go on.
_LOCAL_STORE_RE = re.compile(
    r"Failed to execute statement|Failed query:|database is locked|database is busy"
    r"|SQLITE_BUSY|SQLITE_LOCKED|disk I/O error", re.IGNORECASE
)


def _is_local_store(error) -> bool:
    """KC-62: True when *error*'s message is a Kilo-local store failure."""
    return bool(_LOCAL_STORE_RE.search(_error_message(error)))
```

`_error_message` already exists (≈ line 226). It reads `data.message`, which
is where round 107's payload carries the text:
`{"name": "UnknownError", "data": {"message": "Failed to execute statement"}}`.
Do **not** add these texts to `_RETRYABLE_MSG_RE`: they need their own budget
(§2), and `_retryable` is also used for the gate-side reasoning.

### 2. `tools/contest/runner.py` — `run_agent`, the `idle.status == "error"` branch

**Where:** `run_agent`. The counters are declared at ≈ line 1432–1433
(`continue_used = 0`, `retries_used = 0`); add `local_retries = 0` next to
them. The decision is at ≈ line 1545, anchor
`# KC-45 §2a: KC-19's rules decide most`.

**How:** before `retryable = False`, insert a local-store branch that reuses
the backoff/wait loop the provider retry already has (≈ lines 1556–1584).
Move that loop into a small inner helper `_wait_backoff(seconds) -> bool`
(returns False when `stalled` or `backend.interrupted()`), so both
branches call it and nothing is copied:

```python
                if (not overflow and _is_local_store(idle.error)
                        and local_retries < int(config.max_local_store_retries)):
                    run.turns.append(turn)
                    _append_jsonl(agent_dir / "turns.jsonl",
                                  {"agent": spec.name, **turn, "cause": "local_store"})
                    local_retries += 1
                    base = float(config.local_store_retry_backoff_sec) * (2 ** (local_retries - 1))
                    backoff = base * random.uniform(0.7, 1.3)   # agents that failed together
                    _log.info("%s: kilo store error — retry %d/%d in %.0fs",
                              spec.name, local_retries,
                              int(config.max_local_store_retries), backoff)
                    if not _wait_backoff(backoff):
                        ...  # the same `if stalled:` block the provider path has
                    retry_text = RETRY_PROMPT.format(reason="kilo store error")
                    continue
```

- Same session, `RETRY_PROMPT`. Do not use `fresh_session`: the transcript is
  not lost. Only the write of this step failed.
- `retries_used` (the provider budget) is **not** touched. A 429 and a
  store error in one turn each use their own counter.
- `local_retries` resets where `continue_used` resets on a rework
  (≈ line 1699, anchor `# KC-22: a rework resets the per-attempt continue counter`).
- `import random` at the top (the stdlib only).
- When the budget is spent, fall through to today's fallback (≈ line 1586,
  anchor `# this is the fallback for every other`). The `error` string gets
  a prefix: `after {local_retries} kilo store retries: session.error: …`.

### 3. `tools/contest/runner.py` — the work survives the last retry

**Where:** the KC-21 block after the branch (≈ line 1648, anchor
`# KC-21: a turn that died with a commit under it`). Today it harvests only
`if above:` (commits above base). Round 107's four agents had 0 commits and
a dirty tree, so they were dropped.

**How:** until KC-41 lands, the smallest safe step is to **not make
this kind of `ERROR` terminal for `--resume`**:

- Add `resumable: bool = False` to `AgentRun` (≈ line 574, next to
  `last_error`). Add it to the `to_dict`/`from_dict` field tuple too
  (≈ line 601, anchor `"last_error", "commit", "cost", "tokens", "reaped"`).
  An old `state.json` without the key reads as `False`.
- Set `run.resumable = True` when the final state is `ERROR`, the cause is
  local-store, and `_dirty_tree(ws)` is non-empty. Wrap that call in
  `TreeReadError`, as the KC-54 branch above does.
- In `_plan` (≈ line 1750, anchor `elif not run.terminal:`), change the
  condition to `elif not run.terminal or run.resumable:`. The existing
  mid-flight path then runs: harvest if it has a commit, otherwise
  restart with `dirty_on_resume` (KC-22), with the agent's own worktree
  and prompt.

When KC-41 (the deadline commit) lands, it replaces this with a commit made
on the spot. Leave a `# KC-41 supersedes` comment on the `resumable` line.

### 4. `tools/contest/roster.py` + `contest.ini` — two keys

**Where:** `CONTEST_KEYS` (≈ line 91, next to `"max_error_retries"`), the
dataclass fields (≈ line 233), the loader (≈ line 531, next to
`max_error_retries=limit(...)`), and `contest.ini` under
`error_retry_backoff_sec` (line 75):

```ini
# KC-62: Kilo's own store errors ("Failed to execute statement"): retried in the
# same session with their own budget, backoff doubled and jittered ±30 %.
max_local_store_retries       = 5
local_store_retry_backoff_sec = 10
neighbour_kilo_warn           = 4
```

Defaults are the same three numbers in the dataclass and in `limit(...)`, so a
roster without them behaves as documented.

### 5. `tools/contest/cli.py` + `runner.py` — the round sees its neighbours

**Helper** (new, `tools/contest/kilo_client.py`, next to `KiloServer`):

```python
def kilo_neighbours(server_pid: int) -> tuple[int, str]:
    """KC-62: (count, data_dir) of the other Kilo processes that write the
    same store as *server_pid*. data_dir comes from that server's own
    /proc/<pid>/environ: XDG_DATA_HOME, else HOME + "/.local/share", + "/kilo".
    A process counts when its argv[0] basename is "kilo", it is not
    *server_pid* nor a child of it, and its own environ resolves to the same
    data_dir. Fail-open: (0, "") when /proc is unreadable (not Linux, no perms)."""
```

It reads `/proc`. No `ps` and no hard-coded paths. The server pid is
`KiloServer.pid` (`kilo_client.py` ≈ line 370).

**Callers:**
- Intake, after the server starts in `cmd_run` (`cli.py` ≈ line 1393,
  after `_make_backends(...)`): if `count > config.neighbour_kilo_warn`,
  print `kilo: N other Kilo processes share <dir> — database errors likely`
  to stderr. This is not a refusal.
- `_Heartbeat.line()` (`runner.py` ≈ line 1920): append
  ` · kilo neighbours N` to the line when above the threshold. `_Heartbeat`
  gets the pid through its constructor (default `None` means no check), so
  every existing caller is unchanged.

### 6. `scripts/py_model_test.py` — the check does not outrun a round

- `RETRY_RE` (line 141): add `Failed query:` next to
  `Failed to execute statement`.
- In `main()` after `args = ap.parse_args()`: if a `kilo serve` of this
  user is alive (`/proc/*/cmdline` has `kilo` and `serve`) and
  `args.jobs > 4` and not `args.force`: print
  `живой раунд (kilo serve pid N): -j снижен до 4, --force чтобы оставить`
  and set `args.jobs = 4`. Add `--force` to the parser.

## Tests — where they go

- `tests/test_contest_runner_local_store.py` (new, docstring
  `"""KC-62: Kilo's own store errors are retried in the same session."""`).
  Use the fake backend the KC-45 tests use; copy its setup from
  `tests/test_contest_*retry*` or `*kc45*`. Find it with
  `grep -ln "interrupted the response" tests/`.
- The `_plan` resumable case goes in the same file. It builds a
  `RoundState` with `resumable=True` and checks that `_plan` restarts the
  agent.
- The `kilo_neighbours` test fakes `/proc` with a tmp dir: pass the root as
  a keyword `proc_root=Path("/proc")` so the test can point it elsewhere.
- Tier it with `python3 scripts/sync_test_tiers.py`, and **`git add` the
  symlink** (KC-60).

## Out of scope

- The deadline commit itself (KC-41): §3 only keeps the work resumable until KC-41 lands.
- A separate Kilo data directory per round. The provider credentials live in the same directory, and copying them is not the runner's job. Noted as the real isolation, for the operator to decide.
- Fixing Kilo's own write concurrency.

## Acceptance

- [ ] A fake `session.error` `{"name":"UnknownError","data":{"message":"Failed to execute statement"}}` on the first turn: the agent gets `RETRY_PROMPT` in the same session after the backoff, not `ERROR`. The same for `Failed query: insert into "project" …` and `database is locked`.
- [ ] Six such errors in a row (budget 5): the agent ends `ERROR` with `resumable: true` in `state.json` when its tree is dirty, and `false` when it is clean. `--resume` on that `state.json` restarts it with `dirty_on_resume`, and does not restart a plain `ERROR`.
- [ ] An old `state.json` without `resumable` loads, and every agent reads `False`.
- [ ] A provider 429 in the same turn uses the provider budget, not the local one, and the other way round.
- [ ] Two agents failing in the same second retry at different times (jitter), in a test with a fake clock.
- [ ] Intake with 5 fake neighbour Kilo processes on the server's data dir prints the warning; with 3, it prints nothing. The data dir comes from the server's environment in the test, not from a constant.
- [ ] `py_model_test.py -j 12` with a `kilo serve` running: warned and capped at 4; with `--force`: 12.
- [ ] `tests` and `tests_bugfix` green, sequentially; `CollectBridge._shrink` byte-identical.
