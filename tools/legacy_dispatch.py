"""Run the 1.8.1.8 dispatch tests against the 1.8.1.9 transport, unchanged.

Until 1.8.1.8 the carousel lived in ``dispatch_binding``, which wrapped two
Hermes functions and was handed ``(original, agent, api_kwargs)`` per call.
1.8.1.9 moved the same loop into ``transport.KameTransport`` — Hermes now asks
KAME's provider profile for a client, and the transport drives that client
(catalog rule 9: no rebinding of Hermes core). The decisions inside the loop
were cut from ``dispatch_binding`` verbatim.

Hundreds of tests written against 1.8.1.8 pin those decisions. Rewriting them
would mean the new code is checked by tests written *for* the new code, which
is the trap ``kame-teste-escrito-pelo-proprio-erro`` describes. So instead
they keep their own words: ``conftest.py`` answers ``<package>.dispatch_binding``
with a module built here, whose names resolve to ``<package>.transport`` and
whose ``DispatchBinding.run`` turns the old ``(original, agent, api_kwargs)``
call into a :class:`transport.Call` over that agent. The agent's pool, its
comma-joined ``api_key`` and its interrupt flag mean what they meant in
1.8.1.8, by 1.8.1.8's own code (``candidates`` and ``_apply_key`` below are
copied from it unchanged).

What cannot be expressed this way is reported, not hidden: the tests that
assert the *mechanism* of 1.8.1.8 — wrapping host functions, shims on agent
callbacks, the in-chat spinner — fail with ``AttributeError`` and are listed in
``research/1.8.1.9/legacy-suite.md`` with what replaced each.
"""

from __future__ import annotations

import importlib
import importlib.machinery
import importlib.util
import logging
import os
import sys
import types
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


