"""Give auxiliary calls the same per-model memory as the main conversation.

Hermes' auxiliary lane — summarisation, titling, compression, vision,
whatever a turn needs besides the answer itself — does not fire hooks. Not
``pre_api_request``, not ``transform_api_error_classification``, none of
them. It has its own error handling in ``auxiliary_client``, which reaches
straight for ``mark_exhausted_and_rotate``.

That matters more than it sounds, because the auxiliary lane is exactly where
the per-model problem bites. It deliberately runs a *smaller* model than the
conversation, on the *same* keys. When the main model exhausts a key, the
auxiliary model has spent nothing on it — and it is the one that gets locked
out, on every key, for as long as the main model's cooldown lasts.

The pool binding already covers the read side here: it wraps a class method,
so the fresh ``load_pool()`` object the auxiliary path builds is wrapped too.
What is missing is the announcement — without it, every auxiliary call looks
like an unannounced one and gets the host's provider-wide answer.

So this module wraps the three functions every auxiliary request passes
through on its way to a provider. Each one already receives both facts:
``provider`` as a keyword, and the model inside the request kwargs. The
wrapper announces them for the duration of the call and restores the previous
announcement afterwards — auxiliary calls nest inside a main turn, and an
announcement that outlived its call would attribute the main model's next
failure to the auxiliary model.

Nothing else is touched. The request is forwarded byte for byte, the return
value is passed straight back, and an exception propagates unchanged with the
announcement already unwound by the ``finally``.
"""

from __future__ import annotations

import functools
import inspect
import logging
import time
from typing import Any, Callable, Optional, Tuple

from . import runtime

logger = logging.getLogger(__name__)

_MARK = "__kame_wrapped__"

# The functions every auxiliary request funnels through. Streaming is included
# because a streamed auxiliary call fails the same way a buffered one does.
_SYNC_RELAYS = ("_relay_sync_completion", "_relay_sync_stream")
_ASYNC_RELAYS = ("_relay_async_completion",)


class Incompatible(Exception):
    """The auxiliary client does not present the surfaces this module needs."""


def _check(module: Any, name: str, *, want_async: bool) -> Callable:
    function = getattr(module, name, None)
    if not callable(function):
        raise Incompatible(f"auxiliary_client has no {name}()")
    if inspect.iscoroutinefunction(function) != want_async:
        raise Incompatible(f"{name} is not {'async' if want_async else 'sync'}")
    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError) as exc:
        raise Incompatible(f"cannot inspect {name}") from exc
    for required in ("kwargs", "provider"):
        if required not in parameters:
            raise Incompatible(f"{name} is missing {required}")
    return function


def inspect_module(module: Any) -> None:
    """Raise ``Incompatible`` unless every relay is present and shaped right."""
    for name in _SYNC_RELAYS:
        _check(module, name, want_async=False)
    for name in _ASYNC_RELAYS:
        _check(module, name, want_async=True)


