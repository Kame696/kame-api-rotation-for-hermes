"""1.8.2.7: handing a call to Hermes' native fallback chain (decisions/0025).

Design and the eight tests come from the senior review,
``notes/2026-10-16-fallback-design-review.md`` s4. KAME never chooses a model;
it only stops WAITING when every key of the model in use rests for long, the
user has a chain in Hermes, and has turned "never fall back" off.

Waiting is never exercised for real (it would sleep): the "waits" cases call
``_maybe_hand_over`` directly and assert it answers ``None``.

Run:  uv run --with pytest --with pyyaml python -m pytest tests/test_1_8_2_7_handover.py -q
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_handover_1827_under_test"


def _load_package():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


pkg = _load_package()
transport = importlib.import_module(f"{PACKAGE}.transport")
carousel_mod = importlib.import_module(f"{PACKAGE}.core.carousel")
chain_mod = importlib.import_module(f"{PACKAGE}.core.fallback_chain")
settings = importlib.import_module(f"{PACKAGE}.settings")

KEYS = [f"AIzaSyHND{i}" + "0" * 29 for i in range(3)]
PRIMARY = "gemini:gemini-3.8-flash"
SECOND = "gemini:gemini-3.7-flash"
THIRD = "gemini:gemini-3.6-flash"
REQUEST = {"model": "gemini-3.8-flash", "messages": [{"role": "user", "content": "hi"}]}

CONFIG = """\
model:
  provider: gemini
  default: gemini-3.8-flash
fallback_providers:
  - provider: gemini
    model: gemini-3.7-flash
  - provider: gemini
    model: gemini-3.6-flash
