"""tests_bugfix/test_harden1_a7_fence_inside_json_string.py — HARDEN-1 A7: a
reply whose JSON string contains a code fence parses.

A valid reply quoting a code sample used to be discarded as unparseable,
because every fence-stripping site cut at the first `` ``` `` it saw — which,
when the fence is *inside a string*, is the fence the reply is quoting:

| call | before | now |
|---|---|---|
| ``strip_json_fence(bare)`` | ``JSONDecodeError`` | the object |
| ``strip_json_fence("```json\\n" + bare + "\\n```")`` | parses (fix 36) | the object |
| ``_parse_llm_summary(bare)`` | ``('', '')`` | the summary |
| Gate 2 (bare or wrapped) | ``approved=False unavailable=True`` | the verdict |

That last row is the expensive one: an APPROVED verdict read as "validator
unavailable" made ``InnerLoop`` re-run Gate 2 ``validator_unavailable_retries``
times with a 60 s wait, then reset the task to TODO and **discard the passing
candidate**; a rejection whose hint quotes a code sample never reached the
Coder. The whole reply is now tried as JSON first and only then stripped of a
fence, and Gate 2 goes through ``strip_json_fence`` instead of its own inline
``raw.split("```json")[1].split("```")[0]``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.llm_stream import strip_json_fence  # noqa: E402

# The reply the ticket reproduced, in both ways a model can write it:
# a fenced code block inside a JSON string, escaped (\n) or as a literal newline.
BARE = '{"purpose": "Retry helper.", "notes": "Typical use:\\n```python\\nretry(fetch, 3)\\n```"}'
BARE_RAW = '{"purpose": "Retry helper.", "notes": "Typical use:\n```python\nretry(fetch, 3)\n```"}'
WRAPPED = "```json\n" + BARE + "\n```"
WRAPPED_RAW = "```json\n" + BARE_RAW + "\n```"

EXPECTED_NOTES = 'Typical use:\n```python\nretry(fetch, 3)\n```'


class TestFenceInsideAString:
    @pytest.mark.parametrize("text", [BARE, BARE_RAW, WRAPPED, WRAPPED_RAW])
    def test_the_object_is_returned(self, text):
        parsed = json.loads(strip_json_fence(text))
        assert parsed["purpose"] == "Retry helper."
        assert parsed["notes"] == EXPECTED_NOTES

    def test_the_body_is_plain_json_even_with_literal_newlines(self):
        """`_escape_raw_controls` only runs when the body needs it, so every
        caller's plain `json.loads` still works on a raw-newline reply."""
        for text in (BARE, BARE_RAW, WRAPPED, WRAPPED_RAW):
            json.loads(strip_json_fence(text), strict=True)


class TestTheSixReplyShapes:
    def test_a_bare_json_reply(self):
        assert strip_json_fence('{"a": 1}') == '{"a": 1}'

    def test_a_wrapped_json_reply(self):
        out = strip_json_fence('```json\n{"a": 1}\n```')
        assert out == '{"a": 1}'

    def test_prose_plus_a_block(self):
        out = strip_json_fence('Here you go:\n```json\n{"a": 1}\n```\nDone!')
        assert out == '{"a": 1}'

    def test_an_unclosed_fence(self):
        assert json.loads(strip_json_fence('```json\n{"a": 1}')) == {"a": 1}

    def test_a_plain_fence_with_any_label(self):
        assert json.loads(strip_json_fence('```python\n{"a": 1}\n```')) == {"a": 1}

    def test_no_fence_at_all(self):
        assert strip_json_fence('{"a": 1}') == '{"a": 1}'
        assert strip_json_fence("sorry, I cannot help") == "sorry, I cannot help"


