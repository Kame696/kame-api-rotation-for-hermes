"""Making an error safe to keep, so it can be kept.

1.1.1 built the Events screen and refused to store any provider error text in
it, for a reason that was exactly right:

    a provider can quote the request back inside an error, and the request can
    be the user's prompt.

So the screen showed a short reason and a status code, and nothing else. Then a
person watching fourteen keys rotate had no way to check *why* — whether a key
was benched for an hour on evidence or on a guess — and 1.2.9 added the raw
error back under a click, silently, without ever meeting the rule it was
reversing.

Both moves were half right. The rule protects something real; so does being
able to see what the provider actually said. What was missing is the third
option: **redact before storing, not before showing.**

That ordering is the whole module. A payload is scrubbed on the way *into* the
event store, so the secret is not in the file, not in a screenshot of the
panel, not in a support bundle, and not in an artifact somebody pastes into a
chat. Redacting at display time would leave it in all four.

What is removed:

* **Credentials**, by shape rather than by vendor prefix — a long unbroken
  token is a secret whoever minted it. The known prefixes are matched too,
  because they also appear *shortened* in provider messages
  (``sk-fake-***0000``) where the shape rule alone would miss them.
* **Anything the request carried back**, by bounding what is kept at all. A
  provider that echoes the prompt echoes it at length; the first few hundred
  characters of an error are the error, and the rest is the echo.

What is deliberately kept: status codes, error types, quota identifiers, retry
delays, and the provider's own sentence. Those are the evidence, and a screen
that hides the evidence to protect the secret protects nothing — the secret was
never in them.
"""

from __future__ import annotations

import re
from typing import Any

#: Everything past this is not the error any more. Provider errors that echo a
#: prompt do it after saying what went wrong, so the cut keeps the diagnosis
#: and drops the transcript.
DEFAULT_LIMIT = 600

#: Vendor prefixes, including the truncated forms that appear inside error
#: messages. Matched first so that a partially-redacted key the provider
#: already starred out is still removed rather than left half-visible.
_PREFIXED = re.compile(
    r"\b("
    r"AIzaSy[A-Za-z0-9_\-]{4,}"          # Google
    r"|sk-[A-Za-z0-9_\-*]{4,}"           # OpenAI, Moonshot, DeepSeek, ZenMux…
    r"|nvapi-[A-Za-z0-9_\-*]{4,}"        # NVIDIA
    r"|gsk_[A-Za-z0-9_\-*]{4,}"          # Groq
    r"|xai-[A-Za-z0-9_\-*]{4,}"          # xAI
    r"|hf_[A-Za-z0-9_\-*]{4,}"           # HuggingFace
    r"|glpat-[A-Za-z0-9_\-*]{4,}"
    r"|Bearer\s+[A-Za-z0-9._\-]{8,}"
    r")",
    re.I,
)

#: The shape rule: a long unbroken run **that contains a digit**.
#:
#: The length alone is not enough, and the first draft of this module proved
#: it by eating the single most valuable field a Gemini 429 carries:
#: ``GenerateRequestsPerMinutePerProjectPerModel-FreeTier`` is 51 unbroken
#: characters, and it is the string that says whether a key is throttled for a
#: minute or spent for the day. A redactor that removes the evidence to protect
#: the secret has protected nothing and cost everything.
#:
#: The digit is what separates them. Provider quota identifiers, model names
#: and Google's canonical status strings are words — ``PerMinute``,
#: ``RESOURCE_EXHAUSTED``, ``FreeTier`` — while credentials are encodings, and
#: base64, hex and every vendor's key format carry digits by construction. A
#: purely alphabetic secret would slip past this rule, which is why it is the
#: third rule and not the only one: the named-field rule catches it by where it
#: sits, and the vendor-prefix rule by what it starts with.
_LONG_TOKEN = re.compile(r"\b(?=[A-Za-z0-9_\-]*\d)[A-Za-z0-9_\-]{32,}\b")

#: A JSON field whose *name* says it holds a credential, whatever its value
#: looks like. Catches a short or test key that no shape rule would find.
_SECRET_FIELD = re.compile(
    r'("(?:api[_-]?key|apikey|authorization|access[_-]?token|refresh[_-]?token'
    r'|secret|password|token)"\s*:\s*)"[^"]*"',
    re.I,
)

_PLACEHOLDER = "[redacted]"


def redact(text: Any, limit: int = DEFAULT_LIMIT) -> str:
    """Scrub a provider payload of anything that must never be stored.

    Never raises. This runs on the error path, and an exception here would turn
    a recoverable API failure into a crash — which is a strictly worse outcome
    than the leak it was guarding against, because it takes the user's turn
    with it.

    Order matters: the named fields go first (their values may be short),
    then the vendor prefixes (which may sit inside longer strings), then the
    shape rule (which would otherwise have eaten the evidence that the first
    two rules use to find their targets). The bound is applied last, so the
    cut never lands in the middle of a token that was about to be redacted and
    leave its tail behind.
    """
    try:
        if text is None:
            return ""
        raw = text if isinstance(text, str) else str(text)
        raw = _SECRET_FIELD.sub(r'\1"%s"' % _PLACEHOLDER, raw)
        raw = _PREFIXED.sub(_PLACEHOLDER, raw)
        raw = _LONG_TOKEN.sub(_PLACEHOLDER, raw)
        raw = raw.strip()
        if limit and len(raw) > limit:
            return raw[:limit].rstrip() + " …"
        return raw
    except Exception:  # pragma: no cover - a __str__ that raises
        return ""


def looks_redacted(text: str) -> bool:
    """Whether a string still carries something shaped like a credential.

    Used by the tests, and by the tripwire that keeps this honest: a rule that
    is never checked against a real payload is a rule that has already stopped
    working.
    """
    if not text:
        return True
    return not (
        _PREFIXED.search(text)
        or _LONG_TOKEN.search(text)
    )
