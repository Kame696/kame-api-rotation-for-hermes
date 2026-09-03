"""The live snapshot KAME leaves on disk for the Desktop plugin to read.

Why a file, and not an API. A Hermes plugin has two doors it could publish
through, and only one of them is available to a plugin that is already
installed and running:

* ``plugin_api.py`` mounted at ``/api/plugins/<name>/`` — real, but it lives
  behind ``dashboard/plugin.json`` discovery, an ``plugins.enabled`` allow-list
  entry, and a backend restart. It is the door for a plugin designed around
  a dashboard, not one a user already has running.
* a file the plugin writes and the renderer reads. The Desktop bridge exposes
  ``readFileText`` and ``watchDirectory`` to a runtime plugin (it uses them to
  load runtime plugins in the first place), so this needs nothing enabled,
  nothing restarted, and no new HTTP surface.

The second one is also the honest shape for what this is: a status readout,
written by one process, read by another, where a stale read is harmless and a
missed read costs nothing.

**No key material, ever.** The snapshot carries fingerprints
(``core.carousel.fingerprint``) and counts. It never carries a key, a prefix of
a key, or the contents of ``.env`` — the same rule the log lines follow, for
the same reason: this file is meant to be readable, and a readable file that
holds credentials is a leak with a UI.

The write is atomic (temp file then ``os.replace``) because the reader polls:
a half-written JSON document read at the wrong moment would show the user an
error where the truth is "nothing has changed".
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .core import __version__

logger = logging.getLogger(__name__)

#: The plugin's own name, which is also its directory name under both
#: ``plugins/`` and ``plugin-data/``. Hard-coded rather than derived from
#: ``__package__`` because the package name is underscored by the loader
#: (``hermes_plugins.hermes_kame_api_rotation``) and the directory is not.
PLUGIN_ID = "hermes-kame-api-rotation"

#: Schema version of the document below. The Desktop plugin refuses a document
#: it does not understand rather than half-rendering it, so this number and the
#: check that reads it are a pair — bump both or neither.
#:
#: 2 (1.1.1): settings rows gained everything an editor needs (title, help,
#: bounds, units), pools gained ``invalid`` and ``idle_for``, and the document
#: gained ``events``, ``control`` and ``first_run``.
#: 3 (1.2.0): settings rows gained ``group``, and the document gained
#: ``setting_groups`` — the titles and the one-sentence explanation of each
#: shelf, so the panel lays the screen out from the process that owns the
#: settings rather than from a copy of the same decisions written in
#: JavaScript.
#: 4 (1.2.2): the document gained ``settings_pending_restart`` — the settings
#: whose ``config.yaml`` entry has been edited since this Hermes read it, so
#: the panel can say "restart to apply" instead of showing a value the user
#: has already changed and letting them conclude the edit was wrong.
#: 5 (1.4.0): the document gained ``build`` — whether every module this plugin
#: needs is actually on disk, a fingerprint of the source tree that is there,
#: and where the host-guidance blocks were resolved from. Events gained
#: ``detail`` (the provider's payload, redacted on the way in) and
#: ``sized_by`` (where the cooldown came from). The first exists because an
#: install missing its whole ``core/`` package reported ``installed: true,
#: reason: "active"`` for nine days; the second because two thirds of every
#: cooldown in that period was a guess and nothing said so.
SCHEMA = 5

#: Floor between two writes of an *unchanged* document. A changed document is
#: always written immediately: the whole point is that the chip moves when the
#: pool moves. This only stops a settled pool from rewriting the same bytes on
#: every call.
_MIN_INTERVAL_S = 1.0

_lock = threading.Lock()
_last_written: Optional[str] = None
_last_write_at = 0.0
_disabled_reason = ""

#: The live binding, remembered once at registration so anything that has a
#: reason to republish — the control poller, the heartbeat, a command — can do
#: it without carrying the binding around. Before 1.1.1 a caller with no
#: binding to hand published ``installed: false`` over a perfectly healthy
#: snapshot, and the panel showed the plugin as missing until the next call.
_binding: Any = None
#: The pool binding, for the credential picture. See ``set_pool_binding``.
_pool_binding: Any = None


def attach(binding: Any) -> None:
    """Remember the installed binding for every later publish."""
    global _binding
    _binding = binding


def attached() -> Any:
    return _binding


def _hermes_home() -> Optional[Path]:
    """The base Hermes home, asked of Hermes rather than guessed at.

    A profile-scoped home (``.../profiles/<name>``) is walked back up to its
    base, because the Desktop reads this file relative to the plugin root the
    bridge reports, and that root is the base home. Publishing under a profile
    directory would leave the chip permanently empty for anyone not on the
    default profile — which is the one case where a wrong answer here is
    invisible to me and visible to them.
    """
    home: Optional[Path] = None
    try:
        from hermes_constants import get_hermes_home

        home = Path(get_hermes_home())
    except Exception:
        logger.debug("kame: no hermes_constants; falling back to the environment", exc_info=True)
        raw = os.environ.get("HERMES_HOME", "").strip()
        if raw:
            home = Path(raw)
    if home is None:
        return None
    # Each profile keeps its own plugin-data directory. Do NOT walk to the
    # base home — that collapsed all profiles to the same state.json and
    # caused races between concurrent profiles (fixed v1.2.4).
    return home


def state_dir() -> Optional[Path]:
    home = _hermes_home()
    return None if home is None else home / "plugin-data" / PLUGIN_ID


def state_path() -> Optional[Path]:
    directory = state_dir()
    return None if directory is None else directory / "state.json"


def disabled_reason() -> str:
    """Empty while publishing works; otherwise why it does not.

    Surfaced by ``/kame`` so "the chip is not moving" has an answer that is not
    a guess.
    """
    return _disabled_reason


def _pool_rows() -> List[Dict[str, Any]]:
    try:
        from .core.carousel import ENGINE

        pools: Dict[str, Dict[str, Any]] = ENGINE.snapshot()
    except Exception:
        logger.debug("kame: could not read the pool for the snapshot", exc_info=True)
        return []
    rows: List[Dict[str, Any]] = []
    for identity in sorted(pools):
        row = pools[identity]
        rows.append(
            {
                "identity": identity,
                "keys": int(row.get("keys", 0)),
                "healthy": int(row.get("healthy", 0)),
                "resting": int(row.get("resting", 0)),
                # Seconds until the first key comes back, or null when one is
                # usable now. The renderer turns it into a clock; keeping it a
                # number here means a snapshot read one second late is one
                # second wrong rather than a minute wrong.
                "soonest_s": row.get("soonest"),
                "successes": int(row.get("successes", 0)),
                "failures": int(row.get("failures", 0)),
                "kinds": list(row.get("kinds") or []),
                # Keys the provider refused as credentials. Counted apart from
                # ``resting`` because waiting does not fix one: the panel says
                # "replace this key" rather than showing a countdown that will
                # come back round for ever.
                "invalid": int(row.get("invalid", 0)),
                "invalid_keys": list(row.get("invalid_keys") or []),
                # Seconds since this pool was last asked for a key, or null if
                # it never has been. The status bar shows what is in use and
                # leaves out what is not.
                "idle_for_s": row.get("idle_for"),
            }
        )
    return rows


def _totals(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """One pair of numbers for the status bar, across every pool.

    The chip has room for one fraction. Summing the pools is the only honest
    way to produce it: showing the first pool would silently pick a winner,
    and showing nothing until there is exactly one pool would leave the chip
    blank for anyone using a main model and an auxiliary one.
    """
    keys = sum(r["keys"] for r in rows)
    healthy = sum(r["healthy"] for r in rows)
    waiting = [r["soonest_s"] for r in rows if r["soonest_s"] is not None]
    return {
        "keys": keys,
        "healthy": healthy,
        "resting": keys - healthy,
        # The soonest across every pool that has nothing usable. A pool with a
        # healthy key reports null, so this is null whenever any lane can still
        # be served — which is exactly when an ETA would be a lie.
        "soonest_s": min(waiting) if waiting else None,
    }


def snapshot(binding: Any = None, activity: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The whole document. Pure — builds, never writes."""
    rows = _pool_rows()
    counters = {
        "calls": int(getattr(binding, "calls", 0) or 0),
        "rotations": int(getattr(binding, "rotations", 0) or 0),
        "recovered": int(getattr(binding, "recovered", 0) or 0),
        "surfaced": int(getattr(binding, "surfaced", 0) or 0),
        "waits": int(getattr(binding, "waits", 0) or 0),
        "waited_s": float(getattr(binding, "waited_s", 0.0) or 0.0),
        "mid_stream_cuts": int(getattr(binding, "mid_stream_cuts", 0) or 0),
        # 1.1.1. ``stream_drops`` is how often a provider stopped in the
        # middle of an answer; ``resumes`` is how often KAME carried it on
        # elsewhere; ``stitched`` is how often the result was handed back as
        # one continuous response. ``mid_stream_cuts`` keeps its old meaning —
        # a cut the user was actually shown — so the two can be compared.
        "stream_drops": int(getattr(binding, "stream_drops", 0) or 0),
        "resumes": int(getattr(binding, "resumes", 0) or 0),
        "stitched": int(getattr(binding, "stitched", 0) or 0),
        # 1.1.3. The drops nothing can continue, because the stream stopped
        # inside a tool call. Counted apart from the rest: they are the ones
        # that end a turn whatever KAME does, so a pool where this number
        # climbs is a pool with a key that cannot hold a long stream.
        "tool_call_cuts": int(getattr(binding, "tool_call_cuts", 0) or 0),
        # 1.6.0.0. The same drop caught before it reached Hermes and became
        # "your previous tool call was too large — do NOT retry". A number
        # climbing here is a number *not* climbing above it.
        "tool_call_retries": int(getattr(binding, "tool_call_retries", 0) or 0),
    }
    return {
        "schema": SCHEMA,
        "version": __version__,
        # Wall clock, so the renderer can say how old the reading is. A stale
        # file is the one failure mode a poller cannot detect any other way:
        # a Hermes that died mid-turn leaves a snapshot that looks live.
        "updated_at": time.time(),
        "pid": os.getpid(),
        "installed": binding is not None,
        "reason": str(getattr(binding, "reason", "not installed")),
        # 1.4.0. `installed` and `reason` describe *registration*, and for nine
        # days they said `true` / `"active"` on an install whose `core/`
        # package was not on disk at all — true about registration, worthless
        # about function, and the only thing anybody could see. `build` is
        # computed from the bytes actually present, so it cannot describe files
        # that are missing, and `complete` is the field that says whether this
        # plugin can do its job. See `integrity.py`.
        "build": _integrity(),
        "counters": counters,
        "pools": rows,
        "totals": _totals(rows),
        # What KAME is doing *right now*, or null between calls. The chip reads
        # this to say "on key 3" while a turn is running and fall back to plain
        # health when nothing is in flight.
        "activity": activity,
        "gemini_tool_call_fix": _gemini_fix_state(),
        # 1.6.0.0. The panel's answer to "is it even seeing my keys?".
        #
        # Everything above describes what KAME *did*. Nothing described what
        # it is looking at, and that is the question every report about this
        # plugin has actually been: a profile where the key separation
        # "doesn't seem to work", a provider that says the API key is wrong.
        # Both are answerable in one line each — how many rows the pool has,
        # how many keys those rows turned out to be, and where they came
        # from — and neither was anywhere on screen.
        #
        # Counts and origins only. Never a key, never a prefix of one.
        "credentials": _credential_rows(),
        "settings": _settings_rows(),
        # 1.2.0. The shelves the rows above are sorted onto, titled and
        # explained here rather than in the panel: the reason one setting is
        # optional and another is an escape hatch is a fact about the plugin,
        # and a copy of it in JavaScript is a copy that will disagree.
        "setting_groups": _setting_groups(),
        # 1.2.2. The settings whose config.yaml entry has been edited since
        # this Hermes started, and which therefore still hold the value they
        # were read with. The panel says so rather than leaving somebody to
        # decide, from a screen that shows the old number, whether their edit
        # was wrong or merely not yet in force.
        "settings_pending_restart": _settings_pending_restart(),
        "desktop_ui": _desktop_ui_state(),
        # The last fifty decisions, newest first, so the panel's Events screen
        # can answer "why did that stall" without anyone opening a log file.
        "events": _event_rows(),
        # What became of the last thing the panel asked for. The panel writes a
        # request and has no other way to learn whether it was accepted.
        "control": _control_state(),
        # True when there is nothing to rotate yet. The panel shows a short
        # "how to start" page instead of a wall of zeroes.
        "first_run": not rows,
    }


