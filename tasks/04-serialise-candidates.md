# Checkpoint serialisation aliases live candidate lists into the persisted checkpoint

**Severity:** NONE  
**File:** `tools/auto/architect.py`  
**Symbol:** `_serialise_candidates`  
**Status:** confirmed by adjudication (claude-full)  


## The defect

Latent only. _serialise_candidates copies every scalar field but stores c.target_files and c.raw by reference, so the in-memory checkpoint dict shares the same list and dict objects as the live CandidateTask. _deserialise_candidates then aliases the checkpoint entries into the restored candidates in the same way. Verified live: ser[0]['target_files'] is c.target_files → True, ser[0]['raw'] is c.raw → True, restored[0].target_files is ser[0]['target_files'] → True, restored[0].raw is ser[0]['raw'] → True. There is no consequence today because no code mutates either field in place — grep for .target_files.append/extend/insert/remove/pop/sort/clear and for raw[ assignments across tools/ and main.py returns nothing. If a mutating caller is ever added, it would rewrite an already-serialised checkpoint entry before the next atomic_write_text, corrupting a cache entry that other clusters rely on.

## Evidence

```python
"target_files":     c.target_files,   # tools/auto/architect.py:2462 — no list() wrapper, unlike the sibling scalar fields on :2459-2461; "raw": c.raw,   # :2472 — likewise by reference; and _deserialise_candidates mirrors it: 'target_files     = item.get("target_files", [])' at :2480 and 'raw = item.get("raw", {})' at :2494, both aliasing the checkpoint entry
```

## Reproduction

```
python3 -c "from tools.auto.architect import _serialise_candidates, _deserialise_candidates, CandidateTask, CitedLocation; c = CandidateTask(title='t', instruction='i', target_files=['a.py'], acceptance_check='true', cited_location=CitedLocation(file='a.py'), cluster='c', raw={'k':['v']}); ser = _serialise_candidates([c]); r = _deserialise_candidates(ser); print(ser[0]['target_files'] is c.target_files, ser[0]['raw'] is c.raw, r[0].target_files is ser[0]['target_files'], r[0].raw is ser[0]['raw'])" → True True True True
```

## How it was verified

reproduced live: ser = _serialise_candidates([c]); checked identity of ser[0]['target_files']/['raw'] against c.target_files/c.raw, and of the round-tripped _deserialise_candidates() output

## What was checked to try to disprove it

Disproving fact sought was a mutating caller, since aliasing is only a defect once something writes through it. Checked comprehensively: no in-place mutation of target_files exists anywhere in the repo (all consumers read, slice, set(), sorted() or list() it — pipeline.py:404, plan_emitter.py:329, gate1_filter.py:729/1054/1621/1623, architect.py:2462, backlog_prioritiser.py:199 which already copies); no raw[...] assignment and no .raw mutation either. Also checked the persistence boundary: the checkpoint is serialised immediately after being populated (architect.py:849 then atomic_write_text at :860), and json.dumps snapshots the current values, so a cross-run reload always yields fresh objects. That is why this stays severity NONE rather than MEDIUM — the alias is real and reachable, but nothing today writes through it.

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
