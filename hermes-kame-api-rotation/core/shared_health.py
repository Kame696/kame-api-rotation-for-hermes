"""One pool-health file, read and written by every Hermes profile that shares it.

Why this exists — measured, not re-derived (``decisions/0006-1800-one-pool-
three-profiles.md``). The owner runs three Hermes profiles, ``base`` and
``profiles/k`` and ``profiles/lo1``, whose ``.env`` files hold the SAME
seventeen physical keys (checked by hash: every pairwise intersection equals
the union). Each profile's :class:`core.carousel.Carousel` keeps its own
health memory, so ``base`` can bench a key for twenty seconds believing it is
resting while ``k`` calls the same key in the same second, gets refused again,
and learns the same wrong lesson independently. A second, equally expensive
gap sits beside it: the carousel lives only in the process's memory, so
restarting Hermes throws away every hold and the next turn spends real calls
on keys the previous process already knew were out.

This module is the fix for both at once: a file outside any one profile's
``plugin-data``, at the root every profile can reach, that the carousel reads
before answering "is this key healthy" and writes to after every outcome.
Newest **event** wins reconciliation (not newest **write** — see
:func:`_write_locked`), so a success recorded by one profile releases a bench
another profile is still holding, in whichever order the two writes land.

What this deliberately is **not**: a source of truth about quota (the
provider owns that; this only stops two profiles from lying to each other
about it), a message bus, or a database. It does nothing when only one
profile is running, and it is inert — no file created, no file read — unless
:func:`enabled` says otherwise: on by default since the continuity gate (G4)
passed (``settings.DEFAULTS_ON``, pushed in by the host layer — see
:func:`set_config_default`), ``KAME_SHARE_POOL_HEALTH`` still able to force
it off by hand. RED_TEAM.md F4 (2026-09-19): before :func:`set_config_default`
existed, this module read only the environment variable, so an unset
environment always meant off — a fresh install shipped the feature dark
however ``settings.py``/``plugin.yaml`` described the default.

Framework-free like the rest of ``core``: no logging, and the only clock used
is the one every public method is handed. A write takes an exclusive,
non-blocking lock file and gives up **silently** if it is not free almost at
once (:data:`_LOCK_SPIN_S`) — bookkeeping must never hold up a turn, and a
lost write costs at most one refused request somewhere else, which is the
same asymmetry every cooldown in :mod:`core.carousel` is already built on. A
read is cached by the file's own ``mtime`` — re-parsed only when ``stat()``
says the bytes moved, which is what lets a fresher write anywhere become
visible to a caller on its very next check rather than up to half a second
late. The ``stat()`` call itself is still floored (:data:`READ_STAT_FLOOR_S`,
1.8.0.1) against pathological call rates, because :meth:`SharedHealth.
model_until` and :meth:`SharedHealth.account_until` run on ``select``'s hot
path and a syscall per candidate key per turn is not free — but the floor
gates the *stat*, not the *freshness*: a change on disk older than the floor
is picked up on the very next check, not up to ``READ_STAT_FLOOR_S`` late.

Measured before this shipped (``tools/continuity_gate.py``, scenario 2, real
processes, not threads): with the clock-driven predecessor of this cache
(fixed at twice a second, ``READ_CACHE_S = 0.5``, regardless of how often the
file actually changed), sharing ON still measurably reduced but did not
eliminate cross-process double-burns. Diagnosing the residual (own script,
not committed — the wall-clock gap between the refusal in one process and
the offending call in another, for every residual pair) put 100% of the
survivors' gaps under 0.09s: comfortably inside the old 0.5s cache window,
nowhere near a real race on the file lock. The residual was the cache being
clock- rather than mtime-driven, not contention or a lost write.
`research/1.8.0.0/continuity/double_burn.json` carries the measured
before/after pair for the run that shipped this change.

**Never a raw key.** Every entry is keyed by ``core.carousel.fingerprint`` —
a truncated, non-reversible hash — exactly as every other on-disk or on-log
surface in this plugin already is (I7, R37). A key that is not fingerprinted
before it reaches this module is a bug in the caller, not in this one.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

#: Bumped only if the document's shape changes incompatibly. A file this
#: build does not recognise is ignored rather than guessed at (decisions/0006,
#: risk 4: "a profile running a different Hermes version" reads an unknown
#: schema as nothing, not as license to misinterpret it).
SCHEMA = 1

#: Duplicated from ``state.PLUGIN_ID`` rather than imported. ``core`` imports
#: nothing from the plugin root — see the acyclic note in ``carousel.py`` —
#: and the two names have to be kept in sync by hand, the same way
#: ``carousel.MAX_HOLD_S`` and ``settings.ALL_NUMBERS[settings.MAX_HOLD]``
#: already are.
PLUGIN_ID = "hermes-kame-api-rotation"

#: The escape-hatch environment variable. Read directly here rather than
#: through ``settings.is_on`` — ``core`` has no import of the host layer, the
#: same reason this module carries its own copy of ``PLUGIN_ID`` — so the
#: config.yaml half of this switch lives in ``settings.py``/``plugin.yaml``
#: for discovery and the panel, and the environment is what this module
#: itself actually consults first, matching the "environment always wins"
#: rule ``settings.py``'s own docstring states for every other switch here.
ENV_ENABLE = "KAME_SHARE_POOL_HEALTH"

#: RED_TEAM.md F4 (2026-09-19): what ``settings.is_on(settings.
#: SHARE_POOL_HEALTH)`` last resolved to, pushed in once by the host layer
#: at registration (``dispatch_binding.install()``, the same place
#: ``max_hold_s``/``daily_cooldown_s`` are pushed into the carousel) via
#: :func:`set_config_default`. Before this existed, :func:`enabled` read
#: only :data:`ENV_ENABLE` — an unset environment answered ``False``
#: unconditionally, even though ``settings.DEFAULTS_ON`` had carried this
#: switch ON since G4 passed and ``plugin.yaml`` shipped ``default: true``.
#: A fresh install with nothing in the environment therefore shipped the
#: feature OFF while every document describing it said ON.
#:
#: ``None`` means "nothing has been pushed" — a test constructing
#: :class:`SharedHealth` directly, or any process that never calls
#: :func:`set_config_default`, reads the same ``False`` this module has
#: always defaulted to. This stays a plain module-level value rather than a
#: ``settings`` import for the reason :data:`ENV_ENABLE` already gives:
#: ``core`` has no import of the host layer, so the value has to be pushed
#: in rather than pulled.
_CONFIG_DEFAULT: Optional[bool] = None


def set_config_default(value: Optional[bool]) -> None:
    """Record what the host layer's settings resolved this switch to.

    Called once at registration, not on every check — the same convention
    ``daily_cooldown_s``/``max_hold_s`` already use to reach the carousel
    from ``settings.py`` without ``core`` importing it back. Does not
    override :data:`ENV_ENABLE`: :func:`enabled` still checks the live
    environment first on every call, so the escape hatch keeps working even
    in a process that never re-registers after the variable changes.
    """
    global _CONFIG_DEFAULT
    _CONFIG_DEFAULT = value


#: Overrides the derived path outright. Not part of the ``settings.py``
#: number/flag machinery (there is no string type there); a raw environment
#: read, the same tier every other KAME switch answers to first.
ENV_PATH_OVERRIDE = "KAME_POOL_HEALTH_PATH"

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})

#: Entries whose event is older than this are pruned on every write. A day,
#: matching the absolute sanity bound every cooldown in this plugin already
#: respects (``carousel.HARD_DELAY_CAP_S``) — nothing this module stores is
#: ever supposed to matter for longer than the longest hold it can describe.
PRUNE_AFTER_S = 86400.0

#: Caps the number of rows across both scopes after pruning. Headroom over
#: the owner's own shape (17 keys x a handful of models x two scopes is
#: comfortably under a thousand rows) without being an invitation to grow
#: without bound if a caller ever mis-keys entries.
MAX_ENTRIES = 4096

#: How often the file's ``mtime`` may be RE-CHECKED — not how long a change
#: may take to become visible; those are different questions since 1.8.0.1.
#: ``select`` calls this on every candidate key, so the check has to be
#: nearly free; a ``stat()`` call is, and re-parsing the JSON only happens
#: when the stat says the bytes actually moved. 50ms rather than the
#: original 500ms: `tools/continuity_gate.py` scenario 2 measured every
#: residual cross-process double-burn's gap under 0.09s at the old value —
#: see the module docstring — so 500ms was long enough to serve a stale
#: answer to the exact hot path this cache exists to protect. 50ms is a
#: guard against pathological call rates, not a target staleness window.
READ_STAT_FLOOR_S = 0.05

#: How long a write waits for the lock file before giving up. "Almost
#: immediately" — decisions/0006 is explicit that bookkeeping must never hold
#: a turn, so this is a spin measured in milliseconds, not a real wait.
_LOCK_SPIN_S = 0.05
_LOCK_POLL_S = 0.005

#: A lock file older than this is assumed to belong to a process that died
#: mid-write rather than one still holding it, and is reclaimed. Comfortably
#: longer than any write this module performs (one small JSON document),
#: short enough that a genuine crash does not wedge the file for long.
_LOCK_STALE_S = 5.0

#: RED_TEAM.md F7 (2026-09-19): ``_prune`` age-checks every OTHER row
#: against the ``at`` of the event currently being written — deliberately,
#: so a slow write does not look freshly earned (see :func:`_prune`'s own
#: docstring). That is correct when ``at`` is an ordinary wall-clock moment
#: from the same machine. It stops being correct the moment ``at`` is
#: implausibly far ahead of wall-clock time: every genuine row then reads as
#: older than :data:`PRUNE_AFTER_S` relative to it and is dropped in one
#: write. Measured: a single event with ``at = now + 2 days`` pruned three
#: live entries down to zero. A forward clock/NTP step, a VM resume, or any
#: caller writing a synthetic ``at`` can produce this; every profile then
#: forgets every hold at once and re-learns by burning the calls the file
#: exists to avoid. A few minutes of slack is generous headroom for ordinary
#: clock drift between profiles on the same machine while still catching a
#: jump large enough to matter — this module's own writes are always
#: ``at=now`` from the caller's own clock (``Carousel.mark``), so a healthy
#: process never comes close to it.
_PRUNE_HORIZON_SLACK_S = 300.0


def _flag(value: Optional[str]) -> Optional[bool]:
    if value is None:
        return None
    text = value.strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return None


def enabled() -> bool:
    """Whether the shared file may be read or written at all, right now.

    Read fresh on every call, the same convention ``settings.is_on`` uses for
    every other switch in this plugin — this one just cannot go through that
    function without importing the host layer into ``core``. Environment
    first, exactly like every other switch: an explicit ``KAME_SHARE_POOL_
    HEALTH`` always wins. Only when the environment says nothing at all does
    this fall back to :data:`_CONFIG_DEFAULT` — what ``settings.is_on``
    itself resolved to (environment, then config, then ``DEFAULTS_ON``) the
    last time :func:`set_config_default` was called.

    RED_TEAM.md F4 (2026-09-19): before this fallback existed, an unset
    environment always answered ``False`` here, whatever ``settings.py``'s
    ``DEFAULTS_ON`` or ``plugin.yaml``'s ``default: true`` said — a fresh
    install shipped the feature dark while the panel and the release notes
    both said it was on.
    """
    from_env = _flag(os.environ.get(ENV_ENABLE))
    if from_env is not None:
        return from_env
    return bool(_CONFIG_DEFAULT)


def _root_and_profile(home: Path) -> Tuple[Path, str]:
    """``(<root>, <profile name>)`` for one ``HERMES_HOME`` value.

    A profile-scoped home ends ``.../profiles/<name>`` (observed on the
    owner's machine: ``base`` is the home itself, ``k`` and ``lo1`` are
    ``profiles/k`` and ``profiles/lo1`` under it — ``audit/1.8.0.0-02-
    inventario-evidencia-real.md``). Going up two levels from a profile home
    reaches the base home, which is the one directory every profile shares —
    the whole reason this file does not live beside any one profile's own
    ``plugin-data``.

    Deliberately **not** ``state._hermes_home``'s walk: that function keeps
    each profile's ``plugin-data/<id>/state.json`` separate on purpose
    (v1.2.4 fixed a race from collapsing them), which is the opposite of what
    this file is for. The two functions look similar and must not be merged.
    """
    parts = home.parts
    if len(parts) >= 2 and parts[-2].lower() == "profiles":
        return home.parent.parent, parts[-1]
    return home, ""


def default_path() -> Optional[Path]:
    """``<root>/plugin-data/<PLUGIN_ID>/pool-health.json``, or ``None``.

    ``None`` means nothing here can say where the shared root is — no
    ``HERMES_HOME``, no override — and every caller treats that exactly like
    "the switch is off": local memory only, nothing written, nothing read.
    """
    override = os.environ.get(ENV_PATH_OVERRIDE, "").strip()
    if override:
        return Path(override)
    raw = os.environ.get("HERMES_HOME", "").strip()
    if not raw:
        return None
    root, _profile = _root_and_profile(Path(raw))
    return root / "plugin-data" / PLUGIN_ID / "pool-health.json"


def _profile_name() -> str:
    raw = os.environ.get("HERMES_HOME", "").strip()
    if not raw:
        return ""
    _root, profile = _root_and_profile(Path(raw))
    return profile or "base"


def _fresh_document() -> Dict[str, Any]:
    return {"schema": SCHEMA, "model": {}, "account": {}}


def _read_document(path: Path) -> Dict[str, Any]:
    """The document on disk, or a fresh one for anything short of valid.

    Never raises. A missing file, a file another process is mid-write on, an
    empty file, bytes that are not JSON, JSON that is not this shape, or a
    schema this build does not know: every one of them reads as "nothing
    shared yet" rather than as a reason to stop. See the module docstring —
    this is the "ignore and continue with local memory only" half of the
    design.
    """
    try:
        raw_text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # 1.8.1.5: bytes that are not UTF-8 raised straight past ``OSError``,
        # and since every write reads first, no write could ever replace them
        # -- sharing stayed off for good and *Clear pool* reported failure.
        return _fresh_document()
    try:
        document = json.loads(raw_text)
    except ValueError:
        return _fresh_document()
    if not isinstance(document, dict) or document.get("schema") != SCHEMA:
        return _fresh_document()
    return {
        "schema": SCHEMA,
        "model": _usable_rows(document.get("model")),
        "account": _usable_rows(document.get("account")),
    }


def _usable_rows(bucket: object) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """Only the rows every reader and writer below can do arithmetic on.

    1.8.1.5: one damaged row -- a subject holding a list, an ``at`` that is
    text or an object, a ``NaN`` (which ``json`` accepts) -- raised inside the
    prune or the newest-event comparison of *every* later write, so the file
    was never rewritten and one bad row turned sharing off for good. A damaged
    row now costs that row: it is dropped, and the next write heals the file.
    """
    usable: Dict[str, Dict[str, Dict[str, Any]]] = {}
    if not isinstance(bucket, dict):
        return usable
    for subject, rows in bucket.items():
        if not isinstance(rows, dict):
            continue
        kept = {}
        for fingerprint_key, row in rows.items():
            if not isinstance(row, dict):
                continue
            try:
                until = float(row.get("until", 0.0) or 0.0)
                at = float(row.get("at", 0.0) or 0.0)
            except (TypeError, ValueError):
                continue
            if math.isfinite(until) and math.isfinite(at):
                kept[fingerprint_key] = row
        if kept:
            usable[subject] = kept
    return usable


def _write_document(path: Path, document: Dict[str, Any]) -> None:
    """Temp file then ``os.replace`` — the same atomic pattern ``state.py``
    already uses for the same reason: a reader polling this file must never
    see a half-written document.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(document, stream, sort_keys=True)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _prune(document: Dict[str, Any], now: float) -> None:
    """Drop anything older than :data:`PRUNE_AFTER_S`, then cap the count.

    Age is judged by each entry's own ``at`` — the event's own moment — never
    by when it happened to be written, for the same reason reconciliation
    below reads ``at``: a slow write landing late must not look freshly
    earned just because the bytes are new.
    """
    for scope in ("model", "account"):
        bucket = document.get(scope) or {}
        for subject in list(bucket):
            rows = bucket.get(subject)
            if not isinstance(rows, dict):
                bucket.pop(subject, None)
                continue
            for fingerprint_key in list(rows):
                row = rows.get(fingerprint_key)
                if not isinstance(row, dict) or now - float(row.get("at", 0.0) or 0.0) > PRUNE_AFTER_S:
                    rows.pop(fingerprint_key, None)
            if not rows:
                bucket.pop(subject, None)

    all_rows = [
        (float(row.get("at", 0.0) or 0.0), scope, subject, fingerprint_key)
        for scope in ("model", "account")
        for subject, rows in (document.get(scope) or {}).items()
        for fingerprint_key, row in (rows or {}).items()
        if isinstance(row, dict)
    ]
    if len(all_rows) <= MAX_ENTRIES:
        return
    # Oldest `at` first, evicted until the cap holds — a runaway writer (a
    # bug, never the owner's own pool) must not grow this file without bound
    # between prunes, and the oldest evidence is the least likely to still
    # matter to anyone reading it.
    all_rows.sort(key=lambda item: item[0])
    for _at, scope, subject, fingerprint_key in all_rows[: len(all_rows) - MAX_ENTRIES]:
        rows = (document.get(scope) or {}).get(subject) or {}
        rows.pop(fingerprint_key, None)
        if not rows:
            (document.get(scope) or {}).pop(subject, None)


