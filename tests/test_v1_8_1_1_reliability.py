"""Regression witnesses for privacy, reset acknowledgements and scoped timeouts."""
import importlib
import json
import threading
from contextvars import copy_context
from types import SimpleNamespace

import pytest
from tests.test_v1_8_1_0_clear_pool import PACKAGE, _engine, carousel_mod, control, shared_health, IDENTITY, KEYS

recorder = importlib.import_module(f"{PACKAGE}.recorder")
binding = importlib.import_module(f"{PACKAGE}.dispatch_binding")


@pytest.mark.parametrize("name", ["api_key", "refresh_token", "password", "access-token", "Authorization"])
@pytest.mark.parametrize("value", ['fixture-private-value', 'fixture-escaped-"secret-tail', 'short'])
def test_recorder_redacts_named_secrets(name, value):
    payload = {"error": [{name: value}], "quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}
    for text in (payload, json.dumps(payload)):
        result = recorder._safe_text(text)
        assert value not in result
        assert "secret-tail" not in result
        assert payload["quotaId"] in result


@pytest.mark.parametrize("text", ["Authorization: Bearer fixture-private-value", "password=fixture-private-value", "refresh_token: 'fixture-private-value'"])
def test_recorder_redacts_text_secrets(text):
    assert "fixture-private-value" not in recorder._safe_text(text)


def test_recorder_redacts_entire_composite_secret():
    payload = {"password": ["first-secret", "last-secret"], "token": {"nested": "inner-secret"}}
    for value in (payload, json.dumps(payload)):
        result = recorder._safe_text(value)
        assert all(secret not in result for secret in ("first-secret", "last-secret", "inner-secret"))


def test_reset_lock_failure_is_not_success(tmp_path, monkeypatch):
    engine, store = _engine(tmp_path / "pool-health.json")
    engine.mark(IDENTITY, KEYS[0], False, 600, "per_minute")
    monkeypatch.setattr(shared_health, "_acquire_file_lock", lambda *a, **kw: False)
    monkeypatch.setattr(carousel_mod, "ENGINE", engine)
    ok, detail = control._apply("clear_pool", "", None)
    assert not ok
    assert "shared" in detail.lower()


def test_reset_store_failure_is_reported(monkeypatch):
    package = importlib.import_module(PACKAGE)
    monkeypatch.setattr(package, "_binding", SimpleNamespace(_store=SimpleNamespace(clear=lambda: False), _journal=None))
    monkeypatch.setattr(carousel_mod.ENGINE, "reset_all", lambda: 0)
    ok, detail = control._apply("clear_pool", "", None)
    assert not ok and "ledger" in detail


def test_scoped_timeout_isolated_between_threads_and_host_workers(monkeypatch):
    monkeypatch.delenv("HERMES_STREAM_READ_TIMEOUT", raising=False)
    monkeypatch.setenv("KAME_STREAM_SILENCE_TIMEOUT", "60")
    original = lambda name, default: float(binding.os.environ.get(name, default))
    reader = binding._scoped_timeout_reader(original)
    barrier = threading.Barrier(2)
    results, errors = [], []
    def run(attempt):
        try:
            with binding._SilenceTimeout(SimpleNamespace(base_url="https://provider.invalid"), attempt):
                barrier.wait(timeout=5)
                result = []
                thread = threading.Thread(target=copy_context().run, args=(lambda: result.append(reader("HERMES_STREAM_READ_TIMEOUT", 120)),))
                thread.start(); thread.join(timeout=5)
                results.append((attempt, result[0]))
                assert "HERMES_STREAM_READ_TIMEOUT" not in binding.os.environ
                barrier.wait(timeout=5)
            assert reader("HERMES_STREAM_READ_TIMEOUT", 120) == 120
        except BaseException as exc:
            errors.append(exc)
    threads = [threading.Thread(target=run, args=(a,)) for a in (1, 3)]
    for thread in threads: thread.start()
    for thread in threads: thread.join(timeout=12)
    assert not errors
    assert sorted(results) == [(1, 60), (3, 15)]
    assert "HERMES_STREAM_READ_TIMEOUT" not in binding.os.environ


def test_timeout_cleanup_after_exception_and_explicit_host_override(monkeypatch):
    monkeypatch.setenv("KAME_STREAM_SILENCE_TIMEOUT", "60")
    monkeypatch.delenv("HERMES_STREAM_READ_TIMEOUT", raising=False)
    reader = binding._scoped_timeout_reader(lambda n, d: float(binding.os.environ.get(n, d)))
    with pytest.raises(RuntimeError):
        with binding._SilenceTimeout(SimpleNamespace(base_url="")):
            assert reader("HERMES_STREAM_READ_TIMEOUT", 120) == 60
            monkeypatch.setenv("HERMES_STREAM_READ_TIMEOUT", "700")
            assert reader("HERMES_STREAM_READ_TIMEOUT", 120) == 700
            raise RuntimeError("fixture")
    monkeypatch.delenv("HERMES_STREAM_READ_TIMEOUT")
    assert reader("HERMES_STREAM_READ_TIMEOUT", 120) == 120
