"""CC-2 — the target: a pinned, read-only tree and a collect model built for it once.

A report is written about a commit; the operator's checkout is on another branch,
dirty, or both. Judging a claim against the wrong tree is the quietest way to a
confident wrong verdict, and moving the operator's tree is not acceptable
(EPIC-CC principle 3; `tools/arena/gitref.py` states the same rule). `Target`
reads the commit through `git worktree add --detach` in a scratch directory: the
operator's `HEAD`, index and working tree are never written.

    with Target.open(repo, "origin/kc", scratch=scratch, expect_sha="afa53f1") as target:
        view = target.view()          # a RepoView pinned to target.sha
        model = target.collect()      # CollectModel, cached by sha

`parse_report_ref` is the other half: the refs and shas a report names about itself.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from tools.arena import gitref
from tools.claimcheck.anchors import PathRepoView
from tools.collect import cli as collect_cli
from tools.collect import loader as collect_loader


class TargetError(Exception):
    """The target cannot be built (or the view was asked to write); the message is
    one line: the git command and its first stderr line, and whether `fetch=True` helps."""


# ── parse_report_ref ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RefHint:
    """A ref and/or sha a report names about itself. `source` is the matched text as written."""

    ref: Optional[str]
    sha: Optional[str]
    source: str


_SHA = r"(?P<sha>[0-9a-fA-F]{7,40})(?![\w-])(?!\.\w)"
_REF = r"[\w][\w./\-]*"

# `Code: origin/kc @ afa53f1`, `origin/kc@afa53f1`; the ref has no `@` of its own.
_AT = re.compile(rf"(?<![\w@./\-])(?P<ref>{_REF}?)\s*@\s*{_SHA}")
# ``branch `kc` at 9f99300``, `branch kc @ 9f99300`
_BRANCH_AT = re.compile(
    rf"\bbranch(?:es)?\s+`?(?P<ref>{_REF}?)`?\s+(?:at|@)\s+`?{_SHA}`?", re.I)
# `ctx-overflow-fix (HEAD 7b4e5f9)`, `HEAD 7b4e5f9`, `HEAD: 7b4e5f9`
_HEAD = re.compile(rf"(?<![\w/.\-])HEAD\b[\s:=]*`?{_SHA}`?", re.I)
# `commit 7b4e5f9`, ``commit `7b4e5f9` ``, `commit: 7b4e5f9`
_COMMIT = re.compile(rf"\bcommit[\s:]*`?{_SHA}`?", re.I)


def parse_report_ref(text: str) -> list:
    """Every `RefHint` in *text*, in order of appearance, without duplicates; never raises.

    `ref="HEAD"` for `HEAD <sha>` (also inside `branch (HEAD <sha>)`), `ref=None` for a
    bare commit. The cue words are case-insensitive; the sha is returned lower-case. A
    `commit <word>` needs a digit in the word, so "commit defaced" is not a sha."""
    try:
        return _parse(text)
    except Exception:  # noqa: BLE001 - "never raises" is the contract
        return []


def _parse(text: str) -> list:
    if not isinstance(text, str) or not text:
        return []
    found = []   # (start, end, RefHint)
    for rx, kind in ((_BRANCH_AT, "branch"), (_AT, "at"), (_HEAD, "head"), (_COMMIT, "commit")):
        for m in rx.finditer(text):
            sha = m.group("sha").lower()
            if kind == "commit" and not any(c.isdigit() for c in sha):
                continue
            if kind == "at" and (not any(c.isdigit() for c in sha) or m.group("ref").lower() in _AT_NOT_REFS):
                continue
            ref = "HEAD" if kind == "head" else None if kind == "commit" else m.group("ref")
            if any(s < m.end() and m.start() < e for s, e, _ in found):
                continue   # inside a hint an earlier, more specific rule already made
            found.append((m.start(), m.end(), RefHint(ref, sha, m.group(0).strip())))
    found.sort(key=lambda item: item[0])
    out, seen = [], set()
    for _, _, hint in found:
        key = (hint.ref, hint.sha)
        if key not in seen:
            seen.add(key)
            out.append(hint)
    return out


#: words that stand before an `@` without being a ref ("Author @ 1234567" is a person)
_AT_NOT_REFS = frozenset(("author", "by", "from", "posted", "mail", "email"))


# ── git, with the environment the operator's shell may have set removed ──────

_GIT_ENV_DROP = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                 "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_PREFIX",
                 "GIT_EXTERNAL_DIFF")
#: no hook of the operator's repository runs in a tree we make (`post-checkout`), no gc.
_SAFE_C = ("-c", "core.hooksPath=/dev/null", "-c", "gc.auto=0", "-c", "core.fsmonitor=false")


def _env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV_DROP}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_PAGER"] = "cat"
    return env


def _run_git(cwd: Optional[Path], *args: str) -> subprocess.CompletedProcess:
    """`git <args>` in *cwd*; the single place a target runs git (tests count calls here)."""
    try:
        proc = subprocess.run(["git", *_SAFE_C, *args], cwd=None if cwd is None else str(cwd),
                              capture_output=True, env=_env())
    except OSError as err:
        raise TargetError(f"git {' '.join(args)}: {err}") from err
    # bytes in, bytes out (gitref bug 185): text mode would turn a CRLF blob's \r\n into \n
    return subprocess.CompletedProcess(
        proc.args, proc.returncode,
        proc.stdout.decode("utf-8", "surrogateescape"), proc.stderr.decode("utf-8", "replace"))


def _git(cwd: Optional[Path], *args: str, hint: str = "", strip: bool = True) -> str:
    """stdout of `git <args>` (stripped unless *strip* is False: a blob keeps its bytes); `TargetError` with the command and stderr's first line."""
    proc = _run_git(cwd, *args)
    if proc.returncode != 0:
        first = (proc.stderr.strip().splitlines() or [f"exit {proc.returncode}"])[0]
        raise TargetError(f"git {' '.join(args)}: {first}{hint}")
    return proc.stdout.strip() if strip else proc.stdout


