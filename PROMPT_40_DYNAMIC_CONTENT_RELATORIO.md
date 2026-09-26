# PROMPT 40 — Dynamic Content (texto dinâmico em zona CAPTION/AI_TEXT, SEM geração de IA real)

## 1. Arquivos criados

- `tests/test_dynamic_content.py` (64 testes, todos passando) — arquivo
  DEDICADO à funcionalidade nova (mesmo padrão de organização de testes
  por feature já usado em `test_layout_mapper.py` no Prompt 39),
  separado de `test_template_engine.py`/`test_layout_mapper.py`
  (intocados).
- `PROMPT_40_DYNAMIC_CONTENT_RELATORIO.md` (este arquivo).

## 2. Arquivos modificados

- `_sistema/template_engine.py`:
  - Docstring do módulo estendida com a seção 9 (decisões deste
    Prompt, ver seção 4 abaixo).
  - `import math` novo (usado pelo auto-fit para `ceil`).
  - Vocabulário/faixas novos: `CONTENT_TYPE_*` (7 constantes) +
    `CONTENT_TYPES`, `CONTENT_ALIGNMENTS`, `MAX_LINES_RANGE`,
    `MAX_CHARS_RANGE`, `CONTENT_FONT_SIZE_RANGE`, e as constantes
    privadas do auto-fit (`_AUTOFIT_*`).
  - Validadores novos (privados): `_as_content_int`,
    `_validate_content_int_range`, `_validate_content_float_range`,
    `_validate_content_text_field`, `_validate_content_type`,
    `_validate_content_alignment`, `_validate_dynamic_content`.
  - `_validate_zone` estendida para aceitar/validar um campo opcional
    `content` (rejeitando-o estruturalmente fora de `CAPTION`/
    `AI_TEXT`).
  - Auto-fit puro: `_autofit_capacity_chars_per_line`,
    `_autofit_required_lines`, `_autofit_truncate`, dataclass
    `AutoFitResult`, função pública `apply_auto_fit`.
  - `TemplateEngine.set_zone_content` (novo método público),
    reaproveitando `_read_modify_write` (Prompt 39).
  - `__all__` estendido com os nomes públicos novos.
- `empacotar_release.py`: `"PROMPT_40_DYNAMIC_CONTENT_RELATORIO.md"`
  adicionado a `ALLOWED_ROOT_FILES` (ordem alfabética, logo após o do
  Prompt 39). Nenhuma outra mudança de allowlist necessária --
  `tests/test_dynamic_content.py` já cabe na regra genérica `.py`
  dentro de `tests/`.

**Nenhum outro arquivo foi tocado** — confirmado via `find -newer`:
`media_catalog.py` (nem o `BADGE_AI_TITLE` que já existe lá, de
Geração 1 — conceito diferente, não relacionado a este Prompt, não
confundido), `captions_style.py`, `template_importer.py`, todos os
módulos irmãos e todas as migrations permanecem intocados. Nenhuma
migration nova — `layout_json` já é JSON livre, então `content` vive
dentro do dict de cada zona sem exigir `LATEST_SCHEMA_VERSION` novo
(permanece 9).

## 3. Comportamento novo

- Uma zona `CAPTION`/`AI_TEXT` pode agora carregar um campo opcional
  `content` (DynamicContent) com: `content_type` (um dos 7 do
  roadmap), `max_lines`, `max_chars`, `font_size`, `font_size_min`,
  `alignment`, e — dependendo do `content_type` — `text` (só
  `FIXED_TEXT`) ou `instruction` (só `CUSTOM_AI`). `content` é
  rejeitado estruturalmente em `BACKGROUND`/`VIDEO`/`LOGO`.
- `TemplateEngine.set_zone_content(template_id, zone_id, content) ->
  Template` — atribui/atualiza (ou remove, com `content=None`) o
  `content` de UMA zona `CAPTION`/`AI_TEXT` especificamente, atômico
  (mesma `BEGIN IMMEDIATE` via `_read_modify_write`).
- `add_zone` já aceita `content` opcional dentro do dict `zone` desde a
  criação — sem nenhuma mudança de assinatura (é só mais um campo,
  igual `style`).
