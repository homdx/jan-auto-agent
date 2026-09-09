# AutoController.config is a public attribute, not a property returning live ConfigParser

**Severity:** MEDIUM  
**File:** `tools/auto/controller.py`  
**Symbol:** `AutoController.config`  
**Status:** confirmed by adjudication (claude-full)  


## The defect

self.config is a plain public mutable configparser.ConfigParser (line 346, no @property, no copy). A holder of the controller can call config.set/remove_section/read_string and change values that live reads consume mid-run: _collect_use_flag() at controller.py:1281 reads self.config directly, and resolve_gate_order at inner_loop.py:1901 is handed the same object. task_mode (line 384) and self.limits (line 416) are snapshotted in __init__ so they are inert to later mutation — the exposure is partial and limited to the live-read paths.

## Evidence

```python
controller.py:346: self.config = configparser.ConfigParser(inline_comment_prefixes=(';', '#')) — plain attribute assignment, no @property getter anywhere on AutoController (only RunLimits scalar properties at lines 136-149). controller.py:1281: return safe_getboolean(self.config, 'collect', key, fallback=False) — live read of the exposed object.
```

## Reproduction

```
None — no reachable mutation path. Grepped every production reader of controller.config (controller.py:376,384,392,393,402,403,416,1108,1281,1305; pipeline.py:503-505): all read-only via .get()/safe_getboolean(). Only tests assign a fresh parser (tests/test_collect_bridge_wiring.py:163, tests_bugfix/test_t1_load_config_malformed.py:34-35).
```

## How it was verified

arbiter verdict (validate1/adjudication.md): shape confirmed — plain attribute, no @property; reported impact on task_mode/RunLimits disproved (snapshotted once in __init__), but [gates] order and [collect] use_in_auto are read live off the same object, so REAL / latent (LOW)

## What was checked to try to disprove it

Two checks that would have cleared this: (1) is config a @property returning a copy? grep 'def config' in controller.py: no match on AutoController; isinstance(type(orc).__dict__.get('config'), property) is False, line 346 is a plain assignment — defect holds. (2) does a caller mutate it today, making it HIGH/CRITICAL? grep every config.set/remove_section/remove_option/read_string/add_section/optionxform across tools/ and main.py: zero production writers exist (only apply_skill mutates during __init__ at loader.py:549, by design, and tests_bugfix/test_t1_load_config_malformed.py:35 uses read_string). So the hazard is latent, not active — MEDIUM is the honest ceiling since HIGH/CRITICAL require caller_mutates=YES.

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

- reported by: validation-v1-agnes-2-5-flash-pass1.csv, validation-v1-agnes-2-5-flash.csv, validation-v1-glm-4.5-flash.csv, validation-v1-glm.csv, validation-v1-hy3.csv, validation-v1-kilo.csv, validation-v1-longcat-2-0-free.csv, validation-v1-sensenova-6.7-flash-lite.csv
- adjudicated by: claude-full
- ground truth: `validate1/truth.csv`
