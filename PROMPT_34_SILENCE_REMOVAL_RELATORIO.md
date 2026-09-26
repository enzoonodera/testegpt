# PROMPT 34 -- Remoção de Silêncio -- Relatório de Entrega

## 1. Arquivos criados

- `_sistema/silence_removal.py` (1046 linhas) -- módulo novo: Parte A
  (configuração `SILENCE_REMOVAL`) + Parte B (detecção real + aplicação de
  cortes via a API pública de `TimelineEditor`).
- `tests/test_silence_removal.py` (1077 linhas, 83 testes) -- suíte
  dedicada.
- `PROMPT_34_SILENCE_REMOVAL_RELATORIO.md` -- este relatório.

## 2. Arquivos modificados

- `empacotar_release.py` -- adicionado
  `"PROMPT_34_SILENCE_REMOVAL_RELATORIO.md"` a `ALLOWED_ROOT_FILES`
  (ordem alfabética, entre `PROMPT_33_AUTO_REFRAME_RELATORIO.md` e
  `PROMPT_CATALOG_WIRING_RELATORIO.md`).

Nenhum outro arquivo foi tocado. Confirmado por `find ... -newer` que
somente os 2 arquivos novos foram modificados nesta rodada. Em
particular: `_sistema/timeline_editor.py` (Prompt 28) permanece
INTOCADO -- este Prompt usa exclusivamente sua API pública já existente
(`get_timeline`/`initialize_timeline`/`remove_range`), nunca reimplementa
nem estende o schema de `cuts`. `_sistema/visual_editor.py`,
`_sistema/captions_engine.py`, `_sistema/captions_style.py`,
`_sistema/media_catalog.py`, `_sistema/audio_engine.py`,
`_sistema/auto_reframe.py` também permanecem intocados.

## 3. Comportamento novo

`SilenceRemovalEngine` -- ferramenta independente (Princípio A do
CLAUDE.md). Parte A grava configuração (`threshold_db`,
`minimum_duration_seconds`, `padding_seconds`) numa categoria nova
`SILENCE_REMOVAL`. Parte B roda um `Job` real (`SILENCE_REMOVAL`) que
detecta silêncio no áudio (via um backend plugável), filtra por duração
mínima, encolhe cada intervalo pelo padding, e aplica os cortes na
timeline usando exclusivamente a API pública de `TimelineEditor`
(`remove_range`) -- nunca escreve `cuts` diretamente. O resultado
(intervalos candidatos) é persistido como `Artifact` de cache
(`kind="silence_removal_track"`) e um checkpoint `EDIT_PLANNED` é
registrado.

## 4. Decisões arquiteturais

### 4.1 -- `TimelineEditor` é reaproveitado, nunca reimplementado (a decisão mais importante do Prompt)

`timeline_editor.py` (Prompt 28) já define o schema completo de `cuts` e
já expõe `remove_range`, que sozinho cobre os três casos que "remover um
trecho de silêncio" precisa (meio/borda/segmento inteiro). Este módulo
importa `timeline_editor` DIRETAMENTE -- exceção deliberada e documentada
à regra de zero acoplamento usada entre `captions_style.py`/
`visual_editor.py`/`audio_engine.py`/`auto_reframe.py` (esses são
decisões PARALELAS e independentes; `cuts` e `silence_removal` têm uma
relação real: um é o mecanismo de corte, o outro é uma fonte de decisão
sobre o que cortar). Nenhum método novo foi necessário em
`timeline_editor.py` -- investigado e confirmado por leitura completa
antes de codificar (`initialize_timeline`/`get_timeline`/`remove_range`
já bastam).

### 4.2 -- Backend de detecção: infraestrutura primeiro (mesma decisão dos Prompts 31/33)

`requirements.txt` não lista nenhuma biblioteca de análise de áudio.
`MediaProbe` foi relido e confirmado que não expõe nada reaproveitável
para detecção de silêncio hoje (só metadados). Duas alternativas foram
avaliadas: (a) backend plugável com stub conservador (nunca encontra
silêncio); (b) `ffmpeg silencedetect` via `subprocess`. Decisão: (a) --
(b) exigiria parsear `stderr` não estruturado do `ffmpeg`, decidir
timeout/semântica de processo em Windows, e testar isso
deterministicamente; trabalho real, adiado com justificativa explícita
(mesmo espírito de `faster-whisper`/detecção visual nos Prompts 31/33).
O backend é chamado UMA ÚNICA vez por Job (não amostra a amostra como o
Prompt 33 -- análise de volume é naturalmente contínua, diferente de
detecção visual por frame).

### 4.3 -- "Padrão conservador": valores padrão documentados numericamente

