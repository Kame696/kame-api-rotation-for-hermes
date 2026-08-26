# KAME para Hermes — design

Status: **v1.0.2 implementada, testada, instalada e observada de ponta a ponta.** A 1.0.1 tirou o teto do carrossel e pôs voz na espera, e a 1.0.2 pagou a conta disso no log — ver 4c. **v1.0.0:** Desde a 1.0.0 o plugin escolhe a chave *antes* de cada chamada, no ponto de despacho do próprio Hermes — ver seção 4b; tudo o que segue neste parágrafo continua verdadeiro e descreve a metade que age *depois* da recusa. Um 429 entra pelo socket e sai uma pausa por modelo, atravessando o plugin instalado, o classificador real e a pool real — e o campo de provedor com várias chaves separadas por vírgula vira várias credenciais pelo carregador do próprio Hermes, rotacionando entre elas em recusas reais — e, desde a v0.2.6, espalhando a carga entre as chaves saudáveis antes de qualquer recusa, em vez de martelar a primeira até o provedor mandar parar — espalhamento que, desde a v0.2.8, o `/kame-quota` mostra em números em vez de deixar como promessa, e que desde a v0.2.9 vem acompanhado da conta de quantas falhas o plugin conseguiu ler e quantas ele declinou — e, na v0.3.0, de quanto cada chave levou desde que o Hermes subiu, não só no último minuto — e, na v0.3.1, a resposta que volta vazia deixou de valer como prova de que a chave voltou, que era o jeito de o sintoma de chave espremida soltar o banimento dela em definitivo. Na v0.3.3 a varredura de paridade fechou com uma peça provada impossível de portar e uma décima prova que vigia essa prova. E, na v0.3.2, os dois interruptores passaram a existir onde o Hermes guarda interruptor de plugin, com a variável de ambiente ainda por cima. O que falta é o 429 do provedor em uso normal — a carga naquele socket é captura, não cota acabando de verdade. Baseado em leitura direta do código do Hermes v0.20.1 instalado e do KAME v1.0.9 (`04_plugins/kame/agent-zero/current/`).

Histórico: este documento foi reescrito dezoito vezes. Depois da Fase 0, porque o rascunho original apostava em `plugins/model-providers/` e existia caminho melhor (seção 1). De novo na v0.0.3, porque as v0.0.1/v0.0.2 tinham allowlist de provider — regressão, não escopo (seção 3.1). De novo na v0.0.6, porque a lacuna 4 (pool indexada só por provider) deixou de ser "em aberto" e virou a maior parte do plugin (seções 6.4 e 6.5). Na v0.0.7, porque a precisão que o plugin ganhou desliga a última linha de defesa do próprio Hermes (seção 6.7). Na v0.0.8, porque a dimensão de modelo estava sendo aplicada a provedor que mede por conta (seção 6.8). Na v0.0.9, porque a saída de emergência fazia a pergunta e jogava a resposta fora (seção 6.9). Na v0.1.0, porque o journal já media o único número que o plugin não consegue ler em lugar nenhum, e ninguém usava (seção 6.10). Na v0.1.1, porque essa medição lia só as recusas e supunha que o intervalo entre elas foi passado no banco (seção 6.10.1). Na v0.1.2, porque o alargamento dobrava tudo — e prazo ancorado em relógio não se dobra, se empurra (seção 6.10.2). Na v0.1.3, porque a classificação lia a frase e não a evidência: a mensagem que o Google manda em *todo* estrangulamento de free tier é, palavra por palavra, a frase de crédito acabado da OpenAI — e as três versões anteriores foram verdes contra uma frase que o Google não manda (seção 6.10.3). Na v0.1.4, porque a mesma pergunta feita aos outros provedores achou a mesma classe de erro na chave em vez de na frase: o OpenRouter manda os headers de rate limit **dentro** do corpo, e o corpo e os headers liam o mesmo nome de dois jeitos diferentes (seção 6.10.4). Na v0.1.5, porque descartar a leitura enganosa de janela longa caía direto no palpite da casa, com o número que o provedor disse uma linha abaixo na mesma cascata (seção 6.10.5). E na v0.1.6, porque a mesma pergunta — *o que o mundo real manda mesmo?* — apontada para o caminho de entrada de chave achou o Windows escrevendo marca de ordem de byte que ninguém vê e que custava chave (seção 6.10.6). E na v0.1.7, porque a pergunta virada para o *relato* achou o `/kame-keys status` contando linha de pool como se fosse chave, e lendo o campo guardado enquanto o host roda na propriedade calculada (seção 6.10.7). E na v0.1.8, porque a mesma pergunta virada para a *entrada* achou o defeito que estava debaixo de tudo desde o começo: o Hermes nunca divide lista de chave separada por vírgula, então o conjunto inteiro de chaves do usuário sempre foi uma credencial só, malformada, com nada para onde rotacionar (seção 6.10.8). E na v0.1.9, porque a mesma pergunta virada para a *própria classificação* foi feita ao corpus de erro do Hermes, escrito por quem nunca viu este plugin, e ele achou duas palavras largas demais: um 401 pelado lido como chave morta e um 404 de modelo lido como problema de credencial (seção 6.10.9).

---

## 1. Ponto de integração — o que a Fase 0 achou

O rascunho original recomendava sobrescrever o provider profile `gemini` em `$HERMES_HOME/plugins/model-providers/`. Isso é ponto de extensão real e documentado, mas **não serve pro KAME**. Os hooks que um `ProviderProfile` expõe — `resolve_aux_model`, `get_hostname`, `prepare_messages`, `default_vision_model`, `get_max_tokens` — são todos de *preparação de chamada*. Nenhum vê erro, nenhum vê credencial. Era o item de verificação nº1 e a resposta foi negativa.

O caminho certo é um hook de plugin comum:

```
transform_api_error_classification
```

Registrado em `VALID_HOOKS` (`hermes_cli/plugins.py:211`), despachado por `get_plugin_error_classification()` (`plugins.py:6058`), consultado por `classify_api_error()` **no passo 0, antes do pipeline embutido** (`agent/error_classifier.py:743`).

Por que é melhor que o override de provider:

| | override de provider | hook de classificação |
|---|---|---|
| Vê o erro | não | sim — `status_code`, `error_message`, `error_body` |
| Vê o modelo | não | sim — recebe `provider` **e** `model` |
| Isolamento de exceção | não — quebra a chamada | sim — host captura e ignora callback quebrado |
| Custo em chamada OK | roda sempre | zero — caminho frio, só em falha |
| Superfície de quebra | herda de classe interna | contrato de hook documentado |

**Decisão: KAME-Hermes é plugin com um hook, não override de provider.**

## 2. Como o `reset_at` vira cooldown

A cadeia, verificada em `agent/credential_pool.py`:

```
hook devolve {"error_context": {"reset_at": <epoch>}}
  _normalize_error_context()
  last_error_reset_at
  _exhausted_until()      # sobrepõe o TTL padrão
```

`_exhausted_until()` usa esse valor no lugar de `EXHAUSTED_TTL_429_SECONDS` (1h). É exatamente a alavanca que o KAME precisa: nada de mexer em pool, em seleção de chave, ou em transporte — só dizer ao host *por quanto tempo* a chave fica no banco.

## 3. Correção sobre o pool nativo do Hermes

