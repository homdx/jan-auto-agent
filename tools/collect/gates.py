"""tools/collect/gates.py — COLLECT-15: GATES map.

A worklist, not a discovery process: this codebase has exactly seven
quality gates in its `auto`/creative pipeline (`gate1`, `verdict`,
`continuity`, `theme`, `fact`, `canon`, `language`), and no amount of AST
walking invents a new one — a gate is a design decision, not a structural
pattern COLLECT-4/6/7 could infer. So, like COLLECT-10's `contracts_seed`,
`_GATE_SEED` below is hand-curated *data*: for each gate, its response
protocol, the function that parses that protocol, its fail-mode
(`"open"` — an unparseable/errored verdict is treated as pass, vs.
`"closed"` — treated as reject), whether it spends an LLM call the base
coder→executor→verdict cycle wouldn't otherwise spend, and the config
key that switches it on.

Unlike a fully free-form seed, though, every entry here is citation-checked
against the actual scanned repo (`build_gates_map`, when given `modules`
from a real Pass A scan plus `root`): `module` must be a path Pass A
actually scanned, and `parser`'s bare function name must resolve to a real
`def` *somewhere* in the repo (not necessarily in `module` itself — three
of these gates, `continuity`/`theme`/`fact`, import and reuse
`inner_loop._parse_verdict_soft` rather than defining their own parser, so
the search is repo-wide, not file-scoped). A hand-curated table that could
silently go stale (a renamed parser, a moved module) would be exactly the
kind of unchecked claim EPIC A/C exist to prevent — so, like COLLECT-10,
a citation that no longer resolves is a hard failure, not a warning.

Why these seven and not e.g. `prosody` (also a real creative-mode gate):
COLLECT-15's brief names `gate1 / verdict / continuity / theme / fact /
canon / language` explicitly — this module's job is that exact registry,
not "every gate-shaped thing in the repo". `prosody` is deterministic and
already fully self-contained/tested (`tools/auto/prosody.py`); it can be
added as an eighth seed entry later without changing this module's shape.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from tools.collect.model import ModuleRecord, Provenance

#: Fail-mode vocabulary `GateEntry.fail_mode` is restricted to.
FAIL_MODES = frozenset({"open", "closed"})


class GateCitationError(RuntimeError):
    """Raised when a seeded gate's `module` isn't a path Pass A actually
    scanned, or its `parser`'s bare function name doesn't resolve to a
    real `def` anywhere in the scanned repo — the GATES-map analogue of
    `registries.ContractCitationError` (COLLECT-10): hand-curated
    metadata about code must stay checked against that code, or it's an
    assertion wearing a fact's clothing."""


@dataclass(frozen=True)
class GateEntry:
    """One row of GATES: a gate's response protocol, its parser, and how
    it fails.

    `name` — short id (`"gate1"`, `"verdict"`, ...), matching COLLECT-15's
    brief and this module's `_GATE_SEED` keys.
    `module` — the file whose `check()`/driving logic implements this
    gate (not necessarily where `parser` is *defined* — see module
    docstring).
    `parser` — the function (bare name, or `ClassName.method` for a
    method) that turns the LLM's raw reply into a verdict.
    `protocol` — one-line description of the expected reply shape.
    `fail_mode` — `"open"` (unparseable/errored → treated as pass) or
    `"closed"` (→ treated as reject). One of `FAIL_MODES`.
    `extra_llm_call` — whether running this gate costs an LLM call the
    base coder→executor→Gate-2-verdict cycle would not otherwise spend.
    `config_switch` — the `[section] key` that turns this gate on/off (or
    tunes when it runs); `""` for a gate with no such switch (always on
    whenever its task_mode applies).
    `config_default` — that key's fallback value as it appears in code,
    as a string (so `"false"`/`"3"` etc., not a parsed bool/int) — this is
    documentation of the default, not a re-parsed live value.
    """

    name: str
    module: str
    parser: str
    protocol: str
    fail_mode: str
    extra_llm_call: bool
    config_switch: str
    config_default: str
    provenance: str = Provenance.STATIC

    def __post_init__(self) -> None:
        if self.fail_mode not in FAIL_MODES:
            raise ValueError(
                f"GateEntry {self.name!r}: fail_mode must be one of "
                f"{sorted(FAIL_MODES)}, got {self.fail_mode!r}"
            )

    def to_dict(self) -> Dict[str, object]:
        return {
            "name": self.name,
            "module": self.module,
            "parser": self.parser,
            "protocol": self.protocol,
            "fail_mode": self.fail_mode,
            "extra_llm_call": self.extra_llm_call,
            "config_switch": self.config_switch,
            "config_default": self.config_default,
            "provenance": self.provenance,
        }


