"""tools/arena/cli.py — AR-1: `arena <object> <verb>`, global flags, exit codes.

One parser for the whole epic. Objects and their verbs live in one table,
`OBJECTS`, so a later ticket adds a verb by filling in its `Verb` entry — the
`add_arguments` that declares its own arguments and the `handler` that runs it
— and nothing else here moves. In AR-1 no verb has a handler: every one is
listed (so `arena run --help` already shows the shape of the epic) and prints
`arena: <object> <verb> is not implemented yet (AR-N)`, exit 2.

Global flags come before the object: `-p/--profile NAME`, `-o/--output
table|json`, `-y/--yes`. AR-1 parses and keeps `profile` and `yes` on `args`;
no AR-1 action reads them. `output` picks `emit`'s format.

`--` passthrough: `arena run start 136 -- --no-gate --max-parallel 4` hands the
old `run` command's flags through untouched. `main` splits `argv` at the first
standalone `--` before argparse sees it — argparse would swallow a lone `--`
and then try to parse what follows it — and stores the right half verbatim in
`args.passthrough` (`[]` when there is no `--`). `arena` never reads it.

Usage errors are one line, `arena: <message>`, exit 2 — `ArenaParser.error`
replaces argparse's usage block (principle 9).
"""

from __future__ import annotations

import argparse
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import models, output, profile, rounds

# ── exit codes ───────────────────────────────────────────────────────────────
#: The one exit-code table every `arena` command returns from.
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_NOTHING = 3
# `4` exists because the old `run` command exits 2 both when a round ran and no
# agent came back READY and on an argparse error (a mistyped flag after `--`).
# Passing that `2` through would read as an arena refusal, so AR-3 maps a
# child's `2` to `4` only when the round actually ran, and to `1` otherwise.
EXIT_NO_READY = 4

EXIT_CODES = {
    EXIT_OK: "the action succeeded",
    EXIT_FAILED: "the action failed",
    EXIT_USAGE: "usage error, or a refusal before anything was done",
    EXIT_NOTHING: "nothing to do (e.g. no rounds to list)",
    EXIT_NO_READY: "the round ran and ended with no READY agent",
}


# ── the object/verb table ────────────────────────────────────────────────────
@dataclass
class Verb:
    """One `<object> <verb>`: who implements it and how it parses."""

    help: str
    ticket: str
    # Declares the verb's own arguments on its sub-parser. None: AR-1 accepts
    # anything after an unimplemented verb, so the message below is what the
    # operator sees instead of a parse error about arguments nobody defined yet.
    add_arguments: Optional[Callable[[argparse.ArgumentParser], None]] = None
    # Runs the verb; returns an exit code from the table above. None: not yet.
    handler: Optional[Callable[[argparse.Namespace], int]] = None


@dataclass
class Object:
    help: str
    verbs: dict[str, Verb]


# ── AR-2: profile list / view ────────────────────────────────────────────────
#: The checkout `arena` runs from: the directory that holds `tools/`, the same
#: root the launcher puts on `sys.path` — never the caller's cwd. Tests patch it.
REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _load(args: argparse.Namespace) -> tuple[dict[str, dict[str, str]], str]:
    """Profiles and the selected name (`-p` wins over `[arena] profile`).

    Raises `ProfileError` for a bad file or a `-p` naming no profile.
    """
    profiles, active = profile.load_profiles(REPO_ROOT)
    if args.profile is not None:
        if args.profile not in profiles:
            known = ", ".join(sorted(profiles)) or "none"
            raise profile.ProfileError(
                f"unknown profile {args.profile!r} (known: {known})"
            )
        active = args.profile
    return profiles, active


def _models_summary(models: str) -> str:
    """`a,a,b` → `3 agents (2 models)`: the count, not the list."""
    entries = [m.strip() for m in models.split(",") if m.strip()]
    if not entries:
        return ""
    return f"{len(entries)} agents ({len(set(entries))} models)"


def _profile_list(args: argparse.Namespace) -> int:
    try:
        profiles, active = _load(args)
    except profile.ProfileError as err:
        return output.refuse(str(err))
    if not profiles:
        # Nothing to list is exit 3, not a refusal — so not through `refuse`.
        print(
            "arena: no [arena.profile.*] section in contest.ini or contest.local.ini",
            file=sys.stderr,
        )
        return EXIT_NOTHING
    rows = [
        {
            "NAME": name,
            "ACTIVE": "*" if name == active else "",
            "MODELS": _models_summary(p.get("models", "")),
            "LEGS": p.get("legs", ""),
            "BRANCH": p.get("branch", ""),
        }
        for name, p in sorted(profiles.items())
    ]
    output.emit(rows, ["NAME", "ACTIVE", "MODELS", "LEGS", "BRANCH"], args.output)
    return EXIT_OK


