"""The two promises v1.0.1 adds: an unbounded wait, and an intact stream.

Both are new because the ceiling came off. Agent Zero's ADR 0002 removed every
artificial timeout after watching each one *cause* the failure it was meant to
guard against, and 1.0.1 follows it — but the ADR also records what Agent Zero
never solved: when every key is spent, *"the user typically restarts A0"*. A
restart is not a decision the user made; it is one the silence made for them.

So the wait is unbounded **and** narrated, and both halves are pinned here.

The stream tests belong in the same file because they are the same decision.
Removing the ceiling means more rotations, and a rotation is only safe while
nothing has reached the user yet. The rule that a started stream is never
replayed is what *bounds* the eternal carousel — without it, waiting forever
could mean printing the answer twice.
"""

from __future__ import annotations

import pytest

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


class _Clock:
    """One clock the test drives, so a five-hour wait costs no wall time.

    It has to govern *both* readings or the test deadlocks rather than passes:
    the binding measures elapsed time with ``time.monotonic`` while the engine
    stores cooldown deadlines against ``time.time``. Advancing only the first
    means the binding believes hours went by while the engine still sees every
    key resting — an honest reproduction of a bug, and a useless test.
    """

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start
        self.slept = 0.0

    def install(self, monkeypatch) -> "_Clock":
        # ``dispatch_binding.time`` and ``carousel``'s are the same module
        # object, so one patch reaches both readers.
        monkeypatch.setattr(dispatch_binding.time, "monotonic", lambda: self.now)
        monkeypatch.setattr(dispatch_binding.time, "time", lambda: self.now)
        # The wait loop calls ``_publish`` once per simulated second, and
        # ``state.publish`` does a real tempfile-write-and-replace each time —
        # correct for a live wait (the desktop chip polls at the same 1 Hz),
        # but with the clock faked there is no wall time left to spread those
        # writes over. A six-hour wait here is 21,600 real file operations
        # with no delay between them, which is the entire reason this file
        # took four and a half minutes to run rather than a few seconds. The
        # write path itself is covered by test_v1_1_0.py; this file is about
        # the wait/notice logic, so the publish is a no-op here.
        monkeypatch.setattr(dispatch_binding, "_publish", lambda *a, **k: None)
        return self

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.slept += seconds


def _clock(monkeypatch) -> _Clock:
    return _Clock().install(monkeypatch)


def _resting_pool(binding, *, seconds: float):
    """An agent whose every key is out for `seconds`."""
    agent = Agent()
    identity = carousel.Carousel.identity(agent.provider, agent.model)
    for key in KEYS:
        binding.engine.mark(identity, key, False, seconds, "daily")
    return agent, identity


