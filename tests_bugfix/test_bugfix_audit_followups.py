"""Follow-ups to an external audit that called three real defects "false positives".

1. Strategy C of the prompt parser took any word that merely *occurs* in the source, so
   "what does it return" gave target "return" (a keyword) and "fix bug" gave "bug" (a comment).
2. `.format()` of a stored prompt raises TypeError for `{iteration[0]}` (int) or `{imports[x]}`
   (list); neither the agents' fallback nor the candidate validator knew about it.
3. One failing `stat()` in the search agent sent every reference of the run to not_found.
"""

import pytest

from tools.prompt_evaluator import _validate_candidate_placeholders
from tools.prompt_parser import parse_prompt

_BASE = (
    "{task} {iteration}/{max_iter} {target_block} {imports} {related_code} {missing_refs}"
)


# ---- 1. prompt parser -------------------------------------------------------------------

@pytest.mark.parametrize("prompt, source", [
    ("what does it return in app.py", "def handle(x):\n    return x + 1\n"),
    ("explain how it works in app.py", "def handle(x):\n    '''how it works: adds one'''\n    return x + 1\n"),
    ("fix bug in app.py", "def handle(x):\n    # bug: off by one\n    return x + 1\n"),
    ("fix error handling in app.py", "def h(x):\n    try:\n        return x\n    except Exception as error:\n        raise\n"),
])
def test_a_prose_word_that_only_occurs_in_the_source_is_not_the_target(prompt, source):
    assert parse_prompt(prompt, source=source).target_name == ""


@pytest.mark.parametrize("prompt, source, want", [
    ("fix bug in app.py", "def bug():\n    pass\n", "bug"),
    ("explain work in app.py", "def work():\n    pass\n", "work"),
    ("explain fail in app.py", "class fail:\n    pass\n", "fail"),
    ("explain error in app.py", "error = 1\n", "error"),
    ("explain work in app.js", "function work() {}\n", "work"),
])
def test_a_prose_word_the_source_defines_is_still_the_target(prompt, source, want):
    assert parse_prompt(prompt, source=source).target_name == want


def test_an_ordinary_symbol_is_unchanged():
    src = "def handle_request(req):\n    return req\n"
    assert parse_prompt("improve handle_request in app.py", source=src).target_name == "handle_request"


# ---- 2. TypeError in the prompt template -------------------------------------------------

@pytest.mark.parametrize("bad", [
    "{iteration[0]}", "{max_iter[0]}", "{imports[x]}", "{target_block[abc]}", "{task:d}",
])
def test_candidate_validator_rejects_a_template_that_cannot_be_formatted(bad):
    assert _validate_candidate_placeholders(_BASE + " " + bad) is not None


def test_candidate_validator_still_accepts_a_plain_template():
    assert _validate_candidate_placeholders(_BASE) is None


def _agent_with(template):
    from tools.validator_agent import ValidatorAgent
    store = type("S", (), {"get_current": lambda self, name: template})()
    return ValidatorAgent(model="m", base_url="http://fake", api_key="x",
                          timeout=5, max_iter=3, prompt_store=store)


@pytest.mark.parametrize("bad", ["{iteration[0]}", "{imports[x]}", "{target_block[abc]}"])
def test_validator_agent_falls_back_when_the_stored_prompt_raises_type_error(bad):
    import json
    from unittest.mock import patch
    agent = _agent_with(_BASE + " " + bad)
    raw = json.dumps({"status": "approved", "feedback": "", "suggested_searches": []})
    payload = {"task": "t", "iteration": 1, "target_block": "blk", "imports": ["import os"],
               "related_code": {}, "missing_refs": []}
    with patch("tools.validator_agent.request_completion", return_value=raw):
        assert agent.validate(payload)["status"] == "approved"


@pytest.mark.parametrize("bad", ["{intent[x]}", "{context_lines[0]}", "{imports[x]}"])
def test_improvement_agent_falls_back_when_the_stored_prompt_raises_type_error(bad):
    from unittest.mock import patch
    from tools.improvement_agent import ImprovementAgent
    store = type("S", (), {"get_current": lambda self, name: "{intent} {target_block} " + bad})()
    agent = ImprovementAgent(model="m", base_url="http://fake", api_key="x",
                             timeout=5, prompt_store=store)
    ctx = {"target_block": "blk", "imports": ["import os"], "related_code": {}, "context_lines": 3}
    with patch("tools.improvement_agent.request_completion", return_value="done"):
        out = agent.process("improve", ctx)
    assert isinstance(out, dict)


# ---- 3. search agent stat ---------------------------------------------------------------

def test_one_unreadable_file_does_not_discard_the_whole_search(tmp_path, monkeypatch):
    from pathlib import Path
    from tools.search_agent import SearchAgent

    (tmp_path / "a.py").write_text("def alpha():\n    pass\n")
    (tmp_path / "b.py").write_text("def beta():\n    pass\n")

    real_stat = Path.stat

    def flaky_stat(self, *a, **kw):
        if self.name == "b.py":
            raise PermissionError(13, "denied", str(self))
        return real_stat(self, *a, **kw)

    monkeypatch.setattr(Path, "stat", flaky_stat)
    out = SearchAgent().run(references=["alpha"], base_dir=str(tmp_path))
    assert "alpha" in out["found"], out
