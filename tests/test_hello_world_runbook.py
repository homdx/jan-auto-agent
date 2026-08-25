"""RUNBOOK-1 — every command in the hello-world runbook is still valid.

Documentation rots silently. A runbook that names a skill which was renamed,
a profile whose ``num_ctx`` dropped below the skill's floor, or a flag that
was never added produces a confident copy-paste that fails several minutes
into a run — and the failure looks like a product bug rather than a stale
document.

So rather than trusting the prose, these tests PARSE
``examples/hello-world/RUNBOOK.md`` and check each documented command against
the real repository: the skill adapter exists, the profile exists and clears
that skill's context budget, ``argparse`` recognises every flag, the
``--base`` path is real, and the gate table matches what the adapters
actually resolve to.

The parsing is deliberately strict. If the runbook's command format changes
in a way this file cannot read, the extraction tests fail loudly instead of
quietly finding zero commands and passing — a test that silently validates
nothing is worse than no test.
"""

from __future__ import annotations

import configparser
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from tools.auto.gate_registry import resolve_gate_order
from tools.skills.loader import SkillBudgetError, apply_skill, list_skills, load_skill

REPO_ROOT = Path(__file__).resolve().parent.parent
HELLO_WORLD = REPO_ROOT / "examples" / "hello-world"
RUNBOOK = HELLO_WORLD / "RUNBOOK.md"
SKILLS_DIR = REPO_ROOT / "skills"

#: How many `python3 main.py --auto ... --skill ...` invocations the runbook
#: is expected to document — one per mechanical base, plus Flow 4's
#: same-base provider-split variant (GATE3-PROFILE-5). Pinned so that losing
#: a flow to a bad edit is a failure rather than a smaller, still-green run.
EXPECTED_FLOWS = 4


# ─────────────────────────────────────────────────────────────────────────────
# Parsing
# ─────────────────────────────────────────────────────────────────────────────

def _runbook_text() -> str:
    return RUNBOOK.read_text(encoding="utf-8")


def _shell_commands() -> list[str]:
    """Every command line inside ```bash fences, with continuations joined."""
    out: list[str] = []
    for block in re.findall(r"```bash\n(.*?)```", _runbook_text(), re.DOTALL):
        joined = block.replace("\\\n", " ")
        for line in joined.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(line)
    return out


def _flow_commands() -> list[list[str]]:
    """The `main.py --auto ... --skill ...` invocations, tokenised."""
    flows: list[list[str]] = []
    for line in _shell_commands():
        if "main.py" not in line or "--auto" not in line:
            continue
        flows.append(shlex.split(line))
    return flows


def _opt(argv: list[str], name: str) -> str | None:
    for i, token in enumerate(argv):
        if token == name and i + 1 < len(argv):
            return argv[i + 1]
        if token.startswith(name + "="):
            return token.split("=", 1)[1]
    return None


def _flow_ids() -> list[str]:
    return [_opt(a, "--skill") or f"flow{i}" for i, a in enumerate(_flow_commands())]


FLOWS = _flow_commands()


# ─────────────────────────────────────────────────────────────────────────────
# The parser itself must not silently find nothing
# ─────────────────────────────────────────────────────────────────────────────

def test_runbook_exists():
    assert RUNBOOK.is_file(), f"{RUNBOOK} is missing"


def test_runbook_has_bash_blocks():
    assert _shell_commands(), "no ```bash blocks parsed — has the format changed?"


def test_expected_number_of_flows_documented():
    """A test that validates zero commands would pass vacuously."""
    assert len(FLOWS) == EXPECTED_FLOWS, (
        f"expected {EXPECTED_FLOWS} --auto/--skill commands, parsed {len(FLOWS)}: "
        f"{[' '.join(a) for a in FLOWS]}"
    )


def test_every_flow_names_a_skill():
    for argv in FLOWS:
        assert _opt(argv, "--skill"), f"no --skill in: {' '.join(argv)}"


