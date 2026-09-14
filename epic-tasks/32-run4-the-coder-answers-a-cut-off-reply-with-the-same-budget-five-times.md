# RUN-4 — The coder answers a cut-off reply with the same budget five times

**Status:** open  
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

- [ ] `tests/`: stub returns a cut-off JSON at 3 000, a complete one at
      6 000 → the second call is made with `max_tokens=6000`, the task
      proceeds, no third call.
- [ ] Stub always cut off → budgets 3 000, 6 000, 12 000, 12 000, 12 000
      (cap = 4×), then EXHAUSTED as today.
- [ ] A `NO JSON at all` reply → next attempt at the same budget.
- [ ] Next task on the same `Coder` starts at the learned 6 000.
- [ ] `max_tokens_cap` malformed → warning + default, same style as every
      other key in `Coder.__init__`.
- [ ] `python3 -m pytest tests -q --timeout=180 && python3 -m pytest tests_bugfix -q --timeout=180` green (run sequentially).

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
