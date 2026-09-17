# RUN-10 — The HTTP retry budget of every auto-mode LLM call is hard-wired

**Status:** queued — opens after the RUN-9 live verification (`../prep-run9/BASELINE.md`, runs of 2026-09-17). Not a contest: size S, one design, landed by hand in one commit.  
**Severity:** MEDIUM  
**File:** `tools/llm_stream.py`  
**Symbol:** `request_completion` — the signature defaults `error_retries=60, error_retry_wait_sec=10.0, max_retry_after_sec=180.0` (≈ 1136) and the eight `tools/auto` call sites that inherit them: `architect.py` ≈ 1402 / 2753, `gate1_filter.py` ≈ 1702 (`_check_presence._call`), `coder.py` ≈ 633 / 696, `inner_loop.py` ≈ 754 (`_call_validator`), `story_bible.py` ≈ 968, `summary_memory.py` ≈ 853  
**Round:** 38  
**Size:** S  
**Source:** live runs on `../testtext6` / `../testtext2`, 2026-09-17 (`sensenova`, `[collect] auto_refresh_between_tasks = true`). 12:54–12:58: `HTTP 429 … waiting 60.0s`, `Connection reset`, `Connection timed out` during the collect Pass B rebuild — that `60.0s` is `[collect] error_retry_wait_sec`. The operator lowered it to 15 s to shorten the wait they were watching; the Gate 1 presence calls that came next (and the architect, coder, Gate 2, story-bible and summary calls after them) still waited `request_completion`'s built-in 10 s × 60 attempts, 180 s Retry-After cap. Nothing in `run.log` or in either ini says which budget a given call is under; `agents.ini` documents `error_retries / error_retry_wait_sec / max_retry_after_sec` once, under `[collect]`, and the comment says "these were previously hardcoded (2 / 60 / 180) and ignored this file" — true for Pass B only.  
**Depends on:** nothing (RUN-9 `4ee7a28` is the precedent for reading a per-phase knob with a malformed-value warning: `[gate1] llm_call_retry_max`, `presence_empty_retries`)  
**Also touches:** `tools/auto/architect.py`, `tools/auto/gate1_filter.py`, `tools/auto/coder.py`, `tools/auto/inner_loop.py`, `tools/auto/story_bible.py`, `tools/auto/summary_memory.py`, `tools/auto/pipeline.py` (one start-of-run log line), `agents.ini` (`[loop]`), `agents_128k.ini` (`[loop]`, same keys — committed via `git hash-object -w` + `git update-index --cacheinfo`, the working copy carries a key and is never committed as-is), `tests/test_llm_stream*.py`, `tests/test_gate1*.py`, `tests/test_coder*.py`, `docs/` wherever AUTO-RATE-1 is described

---

## What happens today

`request_completion` has carried its own retry budget since AUTO-RATE-1:
60 extra attempts, 10 s apart, on a retryable status (429 / 402 / 5xx) or a
`URLError`; on 429 the server's `Retry-After` (header, then `ms`/`s`
hints in the body) replaces the fixed wait, and a hint above 180 s is
raised at once as a quota reset. That is a sound default for a shared
library function. It is also the *only* budget every auto-mode caller
has: architect, Gate 1, coder, Gate 2 validator, story bible, summary
memory all call `request_completion(url, headers, payload, timeout=…)`
and stop there. The one caller that reads the ini is the collect
summarizer (`tools/collect/summarizer.py` ≈ 493–528): `[collect]
error_retries / error_retry_wait_sec / max_retry_after_sec`, defaults
2 / 60 / 180, Pass B only.

So a provider outage in the plan phase costs, per Gate 1 call, up to
60 × 10 s = 10 min inside `request_completion`, then `[gate1]
llm_call_retry_max` (3) × `llm_call_retry_wait_sec` (60 s) outside it,
and the operator cannot shorten, lengthen, or even see the inner budget:
the log says `waiting 10.0s and retrying (attempt 7/60)` with no hint
where the 10 and the 60 come from, and the only ini keys with those
names sit in `[collect]` and change nothing here. Two sessions of the
same operator turning `[collect] error_retry_wait_sec` from 60 to 15 and
back is the field report.

`[loop] timeout_seconds` is the precedent for the shape that is missing:
one `[loop]` key, read the same way by every auto-mode caller
(`architect.py:481`, `gate1_filter.py:520`, `coder.py:492`,
`inner_loop.py:2225`, `story_bible.py:881`, `summary_memory.py:802`),
with a malformed-value warning and the library default as fallback.

## What must change