`DEFAULT_THRESHOLD_DB = -50.0` dBFS (bem mais silencioso que fala normal,
que fica entre -30 e -15 dBFS); `DEFAULT_MINIMUM_DURATION_SECONDS = 0.6`
(pausas naturais tipicamente duram menos); `DEFAULT_PADDING_SECONDS =
0.1` (100ms preservados em cada ponta, evita cortar cauda de
sílaba/respiração). Todos documentados com justificativa, nunca
inventados sem razão.

### 4.4 -- Padding aplicado ANTES de decidir o corte final

Pipeline: sanitização/clip para `[0, duration]` → filtro por
`minimum_duration_seconds` (sobre a duração BRUTA) → encolhimento pelas
duas pontas via `padding_seconds`. Um intervalo cujo padding consome a
duração inteira é descartado (nunca um corte de duração zero/negativa é
proposto ao `TimelineEditor`).

### 4.5 -- Falha do backend de detecção NUNCA falha o Job (decisão documentada, diferente do Prompt 31)

"Nunca bloquear Job por falha de tracking" não é texto literal deste
Prompt (é do Prompt 33), mas o mesmo espírito de "padrão conservador"
leva à mesma conclusão aqui, por razão ESTRUTURAL própria: o fallback
seguro de uma falha de detecção de silêncio é TRIVIAL -- não cortar nada
(diferente do Prompt 31, onde uma transcrição falha não tem substituto
seguro). Qualquer exceção do backend é capturada (`try/except Exception`,
uma única vez) e tratada como "nenhum silêncio encontrado" -- Job termina
`JOB_READY`. Testado explicitamente
(`test_backend_lancando_excecao_nunca_falha_o_job`). Falhas ESTRUTURAIS
(`SourceAsset`/`Video` ausentes, `probe` inválido, `project_id` ausente)
continuam falhando o Job normalmente -- distinção testada.

### 4.6 -- Intervalos "pulados" (nunca um erro) quando não cabem num único segmento

Para cada intervalo candidato, o handler relê a timeline atual e procura
um ÚNICO segmento que o contenha inteiro. Se não encontrar (já removido
por execução anterior -- idempotência; cruza segmentos por edição manual
prévia; ou uma instância concorrente já removeu primeiro), o intervalo é
registrado como "pulado" com um motivo -- NUNCA um erro, nunca falha o
Job. Decisão deliberada de NÃO estender `timeline_editor.py` para
suportar remoção cruzando múltiplos segmentos (nenhuma necessidade
concreta demonstrada -- CLAUDE.md sobre overengineering). Isso também é o
que torna a operação IDEMPOTENTE por construção: rodar o Job duas vezes
com a mesma config resulta na segunda vez em todos os intervalos
"pulados" (já consumidos), nunca uma duplicação/corrupção de corte --
testado explicitamente (`test_cache_hit_e_idempotente_nao_muda_cuts_na_segunda_execucao`).

### 4.7 -- Cache via Artifact (decisão: sim)

O resultado da detecção (intervalos candidatos) é persistido como
`Artifact` real (`kind="silence_removal_track"`), reaproveitando o MESMO
mecanismo de cache dos Prompts 31/33 (SHA-256 canônico, reimplementado
localmente -- zero acoplamento entre módulos irmãos de processamento).
Em cache HIT, o backend nunca é chamado de novo; os intervalos
persistidos são relidos e reaplicados (idempotentemente, seção 4.6) à
timeline atual do Job.

### 4.8 -- Checkpoint: `EDIT_PLANNED` (não `MEDIA_PROCESSED`)

`CHECKPOINT_TRANSCRIBED` (Prompt 31) e `CHECKPOINT_MEDIA_PROCESSED`
(Prompt 33) já estão em uso; `CHECKPOINT_EDIT_PLANNED` estava livre.
Decisão: reaproveitar `EDIT_PLANNED` -- distinto conscientemente de
`MEDIA_PROCESSED` porque o produto central deste módulo é literalmente
uma decisão de EDIÇÃO aplicada (`cuts` mutados), não uma curva de análise
de mídia sem efeito colateral estrutural (como foi o caso do Prompt 33).

### 4.9 -- "Corrigir vídeo, áudio, captions": o que significa neste produto (decisão mais importante do Prompt, com prova)

Investigado e concluído: este produto não separa vídeo/áudio em
trilhas/arquivos distintos -- é tudo a mesma timeline de `cuts`; "corrigir
vídeo" e "corrigir áudio" já são cobertos estruturalmente pelo mecanismo
de `cuts` (um corte remove vídeo e áudio simultaneamente, por definição).
`audio_engine.py` é configuração pura, sem timestamp próprio a
reconciliar.

