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

import configparser
import hashlib
import json
import logging
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from tools.git_run import run_git

logger = logging.getLogger(__name__)

COLLECTOR_VERSION = "2"  # V10: real parameter lists in `signature` — forces one full rebuild

#: Directories skipped when discovering manifest files, as [search] skip_dirs does elsewhere.
DEFAULT_SKIP_DIRS = frozenset(
    {".git", "__pycache__", ".pytest_cache", "node_modules", ".venv", "venv", ".collect"}
)


def skip_dirs_from_config(
    config: Optional["configparser.ConfigParser"] = None,
) -> frozenset:
    """`[collect] skip_dirs` of *config* added to `DEFAULT_SKIP_DIRS`.

    CC-13: before this there was no setting — a checkout that keeps round output
    (`contest-out/` with thousands of `.py` files, or a `rounds/` sandbox) was
    scanned whole. The value is a comma-separated list of *extra* directory
    names, added to the default list rather than replacing it, so
    `skip_dirs = contest-out` never re-enables `node_modules`. Empty (the
    default, and an absent key) is exactly today's list. A malformed value,
    a parser that is not one, or a key that is not a string degrades to the
    defaults — the manifest is built, never aborted, on a wrong setting.
    """
    out = set(DEFAULT_SKIP_DIRS)
    if config is None:
        return frozenset(out)
    try:
        # Raw, since a % in a directory name would fail interpolation.
        raw = config.get("collect", "skip_dirs", raw=True, fallback="")
    except (AttributeError, TypeError, ValueError, configparser.Error):
        return frozenset(out)
    if not isinstance(raw, str):
        return frozenset(out)
    for name in raw.split(","):
        name = name.strip()
        if "/" in name or "\\" in name:
            # the walk compares a directory's name: "rounds/sandbox" never matched and skipped nothing
            logger.warning("[collect] skip_dirs: %r is a path, not a directory name — ignored", name)
            continue
        if name:
            out.add(name)
    return frozenset(out)