# ── Hand-curated seed (data, not logic — cf. contracts_seed.yaml) ──────────
#
# Kept as a Python literal rather than a sibling YAML file: unlike
# CONTRACTS, GATES is a closed, small, code-structural registry (exactly
# the gates this pipeline has today) rather than an open-ended list of
# invariants an operator might want to extend by hand without touching
# code — so the extra indirection of a separate data file buys nothing
# COLLECT-10 needed it for.

_GATE_SEED: Dict[str, Dict[str, object]] = {
    "gate1": {
        "module": "tools/auto/gate1_filter.py",
        "parser": "Gate1Filter._parse_presence_response",
        "protocol": (
            'JSON {"verdict": "confirmed" | "rejected", "reason": "<one sentence>"}'
        ),
        "fail_mode": "closed",
        "extra_llm_call": True,
        "config_switch": "[gate1] skip_llm",
        "config_default": "false",
    },
    "verdict": {
        "module": "tools/auto/inner_loop.py",
        "parser": "_parse_verdict_soft",
        "protocol": (
            "line-oriented: first token APPROVED/OK (pass) or "
            "REVISE/REJECT/NO[: reason] (reject); Russian equivalents "
            "recognized too"
        ),
        "fail_mode": "open",
        # This *is* the base Gate-2 validator call the inner loop always
        # makes once per attempt — not an addition on top of anything, so
        # it doesn't count as "extra" the way the opt-in gates below do.
        "extra_llm_call": False,
        "config_switch": "",
        "config_default": "",
    },
    "continuity": {
        "module": "tools/auto/continuity_validator.py",
        "parser": "_parse_verdict_soft",
        "protocol": "line-oriented APPROVED / REVISE: <edit instruction> (reuses the verdict gate's parser)",
        "fail_mode": "open",
        "extra_llm_call": True,
        "config_switch": "[validator_agent] continuity_check_creative",
        "config_default": "false",
    },
    "theme": {
        "module": "tools/auto/theme_validator.py",
        "parser": "_parse_verdict_soft",
        "protocol": "line-oriented APPROVED / REVISE: <instruction> (reuses the verdict gate's parser)",
        "fail_mode": "open",
        "extra_llm_call": True,
        "config_switch": "[validator_agent] theme_check_creative",
        "config_default": "false",
    },
    "fact": {
        "module": "tools/auto/fact_validator.py",
        "parser": "_parse_verdict_soft",
        "protocol": "line-oriented APPROVED / REVISE: <contradicted fact> (reuses the verdict gate's parser)",
        "fail_mode": "open",
        "extra_llm_call": True,
        "config_switch": "[validator_agent] fact_check_creative",
        "config_default": "false",
    },
    "canon": {
        "module": "tools/auto/canon_validator.py",
        "parser": "CanonValidator._ground_claim",
        "protocol": "line verdict: DIRECT | INDIRECT | NONE | CONFLICT: <reason>",
        "fail_mode": "open",
        "extra_llm_call": True,
        # Not a boolean switch: a claim-check cadence in chapters.
        # `canon_check_every <= 0` disables the gate entirely.
        "config_switch": "[auto] canon_check_every",
        "config_default": "3",
    },
    "language": {
        "module": "tools/auto/coder.py",
        "parser": "_find_non_ascii_identifiers",
        "protocol": "deterministic: tokenize written identifiers, reject any containing a non-ASCII character",
        "fail_mode": "closed",
        # Pure AST/tokenize check on already-generated content — no LLM
        # call of its own.
        "extra_llm_call": False,
        "config_switch": "[coder] ascii_identifiers_only",
        "config_default": "false",
    },
}


