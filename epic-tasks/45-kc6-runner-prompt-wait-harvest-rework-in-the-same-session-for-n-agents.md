# KC-6 — `tools/contest/runner.py`: prompt → wait → harvest → rework, in the same session, for N agents at once, resumable

**Status:** landed `e8c6ad3` — ideal patch from a 7-entry contest, `kc6/*.patch`, scored black-box via `contest-bench/kc6/` (28 scenarios: the Acceptance list plus three agents reworking at once, a silent agent next to a chatty one, Ctrl-C then resume). Winner Sensenova-6-8-var1 (27/28); DeepSeek 27/28 but disqualified (read other agents' folders). What sank the rest: silence clocked on any tap event instead of the session's own (5 of 7), `with ThreadPoolExecutor` joining every worker before Ctrl-C's abort, four `turns.jsonl` lines per turn, a windowed `wait_idle` that aborts a live session at every window edge, `time.time()` subtracted from `time.monotonic()`, `AgentState` as a `typing.Literal`. `tests` + `tests_bugfix` green. Written against `docs/kilo-contest/PROBE.md`.  
**Severity:** CRITICAL  
**File:** `tools/contest/runner.py` (new)  
**Symbol:** `AgentRun`, `AgentState`, `run_agent`, `run_round`, `round_prompt`, `RoundState`  
**Round:** 45  
**Size:** M  
**Source:** `scripts/kilo_hello.py --append-model` is this loop for one agent and two turns: prompt, `wait_idle`, check the disk, prompt again into the same session, `wait_idle`, check again (PROBE.md §4). `--scenario rm-outside` is the same loop with a permission answered on the way. The runner is that loop with the probe's flag replaced by the policy (KC-3), the disk check replaced by harvest (KC-5), the second prompt replaced by `rework_message`, and N of them in a pool. `docs/collect-epics/RUN-THE-EPIC-COMPETITION.md` §Stage 1 supplies the prompt text verbatim.  
**Depends on:** KC-1 … KC-5.  
**Also touches:** `tests/test_contest_runner.py` (new, uses `tests/_kilo_fake.py`)

---

## What happens today

Each half exists and none are joined: a client that can wait, a policy
that can decide, a harvest that can judge, workspaces that can be reset.
The operator still pastes the prompt into N UIs and watches.

## What must change

1. **`AgentState`** — a string enum: `CREATED`, `PROMPTED`, `WAITING`,
   `HARVESTING`, `REWORK`, `READY`, `GAVE_UP`, `STALLED`, `ERROR`. Terminal:
   the last four.

2. **`AgentRun`** — mutable dataclass, JSON-serialisable: `agent: AgentSpec`,
   `workspace: Workspace`, `session_id: str | None`, `state: AgentState`,
   `attempt: int` (0 = first turn), `turns: list[dict]` (per turn: prompt
   kind `initial|rework`, `sent_at`, `idle_at`, `idle_status`,
   `harvest` verdict + reason codes), `permissions: dict` (asked /
   allowed / rejected / gated / gate_failed counters), `questions: int`,
   `last_error: str | None`, `commit: str | None`, `cost: float | None`,
   `tokens: dict | None`.

3. **`round_prompt(agent_name: str, ticket_path: Path, base_sha: str) -> str`**
   — the runbook's "PROMPT STARTS … PROMPT ENDS" block with `<YOUR NAME>`
   replaced, plus one line naming the base sha and one line saying
   permissions outside the worktree are decided by a reviewer and a
   rejection is final for that command (the probe showed models retry;
   say it once up front). Kept as a module string, not read from the
   runbook at runtime (the runbook is documentation and may drift).

4. **`run_agent(run: AgentRun, *, client: KiloClient, tap: EventTap, policy: Policy, config: ContestConfig, ticket_path: Path, out_dir: Path, on_transition) -> AgentRun`**
   — the state machine, single-threaded per agent:
   - `CREATED` → `create_session(provider, model, rules=config.session_rules(), title=f"contest/{NN}/{agent}", agent=kilo_agent)`;
     a `KiloHttpError` here (unknown model, provider down) → `ERROR`
     with the body — the round goes on for the others;
   - `PROMPTED` → `client.prompt(initial or rework text)`; `WAITING` →
     `client.wait_idle(tap, session, config.turn_timeout_sec, on_permission=…, on_question=…)`
     where `on_permission` builds a `PolicyContext` (recent tools via
     `client.tool_parts`, `gate_budget_left` from the counters), calls
     `policy.decide`, `policy.record(...)` to `out_dir/<agent>/decisions.jsonl`,
     returns `(decision.reply, decision.reason)`; `on_question` rejects,
     counts; the third question in one turn → `abort` → `STALLED`;
     no event for `idle_event_timeout_sec` → `abort` → `STALLED`
     (the tap exposes `last_event_at`);
   - idle `status == "error"` → `ERROR`; `"timeout"` → `STALLED`;
     `"closed"` (server went away) → `ERROR`;
   - `HARVESTING` → `harvest(workspace, ticket_path)`: `READY` → `READY`
     (+ `commit`); `REWORK` and `attempt < config.max_rework` →
     `attempt += 1`, `REWORK` → `PROMPTED` with `rework_message`;
     `REWORK` and attempts exhausted → `GAVE_UP` (keep `commit` if any);
   - after every transition: `on_transition(run)` (the round writes
     `state.json`), one line to `out_dir/<agent>/turns.jsonl`;
   - in `finally` on a terminal state: `client.session_info` → `cost`,
     `tokens`; `client.messages` → `out_dir/<agent>.session.json`.

5. **`run_round(config, round_no, ticket_path, workspaces, *, server, out_dir, resume: RoundState | None) -> RoundState`**
   — one `EventTap` per agent directory (the tap is per-directory by
   URL), a `ThreadPoolExecutor(max_workers=config.max_parallel)`, one
   `run_agent` per workspace. `RoundState` = `round_no`, `ticket`,
   `base_sha`, `started_at`, `agents: list[AgentRun]`; written to
   `out_dir/state.json` atomically (tmp + rename, as `tools.backoff.save_state`)
   on every transition. `resume`: agents already terminal are skipped;
   agents mid-flight are restarted from `CREATED` **in the same
   worktree** (the session may be gone with the server; the worktree's
   commits are not — harvest first: if the previous attempt already
   left a READY tree, go straight to `READY` without a new session).
   Ctrl-C: the pool is told to stop, every running agent gets
   `client.abort`, `state.json` is written, the exception propagates.

6. **`SUMMARY` inputs** — `RoundState.table_rows()` returns per agent:
   name, model, state, attempts, turns, permissions counters, questions,
   cost, tokens, commit, last reason. KC-7 renders it.

## Acceptance

- [ ] `tests/test_contest_runner.py` against `tests/_kilo_fake.py` and
      temp worktrees (KC-4's fixtures), policy with a stubbed gate:
      - happy path: the fake's `on_prompt` writes a file, commits once,
        adds a test file, writes `PROGRESS.csv` → one turn → `READY`,
        `state.json` has the commit, `turns.jsonl` has one line;
      - rework path: turn 1 commits without a test → `REWORK`, the
        rework prompt sent to the fake contains `no_test_file`'s sentence,
        turn 2 (fake's second `on_prompt`) adds the test and amends →
        `READY` with `attempt == 1`; the session id is the same in both
        turns (assert on the fake's request log);
      - give up: `max_rework = 1`, both turns lacking a test → `GAVE_UP`,
        `commit` kept;
      - permission mid-turn: the fake emits the recorded
        `external_directory` event; with `tmp_roots = /tmp/*` the reply is
        `once` and `decisions.jsonl` has one `mechanical` line; with
        `tmp_roots = /nowhere` and the stubbed gate saying `reject`, the
        reply is `reject` and the line says `gate`;
      - three questions → `STALLED`, `abort` was requested;
      - `session.error` → `ERROR`; an unknown model (fake returns 400 on
        `POST /session`) → `ERROR` with the body, other agents unaffected;
      - `idle_event_timeout_sec = 1` with a fake that emits nothing →
        `STALLED` within 3 s;
      - two agents with `max_parallel = 1` run sequentially (the fake's
        request log shows no interleaving), with `max_parallel = 2` they
        overlap;
      - resume: kill after turn 1 of a rework path (simulate by raising in
        `on_transition`), restart with `resume=RoundState` → the READY
        agent is skipped, the other restarts and reaches `READY`.
- [ ] `round_prompt` contains the runbook's `next_task.py` and
      `append_task.py` command lines and the agent name.
- [ ] `python3 -m pytest tests -q --timeout=180 && python3 -m pytest tests_bugfix -q --timeout=180` green.

## Out of scope

- Export of patches and the summary file — KC-7.
- Retrying a session that hit `ERROR` on the provider side (HTTP 429 at
  the provider) — it is recorded; the operator reruns with `--resume`.
- Any LLM judgement of the *result* — the harvest is mechanical.

## Self-check before `append_task.py` (required — every item, in the worktree you submit)

Every line below is a way a KC-5 entry lost points on the round bench;
the scorer checks all of them mechanically, so check them yourself first.

- [ ] `python3 --version` on the judge is **3.10.12**. Every new/changed
      module imports there: `python3 -c "import tools.contest.runner"` from the
      repo root **and** from `/tmp` (with `PYTHONPATH` unset — the sys.path
      bootstrap is yours to ship). No backslash and no nested same-quote
      inside an f-string expression (a 3.12-only `f"{x.split("\t")}"`
      is a `SyntaxError` here and scores 0).
- [ ] Exactly **one** commit on top of the base: `git log --oneline <base>..HEAD`
      prints one line. Only this ticket's work is in it — no other KC
      ticket, no "while I was here" fixes; amend, do not stack.
- [ ] `git diff --stat <base>..HEAD` names only the files under **File:**
      and **Also touches:** (plus `.smoke_tests/` links). Never `epic-tasks/`.
- [ ] Names and signatures are the ticket's, verbatim — **Symbol:** is the
      contract the bench calls: `AgentRun`, `AgentState`, `run_agent`, `run_round`, `round_prompt`, `RoundState`. Read the modules this ticket
      builds on before calling them (`tools/contest/harvest.py` — `harvest(ws, ticket_path, *, run_tests=False)` and `rework_message(h, attempt, max_rework)`; `tools/contest/workspace.py` — `Workspace.base_sha`, `.branch`, `.progress_csv`; `tools/contest/kilo_client.py` — `wait_idle(..., idle_event_timeout=)` from KC-12); a keyword you invented
      (`run_tests_flag=`, `base="HEAD"`) is a `TypeError` on every scenario.
- [ ] `python3 scripts/sync_test_tiers.py --check` is clean (the new test
      file has its `.smoke_tests/` symlink; the pre-commit hook runs this).
- [ ] The new test is red without the change: check the test file alone
      out onto the base (`git stash` / `git checkout <base> -- <src>`),
      run it, see it fail; restore.
- [ ] `python3 -m pytest tests -q --timeout=180` then
      `python3 -m pytest tests_bugfix -q --timeout=180`, **sequentially**,
      both green.
- [ ] `CollectBridge._shrink` byte-identical:
      `git diff <base> HEAD -- tools/auto/collect_bridge.py` is empty.
- [ ] Then, and only then, `scripts/append_task.py` from the worktree;
      open `runs/<you>/PROGRESS.csv` and see your row with the sha of the
      one commit (the script stores `--outcome DONE` as `FIXED`; that is fine).
- [ ] What you hand in is `git format-patch <base>..HEAD` of that one
      commit — not a raw `git diff`, not the whole branch, not an empty file.
- [ ] `tests/test_contest_runner.py` runs against `tests/_kilo_fake.py` only:
      `grep -n 'kilo serve\|localhost:.*[0-9]\{4\}' tests/test_contest_runner.py` finds nothing
      that reaches a real server; every scenario in **Acceptance** has a
      test whose name says which one it is.

## Ground rules (same as every round)

- Do not touch `CollectBridge._shrink`.
- No test starts a real `kilo` or calls a live provider.
- Do not edit `epic-tasks/`.
- One commit, no push; a test ships with the change and fails without it.
