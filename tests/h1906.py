"""1.9.0.6 - shared helpers: a fake clock for the transport and the carousel (decisions/0037 C9)."""
from __future__ import annotations

import importlib
import os
import time as _real_time

import pytest


class FakeTime:
    """Stands in for the ``time`` module inside transport and carousel; ``sleep`` advances it."""

    def __init__(self):
        self.now = 1_000_000.0

    def time(self):
        return self.now

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += float(seconds)

    def __getattr__(self, name):
        return getattr(_real_time, name)


@pytest.fixture()
def clock(monkeypatch, tmp_path):
    base = importlib.import_module("tests.test_1_8_2_7_timeouts")
    transport, carousel_mod, settings = base.transport, base.carousel_mod, base.settings
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for name in list(os.environ):
        if name.startswith("KAME_"):
            monkeypatch.delenv(name, raising=False)
    settings.forget()
    fake = FakeTime()
    monkeypatch.setattr(transport, "time", fake)
    monkeypatch.setattr(carousel_mod, "time", fake)
    monkeypatch.setattr(transport, "_publish", lambda *a, **k: None)      # the panel file is not what is measured
    monkeypatch.setattr(transport._Spinner, "update", classmethod(lambda cls, *a, **k: None))
    t = transport.KameTransport(engine=carousel_mod.Carousel())
    t._sleep = lambda s: fake.sleep(s)
    yield t, fake
    settings.forget()
