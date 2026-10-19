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

import configparser
import copy
import hashlib
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from tools.claimcheck.anchors import PathRepoView
from tools.collect import cli as collect_cli
from tools.collect import loader as collect_loader

#: The pipeline's `[search] max_depth` (see `agents.ini`), which `RepoIngestor` reads when
#: `Target.collect` is handed a config. When it is not, claim-check falls back to this so
#: `src/`-layout projects with three-level packages still resolve their imports.
_COLLECT_DEFAULT_MAX_DEPTH = 8

#: A singleton default config, so `id()` is stable across `Target.collect` calls when the
#: caller passes `None` — the cache key in `collect` is `id(config)`, and a fresh config
#: per call would defeat the "once per sha" contract.
_COLLECT_DEFAULT_CONFIG = None  # type: ignore[misc]  # set in `_default_config`


def _default_config() -> configparser.ConfigParser:
    """The singleton config used when `collect` is handed `None`. Never rebuilt, so
    `id(config)` is the same object every call and the model cache in `Target._models`
    hits as it did before HARDEN-21 D."""
    global _COLLECT_DEFAULT_CONFIG
    if _COLLECT_DEFAULT_CONFIG is None:
        cfg = configparser.ConfigParser(inline_comment_prefixes=(';', '#'))
        cfg["search"] = {"max_depth": str(_COLLECT_DEFAULT_MAX_DEPTH)}
        _COLLECT_DEFAULT_CONFIG = cfg
    return _COLLECT_DEFAULT_CONFIG


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
#: seconds one `view().git(...)` call may take (CC-4: a hung git is a note, not a hang)
VIEW_GIT_TIMEOUT = 20.0
#: no hook of the operator's repository runs in a tree we make (`post-checkout`), no gc.
_SAFE_C = ("-c", "core.hooksPath=/dev/null", "-c", "gc.auto=0", "-c", "core.fsmonitor=false")


def _env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV_DROP}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_PAGER"] = "cat"
    return env


def _run_git(cwd: Optional[Path], *args: str, view: bool = False) -> subprocess.CompletedProcess:
    """`git <args>` in *cwd*; the single place a target runs git (tests count calls here).

    *view*: a read for evidence — the operator's global and system config are not read
    (`color.ui=always`, `diff.noprefix` changed what a voter is shown) and the call is
    killed after `VIEW_GIT_TIMEOUT` seconds."""
    env = _env()
    if view:
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    try:
        proc = subprocess.run(["git", *_SAFE_C, *args], cwd=None if cwd is None else str(cwd),
                              capture_output=True, env=env, timeout=VIEW_GIT_TIMEOUT if view else None)
    except subprocess.TimeoutExpired as err:
        raise TargetError(f"git {' '.join(args)}: timed out after {err.timeout:g} s") from err
    except OSError as err:
        raise TargetError(f"git {' '.join(args)}: {err}") from err
    # bytes in, bytes out (gitref bug 185): text mode would turn a CRLF blob's \r\n into \n
    return subprocess.CompletedProcess(
        proc.args, proc.returncode,
        proc.stdout.decode("utf-8", "surrogateescape"), proc.stderr.decode("utf-8", "replace"))


