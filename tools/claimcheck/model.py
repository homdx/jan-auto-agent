"""CC-1 — the claim-check data model (docs/claim-check/EPIC-CC.md §4.3).

Everything the later tickets pass between them lives here: an `Anchor` is a
thing in a claim's text that names the repository, a `ResolvedAnchor` is that
anchor looked up in the repository at a pinned commit, a `Chunk` is one piece of
evidence and a `Pack` is the chunks one claim's voters read. `RepoView` is the
read-only window on a repository that anchors are resolved against and CC-3 /
CC-4 read evidence from; `PathRepoView` (anchors.py) is the one over a
directory, CC-2's `Target.view()` is the one over a pinned worktree.

`Chunk` and `Pack` are types here. CC-5 (`pack.py`) owns the two methods of `Pack`:
`render` (the text the voter reads) and `find` (what counts as a verbatim quote
of a chunk); they live in `pack.py` and are called from here, so the rule has one home.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class Anchor:
    """Something in the claim's text that names the repository.

    `text` is the token as written, markup stripped; `claim[start:end] == text`
    holds for every anchor `extract_anchors` returns."""

    kind: str   # "path" | "symbol" | "test" | "commit" | "ticket" | "ref"
    text: str   # "Policy._mechanical", "tools/contest/gates.py", "a73e389"
    start: int
    end: int


@dataclass(frozen=True)
class ResolvedAnchor:
    """An anchor looked up at the pinned commit.

    `found` is the whole answer to "does it exist". An anchor that is *not*
    found but points into the repository anyway (a function of a module that is
    there, a file under a directory that is there) carries that place in `path`:
    `found=False` with `path != ""` is what `classify` calls a dangling anchor.
    `lines` is 1-based and inclusive: the definition's span for a symbol or a
    test, the cited span for a path written `file.py:12-30`."""

    anchor: Anchor
    found: bool
    path: str = ""
    qualname: str = ""
    lines: tuple = (0, 0)
    sha: str = ""
    candidates: tuple = ()   # other definitions of an ambiguous name, at most 3


@dataclass(frozen=True)
class Chunk:
    """One piece of evidence, with an id a voter can cite and a quote can be found by."""

    id: str     # "src:tools/contest/gates.py:120-143", "git:a73e389:gates.py:88-97"
    kind: str   # "source" | "collect" | "git" | "ticket" | "note"
    path: str
    start: int  # 1-based inclusive; 0, 0 for a chunk that is not a file span
    end: int
    text: str
    why: str    # which anchor asked for it


@dataclass(frozen=True)
class Pack:
    """The evidence for one claim at one commit.

    `chunks` are in rank order. `truncated` is True whenever the budget dropped or cut
    anything; `omitted` and `cut` say how many chunks (CC-5 sets both; a `Pack` built
    by hand may leave them 0)."""

    claim: str
    sha: str
    chunks: tuple
    truncated: bool
    omitted: int = 0   # chunks the budget dropped
    cut: int = 0       # chunks it cut (at a line, or a line over the cap)

    def render(self) -> str:
        """The text the voter reads (`tools.claimcheck.pack.render_pack`)."""
        from tools.claimcheck.pack import render_pack   # pack.py imports this module
        return render_pack(self)

    def find(self, quote: str) -> Optional[str]:
        """The id of the one chunk that contains *quote* verbatim, else None
        (`tools.claimcheck.pack.find_quote`: the only place the rule lives)."""
        from tools.claimcheck.pack import find_quote
        return find_quote(self, quote)


@runtime_checkable
class RepoView(Protocol):
    """A read-only window on a repository at one pinned commit."""

    def exists(self, path: str) -> bool: ...

    def read(self, path: str) -> str: ...

    def files(self) -> list: ...

    def rev_parse(self, rev: str) -> Optional[str]: ...

    def ticket_file(self, ticket_id: str) -> Optional[str]: ...

    def git(self, *args: str) -> str: ...
