"""GATES-2 — every shipped profile documents ``[gates]``, and none changes behaviour.

Nine ``agents*.ini`` profiles exist and drift apart easily: a knob added to
one and forgotten in the rest is invisible until somebody switches profile
and wonders why the run behaves differently. These tests pin two properties.

**Parity.** Every profile carries the ``[gates]`` documentation block, so
switching profiles never silently loses the knob's discoverability.

**Inertness.** The block is COMMENTED OUT everywhere. Documenting a feature
must not enable it — every profile must still resolve to the registry
default, which is the behaviour that predates GATES-2. If a profile ever
wants a live ``[gates]`` section, this test is where that decision gets
made explicit rather than slipping in as a doc change.
"""

from __future__ import annotations

import configparser
from pathlib import Path

import pytest

from tools.auto.gate_registry import GATES, GATES_BY_NAME, resolve_gate_order

REPO_ROOT = Path(__file__).resolve().parent.parent
PROFILES = sorted(REPO_ROOT.glob("agents*.ini"))

DEFAULT_CREATIVE = [s.name for s in GATES if "creative" in s.modes]


def _ids(paths):
    return [p.name for p in paths]


def test_profiles_are_discovered():
    """Guard against the glob silently matching nothing."""
    assert len(PROFILES) >= 8, f"expected the agents*.ini family, found {_ids(PROFILES)}"


@pytest.mark.parametrize("profile", PROFILES, ids=_ids(PROFILES))
def test_profile_documents_the_gates_section(profile: Path):
    """A knob nobody can find is a knob nobody uses."""
    text = profile.read_text(encoding="utf-8")
    assert "[gates]" in text, f"{profile.name} has no [gates] documentation"


@pytest.mark.parametrize("profile", PROFILES, ids=_ids(PROFILES))
def test_gates_section_is_commented_out(profile: Path):
    """Documenting the feature must not enable it."""
    cfg = configparser.ConfigParser()
    cfg.read(profile, encoding="utf-8")
    assert not cfg.has_section("gates"), (
        f"{profile.name} has a LIVE [gates] section. That is a behaviour "
        "change, not documentation — if it is intended, update this test."
    )


@pytest.mark.parametrize("profile", PROFILES, ids=_ids(PROFILES))
def test_profile_resolves_to_registry_default(profile: Path):
    """The end-to-end property the two tests above exist to protect."""
    cfg = configparser.ConfigParser()
    cfg.read(profile, encoding="utf-8")
    order = [s.name for s in resolve_gate_order(cfg, "creative")]
    assert order == DEFAULT_CREATIVE


@pytest.mark.parametrize("profile", PROFILES, ids=_ids(PROFILES))
def test_profile_still_parses(profile: Path):
    """A malformed comment block would break the whole profile, not just gates."""
    cfg = configparser.ConfigParser()
    cfg.read(profile, encoding="utf-8")
    assert cfg.sections(), f"{profile.name} parsed to zero sections"


@pytest.mark.parametrize("profile", PROFILES, ids=_ids(PROFILES))
def test_documented_gate_names_are_real(profile: Path):
    """A doc block naming a gate that doesn't exist is worse than none."""
    text = profile.read_text(encoding="utf-8")
    marker = "Known gates:"
    line = next((l for l in text.splitlines() if marker in l), None)
    assert line is not None, f"{profile.name} lost its 'Known gates:' line"
    named = {n.strip(" .") for n in line.split(marker, 1)[1].split(",")}
    assert named == set(GATES_BY_NAME), (
        f"{profile.name} documents {sorted(named)}, registry has "
        f"{sorted(GATES_BY_NAME)}"
    )


@pytest.mark.parametrize("profile", PROFILES, ids=_ids(PROFILES))
def test_suggested_orders_reference_real_gates(profile: Path):
    """Commented `#   creative = a, b` suggestions must stay valid too."""
    for raw in profile.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line.startswith("#"):
            continue
        body = line.lstrip("#").strip()
        if not body.startswith("creative ") and not body.startswith("creative="):
            continue
        _, _, value = body.partition("=")
        for name in (n.strip() for n in value.split(",")):
            if name:
                assert name in GATES_BY_NAME, (
                    f"{profile.name} suggests unknown gate {name!r} in: {line}"
                )
