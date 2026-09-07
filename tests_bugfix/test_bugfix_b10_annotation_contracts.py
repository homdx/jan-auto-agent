"""B10 -- four annotations that contradicted the actual runtime behaviour.

* ``mask_api_key(text: str) -> str`` and ``strip_think(text: str) -> str``
  both return *text* unchanged when it is falsy, so both return ``None`` for
  a ``None`` input. Callers pass the raw model reply, which can be ``None``.
* ``Orchestrator.execute_direct_chat(...) -> None`` returns the reply string
  on the success path and falls off the end of its ``except`` branch.
* ``TicketStore._read(path) -> dict`` is ``json.loads``: it returns whatever
  the file holds.

These are asserted through *behaviour*, not by reading ``__annotations__``.
An annotation-string test catches a typo in the annotation but not a drift
between annotation and runtime -- which is the entire bug class B10 is
about. ``_read`` is annotated ``Any`` rather than ``dict | list`` because
narrowing to two container types would still be a lie about scalars and
null; the tests below pin all four shapes.

Behaviour is unchanged by this patch, so these tests pass on the base commit
too. They are the executable version of the contract the annotations now
state -- they fail the moment someone "tidies" one of these signatures back
to a narrower type and adjusts the code to match.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.ticket_store import TicketStore  # noqa: E402
from tools.llm_stream import mask_api_key, strip_think  # noqa: E402


class TestMaskApiKey:
    def test_none_returns_none(self) -> None:
        assert mask_api_key(None) is None

    def test_empty_string_returns_empty_string(self) -> None:
        assert mask_api_key("") == ""

    def test_masking_still_works(self) -> None:
        assert "here_your_key" in mask_api_key("api_key = sk-secret-value")


class TestStripThink:
    def test_none_returns_none(self) -> None:
        assert strip_think(None) is None

    def test_empty_string_returns_empty_string(self) -> None:
        assert strip_think("") == ""

    def test_stripping_still_works(self) -> None:
        assert "reasoning" not in strip_think("<think>reasoning</think>answer")


class TestTicketStoreRead:
    @pytest.mark.parametrize(
        "payload, expected_type",
        [
            ({"id": "T-1"}, dict),
            ([1, 2, 3], list),
            ("a bare string", str),
            (7, int),
        ],
    )
    def test_read_returns_whatever_the_file_holds(
        self, tmp_path: Path, payload, expected_type
    ) -> None:
        path = tmp_path / "ticket.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        assert isinstance(TicketStore._read(path), expected_type)

    def test_read_returns_none_for_json_null(self, tmp_path: Path) -> None:
        """The case 'dict | list' would still have got wrong."""
        path = tmp_path / "ticket.json"
        path.write_text("null", encoding="utf-8")
        assert TicketStore._read(path) is None


class TestExecuteDirectChat:
    """The success branch returns a str; the failure branch returns None.

    Annotated ``-> None``, that difference was invisible to every caller.
    """

    def _orchestrator(self, monkeypatch):
        import main

        orch = main.Orchestrator.__new__(main.Orchestrator)
        orch._direct_chat_history = []
        orch.base_url = "http://localhost:1/v1"
        orch.api_key = "test"
        orch.api_format = "openai"
        orch.model = "test-model"
        orch.timeout_seconds = 1
        orch.ssl_context = None
        monkeypatch.setattr(
            main.Orchestrator, "_getfloat",
            lambda self, *a, **kw: 0.4, raising=False,
        )
        monkeypatch.setattr(
            main.Orchestrator, "_getint",
            lambda self, *a, **kw: 10, raising=False,
        )
        return main, orch

    def test_failure_path_returns_none(self, monkeypatch, capsys) -> None:
        main, orch = self._orchestrator(monkeypatch)

        def boom(*args, **kwargs):
            raise RuntimeError("upstream is down")

        monkeypatch.setattr(main, "request_completion", boom)
        assert orch.execute_direct_chat("hello") is None

    def test_failure_path_restores_history(self, monkeypatch, capsys) -> None:
        main, orch = self._orchestrator(monkeypatch)
        monkeypatch.setattr(
            main, "request_completion",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("down")),
        )
        orch.execute_direct_chat("hello")
        assert orch._direct_chat_history == []

    def test_success_path_returns_the_reply_string(
        self, monkeypatch, capsys
    ) -> None:
        main, orch = self._orchestrator(monkeypatch)
        monkeypatch.setattr(
            main, "request_completion", lambda *a, **kw: "the answer"
        )
        assert orch.execute_direct_chat("hello") == "the answer"
