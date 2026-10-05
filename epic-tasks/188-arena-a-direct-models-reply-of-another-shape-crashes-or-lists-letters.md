# 188 — arena: a direct `/models` reply of another shape crashes or lists letters

**Status:** landed
**Origin:** `arena-bugs-opus5` ticket 184
**Severity:** LOW
**File:** scripts/py_model_test.py
**Symbol:** free_from_direct_api (used by `tools/arena/models.py::_direct_for`)
**Round:** 188
**Size:** XS
**Also touches:** tests_bugfix/test_arena_direct_models_reply_shape_188.py

## Bug
A provider with a key and a URL is listed directly (`arena model list -p NAME`).
`free_from_direct_api` assumed the JSON reply is a list or an object whose
`text`/`data`/`models` is a list:

- `null`, `"ok"`, `42` → `AttributeError: … has no attribute 'get'`;
- `{"data": [1, 2]}` → the same, from an entry;
- `{"text": "Unauthorized"}` (an error body with HTTP 200) → the string was
  iterated: twelve one-letter "models" `U`, `n`, `a`, … stored and printed.

`_direct_for` turns only `HTTPError`/`OSError`/`ValueError` into the one-line
"direct list failed … — trying Kilo" hint, so the first two were a Python
traceback out of `arena`, and the third poisoned the model list.

## Fix
A reply that is not a list, or whose model field is not a list, raises
`ValueError("/models reply is not a model list: …")` — arena's existing hint
path, falling back to Kilo. Entries that are neither a string nor an object
are skipped.

## Tests
`tests_bugfix/test_arena_direct_models_reply_shape_188.py` — every case fails
on the old code (traceback or letters) and passes now.
