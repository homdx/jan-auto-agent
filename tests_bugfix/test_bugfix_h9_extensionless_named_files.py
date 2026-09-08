"""H9 — the extensionless filenames the report actually named.

H9 on the pullv3 list is ``Path(loc.file).suffix or ".py"`` in gate1_filter:
an extensionless file was handed to block_extractor's AST-based Python
strategy and to extract_module_docstring as if it were a Python module.

FIX-2 #13 (bee47ef) removed the default at all three sites that carried it,
and :mod:`tests_bugfix.test_bugfix_fix2_13_extensionless_not_python` pins
the behaviour using ``Jenkinsfile`` and ``NOTES``. This module pins the
exact filenames the report and every part3 candidate patch named —
``Makefile``, ``Dockerfile`` and the dotfile ``.gitignore`` — because a
dotfile is the one shape where "has no extension" is easy to get wrong:
``Path(".gitignore").suffix`` is ``""``, not ``".gitignore"``, so a
name-based mapping that keys off the suffix silently misses it.

The plan's own remedy was a mapping table (``Makefile`` -> make,
``Dockerfile`` -> docker). That is not what shipped, and does not need to
be: block_extractor branches on ``.py`` and ``.go`` only, so a synthetic
``".make"`` and an empty string take the same language-neutral path. The
empty extension is the honest one — it asserts nothing about a file whose
path claims nothing.
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

MAKEFILE = '''\
"""not a docstring — just a quoted line at the top of a Makefile"""
SENTINEL_TOP_OF_FILE = 1

build:
	echo UNIQUE_MARKER
'''

DOCKERFILE = '''\
FROM python:3.11-slim
RUN echo UNIQUE_MARKER
'''

GITIGNORE = '''\
"""leading string literal, still not a Python module docstring"""
__pycache__/
*.pyc
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


@pytest.mark.parametrize("name", ["Makefile", "Dockerfile", ".gitignore"])
def test_suffix_of_a_named_extensionless_file_is_empty(name: str) -> None:
    """The premise the fix rests on, pinned so a future refactor cannot
    assume a dotfile's name is its suffix."""
    assert Path(name).suffix == ""


@pytest.mark.parametrize(
    "name,content",
    [("Makefile", MAKEFILE), (".gitignore", GITIGNORE)],
)
def test_named_extensionless_file_yields_no_module_docstring(
    filt: Gate1Filter, tmp_path: Path, name: str, content: str
) -> None:
    """A leading string literal is not a module docstring here. With the
    .py default it was extracted as one and injected into Stage B's prompt
    as a fact about a Python module that does not exist."""
    (tmp_path / name).write_text(content, encoding="utf-8")
    assert filt._module_docstring_for(_candidate(name), tmp_path) == ""


def test_dockerfile_citation_is_not_rejected_as_a_python_file(
    filt: Gate1Filter, tmp_path: Path
) -> None:
    """A Dockerfile has no Python symbols at all; citing one by file alone
    must not fail because an AST parse found nothing."""
    (tmp_path / "Dockerfile").write_text(DOCKERFILE, encoding="utf-8")
    ok, reason, _block = filt._check_existence(
        _candidate("Dockerfile"), tmp_path, cluster_files=None
    )
    assert ok, reason


def test_missing_symbol_in_a_makefile_is_still_rejected(
    filt: Gate1Filter, tmp_path: Path
) -> None:
    """Dropping the default must not become "accept anything"."""
    (tmp_path / "Makefile").write_text(MAKEFILE, encoding="utf-8")
    ok, _reason, _block = filt._check_existence(
        _candidate("Makefile", "no_such_target"), tmp_path, cluster_files=None
    )
    assert ok is False
