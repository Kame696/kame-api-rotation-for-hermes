"""1.9.0.3 (C3) - the Speed cost line says what it measures.

Evidence (owner's screenshot): "Extra requests this hour: +337.5% (8 replies). Your setting allows about +10%."
This hour: burst 26, race 1, backup 0, rescue 0. Only backup requests draw from the Speed budget; burst legs and
cross-model races do not, so the line compared two different things.
"""
from __future__ import annotations

import importlib

from tests import test_1_8_2_5_stream_race as R

transport = R.transport
carousel_mod = importlib.import_module(f"{R.PACKAGE}.core.carousel")


def _t():
    t = transport.KameTransport(engine=carousel_mod.Carousel())
    t._speed.clear()
    t._speed_total[:] = [0] * transport._SPEED_COLUMNS
    return t


def test_the_owners_numbers_are_not_limited_by_speed():
    t = _t()
    for _ in range(8):
        t._tally(transport._SPEED_REPLIES)
    for _ in range(26):
        t._tally(transport._SPEED_BURST)
    t._tally(transport._SPEED_RACE)
    hour = t.speed_view()["hour"]
    assert hour["replies"] == 8 and hour["burst"] == 26 and hour["race"] == 1 and hour["backup"] == 0
    assert hour["limited"] == 0 and hour["limited_pct"] == 0.0
    assert hour["extra_pct"] == 337.5, "every older field is kept"


def test_backup_requests_are_the_limited_ones():
    t = _t()
    for _ in range(10):
        t._tally(transport._SPEED_REPLIES)
    for _ in range(3):
        t._tally(transport._SPEED_BACKUP)
    t._tally(transport._SPEED_BURST)
    hour = t.speed_view()["hour"]
    assert hour["limited"] == 3 and hour["limited_pct"] == 30.0


def test_no_replies_has_no_limited_percentage():
    t = _t()
    view = t.speed_view()
    assert view["hour"]["limited_pct"] is None and view["since_start"]["limited_pct"] is None
    assert view["hour"]["limited"] == 0


def test_since_start_carries_it_too():
    t = _t()
    for _ in range(4):
        t._tally(transport._SPEED_REPLIES)
    t._tally(transport._SPEED_BACKUP)
    assert t.speed_view()["since_start"]["limited_pct"] == 25.0
