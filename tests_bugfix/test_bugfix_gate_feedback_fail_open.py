"""tests_bugfix/test_bugfix_gate_feedback_fail_open.py

FIX-1 #4 -- verdict.feedback() broke the gate registry's fail-open contract.

``run_gates`` (tools/auto/gate_registry.py) documents a hard invariant:
every gate is fail-OPEN — an exception from ``check`` (or from reading the
file) approves that file rather than failing the attempt. The try/except
that enforced it wrapped the file read and ``spec.check()`` only, and
stopped there. The rejected verdict was then consumed one line later,
OUTSIDE the guard:

    if verdict is not None and spec.is_rejection(verdict):
        problem_blocks.append(f"{rel_path}:\n{verdict.feedback()}")

``verdict.feedback()`` is not free: creative-mode verdicts build it from
LLM-authored prose (conflict lists, missing-fact lists) that can hold
``None`` or non-string entries, so ``f"...{verdict.feedback()}"`` can raise
(commonly ``TypeError``). That exception escaped ``run_gates`` into
``InnerLoop.run_task`` and aborted the attempt -- the exact opposite of the
module's advertised contract.

The fix wraps ``verdict.feedback()`` in its own fail-open try/except inside
the per-file loop, so a raise there approves that one file only: sibling
files in the same gate are still judged normally, and the failure is
logged for a postmortem.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.gate_registry import GATES, GateSpec, run_gates  # noqa: E402


def _boom_spec() -> GateSpec:
    """A gate whose verdict always rejects, but whose feedback() raises
    only for a file whose name ends in ``bad.md`` -- so a sibling file in
    the same run can still carry a real, successfully-rendered rejection.
    """

    class _Boom:
        def __init__(self, rel_path: str) -> None:
            self.rel_path = rel_path
            self.approved = False

        def feedback(self) -> str:
            if self.rel_path.endswith("bad.md"):
                raise TypeError("can only concatenate str (not 'NoneType')")
            return f"boom: {self.rel_path}"

    return GateSpec(
        name="boom",
        attr="boom_validator",
        cap_attr="max_boom_revisions",
        default_cap=1,
        check=lambda validator, *, text, rel_path, task, loop, base_dir_path: _Boom(
            rel_path
        ),
        is_rejection=lambda verdict: True,
        reject_label="boom rejected",
        reject_log="InnerLoop: attempt %d boom rejected (%d/%d) — %s",
        cap_log="InnerLoop: boom revision cap (%d) reached — accepting.",
        factory_module="tools.auto.gate_registry",
        factory_name="make_boom_validator",
    )


def _loop(spec: GateSpec) -> SimpleNamespace:
    return SimpleNamespace(
        task_mode="creative",
        gate_order=(spec,),
        boom_validator=SimpleNamespace(max_boom_revisions=1),
    )


def _run(tmp_path: Path, files, spec: GateSpec | None = None):
    spec = spec or _boom_spec()
    for name in files:
        (tmp_path / name).write_text("content", encoding="utf-8")
    return run_gates(
        _loop(spec),
        task={"id": "T-1"},
        task_id="T-1",
        attempt=1,
        target_files=list(files),
        base_dir_path=tmp_path,
        revisions={},
        trace_stage=lambda *a, **k: None,
    )


class TestFeedbackFailureIsFailOpen:
    def test_feedback_error_does_not_escape_run_gates(self, tmp_path: Path) -> None:
        """The documented fail-open contract: a raise in feedback() approves."""
        rejection = _run(tmp_path, ["bad.md"])
        assert rejection is None

    def test_feedback_error_is_logged(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.WARNING, logger="tools.auto.gate_registry"):
            _run(tmp_path, ["bad.md"])
        assert any(
            "bad.md" in record.message
            for record in caplog.records
            if record.levelno >= logging.WARNING
        )

    def test_other_files_in_the_same_gate_are_still_rejected(
        self, tmp_path: Path
    ) -> None:
        """One bad file must not hide a genuine rejection from a sibling."""
        rejection = _run(tmp_path, ["bad.md", "good.md"])
        assert rejection is not None
        assert rejection.gate == "boom"
        assert "good.md" in rejection.feedback
        assert "bad.md" not in rejection.feedback

    def test_check_raising_is_still_fail_open(self, tmp_path: Path) -> None:
        """Sanity: the pre-existing guard around spec.check() itself must
        still work after the refactor (no regression on the original
        fail-open path)."""

        def _boom_check(validator, **kwargs):
            raise RuntimeError("check() blew up")

        spec = GateSpec(
            name="boom",
            attr="boom_validator",
            cap_attr="max_boom_revisions",
            default_cap=1,
            check=_boom_check,
            is_rejection=lambda verdict: True,
            reject_label="boom rejected",
            reject_log="InnerLoop: attempt %d boom rejected (%d/%d) — %s",
            cap_log="InnerLoop: boom revision cap (%d) reached — accepting.",
            factory_module="tools.auto.gate_registry",
            factory_name="make_boom_validator",
        )
        rejection = _run(tmp_path, ["anything.md"], spec=spec)
        assert rejection is None


class TestFeedbackFailOpenAgainstRealGates:
    """Same contract, exercised against a real, registered GateSpec (not
    just a synthetic one) so the fix is verified against production wiring
    too, not only against hand-built stand-ins."""

    def test_real_gate_with_raising_feedback_is_approved(self, tmp_path: Path) -> None:
        spec = next(s for s in GATES if "creative" in s.modes)

        class _RealBoom:
            approved = False

            def feedback(self) -> str:
                raise RuntimeError("feedback() blew up — database connection lost")

        class _StubValidator:
            def __init__(self, cap: int = 1) -> None:
                setattr(self, spec.cap_attr, cap)

            def check(self, *args, **kwargs):
                return _RealBoom()

            def should_check(self, _f):
                return True

        loop = SimpleNamespace(task_mode="creative", gate_order=(spec,))
        setattr(loop, spec.attr, _StubValidator())

        (tmp_path / "chapter.md").write_text("hello", encoding="utf-8")

        rejection = run_gates(
            loop,
            task={"id": "T-1"},
            task_id="T-1",
            attempt=1,
            target_files=["chapter.md"],
            base_dir_path=tmp_path,
            revisions={},
            trace_stage=lambda *a, **k: None,
        )
        assert rejection is None