def _credential_rows() -> Dict[str, Any]:
    """What KAME can see of the pool, per provider, without reading a key.

    Three numbers per provider and one word about where they came from:

    ``rows``
        What the host stored — one per credential *entry*, which for a
        multi-key environment variable is one row holding several keys.
    ``keys``
        What those rows are after KAME splits them. ``keys > rows`` is the
        splitter working; ``keys == rows`` on a provider whose variable holds
        a comma list is the report that started this, visible instead of
        inferred.
    ``benched``
        How many of those the host currently considers spent.

    ``origin`` names the source vocabulary the host used — ``env``,
    ``config``, ``manual``, ``gh_cli`` — because "I put my keys in the new
    profile" and "the new profile has a stored row from somewhere else" look
    identical from the outside and are different problems.

    Read from what the pool binding recorded the last time it touched each
    pool, because there is nothing to enumerate at snapshot time:
    ``load_pool()`` builds a new object per call and Hermes keeps no registry
    of them. Numbers only — no entry is held and no key is stored.
    """
    binding = globals().get("_pool_binding")
    installed = binding is not None and getattr(binding, "installed", False)
    out: Dict[str, Any] = {
        "readable": False,
        "reason": "the pool binding is not installed",
        "splitting": bool(getattr(binding, "splitting_multikey", False)),
        "guarding_container": bool(getattr(binding, "guarding_current", False)),
        "providers": [],
    }
    if not installed:
        return out
    try:
        seen = dict(getattr(binding, "seen_pools", {}) or {})
        out["providers"] = [
            {
                "provider": str(row.get("provider", "")),
                "rows": int(row.get("rows", 0) or 0),
                "keys": int(row.get("keys", 0) or 0),
                "benched": int(row.get("benched", 0) or 0),
                "origin": str(row.get("origin", "") or ""),
                "split": bool(row.get("split", False)),
            }
            for row in sorted(seen.values(), key=lambda r: str(r.get("provider", "")))
        ]
        out["readable"] = True
        out["reason"] = "" if out["providers"] else "no pool has been used yet"
    except Exception:
        logger.debug("kame: could not read the credential pools", exc_info=True)
        out["reason"] = "the pool could not be read"
    return out