def _is_url(repo: str) -> bool:
    return bool(re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*://", repo) or re.match(r"^[\w.\-]+@[\w.\-]+:", repo))


_SHORT_HEX = re.compile(r"^[0-9a-fA-F]{4,6}$")
_FULL_HEX = re.compile(r"^[0-9a-fA-F]{7,40}$")


# ── a lock that two processes (or threads) can share: mkdir is atomic ─────────

class _Lock:
    STALE = 600.0   # seconds: a lock older than this was left by a process that died

    def __init__(self, path: Path, wait: float = 300.0) -> None:
        self.path, self.wait = path, wait

    def __enter__(self):
        deadline = time.monotonic() + self.wait
        while True:
            try:
                self.path.mkdir()
                return self
            except FileExistsError:
                try:
                    if time.time() - self.path.stat().st_mtime > self.STALE:
                        shutil.rmtree(self.path, ignore_errors=True)
                        continue
                except OSError:
                    continue
                if time.monotonic() > deadline:
                    raise TargetError(f"lock {self.path}: held for more than {int(self.wait)} s")
                time.sleep(0.05)
            except OSError as err:
                raise TargetError(f"lock {self.path}: {err.strerror or err}") from err

    def __exit__(self, *exc) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


# ── the view ──────────────────────────────────────────────────────────────────

#: what a view may ask git; nothing that moves a ref, the index or a file.
_VIEW_GIT = frozenset(("show diff log cat-file rev-parse ls-tree ls-files grep blame "
                       "merge-base rev-list").split())
#: options that write a file or start a program (`-O<pager>`, `--output=`, a textconv filter).
_VIEW_FORBIDDEN = ("--output", "--ext-diff", "--textconv", "--open-files-in-pager",
                   "--exec-path", "--git-dir", "--work-tree", "--config-env",
                   "--contents")   # blame --contents reads a file outside the commit
#: a diff driver is named by the commit's .gitattributes and defined in the operator's config;
#: git runs its textconv / external diff by default, so the view turns both off for these.
_NO_FILTERS = frozenset(("show", "diff", "log"))


class _TargetView(PathRepoView):
    """`PathRepoView` over the pinned tree, with the ticket's git allow-list and `TargetError`."""

    def files(self) -> list:
        # `.collect/` is what Target.collect() wrote, not what the commit holds.
        return [p for p in super().files() if p != ".collect" and not p.startswith(".collect/")]

    def git(self, *args: str) -> str:
        if not args or args[0] not in _VIEW_GIT:
            raise TargetError(f"git {args[0] if args else ''}: not a read-only subcommand")
        for i, arg in enumerate(args):
            bad = (arg.startswith(_VIEW_FORBIDDEN) or arg.startswith("-O")
                   or arg.startswith("core.") or ("-c" in arg[:2] and "core." in arg)
                   or (arg == "-c" and i + 1 < len(args) and "=" in args[i + 1]))
            if bad:
                raise TargetError(f"git {args[0]} {arg}: refused, it writes or runs a program")
        if args[0] in _NO_FILTERS:
            args = (args[0], "--no-textconv", "--no-ext-diff", *args[1:])
        return _git(self.root, *args, strip=False)


# ── the target ────────────────────────────────────────────────────────────────

class Target:
    """A commit of a repository as a read-only tree. Build one with `Target.open`."""

    def __init__(self, *, repo: str, ref: str, sha: str, tree: Path, scratch: Path,
                 git_dir: Path, created: bool, warnings: list) -> None:
        self.repo, self.ref, self.sha = repo, ref, sha
        self.tree, self.scratch = tree, scratch
        self.warnings = warnings
        self._git_dir = git_dir      # the repository the worktree hangs off
        self._created = created      # this open made the tree (a reused one is left alone)
        self._closed = False
        self._models: dict = {}

    def __enter__(self) -> "Target":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # open ------------------------------------------------------------------

    @classmethod
    def open(cls, repo, ref: str, *, scratch, fetch: bool = False,
             expect_sha: Optional[str] = None) -> "Target":
        repo_s = str(repo)
        scratch = Path(scratch).absolute()   # git runs in another directory: a relative path would move
        warnings: list = []
        base = scratch / "claimcheck"
        try:
            base.mkdir(parents=True, exist_ok=True)
        except OSError as err:
            raise TargetError(f"cannot create {base}: {err.strerror or err}") from err

        if not isinstance(ref, str) or not ref.strip() or ref.startswith("-"):
            raise TargetError(f"ref {ref!r}: not a ref")
        ref = ref.strip()

        if _is_url(repo_s):
            repo_id = hashlib.sha1(repo_s.encode("utf-8")).hexdigest()[:16]
            git_dir = cls._clone_once(repo_s, base / "clones" / repo_id, base)
        else:
            path = Path(repo_s)
            if not path.exists():
                raise TargetError(f"git -C {repo_s}: no such directory")
            if _run_git(path, "rev-parse", "--is-bare-repository").stdout.strip() == "true":
                top = _git(path, "rev-parse", "--absolute-git-dir")
            else:
                top = _git(path, "rev-parse", "--show-toplevel")
            git_dir = Path(top)
            name = re.sub(r"[^\w.\-]+", "_", git_dir.name)[:40] or "repo"
            repo_id = f"{name}-{hashlib.sha1(os.path.realpath(top).encode('utf-8')).hexdigest()[:10]}"

        if fetch:
            cls._fetch(git_dir)
        sha = cls._resolve(git_dir, ref, fetch, is_url=_is_url(repo_s))
        if expect_sha and not cls._ref_is_sha(ref) and not sha.startswith(expect_sha.strip().lower()):
            warnings.append(f"the report says `{expect_sha.strip()}`, `{ref}` is `{sha[:7]}`")

        repo_dir = base / repo_id
        tree = repo_dir / sha / "tree"
        try:
            repo_dir.mkdir(parents=True, exist_ok=True)
        except OSError as err:
            raise TargetError(f"cannot create {repo_dir}: {err.strerror or err}") from err
        with _Lock(repo_dir / ".lock"):
            created = False
            if not cls._tree_is_ready(tree, sha):
                cls._remove_tree(git_dir, tree)
                tree.parent.mkdir(parents=True, exist_ok=True)
                _git(git_dir, "worktree", "add", "--detach", "--force", str(tree), sha)
                created = True
        return cls(repo=repo_s, ref=ref, sha=sha, tree=tree, scratch=scratch,
                   git_dir=git_dir, created=created, warnings=warnings)

    @staticmethod
    def _ref_is_sha(ref: str) -> bool:
        return bool(_FULL_HEX.match(ref))

    @staticmethod
    def _clone_once(url: str, clone: Path, base: Path) -> Path:
        """The clone of *url* under `clones/<hash>/`, made once and reused after."""
        clone.parent.mkdir(parents=True, exist_ok=True)
        with _Lock(base / "clones" / (clone.name + ".lock")):
            if (clone / ".git").exists() or (clone / "HEAD").exists():
                return clone
            if clone.exists():
                shutil.rmtree(clone, ignore_errors=True)   # a clone that died half way
            tmp = clone.with_name(clone.name + ".tmp")
            shutil.rmtree(tmp, ignore_errors=True)
            _git(None, "clone", "--quiet", "--no-checkout", "--filter=blob:none", url, str(tmp))
            tmp.rename(clone)
        return clone

    @staticmethod
    def _fetch(git_dir: Path) -> None:
        """`git fetch` of the remotes — all branches and tags, no pull, merge or reset."""
        remotes = _git(git_dir, "remote").split()
        if not remotes:
            return   # nothing to fetch from: a local-only repository
        _git(git_dir, "fetch", "--all", "--tags", "--no-write-fetch-head", "--no-recurse-submodules")

    @classmethod
    def _resolve(cls, git_dir: Path, ref: str, fetched: bool, *, is_url: bool) -> str:
        """*ref* as a full commit sha; an annotated tag is peeled."""
        if _SHORT_HEX.match(ref) and not cls._named_ref(git_dir, ref):
            raise TargetError(f"ref `{ref}`: a sha shorter than 7 characters is ambiguous, give 7 or more")
        candidates = [ref]
        if not cls._ref_is_sha(ref):
            candidates.append(f"origin/{ref}")   # a clone has the branches as remotes only
        last = ""
        for cand in candidates:
            proc = _run_git(git_dir, "rev-parse", "--verify", f"{cand}^{{commit}}")
            if proc.returncode == 0 and proc.stdout.strip():
                return proc.stdout.strip()
            last = last or (proc.stderr.strip().splitlines() or [f"exit {proc.returncode}"])[0]
        has_remote = is_url or bool(_git(git_dir, "remote").split())
        if fetched:
            help_ = "; fetch=True was set and did not bring it"
        elif has_remote:
            help_ = "; fetch=True may bring it"
        else:
            help_ = "; fetch=True would not help, the repository has no remote"
        what = "sha is not in the object database" if cls._ref_is_sha(ref) else "ref does not resolve"
        raise TargetError(f"git rev-parse --verify {ref}^{{commit}}: {last} ({what}){help_}")

    @staticmethod
    def _named_ref(git_dir: Path, name: str) -> bool:
        for prefix in ("refs/heads/", "refs/tags/", "refs/remotes/"):
            if _run_git(git_dir, "show-ref", "--verify", "--quiet", prefix + name).returncode == 0:
                return True
        return False

    @staticmethod
    def _tree_is_ready(tree: Path, sha: str) -> bool:
        """Reusable: it is a worktree, its `HEAD` is *sha*, no tracked file was changed."""
        if not (tree / ".git").exists():
            return False
        head = _run_git(tree, "rev-parse", "HEAD")
        if head.returncode != 0 or head.stdout.strip() != sha:
            return False
        status = _run_git(tree, "status", "--porcelain", "--untracked-files=no")
        return status.returncode == 0 and not status.stdout.strip()

    @staticmethod
    def _remove_tree(git_dir: Path, tree: Path) -> None:
        if tree.exists():
            _run_git(git_dir, "worktree", "remove", "--force", str(tree))
            shutil.rmtree(tree, ignore_errors=True)
        _run_git(git_dir, "worktree", "prune")

    # view ------------------------------------------------------------------

    def view(self) -> PathRepoView:
        """A read-only `RepoView` over the tree; `git(...)` is the allow-listed subset."""
        return _TargetView(self.tree)

    # collect ---------------------------------------------------------------

    def collect(self, config=None):
        """The `CollectModel` of the tree, built into `<tree>/.collect/` once per sha.

        A failure of collect (not a Python repository, a parse error, 300 s) does not fail
        the target: the result is the loader's absent model and `warnings` says why."""
        key = id(config)
        if key in self._models:
            return self._models[key]
        model = None
        try:
            model = collect_loader.load(self.tree, config=config)
        except Exception:  # noqa: BLE001 - an unreadable artifact is built again below
            model = None
        if model is None or model.status != collect_loader.STATUS_FRESH:
            model = self._build_collect(config)
        self._models[key] = model
        return model

    def _build_collect(self, config):
        box: dict = {}

        def work() -> None:
            try:
                collect_cli.action_collect(self.tree, config=config)
                box["model"] = collect_loader.load(self.tree, config=config)
            except BaseException as err:  # noqa: BLE001 - SystemExit from a cli helper too
                box["error"] = err

        thread = threading.Thread(target=work, daemon=True, name="claimcheck-collect")
        thread.start()
        thread.join(COLLECT_TIMEOUT)
        if thread.is_alive():
            reason = f"collect did not finish in {int(COLLECT_TIMEOUT)} s"
        elif "error" in box:
            err = box["error"]
            reason = f"collect failed: {(str(err).strip().splitlines() or [type(err).__name__])[0]}"
        else:
            return box["model"]
        self.warnings.append(reason)
        return collect_loader._absent(collect_cli.resolve_collect_dir(self.tree, config), reason)

    # close -----------------------------------------------------------------

    def close(self, remove: bool = False) -> None:
        """Remove the worktree this target made (a reused one only with `remove=True`)."""
        if self._closed and not remove:
            return
        if self._created or remove:
            self._remove_tree(self._git_dir, self.tree)
            try:
                self.tree.parent.rmdir()   # the <sha> folder, when nothing else is in it
            except OSError:
                pass
        self._closed = True


#: seconds the collect of one tree may take before the target goes on without a model.
COLLECT_TIMEOUT = 300.0
