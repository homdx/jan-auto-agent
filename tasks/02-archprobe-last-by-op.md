# last_by_op hands out a shallow copy that still shares its hit lists

**Severity:** LOW  
**File:** `tools/auto/arch_probe.py`  
**Symbol:** `ArchProbe.last_by_op`  
**Status:** confirmed by adjudication (claude)  


## The defect

A caller that mutates the returned dict's nested [hits, misses] list changes ArchProbe's own tally for the current round, so last_by_op_str() reports a wrong count. That string is the by_op trace parameter written into the probe_result event (architect.py:1937, :2009) and into the unresolved-decline text (architect.py:1688), so the trace can show facts=999/1 for a round that actually resolved 0 hits. Observability only - nothing gates on it, and execute() rebuilds _last_by_op = {} at the start of every round (line 775), so the damage cannot cross into the next round or the persisted run.

## Evidence

```python
@property / def last_by_op(self) -> dict: / return dict(self._last_by_op)  # tools/auto/arch_probe.py:581-584, with the nested values allocated as mutable lists at :865 via _tally = self._last_by_op.setdefault(op.op, [0, 0])
```

## Reproduction

```
Build ArchProbe with a stub bridge over a temp dir, call execute([ProbeOp('facts','f')]), then d = probe.last_by_op; d['facts'][0] = 999. Verified live: probe._last_by_op becomes {'facts': [999, 1]} and probe.last_by_op_str() becomes 'facts=999/1' where it was 'facts=0/1' before.
```

## What was checked to try to disprove it

Ran the exact disproof step the calibration used for the _progress false positive: does the container hold nested mutable values? Yes - every value is a two-element list built by setdefault(op.op, [0, 0]) at :865, so dict() is not a complete copy here. Verified: the outer dict is a new object but the inner list is the same object, and writing through it changed the owner. Checked the three sibling accessors for the same shape: last_dropped returns list(self._last_dropped) but its elements are frozen ProbeOp dataclasses (frozen=True at :200), so that one is a complete copy; collect_bridge.py:349 returns list of frozen ContractRecord (frozen=True at model.py:173), also complete; and state.py:427 dict(self._progress) holds only str/int values, also complete.

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
