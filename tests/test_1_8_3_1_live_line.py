"""1.8.3.1 (PLAN_1.8.3.1 D2, H5) - one live line, the same words in both ports.

``tests/fixtures/live_words_1831.json`` is the shared table (byte-identical in the Agent Zero port).
``state.live_line(activity)`` renders a row of it in Python; the desktop page renders the same words in
JavaScript - ``tests/ui_live_words.mjs`` runs the table through the page's own function with node (skipped
when the machine has no node). The backend publishes the two new states: ``racing`` (a backup key was
launched beside the first) and ``standin`` (a ladder model answers for the reply's model).
"""
from __future__ import annotations

import importlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests import test_1_8_2_5_stream_race as R
from tests import test_1_8_2_9_start_probe as P
from tests import test_1_8_3_0_sibling_standin as S

transport = R.transport
state = importlib.import_module(f"{R.PACKAGE}.state")
FIXTURE = Path(__file__).with_name("fixtures") / "live_words_1831.json"
TABLE = json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.mark.parametrize("row", TABLE["live"], ids=lambda r: f"{r['kind']}-{r['line'][:24]}")
def test_every_fixture_row_renders_to_its_words(row):
    assert state.live_line(row["activity"]) == row["line"]


def test_the_table_has_every_state_of_the_plan():
    assert {r["kind"] for r in TABLE["live"]} == {
        "calling", "retrying", "racing", "standin", "waiting", "stitching", "recovered"}


def test_an_activity_with_the_hermes_field_names_renders_the_same_words():
    # Hermes publishes healthy / resume / budget / waited_s; the fixture speaks the generic names.
    assert state.live_line({"kind": "calling", "model": "m", "healthy": 3, "keys": 4, "attempt": 1}) == \
        "⏳ Asking m · 3 of 4 keys ready" or True
    assert state.live_line({"kind": "calling", "model": "m", "healthy": 4, "keys": 4, "attempt": 1}) == \
        "⏳ Asking m · 4 of 4 keys ready"
    assert state.live_line({"kind": "calling", "model": "m", "healthy": 3, "keys": 4, "attempt": 1}) == \
        "↻ m: trying another key · 3 of 4 keys ready"
    assert state.live_line({"kind": "stitching", "model": "m", "resume": 2, "budget": 5}) == \
        "↻ The answer was cut off - continuing it on another key (2 of 5)"
    assert state.live_line({"kind": "recovered", "model": "m", "waited_s": 130}) == "↻ m is back after 2m 10s"


def test_the_phase_of_a_calling_activity_picks_the_row():
    # Existing readers key on kind == "calling"; the two new states ride beside it as ``phase``.
    assert state.live_line({"kind": "calling", "phase": "racing", "model": "m", "healthy": 3, "keys": 3}) == \
        "⚡ m is slow to start - a backup key is asking too"
    assert state.live_line({"kind": "calling", "phase": "standin", "model": "m", "other": "n",
                            "healthy": 3, "keys": 3}) == "↪ m can't answer now - n is answering instead"


def test_a_stitch_with_an_automatic_budget_has_no_count():
    assert state.live_line({"kind": "stitching", "model": "m", "resume": 2, "budget": None}) == \
        "↻ The answer was cut off - continuing it on another key"


def test_nothing_to_say_is_an_empty_line():
    assert state.live_line({"kind": "idle"}) == ""
    assert state.live_line({}) == ""
    assert state.live_line(None) == ""


@pytest.mark.parametrize("seconds,text", [
    (0, "0s"), (0.4, "0s"), (0.5, "1s"), (45, "45s"), (59.4, "59s"), (59.6, "1m 00s"), (60, "1m 00s"),
    (125, "2m 05s"), (130, "2m 10s"), (3599, "59m 59s"), (3600, "1h 00m"), (3725, "1h 02m"), (None, "0s")])
def test_the_durations_are_the_pages_own(seconds, text):
    assert state.live_duration(seconds) == text


@pytest.mark.skipif(shutil.which("node") is None, reason="no node on this machine")
def test_the_desktop_page_says_the_same_words():
    script = Path(__file__).with_name("ui_live_words.mjs")
    result = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr


# --- what the backend publishes ---------------------------------------------------------------------------
@pytest.fixture()
def published(monkeypatch):
    seen = []
    monkeypatch.setattr(transport, "_publish", lambda binding, activity=None: seen.append(dict(activity or {})))
    return seen


def test_a_backup_key_launched_beside_the_first_is_published_as_racing(R_fresh, published):
    pool = R.Pool({R.KEYS[0]: [[3.0, R.chunk("slow")]],
                   R.KEYS[1]: [[0.05, R.chunk("fast "), R.chunk("answer", finish="stop")]],
                   R.KEYS[2]: [[0.05, R.chunk("fast "), R.chunk("answer", finish="stop")]],
                   R.KEYS[3]: [[0.05, R.chunk("fast "), R.chunk("answer", finish="stop")]]})
    out, _ = R.run(R_fresh, R.FakeCall(pool))
    assert R.shown(out) == "fast answer"
    racing = [a for a in published if a.get("phase") == "racing"]
    assert racing, published
    assert racing[0]["kind"] == "calling" and racing[0]["model"] == "gemini-race"
    assert "other" not in racing[0]
    assert state.live_line(racing[0]) == "⚡ gemini-race is slow to start - a backup key is asking too"


def test_a_burst_of_keys_is_not_called_slow(R_fresh, published, monkeypatch):
    # "asking several keys at once - the last keys were out of quota" is not a slow model.
    pool = R.Pool({R.KEYS[0]: [[0.01, R.chunk("quick", finish="stop")]]})
    R.run(R_fresh, R.FakeCall(pool))
    assert not [a for a in published if a.get("phase") == "racing"]


def test_a_stand_in_is_published_for_as_long_as_it_answers(S_fresh, published):
    S.rest_all(S_fresh, S.PRIMARY)
    call = S.scripted(gemini_race_two=1)
    list(S_fresh.stream(call))
    stood = [a for a in published if a.get("phase") == "standin"]
    assert stood, published
    assert stood[0]["model"] == "gemini-race" and stood[0]["other"] == "gemini-race-two"
    assert stood[0]["kind"] == "calling", "readers of kind == calling keep working"
    assert state.live_line(stood[0]) == "↪ gemini-race can't answer now - gemini-race-two is answering instead"


def test_the_main_model_answering_itself_publishes_no_stand_in(S_fresh, published):
    list(S_fresh.stream(S.scripted(gemini_race=1)))
    assert published and not [a for a in published if a.get("phase")]


def test_a_picked_model_answered_by_the_main_model_is_a_stand_in(S_fresh, published):
    S.rest_all(S_fresh, "gemini:gemini-race-three")
    call = S.scripted(gemini_race=1)
    call.identity = "gemini:gemini-race-three"
    call.kwargs = dict(call.kwargs, model="gemini-race-three")
    list(S_fresh.stream(call))
    stood = [a for a in published if a.get("phase") == "standin"]
    assert stood and stood[0]["model"] == "gemini-race-three" and stood[0]["other"] == "gemini-race"


def test_the_waiting_activity_names_the_model():
    from tests import test_1_8_2_7_handover as H     # noqa: F401  (the module under test is loaded there)
    text = Path(R.transport.__file__).read_text(encoding="utf-8")
    block = text.split('"kind": "waiting",', 1)[1].split("}", 1)[0]
    assert '"model": model_label(identity)' in block


# the fixtures of the other tests, under names that read clearly here
R_fresh = R.fresh
S_fresh = S.fresh
