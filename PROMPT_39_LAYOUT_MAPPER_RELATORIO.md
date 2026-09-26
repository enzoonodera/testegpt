# PROMPT 39 — Layout Mapper (motor de posicionamento de zonas, SEM frontend)

## 1. Arquivos criados

- `tests/test_layout_mapper.py` (43 testes, todos passando) — arquivo
  DEDICADO à funcionalidade nova (decisão justificada na seção 4.2),
  separado de `tests/test_template_engine.py` (62 testes pré-existentes,
  intocados).
- `PROMPT_39_LAYOUT_MAPPER_RELATORIO.md` (este arquivo).

## 2. Arquivos modificados

- `_sistema/template_engine.py`:
  - Docstring do módulo estendida com a seção 8 (decisões deste Prompt,
    ver seção 4 abaixo).
  - `import` novo: `from uuid import UUID`; `new_uuid` importado de
    `.domain` (já exportado desde `Prompt 36`/`domain/__init__.py` --
    nenhuma duplicação de geração de UUID).
  - 2 erros estruturados novos: `ZonaNaoEncontradaError`,
    `ZonaProtegidaError` (ambos estendem `TemplateEngineError`, mesmo
    padrão de todo erro deste módulo).
  - `_validate_zone_id` (nova função privada) + `_validate_zone`
    estendida para aceitar/gerar/preservar `zone_id`; `_validate_zones`
    estendida para rejeitar `zone_id` duplicado.
  - `update_template` refatorado para ser atômico (correção de um bug
    de concorrência pré-existente -- ver seção 4.3).
  - `TemplateEngine._read_modify_write` (novo método privado,
    reaproveitado por `update_template` e pelas 5 operações novas).
  - 5 métodos públicos novos: `add_zone`, `move_zone`, `resize_zone`,
    `remove_zone`, `reorder_zone`.
  - `__all__` estendido com os 2 erros novos.
- `empacotar_release.py`: `"PROMPT_39_LAYOUT_MAPPER_RELATORIO.md"`
  adicionado a `ALLOWED_ROOT_FILES` (ordem alfabética, logo após o do
  Prompt 38). Nenhuma outra mudança de allowlist necessária --
  `tests/test_layout_mapper.py` já cabe em `ALLOWED_TREE_EXTENSIONS`
  (`.py` dentro de `tests/`, mesma regra genérica que já cobria
  `test_template_importer.py`).

**Nenhum outro arquivo foi tocado** — confirmado via `find -newer`:
`media_catalog.py`, `template_importer.py`, todos os módulos irmãos e
todas as migrations permanecem intocados. Nenhuma migration nova --
`layout_json`/`extra_json` já são JSON livre, então adicionar
`zone_id` a cada zona não exige `LATEST_SCHEMA_VERSION` novo (segue
sendo 9).

## 3. Comportamento novo

- `TemplateEngine.add_zone(template_id, zone, *, index=None) -> Template`
  — adiciona uma zona nova (nunca `BACKGROUND`); `index=None` insere no
  fim, um `index` explícito é sempre ajustado para nunca ficar antes da
  posição 1. Um `zone_id` eventualmente passado pelo chamador é sempre
  IGNORADO -- um novo é sempre gerado para a zona adicionada.
- `TemplateEngine.move_zone(template_id, zone_id, *, x, y) -> Template`
  — atualiza `x`/`y` de UMA zona específica (endereçada por
  `zone_id`), preservando os demais campos. Rejeita `BACKGROUND`.
- `TemplateEngine.resize_zone(template_id, zone_id, *, width, height) -> Template`
  — simétrico para `width`/`height`. Rejeita `BACKGROUND`.
- `TemplateEngine.remove_zone(template_id, zone_id) -> Template` —
  remove a zona identificada. Rejeita `BACKGROUND`/`VIDEO` (cardinalidade
  mínima obrigatória).
- `TemplateEngine.reorder_zone(template_id, zone_id, *, new_index) -> Template`
  — move a zona para uma nova posição na lista. Rejeita `BACKGROUND` e
  rejeita mover qualquer outra zona para o índice 0 (reservado à
  `BACKGROUND`).
- Toda zona (novas e antigas, incluindo as já ingeridas pelos Prompts
  36/38) agora carrega um `zone_id` (UUID) estável -- gerado
  automaticamente quando ausente, preservado em revalidações/escritas
  sucessivas do mesmo layout.
- `update_template` agora é atômico (correção de bug -- seção 4.3),
  mesmo comportamento observável, mesma assinatura pública.
- Erros estruturados novos: `ZonaNaoEncontradaError` (zona inexistente
  dentro de um template existente), `ZonaProtegidaError` (operação
  rejeitada por alvejar `BACKGROUND` ou violar cardinalidade mínima).

