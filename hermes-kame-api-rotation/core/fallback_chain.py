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

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_yaml_said = False


def _load_yaml(text: str) -> Any:
    """Parse ``config.yaml`` the way a stock Hermes can.

    1.8.2.9 (catalog review of 1.8.2.8, 2026-10-19): core Hermes does not ship PyYAML, so
    ``import yaml`` failed inside the callers' ``except Exception`` and the fallback
    list was silently never read - no hand-over, no cross-model race, no switch limit.
    Hermes' own ``hermes_yaml`` (core since v0.21.6) first, PyYAML second; with neither,
    say so once in the log instead of acting as if the user had no list.
    """
    global _yaml_said
    try:
        from hermes_yaml import safe_load  # type: ignore[import-not-found]
    except ImportError:
        try:
            from yaml import safe_load  # type: ignore[no-redef]
        except ImportError:
            if not _yaml_said:
                _yaml_said = True
                logger.warning(
                    "kame: config.yaml cannot be read - neither hermes_yaml nor PyYAML is "
                    "installed, so the fallback model list is ignored")
            raise _NoYaml("neither hermes_yaml nor PyYAML is installed")
    return safe_load(text)


class _NoYaml(ImportError):
    """No YAML reader on this install: a fact about the install, not a file mid-save."""
_lock = threading.Lock()


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


#: 1.8.2.9 (idle-CPU finding, 2026-10-24): the gateway loads this plugin once per profile and
#: each copy used to parse the 42 KB ``config.yaml`` three times every five seconds, ~120 ms
#: each with the pure-Python reader the real Hermes ships (about 30% of a core while idle).
#: The file is now looked at (one ``stat``) at most once a second and parsed only when its
#: ``(mtime, size)`` changed; ``read``, ``entry_source`` and ``switch_limit`` share that one
#: parse. A file that will not parse (Hermes is saving it) keeps the last good reading.
_STAMP_EVERY_S = 1.0
_NONE = "none"          # no config.yaml
_NO_YAML = "noyaml"     # no YAML reader on this install (a fact about the install)
_OK = "ok"
_BAD = "bad"            # unreadable now and nothing good known yet
_parsed: Dict[str, Dict[str, Any]] = {}


def _stamp_of(path: Path) -> Optional[Tuple[int, int]]:
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size)


def _config(home: Optional[Path]) -> Tuple[str, Any, Dict[str, Any]]:
    """``(kind, parsed config, derived-values memo)`` for ``home``'s ``config.yaml``.

    ``kind`` is ``_OK``, ``_NONE``, ``_NO_YAML`` or ``_BAD``. The memo belongs to one parse: it
    is dropped with it, so derived values never outlive the text they came from."""
    if home is None:
        return _NONE, {}, {}
    key = str(home)
    now = time.monotonic()
    with _lock:
        entry = _parsed.get(key)
        if entry is not None and now - entry["checked"] < _STAMP_EVERY_S:
            return entry["kind"], entry["config"], entry["memo"]
    path = home / "config.yaml"
    stamp = _stamp_of(path)
    with _lock:
        entry = _parsed.get(key)
        if entry is not None and entry["stamp"] == stamp and not entry.get("retry"):
            entry["checked"] = now
            return entry["kind"], entry["config"], entry["memo"]
    if stamp is None:
        fresh = {"stamp": None, "kind": _NONE, "config": {}, "memo": {}, "checked": now}
    else:
        try:
            config = _load_yaml(path.read_text(encoding="utf-8")) or {}
            fresh = {"stamp": stamp, "kind": _OK, "config": config, "memo": {}, "checked": now}
        except _NoYaml:
            fresh = {"stamp": stamp, "kind": _NO_YAML, "config": {}, "memo": {}, "checked": now}
        except Exception as error:
            # 1.8.2.7 (review M3): a file Hermes is saving right now, or one Windows holds for a
            # moment, is not "no chain": keep what was last read. 1.8.2.9 (review, 2026-10-24): a read
            # that FAILED (the file is held) is looked at again in a second; text that would not
            # parse is not parsed again until its stamp changes, or a typo left in the file would
            # bring the idle CPU back.
            held = isinstance(error, OSError)
            with _lock:
                previous = _parsed.get(key)
                if previous is not None and previous["kind"] == _OK:
                    previous["checked"] = now
                    previous["retry"] = held
                    if not held:
                        previous["stamp"] = stamp
                    return previous["kind"], previous["config"], previous["memo"]
                fresh = {"stamp": stamp, "kind": _BAD, "config": {}, "memo": {}, "checked": now,
                         "retry": held}
                _parsed[key] = fresh
            return _BAD, {}, fresh["memo"]
    with _lock:
        _parsed[key] = fresh
    return fresh["kind"], fresh["config"], fresh["memo"]


def read(home: Optional[Path] = None) -> Tuple[Optional[str], Tuple[str, ...]]:
    """``(primary identity or None, fallback identities in Hermes' order)``."""
    home = home if home is not None else _home()
    kind, config, memo = _config(home)
    if kind != _OK:
        return None, ()
    value = memo.get("read")
    if value is None:
        primary, chain = _from_config(config)
        value = memo["read"] = (primary, tuple(chain))
    return value


def entry_source(entry: str, home: Optional[Path] = None) -> Optional[Tuple[str, str]]:
    """``(model as written, base_url or "")`` of the primary or a chain entry, or None.

    1.8.2.8 (review R7): the identity is lowercased, but some providers' model ids
    are not; and two entries of one provider name may point at two endpoints. The
    stream race asks the next model by its own name, only on the same endpoint.
    """
    home = home if home is not None else _home()
    if home is None:
        return None
    kind, config, memo = _config(home)
    if kind == _BAD:
        return None
    table = memo.get("sources")
    if table is None:
        rows: List[Any] = []
        model = config.get("model") if isinstance(config, dict) else None
        if isinstance(model, dict):
            rows.append(model)
        if isinstance(config, dict):
            for name in ("fallback_providers", "fallback_model"):
                value = config.get(name)
                rows.extend(value if isinstance(value, list) else [value] if isinstance(value, dict) else [])
        table = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            written = str(row.get("model") or row.get("default") or "").strip()
            ident = _identity(row.get("provider"), written)
            if ident and ident not in table:
                table[ident] = (written, str(row.get("base_url") or "").strip().rstrip("/"))
        memo["sources"] = table
    return table.get(entry)


def forget() -> None:
    with _lock:
        _parsed.clear()


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


def switch_limit(home: Optional[Path] = None) -> int:
    """How many times Hermes will switch model in ONE message.

    Read on the live host (1.8.2.7, owner's chain test 2026-10-17): every
    fallback activation is a "rebuilt-message restart", and a turn ends with
    "No reply" once the restarts pass ``agent.api_max_retries`` (default 3,
    ``agent/turn_iteration_prep.py``). A list with more fallback models than
    that can never reach its last ones when the first are all spent.
    """
    home = home if home is not None else _home()
    kind, config, memo = _config(home)
    limit = memo.get("limit")
    if limit is None:
        limit = HOST_SWITCH_LIMIT_DEFAULT
        try:
            agent = config.get("agent") if isinstance(config, dict) else None
            if isinstance(agent, dict) and agent.get("api_max_retries") is not None:
                limit = max(int(agent["api_max_retries"]), 1)
        except Exception:
            pass
        if kind != _BAD:
            memo["limit"] = limit
    return limit
