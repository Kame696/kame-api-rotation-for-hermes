"""Put the Desktop half where Desktop will actually load it.

The plugin ships two halves in one package. The Python half is loaded from
``<hermes home>/plugins/hermes-kame-api-rotation/``; the Desktop half is a
plain ESM file that Hermes' renderer loads from disk. There are two doors it
could go through, and they are not equivalent:

* ``<hermes home>/plugins/<name>/desktop/plugin.js`` — the unified-package
  door. Loaded with ``defaultEnabled: false``, to mirror the Python half's
  installed-but-inert posture. A status chip nobody can see until they find a
  toggle in Settings is not a status chip.
* ``<hermes home>/desktop-plugins/<name>/plugin.js`` — the standalone door,
  which keeps its default-on trust.

So the file ships inside the package as ``desktop-ui/plugin.js`` — a name
neither door scans — and this module copies it to the standalone door when the
Python half registers. One install, no toggle, no manual step, and an upgrade
refreshes it because the copy is made whenever the bytes differ.

It is deliberately not a hard requirement. Every failure here is reported and
swallowed: a Hermes with no writable home, or a CLI-only install with no
Desktop at all, must keep rotating keys exactly as before. The chip is the
nicety; the rotation is the product.
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

#: Same directory name under both ``plugins/`` and ``desktop-plugins/``.
PLUGIN_ID = "hermes-kame-api-rotation"

_state: Dict[str, object] = {"installed": False, "reason": "not attempted", "path": ""}


def report() -> Dict[str, object]:
    return dict(_state)


def source() -> Path:
    return Path(__file__).resolve().parent / "desktop-ui" / "plugin.js"


def target() -> Optional[Path]:
    from . import state

    home = state._hermes_home()
    return None if home is None else home / "desktop-plugins" / PLUGIN_ID / "plugin.js"


def _digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def install() -> bool:
    """Copy the UI file into the standalone door if it is not already there.

    Returns whether the door now holds this release's file. Never raises.
    """
    origin = source()
    if not origin.is_file():
        _set(False, "the packaged desktop file is missing", "")
        return False

    destination = target()
    if destination is None:
        _set(False, "this Hermes exposes no home directory", "")
        return False

    if _digest(destination) == _digest(origin):
        # Already this release's bytes. Saying so is worth more than saying
        # nothing: it is the difference between "up to date" and "never ran".
        _set(True, "", str(destination))
        return True

    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Atomic, because the renderer watches this directory and would
        # otherwise get a chance to import a half-written module.
        handle, temporary = tempfile.mkstemp(dir=str(destination.parent), suffix=".tmp")
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(origin.read_bytes())
            os.replace(temporary, destination)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
    except Exception as exc:
        _set(False, f"could not write {destination}: {type(exc).__name__}", "")
        logger.debug("kame: could not install the desktop half", exc_info=True)
        return False

    logger.info("kame: installed the desktop panel at %s", destination)
    _set(True, "", str(destination))
    return True


def uninstall() -> bool:
    """Take the Desktop half back out of ``desktop-plugins/``. Never raises.

    The Python half lives where Hermes put it and leaves with it. The Desktop
    half does not: ``install()`` copies it into a directory Hermes never
    associated with this plugin, so removing the plugin would leave a panel
    behind — a sidebar entry, a status chip, and a page reading a snapshot
    nothing is writing any more. Whatever put a file there has to be able to
    take it away.

    Registered through ``ctx.on_unload``, which fires on an uninstall, on a
    disable and on a reload alike. That is the right trade: after a reload
    ``register`` copies the file straight back, so the cost of the extra case
    is a file rewritten, and the cost of *not* covering it would be a panel
    that outlives the plugin.

    Only ever removes this plugin's own file and its own directory, and only
    when the directory has nothing else in it — a directory somebody else has
    put something into is not ours to delete.
    """
    destination = target()
    if destination is None:
        _set(False, "this Hermes exposes no home directory", "")
        return False
    removed = False
    try:
        if destination.is_file():
            os.unlink(destination)
            removed = True
            logger.info("kame: removed the desktop panel at %s", destination)
    except Exception:
        logger.debug("kame: could not remove the desktop half", exc_info=True)
        _set(False, f"could not remove {destination}", str(destination))
        return False
    try:
        parent = destination.parent
        if parent.is_dir() and parent.name == PLUGIN_ID and not any(parent.iterdir()):
            parent.rmdir()
    except Exception:
        # An empty directory left behind is untidy, not broken.
        logger.debug("kame: could not remove the desktop directory", exc_info=True)
    _set(False, "removed" if removed else "was not installed", "")
    return removed


def _set(installed: bool, reason: str, path: str) -> None:
    _state["installed"] = installed
    _state["reason"] = reason
    _state["path"] = path
