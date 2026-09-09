# SearchAgent aliases the module-level skip-dir default, so one instance's append poisons every later instance

**Severity:** LOW  
**File:** `tools/search_agent.py`  
**Symbol:** `_DEFAULT_SKIP_DIRS`  
**Status:** confirmed by adjudication (claude)  
**Also reported as:** `tools/search_agent.py::SearchAgent.__init__`, `tools/search_agent.py::SearchAgent.skip_dirs`

## The defect

latent — a caller appending to one SearchAgent.skip_dirs silently adds that directory to the exclusion set of every SearchAgent() constructed later, so file discovery prunes dirs the operator never configured and the agent misses files

## Evidence

```python
_DEFAULT_SKIP_DIRS = [;. .git, __pycache__, venv, .venv, .tox,; . node_modules, dist, build, .mypy_cache, .pytest_cache,; ]
```

## Reproduction

```
python3 -c "from tools.search_agent import SearchAgent; a=SearchAgent(); a.skip_dirs.append('secret_dir'); b=SearchAgent(); print('secret_dir' in b.skip_dirs)" -> True. Verified live in this repo.
```

## What was checked to try to disprove it

The disproof that would make this a non-issue: a defensive copy at assignment. It is not present — line 94 is 'self.skip_dirs = _DEFAULT_SKIP_DIRS if skip_dirs is None else skip_dirs', a reference bind with no list(...) wrapper. Proven by identity check: SearchAgent().skip_dirs is _DEFAULT_SKIP_DIRS -> True, and a second instance is the same object too, so one append reaches both. State IS used: tools/file_reader.py:108-112 builds skip_set = set(skip_dirs) and prunes dirnames with 'dirnames[:] = [d for d in dirnames if d not in skip_set]', so a poisoned default changes which files the agent enumerates. Reachability check: grep of every .skip_dirs write in the repo returns only the constructor; three test assertions at tests_bugfix/test_fix12_make_search_agent_config.py:75,99,107 read it. Zero callers mutate it, hence LOW and caller_mutates NO.

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

- reported by: validation-v1-sensenova-6-8-flash-lite.csv
- adjudicated by: —
- ground truth: `validate1/truth.csv`
