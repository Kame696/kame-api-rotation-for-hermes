# KAME 1.0.8 Blueprint — Hermes Agent

> **Status:** Blueprint em refino. Nenhuma linha de código executada ainda.
> Baseline: 1103/1103 testes passando (1.0.7 atual).

## Filosofia

O 1.0.8 é uma **limpeza com ganho**, não uma refatoração. O núcleo provado do
1.0.2 permanece intocado. As adições boas do Gemini (classify.py, quota.py)
são mantidas. As adições problemáticas (StreamWatchdog, emit_wait_notice em
rotações, VIGIL_FIRST_S=5s) são removidas e substituídas por abordagens
seguras. Novos recursos (visão ao vivo, bind by signature, jitter, first-token
timeout opcional) são adicionados com default-safe.

## O que MANTER (intocado)

| Arquivo | O que | Por quê |
|---|---|---|
| `core/carousel.py` | Motor RPM+LRU+eternal | Paridade A0 v1.0.9. Provado. |
| `core/quota.py` | Parsing de cooldown (Protobuf, RFC3339, JSON) | Maduro. |
| `core/classify.py` | Classificação + todos os 8 novos padrões + structured_error_tokens | Bom trabalho do Gemini. |
| `core/storm.py` | Colapso de logs | Provado desde 1.0.2. |
| `core/journal.py` | Journal de predições | Observabilidade. |
| `core/ledger.py` | Per-model benches | Paridade A0. |
| `core/escalate.py` | Backoff per key+kind | Paridade A0. |
| `core/answer.py` | Empty answer guard | Provado. |
| `core/dispersion.py` | Spread tracking | Observabilidade. |
| `core/tally.py` | Classification counts | Observabilidade. |
| `core/reconcile.py` | Pool reconciliation | Provado. |
| `core/probe.py` | Key probing | Provado. |
| `core/keys.py`, `core/multikey.py` | Key utilities | Provado. |
| `core/report.py` | /kame-quota report | Provado. |
| `pool_binding.py` | Per-model binding | Provado. |
| `resolver_binding.py` | Split multi-key | Provado. |
| `field_binding.py` | Settings validation | Provado. |
| `aux_binding.py` | Auxiliary lane scoping | Provado. |
| `commands.py` | /kame-keys + /kame-quota | Provado. |
| `store.py` | Plugin state | Provado. |
| `runtime.py` | Runtime tracking | Provado. |
| `status.py` | /kame-quota command | Provado. |

## O que REMOVER

### 1. `_StreamWatchdog` (dispatch_binding.py:354-431)

**Remover:** Classe inteira + uso no loop `run()`.

**Por quê:**
- Violam ADR 0002 ("Trust the Connection")
- Thread daemon chama `client.close()` de fora da thread principal
- Pode corromper estado do client HTTP
- Você mesmo removeu isto do Agent Zero (v0.4.7-v0.5.5) por death loops
- O timeout do Hermes (1800s per-request) já bound o caso real

**Impacto nos testes:** Verificar se algum teste referencia `_StreamWatchdog`.

### 2. `_emit_wait_notice` em rotações (dispatch_binding.py:749-758)

**Remover:** O bloco que chama `_notify_ui(f"🔄 KAME: Rotacionando...")` em cada
tentativa de rotação.

**Por quê:**
- Mexe no `thinking_callback` a cada rotação
- Embora `thinking.delta` não afete ordinais diretamente, chamadas frequentes
  durante rotações rápidas podem interferir com o estado do spinner
- A visão ao vivo será refeita de forma segura (ver adições)

### 3. `VIGIL_FIRST_S = 5.0` → reverter para `90.0`

**Reverter:** `VIGIL_FIRST_S = 90.0`

**Por quê:** 5s é spam. 90s era calibrado para não ser confundido com hang.

### 4. `_emit_wait_notice` no `_Vigil.maybe_speak` (dispatch_binding.py:498-508)

