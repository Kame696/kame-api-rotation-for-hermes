"""1.8.2.9 (decisions/0031 P1, P8): what the snapshot says so the panel can draw a ring per model,
and the two panel requests that act on ONE model.

Run:  uv run --with pytest --with pyyaml python -m pytest tests/test_1_8_2_9_snapshot_for_rings.py -q
"""
from __future__ import annotations

import importlib
import json
import os
import time
from types import SimpleNamespace

import pytest

from tests import test_1_8_2_5_stream_race as R

state = importlib.import_module(f"{R.PACKAGE}.state")
control = importlib.import_module(f"{R.PACKAGE}.control")
carousel_mod = importlib.import_module(f"{R.PACKAGE}.core.carousel")
events_mod = importlib.import_module(f"{R.PACKAGE}.core.events")
facade = importlib.import_module(f"{R.PACKAGE}.facade")
settings = importlib.import_module(f"{R.PACKAGE}.settings")
transport = R.transport
ENGINE = carousel_mod.ENGINE
fingerprint = carousel_mod.fingerprint

FIRST = "gemini:gemini-race"
SECOND = "gemini:gemini-race-two"
KEYS = [f"AIzaSyRING{i}" + "0" * 29 for i in range(6)]
CHAIN = ("model:\n  provider: gemini\n  default: gemini-race\nfallback_providers:\n"
         "  - provider: gemini\n    model: gemini-race-two\n")


