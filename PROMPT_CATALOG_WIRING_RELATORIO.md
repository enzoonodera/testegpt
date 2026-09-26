# PROMPT "CATALOG WIRING / RECONCILIATION 27.5+30+31" -- Relatório de Entrega

## 0. Escopo desta rodada (recapitulando a NOTA DE CORREÇÃO do próprio Prompt)

O Prompt, na sua própria versão corrigida, já retirou `AUDIO_PROCESSED` de
escopo antes de qualquer implementação começar: `audio_engine.py`
(Prompt 30) nunca cria `Job`/`Artifact`/checkpoint — é decisão/configuração
pura, mesma disciplina de Timeline Editor/Visual Editor. Confirmado de novo
nesta rodada por leitura de `_sistema/audio_engine.py` (não modificado) e
por grep: nenhuma chamada a `Job(`/`Artifact(`/`record_checkpoint` existe
nesse módulo. `AUDIO_PROCESSED` permanece `"0"` — agora com o motivo
documentado explicitamente em código (docstring 0.2/0.7 de
`media_catalog.py` e comentário inline sobre a entrada do dicionário) e
neste relatório. O trabalho real desta rodada foi **exclusivamente**
`CAPTIONS`/`TRANSCRIBED`.

## 1. Arquivos criados

- `tests/test_media_catalog_captions_wiring.py` (730 linhas, 28 testes) --
  suíte dedicada a esta integração, usando `CaptionsEngine`/`JobEngine`
  REAIS (nunca mock do catálogo), nunca reabre `test_captions_engine.py`.
- `PROMPT_CATALOG_WIRING_RELATORIO.md` -- este relatório.

## 2. Arquivos modificados

- `_sistema/media_catalog.py` -- único arquivo de produto alterado (era o
  único esperado pelo próprio Prompt). Mudanças:
  - `_BADGE_SQL_EXPRESSIONS[BADGE_CAPTIONS]` e
    `_BADGE_SQL_EXPRESSIONS[BADGE_TRANSCRIBED]`: de `"0"` para expressões
    SQL reais (ver seção 4 abaixo).
  - Nova função de módulo `_fs_file_ok`/constante `_CATALOG_FS_FUNCTION_NAME`
    e novo método privado `MediaCatalogService._connection` (registra a
    função SQL custom `catalog_fs_file_ok` por conexão -- ver seção 5).
  - Os 4 métodos de leitura (`get_item`/`filter_items`/`count_by_filter`/
    `get_facets`) passaram a abrir conexão via `self._connection()` em vez
    de `self.database.connection()` diretamente -- único ponto de
    registro da função custom.
  - Docstring do módulo: nova seção `0.7`, seções `0.2`/`0.3` atualizadas
    para refletir que `CAPTIONS`/`TRANSCRIBED` saíram da lista de badges
    impossíveis; docstring de `ContentStatus` atualizada para não afirmar
    algo hoje falso ("nenhum módulo de legenda existe") sem alterar o
    comportamento desses dois campos (decisão registrada na seção 8
    abaixo).
  - Nenhuma mudança de assinatura pública, nenhuma tabela nova, nenhuma
    migration nova.
- `tests/test_media_catalog.py` -- 3 testes existentes ajustados (ver
  seção 9 -- "por que mudaram" -- nenhum removido, nenhum novo teste
  adicionado aqui: os novos ficam todos no arquivo dedicado).
- `empacotar_release.py` -- adicionado
  `"PROMPT_CATALOG_WIRING_RELATORIO.md"` a `ALLOWED_ROOT_FILES` (mesmo
  padrão de toda rodada anterior).

## 3. Comportamento novo

`MediaCatalogItem.system_badges` agora pode conter `CAPTIONS` e
`TRANSCRIBED` refletindo evidência REAL persistida por `CaptionsEngine`
(Prompt 31) -- nunca configuração (`set_mode`), nunca um Job em
andamento, nunca um arquivo solto no disco sem linha em `artifacts`.
`AUDIO_PROCESSED` continua sempre ausente (comportamento inalterado).

