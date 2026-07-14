"""tools/auto/repo_ingest.py — AUTO-B1: Repo ingest + file clustering.

Walks the project tree and groups files into logical clusters so that each
cluster can be reviewed as one unit by the Architect agent (AUTO-B2+).

Public surface consumed by controller.py / the Architect:

    from tools.auto.repo_ingest import RepoIngestor, RepoCluster

    ingestor = RepoIngestor(base_dir, config)   # config is a ConfigParser
    clusters = ingestor.ingest()                # walk → cluster → print summary
    # clusters: list[RepoCluster], each has .name and .files (relative paths)

Configuration
-------------
Cluster definitions are read from ``agents.ini [architect]``.

    [architect]
    clusters = entry_orchestration:main*,*cli*,*app*
               agents:*agent*,tools/auto/*
               io_analysis:*reader*,*formatter*,*extractor*,*stream*,*parser*,*metrics*
               support:*

Each line is ``name:pattern1,pattern2,...`` where patterns are
``fnmatch``-style globs matched against the **relative** POSIX path.
The last cluster in the list acts as the catch-all for unmatched files;
if no explicit catch-all is present, one named ``support`` is added.

If ``[architect] clusters`` is absent the built-in four-cluster default
is used (see _DEFAULT_CLUSTERS below), which is tuned for this codebase.

Walk limits are inherited from the existing ``[search]`` section
(``skip_dirs``, ``max_depth``, ``max_file_kb``) so no new keys are needed
unless you want to override them under ``[architect]``.

agents.ini [architect] keys
----------------------------
clusters        — newline / semicolon separated  name:pat1,pat2  definitions
                  (optional; falls back to _DEFAULT_CLUSTERS)
skip_dirs       — override [search] skip_dirs for architect walk
max_depth       — override [search] max_depth for architect walk
max_file_kb     — override [search] max_file_kb for architect walk
"""

from __future__ import annotations

import configparser
from tools.auto.utils import _cfg_mode
import fnmatch
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

# ── Default cluster definitions ──────────────────────────────────────────────
# Tuned for this codebase's layout (tools/, tools/auto/, main.py, agents.ini);
# format is (name, [glob_patterns]), tried in order with first match winning.
# The last cluster should be a broad catch-all.

_DEFAULT_CLUSTERS: list[tuple[str, list[str]]] = [
    (
        "entry_orchestration",
        [
            "main.py",
            "*/main.py",
            "*orchestrat*",
            "*controller*",
            "*app.py",
            "*cli.py",
            "*__main__*",
            "*.ini",
            "*.cfg",
            "*.toml",
            "*.gradle",
            "*.kts",
            "*.yaml",
            "*.yml",
        ],
    ),
    (
        "agents",
        [
            "*agent*",
            "tools/auto/*",
            "*validator*",
            "*improver*",
            "*improvement*",
            "*optimizer*",
            "*evaluator*",
            "*llm*",
        ],
    ),
    (
        "io_analysis",
        [
            "*reader*",
            "*formatter*",
            "*extractor*",
            "*stream*",
            "*parser*",
            "*prompt_parser*",
            "*metrics*",
            "*collector*",
            "*output*",
            "*actions*",
        ],
    ),
    (
        "support",
        ["*"],   # catch-all — must be last
    ),
]

# AUTO-CR-28: agent-generated control/memory files. Read into context elsewhere,
# but NEVER editable story content — excluded from the walk so the architect
# can neither cite nor target them.
RESERVED_META_FILES: frozenset[str] = frozenset(
    {"synopsis.md", "improvements.md", "story_bible.md", "plan.json"}
)

# Extensions always skipped during the walk (binary / compiled / generated).
_BINARY_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".pyc", ".pyo", ".pyd",
        ".so", ".dll", ".dylib", ".exe",
        ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".ico", ".svg",
        ".mp3", ".mp4", ".wav", ".avi",
        ".zip", ".tar", ".gz", ".bz2", ".xz", ".rar", ".7z", ".7zip",
        ".pdf", ".docx", ".xlsx", ".pptx",
        ".db", ".sqlite", ".sqlite3",
        ".lock",            # package-manager lock files (large, not useful)
    }
)