O rascunho original dizia que o pool "ignora o `retryDelay` real". **Errado para o código atual** — aquilo veio de issues abertas no GitHub (#16830, #26388, #75641), não da leitura do fonte.

O que `credential_pool.py` já faz bem:

- `_extract_retry_delay_seconds()` (linha 375) entende `quotaResetDelay`, `"retry after Ns"`, `"Resets in 4hr 5min"`
- `failure_reason` separa billing de transiente, via `agent/error_classifier.py`
- `STATUS_DEAD` para credencial revogada em definitivo
- `EXHAUSTED_TTL_SOLE_CREDENTIAL_SECONDS = 60` — cooldown curto quando sobrou só uma chave
- 4 estratégias de seleção: `fill_first` (padrão), `round_robin`, `random`, `least_used`

As lacunas reais:

1. **Só varre texto.** `_extract_retry_delay_seconds(message)` recebe uma string. Não vê header HTTP, não vê atributo de exceção do SDK, não vê corpo estruturado. Google manda em `details[].retryDelay` dentro de `google.rpc.RetryInfo`; OpenAI e Anthropic mandam em header; litellm anexa em `exc.retry_after`. Nada disso chega lá.
2. **Não entende duração composta.** `6m11.52s` (Groq) e `2h 30m` não casam com nenhum dos três regexes.
3. **Não distingue janela por-minuto de janela diária.** As duas caem no mesmo 1h, e erram em direções opostas: throttle de 20s fica banido por 1h; cota diária volta em 1h ainda gasta, falha, é banida mais 1h, o dia inteiro.
4. **Pool indexada só por provider** — `load_pool(provider)`. Vários providers cobram cota por chave **por modelo**.

v0.0.3 fecha (1), (2) e (3). **A v0.0.4 fecha (4)** — e teve de fechar, porque precisão sem escopo é regressão: ver seção 6.4.

### 3.1 A regressão da allowlist

v0.0.1 e v0.0.2 só agiam se `provider in {gemini, google, google-gemini, google-ai-studio}`. Isso foi **regressão minha, não decisão de escopo** — o dono do projeto contestou, a leitura do `kame_engine.py` deu razão a ele, e o comentário na linha 615 do engine é literal: *"STRICT daily / account-limit markers (multi-provider)"*, com marcadores de Groq (`perday`), OpenAI (`rpd`, `insufficient_quota`) e Google lado a lado.

Allowlist é promessa de não fazer nada por quem não está na lista — inclusive por todo provider que ainda não existe. A v0.0.3 removeu a lista inteira e trocou o critério de **identidade** por **evidência**:

> Fala quando a resposta carrega tempo que o host não lê. Recusa quando não carrega.

A cascata, da evidência mais forte para a mais fraca:

| # | Fonte | Exemplos reais |
|---|---|---|
| 1 | atributo de exceção | `exc.retry_after` (litellm/OpenAI/Anthropic), `exc.retry_delay` Duration (Google) |
| 2 | header HTTP | `Retry-After` (segundos **ou** HTTP-date), `x-ratelimit-reset-requests: 6m0s`, `anthropic-ratelimit-tokens-reset: <RFC3339>`, `x-ratelimit-reset: <epoch>` |
| 3 | corpo estruturado | qualquer chave que nomeie retry/reset, achada percorrendo o corpo com profundidade limitada |
| 4 | texto livre | duração depois de palavra-chave de retry, incluindo composta |

Header é casado por **forma**, não por nome — `(ratelimit|quota|usage).*reset`. Nome de header varia por provider; a forma não. Havendo vários, vence o **maior**: soltar chave cedo re-martela um limite ainda gasto, e é essa a falha que cascateia.

Única regra ainda específica de provider: meia-noite US/Pacific para janela diária, que é fato do Google. Fica atrás de `looks_like_google()`, que olha o nome **ou** a impressão digital no corpo (`generativelanguage.googleapis.com`, `google.rpc`) — assim um proxy que repassa erro do Google ainda acerta, e todo o resto cai no re-probe horário conservador.

### 3.2 Reconciliação: quando ignorar o que o provider disse

A parte que mais importa e a menos óbvia. Janela e atraso são lidos **independentes**, e depois reconciliados.

O caso que força isso: numa cota **diária** esgotada, o Google ainda manda `retryDelay: "37s"`. Obedecer devolve a chave gasta à rotação a cada 37 segundos, o dia inteiro. Então janela longa (dia/semana/mês/conta) **ignora** dica curta e usa o reset da própria janela. Dica *longa* em janela longa é o provider sendo específico — essa é respeitada.

### 3.3 Onde o KAME cala a boca — e por quê

Rodar no passo 0 significa que **quando este hook responde, todo o pipeline embutido é pulado** — inclusive verificações que ficam acima do roteamento por status. Três lugares onde a resposta do Hermes já é melhor, achados relendo o `error_classifier.py` depois da v0.0.3 estar "pronta":

1. **Congestionamento do provider.** 5xx, ou 429 cujo corpo diz "Overloaded". O Hermes roteia isso **sem rotação de credencial**, de propósito — rotacionar "esgota o pool enquanto o endpoint ainda está ocupado, e não faz nada por quem tem só uma chave" (#14038). E como `reset_at` só é aplicado por `mark_exhausted_and_rotate()`, qualquer cooldown aqui exigiria exatamente a rotação que não pode acontecer. Primeiro rascunho meu tinha teto de 90s (`_KAME_SERVER_BACKOFF_CAP_S` do A0); a leitura mostrou que o certo é recusar.

2. **Agregador repassando falha alheia.** Envelope `"Provider returned error"` com o erro real em `metadata.raw`. O Hermes classifica como `upstream_rate_limit` — a chave do usuário está sadia, cai pra outro modelo em vez de queimá-la. Essa é a aresta mais afiada do plugin: o texto aninhado está cheio de afirmação sobre credencial que **não é nossa**, e um "API key not valid" de upstream marcaria a chave sadia do OpenRouter como morta em definitivo. Recusa por forma (`_WRAPPER_MESSAGE` ou `metadata.raw`/`provider_name`), não por nome de agregador.

3. **403 sem texto reconhecível.** Status sozinho não é evidência — e isso vale contra a própria versão anterior deste módulo, que decidia por `status in {403}`. O Hermes checa bloqueio de política de conteúdo **antes** do roteamento por status, então reivindicar todo 403 sequestraria recusa de segurança por-prompt e bancaria chave sadia por uma hora.

Espelho disso, e a razão de `should_fallback` ir explícito em todo veredito: o Hermes monta o resultado com `ClassifiedError(**plugin_result)`, onde o campo tem default `False`. Ele liga a flag em **todo** `rate_limit`, `billing` e `auth` que produz — então dica omitida é dica **desligada**, não herdada. Omiti-la teria desligado o fallback de modelo justamente nos erros que este plugin existe pra tratar.

## 4. O que transferiu do KAME do Agent Zero

Do engine atual (`kame_engine.py`, 2314 linhas), o que entrou — tabela revista na v0.3.1, quando a auditoria achou três linhas dizendo "não" sobre peças que já tinham entrado:

| Capacidade | Origem no engine | Estado |
|---|---|---|
| Cascata de 4 fontes pro retry-delay | `_extract_retry_delay` (linha 861) | **portado + ampliado** — o engine tem 4 fontes (`exc.retry_after`, Duration, header `Retry-After`, texto); a v0.0.3 acrescenta corpo estruturado, HTTP-date, epoch ms e header de reset por forma |
| Duração composta | `_parse_duration_to_seconds` (668) | **portado** (`6m 11.52s`, `2h 30m`) |
| Marcadores multi-provider de cota | `_DAILY_LIMIT_INDICATORS` (615), `_RATE_LIMIT_INDICATORS` (604) | **portado + ampliado** — 6 janelas (minuto/hora/dia/semana/mês/conta), casadas da mais larga pra mais estreita |
| Classificação de erro | `_classify_error` (972), `_is_permanent_denial` (958) | **portado** (`classify`) |
| Teto de backoff de servidor | `_KAME_SERVER_BACKOFF_CAP_S = 90` | **portado** |
| Cooldown diário conservador | `_KAME_DAILY_COOLDOWN_S = 3600` | **portado** como fallback de quem não é Google |
| Escalonamento adaptativo por chave | `_mark_key_health` (731), `consecutive_rl`/`consecutive_server` | **portado na v0.1.0** (`core/escalate.py`) — dois refusos seguidos na mesma chave/modelo/janela alargam o próximo bench (2×, 4×, teto 8× e nunca além de um dia); prazo ancorado em relógio anda meia hora em vez de multiplicar |
| Health por `provider:model` | `_get_identity_state` (1914) | **portado na v0.0.4** como ledger `(credencial, modelo)` — ver 6.4 |
| Anti-dogpile | `_KameSleepState`, `_thaw_server_cooled_keys` | **portado inteiro na 1.0.0** — a v0.2.6 trouxe a contagem no instante em que a chave sai (`core/dispersion.py`); o que faltava era dormir, e faltava porque o A0 era dono do laço de chamada e o Hermes não. A 1.0.0 passa a ser dona do laço no ponto de despacho do próprio Hermes, então `_wait_for_recovery` e `thaw_server_cooled` entraram como estão no engine |
| Escolha da melhor chave com RPM | `_get_best_key` (1231) | **portado na v0.2.6, e ligado de verdade na 1.0.0** (`core/carousel.py: Carousel.select`) — mesma janela de 60 s e mesmo desempate (menos usada, depois menos recente). Na v0.2.6 isso só *ordenava* o que o host já ia escolher; da 1.0.0 em diante é quem escolhe, uma vez por chamada, com o carimbo dentro do lock |
| Detecção de resposta vazia | `_kame_result_is_empty` (1533) | **portado na v0.3.1 até onde o host deixa** (`core/answer.py`) — o A0 trata resposta vazia como falha e tenta a próxima chave porque é dono do laço; aqui `post_api_request` recebe métricas e nenhuma forma de mudar o resultado (`run_agent.py:2687`, "no raw `response` object"; `moa_loop.py:1633`, "Read-only: a MoA turn's post_api_request hook must not disturb the accounting"). O que dá pra fazer é não *acreditar*: resposta vazia deixa de valer como prova de que a chave voltou, é contada e mostrada, e o bench continua de pé |
| Rotação por resposta vazia | `_KAME_EMPTY_RETRY_BUDGET` (228), o bloco em 1742 | **portado na 1.0.0** (`dispatch_binding`, orçamento 2, primeira vazia não pune a chave, segunda da mesma chave descansa 3 s) — a premissa que impedia continua verdadeira e deixou de importar: o host de facto retenta resposta vazia sem reler a pool, mas a 1.0.0 devolve a resposta *antes* de o host a ver, então a rotação acontece um nível abaixo. O registo original, que continua correto sobre o host, era: **não portado, e com tripwire (v0.3.3)** — no A0 a resposta vazia rotaciona a chave, com orçamento 2, porque o A0 é dono do carrossel. No Hermes não há por onde: o host retenta resposta vazia até 3 vezes com `continue` (`conversation_loop.py:7361`) e esse caminho **não relê a pool**; a única `pool.select()` de fora fica em `restore_primary_runtime` (`agent_runtime_helpers.py:1661`), por turno, e a chave em uso só troca em `_swap_credential` (`run_agent.py:6080`), alcançado pelo caminho de *erro classificado*. Uma despriorização decidida no hook chegaria tarde demais para toda retentativa. Ver 6.10.23 e `tools/host_assumptions.py` |

Não transferiu, por design: toda a camada de acoplamento com Agent Zero — `_kame_find_entry_points`, `_kame_bind_entry_points`, `apply_kame_patch`, `_patch_rate_limiters`, `extensions/`. Era monkey-patch; aqui o host oferece hook de verdade.

Lição confirmada na prática: **a lógica de decisão é o ativo, o acoplamento é descartável.** Por isso `core/` não importa nada de Hermes nem de Agent Zero.

## 4b. O carrossel — o que a 1.0.0 mudou de premissa

Até a 0.3.7 este plugin agia sempre *depois* de uma recusa. Isso estava certo e era metade da máquina. A outra metade — **escolher a chave antes de cada pedido** — foi adiada seis vezes com a mesma justificação: *o A0 é dono do laço de chamada, aqui o host é*.

A justificação estava errada por omissão. O Hermes tem um ponto de despacho único:

```python
# run_agent.py:6681 e 6191 — os dois forwarders
def _interruptible_streaming_api_call(self, api_kwargs, *, on_first_delta=None):
    from agent.chat_completion_helpers import interruptible_streaming_api_call
    return interruptible_streaming_api_call(self, api_kwargs, on_first_delta=on_first_delta)
```

O `import` está **dentro do corpo**, exatamente como em `resolve_runtime_provider`. Substituir o atributo do módulo alcança todas as chamadas do processo — laço principal, lane auxiliar, compressão, subagentes, gateway e CLI — sem tocar em classe, subclasse ou instância. É por aí que a 1.0.0 entra.

O que isso resolve, medido no log do utilizador de 17/08:

| Facto do host | Onde | Consequência antes da 1.0.0 |
|---|---|---|
| `max_retries = agent._api_max_retries`, default 3 | `conversation_loop.py:2499`, `agent_init.py:1926` | três tentativas e o turno morre |
| a pool só roda em `billing`, `rate_limit`, `auth` | `agent_runtime_helpers.py:1042-1146` | um 503 não roda chave nenhuma |
| as três tentativas usam a mesma chave | `_swap_credential` só é chamado pelo caminho de erro classificado | catorze chaves intactas na pool durante a falha |

Os três continuam verdadeiros. O carrossel corre *dentro* de uma tentativa do host: do ponto de vista do `conversation_loop`, a chamada teve êxito ou falhou uma vez, e as quinze chaves tentadas pelo meio são assunto do `dispatch_binding`.

Um limite, deliberado e testado:

* **Stream parcial nunca se repete.** Se o provider já entregou texto, o utilizador já o viu; repetir imprimia a resposta duas vezes. Essa falha volta ao host, que tem máquina para isso (`[System: The previous response was cut off…]`). O `_Progress` observa os callbacks de entrega em vez de assumir — mesmo motivo do `progress["any"]` da v1.0.9 do A0.

Havia um segundo, e a 1.0.1 tirou-o — ver 4c.

## 4c. A espera — o que a 1.0.1 mudou de premissa

A 1.0.0 dava 600 s a uma chamada (`carousel_deadline_seconds`, 10–3600), pelo raciocínio de que um turno que nunca volta é indistinguível de um bloqueio. O raciocínio estava certo; o número estava errado para o caso que o plugin existe para resolver. Uma free tier da Google que esgota a cota diária às 14:00 só vira à meia-noite do Pacífico: o teto disparava com **todas** as chaves ainda de castigo e deitava fora um turno que estava a horas de paciência de funcionar.

O A0 chegou à mesma conclusão duas versões antes (ADR 0002) e tirou todos os prazos artificiais, **incluindo o configurável**: qualquer valor "seguro" que o utilizador escolha é um valor que ele apanha num prompt difícil. Por isso a 1.0.1 tirou o teto *e* a chave de configuração. Não entrou nada no lugar, porque o Hermes já limita o que precisa de ser limitado, no nível certo.

Três factos do host — nenhum deles verdade no A0, e é por isso que a ADR 0002 teve de aceitar o que aceitou:

| Facto do host | Onde | O que garante |
|---|---|---|
| cada tentativa leva o seu próprio prazo | `run_agent.py:1376-1394 _resolved_api_call_timeout()`, 1800 s por omissão, passado em `chat_completion_helpers.py:1798,1898,1930` | um socket travado vira **erro** que o carrossel roda, e não uma espera que ninguém consegue acabar — exatamente o risco que a ADR 0002 deu como insolúvel |
| o agente não corre no event loop | `hermes_cli/web_server.py:2381` (`asyncio.to_thread`), `:2777`, `:3214` (`run_in_executor`) | dormir uma hora não bloqueia o websocket, nem o heartbeat, nem o botão de parar |
| não há watchdog ao nível do turno | `agent/conversation_loop.py` | nada por cima do carrossel decide que ele demorou demais |

Os três são afirmados contra o Hermes instalado a cada corrida de `tools/host_assumptions.py`. No dia em que um deles deixar de ser verdade, falha pelo nome — em vez de deixar um utilizador pendurado.

**O que tirar o teto revelou.** Um bug que o teto escondia: quando o seletor diz "todas as chaves de castigo" e o relógio de recuperação diz "uma já está pronta" — leituras a microssegundos de distância, ou outra thread libertou uma pelo meio — o laço voltava a selecionar **sem dormir**. Com teto, desperdiçava 600 s; sem teto, prende um core até o processo morrer. Agora cede uma fatia de 1 s antes de reler.

**E a voz.** A própria ADR 0002 admite o que não resolveu: *"the user typically restarts A0 in that scenario"*. Reiniciar não é decisão que o utilizador tomou — é decisão que o silêncio tomou por ele. Aos 90 s de espera, e de 10 em 10 minutos depois disso, o `_Vigil` diz quantas chaves estão de castigo, quando se espera a próxima, há quanto tempo está a esperar e que o botão de parar cancela; uma linha quando volta, e nada de nada se a espera foi curta. Contagens e o par provider/modelo apenas — nunca uma chave, para a linha ser segura num screenshot. Canal: `run_agent.py:964 _emit_status`, que serve CLI e gateway e engole as próprias exceções; o KAME protege à volta na mesma, porque um aviso nunca pode ser o que mata um turno que ia acertar.

## 4d. A tempestade — o que a 1.0.2 arrumou

Tirar um limite não apaga um custo; muda-o de sítio. Cada rotação escreve uma linha, e isso está certo — é o que torna uma pool que funciona visível em vez de promessa. Deixa de estar certo durante uma avaria, quando a mesma frase se repete sem nada de novo dentro. E a 1.0.1 piorou isso: antes uma chamada desistia aos 600 s, agora roda enquanto o provedor recusar.

O A0 já tinha medido o mesmo do lado dele — uma avaria sustentada da Gemini deu **1.063 linhas quase idênticas em 83 minutos** — e por isso traz `kame_collapse_storm_logs`. O Hermes tinha a mesma forma e não tinha a correção.

`core/storm.py` decide o que se escreve, e nada sobre chave nenhuma:

| Situação | O que sai |
|---|---|
| as primeiras 3 falhas de um tipo | linha completa — uma diz *o quê*, três deixam ver a pool a andar de chave em chave |
| repetições a seguir | contadas, não escritas |
| a cada 20 s de tempestade | uma linha agregada: quantas desde a última, quantas chaves envolvidas |
| quando uma chave responde | um resumo: total e duração |
| muda o tipo de falha | linha completa, e fecha a tempestade anterior — 503 que vira 429 é o provedor a mudar de resposta |
| falha de autenticação | **nunca colapsada** — permanente, acionável, e rotação nenhuma repara |
| `storm_collapse_disabled` | tudo volta a sair |

A mesma versão baixa para `debug` a linha que disparava a cada passe da espera — uma por minuto enquanto a cota durar, no único sítio onde nada está a acontecer. O `_Vigil` acima já narra isso num ritmo feito para uma pessoa ler.

E a auditoria que encontrou isto encontrou mais uma coisa que vale dizer: o *heal* de argumentos de ferramenta do A0 **não** vem para cá. O Hermes repara argumentos truncados e malformados sozinho (`chat_completion_helpers.py`, `agent_runtime_helpers.py:3417`), e dois reparadores a discordar sobre a mesma carga malformada é pior do que o problema. É agora o décimo terceiro facto do host em `tools/host_assumptions.py`.

O que **não** foi portado do A0, e continua a não fazer sentido portar: `_kame_find_entry_points`, `_kame_bind_entry_points`, `apply_kame_patch`, `_patch_rate_limiters`, `extensions/`. Era acoplamento por monkey-patch a nomes do Agent Zero; aqui há dois nomes estáveis e um sistema de plugins de verdade.

## 5. Layout entregue

```
kame-hermes/
├── README.md
├── DESIGN.md
├── hermes-kame-api-rotation/   # plugin instalável, autocontido
│   ├── plugin.yaml             # hooks: + provides_hooks:
│   ├── __init__.py             # adaptador Hermes — 3 hooks, liga o resto
│   ├── runtime.py              # o que está em voo: modelo, veredito, chave escolhida
│   ├── store.py                # ledger e journal dentro do ctx.state, com cache
│   ├── pool_binding.py         # leitura e escrita por-modelo na pool
│   ├── aux_binding.py          # a via auxiliar, que não dispara hook nenhum
│   ├── settings.py             # os dois interruptores: ambiente, depois config.yaml
│   ├── commands.py             # /kame-keys — único módulo que escreve chave
│   ├── status.py               # /kame-quota — só lê
│   └── core/                   # motor puro
│       ├── quota.py            # cascata de evidência, janelas, reset
│       ├── classify.py         # veredito, ou recusa
│       ├── ledger.py           # (credencial, modelo) → até quando
│       ├── reconcile.py        # ledger + estado da pool → soltar ou segurar
│       ├── journal.py          # o que foi previsto vs. o que aconteceu
│       ├── probe.py            # quando testar uma previsao em vez de obedecer
│       ├── escalate.py         # prazo que provou ser curto duas vezes, alargado
│       ├── dispersion.py       # de quem está saudável, qual vai agora
│       ├── multikey.py         # uma credencial que carrega várias chaves
│       ├── tally.py            # quanto chegou ao classificador, quanto ele leu
│       ├── answer.py           # a resposta vazia, que não prova nada
│       ├── report.py           # renderização, e nada mais
│       └── keys.py             # parse de lote, dedupe, redação
├── tools/                      # as dez provas — ver §9
│   ├── sandbox_binding.py      # os bindings contra o Hermes REAL, em sandbox
│   ├── host_corpus.py          # o corpus de erros do próprio Hermes, com e sem KAME
│   ├── host_pool_suite.py      # as suítes da pool do próprio Hermes, com e sem KAME
│   ├── host_assumptions.py     # os fatos do host em que as NÃO-decisões se apoiam
│   ├── live_429.py             # 429 de socket real, ponta a ponta
│   ├── live_multikey.py        # várias chaves num campo, pelo loader do host
│   ├── deploy.py               # copia pro install e confere o manifesto
│   └── verify_installed.py     # o plugin INSTALADO, dentro de processo Hermes real
└── tests/                      # 922 testes, sem rede, sem chave real
```

Descobertas de empacotamento e de ambiente que custaram tempo, registradas para não repetir:

- O loader importa o plugin como **pacote isolado**. Import relativo (`from .core import ...`) funciona; import absoluto de irmão não. Por isso `core/` mora dentro do diretório do plugin.
- O manifesto precisa de `hooks:` **e** `provides_hooks:`. O loader lê o primeiro; `hermes plugins doctor` valida os registros contra o segundo. Só um dos dois gera WARN.
- Manifesto e registro têm de casar **exatamente**, não por inclusão. Registrar um hook só quando o binding instala faz o manifesto virar superset num ambiente sem Hermes, e o teste que compara os dois quebra. Solução: registra sempre, e o handler sai na primeira linha quando não há binding.
- O registro de comandos fica em `manager._plugin_commands`, não em `manager._commands`.
- O `python` do PATH nesta máquina é build da Store, com `AppData\Local` virtualizado por MSIX: `os.listdir` devolve `[]` onde o Hermes está instalado. Qualquer script que toque no install real precisa rodar com `…\hermes-agent\venv\Scripts\python.exe`. A suíte de testes continua no python do PATH (o venv não tem pytest).

## 6. Fases

**Fase 0 — verificar — CONCLUÍDA**
- [x] Onde a chave é lida e onde o 429 é capturado — achado melhor: hook de classificação, não transporte
- [x] O 429 chega ao plugin? — sim, com `provider`, `model`, `status_code`, `error_message`, `error_body`
- [x] Pool nativo briga com o KAME? — não. KAME informa o TTL, o pool continua dono da seleção
- [x] Slots auxiliares compartilham pool? — sim, uma pool por provider. É a lacuna 3

**Fase 1 — cooldown correto no Gemini — CONCLUÍDA (v0.0.1)**
Um hook, um provider, 50 testes verdes, `plugins doctor` limpo. Não instalada.

**Fase 2 — entrada de chave em massa — CONCLUÍDA (v0.0.2)**
Comando `/kame-keys` via `ctx.register_command()`. Resolve em sessão CLI **e** de gateway — ou seja, funciona também pelo app Android, que é onde o dono do projeto mais quer.

Descoberta que reduziu o escopo: **o Hermes já tem UI de chave.** Dashboard, página System, seção "Credential pool" (`web/src/pages/SystemPage.tsx:1145`), batendo em `POST /api/credentials/pool` (`web_server.py:13034`). Adiciona e remove pela tela, com preview redigido e status. O que falta lá é só o lote — uma chave por submit. Então a v0.0.2 **não** mexe no dashboard: patch em React quebraria no próximo update do Hermes e duplicaria o que já existe.

Um bug pego em teste, registrado porque a categoria volta: separar `add <provider> <chaves>` de `add <chaves>` por *formato* é impossível. `alibaba-coding-plan` tem 19 caracteres de letras e hífens e passa em `looks_like_api_key` — regra por tamanho arquiva o nome do provider como credencial; regra frouxa o bastante para pegá-lo engole a primeira chave de um paste separado por espaço. A resposta é pertencimento, não formato: consulta o registry real (`providers.list_providers()`, 38 ids) e mais os providers já com pool.

**Fase 2.5 — provider-agnóstico — CONCLUÍDA (v0.0.3)**
Allowlist removida. Cascata de evidência com 4 fontes, 6 janelas de cota, duração composta, HTTP-date, epoch em segundo e milissegundo. 226 testes verdes, `plugins doctor` limpo em 0.0.3. Não instalada.

Seis bugs reais pegos nesta fase, registrados porque as categorias voltam. Os três primeiros por teste, os três últimos por reler o `error_classifier.py` **depois** de o plugin já estar verde — que é a lição em si: teste unitário prova que o plugin faz o que eu quis, não que o que eu quis é o certo perto do host.

1. `_ACCOUNT_MARKERS` continha `"exceeded your current quota"`. O Google manda essa frase em **todo** 429 de free tier, inclusive por-minuto — throttle de 20s viraria banimento de 24h. A variante de conta da OpenAI continua com *"…please check your plan and billing details"*, e é esse par que os padrões de billing pegam.
2. O regex de texto varria uma classe de caracteres até a próxima pontuação. Cortava `6m11.52s` em `6m11` (o ponto decimal terminava a captura) e falhava inteiro em `4hr 5min` (o `i` de `min` não estava na classe). Trocado por pares número+unidade explícitos.
3. `overloaded` estava junto dos padrões de rate limit, então o 529 da Anthropic virava "credencial gasta" em vez de "provider ocupado". Separado em `_QUOTA_PATTERNS` e `_BUSY_PATTERNS`.
4. Envelope de agregador (`"Provider returned error"` + `metadata.raw`) seria classificado pelo texto de **upstream**. Um "API key not valid" de lá dentro mataria a chave sadia do OpenRouter em definitivo. Recusa no passo 0.
5. `should_fallback` omitido em todo veredito. Default `False` no `ClassifiedError` — eu estaria desligando o fallback de modelo em todo `rate_limit`, `billing` e `auth`.
6. 403 decidido por status puro, sem exigir texto. Sequestraria bloqueio de política de conteúdo, que o Hermes checa antes do roteamento por status.

### 6.4 Fase 3 — cota por modelo — CONCLUÍDA (v0.0.4)

A lacuna 4 saiu de "em aberto" porque **precisão sem escopo é regressão**. Antes do KAME, cota diária levava o padrão de 1 hora, então o modelo auxiliar perdia a chave por uma hora. Com o KAME lendo "reseta à meia-noite" corretamente, a mesma chave fica banida pela duração real — e como o banimento é por provider, o modelo que não gastou nada perde a chave o dia inteiro. As duas coisas têm de andar juntas.

O ledger é uma linha por `(credencial, modelo)` com o prazo que aquela chave ganhou ali, guardado no `ctx.state` (armazenamento de plugin do próprio Hermes). Precisa de dois pontos de embrulho, porque a pool não expõe costura nenhuma para modelo:

| Embrulhado | Por quê |
|---|---|
| `CredentialPool._mark_exhausted` | Único lugar onde um banimento é escrito. Arquiva exatamente o prazo que o host guardou, contra o modelo em voo |
| `CredentialPool._available_entries` | Único lugar onde "quais chaves posso usar" é respondido. Devolve chave banida em outro modelo; segura a gasta neste |

Embrulhados na **classe**, não na instância: a conversa segura um `agent._credential_pool` de vida longa enquanto a via auxiliar chama `load_pool()` e recebe objeto novo toda vez.

As quatro regras que tornam isso defensável:

- **Nada é escrito na pool, nunca.** O método original roda primeiro, inteiro; o embrulho só soma ou subtrai da resposta dele. Toda invariante do host — limpeza de cooldown, poda de DEAD, refresh de OAuth, persistência — fica intacta.
- **Banimento que o KAME não escreveu nunca é solto.** A posse é provada pelo prazo: o KAME grava exatamente o número que o host guardou, então banimento cujo prazo não casa com nada no ledger é de outro escritor.
- **Desconhecido significa "faça o que o host faria".** Sem anúncio, anúncio de outro provider, ledger ilegível, exceção em qualquer ponto do embrulho — tudo cai na resposta do próprio host.
- **Recusa instalar se não reconhecer o que está sendo embrulhado.** `inspect_module` confere todo nome de método, nome de parâmetro e campo de entrada a cada partida. Release do Hermes que mexa em qualquer um degrada o plugin para só o dimensionamento de cooldown e avisa uma vez no log. O modo de falha é *menos* plugin, nunca estado de credencial corrompido.

A armadilha que quase custou o recurso inteiro: `_available_entries(clear_expired=True)` faz o host **reescrever a entrada** (`last_status=STATUS_OK`, `last_error_reset_at=None`) e persistir. Ou seja, responder "essa chave está livre" apaga a impressão digital que prova que o banimento era do KAME. Por isso o embrulho é em `_available_entries` e não em `_exhausted_until`.

### 6.5 Fase 3.5 — a via auxiliar — CONCLUÍDA (v0.0.5)

`auxiliary_client` **não dispara hook nenhum**. Toda sumarização e toda titulação sairiam como chamada não anunciada e herdariam o banimento do modelo principal — a via que não gastou nada perdendo a chave. Três funções são a saída de toda requisição auxiliar para um provider, e as três são embrulhadas: `_relay_sync_completion`, `_relay_sync_stream`, `_relay_async_completion`.

O `sandbox_binding.py` roda o corpo genuíno do relay passando um `create` de mentira: rota, metadados e wrapper de proteção reais, sem rede e sem credencial.

### 6.6 Fase 4 — o caderno — CONCLUÍDA (v0.0.6)

Tudo acima é **previsão**. Previsão curta demais e previsão longa demais são, vistas de fora, a mesma coisa: uma chamada que falhou e depois funcionou. Nada no Hermes nem no plugin sabia distinguir.

`core/journal.py` anota duas coisas e se recusa a anotar qualquer outra:

- **bloco** — toda recusa real: quando, provider, modelo, chave, janela de cota, quem dimensionou o cooldown (KAME ou o padrão do host) e o prazo que ficou valendo;
- **recuperação** — a resposta pareada: o instante em que aquela mesma chave voltou a responder naquele mesmo modelo.

Daí saem os dois únicos erros que importam: **previu curto** (bloco novo no mesmo par cai dentro de 3 minutos do prazo que o anterior tinha marcado) e **previu longo** (sucesso chega *antes* do prazo). Dois casos, não um — coincidência isolada não é evidência.

Quatro propriedades, nesta ordem:

1. **Não decide nada nesta versão.** Seleção, banimento e liberação se comportam exatamente como na v0.0.5. O journal é lido pelo `/kame-quota` e por mais nada. Existe para que, quando passar a guiar comportamento, a mudança seja feita contra fato registrado e não contra palpite sobre palpite.
2. **Sucesso não escreve nada.** `record_success` devolve "não persista" se não houver bloco aberto para aquele par exato. O caso esmagador — chamada bem-sucedida comum — encosta na memória e para. Fixado por teste com 50 sucessos e zero escritas.
3. **Nunca guarda chave.** Credencial é nomeada pelo id opaco da própria pool. Nenhum token, nenhum rótulo, nenhum fragmento — é por isso que o relatório pode ser impresso num transcript de chat.
4. **Esquece.** 14 dias, 300 blocos, 300 recuperações, podados a cada carga. Tirar a chave da pool tira o histórico junto.

A identidade da credencial no lado do sucesso veio de um terceiro embrulho, **opcional e só para estatística**: `CredentialPool._select_unlocked`, único lugar onde uma credencial é de fato escolhida. Fica fora do `inspect_module` de propósito — se um release do Hermes renomear, o plugin perde uma estatística, não o recurso.

Como o veredito da classificação chega até o `_mark_exhausted`: hook roda **síncrono na thread de quem chamou**, então um `ContextVar` gravado dentro do hook é visível para o chamador. É consumido uma vez só, expira em 30s e exige casar provider e modelo.

`/kame-quota` mora em `status.py`, separado do `commands.py` de propósito: `commands.py` é o único arquivo do plugin capaz de escrever credencial, e relatório não tem o que fazer ali. `status.py` lê dois stores e renderiza texto. Não pode lançar exceção dentro de um turno de chat.

### 6.7 Fase 5 — a saída de emergência — CONCLUÍDA (v0.0.7)

A v0.0.6 registrou o problema; esta versão fecha o pior caso dele antes de qualquer dado de campo, porque não é ajuste de palpite — é propriedade estrutural.

**O que estava errado:** quanto mais preciso o KAME fica, mais completamente ele desliga a última linha de defesa do próprio Hermes. `EXHAUSTED_TTL_SOLE_CREDENTIAL_SECONDS = 60` existe porque, com uma chave só, uma hora de banimento "significa uma hora de falha dura sem nada pra onde cair". O comentário logo acima da constante termina com *"Provider-supplied reset_at still overrides"* — e `reset_at` vindo do provider é exatamente o que este plugin fornece. Ler uma cota diária certo, com pool de uma chave, é o usuário sem agente até meia-noite do Pacífico. **É a situação do dono do projeto hoje: uma chave na pool.**

A assimetria que decide o desenho:

- acreditar num prazo longo **certo** economiza um punhado de chamadas condenadas;
- acreditar num prazo longo **errado** custa todo turno até ele vencer.

E o caso errado é indescobrível por construção: a chave nunca é tentada, logo nenhum sucesso é observado, logo o detector de "previu longo" da v0.0.6 **jamais dispara** justamente onde mais importa. Sondar não é só rede de segurança — é o que torna a medição possível.

**A regra:** quando o modelo em voo não tem nenhuma credencial usável, um banimento é testado em vez de obedecido. Seis portões, em `core/probe.py`:

| Portão | Regra |
|---|---|
| Último recurso | Nunca enquanto qualquer outra chave servir àquele modelo |
| Só o que é nosso | O prazo guardado pelo host tem de casar com a impressão digital do ledger |
| Vale perguntar | Sentença > 10 min e restando > 5 min. Esperar 21s é mais barato que uma chamada que falha |
| Respondível por repetir | Nunca billing, credit, auth, invalid, revoked, permission, suspension. Saldo zerado não é relógio |
| Espaçado | 5m, 10m, 20m, 30m, e 30m dali em diante |
| Coerente no turno | Emitida, a chave fica oferecida por 60s — `_available_entries` é perguntado várias vezes por requisição e as respostas têm de concordar |

A tentativa é gravada **antes** de a chave sair: sonda não contada é sonda que repete na consulta seguinte. A recusa que ela provoca re-registra o banimento **sem zerar o contador** — só um prazo genuinamente vencido começa episódio novo em 5 minutos. Sem isso o backoff nunca alargaria e o recurso viraria um jeito de martelar chave gasta o dia inteiro.

Nada é escrito na pool. A sonda acrescenta uma entrada à lista que o `_available_entries` devolve, passando pelos mesmos portões de usabilidade do host, e só.

**Bug encontrado ao escrever o teste, e que valia por si:** o store descartava o que a sessão tinha aprendido quando a gravação falhava. `save()` guardava em cache, mas o `load()` seguinte relia o disco — copia mais velha — e ressuscitava banimentos já soltos e perdia sondas já gastas. Corrigido com um sinalizador `_unsaved`: enquanto uma escrita estiver pendente, a cópia em mãos é a nova e o disco é a velha, inclusive em `load(force=True)`. **Armazenamento que não persiste tem de degradar para não-durável, nunca para errado.**

### 6.8 Fase 5.5 — o alcance do banimento — v0.0.8 ENTREGUE

A v0.0.4 ensinou que um banimento pertence a um *modelo*. Isso é certo onde a cota é por modelo e **errado** onde ela é por conta.

**O que estava errado:** o `plan()` soltava a chave para qualquer outro modelo, de qualquer provedor, sem nunca perguntar se a cota era mesmo por modelo. Isso é a crença do Gemini free tier aplicada ao mundo inteiro. A cota grátis do OpenRouter é um teto diário **da conta**, sobre todos os modelos grátis: soltar a chave lá compra uma segunda recusa, um turno mais lento e uma linha no journal sobre um limite que nunca foi daquele modelo. É a mesma classe de erro do host banir por provedor — só muda a direção.

**A regra:** o alcance é lido igual ao tempo — do que o provedor disse.

| O que a resposta diz | Alcance | O que o KAME faz |
|---|---|---|
| `...PerProjectPerModel`, ou uma violação cujo `quotaDimensions` nomeia o modelo | por modelo | solta para os outros modelos, como antes |
| `free-models-per-day`, `per user`, `per organization`, `per-api-key`, `insufficient_quota` | a chave inteira | segura em todos os modelos até vencer |
| nada sobre alcance | desconhecido | **tratado como por modelo** — exatamente o que toda versão anterior fazia |

**A assimetria é o argumento de segurança.** Silêncio não muda nada, então alcance não detectado não custa nada que já não estivesse sendo pago. Só uma afirmação explícita de "vale para a conta" muda comportamento, e muda na direção cara — segurar a chave —, tomada só com evidência. Chutar ali recriaria, numa dimensão nova, exatamente a regressão que a dimensão de modelo foi criada para corrigir.

Três consequências que precisaram de código próprio:

1. **A saída de emergência tem de alcançar esse caso.** Senão um provedor que diz "gasta em tudo por um dia" bane a única chave para todos os modelos sem jeito nenhum de checar a afirmação — a v0.0.7 desfeita por uma porta nova. `_blocking_bench` aceita o banimento de conta escrito sob *outro* modelo, com a mesma prova de posse.
2. **A sonda é contada no banimento que bloqueia**, não no modelo em voo. Contar no modelo em voo deixaria o banimento real eternamente sem teste e o backoff nunca alargaria.
3. **Evidência não evapora.** Provedor que nomeou o alcance uma vez e ficou quieto na repetição não mudou de ideia; um "desconhecido" novo nunca sobrescreve o que ele disse. E linha gravada antes da v0.0.8 lê como desconhecida, nunca como conta — atualizar não pode começar a segurar chave por evidência que ninguém registrou.

**Defeito de teste corrigido junto:** três testes afirmavam que a cota diária do Google bane por *mais de uma hora*. É falso na última hora antes da meia-noite do Pacífico — quando o certo é banir por menos. A suíte quebrava sozinha uma hora por dia, sem nada no plugin ter mudado. Agora afirmam o que importa: o prazo é a meia-noite do Pacífico, com um teste de relógio fixo para o caso comum.

### 6.9 Fase 5.6 — acreditar na resposta — v0.0.9 ENTREGUE

A v0.0.7 deu ao plugin um jeito de testar as próprias previsões. Ela fazia a pergunta e **jogava a resposta fora**.

**O que estava errado:** sonda que *falhava* era tratada certo — regravava o banimento e alargava o backoff. Sonda que *dava certo* não mudava nada. O ledger continuava dizendo "gasta", a seleção seguinte segurava a chave de novo, e a próxima sonda era cinco minutos depois. Com uma chave só e cota diária lida longa demais, isso é a diferença entre um soluço de cinco minutos e um dia inteiro de quase-tranca — o usuário recebendo uma chamada a cada intervalo crescente em vez do agente de volta.

**A regra:** todo prazo do ledger foi *deduzido de mensagem de erro*; nenhum foi medido. Chamada bem-sucedida mede. Quando as duas coisas se contradizem, a medição ganha.

| O que foi observado | O que isso desmente | O que sobrevive |
|---|---|---|
| chamada limpa em `(chave, modelo)` | o banimento daquele par, inteiro | nada — a chave volta à rotação até falhar de novo |
| chamada limpa em *outro* modelo da mesma chave | o **alcance** de um banimento de conta | o prazo em si, estreitado ao modelo que de fato bateu no limite |
| qualquer outra coisa | nada | tudo |

Três detalhes que fazem isso ser seguro em vez de otimista:

1. **Só sonda resolve banimento.** O hook de sucesso traz provider e modelo e nenhuma chave. Existe um espelho da última seleção da pool, mas ele é *best-effort* e diz isso na cara — dois agentes no mesmo provider se sobrescrevem —, então alimenta estatística e nunca soltura. Sonda é diferente **em espécie**: a saída de emergência só é alcançada quando o modelo não tem *mais nada usável*, e o que ela devolve é uma entrada única que ela mesma escolheu pelo nome. Não existe segundo candidato de onde o sucesso seguinte pudesse ter vindo.
2. **O banimento é marcado, não apagado.** O prazo guardado nele é a impressão digital que prova que o cooldown do host é do KAME para desfazer. Apagar a linha deixa a chave presa atrás de um banimento que ninguém reivindica — tranca pior que a original. `/kame-quota` mostra `tested and it worked` em vez de contagem regressiva, porque "livre em 8h" sobre chave que está em rotação agora seria a linha mais mentirosa do relatório.
3. **Só sucesso encurta.** Recusa posterior mais curta, não. Chave segurada até meia-noite por cota diária não fica livre em sessenta segundos porque a sonda voltou com queixa de por-minuto: o contador diário continua gasto.

**Buraco de leitura achado no caminho:** `plan()` decidia "está banida?" pelo *status* da entrada. O host considera a chave usável no instante em que o prazo vence, tenha ou não alguém limpado o status. Ler o status velho como "ainda banida" jogava a decisão inteira no ramo de posse e **pulava o hold** — devolvendo chave que o ledger sabe gasta. Agora status velho com prazo vencido lê como livre, que é o que o host acha.

### 6.10 Fase 6 — acreditar na medição — v0.1.0 ENTREGUE

Todo número que o plugin produz ele **lê** do provedor. Menos um.

**O que dá pra medir e não dá pra ler:** a chave volta no prazo, é entregue na chamada seguinte e leva recusa de novo em menos de três minutos. Uma vez, isso é ruído — rajada, vizinho na mesma chave, provedor arredondando pra baixo. **Duas vezes seguidas na mesma chave, no mesmo modelo e na mesma janela**, não sobra outra leitura: o prazo era curto. E nenhum corpo de resposta vai dizer isso, porque o provedor já disse o que achava.

Então o próximo banimento segura mais: 2× na segunda batida, 4× na terceira, teto de 8× e nunca além de um dia.

**Dois prazos no mesmo banimento, e essa é a versão inteira.** A tentação é escrever o número maior no banimento e pronto. Isso quebra o plugin em silêncio:

| Campo | Número de quem | Pra que serve |
|---|---|---|
| `reset_at` | do **host** — exatamente o que o Hermes guardou | a impressão digital que prova que esse cooldown é do KAME pra desfazer |
| `extended_to` | do **KAME** — quanto ele está de fato segurando | o prazo que decide toda soltura |

Sobrescrever `reset_at` faz a impressão digital parar de bater com o cooldown guardado no host. `_fingerprint_matches` devolve `None`, o banimento não é de ninguém, e a chave fica trancada em **todos os outros modelos** pelo tempo que o host segurar — exatamente a regressão que a dimensão de modelo existe pra impedir. Logo: dois campos, e uma regra de uma linha — toda pergunta sobre **segurar** lê `until = max(reset_at, extended_to)`; toda pergunta sobre **posse** lê `reset_at`.

**Buraco antigo fechado junto:** a guarda de nunca-encurtar, desde a v0.0.5, carregava o prazo mais longo por cima do `reset_at` — destruindo a impressão digital. Agora ela escreve em `extended_to`.

O que impede isso de virar esconde-chave:

1. **Só evidência consecutiva conta.** Uma recusa que chega em hora comum zera a contagem. Sem constante de decaimento e sem janela pra calibrar: "consecutivo" já é uma afirmação sobre ser recente.
2. **A acusação é estreita.** Por chave, por modelo, por janela. Chave gratuita provando curto não fala nada sobre a paga; estrangulamento por minuto não fala nada sobre cota diária.
3. **Nunca em depleção.** Janela de conta, saldo acabado, chave revogada — nada disso é prazo mal medido, e alargar só esconderia a chave. Reusa a mesma lista de nunca-sondar.
4. **Limitado duas vezes**, em 8× e em 24h.
5. **Continua sendo previsão.** Banimento alargado é testado pela saída de emergência como qualquer outro, e uma chamada limpa o aposenta pra sempre. A ordem é de propósito: a refutação saiu na v0.0.7 e na v0.0.9, o escalonamento só depois. A ordem inversa é como um plugin que dimensiona cooldown vira um plugin que esconde chave.

**Detalhe que o journal precisou aprender:** `Block.reset_at` passa a guardar o prazo a que a chave foi **de fato segurada** — o número alargado quando existe um. Guardar o número do host ali faria um banimento já alargado, mas ainda curto, zerar a própria evidência e travar o escalonamento no primeiro degrau para sempre. O `sized_by` continua sendo calculado contra o número do host, porque a pergunta ali é outra: quem dimensionou.

#### 6.10.1 Correção da v0.1.1 — banimento que não foi cumprido não mede nada

A v0.1.0 lia a sequência só nos blocos: recusada, prazo, recusada de novo em minutos. Essa leitura supõe, sem dizer, que a chave passou o intervalo inteiro **no banco**. Muitas vezes não passou:

- a sonda voltou limpa e a v0.0.9 **soltou a chave cedo** — o plugin faz isso de propósito;
- a chave foi devolvida no prazo, **funcionou** por um minuto e só depois bateu no limite outra vez (a tolerância são 180s, então cabe uso real dentro dela).

Nos dois casos a segunda recusa é limite novo sendo batido, não prova de prazo curto — e cobrar como batida alargaria banimento por coincidência. Em pool de duas chaves isso é o caso comum, não o exótico.

**A regra nova:** sucesso registrado no intervalo quebra a corrente. Vale para o `short_streak` (que age) e para o `summarize` (que só conta), senão o relatório afirmaria uma sub-previsão que o próprio alargamento se recusa a cobrar.

**Por que é seguro depender do espelho best-effort.** O sucesso vem de `runtime.selected_for`, que diz na própria docstring que pode errar. Aqui isso é aceitável pela assimetria de sempre: sucesso perdido devolve o comportamento da v0.1.0; sucesso espúrio só deixa de segurar a chave. **Prova é exigida pra reter, nunca pra soltar.**

**Uma recuperação por par basta**, mesmo a corrente sendo caminhada pra trás: sucesso mais novo sobrescreve o mais velho, mas sucesso no intervalo **mais novo** quebra a caminhada já no primeiro elo — então nunca se chega a um intervalo cuja evidência foi sobrescrita.

#### 6.10.2 Correção da v0.1.2 — momento não é duração

Dobrar está certo para prazo que é **cronômetro**: "volta em 21s", "re-sonda em 1h". Está errado para prazo que é **âncora**: meia-noite do Pacífico, o instante em que se acredita que o contador diário do Google vira.

E "errado" aqui não é deselegante, é **inerte**. Chave recusada logo depois da meia-noite é banida até a meia-noite **seguinte** — o prazo já está a um dia de distância, e o teto de 24h engole o multiplicador inteiro. `stretch` devolvia `None`. Ou seja: **o prazo que mais precisava de correção era o único que o escalonamento não conseguia tocar** — e a falha dele se repete todo santo dia, porque cinco minutos de erro de relógio custam um dia inteiro daquela chave, e amanhã de novo.

**A regra nova:** âncora que se prova adiantada é **empurrada**, não multiplicada — +30min, depois +1h, depois +2h, e nunca mais. O dia veio do provedor; só o deslocamento é invencionice do KAME, então só o deslocamento tem teto. E o empurrão é medido a partir da própria âncora, não a partir do instante da recusa — senão o prazo inteiro se arrasta conforme a hora em que a retentativa calhou de cair.

**Como os dois se distinguem:** `quota.py` marca a decisão com `source = "anchor"` no ramo da meia-noite, em vez de deixar adivinhar pela janela — cota diária de provedor cujo relógio o KAME **não** conhece é re-sonda de 1h usando o mesmo nome de janela, e essa é duração, e dobra certo. De quebra isso corrige uma mentirinha antiga: com header trazendo `retry-after`, o `source` dizia `headers` num prazo que veio do calendário, porque o delay do header tinha sido descartado três linhas acima.

#### 6.10.3 Correção da v0.1.3 — a frase que dois provedores dividem

A mensagem que o Google manda hoje em **todo** 429 de free tier é, palavra por palavra, a frase de crédito acabado da OpenAI, com um link de documentação de rate limit grudado no fim:

> `You exceeded your current quota, please check your plan and billing details.`

E ele manda isso para um estrangulamento de **21 segundos** por minuto. Lida como `billing`, uma frase produz quatro falhas que se somam:

| Leitura | O que acontece |
|---|---|
| `billing` | banco de 24h no lugar de 21s |
| escopo `account` | derruba todo modelo daquela chave, não só o estrangulado |
| `billing` está em `probe.NEVER_PROBE_REASONS` | a saída de emergência fica **desarmada** |
| `account` está em `escalate.NEVER_STRETCH_WINDOWS` | o escalonamento fica desarmado também |

Com uma chave só, o agente fica um dia fora — e **nada no sistema consegue descobrir isso**, porque a chave nunca é tentada e sucesso nenhum é observado.

**Consequência para as três versões anteriores:** v0.1.0, v0.1.1 e v0.1.2 eram **inertes** contra tráfego real do Google. Inclusive a correção de âncora da v0.1.2, escrita especificamente para a cota diária do Google: carga real nenhuma chegava naquele caminho.

**O discriminador é evidência, não identidade.** Chavear em "é o Google?" repetiria a regressão de allowlist que o dono do projeto corrigiu na v0.0.3. O que separa os dois significados está na carga:

- **espera nomeada** — mandar voltar em 21s não é dizer que o saldo acabou; depleção não tem o que esperar;
- **contador nomeado** — `GenerateRequestsPerMinutePerProjectPerModel` nomeia uma **taxa**; saldo não tem janela.

`account` e `unknown` de propósito não contam: o primeiro **é** a leitura de depleção, e o segundo é ausência de evidência, que nunca pode derrubar uma correspondência. E os marcadores decisivos (`insufficient_quota`, `credit balance is too low`, `out of credits`, `payment required`, `billing … disabled/required/suspended`) continuam decidindo sozinhos — gateway que grude um `Retry-After` de prateleira numa depleção de verdade ainda lê como `billing`.

**Por que sobreviveu quatro versões: as fixtures eram mais gentis que o provedor.** Todo corpo do Google na suíte parava em *"You exceeded your current quota."* — texto real, mas antigo — e o teste de por-minuto não passava `error_message` nenhum. 631 testes verdes contra uma frase que o Google não manda. **Fixture escrita de memória testa a memória.**

**E o delay na forma em que o Google realmente emite.** `google.rpc.RetryInfo.retryDelay` é uma `Duration` do protobuf, ou seja uma *mensagem*. O endpoint REST renderiza `"21s"`; JSON canônico de proto (transcodificação gRPC-JSON, SDKs de GenAI) renderiza `{"seconds": 21, "nanos": …}`. O `extract_from_body` pulava todo valor que fosse dicionário, então essa forma inteira lia como "sem delay". Proto3 também omite campo em valor default, então delay zero serializa como `{"@type": ".../RetryInfo"}` sem chave de delay alguma — coberto pelo último recurso, que reconhece a *forma* de "volte depois" mesmo quando o valor é ilegível.

Exigir `seconds` ou `nanos` é o que mantém a leitura estreita: dicionário sob chave de retry que não traz nenhum dos dois é a **política** de retry de alguém, e tirar número de lá seria inventar prazo.

#### 6.10.4 Correção da v0.1.4 — um nome, duas respostas

A v0.1.3 achou uma frase lida errado. Fazer a mesma pergunta aos outros provedores — *a fixture da suíte é a carga que o provedor manda mesmo?* — achou a mesma classe de erro uma camada abaixo, na **chave** em vez de na frase.

O OpenRouter manda os headers de rate limit **dentro do corpo**, em `error.metadata.headers`. É documentado, é o que o litellm entrega ao host, e significa que o nome idêntico `X-RateLimit-Reset` chega neste módulo pelos headers na maioria dos provedores e pelo corpo neste. **Os dois caminhos usavam padrões diferentes.** O `extract_from_headers` casava por forma — nome que menciona um limite e um reset. O `extract_from_body` exigia sufixo depois de `reset` (`resetAt`, `reset_time`), então o mesmo nome lia como chave comum e o valor era descartado.

Custo: o OpenRouter diz o instante exato em que o contador de free tier vira, e o KAME jogava fora e caía na re-sonda horária conservadora — nove recusas desperdiçadas por chave num teto que vira nove horas depois, todo dia, no único free tier que uma pool rotativa existe para esticar.

**A correção não é a grafia que faltava.** O defeito era duas leituras de um nome se afastando; o corpo passa a **compartilhar** o padrão dos headers em vez de repeti-lo. De quebra passa a ler `quotaResetDelay` num corpo — chave que o próprio scan de texto do host já conhecia e este não.

**E o balde é de conta em toda janela que ele nomeia.** `free-models-per-day` já lia como conta; `free-models-per-min` é o mesmo teto compartilhado com outra janela, e lia como per-model — então a chave voltava para modelos que ela não conseguia servir. O marcador casa o nome do balde agora, não a grafia de uma janela. Evidência per-model continua sendo checada primeiro e continua ganhando.

**Uma coisa que foi revertida antes de entrar.** Junto com isso eu tinha alargado o padrão dos headers de `rate-limit` para `rate.?limit`, cobrindo `rate_limit_reset`. A auditoria de desativação derrubou **zero** testes, e a única carga que a justificaria eu escreveria de memória — que é exatamente o erro da v0.1.3. Revertido. Guarda que nada derruba é inerte ou não testada, e inventar a carga para testá-la é o pior dos dois mundos.

#### 6.10.5 Correção da v0.1.5 — o número que o provedor disse e o que este módulo inventou

Um 429 diário quase sempre traz os dois tipos de número ao mesmo tempo:

```
headers: x-ratelimit-reset-requests: 58s          <- outro contador
message: ...on requests per day (RPD): Limit 200, Used 200.
         Please try again in 6h12m.               <- a espera diária
```

A cascata pega o header, porque campo estruturado quer dizer o que diz e frase precisa ser garimpada da prosa. Essa ordem está certa. Aí a regra de janela longa põe os 58 segundos de lado como do tipo enganoso — também certo, e é o motivo de o caso diário existir.

**O que entrava no lugar era a re-sonda horária.** Esse número não é do provedor: é o palpite conservador desta casa para um relógio que ela não conhece. E as seis horas do provedor estavam uma linha abaixo, na mesma cascata, sem ser lidas.

**A regra nova:** janela longa cuja leitura mais forte é curta consulta as leituras que a cascata passou por cima e pega a **mais longa** entre as que passam do default da janela. O resto fica igual — dica abaixo do default continua descartada venha de onde vier, a âncora de calendário do Google continua sendo decidida antes de tudo isso, e janela menor que um dia continua obedecendo sua leitura mais forte.

Ordem de força é como se **escolhe uma** leitura. Não é motivo para preferir número inventado a número que o provedor falou.

Junto foi embora uma meia-verdade da mesma família da v0.1.3: quando o default flat **é** usado, a decisão dizia que a origem era o header cujo número acabara de ser jogado fora. Agora diz `window`.

**O que foi considerado e não feito.** A mesma evidência sugere regra mais ousada: tratar **momento absoluto** como autoritativo em janela longa mesmo quando curto, já que momento declarado não é duração enganosa. Está errado, e o contraexemplo é comum — recusa diária costuma vir com headers de reset por minuto, e muito provedor renderiza esses headers como epoch em vez de duração. Obedecer um devolveria a chave sessenta segundos depois num teto de um dia inteiro e marteleria até a meia-noite. A regra atual recusa exatamente isso, e uma hora de chave segurada a mais na última hora antes da virada é erro muito mais barato.

#### 6.10.6 Correção da v0.1.6 — a marca que o Windows põe e ninguém vê

Mesma pergunta das três versões anteriores, apontada agora para o caminho que o usuário vai realmente usar: **`/kame-keys import` num arquivo salvo por ferramenta do Windows.** E, como nas outras, o método foi escrever os arquivos e ler a saída, não raciocinar sobre eles.

| Arquivo salvo como | Lido como UTF-8 | Lido pela marca |
|---|---|---|
| `Set-Content -Encoding utf8` | ok | ok |
| `Set-Content -Encoding utf8BOM`, Notepad "UTF-8 com BOM" | **primeira chave rejeitada** | ok |
| `Set-Content -Encoding unicode`, `>` do Windows PowerShell | **todas rejeitadas** | ok |

A marca são três bytes que viram um caractere de **largura zero** grudado na primeira chave. Sobrevive a todo `strip()`, é invisível no editor, e faz a chave falhar no teste de ASCII imprimível — o usuário vê uma chave rejeitada num arquivo que parece perfeito, sem nada para olhar. UTF-16 é pior: lido como UTF-8 é lixo desde o primeiro byte e não sobra nada.

**A correção:** `decode_text` fareja a marca e decodifica por ela; UTF-8 continua sendo a resposta para arquivo sem marca, que é todo arquivo provável. Ordem importa — `BOM_UTF32_LE` começa com os dois bytes do `BOM_UTF16_LE`, então checar UTF-16 primeiro decodifica UTF-32 errado **em silêncio**. E o mesmo caractere é retirado de token colado, porque copiar de um arquivo marcado leva a marca junto para a área de transferência.

Isto não é fato de provedor, é fato de plataforma — e é verificável na máquina, o que o torna o oposto de fixture escrita de memória.

#### 6.10.7 Correção da v0.1.7 — contar chave do jeito que a pool conta

A mesma pergunta, virada para o **relato**. `/kame-keys status` existe para responder uma coisa: *tenho chave funcionando?* Ele respondia com o número de linhas da pool.

A pool não conta assim. Antes de olhar status, cooldown, prioridade ou qualquer outra coisa, ela faz isto (`agent/credential_pool.py`, `_available_entries`):

```python
if entry.auth_type == AUTH_TYPE_API_KEY and not entry.runtime_api_key:
    continue
```

Linha sem chave de runtime é pulada direto. Não é chave em dia ruim — é linha que **nunca** vai servir requisição. Três jeitos de a pool ficar com uma:

- **fonte de env que resolve para nada** — a linha é semeada de `GOOGLE_API_KEY` e a linha do `.env` está comentada;
- **credencial emprestada**, que persiste como referência só-metadado e é hidratada no load, deixando duplicata velha sem hidratar;
- **lease vencido** — entrada `nous` chaveia no invoke JWT, e `runtime_api_key` vira `""` no instante em que esse JWT deixa de valer.

Nenhum desses mexe em `last_status`, porque nada aconteceu com eles. Então a linha lê `ok`, e pool de três chaves sem nenhuma credencial funcionando era relatada como três chaves, todas bem. **Este é o estado exato da pool Gemini do dono do projeto agora.**

Duas coisas erradas, e eram a mesma: **o KAME lia `access_token`, o campo guardado, enquanto o host roda em `runtime_api_key`, propriedade calculada.** O descasamento relata linha morta como saudável *e* credencial `nous` viva como em branco. As duas direções têm teste, e a contagem se divide quando discordam:

```
gemini — 2 of 3 key(s) usable
  [no key] GOOGLE_API_KEY  (empty — the pool skips it)
```

Entrada OAuth legitimamente não tem chave de API e fica de fora da regra — a regra do host é escopada a `AUTH_TYPE_API_KEY`, e alargar aqui trocaria uma resposta errada por outra.

**Considerado e não feito.** O `status` não mostra *até quando* uma chave banida fica banida, embora o KAME calcule exatamente isso. Fica assim: `/kame-quota` renderiza o ledger com prazo e escopo por modelo, e duplicar número vivo em dois comandos é como os dois começam a discordar. `status` responde "o que tem na pool", `quota` responde "até quando" — o defeito era o `status` errando a **própria** pergunta, não deixando de responder a outra.

**Suspeita checada e descartada.** Que variável de ambiente sombreasse a pool inteira, já que `_resolve_api_key_provider_secret` tenta `api_key_env_vars` antes da pool. Tenta mesmo — mas os dois caminhos que importam chegam na pool primeiro (`agent_runtime_helpers` chama `pool.select()`, `auxiliary_client` chama `_select_pool_entry` e só cai fora quando o provider não tem pool nenhuma). Rotação não é afetada. Registrado porque a versão errada desta nota teria embarcado um aviso sobre um problema que não existe.

### 6.11 Fase 6.1 — decidir com o que foi medido — PRÓXIMA

Só depois de o journal ter visto 429 de verdade:
- verificar previsão contra o observado e corrigir a janela quando o padrão se repetir
- aprender provider desconhecido empiricamente, sem marcador no corpo
- detectar modelos que dividem o mesmo balde de cota **quando o provedor não diz** (a v0.0.8 cobre o caso em que ele diz)

### 6.12 Fase 7 — visibilidade
Painel de saúde de chaves. Depende de o dashboard do Hermes aceitar página de plugin — **não verificado**. `/kame-keys` e `/kame-quota` cobrem a necessidade por enquanto, e funcionam no celular, que o dashboard também cobre mas com o peso de bundle que motivou o app nativo.

#### 6.10.8 Correção da v0.1.8 — uma credencial que guarda várias chaves

**O defeito.** O usuário disse, e repetiu: *"no Hermes no provedor eu coloquei todas APIs juntas mas separadas por vírgula no provedor do Google"*. O Hermes não divide isso em lugar nenhum:

```python
token = _get_env_prefer_dotenv(env_var)          # agent/credential_pool.py
...
_upsert_entry(entries, provider, source, _env_payload(token=token, ...))
```

Uma variável de ambiente vira exatamente uma entrada de pool, com a string inteira dentro. Medido contra o host instalado, antes de existir esta versão:

```
entries created: 1
  label= GOOGLE_API_KEY | key len= 119 | commas in key= 2
available: 1
```

Ou seja: o conjunto inteiro de chaves do usuário sempre foi **uma credencial malformada**. O provedor recusa a string toda, a pool tem uma credencial só, e a rotação — que é a razão de este plugin existir — não tinha para onde ir. Dezoito versões afinando *quanto tempo* deixar uma chave de castigo, com uma chave só.

**Por que o formato não é erro de quem digitou.** É o formato que a pool de chaves do Agent Zero aceita — de onde esta rotação foi portada — e o que o próprio `/kame-keys add` deste plugin aceita. Corrigir isso mandando o usuário redigitar seria transferir para ele um defeito que é do software.

**A correção.** As partes são derivadas na carga, com as mesmas regras de separador do `/kame-keys add` (`core/keys.py::parse_keys`), e nunca escritas. Três propriedades sustentam isso:

1. **Nada é gravado.** `_persist` é embrulhado e esconde as partes durante a escrita; `write_credential_pool` reincorpora qualquer linha que esteja no disco e falte na lista, então omiti-las não apaga nada. A divisão é **ligada por essa guarda**: um Hermes cujo `_persist` o plugin não consegue embrulhar não ganha o recurso. E o host recusa por conta própria — fonte derivada não está em `_PERSISTABLE_PROVIDER_SOURCES`, então `sanitize_borrowed_credential_payload` tira o segredo na fronteira do disco, inclusive com o plugin desinstalado (seção 22 do sandbox prova).
2. **Identidade é a chave, não a posição.** O id de uma parte é SHA-256 truncado da própria chave. Com id posicional, apagar a primeira chave da lista renumeraria todas as seguintes e o ledger — que lembra o que está gasto por id — leria todas como credenciais nunca testadas: pool sem cota pareceria nova.
3. **A lista é relatada como lista.** Redigida, `k1,k2,k3` imprime `AIzaSy…cccc` — começo da primeira chave e fim da última —, indistinguível de uma credencial comum. Vira `[list]`, e não conta como nenhuma das chaves de dentro, que já são contadas onde aparecem.

**O que foi considerado e não feito.** Persistir as partes: cria uma segunda cópia de uma lista cuja única fonte correta é a linha do `.env`, que não acompanha edição e que seria redividida na carga seguinte em partes de partes. E uma exclusão explícita para `nous`, que foi escrita e removida: a credencial de runtime dele é JWT lido de `agent_key`, e a propriedade devolve ou o JWT — que não tem separador — ou string vazia. Nenhum dos dois divide, e nenhum teste caía ao apagar a exclusão, então ela caiu.

#### 6.10.9 Correção da v0.1.9 — o corpus de erro do próprio Hermes

**Por que esta prova faltava.** Todo payload de teste deste projeto foi escrito pela mesma mão que escreveu o classificador. Isso é declaração de intenção, não evidência — e já custou caro uma vez: até a v0.1.3, três versões foram verdes contra uma frase que o Google não manda. O Hermes traz o corpus dele: `tests/agent/test_error_classifier.py` e `tests/test_transform_api_error_classification_hook.py`, 89 casos sobre ~14 provedores, escritos por gente que nunca viu este plugin.

A pergunta que ele responde não é "o KAME classifica bem". É a mais dura: **o KAME deixa o julgamento do host intacto?** O hook do KAME é consultado **antes** de todo o pipeline embutido e a primeira resposta válida vence — então um hook que reivindica demais substitui, em silêncio, catorze provedores de classificação afinada pela opinião dele. `tools/host_corpus.py` roda o corpus duas vezes contra o despacho real (`get_plugin_error_classification`): sem o hook, e com o callback do KAME atrás dele. Todo teste que muda de resultado é um payload em que o KAME atropelou o host.

**O que ele achou.** Dois, e nos dois o host estava certo:

| Payload | Host | KAME dizia |
|---|---|---|
| `401 "Unauthorized"` | `auth` — rotaciona | `auth_permanent` — **aposenta a chave** |
| `404 "model not found"` | `model_not_found` — tenta outro modelo | `auth` + rotação + uma hora de castigo |

Mesma forma nos dois: uma palavra larga o bastante para aparecer em mensagem que não é sobre a credencial. `unauthorized` é a *reason phrase* do 401 — chega em todo 401 pelado de proxy, de gateway, de token OAuth no meio da renovação; ler isso como chave morta joga fora credencial que ia funcionar. E `model not found` é sobre o nome do modelo: com várias chaves, essa resposta percorre a pool inteira por um modelo escrito errado e deixa todas de castigo.

Os dois eram invisíveis aqui porque nenhum teste deste projeto jamais alimentou o plugin com as palavras exatas que o provedor usa — `test_plain_401_is_left_to_the_host` passou o tempo todo dizendo *"authentication failed"*, que não é o que um 401 diz.

**A correção.** `unauthorized` sai dos padrões permanentes (as frases que de fato aposentam chave — *"API key not valid"*, *"invalid api key"*, *"key revoked"*, *"incorrect API key"* — continuam, e há teste para cada uma), e `found` sai da alternação de `model not (…)`, onde `authorized` e `available` ficam porque essas são sobre o nível da chave. Depois: 89/89 do corpus do host idênticos com e sem o KAME.

#### 6.10.10 Correção da v0.2.0 — três coisas podem acontecer com um prazo, e o registro tinha dois valores

**O defeito.** `Block.sized_by` respondia "quem dimensionou este banimento" com `host` ou `kame`. Mas são três situações:

| O que aconteceu | Como era registrado |
|---|---|
| KAME não classificou, ou classificou sem prazo | `host` |
| KAME deu um prazo e a pool guardou | `kame` |
| KAME deu um prazo e **a pool não guardou** | `host` |

A terceira é a que não podia ficar junta com a primeira, porque ela é a **assinatura de plugin inerte**: toda recusa classificada, todo prazo calculado, e nada disso chegando na pool — host que limita o valor, entrada substituída entre o veredito e a escrita, build do Hermes cujo campo de cooldown mudou de lugar. Dobrada em `host`, um plugin cujos números estão todos sendo descartados produz um journal idêntico ao de instalação quieta e saudável. É a mesma forma de falha da v0.1.3, onde três versões de dimensionamento cuidadoso ficaram atrás de uma classificação errada e nunca rodaram — e ali só apareceu porque eu fui reler o payload do provedor. Aqui o próprio relato tinha como dizer, e não dizia.

**A correção.** Terceiro valor, `dropped`. A medição continua contando só `kame` — banimento que o KAME não governou não é evidência sobre o dimensionamento do KAME, em direção nenhuma —, e valor desconhecido (linha de versão mais nova, linha corrompida) cai em `host`, nunca em `kame`, porque `kame` é o valor sobre o qual a medição age. O `/kame-quota` ganha a contagem e, quando **nenhum** prazo passou e dois ou mais foram descartados, uma frase: *"every deadline KAME read here was dropped before it reached the pool — the cooldowns you are seeing are the host's, not KAME's"*. Um prazo descartado é corrida ou limite do host; todos descartados é plugin rodando à toa.

#### 6.10.11 Correção da v0.2.1 — o que seis provedores dizem mesmo sobre chave morta

**Método, de novo o mesmo.** O cabeçalho do `test_core.py` sempre disse a verdade: as cargas do Google eram capturas, as dos outros eram *reconstruídas do formato documentado*. Essa é exatamente a classe de fixture que esteve errada sobre o Google por quatro versões. Dava para capturar: um pedido por provedor com chave obviamente falsa. Chave inválida é recusada antes de qualquer medição, então nenhuma credencial de ninguém foi usada e nenhuma cota foi gasta.

O que voltou, verbatim:

| Provedor | Mensagem |
|---|---|
| OpenAI | `Incorrect API key provided: sk-fake-***0000. …` |
| Anthropic | `API key is invalid.` |
| Groq | `Invalid API Key` |
| Mistral | `Invalid API Key` (em `detail`, sem envelope `error`) |
| DeepSeek | `Authentication Fails, Your api key: ****0000 is invalid` |
| OpenRouter | `Missing Authentication header` |

**O defeito.** Todo padrão de auth permanente lia *invalid key*. Dois dos seis dizem ao contrário — *key **is** invalid* — e nenhum padrão pegava. Consequência: chave morta de verdade na Anthropic ou na DeepSeek voltava para o host como 401 pelado e era **rotacionada para sempre**, nunca aposentada. É o cenário exato que o `auth_permanent` existe para evitar: redescobrir que a chave está morta a cada rodada da pool.

**A correção.** Um padrão para a ordem invertida, com a chave e o veredito na mesma oração: a folga entre `key` e `is invalid` é de 24 caracteres porque a maior real é doze — o `: ****0000 ` redigido da DeepSeek. Cada caractere de folga além da evidência é uma frase sobre outra coisa que o padrão passa a alcançar. O `Missing Authentication header` do OpenRouter continua declinado: nada há de errado com chave nenhuma ali, e aposentar uma seria o plugin inventando defeito.

#### 6.10.12 Adição da v0.2.2 — a suíte de pool do próprio Hermes, e a prova de que a prova enxerga

O `host_corpus.py` (6.10.9) cobre metade do plugin: a classificação. A outra metade é a que chega mais fundo no host — o binding troca cinco métodos do `CredentialPool`, que é todo caminho por onde passa seleção e persistência. Sobre essa metade, a prova mais forte até aqui era o `sandbox_binding.py`, e ele responde a pergunta errada pelo mesmo motivo dos fixtures: prova que os embrulhos fazem o que eu quis, contra uma pool que **eu** montei. Não prova que deixaram em paz o que o host faz com a pool dele.

`tools/host_pool_suite.py` roda catorze suítes de pool do próprio Hermes duas vezes — limpas, e com um `PoolBinding` de verdade instalado na classe de verdade. Refresh adiado, reseleção de lease, write-through de OAuth, tranca de quarentena, fronteira de provedor, cooldown de credencial única, limite de rotação: tudo responde igual. Duas falham nas duas rodadas por dependência que falta neste ambiente (`prompt_toolkit`), e o harness **diz o nome delas em voz alta** em vez de subtrair em silêncio — quem lê "não mudou nada" não pode entender "passou tudo".

E aqui a armadilha que o 6.10.10 já tinha ensinado, em outra roupa: **um harness inerte imprime exatamente a mesma linha tranquilizadora que um plugin inofensivo.** Nada no resultado distingue "o binding não atrapalha" de "o binding nem foi instalado". Então a terceira fase quebra o binding de propósito — esconde uma credencial da seleção, que é o estrago exato de um embrulho descuidado — e o harness se recusa a declarar sucesso se o host não reclamar. O host reclama em 58 testes. O binding de verdade custa zero.

#### 6.10.13 Adição da v0.2.3 — o 429 que ninguém tinha visto atravessar

A definição de pronto (seção 9) sempre disse a mesma coisa: **nenhum 429 de verdade passou pelo plugin instalado**. Todas as provas param antes do fio. O sandbox monta a exceção na mão; o corpus entrega cargas ao classificador; o `verify_installed` mostra os embrulhos presos, mas nunca dispara um. A frase pela qual o plugin existe — *chega um 429 e sai uma pausa* — nunca tinha sido observada inteira.

`tools/live_429.py` observa. Servidor local responde com um 429 free-tier verbatim do Google; o SDK real da OpenAI levanta um `RateLimitError` de verdade, saído de um socket de verdade; o `classify_api_error` do próprio Hermes roda com o plugin instalado atrás do hook; e o `recover_with_credential_pool` do próprio host bane a chave e rotaciona uma `CredentialPool` real. Depois lê de volta o prazo gravado na chave gasta e o texto do `/kame-quota`.

**Duas recusas, não uma**, porque o que interessa é a distinção: a mesma frase e o mesmo `retryDelay: "21s"` chegam no estrangulamento por minuto e no teto diário — só o contador nomeado no corpo separa os dois. O por-minuto foi segurado 21 segundos; o diário foi segurado até a meia-noite US/Pacific, 13 horas depois. Ler os 21 segundos ao pé da letra no teto diário é exatamente o que reprova a chave gasta a cada vinte segundos até meia-noite.

E a fase 7, pela decisão 42: desliga o KAME pelo próprio interruptor dele, dispara a mesma carga no mesmo socket, e confirma que o prazo **some** — o host continua classificando `rate_limit`, sem prazo nenhum. Prova que não tem como falhar não está medindo o que diz medir.

O que isto **não** é: o provedor. Nenhuma cota gasta, nenhuma credencial usada, chaves obviamente falsas e o único endpoint contatado é o socket deste processo. Tudo entre o fio e a pool é real; a frase no fio é captura.

Dois detalhes que custaram tempo e ficam registrados. O plugin é opt-in por `config.yaml` **do home**, então o home descartável precisa da linha que o habilita — sem ela a descoberta acha o plugin e não carrega. E importar o Hermes normaliza as variáveis de proxy: o cliente OpenAI passou a mandar `127.0.0.1` por proxy e o erro (`APIConnectionError`) parece servidor morto. `trust_env=False` no cliente resolve.

#### 6.10.14 Adição da v0.2.4 — as duas partes que o fio ainda não tinha tocado

A v0.2.3 levou o 429 até a pausa. Faltavam as duas partes do plugin que ninguém tinha visto funcionar com tráfego real, e são justamente as duas que existem por causa de casos que doem.

**O lane auxiliar.** Ele não dispara hook nenhum — nem `pre_api_request`, nem o classificador. Por isso o plugin embrulha os três relays: o embrulho é a única coisa que anuncia qual modelo está no fio. A fase 7 pergunta de **dentro** de uma chamada de relay real: enquanto o modelo menor está no socket, a chave que o modelo principal acabou de gastar está disponível pra ele? Está. E um instante depois, com o anúncio desfeito, a mesma pool volta a segurá-la. Essa é a prova e o controle na mesma fase — mesma pool, mesmo instante, só o anúncio muda.

**A saída de emergência.** Uma chave só, gasta no modelo principal, sem alternativa. O `reset_at` do provedor desarma a saída de emergência do próprio host, então o KAME põe uma de volta: a pausa é oferecida pra teste numa escala que abre (5m, 10m, 20m, 30m). A fase 8 bane a chave única com o número que veio do 429 real, confirma que nada é oferecido, adianta **só o relógio do binding** em cinco minutos e confirma que ela volta pra teste. Único ponto do arquivo em que algo não é ao vivo, e está escrito no comentário.

#### 6.10.15 Adição da v0.2.5 — a forma que o usuário realmente tem

A divisão de uma credencial que carrega várias chaves existe desde a v0.1.7 e tinha duas provas: `tests/test_multikey.py` para as regras e o `sandbox_binding.py` contra uma pool construída aqui. Nenhuma das duas passa pelo `load_pool` — a função que lê a variável de ambiente real, pelo registro de provedores real, e monta a pool que uma execução real usa. É exatamente esse caminho que o dono do projeto vai exercitar na mão, com `GOOGLE_API_KEY` guardando todas as chaves dele separadas por vírgula.

`tools/live_multikey.py` observa esse caminho em sete fases. Três chaves falsas de comprimento realista numa variável só; `cp.load_pool("gemini")` com o plugin instalado atrás; e a pool oferece **três** credenciais utilizáveis onde o host sozinho oferece uma. A linha-pai continua na pool — é a linha que existe no disco e a que a variável mapeia — mas nunca é oferecida, porque a chave dela é a lista inteira e nenhum provedor aceita isso.

Depois dois 429 reais saindo do socket, pelo classificador real e pelo `_recover_with_credential_pool` do host: `GOOGLE_API_KEY (1/3)` para `(2/3)`, e `(2/3)` para `(3/3)`, com prazo próprio na chave gasta e as outras intactas. Em seguida a checagem que autoriza o recurso: o `auth.json` do home descartável não tem nenhuma parte derivada e nenhuma das chaves divididas — só a linha que foi digitada. Editar a lista (tirar a do meio) recomputa as partes e as sobreviventes mantêm o mesmo id, porque o id é hash da chave e não da posição.

E a fase 7, pela decisão 42: desinstala o binding, chama o mesmo `load_pool` na mesma variável, e o host volta a ser o que ele é sem o plugin — **uma** credencial, com duas vírgulas dentro da chave, e nada para onde rotacionar.

A fase 8 responde a pergunta do dono direto: **quantas chaves cabem?** Nenhum limite existe em lugar nenhum — a única regra por chave é o piso de 16 caracteres — mas "não tem limite no código que eu escrevi" é afirmação sobre o código, e a pergunta é sobre a pool. Então quinze chaves num campo só: pool montada em ~24 ms, quinze credenciais utilizáveis, e a rotação percorre as quinze uma recusa por vez até não sobrar nenhuma. Custo por chave é uma linha em memória, recomputada na carga.

Essa fase falhou na primeira escrita com **13 de 15**, e a falha estava certa: eu reusei as três chaves das fases anteriores, e duas delas ainda estavam de castigo — o ledger lembra chave gasta pelo **hash da chave**, então elas voltam banidas até de uma pool recém-carregada. Exatamente a decisão 37 funcionando. A fase usa chaves inéditas agora, com o motivo escrito no comentário.

Um detalhe que quase fez o harness mentir: as chaves de teste precisam ter comprimento realista. O `parse_keys` recusa token abaixo de 16 caracteres, porque fragmento curto numa lista colada é pontuação, não chave. Chaves falsas de 13 caracteres foram descartadas pelo motivo certo e a primeira execução mostrou "não dividiu" — que era verdade sobre as chaves, não sobre o plugin.

#### 6.10.16 Adição da v0.2.6 — qual das chaves saudáveis sai agora

Tudo que o plugin fez até aqui responde **"esta chave serve?"**. Nada responde **"das que servem, qual?"** — e a resposta padrão do host é `STRATEGY_FILL_FIRST`: devolver `available[0]` sempre. Com uma chave paga isso está certo. Com quinze chaves e limite por minuto, uma chave absorve todas as requisições até o provedor recusar, depois a próxima absorve tudo até recusar — quinze paredes em fila em vez de quinze chaves de vazão.

O KAME do Agent Zero responde essa pergunta desde a v1.0.0 e essa é a peça grande que tinha ficado para trás:

```python
best_key = min(healthy, key=lambda k: (
    len(pool[k]["request_log"]),   # menos requisições nos últimos 60s
    pool[k]["last_used"],          # empate: a menos usada recentemente
))
pool[best_key]["last_used"] = now
pool[best_key]["request_log"].append(now)   # conta antes da chamada
```

`core/dispersion.py` é isso, mesma janela e mesmo desempate. **Contar na entrega, não no retorno, é o anti-dogpile**: a chave fica marcada como ocupada no instante em que sai, então a thread que seleciona um milissegundo depois já a vê carregada e pega outra. Contar depois da chamada faria todo chamador concorrente escolher a mesma.

Duas coisas que ele deliberadamente não faz. Nunca **exclui** chave: muda a ordem, nunca o conjunto — nenhuma requisição pode falhar por causa de nada decidido ali. E não guarda material de chave.

O estado é global por balde `provider:model`, não por instância de pool, porque o `auxiliary_client.py` chama `load_pool(provider)` a cada chamada auxiliar (linhas 1313, 1329, 4544, 4785): estado preso à instância seria apagado o tempo todo. Memória apenas — janela de sessenta segundos não significa nada depois de um restart — e com teto duplo (64 baldes, 240 marcas por chave) para um agente longo que passeia por muitos modelos não virar vazamento.

**O contador tem nome de chave, não de linha.** Foi a correção que veio da suíte do host. A cota por minuto é medida pelo provedor contra a *chave*; o id da pool nomeia uma *linha*, e os dois se descolam nas duas direções: duas linhas podem carregar uma chave só (o Hermes semeia da variável de ambiente **e** do `auth.json`, e o próprio código de rotação dele precisa detectar irmãs com `runtime_api_key` idêntico), e uma linha pode carregar duas chaves ao longo do tempo (trocar a chave gasta no lugar). Contado por linha, o primeiro caso martela um contador do provedor no dobro do ritmo pretendido enquanto uma terceira chave ociosa espera; o segundo faz a chave nova herdar tudo que a velha gastou. Então o nome é o hash da chave — mesma construção da decisão 37 — e cai para o id quando não há chave para ler, que é o caso da entrada OAuth cujo token o host rotaciona sozinho.

**O que o `_spread` não toca.** Só a chamada de seleção é reordenada, marcada por um `threading.local()`: relatório, contagem, checagem de vazio e a suíte do próprio host chamam `_available_entries` sem estar decidindo chave nenhuma, e recebem a ordem do host. E só o `fill_first` é reordenado — `round_robin`, `random` e `least_used` são escolha digitada por alguém num arquivo de configuração. Sobrescrever default não declarado é correção; sobrescrever escolha declarada é ignorar o usuário.

**As duas asserções do host que este recurso derruba.** O `host_pool_suite.py` acusou quatro; duas eram ambiente (`prompt_toolkit` ausente, falham igual sem o plugin) e duas são de verdade:

```
tests/agent/test_credential_pool_routing.py::TestApiKeyHintRealPool::test_without_hint_current_entry_is_marked
tests/agent/test_credential_pool_routing.py::TestFailureAttribution::test_auth_refresh_targets_failing_key_not_pointer
```

As duas montam pool de duas chaves, selecionam uma vez e exigem que a **mesma** chave volte na seleção seguinte. Está escrito como preparo (*"Point the shared cursor at the healthy entry"*), não como a propriedade sob teste — mas é o `fill_first` que elas afirmam, e é exatamente o default que este recurso substitui. Não dá para ter as duas coisas.

O harness não passou a tolerar isso em silêncio, porque divergência que a bancada não sabe nomear é regressão que ela ainda não percebeu. As duas estão listadas em `EXPECTED_DIVERGENCE` com o motivo, e cada uma tem de **falhar** com o plugin como entregue **e passar** com `KAME_SPREAD_DISABLED=1`. Falhar dos dois jeitos significa que não é este recurso e o harness reprova; passar dos dois jeitos significa que o recurso morreu e o harness reprova também.

`KAME_SPREAD_DISABLED` é chave separada do `KAME_ROTATION_DISABLED` de propósito. Este é o único pedaço do plugin capaz de fazer uma instalação **funcionando** escolher chave diferente do Hermes de fábrica — quem quiser `fill_first` de volta sem perder o dimensionamento de pausa desliga só ele.

#### 6.10.17 Adição da v0.2.7 — a chave que acabou de voltar espera a vez, e o que do Agent Zero não cabe aqui

A v0.2.6 escolhe pela carga. Falta o outro critério que o KAME do Agent Zero usa: **descanso**. Lá o filtro só vale para chamada de compressão (`is_compress_ctx`), porque uma compressão de 90k tokens é a chamada mais cara que existe e falhar nela dói muito mais do que espalhar carga de forma desigual:

```python
if is_compress_ctx and len(healthy) > 1:
    fresh = [k for k in healthy
             if (pool[k].get("last_sick_at") or 0) == 0
             or (now - pool[k]["last_sick_at"]) > _KAME_COMPRESS_FRESH_WINDOW_S]
    if fresh:
        healthy = fresh
```

Aqui esse critério é gasto em **toda** seleção, e a evidência é do próprio plugin: prazo lido curto demais aparece como chave recusada de novo poucos minutos depois de ser devolvida — é exatamente o padrão que o `escalate.py` existe para alargar (v0.1.x). Quando duas chaves estão empatadas, e empatadas é o que elas quase sempre estão, a que descansou é a escolha mais segura, e preferi-la não custa nada.

Descanso vem **antes** da carga na ordenação: uma chamada que falha custa mais que uma carga mal distribuída. E é preferência, nunca exclusão — pool em que todas as chaves acabaram de voltar degrada para a ordenação por carga, não para resposta vazia.

**De onde sai "acabou de voltar": da própria entrada, não do ledger.** A pool guarda `last_error_reset_at` até algo dar certo na credencial, então prazo que venceu agora ainda está legível ali — inclusive os que o **host** escreveu, que o ledger nunca viu. E entrada ainda banida tem prazo no futuro, então nunca cai nesse conjunto: ou já está retida, ou é a sonda da saída de emergência, oferecida justamente porque não havia nada descansado para preferir. Ordenar a sonda por último derrotaria a saída de emergência.

Noventa segundos, não para sempre. As recusas mais curtas que este plugin dimensiona são limites por minuto; chave devolvida no fim de um deles fica dentro da janela seguinte por mais ou menos isso. Prazo que venceu de manhã é chave descansada agora — tratar diferente deixaria uma credencial em último lugar pelo resto do processo.

**As duas peças do Agent Zero que foram checadas e não cabem.** Elas não estão pendentes; elas não têm assunto aqui:

- **Backoff próprio de 5xx** (`consecutive_server`, 5s×2ⁿ até 90s). O Hermes **não bane credencial em 5xx, de propósito**. O comentário está no `conversation_loop.py:4940`: um 429 de overload do Z.AI é classificado como `overloaded` *"(to spare the credential pool)"*, e `overloaded` fica fora do gate de `is_rate_limited`. Não há cooldown de servidor para escalonar, e criar um significaria banir chave que o host deliberadamente poupa — contra a decisão 8, que já dizia que falha de servidor é recusa.
- **Degelo pós-outage** (`_thaw_server_cooled_keys`). O assunto dele são chaves esfriadas por 5xx, que pelo item acima não existem aqui. Encurtar banimento de cota por sucesso em *outra* chave seria exatamente o que a função do Agent Zero se proíbe de fazer, e o que a decisão 19 deste plugin já proíbe: só sucesso **na própria chave** encurta banimento.

Com isso a contabilidade de paridade fecha: das cinco peças que faltavam na v0.2.5, três foram portadas (seleção por RPM, anti-dogpile, filtro de frescor) e duas foram descartadas com a linha do host como prova, não por esquecimento.

#### 6.10.18 Adição da v0.2.8 — o espalhamento visível

Da v0.2.6 até aqui, o recurso mais usado do plugin era o único invisível. Banimento aparece no `/kame-quota`; aprendizado aparece no `/kame-quota`; **escolha de chave** não aparecia em lugar nenhum. Pool rotacionando e pool martelando uma credencial produziam exatamente a mesma tela — e o dono do projeto pediu justamente para poder testar.

`Dispersion.snapshot(now)` devolve `{balde: {chave: requisições na janela}}`, cópia tirada sob o mesmo cadeado e com a mesma poda que a seleção usa. Isso não é detalhe de concorrência: relatório que aplica janela diferente da decisão mente com número, que é pior que não ter número. E devolver cópia é o que impede `RuntimeError: dictionary changed size during iteration` no meio de um turno de chat, com outra thread selecionando.

**Duas fontes de nome, porque a contagem é por chave (decisão 44).** O que chega ao relatório é hash de chave, e só o binding sabe a qual rótulo ele corresponde — daí `PoolBinding._names`, mapa limitado, só rótulos, alimentado no mesmo ponto em que a seleção conta a requisição. Mas entrada sem chave legível (OAuth) cai no id nu, e esse id o próprio listador de pool já rotula. As duas fontes são somadas, não escolhidas.

Seção fica **entre** as duas antigas de propósito: banido é o que não está disponível, espalhamento é o que está sendo usado no lugar, e o aprendizado é a evidência de longo prazo atrás das duas. Ordenado por carga decrescente, porque a pergunta que ela responde é "uma chave está levando tudo?" e a resposta pertence à primeira linha.

Minuto quieto diz que está quieto. Janela é de sessenta segundos e nada sobrevive a reinício, então lista vazia é o estado normal de quem acabou de abrir o Hermes — e página em branco leria como defeito.

#### 6.10.19 Adição da v0.2.9 — quantas ele conseguiu ler

Declinar é o caminho comum e o seguro (decisão 39), e é justamente isso que torna dois instalações idênticas por fora: uma que lê todas as recusas do provedor e outra que parou de reconhecer a carga dele há seis semanas. As duas ficam quietas. O journal não separa as duas, porque ele só registra falha que virou **banimento** — erro que o host classificou como outra coisa não deixa rastro nenhum.

Esse é exatamente o defeito que este projeto já teve duas vezes: frase escrita de memória deixou quatro versões verdes e inertes (decisão 26), e palavra larga demais só apareceu no corpus do próprio host (decisão 38). Nas duas, quem achou foi alguém indo olhar. `core/tally.py` é o contador que dispensa ir olhar.

**Só números.** A carga por trás de cada contagem é `error_message`/`error_body`, que o contrato do hook avisa poder ser dump não redigido. O que entra é nome de provedor, número de status e dois inteiros — não existe campo neste módulo capaz de imprimir chave, prompt ou URL, e isso é propriedade do que ele guarda, não de como é chamado.

**Mora no `runtime`, não no binding.** A metade de classificação roda mesmo quando a ligação com a pool não instalou — e é exatamente essa instalação cujo estado é mais difícil de ver. Por isso o `/kame-quota` sem binding, que antes eram três linhas de desculpa, agora traz a seção.

**Uma linha é apontada, e só uma.** 401 sempre declinado é o plugin funcionando; apontar aquilo faria o estado normal parecer defeito. 429 com nenhuma leitura é outra coisa: ler a espera de um 429 é o trabalho inteiro naquele status, e coluna disso é a assinatura de plugin inerte. Uma única leitura bem-sucedida derruba a acusação, porque a afirmação é "não consegue ler este provedor", e provedor mistura throttle com depleção.

**Teto derruba tudo em vez de despejar uma linha.** A seção é lida como "desde que o Hermes subiu"; total parcial lido como total é resposta errada, recomeçar a conta é resposta honesta.

#### 6.10.20 Adição da v0.3.0 — a janela é curta demais para servir de prova

A seção da v0.2.8 mostra o minuto que a seleção usa para decidir. Ela é honesta e é inútil para quem quer **conferir**: sessenta segundos é uma janela que exige pegar a pool no ato. Quem abre o `/kame-quota` dois minutos depois de uma conversa vê "nada foi entregue" e não fica sabendo de nada.

`Dispersion.totals()` é a contagem desde que o processo subiu, por balde e por chave. Ela **não decide nada** — a ordenação continua lendo só a janela, e tem teste para isso: chave que levou mil requisições há uma hora e nada desde então é a **melhor** escolha agora, e deixar o total pesar enterraria justamente ela.

Um inteiro por chave por balde, e o balde já tem teto; quando um balde é despejado, o total dele vai junto — senão a única estrutura sem expiração seria também a única sem limite.

A linha ficou com dois números porque a pergunta são duas: "está espalhando agora?" e "essa chave já foi usada alguma vez?". A segunda é a que revela o caso que a seção existe para revelar: quinze chaves, uma levando tudo, catorze em `idle · 0 since Hermes started`.

**Cabeçalho e o dois-pontos.** O balde é `provedor:modelo`, e os dois lados podem conter dois-pontos — `custom:<nome>` de um lado, `llama3:8b` do outro. Não existe corte certo para os dois; o primeiro dois-pontos vence, porque preserva a tag do modelo, que é por onde o leitor identifica a linha.

O `reset` passou a zerar as contagens junto com o histórico. Zerar e ver encher é o uso natural delas: "o KAME está lendo este provedor **agora**" é pergunta sobre as próximas recusas, não sobre todas desde que o processo subiu.

#### 6.10.21 Adição da v0.3.1 — a resposta que voltou vazia

Achada por auditoria de paridade: reler o `kame_engine.py` inteiro contra o código de hoje, em vez de reler a tabela do §4 — que estava com três linhas dizendo "não" sobre peças portadas fazia quatro versões (decisão 51).

A peça que faltava de verdade era o `_kame_result_is_empty` (linha 1533). No Agent Zero ele existe porque chave espremida de free tier **não** manda 429 sempre: às vezes ela devolve 200 com nada dentro. Lá o engine é dono do laço de chamada, então resposta vazia é falha e a próxima chave entra.

Aqui o efeito da ausência era pior do que "recurso faltando". O `post_api_request` do plugin tratava qualquer retorno como prova de que a chave funciona — e essa prova, desde a v0.0.9, **aposenta o banimento para sempre**. Ou seja: a chave que acabou de mostrar o sintoma de estar espremida era exatamente a que saía do banco em definitivo.

**O que o host deixa fazer.** Retentar, não: o `post_api_request` recebe métricas e nenhuma alça no resultado — `run_agent.py:2687` ("Token buckets for `post_api_request` plugins (no raw `response` object)") e `moa_loop.py:1633` ("Read-only: a MoA turn's post_api_request hook must not disturb the accounting"). O que ele manda são `assistant_content_chars` e `assistant_tool_call_count`, direto do único ponto de despacho (`agent/conversation_loop.py:6262`) — e isso basta para a decisão que importa: **parar de acreditar**.

Resposta vazia não fecha sonda, não solta banimento, não escreve nada. E também não é registrada como recusa: ninguém recusou nada, não há status, não há janela, e inventar prazo que provedor nenhum disse seria pôr número falso no caderno (decisão 49).

**Chamada com ferramenta e sem prosa é resposta cheia**, e é a forma mais comum de turno útil num agente. Só o turno sem texto **e** sem chamada conta como vazio.

**Campo ausente não é campo zerado** (decisão 50). Se o Hermes parar de mandar os dois números, `carried_nothing` devolve `False` e o comportamento volta a ser o da v0.3.0 — porque ler ausente como zero desligaria a soltura de todo provedor de uma só vez, muito pior que o defeito corrigido.

Contado por provedor e mostrado na seção "What KAME was asked", só quando acontece (decisão 47): banimento de pé com todas as chamadas passando precisa de alguma coisa na tela explicando por quê.

De quebra, o `core.__version__` estava parado em `0.1.0` desde a v0.1.0. Agora tem teste amarrando ele ao `plugin.yaml` — é o número que viaja junto se o `core/` for levantado para outro host.

#### 6.10.22 Adição da v0.3.2 — botão que ninguém acha é botão que ninguém tem

Achada na revisão de configuração pedida pelo dono do projeto: *"até fazendo um checkout pra ver se não falta nada como algo nas configurações"*.

Os dois interruptores do plugin — `KAME_ROTATION_DISABLED` e `KAME_SPREAD_DISABLED` — eram só variável de ambiente. Corretos e invisíveis. O Hermes tem lugar próprio para isso: o manifesto declara `config_schema`, o usuário escreve em `plugins.entries.<id>.settings.<chave>` no `config.yaml`, e o plugin lê de volta por `ctx.get_config` (`hermes_cli/plugins.py:1422`).

**A variável de ambiente continua ganhando.** O `KAME_ROTATION_DISABLED` é a saída de emergência — o que alguém usa quando *suspeita deste plugin* de ter quebrado o agente, do shell, sem editar YAML que talvez nunca tenha criado. Arquivo que pudesse sobrescrever isso tornaria a saída de emergência condicional ao estado que ela existe para descartar. Ordem: ambiente se disser qualquer coisa, config depois, default por último.

**Lido uma vez, no `register`.** O `ctx.get_config` chama `load_config_readonly()` a cada acesso, e esses dois são consultados no caminho de classificação e no de seleção — toda chamada que falha e toda credencial que sai. Arquivo de configuração é relido quando o Hermes reinicia, que é o que mudar arquivo de configuração já significa no resto do host.

**Valor ilegível não é "off".** `_as_flag` devolve três estados, não dois: um `maybe` no YAML cai no default e sai um WARNING dizendo qual chave foi ignorada — erro de digitação silencioso seria indistinguível de desligar de propósito. Mesma forma da decisão 50, aplicada à outra ponta.

E o `verify_installed.py` ganhou duas fases que antes não existiam ou não provavam nada: a fase 6 imprimia "(context not exposed; skipping)" porque o manager constrói o `PluginContext` e não guarda referência — agora o harness constrói um do mesmo jeito, com a classe, o manifesto e o manager reais. A fase 7 pergunta ao host real se ele serve os dois nomes (`_plugin_relative_segments` recusa raiz reservada e caminho com ponto), e termina pedindo um nome que ele *tem* que recusar (decisão 42).

#### 6.10.23 Adição da v0.3.3 — a peça que não dá para portar, e a prova disso

O checkout de configuração da v0.3.2 abriu uma varredura maior: quais superfícies do Hermes o KAME ainda não usa? Uma resposta apareceu logo — `api_request_error`, um hook com três disparos, `retry_count`, `max_retries`, `retryable` e `reason` no payload, que o plugin ignora inteiro.

Dois dos três disparos **nunca passam pelo classificador**: resposta estruturalmente inutilizável (`conversation_loop.py:2959`) e recusa de conteúdo em HTTP 200 (`:3214`) chegam lá sem `classify_api_error`. Só o caminho de exceção (`:4299`) é classificado, e é justamente esse que o KAME já molda. E está certo assim: nenhum dos dois é recusa de provedor, então um plugin que colocasse chave de castigo por causa deles estaria punindo chave por o modelo ter ficado calado.

A recusa de conteúdo rendeu uma segunda descoberta, essa de código que **não** deve existir: o KAME não tem guarda contra `finish_reason == "content_filter"`, e não precisa ter. O host devolve o resultado em `conversation_loop.py:3282`, muito antes do disparo de `post_api_request`. Uma guarda ali seria código morto — e código morto com teste atrás parece proteção que não está lá.

**A peça de verdade.** O engine do A0 rotaciona a chave quando a resposta volta vazia (`kame_engine.py:1742`, orçamento em `:228`): pode ser sintoma de chave morta, então tenta a próxima. O KAME-Hermes v0.3.1 só *não acredita* na resposta vazia. Portar a rotação parecia o item que faltava, e o encaixe parecia perfeito: o host tem o próprio laço de retentativa de resposta vazia, em `conversation_loop.py:7302`, até 3 vezes — o carrossel do A0 oferecido de graça. Bastaria despriorizar a chave calada em `_spread`, que reordena sem nunca excluir.

Não bastaria. O `continue` da linha 7361 volta ao topo do laço **sem reler a pool**. A única `pool.select()` fora da pool está em `restore_primary_runtime` (`agent_runtime_helpers.py:1661`), que roda por turno e sai na primeira linha quando nenhum fallback foi ativado; e a chave viva só é trocada em `_swap_credential` (`run_agent.py:6073-6084`), alcançado por `recover_with_credential_pool`, isto é, pelo caminho de erro classificado. As três retentativas de resposta vazia saem todas na mesma chave, decida o KAME o que decidir.

Então a rotação por resposta vazia **não foi portada**, e a razão não é preguiça nem arquitetura: é que o efeito não teria como chegar. Uma feature dessas passa em todo teste que eu escrevesse — o `_spread` reordenaria certinho — e não faria nada em produção. É o pior resultado possível, pior que a ausência, porque a ausência pelo menos é visível.

O que ficou no lugar dela é `tools/host_assumptions.py`, a décima prova. A decisão 45 mandava citar a linha do host; citar é fazer uma afirmação sobre código dos outros, e código dos outros muda. A ferramenta lê o Hermes instalado e confere as afirmações em que as *não*-decisões do KAME se apoiam. Se um dia o laço de resposta vazia passar a reler a pool, ela falha e diz que a porta abriu — e aí a rotação vale a pena. Cada checagem foi sabotada de propósito antes de entrar; todas pegaram.

A v0.3.4 fechou a varredura pelo outro lado. O host dispara 24 nomes de hook; a maioria é sobre ferramenta, skill, sessão e gateway e não tem nada com credencial. Os que falam de chamada de API são exatamente quatro, o KAME registra três, e o quarto é o `api_request_error` de leitura acima. Isso deixou de ser uma contagem que eu fiz uma vez: virou o sétimo fato, e o oitavo confere que o manifesto ainda declara os três e só os três. Se o Hermes ganhar um quinto hook de API, a prova falha com o nome dele na tela — que é a forma útil de responder "não ficou nada pra trás" numa base que continua andando.

A v0.3.5 saiu do log do próprio usuário. O `agent.log` mostra o Hermes checando `capability=tools.override` contra o KAME e **negando** — o host tem sete superfícies atrás de permissão declarada, e eu nunca tinha olhado essa lista. O `deny` não custa nada: o KAME não registra ferramenta, não troca provedor, modelo, agente, perfil nem tarefa, e chega na pool embrulhando a classe, que o próprio registro diz não ser o que capability governa. Ou seja, ele instala sem pedir consentimento e sem precisar de concessão nenhuma. Virou o nono fato justamente porque isso pode deixar de ser verdade: uma capability nova chamada `credentials.*` ou `pool.*` seria um portão que o KAME teria de declarar — ou seria degradado calado, sem erro em lugar nenhum.

E a primeira execução do sétimo fato falhou por bug meu, não do host: `"_api_" in "api_request_error"` é falso, porque o nome começa com `api`. O filtro da checagem derrubava justamente o hook que ela existia para notar. Ficou registrado no código, porque uma checagem com filtro errado é uma checagem que passa sem olhar.

## 7. Decisões fechadas

1. Escopo: **só rotação**, core desacoplado. Não mega-patch.
2. Embedding do Agent Zero como memory provider: **não** — o embedding *era* a dor original (RAG traz trecho semanticamente perto e contextualmente errado). Usar o `holographic` embutido: SQLite local, FTS5, sem chave de API.
3. Pool: **única, compartilhada**. Cota do Google é por chave-por-modelo; main em `3.6-flash` e auxiliares em `3.5-flash-lite` caem em baldes diferentes e não competem. Pool por slot desperdiçaria cota.
4. Publicação: **privado por enquanto**.
5. Entrada de chave: **`/kame-keys` na v0.0.2** — vírgula, espaço, quebra de linha, `KEY=valor`. Funciona no terminal e no app. As três vias nativas continuam valendo (dashboard System > Credential pool, `hermes auth add`, variável de ambiente). Escrita sempre por `pool.add_entry()`, nunca editando `auth.json` na mão, e com backup antes do primeiro write.
6. Dashboard: **não mexer.** A UI de credencial já existe e é boa; falta só lote. Patch em React quebraria a cada release do Hermes.
7. **Sem allowlist de provider, nunca.** Critério é evidência, não identidade. Recusar é o caminho comum e o seguro — o host tem classificador competente, e sobrescrevê-lo com palpite é estritamente pior que ficar quieto.
8. **Falha de servidor e envelope de agregador: recusa.** Não é limitação, é a resposta certa — ver 3.3.
9. **Toda dica de recuperação vai explícita.** `should_fallback` inclusive, e principalmente. Default do `ClassifiedError` é `False`; omitir desliga.
10. **Substituir função interna do Hermes é permitido — sob uma condição.** O dono do projeto autorizou. A condição é `inspect_module`: o plugin só embrulha o que reconhece, e o modo de falha é sempre *menos* plugin, nunca estado corrompido. Nada é escrito na pool, em versão nenhuma.
11. **Embrulho opcional é marcado como opcional.** `_select_unlocked` serve a estatística e fica fora da checagem de forma. Recurso de correção não pode depender de embrulho cuja perda é aceitável — e vice-versa.
12. **Medir antes de decidir — mas propriedade estrutural não espera medição.** O journal registra e não age (v0.0.6): fechar o laço contra número inventado seria repetir, com mais código, o erro que o plugin existe para corrigir. Já a saída de emergência (v0.0.7) não ajusta palpite nenhum — garante que palpite nenhum tranque o usuário fora. Isso se prova por raciocínio e teste, e esperar dado de campo deixaria o pior caso em pé de graça.
13. **Relatório nunca mora no módulo que escreve chave.** `/kame-quota` em `status.py`, `/kame-keys` em `commands.py`. Separação por capacidade, não por conveniência.
14. **A saída de emergência é regra de último recurso, não otimização.** Só dispara com zero chaves usáveis para o modelo em voo, só em banimento que o KAME escreveu, e nunca em depleção que repetir não resolve. Qualquer versão futura que a use para "tentar mais cedo porque talvez dê certo" está usando errado.
15. **Armazenamento que não persiste degrada para não-durável, nunca para errado.** Escrita falhada mantém a cópia em memória como a verdadeira até uma escrita passar.
16. **Alcance também é evidência, e o silêncio tem de ser inerte.** A leitura "não disse nada" precisa ser exatamente o comportamento da versão anterior, para que uma dimensão nova só possa remover erro, nunca inventar um. Chute só é aceitável na direção barata; segurar credencial nunca é a direção barata.
17. **Teste que só vale 23 horas por dia é teste quebrado.** Afirmação sobre duração ancorada em relógio de parede tem de ser ancorada no evento (a meia-noite do Pacífico), não em "mais de uma hora".
18. **Observação ganha de previsão, e ganha para sempre.** Todo prazo do ledger foi *deduzido* de mensagem de erro; nenhum foi medido. Chamada que volta limpa mede. Quem faz pergunta tem de aceitar a resposta — inclusive quando ela desmente o plugin.
19. **Só sucesso encurta banimento.** Recusa posterior mais curta nunca desfaz uma mais longa que ainda está de pé: a verdade menor não anula a maior. E identidade fraca não pode soltar chave — só a sonda, que o próprio plugin escolheu pelo nome e devolveu sozinha na lista, tem identidade forte o bastante para decidir soltura.
20. **Dois prazos no mesmo banimento.** O número do host é impressão digital e nunca é ajustado; o número do KAME é o que decide soltura. Fundir os dois compra banimento mais longo pagando com a solta por modelo — e a solta por modelo é o plugin.
21. **Só medição alarga prazo, e só na direção segura.** O único número que o KAME inventa é o que ele não consegue ler em lugar nenhum, e mesmo esse ele só inventa depois de medir duas vezes seguidas. Evidência consecutiva, acusação estreita, teto duplo, nunca em depleção.
22. **Escalonamento só depois da refutação.** Segurar chave por mais tempo só é defensável num plugin que já sabe testar e desfazer os próprios palpites. A ordem inversa — alargar primeiro, aprender a refutar depois — é como isso vira esconde-chave.
23. **Banimento que não foi cumprido não mede nada.** Toda leitura de "o prazo era curto" pressupõe que a chave passou o intervalo inteiro no banco. Se ela respondeu no meio — solta cedo pela sonda ou simplesmente usada — a recusa seguinte é limite novo, e cobrar como batida alargaria banimento por coincidência.
24. **Momento não é duração.** Prazo que é cronômetro se multiplica; prazo que é âncora de relógio se empurra por minutos. E a diferença tem de ser **dita pela origem do número**, nunca adivinhada pela janela — duas origens diferentes usam o mesmo nome de janela.
25. **Frase igual, significado oposto: quem decide é a carga, não a palavra.** Dois provedores mandam a mesma sentença para dizer "espere 21 segundos" e "seu saldo acabou". Espera nomeada ou contador de taxa nomeado desfaz a leitura de depleção; nome de provedor nunca entra na conta. E `unknown` não desfaz nada — ausência de evidência não derruba correspondência.
26. **Fixture escrita de memória testa a memória.** Toda carga de provedor na suíte tem de ser a que o provedor manda **hoje**, verbatim. Uma frase desatualizada deixou quatro versões verdes e inertes contra tráfego real.
27. **Um passo errado no começo apaga tudo o que vem depois.** Correção que mora atrás de uma classificação errada não é correção: é código que nunca executa. Toda versão nova precisa de pelo menos uma prova ponta a ponta com carga real, do payload cru até a pool.
28. **O mesmo nome tem de ser lido do mesmo jeito onde quer que chegue.** Header e corpo não são fontes diferentes de informação, são lugares diferentes onde o provedor põe a mesma coisa. Onde as duas leituras existem, elas compartilham o padrão — repetir é convite ao desvio, e o desvio é o defeito.
29. **Guarda que nada derruba não entra.** Se a auditoria de desativação derruba zero testes, ou falta teste ou a guarda é inerte. E se a única carga que testaria seria escrita de memória, a resposta é reverter a guarda, não inventar a carga.
30. **Ordem de força escolhe uma leitura; não justifica preferir palpite a número declarado.** Quando a leitura mais forte é descartada, o que entra no lugar tem de ser a próxima coisa que o *provedor* disse — o default da casa é o último recurso, não o segundo.
31. **A marca de ordem de byte não é parte de nada.** Entrada de arquivo decodifica pela marca do próprio arquivo, e token colado tem a marca retirada. Fato de plataforma, verificado escrevendo os arquivos — não deduzido.
32. **A origem registrada é de onde veio o número que sobreviveu.** Nunca de onde veio o número descartado. Vale para a âncora (v0.1.2), para o log do reconcile (v0.1.3) e para o default de janela (v0.1.5): é sempre o mesmo erro, num log diferente.
33. **Relatório conta do jeito que o consumidor conta.** Se o host pula uma linha antes de olhar qualquer status, essa linha não é chave — nem na contagem, nem no rótulo. Contar linha de armazenamento como capacidade é responder "tenho chave?" com um sim que não é verdade.
34. **Campo guardado e propriedade calculada não são a mesma coisa.** Ler o que está no disco quando o host roda no que é computado erra nas duas direções: mostra morto como vivo e vivo como em branco. Onde o host tem uma propriedade, ela é a fonte.
35. **O formato que o usuário já usa é requisito, não preferência.** Uma lista de chaves separada por vírgula é o que o Agent Zero aceita e o que o `/kame-keys add` aceita. Software que a recusa em silêncio não está sendo estrito — está perdendo chave. E quando o host não divide, quem divide é o plugin: a alternativa é devolver tarefa para o usuário.
36. **Derivado não se grava.** Dado recomputável na carga não ganha cópia em disco: a cópia não acompanha a edição da origem e vira segunda verdade. E quando o derivado é segredo, a guarda que o mantém fora do disco é o que **autoriza** o recurso — sem ela, o recurso não liga.
37. **Identidade de uma chave é a chave.** Nunca a posição dela numa lista. Id posicional faz apagar um item renomear todos os seguintes, e toda memória por id é perdida em silêncio.
38. **Teste que eu escrevi não é evidência sobre o mundo.** É evidência sobre o que eu quis. Onde o host tem corpus próprio, ele roda: escrito por quem nunca viu este plugin, e é ele que acha a palavra larga demais. Duas vezes já — a frase que o Google não manda (v0.1.3) e o `unauthorized` que não é chave morta (v0.1.9).
39. **Hook que roda antes do pipeline do host tem que declinar por padrão.** Vencer primeiro significa substituir tudo que vem depois. A pergunta certa nunca é "consigo classificar isso?", é "o host classificaria isso pior do que eu?" — e a resposta quase sempre é não.
40. **Registro com menos valores que a realidade esconde exatamente o caso ruim.** Três coisas podiam acontecer com um prazo e o campo tinha dois valores — e o valor que sobrava era justamente "o KAME calculou e ninguém guardou", a assinatura de plugin inerte, indistinguível de "o KAME ficou de fora". Antes de escolher o domínio de um campo: enumerar os desfechos e conferir qual deles some.
41. **Dá para perguntar ao provedor.** Uma chave obviamente falsa produz a carga de erro real, sem credencial e sem cota — foi assim que se soube que dois de seis provedores escrevem *key is invalid* e não *invalid key*. Fixture reconstruída de formato documentado é a última coisa a se aceitar quando um pedido responde a pergunta.
42. **Comparação que nunca acusa nada não é prova — é silêncio com formato de prova.** Harness que compara "com" e "sem" imprime a mesma linha boa quando o plugin é inofensivo e quando o próprio harness está inerte. Toda comparação dessas termina quebrando o alvo de propósito e exigindo que o outro lado reclame. Sem essa fase, o que se mediu foi a própria capacidade de medir — e ela não foi medida.

43. **Divergência esperada tem nome, motivo e interruptor — ou é regressão não percebida.** Quando um recurso substitui um default do host de propósito, o teste do host que afirma esse default vai falhar, e a única forma honesta de conviver com isso é listar cada teste pelo nome, exigir que ele falhe **com** o recurso e passe **sem** ele, e reprovar nos dois desvios: falhar dos dois jeitos é outra coisa quebrada; passar dos dois jeitos é o recurso morto sem ninguém notar. Lista aberta de "falhas conhecidas" sem essa dupla exigência é permissão para a próxima regressão passar.
44. **O contador é da chave, não da linha.** O provedor mede a chave. Id de pool nomeia linha, e as duas se descolam nas duas direções — duas linhas com a mesma chave, uma linha com chaves diferentes ao longo do tempo. Contar por linha martela um contador do provedor no dobro do ritmo num caso e dá crédito de chave gasta para chave nova no outro. Extensão direta da decisão 37: identidade de uma chave é a chave, em toda memória que fale sobre ela.
45. **Peça que não foi portada tem de ter a linha do host como motivo.** "Não se aplica" dito por mim é esquecimento com roupa de decisão. As duas peças do Agent Zero que ficaram de fora na v0.2.7 apontam para o arquivo e a linha em que o Hermes diz que não faz aquilo — e se a linha mudar numa versão futura, a decisão volta para a mesa em vez de continuar valendo por inércia.
46. **Recurso que não dá para ver não dá para confiar.** Não é polimento: enquanto o espalhamento era invisível, a única evidência de que ele funcionava eram os testes que eu mesmo escrevi (decisão 38). O número que o relatório mostra tem de ser o **mesmo** que a decisão usou — mesma janela, mesma poda, mesmo cadeado —, senão a tela vira uma segunda verdade sobre o que o plugin fez.

47. **Recusar em silêncio é indistinguível de estar quebrado.** Todo caminho que o plugin toma por decisão de projeto — e declinar é o mais comum deles — precisa de uma contagem que o usuário possa ver, senão a versão inerte e a versão correta produzem a mesma tela. A contagem é de números, nunca do texto que a originou: o que torna a contagem segura de exibir é o que ela **não** guarda.

48. **Número que prova não é o mesmo número que decide.** A janela de sessenta segundos é a certa para escolher chave e a errada para o usuário conferir se a rotação existe. Quando as duas perguntas não coincidem, o relatório mostra as duas contagens — e a que serve para conferir fica proibida de entrar na decisão, com teste que falha se entrar.

49. **Sucesso não é prova quando a chamada voltou vazia.** Todo o mecanismo de soltura da v0.0.9 se apoia numa premissa: chamada que voltou produziu alguma coisa. Chave espremida em free tier quebra a premissa devolvendo 200 sem conteúdo, e acreditar nisso aposenta um banimento de vez em cima de evento que não provou nada. A regra é **não contar**, não "contar como falha": ninguém recusou nada, e prazo que provedor nenhum disse é pior que prazo nenhum.

50. **Campo ausente não é campo zerado.** Quando a decisão depende de número que o host informa, a leitura tem três estados e não dois — presente-e-zero, presente-e-diferente-de-zero, e ausente. Ler ausente como zero fez o caminho mais perigoso virar o default silencioso: uma release do Hermes que parasse de mandar o campo desligaria a soltura de todo provedor de uma vez. Desconhecido continua desconhecido, e o comportamento cai no da versão anterior.

53. **Port que não consegue chegar ao efeito é pior que port nenhum.** A rotação por resposta vazia do A0 tinha lugar óbvio no `_spread`, passaria em todo teste e não mudaria uma chamada, porque o host não relê a pool na retentativa. Antes de portar uma peça, o caminho até o efeito tem de ser percorrido no código do host — não o caminho até o gancho.
54. **Citação de linha alheia envelhece; checagem não.** A decisão 45 mandava citar a linha do host ao não portar algo. `tools/host_assumptions.py` transforma cada citação em asserção contra o Hermes instalado, então uma porta que se abra no host vira falha visível em vez de decisão esquecida.

52. **Botão que ninguém acha é botão que ninguém tem — e a saída de emergência fica por cima de tudo.** Configuração vai onde o host guarda configuração, senão o recurso existe só para quem leu o código. Mas a ordem das fontes não é gosto: o interruptor que serve para *desconfiar do plugin* tem de ser acionável de fora do plugin e de fora de qualquer arquivo que o plugin leia. Ambiente primeiro, config depois, default por último.

51. **Tabela de paridade envelhece igual a código.** Três linhas do §4 ainda diziam "não" sobre peças portadas há quatro versões, e é exatamente a tabela que alguém lê para decidir se falta alguma coisa. Auditoria de paridade é releitura do engine de origem contra o código de hoje, não releitura da tabela — e a mesma auditoria achou a v0.3.1 inteira.

## 8. E o KAME do Agent Zero?

A pergunta apareceu no meio da v0.0.3 e a resposta é **sim, mas por caminho estreito.**

O que a v0.0.3 tem e o engine do A0 não tem hoje:

| Ganho | Onde falta no engine |
|---|---|
| Corpo estruturado como fonte de retry | `_extract_retry_delay` para nas 4 fontes; não percorre o corpo |
| `Retry-After` como HTTP-date | só trata o header como número |
| Header de reset casado por forma | não lê `x-ratelimit-reset-*` nem `anthropic-ratelimit-*-reset` |
| Epoch em segundo/milissegundo | não |
| Janela de semana e de mês | `_DAILY_LIMIT_INDICATORS` para em diário/conta |
| Meia-noite Pacific por impressão digital do corpo | o engine decide por nome de provider |

Somou-se a isso, da v0.0.6:

| Ganho | Onde falta no engine |
|---|---|
| `core/journal.py` inteiro | o engine tem contador de falhas consecutivas por chave, mas **não guarda o que previu**. Nunca sabe se o cooldown que aplicou foi curto ou longo demais — só que a chave falhou de novo |
| Par bloco↔recuperação | o `_mark_key_health` zera no sucesso e perde a informação. Zerar é a ação certa; **jogar fora a medição não é** |
| Ledger `(credencial, modelo)` | o engine tem `_get_identity_state` por `provider:model`, mas não cruza com a credencial |

E, da v0.1.0:

| Ganho | Onde falta no engine |
|---|---|
| Alargar prazo por **medição** e não por contagem de falha | o engine alarga o backoff por falhas consecutivas, o que também dispara quando o prazo estava certo e a chave só continua gasta. Aqui a batida só conta quando a chave volta *no prazo* e é recusada em minutos — que é a única leitura que acusa o prazo, e não a cota |
| Dois prazos no mesmo banimento | o engine não precisa disso hoje porque escolhe a chave ele mesmo; se um dia embrulhar pool de terceiro, precisa |
| Prazo alargado continua sendo testável e refutável | o backoff alargado do engine só encolhe zerando no sucesso; nada o testa de propósito quando não há alternativa |

O que **não** deve ser portado de volta: nada da forma do plugin. O engine do A0 tem escalonamento adaptativo por chave, anti-dogpile e escolha de melhor chave por RPM — e ali o A0 é mais capaz, porque escolhe a chave ele mesmo. Trocar o motor dele pelo core daqui seria downgrade.

Correção sobre o que este documento afirmou na v0.0.3: *"o payload do hook traz `provider` e `model`, nunca qual credencial falhou"* continua verdade sobre o **hook**, e deixou de ser verdade sobre o **plugin**. O binding vê a entrada resolvida na falha (`_mark_exhausted`) e a escolhida no sucesso (`_select_unlocked`). O escalonamento por chave, portanto, estava destravado — e a v0.1.0 escreveu.

E, da v0.1.3 — **o melhor candidato a porte de volta**:

| Ganho | Onde falta no engine |
|---|---|
| Estrangulamento distinguido de depleção pela evidência da carga | o `_is_permanent_denial` do A0 escapa dessa frase **por acidente, não por projeto**: ele não trata `billing` como classe separada. Basta alguém apertar essa classificação para o mesmo bug nascer lá |
| `Duration` do protobuf lida como mensagem JSON | o engine só lê o `retryDelay` na forma de texto (`"21s"`); SDK de GenAI que renderize `{"seconds": 21}` passa batido |
| Reset lido igual no header e no corpo (v0.1.4) | o engine lê corpo por caminho fixo; header de rate limit dentro do corpo do OpenRouter não é visto por nenhum dos dois lados |
| Segunda leitura em janela longa (v0.1.5) | o engine para na primeira fonte que responde; cota diária com header de minuto junto cai no TTL fixo dele igual |

O caminho estreito para o A0, em duas partes:

1. `core/quota.py` não importa nada de host nenhum, de propósito. Dá pra colocar como módulo dentro do `04_plugins/kame/agent-zero/current/` e fazer `_extract_retry_delay` delegar pra `extract_retry_delay_seconds`, mantendo intactos `_mark_key_health` e o resto.
2. `core/journal.py` e `core/report.py` entram do mesmo jeito, e o `_mark_key_health` passa a chamar `record_block`/`record_success` **além** de fazer o que já faz. Zero mudança de comportamento, e o A0 passa a saber o que errou.

Não fazer agora. Isso é atualização do KAME v1.0.10 no A0, com o protocolo de upgrade próprio dele (`tools/a0_upgrade_check.py` + `COMPATIBILITY.md`), e misturar com a entrega do Hermes atrapalha as duas. Registrado aqui para não se perder.

## 9. Definição de pronto

Registrada porque foi violada três vezes antes de ser escrita: **teste verde não é pronto.**

O que a v0.3.3 já tem (dez provas):

| Prova | O que ela cobre | Estado |
|---|---|---|
| `pytest tests/ -q` | as regras contra uma pool de mentira | 922/922 |
| `hermes plugins doctor ./hermes-kame-api-rotation --ci` | manifesto, import e registro contra os contratos reais, **sem instalar** | `0.3.3 (standalone)`, `3 hook(s)` |
| `tools/sandbox_binding.py` | os bindings contra o Hermes **real**, em `HERMES_HOME` descartável | 22/22 seções |
| `tools/host_corpus.py` | o corpus de erro do **próprio Hermes**, ~14 provedores, com e sem o KAME atrás do despacho real | 89/89 idênticos |
| `tools/host_pool_suite.py` | as suítes de pool do **próprio Hermes**, 14 arquivos, com e sem o binding na classe real — mais uma fase que quebra o binding de propósito | idênticos; 58 testes caem quando quebrado |
| `tools/live_429.py` | um 429 de verdade saindo do socket, pelo SDK real, pelo classificador real, ate a pausa na pool real — mais uma fase com o KAME desligado | 9 fases: 21s, meia-noite Pacific, lane auxiliar, saída de emergência |
| `tools/live_multikey.py` | um campo de provedor com várias chaves, pelo carregador do próprio Hermes, ate a rotação em recusas reais — mais uma fase com o binding removido | 8 fases: 3 credenciais, 1/3→2/3→3/3, disco intacto, id estável, 15 chaves percorridas |
| `tools/verify_installed.py` | o plugin **instalado**, dentro de processo Hermes real com o gerenciador real | 7 fases: 3 hooks, 8 embrulhos, 2 comandos, diretório de estado, as 2 chaves de config |
| `tools/host_assumptions.py` | os fatos do host em que as **não**-decisões do KAME se apoiam — cada citação da decisão 45 virada em asserção contra o Hermes instalado | 9 fatos, 4 autossabotagens a cada execução |
| `tools/deploy.py` | copia pro install e confere que o manifesto que chegou é o que saiu | 23 arquivos, 0.3.3 |

O alvo do `doctor` é obrigatório na prática. O padrão é `.`, e apontado para uma árvore grande — esta workspace, ou o próprio install do Hermes com a venv — a varredura não termina, sem imprimir nada enquanto roda. Custou uma hora nesta versão: silêncio total é fácil de ler como "a última mudança travou o host", e não era.

A v0.1.3 acrescentou uma prova de outra natureza: `TestTheRealGooglePayloadEndToEnd` roda o `classify()` de verdade sobre o payload verbatim do Google e joga o veredito real dentro do binding — e a v0.1.4 fez o mesmo para o OpenRouter (`TestTheRealOpenRouterPayloadEndToEnd`). As outras afirmam o que deve acontecer **dado** um veredito certo; essa afirma que o veredito é certo. Foi a que faltou por quatro versões.

E a v0.1.9 acrescentou a única cujos payloads não saíram daqui (`host_corpus.py`, seção 6.10.9). A diferença entre ela e todas as anteriores: as outras perguntam se o plugin faz o que eu quis; essa pergunta se o que eu quis atrapalha quem já estava certo. Achou dois casos em que atrapalhava.

A v0.2.2 fez o mesmo pela metade que mexe na pool (`host_pool_suite.py`, seção 6.10.12) e acrescentou o que faltava em todas as comparações anteriores: uma fase que quebra o alvo de propósito, para que "não mudou nada" seja resultado e não silêncio.

A v0.2.3 fechou o buraco que esta seção nomeava desde o começo (`live_429.py`, seção 6.10.13). O 429 atravessa: socket, SDK, classificador, pool, pausa por modelo e relatório. O `/kame-quota` deixou de dizer *"nothing recorded yet"* e passou a dizer `1 block(s), 1 sized by KAME, held 12h 59m`.

A v0.3.3 acrescentou a décima, e é a de natureza mais estranha: `host_assumptions.py` não checa nada que o KAME faz. Checa o que ele **decidiu não fazer**, e por quê. Cada peça do engine do A0 que ficou de fora tem uma citação de linha do Hermes explicando que o port não teria efeito ou seria errado — e citação é afirmação sobre código dos outros. A ferramenta lê o Hermes instalado e confere as seis. Se o laço de resposta vazia um dia passar a reler a pool, ela falha, e a decisão volta pra mesa em vez de continuar verdadeira só no documento.

O que falta agora é uma coisa só, e é menor do que era: **a recusa do provedor em uso normal.** A carga que sai daquele socket é captura verbatim, não cota acabando de verdade — e captura é do mesmo tipo de evidência que já esteve errada aqui (v0.1.3, v0.2.1). O encadeamento está provado; o que nenhuma máquina prova por mim é o dia em que a chave do usuário acaba de verdade e o plugin resolve sozinho, calado.
