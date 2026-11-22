"""1.9.0.5 (B1) - a provider that supplies its own client (Gemini native) still goes through KAME.

``facade.make_profile_client`` wraps the client the bundled profile built: one inner client per key, made by
the bundled profile's own ``create_client(api_key=key)``, rotated by the carousel. No network: the inner
clients are fakes. One opt-in live test (``KAME_LIVE_GEMINI=1``) is never run by default.
"""
from __future__ import annotations

import os
import sys
import types

import pytest

from tests import h1905
from tests.h1905 import FakeProviders, FProfile, needs_host

facade = h1905.sub("facade")
carousel_mod = h1905.sub("core.carousel")
settings = h1905.sub("settings")

K1 = "AIzaSyFAKEFAKEFAKEFAKEFAKEFAKEFAKE00001"
K2 = "AIzaSyFAKEFAKEFAKEFAKEFAKEFAKEFAKE00002"
JOINED = f"{K1},{K2}"
REQUEST = {"model": "gemini-flash-lite-latest", "messages": [{"role": "user", "content": "hi"}]}


class GeminiAPIError(Exception):
    """The shape of Hermes' ``GeminiAPIError`` (status_code, retry_after, details)."""

    def __init__(self, message, *, status_code=None, retry_after=None, details=None):
        super().__init__(message)
        self.code, self.status_code, self.response = "gemini_api_error", status_code, None
        self.retry_after, self.details = retry_after, details or {}


class Script:
    """What each key does when asked: ``{key: exception-or-None}``; records the order keys were used."""

    def __init__(self, plan=None):
        self.plan = plan or {}
        self.used = []


class FakeInner:
    """Stands in for Hermes' ``GeminiClient``: ``chat.completions.create`` and ``close``."""

    def __init__(self, script, api_key, base_url="https://generativelanguage.googleapis.com/v1beta", **_):
        self.script, self.api_key, self.base_url = script, api_key, base_url
        self.closed = False
        self.marker = "from-own"
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.script.used.append(self.api_key)
        failure = self.script.plan.get(self.api_key)
        if failure is not None:
            raise failure
        return types.SimpleNamespace(
            id="x", model=kw.get("model"), usage=None,
            choices=[types.SimpleNamespace(index=0, finish_reason="stop", message=types.SimpleNamespace(
                role="assistant", content=f"answered by {self.api_key[-5:]}", tool_calls=None))])

    def close(self):
        self.closed = True


def make_base(script, builds):
    class FakeGeminiProfile(FProfile):
        def create_client(self, **kw):
            builds.append(kw.get("api_key"))
            return FakeInner(script, kw["api_key"], kw.get("base_url", self.base_url))

    return FakeGeminiProfile


@pytest.fixture()
def fresh(monkeypatch):
    monkeypatch.setattr(facade.TRANSPORT, "engine", carousel_mod.Carousel())
    monkeypatch.setattr(facade, "_wire", lambda provider, base_url, declared="": "chat_completions")
    monkeypatch.delenv("KAME_ROTATION_DISABLED", raising=False)
    settings.forget()
    yield
    settings.forget()


def build(script, plan_kwargs=None, auth_type="api_key", api_key=JOINED, own=None):
    builds = []
    base = make_base(script, builds)
    profile = base(name="gemini", auth_type=auth_type)
    kw = {"api_key": api_key, "base_url": profile.base_url}
    own = own if own is not None else base.create_client(profile, **kw)
    builds.clear()
    client = facade.make_profile_client("gemini", profile, base, kw, own)
    return client, own, builds


class TestTheClient:
    def test_is_the_profiles_own_class_with_its_attributes(self, fresh):
        client, own, _ = build(Script())
        assert isinstance(client, FakeInner)
        assert type(client).__name__ == "KameProfileClient"
        assert client.marker == "from-own" and client.api_key == own.api_key
        assert callable(client.chat.completions.create)
        assert client.chat.completions.create.__self__ is client

    def test_the_class_is_built_once_per_client_type(self, fresh):
        a, _, _ = build(Script())
        b, _, _ = build(Script())
        assert type(a) is type(b)

    def test_untouched_when_the_profile_is_not_api_key(self, fresh):
        client, own, _ = build(Script(), auth_type="oauth_external")
        assert client is own

    def test_untouched_when_the_client_has_no_chat_completions(self, fresh):
        script = Script()
        bare = types.SimpleNamespace(api_key="x")
        client, own, _ = build(script, own=bare)
        assert client is bare

    def test_untouched_when_kame_is_switched_off(self, fresh, monkeypatch):
        monkeypatch.setenv("KAME_ROTATION_DISABLED", "1")
        settings.forget()
        client, own, _ = build(Script())
        assert client is own

    def test_untouched_when_there_is_no_key(self, fresh):
        client, own, _ = build(Script(), api_key="")
        assert client is own


