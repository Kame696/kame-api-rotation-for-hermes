"""Profile-local leases over process-global host callables.

The coordinator is plugin-owned, shared across the host's per-home import
namespaces. It never stacks one profile's wrapper inside another's wrapper.
Calls outside a registered home keep the original host behaviour. Registry
locks protect ownership only; they are never held while a host call runs.
"""
from __future__ import annotations

import contextvars
import functools
import inspect
import os
from pathlib import Path
import sys
import threading
from types import ModuleType

_KEY = "_kame_profile_leases_v1"
_candidate = ModuleType(_KEY)
_candidate.lock = threading.RLock()
_candidate.entries = {}
_candidate.session = contextvars.ContextVar("kame_registration_session", default=None)
_shared = sys.modules.setdefault(_KEY, _candidate)


@functools.lru_cache(maxsize=128)
def _normal_home(value: str) -> str:
    return os.path.normcase(str(Path(value).resolve()))


def home() -> str:
    """Use the host's ContextVar, not a mutable process-wide profile setting."""
    constants = sys.modules.get("hermes_constants")
    getter = getattr(constants, "get_hermes_home", None)
    value = getter() if callable(getter) else os.environ.get("HERMES_HOME", "")
    return _normal_home(os.fspath(value)) if value else "<unscoped>"


class Patches:
    """Own an install's callable leases; release is idempotent and conditional."""

    def __init__(self):
        self.home = home()
        self.leases = []
        self.session = _shared.session.get()
        if self.session is not None:
            self.session.track(self)

    def original(self, target, name):
        with _shared.lock:
            current = getattr(target, name, None)
            entry = _shared.entries.get((id(target), name))
            if (entry is not None and entry["target"] is target
                    and current is entry["router"]
                    and self.home not in entry["handlers"]):
                return entry["original"]
            return current

    def bind(self, target, name, handler):
        key = (id(target), name)
        with _shared.lock:
            if self.session is not None and self.session.closed:
                raise RuntimeError("KAME registration was unloaded before completion")
            entry = _shared.entries.get(key)
            current = getattr(target, name, None)
            if entry is not None:
                if entry["target"] is not target or current is not entry["router"]:
                    raise RuntimeError("KAME lease target changed during installation")
                if self.home in entry["handlers"]:
                    raise RuntimeError("KAME home already owns this callable")
            else:
                if not callable(current):
                    raise TypeError("KAME callable lease requires a callable target")
                entry = {"target": target, "original": current,
                         "own_attribute": name in vars(target), "handlers": {}}

                def choose():
                    requested = home()
                    with _shared.lock:
                        owned = entry["handlers"].get(requested)
                        return owned[1] if owned is not None else entry["original"]

                if inspect.iscoroutinefunction(current):
                    @functools.wraps(current)
                    async def router(*args, **kwargs):
                        return await choose()(*args, **kwargs)
                else:
                    @functools.wraps(current)
                    def router(*args, **kwargs):
                        return choose()(*args, **kwargs)
                entry["router"] = router
                # Retain compatibility/introspection markers, not profile state.
                router.__dict__.update(handler.__dict__)
                router.__wrapped__ = current
                setattr(target, name, router)
                _shared.entries[key] = entry
            entry["handlers"][self.home] = (self, handler)
            self.leases.append(key)

    def release(self):
        with _shared.lock:
            for key in reversed(self.leases):
                entry = _shared.entries.get(key)
                if entry is None:
                    continue
                owned = entry["handlers"].get(self.home)
                if owned is None or owned[0] is not self:
                    continue
                del entry["handlers"][self.home]
                if entry["handlers"]:
                    continue
                target, name = entry["target"], key[1]
                if getattr(target, name, None) is entry["router"]:
                    if entry["own_attribute"]:
                        setattr(target, name, entry["original"])
                    else:
                        delattr(target, name)
                del _shared.entries[key]
            self.leases.clear()


class _Session:
    def __init__(self, cleanup):
        self.context = contextvars.copy_context()
        self.cleanup = cleanup
        self.patches = []
        self.closed = False
        self.lock = threading.RLock()

    def track(self, patches):
        with self.lock:
            if self.closed:
                raise RuntimeError("KAME registration already unloaded")
            self.patches.append(patches)

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
        try:
            self.context.copy().run(self.cleanup)
        finally:
            for patches in reversed(self.patches):
                patches.release()
            self.patches.clear()


def lifecycle(register):
    """Register cleanup before any host patch, including failed/abandoned loads."""
    active = None
    owner = None
    lock = threading.RLock()

    @functools.wraps(register)
    def guarded(ctx):
        nonlocal active, owner
        with lock:
            if active is not None and not active.closed:
                if owner is ctx:
                    return
                if callable(getattr(owner, "on_unload", None)):
                    raise RuntimeError("KAME namespace already has a live registration")
                # Legacy contexts have no disposal handle. Re-registering that
                # same namespace must replace its own resources, never stack.
                active.close()
            cleanup = register.__globals__["_cleanup_runtime"]
            active = _Session(cleanup)
            owner = ctx
            session = active
            unload = getattr(ctx, "on_unload", None)
            try:
                if callable(unload):
                    unload(session.close)
                token = _shared.session.set(session)
                try:
                    return register(ctx)
                finally:
                    _shared.session.reset(token)
            except BaseException:
                session.close()
                raise
    return guarded
