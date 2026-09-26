# PROMPT 33 -- AutoReframe / 9:16 -- Relatório de Entrega

## 1. Arquivos criados

- `_sistema/auto_reframe.py` (914 linhas) -- módulo novo, Parte A
  (configuração REFRAME) + Parte B (detecção/tracking real via
  ``AutoReframeEngine``).
- `tests/test_auto_reframe.py` (932 linhas, 70 testes) -- suíte dedicada.
- `PROMPT_33_AUTO_REFRAME_RELATORIO.md` -- este relatório.

## 2. Arquivos modificados

- `empacotar_release.py` -- adicionado
  `"PROMPT_33_AUTO_REFRAME_RELATORIO.md"` a `ALLOWED_ROOT_FILES` (mesmo
  padrão de toda rodada anterior).

Nenhum outro arquivo foi tocado. Em particular, confirmado por
`find`/timestamps que `_sistema/visual_editor.py`,
`_sistema/captions_engine.py`, `_sistema/captions_style.py`,
`_sistema/media_catalog.py`, `_sistema/audio_engine.py`,
`_sistema/timeline_editor.py` permanecem intactos nesta rodada.

## 3. Comportamento novo

`AutoReframeEngine` opcional: Parte A grava a decisão de habilitar o
reenquadramento automático 9:16 e seus parâmetros (categoria `REFRAME`,
já reservada desde o Prompt 27). Parte B roda um `Job` real
(`AUTO_REFRAME`) que amostra o vídeo, detecta rosto/pessoa/região
relevante via um backend plugável, suaviza a curva resultante e persiste
o resultado como `Artifact` real (`kind="reframe_track"`) + checkpoint
`MEDIA_PROCESSED`. Se o detector não encontrar nada em quantidade
suficiente, ou lançar uma exceção, um fallback seguro (crop central
estático) é usado -- o Job NUNCA falha só por causa disso.

## 4. Decisões arquiteturais

### 4.1 -- Backend de detecção: infraestrutura primeiro, biblioteca real adiada

Confirmado por leitura de `requirements.txt`: nenhuma biblioteca de
visão computacional está listada (nem instalada/instalável no sandbox,
sem acesso de rede). Estruturalmente idêntico ao que o Prompt 31
enfrentou com `faster-whisper`. Decisão (uma das duas alternativas que o
próprio Prompt aceitava): entregar a infraestrutura COMPLETA
(Job/cache/Artifact/fallback/curva/checkpoint) com um backend
(`detection_backend`) plugável, mesmo padrão de `transcription_backend`
do Prompt 31. O backend de PRODUÇÃO padrão (`_default_detect`) é um stub
explícito que sempre devolve `found=False` -- nunca finge detectar algo
sem biblioteca real por trás. Efeito conhecido e documentado: sem um
backend real injetado, o produto hoje sempre cai no fallback seguro
(crop central) -- comportamento correto por construção (nunca inventa
evidência, nunca bloqueia o Job), registrado como pendência para quando
uma biblioteca real (ex.: MediaPipe) for escolhida com informação
suficiente numa rodada futura.

### 4.2 -- Geometria: `MediaProbe.probe()` raso, chamado dentro do handler

`MediaProbe` (Prompt 26) é stateless/sob demanda por design -- chamado
SOMENTE dentro do `JobHandler`, nunca antecipado. `probe()` raso (só
`ffprobe`) é suficiente: a curva só precisa de `width`/`height` reais
para normalizar coordenadas -- `probe_deep` (decodificação completa)
seria custo desnecessário para esse propósito. `probe().valid is False`
é tratado como falha ESTRUTURAL do Job (sem resolução real não há
geometria possível), nunca uma falha de detector.

### 4.3 -- Checkpoint: `MEDIA_PROCESSED` reaproveitado, nenhum novo criado

