"""Complete plugin dispatch/wait loop with synthetic host replies and clock."""
import pytest

from .test_dispatch import Agent, Answer, Boom, DispatchBinding, Carousel, dispatch_binding


@pytest.mark.parametrize("count", [1, 2, 14])
@pytest.mark.parametrize("status", [429, 503])
def test_all_keys_rest_and_one_recovers_without_parallel_requests(monkeypatch, count, status):
    now = [1800000000.0]
    started = now[0]
    calls = []
    keys = [f"synthetic-{i}" for i in range(count)]
    def sleep(seconds):
        assert seconds > 0
        now[0] += seconds
        assert now[0] < started + 120, "Unexpected retry/wait loop"
    monkeypatch.setattr(dispatch_binding.time, "time", lambda: now[0])
    monkeypatch.setattr(dispatch_binding.time, "monotonic", lambda: now[0])
    binding = DispatchBinding(engine=Carousel(), sleep=sleep, jitter=lambda: 0)
    active = [False]
    def host(agent, api_kwargs, **kwargs):
        assert not active[0], "Overlapping requests"
        active[0] = True
        try:
            calls.append((agent.api_key, now[0]))
            assert len(calls) <= count + 1
            if len(calls) <= count:
                now[0] += 0.1
                error = Boom("RESOURCE_EXHAUSTED" if status == 429 else "UNAVAILABLE", status)
                error.retry_after = 30
                raise error
            return Answer("recovered")
        finally:
            active[0] = False
    result = binding.run(host, Agent(keys), {}, (), {})
    assert result.choices[0].message.content == "recovered"
    assert len(calls) == count + 1
    assert len({key for key, _ in calls[:count]}) == count
    assert calls[-1][0] == calls[0][0]
    first_deadline = started + 0.1 + 30
    assert first_deadline <= calls[-1][1] <= first_deadline + 0.501
    assert binding.waits >= 1


@pytest.mark.parametrize("count", [1, 2, 14])
def test_early_key_recovery_wakes_current_wait(monkeypatch, count):
    from .test_waiting import _clock
    clock = _clock(monkeypatch)
    keys = [f"early-{i}" for i in range(count)]
    agent = Agent(keys)
    identity = Carousel.identity(agent.provider, agent.model)
    start = clock.now
    engine = Carousel()
    for key in keys:
        engine.mark(identity, key, False, 300, "rate_limit", stated=True)
    recovered_at = []
    def sleep(seconds):
        clock.sleep(seconds)
        if not recovered_at:
            # A concurrent completed call establishes recovery, not speculation.
            engine.mark(identity, keys[0], True)
            recovered_at.append(clock.now)
        assert clock.now < start + 120
    binding = DispatchBinding(engine=engine, sleep=sleep)
    calls = []
    def host(a, api_kwargs, **kwargs):
        calls.append(clock.now)
        return Answer("available")
    binding.run(host, agent, {}, (), {})
    assert len(calls) == 1
    assert calls[0] - recovered_at[0] <= 1.0


def test_retired_key_expiry_does_not_count_as_early_recovery(monkeypatch):
    from .test_waiting import _clock
    clock = _clock(monkeypatch)
    agent = Agent(["retired", "valid"])
    identity = Carousel.identity(agent.provider, agent.model)
    engine = Carousel()
    start = clock.now
    engine.mark(identity, "retired", False, 20, "revoked")
    engine.mark(identity, "valid", False, 30, "rate_limit", stated=True)
    binding = DispatchBinding(engine=engine, sleep=clock.sleep)
    calls = []
    def host(a, api_kwargs, **kwargs):
        calls.append((a.api_key, clock.now))
        return Answer()
    binding.run(host, agent, {}, (), {})
    assert calls == [("valid", start + 30.5)]


def test_cancellation_wins_when_key_recovers_during_sleep(monkeypatch):
    from .test_waiting import _clock
    clock = _clock(monkeypatch)
    agent = Agent(["valid"])
    identity = Carousel.identity(agent.provider, agent.model)
    engine = Carousel()
    engine.mark(identity, "valid", False, 300, "rate_limit", stated=True)
    def sleep(seconds):
        clock.sleep(seconds)
        engine.mark(identity, "valid", True)
        agent._interrupt_requested = True
    binding = DispatchBinding(engine=engine, sleep=sleep)
    calls = []
    delegated = []
    def host(a, api_kwargs, **kwargs):
        # The existing contract delegates cancellation to Hermes when no
        # request has failed in this turn. Delegation is not a network call.
        delegated.append(a._interrupt_requested)
        if not a._interrupt_requested:
            calls.append(1)
        return Answer()
    binding.run(host, agent, {}, (), {})
    assert delegated == [True]
    assert calls == []


def test_new_credential_wakes_wait_and_is_attributed(monkeypatch):
    from .test_waiting import _clock
    from .test_dispatch import Entry
    clock = _clock(monkeypatch)
    agent = Agent(["old"])
    identity = Carousel.identity(agent.provider, agent.model)
    engine = Carousel()
    engine.mark(identity, "old", False, 300, "rate_limit", stated=True)
    start = clock.now
    added = []
    def sleep(seconds):
        clock.sleep(seconds)
        if not added:
            agent._credential_pool._entries.append(Entry("new", "new-entry"))
            added.append(clock.now)
        assert clock.now < start + 65, "New credential ignored during wait"
    binding = DispatchBinding(engine=engine, sleep=sleep)
    calls = []
    def host(a, api_kwargs, **kwargs):
        calls.append((a.api_key, a._credential_pool_entry_id, clock.now))
        return Answer()
    binding.run(host, agent, {}, (), {})
    assert calls == [("new", "new-entry", added[0])]
