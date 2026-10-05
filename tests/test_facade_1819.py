"""1.8.1.9 — the client Hermes is handed, over real HTTP, against fake providers.

Needs a Hermes source tree on ``PYTHONPATH`` (the client classes subclass
Hermes' own ``GeminiNativeClient`` and the OpenAI SDK client Hermes ships);
skipped without one. Every provider here is a local HTTP server — no request
leaves the machine and no real key is used.

What is pinned:

* the client has the shape Hermes expects (``isinstance``, the wire flags);
* a comma-joined key is split, and a 429 on one key is answered by the next,
  for both the OpenAI-compatible and the native Gemini wire;
* Google's quota window survives the streaming error path (the field 1.8.1.8
  needed ``quota_id_binding`` to keep);
* KAME declines — and Hermes builds its own client — wherever the request may
  need a wire other than chat completions.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytest.importorskip("agent.gemini_native_adapter")
pytest.importorskip("openai")

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_facade_1819_under_test"


def _load_package():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_load_package()
facade = importlib.import_module(f"{PACKAGE}.facade")
carousel_mod = importlib.import_module(f"{PACKAGE}.core.carousel")
evidence = importlib.import_module(f"{PACKAGE}.core.evidence")
classify = importlib.import_module(f"{PACKAGE}.core.classify")
settings = importlib.import_module(f"{PACKAGE}.settings")

from agent.gemini_native_adapter import GeminiNativeClient  # noqa: E402
from openai import OpenAI  # noqa: E402

QUOTA_BODY = json.dumps({"error": {
    "code": 429, "status": "RESOURCE_EXHAUSTED",
    "message": "You exceeded your current quota, please check your plan and billing details.",
    "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{
            "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
            "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier", "quotaValue": "20"}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "41s"},
    ]}}).encode()


#: Fake keys in the real shape — ``core.multikey`` only splits values whose
#: parts look like credentials, so ``JOINED`` would (rightly) stay whole.
K1 = "AIzaSyFAKEFAKEFAKEFAKEFAKEFAKEFAKE00001"
K2 = "AIzaSyFAKEFAKEFAKEFAKEFAKEFAKEFAKE00002"
JOINED = f"{K1},{K2}"


class Provider:
    """A local provider. ``refuse`` holds the keys that answer 429."""

    def __init__(self, refuse=()):
        self.refuse = set(refuse)
        self.seen = []
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("content-length", 0)) or 0)
                key = (self.headers.get("x-goog-api-key")
                       or (self.headers.get("authorization") or "").replace("Bearer ", ""))
                provider.seen.append((self.path, key))
                if key in provider.refuse:
                    self.send_response(429)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(QUOTA_BODY)))
                    self.end_headers()
                    self.wfile.write(QUOTA_BODY)
                    return
                if "streamGenerateContent" in self.path:
                    events = [{"candidates": [{"content": {"role": "model", "parts": [{"text": "Hello "}]}}]},
                              {"candidates": [{"content": {"role": "model", "parts": [{"text": "world"}]},
                                               "finishReason": "STOP"}]}]
                    payload = b"".join(b"data: " + json.dumps(e).encode() + b"\r\n\r\n" for e in events)
                    self._send(payload, "text/event-stream")
                elif "generateContent" in self.path:
                    self._send(json.dumps({"candidates": [{"content": {"role": "model", "parts": [
                        {"text": "Hello world"}]}, "finishReason": "STOP"}]}).encode(), "application/json")
                elif json.loads(body or b"{}").get("stream"):
                    chunks = [
                        {"id": "x", "object": "chat.completion.chunk", "created": 0, "model": "m",
                         "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Hello "},
                                      "finish_reason": None}]},
                        {"id": "x", "object": "chat.completion.chunk", "created": 0, "model": "m",
                         "choices": [{"index": 0, "delta": {"content": "world"}, "finish_reason": "stop"}]},
                    ]
                    payload = b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks)
                    self._send(payload + b"data: [DONE]\n\n", "text/event-stream")
                else:
                    self._send(json.dumps({"id": "x", "object": "chat.completion", "created": 0, "model": "m",
                                           "choices": [{"index": 0, "finish_reason": "stop", "message": {
                                               "role": "assistant", "content": "Hello world"}}]}).encode(),
                               "application/json")

            def _send(self, payload, ctype):
                self.send_response(200)
                self.send_header("content-type", ctype)
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.root = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()


@pytest.fixture()
def fresh(monkeypatch):
    """A fresh carousel; the local server stands in for Google's host name."""
    import agent.gemini_native_adapter as adapter

    real = adapter.is_native_gemini_base_url
    monkeypatch.setattr(adapter, "is_native_gemini_base_url",
                        lambda url: "/v1beta" in str(url or "") or real(url))
    monkeypatch.setattr(facade.TRANSPORT, "engine", carousel_mod.Carousel())
    monkeypatch.setattr(facade, "_wire", lambda provider, base_url, declared="": "chat_completions")
    settings.forget()
    yield
    settings.forget()


