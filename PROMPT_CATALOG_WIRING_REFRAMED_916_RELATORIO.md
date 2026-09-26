# CATALOG WIRING — REFRAMED_9_16 (Prompt 33) — Relatório de Entrega

## 0. Contexto e escopo desta rodada

`AutoReframeEngine` (Prompt 33, `_sistema/auto_reframe.py`) já produz
evidência real e persistida (`Artifact` `kind="reframe_track"` +
checkpoint `MEDIA_PROCESSED` gravado via
`OperationalAuditLog.record_checkpoint`), mas o próprio Prompt 33
declarou, por decisão explícita (seção 0.11 da sua docstring), que
`BADGE_REFRAMED_9_16` continuaria hardcoded `"0"` em
`_sistema/media_catalog.py` — destravar esse badge seria trabalho de uma
rodada de "Catalog Wiring" dedicada, no mesmo padrão já usado para
`CAPTIONS`/`TRANSCRIBED` (Prompt 31 implementou o processamento real; um
Prompt de Catalog Wiring separado ligou o badge depois).

Esta é essa rodada. Escopo estritamente delimitado: uma nova expressão
SQL para `BADGE_REFRAMED_9_16` em `_BADGE_SQL_EXPRESSIONS`, reaproveitando
a função já registrada `catalog_fs_file_ok` (nenhuma segunda função
criada) — sem nova migration, sem nova tabela, sem tocar nenhum módulo
produtor.

## 1. Investigação obrigatória (antes de qualquer SQL)

**Item 1 — precedente CAPTIONS/TRANSCRIBED reutilizado.** Reli a seção
0.7 da docstring de `media_catalog.py` (arquitetura já usada para
`TRANSCRIBED`) e a suíte `tests/test_media_catalog_captions_wiring.py`
por completo, como template estrutural direto para esta rodada. Mesma
arquitetura reaplicada: expressão SQL booleana em
`_BADGE_SQL_EXPRESSIONS`, reaproveitando `catalog_fs_file_ok` (a mesma
função Python registrada por conexão em
`MediaCatalogService._connection()` via `sqlite3.Connection.create_function`
— nenhuma segunda função criada).

**Item 2 — confirmação em `auto_reframe.py` (lido, não alterado).**
Confirmado por leitura direta do módulo:
- `ARTIFACT_KIND_REFRAME_TRACK = "reframe_track"` (linha 388).
- O checkpoint é gravado em dois pontos de `handle_reframe_job`: no
  caminho de cache hit, `CHECKPOINT_MEDIA_PROCESSED` com
  `data={"cache_hit": True, "cache_key": cache_key}` (sem chave
  `"fallback"` nesse ramo); no caminho de processamento real,
  `CHECKPOINT_MEDIA_PROCESSED` com
  `data={"cache_hit": False, "cache_key": cache_key, "fallback": used_fallback}`.
- O `Artifact` `reframe_track` é escrito (`_write_and_register_artifact`)
  ANTES do checkpoint, na mesma execução, só no caminho sem cache hit —
  no cache hit o Artifact já existe de uma execução anterior
  (`_find_cached_artifact`), nunca duplicado.

**Item 3 — decisão de design mais importante desta rodada: fallback
conta para a badge.** Duas leituras foram avaliadas antes de escolher:

- (a) a badge representa "o AutoReframe RODOU com sucesso e produziu um
  resultado utilizável" — fallback conta;
- (b) a badge representa "o vídeo foi genuinamente REENQUADRADO por
  detecção real" — fallback não conta.

