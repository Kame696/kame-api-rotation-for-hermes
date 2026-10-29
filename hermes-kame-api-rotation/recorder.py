"""Write down what the provider actually sent. Decide nothing, ever.

Why this exists
---------------

The corpus this plugin is measured against holds 13,561 real refusals and not
one of them carries the field that separates a per-minute quota from a per-day
one. Not because it was lost: no version before 1.7.0.0 read the structured
body, so no version ever wrote it down. The field was never there to find.

Without it the gate (``tools/gate.py``) can only test the worst case — the case
where nothing names a wait. This module is what turns the worst case into the
real one: it records the payload as it arrives, one line per refusal, and
returns.

The same gap held decision 0004 D1 hostage: a stated 5xx deadline is obeyed
under the owner's ceiling, but the corpus that decision was measured against —
1,387 real refusals — carried not one ``Retry-After``, because no version
before 1.8.0.0 ever wrote a header down. This module now does, allowlisted
and redacted, so the next version can measure that decision instead of
inheriting the same blind spot forward.

The contract, deliberately short
--------------------------------

* **Writes only.** It does not classify, does not size a rest, does not rotate
  a key, does not change a single code path. Deleting this file changes no
  behaviour at all — which is the point of it existing.
* **Never raises.** The whole body sits under ``except Exception``. A fault here
  must not cost a turn: recording evidence is not worth the price of losing an
  answer.
* **Never leaks a key.** Every string passes the redactor before it touches
  disk, and headers are allowlisted by name before that — Authorization,
  API keys and cookies are never even considered, whatever they are called.
* **Never grows without bound.** At the ceiling the file moves to one previous
  generation and a new one starts (1.8.3.0; it used to stop, silently), and the
  headers block carries its own, smaller caps on top.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: File ceiling. 1.8.3.0 (decisions/0033 D6): past this the file moves to its one previous
#: generation (``refusals.1.jsonl``) and a new one starts, so the disk total stays 8 MB.
#: Until 1.8.2.9 the recorder went quiet here, without a word.
CEILING_BYTES = 4 * 1024 * 1024

#: On by default. ``KAME_RECORDER_DISABLED=1`` turns it off without uninstalling
#: anything.
DISABLE_ENV = "KAME_RECORDER_DISABLED"

FILENAME = "refusals.jsonl"

# API keys carry known prefixes; any of them becomes <KEY> before the text
# reaches disk. The second pattern catches the ``field=value`` shape, which is
# how a key shows up when a provider echoes the whole request back in an error.
_KEY = re.compile(r"\b(?:AIza|sk-|nvapi-|gsk_|xai-|sk-ant-|hf_)[A-Za-z0-9_\-]{12,}", re.I)
_KEY_FIELD = re.compile(
    r"((?:api[_-]?key|access[_-]?token|authorization|bearer|secret)\s*[=:\"']{1,3}\s*)"
    r"([^\s,;&\"']{8,})",
    re.I,
)

# Decision 0004 D1: a stated 5xx deadline is obeyed under the owner's
# ceiling, but that decision had zero evidence either way in this corpus —
# 1,387 real refusals and not one carries a `Retry-After`, because no
# version before 1.8.0.0 ever wrote a header down. This block is what closes
# that gap, in the same "decide nothing, ever" spirit as the rest of the
# file: an allowlist of names, not a blacklist of the credential names
# someone has thought of so far, because the whole reason to keep this
# corpus is to see a header shape nobody predicted.
#
# Only what ``core.quota.extract_from_headers``/``timing_headers`` actually
# read — Retry-After and any rate-limit/quota/usage reset — plus Date and a
# request id for provenance, are worth the disk. Everything else is
# dropped, unread and unnamed, on the theory that the field this module
# exists to catch is worth keeping precise rather than broad.
_HEADER_ALLOW = re.compile(
    r"^(?:"
    r"retry-after(?:-ms)?"
    r"|date"
    r"|(?:x-)?request-id"
    r"|.*(?:rate-?limit|quota|usage)[a-z0-9\-]*"
    r")$",
    re.I,
)

# Named explicitly rather than left to the allowlist missing them: a
# credential header must never reach disk even if some future rename made
# it match the pattern above by accident (imagine a provider calling one
# "x-quota-api-key"). Matched as a substring of the lower-cased header name.
_HEADER_DENY_MARKERS = (
    "authorization", "api-key", "apikey", "cookie", "secret",
    "credential", "password", "bearer",
)

#: Ceilings on the headers block itself, independent of ``CEILING_BYTES`` on
#: the whole file — a provider that echoes back fifty headers, or one
#: absurdly long value, must not let a single refusal dominate the corpus
#: the gate reads.
MAX_HEADERS = 20
MAX_HEADER_NAME_LEN = 80
MAX_HEADER_VALUE_LEN = 200

# A value this long with nothing but token characters and no separators is
# not a retry count, a rate-limit window name, or an RFC-1123 date, whatever
# the header happened to be called — it is opaque, and opaque beside a
# quota/rate-limit-shaped name is exactly what a credential looks like when
# nobody named the header correctly. 40 is comfortably past every real
# rate-limit value seen in this corpus (all under ten characters) and past
# the shortest recognized key prefix in ``_KEY`` plus its minimum body.
_OPAQUE_VALUE = re.compile(r"^[A-Za-z0-9_\-\.]{40,}$")

# Two shapes the length rule above lets through, both found by feeding
# adversarial headers at this function rather than by reading it: a bare
# 32-character hex string (`x-usage-id: a1b2…d6`, the shape of an id, a hash
# and half the world's secrets at once) and an address (`x-quota-account:
# user@example.com`). Neither is a retry count, a window name or a date, so
# neither is worth the disk — and an address is the owner's, not a number
# about his quota.
_HEX_TOKEN = re.compile(r"^[0-9a-f]{24,}$", re.I)
_ADDRESS = re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")

# Kept for callers (and tests) that reset it: since 1.8.3.0 nothing in this module sets it, because a
# full log rotates instead of going quiet.
_silenced = False


def _redact(text: str) -> str:
    from .core.redact import redact
    text = redact(text, limit=0)
    text = _KEY.sub("<KEY>", text)
    return _KEY_FIELD.sub(lambda m: m.group(1) + "<KEY>", text)


def _safe_attr(obj: Any, name: str) -> Any:
    """Read one attribute, or nothing — never let somebody else's property
    raise on us. Same guard ``core.evidence._safe`` uses; kept local so this
    module still needs nothing else in the plugin to do its one job.
    """
    try:
        return getattr(obj, name, None)
    except Exception:
        return None


def _raw_headers(error: Any) -> Any:
    """Headers off the exception, or off its response.

    Same three-deep read ``core.evidence._read_headers`` uses: ``.headers``,
    then ``.response_headers``, then ``.response.headers``.
    """
    headers = _safe_attr(error, "headers")
    if headers is None:
        headers = _safe_attr(error, "response_headers")
    if headers is None:
        response = _safe_attr(error, "response")
        if response is not None:
            headers = _safe_attr(response, "headers")
    return headers


def _header_items(headers: Any) -> List[Tuple[str, Any]]:
    if headers is None:
        return []
    try:
        if hasattr(headers, "items"):
            return [(str(k), v) for k, v in headers.items()]
        if isinstance(headers, (list, tuple)):
            return [(str(k), v) for k, v in headers if k is not None]
    except Exception:
        return []
    return []


def _looks_like_credential(name: str, value: Any) -> bool:
    """Whether this header, allowlisted or not, is not worth the risk.

    Two independent reasons to say yes: the *name* is one a credential is
    actually called, or the *value* is a long unbroken token — the shape of
    a key that ended up under a header nobody meant it to.
    """
    lowered_name = name.strip().lower()
    if any(marker in lowered_name for marker in _HEADER_DENY_MARKERS):
        return True
    text = str(value if value is not None else "")
    if _KEY.search(text):
        return True
    stripped = text.strip()
    if _OPAQUE_VALUE.match(stripped):
        return True
    if _HEX_TOKEN.match(stripped):
        return True
    if _ADDRESS.search(stripped):
        return True
    return False


def _safe_headers(headers: Any) -> Dict[str, str]:
    """The redacted, allowlisted, capped subset of ``headers`` worth keeping.

    Allowlist first, deny second, cap last — a header has to clear all three
    to reach the row. Failing any one of them drops it whole rather than
    writing a partial or placeholder value, matching the rest of this file:
    when in doubt, write nothing.
    """
    kept: Dict[str, str] = {}
    for name, value in _header_items(headers):
        if len(kept) >= MAX_HEADERS:
            break
        clean_name = str(name or "").strip()[:MAX_HEADER_NAME_LEN]
        if not clean_name or not _HEADER_ALLOW.match(clean_name):
            continue
        if _looks_like_credential(clean_name, value):
            continue
        kept[clean_name.lower()] = _redact(str(value))[:MAX_HEADER_VALUE_LEN]
    return kept


#: 1.8.2.0: provider bodies can echo the prompt, so each free-text field is
#: capped near ``core.redact.DEFAULT_LIMIT``. What the corpus actually measures
#: from a body — ``quotaId``, ``quotaMetric``, ``retryDelay``, ``reason`` — is
#: pulled out first into ``fields`` (see :func:`_quota_fields`), so the cap
#: costs no evidence.
TEXT_LIMIT = 600

_FIELD_RE = re.compile(
    r'"(quotaId|quotaMetric|quotaDimensions|retryDelay|reason|domain|quotaValue)"\s*:\s*'
    r'("(?:[^"\\]|\\.){0,200}"|\{[^{}]{0,400}\}|-?\d+(?:\.\d+)?)'
)


def _quota_fields(*texts: Any) -> Dict[str, List[Any]]:
    """Every quota/retry field the provider named, from the full uncapped text."""
    found: Dict[str, List[Any]] = {}
    for text in texts:
        if not text:
            continue
        for name, raw in _FIELD_RE.findall(str(text)):
            try:
                value = json.loads(raw)
            except ValueError:
                value = raw
            bucket = found.setdefault(name, [])
            if value not in bucket and len(bucket) < 8:
                bucket.append(value)
    return found


def private_append(path: Any) -> Any:
    """Open a KAME jsonl for appending, owner-only (0600) — created that way and
    tightened if an older release created it wider. On Windows the mode is
    advisory; the file sits in the user's own profile directory."""
    descriptor = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.chmod(str(path), 0o600)
    except OSError:
        pass
    return os.fdopen(descriptor, "a", encoding="utf-8")


