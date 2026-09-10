# L3 — Gate 1 asks a presence question that three of five goals cannot answer

**Severity:** MEDIUM  
**File:** `—`  
**Symbol:** `—`  
**Round:** 24 of 27  
**Size:** S (measurement only)  
**Source:** `docs/collect-epics/EPIC-L-live-findings.md` § L3  

---

**Priority:** Medium · **Size:** S (measurement only) · **Files:** none yet
**This ticket does not change the gate. It produces a number.**

### The observation

Gate 1's prompt, verbatim from the live trace:

> *"Is the claimed problem actually present in the code shown above, and NOT
> already fixed?"*
>
> *"Before answering, find the SPECIFIC line(s) in the code above that the claim
> depends on. If you cannot point to an actual line that supports the claim, the
> claim is not present — reject it…"*

That is a **defect-presence** question and it is a good one; it is most of why
the noise floor came down. But a goal phrased *"find X and **add** Y"* produces
candidates that are proposals, and a proposal is by construction not present in
the code:

| run | goal shape | rejections | rejected as "a proposal, not a defect" |
|---|---|---|---|
| `testtext` | "add a companion test" | 121 | 41 (**34%**) |
| `testtext7` | "report each mismatch" | 103 | 20 (19%) |
| `testtext6` | "add validation or document why safe" | 60 | 6 (10%) |
| `testtext5` | "make each one aware of…" | 44 | 4 (9%) |
| `testtext3` | "add a warning to except blocks" | 177 | 5 (3%) |
| total | | 505 | 76 (15%) |

`testtext3` is the control that makes this readable: its goal also asks to
*add* something, but the **defect** it is grounded in (an `except` that swallows
silently) is visible in the code, so gate 1 can answer the presence question
about it. `testtext`'s goal asks about a test that does not exist — nothing in
the code can show it.

### Why this is not "just fix the prompt"

Loosening gate 1's presence requirement is precisely the change that lets an
88% noise floor back in. The gate is the reason the floor came down. A prompt
that accepts "this would be good to add" accepts almost everything.

There is also a real possibility that the current behaviour is **correct** and
the goals are the problem: a goal that cannot produce a groundable claim may
simply be a badly-shaped goal, and the fix is to phrase goals the way
`testtext3`'s is — name the defect, then the remedy.

### Do (measurement only)

1. Script over `.agent/trace_*.jsonl`: for every rejected candidate, classify
   the goal shape (`defect-grounded` vs `addition-only`) and the rejection
   reason. Output a table like the one above, per run, reproducible.
2. Take the 41 `testtext` rejections in this bucket and hand-adjudicate a
   sample of 15 against the live tree: of those gate 1 rejected as "a proposal",
   how many describe a real gap? That number is the whole decision.
3. Write the finding into this file. **Stop there.**

### The decision this ticket forces

| adjudicated real gaps in the sample | conclusion |
|---|---|
| 0–2 of 15 | gate 1 is right; the goal shape is the defect. Fix the goals in `docs/TASK-jan-selfaudit.md`, change no code. |
| 3–7 of 15 | worth a second gate question for addition-only goals, scoped narrowly and measured against `validate1/truth.csv` before it ships. |
| 8+ of 15 | the gate is discarding a large share of a whole goal class; escalate to its own epic with its own noise-floor measurement. |

### Acceptance

- [ ] The classifier script is committed and its output is reproducible from
      the traces.
- [ ] The 15-item adjudication is recorded with file, symbol and verdict.
- [ ] This file gains the conclusion and the chosen branch.
- [ ] **No change to any gate-1 prompt in this ticket.**

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
