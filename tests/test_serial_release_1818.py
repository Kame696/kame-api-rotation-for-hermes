"""Owner's serial-only boundary, including stale experiment activation controls."""
import importlib.util
import threading
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from .test_dispatch import Agent, Answer, Boom, Carousel, DispatchBinding, dispatch_binding, settings

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "hermes-kame-api-rotation"


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    settings.forget()
    for name in list(settings._ENV_FOR.values()) + list(settings._NUMBER_ENV_FOR.values()):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KAME_SHARE_POOL_HEALTH", "0")
    monkeypatch.setattr(dispatch_binding, "_publish", lambda *a, **k: None)
    yield
    settings.forget()


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("old_flag", ["1", "true", "yes"])
def test_stale_environment_and_config_cannot_launch_a_second_request(monkeypatch, stream, old_flag):
    monkeypatch.setenv("KAME_PARALLEL_REQUESTS", old_flag)
    monkeypatch.setenv("KAME_PARALLEL_DELAY", "0")
    read = []
    def config(key, default=None):
        read.append(key)
        return {"parallel_requests": True, "parallel_delay_seconds": 0}.get(key, default)
    settings.load(NS(get_config=config))
    payload = {"model": "same-model", "stream": stream,
               "messages": [{"role": "system", "content": "retain all instructions"},
                            {"role": "user", "content": "retain all history"}],
               "tools": [{"type": "function", "function": {"name": "fixture", "parameters": {}}}],
               "reasoning_effort": "medium", "max_tokens": 8192}
    agent = Agent()
    answer = Answer("one complete answer")
    observed = []
    def host(actual, request, **kwargs):
        assert actual is agent and request is payload
        observed.append((actual.api_key, threading.get_ident()))
        # Fail if any concurrent or delayed transport enters this request.
        threading.Event().wait(0.025)
        return answer
    binding = DispatchBinding(engine=Carousel())
    assert binding.run(host, agent, payload, (), {}) is answer
    assert len(observed) == 1
    assert observed[0][1] == threading.get_ident()
    assert not {"parallel_requests", "parallel_delay_seconds"} & set(read)
    assert not hasattr(binding, "parallel_calls")


@pytest.mark.parametrize("stream", [False, True])
def test_serial_failure_rotation_keeps_the_agent_and_payload(monkeypatch, stream):
    monkeypatch.setenv("KAME_PARALLEL_REQUESTS", "1")
    monkeypatch.setenv("KAME_PARALLEL_DELAY", "0")
    agent = Agent()
    request = {"stream": stream, "model": agent.model,
               "messages": [{"role": "assistant", "content": "prior facts"}],
               "reasoning_effort": "medium"}
    calls = []
    active = [False]
    rows = []
    monkeypatch.setattr(dispatch_binding.timings, "record", lambda **row: rows.append(row))
    def host(actual, payload, **kwargs):
        assert actual is agent and payload is request
        assert not active[0]
        active[0] = True
        try:
            calls.append(actual.api_key)
            assert actual.api_key == actual.client.api_key == actual._client_kwargs["api_key"]
            if len(calls) == 1:
                raise Boom("fixture unavailable", 503)
            return Answer("recovered")
        finally:
            active[0] = False
    binding = DispatchBinding(engine=Carousel())
    result = binding.run(host, agent, request, (), {})
    assert result.choices[0].message.content == "recovered"
    assert len(calls) == 2 and calls[0] != calls[1]
    assert binding.rotations == 1
    assert len(rows) == 2 and len({r["call_id"] for r in rows}) == 1
    assert not any(r.get("record_type") == "wire_attempt" for r in rows)


def test_interruption_has_no_second_attempt_even_with_stale_flag(monkeypatch):
    monkeypatch.setenv("KAME_PARALLEL_REQUESTS", "1")
    calls = []
    def host(agent, payload, **kwargs):
        calls.append(agent.api_key)
        raise InterruptedError("owner cancelled")
    binding = DispatchBinding(engine=Carousel())
    with pytest.raises(InterruptedError):
        binding.run(host, Agent(), {}, (), {})
    assert len(calls) == 1 and binding.rotations == 0


def test_experiment_is_absent_from_package_schema_and_settings():
    assert not (SOURCE / "parallel.py").exists()
    manifest = (SOURCE / "plugin.yaml").read_text(encoding="utf-8")
    for key in ("parallel_requests", "parallel_delay_seconds"):
        assert f"\n  {key}:" not in manifest
        assert not settings.known(key)
        assert key not in settings.META
    spec = importlib.util.spec_from_file_location("serial_package_inventory", ROOT / "tools/package.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    inventory = [str(p.relative_to(SOURCE)) for p in module.shipped_files()]
    assert "parallel.py" not in inventory and "scope.py" in inventory
    source = (SOURCE / "dispatch_binding.py").read_text(encoding="utf-8")
    assert "parallel.RequestScope" not in source and "KAME_PARALLEL_REQUESTS" not in source