class TestRotation:
    def test_one_inner_client_per_key_built_by_the_profile(self, fresh):
        script = Script({K1: GeminiAPIError("quota", status_code=429)})
        client, _, builds = build(script)
        answer = client.chat.completions.create(**REQUEST)
        assert answer.choices[0].message.content.endswith("00002")
        # the carousel picks which key goes first; either way K2 answers and K1 is never retried
        assert script.used[-1] == K2 and script.used.count(K1) <= 1
        assert builds == list(dict.fromkeys(script.used))   # the bundled profile built each one, once
        client.close()

    def test_a_429_with_details_rotates(self, fresh):
        details = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "quota"}}
        script = Script({K1: GeminiAPIError("You exceeded your current quota", status_code=429,
                                            retry_after=30.0, details=details)})
        client, _, _ = build(script)
        assert client.chat.completions.create(**REQUEST).choices[0].message.content.endswith("00002")
        assert script.used[-1] == K2 and script.used.count(K1) <= 1
        client.close()

    def test_an_invalid_key_is_set_aside_and_the_next_answers(self, fresh):
        script = Script({K1: GeminiAPIError("API key not valid. Please pass a valid API key.", status_code=400)})
        client, _, _ = build(script)
        assert client.chat.completions.create(**REQUEST).choices[0].message.content.endswith("00002")
        # the dead key is tried at most once, however many requests follow
        for _ in range(3):
            assert client.chat.completions.create(**REQUEST).choices[0].message.content.endswith("00002")
        assert script.used.count(K1) <= 1
        client.close()

    def test_a_none_from_the_profile_falls_back_to_the_own_client(self, fresh):
        script = Script()
        builds = []
        base = make_base(script, builds)
        profile = base(name="gemini")
        own = FakeInner(script, JOINED)
        kw = {"api_key": JOINED, "base_url": profile.base_url}
        client = facade.make_profile_client("gemini", profile, base, kw, own)
        base.create_client = lambda self, **k: None
        assert client._kame_build(K2) is own
        client.close()

    def test_close_closes_every_inner_client_and_the_own_one(self, fresh):
        script = Script()
        client, own, _ = build(script)
        client.chat.completions.create(**REQUEST)
        inners = list(client._kame_inners.values())
        assert inners
        client.close()
        assert all(i.closed for i in inners) and own.closed
        assert client.is_closed is True   # the old Gemini wrapper set it; Hermes reads it


class TestThroughTheProfile:
    """provider_profiles' class -> the real facade -> KameProfileClient."""

    def test_end_to_end_with_a_fake_hermes(self, fresh, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        pp = h1905.sub("provider_profiles")
        pp.reset_status()
        script = Script({K1: GeminiAPIError("quota", status_code=429)})
        builds = []
        base = make_base(script, builds)
        prov = FakeProviders(base(name="gemini"))
        monkeypatch.setitem(sys.modules, "providers", prov)
        pkg = h1905.load()
        facade.publish(pkg)
        try:
            assert pp.install() == ["gemini"]
            profile = prov.get_provider_profile("gemini")
            client = profile.create_client(api_key=JOINED, base_url=profile.base_url)
            assert type(client).__name__ == "KameProfileClient"
            assert client.chat.completions.create(**REQUEST).choices[0].message.content.endswith("00002")
            client.close()
        finally:
            facade.retract(pkg)
            pp.reset_status()


@needs_host
class TestOnARealHermes:
    def test_the_real_gemini_client_is_wrapped(self, tmp_path):
        home = h1905.make_home(tmp_path, "A", plugin=True)
        out = h1905.run_host("h1905_host_one_package.py", home)
        assert out["a_client_class"] == "KameProfileClient"
        assert "GeminiClient" in out["a_client_mro"] or "GeminiNativeClient" in out["a_client_mro"]
        assert out["a_has_chat_completions"] is True


@pytest.mark.skipif(os.environ.get("KAME_LIVE_GEMINI") != "1", reason="opt-in live test (KAME_LIVE_GEMINI=1)")
class TestLive:
    def test_a_bad_key_then_the_real_one(self):
        """Mirrors research/1.9.0.5/probe_gemini_per_key.py. The key comes from the environment, never printed."""
        real = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
        assert real, "set GOOGLE_API_KEY"
        import providers

        profile = providers.get_provider_profile("gemini")
        own = profile.create_client(api_key=real, base_url=profile.base_url)
        client = facade.make_profile_client(
            "gemini", profile, type(profile),
            {"api_key": "AIzaSyBAD_probe_key_000000000000000000," + real, "base_url": profile.base_url}, own)
        stream = client.chat.completions.create(model="gemini-flash-lite-latest", stream=True, max_tokens=16,
                                                messages=[{"role": "user", "content": "Say OK"}])
        text = "".join((c.choices[0].delta.content or "") for c in stream if c.choices)
        assert text
        client.close()
