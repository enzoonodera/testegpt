# PROMPT 31 — Captions Engine — Relatório de Entrega

Data: 2026-09-23

Este relatório segue a estrutura obrigatória de 10 pontos do CLAUDE.md,
acrescida das seções específicas exigidas pela especificação do Prompt
31 (decisões de design das Partes A/B, confirmação sobre `BADGE_CAPTIONS`,
decisões de infraestrutura reaproveitada, e a seção final de verificação
de ZIP com o processo CORRIGIDO desta rodada — ver Seção 11, que aplica
a correção explicitamente exigida após a divergência da rodada do Prompt
30).

---

## 1. Arquivos criados

- `_sistema/captions_engine.py` (874 linhas) — módulo `CaptionsEngine`
  com duas metades: Parte A (decisão de modo `CAPTIONS`) e Parte B
  (pipeline real de transcrição via Whisper, produzindo `Artifact` reais
  registrados via `Job`/`JobEngine`).
- `tests/test_captions_engine.py` (1114 linhas, **85 testes**) — suíte
  dedicada cobrindo toda a matriz exigida pela Seção "TESTES" do Prompt.
- `PROMPT_31_CAPTIONS_ENGINE_RELATORIO.md` (este arquivo).

## 2. Arquivos modificados

- `empacotar_release.py` — adicionado `PROMPT_31_CAPTIONS_ENGINE_RELATORIO.md`
  a `ALLOWED_ROOT_FILES` (mesmo padrão de todo Prompt anterior desta
  fase). Nenhuma outra linha alterada.

Nenhum outro arquivo foi tocado. Em particular, **`_sistema/media_catalog.py`
NÃO foi modificado** (Seção 6 abaixo) e `_sistema/job_engine.py`/
`_sistema/edit_project.py`/`_sistema/storage_manager.py`/`_sistema/app_paths.py`
também não foram modificados — toda a infraestrutura necessária já
existia (Seção 4).

## 3. Comportamento novo

### Parte A — decisão de modo de legenda

`set_mode(project_id, mode)`/`get_mode(project_id)`, com `mode` em
`{OFF, SEPARATE, BURNED}`, persistidos via `EditProjectManager` na
categoria `CAPTIONS` (já reservada desde o Prompt 27, valor `"captions"`
— nenhuma string nova). `BURNED` é só a decisão de que a legenda deve
futuramente ser queimada nos pixels — a queima real é trabalho de um
Render Engine futuro, fora do escopo deste Prompt; este módulo nunca
chama FFmpeg nem compõe vídeo.

### Parte B — pipeline real de transcrição

Nova `operation` de `Job`: `TRANSCRIBE_CAPTIONS`, com handler
`CaptionsEngine.handle_transcription_job` registrado via
`JobEngine.register_handler(..., claims_status=JOB_PROCESSING)`. O
handler:

1. Resolve `Job.video_id -> Video -> SourceAsset.local_path`/`fingerprint`
   (única exceção documentada à regra dos Prompts 27-30 de nunca ler o
   arquivo original).
2. Calcula uma chave de cache determinística
   (`source_fingerprint + model_size + language`, serialização
   canônica com `sort_keys=True`).
3. Procura evidência (`Artifact`) já existente para essa chave; se
   encontrada (os 3 `kind`s juntos), reaproveita sem chamar o backend.
4. Caso contrário, chama o backend de transcrição (injetável;
   produção usa `faster-whisper`, isolado atrás de uma função fina —
   Seção 4.6), valida/normaliza os segmentos, gera SRT/VTT/JSON interno
   a partir da MESMA lista normalizada, promove os 3 arquivos a
   `FINAL_ARTIFACT` via `StorageManager`, insere os 3 `Artifact` numa
   única transaction, e registra o checkpoint `CHECKPOINT_TRANSCRIBED`.
5. Devolve `JobStepResult` — nunca persiste `Job.status` diretamente.

## 4. Decisões arquiteturais

### 4.1 Onde os artefatos finais moram (AppPaths)

Nenhum campo novo foi adicionado a `AppPaths`. Os artefatos finais vivem
sob `paths.projects` (já um managed root mutável, categoria
`CATEGORY_USER_FILE`, não-limpável), no subdiretório determinístico
`<paths.projects>/captions/<video_id>/<cache_key>.{srt,vtt,json}` —
organizado por `video_id` (não `project_id`), porque a transcrição é
propriedade do CONTEÚDO DE ÁUDIO do vídeo, compartilhável entre vários
Projects de edição do mesmo vídeo (Princípio B do CLAUDE.md: "trocar
legenda... não deve obrigar a repetir transcrição"). `promote_to_final`
já cria o diretório de destino automaticamente — nenhuma criação manual
foi necessária.

### 4.2 `paths.models` como `download_root` do Whisper

Confirmado por leitura da API real do backend de referência
(`gerar_textos.py`) e documentado: o backend de produção
(`_default_transcribe`) passa `download_root=str(app_paths.models)` ao
`WhisperModel`, em vez do cache padrão do sistema — `paths.models` já
existia em `AppPaths` exatamente para esse propósito.

### 4.3 Chave de cache — `language` ENTRA na chave

`model_size` e `language` entram na chave de cache
(`sha256` sobre `{"source_fingerprint", "model_size", "language"}`
serializado canonicamente). Motivo: `language` muda materialmente o
resultado da transcrição (força/sugere um idioma) — tratá-lo como fora
da configuração esconderia uma transcrição potencialmente diferente
atrás da mesma chave. `sort_keys=True` garante que a ORDEM de
construção do dict nunca produz uma chave diferente por acidente
(testado explicitamente, GATE ADVERSARIAL).

### 4.4 Cache — só `Artifact` na base conta como evidência

A localização de evidência reaproveitável é SEMPRE por `Artifact` já
persistido (`video_id + kind + fingerprint`), nunca por "o arquivo
existe no disco". Os 3 `kind`s (`transcript_internal`/`captions_srt`/
`captions_vtt`) precisam estar TODOS presentes para contar como cache
válido — qualquer ausência parcial (defensivo, não esperado em operação
normal) é tratada como cache ausente, nunca reaproveitamento parcial.

