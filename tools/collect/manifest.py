"""tools/collect/manifest.py — COLLECT-2: manifest, hashing, git-SHA, freshness.

Produces ``collect_manifest.json``: the receipt Pass A leaves behind so any
later reader (a human, the loader in EPIC G, or a re-run of `collect` itself)
can tell whether the on-disk structural model still matches the working
tree, without re-running the whole AST walk to find out.

A manifest carries:

  * ``collector_version`` — so a format change can be detected.
  * ``generated_at``       — ISO-8601 timestamp of the run (metadata only;
                              never used to decide freshness, since a clock
                              says nothing about content).
  * ``git_sha`` / ``dirty``  — build provenance: which commit, and whether
                              the tree had uncommitted changes at the time.
  * ``file_hashes``        — a ``{relative_path: sha256_hex}`` map over
                              every file the collector considered. This is
                              the *only* thing ``is_fresh`` looks at:
                              freshness is a content question, not a git
                              question, so it stays correct even in a
                              working tree with no git history at all.

``is_fresh(manifest, root)`` recomputes the same hash map for the current
tree and compares it to the stored one. Any changed, added, or removed file
makes the two maps unequal, so staleness is always detected; an unchanged
tree — even rebuilt at a different timestamp — compares equal.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

COLLECTOR_VERSION = "1"

#: Default directories skipped when discovering files for a manifest —
#: mirrors the spirit of `[search] skip_dirs` used elsewhere in this repo.
DEFAULT_SKIP_DIRS = frozenset(
    {".git", "__pycache__", ".pytest_cache", "node_modules", ".venv", "venv", ".collect"}
)


def _utc_iso_now() -> str:
    """Current UTC time as an ISO-8601 string with a trailing 'Z'."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hash_file(path: Path) -> str:
    """sha256 hex digest of a single file's raw bytes."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def discover_files(
    root: Path,
    *,
    suffixes: Iterable[str] = (".py",),
    skip_dirs: Iterable[str] = DEFAULT_SKIP_DIRS,
) -> List[str]:
    """Walk `root`, returning sorted POSIX-relative paths of matching files.

    Self-contained on purpose: COLLECT-2 must not depend on the AST scanner
    (COLLECT-4, EPIC B) which doesn't exist yet. The scanner is free to hand
    `build_manifest` its own file list instead of relying on this walk.
    """
    root = Path(root)
    skip = set(skip_dirs)
    suffix_set = tuple(suffixes)
    out: List[str] = []
    for dirpath, dirnames, filenames in _walk(root, skip):
        for name in filenames:
            if suffix_set and not name.endswith(suffix_set):
                continue
            rel = (dirpath / name).relative_to(root).as_posix()
            out.append(rel)
    return sorted(out)


def _walk(root: Path, skip_dirs: set):
    import os

    for dirpath_str, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs and not d.startswith(".")]
        yield Path(dirpath_str), dirnames, filenames


def hash_tree(root: Path, files: Iterable[str]) -> Dict[str, str]:
    """`{relative_path: sha256_hex}` for every path in `files`, resolved
    relative to `root`. Missing files are simply absent from the result —
    callers (i.e. `is_fresh`) treat a missing key as a mismatch."""
    root = Path(root)
    result: Dict[str, str] = {}
    for rel in files:
        p = root / rel
        if p.is_file():
            result[rel] = hash_file(p)
    return result


def _run_git(root: Path, *args: str) -> Optional[str]:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def get_git_sha(root: Path) -> Optional[str]:
    """Current commit SHA, or None if `root` isn't a git repo / has no commits."""
    return _run_git(root, "rev-parse", "HEAD") or None


