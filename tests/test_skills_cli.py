"""SKILLS-1 — ``--skill`` reaches the controller and fails loudly when it can't.

Separate from ``test_skills_loader.py``: that file covers the loader in
isolation, this one covers the seam — the CLI flag, the signature chain
through ``run_auto``, and the point inside ``AutoController.__init__`` where
the overlay has to land.

The ordering constraint is the subtle part. The overlay must be applied after
``agents.ini`` is read (or there is nothing to overlay) and before
``normalize_task_mode`` runs (because the overlay is what SETS ``task_mode``).
Getting that wrong produces a skill that loads without error and then runs
under the wrong mechanics, which no other test would notice.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tools.auto.controller import AutoController
from tools.auto.gate_registry import resolve_gate_order

REPO_ROOT = Path(__file__).resolve().parent.parent
PROFILE_32K = str(REPO_ROOT / "agents_32k.ini")
PROFILE_8K = str(REPO_ROOT / "agents.ini")
HELLO_WORLD = str(REPO_ROOT / "examples" / "hello-world")


def _controller(skill: str | None, config: str = PROFILE_32K) -> AutoController:
    return AutoController(
        goal="exercise the flow",
        base_dir=HELLO_WORLD,
        config_path=config,
        skill=skill,
        dry_run=True,
    )


# ── the seam ─────────────────────────────────────────────────────────────────

def test_no_skill_leaves_behaviour_untouched():
    """The flag is opt-in — omitting it must change nothing.

    Asserted against the profile's OWN ``[auto] task_mode`` rather than a
    literal, because the shipped profiles disagree (agents.ini is code,
    agents_32k.ini is creative). Hardcoding one here would test the profile,
    not the flag.
    """
    controller = _controller(None)
    expected = controller.config.get("auto", "task_mode", fallback="code")
    assert controller.task_mode == expected


@pytest.mark.parametrize(
    "skill,expected_mode",
    [("hello-code", "code"), ("hello-docs", "docs"), ("hello-creative", "creative")],
)
def test_skill_base_becomes_the_task_mode(skill, expected_mode):
    """The overlay must land BEFORE normalize_task_mode reads the value."""
    assert _controller(skill).task_mode == expected_mode


def test_controller_exposes_the_loaded_skill():
    controller = _controller("hello-code")
    assert controller.skill is not None
    assert controller.skill.name == "hello-code"


def test_skill_is_none_when_not_requested():
    assert _controller(None).skill is None


def test_creative_skill_reaches_the_gate_registry():
    """End-to-end: adapter [gates] -> config -> resolve_gate_order."""
    controller = _controller("hello-creative")
    order = [g.name for g in resolve_gate_order(controller.config, controller.task_mode)]
    assert order == ["canon", "continuity"]


def test_skill_prompt_reaches_the_coder_section():
    controller = _controller("hello-docs")
    assert "Small-Project Documentation" in controller.config.get("coder", "system_docs")


# ── loud failure ─────────────────────────────────────────────────────────────

def test_unknown_skill_raises_and_names_the_alternatives():
    """Never degrade into 'run without it' — that produces a plausible-looking
    run against entirely the wrong prompts."""
    with pytest.raises(ValueError) as exc:
        _controller("does-not-exist")
    message = str(exc.value)
    assert "does-not-exist" in message
    assert "hello-code" in message


def test_skill_too_large_for_the_profile_raises():
    with pytest.raises(ValueError) as exc:
        _controller("hello-code", config=PROFILE_8K)
    assert "num_ctx" in str(exc.value)


def test_failure_message_carries_the_flag_name():
    """The user typed --skill; the error should say so."""
    with pytest.raises(ValueError) as exc:
        _controller("nope")
    assert "--skill" in str(exc.value)


# ── CLI surface ──────────────────────────────────────────────────────────────

def _cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "main.py", *args],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=120,
    )


def test_list_skills_prints_the_shipped_adapters():
    result = _cli("--list-skills")
    assert result.returncode == 0, result.stdout + result.stderr
    printed = set(result.stdout.split())
    assert {"hello-code", "hello-docs", "hello-creative"} <= printed


def test_skill_flag_is_documented_in_help():
    result = _cli("--help")
    assert "--skill" in result.stdout
    assert "--list-skills" in result.stdout
