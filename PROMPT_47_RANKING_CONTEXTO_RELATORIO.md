# PROMPT 47 — Ranking e contexto dos cortes (FASE 7, quarto prompt)

## 1. Arquivos criados

- `_sistema/smart_clip_ranking_engine.py` (~640 linhas) — módulo novo, classe `ClipRankingEngine`.
- `tests/test_smart_clip_ranking_engine.py` (~710 linhas, 28 testes).
- `PROMPT_47_RANKING_CONTEXTO_RELATORIO.md` (este arquivo).

## 2. Arquivos modificados

- `empacotar_release.py` — adicionado `"PROMPT_47_RANKING_CONTEXTO_RELATORIO.md"` a `ALLOWED_ROOT_FILES` (uma linha, ordem alfabética, imediatamente após a entrada do Prompt 46).

**Nenhuma mudança em** `smart_clip_engine.py`, `smart_clip_scoring.py`, `content_type_detector.py`, `domain/checkpoints.py`, `template_engine.py` — confirmado por SHA-256 idêntico antes/depois (seção 8).

## 3. Comportamento novo

`ClipRankingEngine` consome os `ClipCandidate` que `SmartClipScoringEngine` (Prompt 45) já produziu (Artifact `clip_candidates_track`) e:

1. **Expande bordas por evidência local, nunca recalculando sub-scores**, com três motivos independentes e auditáveis:
   - **Frase cortada** (estende o fim): dispara quando `completion_score` do candidato original é menor que `completion_expansion_threshold` (padrão `0.7`). Estende segmento a segmento até encontrar um cujo próprio `completion_score` já atinja o limiar, ou até não haver mais segmentos, ou até o teto ser alcançado.
   - **História sem contexto** (estende o início): dispara quando `role == "DEVELOPMENT"` e `topic_change is False`. Estende por exatamente um segmento anterior.
   - **Conclusão faltando** (estende o fim): dispara quando `role != "CONCLUSION"` e o próximo segmento tem `topic_change is False` (continua a mesma história). Estende por exatamente um segmento seguinte.
   - Todos os três respeitam um **único teto combinado** (`max_expansion_seconds`, padrão `20.0` segundos): a cada tentativa de expansão, o tempo total já adicionado (início + fim) é recomputado, e a expansão só é aplicada se permanecer dentro do teto — nunca por motivo isoladamente. Um candidato já bem formado nunca é expandido.
   - O texto final é sempre a concatenação literal dos textos dos segmentos incluídos — nunca inventa texto novo.
2. **Ranking** por `overall_score` descendente, desempate determinístico por `index` ascendente.
3. **Supressão de sobreposição excessiva**: em ordem de ranking, cada candidato é comparado apenas contra os já aceitos de rank mais alto; se a fração da sua PRÓPRIA duração já coberta por um deles superar `max_overlap_ratio` (padrão `0.5`), é suprimido — mas nunca desaparece: fica no Artifact com `accepted=False` e `suppressed_reason` explicando qual candidato causou a supressão e a razão de sobreposição calculada.
4. Persiste tudo como novo Artifact `ranked_clip_candidates_track`, com `duration` calculado, `algorithm_version` e `cache_key` determinístico.
5. Se `clip_candidates_track` não existir, aciona a cadeia real Scoring → Segmentação → Transcrição pelo mesmo padrão de `JobEngine` interno já usado nos Prompts 44/45/46.

## 4. Decisões arquiteturais

### 4.1 Nomes de campo: `start`/`end` (não `start_time`/`end_time`)

O roadmap descreve o conceito, não uma assinatura Python literal. `ClipCandidate` (Prompt 45, já aprovado) e `SemanticSegment` (Prompt 44) já usam `start`/`end`. Renomear quebraria compatibilidade sem necessidade (CLAUDE.md item 5). Decisão: manter `start`/`end` em toda a pipeline; o único campo genuinamente novo e ausente até aqui, `duration` (`end - start`), foi adicionado a `RankedClipCandidate`.

### 4.2 Limiar de "frase cortada" = 0.7 (não arbitrário)

`score_completion` (Prompt 45) devolve exatamente `0.7`/`1.0` quando o texto termina em pontuação terminal, e `0.3`/`0.6` quando não termina. `0.7` é portanto o valor EXATO que separa as duas categorias — nunca um número escolhido por tentativa e erro.

