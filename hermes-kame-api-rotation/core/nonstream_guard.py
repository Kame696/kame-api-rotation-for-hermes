"""A non-streaming call that KAME carries is never cut by Hermes' stale watchdog: KAME keeps the same leash itself.

1.9.0.6 (decisions/0037 C19b; owner, 2026-11-23: never "try again", never give up).

Hermes bounds a NON-streaming call by wall clock (``AIAgent._compute_non_stream_stale_timeout``, 90 s base and more for
big contexts). Past it, the worker path aborts the call and raises ``TimeoutError`` without waiting for the answer
(``agent/chat_completion_nonstream.py`` ``_stale_kill`` / ``_await_worker_after_kill``), the inline path does the same
through its timer (``agent/chat_completion_helpers.py`` ``_InlineRequest``), and a streak of five ends every later call
at once (``_check_stale_giveup``). A call that returns once cannot carry a keep-alive, so while KAME waits for a key or a
model it is killed and the reply gives up. That is the opposite of what KAME is for.

So, for a call that runs on a KAME client, the host's budget is moved INTO KAME:

* :func:`install` wraps ``run_agent.AIAgent._compute_non_stream_stale_timeout`` once. When :func:`_applies`, the wrapper
  records the host's own budget in a ContextVar and returns ``inf``: Hermes' direct path then arms no timer
  (``math.isfinite``) and adds no socket timeout, and its worker path never sees ``elapsed > inf``. Hermes copies the
  caller's context into its worker thread (``_context_thread_target``), so the recorded value reaches the request.
* KAME applies that same budget to every provider request of the call (``transport.attempt_read_timeout``): a hung
  socket is still cut as fast as before, but the cut is KAME's own timeout, which rests the key and asks again
  (decisions/0037 C9) instead of ending the reply.

What it never touches: streaming calls (they already have keep-alives), cron turns (Hermes' own job timeout is the
user's bound there), a stale timeout the user set explicitly (config or ``HERMES_API_CALL_STALE_TIMEOUT``: the user's
deadline stands), Codex Responses, and any agent whose client is not KAME's. Every function never raises; any doubt
returns Hermes' own value unchanged.
"""
from __future__ import annotations

import contextvars
import logging
import math
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: The host's own non-stream budget for the call being prepared on this context, or ``None``.
_BUDGET: "contextvars.ContextVar[Optional[float]]" = contextvars.ContextVar("kame_nonstream_budget", default=None)

#: Attribute set on the wrapper, so a second ``install`` (a second home, a reload) never wraps twice.
_MARK = "_kame_nonstream_guard"

_STATE = {"cls": None, "original": None}


def host_budget() -> Optional[float]:
    """The budget Hermes would have given the current non-streaming call, or ``None``."""
    try:
        value = _BUDGET.get()
        return float(value) if value is not None and math.isfinite(value) and value > 0 else None
    except Exception:
        return None


def _is_kame_client(client: Any) -> bool:
    try:
        from .. import facade

        return isinstance(client, facade._KameMixin)
    except Exception:
        return False


def _applies(agent: Any) -> bool:
    """Is this agent's next non-streaming call one KAME carries and may keep alive itself?"""
    try:
        from .. import facade

        if not facade.enabled():
            return False
        if str(getattr(agent, "api_mode", "") or "") != "chat_completions":
            return False
        if str(getattr(agent, "platform", "") or "") == "cron":
            return False
        explicit = getattr(agent, "_stale_timeout_is_explicit", None)
        if callable(explicit) and explicit():
            return False
        return _is_kame_client(getattr(agent, "client", None))
    except Exception:
        return False


def _wrap(original: Any) -> Any:
    def _compute_non_stream_stale_timeout(self: Any, api_payload: Any) -> float:
        value = original(self, api_payload)
        try:
            if _applies(self) and isinstance(value, (int, float)) and not isinstance(value, bool) \
                    and math.isfinite(float(value)) and float(value) > 0:
                _BUDGET.set(float(value))
                return float("inf")
            _BUDGET.set(None)
        except Exception:
            logger.debug("kame: the non-stream guard left Hermes' budget as it was", exc_info=True)
        return value

    setattr(_compute_non_stream_stale_timeout, _MARK, True)
    _compute_non_stream_stale_timeout.__wrapped__ = original  # type: ignore[attr-defined]
    _compute_non_stream_stale_timeout.__doc__ = getattr(original, "__doc__", None)
    return _compute_non_stream_stale_timeout


def install() -> bool:
    """Wrap Hermes' non-stream budget once. ``True`` when the guard is in place. Never raises.

    Never IMPORTS Hermes' agent module (about a minute on a busy machine; a plugin must not pay that at load): it
    wraps the module Hermes already loaded, and is called again at every turn start (``pre_llm_call``) until it is."""
    try:
        import sys

        run_agent = sys.modules.get("run_agent")
        if run_agent is None:
            return False
        cls = getattr(run_agent, "AIAgent", None)
        current = getattr(cls, "_compute_non_stream_stale_timeout", None) if cls is not None else None
        if current is None:
            logger.info("kame: this Hermes has no non-stream stale budget to carry - nothing to do")
            return False
        if getattr(current, _MARK, False):
            return True
        cls._compute_non_stream_stale_timeout = _wrap(current)
        _STATE["cls"], _STATE["original"] = cls, current
        return True
    except Exception:
        logger.debug("kame: could not install the non-stream guard", exc_info=True)
        return False


def uninstall() -> None:
    """Put Hermes' own method back (plugin unload, tests). Never raises."""
    try:
        cls, original = _STATE["cls"], _STATE["original"]
        if cls is not None and original is not None \
                and getattr(getattr(cls, "_compute_non_stream_stale_timeout", None), _MARK, False):
            cls._compute_non_stream_stale_timeout = original
        _STATE["cls"] = _STATE["original"] = None
    except Exception:
        logger.debug("kame: could not remove the non-stream guard", exc_info=True)