### 4.5 `Job.output_artifact_ids` NÃO é usado (decisão documentada)

Os 3 `Artifact` referenciam o Job que os produziu via `Artifact.job_id`
(campo que já existe para isso). Decisão explícita de NÃO escrever em
`Job.output_artifact_ids`: `OperationalAuditLog.transition_job` RECARREGA
o `Job` do zero a partir do SQLite antes de persistir a transição (só
altera `status`) — qualquer mutação feita pelo handler no objeto `Job`
em memória seria silenciosamente descartada. Uma chamada extra de
`database.save(job)` feita pelo próprio handler fugiria da AUTORIDADE
ÚNICA do `JobEngine` sobre o ciclo de vida do Job (GATE ADVERSARIAL item
7) — exatamente o tipo de "segunda fonte de verdade" a evitar. Os ids
dos artefatos (criados ou reaproveitados) são devolvidos em
`JobStepResult.data`, auditável via o evento `JOB_PROCESSING_COMPLETED`.

### 4.6 Chamada real ao Whisper — isolada atrás de uma função fina

`faster-whisper` está listado em `requirements.txt` mas **não está
instalado no ambiente onde os testes rodam** (confirmado nesta sessão:
`import faster_whisper` falha com `ModuleNotFoundError` no sandbox de
desenvolvimento — teste `test_import_do_modulo_nao_falha_sem_faster_whisper_instalado`
prova isso e prova que importar `captions_engine` mesmo assim funciona).
Por isso o `import` da lib é LOCAL a `_default_transcribe` (nunca no
topo do módulo, verificado por teste AST). Todos os testes injetam
`transcription_backend` (um callable fake) — nenhum teste exercita
`_default_transcribe`.

### 4.7 Promoção com `overwrite=True` — concorrência determinística

O caminho final é nomeado pela cache key (determinístico). Dois Jobs
concorrentes transcrevendo o MESMO vídeo com a MESMA configuração (antes
de qualquer um ter criado cache) podem colidir no mesmo caminho final;
como o conteúdo é determinístico a partir da mesma entrada, promover com
`overwrite=True` é seguro. Cada Job concorrente ainda insere sua própria
linha de `Artifact` — duas linhas apontando para o mesmo `path`/
`fingerprint` é inofensivo (uma consulta de cache futura só precisa
achar UMA). Testado explicitamente com `threading.Barrier` real (2
Jobs, 2 processos de transcrição simultâneos, ambos terminam `READY`,
ambos com arquivos físicos válidos).

### 4.8 Normalização de segmentos

Segmentos com texto vazio/só espaço são descartados silenciosamente (VAD
real produz isso com frequência) — se TODOS forem descartados (ou a
lista já vier vazia), a transcrição inteira é tratada como saída
inválida (`JOB_FAILED`). Qualquer timestamp não-finito, negativo, ou
`start >= end` invalida a transcrição INTEIRA (não descarta o segmento
individualmente) — um backend que devolve timestamps estruturalmente
quebrados não é confiável o suficiente para aceitar parcialmente.
Nenhuma decisão de produto ainda existe sobre vídeos legitimamente
silenciosos (zero segmentos genuinamente corretos) — registrado como
pendência (Seção 10).

### 4.9 Formatos SRT/VTT — sem variação própria

Ambos gerados a partir da MESMA lista de segmentos normalizada (nunca
duas transcrições independentes). SRT: `HH:MM:SS,mmm`, numeração de cue
1-based. VTT: cabeçalho `WEBVTT`, `HH:MM:SS.mmm`. Testados com string
exata (não só "contém"), incluindo um caso cruzando a fronteira de 1
hora (`01:01:01,500`).

## 5. Migrations

Nenhuma migration nova. `LATEST_SCHEMA_VERSION` continua `9`
(confirmado por teste). `CAPTIONS` já existia desde o Prompt 27;
`Artifact`/`Job` já tinham todos os campos necessários
(`kind`/`path`/`fingerprint`/`job_id`/`video_id`/`project_id`,
`output_artifact_ids` — decidido não usar, Seção 4.5).