**Remover:** O bloco que chama `notice_fn(f"⏳ KAME: ...")`.

**Por quê:** O `_Vigil` já tem `_emit_status` (canal lifecycle, seguro) que
continua funcionando. O `_emit_wait_notice` é redundante aqui.

### 5. `CHUNK_STALE_TIMEOUT` setting + config_schema

**Remover:** `CHUNK_STALE_TIMEOUT` de settings.py, _NUMBER_ENV_FOR,
_NUMBER_RANGE, e `chunk_stale_timeout_seconds` do plugin.yaml.

**Por quê:** Era config do StreamWatchdog. Será substituído por
`first_token_timeout_seconds` (default OFF) que é conceitualmente diferente.

### 6. `_Progress.last_activity` + `completed`

**Avaliar:** Se o first-token timeout for implementado de outra forma, estes
campos podem ser simplificados. Se não, manter mas sem uso do watchdog.

## O que ADICIONAR

### A. Visão ao Viva Segura (Spinner Live Status)

**Objetivo:** Mostrar status da rotação no spinner do Hermes em tempo real,
sem quebrar rewind/edit/resend.

**Canal:** `_emit_wait_notice` → `thinking.delta` event (NÃO afeta ordinais).

**Design:**
- **Throttle de 10s** entre updates (não em cada rotação)
- **Formato simples, não revela chaves:**
  - Normal: nada (spinner padrão)
  - Rotacionando: `KAME: rotating (attempt 3) — 12/15 healthy`
  - Todas resting: `KAME: 3/15 resting — ETA 2m 15s`
  - Recovery: `KAME: back up — 15/15 healthy`
- **Só mostra quando algo muda** (estado anterior ≠ estado atual)
- **Zero chamadas em modo normal** (1 chave, 1 tentativa, sucesso)

**Implementação:**
```python
class _Spinner:
    """Throttled live status via thinking.delta (safe for ordinals)."""
    __slots__ = ("_agent", "_last_say", "_last_text")
    
    _THROTTLE_S = 10.0  # min 10s between updates
    
    def __init__(self, agent):
        self._agent = agent
        self._last_say = 0.0
        self._last_text = ""
    
    def update(self, text: str) -> None:
        now = time.monotonic()
        if text == self._last_text:
            return  # só mostra se mudou
        if now - self._last_say < self._THROTTLE_S:
            return  # throttle
        self._last_say = now
        self._last_text = text
        notice = getattr(self._agent, "_emit_wait_notice", None)
        if callable(notice):
            try:
                notice(text)
            except Exception:
                pass
```

**Uso no loop run():**
```python
# Antes de cada tentativa:
spinner = _Spinner(agent)
# ...
# Durante rotações (apenas se attempt > 1):
healthy = self.engine.healthy_count(identity, keys)
spinner.update(f"KAME: rotating (attempt {attempt}) — {healthy}/{len(keys)} healthy")
# ...
# Quando todas resting (Vigil):
spinner.update(f"KAME: {resting}/{total} resting — ETA {when}")
# ...
# Quando recovery:
spinner.update(f"KAME: back up — {total}/{total} healthy")
```

### B. Bind by Signature (paridade A0 v1.0.9)

**Objetivo:** Não quebrar se Hermes renomear a função dispatch.

**Implementação em `install()`:**
```python
for name in _FUNCTIONS:
    function = getattr(module, name, None)
    if not callable(function):
        self.reason = f"{_MODULE} has no {name}()"
        return False
    # Check signature shape — must accept (agent, api_kwargs, **kwargs)
    import inspect
    try:
        sig = inspect.signature(function)
        params = list(sig.parameters.keys())
        if len(params) < 2:
            self.reason = f"{name}() signature changed — stepping aside"
            return False
    except (ValueError, TypeError):
        pass  # can't inspect, proceed optimistically (current behaviour)
```

**Por quê:** A0 v1.0.9 faz exatamente isto — "bind by shape, not by name".
Se um update do Hermes mudar a assinatura, KAME print e step aside.

