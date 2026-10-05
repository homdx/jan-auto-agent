# 182 — `human_duration` raises on `nan` and `inf`

**Status:** landed
**Origin:** `arena-bugs-sonet5` ticket 179 — the function has no caller in the product (a selfhost-pilot leftover); real for the input, unreachable
**Severity:** LOW
**File:** tools/auto/utils.py
**Symbol:** human_duration
**Round:** 182
**Size:** XS
**Also touches:** tests/test_human_duration_non_finite.py

## The bug

`human_duration` does `int(abs(float(seconds)))`; for non-finite input that raises:

```
human_duration(float("nan"))  -> ValueError: cannot convert float NaN to integer
human_duration(float("inf"))  -> OverflowError: cannot convert float infinity to integer
```

A helper whose job is to format a log/status line crashes the caller on a bad clock reading (e.g. a `0/0` rate or an unset deadline of `inf`).

## Fix

Non-finite input is echoed as `nan` / `inf` / `-inf` before the integer conversion.

## Tests

`tests/test_human_duration_non_finite.py`: nan, ±inf, and finite outputs unchanged.

## Acceptance

```bash
python3 -m pytest tests/test_human_duration_non_finite.py tests/test_human_duration.py -q
python3 scripts/sync_test_tiers.py --check
```
