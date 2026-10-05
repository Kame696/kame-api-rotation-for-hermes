"""The client Hermes is handed when it asks a provider profile for one.

Hermes builds every model client through ``ProviderProfile.create_client`` when
the profile supplies one (``agent_runtime_helpers._provider_supplied_client``,
and the same seam in ``auxiliary_client``). It is the documented way for a
plugin to bring its own transport, and the plugin catalog's rule 9 names
provider profiles as an allowed surface. The companion package
``hermes-kame-provider`` registers a profile per bundled API-key provider whose
``create_client`` lands in :func:`make_client` here.

The client has the shape Hermes already expects from that provider — a
``GeminiNativeClient`` for Google's native endpoint, an ``openai.OpenAI`` for
everything else — so every ``isinstance`` and attribute Hermes reads keeps
working. Only ``chat.completions.create`` is KAME's: it runs the request
through :class:`transport.KameTransport`, which chooses the key, reads every
refusal and moves to the next key. Each key gets its own inner client, built
the way Hermes builds one, and they share one keep-alive connection pool.

Nothing in Hermes is replaced or wrapped. If anything here cannot be built,
:func:`make_client` returns ``None`` and Hermes builds its own client exactly
as it would have without KAME.
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from . import settings
from .core import multikey
from .core.carousel import Carousel
from .transport import Call, KameTransport, attempt_read_timeout

logger = logging.getLogger(__name__)

#: One transport per plugin namespace — and so per Hermes profile home. The
#: carousel engine inside it is process-wide on purpose (``core.carousel.
#: ENGINE``): the owner's profiles share the same physical keys, and a key one
#: profile just saw refused is refused for the others too.
TRANSPORT = KameTransport(jitter=lambda: random.uniform(0.1, 1.5))

#: How long a client trusts the key list it read before asking the pool again.
#: A request-scoped client lives for one turn or a few; this only bounds how
#: stale its list can get inside a long one.
KEYS_TTL_S = 30.0


def enabled() -> bool:
    """Whether KAME should supply clients at all. The escape hatches win."""
    return not (settings.is_on(settings.ROTATION_DISABLED) or settings.is_on(settings.CAROUSEL_DISABLED))


def _pool_name(provider: str, base_url: str = "") -> str:
    """The pool Hermes keeps this provider's keys in.

    A custom endpoint's profile is ``custom``, but Hermes pools its keys under
    ``custom:<name>`` (``credential_pool.get_custom_provider_pool_key``), which
    is the pool 1.8.1.8 read off the agent. Every other provider pools under
    its own name.
    """
    name = str(provider or "")
    if name == "custom" or name.startswith("custom:"):
        try:
            from agent.credential_pool import get_custom_provider_pool_key

            return get_custom_provider_pool_key(base_url or "") or name
        except Exception:
            logger.debug("kame: could not name the custom pool for %s", base_url, exc_info=True)
    return name


def _pool_keys(provider: str, base_url: str = "") -> Tuple[List[str], Dict[str, Any]]:
    """``(keys, entry_by_key)`` from Hermes' own pool for ``provider``. Never raises."""
    keys: List[str] = []
    entries: Dict[str, Any] = {}
    provider = _pool_name(provider, base_url)
    try:
        from agent.credential_pool import load_pool

        pool = load_pool(provider)
        rows = list(pool.entries()) if pool is not None else []
    except Exception:
        logger.debug("kame: could not read the %s pool", provider, exc_info=True)
        return keys, entries
    listed = []
    for entry in rows:
        try:
            raw = multikey.key_on(entry)
        except Exception:
            continue
        if not raw:
            continue
        parts, _rejected = multikey.split_value(raw)
        if len(parts) > 1:
            listed.append((parts, entry))
            continue
        if raw not in entries:
            entries[raw] = entry
            keys.append(raw)
    # A row holding a comma list only supplies the parts nobody else owns —
    # the 1.8.1.8 ``candidates`` rule, for the same reason.
    for parts, entry in listed:
        for part in parts:
            if part not in entries:
                entries[part] = entry
                keys.append(part)
    _note_shape(provider, rows, keys)
    return keys, entries


