"""1.8.4.2 (L1) - a model whose race leg was refused as busy is not handed the same reply again.

Live 2026-11-11 13:34: the best model silent then 503, the next model raced in at 12 s and answered 503 too;
the hand-over then circled the reply to that very model AGAIN (a second try, another 503) before the third
answered. A raced model's verdict is not its pool's evidence (D12) so nothing told the hand-over. Now the
models whose leg was refused busy in THIS reply are skipped by every hand-over of THIS reply; the next reply
decides from the window and the record, not from this list.

Fixtures are those of ``test_1_8_2_9_silent_two_models``: a slow bound of a few seconds, the next model joining
the race at 0.3 s and the one after it at 0.7 s.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from tests import test_1_8_2_9_silent_two_models as T
from tests.test_1_8_2_9_silent_two_models import ByModel, silent_pool  # noqa: F401
from tests.test_1_8_2_9_silent_two_models import fresh as _fresh      # noqa: F401  (reused below)
from tests.test_1_8_2_9_start_probe import Refusal

R = T.R
transport = T.transport
settings = T.settings
chain_mod = T.chain_mod

THREE_MODELS = """\
model:
  provider: gemini
  default: gemini-race
fallback_providers:
  - provider: gemini
    model: gemini-race-two
  - provider: gemini
    model: gemini-race-three
"""
OVERLOADED = "The model is overloaded. Please try again later."


@pytest.fixture()
def fresh(_fresh, monkeypatch):
    # a wide slow bound: nothing in these tests may widen the race by accident
    monkeypatch.setattr(transport, "_SLOW_S", {False: (3.0, 3.0, 3.0), True: (3.0, 3.0, 3.0)})
    # a backup key of the home model joins at 0.1 s: two keys of it refused busy make the model busy at once
    monkeypatch.setattr(R.hedging_mod.HEDGE_TRACKER, "stream_triggers", lambda *a, **k: (0.1, 60.0))
    return _fresh


class Sequence(ByModel):
    """``steps[model]`` is a list: the first ask of a model gets the first entry, the next ask the next one.
    An entry is the raw stream steps; a model with no entry (or past its entries) is silent."""

    def __init__(self, pool, steps):
        super().__init__(pool)
        self.steps = {m: list(v) for m, v in steps.items()}

    def client_for(self, key, attempt):
        def create(**request):
            model = request.get("model")
            self.models_asked.append(model)
            self.asked_at.setdefault(model, time.monotonic())
            queue = self.steps.get(model)
            if queue:
                return R.FakeStream(list(queue.pop(0)))
            return self.pool.open(key)
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def answer(delay=0.05):
    return [delay, R.chunk("answered "), R.chunk("here", finish="stop")]


def test_the_hand_over_does_not_circle_back_to_a_model_whose_leg_was_refused_busy(fresh, tmp_path):
    (tmp_path / "config.yaml").write_text(T.CHAIN_3, encoding="utf-8")        # race, two, three, four
    chain_mod.forget()
    call = Sequence(silent_pool(), {
        "gemini-race": [[0.9, Refusal(503, OVERLOADED)], [0.8, Refusal(503, OVERLOADED)]],
        "gemini-race-two": [[0.2, Refusal(503, OVERLOADED)], answer()],       # a second ask WOULD answer
        "gemini-race-three": [[0.2, Refusal(503, OVERLOADED)], answer()],
        "gemini-race-four": [answer()],
    })
    out = list(fresh.stream(call))
    assert R.shown(out) == "answered here"
    assert call.models_asked.count("gemini-race-two") == 1, call.models_asked
    assert call.models_asked.count("gemini-race-three") == 1, call.models_asked
    assert call.models_asked[-1] == "gemini-race-four", call.models_asked
    assert getattr(call, "_kame_answered_by", "") == "gemini:gemini-race-four"


def test_with_nothing_left_the_hand_over_goes_to_hermes_as_today(fresh, tmp_path):
    (tmp_path / "config.yaml").write_text(THREE_MODELS, encoding="utf-8")
    chain_mod.forget()
    call = Sequence(silent_pool(), {
        "gemini-race": [[0.9, Refusal(503, OVERLOADED)], [0.8, Refusal(503, OVERLOADED)]],
        "gemini-race-two": [[0.2, Refusal(503, OVERLOADED)], answer()],
        "gemini-race-three": [[0.2, Refusal(503, OVERLOADED)], answer()],
    })
    with pytest.raises(transport.KamePoolResting):
        list(fresh.stream(call))
    assert call.models_asked.count("gemini-race-two") == 1 and call.models_asked.count("gemini-race-three") == 1, \
        call.models_asked


def test_a_429_on_the_raced_leg_is_not_busy_and_the_model_stays_a_candidate(fresh, tmp_path):
    (tmp_path / "config.yaml").write_text(T.CHAIN_3, encoding="utf-8")
    chain_mod.forget()
    call = Sequence(silent_pool(), {
        "gemini-race": [[0.9, Refusal(503, OVERLOADED)], [0.8, Refusal(503, OVERLOADED)]],
        "gemini-race-two": [[0.2, R.per_minute()], answer()],                 # a throttle: not the provider's trouble
        "gemini-race-three": [[0.2, Refusal(503, OVERLOADED)], answer()],
        "gemini-race-four": [answer()],
    })
    out = list(fresh.stream(call))
    assert R.shown(out) == "answered here"
    assert call.models_asked.count("gemini-race-two") == 2, call.models_asked   # it was asked again
    assert call.models_asked.count("gemini-race-three") == 1, call.models_asked


def test_the_avoid_list_is_for_this_reply_only(fresh, tmp_path):
    (tmp_path / "config.yaml").write_text(T.CHAIN_3, encoding="utf-8")
    chain_mod.forget()
    first = Sequence(silent_pool(), {
        "gemini-race": [[0.9, Refusal(503, OVERLOADED)], [0.8, Refusal(503, OVERLOADED)]],
        "gemini-race-two": [[0.2, Refusal(503, OVERLOADED)]],
        "gemini-race-three": [[0.2, Refusal(503, OVERLOADED)]],
        "gemini-race-four": [answer()],
    })
    list(fresh.stream(first))
    fresh._busy_until.clear()
    fresh._busy_window.clear()
    fresh._probed_at.clear()
    nxt = Sequence(silent_pool(), {"gemini-race-two": [answer()], "gemini-race": [[0.6, Refusal(503, OVERLOADED)]]})
    out = list(fresh.stream(nxt))
    assert R.shown(out) == "answered here"
    assert "gemini-race-two" in nxt.models_asked, "the next reply decides from the window and the record"
