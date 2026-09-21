"""1.8.1.0 (owner report, 2026-09-21): *Clear pool* did not reset the pool.

The button called ``ENGINE.forget()`` and nothing else. With
``share_pool_health`` on — the default since 1.8.0.0 — the shared file still
held every bench, and being fresher than the emptied memory it won the very
next ``select``: the pool came back exactly as benched as before the click.
The per-model ledger on disk and the host's own "exhausted" mark survived the
click the same way.

These tests drive the real ``Carousel`` against a real (temp-file)
``SharedHealth`` and fail on the build before the fix.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_v1_8_1_0_clear_pool_under_test"


def _load_package():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE, PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_load_package()
carousel_mod = importlib.import_module(f"{PACKAGE}.core.carousel")
shared_health = importlib.import_module(f"{PACKAGE}.core.shared_health")
control = importlib.import_module(f"{PACKAGE}.control")

IDENTITY = "gemini:gemini-3.7-flash"
KEYS = ["AIzaSyTESTKEY-one-000000000000000", "AIzaSyTESTKEY-two-000000000000000"]


def _engine(path: Path, profile: str = "base"):
    store = shared_health.SharedHealth(path=path, profile=profile, enabled_fn=lambda: True)
    return carousel_mod.Carousel(shared_health_store=store), store


def _benched(engine, key) -> bool:
    return engine.select(IDENTITY, KEYS) != key or engine.healthy_count(IDENTITY, KEYS) < len(KEYS)


def test_forget_alone_is_undone_by_the_shared_file(tmp_path):
    # The defect, stated as a fact about forget(): memory is gone, the file
    # is not, and the file wins.
    engine, _ = _engine(tmp_path / "pool-health.json")
    for key in KEYS:
        engine.mark(IDENTITY, key, False, 600.0, "per_minute")
    assert engine.healthy_count(IDENTITY, KEYS) == 0
    engine.forget()
    assert engine.healthy_count(IDENTITY, KEYS) == 0


def test_reset_all_really_frees_every_key(tmp_path):
    engine, _ = _engine(tmp_path / "pool-health.json")
    for key in KEYS:
        engine.mark(IDENTITY, key, False, 600.0, "per_minute")
    assert engine.healthy_count(IDENTITY, KEYS) == 0
    released = engine.reset_all()
    assert released == len(KEYS)
    assert engine.healthy_count(IDENTITY, KEYS) == len(KEYS)
    assert engine.next_recovery_seconds(IDENTITY, KEYS) in (None, 0, 0.0)


def test_the_reset_reaches_another_profile_sharing_the_file(tmp_path):
    # One button in one profile's panel; the other profile had benched the
    # same keys on its own. A release is fresher than its local bench.
    path = tmp_path / "pool-health.json"
    base, _ = _engine(path, "base")
    other, _ = _engine(path, "k")
    for key in KEYS:
        other.mark(IDENTITY, key, False, 600.0, "per_minute")
    assert base.healthy_count(IDENTITY, KEYS) == 0
    base.reset_all()
    assert base.healthy_count(IDENTITY, KEYS) == len(KEYS)
    assert other.healthy_count(IDENTITY, KEYS) == len(KEYS)


def test_account_holds_are_released_too(tmp_path):
    engine, _ = _engine(tmp_path / "pool-health.json")
    engine.mark(IDENTITY, KEYS[0], False, 3600.0, "insufficient_quota")
    assert engine.healthy_count(IDENTITY, KEYS) == 1
    engine.reset_all()
    assert engine.healthy_count(IDENTITY, KEYS) == len(KEYS)


def test_a_switched_off_file_is_left_alone(tmp_path):
    path = tmp_path / "pool-health.json"
    store = shared_health.SharedHealth(path=path, profile="base", enabled_fn=lambda: False)
    engine = carousel_mod.Carousel(shared_health_store=store)
    engine.mark(IDENTITY, KEYS[0], False, 600.0, "per_minute")
    assert engine.reset_all() is None
    assert not path.exists()
    assert engine.healthy_count(IDENTITY, KEYS) == len(KEYS)


def test_the_button_clears_the_ledger_and_the_receipts(monkeypatch):
    cleared = []

    class _Store:
        def __init__(self, name):
            self.name = name

        def clear(self):
            cleared.append(self.name)
            return True

    class _Binding:
        _store = _Store("ledger")
        _journal = _Store("receipts")

    package = sys.modules[PACKAGE]
    monkeypatch.setattr(package, "_binding", _Binding(), raising=False)
    ok, detail = control._apply("clear_pool", "", None)
    assert ok is True
    assert cleared == ["ledger", "receipts"]
    assert "ledger" in detail and "receipts" in detail


def test_the_button_calls_the_engine_reset(monkeypatch):
    called = []
    monkeypatch.setattr(carousel_mod.ENGINE, "reset_all", lambda: called.append(1) or 0)
    ok, _ = control._apply("clear_pool", "", None)
    assert ok is True and called == [1]


# --- the build fingerprint does not depend on line endings ------------------
# Found while republishing this fix: the same release fingerprinted three ways
# (installed copy, Windows checkout, repository bytes) because a Windows
# checkout writes CRLF where the repository holds LF.

integrity = importlib.import_module(f"{PACKAGE}.integrity")


def test_the_fingerprint_ignores_line_endings(tmp_path):
    lf = tmp_path / "lf"
    crlf = tmp_path / "crlf"
    for root, newline in ((lf, "\n"), (crlf, "\r\n")):
        root.mkdir()
        (root / "a.py").write_bytes(("x = 1" + newline + "y = 2" + newline).encode())
    assert integrity.fingerprint(str(lf)) == integrity.fingerprint(str(crlf))


def test_the_fingerprint_still_sees_a_real_edit(tmp_path):
    (tmp_path / "a.py").write_bytes(b"x = 1\n")
    before = integrity.fingerprint(str(tmp_path))
    (tmp_path / "a.py").write_bytes(b"x = 2\n")
    assert integrity.fingerprint(str(tmp_path)) != before