def _profile_view_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("name", nargs="?", help="profile name (default: -p, then the active one)")


def _profile_view(args: argparse.Namespace) -> int:
    try:
        profiles, active = _load(args)
        name = args.name or active
        if name not in profiles:
            known = ", ".join(sorted(profiles)) or "none"
            raise profile.ProfileError(f"unknown profile {name!r} (known: {known})")
        chosen = profiles[name]
        flags = shlex.join(profile.profile_flags(chosen))
    except profile.ProfileError as err:
        return output.refuse(str(err))
    # The rows are SETTING/VALUE pairs, so `emit`'s mask-by-key sees `SETTING`,
    # not the profile key: mask each value by its own key here, `emit` still
    # scrubs. The column is not called `KEY`: `key` is a secret word, and `emit`
    # would mask the whole column.
    rows = [
        {"SETTING": k, "VALUE": output.mask({k: v})[k]} for k, v in chosen.items()
    ]
    rows.append({"SETTING": "flags", "VALUE": flags})
    output.emit(rows, ["SETTING", "VALUE"], args.output)
    return EXIT_OK


# ── AR-3: run start / run list ───────────────────────────────────────────────
def _run_start_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("ticket", type=int, help="the ticket's round number NN")
    p.add_argument("--branch", help="integration branch (default: profile's branch, then HEAD)")
    p.add_argument("--fresh-ticket", action="store_true",
                   help="rebuild arena-round/NN when it holds another ticket text or tip")


def _run_start(args: argparse.Namespace) -> int:
    try:
        profiles, active = _load(args)
    except profile.ProfileError as err:
        return output.refuse(str(err))
    # no profile file, or no profile by the active name: no profile flags
    return rounds.run_start(REPO_ROOT, args, profiles.get(active, {}))


def _run_list(args: argparse.Namespace) -> int:
    return rounds.run_list(REPO_ROOT, args)


# ── AR-59: model available / use / drop ──────────────────────────────────────
def _late_globals(p: argparse.ArgumentParser, *names: str) -> None:
    """Accept `-p`, `-y`, `-o` after the verb too: `arena model use a -p p1 -y`.

    `SUPPRESS` keeps the global's value when the flag is not repeated here, so
    `arena -p p1 model use a` and `arena model use a -p p1` mean the same.
    """
    if "p" in names:
        p.add_argument("-p", "--profile", metavar="NAME", default=argparse.SUPPRESS,
                       help="profile to act on (as the global -p)")
    if "y" in names:
        p.add_argument("-y", "--yes", action="store_true", default=argparse.SUPPRESS,
                       help="apply without asking")
    if "o" in names:
        p.add_argument("-o", "--output", choices=("table", "json"), default=argparse.SUPPRESS,
                       help="output format (as the global -o)")


def _model_available_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("providers", nargs="*", metavar="PROVIDER",
                   help="providers to list (default: every provider Kilo knows)")
    p.add_argument("--free", action="store_true", help="free and maybe-free models only")
    p.add_argument("--search", metavar="TEXT", help="case-insensitive substring of provider/name")
    p.add_argument("--url", metavar="URL",
                   help="base URL of the one provider named (else env ARENA_URL_<NAME>)")
    p.add_argument("--test", action="store_true", help="test every listed model")
    _late_globals(p, "p", "o")


def _model_test_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("names", metavar="NAME[,NAME...]",
                   help="model names (provider/model), comma-separated")
    p.add_argument("--url", metavar="URL",
                   help="base URL of the one provider named (else env ARENA_URL_<NAME>)")
    _late_globals(p, "o")


def _model_names_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("names", metavar="NAME[,NAME...]",
                   help="model names, comma-separated like --models")
    _late_globals(p, "p", "y")


def _model_available(args: argparse.Namespace) -> int:
    return models.available(REPO_ROOT, args)


def _model_test(args: argparse.Namespace) -> int:
    return models.test(REPO_ROOT, args)


def _model_use(args: argparse.Namespace) -> int:
    return models.use(REPO_ROOT, args)


