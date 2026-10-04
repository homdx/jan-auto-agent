# 152 — the overflow reading learns xAI's wording and the images / TGI cases; a small window with no declared limit can be remembered; the repeat guard compares one unit

**Status:** landed
**Severity:** MEDIUM
**File:** tools/contest/runner.py
**Symbol:** _SIZE_REFUSAL_RE, _NOT_SIZE_RE, _REQUEST_FAULT_RE, _CONTEXT_WALL_RE, _SIZE_REFUSAL_STATUSES, run_agent._overflow_of
**Round:** 152
**Size:** S
**Also touches:** tools/contest/context_memory.py (`size_of`), tests/test_contest_overflow_wording.py, tests/test_contest_context_memory.py

**Depends on:** tickets 147–149 (merged on arena at 6e5daf8). Independent of 151 (`backend.py`, `cli.py`); either order.

From the same three reviews as 151; each "False" below was produced by calling `_is_overflow` on 6e5daf8 (status 400).

---

## What to change

1. **xAI wording is not an overflow.** "This model's maximum prompt length is 131072 but the request contains 150000 tokens" matches nothing, so it is read only as an "inferred" reading at ≥ 60 % fill, never written to the memory, and every round hits the wall again. Add `maximum\s+(prompt|context|input)\s+(length|size)` (with a size noun / number) to `_SIZE_REFUSAL_RE`.
2. **The images veto.** "Your prompt with 3 images exceeds the context window" is `False`: `\bimages?\b` in `_NOT_SIZE_RE` vetoes it although it names the window. Let `_CONTEXT_WALL_RE` override the `images?`, `token budget`, `budget exceeded` and `allowance` vetoes, as it already overrides the output-cap veto. A message with the word but no size noun near "exceed" / "too long" stays vetoed.
3. **TGI.** "Input validation error: `inputs` tokens + `max_new_tokens` must be <= 4096" is `False` at 400 and 422, but the `_REQUEST_FAULT_RE` comment says it is read. Either add the pattern (`tokens … must be <= N`) or correct the comment; decide, and test the decision.
4. **422 / 500.** `_SIZE_REFUSAL_STATUSES = (400, 413)` is by design. Document it in the comment and pin it with a test: "prompt too long" at 422 and 500 is not an overflow.
5. **Repeat-guard units.** `_overflow_of` appends `last_ok` to `overflows_seen` but compares `requested = last_ok + grew`. Store the refused request size (or compare `last_ok` with `last_ok`) and fix the docstring so code and text agree.
6. **Small window, no declared limit.** `size_of` lowers the floor to `min(floor, share × D)` only when *declared* is known (`context_memory.py:338`). Apply the same with `context_limit_fallback` when *declared* is `None`, in `size_of` and in the matching floor in `_overflow_of`. With neither, a window under `context_min_window` stays unremembered — say so in the docstring.

## Tests

Each new test fails on 6e5daf8; every negative case from tickets 147 and 148 stays negative.

1. The xAI message at 400 is a worded overflow, is written to the memory, and a second overflow far below it is still the repeat guard's.
2. "3 images exceeds the context window" at 400 → overflow; "image attachment too large" and "token budget exceeded, retry later" stay not-overflow.
3. The TGI message: the decided outcome at 400 and 422.
4. "prompt too long" at 422 and at 500 → not an overflow.
5. First overflow with `last_ok` 98 000, then one with `last_ok` just below and a request well above: the guard's verdict follows the one unit.
6. `declared=None`, `fallback=32 768`, a tight `last_ok` 28 000 → remembered; `declared=None`, no fallback → not remembered.
7. Tests read the share from `context_memory.full_refusal_percent(config)` (or `DEFAULT_FULL_REFUSAL_PERCENT / 100.0` where no config exists), not `runner.FULL_REFUSAL_SHARE` — lines 60, 194, 216, 236, 600 of `tests/test_contest_overflow_wording.py` (spec 148 line 138).

## Acceptance

```bash
python3 scripts/sync_test_tiers.py --check
```

```bash
python3 -m pytest tests -n 8 -q
```

```bash
python3 -m pytest tests_bugfix -n 8 -q
```

## Landed

Round 152 (run on the second machine, base fc517bd = 3416cbe + this ticket's status): winner sensenova-6-7-flash-lite-var2 as-is, ab8897e on arena over 151. TGI decided as an overflow at 400 (`tokens … must be <= / < / at most / no more than N`), never at 422. Bench `contest-bench/152/acceptance_152.py`, 33 cases.
