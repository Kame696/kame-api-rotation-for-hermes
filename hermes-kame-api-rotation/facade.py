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

from . import settings, wires
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

    def __init__(self, client: "_KameMixin", kwargs: Dict[str, Any], wire: Any = wires.CHAT) -> None:
        self._client = client
        self.kwargs = dict(kwargs)
        self.kwargs.pop("stream", None)
        self.provider = client._kame_provider
        self.identity = Carousel.identity(self.provider, str(self.kwargs.get("model") or ""))
        self.wire = wire
        self.response: Any = None

    def attach_response(self, response: Any) -> None:
        self.response = response

    def status_owner(self) -> str:
        # One client per agent, so one spinner throttle per conversation.
        return f"client:{id(self._client):x}"

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

    def _kame_responses_create(self, **kwargs: Any) -> Any:
        call = _FacadeCall(self, kwargs, wires.RESPONSES)
        if kwargs.get("stream"):
            return _EventStream(TRANSPORT.stream(call), call)
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
    """An inner client whose next request carries a per-attempt timeout, on whichever wire it speaks."""

    def __init__(self, inner: Any, timeout: float) -> None:
        self._inner = inner
        self._timeout = timeout
        from types import SimpleNamespace

        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._timed(
            lambda: inner.chat.completions.create)))
        self.responses = SimpleNamespace(create=self._timed(lambda: inner.responses.create))
        self.messages = SimpleNamespace(create=self._timed(lambda: inner.messages.create),
                                        stream=self._timed(lambda: inner.messages.stream))

    def _timed(self, target: Any) -> Any:
        def call(**kwargs: Any) -> Any:
            kwargs.setdefault("timeout", self._timeout)
            return target()(**kwargs)

        return call


class _EventStream:
    """What ``responses.create(stream=True)`` returns: an iterable of the wire's events.

    Hermes iterates it, closes it, and reads the attempt's HTTP response from
    it for its interrupt and diagnostics paths; everything else is the events.
    """

    def __init__(self, events: Any, call: "_FacadeCall") -> None:
        self._events = events
        self._call = call

    def __iter__(self) -> Any:
        return iter(self._events)

    def __next__(self) -> Any:
        return next(self._events)

    @property
    def response(self) -> Any:
        return self._call.response

    def close(self) -> None:
        close = getattr(self._events, "close", None)
        if callable(close):
            close()


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
    if settings.is_on(settings.QUOTA_ID_DISABLED):
        return http
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

        def __init__(self, provider: str, client_kwargs: Dict[str, Any], responses_wire: bool = False) -> None:
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
            self._kame_responses = SimpleNamespace(create=self._kame_responses_create)
            if responses_wire:
                # Declared Responses-only: Hermes' auxiliary path uses a profile's client
                # as-is and calls ``chat.completions`` on it, where it would have wrapped its
                # own client in ``CodexAuxiliaryClient``. Hermes' own adapter is put on that
                # attribute instead, over this client's carousel ``responses``.
                try:
                    from agent.auxiliary_client import _ChatShim, _CodexCompletionsAdapter

                    self._kame_chat = _ChatShim(_CodexCompletionsAdapter(self, ""))
                except Exception:
                    logger.debug("kame: Hermes' Responses chat adapter unavailable", exc_info=True)

        @property
        def chat(self) -> Any:  # type: ignore[override]
            return self._kame_chat

        @property
        def responses(self) -> Any:  # type: ignore[override]
            return self._kame_responses

        def _kame_build(self, key: str) -> Any:
            return OpenAI(**dict(self._kame_openai_kwargs, api_key=key))

        def close(self) -> None:
            self._kame_close()
            OpenAI.close(self)

    return KameOpenAIClient


_CLASSES: Dict[str, Any] = {}

_CHAT_WIRE = "chat_completions"
_RESPONSES_WIRE = "codex_responses"
_MESSAGES_WIRE = "anthropic_messages"


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


def _wire(provider: str, base_url: str, declared: str = "") -> str:
    """Which wire Hermes will speak to this provider/endpoint: chat, responses or messages."""
    return _cached("wire", provider, f"{base_url}|{declared}", lambda: _read_wire(provider, base_url, declared))


def _read_wire(provider: str, base_url: str, declared: str = "") -> str:
    """The wire, from the same facts Hermes picks it from.

    A Messages endpoint (by URL, as Hermes' auxiliary path detects it) or a
    configured ``api_mode`` wins; then the profile's own declared mode.
    """
    try:
        from agent.auxiliary_client import _endpoint_speaks_anthropic_messages

        if _endpoint_speaks_anthropic_messages(base_url):
            return _MESSAGES_WIRE
    except Exception:
        lowered = base_url.lower().rstrip("/")
        if lowered.endswith(("/anthropic", "/anthropic/v1")) or "api.anthropic.com" in lowered:
            return _MESSAGES_WIRE
    configured = _configured_mode(provider, base_url)
    if configured in (_CHAT_WIRE, _RESPONSES_WIRE, _MESSAGES_WIRE):
        return configured
    mode = str(declared or "").strip().lower()
    return mode if mode in (_RESPONSES_WIRE, _MESSAGES_WIRE) else _CHAT_WIRE


