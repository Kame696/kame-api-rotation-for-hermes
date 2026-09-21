"""Offline current-host contracts for REQ-707-06/07.

Fresh subprocess only: temporary HERMES_HOME, recording disabled, no bytecode,
socket connections denied. Real host functions run on synthetic in-memory agents;
construction, persistence and transport boundaries are replaced explicitly.
Mutation success means a named contract assertion failed, not an import/crash.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack, nullcontext
import hashlib
import importlib
import inspect
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
_CONTRACT_LOOPS = []


class ContractViolation(AssertionError):
    pass


def require(condition, message):
    if not condition:
        raise ContractViolation(message)


def load(name):
    return importlib.import_module(name)


def agent_fixture():
    """No AIAgent constructor: it would discover credentials and installed plugins."""
    a = load("run_agent").AIAgent.__new__(load("run_agent").AIAgent)
    for key, value in dict(model="contract-model", provider="openai", api_mode="chat_completions",
            api_key="synthetic-a", base_url="https://contract.invalid/v1", session_id="contract",
            platform="test", log_prefix="", quiet_mode=True, verbose_logging=False,
            thinking_callback=None, status_callback=None, stream_delta_callback=None,
            _stream_callback=None, _interrupt_requested=False, _fallback_chain=[],
            _fallback_index=0, _empty_content_retries=0, _credential_pool=None,
            _client_kwargs={}).items():
        setattr(a, key, value)
    for name in ("_vprint", "_buffer_status", "_buffer_vprint", "_flush_status_buffer",
                 "_persist_session", "_cleanup_task_resources", "_touch_activity",
                 "_enqueue_stream_hook"):
        setattr(a, name, Mock())
    a._try_activate_fallback = Mock(return_value=False)
    a._has_pending_fallback = Mock(return_value=False)
    return a


def call_phase(fn, agent, **overrides):
    """Explicit fixture fields; an added required host argument fails closed."""
    values = dict(response=NS(choices=[]), _retry=load("agent.turn_retry_state").TurnRetryState(),
        thinking_spinner=None, messages=[], api_messages=[], api_kwargs={}, system_message=None,
        active_system_prompt="system", conversation_history=[], finish_reason="stop", retry_count=0,
        max_retries=1, compression_attempts=0, max_compression_attempts=2,
        length_continue_retries=0, truncated_response_parts=[], truncated_tool_call_retries=0,
        current_turn_user_idx=0, api_call_count=1, api_request_id="req", api_start_time=0.0,
        api_duration=0.1, effective_task_id="task", turn_id="turn", approx_tokens=10,
        _preflight_compression_blocked=False, _last_preflight_pressure=None,
        error_details=["empty choices"])
    values.update(overrides)
    return fn(agent, **{n: values[n] for n in inspect.signature(fn).parameters if n != "agent"})


def empty_retry(broken=False):
    m = load("agent.turn_empty_response")
    a = agent_fixture()
    a._credential_pool = Mock()
    a._swap_credential = Mock()
    original = m._retry_empty
    def altered(*args, **kwargs):
        result = original(*args, **kwargs)
        args[0].api_key = "synthetic-b"
        return result
    with patch.object(m, "interruptible_backoff_sleep", return_value=None), \
         patch.object(m, "_retry_empty", altered if broken else original):
        first = m._retry_empty(a, NS(usage=None), "stop", True, messages=[],
                              conversation_history=[], api_call_count=1)
        require(first[0] == "continue" and a._empty_content_retries == 1, "empty retry is scheduled")
        require(a.api_key == "synthetic-a" and not a._credential_pool.mock_calls
                and not a._swap_credential.called, "empty retry preserves credential")
        second = m._retry_empty(a, NS(usage=None), "stop", True, messages=[],
                               conversation_history=[], api_call_count=2)
        require(second == (None, None, True), "deterministic empties stop the retry streak")
    return "First empty retries same key; second deterministic empty ends this retry branch; fallback remains separate."


def pool_selection(broken=False):
    m = load("agent.agent_runtime_helpers")
    a = agent_fixture()
    entry = NS(id="entry-b", provider="openai", runtime_api_key="synthetic-b")
    # ``**_`` because Hermes 0.21.3 passes ``model=`` to both (per-model
    # cooldowns); a stub that refuses it fails the host, not the contract.
    pool = NS(provider="openai", has_available=lambda **_: True, select=Mock(return_value=entry),
              next_available_at=lambda **_: None)
    a._credential_pool = pool
    a._swap_credential = Mock()
    a._fallback_activated = False
    require(m.restore_primary_runtime(a) is False and pool.select.call_count == 0,
            "no selection without fallback restore")
    a._fallback_activated = True
    a._primary_runtime = dict(model=a.model, provider=a.provider, base_url=a.base_url,
        api_mode=a.api_mode, api_key=a.api_key, client_kwargs={}, use_prompt_caching=False,
        compressor_model=a.model, compressor_context_length=100, compressor_base_url=a.base_url,
        compressor_api_key=a.api_key, compressor_provider=a.provider)
    a.context_compressor = NS(update_model=Mock())
    original = m._rebind_primary_credential_pool
    def altered(*args):
        original(*args)
        pool.select()
    with patch.object(m, "_rebuild_primary_client"), \
         patch.object(m, "_rebind_primary_credential_pool", altered if broken else original), \
         patch.object(load("agent.chat_completion_helpers"), "rewrite_prompt_model_identity"):
        require(m.restore_primary_runtime(a) is True, "restore completed")
        require(pool.select.call_count == 1 and a._swap_credential.call_args.args == (entry,),
                "one selection per actual fallback restore")
        require(m.restore_primary_runtime(a) is False and pool.select.call_count == 1,
                "repeated no-op restore does not select")
    return "Zero selections without fallback; one on primary restore; repeated restore is a no-op (not once every turn)."


def credential_swap(broken=False):
    cls = load("run_agent").AIAgent
    original = cls._swap_credential
    def altered(a, entry):
        original(a, entry)
        a.api_key = "synthetic-a"
    with patch.object(cls, "_swap_credential", altered if broken else original):
        for mode in ("chat_completions", "anthropic_messages"):
            a = agent_fixture()
            a.api_mode = mode
            a._reapply_route_client_config = Mock()
            a._replace_primary_openai_client = Mock()
            a._anthropic_client = Mock()
            a._build_direct_anthropic_client = Mock(return_value=NS())
            a._anthropic_oauth_flag = Mock(return_value=False)
            entry = NS(id="entry-b", runtime_api_key="synthetic-b", runtime_base_url="https://new.invalid/v1/")
            a._swap_credential(entry)
            require((a.api_key, a.base_url, a._credential_pool_entry_id) ==
                    ("synthetic-b", "https://new.invalid/v1", "entry-b"), "live identity replaced")
            if mode == "chat_completions":
                require(a._client_kwargs == {"api_key": "synthetic-b", "base_url": "https://new.invalid/v1"}
                        and a._replace_primary_openai_client.call_count == 1
                        and a._reapply_route_client_config.call_args.kwargs == {"route_changed": True},
                        "OpenAI client configuration rebuilt")
            else:
                require(a._anthropic_api_key == "synthetic-b" and a._build_direct_anthropic_client.call_count == 1,
                        "Anthropic client configuration rebuilt")
    return "Actual inherited swap replaces key, route, entry id and rebuild inputs for OpenAI and Anthropic; restore is another caller."


def content_refusal(broken=False):
    m = load("agent.turn_response_check")
    a = agent_fixture()
    a._invoke_api_request_error_hook = Mock()
    response = NS(content="policy message", tool_calls=None, finish_reason="content_filter")
    a._get_transport = lambda: NS(normalize_response=lambda r: r)
    a._should_treat_stop_as_truncated = lambda *args: False
    original = m.handle_content_policy_refusal
    def altered(*args, **kwargs):
        result = original(*args, **kwargs)
        result.action = "break"
        return result
    with patch.object(load("agent.turn_recovery"), "validate_response_shape", return_value=(False, [])), \
         patch.object(m, "handle_content_policy_refusal", altered if broken else original):
        result = call_phase(m.check_api_response, a, response=response)
    require(result.action == "return" and result.result["completed"] is False,
            "content refusal returns terminal verdict before response intake")
    require(a._invoke_api_request_error_hook.call_count == 1 and
            a._invoke_api_request_error_hook.call_args.kwargs["retryable"] is False,
            "content refusal reports nonretryable error")
    return "Actual response check returns terminal content-policy result, before success intake; persistence is intercepted."


def success_fields(broken=False):
    m, lifecycle = load("agent.turn_response_intake"), load("hermes_cli.lifecycle")
    a = agent_fixture()
    a._api_response_payload_for_hook = lambda *args, **kwargs: {"synthetic": True}
    a._usage_summary_for_api_request_hook = lambda *args: {}
    seen = []
    def sink(event, **fields):
        if broken:
            fields.pop("assistant_content_chars", None)
        seen.append((event, fields))
    with patch.object(lifecycle, "has_hook", return_value=True), patch.object(lifecycle, "invoke_hook", sink):
        for text, calls, finish in [("hello", [NS(id="t")], "tool_calls"), ("", [], "stop")]:
            call_phase(m._fire_post_api_request_hook, a, response=NS(model="reply-model"),
                       assistant_message=NS(content=text, tool_calls=calls), finish_reason=finish)
            require(len(seen) == (1 if text else 2), "success hook dispatched exactly once")
            event, row = seen[-1]
            require(event == "post_api_request" and
                    (row.get("assistant_content_chars"), row.get("assistant_tool_call_count"), row.get("finish_reason"))
                    == (len(text), len(calls), finish), "success fields preserve values including zero")
    return "Real success dispatch preserves content/tool counts and finish reason, including legitimate zero values."


def error_classification(broken=False):
    m = load("agent.turn_api_error")
    classifier = load("agent.error_classifier")
    events = []
    original = classifier.classify_api_error
    def classify(*args, **kwargs):
        events.append("classified")
        return original(*args, **kwargs)
    def sink(**kwargs):
        events.append(kwargs["error_type"])
    a = agent_fixture()
    a._invoke_api_request_error_hook = sink
    # Invalid body uses its real terminal handler, not a classifier.
    with patch.object(classifier, "classify_api_error", classify), \
         patch.object(m, "classify_api_error", classify):
        call_phase(load("agent.turn_response_check").retry_invalid_response, a)
        require(events == ["InvalidAPIResponse"], "invalid response bypasses classifier")
        events.clear()
        response = NS(content="policy message", tool_calls=None)
        a._get_transport = lambda: NS(normalize_response=lambda r: r)
        call_phase(load("agent.turn_truncation").handle_content_policy_refusal, a, response=response)
        require(events == ["ContentPolicyBlocked"], "HTTP 200 policy result bypasses classifier")
        events.clear()
        a._extract_api_error_context = lambda e: {}
        import httpx
        from openai import RateLimitError
        error = RateLimitError("synthetic quota", response=httpx.Response(429,
            request=httpx.Request("POST", "https://contract.invalid")), body={})
        def recovery(*args, **kwargs):
            return True, False
        with patch.object(m, "recover_before_classification", return_value=(False, "system")), \
             patch.object(m, "recover_after_classification", recovery):
            if broken:
                a._invoke_api_request_error_hook = lambda **kwargs: None
            result = call_phase(m.handle_api_error, a, api_error=error)
        require(events == ["classified", "RateLimitError"] and result.action == "continue",
                "exception is classified before error report and recovery")
    return "Invalid body and HTTP-200 policy handlers bypass classification; synthetic 429 exception classifies before report/recovery."


def status_delivery(broken=False):
    a = agent_fixture()
    cls = load("run_agent").AIAgent
    original = cls._emit_status
    with patch.object(cls, "_emit_status", (lambda *args: None) if broken else original):
        a.status_callback = Mock()
        a._emit_status("waiting")
        require(a.status_callback.call_args == (("lifecycle", "waiting"), {}) and a._vprint.called,
                "status reaches print and callback")
        a._vprint.side_effect = RuntimeError("broken display")
        a.status_callback.side_effect = RuntimeError("broken status consumer")
        a._emit_status("waiting again")
        require(a.status_callback.call_count == 2, "print failure does not suppress status callback")
        a.thinking_callback = Mock(side_effect=RuntimeError("broken wait consumer"))
        a._emit_wait_notice("wait notice")
        require(a.thinking_callback.call_count == 1 and a._touch_activity.called,
                "wait callback is attempted and exceptions contained")
    return "Inherited lifecycle and wait methods deliver to their callbacks; failing print/callbacks do not abort status delivery."


def stream_retry(broken=False):
    m = load("agent.chat_completion_helpers")
    import httpx
    from openai import RateLimitError, AuthenticationError
    errors = [httpx.ReadTimeout("timeout"), httpx.ConnectError("disconnect"),
        RateLimitError("quota", response=httpx.Response(429, request=httpx.Request("POST", "https://contract.invalid")), body={}),
        AuthenticationError("auth", response=httpx.Response(401, request=httpx.Request("POST", "https://contract.invalid")), body={})]
    original = m._StreamingCall._handle_stream_error
    def altered(self, error, attempt, retries):
        if getattr(error, "status_code", None) == 429:
            return True
        return original(self, error, attempt, retries)
    with patch.object(m._StreamingCall, "_handle_stream_error", altered if broken else original):
        for i, error in enumerate(errors):
            a = agent_fixture()
            a._is_provider_stream_parse_error = lambda e: False
            call = m._StreamingCall(a, {}, None)
            call._retry_after_drop = Mock()
            call._maybe_disable_streaming = Mock()
            retry = call._handle_stream_error(error, 0, 2)
            require(retry is (i < 2), "transient reconnect only; 429/401 propagate")
            require(call._retry_after_drop.call_count == (1 if i < 2 else 0), "reconnect branch count")
            if i >= 2:
                require(call.result["error"] is error, "status exception identity preserved")
            call._request_cancelled["value"] = True
            require(call._handle_stream_error(error, 0, 2) is False, "cancelled request never reconnects")
    return "Timeout/disconnect reconnect; 429/401 reach outer recovery without reconnect; cancellation suppresses retries."


def partial_stream(broken=False):
    m = load("agent.chat_completion_helpers")
    stub_id = load("hermes_constants").PARTIAL_STREAM_STUB_ID
    a = agent_fixture()
    call = m._StreamingCall(a, {}, None)
    original = m._build_partial_stream_stub
    def altered(*args, **kwargs):
        result = original(*args, **kwargs)
        result.id = "untagged"
        return result
    with patch.object(m, "_build_partial_stream_stub", altered if broken else original):
        result = call._finish_chat_stream(NS(), "assistant", ["partial text"], [], {}, None,
            a.model, None, flush_pending=lambda: None)
        require(result.id == stub_id and result.choices[0].finish_reason == "length"
                and result.choices[0].message.content == "partial text"
                and result.choices[0].message.tool_calls is None, "EOF drop returns tagged text-only length stub")
        # Exercise run() outcome selection with request/monitor replaced, no socket/worker.
        a._current_streamed_assistant_text = "partial text"
        call.result["error"] = ConnectionError("synthetic drop")
        call.deltas_were_sent["yes"] = True
        with patch.object(m, "should_use_direct_api_call", return_value=True), \
             patch.object(call, "_resolve_stale_timeout"), patch.object(call, "_monitor_loop"), \
             patch.object(call, "_run_call"):
            result = call.run()
            require(result.id == stub_id and result.choices[0].message.content == "partial text",
                    "delivered-text exception returns stub")
            call.deltas_were_sent["yes"] = False
            try:
                call.run()
            except ConnectionError as error:
                require(error is call.result["error"], "no-text exception identity preserved")
            else:
                require(False, "no-text failure raises")
    return "EOF and delivered-text exception return tagged stubs; same exception before delivery raises."


def continuation_row(broken=False):
    m = load("agent.turn_truncation")
    a = agent_fixture()
    a._build_assistant_message = lambda msg, finish: {"role": "assistant", "content": msg.content}
    st = m._Trunc(agent=a, response=NS(id=load("hermes_constants").PARTIAL_STREAM_STUB_ID),
        finish_reason="length", conversation_history=[], api_call_count=1, effective_task_id="task",
        current_turn_user_idx=0, messages=[{"role": "user", "content": "question"}],
        length_continue_retries=0, truncated_response_parts=[], truncated_tool_call_retries=0,
        retry_count=0, compression_attempts=0)
    retry = load("agent.turn_retry_state").TurnRetryState()
    original = m.append_message
    def altered(messages, row):
        row.pop("_length_continuation_nudge", None)
        return original(messages, row)
    with patch.object(m, "append_message", altered if broken else original):
        result = m._continue_text(st, retry, NS(content="partial text"))
    require(result.action == "break" and retry.restart_with_length_continuation,
            "continuation restarts request")
    require(len(st.messages) == 3 and st.messages[-1].get("_length_continuation_nudge") is True
            and st.messages[-1]["role"] == "user" and st.messages[-2]["content"] == "partial text",
            "continuation adds synthetic user row and preserves partial text")
    projected = load("gateway.platforms.api_server")._project_client_message(st.messages[-1])
    require(projected.get("content") == st.messages[-1]["content"]
            and projected.get("display_kind") != "hidden",
            "current host projection retains continuation text as a visible row")
    return "Row append and actual API projection execute: current host does not hide this nudge. Baseline limitation, not KAME-added projection behavior; screen/ordinal reconciliation remains untested."


def visible_delivery(broken=False):
    cls = load("run_agent").AIAgent
    original = cls._deliver_to_stream_callbacks
    def altered(a, text):
        return a._call_quietly(a.stream_delta_callback, text)
    with patch.object(cls, "_deliver_to_stream_callbacks", altered if broken else original):
        a = agent_fixture()
        a.stream_delta_callback = Mock()
        a._stream_callback = Mock()
        a._fire_stream_delta("visible")
        require(a.stream_delta_callback.call_count == a._stream_callback.call_count == 1
                and a._current_streamed_assistant_text == "visible", "one funnel fans out once and records once")
        a.stream_delta_callback.side_effect = RuntimeError("display failed")
        a._fire_stream_delta(" second")
        require(a._stream_callback.call_count == 2 and a._current_streamed_assistant_text == "visible second",
                "one failing consumer does not block other consumer")
        a._stream_callback.side_effect = RuntimeError("TTS failed")
        a._fire_stream_delta(" not delivered")
        require(a._current_streamed_assistant_text == "visible second", "failed delivery is not recorded as visible")
    return "Inherited funnel sends once to each consumer, records once, isolates failures and does not record undelivered text."


def off_event_loop(broken=False):
    m = load("gateway.platforms.api_server")
    main_thread = threading.get_ident()
    released = threading.Event()
    def blocking_turn(**kwargs):
        require(threading.get_ident() != main_thread, "agent executes outside event loop")
        require(released.wait(2), "event loop remains responsive while worker waits")
        return {"final_response": "done"}
    agent = NS(run_conversation=blocking_turn)
    adapter = NS(_profile_scope=lambda *args: nullcontext(),
        _bind_api_server_session=lambda **kwargs: {}, _create_agent=lambda **kwargs: agent,
        _active_run_agents={}, _shutdown_interruptible_agents={}, _inflight_agent_runs=0,
        _activate_admitted_request=lambda: None,
        _finish_turn_result=lambda a, result, sid, **kwargs: (result, {}))
    async def drive():
        loop = asyncio.get_running_loop()
        async def heartbeat():
            await asyncio.sleep(0.02)
            released.set()
        def inline_executor(executor, fn, *args):
            future = loop.create_future()
            try:
                future.set_result(fn(*args))
            except Exception as exc:
                future.set_exception(exc)
            return future
        task = asyncio.create_task(heartbeat())
        try:
            with patch.object(loop, "run_in_executor", inline_executor) if broken else nullcontext():
                result, usage = await m.APIServerAdapter._run_agent(adapter, "synthetic", [], session_id="test")
            require(result["final_response"] == "done", "worker result preserved")
        finally:
            released.set()
            await task
        require(adapter._inflight_agent_runs == 0 and not adapter._shutdown_interruptible_agents,
                "turn ownership cleaned after completion")
    with patch.object(m, "_publish_turn_process_ownership"), patch.object(m, "_clear_turn_process_ownership"), \
         patch.object(load("gateway.session_context"), "clear_session_vars"):
        loop = _CONTRACT_LOOPS.pop(0)
        try:
            loop.run_until_complete(drive())
        finally:
            loop.run_until_complete(loop.shutdown_default_executor())
            loop.close()
    return "Actual API-server turn executor keeps event loop responsive during a blocked synthetic agent; cleanup verified."


CONTRACTS = {
    "empty_retry": (empty_retry, "agent.turn_empty_response", "_retry_empty"),
    "pool_selection": (pool_selection, "agent.agent_runtime_helpers", "restore_primary_runtime"),
    "credential_swap": (credential_swap, "agent.client_lifecycle", "ClientLifecycleMixin._swap_credential"),
    "content_refusal": (content_refusal, "agent.turn_response_check", "check_api_response"),
    "success_fields": (success_fields, "agent.turn_response_intake", "_fire_post_api_request_hook"),
    "error_classification": (error_classification, "agent.turn_api_error", "handle_api_error"),
    "status_delivery": (status_delivery, "agent.status_output", "StatusOutputMixin._emit_status_kind"),
    "stream_retry": (stream_retry, "agent.chat_completion_helpers", "_StreamingCall._handle_stream_error"),
    "partial_stream": (partial_stream, "agent.chat_completion_helpers", "_StreamingCall.run"),
    "continuation_row": (continuation_row, "agent.turn_truncation", "_continue_text"),
    "visible_delivery": (visible_delivery, "agent.stream_delivery", "StreamDeliveryMixin._fire_stream_delta"),
    "off_event_loop": (off_event_loop, "gateway.platforms.api_server", "APIServerAdapter._run_agent"),
}


def evaluate(operation, broken=False):
    try:
        return {"passed": True, "detail": operation(broken)}
    except Exception as exc:
        import traceback
        return {"passed": False, "kind": type(exc).__name__, "detail": str(exc),
                "traceback": traceback.format_exc()}


def run(host):
    sys.path.insert(0, str(host))
    results, mutations, sources = {}, {}, {}
    for name, (operation, module_name, member) in CONTRACTS.items():
        results[name] = evaluate(operation)
        mutations[name] = evaluate(operation, True)
        try:
            obj = load(module_name)
            for part in member.split("."):
                obj = getattr(obj, part)
            path = Path(inspect.getsourcefile(obj))
            sources[name] = {"path": str(path), "line": inspect.getsourcelines(obj)[1],
                             "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        except Exception as exc:
            sources[name] = {"error": type(exc).__name__}
    detected = {name: row.get("kind") == "ContractViolation" for name, row in mutations.items()}
    return {"host": str(host), "network": "socket.connect and DNS denied",
        "isolation": "temporary HERMES_HOME; recording off; no bytecode; host write guard",
        "checks": results, "negative_controls": mutations, "mutation_detected": detected,
        "sources": sources,
        "retained_alerts": {"continuation_row": "Actual host projection retains visible nudge text. Existing host limitation; screen/ordinal reconciliation not executed."},
        "passed": all(r["passed"] for r in results.values()) and all(detected.values())}


def install_guards(host):
    host = host.resolve()
    def audit(event, args):
        if event in {"socket.connect", "socket.getaddrinfo", "subprocess.Popen"}:
            raise RuntimeError("offline contract blocked " + event)
        if event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
            path = Path(os.fsdecode(args[0])).resolve()
            if path.is_relative_to(host):
                mode, flags = args[1], args[2]
                if (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                    isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)):
                    raise RuntimeError("host write blocked")
                if path.name in {".env", "auth.json", "credentials.json"}:
                    raise RuntimeError("host credential read blocked")
        if event in {"os.remove", "os.rmdir", "os.mkdir", "os.rename"}:
            for value in args[:2 if event == "os.rename" else 1]:
                if isinstance(value, (str, bytes, os.PathLike)) and Path(os.fsdecode(value)).resolve().is_relative_to(host):
                    raise RuntimeError("host mutation blocked")
    sys.addaudithook(audit)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", type=Path, default=Path(os.environ.get("KAME_HERMES_ROOT",
                        str(Path.home() / "AppData/Local/hermes/hermes-agent"))))
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    # Python3.11's Windows platform probe invokes `ver` on first use. Prime
    # stdlib-only host metadata before forbidding child processes; no Hermes
    # module or credential is loaded here. Contracts themselves stay offline.
    import platform
    platform.uname()
    # Windows asyncio uses loopback socketpairs for its own wakeup pipe. Create
    # only those stdlib loops before guards; no application sockets are allowed.
    _CONTRACT_LOOPS.extend(asyncio.new_event_loop() for _ in range(2))
    with tempfile.TemporaryDirectory(prefix="kame-runtime-contract-") as home:
        os.environ.update(HERMES_HOME=home, KAME_RECORDER_DISABLED="1",
                          KAME_CALL_TIMINGS_DISABLED="1", PYTHONDONTWRITEBYTECODE="1")
        install_guards(args.host)
        result = run(args.host)
    if args.out:
        args.out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
