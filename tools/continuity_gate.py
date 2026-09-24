"""G4 — the continuity gate for `decisions/0006-1800-one-pool-three-profiles.md`.

The owner's goal, in one sentence (`PLAN_1.8.0.0.md`, G4): the agent never
stops while any key anywhere still has quota, it comes back on its own when
quota returns, and he never has to reset the pool by hand. `share_pool_health`
is the mechanism that is supposed to deliver that across his three real
profiles (`base`, `profiles/k`, `profiles/lo1`) sharing one physical pool of
seventeen keys. `tests/test_v1_8_0_0_shared_health.py` proves the file format
and the reconciliation rule in-process, with threads standing in for
processes — its own header says so: "a genuine multi-process run was not
executed, for CI speed and reliability." This module is that run.

Six scenarios, each a real subprocess tree (real `python` interpreters, real
PIDs, real file locks, and on scenario 3 a real `Popen.kill()` — SIGKILL on
POSIX, `TerminateProcess` on Windows) — never threads standing in for
processes, because a thread cannot orphan a lock file the way a killed
process can:

1. Three real OS processes (one simulated profile each) driving the real
   `core.carousel.Carousel` over the SAME 17 simulated fingerprints against a
   scripted, deterministic refusal timetable.
2. The double-burn count: how many times a key one process was JUST refused
   on gets called again by ANOTHER process while that refusal's hold is still
   active — measured with sharing off (today's baseline) and on.
3. A stale lock: kill a process mid-hold, prove another process reclaims the
   write lock and keeps going, and time the reclaim.
4. A restart: a fresh process over the same file honours a live hold, ignores
   an expired one, and never lets a stale/inflated hold outlive the ceiling
   once the key is touched again.
5. Pool sizes 1, 2, 14 and 17: storm every key into a hold, release quota at a
   scripted moment, measure the gap to the first success — no manual reset,
   ever, including the one-key case.
6. Never idle with a key available — instrumented into every scenario above
   rather than run separately, because it is a property of every turn any
   driver takes, not of one more contrived timeline.

Plus the rule `learnings/0003-the-gate-that-was-never-run.md` exists to
enforce: a gate that cannot fail is not a gate. `--self-check` reruns
scenario 1's bench-visibility assertion with sharing deliberately OFF — the
"broken configuration" the scenario requires to hold — and the run FAILS
this file's own build if that check does not come back FAIL.

Every number this file measures is printed before any verdict that depends on
it — see the per-scenario JSON under `research/1.8.0.0/continuity/` and the
`REPORT.md` this run writes beside them.

Isolation: every subprocess gets its own `HERMES_HOME` under one throwaway
temp root (never `%LOCALAPPDATA%\\...` — see `_assert_appdata_
untouched`, called before and after every run), the two record switches are
disabled the same way `tests/conftest.py` disables them for the rest of this
suite, and nothing here makes a network call. Timings are real wall-clock
scaled down (a scripted hold is ~1.5s here, not the production 3600s
ceiling) so a full run finishes in well under a minute; the mechanism under
test — `select`/`mark`/`SharedHealth.record`/the file lock — is exactly the
production code, unmodified, imported straight from `hermes-kame-api-
rotation/`.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "hermes-kame-api-rotation"
# 1.8.1.2: the test suite points this at a temp dir (tests/conftest.py), so
# running the tests no longer rewrites the committed 1.8.0.0 evidence.
OUT_DIR = (Path(os.environ["KAME_GATE_OUT_DIR"]) / "continuity" if os.environ.get("KAME_GATE_OUT_DIR")
           else ROOT / "research" / "1.8.0.0" / "continuity")
PACKAGE = "kame_continuity_gate_under_test"

#: The one real directory this gate must never touch, whatever else goes
#: wrong. Hard-coded rather than derived, so a bug in path derivation cannot
#: quietly compute its way into the owner's real install — see CLAUDE.md
#: "before touching Hermes" and `_assert_appdata_untouched` below.
REAL_HERMES_APPDATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "hermes"

# --- scaled-down but real timing -------------------------------------------
# A scripted hold here is measured in seconds, not the production 3600s
# ceiling (`carousel.MAX_HOLD_S`) — the mechanism is identical either way
# (`RL_BASE_S = 1.0`, `applied = max(delay, RL_BASE_S)` for a stated
# `rate_limit`, verified against `hermes-kame-api-rotation/core/carousel.py`
# before this file was written), only the numbers are shrunk so a real
# process tree finishes in seconds rather than an hour.
HOLD_S = 1.5
MAX_HOLD_S = 6.0
DAILY_COOLDOWN_S = 3.0
TURN_SLEEP_S = 0.10
NUM_TURNS = 24
REFUSE_WINDOW_S = 1.2
KEY_COUNT = 17
IDENTITY = "sim:gate-model"
POOL_SIZES = (1, 2, 14, 17)

#: Scenario 2. One storm's double-burn count is not a stable measurement
#: under load — see `scenario_double_burn`'s own docstring for the stress
#: run that proved it. Seven interleaved off/on pairs, compared by median,
#: measured to hold the real effect (a several-fold reduction) even against
#: eight CPU-bound busy processes on an 8-core machine.
DOUBLE_BURN_REPEATS = 7

PROFILE_LABELS = ("base", "k", "lo1")


def _keys(n: int) -> List[str]:
    return [f"sim-key-{i:02d}" for i in range(n)]


# --- loading the real plugin -------------------------------------------------

def _load_plugin():
    """Import `hermes-kame-api-rotation/` as a package, once per process.

    Same recipe `tests/test_v1_8_0_0_shared_health.py::_load_package` uses —
    a worker subprocess is a fresh interpreter, so there is never a second
    load to guard against here; the guard is kept anyway for the orchestrator
    process, which imports this module's own helpers in-process for
    scenario 5.
    """
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
    carousel = importlib.import_module(f"{PACKAGE}.core.carousel")
    shared_health = importlib.import_module(f"{PACKAGE}.core.shared_health")
    quota = importlib.import_module(f"{PACKAGE}.core.quota")
    return carousel, shared_health, quota


# --- AppData safety -----------------------------------------------------------

def _appdata_snapshot() -> Dict[str, float]:
    """`{path: mtime}` for the ONE file this plugin's real install could
    possibly be affected by — the shared pool-health file and its lock,
    under the real Hermes AppData root — plus every `HERMES_HOME` derivation
    input this file could conceivably resolve to by accident.

    Deliberately NOT a snapshot of the whole AppData tree: the owner's real
    Hermes is frequently running while this gate does (measured directly —
    a first draft using the whole tree flagged `cron/ticker_heartbeat`,
    `kanban.db-shm` and the plugin's own `state.json`, none of which this
    gate's code path can reach, all of which the live host legitimately
    writes on its own clock). A whole-tree diff would report a false
    positive on every run with Hermes open, which teaches "ignore this
    check" — exactly the habit `learnings/0003` warns against. Scoped to the
    one artifact this module (`core.shared_health`) is capable of writing —
    `pool-health.json` and its `.lock` — under the plugin's OWN data
    directory in the real install, which is the actual risk this gate exists
    to rule out: every `HERMES_HOME` used anywhere in this file is an
    explicit temp path (see `_env_for`/`_new_shared_root`), never derived
    from the real environment, so this file should never resolve to the real
    root at all; this snapshot is the belt-and-suspenders proof of that.
    """
    watched = [
        REAL_HERMES_APPDATA / "plugin-data" / "hermes-kame-api-rotation" / "pool-health.json",
        REAL_HERMES_APPDATA / "plugin-data" / "hermes-kame-api-rotation" / "pool-health.json.lock",
    ]
    out: Dict[str, float] = {}
    for path in watched:
        try:
            out[str(path)] = path.stat().st_mtime
        except OSError:
            continue
    return out


def _assert_appdata_untouched(before: Dict[str, float]) -> Tuple[bool, str]:
    after = _appdata_snapshot()
    if after == before:
        return True, "unchanged"
    added = sorted(set(after) - set(before))
    changed = sorted(p for p in after.keys() & before.keys() if after[p] != before[p])
    removed = sorted(set(before) - set(after))
    detail = f"added={added[:5]} changed={changed[:5]} removed={removed[:5]}"
    return False, detail


# --- one turn, shared by every worker mode -----------------------------------

def _env_for(spec: Dict[str, Any]) -> None:
    os.environ["HERMES_HOME"] = spec["home"]
    os.environ["KAME_RECORDER_DISABLED"] = "1"
    os.environ["KAME_CALL_TIMINGS_DISABLED"] = "1"
    if spec.get("share_pool_health"):
        os.environ["KAME_SHARE_POOL_HEALTH"] = "1"
    else:
        os.environ.pop("KAME_SHARE_POOL_HEALTH", None)
    override = spec.get("pool_health_path_override")
    if override:
        os.environ["KAME_POOL_HEALTH_PATH"] = override
    else:
        os.environ.pop("KAME_POOL_HEALTH_PATH", None)


def _turn(carousel, identity: str, keys: Sequence[str], now: float, refused: bool, hold_s: float) -> Dict[str, Any]:
    """One select()+mark() turn. Returns the event this turn produced.

    `idle_violation` is scenario 6's instrumentation: `select` and
    `healthy_count` are called with the SAME `now` on purpose — calling
    `time.time()` twice for the two checks would let a key's deadline cross
    `now` between them and manufacture a false positive that has nothing to
    do with the driver's own behaviour.
    """
    key, status = carousel.select(identity, keys, now)
    healthy = carousel.healthy_count(identity, keys, now)
    idle_violation = status == "EXHAUSTED" and healthy > 0
    if key is None:
        return {
            "t": now, "key": None, "status": status, "refused": None,
            "idle_violation": idle_violation, "healthy_count": healthy,
        }
    if refused:
        carousel.mark(identity, key, ok=False, delay=hold_s, kind="rate_limit", now=now, stated=True)
    else:
        carousel.mark(identity, key, ok=True, now=now)
    return {
        "t": now, "key": key, "status": status, "refused": bool(refused),
        "idle_violation": idle_violation, "healthy_count": healthy,
    }


# --- worker: driver (scenarios 1, 2, 4) --------------------------------------

def _worker_driver(spec: Dict[str, Any]) -> None:
    _env_for(spec)
    carousel_mod, _shared_health_mod, _quota_mod = _modules()
    carousel = carousel_mod.Carousel(
        daily_cooldown_s=float(spec["daily_cooldown_s"]), max_hold_s=float(spec["max_hold_s"])
    )
    identity = spec["identity"]
    keys = spec["keys"]
    start_epoch = float(spec["start_epoch"])
    refuse_window_s = float(spec["refuse_window_s"])
    hold_s = float(spec["hold_s"])
    events: List[Dict[str, Any]] = []
    for _turn_index in range(int(spec["num_turns"])):
        now = time.time()
        refused = (now - start_epoch) < refuse_window_s
        event = _turn(carousel, identity, keys, now, refused, hold_s)
        event["profile"] = spec["profile_label"]
        event["pid"] = os.getpid()
        events.append(event)
        time.sleep(float(spec["sleep_s"]))
    Path(spec["out"]).write_text(
        "\n".join(json.dumps(e) for e in events) + ("\n" if events else ""), encoding="utf-8"
    )


def _worker_one_shot(spec: Dict[str, Any]) -> None:
    """A single select()+mark() call, then exit. Used by scenario 4's restart
    steps and the "release by success" check — a fresh, short-lived process
    each time, which is the whole point: the process exits between steps, so
    nothing but the shared file carries anything from one step to the next.
    """
    _env_for(spec)
    carousel_mod, _shared_health_mod, _quota_mod = _modules()
    carousel = carousel_mod.Carousel(
        daily_cooldown_s=float(spec["daily_cooldown_s"]), max_hold_s=float(spec["max_hold_s"])
    )
    identity = spec["identity"]
    keys = spec["keys"]
    now = time.time()
    key, status = carousel.select(identity, keys, now)
    result: Dict[str, Any] = {"t": now, "key": key, "status": status, "pid": os.getpid()}
    action = spec.get("action")
    if action == "select_only":
        pass
    elif action == "refuse":
        carousel.mark(
            identity, key, ok=False, delay=float(spec["hold_s"]), kind="rate_limit", now=now, stated=True
        )
        result["marked"] = "refuse"
    elif action == "succeed":
        carousel.mark(identity, key, ok=True, now=now)
        result["marked"] = "succeed"
    Path(spec["out"]).write_text(json.dumps(result), encoding="utf-8")


def _worker_lockholder(spec: Dict[str, Any]) -> None:
    """Take the real shared-health write lock and hold it until killed.

    Uses the exact primitive `SharedHealth._write_locked` uses
    (`shared_health._acquire_file_lock`) against the real derived path, so
    the lock file this orphans on SIGKILL/TerminateProcess is byte-for-byte
    what a real crash mid-write would leave behind.
    """
    _env_for(spec)
    _carousel_mod, shared_health_mod, _quota_mod = _modules()
    path = shared_health_mod.default_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    acquired = shared_health_mod._acquire_file_lock(lock_path)
    # Atomic write (temp file + os.replace), same reason `shared_health.
    # _write_document` uses the pattern: a reader polling `.exists()` must
    # never observe a half-written marker — a plain `write_text` raced the
    # orchestrator's poll loop often enough to be worth naming here.
    marker_path = Path(spec["ready_marker"])
    tmp_marker = marker_path.with_suffix(marker_path.suffix + ".tmp")
    tmp_marker.write_text(json.dumps({"acquired": acquired, "pid": os.getpid()}), encoding="utf-8")
    os.replace(tmp_marker, marker_path)
    if not acquired:
        return
    while True:
        time.sleep(0.05)


def main_worker(argv: Sequence[str]) -> int:
    spec = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    try:
        mode = spec["mode"]
        if mode == "driver":
            _worker_driver(spec)
        elif mode == "one_shot":
            _worker_one_shot(spec)
        elif mode == "lockholder":
            _worker_lockholder(spec)
        else:
            raise ValueError(f"unknown worker mode {mode!r}")
        return 0
    except Exception:
        sys.stderr.write(traceback.format_exc())
        return 1


# --- orchestrator plumbing ---------------------------------------------------

def _new_shared_root(tmp_root: Path) -> Dict[str, Path]:
    """`{profile_label: HERMES_HOME}` matching the owner's real layout —
    `base` IS the shared root, `k`/`lo1` sit under `profiles/<name>` two
    levels below it (`core.shared_health._root_and_profile`), so all three
    derive the SAME `default_path()`.
    """
    base = tmp_root / "home"
    base.mkdir(parents=True, exist_ok=True)
    k = base / "profiles" / "k"
    lo1 = base / "profiles" / "lo1"
    k.mkdir(parents=True, exist_ok=True)
    lo1.mkdir(parents=True, exist_ok=True)
    return {"base": base, "k": k, "lo1": lo1}


def _spawn(spec_path: Path, spec: Dict[str, Any]) -> subprocess.Popen:
    spec_path.write_text(json.dumps(spec), encoding="utf-8")
    return subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--worker", str(spec_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _drain(proc: subprocess.Popen, timeout: float) -> Tuple[int, str, str]:
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
    return proc.returncode, out, err


def _load_events(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return events


def _validate_pool_health_json(path: Path) -> Tuple[bool, str]:
    if not path.exists():
        return True, "no file yet (nothing shared)"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return False, f"corrupt JSON: {exc}"
    if not isinstance(document, dict) or "schema" not in document:
        return False, "not the expected document shape"
    return True, "valid"


# --- scenario 1 + 2: three real processes, one pool; double-burn count ------

def _count_double_burns(
    events: List[Dict[str, Any]], hold_s: float, *, while_exhausted: bool = False
) -> int:
    """How many times a key one profile was JUST refused on is selected and
    refused again by a DIFFERENT profile while that hold should still be
    active. See the module docstring, scenario 2.

    A turn whose ``select`` said ``"EXHAUSTED"`` is not counted (1.8.1.3).
    That status is the carousel saying *every key is resting and this one is
    merely the soonest back* -- the production caller waits for it rather
    than spending the call, and no amount of sharing can make a key that is
    known to be resting look healthy. The driver spends it anyway, and on a
    machine whose three workers drain all seventeen keys inside the refuse
    window those turns were nearly every burn left with sharing ON: measured
    on Linux, 143 of 144 sharing-ON burns were on EXHAUSTED turns (1 on a
    turn the carousel called healthy), against 102 healthy-turn burns with
    sharing OFF. Counting them made the ON median sit within noise of OFF
    (``on=[52, 53, 55, 55, 55, 55, 51]`` vs ``off=55`` failed the gate one
    run in a few) while measuring the simulator, not the plugin.
    ``while_exhausted=True`` counts exactly those turns instead, for the
    diagnostic the scenario still reports.
    """
    events = sorted(events, key=lambda e: e["t"])
    last_refusal: Dict[str, Tuple[str, float]] = {}
    burns = 0
    for event in events:
        if not event.get("refused") or event.get("key") is None:
            continue
        key = event["key"]
        profile = event["profile"]
        t = event["t"]
        prior = last_refusal.get(key)
        exhausted = event.get("status") == "EXHAUSTED"
        if (
            prior is not None
            and prior[0] != profile
            and t < prior[1] + hold_s
            and exhausted == while_exhausted
        ):
            burns += 1
        last_refusal[key] = (profile, t)
    return burns


def _run_three_process_storm(
    tmp_root: Path,
    share_pool_health: bool,
    tag: str,
    *,
    hold_s: float = HOLD_S,
    refuse_window_s: float = REFUSE_WINDOW_S,
    num_turns: int = NUM_TURNS,
    head_start_s: float = 0.6,
) -> Dict[str, Any]:
    """Spawn the three simulated profiles and let them storm the same pool.

    ``head_start_s`` — 1.8.0.1: raised from a flat 0.15s after a real
    inversion was reproduced under heavy CPU contention (eight busy
    processes on an 8-core machine — see `scenario_double_burn`'s own
    docstring): a `start_epoch` set before `Popen` returns assumes every
    worker finishes launching Python and importing the plugin package
    BEFORE that instant arrives, and under real starvation that assumption
    can simply be false, which reads as "the storm produced almost nothing"
    rather than as the timing bug it is. ``hold_s``/``refuse_window_s``/
    ``num_turns`` are parameters (not only module constants) so
    `scenario_double_burn` can ask for a wider absolute margin without
    changing what scenario 1 and the self-check already measured.
    """
    homes = _new_shared_root(tmp_root / tag)
    keys = _keys(KEY_COUNT)
    start_epoch = time.time() + head_start_s
    specs_dir = tmp_root / tag / "specs"
    specs_dir.mkdir(parents=True, exist_ok=True)
    procs = []
    outs = []
    for label in PROFILE_LABELS:
        out_path = specs_dir / f"{label}.events.jsonl"
        spec = {
            "mode": "driver",
            "home": str(homes[label]),
            "share_pool_health": share_pool_health,
            "identity": IDENTITY,
            "keys": keys,
            "hold_s": hold_s,
            "max_hold_s": MAX_HOLD_S,
            "daily_cooldown_s": DAILY_COOLDOWN_S,
            "num_turns": num_turns,
            "sleep_s": TURN_SLEEP_S,
            "start_epoch": start_epoch,
            "refuse_window_s": refuse_window_s,
            "profile_label": label,
            "out": str(out_path),
        }
        procs.append(_spawn(specs_dir / f"{label}.spec.json", spec))
        outs.append(out_path)
    results = [_drain(p, timeout=60.0) for p in procs]
    errors = [err for (_code, _out, err) in results if err.strip()]
    nonzero = [code for (code, _out, _err) in results if code != 0]
    events: List[Dict[str, Any]] = []
    for out_path in outs:
        events.extend(_load_events(out_path))
    pool_health_path = homes["base"] / "plugin-data" / "hermes-kame-api-rotation" / "pool-health.json"
    file_ok, file_detail = _validate_pool_health_json(pool_health_path)
    double_burns = _count_double_burns(events, hold_s)
    exhausted_turn_burns = _count_double_burns(events, hold_s, while_exhausted=True)
    idle_violations = sum(1 for e in events if e.get("idle_violation"))
    return {
        "events": events,
        "errors": errors,
        "nonzero_exit": nonzero,
        "file_ok": file_ok,
        "file_detail": file_detail,
        "double_burns": double_burns,
        "exhausted_turn_burns": exhausted_turn_burns,
        "idle_violations": idle_violations,
        "homes": {k: str(v) for k, v in homes.items()},
        "pool_health_path": str(pool_health_path),
    }


def _check_release_by_success(tmp_root: Path) -> Dict[str, Any]:
    """The explicit decisions/0006 promise, isolated from storm noise:

    A: refuses one key with a LONG hold, exits.
    B: fresh process, sees the same key as held (visibility).
    C: fresh process, marks the SAME key a SUCCESS, exits.
    D: fresh process, sees the key as healthy again (release by success) —
       even though A's hold would not have naturally expired yet.
    """
    homes = _new_shared_root(tmp_root / "release")
    specs_dir = tmp_root / "release" / "specs"
    specs_dir.mkdir(parents=True, exist_ok=True)
    key = "sim-key-00"
    long_hold = HOLD_S * 20  # far longer than the gap between these four steps

    def _one_shot(label: str, home: Path, action: str, tag: str) -> Dict[str, Any]:
        out_path = specs_dir / f"{tag}.json"
        spec = {
            "mode": "one_shot", "home": str(home), "share_pool_health": True,
            "identity": IDENTITY, "keys": [key], "hold_s": long_hold,
            "max_hold_s": MAX_HOLD_S, "daily_cooldown_s": DAILY_COOLDOWN_S,
            "action": action, "out": str(out_path), "profile_label": label,
        }
        proc = _spawn(specs_dir / f"{tag}.spec.json", spec)
        code, _out, err = _drain(proc, timeout=30.0)
        result = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else {}
        result["exit_code"] = code
        result["stderr"] = err
        return result

    a = _one_shot("base", homes["base"], "refuse", "a_refuse")
    b = _one_shot("k", homes["k"], "select_only", "b_select")
    time.sleep(0.05)
    c = _one_shot("lo1", homes["lo1"], "succeed", "c_succeed")
    time.sleep(0.05)
    d = _one_shot("base", homes["base"], "select_only", "d_select")

    visible = b.get("status") == "EXHAUSTED"
    released = d.get("status") == "SUCCESS"
    return {
        "a": a, "b": b, "c": c, "d": d,
        "visible_across_processes": visible,
        "released_by_success": released,
        "passed": visible and released and not any(r.get("exit_code") for r in (a, b, c, d)),
    }


def scenario_three_processes(tmp_root: Path) -> Dict[str, Any]:
    before = _appdata_snapshot()
    on = _run_three_process_storm(tmp_root, share_pool_health=True, tag="storm_on")
    release = _check_release_by_success(tmp_root)
    appdata_ok, appdata_detail = _assert_appdata_untouched(before)
    passed = (
        not on["errors"] and not on["nonzero_exit"] and on["file_ok"]
        and release["passed"] and appdata_ok
    )
    result = {
        "name": "three_processes",
        "passed": passed,
        "measurements": {
            # RED_TEAM.md F11: this is ONE un-repeated storm, and
            # `scenario_double_burn`'s own docstring measured single repeats
            # inverting under CPU contention (`off=0, on=1` was observed
            # directly) purely from scheduling jitter. A raw count from one
            # run is therefore not a number this scenario can safely gate
            # on -- asserting a bound here would just import that same
            # jitter into a pass/fail signal, which is the exact mistake
            # `scenario_double_burn` exists to avoid by repeating and
            # comparing medians instead. Recorded here for the same reason
            # `idle_violations` below is: visible in the report for a human
            # to look at, never compared to a threshold. The authoritative,
            # asserted number for "does sharing reduce double burns" is
            # `scenario_double_burn`'s `off`/`on` medians -- that is the
            # only number a release note may quote for this claim.
            "double_burns_with_sharing_on_single_run_diagnostic_only": on["double_burns"],
            "idle_violations": on["idle_violations"],
            "process_errors": on["errors"],
            "process_nonzero_exit": on["nonzero_exit"],
            "pool_health_file": {"ok": on["file_ok"], "detail": on["file_detail"]},
            "release_by_success": release,
            "appdata_untouched": {"ok": appdata_ok, "detail": appdata_detail},
        },
    }
    return result


#: Wider than the shared module defaults, only for `scenario_double_burn`'s
#: own storms. Chosen after the extreme-contention repro (eight busy
#: processes, an 8-core machine) still produced a handful of runs with ZERO
#: refused calls on the plain `HOLD_S`/`REFUSE_WINDOW_S`/head-start values —
#: a worker whose Python startup and plugin import alone outrun a 0.15s
#: head start and a 1.2s refuse window under real starvation. These leave
#: roughly 2x the absolute margin without changing the RATIO between hold,
#: refuse window and turn spacing that the rest of the scenario relies on.
_BURN_HOLD_S = 3.0
_BURN_REFUSE_WINDOW_S = 2.5
_BURN_HEAD_START_S = 1.2
#: A repeat is retried (not counted) up to this many extra times if it
#: produced zero refused calls on either side — see `_degenerate_retry`.
_BURN_MAX_DEGENERATE_RETRIES = 2


def _degenerate_retry(tmp_root: Path, share_pool_health: bool, tag: str) -> Dict[str, Any]:
    """One storm, retried (bounded) if it measured nothing at all.

    A storm where not one call landed inside the refuse window is not
    evidence about sharing — every process finished its turns believing
    quota was already open, so `double_burns == 0` there means "the
    experiment did not run", not "sharing worked perfectly". Real process
    errors are never retried away: an errored run is returned immediately so
    `scenario_double_burn` still reports and fails on it.
    """
    run: Dict[str, Any] = {}
    for attempt in range(_BURN_MAX_DEGENERATE_RETRIES + 1):
        run = _run_three_process_storm(
            tmp_root, share_pool_health=share_pool_health, tag=f"{tag}_a{attempt}",
            hold_s=_BURN_HOLD_S, refuse_window_s=_BURN_REFUSE_WINDOW_S, head_start_s=_BURN_HEAD_START_S,
        )
        refused_count = sum(1 for e in run["events"] if e.get("refused"))
        if refused_count > 0 or run["errors"]:
            run["_retries"] = attempt
            return run
    run["_retries"] = _BURN_MAX_DEGENERATE_RETRIES
    return run


def _sample_stats(samples: List[float]) -> Dict[str, float]:
    return {
        "n": len(samples),
        "median": statistics.median(samples),
        "mean": statistics.mean(samples),
        "min": min(samples),
        "max": max(samples),
        "stdev": statistics.stdev(samples) if len(samples) > 1 else 0.0,
        "samples": samples,
    }


def scenario_double_burn(tmp_root: Path) -> Dict[str, Any]:
    """OFF vs ON, on the median of :data:`DOUBLE_BURN_REPEATS` independent
    storms, not on one storm's raw count.

    A single storm is not a stable instrument: under full-suite CPU
    contention (measured directly — twelve repeats run against eight
    CPU-bound busy processes, before this changed) individual repeats swung
    from `off=10, on=4` to `off=0, on=1` — three of twelve single repeats
    would have reported ON *higher* than OFF, purely from process scheduling
    jitter, on a machine with real spare cores. The effect this scenario
    exists to measure (sharing reduces cross-process double-burns) is real
    and large — the SAME stress run's medians were `off=7.5, on=1.0` — the
    single-repeat comparison was measuring the machine's mood, not the
    plugin. Repeating and comparing medians is the fix `PLAN_1.8.0.0.md`'s
    own gate rule implies: report the number, not a coin flip dressed as one.

    Off and on repeats are interleaved (off, on, off, on, ...) rather than
    run as two separate blocks, so a load trend across the whole scenario's
    runtime (a background process ramping up or winding down) cannot bias
    one side more than the other.
    """
    off_samples: List[int] = []
    on_samples: List[int] = []
    exhausted_off: List[int] = []
    exhausted_on: List[int] = []
    errors: List[str] = []
    degenerate_retries = 0
    for i in range(DOUBLE_BURN_REPEATS):
        # Wider absolute margins than scenario 1's storm — see
        # `_run_three_process_storm`'s own docstring for the timing bug this
        # closes: with the plain defaults, extreme CPU starvation can delay
        # a worker's own startup past the whole refuse window, so its run
        # sees NO refusals at all and reads as "sharing achieved nothing"
        # rather than "this repeat never really ran". Retried, bounded,
        # rather than counted: a repeat that produced zero refused calls on
        # EITHER side measured nothing, and a 0 in that case is a timing
        # artifact, not a data point about the plugin.
        off_run = _degenerate_retry(tmp_root, share_pool_health=False, tag=f"storm_off_{i}")
        on_run = _degenerate_retry(tmp_root, share_pool_health=True, tag=f"storm_on_{i}")
        degenerate_retries += off_run.pop("_retries", 0) + on_run.pop("_retries", 0)
        off_samples.append(off_run["double_burns"])
        on_samples.append(on_run["double_burns"])
        exhausted_off.append(off_run.get("exhausted_turn_burns", 0))
        exhausted_on.append(on_run.get("exhausted_turn_burns", 0))
        errors.extend(off_run["errors"])
        errors.extend(on_run["errors"])

    off_stats = _sample_stats(off_samples)
    on_stats = _sample_stats(on_samples)
    # The strict numeric requirement lives here, not in the pytest wrapper —
    # see the module docstring's rule from `learnings/0003`. Median rather
    # than mean or "every repeat individually": the measured spread above
    # shows individual repeats DO invert under contention while the median
    # of seven does not, and a single outlier storm must not decide the
    # verdict either way.
    passed = not errors and on_stats["median"] < off_stats["median"]
    return {
        "name": "double_burn",
        "passed": passed,
        "measurements": {
            "repeats": DOUBLE_BURN_REPEATS,
            "off": off_stats,
            "on": on_stats,
            "sharing_off_is_baseline": True,
            "sharing_on_median_must_be_lower": passed,
            # Diagnostic only, never part of the verdict: burns on turns the
            # carousel itself called EXHAUSTED -- see _count_double_burns.
            "exhausted_turn_burns_diagnostic_only": {
                "off": exhausted_off,
                "on": exhausted_on,
            },
            "process_errors": errors,
            "degenerate_repeats_retried": degenerate_retries,
            "timing": {
                "hold_s": _BURN_HOLD_S,
                "refuse_window_s": _BURN_REFUSE_WINDOW_S,
                "head_start_s": _BURN_HEAD_START_S,
            },
        },
    }


# --- scenario 3: stale lock ---------------------------------------------------

def scenario_stale_lock(tmp_root: Path) -> Dict[str, Any]:
    homes = _new_shared_root(tmp_root / "stale_lock")
    specs_dir = tmp_root / "stale_lock" / "specs"
    specs_dir.mkdir(parents=True, exist_ok=True)
    ready_marker = specs_dir / "ready.json"
    spec = {
        "mode": "lockholder", "home": str(homes["base"]), "share_pool_health": True,
        "ready_marker": str(ready_marker),
    }
    proc = _spawn(specs_dir / "lockholder.spec.json", spec)

    deadline = time.time() + 10.0
    while not ready_marker.exists() and time.time() < deadline:
        time.sleep(0.02)
    marker_seen = ready_marker.exists()
    marker = json.loads(ready_marker.read_text(encoding="utf-8")) if marker_seen else {}

    kill_t = time.time()
    proc.kill()
    proc.wait(timeout=10.0)

    carousel_mod, shared_health_mod, _quota_mod = _modules()
    os.environ["HERMES_HOME"] = str(homes["base"])
    path = shared_health_mod.default_path()
    store = shared_health_mod.SharedHealth(path=path, profile="recover", enabled_fn=lambda: True)

    reclaimed_at = None
    written_ok = False
    deadline = time.time() + 12.0
    while time.time() < deadline:
        now = time.time()
        store.record(scope="model", subject=IDENTITY, fingerprint_key="stale-lock-probe", until=now + 1.0, kind="rate_limit", at=now)
        until, at = store.model_entry(IDENTITY, "stale-lock-probe", now=now + 0.01)
        if until > 0.0 and at > 0.0:
            reclaimed_at = time.time()
            written_ok = True
            break
        time.sleep(0.1)

    file_ok, file_detail = _validate_pool_health_json(path)
    lock_path = path.with_name(path.name + ".lock")
    lock_still_present = lock_path.exists()
    if lock_still_present:
        try:
            lock_path.unlink()
        except OSError:
            pass

    reclaim_s = (reclaimed_at - kill_t) if reclaimed_at else None
    passed = bool(
        marker.get("acquired") and written_ok and file_ok and not lock_still_present and reclaim_s is not None
    )
    return {
        "name": "stale_lock",
        "passed": passed,
        "measurements": {
            "lock_acquired_by_victim": marker.get("acquired"),
            "reclaim_seconds": reclaim_s,
            "write_after_kill_succeeded": written_ok,
            "pool_health_file": {"ok": file_ok, "detail": file_detail},
            "lock_file_left_behind_after_recovery": lock_still_present,
        },
    }


# --- scenario 4: restart -----------------------------------------------------

def scenario_restart(tmp_root: Path) -> Dict[str, Any]:
    homes = _new_shared_root(tmp_root / "restart")
    specs_dir = tmp_root / "restart" / "specs"
    specs_dir.mkdir(parents=True, exist_ok=True)
    key = "sim-key-00"
    restart_hold = HOLD_S * 2

    def _one_shot(home: Path, action: str, tag: str, keys=(key,)) -> Dict[str, Any]:
        out_path = specs_dir / f"{tag}.json"
        spec = {
            "mode": "one_shot", "home": str(home), "share_pool_health": True,
            "identity": IDENTITY, "keys": list(keys), "hold_s": restart_hold,
            "max_hold_s": MAX_HOLD_S, "daily_cooldown_s": DAILY_COOLDOWN_S,
            "action": action, "out": str(out_path), "profile_label": "base",
        }
        proc = _spawn(specs_dir / f"{tag}.spec.json", spec)
        code, _out, err = _drain(proc, timeout=30.0)
        result = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else {}
        result["exit_code"] = code
        result["stderr"] = err
        return result

    # Process 1: refuse the key with a real hold, then exit for real.
    p1 = _one_shot(homes["base"], "refuse", "p1_refuse")

    # Process 2, brand new, starts immediately: the live hold must still be
    # honoured even though process 1's own memory is gone.
    p2 = _one_shot(homes["k"], "select_only", "p2_live")
    live_honoured = p2.get("status") == "EXHAUSTED"

    # Wait the hold out, then a THIRD fresh process: the now-expired hold
    # must not still be honoured.
    time.sleep(restart_hold + 0.3)
    p3 = _one_shot(homes["lo1"], "select_only", "p3_expired")
    expired_not_honoured = p3.get("status") == "SUCCESS"

    # Ceiling across a restart: seed the shared file directly with an
    # inflated `until` for a second key (as if written by a build with a
    # looser ceiling, or simply stale/adversarial data), then confirm a
    # fresh process that TOUCHES that key again never stores a local
    # sick_until beyond ITS OWN max_hold_s — the guarantee carousel.mark's
    # own comment documents ("Trimming it here, on whatever refusal happens
    # to call mark next ... is what makes the ceiling a real ceiling").
    carousel_mod, shared_health_mod, quota_mod = _modules()
    os.environ["HERMES_HOME"] = str(homes["base"])
    # The dormant-read half below builds a Carousel with the DEFAULT
    # (environment-derived) SharedHealth, exactly like production — so the
    # switch has to be genuinely on in this process's own environment, not
    # only passed to the explicit `seed_store` instance further down.
    os.environ["KAME_SHARE_POOL_HEALTH"] = "1"
    path = shared_health_mod.default_path()
    inflated_key = "sim-key-01"
    now = time.time()
    inflated_until = now + MAX_HOLD_S * 100
    seed_store = shared_health_mod.SharedHealth(path=path, profile="seed", enabled_fn=lambda: True)
    seed_store.record(
        scope="model", subject=IDENTITY, fingerprint_key=carousel_mod.fingerprint(inflated_key),
        until=inflated_until, kind="rate_limit", at=now,
    )
    # (a) Before this key is ever touched by the fresh process, does select()
    # alone honour the raw shared value unclamped? Recorded as an observation,
    # not scored — see REPORT.md.
    fresh_carousel = carousel_mod.Carousel(daily_cooldown_s=DAILY_COOLDOWN_S, max_hold_s=MAX_HOLD_S)
    read_now = time.time()
    _dormant_key, dormant_status = fresh_carousel.select(IDENTITY, [inflated_key], read_now)
    dormant_until = fresh_carousel._combined_until(
        fresh_carousel._pool_for(IDENTITY, [inflated_key], read_now), IDENTITY, "sim", inflated_key,
        read_now, fresh_carousel._shared.active(),
    )
    dormant_unclamped = (dormant_until - read_now) > MAX_HOLD_S + 1.0

    # (b) Touch it — any outcome — and the LOCAL, then-shared value must be
    # clamped to the ceiling from that point on.
    touch_now = time.time()
    fresh_carousel.mark(IDENTITY, inflated_key, ok=False, delay=1.0, kind="rate_limit", now=touch_now, stated=True)
    after_touch = fresh_carousel._pools[IDENTITY][inflated_key]["sick_until"]
    clamped_after_touch = (after_touch - touch_now) <= MAX_HOLD_S + 0.05

    passed = bool(
        live_honoured and expired_not_honoured and clamped_after_touch
        and not p1.get("exit_code") and not p2.get("exit_code") and not p3.get("exit_code")
    )
    return {
        "name": "restart",
        "passed": passed,
        "measurements": {
            "live_hold_honoured_after_restart": live_honoured,
            "expired_hold_not_honoured_after_restart": expired_not_honoured,
            "dormant_shared_value_unclamped_before_any_touch": dormant_unclamped,
            "clamped_to_ceiling_once_touched_again": clamped_after_touch,
            "ceiling_s": MAX_HOLD_S,
        },
    }


# --- scenario 5: pool sizes 1, 2, 14, 17 -------------------------------------

def _pool_size_run(pool_size: int) -> Dict[str, Any]:
    """In-process: this scenario tests resume timing, not cross-process
    visibility, so a single real `Carousel` (still the real production code,
    just not wrapped in a subprocess) is the right-weight instrument — the
    cross-process half is already covered end to end by scenarios 1-4.
    """
    carousel_mod, _shared_health_mod, _quota_mod = _modules()
    os.environ.pop("KAME_SHARE_POOL_HEALTH", None)
    carousel = carousel_mod.Carousel(daily_cooldown_s=DAILY_COOLDOWN_S, max_hold_s=MAX_HOLD_S)
    keys = _keys(pool_size)
    storm_hold = HOLD_S

    idle_violations = 0
    # Storm: refuse every key once, until none is healthy.
    deadline = time.time() + 20.0
    while carousel.healthy_count(IDENTITY, keys, time.time()) > 0 and time.time() < deadline:
        now = time.time()
        key, status = carousel.select(IDENTITY, keys, now)
        healthy = carousel.healthy_count(IDENTITY, keys, now)
        if status == "EXHAUSTED" and healthy > 0:
            idle_violations += 1
        if key is None:
            break
        carousel.mark(IDENTITY, key, ok=False, delay=storm_hold, kind="rate_limit", now=now, stated=True)

    storm_complete = carousel.healthy_count(IDENTITY, keys, time.time()) == 0
    release_at = time.time()  # quota "available again" from this instant on

    first_success_at = None
    poll_deadline = release_at + storm_hold + 4.0
    while time.time() < poll_deadline:
        now = time.time()
        key, status = carousel.select(IDENTITY, keys, now)
        healthy = carousel.healthy_count(IDENTITY, keys, now)
        if status == "EXHAUSTED" and healthy > 0:
            idle_violations += 1
        if status != "EXHAUSTED" and key is not None:
            carousel.mark(IDENTITY, key, ok=True, now=now)
            first_success_at = now
            break
        nrs = carousel.next_recovery_seconds(IDENTITY, keys, now)
        time.sleep(min(nrs, 0.05) if nrs else 0.02)

    resumed = first_success_at is not None
    gap_s = (first_success_at - release_at) if resumed else None
    return {
        "pool_size": pool_size,
        "storm_completed": storm_complete,
        "resumed_without_manual_reset": resumed,
        "resume_gap_s": gap_s,
        "idle_violations": idle_violations,
    }


def scenario_pool_sizes(_tmp_root: Path) -> Dict[str, Any]:
    runs = [_pool_size_run(n) for n in POOL_SIZES]
    passed = all(r["storm_completed"] and r["resumed_without_manual_reset"] for r in runs)
    return {
        "name": "pool_sizes",
        "passed": passed,
        "measurements": {"runs": runs},
    }


# --- scenario 6: never idle with a key available -----------------------------

def scenario_idle(all_results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Not its own timeline — a rollup of the `idle_violation`/`idle_violations`
    instrumentation embedded in every driver loop above (scenarios 1, 2 and
    5). See `_turn`'s own docstring for how one violation is defined.
    """
    total = 0
    for result in all_results:
        measurements = result.get("measurements", {})
        if "idle_violations" in measurements:
            total += measurements["idle_violations"]
        for run in measurements.get("runs", []):
            total += run.get("idle_violations", 0)
    return {"name": "idle", "passed": total == 0, "measurements": {"idle_violations_total": total}}


