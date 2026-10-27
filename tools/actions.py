"""
Orchestrator action handlers — extracted from main.py to keep it lighter.

OrchestratorActions is a mixin; Orchestrator inherits from it and supplies:
  self.model, self.base_url, self.api_key, self.timeout_seconds,
  self.max_iterations, self.stream_agents, self.search_full_file_max_chars,
  self.ssl_context (ssl.SSLContext | None),
  tracer (module-level), logger (module-level).
"""
import os
import re
import json
import shlex
import time
import logging
from typing import List, Tuple

from tools.llm_stream import request_completion, strip_think, ollama_chat_url, strip_json_fence, restore_masked_secrets, unrestored_masked_lines
# HARDEN-7 C: the shared reader of a reply field's type. The validators this
# module drives answer `feedback` themselves, and a list or a number in that
# field raised AttributeError out of `/edit` and the text-QA loop.
from tools.llm_stream import reply_text
from tools.agent_trace import tracer
from tools.file_reader import read_file
from tools.ui import Spinner, stream_tracker
from tools import backoff
from tools.config_safe import safe_getint

logger = logging.getLogger(__name__)


def _ts() -> str:
    """Return the current local time as HH:MM:SS — used to prefix every status line."""
    return time.strftime("%H:%M:%S")


def _resolve_within_base(base_dir: str, file_path: str) -> "str | None":
    """Resolve *file_path* against *base_dir* and refuse anything that
    escapes it.

    SECURITY: /search and /edit used to accept an absolute file_path
    verbatim, or a relative one containing "..", and read (or, for /edit,
    read-and-write-back) whatever that path resolved to — a
    prompt/instruction like "/search :: query in ../../.ssh/id_rsa" (or
    any absolute path) could exfiltrate, and for /edit even overwrite,
    arbitrary files on the host. Returns the resolved absolute path if it
    stays within base_dir, else None.
    """
    # realpath: a symlink inside base_dir that points outside it passed the string test
    base_real = os.path.realpath(base_dir)
    target_real = os.path.realpath(os.path.join(base_dir, file_path))
    try:
        inside = os.path.commonpath([base_real, target_real]) == base_real
    except ValueError:
        return None
    return target_real if inside else None


#: The legacy codecs a non-UTF-8 file may be written back in, most faithful first.
#: `cp1251` leads because it is this repository's own codec, and on a tie it wins.
_LEGACY_WRITE_ENCODINGS: Tuple[str, ...] = ("cp1251", "cp1252")
#: The PEP 263 coding cookie: `# coding: name`, `# coding=name`, `# -*- coding: name -*-`.
_CODING_COOKIE_RE = re.compile(rb"coding[ \t]*[:=][ \t]*([A-Za-z0-9._-]+)")


def _source_coding_cookie(raw: bytes) -> "str | None":
    """The codec a coding cookie in the first two lines of *raw* names, or `None`.

    HARDEN-9 C: a cookie is the author's declaration about this file, so it wins over
    the script guess — but only where Python reads one: a `#` comment inside the first
    two lines. The same words anywhere else are prose. Nothing here can raise, so a
    malformed first line never reaches a run.
    """
    for line in raw.splitlines(True)[:2]:
        if not line.lstrip().startswith(b"#"):
            continue
        match = _CODING_COOKIE_RE.search(line)
        if match:
            name = match.group(1).decode("ascii", "ignore").strip()
            if name:
                return name
    return None


#: The Cyrillic letters, in their two Unicode blocks.
_CYRILLIC_RANGE = (0x0400, 0x052F)
#: An accented run this long, with no plain letter in it, is a Cyrillic word
#: seen through the cp1252 table.
_ACCENTED_RUN_MIN = 2


def _letter_runs(text: str):
    """The maximal runs of `isalpha()` characters of *text*, in order."""
    run: List[str] = []
    for ch in text:
        if ch.isalpha():
            run.append(ch)
        elif run:
            yield run
            run = []
    if run:
        yield run


def _legacy_symbol_score(text: str) -> int:
    """How many of *text*'s non-ASCII characters read as a lone letter of another script
    (HARDEN-14 C4) — the tie-break `_legacy_script_score` leaves open.

    With no letters in it at all the script score is 0 for both cp1251 and cp1252, so
    `cp1251` — this repository's own codec, and last in the tie-break — won every such file.
    That is wrong for a cp1252 file whose only non-ASCII character is `€`: byte `0x80` is
    `Ђ` in cp1251 and `€` in cp1252, and `/edit` wrote the `€` back as `0x88`, which reads
    back as `ˆ`. A character of a script the rest of the text shows no trace of is a
    mis-decode; a symbol or a digit is what a correctly-read file keeps. The score is 0 when
    the text has a letter of its own, since there the script score already decided.
    """
    if not text:
        return 0
    # "A letter of its own" is a letter of the script the text is otherwise in: a
    # non-Cyrillic non-ASCII letter (an accented Latin one, `é` of `Café`). A lone Cyrillic
    # letter in an otherwise-plain-ASCII text is exactly the mis-decode being counted.
    has_own = any(
        not ch.isascii() and ch.isalpha()
        and not (_CYRILLIC_RANGE[0] <= ord(ch) <= _CYRILLIC_RANGE[1])
        for ch in text
    )
    if has_own:
        return 0
    bad = 0
    for ch in text:
        if ch.isascii():
            continue
        code = ord(ch)
        if _CYRILLIC_RANGE[0] <= code <= _CYRILLIC_RANGE[1]:
            bad += 1          # a Cyrillic letter in a text with none of its own
    return bad


def _legacy_script_score(text: str) -> int:
    """How many of *text*'s word runs read as a neighbouring codec — 0 when every
    run reads as one script, which is what a correctly-read file gives.

    cp1251 and cp1252 agree on almost every byte, so a Western file decoded as
    cp1251 round-trips (`text.encode("cp1251") == raw`) and the round-trip test
    cannot tell the codecs apart — it is `Cafй`, not `Café`, and both are "fine"
    (HARDEN-9 C). What does is the *run* of letters each table gives, because the
    two codecs differ only in what they make of a high byte: cp1252's high bytes
    are accented Latin, cp1251's are Cyrillic, so the same bytes land in different
    buckets. A word is one run of letters, and there a letter is Cyrillic, accented
    Latin, or plain ASCII Latin:

    * a Western file read as cp1251 puts a Cyrillic letter *inside* a run of
      plain ones (`GrьЯe цl`, `Cafй`, `fazade`), so a run that holds Cyrillic
      next to a Latin letter is a mis-decode;
    * a Cyrillic file read as cp1252 turns every letter of a word into an
      accented Latin one, so the run is all accents with no plain letter among
      them (`ÐÐÈÂÅÒ` for `Привет`, `ìèð` for `мир`). Two or more, because one
      accent is what a Western word keeps — the `é` of `Café`, the `À` of `À la`
      — and a real word always has a plain letter with it.
    * everything else — a run of one script, or accents among plain letters —
      is what a correctly-read file gives.

    Counting over the whole text cannot do this: cp1251's А-Я / а-я are the same
    bytes as cp1252's À-ß / à-ÿ, so the tables differ only in which bucket a
    letter goes into and a text-level share of the letters does not separate
    them — `Hello мир friend` scored like a Western file either way. A file with
    no letters gives no evidence and scores 0.
    """
    bad = 0
    for run in _letter_runs(text):
        cyr = lat = asc = 0
        for ch in run:
            code = ord(ch)
            if _CYRILLIC_RANGE[0] <= code <= _CYRILLIC_RANGE[1]:
                cyr += 1
            elif ch.isascii():
                asc += 1
            else:
                lat += 1
        if cyr and lat + asc:
            bad += 1                    # Cyrillic inside a Latin run
        elif lat >= _ACCENTED_RUN_MIN and asc == 0:
            bad += 1                    # an all-accent word: a Cyrillic file
    return bad


