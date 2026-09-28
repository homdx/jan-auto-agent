# KC-68 — the pytest worker count is split by the suite slots, not only by the crowd

**Status:** landed `8d0b875` — by hand, 2026-09-26; found in round 69
**Severity:** MEDIUM (serialized harvests ran their roots at half the cores)
**Round:** 121
**Size:** S
**File:** `tools/contest/cli.py`, `tools/contest/runner.py`
**Depends on:** KC-58 (landed `dbab165` — `agent_suite_slots`), KC-65 (landed `23db0b3` — the auto worker count)

## Problem

With KC-58's slots armed, at most `agent_suite_slots` whole roots run at once,
but the auto worker count still divided the cores by the live agents. Round 69
had 8 live agents, 2 slots and 8 cores, so every root got `-n 2`: four cores
were busy while the serialized harvests ran one by one.

## Fix

The auto count is `cores // min(live, slots)`, which gives 4 each in round 69.
A fixed `pytest_workers_per_agent` still wins, and `slots = 0` keeps KC-65's
rule. The plan names the rule ("4 each (auto, 2 suite slots)"). The harvest
line says it waited for a slot, but only when the wait took a second or more.

Tests: `tests/test_contest_cli.py`, `tests/test_contest_runner.py`.