### 4.3 Teto de expansão combinado, não por motivo

O teste `test_teto_de_expansao_nunca_e_ultrapassado_com_multiplos_motivos_simultaneos` construiu um candidato com os três motivos disparando ao mesmo tempo e verificou, para sete valores de teto (`0.0` a `100.0`), que o total adicionado (início + fim) nunca excede o teto configurado — cada tentativa de expansão recomputa o total ANTES de commitar.

### 4.4 `coverage_ratio` — métrica de sobreposição assimétrica, não Jaccard

`coverage_ratio = segundos_de_intersecao / duration(candidato_avaliado)`. Escolha deliberada: um candidato curto inteiramente contido num candidato aceito muito mais longo deve ser suprimido (`ratio = 1.0`), mesmo que, do ponto de vista do candidato longo, a sobreposição seja uma fração pequena da própria duração dele. O inverso (um candidato longo que só tangencia um curto aceito) não é suprimido por essa tangência pequena. Testado explicitamente em `test_coverage_ratio_e_assimetrico_por_design`.

### 4.5 Supressão só compara contra ACEITOS, nunca contra suprimidos

Um candidato suprimido não é mostrado ao usuário, então não deveria impedir outro de aparecer só por também se sobrepor a ele. `rank_and_suppress` mantém uma lista `accepted` separada da lista de resultados completa — a comparação de cada novo candidato usa exclusivamente `accepted`.

### 4.6 `split_gap_seconds`/`split_discourse_marker` — reaproveitados, nunca recalculados

Quando uma expansão cruza a fronteira de um segmento, o módulo lê (best-effort, via `content_segments_track`) esses dois campos já calculados pelo Prompt 44 e os inclui no texto de auditoria (`expansion_reasons`) — evidência real e já medida (silêncio cronometrado ou marcador de discurso), nunca um número recalculado. Se o Artifact de segmentos não estiver mais disponível, a expansão continua funcionando normalmente usando só os `ClipCandidate` — a leitura dos segmentos é estritamente um enriquecimento de auditoria, nunca uma dependência dura.

### 4.7 Autoridade única (GATE item 7)

Este é o único módulo que decide bordas/ranking/overlap. Nunca recalcula `hook_score`/`context_score`/`speech_score`/`completion_score`/`visual_score`/`audio_score` — só consome o que `SmartClipScoringEngine` já produziu. Verificado por teste estrutural (`test_modulo_nao_referencia_checkpoints_novos`, `test_modulo_nao_importa_domain_checkpoints`) e por revisão manual: o módulo nunca importa nenhuma função `score_*` de `smart_clip_scoring.py`.

## 5. Migrations

Nenhuma. Novo Artifact `kind="ranked_clip_candidates_track"` — vocabulário de Artifact, não requer migration de schema (mesmo padrão dos Prompts 44/45/46).

## 6. Testes automatizados executados

`tests/test_smart_clip_ranking_engine.py` — **28 testes novos**, organizados em 14 seções:

1. Todos os campos do roadmap presentes + `duration` correto.
2. Expansão "frase cortada" — positivo e negativo (candidato já completo não é expandido).
3. Expansão "história sem contexto" — positivo e negativo (BEGINNING nunca expandido por esse motivo, mesmo com vizinho artificial presente).
4. Expansão "conclusão faltando" — positivo e negativo (CONCLUSION nunca expandido por esse motivo).
5. Teto de expansão nunca ultrapassado, com múltiplos motivos simultâneos (7 valores de teto testados).
6. Expansão nunca inventa texto (dois testes: ausência de vizinho anterior mantém texto original; concatenação usa apenas texto real dos segmentos incluídos).
7. Ranking determinístico entre execuções (ordem e ranks estáveis, desempate por índice).
8. Supressão de sobreposição — excessiva suprime e é auditável; leve aceita ambos; `coverage_ratio` assimétrico; sem interseção é zero.
9. Nenhum checkpoint novo gravado/referenciado (estrutural, com helper que ignora a docstring de módulo); nenhum import de `domain.checkpoints`.
10. Cache — reaproveita quando nada muda; recalcula quando um parâmetro (`max_overlap_ratio`) muda; nunca duplica Artifact.
11. Encadeamento honesto até scoring/segmentação/transcrição quando nada existe.
12. Falha honesta — sem `video_id`; candidato com schema inválido (endurecimento adicional, ver seção 9); quando scoring falha em cadeia.
13. Construção — validação (manager inválido, `completion_expansion_threshold`/`max_expansion_seconds`/`max_overlap_ratio` fora de faixa).
14. Restart com instâncias totalmente novas vê o mesmo resultado (mesmo cache_key, cache_hit).

