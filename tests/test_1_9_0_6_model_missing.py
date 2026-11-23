"""1.9.0.6 (decisions/0037 C13) - a model the provider does not know is reported, not retried all day.

The owner's real refusal (refusals.jsonl, keys removed): Gemini 404 "Model 'gemini-3.7.3.6' not found. Did you mean
'gemini-3.7-flash'?".
"""
from __future__ import annotations

import importlib
import logging
import os
import time
from types import SimpleNamespace

import pytest

from tests import test_1_8_2_5_stream_race as R
from tests import test_1_8_2_9_start_probe as P

transport, settings, chain_mod = R.transport, R.settings, P.chain_mod
mm = importlib.import_module(f"{R.PACKAGE}.core.model_missing")
state = importlib.import_module(f"{R.PACKAGE}.state")
doctor = importlib.import_module(f"{R.PACKAGE}.core.doctor")

OWNER_MESSAGE = ("Gemini HTTP 404 (not_found): Model 'gemini-3.7.3.6' not found. Did you mean 'gemini-3.7-flash'? "
                 "Please verify the model name against the supported list: https://ai.google.dev/gemini-api/docs/models")
CHAIN = """\
model:
  provider: gemini
  default: gemini-race
fallback_providers:
  - provider: gemini
    model: gemini-3.7.3.6
  - provider: gemini
    model: gemini-race-three
"""
MISSING = "gemini:gemini-3.7.3.6"
WANT = ("Model 'gemini-3.7.3.6' was not found by Gemini - check the name in your fallback list "
        "(it suggests 'gemini-3.7-flash')")


class NotFound(Exception):
    status_code = 404

    def __init__(self, text=OWNER_MESSAGE):
        super().__init__(text)
        self.body = {}


@pytest.fixture()
def fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for name in list(os.environ):
        if name.startswith("KAME_"):
            monkeypatch.delenv(name, raising=False)
    (tmp_path / "config.yaml").write_text(CHAIN, encoding="utf-8")
    settings.forget()
    chain_mod.forget()
    mm.forget()
    R.hedging_mod.HEDGE_TRACKER.clear()
    R.pacing_mod.PACER.clear()
    monkeypatch.setattr(R.hedging_mod.HEDGE_TRACKER, "stream_triggers", lambda *a, **k: (30.0, 60.0))
    t = transport.KameTransport(engine=R.carousel_mod.Carousel())
    yield t
    settings.forget()
    chain_mod.forget()
    mm.forget()


def test_the_owners_real_refusal_is_read_with_its_suggestion():
    assert mm.detect(404, OWNER_MESSAGE, None) == {"written": "gemini-3.7.3.6", "suggestion": "gemini-3.7-flash"}


@pytest.mark.parametrize("status,message,body", [
    (404, "models/gemini-x is not found for API version v1beta, or is not supported for generateContent", None),
    (404, "The model `gpt-9` does not exist",
     {"error": {"code": "model_not_found", "message": "The model `gpt-9` does not exist"}}),
    (404, "model: claude-9", {"error": {"type": "not_found_error", "message": "model: claude-9"}}),
])
def test_other_providers_wording_is_read(status, message, body):
    assert mm.detect(status, message, body) is not None


@pytest.mark.parametrize("status,message", [(404, "Not Found"), (400, "Model 'x' not found"), (429, "model not found"),
                                            (404, "route /v1/chat not found")])
def test_a_404_that_is_not_about_a_model_is_not_this(status, message):
    assert mm.detect(status, message, None) is None


def test_marked_models_are_skipped_by_every_picker(fresh):
    mm.mark(MISSING + ":global", written="gemini-3.7.3.6", suggestion="gemini-3.7-flash")
    assert mm.is_missing(MISSING) and not fresh._entry_usable(MISSING) and not fresh._entry_free(MISSING)
    call = R.FakeCall(P.pool())
    for key in R.KEYS:
        fresh.engine.mark("gemini:gemini-race", key, False, 3600.0, "daily")
    stand = fresh._maybe_hand_over(call, "gemini:gemini-race", R.KEYS, 0.0, False, stand_in_ok=True)
    assert isinstance(stand, transport._StandIn) and stand.identity == "gemini:gemini-race-three"
    widen = fresh._widen_candidate(call, "gemini:gemini-race", R.KEYS, [])
    assert widen is None or widen[0] != MISSING


def test_a_reply_that_reaches_the_missing_model_marks_it_and_a_sibling_takes_the_reply(fresh):
    class Call(P.Scripted):
        def client_for(self, key, attempt):
            def create(**request):
                model = request.get("model")
                self.models_asked.append(model)
                if model == "gemini-3.7.3.6":
                    raise NotFound()
                return R.FakeStream([0.02, R.chunk(f"{model} "), R.chunk("here", finish="stop")])
            return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    call = Call(P.pool(), {})
    call.identity = MISSING
    call.kwargs = dict(call.kwargs, model="gemini-3.7.3.6")
    out = list(fresh.stream(call))
    assert R.shown(out) in ("gemini-race here", "gemini-race-three here"), R.shown(out)      # a usable sibling
    assert call.models_asked.count("gemini-3.7.3.6") == 1, "the missing model was asked once, never again"
    assert mm.is_missing(MISSING)


def test_a_config_change_ends_the_mark(fresh, tmp_path):
    mm.mark(MISSING, written="gemini-3.7.3.6")
    assert mm.is_missing(MISSING)
    path = tmp_path / "config.yaml"
    later = path.stat().st_mtime + 5
    os.utime(path, (later, later))
    assert not mm.is_missing(MISSING)


def test_six_hours_end_the_mark(fresh, monkeypatch):
    mm.mark(MISSING, written="gemini-3.7.3.6")
    real = time.time
    monkeypatch.setattr(mm.time, "time", lambda: real() + mm.HOLD_S + 1)
    assert not mm.is_missing(MISSING)


def test_the_doctor_and_the_panel_say_it_with_the_suggestion(fresh):
    mm.mark(MISSING, written="gemini-3.7.3.6", suggestion="gemini-3.7-flash")
    snap = state.snapshot(fresh)
    assert snap["missing_models"][0]["line"] == WANT
    assert WANT in doctor.trouble_lines(snap, ())[0]
    assert state._fallback_view([])["missing"] == [WANT]


def test_the_log_names_the_model_not_the_request(fresh, caplog):
    caplog.set_level(logging.WARNING)
    call = R.FakeCall(P.pool())
    verdict = fresh._on_failure(MISSING + ":global", R.KEYS[0], NotFound(), "label", 1, False, call=call)
    assert verdict[1] == "model_missing"
    assert "gemini-3.7.3.6" in caplog.text and "check the name in your fallback list" in caplog.text


def test_a_model_outside_the_keys_plan_is_not_a_missing_model():
    """Review fix (provider answer key, zenmux): the model exists, this key's plan does not include it."""
    body = {"error": {"code": "404", "type": "model_not_available",
                      "message": "The requested model is not included in your subscription plan."}}
    assert mm.detect(404, "The requested model is not included in your subscription plan.", body) is None