#: What each provider's pool turned out to be, the last time a client read it:
#: rows, keys after splitting, how many the host considers spent, and where
#: they came from. Numbers only — the panel's "is it even seeing my keys?"
#: card (1.6.0.0), fed from here now that ``pool_binding`` is gone.
SEEN_POOLS: Dict[str, Dict[str, Any]] = {}


def _note_shape(provider: str, rows: List[Any], keys: List[str]) -> None:
    try:
        if not rows and not keys:
            SEEN_POOLS.pop(provider, None)
            return
        origins = sorted({
            str(getattr(row, "source", "") or "?").split(":", 1)[0].split("#", 1)[0]
            for row in rows
        })
        SEEN_POOLS[provider] = {
            "provider": provider,
            "rows": len(rows),
            "keys": len(keys),
            "benched": sum(1 for row in rows if getattr(row, "last_status", None) == "exhausted"),
            "origin": ", ".join(origins),
            "split": len(keys) > len(rows),
            "at": time.time(),
        }
        if len(SEEN_POOLS) > 32:  # pragma: no cover — bounded, never hit
            oldest = min(SEEN_POOLS, key=lambda name: SEEN_POOLS[name]["at"])
            SEEN_POOLS.pop(oldest, None)
    except Exception:  # pragma: no cover - a readout
        logger.debug("kame: could not note the pool shape", exc_info=True)


class _KeySource:
    """Every key this client may send, refreshed on a short clock."""

    def __init__(self, provider: str, api_key: str, base_url: str = "") -> None:
        self.provider = provider
        self.base_url = str(base_url or "")
        self.api_key = str(api_key or "").strip()
        self._lock = threading.Lock()
        self._keys: List[str] = []
        self._entries: Dict[str, Any] = {}
        self._read_at = 0.0

    def keys(self) -> List[str]:
        now = time.monotonic()
        with self._lock:
            if self._keys and now - self._read_at < KEYS_TTL_S:
                return list(self._keys)
        keys, entries = _pool_keys(self.provider, self.base_url)
        # The key Hermes resolved for this client is a working credential
        # whether or not the pool knows it — a comma-joined variable, a key
        # set by hand — and dropping it would narrow what Hermes would have sent.
        parts, _rejected = multikey.split_value(self.api_key)
        for part in (parts if len(parts) > 1 else ([self.api_key] if self.api_key else [])):
            if part not in entries:
                entries[part] = None
                keys.append(part)
        with self._lock:
            self._keys, self._entries, self._read_at = keys, entries, now
            return list(keys)

    def entry(self, key: str) -> Any:
        with self._lock:
            return self._entries.get(key)


class _FacadeCall(Call):
    """One request: the transport's view of the client making it."""

    def __init__(self, client: "_KameMixin", kwargs: Dict[str, Any]) -> None:
        self._client = client
        self.kwargs = dict(kwargs)
        self.kwargs.pop("stream", None)
        self.provider = client._kame_provider
        self.identity = Carousel.identity(self.provider, str(self.kwargs.get("model") or ""))

    def keys(self) -> List[str]:
        return self._client._kame_keys.keys()

    def entry(self, key: str) -> Any:
        return self._client._kame_keys.entry(key)

    def client_for(self, key: str, attempt: int) -> Any:
        return self._client._kame_inner(key, attempt)

    def cancelled(self) -> bool:
        return self._client._kame_cancelled.is_set()