# --- 1.8.3.0 (decisions/0033 D6): the instrument logs rotate instead of going silent ---------------
#
# Until 1.8.2.9 ``calls.jsonl`` stopped writing at 4 MB and ``refusals.jsonl`` at 8 MB and said
# nothing; the stop held for 13 days before anyone saw it. Now a log that reaches its ceiling moves to
# its ONE previous generation (``calls.1.jsonl``) and a new file starts, so the disk total is what it
# was. The same scheme and sizes as the Agent Zero port (its decision 0011).

#: The size is looked at on the first write after start and then every this many writes, so a row costs
#: one ``open`` and no ``stat``.
_CHECK_EVERY = 64
_APPEND_LOCK = threading.Lock()
_WRITES: Dict[str, int] = {}
#: What is already failing, so a failure that repeats on every row is said once.
_FAILING: set = set()
#: Logs past twice their ceiling that could not be rotated: looked at on every write until one works.
_BLOCKED: set = set()


def previous_path(path: Any) -> str:
    """``.../calls.jsonl`` -> ``.../calls.1.jsonl``: the one previous generation of a log."""
    folder, name = os.path.split(str(path))
    stem, dot, ext = name.rpartition(".")
    return os.path.join(folder, f"{stem}.1.{ext}" if dot else f"{name}.1")


