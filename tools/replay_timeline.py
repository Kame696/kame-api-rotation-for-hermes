"""Replay the owner's REAL recorded KAME-Hermes timeline through a plugin
version's REAL decision code, and report metrics so versions can be compared.

Three modes:

    python tools/replay_timeline.py --plugin-dir <hermes-kame-api-rotation dir> \
        [--calls PATH] [--refusals PATH] --out <metrics.json>

        Replays calls.jsonl (and the refusals.jsonl payloads it can be joined
        to) through that plugin version's own ``DispatchBinding._on_failure``
        and ``core.carousel.Carousel.mark``, in strict chronological order.

        Fidelity to production requires more than "one continuous engine
        replaying every row": Hermes restarts, and a restart throws away the
        carousel's in-memory learning (``_no_answer_since``, the named-window
        memory, every key's ``sick_until``) along with the rest of the
        process. This replay detects those restarts from the host's own
        ``agent.log*`` plugin-registration lines and rebuilds a fresh
        DispatchBinding/Carousel at each one, so state only ever carries over
        exactly as far as it did in production. Writes ``<out>`` (metrics
        JSON) and a sibling ``<stem>.decisions.jsonl`` (one row per replayed
        failure).

    python tools/replay_timeline.py --compare A.json B.json

        Prints (to stdout) what changed between two metrics runs produced by
        the mode above: per-event hold deltas (same input, same order, so
        events line up by index), the largest changes, and metric deltas.

    python tools/replay_timeline.py --fidelity-build BUILD_HASH METRICS.json

        Checks one version's replay against production's own recorded
        ``calls.jsonl`` kind/rest_s, restricted to the time windows during
        which that build hash was actually the registered plugin (per
        agent.log*). Prints an agreement percentage and the disagreement
        groups (production kind/rest vs replay kind/hold, with counts) so a
        human can decide whether each remaining gap is a real defect or an
        irreducible replay limitation (see research/1.8.0.0/replay/FIDELITY.md).

Isolation (mandatory, see tests/conftest.py for the same pattern): importing
the plugin makes it write recorder/journal/state files under HERMES_HOME, so
HERMES_HOME is redirected to a fresh temp directory and the two record
switches are turned off *before* the plugin package is ever imported. Nothing
here makes a network call, and nothing here writes into the owner's real
AppData — the host logs and the plugin evidence files are opened read-only.
"""

from __future__ import annotations

import argparse
import bisect
import inspect
import json
import math
import os
import re
import sys
import tempfile
import time as time_module
import types
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Records older than this are quarantined garbage (2023 fixture dates that
#: leaked into the live files before HERMES_HOME isolation existed). See
#: corpus/INVENTORY.md section 2.
MIN_AT = 1.75e9

#: A synthetic provider name that shows up in a handful of refusals; excluded
#: per the task's evidence-filtering instructions.
EXCLUDED_PROVIDER = "some-gateway"

#: How close (seconds) a call and a refusal payload have to be, on the same
#: status and the same model, to be considered the same event.
MATCH_WINDOW_S = 3.0

#: Files already renamed out of the live filename by a prior quarantine pass
#: (see corpus/INVENTORY.md section 1). Refuse to read them even if pointed
#: at one by mistake.
_QUARANTINE_MARKERS = (".polluted-by-gate-", ".written-by-tests-")

_DEFAULT_HOME = os.environ.get("LOCALAPPDATA", "")
DEFAULT_CALLS = Path(_DEFAULT_HOME) / "hermes" / "plugin-data" / "hermes-kame-api-rotation" / "calls.jsonl"
DEFAULT_REFUSALS = Path(_DEFAULT_HOME) / "hermes" / "plugin-data" / "hermes-kame-api-rotation" / "refusals.jsonl"

#: The host logs that record every plugin (re)registration — one line per
#: process start. Read-only.
_DEFAULT_LOG_DIR = Path(_DEFAULT_HOME) / "hermes" / "logs"
DEFAULT_AGENT_LOGS = [
    _DEFAULT_LOG_DIR / "agent.log",
    _DEFAULT_LOG_DIR / "agent.log.1",
    _DEFAULT_LOG_DIR / "agent.log.2",
    _DEFAULT_LOG_DIR / "agent.log.3",
]

#: Two registrations closer together than this are a possible sign of two
#: Hermes processes overlapping (sharing one calls.jsonl) rather than one
#: restarting cleanly. Reported, never used to split the timeline.
CLOSE_REGISTRATION_THRESHOLD_S = 60.0

#: A block of test-double records the coordinator identified and this run
#: independently verified: 376 of 1,763 refusals and 348 of 2,094 calls fall
#: in [2026-09-06T18:37:50Z, 2026-09-06T18:41:40Z] and carry giveaway
#: providers/types never seen outside it — "some-gateway", "a-provider-
#: from-2029", "google" (as a *provider*; the real Gemini provider in this
#: corpus is always "gemini"), bare "Boom"/"_Boom"/"DeniedError" exception
#: names, a bare "503 Service Unavailable" message. Outside the window every
#: refusal provider is one of gemini/nvidia/openai-codex/custom — confirmed
#: by re-reading the raw files, not merely taken on faith. These are fixture
#: rows, not the owner's real traffic, and replaying them would size holds
#: against exceptions no real provider ever sent.
CONTAMINATED_WINDOW_START = 1788719870.0  # 2026-09-06T18:37:50Z
CONTAMINATED_WINDOW_END = 1788720100.0  # 2026-09-06T18:41:40Z

_DIGITS = re.compile(r"\d")


# ---------------------------------------------------------------------------
# Loading the evidence (pure — no plugin import here)
# ---------------------------------------------------------------------------


def _check_not_quarantined(path: Path) -> None:
    name = Path(path).name
    for marker in _QUARANTINE_MARKERS:
        if marker in name:
            raise SystemExit(f"refusing to read a quarantined file: {path}")


def _read_jsonl(path: Path) -> List[dict]:
    rows: List[dict] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _in_contaminated_window(at: float) -> bool:
    return CONTAMINATED_WINDOW_START <= at <= CONTAMINATED_WINDOW_END


def load_calls(path: Path) -> Tuple[List[dict], dict]:
    """``(kept, filter_report)`` — calls.jsonl rows, filtered per policy."""
    _check_not_quarantined(path)
    rows = _read_jsonl(path)
    timestamped = [r for r in rows if isinstance(r.get("at"), (int, float))]
    excluded_old = sum(1 for r in timestamped if r["at"] < MIN_AT)
    after_age = [r for r in timestamped if r["at"] >= MIN_AT]
    excluded_window = sum(1 for r in after_age if _in_contaminated_window(r["at"]))
    kept = [r for r in after_age if not _in_contaminated_window(r["at"])]
    report = {
        "total_read": len(rows),
        "excluded_below_min_at": excluded_old,
        "excluded_contaminated_window": excluded_window,
        "kept": len(kept),
    }
    return kept, report


def load_refusals(path: Path) -> Tuple[List[dict], dict]:
    """``(kept, filter_report)`` — refusals.jsonl rows, filtered per policy."""
    _check_not_quarantined(path)
    rows = _read_jsonl(path)
    timestamped = [r for r in rows if isinstance(r.get("at"), (int, float))]
    excluded_old = sum(1 for r in timestamped if r["at"] < MIN_AT)
    after_age = [r for r in timestamped if r["at"] >= MIN_AT]
    excluded_gateway = sum(1 for r in after_age if r.get("provider") == EXCLUDED_PROVIDER)
    after_gateway = [r for r in after_age if r.get("provider") != EXCLUDED_PROVIDER]
    excluded_window = sum(1 for r in after_gateway if _in_contaminated_window(r["at"]))
    kept = [r for r in after_gateway if not _in_contaminated_window(r["at"])]
    report = {
        "total_read": len(rows),
        "excluded_below_min_at": excluded_old,
        "excluded_some_gateway": excluded_gateway,
        "excluded_contaminated_window": excluded_window,
        "kept": len(kept),
    }
    return kept, report


# ---------------------------------------------------------------------------
# Process boundaries — one Hermes restart = one fresh DispatchBinding/Carousel
# ---------------------------------------------------------------------------

#: ``2026-09-08 19:12:57,256 INFO hermes_plugins.hermes_kame_api_rotation:
#: hermes-kame-api-rotation: build 91eb3775ab22 <mojibake> complete``
#: Timestamps in this log are LOCAL time; ``time.mktime`` is what the
#: coordinator specified for converting them, and that is what is used here.
_BUILD_LINE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3}).*"
    r"hermes-kame-api-rotation: build ([0-9a-fA-F]+)"
)


