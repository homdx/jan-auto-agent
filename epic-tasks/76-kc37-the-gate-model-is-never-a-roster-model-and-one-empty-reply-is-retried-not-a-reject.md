# KC-37 — the safety gate never runs on a model the round is competing, and one empty reply is retried instead of becoming a `reject`

**Status:** queued, shrunk to §2 — KC-55 (`54de4bf`) took over §1 (the re-ask of an empty reply) and §3 (the plan line); only §2, refusing a gate model the roster runs, is left. found live on 2026-09-21 in round 64, where the gate was `hy3:free` and `hy3` was agent 4 of 8.
**Severity:** MEDIUM (a gate that answers nothing rejects the agent's work, and a gate sharing a model with the roster fails exactly when the round is busiest)
**File:** `tools/contest/policy.py`, `tools/contest/cli.py`, `tools/contest/roster.py`
**Symbol:** `Policy._ask_gate`, `GATE_RETRIES`, `intake`, `_apply_flags`, `ContestConfig.gate_settings`
**Round:** 76
**Size:** S
**Source:** round 64, `contest-out/64/glm-4-7-flash/decisions.jsonl`:

```json
{"layer": "gate-failed", "reply": "reject",
 "reason": "gate unavailable: empty reply",
 "gate_elapsed": 6.647, "gate_model": "hy3:free"}
```

One call, 6.6 s, an empty body, and the agent's `python3 /tmp/fix_parser.py`
was refused. `contest.local.ini` sets `[contest_gate_llm] model = hy3:free`
(`contest.ini` itself still ships the placeholder `some/model`), and the round's
own `--models` line lists `hy3:free` as a competitor. So the gate and an agent
were the same free endpoint, and the gate's turn came while eight sessions were
hammering it — `hy3` the agent reached `turn_timeout_sec` with 344 uncommitted
lines in the same window. Two independent faults meet here: the gate has no
business being a roster model, and a single empty reply — the ordinary failure
mode of a free endpoint under load — is treated as a verdict.
`_ask_gate` (`policy.py:627`) calls `self._completion_fn(..., error_retries=0)`
and turns anything that is not a parsable `allow`/`reject` into
`Decision("reject", "gate-failed", …)` (`policy.py:694-698`). Failing closed is
right for a gate that is *broken*; an empty 200 from a loaded free model is not
a decision, it is a dropped call.
**Depends on:** KC-6 (the policy layers, landed `e8c6ad3`), KC-16 (`intake`, `_apply_flags`, landed `1304950`).
**Also touches:** `tests/test_contest_policy.py`, `tests/test_contest_cli.py`, `contest.ini`


**Seen again, round 86 (run 3, 2026-09-23, base `4634507`):** the gate was
`hy3:free` again, and `hy3:free` was again in `--models`. 12 of 13 gate calls
failed (11 `empty reply`, 1 `RuntimeError`) in 3–5 s each; one parsed. All
13 were `2>/dev/null` reads that should never have reached the gate (KC-28).
A retry (§1) would not have saved these: the endpoint was saturated by the
same round. §2's intake refusal would have.

---

## What must change

### 1. An empty or unparsable reply is retried once

`GATE_RETRIES = 1` — a module constant next to `GATE_TIMEOUT`, so a test can
set it to `0` and get today's path.

In `_ask_gate`, when `_extract_verdict` yields neither `allow` nor `reject`
(this includes the empty body) **and** at least one retry is left, the same
request is sent again after a short fixed backoff (`GATE_RETRY_WAIT = 2.0`,
patched in tests through the injected clock — never a real `sleep` in a test).
A transport exception retries on the same budget. The verdict of the last
attempt stands; when it is still not parsable the `gate-failed` reject is
exactly today's, with the reason naming the attempts:
`gate unavailable: empty reply (2 attempts)`.

- `gate_elapsed` is the **total** across attempts, so a reader sees the true
  cost; `gate_raw` is the last reply.
- A retry does **not** spend a second `gate_max_calls_per_session` unit. The
  budget counts asks the agent made, not calls the transport lost; `budget <= 0`
  is still checked once, up front, unchanged.
- The `decisions.jsonl` record gains `"gate_attempts": int` (absent when `1`).

### 2. Intake refuses a gate model that is competing in the round

In `intake`, after the roster and `--models` are resolved and before the first
session is opened: if the gate's configured `model` matches any agent's
`model_id`, the round stops with the existing intake failure path and a message
that names both and says what to do —

```
gate model hy3:free is also agent hy3 (and hy3-var2) in this round
  the gate must be a second, independent model: set [contest_gate_llm] model
  in contest.local.ini to something the roster does not run, or pass --no-gate
```

The comparison is on **`model_id`**, not on the agent name, and it reports
*every* matching agent: with KC-34 §7 a round can hold `hy3-var1` and `hy3-var2`
on one model, and the name tells you nothing about which endpoint the gate
shares. The provider prefix is part of the match — `kenary/hy3:free` and
`openrouter/hy3:free` are different models and do not collide.

`--no-gate` skips the check entirely (there is no gate), and so does a gate
whose model is still the placeholder — that case already has its own intake
complaint and must not grow a second, confusing one.

**Seen again in round 74 (addendum 2026-09-24, base `3017885`, KC-55 already
landed).** The gate was `hy3:free` on `kenary`, `hy3` was one of the ten agents,
and five of the agents shared that endpoint. Of 235 permission decisions, 231
were mechanical allows and 1 a mechanical reject. **All 3** that reached the
gate came back `gate-failed` → `reject`, each after 11-15 s with
`gate unavailable: empty reply (2 attempts)`. KC-55's re-ask ran, and it could
not help, because the second ask goes to the same busy free model. The round
therefore had no reviewer: anything the mechanical layer did not allow was
refused. That included `cd /tmp && python3 - <<PY … PY` (a stub check,
`sensenova-6-8-flash-lite-var1`) and a heredoc test edit that wrote
`/tmp/dbg.txt` (`-var2`); the second is KC-53's case of a scratch path the
prompt never named. KC-55's shared-endpoint check only warns, and a warning is
not enough when the gate is also a competitor, so this section is the one to
land next.

### 3. The plan line says which model guards the round

`_print_plan`'s `("gate", "on"|"off")` row becomes `("gate", "hy3:free")` /
`("gate", "off")`, so the operator reads the gate model before the round starts
rather than out of `decisions.jsonl` afterwards. Never the key, never the
base url — the model id only.

## Acceptance

- [ ] `tests/test_contest_policy.py`, with an injected `_completion_fn` and clock:
  - an empty reply then a parsable `allow` → one `once`/`gate` decision, the
    fn called twice, `gate_elapsed` the sum, `gate_attempts == 2`, no sleep of
    real wall time;
  - two empty replies → `reject`/`gate-failed`, reason
    `gate unavailable: empty reply (2 attempts)`;
  - a transport exception then a parsable `reject` → `reject`/`gate` (a real
    verdict, not `gate-failed`);
  - two exceptions → today's `gate unavailable: <ExcName>` reject;
  - `GATE_RETRIES = 0` → every existing gate test passes unchanged, fn called
    once;
  - the retry does not consume budget: with `gate_budget_left == 1` and one
    retry, the decision is the gate's, not `budget`;
  - a parsable reply on the first call → fn called exactly once (no
    speculative retry).
- [ ] `tests/test_contest_cli.py`:
  - `--models hy3:free,x:free` with the gate on `hy3:free` → intake fails,
    non-zero exit, the message naming the model and the agent, no session
    opened;
  - the same with `--no-gate` → the round runs;
  - the gate on a model the roster does not run → the round runs and the plan
    line reads `gate  hy3:free`;
  - KC-34 §7 shape: `--models hy3:free,hy3:free` with the gate on `hy3:free`
    → the message names **both** `hy3-var1` and `hy3-var2`;
  - `kenary/hy3:free` in the roster with the gate on `openrouter/hy3:free`
    → no collision, the round runs.
- [ ] Every existing test unmodified and green.
- [ ] `python3 -m pytest tests -n 4 -q --timeout=180` then
      `python3 -m pytest tests_bugfix -n 4 -q --timeout=180`, sequentially, both green.

## Out of scope

- The gate prompt, `GATE_SYSTEM_PROMPT`, and what the gate decides when it
  *does* answer.
- `GATE_TIMEOUT` (60 s) — the round-64 call came back in 6.6 s; the problem was
  the body, not the clock.
- Retrying a gate that answered a clean `reject`. A verdict is a verdict.
- Shipping a real gate model in `contest.ini`: the placeholder `some/model`
  and the operator's `contest.local.ini` stay as they are, and no key is ever
  read, logged or printed by this change.
- `tools/auto/llm_profile.py` and the shared `build_chat_request`.

## Self-check before `append_task.py`

- [ ] `python3 --version` on the judge is **3.10.12**.
- [ ] Exactly **one** commit; only the files listed in `**File:**` and the test files touched.
- [ ] `git diff --stat <base>..HEAD` names only `tools/contest/policy.py`,
      `tools/contest/cli.py`, `tools/contest/roster.py`, `contest.ini`,
      `tests/test_contest_policy.py`, `tests/test_contest_cli.py`
      (plus `.smoke_tests/` links). Never `epic-tasks/`, never `contest.local.ini`.
- [ ] `python3 scripts/sync_test_tiers.py --check` clean.
- [ ] No test sleeps real wall time for a retry.
- [ ] New tests red without the change.
- [ ] `CollectBridge._shrink` byte-identical.
