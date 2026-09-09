# FIX-3 — `pullv3` model benchmark task

**Type:** Task
**Component:** `jan-auto-agent` / `tools/auto/state.py`
**Branch:** `pullv3`
**Prepared:** 2026-09-08
**Supersedes:** `TODO-FIX-pullv3.md` (16 items — 15 now closed; see Appendix B)

---

## Purpose

Re-run the multi-model patch benchmark against `pullv3`. Unlike the previous
round, **there is only one item of real work left** (M1 below). The rest of
this ticket exists so the run measures something useful:

1. Can the model fix M1 correctly?
2. Can the model **recognise that the other 15 items are already fixed** and
   decline to patch them?

Point 2 is the harder test. In the previous round, every model was handed a
list whose premises had gone stale and **not one of them checked the live code
first** — several produced patches against deleted code, and two shipped
changes that left the test suite red because they never opened the tests
pinning the old contract.

---

## Ground rules for the run

Give these to every model verbatim. They are the scoring criteria.

1. **Verify before fixing.** Grep the live source for the claimed defect. If it
   is already fixed, say so and stop — do not produce a patch.
2. **One local commit per item.** Never push.
3. **Every fix ships a test** in `tests_bugfix/` (repo convention: every
   `FIX-2` commit has one).
4. **Run the suite as four separate invocations.** Combining the roots in one
   `pytest` call produces ~362 false errors from a conftest collision:
   ```bash
   for d in tests tests_bugfix .smoke_tests .regression_tests; do python3 -m pytest "$d" -q --timeout=180; done
   ```
   `python` is not on PATH — use `python3`.
   `.smoke_tests/test_coder_smart_context.py::TestRewriteGateContextSatisfied::test_rewriter_called_when_context_satisfied`
   is timing-flaky under load; re-run it alone before treating it as a real failure.
5. **Do not adjust a test to make a change pass** unless the test pins a
   contract the change deliberately replaces — and say so explicitly if it does.
6. **Stay in scope.** No opportunistic refactors bundled into an unrelated fix.

---

## M1 — `_save_plan()` writes `plan.json` without validation

**Priority:** Low
**File:** `tools/auto/state.py` (`StateStore._save_plan`)
**Status:** OPEN — the only unresolved item on the list

### Current code

```python
def _save_plan(self) -> None:
    self._atomic_write(
        self._plan_path,
        json.dumps(self._plan, indent=2, ensure_ascii=False),
    )
```

### What was reported

> `_save_plan()` writes `self._plan` straight to disk with zero validation. If
> any code path ever mutates `self._plan` directly and calls `_save_plan`
> without going through `set_task_status`/`upsert_task`, corrupted data still
> reaches disk.

### What the code actually shows

This was audited on 2026-09-08. The premise describes a path that **does not
exist today**:

- All **8** `_save_plan()` call sites are inside validated setters:
  `upsert_task`, `set_task_status`, `remove_task`, `increment_task_counters`,
  `increment_impl_version`, `apply_rewrite`, `_create_fresh`.
- `self._plan` is **never** referenced outside `state.py`, and is never
  returned by reference — no caller can hold and mutate it.
- The write is already atomic via `self._atomic_write`.

So this is **not a live bug**. It is an undocumented invariant. The original
report asks only to *"decide whether `_save_plan` should re-validate every task
before writing, or whether this is acceptable given all current call sites go
through validated setters — **document the decision either way**."*

### The task

Make and record the decision. Two defensible answers:

**(a) Document the invariant (recommended).** Add a `_save_plan` docstring
stating that every caller is a validated setter, that `self._plan` never
escapes the class, and that a new call site must therefore either go through a
setter or validate first. Zero runtime cost. Add a test asserting the invariant
holds — e.g. an AST check that `_save_plan()` is only called from the known
setters, so a ninth unvalidated call site fails CI.

**(b) Re-validate on write.** Run every task through the existing validator
before serialising. Defense in depth against a path that does not exist, paid
on every status change — of which there are many per task per run. If chosen,
the docstring must justify the cost.

**A model that argues for (a) with evidence scores higher than one that
implements (b) reflexively.** This item is testing judgment, not typing.

### Acceptance criteria

- [ ] `_save_plan` carries a docstring recording the decision and its reasoning.
- [ ] If validation was added: a test proves a malformed task is rejected before
      it reaches disk, and the docstring justifies the per-write cost.
- [ ] If the invariant was documented instead: a test in `tests_bugfix/` fails
      if a `_save_plan()` call site is added outside the validated setters.
- [ ] All four test roots green, run separately.
- [ ] One commit, not pushed.

### Do NOT

- Do not touch `set_task_status` — its `_validate_extra_task_fields` check is
  intentional and correct (the original report says so explicitly).
- Do not touch `increment_task_counters` — that was M2, fixed in `ea7e613`.
- Do not make `_save_plan` non-atomic.

---

## Appendix A — decoy items (all already fixed)

**These are the benchmark's control group.** Each was a real, open defect when
`TODO-FIX-pullv3.md` was written at `2026-09-08 00:28`. All are now fixed. A
model handed the original list should detect this and decline to patch.

**Every line number in the original document is stale** — nine files have
shifted. Navigating by them will land in the wrong function.