def parse_agent_log_boundaries(paths: Sequence[Path]) -> List[Tuple[float, str]]:
    """One ``(epoch, build_hash)`` per plugin registration line, sorted.

    Each registration is a fresh module import in a fresh Hermes process —
    the in-memory carousel state from the previous line did not survive to
    see this one, and replaying as though it did is exactly the fidelity gap
    this function exists to close.
    """
    seen: set = set()
    rows: List[Tuple[float, str]] = []
    for raw_path in paths:
        path = Path(raw_path)
        if not path.is_file():
            continue
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                match = _BUILD_LINE.match(line)
                if not match:
                    continue
                ts, ms, build_hash = match.groups()
                try:
                    epoch = time_module.mktime(time_module.strptime(ts, "%Y-%m-%d %H:%M:%S")) + int(ms) / 1000.0
                except ValueError:
                    continue
                dedupe_key = (round(epoch, 3), build_hash)
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                rows.append((epoch, build_hash))
    rows.sort(key=lambda row: row[0])
    return rows


def find_close_registrations(
    boundaries: Sequence[Tuple[float, str]], threshold: float = CLOSE_REGISTRATION_THRESHOLD_S
) -> List[dict]:
    """Consecutive registrations — any hash, including the same hash twice —
    closer together than ``threshold``. Reported, never used to split the
    timeline: this tool has no way to tell which of two overlapping processes
    a given call row belongs to.
    """
    close: List[dict] = []
    for (epoch_a, hash_a), (epoch_b, hash_b) in zip(boundaries, boundaries[1:]):
        gap = epoch_b - epoch_a
        if gap < threshold:
            close.append(
                {"at_a": epoch_a, "hash_a": hash_a, "at_b": epoch_b, "hash_b": hash_b, "gap_s": round(gap, 3)}
            )
    return close


def compute_hash_windows(boundaries: Sequence[Tuple[float, str]], target_hash: str) -> List[Tuple[float, float]]:
    """``[start, end)`` windows during which ``target_hash`` was the
    registered build — each ends at the next registration of *any* hash, or
    stays open (``math.inf``) if it is the last one seen."""
    windows: List[Tuple[float, float]] = []
    n = len(boundaries)
    for i, (epoch, build_hash) in enumerate(boundaries):
        if build_hash != target_hash:
            continue
        end = boundaries[i + 1][0] if i + 1 < n else math.inf
        windows.append((epoch, end))
    return windows


def _in_windows(t: float, windows: Sequence[Tuple[float, float]]) -> bool:
    return any(start <= t < end for start, end in windows)


# ---------------------------------------------------------------------------
# Joining a failed call to its refusals.jsonl payload (pure)
# ---------------------------------------------------------------------------


def _model_suffix(identity: str) -> str:
    return identity.split(":", 1)[1] if ":" in identity else identity


def _refusal_matches_identity(identity: str, model: Any) -> bool:
    """Whether a refusal's ``model`` field names the same call as ``identity``.

    Most refusals carry ``model`` as the exact ``provider:model`` identity the
    call was recorded under. A minority carry only the bare model name (no
    provider prefix) — seen directly in the corpus, e.g. ``"gpt-5.6-luna"`` or
    ``"z-ai/glm-5.2"`` beside identities like ``"openai-codex:gpt-5.6-luna"``.
    Both are honoured; a bare name is matched against the identity's suffix
    after the first colon.
    """
    if not model or not isinstance(model, str):
        return False
    if model == identity:
        return True
    if ":" not in model:
        return model == _model_suffix(identity)
    return False


def match_refusals(calls: Sequence[dict], refusals: Sequence[dict]) -> Dict[str, int]:
    """Attach ``row["_refusal"]`` (a refusal dict, or ``None``) to every
    non-``answered`` row in ``calls``, in place. Returns match counters.

    Matching is nearest-within-window, same status, same model, **without
    replacement** — once a refusal is used it cannot be reused for a second
    call, which matters because the two files are close to 1:1 in this
    corpus (1,497 non-answered calls vs 1,759 kept refusals).
    """
    by_status: Dict[Any, List[int]] = {}
    for i, r in enumerate(refusals):
        by_status.setdefault(r.get("status"), []).append(i)
    for indices in by_status.values():
        indices.sort(key=lambda i: refusals[i]["at"])

    used = [False] * len(refusals)
    matched = 0
    unmatched = 0

    for row in calls:
        if row.get("outcome") == "answered":
            continue
        candidates = by_status.get(row.get("status"), [])
        if not candidates:
            row["_refusal"] = None
            unmatched += 1
            continue
        ats = [refusals[i]["at"] for i in candidates]
        pos = bisect.bisect_left(ats, row["at"])
        best_idx: Optional[int] = None
        best_dt: Optional[float] = None
        k = pos - 1
        while k >= 0:
            i = candidates[k]
            dt = abs(refusals[i]["at"] - row["at"])
            if dt > MATCH_WINDOW_S:
                break
            if not used[i] and _refusal_matches_identity(row["identity"], refusals[i].get("model")):
                if best_dt is None or dt < best_dt:
                    best_idx, best_dt = i, dt
            k -= 1
        k = pos
        while k < len(candidates):
            i = candidates[k]
            dt = abs(refusals[i]["at"] - row["at"])
            if dt > MATCH_WINDOW_S:
                break
            if not used[i] and _refusal_matches_identity(row["identity"], refusals[i].get("model")):
                if best_dt is None or dt < best_dt:
                    best_idx, best_dt = i, dt
            k += 1
        if best_idx is not None:
            used[best_idx] = True
            row["_refusal"] = refusals[best_idx]
            matched += 1
        else:
            row["_refusal"] = None
            unmatched += 1

    return {"matched": matched, "unmatched": unmatched}