**Decisão: (a).** Razão: nenhuma outra badge deste catálogo distingue
"qualidade" da evidência — `CAPTIONS` não avalia se o modelo Whisper
usado foi pequeno ou grande, `EDITED` não avalia se a edição é "boa"; o
catálogo inteiro responde só "existe evidência estruturada real?",
nunca "essa evidência é de alta qualidade?". `AutoReframeEngine`, pela
sua própria arquitetura, trata fallback como `JOB_READY` legítimo (nunca
uma falha ou resultado degradado do ponto de vista do Job) — introduzir
uma distinção de qualidade só para este badge quebraria essa
consistência sem necessidade demonstrada pelo roadmap. A expressão SQL
não inspeciona o campo `"fallback"` do JSON do checkpoint — só a
existência do checkpoint + Artifact com arquivo real, exatamente como
`TRANSCRIBED`. Testado explicitamente
(`test_reframed_9_16_acende_mesmo_com_resultado_fallback`), com um
detector que nunca encontra nada, provando por asserção
(`result.data["fallback"] is True`) que o caminho de fallback foi
realmente exercitado antes de checar a badge.

Documentação completa desta decisão (ambas leituras argumentadas) foi
escrita na nova seção "0.8" da docstring de `media_catalog.py`, exigida
pelo Prompt.

**Item 4 (crítico) — `CHECKPOINT_MEDIA_PROCESSED` não é exclusivo do
AutoReframe; investigado antes de escrever SQL.** Confirmado por grep no
domínio fechado (`domain/checkpoints.py`) e por busca em todos os módulos
produtores: hoje, `CHECKPOINT_MEDIA_PROCESSED` é gravado **somente** por
`auto_reframe.py`. `CHECKPOINT_TRANSCRIBED` é gravado somente por
`captions_engine.py`. `CHECKPOINT_EDIT_PLANNED` (usado por
`silence_removal.py`, Prompt 34) é distinto. Não há colisão hoje.

Isso é a premissa que torna a badge segura **hoje**, mas
`domain/checkpoints.py` documenta explicitamente que o vocabulário de
checkpoints é GERAL — "cada operação decide, na prática, quais etapas
percorre" — `MEDIA_PROCESSED` não é reservado ao AutoReframe por design.
**Risco não-bloqueante registrado**: se um Prompt futuro reaproveitar
`MEDIA_PROCESSED` para outra operação de processamento de mídia, uma
expressão SQL que olhasse só para o checkpoint ficaria ambígua entre
"foi reenquadrado" e "outra operação qualquer atingiu esse checkpoint
genérico". Por isso a expressão SQL desta rodada **exige também** o
`Artifact` `kind='reframe_track'` — como só `auto_reframe.py` cria esse
`kind` hoje, a combinação dos dois fatos permanece inequívoca mesmo que
o checkpoint deixe de ser exclusivo no futuro. **Recomendação registrada**
(mesma já aplicada a `TRANSCRIBED`): toda badge futura baseada em
checkpoint deve sempre exigir também o Artifact correspondente, nunca o
checkpoint isolado. Testado explicitamente com um Job de outra operação
gravando o mesmo checkpoint genérico, sem o Artifact correspondente —
a badge não acende
(`test_checkpoint_de_outra_operacao_generica_nao_acende_reframed`).

## 2. Arquivos modificados

- `_sistema/media_catalog.py`:
  - `BADGE_REFRAMED_9_16` deixou de ser `"0"` e passou a ter a expressão
    SQL real (checkpoint `MEDIA_PROCESSED` via JOIN `jobs`↔`audit_events`
    + Artifact `reframe_track` com `catalog_fs_file_ok(a.path) = 1`).
  - Nova seção "0.8" na docstring do módulo, documentando: a arquitetura
    reaproveitada, a confirmação do risco do checkpoint não-exclusivo (com
    a recomendação), e a decisão do fallback com as duas leituras
    argumentadas.
  - Narrativa do topo da docstring atualizada: `REFRAMED_9_16` saiu da
    lista de badges "estruturalmente impossíveis hoje" (agora são 9, não
    mais 10).
- `tests/test_media_catalog.py`: os 3 testes que referenciam
  `DERIVABLE_BADGES_TODAY`/`IMPOSSIBLE_BADGES_TODAY` foram atualizados
  para refletir `REFRAMED_9_16` no conjunto derivável (mesmo padrão já
  aplicado quando `CAPTIONS`/`TRANSCRIBED` saíram do conjunto impossível).
