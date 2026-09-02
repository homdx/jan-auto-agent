"""tests/test_auto_p3_profile_matrix.py — AUTO-P3: every shipped
``agents*.ini`` profile must load, construct a ClusterReviewer, and resolve
the AUTO-P keys sanely — including the profiles that never mention them.

Why this file exists
--------------------
AUTO-P adds five ``[architect]`` keys and one startup lint. The repo ships
nine window profiles (4k → 256k → 1M, plus two CPU-tuned 32k variants and a
stub profile), and only ONE of them was edited by AUTO-P3. Everything else
has to keep working through ``fallback=``. A key that raises ``NoSectionError``
on a profile without ``[architect]``, or a default that silently enables a
feature on a 4k window, would surface as a broken plan phase on somebody
else's machine and nowhere in CI.

The matrix also pins a fact worth knowing before turning the feature on:
``agents_128k.ini`` is the only shipped profile with
``[collect] use_in_auto = true``, so it is the only one where a probe can
resolve anything today. On every other profile ``probe_enabled = true``
would produce probes that fall straight through to the forced call — which
is exactly the trap ``_lint_probe_config`` warns about.

  AC-P3M-1  Every profile parses and resolves all five AUTO-P keys.
  AC-P3M-2  Every profile defaults to probe_enabled = false.
  AC-P3M-3  ClusterReviewer constructs on every profile without raising.
  AC-P3M-4  Probe force-enabled on a small-window profile warns.
  AC-P3M-5  Probe force-enabled without collect warns.
  AC-P3M-6  agents_128k.ini — the one profile with a live collect artifact —
            warns about NEITHER trap when the probe is enabled.
  AC-P3M-7  A config with no [architect] section at all resolves defaults
            instead of raising NoSectionError.
  AC-P3M-8  probe_allowed_ops = <empty> fails closed (no ops), rather than
            silently restoring the "facts" default.
  AC-P3M-9  Every profile's probe budget FITS its own num_ctx (AUTO-P4c).
  AC-P3M-10 Budgets are internally coherent and scale with the window.
"""

from __future__ import annotations

import configparser
from pathlib import Path

import pytest

from tools.auto import arch_probe
from tools.auto.architect import ClusterReviewer
from tools.auto.controller import _lint_probe_config

REPO_ROOT = Path(__file__).resolve().parent.parent
PROFILES = sorted(p.name for p in REPO_ROOT.glob("agents*.ini"))

# Guards against a profile being added later and silently escaping the matrix.
_EXPECTED_MIN_PROFILES = 8


def _load(name: str) -> configparser.ConfigParser:
    c = configparser.ConfigParser()
    c.read(REPO_ROOT / name, encoding="utf-8")
    return c


def _reviewer(cfg: configparser.ConfigParser) -> ClusterReviewer:
    active = cfg.get("api", "active", fallback="local")
    sec = f"api_{active}"
    return ClusterReviewer(
        config=cfg,
        base_url=cfg.get(sec, "base_url", fallback="http://localhost:1337/v1"),
        api_key=cfg.get(sec, "api_key", fallback="test"),
        model=cfg.get(sec, "model", fallback="test-model"),
        api_format=cfg.get(sec, "api_format", fallback="openai"),
        verify_ssl=False,
    )


def test_matrix_is_not_empty() -> None:
    assert len(PROFILES) >= _EXPECTED_MIN_PROFILES, PROFILES


