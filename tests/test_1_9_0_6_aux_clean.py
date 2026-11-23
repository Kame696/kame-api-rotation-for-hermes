"""1.9.0.6 (decisions/0037 C10) - a background-job stop carries no provider error.

KAME's own stop is raised inside the ``except`` block of the provider's 503; Hermes' classifier follows
``__context__`` and read "overloaded" from it (errors.log 2026-11-23 10:03:18), blocking Gemini for every task.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests import test_1_8_2_5_stream_race as R

transport = R.transport
DEV_VENV = Path.home() / "dev" / "hermes-venv" / "Scripts" / "python.exe"   # a local Hermes checkout and its venv, when present
DEV_CLONE = Path.home() / "dev" / "hermes-agent"


class Overloaded(Exception):
    status_code = 503
    body = {"error": {"code": 503, "message": "The model is overloaded. Please try again later.", "status": "UNAVAILABLE"}}


@pytest.mark.parametrize("cls", ["KameAuxTimeout", "KameAuxStopped"])
def test_the_stop_never_shows_the_providers_error_as_cause_or_context(cls):
    stop = getattr(transport, cls)
    try:
        try:
            raise Overloaded("503")
        except Overloaded:
            raise stop("kame: stopped")
    except stop as caught:
        assert caught.__context__ is None and caught.__cause__ is None and caught.__suppress_context__ is True
        assert getattr(caught, "__context__", None) is None


def test_the_type_names_still_carry_what_the_host_reads():
    assert issubclass(transport.KameAuxTimeout, TimeoutError)
    assert "Timeout" in transport.KameAuxTimeout.__name__
    assert not hasattr(transport.KameAuxStopped, "status_code")


@pytest.mark.skipif(not (DEV_VENV.exists() and DEV_CLONE.exists()), reason="the dev clone of Hermes is not on this machine")
@pytest.mark.parametrize("cls", ["KameAuxTimeout", "KameAuxStopped"])
def test_hermes_own_classifier_does_not_read_overloaded_or_rate_limited(cls):
    plugin = Path(R.PLUGIN_DIR)
    script = textwrap.dedent(f"""
        import importlib.util, sys
        sys.path.insert(0, {str(DEV_CLONE)!r})
        spec = importlib.util.spec_from_file_location("kame_c10", {str(plugin / '__init__.py')!r},
                                                      submodule_search_locations=[{str(plugin)!r}])
        mod = importlib.util.module_from_spec(spec); sys.modules["kame_c10"] = mod; spec.loader.exec_module(mod)
        import kame_c10.transport as t
        from agent.error_classifier import classify_api_error
        class Overloaded(Exception):
            status_code = 503
            body = {{"error": {{"code": 503, "message": "The model is overloaded. Please try again later.", "status": "UNAVAILABLE"}}}}
        try:
            try:
                raise Overloaded("503")
            except Overloaded:
                raise t.{cls}("kame: background job stopped")
        except t.{cls} as e:
            c = classify_api_error(e, provider="gemini", model="gemini-x")
            print("REASON", c.reason.value if hasattr(c.reason, "value") else c.reason)
    """)
    done = subprocess.run([str(DEV_VENV), "-B", "-c", script], capture_output=True, text=True, timeout=120,
                          env={**__import__("os").environ, "HERMES_HOME": str(Path(R.__file__).parent)})
    out = done.stdout + done.stderr
    assert "REASON" in out, out[-1500:]
    reason = out.split("REASON", 1)[1].split()[0]
    assert reason not in ("overloaded", "rate_limit", "server_error"), reason