@pytest.fixture()
def world(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for name in list(os.environ):
        if name.startswith("KAME_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KAME_RECORDER_DISABLED", "1")
    (tmp_path / "config.yaml").write_text(CHAIN, encoding="utf-8")
    settings.forget()
    chain = importlib.import_module(f"{R.PACKAGE}.core.fallback_chain")
    chain.forget()
    ENGINE.forget()
    events_mod.INFLIGHT.clear()
    for identity in (FIRST, SECOND):
        ENGINE.select(identity, KEYS)
    t = facade.TRANSPORT
    t._busy_until.clear(); t._busy_window.clear(); t._busy_kind.clear(); t._probed_at.clear()
    t._speed.clear(); t._speed_total[:] = [0, 0, 0, 0, 0]
    yield t
    ENGINE.forget()
    events_mod.INFLIGHT.clear()
    settings.forget()
    chain.forget()


def states_of(identity):
    row = next(r for r in state._pool_rows() if r["identity"] == identity)
    return {fp: (code, until, kind) for fp, code, until, kind in row["key_states"]}, row


def script_pool(identity=FIRST):
    ENGINE.mark(identity, KEYS[1], False, 45.0, "per_minute")
    ENGINE.mark(identity, KEYS[2], False, 3600.0, "daily")
    ENGINE.mark(identity, KEYS[3], False, 3600.0, "auth")
    ENGINE._pools[identity][KEYS[4]]["retired"] = True


def test_each_key_gets_its_code_and_the_list_is_in_a_stable_order(world):
    script_pool()
    by_fp, row = states_of(FIRST)
    fp = [fingerprint(k) for k in KEYS]
    assert by_fp[fp[0]][0] == "r" and by_fp[fp[5]][0] == "r"
    assert by_fp[fp[1]][0] == "s" and by_fp[fp[1]][2] == "per_minute"
    assert by_fp[fp[2]][0] == "d" and by_fp[fp[2]][2] == "daily"
    assert by_fp[fp[3]][0] == "x"
    assert by_fp[fp[4]][0] == "o"
    assert [e[0] for e in row["key_states"]] == sorted(e[0] for e in row["key_states"])   # fingerprint order


def test_until_is_an_epoch_and_does_not_drift_between_two_reads(world):
    script_pool()
    first, _ = states_of(FIRST)
    time.sleep(0.05)
    second, _ = states_of(FIRST)
    assert first == second
    fp = fingerprint(KEYS[1])
    assert first[fp][1] > time.time() + 30


def test_the_key_a_running_call_put_on_the_wire_is_marked_in_flight(world):
    call = SimpleNamespace(channel="main", task="", identity=FIRST)
    events_mod.INFLIGHT.start(call)
    events_mod.INFLIGHT.note(call, "sent", "1 of 6 key(s) ready", identity=FIRST, fingerprint=fingerprint(KEYS[0]))
    by_fp, row = states_of(FIRST)
    assert by_fp[fingerprint(KEYS[0])][0] == "f" and row["in_flight"] == 1
    events_mod.INFLIGHT.end(call)
    by_fp, row = states_of(FIRST)
    assert by_fp[fingerprint(KEYS[0])][0] == "r" and row["in_flight"] == 0


def test_a_busy_and_a_slow_model_say_which_and_when_they_may_be_asked_again(world):
    t = world
    assert t.busy_view(SECOND) == {"busy_until": None, "busy_kind": None, "probe_at": None}
    t._note_busy(SECOND)
    view = t.busy_view(SECOND)
    assert view["busy_kind"] == "busy" and view["busy_until"] > time.time()
    assert view["probe_at"] == pytest.approx(t._probed(SECOND) + transport._PROBE_EVERY_S, abs=0.5)
    t._note_busy(SECOND, first=transport._SLOW_FIRST_S, ceiling=transport._SLOW_MAX_S)
    assert t.busy_view(SECOND)["busy_kind"] == "slow"


def test_the_snapshot_carries_the_busy_fields_the_speed_block_and_stays_small(world):
    t = world
    t._note_busy(SECOND)
    t._tally(transport._SPEED_REPLIES)
    t._tally(transport._SPEED_BACKUP)
    snap = state.snapshot(t)
    entry = next(e for e in snap["fallback"]["entries"] if e["identity"] == SECOND)
    assert entry["busy_until"] and entry["busy_kind"] == "busy"
    assert snap["speed"]["since_start"]["extra_pct"] == 100.0
    assert snap["speed"]["mode"] == "adaptive" and snap["speed"]["cap_pct"] == 10
    # 4 models x 14 keys of key_states: well under the 6 KB the design allows
    keys = [f"AIzaSyBIG{i:02d}" + "0" * 27 for i in range(14)]
    for model in range(4):
        ENGINE.select(f"gemini:big-{model}", keys)
        ENGINE.mark(f"gemini:big-{model}", keys[0], False, 45.0, "per_minute")
    rows = [r for r in state._pool_rows() if r["identity"].startswith("gemini:big-")]
    assert len(json.dumps([r["key_states"] for r in rows])) < 6000


def test_the_speed_view_counts_extra_requests_against_replies(world):
    t = world
    for _ in range(10):
        t._tally(transport._SPEED_REPLIES)
    t._tally(transport._SPEED_BACKUP)
    t._tally(transport._SPEED_RACE)
    hour = t.speed_view()["hour"]
    assert hour["replies"] == 10 and hour["backup"] == 1 and hour["race"] == 1 and hour["extra_pct"] == 20.0


# --- the two requests that act on one model (P8) ----------------------------------------

def test_wake_this_model_clears_only_that_pool(world):
    script_pool(FIRST)
    ENGINE.mark(SECOND, KEYS[1], False, 3600.0, "daily")
    ok, detail = control._apply("clear_pool", FIRST, None)
    assert ok, detail
    assert {code for code, _u, _k in states_of(FIRST)[0].values()} == {"r"}   # every key as if never tried
    assert states_of(SECOND)[0][fingerprint(KEYS[1])][0] == "d"           # the other model keeps its bench


def test_wake_this_model_refuses_a_model_kame_has_not_used(world):
    ok, detail = control._apply("clear_pool", "gemini:never-heard-of", None)
    assert not ok and "not used" in detail


def test_try_this_model_now_drops_the_busy_window_and_is_refused_when_there_is_none(world):
    t = world
    ok, detail = control._apply("forget_busy", SECOND, None)
    assert not ok and "not known busy" in detail
    t._note_busy(SECOND)
    assert t._entry_busy(SECOND)
    ok, detail = control._apply("forget_busy", SECOND, None)
    assert ok and not t._entry_busy(SECOND) and t.busy_view(SECOND)["busy_until"] is None
    ok, detail = control._apply("forget_busy", "", None)
    assert not ok


def test_the_empty_key_still_means_everything(world):
    script_pool(FIRST)
    ENGINE.mark(SECOND, KEYS[1], False, 3600.0, "daily")
    ok, detail = control._apply("clear_pool", "", None)
    assert ok, detail
    assert SECOND not in {r["identity"] for r in state._pool_rows()}      # as before: every pool is forgotten