def _text(stream):
    out = ""
    for chunk in stream:
        for choice in getattr(chunk, "choices", None) or []:
            out += getattr(choice.delta, "content", None) or ""
    return out


class TestTheShapeHermesExpects:
    def test_gemini(self, fresh):
        p = Provider()
        try:
            client = facade.make_client("gemini", {"api_key": JOINED, "base_url": p.root + "/v1beta"})
            assert isinstance(client, GeminiNativeClient)
            assert getattr(client, "HERMES_SKIP_TRANSPORT_WRAP", False) is True
            assert client.api_key == K1  # never the joined list
            client.close()
        finally:
            p.close()

    def test_openai_compatible(self, fresh):
        p = Provider()
        try:
            client = facade.make_client("nvidia", {"api_key": JOINED, "base_url": p.root + "/v1"})
            assert isinstance(client, OpenAI)
            assert client.max_retries == 0
            assert client.api_key == K1
            client.close()
        finally:
            p.close()


class TestARefusedKeyIsAnsweredByTheNext:
    @pytest.mark.parametrize("stream", [False, True])
    def test_openai_compatible(self, fresh, stream):
        p = Provider(refuse={K1})
        try:
            client = facade.make_client("nvidia", {"api_key": JOINED, "base_url": p.root + "/v1"})
            request = {"model": "moonshotai/kimi-k3", "messages": [{"role": "user", "content": "hi"}]}
            if stream:
                assert _text(client.chat.completions.create(stream=True, **request)) == "Hello world"
            else:
                answer = client.chat.completions.create(**request)
                assert answer.choices[0].message.content == "Hello world"
            assert [k for _, k in p.seen] == [K1, K2]
            client.close()
        finally:
            p.close()

    @pytest.mark.parametrize("stream", [False, True])
    def test_native_gemini(self, fresh, stream):
        p = Provider(refuse={K1})
        try:
            client = facade.make_client("gemini", {"api_key": JOINED, "base_url": p.root + "/v1beta"})
            request = {"model": "gemini-3.6-flash", "messages": [{"role": "user", "content": "hi"}]}
            if stream:
                assert _text(client.chat.completions.create(stream=True, **request)).strip() == "Hello world"
            else:
                answer = client.chat.completions.create(**request)
                assert answer.choices[0].message.content == "Hello world"
            assert [k for _, k in p.seen] == [K1, K2]
            # The daily quota window was read, so the refused key rests for
            # the day-shaped hold rather than Google's per-minute retryDelay.
            identity = carousel_mod.Carousel.identity("gemini", "gemini-3.6-flash")
            assert facade.TRANSPORT.engine.next_recovery_seconds(identity, [K1]) > 120
            client.close()
        finally:
            p.close()


class TestTheQuotaWindowSurvivesTheStream:
    def test_streaming_refusal_keeps_the_body(self):
        p = Provider(refuse={"k"})
        try:
            import httpx

            http = facade._with_error_body_hook(httpx.Client(timeout=10))
            client = GeminiNativeClient(api_key="k", base_url=p.root + "/v1beta", http_client=http)
            with pytest.raises(Exception) as caught:
                list(client.chat.completions.create(model="g", messages=[{"role": "user", "content": "hi"}],
                                                    stream=True))
            ev = evidence.harvest(caught.value)
            assert classify.stated_window(error_body=ev.body, error=caught.value) == "per_day"
            # And Hermes' own message is untouched.
            assert "exceeded your current quota" in str(caught.value)
        finally:
            p.close()


