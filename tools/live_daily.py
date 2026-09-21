"""Push the owner's own PerDay refusal through the INSTALLED plugin, end to end.

Everything else about 1.7.0.2 was checked against fakes, replays and the source
tree. This asks the copy Hermes will actually load: given the exact bytes Google
sent on 2026-09-06 at 18:41:15, how long does the key rest?

Answer required: a re-probe while the pool is answering, the daily cooldown once
the pool has been silent, and a genuinely stated long deadline untouched. If any
of the three is wrong here, the release is wrong, whatever the suite says.

Read-only against the install, no network, and writes nothing anywhere.

    python tools/live_daily.py
"""

from __future__ import annotations

# --- do not write into the owner's evidence -------------------------------
# This loads the real plugin, and the real plugin records what it sees beside
# the *installed* state file. Redirecting the home is the guard that actually
# works; the switches alone were not enough (see tests/conftest.py).
import os as _os
import tempfile as _tempfile

_os.environ["HERMES_HOME"] = _tempfile.mkdtemp(prefix="kame-live-daily-")
_os.environ.setdefault("KAME_RECORDER_DISABLED", "1")
_os.environ.setdefault("KAME_CALL_TIMINGS_DISABLED", "1")
# ---------------------------------------------------------------------------

import importlib
import importlib.util
import json
import sys
import time
import types
from pathlib import Path

INSTALLED = (
    Path(_os.environ.get("LOCALAPPDATA", "")) / "hermes" / "plugins"
    / "hermes-kame-api-rotation"
)
PACKAGE = "kame_installed_live_daily"

# The exact body recorded off the wire, `refusals.jsonl`, 2026-09-06 18:41:15.
REAL_PERDAY = json.dumps({
    "error": {
        "code": 429,
        "message": (
            "You exceeded your current quota, please check your plan and billing "
            "details. For more information on this error, head to: "
            "https://ai.google.dev/gemini-api/docs/rate-limits. \n* Quota exceeded "
            "for metric: generativelanguage.googleapis.com/"
            "generate_content_free_tier_requests, limit: 20, model: "
            "gemini-3.7-flash\nPlease retry in 45.610682767s."
        ),
        "status": "RESOURCE_EXHAUSTED",
        "details": [
            {
                "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [{
                    "quotaMetric": (
                        "generativelanguage.googleapis.com/"
                        "generate_content_free_tier_requests"
                    ),
                    "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                    "quotaDimensions": {"location": "global", "model": "gemini-3.7-flash"},
                    "quotaValue": "20",
                }],
            },
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "45s"},
        ],
    }
})

# OpenRouter naming a real one, for the contract that must not move.
OPENROUTER_NINE_HOURS = json.dumps({
    "error": {
        "code": 429,
        "message": "Rate limit exceeded: free-models-per-day",
        "metadata": {"headers": {"X-RateLimit-Reset": str(int((time.time() + 9 * 3600) * 1000))}},
    }
})

IDENTITY = "gemini:gemini-3.7-flash"
failures = []


def check(label, got, want):
    ok = got == want
    print("  %-4s %s" % ("PASS" if ok else "FAIL", label))
    if not ok:
        print("       got  %r" % (got,))
        print("       want %r" % (want,))
        failures.append(label)


class Refusal(Exception):
    def __init__(self, message, status, body_text):
        super().__init__(message)
        self.message = message
        self.status_code = status
        try:
            self.body = json.loads(body_text)
        except Exception:
            self.body = None
        self.response = types.SimpleNamespace(
            text=body_text or "", status_code=status, headers={}
        )


