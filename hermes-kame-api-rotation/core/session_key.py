"""The task-local session values of the host, read the one safe way.

1.9.0.6 (decisions/0037 C1, C16). ``gateway.session_context`` keeps ``HERMES_SESSION_ID`` and the others in
ContextVars bound per task, and ALSO writes the last agent's id into ``os.environ`` for the whole process (and the
legacy ``HERMES_CRON_SESSION`` the same way). A reader that falls back to ``os.environ`` (``get_session_env``) would
see another conversation's value. KAME reads the ContextVar only, through the host's own ``_bound_session_value``:
a value that is ``_UNSET`` (never bound in this task) reads as ``""``. Never raises.
"""
from __future__ import annotations

from typing import Tuple


def bound(name: str) -> str:
    """The value of the task-local variable ``name`` (``HERMES_SESSION_ID`` ...), or ``""``."""
    try:
        from gateway import session_context as sc  # type: ignore[import-not-found]

        value = sc._bound_session_value(name)
        if value is getattr(sc, "_UNSET", None) or not isinstance(value, str):
            return ""
        return value.strip()
    except Exception:
        return ""


def first_bound(names: Tuple[str, ...] = ("HERMES_SESSION_ID", "HERMES_SESSION_KEY")) -> str:
    """The first of ``names`` that is bound and not empty, else ``""``."""
    for name in names:
        value = bound(name)
        if value:
            return value
    return ""
