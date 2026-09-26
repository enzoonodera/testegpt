# PROMPT 45 — Detecção local de melhores momentos (scores internos, sem API externa) — Relatório de Entrega

## 0. Texto literal do Prompt

> Smart Clip deve funcionar sem Google Analytics e sem API externa
> obrigatória. Usar sinais locais: transcrição, contexto, ritmo de fala,
> perguntas, respostas, hooks, histórias, emoção, frases fortes,
> mudanças visuais, áudio, completude do raciocínio. Gerar candidatos
> com scores internos. Não chamar score de "visualizações" ou "retenção
> real".

Segundo Prompt da Fase 7. Consome os `SemanticSegment` do Prompt 44,
calcula sub-scores auditáveis a partir de sinais locais e gera um
candidato por segmento, com um `overall_score` interno — nunca nomeado
como métrica real de audiência.

## 1. Arquivos criados

- `_sistema/smart_clip_scoring.py` (novo, ~640 linhas) — módulo completo:
  docstring de decisões, `ActivitySample`, `ClipCandidate`, funções puras
  (`score_context`, `score_speech_rate`, `score_hook`, `score_completion`,
  `combine_overall`, `compute_cache_key`), classe `SmartClipScoringEngine`
  (Job handler `handle_score_job`).
- `tests/test_smart_clip_scoring.py` (novo, ~440 linhas, **38 testes**).
- `PROMPT_45_SMART_CLIP_LOCAL_SCORING_RELATORIO.md` (este arquivo).

## 2. Arquivos modificados

- `empacotar_release.py` — adicionado
  `"PROMPT_45_SMART_CLIP_LOCAL_SCORING_RELATORIO.md"` a
  `ALLOWED_ROOT_FILES` (ordem alfabética preservada).

**Nenhum outro arquivo foi tocado** — confirmado por hash
(`sha256sum`) antes/depois desta entrega: `_sistema/smart_clip_engine.py`,
`_sistema/captions_engine.py`, `_sistema/auto_reframe.py`,
`_sistema/silence_removal.py` e `_sistema/domain/checkpoints.py`
permanecem byte-idênticos.

## 3. Comportamento novo

`SmartClipScoringEngine.handle_score_job(job)` — novo Job handler,
operação `SCORE_CLIP_CANDIDATES`:

1. Exige `job.video_id` (falha honesta se ausente).
2. Localiza o Artifact `content_segments_track` (Prompt 44) mais recente
   e válido para o vídeo; se ausente, aciona o Job real de
   `SmartClipEngine` (`ANALYZE_CONTENT`) através de um `JobEngine`
   interno e privado — que por sua vez aciona a transcrição, se também
   ausente. Nenhuma das duas lógicas é duplicada.
3. Calcula seu próprio `cache_key` (fingerprint da segmentação de origem
   + todos os parâmetros de scoring + versão do algoritmo). Reaproveita
   (`cache_hit: True`) se já existe.
4. Caso contrário, para cada `SemanticSegment` da segmentação de origem,
   calcula 6 sub-scores auditáveis (`hook_score`, `context_score`,
   `speech_score`, `completion_score`, `visual_score`, `audio_score`),
   combina num `overall_score` (média ponderada, pesos configuráveis) e
   monta um `reason` legível explicando os sinais que mais contribuíram.
5. Persiste um novo Artifact `kind="clip_candidates_track"` com um
   candidato por segmento — sem expandir bordas, sem deduplicar (Prompt
   47), sem descartar nem duplicar nenhum segmento.
6. **Nenhum checkpoint novo é gravado.** Idempotência é resolvida
   inteiramente por `(video_id, kind, fingerprint)` sobre o próprio
   Artifact de candidatos.

## 4. Decisões arquiteturais (com justificativa)

### 4.1 — Doze sinais do roadmap → seis sub-scores computáveis hoje

Investigação real do código mostrou que nem todo sinal listado no
roadmap tem um backend real disponível:

