# PROMPT 44 — Base do SmartClipEngine (Fase 7: Smart Clip) — Relatório de Entrega

## 0. Texto literal do Prompt

> Crie base do SmartClipEngine.
>
> Receber vídeo longo. Transcrever. Dividir semanticamente em
> assuntos/segmentos. Detectar: começo da ideia; desenvolvimento;
> conclusão; mudanças de assunto. Não escolher corte somente pelo
> timestamp da frase mais forte.

Este é o primeiro Prompt da Fase 7 (Smart Clip). Entrega a BASE: a
segmentação estrutural do conteúdo, com papéis (começo/desenvolvimento/
conclusão) e marcação de mudança de assunto. Não entrega score/ranking de
candidatos a clipe (Prompt 45), geração de clipe final com start/end
prontos pra render (Prompt 47) nem detecção de tipo de conteúdo
(Prompt 46) — nenhum desses foi implementado aqui, por estarem
explicitamente fora de escopo deste Prompt.

## 1. Arquivos criados

- `_sistema/smart_clip_engine.py` (novo, ~610 linhas) — módulo completo:
  docstring de decisões arquiteturais, constantes, `SemanticSegment`,
  `segment_transcript()` (função pura), `compute_cache_key()`, classe
  `SmartClipEngine` (Job handler `handle_analyze_job`).
- `tests/test_smart_clip_engine.py` (novo, ~460 linhas, **29 testes**).
- `PROMPT_44_SMART_CLIP_BASE_RELATORIO.md` (este arquivo).

## 2. Arquivos modificados

- `empacotar_release.py` — adicionado `"PROMPT_44_SMART_CLIP_BASE_RELATORIO.md"`
  a `ALLOWED_ROOT_FILES` (ordem alfabética preservada).

**`_sistema/captions_engine.py` NÃO foi modificado** — confirmado por
hash (`sha256sum`) antes/depois desta entrega, permanecendo idêntico.
Reaproveitado apenas por leitura/acionamento do seu Job handler já
existente, exatamente como o Prompt pediu.

## 3. Comportamento novo

`SmartClipEngine.handle_analyze_job(job)` — novo Job handler, operação
`ANALYZE_CONTENT`:

1. Exige `job.video_id` (falha honesta com `reason="job_missing_video_id"`
   se ausente — nenhuma validação de `Video`/`SourceAsset` é feita aqui de
   propósito: este módulo nunca lê o arquivo de mídia original, só a
   transcrição já produzida por `CaptionsEngine`; essa validação mais
   profunda já acontece dentro do handler de transcrição quando ele é
   efetivamente acionado).
2. Localiza o `Artifact` `transcript_internal` mais recente para o
   `video_id` (mesma convenção "mais recente é o atual" de
   `RenderEngine`/`FinalMediaValidator`), verificando que o arquivo ainda
   existe em disco.
3. Se ausente/apagado: aciona o Job real de transcrição de
   `CaptionsEngine` através de um `JobEngine` interno e privado
   (`self._transcription_job_engine`), nunca reimplementando a lógica de
   transcrição.
4. Usa o `fingerprint` do Artifact de transcrição diretamente como seu
   cache_key de origem — nunca recomputa `CaptionsEngine.compute_cache_key`.
5. Calcula seu próprio `cache_key` (transcrição de origem + parâmetros de
   segmentação + versão do algoritmo). Se já existe um Artifact
   `content_segments_track` com esse `cache_key` para este vídeo, reaproveita
   (`cache_hit: True`) sem reprocessar.
6. Caso contrário, lê os segmentos brutos da transcrição e roda
   `segment_transcript()` (função pura, dois sinais locais — ver seção 4).
7. Persiste o resultado como novo `Artifact` `kind="content_segments_track"`
   (JSON, via `StorageManager.allocate_temp`/`promote_to_final`, mesmo
   padrão atômico de `auto_reframe.py`).
8. Grava `CHECKPOINT_ANALYZED` (reservado desde o início do vocabulário do
   projeto, nunca escrito até este Prompt) **somente no caminho de
   sucesso total** — nunca em `FAILED`/`CANCELLED`.

## 4. Decisões arquiteturais (com justificativa)

### 4.1 — Sinais de segmentação escolhidos (nenhuma API externa)

Exatamente **dois** sinais locais, calculados só a partir da própria
sequência `start`/`end`/`text` já transcrita:

- **Gap de silêncio** — `próximo.start - atual.end >= min_topic_gap_seconds`
  (padrão `1.5s`, exposto como parâmetro do construtor). Uma pausa longa
  entre frases costuma acompanhar uma transição real de assunto.
- **Marcador de discurso** — o texto do próximo segmento (normalizado)
  começa com uma expressão de um léxico curado em português ("agora,",
  "voltando", "próximo assunto", "concluindo", "por fim", etc.), também
  exposto como parâmetro do construtor.

