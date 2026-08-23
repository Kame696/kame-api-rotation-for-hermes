"""Let the provider field in Settings hold the several keys it can already carry.

The pool half of this plugin has read ``GOOGLE_API_KEY=k1,k2,k3`` as three
credentials since v0.2.5, and ``live_multikey.py`` watches fifteen of them
rotate through Hermes' own loader. All of that begins with the value being
*stored*, and for a desktop user there is exactly one place to store it: the
key field in Settings.

That field does not reach the pool unchanged. Before it saves, the SPA asks
``POST /api/providers/validate``, and the handler sends the field's entire
contents to the provider as one key::

    params["key"] = value

Google answers 400 for a comma-joined string, the handler returns
``{"ok": False, "reachable": True}``, and its own docstring says what the
caller does with that: *"ok=False + reachable=True means the key is bad
(caller should block)"*. So the paste is refused and nothing is written —
the user sees their keys rejected as one invalid key, which is precisely the
shape this plugin exists to make work. The env var without a probe entry
saves the same paste without complaint, so today the field's answer depends
on which of a provider's two variable names the user happened to click.

This binding probes the keys instead of the field. A value that holds one
key is forwarded untouched. A value that holds several is checked key by
key — by calling the host's own handler once per key, so what counts as a
valid credential stays Hermes' rule and never becomes a rule invented here —
and one key the provider accepts is enough to let the paste be saved.

One accepted key, deliberately. A pool of fifteen keys where the twelfth was
revoked last week is a working pool: the host skips a credential it cannot
use, and this plugin retires a dead one. Refusing the whole paste over it
would block a save that is right, to protect the user from a key the pool
already handles. The count is reported either way, so a paste where four of
fifteen were rejected says so instead of passing in silence.

Nothing here touches what gets saved. The wrapper answers the question the
SPA asked and returns; the value the user typed is what the SPA then sends
to ``PUT /api/env``, byte for byte, commas included.

``KAME_FIELD_PROBE_DISABLED=1`` (or ``field_probe_disabled`` in the plugin's
config entry) gives the host's one-key-per-field answer back without giving
up anything else.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import sys
from typing import Any, Callable, Dict, List, Optional, Sequence

from . import settings
from .core import multikey

logger = logging.getLogger(__name__)

_MARK = "__kame_wrapped__"

# The route the desktop asks before it saves a credential.
_PATH = "/api/providers/validate"

# The handler's parameters, by name. They are what the wrapper is called with
# — FastAPI resolves a dependant by keyword — so a release that renames either
# one is a release this binding must decline rather than guess at.
_PARAMS = ("body", "request")

# How many keys are probed, and how many probes are in flight at once. A paste
# is normally under twenty keys and every probe is one cheap read-only call, so
# the cap exists for the paste that is not: two hundred keys must not become
# two hundred simultaneous requests, nor a save the user waits a minute for.
# Past the cap the answer is honest about what it checked.
_MAX_PROBED = 25
_CONCURRENCY = 6


class Incompatible(Exception):
    """The gateway does not present the surface this module needs."""


def _find_route(app: Any) -> Any:
    for route in getattr(app, "routes", None) or ():
        if getattr(route, "path", "") != _PATH:
            continue
        methods = getattr(route, "methods", None) or ()
        if "POST" in methods:
            return route
    return None


def inspect_route(route: Any) -> Callable:
    """Raise ``Incompatible`` unless the route is the handler we expect.

    Checked rather than assumed, for the same reason the pool binding checks
    every method it wraps: the failure this must never have is a wrapper that
    installs onto something else and answers for it.
    """
    if route is None:
        raise Incompatible(f"the gateway has no POST {_PATH}")
    dependant = getattr(route, "dependant", None)
    if dependant is None or not callable(getattr(dependant, "call", None)):
        raise Incompatible("the route exposes no dependant to redirect")
    handler = dependant.call
    if not inspect.iscoroutinefunction(handler):
        raise Incompatible("the validate handler is not async")
    try:
        parameters = inspect.signature(handler).parameters
    except (TypeError, ValueError) as exc:
        raise Incompatible("cannot inspect the validate handler") from exc
    for required in _PARAMS:
        if required not in parameters:
            raise Incompatible(f"the validate handler is missing {required}")
    return handler


def _with_value(body: Any, value: str) -> Any:
    """A copy of the request body carrying one key instead of the list.

    Pydantic v2 names this ``model_copy`` and v1 named it ``copy``; both are
    tried before giving up, and giving up means the caller forwards the
    original request rather than inventing a body of its own.
    """
    for name in ("model_copy", "copy"):
        copier = getattr(body, name, None)
        if callable(copier):
            try:
                return copier(update={"value": value})
            except Exception:
                continue
    raise Incompatible("the request body cannot be copied")


def _describe(accepted: int, rejected: int, probed: int, total: int) -> str:
    checked = (
        f"{probed} of {total} keys"
        if probed < total
        else f"{total} keys"
    )
    if rejected:
        return f"{accepted} of {checked} accepted, {rejected} rejected."
    return f"All {checked} accepted."


def _summarise(
    results: Sequence[Any], *, probed: int, total: int
) -> Optional[Dict[str, Any]]:
    """Fold the per-key answers into the one answer the SPA reads.

    Returns ``None`` when no probe produced a usable answer at all, which
    tells the caller to fall back to the host's own single call — an empty
    verdict invented here would be a third possibility the SPA has no
    handling for.
    """
    answers = [r for r in results if isinstance(r, dict)]
    if not answers:
        return None

    accepted = [r for r in answers if r.get("ok") and r.get("reachable")]
    reachable = [r for r in answers if r.get("reachable")]

    if not reachable:
        # No probe is configured for this variable, or the network is down.
        # Either way the host's answer for a single key is the right answer
        # for the list, and it is not this binding's place to sharpen it.
        return dict(answers[0])

    if accepted:
        return {
            "ok": True,
            "reachable": True,
            "message": _describe(len(accepted), len(reachable) - len(accepted), probed, total),
        }

    detail = next((str(r.get("message") or "") for r in reachable if r.get("message")), "")
    checked = f"the first {probed} of {total}" if probed < total else f"all {total}"
    return {
        "ok": False,
        "reachable": True,
        "message": f"None of {checked} keys in this field were accepted. {detail}".strip(),
    }


class FieldBinding:
    """Installs, owns, and can fully remove the per-key probe."""

    def __init__(self) -> None:
        self._route: Any = None
        self._original: Optional[Callable] = None
        self.installed = False
        self.reason = "not installed"

    # -- lifecycle -------------------------------------------------------

    def install(self, app: Any) -> bool:
        if self.installed:
            return True
        route = _find_route(app)
        try:
            original = inspect_route(route)
        except Incompatible as exc:
            self.reason = str(exc)
            logger.info("kame: the settings field still takes one key — %s", self.reason)
            return False

        if getattr(original, _MARK, False):
            self.reason = "already wrapped by another KAME instance"
            logger.debug("kame: %s", self.reason)
            return False

        self._route = route
        self._original = original
        wrapper = self._wrap(original)
        route.dependant.call = wrapper
        # Kept in step so anything reading the route for documentation or for
        # a second wrap sees the same function the request will reach.
        try:
            route.endpoint = wrapper
        except Exception:  # pragma: no cover — a frozen route object
            logger.debug("kame: could not restamp route.endpoint", exc_info=True)

        self.installed = True
        self.reason = "active"
        logger.info("kame: the settings field accepts several keys")
        return True

    def uninstall(self) -> None:
        if not self.installed or self._route is None or self._original is None:
            return
        if getattr(self._route.dependant.call, _MARK, False):
            self._route.dependant.call = self._original
            try:
                self._route.endpoint = self._original
            except Exception:  # pragma: no cover
                logger.debug("kame: could not restore route.endpoint", exc_info=True)
        self._route = None
        self._original = None
        self.installed = False
        self.reason = "uninstalled"

    # -- the wrapper -----------------------------------------------------

    def _wrap(self, original: Callable) -> Callable:
        binding = self

        @functools.wraps(original)
        async def _kame_validate(body, request):
            try:
                keys, _ = multikey.split_value(getattr(body, "value", ""))
            except Exception:
                logger.debug("kame: could not read the pasted value", exc_info=True)
                keys = []
            # One key, or none, is the overwhelmingly common case and must
            # cost nothing: same call, same answer, same timing.
            if len(keys) < 2:
                return await original(body=body, request=request)
            try:
                return await binding._probe_each(original, body, request, keys)
            except Exception:
                # A fault in the fan-out must never be what refuses a paste.
                logger.warning(
                    "kame: per-key probe failed, falling back to the host's own",
                    exc_info=True,
                )
                return await original(body=body, request=request)

        setattr(_kame_validate, _MARK, True)
        return _kame_validate

    async def _probe_each(
        self, original: Callable, body: Any, request: Any, keys: List[str]
    ) -> Dict[str, Any]:
        probed = keys[:_MAX_PROBED]
        gate = asyncio.Semaphore(_CONCURRENCY)

        async def one(key: str) -> Any:
            async with gate:
                try:
                    return await original(body=_with_value(body, key), request=request)
                except Exception:
                    # One key's probe failing is not the paste's verdict; the
                    # summary is taken from the answers that did arrive.
                    logger.debug("kame: a per-key probe raised", exc_info=True)
                    return None

        results = await asyncio.gather(*(one(key) for key in probed))
        verdict = _summarise(results, probed=len(probed), total=len(keys))
        if verdict is None:
            return await original(body=body, request=request)
        return verdict


def install(app: Optional[Any] = None) -> Optional[FieldBinding]:
    """Convenience entry point used by ``register()``; never raises.

    Finds the app in ``sys.modules`` rather than importing the gateway. A
    plugin also registers inside the CLI and the TUI, where no web server is
    running and importing one would be a cost paid by every session for a
    surface none of them has.
    """
    if settings.is_on(settings.ROTATION_DISABLED) or settings.is_on(
        settings.FIELD_PROBE_DISABLED
    ):
        return None
    try:
        if app is None:
            module = sys.modules.get("hermes_cli.web_server")
            if module is None:
                logger.debug("kame: no gateway in this process; settings field untouched")
                return None
            app = getattr(module, "app", None)
        binding = FieldBinding()
        return binding if binding.install(app) else None
    except Exception:
        logger.warning("kame: the settings field still takes one key", exc_info=True)
        return None
