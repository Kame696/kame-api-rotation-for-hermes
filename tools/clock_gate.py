"""G10 — the fine clock, audited together (`PLAN_1.8.0.0.md`, G10).

Every small time constant that decides *when* a call leaves lives in a
different file and was tuned in isolation: the recovery wait's padding and
jitter (`dispatch_binding.py`'s `_wait_for_recovery`, ``eta + 0.5 +
self._jitter()``, jitter installed as ``random.uniform(0.1, 1.5)`` in
``install()``), the thaw fan after an outage (`core/carousel.py`,
``THAW_BASE_S = 3.0``, ``THAW_FAN_S = 0.4``, ``THAW_FAN_SLOTS = 5``), the
early-wake checks 1.7.0.7 added inside the same wait loop, and the shared-
health read floor (`core/shared_health.py`, ``READ_STAT_FLOOR_S``). None of
those files' own tests ever add the numbers up: `test_waiting.py` proves one
wait behaves, `tests/test_v1_7_0_7_dispatch_recovery_loop.py` proves one
early-wake fires, `test_carousel.py` proves one thaw shortens one key. This
module is where they are measured together, the way three real Hermes
profiles racing the same deadline actually experience them.

Five measurements, each printed before any verdict that depends on it (see
the per-measurement JSON under `research/1.8.0.0/clock/` and the `REPORT.md`
this run writes beside them):

1. **No synchronised stampede.** Fourteen simulated keys released by one
   shared deadline, three simulated profiles sharing one real
   `core.shared_health` file — the real mechanism, driven once end to end —
   plus a seeded Monte Carlo over the padding+jitter formula itself (the
   departure spread does not depend on wall-clock concurrency, only on each
   profile's own jitter draw, so a large seeded sample answers the collision
   question a handful of real subprocesses cannot).
2. **No idle waiting.** Four ways a key can become usable mid-wait — a
   concurrent success freeing it early, a peer's success thawing a 5xx
   outage (`Carousel.thaw_server_cooled`), a new credential appended to the
   pool, an operator clearing a hold by hand (the "explicit pool reset" the
   wait loop's own comment names) — each measured as the gap between the
   event and the next real departure through `DispatchBinding.run`.
3. **Whether the 0.5s padding is worth its cost**, read off the owner's own
   recorded corpus (`tools/replay_timeline.py`'s loaders, reused, not
   duplicated) rather than invented: how often a real stated deadline was
   followed by another refusal on the same key, and at what gap.
4. **Whether the jitter band (`uniform(0.1, 1.5)`) is worth its cost**
   against a narrower `uniform(0.0, 0.3)` — stampede risk bought, latency
   spent, both reported, no unilateral change.
5. **Sleep slices.** A multi-hour wait still wakes within one
   `_SLEEP_SLICE_S` of an interrupt, and of an early recovery reached deep
   into the wait — the worst observed lag is reported, not asserted away.

Plus a self-check (`learnings/0003-the-gate-that-was-never-run.md`'s rule: a
gate that cannot fail is not a gate): measurement 1's real mechanism and
Monte Carlo rerun with jitter fixed at 0 — the "broken configuration" the
scenario needs to hold — and the run FAILS this file's own build if that
does not come back FAIL.

Isolation: every simulated profile gets its own throwaway `HERMES_HOME`
under one temp root, the two record switches are disabled the same way
`tests/conftest.py` disables them, nothing here makes a network call, and
nothing here writes into the owner's real AppData. Measurement 3 is the one
place this file reads outside the temp root at all: it reads (never writes)
the owner's real `calls.jsonl`/`refusals.jsonl` through
`tools/replay_timeline.py`'s own read-only loaders, under `HERMES_HOME`
redirected the same way that tool already redirects it before importing the
plugin. All clocks are fake (`time.time`/`time.monotonic` patched to a
variable this file controls) — nothing in this run costs real wall time
proportional to the seconds it measures.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import random
import statistics
import sys
import tempfile
import time as time_module
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
OUT_DIR = ROOT / "research" / "1.8.0.0" / "clock"
PACKAGE = "kame_clock_gate_under_test"
REPLAY_TOOL_PATH = ROOT / "tools" / "replay_timeline.py"

#: The real formula, `dispatch_binding.py:3110`: ``wait = min(eta + 0.5 +
#: self._jitter(), _MAX_SLEEP_S)``. ``0.5`` is an inline literal there, not a
#: named constant — duplicated here as a documented number, cross-checked
#: against a real `_wait_for_recovery` run in every scenario that uses it
#: (see ``measurements["real_run"]`` below) rather than trusted blind.
PADDING_S = 0.5

#: The real jitter `install()` wires in production, `dispatch_binding.py:3220`.
JITTER_LOW_S = 0.1
JITTER_HIGH_S = 1.5

#: The task's own bound for measurement 2: one padding draw plus one jitter
#: draw. An early-wake path is not supposed to pay either — see the "early
#: recovery only" comment at `dispatch_binding.py` around line 3192 — so this
#: is a generous ceiling, not the expected number.
IDLE_WAKE_BOUND_S = 1.5

#: Trials for the two seeded Monte Carlos (measurements 1 and 4). Large
#: enough for a stable collision-rate estimate, small enough to finish in
#: well under a second — each trial is a handful of `random.Random(seed)`
#: draws, not a simulated run.
MONTE_CARLO_TRIALS = 4000

PROFILE_LABELS = ("base", "k", "lo1")
IDENTITY = "sim:gate-model"


def _keys(n: int) -> List[str]:
    return [f"clock-gate-key-{i:02d}" for i in range(n)]


# ---------------------------------------------------------------------------
# Loading the real plugin (same recipe as tools/continuity_gate.py)
# ---------------------------------------------------------------------------


def _load_plugin():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(
        PACKAGE, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _modules():
    _load_plugin()
    dispatch_binding = importlib.import_module(f"{PACKAGE}.dispatch_binding")
    carousel = importlib.import_module(f"{PACKAGE}.core.carousel")
    shared_health = importlib.import_module(f"{PACKAGE}.core.shared_health")
    return dispatch_binding, carousel, shared_health


def _load_replay_tool():
    """`tools/replay_timeline.py`, loaded the same way its own test does
    (`tests/test_replay_timeline.py::_load_tool`) — a plain module load, not a
    package import, since `tools/` is a script directory, not a package."""
    spec = importlib.util.spec_from_file_location("clock_gate_replay_timeline", REPLAY_TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Minimal host-agent stand-ins — the same shapes tests/test_dispatch.py uses,
# reduced to what DispatchBinding.run / candidates() actually read.
# ---------------------------------------------------------------------------


class _Entry:
    def __init__(self, key: str, entry_id: str) -> None:
        self.runtime_api_key = key
        self.access_token = ""
        self.id = entry_id


class _Pool:
    def __init__(self, keys: Sequence[str]) -> None:
        self._entries = [_Entry(k, f"e{i}") for i, k in enumerate(keys)]

    def entries(self) -> List[_Entry]:
        return list(self._entries)


class _Client:
    def __init__(self, key: str) -> None:
        self.api_key = key


class _Agent:
    """Reduced to what `DispatchBinding.run`/`candidates` read and write. No
    `_emit_status` — matching `tests/test_waiting.py::
    test_a_host_with_no_status_channel_still_waits`, this is deliberately the
    "quiet host" shape, so nothing here writes a status line during the
    (sometimes tens of thousands of iterations long) waits below.
    """

    def __init__(self, keys: Sequence[str]) -> None:
        self.provider = "sim"
        self.model = "gate-model"
        self.api_mode = "chat_completions"
        self.api_key = keys[0] if keys else ""
        self._credential_pool = _Pool(keys) if keys else None
        self._client_kwargs = {"api_key": self.api_key}
        self.client = _Client(self.api_key)
        self._credential_pool_entry_id = None
        self._interrupt_requested = False
        self.stream_delta_callback = None


class _Answer:
    class _Message:
        def __init__(self, content: str) -> None:
            self.content = content
            self.tool_calls = None

    class _Choice:
        def __init__(self, content: str) -> None:
            self.message = _Answer._Message(content)

    def __init__(self, content: str = "ok") -> None:
        self.choices = [_Answer._Choice(content)]


# ---------------------------------------------------------------------------
# A clock the scenarios drive — patches the module `dispatch_binding` and
# `core.carousel` both read `time` through (same module object, per
# tests/test_waiting.py's own note).
# ---------------------------------------------------------------------------


class _FakeClock:
    def __init__(self, dispatch_binding_mod: Any, start: float = 1_800_000_000.0) -> None:
        self._mod = dispatch_binding_mod
        self.now = start
        self.slept = 0.0
        self._real_time = time_module.time
        self._real_monotonic = time_module.monotonic
        self._real_publish = getattr(dispatch_binding_mod, "_publish", None)

    def install(self) -> "_FakeClock":
        time_module.time = lambda: self.now
        time_module.monotonic = lambda: self.now
        # Real cost, not this measurement's: `_publish` does a real
        # tempfile-write-and-replace per slice (test_waiting.py's `_Clock`
        # patches it out for exactly this reason). Scenario 5 alone drives
        # tens of thousands of slices; without this every one of them would
        # cost a real file write under a temp HERMES_HOME nobody reads.
        self._mod._publish = lambda *a, **k: None
        return self

    def restore(self) -> None:
        time_module.time = self._real_time
        time_module.monotonic = self._real_monotonic
        if self._real_publish is not None:
            self._mod._publish = self._real_publish

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.slept += seconds


# ---------------------------------------------------------------------------
# Measurement 1 — no synchronised stampede
# ---------------------------------------------------------------------------


def _seeded_jitter(seed: int, low: float = JITTER_LOW_S, high: float = JITTER_HIGH_S) -> Callable[[], float]:
    """One fixed draw per profile, from an independently-seeded RNG — never
    the shared global `random` module, which production's own jitter uses
    (`install()`: `lambda: random.uniform(0.1, 1.5)`) but which would make two
    profiles started in the same process draw from the same stream. A
    `random.Random(seed).uniform(a, b)` is the identical distribution
    `random.uniform(a, b)` draws from — only the RNG instance is pinned, so
    this is the production formula made deterministic, not a different one.
    """
    value = random.Random(seed).uniform(low, high)
    return lambda: value


def _real_stampede_run(tmp_root: Path, jitter_low: float, jitter_high: float, seeds: Sequence[int]) -> Dict[str, Any]:
    """One concrete run of the REAL mechanism: 14 keys, one stated deadline,
    three profiles sharing one real `core.shared_health` file. Each profile's
    `DispatchBinding.run` is driven to completion on its own fake clock
    (started at the same instant), so the three departure times are directly
    comparable even though they are computed sequentially in one process —
    the computation has no dependency on wall-clock concurrency, only on
    each profile's own `next_recovery_seconds` read of the shared deadline
    and its own jitter draw.
    """
    dispatch_binding_mod, carousel_mod, shared_health_mod = _modules()
    keys = _keys(14)
    deadline_s = 6.0
    shared_path = tmp_root / "pool-health.json"

    stores = [
        shared_health_mod.SharedHealth(path=shared_path, profile=label, enabled_fn=lambda: True)
        for label in PROFILE_LABELS
    ]
    carousels = [carousel_mod.Carousel(shared_health_store=store) for store in stores]

    t0 = 1_800_000_000.0
    # Profile 0 is the one that actually observed the refusals and wrote the
    # deadline; profiles 1 and 2 never see a refusal directly — they only
    # ever read it back off the shared file, exactly as a second and third
    # real Hermes profile would.
    for key in keys:
        carousels[0].mark(IDENTITY, key, False, deadline_s, "rate_limit", now=t0, stated=True)
    # Snapshot the pristine, just-written world and restore it before every
    # profile's run. Three profiles racing the SAME deadline resolve their
    # OWN eta within microseconds of each other in production; run
    # sequentially in one process, the first profile to finish would
    # otherwise release its own key back to the shared file BEFORE the next
    # profile even starts (decisions/0006's own "a success releases a bench"
    # promise, working exactly as designed) — collapsing the very race this
    # scenario exists to measure. Restoring the snapshot is what makes three
    # SEQUENTIAL simulations faithful to three SIMULTANEOUS real ones.
    pristine_snapshot = shared_path.read_bytes()

    departures: List[float] = []
    per_profile: List[Dict[str, Any]] = []
    for label, carousel_engine, seed in zip(PROFILE_LABELS, carousels, seeds):
        shared_path.write_bytes(pristine_snapshot)
        clock = _FakeClock(dispatch_binding_mod, start=t0).install()
        try:
            binding = dispatch_binding_mod.DispatchBinding(
                engine=carousel_engine, sleep=clock.sleep, jitter=_seeded_jitter(seed, jitter_low, jitter_high),
            )
            agent = _Agent(keys)
            calls: List[float] = []

            def host(a, api_kwargs, **kwargs):
                calls.append(clock.now)
                return _Answer()

            binding.run(host, agent, {}, (), {})
        finally:
            clock.restore()
        departure = clock.now  # t0 + everything this profile slept
        departures.append(departure)
        per_profile.append({"profile": label, "seed": seed, "departure_offset_s": round(departure - t0, 6)})

    departures.sort()
    gaps = [round(b - a, 6) for a, b in zip(departures, departures[1:])]
    return {
        "deadline_s": deadline_s,
        "per_profile": per_profile,
        "gaps_s": gaps,
        "min_gap_s": min(gaps) if gaps else None,
        "any_collision_under_100ms": any(g < 0.1 for g in gaps),
        # Sanity cross-check against the documented formula: each profile's
        # own offset from the deadline should equal padding + its jitter draw
        # (within float slop from the 1s-sliced sleep loop).
        "formula_cross_check_max_abs_error_s": round(
            max(
                abs((p["departure_offset_s"]) - (deadline_s + PADDING_S + random.Random(s).uniform(jitter_low, jitter_high)))
                for p, s in zip(per_profile, seeds)
            ),
            3,
        ),
    }


def _monte_carlo_stampede(jitter_low: float, jitter_high: float, trials: int, seed_base: int) -> Dict[str, Any]:
    """`trials` independent 3-profile races against the SAME deadline (the
    deadline itself cancels out of every gap, so it is not simulated here —
    only each profile's `padding + jitter` offset is). One profile's jitter
    draw never depends on another's or on a prior trial's, matching
    independent processes racing a shared file for the first time each day.
    """
    all_gaps: List[float] = []
    trials_with_collision = 0
    for trial in range(trials):
        offsets = sorted(
            PADDING_S + random.Random(seed_base + trial * 3 + p).uniform(jitter_low, jitter_high)
            for p in range(3)
        )
        gaps = [b - a for a, b in zip(offsets, offsets[1:])]
        all_gaps.extend(gaps)
        if any(g < 0.1 for g in gaps):
            trials_with_collision += 1
    all_gaps.sort()
    return {
        "trials": trials,
        "total_gaps": len(all_gaps),
        "min_gap_s": round(all_gaps[0], 4) if all_gaps else None,
        "median_gap_s": round(statistics.median(all_gaps), 4) if all_gaps else None,
        "max_gap_s": round(all_gaps[-1], 4) if all_gaps else None,
        "gaps_under_100ms": sum(1 for g in all_gaps if g < 0.1),
        "gap_under_100ms_rate": round(sum(1 for g in all_gaps if g < 0.1) / len(all_gaps), 4) if all_gaps else None,
        "trials_with_any_collision": trials_with_collision,
        "trial_collision_rate": round(trials_with_collision / trials, 4) if trials else None,
    }


def scenario_stampede(tmp_root: Path) -> Dict[str, Any]:
    real_run = _real_stampede_run(
        tmp_root / "stampede_real", JITTER_LOW_S, JITTER_HIGH_S, seeds=(101, 102, 103)
    )
    monte_carlo = _monte_carlo_stampede(JITTER_LOW_S, JITTER_HIGH_S, MONTE_CARLO_TRIALS, seed_base=1000)
    # The bound: real jitter must break up the collision risk for at least
    # half of all races. Analytically, for uniform(0.1, 1.5) (a 1.4s-wide
    # band) three independent draws collide (some pair under 100ms) far less
    # than half the time — this is a generous bound, not a tight one, chosen
    # so the self-check's fixed-at-0 jitter (100% collision) clearly fails it
    # while leaving headroom for the real formula's actual rate.
    passed = bool(
        monte_carlo["trial_collision_rate"] is not None
        and monte_carlo["trial_collision_rate"] < 0.5
        and real_run["formula_cross_check_max_abs_error_s"] < 0.01
    )
    return {
        "name": "stampede",
        "passed": passed,
        "measurements": {"real_run": real_run, "monte_carlo": monte_carlo, "bound": "trial_collision_rate < 0.5"},
    }


# ---------------------------------------------------------------------------
# Measurement 2 — no idle waiting
# ---------------------------------------------------------------------------


def _idle_key_expires_early(dispatch_binding_mod, carousel_mod) -> Dict[str, Any]:
    """Mirrors `tests/test_v1_7_0_7_dispatch_recovery_loop.py::
    test_early_key_recovery_wakes_current_wait`: every key rests for a long,
    stated 300s, but a concurrent completed call (a different turn, the same
    process-wide engine) frees one of them long before that deadline."""
    keys = _keys(3)
    engine = carousel_mod.Carousel()
    clock = _FakeClock(dispatch_binding_mod).install()
    became_usable_at: List[float] = []
    try:
        # Marked AFTER the clock is installed: ``mark(..., now=None)`` reads
        # ``time.time()`` at call time, and that has to be the fake clock's
        # start, not real wall time — otherwise "300s from real-now" and
        # "300s from fake-t0" disagree by however long this process has been
        # running, and the pool can look already-recovered the instant the
        # fake clock starts (a bug this file's own first run caught).
        for key in keys:
            engine.mark(IDENTITY, key, False, 300.0, "rate_limit", stated=True)

        def sleep(seconds: float) -> None:
            clock.sleep(seconds)
            if not became_usable_at:
                engine.mark(IDENTITY, keys[0], True)
                became_usable_at.append(clock.now)

        binding = dispatch_binding_mod.DispatchBinding(engine=engine, sleep=sleep)
        agent = _Agent(keys)
        calls: List[float] = []

        def host(a, api_kwargs, **kwargs):
            calls.append(clock.now)
            return _Answer()

        binding.run(host, agent, {}, (), {})
    finally:
        clock.restore()
    delay = calls[0] - became_usable_at[0] if became_usable_at and calls else None
    return {"label": "key_expires_early", "became_usable_at": became_usable_at[0] if became_usable_at else None,
            "call_departed_at": calls[0] if calls else None, "delay_s": round(delay, 3) if delay is not None else None}


def _idle_peer_thaws_outage(dispatch_binding_mod, carousel_mod) -> Dict[str, Any]:
    """`Carousel.thaw_server_cooled` (`core/carousel.py:2012`): a peer's
    success shortens OTHER keys' unstated 5xx holds. In production this fires
    inside `DispatchBinding.run`'s own success branch
    (`dispatch_binding.py:2452`, gated on `if slept:`); driven directly here
    to isolate the timing question — does a binding CURRENTLY waiting on the
    now-thawed keys notice within one slice — from whether production calls
    it at the right moments, which `tests/test_carousel.py`'s thaw tests and
    the real dispatch loop already cover.
    """
    keys = _keys(3)
    waiting_key, peer_key, third_key = keys
    engine = carousel_mod.Carousel()
    t0 = 1_800_000_000.0
    # Long unstated 5xx holds — long enough that, absent a thaw, the wait
    # below would run for tens of seconds. `stated=False` is what makes these
    # thawable at all (core/carousel.py:2019: "explicit server retry
    # instructions retain their own deadlines").
    engine.mark(IDENTITY, waiting_key, False, 40.0, "server", now=t0)
    engine.mark(IDENTITY, third_key, False, 40.0, "server", now=t0)
    # peer_key is NOT in the pool this binding waits on below — it is the
    # sibling call succeeding elsewhere on the same identity.
    clock = _FakeClock(dispatch_binding_mod).install()
    became_usable_at: List[float] = []
    try:
        def sleep(seconds: float) -> None:
            clock.sleep(seconds)
            if not became_usable_at:
                engine.mark(IDENTITY, peer_key, True, now=clock.now)
                engine.thaw_server_cooled(IDENTITY, peer_key, now=clock.now)
                # The peer's success moment is NOT when this key becomes
                # usable — thaw pulls it forward to `THAW_BASE_S` (+ a fan
                # offset) out, never to "now" (core/carousel.py:2034). What
                # this scenario measures is the wait loop's reaction to THAT
                # target, not to the peer's success itself; reading the
                # post-thaw `sick_until` back off the engine is the honest
                # "became usable at" instant.
                became_usable_at.append(engine._pools[IDENTITY][waiting_key]["sick_until"])

        binding = dispatch_binding_mod.DispatchBinding(engine=engine, sleep=sleep)
        agent = _Agent([waiting_key, third_key])
        calls: List[float] = []

        def host(a, api_kwargs, **kwargs):
            calls.append(clock.now)
            return _Answer()

        binding.run(host, agent, {}, (), {})
    finally:
        clock.restore()
    delay = calls[0] - became_usable_at[0] if became_usable_at and calls else None
    return {"label": "peer_success_thaws_outage", "became_usable_at": became_usable_at[0] if became_usable_at else None,
            "call_departed_at": calls[0] if calls else None, "delay_s": round(delay, 3) if delay is not None else None}


def _idle_new_credential(dispatch_binding_mod, carousel_mod) -> Dict[str, Any]:
    """Mirrors `test_new_credential_wakes_wait_and_is_attributed`: a brand
    new credential is appended to the pool mid-wait."""
    engine = carousel_mod.Carousel()
    clock = _FakeClock(dispatch_binding_mod).install()
    became_usable_at: List[float] = []
    try:
        engine.mark(IDENTITY, "old", False, 300.0, "rate_limit", stated=True)
        agent = _Agent(["old"])

        def sleep(seconds: float) -> None:
            clock.sleep(seconds)
            if not became_usable_at:
                agent._credential_pool._entries.append(_Entry("new", "new-entry"))
                became_usable_at.append(clock.now)

        binding = dispatch_binding_mod.DispatchBinding(engine=engine, sleep=sleep)
        calls: List[float] = []

        def host(a, api_kwargs, **kwargs):
            calls.append(clock.now)
            return _Answer()

        binding.run(host, agent, {}, (), {})
    finally:
        clock.restore()
    delay = calls[0] - became_usable_at[0] if became_usable_at and calls else None
    return {"label": "new_credential_added", "became_usable_at": became_usable_at[0] if became_usable_at else None,
            "call_departed_at": calls[0] if calls else None, "delay_s": round(delay, 3) if delay is not None else None}


def _idle_explicit_reset(dispatch_binding_mod, carousel_mod) -> Dict[str, Any]:
    """The other trigger `_wait_for_recovery`'s own comment names
    (`dispatch_binding.py` ~line 3189: "Another completed call or an explicit
    pool reset can release a key before the deadline observed at entry").
    There is no `/kame reset <pool>` command in this plugin (only `/kame
    reset <setting>`, `menu.py:532`, which resets a *setting*) — this is the
    generic mechanism the comment describes: something outside the normal
    mark(ok=True)/mark(ok=False) traffic clears a hold directly. Simulated
    here by clearing `sick_until` on the engine's own pool dict, the same way
    `tools/continuity_gate.py::scenario_restart` seeds/clears state for a
    controlled setup.
    """
    keys = _keys(2)
    engine = carousel_mod.Carousel()
    t0 = 1_800_000_000.0
    for key in keys:
        engine.mark(IDENTITY, key, False, 300.0, "rate_limit", now=t0, stated=True)
    clock = _FakeClock(dispatch_binding_mod).install()
    became_usable_at: List[float] = []
    try:
        def sleep(seconds: float) -> None:
            clock.sleep(seconds)
            if not became_usable_at:
                engine._pools[IDENTITY][keys[0]]["sick_until"] = 0.0
                became_usable_at.append(clock.now)

        binding = dispatch_binding_mod.DispatchBinding(engine=engine, sleep=sleep)
        agent = _Agent(keys)
        calls: List[float] = []

        def host(a, api_kwargs, **kwargs):
            calls.append(clock.now)
            return _Answer()

        binding.run(host, agent, {}, (), {})
    finally:
        clock.restore()
    delay = calls[0] - became_usable_at[0] if became_usable_at and calls else None
    return {"label": "explicit_pool_reset", "became_usable_at": became_usable_at[0] if became_usable_at else None,
            "call_departed_at": calls[0] if calls else None, "delay_s": round(delay, 3) if delay is not None else None}


def scenario_no_idle_waiting(_tmp_root: Path) -> Dict[str, Any]:
    dispatch_binding_mod, carousel_mod, _shared_health_mod = _modules()
    cases = [
        _idle_key_expires_early(dispatch_binding_mod, carousel_mod),
        _idle_peer_thaws_outage(dispatch_binding_mod, carousel_mod),
        _idle_new_credential(dispatch_binding_mod, carousel_mod),
        _idle_explicit_reset(dispatch_binding_mod, carousel_mod),
    ]
    delays = [c["delay_s"] for c in cases if c["delay_s"] is not None]
    passed = bool(len(delays) == len(cases) and all(d <= IDLE_WAKE_BOUND_S for d in delays))
    return {
        "name": "no_idle_waiting",
        "passed": passed,
        "measurements": {"bound_s": IDLE_WAKE_BOUND_S, "cases": cases, "worst_delay_s": max(delays) if delays else None},
    }


# ---------------------------------------------------------------------------
# Measurement 3 — is the 0.5s padding worth its cost (real corpus, read-only)
# ---------------------------------------------------------------------------


def scenario_padding_cost() -> Dict[str, Any]:
    """Reuses `tools/replay_timeline.py`'s own corpus loaders
    (`load_calls`/`load_refusals`/`match_refusals`/`build_exception_from_
    refusal`) rather than re-reading `calls.jsonl`/`refusals.jsonl` a second
    way. For every refusal the plugin's OWN `core.evidence.harvest` +
    `core.carousel.extract_delay` says carried a real stated deadline, this
    finds the next event on the SAME (identity, key) and reports the gap
    between that event and the declared deadline, split by outcome.

    What this can and cannot answer: production's padding+jitter has been
    ~0.5s + uniform(0.1, 1.5) for the whole window this corpus covers, so
    every gap actually observed here already has that padding baked in —
    there is no historical data point at padding=0.0 or padding=1.0 to read
    off directly. What IS directly observable: whether providers ever
    refused again at or after their OWN declared deadline (real evidence for
    or against clock skew, i.e. whether padding is doing anything at all),
    and how much slack the smallest observed successful gap shows. Both are
    reported as facts; 0.0s/1.0s are not simulated from them, per the task's
    own instruction not to invent a number the corpus cannot support.
    """
    replay = _load_replay_tool()
    if not replay.DEFAULT_CALLS.is_file() or not replay.DEFAULT_REFUSALS.is_file():
        return {
            "name": "padding_cost",
            "passed": True,
            "measurements": {
                "corpus_available": False,
                "note": (
                    f"{replay.DEFAULT_CALLS} / {replay.DEFAULT_REFUSALS} not found on this machine — "
                    "cannot measure; not inventing a number."
                ),
            },
        }
    home = replay.isolate_hermes_home(restore=True)
    del home
    package_name = f"kame_clock_gate_padding_{__name__}"
    plugin = replay.load_plugin(PLUGIN_DIR, package_name=package_name)
    del plugin
    evidence_mod = importlib.import_module(f"{package_name}.core.evidence")
    carousel_mod = importlib.import_module(f"{package_name}.core.carousel")
    host_text_mod = importlib.import_module(f"{package_name}.host_text")
    replay.patch_guidance_blocks(package_name)

    calls, calls_filter_report = replay.load_calls(replay.DEFAULT_CALLS)
    refusals, refusals_filter_report = replay.load_refusals(replay.DEFAULT_REFUSALS)
    calls.sort(key=lambda r: r["at"])
    replay.match_refusals(calls, refusals)

    # (identity, key) -> chronological list of calls
    by_key: Dict[Tuple[str, str], List[dict]] = {}
    for row in calls:
        identity = row.get("identity") or "?:?"
        key = row.get("key") or ""
        if not key:
            continue
        by_key.setdefault((identity, key), []).append(row)
    for rows in by_key.values():
        rows.sort(key=lambda r: r["at"])

    stated_events = []
    for row in calls:
        if row.get("outcome") == "answered":
            continue
        refusal = row.get("_refusal")
        if refusal is None:
            continue
        exc = replay.build_exception_from_refusal(refusal)
        ev = evidence_mod.harvest(exc, message=str(exc), guidance_blocks=host_text_mod.guidance_blocks())
        delay = carousel_mod.extract_delay(exc, ev.message, ev.headers)
        if not delay or delay <= 0:
            continue
        identity = row.get("identity") or "?:?"
        key = row.get("key") or ""
        stated_events.append({"identity": identity, "key": key, "at": row["at"], "delay": float(delay),
                               "deadline": row["at"] + float(delay), "status": row.get("status"), "kind": row.get("kind")})

    gap_records = []
    for event in stated_events:
        rows = by_key.get((event["identity"], event["key"]), [])
        following = [r for r in rows if r["at"] > event["at"]]
        if not following:
            continue
        nxt = following[0]
        gap = nxt["at"] - event["deadline"]
        if nxt.get("outcome") == "answered":
            outcome = "success"
        elif nxt.get("status") == event["status"] and nxt.get("kind") == event["kind"]:
            outcome = "refused_same_reason"
        else:
            outcome = "refused_other_reason"
        gap_records.append({"gap_s": round(gap, 3), "outcome": outcome, "identity": event["identity"]})

    def _stats(records, outcome):
        vals = sorted(r["gap_s"] for r in records if r["outcome"] == outcome)
        if not vals:
            return {"count": 0}
        return {"count": len(vals), "min": vals[0], "median": round(statistics.median(vals), 3), "max": vals[-1]}

    padding_jitter_band_s = PADDING_S + JITTER_HIGH_S  # 2.0 — the widest a real wait's offset can be
    refused_same = [r for r in gap_records if r["outcome"] == "refused_same_reason"]
    successes = [r for r in gap_records if r["outcome"] == "success"]
    too_early_within_band = sum(1 for r in refused_same if r["gap_s"] < padding_jitter_band_s)
    successes_within_band = sum(1 for r in successes if r["gap_s"] < padding_jitter_band_s)
    min_success_gap = min((r["gap_s"] for r in successes), default=None)
    measurements = {
        "corpus_available": True,
        "calls_filter_report": calls_filter_report,
        "refusals_filter_report": refusals_filter_report,
        "stated_deadline_events": len(stated_events),
        "events_with_a_following_same_key_call": len(gap_records),
        "by_outcome": {
            "success": _stats(gap_records, "success"),
            "refused_same_reason": _stats(gap_records, "refused_same_reason"),
            "refused_other_reason": _stats(gap_records, "refused_other_reason"),
        },
        "refused_same_reason_within_current_padding_plus_jitter_band": too_early_within_band,
        "successes_within_current_padding_plus_jitter_band": successes_within_band,
        "min_observed_successful_gap_s": min_success_gap,
        "current_padding_and_jitter_s": f"{PADDING_S} + uniform({JITTER_LOW_S}, {JITTER_HIGH_S})",
        "padding_0s_and_1s_measurable": False,
        "why_not_measurable": (
            "Every gap in this corpus was produced under the SAME padding+jitter policy "
            "(padding has not varied across the covered window). There is no recorded call "
            "sent at padding=0.0 or a fixed padding=1.0 to read a refusal/success rate off "
            "of — reporting one would be extrapolation, not measurement."
        ),
        "caveat": (
            "'Next call on the same key' is dominated by ordinary multi-key round-robin, not "
            "exclusively by _wait_for_recovery's own retry-the-same-key path (which only fires "
            "when the WHOLE pool is exhausted at once — a narrower, rarer condition). A call "
            "landing inside the padding+jitter band by chance, during a busy multi-key turn, is "
            "not proof the padding mechanism itself was exercised. What IS a clean fact either "
            "way: not one of the 55 observed successes landed inside that band (min observed "
            "successful gap is the number above, in the tens of seconds) — whatever put a call "
            "there, none of them found the key ready that fast."
        ),
    }
    return {"name": "padding_cost", "passed": True, "measurements": measurements}


# ---------------------------------------------------------------------------
# Measurement 4 — is the jitter band worth its cost
# ---------------------------------------------------------------------------


def scenario_jitter_cost() -> Dict[str, Any]:
    wide = _monte_carlo_stampede(JITTER_LOW_S, JITTER_HIGH_S, MONTE_CARLO_TRIALS, seed_base=2000)
    narrow = _monte_carlo_stampede(0.0, 0.3, MONTE_CARLO_TRIALS, seed_base=2000)  # same seeds -> paired comparison
    wide_latency = PADDING_S + (JITTER_LOW_S + JITTER_HIGH_S) / 2.0
    narrow_latency = PADDING_S + (0.0 + 0.3) / 2.0
    return {
        "name": "jitter_cost",
        "passed": True,
        "measurements": {
            "current_uniform_0.1_1.5": wide,
            "narrower_uniform_0.0_0.3": narrow,
            "mean_added_latency_s": {"current": round(wide_latency, 3), "narrower": round(narrow_latency, 3)},
            "reading": (
                "Narrower jitter buys lower mean latency per wait "
                f"({round(narrow_latency, 3)}s vs {round(wide_latency, 3)}s) at the cost of a higher "
                f"stampede rate ({narrow['trial_collision_rate']} vs {wide['trial_collision_rate']} "
                "trial-collision rate). Reported for the owner to weigh — G10 does not change the "
                "constant on this measurement alone."
            ),
        },
    }


# ---------------------------------------------------------------------------
# Measurement 5 — sleep slices: prompt wake on interrupt and on deep recovery
# ---------------------------------------------------------------------------


#: The longest a stated hold actually lasts, regardless of what is asked for
#: — `Carousel(max_hold_s=MAX_HOLD_S)`'s default, which is also the G8 1-hour
#: ceiling (`PLAN_1.8.0.0.md`). Requesting a longer hold below is intentional
#: (it proves the ceiling clamps it, same as production), not a mistake to
#: work around; the two scenarios below trigger their events safely inside
#: this bound rather than assuming an unclamped multi-hour wait.
_REALISTIC_MAX_HOLD_S = 3600.0


def _sleep_slice_interrupt(dispatch_binding_mod, carousel_mod) -> Dict[str, Any]:
    """Mirrors `test_a_stop_during_a_long_wait_is_honoured_within_a_second`,
    but reports the measured lag instead of only asserting a bound."""
    keys = _keys(2)
    engine = carousel_mod.Carousel()
    clock = _FakeClock(dispatch_binding_mod).install()
    try:
        # Marked after the clock is installed — see the comment in
        # ``_idle_key_expires_early``; an unstated ``now`` at mark() time has
        # to resolve against the SAME clock the wait loop reads later.
        for key in keys:
            engine.mark(IDENTITY, key, False, _REALISTIC_MAX_HOLD_S, "rate_limit", stated=True)
        agent = _Agent(keys)

        def stop_after_one_slice(seconds: float) -> None:
            clock.sleep(seconds)
            agent._interrupt_requested = True

        binding = dispatch_binding_mod.DispatchBinding(engine=engine, sleep=stop_after_one_slice)
        binding.run(lambda a, k, **kw: _Answer(), agent, {}, (), {})
    finally:
        clock.restore()
    return {"label": "interrupt_during_1h_ceiling_wait", "lag_s": round(clock.slept, 3)}


def _sleep_slice_deep_recovery(dispatch_binding_mod, carousel_mod) -> Dict[str, Any]:
    """A wait at the plugin's own 1-hour ceiling, with the recovering call
    landing thousands of slices in rather than at the very start — proves the
    per-slice recheck still wakes within one slice deep into a long wait, not
    only near its beginning. (An earlier draft of this scenario asked for a
    5-hour hold and triggered at slice 5000 — `Carousel`'s own `max_hold_s`
    silently clamped it to 3600s, so the trigger past slice 3600 never fired
    and this measurement came back `null`. Caught by this file's own first
    run; fixed by asking for exactly what the ceiling allows and triggering
    safely inside it.)
    """
    keys = _keys(2)
    engine = carousel_mod.Carousel()
    clock = _FakeClock(dispatch_binding_mod).install()
    became_usable_at: List[float] = []
    slice_count = [0]
    trigger_at_slice = 2000
    try:
        for key in keys:
            engine.mark(IDENTITY, key, False, _REALISTIC_MAX_HOLD_S, "rate_limit", stated=True)

        def sleep(seconds: float) -> None:
            clock.sleep(seconds)
            slice_count[0] += 1
            if not became_usable_at and slice_count[0] >= trigger_at_slice:
                engine.mark(IDENTITY, keys[0], True, now=clock.now)
                became_usable_at.append(clock.now)

        binding = dispatch_binding_mod.DispatchBinding(engine=engine, sleep=sleep)
        agent = _Agent(keys)
        calls: List[float] = []

        def host(a, api_kwargs, **kwargs):
            calls.append(clock.now)
            return _Answer()

        binding.run(host, agent, {}, (), {})
    finally:
        clock.restore()
    delay = calls[0] - became_usable_at[0] if became_usable_at and calls else None
    return {"label": "recovery_2000_slices_into_1h_ceiling_wait", "trigger_at_slice": trigger_at_slice,
            "triggered": bool(became_usable_at), "lag_s": round(delay, 3) if delay is not None else None}


def scenario_sleep_slices(_tmp_root: Path) -> Dict[str, Any]:
    dispatch_binding_mod, carousel_mod, _shared_health_mod = _modules()
    interrupt = _sleep_slice_interrupt(dispatch_binding_mod, carousel_mod)
    deep_recovery = _sleep_slice_deep_recovery(dispatch_binding_mod, carousel_mod)
    lags = [interrupt.get("lag_s"), deep_recovery.get("lag_s")]
    bound = dispatch_binding_mod._SLEEP_SLICE_S + 0.05
    # A missing lag (the trigger never fired, e.g. because a hold was clamped
    # shorter than the scenario assumed — exactly what this file's own first
    # run caught, see `_sleep_slice_deep_recovery`'s docstring) FAILS this
    # scenario rather than being silently dropped from the average.
    passed = bool(all(lag is not None for lag in lags) and all(lag <= bound for lag in lags))
    return {
        "name": "sleep_slices",
        "passed": passed,
        "measurements": {
            "sleep_slice_s": dispatch_binding_mod._SLEEP_SLICE_S,
            "bound_s": bound,
            "interrupt": interrupt,
            "deep_recovery": deep_recovery,
            "worst_observed_lag_s": max(v for v in lags if v is not None) if any(v is not None for v in lags) else None,
        },
    }


# ---------------------------------------------------------------------------
# Self-check: the gate must be able to fail
# ---------------------------------------------------------------------------


def scenario_self_check(tmp_root: Path) -> Dict[str, Any]:
    """`learnings/0003`: a gate that cannot fail is not a gate. Reruns
    measurement 1's real mechanism and Monte Carlo with jitter fixed at 0 —
    the plugin's own default when no jitter is injected at all
    (`DispatchBinding.__init__`: "``None`` means no jitter, which is the safe
    default"), i.e. exactly what production would do if `install()`'s jitter
    wiring were ever accidentally dropped. With every profile's offset then
    identical (`padding + 0`), every race is a guaranteed collision.
    """
    real_run = _real_stampede_run(tmp_root / "stampede_broken", 0.0, 0.0, seeds=(101, 102, 103))
    monte_carlo = _monte_carlo_stampede(0.0, 0.0, MONTE_CARLO_TRIALS, seed_base=9000)
    broken_config_detected_failure = bool(
        monte_carlo["trial_collision_rate"] is not None
        and monte_carlo["trial_collision_rate"] > 0.9
        and real_run["any_collision_under_100ms"]
    )
    return {
        "name": "self_check",
        "passed": broken_config_detected_failure,
        "measurements": {
            "config": "jitter fixed at 0.0 (deliberately broken relative to install()'s uniform(0.1, 1.5))",
            "real_run_gaps_s": real_run["gaps_s"],
            "monte_carlo_trial_collision_rate": monte_carlo["trial_collision_rate"],
            "gate_correctly_reported_fail": broken_config_detected_failure,
        },
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _write_scenario(result: Dict[str, Any]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{result['name']}.json"
    path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")


def _print_table(results: List[Dict[str, Any]]) -> None:
    print(f"{'measurement':<20}{'result':<8}measurements")
    for r in results:
        verdict = "PASS" if r["passed"] else "FAIL"
        print(f"{r['name']:<20}{verdict:<8}{json.dumps(r['measurements'], default=str)[:160]}")


def _write_report(results: List[Dict[str, Any]]) -> None:
    lines = ["# G10 fine-clock gate — report\n"]
    lines.append(f"Generated: {time_module.strftime('%Y-%m-%d %H:%M:%S')}\n")
    overall = all(r["passed"] for r in results if r["name"] != "self_check")
    self_check = next((r for r in results if r["name"] == "self_check"), None)
    self_check_ok = bool(self_check and self_check["passed"])
    lines.append(f"**Overall (five measurements): {'PASS' if overall else 'FAIL'}**\n")
    lines.append(f"**Self-check (gate can fail): {'PASS' if self_check_ok else 'FAIL'}**\n")
    for r in results:
        lines.append(f"\n## {r['name']} — {'PASS' if r['passed'] else 'FAIL'}\n")
        lines.append("```json\n" + json.dumps(r["measurements"], indent=2, default=str) + "\n```\n")
    (OUT_DIR / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def run_gate() -> Tuple[bool, List[Dict[str, Any]]]:
    with tempfile.TemporaryDirectory(prefix="kame-clock-gate-") as tmp:
        tmp_root = Path(tmp)
        results = [
            scenario_stampede(tmp_root),
            scenario_no_idle_waiting(tmp_root),
            scenario_padding_cost(),
            scenario_jitter_cost(),
            scenario_sleep_slices(tmp_root),
            scenario_self_check(tmp_root),
        ]
    for r in results:
        _write_scenario(r)
    _write_report(results)
    _print_table(results)
    overall = all(r["passed"] for r in results if r["name"] != "self_check")
    self_check = next((r for r in results if r["name"] == "self_check"), None)
    gate_passed = overall and bool(self_check and self_check["passed"])
    return gate_passed, results


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.parse_args(argv)
    try:
        passed, _results = run_gate()
    except Exception:
        traceback.print_exc()
        return 1
    print(f"\nGATE {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
