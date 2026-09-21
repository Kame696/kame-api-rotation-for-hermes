"""Where the Desktop half lives, and whether Desktop can find it. Reads only.

The plugin ships two halves in one package. Since 1.8.1.0 the Desktop half
sits at ``desktop/plugin.js`` inside the package — Hermes' unified-package
door, ``<hermes home>/plugins/<name>/desktop/plugin.js``. Desktop inventories
it in Settings → Plugins, off until the user turns it on, which is the same
installed-but-inert posture the Python half has.

Until 1.8.1.0 the file shipped as ``desktop-ui/plugin.js`` and ``register()``
copied it into ``<hermes home>/desktop-plugins/`` so it would load default-on.
The catalog review (NousResearch/hermes-agent#117966) asked for that copy to
go, for two reasons that are both right: a catalog plugin must not write
outside its own install directory to change how Desktop trusts it — default-on
versus opt-in is the app's decision — and the admission lint scans only
``desktop/*.js``, so a bundle under another name went unreviewed by CI.

So nothing here writes anything. :func:`report` says whether the packaged file
is present and whether an old copy from a release before 1.8.1.0 is still
sitting in ``desktop-plugins/`` — that copy would load the panel twice, and
removing it is the user's call, so it is reported, never deleted.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

PLUGIN_ID = "hermes-kame-api-rotation"


def source() -> Path:
    return Path(__file__).resolve().parent / "desktop" / "plugin.js"


def legacy_copy() -> Optional[Path]:
    """The pre-1.8.1.0 copy in ``desktop-plugins/``, if one is still there."""
    try:
        from . import state

        home = state._hermes_home()
        if home is None:
            return None
        path = home / "desktop-plugins" / PLUGIN_ID / "plugin.js"
        return path if path.is_file() else None
    except Exception:
        return None


def report() -> Dict[str, object]:
    """Never raises. ``installed`` means the packaged file is where Desktop looks."""
    try:
        origin = source()
        present = origin.is_file()
        old = legacy_copy()
        return {
            "installed": present,
            "reason": "" if present else "the packaged desktop/plugin.js is missing",
            "path": str(origin) if present else "",
            "door": "unified",
            "enable": "Settings > Plugins > KAME API Rotation (off until turned on)",
            "legacy_copy": str(old) if old is not None else "",
        }
    except Exception:
        logger.debug("kame: could not read the desktop half", exc_info=True)
        return {"installed": False, "reason": "unavailable", "path": "", "legacy_copy": ""}
