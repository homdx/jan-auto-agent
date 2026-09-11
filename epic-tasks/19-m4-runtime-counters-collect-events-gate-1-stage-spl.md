# M4 — Runtime counters: collect events + Gate-1 stage split

**Status:** open (verified against `2f9005d`, 2026-09-12)  
**Severity:** HIGH  
**File:** `tools/auto/run_trace.py`  
**Symbol:** `—`  
**Round:** 19 of 27  
**Size:** M  
**Source:** `docs/collect-epics/EPIC-M-metrics.md` § M4  
**Also touches:** `tools/auto/collect_bridge.py`, `tools/auto/gate1_filter.py`, `analyze_logs.py`  

---

**Priority:** High · **Size:** M · **Files:** `tools/auto/run_trace.py`,
`tools/auto/collect_bridge.py`, `tools/auto/gate1_filter.py`, `analyze_logs.py`

Tier 0 and Tier 1 are static. Tier 2 needs the run to say what happened.
(This ticket absorbs v1's C5 and B4, which were measurement work filed as
feature work at the end of two different epics.)

### Verified against `2f9005d` (2026-09-12)

- `collect_miss` already exists: V9 emits it through `tracer.event(...,
  kind="collect_miss", params={reason, target, task_id?})` from
  `CollectBridge._miss`, with per-run counts on `bridge.collect_misses`.
  Reuse that channel for `absent` / `stale` / `unknown_module`; do not add a
  second one. `run_trace.py` is not where it lives — it is `tools/agent_trace.py`.
- `scripts/trace_round_snapshot.py` today counts a block by grepping the trace
  for the literal header `COLLECT MODEL (static facts` in the coder prompt.
  Once `collect_block` events exist, the snapshot must count those instead
  (and keep the grep as a fallback for pre-M4 traces), or the before/after
  columns are not comparable.
- `collect_shrink` observes `_shrink` from `context_for`: `before=len(raw)`,
  `after=len(result)`, `path="llm"` if `shrink_calls` incremented else
  `"truncate"`. Still no edit inside `_shrink`.

### Do

1. Four collect events, structured like the existing ones:

   | event | fields |
   |---|---|
   | `collect_block` | `task_id`, `target_file`, `chars`, `rows_kept`, `rows_cut` |
   | `collect_shrink` | `task_id`, `target_file`, `before`, `after`, `path` = `llm` \| `truncate` |
   | `collect_miss` | `task_id`, `target_file`, `reason` = `absent` \| `stale` \| `dirty` \| `unknown_module` |
   | `collect_summary` | per run: blocks injected, chars injected, shrink calls, misses |

   `collect_shrink` observes `_shrink` **from the outside** — from `context_for`,
   by comparing lengths and reading the existing `shrink_calls`. It does not
   modify `_shrink`.

2. `plan_phase: gate1 accepted=N rejected=M` gains the per-stage split
   (`existence=… already_safe=… presence=… duplicate=…`). With the feature off
   the line still prints `already_safe=0`, so log parsing is stable across the
   flag.

3. `analyze_logs.py` prints a **collect** section next to its existing probe
   section:

```
collect   blocks 12/14 tasks · 6 431 chars · shrink 1 (llm) · miss 2 (dirty)
probe     requests 9 · hits 7 · misses 2 · declined 0 · escalated 1
gate1     accepted 4 · existence 3 · already_safe 0 · presence 5 · duplicate 1
```

### Acceptance

- [ ] A `--dry-run` against a stub emits all four events plus the summary.
- [ ] `git diff` touches no line inside `CollectBridge._shrink`.
- [ ] Zero events and an unchanged log shape when no bridge is wired in.
- [ ] New: `tests/test_run_trace_collect_events.py`.

---

## Ground rules — these are the scoring criteria

1. **Verify before implementing.** Grep the live source for what this ticket
   claims. It was written against `competition` at `68b78a0`; if the code has
   moved, the ticket is the stale one — say so and adapt rather than forcing it.
2. **`CollectBridge._shrink` is not to be modified, reordered, or replaced.**
   New work runs *before* it, never instead of it. A diff that touches a line
   inside `_shrink` fails this round regardless of what else it does.
3. **One local commit for this ticket alone.** Never `git push`. No
   opportunistic refactors bundled in.
4. **Ships a test.** New behaviour → `tests/`; a fix to something that used to
   be wrong → `tests_bugfix/`. If you add a `tests/test_*.py`, run
   `python3 scripts/sync_test_tiers.py` so the `.smoke_tests/` mirror exists
   (a pre-commit hook enforces it).
5. **Run the two real roots, one after the other, never combined** —
   `.smoke_tests/` and `.regression_tests/` are symlink views onto `tests/`
   and run nothing extra; combining roots in one `pytest` call produces ~362
   false errors from a conftest collision:
   ```bash
   python3 -m pytest tests -q --timeout=180 && python3 -m pytest tests_bugfix -q --timeout=180
   ```
   `python` is not on PATH — use `python3`. One suite at a time — the machine
   is shared with the other round entrants.
6. **Fail open.** A broken artifact, a malformed config key or an absent model
   degrades to "no collect data" and never raises into a run. Everything added
   here inherits that.
7. **Never widen provenance.** Static facts and LLM prose stay separately
   labelled. `is_safe()` keeps reading only `guarded_accesses` /
   `FAIL_OPEN_REGISTRY` / `CONTRACTS`.
8. **Never run anything against a live provider config.** Copy
   `agents_128k.ini` to a scratch path and point `base_url` at a stub before any
   measurement run.