class TestEveryWireIsKames:
    """1.8.1.9 first stepped aside on the Messages and Responses wires; every wire is KAME's now."""

    @pytest.fixture(autouse=True)
    def _fresh_config_reads(self):
        facade._CONFIG_READS.clear()
        yield
        facade._CONFIG_READS.clear()

    @pytest.mark.parametrize("url", [
        "https://api.minimax.io/anthropic",
        "https://api.kimi.com/coding/v1",
        "https://api.anthropic.com",
    ])
    def test_a_messages_endpoint_gets_its_client_from_the_messages_seam(self, url):
        assert facade._wire("custom", url) == "anthropic_messages"
        settings.forget()
        assert facade.make_client("custom", {"api_key": JOINED, "base_url": url}) is None
        client = facade.make_messages_client("custom", {"api_key": JOINED, "base_url": url})
        try:
            assert isinstance(client, facade.KameMessagesClient)
            assert len(client._kame_keys.keys()) == len(JOINED.split(","))
        finally:
            client.close()

    @pytest.mark.parametrize("url,header", [
        ("https://api.minimax.io/anthropic", "Authorization"),
        ("https://proxy.example/anthropic", "X-Api-Key"),
    ])
    def test_every_key_is_sent_the_way_hermes_sends_the_first(self, url, header):
        settings.forget()
        client = facade.make_messages_client("custom", {"api_key": JOINED, "base_url": url})
        try:
            keys = JOINED.split(",")
            for key in keys:
                inner = client._kame_inner(key, 1)
                assert inner.auth_headers and list(inner.auth_headers) == [header]
                assert key in str(inner.auth_headers[header])
                # Derived from the first key's client: one connection pool, not one per key.
                assert inner._client is client._kame_primary._client
        finally:
            client.close()

    def test_a_key_of_another_shape_is_built_by_hermes(self, monkeypatch):
        import agent.anthropic_adapter as adapter

        built = []
        real = adapter.build_anthropic_client

        def spy(key, *a, **k):
            built.append(key)
            return real(key, *a, **k)

        monkeypatch.setattr(adapter, "build_anthropic_client", spy)
        settings.forget()
        client = facade.make_messages_client(
            "anthropic", {"api_key": "sk-ant-api03-aaaa,sk-ant-oat01-bbbb", "base_url": "https://api.anthropic.com"})
        try:
            oauth = client._kame_inner("sk-ant-oat01-bbbb", 1)
            assert built == ["sk-ant-api03-aaaa", "sk-ant-oat01-bbbb"]
            assert "Authorization" in oauth.auth_headers
            assert "X-Api-Key" in client._kame_inner("sk-ant-api03-aaaa", 1).auth_headers
        finally:
            client.close()

    def test_a_declared_responses_profile_gets_a_responses_client(self):
        settings.forget()
        client = facade.make_client("xai", {"api_key": JOINED, "base_url": "https://api.x.ai/v1"},
                                    "codex_responses")
        try:
            assert client is not None
            assert callable(client.responses.create)
            # The auxiliary path calls chat.completions on a profile's client: Hermes' own
            # Responses adapter answers there, over KAME's carousel.
            assert type(client.chat).__name__ == "_ChatShim"
        finally:
            client.close()

    def test_api_openai_com_gets_both_shapes(self):
        settings.forget()
        client = facade.make_client("openai", {"api_key": JOINED, "base_url": "https://api.openai.com/v1"})
        try:
            assert callable(client.chat.completions.create)
            assert callable(client.responses.create)
        finally:
            client.close()

    def test_by_configured_api_mode(self, monkeypatch):
        import hermes_cli.config as config

        monkeypatch.setattr(config, "load_config_readonly", lambda *a, **k: {
            "auxiliary": {"vision": {"provider": "nvidia", "api_mode": "codex_responses"}}})
        assert facade._wire("nvidia", "https://integrate.api.nvidia.com/v1") == "codex_responses"
        assert facade._wire("gemini", "https://generativelanguage.googleapis.com/v1beta") == "chat_completions"

    def test_a_plain_chat_endpoint_is_chat(self, monkeypatch):
        import hermes_cli.config as config

        monkeypatch.setattr(config, "load_config_readonly", lambda *a, **k: {
            "model": {"provider": "nvidia", "default": "moonshotai/kimi-k3"}})
        assert facade._wire("nvidia", "https://integrate.api.nvidia.com/v1") == "chat_completions"

    def test_switched_off_means_hermes_builds_its_own(self, monkeypatch):
        monkeypatch.setenv("KAME_ROTATION_DISABLED", "1")
        settings.forget()
        try:
            assert facade.make_client("nvidia", {"api_key": "k1", "base_url": "https://x.example/v1"}) is None
            assert facade.make_messages_client("minimax", {"api_key": "k1",
                                                           "base_url": "https://x.example/anthropic"}) is None
        finally:
            settings.forget()

    def test_no_key_means_hermes_builds_its_own(self):
        assert facade.make_client("nvidia", {"api_key": "", "base_url": "https://x.example/v1"}) is None
        assert facade.make_messages_client("minimax", {"api_key": "", "base_url": "https://x.example/anthropic"}) is None

    def test_a_callable_bearer_is_hermes_own(self):
        assert facade.make_messages_client("anthropic", {"api_key": lambda: "tok",
                                                         "base_url": "https://api.anthropic.com"}) is None


