# KC-19 — A `session.error` the provider marks `isRetryable` is re-prompted into the same session, bounded — not `ERROR`

**Status:** landed `b135e19` — round 58, the third manager-run round (`python3 -m tools.contest run --ticket 58`, eight kenary free slots, `--max-parallel 8`, base `c27bc32`), scored black-box in `contest-bench/kc19/`; winner mimo-v2-5 (`7929e4e`, the only harvest: three ids did not exist — KC-25, muse-spark-1-3 died on `the model's provider interrupted the response stream`, and agnes-2-5-flash / step-3-7-flash / hy3 were STALLED at 1800 s inside their self-check `pytest tests` with the change uncommitted — KC-21). Ideal = the winner + the INFO line naming `data.message`. Was: round 58 of EPIC KC (`docs/kilo-contest/EPIC-KC.md`); found live on 2026-09-20 in the first manager-run round (`python3 -m tools.contest run --ticket 52`). Independent of KC-13/14/17 (different file) and of KC-18 (same file, disjoint lines — a heartbeat and log lines; rebase is trivial). KC-9's `continue` nudge is a different edge (an *idle* that is a cut-off) and stays its own ticket.
**Severity:** HIGH
**File:** `tools/contest/runner.py` (`run_agent`, the `WAITING → ERROR` edge)
**Symbol:** `run_agent`, `RETRY_PROMPT`, `_retryable`, `ContestConfig.max_error_retries`
**Round:** 58
**Size:** S
**Source:** round 52, three kenary free models, `max_parallel = 3`. At 07:53:37 — 210 s after the prompt, both mid-stream — `laguna-s-2-1:free` and `hy3:free` received the **same** `session.error` in the same millisecond: `{"name": "APIError", "data": {"message": "Connection reset by server", "isRetryable": true, "metadata": {"code": "ECONNRESET", …}}}` (`contest-out/52/laguna/events.jsonl`, `hy3/events.jsonl`; `kilo-serve.log` `level=ERROR … The socket connection was closed unexpectedly`). One upstream reset, two of three agents `ERROR` at minute 3 of a 30-minute turn, their worktrees untouched, their sessions still open and resumable — `mistral` survived only because it was inside a tool call at that moment. The provider *says* retry (`isRetryable: true`); the runner's edge `WAITING → ERROR (session.error)` is unconditional (KC-6, `EPIC-KC.md` §7 diagram) and the round table ends with one entry. `contest-bench/kc6/RUNBOOK.md` §10 saw the other kind on the same provider — `Model not found`, `the model's provider rejected the request` — which must stay `ERROR`.
**Depends on:** KC-6 (`runner.py`, landed `e8c6ad3`), KC-12 (the stall clock, `183de9b`).
**Also touches:** `tools/contest/roster.py` (`CONTEST_KEYS`, `ContestConfig`: `max_error_retries`, `error_retry_backoff_sec`), `contest.ini`, `tests/test_contest_runner.py`, `tests/_kilo_fake.py` (a turn's `"error"` payload is already passed through verbatim; a scenario needs a turn that errors and a *next* turn that answers the retry prompt — check that the fake advances its turn index on the retry `prompt_async` as it does on a rework)

---

## What happens today

`run_agent` → `_wait_turn` → `IdleResult(status="error", error=<payload>)` →
`finish(AgentState.ERROR, "session.error: …")`. No look at the payload, no
second prompt, the turn's `sent_at`/`idle_at` recorded, the session
abandoned at the server. `--resume` (KC-16) skips the agent: `ERROR` is
terminal.

## What must change

1. `_retryable(error) -> bool`: the payload is retryable when
   `error["data"]["isRetryable"]` is truthy (Kilo's `APIError` from the AI
   SDK carries it), **or** `error["data"]["metadata"]["code"]` is one of
   `ECONNRESET`, `ECONNREFUSED`, `ETIMEDOUT`, `EPIPE`, `UND_ERR_SOCKET`, **or**
   the message (any of `data.message`, `message`) matches `429`, `502`,
   `503`, `504`, `overloaded`, `rate limit`, `timeout` case-insensitively.
   Anything else — `Model not found`, `provider rejected the request`, a
   context-length error, an unknown shape — is **not** retryable. A payload
   that is not a dict is not retryable.
2. On `idle.status == "error"` with a retryable payload and
   `retries_used < config.max_error_retries`: record the turn with
   `idle_status = "error"` as today (it *is* a turn that ended in an error),
   wait `config.error_retry_backoff_sec × 2**retries_used` seconds on a
   `threading.Event` that the runner's stall/Ctrl-C path can set (never a bare
   `time.sleep` — `test_ctrl_c_aborts_writes_state_and_propagates_then_resume_finishes`
   must stay under its time budget), then `client.prompt(session, RETRY_PROMPT)`
   into the **same** session and go back to `WAITING`. The new turn's `kind`
   is `"retry"`; `attempt` (the rework counter) does not move — a provider's
   reset is not the model's fault and must not eat a rework.
   `RETRY_PROMPT` names the cause and asks to go on:
   `The provider dropped the connection mid-turn (Connection reset by server). Your worktree and this conversation are intact — continue from where you were; do not start over.`
   (the parenthesis is the payload's `data.message`, `_brief`ed).
3. Retries exhausted, or not retryable → `ERROR` exactly as today, the
   `last_error` text prefixed with `after N retries: ` when N > 0.
4. `run_agent` logs the retry at INFO (`laguna: retry 1/2 in 15s — Connection reset by server`)
   — the line KC-18's narration will sit next to.
5. `roster.py`: `max_error_retries` (`int = 2`) and `error_retry_backoff_sec`
   (`int = 15`) join `CONTEST_KEYS` after `max_questions_per_turn`,
   `ContestConfig`, `load_roster`; `contest.ini` gets both with one-line
   comments. `0` retries reproduces today's behaviour byte-for-byte.
6. The `EPIC-KC.md` §7 diagram is not this ticket's to edit; the module
   docstring's state diagram in `runner.py` gains the loop
   `WAITING → (retry) → PROMPTED` with the bound.

## Acceptance

- [ ] `tests/test_contest_runner.py`:
      - the live case: turn 1 errors with the ECONNRESET payload above,
        turn 2 answers with `work_ready` → `READY`, `attempt == 0`,
        `[t["kind"] for t in run.turns] == ["initial", "retry"]`,
        `run.turns[0]["idle_status"] == "error"`, two prompts into **one**
        session, the second prompt's text contains `dropped the connection`
        and `Connection reset by server`, one `POST /session`;
      - `error_retry_backoff_sec=0` in that test; a second test with
        `error_retry_backoff_sec=1` asserts the second prompt arrives ≥ 1 s
        after the error event and the run still finishes in < 5 s;
      - `max_error_retries=1`, two retryable errors in a row → `ERROR`,
        `last_error` starts with `after 1 retries: session.error:`,
        turns are `["initial", "retry"]`;
      - `max_error_retries=0` with a retryable error → `ERROR` after one
        turn, no second prompt (today's behaviour);
      - a non-retryable payload (`{"name": "UnknownError", "data": {"message": "Model not found: kenary/x"}}`)
        → `ERROR` after one turn, no second prompt, even with retries left;
      - `{"name": "APIError", "data": {"message": "502 Bad Gateway"}}` (no
        `isRetryable`) → retried by the message rule;
      - `test_session_error_is_error_with_the_payload` **unmodified** and
        green — its payload (`ProviderError`, `boom-42`) is not retryable;
      - a Ctrl-C (`SIGINT` to the thread, as the existing test does) during
        the backoff wait ends the round within 2 s.
- [ ] `tests/test_contest_roster.py`: both keys parse, default, and are in
      `CONTEST_KEYS`.
- [ ] Every existing `tests/test_contest_runner.py` and
      `tests/test_contest_cli.py` test unmodified and green.
- [ ] `python3 -m pytest tests -q --timeout=180 && python3 -m pytest tests_bugfix -q --timeout=180` green.

## Out of scope

- Retrying `POST /session` or `prompt_async` HTTP failures (`CREATED → ERROR`,
  `prompt failed`) — those are the server, not the provider; a different edge.
- Re-creating the session — the point is the same session, its context
  intact; if the server lost it (`session.status` never arrives), that is
  the stall clock's job (KC-12) and comes back as `STALLED`.
- Kilo's own free-tier retries (`four free-tier retries` in RUNBOOK §10) —
  they happen inside the server before `session.error` is emitted; this
  ticket adds one bounded layer above them.
- KC-9's `continue` for an idle that is a cut-off — separate edge, separate
  prompt, separate bound (`max_continues`).

## Self-check before `append_task.py` (required — every item, in the worktree you submit)

- [ ] `python3 --version` on the judge is **3.10.12**;
      `python3 -c "import tools.contest.runner, tools.contest.roster"` from
      the repo root. No backslash and no nested same-quote inside an
      f-string expression.
- [ ] Exactly **one** commit on top of the base; only this ticket's work.
- [ ] `git diff --stat <base>..HEAD` names only `tools/contest/runner.py`,
      `tools/contest/roster.py`, `contest.ini`, `tests/test_contest_runner.py`,
      `tests/test_contest_roster.py`, `tests/_kilo_fake.py` (plus
      `.smoke_tests/` links). Never `epic-tasks/`.
- [ ] Names and signatures are the ticket's, verbatim — `run_agent`'s
      signature is unchanged; `RETRY_PROMPT` is a module constant;
      `_retryable(error) -> bool`. Read `tools/contest/runner.py` (`run_agent`,
      `_wait_turn`, `finish`, `stall`) and `tests/_kilo_fake.py` (how a turn's
      `"error"` is emitted and how the turn index advances) before adding to
      them.
- [ ] `python3 scripts/sync_test_tiers.py --check` is clean.
- [ ] The new tests are red without the change.
- [ ] `python3 -m pytest tests -q --timeout=180` then
      `python3 -m pytest tests_bugfix -q --timeout=180`, **sequentially**,
      both green.
- [ ] `CollectBridge._shrink` byte-identical.
- [ ] `scripts/append_task.py` from the worktree with the **sha** of the
      one commit — not `HEAD`; hand in `git format-patch <base>..HEAD`.

## Ground rules (same as every round)

- Do not touch `CollectBridge._shrink`.
- No test starts a real `kilo` or calls a live provider.
- Do not edit `epic-tasks/`.
- One commit, no push; a test ships with the change and fails without it.