class _KameMixin:
    """Shared by both client shapes: key source, inner clients, cancellation."""

    _kame_provider: str = ""

    def _kame_setup(self, provider: str, client_kwargs: Dict[str, Any]) -> None:
        self._kame_provider = provider
        self._kame_kwargs = dict(client_kwargs)
        self._kame_keys = _KeySource(provider, str(client_kwargs.get("api_key") or ""),
                                     str(client_kwargs.get("base_url") or ""))
        self._kame_cancelled = threading.Event()
        self._kame_inners: Dict[str, Any] = {}
        self._kame_lock = threading.Lock()

    def _kame_create(self, **kwargs: Any) -> Any:
        call = _FacadeCall(self, kwargs)
        if kwargs.get("stream"):
            return TRANSPORT.stream(call)
        return TRANSPORT.complete(call)

    def _kame_inner(self, key: str, attempt: int) -> Any:
        key = key or self._kame_keys.api_key
        with self._kame_lock:
            inner = self._kame_inners.get(key)
            if inner is None:
                inner = self._kame_inners[key] = self._kame_build(key)
        timeout = attempt_read_timeout(getattr(self, "base_url", ""), attempt)
        if timeout is None:
            return inner
        return _WithTimeout(inner, timeout)

    def _kame_close(self) -> None:
        self._kame_cancelled.set()
        with self._kame_lock:
            inners, self._kame_inners = list(self._kame_inners.values()), {}
        for inner in inners:
            try:
                inner.close()
            except Exception:  # pragma: no cover - closing a client
                logger.debug("kame: could not close an inner client", exc_info=True)


class _WithTimeout:
    """An inner client whose next request carries a per-attempt timeout."""

    def __init__(self, inner: Any, timeout: float) -> None:
        self._inner = inner
        self._timeout = timeout
        from types import SimpleNamespace

        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> Any:
        kwargs.setdefault("timeout", self._timeout)
        return self._inner.chat.completions.create(**kwargs)


#: How long a reading of Hermes' configuration (TLS settings, declared wire)
#: is reused for the same provider and endpoint. Hermes builds a client per
#: turn, and per auxiliary call; re-reading config.yaml for each one cost
#: ~5 ms a client. A change made in the config is seen within this many seconds.
CONFIG_READ_TTL_S = 5.0

_CONFIG_READS: Dict[Tuple[str, str, str], Tuple[float, Any]] = {}


def _cached(kind: str, provider: str, base_url: str, read: Any) -> Any:
    key = (kind, str(provider or ""), str(base_url or ""))
    now = time.monotonic()
    hit = _CONFIG_READS.get(key)
    if hit is not None and now - hit[0] < CONFIG_READ_TTL_S:
        return hit[1]
    value = read()
    if len(_CONFIG_READS) > 256:  # pragma: no cover — bounded, never hit
        _CONFIG_READS.clear()
    _CONFIG_READS[key] = (now, value)
    return value


def _verify_for(base_url: str) -> Any:
    return _cached("verify", "", base_url, lambda: _read_verify(base_url))


def _read_verify(base_url: str) -> Any:
    """httpx ``verify`` for ``base_url``, the way Hermes resolves it.

    Hermes pops ``ssl_ca_cert`` / ``ssl_verify`` from the client kwargs before
    a profile is asked for a client, so they are read again here from the same
    place Hermes' auxiliary client reads them (``_resolve_aux_verify``): a
    custom provider's TLS settings, else the platform trust store.
    """
    try:
        from agent.ssl_verify import resolve_httpx_verify
        from hermes_cli.config import get_custom_provider_tls_settings, load_config_readonly

        tls = get_custom_provider_tls_settings(str(base_url or ""), config=load_config_readonly())
        return resolve_httpx_verify(
            ca_bundle=tls.get("ssl_ca_cert"), ssl_verify=tls.get("ssl_verify"), base_url=str(base_url or ""))
    except Exception:
        logger.debug("kame: TLS settings unreadable; using the platform store", exc_info=True)
        return True


