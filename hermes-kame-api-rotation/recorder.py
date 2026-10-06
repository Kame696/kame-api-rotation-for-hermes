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
* **Never grows without bound.** It stops at the ceiling and says so once, and
  the headers block carries its own, smaller caps on top.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: File ceiling. Past this the recorder goes quiet — a user's disk is no place
#: to grow unattended, and a few thousand refusals are already corpus enough for
#: any question worth asking of them.
CEILING_BYTES = 8 * 1024 * 1024

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


def _safe_text(value: Any, limit: int = 20000) -> str:
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

        return _safe_text(_response_text(error))
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

        try:
            if os.path.getsize(path) >= CEILING_BYTES:
                _silenced = True
                logger.info(
                    "kame: refusal recorder stopped at %s - %d MB ceiling reached",
                    path, CEILING_BYTES // (1024 * 1024),
                )
                return
        except OSError:
            pass  # not created yet

        kind = error_type or (type(error).__name__ if error is not None else "")
        row: Dict[str, Any] = {
            "at": round(time.time(), 3),
            "provider": _safe_text(provider, 256),
            "model": _safe_text(model, 256),
            "status": status_code if isinstance(status_code, int) else None,
            "type": _safe_text(kind, 256),
            "code": _safe_text(error_code, 256),
            "message": _safe_text(error_message),
            "body": _safe_text(error_body),
            "response": _response_payload(error),
        }

        # Decision 0004 D1's consequence: keep the headers, allowlisted and
        # redacted, so the next corpus can actually measure a stated 5xx
        # deadline instead of guessing at it. Added to the row only when
        # something survives the allowlist above — a refusal with no
        # headers, or none worth keeping, writes exactly the row it always
        # did, with no new empty field to explain.
        headers_payload = _safe_headers(_raw_headers(error))
        if headers_payload:
            row["headers"] = headers_payload

        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        logger.debug("kame: refusal recorder failed; carrying on without it", exc_info=True)
