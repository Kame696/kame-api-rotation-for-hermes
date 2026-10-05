"""1.8.1.9 — the 1.8.x source invariants, read where the code now lives.

Several 1.8.1.8 tests prove a rule by reading ``dispatch_binding.py`` or
``pool_binding.py``. In 1.8.1.9 the carousel those files held moved, decision
for decision, into ``transport.py`` (and the journal writer into
``journal_keeper.py``). The rules did not move with a rename on their own:
each scan below is the 1.8.1.8 one, pointed at the file that now holds the code.
The originals stay in their files, marked retired in ``legacy_1818_retired.py``
with a pointer here.
"""

from __future__ import annotations

import importlib
import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_source_invariants_1819"


def _load():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_load()
vocabulary = importlib.import_module(f"{PACKAGE}.core.vocabulary")
TRANSPORT = (PLUGIN_DIR / "transport.py").read_text(encoding="utf-8")
JOURNAL = (PLUGIN_DIR / "journal_keeper.py").read_text(encoding="utf-8")


def test_i2_no_deleted_watchdog_construct_has_come_back():
    # test_v1_8_0_0_invariants::TestI2TrustTheConnection
    for construct in ("_StreamWatchdog", "CHUNK_STALE_TIMEOUT", "ZombieGuard", "Zombie Guard"):
        assert construct not in TRANSPORT, construct


def test_i7_every_events_add_call_site_fingerprints_the_key_first():
    # test_v1_8_0_0_invariants::TestI7NoKeyMaterialAnywhere
    calls = re.findall(r"EVENTS\.add\(.*?\n(?:.*?\n)*?\s*\)", TRANSPORT)
    assert calls, "no EVENTS.add( call sites found to check"
    key_lines = [line for block in calls for line in block.splitlines() if "key=" in line]
    assert key_lines, "no key= arguments found to check"
    for line in key_lines:
        assert "fingerprint(" in line or 'key=""' in line, line


def test_i8_the_journal_is_fed_only_short_canonical_reasons():
    # test_v1_8_0_0_invariants::TestI8 (pool_binding._record_block -> journal_keeper.note_rotation)
    marker = JOURNAL.index("def note_rotation(")
    body = JOURNAL[marker: marker + 4000]
    assert "reason=reason," in body
    assert "raw_message" not in body
    assert "str(exc)" not in body
    # ...and the one caller threads the classification's own word through.
    call = TRANSPORT[TRANSPORT.index("runtime.record_rotation("):]
    call = call[: call.index("judgement=")]
    assert "reason=kind," in call and "raw_message" not in call and "str(exc)" not in call


def test_the_kinds_that_were_never_written_now_are():
    # test_v1_6_0_1::TestEveryRotationIsOnTheScreen
    for kind in ('"switch"', '"recovery"', '"wait"'):
        assert re.search(r"EVENTS\.add\(\s*\n\s*" + re.escape(kind), TRANSPORT), kind


def test_the_screen_can_tell_resting_from_out_for_good():
    # test_v1_6_0_1::TestARefusedKeyStopsBeingOffered
    assert "out of rotation until it is replaced" in TRANSPORT
    assert "is_retired" in TRANSPORT


def test_the_vocabulary_covers_what_the_transport_actually_writes():
    # test_v1_7_0_5::test_the_vocabulary_covers_what_dispatch_actually_writes
    written = set(re.findall(r'sized_by = "(\w+)"', TRANSPORT))
    written |= set(re.findall(r'"(\w+)" if window_number_declined', TRANSPORT))
    assert written, "no sized_by literal found to check"
    unknown = sorted(written - vocabulary.SIZED_BY_SOURCES)
    assert not unknown, f"transport writes these and nothing knows them: {unknown}"


def test_the_transport_hands_the_rung_to_the_timings_line():
    # test_v1_8_1_0_backoff::TestTheRungIsVisible::test_dispatch_hands_the_rung_to_the_timings_line
    assert "rest_source=self.engine.unsized_backoff_label(identity, key)" in TRANSPORT


def test_the_status_line_says_nothing_about_backoff():
    # test_v1_8_1_0_backoff::TestTheRungIsVisible::test_the_status_surface_says_whether_it_is_on (spinner half)
    spinner = re.search(r"def status_line\(.*?\n(?=\ndef |\nclass )", TRANSPORT, re.S)
    assert spinner and "backoff" not in spinner.group(0)


def test_no_host_function_is_wrapped_or_rebound():
    # The rule 1.8.1.9 exists for (plugin catalog rule 9), over every module.
    for path in PLUGIN_DIR.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for pattern in (r"setattr\(\s*(agent|module|host|run_agent|chat_completion_helpers)\b",
                        r"\bPatches\(\)", r"_originals\[", r"__kame_carousel__"):
            assert not re.search(pattern, text), f"{path.name}: {pattern}"
