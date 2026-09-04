# 📌 KAME for Hermes — Backlog & Issue Tracker

> Open items only. Anything shipped moves to `CHANGELOG.md` and is deleted from
> here, so the length of this file is the size of the backlog.

## Open

### 🔲 O journal não enxerga o caminho principal — medido em 1.6.0.2, documentado em 1.6.0.3

- **Medida (`tools/inspect_run.py`, build `2e3270b639b9`, 03/09/2026):**
  `rotations since this build started: rate_limit 74, server 6` e
  `journal: 110 refusal(s) kept, 0 since this build started`. Oitenta recusas
  reais, zero linhas.
- **Causa:** há dois caminhos de recusa e o journal só vê um.
  `pool_binding._remember` (`:969`) começa com
  `if updated.last_status != STATUS_EXHAUSTED: return`, então só grava o que o
  **host** benchou. As rotações que o `dispatch_binding` faz dentro do turno
  marcam o carrossel direto e nunca chegam lá.
- **Consequência:** `_short_streak` devolve 0 para esse tráfego, logo
  `escalate.stretch` — a única coisa autorizada a alargar um castigo por
  evidência — nunca dispara nele. O relatório do `/kame doctor` também fica
  vazio para o caminho onde tudo acontece.
- **Por que NÃO foi consertado na 1.6.0.3:** ligar um segundo ponto de escrita
  exige deduplicação (`record_block` acrescenta sem checar nada) e a lógica de
  "o castigo chegou a ser cumprido" foi escrita para benches do host. Isso é
  escopo novo numa versão que precisa ser final, num mecanismo que é *rede de
  segurança*, não o caminho de correção.
- **O que foi feito em vez disso:** a 1.6.0.3 parou de depender dele. Todo
  descanso agora é o número que o provider declarou, ou o que ele declarou
  antes para o mesmo `provider:model`, ou um re-teste plano — nenhum é uma
  curva inventada que precise ser corrigida depois. Os comentários que
  afirmavam a rede de segurança foram corrigidos para dizer o que é verdade.
- **Quando consertar:** junto com dedupe por `(credential_id, model, ~at)`, e
  só com um teste que prove que uma recusa que passa pelos dois caminhos vira
  uma linha, não duas.

### 🔲 Aux lane não tenta de novo antes de cair pro fallback provider-wide — planejado pra 1.5.1
- **Sintoma (visto em produção, 30/08/2026, `agent.log` 20:18:56):** pool
  Gemini do aux (`skills_hub`, modelo `gemini-3.5-flash-lite`) ficou sem
  nenhuma chave saudável no momento da chamada. `auxiliary_client` do próprio
  Hermes caiu pro fallback provider-wide (OpenRouter → Nous) na hora, sem
  tentar de novo dentro do pool Gemini. Os dois fallbacks também falharam
  (OpenRouter com modelo fora de `:free`, Nous sem `hermes auth`), então a
  chamada de aux morreu de vez — nesse caso específico, o roteador de skill
  (`skills_hub`) não decidiu direito qual skill carregar, e a resposta saiu
  sem o formato aprovado (`central-inteligencia`).
- **Causa:** `aux_binding.py` só anuncia provider+model pro pool scope
  (`runtime.scoped_call`) — quem decide desistir ou tentar de novo é o
  `auxiliary_client` do host, chamando `mark_exhausted_and_rotate` uma vez e
  seguindo pro fallback configurado. KAME não intercepta esse "desistiu cedo
  demais".
- **Proposta pra 1.5.1:** dentro do `scoped_call` do aux, se o pool anunciado
  não tiver nenhuma chave saudável no instante da chamada, esperar o menor
  cooldown do pool e tentar de novo (pelo menos 1 retry) antes de deixar a
  chamada cair pro fallback provider-wide do host. Justamente o KAME existe
  pra rotacionar em vez de desistir — o aux devia ganhar o mesmo tratamento
  que o modelo principal já tem.
- **Nenhum fix aplicado.** Usuário não usa OpenRouter (sem key configurada
  pra esse provedor) — mexer no SKU do fallback não muda nada de verdade.
  Fica só a proposta de retry acima pro 1.5.1.

---

## Closed

### ✅ Gemini streaming read timeout — shipped in 1.2.1
- **Sintoma:** `Gemini streaming request failed: The read operation timed out`
  durante requisição longa em stream, terminando o turno.
- **Resolvido em 1.2.1:** `core/carousel.py` ganhou `TIMEOUT_INDICATORS`, lidas
  tanto por `is_terminal` (que passa a devolver `False`) quanto por `classify`
  (que devolve `TIMEOUT_S, "timeout"`). Um read timeout embrulhado pela SDK —
  sem exceção `TimeoutError` para reconhecer — agora roda a chave em vez de
  propagar o erro.

### ✅ Pool nunca esquecia uma chave — shipped in 1.2.2
- **Sintoma:** pool da NVIDIA com 2 chaves reportava 3, sendo a terceira a
  própria lista `k1,k2` mandada inteira ao provedor (403), e as duas reais
  quarentenadas em 401.
- **Resolvido em 1.2.2:** `candidates()` divide todo valor bruto antes de
  qualquer envio e deduplica por texto da chave; `Carousel.select` espelha a
  lista de candidatos e aposenta a linha que ninguém oferece há
  `MIRROR_GRACE_S`.

### ✅ Suíte falhava 1 em ~2 runs completos — shipped in 1.2.2 (só teste)
- **Sintoma:** `test_v1_1_3.py::TestASingleKeyContinuesItsAnswerAtOnce::
  test_a_pool_with_company_still_rests_the_key_that_cut` falhava em
  `assert slept == []` num run completo e passava no outro, com o mesmo código.
  Nunca falhava sozinho.
- **Causa:** os dois testes gravavam com
  `monkeypatch.setattr(dispatch_binding.time, "sleep", ...)`, e
  `dispatch_binding.time` *é* o módulo `time` do stdlib — o patch valia para o
  interpretador inteiro. `register()` sobe `_start_state_heartbeat()`, uma
  daemon `while True: time.sleep(_CONTROL_TICK_S)` que nunca morre; com
  `test_plugin.py` e `test_v1_0_9.py` carregando o plugin sob nomes de pacote
  diferentes, o processo terminava com **duas** dessas threads vivas. Bastava
  uma delas alcançar o `sleep` dentro da janela do patch para o gravador
  registrar um tique que o KAME não pediu.
- **Resolvido em 1.2.2:** os testes passam a usar `DispatchBinding(sleep=...)`,
  que é o ponto de injeção que o próprio binding já expunha desde 1.0.1.
  Nada do código embarcado mudou: em produção `register()` roda uma vez, com
  uma instância do módulo, e uma thread só.

### ✅ Painel piscava o valor ao salvar — shipped in 1.2.2
- **Sintoma:** salvar um número nas Settings fazia o campo voltar ao valor
  antigo e pular pro novo, lido como "o menu muda sozinho".
- **Resolvido em 1.2.2:** o controle segura o que foi escrito até o backend
  reportar aquele valor. Junto: o painel avisa quando o `config.yaml` foi
  editado depois que o Hermes subiu, que é o caso em que reiniciar é mesmo
  necessário.
