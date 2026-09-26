# PROMPT 48 — Tela de seleção do Smart Clip: relatório

Fase 7, quinto e último prompt. É uma "interface conceitual": lógica de negócio de tela, sem GUI, no mesmo padrão de `TimelineEditor`, `VisualEditor` e `TemplateSelector`.

## 1. Arquivos criados

- `_sistema/smart_clip_selection_screen.py`: classe `ClipSelectionScreen` e funções puras de apresentação.
- `tests/test_smart_clip_selection_screen.py`: 47 testes.
- `PROMPT_48_TELA_SELECAO_RELATORIO.md`: este relatório.

## 2. Arquivos modificados

- `tests/test_video_promotion.py`: uma única mudança, na allowlist do teste `test_apenas_video_promotion_e_legacy_migration_criam_video_no_projeto`.
  - **Evidência de que era necessária:** o teste procura o texto `Video(` em todo o `_sistema/` e só aceita `models.py`, `video_promotion.py` e `legacy_migration.py`.
  - Criar N `Video`s por corte exige construir `Video(...)` fora do `VideoPromotionService`, porque o contrato 1:1 dele impede N vídeos por origem.
  - A alternativa seria "esconder" a construção do grep, por exemplo com `Video.from_dict` ou um alias. Isso seria um teste desonesto (GATE 12).
  - O arquivo novo foi acrescentado à allowlist com uma docstring explicando o motivo. Foi o mesmo tipo de atualização consciente que o Prompt 27.5 fez no `LATEST_SCHEMA_VERSION` desse arquivo.

Nenhum módulo já aprovado de `_sistema/` foi alterado. Isso inclui `video_promotion.py`, `smart_clip_ranking_engine.py`, `timeline_editor.py`, `edit_project.py` e `domain/checkpoints.py`.

## 3. Comportamento novo

### Apresentação

`list_clips(ranked_artifact_id)` e `get_clip(...)` apresentam **apenas** os candidatos com `accepted=True` do Prompt 47, na ordem de rank.

Visão padrão:

| Campo | Exemplo |
|---|---|
| `label` | `"Corte 1"` |
| `title` | `"Você sabia que isso muda tudo?"` |
| `time_range` | `"00:12:21 → 00:13:05"` |
| `quality_label` | `"Excelente corte"` |
| `start` / `end` | limites vigentes, em segundos |
| `preview` | `{source_video_id, start, end}`, para o player tocar o trecho do **original** |
| `user_decision` | `PENDING` / `ACCEPTED` / `REJECTED` |
| `bounds_edited` | `true` / `false` |
| `promoted_video_id` / `promoted_project_id` | ids criados na promoção, ou `null` |

A visão padrão não tem nenhum score, `reason`, `rank` ou `role`.

A visão avançada é explícita (`include_scores=True`). Ela acrescenta `overall_score`, todos os sub-scores, `reason`, `expansion_reasons`, `rank`, `role` e `original_start`/`original_end`.

### Ações do usuário

Cada ação é um método validado e auditável:

- **`decide(id, idx, "ACCEPTED" | "REJECTED")`**
  - Aceitar promove o corte a Video + Project.
  - Rejeitar nunca cria nada.
  - Repetir a decisão vigente é no-op, sem evento extra.
  - Mudar de ideia acrescenta um evento novo e não apaga nada.
- **`edit_bounds(id, idx, start=, end=)`**
  - Exige números finitos, `>= 0` e `start < end`; `bool` e `str` são recusados.
  - Os limites originais continuam em `original_bounds()` e na visão avançada.
  - Depois que o corte é promovido, a edição falha com `CorteJaPromovidoError`, e o ajuste passa a ser feito no Project.
- **`select_all(id, decision="ACCEPTED")`**
  - Aplica a decisão a todos os cortes apresentáveis que ainda estão `PENDING`, numa única transação.
  - Nunca toca os suprimidos e nunca sobrescreve uma decisão que o usuário já tomou.