def _bare_name(qualified: str) -> str:
    """The bare function/method name to search for: `"Foo.bar"` -> `"bar"`,
    `"bar"` -> `"bar"`. Gate parsers are cited either as a plain function
    name or `ClassName.method_name`; only the method name itself is a
    real `ast.FunctionDef.name` to search for — the class qualifier isn't
    something `ast.walk` needs (or can use) to find it."""
    return qualified.rsplit(".", 1)[-1]


def _repo_defines_function(
    modules: Iterable[ModuleRecord], root: Path, bare_name: str
) -> bool:
    """Whether some module in `modules` defines a function or method
    named `bare_name`, searched via a full `ast.walk` of each module's
    source (not just COLLECT-4's top-level `public_symbols` index, which
    intentionally excludes methods) — so a parser cited as
    `ClassName.method_name` resolves correctly, and so does a bare
    function name reused across modules via `import` (e.g.
    `_parse_verdict_soft`, defined once in `inner_loop.py` and imported
    by three other gates' modules).

    A module that fails to read or parse is skipped, not fatal — the
    citation only needs *one* module in the whole repo to confirm the
    symbol; one unreadable file among many shouldn't sink the check.
    """
    for m in modules:
        if m.parse_error:
            continue
        try:
            source = (Path(root) / m.path).read_text(encoding="utf-8")
            tree = ast.parse(source, filename=m.path)
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == bare_name:
                return True
    return False


def build_gates_map(
    modules: Optional[Iterable[ModuleRecord]] = None,
    root: Optional[Path] = None,
    *,
    seed: Dict[str, Dict[str, object]] = _GATE_SEED,
) -> List[GateEntry]:
    """GATES: one `GateEntry` per entry in `seed` (default `_GATE_SEED`),
    sorted by `name` for determinism (COLLECT-3).

    When both `modules` and `root` are given, every entry is
    citation-checked (COLLECT-15's whole reason for existing over just
    hardcoding a table nobody re-verifies): `module` must be a path
    present in `modules`, and `parser`'s bare name must resolve to a real
    `def` somewhere in the repo under `root`. A failure raises
    `GateCitationError` rather than silently shipping a stale claim.

    Without `modules`/`root` (either omitted), the map is still built —
    just unchecked — the same graceful-degradation posture COLLECT-13's
    `_loc` takes without a `root`: a caller assembling the artifact who
    hasn't done a real scan yet still gets the structural shape of GATES.
    """
    verify = modules is not None and root is not None
    module_paths = {m.path for m in modules} if modules is not None else set()
    modules_list = list(modules) if modules is not None else []

    entries: List[GateEntry] = []
    for name, spec in seed.items():
        module = str(spec["module"])
        parser = str(spec["parser"])

        if verify:
            if module not in module_paths:
                raise GateCitationError(
                    f"gate {name!r} cites module {module!r}, which Pass A "
                    "did not scan (renamed/removed/typo — this seed is "
                    "stale and must be fixed or dropped)"
                )
            bare = _bare_name(parser)
            if not _repo_defines_function(modules_list, root, bare):  # type: ignore[arg-type]
                raise GateCitationError(
                    f"gate {name!r} cites parser {parser!r}, which does not "
                    "resolve to a real function/method definition anywhere "
                    "in the scanned repo (renamed, removed, or a typo — "
                    "this seed is stale and must be fixed or dropped)"
                )

        entries.append(
            GateEntry(
                name=name,
                module=module,
                parser=parser,
                protocol=str(spec["protocol"]),
                fail_mode=str(spec["fail_mode"]),
                extra_llm_call=bool(spec["extra_llm_call"]),
                config_switch=str(spec["config_switch"]),
                config_default=str(spec["config_default"]),
            )
        )

    entries.sort(key=lambda e: e.name)
    return entries