class TestTheOldBehaviourThatStillHolds:
    """Every previously-working path is byte-identical."""

    @pytest.mark.parametrize("text, expected", [
        ('```json\n{"result": "ok"}\n```', '{"result": "ok"}'),
        ('```\n{"result": "ok"}\n```', '{"result": "ok"}'),
        ('```JSON\n{"Key": "Value"}\n```', '{"Key": "Value"}'),
        ('```json\n{"a": 1}\n````', '{"a": 1}'),
    ])
    def test_closed_fences_are_unchanged(self, text, expected):
        assert strip_json_fence(text) == expected

    def test_text_after_the_closing_fence_is_dropped(self):
        assert strip_json_fence('```python\n{"a": 1}\n```\nDone!') == '{"a": 1}'

    def test_a_truncated_body_still_fails_cleanly(self):
        with pytest.raises(json.JSONDecodeError):
            json.loads(strip_json_fence('```python\n{"a":'))


# ── Gate 2 itself, with the model call stubbed ─────────────────────────────────

TASK = {"id": "AUTO-T1", "title": "fix it", "instruction": "do x",
        "target_files": ["f.py"], "acceptance_check": "pytest -q"}


def _exec_result():
    from types import SimpleNamespace
    return SimpleNamespace(passed=True, exit_code=0, stdout="ok", stderr="", traceback="",
                           timed_out=False)


def _coder_result():
    from types import SimpleNamespace
    return SimpleNamespace(succeeded=True, files_written=[], files_skipped=[], error="",
                           raw_response="")


def _verdict(monkeypatch, reply: str):
    """Gate 2's verdict for one stubbed model reply."""
    import tools.llm_stream as ls
    monkeypatch.setattr(ls, "request_completion", lambda **kw: reply)
    from tools.auto.inner_loop import LLMGate2Validator
    validator = LLMGate2Validator(base_url="http://stub.local/v1", model="m")
    return validator.approve_verdict(TASK, _exec_result(), _coder_result())


class TestGate2WithAFenceQuotingReply:
    APPROVAL = '{"approved": true, "feedback": "", "hints": ["```python\\nrun()\\n```"]}'
    APPROVAL_RAW = '{"approved": true, "feedback": "", "hints": ["```python\nrun()\n```"]}'
    REJECTION = ('{"approved": false, "feedback": "the hint is a sample:", '
                 '"hints": ["```python\\nretry(fetch, 3)\\n```"], '
                 '"suggested_approach": "wrap it"}')

    @pytest.mark.parametrize("reply", [APPROVAL, APPROVAL_RAW])
    def test_an_approval_quoting_a_fence_is_an_approval(self, monkeypatch, reply):
        verdict = _verdict(monkeypatch, reply)
        assert verdict.approved is True
        assert verdict.unavailable is False

    def test_a_wrapped_approval_quoting_a_fence_is_an_approval(self, monkeypatch):
        verdict = _verdict(monkeypatch, "```json\n" + self.APPROVAL + "\n```")
        assert verdict.approved is True and verdict.unavailable is False

    @pytest.mark.parametrize("reply", [REJECTION, "```json\n" + REJECTION + "\n```"])
    def test_a_rejections_hint_reaches_the_coder_intact(self, monkeypatch, reply):
        verdict = _verdict(monkeypatch, reply)
        assert verdict.approved is False and verdict.unavailable is False
        assert "retry(fetch, 3)" in verdict.reason

    def test_a_reply_with_no_fence_is_unchanged(self, monkeypatch):
        verdict = _verdict(monkeypatch, '{"approved": true, "feedback": ""}')
        assert verdict.approved is True and verdict.unavailable is False


def test_an_unclosed_json_fence_whose_string_quotes_a_fence_is_whole():
    """The model opened ```json, never closed it, and a string of its complete JSON quotes a code fence."""
    import json
    from tools.llm_stream import strip_json_fence
    body = json.dumps({"purpose": "Retry helper.", "notes": "Typical use:\n```python\nretry(fetch, 3)\n```"})
    assert json.loads(strip_json_fence("```json\n" + body)) == json.loads(body)
    # ... and the closed form still strips its closing fence
    assert json.loads(strip_json_fence("```json\n" + body + "\n```")) == json.loads(body)