class AuxBinding:
    """Installs, owns, and can fully remove the auxiliary announcements."""

    def __init__(self) -> None:
        self._module: Any = None
        self._originals: dict = {}
        self.installed = False
        self.reason = "not installed"

    # -- lifecycle -------------------------------------------------------

    def install(self, module: Any) -> bool:
        if self.installed:
            return True
        try:
            inspect_module(module)
        except Incompatible as exc:
            self.reason = str(exc)
            logger.info(
                "kame: auxiliary calls stay provider-wide — %s", self.reason
            )
            return False

        names = _SYNC_RELAYS + _ASYNC_RELAYS
        if any(getattr(getattr(module, name), _MARK, False) for name in names):
            self.reason = "already wrapped by another KAME instance"
            logger.debug("kame: %s", self.reason)
            return False

        self._module = module
        for name in _SYNC_RELAYS:
            original = getattr(module, name)
            self._originals[name] = original
            setattr(module, name, self._wrap_sync(module, original))
        for name in _ASYNC_RELAYS:
            original = getattr(module, name)
            self._originals[name] = original
            setattr(module, name, self._wrap_async(module, original))

        self.installed = True
        self.reason = "active"
        logger.info("kame: auxiliary calls are model-scoped")
        return True

    def uninstall(self) -> None:
        if not self.installed or self._module is None:
            return
        for name, original in self._originals.items():
            if getattr(getattr(self._module, name, None), _MARK, False):
                setattr(self._module, name, original)
        self._originals = {}
        self._module = None
        self.installed = False
        self.reason = "uninstalled"

    # -- wrappers --------------------------------------------------------

    def _wrap_sync(self, module: Any, original: Callable) -> Callable:
        binding = self

        @functools.wraps(original)
        def _kame_relay(client, kwargs, **options):
            provider, model = binding._identify(module, kwargs, options)
            if not model:
                return original(client, kwargs, **options)
            with runtime.scoped_call(provider, model):
                try:
                    return original(client, kwargs, **options)
                except BaseException as exc:
                    binding._read_refusal(provider, model, exc)
                    raise

        setattr(_kame_relay, _MARK, True)
        return _kame_relay

    def _wrap_async(self, module: Any, original: Callable) -> Callable:
        binding = self

        @functools.wraps(original)
        async def _kame_relay_async(client, kwargs, **options):
            provider, model = binding._identify(module, kwargs, options)
            if not model:
                return await original(client, kwargs, **options)
            with runtime.scoped_call(provider, model):
                try:
                    return await original(client, kwargs, **options)
                except BaseException as exc:
                    binding._read_refusal(provider, model, exc)
                    raise

        setattr(_kame_relay_async, _MARK, True)
        return _kame_relay_async

    # -- the failure nobody else reads -----------------------------------

    def _read_refusal(self, provider: str, model: str, exc: BaseException) -> None:
        """Classify an auxiliary failure, because no hook will.

        The main lane's refusals reach ``core.classify`` through
        ``transform_api_error_classification``. This lane fires no hooks at
        all, so its refusals were the only ones the plugin never read — and
        it is the lane that benches most eagerly, reaching straight for
        ``mark_exhausted_and_rotate`` (``agent/auxiliary_client:4560``,
        ``:4572``). Every auxiliary 429 therefore got the host's fixed TTL
        on a key the auxiliary model had barely touched.

        Two facts are left behind, both with the provider match and the
        30-second expiry that govern every hand-off in ``runtime``:

        * the **verdict**, so ``pool_binding`` can carry KAME's deadline into
          the bench the way it now does on the main lane;
        * the **model**, because ``_recover_provider_pool`` runs *after* this
          call has unwound and ``scoped_call`` has already put the
          conversation's model back. Without it the bench earned by a
          titling call lands on the model that answers the user.

        Nothing here may change what the caller sees. The exception is
        re-raised by the caller untouched, and every failure inside this
        function is swallowed: reading a refusal is worth less than the
        refusal itself, which the host still handles exactly as before.
        """
        try:
            from .core import classify as classify_error
            from .core import evidence
            from . import host_text

            ev = evidence.harvest(
                exc,
                message=str(exc),
                guidance_blocks=host_text.guidance_blocks(),
            )
            verdict = classify_error(
                provider=provider,
                model=model,
                status_code=ev.status_code,
                error_message=ev.message,
                error_body=ev.body,
                headers=ev.headers,
                error=exc,
                error_type=type(exc).__name__,
                error_code=str(ev.code or ""),
                now_epoch=time.time(),
            )
        except Exception:
            logger.debug("kame: could not read the auxiliary refusal", exc_info=True)
            return

        now = time.time()
        try:
            # Set even when the verdict is ``None``. The attribution is not
            # KAME's opinion about the failure — it is a fact about which
            # model was on the wire, and it is right whether or not this
            # plugin has anything to say about the refusal itself.
            runtime.note_bench_model(provider, model, now=now)
            if verdict is not None:
                runtime.note_judgement(
                    provider,
                    model,
                    window=verdict.quota_window,
                    source=verdict.source,
                    reset_at=verdict.reset_at,
                    now=now,
                    scope=verdict.quota_scope,
                )
                logger.info(
                    "kame: auxiliary %s/%s -> %s [%s via %s]",
                    provider, model, verdict.reason,
                    verdict.quota_window, verdict.source or "-",
                )
        except Exception:  # pragma: no cover — ContextVar sets do not fail
            logger.debug("kame: could not stage the auxiliary verdict", exc_info=True)

    # -- identification --------------------------------------------------

    def _identify(self, module: Any, kwargs: Any, options: dict) -> Tuple[str, str]:
        """Work out which provider pool and model this request belongs to.

        The provider is normalised through the host's own
        ``_normalize_aux_provider``, because that is what
        ``_recoverable_pool_provider`` uses to pick the pool — announcing the
        raw label would name a scope no pool answers to, and the announcement
        would be discarded as belonging to a different provider.

        Returns ``("", "")`` whenever either fact is missing or ambiguous.
        An auxiliary call that cannot be identified is left exactly as the
        host would handle it.
        """
        try:
            model = str((kwargs or {}).get("model") or "").strip()
        except Exception:
            return "", ""
        if not model:
            return "", ""

        provider = str(options.get("provider") or "").strip()
        if not provider:
            return "", ""

        normalize = getattr(module, "_normalize_aux_provider", None)
        if callable(normalize):
            try:
                provider = str(normalize(provider) or "").strip()
            except Exception:
                return "", ""
        # ``auto`` and ``custom`` are routing labels, not pools: the host
        # resolves them from the client's base_url, which is not visible from
        # here. Guessing would attribute a bench to the wrong provider.
        if provider.lower() in {"", "auto", "custom"}:
            return "", ""
        return provider, model


def install(module: Optional[Any] = None) -> Optional[AuxBinding]:
    """Convenience entry point used by ``register()``; never raises."""
    try:
        if module is None:
            from agent import auxiliary_client as module  # type: ignore
        binding = AuxBinding()
        return binding if binding.install(module) else None
    except ImportError:
        logger.debug("kame: no auxiliary client to bind")
        return None
    except Exception:
        logger.warning("kame: auxiliary calls stay provider-wide", exc_info=True)
        return None