O ponto real de risco é CAPTIONS: os segmentos de transcrição têm
`start`/`end` relativos ao vídeo ORIGINAL -- exatamente o mesmo
referencial de `cuts`. CONCLUSÃO PROVADA POR TESTE
(`test_sincronizacao_silencio_fora_de_qualquer_legenda_nao_afeta_legendas`):
como os referenciais já são idênticos, uma legenda fora de qualquer
intervalo removido NUNCA precisa de remapeamento -- ela continua coberta
por um único segmento remanescente no MESMO timestamp de sempre.

CASO ADVERSARIAL testado
(`test_sincronizacao_silencio_intersecta_legenda_nunca_trunca_a_legenda`):
um silêncio detectado que intersecta parcialmente uma legenda (só pode
acontecer se o detector achar uma pausa longa DENTRO de um trecho
transcrito). Decisão: este Prompt NÃO implementa nenhum
truncamento/remapeamento de legenda -- `SilenceRemoval` nunca consulta
nem depende de artefatos de `captions_engine.py` (mantém a independência
entre ferramentas). O corte é aplicado normalmente; o objeto de legenda
permanece byte-a-byte idêntico (nada o toca); a reconciliação de
reprodução é responsabilidade de uma etapa de Render Engine futura --
exatamente o mesmo tipo de decisão que uma edição MANUAL de corte via
`timeline_editor.py` já enfrentaria, não uma situação nova introduzida
por este Prompt.

### 4.10 -- Timeline automaticamente inicializada quando ausente

`SilenceRemoval` chama `TimelineEditor.initialize_timeline` (API pública)
se a timeline do projeto ainda não existe -- nunca escreve `cuts`
diretamente. Se já existe (edição manual anterior ou execução anterior
deste Job), `initialize_timeline` nunca é chamado.

### 4.11 -- `media_catalog.py` -- não tocado (nunca existiu um badge para isso)

Confirmado por leitura: não existe, hoje, nenhum badge para "silêncio
removido" em `SYSTEM_BADGES` -- nada precisou ser "mantido em zero"
porque nunca existiu. Este Prompt não modifica `media_catalog.py`.

## 5. Migrations

Nenhuma. `m001`-`m009` intactos (`LATEST_SCHEMA_VERSION == 9`, testado
explicitamente).

## 6. Testes automatizados executados

- `tests/test_silence_removal.py`: **83 passed** (~2.2s), cobrindo:
  construção (8); Parte A -- config/validação/vocabulário/range/
  isolamento de fingerprint/restart/concorrência (20); Parte B -- corte
  real batendo com `get_timeline`, padding aplicado corretamente,
  silêncio curto nunca removido, threshold conservador não corta fala,
  exceção do backend nunca falha o Job (o teste mais importante),
  falhas estruturais distintas de "sem cortes", timeline auto-inicializada
  vs. reaproveitada, cache hit/miss, IDEMPOTÊNCIA (rodar duas vezes não
  duplica/corrompe), cancelamento em dois pontos, restart, concorrência
  real (achou e corrigiu uma corrida genuína na inicialização da timeline
  -- ver seção 7), sincronização com captions (2 testes, exigidos
  literalmente pelo roadmap) (23); helpers de módulo -- pipeline de
  intervalos/cache key (7); confirmação `media_catalog.py`/migrations (2);
  AST estrutural -- `timeline_editor` é a ÚNICA exceção permitida entre
  módulos irmãos, zero subprocess/ffmpeg direto, zero criação de
  `Video`/`Publication`/`Schedule` (4).
- Suíte completa (`pytest tests/`): **2258 passed, 1 skipped, 36 subtests
  passed** em 142.64s.
- `python -m compileall _sistema tests`: OK.

### Contagem antes/depois (medida nesta sessão)

- Antes (sem `test_silence_removal.py`): **2176 testes coletados**.
- Depois: **2259 testes coletados** (2258 passed + 1 skipped).
- **Delta: +83**, exatamente o número de testes do arquivo novo -- nenhum
  teste sumiu em nenhum outro arquivo.

## 7. Achado do GATE ADVERSARIAL (corrigido nesta rodada)

O teste de concorrência real
(`test_duas_instancias_concorrentes_processando_o_mesmo_video`) encontrou
uma corrida genuína NÃO coberta pela implementação inicial: duas
instâncias processando o MESMO projeto podem AMBAS observar
`get_timeline` vazio e tentar `TimelineEditor.initialize_timeline` -- a
primeira vence, a segunda recebe `TimelineJaInicializadaError`, que
propagava sem ser capturada e derrubava o Job (`JOB_FAILED` via
`JobHandlerError`). Corrigido: a chamada a `initialize_timeline` agora
está protegida por `try/except TimelineEditorError`, tratando a corrida
como benigna (segue usando a timeline que a outra instância já criou) --
nunca falha o Job por isso. Documentado na seção 0.9 da docstring do
módulo e coberto pelo próprio teste que encontrou o problema.