def effective_skip_dirs(
    config: Optional["configparser.ConfigParser"] = None,
    extra: Optional[Iterable[str]] = None,
) -> frozenset:
    """`DEFAULT_SKIP_DIRS` plus `[collect] skip_dirs` of *config* plus *extra*:
    the one place the effective list is computed, so the walk, the manifest and
    the freshness check cannot disagree about it."""
    out = set(skip_dirs_from_config(config))
    if extra is not None:
        for name in extra:
            name = str(name).strip()
            if name:
                out.add(name)
    return frozenset(out)


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
    skip_dirs: Optional[Iterable[str]] = None,
    config: Optional["configparser.ConfigParser"] = None,
) -> List[str]:
    """Walk `root`, returning sorted POSIX-relative paths of matching files.

    Self-contained on purpose: COLLECT-2 must not depend on the AST scanner
    (COLLECT-4, EPIC B) which doesn't exist yet. The scanner is free to hand
    `build_manifest` its own file list instead of relying on this walk.

    `skip_dirs` is the effective list in force: `DEFAULT_SKIP_DIRS` plus
    `[collect] skip_dirs` of *config* (`effective_skip_dirs` is the one place
    it is computed). Passing `None` means "the defaults plus whatever *config*
    says", which is the same as today for a config that does not set the key.
    """
    root = Path(root)
    skip = effective_skip_dirs(config, None if skip_dirs is None else skip_dirs)
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
    callers (i.e. `is_fresh`) treat a missing key as a mismatch.

    AUTO-MANIFEST-GUARD-1: a file that exists (passes is_file()) but isn't
    readable (permissions changed after the check, or from the start) hit
    an unguarded `open()` inside hash_file() and crashed the whole
    freshness check. Treated the same as "missing" — omitted from the
    result, with a warning — rather than aborting every other file's hash
    over one unreadable path.
    """
    root = Path(root)
    result: Dict[str, str] = {}
    for rel in files:
        p = root / rel
        if p.is_file():
            try:
                result[rel] = hash_file(p)
            except OSError as exc:
                logger.warning("hash_tree: skipping unreadable file %s: %s", p, exc)
    return result


def _run_git(root: Path, *args: str) -> Optional[str]:
    """Run one git subcommand under *root* and return its stripped stdout,
    or ``None`` on any failure (missing git, not a repo, timeout, non-zero
    exit) — every caller in this module already treats a git-unavailable
    environment as "nothing to report", never an error to propagate.

    BUGFIX: ``-c core.quotepath=false`` disables git's default behaviour of
    octal-escaping non-ASCII path bytes in porcelain/status output (e.g. a
    Cyrillic filename renders as ``"\320\263\320\273..."`` rather than the
    real UTF-8 text). ``is_dirty``'s path-based exclusion filtering compares
    these strings directly against a plain Python path string built from
    ``exclude_dir`` — so when the EXCLUDED directory's own name contains
    non-ASCII characters (a non-default, non-English ``[collect] dir``
    config value — plausible for a non-English-language project, which this
    one is), the escaped path never matches the plain prefix, and
    ``is_dirty`` reports True for output that is entirely the collector's
    own, exactly the "dirty on every refresh" bug this module's docstrings
    already describe fixing once, reopened here for one case they missed.
    Fixed by this flag; before adding it the failure reproduced directly:

        exclude_dir = root / "выход"    (a Cyrillic collect-output dir)
        is_dirty(root, exclude_dir) == True   (WRONG — nothing outside
                                               "выход/" changed)

    Passing this flag on every call (not just the status/porcelain one) is
    harmless for the other subcommands this helper serves (``rev-parse``,
    ``log``) — it only affects how PATHS are quoted in output, and none of
    them emit paths.

    FL-2: the call goes through ``tools.git_run.run_git``, so the "git is
    unavailable" case above is reached only after the ladder has waited out a
    transient held index — the one way a `status` fails while the tree itself
    is perfectly fine.
    """
    try:
        proc = run_git(
            ["git", "-c", "core.quotepath=false", *args],
            cwd=str(root),
            timeout=10,
            encoding="utf-8",
            errors="surrogateescape",   # a name that is not UTF-8 is a name, as os.walk gives it
        )
    except subprocess.TimeoutExpired:
        # still "nothing to report", but not silently: the git was killed
        logger.warning("git %s in %s timed out after 10s", " ".join(args), root)
        return None
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.rstrip("\n")       # not strip(): porcelain's first " M path" lost its leading space


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
    # --no-optional-locks: status never writes .git/index.lock, so a timeout kill cannot leave it behind.
    # --untracked-files=all (HARDEN-4 D7): without it git collapses a whole untracked directory into
    # one `?? out/` entry, which is not under `out/collect/`, so the exclusion below could not
    # recognise the collector's own output and `dirty` was True on every build of a nested collect
    # dir. With it every entry is a file, so the path filter works for any layout.
    status = _run_git(root, "--no-optional-locks", "status", "--porcelain", "--untracked-files=all")
    if not status:
        return False
    if exclude_dir is None:
        return True
    try:
        rel = Path(exclude_dir).resolve().relative_to(Path(root).resolve())
    except ValueError:
        # A collect dir outside the repo cannot appear in its porcelain output, so no filter is needed.
        return True
    prefix = rel.as_posix().rstrip("/") + "/"
    for line in status.splitlines():
        # Porcelain v1 line: two status chars, a space, then the path (rename: "old -> new").
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
    # Effective skip_dirs when built; a changed list reads as stale, and old manifests read as default.
    skip_dirs: Tuple[str, ...] = field(default_factory=tuple)
    # CC-15: modules whose Pass B produced no summary (gave up or reply never parsed).
    # A --no-llm build records () — no summary is expected there. Older manifests
    # without the key read as () and are unaffected.
    unsummarized: Tuple[str, ...] = field(default_factory=tuple)
    # HARDEN-21 C: the `.ini` files `CONFIG_MAP.md` is built from — the primary config and every
    # `[collect] sibling_profiles` entry. A separate map, not extra keys in `file_hashes`, so a
    # profile edit shows as "one file changed" to nothing but this comparison and never appears as
    # an added or removed *module* in `--refresh`'s diff. Older manifests read as {} — and are
    # fresh for the setting's own purposes, since a tree without profiles hashes nothing.
    profile_hashes: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "collector_version": self.collector_version,
            "generated_at": self.generated_at,
            "git_sha": self.git_sha,
            "dirty": self.dirty,
            "file_hashes": dict(self.file_hashes),
            "skip_dirs": sorted(self.skip_dirs),
            "unsummarized": sorted(self.unsummarized),
            "profile_hashes": dict(self.profile_hashes),
        }

    def to_json(self, *, indent: int = 2) -> str:
        # sort_keys keeps the manifest stable and diffable, as Pass A JSON is (COLLECT-3).
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    @classmethod
    def from_dict(cls, d: dict) -> "Manifest":
        # Malformed manifests raise ValueError, which every read_manifest() call site already catches (AUTO-MANIFEST-GUARD-2).
        try:
            collector_version = d["collector_version"]
            generated_at = d["generated_at"]
        except KeyError as exc:
            raise ValueError(f"manifest is missing required field {exc}") from exc
        except TypeError as exc:
            # A valid JSON non-object also raises ValueError, so the call-site guards catch it (BUGFIX).
            raise ValueError(f"manifest is not a JSON object: {exc}") from exc
        return cls(
            collector_version=collector_version,
            generated_at=generated_at,
            git_sha=d.get("git_sha"),
            dirty=bool(d.get("dirty", False)),
            file_hashes=dict(d.get("file_hashes", {})),
            # A missing or malformed skip_dirs key falls back to the default; the manifest is stale, not unreadable.
            skip_dirs=tuple(sorted(d["skip_dirs"]))
            if isinstance(d.get("skip_dirs"), (list, tuple))
            and all(isinstance(x, str) for x in d["skip_dirs"])
            else tuple(sorted(DEFAULT_SKIP_DIRS)),
            # A missing or malformed unsummarized key falls back to [] (older manifests); the manifest is not unreadable.
            unsummarized=tuple(sorted(d.get("unsummarized", [])))
            if isinstance(d.get("unsummarized"), (list, tuple))
            and all(isinstance(x, str) for x in d.get("unsummarized", []))
            else tuple(),
            # HARDEN-21 C: a missing or malformed profile_hashes key reads as {} (older
            # manifests, and the common case of a tree with no sibling_profiles).
            profile_hashes=dict(d["profile_hashes"])
            if isinstance(d.get("profile_hashes"), dict)
            and all(isinstance(k, str) and isinstance(v, str) for k, v in d["profile_hashes"].items())
            else {},
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
    file_hashes: Optional[Dict[str, str]] = None,
    config: Optional["configparser.ConfigParser"] = None,
    skip_dirs: Optional[Iterable[str]] = None,
    unsummarized: Optional[Iterable[str]] = None,
    profile_hashes: Optional[Dict[str, str]] = None,
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

    `file_hashes`, if given, is a `{relative_path: sha256}` map the caller
    already computed with `hash_tree` over the same `files` — reused verbatim
    instead of re-hashing the tree. The incremental `--refresh` path already
    hashes every current file once to diff against the previous manifest, and
    the full build has the same list straight from its own scan, so without
    this a single `--collect` on a stale tree hashed the same files twice
    (once to decide what changed, once here, after `_write_artifact` had
    already touched disk).

    CC-13: `config` / `skip_dirs` set the effective `skip_dirs` the walk above
    uses, and that list is recorded on the manifest itself (see `Manifest.skip_dirs`)
    so a change of the setting is a change of the artifact, visible to
    `is_fresh` — without it, a manifest built with one list compared against a
    tree walked with another would drift silently.
    CC-15: `unsummarized` records the paths of modules whose Pass B produced no
    summary; a --no-llm build passes () so the manifest is fresh, and an older
    manifest without the key is read as () (below).
    HARDEN-21 C: `profile_hashes` is the `{path: sha256}` map of the `.ini` files
    `CONFIG_MAP.md` depends on, built with `hash_tree` over the same path list the
    caller hashes its modules with — `is_fresh(..., profile_hashes=...)` compares it.
    """
    root = Path(root)
    effective = effective_skip_dirs(config, skip_dirs)
    file_list = list(files) if files is not None else discover_files(root, skip_dirs=effective)
    if provenance is not None:
        git_sha, dirty = provenance
    else:
        git_sha, dirty = get_git_sha(root), is_dirty(root)
    return Manifest(
        collector_version=collector_version,
        generated_at=_utc_iso_now(),
        git_sha=git_sha,
        dirty=dirty,
        file_hashes=file_hashes if file_hashes is not None else hash_tree(root, file_list),
        skip_dirs=tuple(sorted(effective)),
        unsummarized=tuple(sorted(unsummarized)) if unsummarized is not None else tuple(),
        profile_hashes=dict(profile_hashes) if profile_hashes is not None else {},
    )