class TestWaitingForAKeyToComeBack:
    def test_a_multi_hour_wait_is_served_rather_than_abandoned(self, monkeypatch):
        # Three hours. Before 1.0.1 this call died at ten minutes with every
        # key still on cooldown and nothing to show for it.
        clock = _clock(monkeypatch)
        binding = DispatchBinding(engine=Carousel(), sleep=clock.sleep)
        agent, _ = _resting_pool(binding, seconds=3 * 3600.0)

        calls = []

        def host(a, api_kwargs, **kwargs):
            calls.append(1)
            return Answer("worth the wait")

        result = binding.run(host, agent, {}, (), {})

        assert result.choices[0].message.content == "worth the wait"
        assert calls == [1], "no request was spent on a key it knew was resting"
        assert clock.slept > 0
        assert binding.waits > 0
        assert binding.waited_s > 0

    def test_the_wait_is_rechecked_rather_than_committed_to(self, monkeypatch):
        # A three-hour cooldown is not slept in one three-hour call: a key that
        # recovers early, or a pool that gains a key, must be used at once.
        clock = _clock(monkeypatch)
        binding = DispatchBinding(engine=Carousel(), sleep=clock.sleep)
        agent, _ = _resting_pool(binding, seconds=3 * 3600.0)

        binding.run(lambda a, k, **kw: Answer(), agent, {}, (), {})

        assert binding.waits >= 1
        # Each pass sleeps at most the per-pass cap, never the whole cooldown.
        assert clock.slept <= dispatch_binding._MAX_SLEEP_S * binding.waits + 1

    def test_the_user_is_told_a_long_wait_is_a_wait(self, monkeypatch):
        clock = _clock(monkeypatch)
        binding = DispatchBinding(engine=Carousel(), sleep=clock.sleep)
        agent, _ = _resting_pool(binding, seconds=2 * 3600.0)

        said = []
        agent._emit_status = said.append

        binding.run(lambda a, k, **kw: Answer(), agent, {}, (), {})

        assert said, "a multi-hour wait must not be silent"
        first = said[0]
        assert "resting" in first
        assert "stop" in first.lower(), "the way out has to be in the notice"
        assert "back up" in said[-1], "the loop it opened has to be closed"

    def test_the_notice_never_names_a_key(self, monkeypatch):
        # A status line the user cannot screenshot is a status line that leaks.
        clock = _clock(monkeypatch)
        binding = DispatchBinding(engine=Carousel(), sleep=clock.sleep)
        agent, _ = _resting_pool(binding, seconds=2 * 3600.0)

        said = []
        agent._emit_status = said.append
        binding.run(lambda a, k, **kw: Answer(), agent, {}, (), {})

        blob = " ".join(said)
        for key in KEYS:
            assert key not in blob
            assert key[:12] not in blob

    def test_a_short_wait_says_nothing(self, monkeypatch):
        # Below the threshold this is a hiccup. Narrating it would train the
        # user to ignore the notice that matters.
        clock = _clock(monkeypatch)
        binding = DispatchBinding(engine=Carousel(), sleep=clock.sleep)
        agent, _ = _resting_pool(binding, seconds=2.0)

        said = []
        agent._emit_status = said.append
        binding.run(lambda a, k, **kw: Answer(), agent, {}, (), {})

        assert said == []

    def test_a_long_wait_does_not_narrate_every_minute(self, monkeypatch):
        # Sixty lines for an hour of waiting is spam; a handful is information.
        clock = _clock(monkeypatch)
        binding = DispatchBinding(engine=Carousel(), sleep=clock.sleep)
        agent, _ = _resting_pool(binding, seconds=6 * 3600.0)

        said = []
        agent._emit_status = said.append
        binding.run(lambda a, k, **kw: Answer(), agent, {}, (), {})

        # Six hours of waiting, slept a minute at a time, is ~360 passes.
        assert len(said) <= 12, f"too chatty: {len(said)} notices"

    def test_no_notice_can_ever_end_a_turn(self, monkeypatch):
        # `_emit_status` is documented never to raise. If a host ever changes
        # that, a recovering turn must survive it — the notice is the least
        # important thing happening.
        clock = _clock(monkeypatch)
        binding = DispatchBinding(engine=Carousel(), sleep=clock.sleep)
        agent, _ = _resting_pool(binding, seconds=2 * 3600.0)

        def explode(_message):
            raise RuntimeError("the gateway went away")

        agent._emit_status = explode
        result = binding.run(lambda a, k, **kw: Answer("fine"), agent, {}, (), {})
        assert result.choices[0].message.content == "fine"

    def test_a_host_with_no_status_channel_still_waits(self, monkeypatch):
        # Older Hermes, or a bare agent: the wait is the feature, the notice is
        # the courtesy, and the courtesy is not allowed to be a requirement.
        clock = _clock(monkeypatch)
        binding = DispatchBinding(engine=Carousel(), sleep=clock.sleep)
        agent, _ = _resting_pool(binding, seconds=2 * 3600.0)
        assert not hasattr(agent, "_emit_status")

        result = binding.run(lambda a, k, **kw: Answer("fine"), agent, {}, (), {})
        assert result.choices[0].message.content == "fine"

    def test_a_stop_during_a_long_wait_is_honoured_within_a_second(self, monkeypatch):
        clock = _clock(monkeypatch)
        binding = DispatchBinding(engine=Carousel(), sleep=clock.sleep)
        agent, _ = _resting_pool(binding, seconds=5 * 3600.0)

        def stop_after_one_slice(seconds):
            clock.sleep(seconds)
            agent._interrupt_requested = True

        binding._sleep = stop_after_one_slice
        binding.run(lambda a, k, **kw: Answer(), agent, {}, (), {})

        assert clock.slept <= dispatch_binding._SLEEP_SLICE_S, (
            "a stop must not wait out the remaining five hours"
        )