def set_pool_binding(binding: Any) -> None:
    """Remember the pool binding, so the snapshot can ask it what it sees.

    Separate from ``remember`` above, which holds the dispatch binding: the
    two are different objects with different jobs, and the counters come from
    one while the credential picture comes from the other.
    """
    global _pool_binding
    _pool_binding = binding


def _settings_rows() -> List[Dict[str, Any]]:
    """Every KAME setting, described well enough for the panel to edit it.

    Read-only until 1.1.1, when ``control.py`` gave the page a way back and
    ``settings.describe_all`` gave it something to render: the title, the
    one-sentence explanation, the value, the default, where the value came
    from, the range, and whether changing it is consequential enough to ask
    first. Everything the editor needs is decided here, in the process that
    owns the settings, rather than restated in JavaScript where it would drift.
    """
    try:
        from . import settings as _settings

        return list(_settings.describe_all())
    except Exception:
        logger.debug("kame: could not read settings for the snapshot", exc_info=True)
        return []


def _settings_pending_restart() -> List[str]:
    """Settings edited in ``config.yaml`` since Hermes read it.

    Empty on every host without a config surface, and empty — deliberately —
    for a setting the environment owns, since restarting would not apply that
    edit either. See ``settings.pending_restart``.
    """
    try:
        from . import settings as _settings

        return list(_settings.pending_restart())
    except Exception:
        logger.debug("kame: could not check the config for edits", exc_info=True)
        return []


