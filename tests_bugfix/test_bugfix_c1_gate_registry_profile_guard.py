"""C1 — gate registry must not abort the whole builder on a malformed profile.

``build_validators`` resolves each enabled gate's ``<gate>_llm_profile``
through :func:`tools.auto.llm_profile.resolve_llm_profile`, which raises
``ValueError`` when the named profile section is missing or incomplete.
That call sat *outside* the per-gate ``try/except`` (the ``try`` began only
at the ``__import__`` line below it), so one bad profile propagated out of
``make_inner_loop`` and killed the entire ``--auto`` run instead of
gracefully disabling a single gate.

The fix moves the profile resolution inside the existing ``try`` block, so a
malformed profile disables only that gate — the fail-open architecture the
registry exists to provide.
"""

from __future__ import annotations

import configparser
from pathlib import Path

from tools.auto.gate_registry import build_validators

_SHARED = (
    "[api]\n"
    "active = local\n"
    "[api_local]\n"
    "base_url = https://shared.example/v1\n"
    "api_key = shared-key\n"
    "model = shared-model\n"
)


def _cfg(extra: str) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    cfg.read_string(_SHARED + extra)
    return cfg


def test_missing_profile_section_disables_the_gate_instead_of_raising(tmp_path: Path) -> None:
    """Before the fix this raised ``ValueError`` out of ``build_validators``."""
    cfg = _cfg(
        "[validator_agent]\n"
        "canon_check_every = 1\n"
        "canon_llm_profile = does_not_exist\n"
    )
    out = build_validators(cfg, tmp_path, task_mode="code")
    assert out["canon_validator"] is None


def test_incomplete_profile_section_disables_the_gate(tmp_path: Path) -> None:
    """An existing-but-incomplete profile (no api_key/model) raises from
    ``resolve_llm_profile`` too; the guard must cover that path as well."""
    cfg = _cfg(
        "[validator_agent]\n"
        "canon_check_every = 1\n"
        "canon_llm_profile = broken\n"
        "[broken]\n"
        "base_url = https://broken.example/v1\n"
    )
    out = build_validators(cfg, tmp_path, task_mode="code")
    assert out["canon_validator"] is None


def test_other_gates_still_build_when_one_profile_is_bad(tmp_path: Path) -> None:
    """The fail-open contract: a bad profile disables one gate, not all of
    them. ``delta_validator`` has no profile_key, so it is untouched by the
    broken ``canon_llm_profile`` and must still be constructed."""
    cfg = _cfg(
        "[validator_agent]\n"
        "canon_check_every = 1\n"
        "canon_llm_profile = does_not_exist\n"
    )
    out = build_validators(cfg, tmp_path, task_mode="creative")
    assert out["canon_validator"] is None
    assert out["delta_validator"] is not None
