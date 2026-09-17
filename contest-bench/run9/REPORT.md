# RUN-9 bench report — 20 submissions, 18 unique, 45 scenarios, 211 checks

Ticket: `epic-tasks/37-run9-an-empty-presence-reply-is-not-a-garbled-verdict.md`
(base `da1e9b3`). Run on 2026-09-17. Raw data: `results.json`; table and every
miss with what the entry produced instead: `RESULTS.md`; what each scenario
feeds and expects: `SCENARIOS.md`.

## Inputs

20 files → 18 unique entries (`entrants.json`): `Deepseek-v-4-1-var1.patch` ≡
`SenSenova-6-7-var1.patch` (same md5), `sonet-4-6-var1-run9-changes.zip` ≡
`sonet-5-var1-run9-changes.zip` (same md5). `SenSenova-6-8-var2.patch` is a
6-commit series whose first five are the `tickets` history `9fcaba0..777535b`;
only 6/6 is the entry (based on `777535b`). `GLM4-7-Flash.patch` is a
3-commit series (ground rule: one commit).

## Rounds of data

1. 23 scenarios straight from the ticket's Acceptance list (groups A–J, S).
2. After reading the top-4 code: Ollama end-to-end, `usage` inline on the
   finish chunk, HTTP 400 in OpenAI wording, an unrelated 400 that must *not*
   strip `stream_options`, `think` via `[gate1_llm]` profile, a legitimate cap
   use, transport→garbled, `elapsed` measured (K/E3/E4/H2/C3).
3. Learned budget reused by the transport retry, trace events per HTTP call,
   `elapsed` per call, a 404 in the middle of a transport retry,
   `stream_options` only when `stream=True` (L/K8/I5).

Base `da1e9b3` scores 73/211 — those are the regression guards.

## Table

| # | entry | score | note |
|---|---|---|---|
| 1 | **sn68-v3** | 210/211 + bonus | only entry counting the OpenRouter `delta.reasoning` key (K4); no nudge on an exhausted re-ask (§4 reading); +72 tests |
| 1 | ds41-v3 | 210/211 | nudge on exhausted re-ask (AC-3 reading) |
| 1 | sn68-v1 ≈ sn68-v2 | 210/211 | v2 is v1 rebased on `777535b` |
| 5 | ds41-v1 (= sn67-v1), ds41-v2 | 209 | new counters not in `format_gate1_split`; `on_meta=` callback API |
| 7 | aria | 208 | no new tests at all; also edits README / analyze_logs.py / STATUS.md |
| 7 | sn67-v2 | 208 | counters not in the split line |
| 9 | laguna | 207 | UNKNOWN wording keeps the generic "Expecting value" text |
| 9 | sonet5-v2 | 207 | no sleep between transport retries; counts nothink-ignored per candidate |
| 11 | sonet5-v3 | 202 | counters/wording |
| 12 | hy3 | 199 | **HTTP 404 during a transport retry raises out of `filter()`**; `finish_reason` lost when a usage chunk follows |
| 13 | sonet46-v1 (= sonet5-v1) | 196 | **404 during a transport retry → REJECTED** (technical failure drops the candidate — the RUN-5 regression); key not in agents.ini; no smoke mirror |
| 14 | stepfun | 193 | no `stream_options` at all; 0 new tests |
| 15 | ling3 | 176 | no no-think rung; `presence_empty_transport` counted only when the candidate ends unknown |
| 16 | agness | 155 | **`request_completion` now returns a tuple** — every other caller (Coder, Architect, …) breaks; no tests; key not in agents.ini |
| 16 | glm47 / glm47-flash | 155 / 154 | same tuple return; meta never sees `finish_reason` / `reasoning_content` |

The only check the top-4 miss is `K8.soft` (a 404 inside the transport retry
goes straight to UNKNOWN instead of through the outer `llm_call_retry` loop) —
a taste call, not the ticket's letter. Without it they are tied on the ticket.

## Nominations for part 2 (the ideal patch)

* **Skeleton: `sn68-v3`.** Best score, the one bonus, `_classify_empty_reply`
  as a pure module-level function, config/warning conventions of the project,
  the largest test file.
* **Continuation / donor: `ds41-v3`** (independent implementation of the same
  `request_completion_ex` shape — the cross-check for every disputed spot),
  plus `laguna` / `ds41-v1` for one detail: a failure inside the transport
  retry re-enters the outer retry loop and the candidate gets another chance.

## Decisions the ticket leaves open (taken for the ideal patch)

1. **Nudge on an exhausted re-ask** — §4 says "only when the previous reply
   was non-empty", AC-3 says "with the nudge". Top entries split 50/50. Taken:
   §4 — a model that said nothing cannot be told its previous reply was not
   valid JSON.
2. **`presence_nothink_ignored`** — AC-5 says `== 1` for 5 candidates (per
   `(url, model)`); `sonet5-v2` / `ling3` / `sonet46-v1` count 5 (per candidate).
   Taken: the AC's letter, 1 per provider per run.
3. **Classification line per empty reply** — the ticket says "once per empty
   reply"; only 4 entries log it for the retries too. Taken: every empty reply,
   retries included (`A1.exactly one classification line per empty reply`).

## Added beyond the ticket (data already in `scenarios.py`)

* K8: a technical failure inside the transport retry must never become a
  verdict and never raise — `hy3` and `sonet46-v1` showed both failure modes.
* K4 (bonus): `delta.reasoning` (OpenRouter / kenari-style gateways) counted as
  reasoning characters.
* I5: `stream_options` only with `stream=True` — OpenAI answers 400 otherwise.
* L1: the transport retry re-issues the request at the *learned* budget, not
  the configured one.
* L3: one `llm_request` / `llm_response` trace event per HTTP call, retries included.
