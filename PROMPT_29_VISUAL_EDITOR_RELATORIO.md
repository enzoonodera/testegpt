# PROMPT 29 — Editor: vídeo e imagem — Relatório de entrega

## 0. Contexto e escopo (recapitulando o Prompt)

Texto literal do roadmap (Fase 5, imediatamente após o Prompt 28):

> PROMPT 29 — Editor: vídeo e imagem
> Crie módulo de transformações visuais.
> Suportar: crop; resize; fit/fill; zoom; position; rotation quando
> necessário; brightness; contrast; saturation; gamma; sharpen; noise;
> ajustes existentes no protótipo.
> Tudo deve poder funcionar sozinho e em lote.

O Prompt trouxe uma investigação prévia obrigatória (seção 0) confirmando:
(0.2) `EditProjectManager`/`update_category` (Prompts 27/28) continuam
sendo a única camada de persistência, com `CROP`/`REFRAME` já reservados
como nomes de conveniência; (0.3) `_sistema/limpar_metadados_oficial.py`
(Geração 1) já aplica, em produção, uma cadeia de filtros ffmpeg com zoom
progressivo/eq/colorbalance/vignette/noise/unsharp — usado só como
referência, nunca importado; (0.4) nem `Video`/`SourceAsset` têm resolução
persistida hoje, então toda validação geométrica precisa ser
autocontida (nunca contra o arquivo real); (0.5) nenhum badge novo é
necessário no Media Catalog.

## 1. Arquivos criados

- `_sistema/visual_editor.py` (654 linhas) — novo módulo de
  transformações visuais não destrutivas: classe `VisualEditor`,
  dataclasses `FrameState`/`AdjustmentsState`/`BulkVisualEditResult`, 4
  classes de erro estruturado, e todas as operações do roadmap
  (`set_crop`, `set_resize`, `set_fit_mode`, `set_zoom`, `set_position`,
  `set_rotation`, `set_brightness`, `set_contrast`, `set_saturation`,
  `set_gamma`, `set_sharpen`, `set_noise`, mais `bulk_set_frame`/
  `bulk_set_adjustments`).
- `tests/test_visual_editor.py` (732 linhas, 83 testes coletados) —
  suíte dedicada e permanente cobrindo a lista obrigatória do Prompt
  (seção 4), mais 1 achado do GATE ADVERSARIAL (seção 6 abaixo).
- `PROMPT_29_VISUAL_EDITOR_RELATORIO.md` — este relatório.

## 2. Arquivos modificados

- `empacotar_release.py` — `PROMPT_29_VISUAL_EDITOR_RELATORIO.md`
  adicionado a `ALLOWED_ROOT_FILES` (arquivo opcional, não bloqueia build
  na ausência).

Nenhum outro arquivo de produção foi tocado. Em particular, **não foram
alterados**: `timeline_editor.py`, `edit_project.py`, `media_catalog.py`,
`video_promotion.py`, `media_probe.py`, nenhuma migration existente
(`m001`–`m009`). Este Prompt não precisou de nenhuma extensão em
`EditProjectManager` — `get_category`/`set_category`/`update_category`
já existentes (do Prompt 28) foram suficientes.

## 3. Comportamento novo e decisões arquiteturais

### 3.1 — Duas categorias: `CROP` (enquadramento) e `visual_adjustments` (nova, imagem/cor)

`crop`/`resize`/`fit_mode`/`zoom`/`position`/`rotation` vivem juntos na
categoria já reservada `CROP` — são geometricamente interdependentes (um
`rotation` de 90° muda o que `crop`/`position` significam espacialmente,
`zoom` interage com `crop`), mesmo raciocínio já usado no Prompt 28 para
decidir que `speed` vive dentro de `cuts`.

`brightness`/`contrast`/`saturation`/`gamma`/`sharpen`/`noise` vivem numa
categoria NOVA, `visual_adjustments` — são ajustes de imagem/cor sem
nenhuma interdependência geométrica com o enquadramento. Categorias
separadas preservam a granularidade por categoria do Prompt 27 (mudar o
brilho nunca recomputa fingerprint/revisão do enquadramento, e
vice-versa). `EditProjectManager` não exige que toda categoria esteja
pré-declarada como constante — `"visual_adjustments"` é uma string livre
válida.