- `tests/test_auto_reframe.py`: o teste
  `test_reframed_9_16_continua_hardcoded_zero_apos_este_prompt` (que
  documentava, no momento do Prompt 33, que a badge continuava `"0"`)
  foi reescrito como
  `test_reframed_9_16_foi_ligado_por_rodada_dedicada_posterior_nunca_por_este_prompt`,
  confirmando que a badge tem expressão SQL real hoje e apontando para a
  suíte dedicada desta rodada — sem tocar em nenhuma linha de
  `_sistema/auto_reframe.py`.
- `empacotar_release.py`: adicionado
  `"PROMPT_CATALOG_WIRING_REFRAMED_916_RELATORIO.md"` a
  `ALLOWED_ROOT_FILES` (ordem alfabética, antes de
  `PROMPT_CATALOG_WIRING_RELATORIO.md`).

## 3. Arquivos criados

- `tests/test_media_catalog_reframe_wiring.py` (NOVO, 27 testes) —
  suíte dedicada, mesma estrutura de
  `tests/test_media_catalog_captions_wiring.py`: evidência completa
  acende (detecção real e fallback, os dois cenários); PENDING não
  acende; FAILED não acende e não apaga evidência anterior válida
  (usando config diferente para forçar cache miss e realmente exercitar
  o caminho de falha); nenhum Artifact → nenhuma badge; arquivo órfão
  sem Artifact não acende; Artifact com arquivo deletado do disco (via
  pipeline real + `os.remove`) e Artifact manualmente inserido com path
  inexistente não acendem; arquivo vazio não acende; checkpoint sozinho
  sem Artifact não acende (incluindo o cenário do risco documentado —
  checkpoint gravado por uma OUTRA operação); cache hit preserva sem
  duplicar Artifact; restart com instâncias totalmente novas preserva;
  isolamento entre vídeos; concorrência (leitura durante processamento
  real e dois vídeos processados simultaneamente); filtros (isolado,
  combinado, sem resultados, todos os resultados); seleção filtrada
  compatível com bulk edit; refresh antes/durante/depois; UNKNOWN não
  acende; `AUDIO_PROCESSED` confirmado fora de escopo; `get_facets`
  conta corretamente.
- `PROMPT_CATALOG_WIRING_REFRAMED_916_RELATORIO.md` (este arquivo).

## 4. Comportamento novo

`BADGE_REFRAMED_9_16` agora reflete evidência real: acende quando existe,
para o vídeo, um checkpoint `MEDIA_PROCESSED` registrado por QUALQUER Job
(hoje só o AutoReframe grava esse checkpoint) **e** um `Artifact`
`reframe_track` cujo arquivo ainda existe e não está vazio no disco.
Acende tanto para reenquadramento com detecção real quanto para
resultado de fallback (crop central estático) — decisão documentada na
seção 0.8. Não distingue qualidade da detecção, apenas existência de
evidência estruturada — consistente com todas as demais badges do
catálogo.

## 5. Decisões arquiteturais

- Reaproveitamento estrito da arquitetura já validada para
  `TRANSCRIBED` (mesma função `catalog_fs_file_ok`, mesmo padrão de
  checkpoint+Artifact combinados) — zero nova abstração criada.
- Checkpoint isolado nunca é suficiente — sempre exigir também o
  Artifact do `kind` correto, protegendo contra a ambiguidade futura do
  vocabulário de checkpoints não-exclusivo (risco documentado no item 4
  acima e na seção 0.8 da docstring).
- Fallback conta para a badge (decisão do item 3, documentada com as
  duas leituras argumentadas).
- Nenhum módulo produtor foi tocado — `auto_reframe.py`,
  `captions_engine.py`, `audio_engine.py`, `captions_style.py`,
  `visual_editor.py`, `timeline_editor.py`, `silence_removal.py`
  permanecem intocados (confirmado por `find -newer`, seção 8 abaixo).

## 6. Migrations

