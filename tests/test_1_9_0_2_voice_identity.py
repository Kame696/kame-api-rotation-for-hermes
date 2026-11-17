"""1.9.0.2 (H3) - voice and transcription meter the same bucket as the chat.

Live (audit/2026-11-15-pre-1902-bug-hunt.md, F4): ``listen.py`` and ``voice.py`` built
``identity = "gemini:" + model`` while every chat call is ``gemini:<model>:global`` (the region is part
of the identity since 1.8.2.3). Same keys, same Google per-project-per-model quota, two memories: on
2026-11-14 between 08:00 and 10:59 transcription took 43 refusals on keys the chat already knew were
spent, and ``lifetime.json`` keeps limits and counts under both names.

Pinned: both build the chat's identity for the same endpoint, so a key benched by one is skipped by
the other; the region follows the endpoint they actually call.
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_1_9_0_2_voice_identity_under_test"


def _load_package():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_load_package()
voice = importlib.import_module(f"{PACKAGE}.voice")
listen = importlib.import_module(f"{PACKAGE}.listen")
carousel_mod = importlib.import_module(f"{PACKAGE}.core.carousel")
region_mod = importlib.import_module(f"{PACKAGE}.core.region")

GOOGLE = "https://generativelanguage.googleapis.com/v1beta"


def chat_identity(model, base=GOOGLE):
    """What ``facade._FacadeCall`` builds for the chat (facade.py, 1.8.2.3)."""
    region, _ = region_mod.parse_region(base, model)
    return carousel_mod.Carousel.identity("gemini", model, region)


def test_transcription_uses_the_chat_identity():
    assert listen._identity("gemini-3.6-flash", GOOGLE) == chat_identity("gemini-3.6-flash")
    assert listen._identity("gemini-3.6-flash", GOOGLE).endswith(":global")


def test_speech_uses_the_chat_identity():
    assert voice._identity("gemini-3.6-flash", GOOGLE) == chat_identity("gemini-3.6-flash")


def test_a_key_benched_by_the_chat_is_resting_for_transcription_and_back():
    engine = carousel_mod.Carousel()
    model = "gemini-3.6-flash"
    engine.mark(chat_identity(model), "k1", False, 3600.0, "daily", stated=True)
    assert engine.next_recovery_seconds(listen._identity(model, GOOGLE), ["k1"]) > 3000
    engine.mark(listen._identity(model, GOOGLE), "k2", False, 3600.0, "daily", stated=True)
    assert engine.next_recovery_seconds(chat_identity(model), ["k2"]) > 3000


def test_the_region_follows_the_endpoint_each_one_calls():
    vertex = "https://us-central1-aiplatform.googleapis.com/v1/projects/p/locations/us-central1/publishers/google"
    assert voice._identity("gemini-3.6-flash", vertex) == chat_identity("gemini-3.6-flash", vertex)
