"""tests_bugfix/test_llm_stream_monkeypatch_isolation.py

Regression guard for the bare-module-assignment test-isolation leak in
tests/test_cr30_validator_sees_prior.py and
tests_bugfix/test_cr31_revalidate_once.py.

Bug
---
Both files patched tools.llm_stream by assigning directly onto the module
alias (``ls.request_completion = rc``).  A bare assignment is never undone:
under pytest-xdist, which reuses one worker process for many test files,
the stub remained installed on tools.llm_stream for every test that ran
afterward in that same worker.  The leak is not theoretical — reproduced
directly on pullv3 HEAD: running the cr30+cr31 files then importing
tools.llm_stream in the same process showed request_completion still bound
to test_clear_first_reply_no_retry.<locals>.rc instead of the real function.

Why the mechanism is real
-------------------------
tools.auto.inner_loop.LLMGate2Validator.approve() does a deferred import
inside its method body::

    from tools.llm_stream import request_completion, strip_think

Because ``ls`` IS ``sys.modules['tools.llm_stream']`` (same object), patching
``ls.request_completion`` patches the attribute the deferred import reads at
call time — confirmed by direct experimentation:

    before patch: REAL_RESPONSE
    after bare assignment: LEAKED_STUB           # same worker, next test

Fix
---
Both files now use ``monkeypatch.setattr(ls, name, stub)``, which pytest
unconditionally reverts when the test ends (pass, fail, or error), regardless
of worker / process reuse.
"""
from __future__ import annotations

import re
from pathlib import Path

import tools.llm_stream as ls
from tools.auto.inner_loop import LLMGate2Validator

_REPO_ROOT = Path(__file__).resolve().parent.parent

# Pattern that matches the exact shape of the old bug:
# a bare assignment onto the module-alias variable.
_BARE_ASSIGN_RE = re.compile(
    r"^\s*ls\.(request_completion|strip_think|ollama_chat_url)\s*=",
    re.MULTILINE,
)

_FIXED_FILES = [
    _REPO_ROOT / "tests" / "test_cr30_validator_sees_prior.py",
    _REPO_ROOT / "tests_bugfix" / "test_cr31_revalidate_once.py",
]


def test_fixed_files_no_longer_use_bare_module_assignment():
    """Prevent silent regression: neither fixed file may go back to
    assigning directly onto the tools.llm_stream module alias instead
    of routing through monkeypatch.setattr.
    """
    offenders = [
        str(p.relative_to(_REPO_ROOT))
        for p in _FIXED_FILES
        if _BARE_ASSIGN_RE.search(p.read_text(encoding="utf-8"))
    ]
    assert not offenders, (
        "bare `ls.<attr> = ...` module assignment reintroduced in: "
        + ", ".join(offenders)
    )


def test_monkeypatch_setattr_restores_llm_stream_after_test_ends(monkeypatch):
    """Positive proof that monkeypatch.setattr actually restores the real
    function once the patch's scope ends — the property the old bare
    assignment never had.

    Uses monkeypatch.context() to simulate one test's lifetime: the patch
    is active inside, pytest tears it down at the ``with`` block's exit
    (standing in for "the test function returns or raises").
    """
    real_rc = ls.request_completion
    real_st = ls.strip_think
    real_ocu = ls.ollama_chat_url

    class _R:
        exit_code = 0
        stdout = ""
        stderr = ""

    with monkeypatch.context() as m:
        m.setattr(ls, "request_completion", lambda **k: "APPROVED")
        m.setattr(ls, "strip_think", lambda x: x)
        m.setattr(ls, "ollama_chat_url", lambda u: u)

        v = object.__new__(LLMGate2Validator)
        for attr, val in dict(
            task_mode="creative", api_format="ollama", base_url="http://x",
            api_key="k", model="m", ssl_context=None, temperature=0.1,
            max_tokens=512, timeout=30, num_ctx=8192, _system="SYS",
        ).items():
            setattr(v, attr, val)
        v._read_changed_content = lambda *a, **k: "Глава: текст"

        approved, _ = v.approve({"instruction": "x"}, _R(), None)
        assert approved is True
        assert ls.request_completion is not real_rc  # stub is live inside

    # Once the context exits the real callables must be restored — exactly
    # what the bare assignment never guaranteed.
    assert ls.request_completion is real_rc
    assert ls.strip_think is real_st
    assert ls.ollama_chat_url is real_ocu
