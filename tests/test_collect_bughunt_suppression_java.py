"""tests/test_collect_bughunt_suppression_java.py — COLLECT-28.

A candidate bug on a Java file that contradicts a static guard or
fail-open site is suppressed the same way as for Python.

`bughunt_filter.py`'s suppression decision is built entirely on
`registries.AlreadySafeIndex`, which works off `GuardedAccess.location`/
`FailOpenEntry.location` strings and `ModuleRecord.guarded_accesses` —
none of it keys off `ModuleRecord.language` at all (confirmed by reading
the source: there is no branch anywhere in `bughunt_filter.py`/
`loader.py`/`registries.py` that inspects `.language`). This file verifies
that language-neutrality holds for real Java-labeled data, which is
COLLECT-28's actual concern here.

Java's own `except`/guarded-access *extractors* are COLLECT-27's job, not
yet implemented — so unlike `test_collect_bughunt_suppression.py`'s
Python tests (which go through a real `action_collect` + `loader.load`
end-to-end pipeline), these construct a `CollectModel` directly from
hand-built Java-language `ModuleRecord`/`FailOpenEntry`/`ContractRecord`
data. That's a deliberate, narrower scope than the Python tests: it
verifies bughunt_filter.py correctly suppresses against Java-labeled
facts *whatever their source*, without depending on COLLECT-27 existing
first. COLLECT-27's own tests are the right place to verify its
extractors *produce* correct guarded_accesses/except_sites for real Java
source; this file assumes that data is already there and checks what
happens next.
"""

from __future__ import annotations

from pathlib import Path

from tools.collect import bughunt_filter as bf
from tools.collect import loader as loader_mod
from tools.collect.model import GuardedAccess, ModuleRecord
from tools.collect.registries import FailOpenEntry


def _java_model(*, modules=(), fail_open_registry=(), contracts=()) -> loader_mod.CollectModel:
    return loader_mod.CollectModel(
        status=loader_mod.STATUS_FRESH,
        collect_dir=Path("/nonexistent/.collect"),
        modules=tuple(modules),
        fail_open_registry=tuple(fail_open_registry),
        contracts=tuple(contracts),
    )


def test_java_guarded_access_candidate_is_suppressed():
    module = ModuleRecord(
        path="com/example/Point.java",
        language="java",
        guarded_accesses=(
            GuardedAccess(
                location="com/example/Point.java:12",
                access="values[0]",
                status="GUARDED",
                guard="early-return at com/example/Point.java:10",
            ),
        ),
    )
    model = _java_model(modules=[module])
    candidate = bf.BughuntCandidate(
        location="com/example/Point.java:12",
        access="values[0]",
        claim="values[0] throws ArrayIndexOutOfBoundsException",
    )

    verdicts = bf.suppress([candidate], model)
    assert verdicts[0].suppressed is True
    assert verdicts[0].reason == "guarded"
    assert verdicts[0].detail == "early-return at com/example/Point.java:10"


def test_java_real_unguarded_bug_is_not_suppressed():
    module = ModuleRecord(
        path="com/example/Point.java",
        language="java",
        guarded_accesses=(
            GuardedAccess(
                location="com/example/Point.java:20",
                access="values[1]",
                status="UNGUARDED",
            ),
        ),
    )
    model = _java_model(modules=[module])
    candidate = bf.BughuntCandidate(
        location="com/example/Point.java:20",
        access="values[1]",
        claim="values[1] throws ArrayIndexOutOfBoundsException on a short array",
    )

    verdicts = bf.suppress([candidate], model)
    assert verdicts[0].suppressed is False
    assert verdicts[0].reason == "unguarded"


