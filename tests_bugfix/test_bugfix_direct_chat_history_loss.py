"""tests_bugfix/test_bugfix_direct_chat_history_loss.py

execute_direct_chat trims chat history to the rolling cap BEFORE the
API call, then on failure calls self._direct_chat_history.pop() to
remove the just-added user message. But pop() only removes the LAST
element — the older messages that were trimmed away before the call
are permanently lost and cannot be restored.

Reproduction:
  history_max_turns = 2 (max_msgs = 4)
  history = [A_user, A_asst, B_user, B_asst]  (4 messages, at cap)
  send C_user → append → trim to [B_user, B_asst, C_user] (A lost)
  API call fails → pop() → [B_user, B_asst]  (C removed, A still lost)

Expected: [A_user, A_asst, B_user, B_asst] (pre-append state).

The fix snapshots the history before appending and restores it on
exception, so the failed turn is cleanly rolled back with no data loss.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from main import Orchestrator


def _make_orchestrator(tmp_path: Path) -> Orchestrator:
    """Build an Orchestrator with a minimal stub config so
    _build_agents does not try to reach a real LLM."""
    cfg_path = tmp_path / "agents.ini"
    cfg_path.write_text("""\
[api]
active     = local
verify_ssl = false

[api_local]
base_url   = http://localhost:99999/v1
api_key    = test
model      = test-model
api_format = openai

[loop]
max_iterations   = 3
timeout_seconds  = 10

[direct_chat]
temperature       = 0.3
history_max_turns = 2

[prompt_store]
store_path   = prompts.json
max_versions = 3
""")
    return Orchestrator(config_path=str(cfg_path))


class TestDirectChatHistoryLoss:
    def test_failed_api_call_restores_full_history(self, tmp_path, capsys):
        """After a failed API call, the history must be exactly what it
        was before the message was sent — including the messages that
        were trimmed by the cap during the append."""
        orch = _make_orchestrator(tmp_path)

        # Pre-populate history to the cap (4 messages = 2 turns)
        original = [
            {"role": "user", "content": "A user"},
            {"role": "assistant", "content": "A asst"},
            {"role": "user", "content": "B user"},
            {"role": "assistant", "content": "B asst"},
        ]
        orch._direct_chat_history = list(original)

        # Make the API call fail
        with patch("main.request_completion", side_effect=RuntimeError("API down")):
            orch.execute_direct_chat("C user")

        # The failed turn must not appear in history, AND the messages
        # that were trimmed during the append must be restored.
        assert orch._direct_chat_history == original, (
            "History after a failed API call should be the pre-append "
            f"state, got {orch._direct_chat_history}"
        )

    def test_successful_api_call_appends_both_turns(self, tmp_path, capsys):
        """Sanity: on success, the user message and the assistant reply
        are both appended to history (the fix must not break this)."""
        orch = _make_orchestrator(tmp_path)
        orch._direct_chat_history = []

        with patch("main.request_completion", return_value="reply text"):
            with patch("main.strip_think", return_value="reply text"):
                result = orch.execute_direct_chat("hello")

        assert result == "reply text"
        assert len(orch._direct_chat_history) == 2
        assert orch._direct_chat_history[0]["role"] == "user"
        assert orch._direct_chat_history[1]["role"] == "assistant"

    def test_failed_api_call_with_empty_history(self, tmp_path, capsys):
        """With empty history, a failed call should leave history empty."""
        orch = _make_orchestrator(tmp_path)
        orch._direct_chat_history = []

        with patch("main.request_completion", side_effect=RuntimeError("API down")):
            orch.execute_direct_chat("hello")

        assert orch._direct_chat_history == [], (
            f"History should be empty after failed call, got {orch._direct_chat_history}"
        )

    def test_failed_api_call_with_under_cap_history(self, tmp_path, capsys):
        """With history under the cap (no trimming needed), a failed call
        removes only the user message — nothing was lost to trimming."""
        orch = _make_orchestrator(tmp_path)
        original = [
            {"role": "user", "content": "A user"},
            {"role": "assistant", "content": "A asst"},
        ]
        orch._direct_chat_history = list(original)

        with patch("main.request_completion", side_effect=RuntimeError("API down")):
            orch.execute_direct_chat("B user")

        assert orch._direct_chat_history == original, (
            f"Under-cap history should be restored to pre-append state, "
            f"got {orch._direct_chat_history}"
        )