## 4. Decisões arquiteturais (com justificativa)

### 4.1 — Endereçamento de zona: `zone_id` (UUID), não índice posicional

Três opções avaliadas, exigidas explicitamente pelo Prompt: (a)
`zone_id` UUID estável por zona; (b) índice posicional mitigado por
reler a lista dentro da mesma transação; (c) uma alternativa.

**Decisão: (a).** A opção (b) resolve apenas a corrida de ESCRITA
concorrente dentro de uma única chamada (o que a transação atômica já
resolveria de qualquer forma -- ver 4.3) — ela NÃO resolve o problema
de identidade ao longo de VÁRIAS chamadas de API separadas. Um índice
é inerentemente instável entre chamadas: se um chamador (ex.: a futura
UI de arrastar do Prompt 64) guarda "a zona que quero mover é o índice
3" e, antes da próxima chamada, outra operação legítima insere/remove
uma zona antes do índice 3, esse índice passa a apontar para uma zona
DIFERENTE -- um "mover a zona errada" silencioso. `zone_id` é o padrão
correto para exatamente esta classe de problema (identidade estável
através de múltiplas interações). Custo de implementação baixo:
`new_uuid()` já existe e já é exportado de `_sistema/domain`
(reaproveitado via import direto); a disciplina de validação de
`_validate_uuid` (privado do módulo `domain`, nunca importado através
de módulos) foi reimplementada localmente como `_validate_zone_id`,
mesmo padrão de isolamento já usado em todo o resto do arquivo.

Retrocompatibilidade total: `validate_layout`/`_validate_zone`
continuam aceitando um `zone_id` ausente (gerado automaticamente) --
todo caller pré-existente (`create_template`,
`ingest_builtin_templates`, `TemplateImporter.import_template` do
Prompt 38) continua funcionando sem nenhuma mudança, ganhando um
`zone_id` de forma transparente. `zone_id` já presente é sempre
PRESERVADO (nunca regenerado), o que torna o campo estável através de
sucessivas leituras/escritas do mesmo layout (testado explicitamente:
`test_zone_id_estavel_apos_update_template_com_layout_relido`).
`zone_id` duplicado dentro do mesmo layout é rejeitado
(`LayoutInvalidoError`).

### 4.2 — Onde vivem as operações novas: `TemplateEngine`, não um módulo novo

Avaliado por analogia direta ao Prompt 38 (`TemplateImporter` virou um
módulo NOVO que DEPENDE de `TemplateEngine`, em vez de estendê-lo).
**Decisão: estender `TemplateEngine` diretamente** (métodos novos na
mesma classe/arquivo), não criar `_sistema/layout_mapper.py`.

Justificativa: `TemplateImporter` nunca precisou de leitura-decisão-
escrita transacional contra a MESMA linha `templates` -- só chamava
`create_template` (um INSERT único, sem leitura prévia). As operações
de zona são o oposto: cada uma precisa ler-decidir-escrever a MESMA
linha sob UMA `BEGIN IMMEDIATE` -- ou seja, precisam do MESMO acesso a
`self._database`/`self._database.transaction()` que `update_template`/
`ingest_builtin_templates` já usam. Um módulo separado seria obrigado a
manter seu PRÓPRIO acesso a `self._database` (uma segunda facade
escrevendo na mesma tabela -- exatamente o que o item 1 do GATE
ADVERSARIAL do CLAUDE.md veda) ou reimplementar leitura+escrita como
duas chamadas separadas (reintroduzindo a janela de corrida que este
Prompt existe para fechar). Decisão: `TemplateEngine` continua sendo o
ÚNICO dono de leitura/escrita da entidade `Template` (mesmo princípio
já documentado desde o Prompt 36), e as 5 operações são métodos novos
dessa mesma classe, reaproveitando o mesmo helper interno
(`_read_modify_write`) que a correção da seção 4.3 introduz.

Os testes deste Prompt, no entanto, ficam num arquivo DEDICADO
(`tests/test_layout_mapper.py`), separado dos 62 testes pré-existentes
de `test_template_engine.py` -- decisão de organização de testes por
FEATURE (não por módulo Python), para manter cada arquivo de teste
focado e não inflar ainda mais um arquivo já com 62 casos.

### 4.3 — Correção: `update_template` não era atômico (TOCTOU real)