## 6. CONFIRMAÇÃO EXPLÍCITA — `BADGE_CAPTIONS` NÃO foi alterado

- `_sistema/media_catalog.py` **não foi tocado** — zero linhas
  modificadas.
- `BADGE_CAPTIONS` continua hardcoded `"0"` em `_BADGE_SQL_EXPRESSIONS`
  (sempre falso), exatamente como estava antes deste Prompt.
- A evidência estruturada produzida aqui (`Artifact` reais + checkpoint
  `TRANSCRIBED`) é o que um Prompt FUTURO de "wiring" vai usar para
  acender o badge de verdade — essa ligação NÃO foi feita agora, por
  decisão explícita do escopo do Prompt.

## 7. Testes automatizados executados

Suíte dedicada: `tests/test_captions_engine.py` — **85 testes**:

- Construção (7 testes: cada dependência com tipo errado, `default_model_size` vazio).
- Parte A: cada modo válido, cada modo inválido (8 casos parametrizados),
  `BURNED` é só decisão, isolamento de fingerprint vs `AUDIO_SETTINGS`/
  `TEMPLATE` nas duas direções, restart, idempotência, concorrência real
  com `threading.Barrier`, `project_id` não-UUID (regressão, mesmo achado
  do Prompt 30), `mode` persistido corrompido.
- Cache key: determinismo, muda com fingerprint/model_size/language
  diferentes, independência de ordem de chaves do dict (GATE
  ADVERSARIAL).
- Normalização/validação de segmentos: texto vazio descartado, todos
  vazios = inválido, lista vazia = inválido, 8 casos parametrizados de
  timestamps quebrados (fora de ordem, negativos, `inf`/`-inf`/`nan` em
  start OU end, `start == end`), bool rejeitado, unicode/emoji/acentuação
  preservados.
- Formatos SRT/VTT: string exata (não só "contém"), representação
  interna JSON com campos esperados, SRT e VTT derivados da MESMA lista
  normalizada.
- Pipeline completo: sucesso gera os 3 Artifacts com `path` real
  gravado, `fingerprint` correto, `job_id`/`video_id`/`project_id`
  corretos, checkpoint `TRANSCRIBED` registrado; Job sem `project_id`
  funciona; cache hit não chama o backend de novo (incluindo um mock que
  FALHA o teste se chamado mais de uma vez, como exigido explicitamente);
  fingerprint diferente, `model_size` diferente e `language` diferente
  cada um força nova transcrição.
- Falhas estruturadas: Job sem `video_id`, Video sem `source_asset_id`,
  SourceAsset sem `local_path`, sem `fingerprint`, arquivo ausente no
  disco, saída vazia, timestamps quebrados, vídeo com duração zero — TODOS
  confirmando ZERO Artifacts criados. Exceção não tratada no backend
  propaga e o próprio `JobEngine` pousa em `FAILED` sozinho (não
  reimplementado, só provado). Falha DURANTE a promoção (2º de 3
  arquivos) via `monkeypatch` — confirma que NENHUM Artifact fica
  registrado mesmo com promoção parcial.
- Cancelamento: observado antes da transcrição e observado DEPOIS da
  transcrição mas antes de promover (backend que dispara o cancelamento
  como efeito colateral, simulando o timing real) — ambos sem criar
  nenhum Artifact; sem `control_manager`, comportamento idêntico a antes
  (nunca quebra).
- Concorrência real: 2 Jobs completos (pipelines independentes,
  `threading.Barrier`) transcrevendo o MESMO vídeo/config
  simultaneamente — ambos terminam `READY`, 6 Artifacts (3 kinds × 2
  Jobs), todos os arquivos físicos existem.
- Restart (instâncias totalmente novas) e idempotência (reprocessar um
  Job já `READY` é rejeitado pela State Machine central, backend não é
  chamado de novo).
- AST estrutural: nenhum import de `gerar_textos` nem de nenhum módulo
  protegido de Geração 1, nenhuma chamada a `subprocess`/`ffmpeg`/
  `ffprobe`, nenhuma criação direta de `Video`/`Publication`/`Schedule`/
  `Job` fora da docstring, nunca chama `.transition_to`, nunca importa
  `job_state_machine`, `database.save()` nunca usado (só `insert()`, só
  para `Artifact`), `faster_whisper` nunca importado no topo do módulo.
- Confirmação de que `CAPTIONS` é a string já reservada (`"captions"`) e
  de nenhuma migration nova.

Resultado: **85 passed**.

Suíte completa do projeto (`/root/.local/bin/pytest -q`):

```
1940 passed, 1 skipped, 36 subtests passed in 131.34s
```

Baseline antes deste Prompt: 1855 passed. Delta: **+85** (exatamente o
número de testes novos).

`python3 -m compileall -q _sistema tests empacotar_release.py` —
concluído sem erros.

## 8. GATE ADVERSARIAL — ataques executados

Seguindo a mesma disciplina das rodadas anteriores: scripts manuais de
sanidade adversarial ANTES de declarar a suíte formal completa,
tentando ativamente quebrar a implementação — nenhum achado adicional
além do já incorporado nos testes/decisões acima. Resumo dos ataques:

