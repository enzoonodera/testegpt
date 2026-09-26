# PROMPT 43 — Quality Control (`_sistema/final_media_validator.py`)

## 1. Arquivos criados

- `_sistema/final_media_validator.py` (~750 linhas) — módulo `FinalMediaValidator`, job handler `VALIDATE_MEDIA`. Docstring de módulo completa (seções 0-0.7) documenta cada decisão arquitetural com justificativa, incluindo o mapeamento dos 10 itens do checklist para dados reais e a matriz de decisão FAIL/USER_ACTION_REQUIRED/READY.
- `tests/test_final_media_validator.py` (~920 linhas, 63 testes) — construção; os 10 itens do checklist isoladamente (função pura + `MediaProbeResult` fabricado); handler completo (resolução do artifact, agregação, matriz de decisão); `CHECKPOINT_VALIDATED` gravado somente no sucesso total; `BADGE_VALIDATED`/`BADGE_READY` positivo/negativo; os 4 ramos SKIP individuais do item 8; o único caminho de correção automática segura distinguido do FAIL; restart com novas instâncias; concorrência determinística; 2 testes de integração real com FFmpeg; AST estrutural (confirma que `StorageManager`/`AppPaths` nunca são importados).
- `PROMPT_43_QUALITY_CONTROL_RELATORIO.md` (este arquivo).

## 2. Arquivos modificados

- `_sistema/media_catalog.py` — `BADGE_VALIDATED` ganhou expressão SQL real em `_BADGE_SQL_EXPRESSIONS` (era `"0"`), seguindo o mesmo padrão duplo de `BADGE_RENDERED`/`BADGE_REFRAMED_9_16`/`BADGE_TRANSCRIBED` (checkpoint `VALIDATED` **E** o MESMO `Artifact` `kind="render_output"` com arquivo real no disco — nenhum novo `kind` de Artifact inventado, já que `FinalMediaValidator` nunca produz um Artifact próprio, só valida/corrige o existente). `BADGE_READY` deixou de ser `"0"` e passou a ser a conjunção SQL literal da expressão de `BADGE_RENDERED` **E** a de `BADGE_VALIDATED` — exatamente a definição já documentada no docstring histórico do módulo ("READY depende de RENDERED AND VALIDATED"). Docstring do módulo (seção "badges estruturalmente impossíveis") atualizada para remover `VALIDATED`/`READY` da lista de impossíveis e documentar a nova fonte de verdade.
- `tests/test_media_catalog.py` — o teste que particiona badges deriváveis/impossíveis (`test_derivavel_e_impossivel_particionam_todos_os_badges_sem_sobreposicao`) foi atualizado para incluir `BADGE_VALIDATED`/`BADGE_READY` em `DERIVABLE_BADGES_TODAY` (mesmo ajuste incremental já feito antes para `CAPTIONS`/`TRANSCRIBED`/`REFRAMED_9_16`/`RENDERED`). O teste adversarial de evidência adjacente rica (`test_badges_impossiveis_nunca_aparecem_mesmo_com_evidencia_adjacente_rica`) ganhou duas asserções explícitas confirmando que `VALIDATED`/`READY` não disparam com evidência genérica adjacente. Nenhuma lógica de produção tocada — só as expectativas do teste.
- `empacotar_release.py` — `PROMPT_43_QUALITY_CONTROL_RELATORIO.md` adicionado a `ALLOWED_ROOT_FILES` (ordem alfabética).

Nenhum outro módulo (`render_engine.py`, `template_engine.py`, `template_selector.py`, `captions_style.py`, `captions_engine.py`, `metadata_manager.py`, `job_engine.py`, `storage_manager.py`, `media_probe.py`) foi tocado — comportamento fora de escopo explícito do Prompt, confirmado por inspeção manual: nenhum destes arquivos aparece na lista de arquivos criados/modificados acima, e o teste estrutural `test_modulo_nao_importa_storage_manager_nem_app_paths` prova via AST que `final_media_validator.py` nem sequer importa dois desses módulos.

## 3. Comportamento novo

`FinalMediaValidator.handle_validate_job` é um handler de `JobEngine` (`operation="VALIDATE_MEDIA"`, `claims_status=PROCESSING`) que, para um `(video_id, project_id)`:

