"""What gets written down while a provider is refusing everything.

1.0.1 took the ceiling off, which is the right behaviour and a worse log: a
call used to give up after ten minutes, so an outage wrote lines for ten
minutes whether or not anybody wanted them. Now it rotates for as long as the
provider keeps refusing, and Agent Zero has already measured what that costs —
1,063 near-identical failure lines in 83 minutes of one Gemini outage.

So 1.0.2 collapses the repeats. These tests pin the two halves of that: what is
allowed to disappear, and what must never disappear with it.
"""

from __future__ import annotations

import logging

import pytest

from .test_waiting import _Clock as _DrivenClock
from .test_dispatch import (
    KEYS,
    Agent,
    Answer,
    Boom,
    Carousel,
    DispatchBinding,
    _binding,
    carousel,
    dispatch_binding,
)

import importlib

storm_mod = importlib.import_module(
    f"{dispatch_binding.__name__.rsplit('.', 1)[0]}.core.storm"
)
AGGREGATE_EVERY_S = storm_mod.AGGREGATE_EVERY_S
LOUD_N = storm_mod.LOUD_N
StormFilter = storm_mod.StormFilter


class _Clock:
    """A clock the test drives, so a long outage costs no wall time."""

    def __init__(self) -> None:
        self.now = 0.0

    def tick(self, seconds: float) -> float:
        self.now += seconds
        return self.now


class TestTheFilterItself:
    def test_the_first_failures_are_printed_in_full(self):
        # One line says what is failing; three let somebody watch the pool walk
        # from key to key, which is the thing they installed this to see.
        storm = StormFilter()
        clock = _Clock()
        for i in range(LOUD_N):
            verdict = storm.observe("server", 503, f"k{i}", clock.tick(1.0))
            assert verdict.speak_full, f"failure {i + 1} of {LOUD_N} went quiet"
            assert verdict.summary is None

    def test_the_repeats_after_that_go_quiet(self):
        storm = StormFilter()
        clock = _Clock()
        for _ in range(LOUD_N):
            storm.observe("server", 503, "aaa", clock.tick(1.0))

        for _ in range(50):
            verdict = storm.observe("server", 503, "aaa", clock.tick(0.1))
            assert not verdict.speak_full
            assert verdict.summary is None

    def test_a_long_storm_still_says_something_periodically(self):
        # Silence and a hang look the same. The collapse is only safe because
        # it keeps reporting a count.
        storm = StormFilter()
        clock = _Clock()
        for _ in range(LOUD_N):
            storm.observe("server", 503, "aaa", clock.tick(1.0))

        summaries = []
        for _ in range(400):
            verdict = storm.observe("server", 503, "aaa", clock.tick(1.0))
            if verdict.summary:
                summaries.append(verdict.summary)

        assert summaries, "a storm that says nothing at all is a hang"
        # 400 seconds at one failure a second, aggregated every 20.
        assert 15 <= len(summaries) <= 25, len(summaries)
        assert "still rotating" in summaries[0]

    def test_the_aggregate_accounts_for_every_failure_it_held_back(self):
        # A reader must be able to add the numbers up. A collapse that loses
        # count is worse than no collapse, because it looks authoritative.
        storm = StormFilter()
        clock = _Clock()
        printed = 0
        counted = 0
        for _ in range(200):
            verdict = storm.observe("server", 503, "aaa", clock.tick(1.0))
            if verdict.speak_full:
                printed += 1
            if verdict.summary:
                counted += int(verdict.summary.split("×")[1].split()[0])
        recap = storm.ended(clock.tick(1.0))
        assert recap is not None
        total = int(recap.split("— ")[1].split()[0])
        assert total == 200
        # Everything is either printed, counted in an aggregate, or still
        # pending in the last unreported window — and the recap covers it.
        assert printed == LOUD_N
        assert counted <= 200 - printed

    def test_a_change_of_shape_is_never_collapsed(self):
        # The provider changed its answer. That is news, whatever came before.
        storm = StormFilter()
        clock = _Clock()
        for _ in range(50):
            storm.observe("server", 503, "aaa", clock.tick(1.0))

        verdict = storm.observe("per_minute", 429, "aaa", clock.tick(1.0))
        assert verdict.speak_full, "a new kind of failure must be visible"
        assert verdict.summary, "and the old storm has to be closed, not dropped"
        assert "storm over" in verdict.summary

    def test_a_storm_that_never_collapsed_gets_no_recap(self):
        # Three lines the reader can still see do not need summarising.
        storm = StormFilter()
        clock = _Clock()
        for _ in range(LOUD_N):
            storm.observe("server", 503, "aaa", clock.tick(1.0))
        assert storm.ended(clock.tick(1.0)) is None

    def test_the_recap_counts_keys_without_naming_them(self):
        storm = StormFilter()
        clock = _Clock()
        for i in range(60):
            storm.observe("server", 503, f"key{i % 4}", clock.tick(1.0))
        recap = storm.ended(clock.tick(1.0))
        assert "4 key(s)" in recap
        assert "key0" not in recap

    def test_it_reports_whether_it_is_holding_anything_back(self):
        storm = StormFilter()
        clock = _Clock()
        assert not storm.storming
        for _ in range(LOUD_N):
            storm.observe("server", 503, "aaa", clock.tick(1.0))
        assert not storm.storming, "nothing has been withheld yet"
        storm.observe("server", 503, "aaa", clock.tick(1.0))
        assert storm.storming


