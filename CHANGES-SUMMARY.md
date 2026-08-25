# Change log — feature/validate-plan (2 commits on top of it)

Commits: `891d6af` (COLLECT-24), `122c604` (docs), `4f46daf` (AUTO-REASONING-1).

## 1. COLLECT-24 — collect artifact wired into `--auto`
See `COLLECT-24-SUMMARY.md` for full detail. Short version: `--collect`
built a structural artifact nothing ever read; `CollectBridge` now feeds it
into the coder's static per-task context AND the pull-model
(`context_request`/`missing_context`) channel, budget-aware with an LLM
shrink fallback, loaded once per run, `use_in_auto` opt-in unchanged.

## 2. AUTO-REASONING-1 — Gemini HTTP 400 on the `reasoning` field

### What you hit
```
HTTP 400 from https://generativelanguage.googleapis.com/v1beta/openai/chat/completions:
{"error":{"code":400,"message":"Invalid JSON payload received. Unknown name
\"reasoning\": Cannot find field.", "status":"INVALID_ARGUMENT", ...}}
```

### Root cause
`build_chat_request()` (`tools/llm_stream.py`) sends an OpenRouter-style
`reasoning: {"effort": "low", "exclude": true}` field whenever a caller
sets `think=False` on an openai-format request — this is how every
`[section] think = false` default (architect/coder/gate1/validator) is
supposed to suppress a reasoning model's hidden chain-of-thought. The
docstring assumed this field is silently ignored by any provider that
doesn't recognise it. That's true for kenari.id/OpenRouter-style
aggregators. It's **false** for Gemini's strict openai-compat endpoint,
which validates the schema and rejects unknown fields with HTTP 400 — a
status `_is_retryable_status` correctly treats as non-retryable (it's not
a transient error in general), so every single call failed closed.

