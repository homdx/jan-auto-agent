"""GYRO-01: an external --target round gets no jan-auto-agent house rules.

Before: every round prompt and every REWORK message named `CollectBridge._shrink`,
`agents_128k.ini` and the fail-open collect rule, so agents on llama.cpp searched
its tree for `tools/` files that do not exist.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from tools.contest.gates import BRIDGE
from tools.contest.harvest import Harvest, Reason, rework_message
from tools.contest.runner import round_prompt


def _repo(root: Path, with_bridge: bool) -> tuple[Path, str]:
    """A one-commit repo with a ticket, and the bridge file when *with_bridge*."""
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "epic-tasks").mkdir()
    ticket = root / "epic-tasks" / "01-x.md"
    ticket.write_text("# X-01 — x\n\n**Status:** open\n", encoding="utf-8")
    if with_bridge:
        (root / BRIDGE).parent.mkdir(parents=True)
        (root / BRIDGE).write_text("def _shrink():\n    pass\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-qm", "base"], check=True)
    sha = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], check=True,
                         capture_output=True, text=True).stdout.strip()
    return ticket, sha


def test_external_target_prompt_has_no_house_rules(tmp_path):
    ticket, sha = _repo(tmp_path, with_bridge=False)
    text = round_prompt("a", ticket, sha)
    for house in ("CollectBridge", "agents_128k.ini", "fail-open", "68b78a0"):
        assert house not in text
    assert "One local commit." in text and "A test ships with the change" in text


def test_jan_auto_agent_prompt_keeps_house_rules(tmp_path):
    ticket, sha = _repo(tmp_path, with_bridge=True)
    text = round_prompt("a", ticket, sha)
    assert "CollectBridge._shrink" in text and "agents_128k.ini" in text


def test_rework_names_shrink_only_when_the_base_has_a_bridge():
    reasons = (Reason("no_progress_row", "no row"),)
    external = Harvest("REWORK", reasons, None, {"shrink": "?"}, 0.0)
    house = Harvest("REWORK", reasons, None, {"shrink": "same"}, 0.0)
    assert "CollectBridge" not in rework_message(external, 1, 2)
    assert "CollectBridge._shrink" in rework_message(house, 1, 2)