- Ciclo completo via `JobEngine` real (não só chamadas diretas ao
  handler) — claim atômico, transição final, checkpoint.
- Cancelamento simulado nos DOIS pontos possíveis, usando o
  `ControlManager` real (não um fake) e observando corretamente que
  `cancel()` numa PROCESSING é `DEFERRED` (nunca aplicado na hora),
  exatamente o timing que o handler precisa observar.
- Corrida real de dois Jobs completos transcrevendo o mesmo vídeo/config
  simultaneamente (`threading.Barrier`, sincronizado dentro do próprio
  backend fake) — confirmando que `overwrite=True` na promoção não
  quebra e que a tabela `artifacts` aceita as duas linhas sem erro.
- Falha simulada exatamente no 2º de 3 arquivos durante a promoção
  (`monkeypatch` em `storage.promote_to_final`) — confirmando que os 3
  INSERTs de `Artifact` só acontecem depois que TODOS os 3 arquivos
  foram promovidos com sucesso (nenhum Artifact fantasma).
- `bool` passado como timestamp (`start=True`) — rejeitado pela mesma
  armadilha bool-como-int já documentada nos Prompts 28-30.
- `inf`/`-inf`/`nan` em `start` OU `end`, isoladamente — todos
  rejeitados.
- Dict de configuração com ordem de chaves diferente — chave de cache
  idêntica (prova direta via `_canonical_json`).
- Confirmação direta, nesta sessão, de que `faster_whisper` não está
  instalado no sandbox (`ModuleNotFoundError`) e de que isso NÃO impede
  importar `captions_engine` (import local corretamente isolado).
- `mode` persistido corrompido diretamente via `EditProjectManager`
  (fora do vocabulário fechado) — `get_mode` rejeita estruturadamente.
- `project_id` não-UUID em `set_mode`/`get_mode` — mesma lacuna
  encontrada e corrigida no Prompt 30 (`audio_engine.py`), já corrigida
  PROATIVAMENTE nesta implementação (nunca chegou a ser um bug -- os
  testes de regressão só confirmam o comportamento correto desde o
  início).

## 9. Como testar manualmente

```python
from _sistema.storage.database import LocalDatabase
from _sistema.storage.audit import OperationalAuditLog
from _sistema.storage_manager import StorageManager
from _sistema.edit_project import EditProjectManager
from _sistema.job_engine import JobEngine
from _sistema.captions_engine import CaptionsEngine
from _sistema.domain import Job, JOB_PROCESSING

db = LocalDatabase("caminho/para/painel.db")
db.initialize()
audit = OperationalAuditLog(db)
storage = StorageManager(app_paths, database_path=db.path)
manager = EditProjectManager(db)

engine = CaptionsEngine(manager, database=db, storage_manager=storage, app_paths=app_paths, audit_log=audit)
job_engine = JobEngine(db, audit_log=audit)
job_engine.register_handler(CaptionsEngine.OPERATION, engine.handle_transcription_job, claims_status=JOB_PROCESSING)

job = audit.create_job(Job(video_id=video_id, operation=CaptionsEngine.OPERATION))
result = job_engine.advance(job.id)
print(result.status)  # READY, com 3 Artifacts reais gravados
```

Rodar a suíte dedicada: `pytest -q tests/test_captions_engine.py`.

## 10. Riscos conhecidos / dívida técnica / pendências

- Nenhuma decisão de produto ainda existe sobre vídeos legitimamente
  silenciosos (zero segmentos corretos vs. saída inválida) — hoje ambos
  são tratados como `JOB_FAILED`. Uma distinção futura exigiria um sinal
  adicional do backend (ex.: duração real do áudio) que este Prompt
  deliberadamente não introduz.
- Vazamento de armazenamento conhecido e documentado (Seção 0.6 do
  módulo): se o processo morrer entre `promote_to_final` (arquivo já
  publicado) e o commit da transaction que insere os 3 `Artifact`, o(s)
  arquivo(s) físico(s) ficam órfãos no disco — nunca tratados como cache
  válido (correção nunca lê "arquivo existe" como evidência, só
  `Artifact` na base), mas também nunca limpos automaticamente nesta
  etapa. Um GC futuro de artefatos órfãos (fora de escopo aqui) poderia
  varrer `FINAL_ARTIFACT` sem `Artifact` correspondente.
- Nenhuma validação cruzada entre a duração real do vídeo e os
  timestamps devolvidos pelo Whisper (ex.: um segmento com `end` maior
  que a duração real do arquivo) — este módulo não lê/persiste duração
  real de mídia (mesmo motivo pelo qual `visual_editor.py`/
  `audio_engine.py` também não o fazem).
- `Job.output_artifact_ids` continua sempre vazio para Jobs desta
  `operation` (decisão documentada, Seção 4.5) — se um Prompt futuro
  genuinamente precisar dele preenchido, isso deveria ser um recurso do
  próprio `JobEngine`, não um desvio deste handler.
- Nenhuma pendência de código para este Prompt especificamente; a
  implementação, testes e GATE adversarial estão completos.

