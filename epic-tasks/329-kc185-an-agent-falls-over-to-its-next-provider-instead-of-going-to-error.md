# KC-185 — an agent falls over to its next provider instead of going to ERROR

**Status:** open
**Severity:** MEDIUM (a free provider's outage ends an agent's round, though the same model is free elsewhere)
**File:** `tools/contest/cli.py` (the `--models` list), `tools/contest/roster.py` (`AgentSpec`), `tools/contest/runner.py` (the turn's error handling), `tools/contest/kilo_client.py` (the prompt's model)
**Symbol:** `agents_from_models`, `AgentSpec`, the provider-error branch of the agent loop (`_is_provider_unavailable`, `_retryable`, `_rejected_request`), `KiloClient.prompt`, `revive_round.py`'s view of `state.json`
**Round:** 329
**Size:** M
**Depends on:** nothing; written against `contest-cpp` at `059675c`
**Also touches:** `tests/` (new files only), `docs/kilo-contest/RUN-THE-KILO-CONTEST.md` (the `--models` syntax), `scripts/revive_round.py` (prints the model an agent is on)

---

## Why

Round 328 (HARDEN-30, 2026-11-02) ran `glm-4-7-flash` through kenari.id. In leg 1, kenari answered "the
model's provider rejected the request" over and over: 8 retries of 60, then `ERROR`. The same model is
free on z.ai (`zai/glm-4.7-flash`, 15/15 in `py_model_test.py`). To move the agent, the operator had to:

1. stop the round (`kill -9` of the runner and its `kilo serve`);
2. run `revive_round.py`;
3. hand-edit `state.json` (`provider_id`/`model_id`/`variant` of that one agent), because `--resume`
   reads the agent's model from `state.json` and not from `--models`;
4. restart the leg with `--resume`.

Each restart costs about 25 minutes of start-up harvests. In leg 2 and leg 3, z.ai itself answered
"The service may be temporarily overloaded" and the agent went to `ERROR` again, this time with
`provider_unavailable after 9 retries`. If kenari had been tried again at that moment, the agent could have
kept going.

---

## What happens today

- One agent is one model. `AgentSpec` (`tools/contest/roster.py:239`) has exactly one `provider_id`
  and one `model_id`. `agents_from_models` (`tools/contest/cli.py:382`) turns each comma item of
  `--models` into one agent. No syntax names a second provider for the same agent.
- A provider that kilo gave up on is final. `_is_provider_unavailable` (`tools/contest/runner.py:952`,
  KC-64) is never retried by the runner. The agent goes straight to `ERROR` with
  `provider_unavailable after N retries` (`:5427`), and the next leg does not take an `ERROR` agent.
- A rejected request or a dropped stream is retried up to `max_error_retries` times (`:5399`),
  always on the same provider. When the budget runs out, the agent goes to `ERROR`.
- The model travels with every prompt. `KiloClient.prompt` (`tools/contest/kilo_client.py:1368`)
  sends `{"providerID", "modelID"}` in each `prompt_async`. So the same kilo session can go on with
  another provider, and the agent's conversation and worktree stay as they are.

---

## What it does after

### A — a model list per agent

A `--models` item (and a profile's `models =`) may name several models joined with `|`:

```
kenary/glm-4-7-flash:free|zai/glm-4.7-flash,sensenova/sensenova-6.8-flash-lite
```

- That is one agent with two models, tried in the written order, plus one more agent.
- The agent's name comes from the first model, as today (`glm-4-7-flash`). A `|` list never adds a
  `-varN` suffix of its own.
- `@variant` belongs to each model on its own: `a/m@high|b/m` gives `high` to the first only.
- An item without `|` behaves exactly as today. Its name, its spec and its `state.json` stay
  byte-identical.

### B — fall over instead of ERROR

When a turn ends on a provider error that today ends the agent, the agent moves to the next model of
its list and the runner sends `RETRY_PROMPT` into the same session. Such an error is one of:

- `provider_unavailable` (KC-64);
- `max_error_retries` used up on a rejected request or a dropped stream;
- `provider_quota`.

Rules:

- The retry budget (`max_error_retries`) starts again for the new model.
- The list is a ring with one pass. After the last model, the agent goes back to the first one only if
  at least one turn has finished since the previous switch; otherwise it goes to `ERROR` as today. The
  `ERROR` text names every model that was tried.
- A context overflow, a stall, a time-up, or a tool or permission error is not a provider error.
  None of them switches the model.
- A switch is logged on one line, `<agent>: provider <from> → <to> — <reason>`, and appended to the
  agent's `turns.jsonl` with `"cause": "failover"`.

### C — the model an agent is on survives a restart

- `state.json` records the agent's whole list and the index it is on.
- `--resume` goes on with the model the agent was on. An agent revived from `ERROR` by
  `revive_round.py` starts again at the first model of its list.
- `revive_round.py` prints the model each agent will resume on.
- A `state.json` written before this change, with one model, loads as a list of one.

### D — the operator sees it

- `status` shows the model in use in the `model` column. When it is not the first model of the list,
  the cell reads `zai/glm-4.7-flash (2/2)`.
- The `SUMMARY.md` row of an agent that switched names both models.
- `RUN-THE-KILO-CONTEST.md` documents the `|` syntax next to `--models`.

---

## Tests

Each fails on the base and passes after the change. They use the fake backend the runner tests already
use, never a live provider.

- A: `agents_from_models("kenary/glm-4-7-flash:free|zai/glm-4.7-flash,sensenova/x")` gives two agents;
  the first is named `glm-4-7-flash` and carries both models in order. A list without `|` gives the
  same specs as today.
- B: the first model ends every turn with `ProviderUnavailable` and the second answers. The agent
  reaches `READY` on the second model, in the same session, and the second model got `RETRY_PROMPT`.
  With one model, the agent ends in `ERROR` as today.
- B: both models are down. The agent ends in `ERROR`, the text names both, and nothing loops.
- B: a context overflow on the first model does not switch the model.
- C: a leg stopped while the agent is on its second model resumes on the second model. An agent in
  `ERROR` revived by `revive_round.py` resumes on the first. An old one-model `state.json` loads.
- D: the `status` row shows `(2/2)` after a switch.

---

## Contest

One commit. The judge's checks are written after the round, from the entries.

## Acceptance

**Layer 1 — offline.** The tests above pass. `.smoke_tests/ .regression_tests/` and `tests_bugfix -n 4`
stay green and unchanged.

**Layer 2 — the operator.** Run a round with `kenary/glm-4-7-flash:free|zai/glm-4.7-flash` while one of the
two is failing. The agent switches models without a restart, and `round.log` shows the switch line.

## Not in scope

Choosing the list automatically (from `py_model_test.py` scores or from kilo's model list). Switching
between different models of one agent, for example glm then qwen: the list is meant for one model
served by several providers, and nothing enforces that. The permission gate's own provider
(`[contest_gate_llm]`). Retrying a provider error inside one turn: that stays with kilo.