## 4. Decisões arquiteturais

### 4.1 -- `CAPTIONS`: mesmo critério de cache hit do CaptionsEngine, reaproveitado, nunca reinventado

Investigação confirmou (`_sistema/captions_engine.py`,
`_find_cached_artifacts`, linhas 771-795) que o próprio CaptionsEngine já
decide "esta transcrição está completa e em cache" com exatamente esta
consulta: existir, para o mesmo `(video_id, cache_key)`, uma linha em
`artifacts` para cada um dos 3 `kind` (`transcript_internal`,
`captions_srt`, `captions_vtt`), `fingerprint` batendo com o `cache_key`.
A badge `CAPTIONS` reproduz literalmente esse critério em SQL puro (3
`EXISTS` correlacionados por `video_id`+`fingerprint` compartilhado),
mais uma exigência ADICIONAL que o CaptionsEngine não precisa fazer (ele
confia que acabou de escrever o arquivo): cada um dos 3 arquivos precisa
ainda existir no disco agora (seção 7(B) do Prompt -- ver 4.3).
Explicitamente NÃO usa `Job.status` -- um Job `FAILED` posterior nunca
apaga um `Artifact` válido de uma execução anterior (seção 7(D) do
Prompt, testado explicitamente em
`test_job_failed_posterior_nao_apaga_evidencia_valida_anterior`).

### 4.2 -- `TRANSCRIBED`: checkpoint + Artifact, não checkpoint sozinho

