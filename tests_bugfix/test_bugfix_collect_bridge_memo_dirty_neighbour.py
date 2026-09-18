"""tests_bugfix/test_bugfix_collect_bridge_memo_dirty_neighbour.py — V6 memo.

Bug (reproduced against the live code before the fix):

    block = bridge.context_for("pkg/a.py")      # cached in the V6 memo
    ... a task edits pkg/caller_a.py, commits, bridge.invalidate([it]) ...
    block = bridge.context_for("pkg/a.py")      # memo hit, byte-identical

The second call returned a block whose `neighbours:` row still quoted
`pkg/caller_a.py`'s pre-edit LLM purpose, under a header that reads
"COLLECT MODEL (static facts, do not contradict):".

`invalidate()` only dropped memo entries *keyed* by the dirtied path, so a
block about some other file — one that merely *cited* the dirtied file's
purpose — survived the commit and was served on a memo hit, paying no LLM
call and never re-checking dirt. The `_row_neighbours(is_dirty=...)` guard
that fixes this for a freshly built block was unreachable from the cache:
the rebuild that would have honoured it never happened.

The fix
-------

`build_collect_context_block_stats` now reports ``neighbours_paths`` — the
paths the rendered `neighbours:` row actually named — in its stats, which
already travel with the memo entry. `invalidate()` drops an entry when its
own key is dirtied *or* when its quoted set intersects the commit's dirt.

Deliberately narrow: a path that appears only in the static `callers:` /
`calls_into:` rows does not invalidate a block. Those are graph facts the
artifact froze at build time and the row keeps by design — only LLM prose
about another module can go stale under it. And a path the cut or the
row's own skip rules kept out of the block was never shown to a coder, so
editing it cannot make a block stale.

Only structural dataclasses are built here — no git repo, no artifact on
disk, no LLM.
"""

from __future__ import annotations

import pytest

from tools.auto.collect_bridge import CollectBridge, _drop_stale_memo, _memo_neighbours
from tools.auto.context_assembler import (
    _NEIGHBOURS_PREFIX,
    _neighbour_paths_in,
    build_collect_context_block_stats,
)
from tools.collect.loader import STATUS_ABSENT, STATUS_FRESH, CollectModel
from tools.collect.model import FunctionRecord, LLMSummary, ModuleRecord

TARGET = "pkg/a.py"
NEIGHBOUR = "pkg/hub.py"
OTHER_TARGET = "pkg/b.py"
OTHER_NEIGHBOUR = "pkg/other.py"
DEP = "pkg/dep_a.py"
PURPOSE = "Orchestrates the whole run."


# ── helpers ────────────────────────────────────────────────────────────────


def _symbol(qualname: str) -> FunctionRecord:
    return FunctionRecord(qualname=qualname, module=qualname.rsplit(".", 1)[0],
                          lineno=1, signature="")


def _model() -> CollectModel:
    """Two targets, each with one caller and one callee. Only `NEIGHBOUR`
    carries an LLM purpose, so only `TARGET`'s block has a `neighbours:` row.
    `OTHER_NEIGHBOUR` has none, so `OTHER_TARGET`'s block quotes nobody."""
    modules = (
        ModuleRecord(path=TARGET, public_symbols=(_symbol("pkg.a.foo"),)),
        # `NEIGHBOUR` carries symbols too, so its own block renders and memoizes
        # — a block that returned "" would never be cached, and this file tests
        # the cache.
        ModuleRecord(
            path=NEIGHBOUR,
            public_symbols=(_symbol("pkg.hub.run"),),
            summary=LLMSummary(purpose=PURPOSE),
        ),
        ModuleRecord(path=DEP),
        ModuleRecord(path=OTHER_TARGET, public_symbols=(_symbol("pkg.b.bar"),)),
        ModuleRecord(path=OTHER_NEIGHBOUR),
        ModuleRecord(path="pkg/dep_b.py"),
    )
    return CollectModel(
        status=STATUS_FRESH,
        modules=modules,
        import_edges={TARGET: (DEP,), OTHER_TARGET: ("pkg/dep_b.py",)},
        imported_by={TARGET: (NEIGHBOUR,), OTHER_TARGET: (OTHER_NEIGHBOUR,)},
        test_map={TARGET: (), OTHER_TARGET: ()},
    )


