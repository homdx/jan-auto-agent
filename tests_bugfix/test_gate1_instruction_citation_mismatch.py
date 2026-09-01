"""tests/test_gate1_instruction_citation_mismatch.py — AUTO-H2-6b regression.

Confirmed in production, not synthesized: a real ``--auto`` run against
``examples/hello-world`` (config ``agents_128k.ini``, architect model
``deepseek-v4-flash``) reviewed the "support" cluster — legal citations
CHANGELOG.md, README.md, RUNBOOK.md only — against the run goal "Harden
main.py: docstrings, type hints, a pytest test". Three of the four
candidates it returned had instruction text entirely about ``main.py``, a
file outside that cluster, while ``cited_location.file`` AND
``target_files`` both landed on ``CHANGELOG.md`` — a legal-but-wrong
citation, not a hallucinated path (Stage A's existence check passed all
three; ``CHANGELOG.md`` is a real file).

AUTO-H2-6's ``target_file_context()`` exists for exactly this shape of
bug, but does not fire here: its mismatch condition is ``cited_file not
in target_files``, and here they agree (both wrongly point at
CHANGELOG.md). Stage B was shown only CHANGELOG.md's prose and rejected
all three — correctly, given what it saw — including the one candidate
that proposed the fix the run actually needed: a new
``tests/test_main.py``. The deterministic checker (``scripts/
check_runbook.py``'s ``check_task1``) then failed with "test file
created — none", even though the Architect had, in a very real sense,
already thought of it.

_CAND_DOCSTRING / _CAND_TYPE_HINTS / _CAND_TEST_FILE below are the *exact*
three "support"-cluster candidates from that run's trace (same title, same
instruction, same target_files, same cited_location) — reconstructed
against ``examples/hello-world`` in this actual repo checkout, which is
the same fixture the run itself used.
"""

from __future__ import annotations

import configparser
from pathlib import Path

import pytest

from tools.auto.architect import CandidateTask, CitedLocation
from tools.auto.gate1_filter import Gate1Filter
from tools.auto.gate1_grounding import instruction_file_context

REPO_ROOT = Path(__file__).resolve().parent.parent
HELLO_WORLD_DIR = REPO_ROOT / "examples" / "hello-world"


@pytest.fixture()
def minimal_config() -> configparser.ConfigParser:
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
    return cfg


@pytest.fixture()
def filt(minimal_config: configparser.ConfigParser) -> Gate1Filter:
    return Gate1Filter(
        config=minimal_config, base_url="http://localhost:1337/v1",
        api_key="test", model="test-model", api_format="openai", verify_ssl=False,
    )


# The three real "support"-cluster candidates, verbatim from the run trace.

_CAND_DOCSTRING = CandidateTask(
    title="Add module and function docstrings to main.py",
    instruction=(
        "Add a module-level docstring at the top of main.py (before any "
        "imports) describing what the script does, and add a docstring to "
        "the main() function describing its behavior. Ensure the module "
        "docstring is the very first statement in the file so "
        "module.__doc__ is set. Do not change any printed output."
    ),
    target_files=["CHANGELOG.md"],
    acceptance_check="true",
    cited_location=CitedLocation(file="CHANGELOG.md", line_start=1, line_end=20),
    cluster="support",
)

_CAND_TYPE_HINTS = CandidateTask(
    title="Add type hints to main.py functions",
    instruction=(
        "Add type hints to every function signature in main.py. The "
        "main() function should be annotated to return int (e.g., 'def "
        "main() -> int:'). If there are any other functions, annotate "
        "their parameters and return types as well. Do not change any "
        "printed output."
    ),
    target_files=["CHANGELOG.md"],
    acceptance_check="true",
    cited_location=CitedLocation(file="CHANGELOG.md", line_start=1, line_end=20),
    cluster="support",
)

# The candidate that matters most: this is the "create a separate test
# file" proposal the deterministic checker wanted, and it was rejected
# right alongside the other two.
_CAND_TEST_FILE = CandidateTask(
    title="Add a pytest test for main.py output",
    instruction=(
        "Create a pytest test file (tests/test_main.py) that imports main "
        "from main.py and asserts that calling main() prints 'Hello "
        "world' to stdout using capsys. The test should also assert that "
        "main() returns 0. Ensure the test passes with 'python3 -m "
        "pytest tests/test_main.py -q'."
    ),
    target_files=["CHANGELOG.md"],
    acceptance_check="python3 -m pytest tests/test_main.py -q",
    cited_location=CitedLocation(file="CHANGELOG.md", line_start=1, line_end=20),
    cluster="support",
)