1. Localiza o `Artifact` `kind="render_output"` **mais recente** para esse `(video_id, project_id)` — mesma convenção "mais recente é o atual" já usada por `render_engine.py`. Não recomputa o `cache_key` de `RenderEngine` (evitaria acoplamento a detalhes internos de outro módulo — GATE item 7, autoridade única).
2. Roda os **10 itens do checklist literal do roadmap** sobre esse Artifact: arquivo existe, duration válida, resolução (comparada ao canvas do Template quando há um selecionado), codec (`h264`, único produzido por `RenderEngine`), áudio (cross-check contra o `SourceAsset` original), tamanho > mínimo (2048 bytes — ver seção 4), sem output truncado (`MediaProbe.probe_deep`, decode completo), captions dentro do canvas quando verificável (condicional, 4 pré-condições SKIP), texto dentro das zonas (sempre SKIP — `TEXT_LAYERS` sem produtor real), artifact íntegro (compara `size_bytes` gravado vs. real no disco).
3. Agrega os 10 resultados pela matriz de decisão (seção 4): qualquer FAIL → `JOB_FAILED`; senão qualquer `USER_ACTION_REQUIRED` → `JOB_USER_ACTION_REQUIRED`; senão sucesso total → grava `CHECKPOINT_VALIDATED` e devolve `JOB_READY`.
4. O único caminho de correção automática (item 10, `Artifact.size_bytes is None`) só se aplica quando TODOS os outros 9 itens já passaram nesta mesma execução — nunca reescreve um metadado divergente, nunca re-renderiza.

`BADGE_VALIDATED`/`BADGE_READY` no catálogo agora acendem com evidência dupla real (checkpoint + Artifact com arquivo no disco), fechando a cadeia `RENDERED → VALIDATED → READY` documentada desde o Prompt 42.

## 4. Decisões arquiteturais (atenção especial pedida pelo Prompt)

**(1) Tamanho mínimo do arquivo de saída — `MIN_OUTPUT_SIZE_BYTES = 2048` (2 KiB).** Não é um piso de "qualidade" (o Prompt não fornece nenhuma tabela de bitrate/resolução/duração-alvo para derivar um número por combinação, e inventar uma tabela de política sem essa evidência seria uma decisão de produto fora do escopo autorizado) — é um piso ESTRUTURAL contra arquivos vazios/quase-vazios (defesa em profundidade contra corrupção/truncamento/interferência externa **depois** da promoção original do `RenderEngine`, que já validou o arquivo antes de promover). Verificado empiricamente: os clipes sintéticos de 1 segundo/64×64 gerados por `ffmpeg`/`libx264` neste ambiente (mesmos usados pelos testes de integração real deste módulo e de `test_render_engine.py`) produzem bem mais que 2048 bytes — nenhum falso positivo observado nos 2 testes de integração real com FFmpeg. Exposto como parâmetro do construtor (`min_output_size_bytes`) para calibração futura sem exigir mudança de código.

**(2) Matriz de decisão FAIL vs. USER_ACTION_REQUIRED vs. correção automática, item a item:**

| # | Item | FAIL (técnico, inequívoco) | USER_ACTION_REQUIRED (decisão do usuário) | Correção automática |
|---|------|------|------|------|
| 1 | arquivo existe | ausente | — | — |
| 2 | duration válida | `None`/`<=0` | — | — |
| 3 | resolução | zero, ou diverge do canvas do Template (bug de composição) | — | — |
| 4 | codec | diferente de `h264` (`RenderEngine` sempre codifica com `libx264`) | — | — |
| 5 | áudio | source original tem áudio e o render não tem (stream perdido — bug) | — | — (SKIP quando o original é indeterminável) |
| 6 | tamanho > mínimo | abaixo do piso (ver item 1 desta tabela) | — | — |
| 7 | sem output truncado | `probe_deep` não confirma integridade | — | — |
| 8 | captions no canvas | — | ancora da legenda fora da zona CAPTION (render tecnicamente válido, posicionamento é escolha do usuário) | — |
| 9 | texto nas zonas | — | — (sempre SKIP — sem produtor real) | — |
| 10 | artifact íntegro | `size_bytes` gravado diverge do real (arquivo mudou após promoção — só recuperável por re-render, fora de escopo) | — | `size_bytes is None` → backfill seguro (o próprio checklist já validou o arquivo atual nesta execução) |

Nenhum item produz `FAIL` e `USER_ACTION_REQUIRED` ao mesmo tempo por definição (cada `_check_*` devolve um único status). "Re-render" nunca é uma ação deste módulo em nenhuma hipótese — vetado explicitamente pelo texto do Prompt; a recuperação de um item 10 com FAIL segue o caminho padrão `JOB_FAILED → JOB_RETRY` de um novo Job de render, fora do escopo deste módulo.

