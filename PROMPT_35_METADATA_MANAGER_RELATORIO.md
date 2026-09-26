# PROMPT 35 — Metadata Manager — Relatório de Entrega

## 1. Arquivos criados

- `_sistema/metadata_manager.py` (novo, ~640 linhas) — módulo completo:
  Parte A (configuração `METADATA_MODE`, preferência lembrada, contrato
  de decisão pendente) + Parte B (processamento real só para
  `mode == CLEAN`).
- `tests/test_metadata_manager.py` (novo, 72 testes, todos passando).
- `PROMPT_35_METADATA_MANAGER_RELATORIO.md` (este arquivo).

## 2. Arquivos modificados

Nenhum. `_sistema/media_catalog.py` não foi tocado (confirmado por
`find -newer` e por teste dedicado
`test_media_catalog_badge_metadata_clean_continua_hardcoded_zero`) —
`BADGE_METADATA_CLEAN` permanece hardcoded `"0"`.

## 3. Decisão 1 — Parte A + Parte B real para CLEAN; PROFILE deliberadamente adiado

Avaliadas as duas leituras propostas pelo Prompt. Resultado: nem (a)
puro nem (b) puro — um meio-termo justificado por módulo:

- **CLEAN**: Parte B real, HOJE. A operação (`ffmpeg -map_metadata -1`)
  é determinística e já comprovadamente funcional neste ambiente (é a
  mesma flag que o módulo legado usa em produção) — ao contrário dos
  Prompts 33/34, não existe aqui nenhuma "biblioteca de detecção ainda
  incerta" a justificar adiamento. Adiar seria overengineering pelo lado
  errado.
- **PROFILE**: Parte B real deliberadamente ADIADA. O roadmap não
  especifica nenhum campo do que um "perfil configurado" contém além da
  frase em si — diferente de CLEAN, que o próprio roadmap já define
  operacionalmente ("remover metadata desnecessária" = tudo).
  Implementar processamento real hoje exigiria consolidar um schema de
  produto que este módulo teve que inventar (seção 0.2 da docstring),
  o que "Perfil avançado pode ficar em Configurações" sugere ser
  responsabilidade de uma camada futura dedicada.