def write_manifest(manifest: Manifest, path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    from tools.auto.utils import atomic_write_text
    atomic_write_text(path, manifest.to_json() + "\n")


def read_manifest(path: Path) -> Manifest:
    return Manifest.from_json(Path(path).read_text(encoding="utf-8"))


def is_fresh(
    manifest: Manifest,
    root: Path,
    *,
    files: Optional[Iterable[str]] = None,
    hashes: Optional[Dict[str, str]] = None,
    skip_dirs: Optional[Iterable[str]] = None,
    profile_hashes: Optional[Dict[str, str]] = None,
) -> bool:
    """True iff `manifest` was built by the CURRENT collector_version AND
    `root`'s current content hashes exactly match `manifest`'s.

    By default the hash comparison re-*discovers* the file set under `root`
    (rather than only re-hashing the paths the manifest already knows
    about), so a file added since the manifest was built shows up as an
    extra key and makes the comparison fail — not just edits to
    already-tracked files. Removing or editing a tracked file is caught the
    same way: any difference in the resulting `{path: hash}` dict, whether
    it's a missing key, an extra key, or a changed value, means "not
    fresh". A tree rebuilt with no changes at all — even at a different
    timestamp — compares equal and is fresh.

    BUGFIX: this module's own module-level docstring documents
    `collector_version` as existing "so a format change can be detected" —
    but nothing anywhere in this package ever actually COMPARED it, on any
    of this function's four call sites (cli.action_check — and, through
    it, action_collect, which skips rebuilding ENTIRELY on fresh=True;
    loader's consumer-facing freshness check, which returns a
    schema-mismatched artifact labelled STATUS_FRESH to whatever downstream
    code depends on it). Every field in ModuleRecord.from_dict() degrades
    gracefully via `.get(key, default)` on a missing key — correct for a
    corrupt/hand-edited file, but it silently absorbed a genuine SCHEMA
    CHANGE the same way: a manifest built by an older collector_version
    could report "fresh" against an unchanged tree even though records
    built under the new version would carry a field the reused ones don't
    have.  Reproduced directly:

        action_check after a hand-simulated version bump:
          fresh: True                 <- WRONG, should be False
          message: up to date

    Checking `collector_version` here, once, closes it at every call site
    rather than needing four separate fixes (action_refresh already got its
    own version check separately, since it doesn't call is_fresh at all).

    CC-13: `skip_dirs`, when given, is the *effective* list in force now
    (`effective_skip_dirs` of the caller's config) and is compared against the
    list the manifest recorded. The file hashes already catch most of it — a
    changed list hashes a different file set — but the explicit comparison is
    what makes "the setting changed and nothing else did" report stale rather
    than fresh, and it holds on the `hashes=` path too, where the caller's own
    hash map may already have been built with the new list. Omitting it (the
    older call sites) keeps today's behaviour exactly.

    Pass `files` explicitly to freshness-check against a specific file set
    instead of a fresh directory walk (e.g. when the caller already has the
    scanner's own file list from EPIC B). Pass `hashes` to skip the hash
    pass entirely when the caller already holds a `hash_tree` result over
    that same `files` — `action_collect` did, and hashing the tree here and
    then again inside the `action_refresh` it delegates to was the second of
    the three full tree hashes one stale `--collect` used to cost.
    HARDEN-21 C: `profile_hashes` is the `{path: sha256}` map of the `.ini` files
    `CONFIG_MAP.md` depends on — the primary config and every `[collect]
    sibling_profiles` entry. Those are not in `file_hashes`, which holds modules only,
    so without this an edit to a sibling profile left the artifact reporting "up to
    date" until some `.py` file happened to change, and `CONFIG_MAP.md` kept the
    gap the profile was written to fix. Omitting it (the older call sites) keeps
    today's behaviour exactly.
    """
    if manifest.collector_version != COLLECTOR_VERSION:
        return False
    if skip_dirs is not None and tuple(sorted(skip_dirs)) != tuple(manifest.skip_dirs):
        return False
    if profile_hashes is not None and dict(profile_hashes) != dict(manifest.profile_hashes):
        return False
    # CC-15: a manifest with unsummarized modules is stale — they will be retried
    # at the next --collect / --refresh instead of being reported up to date.
    if manifest.unsummarized:
        return False
    if hashes is not None:
        return hashes == manifest.file_hashes
    root = Path(root)
    current_files = list(files) if files is not None else discover_files(root)
    return hash_tree(root, current_files) == manifest.file_hashes


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
    # Stamp the current COLLECTOR_VERSION, not the previous one, so a refreshed manifest is fresh (BUGFIX).
    return build_manifest(root, file_list, collector_version=COLLECTOR_VERSION)
