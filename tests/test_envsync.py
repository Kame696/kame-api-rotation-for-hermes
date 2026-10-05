"""envsync: a multi-key variable becomes, and stays, one pool row per key.

Against Hermes' own credential pool and auth.json, in the suite's redirected
HERMES_HOME (conftest) — never a real home.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("agent.credential_pool")

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_envsync_under_test"


def _load():
    if PACKAGE not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            PACKAGE, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return importlib.import_module(f"{PACKAGE}.envsync"), importlib.import_module(f"{PACKAGE}.settings")


envsync, settings = _load()

K = [f"AIzaSyFAKEENVSYNC{i:02d}" + "x" * 20 for i in range(1, 6)]
VAR = "GEMINI_API_KEY"


@pytest.fixture(autouse=True)
def _clean_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for var in ("GOOGLE_API_KEY", "GEMINI_API_KEY", "KAME_RESOLVER_DISABLED", "KAME_ROTATION_DISABLED"):
        monkeypatch.delenv(var, raising=False)
    settings.forget()
    yield
    settings.forget()


def _rows():
    from agent.credential_pool import load_pool

    return [(e.source, e.access_token) for e in load_pool("gemini").entries()]


def _ours():
    return sorted(t for s, t in _rows() if s == envsync.SOURCE_PREFIX + VAR)


def _suppressed():
    from hermes_cli.auth import is_source_suppressed

    return is_source_suppressed("gemini", f"env:{VAR}")


def _backups(home):
    return sorted(Path(home).glob("auth.json.kame-*.bak"))


def test_a_list_becomes_one_row_per_key_and_hermes_no_longer_sees_the_list(monkeypatch):
    monkeypatch.setenv(VAR, ",".join(K[:3]))
    report = envsync.sync()
    assert report and "3 keys" in report[0]
    assert _ours() == sorted(K[:3])
    assert _suppressed()
    # No row holds the whole list, and Hermes' resolver picks one key from the pool.
    assert all("," not in token for _source, token in _rows())
    from hermes_cli.runtime_provider import resolve_runtime_provider

    runtime = resolve_runtime_provider(requested="gemini")
    assert runtime["api_key"] in K[:3]


def test_a_second_start_changes_nothing_and_writes_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv(VAR, ",".join(K[:3]))
    envsync.sync()
    before = (tmp_path / "auth.json").read_bytes()
    backups = _backups(tmp_path)
    assert envsync.sync() == []
    assert (tmp_path / "auth.json").read_bytes() == before
    assert _backups(tmp_path) == backups


def test_the_pool_follows_the_variable_and_keeps_rows_the_user_added(monkeypatch):
    monkeypatch.setenv(VAR, ",".join(K[:3]))
    envsync.sync()
    from agent.credential_pool import load_pool

    commands = importlib.import_module(f"{PACKAGE}.commands")
    load_pool("gemini").add_entry(commands._make_entry("gemini", K[4], "by hand"))

    monkeypatch.setenv(VAR, ",".join([K[0], K[2], K[3]]))
    report = envsync.sync()
    assert report and "1 row(s) added, 1 removed" in report[0]
    assert _ours() == sorted([K[0], K[2], K[3]])
    assert ("manual", K[4]) in _rows()


def test_back_to_one_key_gives_the_variable_back_to_hermes(monkeypatch):
    monkeypatch.setenv(VAR, ",".join(K[:3]))
    envsync.sync()
    monkeypatch.setenv(VAR, K[0])
    envsync.sync()
    assert _ours() == []
    assert not _suppressed()
    assert (f"env:{VAR}", K[0]) in _rows()


def test_switched_off_it_touches_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv(VAR, ",".join(K[:3]))
    monkeypatch.setenv("KAME_RESOLVER_DISABLED", "1")
    settings.forget()
    assert envsync.sync() == []
    assert not (tmp_path / "auth.json").exists() or _ours() == []


def test_auth_json_is_backed_up_before_the_first_write(monkeypatch, tmp_path):
    from agent.credential_pool import load_pool

    commands = importlib.import_module(f"{PACKAGE}.commands")
    load_pool("gemini").add_entry(commands._make_entry("gemini", K[4], "by hand"))
    monkeypatch.setenv(VAR, ",".join(K[:2]))
    envsync.sync()
    assert len(_backups(tmp_path)) == 1


def test_an_alias_of_the_provider_gets_no_pool_of_its_own(monkeypatch):
    # Provider plugins mirror aliases ("google", "google-ai-studio") into the
    # registry with the canonical provider's config; the pool is the canonical's.
    from hermes_cli import auth

    monkeypatch.setitem(auth.PROVIDER_REGISTRY, "google", auth.PROVIDER_REGISTRY["gemini"])
    monkeypatch.setitem(auth.PROVIDER_REGISTRY, "google-ai-studio", auth.PROVIDER_REGISTRY["gemini"])
    monkeypatch.setenv(VAR, ",".join(K[:2]))
    report = envsync.sync()
    assert [line.split(":", 1)[0] for line in report] == ["gemini"]
    assert sorted(auth.read_credential_pool().keys()) == ["gemini"]