def _edit_write_encodings(target: str) -> "Tuple[str, ...]":
    """CC-16 (Part C): the encodings `/edit` may write *target* back in, most
    faithful first — decided **once, from the file's own bytes, before the
    model is asked**, so the whole run works from one decision instead of
    re-deriving one at the write (where it is too late to matter).

    Fix 34 chose the encoding at the write, by testing whether the bytes
    decode as UTF-8 and otherwise falling back to `latin-1` — "write back
    what it read". That is right while the model hands back what it was
    shown (the file read as Latin-1, mojibake and all) and wrong the moment
    it *fixes* the text, which is what it is asked to do for a legacy file:
    a revision with real Cyrillic, or `“ ” — … → ✓`, cannot be encoded in
    Latin-1, the write fails, and — because the failure is only discovered
    after every LLM iteration has run — the whole edit is thrown away and
    the user is told a codec could not encode, not that the file is not
    UTF-8.

    So: `utf-8` when the bytes decode as UTF-8 (unchanged, in every
    respect — a UTF-8 BOM is valid UTF-8, so it stays on this path). Otherwise
    the legacy encodings that both decode these bytes and can be written back,
    tried in order — the file's own codec first (`cp1251` then `cp1252`, the
    two this repository's creative pipeline actually holds, and whichever
    round-trips wins), `latin-1` last since it decodes every byte and can
    therefore always be written back to. A UTF-8 file returns exactly
    `("utf-8",)`, so a UTF-8 edit is byte-identical to fix 34's.

    HARDEN-9 C: "whichever round-trips wins" was the whole of the probe, and
    `cp1251` and `cp1252` define almost every byte, so a Western file always
    round-tripped as `cp1251` and `Café` went out to the model as `Cafй`.
    The order is now judged by the script the codec gives back — a codec that
    splits one file's letters between Cyrillic and Latin mis-read it, and
    `cp1251`'s tie wins because it is this repository's own codec. A coding
    cookie in the first two lines (`# -*- coding: latin-1 -*-`) names the
    codec outright and overrides the guess.
    """
    try:
        with open(target, "rb") as fh:
            raw = fh.read()
    except OSError:
        # Unreadable here means unreadable at the write too, and run_edit
        # already refused to read it before this point.
        return ("utf-8",)

    try:
        raw.decode("utf-8")
        return ("utf-8",)
    except UnicodeDecodeError:
        pass

    # HARDEN-9 C: a coding cookie in the first two lines names the codec, and a
    # named codec wins over the script guess below. Fail-open: an unknown name or a
    # cookie that cannot round-trip the file is simply ignored, and the guess stands.
    cookie = _source_coding_cookie(raw)

    ranked: List[Tuple[int, int, str]] = []
    for index, legacy in enumerate(_LEGACY_WRITE_ENCODINGS):
        try:
            text = raw.decode(legacy)
        except (UnicodeDecodeError, LookupError):
            continue
        # The codec has to describe *this* file, not merely accept the bytes:
        # cp1251 and cp1252 both decode every byte, so the test is that
        # encoding the decoded text reproduces the original.
        try:
            faithful = text.encode(legacy) == raw
        except UnicodeEncodeError:
            continue
        if not faithful:
            continue
        # Then it has to *read* it: a codec that splits one file's letters
        # between two scripts is the wrong one for it, whatever it round-trips.
        ranked.append((_legacy_script_score(text), _legacy_symbol_score(text), index, legacy))
    # Primary: the script score (HARDEN-9 C). Secondary (HARDEN-14 C4): with no letters at
    # all the script score is 0 for both codecs and `cp1251` won every such tie, so a
    # cp1252 file whose only non-ASCII character is `€` (byte `0x80`, cp1251's `Ђ`) was read
    # as cp1251 and written back as `0x88`, which reads back as `ˆ`. The lower score wins:
    # a decoding that leaves the text with a printable symbol beats one that reads it as a
    # lone Cyrillic letter. The index stays last, so a genuine tie still falls to `cp1251`.
    ranked.sort(key=lambda item: (item[0], item[1], item[2]))
    candidates: List[str] = [legacy for _, _, _, legacy in ranked]
    candidates.append("latin-1")  # decodes anything, so always writable

    if cookie:
        try:
            if raw.decode(cookie).encode(cookie) == raw:
                # A named codec leads: it is the author's declaration, and it is still
                # behind the list's other candidates, so a write the cookie cannot hold
                # still has somewhere to go.
                candidates = [cookie] + [enc for enc in candidates if enc != cookie]
        except (UnicodeDecodeError, UnicodeEncodeError, LookupError):
            pass
    return tuple(candidates)


def _pick_edit_encoding(encodings: "Tuple[str, ...]", revised: str) -> "Tuple[str | None, str]":
    """CC-16 (Part C): the first of *encodings* that can hold *revised*, or
    `(None, the characters that fit none of them)`.

    The same list is walked in order at every revision, so one file has one
    encoding for the whole run: a first attempt that fitted in cp1252 is
    not followed by a retry that comes back in cp1251. The returned
    characters are the reportable ones — printable, de-duplicated, in the
    order they appear — so the refusal can name what is missing instead of
    quoting a bare codec message, and they are the ones the file's own codec,
    the first candidate, cannot hold.
    """
    for encoding in encodings:
        try:
            revised.encode(encoding)
            return encoding, ""
        except UnicodeEncodeError:
            continue
    # No candidate holds the revision. Name the characters that keep it out, so the
    # message says what to drop rather than quoting a codec or saying "some
    # characters": the file is written back in its own codec, the first candidate,
    # so the characters it cannot hold are the ones a second try has to lose
    # (HARDEN-9 C: a reply that added a genuine Western letter to a legacy file was
    # refused as "missing some characters", with nothing named). A character that fits
    # no codec at all is in this same set, so there is nothing else to look for.
    if not encodings:
        return None, ""
    seen: List[str] = []
    for ch in revised:
        if ch in seen or not ch.isprintable():
            continue
        if _cannot_encode(ch, encodings[0]):
            seen.append(ch)
    return None, " ".join(seen[:12])


def _cannot_encode(ch: str, encoding: str) -> bool:
    """True when *ch* has no representation in *encoding*."""
    try:
        ch.encode(encoding)
        return False
    except UnicodeEncodeError:
        return True


#: The byte-order marks of the wide encodings `/edit` refuses by name (HARDEN-14 C3). A UTF-16
#: or UTF-32 file read as latin-1 is a wall of NULs the model cannot work with, and the edit
#: used to print `status=approved` with a backup anyway.
_WIDE_BOMS = (
    (b"\x00\x00\xfe\xff", "UTF-32 BE"),
    (b"\xff\xfe\x00\x00", "UTF-32 LE"),
    (b"\xfe\xff", "UTF-16 BE"),
    (b"\xff\xfe", "UTF-16 LE"),
)


def _wide_bom_name(raw: bytes) -> "str | None":
    """The name of the wide encoding *raw* starts with, or ``None``.

    HARDEN-14 C3: the UTF-32 BOMs are tested before the UTF-16 ones, because a UTF-32 LE BOM
    begins with the UTF-16 LE one. Fail-open by construction: an unreadable or empty file
    has no BOM here and is read normally.
    """
    for bom, name in _WIDE_BOMS:
        if raw.startswith(bom):
            return name
    return None


def _text_encoding_of(raw: bytes, encodings: "Tuple[str, ...]") -> str:
    """The first codec of *encodings* that decodes *raw* — the codec `_read_edit_target` chose.

    HARDEN-20 B3: the line-ending match must read the same bytes the same way, and the caller
    otherwise has no way to ask which codec that was. Fail-open: a codec name Python does not
    know, or a list that none of it matches, yields the UTF-8 fallback `_read_edit_target`
    falls back to, so nothing here can raise into a run.
    """
    for encoding in encodings:
        try:
            raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        return encoding
    return "utf-8"


def _decode_lenient(raw: bytes, encoding: str) -> str:
    """*raw* in *encoding*, falling back to the lossy UTF-8 read when it no longer describes
    the bytes (a file that changed between the read and the write, or a codec name Python
    does not know). The same two-step read `_read_edit_target` makes."""
    try:
        return raw.decode(encoding)
    except (UnicodeDecodeError, LookupError):
        return raw.decode("utf-8", errors="replace")


def _read_edit_target(target: str, encodings: "Tuple[str, ...]") -> str:
    """Read *target* in the codec its own bytes say, for `/edit` to show the model (HARDEN-14 C1).

    `run_edit` used to read the file with `tools.file_reader.read_file` — UTF-8, else
    `latin-1` — while the write codec came from `_edit_write_encodings`, which puts `cp1251`
    first. A cp1251 file was therefore *shown* to the model as `Ïðèâåò ìèð` and then written
    back as cp1251, so an instruction in Russian about a Russian file could not be done: the
    text the model was handed is not the text it was asked about. The Coder reads such a file
    in its own codec (`_file_read_encoding`, HARDEN-9 C); this is the same one decision, made
    once and taken from the same list the write walks.

    Fail-open, as everywhere in this part: an undecodable file, an unreadable file or a codec
    that no longer describes the bytes falls back to the lossy read, so `/edit` still has
    something to work with rather than an exception into the run.
    """
    try:
        with open(target, "rb") as fh:
            raw = fh.read()
    except OSError:
        return ""
    if not raw:
        return ""
    return _decode_lenient(raw, _text_encoding_of(raw, encodings))