def load_installed():
    if not (INSTALLED / "__init__.py").is_file():
        print("nao achei o plugin instalado em %s" % INSTALLED)
        raise SystemExit(2)
    spec = importlib.util.spec_from_file_location(
        PACKAGE, INSTALLED / "__init__.py",
        submodule_search_locations=[str(INSTALLED)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def the_dispatch_cascade(classify_mod, carousel_mod, engine, exc, body, now):
    """What `dispatch_binding` does with a verdict, reproduced faithfully.

    Only the two branches this release touches: a named per-day window, and the
    fallback for everything else. Kept in step with `dispatch_binding` by
    `tools/host_assumptions.py`-style reading, not by import, because the real
    one needs a live agent and a stream.
    """
    verdict = classify_mod.classify(
        provider="gemini", model=IDENTITY, status_code=429,
        error_message=str(exc), error_body=body, headers={},
        error=exc, now_epoch=now,
    )
    if verdict is None:
        delay, kind, _ = carousel_mod.classify(
            exc, str(exc), status_code=429, headers={},
            daily_cooldown_s=engine.daily_cooldown_s,
        )
        stated = bool(carousel_mod.extract_delay(exc, str(exc), {}))
        return kind, delay, stated, "table"

    delay = max(0.0, verdict.reset_at - now) if verdict.reset_at else 0.0
    kind = verdict.reason
    if getattr(verdict, "quota_window", "") == "per_day":
        kind = "daily"
        if getattr(verdict, "source", "") == "window":
            delay = float(carousel_mod.extract_delay(exc, str(exc), {}) or 0.0)
    stated = bool(carousel_mod.extract_delay(exc, str(exc), {}))
    return kind, delay, stated, verdict.source or "verdict"


def main():
    module = load_installed()
    print("plugin instalado: %s" % getattr(
        importlib.import_module(f"{PACKAGE}.core"), "__version__", "?"))
    print("fonte           : %s" % INSTALLED)
    print()

    carousel_mod = importlib.import_module(f"{PACKAGE}.core.carousel")
    classify_mod = importlib.import_module(f"{PACKAGE}.core.classify")

    body = json.loads(REAL_PERDAY)
    exc = Refusal(
        "Gemini HTTP 429 (RESOURCE_EXHAUSTED): "
        + body["error"]["message"], 429, REAL_PERDAY)

    print("[1] o payload real dele — a pool ainda responde")
    engine = carousel_mod.Carousel(daily_cooldown_s=3600.0)
    now = time.time()
    kind, delay, stated, source = the_dispatch_cascade(
        classify_mod, carousel_mod, engine, exc, body, now)
    check("a janela e lida como diaria", kind, "daily")
    check("o numero de 3600 e reconhecido como nosso", source, "window")
    # 45.610682767s na prosa: chega inteiro, com as casas decimais e tudo.
    check("e o hint do provedor chega inteiro", round(delay, 1), 45.6)
    applied = engine.mark(IDENTITY, "k0", False, delay, kind, now=now, stated=stated)
    check("descanso aplicado = re-sondagem", applied, carousel_mod.RL_BACKOFF_CAP_S)
    check("e nao a hora", applied < 3600.0, True)

    print()
    print("[2] o mesmo payload depois de 20 min de silencio da pool")
    quiet = now + carousel_mod.POOL_SILENCE_BEFORE_THE_DAY_S + 1.0
    applied = engine.mark(IDENTITY, "k0", False, delay, kind, now=quiet, stated=stated)
    check("agora sim a hora", applied, 3600.0)

    print()
    print("[3] uma resposta em qualquer chave reabre a duvida")
    fresh = carousel_mod.Carousel(daily_cooldown_s=3600.0)
    fresh.mark(IDENTITY, "k0", False, delay, kind, now=now, stated=stated)
    fresh.mark(IDENTITY, "k1", True, now=now + 60.0)
    applied = fresh.mark(IDENTITY, "k0", False, delay, kind, now=quiet, stated=stated)
    check("volta a re-sondagem", applied, carousel_mod.RL_BACKOFF_CAP_S)

    print()
    print("[4] o hint em milissegundos que custou cinco chaves em 07/09")
    # 1.7.0.3. Os bytes exatos de `refusals.jsonl` as 14:31:00: mesmo rotulo
    # PerDay do caso [1], mas com o hint escrito em MILISSEGUNDOS, que e como
    # o Google escreve sempre que a espera e menor que um segundo. Ate esta
    # versao a alternancia de unidades da regex nao tinha ramo `ms`, entao o
    # `m` casava, o `s` sobrava, e 683 milissegundos viravam 683 MINUTOS —
    # 40.983s de castigo numa chave sadia, cinco vezes numa sessao de 90 min.
    ms_body = json.loads(REAL_PERDAY)
    ms_body["error"]["message"] = (
        "You exceeded your current quota, please check your plan and billing "
        "details. For more information on this error, head to: "
        "https://ai.google.dev/gemini-api/docs/rate-limits. \n* Quota exceeded "
        "for metric: generativelanguage.googleapis.com/"
        "generate_content_free_tier_requests, limit: 20, model: "
        "gemini-3.8-flash\nPlease retry in 683.050353ms."
    )
    ms_text = json.dumps(ms_body)
    ms_exc = Refusal(
        "Gemini HTTP 429 (RESOURCE_EXHAUSTED): " + ms_body["error"]["message"],
        429, ms_text)
    ms_engine = carousel_mod.Carousel(daily_cooldown_s=3600.0)
    now2 = time.time()
    kind, delay, stated, source = the_dispatch_cascade(
        classify_mod, carousel_mod, ms_engine, ms_exc, ms_body, now2)
    check("683.050353ms lido como milissegundos", round(delay, 4), 0.6831)
    check("e nao como 683 minutos", delay < 1.0, True)
    applied = ms_engine.mark(
        IDENTITY, "k0", False, delay, kind, now=now2, stated=stated)
    check("descanso aplicado = re-sondagem", applied, carousel_mod.RL_BACKOFF_CAP_S)
    check("e nao as 11.4 horas de 07/09", applied < 3600.0, True)

    print()
    print("[5] o prazo de 9h do OpenRouter agora para no teto do dono")
    # Ate a 1.7.0.x este numero era servido inteiro: o provedor declarou nove
    # horas e o plugin obedecia. Em 15/09/2026 o dono fechou a regra do teto
    # (`max_hold_seconds`, padrao 3600) e ela vale em TODO caminho, inclusive
    # num prazo declarado. O custo aceito e uma requisicao recusada por hora
    # numa chave que de fato esta fora; o ganho e que nenhuma chave boa fica
    # nove horas na geladeira por um numero que ninguem conferiu.
    # Ver `decisions/0005`, portao G8 do `PLAN_1.8.0.0.md`.
    or_exc = Refusal("Rate limit exceeded: free-models-per-day", 429, OPENROUTER_NINE_HOURS)
    or_engine = carousel_mod.Carousel(daily_cooldown_s=3600.0)
    applied = or_engine.mark(
        "openrouter:free", "k0", False, 9 * 3600.0, "daily",
        now=time.time(), stated=True)
    check("nove horas cortadas no teto de uma hora", applied, or_engine.max_hold_s)
    check("e o teto e o do dono, nao um numero inventado aqui",
          or_engine.max_hold_s, 3600.0)
    assert or_exc is not None

    print()
    if failures:
        print("%d FALHOU: %s" % (len(failures), "; ".join(failures)))
        return 1
    print("o payload real dele entrou e uma re-sondagem saiu — classificacao,")
    print("        janela, numero e descanso, todos do plugin instalado.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