# --- self-check: the gate must be able to fail -------------------------------

def scenario_self_check(tmp_root: Path) -> Dict[str, Any]:
    """`learnings/0003`: a gate that cannot fail is not a gate.

    Reruns the storm with sharing deliberately OFF and asks scenario 1's own
    question of it — "is a key benched in one process ever reselected and
    refused again by another process while the hold lasts". With sharing off
    that is exactly what decisions/0006 predicts will happen, so this run is
    expected, and required, to come back FAIL.
    """
    # Use the same bounded empty-experiment guard as the measured OFF/ON
    # comparison. A worker starting after the refusal window measures nothing;
    # do not confuse that with a negative control that exercised the defect.
    # Worker errors and real zero-double-burn observations are never retried.
    off = _degenerate_retry(tmp_root, share_pool_health=False, tag="self_check_off")
    broken_config_detected_failure = (
        off["double_burns"] > 0 and not off["errors"] and not off["nonzero_exit"]
    )
    return {
        "name": "self_check",
        "passed": broken_config_detected_failure,
        "measurements": {
            "config": "share_pool_health=False (deliberately broken relative to decisions/0006)",
            "double_burns_observed": off["double_burns"],
            "empty_storm_retries": off.get("_retries", 0),
            "process_errors": off["errors"],
            "process_nonzero_exit": off["nonzero_exit"],
            "gate_correctly_reported_fail": broken_config_detected_failure,
        },
    }