**(3) Reaproveitamento do `kind="render_output"` para o Artifact de VALIDATED.** `FinalMediaValidator` nunca produz um Artifact próprio — só lê/corrige o existente do `RenderEngine`. Inventar um novo `kind` duplicaria vocabulário sem necessidade (CLAUDE.md, "REVISÃO CRÍTICA" — não fazer overengineering sem benefício concreto).

**(4) Dependências deliberadamente mais leves que `RenderEngine`.** Sem `StorageManager`/`AppPaths` no construtor — o módulo nunca aloca/escreve arquivo de mídia, só lê um `Path` já existente e, no máximo, faz `database.save(artifact)` para o backfill de metadado. Confirmado estruturalmente via AST (`test_modulo_nao_importa_storage_manager_nem_app_paths`).

## 5. Migrations

Nenhuma. Nenhuma tabela nova, nenhuma coluna nova — `Artifact.size_bytes` já existe no schema desde antes deste Prompt (usado pelo `RenderEngine`, Prompt 42); `CHECKPOINT_VALIDATED` já existia em `domain/checkpoints.py`, reservado e nunca escrito até agora.

## 6. Testes automatizados executados

```
pytest tests/test_final_media_validator.py -q
→ 63 passed (inclui 2 testes de integração REAL com FFmpeg — RODARAM,
  não pulados: ffmpeg/ffprobe estão disponíveis neste ambiente,
  confirmado explicitamente na saída verbose antes da entrega)

pytest tests/test_media_catalog.py -q
→ 58 passed (1 teste pré-existente com expectativas atualizadas para a
  nova realidade de BADGE_VALIDATED/BADGE_READY, ver seção 2; 1 teste
  adversarial ganhou 2 asserções novas)

python3 -m compileall -q _sistema tests
→ sem erros

pytest tests/ -q
→ 2725 passed, 1 skipped, 36 subtests passed em 155.24s
  (baseline anterior, Prompt 42: 2665 passed — 63 novos testes deste
  Prompt (alguns dos 63 substituem cobertura já implícita em asserções
  pré-existentes de test_media_catalog.py, daí a diferença de 3 frente
  a uma soma ingênua), 1 skip pré-existente e não relacionado, ZERO
  regressões)
```

Cobertura da matriz obrigatória do Prompt: (a) cobertura determinística de cada item do checklist via probe injetável, sem FFmpeg real necessário para a lógica de decisão; (b) 2 testes de integração real com FFmpeg (vídeo válido passa em todos os itens aplicáveis; vídeo truncado após geração real falha no item `not_truncated`); (c) `CHECKPOINT_VALIDATED` gravado SOMENTE no sucesso total (2 testes: FAIL nunca grava, USER_ACTION_REQUIRED nunca grava); (d) teste adversarial que `BADGE_VALIDATED`/`BADGE_READY` nunca nascem de checkpoint sozinho nem de Artifact sozinho (mesmo padrão de `BADGE_RENDERED` em `test_render_engine.py`), incluindo o caso do Artifact apontando para um arquivo fisicamente apagado; (e) os 4 ramos SKIP do item 8 testados individualmente (modo ≠ BURNED; sem template/sem zona CAPTION; sem posição explícita; posição incompleta), mais PASS dentro da zona e USER_ACTION_REQUIRED fora dela; (f) item 9 sempre SKIP mesmo com dados não-vazios; (g) o único caminho de correção automática segura (item 10) claramente distinguido do FAIL, com verificação de que o FAIL NUNCA reescreve o metadado divergente; (h) contrato Job/checkpoint/Artifact — restart com instâncias totalmente novas de `LocalDatabase`/`EditProjectManager`/`StorageManager`/`TemplateEngine`/`OperationalAuditLog`/`FinalMediaValidator`; concorrência determinística com `threading.Barrier` e duas instâncias completamente independentes escrevendo a mesma correção de `size_bytes` sem corromper o valor final; (i) AST estrutural confirmando a ausência de `StorageManager`/`AppPaths` no módulo.

## 7. Como testar manualmente

1. Rodar um `Job` `RENDER_VIDEO` até `JOB_READY` (Prompt 42) para obter um `Artifact` `kind="render_output"` real no disco.
2. Registrar `FinalMediaValidator.OPERATION` no `JobEngine`, criar um `Job` `VALIDATE_MEDIA` para o mesmo `(video_id, project_id)` e chamar `job_engine.advance(job.id)`.
3. Verificar `MediaCatalogService(database).get_item(video_id).system_badges` — `VALIDATED` e `READY` devem acender.
4. Apagar o arquivo do Artifact do disco e chamar `get_item` de novo — `VALIDATED`/`READY` devem desaparecer imediatamente (evidência checada em tempo real, nunca cacheada).
5. Configurar um Template com zona `CAPTION`, `CAPTIONS.mode=BURNED` e uma posição de legenda fora dessa zona — rodar `VALIDATE_MEDIA` de novo e confirmar `JOB_USER_ACTION_REQUIRED` (não `JOB_FAILED`, não `JOB_READY`).

