"""Real snapshots for looking at the rotation page (1.8.2.9, decisions/0031 2.10).

Drives the REAL ``state.snapshot()`` with a scripted carousel and transport (no Hermes import, a
throwaway ``HERMES_HOME``), and writes one JSON per situation into
``tools/panel_preview/fixtures/``. ``tools/panel_preview.mjs --fixture <file>`` draws the page from
one. Used by the contract test too: every path the page reads must exist in these snapshots.

Run:  uv run --with pyyaml python tools/panel_fixtures.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "hermes-kame-api-rotation"
OUT = ROOT / "tools" / "panel_preview" / "fixtures"

MODELS = ["gemini:gemini-3.8-flash", "gemini:gemini-3.7-flash", "gemini:gemini-3.6-flash", "gemini:gemini-3.5-flash"]
CHAIN = ("model:\n  provider: gemini\n  default: gemini-3.8-flash\nfallback_providers:\n"
         + "".join(f"  - provider: gemini\n    model: {m.split(':')[1]}\n" for m in MODELS[1:]))


def load(home: Path):
    os.environ["HERMES_HOME"] = str(home)
    for name in list(os.environ):
        if name.startswith("KAME_"):
            del os.environ[name]
    os.environ["KAME_RECORDER_DISABLED"] = "1"
    name = "kame_panel_fixtures"
    spec = importlib.util.spec_from_file_location(name, PLUGIN / "__init__.py", submodule_search_locations=[str(PLUGIN)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    mods = SimpleNamespace(
        state=importlib.import_module(f"{name}.state"),
        carousel=importlib.import_module(f"{name}.core.carousel"),
        events=importlib.import_module(f"{name}.core.events"),
        facade=importlib.import_module(f"{name}.facade"),
        chain=importlib.import_module(f"{name}.core.fallback_chain"),
        settings=importlib.import_module(f"{name}.settings"),
    )
    return mods


def keys(n: int, tag: str = "A") -> list:
    return [f"AIzaSy{tag}{i:03d}" + "0" * 26 for i in range(n)]


def build(name: str, home: Path, m) -> dict:
    """One situation -> one real snapshot document."""
    engine, transport = m.carousel.ENGINE, m.facade.TRANSPORT
    engine.forget()
    m.events.INFLIGHT.clear()
    m.events.EVENTS.clear()
    transport._busy_until.clear(); transport._busy_window.clear(); transport._busy_kind.clear()
    transport._probed_at.clear(); transport._speed.clear(); transport._speed_total[:] = [0] * 5
    transport.replies.clear()
    transport._requests.clear(); transport._request_tasks.clear(); transport._aux_stopped.clear(); transport._aux_memo.clear()
    long_list = CHAIN + "".join(f"  - provider: gemini\n    model: gemini-3.{n}-flash-extra\n" for n in (4, 3, 2))
    config = ("model:\n  provider: gemini\n  default: gemini-3.8-flash\n" if name == "no-fallback-list"
              else long_list if name == "long-list" else CHAIN)
    (home / "config.yaml").write_text(config, encoding="utf-8")
    m.settings.forget(); m.chain.forget()
    pool_keys = keys(14)
    activity = None

    if name != "first-run":
        for model in MODELS:
            engine.select(model, pool_keys)
        for model in MODELS:
            transport._tally(0)

    now = time.time()
    if name == "healthy-idle":
        for _ in range(46):
            transport._tally(0)
        for _ in range(3):
            transport._tally(1)
        transport.replies.append({"at": now - 20, "model": "gemini-3.8-flash", "for": "", "first_s": 1.9, "total_s": 2.4})
    elif name == "answering":
        call = SimpleNamespace(channel="main", task="", identity=MODELS[0])
        m.events.INFLIGHT.start(call)
        m.events.INFLIGHT.note(call, "sent", "14 of 14 key(s) ready", identity=MODELS[0],
                               fingerprint=m.carousel.fingerprint(pool_keys[3]))
        activity = {"kind": "calling", "identity": MODELS[0], "model": "gemini-3.8-flash", "attempt": 1,
                    "healthy": 14, "keys": 14, "started_at": now - 2.4, "key": m.carousel.fingerprint(pool_keys[3])}
    elif name == "all-busy-circle":
        for model in MODELS:
            transport._note_busy(model)
        transport._probed_at[MODELS[0]] = now - 6
        for key in pool_keys[:3]:
            engine.mark(MODELS[1], key, False, 30.0, "per_minute")
        m.events.EVENTS.add("handover", identity=MODELS[0], reason="gemini-3.8-flash is busy - trying gemini-3.7-flash (your list, best first)")
        m.events.EVENTS.add("handover", identity=MODELS[1], reason="gemini-3.7-flash is busy - trying gemini-3.8-flash (your list, best first)")
        activity = {"kind": "calling", "identity": MODELS[3], "model": "gemini-3.5-flash", "attempt": 4,
                    "healthy": 14, "keys": 14, "started_at": now - 9.8, "key": m.carousel.fingerprint(pool_keys[7])}
    elif name == "racing":
        call = SimpleNamespace(channel="main", task="", identity=MODELS[0])
        m.events.INFLIGHT.start(call)
        activity = {"kind": "calling", "identity": MODELS[0], "model": "gemini-3.8-flash", "attempt": 1,
                    "healthy": 14, "keys": 14, "started_at": now - 21.0, "key": m.carousel.fingerprint(pool_keys[3]),
                    "racing": ["gemini-3.7-flash", "gemini-3.6-flash"]}
    elif name == "probing":
        call = SimpleNamespace(channel="main", task="", identity=MODELS[0])
        m.events.INFLIGHT.start(call)
        activity = {"kind": "calling", "identity": MODELS[1], "model": "gemini-3.7-flash", "attempt": 1,
                    "healthy": 14, "keys": 14, "started_at": now - 0.8, "key": m.carousel.fingerprint(pool_keys[3]),
                    "racing": [], "probe": "gemini-3.8-flash"}
    elif name == "side-jobs":
        for _ in range(77):
            transport._count_request(SimpleNamespace(channel="main", task="chat"), MODELS[0])
        for task, n in (("title_generation", 20), ("compression", 3)):
            for _ in range(n):
                transport._count_request(SimpleNamespace(channel="secondary", task=task), MODELS[0])
        transport._aux_stopped["title_generation"] = 4
    elif name == "long-list":
        for _ in range(5):
            transport._tally(0)
    elif name == "out-for-today":
        for key in pool_keys[:12]:
            engine.mark(MODELS[0], key, False, 3600.0 * 5, "daily")
        engine.mark(MODELS[0], pool_keys[12], False, 40.0, "per_minute")
        transport.replies.append({"at": now - 8, "model": "gemini-3.7-flash", "for": "gemini-3.8-flash", "first_s": 1.6, "total_s": 2.3})
        for _ in range(30):
            transport._tally(0)
        for _ in range(5):
            transport._tally(1)
    elif name == "keys-to-replace":
        engine.mark(MODELS[1], pool_keys[0], False, 3600.0, "auth")
        engine.mark(MODELS[1], pool_keys[1], False, 3600.0, "auth")
        engine._pools[MODELS[1]][pool_keys[1]]["retired"] = True
        engine.mark(MODELS[0], pool_keys[5], False, 25.0, "per_minute")
    elif name == "sixty-keys":
        engine.select("gemini:gemini-big", keys(60, "B"))
        for key in keys(60, "B")[:9]:
            engine.mark("gemini:gemini-big", key, False, 3600.0, "daily")
        for key in keys(60, "B")[9:14]:
            engine.mark("gemini:gemini-big", key, False, 40.0, "per_minute")
    elif name == "all-resting":
        for model in MODELS:
            for key in pool_keys:
                engine.mark(model, key, False, 70.0 + pool_keys.index(key) * 5, "per_minute")
        activity = {"kind": "waiting", "eta_s": 70.0, "healthy": 0, "keys": 14}

    document = m.state.snapshot(transport, activity)
    document["updated_at"] = time.time()
    return document


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    home = Path(tempfile.mkdtemp(prefix="kame-fixtures-"))
    m = load(home)
    names = ["healthy-idle", "answering", "all-busy-circle", "out-for-today", "keys-to-replace",
             "first-run", "sixty-keys", "no-fallback-list", "all-resting", "racing", "probing", "side-jobs", "long-list"]
    for name in names:
        document = build(name, home, m)
        (OUT / f"{name}.json").write_text(json.dumps(document, indent=1, sort_keys=True), encoding="utf-8")
        print(f"{name:18s} {len(json.dumps(document)):7d} bytes")


if __name__ == "__main__":
    main()