def is_dirty(root: Path, exclude_dir: Optional[Path] = None) -> bool:
    """True if the working tree has uncommitted changes. False (not
    unknown) if git is unavailable or `root` isn't a repo — a manifest built
    outside git has nothing to report here, and that's not the same claim
    as "the tree is dirty".

    `exclude_dir`, if given, is the collector's own output directory
    (`resolve_collect_dir(...)`): any porcelain entry whose path lies under
    it is ignored. The call-ordering contract below protects only against
    output written by the *current* run; a *previous* run's `.collect/`
    output is already sitting untracked when this run starts, which made
    `dirty` read True on every build after the first — i.e. on effectively
    every real `refresh`. Filtering by path is the only fix that holds
    across runs, and it must use the *configured* dir (the location is
    `[collect] dir`-configurable), which is why the exclusion is a
    parameter rather than a hardcoded `.collect`.

    CALLER CONTRACT: must be called *before* the collector writes any
    output (artifact.json, the manifest itself, rendered pages) to disk.
    `.collect/` is not a git-ignored directory, so a `collect` run's own
    freshly-written, untracked output would otherwise show up in
    `git status --porcelain` and make every full build report `dirty=True`
    regardless of whether the tracked source tree is actually clean. See
    `capture_provenance`, which callers should use instead of calling
    `get_git_sha`/`is_dirty` directly, to make this ordering hard to get
    wrong."""
    status = _run_git(root, "status", "--porcelain")
    if not status:
        return False
    if exclude_dir is None:
        return True
    try:
        rel = Path(exclude_dir).resolve().relative_to(Path(root).resolve())
    except ValueError:
        # Collect dir configured outside the repo — nothing it writes can
        # show up in this repo's porcelain output, so no filtering needed.
        return True
    prefix = rel.as_posix().rstrip("/") + "/"
    for line in status.splitlines():
        # Porcelain v1: 2-char status, space, then the path. Renames are
        # `old -> new`; either side under the collect dir is the
        # collector's own doing.
        payload = line[3:] if len(line) > 3 else ""
        paths = payload.split(" -> ") if " -> " in payload else [payload]
        for p in paths:
            p = p.strip().strip('"')
            if not (p.startswith(prefix) or p == prefix.rstrip("/")):
                return True
    return False


def capture_provenance(
    root: Path, collect_dir: Optional[Path] = None
) -> Tuple[Optional[str], bool]:
    """Snapshot `(git_sha, dirty)` right now, at a single call site meant
    to run *before* writing any collector output under `root`.

    This exists so every caller captures provenance at one well-defined
    point (the top of a build, before `_write_artifact`/`_write_manifest`
    touch disk) instead of each call site re-deriving `git_sha`/`is_dirty`
    inline after output has already been written — which is what silently
    made `dirty` report `True` on every fresh full build, since `.collect/`
    isn't git-ignored and its own new files count as untracked changes.

    `collect_dir` should be the resolved output directory
    (`resolve_collect_dir(root, config)`): output from *previous* runs is
    already untracked before this run writes anything, so the
    capture-before-write ordering alone only kept the very first build
    honest — every subsequent build read `dirty=True` from the prior run's
    leftovers. Passing the dir lets `is_dirty` exclude it by path, which
    holds across runs.
    """
    return get_git_sha(root), is_dirty(root, exclude_dir=collect_dir)


@dataclass(frozen=True)
class Manifest:
    """The full `collect_manifest.json` payload."""

    collector_version: str
    generated_at: str
    git_sha: Optional[str]
    dirty: bool
    file_hashes: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "collector_version": self.collector_version,
            "generated_at": self.generated_at,
            "git_sha": self.git_sha,
            "dirty": self.dirty,
            "file_hashes": dict(self.file_hashes),
        }

    def to_json(self, *, indent: int = 2) -> str:
        # sort_keys for the same reason Pass A JSON is canonicalized
        # (COLLECT-3): a stable, diffable manifest.
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    @classmethod
    def from_dict(cls, d: dict) -> "Manifest":
        return cls(
            collector_version=d["collector_version"],
            generated_at=d["generated_at"],
            git_sha=d.get("git_sha"),
            dirty=bool(d.get("dirty", False)),
            file_hashes=dict(d.get("file_hashes", {})),
        )

    @classmethod
    def from_json(cls, raw: str) -> "Manifest":
        return cls.from_dict(json.loads(raw))


