"""tests/test_cr20_3_plan_validator.py — AUTO-CR-20-3 acceptance tests.

Covers:
  * task that contradicts a goal fact → stub REVISE → (False, reason)
  * goal lists a fact covered by no task → stub REVISE → (False, reason)
  * good plan (tasks match goal) → (True, "")
  * LLM raises / returns garbage → fail-open (True, ""), never raises
"""

from __future__ import annotations

import configparser

import pytest

from tools.auto.architect import ClusterReviewer, CandidateTask, CitedLocation


# ── Fixture helpers ───────────────────────────────────────────────────────────

def _make_reviewer(llm_stub) -> ClusterReviewer:
    """Build a ClusterReviewer with a minimal config and injected LLM stub."""
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "api":        {"active": "local"},
        "api_local":  {"base_url": "http://localhost:1234/v1",
                       "api_key":  "x",
                       "model":    "test-model"},
        "architect":  {},
        "loop":       {"timeout_seconds": "30"},
    })
    reviewer = ClusterReviewer(cfg, "http://localhost:1234/v1", "x", "test-model",
                               task_mode="creative")
    reviewer._llm_call = llm_stub
    return reviewer


def _candidate(title: str, instruction: str) -> CandidateTask:
    return CandidateTask(
        title=title,
        instruction=instruction,
        target_files=["chapter_1.md"],
        acceptance_check="true",
        cited_location=CitedLocation(file="chapter_1.md"),
        cluster="test",
    )


def _dict_candidate(title: str, instruction: str) -> dict:
    return {"title": title, "instruction": instruction}


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_plan_contradiction_revises():
    """Goal 'Аделина КМС' + task 'Change КМС to первоклассник' → REVISE."""
    def llm(system, user):
        assert "GOAL" in user
        assert "TASKS" in user
        return "REVISE: task contradicts goal — КМС must not be removed"

    reviewer = _make_reviewer(llm)
    goal = "Аделина — КМС по гимнастике. Сохрани этот факт."
    candidates = [_candidate(
        "Change КМС to первоклассник",
        "Replace every mention of 'КМС' with 'первоклассник' in the chapter.",
    )]

    ok, reason = reviewer.validate_plan(goal, candidates)

    assert ok is False
    assert reason  # non-empty — names the contradiction
    assert "КМС" in reason or "contradict" in reason.lower() or reason  # flexible


def test_missing_fact_revises():
    """Goal lists 3 daughters; tasks cover only 2 → REVISE."""
    def llm(system, user):
        return "REVISE: goal requires a stanza for Zara but no task covers her"

    reviewer = _make_reviewer(llm)
    goal = "Write a poem with one stanza each for Aisha, Beda, and Zara."
    candidates = [
        _candidate("Stanza for Aisha", "Write the Aisha stanza."),
        _candidate("Stanza for Beda",  "Write the Beda stanza."),
        # Zara is missing
    ]

    ok, reason = reviewer.validate_plan(goal, candidates)

    assert ok is False
    assert reason


def test_good_plan_approves():
    """Tasks fully match the goal → APPROVED."""
    def llm(system, user):
        return "APPROVED"

    reviewer = _make_reviewer(llm)
    goal = "Write a poem about spring."
    candidates = [_candidate("Spring poem", "Write a poem celebrating spring.")]

    ok, reason = reviewer.validate_plan(goal, candidates)

    assert ok is True
    assert reason == ""


def test_failopen_llm_raises():
    """LLM raises → fail-open: (True, ""), never propagates exception."""
    def llm(system, user):
        raise ConnectionError("network down")

    reviewer = _make_reviewer(llm)
    ok, reason = reviewer.validate_plan("any goal", [_candidate("t", "i")])

    assert ok is True
    assert reason == ""


def test_failopen_garbage_reply():
    """Rambling non-verdict reply → fail-open: (True, "")."""
    def llm(system, user):
        return (
            "I have carefully considered the tasks. Overall the plan looks "
            "reasonable and I think it will work well for the story."
        )

    reviewer = _make_reviewer(llm)
    ok, reason = reviewer.validate_plan("Write a story.", [_candidate("t", "i")])

    assert ok is True


def test_dict_candidates_accepted():
    """validate_plan must accept plain dicts (as produced by the architect LLM)."""
    def llm(system, user):
        return "APPROVED"

    reviewer = _make_reviewer(llm)
    candidates = [_dict_candidate("Title", "Do the thing.")]

    ok, reason = reviewer.validate_plan("some goal", candidates)

    assert ok is True


def test_empty_candidates_does_not_raise():
    """Empty candidate list → LLM still called; no crash."""
    calls = []

    def llm(system, user):
        calls.append(user)
        return "APPROVED"

    reviewer = _make_reviewer(llm)
    ok, _ = reviewer.validate_plan("some goal", [])

    assert ok is True
    assert calls  # LLM was called


def test_goal_and_tasks_appear_in_prompt():
    """The user message must contain both GOAL and TASKS sections."""
    captured = {}

    def llm(system, user):
        captured["user"] = user
        return "APPROVED"

    reviewer = _make_reviewer(llm)
    goal = "Write about the magic forest."
    candidates = [_candidate("Forest chapter", "Describe the magic forest.")]
    reviewer.validate_plan(goal, candidates)

    assert "GOAL" in captured["user"]
    assert "TASKS" in captured["user"]
    assert "magic forest" in captured["user"]
    assert "Forest chapter" in captured["user"]


