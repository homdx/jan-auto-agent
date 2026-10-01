# RUN-4 — The coder answers a cut-off reply with the same budget five times

**Status:** landed — `ade28e6` on top of `26b5d7f`. Competition (9 entrants in `run4/` — DeepSeek and the two Sonnet entries had not arrived; the round can reopen for them as RUN-6/RUN-3 did; reviewer probe = the 5 ACs driven through the real `InnerLoop` with a stubbed provider, plus: a task exhausted at every rung must not teach the next task, an explicit cap, a cap below `max_tokens`, the *raised to N* line must be in the prompt of the attempt that goes out at N, the existing sentence at the cap, `max_tokens` on the coder decision event, `_parse_response` keeps its two-tuple — 13 probes; then the entrant's own tests, `tests`, `tests_bugfix`, `sync_test_tiers --check`): **Sensenova-68-var1 — winner, base of the ideal** (13/13, both suites green, 22 tests, smoke tier synced; task-id cursor so a retry of the same task keeps the climbed budget and a new task starts at `max(config, learned)`; the parser only emits a stable head and `generate()` swaps the advice clause, so `_parse_response` stays pure; `CoderResult.max_tokens`/`budget_raised` → `max_tokens`/`budget_raised` params on the coder decision event → `cod esc` column in `trace_round_snapshot.py`; cap below `max_tokens` clamped up with a warning); Sensenova-68-var2 — 13/13 on the probe but 4 failures in `tests`: the raise lives inside `_parse_response` (so `tests/test_theme_validator.py`'s `Coder.__new__` parser test breaks) and the fail-open line pins were not updated; its `BUDGET_RAISED` decision event and `raises@top` snapshot column are a fine alternative shape — taken: the *a cap ≤ 0 is a typo* reading (as a note); Laguna-S-2-1 — 12/13 (budget only on a private `budget_raised` event, not the decision event), green, 12 tests + smoke tier; `reset_task_budget()` from `run_task` re-climbs the ladder on every outer round — the opposite of what AUTO-T30's 8 rounds need; Sensenova-67-var1 — 12/13 (budget only on `llm_request`), green, 19 tests (in `tests_bugfix`); learned only after a successful *write*, not a parse — taken: the creative-mode head (`creative coder: response hit the token budget`) climbs the same ladder, cap-below-floor warning; HY3 — 11/13 (a cap below `max_tokens` *shrinks* the second attempt to it; trace on `llm_request` only), green, 13 tests + smoke; task-id rebase, creative climb, `getattr` guards for `__new__` parsers — taken: the creative-mode climb; agnes-2-5 — 10/13 (`_parse_response` becomes a 3-tuple; an exhausted task leaks its 12 000 into the next task; a NO-JSON reply mid-ladder drops back to the floor), 3 failures in `tests` (no `.smoke_tests` mirror → pre-commit would refuse), 15 tests; Step3-7-Flash — 8/13: the *raised to* note is attached one attempt late (to the failure at the raised budget, not the one that caused the raise), at the cap it still says *raised*, the ladder leaks across tasks; green, 5 tests in `test_auto_c2.py`; MistralMedium-3.5 — 9/13, 15 failures in `tests` + 6 in `tests_bugfix` (3-tuple parser, no pin updates); a truncation raises the *learned* tier before anything parsed; Nvidia-nemotron-3-ultra — 8/13, 52 failures in `tests` (3-tuple parser), no tests of its own, a stray `.kilo/kilo.jsonc`, ladder state on `InnerLoop` never reset and computed from the config floor so it stalls at the learned tier — disqualified; Sensenova6-7-var2 — a RUN-3 patch (`executor.py`/`utils.py`/`epic-tasks/`), not a RUN-4 entrant. Late entrant Sensenova6-7-var3 (filed in `run3/`, reviewed against `26b5d7f` after `ade28e6` had landed) — 9/13, 15 failures in `tests` + 6 in `tests_bugfix`: the ladder lives inside `_parse_response` (3-tuple, `self._…` state read from a `Coder.__new__` parser instance → `AttributeError` in `test_theme_validator`/`test_cr1_coder_prose`/`tests_bugfix`), no pin updates; nothing to fold in. The four Sonnet patches in `run4/` are RUN-3 entries and were scored there. Late entrant Sonnet-5 (`run4/run4-truncation-ladder.zip`, whole files, reviewed against `26b5d7f` after `ade28e6`) — 11/13, both suites green, 23 tests in `tests_bugfix/test_coder_truncation_ladder.py`, smoke tier synced: the ladder is a 3-tuple `_parse_response` (`is_truncated` flag) — the only 3-tuple entry that also updated every caller and line pin, so nothing breaks — and the raise is reported only on a private `coder_decision` event, not on `CoderResult`/the coder decision event (the snapshot's `cod esc` column would stay empty); learned tier only from a parsed reply, task-id boundary, cap clamps silently, creative mode explicitly excluded; nothing to fold in. Competition closed; DeepSeek did not enter.  
**Severity:** HIGH  
**File:** `tools/auto/coder.py`  
**Symbol:** `Coder._parse_response` (≈ 1380–1420, the `truncated` branch) and the retry path in `Coder.run` / `InnerLoop.run_task` that re-invokes it  
**Round:** 32  
**Size:** S  
**Source:** live runs on `../testtext` and `../testtext6` — the single most frequent coder failure string in both trees is `LLM response was cut off before the JSON was complete` (testtext 807 occurrences in feedback, testtext6 572, both counting the re-quoted feedback; 6 of the 29 BLOCKED tasks ended on it at attempt 5). `[coder] max_tokens = 3000` in the user's config; RUN-2 "Out of scope" called this a config problem — the traces say the code never gives the config a chance  
**Depends on:** RUN-3 (`31-run3-…`) — independent in code, but land RUN-3 first so the exec-side blocked tasks are separated from the coder-side ones in the next snapshot  
**Also touches:** `tools/auto/inner_loop.py` (attempt loop), `agents.ini` (one new key, documented), `tests/test_coder*.py`  

