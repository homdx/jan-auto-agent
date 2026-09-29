# KC-EXT-02 — Add `__eq__` to the `Calc` class

**Status:** open
**File:** `calc.py`, `tests/test_calc.py`
**Symbol:** `Calc.__eq__`
**Size:** S
**Source:** operator demo — part 2 of the 2-ticket relay validating external-repo contest support

---

## Context

Part 2 of 2. `__repr__` (part 1, KC-EXT-01) is already in the tree on the base
branch you receive. Do not change it.

## What must change

Add `Calc.__eq__` that returns `True` when both sides are `Calc` instances with
equal `value`, and `NotImplemented` for any other type. For example:

```python
>>> Calc(5) == Calc(5)
True
>>> Calc(5) == Calc(6)
False
>>> Calc(5) == 5          # Python falls back to identity
False
>>> Calc(5).__eq__(5)
NotImplemented
```

Add one test in `tests/test_calc.py` that covers at least:
- equal values → `True`
- unequal values → `False`
- comparing with a non-`Calc` returns `NotImplemented`

## Acceptance

- [ ] `Calc(3) == Calc(3)` is `True`
- [ ] `Calc(3) == Calc(4)` is `False`
- [ ] `Calc(3).__eq__(3)` is `NotImplemented`
- [ ] `tests/test_calc.py` contains a test named `test_eq` that passes under `pytest`.

## Ground rules

- Only touch `calc.py` and `tests/test_calc.py`.
- No test calls a live external service.
- One commit, no push.
