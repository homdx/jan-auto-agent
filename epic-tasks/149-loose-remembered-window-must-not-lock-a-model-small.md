# 149 — a loose remembered window must not override Kilo's declared one (KC-73 regression)

**Status:** open
**Severity:** HIGH
**File:** tools/contest/context_memory.py
**Symbol:** size_of, _pick, smallest_size, remembered
**Round:** 149
**Size:** M
**Also touches:** tools/contest/runner.py (`_context_budget`), tools/contest/cli.py (`_with_remembered_limits`), tests/test_contest_context_memory.py, tests/test_contest_overflow_wording.py

**Depends on:** ticket 148 (branch `ctx-overflow-fix`). A separate round, started over the commit that lands 148.

Found by all three reviews of 147. Confirmed at c0c9c74: a record `last_ok` 33 000 / `grew` 200 000 under a Kilo window of 262 144 gives `_context_budget` → `(33000, 'remembered')`.

**What this ticket does not cover:** a *tight* record that is wrong (written by a false overflow) still sizes the model until it expires. Ticket 148 stops such records from being written; this ticket does not add a second defence.

---

## The bug

Ticket 147 changed `size_of`. With `context_min_window = 32000` (the committed default), any `last_ok` at or above the floor sizes the model, however loose the record is. KC-73's check (`grew > LOOSE_FLOOR_SHARE × last_ok` sizes nothing) now runs only when the floor is 0.

Three other changes in 147 make that dangerous together:
- `_context_budget` and `_with_remembered_limits` let a remembered size **smaller than Kilo's declared window** win.
- Ticket 148's false readings made wrong records easy to create.
- `_pick` lets the largest `last_ok` win. Once the runner compacts at 80 % of a wrong small size, the session never grows far enough to write a larger record, so the bad size lasts until it expires (`context_memory_days`). Every agent on that model is affected, in this round and the next ones.

Example: a record with `last_ok` 33 000 and `grew` 200 000 on a model Kilo declares at 262 144.
- It sizes the model at 33 000.
- The runner compacts at 26 400.
- The round prompt plus tools alone is 15–20k, so the agent compacts every turn. This is exactly KC-73's hy3 and agnes failure.

Loose records already sitting in memory files take effect immediately, with no new evidence.

A second, smaller fault: a genuine small-window model has no named limit, and every `last_ok` below 32 000 is dropped. A true 32k model refused at about 28k is never remembered. It is covered only when Kilo declares its window, which Kilo then compacts by itself.

## The fix — one rule, no new config key

A loose record (`grew > LOOSE_FLOOR_SHARE × last_ok`) sizes the model only when it is close enough to the wall to be evidence of it:

- **Kilo declares a window D:** the loose `last_ok` counts only when `last_ok >= context_full_refusal_percent % × D` (60 % by default).
  - glm-4.5-flash: 81 311 of 131 072 is 62 %, so it counts. This is 147's live case, which must keep working.
  - 33 000 of 262 144 is 13 %, so it does not, and Kilo's window stays in charge.
- **Kilo declares nothing:** the same share of `context_limit_fallback`. With no fallback either, KC-73 applies as it was (a loose record sizes nothing).
- **A tight record** (`grew` small, or `None`) sizes the model as before, at or above `context_min_window`.
- **Named limits** are untouched.
- **The small-window case:** the floor becomes `min(context_min_window, share × D)` when D is known. A model Kilo declares at 32 768 can then remember a tight 28 000.

`size_of`, `_pick`, `smallest_size` and `remembered` take the declared window (or `None`) next to `min_window`. The two callers pass it:
- `_context_budget`: `spec.context_limit`;
- `_with_remembered_limits`: `agent.context_limit`.

Both reuse `context_memory.full_refusal_percent(config)` for the share. That is deliberate — one ini number for "close enough to the wall to be evidence", whether the evidence is a wordless refusal (148) or a loose record (here) — and the ini comment on `context_full_refusal_percent` must say it now governs both.

**The cost, stated:** glm's other live pattern — a fresh session at ~20 000 reading a ~60 000 batch — writes a loose record that sizes nothing under this rule, so the next fresh session can hit the same wall; only the sessions ceiling ends it (148's test 7 pins that it ends, not loops).

Cleanup in the same files:
- `cli._with_remembered_limits`: reset `output = None` together with `size = None`.
- `cli._with_remembered_limits`: rewrite the garbled sentence "A model intake knows the size of keeps intake's number…" and the line over 120 columns. This text predates 147.

---

## Tests

1. `last_ok` 33 000, `grew` 200 000, Kilo 262 144:
   - `_context_budget` is `(262144, "kilo")`;
   - the next round's overlay carries nothing for it.
2. The same record with no Kilo window and a fallback of 128 000: it sizes nothing (KC-73).
3. glm: `last_ok` 81 311, `grew` 42 000, Kilo 131 072 → 81 311 (`remembered`). All of 147's positive tests still pass.
4. A tight record of 40 000 (`grew` 1 000) under a 131 072 Kilo window still sizes the model.
5. Kilo 32 768 and a tight 28 000 → 28 000 is remembered and sizes the model.
6. A KC-73 loop guard: a run whose memory holds a record between 32k and 60k with a large `grew`, on a model with a big Kilo window, does not compact more often than every 3 turns. It ends READY.
7. `_context_budget` when the remembered size equals Kilo's window exactly: `(size, "kilo")`.

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
