"""The one place this plugin is allowed to write to a file full of secrets.

Hermes keeps durable environment in a ``.env`` next to its config, and that
file holds API keys — the user's, for every provider they have configured. A
plugin that writes to it is one bad line away from destroying credentials that
exist nowhere else, so the rules are narrow and they are enforced here rather
than trusted to each caller:

* **only ``KAME_*``.** A name outside this plugin's own namespace is refused,
  not sanitised. There is no legitimate reason for a key-rotation plugin to
  set ``OPENAI_API_KEY``, and the day some future code path tries, this
  refuses instead of succeeding.
* **every other line survives byte for byte.** Comments, blank lines, ordering
  and every credential are copied through untouched. Only the exact variable
  being set is rewritten.
* **nothing read is ever returned or logged.** Not on success, not in an
  error, not at debug level. The return value names the file and says whether
  the write happened; it never quotes a line.

Extracted from ``menu.py`` in 1.1.1, when the settings panel became the second
caller. Two copies of a function that edits a credential store is one copy too
many: they drift, and the half that drifts is the half nobody reads.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

#: The only prefix this module will write. Not configurable, on purpose.
NAMESPACE = "KAME_"


def path() -> Optional[Path]:
    """Hermes' own ``.env``, asked of Hermes rather than guessed at.

    Imported inside the function so this module stays importable with no
    Hermes in the process, which is how the test suite exercises it.
    """
    try:
        from hermes_cli.config import get_env_path

        found = get_env_path()
        return Path(found) if found else None
    except Exception:
        logger.debug("kame: this Hermes exposes no .env path", exc_info=True)
        return None


def write(name: str, value: str) -> Tuple[bool, str]:
    """Set one ``KAME_*`` line. Returns ``(ok, detail)`` and never raises.

    The whole file is rewritten because there is no line-level API for it,
    which makes "everything else is left exactly as it was" a property worth
    stating rather than assuming.
    """
    if not name.startswith(NAMESPACE):
        # Belt and braces. Callers only ever pass a name that came out of
        # ``settings.env_name``, but this function edits a file full of
        # secrets, so it refuses anything outside the plugin's own namespace
        # rather than trusting its callers to stay correct forever.
        return False, f"refusing to write {name}: only KAME_* variables belong to this plugin"
    target = path()
    if target is None:
        return False, "this Hermes exposes no .env path, so the change applies to this session only"
    try:
        lines = target.read_text(encoding="utf-8").splitlines() if target.is_file() else []
    except Exception as exc:
        return False, f"could not read {target}: {type(exc).__name__}"

    replaced = False
    out: List[str] = []
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith(f"{name}=") and not stripped.startswith("#"):
            if not replaced:
                out.append(f"{name}={value}")
                replaced = True
            # A second assignment of the same variable is dropped rather than
            # kept: dotenv takes the last one, so leaving a stale duplicate
            # below the line just written would silently undo the write.
            continue
        out.append(line)
    if not replaced:
        if out and out[-1].strip():
            out.append("")
        out.append("# KAME API Rotation")
        out.append(f"{name}={value}")

    try:
        target.write_text("\n".join(out) + "\n", encoding="utf-8")
    except Exception as exc:
        return False, f"could not write {target}: {type(exc).__name__}"
    return True, f"{'updated' if replaced else 'added'} in {target}"


def forget(name: str) -> Tuple[bool, str]:
    """Remove one ``KAME_*`` line, so the setting falls back to its default.

    The other half of "Reset to defaults". Setting the variable to the default
    value would look identical on screen and be a different thing: the setting
    would still read as coming from the environment, and a later change to
    what the default *is* would not reach a user who had reset.
    """
    if not name.startswith(NAMESPACE):
        return False, f"refusing to touch {name}: only KAME_* variables belong to this plugin"
    target = path()
    if target is None:
        return False, "this Hermes exposes no .env path, so the change applies to this session only"
    if not target.is_file():
        return True, "nothing to remove"
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except Exception as exc:
        return False, f"could not read {target}: {type(exc).__name__}"
    kept = [
        line
        for line in lines
        if not (line.lstrip().startswith(f"{name}=") and not line.lstrip().startswith("#"))
    ]
    if len(kept) == len(lines):
        return True, "nothing to remove"
    try:
        target.write_text(("\n".join(kept) + "\n") if kept else "", encoding="utf-8")
    except Exception as exc:
        return False, f"could not write {target}: {type(exc).__name__}"
    return True, f"removed from {target}"
