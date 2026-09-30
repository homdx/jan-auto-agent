"""KC-8 — every claim in the Kilo-contest runbook still holds.

``docs/kilo-contest/RUN-THE-KILO-CONTEST.md`` is the operator's reset/run/
score page for the contest built by the KC epic. Its commands are the real
``tools.contest`` CLI and the scorer the round hands over to, and its last
section holds the first round on record. Parse the page the way
``tests/test_hello_world_runbook.py`` does: the sections exist, every file it
names exists, every ``--flag`` it documents is still a flag of the CLI it
belongs to, the cross-references the ticket asked for point at the page, and
the recorded round's table carries at least one ``READY``.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RUNBOOK = REPO_ROOT / "docs" / "kilo-contest" / "RUN-THE-KILO-CONTEST.md"

#: The ticket's section list, in the order the page must carry it.
REQUIRED_SECTIONS = (
    "## The map",
    "## What you need, once",
    "## Stage R — reset",
    "## Stage RUN",
    "## The gate",
    "## Hand-over",
    "## Known limits",
    "## First round on record",
)

#: Every backticked repo path the page may name.
REFERENCED_FILES = (
    "scripts/contest_reset.sh",
    "contest.ini",
    "contest-bench/README.md",
    "docs/collect-epics/RUN-THE-EPIC-COMPETITION.md",
    "docs/kilo-contest/EPIC-KC.md",
    "docs/kilo-contest/PROBE.md",
    "tests/_kilo_fake.py",
    "scripts/judge_epic_round.py",
    "contest-bench/harness/setup_worktrees.py",
)

#: The CLIs whose ``--flag`` set the page draws from, one help text each.
_HELP_COMMANDS = (
    ["-m", "tools.contest", "run", "--help"],
    ["-m", "tools.contest", "status", "--help"],
    # stage D: `contest draft` and the `--collect` it runs (main.py's flag)
    ["-m", "tools.contest", "draft", "--help"],
    ["main.py", "--help"],
    ["-m", "tools.contest.workspace", "prepare", "--help"],
    ["contest-bench/harness/setup_worktrees.py", "--help"],
    ["scripts/judge_epic_round.py", "--help"],
)

_FLAG_RE = re.compile(r"--[a-z][a-z0-9-]*")


def _runbook_text() -> str:
    return RUNBOOK.read_text(encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# the page and its sections
# ─────────────────────────────────────────────────────────────────────────────

def test_runbook_page_exists():
    assert RUNBOOK.is_file(), f"{RUNBOOK} is missing — the KC-8 deliverable"


def test_page_carries_the_ticket_sections_in_order():
    text = _runbook_text()
    positions = []
    for section in REQUIRED_SECTIONS:
        assert section in text, f"runbook is missing its {section!r} section"
        positions.append(text.index(section))
    assert positions == sorted(positions), (
        f"the sections are out of the ticket's order: {positions}")


def test_map_names_the_reset_run_score_judge_merge_commands():
    text = _runbook_text()
    for command in (
        "scripts/contest_reset.sh",
        "python3 -m tools.contest run",
        "python3 -m tools.contest status",
        "judge_epic_round.py",
        "setup_worktrees.py",
        "cherry-pick",
    ):
        assert command in text, f"runbook's map never names {command!r}"


def test_every_referenced_file_exists():
    text = _runbook_text()
    for rel in REFERENCED_FILES:
        if rel in text:
            assert (REPO_ROOT / rel).exists(), f"runbook names missing {rel!r}"


# ─────────────────────────────────────────────────────────────────────────────
# the documented flags are still flags of the CLIs they belong to
# ─────────────────────────────────────────────────────────────────────────────

def _documented_flags() -> set[str]:
    # The recorded SUMMARY.md quotes the agents' own commands verbatim; those
    # are not the page's instructions, so their dashes are not flags.
    text = re.sub(r"^````markdown\n.*?^````", "", _runbook_text(),
                  flags=re.MULTILINE | re.DOTALL)
    return set(_FLAG_RE.findall(text))


def test_documented_flags_are_recognised_by_their_clis():
    help_texts = []
    for argv in _HELP_COMMANDS:
        result = subprocess.run(
            [sys.executable, *argv],
            cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120,
        )
        assert result.returncode == 0, f"{' '.join(argv)}: {result.stderr}"
        help_texts.append(result.stdout)
    known = set()
    for lines in help_texts:
        known.update(_FLAG_RE.findall(lines))
    unknown = _documented_flags() - known
    assert not unknown, (
        f"the runbook documents flags no CLI recognises: {sorted(unknown)}")


# ─────────────────────────────────────────────────────────────────────────────
# the cross-references the ticket asked for
# ─────────────────────────────────────────────────────────────────────────────

def _text(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def test_epic_runbook_stage_1_points_at_the_runbook():
    text = _text("docs/collect-epics/RUN-THE-EPIC-COMPETITION.md")
    start = text.index("## Stage 1")
    end = text.index("## Stage 3")
    assert "RUN-THE-KILO-CONTEST.md" in text[start:end], (
        "Stage 1 of the epic runbook never points at the contest page")


def test_pipeline_docs_index_lists_the_runbook():
    assert "RUN-THE-KILO-CONTEST.md" in _text("docs/PIPELINE-DOCS-INDEX.md")


def test_agents_md_points_at_the_runbook():
    assert "RUN-THE-KILO-CONTEST.md" in _text("AGENTS.md")


def test_probe_links_the_runbook():
    assert "RUN-THE-KILO-CONTEST.md" in _text("docs/kilo-contest/PROBE.md")


# ─────────────────────────────────────────────────────────────────────────────
# the first round on record
# ─────────────────────────────────────────────────────────────────────────────

def _recorded_block() -> str:
    """The fenced block the 'First round on record' section holds verbatim."""
    text = _runbook_text()
    section = text[text.index("## First round on record"):]
    fence = re.search(r"^````markdown\n(.*?)^````", section,
                      re.MULTILINE | re.DOTALL)
    assert fence, "no verbatim SUMMARY.md fence under 'First round on record'"
    return fence.group(1)


def test_recorded_round_holds_at_least_one_ready():
    block = _recorded_block()
    rows = [line for line in block.splitlines()
            if line.startswith("| ") and "model" not in line.split("|")[1]]
    agents = [line for line in rows if not line.startswith("| ---")]
    assert agents, "the recorded round names no agent"
    ready = [line for line in agents if line.split("|")[3].strip() == "READY"]
    assert ready, "the first round on record has no READY agent"


def test_recorded_round_carries_its_decision_list():
    block = _recorded_block()
    assert "## Decisions worth a look" in block
    decisions = [line for line in block.splitlines()
                 if re.match(r"^- [\w.-]+: gate ", line)]
    assert decisions, "the recorded round's gate decisions are not listed"
    assert any("reject" in line for line in decisions)
    assert any("once" in line for line in decisions)


def test_recorded_round_says_what_the_gate_did():
    text = _runbook_text()
    section = text[text.index("## First round on record"):]
    assert "What the gate did" in section