`domain/checkpoints.py` é vocabulário fechado. Confirmado por grep que
nenhum módulo além de `captions_engine.py` usa `record_checkpoint` hoje
(só `CHECKPOINT_TRANSCRIBED`) -- `CHECKPOINT_MEDIA_PROCESSED` estava
livre e é o encaixe semântico direto ("a curva de tracking foi calculada
e persistida" é literalmente uma etapa de processamento de mídia
concluída). Nenhum checkpoint novo foi necessário nem proposto.

### 4.4 -- A decisão de design mais importante: falha de DETECTOR nunca falha o Job

Diferente do Prompt 31 (onde qualquer falha do backend de transcrição
falha o Job -- a transcrição É o produto, sem substituto seguro), aqui o
roadmap pede explicitamente o oposto para falha de detecção
("nunca bloquear Job por falha de tracking"). Mecanismo: o handler
processa `DETECTION_SAMPLE_COUNT=30` amostras fixas, distribuídas
uniformemente ao longo da duração real do vídeo; uma exceção do backend
em QUALQUER amostra é capturada (`try/except Exception`) e tratada
exatamente como `found=False` para aquela amostra -- nunca propaga. Se a
fração de amostras `found=True` cair abaixo de
`FALLBACK_MIN_FOUND_RATIO=0.5`, o fallback (crop central) é engajado --
mesmo caminho de código para "detector não achou nada" e "detector
lançou exceção em toda amostra", ambos terminando em `JOB_READY` com um
`Artifact` de fallback marcado (`"fallback": true` no JSON). Isso é
DISTINTO de falha estrutural (`SourceAsset`/`Video` ausente, `probe`
inválido) -- verificadas ANTES de qualquer amostra, continuam falhando o
Job normalmente. Testado explicitamente com dois backends fake
diferentes (`_fake_backend_not_found`/`_fake_backend_raising`),
provando o mesmo resultado por caminhos de entrada diferentes.

### 4.5 -- `enabled` ausente/falso: falha estrutural, não fallback

Um `Job AUTO_REFRAME` criado para um Project sem `enabled=True` explícito
falha (`JOB_FAILED`, `reason="reframe_not_enabled"`) -- decisão
documentada: isso é um erro de orquestração/precondição (alguém criou um
Job para uma feature desligada), não uma "falha de detector" -- a regra
"nunca bloquear Job por falha de tracking" é sobre o DETECTOR
especificamente, não sobre pular a checagem de configuração.

### 4.6 -- `project_id` obrigatório (diferente do Prompt 31)

Diferente de `captions_engine.py` (onde a transcrição independe de um
Project), aqui a decisão `enabled`/config vive NO Project -- um Job sem
`project_id` não tem de onde ler essa decisão, então falha
estruturalmente (`reason="job_missing_project_id"`). Diferença
documentada explicitamente na docstring do módulo.

### 4.7 -- Amostragem, prioridade de detecção e suavização

- **Amostragem**: `DETECTION_SAMPLE_COUNT=30`, fixo e uniforme ao longo
  da duração real -- custo previsível por vídeo, independente da duração
  (Princípio C do CLAUDE.md, "alto volume": lote de 500+ vídeos precisa
  de custo previsível por item). Trade-off documentado: vídeos muito
  longos têm cobertura temporal mais esparsa por amostra.
- **Prioridade de detecção** (`rosto > pessoa > região`): constante FIXA
  (`DETECTION_PRIORITY`), NÃO configurável por projeto -- decisão
  documentada de que é uma cascata interna do detector, não um campo de
  projeto (o roadmap não pede que seja configurável).
- **Suavização**: EMA sobre `(cx, cy)` das amostras `found=True`, na
  ordem temporal (amostras não encontradas são puladas, nunca
  interpoladas). `alpha = 1.0 - (smoothing_strength * 0.9)` -- nunca
  exatamente `0` (congelaria a curva, o que não é "suavizar", é
  "ignorar").

### 4.8 -- Chave de cache