---

## 11. VERIFICAÇÃO DE ENTREGA — ZIP e arquivos individuais

### 11.1 Correção de processo aplicada nesta rodada (exigência explícita do usuário)

Na rodada do Prompt 30, a tabela `unzip -l`/SHA-256 colada no relatório
não batia com o ZIP realmente anexado (2 arquivos ausentes E tamanhos
errados para arquivos nem tocados nesta rodada) — a alegação de que a
tabela tinha sido gerada sobre o arquivo efetivamente anexado estava
incorreta. Correção aplicada nesta rodada, seguida à risca: o
`unzip -l` e o `sha256sum` abaixo foram gerados com UM ÚNICO comando
(`sha256sum <zip> && unzip -l <zip>`), executado DEPOIS que o ZIP final
já estava fechado em disco, sobre o caminho literal do arquivo que foi
de fato anexado a esta mensagem — colado abaixo LITERALMENTE, sem
edição posterior deste relatório depois desse comando ter rodado (a
única exceção aceita e já documentada nas rodadas anteriores: o próprio
arquivo deste relatório DENTRO do ZIP é inevitavelmente a versão
RASCUNHO, já que o relatório não pode conter o hash de si mesmo
finalizado — ver Seção 11.3).

### 11.1-BIS Correção desta rodada — o que estava errado na entrega anterior

Auditoria da entrega anterior do Prompt 31 encontrou uma inconsistência
real: a tabela `unzip -l`/SHA-256 colada na Seção 11.3 daquela versão
foi gerada sobre `PAINEL_OFICIAL_YOUTUBE_TIKTOK_20260923_154303Z.zip`,
mas o arquivo de fato anexado à mensagem de entrega era
`PAINEL_OFICIAL_YOUTUBE_TIKTOK_20260923_180325Z.zip` — gerado quase 2h30
depois. A auditoria confirmou isso porque a tabela colada tinha os
MESMOS dois arquivos faltando e os MESMOS tamanhos errados para arquivos
não tocados que já tinham aparecido errados na rodada do Prompt 30 —
evidência de reaproveitamento de um snapshot antigo, não de uma
regeneração real. **O código (`_sistema/captions_engine.py`,
`tests/test_captions_engine.py`, mudança em `empacotar_release.py`) foi
lido, testado de forma independente e está aprovado** — nada nele foi
alterado nesta rodada. A correção abaixo é exclusivamente de processo
na Seção 11: um único ZIP foi gerado agora, nenhum outro build rodou
depois do comando de verificação, e o nome do arquivo no comando abaixo
foi conferido visualmente contra o nome do arquivo efetivamente anexado
antes de colar esta seção.

### 11.2 Disponibilidade de `device_bash` nesta rodada (verificada de novo, não presumida)

Verificado nesta sessão, antes de qualquer suposição: `device_bash`
(shell isolado no computador Windows do usuário) foi chamado e retornou
`"Workspace unavailable. The isolated Linux environment on this device
failed to start."` — ou seja, indisponível NESTA rodada especificamente
(não presumido a partir de um estado anterior da sessão, e checado de
novo mesmo já sabendo que tinha falhado na rodada passada). Isso
significa que o `unzip -l`/SHA-256 abaixo **NÃO** foi gerado rodando um
comando no computador Windows real — foi gerado no sandbox de
desenvolvimento (Linux), sobre o arquivo ZIP final e fechado que será de
fato anexado a esta entrega. Isso é uma limitação honesta desta rodada,
não uma tentativa de contornar a exigência do usuário: o comando abaixo
é único, rodou depois do ZIP fechado, sobre o caminho literal do
arquivo anexado — só o AMBIENTE em que rodou é o sandbox, não o Windows
real, porque o Windows real não respondeu ao shell nesta chamada.

A verificação arquivo-a-arquivo de cada entrega individual (Seção 11.4)
continua sendo feita de ponta a ponta contra o computador Windows real,
porque `device_stage_files`/`device_commit_files` (que não dependem do
shell isolado) continuaram funcionando normalmente — só `device_bash`
falhou ao iniciar.

### 11.3 Comando único — SHA-256 + `unzip -l` do ZIP final fechado (saída literal, sandbox)

Comando executado, em uma única chamada, depois do ZIP já fechado em
disco, sobre o caminho literal do arquivo que será efetivamente anexado
a esta mensagem de entrega (`PAINEL_OFICIAL_YOUTUBE_TIKTOK_20260923_181524Z.zip`). Nenhum outro build do ZIP
rodou depois deste comando — o nome de arquivo abaixo foi conferido
visualmente contra o nome do anexo antes de colar esta seção, exatamente
como pedido:

