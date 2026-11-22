"""KAME's provider profiles: Hermes' own, with KAME's client (1.9.0.5: part of this package).

Until 1.9.0.4 this lived in a second plugin, ``hermes-kame-provider``, because Hermes imports an installed
plugin as a model-provider plugin only when its manifest says ``kind: model-provider``. One package can do
the same through the public ``providers.register_provider()``: ``register(ctx)`` calls :func:`install`, which
forces Hermes' bundled discovery first (so a bundled profile registered later cannot overwrite ours) and
then registers, for every bundled API-key profile, a *new* profile object of a subclass of the bundled
profile's class carrying the same field values. Everything - endpoints, model catalog, auth, request
shaping, error classification - is inherited unchanged. Only the two client factories are added:
``create_client`` (Chat Completions, Responses) and ``create_messages_client`` (Anthropic Messages, asked by
Hermes from the release that offers it on).

* a profile whose bundled ``create_client`` supplies its own client (Gemini's native client) is wrapped by
  ``facade.make_profile_client`` when it speaks an API key, and returned untouched otherwise (OAuth,
  external-process clients);
* otherwise the request goes to ``facade.make_client``;
* if KAME is not loaded for this home (the process-wide profile registry is shared by every profile home a
  gateway serves) or cannot build a client, the answer is exactly what the bundled profile returns.

The bundled profile objects are never modified, wrapped or rebound. The facade is found through the
``sys.modules`` registry the facade publishes (``facade.publish``), keyed by profile home.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import sys
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: The name the rotation plugin publishes itself under (``facade.REGISTRY_KEY``).
REGISTRY_KEY = "kame_rotation_registry_v1"

#: Profiles whose ``auth_type`` means "one API key per request" - the shape KAME can rotate. OAuth,
#: external-process and SDK-chain profiles keep Hermes' own client.
_ROTATABLE_AUTH = frozenset({"api_key"})

#: The wires KAME's clients speak: Chat Completions and Responses through ``create_client``, Anthropic
#: Messages through ``create_messages_client`` (a Hermes that does not ask the latter builds its own).
_ROTATABLE_WIRES = frozenset({"chat_completions", "codex_responses", "anthropic_messages"})

#: The second package of 1.9.0.4 and older, and what to tell a user who still has it.
OLD_PROVIDER_PLUGIN = "hermes-kame-provider"
OLD_PROVIDER_NOTICE = ("hermes-kame-provider is no longer needed, and while it is installed Gemini is NOT "
                       "rotated (its own Gemini profile takes the place of API Rotation's): remove it with "
                       "`hermes plugins remove hermes-kame-provider`")

#: Set by Hermes in the child process of ``plugins.isolation: host``.
HOST_ISOLATION_ENV = "HERMES_PLUGIN_HOST_PROCESS"
HOST_ISOLATION_NOTICE = ("API Rotation needs `plugins.isolation: in_process`; in `host` mode Hermes keeps its "
                         "own single-key client")

#: What ``/kame``, the panel and the chat line read.
_STATUS: Dict[str, Any] = {"installed": [], "host_isolation": False}


def reset_status() -> None:
    _STATUS["installed"] = []
    _STATUS["host_isolation"] = False


def status() -> Dict[str, Any]:
    """A copy of what ``register_profiles`` did: names installed, and whether host isolation blocked it."""
    return {"installed": list(_STATUS["installed"]), "host_isolation": bool(_STATUS["host_isolation"])}


# --- finding the facade ------------------------------------------------------------------------------


def _home() -> str:
    try:
        from hermes_constants import get_hermes_home

        return str(get_hermes_home())
    except Exception:
        return os.environ.get("HERMES_HOME", "")


def _rotation_plugin() -> Optional[Any]:
    registry = sys.modules.get(REGISTRY_KEY)
    if registry is None:
        return None
    homes = getattr(registry, "homes", {}) or {}
    return homes.get(os.path.normcase(_home()))


def _facade() -> Optional[Any]:
    plugin = _rotation_plugin()
    return getattr(plugin, "facade", None) if plugin is not None else None


# --- the profile class -------------------------------------------------------------------------------


def _kame_profile_class(base: type) -> type:
    def create_client(self, **client_kwargs: Any) -> Any:
        own = base.create_client(self, **client_kwargs)
        facade = _facade()
        if facade is None:
            return own
        if own is not None:
            maker = getattr(facade, "make_profile_client", None)
            if maker is None:
                return own
            try:
                wrapped = maker(self.name, self, base, client_kwargs, own)
            except Exception:
                logger.warning("kame: %s client could not be wrapped; Hermes keeps its own", self.name,
                               exc_info=True)
                return own
            return own if wrapped is None else wrapped
        try:
            return facade.make_client(self.name, client_kwargs, getattr(self, "api_mode", "") or "")
        except Exception:
            logger.warning("kame: %s client could not be built; Hermes builds its own", self.name, exc_info=True)
            return None

    def create_messages_client(self, **client_kwargs: Any) -> Any:
        own_hook = getattr(base, "create_messages_client", None)
        own = own_hook(self, **client_kwargs) if callable(own_hook) else None
        if own is not None:
            return own
        facade = _facade()
        maker = getattr(facade, "make_messages_client", None) if facade is not None else None
        if maker is None:
            return None
        try:
            return maker(self.name, client_kwargs)
        except Exception:
            logger.warning("kame: %s Messages client could not be built; Hermes builds its own", self.name,
                           exc_info=True)
            return None

    return type("Kame" + base.__name__, (base,), {
        "create_client": create_client,
        "create_messages_client": create_messages_client,
        "__doc__": f"{base.__name__} with KAME's rotating client.",
        "__module__": __name__,
    })


def _copy_as(profile: Any, cls: type) -> Any:
    """A new ``cls`` instance carrying ``profile``'s field values."""
    fields = dataclasses.fields(profile)
    init = {f.name: getattr(profile, f.name) for f in fields if f.init}
    copy = cls(**init)
    for f in fields:
        if not f.init:
            object.__setattr__(copy, f.name, getattr(profile, f.name))
    return copy