Investigação prévia (item 2 do GATE ADVERSARIAL, obrigatória antes de
qualquer novo componente) encontrou que `update_template` fazia
`self.get_template(template_id)` (leitura SEM transação) seguido de
`self._database.save(entity)` (escrita SEPARADA, também sem
transação) -- duas operações SQLite distintas, não atômicas entre si.
Duas chamadas concorrentes contra o MESMO `template_id` podiam se
intercalar (leitura de A, leitura de B, escrita de A, escrita de B --
a escrita de B, baseada numa leitura anterior à escrita de A,
silenciosamente descarta a mudança de A: last-write-wins sem aviso).
Isso já era um bug real e pré-existente em `update_template` de forma
geral (não hipotético), mas se torna muito mais fácil de disparar na
prática assim que existe uma API de mover UMA zona por vez (chamadas
pequenas e frequentes contra o mesmo template, como uma UI de
arrastar-e-soltar futura disparando uma chamada por frame de arraste).

**Correção** (mínima, sem mudar assinatura pública nem comportamento
observável -- só a atomicidade interna): `update_template` e as 5
operações de zona agora passam por um único helper privado novo,
`TemplateEngine._read_modify_write`, que abre
`self._database.transaction()` e faz a leitura
(`self._database.get(..., connection=conn)`) E a escrita
(`self._database.save(..., connection=conn)`) dentro da MESMA
`BEGIN IMMEDIATE` -- mesmo padrão já comprovado em
`ingest_builtin_templates` desde o Prompt 36. Nenhuma duplicação de
lógica de transação entre `update_template` e as operações de zona.

Provado com DOIS testes de concorrência real (`threading.Barrier`,
nunca `time.sleep`), mesmo estilo de
`test_ingest_builtin_templates_concorrente_duas_instancias_nao_duplica`:
`test_update_template_concorrente_duas_instancias_nao_perde_mudanca`
(duas instâncias, cada uma alterando um campo DIFERENTE do mesmo
template ao mesmo tempo -- ambas as mudanças sobrevivem) e
`test_add_zone_concorrente_duas_instancias_nao_perde_nenhuma_zona`
(duas instâncias adicionando zonas DIFERENTES ao mesmo template ao
mesmo tempo -- ambas as zonas sobrevivem, nenhuma perdida).

### 4.4 — Mover/redimensionar/remover a zona `BACKGROUND`: rejeição explícita, nunca no-op silencioso

**Decisão: rejeitar explicitamente** (`ZonaProtegidaError`), nunca um
no-op silencioso.

Justificativa: um no-op silencioso violaria a disciplina de "nunca
falhar silenciosamente" usada em todo o resto do projeto (ex.:
`TemplateImportError` com `.code` em vez de aceitar e ignorar um
arquivo inválido). Um chamador futuro (a UI de arrastar do Prompt 64)
que tentasse mover a zona `BACKGROUND` precisa de um jeito
PROGRAMÁTICO de saber que a operação não é permitida (para, por
exemplo, desabilitar a alça de arraste daquela zona especificamente),
não descobrir por tentativa-e-erro que "nada aconteceu". A mesma
rejeição explícita se aplica a `remove_zone` sobre a única
`BACKGROUND`/`VIDEO` (cardinalidade mínima) e a `reorder_zone` sobre
`BACKGROUND` (deve permanecer sempre a primeira) ou sobre mover
qualquer outra zona para o índice 0 (reservado à `BACKGROUND`). A
geometria da `BACKGROUND` continua travada ao frame inteiro por
`_validate_zone` independentemente disso -- a rejeição aqui é uma
camada adicional, mais cedo e com mensagem mais específica sobre a
INTENÇÃO da operação, não só sobre o resultado.

### 4.5 — Reaproveitamento total de `validate_layout`

Cada uma das 5 operações constrói uma lista de zonas CANDIDATA (com a
mutação aplicada) e chama `validate_layout({"zones": candidata})` --
toda a validação de geometria/vocabulário/cardinalidade já existente
(desde o Prompt 36) é 100% reaproveitada, NUNCA reimplementada em
paralelo para o caso "zona única". Qualquer regra futura adicionada a
`validate_layout` automaticamente se aplica a estas 5 operações sem
nenhuma mudança adicional.

## 5. Migrations

Nenhuma. `zone_id` vive dentro do dict de cada zona em `layout_json`
(já JSON livre desde `m001_initial.py`) -- nenhum campo novo em
`Template`/na tabela `templates` foi necessário. `LATEST_SCHEMA_VERSION`
permanece 9.

## 6. Testes automatizados executados (saída real, não presumida)

```
$ pytest tests/test_layout_mapper.py -q
...
43 passed in 0.70s
```

Suíte completa:

```
$ pytest tests/ -q
...
2490 passed, 1 skipped, 36 subtests passed in 149.55s (0:02:29)
```

