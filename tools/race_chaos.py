"""Chaos across the model ladder: four models of one provider, real threads, real (scaled) time.

``tools/chaos.py`` drives one model on a virtual clock and cannot race. This one drives the paths
added since 1.8.2.8 - the cross-model race, the widened race, the start probe (E8 tail), the
stand-in sibling, mid-stream resume across models - with a fake provider that answers every
request in a random shape, and checks after every reply:

  I1  the reply ends (an answer or an exception) within a bound; no hang
  I2  the text shown is the canonical answer or a beginning of it; never a word twice, never
      a word out of order. A beginning only ("short" in the outcomes) is allowed only when the
      reply's last request got the empty shape (a continuation the fake ended empty after a cut:
      a model that says it is done is believed, by design). Any other beginning is a defect (F6).
  I3  a reserve key is asked only when no other key of that model is healthy (E4: the reply's
      own request and the same-model backups)
  I3b a cross-model leg or the start probe never asks a reserve key, whatever else is ready (R1,
      read from ``leg.race_kind``). A widened leg may (F1), so it is not checked here.
  I4  an exception that reaches the caller is a provider refusal or one of KAME's own,
      never an internal error (AttributeError, KeyError, TypeError, ...)

Offline. HERMES_HOME is a temp folder; nothing reaches the owner's logs. Constants are the
scaled ones of ``tests/test_1_8_2_9_silent_two_models.py`` (race at 0.3 s, slow bound 1 s).

    python tools/race_chaos.py --seeds 150
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import importlib
import importlib.util
import os
import random
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from types import SimpleNamespace

HOME = tempfile.mkdtemp(prefix="kame-race-chaos-")
os.environ["HERMES_HOME"] = HOME
for _name in list(os.environ):
    if _name.startswith("KAME_"):
        del os.environ[_name]
os.environ["KAME_RECORDER_DISABLED"] = "1"
os.environ["KAME_CALL_TIMINGS_DISABLED"] = "1"
os.environ["KAME_MAX_TOTAL_WAIT"] = "8"          # a chat never gives up; here it must, to be checked

import logging

logging.disable(logging.CRITICAL)

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = Path(os.environ.get("KAME_PLUGIN_DIR") or ROOT / "hermes-kame-api-rotation")
PACKAGE = "kame_race_chaos_under_test"
spec = importlib.util.spec_from_file_location(PACKAGE, PLUGIN_DIR / "__init__.py",
                                              submodule_search_locations=[str(PLUGIN_DIR)])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
transport = importlib.import_module(f"{PACKAGE}.transport")
carousel_mod = importlib.import_module(f"{PACKAGE}.core.carousel")
hedging_mod = importlib.import_module(f"{PACKAGE}.core.hedging")
pacing_mod = importlib.import_module(f"{PACKAGE}.core.pacing")
chain_mod = importlib.import_module(f"{PACKAGE}.core.fallback_chain")
settings = importlib.import_module(f"{PACKAGE}.settings")
state_mod = importlib.import_module(f"{PACKAGE}.state")
state_mod.publish = lambda *a, **k: None
transport._turn_status = lambda *a, **k: False

MODELS = ["gemini-race", "gemini-race-two", "gemini-race-three", "gemini-race-four"]
CHAIN = ("model:\n  provider: gemini\n  default: gemini-race\nfallback_providers:\n"
         + "".join(f"  - provider: gemini\n    model: {m}\n" for m in MODELS[1:]))
KEYS = [f"AIzaSyCHAOS{i}" + "0" * 28 for i in range(5)]
ANSWER = ["the ", "quick ", "brown ", "fox ", "jumps"]
CANON = "".join(ANSWER)

# 1.8.4.1 (K6): answers that repeat, five pieces each like the prose one so a seed draws the same
# shapes (cut points, delays) whatever the text. Seeds divisible by 3 keep the prose answer, so those
# seed numbers in the old records mean the same run; ``--no-patterns`` keeps it for every seed.
PROSE = list(ANSWER)
PATTERN_ANSWERS = [
    ["ha ha ha ha ", "ha ha ha ha ", "ha ha ha ha ", "ha ha ha ha ", "ha ha ha ha"],      # a word repeated
    ["| a | 1 |\n", "| a | 1 |\n", "| a | 1 |\n", "| a | 1 |\n", "| b | 2 |"],        # equal table rows
    ["The rule: ", "----------", "----------", "----------", " and done"],              # a rule of dashes in prose
    ["x = f(1)\n", "x = f(1)\n", "x = f(1)\n", "x = f(1)\n", "return x"],             # equal code lines
    # more of both kinds: text before the pattern (a sentence, a header row, a def line) and a bare rule of dashes.
    # A repeating answer (REPEATS below) is only ever started again by the provider here, never carried
    # on. Carried on after a cut, repeating text cannot be told from a repeat of what was shown, and the
    # stitcher reads it as a repeat: an open point, measured and written down (PLAN_1.8.4.1 K2, withdrawn).
    ["He said: ", "ha ha ha ha ", "ha ha ha ha ", "ha ha ha ha ", "ha ha ha ha"],
    ["| h | n |\n", "| a | 1 |\n", "| a | 1 |\n", "| a | 1 |\n", "| b | 2 |"],
    ["----------", "----------", "----------", "----------", " and done"],               # a bare rule of dashes
    ["def f(x):\n", "x = f(1)\n", "x = f(1)\n", "x = f(1)\n", "return x"],
]
USE_PATTERNS = True
REPEATS = False     # the chosen answer is one of the repeating ones


def choose_answer(seed):
    """The answer this seed asks for: prose, or (seed not divisible by 3) one that repeats."""
    global ANSWER, CANON, REPEATS
    ANSWER = PROSE if (not USE_PATTERNS or seed % 3 == 0) else PATTERN_ANSWERS[seed % len(PATTERN_ANSWERS)]
    CANON = "".join(ANSWER)
    REPEATS = ANSWER is not PROSE


INTERNAL = (AttributeError, KeyError, TypeError, IndexError, NameError, UnboundLocalError,
            AssertionError, ZeroDivisionError, RecursionError)


def label(key):
    return "key:" + hashlib.sha256(key.encode()).hexdigest()[:6]


class Refusal(Exception):
    def __init__(self, status, message, details=()):
        super().__init__(message)
        self.status_code = status
        self.body = {"error": {"code": status, "message": message,
                               "status": {429: "RESOURCE_EXHAUSTED", 404: "NOT_FOUND"}.get(status, "UNAVAILABLE"),
                               "details": list(details)}}


def per_minute():
    return Refusal(429, "Quota exceeded for metric: generate_content_free_tier_requests", [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
         "violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "2s"}])


def daily():
    return Refusal(429, "Quota exceeded for metric: generate_content_free_tier_requests", [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
         "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier", "quotaValue": "20"}]}])


def busy():
    return Refusal(503, "The model is overloaded. Please try again later.")


def gone(model):
    return Refusal(404, f"models/{model} is not found for API version v1beta, or is not supported for generateContent.")


def chunk(text=None, finish=None):
    delta = SimpleNamespace(role=None, content=text, tool_calls=None)
    return SimpleNamespace(id="c", model="m", usage=None,
                           choices=[SimpleNamespace(index=0, delta=delta, finish_reason=finish)])


class Response:
    def __init__(self):
        self.closed = threading.Event()

    def close(self):
        self.closed.set()


class FakeStream:
    def __init__(self, steps):
        self.steps = steps
        self.response = Response()

    def __iter__(self):
        for step in self.steps:
            if isinstance(step, (int, float)):
                end = time.monotonic() + step
                while time.monotonic() < end:
                    if self.response.closed.is_set():
                        raise ConnectionError("response closed")
                    time.sleep(0.005)
                continue
            if isinstance(step, BaseException):
                raise step
            yield step

    def close(self):
        self.response.close()


def shape(rng, model):
    """One request's answer: (name, steps or exception factory)."""
    roll = rng.random()
    if roll < 0.38:
        return "fast", [rng.uniform(0.02, 0.4)] + [chunk(w) for w in ANSWER] + [chunk(finish="stop")]
    if roll < 0.50:
        return "silent", [3.0] + [chunk(w) for w in ANSWER] + [chunk(finish="stop")]
    if roll < 0.62:
        return "503", busy
    if roll < 0.70:
        return "per_minute", per_minute
    if roll < 0.76:
        return "daily", daily
    if roll < 0.80:
        return "gone", lambda: gone(model)
    if roll < 0.92:
        cut = rng.randint(1, len(ANSWER) - 1)
        return "cut", ([rng.uniform(0.02, 0.2)] + [chunk(w) for w in ANSWER[:cut]]
                       + [ConnectionError("peer closed connection without sending complete message body")])
    return "empty", [rng.uniform(0.02, 0.2), chunk(finish="stop")]