### Fix
`request_completion()` now recognises this specific shape (`HTTP 400` +
payload had a `reasoning` key + error text says "unknown name"/"cannot
find field"/etc mentioning "reasoning") and retries **once, immediately**,
with the `reasoning` key stripped from the payload — no wait, no
`error_retries` budget spent (this is a payload-shape mismatch, not a rate
limit or transient failure). A second 400 after the strip raises normally.
An unrelated 400 (no `reasoning` in the outgoing payload, or an
error message not about `reasoning`) is untouched — still fails immediately
as before.

This fixes it for **every** call site that goes through
`request_completion` (Architect, Coder, Gate1Filter, TaskRewriter,
FaqAgent, summarizer's Pass B, ...) — not just the architect call you hit
it on.

### Files changed
* `tools/llm_stream.py` — the fallback, in `request_completion()`.
* `tests/test_llm_stream_reasoning_field_fallback.py` — new: 6 tests
  (strip-and-retry-once, retry doesn't touch `error_retries` budget,
  second-400-raises, no-reasoning-in-payload untouched, unrelated-400
  untouched, `on_retry` callback fires).

### What this does NOT fix
* If Gemini's model still burns the whole `max_tokens` budget on internal
  reasoning even without the `reasoning` field (no `exclude` mechanism at
  all on this provider), you may still see empty/truncated JSON — that's a
  separate problem (raise `max_tokens`, or pick a non-reasoning Gemini
  model) from the 400 this fix addresses.
* No provider-specific allowlist/denylist was added for the `reasoning`
  field — the fallback is reactive (fixes it after the first 400 per
  call), not proactive per-provider config. If you want it to never even
  try the field for `api_remote` when it's pointed at Gemini, that would
  be a small follow-up (`[api_remote] send_reasoning_field = false`).

## 3. AUTO-REASONING-2 — stop resending a field we already know is rejected

### Why
AUTO-REASONING-1 fixed correctness (no more failing closed on a 400), but
every one of the ~90+ gate1/architect/coder calls in a run still paid for
one guaranteed-to-fail round trip each — send `reasoning` → 400 → strip →
retry — because each call builds its payload from scratch via
`build_chat_request()`, which had no memory of the previous call's 400.

### Fix
`tools/llm_stream.py` now keeps a process-lifetime set of endpoint URLs
that have already rejected the `reasoning` field
(`_REASONING_UNSUPPORTED_URLS`, `mark_reasoning_field_unsupported()`,
`reasoning_field_is_supported()`). `request_completion()`'s existing
AUTO-REASONING-1 fallback marks the URL the moment it hits the 400 (before
even retrying). `build_chat_request()` checks the cache before adding the
field: once any call to a given `{base_url}/chat/completions` has been
marked, every subsequent `build_chat_request()` call to that same URL
— for the rest of the process, across every agent (gate1, architect, coder,
...) — omits `reasoning` outright. First call still pays one retry; every
call after that sends a correct request the first time.

Scoped by exact endpoint URL, not "provider" in the abstract — a different
`base_url` (e.g. `[api_local]` vs `[api_remote]`) is unaffected even if one
of them is Gemini.

### Files changed
* `tools/llm_stream.py` — the cache + `build_chat_request()` check +
  `request_completion()` marking on the AUTO-REASONING-1 fallback path.
* `tests/test_llm_stream_reasoning_field_fallback.py` — 6 new tests
  (cache read/write, end-to-end two-call integration confirming the
  second call's outgoing payload never has `reasoning`, per-URL isolation,
  `think=True`/`ollama` format unaffected) on top of the existing 6.

### Known limitation
The cache is **in-process, in-memory only** — cleared on process restart.
A fresh `--auto`/`--validate-plan` invocation always pays the first-call
retry again. This was a deliberate scope decision (no new config, no disk
state) matching AUTO-REASONING-1's approach; persisting it across runs
would be a separate, larger change if you want it.


## 4. GATE1-CTX-1..4 — deeper grounding for Gate 1 / `--validate-plan`

### Why
Manual false-positive review of a real `plan.json` (18 tasks, `--validate-plan`
run) confirmed Gate 1 already sends real extracted code (not just prose) —
`cited_location` resolution via `block_extractor`, plus the existing AUTO-H2
grounding notes (module docstring, target-file mismatch, one-hop callee,
config-fallback wrapper resolution). That's why the real run already
self-corrected several false positives (e.g. "test file already contains
test_crashing_callback_does_not_propagate"). Four gaps remained:

1. **COLLECT-24's `CollectBridge` was never wired into Gate 1 at all** —
   only the coder path used it. Structural contracts (e.g. a seeded
   "fail-open by design" contract) never reached the presence check.
2. **`CollectModel.test_map`** (which test files already import a module —
   built by `collect` but unused anywhere) never reached Gate 1 either. A
   "no test exists" claim had no counter-evidence when the citation was
   the SOURCE file, not the test file.
3. A truncated `code_block` carried an in-band `"...[truncated]"` marker
   but nothing told the model how to weigh that uncertainty.
4. The system prompt didn't explicitly say "look for existing handling
   before confirming."

### Fix
* `tools/auto/collect_bridge.py` — two new read-only `CollectBridge`
  methods: `contracts_for_symbol(name)` and `tests_covering(file_path)`.
* `tools/auto/gate1_grounding.py` — three new note builders:
  `collect_contract_note()`, `existing_test_coverage_note()`,
  `truncation_safety_note()`. All additive, all "evidence to weigh, never
  a decision by itself" (same non-rejecting philosophy as every existing
  note in this module) — **truncation_safety_note is deliberately neutral
  wording**; an earlier draft told the model to reject whenever a block
  was truncated, which caused a real regression (wrongly rejected a
  legitimate task in `tests/test_gate1_corpus_precision.py`'s
  precision/recall corpus) before being softened.
* `tools/auto/gate1_filter.py` — `Gate1Filter.__init__` accepts
  `collect_bridge=None`; `_build_grounding_notes()` calls all three new
  note builders (fail-open on any exception, same as every existing note);
  `_SYSTEM_PROMPT_CODE` gained one explicit sentence about checking for
  existing handling/coverage first; `filter_candidates()` accepts and
  threads `collect_bridge` through.
* `tools/auto/pipeline.py` (live `--auto`) — passes
  `controller._get_collect_bridge(task_mode)` via `getattr(..., None)`
  (defensive: some tests build a lightweight fake controller without that
  method — must not crash, must degrade to `collect_bridge=None`).
* `tools/auto/plan_validator.py` (`--validate-plan`) — builds a
  `CollectBridge` once via `make_collect_bridge()` (same `[collect]
  use_in_auto`/`use_in_doc` opt-in as `--auto`) and passes it through.

### Files changed
* `tools/auto/collect_bridge.py`, `tools/auto/gate1_grounding.py`,
  `tools/auto/gate1_filter.py`, `tools/auto/pipeline.py`,
  `tools/auto/plan_validator.py`
* `tests/test_gate1_ctx_epic.py` — new, 24 tests covering all four notes
  in isolation, their wiring into `_build_grounding_notes`, the system
  prompt change, and `filter_candidates()` threading `collect_bridge`
  through (including the "default is None, byte-for-byte regression"
  case).
* `tests/test_collect_bridge.py` — +7 tests for `contracts_for_symbol` /
  `tests_covering`.
* `tests/test_collect_bridge_wiring.py` — +2 tests confirming
  `plan_validator.validate_plan()` actually builds and passes the bridge
  (and passes `None` when `use_in_auto=false`).
* `tests/test_cr20_4_plan_wiring.py` — `_noop_filter` test double updated
  to accept `**_kwargs` (needed once `filter_candidates` gained the new
  `collect_bridge` parameter — this is a test-double signature fix, not a
  behavior change).

### Known limitations
* `tests_covering()` is module-level granularity (from `collect`'s own
  `test_map` — "which test files import this module"), not symbol-level —
  it's a hint to look closer, not proof the specific claimed behavior is
  covered.
* `collect_contract_note`/`existing_test_coverage_note` both require
  `[collect] use_in_auto` (or `use_in_doc`) to be `true` and a FRESH
  artifact — same opt-in and same stale-means-absent policy as COLLECT-24.
  With the flag off (today's default), Gate 1's grounding notes are
  byte-for-byte unchanged from before this epic.


## Tests
Full suite green: `python3 -m pytest tests/ -q` — no regressions.
New/changed test files across all 4 changes:
* COLLECT-24: `tests/test_collect_bridge.py` (25), `tests/test_collect_bridge_wiring.py` (10)
* AUTO-REASONING-1/2: `tests/test_llm_stream_reasoning_field_fallback.py` (12)
* GATE1-CTX-1..4: `tests/test_gate1_ctx_epic.py` (24), plus the additions to
  `test_collect_bridge.py`/`test_collect_bridge_wiring.py` above, plus a
  test-double signature fix in `tests/test_cr20_4_plan_wiring.py`.

## 5. AUTO-URL-1 — double-slash 404 on trailing-slash base_url

### What you hit
```
HTTP 404 from https://generativelanguage.googleapis.com/v1beta/openai//chat/completions: Not Found
```
Note the double slash before `chat/completions`. Hit on every `--collect`
Pass B summarizer call (`analyze_logs.py`, `check_improvements.py`, ...),
even though nothing in `agents_128k.ini` had changed since the earlier
successful `--validate-plan` run.

### Root cause
`build_chat_request()`'s openai-format branch built the URL as a bare
`f"{base_url}/chat/completions"` with no trailing-slash normalization. If
`[api_remote] base_url` has a trailing `/` (e.g.
`https://generativelanguage.googleapis.com/v1beta/openai/`), that produces
`.../openai//chat/completions` — a double slash. Gemini's router 404s on
that instead of normalizing it away. The **Ollama branch already handled
this correctly** — `ollama_chat_url()` does `base_url.rstrip("/")` before
building its URL — so this was purely an inconsistency between the two
branches, not a new problem introduced by anything else in this session.

### Fix
`tools/llm_stream.py`'s openai branch now does the same
`base_url.rstrip("/")` normalization as the Ollama branch before
concatenating `/chat/completions`. A trailing slash (single or multiple)
is now harmless regardless of `api_format`; a `base_url` without one is
completely unaffected.

This also matters for AUTO-REASONING-2's per-URL cache
(`mark_reasoning_field_unsupported`/`reasoning_field_is_supported`): before
this fix, a trailing-slash `base_url` and a non-trailing-slash `base_url`
pointing at the same real endpoint would have landed on two DIFFERENT
cache keys, silently halving that cache's effectiveness. Now both forms
normalize to the same URL string.

### Files changed
* `tools/llm_stream.py` — the `rstrip('/')` in the openai branch.
* `tests/test_llm_stream_url_trailing_slash.py` — new: 5 tests (trailing
  slash stripped, no-slash unaffected, multiple trailing slashes, kenari-
  style base_url regression, AUTO-REASONING-2 cache-key consistency
  between slash/no-slash forms).

### Unrelated pre-existing issue noticed along the way (not fixed here)
`tests/test_cr30_validator_sees_prior.py` and
`tests/test_cr31_revalidate_once.py` monkeypatch
`tools.llm_stream.ollama_chat_url`/`request_completion`/`strip_think`
directly on the module object (`import tools.llm_stream as ls; ls.foo =
...`) instead of via pytest's `monkeypatch` fixture, so the patch is never
restored — it leaks into whichever other test happens to run afterward in
the same xdist worker process. Harmless today (nothing else currently
depends on the un-patched values in the same worker), but it's a latent
test-isolation bug worth fixing separately if it starts causing flakiness.
