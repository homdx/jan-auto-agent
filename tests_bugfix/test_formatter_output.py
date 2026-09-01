"""Regression coverage for OutputFormatter.render output_config toggles.

A typo in main.py once built the output_config dict with the key
``show_iteration_`` instead of ``show_iteration_count``; the formatter reads
``show_iteration_count`` and so silently always defaulted to True, ignoring the
user's ``[output] show_iteration_count = false``. These tests lock the key name
as the contract between caller and formatter.
"""
import io
import sys
from types import SimpleNamespace

PROJECT_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.formatter import OutputFormatter


def _render(show_iter: bool) -> str:
    parsed = SimpleNamespace(
        target_type="function", target_name="foo",
        file_path="x.py", intent="show",
    )
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        OutputFormatter.render(
            parsed=parsed,
            imports=[],
            block="",
            search_result={"found": {}},
            improvement={},
            elapsed_time=1.0,
            iteration=2,
            output_config={
                "show_timing": False,
                "show_iteration_count": show_iter,
                "max_iterations": 3,
            },
        )
    finally:
        sys.stdout = old
    return buf.getvalue()


def test_iteration_count_shown_when_enabled():
    assert "iter: 2/3" in _render(True)


def test_iteration_count_hidden_when_disabled():
    # The exact key 'show_iteration_count' must be honoured; a mismatched key
    # would default to True and this assertion would fail.
    assert "iter:" not in _render(False)