def _configured_mode(provider: str, base_url: str) -> str:
    """An ``api_mode`` the owner configured for this provider or endpoint, or ``""``."""
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly() or {}
    except Exception:
        return ""
    name = str(provider or "").strip().lower()

    def _declared(section: Any) -> str:
        if not isinstance(section, dict):
            return ""
        return str(section.get("api_mode") or "").strip().lower()

    model = config.get("model")
    if isinstance(model, dict) and str(model.get("provider") or "").strip().lower() == name:
        mode = _declared(model)
        if mode:
            return mode
    aux = config.get("auxiliary")
    if isinstance(aux, dict):
        for section in aux.values():
            if isinstance(section, dict) and str(section.get("provider") or "").strip().lower() == name:
                mode = _declared(section)
                if mode:
                    return mode
    customs = config.get("custom_providers")
    if isinstance(customs, list):
        for entry in customs:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("base_url") or "").rstrip("/") == base_url.rstrip("/") or (
                    name and str(entry.get("name") or "").strip().lower() == name):
                mode = _declared(entry)
                if mode:
                    return mode
    return ""


_SYNCED_LISTS: set = set()


def _follow_the_list(api_key: Any) -> None:
    """A list Hermes hands over that the pool has not seen yet: sync it, off the turn.

    The variable was edited while Hermes ran (the Settings field writes .env).
    ``envsync`` normally runs at start; this catches the edit without one. Once
    per distinct value, on a daemon thread, so building a client never waits on
    auth.json.
    """
    if not isinstance(api_key, str) or len(multikey.split_value(api_key)[0]) < 2:
        return
    import hashlib

    digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    if digest in _SYNCED_LISTS:
        return
    _SYNCED_LISTS.add(digest)

    def run() -> None:
        try:
            from . import envsync

            envsync.sync()
        except Exception:
            logger.debug("kame: could not sync a new key list", exc_info=True)

    threading.Thread(target=run, name="kame-envsync", daemon=True).start()


def make_client(provider: str, client_kwargs: Dict[str, Any], api_mode: str = "") -> Optional[Any]:
    """KAME's client for ``provider``, or ``None`` to let Hermes build its own.

    ``None`` whenever KAME is switched off, there is no key to rotate, the
    shape cannot be built, or the endpoint speaks Anthropic Messages (that
    client comes through :func:`make_messages_client`) — every one of those
    leaves Hermes exactly where it would have been without this plugin.
    """
    if not enabled():
        return None
    if not str(client_kwargs.get("api_key") or "").strip():
        return None
    _follow_the_list(client_kwargs.get("api_key"))
    base_url = str(client_kwargs.get("base_url") or "")
    wire = _wire(provider, base_url, api_mode)
    if wire == _MESSAGES_WIRE:
        logger.debug("kame: %s speaks Anthropic Messages - its client comes from the Messages seam", provider)
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
        if native_gemini:
            return cls(provider, client_kwargs)
        return cls(provider, client_kwargs, responses_wire=(wire == _RESPONSES_WIRE))
    except Exception:
        logger.warning("kame: could not build a %s client; Hermes builds its own", provider, exc_info=True)
        return None


# --- the Anthropic Messages wire ---------------------------------------------


class _MessagesProxy:
    """``client.messages``: ``create`` and ``stream`` through the carousel, the rest Hermes' own."""

    def __init__(self, owner: "KameMessagesClient") -> None:
        self._owner = owner

    def create(self, **kwargs: Any) -> Any:
        call = _FacadeCall(self._owner, kwargs, wires.MESSAGES)
        if kwargs.get("stream"):
            return _MessageStream(call)
        return TRANSPORT.complete(call)

    def stream(self, **kwargs: Any) -> "_MessageStreamManager":
        return _MessageStreamManager(_FacadeCall(self._owner, kwargs, wires.MESSAGES))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._owner._kame_primary.messages, name)


class _MessageStreamManager:
    """What ``messages.stream()`` returns: a context manager around one carousel call."""

    def __init__(self, call: "_FacadeCall") -> None:
        self._call = call
        self._stream: Optional[_MessageStream] = None

    def __enter__(self) -> "_MessageStream":
        self._stream = _MessageStream(self._call)
        return self._stream

    def __exit__(self, *exc: Any) -> None:
        if self._stream is not None:
            self._stream.close()


