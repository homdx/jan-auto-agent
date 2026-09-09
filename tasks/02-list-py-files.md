# Path-qualified skip_dirs entries are silently ignored by list_py_files / list_source_files

**Severity:** MEDIUM  
**File:** `tools/file_reader.py`  
**Symbol:** `list_py_files`  
**Status:** confirmed by adjudication (claude-full)  


## The defect

An operator who writes a path-qualified exclusion in [search] skip_dirs, e.g. 'tools/auto' or './.git', gets no warning and no effect: the entry never matches, so those directories are walked and their files offered as search candidates. The Coder/Architect then sees content the operator explicitly excluded, which can add noise, blow the context budget, and pull excluded files into the Coder's view. The identical comparison at repo_ingest.py:247 means the same misspelling silently broadens what the Architect clusters for review.

## Evidence

```python
skip_set = set(skip_dirs)   # tools/file_reader.py:74; dirnames[:] = [d for d in dirnames if d not in skip_set]   # :77 — d is os.walk's bare directory NAME, so 'y/z' and './.git' can never equal d; identical at :108-113 (list_source_files) and repo_ingest.py:247
```

## Reproduction

```
Temp dir with x/a.py, y/z/b.py, y/keep.py, then list_py_files(d, ['x','y/z']) returns ['keep.py','b.py'] — the bare 'x' entry worked, the path form 'y/z' was dropped with no log. Also confirmed list_source_files(d, ['sub/dep']) and list_source_files(d, ['./.git']) both return the supposedly-skipped files.
```

## How it was verified

reproduced live: built a temp dir with tests/fixtures/keepme.py, called list_py_files with skip_dirs=['tests/fixtures'] vs skip_dirs=['fixtures']

## What was checked to try to disprove it

Disproving facts sought: (1) is there any normalisation that splits or resolves path entries? No — the value goes into a set verbatim, and the config loaders (main.py:153, search_agent.py:383-384, repo_ingest.py:351-352) only strip() and split on ',', never Path-normalising. (2) is the failure at least logged? No log line exists for an unmatched entry, so the operator gets zero signal. (3) is any site comparing on the relative path instead? No — all three sites compare against os.walk dirnames entries, which are bare names by construction. Confirmed live that bare names work and path forms are dropped, so this is a silent no-op, not a total failure.

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
