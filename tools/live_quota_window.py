"""Does the quota window survive the real Hermes adapter? Run it and see.

Not a unit test. The unit tests use a stand-in module, which proves the
wrapper wraps; this drives the *installed* Hermes adapter with the two Google
payloads that differ only in ``quotaId``, on both the streaming and the
non-streaming path, and prints what the classifier ends up believing.

It exists because the streaming case is the one that matters and the one a
fake cannot show. Recorded output on 2026-09-06, against Hermes on this
machine::

    SEM o binding
      per-day, nao-streaming     body=NAO  stated_window=per_day   verdict=per_day
      per-day, streaming         body=NAO  stated_window=unknown   verdict=unknown
      per-minute, streaming      body=NAO  stated_window=unknown   verdict=unknown

    COM o binding
      per-day, nao-streaming     body=sim  stated_window=per_day   verdict=per_day
      per-day, streaming         body=sim  stated_window=per_day   verdict=per_day
      per-minute, streaming      body=sim  stated_window=per_minute verdict=per_minute

The middle row of the first block is the owner's whole session: Hermes
streams, the streaming path drains the response before building the error, and
``response.text`` then raises for anything reading it afterwards. 300 journal
rows, 300 of them ``unknown``.

Needs Hermes installed. Prints and exits; changes nothing.
"""
# --- do not write into the owner's evidence -------------------------------
# These tools load the real plugin, and the real plugin records what it sees
# beside the *installed* state file. Running the gate therefore appended its
# 13,561 corpus refusals to `refusals.jsonl` on this machine and filled its
# 8 MB ceiling at 14:01 on 2026-09-06 -- twenty minutes before the owner ran
# the session those recordings existed to explain. The evidence for a real
# refusal was lost to a measurement of a synthetic one.
#
# Set before the plugin is imported, so its modules read it at first use. Only
# this process is affected; nothing about the installed plugin changes.
import os as _os

_os.environ.setdefault("KAME_RECORDER_DISABLED", "1")
_os.environ.setdefault("KAME_CALL_TIMINGS_DISABLED", "1")
# ---------------------------------------------------------------------------

import importlib
import importlib.util
import json
import sys
from pathlib import Path

sys.path.insert(0, _os.path.join(_os.environ.get("LOCALAPPDATA", ""), "hermes", "hermes-agent"))

PLUGIN = Path(__file__).resolve().parents[1] / "hermes-kame-api-rotation"
PKG = "kame_live_quota"
spec = importlib.util.spec_from_file_location(
    PKG, PLUGIN / "__init__.py", submodule_search_locations=[str(PLUGIN)]
)
m = importlib.util.module_from_spec(spec)
sys.modules[PKG] = m
spec.loader.exec_module(m)

qid = importlib.import_module(PKG + ".quota_id_binding")
classify = importlib.import_module(PKG + ".core.classify")
evidence = importlib.import_module(PKG + ".core.evidence")
host_text = importlib.import_module(PKG + ".host_text")

import agent.gemini_native_adapter as adapter

w = lambda s: sys.stdout.buffer.write((s + "\n").encode("utf-8"))

BODY = {
    "error": {
        "code": 429,
        "status": "RESOURCE_EXHAUSTED",
        "message": (
            "You exceeded your current quota, please check your plan and billing "
            "details.\n* Quota exceeded for metric: "
            "generativelanguage.googleapis.com/generate_content_free_tier_requests, "
            "limit: 20, model: gemini-3.8-flash\nPlease retry in 53.585627668s."
        ),
        "details": [
            {
                "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [
                    {
                        "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
                        "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                        "quotaValue": "20",
                    }
                ],
            },
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "53.585627668s"},
        ],
    }
}

PER_MINUTE = json.loads(json.dumps(BODY))
PER_MINUTE["error"]["details"][0]["violations"][0]["quotaId"] = (
    "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"
)


class Resp:
    def __init__(self, text):
        self._t = text
        self.status_code = 429
        self.headers = {}

    @property
    def text(self):
        return self._t


def look(label, payload, streaming):
    text = json.dumps(payload)
    if streaming:
        err = adapter.gemini_http_error(Resp(""), body_text=text)
    else:
        err = adapter.gemini_http_error(Resp(text))
    body = getattr(err, "body", None)
    ev = evidence.harvest(err, message=str(err), guidance_blocks=host_text.guidance_blocks())
    named = classify.stated_window(error_body=ev.body, error=err)
    v = classify.classify(
        provider="gemini", model="gemini:gemini-3.8-flash", status_code=429,
        error_message=ev.message, error_body=ev.body, headers=ev.headers, error=err,
    )
    w("  %-34s body=%-5s stated_window=%-10s verdict.window=%s"
      % (label, "sim" if body else "NAO", named or "-",
         getattr(v, "quota_window", None) if v else "<declina>"))


w("SEM o binding (1.7.0.1 como esta hoje):")
look("per-day, nao-streaming", BODY, False)
look("per-day, streaming", BODY, True)
look("per-minute, streaming", PER_MINUTE, True)

w("")
w("instalando: %s" % qid.install())
w("COM o binding:")
look("per-day, nao-streaming", BODY, False)
look("per-day, streaming", BODY, True)
look("per-minute, streaming", PER_MINUTE, True)
qid.uninstall()
w("")
w("desinstalado, host de volta: %s" % (adapter.gemini_http_error.__name__,))