### C. Jitter (paridade A0)

**Objetivo:** Anti-bot, anti-sync-collision em multi-client deployments.

**Implementação em `_wait_for_recovery()`:**
```python
import random
jitter = random.uniform(0.1, 1.5)
wait = min(eta + 0.5 + jitter, _MAX_SLEEP_S)
```

**Por quê:** A0 tem `random.uniform(0.1, 1.5)` em todos os waits. Hermes não.
Anti-bot detection, multi-client sync-collision prevention.

### D. First-Token Timeout (configurável, default OFF)

**Objetivo:** Detectar conexões que aceitaram mas nunca respondem, sem death
loops em modelos que pensam.

**NÃO é o StreamWatchdog.** Diferenças:
| | StreamWatchdog (1.0.7) | First-Token (1.0.8) |
|---|---|---|
| Thread | daemon separada | NÃO — no loop principal |
| Ação | mata client de fora | deixa timeout do Hermes matar |
| Default | ON (60s) | OFF |
| Trigger | inatividade geral | só se progress.any == False |
| Escopo | streaming + non-streaming | só streaming |

**Design:**
- Setting: `first_token_timeout_seconds` (default: 0 = OFF)
- Só ativa se `first_token_timeout_seconds > 0`
- **NÃO** mata o client — apenas checa `progress.any` após o tempo
- Se `progress.any == False` após timeout → rotaciona
- Se `progress.any == True` → Trust the Connection (ADR 0002)

**Implementação alternativa (sem thread):**
O problema do StreamWatchdog era a thread. Em vez disso, podemos usar o
próprio timeout do Hermes (`HERMES_API_TIMEOUT` = 1800s) que já bound o caso.
Se o usuário quer um timeout mais curto para first-token, ele pode configurar
`request_timeout_seconds` no provider config do Hermes.

**Decisão:** Vamos discutir. Talvez nem precisamos implementar esta feature
se o timeout do Hermes já for suficiente. ADR 0002 diz que qualquer timeout
artificial eventualmente vira o problema.

### E. README + CHANGELOG atualizados para 1.0.8

## Ordem de Implementação

1. Remover `_StreamWatchdog` + referências
2. Remover `_emit_wait_notice` em rotações + no Vigil
3. Reverter `VIGIL_FIRST_S` para 90s
4. Remover `CHUNK_STALE_TIMEOUT` do settings/plugin.yaml
5. Implementar `_Spinner` (visão ao vivo com throttle)
6. Adicionar jitter em `_wait_for_recovery`
7. Adicionar bind by signature em `install()`
8. Atualizar version para 1.0.8
9. Atualizar README + CHANGELOG
10. Rodar testes (meta: 1103/1103 + novos)
11. Verificar compatibilidade com hooks Hermes
12. Deploy para pasta instalada
13. Refino final

## Incompatibilidades Conhecidas (resolvidas por este blueprint)

1. ✅ Rewind/Edit/Resend — causado por StreamWatchdog thread → removido
2. ✅ Client HTTP corrompido — StreamWatchdog.client.close() → removido
3. ⚠️ Bind por nome → bind by signature adicionado
4. ⚠️ Sem jitter → jitter adicionado
5. ✅ Storm de status no spinner → throttle de 10s

## Verificação Final (antes de marcar como pronto)

- [ ] 1103+ testes passando
- [ ] verify_installed.py OK
- [ ] Plugin carrega sem erros no Hermes
- [ ] /kame-quota funcional
- [ ] /kame-keys funcional
- [ ] Rotação visível nos logs
- [ ] Spinner mostra status sem spam
- [ ] Rewind/Edit/Resend funcionam
- [ ] Nenhuma thread daemon
- [ ] Nenhum timeout artificial com default ON
- [ ] README atualizado
- [ ] CHANGELOG atualizado
- [ ] marketplace-ready (plugin.yaml, description, author)