class TestTheCarouselUsesIt:
    """Against the real binding, with a driven clock.

    The clock has to govern ``time.time`` as well as ``time.monotonic``: the
    engine stores cooldown deadlines against the first and the binding measures
    elapsed time with the second, so patching only one leaves the carousel
    waiting on keys that never recover — which, since 1.0.1, it will do
    forever. ``test_waiting`` learned that the hard way and owns the clock.
    """

    def _driven(self, monkeypatch):
        clock = _DrivenClock().install(monkeypatch)
        binding = DispatchBinding(engine=Carousel(), sleep=clock.sleep)
        return clock, binding

    def _noisy_host(self, clock, fail_times: int, *, per_attempt: float = 0.5):
        attempts = []

        def host(agent, api_kwargs, **kwargs):
            attempts.append(1)
            # A real refusal takes time to arrive. Without this the whole storm
            # happens at one instant and no aggregate window ever elapses.
            clock.now += per_attempt
            if len(attempts) <= fail_times:
                raise Boom("503 Service Unavailable", status_code=503)
            return Answer("finally")

        return host, attempts

    def test_a_long_outage_does_not_write_a_line_per_rotation(self, caplog, monkeypatch):
        clock, binding = self._driven(monkeypatch)
        host, attempts = self._noisy_host(clock, 300)

        with caplog.at_level(logging.WARNING):
            binding.run(host, Agent(), {}, (), {})

        assert len(attempts) == 301
        lines = [r for r in caplog.records if "resting" in r.getMessage()]
        assert len(lines) == LOUD_N, (
            f"{len(lines)} full failure lines for 300 rotations"
        )
        assert binding.suppressed > 250
        # And it did not go silent either.
        assert any("still rotating" in r.getMessage() for r in caplog.records)

    def test_the_recap_lands_when_a_key_finally_answers(self, caplog, monkeypatch):
        clock, binding = self._driven(monkeypatch)
        host, _ = self._noisy_host(clock, 40)

        with caplog.at_level(logging.WARNING):
            result = binding.run(host, Agent(), {}, (), {})

        assert result.choices[0].message.content == "finally"
        recaps = [r for r in caplog.records if "storm over" in r.getMessage()]
        assert recaps, "a collapsed storm has to say how big it got"
        assert "40 failure(s)" in recaps[-1].getMessage()

    def test_an_auth_failure_is_never_collapsed(self, caplog, monkeypatch):
        # Permanent, actionable, and no amount of rotation repairs it. It is
        # logged on its own path, and every one has to be named however much
        # noise is around it.
        clock, binding = self._driven(monkeypatch)
        attempts = []

        def host(agent, api_kwargs, **kwargs):
            attempts.append(1)
            clock.now += 0.5
            if len(attempts) <= 3:
                raise Boom("400 API key not valid", status_code=400)
            return Answer("on the fourth key")

        with caplog.at_level(logging.ERROR):
            binding.run(host, Agent(), {}, (), {})

        errors = [
            r for r in caplog.records
            if "not a valid credential" in r.getMessage()
        ]
        assert len(errors) == 3, "every dead key has to be named"

    def test_the_switch_gives_every_line_back(self, caplog, monkeypatch):
        monkeypatch.setenv("KAME_STORM_COLLAPSE_DISABLED", "1")
        clock, binding = self._driven(monkeypatch)
        host, _ = self._noisy_host(clock, 30)

        with caplog.at_level(logging.WARNING):
            binding.run(host, Agent(), {}, (), {})

        lines = [r for r in caplog.records if "resting" in r.getMessage()]
        assert len(lines) == 30, f"switched off, every line should be there: {len(lines)}"
        assert binding.suppressed == 0

    def test_collapsing_changes_no_rotation_decision(self, monkeypatch):
        # The whole feature is about text. If it ever moved a key it would be
        # the worst kind of bug: invisible in the thing it was written for.
        def run_once(disabled: bool):
            if disabled:
                monkeypatch.setenv("KAME_STORM_COLLAPSE_DISABLED", "1")
            else:
                monkeypatch.delenv("KAME_STORM_COLLAPSE_DISABLED", raising=False)
            clock, binding = self._driven(monkeypatch)
            host, attempts = self._noisy_host(clock, 25)
            binding.run(host, Agent(), {}, (), {})
            return len(attempts), binding.rotations, round(binding.waited_s, 3)

        assert run_once(True) == run_once(False)