class TestTheProviderPackage:
    def test_every_api_key_wire_is_overridden(self):
        source = (ROOT / "hermes-kame-provider" / "__init__.py").read_text(encoding="utf-8")
        assert '_ROTATABLE_WIRES = frozenset({"chat_completions", "codex_responses", "anthropic_messages"})' in source
        assert "create_messages_client" in source
        # Never a rebind: the bundled profile objects are copied, not edited.
        assert "setattr(profile" not in source and "profile.create_client =" not in source


class TestHermesHeadersReachKamesClient:
    """The auxiliary path asks the profile before it computes endpoint headers.

    Found by ``tools/host_suite_1819.py``: Hermes' own NVIDIA billing-header
    tests diverged with KAME live. These pin the fix on KAME's client itself
    (the host tests assert on a mocked ``OpenAI`` constructor, which a subclass
    never calls).
    """

    def test_nvidia_cloud_gets_the_billing_origin(self, monkeypatch):
        monkeypatch.setattr(facade, "_wire", lambda provider, base_url, declared="": "chat_completions")
        client = facade.make_client("nvidia", {"api_key": "nvapi-" + "x" * 60,
                                               "base_url": "https://integrate.api.nvidia.com/v1"})
        try:
            assert client.default_headers.get("X-BILLING-INVOKE-ORIGIN") == "HermesAgent"
        finally:
            client.close()

    def test_a_local_nim_does_not(self, monkeypatch):
        monkeypatch.setattr(facade, "_wire", lambda provider, base_url, declared="": "chat_completions")
        client = facade.make_client("nvidia", {"api_key": "nvapi-" + "x" * 60,
                                               "base_url": "http://localhost:8000/v1"})
        try:
            assert "X-BILLING-INVOKE-ORIGIN" not in client.default_headers
        finally:
            client.close()

    def test_headers_the_main_agent_already_set_are_kept_as_they_are(self, monkeypatch):
        monkeypatch.setattr(facade, "_wire", lambda provider, base_url, declared="": "chat_completions")
        client = facade.make_client("nvidia", {"api_key": "nvapi-" + "x" * 60,
                                               "base_url": "https://integrate.api.nvidia.com/v1",
                                               "default_headers": {"X-Own": "1"}})
        try:
            assert client.default_headers.get("X-Own") == "1"
            assert "X-BILLING-INVOKE-ORIGIN" not in client.default_headers
        finally:
            client.close()


def test_config_reads_are_reused_briefly_and_then_read_again(monkeypatch):
    import hermes_cli.config as config

    facade._CONFIG_READS.clear()
    reads = []
    monkeypatch.setattr(config, "load_config_readonly", lambda *a, **k: reads.append(1) or {})
    clock = [1000.0]
    monkeypatch.setattr(facade.time, "monotonic", lambda: clock[0])
    for _ in range(5):
        facade._wire("nvidia", "https://integrate.api.nvidia.com/v1")
    assert len(reads) == 1
    clock[0] += facade.CONFIG_READ_TTL_S + 0.1
    facade._wire("nvidia", "https://integrate.api.nvidia.com/v1")
    assert len(reads) == 2
    facade._CONFIG_READS.clear()
