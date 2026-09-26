# PROMPT 42 — Render Engine (`_sistema/render_engine.py`)

## 1. Arquivos criados

- `_sistema/render_engine.py` (~1.520 linhas) — módulo `RenderEngine`, job handler `RENDER_VIDEO`. Docstring de módulo completa (seções 0-2) documenta cada decisão arquitetural com justificativa.
- `tests/test_render_engine.py` (~1.060 linhas, 75 testes) — builders puros, resolução de entradas, cache/idempotência, validação pós-render, badge, SourceAsset intocado, Job/checkpoint/Artifact, concorrência, restart, integração real com FFmpeg, AST estrutural.
- `PROMPT_42_RENDER_ENGINE_RELATORIO.md` (este arquivo).

## 2. Arquivos modificados

- `_sistema/media_catalog.py` — `BADGE_RENDERED` ganhou expressão SQL real em `_BADGE_SQL_EXPRESSIONS` (era `"0"`), seguindo exatamente o padrão duplo de `BADGE_REFRAMED_9_16`/`BADGE_TRANSCRIBED` (checkpoint `RENDERED` **E** `Artifact` `kind="render_output"` com arquivo real no disco — checkpoint sozinho nunca basta, exigência literal do roadmap: "a existência de arquivo parcial não basta"). Docstring do módulo (linhas ~97-121) atualizada para remover `RENDERED` da lista de badges estruturalmente impossíveis e documentar a nova fonte de verdade.
- `tests/test_media_catalog.py` — dois testes pré-existentes que hardcodavam `RENDERED` como membro de `IMPOSSIBLE_BADGES_TODAY` foram atualizados para refletir a nova realidade (mesmo ajuste já feito antes para `CAPTIONS`/`TRANSCRIBED`/`REFRAMED_9_16` em rodadas anteriores). Nenhuma lógica de produção tocada — só as expectativas do teste, que estavam desatualizadas pela própria natureza incremental do catálogo.
- `empacotar_release.py` — `PROMPT_42_RENDER_ENGINE_RELATORIO.md` adicionado a `ALLOWED_ROOT_FILES` (ordem alfabética).

Nenhum outro módulo (`template_engine.py`, `template_selector.py`, `audio_engine.py`, `timeline_editor.py`, `visual_editor.py`, `captions_engine.py`, `captions_style.py`, `auto_reframe.py`, `metadata_manager.py`, `job_engine.py`, `storage_manager.py`) foi tocado — confirmado por `find . -newer <marco> -name "*.py"`, que lista exatamente os 4 arquivos acima.

## 3. Comportamento novo

`RenderEngine.handle_render_job` é um handler de `JobEngine` (`operation="RENDER_VIDEO"`, `claims_status=PROCESSING`) que, para um `(video_id, project_id)`:

1. Resolve as **9 fontes de entrada** do roadmap (Source, CUTS/SPEED, CROP/VISUAL_ADJUSTMENTS/REFRAME, TEMPLATE, CAPTIONS, TEXT_LAYERS, Images/assets, AUDIO_SETTINGS, METADATA_MODE) através dos módulos que já as possuem — nunca uma segunda leitura/validação paralela.
2. Calcula um `cache_key` determinístico (`compute_cache_key`) e verifica cache-hit por `(project_id, kind="render_output", fingerprint=cache_key)` — escopo por **Project**, não por vídeo (dois Projects do mesmo vídeo podem renderizar diferente).
3. Em cache-miss, monta e executa um **pipeline FFmpeg modular de 6 etapas** (trim/speed → crop/reframe/ajustes → áudio → template → legenda queimada → encode final), cada etapa uma função pura `build_*_command` que devolve `list[str] | None` (`None` = etapa pulada).
4. Valida o resultado via `MediaProbe.probe_deep` (nunca considera o render completo sem isso — exigência literal do roadmap).
5. Só então promove via `StorageManager.promote_to_final` (rename atômico), registra `Artifact` `kind="render_output"` e grava `CHECKPOINT_RENDERED`.

`BADGE_RENDERED` no catálogo agora acende com evidência dupla real (checkpoint + Artifact com arquivo no disco).

## 4. Decisões arquiteturais (atenção especial pedida pelo Prompt)

**(1) Decomposição do pipeline.** 6 etapas, cada uma uma função pura de módulo (não método), para satisfazer a exigência explícita de testabilidade isolada sem FFmpeg real. Ordem: cortes/velocidade → crop/reframe/ajustes → áudio → template → legenda → encode final (sempre executa, incondicional — garante ao menos uma invocação real de FFmpeg e é o ponto natural de validação). Justificativa completa de cada posição na ordem está na seção 1 da docstring do módulo.

