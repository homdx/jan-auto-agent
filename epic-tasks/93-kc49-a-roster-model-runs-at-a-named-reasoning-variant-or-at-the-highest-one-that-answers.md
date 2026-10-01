# KC-49 — a roster model runs at its highest reasoning variant that answers `say: hello` unless told otherwise; a named variant, or none, on request

**Status:** landed `620518e` (2026-09-23, by hand on branch `kc49`, not a round). Asked by the operator 2026-09-23 after round 86, where every agent (SenseNova included) ran at its provider's default reasoning effort because no variant ever reached Kilo. Split out of KC-11: this is the plumbing and the ladder; KC-11 keeps the cache.
**Severity:** MEDIUM (the strongest setting of the models the round depends on is unreachable from `run`)
**File:** `tools/contest/kilo_client.py`, `tools/contest/backend.py`, `tools/contest/runner.py`, `tools/contest/cli.py`, `tools/contest/roster.py`, `tools/contest/variant.py` (new), `contest.ini`
**Symbol:** `KiloClient.create_session`, `KiloClient.prompt`, `KiloClient.delete_session` (new), `SessionRef.variant`, `ContestBackend.create_session`, `run_agent`, `agents_from_models`, `resolve_variants` (new), `_check_offer`, `ContestConfig.variant`, `Intake.agents`, `_apply_flags`, `_print_plan`, `pick_variant`, `hello_probe`
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
**Also touches:** `tests/_kilo_fake.py` (`DELETE /session/{id}`; `reject_variants` answers a variant with a `session.error`), `tests/test_contest_variant.py` (new), `tests/test_contest_cli.py`, `tests/test_contest_backend.py`, `tests/test_contest_roster.py`, `.smoke_tests/`

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

### 2. The highest by default; the operator can name another

- **Default: `highest`.** `[contest] variant` (new key, `contest.ini` ships
  `variant = highest`; absent or empty is `highest`) is the variant of every
  agent that names none. A round with no flag runs every model at the top
  variant that answers.
- `--variant NAME` overrides the key for this round.
- An agent's own variant overrides both: a `--models` item
  `provider/model@variant` (not part of the agent's name: `m@high,m@low` →
  `m-var1`, `m-var2`), or a roster section's `variant =` (already read, now
  used).
- `default` — as the key, the flag or `@default` — sends no variant: the
  provider's own default, today's behaviour.

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
  reason. A model that lists no variants is not asked — `highest` of nothing
  is no variant, so a roster of such models costs no probe. With no offer to
  read (no server started, `GET /provider` failed, `backend = openrouter`)
  `highest` becomes no variant: it is the default, and the round reports its
  own server failure.
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
- [ ] No flag at all: the glm case above runs at `high` — `highest` is the default.
- [ ] `--variant default` (and `@default`) sends no `variant` key and probes nothing.
- [ ] `[contest] variant` parses: absent or empty → `highest`; `high`, `default` as written; `--variant` overrides it; an agent's own variant overrides both.
- [ ] `highest` on a model that lists no variants: no probe, no variant sent — every existing fake round (its models list none) is unchanged.
- [ ] Every test runs offline: the fake server only, no provider, no internet.
- [ ] `tests` and `tests_bugfix` green; `sync_test_tiers --check` clean; `CollectBridge._shrink` byte-identical.