def _keepalive_http(base_url: str) -> Any:
    try:
        from agent.process_bootstrap import build_keepalive_http_client

        return build_keepalive_http_client(base_url or "", verify=_verify_for(base_url))
    except Exception:
        logger.debug("kame: no keep-alive client builder in this Hermes", exc_info=True)
        return None


#: Error bodies larger than this are left for Hermes to read its own way
#: (``bounded_response.DEFAULT_ERROR_BODY_MAX_BYTES``). Google's are ~2 KB.
ERROR_BODY_KEEP_MAX = 64 * 1024


def _keep_error_body(response: Any) -> None:
    """httpx response hook: read a refusal's body so it can be read twice.

    Google says whether a 429 is the per-minute or the per-day quota only in
    ``QuotaFailure.violations[].quotaId``. ``gemini_http_error`` keeps the
    ``ErrorInfo`` entry and drops that one, and on the streaming path the body
    is drained (``read_streaming_error_body``) before the exception exists, so
    ``exc.response`` arrives with nothing left to read. Until 1.8.1.8 KAME
    wrapped the host factory to keep it; that is the kind of rebinding the
    catalog forbids, and it is not needed: reading the body here, on KAME's own
    connection, caches it on the response (httpx ``Response.read``). Hermes then
    reads the same bytes it always did — ``iter_bytes`` replays a read body —
    and ``core.evidence`` finds them on ``exc.response``.

    Only refusals, only bodies that say they are small. Never raises: a body
    that cannot be read here is read, or not, by Hermes exactly as before.
    """
    try:
        if int(getattr(response, "status_code", 0) or 0) < 400:
            return
        length = response.headers.get("content-length")
        if length is not None and int(length) > ERROR_BODY_KEEP_MAX:
            return
        response.read()
    except Exception:
        logger.debug("kame: refusal body left unread", exc_info=True)


def _with_error_body_hook(http: Any) -> Any:
    try:
        hooks = http.event_hooks
        response_hooks = list(hooks.get("response", []))
        if _keep_error_body not in response_hooks:
            response_hooks.append(_keep_error_body)
            hooks["response"] = response_hooks
            http.event_hooks = hooks
    except Exception:
        logger.debug("kame: could not keep refusal bodies on this client", exc_info=True)
    return http


def _gemini_client_class() -> Any:
    from agent.gemini_native_adapter import GeminiNativeClient

    class KameGeminiClient(_KameMixin, GeminiNativeClient):
        """A ``GeminiNativeClient`` whose requests go through KAME's carousel."""

        def __init__(self, provider: str, client_kwargs: Dict[str, Any]) -> None:
            safe = {k: v for k, v in client_kwargs.items()
                    if k in {"api_key", "base_url", "default_headers", "timeout", "http_client"}}
            first, _rejected = multikey.split_value(str(safe.get("api_key") or ""))
            if first:
                safe["api_key"] = first[0]
            if "http_client" not in safe:
                http = _keepalive_http(str(safe.get("base_url") or ""))
                if http is None:
                    import httpx

                    http = httpx.Client(
                        timeout=safe.get("timeout") or httpx.Timeout(connect=15.0, read=600.0, write=30.0, pool=30.0),
                        verify=_verify_for(str(safe.get("base_url") or "")))
                safe["http_client"] = http
            _with_error_body_hook(safe["http_client"])
            GeminiNativeClient.__init__(self, **safe)
            self._kame_safe = safe
            self._kame_setup(provider, client_kwargs)
            from types import SimpleNamespace

            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._kame_create))

        def _kame_build(self, key: str) -> Any:
            return GeminiNativeClient(**dict(self._kame_safe, api_key=key))

        def close(self) -> None:
            self._kame_close()
            GeminiNativeClient.close(self)

    return KameGeminiClient


