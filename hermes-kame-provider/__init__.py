"""KAME's provider profiles: Hermes' own, with KAME's client.

Hermes asks a provider profile for a client before it builds its own
(``ProviderProfile.create_client``). A ``$HERMES_HOME`` plugin that registers a
profile under a bundled name replaces that profile for its home — the
documented way to change a provider without editing Hermes
(``website/docs/developer-guide/model-provider-plugin.md``, "User overrides").

For every bundled API-key provider this package registers a *new* profile
object of a subclass of the bundled profile's class, carrying the same field
values. Everything — endpoints, model catalog, auth, request shaping, error
classification — is inherited unchanged. Only the two client factories are
added, ``create_client`` (Chat Completions, Responses) and
``create_messages_client`` (Anthropic Messages, asked by Hermes from the
release that offers it on):

* a client the bundled profile supplies itself (an external-process or bespoke
  transport) is returned untouched;
* otherwise the request is handed to the ``hermes-kame-api-rotation`` plugin
  loaded for this home, which returns a client that rotates the provider's keys
  per request; and
* if that plugin is not loaded, switched off, or cannot build a client, the
  answer is ``None`` and Hermes builds its own client exactly as it always has.

The bundled profile objects are never modified, wrapped or rebound.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import sys
from typing import Any, Optional

from providers import list_providers, register_provider

logger = logging.getLogger(__name__)

#: The name the rotation plugin publishes itself under (``facade.REGISTRY_KEY``).
REGISTRY_KEY = "kame_rotation_registry_v1"

#: Profiles whose ``auth_type`` means "one API key per request" — the shape the
#: rotation plugin knows how to rotate. OAuth, external-process and SDK-chain
#: profiles keep Hermes' own client.
_ROTATABLE_AUTH = frozenset({"api_key"})

#: The wires KAME's clients speak: Chat Completions and the Responses API
#: through ``create_client``, Anthropic Messages through
#: ``create_messages_client`` (a Hermes that does not ask the latter simply
#: builds its own Messages client, as before).
_ROTATABLE_WIRES = frozenset({"chat_completions", "codex_responses", "anthropic_messages"})


def _rotation_plugin() -> Optional[Any]:
    registry = sys.modules.get(REGISTRY_KEY)
    if registry is None:
        return None
    try:
        from hermes_constants import get_hermes_home

        home = os.path.normcase(str(get_hermes_home()))
    except Exception:
        home = os.path.normcase(os.environ.get("HERMES_HOME", ""))
    homes = getattr(registry, "homes", {}) or {}
    return homes.get(home)


def _facade() -> Optional[Any]:
    plugin = _rotation_plugin()
    return getattr(plugin, "facade", None) if plugin is not None else None


def _kame_profile_class(base: type) -> type:
    def create_client(self, **client_kwargs: Any) -> Any:
        own = base.create_client(self, **client_kwargs)
        if own is not None:
            return own
        facade = _facade()
        if facade is None:
            return None
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


def _bundled_profiles() -> list:
    """The bundled profiles, read from the bundled plugin modules themselves.

    Not through ``list_providers()``: this module is imported *during* Hermes'
    provider discovery, and asking the registry from inside its own scan is
    asking it to scan again. The bundled plugins are already imported by then
    (bundled before user, ``providers._scan``), each holding its profile as a
    module attribute.
    """
    from providers.base import ProviderProfile

    found = {}
    for name, module in list(sys.modules.items()):
        if not name.startswith("plugins.model_providers.") or module is None:
            continue
        for value in list(vars(module).values()):
            if isinstance(value, ProviderProfile) and getattr(value, "name", ""):
                found.setdefault(value.name, value)
    if not found:
        try:
            for value in list_providers():
                found.setdefault(value.name, value)
        except Exception:
            logger.debug("kame: provider list unavailable", exc_info=True)
    return list(found.values())


def _register_all() -> int:
    count = 0
    for profile in _bundled_profiles():
        try:
            if getattr(profile, "auth_type", "") not in _ROTATABLE_AUTH:
                continue
            if (getattr(profile, "api_mode", "") or "chat_completions") not in _ROTATABLE_WIRES:
                continue
            if not dataclasses.is_dataclass(profile):
                continue
            if type(profile).__module__ == __name__:
                continue
            register_provider(_copy_as(profile, _kame_profile_class(type(profile))))
            count += 1
        except Exception:
            logger.debug("kame: left %s with Hermes' own profile", getattr(profile, "name", "?"), exc_info=True)
    return count


_REGISTERED = _register_all()
logger.debug("kame: %d provider profile(s) now ask KAME for a client", _REGISTERED)