- `apply_auto_fit(text, *, max_lines, max_chars, font_size,
  font_size_min) -> AutoFitResult` — função pura e determinística,
  exportada, que reduz `font_size` em passos controlados e/ou trunca
  deterministicamente o texto de forma que o resultado NUNCA excede
  `max_lines`/`max_chars`. Não é chamada automaticamente por
  `set_zone_content` (decisão -- seção 4.4).

## 4. Decisões arquiteturais (com justificativa)

### 4.1 — Campos de texto por `content_type`: `text` vs `instruction`

Dois campos distintos, cada um exigido/proibido conforme o
`content_type` (decisão exigida explicitamente pelo Prompt):
`FIXED_TEXT` exige `text` (o único tipo cujo valor final já é conhecido
agora) e proíbe `instruction`; `CUSTOM_AI` exige `instruction` (uma
instrução livre para o futuro `ContentEngine`) e proíbe `text`; os
demais 5 (`AI_HOOK`/`AI_TITLE`/`AI_SUMMARY`/`AI_QUESTION`/`AI_CTA`) não
aceitam NENHUM dos dois campos -- inventar um texto de exemplo para
eles seria dado fictício persistido como se fosse real (risco que a
seção "REVISÃO CRÍTICA" do CLAUDE.md pede para evitar). A presença de
um campo onde é proibido é rejeitada ESTRUTURALMENTE
(`CampoInvalidoError`), nunca descartada em silêncio -- mesma
disciplina de "reject unknown field" já usada em `_validate_style`.

### 4.2 — Faixas de `max_lines`/`max_chars`/`font_size`: reimplementadas, não importadas, e mais generosas que `captions_style.py`

`captions_style.py` já tem o precedente (`LINES_RANGE=(1,5)`,
`MAX_WORDS_RANGE=(1,20)`, `SIZE_RANGE=(8.0,200.0)`), mas esse módulo
valida legenda QUEIMADA e animada palavra-por-palavra -- um domínio
deliberadamente curto. Uma zona de Template pode carregar um
`AI_SUMMARY` inteiro (um parágrafo curto), não só uma legenda de 1-5
linhas. Por isso escolhi `MAX_LINES_RANGE=(1,10)` e
`MAX_CHARS_RANGE=(1,500)` -- faixas PRÓPRIAS deste módulo, mais
generosas, reimplementadas do zero (nunca importadas de
`captions_style.py`, que é módulo irmão de domínio diferente).
`CONTENT_FONT_SIZE_RANGE=(8.0,200.0)` reaproveita a MESMA ordem de
grandeza de `SIZE_RANGE`, também reimplementada de forma independente.