def _openai_client_class() -> Any:
    from openai import OpenAI

    class KameOpenAIClient(_KameMixin, OpenAI):
        """An ``openai.OpenAI`` whose chat completions go through KAME's carousel."""

        def __init__(self, provider: str, client_kwargs: Dict[str, Any]) -> None:
            kwargs = dict(client_kwargs)
            kwargs.setdefault("max_retries", 0)
            first, _rejected = multikey.split_value(str(kwargs.get("api_key") or ""))
            if first:
                kwargs["api_key"] = first[0]
            if "http_client" not in kwargs:
                http = _keepalive_http(str(kwargs.get("base_url") or ""))
                if http is not None:
                    kwargs["http_client"] = http
            try:
                from agent.codex_headers import apply_required_codex_headers

                apply_required_codex_headers(kwargs, access_token=kwargs.get("api_key", ""),
                                             base_url=str(kwargs.get("base_url", "")))
            except Exception:
                logger.debug("kame: codex header helper unavailable", exc_info=True)
            OpenAI.__init__(self, **kwargs)
            self._kame_openai_kwargs = kwargs
            self._kame_setup(provider, client_kwargs)
            from types import SimpleNamespace

            self._kame_chat = SimpleNamespace(completions=SimpleNamespace(create=self._kame_create))

        @property
        def chat(self) -> Any:  # type: ignore[override]
            return self._kame_chat

        def _kame_build(self, key: str) -> Any:
            return OpenAI(**dict(self._kame_openai_kwargs, api_key=key))

        def close(self) -> None:
            self._kame_close()
            OpenAI.close(self)

    return KameOpenAIClient


_CLASSES: Dict[str, Any] = {}

#: Wires KAME's client does not speak. A profile's ``create_client`` answer is
#: used as-is — Hermes' auxiliary path skips its own wire adapters for it
#: (``auxiliary_client._wrap_transport``) — so a chat-completions client handed
#: out where Hermes would have wrapped one for the Responses or Messages API
#: would break that call. Wherever such a wire is possible, KAME answers
#: ``None`` and Hermes builds its own client; the refusal hook still sizes
#: every refusal on it.
_CHAT_WIRE = "chat_completions"


def _with_hermes_headers(provider: str, client_kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """``client_kwargs`` with the headers Hermes would have put on this client.

    The main agent applies its endpoint headers (NVIDIA's billing origin, the
    Kimi Code user agent, a profile's attribution, the user's own
    ``default_headers``) before it asks the profile, so they arrive here. The
    auxiliary path asks the profile first and computes them only for the
    client it builds itself (``auxiliary_client._endpoint_default_headers``),
    so an auxiliary request reaches KAME without them; they are computed here
    the same way, by the same function.
    """
    if "default_headers" in client_kwargs:
        return client_kwargs
    try:
        from agent.auxiliary_client import _endpoint_default_headers

        headers = _endpoint_default_headers(str(client_kwargs.get("base_url") or ""), provider, xai=True)
    except Exception:
        logger.debug("kame: could not read Hermes' default headers for %s", provider, exc_info=True)
        return client_kwargs
    if not headers:
        return client_kwargs
    return dict(client_kwargs, default_headers=dict(headers))


def _other_wire(provider: str, base_url: str) -> str:
    """Why this provider/endpoint may need a wire other than chat completions, or ``""``."""
    return _cached("wire", provider, base_url, lambda: _read_other_wire(provider, base_url))


def _read_other_wire(provider: str, base_url: str) -> str:
    try:
        from agent.auxiliary_client import _endpoint_speaks_anthropic_messages

        if _endpoint_speaks_anthropic_messages(base_url):
            return "the endpoint speaks Anthropic Messages"
    except Exception:
        lowered = base_url.lower().rstrip("/")
        if lowered.endswith(("/anthropic", "/anthropic/v1")) or "api.anthropic.com" in lowered:
            return "the endpoint speaks Anthropic Messages"
    try:
        from urllib.parse import urlparse

        if (urlparse(base_url).hostname or "").lower() == "api.openai.com":
            # Hermes picks the Responses API there by model name.
            return "api.openai.com chooses its wire per model"
    except Exception:
        pass
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly() or {}
    except Exception:
        return ""
    name = str(provider or "").strip().lower()

    def _declared(section: Any) -> str:
        if not isinstance(section, dict):
            return ""
        mode = str(section.get("api_mode") or "").strip().lower()
        return mode if mode and mode != _CHAT_WIRE else ""

    model = config.get("model")
    if isinstance(model, dict) and str(model.get("provider") or "").strip().lower() == name:
        mode = _declared(model)
        if mode:
            return f"model.api_mode is {mode}"
    aux = config.get("auxiliary")
    if isinstance(aux, dict):
        for task, section in aux.items():
            if isinstance(section, dict) and str(section.get("provider") or "").strip().lower() == name:
                mode = _declared(section)
                if mode:
                    return f"auxiliary.{task}.api_mode is {mode}"
    customs = config.get("custom_providers")
    if isinstance(customs, list):
        for entry in customs:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("base_url") or "").rstrip("/") == base_url.rstrip("/") or (
                    name and str(entry.get("name") or "").strip().lower() == name):
                mode = _declared(entry)
                if mode:
                    return f"custom provider api_mode is {mode}"
    return ""


