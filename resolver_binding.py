"""The first key of a turn, taken from the pool instead of from the raw variable.

The pool binding has read ``GOOGLE_API_KEY=k1,k2,k3`` as three credentials
since v0.2.5, and the field binding lets that value be typed into Settings
without being refused. Both were true and the user still saw *API inválida*
on the very first message, because nothing that decides the key a request
actually carries ever consults the pool.

Hermes resolves an API-key provider like this
(``hermes_cli/auth.py:687``, reached from
``resolve_api_key_provider_credentials`` and from
``runtime_provider.resolve_runtime_provider``)::

    for env_var in pconfig.api_key_env_vars:
        val = (get_env_value_prefer_dotenv(env_var) or "").strip()
        if has_usable_secret(val):
            return val, env_var

The whole variable is the key. The credential pool is consulted only as a
*fallback*, when no env var holds anything at all — so for the one user who
has fifteen keys in the variable, the pool is never asked. Worse, the runtime
dict that branch returns (``runtime_provider.py:2285``) carries no
``credential_pool`` key at all, and that dict is the only place the agent
gets one from (``agent/agent_init.py:669``). So an API-key provider gets:

* the comma-joined list sent as one Bearer token, which every provider
  refuses — Google answers ``400 INVALID_ARGUMENT: API key not valid``, which
  Hermes classifies ``non_retryable_client_error``; and
* no pool on the agent, so there is nothing to rotate to even if it had been
  classified as a quota failure.

Measured on the live gateway before this module existed: the process
environment held a 613-character, 14-comma ``GOOGLE_API_KEY``; ``load_pool``
with this plugin installed built **fifteen** usable credentials from it; and
the request dump for the same minute shows the whole 613 characters going out
as one ``Authorization: Bearer`` header.

This binding wraps ``resolve_runtime_provider`` and repairs its answer, and
only its answer:

* a resolved ``api_key`` that does not split is returned untouched — the
  overwhelmingly common case, which must cost one function call and nothing
  else;
* one that does split is replaced by a single key **the pool selected**, and
  that same pool is attached to the runtime so rotation has somewhere to go;
* if there is no usable pool — no ``auth.json`` row, every key benched, a
  build whose pool this cannot read — the first key of the list is used
  anyway. That is still a credential the provider can accept, where the list
  never was.

It never widens what the host would send. The key it substitutes always comes
from the value the host itself resolved, or from a pool entry derived from it.
A provider with one key, an OAuth provider, a pool-backed provider that
already carries its own ``credential_pool``: all are returned exactly as the
host built them.

``KAME_RESOLVER_DISABLED=1`` (or ``resolver_disabled`` in the plugin's config
entry) puts the host's own resolution back.
"""

from __future__ import annotations

import functools
import logging
import sys
from typing import Any, Callable, List, Optional, Tuple

from . import settings
from .core import multikey

logger = logging.getLogger(__name__)

_MARK = "__kame_wrapped__"

# The single function every caller in the host goes through to learn which
# provider, which base URL and which key a turn will use. Wrapping it is what
# makes one patch reach the CLI, the gateway, the TUI, cron jobs, the
# auxiliary lane and the fallback chain alike — every one of them imports it
# from this module inside the function that calls it.
_FUNCTION = "resolve_runtime_provider"

_MODULE = "hermes_cli.runtime_provider"


class Incompatible(Exception):
    """The installed Hermes does not present the surface this module needs."""


def inspect_module(module: Any) -> Callable:
    """Raise ``Incompatible`` unless the resolver is the one we expect.

    Shape only, deliberately — same rule as the other bindings. What is
    checked is that the name exists and is callable; what is *not* assumed is
    anything about the dict it returns, which is why every read of that dict
    below is guarded and every failure falls back to handing it over unchanged.
    """
    if module is None:
        raise Incompatible(f"{_MODULE} is not imported in this process")
    resolver = getattr(module, _FUNCTION, None)
    if not callable(resolver):
        raise Incompatible(f"{_MODULE} has no {_FUNCTION}()")
    return resolver


def _key_of(entry: Any) -> str:
    """The key an entry would actually send, by whichever field carries it."""
    try:
        value = getattr(entry, "runtime_api_key", None) or getattr(entry, "access_token", "")
    except Exception:
        return ""
    return str(value or "").strip()


def choose(keys: List[str], candidate: str) -> Tuple[str, bool]:
    """Which key this turn should carry, and whether the pool chose it.

    Pure, so the rule can be read on its own: a candidate the pool offered is
    used when it is a single usable key, and the first key of the list is used
    when it is not. ``keys`` is never empty here — the caller only reaches
    this once the value split into two or more.

    A candidate that itself splits is refused. That is the parent row of a
    split — the credential whose value *is* the list — and handing it back
    would reintroduce the exact bug this module exists to remove. It happens
    on any host where the persist guard could not install, so it is a real
    branch and not a theoretical one.
    """
    text = str(candidate or "").strip()
    if text and not multikey.split_value(text)[0]:
        return text, True
    return keys[0], False