def _acquire_file_lock(
    lock_path: Path,
    *,
    spin_s: float = _LOCK_SPIN_S,
    poll_s: float = _LOCK_POLL_S,
    stale_after_s: float = _LOCK_STALE_S,
) -> bool:
    """A cross-process, non-blocking lock: exclusive file creation as the mutex.

    ``O_CREAT | O_EXCL`` is atomic on both Windows and POSIX, which is the
    whole reason it is used instead of ``fcntl``/``msvcrt`` — one code path
    for every platform this plugin runs on. A lock file older than
    :data:`_LOCK_STALE_S` is reclaimed on the theory that its owner crashed
    mid-write; anything younger just means "try again in a moment", and this
    function gives up rather than waiting more than :data:`_LOCK_SPIN_S`.
    """
    deadline = time.time() + spin_s
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return True
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
            except OSError:
                # Vanished between the failed create and the stat — another
                # writer just finished. Loop immediately and try again.
                age = stale_after_s + 1.0
            if age > stale_after_s:
                # RED_TEAM.md F5 (2026-09-19): this branch used to ``pass``
                # on a failed ``unlink()`` and ``continue`` unconditionally
                # — neither checking ``deadline`` nor sleeping. A lock file
                # that is old enough to look stale but that this process
                # cannot delete (owned by another Windows user, an ACL
                # without delete, another process holding it open without
                # ``FILE_SHARE_DELETE``, a read-only filesystem — exactly
                # the cases ``decisions/0006`` lists as accepted risks) made
                # the loop spin at full CPU forever: ``age`` recomputes to
                # the same "stale" answer every pass, ``unlink()`` fails the
                # same way every time, and nothing in the branch ever looked
                # at the clock. Measured: still spinning after 3s against a
                # 0.05s (:data:`_LOCK_SPIN_S`) contract. The module's own
                # docstring promises "gives up rather than waiting more than
                # _LOCK_SPIN_S" — a promise this branch did not keep.
                #
                # A lock this process cannot remove is a lock it cannot
                # acquire, whatever the deadline says, so ``unlink()``
                # raising ends the attempt immediately rather than looping;
                # a successful removal still checks ``deadline`` before
                # looping again, so "every path honours the deadline" is
                # true here too, not only in the two branches below.
                try:
                    lock_path.unlink()
                except OSError:
                    return False
                if time.time() >= deadline:
                    return False
                continue
            if time.time() >= deadline:
                return False
            time.sleep(poll_s)
        except OSError:
            # A directory that vanished, a read-only filesystem, anything
            # else the OS objects to. Never this module's job to fix.
            return False