def _bridge(**kwargs) -> CollectBridge:
    return CollectBridge(_model(), summarizer_call=None, **kwargs)


def _neighbour_lines(block: str) -> list:
    return [line for line in block.split("\n") if line.startswith(_NEIGHBOURS_PREFIX)]


def _quoted(entry) -> tuple:
    return _memo_neighbours(entry)


# ── the defect, at the bridge level ────────────────────────────────────────


def test_invalidate_drops_a_cached_block_that_quotes_the_dirtied_neighbour():
    """The bug. A block is cached, a task commits an edit to a *neighbour*, and
    the next request for the same target must not be served from that cache."""
    bridge = _bridge(max_context_chars=5000)
    first = bridge.context_for(TARGET)
    assert PURPOSE in first

    bridge.invalidate([NEIGHBOUR])
    assert not bridge._memo, "the cached block cited the dirtied path"

    again = bridge.context_for(TARGET)
    assert PURPOSE not in again
    assert not _neighbour_lines(again)


def test_the_dropped_block_is_rebuilt_not_patched():
    """Dropping the entry is what forces the rebuild, and the rebuild is what
    re-checks dirt per path. The rebuilt block still carries every static fact
    about the dirtied path — only its LLM prose is withheld."""
    bridge = _bridge(max_context_chars=5000)
    bridge.context_for(TARGET)
    bridge.invalidate([NEIGHBOUR])

    again = bridge.context_for(TARGET)
    assert NEIGHBOUR in again, "the callers row still names it"
    assert f"calls_into: {DEP}" in again
    assert PURPOSE not in again
    # and it was rebuilt, not served from the cache
    assert bridge.collect_stats["memo_hits"] == 0
    assert bridge._memo


def test_invalidate_keeps_a_block_that_only_names_the_path_in_static_rows():
    """`callers:` and `calls_into:` are graph facts frozen at build time. A
    block that names a dirtied path only there is not stale, so its entry must
    survive — otherwise every commit would flush half the cache for nothing."""
    bridge = _bridge(max_context_chars=5000)
    clean = bridge.context_for(OTHER_TARGET)
    assert OTHER_NEIGHBOUR in clean and not _neighbour_lines(clean)

    bridge.invalidate([OTHER_NEIGHBOUR])
    assert (OTHER_TARGET, 5000, True) in bridge._memo, "nothing quoted it"

    assert bridge.context_for(OTHER_TARGET) == clean
    assert bridge.collect_stats["memo_hits"] == 1


def test_invalidate_drops_only_the_entries_that_quote_the_path():
    """One dirty path, several cached blocks: exactly the one that quotes it
    goes. A commit to one file must not flush the cache for the other targets."""
    bridge = _bridge(max_context_chars=5000)
    bridge.context_for(TARGET)
    clean = bridge.context_for(OTHER_TARGET)
    assert len(bridge._memo) == 2

    bridge.invalidate([NEIGHBOUR])
    assert list(bridge._memo.keys()) == [(OTHER_TARGET, 5000, True)]
    assert bridge.context_for(OTHER_TARGET) == clean


def test_invalidate_drops_the_keyed_entry_and_the_quoting_entry_at_once():
    """Both drop reasons fire on one call: the block *about* the dirtied path
    and the block that *quotes* it."""
    bridge = _bridge(max_context_chars=5000)
    bridge.context_for(TARGET)
    bridge.context_for(NEIGHBOUR)
    assert len(bridge._memo) == 2

    bridge.invalidate([NEIGHBOUR])
    assert bridge._memo == {}


# ── `_drop_stale_memo` ─────────────────────────────────────────────────────


def test_drop_stale_memo_drops_by_key_and_by_quote():
    memo = {
        ("pkg/a.py", 5000, True): ("block", {"neighbours_paths": (NEIGHBOUR,)}),
        ("pkg/b.py", 5000, True): ("block", {"neighbours_paths": (OTHER_NEIGHBOUR,)}),
        (NEIGHBOUR, 5000, True): ("block", {"neighbours_paths": ()}),
    }
    out = _drop_stale_memo(memo, {NEIGHBOUR})
    assert list(out.keys()) == [("pkg/b.py", 5000, True)]