# Control: the one "entry_orchestration"-cluster candidate from the same
# run, where citation and instruction already agree (both main.py). Must
# never get a note — asserted below alongside the three broken ones.
_CAND_CORRECTLY_CITED = CandidateTask(
    title="Add a pytest test asserting the printed output",
    instruction=(
        "Add a test function to main.py that uses pytest's capsys "
        "fixture to capture stdout, calls main(), and asserts that the "
        "captured output is exactly 'Hello world\\n'. This verifies the "
        "script's observable behavior."
    ),
    target_files=["main.py"],
    acceptance_check="python -m pytest main.py -q",
    cited_location=CitedLocation(file="main.py", symbol="main", line_start=8, line_end=13),
    cluster="entry_orchestration",
)


class TestInstructionFileContextUnit:
    """Direct unit tests of instruction_file_context() — no Gate1Filter."""

    def test_wrong_citation_pulls_the_real_file_content(self) -> None:
        note = instruction_file_context(
            instruction=_CAND_TEST_FILE.instruction,
            cited_file="CHANGELOG.md",
            base_dir=HELLO_WORLD_DIR,
        )
        assert note is not None
        assert "main.py" in note
        assert "CHANGELOG.md" in note
        # The real main.py content must be in there, not a placeholder.
        assert "def main()" in note

    def test_matching_citation_returns_none(self) -> None:
        note = instruction_file_context(
            instruction="Improve error handling in main.py.",
            cited_file="main.py",
            base_dir=HELLO_WORLD_DIR,
        )
        assert note is None

    def test_already_noted_short_circuits(self) -> None:
        """When target_file_context already found something, this stays
        out of the way rather than adding a second, possibly-redundant
        note for the same candidate."""
        note = instruction_file_context(
            instruction=_CAND_TEST_FILE.instruction,
            cited_file="CHANGELOG.md",
            base_dir=HELLO_WORLD_DIR,
            already_noted=True,
        )
        assert note is None

    def test_instruction_naming_no_real_file_returns_none(self) -> None:
        """Prose that merely looks file-shaped (an abbreviation, a
        version number) must not misfire just because it matches the
        filename regex — it has to resolve to something real on disk."""
        note = instruction_file_context(
            instruction="Bump the tool to v1.2, e.g. update the banner text.",
            cited_file="CHANGELOG.md",
            base_dir=HELLO_WORLD_DIR,
        )
        assert note is None

    def test_second_file_mentioned_alongside_correct_citation_is_left_alone(self) -> None:
        """The narrower guard: if the cited file IS named in the
        instruction, a second file mentioned for context must not trigger
        a note — the citation was never wrong."""
        note = instruction_file_context(
            instruction="Update main.py to match the pattern already used in README.md.",
            cited_file="main.py",
            base_dir=HELLO_WORLD_DIR,
        )
        assert note is None


class TestGroundingNotesIntegration:
    """Through Gate1Filter's actual Stage A + grounding-notes assembly,
    against the real examples/hello-world fixture the production run used.
    """

    @pytest.mark.parametrize(
        "candidate", [_CAND_DOCSTRING, _CAND_TYPE_HINTS, _CAND_TEST_FILE],
        ids=["docstrings", "type_hints", "test_file"],
    )
    def test_mismatch_note_fires_for_all_three_real_candidates(
        self, filt: Gate1Filter, candidate: CandidateTask,
    ) -> None:
        ok, reason, block = filt._check_existence(candidate, HELLO_WORLD_DIR, cluster_files=None)
        assert ok, f"existence check failed: {reason}"
        # Sanity: Stage A's extracted block is CHANGELOG.md's content, the
        # exact condition that makes Stage B reject these without the fix.
        assert "Changelog" in block

        module_docstring = filt._module_docstring_for(candidate, HELLO_WORLD_DIR)
        notes = filt._build_grounding_notes(candidate, block, module_docstring, HELLO_WORLD_DIR)

        assert "NOTE (automated)" in notes
        assert "AUTO-H2-6b" in notes
        assert "main.py" in notes
        assert "def main()" in notes

    def test_no_note_when_citation_already_correct(self, filt: Gate1Filter) -> None:
        """Sanity: the well-formed entry_orchestration candidate from the
        same run must not get an AUTO-H2-6b note — nothing is wrong with
        it."""
        candidate = _CAND_CORRECTLY_CITED
        ok, reason, block = filt._check_existence(candidate, HELLO_WORLD_DIR, cluster_files=None)
        assert ok, f"existence check failed: {reason}"

        module_docstring = filt._module_docstring_for(candidate, HELLO_WORLD_DIR)
        notes = filt._build_grounding_notes(candidate, block, module_docstring, HELLO_WORLD_DIR)
        assert "AUTO-H2-6b" not in notes