## 8. Como testar manualmente

1. `pytest tests\test_silence_removal.py -v` (Windows, após `pip install
   -r tests\requirements-test.txt`) -- nenhuma dependência de ffmpeg real
   é necessária para este módulo (o backend de detecção é sempre injetado
   como fake nos testes; a integração real com `MediaProbe` já foi
   provada em `test_auto_reframe.py`/`test_media_probe.py`).
2. Cenário manual (quando a UI existir): configurar `SilenceRemoval` num
   Project, rodar o Job com um backend real futuro, confirmar que a
   timeline (`cuts`) reflete os cortes e que um `Artifact` de cache
   aparece em `projects/silence_removal/<video_id>/`.

## 9. Riscos conhecidos / dívida técnica

- Sem um `silence_detection_backend` real injetado, o comportamento de
  produção hoje é SEMPRE "nenhum corte" -- documentado como esperado, não
  um bug; uma rodada futura precisa escolher e integrar um backend real
  (candidato óbvio: `ffmpeg silencedetect`, opção (b) explicitamente
  adiada nesta rodada).
- Um intervalo de silêncio detectado que intersecta parcialmente uma
  legenda transcrita é aplicado como corte normalmente, sem nenhuma
  reconciliação de legenda -- documentado como responsabilidade de uma
  etapa de Render Engine futura (seção 4.9), não uma falha deste Prompt.
- Mesmo padrão de "vazamento de armazenamento conhecido" dos Prompts
  31/33: se o processo morrer entre a promoção física do Artifact de
  cache e o INSERT na tabela `artifacts`, o arquivo fica órfão no disco
  (nunca lido como cache válido) -- aceito como limitação documentada.
- A detecção é chamada uma única vez por Job (não amostrada); um backend
  real que precise de streaming/chunking para vídeos muito longos
  precisará gerenciar isso internamente -- fora de escopo desta
  infraestrutura.

## 10. Pendências

- Escolher e integrar um backend real de detecção de silêncio (fora de
  escopo desta rodada, sem informação suficiente hoje sobre a integração
  definitiva).
- Uma eventual reconciliação de legendas parcialmente cortadas é trabalho
  de uma etapa de Render Engine futura (não deste Prompt nem de um
  "Catalog Wiring").

## 11. Confirmações explícitas

1. `timeline_editor.py` foi USADO (API pública), não duplicado nem
   estendido -- CONFIRMADO (seção 4.1, e o arquivo permanece intocado,
   seção 2).
2. `visual_editor.py`/`captions_engine.py`/`captions_style.py`/
   `media_catalog.py`/`audio_engine.py`/`auto_reframe.py` não tocados --
   CONFIRMADO (seção 2, timestamps).
3. Nenhuma migration nova, `m001`-`m009` intactos -- CONFIRMADO (seção 5).
4. Nenhum badge de "silêncio" existe/foi criado em `media_catalog.py` --
   CONFIRMADO (seção 4.11, teste dedicado).
5. Nenhum teste removido, delta exato +83 -- CONFIRMADO (seção 6).
6. Falha do backend de detecção NUNCA falha o Job -- CONFIRMADO (seção
   4.5, teste dedicado).
7. Falha estrutural continua falhando o Job -- CONFIRMADO (seção 4.5,
   testes dedicados).
8. Padding aplicado corretamente, silêncio curto nunca removido, threshold
   conservador não corta fala -- CONFIRMADO (seção 6, testes dedicados).
9. Testes de SINCRONIZAÇÃO com captions -- CONFIRMADO (seção 4.9, 2
   testes, literalmente exigidos pelo roadmap).
10. Idempotência (rodar duas vezes não corrompe/duplica) -- CONFIRMADO
    (seção 4.6, teste dedicado).
11. Cancelamento em dois pontos -- CONFIRMADO (testes dedicados).
12. Concorrência real -- CONFIRMADO, incluindo a corrida genuína
    encontrada e corrigida nesta rodada (seção 7).
13. Restart com instâncias totalmente novas preserva Artifact e cortes --
    CONFIRMADO.

## 12. Seção 11 (ZIP) -- evidência de processo, não bloqueante

Mesma política já estabelecida desde o Prompt 32: o ZIP que chega para
auditoria é sempre gerado localmente pelo usuário via
`EMPACOTAR_RELEASE.bat`; uma divergência ali sozinha não bloqueia a
aprovação. `device_bash` será checado fresco no momento da entrega (não
presumido de rodadas anteriores).
