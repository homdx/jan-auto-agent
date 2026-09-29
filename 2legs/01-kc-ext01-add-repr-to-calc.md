# KC-EXT-01 — Add `__repr__` to the `Calc` class

**Status:** open
**File:** `calc.py`, `tests/test_calc.py`
**Size:** S
**Source:** operator demo — validating external-repo contest support

---

## What must change

Add a `__repr__` method to the `Calc` class in `calc.py` that returns
the string `Calc(value=<value>)`. For example:

```python
>>> repr(Calc(42))
'Calc(value=42)'
```

Add one test in `tests/test_calc.py` that asserts `repr(Calc(7)) == "Calc(value=7)"`.

## Acceptance

- [ ] `repr(Calc(0))` returns `"Calc(value=0)"`
- [ ] `repr(Calc(42))` returns `"Calc(value=42)"`
- [ ] `tests/test_calc.py` contains a test named `test_repr` that asserts
      `repr(Calc(7)) == "Calc(value=7)"` and passes under `pytest`.

## Ground rules

- Only touch `calc.py` and `tests/test_calc.py`.
- No test calls a live external service.
- One commit, no push.
