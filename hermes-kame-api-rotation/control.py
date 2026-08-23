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
ACTIONS = ("set", "reset", "reset_all", "clear_pool", "clear_events")

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


def _apply(action: str, key: str, value: Any) -> "tuple[bool, str]":
    from . import envfile, settings

    if action not in ACTIONS:
        return False, f"{action or 'a request with no action'} is not something KAME does"

    if action == "clear_pool":
        # Deliberately not "forget the keys". Nothing here can reach a
        # credential: the carousel's bench is health state — cooldowns,
        # counts, the last error kind — and clearing it is a decision about
        # rotation, not about configuration.
        try:
            from .core.carousel import ENGINE

            ENGINE.forget()
        except Exception:
            logger.debug("kame: could not clear the pool", exc_info=True)
            return False, "the pool could not be cleared — see the log"
        return True, "every key starts again as if it had never been tried"

    if action == "clear_events":
        try:
            from .core.events import EVENTS

            EVENTS.clear()
        except Exception:
            logger.debug("kame: could not clear the events", exc_info=True)
            return False, "the event list could not be cleared — see the log"
        return True, "the event list is empty"

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

    variable = getattr(settings, "_LEGACY_ENV_FOR", {}).get(key, "")
    return (variable,) if variable else ()


def _record(result: Dict[str, Any]) -> None:
    global _last_result
    result["at"] = time.time()
    _last_result = result
    try:
        from . import state

        state.publish(force=True)
    except Exception:
        logger.debug("kame: could not publish the control result", exc_info=True)


def forget() -> None:
    """Drop what has been applied. For tests."""
    global _last_result
    _last_result = {}
    _applied.clear()