**(2) ResourceManager — decisão: ADIAR.** `ResourceManager` (Prompt 18) está 100% não-instanciado em produção hoje. Integrá-lo aqui seria a primeira integração real, sem nenhum caso de uso testado para calibrar valores de CPU/RAM/GPU — overengineering sem necessidade demonstrada agora, e um décimo eixo de risco não testado no mesmo Prompt que já tem escopo máximo. **Risco documentado**: renders concorrentes hoje não têm nenhum controle de recursos do sistema operacional — comportamento herdado de todo o projeto, não uma regressão introduzida aqui. Nenhum mecanismo alternativo/paralelo foi inventado.

**(3) METADATA_MODE — ordenação.** `CLEAN` é dobrado DENTRO da própria etapa de encode final (`-map_metadata -1`, mesmas flags de `metadata_manager.py`), nunca como uma segunda passada sobre o output já renderizado (evitaria um segundo re-encode com perda, sem necessidade — `-map_metadata -1` é praticamente grátis junto do encode que já vai acontecer). `PROFILE` tratado como `KEEP` nesta etapa (mesma decisão de adiamento já tomada por `metadata_manager.py`).

**(4) Images/assets — fonte real.** Não existe hoje nenhuma fonte de "imagem solta" anexável a um Project além do próprio `Template.source_path`. Tratado como já coberto pelo item Template — nenhuma categoria nova inventada.

**(5) Checkset de validação pós-render.** `MediaProbe.probe_deep` (não `probe` raso — este é o entregável final): `valid is True`, `duration` presente e `> 0`, `width`/`height` presentes e `> 0` (ausência de largura/altura é o sinal de "sem stream de vídeo decodificável" — `MediaProbeResult` não tem um campo dedicado). Falha em qualquer checagem: apaga o temporário, `JOB_FAILED` com `reason="post_render_validation_failed"`, **nunca** promove/grava checkpoint/Artifact.

## 5. Migrations

Nenhuma. Nenhuma tabela nova, nenhuma coluna nova — `Job`/`Artifact` já suportam `kind`/`fingerprint` livres desde o schema original.

## 6. Testes automatizados executados

```
pytest tests/test_render_engine.py -q
→ 75 passed (inclui 2 testes de integração REAL com FFmpeg — RODARAM,
  não pulados: ffmpeg/ffprobe estão disponíveis neste ambiente
  (/usr/bin/ffmpeg, /usr/bin/ffprobe), confirmado explicitamente antes
  da execução)

pytest tests/test_media_catalog.py -q
→ 60 passed (2 testes pré-existentes atualizados para a nova realidade
  de BADGE_RENDERED, ver seção 2)

python3 -m compileall -q _sistema tests
→ sem erros

pytest tests/ -q
→ 2665 passed, 1 skipped, 36 subtests passed em 172.49s
  (baseline anterior: 2591 passed — 75 novos testes deste Prompt,
  1 skip pré-existente e não relacionado, zero regressões)
```

Cobertura da matriz obrigatória: builders puros de cada etapa (sem FFmpeg real); resolução de cada entrada opcional presente/ausente isoladamente e em combinação; cache/idempotência (incluindo a correção do fingerprint de TEMPLATE, ver seção 8); cache-hit por `(project_id, kind, fingerprint)`; falha de validação pós-render nunca promove; falha do backend FFmpeg nunca promove; `BADGE_RENDERED` positivo e 3 variantes negativas (checkpoint sozinho, Artifact sozinho, Artifact apontando para arquivo apagado); `SourceAsset` original nunca tocado (hash+mtime antes/depois); Job/checkpoint/Artifact como autoridade única; cancelamento antes do pipeline (checagem proativa do próprio handler); concorrência determinística com duas instâncias TOTALMENTE independentes (própria `LocalDatabase`/`StorageManager`/etc., mesmo padrão de `test_auto_reframe.py`); restart com instâncias novas sobre o mesmo `.db`; 2 testes de integração real com FFmpeg (cortes+áudio+crop, validação via `MediaProbe` real); AST estrutural (nenhuma referência a `CHECKPOINT_VALIDATED`/`BADGE_VALIDATED`/`FinalMediaValidator`, nenhuma instanciação de `ResourceManager`).

## 7. Como testar manualmente

1. Criar um `Project` para um `Video` com `SourceAsset.local_path` apontando a um MP4 real.
2. Opcionalmente configurar `TimelineEditor`/`VisualEditor`/`AudioEngine`/`TemplateSelector`/`CaptionsEngine`/`AutoReframeEngine`/`METADATA_MODE` para o Project.
3. Registrar `RenderEngine.OPERATION` no `JobEngine`, criar um `Job` com esse `operation` e chamar `job_engine.advance(job.id)`.
4. Verificar `MediaCatalogService(database).get_item(video_id).system_badges` — `RENDERED` deve acender.
5. Rodar de novo (mesmas decisões) — deve ser cache-hit (nenhuma chamada nova de FFmpeg, mesmo `Artifact` reutilizado).

## 8. Riscos conhecidos

