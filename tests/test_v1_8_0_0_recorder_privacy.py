"""What the header capture must never write down.

1.8.0.0 started recording response headers, because decision 0004's D1 — obey
a stated 5xx deadline under the owner's ceiling — had **no evidence either
way**: not one of the 1,387 real refusals on his machine carried a
``Retry-After``, since no version before this one ever wrote a header down.

The capture is allowlist-first and drops the names a credential is called by.
The adversarial review (`research/1.8.0.0/RED_TEAM.md`, F12) then did the thing
reading the code cannot: it fed the function headers nobody had thought of, and
two shapes walked straight through — a bare 32-character hex string under an
innocent name, and an account address. Both are now dropped on the value's own
shape, and these tests are what stops them coming back.

Invariant I7 is the reason this file exists at all: no key material anywhere,
not in the snapshot, not in the events, not in the logs, not in an error path.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
PACKAGE = "kame_recorder_privacy_under_test"


def _load_package():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE,
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_load_package()
recorder = importlib.import_module(f"{PACKAGE}.recorder")


USEFUL = {
    "retry-after": "30",
    "retry-after-ms": "1500",
    "x-ratelimit-reset-requests": "12s",
    "x-ratelimit-remaining-tokens": "0",
    "date": "Tue, 19 Sep 2026 03:00:00 GMT",
    "x-request-id": "req_7",
}


class TestTheValuesThatMustNotSurvive:
    @pytest.mark.parametrize(
        "name, value",
        [
            # The two the review measured, under names the allowlist admits.
            ("x-usage-id", "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"),
            ("x-ratelimit-owner", "9f86d081884c7d659a2feaa0c55ad015"),
            ("x-quota-account", "user@example.com"),
            # And the shapes they generalise to.
            ("x-quota-owner", "KAME@example.co.uk"),
            ("x-usage-token", "DEADBEEFDEADBEEFDEADBEEFDEADBEEF"),
            ("x-ratelimit-key", "sk-abcdefghijklmnopqrstuvwxyz012345"),
        ],
    )
    def test_a_header_shaped_like_a_secret_or_a_person_is_dropped(self, name, value):
        kept = recorder._safe_headers({name: value, **USEFUL})
        assert name not in kept, f"{name} reached disk carrying {value[:6]}…"

    def test_the_numbers_the_capture_exists_for_are_kept(self):
        kept = recorder._safe_headers(dict(USEFUL))
        assert kept == USEFUL, "the retry/quota headers are the whole point"

    def test_an_address_anywhere_in_the_value_is_enough(self):
        kept = recorder._safe_headers({"x-quota-note": "owner user@example.com over"})
        assert kept == {}

    def test_a_short_hex_id_is_not_mistaken_for_a_secret(self):
        # Eight hex characters is a request id, not a key: dropping it would
        # cost provenance for nothing. The bound is deliberate, not incidental.
        kept = recorder._safe_headers({"x-request-id": "a1b2c3d4"})
        assert kept == {"x-request-id": "a1b2c3d4"}
