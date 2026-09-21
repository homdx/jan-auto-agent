# KC-11 — Each roster model is probed once: the highest thinking variant that answers `hello` is the one the round uses, saved next to the roster

**Status:** queued — after KC-25 (round 64; `KiloClient.providers()` and the `GET /provider` fake come from there) and KC-10 (round 49); runs at `cli.py`'s `intake` (KC-16, landed `1304950` — the intake KC-7 was to own moved there). **Re-verified against the code and a live Kilo 7.6.2 on 2026-09-20:** `AgentSpec.variant` exists (`roster.py`) but `KiloClient.create_session`/`prompt` take no `variant` and the client has no `delete_session` — this ticket adds them (the original text assumed the client already had them; it never did). Live: `GET /provider` lists for **every** openai-compatible model marked `reasoning: true` in `kilo.jsonc` the same `variants: {"low": {"reasoningEffort": "low"}, "medium": …, "high": …}` — no `max`, no `minimal` — so the ladder below runs `high → medium → low → none` in practice; `POST /session` with `model.variant: "high"` is accepted and echoed back in the session's `model`; `DELETE /session/{id}` answers 200.  
**Severity:** MEDIUM  
**File:** `tools/contest/think_probe.py` (new)  
**Symbol:** `probe_model`, `ProbeResult`, `load_probe_cache`, `save_probe_cache`, `VARIANT_LADDER`  
**Round:** 50  
**Size:** M  
**Source:** Kilo's `GET /provider` reports per model `capabilities.reasoning: bool` and `variants: {name: {...}}`; `POST /session/{id}/prompt_async` and `POST /session` accept `variant` (the CLI's `--variant high|max|minimal`). The operator's `kilo.jsonc` marks every `kenary` and `sensenova123` model `reasoning: true` and declares no variants; the server synthesises `low`/`medium`/`high` (`reasoningEffort`) for each of them — so the server's view, not the config file, is what the probe reads. The auto pipeline solved the same problem for its own providers with capability memos (`tools.llm_stream.mark_think_depth_unsupported`, `think_depth_is_supported`); this is the Kilo-side twin, persisted.  
**Depends on:** KC-1, KC-2, KC-16 (`cli.py run` / `intake`), KC-25 (`KiloClient.providers()`, fake `GET /provider`).  
**Also touches:** `tools/contest/kilo_client.py` (`create_session(..., variant=None)` → `model.variant` when set; `delete_session(session)` → `DELETE /session/{id}`), `tools/contest/runner.py` (`create_session(..., variant=spec.variant)`), `tools/contest/roster.py` (`AgentSpec.variant` becomes "resolved" from the cache when empty), `tools/contest/cli.py` (intake step + `run --reprobe` / `--allow-unprobed`), `tests/test_contest_think_probe.py`, `tests/_kilo_fake.py` (`/provider` with `variants`; `POST /session` keeps `model.variant`; a turn may answer differently per the session's variant; `DELETE /session/{id}`), `tests/test_contest_kilo_client.py` (added cases only), `.gitignore` (`contest-probe.json` is local)

---

## What happens today

`AgentSpec.variant` is whatever the roster says, usually empty. A model
that can think harder runs at its default; a model whose `max` variant
the provider silently rejects (HTTP 400, or an empty reply) would fail
the whole first turn of the round if the operator guessed `max` in the
roster.

## What must change

1. **`VARIANT_LADDER`** — the order tried, highest first, filtered by
   what `GET /provider` lists for the model (`providers()["all"]` →
   the provider's `models[<id>]["variants"]`): `["max", "high", "medium",
   "low", "minimal"]`, then **no variant** — on Kilo 7.6.2 that is
   `high, medium, low, none` for every roster model. A model with
   `capabilities.reasoning == false` or an empty `variants` map skips the
   ladder: result `variant=None, reason="no reasoning capability"`, no
   session created.

2. **`probe_model(client_factory, spec, *, directory, timeout=60) -> ProbeResult`**
   — for each rung: a **fresh session** (`title="think-probe/<agent>/<variant>"`,
   rules `*` allow — the prompt cannot touch a file) in a throwaway
   `directory`, created with `create_session(..., variant=<rung>)` —
   the variant lives on the session (`model.variant`, verified live);
   the prompt body is unchanged — then `prompt("say: hello")`, `wait_idle`,
   then `last_assistant_text`. **Success** = idle with `status == "idle"`,
   no `session.error`, non-empty text. First success wins: the session is
   **deleted** (`delete_session`, `DELETE /session/{id}` → 200 live — a
   probe session must not become the round's context) and `ProbeResult(variant=<rung>, elapsed,
   reasoning_tokens=<last assistant tokens.reasoning>, tried=[...])` is
   returned. A rung that fails (400 on prompt, `session.error`, empty
   text, timeout) is recorded in `tried` with its reason and the ladder
   moves down. All rungs failing → `variant=None`, `usable=False` — the
   round's intake (KC-7) refuses that agent and says so; `--allow-unprobed`
   overrides.

3. **The cache** — `contest-probe.json` next to `contest.ini`
   (git-ignored): `{"<provider>/<model>": {"variant": "high", "probed_at": …,
   "kilo_version": …, "tried": [...], "reasoning_tokens": …}}`. Intake
   loads it; a model with a fresh entry (younger than
   `[contest] probe_ttl_days`, default 7, same `kilo_version`) is not
   probed again; `--reprobe` forces. `AgentSpec.variant` empty → the
   cache's value; non-empty → the roster wins and the probe only checks
   that exact variant answers.

4. **Where it is used** — the runner's `create_session` call passes
   `variant=spec.variant`; the client puts it in `model.variant` only
   when it is not `None` (a `None` sends no key — today's body,
   byte-for-byte). The prompt body stays `{"providerID", "modelID"}`:
   whether `prompt_async` also takes a `variant` is **not** verified and
   is not needed while the session carries it. `_print_plan` and the
   round's table row show the variant per agent.

## Acceptance

- [ ] `tests/test_contest_think_probe.py` against the fake: a model with
      `variants: {max, high}` whose fake rejects `max` (400) and answers
      `high` with `hello` → `variant == "high"`, `tried == [("max","http 400")]`,
      two sessions created, the winning one deleted; a model with
      `reasoning: false` → no session, `variant is None`, `usable`; a
      model whose every rung returns empty text → `usable is False`; a
      cache younger than the TTL skips the probe (no `POST /session` in
      the fake's log); `--reprobe` probes anyway; a roster-set
      `variant = low` is checked once and kept.
- [ ] The round's `POST /session` body (fake log) carries the resolved
      variant in `model.variant`; a `None` variant sends no `variant`
      key at all; the `prompt_async` body is unchanged.
- [ ] `tests/test_contest_kilo_client.py` gains `delete_session` (200 →
      `None`; 404 → `KiloHttpError`) and `create_session(variant=)`;
      no existing line removed.
- [ ] `python3 -m pytest tests -n 4 -q --timeout=180 && python3 -m pytest tests_bugfix -n 4 -q --timeout=180` green.

## Out of scope

- Probing tool use (`say: hello` is text only) — the first real turn is
  that test.
- Per-provider `reasoning_effort` payload fields — Kilo maps `variant`
  to them; we do not build provider payloads on this side.

## Ground rules (same as every round)

- Do not touch `CollectBridge._shrink`.
- No test starts a real `kilo` or calls a live provider.
- Do not edit `epic-tasks/`.
- One commit, no push; a test ships with the change and fails without it.
