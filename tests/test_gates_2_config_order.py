"""GATES-2 — configurable Gate-3 order via the ``[gates]`` config section.

Covers `tools/auto/gate_registry.resolve_gate_order` and the two places
that consume it (`build_validators` construction-time filtering and
`run_gates` execution order), plus the wiring in `make_inner_loop`.

The load-bearing property under test is BACKWARD COMPATIBILITY: an
`agents.ini` with no `[gates]` section must behave exactly as it did
before this feature existed. Everything else here is the new surface.
"""

from __future__ import annotations

import configparser
from pathlib import Path

import pytest

from tools.auto.gate_registry import (
    GATES,
    GATES_BY_NAME,
    GateRejection,
    build_validators,
    resolve_gate_order,
    run_gates,
)


# ── helpers ──────────────────────────────────────────────────────────────────

def _cfg(text: str = "") -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read_string(text)
    return cfg


def _names(specs) -> list[str]:
    return [s.name for s in specs]


class _StubVerdict:
    """Minimal stand-in for a validator verdict object.

    Carries BOTH verdict shapes: the canon gate's predicate reads
    ``has_conflict`` while every other gate reads ``approved``. A stub
    with only one of them silently AttributeErrors inside run_gates'
    fail-open handler, which would make a broken gate look like a
    passing one.
    """

    def __init__(self, approved: bool, text: str = "problem"):
        self.approved = approved
        self.has_conflict = not approved
        self._text = text

    def feedback(self) -> str:
        return self._text


class _StubValidator:
    """Records the order in which gates were invoked."""

    def __init__(self, calls: list, name: str, approved: bool = True, cap: int = 1):
        self._calls = calls
        self._name = name
        self._approved = approved
        # every gate reads its cap off its own attribute name
        for spec in GATES:
            if spec.name == name:
                setattr(self, spec.cap_attr, cap)

    def check(self, *args, **kwargs):
        self._calls.append(self._name)
        return _StubVerdict(self._approved)

    def should_check(self, _f):  # canon only
        return True


class _StubLoop:
    """Enough of an InnerLoop for run_gates."""

    task_mode = "creative"

    def __init__(self, validators: dict, gate_order=None):
        for spec in GATES:
            setattr(self, spec.attr, validators.get(spec.name))
        if gate_order is not None:
            self.gate_order = gate_order

    def _task_with_goal(self, task):
        return task


def _run(loop, tmp_path, files=("ch1.md",)):
    for f in files:
        (tmp_path / f).write_text("text", encoding="utf-8")
    traces: list = []
    rejection = run_gates(
        loop,
        task={"id": "t1"},
        task_id="t1",
        attempt=1,
        target_files=list(files),
        base_dir_path=Path(tmp_path),
        revisions={},
        trace_stage=lambda *a, **k: traces.append((a, k)),
    )
    return rejection, traces


# ── resolve_gate_order: defaults / backward compatibility ────────────────────

def test_no_gates_section_falls_back_to_registry_order():
    """An untouched agents.ini must keep the pre-GATES-2 behaviour."""
    order = resolve_gate_order(_cfg(), "creative")
    assert _names(order) == [s.name for s in GATES if "creative" in s.modes]


def test_gates_section_without_this_mode_falls_back():
    """A [gates] section that only configures another mode is a no-op here."""
    cfg = _cfg("[gates]\ncode = \n")
    order = resolve_gate_order(cfg, "creative")
    assert _names(order) == [s.name for s in GATES if "creative" in s.modes]


def test_none_config_falls_back():
    """make_inner_loop can be reached with config=None in embedding contexts."""
    assert _names(resolve_gate_order(None, "creative")) == [
        s.name for s in GATES if "creative" in s.modes
    ]


def test_non_creative_mode_gets_no_gates_by_default():
    """Gate-3 is creative-only until a config says otherwise."""
    assert resolve_gate_order(_cfg(), "code") == ()


# ── resolve_gate_order: explicit lists ───────────────────────────────────────