class TestStreamIntegrity:
    """Nothing the user has already read is ever printed again, or lost."""

    def _streaming_agent(self):
        agent = Agent()
        seen = []
        agent.stream_delta_callback = seen.append
        return agent, seen

    def test_text_already_delivered_is_never_delivered_twice(self):
        agent, seen = self._streaming_agent()
        attempts = []

        def host(a, api_kwargs, **kwargs):
            attempts.append(1)
            a.stream_delta_callback("Hello, the answer is ")
            raise Boom("503 Service Unavailable", status_code=503)

        with pytest.raises(Boom):
            _binding().run(host, agent, {}, (), {})

        assert attempts == [1], "a started stream must not be retried"
        assert seen == ["Hello, the answer is "]

    def test_a_failure_before_the_first_chunk_rotates_cleanly(self):
        agent, seen = self._streaming_agent()
        attempts = []

        def host(a, api_kwargs, **kwargs):
            attempts.append(1)
            if len(attempts) < 3:
                raise Boom("503", status_code=503)
            a.stream_delta_callback("the whole answer")
            return Answer("the whole answer")

        _binding().run(host, agent, {}, (), {})

        assert len(attempts) == 3
        assert seen == ["the whole answer"], "no duplicated and no partial text"

    def test_chunks_reach_the_user_unchanged_and_in_order(self):
        # The shim wraps the delivery callback. One that dropped, reordered or
        # mutated a chunk would corrupt every streamed answer, silently.
        agent, seen = self._streaming_agent()
        chunks = ["The ", "quick ", "brown ", "fox", " jumps."]

        def host(a, api_kwargs, **kwargs):
            for chunk in chunks:
                a.stream_delta_callback(chunk)
            return Answer("".join(chunks))

        _binding().run(host, agent, {}, (), {})

        assert seen == chunks
        assert "".join(seen) == "The quick brown fox jumps."

    def test_the_shim_returns_what_the_real_callback_returned(self):
        # Hermes uses these return values for early stopping. A shim that
        # swallowed them would break cancellation, not styling.
        agent = Agent()
        agent.stream_delta_callback = lambda text: "STOP" if text == "halt" else None
        returned = []

        def host(a, api_kwargs, **kwargs):
            returned.append(a.stream_delta_callback("go"))
            returned.append(a.stream_delta_callback("halt"))
            return Answer()

        _binding().run(host, agent, {}, (), {})
        assert returned == [None, "STOP"]

    def test_many_rotations_leave_no_shim_stacked_on_the_agent(self, monkeypatch):
        # Twenty rotations must not leave twenty wrappers. The user would pay
        # for that on every chunk of the answer that finally works. Twenty
        # rotations through four keys means the pool empties and the carousel
        # waits, so this needs the driven clock too.
        clock = _clock(monkeypatch)
        binding = DispatchBinding(engine=Carousel(), sleep=clock.sleep)
        agent, seen = self._streaming_agent()
        real = agent.stream_delta_callback
        attempts = []

        def host(a, api_kwargs, **kwargs):
            attempts.append(1)
            if len(attempts) < 20:
                raise Boom("503", status_code=503)
            a.stream_delta_callback("done")
            return Answer()

        binding.run(host, agent, {}, (), {})

        assert agent.stream_delta_callback is real
        assert seen == ["done"]

    def test_a_stream_that_started_is_not_replayed_even_after_a_long_wait(
        self, monkeypatch
    ):
        # The two 1.0.1 promises meeting: the carousel waited hours, got a key,
        # started streaming, and then dropped. The wait does not buy the right
        # to print the answer twice.
        clock = _clock(monkeypatch)
        binding = DispatchBinding(engine=Carousel(), sleep=clock.sleep)
        agent, _ = _resting_pool(binding, seconds=2 * 3600.0)
        seen = []
        agent.stream_delta_callback = seen.append
        attempts = []

        def host(a, api_kwargs, **kwargs):
            attempts.append(1)
            a.stream_delta_callback("half an answer")
            raise Boom("503", status_code=503)

        with pytest.raises(Boom):
            binding.run(host, agent, {}, (), {})

        assert attempts == [1]
        assert seen == ["half an answer"]