# ---------------------------------------------------------------------------
# Rebuilding an exception the plugin's evidence reader accepts
# ---------------------------------------------------------------------------

_exc_class_cache: Dict[str, type] = {}


def _exception_class(name: str) -> type:
    safe = "".join(c if (c.isalnum() or c == "_") else "_" for c in (name or "")).strip("_")
    safe = safe or "ReplayedRefusal"
    if safe[0].isdigit():
        safe = "_" + safe
    if safe not in _exc_class_cache:
        _exc_class_cache[safe] = type(safe, (Exception,), {})
    return _exc_class_cache[safe]


def _parse_maybe_json(text: Any) -> Any:
    if not text or not isinstance(text, str):
        return None
    try:
        return json.loads(text)
    except Exception:
        return text


def build_exception_from_refusal(refusal: dict) -> BaseException:
    """The reference shape is ``tools/live_daily.py``'s ``Refusal`` class:
    ``status_code``, ``body`` (parsed JSON when it parses), and a ``response``
    carrying ``.text`` — exactly what ``core.evidence.harvest`` reads.

    Headers were never part of this recorder's schema (``recorder.py`` never
    captures them), so ``exc.headers`` / a real ``Retry-After`` header can
    never be reconstructed here — only whatever retry hint survived inside
    the body (Google's ``RetryInfo``/``quotaId`` members do). Noted as a
    replay limitation, not a bug in this tool.
    """
    cls = _exception_class(refusal.get("type") or "")
    message = str(refusal.get("message") or "")
    exc = cls(message)
    status = refusal.get("status")
    if isinstance(status, int):
        exc.status_code = status
    code = refusal.get("code")
    if isinstance(code, str) and code.strip():
        exc.code = code.strip()
    body_parsed = _parse_maybe_json(refusal.get("body"))
    if body_parsed is not None:
        exc.body = body_parsed
    response_text = refusal.get("response")
    if response_text:
        exc.response = types.SimpleNamespace(
            text=response_text,
            status_code=status if isinstance(status, int) else None,
            headers={},
        )
        if not hasattr(exc, "body"):
            resp_parsed = _parse_maybe_json(response_text)
            if resp_parsed is not None:
                exc.body = resp_parsed
    return exc


def build_minimal_exception(call_row: dict) -> BaseException:
    """For a failed call with no matching refusal payload: status + kind only."""
    kind = str(call_row.get("kind") or "unknown")
    status = call_row.get("status")
    cls = _exception_class("Replayed_" + kind)
    exc = cls(f"replayed {kind} failure (status={status})")
    if isinstance(status, int):
        exc.status_code = status
    return exc


def _redact_message(message: str, limit: int = 120) -> str:
    if not message:
        return ""
    return _DIGITS.sub("N", message)[:limit]


# ---------------------------------------------------------------------------
# Pure bookkeeping: contradicted holds + idle-with-key-available
# ---------------------------------------------------------------------------


class ReplayState:
    """Tracks, per (identity, key), the hold this replay's engine applied —
    mirrored from the real engine's own ``sick_until`` after each decision —
    so the two cross-checks below can be computed without re-querying the
    engine at arbitrary later times.

    An instance is scoped to one simulated Hermes process: the replay driver
    creates a fresh ``ReplayState`` at every detected restart, exactly as a
    fresh ``Carousel`` forgets everything a restarted production process
    forgets. Carrying a hold or a "seen key" across a restart boundary would
    be claiming knowledge the real, freshly-started carousel never had.
    """

    def __init__(self) -> None:
        self._sick_until: Dict[Tuple[str, str], float] = {}
        self._seen_keys: Dict[str, List[str]] = {}
        # (identity, key) -> [hold_start, hold_end, contradicted?]
        self._open_hold: Dict[Tuple[str, str], List[Any]] = {}
        self.contradicted_count = 0
        self.contradicted_seconds_lost = 0.0

    def _mark_seen(self, identity: str, key: str) -> None:
        seen = self._seen_keys.setdefault(identity, [])
        if key not in seen:
            seen.append(key)

    def mark_seen(self, identity: str, key: str) -> None:
        """Record that ``key`` appeared on ``identity``, without touching its
        hold state — for a decision that neither marked nor answered (a
        ``raise`` verdict that left the key's own availability untouched)."""
        self._mark_seen(identity, key)

    def register_hold(self, identity: str, key: str, at: float, hold_s: float) -> None:
        until = at + max(0.0, hold_s)
        self._sick_until[(identity, key)] = until
        self._mark_seen(identity, key)
        if hold_s > 0:
            self._open_hold[(identity, key)] = [at, until, False]
        else:
            self._open_hold.pop((identity, key), None)

    def check_idle_before_success(self, identity: str, key: str, at: float) -> Optional[dict]:
        """Call BEFORE :meth:`register_success` for the same event.

        Returns an event dict when every key of ``identity`` this replay has
        seen *before* this moment is currently held (``sick_until > at``) —
        i.e. this version's carousel would have found nothing to send, while
        the real log is about to show an answer (on this key, or on a key not
        yet seen). Returns ``None`` when at least one previously-seen key was
        free, or when no key of this identity has been seen yet (a first
        call is not evidence of anything).
        """
        seen = list(self._seen_keys.get(identity, []))
        if not seen:
            return None
        releases = []
        for candidate in seen:
            until = self._sick_until.get((identity, candidate), 0.0)
            if until <= at:
                return None
            releases.append(until)
        return {
            "at": at,
            "identity": identity,
            "keys_checked": len(seen),
            "time_until_release": min(releases) - at,
        }

    def register_success(self, identity: str, key: str, at: float) -> None:
        pair = (identity, key)
        hold = self._open_hold.get(pair)
        if hold is not None:
            hold_start, hold_end, contradicted = hold
            if not contradicted and hold_start < at < hold_end:
                self.contradicted_count += 1
                self.contradicted_seconds_lost += hold_end - at
                hold[2] = True
        self._sick_until[pair] = 0.0
        self._open_hold.pop(pair, None)
        self._mark_seen(identity, key)


# ---------------------------------------------------------------------------
# Hold statistics (pure)
# ---------------------------------------------------------------------------


def _percentile(sorted_values: Sequence[float], pct: float) -> Optional[float]:
    if not sorted_values:
        return None
    k = (len(sorted_values) - 1) * pct
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return sorted_values[int(k)]
    return sorted_values[f] + (sorted_values[c] - sorted_values[f]) * (k - f)


def hold_stats(hold_seconds: Sequence[float], by_kind: Counter, by_status: Counter) -> dict:
    values = sorted(hold_seconds)
    n = len(values)
    return {
        "count": n,
        "total_seconds": round(sum(values), 3) if n else 0.0,
        "p50": round(_percentile(values, 0.50), 3) if n else None,
        "p90": round(_percentile(values, 0.90), 3) if n else None,
        "max": round(values[-1], 3) if n else None,
        "over_300s": sum(1 for v in values if v > 300.0),
        "over_1800s": sum(1 for v in values if v > 1800.0),
        "over_3600s": sum(1 for v in values if v > 3600.0),
        "by_kind": dict(by_kind),
        "by_status": {str(k): v for k, v in by_status.items()},
    }