def _setting_groups() -> List[Dict[str, Any]]:
    """The shelves, so the panel can title and explain each card."""
    try:
        from . import settings as _settings

        return list(_settings.groups())
    except Exception:
        logger.debug("kame: could not read the setting groups", exc_info=True)
        return []


def _event_rows() -> List[Dict[str, Any]]:
    """The recent decisions. Fingerprints and counts only — never a key."""
    try:
        from .core.events import EVENTS

        return EVENTS.recent()
    except Exception:
        logger.debug("kame: could not read the events for the snapshot", exc_info=True)
        return []


def _control_state() -> Dict[str, Any]:
    """What happened to the panel's last request, so it can stop waiting."""
    try:
        from . import control

        return control.last_result()
    except Exception:
        logger.debug("kame: could not read the control result", exc_info=True)
        return {}


def _integrity() -> Dict[str, Any]:
    """What the install actually contains, for every reader of the snapshot.

    Read from the value `register()` computed rather than recomputed here: the
    fingerprint walks the package directory, and the snapshot is written on a
    twenty-second heartbeat. Hashing every file on every tick to answer a
    question whose answer cannot change without a restart would be a lot of
    disk for nothing.
    """
    try:
        from . import _INTEGRITY, host_text

        return {
            "complete": bool(_INTEGRITY.get("complete", True)),
            "fingerprint": str(_INTEGRITY.get("fingerprint") or ""),
            "missing": list(_INTEGRITY.get("missing_required") or []),
            # Where the host's appended-guidance blocks came from. A plugin
            # silently running on fallback literals is one Hermes reword away
            # from reading its own host's handwriting as evidence, which is the
            # bug that benched fourteen keys for an hour at a time.
            "guidance": host_text.guidance_source(),
        }
    except Exception:
        logger.debug("kame: could not read the install report", exc_info=True)
        return {"complete": True, "fingerprint": "", "missing": [], "guidance": ""}