```
$ sha256sum PAINEL_OFICIAL_YOUTUBE_TIKTOK_20260923_181524Z.zip && unzip -l PAINEL_OFICIAL_YOUTUBE_TIKTOK_20260923_181524Z.zip
d7785094b6263a4c9148986387a47c22f82841c141d4fb1c38ea3a2a6da2431d  PAINEL_OFICIAL_YOUTUBE_TIKTOK_20260923_181524Z.zip
Archive:  PAINEL_OFICIAL_YOUTUBE_TIKTOK_20260923_181524Z.zip
  Length      Date    Time    Name
---------  ---------- -----   ----
    40632  2026-09-23 15:15   ARQUITETURA_ATUAL.md
    13132  2026-09-23 15:15   CLAUDE.md
     2687  2026-09-23 15:15   EMPACOTAR_RELEASE.bat
   169662  2026-09-23 15:15   GATE_19_5_ESTAGIO2_RELATORIO.md
    11446  2026-09-23 15:15   INSTALAR_DEPENDENCIAS_TESTE.bat
     2969  2026-09-23 15:15   LEIA_ME_PRIMEIRO.txt
      866  2026-09-23 15:15   LIMPAR_ANTES_DE_ZIPAR.bat
    24932  2026-09-23 15:15   MAPA_DE_DADOS.md
      686  2026-09-23 15:15   PAINEL_OFICIAL.bat
    29374  2026-09-23 15:15   PRODUCT_INVARIANTS.md
    23512  2026-09-23 15:15   PROMPT_20_CIRCUIT_BREAKER_RELATORIO.md
    14164  2026-09-23 15:15   PROMPT_21_RETRY_INTELIGENTE_RELATORIO.md
    13986  2026-09-23 15:15   PROMPT_22_IDEMPOTENCIA_RELATORIO.md
    17346  2026-09-23 15:15   PROMPT_23_CORRECAO_RELATORIO.md
    13751  2026-09-23 15:15   PROMPT_23_SECRETS_MANAGER_RELATORIO.md
    31899  2026-09-23 15:15   PROMPT_24B_IMPORT_OPTIONS_RELATORIO.md
    25358  2026-09-23 15:15   PROMPT_24_SOURCE_IMPORT_RELATORIO.md
    28360  2026-09-23 15:15   PROMPT_25_SOURCE_CONTEXT_RESOLVER_RELATORIO.md
    29422  2026-09-23 15:15   PROMPT_26_MEDIA_PROBE_RELATORIO.md
    23735  2026-09-23 15:15   PROMPT_27B_VIDEO_PROMOTION_RELATORIO.md
    19021  2026-09-23 15:15   PROMPT_27_5_MEDIA_CATALOG_RELATORIO.md
    28168  2026-09-23 15:15   PROMPT_27_EDIT_PROJECT_RELATORIO.md
    32664  2026-09-23 15:15   PROMPT_28_TIMELINE_EDITOR_RELATORIO.md
    28847  2026-09-23 15:15   PROMPT_29_VISUAL_EDITOR_RELATORIO.md
    31397  2026-09-23 15:15   PROMPT_30_AUDIO_ENGINE_RELATORIO.md
    33446  2026-09-23 15:15   PROMPT_31_CAPTIONS_ENGINE_RELATORIO.md
    24543  2026-09-23 15:15   REGRESSION_CHECKLIST.md
    26204  2026-09-23 15:15   RISCOS_ATUAIS.md
    55747  2026-09-23 15:15   ROADMAP_COMPLETO.md
     6104  2026-09-23 15:15   RODAR_TESTES.bat
    15403  2026-09-23 15:15   TESTE_MANUAL_WINDOWS_ESTAGIO2.md
    97511  2026-09-23 15:15   _sistema/agendar_tiktok.py
    91980  2026-09-23 15:15   _sistema/agendar_youtube.py
    18611  2026-09-23 15:15   _sistema/app_paths.py
    30509  2026-09-23 15:15   _sistema/audio_engine.py
    68310  2026-09-23 15:15   _sistema/batch_engine.py
    39225  2026-09-23 15:15   _sistema/captions_engine.py
    27989  2026-09-23 15:15   _sistema/circuit_breaker.py
    55733  2026-09-23 15:15   _sistema/control_manager.py
     2545  2026-09-23 15:15   _sistema/domain/__init__.py
     3307  2026-09-23 15:15   _sistema/domain/checkpoints.py
     5442  2026-09-23 15:15   _sistema/domain/job_state_machine.py
    15978  2026-09-23 15:15   _sistema/domain/models.py
    23434  2026-09-23 15:15   _sistema/edit_project.py
    17683  2026-09-23 15:15   _sistema/gerar_textos.py
    85229  2026-09-23 15:15   _sistema/job_engine.py
    27891  2026-09-23 15:15   _sistema/limpar_metadados_oficial.py
     2724  2026-09-23 15:15   _sistema/login_conta.py
    37607  2026-09-23 15:15   _sistema/media_catalog.py
    23350  2026-09-23 15:15   _sistema/media_probe.py
    44162  2026-09-23 15:15   _sistema/painel_oficial.py
    21683  2026-09-23 15:15   _sistema/publication_idempotency.py
    32885  2026-09-23 15:15   _sistema/recovery_manager.py
    48725  2026-09-23 15:15   _sistema/resource_manager.py
    27252  2026-09-23 15:15   _sistema/retry_policy.py
    17712  2026-09-23 15:15   _sistema/secrets_manager.py
    94188  2026-09-23 15:15   _sistema/shutdown_coordinator.py
    20616  2026-09-23 15:15   _sistema/source_context.py
    49320  2026-09-23 15:15   _sistema/source_import.py
     1036  2026-09-23 15:15   _sistema/state_json.py
     3365  2026-09-23 15:15   _sistema/storage/__init__.py
    19254  2026-09-23 15:15   _sistema/storage/audit.py
    51041  2026-09-23 15:15   _sistema/storage/backup.py
    27352  2026-09-23 15:15   _sistema/storage/database.py
    38955  2026-09-23 15:15   _sistema/storage/legacy_migration.py
     1359  2026-09-23 15:15   _sistema/storage/migrations/__init__.py
     7411  2026-09-23 15:15   _sistema/storage/migrations/m001_initial.py
      764  2026-09-23 15:15   _sistema/storage/migrations/m002_audit_append_only.py
     2395  2026-09-23 15:15   _sistema/storage/migrations/m003_batch_engine.py
     2538  2026-09-23 15:15   _sistema/storage/migrations/m004_circuit_breaker.py
     2135  2026-09-23 15:15   _sistema/storage/migrations/m005_retry_policy.py
     2605  2026-09-23 15:15   _sistema/storage/migrations/m006_publication_idempotency.py
     4713  2026-09-23 15:15   _sistema/storage/migrations/m007_source_asset_declarations.py
     4856  2026-09-23 15:15   _sistema/storage/migrations/m008_source_asset_context.py
     5469  2026-09-23 15:15   _sistema/storage/migrations/m009_video_declarations.py
   119736  2026-09-23 15:15   _sistema/storage_manager.py
    14323  2026-09-23 15:15   _sistema/time_utils.py
    37920  2026-09-23 15:15   _sistema/timeline_editor.py
    14013  2026-09-23 15:15   _sistema/video_promotion.py
    29484  2026-09-23 15:15   _sistema/visual_editor.py
    36158  2026-09-23 15:15   empacotar_release.py
       33  2026-09-23 15:15   requirements.txt
     6014  2026-09-23 15:15   tests/README.md
       65  2026-09-23 15:15   tests/__init__.py
    11123  2026-09-23 15:15   tests/fakes_playwright.py
       21  2026-09-23 15:15   tests/requirements-test.txt
    23822  2026-09-23 15:15   tests/test_agendar_tiktok_check_item_detection.py
    19670  2026-09-23 15:15   tests/test_agendar_tiktok_checks_card_scope.py
    36470  2026-09-23 15:15   tests/test_agendar_tiktok_copyright_policy.py
    14773  2026-09-23 15:15   tests/test_agendar_tiktok_date_before_time_order.py
    13400  2026-09-23 15:15   tests/test_agendar_tiktok_interactive_copyright_policy.py
    28453  2026-09-23 15:15   tests/test_agendar_tiktok_preflight_checks.py
     7817  2026-09-23 15:15   tests/test_agendar_tiktok_skip_checks_on_allow.py
    33286  2026-09-23 15:15   tests/test_agendar_tiktok_time_layers.py
    31582  2026-09-23 15:15   tests/test_agendar_youtube_copyright_policy.py
    23904  2026-09-23 15:15   tests/test_agendar_youtube_interactive_copyright_policy.py
    10080  2026-09-23 15:15   tests/test_agendar_youtube_time_layers.py
     7327  2026-09-23 15:15   tests/test_ai_cache_parsing.py
    20247  2026-09-23 15:15   tests/test_app_paths.py
    33321  2026-09-23 15:15   tests/test_audio_engine.py
    31931  2026-09-23 15:15   tests/test_backup_restore.py
    70112  2026-09-23 15:15   tests/test_batch_engine.py
    45382  2026-09-23 15:15   tests/test_captions_engine.py
    19226  2026-09-23 15:15   tests/test_checkpoints.py
    26822  2026-09-23 15:15   tests/test_circuit_breaker.py
     7389  2026-09-23 15:15   tests/test_config_names_paths.py
    86602  2026-09-23 15:15   tests/test_control_manager.py
    13408  2026-09-23 15:15   tests/test_domain_models.py
    27954  2026-09-23 15:15   tests/test_edit_project.py
     6240  2026-09-23 15:15   tests/test_ffmpeg_processing.py
     4450  2026-09-23 15:15   tests/test_fingerprint_state.py
     5547  2026-09-23 15:15   tests/test_gerar_textos_exception_persistence.py
     8899  2026-09-23 15:15   tests/test_gerar_textos_ollama_adversarial.py
     4796  2026-09-23 15:15   tests/test_gerar_textos_whisper_adversarial.py
    25385  2026-09-23 15:15   tests/test_job_engine.py
     6839  2026-09-23 15:15   tests/test_job_state_machine.py
    17472  2026-09-23 15:15   tests/test_legacy_json_migration.py
     3209  2026-09-23 15:15   tests/test_limpeza_permission_error.py
    34192  2026-09-23 15:15   tests/test_media_catalog.py
    23595  2026-09-23 15:15   tests/test_media_probe.py
     6950  2026-09-23 15:15   tests/test_migrations_frozen.py
    11476  2026-09-23 15:15   tests/test_operational_audit.py
    16612  2026-09-23 15:15   tests/test_painel_oficial_horarios_por_dia.py
     4723  2026-09-23 15:15   tests/test_painel_oficial_youtube_copyright_timeout_menu.py
    20700  2026-09-23 15:15   tests/test_publication_idempotency.py
    36620  2026-09-23 15:15   tests/test_recovery_manager.py
    37274  2026-09-23 15:15   tests/test_release_packaging.py
   105416  2026-09-23 15:15   tests/test_resource_manager.py
     4676  2026-09-23 15:15   tests/test_resume_detection.py
    31831  2026-09-23 15:15   tests/test_retry_policy.py
     7501  2026-09-23 15:15   tests/test_schedule_slots.py
    19241  2026-09-23 15:15   tests/test_secrets_manager.py
    89276  2026-09-23 15:15   tests/test_shutdown_coordinator.py
    21566  2026-09-23 15:15   tests/test_source_context.py
    18065  2026-09-23 15:15   tests/test_source_import.py
    29758  2026-09-23 15:15   tests/test_source_import_options.py
    25017  2026-09-23 15:15   tests/test_sqlite_storage.py
     7928  2026-09-23 15:15   tests/test_state_json_persistence.py
   116323  2026-09-23 15:15   tests/test_storage_manager.py
     5928  2026-09-23 15:15   tests/test_time_utils.py
    29296  2026-09-23 15:15   tests/test_timeline_editor.py
    16852  2026-09-23 15:15   tests/test_video_promotion.py
    28035  2026-09-23 15:15   tests/test_visual_editor.py
---------                     -------
  3759868                     143 files
```