"""


class FakeCall(transport.Call):
    def __init__(self, identity=PRIMARY, keys=KEYS):
        self._keys = list(keys)
        self.kwargs = dict(REQUEST)
        self.provider = identity.split(":", 1)[0]
        self.identity = identity
        self.sent = 0

    def keys(self):
        return list(self._keys)

    def entry(self, key):
        return None

    def entry_id(self, key):
        return ""

    def client_for(self, key, attempt):
        def create(**request):
            self.sent += 1
            raise AssertionError("no request may be sent while every key rests")
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    def cancelled(self):
        return False


@pytest.fixture()
def fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for name in list(__import__("os").environ):
        if name.startswith("KAME_"):
            monkeypatch.delenv(name, raising=False)
    (tmp_path / "config.yaml").write_text(CONFIG, encoding="utf-8")
    settings.forget()
    chain_mod.forget()
    # The user's opt-in: "Stay on this model, always" OFF.
    monkeypatch.setenv("KAME_NO_MODEL_FALLBACK", "0")
    t = transport.KameTransport(engine=carousel_mod.Carousel())
    yield t
    settings.forget()
    chain_mod.forget()


def rest(t, identity, seconds=3600.0, kind="daily"):
    for key in KEYS:
        t.engine.mark(identity, key, False, seconds, kind)


def test_the_flag_override_is_really_off_in_the_fixture(fresh):
    assert not settings.is_on(settings.NO_MODEL_FALLBACK)


def test_chain_is_read_in_hermes_order(fresh):
    assert chain_mod.read() == (PRIMARY, (SECOND, THIRD))
    assert chain_mod.later_entries(PRIMARY) == [SECOND, THIRD]
    assert chain_mod.later_entries(SECOND) == [THIRD]
    assert chain_mod.later_entries(THIRD) == []
    assert chain_mod.later_entries(PRIMARY + ":global") == [SECOND, THIRD]


# 1
def test_handover_raises_a_synthetic_error_before_any_request(fresh):
    rest(fresh, PRIMARY)
    call = FakeCall()
    before = time.time()
    with pytest.raises(transport.KamePoolResting) as caught:
        fresh.complete(call)
    exc = caught.value
    assert call.sent == 0
    assert exc.status_code == 429
    assert not hasattr(exc, "response") and not hasattr(exc, "retry_after")
    # 1.8.2.8: the fallback lasts one reply; Hermes restores the primary on the
    # next one and KAME hands over again, without a request, while it rests.
    reset_at = exc.body["error"]["reset_at"]
    assert before < reset_at <= time.time() + transport._HANDOVER_HOST_BACK_S + 1
    assert fresh.surfaced == 0 and fresh.handed_over == 1  # a switch, not an error


# 2
@pytest.mark.parametrize("case", ["flag_on", "no_chain", "zero"])
def test_no_handover_when_flag_on_or_no_chain_or_zero(fresh, monkeypatch, tmp_path, case):
    rest(fresh, PRIMARY)
    if case == "flag_on":
        monkeypatch.setenv("KAME_NO_MODEL_FALLBACK", "1")
    elif case == "no_chain":
        (tmp_path / "config.yaml").write_text("model:\n  provider: gemini\n  default: gemini-3.8-flash\n")
        chain_mod.forget()
    else:
        monkeypatch.setenv("KAME_FAILOVER_AFTER", "0")
    settings.forget()
    assert fresh._maybe_hand_over(FakeCall(), PRIMARY, KEYS, 0.0, False) is None


# 3
def test_short_rest_is_waited_then_a_rebench_hands_over(fresh, monkeypatch):
    # 1.8.2.8: "After my time" (60 s) keeps the 1.8.2.7 rule exactly.
    monkeypatch.setenv("KAME_FAILOVER_AFTER", "60")
    rest(fresh, PRIMARY, seconds=45.0, kind="per_minute")
    assert fresh._maybe_hand_over(FakeCall(), PRIMARY, KEYS, 0.0, False) is None  # 45 s <= 60 s
    # woke after ~45 s, the keys were re-benched for an hour: waited + soonest > 60
    rest(fresh, PRIMARY, seconds=3600.0)
    assert isinstance(fresh._maybe_hand_over(FakeCall(), PRIMARY, KEYS, 45.0, False), transport.KamePoolResting)


# 4
def test_the_last_entry_or_no_usable_later_entry_waits(fresh):
    rest(fresh, THIRD)
    assert fresh._maybe_hand_over(FakeCall(THIRD), THIRD, KEYS, 0.0, False) is None  # last entry
    rest(fresh, PRIMARY)
    rest(fresh, SECOND)
    rest(fresh, THIRD)
    assert fresh._maybe_hand_over(FakeCall(), PRIMARY, KEYS, 0.0, False) is None  # nothing usable after it


# 5
def test_the_second_ask_waits(fresh):
    # The memo is per conversation (one status owner); 1.8.2.7's version of this
    # test passed two calls whose owners matched only because CPython reused the
    # first object's id.
    rest(fresh, PRIMARY)
    call = FakeCall()
    first = fresh._maybe_hand_over(call, PRIMARY, KEYS, 0.0, False)
    assert isinstance(first, transport.KamePoolResting)
    assert fresh._maybe_hand_over(call, PRIMARY, KEYS, 0.0, False) is None


# 1.8.2.8 (decisions/0028)
def test_automatic_hands_over_a_short_rest_that_the_old_60_s_rule_waited_out(fresh):
    rest(fresh, PRIMARY, seconds=45.0, kind="per_minute")
    assert settings.model_switch_mode() == "auto"
    assert isinstance(fresh._maybe_hand_over(FakeCall(), PRIMARY, KEYS, 0.0, False), transport.KamePoolResting)


def test_the_switch_clock_is_the_users_wait_not_only_the_resting_time(fresh):
    # Keys usable (a 503 rests a key 1 s): after a failure, 25 s of waiting
    # with nothing on screen is past the automatic bound (10 s cold).
    call = FakeCall()
    assert fresh._maybe_hand_over(call, PRIMARY, KEYS, 0.0, False, waited=5.0, after_failure=True) is None
    assert isinstance(fresh._maybe_hand_over(call, PRIMARY, KEYS, 0.0, False, waited=25.0, after_failure=True),
                      transport.KamePoolResting)


def test_a_busy_provider_hands_over_at_once_and_is_left_alone_for_a_while(fresh):
    call = FakeCall()
    before = time.time()
    exc = fresh._maybe_hand_over(call, PRIMARY, KEYS, 0.0, False, waited=0.5, busy=True, after_failure=True)
    assert isinstance(exc, transport.KamePoolResting)
    assert "busy" in str(exc)
    assert exc.body["error"]["reset_at"] <= time.time() + transport._HANDOVER_HOST_BACK_S + 1
    # a second ask of this model in the same conversation waits instead of bouncing
    assert fresh._handover_until[fresh._memo_key(call, PRIMARY)] >= before + 30.0 - 1


def test_never_ignores_the_busy_rule_and_after_my_time_follows_it(fresh, monkeypatch):
    monkeypatch.setenv("KAME_MODEL_SWITCH", "never")
    assert fresh._maybe_hand_over(FakeCall(), PRIMARY, KEYS, 0.0, False, waited=99.0, busy=True,
                                  after_failure=True) is None
    # "my time" is how long to wait for resting keys, not for a busy model (review M6)
    monkeypatch.setenv("KAME_MODEL_SWITCH", "timed")
    monkeypatch.setenv("KAME_FAILOVER_AFTER", "60")
    assert isinstance(fresh._maybe_hand_over(FakeCall(), PRIMARY, KEYS, 0.0, False, waited=5.0, busy=True,
                                             after_failure=True), transport.KamePoolResting)


def test_a_number_set_before_1828_keeps_meaning_my_time(fresh, monkeypatch):
    monkeypatch.setenv("KAME_FAILOVER_AFTER", "120")
    assert settings.model_switch_mode() == "timed"
    monkeypatch.setenv("KAME_FAILOVER_AFTER", "0")
    assert settings.model_switch_mode() == "never"


# 6
def test_the_hook_maps_the_handover_to_upstream_rate_limit(fresh, monkeypatch):
    def forbidden(**_):
        raise AssertionError("a hand-over is not a provider payload; the corpus must not record it")
    monkeypatch.setattr(pkg.recorder, "record", forbidden)
    exc = transport.KamePoolResting("kame: 3/3 keys resting", reset_at=1_900_000_000.0)
    # the flag ON must not flip it: the transport only raises it when the flag is off
    monkeypatch.setenv("KAME_NO_MODEL_FALLBACK", "1")
    settings.forget()
    result = pkg._on_api_error_classification(
        provider="gemini", model="gemini-3.8-flash", status_code=429,
        error_message=str(exc), error_body=exc.body, error=exc, error_type="KamePoolResting")
    assert result["reason"] == "upstream_rate_limit"
    assert result["should_rotate_credential"] is False
    assert result["should_fallback"] is True
    assert result["error_context"]["reset_at"] == 1_900_000_000.0


# 7
def test_nothing_is_handed_over_mid_stream(fresh):
    rest(fresh, PRIMARY)
    assert fresh._maybe_hand_over(FakeCall(), PRIMARY, KEYS, 0.0, True) is None


def test_a_chain_that_cannot_be_read_means_waiting_as_before(fresh, tmp_path):
    (tmp_path / "config.yaml").write_text(": : not yaml : [", encoding="utf-8")
    chain_mod.forget()
    rest(fresh, PRIMARY)
    assert fresh._maybe_hand_over(FakeCall(), PRIMARY, KEYS, 0.0, False) is None


def test_the_setting_is_declared_everywhere_the_panel_looks(fresh):
    rows = {row["key"]: row for row in settings.describe_all()}
    row = rows["failover_after_seconds"]
    assert row["default"] == 60.0 and row["min"] == 0.0 and row["max"] == 3600.0
    assert row["title"] and row["help"] and row["impact"] and row["when"]
