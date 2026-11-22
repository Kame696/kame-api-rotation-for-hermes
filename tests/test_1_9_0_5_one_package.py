"""1.9.0.5 (A1-A4) - one package: the provider profiles live in the rotation plugin.

Unit tests use a stand-in for Hermes' ``providers`` module; the host tests run the real plugin manager in
Hermes' own venv against a source checkout (v0.23.3) with a temp HERMES_HOME, and skip when it is absent.
"""
from __future__ import annotations

import sys
import types

import pytest

from tests import h1905
from tests.h1905 import FakeProviders, FProfile, needs_host


class OwnClientProfile(FProfile):
    """A bundled profile that supplies its own client (like gemini)."""

    def create_client(self, **kw):
        return types.SimpleNamespace(own=True, kw=kw)


class OAuthProfile(FProfile):
    pass


@pytest.fixture()
def pp(monkeypatch):
    mod = h1905.sub("provider_profiles")
    mod.reset_status()
    yield mod
    mod.reset_status()


@pytest.fixture()
def fake(monkeypatch):
    prov = FakeProviders(
        FProfile("groq"), OwnClientProfile("gemini"), OAuthProfile("codex", auth_type="oauth_external"),
        FProfile("weird", api_mode="bedrock_converse"))
    monkeypatch.setitem(sys.modules, "providers", prov)
    return prov


class TestInstall:
    def test_registers_a_kame_copy_of_every_rotatable_bundled_profile(self, pp, fake):
        names = pp.install()
        assert sorted(names) == ["gemini", "groq"]
        assert type(fake.get_provider_profile("gemini")).__name__ == "KameOwnClientProfile"
        assert type(fake.get_provider_profile("groq")).__name__ == "KameFProfile"
        assert fake.provider_source("gemini") == "runtime"
        # oauth and unknown wires keep Hermes' own
        assert type(fake.get_provider_profile("codex")) is OAuthProfile
        assert fake.provider_source("codex") == "bundled"
        assert fake.provider_source("weird") == "bundled"

    def test_discovery_is_forced_before_anything_is_registered(self, pp, fake):
        pp.install()
        assert fake.calls[0] == "list_providers"
        assert any(isinstance(c, tuple) for c in fake.calls)

    def test_fields_are_copied(self, pp, fake):
        before = fake.get_provider_profile("groq")
        pp.install()
        after = fake.get_provider_profile("groq")
        assert after is not before
        assert (after.name, after.auth_type, after.api_mode, after.base_url) == (
            before.name, before.auth_type, before.api_mode, before.base_url)

    def test_second_install_registers_nothing_new(self, pp, fake):
        assert pp.install()
        count = len([c for c in fake.calls if isinstance(c, tuple)])
        assert pp.install() == []
        assert len([c for c in fake.calls if isinstance(c, tuple)]) == count

    def test_a_kame_class_from_another_module_is_left_alone(self, pp, fake):
        foreign = type("KameOwnClientProfile", (OwnClientProfile,), {"__module__": "hermes_kame_provider"})
        existing = foreign(name="gemini")
        fake._profiles["gemini"] = existing
        fake._source["gemini"] = "user"
        names = pp.install()
        assert names == ["groq"]
        assert fake.get_provider_profile("gemini") is existing

    def test_never_raises(self, pp, fake):
        fake.fail_register = True
        assert pp.install() == []

    def test_never_raises_without_hermes(self, pp, monkeypatch):
        monkeypatch.setitem(sys.modules, "providers", None)  # import fails
        assert pp.install() == []


class TestTheKameProfileHandsBackWhatItShould:
    """The class installed for each profile: ``create_client`` and the facade lookup."""

    @pytest.fixture()
    def env(self, pp, fake, monkeypatch, tmp_path):
        facade = h1905.sub("facade")
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        calls = {"client": [], "profile": []}

        class FakeFacade:
            def make_client(self, name, kw, api_mode=""):
                calls["client"].append((name, kw, api_mode))
                return "kame-client"

            def make_profile_client(self, provider, profile, base_cls, kw, own):
                calls["profile"].append((provider, base_cls.__name__, own))
                return "kame-profile-client"

        registry = types.ModuleType(pp.REGISTRY_KEY)
        registry.homes = {facade._home(): types.SimpleNamespace(facade=FakeFacade())}
        monkeypatch.setitem(sys.modules, pp.REGISTRY_KEY, registry)
        pp.install()
        return types.SimpleNamespace(calls=calls, registry=registry, facade=facade)

    def test_a_profile_without_its_own_client_gets_the_rotating_one(self, env, fake):
        out = fake.get_provider_profile("groq").create_client(api_key="k", base_url="u")
        assert out == "kame-client"
        assert env.calls["client"][0][0] == "groq"

    def test_a_profile_with_its_own_client_is_offered_to_the_facade(self, env, fake):
        out = fake.get_provider_profile("gemini").create_client(api_key="k", base_url="u")
        assert out == "kame-profile-client"
        provider, base_name, own = env.calls["profile"][0]
        assert (provider, base_name) == ("gemini", "OwnClientProfile")
        assert own.own is True

    def test_without_kame_in_this_home_the_bundled_answer_is_returned_as_is(self, env, fake):
        env.registry.homes.clear()
        out = fake.get_provider_profile("gemini").create_client(api_key="k", base_url="u")
        assert out.own is True
        assert fake.get_provider_profile("groq").create_client(api_key="k") is None

    def test_a_facade_that_raises_leaves_hermes_with_its_own(self, env, fake):
        def boom(*a, **k):
            raise RuntimeError("x")

        env.registry.homes[env.facade._home()].facade.make_profile_client = boom
        out = fake.get_provider_profile("gemini").create_client(api_key="k")
        assert out.own is True


