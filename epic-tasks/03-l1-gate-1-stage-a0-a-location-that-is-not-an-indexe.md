# L1 — Gate-1 Stage A0: a location that is not an indexed source file costs no LLM call

**Severity:** HIGH  
**File:** `tools/auto/gate1_filter.py`  
**Symbol:** `—`  
**Round:** 3 of 24  
**Size:** S  
**Source:** `docs/collect-epics/EPIC-L-live-findings.md` § L1  
**Depends on:** nothing. Not gated on `M3` — this is about file type, not about  

---

**Priority:** High · **Size:** S · **Files:** `tools/auto/gate1_filter.py`
**Depends on:** nothing. Not gated on `M3` — this is about file type, not about
safety suppression.

### The observation

Gate 1's Stage A is an existence check (`_check_existence`, no LLM); Stage B is
one LLM call per surviving candidate (`_init_presence_provider`,
`architect.py`-independent). In the live round Stage B spent **74 of 834 calls**
— roughly 18 minutes — judging claims against files that are not code:

| location extension | gate-1 LLM calls | confirmed | rejected |
|---|---|---|---|
| `.md` | 28 | 7 | 21 |
| `.ini` | 27 | 10 | 17 |
| `.yaml` | 15 | 5 | 10 |
| `.json` / `.sh` / none | 4 | 1 | 3 |
| **total non-code** | **74** | **23** | **51** |

Typical rejection, verbatim from the trace:

> *"The code shown is a snippet of agents.ini configuration (comments and INI
> section headers), containing no Python code, no test files, and no references
> to test coverage — there is nothing in the excerpt to support the claim."*

The architect proposes these because the cluster it is handed *is* the ini
files. One observed architect prompt was 12 015 chars of `agents.ini` +
`agents_128k.ini` + `agents_256k.ini` + `agents_32k*.ini` verbatim.

### The thing to be careful about

**23 of the 74 were confirmed.** A claim about a `.md` or `.ini` file is not
automatically wrong — a doc that contradicts the code is a real finding, and
`testtext7`'s entire goal is about docstrings and comments. So this ticket must
**not** reject non-code locations outright.

What it may do is skip the *LLM presence check* for a location the collect model
does not index, and route it by task mode instead:

- `task_mode == "docs"`, or the goal text names docs/config → keep today's
  behaviour exactly, LLM call included.
- otherwise → reject at Stage A0 with
  `stage="existence"`, `reason="location is not an indexed source file (<ext>)"`.

### Do

1. In `Gate1Filter.filter`, between Stage A and Stage B, add Stage A0. It runs
   only when a usable collect model is available; with no model it is a no-op
   and every candidate proceeds exactly as today (fail-open, house rule 5).
2. The test is *membership in the collect model*, not an extension allowlist —
   the artifact indexes `.py` and `.java`, and the right question is "does the
   model know this path". Fall back to the extension only when the model is
   absent, in which case the stage is skipped entirely.
3. Record the rejection through the existing `stage="existence"` channel so
   `analyze_logs.py` needs no change and the count shows up in the existing
   Stage-A bucket. Add a distinct `reason` prefix so the two are separable.
4. One config key, defaulting to today's behaviour:
   `[gate1] skip_llm_for_unindexed = true`. Set it `false` and Stage A0 does
   nothing.

### Acceptance

- [ ] A candidate on `agents.ini` with `task_mode="code"` and a fresh model is
      rejected at `stage="existence"` with no LLM call.
- [ ] The same candidate with `task_mode="docs"` reaches Stage B and makes its
      LLM call, exactly as today.
- [ ] With no collect model (absent or stale), every candidate reaches Stage B
      — byte-identical behaviour to today.
- [ ] `skip_llm_for_unindexed = false` disables the stage.
- [ ] A `.java` candidate is **not** rejected — the artifact indexes Java.
- [ ] Counted: the run summary reports how many candidates Stage A0 removed.
- [ ] New: `tests/test_gate1_stage_a0_unindexed.py`.

### How to know it worked

Re-run the same goal against the same tree and compare
`gate1 llm_request` counts in the trace. The target is the 74 from
`LIVE-RUN-VALIDATION.md` §3(a) going to near zero **with the confirmed-candidate
count for `.py` locations unchanged**. If confirmed `.py` candidates drop, the
stage is over-reaching — that is the regression to watch, not the saving.

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
5. **Run the suite as four separate invocations** — combining the roots in one
   `pytest` call produces ~362 false errors from a conftest collision:
   ```bash
   for d in tests tests_bugfix .smoke_tests .regression_tests; do python3 -m pytest "$d" -q --timeout=180; done
   ```
   `python` is not on PATH — use `python3`.
6. **Fail open.** A broken artifact, a malformed config key or an absent model
   degrades to "no collect data" and never raises into a run. Everything added
   here inherits that.
7. **Never widen provenance.** Static facts and LLM prose stay separately
   labelled. `is_safe()` keeps reading only `guarded_accesses` /
   `FAIL_OPEN_REGISTRY` / `CONTRACTS`.
8. **Never run anything against a live provider config.** Copy
   `agents_128k.ini` to a scratch path and point `base_url` at a stub before any
   measurement run.