Qualquer um dos dois, isoladamente, já dispara uma quebra. O sinal que
efetivamente disparou cada corte é registrado no Artifact
(`split_gap_seconds`/`split_discourse_marker`) — nunca um score opaco
único. Isso é a aplicação concreta da frase final do Prompt: a decisão de
onde cortar nunca se resume ao timestamp de uma única "frase mais forte"
— é sempre a combinação transparente de sinais estruturais auditáveis.

**Limitação documentada e coberta por teste** (exigida pelo próprio
Prompt): se um transcript não tem nenhum gap perceptível E nenhum trecho
começa com um marcador do léxico, o resultado é um único segmento
semântico cobrindo o vídeo inteiro — não existe um terceiro sinal de
"força bruta" (ex. teto de duração) para forçar uma quebra artificial,
porque isso seria exatamente o tipo de corte arbitrário que o Prompt pede
para evitar.

### 4.2 — Papel estrutural por posição

Sem análise semântica de conteúdo real (fora de escopo — exigiria
IA/API), o papel é atribuído pela posição no resultado: primeiro =
`BEGINNING`, último = `CONCLUSION`, meio = `DEVELOPMENT`. Caso degenerado
(exatamente 1 segmento): `BEGINNING`, por convenção documentada — não há
desenvolvimento/conclusão a distinguir quando existe só uma unidade.

### 4.3 — `JobEngine` interno e privado, não uma dependência externa

Nenhum outro engine de `_sistema/` (exceto `BatchEngine`, que é uma
camada de orquestração por natureza) recebe `JobEngine` como dependência
de construtor. Fazer `SmartClipEngine` exigir um externo introduziria uma
forma de dependência sem precedente e uma ambiguidade real sobre qual
`JobEngine` usar. A decisão adotada — mesmo espírito de `RenderEngine`
construindo internamente um `CaptionsEngine`/`AutoReframeEngine` — foi
`SmartClipEngine` construir um `JobEngine` próprio e privado, usado
SOMENTE para acionar (`advance`) o Job de transcrição através do
mecanismo real de produção, nunca uma casca simplificada que deixaria o
Job de transcrição órfão sem transição final persistida (GATE item 13).
Confirmado por leitura direta de `job_engine.py` que `advance()` nunca
mantém uma transação aberta durante a execução do handler — reentrância
seguro, sem risco de deadlock.

### 4.4 — `job.project_id` não é exigido

Ao contrário de `AutoReframeEngine.handle_reframe_job` (que exige
`project_id` especificamente porque lê configuração por-Project via
`get_config(project_id)`), `SmartClipEngine` não tem configuração
por-Project nenhuma — mesmo raciocínio de
`CaptionsEngine.handle_transcription_job`, que também não exige
`project_id`. Exigi-lo aqui seria uma restrição artificial sem motivo
arquitetural.

### 4.5 — Cobertura sem buraco/sobreposição

Cada segmento semântico é um agrupamento CONTÍGUO de segmentos brutos, em
ordem — nunca reordena/descarta/duplica. Garantido estruturalmente pela
própria construção (particiona a sequência, nunca filtra), não por um
ajuste manual. Coberto por teste dedicado que confirma início/fim exatos
e ausência de sobreposição/perda de texto.

### 4.6 — `kind="content_segments_track"`, escopado por `video_id`

Mesmo padrão de nomeação `<domínio>_track` de
`ARTIFACT_KIND_REFRAME_TRACK`/`ARTIFACT_KIND_SILENCE_TRACK`. Escopado só
por `video_id` (não por `project_id`) — a segmentação semântica é
propriedade do CONTEÚDO do vídeo, não de nenhuma decisão de edição de um
Project específico.

## 5. Migrations

Nenhuma. Nenhuma tabela nova — reaproveita `jobs`/`artifacts`/
`audit_events` já existentes; `CHECKPOINT_ANALYZED` já estava reservado
no vocabulário de checkpoints desde antes deste Prompt (só nunca havia
sido gravado).

## 6. Testes automatizados executados

`tests/test_smart_clip_engine.py` — **29 testes**, cobrindo:

1. `segment_transcript` isolada (um só assunto; múltiplos via gap;
   múltiplos via marcador sem gap; três assuntos com papel `DEVELOPMENT`
   no meio; sem nenhum sinal — limitação documentada; caso degenerado de
   1 segmento bruto; validação de argumentos).
2. Invariante de cobertura sem buraco/sobreposição.
3. `compute_cache_key` — determinístico, sensível a cada parâmetro.
4. Validação de `SemanticSegment` (role desconhecido).
5. Construção do `SmartClipEngine` (tipos inválidos, `min_topic_gap_seconds`
   não positivo).
6. Handler — falha honesta sem `video_id`.
7. Handler — **nunca dispara nova transcrição quando um artifact de
   transcrição válido já existe** (teste explicitamente exigido pelo
   Prompt, com contagem de chamadas ao backend fake).
8. Handler — nunca duplica o Artifact de análise entre dois Jobs para o
   mesmo vídeo (reaproveita via cache_key).
9. Handler — aciona a transcrição real (via backend fake, nunca Whisper
   real) quando nenhum artifact existe, incluindo caso de vídeo curto
   (1 segmento bruto → 1 segmento semântico) e falha honesta quando a
   transcrição em si falha.