def _edit_line_ends(dest: str, content: str, encoding: "str | None" = None) -> str:
    """*content* with *dest*'s line endings, decided per line (HARDEN-14 C2, HARDEN-20 B).

    `in_style_of` (tools.auto.utils) takes CRLF for the whole file when more than half of its
    `\\n` follow a `\\r`, so `a\\r\\nb\\r\\nc\\nd\\ne\\n` edited on `c` came out as
    `a\\nb\\nC\\nd\\ne\\n` — the untouched `a` and `b` lost the `\\r` they were written with.
    A line of the reply that equals a line of the original (after the line ending) keeps *that
    line's own* ending, used once; a changed or a new line takes the file's majority style, so
    an all-CRLF and an all-LF file are unchanged and only a genuinely mixed one is spared the
    flattening. The BOM is put back at the end, so this also supplies the BOM.

    This is the one shared rule: `/edit` and the Coder's `in_style_of` both call it, so the two
    paths cannot drift apart again.

    HARDEN-20 B2: an unterminated original last line has an **empty** ending, and only the
    reply's own last line may take it. Handing it to a middle line glues two lines: the
    original `alpha\\nbeta` edited to `beta\\nalpha\\nbeta\\n` would be written as
    `betaalpha\\nbeta\\n`, and a source file whose last line an edit repeats (`return x`,
    `}`, `pass`, a blank line) becomes invalid.

    HARDEN-20 B3: the original is decoded in *encoding*, the codec `_read_edit_target` chose
    for this file, or the same codec re-derived from the file's own bytes when the caller did
    not say. A cp1251 file decoded as UTF-8 is U+FFFD text that never equals the reply's
    correctly decoded line, so every original line looked changed and the file came out flat
    LF. A leading BOM is stripped from the first original body before the match — otherwise it
    stays in that body and `a = 1` never equals `\\ufeffa = 1`, and the first line loses its
    ending. Fail-open: an unreadable file comes back as *content* untouched.
    """
    try:
        with open(dest, "rb") as fh:
            raw = fh.read()
    except OSError:
        return content
    if not raw:
        return content.replace("\r\n", "\n")      # an empty file has no style: LF, as `in_style_of` always said
    if encoding is None:
        try:
            encoding = _text_encoding_of(raw, _edit_write_encodings(dest))
        except Exception:
            encoding = "utf-8"
    majority_crlf = raw.count(b"\r\n") * 2 > raw.count(b"\n")
    changed_ending = "\r\n" if majority_crlf else "\n"
    original_lines = _decode_lenient(raw, encoding).splitlines(keepends=True)
    # HARDEN-20 B3: a BOM is not part of the first line's text, and the match is on the text.
    if original_lines and original_lines[0].startswith("\ufeff"):
        original_lines[0] = original_lines[0].removeprefix("\ufeff")
    by_text: dict = {}
    for line in original_lines:
        body = line.rstrip("\r\n")
        by_text.setdefault(body, []).append(line[len(body):])
    # The reply may arrive with either ending (a faithful model echoes what it was shown), so
    # it is normalised to LF first: a `\r` left in the body would be doubled when the line's
    # own ending is put back.
    parts = content.replace("\r\n", "\n").split("\n")
    out: list = []
    last = len(parts) - 1
    for pos, body in enumerate(parts):
        if pos == last and body == "":
            out.append("")                    # the empty string after a trailing newline
            continue
        # HARDEN-20 B2: an empty ending (the original's unterminated last line) may only go to
        # the reply's last line, or `beta` + `alpha` is written as `betaalpha`.
        endings = by_text.get(body)
        usable = [e for e in (endings or []) if e or pos == last]
        if usable:
            endings.remove(usable[0])
            out.append(body + usable[0])       # the line's own ending, used once
        elif pos == last:
            out.append(body)                   # a last line with no ending of its own
        else:
            out.append(body + changed_ending)  # a changed or a new line: the majority
    text = "".join(out)
    # The BOM is this function's own half: the model writes no BOM, so a BOM the original had
    # is put back rather than dropped on the floor.
    if raw.startswith(b"\xef\xbb\xbf") and not text.startswith("\ufeff"):
        text = "\ufeff" + text
    return text


def _remove_edit_backup(path: str) -> None:
    """CC-16 (Part C): remove the `.bak` a failed `/edit` write made, so no backup of
    an edit that did not happen is left behind.

    Fail-open throughout — nothing here raises into a run. The interesting case is
    the one where the *unlink* fails too: a write that failed because the directory
    is not writable is blocked by the same permission from cleaning up after itself,
    which is exactly when the stray backup matters most. The backup is this run's
    own artifact and its name is known, so the directory's write bit is put back for
    the single unlink and the original mode restored immediately afterwards.
    """
    try:
        os.unlink(path)
        return
    except OSError:
        pass

    parent = os.path.dirname(path) or "."
    try:
        mode = os.stat(parent).st_mode & 0o7777
    except OSError:
        return
    if mode & 0o200:
        return  # writable, and the unlink still failed: nothing to gain here
    try:
        os.chmod(parent, mode | 0o700)
        try:
            os.unlink(path)
        finally:
            try:
                os.chmod(parent, mode)
            except OSError:
                pass
    except OSError as exc:
        logger.warning("could not remove the backup of a failed edit %s: %s", path, exc)


class _SearchChunkFailed(Exception):
    """Internal: _ask_over_text's None failure sentinel, as an exception —
    the shape backoff.retry_with_backoff retries on."""


