"""tools/auto/arch_probe.py — AUTO-P: architect context probe protocol.

The Architect plans against a fixed slice of the repository
(``[architect] max_files_per_review`` files × ``max_file_chars`` each) and
has no channel to say *"I need to see X before I can plan this"*.  AUTO-P
adds one: the Architect may reply with a single line

    ARCH_PROBE: facts <name>, facts <name>

instead of the JSON task array, the harness resolves those read-only
lookups, appends a digest, and re-asks.

AUTO-P2 scope — what this file actually does today
--------------------------------------------------
**Protocol constants and the parser contract only.**
``extract_probe_request`` is a deliberate stub that always returns ``[]``,
so importing this module changes no behaviour anywhere.

That is the point.  AUTO-P2's real payload lives in ``architect.py``: an
``ARCH_PROBE:`` reply is non-JSON prose, so ``_parse_candidates_ex``
classifies it **unsalvageable** and the AUTO-H5 ladder fires — re-asking
the identical question up to ``empty_response_retry_max`` times at rising
``max_tokens``/``temperature``, while ignoring the request the model
actually made.  In the logs that reads as a flaky model; the feature simply
appears not to work.  AUTO-P2 installs the guard against that *before* any
parser exists to trigger it, so the guard can be proven correct against a
function that returns nothing, rather than against one that returns real
ops.

AUTO-P1 replaces the stub body with the real parser (dedup,
order-preserving, capped at ``_MAX_OPS``, mirroring
``coder.py::_extract_context_request``) and adds ``ArchProbe`` — the
read-only executor backed by ``CollectBridge`` — plus the probe→re-ask
loop.  Nothing in this file's public surface changes when that lands.

Gated by ``[architect] probe_enabled`` (absent ⇒ ``False`` ⇒ the parser is
never even called).
"""

from __future__ import annotations

from dataclasses import dataclass

# Recognised on its own line, at the end of an Architect response. Chosen to
# mirror coder.py's existing ``CONTEXT_REQUEST:`` convention rather than
# introduce a second protocol shape.
PROBE_PREFIX = "ARCH_PROBE:"

# Upper bound on ops honoured from a single request. Enforced by AUTO-P1's
# parser; declared here so the protocol's limits live with the protocol.
_MAX_OPS = 8


@dataclass(frozen=True)
class ProbeOp:
    """One read-only lookup the Architect asked for.

    ``op`` is the operation name (AUTO-P1 ships ``"facts"``; ``symbol`` /
    ``refs`` / ``read`` follow in Phase 1); ``arg`` is its single argument,
    normally a symbol name.
    """

    op: str
    arg: str

    def __str__(self) -> str:  # readable in logs and trace params
        return f"{self.op} {self.arg}".strip()


def extract_probe_request(text: str) -> list[ProbeOp]:
    """Parse a trailing ``ARCH_PROBE:`` line out of *text*.

    Returns the requested ops in order, or ``[]`` when *text* carries no
    well-formed probe request.  Never raises — a malformed request is not
    a probe, and the caller must fall through to its normal handling of an
    unparseable response.

    **AUTO-P2 stub: always returns ``[]``.**  AUTO-P1 implements the body.
    Tests that need to exercise the probe path monkeypatch this function
    (or, preferably, the module attribute ``arch_probe.extract_probe_request``,
    which is how ``architect.py`` reaches it).
    """
    return []