Nenhuma. `LATEST_SCHEMA_VERSION` permanece `9`.

## 7. Testes automatizados executados

- `tests/test_media_catalog_reframe_wiring.py` (novo): **27 passed**.
- `tests/test_media_catalog.py` (3 asserções atualizadas): **61 passed**.
- `tests/test_auto_reframe.py` (1 teste reescrito): incluído na suíte
  completa abaixo, sem falhas.
- Suíte completa (`/root/.local/bin/py.test tests/ -q`): **2284 passed,
  1 skipped, 36 subtests passed** (141.48s), coleta total **2285**
  (delta exato de **+27** sobre a baseline de 2258 passed/2259
  coletados do Prompt 34 — os 27 testes novos deste arquivo; os 3
  testes atualizados em `test_media_catalog.py` e o 1 teste reescrito em
  `test_auto_reframe.py` mantêm suas contagens, só mudaram asserções).
  Zero regressões em qualquer outro módulo.
- `python3 -m compileall -q _sistema tests`: OK, sem erros.
- Verificação direta: `frozenset(_BADGE_SQL_EXPRESSIONS) == _SYSTEM_BADGES_SET`
  confirmado (`assert OK`); `DERIVABLE_BADGES_TODAY` agora inclui
  `REFRAMED_9_16` (8 badges deriváveis); `IMPOSSIBLE_BADGES_TODAY`
  reduzido para 9 badges.

## 8. Como testar manualmente

1. Rodar `RODAR_TESTES.bat` (ou `pytest tests/`) e confirmar 0 falhas.
2. No app: processar um vídeo com AutoReframe habilitado (Config REFRAME
   `enabled=True`) até `JOB_READY`. Confirmar que a badge
   `REFRAMED_9_16` aparece no Catálogo de Mídia para esse vídeo, tanto
   quando a detecção encontra rosto/pessoa quanto quando cai em
   fallback (crop central).
3. Deletar manualmente o arquivo do Artifact `reframe_track` no disco e
   atualizar a tela do catálogo — confirmar que a badge some.
4. Confirmar que `AUDIO_PROCESSED` continua ausente mesmo em vídeos com
   `REFRAMED_9_16` ativo.

## 9. Riscos conhecidos

- **Não-bloqueante, documentado nas seções 0.8/1 (item 4) e no código**:
  `CHECKPOINT_MEDIA_PROCESSED` não é exclusivo do AutoReframe por design
  do vocabulário fechado. Hoje só `auto_reframe.py` o grava. Se um Prompt
  futuro reaproveitar esse checkpoint para outra operação, a combinação
  checkpoint+Artifact `reframe_track` continua protegendo esta badge
  especificamente, mas a recomendação de sempre exigir o Artifact junto
  do checkpoint deve ser mantida para qualquer badge futura que reuse
  checkpoints deste vocabulário.
- Confirmado via find -newer que a ferramenta de empacotamento
  (`empacotar_release.py`) e os arquivos de teste foram os únicos
  tocados além de `media_catalog.py` — nenhum módulo produtor alterado.

## 10. Dívida técnica criada

Nenhuma nova. A dívida pré-existente (`AUDIO_PROCESSED` e os demais 8
badges hardcoded `"0"`) permanece documentada e inalterada.

## 11. Pendências

Nenhuma pendência funcional para este escopo. Os 9 badges restantes
(`VALIDATED`, `METADATA_CLEAN`, `AUDIO_PROCESSED`, `TEMPLATE_APPLIED`,
`AI_TITLE`, `AI_DESCRIPTION`, `AI_HASHTAGS`, `RENDERED`, `READY`)
continuam fora de escopo, como já documentado.

**Nota sobre o ZIP (Seção 11 / processo, não bloqueante)**: o ZIP de
auditoria final é regenerado localmente pelo usuário via
`EMPACOTAR_RELEASE.bat`. Uma eventual divergência de hash/listagem nesse
processo local não é bloqueante para esta entrega — é evidência de
processo, preenchida como tal.