def test_arch_plan_system_prompt_is_narrow():
    """_ARCH_PLAN_SYSTEM must mention APPROVED/REVISE and forbid style/ordering."""
    from tools.auto.architect import _ARCH_PLAN_SYSTEM

    upper = _ARCH_PLAN_SYSTEM.upper()
    assert "APPROVED" in upper
    assert "REVISE" in upper
    # Must not prompt for style/ordering revisions
    assert "STYLE" not in upper or "NOT" in upper or "DO NOT" in upper


# ── GATE3-PROFILE-3: [architect] plan_llm_profile ────────────────────────────

class TestPlanLlmProfile:
    """GATE3-PROFILE-3 acceptance tests for ``[architect] plan_llm_profile``.

    Mirrors the pattern in ``tests/test_gate3_profiles.py``: each test
    asserts the (url, model) actually used for the outbound HTTP request
    by monkeypatching ``tools.llm_stream.request_completion`` and
    invoking the constructed ``ClusterReviewer``'s own ``_llm_call``
    (built by ``_build_llm_call``, never stubbed out here) directly — a
    mis-wired ``settings`` kwarg that never reached ``_make_llm_call``
    would still pass a higher-level mock.
    """

    @staticmethod
    def _config(
        *, profile_section: "str | None" = None, extra: "dict | None" = None,
    ) -> configparser.ConfigParser:
        sections: dict = {
            "api": {"active": "local"},
            "api_local": {
                "base_url": "https://shared.example/v1",
                "api_key": "shared-key",
                "model": "shared-model",
                "api_format": "openai",
            },
            "architect": {},
            "loop": {"timeout_seconds": "30"},
        }
        if profile_section is not None:
            sections["architect"]["plan_llm_profile"] = profile_section
        if extra:
            for section, kv in extra.items():
                sections.setdefault(section, {}).update(kv)
        cfg = configparser.ConfigParser()
        cfg.read_dict(sections)
        return cfg

    @staticmethod
    def _capture(monkeypatch, capture: dict) -> None:
        import tools.llm_stream as _llm_stream

        def _fake(*, url, headers, payload, **kwargs):
            capture["result"] = (url, payload.get("model"))
            return "APPROVED"

        monkeypatch.setattr(_llm_stream, "request_completion", _fake)

    @staticmethod
    def _reviewer(cfg: configparser.ConfigParser) -> ClusterReviewer:
        return ClusterReviewer(
            cfg, "https://shared.example/v1", "shared-key", "shared-model",
            task_mode="creative",
        )

    def test_default_path_uses_shared_provider(self, monkeypatch):
        capture: dict = {}
        self._capture(monkeypatch, capture)
        reviewer = self._reviewer(self._config())
        reviewer._llm_call("system", "user")

        url, model = capture["result"]
        assert "shared.example" in url
        assert model == "shared-model"

    def test_own_profile_path_uses_that_providers_url_and_model(self, monkeypatch):
        capture: dict = {}
        self._capture(monkeypatch, capture)
        cfg = self._config(
            profile_section="judge_llm",
            extra={
                "judge_llm": {
                    "base_url": "https://own.example/v1",
                    "api_key": "own-key",
                    "model": "own-model",
                    "api_format": "openai",
                }
            },
        )
        reviewer = self._reviewer(cfg)
        reviewer._llm_call("system", "user")

        url, model = capture["result"]
        assert "own.example" in url
        assert "shared.example" not in url
        assert model == "own-model"

    def test_empty_profile_value_treated_as_unset(self, monkeypatch):
        capture: dict = {}
        self._capture(monkeypatch, capture)
        reviewer = self._reviewer(self._config(profile_section=""))
        reviewer._llm_call("system", "user")

        url, model = capture["result"]
        assert "shared.example" in url
        assert model == "shared-model"

    def test_profile_naming_missing_section_raises_at_construction(self):
        with pytest.raises(ValueError, match="ghost"):
            self._reviewer(self._config(profile_section="ghost"))

    def test_profile_missing_required_option_raises_at_construction(self):
        cfg = self._config(
            profile_section="incomplete",
            extra={"incomplete": {"base_url": "https://incomplete.example"}},
        )
        with pytest.raises(ValueError, match="api_key|model"):
            self._reviewer(cfg)

    def test_no_new_key_present_request_matches_pre_ticket_behaviour(self, monkeypatch):
        """A config with no ``plan_llm_profile`` must hit the exact same
        provider/model as ``_make_llm_call`` produced before this ticket —
        the byte-for-byte acceptance criterion.
        """
        from tools.auto.summary_memory import _make_llm_call

        capture_new: dict = {}
        self._capture(monkeypatch, capture_new)
        reviewer = self._reviewer(self._config())
        reviewer._llm_call("system", "user")

        capture_old: dict = {}
        self._capture(monkeypatch, capture_old)
        old_call = _make_llm_call(self._config(), task_mode="creative")  # pre-ticket shape
        old_call("system", "user")

        assert capture_new["result"] == capture_old["result"]