Saída colada acima é literal e completa (todas as 143 entradas), copiada
diretamente do resultado da ferramenta que executou o comando único —
nenhuma linha foi omitida, reordenada ou editada após a execução.

### 11.4 Verificação individual dos arquivos entregues (byte a byte, no computador Windows real)

Ver Seção 11.5 abaixo — preenchida após a entrega de cada arquivo via
`SendUserFile` → `device_commit_files` → `device_stage_files` →
comparação byte a byte, já que `device_bash` (e portanto `cmp`/
`sha256sum` rodando NO Windows) esteve indisponível nesta rodada
especificamente.

### 11.5 Resultado da verificação individual

O código (`_sistema/captions_engine.py`, `tests/test_captions_engine.py`,
`empacotar_release.py`) **não foi alterado nesta rodada** — a auditoria
confirmou que o problema estava só na Seção 11 do relatório. Esses 3
arquivos já haviam sido gravados no computador Windows real
(`C:\Users\Enzo\Desktop\teste\...`) na rodada anterior e verificados
byte-a-byte (`cmp -s` + SHA-256) contra os originais do sandbox naquela
ocasião — resultado então: IDÊNTICO nos 3 casos, com os SHA-256
`7d2c10d41e195610c75175b4f2aa5f5cdd0769ed8c44303d04059277598d450d`
(`captions_engine.py`),
`daf18f3331b5fb9b471859d7ae1f243c420c16a36770acff647614921462cfda`
(`test_captions_engine.py`) e
`b3df35e774ea6bcd8cf0f9d85d8a2f06e6e36c6b851724b7a7e3746bab625c24`
(`empacotar_release.py`). Como nenhum desses 3 arquivos muda nesta
rodada de correção, eles não precisam ser reenviados nem re-verificados
— só o relatório muda.