def continued_from(messages):
    """The text this request asks the model to continue (the last assistant turn), or ``None``."""
    for m in reversed(list(messages or [])[-2:]):
        if isinstance(m, dict) and m.get("role") == "assistant":
            content = m.get("content")
            return content if isinstance(content, str) else None
    return None


_LEG = threading.local()
_real_pump = transport._StreamLeg._pump


def _pump(self, wire, request):
    """Tell the fake provider which race leg is asking (the leg runs on its own thread)."""
    _LEG.leg = self
    try:
        return _real_pump(self, wire, request)
    finally:
        _LEG.leg = None


transport._StreamLeg._pump = _pump


class Call(transport.Call):
    def __init__(self, t, rng, reserve_keys):
        self.t = t
        self.rng = rng
        self.reserve = set(reserve_keys)
        self._keys = list(KEYS)
        self.kwargs = {"model": MODELS[0], "messages": [{"role": "user", "content": "hi"}], "stream": True}
        self.provider = "gemini"
        self.identity = "gemini:" + MODELS[0]
        self.closed = False
        self.asked = []
        self.violations = []
        self.lock = threading.Lock()

    def keys(self):
        return list(self._keys)

    def entry(self, key):
        return None

    def entry_id(self, key):
        return ""

    def cancelled(self):
        return self.closed

    def _healthy_ordinary(self, model):
        ordinary = [k for k in KEYS if k not in self.reserve]
        best = 0
        for name, pool in list(self.t.engine._pools.items()):
            if name == "gemini:" + model or name.startswith("gemini:" + model + ":"):
                best = max(best, self.t.engine.healthy_count(name, ordinary))
        return best

    def client_for(self, key, attempt):
        def create(**request):
            model = request.get("model")
            if key in self.reserve and len(self.reserve) < len(KEYS):
                healthy = self._healthy_ordinary(model)
                if healthy:
                    self.violations.append(f"I3 reserve key asked on {model} while {healthy} other key(s) healthy")
                leg = getattr(_LEG, "leg", None)
                kind = getattr(leg, "race_kind", None)
                if kind in ("cross", "probe"):
                    # I3b (R1): a cross-model leg and the start probe are backup requests; they never take
                    # a reserve key, whatever else is ready. (A widen may: F1.)
                    self.violations.append(f"I3b {kind} race leg asked a reserve key on {model}")
            with self.lock:
                name, how = shape(self.rng, model)
                prefix = continued_from(request.get("messages"))
                if (prefix and not callable(how) and CANON.startswith(prefix.rstrip())
                        and self.rng.random() < 0.5 and not REPEATS):
                    # continue where the cut answer stopped, as a model handed a prefill does (R5):
                    # the rest opens with the whitespace the prefill had stripped
                    rest = CANON[len(prefix.rstrip()):]
                    words = [step for step in how if hasattr(step, "choices")
                             and getattr(step.choices[0].delta, "content", None)]
                    tail = [chunk(rest)] if rest else []
                    if name == "cut" and words:
                        tail = [chunk(rest[: max(1, len(rest) // 2)]),
                                ConnectionError("peer closed connection without sending complete message body")]
                    how = [how[0] if isinstance(how[0], (int, float)) else 0.05] + tail + (
                        [chunk(finish="stop")] if name != "cut" else [])
                    name += "+cont"
                self.asked.append((model, key[-6:], name))
            if callable(how):
                raise how()
            return FakeStream(how)
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def shown(chunks):
    out = ""
    for c in chunks:
        for ch in getattr(c, "choices", None) or []:
            text = getattr(ch.delta, "content", None)
            if text:
                out += text
    return out


def setup_state(t, rng, reserve=()):
    """Random busy windows, tails and resting keys, as the owner's day leaves them."""
    now = time.time()
    if reserve and rng.random() < 0.4:
        # a model whose only free key is a reserve key: the case R1 (I3b) is about
        ident = "gemini:" + rng.choice(MODELS[1:])
        with t.engine._lock:
            t.engine._pool_for(ident, list(KEYS), now)      # the whole pool is known, the reserve key too
        for k in KEYS:
            if k not in reserve:
                t.engine.mark(ident, k, False, 600.0, "server")
    for m in MODELS:
        ident = "gemini:" + m
        roll = rng.random()
        if roll < 0.2:
            t._note_busy(ident)
            if rng.random() < 0.5:
                t._probed_at[ident] = now - 60.0          # due another look
        elif roll < 0.35:                                  # the tail of a window (E8)
            t._busy_until[ident] = now - rng.uniform(1.0, 20.0)
            t._busy_window[ident] = rng.choice([30.0, 60.0])
            t._probed_at[ident] = now - 90.0
        for k in KEYS:
            if rng.random() < 0.25:
                kind = rng.choice(["server", "per_minute", "daily", "timeout"])
                t.engine.mark(ident, k, False, rng.choice([1.0, 3.0, 600.0]), kind)


def run_one(seed):
    choose_answer(seed)
    rng = random.Random(seed)
    settings.forget()
    chain_mod.forget()
    hedging_mod.HEDGE_TRACKER.clear()
    pacing_mod.PACER.clear()
    reserve = [KEYS[0]] if rng.random() < 0.5 else []
    if reserve:
        os.environ["KAME_KEY_RESERVE"] = ", ".join(label(k) for k in reserve)
    else:
        os.environ.pop("KAME_KEY_RESERVE", None)
    os.environ["KAME_HEDGE_MODE"] = rng.choice(["adaptive", "adaptive", "max", "off"])
    settings.forget()
    pacing_mod.PACER._synced_at.clear()
    triggers = rng.choice([(30.0, 60.0), (0.2, 0.4)])
    hedging_mod.HEDGE_TRACKER.stream_triggers = lambda *a, **k: triggers
    t = transport.KameTransport(engine=carousel_mod.Carousel())
    setup_state(t, rng, reserve)
    call = Call(t, rng, reserve)
    box = {"chunks": []}

    def body():
        try:
            for chunk in t.stream(call):      # kept as it streams: text shown before an error counts too
                box["chunks"].append(chunk)
            box["out"] = box["chunks"]
        except BaseException as exc:  # noqa: BLE001 - the chaos wants every shape
            box["exc"] = exc
            box["tb"] = traceback.format_exc()

    th = threading.Thread(target=body, daemon=True)
    started = time.monotonic()
    th.start()
    th.join(40.0)
    took = time.monotonic() - started
    problems = list(call.violations)
    if th.is_alive():
        call.closed = True
        problems.append(f"I1 no end after {took:.0f}s")
    elif "exc" in box:
        exc = box["exc"]
        if isinstance(exc, INTERNAL):
            problems.append(f"I4 internal {type(exc).__name__}: {exc}\n{box['tb'][-1500:]}")
        partial = shown(box["chunks"])
        if not CANON.startswith(partial):
            problems.append(f"I2 shown before {type(exc).__name__}: {partial!r}")
    else:
        text = shown(box["out"])
        if not CANON.startswith(text):
            problems.append(f"I2 shown {text!r}")
        elif text != CANON:
            # F6: a beginning only is "short" when the last request got the empty shape (a model that says
            # it is done after a cut is believed, by design). Any other strict beginning is a defect.
            if text and (not call.asked or call.asked[-1][2] != "empty"):
                last = call.asked[-1] if call.asked else None
                problems.append(f"I2 shown only a beginning {text!r} and the last request was not empty: {last}")
            return problems, took, call.asked, "short" if text else "empty"
    return problems, took, call.asked, ("exc" in box and type(box["exc"]).__name__) or "answer"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=150)
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--show", action="store_true", help="print every seed that did not end in a full answer")
    ap.add_argument("--no-patterns", action="store_true",
                    help="every seed asks for the prose answer (the runs of the records before 1.8.4.1)")
    args = ap.parse_args()
    global USE_PATTERNS
    USE_PATTERNS = not args.no_patterns
    (Path(HOME) / "config.yaml").write_text(CHAIN, encoding="utf-8")
    transport._SLOW_S = {False: (1.0, 1.0, 1.0), True: (1.0, 1.0, 1.0)}
    transport._CROSS_COLD_S = 0.3
    transport._CROSS_STEP_S = 0.4
    transport._CROSS_MIN_S = 0.3
    outcomes = collections.Counter()
    times = []
    bad = []
    continued = 0
    for seed in range(args.start, args.start + args.seeds):
        problems, took, asked, outcome = run_one(seed)
        outcomes[outcome] += 1
        continued += sum(1 for _m, _k, s in asked if s.endswith("+cont"))
        times.append(took)
        if args.show and outcome != "answer" and not problems:
            print(f"seed {seed}: {outcome}, {took:.2f}s, asked {asked[-6:]}", flush=True)
        if problems:
            bad.append((seed, problems, asked))
            print(f"seed {seed}: " + " | ".join(p.splitlines()[0] for p in problems), flush=True)
    times.sort()
    print(f"seeds {args.seeds}, defects {len(bad)}, outcomes {dict(outcomes)}, "
          f"p50 {times[len(times) // 2]:.2f}s, max {times[-1]:.2f}s, "
          f"requests that continued a cut answer {continued}")
    for seed, problems, asked in bad[:5]:
        print(f"--- seed {seed}")
        for p in problems:
            print(p)
        print("asked:", asked[:30])
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
