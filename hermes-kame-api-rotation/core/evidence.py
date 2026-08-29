"""Turning a host exception into the evidence the classifier was built for.

This module exists because of a measurement, not a theory. Nine days of
production telemetry from one real pool (276 recorded blocks, 2026-08-17 to
2026-08-26) said:

    reset_at set ............  0 / 276   (0 %)
    sized_by "dropped" ...... 184 / 276  (67 %)
    source "header" .........  0         (never fired)
    source "pattern" ........ 183

The classifier in :mod:`classify` and the sizing cascade in :mod:`quota` are
careful, layered and evidence-first. They were reading almost nothing. The
binding handed them ``getattr(exc, "message", "")`` — an attribute the host's
own ``GeminiAPIError`` does not define — so the cascade fell through to prose
matching on ``str(exc)`` every single time, and prose is the weakest evidence
there is.

Everything the cascade wanted was on the exception already:

    exc.status_code      int
    exc.code             "gemini_rate_limited" / "gemini_unauthorized" / ...
    exc.retry_after      float, from the Retry-After header
    exc.details          google.rpc.ErrorInfo -> {reason, metadata}
    exc.response         the httpx.Response, body and headers included

So this module is a harvester, not a decider. It does not classify anything.
It reads what is there, guarded, and hands the classifier a full payload.

**Two rules keep it honest.**

*Nothing here may raise.* It runs on the host's error path, where an exception
turns a recoverable API failure into a crash. Every read is guarded
individually, so a hostile property costs one field rather than the harvest.

*Nothing here imports the host.* ``core`` is framework-agnostic on purpose —
the decision rules are the asset and the bindings are disposable. The host's
own text blocks are passed **in** by the binding layer (``host_text.py``),
never imported here.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

#: How much of a remote body is worth keeping. The body is arbitrary JSON from
#: somebody else's server and everything downstream of it is regex work.
_BODY_LIMIT = 20000

#: A trailing block has to be *big* to be worth cutting — a short sentence is
#: as likely to be the provider's own words as the host's. Below this, leave it
#: alone and let the classifier see it.
_MIN_BLOCK_LEN = 24


class Evidence:
    """Everything one failure said about itself, in the shapes it said it.

    ``message`` is the text with the host's own appended guidance removed;
    ``raw_message`` is what actually arrived. The classifier reads the first —
    matching a sentence the *host* wrote is reading our own reflection. The
    second is what a human is shown when they ask what happened.
    """

    __slots__ = (
        "status_code", "message", "raw_message", "body", "body_text",
        "headers", "code", "retry_after", "details", "reason", "notes",
    )

    def __init__(
        self,
        *,
        status_code: Optional[int] = None,
        message: str = "",
        raw_message: str = "",
        body: Any = None,
        body_text: str = "",
        headers: Any = None,
        code: str = "",
        retry_after: Optional[float] = None,
        details: Optional[Dict[str, Any]] = None,
        reason: str = "",
        notes: Optional[List[str]] = None,
    ) -> None:
        self.status_code = status_code
        self.message = message
        self.raw_message = raw_message
        self.body = body
        self.body_text = body_text
        self.headers = headers
        self.code = code
        self.retry_after = retry_after
        self.details = details or {}
        self.reason = reason
        #: Which sources actually produced something. This is the field that
        #: would have made the 67 % visible on day one, so it is carried all
        #: the way to the panel rather than kept for debugging.
        self.notes = notes or []

    def has_structured_evidence(self) -> bool:
        """Did anything beyond prose survive the harvest?

        The honest answer to "does KAME know more than the host here". When
        this is false, a decline is the right verdict and the panel should be
        able to say so.
        """
        return bool(
            self.status_code is not None
            or self.code
            or self.retry_after is not None
            or self.details
            or self.reason
            or self.body
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"Evidence(status={self.status_code!r}, code={self.code!r}, "
            f"retry_after={self.retry_after!r}, reason={self.reason!r}, "
            f"notes={self.notes!r})"
        )


def _safe(obj: Any, name: str) -> Any:
    """Read one attribute, or nothing.

    ``getattr`` with a default does **not** suppress an exception raised by a
    property, and the properties here belong to somebody else's SDK —
    ``httpx.Response.text`` raises ``ResponseNotRead`` on a stream nobody
    consumed, which is precisely the shape that cost a hotfix release.
    """
    try:
        return getattr(obj, name, None)
    except Exception:
        return None


def strip_trailing_blocks(
    message: str, blocks: Sequence[str]
) -> Tuple[str, List[str]]:
    """Remove text the *host* appended to the provider's own message.

    Hermes appends actionable guidance to some provider errors — a free-tier
    429 gains a paragraph explaining the free tier, a legacy-key 401 gains one
    about key types. Useful to a human, and poison to a classifier: the
    free-tier paragraph contains the words *"a few hundred requests/day"*, and
    ``quota._PER_DAY_MARKERS`` matches ``/day``.

    Measured cost of not doing this: **79** log lines reading
    ``daily [429] — resting 1h 0m`` against a pool of fourteen keys, for a
    per-minute throttle that would have cleared in sixty seconds. The whole
    pool went down for an hour, repeatedly, on the host's own helpfulness.

    Matching is by content, not by position: a host may append one block, both,
    or reorder them, and a provider may itself echo a fragment. Only a block
    long enough to be unambiguous is cut, and only where it actually appears.

    Returns the cleaned message and the list of block prefixes removed, so the
    removal is reportable rather than silent.
    """
    if not message:
        return "", []
    cleaned = message
    removed: List[str] = []
    for block in blocks:
        if not block or len(block) < _MIN_BLOCK_LEN:
            continue
        if block in cleaned:
            cleaned = cleaned.replace(block, " ")
            removed.append(block.strip()[:40])
    return cleaned.strip(), removed


def _read_body(error: Any, notes: List[str]) -> Any:
    """Get the response body, whatever shape it survived in.

    Four routes, cheapest and safest first. Every one of them is guarded
    separately: a streaming response that was never read raises on ``.json()``
    *and* on ``.text``, and neither may cost the other three.
    """
    body = _safe(error, "body")
    if body is not None:
        notes.append("body:attr")
        return body

    response = _safe(error, "response")
    if response is None:
        return None

    try:
        parsed = response.json()
    except Exception:
        parsed = None
    if parsed is not None:
        notes.append("body:response.json")
        return parsed

    try:
        text = response.text
    except Exception:
        text = None
    if text:
        notes.append("body:response.text")
        try:
            return json.loads(text)
        except Exception:
            return text[:_BODY_LIMIT]

    # Last resort: bytes already buffered by the transport. ``.content`` raises
    # on an unread stream exactly like ``.text``, so it is guarded the same way
    # and tried last because it is the least likely to be there.
    try:
        content = response.content
    except Exception:
        content = None
    if content:
        notes.append("body:response.content")
        try:
            return json.loads(content)
        except Exception:
            try:
                return content.decode("utf-8", "replace")[:_BODY_LIMIT]
            except Exception:
                return None
    return None


def _read_headers(error: Any, notes: List[str]) -> Any:
    """Headers off the exception, or off its response."""
    headers = _safe(error, "headers")
    if headers is None:
        headers = _safe(error, "response_headers")
    if headers is None:
        response = _safe(error, "response")
        if response is not None:
            headers = _safe(response, "headers")
    if headers is not None:
        notes.append("headers")
    return headers


def _read_status(error: Any, body: Any, notes: List[str]) -> Optional[int]:
    """The HTTP status, from the exception or from the body that carries it.

    The body fallback is not padding. Production logs show NVIDIA failures
    arriving with ``status_code=None`` and the number sitting in the payload —
    fifteen of thirty-eight recorded NVIDIA blocks had no status at all, and a
    classifier that cannot see 429 cannot size a 429.
    """
    for attribute in ("status_code", "status", "http_status"):
        value = _safe(error, attribute)
        if isinstance(value, int) and 100 <= value <= 599:
            notes.append(f"status:{attribute}")
            return value
    response = _safe(error, "response")
    if response is not None:
        value = _safe(response, "status_code")
        if isinstance(value, int) and 100 <= value <= 599:
            notes.append("status:response")
            return value
    if isinstance(body, dict):
        inner = body.get("error")
        for candidate in (body, inner if isinstance(inner, dict) else {}):
            for key in ("status_code", "status", "code"):
                value = candidate.get(key)
                if isinstance(value, int) and 100 <= value <= 599:
                    notes.append("status:body")
                    return value
    return None


def _read_code(error: Any, body: Any, notes: List[str]) -> str:
    """The provider's machine-readable code.

    Only strings. The host's Gemini adapter sets ``gemini_rate_limited`` /
    ``gemini_unauthorized`` / ``gemini_model_not_found``, which is a cleaner
    statement of the failure than any sentence — and it was never read. An
    integer ``code`` is a status code wearing a different key and says nothing
    new, so it is left to :func:`_read_status`.
    """
    value = _safe(error, "code")
    if isinstance(value, str) and value.strip():
        notes.append("code:exception")
        return value.strip()
    if isinstance(body, dict):
        inner = body.get("error")
        if isinstance(inner, dict):
            candidate = inner.get("code") or inner.get("status")
            if isinstance(candidate, str) and candidate.strip():
                notes.append("code:body")
                return candidate.strip()
    return ""


def _read_details(error: Any, body: Any, notes: List[str]) -> Tuple[Dict[str, Any], str]:
    """Structured details, and the ``ErrorInfo.reason`` inside them.

    The host harvests ``google.rpc.ErrorInfo`` into ``exc.details`` and drops
    everything else in the list — including ``google.rpc.RetryInfo``, which is
    the one member carrying ``retryDelay``. That single omission is why
    ``reset_at`` was null on all 276 recorded blocks: ``retry_after`` is
    populated only from a ``Retry-After`` header, and Gemini does not send one.

    So the details are read from the exception *and* the raw body is walked for
    the members the host discarded. :func:`retry_info_seconds` does the second
    half; this returns what the host kept plus the reason it named.
    """
    details: Dict[str, Any] = {}
    value = _safe(error, "details")
    if isinstance(value, dict) and value:
        details = dict(value)
        notes.append("details:exception")
    elif isinstance(value, list) and value:
        details = {"details": list(value)}
        notes.append("details:exception")

    reason = ""
    for candidate in (details.get("reason"), details.get("Reason")):
        if isinstance(candidate, str) and candidate.strip():
            reason = candidate.strip()
            break

    if not reason and isinstance(body, dict):
        inner = body.get("error")
        if isinstance(inner, dict):
            for member in inner.get("details") or []:
                if not isinstance(member, dict):
                    continue
                if str(member.get("@type") or "").endswith("/google.rpc.ErrorInfo"):
                    candidate = member.get("reason")
                    if isinstance(candidate, str) and candidate.strip():
                        reason = candidate.strip()
                        notes.append("reason:body")
                        break
    return details, reason


def retry_info_seconds(body: Any) -> Optional[float]:
    """Pull ``retryDelay`` out of a ``google.rpc.RetryInfo`` member.

    The value is a protobuf Duration rendered as a string — ``"21s"``,
    ``"1.5s"``. Parsed here rather than in :mod:`quota` because it is a shape
    question, not a sizing question; the number goes back into the ordinary
    cascade to be bounded and sanity-checked like any other candidate.
    """
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if not isinstance(error, dict):
        return None
    for member in error.get("details") or []:
        if not isinstance(member, dict):
            continue
        if not str(member.get("@type") or "").endswith("/google.rpc.RetryInfo"):
            continue
        raw = member.get("retryDelay") or member.get("retry_delay")
        if raw is None:
            continue
        if isinstance(raw, (int, float)):
            return float(raw)
        text = str(raw).strip()
        if text.endswith("s"):
            text = text[:-1]
        try:
            return float(text)
        except (TypeError, ValueError):
            continue
    return None


def _read_retry_after(error: Any, body: Any, notes: List[str]) -> Optional[float]:
    """A retry hint, from the exception first and the discarded body second."""
    value = _safe(error, "retry_after")
    if value is not None:
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            seconds = None
        if seconds is not None and seconds > 0:
            notes.append("retry_after:exception")
            return seconds
    seconds = retry_info_seconds(body)
    if seconds is not None and seconds > 0:
        notes.append("retry_after:retryinfo")
        return seconds
    return None


def _flatten(body: Any) -> str:
    if body is None:
        return ""
    if isinstance(body, str):
        return body[:_BODY_LIMIT]
    try:
        return str(body)[:_BODY_LIMIT]
    except Exception:
        return ""


def harvest(
    error: Any,
    *,
    message: str = "",
    guidance_blocks: Sequence[str] = (),
) -> Evidence:
    """Read one exception for everything it is willing to say.

    ``message`` may be passed by a caller that already has the text; when it is
    empty the exception's own string is used. Note that ``getattr(exc,
    "message", "")`` is **not** a reliable source — the host's ``GeminiAPIError``
    passes its text to ``Exception.__init__`` and defines no ``message``
    attribute, so reading only that attribute yields the empty string and every
    downstream sizing attempt fails. That is the bug this argument's default
    exists to make impossible to reintroduce.
    """
    notes: List[str] = []

    raw_message = str(message or "")
    if not raw_message and error is not None:
        try:
            raw_message = str(error)
        except Exception:
            raw_message = ""

    body = _read_body(error, notes)
    headers = _read_headers(error, notes)
    status_code = _read_status(error, body, notes)
    code = _read_code(error, body, notes)
    details, reason = _read_details(error, body, notes)
    retry_after = _read_retry_after(error, body, notes)

    clean_message, removed = strip_trailing_blocks(raw_message, guidance_blocks)
    for block in removed:
        notes.append("stripped-host-guidance")

    return Evidence(
        status_code=status_code,
        message=clean_message,
        raw_message=raw_message,
        body=body,
        body_text=_flatten(body),
        headers=headers,
        code=code,
        retry_after=retry_after,
        details=details,
        reason=reason,
        notes=notes,
    )