@pytest.mark.parametrize("profile", PROFILES)
class TestEveryProfile:

    def test_probe_keys_resolve(self, profile: str) -> None:
        """AC-P3M-1 / AC-P3M-2: fallbacks cover every profile AUTO-P3 did not
        edit, and none of them silently ships the feature on."""
        cfg = _load(profile)
        assert cfg.getboolean("architect", "probe_enabled", fallback=False) is False
        assert cfg.getint("architect", "probe_max_rounds", fallback=1) >= 0
        assert cfg.getint("architect", "probe_max_chars", fallback=2000) > 0
        assert cfg.getint("architect", "probe_max_total_chars", fallback=6000) > 0
        assert cfg.get("architect", "probe_allowed_ops", fallback="facts")

    def test_reviewer_constructs(self, profile: str) -> None:
        """AC-P3M-3: the new __init__ reads must not raise on any profile."""
        r = _reviewer(_load(profile))
        assert r._probe_enabled is False
        assert r._probe is None and r._probe_built is False

    def test_lint_is_silent_as_shipped(self, profile: str) -> None:
        """Nothing ships with the probe on, so nothing should nag."""
        assert _lint_probe_config(_load(profile)) == []

    def test_force_enabled_lint_matches_the_profile(self, profile: str) -> None:
        """AC-P3M-4 / AC-P3M-5 / AC-P3M-6: turning the probe on must produce
        exactly the warnings that profile deserves — the small-window trap,
        the no-collect trap, or (128k) neither."""
        cfg = _load(profile)
        cfg.set("architect", "probe_enabled", "true")
        warnings = _lint_probe_config(cfg)
        text = " ".join(warnings)

        active = cfg.get("api", "active", fallback="local")
        num_ctx = cfg.getint(f"api_{active}", "num_ctx", fallback=0)
        has_collect = cfg.getboolean("collect", "use_in_auto", fallback=False)

        assert ("use_in_auto" in text) is (not has_collect)
        assert ("num_ctx=" in text) is bool(0 < num_ctx < 8192)


# Chars-per-token used for the headroom arithmetic. Deliberately optimistic
# (real English/code prose runs ~3.5–4); a budget that fails this check would
# fail worse in practice.
_CPT = 3.5

# What a probe round adds on top of the architect prompt, beyond the digest.
_INSTR_CHARS = len(arch_probe.PROBE_INSTRUCTIONS)


@pytest.mark.parametrize("profile", PROFILES)
def test_probe_budget_fits_the_window(profile: str) -> None:
    """AC-P3M-9 (AUTO-P4c): a profile whose probe budget cannot fit its own
    context window is a latent truncation bug, not a config preference.

    The probe appends PROBE_INSTRUCTIONS to every architect prompt and, on a
    firing batch, up to probe_max_total_chars of digest on top. That has to
    coexist with the file contents the architect is actually reviewing
    (max_files_per_review x max_file_chars), the prompt template, and the
    max_tokens reserved for the reply.

    This is the check that was missing when the AUTO-P defaults (6 000-char
    digest) were copied unchanged into profiles with an 8 192- or 4 096-token
    window. Anyone retuning a window — changing num_ctx, max_file_chars or
    max_files_per_review — trips this before a run does.
    """
    cfg = _load(profile)
    active = cfg.get("api", "active", fallback="local")
    num_ctx = cfg.getint(f"api_{active}", "num_ctx", fallback=0)
    if num_ctx <= 0:
        pytest.skip(f"{profile}: num_ctx not set — server default, nothing to check")

    files = cfg.getint("architect", "max_files_per_review", fallback=6)
    file_chars = cfg.getint("architect", "max_file_chars", fallback=4000)
    reply = cfg.getint("architect", "max_tokens", fallback=2048)
    digest = cfg.getint("architect", "probe_max_total_chars", fallback=6000)

    # +4000 chars for the prompt template, file listing and goal.
    prompt_tok = (files * file_chars + 4000) / _CPT
    probe_tok = (_INSTR_CHARS + digest) / _CPT
    total = prompt_tok + probe_tok + reply

    assert total <= num_ctx, (
        f"{profile}: a probe round needs ~{total:.0f} tokens but num_ctx is "
        f"{num_ctx}. Lower probe_max_total_chars (currently {digest}), "
        f"max_file_chars ({file_chars}) or max_files_per_review ({files})."
    )


