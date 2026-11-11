"""Chaos run against the Hermes transport: a fake provider that fails in every shape.

Offline. HERMES_HOME is moved to a temp folder before the plugin loads, so nothing reaches
the owner's logs. A failure here is a crash, a runaway loop or a broken invariant, reported
with the seed that reproduces it.

    python tools/chaos.py --seeds 200 --calls 25
"""
from __future__ import annotations

import argparse
import collections
import importlib
import importlib.util
import os
import random
import sys
import tempfile
import traceback
from pathlib import Path
from types import SimpleNamespace

HOME = tempfile.mkdtemp(prefix="kame-chaos-")
os.environ["HERMES_HOME"] = HOME
os.environ["KAME_RECORDER_DISABLED"] = "1"
os.environ["KAME_CALL_TIMINGS_DISABLED"] = "1"
if "--hedge" not in sys.argv:
    os.environ["KAME_HEDGING_DISABLED"] = "1"   # the virtual clock cannot drive racing threads

import faulthandler
import logging

logging.disable(logging.CRITICAL)

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = Path(os.environ.get("KAME_PLUGIN_DIR") or ROOT / "hermes-kame-api-rotation")
PACKAGE = "kame_chaos_under_test"
spec = importlib.util.spec_from_file_location(PACKAGE, PLUGIN_DIR / "__init__.py",
                                              submodule_search_locations=[str(PLUGIN_DIR)])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
transport = importlib.import_module(f"{PACKAGE}.transport")
carousel_mod = importlib.import_module(f"{PACKAGE}.core.carousel")
settings = importlib.import_module(f"{PACKAGE}.settings")
state_mod = importlib.import_module(f"{PACKAGE}.state")
state_mod.publish = lambda *a, **k: None   # no heartbeat here: each attempt would write a file on this thread
transport._turn_status = lambda *a, **k: False   # a failed host import per virtual second is not a real cost

IDENTITY = "gemini:gemini-test"
REQUEST = {"model": "gemini-test", "messages": [{"role": "user", "content": "hi"}], "stream": True}


class Runaway(BaseException):
    pass


class Clock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def time(self):
        return self.now

    def monotonic(self):
        return self.now

    def time_ns(self):
        return int(self.now * 1e9)

    def sleep(self, s):
        self.now += max(0.0, float(s))


class Refusal(Exception):
    def __init__(self, status, message, details=(), body="default", headers=None):
        super().__init__(message)
        self.status_code = status
        self.body = ({"error": {"code": status, "message": message,
                                "status": "RESOURCE_EXHAUSTED" if status == 429 else "UNAVAILABLE",
                                "details": list(details)}} if body == "default" else body)
        if headers is not None:
            self.response = SimpleNamespace(headers=headers, status_code=status, text=str(message))