| Sinal do roadmap | Sub-score | Como é calculado |
|---|---|---|
| transcrição | (base de todos) | texto real de cada `SemanticSegment` |
| contexto | `context_score` | `role`/`topic_change` já calculados pelo Prompt 44 |
| ritmo de fala | `speech_score` | palavras / duração do segmento |
| perguntas | `hook_score` (categoria `question`) | presença de `"?"` |
| respostas | `hook_score` (categoria `answer_cue`) | léxico de conclusão/resposta |
| hooks | `hook_score` (categoria `hook`) | léxico de gancho de atenção |
| histórias | `hook_score` (categoria `story`) | léxico de narrativa pessoal |
| emoção | `hook_score` (categoria `emotion`) | léxico de emoção forte |
| frases fortes | `hook_score` (categoria `strong_phrase`) | léxico de afirmação categórica |
| completude do raciocínio | `completion_score` | pontuação final + `role == CONCLUSION` |
| mudanças visuais | `visual_score` | **backend injetável, sem implementação real hoje** |
| áudio | `audio_score` | **backend injetável, sem implementação real hoje** |

Perguntas/respostas/hooks/histórias/emoção/frases fortes foram agrupados
sob `hook_score` porque são, na prática, a mesma pergunta ("o quanto este
trecho isolado prende atenção") — mas cada categoria é reportada
SEPARADAMENTE em `hook_signals` e no `reason`, nunca colapsada num número
sem explicação.

### 4.2 — `visual_score`/`audio_score`: a decisão mais importante deste Prompt

Achado crítico herdado da investigação de `auto_reframe.py`/
`silence_removal.py`: `_default_detect` (visual) e
`_default_detect_silences` (áudio) são STUBS DELIBERADOS — nenhuma
biblioteca real de visão computacional ou análise de áudio está
integrada nesta etapa do produto. Este módulo herda a mesma decisão
honesta, com backends PRÓPRIOS (`VisualActivityBackend`/
`AudioActivityBackend`) — deliberadamente NÃO reaproveitando os tipos de
`auto_reframe`/`silence_removal`, porque respondem perguntas diferentes
(geometria de enquadramento; intervalos de silêncio pra corte editorial)
e reaproveitar por conveniência violaria autoridade única (GATE item 7).

Quando o backend (próprio, padrão) devolve `available=False`, o
sub-score correspondente é persistido como **`None`**, nunca `0.0` —
`0.0` pareceria "sinal real, e ele é ruim", quando na verdade é "não
sabemos". `combine_overall` exclui sub-scores `None` do cálculo e
RENORMALIZA os pesos restantes — a ausência de visual/áudio nunca
penaliza silenciosamente um candidato. Coberto por teste dedicado que
prova que tratar ausência como `0.0` (fabricado) produziria um resultado
diferente (menor) do que a renormalização correta.

### 4.3 — Vocabulário proibido: nunca "visualizações"/"retenção real"

Testado estruturalmente: nenhum trecho de CÓDIGO (fora da docstring de
módulo, que precisa poder EXPLICAR a regra) usa esses termos, e nenhum
campo de `ClipCandidate` usa nomes como `views`/`watch_time`. Os nomes
usados são sempre `score`/sub-scores nomeados pelo sinal de origem.

### 4.4 — Nenhum checkpoint novo

`domain/checkpoints.py` é vocabulário fechado por design.
`CHECKPOINT_ANALYZED` já foi consumido pelo Prompt 44. Inventar um
checkpoint novo seria mudar um módulo fechado sem necessidade
arquitetural demonstrada; reutilizar um existente seria desonesto (GATE
item 12 — o nome do checkpoint precisa corresponder ao que ele prova).
A idempotência deste Prompt é resolvida inteiramente pelo par
`(video_id, kind, fingerprint)` sobre o Artifact `clip_candidates_track`
— o mesmo padrão que `FinalMediaValidator` já usa para não precisar de um
checkpoint dedicado por checagem. Testado estruturalmente (nenhuma
chamada a `record_checkpoint`, nenhuma constante `CHECKPOINT_*` nova).

### 4.5 — Um candidato por segmento, sem expansão/dedup

Candidatos usam EXATAMENTE os limites (`start`/`end`) do
`SemanticSegment` de origem — nenhuma expansão de borda, nenhuma
deduplicação entre candidatos (ambos reservados ao Prompt 47,
explicitamente fora de escopo aqui). Coberto por teste que confirma que
os limites do candidato batem exatamente com os do segmento de origem.

### 4.6 — Encadeamento honesto: Scoring → Segmentação → Transcrição

Mesmo padrão de "JobEngine interno e privado" já estabelecido no Prompt
44 (lá, justificado em detalhe): `SmartClipScoringEngine` constrói um
`SmartClipEngine` (se não injetado) e um `JobEngine` privado para
acioná-lo quando a segmentação ainda não existe — que por sua vez aciona
a transcrição, se também ausente. Nenhuma das duas lógicas é
reimplementada; cada camada só é responsável pela SUA etapa.

## 5. Migrations

Nenhuma. Reaproveita `jobs`/`artifacts` já existentes; nenhuma tabela
nova, nenhum checkpoint novo.

## 6. Testes automatizados executados

`tests/test_smart_clip_scoring.py` — **38 testes**, cobrindo:

1. Cada sub-score isolado (`score_context`, `score_speech_rate`,
   `score_hook`, `score_completion`) com casos de borda (ritmo muito
   lento/rápido, duração zero, sem palavras, sem nenhum sinal de hook,
   frase cortada no meio).
2. `combine_overall` com todos os sinais disponíveis, com visual/áudio
   ausentes (renormalização), e prova explícita de que ausência nunca é
   tratada como `0.0` fabricado.
3. `compute_cache_key` — determinístico, sensível a cada parâmetro
   (segmentação de origem, pesos, faixa de ritmo).
4. **Backends sem implementação real nunca fabricam valor** — teste
   dedicado confirmando `visual_score`/`audio_score` `None` com o backend
   padrão, e um segundo teste confirmando que um backend INJETADO real é
   de fato usado (nunca ignorado).
5. **Teste estrutural**: nenhum trecho de código (fora da docstring de
   módulo) nomeia o score como "visualizações"/"retenção real"/"views";
   nenhum campo do candidato usa esses nomes.
6. Um candidato por segmento, sem descarte/duplicação, usando exatamente
   os limites do segmento de origem.
7. Cache reaproveitado entre dois Jobs quando nada muda (nunca duplica o
   Artifact); recalculado quando os pesos mudam (dois cache_keys
   diferentes → dois Artifacts).
8. **Nenhum checkpoint novo é gravado** — testado tanto no runtime
   (nenhum `JOB_CHECKPOINTS` foi atingido pelo Job) quanto
   estruturalmente (nenhuma chamada a `record_checkpoint`, nenhuma
   constante `CHECKPOINT_*` nova no módulo).
9. Falha honesta sem `video_id`; acionamento em cadeia até a transcrição
   quando nada existe ainda; falha honesta quando a segmentação
   subjacente falha.
10. Construção — validação de tipos/valores (`manager` inválido, faixa de
    ritmo invertida, `weights` vazio).
11. Contrato Job/Artifact — restart com instâncias TOTALMENTE novas de
    `LocalDatabase`/`EditProjectManager`/`StorageManager`/`CaptionsEngine`/
    `SmartClipEngine`/`SmartClipScoringEngine` contra o mesmo arquivo
    `.db`.

Resultado: **38 passed** (isolado) e **suíte completa: 2800 passed,
1 skipped, 36 subtests passed** — baseline anterior era 2762 (Prompt 44),
crescimento de exatamente 38, **zero regressões**.

```
2800 passed, 1 skipped, 36 subtests passed in 164.43s (0:02:44)
```

`python3 -m compileall -q _sistema tests` — sem erros.

## 7. Como testar manualmente

Não há UI nova neste Prompt. Verificação via Python:

```python
from _sistema.smart_clip_scoring import SmartClipScoringEngine, OPERATION_SCORE_CANDIDATES
# construir com manager/database/storage_manager/app_paths reais
# criar um Job(video_id=..., operation=OPERATION_SCORE_CANDIDATES)
# chamar engine.handle_score_job(job)
```

O Artifact resultante (`kind="clip_candidates_track"`) é um JSON legível
com a lista de candidatos, cada um com todos os sub-scores, `hook_signals`
(quais categorias dispararam), `overall_score` e `reason`.

## 8. Riscos conhecidos

- Todos os léxicos (hook/story/emotion/strong_phrase/answer_cue) são
  heurísticas modestas de v1, em português coloquial — podem não
  capturar todos os padrões de fala reais. São parâmetros do construtor,
  ajustáveis sem mudança de código.
- `context_score`/`completion_score` são aproximações honestas e
  documentadas como tais — nunca uma compreensão semântica real do
  conteúdo (exigiria IA, fora de escopo).
- `visual_score`/`audio_score` continuam sem sinal real em produção até
  que um backend de visão computacional/áudio seja de fato integrado —
  comportamento conhecido e seguro por construção (nunca finge um sinal
  que não existe).
- Faixas de ritmo de fala (`1.5`-`3.5` palavras/s) não foram validadas
  empiricamente contra vídeos reais.

## 9. Dívida técnica criada

Nenhuma dívida nova deliberada. O módulo segue os padrões já
estabelecidos (Job/Artifact/cache_key determinístico, backend injetável
honesto, JobEngine interno para encadeamento) sem atalhos.

## 10. Pendências

- Prompt 46 (detecção de tipo de conteúdo) e Prompt 47 (expansão de
  bordas + deduplicação, usando os nomes de campo já reaproveitados aqui)
  constroem sobre esta base — nenhum foi antecipado.
- Quando um backend real de visão computacional/áudio existir, basta
  injetar `visual_activity_backend`/`audio_activity_backend` — nenhuma
  mudança estrutural necessária neste módulo.
- **Pendência não relacionada a este Prompt, ainda em aberto**: a
  correção Windows do teardown SQLite/WAL
  (`CORRECAO_WINDOWS_SQLITE_WAL_TEARDOWN_RELATORIO.md`) segue aguardando
  os 3 resultados de `RODAR_TESTES.bat` na máquina Windows real.

## Ataques adversariais adicionais executados (GATE item 15)

- Confirmado por teste que tratar ausência de visual/áudio como `0.0`
  fabricado produziria um `overall_score` MENOR do que a renormalização
  correta — provando que a decisão de excluir/renormalizar realmente
  protege contra penalização silenciosa.
- Confirmado que o teste estrutural de vocabulário proibido distingue
  corretamente entre "citar o termo na docstring para EXPLICAR a regra"
  (legítimo) e "usar o termo no código real" (proibido) — evitando um
  falso positivo que mascararia um falso negativo real no futuro.
- Confirmado que uma falha real na transcrição (exceção não capturada no
  backend injetado) propaga corretamente como `JOB_FAILED` honesto em
  toda a cadeia (Scoring → Segmentação → Transcrição), sem deixar nenhum
  Job "preso" em estado intermediário.
- Confirmado por teste que dois `SmartClipScoringEngine` com pesos
  diferentes sobre o MESMO vídeo produzem dois Artifacts distintos
  (`cache_key` diferentes), nunca um sobrescrevendo o outro
  silenciosamente.
- Confirmado por teste de restart com instâncias 100% novas (banco
  reaberto do zero) que o cache de candidatos sobrevive e é reconhecido
  corretamente.
- Confirmado por hash que `auto_reframe.py`, `silence_removal.py`,
  `domain/checkpoints.py`, `smart_clip_engine.py` e `captions_engine.py`
  permanecem byte-idênticos antes/depois desta entrega.