def _model_drop(args: argparse.Namespace) -> int:
    return models.drop(REPO_ROOT, args)


OBJECTS: dict[str, Object] = {
    "profile": Object(
        "named run settings ([arena.profile.NAME])",
        {
            "list": Verb("list the profiles", "AR-2", handler=_profile_list),
            "view": Verb(
                "show one profile and its run flag line",
                "AR-2",
                add_arguments=_profile_view_arguments,
                handler=_profile_view,
            ),
        },
    ),
    "model": Object(
        "the model list from Kilo, by name",
        {
            "available": Verb(
                "list Kilo's models, free ones marked",
                "AR-59",
                add_arguments=_model_available_arguments,
                handler=_model_available,
            ),
            "test": Verb(
                "run the 15-check code task on models, keep the score",
                "AR-60",
                add_arguments=_model_test_arguments,
                handler=_model_test,
            ),
            "use": Verb(
                "set a profile's models by name",
                "AR-59",
                add_arguments=_model_names_arguments,
                handler=_model_use,
            ),
            "drop": Verb(
                "remove names from a profile's models",
                "AR-59",
                add_arguments=_model_names_arguments,
                handler=_model_drop,
            ),
        },
    ),
    "issue": Object(
        "tickets: draft, list, view, land",
        {
            "create": Verb("draft a ticket from a brief", "AR-7"),
            "list": Verb("list tickets and their state", "AR-6"),
            "view": Verb("show one ticket", "AR-6"),
            "land": Verb("land a ticket's winning entry", "AR-8"),
        },
    ),
    "run": Object(
        "contest rounds",
        {
            "start": Verb(
                "start the round for a ticket",
                "AR-3",
                add_arguments=_run_start_arguments,
                handler=_run_start,
            ),
            "list": Verb("list rounds", "AR-3", add_arguments=lambda p: None,
                         handler=_run_list),
            "view": Verb("show a round or one leg", "AR-4"),
            "rerun": Verb("rerun failed or named agents", "AR-5"),
        },
    ),
    "entry": Object(
        "one agent's result in a round",
        {
            "merge": Verb("merge an agent's entry", "AR-8"),
        },
    ),
}


# ── the parser ───────────────────────────────────────────────────────────────
class ArenaParser(argparse.ArgumentParser):
    """argparse with one-line usage errors: `arena: <message>`, exit 2."""

    def error(self, message: str) -> None:  # type: ignore[override]
        output.refuse(message)
        raise SystemExit(EXIT_USAGE)


def build_parser() -> ArenaParser:
    """Build the two-level parser from `OBJECTS` as it is right now."""
    parser = ArenaParser(prog="arena", description="One short command for the contest flow.")
    parser.add_argument("-p", "--profile", metavar="NAME", help="run profile to use")
    parser.add_argument(
        "-o", "--output", choices=("table", "json"), default="table", help="output format"
    )
    parser.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")

    objects = parser.add_subparsers(dest="object", metavar="<object>", required=True)
    for obj_name, obj in OBJECTS.items():
        obj_parser = objects.add_parser(obj_name, help=obj.help, description=obj.help)
        verbs = obj_parser.add_subparsers(dest="verb", metavar="<verb>", required=True)
        for verb_name, verb in obj.verbs.items():
            vp = verbs.add_parser(verb_name, help=f"{verb.help} [{verb.ticket}]")
            if verb.add_arguments is not None:
                verb.add_arguments(vp)
            else:
                vp.add_argument("rest", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
            vp.set_defaults(_verb=verb)
    return parser


def split_passthrough(argv: list[str]) -> tuple[list[str], list[str]]:
    """Split *argv* at the first standalone `--`: (arena's part, passthrough)."""
    if "--" in argv:
        i = argv.index("--")
        return argv[:i], argv[i + 1 :]
    return argv, []


def main(argv: Optional[list[str]] = None) -> int:
    """Parse *argv* (default `sys.argv[1:]`), run the verb, return its exit code."""
    argv = list(sys.argv[1:] if argv is None else argv)
    own, passthrough = split_passthrough(argv)
    try:
        args = build_parser().parse_args(own)
    except SystemExit as exc:
        # `--help` exits 0; usage errors already printed their one line.
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    args.passthrough = passthrough
    verb: Verb = args._verb
    if verb.handler is None:
        return output.refuse(
            f"{args.object} {args.verb} is not implemented yet ({verb.ticket})"
        )
    return verb.handler(args)