def generations(path: Any) -> List[str]:
    """The files that hold this log's rows and exist, OLDEST FIRST."""
    return [each for each in (previous_path(path), str(path)) if os.path.isfile(each)]


def failed_once(path: Any, what: str, exc: BaseException) -> None:
    """Say a failure once; it is said again only after the thing worked in between."""
    token = f"{path}:{what}"
    if token in _FAILING:
        return
    _FAILING.add(token)
    logger.warning("kame: %s could not %s (%s: %s)", os.path.basename(str(path)), what,
                   type(exc).__name__, str(exc)[:160])


def _rotate_if_full(path: str, ceiling: int) -> bool:
    """Move a full log to its previous generation. False when it is past twice its ceiling and
    could not be rotated: that row is dropped, the disk stays bounded."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return True              # not there yet
    if size < ceiling:
        return True
    try:
        os.replace(path, previous_path(path))
    except Exception as exc:
        failed_once(path, "rotate", exc)
        return size < 2 * ceiling
    _FAILING.discard(f"{path}:rotate")
    logger.info("kame: %s rotated (%.1f MB); the previous generation is %s",
                os.path.basename(path), size / (1024 * 1024), os.path.basename(previous_path(path)))
    return True


def append_line(path: Any, ceiling: int, line: str) -> bool:
    """Append one line to a KAME log, rotating first when the file has reached ``ceiling``.

    False only when nothing could be written (a failed write, or a log that cannot be rotated and has
    grown to twice its ceiling); the reason was said in the log, once. Never raises.
    """
    path = str(path)
    try:
        with _APPEND_LOCK:
            count = _WRITES.get(path, 0)
            _WRITES[path] = count + 1
            if count % _CHECK_EVERY == 0 or path in _BLOCKED:
                if not _rotate_if_full(path, ceiling):
                    _BLOCKED.add(path)
                    return False
                _BLOCKED.discard(path)
            try:
                with private_append(path) as handle:
                    handle.write(line + "\n")
            except Exception as exc:
                failed_once(path, "write", exc)
                return False
            _FAILING.discard(f"{path}:write")
        return True
    except Exception:  # an instrument must never cost a turn
        logger.debug("kame: could not append to %s", path, exc_info=True)
        return False


def forget_rotation() -> None:
    """Start the counters and the 'already said' memory over. Tests, and a fresh start."""
    with _APPEND_LOCK:
        _WRITES.clear()
        _FAILING.clear()
        _BLOCKED.clear()


def _safe_text(value: Any, limit: int = TEXT_LIMIT) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        try:
            value = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            value = str(value)
    return _redact(str(value))[:limit]


def _response_payload(error: Any) -> str:
    """The whole payload, by the same path 1.7.0.0 learned to use.

    ``core.classify._response_text`` tries ``.text``, ``.content`` and finally
    ``.read()`` — and it is ``.read()`` that returns the ``QuotaFailure`` block
    on a streaming response, which is the only place ``quotaId`` lives. Reading
    twice costs nothing: after the first, httpx serves from memory.
    """
    try:
        from .core.classify import _response_text  # type: ignore[attr-defined]

        return _redact(str(_response_text(error) or ""))
    except Exception:
        return ""


def _destination() -> Optional[str]:
    try:
        from . import state

        folder = state.state_dir()
        if folder is None:
            return None
        folder.mkdir(parents=True, exist_ok=True)
        return str(folder / FILENAME)
    except Exception:
        return None


def record(
    *,
    provider: str = "",
    model: str = "",
    status_code: Optional[int] = None,
    error_message: str = "",
    error_body: Any = None,
    error: Any = None,
    error_type: str = "",
    error_code: str = "",
    **_ignored: Any,
) -> None:
    """Write one refusal down. Fails silently, always."""
    global _silenced
    try:
        if _silenced:
            return
        # The config entry as well as the variable, so it appears in the panel
        # beside every other switch instead of being a secret only the author
        # knows about.
        try:
            from . import settings

            if settings.is_on(settings.RECORDER_DISABLED):
                return
        except Exception:
            if os.environ.get(DISABLE_ENV, "").strip() not in ("", "0"):
                return

        path = _destination()
        if path is None:
            return

        kind = error_type or (type(error).__name__ if error is not None else "")
        body_full = _safe_text(error_body, limit=1_000_000)
        response_full = _response_payload(error)
        row: Dict[str, Any] = {
            "at": round(time.time(), 3),
            "provider": _safe_text(provider, 256),
            "model": _safe_text(model, 256),
            "status": status_code if isinstance(status_code, int) else None,
            "type": _safe_text(kind, 256),
            "code": _safe_text(error_code, 256),
            "message": _safe_text(error_message),
            "body": body_full[:TEXT_LIMIT],
            "response": response_full[:TEXT_LIMIT],
        }
        fields = _quota_fields(body_full, response_full, error_message)
        if fields:
            row["fields"] = fields

        # Decision 0004 D1's consequence: keep the headers, allowlisted and
        # redacted, so the next corpus can actually measure a stated 5xx
        # deadline instead of guessing at it. Added to the row only when
        # something survives the allowlist above — a refusal with no
        # headers, or none worth keeping, writes exactly the row it always
        # did, with no new empty field to explain.
        headers_payload = _safe_headers(_raw_headers(error))
        if headers_payload:
            row["headers"] = headers_payload

        # 1.8.3.0: rotates at the ceiling instead of stopping; a failure is said once.
        append_line(path, CEILING_BYTES, json.dumps(row, ensure_ascii=False))
    except Exception:
        logger.debug("kame: refusal recorder failed; carrying on without it", exc_info=True)
