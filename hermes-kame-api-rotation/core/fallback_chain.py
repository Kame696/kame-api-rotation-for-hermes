"""Hermes' own fallback chain, read-only (1.8.2.7).

KAME never chooses a model (R02). Hermes does, from the list the user keeps in
its config (``fallback_providers``, plus the legacy ``fallback_model``). This
module only READS that list so KAME can tell, when every key of the model in
use is resting, whether handing the call to Hermes would reach another model
(``transport.KameTransport._drive``, ``decisions/0025``). It never writes it.

Source: the same two keys Hermes reads (``fallback_providers`` first, then
``fallback_model``, duplicates dropped; ``hermes_cli/fallback_config.py:126``),
read from ``config.yaml`` in ``HERMES_HOME`` (each profile has its own). Not
the host's helper, on purpose: this must keep working if that import path moves.
Any failure is an empty chain: with no chain KAME behaves exactly as before.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_TTL_S = 5.0
_UNREADABLE: Tuple[Optional[str], Tuple[str, ...]] = (None, ("<unreadable>",))
_lock = threading.Lock()
_cache: Dict[str, Tuple[float, Tuple[Optional[str], Tuple[str, ...]]]] = {}


def _home() -> Optional[Path]:
    value = os.environ.get("HERMES_HOME")
    if value:
        return Path(value)
    try:  # the plugin's own notion of the home, which already handles Windows
        from .. import state

        return Path(str(state._hermes_home()))
    except Exception:
        return None


def _identity(provider: Any, model: Any) -> Optional[str]:
    provider = str(provider or "").strip()
    model = str(model or "").strip()
    if not provider or not model:
        return None
    # KAME lowercases every identity (``Carousel.identity``); the chain must match,
    # or "Qwen/Qwen3-235B" never finds its pool (review H3).
    return f"{provider.lower()}:{model.lower()}"


def _from_config(config: Any) -> Tuple[Optional[str], List[str]]:
    """``(primary identity, [fallback identities])`` from a loaded config dict."""
    primary: Optional[str] = None
    model = config.get("model") if isinstance(config, dict) else None
    if isinstance(model, dict):
        primary = _identity(model.get("provider"), model.get("default") or model.get("model"))
    elif isinstance(model, str) and isinstance(config, dict):
        # The legacy one-line form (review L5): ``model: name`` beside ``provider: x``.
        primary = _identity(config.get("provider"), model)
    chain: List[str] = []
    entries: List[Any] = []
    if isinstance(config, dict):
        for key in ("fallback_providers", "fallback_model"):
            value = config.get(key)
            if isinstance(value, dict):
                entries.append(value)
            elif isinstance(value, list):
                entries.extend(value)
    for entry in entries:
        if isinstance(entry, dict):
            ident = _identity(entry.get("provider"), entry.get("model"))
            if ident and ident not in chain and ident != primary:
                chain.append(ident)
    return primary, chain


def _read_uncached(home: Optional[Path]) -> Tuple[Optional[str], Tuple[str, ...]]:
    if home is None:
        return None, ()
    path = home / "config.yaml"
    if not path.exists():
        return None, ()
    try:
        import yaml  # the host ships it

        config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        # 1.8.2.7 (review M3): a file Hermes is saving right now, or one Windows
        # holds for a moment, is not "no chain"; the caller must not cache it.
        return _UNREADABLE
    primary, chain = _from_config(config)
    return primary, tuple(chain)


def read(home: Optional[Path] = None) -> Tuple[Optional[str], Tuple[str, ...]]:
    """``(primary identity or None, fallback identities in Hermes' order)``."""
    home = home if home is not None else _home()
    key = str(home)
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit is not None and now - hit[0] < _TTL_S:
            return hit[1]
    value = _read_uncached(home)
    if value is _UNREADABLE:
        with _lock:
            hit = _cache.get(key)
        return hit[1] if hit is not None else (None, ())
    with _lock:
        _cache[key] = (now, value)
    return value


_source_cache: Dict[str, Tuple[float, Dict[str, Tuple[str, str]]]] = {}


def entry_source(entry: str, home: Optional[Path] = None) -> Optional[Tuple[str, str]]:
    """``(model as written, base_url or "")`` of the primary or a chain entry, or None.

    1.8.2.8 (review R7): the identity is lowercased, but some providers' model ids
    are not; and two entries of one provider name may point at two endpoints. The
    stream race asks the next model by its own name, only on the same endpoint.
    """
    home = home if home is not None else _home()
    if home is None:
        return None
    key = str(home)
    now = time.monotonic()
    with _lock:
        hit = _source_cache.get(key)
    if hit is None or now - hit[0] >= _TTL_S:
        try:
            import yaml  # the host ships it

            config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")) or {}
        except Exception:
            return None
        rows: List[Any] = []
        model = config.get("model") if isinstance(config, dict) else None
        if isinstance(model, dict):
            rows.append(model)
        if isinstance(config, dict):
            for name in ("fallback_providers", "fallback_model"):
                value = config.get(name)
                rows.extend(value if isinstance(value, list) else [value] if isinstance(value, dict) else [])
        table: Dict[str, Tuple[str, str]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            written = str(row.get("model") or row.get("default") or "").strip()
            ident = _identity(row.get("provider"), written)
            if ident and ident not in table:
                table[ident] = (written, str(row.get("base_url") or "").strip().rstrip("/"))
        hit = (now, table)
        with _lock:
            _source_cache[key] = hit
    return hit[1].get(entry)


def forget() -> None:
    with _lock:
        _cache.clear()
        _limit_cache.clear()
        _source_cache.clear()


def _same(identity: str, entry: str) -> bool:
    """KAME suffixes some identities (``gemini:m:global``); compare the prefix."""
    return identity == entry or identity.startswith(entry + ":")


def later_entries(identity: str, home: Optional[Path] = None) -> List[str]:
    """The chain entries Hermes would try AFTER ``identity``.

    ``[]`` when there is no chain, when ``identity`` is not in ``[primary]+chain``
    and is not the primary's stand-in, or when it is already the last entry.
    """
    primary, chain = read(home)
    if not chain:
        return []
    order = ([primary] if primary else []) + list(chain)
    for index, entry in enumerate(order):
        if _same(identity, entry):
            return order[index + 1:]
    return []


def primary_identity(home: Optional[Path] = None) -> Optional[str]:
    return read(home)[0]


#: Hermes' own default for ``agent.api_max_retries`` (``hermes_cli/config_defaults.py``).
HOST_SWITCH_LIMIT_DEFAULT = 3
_limit_cache: Dict[str, Tuple[float, int]] = {}


def switch_limit(home: Optional[Path] = None) -> int:
    """How many times Hermes will switch model in ONE message.

    Read on the live host (1.8.2.7, owner's chain test 2026-10-17): every
    fallback activation is a "rebuilt-message restart", and a turn ends with
    "No reply" once the restarts pass ``agent.api_max_retries`` (default 3,
    ``agent/turn_iteration_prep.py``). A list with more fallback models than
    that can never reach its last ones when the first are all spent.
    """
    home = home if home is not None else _home()
    key = str(home)
    now = time.monotonic()
    with _lock:
        hit = _limit_cache.get(key)
        if hit is not None and now - hit[0] < _TTL_S:
            return hit[1]
    limit = HOST_SWITCH_LIMIT_DEFAULT
    try:
        import yaml

        config = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")) or {}
        agent = config.get("agent") if isinstance(config, dict) else None
        if isinstance(agent, dict) and agent.get("api_max_retries") is not None:
            limit = max(int(agent["api_max_retries"]), 1)
    except Exception:
        pass
    with _lock:
        _limit_cache[key] = (now, limit)
    return limit
