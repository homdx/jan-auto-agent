"""tools/auto/llm_profile.py — GATE3-PROFILE-1: shared LLM-profile resolver.

Two call sites already solve "let this one caller use a different
provider/model than the shared one" independently:

  * ``[gate1] presence_llm_profile``           — Gate1Filter._resolve_presence_profile
  * ``[validator_agent] validator_llm_profile`` — tools.auto.inner_loop.resolve_validator_llm_profile

GATE3-PROFILE-2/3 need the same behaviour at seven more call sites (the
five Gate-3 validators, SummaryMemory, the plan validator) and a third and
fourth copy-paste of ~120 lines each is exactly the duplication this
module exists to stop. ``resolve_llm_profile`` extracts the shared logic;
callers keep whatever field names they already use and adapt through
:class:`LlmSettings`.

Was deliberately NOT wired into gate1_filter.py or inner_loop.py when this
module was introduced — see GATE3-PROFILE-6's original reasoning: doing
that migration in the same change that introduces the helper would make
any regression impossible to bisect between "the helper is wrong" and
"the migration is wrong". GATE3-PROFILE-6 has since landed (see
gate1_filter.py's own ``resolve_llm_profile`` import and
inner_loop.py's ``resolve_validator_llm_profile``) — both call sites use
this module directly now.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Optional

logger = logging.getLogger(__name__)

__all__ = ["LlmSettings", "resolve_llm_profile"]


@dataclass(frozen=True)
class LlmSettings:
    """The full set of per-caller-tunable LLM settings.

    ``base_url`` / ``api_key`` / ``model`` are the three fields that are
    REQUIRED and NEVER inherited when a named profile is used — sending one
    provider's credential to another provider's URL must fail loudly at
    startup rather than silently at request time.

    Every other field is optional: when a named profile doesn't set it,
    the caller's own ``defaults`` value is used instead — never a
    hardcoded literal, and never another profile's value.
    """

    base_url: str
    api_key: str
    model: str
    api_format: str = "openai"
    verify_ssl: bool = True
    think: bool = False
    temperature: float = 0.0
    max_tokens: int = 1024
    num_ctx: int = 8192
    response_format: bool = False
    think_effort_enabled: bool = False
    think_effort: Optional[str] = None


# Optional fields, in the order they should be resolved/logged. Kept
# separate from LlmSettings' own field order so adding a future optional
# key only means appending here and to the dataclass — not touching the
# per-type parsing branches below.
_OPTIONAL_BOOL_FIELDS = ("verify_ssl", "think", "response_format")
_OPTIONAL_FLOAT_FIELDS = ("temperature",)
_OPTIONAL_INT_FIELDS = ("max_tokens", "num_ctx")
_OPTIONAL_STR_FIELDS = ("api_format",)
# think_effort_enabled/think_effort are handled together, as a pair, since
# think_effort only means anything when think_effort_enabled is true.


def resolve_llm_profile(
    config, section: str, key: str, *, defaults: LlmSettings
) -> tuple[LlmSettings, Optional[str]]:
    """Resolve an optional named-profile override of *defaults*.

    Parameters
    ----------
    config:
        A ``configparser.ConfigParser`` (or compatible) already loaded.
    section, key:
        Where the profile NAME (not the profile's own settings) lives,
        e.g. ``section="validator_agent", key="canon_llm_profile"``.
        ``config.get(section, key, fallback="")`` — an unset or empty
        value is treated as "no profile", never as a profile named ``""``.
    defaults:
        The settings to use when no profile is configured, and the
        fallback source for every optional field a configured profile
        doesn't itself set.

    Returns
    -------
    (settings, profile_name):
        ``settings`` is a new :class:`LlmSettings` (never a mutation of
        *defaults* — two different profiles read in the same process
        never share any value, since each resolution starts fresh from
        *defaults* and reads only its own section). ``profile_name`` is
        ``None`` when unset, else the resolved section name.

    Raises
    ------
    ValueError:
        The named section doesn't exist, or exists but is missing one of
        base_url / api_key / model. Both raise at call time — i.e. at
        construction, not at request time — so a misconfigured profile is
        never discovered mid-run.
    """
    profile_name = config.get(section, key, fallback="").strip()

    if not profile_name:
        logger.info(
            "%s.%s: provider = %s (%s) — shared provider (no %s configured)",
            section, key, defaults.base_url, defaults.model, key,
        )
        return defaults, None

    if not config.has_section(profile_name):
        raise ValueError(
            f"[{section}] {key} = {profile_name!r} but the config has no "
            f"[{profile_name}] section. Add one with at least "
            f"base_url/api_key/model, or remove {key} to use the shared "
            f"provider."
        )

    missing = [
        opt for opt in ("base_url", "api_key", "model")
        if not config.has_option(profile_name, opt)
    ]
    if missing:
        raise ValueError(
            f"[{profile_name}] ({key}) is missing required option(s): "
            f"{', '.join(missing)}. A profile must fully specify its own "
            f"connection details — it never silently inherits "
            f"base_url/api_key/model from the shared provider, since that "
            f"could send a different provider's credential to the wrong "
            f"host."
        )

    overrides: dict = {
        "base_url": config.get(profile_name, "base_url").rstrip("/"),
        "api_key": config.get(profile_name, "api_key"),
        "model": config.get(profile_name, "model"),
    }

    for field_name in _OPTIONAL_STR_FIELDS:
        overrides[field_name] = config.get(
            profile_name, field_name, fallback=getattr(defaults, field_name)
        )

    for field_name in _OPTIONAL_BOOL_FIELDS:
        default_value = getattr(defaults, field_name)
        if config.has_option(profile_name, field_name):
            try:
                overrides[field_name] = config.getboolean(profile_name, field_name)
            except ValueError:
                logger.warning(
                    "[%s] %s invalid — keeping default %s",
                    profile_name, field_name, default_value,
                )
                overrides[field_name] = default_value
        else:
            overrides[field_name] = default_value

    for field_name in _OPTIONAL_FLOAT_FIELDS:
        default_value = getattr(defaults, field_name)
        raw = config.get(profile_name, field_name, fallback=None)
        if raw is None:
            overrides[field_name] = default_value
        else:
            try:
                overrides[field_name] = float(raw)
            except ValueError:
                logger.warning(
                    "[%s] %s invalid — keeping default %s",
                    profile_name, field_name, default_value,
                )
                overrides[field_name] = default_value

    for field_name in _OPTIONAL_INT_FIELDS:
        default_value = getattr(defaults, field_name)
        if config.has_option(profile_name, field_name):
            try:
                overrides[field_name] = config.getint(profile_name, field_name)
            except ValueError:
                logger.warning(
                    "[%s] %s invalid — keeping default %s",
                    profile_name, field_name, default_value,
                )
                overrides[field_name] = default_value
        else:
            overrides[field_name] = default_value

    if config.has_option(profile_name, "think_effort_enabled"):
        try:
            effort_enabled = config.getboolean(profile_name, "think_effort_enabled")
        except ValueError:
            logger.warning(
                "[%s] think_effort_enabled invalid — keeping default %s",
                profile_name, defaults.think_effort_enabled,
            )
            effort_enabled = defaults.think_effort_enabled
        overrides["think_effort_enabled"] = effort_enabled
        overrides["think_effort"] = (
            config.get(profile_name, "think_effort", fallback="").strip() or None
            if effort_enabled else None
        )
    else:
        overrides["think_effort_enabled"] = defaults.think_effort_enabled
        overrides["think_effort"] = defaults.think_effort

    settings = replace(defaults, **overrides)

    logger.info(
        "%s.%s: provider = %s (%s) — via %s = [%s]",
        section, key, settings.base_url, settings.model, key, profile_name,
    )
    return settings, profile_name
