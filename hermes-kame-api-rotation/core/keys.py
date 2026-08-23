"""Bulk API-key parsing — pure, no Hermes, no filesystem, no secrets logged.

Everything here operates on bytes or strings the caller already has in hand.
The module never reads a file, never writes one, and never emits a key: the
only outward representation of a key it produces is ``redact()``.

Split from the classification core on purpose. Rotation reasoning and key
intake share nothing but the plugin they ship in, and keeping intake pure is
what lets it be tested without any credential ever existing.
"""

from __future__ import annotations

import codecs
import re
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# Callers paste from wildly different places — a .env line, a spreadsheet
# column, a chat message, Agent Zero's comma list. Treat every plausible
# separator the same rather than making the user reformat.
_SEPARATORS = re.compile(r"[,;\r\n\t|]+")

# A byte-order mark is not part of anything. It reaches this module two ways
# on Windows and both were losing keys: a file written by Notepad or by
# ``Set-Content -Encoding utf8BOM`` starts with one, and text copied out of
# such a file carries it into a paste. Verified against files written by
# PowerShell rather than assumed — with the mark left in place the first key
# of a UTF-8-with-BOM file was rejected, and a UTF-16 file lost every key.
# Spelled as escapes: written literally these are invisible in an editor,
# which is the whole reason they cost keys in the first place.
_BOM_CHARS = "﻿￾"

# Longest first. ``BOM_UTF32_LE`` begins with the two bytes of
# ``BOM_UTF16_LE``, so checking UTF-16 first would decode a UTF-32 file as
# UTF-16 and produce mojibake with no error at all. The codec names carry no
# endianness suffix on purpose: that is what makes the codec itself consume
# the mark instead of leaving it in the text.
_BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)


def decode_text(data: bytes) -> str:
    """Decode an imported file, honouring whatever mark it starts with.

    UTF-8 is the answer for anything unmarked, which is every file this is
    likely to see. The marks are handled because the user is on Windows and
    the tools there are one flag from writing them: ``Set-Content -Encoding
    utf8BOM`` and ``unicode``, and Windows PowerShell's ``>`` redirect, which
    is UTF-16 by default.

    ``errors="replace"`` throughout: a file that is not text at all should
    produce rejected tokens and a report, never an exception in a chat turn.
    """
    if not isinstance(data, (bytes, bytearray)):
        return str(data or "")
    for mark, encoding in _BOMS:
        if data.startswith(mark):
            return bytes(data).decode(encoding, errors="replace")
    return bytes(data).decode("utf-8", errors="replace")


# A key that survives parsing must still look like a credential. These bounds
# are deliberately loose: they exist to catch a pasted label, a stray URL, or
# a header row, not to validate any particular provider's format.
MIN_KEY_LENGTH = 16
MAX_KEY_LENGTH = 512

# Common shapes that are definitely not a key, even though they are long
# enough and have no spaces.
_NOT_A_KEY = re.compile(
    r"^(?:https?://|[A-Z_]+=|#|//|-{2,})",
    re.I,
)

# `KEY=value` and `KEY: value` are how keys appear in .env files and YAML.
# Take the value; the name is a label at best.
_ASSIGNMENT = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*[=:]\s*(.+?)\s*$")

_QUOTES = "\"'`"


def redact(key: str) -> str:
    """Display form of a key: enough to recognise, not enough to use.

    The only function in this package that takes a live key and returns
    something printable. Everything user-facing goes through it.
    """
    text = (key or "").strip()
    if not text:
        return ""
    if len(text) <= 12:
        # Too short to show any of it without giving most of it away.
        return "*" * len(text)
    return f"{text[:6]}…{text[-4:]}"


def _strip_wrapper(token: str) -> str:
    """Peel quotes, and unwrap a `NAME=value` / `NAME: value` assignment."""
    # Before anything else, the mark: it is zero-width, so it survives every
    # ``strip()`` below and every eye reading the paste, and it makes the key
    # it is glued to fail the printable-ASCII test.
    text = token.strip().strip(_BOM_CHARS).strip()
    match = _ASSIGNMENT.match(text)
    # `https://host/path` also matches the assignment shape — scheme, colon,
    # rest. Unwrapping it would turn a URL into `//host/path` and lose the
    # evidence that it was a URL, so leave anything protocol-like alone.
    if match and not match.group(2).startswith("//"):
        text = match.group(2).strip()
    while len(text) >= 2 and text[0] in _QUOTES and text[-1] == text[0]:
        text = text[1:-1].strip()
    return text


def looks_like_api_key(token: str) -> bool:
    text = (token or "").strip()
    if not (MIN_KEY_LENGTH <= len(text) <= MAX_KEY_LENGTH):
        return False
    if any(ch.isspace() for ch in text):
        return False
    if _NOT_A_KEY.match(text):
        return False
    # Printable ASCII only. A key with a smart quote or a zero-width space in
    # it came from a mangled copy-paste and would fail at the provider with a
    # confusing 401 hours later.
    return all(32 < ord(ch) < 127 for ch in text)


