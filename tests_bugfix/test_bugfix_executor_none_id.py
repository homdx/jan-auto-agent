"""tests/test_bugfix_executor_none_id.py

Bug: `Executor.run()` read the task id with `task.get("id", "").strip()`.
The `""` default only covers a MISSING 'id' key — a task dict with an
explicit `"id": None` made `.get()` return `None`, and `None.strip()`
raised a raw, undocumented `AttributeError` instead of the `ValueError`
the docstring promises for a bad id. The sibling `acceptance_check` read
right below it already used `(x or "").strip()` for exactly this reason.
"""
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.executor import Executor


def test_explicit_none_id_raises_documented_value_error(tmp_path):
    executor = Executor(base_dir=tmp_path)
    task = {"id": None, "acceptance_check": "true", "target_files": []}
    with pytest.raises(ValueError):
        executor.run(task)


def test_missing_id_key_raises_documented_value_error(tmp_path):
    executor = Executor(base_dir=tmp_path)
    task = {"acceptance_check": "true", "target_files": []}
    with pytest.raises(ValueError):
        executor.run(task)


def test_empty_string_id_raises_documented_value_error(tmp_path):
    executor = Executor(base_dir=tmp_path)
    task = {"id": "", "acceptance_check": "true", "target_files": []}
    with pytest.raises(ValueError):
        executor.run(task)