class _MessageStream:
    """The carousel's Messages events, with the SDK ``MessageStream`` surface Hermes reads.

    One message however many keys it took: ``get_final_message`` is folded
    from the events Hermes was actually handed (``wires._MessagesEmitter``),
    so a continued answer arrives whole.
    """

    def __init__(self, call: "_FacadeCall") -> None:
        self._call = call
        self._emitter = wires._MessagesEmitter()
        self._events = self._emitter.run(TRANSPORT._views(call))
        self._done = False

    def __iter__(self) -> Any:
        return self

    def __next__(self) -> Any:
        try:
            return next(self._events)
        except StopIteration:
            self._done = True
            raise

    @property
    def response(self) -> Any:
        return self._call.response

    def until_done(self) -> None:
        for _ in self:
            pass

    def get_final_message(self) -> Any:
        if not self._done:
            self.until_done()
        snapshot = self._emitter.final_message()
        if snapshot is None:
            raise RuntimeError("kame: the Messages stream ended without a message")
        return snapshot

    def get_final_text(self) -> str:
        message = self.get_final_message()
        return "".join(str(getattr(block, "text", "") or "") for block in getattr(message, "content", []) or []
                       if getattr(block, "type", "") == "text")

    def close(self) -> None:
        close = getattr(self._events, "close", None)
        if callable(close):
            close()


class KameMessagesClient(_KameMixin):
    """An Anthropic Messages client whose requests go through KAME's carousel.

    Every key's client is built by Hermes' own ``build_anthropic_client`` —
    auth style, beta headers, endpoint normalisation and attribution exactly
    as Hermes would have done it. Attributes the carousel does not own are the
    first key's client's.
    """

    def __init__(self, provider: str, client_kwargs: Dict[str, Any]) -> None:
        from agent.anthropic_adapter import build_anthropic_client

        self._kame_builder = build_anthropic_client
        self._kame_messages_kwargs = {
            "timeout": client_kwargs.get("timeout"),
            "drop_context_1m_beta": bool(client_kwargs.get("drop_context_1m_beta") or False),
        }
        self._kame_base_url = client_kwargs.get("base_url")
        self._kame_options: Dict[str, Any] = {}
        first, _rejected = multikey.split_value(str(client_kwargs.get("api_key") or ""))
        self._kame_first_key = first[0] if first else str(client_kwargs.get("api_key") or "")
        self._kame_primary = self._kame_build(self._kame_first_key)
        self._kame_setup(provider, client_kwargs)
        self.base_url = getattr(self._kame_primary, "base_url", self._kame_base_url)
        self.messages = _MessagesProxy(self)

    def _kame_build(self, key: str) -> Any:
        # A fresh SDK client costs ~0.4 s (its own HTTP pool and TLS context), paid on the
        # first request of every key. A key with the first key's credential shape gets the
        # first client's copy instead: same headers, same connection pool, its own key.
        primary = self.__dict__.get("_kame_primary")
        if primary is not None and self._kame_same_auth(key):
            field = "auth_token" if getattr(primary, "auth_token", None) else "api_key"
            try:
                return primary.with_options(**{field: key})
            except Exception:  # pragma: no cover - fall back to Hermes' own builder
                logger.debug("kame: could not derive a Messages client", exc_info=True)
        client = self._kame_builder(key, self._kame_base_url, **self._kame_messages_kwargs)
        if self._kame_options:
            client = client.with_options(**self._kame_options)
        return client

    def _kame_same_auth(self, key: str) -> bool:
        """Would Hermes authenticate ``key`` the way it authenticated the first key?

        Only the key's own shape (OAuth token or API key) can make two keys of one
        endpoint differ; every other part of Hermes' choice is the endpoint's.
        """
        try:
            from agent.anthropic_adapter import _is_oauth_token
        except Exception:
            return False
        try:
            return bool(_is_oauth_token(key)) == bool(_is_oauth_token(self._kame_first_key))
        except Exception:
            return False

    def with_options(self, **options: Any) -> "KameMessagesClient":
        clone = object.__new__(KameMessagesClient)
        clone.__dict__.update(self.__dict__)
        clone._kame_options = dict(self._kame_options, **options)
        clone._kame_primary = self._kame_primary.with_options(**options)
        clone._kame_inners = {}
        clone._kame_lock = threading.Lock()
        clone._kame_cancelled = threading.Event()
        clone.messages = _MessagesProxy(clone)
        return clone

    copy = with_options

    def close(self) -> None:
        self._kame_close()
        try:
            self._kame_primary.close()
        except Exception:  # pragma: no cover - closing a client
            logger.debug("kame: could not close the primary Messages client", exc_info=True)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_kame") or name.startswith("__"):
            raise AttributeError(name)
        return getattr(self.__dict__["_kame_primary"], name)


def make_messages_client(provider: str, client_kwargs: Dict[str, Any]) -> Optional[Any]:
    """KAME's Anthropic Messages client, or ``None`` to let Hermes build its own.

    Asked through ``ProviderProfile.create_messages_client``. A callable
    ``api_key`` (an Entra / OAuth bearer provider that mints per request) is
    Hermes' own business and is left to it, as is anything that fails to build.
    """
    if not enabled():
        return None
    api_key = client_kwargs.get("api_key")
    if not isinstance(api_key, str) or not api_key.strip():
        return None
    _follow_the_list(api_key)
    try:
        return KameMessagesClient(provider, client_kwargs)
    except Exception:
        logger.warning("kame: could not build a Messages client for %s; Hermes builds its own", provider,
                       exc_info=True)
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