Delta exato de **+43** em relação à baseline anterior (2447, pós-Prompt
38) -- o número exato de testes novos deste Prompt. Zero regressões em
`test_template_engine.py` (62/62 continuam passando, sem nenhuma
alteração no arquivo) ou em qualquer outro arquivo de teste.
`python3 -m compileall -q _sistema tests` -- OK. `find -newer` (contra
`PROMPT_38_TEMPLATE_IMPORTER_RELATORIO.md`) confirmou que só
`_sistema/template_engine.py`, `tests/test_layout_mapper.py` e
`empacotar_release.py` foram tocados (fora de `__pycache__`/o ZIP de
release antigo, que já existia).

Cobertura da matriz obrigatória do Prompt: as 5 operações validadas
isoladamente incluindo TODOS os caminhos de rejeição (geometria
inválida, cardinalidade, `zone_id`/`template_id` inexistente, zona
`BACKGROUND` protegida); endereçamento correto entre múltiplas zonas
do MESMO `zone_type` (2 zonas `LOGO`, mover/remover uma nunca afeta a
outra); concorrência real com `threading.Barrier` (nunca `sleep`)
contra `update_template` E contra `add_zone` especificamente; restart
com instâncias novas de `LocalDatabase`/`StorageManager`/
`TemplateEngine`; confirmação de que as regras pré-existentes (geometria
0..1, cardinalidade `BACKGROUND`/`VIDEO` exatamente 1,
`CAPTION`/`AI_TEXT`/`LOGO` 0..N) continuam valendo sem regressão.

## 7. Como testar manualmente

```python
from _sistema.app_paths import build_app_paths, ensure_app_directories
from _sistema.storage.database import LocalDatabase
from _sistema.storage_manager import StorageManager
from _sistema.template_engine import TemplateEngine, ZONE_TYPE_LOGO

paths = build_app_paths(data_root="/tmp/painel_teste")
ensure_app_directories(paths)
db = LocalDatabase(paths.database / "painel.db")
db.initialize()
storage = StorageManager(paths, database_path=paths.database / "painel.db")
engine = TemplateEngine(db, storage_manager=storage, app_paths=paths)

tpl = engine.create_template(
    name="Teste Layout Mapper",
    layout={"zones": [
        {"zone_type": "BACKGROUND"},
        {"zone_type": "VIDEO", "x": 0.1, "y": 0.1, "width": 0.8, "height": 0.8},
    ]},
)

# adiciona uma zona LOGO
tpl = engine.add_zone(tpl.id, {"zone_type": ZONE_TYPE_LOGO, "x": 0.0, "y": 0.0, "width": 0.15, "height": 0.15})
logo_id = [z for z in tpl.layout["zones"] if z["zone_type"] == "LOGO"][0]["zone_id"]
print("logo_id:", logo_id)

# move só essa zona
tpl = engine.move_zone(tpl.id, logo_id, x=0.7, y=0.7)
print(tpl.layout)

# tentar mover a zona BACKGROUND deve falhar
bg_id = tpl.layout["zones"][0]["zone_id"]
try:
    engine.move_zone(tpl.id, bg_id, x=0.1, y=0.1)
except Exception as e:
    print("rejeitado como esperado:", type(e).__name__, e)
```

## 8. Riscos conhecidos

- `add_zone` sempre ignora um `zone_id` eventualmente enviado pelo
  chamador (sempre gera um novo) -- decisão deliberada para garantir
  que ids nunca colidam entre inserções concorrentes/repetidas, mas
  significa que um chamador não pode "escolher" o id de uma zona nova
  antecipadamente (teria que ler o `Template` retornado para descobrir
  o id gerado). Documentado, não é uma limitação inesperada.
- Nenhuma zona `LOGO`/`CAPTION`/`AI_TEXT` tem limite superior de
  cardinalidade (continua 0..N, decisão herdada do Prompt 36) -- um
  chamador mal-intencionado ou com bug poderia adicionar centenas de
  zonas à mesma lista sem rejeição estrutural. Não é o escopo deste
  Prompt (nenhum limite foi pedido) -- documentado como possível
  trabalho futuro se uma necessidade real surgir (ex.: uma UI que
  precise de um teto para não degradar performance de renderização).

## 9. Dívida técnica criada

- Nenhuma migração de schema.
- `zone_id` não tem nenhum uso funcional ainda além de endereçamento
  (não aparece em nenhuma tela -- não existe tela ainda). É consumido
  apenas pelas 5 operações deste Prompt e pela validação/geração
  automática.

## 10. Pendências

- Prompt 64 — Layout Mapper VISUAL (arrastar/redimensionar numa tela,
  consumindo `zone_id` para saber qual zona está sendo manipulada).
- Prompt 40 — DynamicContent (fora do escopo deste Prompt).
- Render Engine (ainda não existe).
