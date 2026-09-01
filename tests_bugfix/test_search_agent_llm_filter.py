"""tests/test_search_agent_llm_filter.py

Regression guard for audit bug #4: SearchAgent._evaluate_with_llm used to be a
permanent stub that approved every discovered reference. It now performs a real
single-batch LLM noise-filter, with a strict fail-open contract (never break the
run) and a backward-compatible no-LLM mode (approve all when unconfigured).
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import tools.search_agent as SA

_REFS = {
    "my_helper": {"code": "def my_helper(): ...", "file": "a.py"},
    "os_wrapper": {"code": "import os\n", "file": "b.py"},
}


def _agent_with_llm():
    return SA.SearchAgent(model="m", base_url="http://x/v1", api_format="openai")


def test_no_llm_configured_approves_all():
    agent = SA.SearchAgent()  # no model/base_url
    assert sorted(agent._evaluate_with_llm(_REFS)) == ["my_helper", "os_wrapper"]


def test_empty_refs_returns_empty():
    assert _agent_with_llm()._evaluate_with_llm({}) == []


def test_llm_filters_to_subset(monkeypatch):
    monkeypatch.setattr(SA, "_request_completion", lambda *a, **k: '["my_helper"]')
    assert _agent_with_llm()._evaluate_with_llm(_REFS) == ["my_helper"]


def test_handles_markdown_fences(monkeypatch):
    monkeypatch.setattr(SA, "_request_completion",
                        lambda *a, **k: '```json\n["my_helper"]\n```')
    assert _agent_with_llm()._evaluate_with_llm(_REFS) == ["my_helper"]


def test_strips_think_tokens(monkeypatch):
    monkeypatch.setattr(SA, "_request_completion",
                        lambda *a, **k: '<think>reasoning</think>["my_helper"]')
    assert _agent_with_llm()._evaluate_with_llm(_REFS) == ["my_helper"]


def test_ignores_hallucinated_names(monkeypatch):
    monkeypatch.setattr(SA, "_request_completion",
                        lambda *a, **k: '["my_helper", "NOT_A_REAL_REF"]')
    # only names actually present in the input survive
    assert _agent_with_llm()._evaluate_with_llm(_REFS) == ["my_helper"]


def test_fail_open_on_network_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("conn refused")
    monkeypatch.setattr(SA, "_request_completion", boom)
    assert sorted(_agent_with_llm()._evaluate_with_llm(_REFS)) == ["my_helper", "os_wrapper"]


def test_fail_open_on_bad_json(monkeypatch):
    monkeypatch.setattr(SA, "_request_completion", lambda *a, **k: "not json")
    assert sorted(_agent_with_llm()._evaluate_with_llm(_REFS)) == ["my_helper", "os_wrapper"]


def test_fail_open_on_non_list_json(monkeypatch):
    monkeypatch.setattr(SA, "_request_completion", lambda *a, **k: '{"approved": "my_helper"}')
    assert sorted(_agent_with_llm()._evaluate_with_llm(_REFS)) == ["my_helper", "os_wrapper"]


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
