"""The pool wrappers must pass through arguments the host adds.

Hermes 0.21.3 (upstream main, 2026-09-20) gave ``CredentialPool`` per-model
cooldowns: ``_select_unlocked``, ``_available_entries`` and friends gained a
``model=`` keyword, and ``_select_under_lock`` now calls
``self._select_unlocked(model=model)``. KAME's replacements declared only the
keywords of the version they were written against, so on 0.21.3 **every**
``pool.select()`` raised ``TypeError`` - measured with ``tools/host_pool_suite.py``
against a clone of upstream main: 58 of the host's own pool behaviours changed.

The same shape of bug was already latent on 0.21.1: ``try_refresh_matching``
calls ``_select_unlocked(refresh=False, count=False)``, and ``count`` was never
accepted either.

So: forward whatever the host passes, and honour ``count=False`` - a selection
that is only a look (the host's own name for it) is not a hand-out, and must
not be counted as load on the key.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_binding as tb  # noqa: E402  - the shared stand-in host


class NewerPool(tb.FakePool):
    """``CredentialPool`` as of Hermes 0.21.3: ``model=`` and ``count=`` everywhere."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.seen_models: List[Optional[str]] = []
        self.seen_counts: List[bool] = []

    def _available_entries(self, *, clear_expired: bool = False, refresh: bool = False,
                           model: Optional[str] = None):
        self.seen_models.append(model)
        return super()._available_entries(clear_expired=clear_expired, refresh=refresh)

    def _select_unlocked(self, *, refresh: bool = True, count: bool = True,
                         model: Optional[str] = None):
        self.seen_counts.append(count)
        available, pending = self._available_entries(clear_expired=True, refresh=refresh, model=model)
        if not available:
            return None, pending
        return available[0], pending

    def _mark_exhausted(self, entry, status_code=None, error_context=None, *,
                        persist: bool = True, failure_reason: Optional[str] = None,
                        model: Optional[str] = None):
        self.seen_models.append(model)
        return super()._mark_exhausted(entry, status_code, error_context,
                                       persist=persist, failure_reason=failure_reason)

    def current(self, *, model: Optional[str] = None):
        self.seen_models.append(model)
        return self._entries[0] if self._entries else None

    # The host's own entry points, spelled as 0.21.3 spells them.
    def select(self, *, model: Optional[str] = None):
        with self._lock:
            return self._select_unlocked(model=model)[0]

    def peek(self):
        with self._lock:
            return self._select_unlocked(refresh=False, count=False)[0]


@pytest.fixture
def newer():
    module = tb._fresh_module()
    module.CredentialPool = type("Pool", (NewerPool,), {})
    binding = tb.PoolBinding(tb.LedgerStore(tb.FakeState(), ttl_seconds=0.0), clock=lambda: tb.NOW)
    assert binding.install(module) is True
    pool = module.CredentialPool("gemini", tb.three_healthy_keys())
    notes = []
    real_note = binding._dispersion.note
    binding._dispersion.note = lambda *a, **k: (notes.append(a), real_note(*a, **k))[1]
    yield binding, pool, notes
    binding.uninstall()


def test_selection_with_a_model_reaches_the_host_instead_of_raising(newer):
    _binding, pool, _notes = newer
    entry = pool.select(model="gemini-3.8-flash")
    assert entry is not None
    assert "gemini-3.8-flash" in pool.seen_models


def test_a_look_at_the_pool_is_not_counted_as_a_hand_out(newer):
    _binding, pool, notes = newer
    assert pool.peek() is not None
    assert pool.seen_counts == [False]
    assert notes == [], "count=False handed nothing out, so no key may be charged for it"
    pool.select(model="m")
    assert len(notes) == 1, "a real selection is still counted"


def test_availability_forwards_the_model(newer):
    _binding, pool, _notes = newer
    available, _pending = pool._available_entries(clear_expired=True, refresh=False, model="m2")
    assert available
    assert pool.seen_models[-1] == "m2"


def test_a_refusal_forwards_what_the_host_added(newer):
    _binding, pool, _notes = newer
    entry = pool.entries()[0]
    pool._mark_exhausted(entry, 429, {"reason": "rate_limit"}, model="m3")
    assert "m3" in pool.seen_models


def test_current_forwards_what_the_host_added(newer):
    _binding, pool, _notes = newer
    assert pool.current(model="m4") is not None
    assert "m4" in pool.seen_models
