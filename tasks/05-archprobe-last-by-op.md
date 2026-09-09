# Fix ArchProbe.last_by_op

**Severity:** MEDIUM  
**File:** `tools/auto/arch_probe.py`  
**Symbol:** `ArchProbe.last_by_op`  
**Status:** confirmed by adjudication (ground-file)  


## The defect

dict() is shallow and the values are [hits,misses] lists built by setdefault(op,[0,0]); the nested lists are shared out to callers. Verified in a prior round, carried in GROUND-competition.md ground truth.

## Evidence

```python
—
```

## How it was verified

dict() is shallow and the values are [hits,misses] lists built by setdefault(op,[0,0]); the nested lists are shared out to callers. Verified in a prior round, carried in GROUND-competition.md ground truth.

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

- reported by: —
- adjudicated by: ground-file
- ground truth: `validate1/truth.csv`
