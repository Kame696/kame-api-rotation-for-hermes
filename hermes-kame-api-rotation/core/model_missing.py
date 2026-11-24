"""A model the provider does not know: reported once, never retried all day.

1.9.0.6 (decisions/0037 C13). The owner's fallback list held ``gemini-3.7.3.6`` (a typo); every reply that reached it
got a 404 "Model ... not found. Did you mean 'gemini-3.7-flash'?" and KAME raced, widened, stood in and handed over to
it again and again. A refusal that says the MODEL does not exist is a fact about the list, not about the keys or the
moment: the model is marked missing for six hours or until the user's ``config.yaml`` changes, and nothing picks it.

Pure bookkeeping, one lock, no I/O inside it. Every function never raises.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: How long a missing model stays out (the file changing ends it earlier).
HOLD_S = 6 * 3600.0

_LOCK = threading.Lock()
#: normalised identity -> {"at", "written", "provider", "suggestion", "config_mtime"}
_MISSING: Dict[str, Dict[str, Any]] = {}

_SUFFIX = re.compile(r":(global|[a-z]+(?:-[a-z]+)*\d+(?:-[a-z])?)$")
_NAMED = re.compile(r"""model\s+['"`]?([A-Za-z0-9._:/\-]+)['"`]?\s+(?:is\s+)?not\s+found""", re.I)
_NOT_FOUND_API = re.compile(r"is not found for API version|models/[^\s]+ is not found", re.I)
_DID_YOU_MEAN = re.compile(r"""did you mean\s+['"`]([^'"`]+)['"`]""", re.I)
_CODE_WORDS = ("model_not_found", "model_not_available")
#: Words that tie a "not found / not available" to the KEY (its plan, tier or access), not to the model name.
_KEY_WORDS = ("plan", "subscription", "tier", "upgrade", "access", "permission", "pay-as-you-go", "entitle")


def normalise(identity: str) -> str:
    """``gemini:gemini-3.7.3.6:global`` -> ``gemini:gemini-3.7.3.6`` (lower case)."""
    try:
        return _SUFFIX.sub("", str(identity or "").strip().lower())
    except Exception:
        return str(identity or "").strip().lower()


def detect(status: Any, message: Any, body: Any = None) -> Optional[Dict[str, str]]:
    """``{"written": ..., "suggestion": ...}`` when a refusal says the MODEL was not found, else ``None``.

    Only a 404 counts: Gemini "Model 'x' not found" / "is not found for API version", OpenAI ``model_not_found``,
    Anthropic ``not_found_error`` whose message names the model. A 404 of a route or a file is not this."""
    try:
        if int(status) != 404:
            return None
    except (TypeError, ValueError):
        return None
    try:
        text = str(message or "")
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict):
            text = f"{text} {error.get('message') or ''} {error.get('code') or ''} {error.get('type') or ''}"
        lowered = text.lower()
        named = _NAMED.search(text)
        anthropic = "not_found_error" in lowered and "model" in lowered
        coded = any(word in lowered for word in _CODE_WORDS)
        if not (named or coded or anthropic or _NOT_FOUND_API.search(text)):
            return None
        if any(word in lowered for word in _KEY_WORDS):
            # The model exists but not for THIS key ("not included in your subscription plan", zenmux): another key
            # may have it, so it is the key's refusal (rotated as before), never a fact about the list.
            return None
        suggestion = _DID_YOU_MEAN.search(text)
        return {"written": named.group(1) if named else "",
                "suggestion": suggestion.group(1) if suggestion else ""}
    except Exception:  # pragma: no cover - a reading of an error text
        return None


def _config_mtime() -> float:
    try:
        from . import fallback_chain

        home = fallback_chain._home()
        return (home / "config.yaml").stat().st_mtime if home is not None else 0.0
    except Exception:
        return 0.0


def mark(identity: str, *, written: str = "", suggestion: str = "") -> None:
    try:
        key = normalise(identity)
        if not key:
            return
        provider = key.split(":", 1)[0]
        model = written or (key.split(":", 1)[1] if ":" in key else key)
        mtime = _config_mtime()
        with _LOCK:
            _MISSING[key] = {"at": time.time(), "written": model, "provider": provider,
                             "suggestion": suggestion, "config_mtime": mtime}
    except Exception:  # pragma: no cover
        logger.debug("kame: could not mark a missing model", exc_info=True)


def is_missing(identity: str) -> bool:
    try:
        key = normalise(identity)
        if not key or not _MISSING:
            return False
        mtime = _config_mtime()
        with _LOCK:
            row = _MISSING.get(key)
            if row is None:
                return False
            if time.time() - row["at"] >= HOLD_S or (mtime and row["config_mtime"] and mtime != row["config_mtime"]):
                _MISSING.pop(key, None)         # six hours, or the user edited the list: look again
                return False
            return True
    except Exception:
        return False


def entries() -> List[Dict[str, Any]]:
    """The missing models still held, for the panel and the doctor."""
    try:
        out = []
        with _LOCK:
            keys = list(_MISSING)
        for key in keys:
            if is_missing(key):
                with _LOCK:
                    row = dict(_MISSING.get(key) or {})
                if row:
                    out.append({"identity": key, "model": row["written"], "provider": row["provider"],
                                "suggestion": row["suggestion"], "line": line(row)})
        return out
    except Exception:
        return []


def line(row: Dict[str, Any]) -> str:
    """The one sentence the doctor and the panel say."""
    text = f"Model '{row.get('written')}' was not found by {str(row.get('provider') or 'the provider').capitalize()}"
    text += " - check the name in your fallback list"
    if row.get("suggestion"):
        text += f" (it suggests '{row['suggestion']}')"
    return text


def forget() -> None:
    try:
        with _LOCK:
            _MISSING.clear()
    except Exception:  # pragma: no cover
        pass