def build_manifest(
    root: Path,
    files: Optional[Iterable[str]] = None,
    *,
    collector_version: str = COLLECTOR_VERSION,
    provenance: Optional[Tuple[Optional[str], bool]] = None,
) -> Manifest:
    """Build a `Manifest` for `root`. If `files` isn't given, discovers
    `*.py` files under `root` via `discover_files`.

    `provenance`, if given, is a pre-captured `(git_sha, dirty)` pair from
    `capture_provenance(root)` — callers that write collector output
    (`.collect/...`) before calling this must capture provenance first and
    pass it in here, since `.collect/` isn't git-ignored and its own
    freshly-written files would otherwise make `is_dirty` see a dirty tree
    that doesn't reflect the actual tracked source. When omitted,
    `git_sha`/`dirty` are computed now, which is only correct for callers
    that haven't written anything yet.
    """
    root = Path(root)
    file_list = list(files) if files is not None else discover_files(root)
    if provenance is not None:
        git_sha, dirty = provenance
    else:
        git_sha, dirty = get_git_sha(root), is_dirty(root)
    return Manifest(
        collector_version=collector_version,
        generated_at=_utc_iso_now(),
        git_sha=git_sha,
        dirty=dirty,
        file_hashes=hash_tree(root, file_list),
    )


def write_manifest(manifest: Manifest, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(manifest.to_json() + "\n", encoding="utf-8")


def read_manifest(path: Path) -> Manifest:
    return Manifest.from_json(Path(path).read_text(encoding="utf-8"))


def is_fresh(
    manifest: Manifest,
    root: Path,
    *,
    files: Optional[Iterable[str]] = None,
) -> bool:
    """True iff `root`'s current content hashes exactly match `manifest`'s.

    By default this re-*discovers* the file set under `root` (rather than
    only re-hashing the paths the manifest already knows about), so a file
    added since the manifest was built shows up as an extra key and makes
    the comparison fail — not just edits to already-tracked files. Removing
    or editing a tracked file is caught the same way: any difference in the
    resulting `{path: hash}` dict, whether it's a missing key, an extra
    key, or a changed value, means "not fresh". A tree rebuilt with no
    changes at all — even at a different timestamp — compares equal and is
    fresh.

    Pass `files` explicitly to freshness-check against a specific file set
    instead of a fresh directory walk (e.g. when the caller already has the
    scanner's own file list from EPIC B).
    """
    root = Path(root)
    current_files = list(files) if files is not None else discover_files(root)
    current = hash_tree(root, current_files)
    return current == manifest.file_hashes


@dataclass(frozen=True)
class FileChanges:
    """The result of diffing two `{path: sha256}` maps (COLLECT-24).

    Used by `--refresh`'s incremental path to decide, for each file the
    collector knows about, whether it needs re-scanning: `added`/`modified`
    do, `removed` drops the file's record entirely, and anything in
    neither set — the overwhelming majority on a typical re-run — is
    reused verbatim, LLM summary included.
    """

    added: frozenset
    modified: frozenset
    removed: frozenset

    @property
    def changed(self) -> frozenset:
        """`added | modified` — the set of paths that need re-scanning."""
        return self.added | self.modified

    def is_empty(self) -> bool:
        return not (self.added or self.modified or self.removed)


def diff_files(previous: Dict[str, str], current: Dict[str, str]) -> FileChanges:
    """Diff a previous `{path: sha256}` map (typically `manifest.file_hashes`)
    against a freshly-hashed current one.

    A path present in both with an unchanged hash is neither added,
    modified, nor removed — it's simply absent from every set on the
    returned `FileChanges`, which is exactly "nothing to do for this file".
    """
    prev_paths = set(previous)
    cur_paths = set(current)
    added = frozenset(cur_paths - prev_paths)
    removed = frozenset(prev_paths - cur_paths)
    modified = frozenset(
        path for path in (prev_paths & cur_paths) if previous[path] != current[path]
    )
    return FileChanges(added=added, modified=modified, removed=removed)


def refresh_manifest(root: Path, previous: Manifest) -> Manifest:
    """Rebuild a manifest for `root` using the *same file set* `previous`
    tracked, plus any new files discovered under `root`.

    Used by callers who want to pick up newly-added files (which a plain
    `is_fresh` check would just report as stale) rather than only refresh
    the files that already existed.
    """
    root = Path(root)
    discovered = set(discover_files(root))
    tracked = set(previous.file_hashes.keys())
    file_list = sorted(discovered | tracked)
    return build_manifest(root, file_list, collector_version=previous.collector_version)
