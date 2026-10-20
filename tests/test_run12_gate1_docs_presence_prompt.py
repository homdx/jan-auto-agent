"""tests/test_run12_gate1_docs_presence_prompt.py — RUN-12: Gate 1 asks a docs candidate whether the change is still needed.

Live runbook runs on gpt-oss-40k, 2026-10-19: flow 2 (hello-docs) proposes
"Append a ## Usage section to README.md …" and "Add a ## Development section
…". The presence check sent that instruction as ``Claimed problem:`` under the
code template's rule — reject a claim you cannot point a line at — and every
missing section came back rejected for the absence it named ("The excerpt does
not contain a ## Usage section"): low 0 of 4, medium 1 of 4, high 0 of 3, and
the run planned nothing.

Docs mode now has its own template: a proposed change, is it still needed, an
addition is needed exactly when the text lacks it, and the evidence for
something missing is the text it would go next to. The verbatim-evidence rule
is the same, and the code template is unchanged.
"""

from __future__ import annotations

import configparser
import json
from unittest.mock import patch

from tools.auto.architect import CandidateTask, CitedLocation
from tools.auto.gate1_filter import Gate1Filter

README = (
    "# hello-world\n"
    "\n"
    "A deliberately tiny Python project used to exercise jan-auto-agent flows\n"
    "end to end. `main.py` prints `Hello world` and nothing else.\n"
    "\n"
    "## Changelog\n"
    "\n"
    "Narrative entries live in `CHANGELOG.md`, newest first.\n"
)
USAGE = ("Append a ## Usage section to README.md containing a copy-pasteable command "
         "`python main.py` and the exact output `Hello world`.")


def _filter(task_mode: str) -> Gate1Filter:
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "api": {"active": "local", "verify_ssl": "false"},
        "api_local": {"base_url": "http://localhost:1337/v1", "api_key": "test",
                      "model": "test-model", "api_format": "openai"},
        "gate1": {"temperature": "0.0", "max_tokens": "64", "skip_llm": "false",
                  "unparseable_max_retries": "0", "llm_call_retry_wait_sec": "0"},
        "loop": {"timeout_seconds": "10"},
    })
    return Gate1Filter(config=cfg, base_url="http://localhost:1337/v1", api_key="test",
                       model="test-model", api_format="openai", verify_ssl=False, task_mode=task_mode)


def _candidate(instruction: str, file: str = "README.md") -> CandidateTask:
    return CandidateTask(title="Add Usage section to README", instruction=instruction, target_files=[file],
                         acceptance_check="true", cited_location=CitedLocation(file=file), cluster="docs")


def _presence(task_mode: str, reply: dict, *, block: str = README, instruction: str = USAGE):
    """One presence call against a stub provider; returns the verdict and the user message it was sent."""
    sent: list[str] = []

    def provider(url, headers, payload, **kwargs):
        sent.append(payload["messages"][-1]["content"])
        return json.dumps(reply)

    with patch("tools.llm_stream.request_completion", side_effect=provider):
        result = _filter(task_mode)._check_presence(_candidate(instruction), block)
    assert len(sent) == 1
    return result, sent[0]


def test_docs_presence_asks_whether_the_change_is_still_needed():
    _, user_msg = _presence("docs", {"verdict": "rejected", "evidence": "", "reason": "x"})
    assert user_msg.startswith(f"Proposed documentation change: {USAGE}\n")
    assert f"Text at that location:\n```\n{README}" in user_msg
    assert "that absence is the problem, so confirm it" in user_msg
    assert "the heading or line it would go next to" in user_msg
    # The code template's rule is what turned every missing section into a rejection.
    assert "Claimed problem:" not in user_msg
    assert "If you cannot point to an actual line" not in user_msg


def test_a_missing_section_confirmed_with_the_text_it_goes_next_to_is_kept():
    (confirmed, _reason, verdict), _ = _presence(
        "docs", {"verdict": "confirmed", "evidence": "## Changelog",
                 "reason": "The README has no Usage section; it would go before ## Changelog."})
    assert (confirmed, verdict) == (True, "confirmed")


def test_evidence_that_is_not_in_the_text_still_downgrades_a_confirmation():
    (confirmed, reason, _verdict), _ = _presence(
        "docs", {"verdict": "confirmed", "evidence": "## Usage\n\nRun python main.py",
                 "reason": "The section is missing."})
    assert confirmed is False
    assert "no matching evidence quote" in reason


def test_an_addition_the_text_already_makes_can_still_be_rejected():
    (confirmed, _reason, verdict), _ = _presence(
        "docs", {"verdict": "rejected", "evidence": "",
                 "reason": "The README already says main.py prints Hello world."},
        instruction="Add a sentence saying main.py prints Hello world.")
    assert (confirmed, verdict) == (False, "rejected")


def test_the_code_template_is_unchanged():
    block = "def parse(raw):\n    return raw\n"
    _, user_msg = _presence("code", {"verdict": "rejected", "evidence": "", "reason": "x"},
                            block=block, instruction="parse() never validates raw")
    assert user_msg.startswith("Claimed problem: parse() never validates raw\n")
    assert f"Code at that location:\n```\n{block}" in user_msg
    assert "If you cannot point to an actual line that supports the claim" in user_msg
    assert "Proposed documentation change" not in user_msg