class ResolverBinding:
    """Installs, owns, and can fully remove the repair."""

    def __init__(self) -> None:
        self._module: Any = None
        self._original: Optional[Callable] = None
        self.installed = False
        self.reason = "not installed"
        # Counters for ``/kame-quota``: an install that has never seen a
        # multi-key variable and one that is silently failing to repair it
        # look identical from every other angle.
        self.repaired = 0
        self.pooled = 0

    # -- lifecycle -------------------------------------------------------

    def install(self, module: Any) -> bool:
        """Wrap the resolver. Never raises; a refusal is a supported outcome."""
        if self.installed:
            return True
        try:
            original = inspect_module(module)
        except Incompatible as exc:
            self.reason = str(exc)
            logger.info("kame: the first key of a turn is the host's — %s", self.reason)
            return False

        if getattr(original, _MARK, False):
            self.reason = "already wrapped by another KAME instance"
            logger.debug("kame: %s", self.reason)
            return False

        self._module = module
        self._original = original
        setattr(module, _FUNCTION, self._wrap(original))
        self.installed = True
        self.reason = "active"
        logger.info("kame: a multi-key variable resolves to one key from the pool")
        return True

    def uninstall(self) -> None:
        if not self.installed or self._module is None or self._original is None:
            return
        if getattr(getattr(self._module, _FUNCTION, None), _MARK, False):
            setattr(self._module, _FUNCTION, self._original)
        self._module = None
        self._original = None
        self.installed = False
        self.reason = "uninstalled"

    # -- the wrapper -----------------------------------------------------

    def _wrap(self, original: Callable) -> Callable:
        binding = self

        @functools.wraps(original)
        def _kame_resolve_runtime_provider(*args, **kwargs):
            # Passed straight through. This resolver has grown parameters
            # across releases (``requested``, ``explicit_api_key``,
            # ``explicit_base_url``, ``target_model``) and a wrapper that
            # named any of them would break on the next one it did not.
            runtime = original(*args, **kwargs)
            try:
                return binding.repair(runtime)
            except Exception:
                # The host's own answer is a working answer for everybody who
                # has one key, which is almost everybody. A fault here must
                # cost the repair and never the turn.
                logger.warning("kame: could not repair the resolved key", exc_info=True)
                return runtime

        setattr(_kame_resolve_runtime_provider, _MARK, True)
        return _kame_resolve_runtime_provider

    # -- the repair ------------------------------------------------------

    def repair(self, runtime: Any) -> Any:
        """Give back the host's runtime, or a copy of it carrying one key."""
        if not isinstance(runtime, dict):
            return runtime
        keys, rejected = multikey.split_value(runtime.get("api_key"))
        if len(keys) < 2:
            return runtime

        provider = str(runtime.get("provider") or "").strip().lower()
        pool = runtime.get("credential_pool")
        if pool is None and provider:
            pool = self._load_pool(provider)

        key, from_pool = choose(keys, self._offer(pool))

        repaired = dict(runtime)
        repaired["api_key"] = key
        if from_pool:
            # Attached only when the key came from it. A pool whose selection
            # was overruled must not then be handed the job of choosing the
            # next one: the agent would rotate to whatever this just refused.
            repaired["credential_pool"] = pool
            self.pooled += 1
        self.repaired += 1

        logger.info(
            "kame: %s resolved a variable holding %d key%s — sending one%s%s",
            provider or "?",
            len(keys),
            "" if len(keys) == 1 else "s",
            " chosen by the pool" if from_pool else " (the pool offered none)",
            f", {rejected} fragment(s) ignored" if rejected else "",
        )
        return repaired

    def _load_pool(self, provider: str) -> Any:
        """The pool for this provider, or ``None`` if there is not one to have.

        The host does this itself for pool-backed providers and skips it
        entirely for API-key ones, which is the gap. Reading it costs one
        ``auth.json`` load on a path that already reads config and ``.env``,
        and only on a turn whose variable actually holds several keys.
        """
        try:
            from agent.credential_pool import load_pool

            pool = load_pool(provider)
        except Exception:
            logger.debug("kame: no credential pool for %s", provider, exc_info=True)
            return None
        try:
            return pool if pool is not None and pool.has_credentials() else None
        except Exception:
            return None

    def _offer(self, pool: Any) -> str:
        """The key the pool would hand out now, or the empty string.

        ``select`` rather than ``peek``: this *is* a credential being handed
        out for a real turn, so it must go through the same path every other
        handout goes through — the one the plugin's own selection mirror and
        load spreading are wrapped around. Peeking would take a key without
        counting it and quietly make the spread lopsided.
        """
        if pool is None:
            return ""
        try:
            entry = pool.select()
        except Exception:
            logger.debug("kame: the pool could not select a credential", exc_info=True)
            return ""
        return _key_of(entry) if entry is not None else ""


def install(module: Optional[Any] = None) -> Optional[ResolverBinding]:
    """Convenience entry point used by ``register()``; never raises.

    Takes the module out of ``sys.modules`` when it is already there and
    imports it when it is not. Importing is right here where it would be
    wrong for the gateway app: every caller of the resolver imports it lazily,
    inside the function that calls it, so at registration time it is often
    absent — and a binding that installs only when somebody happened to have
    resolved a provider first is a binding that works on some starts and not
    others. The module itself is plain: no server, no socket, no I/O at
    import.
    """
    if settings.is_on(settings.ROTATION_DISABLED) or settings.is_on(
        settings.RESOLVER_DISABLED
    ):
        return None
    try:
        if module is None:
            module = sys.modules.get(_MODULE)
        if module is None:
            from importlib import import_module

            module = import_module(_MODULE)
        binding = ResolverBinding()
        return binding if binding.install(module) else None
    except ImportError:
        # Running outside a Hermes install — the doctor, a unit test, a
        # packaging check. Nothing to bind to and nothing to warn about.
        logger.debug("kame: no %s to bind", _MODULE)
        return None
    except Exception:
        logger.warning("kame: the first key of a turn is the host's", exc_info=True)
        return None