| Item | Original claim | Fixed by | How to verify it is closed |
|---|---|---|---|
| C1 | `resolve_llm_profile` outside the per-gate `try` | `6dd8ee6` | `build_validators` resolves inside the `try`; a bad profile disables one gate |
| C2 | 8 unguarded probe reads in `ClusterReviewer.__init__` | `eaf0f49` | all **9** keys wrapped in `try/except ValueError` |
| H1 | `int(impl_version or 1)` raises | `1a25a95`, `658208e` | `_coerce_impl_version()` exists in `outer_loop.py` |
| H2 | `strip("\"'()[],;:")` corrupts `[file].py` | `efe5fbf`, `6064891` | `_strip_wrapping_delimiters()` replaced the blanket strip |
| H3 | `--collect` never checks config exists | `cb752e4`, `2945da3` | check present — see deviation note below |
| H4 | `(?=\n### )` truncates a task section | `5bba7ce`, `15a787d` | separator terminator **and** fence-aware body |
| H5 | `startswith("OK") and len(reply) <= 4` | `0ccfdef`, `4fc761e` | `_is_ok_sentinel()` matches the whole reply |
| H6 | `feedback()` returns `"approved"` | `af6f0b5` | returns `""` when approved |
| H7 | `count += 1` before the exclusion check | `607a21f`, `9c5b569` | exclusion `continue` now precedes the increment |
| H8 | wrapper regex searches `full_source` | `514e000`, `f9a1d69` | AST-based, scoped to the call site's class |
| H9 | `suffix or ".py"` at 2 sites | `bee47ef`, `4a5a2b5` | **3** sites — the doc missed `gate1_grounding` |
| H10 | non-atomic write + unguarded upsert loop | `607a21f`, `b5aa13c` | `atomic_write_text` + per-task `try/except ValueError` |
| H11a | wave-local tie-break | `607a21f`, `c4b710b` | queue globally re-sorted after each pop |
| H11b | null `line_end` defaults to `line_start` | `2c7ad71`, `85664d4` | requires `line_start` **and** `line_end` on A |
| M2 | `increment_task_counters` unvalidated deltas | `ea7e613` | `_coerce_delta()` repairs and warns |
| M3 | 41 unguarded `getint`/`getfloat`/`getboolean` | `3c8cf44` | `tools/config_safe.py`; 31 sites converted |

### Corrections to the original document

Flag these to any model working from `TODO-FIX-pullv3.md`:

1. **C2 undercounts.** Says "8 config values", then lists 9. The ninth,
   `probe_memo_max_entries`, is the one two models missed last round.
2. **H9 undercounts.** Names 2 sites; there were 3. The third was in
   `gate1_grounding.py` and appears nowhere in the report.
3. **H3's fix intentionally deviates from the spec.** The document asks for the
   same unconditional `exists → exit(1)` that `--auto` uses. What shipped only
   errors when `--config` was passed *explicitly*; a missing **default** path
   warns and continues. This is deliberate — the literal fix breaks every run
   that relies on built-in defaults from outside the repo root. **Do not
   "correct" this back.**
4. **M3's "41 of 126" is not wrong, it is a different measure.** The document
   counted `tools/auto/*.py` calls lacking a *local* `try/except`. The sweep
   counted all of `tools/`, required a `fallback=`, and treated any enclosing
   `try/except ValueError|Exception` as guarded — 31 sites.
5. **H4 is the cautionary tale.** It was fixed once, correctly, against the
   defect as written — and the replacement terminator reopened the same class
   of bug through a different door. It took a live reproduction, not a re-read,
   to catch. Fixed items deserve re-testing, not re-reading.

---

## Appendix B — scoring the previous round

Baseline for comparison. Nine models, patches in `part3/`.

| Model | Attempted | Best-in-class | Wrong / broken | Notes |
|---|---|---|---|---|
| sensenova-6.8-flash-lite | 7 | 3 | 1 | Sharpest root-cause analysis; only model to *decline* items honestly |
| step-3-7-flash | 9 | 2 | 1 | Only model to complete M3; chronically bundles unrelated refactors |
| hy3 | 8 | 2 | 2 | Clean style, incomplete finishes (missed a key in C2) |
| dots3-note | 4 | 2 | 1 | Small and sharp; H2 normalised `[file].py` → `file.py`, wrong |
| step1-dots-3-notes | 5 | 2 | 1 | Solid, unambitious |
| laguna-s-2-1 | 8 | 0 | 1 | Competent, never best; C1 left the shared-profile crash |
| sensenova-6.7-flash | 8 | 0 | 2 | Middling |
| mistral-medium-3-5 | 6 | 0 | 4 | **Twice left the suite red** — changed behaviour without touching the tests pinning the old contract |
| z-ai-glm-4-5-flash | 7 | 1 | 3 | Erratic; C1 duplicated a block into an exception handler; H8 was a no-op |

### What to watch for this round

- **Did the model check the live code before patching?** Last round: zero did.
- **Did it decline the 15 closed items?** Any patch against a decoy is a miss.
- **Did it run the tests that pin the contract it changed?**
- **Did it keep the change in scope?**
- **On M1, did it argue for the cheap correct answer, or reflexively add code?**