`max_lines`/`max_chars`/`font_size`/`font_size_min` são OBRIGATÓRIOS
quando `content` está presente -- nenhum default silencioso, porque
afetam diretamente a garantia central deste Prompt ("nunca deixar
texto sair do canvas"), mesmo espírito de x/y/width/height de zona
(também sem default). `alignment` é o ÚNICO campo com default
(`"CENTER"`) -- puramente cosmético, então um default razoável não é
uma decisão de alto risco.

### 4.3 — Auto-fit: heurística de 3 passos, nunca medição real

Confirmado por investigação prévia: `requirements.txt` não tem Pillow
nem nenhuma lib de métrica de fonte real, e não existe Render Engine
no produto. `apply_auto_fit` é, portanto, uma função PURA e
determinística baseada em contagem de caracteres, documentada
explicitamente como HEURÍSTICA (nunca renderização):

1. Corte duro por `max_chars` -- sempre primeiro, independente de
   `font_size`, com marcador de truncamento (`"…"`) substituindo o
   último caractere quando corta. Garante `len(resultado) <= max_chars`
   incondicionalmente.
2. Se o texto (já cortado) ainda precisar de mais linhas que
   `max_lines` no `font_size` atual (capacidade de caracteres por linha
   modelada como INVERSAMENTE proporcional ao `font_size`, calibrada
   por uma referência arbitrária documentada --
   `_AUTOFIT_REFERENCE_FONT_SIZE=32.0` -> `_AUTOFIT_REFERENCE_
   CHARS_PER_LINE=18`, mesma ordem de grandeza de
   `captions_style.py::MAX_WORDS_RANGE`), reduz `font_size` em passos
   controlados (`_AUTOFIT_FONT_STEP=1.0`) até `font_size_min`.
3. Se AINDA não couber em `max_lines` mesmo em `font_size_min`, corta
   mais uma vez para o número exato de caracteres que cabe em
   `max_lines` linhas naquele tamanho -- garantia matemática
   (`ceil(a/b) <= m` sse `a <= b*m`) de que o resultado final NUNCA
   excede `max_lines`.

O modelo é parametrizado só por `font_size` (nunca pela geometria da
zona) porque `width`/`height` de uma zona são frações normalizadas
0..1 do frame, não pixels de uma resolução final concreta -- não há
informação real de largura disponível nesta etapa. Documentado
explicitamente como uma aproximação a ser substituída por medição real
quando o Render Engine existir.

**Garantia provada por teste**: para qualquer texto de entrada
(incluindo um texto sintético de até 50000 "palavras" no teste
adversarial), `len(resultado.text) <= max_chars` E `resultado.lines <=
max_lines` sempre.

### 4.4 — `apply_auto_fit` NÃO é chamado automaticamente por `set_zone_content`

Decisão: `apply_auto_fit` fica como função PURA standalone, exportada,
consumida sob demanda -- NÃO é invocada automaticamente ao
validar/persistir `content`. Justificativa: armazenar um resultado de
auto-fit dentro de `layout_json` seria um valor DERIVADO cacheado sem
nenhum consumidor real ainda (mesmo raciocínio já documentado no
Prompt 36, seção 5 -- decisão de preview metadata-only: "não existe
hoje nenhuma tela que consuma isso, gerar/persistir antecipadamente
seria overengineering sem benefício concreto"). Quando o Render Engine
existir, ele chama `apply_auto_fit(texto_resolvido, **content)` no
momento de renderizar -- nunca um valor persistido que poderia ficar
desatualizado se `font_size`/`max_lines` forem editados depois sem que
o auto-fit seja recalculado.

### 4.5 — Integração com o Prompt 39: `set_zone_content` reaproveita `_read_modify_write`, rejeição reaproveita `ZonaProtegidaError`

`set_zone_content` segue EXATAMENTE o mesmo padrão de `move_zone`/
`resize_zone` (Prompt 39): reconstrói a lista de zonas com o `content`
da zona endereçada por `zone_id` substituído, e revalida o layout
INTEIRO via `validate_layout` (reaproveitamento total, nunca validação
paralela). Reutilizei `ZonaProtegidaError` (em vez de criar um erro
novo quase-duplicado) para "esta zona não aceita DynamicContent" --
mesma semântica de "operação não permitida para este tipo de zona" já
usada para proteger a zona `BACKGROUND`. `content=None` remove o
`content` existente (decisão: limpar precisa ser tão fácil quanto
atribuir).

## 5. Migrations

Nenhuma. `content` vive dentro do dict de cada zona em `layout_json`
(já JSON livre desde `m001_initial.py`). `LATEST_SCHEMA_VERSION`
permanece 9.

## 6. Testes automatizados executados (saída real, não presumida)

```
$ pytest tests/test_dynamic_content.py -v
...
64 passed in 0.98s
```

Suíte completa:

```
$ pytest tests/ -q
...
2554 passed, 1 skipped, 36 subtests passed in 150.05s (0:02:30)
```

Delta exato de **+64** em relação à baseline anterior (2490, pós-Prompt
39) -- o número exato de testes novos deste Prompt. Zero regressões em
`test_template_engine.py`/`test_layout_mapper.py` (nenhuma alteração
nesses arquivos) ou em qualquer outro arquivo de teste.
`python3 -m compileall -q _sistema tests` -- OK. `find -newer` (contra
`PROMPT_39_LAYOUT_MAPPER_RELATORIO.md`) confirmou que só
`_sistema/template_engine.py` e `tests/test_dynamic_content.py` foram
tocados (fora de `__pycache__`/o ZIP de release anterior, que já
existia).

Cobertura da matriz obrigatória do Prompt: cada `content_type`
validado isoladamente (incluindo os 5 tipos `AI_*` sem texto,
parametrizados) com todas as rejeições (campo ausente onde exigido,
presente onde proibido, vocabulário/alignment/faixas inválidos);
`font_size_min > font_size` rejeitado (e `==` aceito); `content`
rejeitado em `BACKGROUND`/`VIDEO`/`LOGO` (via `validate_layout` direto,
via `set_zone_content`, e via `add_zone`); auto-fit com texto que já
cabe (sem mudança), texto gigante (parametrizado até 50000
repetições), truncamento com marcador, determinismo, idempotência
(ponto fixo), e `font_size` nunca abaixo de `font_size_min`;
`set_zone_content` isolado (zona/template inexistente, zona de tipo
errado, limpar com `None`, substituir, endereçamento correto entre 2
zonas `AI_TEXT`); `add_zone` com/sem `content`; retrocompatibilidade
(zona `CAPTION` pré-existente sem `content` continua válida após
`get_template`); `zone_id` (Prompt 39) continua estável após
`set_zone_content`; geometria de zona com `content` continua validada
normalmente.

## 7. Como testar manualmente

```python
from _sistema.app_paths import build_app_paths, ensure_app_directories
from _sistema.storage.database import LocalDatabase
from _sistema.storage_manager import StorageManager
from _sistema.template_engine import TemplateEngine, ZONE_TYPE_CAPTION, apply_auto_fit

paths = build_app_paths(data_root="/tmp/painel_teste")
ensure_app_directories(paths)
db = LocalDatabase(paths.database / "painel.db")
db.initialize()
storage = StorageManager(paths, database_path=paths.database / "painel.db")
engine = TemplateEngine(db, storage_manager=storage, app_paths=paths)

tpl = engine.create_template(
    name="Teste Dynamic Content",
    layout={"zones": [
        {"zone_type": "BACKGROUND"},
        {"zone_type": "VIDEO", "x": 0.1, "y": 0.1, "width": 0.8, "height": 0.8},
        {"zone_type": ZONE_TYPE_CAPTION, "x": 0.0, "y": 0.8, "width": 1.0, "height": 0.2},
    ]},
)
caption_id = [z for z in tpl.layout["zones"] if z["zone_type"] == "CAPTION"][0]["zone_id"]

tpl = engine.set_zone_content(tpl.id, caption_id, {
    "content_type": "FIXED_TEXT",
    "text": "Um texto de exemplo bem mais longo do que cabe confortavelmente",
    "max_lines": 2,
    "max_chars": 60,
    "font_size": 40.0,
    "font_size_min": 16.0,
})
print(tpl.layout)

# auto-fit standalone (o futuro Render Engine chamaria isto no momento de renderizar)
r = apply_auto_fit(
    "Um texto de exemplo bem mais longo do que cabe confortavelmente",
    max_lines=2, max_chars=60, font_size=40.0, font_size_min=16.0,
)
print(r)
```

## 8. Riscos conhecidos

- A heurística de auto-fit (chars-por-linha inversamente proporcional
  ao `font_size`, calibrada por uma referência arbitrária) é uma
  aproximação intencionalmente simples -- quando o Render Engine e uma
  biblioteca de métrica de fonte real existirem, o resultado visual
  real pode divergir da estimativa. Documentado explicitamente como
  heurística no código e nesta seção -- não é um bug, é uma limitação
  conhecida e aceita para esta etapa (sem Pillow/render engine
  disponível).
- `MAX_LINES_RANGE=(1,10)`/`MAX_CHARS_RANGE=(1,500)` são estimativas
  razoáveis sem dado real de uso -- se um `AI_SUMMARY` real precisar de
  mais que 500 caracteres, o limite precisará ser revisitado com dado
  real (mesma ressalva já registrada para `MAX_IMPORT_FILE_SIZE_BYTES`
  no Prompt 38).

## 9. Dívida técnica criada

- Nenhuma migração de schema.
- `apply_auto_fit` não tem nenhum consumidor real ainda (não existe
  Render Engine) -- é infraestrutura pronta para quando ele existir,
  mesmo padrão de "infra-primeiro" já usado em Prompts anteriores
  (ex.: `_read_image_dimensions` do Prompt 36, criado antes de haver
  UI que o consumisse).

## 10. Pendências

- `ContentEngine` (geração real via IA local/Ollama para os 6
  `content_type` `AI_*`) -- Fase 7 do roadmap, não antecipada.
- Render Engine (consumiria `apply_auto_fit` no momento de renderizar)
  -- ainda não existe.
- Prompt 64 -- Layout Mapper VISUAL (UI de edição de texto).
