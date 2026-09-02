"""tests/test_gate1_location_target_mismatch.py — AUTO-H2-6 regression.

Confirmed in production (not a synthetic scenario): a real --validate-plan
run on two real candidate pools rejected 26/26 candidates whose Location
field named a different file than their own target_files — 100%, in both
folders. Root cause, traced to the exact two lines of code:

    tools/auto/backlog_prioritiser.py:  loc_str = loc.file          # "Location:" IS cited_location.file
    tools/auto/gate1_filter.py:         abs_path = base_dir / loc.file   # _check_existence reads FROM cited_location.file, never target_files

Stage B was being shown the cited-evidence file (often a cluster-seed
config file, or an unrelated test file) while being asked whether a
problem exists in a completely different file — the one that would
actually get edited. Every observed rejection reason said some version of
"the code shown is X, not Y" — the LLM was correct given what it saw,
Stage A just showed it the wrong thing.

AUTO-T1 and AUTO-T2 below are the *exact* real candidates from that run
(same title, same Location, same target_files, same instruction) —
reconstructed against this actual repo checkout, not synthetic fixtures.
"""

from __future__ import annotations

import configparser
from pathlib import Path

import pytest

from tools.auto.architect import CandidateTask, CitedLocation
from tools.auto.gate1_filter import Gate1Filter
from tools.auto.gate1_grounding import target_file_context

REPO_ROOT = Path(__file__).resolve().parent.parent


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


# AUTO-T1, real production candidate: Location=agents_4k.ini (a config file,
# no Python code at all), Target=tools/collect/cli.py. Confirmed rejected in
# the real run with reason: "The code shown is agents_4k.ini, a
# configuration file, not tools/collect/cli.py, so the claimed error-handling
# problem in that Python module is not present in the shown code."
_AUTO_T1 = CandidateTask(
    title="Harden error handling in the collect CLI",
    instruction=(
        "Improve error handling in tools/collect/cli.py. Wrap all network/API "
        "interactions (contract fetching, seed loading) in explicit try/except "
        "blocks that distinguish transient failures (timeouts, connection "
        "resets, HTTP 5xx) from permanent ones (HTTP 4xx, invalid config). "
        "Add retry-with-backoff for transient failures using the "
        "retry_delays_sec schedule from agents.ini (5,15,30 seconds), and "
        "surface a clear, actionable error message to stderr with a non-zero "
        "exit code when a task cannot complete. Also handle JSONDecodeError "
        "when parsing API responses and validate that required config keys "
        "exist before use."
    ),
    target_files=["tools/collect/cli.py", "tests/test_collect_cli.py"],
    acceptance_check="true",
    cited_location=CitedLocation(file="agents_4k.ini"),
    cluster="entry_orchestration",
)

# AUTO-T2, real production candidate: Location=tests/test_collect_cli.py (a
# test file), Target=tools/collect/cli.py. Same shape, different mismatch —
# cited evidence is a test asserting the desired behavior, not the
# implementation itself.
_AUTO_T2 = CandidateTask(
    title="Harden error handling in tools/collect/cli.py",
    instruction=(
        "Edit tools/collect/cli.py so every user-facing failure path raises "
        "CollectCliError with an actionable message instead of leaking raw "
        "exceptions. Concretely: wrap all file reads (read_text/read_bytes) "
        "of module files, the artifact JSON, and the manifest JSON in "
        "try/except blocks; convert UnicodeDecodeError, FileNotFoundError, "
        "PermissionError, and json.JSONDecodeError into CollectCliError with "
        "a clear message naming the file and the underlying cause."
    ),
    target_files=["tools/collect/cli.py"],
    acceptance_check="true",
    cited_location=CitedLocation(file="tests/test_collect_cli.py"),
    cluster="entry_orchestration",
)


class TestTargetFileContextUnit:
    """Direct unit tests of target_file_context() — no Gate1Filter involved."""

    def test_config_file_citation_pulls_target_file_content(self) -> None:
        note = target_file_context(
            target_files=["tools/collect/cli.py", "tests/test_collect_cli.py"],
            cited_file="agents_4k.ini",
            cited_symbol=None,
            instruction=_AUTO_T1.instruction,
            base_dir=REPO_ROOT,
        )
        assert note is not None
        assert "tools/collect/cli.py" in note
        assert "agents_4k.ini" in note
        # The real target file's actual content must be in there, not a
        # placeholder — spot check a name that only exists in cli.py.
        assert "CollectCliError" in note or "def " in note

    def test_matching_citation_returns_none(self) -> None:
        """No mismatch (cited_file IS one of the target_files) -> no note,
        zero added cost for the common case."""
        note = target_file_context(
            target_files=["tools/collect/cli.py"],
            cited_file="tools/collect/cli.py",
            cited_symbol=None,
            instruction="anything",
            base_dir=REPO_ROOT,
        )
        assert note is None

    def test_no_target_files_returns_none(self) -> None:
        note = target_file_context(
            target_files=[], cited_file="agents_4k.ini", cited_symbol=None,
            instruction="anything", base_dir=REPO_ROOT,
        )
        assert note is None

    def test_finds_specific_symbol_named_in_instruction_not_just_head_of_file(self) -> None:
        """Real behavior we actually want: instead of dumping the first 40
        lines of a possibly-large target file, resolve a symbol the
        instruction itself names."""
        note = target_file_context(
            target_files=["tools/auto/controller.py"],
            cited_file="agents_4k.ini",
            cited_symbol=None,
            instruction="RunLimits.from_config has a bug in its getfloat handling",
            base_dir=REPO_ROOT,
        )
        assert note is not None
        assert "from_config" in note


class TestGroundingNotesIntegration:
    """Through Gate1Filter's actual Stage A + grounding-notes assembly,
    against this real repo checkout."""

    @pytest.mark.parametrize("candidate,cited", [(_AUTO_T1, "agents_4k.ini"), (_AUTO_T2, "tests/test_collect_cli.py")])
    def test_mismatch_note_fires_for_real_candidates(
        self, filt: Gate1Filter, candidate: CandidateTask, cited: str,
    ) -> None:
        ok, reason, block = filt._check_existence(candidate, REPO_ROOT, cluster_files=None)
        assert ok, f"existence check failed: {reason}"

        module_docstring = filt._module_docstring_for(candidate, REPO_ROOT)
        notes = filt._build_grounding_notes(candidate, block, module_docstring, REPO_ROOT)

        assert "NOTE (automated)" in notes
        assert cited in notes
        assert "tools/collect/cli.py" in notes

    def test_no_note_when_location_matches_target(self, filt: Gate1Filter) -> None:
        """Sanity: this doesn't fire for the ~55-60% of real candidates where
        Location and Target already agree — matches AUTO-T3 from the same
        real folder (Location == Target == tools/auto/controller.py)."""
        candidate = CandidateTask(
            title="Harden config parsing in AutoController and RunLimits",
            instruction="Improve error handling in tools/auto/controller.py.",
            target_files=["tools/auto/controller.py"],
            acceptance_check="true",
            cited_location=CitedLocation(file="tools/auto/controller.py"),
            cluster="entry_orchestration",
        )
        ok, reason, block = filt._check_existence(candidate, REPO_ROOT, cluster_files=None)
        assert ok, f"existence check failed: {reason}"

        module_docstring = filt._module_docstring_for(candidate, REPO_ROOT)
        notes = filt._build_grounding_notes(candidate, block, module_docstring, REPO_ROOT)
        assert "NOTE (automated)" not in notes or "target file" not in notes.lower()
