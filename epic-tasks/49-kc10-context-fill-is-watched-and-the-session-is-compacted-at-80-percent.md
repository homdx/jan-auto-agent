# KC-10 — Context fill is measured after every turn; at 80 % the session is compacted, not abandoned

**Status:** queued — after KC-9 (round 48). Written against `f069f07`.  
**Severity:** HIGH  
**File:** `tools/contest/runner.py`, `tools/contest/kilo_client.py`  
**Symbol:** `KiloClient.model_limit`, `KiloClient.session_tokens`, `KiloClient.compact`, `runner.context_fill`, `runner.maybe_compact`  
**Round:** 49  
**Size:** S  
**Size note:** S because every piece is one HTTP call already in the OpenAPI capture of 2026-09-17: `GET /provider` → `all[].models[<id>].limit.context`; `GET /session/{id}` → `tokens.input/output/reasoning/cache.read/cache.write`; `GET /session/{id}/message` → each `AssistantMessage.tokens.total`; `POST /session/{id}/summarize` body `{"providerID","modelID","auto"?}` → event `session.compacted` then `session.idle`.  
**Source:** a rework loop of 3 attempts plus continues on a 32k-context free model overflows: the provider returns `ContextOverflowError` (in the `session.error` enum of this build) and the round is lost with the work sitting in the worktree. Kilo has the fix built in — summarize — but nobody calls it.  
**Depends on:** KC-9.  
**Also touches:** `tests/test_contest_kilo_client.py`, `tests/test_contest_runner.py`, `tests/_kilo_fake.py` (serves `/provider`, `/session/{id}` with tokens, `/session/{id}/summarize`), `contest.ini` (`compact_at_percent = 80`, `context_limit_fallback = 32768`)

---

## What happens today

Nothing reads tokens. `session_info` (KC-1) returns them for the summary
only.

## What must change

1. **`KiloClient.model_limit(provider_id, model_id) -> int | None`** —
   `GET /provider`, find the model, return `limit.context`; `None` if the
   provider or model is absent or has no limit (free-tier custom providers
   often declare none — the probe's `kenary` entries in `kilo.jsonc` set
   only `name` and `reasoning`). Cached per client.

2. **`KiloClient.session_tokens(session) -> dict`** — from
   `GET /session/{id}/message`: the **last assistant message's**
   `tokens` (`total`, `input`, `output`, `reasoning`, `cache`), which is
   the size of the context the next call will carry; plus the session
   total from `GET /session/{id}` for the summary.

3. **`KiloClient.compact(session) -> None`** — `POST /session/{id}/summarize`
   `{"providerID": …, "modelID": …}`; the caller waits for `session.idle`
   afterwards through the normal tap (a `session.compacted` event precedes
   it and is recorded).

4. **`runner.context_fill(client, run, config) -> float | None`** —
   `last_assistant.tokens.input + cache.read` divided by
   `model_limit or config.context_limit_fallback`; `None` when no
   assistant message yet. Written into every `turns.jsonl` line as
   `fill`.

5. **`runner.maybe_compact(...)`** — called after every idle that leads to
   another prompt (a rework or a continue), **before** the prompt is
   sent: `fill >= config.compact_at_percent / 100` → `client.compact`,
   `wait_idle`, `run.compactions += 1`, turn record `compacted: true`,
   then the prompt goes out. A compact that ends in `session.error` →
   `ERROR` (better than a guaranteed overflow). Never more than one
   compact per prompt.

6. **The context overflow itself, if it still happens** — a
   `session.error` whose `error.name == "ContextOverflowError"` on a
   session that has never been compacted → one `compact`, then the
   same prompt again, once; a second overflow → `ERROR`.

## Acceptance

- [ ] `tests/test_contest_kilo_client.py`: `model_limit` returns the fake's
      `limit.context`, `None` for a model without one; `compact` posts the
      right body and the tap sees `session.compacted` then `session.idle`.
- [ ] `tests/test_contest_runner.py`: a fake whose last assistant message
      reports `tokens.input = 27000` on a 32768 model → the rework path
      compacts before the second prompt (request log order:
      `summarize`, then `prompt_async`); at `20000` it does not; a model
      with no limit uses `context_limit_fallback`; a `session.error` with
      `ContextOverflowError` on an un-compacted session → compact + the
      same prompt again → `READY`; the same error twice → `ERROR`.
- [ ] `turns.jsonl` lines carry `fill` and `compacted`; `SUMMARY.md` (KC-7)
      shows `fill%` at the last turn and the number of compactions.
- [ ] `python3 -m pytest tests -q --timeout=180 && python3 -m pytest tests_bugfix -q --timeout=180` green.

## Out of scope

- Estimating tokens ourselves — the server's numbers are the numbers.
- Compacting mid-turn — only between prompts.

## Ground rules (same as every round)

- Do not touch `CollectBridge._shrink`.
- No test starts a real `kilo` or calls a live provider.
- Do not edit `epic-tasks/`.
- One commit, no push; a test ships with the change and fails without it.