class OrchestratorActions:
    """Mixin that adds run_search, run_text_qa, and run_edit
    to the Orchestrator without cluttering its core orchestration logic."""

    def _cfg_temp(self, section: str, default: float) -> float:
        """Read a temperature from agents.ini [section], falling back to the
        historical literal if config is unavailable or the key is absent.
        Keeps behavior identical unless the operator overrides it."""
        cfg = getattr(self, "config", None)
        if cfg is None:
            return default
        try:
            return cfg.getfloat(section, "temperature", fallback=default)
        except Exception:
            return default

    def _chat_url(self) -> str:
        """Return the correct chat completions URL for the active api_format."""
        base = self.base_url.rstrip("/")
        if getattr(self, "api_format", "openai") == "ollama":
            return ollama_chat_url(base)
        return f"{base}/chat/completions"

    def _headers(self) -> dict:
        return {"Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}"}

    # Full-file search (/search)

    @staticmethod
    def _unquote_path(value: str) -> str:
        """The path a user typed, without the shell quotes they wrapped it in.

        HARDEN-18 C: ``/edit "my notes.txt" :: …`` looked up
        ``dir/"my notes.txt"`` — the quotes stayed in the name, and the error
        named a file that does not exist. A matching pair of single or double
        quotes around the whole path is the quoting of a name the user copied
        from a listing; an unquoted path is unchanged, and a name that carries a
        quote inside it (``it's a note.txt``, ``"my"file.txt``) is left alone,
        since no pair surrounds it (`"a"b"` has the same quote inside: it is not a
        quoted name, so it is not unwrapped).
        """
        if (len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"')
                and value[0] not in value[1:-1]):
            return value[1:-1]
        return value

    @staticmethod
    def _parse_payload_and_file(user_input: str, command: str):
        """
        Parse '/<command> <payload> in <file>' (also accepts '<file> :: <payload>').
        Returns (payload, file_path) or (None, None) if it can't be parsed.
        Shared by /search (payload = query) and /edit (payload = instruction).
        """
        body = user_input.strip()[len(command):].strip()
        if not body:
            return None, None
        # The separator is " :: " with spaces; a bare "::" would split identifiers like std::vector.
        if " :: " in body:                     # /<command> <file> :: <payload>
            file_path, payload = body.split(" :: ", 1)
            return payload.strip(), OrchestratorActions._unquote_path(file_path.strip())
        if " in " in body:                     # /<command> <payload> in <file>
            payload, file_path = body.rsplit(" in ", 1)
            return payload.strip(), OrchestratorActions._unquote_path(file_path.strip())
        # Fallback: first token is the file, the rest is the payload.
        parts = body.split(None, 1)
        if len(parts) == 2:
            return parts[1].strip(), OrchestratorActions._unquote_path(parts[0].strip())
        return None, None

    @staticmethod
    def _parse_search_command(user_input: str):
        """Parse '/search <query> in <file>'  (also accepts '<file> :: <query>')."""
        return OrchestratorActions._parse_payload_and_file(user_input, "/search")

    def _ask_over_text(self, query: str, file_label: str, text: str,
                       chunk_label: str = None, generative: bool = False) -> "str | None":
        """
        Send the whole `text` plus the question to the model and stream the
        answer. Returns the assistant's full reply (stripped).

        generative=False (default, used by /search and show intents): retrieval-only
          mode — the model is told to answer strictly from the provided content and
          reply 'NONE' when the answer is absent (lets chunk mode decide to continue).

        generative=True (used by improve/fix/optimize/explain on non-code files):
          the model is allowed — and encouraged — to suggest new or rewritten content.
          The 'NONE' sentinel is not used here because there is always something to say.
        """
        where = f" (chunk {chunk_label})" if chunk_label else ""
        if generative:
            system = (
                "You are an expert writing and content assistant. "
                "The user will show you a file and ask you to improve, fix, explain, or "
                "optimise it. Use the file content as the primary context, but you are "
                "free — and expected — to suggest rewritten passages, new structure, or "
                "concrete fixes. Be specific and actionable."
            )
        else:
            system = (
                "You are a retrieval assistant. Answer the user's question using ONLY "
                "the file content provided. Quote the relevant question/answer text. "
                "If the answer is not present in this content, reply with exactly: NONE"
            )
        user = f"FILE: {file_label}{where}\n-----\n{text}\n-----\nQUESTION: {query}"
        url = self._chat_url()
        headers = self._headers()
        payload = {"model": self.model,
                   "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": user}],
                   "temperature": self._cfg_temp("search_agent", 0.2),
                   "num_ctx": getattr(self, "num_ctx", 0)}
        print(f"\n[{_ts()}] 🔎 search → model{where}:")
        tracer.event("orchestrator", "search_fullfile", "llm_request",
                     params={"file": file_label, "chunk": chunk_label, "query": query},
                     content=user, model=self.model, temperature=self._cfg_temp("search_agent", 0.2))
        try:
            _on_tok, _tok_stats = stream_tracker()
            answer = request_completion(
                url, headers, payload, self.timeout_seconds,
                stream=True,
                api_format=self.api_format,
                on_token=_on_tok,
                ssl_context=self.ssl_context,
            )
            print()
            if _s := _tok_stats():
                print(f"[{_ts()}] {_s}")
            tracer.event("search_fullfile", "orchestrator", "llm_response", content=answer)
            return strip_think(answer).strip()
        except Exception as e:
            # A transient error is None, not "", so run_search retries the chunk instead of treating it as no answer.
            logger.error(f"/search model call failed: {e}")
            print(f"[{_ts()}] search failed: {e}")
            return None

    def _ask_over_text_retryable(self, query: str, file_path: str,
                                 text: str, chunk_label=None):
        """_ask_over_text with its None failure sentinel translated into a
        private exception, so run_search can drive it through
        backoff.retry_with_backoff (which retries on exceptions, not on
        falsy returns — a falsy return is a SUCCESS there)."""
        ans = self._ask_over_text(query, file_path, text, chunk_label=chunk_label)
        if ans is None:
            raise _SearchChunkFailed()
        return ans

    @staticmethod
    def _split_text(text: str, budget: int, overlap: int = 200):
        """Split text into <=budget-char chunks on line boundaries, with overlap."""
        overlap = min(overlap, max(0, budget // 4))   # keep overlap < budget
        lines = text.splitlines(keepends=True)
        chunks, cur = [], ""
        for ln in lines:
            if len(cur) + len(ln) > budget and cur:
                chunks.append(cur)
                cur = cur[-overlap:] + ln if overlap else ln
            else:
                cur += ln
        if cur:
            chunks.append(cur)
        return chunks

    def run_search(self, user_input: str, base_dir: str) -> None:
        """
        /search — answer a question against the WHOLE file, no AST extraction.

        If the file fits within [search] full_file_max_chars, the entire file is
        sent in one shot. If it is larger, the file is split into chunks (the
        'agent/validator may split if required' path) and each chunk is queried
        in turn until one answers.

        Each chunk's candidate answer is passed through _validate_text_answer
        before being accepted — the model is told to reply "NONE" when a chunk
        has no answer, but a weakly relevant or incomplete answer would still
        clear that gate on its own, so the same grounding check run_text_qa
        uses is applied here too. A chunk whose answer fails validation
        (needs_fix) is treated like a non-answer and the scan continues to the
        next chunk. If the validator itself is unreachable (_api_error), the
        candidate answer is accepted as-is rather than blocking the search.
        """
        query, file_path = self._parse_search_command(user_input)
        if not query or not file_path:
            print("Usage: /search <query> in <file>   (or: /search <file> :: <query>)")
            return

        target = _resolve_within_base(base_dir, file_path)
        if target is None:
            print(f"[{_ts()}] Error: '{file_path}' is outside the project directory.")
            return
        tracer.start_run(f"/search {query} in {file_path}")
        try:
            source = read_file(target)
        except Exception as e:
            print(f"[{_ts()}] Could not read {target}: {e}")
            return

        budget = self.search_full_file_max_chars
        if len(source) <= budget:
            print(f"[{_ts()}] Full-file search over {file_path} ({len(source)} chars).")
            # The single-file path retries transient errors (None) the same way the chunked path does.
            _RETRY = 3

            def _ask_full_file():
                return self._ask_over_text_retryable(query, file_path, source)

            try:
                ans = backoff.retry_with_backoff(
                    _ask_full_file, attempts=_RETRY,
                    sleep_fn=time.sleep,
                    on_retry=lambda exc, n, wait: print(
                        f"[{_ts()}] ⚠️  Retrying after error "
                        f"(attempt {n + 1}/{_RETRY})..."),
                )
            except Exception:
                ans = None
            if ans is None:
                print(f"[{_ts()}] ⚠️  Search unreachable after {_RETRY} attempts.")
                return

            # NONE is no answer, and the single-chunk path gets the same grounding check as the chunked one.
            if not ans or ans.strip().upper() == "NONE":
                print(f"[{_ts()}] No answer found in {file_path} for: {query}")
                return

            print(f"[{_ts()}] 🤖 Validating candidate answer...")
            with Spinner("Search validator"):
                validation = self._validate_text_answer(query, source, ans,
                                                         stream_mode=self.stream_agents)

            if validation.get("_api_error") or validation.get("_unparseable"):
                print(f"[{_ts()}] ⚠️  Validator unavailable — accepting answer as-is. "
                      f"({reply_text(validation.get('feedback'))})")
                print(f"[{_ts()}] ✅ Answer found in {file_path} (unvalidated).")
                return

            if validation.get("status") == "approved":
                print(f"[{_ts()}] ✅ Answer found in {file_path} "
                      f"(validated, grounded={validation.get('grounded')}).")
                return

            feedback = reply_text(validation.get("feedback"))
            if feedback:
                print(f"[{_ts()}] ❗ Answer rejected: {feedback}")
            print(f"[{_ts()}] No chunk contained a validated answer to: {query}")
            return

        # File too large for one context → allowed to split.
        chunks = self._split_text(source, budget)
        print(f"[{_ts()}] {file_path} is {len(source)} chars > budget {budget}; "
              f"splitting into {len(chunks)} chunks and searching each.")
        for i, ch in enumerate(chunks, 1):
            # A transient error (None) on a chunk is retried a bounded number of times, not read as no answer.
            _CHUNK_RETRY_ATTEMPTS = 3

            def _ask_this_chunk(_ch=ch, _i=i, _n=len(chunks)):
                return self._ask_over_text_retryable(
                    query, file_path, _ch, chunk_label=f"{_i}/{_n}")

            try:
                ans = backoff.retry_with_backoff(
                    _ask_this_chunk, attempts=_CHUNK_RETRY_ATTEMPTS,
                    sleep_fn=time.sleep,
                    on_retry=lambda exc, n, wait, _i=i, _n=len(chunks): print(
                        f"[{_ts()}] ⚠️  Retrying chunk {_i}/{_n} "
                        f"after error (attempt {n + 1}/{_CHUNK_RETRY_ATTEMPTS})..."),
                )
            except Exception:
                ans = None
            if ans is None:
                print(f"[{_ts()}] ⚠️  Chunk {i}/{len(chunks)} unreachable after "
                      f"{_CHUNK_RETRY_ATTEMPTS} attempts — skipping.")
                continue
            if not ans or ans.strip().upper() == "NONE":
                continue

            print(f"[{_ts()}] 🤖 Validating candidate answer from chunk {i}/{len(chunks)}...")
            with Spinner(f"Search validator {i}/{len(chunks)}"):
                validation = self._validate_text_answer(query, ch, ans,
                                                         stream_mode=self.stream_agents)

            if validation.get("_api_error") or validation.get("_unparseable"):
                print(f"[{_ts()}] ⚠️  Validator unavailable — accepting answer as-is. "
                      f"({reply_text(validation.get('feedback'))})")
                print(f"[{_ts()}] ✅ Answer found in chunk {i}/{len(chunks)} (unvalidated).")
                return

            if validation.get("status") == "approved":
                print(f"[{_ts()}] ✅ Answer found in chunk {i}/{len(chunks)} "
                      f"(validated, grounded={validation.get('grounded')}).")
                return

            feedback = reply_text(validation.get("feedback"))
            if feedback:
                print(f"[{_ts()}] ❗ Chunk {i}/{len(chunks)} answer rejected: {feedback}")

        print(f"[{_ts()}] No chunk contained a validated answer to: {query}")


    # Validated text Q&A (FAQ and documentation files)

    _GENERIC_VERBS = {"show", "view", "get", "answer", "ask", "explain",
                      "describe", "find", "tell", "please"}

    @classmethod
    def _extract_question(cls, user_input: str, file_path: str) -> str:
        """
        Derive the question from the user's request without butchering sentences.

        Strategy: remove the actual file path token (and an immediately preceding
        connector like 'in'/'from'/'about'/'of'), then strip a single leading
        command verb. Phrases that merely contain the word 'in' are preserved.
        'answer how do I reset in faq.md' -> 'how do I reset'
        'explain hello.txt do it from your mind' -> 'do it from your mind'
        'hello.txt' -> ''  (caller treats the FILE itself as the question)
        """
        q = user_input.strip()
        base = os.path.basename(file_path)
        for token in (file_path, base):
            if token and token in q:
                q = re.sub(r"\b(in|from|about|of|on)\s+" + re.escape(token), " ", q)
                q = q.replace(token, " ")
        tokens = q.split()
        while tokens and tokens[0].lower().strip(".,:;-") in cls._GENERIC_VERBS:
            tokens.pop(0)
        return " ".join(tokens).strip(" .,:;?-")

    def _answer_from_file(self, question: str, file_label: str,
                          knowledge: str, feedback: str = None) -> "str | None":
        """Generate a grounded answer to `question` using `knowledge` as the source."""
        system = (
            "You are a documentation/FAQ assistant. Answer the QUESTION using the "
            "DOCUMENT as the primary source of truth. If the document contains the "
            "answer, give it and cite the relevant lines. If the document does not "
            "contain the answer, say so explicitly, then answer from general knowledge "
            "and label that part as not taken from the document."
        )
        fb = ("\n\nA previous answer was rejected by the validator. "
              f"Address this feedback and try again:\n{feedback}") if feedback else ""
        user = f"DOCUMENT: {file_label}\n-----\n{knowledge}\n-----\nQUESTION: {question}{fb}"
        url = self._chat_url()
        headers = self._headers()
        payload = {"model": self.model,
                   "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": user}],
                   "temperature": self._cfg_temp("main_agent", 0.3),
                   "num_ctx": getattr(self, "num_ctx", 0)}
        print(f"\n[{_ts()}] 💬 answer → model:")
        tracer.event("orchestrator", "text_answerer", "llm_request",
                     params={"file": file_label, "question": question,
                             "retry_feedback": feedback},
                     content=user, model=self.model, temperature=self._cfg_temp("main_agent", 0.3))
        try:
            _on_tok, _tok_stats = stream_tracker()
            ans = request_completion(
                url, headers, payload, self.timeout_seconds,
                stream=True,
                api_format=self.api_format,
                on_token=_on_tok,
                ssl_context=self.ssl_context,
            )
            print()
            if _s := _tok_stats():
                print(f"[{_ts()}] {_s}")
            tracer.event("text_answerer", "orchestrator", "llm_response", content=ans)
            return strip_think(ans).strip()
        except Exception as e:
            # A transient error here is None, so the run retries the generation instead of validating an empty answer.
            logger.error(f"text answer call failed: {e}")
            print(f"[{_ts()}] answer generation failed: {e}")
            return None

    def _parse_llm_json(self, content: str) -> dict:
        """
        Parse a validator's raw LLM response into a dict.

        Strips ```json / ``` fences (if present) and JSON-decodes the
        result. The model sometimes returns valid JSON that is NOT an
        object — a list ([{...}]), a bare string, or null. A single-element
        list wrapping an object is unwrapped transparently; anything else
        that isn't a dict raises ValueError so the caller can fail closed
        with its own validator-specific feedback message (callers differ in
        which extra keys, e.g. "grounded", their fail-closed dict carries).
        """
        content = strip_json_fence(content)
        data = json.loads(content)
        if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict):
            data = data[0]
        if not isinstance(data, dict):
            raise ValueError(f"validator returned {type(data).__name__}, expected object")
        return data

    def _validate_text_answer(self, question: str, knowledge: str, answer: str,
                               stream_mode: bool = False) -> dict:
        """
        Validate a proposed answer against the document. Returns
        {status: approved|needs_fix, grounded: bool, feedback: str}.
        Fail-closed (needs_fix + _api_error) on any LLM/parse error.
        stream_mode=True uses streaming to avoid Ollama blocking hangs (tokens
        are accumulated silently; caller always shows a Spinner for feedback).
        """
        system = (
            "You are a strict QA validator. Given a DOCUMENT, a QUESTION and a PROPOSED "
            "ANSWER, decide whether the answer correctly and completely addresses the "
            "question and, where the document is relevant, is consistent with it. "
            "Return STRICT JSON only, no text around it:\n"
            '{"status": "approved" or "needs_fix", "grounded": true or false, '
            '"feedback": "what is wrong or missing, empty if approved"}'
        )
        user = (f"DOCUMENT:\n{knowledge}\n\nQUESTION:\n{question}\n\n"
                f"PROPOSED ANSWER:\n{answer}")
        url = self._chat_url()
        headers = self._headers()
        payload = {"model": self.model,
                   "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": user}],
                   "temperature": self._cfg_temp("validator_agent", 0.1),
                   "num_ctx": getattr(self, "num_ctx", 0)}
        tracer.event("orchestrator", "text_validator", "llm_request",
                     params={"question": question}, content=user,
                     model=self.model, temperature=self._cfg_temp("validator_agent", 0.1))
        # A network failure is an API error; a malformed reply is _unparseable and gets normal feedback, not a backoff retry.
        try:
            content = request_completion(url, headers, payload, self.timeout_seconds,
                                         stream=stream_mode,
                                         api_format=self.api_format,
                                         ssl_context=self.ssl_context)
        except Exception as e:
            logger.error(f"text validator API call failed: {e}")
            return {"status": "needs_fix", "grounded": False,
                    "feedback": f"validator unavailable: {e}", "_api_error": True}

        tracer.event("text_validator", "orchestrator", "llm_response", content=content)
        content = strip_think(content)
        try:
            return self._parse_llm_json(content)
        except Exception as e:
            logger.warning(f"text validator returned unparseable content: {e}")
            # A parse failure is _unparseable, so run_search's fail-open rule still applies to it.
            return {
                "status": "needs_fix",
                "grounded": False,
                "feedback": (
                    "Your reply could not be parsed as the required STRICT "
                    'JSON {"status": ..., "grounded": ..., "feedback": ...} '
                    "— reply with JSON only, no prose, no code fences."
                ),
                "_unparseable": True,
            }

    def run_text_qa(self, question: str, file_path: str, source: str, base_dir: str,
                     resume_state: dict = None) -> None:
        """
        Validated question-answering over a text/FAQ/doc file:
          1. generate an answer grounded in the file,
          2. validate it against the file,
          3. retry with feedback up to max_iterations,
          4. render the final, validated answer (Issue 7: exponential backoff on
             API errors, with checkpoint save on KeyboardInterrupt).
        """
        start = time.time()
        tracer.start_run(f"answer: {question or '[file-as-question]'} in {file_path}")

        # If no explicit question was given, the file content IS the question.
        if not question.strip():
            question = source.strip()
            print(f"[{_ts()}] \U0001f4c4 No explicit question — treating the file content as the question.")
        knowledge = source

        # Over-budget documents keep the head and tail, with an elision marker, instead of being passed whole.
        if len(knowledge) > self.search_full_file_max_chars:
            _budget = self.search_full_file_max_chars
            _half = max(1, _budget // 2)
            _omitted = len(knowledge) - 2 * _half
            knowledge = (
                knowledge[:_half]
                + f"\n\n… [{_omitted} chars omitted to fit the context budget] …\n\n"
                + knowledge[-_half:]
            )
            print(f"[{_ts()}] Document {len(source)} chars > budget "
                  f"{_budget}; using head+tail slice ({_omitted} chars omitted).")

        iteration = 1
        feedback = None
        validation: dict = {}
        answer = ""
        _api_err_count = 0  # consecutive API errors; reset on any non-error response

        # ── Restore checkpoint (Issue 7) ──────────────────────────────────
        if resume_state and resume_state.get("loop") == "run_text_qa":
            iteration = resume_state.get("iteration", 1)
            feedback  = resume_state.get("feedback")
            answer    = resume_state.get("answer", "")
            print(f"[{_ts()}] ▶  Resuming run_text_qa from iteration {iteration} "
                  "(checkpoint restored).")

        while iteration <= self.max_iterations:
            if time.time() - start >= self.timeout_seconds:
                print(f"[{_ts()}] ⏳ timeout reached; returning best answer so far.")
                break
            _step_start = time.time()
            _new_answer = self._answer_from_file(question, file_path, knowledge, feedback)

            if _new_answer is None:
                # Retry a transient generation failure, keeping the previous best answer.
                _api_err_count += 1
                if iteration >= self.max_iterations:
                    print(f"[{_ts()}] ⚠️  Answer generation unavailable — "
                          f"max iterations reached, keeping best answer so far.")
                    break
                _chk = {
                    "loop": "run_text_qa",
                    "question": question,
                    "file_path": file_path,
                    "base_dir": base_dir,
                    "iteration": iteration,  # retry same iteration
                    "feedback": feedback,
                    "answer": answer,
                }
                backoff.api_error_pause(_api_err_count, _chk)
                continue  # retry same iteration (do not increment)

            answer = _new_answer
            print(f"[{_ts()}] ⏱  Answer generated in {time.time() - _step_start:.1f}s")
            print(f"[{_ts()}] \U0001f916 Validating answer ({iteration}/{self.max_iterations})...")
            _val_start = time.time()
            with Spinner(f"Validator iter {iteration}/{self.max_iterations}"):
                validation = self._validate_text_answer(question, knowledge, answer,
                                                        stream_mode=self.stream_agents)
            _val_elapsed = time.time() - _val_start

            status = validation.get("status")
            if status == "approved":
                _api_err_count = 0
                print(f"[{_ts()}] ✅ Answer approved by validator "
                      f"(grounded={validation.get('grounded')}, {_val_elapsed:.1f}s).")
                break

            if validation.get("_api_error"):
                # Back off on an API error; keep the current best answer and do not feed the error back as feedback.
                _api_err_count += 1
                if iteration >= self.max_iterations:
                    print(f"[{_ts()}] ⚠️  Validator unavailable — "
                          f"max iterations reached, keeping best answer. "
                          f"({reply_text(validation.get('feedback'))}, {_val_elapsed:.1f}s)")
                    break
                _chk = {
                    "loop": "run_text_qa",
                    "question": question,
                    "file_path": file_path,
                    "base_dir": base_dir,
                    "iteration": iteration,  # retry same iteration
                    "feedback": feedback,
                    "answer": answer,
                }
                backoff.api_error_pause(_api_err_count, _chk)
                continue  # retry same iteration (do not increment)

            _api_err_count = 0
            feedback = reply_text(validation.get("feedback"))
            if feedback:
                print(f"[{_ts()}] ❗ Validator feedback: {feedback}")
            iteration += 1

        # A concluding block prints the final validated answer, and a warning if the loop exhausted without approval.
        total = time.time() - start
        print(f"\n[{_ts()}] ── ANSWER — {file_path} — {total:.1f}s from request ──")
        if validation.get("status") != "approved":
            print(f"[{_ts()}] ⚠️  UNVALIDATED ANSWER — validator never approved "
                  f"after {self.max_iterations} iteration(s). Last feedback: "
                  f"{reply_text(validation.get('feedback'))!r}. Review carefully.")
        print(answer.strip() if answer and answer.strip() else "(no answer produced)")
        print(f"\n[{_ts()}] status={validation.get('status', 'unknown')}  "
              f"grounded={validation.get('grounded')}")
    # In-place file editing (/edit): writes the file, with backup and diff

    @staticmethod
    def _parse_edit_command(user_input: str):
        """Parse '/edit <instruction> in <file>' (or '/edit <file> :: <instruction>')."""
        return OrchestratorActions._parse_payload_and_file(user_input, "/edit")

    @staticmethod
    def _strip_code_fence(text: str) -> str:
        """If the model wrapped the whole file in a ``` fence, return the inside.

        BUGFIX: stripping fired on a leading "```" with no matching closer,
        deleting the real first line of e.g. a markdown file that opens with
        a fenced snippet. Requires a genuine whole-body wrap now.
        """
        t = text.strip()
        if t.startswith("```"):
            if "\n" not in t:
                t = ""
            else:
                rest = t.split("\n", 1)[1]
                if rest.rstrip().endswith("```"):
                    t = rest.rstrip()[:-3]
        return t.rstrip("\n") + "\n"

    def _edit_file_content(self, instruction: str, file_label: str,
                           source: str, feedback: str = None,
                           previous_revised: str = None) -> "str | None":
        """Ask the model for the COMPLETE revised file content (no commentary).

        Message structure depends on whether this is a first attempt or a retry:

        First attempt (feedback=None):
            user: DOCUMENT + INSTRUCTION

        Retry with previous attempt (feedback + previous_revised):
            user:      DOCUMENT + INSTRUCTION      ← never changes
            assistant: <previous_revised>           ← model's own prior output
            user:      correction instruction only  ← unambiguously meta, not content

        Retry without previous attempt (reset iter / feature disabled):
            user: DOCUMENT + INSTRUCTION + clearly-delimited CORRECTION block

        The multi-turn structure for the second case is critical: when feedback is
        appended to the same user message as the instruction, the model can confuse
        the feedback text for document content and start editing the feedback instead
        of the original document. Putting feedback in a separate user turn after an
        assistant turn removes that ambiguity entirely.
        """
        system = (
            "You are a precise text/file editor. Apply the INSTRUCTION to the DOCUMENT. "
            "Output ONLY the complete, updated file content — no explanations, no "
            "commentary, no markdown code fences. Preserve everything that should not "
            "change; fix what the instruction asks; if asked to add a question or note, "
            "append it as plain text in the file."
        )

        base_user = (
            f"DOCUMENT ({file_label}):\n-----\n{source}\n-----\n"
            f"INSTRUCTION: {instruction}"
        )

        if not feedback:
            # ── First attempt: single turn ─────────────────────────────────
            messages = [
                {"role": "system",    "content": system},
                {"role": "user",      "content": base_user},
            ]
            trace_content = base_user

        elif previous_revised:
            # Retry with context: the previous output is an assistant turn, and the document and instruction are restated.
            correction = (
                f"That edit was rejected.\n\n"
                f"Error: {feedback}\n\n"
                f"Re-apply the INSTRUCTION to the DOCUMENT below, correcting "
                f"the error above. Output ONLY the complete corrected file — "
                f"no explanations, no code fences.\n\n"
                f"DOCUMENT ({file_label}):\n-----\n{source}\n-----\n"
                f"INSTRUCTION: {instruction}"
            )
            messages = [
                {"role": "system",    "content": system},
                {"role": "user",      "content": base_user},
                {"role": "assistant", "content": previous_revised},
                {"role": "user",      "content": correction},
            ]
            trace_content = (
                f"{base_user}\n"
                f"[assistant turn: {len(previous_revised)} chars]\n"
                f"{correction}"
            )

        else:
            # Clean retry: a single turn with a delimited correction, referring to the document by its label.
            correction_block = (
                f"\n\n---CORRECTION---\n"
                f"Error in previous attempt: {feedback}\n"
                f"Re-apply the INSTRUCTION to DOCUMENT ({file_label}), "
                f"correcting the error above.\n"
                f"INSTRUCTION (reminder): {instruction}\n"
                "Output ONLY the complete corrected file content — no explanations, no code fences.\n"
                "---END CORRECTION---"
            )
            messages = [
                {"role": "system", "content": system},
                {"role": "user",   "content": base_user + correction_block},
            ]
            trace_content = base_user + correction_block

        url = self._chat_url()
        headers = self._headers()
        payload = {"model": self.model,
                   "messages": messages,
                   "temperature": self._cfg_temp("file_editor", 0.2),
                   "num_ctx": getattr(self, "num_ctx", 0)}
        if getattr(self, "file_editor_max_tokens", 0):
            payload["max_tokens"] = self.file_editor_max_tokens
        print(f"\n[{_ts()}] ✏️  edit → model:")
        tracer.event("orchestrator", "file_editor", "llm_request",
                     params={"file": file_label, "instruction": instruction,
                             "prev_context": previous_revised is not None},
                     content=trace_content, model=self.model,
                     temperature=self._cfg_temp("file_editor", 0.2))
        try:
            _on_tok, _tok_stats = stream_tracker()
            out = request_completion(
                url, headers, payload, self.timeout_seconds,
                stream=True,
                api_format=self.api_format,
                on_token=_on_tok,
                ssl_context=self.ssl_context,
            )
            print()
            if _s := _tok_stats():
                print(f"[{_ts()}] {_s}")
            out = self._strip_code_fence(strip_think(out))
            tracer.event("file_editor", "orchestrator", "llm_response", content=out)
            return out
        except Exception as e:
            # A transient failure returns None, not "", so run_edit retries it instead of reporting no content.
            logger.error(f"file edit call failed: {e}")
            print(f"[{_ts()}] edit generation failed: {e}")
            return None

    def _validate_edit(self, instruction: str, original: str, revised: str,
                        stream_mode: bool = False) -> dict:
        """Validate that `revised` correctly applies `instruction` to `original`.
        stream_mode=True uses streaming to avoid Ollama blocking hangs (tokens
        are accumulated silently; caller always shows a Spinner for feedback).
        """
        system = (
            "You are an edit QA validator. Given ORIGINAL, INSTRUCTION and REVISED file "
            "content, decide whether REVISED correctly applies the instruction, fixes the "
            "stated errors, preserves content that should stay, and is not truncated or "
            "corrupted. Return STRICT JSON only: "
            '{"status":"approved" or "needs_fix","feedback":"what is wrong, empty if ok"}'
        )
        user = (f"ORIGINAL:\n{original}\n\nINSTRUCTION:\n{instruction}\n\n"
                f"REVISED:\n{revised}")
        # ORIGINAL plus REVISED can overflow the context window and silently lose the prompt head; warn when it does.
        _nc = getattr(self, "num_ctx", 0)
        if _nc:
            try:
                from tools.auto.utils import chars_per_token
                _est_tokens = len(user) / chars_per_token(user)
                if _est_tokens > _nc * 0.9:
                    print(f"[{_ts()}] ⚠️  Edit validator prompt ≈{int(_est_tokens)} "
                          f"tokens vs num_ctx={_nc} — the window will overflow "
                          f"and the verdict may be based on a truncated file. "
                          f"Consider a larger num_ctx profile for files this size.")
            except Exception:
                pass  # estimation is best-effort; never block validation
        url = self._chat_url()
        headers = self._headers()
        payload = {"model": self.model,
                   "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": user}],
                   "temperature": self._cfg_temp("validator_agent", 0.1),
                   "num_ctx": getattr(self, "num_ctx", 0)}
        tracer.event("orchestrator", "edit_validator", "llm_request",
                     params={"instruction": instruction}, content=user,
                     model=self.model, temperature=self._cfg_temp("validator_agent", 0.1))
        # A network failure is an API error; a malformed reply is validated as a normal needs_fix (no unbounded backoff).
        try:
            content = strip_think(request_completion(url, headers, payload, self.timeout_seconds,
                                                      stream=stream_mode,
                                                      api_format=self.api_format,
                                                      ssl_context=self.ssl_context))
        except Exception as e:
            logger.error(f"edit validator API call failed: {e}")
            return {"status": "needs_fix", "feedback": f"validator unavailable: {e}",
                    "_api_error": True}

        tracer.event("edit_validator", "orchestrator", "llm_response", content=content)
        try:
            return self._parse_llm_json(content)
        except Exception as e:
            logger.warning(f"edit validator returned unparseable content: {e}")
            # A parse failure sets _unparseable, like the other validators.
            return {
                "status": "needs_fix",
                "feedback": (
                    "Your reply could not be parsed as the required STRICT "
                    'JSON {"status": ..., "feedback": ...} — reply with '
                    "JSON only, no prose, no code fences."
                ),
                "_unparseable": True,
            }

    def run_edit(self, user_input: str, base_dir: str,
                 resume_state: dict = None) -> None:
        """
        /edit — apply an instruction to a file AND WRITE IT BACK.
        Generates the full revised content, validates it (retry up to
        max_iterations), backs up the original to <file>.bak.<HHMMSS>,
        writes the new content, and prints a unified diff.
        Issue 7: exponential back-off on API errors; checkpoint saved to
        pipeline_state.json on KeyboardInterrupt for resume on restart.
        """
        import difflib
        instruction, file_path = self._parse_edit_command(user_input)
        if not instruction or not file_path:
            print("Usage: /edit <instruction> in <file>   (or: /edit <file> :: <instruction>)")
            return
        target = _resolve_within_base(base_dir, file_path)
        if target is None:
            print(f"[{_ts()}] Error: '{file_path}' is outside the project directory.")
            return
        if not os.path.isfile(target):
            print(f"[{_ts()}] Not a file: {target}")
            return
        start = time.time()
        tracer.start_run(f"/edit {instruction} in {file_path}")

        # ── HARDEN-14 C3: a UTF-16 / UTF-32 file is refused by name ───────────────
        # Such a file read as latin-1 is a wall of NULs the model cannot work with: it
        # cannot find its line, the edit applies nothing, and `/edit` still printed
        # `status=approved` with a backup while the file lost its `\r`. Refused like a file
        # with undecodable bytes today — and left byte-identical.
        try:
            with open(target, "rb") as _fh:
                _wide = _wide_bom_name(_fh.read())
        except OSError:
            _wide = None
        if _wide:
            print(f"[{_ts()}] ❌ {file_path} is {_wide} — convert it to UTF-8 first; "
                  f"nothing was written.")
            return

        # ── CC-16 (Part C): the write encoding, decided once ───────────────────────
        # From the file's own bytes, before the model is asked, so the run has one
        # decision rather than one derived at the write (where the edit is already
        # spent). See `_edit_write_encodings`: a UTF-8 file is ("utf-8",), exactly
        # what fix 34 chose; a legacy file gets the codecs it can be written back
        # in, its own first and latin-1 last.
        _edit_encodings = _edit_write_encodings(target)
        _legacy_named = [e for e in _edit_encodings if e not in ("utf-8", "latin-1")]
        _encoding = _edit_encodings[0] if len(_edit_encodings) == 1 else ""

        # HARDEN-14 C1: the file is read in the codec it will be *written* back in, so the
        # model is shown the text it is asked about — not latin-1 mojibake of a cp1251 file.
        original = _read_edit_target(target, _edit_encodings)

        def _refuse_unwritable(text: str) -> bool:
            """True (after saying why) when *text* fits none of the file's write
            encodings. Checked the moment a revision exists — so a legacy file
            whose revision carries characters its codec cannot hold is refused
            before the validator runs and long before a write is attempted,
            instead of after every iteration has been spent."""
            nonlocal _encoding
            encoding, missing = _pick_edit_encoding(_edit_encodings, text)
            if encoding is not None:
                _encoding = encoding
                return False
            print(f"[{_ts()}] ❌ Cannot write {file_path}: the file is not UTF-8 "
                  f"({_legacy_named[0] + '?' if _legacy_named else 'latin-1'}) and the "
                  f"revised text has characters it cannot hold: {missing}. "
                  f"Convert the file to UTF-8 first, or ask for a revision "
                  f"without those characters — nothing was written.")
            return True

        # ── prev-context window ─────────────────────────────────────────────────────────
        _prev_ctx_every   = 0
        _prev_ctx_max     = 0   # max chars for previous_revised; 0 = no limit
        _cfg = getattr(self, "config", None)
        if _cfg:
            _prev_ctx_every = safe_getint(_cfg, "file_editor", "prev_context_every",   fallback=0)
            _prev_ctx_max   = safe_getint(_cfg, "file_editor", "prev_context_max_chars", fallback=0)

        revised, validation, feedback = "", {}, None
        _prev_shown_count = 0     # consecutive iters where previous_revised was shown
        _is_reset_iter    = False  # True → this iteration must be clean (no prev)
        _api_err_count    = 0     # consecutive API errors; reset on non-error response

        # ── Issue 7: restore checkpoint ────────────────────────────────────────
        iteration = 1
        if resume_state and resume_state.get("loop") == "run_edit":
            iteration         = resume_state.get("iteration", 1)
            revised           = resume_state.get("revised", "")
            feedback          = resume_state.get("feedback")
            _prev_shown_count = resume_state.get("_prev_shown_count", 0)
            _is_reset_iter    = resume_state.get("_is_reset_iter", False)
            print(f"[{_ts()}] ▶  Resuming run_edit from iteration {iteration} "
                  "(checkpoint restored).")
            # A resumed revision is checked before anything else is asked for: it
            # is already known to be unwritable, and re-deriving it would cost
            # another round of model calls for the same refusal.
            if revised.strip() and _refuse_unwritable(revised):
                return

        # A while loop lets an API error retry without using up the iteration count.
        while iteration <= self.max_iterations:
            if time.time() - start >= self.timeout_seconds:
                print(f"[{_ts()}] ⏳ timeout; using best edit so far.")
                break

            # Decide whether to pass the previous attempt to the editor.
            _use_prev = (
                bool(revised)            # have a previous attempt
                and _prev_ctx_every > 0  # feature enabled in agents.ini
                and not _is_reset_iter   # not a forced clean iteration
            )
            if _is_reset_iter and revised:
                print(f"[{_ts()}] 🔄 Reset iter {iteration} — clean feedback only "
                      f"(window of {_prev_ctx_every} exhausted).")

            # Truncate previous_revised to avoid blowing the context window.
            _prev_to_pass = None
            if _use_prev:
                if _prev_ctx_max > 0 and len(revised) > _prev_ctx_max:
                    _prev_to_pass = revised[:_prev_ctx_max] + f"\n…(truncated at {_prev_ctx_max} chars)"
                    logger.debug("previous_revised truncated %d→%d chars", len(revised), _prev_ctx_max)
                else:
                    _prev_to_pass = revised

            _step_start = time.time()
            _new_revised = self._edit_file_content(
                instruction, file_path, original,
                feedback=feedback,
                previous_revised=_prev_to_pass,
            )

            if _new_revised is None:
                # A transient generation failure is retried, as the validator call is, instead of ending /edit.
                _api_err_count += 1
                if iteration >= self.max_iterations:
                    print(f"[{_ts()}] ⚠️  Edit generation unavailable — "
                          f"max iterations reached, using best edit so far.")
                    break
                _chk = {
                    "loop": "run_edit",
                    "user_input": user_input,
                    "base_dir": base_dir,
                    "instruction": instruction,
                    "file_path": file_path,
                    "iteration": iteration,  # retry same iteration
                    "revised": revised,      # keep the last good attempt, not the failure
                    "feedback": feedback,
                    "_prev_shown_count": _prev_shown_count,
                    "_is_reset_iter": _is_reset_iter,
                }
                backoff.api_error_pause(_api_err_count, _chk)
                continue  # retry same iteration (do not increment)

            revised = _new_revised
            if not revised.strip():
                print(f"[{_ts()}] No content produced — file left unchanged.")
                return
            # CC-16 (Part C): the revision is checked against the file's write
            # encoding as soon as it exists, before the validator is called and
            # before any backup is made. A legacy file the model has *fixed* —
            # real Cyrillic, curly quotes, a dash — is written back in its own
            # codec; a revision no codec here can hold is refused here, not after
            # the last iteration has run and the whole edit is already spent.
            if _refuse_unwritable(revised):
                return
            print(f"[{_ts()}] ⏱  Edit generated in {time.time() - _step_start:.1f}s")
            print(f"[{_ts()}] 🤖 Validating edit ({iteration}/{self.max_iterations})...")
            _val_start = time.time()
            with Spinner(f"Edit validator {iteration}/{self.max_iterations}"):
                validation = self._validate_edit(instruction, original, revised,
                                                 stream_mode=self.stream_agents)
            _val_elapsed = time.time() - _val_start

            if validation.get("status") == "approved":
                _api_err_count = 0
                print(f"[{_ts()}] ✅ Edit approved by validator. ({_val_elapsed:.1f}s)")
                break

            if validation.get("_api_error"):
                # Issue 7: exponential backoff instead of immediate break.
                _api_err_count += 1
                if iteration >= self.max_iterations:
                    print(f"[{_ts()}] ⚠️  Edit validator unavailable — "
                          f"max iterations reached, writing best effort. "
                          f"({reply_text(validation.get('feedback'))}, {_val_elapsed:.1f}s)")
                    break
                _chk = {
                    "loop": "run_edit",
                    "user_input": user_input,
                    "base_dir": base_dir,
                    "instruction": instruction,
                    "file_path": file_path,
                    "iteration": iteration,  # retry same iteration
                    "revised": revised,
                    "feedback": feedback,
                    "_prev_shown_count": _prev_shown_count,
                    "_is_reset_iter": _is_reset_iter,
                }
                backoff.api_error_pause(_api_err_count, _chk)
                continue  # retry same iteration (do not increment)

            _api_err_count = 0
            # HARDEN-7 C: the feedback is read as text, so a validator that
            # listed its complaints as a JSON array prints them instead of
            # raising AttributeError out of `/edit` — and out of the shell with
            # it, since nothing there catches it.
            feedback = reply_text(validation.get("feedback"))
            if feedback:
                print(f"[{_ts()}] ❗ Edit feedback: {feedback}")

            # ── update prev-context window state for the NEXT iteration ───
            if _prev_ctx_every > 0:
                if _use_prev:
                    _prev_shown_count += 1
                    if _prev_shown_count >= _prev_ctx_every:
                        _prev_shown_count = 0
                        _is_reset_iter    = True
                else:
                    _is_reset_iter = False

            iteration += 1

        # An empty revision is never written, so the target is not emptied.
        if not revised.strip():
            print(f"[{_ts()}] No content produced — file left unchanged.")
            return

        # The model was shown the file with its secrets masked: a line it gave back masked is the original line.
        revised = restore_masked_secrets(original, revised)
        # HARDEN-13 A2: a line the restore could not put a real key back into is a
        # placeholder written over a working key, and the write below would report
        # success while doing it. The edit is refused, the file is left as it is,
        # and the line that could not be restored is named.
        for number, line in unrestored_masked_lines(original, revised):
            print(f"[{_ts()}] ❌ {file_path}: line {number} still holds the masked "
                  f"placeholder ({line.strip()}) — the original key it came from "
                  f"could not be restored; the file is left unchanged. Ask for the "
                  f"line exactly as it was given to you.")
            return
        if _refuse_unwritable(revised):
            return

        # If the loop ends with needs_fix, the file is written anyway (the .bak keeps it reversible), and the user is told.
        if validation.get("status") != "approved":
            print(f"[{_ts()}] ⚠️  WRITING UNAPPROVED EDIT — validator never "
                  f"approved after {self.max_iterations} iteration(s). "
                  f"Last feedback: {validation.get('feedback', '(none)')!r}. "
                  f"Original is preserved in the .bak file below — review the "
                  f"diff carefully.")

        # Write: back up current content with a timestamped suffix, then overwrite.
        backup = target + ".bak." + time.strftime("%H%M%S")
        _bak_n = 0
        while os.path.exists(backup):
            _bak_n += 1
            backup = f"{target}.bak.{time.strftime('%H%M%S')}.{_bak_n}"
        try:
            import shutil
            from tools.auto.utils import atomic_write_text
            shutil.copy2(target, backup)        # the original's bytes and mode
            # HARDEN-14 C2: `_edit_line_ends` decides each line's own ending, and keeps the
            # BOM, so the write is not run through `in_style_of`'s whole-file majority pass —
            # which is exactly the flattening this part removes.
            atomic_write_text(
                target, _edit_line_ends(target, revised), newline="", encoding=_encoding,
            )
        except Exception as e:
            # CC-16 (Part C): the backup was made for a write that never landed.
            # `copy2` runs before anything can fail, so without this a refused or
            # failed edit leaves a timestamped `.bak` behind — an artifact of an
            # edit that did not happen, which is exactly the stray
            # `note.txt.bak.135608` this part was filed over. A backup of a write
            # that *succeeded* stays, as fix 34 left it.
            _remove_edit_backup(backup)
            print(f"[{_ts()}] ❌ Failed to write {target}: {e}")
            return

        total = time.time() - start
        diff = "".join(difflib.unified_diff(
            original.splitlines(keepends=True), revised.splitlines(keepends=True),
            fromfile=f"{file_path} (before)", tofile=f"{file_path} (after)"))
        print(f"\n[{_ts()}] ── FILE EDITED: {file_path} — {total:.1f}s from request ──")
        print(diff if diff.strip() else "(no textual changes)")
        # HARDEN-18 C: a path may hold a space, so both sides of the hint are
        # shell-quoted — `mv dir/my notes.txt.bak.105124 dir/my notes.txt` is
        # four arguments and fails before it helps anyone.
        print(f"\n[{_ts()}] status={validation.get('status','unknown')}  "
              f"backup={os.path.basename(backup)}  "
              f"(restore with: mv {shlex.quote(backup)} {shlex.quote(target)})")
