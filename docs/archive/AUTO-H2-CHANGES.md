# AUTO-H2 — Grounded Gate 1 verification

Implements all five tickets from the AUTO-H2 epic. Root cause being fixed:
Gate 1's Stage B LLM verifies claims against a single extracted symbol with
zero surrounding context (no module docstring, no callee bodies), and
nothing deterministic runs before spending an LLM call on claims that are
mechanically checkable. `--validate-plan` re-runs the exact same check, so
it inherited the same blind spots rather than adding new verification power.

Six confirmed false positives from manual review motivated this (see the
corpus in `tests/test_gate1_corpus_precision.py` for the full detail on
each): three from claiming `config.getX(...)` could raise `NoSectionError`
when the call already had `fallback=`, two from "hardening" test fixtures
whose module docstrings explicitly say they're deliberately bad, one from
a two-call-hop crash claim (`suppress → is_safe → query`) that this pass
does not close — documented as a known gap, not silently ignored.

## What changed

| File | Change |
|---|---|
| `tools/auto/gate1_grounding.py` | **New.** AUTO-H2-1 (`config_fallback_note`) + AUTO-H2-3 (`callee_context`) + AUTO-H2-6 (`target_file_context`). Pure functions, independently unit-tested, no LLM calls. |
| `tools/block_extractor.py` | AUTO-H2-2: `extract_module_docstring()` added. Python-only (`ast.get_docstring`) — see its docstring for why other languages weren't worth the complexity yet. |
| `tools/auto/gate1_filter.py` | Wires the above into Stage B: `_build_grounding_notes()` assembles evidence, injected into `_USER_PROMPT_TMPL` via a new `{grounding_notes}` slot. `_check_existence`'s signature is untouched (existing tests call it directly with the old 3-tuple contract). `_check_presence` gained two keyword-only params (`module_docstring`, `base_dir`), both defaulted so the one existing direct 2-arg call site (`test_llm_stream_think_suppression.py`) is unaffected. `filter_candidates()` gained `model_override`/`active_override`, both `None` by default — the live `--auto` call site (`tools/auto/pipeline.py`) never passes them, so live Gate 1 behavior is unchanged. |
| `tools/auto/plan_validator.py` | AUTO-H2-4: reads an optional `[gate1_validate]` section (`model`, `active`) and passes it through as the override above. Absent section → identical behavior to before. |
| `tests/test_gate1_corpus_precision.py` | **New.** AUTO-H2-5: permanent regression corpus (6 confirmed-false + 6 confirmed-legit real candidates against real files in this repo), two test tiers, plus an opt-in live-model test. |

**Deliberately NOT touched:** `tools/auto/pipeline.py` (live `--auto` Gate 1
call site) — every new parameter defaults to today's behavior, so live
mode gets the free/cheap improvements (Stage A2, module docstring context,
callee context) automatically through the shared `Gate1Filter`, with zero
code changes needed there, and zero risk of the model-override plumbing
leaking into the hot path.

## Design decision worth knowing about

Stage A2 never auto-rejects. Every check in `gate1_grounding.py` returns
*evidence* injected into Stage B's prompt as an explicit counter-fact the
LLM must address — not a verdict. Auto-rejecting on a regex/AST heuristic
trades one false-positive class for a false-negative class (a real bug
silently dropped because a pattern matched by coincidence), which isn't
obviously a win. The LLM stays the final arbiter; it just stops being
blind to facts that were already sitting in the code block it was shown.

A real bug in the corpus test's first draft accidentally validated this
design choice: the first version of `_build_grounding_notes` appended a
guidance sentence to the LLM containing the literal words "deliberately"/
"toy module"/"negative case" — which then falsely matched the corpus
test's own fixture-detection assertion, even for `gates.py`/`loader.py`'s
perfectly ordinary, unrelated module docstrings. Caught immediately because
the corpus includes confirmed-*legitimate* candidates specifically to catch
this class of over-triggering, not just confirmed-false ones. Fixed by
rephrasing the guidance to avoid the literal trigger words while keeping
the same meaning for a real model to act on.

## AUTO-H2-6 — confirmed in production, added after initial delivery

While grading a real `--validate-plan` run against two real 31/35-candidate
pools, found that **100% of candidates whose `Location:` field named a
different file than their own `target_files`** were rejected (14/14 in one
pool, 12/12 in the other). Traced to the exact mechanism, not a guess:

```python
# tools/auto/backlog_prioritiser.py — "Location:" IS cited_location.file
loc_str = loc.file

# tools/auto/gate1_filter.py — _check_existence reads FROM cited_location.file,
# never target_files
abs_path = base_dir / loc.file
```

Stage B was being shown the cited-evidence file (often a cluster-seed
config file or an unrelated test) while being asked whether a problem
exists in a *different* file — the one that would actually get edited.
Every observed rejection reason said some version of "the code shown is X,
not Y" — Stage B was correct given what it saw; Stage A showed it the
wrong thing.

`tools/auto/gate1_grounding.py::target_file_context()` fixes this the same
way as everything else in this epic: when `cited_location.file` isn't
among `target_files`, pull relevant content from the actual target file
(resolving a symbol named in the instruction when possible, falling back
to the first real `def`/`class` rather than a docstring-only head-of-file
slice) and inject it as additional evidence — never auto-accept, Stage B
still judges. Zero cost for the ~55-60% of real candidates where Location
and Target already agree.

Regression test (`tests/test_gate1_location_target_mismatch.py`) uses the
*exact* real candidates from that production run (AUTO-T1/AUTO-T2,
reconstructed with the same title/Location/target_files/instruction),
checked against this actual repo, not synthetic fixtures.

## Running the tests

```bash
# Everything touched by this change (fast, no network):
python -m pytest tests/test_gate1_corpus_precision.py tests/test_gate1_location_target_mismatch.py \
    tests/test_auto_b3.py tests/test_auto_h1.py tests/test_bugfix_review.py \
    tests/test_gate1_log_levels.py tests/test_cr8_gate1_creative_empty_target.py \
    tests/test_llm_stream_think_suppression.py tests/test_architect_resilience.py \
    tests/test_block_extractor_dotted_python_symbols.py tests/test_cr20_3_plan_validator.py -q

# Full suite (confirmed clean: 3342 passed, 179 skipped, 12 xfail, 0 failed):
python -m pytest -q

# The corpus against a REAL model instead of the mocked Stage B (needs a
# working agents.ini pointed at a real endpoint):
GATE1_CORPUS_LIVE=1 python -m pytest tests/test_gate1_corpus_precision.py::test_corpus_against_real_model -q -s
```

## Known gaps (by design, not oversight)

- **AUTO-T11-shaped claims** (crash risk two call-hops away) aren't caught.
  `callee_context` only resolves one hop. Documented in the corpus entry
  for AUTO-T11 rather than silently claiming full coverage.
- **`extract_module_docstring`** is Python-only. Extend if a real false
  positive of this shape shows up in a non-Python file — no evidence that's
  happened yet.
- **`GATE1_CORPUS_LIVE` test** needs a real `agents.ini` + endpoint; not run
  in CI, by design (this repo's tests are otherwise fully offline).