- **`history(id, idx=None)`**: devolve o histórico append-only.
- **`resume_pending_promotions(id)`**: recuperação explícita depois de crash.

### Candidato suprimido (`accepted=False` no 47)

- Nunca aparece na lista.
- `decide`, `edit_bounds` e `original_bounds` falham com `CorteSuprimidoError`, e nada é gravado.
- O campo `accepted` do 47 é uma decisão **automática** de sobreposição. A decisão do usuário tem nome próprio, `user_decision`, para os dois nunca se confundirem.

## 4. Decisões arquiteturais

### 4.1 Criação de Video/Project: por que não reaproveita `VideoPromotionService`

`VideoPromotionService.promote()` é idempotente por `source_asset_id`: devolve sempre o mesmo `Video` da origem. N cortes aprovados do mesmo vídeo longo precisam de N `Video`s, então `promote()` devolveria sempre o mesmo.

O contrato 1:1 dele está certo para o que ele faz (promover uma importação bruta). Por isso ele **não foi alterado nem chamado**. O teste AST `test_promocao_nunca_reaproveita_video_promotion_service` confirma que o módulo novo não importa nem chama `VideoPromotionService`, `video_promotion` ou `promote`. O schema já permite N `Video`s por origem, porque não há índice único em `videos.source_asset_id`.

**Caminho próprio.** Dentro de uma única `BEGIN IMMEDIATE`, na mesma transação:

1. evento de decisão;
2. `Video(source_asset_id = o mesmo da origem longa, name = "<origem> - corte HH:MM:SS-HH:MM:SS")`;
3. `Project(video_id = novo Video)`;
4. evento `SMART_CLIP_PROMOTED`, com o vínculo `source_video_id`/`candidate_index` e os limites congelados.

### 4.2 Chave de idempotência própria

A chave é `(ranked_artifact_id, candidate_index)`. Os ids do Video, do Project e do segmento inicial são `uuid5` determinísticos dessa chave, gerados por `promotion_ids()` com um namespace fixo.

A idempotência fica garantida em duas camadas:

1. **Checagem do evento.** Dentro da transação, se já existe um `SMART_CLIP_PROMOTED` para o corte, ele é reaproveitado.
2. **PRIMARY KEY do SQLite.** Mesmo se a checagem fosse contornada, a chave primária recusa um segundo `Video` do mesmo corte. Isso é testado.

### 4.3 Reaproveitamento de `TimelineEditor`/`Segment`

O Project do corte é inicializado pela API pública do Prompt 28:

1. `initialize_timeline(project_id, end, segment_id=<uuid5>)` cria `Segment(0, end)`;
2. `trim(start=start)` reduz para `Segment(start, end)`.

O resultado é exatamente o `Segment` relativo ao vídeo original que o `RenderEngine` já consome. O módulo não escreve em `edit_state`, não chama `EditProjectManager` e não define nenhuma classe de segmento; isso tem teste AST.

### 4.4 Persistência das decisões: `audit_events`, não Artifact-arquivo

O Prompt sugeriu, como exemplo, o padrão de Artifact do override do Prompt 46. Não usei esse padrão pelo seguinte motivo:

- A aceitação precisa ser gravada **na mesma transação** que cria o Video/Project (GATE 2 e 3).
- Um Artifact exige escrever um arquivo fora do SQLite, o que abre uma janela de crash com "decisão sem Video" ou "Video sem decisão".
- `audit_events` é append-only garantido pelos triggers da m002, entra na `BEGIN IMMEDIATE` e já é usado com tipos de evento próprios por `circuit_breaker` e `batch_engine`.

Os eventos usam `entity_type="SmartClipSelection"` e `entity_id=ranked_artifact_id`, com três tipos: `SMART_CLIP_USER_DECISION`, `SMART_CLIP_BOUNDS_EDITED` e `SMART_CLIP_PROMOTED`. O estado vigente é o replay dos eventos em ordem, e o mais recente vence (mesma convenção do Prompt 46).

