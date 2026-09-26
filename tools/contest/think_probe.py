"""KC-11 — probe cache for reasoning-variant discovery.

:func:`probe_model` wraps KC-49's ``hello_probe`` / ``pick_variant`` pair with
a JSON cache (``contest-probe.json`` next to ``contest.ini``) so each
``provider/model`` is asked at most once per ``probe_ttl_days`` days on the
same Kilo version. :func:`load_probe_cache` / :func:`save_probe_cache` handle
the file; :data:`VARIANT_LADDER` names the order ``highest`` tries.

The cache key is ``"<provider_id>/<model_id>"``. A roster-set variant (not
``highest``) is checked but not saved: the operator already committed to a
name, so the cache is not the source of truth.

Git-ignores ``contest-probe.json`` — the file is local to the operator's
machine and changes every time a variant probe succeeds.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field

__all__ = [
    "ProbeResult",
    "VARIANT_LADDER",
    "load_probe_cache",
    "probe_model",
    "save_probe_cache",
]

_LOG = logging.getLogger(__name__)

#: The order ``highest`` tries, strongest first. From KC-49's ``VARIANT_RANK``
#: minus ``none`` (not a reasoning escalation) plus ``None`` (no variant).
VARIANT_LADDER = ("max", "xhigh", "high", "medium", "low", "minimal", "none", None)

#: Default TTL: re-probe after this many days even when no ``--reprobe`` is
#: given and the Kilo version has not changed.
_DEFAULT_TTL_DAYS = 7

#: Name of the cache file, placed next to ``contest.ini``.
PROBE_CACHE_FILE = "contest-probe.json"


@dataclass
class ProbeResult:
    """The outcome of :func:`probe_model`.

    ``variant`` is the highest rung that answered, ``None`` when the plain
    (no-variant) request won or when nothing answered. ``usable`` is ``False``
    only when every rung failed. ``tried`` is the rungs that failed before the
    winner, as ``[(variant_or_None, reason), …]``. ``elapsed`` is wall-clock
    seconds for the winning probe (0.0 when the cache answered). ``reasoning_tokens``
    is the token count from the winning session's last assistant message (0 when
    the cache answered or the server did not report it).
    """

    variant: str | None
    usable: bool
    tried: list = field(default_factory=list)
    elapsed: float = 0.0
    reasoning_tokens: int = 0


def load_probe_cache(cache_path: str) -> dict:
    """Load ``contest-probe.json`` from *cache_path*.

    Returns ``{}`` when the file does not exist or is malformed — a missing or
    corrupt cache is not an error: the probe runs and the file is rebuilt.
    """
    try:
        with open(cache_path, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def save_probe_cache(cache_path: str, cache: dict) -> None:
    """Write *cache* to *cache_path* atomically (write-then-rename)."""
    tmp = cache_path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cache, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, cache_path)
    except OSError as exc:
        _LOG.warning("could not save probe cache to %s: %s", cache_path, exc)
        try:
            os.unlink(tmp)
        except OSError:
            pass


def probe_model(
    client_factory,
    provider_id: str,
    model_id: str,
    listed: list,
    *,
    cache: dict | None = None,
    cache_path: str | None = None,
    kilo_version: str = "",
    ttl_days: int = _DEFAULT_TTL_DAYS,
    reprobe: bool = False,
) -> ProbeResult:
    """Find the highest reasoning variant that answers for *provider_id*/*model_id*.

    *client_factory(variant)* is a ``try_one`` callable as :func:`variant.pick_variant`
    expects: call it with each rung and it returns ``None`` on success or a short
    reason string on failure. It is called in ladder order — highest first — and
    the first success wins.

    *listed* is the variant names ``GET /provider`` reports for the model
    (from :func:`variant.listed_variants`). When *listed* is empty the model
    has no reasoning capability: the result is ``ProbeResult(variant=None,
    usable=False, tried=[])`` immediately and no session is created.

    When *cache* (a dict already loaded from *cache_path*) holds a fresh entry
    (``probed_at`` within *ttl_days*, same *kilo_version* when non-empty) and
    *reprobe* is ``False``, the cached result is returned and no session is
    opened.

    A cache hit returns ``elapsed=0.0`` and ``reasoning_tokens=0``; the cached
    ``tried`` list is restored as recorded. The cache is updated in-place when a
    live probe runs, and written to *cache_path* when *cache_path* is given.
    """
    from .variant import ladder as build_ladder, pick_variant

    key = f"{provider_id}/{model_id}"

    # No reasoning capability → nothing to probe.
    if not listed:
        return ProbeResult(variant=None, usable=False)

    now = time.time()

    # Cache check.
    if not reprobe and cache is not None:
        entry = cache.get(key)
        if isinstance(entry, dict):
            age_days = (now - float(entry.get("probed_at", 0))) / 86400.0
            cached_version = entry.get("kilo_version", "")
            version_match = (not kilo_version) or (cached_version == kilo_version)
            if age_days < ttl_days and version_match:
                _LOG.debug("probe cache hit for %s (age %.1fd)", key, age_days)
                tried = [tuple(t) for t in (entry.get("tried") or [])]
                return ProbeResult(
                    variant=entry.get("variant"),
                    usable=entry.get("usable", entry.get("variant") is not None),
                    tried=tried,
                    elapsed=0.0,
                    reasoning_tokens=0,
                )

    # Live probe.
    rungs = build_ladder(listed)
    t0 = time.monotonic()
    pick = pick_variant(rungs, client_factory)
    elapsed = time.monotonic() - t0

    result = ProbeResult(
        variant=pick.variant,
        usable=pick.usable,
        tried=list(pick.tried),
        elapsed=elapsed,
        reasoning_tokens=0,
    )

    # Update cache.
    if cache is not None:
        cache[key] = {
            "variant": pick.variant,
            "usable": pick.usable,
            "probed_at": now,
            "kilo_version": kilo_version,
            "tried": [[rung, reason] for rung, reason in pick.tried],
            "reasoning_tokens": 0,
        }
        if cache_path:
            save_probe_cache(cache_path, cache)

    return result
