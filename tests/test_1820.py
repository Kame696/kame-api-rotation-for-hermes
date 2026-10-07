"""1.8.2.0 — the catalog review, every call on the Events tab, keys on the new Hermes.

Pins what 1.8.2.0 adds and nothing it only moved:

* ``max_total_wait_seconds`` is off by default (the wait ends when a key
  returns) and, once set, hands Hermes the original refusal;
* every call leaves a ``sent`` row and a first-try answer an ``answered`` row;
* refusal records are capped, keep the quota fields, and are created 0600;
* ``dashboard/plugin_api.py`` serves the snapshot and validates control requests;
* the ``.env`` sync binds the profile's secret scope on a Hermes that refuses an
  unscoped read;
* the status-line seam is looked up again when it appears after KAME loads;
* ``hermes-kame-bridge`` installs each of its two seams only where it is missing.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

from test_transport_1819 import (  # noqa: F401  (``clock`` is a fixture)
    IDENTITY, KEYS, PACKAGE, FakeCall, Script, _transport, clock, finish, per_minute, settings, shown,
    text, transport,
)

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
events_mod = importlib.import_module(f"{PACKAGE}.core.events")
recorder = importlib.import_module(f"{PACKAGE}.recorder")
envsync = importlib.import_module(f"{PACKAGE}.envsync")
state = importlib.import_module(f"{PACKAGE}.state")


def _everyone_refuses(times: int = 3):
    return Script({key: [per_minute(30) for _ in range(times)] for key in KEYS})


class TestTheTotalWaitBound:
    def test_off_by_default_the_call_waits_for_the_key_to_return(self, clock):
        plan = {key: [per_minute(30)] for key in KEYS}
        plan[KEYS[0]].append([text("back", "stop")])
        out = list(_transport(clock).stream(FakeCall(Script(plan))))
        assert shown(out) == "back"
        assert clock.slept >= 30

    def test_once_set_the_original_refusal_reaches_hermes(self, clock, monkeypatch):
        monkeypatch.setenv("KAME_MAX_TOTAL_WAIT", "10")
        settings.forget()
        with pytest.raises(Exception) as caught:
            list(_transport(clock).stream(FakeCall(_everyone_refuses())))
        assert "Quota exceeded" in str(caught.value)
        assert clock.slept <= 10 + 1
        kinds = [row["kind"] for row in events_mod.EVENTS.recent(20)]
        assert "gave_up" in kinds

    def test_the_setting_is_declared_off_with_its_environment_variable(self):
        assert settings.ALL_NUMBERS[settings.MAX_TOTAL_WAIT] == 0.0
        assert settings.env_name(settings.MAX_TOTAL_WAIT) == "KAME_MAX_TOTAL_WAIT"
        manifest = (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8")
        assert "max_total_wait_seconds:\n    type: int\n    default: 0" in manifest


class TestEveryCallIsOnTheEventsTab:
    def test_a_clean_call_leaves_sent_and_answered(self, clock):
        events_mod.EVENTS.clear()
        list(_transport(clock).stream(FakeCall(Script({KEYS[0]: [[text("hi", "stop")]]}))))
        rows = events_mod.EVENTS.recent(10)
        assert [row["kind"] for row in rows][:2] == ["answered", "sent"]
        assert all(row["key"] and row["key"] not in KEYS for row in rows)
        assert rows[0]["seconds"] is not None

    def test_the_panel_knows_every_kind_the_python_half_writes(self):
        source = (PLUGIN_DIR / "desktop" / "plugin.js").read_text(encoding="utf-8")
        for kind in ("sent", "answered", "gave_up"):
            assert f"\n  {kind}: [" in source and f"\n  {kind}: '" in source
        good = {k for k in events_mod.GOOD_KINDS}
        assert "const GOOD_KINDS = new Set([" + ", ".join(
            f"'{k}'" for k in ["switch", "recovery", "stitch", "wait", "setting", "sent", "answered"]) + "])" in source
        assert good == {"switch", "recovery", "stitch", "wait", "setting", "sent", "answered"}


class TestRefusalRecords:
    def test_capped_with_the_quota_fields_kept_and_owner_only(self, monkeypatch):
        monkeypatch.delenv("KAME_RECORDER_DISABLED", raising=False)
        settings.forget()
        monkeypatch.setattr(recorder, "_silenced", False)
        folder = state.state_dir()
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / recorder.FILENAME
        if path.exists():
            path.unlink()
        body = {"error": {"message": "x" * 5000, "details": [
            {"violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                             "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests"}]},
            {"retryDelay": "41s"}]}}
        recorder.record(provider="gemini", model="m", status_code=429, error_message="Quota exceeded", error_body=body)
        row = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
        assert len(row["body"]) <= recorder.TEXT_LIMIT
        assert row["fields"]["quotaId"] == ["GenerateRequestsPerDayPerProjectPerModel-FreeTier"]
        assert row["fields"]["retryDelay"] == ["41s"]
        if os.name != "nt":
            assert (path.stat().st_mode & 0o777) == 0o600
        path.unlink()


def _load_plugin_api():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    spec = importlib.util.spec_from_file_location("kame_plugin_api_under_test", PLUGIN_DIR / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestThePanelBackendRoute:
    @pytest.fixture()
    def client(self, tmp_path, monkeypatch):
        api = _load_plugin_api()
        monkeypatch.setattr(api, "get_hermes_home", lambda: tmp_path)
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()
        app.include_router(api.router)
        return TestClient(app), tmp_path / "plugin-data" / api.PLUGIN_ID

    def test_no_snapshot_is_a_404_not_an_error(self, client):
        http, _folder = client
        assert http.get("/state").status_code == 404

    def test_the_snapshot_comes_back_byte_for_byte(self, client):
        http, folder = client
        folder.mkdir(parents=True)
        (folder / "state.json").write_text('{"schema": 6, "processes": {}}', encoding="utf-8")
        response = http.get("/state")
        assert response.status_code == 200
        assert response.json() == {"schema": 6, "processes": {}}

    def test_a_valid_request_is_written_for_the_python_half(self, client):
        http, folder = client
        folder.mkdir(parents=True)
        body = {"action": "set", "id": "abc", "key": "max_hold_seconds", "schema": 1, "value": 1800}
        assert http.post("/control", json=body).status_code == 200
        assert json.loads((folder / "control.json").read_text(encoding="utf-8")) == body

    @pytest.mark.parametrize("body", [
        {"action": "set", "id": "a", "schema": 2},
        {"action": "", "id": "a", "schema": 1},
        {"action": "set", "id": "a", "schema": 1, "extra": True},
        ["not", "an", "object"],
    ])
    def test_anything_else_is_refused(self, client, body):
        http, folder = client
        folder.mkdir(parents=True)
        assert http.post("/control", json=body).status_code == 400
        assert not (folder / "control.json").exists()

    def test_before_kame_started_in_this_profile_nothing_is_created(self, client):
        http, folder = client
        response = http.post("/control", json={"action": "refresh", "id": "a", "schema": 1})
        assert response.status_code == 409
        assert not folder.exists()


class TestTheEnvSyncOnAScopedHermes:
    def test_the_profile_scope_is_bound_while_syncing(self, monkeypatch):
        secret_scope = pytest.importorskip("agent.secret_scope")
        home = Path(os.environ["HERMES_HOME"])
        env = home / ".env"
        before = env.read_text(encoding="utf-8") if env.exists() else None
        env.write_text("KAME_SCOPE_PROBE=from-dotenv\n", encoding="utf-8")
        secret_scope.invalidate_env_file_cache()
        monkeypatch.setattr(secret_scope, "is_multiplex_active", lambda: True)
        try:
            with pytest.raises(secret_scope.UnscopedSecretError):
                secret_scope.get_secret("KAME_SCOPE_PROBE")
            with envsync._profile_scope():
                assert secret_scope.get_secret("KAME_SCOPE_PROBE") == "from-dotenv"
            assert secret_scope.current_secret_scope() is None
        finally:
            if before is None:
                env.unlink()
            else:
                env.write_text(before, encoding="utf-8")
            secret_scope.invalidate_env_file_cache()

    def test_an_existing_scope_is_left_alone(self):
        secret_scope = pytest.importorskip("agent.secret_scope")
        token = secret_scope.set_secret_scope({"X": "1"})
        try:
            with envsync._profile_scope():
                assert secret_scope.current_secret_scope() == {"X": "1"}
        finally:
            secret_scope.reset_secret_scope(token)


class TestTheStatusSeamIsFoundLate:
    def test_a_seam_installed_after_the_first_lookup_is_used(self, monkeypatch):
        status_output = pytest.importorskip("agent.status_output")
        monkeypatch.setattr(transport, "_NOTIFY", False)
        monkeypatch.setattr(status_output, "notify_turn_status", None, raising=False)
        assert transport._turn_status("activity", "x") is False
        seen = []
        monkeypatch.setattr(status_output, "notify_turn_status", lambda message, kind: seen.append((kind, message)) or True)
        assert transport._turn_status("activity", "next key in 10s") is True
        assert seen == [("activity", "next key in 10s")]


def _load_bridge():
    path = ROOT / "hermes-kame-bridge" / "__init__.py"
    spec = importlib.util.spec_from_file_location("kame_bridge_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestTheSpinnerBridge:
    @pytest.fixture()
    def hermes(self, monkeypatch):
        status_output = pytest.importorskip("agent.status_output")
        turn_api_call = pytest.importorskip("agent.turn_api_call")
        conversation_loop = pytest.importorskip("agent.conversation_loop")
        # Every attribute the bridge may set is registered with monkeypatch
        # first, so teardown restores this process's Hermes exactly.
        for name in ("notify_turn_status", "_TURN_STATUS_SINK", "TURN_STATUS_MAX_CHARS", "TURN_STATUS_KINDS"):
            monkeypatch.setattr(status_output, name, None, raising=False)
        calls = []

        def perform_api_call(agent, *, api_kwargs=None):
            calls.append(status_output.notify_turn_status("next key in 2m", kind="activity"))
            return "verdict"

        monkeypatch.setattr(turn_api_call, "perform_api_call", perform_api_call)
        monkeypatch.setattr(conversation_loop, "perform_api_call", perform_api_call)
        monkeypatch.setattr(transport, "_NOTIFY", False)
        return status_output, turn_api_call, conversation_loop, calls

    def test_installs_the_seam_where_it_is_missing(self, hermes):
        status_output, turn_api_call, conversation_loop, calls = hermes
        outcome = _load_bridge().install_status_seam()
        assert outcome.startswith("status line: installed")
        assert turn_api_call.perform_api_call is conversation_loop.perform_api_call
        lines = []

        class Agent:
            def _emit_wait_notice(self, text):
                lines.append(text)

        assert turn_api_call.perform_api_call(Agent(), api_kwargs={}) == "verdict"
        assert calls == [True] and lines == ["next key in 2m"]
        # Outside a provider call there is no turn to speak to.
        assert status_output.notify_turn_status("x", kind="activity") is False

    def test_does_nothing_on_a_hermes_that_has_the_seam(self, hermes, monkeypatch):
        status_output, turn_api_call, _loop, _calls = hermes
        native = lambda message, kind="lifecycle": True  # noqa: E731
        monkeypatch.setattr(status_output, "notify_turn_status", native)
        before = turn_api_call.perform_api_call
        assert _load_bridge().install_status_seam().startswith("status line: not needed")
        assert status_output.notify_turn_status is native
        assert turn_api_call.perform_api_call is before

    def test_the_wrapper_keeps_the_signature_hermes_inspects(self, hermes):
        import inspect

        _status_output, turn_api_call, _loop, _calls = hermes
        _load_bridge().install_status_seam()
        assert list(inspect.signature(turn_api_call.perform_api_call).parameters) == ["agent", "api_kwargs"]


class TestTheMessagesSeamBridge:
    @pytest.fixture()
    def adapter(self, monkeypatch):
        anthropic_adapter = pytest.importorskip("agent.anthropic_adapter")
        built = []

        def build_anthropic_client(api_key, base_url=None, timeout=None, *, drop_context_1m_beta=False):
            built.append((api_key, base_url))
            return "hermes-client"

        monkeypatch.setattr(anthropic_adapter, "build_anthropic_client", build_anthropic_client)
        return anthropic_adapter, built

    def test_the_profile_is_asked_first_with_the_callers_provider(self, adapter, monkeypatch):
        anthropic_adapter, built = adapter
        asked = []

        class Profile:
            def create_messages_client(self, **kwargs):
                asked.append(kwargs)
                return "kame-client"

        providers = pytest.importorskip("providers")
        monkeypatch.setattr(providers, "get_provider_profile", lambda name: Profile() if name == "minimax" else None)
        assert _load_bridge().install_messages_seam().startswith("messages client: installed")

        class Agent:
            provider = "minimax"

        def _init_anthropic_client(agent):
            return anthropic_adapter.build_anthropic_client("sk-1", "https://x/anthropic", timeout=5)

        assert _init_anthropic_client(Agent()) == "kame-client"
        assert asked == [{"api_key": "sk-1", "base_url": "https://x/anthropic", "timeout": 5, "drop_context_1m_beta": False}]
        assert built == []

    def test_without_a_profile_client_hermes_builds_its_own(self, adapter, monkeypatch):
        anthropic_adapter, built = adapter
        providers = pytest.importorskip("providers")
        monkeypatch.setattr(providers, "get_provider_profile", lambda name: None)
        _load_bridge().install_messages_seam()
        provider = "anthropic"  # noqa: F841 - read from this frame by the bridge
        assert anthropic_adapter.build_anthropic_client("sk-2", "https://api.anthropic.com") == "hermes-client"
        assert built == [("sk-2", "https://api.anthropic.com")]

    def test_left_alone_on_a_hermes_that_has_the_seam(self, adapter, monkeypatch):
        anthropic_adapter, _built = adapter

        def native(api_key, base_url=None, timeout=None, *, drop_context_1m_beta=False, provider=None):
            return "native"

        monkeypatch.setattr(anthropic_adapter, "build_anthropic_client", native)
        assert _load_bridge().install_messages_seam().startswith("messages client: not needed")
        assert anthropic_adapter.build_anthropic_client is native


class TestTheBridgeNeverNestsRotatingClients:
    def test_a_profile_building_its_client_reaches_hermes_builder_directly(self, monkeypatch):
        anthropic_adapter = pytest.importorskip("agent.anthropic_adapter")
        providers = pytest.importorskip("providers")
        built = []

        def build_anthropic_client(api_key, base_url=None, timeout=None, *, drop_context_1m_beta=False):
            built.append(api_key)
            return f"hermes-client-{api_key}"

        monkeypatch.setattr(anthropic_adapter, "build_anthropic_client", build_anthropic_client)
        asked = []

        class Profile:
            def create_messages_client(self, **kwargs):
                asked.append(kwargs["api_key"])
                # A rotating client builds one plain client per key through the
                # same (now wrapped) function.
                inner = [anthropic_adapter.build_anthropic_client(k, kwargs["base_url"]) for k in ("k1", "k2")]
                return ("kame-client", inner)

        monkeypatch.setattr(providers, "get_provider_profile", lambda name: Profile())
        _load_bridge().install_messages_seam()
        provider = "minimax"  # noqa: F841 - read from this frame by the bridge
        result = anthropic_adapter.build_anthropic_client("k1,k2", "https://x/anthropic")
        assert result == ("kame-client", ["hermes-client-k1", "hermes-client-k2"])
        assert asked == ["k1,k2"] and built == ["k1", "k2"]

    def test_kame_unwraps_the_bridge_for_its_per_key_clients(self):
        source = (PLUGIN_DIR / "facade.py").read_text(encoding="utf-8")
        assert 'getattr(build_anthropic_client, "_kame_bridge", False)' in source


class TestTheCronBound1821:
    """1.8.2.1: unset, a cron run is bounded at 300 s; a chat is not; a user value wins."""

    @pytest.fixture()
    def cron(self, monkeypatch):
        monkeypatch.delenv("KAME_MAX_TOTAL_WAIT", raising=False)
        settings.forget()
        session = pytest.importorskip("gateway.session_context")
        var = session._VAR_MAP["HERMES_CRON_SESSION"]
        token = var.set("1")
        yield
        var.reset(token)
        settings.forget()

    def test_unset_in_a_chat_is_unbounded(self, monkeypatch):
        monkeypatch.delenv("KAME_MAX_TOTAL_WAIT", raising=False)
        monkeypatch.delenv("HERMES_CRON_SESSION", raising=False)
        settings.forget()
        assert settings.total_wait_bound() == 0.0

    def test_unset_in_cron_is_300_seconds(self, cron):
        assert settings.total_wait_bound() == settings.CRON_TOTAL_WAIT_DEFAULT_S == 300.0

    def test_an_explicit_zero_keeps_cron_unbounded(self, cron, monkeypatch):
        monkeypatch.setenv("KAME_MAX_TOTAL_WAIT", "0")
        assert settings.total_wait_bound() == 0.0

    def test_an_explicit_number_wins_in_cron(self, cron, monkeypatch):
        monkeypatch.setenv("KAME_MAX_TOTAL_WAIT", "900")
        assert settings.total_wait_bound() == 900.0

    def test_a_cron_call_hands_over_the_refusal_after_the_bound(self, cron, clock):
        with pytest.raises(Exception) as caught:
            list(_transport(clock).stream(FakeCall(_everyone_refuses(times=30))))
        assert "Quota exceeded" in str(caught.value)
        assert 299 <= clock.slept <= 301