Nesta rodada, o único arquivo reentregue é este próprio relatório
(`PROMPT_31_CAPTIONS_ENGINE_RELATORIO.md`), na versão completa e final
(incluindo esta Seção 11.5). Ele é gravado no computador Windows real
via `SendUserFile` → `device_commit_files`, depois lido de volta de lá
via `device_stage_files` (cópia ponto-no-tempo buscada NO Windows, não
reenviada pelo sandbox), e comparado byte-a-byte (`cmp -s` + SHA-256)
contra o original do sandbox — resultado preenchido logo após a
gravação, sem editar mais nada deste relatório depois disso:

| Arquivo | Destino no Windows | Resultado | SHA-256 |
|---|---|---|---|
| `PROMPT_31_CAPTIONS_ENGINE_RELATORIO.md` (versão final, com esta Seção 11.5) | `C:\Users\Enzo\Desktop\teste\PROMPT_31_CAPTIONS_ENGINE_RELATORIO.md` | *(preenchido após a gravação — ver mensagem de entrega)* | *(preenchido após a gravação — ver mensagem de entrega)* |

Nota honesta sobre a auto-referência: como esta linha da tabela é
escrita ANTES da gravação (a comparação só pode acontecer depois que o
arquivo já existe em disco com este texto), o resultado exato
(IDÊNTICO/DIFERE + hash) não pode estar dentro do próprio arquivo — ele
é reportado na mensagem de entrega enviada ao usuário nesta mesma
rodada, e o arquivo entregue no Windows é, portanto, a versão de
trabalho até este ponto (mesma limitação de auto-referência já aceita e
documentada desde as rodadas anteriores).