def make_client(provider: str, client_kwargs: Dict[str, Any]) -> Optional[Any]:
    """KAME's client for ``provider``, or ``None`` to let Hermes build its own.

    ``None`` whenever KAME is switched off, there is no key to rotate, or the
    shape cannot be built — every one of those leaves Hermes exactly where it
    would have been without this plugin.
    """
    if not enabled():
        return None
    if not str(client_kwargs.get("api_key") or "").strip():
        return None
    base_url = str(client_kwargs.get("base_url") or "")
    why = _other_wire(provider, base_url)
    if why:
        logger.debug("kame: %s keeps Hermes' own client — %s", provider, why)
        return None
    client_kwargs = _with_hermes_headers(provider, client_kwargs)
    try:
        from agent.gemini_native_adapter import is_native_gemini_base_url

        native_gemini = is_native_gemini_base_url(str(client_kwargs.get("base_url") or ""))
    except Exception:
        native_gemini = False
    shape = "gemini" if native_gemini else "openai"
    try:
        cls = _CLASSES.get(shape)
        if cls is None:
            cls = _CLASSES[shape] = _gemini_client_class() if native_gemini else _openai_client_class()
        return cls(provider, client_kwargs)
    except Exception:
        logger.warning("kame: could not build a %s client; Hermes builds its own", provider, exc_info=True)
        return None


# --- the registry the provider package finds this module through -----------

#: A plugin-owned name in ``sys.modules`` (never a Hermes one): the provider
#: package is imported by Hermes' provider discovery, this package by its
#: plugin manager, and neither may import the other by path without loading a
#: second copy with a second carousel. Keyed by profile home, because one
#: process can serve several homes.
REGISTRY_KEY = "kame_rotation_registry_v1"


def _home() -> str:
    try:
        from hermes_constants import get_hermes_home

        return os.path.normcase(str(get_hermes_home()))
    except Exception:
        return os.path.normcase(os.environ.get("HERMES_HOME", ""))


def publish(module: Any) -> None:
    """Let the provider package find ``module`` (this package) for the current home."""
    import sys
    from types import ModuleType

    registry = sys.modules.get(REGISTRY_KEY)
    if registry is None:
        registry = ModuleType(REGISTRY_KEY)
        registry.homes = {}
        registry.lock = threading.Lock()
        registry = sys.modules.setdefault(REGISTRY_KEY, registry)
    with registry.lock:
        registry.homes[_home()] = module


def retract(module: Any) -> None:
    import sys

    registry = sys.modules.get(REGISTRY_KEY)
    if registry is None:
        return
    with registry.lock:
        if registry.homes.get(_home()) is module:
            registry.homes.pop(_home(), None)