### 4.5 Faixas da etiqueta qualitativa (`overall_score`)

| `overall_score` | Etiqueta |
|---|---|
| ≥ 0.75 | Excelente corte |
| ≥ 0.60 | Ótimo corte |
| ≥ 0.45 | Bom corte |
| < 0.45 | Corte possível |

- O hook pesa 0.30. Um corte sem gancho, mas com todo o resto perfeito, chega a 0.70. Por isso "Excelente" exige 0.75, ou seja, precisa de gancho real.
- 0.45 é o piso em que cerca de metade dos sinais ponderados está presente.
- Nenhuma etiqueta fala em audiência. "Mais assistido" é proibido pelo item H do CLAUDE.md.

### 4.6 Título/resumo sem IA

`derive_title` é um recorte literal do `text` já transcrito:

1. normaliza os espaços;
2. pega a primeira frase (até `.`, `!`, `?` ou `…` seguidos de espaço ou fim);
3. se passar de 60 caracteres, corta na última palavra inteira e acrescenta `…`.

Texto vazio vira `"Trecho sem fala detectada"`. Não há modelo nem chamada de rede; título por IA fica para a Fase 8.

### 4.7 Intervalo formatado

O formato é `HH:MM:SS → HH:MM:SS`, o formato literal do roadmap. O início é arredondado para baixo e o fim para cima, para o intervalo exibido sempre conter o corte real.

### 4.8 "Job normal"

Nenhum `Job` é criado. Criar um Job de render agora seria acionar render implicitamente, o que está fora de escopo. O Video/Project promovido é igual a qualquer outro, então o fluxo normal de Job (render, template, legenda) se aplica sem nada especial.

## 5. Migrations

Nenhuma. `LATEST_SCHEMA_VERSION` continua em 9; há teste para isso, e `test_migrations_frozen.py` passa.

Nenhum checkpoint novo: `domain/checkpoints.py` não é importado, e o teste confirma zero `JOB_CHECKPOINT_REACHED`.

## 6. Testes automatizados executados

- **Módulo novo:** `tests/test_smart_clip_selection_screen.py`, 47 testes, todos passando. O ranking dos testes é **real**: `ClipRankingEngine.handle_rank_job` do Prompt 47, com um candidato suprimido de verdade. Os testes de concorrência e crash foram repetidos 3 vezes, todos verdes.
- **Módulos relacionados:** `test_video_promotion.py`, `test_migrations_frozen.py`, `test_smart_clip_ranking_engine.py` e `test_timeline_editor.py` somam 162 testes junto com os novos, todos passando.
- **`compileall`** de `_sistema` e `tests`: OK.
- **Suíte completa:** 2845 passaram, 40 falharam e 21 foram pulados.

**As 40 falhas já existiam e são de ambiente.** Elas estão em `test_media_probe`, `test_auto_reframe`, `test_template_engine` e `test_template_importer`, e todas precisam de `ffmpeg`/`ffprobe`, que não estão instalados no container Linux onde a suíte rodou. Rodei exatamente essas 40 com as mudanças deste Prompt removidas (stash): as 40 falharam do mesmo jeito. Sem as mudanças, eram 2798 testes passando; com elas, 2845, ou seja, 2798 + os 47 novos.

### Ataques adversariais executados (GATE)

1. **Storage / split-brain:** um único `LocalDatabase`; o `TimelineEditor` é construído com o mesmo banco. Os testes concorrentes usam duas facades distintas do mesmo arquivo.
2. **TOCTOU:** em `decide`, `edit_bounds` e `select_all`, o replay dos eventos, a decisão e as escritas ficam na mesma `BEGIN IMMEDIATE`.
3. **Crash real** (`os._exit` num subprocesso, não uma exceção capturada), em três pontos:
   - antes do COMMIT: nada persiste;
   - depois do COMMIT e antes da timeline: o Project fica com a timeline vazia e `resume_pending_promotions` completa;
   - entre `initialize_timeline` e `trim`: `Segment(0, end)` fica pendente e um `decide` repetido completa.