def test_drop_stale_memo_with_no_dirt_returns_the_memo_unchanged():
    memo = {"k": ("block", {"neighbours_paths": (NEIGHBOUR,)})}
    assert _drop_stale_memo(memo, set()) == memo


def test_drop_stale_memo_never_raises_on_a_malformed_memo():
    """A hand-seeded or partially-corrupted memo degrades to a dropped entry,
    never to an exception out of `invalidate` — which is mid-run, right after
    a commit."""
    for memo in (
        {"bad-key": object(), None: None, (): ()},
        {"k": [1, 2]},
        {"k": ("block", "not-a-dict")},
        {"k": ("block", {"neighbours_paths": "not-a-list"})},
        {},
    ):
        assert isinstance(_drop_stale_memo(memo, {NEIGHBOUR}), dict)


# ── `_memo_neighbours`: fail-open on every entry shape ─────────────────────


@pytest.mark.parametrize(
    "entry",
    [
        "a bare string, pre-M4 shape",
        ("block", {}),
        ("block", None),
        ("block", "not a dict"),
        ("block", {"neighbours_paths": "not-a-list"}),
        ("block", {"neighbours_paths": 42}),
        ("block", {"neighbours_paths": None}),
    ],
)
def test_memo_neighbours_reads_nothing_from_a_malformed_entry(entry):
    """A block built before this fix, or a hand-seeded entry in a probe test,
    reads as "quotes nothing" — never as the dirt, and never raises. The one
    miss that makes is a wasted rebuild, which is the safe direction."""
    assert _memo_neighbours(entry) == ()


def test_memo_neighbours_reads_the_stats_even_from_a_malformed_block():
    """`stats` is the source of truth, not the block string. A block that failed
    to render is still dropped when its stats name a dirtied path."""
    assert _memo_neighbours((None, {"neighbours_paths": (NEIGHBOUR,)})) == (NEIGHBOUR,)


def test_memo_neighbours_reads_the_paths_in_render_order():
    assert _memo_neighbours(
        ("block", {"neighbours_paths": [NEIGHBOUR, OTHER_NEIGHBOUR]})) == (
        NEIGHBOUR, OTHER_NEIGHBOUR
    )


# ── `stats["neighbours_paths"]`: what the row actually named ───────────────


def test_stats_reports_the_paths_the_row_rendered():
    _, stats = build_collect_context_block_stats(_model(), TARGET, budget=None)
    assert stats["neighbours_paths"] == (NEIGHBOUR,)


def test_stats_reports_only_paths_that_reached_the_block():
    """A path the row skipped for having no purpose was never rendered, so it
    cannot go stale and must not appear here. Otherwise every purpose-less
    neighbour would flush the cache on any commit that touched it."""
    _, stats = build_collect_context_block_stats(_model(), OTHER_TARGET, budget=None)
    assert stats["neighbours_paths"] == ()


def _six_callers_model() -> CollectModel:
    """One target, six callers, every caller with a purpose. `_row_neighbours`
    caps at three entries and the budget cut drops further, so a tight budget
    renders a strict subset — the shape this key is supposed to describe."""
    callers = tuple(f"pkg/caller{i}.py" for i in range(6))
    modules = (ModuleRecord(path=TARGET, public_symbols=()),) + tuple(
        ModuleRecord(path=path, summary=LLMSummary(purpose=f"Caller number {i} runs it."))
        for i, path in enumerate(callers)
    )
    return CollectModel(
        status=STATUS_FRESH, modules=modules,
        import_edges={TARGET: ()}, imported_by={TARGET: callers},
        test_map={TARGET: ()},
    )


def test_stats_reports_exactly_the_paths_the_cut_kept():
    """The read happens after the cut, so a path the cut dropped must not be
    reported — invalidating it would otherwise flush a cache entry that never
    showed it. `neighbours_paths` has to equal the rendered lines, not the
    row's candidate list."""
    model = _six_callers_model()
    for budget in (5000, 350, 250, 200):
        block, stats = build_collect_context_block_stats(model, TARGET, budget=budget)
        rendered = tuple(
            line[len(_NEIGHBOURS_PREFIX):].split(" — ", 1)[0]
            for line in _neighbour_lines(block)
        )
        assert stats["neighbours_paths"] == rendered, budget


