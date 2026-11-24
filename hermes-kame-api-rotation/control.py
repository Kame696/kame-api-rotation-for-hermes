"""How a button in the panel reaches the plugin that has to act on it.

The panel is a file reader. ``state.py`` explains why — a runtime Desktop
plugin has ``readFileText`` and ``watchDirectory`` and no HTTP surface of its
own, because the ``/api/plugins/<name>/`` door belongs to dashboard plugins,
behind a ``dashboard/plugin.json``, an ``plugins.enabled`` entry and a restart.
Reading was enough for 1.1.0, which only showed things.

1.1.1 has switches, and a switch has to get back. The bridge can write a file
(``hermesDesktop.writeTextFile``), so the return path is the mirror image of
the outward one: the panel writes ``control.json`` next to ``state.json``, and
this module — running on the plugin's own heartbeat — reads it, applies it,
deletes it, and reports the outcome in the next snapshot.

**Why a request file and not a settings file.** A file the UI owns and the
backend obeys would make the UI the source of truth for configuration, and it
is not: the environment is, then the config file, then the default. So this
carries *requests* — "set this to that", "forget this one" — which are applied
through exactly the same code path ``/kame set`` uses and then thrown away.
There is one writer of settings in this plugin, and it is ``settings``.

**What it will not do.** The action list is closed, the setting name has to be
one KAME already knows, and the value goes through ``settings.parse`` before
anything is written. There is no action that reads a key, writes a key, or
touches a file outside this plugin's own namespace — the only file this can
cause to be written is Hermes' ``.env``, and only ``KAME_*`` lines in it (see
``envfile``). A malformed or unknown request is reported and dropped.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

#: Bumped when the shape below changes in a way an older panel would
#: misunderstand. The panel writes it; a mismatch is refused rather than
#: guessed at.
SCHEMA = 1

#: Every action this module will carry out. Anything else is reported back as
#: unknown — including, deliberately, anything to do with keys.
ACTIONS = ("set", "reset", "reset_all", "clear_pool", "clear_events", "refresh", "forget_busy", "undo_setup")

#: The request most recently applied, kept so the panel can tell "saved" from
#: "still waiting" without polling anything else.
_last_result: Dict[str, Any] = {}

#: Ids already applied, so a file that reappears (a slow disk, a restore, a
#: second reader) cannot apply the same request twice.
_applied: list = []
_APPLIED_MEMORY = 32


def control_path() -> Optional[Path]:
    from . import state

    directory = state.state_dir()
    return None if directory is None else directory / "control.json"


def last_result() -> Dict[str, Any]:
    """What happened to the most recent request. Empty before the first one."""
    return dict(_last_result)


def poll() -> bool:
    """Apply a pending request, if there is one. Returns whether anything ran.

    Never raises. This is called from a daemon thread on a timer, and a
    settings panel that can take down the heartbeat it depends on would be
    worse than a settings panel that does not save.
    """
    path = control_path()
    if path is None:
        return False
    try:
        if not path.is_file():
            return False
        raw = path.read_text(encoding="utf-8")
    except Exception:
        logger.debug("kame: could not read the control file", exc_info=True)
        return False

    # Removed before it is acted on, not after. A request that crashes the
    # interpreter half way through must not be waiting again on the next
    # start; at worst the user presses the button a second time.
    try:
        os.unlink(path)
    except OSError:
        logger.debug("kame: could not remove the control file", exc_info=True)

    # Every refusal below carries the request's id when there is one. The panel
    # waits on that id and gives up after a few seconds, so a request rejected
    # without it reads as "the backend is not running" — which is the one thing
    # a rejection proves is untrue.
    try:
        request = json.loads(raw)
    except Exception:
        _record({"ok": False, "detail": "the control file was not valid JSON"})
        return False
    if not isinstance(request, dict):
        _record({"ok": False, "detail": "the control file did not hold an object"})
        return False

    identifier = str(request.get("id") or "")

    if request.get("schema") != SCHEMA:
        _record(
            {
                "id": identifier,
                "ok": False,
                "detail": f"this API Rotation understands control schema {SCHEMA}, "
                f"the panel wrote {request.get('schema')!r}",
            }
        )
        return False

    if identifier and identifier in _applied:
        return False

    action = str(request.get("action") or "")
    key = str(request.get("key") or "")
    value = request.get("value")

    try:
        ok, detail = _apply(action, key, value)
    except Exception:
        # ``poll`` promises never to raise, and the panel waits on this id:
        # an unrecorded crash reads there as "the backend is not running".
        logger.warning("kame: panel request %s failed", action, exc_info=True)
        ok, detail = False, "API Rotation could not apply that — see the log"
    _record({"id": identifier, "action": action, "key": key, "ok": ok, "detail": detail})
    if identifier:
        _applied.append(identifier)
        del _applied[:-_APPLIED_MEMORY]
    logger.info("kame: panel requested %s%s — %s", action, f" {key}" if key else "", detail)
    return True


_SECRET_LIKE = re.compile(r"[A-Za-z0-9_\-]{24,}")


def _why(exc: BaseException) -> str:
    """``Type: message`` for a log line - cut short, and with anything key-shaped blanked.

    1.9.0.2 (H2, audit F3). The reset failed on every press for a month and the reason sat at
    DEBUG, so nobody could say what failed. The message is the host's own and may quote a path or
    a lock holder; a run of 24+ token characters is replaced, so a key can never ride along.
    """
    text = _SECRET_LIKE.sub("<redacted>", str(exc) or "")[:200]
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _failed(step: str, exc: BaseException, failed: list, storage: list) -> None:
    """Record one failed reset step: the panel gets ``step (Type)``, the log the reason, at WARNING."""
    failed.append(f"{step} ({type(exc).__name__})")
    if isinstance(exc, OSError) and not isinstance(exc, TimeoutError):
        storage.append(step)
    logger.warning("kame: Reset pool could not clear %s - %s", step, _why(exc))


class _ProfileScope:
    """Run one host call inside this home's secret scope, the way Hermes runs work on a bare thread.

    1.9.0.2 (H2b, audit F3). Live press 2026-11-15 15:37: every provider's host-marks step raised
    ``UnscopedSecretError`` - since the host serves every profile from one gateway
    (``gateway.multiplex_profiles``), a credential read outside a profile's secret scope fails closed,
    and the panel's request runs on KAME's heartbeat, a bare thread with an empty context. The host's
    own chores bind the profile first (``gateway/run_profile_reconcile.py::_for_each_served_profile``,
    ``agent/curator.py``); this does the same for one call, with the home KAME publishes for (the one
    whose ``control.json`` was read). Used only after the host refused without a scope, so a host
    where the plain call works never sees one. The secrets are built once per press.
    """

    class NoScopeApi(Exception):
        """This host has no profile secret scope to bind: report the refusal it gave."""

    def __init__(self) -> None:
        self._secrets: Any = None
        self._home = ""
        self._broken: Optional[BaseException] = None

    def run(self, fn: Any) -> Any:
        try:
            from agent.secret_scope import build_profile_secret_scope, reset_secret_scope, set_secret_scope
            from hermes_constants import get_hermes_home
        except ImportError as exc:
            raise self.NoScopeApi() from exc
        if self._broken is not None:
            raise self._broken              # built once per press: a scope that failed fails the same way
        if self._secrets is None:
            try:
                self._home = str(get_hermes_home())
                try:
                    # The host hydrates external secret sources before building a scope (gateway/run.py
                    # ``_load_profile_secret_scope``); a host without that step still has the .env.
                    from hermes_cli.env_loader import hydrate_profile_secret_sources

                    hydrate_profile_secret_sources(Path(self._home))
                except Exception:
                    logger.debug("kame: could not hydrate the profile's secret sources", exc_info=True)
                self._secrets = build_profile_secret_scope(Path(self._home))
            except Exception as exc:
                self._broken = exc
                raise
        token = set_secret_scope(self._secrets, profile_home=self._home)
        try:
            return fn()
        finally:
            reset_secret_scope(token)


def _clear_benches_on_disk(storage: "list | None" = None) -> "tuple[str, list[str]]":
    """Everything *Clear pool* must drop besides the carousel's memory.

    Each step is independent and never raises: a step that fails leaves its
    own bench standing and says so in the log, and never stops the others.
    Returns a short summary for the panel's acknowledgement, empty when there
    was nothing on disk to clear.

    What is deliberately left alone: the refusal and call recordings (they
    are evidence about the past, not a bench on the present), the event list
    (it has its own button), and every setting.
    """
    done = []
    failed = []
    storage = storage if storage is not None else []
    try:
        from . import _binding  # type: ignore[attr-defined]
    except Exception:
        _binding = None
    if _binding is not None:
        for label, store in (("ledger", getattr(_binding, "_store", None)),
                             ("receipts", getattr(_binding, "_journal", None))):
            if store is None:
                continue
            try:
                if store.clear():
                    done.append(label)
                else:
                    failed.append(label)
                    storage.append(label)       # ``clear`` returns False when the file write failed
                    logger.warning("kame: Reset pool could not clear the %s (the file was not written)", label)
            except Exception as exc:
                _failed(label, exc, failed, storage)
    # The host's own mark. Hermes writes ``last_status: exhausted`` with its
    # own reset time into the credential store, and that mark outlives every
    # piece of KAME state — so a key could still be skipped by Hermes itself
    # after everything above was cleared. Same call ``/kame-keys reset``
    # already makes.
    try:
        from hermes_cli.auth import read_credential_pool
        from agent.credential_pool import load_pool

        reset = 0
        scope = _ProfileScope()
        for provider in sorted(read_credential_pool().keys()):
            try:
                try:
                    reset += int(load_pool(provider).reset_statuses() or 0)
                except Exception as unscoped:
                    # 1.9.0.2 (H2b): a multiplexed host refuses a credential read with no profile
                    # scope; retry this provider once inside this home's scope. Anything else, or a
                    # host without the scope API, is reported as before.
                    if type(unscoped).__name__ != "UnscopedSecretError":
                        raise
                    try:
                        retried = scope.run(lambda p=provider: load_pool(p).reset_statuses())
                    except _ProfileScope.NoScopeApi:
                        raise unscoped from None
                    reset += int(retried or 0)
            except Exception as exc:
                # 1.9.0.2 (H2): which provider, and how - every press since 2026-10-19 said only
                # "host marks". The other providers are still reset.
                _failed(f"Hermes' marks for {provider}", exc, failed, storage)
        if reset:
            done.append(f"{reset} host mark{'s' if reset != 1 else ''}")
    except ImportError:
        # No host in offline tools: there is no host credential store to reset.
        pass
    except Exception as exc:
        _failed("Hermes' credential store", exc, failed, storage)
    try:
        from . import runtime

        runtime.forget_bench_model()
    except Exception as exc:
        _failed("runtime bench", exc, failed, storage)
    return ", ".join(done), failed


def _report_reset() -> None:
    """1.9.0.5 (E3): one log line and one ``reset`` event saying what *Clear pool* is about to forget.

    Counts only (keys held, longest hold left, holds by kind, account holds, busy/slow windows still open): no
    key and no fingerprint. Never raises and never blocks the reset it describes.
    """
    try:
        from .core.carousel import ENGINE
        from .core.events import EVENTS

        summary = ENGINE.reset_summary()
        try:
            from . import facade

            summary["windows_open"] = int(facade.TRANSPORT.open_windows())
        except Exception:
            summary["windows_open"] = 0
        kinds = summary["by_kind"]
        sentence = (f"Clear pool: {summary['keys_held']} key(s) were resting (longest {summary['longest_hold_s']:.0f}s left; "
                    f"daily {kinds['daily']}, account {kinds['account']}, stated {kinds['stated']}, "
                    f"unsized {kinds['unsized']}); {summary['account_holds']} account hold(s); "
                    f"{summary['windows_open']} busy/slow window(s) stay open")
        logger.info("kame: %s", sentence)
        EVENTS.add("reset", reason=sentence, seconds=float(summary["longest_hold_s"]), detail=json.dumps(summary))
    except Exception:
        logger.debug("kame: could not report what the reset cleared", exc_info=True)


def _apply(action: str, key: str, value: Any) -> "tuple[bool, str]":
    from . import envfile, settings

    if action not in ACTIONS:
        return False, f"{action or 'a request with no action'} is not something API Rotation does"

    if action == "forget_busy":
        # 1.8.2.9 (decisions/0031 2.7): the panel's 'Try this model now'. Applied on the heartbeat,
        # never on a call; drops one model's busy / slow window and its probe stamp.
        name = str(key or "").strip()
        if not name:
            return False, "forget_busy needs the model it is for"
        try:
            from . import facade

            dropped = facade.TRANSPORT.forget_busy(name)
        except Exception:
            logger.debug("kame: could not forget a busy window", exc_info=True)
            return False, "could not clear that model's busy window - see the log"
        if not dropped:
            return False, f"{name} is not known busy or slow right now"
        return True, f"{name} will be asked first on the next reply"

    if action == "undo_setup":
        # 1.9.0.5 (D1): "Remove API Rotation's keys". Takes back what the plugin put in the pool and, for a key
        # variable that holds a list, cuts ``.env`` to its first key (backed up first). No key is read out,
        # added or shown: the reply carries counts and fingerprints. ``key`` may name one provider.
        from . import envsync

        return True, envsync.describe_removal(envsync.remove_all(str(key or "").strip().lower() or None))

    if action == "clear_pool" and str(key or "").strip():
        # 1.8.2.9 (decisions/0031 2.7): ONE model ('Wake this model'). Its keys start again; no other
        # model, no account-wide hold and no key is touched.
        name = str(key).strip()
        try:
            from .core.carousel import ENGINE

            if not ENGINE.known(name):
                return False, f"API Rotation has not used {name} yet, so there is nothing to clear"
            ENGINE.reset_pool(name)
            try:
                from . import facade

                facade.TRANSPORT.forget_busy(name)
            except Exception:  # pragma: no cover - the bench was cleared; the busy window is a courtesy
                logger.debug("kame: could not drop the busy window after a clear", exc_info=True)
        except Exception:
            logger.debug("kame: could not clear one pool", exc_info=True)
            return False, "that model's pool could not be cleared - see the log"
        return True, f"{name}: every key starts again as if it had never been tried"

    if action == "clear_pool":
        # Deliberately not "forget the keys". Nothing here can reach a
        # credential: the carousel's bench is health state — cooldowns,
        # counts, the last error kind — and clearing it is a decision about
        # rotation, not about configuration.
        #
        # 1.8.1.0 (owner report, 2026-09-21): this used to be
        # ``ENGINE.forget()`` and nothing else. Four other places still held
        # the benches it promised to drop, and the first of them put every
        # key straight back: the shared pool-health file (on by default),
        # KAME's per-model ledger on disk, the receipts that widen a hold
        # after a deadline proves short, and the host's own "exhausted" mark
        # on each pooled credential. All four are cleared now.
        failures = []
        storage: list = []
        _report_reset()
        try:
            from .core.carousel import ENGINE

            ENGINE.reset_all()
        except Exception as exc:
            _failed("shared pool health", exc, failures, storage)
        cleared, disk_failures = _clear_benches_on_disk(storage)
        failures.extend(disk_failures)
        if failures:
            # 1.9.0.2 (H2): say what failed and how; the storage advice only when it was storage.
            advice = ("retry after checking storage access to the Hermes folder" if storage
                      else "the log has the exact error")
            done_part = f"; cleared {cleared}" if cleared else ""
            return False, ("pool reset incomplete: " + ", ".join(dict.fromkeys(failures))
                           + done_part + "; " + advice)
        tail = f" ({cleared})" if cleared else ""
        return True, "every key starts again as if it had never been tried" + tail

    if action == "clear_events":
        try:
            from .core.events import EVENTS

            EVENTS.clear()
        except Exception:
            logger.debug("kame: could not clear the events", exc_info=True)
            return False, "the event list could not be cleared — see the log"
        return True, "the event list is empty"

    if action == "refresh":
        # 1.6.0.1. Reads the environment again and rebuilds the snapshot on the
        # spot, so a change made outside this panel shows up without waiting
        # for the heartbeat or restarting Hermes.
        #
        # It exists because the honest answer to "did my edit land?" was
        # previously "wait and see". A key pasted into Hermes' own credential
        # screen, a KAME_ line edited in the .env by hand, a second Hermes
        # started beside this one — each changes what this page should say,
        # and none of them is something the page could ask about. The caller
        # that applied this request publishes immediately afterwards, so the
        # answer arrives in the same read as the acknowledgement.
        #
        # Deliberately not a re-read of config.yaml: KAME reads that once, at
        # start-up, and pretending otherwise here would make the
        # "pending restart" notice on the same page a lie. The environment is
        # what this plugin can honestly re-read, and it is where the panel's
        # own writes go.
        changed = settings.reread_environment()
        if changed:
            return True, (
                f"re-read — {', '.join(changed)} "
                f"{'has' if len(changed) == 1 else 'have'} changed since the last look"
            )
        return True, "re-read — nothing outside this panel had changed"

    if action == "reset_all":
        failures = []
        # 1.8.2.3: string settings are reset too — a "reset everything"
        # that quietly kept a hand-typed proxy or RPM override is not a
        # reset (the gap the design review called out).
        for name in list(settings.ALL_FLAGS) + list(settings.ALL_NUMBERS) + list(settings.ALL_STRINGS):
            ok, detail = _forget_one(name)
            if not ok:
                failures.append(f"{name}: {detail}")
        if failures:
            return False, "; ".join(failures[:3])
        return True, "every setting is back to its default"

    # A name this setting used to carry is answered under its current one, so a
    # panel built against an older release keeps working.
    key = settings.canonical(key)
    if not settings.known(key):
        return False, f"{key or 'an unnamed setting'} is not an API Rotation setting"

    if action == "reset":
        return _forget_one(key)

    parsed, error = settings.parse(key, value)
    if parsed is None:
        return False, error
    variable = settings.env_name(key)
    if not variable:
        return False, f"{key} has no environment variable, so it cannot be set from here"
    # The process environment first, so the change is live even if the file
    # write fails — a setting that took effect but was not persisted is a much
    # better outcome than one that did neither.
    os.environ[variable] = parsed
    written, detail = envfile.write(variable, parsed)
    if not written:
        return True, f"in force now, but not saved: {detail}"
    return True, f"in force now and saved ({detail})"


def _forget_one(key: str) -> "tuple[bool, str]":
    from . import envfile, settings

    for variable in (settings.env_name(key), *_legacy_names(key)):
        if not variable:
            continue
        os.environ.pop(variable, None)
        ok, detail = envfile.forget(variable)
        if not ok:
            return False, detail
    # The config file is left alone on purpose: this plugin does not own
    # config.yaml, and a reset that silently deleted a line somebody hand-wrote
    # there would be a surprise with no undo. The panel says as much when a
    # setting still reads "config" after a reset.
    return True, "back to the default"


def _legacy_names(key: str) -> "tuple[str, ...]":
    from . import settings

    # RED_TEAM.md F13: ``_LEGACY_ENV_FOR_1_0_8`` (the oldest spelling,
    # restored for I11 — see its own docstring in settings.py) is a
    # SEPARATE table from ``_LEGACY_ENV_FOR`` on purpose, so it was never
    # read here. That made ``/kame reset`` report success while leaving
    # ``KAME_FIRST_TOKEN_PATIENCE`` set in the environment and in ``.env`` —
    # exactly where ``settings._env_names`` still reads it from. A name that
    # answers has to be one this clears too, so both tables are walked here;
    # settings.py's own separation (why control.py/menu.py read the newer
    # table directly, by value) is untouched.
    names = []
    for table in ("_LEGACY_ENV_FOR", "_LEGACY_ENV_FOR_1_0_8"):
        variable = getattr(settings, table, {}).get(key, "")
        if variable:
            names.append(variable)
    return tuple(names)


def _record(result: Dict[str, Any]) -> None:
    global _last_result
    result["at"] = time.time()
    _last_result = result
    _remember_the_change(result)
    try:
        from . import state

        state.publish(force=True)
    except Exception:
        logger.debug("kame: could not publish the control result", exc_info=True)


def _remember_the_change(result: Dict[str, Any]) -> None:
    """Put the setting change in the Events tab, beside the rotations.

    ``last_result`` above is one slot: it holds the panel's *most recent*
    request and nothing else, because its only job is to stop the panel
    waiting for an answer. That was enough until the owner asked a question it
    cannot answer.

    On 2026-09-05 he set ``stream_silence_timeout_seconds`` to 10 during a
    session and reset it at 19:25:40. The reset was in the state file; the
    moment he *set* it was nowhere, so "did the ten-second first-token wait
    help?" could only be answered with a window — some time between 14:54 and
    16:44, inferred from when timeouts started appearing in the journal.

    A setting change is a decision about how the pool behaves, and the Events
    tab already holds every other one. Recorded here rather than at the call
    sites so a control path added later cannot forget: everything that reaches
    ``_record`` is, by definition, something the panel asked KAME to change.

    The value is **not** stored. Some settings are numbers and some are flags,
    but ``control`` is a general path and a future setting could carry
    something a screenshot should not, and the two facts worth having — which
    setting, and exactly when — are both here without it.
    """
    key = str(result.get("key") or "").strip()
    if not key:
        # A whole-panel reset, or a request that never named a setting.
        key = "every setting"
    action = str(result.get("action") or "changed").strip() or "changed"
    try:
        from .core.events import EVENTS

        EVENTS.add(
            "setting",
            reason=f"{action} — {key}",
            detail=str(result.get("detail") or ""),
            at=result.get("at"),
        )
    except Exception:
        logger.debug("kame: could not record the setting change", exc_info=True)

    _write_the_change_down(action, key, result)


#: Where the durable copy goes, beside the state file and the refusal log.
CHANGES_FILENAME = "settings-changes.jsonl"

#: Small on purpose. One line per change, and a person changing settings all
#: day writes a few hundred bytes.
CHANGES_CEILING_BYTES = 1024 * 1024


def _write_the_change_down(action: str, key: str, result: Dict[str, Any]) -> None:
    """Append the change to a file, because the Events tab forgets.

    1.7.0.1 put setting changes on the Events timeline and that was half an
    answer: the timeline is a 150-row ring in memory, so it is gone on a
    restart and gone the moment anyone clicks *Clear events*. The owner asked
    the obvious question — "settings I change should stay recorded, no?" — and
    the honest answer was no, they did not.

    They have to, because the question they exist to answer is asked later.
    *Did the ten-second first-token wait help?* is a comparison between two
    stretches of a session, and by the time it is asked the session has been
    restarted at least once. A file is the only thing that survives that.

    The value is deliberately not written. ``control`` is a general path, a
    future setting could carry something a support bundle should not hold, and
    which setting and exactly when are both here without it.

    Never raises. A settings change that took effect but was not written down
    is a worse log; a settings change that raised is a broken panel.
    """
    try:
        from . import state

        folder = state.state_dir()
        if folder is None:
            return
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / CHANGES_FILENAME
        if path.is_file() and path.stat().st_size >= CHANGES_CEILING_BYTES:
            return
        row = {
            "at": result.get("at"),
            "action": action,
            "key": key,
            "ok": bool(result.get("ok")),
            "detail": str(result.get("detail") or "")[:400],
        }
        from .recorder import private_append

        with private_append(path) as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + chr(10))
    except Exception:
        logger.debug("kame: could not write the setting change down", exc_info=True)


def forget() -> None:
    """Drop what has been applied. For tests."""
    global _last_result
    _last_result = {}
    _applied.clear()