def test_explicit_order_is_respected_verbatim():
    cfg = _cfg("[gates]\ncreative = prosody, canon\n")
    assert _names(resolve_gate_order(cfg, "creative")) == ["prosody", "canon"]


def test_explicit_order_can_reverse_the_registry():
    registry = [s.name for s in GATES if "creative" in s.modes]
    cfg = _cfg(f"[gates]\ncreative = {', '.join(reversed(registry))}\n")
    assert _names(resolve_gate_order(cfg, "creative")) == list(reversed(registry))


def test_empty_value_disables_every_gate():
    """`creative =` is the documented way to turn Gate-3 off entirely."""
    cfg = _cfg("[gates]\ncreative =\n")
    assert resolve_gate_order(cfg, "creative") == ()


def test_whitespace_and_blank_entries_are_tolerated():
    cfg = _cfg("[gates]\ncreative =   theme ,, ,  canon  \n")
    assert _names(resolve_gate_order(cfg, "creative")) == ["theme", "canon"]


def test_unknown_gate_is_skipped_not_raised(caplog):
    """A typo must degrade, never abort a run that would otherwise work."""
    cfg = _cfg("[gates]\ncreative = canon, tyop, theme\n")
    with caplog.at_level("WARNING"):
        order = resolve_gate_order(cfg, "creative")
    assert _names(order) == ["canon", "theme"]
    assert "tyop" in caplog.text


def test_all_unknown_names_yields_empty_not_default():
    """Garbage must not silently fall back to running every gate."""
    cfg = _cfg("[gates]\ncreative = nope, alsonope\n")
    assert resolve_gate_order(cfg, "creative") == ()


def test_duplicate_name_kept_once_at_first_position():
    """A copy-paste slip must not let one gate spend its cap twice."""
    cfg = _cfg("[gates]\ncreative = theme, canon, theme\n")
    assert _names(resolve_gate_order(cfg, "creative")) == ["theme", "canon"]


def test_every_registry_gate_is_addressable_by_name():
    """GATES_BY_NAME must stay in sync with GATES."""
    assert set(GATES_BY_NAME) == {s.name for s in GATES}
    for name, spec in GATES_BY_NAME.items():
        assert spec.name == name


@pytest.mark.parametrize("name", [s.name for s in GATES])
def test_each_gate_can_be_selected_alone(name):
    cfg = _cfg(f"[gates]\ncreative = {name}\n")
    assert _names(resolve_gate_order(cfg, "creative")) == [name]


# ── run_gates: execution honours the configured order ────────────────────────

def test_run_gates_follows_loop_gate_order(tmp_path):
    calls: list = []
    validators = {s.name: _StubValidator(calls, s.name) for s in GATES}
    order = (GATES_BY_NAME["prosody"], GATES_BY_NAME["canon"])
    loop = _StubLoop(validators, gate_order=order)

    rejection, _ = _run(loop, tmp_path)

    assert rejection is None
    assert calls == ["prosody", "canon"]


def test_run_gates_without_gate_order_uses_registry_order(tmp_path):
    """A directly-constructed InnerLoop (no make_inner_loop) still works."""
    calls: list = []
    validators = {s.name: _StubValidator(calls, s.name) for s in GATES}
    loop = _StubLoop(validators)  # no gate_order attribute at all

    _run(loop, tmp_path)

    assert calls == [s.name for s in GATES if "creative" in s.modes]


def test_run_gates_with_empty_order_runs_nothing(tmp_path):
    calls: list = []
    validators = {s.name: _StubValidator(calls, s.name, approved=False) for s in GATES}
    loop = _StubLoop(validators, gate_order=())

    rejection, traces = _run(loop, tmp_path)

    assert calls == []
    assert rejection is None
    assert traces == []


