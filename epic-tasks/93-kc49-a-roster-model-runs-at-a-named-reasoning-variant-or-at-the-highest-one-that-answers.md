# KC-49 — a roster model runs at a named reasoning variant, or at `highest`: the top variant that answers `say: hello`

**Status:** queued — asked by the operator 2026-09-23 after round 86, where every agent (SenseNova included) ran at its provider's default reasoning effort because no variant ever reached Kilo. Split out of KC-11: this is the plumbing and the ladder; KC-11 keeps the cache.
**Severity:** MEDIUM (the strongest setting of the models the round depends on is unreachable from `run`)
**File:** `tools/contest/kilo_client.py`, `tools/contest/backend.py`, `tools/contest/runner.py`, `tools/contest/cli.py`, `tools/contest/variant.py` (new)
**Symbol:** `KiloClient.create_session`, `KiloClient.prompt`, `KiloClient.delete_session` (new), `SessionRef.variant`, `ContestBackend.create_session`, `run_agent`, `agents_from_models`, `resolve_variants` (new), `_check_offer`, `Intake.agents`, `_apply_flags`, `_print_plan`, `pick_variant`, `hello_probe`
**Round:** 93
**Size:** S
**Source:** the code at `1852ee2` and a live Kilo 7.6.2.

- `AgentSpec.variant` exists and `contest.ini` reads `variant =` for a roster
  agent (`roster.py`), but nothing downstream reads it:
  `grep variant tools/contest/runner.py tools/contest/kilo_client.py tools/contest/backend.py`
  is empty. `--models` has no way to name one. `:free` in a model id is part
  of the id, not a variant.
- 7.6.2's OpenAPI (`GET /doc`): `POST /session` takes `model.variant`, and
  `POST /session/{id}/prompt_async` takes a top-level `variant`. Every prompt
  sends `model` afresh, so the variant is sent on both.
- `GET /provider` lists per model `variants: {name: {reasoningEffort}}`:
  `low/medium/high` for every `kenary` and `sensenova123` model on this box;
  `none/low/medium/high/xhigh/max` for `glm-4-7-flash:free`, `kimi-k3`,
  `qwen3-8-max`; `…/max` for `deepseek-v4-*`.
- **A listed variant is not a working one.** The operator found it in the
  Kilo UI and it reproduces through the API: `kenary/glm-4-7-flash:free` at
  `max` → HTTP 400 `APIError` "the model's provider rejected the request.
  check the model id, request fields, and context length" (`isRetryable:
  false`); `xhigh` the same; `high` answers. So "highest" cannot be read off
  the list — it has to be asked.

**Depends on:** KC-25 (`KiloClient.providers`, `roster_on_offer`, the fake's `GET /provider`, landed `215a740`), KC-16 (`intake`, `cmd_run`).
**Also touches:** `tests/_kilo_fake.py` (`DELETE /session/{id}`; `reject_variants` answers a variant with a `session.error`), `tests/test_contest_variant.py` (new), `tests/test_contest_cli.py`, `tests/test_contest_backend.py`, `.smoke_tests/`

---

## What must change

### 1. The variant reaches Kilo

- `KiloClient.create_session(..., variant=None)`: `model.variant` when set;
  the returned `SessionRef` carries it (`SessionRef.variant`, default `None`).
- `KiloClient.prompt`: the session's variant as top-level `variant` when set.
- `None` sends no key anywhere — today's bodies byte for byte.
- `ContestBackend.create_session` gains `variant=None`; `KiloBackend` passes
  it through, `OpenRouterBackend` keeps it on the ref and ignores it (its
  subprocess loop has no such knob).
- `run_agent` passes `variant=spec.variant`.
- `KiloClient.delete_session(session)` — `DELETE /session/{id}`.

### 2. The operator names it

- `--models` item `provider/model@variant`: the variant is not part of the
  agent's name (`m@high,m@low` → `m-var1`, `m-var2`).
- `--variant NAME` — the default for every agent (from `--models` or the
  roster) that names none.
- A roster section's `variant =` already exists and now works.

### 3. Intake makes it real

After KC-25's pair check passes, and only when every other intake check has
passed (a refused round spends no model call):

- a **name** must be in the model's listed variants, else one line:
  `[agent] provider/model: no variant 'turbo' — listed: none, low, …`;
- **`highest`** walks `ladder(listed)` — `VARIANT_RANK = (max, xhigh, high,
  medium, low, minimal, none)` filtered by what the model lists, strongest
  first, then no variant at all — asking `say: hello` in a fresh session per
  rung (throwaway directory, every permission denied, 60 s), deleting each
  session after. The first rung with an idle turn, no `session.error` and a
  non-empty reply wins. One probe per `provider/model`, however many agents
  ask for it. Nothing answering is one failure line naming every rung and its
  reason. `highest` with no offer to read is a failure, not a default.
- stdout: one `variant: provider/model@highest → high (failed: max: …; xhigh: …)`
  line per probed model; the plan's `agents` line shows `model@variant`.
- `cmd_run` runs the resolved roster (`Intake.agents`).

## Out of scope — KC-11

The cache (`contest-probe.json`, `probe_ttl_days`, `--reprobe`,
`--allow-unprobed`), reasoning-token reporting, and probing a variant that was
named explicitly.

## Acceptance

- [ ] `create_session(..., variant="high")` → `POST /session` `model == {"providerID", "id", "variant": "high"}`; every `prompt_async` of that session has `"variant": "high"`; without a variant neither body has the key.
- [ ] `delete_session` sends `DELETE /session/{id}`.
- [ ] `agents_from_models("sensenova123/sensenova-6.8-flash-lite@high,glm-4-7-flash:free@highest,hy3:free")` → variants `high`, `highest`, `None`; names unchanged.
- [ ] `ladder(("none","low","medium","high","xhigh","max")) == ["max","xhigh","high","medium","low","none",None]`; an unranked name is left out.
- [ ] With the fake rejecting `max` and `xhigh` (glm's live payload), `--models glm-4-7-flash:free@highest` runs the round at `high`: three probe sessions (`max`, `xhigh`, `high`) all deleted, the round's session and prompts carry `high`, the plan reads `kenary/glm-4-7-flash:free@high`.
- [ ] `@turbo` is refused at intake with the listed names, and no session — probe or round — is created.
- [ ] `--variant medium` sends `medium` and probes nothing.
- [ ] Live, once, by hand (not a test): `highest` on `kenary/glm-4-7-flash:free` → `high` with `max`/`xhigh` failed; on `sensenova123/sensenova-6.8-flash-lite` → `high`.
- [ ] `tests` and `tests_bugfix` green; `sync_test_tiers --check` clean; `CollectBridge._shrink` byte-identical.
