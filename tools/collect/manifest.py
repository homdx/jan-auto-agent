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

import dataclasses
import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

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


def is_dirty(root: Path) -> bool:
    """True if the working tree has uncommitted changes. False (not
    unknown) if git is unavailable or `root` isn't a repo — a manifest built
    outside git has nothing to report here, and that's not the same claim
    as "the tree is dirty"."""
    status = _run_git(root, "status", "--porcelain")
    return bool(status)


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
) -> Manifest:
    """Build a `Manifest` for `root`. If `files` isn't given, discovers
    `*.py` files under `root` via `discover_files`."""
    root = Path(root)
    file_list = list(files) if files is not None else discover_files(root)
    return Manifest(
        collector_version=collector_version,
        generated_at=_utc_iso_now(),
        git_sha=get_git_sha(root),
        dirty=is_dirty(root),
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