# ─────────────────────────────────────────────────────────────────────────────
# Each documented command is runnable in principle
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("argv", FLOWS, ids=_flow_ids())
def test_flow_skill_exists(argv):
    skill = _opt(argv, "--skill")
    assert skill in list_skills(SKILLS_DIR), (
        f"runbook names skill {skill!r}; available: {list_skills(SKILLS_DIR)}"
    )


@pytest.mark.parametrize("argv", FLOWS, ids=_flow_ids())
def test_flow_config_file_exists(argv):
    config = _opt(argv, "--config")
    assert config, f"no --config in: {' '.join(argv)}"
    assert (REPO_ROOT / config).is_file(), f"runbook names missing profile {config!r}"


@pytest.mark.parametrize("argv", FLOWS, ids=_flow_ids())
def test_flow_base_dir_exists(argv):
    base = _opt(argv, "--base")
    assert base, f"no --base in: {' '.join(argv)}"
    assert (REPO_ROOT / base).is_dir(), f"runbook names missing base_dir {base!r}"


@pytest.mark.parametrize("argv", FLOWS, ids=_flow_ids())
def test_flow_goal_is_non_empty(argv):
    goal = _opt(argv, "--auto")
    assert goal and goal.strip(), f"empty --auto goal in: {' '.join(argv)}"


@pytest.mark.parametrize("argv", FLOWS, ids=_flow_ids())
def test_flow_profile_clears_the_skill_budget(argv):
    """The exact check that would otherwise fail minutes into a run."""
    skill = _opt(argv, "--skill")
    config = _opt(argv, "--config")
    cfg = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    cfg.read(REPO_ROOT / config, encoding="utf-8")
    try:
        load_skill(skill, cfg, REPO_ROOT, skills_dir=SKILLS_DIR)
    except SkillBudgetError as exc:
        pytest.fail(f"runbook pairs {skill!r} with {config!r}, which it cannot fit: {exc}")


@pytest.mark.parametrize("argv", FLOWS, ids=_flow_ids())
def test_flow_flags_are_recognised_by_argparse(argv):
    """Catches a flag documented but never implemented (or since renamed)."""
    help_text = subprocess.run(
        [sys.executable, "main.py", "--help"],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120,
    ).stdout
    for token in argv:
        if token.startswith("--"):
            flag = token.split("=", 1)[0]
            assert flag in help_text, f"{flag} is not a real main.py flag"


# ─────────────────────────────────────────────────────────────────────────────
# The runbook's claims match the adapters
# ─────────────────────────────────────────────────────────────────────────────

def _gate_table() -> dict[str, list[str]]:
    """Parse the 'Expected gate sets' markdown table."""
    table: dict[str, list[str]] = {}
    for row in re.findall(r"^\|\s*\d+\s*\|(.+)$", _runbook_text(), re.MULTILINE):
        cells = [c.strip() for c in row.split("|")]
        if len(cells) < 3:
            continue
        skill = cells[0].strip("`")
        gates_cell = cells[2]
        if "none" in gates_cell.lower():
            gates: list[str] = []
        else:
            gates = [g.strip().strip("`") for g in gates_cell.split(",") if g.strip()]
        table[skill] = gates
    return table


def test_gate_table_is_parseable():
    table = _gate_table()
    assert len(table) == EXPECTED_FLOWS, f"parsed gate table: {table}"


@pytest.mark.parametrize("argv", FLOWS, ids=_flow_ids())
def test_documented_gates_match_the_adapter(argv):
    """The table is a copy of the adapters; copies drift."""
    skill = _opt(argv, "--skill")
    config = _opt(argv, "--config")
    documented = _gate_table().get(skill)
    assert documented is not None, f"{skill!r} missing from the gate table"

    cfg = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    cfg.read(REPO_ROOT / config, encoding="utf-8")
    apply_skill(cfg, skill, REPO_ROOT, skills_dir=SKILLS_DIR)
    mode = cfg.get("auto", "task_mode")
    actual = [g.name for g in resolve_gate_order(cfg, mode)]
    assert actual == documented, (
        f"runbook says {skill} -> {documented}, adapter resolves to {actual}"
    )