# --- reporting -----------------------------------------------------------------

def _write_scenario(result: Dict[str, Any]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{result['name']}.json"
    slim = dict(result)
    path.write_text(json.dumps(slim, indent=2, default=str), encoding="utf-8")


def _print_table(results: List[Dict[str, Any]]) -> None:
    print(f"{'scenario':<20}{'result':<8}measurements")
    for r in results:
        verdict = "PASS" if r["passed"] else "FAIL"
        print(f"{r['name']:<20}{verdict:<8}{json.dumps(r['measurements'], default=str)[:160]}")


def _write_report(results: List[Dict[str, Any]]) -> None:
    lines = ["# G4 continuity gate — report\n"]
    lines.append(f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    overall = all(r["passed"] for r in results if r["name"] != "self_check")
    self_check = next((r for r in results if r["name"] == "self_check"), None)
    self_check_ok = bool(self_check and self_check["passed"])
    lines.append(f"**Overall (six scenarios): {'PASS' if overall else 'FAIL'}**\n")
    lines.append(f"**Self-check (gate can fail): {'PASS' if self_check_ok else 'FAIL'}**\n")
    for r in results:
        lines.append(f"\n## {r['name']} — {'PASS' if r['passed'] else 'FAIL'}\n")
        lines.append("```json\n" + json.dumps(r["measurements"], indent=2, default=str) + "\n```\n")
    (OUT_DIR / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")


# --- entry points --------------------------------------------------------------

def run_gate() -> Tuple[bool, List[Dict[str, Any]]]:
    with tempfile.TemporaryDirectory(prefix="kame-continuity-gate-") as tmp:
        tmp_root = Path(tmp)
        results = []
        results.append(scenario_three_processes(tmp_root))
        results.append(scenario_double_burn(tmp_root))
        results.append(scenario_stale_lock(tmp_root))
        results.append(scenario_restart(tmp_root))
        results.append(scenario_pool_sizes(tmp_root))
        results.append(scenario_idle(results))
        results.append(scenario_self_check(tmp_root))
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
    if argv and argv[0] == "--worker":
        return main_worker(argv[1:])
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--worker", metavar="SPEC_JSON", help=argparse.SUPPRESS)
    parser.parse_args(argv)
    before = _appdata_snapshot()
    passed, _results = run_gate()
    ok, detail = _assert_appdata_untouched(before)
    if not ok:
        print(f"APPDATA TOUCHED: {detail}")
        return 1
    print(f"\nGATE {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