10. `CHECKPOINT_ANALYZED` gravado somente no sucesso (inclusive no
    caminho onde a transcrição já existia mas a análise era nova),
    nunca em falha (incluindo a falha mais cedo, por `video_id` ausente).
11. Contrato Job/checkpoint/Artifact — restart com instâncias
    TOTALMENTE novas de `LocalDatabase`/`EditProjectManager`/
    `StorageManager`/`CaptionsEngine`/`SmartClipEngine` contra o mesmo
    arquivo `.db`, confirmando reaproveitamento do cache pós-restart.
12. Checagem estrutural honesta: nenhuma referência a bibliotecas de
    rede (`requests`/`httpx`/sockets/APIs de IA) nem a nenhum backend de
    Whisper importado diretamente no módulo.

Resultado: **29 passed** (isolado) e **suíte completa: 2762 passed,
1 skipped, 36 subtests passed** — baseline anterior era 2733 passed
(Prompt 43 + correção Windows), crescimento de exatamente 29 (as novas
adicionadas), **zero regressões**.

```
2762 passed, 1 skipped, 36 subtests passed in 194.31s (0:03:14)
```

`python3 -m compileall -q _sistema tests` — sem erros.

## 7. Como testar manualmente

Não há UI nova neste Prompt (é um Job handler interno). Verificação via
Python:

```python
from _sistema.smart_clip_engine import SmartClipEngine, OPERATION_ANALYZE_CONTENT
# construir com manager/database/storage_manager/app_paths reais
# criar um Job(video_id=..., operation=OPERATION_ANALYZE_CONTENT)
# chamar engine.handle_analyze_job(job) (chamada direta) ou registrar no
# JobEngine principal via register_handler + advance(job.id)
```

O Artifact resultante (`kind="content_segments_track"`) é um JSON legível
com a lista de segmentos, cada um com `role`/`topic_change`/
`split_gap_seconds`/`split_discourse_marker`.

## 8. Riscos conhecidos

- Os dois sinais de segmentação (gap + marcadores) são heurísticas
  simples de v1, deliberadamente sem IA — para conteúdo falado sem pausas
  perceptíveis e sem uso de nenhuma das expressões do léxico, o resultado
  colapsa em um único segmento. Documentado como limitação conhecida, não
  como bug; comportamento coberto por teste dedicado.
- O léxico de marcadores é modesto e voltado a português coloquial —
  pode não capturar todos os padrões de fala reais; é um parâmetro do
  construtor, ajustável sem mudança de código.
- `min_topic_gap_seconds=1.5s` é um valor conservador não validado
  empiricamente contra vídeos reais — também ajustável via parâmetro.

## 9. Dívida técnica criada

Nenhuma dívida nova deliberada. O módulo segue os mesmos padrões já
estabelecidos (Job/Artifact/Checkpoint, cache_key determinístico,
promoção atômica de arquivo) sem atalhos.

## 10. Pendências

- Prompts futuros da Fase 7 (45: score/ranking de candidatos; 46:
  detecção de tipo de conteúdo; 47: geração do clipe final com
  start/end prontos pra render) constroem sobre esta base — nenhum foi
  antecipado aqui.
- **Pendência não relacionada a este Prompt, ainda em aberto**: a
  correção Windows do teardown SQLite/WAL (`CORRECAO_WINDOWS_SQLITE_WAL_TEARDOWN_RELATORIO.md`)
  segue aguardando que o usuário rode `RODAR_TESTES.bat` 3 vezes na
  máquina Windows real e reporte os 3 resultados — este ambiente não
  consegue executar um shell Windows real para confirmar empiricamente.

## Ataques adversariais adicionais executados (GATE item 15)

- Confirmado que a validação de `video_id`/`Video`/`SourceAsset` NÃO é
  feita incondicionalmente no início do handler (removida deliberadamente
  após revisão própria) — evitaria quebrar espuriamente o caminho de
  cache-hit se o `Video`/`SourceAsset` associado fosse alterado depois,
  já que o handler nunca lê esses campos.
- Confirmado por teste que dois Jobs distintos para o mesmo `video_id`
  nunca duplicam o Artifact de análise (`len(content_artifacts) == 1`).
- Confirmado por teste que uma falha na transcrição nested propaga como
  `JOB_FAILED` honesto (`reason="transcription_trigger_failed"`), nunca
  deixando o Job "preso" em estado intermediário.
- Confirmado por teste de restart com instâncias 100% novas (banco
  reaberto do zero) que o cache sobrevive e é reconhecido corretamente.
- Confirmado por grep/teste estrutural que nenhuma biblioteca de
  rede/IA externa é referenciada no módulo, e que `captions_engine.py`
  permanece byte-idêntico (hash antes/depois).
- Verificado que `CHECKPOINT_ANALYZED` nunca é gravado no caminho de
  falha mais cedo (`job_missing_video_id`), não só nas falhas "tardias".
