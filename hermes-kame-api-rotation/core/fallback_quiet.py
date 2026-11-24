"""No "⚠️ Model fallback" line in the chat for a switch API Rotation itself asked for.

1.9.0.6 (decisions/0037 C4b; owner, 2026-11-23: the notice must not appear in the chat;
the status bar is enough). When every option inside KAME is spent, KAME hands the reply to the next model of the
user's list with ``KamePoolResting``; Hermes then posts its own switch notice into the chat
(``agent/chat_completion_helpers.try_activate_fallback`` -> ``agent/fallback_notice.announce_fallback_switch``), with
KAME's short reason (``KAME: <model> is busy``) inside it. The live line in the status bar, the reply row and the panel
already say the same thing, so that one line is noise.

:func:`install` wraps ``agent.fallback_notice.announce_fallback_switch`` once. A notice whose reason is KAME's own
(``KAME: `` inside it - KAME's hand-over text, never a provider's) is held exactly the way Hermes holds a notice on a
background wake turn (``agent._hidden_fallback_notice``), so Hermes' own bookkeeping stays right: its turn-start
reconciliation re-offers it (held again) and the "✅ Primary model restored" line is skipped for a switch nobody
saw. Every other notice - a real provider failure, Hermes' own switch - is shown as before. Hermes imports the
function inside ``try_activate_fallback`` at call time, so the wrapped module attribute is the one it calls
(pinned by ``tools/host_assumptions.py``). Never raises; any doubt shows the notice.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: KAME's hand-over reasons all start with this (``transport._maybe_hand_over``, decisions/0037 C4).
MARK = "KAME: "
_FLAG = "_kame_fallback_quiet"
_STATE = {"module": None, "original": None}


def is_kames(notice: Any) -> bool:
    try:
        return MARK in str(notice or "")
    except Exception:
        return False


def _wrap(original: Any) -> Any:
    def announce_fallback_switch(agent: Any, notice: str) -> None:
        if is_kames(notice):
            try:
                logger.info("kame: the switch notice is not posted in the chat (the live line says it): %s", notice)
                agent._hidden_fallback_notice = notice
                return None
            except Exception:
                logger.debug("kame: could not hold the switch notice; Hermes shows it", exc_info=True)
        return original(agent, notice)

    setattr(announce_fallback_switch, _FLAG, True)
    announce_fallback_switch.__wrapped__ = original  # type: ignore[attr-defined]
    announce_fallback_switch.__doc__ = getattr(original, "__doc__", None)
    return announce_fallback_switch


def install() -> bool:
    """Wrap Hermes' switch notice once. ``True`` when in place. Never raises.

    Only once Hermes' ``agent`` package is loaded (then the notice module is a light import); never imports the
    agent itself at plugin load."""
    try:
        import sys

        if "agent" not in sys.modules:
            return False
        from agent import fallback_notice  # type: ignore[import-not-found]

        current = getattr(fallback_notice, "announce_fallback_switch", None)
        if current is None:
            return False
        if getattr(current, _FLAG, False):
            return True
        fallback_notice.announce_fallback_switch = _wrap(current)
        _STATE["module"], _STATE["original"] = fallback_notice, current
        return True
    except Exception:
        logger.debug("kame: could not quiet the switch notice", exc_info=True)
        return False


def uninstall() -> None:
    """Put Hermes' own function back. Never raises."""
    try:
        module, original = _STATE["module"], _STATE["original"]
        if module is not None and original is not None \
                and getattr(getattr(module, "announce_fallback_switch", None), _FLAG, False):
            module.announce_fallback_switch = original
        _STATE["module"] = _STATE["original"] = None
    except Exception:
        logger.debug("kame: could not restore the switch notice", exc_info=True)
