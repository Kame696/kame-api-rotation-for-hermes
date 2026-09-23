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
ACTIONS = ("set", "reset", "reset_all", "clear_pool", "clear_events", "refresh")

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
                "detail": f"this KAME understands control schema {SCHEMA}, "
                f"the panel wrote {request.get('schema')!r}",
            }
        )
        return False

    if identifier and identifier in _applied:
        return False

    action = str(request.get("action") or "")
    key = str(request.get("key") or "")
    value = request.get("value")

    ok, detail = _apply(action, key, value)
    _record({"id": identifier, "action": action, "key": key, "ok": ok, "detail": detail})
    if identifier:
        _applied.append(identifier)
        del _applied[:-_APPLIED_MEMORY]
    logger.info("kame: panel requested %s%s — %s", action, f" {key}" if key else "", detail)
    return True


def _clear_benches_on_disk() -> "tuple[str, list[str]]":
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
            except Exception:
                failed.append(label)
                logger.debug("kame: could not clear the %s", label, exc_info=True)
    # The host's own mark. Hermes writes ``last_status: exhausted`` with its
    # own reset time into the credential store, and that mark outlives every
    # piece of KAME state — so a key could still be skipped by Hermes itself
    # after everything above was cleared. Same call ``/kame-keys reset``
    # already makes.
    try:
        from hermes_cli.auth import read_credential_pool
        from agent.credential_pool import load_pool

        reset = 0
        for provider in sorted(read_credential_pool().keys()):
            try:
                reset += int(load_pool(provider).reset_statuses() or 0)
            except Exception:
                failed.append("host marks")
                logger.debug("kame: could not reset %s", provider, exc_info=True)
        if reset:
            done.append(f"{reset} host mark{'s' if reset != 1 else ''}")
    except ImportError:
        # No host in offline tools: there is no host credential store to reset.
        pass
    except Exception:
        failed.append("host credential store")
        logger.debug("kame: host credential store not reachable", exc_info=True)
    try:
        from . import runtime

        runtime.forget_bench_model()
    except Exception:
        failed.append("runtime bench")
    return ", ".join(done), failed


def _apply(action: str, key: str, value: Any) -> "tuple[bool, str]":
    from . import envfile, settings

    if action not in ACTIONS:
        return False, f"{action or 'a request with no action'} is not something KAME does"

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
        try:
            from .core.carousel import ENGINE

            ENGINE.reset_all()
        except Exception:
            logger.debug("kame: could not clear the pool", exc_info=True)
            failures.append("shared pool health")
        cleared, disk_failures = _clear_benches_on_disk()
        failures.extend(disk_failures)
        if failures:
            return False, "pool reset incomplete: " + ", ".join(dict.fromkeys(failures)) + "; retry after checking storage access"
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
        for name in list(settings.ALL_FLAGS) + list(settings.ALL_NUMBERS):
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
        return False, f"{key or 'an unnamed setting'} is not a KAME setting"

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
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + chr(10))
    except Exception:
        logger.debug("kame: could not write the setting change down", exc_info=True)


def forget() -> None:
    """Drop what has been applied. For tests."""
    global _last_result
    _last_result = {}
    _applied.clear()