# Backup / patch / editor-temp files: present in working trees but not real
# source. Reviewing them wastes prompt budget and their near-duplicate names
# (e.g. angie_ops.py.old) confuse the model into emitting non-verbatim paths.
_BACKUP_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".old", ".orig", ".rej", ".bak", ".patch", ".diff",
        ".tmp", ".temp", ".swp", ".swo", ".save", "~",
    }
)


# ─────────────────────────────────────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RepoCluster:
    """One logical review unit: a named group of relative file paths.

    Attributes
    ----------
    name:
        Short identifier, e.g. ``"entry_orchestration"``.
    patterns:
        The glob patterns that populate this cluster (informational).
    files:
        Relative POSIX paths of files that matched, sorted lexicographically.
    """

    name: str
    patterns: list[str]
    files: list[str] = field(default_factory=list)

    # ── Convenience helpers ──────────────────────────────────────────────────

    def __len__(self) -> int:
        return len(self.files)

    def __repr__(self) -> str:  # pragma: no cover
        return f"RepoCluster(name={self.name!r}, files={len(self.files)})"


# ─────────────────────────────────────────────────────────────────────────────
# RepoIngestor
# ─────────────────────────────────────────────────────────────────────────────

class RepoIngestor:
    """Walks a project tree and groups files into logical review clusters.

    Parameters
    ----------
    base_dir:
        Root of the project to walk.
    config:
        A ``configparser.ConfigParser`` instance.  The ``[search]`` and
        optional ``[architect]`` sections are consulted.  Pass ``None``
        to use all defaults.
    """

    def __init__(
        self,
        base_dir: str | Path,
        config: configparser.ConfigParser | None = None,
        task_mode: str = "code",
    ) -> None:
        self.base_dir  = Path(base_dir).resolve()
        self._config   = config or configparser.ConfigParser(inline_comment_prefixes=(';', '#'))
        self._task_mode = task_mode

        # Walk limits — prefer [architect] overrides, fall back to [search].
        # AUTO-CR-3: max_file_kb_creative overrides max_file_kb in creative mode.
        self._skip_dirs  = self._read_skip_dirs()
        self._max_depth  = self._read_int("max_depth",  2)
        self._max_file_kb = self._read_int_mode("max_file_kb", 500)

        # Cluster definitions — config-driven or built-in default.
        self._cluster_defs: list[tuple[str, list[str]]] = self._read_cluster_defs()

    # ── Public API ────────────────────────────────────────────────────────────

    def ingest(self) -> list[RepoCluster]:
        """Walk the repo, cluster the files, print a summary, and return clusters.

        Returns
        -------
        list[RepoCluster]
            One entry per defined cluster; empty clusters are included so
            downstream code can see which groups produced no files.
        """
        files = list(self.walk())
        clusters = self.cluster(files)
        self._print_summary(clusters)
        return clusters

    def walk(self) -> Iterator[str]:
        """Yield relative POSIX paths of every walkable file in *base_dir*.

        Applies skip_dirs, max_depth, max_file_kb, and binary-extension
        filters.  Hidden files (starting with ``.``) are skipped unless they
        are inside a directory that was not itself skipped.
        """
        base = self.base_dir
        for dirpath, dirnames, filenames in os.walk(base):
            rel_dir = Path(dirpath).relative_to(base)
            depth   = len(rel_dir.parts)

            # Prune directories that exceed max_depth or are on the skip list.
            dirnames[:] = [
                d for d in sorted(dirnames)
                if d not in self._skip_dirs
                and not d.startswith(".")
                and (self._max_depth <= 0 or depth < self._max_depth)
            ]

            for fname in sorted(filenames):
                if fname.startswith("."):
                    continue
                ext = Path(fname).suffix.lower()
                if ext in _BINARY_EXTENSIONS or ext in _BACKUP_EXTENSIONS:
                    continue
                if fname.endswith("~"):        # emacs/vim backup
                    continue
                # AUTO-CR-15 / AUTO-CR-28: never ingest agent-generated control
                # files. These are running MEMORY / plan state, not story
                # content — ingesting them lets the architect cite or target
                # them as if they were chapters, so the coder rewrites the bible
                # as prose and the redundancy gate loops forever (observed: a
                # "remove repetition" run spent ~1h editing story_bible.md).
                if fname.lower() in RESERVED_META_FILES:
                    continue

                abs_path = Path(dirpath) / fname
                try:
                    size_kb = abs_path.stat().st_size / 1024
                except OSError:
                    continue
                if self._max_file_kb > 0 and size_kb > self._max_file_kb:
                    logger.debug("walk: skipping large file %s (%.1f KB)", abs_path, size_kb)
                    continue

                rel_posix = (rel_dir / fname).as_posix()
                yield rel_posix

    def cluster(self, files: list[str]) -> list[RepoCluster]:
        """Assign each file path to the first matching cluster.

        Files that match no cluster (only possible if the caller supplies
        custom cluster defs with no catch-all) are silently dropped and
        logged at DEBUG level.

        Parameters
        ----------
        files:
            Relative POSIX paths, as returned by :meth:`walk`.

        Returns
        -------
        list[RepoCluster]
            One per cluster definition, in definition order.
        """
        clusters = [
            RepoCluster(name=name, patterns=list(patterns))
            for name, patterns in self._cluster_defs
        ]

        for rel_path in files:
            placed = False
            for cluster in clusters:
                if self._matches_any(rel_path, cluster.patterns):
                    cluster.files.append(rel_path)
                    placed = True
                    break
            if not placed:
                logger.debug("cluster: no cluster matched %r — file skipped", rel_path)

        # Sort files within each cluster for deterministic ordering.
        for cluster in clusters:
            cluster.files.sort()

        return clusters

    # ── Private helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _matches_any(rel_path: str, patterns: list[str]) -> bool:
        """Return True if *rel_path* matches at least one glob pattern.

        Matching is done against both the full relative path and the
        bare filename so that patterns like ``"*agent*"`` work without
        needing the full prefix.
        """
        filename = Path(rel_path).name
        for pat in patterns:
            if fnmatch.fnmatch(rel_path, pat) or fnmatch.fnmatch(filename, pat):
                return True
        return False

    # Well-known noise directories always pruned from the architect walk,
    # mirroring tools/auto/context_broker._iter_project_files.  Without this,
    # an empty/minimal [search] skip_dirs let node_modules/venv/dist/build be
    # ingested and clustered for review (dot-dirs were already pruned by the
    # walk's startswith('.') filter, but these non-dot ones were not).
    _DEFAULT_SKIP_DIRS: frozenset[str] = frozenset({
        "__pycache__", ".git", ".hg", ".svn",
        "node_modules", "venv", ".venv", "dist", "build", ".tox",
    })

    def _read_skip_dirs(self) -> set[str]:
        """Read skip_dirs, preferring [architect] then [search]; always union
        in the built-in noise-directory defaults."""
        raw = (
            self._config.get("architect", "skip_dirs", fallback=None)
            or self._config.get("search",   "skip_dirs", fallback="")
        )
        user = {d.strip() for d in raw.split(",") if d.strip()}
        return set(self._DEFAULT_SKIP_DIRS) | user

    def _read_int(self, key: str, default: int) -> int:
        """Read an int config key, preferring [architect] then [search]."""
        val = (
            self._config.get("architect", key, fallback=None)
            or self._config.get("search",   key, fallback=None)
        )
        try:
            return int(val) if val is not None else default
        except (ValueError, TypeError):
            return default

    def _read_int_mode(self, key: str, default: int) -> int:
        """Read a mode-suffixed int config key (AUTO-CR-3).

        Prefers [architect] then [search], with the mode-specific variant
        (e.g. ``max_file_kb_creative``) taking priority over the base key
        (``max_file_kb``) within each section — mirrors ``_read_int`` but
        routes lookups through ``_cfg_mode`` so creative-mode overrides win.
        """
        val = _cfg_mode(self._config, "architect", key, self._task_mode, fallback=None)
        if val is None:
            val = _cfg_mode(self._config, "search", key, self._task_mode, fallback=None)
        try:
            return int(val) if val is not None else default
        except (ValueError, TypeError):
            return default

    def _read_cluster_defs(self) -> list[tuple[str, list[str]]]:
        """Parse cluster definitions from config or return built-in defaults.

        Config format (value of ``[architect] clusters``):

            entry_orchestration:main*,*cli*,*app*
            agents:*agent*,tools/auto/*
            io_analysis:*reader*,*formatter*
            support:*

        Lines may be separated by newlines or semicolons.  Whitespace around
        names and patterns is stripped.  Empty lines are ignored.
        """
        raw = self._config.get("architect", "clusters", fallback="").strip()
        if not raw:
            return _DEFAULT_CLUSTERS

        defs: list[tuple[str, list[str]]] = []
        # Support both newline and semicolon as line separators.
        lines = [ln.strip() for ln in raw.replace(";", "\n").splitlines() if ln.strip()]
        for line in lines:
            if ":" not in line:
                logger.warning("_read_cluster_defs: ignoring malformed line %r", line)
                continue
            name, _, pat_str = line.partition(":")
            name = name.strip()
            patterns = [p.strip() for p in pat_str.split(",") if p.strip()]
            if name and patterns:
                defs.append((name, patterns))

        if not defs:
            logger.warning("_read_cluster_defs: config produced no clusters — using defaults")
            return _DEFAULT_CLUSTERS

        # Ensure there is at least one catch-all cluster at the end.
        last_patterns = defs[-1][1]
        if "*" not in last_patterns and "**" not in last_patterns:
            logger.debug(
                "_read_cluster_defs: no catch-all detected — appending 'support' cluster"
            )
            defs.append(("support", ["*"]))

        return defs

    @staticmethod
    def _print_summary(clusters: list[RepoCluster]) -> None:
        """Print a concise cluster summary to stdout."""
        total = sum(len(c) for c in clusters)
        print(f"\n📂 Repo ingest complete — {total} file(s) across {len(clusters)} cluster(s):")
        for cluster in clusters:
            file_preview = ", ".join(cluster.files[:4])
            if len(cluster.files) > 4:
                file_preview += f", … (+{len(cluster.files) - 4} more)"
            print(f"  [{cluster.name}]  {len(cluster.files)} file(s)")
            if file_preview:
                print(f"    {file_preview}")
        print()


