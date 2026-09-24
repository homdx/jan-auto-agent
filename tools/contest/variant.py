"""KC-49 — the reasoning variant a roster model runs at, and ``highest``.

Kilo 7.6.2 lists per model, in ``GET /provider``, the reasoning variants it
will accept on ``POST /session``'s ``model.variant`` and on ``prompt_async``'s
``variant`` — ``low``/``medium``/``high`` (``reasoningEffort``) for every
``kenary`` and ``sensenova123`` model, and ``none``…``max`` for a few. A
listed variant is not a working one: ``kenary/glm-4-7-flash:free`` lists
``max`` and its provider answers a ``max`` request with HTTP 400 ("the model's
provider rejected the request"), while ``high`` answers.

So a variant is spelled one of three ways:

* a **name** (``high``, ``max``, …) — checked against the listed variants at
  intake, refused with the list when it is not there, and sent as is;
* ``highest`` — resolved at intake by asking the model to ``say: hello``,
  one fresh session per rung, from the top of :data:`VARIANT_RANK` down
  through the variants the model lists, then with no variant at all. The
  first rung that answers is the one the round runs at. A model that lists
  no variants is not asked at all — there is nothing to choose. Nothing is
  cached: KC-11 adds the cache and its TTL on top of this;
* ``default`` — no variant is sent: the provider's own default.

``highest`` is the round's default (``[contest] variant``); ``--variant`` and
an agent's own ``@variant`` / ``variant =`` override it.

:func:`listed_variants`, :func:`ladder` and :func:`pick_variant` are pure;
:func:`hello_probe` is the one piece that talks to a server.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field

__all__ = [
    "DEFAULT",
    "HIGHEST",
    "VARIANT_RANK",
    "VariantPick",
    "hello_probe",
    "ladder",
    "listed_variants",
    "needs_login",
    "pick_variant",
]

_LOG = logging.getLogger(__name__)

#: The spelling that asks intake to find the top working variant — also the
#: round's default (``[contest] variant``).
HIGHEST = "highest"

#: The spelling that sends no variant at all: the provider's own default.
DEFAULT = "default"

#: Every variant name Kilo 7.6.2 was seen to list, strongest first. A listed
#: name outside this tuple has no known place on the ladder, so ``highest``
#: never tries it — it can still be named explicitly.
VARIANT_RANK = ("max", "xhigh", "high", "medium", "low", "minimal", "none")

#: What the probe asks for, and how long one rung may take.
HELLO_PROMPT = "say: hello"
HELLO_TIMEOUT_SEC = 60.0

#: A probe session may do nothing but answer: every permission is denied.
_DENY_ALL = [{"permission": "*", "pattern": "*", "action": "deny"}]


@dataclass(frozen=True)
class VariantPick:
    """The outcome of :func:`pick_variant`.

    ``variant`` is the rung that answered — ``None`` both for "no variant
    answered but the plain request did" and for "nothing answered"; ``usable``
    tells them apart. ``tried`` is every rung that failed before it, in order,
    as ``(variant, reason)`` with ``None`` for the plain request.
    """

    variant: str | None
    usable: bool
    tried: tuple = field(default_factory=tuple)

    def describe(self) -> str:
        """``high`` / ``(none)``, then the failed rungs: one line for the plan."""
        head = self.variant if self.variant is not None else "(none)"
        if not self.usable:
            head = "unusable"
        if not self.tried:
            return head
        rungs = "; ".join(f"{rung or '(none)'}: {reason}" for rung, reason in self.tried)
        return f"{head} (failed: {rungs})"


#: The words a provider's refusal uses when the credentials are the problem —
#: `bynara` says "A valid API key is required.", `kilo` "You need to sign in to
#: use this model." Matched lower-cased, as substrings.
LOGIN_MARKERS = ("api key", "api_key", "apikey", "sign in", "log in", "login",
                 "unauthorized", "unauthenticated", "authentication", "401")


def needs_login(pick: VariantPick) -> bool:
    """True when every rung of an unusable *pick* was refused for credentials:
    the fix is `kilo auth login`, not another model or variant."""
    return (not pick.usable and bool(pick.tried)
            and all(any(m in reason.lower() for m in LOGIN_MARKERS)
                    for _, reason in pick.tried))


def listed_variants(providers: dict, provider_id: str, model_id: str) -> list:
    """The variant names ``GET /provider`` lists for one model, in its order.

    ``[]`` for a provider or model that is not there, a model with no
    ``variants`` map, or a body that is not the shape 7.6.2 sends.
    """
    for provider in (providers or {}).get("all") or []:
        if not isinstance(provider, dict) or provider.get("id") != provider_id:
            continue
        model = (provider.get("models") or {}).get(model_id)
        if not isinstance(model, dict):
            return []
        variants = model.get("variants")
        if not isinstance(variants, dict):
            return []
        return [name for name in variants if isinstance(name, str) and name]
    return []


def ladder(listed) -> list:
    """The rungs ``highest`` tries: the listed names :data:`VARIANT_RANK` knows,
    strongest first, then ``None`` — the request with no variant at all."""
    known = [name for name in VARIANT_RANK if name in set(listed or ())]
    return known + [None]


def pick_variant(rungs, try_one: Callable[[str | None], str | None]) -> VariantPick:
    """Walk *rungs* in order; the first one ``try_one`` accepts wins.

    ``try_one(rung)`` returns ``None`` when the model answered at that rung,
    or a short reason when it did not. An exception from it is a failed rung
    with the exception as its reason — one bad rung never stops the walk.
    """
    tried = []
    for rung in rungs:
        try:
            reason = try_one(rung)
        except Exception as exc:  # noqa: BLE001 — a rung that blew up is a rung that failed
            reason = f"{type(exc).__name__}: {exc}"
        if reason is None:
            return VariantPick(variant=rung, usable=True, tried=tuple(tried))
        tried.append((rung, str(reason)))
    return VariantPick(variant=None, usable=False, tried=tuple(tried))


def _error_text(error) -> str:
    """The message of a ``session.error`` payload, one line, quotes stripped."""
    message = ""
    if isinstance(error, dict):
        data = error.get("data")
        if isinstance(data, dict) and isinstance(data.get("message"), str):
            message = data["message"]
        elif isinstance(error.get("message"), str):
            message = error["message"]
        elif isinstance(error.get("name"), str):
            message = error["name"]
    elif error is not None:
        message = str(error)
    message = " ".join(message.split()).strip().strip('"')
    return message[:160] or "session.error"


def hello_probe(server, provider_id: str, model_id: str, *,
                timeout: float = HELLO_TIMEOUT_SEC) -> Callable[[str | None], str | None]:
    """``try_one`` for :func:`pick_variant` against a live Kilo *server*.

    Each call opens a fresh session in a throwaway directory with every
    permission denied, prompts :data:`HELLO_PROMPT` at that variant, waits for
    idle, and deletes the session: a probe session never becomes anyone's
    context. The answer counts when the turn went idle with no
    ``session.error`` and a non-empty assistant text.
    """
    # imported here so the pure half of this module needs no server code
    from .backend import _wait_for_stream
    from .kilo_client import EventTap, KiloClient, KiloHttpError

    def try_one(variant):
        directory = tempfile.mkdtemp(prefix="kilo-variant-")
        tap = None
        session = None
        client = KiloClient(server, directory)
        try:
            tap = EventTap(server.base_url, directory,
                           os.path.join(directory, "events.jsonl")).start()
            _wait_for_stream(tap)
            title = f"variant-probe/{model_id}/{variant or 'none'}"
            try:
                session = client.create_session(provider_id, model_id, rules=_DENY_ALL,
                                                title=title, variant=variant)
                client.prompt(session, HELLO_PROMPT)
            except KiloHttpError as exc:
                return f"HTTP {exc}"
            idle = client.wait_idle(tap, session, timeout,
                                    on_permission=lambda event: ("reject", "probe"),
                                    on_question=lambda event: None)
            if idle.status == "error":
                return _error_text(idle.error)
            if idle.status != "idle":
                return f"no answer ({idle.status})"
            if not client.last_assistant_text(session):
                return "empty reply"
            return None
        finally:
            if session is not None:
                try:
                    client.delete_session(session)
                except Exception as exc:  # noqa: BLE001 — a leftover probe session is harmless
                    _LOG.debug("probe session %s not deleted: %s", session.id, exc)
            if tap is not None:
                tap.stop()
                tap.join(2.0)
            shutil.rmtree(directory, ignore_errors=True)

    return try_one