def build(package: str) -> types.ModuleType:
    """The ``<package>.dispatch_binding`` module for a loaded plugin package."""
    transport = importlib.import_module(f"{package}.transport")
    multikey = importlib.import_module(f"{package}.core.multikey")

    # 1.8.1.8 drew its spinner line through ``agent._emit_wait_notice`` and its
    # wait notices through ``agent._emit_status``. 1.8.1.9 draws both through
    # Hermes' ``notify_turn_status``, which the host routes to those same two
    # methods of the agent whose call is in flight. Here the old tests' agent
    # is that agent: the router below plays the host's part.
    from contextvars import ContextVar

    _AGENT: "ContextVar[Any]" = ContextVar(f"{package}_legacy_agent", default=None)
    try:
        from agent.status_output import notify_turn_status as _host_notify
    except Exception:
        _host_notify = None

    def _route(message: str, *, kind: str = "lifecycle") -> bool:
        agent = _AGENT.get()
        if agent is None:
            return bool(_host_notify(message, kind=kind)) if _host_notify else False
        method = getattr(agent, "_emit_wait_notice" if kind == "activity" else "_emit_status", None)
        if not callable(method):
            return False
        method(message)
        return True

    transport._NOTIFY = _route
    carousel = importlib.import_module(f"{package}.core.carousel")
    settings = importlib.import_module(f"{package}.settings")

    def _entries_of(agent: Any) -> List[Any]:
        pool = getattr(agent, "_credential_pool", None)
        if pool is None:
            return []
        try:
            entries = list(pool.entries())
        except Exception:
            return []
        usable = []
        for entry in entries:
            try:
                key = getattr(entry, "runtime_api_key", None) or getattr(entry, "access_token", "")
            except Exception:
                continue
            if str(key or "").strip():
                usable.append(entry)
        return usable

    def candidates(agent: Any) -> Tuple[List[str], Dict[str, Any]]:
        entry_by_key: Dict[str, Any] = {}
        keys: List[str] = []

        def _add(raw: str, entry: Any) -> None:
            split, _rejected = multikey.split_value(raw)
            parts = split if len(split) > 1 else ([raw] if raw else [])
            for part in parts:
                if part not in entry_by_key:
                    entry_by_key[part] = entry
                    keys.append(part)

        listed: List[Tuple[str, Any]] = []
        for entry in _entries_of(agent):
            key = multikey.key_on(entry)
            if not key:
                continue
            try:
                parts, _rejected = multikey.split_value(key)
            except Exception:
                parts = []
            if len(parts) > 1:
                listed.append((key, entry))
                continue
            _add(key, entry)
        for key, entry in listed:
            _add(key, entry)
        current = str(getattr(agent, "api_key", "") or "").strip()
        if not keys:
            _add(current, None)
        elif current and current not in entry_by_key:
            _add(current, None)
        return keys, entry_by_key

    def _attribute(agent: Any, entry: Any) -> None:
        if entry is None:
            return
        try:
            entry_id = getattr(entry, "id", None)
            if isinstance(entry_id, str) and entry_id:
                agent._credential_pool_entry_id = entry_id
        except Exception:
            pass

    def _apply_key(agent: Any, key: str, entry: Any) -> bool:
        if not key:
            return False
        if str(getattr(agent, "api_key", "") or "") == key:
            _attribute(agent, entry)
            return True
        try:
            agent.api_key = key
            client_kwargs = getattr(agent, "_client_kwargs", None)
            if isinstance(client_kwargs, dict):
                client_kwargs["api_key"] = key
            client = getattr(agent, "client", None)
            if client is not None and hasattr(client, "api_key"):
                client.api_key = key
            _attribute(agent, entry)
            return True
        except Exception:
            return False

    def _interrupted(agent: Any) -> bool:
        try:
            return bool(getattr(agent, "_interrupt_requested", False))
        except Exception:
            return False

    STUB_ID = getattr(transport, "PARTIAL_STUB_ID", "partial-stream-stub")
    NS = types.SimpleNamespace

    def _chunk(content=None, tool_calls=None, finish=None):
        delta = NS(role="assistant", content=content, tool_calls=tool_calls)
        return NS(id="c", model="m", object="chat.completion.chunk",
                  choices=[NS(index=0, delta=delta, finish_reason=finish)], usage=None)

    def _tool_delta(index, call):
        function = getattr(call, "function", None)
        return NS(index=index, id=getattr(call, "id", None) or f"call_{index}", type="function",
                  function=NS(name=getattr(function, "name", None) or getattr(call, "name", None),
                              arguments=getattr(function, "arguments", "") or ""))

    class _AgentClient:
        """The legacy host function, seen from the client the way Hermes now sees it.

        Non-streaming: called as-is. Streaming (the agent has a delivery funnel,
        ``_fire_stream_delta``): every delta the host would have delivered is
        captured instead and replayed as a provider chunk, a returned
        partial-stream stub becomes what it is on the wire — a stream that
        ends without a finish — and the final answer becomes its finish chunk.
        """

        def __init__(self, call: "_AgentCall", key: str) -> None:
            self._call = call
            self._key = key
            self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self._create))

        def _create(self, **request: Any) -> Any:
            call = self._call
            if self._key:
                _apply_key(call.agent, self._key, call.entry(self._key))
            streaming = bool(request.pop("stream", False))
            if not streaming:
                return call.original(call.agent, request, *call.args, **call.extra)
            agent = call.agent
            captured: List[str] = []
            error: Optional[BaseException] = None
            result: Any = None
            # Text a legacy host delivers — through the class funnel, or
            # straight to ``stream_delta_callback`` — is what the provider
            # streamed; capture it so it arrives as chunks, as it does now.
            def capture(text, *a, **k):
                if isinstance(text, str) and text:
                    captured.append(text)
                return None

            funnel = callable(getattr(type(agent), "_fire_stream_delta", None))
            name = "_fire_stream_delta" if funnel else "stream_delta_callback"
            had_own = name in getattr(agent, "__dict__", {})
            own = agent.__dict__.get(name) if had_own else None
            setattr(agent, name, capture)
            try:
                result = call.original(agent, request, *call.args, **call.extra)
            except BaseException as exc:  # noqa: BLE001 - replayed below
                error = exc
            finally:
                if had_own:
                    setattr(agent, name, own)
                else:
                    try:
                        delattr(agent, name)
                    except AttributeError:
                        pass
            if error is not None and not captured:
                raise error

            def chunks():
                for text in captured:
                    yield _chunk(content=text)
                if error is not None:
                    raise error
                if getattr(result, "id", None) == STUB_ID:
                    for index, name in enumerate(getattr(result, "_dropped_tool_names", None) or []):
                        yield _chunk(tool_calls=[NS(index=index, id=f"call_{index}", type="function",
                                                    function=NS(name=name, arguments='{"'))])
                    # Hermes returned this stub where the provider's stream ended
                    # without a finish; at the client that is exactly what is seen.
                    return
                choices = getattr(result, "choices", None) or []
                if not choices:
                    return
                message = getattr(choices[0], "message", None)
                content = getattr(message, "content", None)
                got = "".join(captured)
                if content and not captured:
                    yield _chunk(content=content)
                elif isinstance(content, str) and len(content) > len(got) and content.startswith(got):
                    # The final message carries words the host never streamed.
                    yield _chunk(content=content[len(got):])
                for index, tc in enumerate(getattr(message, "tool_calls", None) or []):
                    yield _chunk(tool_calls=[_tool_delta(index, tc)])
                yield _chunk(finish=getattr(choices[0], "finish_reason", None) or "stop")

            return chunks()

    class _AgentCall(transport.Call):
        def __init__(self, original, agent, api_kwargs, args, extra) -> None:
            self.original = original
            self.agent = agent
            self.args = tuple(args or ())
            self.extra = dict(extra or {})
            self.kwargs = api_kwargs if isinstance(api_kwargs, dict) else {}
            self.provider = str(getattr(agent, "provider", "") or "")
            self.identity = carousel.Carousel.identity(self.provider, getattr(agent, "model", ""))
            self._entries: Dict[str, Any] = {}

        def keys(self) -> List[str]:
            keys, self._entries = candidates(self.agent)
            return keys

        def entry(self, key: str) -> Any:
            return self._entries.get(key)

        def client_for(self, key: str, attempt: int) -> Any:
            return _AgentClient(self, key)

        def cancelled(self) -> bool:
            return _interrupted(self.agent)

        def status_owner(self) -> str:
            return _Spinner.key_for(self.agent)

    def _response(content, tool_calls, finish, rid):
        message = NS(content=content, tool_calls=tool_calls, role="assistant")
        return NS(id=rid, model="m", choices=[NS(index=0, message=message, finish_reason=finish)], usage=None)

    def _stub(content, tools):
        """What Hermes builds from a stream that stopped without a finish."""
        stub = _response(content, None, "length", STUB_ID)
        names = [t.function.name for t in tools if getattr(t.function, "name", None)]
        if names:
            stub._dropped_tool_names = names
        return stub

    class DispatchBinding(transport.KameTransport):
        def __init__(self, *, engine=None, sleep=None, jitter=None) -> None:
            super().__init__(engine=engine, sleep=sleep, jitter=jitter)
            self.installed = False
            self.reason = "not installed"

        def __setattr__(self, name: str, value: Any) -> None:
            # 1.8.1.8 tests stub the wait with a plain function returning a
            # bool; the transport's wait is a generator (it yields keep-alives).
            if name == "_wait_for_recovery" and callable(value):
                plain = value

                def _as_generator(*args: Any, **kwargs: Any):
                    result = plain(*args, **kwargs)
                    if hasattr(result, "__next__"):
                        return (yield from result)
                    return result

                value = _as_generator
            super().__setattr__(name, value)

        def install(self, module: Any = None) -> bool:
            # 1.8.1.8's instance ``install`` wrapped two functions and pushed no
            # dials; only the module-level ``install()`` did (below). Keeping
            # that split keeps the process-wide shared-health default where the
            # old tests expect it.
            self.installed = True
            self.reason = "active"
            return True

        def uninstall(self) -> bool:
            self.installed = False
            self.reason = "not installed"
            return True

        def run(self, original, agent, api_kwargs, args: Sequence[Any] = (), kwargs: Optional[Dict[str, Any]] = None) -> Any:
            token = _AGENT.set(agent)
            try:
                return self._run(original, agent, api_kwargs, args, kwargs)
            finally:
                _AGENT.reset(token)

        def _run(self, original, agent, api_kwargs, args: Sequence[Any] = (), kwargs: Optional[Dict[str, Any]] = None) -> Any:
            call = _AgentCall(original, agent, api_kwargs, args, kwargs)
            funnel = getattr(type(agent), "_fire_stream_delta", None)
            callback = getattr(agent, "stream_delta_callback", None)
            if callable(funnel):
                fire = funnel
            elif callable(callback):
                fire = lambda _agent, text: callback(text)  # noqa: E731
            else:
                fire = None
            if fire is None:
                try:
                    return self.complete(call)
                except InterruptedError:
                    # The transport hands a stop to Hermes; Hermes' own call
                    # path is what handles it, which in these tests is the host
                    # function — exactly where 1.8.1.8 sent it.
                    if _interrupted(agent):
                        return original(agent, api_kwargs, *call.args, **call.extra)
                    raise
            # Streaming: what reaches the screen is what the transport yields,
            # delivered through the agent's own funnel, as Hermes does.
            shown: List[str] = []
            tools: List[Any] = []
            finish = None
            try:
                for chunk in self.stream(call):
                    choices = getattr(chunk, "choices", None) or []
                    if not choices:
                        continue
                    delta = choices[0].delta
                    text = getattr(delta, "content", None)
                    if text:
                        shown.append(text)
                        fire(agent, text)
                    for tc in getattr(delta, "tool_calls", None) or []:
                        tools.append(NS(id=tc.id, type="function",
                                        function=NS(name=tc.function.name, arguments=tc.function.arguments)))
                    if choices[0].finish_reason:
                        finish = choices[0].finish_reason
            except InterruptedError:
                if shown or tools:
                    return _stub("".join(shown), tools)
                if _interrupted(agent):
                    return original(agent, api_kwargs, *call.args, **call.extra)
                raise
            except BaseException:
                # Hermes keeps what it already streamed when the stream fails or
                # the turn is stopped after text; that is the stub it returns.
                if shown or tools:
                    return _stub("".join(shown), tools)
                raise
            content = "".join(shown)
            if finish is None:
                return _stub(content, tools) if (content or tools) else _response(content, None, None, "chatcmpl-kame")
            return _response(content, tools or None, finish, "chatcmpl-kame")

    def install(module: Any = None) -> Optional[DispatchBinding]:
        if settings.is_on(settings.ROTATION_DISABLED) or settings.is_on(settings.CAROUSEL_DISABLED):
            return None
        if module is None:
            # Nothing of Hermes' to bind to (1.8.1.8: "outside a Hermes declines
            # quietly"). Configuring here would switch on the process-wide
            # shared pool health for every later test in this process.
            return None
        binding = DispatchBinding()
        transport.configure(binding)
        binding.install(module)
        return binding

    class _Spinner(transport._Spinner):
        """1.8.1.8's spinner API over 1.8.1.9's ``transport._Spinner``.

        Same throttle, same state; 1.8.1.8 keyed it by the agent's session and
        drew through the agent, which is what ``key_for`` and the router above
        supply.
        """

        @staticmethod
        def key_for(agent: Any) -> str:
            session = getattr(agent, "session_id", None)
            if session:
                return str(session)
            return f"anon:{id(agent):x}"

        @classmethod
        def update(cls, agent: Any, text: str, *, interval: Any = None) -> None:
            token = _AGENT.set(agent)
            try:
                transport._Spinner.update(cls.key_for(agent), text, interval=interval)
            finally:
                _AGENT.reset(token)

        @classmethod
        def reset(cls, session_id: Any = None) -> None:
            transport._Spinner.reset(session_id)

    class _Vigil(transport._Vigil):
        """1.8.1.8's ``_Vigil(agent, label)``: the notice reaches that agent's status rail."""

        __slots__ = ("agent",)

        def __init__(self, agent: Any, label: str) -> None:
            super().__init__(label)
            self.agent = agent

        def _emit(self, message: str) -> None:
            token = _AGENT.set(self.agent)
            try:
                super()._emit(message)
            finally:
                _AGENT.reset(token)

    class _Forwarding(types.ModuleType):
        """Reads and ``monkeypatch.setattr`` both land on ``transport`` itself."""

        def __getattr__(self, name: str) -> Any:
            return getattr(transport, name)

        def __setattr__(self, name: str, value: Any) -> None:
            if name not in self.__dict__ and hasattr(transport, name):
                setattr(transport, name, value)
            else:
                super().__setattr__(name, value)

        def __delattr__(self, name: str) -> None:
            if name not in self.__dict__ and hasattr(transport, name):
                delattr(transport, name)
            else:
                super().__delattr__(name)

    module = _Forwarding(f"{package}.dispatch_binding", __doc__)
    for name, value in {
        "DispatchBinding": DispatchBinding,
        "install": install,
        "candidates": candidates,
        "_entries_of": _entries_of,
        "_apply_key": _apply_key,
        "_attribute": _attribute,
        "_interrupted": _interrupted,
        "_AgentCall": _AgentCall,
        "_Spinner": _Spinner,
        "_Vigil": _Vigil,
        "__file__": transport.__file__,
        "__package__": package,
    }.items():
        types.ModuleType.__setattr__(module, name, value)
    return module


class Finder:
    """``import <package>.dispatch_binding`` for a package that has ``transport``."""

    @staticmethod
    def find_spec(fullname: str, path: Any = None, target: Any = None):
        if not fullname.endswith(".dispatch_binding"):
            return None
        package = fullname.rsplit(".", 1)[0]
        parent = sys.modules.get(package)
        if parent is None or not hasattr(parent, "__path__"):
            return None
        roots = list(parent.__path__)
        if any(os.path.exists(os.path.join(r, "dispatch_binding.py")) for r in roots):
            return None  # an older plugin version that still has the real one
        if not any(os.path.exists(os.path.join(r, "transport.py")) for r in roots):
            return None
        return importlib.machinery.ModuleSpec(fullname, _Loader(package))


class _Loader:
    def __init__(self, package: str) -> None:
        self.package = package

    def create_module(self, spec):
        return build(self.package)

    def exec_module(self, module) -> None:
        return None


def enable() -> None:
    if not any(isinstance(f, Finder) or f is Finder for f in sys.meta_path):
        sys.meta_path.insert(0, Finder)