**Resultado**: 28 passed (isolado). **Suíte completa**: 2858 passed, 1 skipped, 36 subtests passed — 2830 anteriores + 28 novos, **zero regressões**.

## 7. Como testar manualmente

1. `python3 -m compileall _sistema/smart_clip_ranking_engine.py` — sem erros.
2. `pytest tests/test_smart_clip_ranking_engine.py -v` — 28 testes, todos verdes.
3. `pytest tests/ -q` — suíte completa, sem regressão.
4. Fluxo real: criar um vídeo com transcrição real (via `CaptionsEngine`), acionar `ClipRankingEngine.handle_rank_job` num `Job` novo — a cadeia completa (Ranking → Scoring → Segmentação → Transcrição) deve rodar sozinha na primeira chamada, e a segunda chamada com o mesmo vídeo deve reaproveitar o cache (`cache_hit: True`).

## 8. Riscos conhecidos

- **Não atomicidade check-then-insert** (herdada dos Prompts 44/45/46, não introduzida aqui): entre a checagem de cache e a inserção do Artifact, duas chamadas concorrentes ao mesmo `handle_rank_job` para o mesmo `video_id` poderiam, em teoria, inserir dois Artifacts com o mesmo `fingerprint`. Este é um risco já aceito e documentado no design idêntico dos três Prompts anteriores (mesma arquitetura, mesmo grau de risco, nenhuma regressão introduzida por este Prompt).
- **`topic_change` sempre `True` para segmentos não-iniciais na implementação atual de `segment_transcript`** (Prompt 44): isso significa que, na prática, os gatilhos de "história sem contexto" e "conclusão faltando" (que dependem de `topic_change is False`) nunca disparam com segmentação real hoje — apenas com dados sintéticos de teste (como os construídos nos testes deste Prompt) ou se a lógica de `segment_transcript` mudar no futuro. Implementado exatamente como o roadmap descreveu (usando os campos reais já calculados), documentado honestamente aqui — corrigir isso seria uma mudança no Prompt 44, fora de escopo deste Prompt, e não foi feita sem evidência concreta de necessidade.

## 9. Dívida técnica criada

- Endurecimento adicional (não pedido explicitamente pelo roadmap, mas adicionado durante a revisão adversarial): um item malformado dentro de `clip_candidates_track` (schema inválido) agora falha honestamente com `reason="invalid_candidate_schema"` em vez de propagar uma exceção não tratada — trivial, sem dívida associada.

## 10. Pendências

- Prompt 48 (tela de seleção/override do usuário) não foi iniciado — fora de escopo explícito.
- A verificação `RODAR_TESTES.bat` 3× no Windows real (pendência da correção SQLite/WAL anterior) continua aberta.

## 11. Ataques adversariais adicionais executados (GATE item 15)

- Verificado que nenhuma função `score_*` de `smart_clip_scoring.py` é importada ou reimplementada neste módulo (autoridade única).
- Testado com teto de expansão extremamente pequeno (`0.0`) para confirmar que zero expansão é aplicada mesmo com múltiplos motivos disparando.
- Testado que um candidato suprimido não impede outro de ser aceito (comparação só contra `accepted`, nunca contra suprimidos).
- Testado `coverage_ratio` com duração zero e sem interseção (nunca divide por zero, nunca devolve valor negativo).
- Testado payload de candidato com schema inválido — falha honesta, nunca uma exceção não tratada.
- Testado restart com instâncias de banco totalmente novas.
- Confirmado, via SHA-256, que `smart_clip_engine.py`, `smart_clip_scoring.py`, `content_type_detector.py`, `domain/checkpoints.py` e `template_engine.py` permanecem byte-idênticos antes e depois deste Prompt.