def test_a_cut_away_neighbour_does_not_invalidate_the_entry():
    """End to end: `pkg/caller2.py` is a real, summarised neighbour that the
    budget cut keeps out of the block's `neighbours:` row. Committing an edit
    to it must not drop the cached entry for `TARGET` — that row never quoted
    it. (The path still appears in the static `callers:` row, which is a
    frozen graph fact and stays put by design.)"""
    model = _six_callers_model()
    bridge = CollectBridge(model, max_context_chars=350, summarizer_call=None)
    first = bridge.context_for(TARGET)
    neighbours = _neighbour_lines(first)
    assert neighbours, "the row must render at least one entry"
    assert all("pkg/caller2.py" not in line for line in neighbours)

    bridge.invalidate(["pkg/caller2.py"])
    assert (TARGET, 350, True) in bridge._memo, "the block never quoted it"
    assert bridge.context_for(TARGET) == first
    assert bridge.collect_stats["memo_hits"] == 1


def test_stats_reports_nothing_when_is_dirty_kept_a_neighbour_out():
    """The skip-not-pad rule: `is_dirty` withheld a neighbour, so the block
    quoted no one and the entry has nothing that could go stale."""
    _, stats = build_collect_context_block_stats(
        _model(), TARGET, budget=None, is_dirty=lambda path: path == NEIGHBOUR,
    )
    assert stats["neighbours_paths"] == ()


@pytest.mark.parametrize(
    "kwargs",
    [{"budget": None}, {"budget": 200}, {"pack_enabled": False}],
)
def test_stats_reports_empty_when_the_row_is_absent(kwargs):
    model = CollectModel(status=STATUS_FRESH, modules=())
    _, stats = build_collect_context_block_stats(model, TARGET, **kwargs)
    assert stats["neighbours_paths"] == ()


@pytest.mark.parametrize("model", [None, CollectModel(status=STATUS_ABSENT)])
def test_stats_reports_empty_on_an_unusable_model(model):
    _, stats = build_collect_context_block_stats(model, TARGET)
    assert stats["neighbours_paths"] == ()


def test_stats_key_is_always_present():
    """`invalidate` relies on `.get(..., ())`, but the key should not have to
    be there. Every return path of the assembler carries it."""
    model = _model()
    for target, budget, pack in (
        (TARGET, None, True), (TARGET, 200, True), (TARGET, None, False),
        (NEIGHBOUR, None, True), ("pkg/unknown.py", None, True),
    ):
        _, stats = build_collect_context_block_stats(
            model, target, budget=budget, pack_enabled=pack,
        )
        assert "neighbours_paths" in stats


# ── `_neighbour_paths_in` ──────────────────────────────────────────────────


def test_neighbour_paths_in_reads_the_rendered_lines():
    block = (
        "COLLECT MODEL (static facts, do not contradict):\n"
        f"neighbours: {NEIGHBOUR} — {PURPOSE} (llm)\n"
        f"neighbours: {OTHER_NEIGHBOUR} — Another purpose. (llm)\n"
        f"callers: 1 module imports this: {NEIGHBOUR}"
    )
    assert _neighbour_paths_in(block.split("\n")) == (NEIGHBOUR, OTHER_NEIGHBOUR)


@pytest.mark.parametrize(
    "line",
    [
        "callers: 1 module imports this: pkg/hub.py",
        "neighbours: pkg/hub.py",          # no purpose separator
        "",
        "module: pkg/a.py",
    ],
)
def test_neighbour_paths_in_ignores_non_neighbour_lines(line):
    assert _neighbour_paths_in([line]) == ()


def test_neighbour_paths_in_handles_a_line_without_a_space_before_the_dash():
    """Fail open on a malformed line rather than raising into a build."""
    assert _neighbour_paths_in([_NEIGHBOURS_PREFIX + "pkg/x.py—no space"]) == ()
