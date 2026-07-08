"""tools/auto/plan_emitter.py — AUTO-B5: Emit & commit the plan.

Writes two artefacts after the Architect + Gate-1 + Prioritiser pipeline:

1. ``IMPROVEMENTS.md`` — human-readable backlog committed to the repo root
   so any developer can read what the agent intends to do.
2. ``.agent/plan.json`` — machine-readable task list consumed by the Coder
   loop (written via :class:`~tools.auto.state.StateStore`).

Both are then committed with the agent identity (AUTO-A3 / GitManager).

Re-run cheapness
----------------
To avoid re-reviewing clusters that haven't changed, the emitter records a
SHA-256 fingerprint for each cluster's file-list in ``.agent/cluster_hashes.json``.
On the next invocation of :meth:`PlanEmitter.emit`, only clusters whose
fingerprint differs from the stored value are marked as "changed"; the caller
can use :meth:`PlanEmitter.changed_clusters` to restrict which clusters the
Architect re-reviews.

This keeps re-runs cheap: if only one cluster changed, one LLM call is made
instead of four.

Public surface consumed by ``controller.py``::

    from tools.auto.plan_emitter import PlanEmitter

    emitter = PlanEmitter(
        base_dir   = Path("."),       # repo root — where IMPROVEMENTS.md lives
        state      = store,           # StateStore (AUTO-A2)
        git        = git_manager,     # GitManager (AUTO-A3)
    )

    # Emit artefacts, upsert tasks into state, commit.
    commit_hash = emitter.emit(backlog, clusters)
    # Returns the commit hash (str) or None when nothing changed.

    # On the NEXT run — find out which clusters need re-reviewing:
    stale = emitter.changed_clusters(clusters)
    # stale: list[RepoCluster] — only those whose file-list changed.

Commit message
--------------
``auto(AUTO-B5): emit plan — N task(s)``
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from tools.auto.backlog_prioritiser import PrioritisedBacklog, to_improvements_md
from tools.auto.utils import file_set_fingerprint

if TYPE_CHECKING:  # avoid circular imports at runtime
    from tools.auto.git_manager import GitManager
    from tools.auto.repo_ingest import RepoCluster
    from tools.auto.state import StateStore

logger = logging.getLogger(__name__)

# Name of the file written to the repo root.
IMPROVEMENTS_FILENAME = "IMPROVEMENTS.md"

# Name of the cluster-hash cache inside .agent/
_CLUSTER_HASHES_FILENAME = "cluster_hashes.json"

# Commit message template.
_COMMIT_TASK_ID = "AUTO-B5"


class PlanEmitter:
    """Writes IMPROVEMENTS.md and seeds plan.json, then commits both.

    Parameters
    ----------
    base_dir:
        Repo root directory.  ``IMPROVEMENTS.md`` is written here.
    state:
        A :class:`~tools.auto.state.StateStore` instance (already
        initialised).  Tasks are upserted into it.
    git:
        A :class:`~tools.auto.git_manager.GitManager` instance (already
        set up with the agent identity).
    """

    def __init__(
        self,
        base_dir: str | Path,
        state: "StateStore",
        git: "GitManager",
    ) -> None:
        self._base_dir = Path(base_dir)
        self._state    = state
        self._git      = git
        self._hashes_path = state.agent_dir / _CLUSTER_HASHES_FILENAME

    # ── Public API ────────────────────────────────────────────────────────────

    def emit(
        self,
        backlog: PrioritisedBacklog,
        clusters: "list[RepoCluster] | None" = None,
    ) -> Optional[str]:
        """Write artefacts, upsert tasks, update cluster hashes, commit.

        Parameters
        ----------
        backlog:
            The :class:`~tools.auto.backlog_prioritiser.PrioritisedBacklog`
            produced by the Architect + Gate-1 + Prioritiser pipeline.
        clusters:
            The :class:`~tools.auto.repo_ingest.RepoCluster` list used for
            this run.  When supplied, the cluster-file-list fingerprints are
            updated so that the *next* call to :meth:`changed_clusters` can
            detect stale clusters cheaply.  Pass ``None`` to skip hash update.

        Returns
        -------
        str or None
            The git commit hash if a commit was created; ``None`` if there
            were no changes to commit (idempotent re-run).
        """
        # 1. Write IMPROVEMENTS.md to repo root.
        md_content  = to_improvements_md(backlog)
        md_path     = self._base_dir / IMPROVEMENTS_FILENAME
        md_path.write_text(md_content, encoding="utf-8")
        logger.info("emit: wrote %s (%d bytes)", md_path, len(md_content))

        # 2. Upsert all auto tasks into plan.json via StateStore.
        state_tasks = backlog.to_state_tasks()
        for task in state_tasks:
            self._state.upsert_task(task)
        logger.info(
            "emit: upserted %d auto task(s) into plan.json", len(state_tasks)
        )

        # 3. Update cluster hashes (re-run cheapness).
        if clusters is not None:
            self._save_cluster_hashes(clusters)

        # 4. Log a summary line.
        n_auto   = len(backlog.auto_tasks)
        n_manual = len(backlog.manual_suggestions)
        self._state.log(
            f"emit: plan written — {n_auto} auto task(s), "
            f"{n_manual} manual suggestion(s)"
        )

        # 5. Commit IMPROVEMENTS.md + .agent/ changes with agent identity.
        commit_msg = (
            f"auto({_COMMIT_TASK_ID}): emit plan — {n_auto} task(s)"
        )
        commit_hash = self._git.commit(commit_msg)
        if commit_hash:
            logger.info("emit: committed as %s", commit_hash[:12])
            self._state.log(f"emit: committed plan — {commit_hash[:12]}")
        else:
            logger.info("emit: nothing to commit (plan unchanged)")
            self._state.log("emit: plan unchanged — nothing committed")

        return commit_hash

    def changed_clusters(
        self,
        clusters: "list[RepoCluster]",
    ) -> "list[RepoCluster]":
        """Return only those clusters whose file-list has changed since last emit.

        Uses the SHA-256 fingerprint of the sorted file-list string stored in
        ``.agent/cluster_hashes.json``.  If no hash file exists (first run),
        all clusters are returned.

        Parameters
        ----------
        clusters:
            Full cluster list from :class:`~tools.auto.repo_ingest.RepoIngestor`.

        Returns
        -------
        list[RepoCluster]
            Subset of *clusters* that are new or whose contents differ from
            the stored fingerprint.  Empty list only when every cluster is
            identical to the last emit.
        """
        stored = self._load_cluster_hashes()
        stale: list = []
        for cluster in clusters:
            current_hash = _cluster_hash(cluster, self._base_dir)
            if stored.get(cluster.name) != current_hash:
                stale.append(cluster)
                logger.debug(
                    "changed_clusters: %r is stale (stored=%r, current=%r)",
                    cluster.name,
                    stored.get(cluster.name),
                    current_hash,
                )
            else:
                logger.debug("changed_clusters: %r is unchanged", cluster.name)
        return stale

    # ── Private ───────────────────────────────────────────────────────────────

    def _save_cluster_hashes(self, clusters: "list[RepoCluster]") -> None:
        """Persist current cluster fingerprints to .agent/cluster_hashes.json."""
        hashes = {c.name: _cluster_hash(c, self._base_dir) for c in clusters}
        self._hashes_path.write_text(
            json.dumps(hashes, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.debug("_save_cluster_hashes: wrote %d entry/entries", len(hashes))

    def _load_cluster_hashes(self) -> dict[str, str]:
        """Load stored cluster fingerprints; return empty dict if absent."""
        if not self._hashes_path.exists():
            return {}
        try:
            return json.loads(self._hashes_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("_load_cluster_hashes: could not read hash file: %s", exc)
            return {}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _cluster_hash(cluster: "RepoCluster", base_dir: "str | Path | None" = None) -> str:
    """Return a stable SHA-256 hex digest identifying a cluster's file list.

    The hash is over the newline-joined sorted relative paths, encoded as
    UTF-8.  The cluster *name* is also included so that renaming a cluster
    (without changing its files) registers as a change.

    Bugfix: when *base_dir* is given, each file's size and mtime are folded
    in too (via tools.auto.utils.file_set_fingerprint — the same fingerprint
    architect.py's checkpoint uses for the identical "did the content
    actually change?" question). Previously this hash was blind to file
    CONTENT — only to which paths were in the cluster — so editing a file in
    place (by hand, or by an earlier auto task's own coder) never changed
    the hash. changed_clusters() would then report that cluster "unchanged"
    forever, silently skipping it from all future re-review.

    *base_dir* is optional and defaults to the old, path-only behaviour so
    this function's return format is unaffected for any direct caller that
    doesn't have a base_dir handy; PlanEmitter itself always passes its own
    ``self._base_dir``.
    """
    content = cluster.name + "\n" + "\n".join(sorted(cluster.files))
    if base_dir is not None:
        content += "\n" + file_set_fingerprint(base_dir, cluster.files)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