- **Concorrência entre instâncias distintas de `StorageManager` promovendo para o MESMO `final_path` simultaneamente**: `StorageManager._promotion_claims` é um conjunto por-instância — duas instâncias diferentes (dois processos/threads reais) promovendo para o mesmo `cache_key` ao mesmo tempo não colidem no claim (cada uma vê seu próprio conjunto vazio) e ambas promovem com sucesso via `overwrite=True`, resultando em **duas linhas de `Artifact`** para o mesmo `cache_key` (comportamento idêntico e já aceito por `auto_reframe.py`/`captions_engine.py` — nenhuma deduplicação entre processos concorrentes foi prometida por nenhum destes módulos). Documentado e testado explicitamente (`test_duas_instancias_concorrentes_processando_o_mesmo_project`) — nenhum dos dois falha, nenhum promove um arquivo inválido, é apenas uma duplicação de evidência aceitável.
- Sem `ResourceManager` (decisão de adiar, seção 4) — nenhum controle de CPU/RAM/GPU entre renders concorrentes.

## 9. Dívida técnica criada

- **REFRAME aplicado como posição ESTÁTICA** (média dos keyframes da curva de tracking), não dinâmica por frame. Documentado explicitamente na docstring do módulo (seção 0.3) como simplificação deliberada — aplicação dinâmica exigiria uma expressão de tempo no filtro `crop`, escopo comparável a um Prompt inteiro à parte.
- **Queima de legenda usa um SUBSET de `CaptionsStyleState`** via `force_style` do filtro `subtitles` (`FontName`/`FontSize`/`Alignment`/`Outline`/`Shadow`/aproximação de `position` via `MarginV`). `background`/`animation`/`current_word_highlight` não têm mapeamento direto nesse filtro simples — exigiriam gerar um `.ass` completo com estilos por palavra. Documentado.
- **TEXT_LAYERS** lido (arquitetura pronta) mas não processado — não existe schema/produtor real ainda.
- **ResourceManager** não integrado (seção 4).

## 10. Pendências

- Nenhuma pendência dentro do escopo deste Prompt. Fora de escopo (explicitamente, por instrução do Prompt): `CHECKPOINT_VALIDATED`/`BADGE_VALIDATED` (Prompt futuro "FinalMediaValidator"), qualquer Connector, UI, produtor real de `TEXT_LAYERS`, integração com `BatchEngine`.

---

## Ataques adversariais executados (GATE, item 15)

1. Duas instâncias de `RenderEngine`/`StorageManager`/`JobEngine` completamente independentes renderizando o MESMO Project simultaneamente (Barrier determinístico) — nenhuma corrompe, nenhuma falha, nenhuma promove arquivo inválido.
2. Restart com instâncias TOTALMENTE NOVAS de `LocalDatabase`/`EditProjectManager`/`StorageManager`/`TemplateEngine`/`OperationalAuditLog`/`RenderEngine`/`JobEngine` sobre o mesmo arquivo `.db` — cache-hit reconhecido corretamente após reinício, backend FFmpeg não chamado de novo.
3. Backend FFmpeg que lança exceção — handler termina em `JOB_FAILED` estruturado, nunca deixa `PROCESSING` pendurado, nunca promove.
4. Probe pós-render que reporta resultado inválido (simulando corrupção) — handler apaga o temporário, `JOB_FAILED`, zero `Artifact`/checkpoint gravado.
5. Cancelamento solicitado enquanto o Job está `PROCESSING` — checagem proativa do próprio handler intercepta antes do pipeline, `JOB_CANCELLED`, zero `Artifact`.
6. `Artifact` `render_output` apontando para um arquivo fisicamente apagado do disco — `BADGE_RENDERED` corretamente NÃO acende (a expressão SQL checa o arquivo real via `_CATALOG_FS_FUNCTION_NAME`).
7. Checkpoint `RENDERED` gravado sem nenhum `Artifact` correspondente — badge não acende (evidência simples nunca basta).
8. `Artifact` `render_output` existente sem nenhum checkpoint `RENDERED` gravado — badge não acende.
9. **Achado por teste, corrigido em produção**: `template.updated_at` como componente do `cache_key` tem resolução de segundo inteiro — duas edições do mesmo template no mesmo segundo produziam o MESMO `updated_at`, deixando o cache_key idêntico apesar do template fisicamente alterado. Corrigido substituindo por `compute_template_content_fingerprint` (hash de `source_path`/`layout`/`extra`), robusto a qualquer granularidade de timestamp — teste de regressão dedicado adicionado.
10. `_round_even` aplicado incorretamente a deslocamentos (x/y) de crop — forçava um deslocamento `0` legítimo para `2`, corrompendo geometria. Corrigido com `_round_offset` dedicado para deslocamentos, mantendo `_round_even` só para largura/altura (onde a paridade É exigida pelo H.264).
11. `SourceAsset.local_path` — hash SHA-256 e `mtime_ns` idênticos antes/depois de um render completo (nunca aberto em modo de escrita).