SHA-256 canônico (mesmo padrão do Prompt 31, reimplementado, nunca
importado) sobre `{source_fingerprint, target_aspect_ratio,
smoothing_strength, fallback_strategy, detector_backend_version}`.
`detector_backend_version` (`"v1"` hoje) entra na chave -- diferente do
Prompt 31, aqui um backend/versão de detecção diferente pode produzir
resultado materialmente diferente para a mesma config. A prioridade de
detecção (fixa) NÃO entra (nenhuma variação possível hoje); documentado
como pendência para quando/se tornar configurável.

### 4.9 -- Formato/local do arquivo

JSON de keyframes normalizados (`0.0`-`1.0`, reaproveitando a convenção
já usada por `visual_editor.py`/`captions_style.py`), sob
`<paths.projects>/reframe/<video_id>/<cache_key>.json` -- por
`video_id` (não `project_id`), mesmo raciocínio do Prompt 31: a curva é
propriedade do CONTEÚDO visual do vídeo, reaproveitável entre Projects.

## 5. Migrations

Nenhuma. `m001`-`m009` intactos (`LATEST_SCHEMA_VERSION == 9`, testado
explicitamente).

## 6. `media_catalog.py` / `REFRAMED_9_16`

Confirmado explicitamente: `_sistema/media_catalog.py` NÃO foi tocado.
`BADGE_REFRAMED_9_16` continua `_BADGE_SQL_EXPRESSIONS[...] == "0"` após
este Prompt (testado em
`test_reframed_9_16_continua_hardcoded_zero_apos_este_prompt`). Destravar
esse badge é trabalho de uma rodada futura de "Catalog Wiring" dedicada
-- mesmo padrão já usado para `CAPTIONS`/`TRANSCRIBED`.

## 7. Testes automatizados executados

- `tests/test_auto_reframe.py`: **70 passed** (~5.5s), cobrindo:
  construção (7); Parte A -- config/validação/vocabulário/range/
  isolamento de fingerprint/restart/concorrência (18); Parte B -- sucesso
  completo com Artifact+checkpoint, geometria real via `MediaProbe`
  (vídeo gerado com ffmpeg de verdade), detector sem achar nada →
  fallback, detector lançando exceção → fallback (o teste mais
  importante), falhas estruturais (`SourceAsset` ausente, `probe`
  inválido, `video_id`/`project_id` ausentes, `enabled` ausente) NUNCA
  confundidas com fallback, cache hit/miss, cancelamento em dois pontos,
  restart, concorrência real de duas instâncias no mesmo vídeo (16);
  helpers de módulo -- cache key/amostragem/suavização/fallback (8);
  confirmação `media_catalog.py`/`REFRAMED_9_16`/migrations (2); AST
  estrutural -- zero import de módulos irmãos/Geração 1, zero import de
  biblioteca pesada de visão no topo, zero chamada direta a
  subprocess/ffmpeg, zero criação de `Video`/`Publication`/`Schedule` (4).
- Suíte completa (`pytest tests/`): **2175 passed, 1 skipped, 36 subtests
  passed** em 142s.
- `python -m compileall _sistema tests`: OK.

### Contagem antes/depois (medida nesta sessão)

- Antes (sem `test_auto_reframe.py`): **2106 testes coletados**.
- Depois: **2176 testes coletados** (2175 passed + 1 skipped).
- **Delta: +70**, exatamente o número de testes do arquivo novo --
  nenhum teste sumiu em nenhum outro arquivo, nenhum removido.

## 8. Como testar manualmente

1. `pytest tests\test_auto_reframe.py -v` (Windows, após `pip install -r
   tests\requirements-test.txt`) -- os testes que dependem de geometria
   real (`@pytest.mark.skipif(not FFMPEG_DISPONIVEL, ...)`) só rodam se
   `ffmpeg`/`ffprobe` estiverem no PATH; caso contrário são pulados
   honestamente (nunca substituídos por mock silencioso), mesmo padrão
   de `test_media_probe.py`.