def _release_file_lock(lock_path: Path) -> None:
    try:
        lock_path.unlink()
    except OSError:
        pass


class SharedHealth:
    """One handle onto the shared pool-health file.

    A real instance does nothing — no stat, no read, no write — until
    :meth:`active` says the switch is on and a path can be derived. Every
    public method is a no-op on any other failure too: a corrupt file, a
    lock that never frees, a directory that will not create. Bookkeeping
    must never be the reason a turn stalls or an exception reaches the caller
    — see the module docstring.
    """

    def __init__(
        self,
        *,
        path: Optional[Path] = None,
        profile: Optional[str] = None,
        enabled_fn: Optional[Callable[[], bool]] = None,
    ) -> None:
        #: Fixed by the caller (tests: two instances, one temp path) or
        #: derived fresh from the environment on every call (the plugin's own
        #: use) — ``None`` here means "derive it", not "there is none".
        self._fixed_path = path
        self._fixed_profile = profile
        self._enabled_fn = enabled_fn or enabled
        #: In-process serialisation only. The real exclusion between two
        #: *processes* is :func:`_acquire_file_lock`; this just keeps two
        #: threads in the same process from interleaving a read-modify-write.
        self._lock = threading.Lock()
        self._cache: Dict[str, Dict[str, Dict[str, Any]]] = {"model": {}, "account": {}}
        self._cache_mtime: Optional[float] = None
        self._cache_checked_at: float = 0.0

    # -- where -------------------------------------------------------------

    def _path(self) -> Optional[Path]:
        return self._fixed_path if self._fixed_path is not None else default_path()

    def _profile(self) -> str:
        return self._fixed_profile if self._fixed_profile is not None else _profile_name()

    def active(self) -> bool:
        """Whether this instance should touch the filesystem at all right now."""
        try:
            return bool(self._enabled_fn())
        except Exception:
            return False

    # -- reading -------------------------------------------------------------

    def _reload_if_needed(self, now: float) -> None:
        """``stat()`` the file, and only re-parse it if the bytes moved.

        1.8.0.1: the floor below only rate-limits the ``stat()`` call — a
        cheap syscall, safe to spend on nearly every ``select()`` — not the
        decision of whether the cached document is still good, which is
        ``mtime`` alone. Before this, the SAME constant gated both: a write
        landing 0.1s after the last check was invisible for up to 0.4s more,
        which is exactly the window `tools/continuity_gate.py` measured every
        residual cross-process double-burn falling inside (see the module
        docstring). All of the degradation paths below are unchanged.
        """
        if now - self._cache_checked_at < READ_STAT_FLOOR_S:
            return
        self._cache_checked_at = now
        path = self._path()
        if path is None:
            return
        try:
            mtime = path.stat().st_mtime
        except OSError:
            # Missing or unreadable — including "vanished mid-read", one of
            # the failure modes this module is required to survive. Treated
            # as "nothing shared yet", not as an error worth remembering.
            self._cache_mtime = None
            self._cache = {"model": {}, "account": {}}
            return
        if self._cache_mtime is not None and mtime == self._cache_mtime:
            return
        document = _read_document(path)
        self._cache = {"model": document["model"], "account": document["account"]}
        self._cache_mtime = mtime

    def _entry(
        self, scope: str, subject: str, fingerprint_key: str, now: Optional[float]
    ) -> Tuple[float, float]:
        """``(until, at)`` for one credential, or ``(0.0, 0.0)`` for "no evidence".

        ``at`` travels with ``until`` on purpose: the caller cannot tell "this
        credential is healthy" from "this file knows nothing about this
        credential" by looking at ``until`` alone, and the two have to be
        told apart to decide whether shared evidence should outrank whatever
        the caller's own local memory already believes — see ``Carousel.
        _model_until``/``_account_until_combined`` for why.
        """
        if not fingerprint_key or not self.active():
            return 0.0, 0.0
        now = time.time() if now is None else now
        try:
            self._reload_if_needed(now)
            row = self._cache.get(scope, {}).get(subject, {}).get(fingerprint_key)
            if not isinstance(row, dict):
                return 0.0, 0.0
            return float(row.get("until", 0.0) or 0.0), float(row.get("at", 0.0) or 0.0)
        except Exception:
            # Whatever went wrong, the caller's own local memory is the
            # fallback — "no evidence" is the safe answer, not a guess.
            return 0.0, 0.0

    def model_entry(self, identity: str, fingerprint_key: str, now: Optional[float] = None) -> Tuple[float, float]:
        """``(until, at)`` for ``(identity, fingerprint)``, or ``(0.0, 0.0)``.

        Mirrors ``Carousel._pools[identity][key]["sick_until"]`` — the per-
        ``provider:model`` bench — across every profile writing this file.
        """
        return self._entry("model", identity, fingerprint_key, now)

    def account_entry(self, provider: str, fingerprint_key: str, now: Optional[float] = None) -> Tuple[float, float]:
        """``(until, at)`` for ``(provider, fingerprint)``, or ``(0.0, 0.0)``.

        Mirrors ``Carousel._account_hold`` — see that dict's own docstring
        for why an account-wide refusal is tracked by ``(provider, key)``
        rather than by ``identity``.
        """
        return self._entry("account", provider, fingerprint_key, now)

    # -- writing -------------------------------------------------------------

    def record(
        self,
        *,
        scope: str,
        subject: str,
        fingerprint_key: str,
        until: float,
        kind: str,
        at: float,
    ) -> None:
        """Write one event. Newest ``at`` wins; never blocks, never raises.

        Called from :meth:`Carousel.mark` after every outcome, success
        included — a success writes ``until=0.0``, which is the mechanism
        that lets one profile's good answer release a bench another profile
        is holding on the same credential.

        ``scope`` is ``"model"`` (``subject`` is an ``identity`` string, the
        same ``provider:model`` key ``Carousel._pools`` uses) or ``"account"``
        (``subject`` is a bare provider name, matching ``Carousel.
        _account_hold``).
        """
        if not fingerprint_key or not self.active():
            return
        path = self._path()
        if path is None:
            return
        try:
            with self._lock:
                self._write_locked(path, scope, subject, fingerprint_key, until, kind, at)
        except Exception:
            # Bookkeeping must never hold up or break the turn that produced
            # this event — the caller (``mark``) has already applied the
            # cooldown to its own local pool by the time this runs. A write
            # that fails changes what OTHER profiles see, never this one.
            pass

    def release_all(self, now: Optional[float] = None) -> Optional[int]:
        """Release every hold in the file — the *Clear pool* button.

        1.8.1.0 (owner report, 2026-09-21): *Clear pool* forgot the carousel's
        memory and nothing else, so the very next ``select`` read this file,
        found every bench fresher than the (now empty) local state, and put
        each key straight back on it. The button promised "every key starts
        again as if it had never been tried" and delivered a pool that looked
        exactly as benched as before.

        Written as releases (``until: 0`` at ``now``) rather than by deleting
        rows, so the same "newest event wins" rule that lets one profile's
        success release another's bench carries the reset to every profile
        sharing the file, instead of leaving their local holds as the only
        fresher fact. Returns how many rows were released, or ``None`` when
        the file could not be written (switch off, no root, lock not free).
        Waits a little longer for the lock than a hot-path write does: this is
        a button a person pressed, not bookkeeping on a turn.
        """
        if not self.active():
            return None
        path = self._path()
        if path is None:
            return None
        stamp = time.time() if now is None else float(now)
        lock_path = path.with_name(path.name + ".lock")
        try:
            with self._lock:
                if not path.exists():
                    return 0
                if not _acquire_file_lock(lock_path, spin_s=1.0):
                    return None
                try:
                    document = _read_document(path)
                    released = 0
                    for scope in ("model", "account"):
                        for rows in (document.get(scope) or {}).values():
                            for fingerprint_key, row in list((rows or {}).items()):
                                if not isinstance(row, dict):
                                    continue
                                rows[fingerprint_key] = {
                                    "until": 0.0,
                                    "kind": "",
                                    "at": max(stamp, float(row.get("at", 0.0) or 0.0)),
                                    "profile": self._profile(),
                                }
                                released += 1
                    _write_document(path, document)
                finally:
                    _release_file_lock(lock_path)
                self._cache = {"model": {}, "account": {}}
                self._cache_mtime = None
                self._cache_checked_at = 0.0
                return released
        except Exception:
            return None

    def _write_locked(
        self,
        path: Path,
        scope: str,
        subject: str,
        fingerprint_key: str,
        until: float,
        kind: str,
        at: float,
    ) -> None:
        lock_path = path.with_name(path.name + ".lock")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        if not _acquire_file_lock(lock_path):
            # The lock was not free almost immediately. Give up silently —
            # see the module docstring — rather than wait, retry, or raise.
            return
        try:
            document = _read_document(path)
            bucket = document.setdefault(scope, {})
            row = bucket.setdefault(subject, {})
            existing = row.get(fingerprint_key)
            existing_at = (
                float(existing.get("at", 0.0) or 0.0) if isinstance(existing, dict) else -1.0
            )
            # RED_TEAM.md F8: ``at`` >= ``existing_at`` is the right guard for
            # an out-of-order WRITE — two events on a clock that never moves
            # backwards — and the wrong one the moment the machine's own
            # clock steps, in either direction, between the two events being
            # compared. A release (``until <= 0``, i.e. a success) is let
            # through unconditionally rather than gated by that comparison,
            # because the two ways this can go wrong are not equally bad:
            # losing a release to a clock step keeps a healthy key benched
            # for up to the ceiling, which costs the pool that key's
            # throughput for the whole span; accepting a release that turns
            # out to be stale costs at most one refused request the next
            # time someone tries that key. Only a release gets the
            # exemption — a stale BENCH write (``until > 0``) is still
            # dropped by the ordinary comparison below, which is the
            # "genuinely stale write" this guard exists to catch in the
            # first place.
            is_release = float(until) <= 0.0
            if is_release or at >= existing_at:
                # Newest EVENT wins, not newest write — a write that landed
                # late for an event that happened earlier must not clobber
                # news a faster writer already recorded. ``>=`` rather than
                # ``>`` so this process's own event always lands even when it
                # races a duplicate of itself with an identical timestamp.
                row[fingerprint_key] = {
                    "until": float(until),
                    "kind": str(kind or ""),
                    "at": float(at),
                    "profile": self._profile(),
                }
            # RED_TEAM.md F7: an implausibly future ``at`` (see
            # _PRUNE_HORIZON_SLACK_S above) must not set the pruning
            # horizon, or this one write drops every other live row. Fall
            # back to this process's own wall clock instead of trusting the
            # event's stamp for that one purpose; the event itself is still
            # stored with its own ``at`` unchanged, above -- this only
            # bounds what "now" means for deciding what else is too old.
            wall_now = time.time()
            prune_horizon = at if at <= wall_now + _PRUNE_HORIZON_SLACK_S else wall_now
            _prune(document, prune_horizon)
            _write_document(path, document)
        finally:
            _release_file_lock(lock_path)


__all__ = [
    "SCHEMA",
    "PLUGIN_ID",
    "ENV_ENABLE",
    "ENV_PATH_OVERRIDE",
    "PRUNE_AFTER_S",
    "MAX_ENTRIES",
    "READ_STAT_FLOOR_S",
    "SharedHealth",
    "default_path",
    "enabled",
    "set_config_default",
]