`REFRAME` (reservado ao Prompt 33, Auto Reframe) não foi usado — este
Prompt é só sobre decisões manuais do usuário, nunca detecção automática.

### 3.2 — Schema

Categoria `CROP` — `data`:

```json
{
  "crop": {"x": 0.0-1.0, "y": 0.0-1.0, "width": ">0", "height": ">0"},
  "resize": {"width": "int 1-7680", "height": "int 1-7680"},
  "fit_mode": "FIT | FILL | STRETCH",
  "zoom": "float 1.0-4.0",
  "position": {"x": -1.0 a 1.0, "y": -1.0 a 1.0},
  "rotation": "0 | 90 | 180 | 270"
}
```

Categoria `visual_adjustments` — `data`:

```json
{
  "brightness": "-1.0 a 1.0 (neutro 0.0)",
  "contrast": "0.0 a 3.0 (neutro 1.0)",
  "saturation": "0.0 a 3.0 (neutro 1.0)",
  "gamma": "0.1 a 3.0 (neutro 1.0)",
  "sharpen": "0.0 a 5.0 (neutro 0.0)",
  "noise": "0.0 a 1.0 (neutro 0.0)"
}
```

Cada chave é INDEPENDENTE — ausente significa "não definido", nunca um
valor neutro implícito gravado automaticamente. Cada `set_*` só escreve a
sua própria chave, preservando as demais já definidas (prova de "funciona
sozinho", item 1.d do Prompt).

### 3.3 — Coordenadas normalizadas para `crop`/`position` (decisão-chave para validação sem arquivo real)

`crop`/`position` usam frações do frame (`0.0`–`1.0`), não pixels — isso
torna a validação geométrica (`x + width <= 1.0`, etc.) inteiramente
autocontida, sem precisar conhecer a resolução real do vídeo (que não
existe persistida, seção 0.4 do Prompt). `resize` usa pixels absolutos
(um alvo de saída é inerentemente absoluto), validado apenas
estruturalmente (inteiros positivos, teto de produto de 7680px — 8K —
nunca comparado contra a resolução real de origem).

### 3.4 — `rotation`/`fit_mode`: vocabulário fechado, nunca string livre

`rotation` limitado a `{0, 90, 180, 270}` — um ângulo arbitrário exigiria
matemática de bounding-box dependente da resolução real (indisponível);
múltiplos de 90° preservam retângulo e cobrem o caso real do produto
(correção de orientação vertical/horizontal). `fit_mode` limitado a
`{"FIT", "FILL", "STRETCH"}`.

### 3.5 — "Sozinho e em lote" (item 1.d do Prompt)

SOZINHO: cada `set_*` funciona isoladamente, sem exigir as demais
transformações definidas (schema com chaves independentes).

EM LOTE: `bulk_set_frame`/`bulk_set_adjustments` aplicam os mesmos campos
a vários `project_id`, no mesmo espírito de `BulkEditResult` (Prompt
27.5) — `requested`/`updated`/`unchanged`/`failed`. Os valores são
validados UMA VEZ, antes de tocar qualquer projeto (falha rápida e
idêntica para todos); só a existência do projeto é resolvida por item do
lote — um `project_id` inexistente/inválido nunca aborta os demais.

### 3.6 — Sem leitura separada para classificar `updated`/`unchanged` no lote

A primeira versão de `_bulk_apply` fazia um `get_category()` ANTES do
`update_category()` só para comparar revisões e decidir se o item entra
em `updated` ou `unchanged`. Identificado durante a implementação (antes
mesmo de rodar testes) que isso reabriria, para fins de CLASSIFICAÇÃO
(não para a escrita em si, que já é atômica), o mesmo padrão
`read()`/`decide()` fora de uma única transação que o Prompt 28
eliminou — outra chamada concorrente poderia mudar a revisão entre a
leitura e a escrita, tornando a classificação `updated`/`unchanged`
imprecisa sob concorrência. Corrigido: o sinalizador de "mudou ou não" é
computado DENTRO do próprio `mutator`, na mesma transação atômica de
`update_category` — nenhuma leitura separada.

## 4. Migrations

Nenhuma migration nova. `LATEST_SCHEMA_VERSION` permanece **9**
(confirmado por teste dedicado e por `tests/test_migrations_frozen.py`,
que continua passando).

## 5. Testes automatizados executados

```
$ /root/.local/bin/pytest -q tests/test_visual_editor.py
..........................................................................  [ 87%]
..........                                                                  [100%]
82 passed in 1.98s

$ /root/.local/bin/pytest -q
(suíte completa)
1738 passed, 1 skipped, 36 subtests passed in 130.74s (0:02:10)

$ python3 -m compileall -q _sistema tests empacotar_release.py
(sem saída = sucesso)
```

Baseline antes do Prompt 29: 1656 passed, 1 skipped, 36 subtests.
Delta: **+82 testes**, todos em `test_visual_editor.py` (nenhum arquivo
de teste pré-existente foi modificado, já que nenhuma extensão em
`edit_project.py` foi necessária). 1656 + 82 = 1738. Confere.

Cobertura da lista de testes obrigatórios do Prompt (seção 4 do
documento-fonte):

- cada transformação de enquadramento (crop/resize/fit_mode/zoom/
  position/rotation) e cada ajuste de imagem (brightness/contrast/
  saturation/gamma/sharpen/noise) funcionando SOZINHA, sem exigir as
  demais;
- cada transformação com valor inválido (tipo errado — incluindo a
  armadilha `isinstance(bool, int)` já corrigida no Prompt 28, repetida
  aqui em 6 variantes parametrizadas —, fora de faixa, não-finito)
  levantando o erro estruturado correto;
- geometria inválida de crop (largura/altura `<=0`, ultrapassar bordas
  normalizadas) e resize (`<=0`, acima do teto de 7680px) rejeitada, com
  um teste explícito de que a borda EXATA (`x+width==1.0`) é aceita;
- múltiplas transformações no mesmo projeto persistindo e sendo lidas
  juntas (enquadramento e ajustes, separadamente), e prova de que alterar
  um campo não afeta os demais já definidos;
- operação em lote: sucesso total, sucesso parcial (projeto inexistente
  ou `project_id` malformado no meio do lote não aborta os demais — dois
  testes distintos para as duas formas de "inválido"), lote de projetos
  vazio (aceito, resultado vazio), lote de campos vazio (rejeitado —
  distinção documentada e testada), campo desconhecido no payload do lote
  (rejeitado), validação acontecendo ANTES de tocar qualquer projeto
  (prova de que um valor inválido não aplica parcialmente), e um valor
  repetido classificado como `unchanged`;
- isolamento de fingerprint entre categorias nas DUAS direções (alterar
  `visual_adjustments` não muda `TEMPLATE` nem `CROP`, e vice-versa) —
  mesma garantia central do Prompt 27, agora provada também entre as duas
  categorias novas deste módulo;
- reaproveitamento atômico de `update_category` (nenhuma composição de
  `get_category`+`set_category`) — confirmado estruturalmente (seção 6)
  e por teste AST;
- concorrência real (`threading.Barrier`, 2 threads): duas operações em
  campos DIFERENTES da MESMA categoria simultaneamente, e duas operações
  em categorias DIFERENTES simultaneamente — ambas provando que nenhuma
  mudança se perde;
- integração read-only com o badge `EDITED` do Media Catalog (via crop e
  via um ajuste de imagem isoladamente), sem nenhuma alteração em
  `media_catalog.py`;
- restart: reabrir com instâncias totalmente novas de `LocalDatabase`/
  `VisualEditor` e confirmar que o estado (enquadramento e ajustes)
  persiste;
- idempotência: repetir a mesma operação não acumula efeito;
- testes estruturais via AST: nenhum import proibido (incluindo
  `timeline_editor`/`limpar_metadados_oficial`, específicos deste
  Prompt), nenhuma chamada a `subprocess`/`ffmpeg`/`ffprobe`, nenhuma
  criação de `Job`/`Artifact`/`Publication`/`Schedule`/`Video`, nenhum
  acesso direto a `.transaction()`/métodos de `LocalDatabase`;
- confirmação de `LATEST_SCHEMA_VERSION` inalterado (9).

## 6. GATE ADVERSARIAL OBRIGATÓRIO — segunda passagem, tentativa de quebrar a solução

Passagem sistemática pelos 15 pontos do CLAUDE.md, focada no novo módulo:

1. **Storage/split-brain**: `VisualEditor.__init__` só aceita
   `LocalDatabase` (`TypeError` caso contrário) e constrói
   `EditProjectManager` internamente — um único caminho de persistência.
2. **Transações/TOCTOU**: identificado e corrigido ANTES da entrega
   (seção 3.6) — a primeira versão de `_bulk_apply` fazia uma leitura
   separada só para classificar `updated`/`unchanged`; reescrita para
   computar isso dentro do próprio `mutator` atômico.
3. **Crash windows**: não aplicável de forma nova — todas as escritas
   passam por uma única `BEGIN IMMEDIATE` de `update_category`, já
   coberta pelas garantias dos Prompts 27/28.
4. **Restart**: `test_estado_visual_sobrevive_reabertura_com_instancias_totalmente_novas`
   — grava com uma instância, abre OUTRA totalmente nova apontando para
   o mesmo arquivo, confirma que enquadramento E ajustes persistem.
5. **Idempotência**: `test_repetir_o_mesmo_set_brightness_sequencialmente_e_deterministico`,
   `test_repetir_o_mesmo_set_crop_sequencialmente_nao_acumula`, e
   `test_bulk_set_adjustments_valor_repetido_e_unchanged` (lote).
6. **Concorrência**: duas provas com `threading.Barrier` real — campos
   diferentes da mesma categoria, e categorias diferentes — ambas
   confirmando zero perda de escrita.
7. **Autoridade única**: não aplicável — este módulo não decide
   `Job.status`/claim/cancel/retry/lifecycle.
8. **Construção dos objetos**: `VisualEditor.__init__` valida o tipo de
   `database` ANTES de construir `EditProjectManager`.
9. **Histórico**: não aplicável — nenhuma operação de retomada existe
   aqui; `EditProjectManager` já mantém revisão/fingerprint/timestamp.
10. **Segredos**: nenhuma mensagem de erro inclui `str(exc)`/traceback/
    token/credencial. No `_bulk_apply`, a falha de um `project_id`
    malformado usa um código próprio (`PROJETO_ID_INVALIDO`), nunca o
    texto cru do `ValueError` de `LocalDatabase.get` (achado e corrigido
    durante a implementação — a primeira versão usava
    `exc.__class__.__name__` genérico via um `except Exception` amplo
    demais, que também arriscava mascarar bugs reais do próprio módulo;
    trocado por `except (ProjectNaoEncontradoError, ValueError)`
    explícito).
11. **Windows**: nunca abre arquivo, cria lock, spawna subprocesso, ou
    faz rename/delete/tempfile — confirmado por teste AST.
12. **Teste honesto**: nomes revisados para corresponder ao que provam;
    removida uma função de teste vazia (só docstring, sem asserção
    nenhuma) encontrada durante a escrita da suíte, antes da entrega.
13. **Falha de handler**: não aplicável — sem handlers de fila/job.
14. **Dois chamadores**: coberto pelo item 6.
15. **Prova final**: testes específicos executados, suíte completa
    executada (1738 passed, 1 skipped, 36 subtests), `compileall`
    limpo, diff revisado, migrations confirmadas congeladas.

**Ataques adicionais executados manualmente (fora da suíte formal, via
scripts ad-hoc) e resultado:**

- `data["crop"]` persistido como uma STRING (simulando corrupção/escrita
  por outra via) → **aceito silenciosamente antes da correção**:
  `dict("nao-e-um-objeto")` tentava iterar a string como pares
  chave/valor e levantava um `ValueError` CRU, escapando de `get_frame`.
  Corrigido com `_as_dict_or_none` (valida `Mapping` antes de `dict(...)`)
  e coberto por teste de regressão
  (`test_leitura_rejeita_sub_objeto_corrompido_persistido_diretamente`).
- `set_position(project_id, 0.1, None)` → rejeitado corretamente
  (`CampoInvalidoError`, `None` não é numérico).
- `set_fit_mode(project_id, True)` → rejeitado corretamente (`bool` não é
  um `fit_mode` válido).
- `bulk_set_frame([...], crop=(0.1, 0.1, 0.5, 0.5))` (tupla em vez de
  dict) → rejeitado corretamente (`CampoInvalidoError`, mesma validação
  de `set_crop` reaproveitada no caminho de lote).
- `project_id` duplicado na lista de um lote → deduplicado
  (`_dedupe_preserve_order`, mesmo padrão de `bulk_edit` do Prompt 27.5),
  aplicado uma única vez por projeto.

Nenhum outro problema foi encontrado que exigisse correção adicional.

## 7. Como testar manualmente

```python
from _sistema.storage.database import LocalDatabase
from _sistema.visual_editor import VisualEditor

db = LocalDatabase("caminho/para/painel.db")
db.initialize()
editor = VisualEditor(db)

editor.set_crop(project_id, x=0.1, y=0.1, width=0.8, height=0.8)
editor.set_zoom(project_id, 1.5)
editor.set_brightness(project_id, 0.2)
editor.set_contrast(project_id, 1.1)

print(editor.get_frame(project_id))
print(editor.get_adjustments(project_id))

# lote
result = editor.bulk_set_adjustments([id1, id2, id3], saturation=1.3)
print(result.updated, result.unchanged, result.failed)
```

O vídeo correspondente já aparece com o badge `EDITED` em
`MediaCatalogService.get_item(video_id)` assim que qualquer `set_*` grava
a primeira decisão — sem nenhuma chamada adicional.

## 8. Riscos conhecidos

- Este módulo só valida ESTRUTURALMENTE (nunca contra a resolução real do
  arquivo — decisão deliberada da seção 0.2/0.4 do Prompt). Um `resize`
  para uma resolução estruturalmente válida (ex.: 7680×4320) ainda pode
  fazer pouco sentido para um vídeo de origem específico — uma etapa de
  renderização futura precisará confrontar isso com `MediaProbe` por
  conta própria, fora do escopo aqui.
- As faixas de `brightness`/`contrast`/`saturation`/`gamma`/`sharpen`/
  `noise` são decisão de produto documentada (seção 3.2), não derivadas
  de nenhum arquivo real nem calibradas contra o preset "BALA" do
  protótipo — podem precisar de ajuste fino quando a renderização real
  (Prompt 42) existir e puder ser comparada visualmente.

## 9. Dívida técnica criada

Nenhuma dívida técnica nova deliberada.

## 10. Pendências

Nenhuma pendência dentro do escopo deste Prompt. Fora de escopo,
explicitamente adiado por decisão do próprio Prompt (seção 6, "NÃO
FAZER"): renderização real (Prompt 42), frontend/UI, e Auto Reframe/
detecção automática (Prompt 33).

## 11. Entrega / verificação (ZIP)

**Estado da ponte com a máquina Windows nesta rodada**: `get_device_info`
confirmou a pasta conectada (`C:\Users\Enzo\Desktop\teste`) DISPONÍVEL, e
os 4 arquivos desta entrega (`_sistema/visual_editor.py`,
`tests/test_visual_editor.py`, `empacotar_release.py`,
`PROMPT_29_VISUAL_EDITOR_RELATORIO.md`) foram copiados para lá via
`device_commit_files` e verificados byte-a-byte (stage de volta + `cmp -s`,
os 4 idênticos) ANTES desta seção ser escrita. Porém, ao tentar rodar
`pytest`/`empacotar_release.py` diretamente na máquina Windows via
`device_bash`, a ferramenta retornou "Workspace unavailable. The isolated
Linux environment on this device failed to start." — ou seja, o ambiente
isolado que executaria comandos NA máquina do usuário não estava
disponível neste momento, mesmo com a pasta conectada e os arquivos já
lá. Por isso, **esta seção reflete um ZIP gerado no SANDBOX (não via
`device_bash` na máquina Windows)** — declarado honesta e explicitamente,
conforme exigido pela seção 5 do Prompt 29. Importante: este NÃO é um
reaproveitamento de uma entrega anterior — o ZIP abaixo foi gerado
NESTA RODADA, a partir dos arquivos desta mesma entrega (o próprio
`unzip -l` abaixo lista `_sistema/visual_editor.py`,
`tests/test_visual_editor.py` e este relatório, todos do tamanho em bytes
já conferido byte-a-byte no Windows momentos antes).

Comando executado (sandbox):

```
$ cd /home/claude/project && python3 empacotar_release.py --out /tmp/zipcheck/entrega_prompt29.zip
...
[2/3] ZIP gerado atomicamente, com identidade revalidada por arquivo, e ja inspecionado.

[3/3] Calculando SHA-256 do ZIP final...
      SHA-256: ac2e9cf36731ea5c93d35903f7be57c6dfeff676b92504c605f611379d8b8023
      Tamanho: 0.97 MB

======================================================================
EMPACOTAMENTO CONCLUIDO COM SUCESSO
Arquivo: entrega_prompt29.zip
SHA-256: ac2e9cf36731ea5c93d35903f7be57c6dfeff676b92504c605f611379d8b8023
======================================================================
```

Nota: o texto do PRÓPRIO relatório dentro do ZIP contém, necessariamente,
uma versão anterior desta seção (sem os dados finais) — é fisicamente
impossível um arquivo conter o hash de si mesmo já incluindo esse hash. A
versão AUTORITATIVA, com os dados reais preenchidos, é esta que está
sendo entregue nesta mensagem/anexo e recopiada para o Windows por cima
da versão que entrou no ZIP.

### (a) Saída literal de `unzip -l`

```
Archive:  /tmp/zipcheck/entrega_prompt29.zip
  Length      Date    Time    Name
---------  ---------- -----   ----
    40632  2026-09-23 10:56   ARQUITETURA_ATUAL.md
    13132  2026-09-23 10:56   CLAUDE.md
     2687  2026-09-23 10:56   EMPACOTAR_RELEASE.bat
   169662  2026-09-23 10:56   GATE_19_5_ESTAGIO2_RELATORIO.md
    11446  2026-09-23 10:56   INSTALAR_DEPENDENCIAS_TESTE.bat
     2969  2026-09-23 10:56   LEIA_ME_PRIMEIRO.txt
      866  2026-09-23 10:56   LIMPAR_ANTES_DE_ZIPAR.bat
    24932  2026-09-23 10:56   MAPA_DE_DADOS.md
      686  2026-09-23 10:56   PAINEL_OFICIAL.bat
    29374  2026-09-23 10:56   PRODUCT_INVARIANTS.md
    23512  2026-09-23 10:56   PROMPT_20_CIRCUIT_BREAKER_RELATORIO.md
    14164  2026-09-23 10:56   PROMPT_21_RETRY_INTELIGENTE_RELATORIO.md
    13986  2026-09-23 10:56   PROMPT_22_IDEMPOTENCIA_RELATORIO.md
    17346  2026-09-23 10:56   PROMPT_23_CORRECAO_RELATORIO.md
    13751  2026-09-23 10:56   PROMPT_23_SECRETS_MANAGER_RELATORIO.md
    31899  2026-09-23 10:56   PROMPT_24B_IMPORT_OPTIONS_RELATORIO.md
    25358  2026-09-23 10:56   PROMPT_24_SOURCE_IMPORT_RELATORIO.md
    28360  2026-09-23 10:56   PROMPT_25_SOURCE_CONTEXT_RESOLVER_RELATORIO.md
    29422  2026-09-23 10:56   PROMPT_26_MEDIA_PROBE_RELATORIO.md
    23735  2026-09-23 10:56   PROMPT_27B_VIDEO_PROMOTION_RELATORIO.md
    19021  2026-09-23 10:56   PROMPT_27_5_MEDIA_CATALOG_RELATORIO.md
    28168  2026-09-23 10:56   PROMPT_27_EDIT_PROJECT_RELATORIO.md
    32664  2026-09-23 10:56   PROMPT_28_TIMELINE_EDITOR_RELATORIO.md
    17892  2026-09-23 10:56   PROMPT_29_VISUAL_EDITOR_RELATORIO.md
    24543  2026-09-23 10:56   REGRESSION_CHECKLIST.md
    26204  2026-09-23 10:56   RISCOS_ATUAIS.md
    55747  2026-09-23 10:56   ROADMAP_COMPLETO.md
     6104  2026-09-23 10:56   RODAR_TESTES.bat
    15403  2026-09-23 10:56   TESTE_MANUAL_WINDOWS_ESTAGIO2.md
    97511  2026-09-23 10:56   _sistema/agendar_tiktok.py
    91980  2026-09-23 10:56   _sistema/agendar_youtube.py
    18611  2026-09-23 10:56   _sistema/app_paths.py
    68310  2026-09-23 10:56   _sistema/batch_engine.py
    27989  2026-09-23 10:56   _sistema/circuit_breaker.py
    55733  2026-09-23 10:56   _sistema/control_manager.py
     2545  2026-09-23 10:56   _sistema/domain/__init__.py
     3307  2026-09-23 10:56   _sistema/domain/checkpoints.py
     5442  2026-09-23 10:56   _sistema/domain/job_state_machine.py
    15978  2026-09-23 10:56   _sistema/domain/models.py
    23434  2026-09-23 10:56   _sistema/edit_project.py
    17683  2026-09-23 10:56   _sistema/gerar_textos.py
    85229  2026-09-23 10:56   _sistema/job_engine.py
    27891  2026-09-23 10:56   _sistema/limpar_metadados_oficial.py
     2724  2026-09-23 10:56   _sistema/login_conta.py
    37607  2026-09-23 10:56   _sistema/media_catalog.py
    23350  2026-09-23 10:56   _sistema/media_probe.py
    44162  2026-09-23 10:56   _sistema/painel_oficial.py
    21683  2026-09-23 10:56   _sistema/publication_idempotency.py
    32885  2026-09-23 10:56   _sistema/recovery_manager.py
    48725  2026-09-23 10:56   _sistema/resource_manager.py
    27252  2026-09-23 10:56   _sistema/retry_policy.py
    17712  2026-09-23 10:56   _sistema/secrets_manager.py
    94188  2026-09-23 10:56   _sistema/shutdown_coordinator.py
    20616  2026-09-23 10:56   _sistema/source_context.py
    49320  2026-09-23 10:56   _sistema/source_import.py
     1036  2026-09-23 10:56   _sistema/state_json.py
     3365  2026-09-23 10:56   _sistema/storage/__init__.py
    19254  2026-09-23 10:56   _sistema/storage/audit.py
    51041  2026-09-23 10:56   _sistema/storage/backup.py
    27352  2026-09-23 10:56   _sistema/storage/database.py
    38955  2026-09-23 10:56   _sistema/storage/legacy_migration.py
     1359  2026-09-23 10:56   _sistema/storage/migrations/__init__.py
     7411  2026-09-23 10:56   _sistema/storage/migrations/m001_initial.py
      764  2026-09-23 10:56   _sistema/storage/migrations/m002_audit_append_only.py
     2395  2026-09-23 10:56   _sistema/storage/migrations/m003_batch_engine.py
     2538  2026-09-23 10:56   _sistema/storage/migrations/m004_circuit_breaker.py
     2135  2026-09-23 10:56   _sistema/storage/migrations/m005_retry_policy.py
     2605  2026-09-23 10:56   _sistema/storage/migrations/m006_publication_idempotency.py
     4713  2026-09-23 10:56   _sistema/storage/migrations/m007_source_asset_declarations.py
     4856  2026-09-23 10:56   _sistema/storage/migrations/m008_source_asset_context.py
     5469  2026-09-23 10:56   _sistema/storage/migrations/m009_video_declarations.py
   119736  2026-09-23 10:56   _sistema/storage_manager.py
    14323  2026-09-23 10:56   _sistema/time_utils.py
    37920  2026-09-23 10:56   _sistema/timeline_editor.py
    14013  2026-09-23 10:56   _sistema/video_promotion.py
    29484  2026-09-23 10:56   _sistema/visual_editor.py
    36069  2026-09-23 10:56   empacotar_release.py
       33  2026-09-23 10:56   requirements.txt
     6014  2026-09-23 10:56   tests/README.md
       65  2026-09-23 10:56   tests/__init__.py
    11123  2026-09-23 10:56   tests/fakes_playwright.py
       21  2026-09-23 10:56   tests/requirements-test.txt
    23822  2026-09-23 10:56   tests/test_agendar_tiktok_check_item_detection.py
    19670  2026-09-23 10:56   tests/test_agendar_tiktok_checks_card_scope.py
    36470  2026-09-23 10:56   tests/test_agendar_tiktok_copyright_policy.py
    14773  2026-09-23 10:56   tests/test_agendar_tiktok_date_before_time_order.py
    13400  2026-09-23 10:56   tests/test_agendar_tiktok_interactive_copyright_policy.py
    28453  2026-09-23 10:56   tests/test_agendar_tiktok_preflight_checks.py
     7817  2026-09-23 10:56   tests/test_agendar_tiktok_skip_checks_on_allow.py
    33286  2026-09-23 10:56   tests/test_agendar_tiktok_time_layers.py
    31582  2026-09-23 10:56   tests/test_agendar_youtube_copyright_policy.py
    23904  2026-09-23 10:56   tests/test_agendar_youtube_interactive_copyright_policy.py
    10080  2026-09-23 10:56   tests/test_agendar_youtube_time_layers.py
     7327  2026-09-23 10:56   tests/test_ai_cache_parsing.py
    20247  2026-09-23 10:56   tests/test_app_paths.py
    31931  2026-09-23 10:56   tests/test_backup_restore.py
    70112  2026-09-23 10:56   tests/test_batch_engine.py
    19226  2026-09-23 10:56   tests/test_checkpoints.py
    26822  2026-09-23 10:56   tests/test_circuit_breaker.py
     7389  2026-09-23 10:56   tests/test_config_names_paths.py
    86602  2026-09-23 10:56   tests/test_control_manager.py
    13408  2026-09-23 10:56   tests/test_domain_models.py
    27954  2026-09-23 10:56   tests/test_edit_project.py
     6240  2026-09-23 10:56   tests/test_ffmpeg_processing.py
     4450  2026-09-23 10:56   tests/test_fingerprint_state.py
     5547  2026-09-23 10:56   tests/test_gerar_textos_exception_persistence.py
     8899  2026-09-23 10:56   tests/test_gerar_textos_ollama_adversarial.py
     4796  2026-09-23 10:56   tests/test_gerar_textos_whisper_adversarial.py
    25385  2026-09-23 10:56   tests/test_job_engine.py
     6839  2026-09-23 10:56   tests/test_job_state_machine.py
    17472  2026-09-23 10:56   tests/test_legacy_json_migration.py
     3209  2026-09-23 10:56   tests/test_limpeza_permission_error.py
    34192  2026-09-23 10:56   tests/test_media_catalog.py
    23595  2026-09-23 10:56   tests/test_media_probe.py
     6950  2026-09-23 10:56   tests/test_migrations_frozen.py
    11476  2026-09-23 10:56   tests/test_operational_audit.py
    16612  2026-09-23 10:56   tests/test_painel_oficial_horarios_por_dia.py
     4723  2026-09-23 10:56   tests/test_painel_oficial_youtube_copyright_timeout_menu.py
    20700  2026-09-23 10:56   tests/test_publication_idempotency.py
    36620  2026-09-23 10:56   tests/test_recovery_manager.py
    37274  2026-09-23 10:56   tests/test_release_packaging.py
   105416  2026-09-23 10:56   tests/test_resource_manager.py
     4676  2026-09-23 10:56   tests/test_resume_detection.py
    31831  2026-09-23 10:56   tests/test_retry_policy.py
     7501  2026-09-23 10:56   tests/test_schedule_slots.py
    19241  2026-09-23 10:56   tests/test_secrets_manager.py
    89276  2026-09-23 10:56   tests/test_shutdown_coordinator.py
    21566  2026-09-23 10:56   tests/test_source_context.py
    18065  2026-09-23 10:56   tests/test_source_import.py
    29758  2026-09-23 10:56   tests/test_source_import_options.py
    25017  2026-09-23 10:56   tests/test_sqlite_storage.py
     7928  2026-09-23 10:56   tests/test_state_json_persistence.py
   116323  2026-09-23 10:56   tests/test_storage_manager.py
     5928  2026-09-23 10:56   tests/test_time_utils.py
    29296  2026-09-23 10:56   tests/test_timeline_editor.py
    16852  2026-09-23 10:56   tests/test_video_promotion.py
    28035  2026-09-23 10:56   tests/test_visual_editor.py
---------                     -------
  3535544                     137 files
```

### (b) SHA-256 do ZIP gerado

```
$ sha256sum /tmp/zipcheck/entrega_prompt29.zip
ac2e9cf36731ea5c93d35903f7be57c6dfeff676b92504c605f611379d8b8023  /tmp/zipcheck/entrega_prompt29.zip
```