def parse_keys(raw: str) -> Tuple[List[str], List[str]]:
    """Split pasted text into ``(keys, rejected)``.

    Order is preserved and exact duplicates within the input collapse to the
    first occurrence, so pasting the same list twice is harmless.

    Whitespace is a separator only *between* candidates — a key never
    contains one — which is what lets a space-separated paste and a
    comma-separated paste both work without the caller declaring which it is.

    ``rejected`` holds only tokens long enough to have been *meant* as a key.
    Short words are dropped silently: a paste like ``minhas chaves: K1, K2``
    should report nothing about the first two words, and a rejection list
    longer than the import itself trains the user to ignore it.
    """
    keys: List[str] = []
    rejected: List[str] = []
    seen = set()

    for chunk in _SEPARATORS.split(raw or ""):
        for token in chunk.split():
            candidate = _strip_wrapper(token)
            if not candidate:
                continue
            if not looks_like_api_key(candidate):
                if len(candidate) >= MIN_KEY_LENGTH:
                    rejected.append(candidate)
                continue
            if candidate in seen:
                continue
            seen.add(candidate)
            keys.append(candidate)

    return keys, rejected


class ImportPlan:
    """What a bulk import would do, decided before anything is written.

    Held as a plan rather than executed inline so the command layer can show
    it, and so this can be tested without a credential store existing.
    """

    __slots__ = ("new", "duplicates", "rejected")

    def __init__(
        self,
        new: Sequence[str],
        duplicates: Sequence[str],
        rejected: Sequence[str],
    ) -> None:
        self.new: List[str] = list(new)
        self.duplicates: List[str] = list(duplicates)
        self.rejected: List[str] = list(rejected)

    @property
    def is_empty(self) -> bool:
        return not self.new

    def summary(self) -> str:
        parts = [f"{len(self.new)} new"]
        if self.duplicates:
            parts.append(f"{len(self.duplicates)} already in pool")
        if self.rejected:
            parts.append(f"{len(self.rejected)} rejected")
        return ", ".join(parts)


def plan_import(raw: str, existing: Iterable[str]) -> ImportPlan:
    """Decide which pasted keys are actually new.

    ``existing`` is the set of raw tokens already in the pool. Matching on the
    full token is the only safe test — two different keys can share a prefix,
    and redacted previews are lossy by design.
    """
    parsed, rejected = parse_keys(raw)
    existing_set = {str(token).strip() for token in existing if str(token).strip()}

    new = [key for key in parsed if key not in existing_set]
    duplicates = [key for key in parsed if key in existing_set]
    return ImportPlan(new=new, duplicates=duplicates, rejected=rejected)


def build_labels(
    count: int,
    *,
    prefix: str = "kame",
    start: int = 1,
    taken: Optional[Iterable[str]] = None,
) -> List[str]:
    """Generate non-colliding labels for a batch of imported keys.

    Labels are cosmetic — the pool keys on id — but colliding ones make the
    dashboard list unreadable, which is exactly when the user needs it.
    """
    used = {str(label).strip() for label in (taken or ()) if str(label).strip()}
    labels: List[str] = []
    index = start
    for _ in range(max(0, count)):
        while f"{prefix} #{index}" in used:
            index += 1
        label = f"{prefix} #{index}"
        used.add(label)
        labels.append(label)
        index += 1
    return labels


def format_plan_lines(plan: ImportPlan) -> List[str]:
    """Human-readable, redacted rendering of a plan. Never prints a live key."""
    lines: List[str] = []
    for key in plan.new:
        lines.append(f"  + {redact(key)}")
    for key in plan.duplicates:
        lines.append(f"  = {redact(key)} (already in pool)")
    for token in plan.rejected:
        # Rejected tokens are shown redacted too. A rejected token is usually
        # junk, but "usually" is not a property worth betting a key on.
        lines.append(f"  ! {redact(token)} (does not look like an API key)")
    return lines


def group_status(entries: Sequence[Dict[str, object]]) -> List[str]:
    """Render pool entries as status lines, given already-redacted summaries.

    An entry the caller marked ``usable: False`` is one the pool will never
    select — it holds no runtime key at all. Its stored status is whatever it
    was the last time anything happened to it, which is usually ``ok``, and
    printing that is how a pool with no working credential reads as healthy.
    Report the reason it will never be picked instead of the status of a
    request it will never make.
    """
    lines: List[str] = []
    for entry in entries:
        label = str(entry.get("label") or "?")
        preview = str(entry.get("token_preview") or "")
        count = entry.get("request_count") or 0
        holds = int(entry.get("holds") or 0)
        if holds > 1:
            # A row whose value is several keys is not a key, and redacting it
            # is what makes that invisible: the first six characters and the
            # last four of ``k1,k2,k3`` are the start of one key and the end of
            # another, so it prints as a perfectly ordinary credential. Say
            # what it is instead, and let the parts below speak for themselves.
            lines.append(f"  [list] {label}  (holds {holds} keys — the rows below)")
            continue
        if entry.get("usable") is False:
            lines.append(f"  [no key] {label}  (empty — the pool skips it)")
            continue
        status = str(entry.get("last_status") or "ok")
        lines.append(f"  [{status}] {label}  {preview}  ({count} reqs)")
    return lines


def count_usable(entries: Sequence[Dict[str, object]]) -> int:
    """How many of these entries the pool could actually pick.

    Absent the flag an entry counts as usable: every caller that does not
    know about the distinction should keep reading the way it always did.

    A row that holds several keys counts as none of them. It is a container,
    and the keys inside it are counted where they appear — as their own
    entries. Counting both would report eight credentials for seven keys.
    """
    return sum(
        1
        for entry in entries
        if entry.get("usable") is not False and not int(entry.get("holds") or 0) > 1
    )