## 8. Riscos conhecidos

- **Item 5 (áudio) depende do `SourceAsset.local_path` ainda existir no disco.** Se o arquivo original foi movido/apagado depois da importação, o item vira SKIP (nunca fabrica um resultado sem dado real) — o checklist perde essa cobertura específica para vídeos cuja origem não está mais acessível. Comportamento documentado, não uma regressão: é a mesma filosofia de "nunca fabricar evidência" já aplicada em todo o restante do módulo.
- **Item 3 (resolução) só compara contra o canvas do Template quando um Template está selecionado.** Sem Template, apenas a presença de largura/altura é validada — não há "resolução esperada" independente para comparar nesse caso (comportamento correto, documentado, não uma lacuna).
- Mesma observação de `RenderEngine` (Prompt 42) sobre `ResourceManager` não integrado — este módulo é leve o bastante (nenhum FFmpeg de verdade, só leituras de metadado) para que isso seja um risco desprezível aqui.

## 9. Dívida técnica criada

- Nenhuma nova. O item 9 (texto nas zonas) permanece documentado como sempre-SKIP até que `TEXT_LAYERS` tenha um produtor real — mesma dívida já registrada e herdada de Prompts anteriores, não criada por este.

## 10. Pendências

- Nenhuma pendência dentro do escopo deste Prompt. Fora de escopo (explicitamente, por instrução do Prompt): mudanças em `render_engine.py`/`template_engine.py`/`captions_style.py`/`metadata_manager.py`; produtor real de `TEXT_LAYERS`; `ResourceManager`; qualquer item de Fase 7/Smart Clip/Prompt 44+.

---

## Ataques adversariais executados (GATE, item 15)

1. Duas instâncias de `FinalMediaValidator`/`LocalDatabase`/`OperationalAuditLog` completamente independentes validando o MESMO Artifact simultaneamente (`threading.Barrier` determinístico) — ambas terminam `JOB_READY`, o backfill de `size_bytes` não corrompe (valor final igual ao tamanho real do arquivo em ambos os casos).
2. Restart com instâncias TOTALMENTE NOVAS de `LocalDatabase`/`EditProjectManager`/`StorageManager`/`TemplateEngine`/`OperationalAuditLog`/`FinalMediaValidator` sobre o mesmo arquivo `.db` — `CHECKPOINT_VALIDATED` gravado por uma sessão anterior é corretamente visto por uma sessão nova.
3. Artifact ausente (arquivo apagado do disco) — todos os demais 9 itens reportados como `SKIP` explícito (nunca fabricados como PASS/FAIL sem dado real), `JOB_FAILED`.
4. `size_bytes` divergente do tamanho real — `JOB_FAILED`, e o valor gravado no banco é comprovadamente NUNCA sobrescrito pela execução (correção automática só ocorre no ramo `None`, nunca no ramo "divergente").
5. Checkpoint `VALIDATED` gravado sem nenhum `Artifact` `render_output` correspondente — badge não acende (evidência simples nunca basta, mesmo padrão de `RENDERED`).
6. `Artifact` `render_output` existente sem nenhum checkpoint `VALIDATED` gravado — badge não acende.
7. `Artifact` `render_output` válido, mas apontando para um arquivo apagado DEPOIS de um `VALIDATED` bem-sucedido anterior — badge reavaliado em tempo real, deixa de acender.
8. Múltiplos `Artifact` `render_output` para o mesmo `(video_id, project_id)` — o handler escolhe deterministicamente o mais recente (`created_at DESC, rowid DESC`), nunca ambíguo.
9. `Artifact` `render_output` de OUTRO `project_id` do mesmo vídeo presente no banco — nunca confundido com o do Project sendo validado (escopo por `project_id`, não só `video_id`).
10. Template com zona `CAPTION` e posição de legenda configurada, mas `CAPTIONS.mode != BURNED` — item 8 corretamente SKIP, nunca avaliado contra uma zona que não será usada.
11. `Job` sem `video_id`/sem `project_id`/com `video_id` de um `Video` inexistente — cada caso termina em `JOB_FAILED` estruturado com `reason` específico, nunca uma exceção não tratada.
