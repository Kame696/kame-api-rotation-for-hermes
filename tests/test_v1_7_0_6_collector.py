"""The standalone collector's boundaries, without credentials or network."""
import importlib.util
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("kame_observer_706",
    Path(__file__).resolve().parents[1] / "tools" / "gemini_observe.py")
observer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(observer)


def test_redacts_configured_and_key_shaped_values():
    fake = "AIza" + "x" * 30
    value = {"error": {"message": "oops " + fake + " configured-secret"}}
    redacted = observer.redact(value, ["configured-secret"])
    assert redacted == {"error": {"message": "oops [KEY] [KEY]"}}
    assert fake in value["error"]["message"]  # never mutates the raw input


def test_collector_does_not_adopt_a_one_second_spin():
    assert observer.retry_seconds({}, {"Retry-After": "1"}) == 20
    assert observer.retry_seconds({}, {"Retry-After": "35"}) == 35
    assert observer.retry_seconds({"error": {"details": [{"retryDelay": "42s"}]}}, {}) == 42
    assert observer.retry_seconds({"error": {"details": [None, {"retryDelay": "900ms"}]}}, {}) == 20
