"""FIX-2 #13 — extensionless files were asserted to be Python.

``Path(loc.file).suffix or ".py"`` gave every extensionless path a ``.py``
extension, so ``Makefile``, ``Dockerfile``, ``Jenkinsfile``, ``.gitignore``
and friends were handed to ``block_extractor``'s AST-based Python strategy
and to ``extract_module_docstring`` as if they were Python modules.

An empty extension is the honest answer: ``block_extractor`` then assumes no
language and uses its language-neutral brace search, and
``extract_module_docstring`` returns "" instead of parsing a non-Python file
as Python.

The report named ``gate1_filter.py:879``. The identical expression existed
at two further sites — ``gate1_filter.py:982`` (module-docstring context)
and ``gate1_grounding.py:120`` (``target_file_context``) — feeding the same
two extractors. All three are covered here; the repo now contains no
remaining instance of the pattern, which
``test_no_remaining_py_default_in_the_tree`` pins.
"""

from __future__ import annotations

import configparser
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.architect import CandidateTask, CitedLocation  # noqa: E402
from tools.auto.gate1_filter import Gate1Filter  # noqa: E402
from tools.auto.gate1_grounding import target_file_context  # noqa: E402

# A brace-delimited definition in an extensionless file — the shape the
# language-neutral extractor handles and the Python one cannot.
JENKINSFILE = """\
// SENTINEL_TOP_OF_FILE
pipeline {
    agent any
    stages {
        stage('build') { sh 'make UNIQUE_MARKER' }
    }
}
"""

# Valid Python whose first statement is a string literal. Read as Python this
# looks like a module docstring; it is a plain text file.
TEXTFILE = '''\
"""not actually a docstring — this file is not Python"""
'''

PY_MODULE = '''\
"""A real module docstring."""


def build():
    return 1
'''


@pytest.fixture()
def filt() -> Gate1Filter:
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "api":       {"active": "local", "verify_ssl": "false"},
        "api_local": {
            "base_url": "http://localhost:1337/v1", "api_key": "test",
            "model": "test-model", "api_format": "openai",
        },
        "gate1": {"temperature": "0.0", "max_tokens": "512", "skip_llm": "false"},
        "loop":  {"timeout_seconds": "10"},
    })
    return Gate1Filter(
        config=cfg, base_url="http://localhost:1337/v1", api_key="test",
        model="test-model", api_format="openai", verify_ssl=False,
    )


def _candidate(file: str, symbol: str | None = None) -> CandidateTask:
    return CandidateTask(
        title="t",
        instruction="i",
        target_files=[file],
        acceptance_check="true",
        cited_location=CitedLocation(file=file, symbol=symbol),
        cluster="c",
    )


def _write(root: Path, rel: str, src: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(src, encoding="utf-8")
    return p


# ── site 1: gate1_filter._check_existence (the reported line) ────────────────

class TestExistenceCheck:
    def test_extensionless_brace_file_symbol_resolves(
        self, filt: Gate1Filter, tmp_path: Path
    ) -> None:
        """With the .py default the Python extractor found nothing here and
        the candidate was rejected for a reason that never applied."""
        _write(tmp_path, "Jenkinsfile", JENKINSFILE)
        ok, reason, block = filt._check_existence(
            _candidate("Jenkinsfile", "stage"), tmp_path, cluster_files=None
        )
        assert ok, reason
        assert block

    def test_python_file_is_unaffected(
        self, filt: Gate1Filter, tmp_path: Path
    ) -> None:
        _write(tmp_path, "pkg/mod.py", PY_MODULE)
        ok, reason, block = filt._check_existence(
            _candidate("pkg/mod.py", "build"), tmp_path, cluster_files=None
        )
        assert ok, reason
        assert "def build" in block

    def test_missing_symbol_in_extensionless_file_still_rejected(
        self, filt: Gate1Filter, tmp_path: Path
    ) -> None:
        """Dropping the default must not turn into "accept anything"."""
        _write(tmp_path, "Jenkinsfile", JENKINSFILE)
        ok, _reason, _block = filt._check_existence(
            _candidate("Jenkinsfile", "no_such_stage"), tmp_path, cluster_files=None
        )
        assert ok is False


# ── site 2: gate1_filter._module_docstring_for ───────────────────────────────

class TestModuleDocstringContext:
    def test_extensionless_file_yields_no_docstring(
        self, filt: Gate1Filter, tmp_path: Path
    ) -> None:
        """A leading string literal in a non-Python file is not a module
        docstring; with the .py default it was extracted as one and injected
        into Stage B's prompt as context about a Python module."""
        _write(tmp_path, "NOTES", TEXTFILE)
        assert filt._module_docstring_for(_candidate("NOTES"), tmp_path) == ""

    def test_python_module_docstring_still_extracted(
        self, filt: Gate1Filter, tmp_path: Path
    ) -> None:
        _write(tmp_path, "pkg/mod.py", PY_MODULE)
        doc = filt._module_docstring_for(_candidate("pkg/mod.py"), tmp_path)
        assert "A real module docstring." in doc

    def test_missing_file_is_still_silent(
        self, filt: Gate1Filter, tmp_path: Path
    ) -> None:
        assert filt._module_docstring_for(_candidate("ghost"), tmp_path) == ""


# ── site 3: gate1_grounding.target_file_context (unreported) ─────────────────

class TestTargetFileContext:
    def test_extensionless_target_file_resolves_its_symbol(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path, "Jenkinsfile", JENKINSFILE)
        _write(tmp_path, "pkg/mod.py", PY_MODULE)
        note = target_file_context(
            target_files=["Jenkinsfile"],
            cited_file="pkg/mod.py",
            cited_symbol="stage",
            instruction="the stage block is wrong",
            base_dir=tmp_path,
        )
        assert note is not None
        # Asserting "stage" alone would pass either way: with the .py default
        # extract_block returns nothing and target_file_context falls back to
        # a head-of-file slice that happens to contain the word. Pin the
        # extracted block itself instead — the fallback carries the file's
        # first line, the real block does not.
        assert "UNIQUE_MARKER" in note
        assert "SENTINEL_TOP_OF_FILE" not in note

    def test_python_target_file_is_unaffected(self, tmp_path: Path) -> None:
        _write(tmp_path, "pkg/other.py", PY_MODULE)
        note = target_file_context(
            target_files=["pkg/other.py"],
            cited_file="pkg/mod.py",
            cited_symbol="build",
            instruction="build() is wrong",
            base_dir=tmp_path,
        )
        assert note is not None
        assert "def build" in note


# ── the pattern must not come back ───────────────────────────────────────────

def test_no_remaining_py_default_in_the_tree() -> None:
    """The report named one line; three carried the same expression. Pin the
    absence so a fourth cannot be introduced silently."""
    offenders = []
    for path in (PROJECT_ROOT / "tools").rglob("*.py"):
        for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if "suffix or" in line and '".py"' in line:
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{n}")
    assert offenders == [], f"`suffix or \".py\"` reintroduced at: {offenders}"
