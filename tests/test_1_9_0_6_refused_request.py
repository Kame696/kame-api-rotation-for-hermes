"""1.9.0.6 (decisions/0037 C22) - a refused request tries the siblings before the reply ends.

Parity with Agent Zero 0019 A6: a model that refuses the REQUEST (410 gone, 400 the provider calls the request's
fault) is not the end of the reply. Each usable sibling of the user's list is asked once, inside KAME; only when every
one refused does Hermes get the FIRST model's own error (its compression on a 413 and its own fallback still work).
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from tests import test_1_8_2_5_stream_race as R
from tests import test_1_8_2_9_start_probe as P
from tests import test_1_9_0_6_model_missing as M

transport, settings, chain_mod = R.transport, R.settings, P.chain_mod
fresh = M.fresh  # noqa: F811 - the same three-model Gemini list (fixture by name)

HOME = "gemini-race"


class Refused(Exception):
    def __init__(self, status, text):
        super().__init__(text)
        self.status_code = status
        self.body = {}


def _call(refuses):
    """A main-chat stream on the home model; ``refuses`` maps a model name to the error it raises."""
    class Call(P.Scripted):
        def client_for(self, key, attempt):
            def create(**request):
                model = request.get("model")
                self.models_asked.append(model)
                if model in refuses:
                    raise refuses[model]
                return R.FakeStream([0.02, R.chunk(f"{model} "), R.chunk("here", finish="stop")])
            return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    call = Call(P.pool(), {})
    call.kwargs = dict(call.kwargs, model=HOME)
    return call


def test_a_gone_model_hands_the_reply_to_a_free_sibling(fresh):
    gone = Refused(410, "Gone: this model version was retired")
    call = _call({HOME: gone})
    out = list(fresh.stream(call))
    assert R.shown(out) in ("gemini-3.7.3.6 here", "gemini-race-three here"), R.shown(out)
    assert call.models_asked.count(HOME) == 1, "the model that refused is never asked again in this reply"


def test_every_model_refusing_raises_the_first_models_own_error_once(fresh):
    first = Refused(400, "Invalid argument: the request itself is malformed (home)")
    refuses = {HOME: first,
               "gemini-3.7.3.6": Refused(400, "Invalid argument: the request itself is malformed (2)"),
               "gemini-race-three": Refused(400, "Invalid argument: the request itself is malformed (3)")}
    call = _call(refuses)
    with pytest.raises(Exception) as caught:
        list(fresh.stream(call))
    assert "(home)" in str(caught.value), str(caught.value)
    for model in refuses:
        assert call.models_asked.count(model) <= 1, call.models_asked
    assert call.models_asked.count(HOME) == 1


def test_a_refused_title_job_is_not_moved(fresh):
    """Background jobs keep their own path (C18): C22 is for the reply itself."""
    call = _call({HOME: Refused(400, "Invalid argument: bad request")})
    call.channel = "aux"
    with pytest.raises(Exception):
        list(fresh.stream(call))
    assert call.models_asked == [HOME]