# ---------------------------------------------------------------------------
# Isolation + dynamic plugin loading
# ---------------------------------------------------------------------------


def isolate_hermes_home(*, restore: bool = False) -> Path:
    """Redirect HERMES_HOME and disable the two record switches — must run
    BEFORE the plugin package is imported. Same guard as tests/conftest.py.

    ``restore=True`` registers an ``atexit`` hook putting the previous value
    back. The default stays "set it and leave it", because the tool's own
    process exists to replay one version and then die — but a test process
    lives on, and a leaked HERMES_HOME there silently moves every later
    test's destination out from under ``conftest``'s guard. That happened:
    `test_v1_7_0_2::test_the_suite_cannot_reach_the_installed_state_directory`
    caught it in the full run while passing on its own. Tests must either
    pass ``restore=True`` or set the variable through ``monkeypatch``.
    """
    previous = os.environ.get("HERMES_HOME")
    home = Path(tempfile.mkdtemp(prefix="kame-replay-home-"))
    os.environ["HERMES_HOME"] = str(home)
    os.environ.setdefault("KAME_RECORDER_DISABLED", "1")
    os.environ.setdefault("KAME_CALL_TIMINGS_DISABLED", "1")
    if restore:
        import atexit

        def _put_it_back() -> None:
            if previous is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = previous

        atexit.register(_put_it_back)
    return home


