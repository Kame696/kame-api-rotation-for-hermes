"""1.8.2.7: the panel must not go blank because Windows held ``state.json`` for a moment.

The owner reported (1.8.2.6) that the KAME menu sometimes stopped rendering. His
desktop log has two causes: the ``StringSetting is not defined`` crash (11-15/10,
fixed in 1.8.2.7, pinned by ``tests/ui_reconcile.mjs``), and the panel's backend
answering ``503 snapshot unreadable`` - 427 times on 14/10 - whenever the file was
being replaced by a Hermes process at the moment the panel read it. These pin the
fix: a short retry, then the last good copy; and a writer that waits out a reader.
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_panel_reads_1827_under_test"


def _load_plugin_api():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    if "hermes_constants" not in sys.modules:
        try:
            importlib.import_module("hermes_constants")
        except ImportError:  # outside a Hermes checkout: the one name the door imports
            import types
            stub = types.ModuleType("hermes_constants")
            stub.get_hermes_home = lambda: Path(os.environ.get("HERMES_HOME", "."))
            sys.modules["hermes_constants"] = stub
    spec = importlib.util.spec_from_file_location("kame_plugin_api_1827_under_test",
                                                  PLUGIN_DIR / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_package():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def door(tmp_path, monkeypatch):
    api = _load_plugin_api()
    monkeypatch.setattr(api, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(api, "_READ_PAUSE_S", 0.0)
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(api.router)
    folder = tmp_path / "plugin-data" / api.PLUGIN_ID
    folder.mkdir(parents=True)
    (folder / "state.json").write_text('{"schema": 6, "processes": {}}', encoding="utf-8")
    return api, TestClient(app), folder


def _flaky_read(monkeypatch, fail_times):
    real = Path.read_text
    calls = {"n": 0}

    def read_text(self, *a, **k):
        if self.name == "state.json" and calls["n"] < fail_times:
            calls["n"] += 1
            raise PermissionError(13, "The process cannot access the file")
        return real(self, *a, **k)
    monkeypatch.setattr(Path, "read_text", read_text)
    return calls


def test_a_file_held_for_a_moment_is_read_on_a_retry(door, monkeypatch):
    _api, http, _folder = door
    _flaky_read(monkeypatch, 3)
    response = http.get("/state")
    assert response.status_code == 200 and response.json()["schema"] == 6


def test_a_file_held_for_longer_serves_the_last_good_copy(door, monkeypatch):
    _api, http, _folder = door
    assert http.get("/state").status_code == 200  # a good read first
    _flaky_read(monkeypatch, 100)
    response = http.get("/state")
    assert response.status_code == 200 and response.headers.get("X-Kame-Stale") == "1"


def test_with_nothing_good_to_serve_it_still_says_so(door, monkeypatch):
    api, http, _folder = door
    api._LAST_GOOD.clear()
    _flaky_read(monkeypatch, 100)
    assert http.get("/state").status_code == 503


def test_the_writer_waits_out_a_reader_instead_of_skipping_the_snapshot(tmp_path, monkeypatch):
    _load_package()
    state = importlib.import_module(f"{PACKAGE}.state")
    target = tmp_path / "state.json"
    source = tmp_path / "x.tmp"
    source.write_text("{}", encoding="utf-8")
    real = os.replace
    calls = {"n": 0}

    def replace(a, b):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(13, "in use")
        return real(a, b)
    monkeypatch.setattr(state.os, "replace", replace)
    state._replace_patiently(str(source), target, pause_s=0.0)
    assert target.read_text(encoding="utf-8") == "{}" and calls["n"] == 3
