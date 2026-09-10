# V10 — Real signatures

**Severity:** MEDIUM  
**File:** `tools/collect/ast_facts.py`  
**Symbol:** `—`  
**Round:** 14 of 27  
**Size:** M  
**Source:** `docs/collect-epics/PLAN-v2.md` § V10  
**Also touches:** `tools/collect/model.py`  

---

**Priority:** Medium · **Size:** M · **Files:** `tools/collect/ast_facts.py`,
`tools/collect/model.py` *(v1: C4)*

`PROBE_INSTRUCTIONS` tells the Architect that `facts <symbol>` "returns its
signature". It returns `name(...)` for **4030 of 4030** symbols. The elision is
deliberate and documented, but for an agent that has to *call* the function the
parameter list is the whole point.

**Do**

1. `signature = f"{node.name}({ast.unparse(node.args)})"` for functions; classes
   keep `name(...)` unless a bare `__init__` is trivially available.
2. Truncate at 160 chars with a trailing `, …)`.
3. `ast.unparse` needs 3.9+; fall back to `name(...)` on `AttributeError` rather
   than raising.
4. This changes artifact content for every module → bump `collector_version` so
   `manifest.is_fresh` forces one rebuild. Say so in the commit message; it is
   the one intentional full rebuild in this plan, and it is why V10 lands after
   V7 makes rebuilds explicit.

**Note on methods.** `extract_symbols` walks `ast.iter_child_nodes` — top level
only — so there are **0 methods** among the 4030 symbols, and
`_qualname_matches`'s dotted-suffix branch can never match. `PROBE_INSTRUCTIONS`
already says methods cannot be looked up, so this is consistent, not broken.
Indexing methods is a separate decision with real cost and is out of scope —
but see `M3`: the adjudicated finding corpus is 35/54 method-level, so if
Stage 3 ever needs symbol-level resolution, this is the ticket it depends on.

**Acceptance**

- [ ] `pull_symbol("build_chat_request")` returns a real parameter list.
- [ ] A 40-symbol module's `module_symbols` output stays readable.
- [ ] Two builds produce byte-identical signatures.
- [ ] New: `tests/test_collect_real_signatures.py`.

---

### Stage 3 — bug hunting (V11 unconditional; V12–V13 gated on M3)

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
