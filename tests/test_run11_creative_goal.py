"""tests/test_run11_creative_goal.py — RUN-11: the creative flows ask for an entry the seed does not already tell.

Live runbook runs on gpt-oss-40k, 2026-10-19 (five runs on profiles low,
medium and high): flow 3 asked to "Write a narrative changelog entry for the
greeting", the story examples/hello-world/CHANGELOG.md's seed entry
"### The first greeting" already tells. Every new entry retold it, Gate 2
asked for the duplicate to go ("Remove the duplicate entry so that only one
narrative entry appears at the top of CHANGELOG.md"), the coder rewrote the
file, and check_runbook failed "seed entry preserved" in all five runs.

The goal now names the runbook, which no entry covers. These tests pin that
scripts/run_flows.sh runs the goals RUNBOOK.md documents for each skill and
that the creative goals do not ask for the seed entry's story again.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RUN_FLOWS = REPO_ROOT / "scripts" / "run_flows.sh"
RUNBOOK = REPO_ROOT / "examples" / "hello-world" / "RUNBOOK.md"
CHANGELOG = REPO_ROOT / "examples" / "hello-world" / "CHANGELOG.md"
CREATIVE_SKILLS = ("hello-creative", "hello-creative-split")


def _run_flows_goals() -> dict[str, str]:
    """skill → goal, from run_flows.sh's GOAL_<n> / SKILL_<n> pairs."""
    text = RUN_FLOWS.read_text(encoding="utf-8")
    goals = dict(re.findall(r'^GOAL_(\d+)="([^"]*)"$', text, re.M))
    skills = dict(re.findall(r'^SKILL_(\d+)="([^"]*)"$', text, re.M))
    assert goals and goals.keys() == skills.keys()
    return {skills[n]: goals[n] for n in skills}


def _runbook_goals() -> dict[str, str]:
    """skill → goal, from RUNBOOK.md's `main.py --auto "<goal>" … --skill <skill>` commands."""
    out: dict[str, str] = {}
    for block in re.findall(r"```bash\n(.*?)```", RUNBOOK.read_text(encoding="utf-8"), re.DOTALL):
        for line in block.replace("\\\n", " ").splitlines():
            if "main.py" not in line or "--auto" not in line or "--skill" not in line:
                continue
            argv = shlex.split(line)
            out[argv[argv.index("--skill") + 1]] = argv[argv.index("--auto") + 1]
    return out


def test_run_flows_runs_the_goals_the_runbook_documents():
    runbook = _runbook_goals()
    for skill, goal in _run_flows_goals().items():
        assert runbook.get(skill) == goal, f"{skill}: run_flows.sh asks {goal!r}, RUNBOOK.md documents {runbook.get(skill)!r}"


def test_the_creative_goals_do_not_ask_for_the_seed_entrys_story():
    heading = re.search(r"^### (.+)$", CHANGELOG.read_text(encoding="utf-8"), re.M)[1]
    assert heading == "The first greeting"
    seed_topic = set(re.findall(r"[a-z]+", heading.lower())) - {"the", "first"}
    goals = _run_flows_goals()
    for skill in CREATIVE_SKILLS:
        words = set(re.findall(r"[a-z]+", goals[skill].lower()))
        assert not words & seed_topic, f"{skill}'s goal {goals[skill]!r} asks for the seed entry's story again"
