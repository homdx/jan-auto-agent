# L4 — `graph.py` drops `from <pkg> import <module> as <alias>` edges

**Severity:** HIGH  
**File:** `tools/collect/graph.py`  
**Symbol:** `—`  
**Round:** 25 of 27  
**Size:** S  
**Source:** `docs/collect-epics/EPIC-L-live-findings.md` § L4  
**Depends on:** V1 (the queries that expose it). Does not touch `context_assembler.py`.  

---

**Priority:** High · **Size:** S · **Files:** `tools/collect/graph.py`
**Depends on:** V1 (the queries that expose it). Does not touch `context_assembler.py`.

### The observation

`tools/collect/cli.py:57-65` imports nine sibling modules the same way:

```python
from tools.collect import test_map as test_map_mod
from tools.collect import risk as risk_mod
```

The artifact's `import_edges["tools/collect/cli.py"]` lists five targets, none
of those nine. So `imported_by["tools/collect/test_map.py"]` is three tests plus
`risk.py`, and the V3 row the coder sees for `test_map.py` reads
`callers: 4 modules import this (1 non-test): tools/collect/risk.py` — under a
header that says *do not contradict*. The module's main shipped consumer is
missing from a fact the model is told to trust.

The `from tools.collect import X` form is the house style of `tools/collect/`,
so the rows are wrong precisely for the package the epics are about.

### Do

1. In `resolve_import` (or wherever `from pkg import name` is resolved): when
   `name` resolves to a **module** file under `pkg` — `pkg/name.py` or
   `pkg/name/__init__.py` — emit an edge to that module, not (only) to
   `pkg/__init__.py`. The `as alias` part must be irrelevant to resolution.
2. Keep the existing behaviour when `name` is a symbol inside `pkg/__init__.py`.
3. Deterministic output: sorted, deduplicated, as the tables are today.

### Acceptance

- [ ] A fixture package where `a.py` does `from pkg import b as bb` yields
      `import_edges["pkg/a.py"] ∋ "pkg/b.py"` and `imported_by["pkg/b.py"] ∋ "pkg/a.py"`.
- [ ] `from pkg import SYMBOL` where `SYMBOL` is defined in `pkg/__init__.py`
      still resolves to `pkg/__init__.py` and nothing else.
- [ ] Rebuilt against this repo: `import_edges["tools/collect/cli.py"]` contains
      all nine `*_mod` targets; `callers_of("tools/collect/test_map.py")` contains
      `tools/collect/cli.py`.
- [ ] `scripts/collect_metrics.py` (M1) is re-run and the new
      `import_edges` total is recorded next to the old one in the commit message.
- [ ] New: `tests/test_collect_graph_from_import_module_alias.py`.

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