@pytest.mark.parametrize("argv", FLOWS, ids=_flow_ids())
def test_documented_base_matches_the_adapter(argv):
    """Each flow's prose names its `base`; check it against the adapter."""
    skill = _opt(argv, "--skill")
    overlay = load_skill(
        skill,
        _cfg_for(REPO_ROOT / (_opt(argv, "--config") or "agents_32k.ini")),
        REPO_ROOT,
        skills_dir=SKILLS_DIR,
    )
    assert f"`base = {overlay.base}`" in _runbook_text(), (
        f"runbook never states `base = {overlay.base}` for {skill}"
    )


def _cfg_for(path: Path) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    cfg.read(path, encoding="utf-8")
    return cfg


def test_all_three_bases_are_exercised():
    """The runbook's purpose is one flow per mechanical base."""
    bases = set()
    for argv in FLOWS:
        cfg = _cfg_for(REPO_ROOT / (_opt(argv, "--config") or "agents_32k.ini"))
        bases.add(load_skill(_opt(argv, "--skill"), cfg, REPO_ROOT,
                             skills_dir=SKILLS_DIR).base)
    assert bases == {"code", "docs", "creative"}


def test_every_shipped_skill_has_a_documented_flow():
    """A skill nobody documents is a skill nobody runs."""
    documented = {_opt(a, "--skill") for a in FLOWS}
    assert set(list_skills(SKILLS_DIR)) == documented


# ─────────────────────────────────────────────────────────────────────────────
# The baseline the flows measure against
# ─────────────────────────────────────────────────────────────────────────────

def test_baseline_main_prints_hello_world():
    result = subprocess.run(
        [sys.executable, "main.py"],
        cwd=str(HELLO_WORLD), capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "Hello world"


def test_baseline_main_is_unhardened():
    """Flow 1 adds sys.exit and type hints; if they are already there, a run
    validates nothing. This is the check the reset step in the runbook exists
    to guarantee."""
    source = (HELLO_WORLD / "main.py").read_text(encoding="utf-8")
    assert "sys.exit" not in source, (
        "main.py already hardened — reset the sandbox before running flow 1"
    )


def test_baseline_has_no_test_file():
    """Flow 1 creates it; flow 2's known defect is claiming it already exists."""
    assert not (HELLO_WORLD / "test_main.py").exists(), (
        "test_main.py is committed — flow 1 has nothing to create and flow 2's "
        "invented-test-suite defect becomes untestable"
    )


def test_baseline_changelog_seed_entry_present():
    """`continuity` needs a predecessor; without one its approval is hollow."""
    text = (HELLO_WORLD / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "### The first greeting" in text
    assert "Hello world" in text


def test_baseline_is_not_a_git_repo():
    """The flows commit into examples/hello-world; a leftover .git means the
    next run starts from the previous run's output."""
    assert not (HELLO_WORLD / ".git").exists(), (
        "leftover .git in examples/hello-world — run the runbook reset step"
    )


def test_no_leftover_agent_state():
    assert not (HELLO_WORLD / ".agent").exists(), (
        "leftover .agent state — a run will resume instead of starting fresh"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Documented facts that are easy to get wrong
# ─────────────────────────────────────────────────────────────────────────────

def test_runbook_documents_the_reset_step():
    """Every observed re-run problem traced back to a dirty sandbox."""
    text = _runbook_text()
    assert ".agent" in text and "git checkout" in text


def test_runbook_warns_that_8k_is_refused():
    """The single most likely first-run failure."""
    assert "16384" in _runbook_text()


def test_runbook_explains_silent_gate3():
    """Silence means approved, not absent — the reading that cost a session."""
    text = _runbook_text().lower()
    assert "only" in text and "reject" in text
    assert "unreachable" in text


def test_runbook_documents_the_validator_warning_as_expected():
    assert "AUTO-CR-19-1" in _runbook_text()


def test_runbook_records_the_known_docs_defect():
    """Flow 2 invents a test suite; a reader must not chase it as a new bug."""
    assert "unittest discover" in _runbook_text()


def test_referenced_helper_scripts_exist():
    for name in ("view_trace.py", "analyze_logs.py"):
        if name in _runbook_text():
            assert (REPO_ROOT / name).is_file(), f"runbook references missing {name}"