@pytest.mark.parametrize("profile", PROFILES)
def test_probe_budgets_are_coherent(profile: str) -> None:
    """AC-P3M-10: internal consistency of the three budget keys.

    max_total_chars below max_chars is silently clamped up by ArchProbe
    (`max(max_chars, max_total_chars)`), so a profile written that way does
    not do what it says. Rounds must be at least 1 — 0 disables probing
    through the back door while probe_enabled still reads true.
    """
    cfg = _load(profile)
    if not cfg.has_option("architect", "probe_max_rounds"):
        pytest.skip(f"{profile}: no explicit AUTO-P block — fallbacks apply")

    rounds = cfg.getint("architect", "probe_max_rounds")
    per_op = cfg.getint("architect", "probe_max_chars")
    total = cfg.getint("architect", "probe_max_total_chars")

    assert rounds >= 1, "0 rounds disables probing without saying so"
    # AUTO-P13 revision: this used to read "<= 5, no measured run ever
    # needed more than 3 rounds". A live run against agents_128k.ini's
    # actual (remote, 1M-token) endpoint needed all 5 of its then-configured
    # rounds and still hit the cap mid-batch with escalation budget unused
    # — the opposite of what that comment claimed. 10 is a sanity ceiling
    # against a runaway typo (e.g. an extra zero), not a measured maximum;
    # see agents_128k.ini's own probe_max_rounds comment for the specific
    # figure this repo has actually observed needing more than 3.
    assert rounds <= 10, "probe_max_rounds looks like a runaway value (>10)"
    assert per_op > 0
    assert total >= per_op, (
        f"{profile}: probe_max_total_chars ({total}) < probe_max_chars "
        f"({per_op}) — ArchProbe clamps the total up, so this profile does "
        f"not do what it says"
    )


def test_every_profile_declares_its_probe_budget() -> None:
    """AUTO-P4c: fallbacks exist, but a profile that relies on them silently
    inherits budgets tuned for a different window. Every shipped profile
    states its own, so the numbers can be reviewed next to the num_ctx they
    were chosen for.

    agents_128k.ini is the documented exception — see the patch notes: it is
    the operator's live, hand-tuned profile and is deliberately left alone.
    """
    missing = [
        p for p in PROFILES
        if not _load(p).has_option("architect", "probe_max_rounds")
    ]
    # agents_128k.ini used to be the documented exception — the operator's
    # live, hand-tuned profile, deliberately skipped by every AUTO-P patch.
    # It has since been removed from the repository (it carried credentials
    # and now lives only locally), so the exception list is allowed to be
    # empty. Written as a subset check rather than equality so the test
    # survives the profile coming back without needing another edit.
    assert set(missing) <= {"agents_128k.ini"}, (
        f"profiles without an explicit AUTO-P budget block: {missing}"
    )


def test_no_profile_is_silently_probe_ready() -> None:
    """AC-P3M-6, stated as a fact rather than as a per-profile assertion,
    because it is the single most useful thing to know before enabling the
    feature: only agents_128k.ini has a collect artifact for the probe to
    read. Enabling it anywhere else today produces probes that resolve
    nothing and fall through to the forced call every time.

    If a future change turns [collect] use_in_auto on in another profile,
    this test fails and the runbook needs updating — that is the point.
    """
    ready = [
        p for p in PROFILES
        if _load(p).getboolean("collect", "use_in_auto", fallback=False)
    ]
    # agents_128k.ini was the only shipped profile with a collect artifact,
    # and it is no longer in the repository (see the note above). What this
    # test guards is unchanged and still worth guarding: NO OTHER profile may
    # quietly become probe-ready, because enabling probing where collect is
    # off produces lookups that resolve nothing and fall straight through to
    # the forced call — a measurement of the plumbing, not of the feature.
    assert set(ready) <= {"agents_128k.ini"}, (
        f"probe-ready profiles changed: {ready}. Update AUTO-P-RUNBOOK.md "
        f"and re-check the enabling instructions in README.md."
    )


def test_no_architect_section_at_all() -> None:
    """AC-P3M-7: configparser's fallback= covers NoSectionError as well as
    NoOptionError — assert it, because the whole no-edit-needed story for the
    other eight profiles rests on it."""
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "api":       {"active": "local"},
        "api_local": {"base_url": "http://x/v1", "api_key": "k", "model": "m"},
    })
    r = _reviewer(cfg)
    assert r._probe_enabled is False
    assert _lint_probe_config(cfg) == []


def test_empty_allowed_ops_fails_closed() -> None:
    """AC-P3M-8: `probe_allowed_ops =` means "allow nothing". Restoring the
    default would re-enable a probe the operator just switched off."""
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "api":       {"active": "local"},
        "api_local": {"base_url": "http://x/v1", "api_key": "k", "model": "m"},
        "architect": {"probe_enabled": "true", "probe_allowed_ops": ""},
    })
    r = _reviewer(cfg)
    assert r._probe_allowed_ops == ()
    assert arch_probe.extract_probe_request(
        "ARCH_PROBE: facts fn", allowed_ops=r._probe_allowed_ops
    ) == []
