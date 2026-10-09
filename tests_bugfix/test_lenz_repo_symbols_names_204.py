"""Bug 27 (round 204): _repo_symbols missed files with a space or a non-ASCII name.

It cut `git ls-files "*.py"` output with .split(): "my module.py" became two
"files" and git C-quoted the Cyrillic name, so neither file was read and a
claim naming their code was not internal.  ls-files -z, split on NUL.
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import lenz_claim_filter as lf  # noqa: E402


def _repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "my module.py").write_text("def foo_bar_baz():\n    pass\n", encoding="utf-8")
    (tmp_path / "ф.py").write_text("def cyr_function():\n    pass\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    return tmp_path


def test_names_with_a_space_and_a_cyrillic_letter_are_read(tmp_path):
    symbols = lf._repo_symbols(_repo(tmp_path))
    assert {"foo_bar_baz", "cyr_function", "my module"} <= symbols


def test_a_claim_naming_their_code_is_internal(tmp_path):
    symbols = lf._repo_symbols(_repo(tmp_path))
    assert lf.is_internal("`foo_bar_baz` returns None when pytest passes.", symbols)
    assert lf.is_internal("cyr_function raises when pytest is missing.", symbols)