Job de `PROFILE` sempre completa `JOB_READY` sem processar nada
(`data={"skipped": True, "reason": "profile_processing_deferred"}`) —
nunca falha, nunca grava evidência. Job de `KEEP` sempre completa
`JOB_READY` sem processar (`skipped: True, reason: "keep_never_processes"`).
Isso satisfaz automaticamente o texto literal do roadmap ("KEEP ou
PROFILE não podem ser confundidos com CLEAN"): como nenhum dos dois
grava QUALQUER evidência hoje, é estruturalmente impossível confundi-los
— testado explicitamente
(`test_job_modo_keep_e_ready_mas_nunca_grava_evidencia`,
`test_job_modo_profile_e_ready_mas_nunca_grava_evidencia`,
`test_job_modo_keep_nunca_chama_o_backend`). Uma rodada futura que
implemente processamento real de PROFILE deve usar um `Artifact.kind`
PRÓPRIO, nunca `metadata_clean_output` — documentado como restrição de
design para essa rodada futura.

"A ferramenta deve funcionar sozinha em lote" é satisfeito hoje: a
decisão (Parte A) já se aplica a N vídeos de uma vez (cada `Project` tem
sua categoria `METADATA_MODE` independente) e o processamento de `CLEAN`
já roda via `JobEngine` — o mesmo motor que já processa lotes de
qualquer tamanho para todos os módulos anteriores.

## 4. Decisão 2 — distinção frente a `limpar_metadados_oficial.py` (Geração 1) e confirmação de que nenhuma metadata falsa é fabricada

`_sistema/limpar_metadados_oficial.py` (821 linhas, Geração 1) foi relido
por inteiro. Ele faz duas coisas: (a) remove metadata do container
(`-map_metadata -1 -map_chapters -1` + campos de texto em branco) — o
mesmo que este módulo faz no modo CLEAN; (b) em seguida FABRICA metadata
falsa de captura por smartphone (`gerar_metadados_smartphone()`: data
aleatória, GPS aleatório em torno de São Paulo, `make: "Apple"`, modelo
de iPhone aleatório) e REENCODA o vídeo com variação de velocidade
(0.99x-1.01x) e CRF aleatório, para dificultar detecção de
reupload/duplicata.

O item (b) é exatamente o "recurso para burlar plataforma" que o
roadmap deste Prompt proíbe explicitamente. `_sistema/metadata_manager.py`
é INTEIRAMENTE independente: não importa, não chama, não reaproveita
nenhuma linha de `limpar_metadados_oficial.py` (confirmado por teste AST
dedicado, `test_modulo_nao_importa_modulos_protegidos_nem_irmaos`, que
inclui `limpar_metadados_oficial` na lista proibida). O backend de
produção deste módulo usa `-c copy` (stream copy, sem reencode — nunca
altera pixel/áudio) e escreve SÓ campos vazios (`-metadata title=` etc.)
— nunca nenhum valor de substituição. Testado negativamente de forma
explícita: `test_backend_nunca_escreve_metadata_fabricada` (nenhum campo
GPS/data de criação/fabricante/modelo de dispositivo existe na saída) e
`test_backend_padrao_usa_stream_copy_nunca_reencoda` (o `codec_name` do
stream de vídeo permanece idêntico ao original — prova de que não houve
reencode).

## 5. Decisão 3 — checkpoint reaproveitado: `COMPOSED`, não `VALIDATED`

Investigado antes de decidir: hoje só `CHECKPOINT_MEDIA_PROCESSED`
(`auto_reframe.py`), `CHECKPOINT_TRANSCRIBED` (`captions_engine.py`) e
`CHECKPOINT_EDIT_PLANNED` (`silence_removal.py`) têm gravador real —
`IMPORTED`, `ANALYZED`, `COMPOSED`, `RENDERED`, `VALIDATED`,
`UPLOAD_STARTED`, `REMOTE_CONFIRMED` estavam todos livres.

`CHECKPOINT_VALIDATED` é a escolha óbvia à primeira vista — o texto do
roadmap literalmente diz "resultado validado". **Deliberadamente
evitada**: `media_catalog.py` já reserva `BADGE_VALIDATED` (hardcoded
`"0"`) para um significado FUTURO e DIFERENTE (validação de qualidade/QA
mais ampla do conteúdo). "Resultado validado" no texto deste Prompt, em
contexto, significa só "a operação terminou com sucesso verificado" —
não uma referência à futura feature de validação. Gravar
`CHECKPOINT_VALIDATED` aqui criaria uma colisão de NOME direta e
previsível com um futuro módulo de Validação real (que quase certamente
vai querer gravar exatamente esse checkpoint para acender
`BADGE_VALIDATED`) — diferente do risco hipotético já aceito para
`MEDIA_PROCESSED`/`REFRAMED_9_16`, aqui a colisão é concreta e visível
hoje na própria lista de badges. `RENDERED` tem o mesmo problema
(`BADGE_RENDERED` já reservado para um futuro Render Engine).
`IMPORTED`/`UPLOAD_STARTED`/`REMOTE_CONFIRMED` têm forte conotação de
evento de OUTRO pipeline (importação/publicação).

**Decisão: `CHECKPOINT_COMPOSED`** — nenhum badge chamado `COMPOSED`
existe hoje em `media_catalog.py`, e semanticamente ("montar/preparar a
saída final") é o encaixe mais direto disponível para "uma cópia
processada do arquivo foi montada", por eliminação frente a `ANALYZED`
(sugere leitura/inspeção, o oposto de uma operação de escrita). Nenhum
checkpoint do vocabulário fechado foi desenhado pensando em "limpeza de
metadata" — esta é a melhor correspondência disponível, escolhida e
documentada por escrito (seção 0.6 da docstring do módulo). Regra geral
já estabelecida continua valendo: uma futura rodada de Catalog Wiring
para `METADATA_CLEAN` deve exigir o checkpoint `COMPOSED` **e** o
`Artifact` `metadata_clean_output` juntos — nunca o checkpoint sozinho.

## 6. Comportamento novo

- Categoria `METADATA_MODE` (`"metadata_mode"`, já reservada em
  `edit_project.py` desde o Prompt 27, sem consumidor até agora):
  `mode` ∈ {KEEP, CLEAN, PROFILE}, `profile_fields` (vocabulário fechado
  de 5 campos de texto: title/comment/artist/copyright/description) só
  quando `mode == PROFILE`.
- "Lembrar minha preferência": `remember_preference`/
  `get_remembered_preference`/`forget_remembered_preference`, via
  `LocalDatabase.set_setting`/`get_setting`/`delete_setting` (tabela
  `settings` já migrada, nenhuma tabela nova) — escopo GLOBAL de
  instalação (investigado: não existe conceito de multi-usuário no
  banco local hoje; `Account` é conta de publicação/plataforma, não
  usuário da aplicação).
- Contrato de decisão pendente (`get_pending_decision(project_id)`) para
  uma camada de UI futura consumir: `has_decision`, `current_mode`,
  `suggested_mode` (da preferência lembrada) — este Prompt não
  implementa UI.
- `mode == CLEAN` via `JobEngine`: copia o `SourceAsset` (nunca o
  original), remove metadata via `ffmpeg -map_metadata -1 -map_chapters
  -1` + campos de texto em branco (stream copy, `-c copy`), promove a
  cópia via `StorageManager`, registra `Artifact`
  (`kind="metadata_clean_output"`) + checkpoint `COMPOSED` como
  evidência para uma futura rodada de Catalog Wiring. Cache por
  `(video_id, cache_key)`. Falha do backend SEMPRE falha o Job (decisão
  diferente do Prompt 34 — ver seção 0.5 da docstring: não existe aqui
  um "resultado vazio seguro" análogo a "nenhum silêncio encontrado";
  CLEAN promete remover metadata, então silenciosamente não remover
  nada e ainda reportar sucesso seria enganoso).
- `mode == KEEP`/`PROFILE`: `JOB_READY` imediato, sem processar, sem
  gravar evidência alguma.

## 7. Testes automatizados executados

- `tests/test_metadata_manager.py` (novo): **72 passed**, cobrindo:
  construção; Parte A (vocabulário/merge atômico/isolamento de
  fingerprint/restart/concorrência/`project_id` inválido); preferência
  lembrada (grava/lê/esquece/sobrevive restart/é global); contrato de
  decisão pendente; adversarial KEEP/PROFILE nunca produzem evidência de
  CLEAN; Parte B completa (sucesso real, arquivo original nunca tocado,
  cópia fisicamente distinta, backend falhando sempre falha o Job,
  falhas estruturais, cache hit sem duplicar, restart, concorrência real
  entre 2 vídeos, cancelamento em 2 pontos); backend padrão `ffmpeg`
  REAL (remove metadata de verdade, nunca fabrica metadata, usa stream
  copy sem reencode, erro estruturado quando `ffmpeg` ausente); AST
  estrutural (zero import de módulos protegidos/irmãos/legado;
  `subprocess`/`ffmpeg` só dentro do backend isolado; nenhuma criação de
  `Video`/`Publication`/`Schedule`; `media_catalog.py` confirmado
  intocado; nenhuma migration nova).
- Suíte completa (`/root/.local/bin/py.test tests/ -q`): **2356 passed,
  1 skipped, 36 subtests passed** (150.02s) — delta exato de **+72**
  sobre a baseline de 2284 (rodada Catalog Wiring REFRAMED_9_16). Zero
  regressões em qualquer outro módulo.
- `python3 -m compileall -q _sistema tests`: OK, sem erros.
- `find -newer` confirma: só `_sistema/metadata_manager.py` e
  `tests/test_metadata_manager.py` foram criados; nenhum arquivo
  existente foi modificado.
- `LATEST_SCHEMA_VERSION` continua `9` — nenhuma migration nova.

## 8. Como testar manualmente

1. Rodar `RODAR_TESTES.bat` (ou `pytest tests/`) e confirmar 0 falhas.
2. No app (quando a UI existir): configurar `mode=CLEAN` para um Project
   e processar via Job. Confirmar que um novo arquivo aparece em
   `<paths.projects>/metadata_clean/<video_id>/` sem os campos
   título/comentário/artista/copyright/descrição, e que o arquivo
   original permanece byte-a-byte idêntico.
3. Configurar `mode=KEEP` e confirmar que o Job completa sem criar
   nenhum arquivo novo.
4. Chamar `remember_preference("CLEAN")`, reiniciar o processo e
   confirmar que `get_remembered_preference()` ainda retorna `CLEAN`.

## 9. Riscos conhecidos

- `CHECKPOINT_COMPOSED` não é uma correspondência semântica perfeita
  (nenhum checkpoint do vocabulário fechado foi desenhado pensando em
  "limpeza de metadata") — escolhido por eliminação e documentado por
  escrito (seção 0.6). Mesma mitigação geral já estabelecida
  (checkpoint + Artifact sempre juntos) continua protegendo contra
  ambiguidade funcional se um futuro módulo também reaproveitar
  `COMPOSED`.
- `PROFILE` não tem processamento real nesta rodada — decisão
  deliberada e documentada (seção 3 deste relatório / seção 0.3 da
  docstring do módulo), não uma omissão.
- Timeout do `ffmpeg` fixo em 120s (`_FFMPEG_TIMEOUT_SECONDS`) — razoável
  para vídeos curtos de teste; pode precisar de ajuste para vídeos muito
  longos em produção (não testado neste Prompt).

## 10. Dívida técnica criada

Nenhuma nova além da já esperada: `BADGE_METADATA_CLEAN` continua
hardcoded `"0"` (trabalho de uma futura rodada de Catalog Wiring
dedicada); processamento real de `PROFILE` fica para um Prompt futuro
com schema de produto definido.

## 11. Pendências

- Rodada futura de Catalog Wiring para `METADATA_CLEAN` (mesmo padrão
  de CAPTIONS/TRANSCRIBED/REFRAMED_9_16): exigir checkpoint `COMPOSED` +
  Artifact `metadata_clean_output` juntos.
- Definição de schema de produto para `PROFILE` antes de implementar seu
  processamento real (Parte B) — fora de escopo deste Prompt.
- Camada de UI que consome `get_pending_decision()`/
  `remember_preference()` — fora de escopo deste Prompt (não há UI neste
  projeto ainda).

**Nota sobre o ZIP (Seção 11 / processo, não bloqueante)**: o ZIP de
auditoria final é regenerado localmente pelo usuário via
`EMPACOTAR_RELEASE.bat`. Uma eventual divergência de hash/listagem nesse
processo local não é bloqueante para esta entrega — é evidência de
processo, preenchida como tal.
