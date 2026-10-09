"""tests_bugfix/test_lenz_repo_symbols_names_219.py — bug 219: `_repo_symbols` does not know the code of a file whose name has a space or a non-ASCII letter.

Bug: `_repo_symbols` lists the repo's code with `git ls-files "*.py"` and cuts the output
with `.split()` — on whitespace, so `my module.py` became the two "files" `my` and
`module.py`, and git QUOTES a name holding a non-ASCII byte (`"\\321\\204.py"`), which no
file is called. Neither file was read, so a claim naming a function in it was not
`needs_code`: `claim_vote` is told "never accept votes on a claim about our own code",
and was handed one to accept. The list is read with `-z`, NUL-separated and raw.
"""

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import lenz_claim_filter as lf  # noqa: E402

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}


def _repo(tmp_path, files):
    root = tmp_path / "repo"
    root.mkdir()
    for name, code in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(code, encoding="utf-8")
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "x"]):
        subprocess.run(["git", *args], cwd=root, env=ENV, check=True, capture_output=True)
    return root


def test_the_functions_of_oddly_named_files_are_known(tmp_path):
    root = _repo(tmp_path, {
        "plain.py": "def plain_function_name():\n    pass\n",
        "my module.py": "def spaced_function_name():\n    pass\n",
        "файл.py": "class CyrillicClassName:\n    pass\n",
        "dir with space/inner.py": "def nested_function_name():\n    pass\n",
    })
    symbols = lf._repo_symbols(root)
    assert {"plain_function_name", "spaced_function_name", "CyrillicClassName",
            "nested_function_name"} <= symbols


def test_a_claim_about_such_a_function_is_about_our_code(tmp_path):
    root = _repo(tmp_path, {"my module.py": "def spaced_function_name():\n    pass\n"})
    symbols = lf._repo_symbols(root)
    assert lf.is_internal("The `spaced_function_name` call never returns a value at all.", symbols)


def test_the_pieces_of_a_split_name_are_no_symbols(tmp_path):
    root = _repo(tmp_path, {"my module.py": "x = 1\n"})
    assert "module" not in lf._repo_symbols(root)


def test_a_directory_that_is_no_repo_has_no_symbols(tmp_path):
    assert lf._repo_symbols(tmp_path) == set()
