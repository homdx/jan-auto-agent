# SearchAgent aliases the module-level _DEFAULT_SKIP_DIRS list into self.skip_dirs

**Severity:** MEDIUM  
**File:** `tools/search_agent.py`  
**Symbol:** `_DEFAULT_SKIP_DIRS`  
**Status:** confirmed by adjudication (claude-full)  
**Also reported as:** `tools/search_agent.py::SearchAgent.skip_dirs`

## The defect

Any in-place mutation of one SearchAgent's skip_dirs leaks into the module default and into every SearchAgent constructed afterwards in the same process. A caller that appends an exclusion (e.g. sa.skip_dirs.append('docs')) permanently widens the skip list for all later instances — including the inner-loop agent built mid-auto — silently changing what the Coder is ever shown, with no log line. The reverse mutation (remove/extend) can re-enable skipped dirs such as .git or __pycache__, inflating every search_agent call. MEDIUM rather than HIGH because no caller mutates skip_dirs today, so the state is reachable but unexercised.

## Evidence

```python
_DEFAULT_SKIP_DIRS = [ ... ]   # tools/search_agent.py:70-73, a mutable list literal at module scope; self.skip_dirs: List[str] = _DEFAULT_SKIP_DIRS if skip_dirs is None else skip_dirs   # :94 — binds the same object by reference when skip_dirs is None
```

## Reproduction

```
python3 -c "from tools import search_agent as sa; a = sa.SearchAgent(); print(a.skip_dirs is sa._DEFAULT_SKIP_DIRS); a.skip_dirs.append('INJECTED'); print('INJECTED' in sa.SearchAgent().skip_dirs); print('INJECTED' in sa._DEFAULT_SKIP_DIRS)" → True / True / True. The two True values confirm the new instance and the module constant both carry the injected entry.
```

## How it was verified

same identity/propagation check as SearchAgent.skip_dirs

## What was checked to try to disprove it

Disproving fact sought: is a copy taken at the branch? No — the conditional selects the container object itself, not list(_DEFAULT_SKIP_DIRS), and there is no __post_init__ or later re-assignment (grep for '.skip_dirs =' across the repo finds only the single assignment at :94). Second disproof attempted: does the default get replaced per-instance? No — make_search_agent(config=None) at :364-365 and an empty config both reach the None branch via 'or None' at :384, so the aliasing is the documented default path, not an edge case.

## Acceptance

- [ ] Verify the defect against the live code before changing anything — these reports go stale faster than anyone updates them.
- [ ] Check every caller of the symbol before altering what it returns.
- [ ] Fix, with a regression test in `tests_bugfix/` that fails without it.
- [ ] All four pytest roots, run separately:
  ```bash
  for d in tests tests_bugfix .smoke_tests .regression_tests; do python3 -m pytest "$d" -q --timeout=180; done
  ```
- [ ] One local commit for this bug alone.

## Provenance

- reported by: validation-v6-sensenova-6.8-flash-lite.csv
- adjudicated by: claude-full
- ground truth: `validate1/truth.csv`
