"""Keep the field that says whether a quota is per-minute or per-day.

Google's two free-tier quotas report the **identical** metric name and differ
only in one field::

    "quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"
    "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"

That field decides everything this plugin does with a refusal, and it never
arrives. ``agent.gemini_native_adapter.gemini_http_error`` parses the error
body, walks ``details``, and keeps only the entry whose ``@type`` ends in
``google.rpc.ErrorInfo``. ``google.rpc.QuotaFailure`` — which carries the
``quotaId`` — and ``google.rpc.RetryInfo`` are read past and dropped. What
reaches the exception is a message, a status code, and four fields that do not
include either.

The owner's first session on 1.7.0.0 is the measurement: **300 journal rows,
300 of them reading ``window: unknown``**, on a pool where every single
refusal came from Google. And the cost is not academic. On 2026-09-05 the same
fourteen keys were swept once a minute for five minutes and every one refused
on its *first* request of that minute — which a per-minute ceiling cannot do,
so the window doing the blocking was a longer one. Google sends a
per-minute-shaped ``retryDelay`` on a daily cap regardless; its own forum says
so in as many words. About 65 calls that had no chance, and nothing in the
payload KAME could see to tell the two apart.

**Why a wrapper and not a parser.** The body is not lost — it is read, parsed
and discarded within one function, and on the streaming path the host has
already gone to the trouble of draining the response
(``read_streaming_error_body``) before calling it. Re-deriving the quota
window from the prose is guesswork over text Google may reword; keeping the
structured body it was built from is not. So this wraps the host's own
factory, lets it do exactly what it did, and attaches the parsed body to the
exception it returns.

Nothing is invented and nothing is changed: ``core.evidence`` already looks
for ``error.body`` first, so a body that is present is simply read. When this
binding is not installed — an older Hermes, a rewritten adapter, a provider
that is not Gemini — every path behaves exactly as it did in 1.7.0.1.

``KAME_QUOTA_ID_DISABLED=1`` (or ``quota_id_disabled`` in the plugin's config
entry) takes it back out.
"""

from __future__ import annotations

import functools
import json
import logging
from typing import Any, Optional

from . import settings

logger = logging.getLogger(__name__)

#: The host module and the factory inside it. Named here rather than reached
#: for inline so ``tools/host_assumptions.py`` can assert both still exist and
#: fail loudly the day they move, instead of this binding going quiet.
HOST_MODULE = "agent.gemini_native_adapter"
HOST_FACTORY = "gemini_http_error"

#: Set on the wrapper so a second install is a no-op rather than a second
#: layer. Hermes can import a plugin more than once per process.
_MARK = "_kame_keeps_the_quota_id"

#: Where the parsed body is left. ``core.evidence._read_body`` reads this
#: attribute before it tries anything else, so the name is not a choice.
BODY_ATTRIBUTE = "body"

_installed: Optional[Any] = None


def _parsed_body(response: Any, body_text: Optional[str]) -> Optional[dict]:
    """The error body as a dict, by the same route the host took to build it.

    ``body_text`` when the caller passed one — the streaming path drains the
    response itself and hands the text over, and asking a drained stream for
    its text again is how a plugin turns a rate limit into a crash. Otherwise
    ``response.text``, which httpx has already materialised and cached by the
    time the host's own parse has run.
    """
    text = body_text
    if text is None:
        try:
            text = response.text
        except Exception:
            return None
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def install() -> bool:
    """Wrap the host's error factory. ``False`` when there is nothing to wrap.

    ``False`` is not a failure. The CLI without the native adapter, a Hermes
    that renamed the function, a machine with no Gemini configured — all of
    them are installs where this changes nothing and should say so rather than
    warn about a host that is simply different.
    """
    global _installed
    if settings.is_on(settings.QUOTA_ID_DISABLED):
        return False
    try:
        from importlib import import_module

        module = import_module(HOST_MODULE)
    except Exception:
        return False

    original = getattr(module, HOST_FACTORY, None)
    if not callable(original):
        return False
    if getattr(original, _MARK, False):
        return True

    @functools.wraps(original)
    def _kame_gemini_http_error(response: Any, *args: Any, **kwargs: Any) -> Any:
        error = original(response, *args, **kwargs)
        # Everything below is best-effort and wrapped as one: this runs on the
        # error path of every Gemini failure, and a plugin that turns a 429
        # into a TypeError has done more damage than the misclassification it
        # was trying to prevent.
        try:
            body = _parsed_body(response, kwargs.get("body_text"))
            if body is not None and getattr(error, BODY_ATTRIBUTE, None) is None:
                setattr(error, BODY_ATTRIBUTE, body)
        except Exception:
            logger.debug("kame: could not keep the error body", exc_info=True)
        return error

    setattr(_kame_gemini_http_error, _MARK, True)
    setattr(module, HOST_FACTORY, _kame_gemini_http_error)
    _installed = module
    return True


def uninstall() -> None:
    """Put the host's own factory back. Tests, and an orderly shutdown."""
    global _installed
    module = _installed
    _installed = None
    if module is None:
        return
    try:
        wrapper = getattr(module, HOST_FACTORY, None)
        original = getattr(wrapper, "__wrapped__", None)
        if callable(original):
            setattr(module, HOST_FACTORY, original)
    except Exception:
        logger.debug("kame: could not unwrap the error factory", exc_info=True)


def installed() -> bool:
    return _installed is not None