def load_plugin(plugin_dir: Path, package_name: str = "kame_replay_plugin_under_test"):
    """Load one ``hermes-kame-api-rotation`` tree under its own module name.

    Two versions replayed in the same interpreter would collide (both use the
    same module names, e.g. ``dispatch_binding``, ``core.carousel``) — the
    caller is responsible for running each version in its own process, which
    is naturally what happens when this script is invoked twice from the
    shell. Idempotent within one process for the same ``package_name``, which
    is what lets tests reuse it.
    """
    plugin_dir = Path(plugin_dir).resolve()
    init_file = plugin_dir / "__init__.py"
    if not init_file.is_file():
        raise SystemExit(f"no __init__.py under {plugin_dir} — not a plugin directory")
    if package_name in sys.modules:
        return sys.modules[package_name]
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        package_name, init_file, submodule_search_locations=[str(plugin_dir)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _introspect_adaptations(dispatch_binding_mod, carousel_mod) -> dict:
    info: Dict[str, Any] = {}
    try:
        params = list(inspect.signature(dispatch_binding_mod.DispatchBinding._on_failure).parameters.keys())
        info["_on_failure_params"] = params
        info["_on_failure_has_credential_id"] = "credential_id" in params
    except (TypeError, ValueError) as exc:
        info["_on_failure_introspection_error"] = str(exc)
    try:
        info["carousel_mark_params"] = list(inspect.signature(carousel_mod.Carousel.mark).parameters.keys())
    except (TypeError, ValueError) as exc:
        info["carousel_mark_introspection_error"] = str(exc)
    return info


#: Sentinel distinguishing "mark() was called and returned this" from "mark()
#: was never called for this decision" — a plain ``None``/``0.0`` cannot do
#: that job because both are valid real return values.
_NO_MARK = object()


def _install_mark_capture(engine: Any) -> Dict[str, Any]:
    """Wrap ``engine.mark`` so the value it returns can be read by the caller
    without changing what it does. ``_on_failure`` calls ``self.engine.mark``
    internally and does not hand the result back, so this is the only way to
    see exactly what production's own ``rest_s`` is defined against (the
    plugin's own docstring: "the cooldown actually applied")."""
    original_mark = engine.mark
    capture: Dict[str, Any] = {"last": _NO_MARK}

    def _wrapped(*args, **kwargs):
        result = original_mark(*args, **kwargs)
        capture["last"] = result
        return result

    engine.mark = _wrapped
    return capture


#: Sourced verbatim from refusals.jsonl's own raw ``message`` field — 451
#: byte-identical occurrences in this corpus, all ending in this exact text.
#: This is Hermes' ``agent.gemini_native_adapter._FREE_TIER_GUIDANCE``
#: paragraph, the one production strips before classifying (host_text.py's
#: docstring: "Hermes' Gemini adapter appends its own guidance... poison to a
#: classifier"). This replay has no Hermes host to import it from, and
#: host_text.py's own fallback (``_FALLBACK_BLOCKS``) is deliberately only
#: each paragraph's OPENING CLAUSE — sized for ``_cut_at_the_advice``'s
#: truncate-from-here behaviour, not for ``evidence.strip_trailing_blocks``'s
#: exact-substring-removal, which needs the FULL paragraph to remove the word
#: "billing" the paragraph ends on. Without this, every replayed free-tier
#: refusal keeps "...exceeded your current quota...billing..." in the
#: message the classifier sees, `core.classify`'s ``_AMBIGUOUS_BILLING_
#: PATTERNS`` fires, and a real "daily"/"rate_limit" refusal is misread as
#: "billing" -> "insufficient_quota" — measured at ~450 direct misreads,
#: compounding further through the carousel's own named-window memory.
_KNOWN_HOST_GUIDANCE_BLOCKS: Tuple[str, ...] = (
    "Your Google API key is on the free tier (a few hundred requests/day for "
    "Gemini Flash models). Hermes typically makes 3-10 API calls per user "
    "turn, so the free tier is exhausted in a handful of messages and cannot "
    "sustain an agent session. Enable billing on your Google Cloud project "
    "and regenerate the key in a billing-enabled project: "
    "https://aistudio.google.com/apikey",
)


def patch_guidance_blocks(package_name: str) -> bool:
    """Make ``host_text.guidance_blocks()`` return the real, full paragraph
    this corpus shows Hermes appending, instead of host_text.py's own
    (deliberately partial) fallback. Returns whether the patch was applied.
    """
    import importlib

    try:
        host_text_mod = importlib.import_module(f"{package_name}.host_text")
    except Exception:
        return False
    existing_fallback = tuple(getattr(host_text_mod, "_FALLBACK_BLOCKS", ()))
    combined = list(_KNOWN_HOST_GUIDANCE_BLOCKS)
    for block in existing_fallback:
        if block not in combined:
            combined.append(block)
    host_text_mod.guidance_blocks = lambda: list(combined)
    return True


#: Above this, a hold is reported under ``over_ceiling`` regardless of which
#: version produced it. Matches ``core.carousel.DAILY_COOLDOWN_S`` (the
#: plugin's own "an hour" default) — anything past it is either a genuinely
#: long provider-stated deadline, a calendar reset, escalation, or a
#: hardcoded ceiling, never an ordinary throttle.
OVER_CEILING_THRESHOLD_S = 3600.0


def classify_over_ceiling_cause(
    *, retained: bool, calendar_reset: bool, stated: bool, escalation_produced: bool
) -> str:
    """Best-available explanation for a hold above ``OVER_CEILING_THRESHOLD_S``.

    Checked in this order because a retained (extended pre-existing) hold is
    never this decision's own number to explain a different way; a calendar
    reset and a provider-stated deadline are both readable directly off the
    re-derived evidence; escalation (``core.escalate.stretch``) is checked
    only once neither applies — it is a pure OBSERVATION here (see
    ``run_replay``'s escalation tracking), never wired into the real decision,
    so it will only ever match a real over-ceiling hold by coincidence, and
    that is itself the finding worth reporting. "default" is whatever is
    left: a hardcoded ceiling such as ``insufficient_quota``'s daily-cooldown
    default, or an unstated legacy-classifier fallback.
    """
    if retained:
        return "retained_prior_hold"
    if calendar_reset:
        return "calendar_reset"
    if stated:
        return "stated_deadline"
    if escalation_produced:
        return "escalation"
    return "default"


def _recompute_verdict_fields(
    evidence_mod, classify_mod, carousel_mod, host_text_mod, exc: BaseException, identity: str, at: float,
) -> Tuple[str, bool, bool]:
    """``(window, stated, calendar_reset)`` — facts ``_on_failure`` computes
    internally but does not return.

    Re-derived by calling the SAME pure, read-only evidence/classify
    functions a second time on the SAME exception, under the SAME frozen
    clock (`_on_failure` has already run and marked the engine by the time
    this is called; nothing here touches engine state). Restricted to marked
    failures only, so the branches `_on_failure` returns "raise" from before
    ever reaching this computation (host_breaker, is_terminal, 421/401
    special cases) never apply here either — a "raise" verdict was never
    journaled or stretch-eligible in production, so it is not tracked here.
    """
    provider_name = identity.split(":", 1)[0] if ":" in identity else identity
    ev = evidence_mod.harvest(exc, message=str(exc), guidance_blocks=host_text_mod.guidance_blocks())
    verdict = classify_mod.classify(
        provider=provider_name, model=identity, status_code=ev.status_code,
        error_message=ev.message, error_body=ev.body, headers=ev.headers,
        error=exc, now_epoch=at,
    )
    window = getattr(verdict, "quota_window", "unknown") if verdict is not None else "unknown"
    try:
        stated = bool(carousel_mod.extract_delay(exc, ev.message, ev.headers))
    except Exception:
        stated = False
    calendar_reset = bool(
        verdict is not None
        and getattr(verdict, "window_scoped_reset", False)
        and getattr(verdict, "reset_at", None)
        and float(verdict.reset_at) > at
    )
    return window, stated, calendar_reset


def _provider_timed(vocabulary_mod, source: str) -> Optional[bool]:
    """``vocabulary.provider_timed`` itself is new — 1.7.0.5's core/vocabulary.py
    has no such function at all, so the very CONCEPT the 1.7.0.7 guard is
    built on does not exist there yet. ``None`` means "this version cannot
    even ask the question", which is a materially different fact from
    "asked, and the answer was no" (``False``) — collapsing the two would
    make an absent guard look like a guard that never fires.
    """
    fn = getattr(vocabulary_mod, "provider_timed", None)
    if fn is None:
        return None
    try:
        return bool(fn(source))
    except Exception:
        return None


def _aggregate_escalation(events: Sequence[dict], guard_available: bool) -> dict:
    by_identity: Dict[str, Dict[str, Any]] = {}
    for e in events:
        bucket = by_identity.setdefault(
            e["identity"],
            {
                "checked": 0, "strikes_ge_2": 0, "produced": 0,
                "blocked_by_provider_timed_guard": 0, "total_widened_seconds": 0.0,
            },
        )
        bucket["checked"] += 1
        if e["source_is_provider_timed"] is True:
            bucket["blocked_by_provider_timed_guard"] += 1
        if e["strikes"] >= 2:
            bucket["strikes_ge_2"] += 1
        if e["produced"]:
            bucket["produced"] += 1
            bucket["total_widened_seconds"] += e["widened_by_seconds"] or 0.0
    for bucket in by_identity.values():
        bucket["total_widened_seconds"] = round(bucket["total_widened_seconds"], 3)
    return {
        # Whether THIS version's core.vocabulary even has provider_timed —
        # 1.7.0.5 does not, so "blocked_by_provider_timed_guard" being 0
        # there means "the guard does not exist", not "the guard exists and
        # never fired". Read the two together, never guard-count alone.
        "guard_available": guard_available,
        "checked": len(events),
        "strikes_ge_2": sum(1 for e in events if e["strikes"] >= 2),
        "produced": sum(1 for e in events if e["produced"]),
        "blocked_by_provider_timed_guard": sum(1 for e in events if e["source_is_provider_timed"] is True),
        "by_identity": by_identity,
    }


def _aggregate_over_ceiling(events: Sequence[dict], threshold: float) -> dict:
    by_cause: Counter = Counter(e["cause"] for e in events)
    return {
        "threshold_s": threshold,
        "count": len(events),
        "by_cause": dict(by_cause),
        "events": list(events),
    }


def _new_segment(carousel_mod, dispatch_binding_mod) -> Tuple[Any, Any, Dict[str, Any]]:
    """A fresh engine + binding + mark-capture — one simulated process life."""
    engine = carousel_mod.Carousel()
    binding = dispatch_binding_mod.DispatchBinding(engine=engine)
    capture = _install_mark_capture(engine)
    return engine, binding, capture


# ---------------------------------------------------------------------------
# The replay driver
# ---------------------------------------------------------------------------


def run_replay(
    plugin_dir: Path,
    calls_path: Path,
    refusals_path: Path,
    out_path: Path,
    boundaries: Optional[Sequence[Tuple[float, str]]] = None,
    agent_logs: Optional[Sequence[Path]] = None,
) -> dict:
    """``boundaries`` overrides the parsed agent.log registrations — tests
    inject a small synthetic list here so they never depend on, or read,
    the real host logs. Passing ``[]`` explicitly means "replay with a single
    continuous process, no resets" (the pre-fidelity-fix behaviour), which is
    also useful as a control run."""
    calls, calls_filter_report = load_calls(calls_path)
    refusals, refusals_filter_report = load_refusals(refusals_path)
    calls.sort(key=lambda r: r["at"])
    match_counts = match_refusals(calls, refusals)

    if boundaries is None:
        boundaries = parse_agent_log_boundaries(agent_logs if agent_logs is not None else DEFAULT_AGENT_LOGS)
    boundaries = sorted(boundaries, key=lambda row: row[0])
    close_regs = find_close_registrations(boundaries)
    boundary_epochs = sorted({epoch for epoch, _hash in boundaries})

    calls_span: Tuple[Optional[float], Optional[float]] = (
        (calls[0]["at"], calls[-1]["at"]) if calls else (None, None)
    )

    def _within_span(t: float) -> bool:
        return calls_span[0] is not None and calls_span[0] <= t <= calls_span[1]

    close_regs_in_span = [row for row in close_regs if _within_span(row["at_b"])]

    # Isolation FIRST, then import — mirrors tests/conftest.py exactly.
    # ``restore=True`` because this function is called from tests too, and a
    # HERMES_HOME left behind there moves every later test's destination out
    # from under conftest's guard.
    hermes_home = isolate_hermes_home(restore=True)
    package_name = f"kame_replay_target_{os.getpid()}_{id(plugin_dir)}"
    plugin = load_plugin(plugin_dir, package_name=package_name)
    import importlib

    dispatch_binding_mod = importlib.import_module(f"{package_name}.dispatch_binding")
    carousel_mod = importlib.import_module(f"{package_name}.core.carousel")
    events_mod = importlib.import_module(f"{package_name}.core.events")
    evidence_mod = importlib.import_module(f"{package_name}.core.evidence")
    classify_mod = importlib.import_module(f"{package_name}.core.classify")
    journal_mod = importlib.import_module(f"{package_name}.core.journal")
    escalate_mod = importlib.import_module(f"{package_name}.core.escalate")
    vocabulary_mod = importlib.import_module(f"{package_name}.core.vocabulary")
    host_text_mod_ref = importlib.import_module(f"{package_name}.host_text")
    guidance_patched = patch_guidance_blocks(package_name)

    # Escalation is measured, not driven: `core.escalate.stretch` is only
    # ever called from `pool_binding.PoolBinding._remember`, which fires when
    # HERMES' OWN credential pool marks a credential exhausted — a code path
    # this replay (which drives `dispatch_binding._on_failure` directly) does
    # not exercise at all. So the journal `_remember`/`note_rotation` would
    # feed is built here instead, fed from exactly the facts `_on_failure`'s
    # own journal write (`runtime.record_rotation`) would have carried, and
    # `short_streak`/`stretch` are called against it as a pure observation —
    # never affecting the real replayed hold. One Journal for the whole
    # timeline: it is Hermes' own disk-persisted state (`self._journal.load/
    # .save` in pool_binding.py), unlike the in-memory Carousel, so unlike
    # `ReplayState` it is NOT reset at process boundaries.
    journal_book = journal_mod.Journal()
    escalation_events: List[dict] = []
    over_ceiling_events: List[dict] = []
    # 1.7.0.5's short_streak(journal, *, credential_id, model, window, at)
    # has no source/reason parameters at all — the guard vocabulary those
    # would feed did not exist yet. Detected once, per the same
    # inspect.signature adaptation pattern used for _on_failure.
    try:
        _short_streak_params = set(inspect.signature(journal_mod.short_streak).parameters.keys())
    except (TypeError, ValueError):
        _short_streak_params = set()
    short_streak_accepts_source_reason = {"source", "reason"} <= _short_streak_params

    adaptations = _introspect_adaptations(dispatch_binding_mod, carousel_mod)
    adaptations["guidance_blocks_patched"] = guidance_patched
    adaptations["short_streak_params"] = sorted(_short_streak_params)
    adaptations["short_streak_accepts_source_reason"] = short_streak_accepts_source_reason
    adaptations["vocabulary_has_provider_timed"] = hasattr(vocabulary_mod, "provider_timed")
    accepts_credential_id = adaptations.get("_on_failure_has_credential_id", True)
    events = events_mod.EVENTS

    engine, binding, mark_capture = _new_segment(carousel_mod, dispatch_binding_mod)
    replay_state = ReplayState()
    segment_index = 0
    b_idx = 0
    boundaries_applied = 0
    # ReplayState is scoped to one simulated process life and is replaced
    # wholesale at every boundary, so its contradicted-hold counters have to
    # be folded into a running total before each replacement (and once more
    # after the loop) rather than read off whichever instance is current.
    total_contradicted_count = 0
    total_contradicted_seconds_lost = 0.0

    def _fold_contradicted(state: "ReplayState") -> None:
        nonlocal total_contradicted_count, total_contradicted_seconds_lost
        total_contradicted_count += state.contradicted_count
        total_contradicted_seconds_lost += state.contradicted_seconds_lost

    # Clock control: every decision must see now == record.at, not wall time.
    # Patching the `time` module object reaches every core/* module too,
    # since all of them do `import time` (same module object) rather than
    # `from time import time`.
    clock = {"now": calls[0]["at"] if calls else time_module.time()}
    real_time_fn = time_module.time
    real_monotonic_fn = time_module.monotonic
    time_module.time = lambda: clock["now"]
    time_module.monotonic = lambda: clock["now"]

    decisions: List[dict] = []
    hold_seconds: List[float] = []
    holds_by_kind: Counter = Counter()
    holds_by_status: Counter = Counter()
    idle_events: List[dict] = []
    successes = 0
    failures = 0
    marked_failures = 0
    on_failure_call_mode = "with_credential_id" if accepts_credential_id else "positional_only"

    try:
        for row in calls:
            clock["now"] = row["at"]

            while b_idx < len(boundary_epochs) and boundary_epochs[b_idx] <= row["at"]:
                _fold_contradicted(replay_state)
                engine, binding, mark_capture = _new_segment(carousel_mod, dispatch_binding_mod)
                replay_state = ReplayState()
                segment_index += 1
                boundaries_applied += 1
                b_idx += 1

            identity = row.get("identity") or "?:?"
            key = row.get("key") or ""
            at = row["at"]
            if not key:
                continue

            if row.get("outcome") == "answered":
                idle = replay_state.check_idle_before_success(identity, key, at)
                if idle is not None:
                    idle["segment"] = segment_index
                    idle_events.append(idle)
                engine.mark(identity, key, True, now=at)
                replay_state.register_success(identity, key, at)
                provider_name = identity.split(":", 1)[0] if ":" in identity else identity
                try:
                    journal_book.record_success(
                        at=at, provider=provider_name, model=identity, credential_id=key,
                    )
                except Exception:
                    pass
                successes += 1
                continue

            failures += 1
            refusal = row.get("_refusal")
            matched = refusal is not None
            exc = build_exception_from_refusal(refusal) if matched else build_minimal_exception(row)
            attempt = row.get("attempt") or 1

            seq_before = events.total
            mark_capture["last"] = _NO_MARK
            try:
                if accepts_credential_id:
                    verdict, kind_returned, status = binding._on_failure(
                        identity, key, exc, identity, attempt, False, credential_id=key,
                    )
                else:
                    verdict, kind_returned, status = binding._on_failure(
                        identity, key, exc, identity, attempt, False,
                    )
            except TypeError:
                # Signature actually differs from what introspection found —
                # fall back to the narrowest call every version supports.
                on_failure_call_mode = "fallback_after_TypeError"
                verdict, kind_returned, status = binding._on_failure(
                    identity, key, exc, identity, attempt, False,
                )

            sized_by = ""
            if events.total > seq_before:
                newest = events.recent(limit=1)
                if newest:
                    sized_by = newest[0].get("sized_by", "")

            applied_now = mark_capture["last"]
            marked = applied_now is not _NO_MARK
            retained = bool(getattr(applied_now, "retained", False)) if marked else False

            sick_until = engine._pools.get(identity, {}).get(key, {}).get("sick_until", 0.0)
            sick_until_delta = round(max(0.0, sick_until - at), 3)

            if marked:
                applied_float = float(applied_now)
                hold_s: Optional[float] = round(applied_float, 3)
                marked_failures += 1
                hold_seconds.append(applied_float)
                holds_by_kind[kind_returned or ""] += 1
                holds_by_status[status] += 1
                replay_state.register_hold(identity, key, at, applied_float)

                provider_name = identity.split(":", 1)[0] if ":" in identity else identity
                window, stated, calendar_reset = _recompute_verdict_fields(
                    evidence_mod, classify_mod, carousel_mod, host_text_mod_ref, exc, identity, at,
                )
                reset_at = at + applied_float
                source_is_provider_timed = _provider_timed(vocabulary_mod, sized_by)
                try:
                    if short_streak_accepts_source_reason:
                        strikes = journal_mod.short_streak(
                            journal_book, credential_id=key, model=identity, window=window,
                            at=at, source=sized_by, reason=kind_returned,
                        )
                    else:
                        strikes = journal_mod.short_streak(
                            journal_book, credential_id=key, model=identity, window=window, at=at,
                        )
                except Exception:
                    strikes = 0
                try:
                    stretched = escalate_mod.stretch(
                        reset_at=reset_at, now=at, strikes=strikes, window=window,
                        reason=kind_returned, source=sized_by,
                    )
                except Exception:
                    stretched = None
                escalation_produced = stretched is not None
                escalation_events.append(
                    {
                        "at": at, "identity": identity, "key": key, "window": window,
                        "source": sized_by, "reason": kind_returned, "strikes": strikes,
                        "produced": escalation_produced,
                        "widened_by_seconds": (
                            round(float(stretched) - reset_at, 3) if escalation_produced else None
                        ),
                        "source_is_provider_timed": source_is_provider_timed,
                    }
                )
                try:
                    journal_book.record_block(
                        at=at, provider=provider_name, model=identity, credential_id=key,
                        status_code=status if isinstance(status, int) else None, window=window,
                        source=sized_by, reset_at=reset_at, sized_by=journal_mod.SIZED_BY_KAME,
                        reason=kind_returned, stated_window="unknown",
                    )
                except Exception:
                    pass

                if applied_float > OVER_CEILING_THRESHOLD_S:
                    cause = classify_over_ceiling_cause(
                        retained=retained, calendar_reset=calendar_reset, stated=stated,
                        escalation_produced=escalation_produced,
                    )
                    over_ceiling_events.append(
                        {
                            "at": at, "identity": identity, "key": key, "status": status,
                            "message_shape": _redact_message(str(exc)),
                            "hold_s": round(applied_float, 3), "cause": cause,
                            "source": sized_by, "kind_returned": kind_returned,
                        }
                    )
            else:
                # No new mark — e.g. a "raise" verdict (terminal, host
                # breaker, upstream wrapper, model-not-ready, auth-refresh).
                # The key's availability did not change because of THIS
                # decision, so it is excluded from the holds this replay
                # claims to have caused, and the engine's belief about the
                # key is left exactly as it was.
                hold_s = None
                replay_state.mark_seen(identity, key)

            decisions.append(
                {
                    "at": at,
                    "identity": identity,
                    "key": key,
                    "segment": segment_index,
                    "status": status,
                    "kind_returned": kind_returned,
                    "verdict": verdict,
                    "hold_s": hold_s,
                    "retained": retained,
                    "sick_until_delta": sick_until_delta,
                    "source": sized_by,
                    "matched": matched,
                    "message_shape": _redact_message(str(exc)),
                    "production_kind": row.get("kind"),
                    "production_rest_s": row.get("rest_s"),
                }
            )
    finally:
        time_module.time = real_time_fn
        time_module.monotonic = real_monotonic_fn

    _fold_contradicted(replay_state)  # the last (still-current) segment

    metrics = {
        "plugin_dir": str(plugin_dir),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "isolation": {"hermes_home": str(hermes_home)},
        "adaptations": {**adaptations, "on_failure_call_mode": on_failure_call_mode},
        "input": {
            "calls_read": calls_filter_report["total_read"],
            "calls_kept": len(calls),
            "refusals_read": refusals_filter_report["total_read"],
            "refusals_kept": len(refusals),
        },
        "filtering": {
            "contaminated_window": [CONTAMINATED_WINDOW_START, CONTAMINATED_WINDOW_END],
            "calls": calls_filter_report,
            "refusals": refusals_filter_report,
        },
        "process_boundaries": {
            "total_registrations_in_logs": len(boundaries),
            "distinct_build_hashes_in_logs": len({h for _e, h in boundaries}),
            "applied_during_replay": boundaries_applied,
            "segments": segment_index + 1,
            "close_registrations_in_logs": len(close_regs),
            "close_registrations_in_replay_span": close_regs_in_span,
        },
        "matching": {
            "matched": match_counts["matched"],
            "unmatched": match_counts["unmatched"],
            "match_rate": (
                round(match_counts["matched"] / failures, 4) if failures else None
            ),
        },
        "counts": {
            "successes_replayed": successes,
            "failures_replayed": failures,
            "marked_failures": marked_failures,
            "non_marking_failures": failures - marked_failures,
        },
        "holds": hold_stats(hold_seconds, holds_by_kind, holds_by_status),
        "contradicted_holds": {
            "count": total_contradicted_count,
            "seconds_lost": round(total_contradicted_seconds_lost, 3),
        },
        "idle_with_key_available": {
            "count": len(idle_events),
            "events": idle_events,
        },
        "escalation": _aggregate_escalation(
            escalation_events, guard_available=hasattr(vocabulary_mod, "provider_timed"),
        ),
        "over_ceiling": _aggregate_over_ceiling(over_ceiling_events, OVER_CEILING_THRESHOLD_S),
        "decisions_file": None,
    }

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    decisions_path = out_path.parent / (out_path.stem + ".decisions.jsonl")
    with open(decisions_path, "w", encoding="utf-8") as handle:
        for row in decisions:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    metrics["decisions_file"] = str(decisions_path)

    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(metrics, handle, indent=2)

    return metrics


# ---------------------------------------------------------------------------
# --compare mode
# ---------------------------------------------------------------------------


def _decisions_path(metrics_json_path: Path) -> Path:
    p = Path(metrics_json_path)
    return p.parent / (p.stem + ".decisions.jsonl")


def _fmt_hold(value: Any) -> str:
    return "None" if value is None else f"{value}"


def compare_mode(path_a: Path, path_b: Path) -> str:
    a = json.loads(Path(path_a).read_text(encoding="utf-8"))
    b = json.loads(Path(path_b).read_text(encoding="utf-8"))
    decisions_a = _read_jsonl(_decisions_path(path_a))
    decisions_b = _read_jsonl(_decisions_path(path_b))

    lines: List[str] = []
    lines.append(f"Compare: A={Path(path_a).name} ({a.get('plugin_dir')})")
    lines.append(f"         B={Path(path_b).name} ({b.get('plugin_dir')})")
    lines.append("")
    lines.append("Metric deltas (A -> B):")
    for section in (
        "filtering", "process_boundaries", "matching", "counts", "holds",
        "contradicted_holds", "idle_with_key_available", "escalation", "over_ceiling",
    ):
        lines.append(f"  {section}:")
        lines.append(f"    A: {json.dumps(a.get(section))}")
        lines.append(f"    B: {json.dumps(b.get(section))}")

    n = min(len(decisions_a), len(decisions_b))
    lines.append("")
    if len(decisions_a) != len(decisions_b):
        lines.append(
            f"WARNING: decision counts differ (A={len(decisions_a)}, B={len(decisions_b)}) "
            f"— comparing the first {n} by index only."
        )

    changes = []
    for i in range(n):
        ra, rb = decisions_a[i], decisions_b[i]
        hold_a, hold_b = ra.get("hold_s"), rb.get("hold_s")
        if hold_a is None and hold_b is None:
            continue
        if hold_a is None or hold_b is None:
            delta = hold_b if hold_a is None else hold_a
            changes.append((abs(delta or 0.0), i, ra, rb))
            continue
        if round(hold_a, 3) != round(hold_b, 3):
            changes.append((abs(hold_b - hold_a), i, ra, rb))

    lines.append(f"Events compared: {n}")
    lines.append(f"Events with a changed hold_s: {len(changes)}")
    lines.append("")
    lines.append("Top 30 largest |hold_s| changes:")
    changes.sort(key=lambda t: -t[0])
    for delta, i, ra, rb in changes[:30]:
        lines.append(
            f"  #{i} at={ra.get('at')} identity={ra.get('identity')} status={ra.get('status')} "
            f"kind A={ra.get('kind_returned')!r} B={rb.get('kind_returned')!r} "
            f"hold A={_fmt_hold(ra.get('hold_s'))}s B={_fmt_hold(rb.get('hold_s'))}s delta={delta:.1f}s "
            f"verdict A={ra.get('verdict')!r} B={rb.get('verdict')!r} "
            f"matched={ra.get('matched')} shape={ra.get('message_shape', '')!r}"
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# --fidelity-build mode
# ---------------------------------------------------------------------------


def _bucket(value: Any) -> Any:
    if value is None:
        return None
    try:
        return round(float(value), 1)
    except (TypeError, ValueError):
        return value


def _rest_close(a: Any, b: Any, tolerance: float) -> bool:
    """``None`` and ``0.0`` are the SAME value here, not two different ones.

    ``calls.jsonl``'s ``rest_s`` is ``engine.next_recovery_seconds(...)``,
    which returns ``None`` specifically when the key is already available
    (``sick_until <= now``) — the exact same fact this replay's ``hold_s``/
    ``sick_until_delta`` represent as ``0.0``. Treating them as unequal would
    count "both sides say the key is free" as a disagreement.
    """
    a_val = 0.0 if a is None else a
    b_val = 0.0 if b is None else b
    try:
        return abs(float(a_val) - float(b_val)) <= tolerance
    except (TypeError, ValueError):
        return False


def fidelity_report(
    target_hash: str,
    metrics_json_path: Path,
    agent_logs: Optional[Sequence[Path]] = None,
    tolerance: float = 1.0,
) -> str:
    """Replay-vs-production agreement, restricted to ``target_hash``'s own
    process windows. ``production_kind``/``production_rest_s`` on each
    decision row are calls.jsonl's own recorded values (see ``run_replay``);
    the replay side is ``hold_s`` when this decision newly marked the key,
    falling back to ``sick_until_delta`` when it did not (a "raise" verdict
    still has a rest_s in production — whatever the key's pre-existing hold
    already was — and that is what ``sick_until_delta`` reads).
    """
    metrics = json.loads(Path(metrics_json_path).read_text(encoding="utf-8"))
    decisions = _read_jsonl(Path(metrics["decisions_file"]))

    boundaries = parse_agent_log_boundaries(agent_logs if agent_logs is not None else DEFAULT_AGENT_LOGS)
    windows = compute_hash_windows(boundaries, target_hash)

    in_window = [d for d in decisions if _in_windows(d["at"], windows)]
    total = len(in_window)
    agree = 0
    groups: Counter = Counter()

    for d in in_window:
        prod_kind = d.get("production_kind") or ""
        prod_rest = d.get("production_rest_s")
        replay_kind = d.get("kind_returned") or ""
        replay_rest = d.get("hold_s")
        if replay_rest is None:
            replay_rest = d.get("sick_until_delta")
        if prod_kind == replay_kind and _rest_close(prod_rest, replay_rest, tolerance):
            agree += 1
        else:
            groups[(prod_kind, _bucket(prod_rest), replay_kind, _bucket(replay_rest))] += 1

    lines: List[str] = []
    lines.append(f"Fidelity report for build {target_hash}")
    lines.append(f"Windows for this hash: {len(windows)} (of {len(boundaries)} total registrations in the logs)")
    shown = windows[:10]
    for start, end in shown:
        end_label = "open" if end == math.inf else f"{end:.3f}"
        lines.append(f"  window: {start:.3f} .. {end_label}")
    if len(windows) > len(shown):
        lines.append(f"  ... and {len(windows) - len(shown)} more")
    lines.append("")
    lines.append(f"Records inside window(s): {total}")
    if total:
        pct = round(100.0 * agree / total, 2)
        lines.append(f"Agreement: {agree}/{total} = {pct}%")
        lines.append(f"Target: >= 95% -> {'PASS' if pct >= 95.0 else 'FAIL'}")
    else:
        lines.append("Agreement: n/a (no decisions fall inside this hash's windows)")
    lines.append("")
    lines.append("Disagreement groups (production kind/rest vs replay kind/hold), largest first:")
    for (prod_kind, prod_rest, replay_kind, replay_rest), count in groups.most_common(50):
        lines.append(
            f"  {count:5d}x  production kind={prod_kind!r} rest={prod_rest}  "
            f"vs  replay kind={replay_kind!r} hold={replay_rest}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--plugin-dir", help="path to a hermes-kame-api-rotation folder")
    parser.add_argument("--calls", help="path to calls.jsonl (default: the real installed evidence)")
    parser.add_argument("--refusals", help="path to refusals.jsonl (default: the real installed evidence)")
    parser.add_argument("--out", help="output metrics JSON path")
    parser.add_argument("--compare", nargs=2, metavar=("A_JSON", "B_JSON"), help="compare two metrics runs")
    parser.add_argument(
        "--fidelity-build", nargs=2, metavar=("BUILD_HASH", "METRICS_JSON"),
        help="check replay-vs-production agreement for one build's process windows",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    if args.compare:
        print(compare_mode(Path(args.compare[0]), Path(args.compare[1])))
        return 0

    if args.fidelity_build:
        build_hash, metrics_json = args.fidelity_build
        print(fidelity_report(build_hash, Path(metrics_json)))
        return 0

    if not args.plugin_dir or not args.out:
        print("error: --plugin-dir and --out are required unless --compare/--fidelity-build is used", file=sys.stderr)
        return 2

    calls_path = Path(args.calls) if args.calls else DEFAULT_CALLS
    refusals_path = Path(args.refusals) if args.refusals else DEFAULT_REFUSALS
    if not calls_path.is_file():
        print(f"error: calls file not found: {calls_path}", file=sys.stderr)
        return 2
    if not refusals_path.is_file():
        print(f"error: refusals file not found: {refusals_path}", file=sys.stderr)
        return 2

    metrics = run_replay(Path(args.plugin_dir), calls_path, refusals_path, Path(args.out))
    summary = {k: v for k, v in metrics.items() if k != "idle_with_key_available"}
    summary["idle_with_key_available"] = {"count": metrics["idle_with_key_available"]["count"]}
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
