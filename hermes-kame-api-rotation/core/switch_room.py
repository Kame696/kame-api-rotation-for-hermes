"""While KAME carries a reply, Hermes has room to switch to every model of the user's list.

1.9.0.6 (owner's live test, 2026-11-23: no dependency on Hermes settings; 1.9.0.5 only reached a long list because
the owner had set ``agent.api_max_retries: 5`` by hand).

Hermes ends a turn once its rebuilt-message restarts pass ``agent._api_max_retries`` (``agent.api_max_retries``,
3 by default; ``agent/agent_init.py`` sets the attribute, ``agent/conversation_loop.py`` reads it before every API
call). Every fallback switch is such a restart, so with the default a list of five models could never reach its
last ones, and KAME's switch budget (``core.switch_budget``, never the last switch) kept the reply on the third.

:func:`install` puts a property named ``_api_max_retries`` on ``run_agent.AIAgent``. Hermes' own assignment still
lands in the instance (the setter writes the same ``__dict__`` slot, so :func:`uninstall` leaves every agent exactly
as Hermes made it). The getter returns Hermes' value, or - while API Rotation is on, outside cron - at
least :func:`room`: one switch per model of the list, plus the one KAME never spends. A value the user wrote larger
stands. Nothing is written to any file; any doubt returns Hermes' own value. Never raises.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

_ATTR = "_api_max_retries"
_MARK = "_kame_switch_room"
_STATE = {"cls": None, "had": False, "original": None}


def room() -> int:
    """The switch limit KAME asks for: every model of the user's list (main model included) plus one."""
    try:
        from . import fallback_chain

        primary, chain = fallback_chain.read()
        if not chain:
            return 0
        return len(([primary] if primary else []) + list(chain)) + 1
    except Exception:
        return 0


def active() -> bool:
    return _STATE["cls"] is not None


def effective_limit() -> int:
    """Hermes' switch limit as KAME's own budget must count it: the configured one, raised to :func:`room` while
    the property is in place."""
    try:
        from . import fallback_chain

        limit = int(fallback_chain.switch_limit())
        return max(limit, room()) if active() else limit
    except Exception:
        return 1        # unknown: the safe direction, the budget then hands nothing over (as before)


def _applies(agent: Any) -> bool:
    """API Rotation is on and this is not a cron job. Not tied to the agent's CURRENT client: Hermes reads the limit
    before every call of the turn, and a hop to a model API Rotation does not carry (another provider of the list)
    must not drop the limit back to 3 in the middle of the walk the room was given for."""
    try:
        from .. import facade

        if not facade.enabled():
            return False
        return str(getattr(agent, "platform", "") or "") != "cron"
    except Exception:
        return False


def _get(self: Any) -> Any:
    value = self.__dict__.get(_ATTR, 3)
    try:
        if isinstance(value, int) and not isinstance(value, bool) and _applies(self):
            wanted = room()
            if wanted > value:
                return wanted
    except Exception:
        logger.debug("kame: the switch room left Hermes' limit as it was", exc_info=True)
    return value


def _set(self: Any, value: Any) -> None:
    self.__dict__[_ATTR] = value


def install() -> bool:
    """Put the property in place once. ``True`` when it is. Never imports Hermes' agent module."""
    try:
        import sys

        run_agent = sys.modules.get("run_agent")
        cls = getattr(run_agent, "AIAgent", None) if run_agent is not None else None
        if cls is None:
            return False
        current = cls.__dict__.get(_ATTR)
        if getattr(current, _MARK, False):
            return True
        prop = property(_get, _set)
        try:
            setattr(prop, _MARK, True)
        except AttributeError:
            # a property takes no attributes on some Pythons: mark it through a subclass
            class _Room(property):
                pass
            prop = _Room(_get, _set)
            setattr(prop, _MARK, True)
        _STATE["had"], _STATE["original"] = _ATTR in cls.__dict__, current
        setattr(cls, _ATTR, prop)
        _STATE["cls"] = cls
        return True
    except Exception:
        logger.debug("kame: could not give Hermes room to switch", exc_info=True)
        return False


def uninstall() -> None:
    """Take the property off (plugin unload, tests). Instances keep Hermes' own values. Never raises."""
    try:
        cls = _STATE["cls"]
        if cls is not None and getattr(cls.__dict__.get(_ATTR), _MARK, False):
            if _STATE["had"]:
                setattr(cls, _ATTR, _STATE["original"])
            else:
                delattr(cls, _ATTR)
        _STATE["cls"] = None
        _STATE["had"], _STATE["original"] = False, None
    except Exception:
        logger.debug("kame: could not take the switch room off", exc_info=True)
