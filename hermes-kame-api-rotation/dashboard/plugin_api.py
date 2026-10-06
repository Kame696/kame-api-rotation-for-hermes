"""KAME API Rotation — the panel's backend door, mounted at /api/plugins/hermes-kame-api-rotation/.

Until 1.8.2.0 the Desktop panel read ``state.json`` and wrote ``control.json``
through the raw preload bridge (``window.hermesDesktop.readFileText`` /
``writeTextFile``). The catalog asks plugins to stay on ``@hermes/plugin-sdk``,
and the SDK's door to a plugin's own backend is ``ctx.rest`` -> this router.

Nothing about the protocol changed: the Python half still publishes the
snapshot and still applies requests on its own heartbeat (``state.py``,
``control.py``). This module only moves the two files across the boundary, and
because ``ctx.rest`` is profile-scoped, ``get_hermes_home()`` here is the home of
the profile the panel is looking at — which also ends the old mismatch where the
panel read the base home while each profile wrote its own.

Never a credential: the snapshot holds fingerprints and counts only, and a
control request is validated against a closed shape before it is written.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from hermes_constants import get_hermes_home

router = APIRouter()

PLUGIN_ID = "hermes-kame-api-rotation"
CONTROL_SCHEMA = 1
_MAX_CONTROL_BYTES = 16 * 1024
_CONTROL_KEYS = {"action", "id", "key", "schema", "value"}


def _data_dir() -> Path:
    return Path(get_hermes_home()) / "plugin-data" / PLUGIN_ID


@router.get("/state")
def state() -> Response:
    """The snapshot, byte for byte, as the Python half last published it."""
    path = _data_dir() / "state.json"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="no snapshot yet")
    except OSError:
        raise HTTPException(status_code=503, detail="snapshot unreadable")
    return Response(content=text, media_type="application/json")


def _valid(request: Any) -> Dict[str, Any]:
    if not isinstance(request, dict) or set(request) - _CONTROL_KEYS:
        raise HTTPException(status_code=400, detail="unexpected request shape")
    if request.get("schema") != CONTROL_SCHEMA:
        raise HTTPException(status_code=400, detail="unknown control schema")
    if not isinstance(request.get("action"), str) or not request["action"]:
        raise HTTPException(status_code=400, detail="missing action")
    if not isinstance(request.get("id"), str) or not request["id"]:
        raise HTTPException(status_code=400, detail="missing id")
    if not isinstance(request.get("key", ""), str):
        raise HTTPException(status_code=400, detail="key must be text")
    return request


@router.post("/control")
async def control(request: Request) -> Dict[str, Any]:
    """Queue one request for the Python half; it decides and reports in the next snapshot."""
    raw = await request.body()
    if len(raw) > _MAX_CONTROL_BYTES:
        raise HTTPException(status_code=413, detail="request too large")
    try:
        body = _valid(json.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="request is not JSON")
    directory = _data_dir()
    if not directory.is_dir():
        # The snapshot directory exists once the backend has loaded KAME.
        raise HTTPException(status_code=409, detail="KAME has not started in this profile yet")
    handle, temporary = tempfile.mkstemp(dir=directory, prefix=".control-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(body, stream)
        os.replace(temporary, directory / "control.json")
    except OSError:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise HTTPException(status_code=503, detail="request could not be written")
    return {"ok": True, "id": body["id"]}