def _desktop_ui_state() -> Dict[str, Any]:
    """Whether this release's UI file reached the door Desktop loads from.

    Read by nothing on screen -- the panel that would show it is the panel
    that is missing when this is False. It is here for the log, for `/kame`,
    and for the next person asking why the chip did not appear.
    """
    try:
        from . import desktop_ui

        return desktop_ui.report()
    except Exception:
        logger.debug("kame: could not read the desktop install state", exc_info=True)
        return {"installed": False, "reason": "unavailable", "path": ""}


def _gemini_fix_state() -> Dict[str, Any]:
    try:
        from . import gemini_slots

        return gemini_slots.report()
    except Exception:
        logger.debug("kame: could not read the Gemini patch state", exc_info=True)
        return {"applied": False, "reason": "unavailable", "repaired": 0}


def publish(
    binding: Any = None,
    *,
    activity: Optional[Dict[str, Any]] = None,
    force: bool = False,
) -> bool:
    """Write the snapshot. Returns whether bytes reached the disk.

    Never raises. This is called from the rotation path, and a status readout
    that can break a chat turn is worse than no status readout at all — the
    whole reason the live line went through ``thinking.delta`` in the first
    place.
    """
    global _last_written, _last_write_at, _disabled_reason
    if binding is None:
        # A caller with nothing to hand means "publish what is installed", not
        # "publish that nothing is installed".
        binding = _binding
    path = state_path()
    if path is None:
        _disabled_reason = "this Hermes exposes no home directory"
        return False
    try:
        document = json.dumps(snapshot(binding, activity), sort_keys=True)
    except Exception as exc:
        _disabled_reason = f"could not build the snapshot: {type(exc).__name__}"
        logger.debug("kame: could not build the snapshot", exc_info=True)
        return False

    now = time.monotonic()
    with _lock:
        # `updated_at` changes on every build, so comparing whole documents
        # would never match. Compare everything else.
        comparable = _without_clock(document)
        if not force and comparable == _last_written and (now - _last_write_at) < _MIN_INTERVAL_S:
            return False
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic: the reader polls, and a torn read would show an error
            # where the truth is "nothing changed".
            handle, temporary = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    stream.write(document)
                os.replace(temporary, path)
            except BaseException:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
                raise
        except Exception as exc:
            _disabled_reason = f"could not write {path}: {type(exc).__name__}"
            logger.debug("kame: could not write the snapshot", exc_info=True)
            return False
        _last_written = comparable
        _last_write_at = now
        _disabled_reason = ""
    _sweep_once(path.parent)
    return True


# Stale temporary files are swept at most once per process. Orphans only ever
# accumulate across restarts, so a scan per publish would be a directory
# listing on the rotation path in exchange for nothing.
_swept = False


def _sweep_once(directory: Any) -> None:
    """Remove temporary files a previous run never got to rename.

    The write above is atomic and cleans up after itself on every exception.
    What it cannot clean up after is not running: ``os.replace`` is the last
    statement, and a process killed between ``mkstemp`` and that line leaves
    the temporary behind with nobody left to unlink it.

    That is not hypothetical. The owner's own plugin-data directory holds six
    of them — three empty, three with a snapshot in them, 40 KB in total —
    one per time Hermes Desktop was closed or restarted mid-write. Nothing
    ever removed them and nothing ever would have.

    Two rules keep this from touching a file that is still in use:

    * only names ``mkstemp`` produces, in KAME's own state directory;
    * only files older than a minute, which is far longer than the gap this
      leaks in and means a second Hermes process writing right now is safe.

    Failures are ignored on purpose. This runs after the snapshot has already
    been written, and tidying is never worth a raised exception on the
    rotation path.
    """
    global _swept
    if _swept:
        return
    _swept = True
    try:
        cutoff = time.time() - 60.0
        for candidate in directory.glob("tmp*.tmp"):
            try:
                if candidate.is_file() and candidate.stat().st_mtime < cutoff:
                    candidate.unlink()
            except OSError:
                continue
    except Exception:  # pragma: no cover - a listing that cannot be done
        logger.debug("kame: could not sweep stale snapshot temporaries", exc_info=True)


def _without_clock(document: str) -> str:
    """The document minus the fields that change on every build.

    Used only for the "has anything actually changed" test. Parsing and
    re-serialising is cheap next to the file write it avoids.
    """
    try:
        loaded = json.loads(document)
        loaded.pop("updated_at", None)
        return json.dumps(loaded, sort_keys=True)
    except Exception:
        return document


def clear() -> None:
    """Forget the write throttle. For tests, and for a pool that was replaced."""
    global _last_written, _last_write_at
    with _lock:
        _last_written = None
        _last_write_at = 0.0