# --- installing --------------------------------------------------------------------------------------


def install() -> List[str]:
    """Register KAME's copy of every bundled rotatable profile. Returns the names registered. Never raises."""
    try:
        import providers

        # Forces Hermes' bundled discovery: a bundled profile registered later cannot overwrite ours.
        profiles = list(providers.list_providers())
    except Exception:
        logger.debug("kame: provider profiles unavailable", exc_info=True)
        return []
    done: List[str] = []
    for profile in profiles:
        name = getattr(profile, "name", "")
        try:
            if not name:
                continue
            # Already a KAME class, from this home's package, a second home's, or the old provider package.
            if type(profile).__name__.startswith("Kame"):
                continue
            if providers.provider_source(name) != "bundled":
                continue
            if getattr(profile, "auth_type", "") not in _ROTATABLE_AUTH:
                continue
            if (getattr(profile, "api_mode", "") or "chat_completions") not in _ROTATABLE_WIRES:
                continue
            if not dataclasses.is_dataclass(profile):
                continue
            providers.register_provider(_copy_as(profile, _kame_profile_class(type(profile))))
            done.append(name)
        except Exception:
            logger.debug("kame: left %s with Hermes' own profile", name or "?", exc_info=True)
    for name in done:
        if name not in _STATUS["installed"]:
            _STATUS["installed"].append(name)
    logger.debug("kame: %d provider profile(s) now ask KAME for a client", len(done))
    return done


def register_profiles() -> List[str]:
    """What ``register(ctx)`` calls: install the profiles unless Hermes runs plugins in ``host`` isolation."""
    if os.environ.get(HOST_ISOLATION_ENV) == "1":
        _STATUS["host_isolation"] = True
        logger.warning("kame: %s", HOST_ISOLATION_NOTICE)
        return []
    _STATUS["host_isolation"] = False
    return install()


# --- what to tell the user ---------------------------------------------------------------------------


def old_provider_present() -> bool:
    try:
        home = _home()
        return bool(home) and os.path.isdir(os.path.join(home, "plugins", OLD_PROVIDER_PLUGIN))
    except Exception:
        return False


def old_provider_notice() -> str:
    """The "remove the old provider package" sentence, or ``""`` when it is not installed."""
    return OLD_PROVIDER_NOTICE if old_provider_present() else ""


def isolation_notice() -> str:
    return HOST_ISOLATION_NOTICE if _STATUS["host_isolation"] else ""


def problems() -> List[str]:
    """The notices that mean something does not work (shown as a problem, not a tip)."""
    return [text for text in (isolation_notice(), old_provider_notice()) if text]


def notices() -> List[str]:
    """Every sentence the readouts (``/kame``, the panel, the chat line) owe the user about this package."""
    return [text for text in (isolation_notice(), old_provider_notice()) if text]