def quota(quota_id, retry=None):
    d = [{"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [{"quotaId": quota_id}]}]
    if retry is not None:
        d.append({"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry})
    return d


def text(t, finish=None):
    delta = SimpleNamespace(role="assistant", content=t, tool_calls=None)
    return SimpleNamespace(id="c", model="m", choices=[SimpleNamespace(index=0, delta=delta, finish_reason=finish)], usage=None)


def tool(name="", args="", finish=None):
    call = SimpleNamespace(index=0, id="call_1" if name else None, type="function",
                           function=SimpleNamespace(name=name or None, arguments=args))
    delta = SimpleNamespace(role="assistant", content=None, tool_calls=[call])
    return SimpleNamespace(id="c", model="m", choices=[SimpleNamespace(index=0, delta=delta, finish_reason=finish)], usage=None)


def finish(reason="stop"):
    delta = SimpleNamespace(role=None, content=None, tool_calls=None)
    return SimpleNamespace(id="c", model="m", choices=[SimpleNamespace(index=0, delta=delta, finish_reason=reason)], usage=None)


FAULTS = {
    "ok": lambda r: [text("Hello "), text("world", "stop")],
    "ok_tool": lambda r: [tool("search", '{"q":"x"}'), finish("tool_calls")],
    "rpm": lambda r: Refusal(429, "Quota exceeded for metric", quota("GenerateRequestsPerMinutePerProjectPerModel-FreeTier", f"{r.randint(1, 59)}s")),
    "rpd": lambda r: Refusal(429, "Quota exceeded for metric", quota("GenerateRequestsPerDayPerProjectPerModel-FreeTier", f"{r.randint(1, 59)}s")),
    "bare429": lambda r: Refusal(429, "Resource has been exhausted (e.g. check quota)."),
    "503": lambda r: Refusal(503, "The model is overloaded. Please try again later."),
    "500": lambda r: Refusal(500, "Internal error encountered."),
    "401": lambda r: Refusal(401, "API key not valid. Please pass a valid API key."),
    "403": lambda r: Refusal(403, "Permission denied"),
    "400": lambda r: Refusal(400, "Invalid request: unsupported parameter"),
    "ctx400": lambda r: Refusal(400, "prompt is too long: 214290 tokens > 200000 maximum"),
    "timeout": lambda r: TimeoutError("The read operation timed out"),
    "conn": lambda r: ConnectionError("Connection reset by peer"),
    "cut": lambda r: [text("Hello "), ConnectionError("peer closed connection without sending complete message body")],
    "cut_tool": lambda r: [tool("search", '{"q":"pa'), ConnectionError("stream reset")],
    "cut_nofinish": lambda r: [text("Hello ")],
    "empty": lambda r: [],
    "no_choices": lambda r: [SimpleNamespace(id="c", model="m", choices=[], usage=None), finish()],
    "none_choices": lambda r: [SimpleNamespace(id="c", model="m", choices=None, usage=None), finish()],
    "len": lambda r: [text("Hello"), finish("length")],
    "filter": lambda r: [finish("content_filter")],
    "reason_only": lambda r: [SimpleNamespace(id="c", model="m", usage=None, choices=[SimpleNamespace(index=0, finish_reason=None, delta=SimpleNamespace(role="assistant", content=None, tool_calls=None, reasoning_content="thinking"))]), finish()],
    "weird_status": lambda r: Refusal("429", "Too many requests", body=None),
    "body_str": lambda r: Refusal(429, "x", body="not json at all"),
    "body_list": lambda r: Refusal(503, "x", body=[{"error": {"code": 503}}]),
    "bad_delay": lambda r: Refusal(429, "Quota", quota("X", r.choice(["abc", "-5s", "1e9s", "", "0s", "999999999999s", "NaNs", "12"]))),
    "bad_details": lambda r: Refusal(429, "Quota", [None, 5, "x", {"@type": None}, {"violations": "no"}, {"violations": [None]}]),
    "header_retry": lambda r: Refusal(429, "Slow down", body=None, headers={"retry-after": r.choice(["7", "Wed, 21 Oct 2015 07:28:00 GMT", "soon", "-1", "1e400"])}),
    "bare_exc": lambda r: Exception("something odd"),
    "value_err": lambda r: ValueError("Expecting value: line 1 column 1 (char 0)"),
    "unicode": lambda r: [text("olá \ud83d"), text("!", "stop")],
    "huge_text": lambda r: [text("x" * 200000, "stop")],
}
FATAL_TYPES = (TypeError, KeyError, AttributeError, IndexError, UnboundLocalError, NameError, RecursionError, AssertionError, ZeroDivisionError, OverflowError)


class Script:
    def __init__(self, rng, weights, limit=400):
        self.rng, self.weights, self.limit = rng, weights, limit
        self.names = list(weights)
        self.w = [weights[n] for n in self.names]
        self.asked = []
        self.history = []

    def run(self, key, request):
        self.asked.append(key)
        if len(self.asked) > self.limit:
            raise Runaway(f"{len(self.asked)} requests in one call")
        name = self.rng.choices(self.names, self.w)[0]
        self.history.append(name)
        steps = FAULTS[name](self.rng)
        if isinstance(steps, BaseException):
            raise steps

        def gen():
            for step in steps:
                if isinstance(step, BaseException):
                    raise step
                yield step
        return gen()


class FakeCall(transport.Call):
    def __init__(self, script, keys):
        self.script = script
        self._keys = list(keys)
        self.kwargs = dict(REQUEST)
        self.provider = "gemini"
        self.identity = IDENTITY

    def keys(self):
        return list(self._keys)

    def client_for(self, key, attempt):
        def create(**request):
            return self.script.run(key, request)
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    def cancelled(self):
        return False


def scenario(rng):
    """A mix of faults: mostly healthy, or a bad day, or nothing but one fault."""
    kind = rng.choice(["healthy", "bad", "storm", "single", "dead"])
    names = list(FAULTS)
    if kind == "healthy":
        w = {n: 1 for n in names}
        w["ok"] = 60
    elif kind == "bad":
        w = {n: rng.randint(1, 8) for n in names}
        w["ok"] = 15
    elif kind == "storm":
        w = {n: rng.randint(0, 5) for n in names}
        w["ok"] = 3
        w[rng.choice(["rpm", "rpd", "503", "bare429"])] = 40
    elif kind == "single":
        w = {"ok": 5, rng.choice(names): 20}
    else:
        w = {n: rng.randint(1, 5) for n in names if n != "ok" and n != "ok_tool"}
        w["ok"] = 1
    return kind, w


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=100)
    ap.add_argument("--calls", type=int, default=20)
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--show", type=int, default=6)
    ap.add_argument("--hedge", action="store_true")
    a = ap.parse_args()
    fake_time = {}
    outcomes = collections.Counter()
    defects = collections.Counter()
    examples = {}
    waits = []
    for seed in range(a.start, a.start + a.seeds):
        rng = random.Random(seed)
        clock = Clock()
        fake = SimpleNamespace(time=clock.time, monotonic=clock.monotonic, time_ns=clock.time_ns, sleep=clock.sleep,
                               strftime=__import__("time").strftime, localtime=__import__("time").localtime,
                               gmtime=__import__("time").gmtime, perf_counter=clock.monotonic)
        carousel_mod.time = fake
        transport.time = fake
        settings.forget()
        kind, weights = scenario(rng)
        keys = [f"AIzaSyKEY{seed}x{i}" + "0" * 29 for i in range(rng.choice([1, 2, 3, 5, 14]))]
        t = transport.KameTransport(engine=carousel_mod.Carousel(), sleep=clock.sleep)
        for n in range(a.calls):
            script = Script(rng, weights)
            started = clock.now
            faulthandler.dump_traceback_later(120, exit=True)   # a real-time hang is a defect too
            try:
                if rng.random() < 0.7:
                    said = ""
                    for chunk in t.stream(FakeCall(script, keys)):
                        for choice in (getattr(chunk, "choices", None) or []):
                            said += getattr(getattr(choice, "delta", None), "content", None) or ""
                    if "Hello Hello" in said or "world" in said.replace("world", "", 1):
                        defects["DUPLICATE TEXT"] += 1
                        examples.setdefault("DUPLICATE TEXT", (seed, n, kind, len(keys), script.history[-4:], said[:80]))
                else:
                    t.complete(FakeCall(script, keys))
                outcomes["answered"] += 1
            except Runaway as e:
                defects["RUNAWAY"] += 1
                examples.setdefault("RUNAWAY", (seed, n, kind, len(keys), collections.Counter(script.history).most_common(4), str(e)))
            except FATAL_TYPES as e:
                tb = traceback.extract_tb(e.__traceback__)
                where = next((f"{Path(f.filename).name}:{f.lineno}" for f in reversed(tb) if PLUGIN_DIR.name in f.filename or "core" in f.filename), "?")
                sig = f"{type(e).__name__} at {where}"
                defects[sig] += 1
                examples.setdefault(sig, (seed, n, kind, len(keys), script.history[-3:], str(e)[:120]))
            except BaseException as e:
                outcomes[type(e).__name__] += 1
            faulthandler.cancel_dump_traceback_later()
            waits.append(clock.now - started)
    print("outcomes", outcomes.most_common())
    waits.sort()
    print("virtual wait per call: p50", round(waits[len(waits) // 2], 1), "p99", round(waits[int(len(waits) * .99)], 1), "max", round(waits[-1], 1))
    print("defects", sum(defects.values()), "distinct", len(defects))
    for sig, count in defects.most_common(25):
        print(f"  {count:5} {sig}  e.g. {examples.get(sig)}")


if __name__ == "__main__":
    main()