1. **Three `[loop]` keys, one reader.** `[loop] error_retries`,
   `[loop] error_retry_wait_sec`, `[loop] max_retry_after_sec`, read by
   one helper — `tools/llm_stream.retry_kwargs_from_config(config,
   section="loop")` (or next to `safe_getint` in `tools/config_safe.py`)
   — returning `{"error_retries": int, "error_retry_wait_sec": float,
   "max_retry_after_sec": float}`. Missing key → the `request_completion`
   default (60 / 10.0 / 180.0), so a config without the keys behaves
   byte-for-byte as today. Malformed value → `logger.warning("config
   [loop] error_retries is malformed (%s) — using default 60", …)` and
   the default, the RUN-9 shape. Negative → clamped to 0 / 0.0.

2. **Every auto-mode caller passes them through.** The eight call sites
   listed under **Symbol** add `**self._retry_kwargs` (read once in the
   constructor, next to `_timeout`) or `**retry_kwargs` (module-level
   functions read it where they read `timeout_seconds`). No caller keeps
   a private copy of the defaults; no caller reads `[collect]`.

3. **`[collect]` stays Pass B's.** The summarizer keeps its own three
   keys and defaults (2 / 60 / 180) — Pass B is a ~550-call batch with a
   different failure economy, and the keys are already documented and
   in use. The `[collect]` comment in both inis gains one sentence:
   *"Pass B only; the auto-mode calls (architect, Gate 1, coder, Gate 2,
   story bible, summary memory) use the same three keys under `[loop]`."*
   The RUN-7 reuse of `[collect] error_retry_wait_sec` as the
   validator-unavailable wait (`inner_loop.py` ≈ 2354–2382) is
   unchanged.

4. **Say what is in force.** One INFO line at pipeline start, after the
   config is loaded and before the architect runs:
   `LLM retry budget [loop]: error_retries=60 wait=10.0s retry_after_cap=180s timeout=4800s`
   — the numbers actually resolved (defaults included), so the operator
   reading `run.log` during the next 429 storm sees which knob the
   `attempt 7/60` belongs to. `request_completion`'s own retry lines are
   unchanged.

5. **Document the two budgets once.** `agents.ini` `[loop]` gains the
   three keys, commented like the `[collect]` block (what `error_retries`
   counts, when `Retry-After` wins, what the cap means), plus the line
   *"the outer per-call retries — `[gate1] llm_call_retry_max`,
   `[auto] validator_unavailable_retries` — wrap this budget, they do not
   replace it."* `agents_128k.ini` gets the same keys.

## Acceptance

- [ ] `tests/test_llm_stream*.py`: `retry_kwargs_from_config` on an empty
      config → `{60, 10.0, 180.0}`; on `[loop] error_retries = 1,
      error_retry_wait_sec = 0, max_retry_after_sec = 30` → those values;
      on `error_retries = x` → warning logged, default, the other two keys
      still honoured; on `-5` → 0.
- [ ] One test per caller (parametrised over the eight call sites, a stub
      `request_completion` that records its kwargs): with `[loop]
      error_retries = 1, error_retry_wait_sec = 0` every auto-mode call
      arrives with exactly those kwargs; with no `[loop]` keys the kwargs
      are absent or equal to the defaults.
- [ ] Gate 1 under a stub that raises `HTTPError 429` (no `Retry-After`)
      on every call with `[loop] error_retries = 2, error_retry_wait_sec
      = 0` and `[gate1] llm_call_retry_max = 1, llm_call_retry_wait_sec =
      0`: the stub is called exactly (2 + 1) × (1 + 1) = 6 times and the
      candidate ends `unknown` (RUN-5) — the two budgets nest, neither
      replaces the other.
- [ ] Summarizer tests unchanged: `[collect] error_retries = 5` still
      reaches Pass B's `request_completion`; `[loop] error_retries = 1`
      does not.
- [ ] `run.log` of a stubbed `--auto` run contains exactly one
      `LLM retry budget [loop]:` line with the resolved numbers.
- [ ] `agents.ini` and `agents_128k.ini` (committed version) carry the
      three `[loop]` keys; `[collect]`'s comment says "Pass B only".
- [ ] `python3 -m pytest tests -q --timeout=180 && python3 -m pytest tests_bugfix -q --timeout=180` green (run sequentially).

## Out of scope

- Changing any default. 60 / 10 / 180 stays what a config without the keys
  gets; whether Gate 1 *should* wait ten minutes per call is the operator's
  setting after this ticket, not this ticket's opinion.
- The `Connection timed out` (errno 110) SYN timeout — that is the kernel's
  ~130 s, before `timeout_seconds` applies; not a retry-budget question.
- Per-phase budgets (`[gate1] error_retries` overriding `[loop]`) — add
  only if a live run shows one phase needing a different budget from the
  rest.
- `tools/faq_agent.py`, `tools/actions.py`, `tools/improvement_agent.py`,
  `tools/validator_agent.py`, `tools/search_agent.py` — interactive
  callers, not the auto pipeline.

## Ground rules (same as every round)

- Do not touch `CollectBridge._shrink`.
- Do not run anything against `agents_128k.ini` or any live provider.
- Do not edit `epic-tasks/`.
- One commit, no push.