# ─────────────────────────────────────────────────────────────────────────────
# Convenience factory — used by AutoController / Architect
# ─────────────────────────────────────────────────────────────────────────────

def ingest_repo(
    base_dir: str | Path,
    config: configparser.ConfigParser | None = None,
    task_mode: str = "code",
) -> list[RepoCluster]:
    """Create a :class:`RepoIngestor`, walk the repo, and return clusters.

    This is the preferred one-call entry-point for ``AutoController``.

    Parameters
    ----------
    base_dir:
        Root of the project to walk.
    config:
        Parsed ``agents.ini``.  Pass ``None`` to use all defaults.
    task_mode:
        BUGFIX: was missing entirely, so RepoIngestor always defaulted to
        "code" here regardless of the run's actual mode. That silently
        disabled the AUTO-CR-3 max_file_kb_creative override (and any other
        mode-suffixed [architect]/[search] key) for every real pipeline run,
        since pipeline.py's only call site — ``ingest_repo(controller.base_dir,
        cfg)`` — had no way to pass it through. Forward the controller's
        task_mode so creative-mode file-size overrides actually apply.

    Returns
    -------
    list[RepoCluster]
        Ordered list of clusters ready for per-cluster Architect review.
    """
    return RepoIngestor(base_dir, config, task_mode=task_mode).ingest()