class TestNotices:
    def test_old_provider_package_is_noticed(self, pp, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        assert pp.old_provider_notice() == ""
        (tmp_path / "plugins" / "hermes-kame-provider").mkdir(parents=True)
        text = pp.old_provider_notice()
        assert "hermes-kame-provider is no longer needed" in text
        assert "hermes plugins remove hermes-kame-provider" in text
        assert text in pp.notices()

    def test_notice_is_in_the_kame_readout(self, pp, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        (tmp_path / "plugins" / "hermes-kame-provider").mkdir(parents=True)
        menu = h1905.sub("menu")
        shown = " ".join(menu.MenuCommand(None).show().split())
        assert "hermes plugins remove hermes-kame-provider" in shown

    def test_old_provider_is_a_problem_in_kame_not_a_tip(self, pp, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        (tmp_path / "plugins" / "hermes-kame-provider").mkdir(parents=True)
        assert pp.old_provider_notice() in pp.problems()
        raw = h1905.sub("menu").MenuCommand(None).show()
        shown = " ".join(raw.split())
        assert "Problems:" in raw and "  ! " in raw and "Notices:" not in raw
        assert "Gemini is NOT rotated" in shown and "hermes plugins remove hermes-kame-provider" in shown

    def test_readout_has_no_notices_block_when_there_is_nothing_to_say(self, pp, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.delenv("HERMES_PLUGIN_HOST_PROCESS", raising=False)
        shown = " ".join(h1905.sub("menu").MenuCommand(None).show().split())
        assert "no longer needed" not in shown and "plugins.isolation" not in shown

    def test_host_isolation_skips_install_and_says_why(self, pp, fake, monkeypatch):
        monkeypatch.setenv("HERMES_PLUGIN_HOST_PROCESS", "1")
        assert pp.register_profiles() == []
        assert not [c for c in fake.calls if isinstance(c, tuple)]
        assert pp.status()["host_isolation"] is True
        text = " ".join(pp.notices())
        assert "plugins.isolation: in_process" in text and "host" in text
        assert "plugins.isolation: in_process" in " ".join(h1905.sub("menu").MenuCommand(None).show().split())

    def test_in_process_installs(self, pp, fake, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.delenv("HERMES_PLUGIN_HOST_PROCESS", raising=False)
        assert sorted(pp.register_profiles()) == ["gemini", "groq"]
        assert pp.status()["host_isolation"] is False
        assert sorted(pp.status()["installed"]) == ["gemini", "groq"]
        assert pp.notices() == []


class TestRegisterCallsIt:
    def test_register_calls_register_profiles_after_publish(self, monkeypatch):
        pkg = h1905.load()
        pp = h1905.sub("provider_profiles")
        facade = h1905.sub("facade")
        order = []
        monkeypatch.setattr(facade, "publish", lambda module: order.append("publish"))
        monkeypatch.setattr(pp, "register_profiles", lambda: order.append("profiles") or [])
        ctx = types.SimpleNamespace(register_hook=lambda *a, **k: None, register_command=lambda *a, **k: None,
                                    on_unload=lambda f: None)
        monkeypatch.setattr(pkg, "_REGISTERED_CTX", None)
        monkeypatch.setattr(pkg, "_start_state_heartbeat", lambda: None)
        try:
            pkg.register(ctx)
        finally:
            pkg._cleanup_runtime()
        assert order[:2] == ["publish", "profiles"]


# --- the real Hermes ---------------------------------------------------------------------------------


@needs_host
class TestOnARealHermes:
    @pytest.fixture(scope="class")
    def result(self, tmp_path_factory):
        root = tmp_path_factory.mktemp("kame-1905-host")
        home_a = h1905.make_home(root, "A", plugin=True)
        home_b = h1905.make_home(root, "B", plugin=False)
        return h1905.run_host("h1905_host_one_package.py", home_a, str(home_b))

    def test_gemini_profile_is_kames_and_runtime(self, result):
        assert result["profile_class"] == "KameGeminiProfile"
        assert result["provider_source"] == "runtime"

    def test_create_client_gives_a_kame_client(self, result):
        assert result["a_client_class"] == "KameProfileClient"
        assert "GeminiClient" in result["a_client_mro"] or "GeminiNativeClient" in result["a_client_mro"]
        assert result["a_has_chat_completions"] is True

    def test_second_install_registers_nothing(self, result):
        assert result["second_install"] == []

    def test_a_home_without_the_plugin_gets_the_bundled_client(self, result):
        assert result["b_profile_class"] == "KameGeminiProfile"  # the registry is process-wide
        assert "KameProfileClient" not in result["b_client_mro"]
        assert result["b_client_class"] != "KameProfileClient"