---

## What happens today

The coder is called with a fixed `max_tokens` (`[coder] max_tokens`, or
`max_tokens_<mode>`). When the reply stops mid-JSON, `_parse_response`
detects it and writes a feedback line telling the model to *"keep the
response minimal … or raise [coder] max_tokens"*. The next attempt sends
the same budget. A whole-file rewrite of a 7 kB test module needs ~2 500–
3 500 output tokens; at 3 000 the model is cut at the same place five
times, the task is EXHAUSTED, the outer loop opens the next round with the
same budget, and after 10 rounds the task is BLOCKED. AUTO-T30 in testtext6
spent 8 rounds this way.

The model is being blamed for a limit it cannot see. Gate 1 already solved
the same problem for itself (GATE1-LEARN-1/2: a re-ask ladder that raises
the budget on a truncated reply and learns the tier that worked). The coder
has no ladder.

## What must change

1. **A truncated reply raises the budget for the next attempt.** When
   `_parse_response` reports the `truncated` case, the next attempt of the
   *same task* is sent with `max_tokens × 2`, capped by
   `[coder] max_tokens_cap` (new key; default = 4 × `max_tokens`, so a
   3 000 config climbs 6 000 → 12 000 and stops). The cap is the operator's
   protection against a runaway provider bill; document it next to
   `max_tokens` in `agents.ini`.

2. **Only truncation climbs.** The `NO JSON at all` and `JSON decode failed`
   cases keep today's budget — more tokens do not fix prose or a malformed
   object (the same reasoning GATE1-LEARN-2 applied to empty replies).

3. **The learned tier survives the task.** Keep the last budget that
   produced a parseable reply on the `Coder` instance (`_learned_max_tokens`)
   and start the next task from `max(config, learned)`, the way Gate 1's
   `_record_parseable_budget` does. Reset on a new run; do not persist to
   disk.

4. **Feedback text.** When the budget was raised, say so in the feedback
   line (`output budget raised to 6000 tokens for this attempt`) so the
   coder stops trying to shorten a file that must be complete. Keep the
   existing sentence for the case where the cap is already reached — that
   is the real "shorten it" case.

5. **Trace it.** Emit the raised budget as a param on the coder decision
   event (`max_tokens=6000`) so `scripts/trace_round_snapshot.py` can count
   how often the ladder was needed.

## Acceptance

- [x] `tests/`: stub returns a cut-off JSON at 3 000, a complete one at
      6 000 → the second call is made with `max_tokens=6000`, the task
      proceeds, no third call.
- [x] Stub always cut off → budgets 3 000, 6 000, 12 000, 12 000, 12 000
      (cap = 4×), then EXHAUSTED as today.
- [x] A `NO JSON at all` reply → next attempt at the same budget.
- [x] Next task on the same `Coder` starts at the learned 6 000.
- [x] `max_tokens_cap` malformed → warning + default, same style as every
      other key in `Coder.__init__`.
- [x] `python3 -m pytest tests -n 4 -q --timeout=180 && python3 -m pytest tests_bugfix -n 4 -q --timeout=180` green (run sequentially).

## Out of scope

- A diff/patch output mode for the coder (would cut output size at the
  root; separate design ticket).
- The `[SAFETY] LLM tried to write '<path>' which is not in target_files`
  line (RUN-1 already demoted it to a warning).

## Ground rules (same as every round)

- Do not touch `CollectBridge._shrink`.
- Do not run anything against `agents_128k.ini` or any live provider.
- Do not edit `epic-tasks/`.
- One commit, no push.