2. Cenário manual (quando a UI existir): habilitar AutoReframe num
   Project, rodar o Job, confirmar que um arquivo `.json` de curva
   aparece em `projects/reframe/<video_id>/`; desabilitar a config e
   confirmar que um novo Job falha estruturalmente (`reframe_not_enabled`).

## 9. Riscos conhecidos / dívida técnica

- Sem um `detection_backend` real injetado, o comportamento de produção
  hoje é SEMPRE fallback (crop central) -- documentado como esperado,
  não um bug; uma rodada futura precisa escolher e integrar uma
  biblioteca real de detecção (decisão explicitamente adiada por falta
  de informação suficiente hoje, mesmo espírito de outras decisões já
  documentadas em `audio_engine.py`/`captions_style.py`).
- `DETECTION_SAMPLE_COUNT=30` fixo é um trade-off deliberado
  (previsibilidade de custo vs. cobertura temporal) -- vídeos muito
  longos (ex. 1h+) terão cobertura esparsa por amostra; não medido nesta
  rodada com vídeos reais longos (os vídeos de teste gerados por ffmpeg
  são de 2s).
- `FALLBACK_MIN_FOUND_RATIO=0.5` e a fórmula de `alpha` (suavização) são
  constantes escolhidas nesta rodada sem dados reais de qualidade de
  detecção (não existe detector real ainda) -- podem precisar de ajuste
  quando um backend real for integrado.
- Mesmo padrão de "vazamento de armazenamento conhecido" do Prompt 31: se
  o processo morrer entre a promoção física do arquivo e o INSERT do
  `Artifact`, o arquivo fica órfão no disco (nunca lido como cache
  válido) -- aceito como limitação documentada, não corrupção de dados.

## 10. Pendências

- Escolher e integrar uma biblioteca real de detecção (fora de escopo
  desta rodada, sem informação suficiente hoje).
- Uma rodada futura de "Catalog Wiring" para ligar `REFRAMED_9_16` no
  Media Catalog à evidência real produzida aqui (`Artifact
  kind="reframe_track"` + checkpoint `MEDIA_PROCESSED`), mesmo padrão já
  aplicado para `CAPTIONS`/`TRANSCRIBED`.

## 11. Confirmações explícitas

1. `visual_editor.py`/`captions_engine.py`/`captions_style.py`/
   `media_catalog.py`/`audio_engine.py`/`timeline_editor.py` não
   tocados -- CONFIRMADO (seção 2, timestamps).
2. Nenhuma migration nova, `m001`-`m009` intactos -- CONFIRMADO (seção
   5).
3. `REFRAMED_9_16` continua `"0"` -- CONFIRMADO (seção 6, teste
   dedicado).
4. Nenhum teste removido, delta exato +70 -- CONFIRMADO (seção 7).
5. Falha de detector NUNCA falha o Job -- CONFIRMADO (seção 4.4, 2
   testes dedicados com backends diferentes produzindo o mesmo
   resultado).
6. Falha estrutural continua falhando o Job -- CONFIRMADO (seção 4.4, 5
   testes dedicados).
7. Cancelamento em dois pontos -- CONFIRMADO (seção 0.8 do módulo, 2
   testes).
8. Cache hit não duplica Artifact nem rechama o backend -- CONFIRMADO.
9. Restart com instâncias totalmente novas preserva evidência --
   CONFIRMADO.
10. Concorrência real (duas instâncias, mesmo vídeo) -- CONFIRMADO,
    nenhuma corrida na tabela `artifacts` (mesmo padrão
    `overwrite=True` do Prompt 31).

## 12. Seção 11 (ZIP) -- evidência de processo, não bloqueante

Mesma política já estabelecida desde o Prompt 32: o ZIP que chega para
auditoria é sempre gerado localmente pelo usuário via
`EMPACOTAR_RELEASE.bat`; uma divergência ali sozinha não bloqueia mais a
aprovação. `device_bash` será checado fresco no momento da entrega (não
presumido de rodadas anteriores) -- ver o restante do processo de
entrega para o resultado real desta rodada.