4. **Restart:** novas instâncias de `LocalDatabase` e `ClipSelectionScreen` veem a mesma lista e o mesmo histórico, e a idempotência vale depois do restart.
5. **Idempotência:** aceite sequencial repetido, aceite concorrente (Barrier) e `select_all` concorrente (Barrier) resultam em exatamente um Video e um Project por corte. A PK recusa duplicata.
6. **Concorrência `edit_bounds` × `decide` (Barrier):** qualquer ordem termina consistente. Ou a edição é recusada e o corte é promovido com os limites originais, ou a promoção usa os limites editados. A timeline sempre bate com o evento.
7. **Autoridade única:** o módulo não recalcula score, borda ou supressão. O `accepted` do 47 é só lido.
8. **Construção:** o `__init__` só valida o tipo e não muta nenhuma dependência.
9. **Histórico:** os limites da promoção ficam congelados no evento. Mudar de ideia acrescenta evento, e o `DELETE` em `audit_events` é negado pelo SQLite (testado).
10. **Segredos:** um JSON ilegível contendo `token=...` gera um erro sem esse conteúdo; só o nome da classe da exceção aparece. Nenhum `str(exc)` é persistido.
11. **Windows:** o módulo não abre, renomeia nem apaga arquivos; só lê o JSON do ranking.
12. **Handler/timeline:** `_ensure_timeline` só age nos dois estados que ele mesmo produz. Uma edição do usuário no Project, como um `split`, nunca é sobrescrita por `resume` nem por `decide` repetido (testado).

## 7. Como testar manualmente

```python
from _sistema.storage.database import LocalDatabase
from _sistema.smart_clip_selection_screen import ClipSelectionScreen

db = LocalDatabase("<painel.db>")
tela = ClipSelectionScreen(db)
rid = tela.latest_ranked_artifact_id("<video_id com ranking do Prompt 47>")
for c in tela.list_clips(rid):
    print(c["label"], c["title"], c["time_range"], c["quality_label"])
tela.edit_bounds(rid, <idx>, start=..., end=...)
tela.decide(rid, <idx>, "ACCEPTED")   # cria Video+Project com Segment(start, end)
tela.select_all(rid)                   # aceita os pendentes restantes
tela.list_clips(rid, include_scores=True)  # visão avançada
tela.history(rid)                      # auditoria
```

## 8. Riscos conhecidos

- **Rejeitar depois de aceitar não apaga o Video/Project criado.** Isso é intencional (não destrutivo), e a view mostra `REJECTED` junto com os ids promovidos. Ainda não existe um fluxo de "arquivar" esse Video.
- **Um ranking novo gera cortes novos.** Se o ranking for refeito com outros parâmetros, surge outro `ranked_artifact_id` e outra chave. O mesmo trecho aprovado nos dois rankings vira dois Videos, porque a idempotência vale por ranking.
- **O Project só consegue encolher o corte.** `initialize_timeline` recebe `end` como duração, já que nenhuma duração do original é persistida hoje. Alargar o corte é feito com `edit_bounds` antes de aceitar.
- **`edit_bounds` não valida contra a duração real do arquivo,** pelo mesmo motivo da seção 0.3 do `timeline_editor.py`.

## 9. Dívida técnica criada

- `test_video_promotion.py` agora tem 4 módulos na allowlist de `Video(`. Se surgir um terceiro criador legítimo, vale trocar o grep textual por uma lista centralizada de criadores autorizados.
- A leitura do estado faz replay de todos os eventos do ranking a cada chamada. É linear no número de ações; está ok para centenas de cortes, mas pode precisar de índice ou snapshot em volumes muito maiores.

## 10. Pendências

- Não há Job de render automático para cortes aprovados; render continua sendo uma ação explícita.
- Título/resumo por IA fica para a Fase 8 (Prompts 49/50).
- A GUI real (player de preview e botões) fica para quando o frontend for construído.