def _git(cwd: Optional[Path], *args: str, hint: str = "", strip: bool = True, view: bool = False) -> str:
    """stdout of `git <args>` (stripped unless *strip* is False: a blob keeps its bytes); `TargetError` with the command and stderr's first line."""
    proc = _run_git(cwd, *args, view=True) if view else _run_git(cwd, *args)
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
    STALE = 600.0   # seconds: a lock older than this, with no live owner on record, was left by a crash

    def __init__(self, path: Path, wait: float = 300.0) -> None:
        self.path, self.wait = path, wait
        self.token = f"{os.getpid()}-{uuid.uuid4().hex}"

    def __enter__(self):
        deadline = time.monotonic() + self.wait
        while True:
            try:
                self.path.mkdir()
            except FileExistsError:
                stale, seen = self._stale()
                if stale:
                    self._break(seen)
                    continue
                if time.monotonic() > deadline:
                    raise TargetError(f"lock {self.path}: held for more than {int(self.wait)} s")
                time.sleep(0.05)
                continue
            except OSError as err:
                raise TargetError(f"lock {self.path}: {err.strerror or err}") from err
            try:
                (self.path / "owner").write_text(self.token, encoding="utf-8")
            except OSError:
                pass
            return self

    def _stale(self) -> tuple:
        """(stale, the owner token it was judged by, or None). A lock is broken only when its
        owner is gone: a live owner on this host keeps it however long it takes (a clone past
        STALE was broken and its .tmp deleted under it); a lock with no owner on record falls
        back to its age."""
        try:
            owner = (self.path / "owner").read_text(encoding="utf-8")
        except OSError:
            owner = None
        if owner is not None:
            pid = _token_pid(owner)
            if pid is not None:
                return not _pid_alive(pid), owner
        try:
            return time.time() - self.path.stat().st_mtime > self.STALE, owner
        except OSError:
            return False, owner

    def _break(self, seen: "str | None" = None) -> None:
        """Move the stale lock *seen* aside and delete it. Two waiters that both judged the dead
        lock stale: the first breaks it and takes a new one, and the second's rename used to move
        that NEW, live lock aside and delete it — both then held "the" lock. What was moved is
        checked against what was judged, and a lock that is not the one judged is put back."""
        aside = self.path.with_name(f"{self.path.name}.stale-{uuid.uuid4().hex[:8]}")
        try:
            self.path.rename(aside)
        except OSError:
            return
        try:
            moved = (aside / "owner").read_text(encoding="utf-8")
        except OSError:
            moved = None
        if moved != seen:
            try:
                aside.rename(self.path)     # a live lock taken in between: its owner keeps it
                return
            except OSError:
                pass                        # a third lock is there already: this one cannot go back
        shutil.rmtree(aside, ignore_errors=True)

    def __exit__(self, *exc) -> None:
        try:
            mine = (self.path / "owner").read_text(encoding="utf-8") == self.token
        except OSError:
            mine = False
        if mine:   # a lock broken and taken over by another process is not ours to remove
            shutil.rmtree(self.path, ignore_errors=True)


def _token_pid(token: str) -> "int | None":
    """The pid of a ``<pid>-<uuid>`` token (a users/ entry, a lock's owner), or None for any other name."""
    head = str(token).split("-", 1)[0].strip()
    return int(head) if head.isdigit() else None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


# ── the view ──────────────────────────────────────────────────────────────────

#: what a view may ask git; nothing that moves a ref, the index or a file.
_VIEW_GIT = frozenset(("show diff log cat-file rev-parse ls-tree ls-files grep blame "
                       "merge-base rev-list").split())
#: options that write a file or start a program (`-O<pager>`, `--output=`, a textconv filter).
_VIEW_FORBIDDEN = ("--output", "--ext-diff", "--textconv", "--open-files-in-pager",
                   "--exec-path", "--git-dir", "--work-tree", "--config-env",
                   "--contents",   # blame --contents reads a file outside the commit
                   "--no-index")   # diff/grep --no-index read any path on disk, not the commit
#: Diff drivers are turned off for these git commands, so the view is plain text.
_NO_FILTERS = frozenset(("show", "diff", "log", "blame"))   # blame runs textconv too