def test_java_fail_open_catch_site_is_suppressed():
    # The Java analogue of a Python `except: pass` — an empty
    # `catch (Exception e) {}` (COLLECT-27's own future job to extract;
    # this test just confirms the registry consuming it doesn't care
    # which language produced the FailOpenEntry).
    module = ModuleRecord(path="com/example/Loader.java", language="java")
    fail_open = FailOpenEntry(
        location="com/example/Loader.java:45",
        exception_type="java.io.IOException",
        rationale=None,
    )
    model = _java_model(modules=[module], fail_open_registry=[fail_open])
    candidate = bf.BughuntCandidate(
        location="com/example/Loader.java:45",
        claim="silent catch swallows a real I/O failure",
    )

    verdicts = bf.suppress([candidate], model)
    assert verdicts[0].suppressed is True
    assert verdicts[0].reason == "fail_open"


def test_java_candidate_backed_only_by_llm_summary_is_not_suppressed():
    # The same non-negotiable guarantee as the Python test suite's
    # equivalent: is_safe() never reads ModuleRecord.summary, for any
    # language — an LLM's prose about a Java module never auto-suppresses
    # anything.
    from tools.collect.model import LLMSummary

    module = ModuleRecord(path="com/example/Widget.java", language="java")
    module = module.with_llm_summary(
        LLMSummary(purpose="Widget is always thread-safe.", notes="Verified by the author.")
    )
    model = _java_model(modules=[module])
    candidate = bf.BughuntCandidate(
        location="com/example/Widget.java:30",
        claim="a race condition in Widget.increment()",
    )

    verdicts = bf.suppress([candidate], model)
    assert verdicts[0].suppressed is False
    assert verdicts[0].reason == "unknown"


def test_java_and_python_candidates_suppressed_side_by_side_in_one_call():
    # A mixed batch — exactly what a real bughunt run over a mixed
    # Python+Java repo would submit — must not have one language's
    # candidates interfere with the other's verdicts.
    java_module = ModuleRecord(
        path="com/example/Point.java",
        language="java",
        guarded_accesses=(
            GuardedAccess(
                location="com/example/Point.java:12",
                access="values[0]",
                status="GUARDED",
                guard="early-return at com/example/Point.java:10",
            ),
        ),
    )
    python_module = ModuleRecord(
        path="pkg/a.py",
        language="python",
        guarded_accesses=(
            GuardedAccess(location="pkg/a.py:5", access="stack[-1]", status="UNGUARDED"),
        ),
    )
    model = _java_model(modules=[java_module, python_module])

    candidates = [
        bf.BughuntCandidate(location="com/example/Point.java:12", access="values[0]", claim="java"),
        bf.BughuntCandidate(location="pkg/a.py:5", access="stack[-1]", claim="python"),
    ]
    verdicts = bf.suppress(candidates, model)
    assert verdicts[0].suppressed is True and verdicts[0].reason == "guarded"
    assert verdicts[1].suppressed is False and verdicts[1].reason == "unguarded"


def test_java_contract_covered_location_is_suppressed():
    from tools.collect.model import ContractRecord, FunctionRecord

    module = ModuleRecord(
        path="com/example/Cache.java",
        language="java",
        public_symbols=(
            FunctionRecord(
                qualname="com/example/Cache.java:Cache",
                module="com/example/Cache.java",
                lineno=1,
                signature="class Cache",
                access_modifier="public",
            ),
        ),
    )
    contract = ContractRecord(
        name="cache_thread_safe",
        description="Cache is safe to use from multiple threads.",
        kind="seed",
        known_edge="com/example/Cache.java:Cache",
    )
    model = _java_model(modules=[module], contracts=[contract])
    candidate = bf.BughuntCandidate(
        location="com/example/Cache.java:50",
        claim="a race condition in Cache",
    )
    # No guarded_accesses/fail_open data at all — only a class-wide
    # contract with no method reference in its description, so it
    # matches at the class-wide grain (see registries.AlreadySafeIndex).
    verdicts = bf.suppress([candidate], model)
    assert verdicts[0].suppressed is True
    assert verdicts[0].reason == "contract"