def test_gate_absent_from_order_is_not_run_even_if_validator_present(tmp_path):
    """Ordering is the switch — a live validator off the list stays silent."""
    calls: list = []
    validators = {s.name: _StubValidator(calls, s.name, approved=False) for s in GATES}
    loop = _StubLoop(validators, gate_order=(GATES_BY_NAME["theme"],))

    rejection, _ = _run(loop, tmp_path)

    assert calls == ["theme"]
    assert isinstance(rejection, GateRejection)
    assert rejection.gate == "theme"


def test_first_gate_in_order_wins_the_rejection(tmp_path):
    """Reordering changes which gate reports the problem, not whether one does."""
    calls: list = []
    validators = {s.name: _StubValidator(calls, s.name, approved=False) for s in GATES}

    loop_a = _StubLoop(
        validators, gate_order=(GATES_BY_NAME["theme"], GATES_BY_NAME["prosody"])
    )
    rej_a, _ = _run(loop_a, tmp_path)

    calls.clear()
    loop_b = _StubLoop(
        validators, gate_order=(GATES_BY_NAME["prosody"], GATES_BY_NAME["theme"])
    )
    rej_b, _ = _run(loop_b, tmp_path)

    assert rej_a.gate == "theme"
    assert rej_b.gate == "prosody"
    # the later gate never ran — run_gates short-circuits on first rejection
    assert calls == ["prosody"]


# ── build_validators: disabled gates cost nothing to construct ───────────────

def test_build_validators_skips_gates_absent_from_config_order(monkeypatch, tmp_path):
    built: list = []

    def _spy(name):
        def _factory(config, **kwargs):
            built.append(name)
            return object()
        return _factory

    for spec in GATES:
        module = __import__(spec.factory_module, fromlist=[spec.factory_name])
        monkeypatch.setattr(module, spec.factory_name, _spy(spec.name))

    cfg = _cfg("[gates]\ncreative = theme\n")
    out = build_validators(cfg, tmp_path, task_mode="creative")

    assert built == ["theme"]
    assert out[GATES_BY_NAME["theme"].attr] is not None
    for spec in GATES:
        if spec.name != "theme":
            assert out[spec.attr] is None


def test_build_validators_returns_every_attr_key(tmp_path):
    """The result is splatted into InnerLoop(**...), so no key may be missing."""
    out = build_validators(_cfg("[gates]\ncreative =\n"), tmp_path, task_mode="creative")
    assert set(out) == {s.attr for s in GATES}
    assert all(v is None for v in out.values())


def test_build_validators_failing_factory_yields_none(monkeypatch, tmp_path):
    """Setup must never block the loop — a raising factory disables its gate."""
    spec = GATES_BY_NAME["theme"]
    module = __import__(spec.factory_module, fromlist=[spec.factory_name])

    def _boom(config, **kwargs):
        raise RuntimeError("no LLM configured")

    monkeypatch.setattr(module, spec.factory_name, _boom)
    out = build_validators(_cfg("[gates]\ncreative = theme\n"), tmp_path, task_mode="creative")
    assert out[spec.attr] is None


# ── make_inner_loop wiring ───────────────────────────────────────────────────

def test_make_inner_loop_attaches_resolved_gate_order(tmp_path):
    from tools.auto.inner_loop import make_inner_loop

    cfg = _cfg(
        "[api]\nactive = local\n"
        "[api_local]\nbase_url = http://localhost:1\nmodel = m\n"
        "[gates]\ncreative = theme, canon\n"
    )
    loop = make_inner_loop(cfg, tmp_path, task_mode="creative")
    assert _names(loop.gate_order) == ["theme", "canon"]


def test_make_inner_loop_gate_order_defaults_without_section(tmp_path):
    from tools.auto.inner_loop import make_inner_loop

    cfg = _cfg(
        "[api]\nactive = local\n"
        "[api_local]\nbase_url = http://localhost:1\nmodel = m\n"
    )
    loop = make_inner_loop(cfg, tmp_path, task_mode="creative")
    assert _names(loop.gate_order) == [s.name for s in GATES if "creative" in s.modes]