class _TargetView(PathRepoView):
    """`PathRepoView` over the pinned tree, with the ticket's git allow-list and `TargetError`."""

    @staticmethod
    def _is_collect(path: str) -> bool:
        norm = os.path.normpath(path.strip("/")) if path else ""
        return norm == ".collect" or norm.startswith(".collect" + os.sep)

    # .collect/ comes from the collect run, not from the commit, so files() and read() say so.
    def files(self) -> list:
        return [p for p in super().files() if not self._is_collect(p)]

    def exists(self, path: str) -> bool:
        return False if self._is_collect(path) else super().exists(path)

    def read(self, path: str) -> str:
        if self._is_collect(path):
            raise FileNotFoundError(path)
        return super().read(path)

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
        return _git(self.root, *args, strip=False, view=True)


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
        self._token: Optional[Path] = None   # this open's entry in <sha>/users (see close)
        self._closed = False
        self._models: dict = {}
        self._config_memo: dict = {}   # id(caller config) -> (caller config, config with a depth)

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
            # Each open is on record under users/, so one close never removes a tree another open still uses.
            users = tree.parent / "users"
            users.mkdir(exist_ok=True)
            token = users / f"{os.getpid()}-{uuid.uuid4().hex}"
            token.touch()
            if created:
                # A maker killed mid-open leaves its tree behind, so the tree is taken over by its owner only.
                (tree.parent / "creator").write_text(token.name, encoding="utf-8")
        # A local path is recorded absolute, so votes.json works from any directory.
        target = cls(repo=repo_s if _is_url(repo_s) else os.path.abspath(repo_s), ref=ref, sha=sha, tree=tree, scratch=scratch,
                     git_dir=git_dir, created=created, warnings=warnings)
        target._token = token
        return target

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
            # A clone's local branch does not move on fetch; a URL target looks at origin/<ref> first.
            candidates.insert(0 if is_url else 1, f"origin/{ref}")
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
        """A read-only `RepoView` over the tree; `git(...)` is the allow-listed subset. A tree
        that is gone is a `TargetError`, never a view that finds nothing (empty evidence would
        turn every code claim into a world claim)."""
        if not self.tree.is_dir():
            raise TargetError(f"worktree {self.tree} is gone (removed while this target was open)")
        return _TargetView(self.tree)

    # collect ---------------------------------------------------------------

    def collect(self, config=None):
        """The `CollectModel` of the tree, built into `<tree>/.collect/` once per sha.

        A failure of collect (not a Python repository, a parse error, 300 s) does not fail
        the target: the result is the loader's absent model and `warnings` says why.

        `config` may be `None`: the collect defaults to `[search] max_depth = 8` — the
        same value `agents.ini` sets — so a `src/`-layout project with a three-level
        package still resolves its imports. Without this, `RepoIngestor` falls back to
        `max_depth = 2` and every module of a nested package sits out of the index, so
        the import graph, callers, config reads, gates and contracts in the evidence
        packs are missing without any warning (HARDEN-21 D).
        """
        config = self._collect_config(config)
        # The config is held by the cache entry, so a reused id() cannot return another config's model.
        key = id(config)
        hit = self._models.get(key)
        if hit is not None and hit[0] is config:
            return hit[1]
        model = None
        try:
            model = collect_loader.load(self.tree, config=config)
        except Exception:  # noqa: BLE001 - an unreadable artifact is built again below
            model = None
        if model is None or model.status != collect_loader.STATUS_FRESH:
            model = self._build_collect(config)
        # HARDEN-21 D: a truncated walk should say so, not silently omit deep files.
        trunc = self._truncation_reason(config)
        if trunc:
            self.warnings.append(trunc)
        self._models[key] = (config, model)
        return model

    @staticmethod
    def _names_a_depth(config) -> bool:
        """Does *config* name a walk depth, in the sections `RepoIngestor` reads it from?"""
        try:
            for section in ("architect", "search"):
                value = config.get(section, "max_depth", fallback=None)
                if value is not None and str(value).strip():
                    return True
        except (configparser.Error, AttributeError, TypeError, ValueError):
            return False
        return False

    def _collect_config(self, config):
        """The config claim-check's collect uses: *config* when it names a walk depth, else a
        copy of it (or a minimal `[search]` section) with `max_depth = 8` — the value
        `agents.ini` sets. The claim-check ini (`contest.ini`) has no `[search]` at all, so
        handing it over unchanged would leave `RepoIngestor` at its own fallback of 2 and
        silently truncate the walk.

        One effective config per caller config, kept on the target: the model cache below is
        keyed by `id()`, and a copy made on every call would never hit it. A caller config is
        held by the memo, so a reused `id()` cannot return another config's copy.
        """
        if config is None:
            return _default_config()
        if self._names_a_depth(config):
            return config
        hit = self._config_memo.get(id(config))
        if hit is not None and hit[0] is config:
            return hit[1]
        effective = copy.deepcopy(config)
        if not effective.has_section("search"):
            effective.add_section("search")
        effective.set("search", "max_depth", str(_COLLECT_DEFAULT_MAX_DEPTH))
        self._config_memo[id(config)] = (config, effective)
        return effective

    @staticmethod
    def _max_depth(config):
        """`[search] max_depth` from `config` (the same key `RepoIngestor` reads) —
        the value that decides how deep the collect walked, so a truncated scan can
        say so in `warnings` instead of silently omitting deep files."""
        try:
            raw = (config.get("architect", "max_depth", fallback=None)
                   or config.get("search", "max_depth", fallback=None))
            return int(raw) if raw is not None else _COLLECT_DEFAULT_MAX_DEPTH
        except (configparser.Error, AttributeError, TypeError, ValueError):
            return _COLLECT_DEFAULT_MAX_DEPTH

    def _truncation_reason(self, config):
        """A warning when the collect walk hit `max_depth`, or `""` otherwise.

        Files one level past the limit are the signal: the walk prunes any directory at
        or beyond the limit, so their presence means the scan is incomplete (HARDEN-21 D).
        Fail-open: a broken view, a malformed config or an unbounded depth
        (`max_depth <= 0`) returns `""` — no warning, no crash.
        """
        try:
            depth = self._max_depth(config)
            if depth <= 0:
                return ""
            limit = depth
        except Exception:  # noqa: BLE001 — the warning is a nice-to-have, never a hard failure
            return ""
        try:
            view = self.view()
        except Exception:  # noqa: BLE001
            return ""
        try:
            for path in view.files():
                # RepoIngestor yields files from directories at depth <= max_depth, so a
                # file with `parts` count > max_depth + 1 lives in a pruned directory.
                parts = Path(path).parts
                if len(parts) > limit + 1:
                    return f"collect walk hit max_depth={limit}; files deeper than that are not in the model"
        except Exception:  # noqa: BLE001
            return ""
        return ""

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
        """Remove the worktree this target made (a reused one only with `remove=True`, or when the
        process that made it is gone without removing it), once no other open of it is still in use:
        then the last one to close removes it."""
        if self._closed and not remove:
            return
        self._closed = True
        users = self.tree.parent / "users"
        marker = self.tree.parent / ".remove-when-free"
        with _Lock(self.tree.parent.parent / ".lock"):
            if self._token is not None:
                self._token.unlink(missing_ok=True)
                self._token = None
            # A name that is not one of our tokens is skipped, instead of raising and leaving the worktree.
            others = [u for u in users.glob("*") if (pid := _token_pid(u.name)) is not None and _pid_alive(pid)] \
                if users.is_dir() else []
            creator = self.tree.parent / "creator"
            try:
                maker = _token_pid(creator.read_text(encoding="utf-8").strip())
            except OSError:
                maker = None              # no record (a tree of an older run): left alone, as before
            orphan = maker is not None and maker != os.getpid() and not _pid_alive(maker)
            wanted = self._created or remove or marker.exists() or orphan
            if not wanted:
                return
            if others:
                marker.touch()     # the last user removes it
                return
            self._remove_tree(self._git_dir, self.tree)
            shutil.rmtree(users, ignore_errors=True)
            marker.unlink(missing_ok=True)
            creator.unlink(missing_ok=True)
            try:
                self.tree.parent.rmdir()   # the <sha> folder, when nothing else is in it
            except OSError:
                pass


#: seconds the collect of one tree may take before the target goes on without a model.
COLLECT_TIMEOUT = 300.0