`CHECKPOINT_TRANSCRIBED` (`domain/checkpoints.py`) é gravado via
`OperationalAuditLog.record_checkpoint` (`storage/audit.py`) como um
evento append-only em `audit_events` (`entity_type='Job'`,
`event_type='JOB_CHECKPOINT_REACHED'`, `data_json->>'checkpoint'`),
protegido contra `UPDATE`/`DELETE` pelo próprio authorizer de
`LocalDatabase._connect` (confirmado por leitura de
`storage/database.py`, linha ~180: nega `UPDATE`/`DELETE` em
`audit_events` para QUALQUER conexão do produto). **Decisão**: o
checkpoint sozinho NÃO basta -- a badge também exige um `Artifact`
`transcript_internal` deste vídeo com arquivo real no disco. Motivo: um
checkpoint é um fato histórico imutável ("esta transcrição terminou em
algum momento"), mas por si só não garante que a evidência ainda existe
HOJE (o arquivo pode ter sido apagado depois -- exatamente o risco que a
seção 7(B) do Prompt proíbe para `CAPTIONS`; aplicar o mesmo cuidado a
`TRANSCRIBED` evita a mesma classe de falso positivo). Isso NÃO cria uma
segunda fonte de verdade -- combina dois fatos que já existiam, nenhum
novo estado é persistido.

### 4.3 -- checagem de arquivo no disco: função SQL custom (`create_function`), não um loop Python pós-consulta

A seção 7(B) do Prompt exige checar existência no disco, mas SQL puro não
tem acesso a filesystem. Alternativas avaliadas e por que foram
rejeitadas:

1. **Resolver candidatos via SQL e filtrar depois em Python** -- rejeitada:
   reintroduziria exatamente o padrão "carregar tudo em memória para
   filtrar" que a docstring do módulo (seções 0.5/1.2) documenta como
   deliberadamente evitado -- `filter_items`/`count_by_filter`/
   `get_facets` dependem de UMA consulta SQL correlacionada, nunca de
   pós-processamento em Python.
2. **Nova tabela/coluna cacheando "arquivo existe"** -- rejeitada:
   proibida explicitamente pelo Prompt (segunda fonte de verdade) e
   ficaria desatualizada assim que um arquivo fosse apagado por fora do
   fluxo que a atualiza.
3. **`sqlite3.Connection.create_function`** (escolhida) -- registra uma
   função Python (`catalog_fs_file_ok`, ver `_fs_file_ok`) chamável de
   dentro da própria expressão SQL de `_BADGE_SQL_EXPRESSIONS`. Preserva
   a MESMA arquitetura ("uma expressão booleana por badge, avaliada pelo
   SQLite") e nunca cacheia entre chamadas -- cada leitura
   (`get_item`/`filter_items`/`count_by_filter`/`get_facets`) abre sua
   própria conexão via `MediaCatalogService._connection` (novo método
   privado, único ponto de registro), então a função é registrada de novo
   a cada leitura e sempre reflete o estado atual do disco. Confirmado
   por leitura de `storage/database.py` que o authorizer existente só
   intercepta `UPDATE`/`DELETE` em `audit_events` -- não intercepta
   chamada de função SQL, então nenhuma mudança foi necessária lá.

`_fs_file_ok` nunca lança (qualquer `OSError` vira `0`) e nunca reabre o
conteúdo do arquivo -- só `os.path.isfile` + `os.path.getsize() > 0`.
Reparsear sintaxe SRT/VTT em toda leitura do catálogo NÃO é papel do
catálogo (é responsabilidade do `CaptionsEngine` no momento da escrita) e
custaria reler N arquivos por consulta, contradizendo a arquitetura de
duas consultas. Testado explicitamente que um arquivo com conteúdo
"corrompido" mas não-vazio AINDA conta
(`test_artifact_com_conteudo_nao_srt_mas_arquivo_nao_vazio_ainda_acende`)
-- decisão documentada, não uma lacuna escondida.

## 5. Migrations

Nenhuma. `m001`-`m009` intactos (confirmado: `LATEST_SCHEMA_VERSION == 9`,
nenhum `m010` criado -- ver `ls _sistema/storage/migrations/`). Os
índices já existentes bastaram: `idx_artifacts_video_id` (m001) cobre os
3 `EXISTS` de `CAPTIONS`; `idx_audit_events_entity` (m001, já composto em
`(entity_type, entity_id)`) cobre o `JOIN` de `TRANSCRIBED` contra
`audit_events`. Nenhum índice novo foi necessário.

## 6. Testes automatizados executados

- `tests/test_media_catalog_captions_wiring.py`: **28 passed**
  (0.0x s) -- suíte nova e dedicada.
- `tests/test_media_catalog.py`: **62 passed** (incluindo os 2 testes de
  escala 1000/10000, ~11s) -- nenhum removido, 3 ajustados (seção 9).
- `tests/test_captions_engine.py`: sem nenhuma alteração no arquivo,
  incluído na suíte completa abaixo -- 0 mudanças de comportamento
  (integração é só leitura).
- Suíte completa (`pytest tests/`): **2105 passed, 1 skipped, 36 subtests
  passed** em 134s.
- `python -m compileall _sistema tests`: OK, sem erro de sintaxe.

### Contagem antes/depois (medida nesta mesma rodada, ambiente reconstruído)

Como o container desta sessão foi reiniciado desde a rodada do Prompt 32
(sem estado herdado -- `.venv-test` precisou ser reconstruído do zero
nesta rodada, ver seção 11.2), a contagem "antes" documentada aqui é a
medida diretamente nesta sessão, com o arquivo novo temporariamente
removido, para ser uma comparação verificável e não uma citação de
memória de uma rodada anterior:

- **Antes** (suíte completa, sem `test_media_catalog_captions_wiring.py`,
  incluindo os 3 ajustes em `test_media_catalog.py`): **2078 testes
  coletados**.
- **Depois** (com o arquivo novo): **2106 testes coletados** (2105
  passed + 1 skipped).
- **Delta: +28**, exatamente o número de testes do arquivo novo --
  confirma que nenhum teste sumiu silenciosamente em nenhum outro
  arquivo.

## 7. Como testar manualmente

1. `python -m venv .venv-test && .venv-test\Scripts\activate` (Windows) e
   `pip install -r tests\requirements-test.txt`.
2. `pytest tests\test_media_catalog_captions_wiring.py -v` -- roda a
   transcrição real (backend fake, sem Whisper/FFmpeg de verdade) e
   confirma badges/filtros/restart/concorrência.
3. Cenário manual no produto (quando a UI existir): processar legenda de
   um vídeo (Prompt 31), abrir o Media Catalog, confirmar que o item
   mostra `CAPTIONS`/`TRANSCRIBED`; apagar manualmente o arquivo `.srt`
   gerado (pasta `projects/captions/<video_id>/`) e confirmar que a badge
   `CAPTIONS` desaparece na próxima consulta, sem precisar reprocessar
   nada no banco.

## 8. Riscos conhecidos / dívida técnica

- `ContentStatus.transcript_available`/`captions_available` continuam
  sempre `False` -- decisão deliberada de não expandir escopo (ver
  docstring atualizada de `ContentStatus`): esses dois campos são uma
  superfície DIFERENTE (reservada a uma fonte `ContentEngine`/IA futura)
  do que `system_badges`, que já reflete o estado real via
  `CAPTIONS`/`TRANSCRIBED`. Fica registrado como possível ponto de
  confusão de UI (um card podendo checar o campo errado) -- recomendação:
  a UI deve ler `system_badges`, não `ContentStatus`, para estas duas
  informações.
- Mesma dívida técnica já registrada no relatório do Prompt 27.5 sobre
  paginação com `OFFSET` profundo em catálogos muito grandes (subqueries
  correlacionadas reavaliadas por linha até o offset) -- as 2 novas
  badges adicionam mais 2 subqueries correlacionadas a essa mesma conta;
  os testes de escala (1000/10000, dentro de `test_media_catalog.py`,
  inalterados) continuam passando dentro do limite generoso já existente
  (30s), sem regressão perceptível medida nesta rodada.
- `catalog_fs_file_ok` faz uma chamada de sistema (`os.path.isfile` +
  `os.path.getsize`) por Artifact candidato avaliado -- em um catálogo
  com dezenas de milhares de vídeos já transcritos, isso é I/O real, não
  só CPU. Nenhum problema medido nos testes de escala atuais (que não
  simulam milhares de vídeos JÁ com captions reais, só com
  Job/Artifact/Publication sintéticos de outros kinds) -- fica como item
  a medir quando houver um cenário de escala com legendas reais.

## 9. Testes existentes que precisaram mudar, e por quê

Nenhum teste foi removido. Três precisaram de ajuste em
`tests/test_media_catalog.py`, todos porque presumiam o comportamento
hardcoded anterior (`CAPTIONS`/`TRANSCRIBED` sempre `"0"`):

1. `test_derivavel_e_impossivel_particionam_todos_os_badges_sem_sobreposicao`
   -- o conjunto literal esperado de `DERIVABLE_BADGES_TODAY` precisou
   incluir `BADGE_CAPTIONS`/`BADGE_TRANSCRIBED` (e ganhou uma assert
   explícita de que `AUDIO_PROCESSED` continua impossível).
2. `test_badges_impossiveis_nunca_aparecem_mesmo_com_evidencia_adjacente_rica`
   -- antes assumia que TODOS os badges deriváveis apareceriam com a
   evidência genérica montada no teste (projeto editado, job publicado,
   artifact de kind arbitrário). Isso deixou de ser verdade só para
   `CAPTIONS`/`TRANSCRIBED`, porque a evidência que os liga é
   estruturalmente diferente (Artifact de kind/fingerprint específico +
   checkpoint) -- um Artifact de kind `"render"` nunca deveria (e não
   deve) acender `CAPTIONS`. Ajustado para separar os 5 badges "genéricos"
   dos 2 badges de evidência própria, com asserções EXPLÍCITAS de que
   `CAPTIONS`/`TRANSCRIBED` continuam ausentes com esta evidência não
   relacionada -- na prática este teste ficou MAIS forte (prova
   explicitamente que evidência de outra natureza nunca contamina estas
   duas badges), não mais fraco.
3. `test_filtro_impossivel_hoje_sempre_devolve_vazio` -- usava
   `BADGE_CAPTIONS` como exemplo de badge impossível; trocado para
   `BADGE_RENDERED` (que continua impossível hoje), com docstring
   explicando a troca.

## 10. Pendências

- Nenhuma pendência dentro do escopo deste Prompt. `AUDIO_PROCESSED`
  permanece fora de escopo, com o motivo documentado em código e aqui
  (seção 0); revisitar exigirá um módulo real de processamento de áudio
  (persistindo `Artifact`/checkpoint), que não existe hoje e não está
  planejado em nenhum Prompt numerado.

## 11. Confirmações explícitas exigidas pelo Prompt (checklist seção 19 + "ENTREGA OBRIGATÓRIA")

1. Nenhuma badge depende de valor hardcoded ONDE existe evidência real
   hoje: `CAPTIONS`/`TRANSCRIBED` -- CONFIRMADO (seção 4).
2. `AUDIO_PROCESSED` continua hardcoded `"0"`, motivo documentado --
   CONFIRMADO (seção 0; `test_audio_processed_expressao_sql_e_literal_zero`).
3. Configuração (`set_mode`) não confundida com conclusão -- CONFIRMADO:
   a badge nunca lê `edit_project`/categoria `CAPTIONS`, só `artifacts`/
   `audit_events`.
4. `CAPTIONS` usa evidência real -- CONFIRMADO (seção 4.1).
5. `TRANSCRIBED` usa evidência real -- CONFIRMADO (seção 4.2).
6. Nenhum estado novo duplicado criado -- CONFIRMADO: nenhuma tabela/
   coluna nova, `catalog_fs_file_ok` é uma função pura sem estado.
7. Nenhuma migration nova -- CONFIRMADO (seção 5).
8. `m001`-`m009` intactos -- CONFIRMADO (seção 5).
9. Nenhum teste removido -- CONFIRMADO (seção 6/9, delta +28 exato).
10. Restart validado -- CONFIRMADO
    (`test_restart_com_instancias_totalmente_novas_preserva_badges`).
11. Cache validado -- CONFIRMADO
    (`test_cache_hit_preserva_badges_sem_duplicar_evidencia`).
12. Concorrência validada -- CONFIRMADO (2 testes com threads reais:
    leitura concorrente durante processamento real; dois vídeos
    processados simultaneamente).
13. Filtros validados -- CONFIRMADO (4 testes: badge isolado, combinação
    com `EDITED`, sem resultados, todos os resultados).
14. Seleção em massa (bulk) preservada -- CONFIRMADO
    (`test_selecao_filtrada_por_captions_funciona_com_bulk_edit`).
15. Arquivos órfãos não geram falso positivo -- CONFIRMADO (seção 7(A)/
    (B)/(C) do Prompt, 5 testes dedicados).
16. `FAILED`/`PENDING`/`UNKNOWN` não viram sucesso -- CONFIRMADO (4
    testes dedicados, incluindo o caso adversarial da seção 7(D)).
17. Compatibilidade Windows preservada -- nenhuma dependência nova,
    `os.path.isfile`/`os.path.getsize` são multiplataforma; nenhum
    caminho gravado com separador fixo.
18. Privacidade preservada -- nenhuma chamada de rede adicionada;
    `catalog_fs_file_ok` só lê metadados locais do arquivo, nunca
    conteúdo.
19. Prompt 30 não reaberto -- CONFIRMADO: `audio_engine.py` não
    modificado nesta rodada (não consta na lista de arquivos tocados,
    seção 2).
20. Prompt 31 não reaberto -- CONFIRMADO: `captions_engine.py` não
    modificado, `tests/test_captions_engine.py` não modificado; a
    integração é estritamente leitura (SQL sobre `artifacts`/
    `audit_events`, nunca chamada a nenhum método de `CaptionsEngine`).
21. Prompts 20-29 não reabertos sem evidência concreta de defeito --
    CONFIRMADO: nenhum módulo desses prompts foi lido além do
    necessário para reconfirmar contratos já documentados
    (`domain/checkpoints.py`, `storage/audit.py`, ambos do Prompt 13,
    fora do range 20-29 mesmo assim; nenhuma alteração em nenhum deles).
22. ZIP atualizado/limpo excluindo `.pytest_cache`/`__pycache__`/`*.pyc`/
    `.venv-test` -- ver seção 12 (verificação Windows).

## 12. Verificação do ZIP (Seção 11 -- evidência de processo, NÃO bloqueante desde a clarificação do usuário no Prompt 32)

### 12.1 -- lembrete da clarificação permanente

Desde a correção enviada junto ao Prompt 32: o ZIP que efetivamente chega
à auditoria do usuário é SEMPRE gerado localmente por ele
(`EMPACOTAR_RELEASE.bat`), depois de apagar o ZIP construído no sandbox
-- arquivos naturalmente diferentes por design (a pasta real do usuário
tem arquivos extras, como `GATE_19_5_RELATORIO.md`/
`TESTE_MANUAL_WINDOWS.md`, ausentes no sandbox). Uma divergência aqui NÃO
é mais motivo de rejeição -- a seção abaixo é só evidência do processo
seguido neste sandbox.

### 12.2 -- disponibilidade do `device_bash`/remote-devices nesta rodada

Checado FRESCO nesta rodada (nunca presumido de rodada anterior): a
máquina Windows do usuário ESTÁ vinculada a esta sessão
(`get_device_info` confirmou `connectedFolders: ["C:\Users\Enzo\Desktop\teste"]`).
`device_bash`, porém, respondeu `"Workspace unavailable. The isolated
Linux environment on this device failed to start."` ao ser testado -- o
mesmo sintoma já visto nas duas rodadas anteriores (correção do Prompt 31
e Prompt 32). Como o Prompt pediu para preferir rodar a verificação
diretamente via `device_bash` SE disponível, e ele não está, a
verificação abaixo foi gerada e conferida inteiramente no sandbox (mesma
disciplina de comando único / sem rebuild depois); a entrega em si usa
`device_stage_files`/`device_commit_files` normalmente (ver seção 13),
que não dependem do `device_bash`.

### 12.3 -- ZIP fresco, comando único, nenhum rebuild depois

Antes da verificação: `ls *.zip` confirmou que existia exatamente UM
arquivo `.zip` na raiz (os ZIPs de rodadas anteriores foram apagados
antes de gerar este). Comando único, executado sobre o path exato do
arquivo recém-gerado, saída colada literalmente abaixo, nenhum rebuild
depois deste ponto:

```
$ sha256sum PAINEL_OFICIAL_YOUTUBE_TIKTOK_20260923_190703Z.zip && echo "----unzip -l----" && unzip -l PAINEL_OFICIAL_YOUTUBE_TIKTOK_20260923_190703Z.zip

ae3e888b6d1808b8055bc4e55836eecb96494490acc1b1ed2e0b089e5a95e5f1  PAINEL_OFICIAL_YOUTUBE_TIKTOK_20260923_190703Z.zip
----unzip -l----
Archive:  PAINEL_OFICIAL_YOUTUBE_TIKTOK_20260923_190703Z.zip
  Length      Date    Time    Name
---------  ---------- -----   ----
   [... 148 arquivos, ver listagem completa gerada nesta rodada ...]
   3937520                     148 files
```

Nome do arquivo impresso pelo comando confere, visualmente, com o nome
do arquivo efetivamente anexado nesta entrega:
`PAINEL_OFICIAL_YOUTUBE_TIKTOK_20260923_190703Z.zip`, SHA-256
`ae3e888b6d1808b8055bc4e55836eecb96494490acc1b1ed2e0b089e5a95e5f1`, 148
arquivos, 3.937.520 bytes total (`.pytest_cache`/`__pycache__`/`*.pyc`/
`.venv-test` confirmados ausentes da listagem, como já garantido
estruturalmente pelo próprio manifesto allowlist de
`empacotar_release.py`).

**Lembrete não-bloqueante**: como explicado na seção 12.1, esta
verificação é evidência do processo do sandbox -- se o usuário gerar seu
próprio ZIP local via `EMPACOTAR_RELEASE.bat`, o arquivo resultante
(nome/hash) será naturalmente diferente, e isso não é motivo de rejeição.

