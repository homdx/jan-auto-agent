# KC-11 — Each roster model is probed once: the highest thinking variant that answers `hello` is the one the round uses, saved next to the roster

**Status:** queued — after KC-10 (round 49); runs at intake (KC-7). Written against `f069f07`.  
**Severity:** MEDIUM  
**File:** `tools/contest/think_probe.py` (new)  
**Symbol:** `probe_model`, `ProbeResult`, `load_probe_cache`, `save_probe_cache`, `VARIANT_LADDER`  
**Round:** 50  
**Size:** M  
**Source:** Kilo's `GET /provider` reports per model `capabilities.reasoning: bool` and `variants: {name: {...}}`; `POST /session/{id}/prompt_async` and `POST /session` accept `variant` (the CLI's `--variant high|max|minimal`). The operator's `kilo.jsonc` marks every `kenary` model `reasoning: true` and declares no variants — so the server's view, not the config file, is what the probe reads. The auto pipeline solved the same problem for its own providers with capability memos (`tools.llm_stream.mark_think_depth_unsupported`, `think_depth_is_supported`); this is the Kilo-side twin, persisted.  
**Depends on:** KC-1, KC-2; wired in by KC-7's intake.  
**Also touches:** `tools/contest/roster.py` (`AgentSpec.variant` becomes "resolved" from the cache when empty), `tools/contest/cli.py` (intake step + `--reprobe`), `tests/test_contest_think_probe.py`, `tests/_kilo_fake.py` (`/provider` with variants; a turn may answer differently per `variant`), `.gitignore` (`contest-probe.json` is local)

---

## What happens today

`AgentSpec.variant` is whatever the roster says, usually empty. A model
that can think harder runs at its default; a model whose `max` variant
the provider silently rejects (HTTP 400, or an empty reply) would fail
the whole first turn of the round if the operator guessed `max` in the
roster.

## What must change

1. **`VARIANT_LADDER`** — the order tried, highest first, filtered by
   what `GET /provider` lists for the model: `["max", "high", "medium",
   "low", "minimal"]`, then **no variant**. A model with
   `capabilities.reasoning == false` or an empty `variants` map skips the
   ladder: result `variant=None, reason="no reasoning capability"`, no
   session created.

2. **`probe_model(client_factory, spec, *, directory, timeout=60) -> ProbeResult`**
   — for each rung: a **fresh session** (`title="think-probe/<agent>/<variant>"`,
   rules `*` allow — the prompt cannot touch a file) in a throwaway
   `directory`, `prompt("say: hello", variant=<rung>)`, `wait_idle`,
   then `last_assistant_text`. **Success** = idle with `status == "idle"`,
   no `session.error`, non-empty text. First success wins: the session is
   **deleted** (`DELETE /session/{id}` — a probe session must not become
   the round's context) and `ProbeResult(variant=<rung>, elapsed,
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

4. **Where it is used** — KC-6's `create_session` and `prompt` pass
   `variant=spec.variant` when it is not `None` (the client already
   accepts it in `model.variant` on `POST /session` and `variant` on the
   prompt body per the capture). KC-7's `SUMMARY.md` shows the variant
   per agent.

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
- [ ] The round's `create_session`/`prompt` bodies (fake log) carry the
      resolved variant; a `None` variant sends no `variant` key at all.
- [ ] `python3 -m pytest tests -q --timeout=180 && python3 -m pytest tests_bugfix -q --timeout=180` green.

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
