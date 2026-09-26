# PROMPT 32 — Estilos de Legenda — Relatório de Entrega

Data: 2026-09-23

Este relatório segue a estrutura obrigatória de 10 pontos do CLAUDE.md,
acrescida das seções específicas exigidas pela especificação do Prompt
32 (decisões de categoria/formato de cada campo, presets, confirmação de
que `media_catalog.py` e os módulos irmãos da Fase 5 não foram tocados,
e a Seção 11 de verificação de entrega, agora não-bloqueante conforme
esclarecido pelo usuário nesta rodada).

---

## 1. Arquivos criados

- `_sistema/captions_style.py` (novo módulo, ~700 linhas) — decisões de
  estilo visual de legenda: `font`, `size`, `position`, `alignment`,
  `stroke`, `shadow`, `background`, `max_words`, `lines`, `animation`,
  `current_word_highlight`, mais 3 presets profissionais e operações em
  lote. 100% decisão — mesmo padrão dos Prompts 27-30, nunca o padrão
  de processamento real do Prompt 31.
- `tests/test_captions_style.py` (139 testes).
- `PROMPT_32_CAPTIONS_STYLE_RELATORIO.md` (este arquivo).

## 2. Arquivos modificados

- `empacotar_release.py` — adicionado `PROMPT_32_CAPTIONS_STYLE_RELATORIO.md`
  a `ALLOWED_ROOT_FILES` (mesmo padrão de toda rodada anterior). Nenhuma
  outra linha alterada.

Nenhum outro arquivo foi tocado. Em particular, confirmado por hash
SHA-256 nesta sessão que `_sistema/media_catalog.py`,
`_sistema/captions_engine.py`, `_sistema/visual_editor.py`,
`_sistema/audio_engine.py` e `_sistema/timeline_editor.py` permanecem
byte-a-byte idênticos ao estado anterior (o hash de `captions_engine.py`
bate exatamente com o já verificado na entrega do Prompt 31,
`7d2c10d4...`). `_sistema/edit_project.py` também não foi modificado —
toda a infraestrutura necessária (`update_category`, categorias como
string livre) já existia.

## 3. Comportamento novo

`CaptionsStyleEngine`, operando sobre uma categoria NOVA de
`EditProjectManager`, `CAPTIONS_STYLE = "captions_style"` (ver Seção 4
para a justificativa de não reaproveitar `CAPTIONS`/`TEXT_LAYERS`):

- `get_style(project_id)` — leitura somente-leitura, todos os campos
  `Optional` (`None` = não definido).
- `set_style(project_id, **campos)` — define um ou mais campos de uma
  vez (mescla com o que já existia, nunca substitui a categoria
  inteira). Único ponto de escrita campo-a-campo (ver Seção 4.4 para a
  justificativa de não ter setters individuais por campo).
- `apply_preset(project_id, preset_name)` — substitui a categoria
  INTEIRA pelos valores completos e validados do preset escolhido
  (`CLASSIC`, `BOLD_SOCIAL`, `KARAOKE`).
- `bulk_set_style(project_ids, **campos)` / `bulk_apply_preset(project_ids, preset_name)`
  — mesma operação, aplicada a vários projetos, com pré-validação única
  antes de qualquer escrita.

Nenhum renderer é criado ou tocado — não existe hoje nenhum
`RenderEngine`/`def render` no produto (confirmado por leitura direta de
`_sistema/` nesta sessão). "Nada hardcoded no renderer" foi tratado como
restrição sobre o MODELO DE DADOS: todo valor visual vem de configuração
persistida por este módulo, nunca de uma constante embutida num
renderer futuro.

## 4. Decisões arquiteturais

### 4.1 Por que uma categoria nova (`captions_style`), não `CAPTIONS` nem `TEXT_LAYERS`

- `CAPTIONS` (`"captions"`) já tem um contrato em uso real desde o
  Prompt 31 (`{"mode": "OFF"|"SEPARATE"|"BURNED"}`, escrito por
  `CaptionsEngine.set_mode`/`get_mode`). Misturar `style` ali arriscaria
  colisão de escrita entre dois módulos competindo pela mesma categoria
  sem coordenação, e quebraria a garantia de que uma categoria
  representa UM contrato coerente.
- `TEXT_LAYERS` (`"text_layers"`), pelo nome e pela lista de exemplos
  PARALELOS do roadmap do Prompt 27, parece destinada a camadas de texto
  GENÉRICAS (títulos, callouts), não à legenda transcrita especificamente
  — usar essa categoria aqui forçaria dois conceitos diferentes na mesma
  categoria.
- Decisão: `CAPTIONS_STYLE = "captions_style"`, categoria nova. Precedente
  já aceito no produto: `VisualEditor` (Prompt 29) criou
  `VISUAL_ADJUSTMENTS` fora da lista original de nove convenções quando
  nenhuma categoria existente servia, sem exigir migration (categoria é
  só uma string dentro do JSON já existente, `_validate_category_name`
  aceita qualquer string não vazia).

### 4.2 Distinção: caixa de legenda (este Prompt) x zona de template (Prompt 39, futuro)

Releitura da seção do Prompt 39 (Layout Mapper, "VIDEO, CAPTION,
AI_TEXT, IMAGE/LOGO") confirmada nesta sessão: aquele Prompt futuro
define ZONAS de um TEMPLATE (layout de uma imagem de template
importada). O campo `position` deste módulo é outra coisa: o ponto de
ancoragem da legenda transcrita dentro do FRAME DO VÍDEO em si (mesma
superfície onde `crop`/`zoom`/`rotation` do Prompt 29 operam). Os dois
conceitos são relacionados mas não são o mesmo dado nem a mesma
categoria — este Prompt não antecipa nem depende do formato que o
Layout Mapper venha a usar.

### 4.3 Cada campo, formato e faixa

- `font`: string livre (unicode/emoji aceitos), não vazia, até 200
  caracteres (limite de sanidade, nunca validação de existência real da
  fonte — decisão explícita, adiada para uma etapa futura de
  gerenciamento de assets/fontes).
- `size`: float, `[8.0, 200.0]` (unidades abstratas, não pixels — mesma
  razão de `visual_editor.py` usar coordenadas normalizadas).
- `position`: `{"x": 0.0-1.0, "y": 0.0-1.0}` — ponto de ancoragem
  absoluto, reaproveitando a MESMA convenção de `crop.x`/`crop.y` do
  Prompt 29. Deliberadamente DIFERENTE do campo `position` de
  `visual_editor.py` (que é um deslocamento, `-1.0`-`1.0`) — documentado
  explicitamente para não confundir os dois. Este módulo não modela
  largura/altura da caixa de legenda (depende de métricas de texto
  renderizado que só um renderer futuro pode calcular).
- `alignment`: vocabulário fechado `LEFT`/`CENTER`/`RIGHT`.
- `stroke`: `{"enabled": bool, "color": <hex>, "width": 0.0-20.0}`.
- `shadow`: `{"enabled": bool, "color": <hex>, "blur": 0.0-20.0, "offset_x": -20.0-20.0, "offset_y": -20.0-20.0}`
  — parâmetros mínimos padrão de qualquer sombra de texto realista (CSS
  `text-shadow`, After Effects), não overengineering.
- `background`: `{"enabled": bool, "color": <hex>, "opacity": 0.0-1.0}`
  — `opacity` mantido como campo separado do alfa da cor porque o
  próprio texto do Prompt pede os dois lado a lado explicitamente;
  documentado para não parecer redundância acidental (composição
  `alfa_efetivo = alfa_da_cor * opacity` é responsabilidade de um
  renderer futuro, este módulo só grava os dois valores).
- Cores: formato único em todo o módulo, `#RRGGBB` ou `#RRGGBBAA`,
  validado por regex explícita, normalizado para maiúsculas antes de
  persistir (forma canônica única evita que a mesma cor produza
  fingerprints diferentes só por causa alta/baixa).
- `max_words`: int, `[1, 20]`.
- `lines`: int, `[1, 5]`.
- `animation`: vocabulário fechado `NONE`/`FADE`/`POP`/`SLIDE` (`SLIDE`
  acrescentado além dos 3 exemplos literais do roadmap por ser uma
  animação de legenda genuinamente comum em produto real — decisão
  documentada, vocabulário permanece fechado e real, não uma lista
  aberta disfarçada).
- `current_word_highlight`: `{"enabled": bool, "color": <hex>}` — forma
  mínima suficiente; não modela timing por palavra (depende dos
  timestamps do Prompt 31 e de um renderer futuro).

### 4.4 API única `set_style(project_id, **campos)`, sem setters individuais por campo

O Prompt sugere explicitamente esse formato. Decisão: usá-lo como ÚNICA
via de escrita campo-a-campo, em vez de replicar o padrão de setters
individuais de `audio_engine.py`/`visual_editor.py`. Motivo: a maioria
dos campos aqui é um OBJETO aninhado (`position`, `stroke`, `shadow`,
`background`, `current_word_highlight`), e a validação já precisa estar
centralizada (`_validate_style_fields`) para ser reaproveitada por
`set_style`, `bulk_set_style` E pela construção dos próprios presets —
criar N métodos `set_font`/`set_size`/... só para chamar essa mesma
função central um campo de cada vez adicionaria superfície de API
duplicada sem ganho real de validação. `set_style(project_id,
font="Arial")` já cobre "campo a campo" (um kwarg); vários kwargs cobrem
"em lote de campos para o mesmo projeto".

### 4.5 Padrão preset nomeado (sem uma constante `CUSTOM` explícita)

Reaproveitado em espírito de `source_import.py` (Prompt 24, presets
nomeados com valores concretos em código + possibilidade de estado
customizado), mas SEM importar nada daquele módulo. Diferente de
`source_import.py`, não existe uma constante `CUSTOM` aqui — um estilo
"customizado" é simplesmente o resultado de `set_style`/`bulk_set_style`,
sem precisar de uma flag adicional dizendo qual caminho foi usado por
último (informação sem consumidor real hoje).

### 4.6 Presets construídos e validados na importação do módulo

Cada preset é construído chamando a MESMA função de validação central
usada por `set_style` (`_validate_style_fields(..., require_all=True)`)
— garante que um preset nunca representa um estado que a validação
normal rejeitaria. Os 3 presets são validados UMA VEZ, ao importar o
módulo (falha rápida se um preset estiver malformado, nunca
silenciosamente em produção).

### 4.7 `apply_preset`/`bulk_apply_preset` usam `update_category`, nunca `set_category` direto

Mesma disciplina atômica dos Prompts 28-31 Parte A: mesmo quando o novo
valor não depende do estado anterior (substituição completa), a escrita
passa por `update_category` (mutator que ignora `current_data` e
devolve o preset) — nunca por uma leitura e escrita separadas. Verificado
estruturalmente por teste (`self._manager.set_category(` não aparece no
código-fonte do módulo).

### 4.8 Erros estruturados — 4 subtipos

Mais granular que `audio_engine.py` porque este módulo tem vocabulário
fechado E cor estruturada, nenhum dos dois presentes naquele módulo:
`CampoInvalidoError` (tipo/range/campo desconhecido/`project_id`
malformado), `VocabularioInvalidoError` (`alignment`/`animation` fora do
vocabulário), `CorInvalidaError` (cor estruturalmente inválida),
`PresetInvalidoError` (nome de preset desconhecido).

## 5. Migrations

Nenhuma migration nova. `LATEST_SCHEMA_VERSION` continua `9` (confirmado
por teste). A categoria `captions_style` é só uma string dentro do JSON
já existente de `edit_state_json`.

## 6. Confirmação explícita — módulos irmãos não tocados

- `_sistema/media_catalog.py` **não foi tocado** — hash SHA-256
  confirmado idêntico ao estado anterior nesta sessão. Nenhum badge
  novo foi adicionado ou modificado.
- `_sistema/captions_engine.py` **não foi tocado** — hash SHA-256
  confirmado idêntico ao já verificado na entrega do Prompt 31
  (`7d2c10d4...`).
- `_sistema/visual_editor.py`, `_sistema/audio_engine.py`,
  `_sistema/timeline_editor.py` **não foram tocados** — hashes
  confirmados idênticos ao estado anterior.
- Este módulo nunca importa nenhum dos módulos acima (confirmado por
  teste AST).

## 7. Testes automatizados executados

Suíte dedicada: `tests/test_captions_style.py` — **139 testes**:

- Construção (1 teste: `manager` de tipo errado).
- Estado inicial (`None` em todos os campos).
- Presets: valores exatos campo a campo para os 3 presets, mínimo de 3
  presets confirmado, distinção par a par entre TODOS os presets
  (comparação de dataclasses completas), preset desconhecido rejeitado,
  `apply_preset` é substituição COMPLETA (não mescla com estado
  anterior).
- `set_style`: cada campo isolado (11 campos), múltiplos campos juntos,
  um campo não afeta os demais já definidos, sem nenhum campo é erro,
  campo desconhecido é erro.
- Validação de range: `size`, `position.x/y`, `stroke.width`,
  `shadow.blur`, `shadow.offset_x/y`, `background.opacity`, `max_words`,
  `lines` — cada um com valores fora do intervalo, mais `inf`/`-inf`/
  `nan` em `size`/`position`.
- Validação de tipo: `size` com string/`None`/lista/dict, armadilha
  bool-como-int/float em `size`/`max_words`/`lines`, `max_words` com
  float (mesmo numericamente inteiro) rejeitado sem truncamento
  silencioso, `stroke.enabled` não-bool, `stroke` não-objeto, `font`
  tipo errado/vazio/excede comprimento máximo, `font` com unicode/emoji
  aceito.
- Vocabulário fechado: `alignment`/`animation` fora do vocabulário
  (parametrizado incluindo lowercase/vazio/`None`/int/bool), cada valor
  válido de `ALIGNMENTS`/`ANIMATIONS` aceito individualmente.
- Cor: formatos estruturalmente inválidos parametrizados (`"red"`,
  `#FFF`, caracteres não-hex, sem `#`, comprimento errado, `None`, int),
  6 e 8 dígitos aceitos, normalização para maiúsculas, prova de que
  minúsculas/maiúsculas produzem o MESMO fingerprint (canonicalização
  real, não só validação).
- Isolamento de fingerprint: `captions_style` nunca muda o fingerprint
  de `CAPTIONS`/`AUDIO_SETTINGS`/`CROP` e vice-versa (nas duas
  direções), categorias `captions_style` e `captions` comprovadamente
  distintas (uma não vaza campos da outra).
- `update_category` atômico: teste estrutural que `self._manager.set_category(`
  nunca aparece no código-fonte.
- Concorrência real: dois `threading.Barrier` — campos diferentes do
  mesmo projeto, e categorias diferentes (`captions_style` vs `CROP`) —
  nenhuma mudança perdida.
- Restart: instâncias totalmente novas de `LocalDatabase`/
  `EditProjectManager`/`CaptionsStyleEngine`.
- Idempotência: `set_style`/`apply_preset`/`bulk_set_style`/
  `bulk_apply_preset` repetidos não acumulam revisão nem mudam
  fingerprint.
- Lote: sucesso total (`set_style` e `apply_preset`), sucesso parcial
  (projeto inexistente no meio, `set_style` e `apply_preset`), lote
  vazio é erro, sem campos é erro, ids duplicados deduplicados, campo
  inválido NÃO escreve em nenhum projeto antes de validar, preset
  inválido NÃO escreve em nenhum projeto, campo desconhecido é erro,
  lote não afeta categorias de outros módulos.
- AST/estrutural: nenhum import de `media_catalog`/`captions_engine`/
  `visual_editor`/`audio_engine`/`timeline_editor`/Geração 1, nenhuma
  chamada a `subprocess`/`ffmpeg`/`ffprobe`, nunca cria `Job`/
  `Artifact`/`Video`/`Publication`/`Schedule`, nunca lê
  `SourceAsset.local_path` (via AST, não grep textual — a própria
  docstring cita o termo para explicar a não-utilização), nunca abre
  `transaction()` própria nem chama métodos de `LocalDatabase`
  diretamente.
- Leitura defensiva: `stroke`/`background` persistidos corrompidos
  (não-objeto) são rejeitados estruturalmente, nunca lançam `ValueError`
  cru.
- Regressão `project_id` não-UUID: `get_style`/`set_style`/
  `apply_preset` nunca deixam `ValueError` cru escapar; `bulk_set_style`/
  `bulk_apply_preset` classificam como falha estruturada
  (`PROJETO_ID_INVALIDO`).
- Confirmação de nenhuma migration nova.
- Todos os 4 subtipos de erro são subclasses de `CaptionsStyleError`.

Resultado: **139 passed**.

Suíte completa do projeto (`/root/.local/bin/pytest -q`):

```
2079 passed, 1 skipped, 36 subtests passed in 134.74s
```

Baseline antes deste Prompt: 1940 passed. Delta: **+139** (exatamente o
número de testes novos).

`python3 -m compileall -q _sistema tests empacotar_release.py` —
concluído sem erros.

## 8. GATE ADVERSARIAL — ataques executados

Scripts manuais de sanidade adversarial ANTES de declarar a suíte formal
completa (mesma disciplina das rodadas anteriores):

- Aplicar os 3 presets sequencialmente ao mesmo projeto e confirmar que
  são par a par distintos e que `apply_preset` substitui completamente
  (não mescla com estado anterior de `set_style`).
- `set_style` parcial preservando campos definidos por um preset
  anterior (prova de que `set_style` faz merge, `apply_preset` não).
- Escrever em `CAPTIONS`/`AUDIO_SETTINGS` no mesmo projeto e confirmar
  isolamento total de `captions_style`.
- `max_words=True` (armadilha bool-como-int) rejeitado.
- Cor `"red"` (formato inválido) rejeitada; cor `"#abc123"` normalizada
  para `"#ABC123"`.
- `alignment="MIDDLE"` (fora do vocabulário) rejeitado.
- `apply_preset` com nome desconhecido rejeitado.
- `font` com unicode/emoji (`"日本語フォント 😀"`) aceito e preservado
  byte-a-byte.
- `project_id` não-UUID nunca escapa como `ValueError` cru.
- Lote com um projeto real e um `project_id` UUID válido mas
  inexistente — sucesso parcial correto, o inexistente cai em `failed`.
- Corrida real (`threading.Barrier`, 2 threads, instâncias de banco
  separadas) setando `font` a partir de dois valores diferentes no
  mesmo projeto simultaneamente — resultado final determinístico (um
  dos dois valores, nunca corrompido/perdido), confirmando que
  `update_category` fecha a janela de TOCTOU também para este módulo.

## 9. Como testar manualmente

```python
from _sistema.storage.database import LocalDatabase
from _sistema.edit_project import EditProjectManager
from _sistema.captions_style import CaptionsStyleEngine, PRESET_BOLD_SOCIAL

db = LocalDatabase("caminho/para/painel.db")
db.initialize()
manager = EditProjectManager(db)
engine = CaptionsStyleEngine(manager)

engine.apply_preset(project_id, PRESET_BOLD_SOCIAL)
engine.set_style(project_id, size=44.0, alignment="CENTER")
print(engine.get_style(project_id))

engine.bulk_apply_preset([id1, id2, id3], PRESET_BOLD_SOCIAL)
```

Rodar a suíte dedicada: `pytest -q tests/test_captions_style.py`.

## 10. Riscos conhecidos / dívida técnica / pendências

- Nenhuma validação cruzada entre `position`/geometria de legenda e
  `CROP`/`REFRAME` (Prompt 29) — os dois sistemas de coordenadas
  normalizadas são conceitualmente compatíveis (ambos fração do frame),
  mas este módulo nunca lê a categoria `CROP` para, por exemplo, avisar
  se a legenda ficaria fora da área visível após um crop agressivo.
  Adiado deliberadamente pelo mesmo raciocínio de modularidade já usado
  por `audio_engine.py` (seção 0.6 daquele módulo) — validação cruzada
  fica para uma etapa futura (Render Engine), quando a geometria final
  realmente importa.
- Nenhuma validação de que a fonte referenciada por `font` existe de
  fato no sistema/está instalada — decisão explícita e documentada
  (Seção 4.3), não uma omissão silenciosa.
- `current_word_highlight` não modela timing por palavra — depende dos
  timestamps reais do Prompt 31 e de um renderer futuro para aplicar a
  sincronização.
- `background.opacity` e o canal alfa opcional de `background.color`
  coexistem sem um renderer que efetivamente os combine ainda — a
  fórmula de composição está documentada (Seção 4.3) mas não há código
  que a exercite nesta etapa (não existe renderer).
- Nenhuma pendência de código para este Prompt especificamente; a
  implementação, testes e GATE adversarial estão completos.

---

## 11. VERIFICAÇÃO DE ENTREGA — ZIP e arquivos individuais

### 11.1 Mudança de processo nesta rodada (esclarecimento do usuário)

Ficou esclarecido nesta rodada que o ZIP que efetivamente chega para
auditoria é gerado pelo usuário rodando `EMPACOTAR_RELEASE.bat`
localmente, depois de apagar o ZIP que o agente gerou no sandbox — são
dois arquivos diferentes por natureza (a pasta real do usuário tem pelo
menos 2 arquivos a mais, `GATE_19_5_RELATORIO.md` e
`TESTE_MANUAL_WINDOWS.md`, que não existem no sandbox). Isso deixou de
ser motivo de recusa: a auditoria sempre recalcula hash/listagem em cima
do arquivo que o usuário realmente anexa, independente do que esta
Seção 11 declarar. Esta seção continua sendo preenchida como sempre foi
(é evidência útil do processo do agente), mas uma divergência nela,
sozinha, não bloqueia mais a aprovação.

### 11.2 Disponibilidade de `device_bash` nesta rodada

Verificado nesta sessão antes de qualquer suposição: `device_bash`
retornou `"Workspace unavailable. The isolated Linux environment on
this device failed to start."` -- indisponível nesta rodada
especificamente. Como esclarecido pelo usuário (Seção 11.1), isso não é
mais bloqueante: a comparação byte-a-byte dos arquivos entregues (Seção
11.4) continua funcionando normalmente via `device_stage_files`/
`device_commit_files`, que não dependem desse shell isolado.

### 11.3 Comando único -- SHA-256 + `unzip -l` do ZIP gerado nesta rodada (evidência, não mais bloqueante)

Executado em uma única chamada, depois do ZIP já fechado em disco, sobre
o caminho literal do arquivo gerado nesta rodada no sandbox
(`PAINEL_OFICIAL_YOUTUBE_TIKTOK_20260923_183800Z.zip`) -- este é o ZIP construído pelo agente; o arquivo que o
usuário efetivamente anexa é gerado localmente por ele via
`EMPACOTAR_RELEASE.bat` (Seção 11.1) e pode ter conteúdo adicional
(`GATE_19_5_RELATORIO.md`, `TESTE_MANUAL_WINDOWS.md`) que não existe no
sandbox -- por isso esta seção não é mais usada como critério de
aprovação, só como evidência do processo do agente:

```
$ sha256sum PAINEL_OFICIAL_YOUTUBE_TIKTOK_20260923_183800Z.zip && unzip -l PAINEL_OFICIAL_YOUTUBE_TIKTOK_20260923_183800Z.zip
7583ac603a9355218a0cf1a73550f78fdb8ba328cb041b7718c7861a86c3fcc9  PAINEL_OFICIAL_YOUTUBE_TIKTOK_20260923_183800Z.zip
Archive:  PAINEL_OFICIAL_YOUTUBE_TIKTOK_20260923_183800Z.zip
  Length      Date    Time    Name
---------  ---------- -----   ----
    40632  2026-09-23 15:38   ARQUITETURA_ATUAL.md
    13132  2026-09-23 15:38   CLAUDE.md
     2687  2026-09-23 15:38   EMPACOTAR_RELEASE.bat
   169662  2026-09-23 15:38   GATE_19_5_ESTAGIO2_RELATORIO.md
    11446  2026-09-23 15:38   INSTALAR_DEPENDENCIAS_TESTE.bat
     2969  2026-09-23 15:38   LEIA_ME_PRIMEIRO.txt
      866  2026-09-23 15:38   LIMPAR_ANTES_DE_ZIPAR.bat
    24932  2026-09-23 15:38   MAPA_DE_DADOS.md
      686  2026-09-23 15:38   PAINEL_OFICIAL.bat
    29374  2026-09-23 15:38   PRODUCT_INVARIANTS.md
    23512  2026-09-23 15:38   PROMPT_20_CIRCUIT_BREAKER_RELATORIO.md
    14164  2026-09-23 15:38   PROMPT_21_RETRY_INTELIGENTE_RELATORIO.md
    13986  2026-09-23 15:38   PROMPT_22_IDEMPOTENCIA_RELATORIO.md
    17346  2026-09-23 15:38   PROMPT_23_CORRECAO_RELATORIO.md
    13751  2026-09-23 15:38   PROMPT_23_SECRETS_MANAGER_RELATORIO.md
    31899  2026-09-23 15:38   PROMPT_24B_IMPORT_OPTIONS_RELATORIO.md
    25358  2026-09-23 15:38   PROMPT_24_SOURCE_IMPORT_RELATORIO.md
    28360  2026-09-23 15:38   PROMPT_25_SOURCE_CONTEXT_RESOLVER_RELATORIO.md
    29422  2026-09-23 15:38   PROMPT_26_MEDIA_PROBE_RELATORIO.md
    23735  2026-09-23 15:38   PROMPT_27B_VIDEO_PROMOTION_RELATORIO.md
    19021  2026-09-23 15:38   PROMPT_27_5_MEDIA_CATALOG_RELATORIO.md
    28168  2026-09-23 15:38   PROMPT_27_EDIT_PROJECT_RELATORIO.md
    32664  2026-09-23 15:38   PROMPT_28_TIMELINE_EDITOR_RELATORIO.md
    28847  2026-09-23 15:38   PROMPT_29_VISUAL_EDITOR_RELATORIO.md
    31397  2026-09-23 15:38   PROMPT_30_AUDIO_ENGINE_RELATORIO.md
    35288  2026-09-23 15:38   PROMPT_31_CAPTIONS_ENGINE_RELATORIO.md
    21281  2026-09-23 15:38   PROMPT_32_CAPTIONS_STYLE_RELATORIO.md
    24543  2026-09-23 15:38   REGRESSION_CHECKLIST.md
    26204  2026-09-23 15:38   RISCOS_ATUAIS.md
    55747  2026-09-23 15:38   ROADMAP_COMPLETO.md
     6104  2026-09-23 15:38   RODAR_TESTES.bat
    15403  2026-09-23 15:38   TESTE_MANUAL_WINDOWS_ESTAGIO2.md
    97511  2026-09-23 15:38   _sistema/agendar_tiktok.py
    91980  2026-09-23 15:38   _sistema/agendar_youtube.py
    18611  2026-09-23 15:38   _sistema/app_paths.py
    30509  2026-09-23 15:38   _sistema/audio_engine.py
    68310  2026-09-23 15:38   _sistema/batch_engine.py
    39225  2026-09-23 15:38   _sistema/captions_engine.py
    44048  2026-09-23 15:38   _sistema/captions_style.py
    27989  2026-09-23 15:38   _sistema/circuit_breaker.py
    55733  2026-09-23 15:38   _sistema/control_manager.py
     2545  2026-09-23 15:38   _sistema/domain/__init__.py
     3307  2026-09-23 15:38   _sistema/domain/checkpoints.py
     5442  2026-09-23 15:38   _sistema/domain/job_state_machine.py
    15978  2026-09-23 15:38   _sistema/domain/models.py
    23434  2026-09-23 15:38   _sistema/edit_project.py
    17683  2026-09-23 15:38   _sistema/gerar_textos.py
    85229  2026-09-23 15:38   _sistema/job_engine.py
    27891  2026-09-23 15:38   _sistema/limpar_metadados_oficial.py
     2724  2026-09-23 15:38   _sistema/login_conta.py
    37607  2026-09-23 15:38   _sistema/media_catalog.py
    23350  2026-09-23 15:38   _sistema/media_probe.py
    44162  2026-09-23 15:38   _sistema/painel_oficial.py
    21683  2026-09-23 15:38   _sistema/publication_idempotency.py
    32885  2026-09-23 15:38   _sistema/recovery_manager.py
    48725  2026-09-23 15:38   _sistema/resource_manager.py
    27252  2026-09-23 15:38   _sistema/retry_policy.py
    17712  2026-09-23 15:38   _sistema/secrets_manager.py
    94188  2026-09-23 15:38   _sistema/shutdown_coordinator.py
    20616  2026-09-23 15:38   _sistema/source_context.py
    49320  2026-09-23 15:38   _sistema/source_import.py
     1036  2026-09-23 15:38   _sistema/state_json.py
     3365  2026-09-23 15:38   _sistema/storage/__init__.py
    19254  2026-09-23 15:38   _sistema/storage/audit.py
    51041  2026-09-23 15:38   _sistema/storage/backup.py
    27352  2026-09-23 15:38   _sistema/storage/database.py
    38955  2026-09-23 15:38   _sistema/storage/legacy_migration.py
     1359  2026-09-23 15:38   _sistema/storage/migrations/__init__.py
     7411  2026-09-23 15:38   _sistema/storage/migrations/m001_initial.py
      764  2026-09-23 15:38   _sistema/storage/migrations/m002_audit_append_only.py
     2395  2026-09-23 15:38   _sistema/storage/migrations/m003_batch_engine.py
     2538  2026-09-23 15:38   _sistema/storage/migrations/m004_circuit_breaker.py
     2135  2026-09-23 15:38   _sistema/storage/migrations/m005_retry_policy.py
     2605  2026-09-23 15:38   _sistema/storage/migrations/m006_publication_idempotency.py
     4713  2026-09-23 15:38   _sistema/storage/migrations/m007_source_asset_declarations.py
     4856  2026-09-23 15:38   _sistema/storage/migrations/m008_source_asset_context.py
     5469  2026-09-23 15:38   _sistema/storage/migrations/m009_video_declarations.py
   119736  2026-09-23 15:38   _sistema/storage_manager.py
    14323  2026-09-23 15:38   _sistema/time_utils.py
    37920  2026-09-23 15:38   _sistema/timeline_editor.py
    14013  2026-09-23 15:38   _sistema/video_promotion.py
    29484  2026-09-23 15:38   _sistema/visual_editor.py
    36203  2026-09-23 15:38   empacotar_release.py
       33  2026-09-23 15:38   requirements.txt
     6014  2026-09-23 15:38   tests/README.md
       65  2026-09-23 15:38   tests/__init__.py
    11123  2026-09-23 15:38   tests/fakes_playwright.py
       21  2026-09-23 15:38   tests/requirements-test.txt
    23822  2026-09-23 15:38   tests/test_agendar_tiktok_check_item_detection.py
    19670  2026-09-23 15:38   tests/test_agendar_tiktok_checks_card_scope.py
    36470  2026-09-23 15:38   tests/test_agendar_tiktok_copyright_policy.py
    14773  2026-09-23 15:38   tests/test_agendar_tiktok_date_before_time_order.py
    13400  2026-09-23 15:38   tests/test_agendar_tiktok_interactive_copyright_policy.py
    28453  2026-09-23 15:38   tests/test_agendar_tiktok_preflight_checks.py
     7817  2026-09-23 15:38   tests/test_agendar_tiktok_skip_checks_on_allow.py
    33286  2026-09-23 15:38   tests/test_agendar_tiktok_time_layers.py
    31582  2026-09-23 15:38   tests/test_agendar_youtube_copyright_policy.py
    23904  2026-09-23 15:38   tests/test_agendar_youtube_interactive_copyright_policy.py
    10080  2026-09-23 15:38   tests/test_agendar_youtube_time_layers.py
     7327  2026-09-23 15:38   tests/test_ai_cache_parsing.py
    20247  2026-09-23 15:38   tests/test_app_paths.py
    33321  2026-09-23 15:38   tests/test_audio_engine.py
    31931  2026-09-23 15:38   tests/test_backup_restore.py
    70112  2026-09-23 15:38   tests/test_batch_engine.py
    45382  2026-09-23 15:38   tests/test_captions_engine.py
    37162  2026-09-23 15:38   tests/test_captions_style.py
    19226  2026-09-23 15:38   tests/test_checkpoints.py
    26822  2026-09-23 15:38   tests/test_circuit_breaker.py
     7389  2026-09-23 15:38   tests/test_config_names_paths.py
    86602  2026-09-23 15:38   tests/test_control_manager.py
    13408  2026-09-23 15:38   tests/test_domain_models.py
    27954  2026-09-23 15:38   tests/test_edit_project.py
     6240  2026-09-23 15:38   tests/test_ffmpeg_processing.py
     4450  2026-09-23 15:38   tests/test_fingerprint_state.py
     5547  2026-09-23 15:38   tests/test_gerar_textos_exception_persistence.py
     8899  2026-09-23 15:38   tests/test_gerar_textos_ollama_adversarial.py
     4796  2026-09-23 15:38   tests/test_gerar_textos_whisper_adversarial.py
    25385  2026-09-23 15:38   tests/test_job_engine.py
     6839  2026-09-23 15:38   tests/test_job_state_machine.py
    17472  2026-09-23 15:38   tests/test_legacy_json_migration.py
     3209  2026-09-23 15:38   tests/test_limpeza_permission_error.py
    34192  2026-09-23 15:38   tests/test_media_catalog.py
    23595  2026-09-23 15:38   tests/test_media_probe.py
     6950  2026-09-23 15:38   tests/test_migrations_frozen.py
    11476  2026-09-23 15:38   tests/test_operational_audit.py
    16612  2026-09-23 15:38   tests/test_painel_oficial_horarios_por_dia.py
     4723  2026-09-23 15:38   tests/test_painel_oficial_youtube_copyright_timeout_menu.py
    20700  2026-09-23 15:38   tests/test_publication_idempotency.py
    36620  2026-09-23 15:38   tests/test_recovery_manager.py
    37274  2026-09-23 15:38   tests/test_release_packaging.py
   105416  2026-09-23 15:38   tests/test_resource_manager.py
     4676  2026-09-23 15:38   tests/test_resume_detection.py
    31831  2026-09-23 15:38   tests/test_retry_policy.py
     7501  2026-09-23 15:38   tests/test_schedule_slots.py
    19241  2026-09-23 15:38   tests/test_secrets_manager.py
    89276  2026-09-23 15:38   tests/test_shutdown_coordinator.py
    21566  2026-09-23 15:38   tests/test_source_context.py
    18065  2026-09-23 15:38   tests/test_source_import.py
    29758  2026-09-23 15:38   tests/test_source_import_options.py
    25017  2026-09-23 15:38   tests/test_sqlite_storage.py
     7928  2026-09-23 15:38   tests/test_state_json_persistence.py
   116323  2026-09-23 15:38   tests/test_storage_manager.py
     5928  2026-09-23 15:38   tests/test_time_utils.py
    29296  2026-09-23 15:38   tests/test_timeline_editor.py
    16852  2026-09-23 15:38   tests/test_video_promotion.py
    28035  2026-09-23 15:38   tests/test_visual_editor.py
---------                     -------
  3864246                     146 files
```

### 11.4 Verificação individual dos arquivos entregues

Ver Seção 11.5 abaixo -- preenchida após a entrega de cada arquivo via
`SendUserFile` → `device_commit_files` → `device_stage_files` →
comparação byte a byte (`cmp -s` + SHA-256), já que `device_bash`
esteve indisponível nesta rodada (Seção 11.2).

### 11.5 Resultado da verificação individual

Os 4 arquivos foram gravados no computador Windows real
(`C:\Users\Enzo\Desktop\teste\...`) via `device_commit_files`, depois
lidos de volta de lá via `device_stage_files` (cópia ponto-no-tempo
buscada NO Windows, não reenviada pelo sandbox), e comparados
byte-a-byte (`cmp -s` + SHA-256) contra os originais no sandbox de
desenvolvimento:

| Arquivo | Destino no Windows | Resultado | SHA-256 |
|---|---|---|---|
| `_sistema/captions_style.py` | `C:\Users\Enzo\Desktop\teste\_sistema\captions_style.py` | IDÊNTICO | `25f0f3af9f686986eb63b5fb732bcc060ea3d81c01877b226815676d0ee4f2d6` |
| `tests/test_captions_style.py` | `C:\Users\Enzo\Desktop\teste\tests\test_captions_style.py` | IDÊNTICO | `b079c2e0b89f12dc6a108e69e67b4a2b27c02450a3e5b242b04d324f6bf19d62` |
| `empacotar_release.py` | `C:\Users\Enzo\Desktop\teste\empacotar_release.py` | IDÊNTICO | `65961df7d3a64009c6f77491480d8c7b8f27daed1324fc1ed015bd942c5e0b38` |
| `PROMPT_32_CAPTIONS_STYLE_RELATORIO.md` (versão anterior a esta seção) | `C:\Users\Enzo\Desktop\teste\PROMPT_32_CAPTIONS_STYLE_RELATORIO.md` | IDÊNTICO (nessa versão) | `a169e7e0e0c93607340ca290402a9566b45dbba544d8276ea428ca9862468c5c` |

Nota sobre o próprio relatório: como esta Seção 11.5 é escrita DEPOIS da
comparação acima, o arquivo final no Windows (após esta última
gravação) difere em bytes do hash tabelado acima -- esse hash
corresponde à versão do relatório ANTES desta seção existir, que foi a
versão de fato comparada byte a byte (mesma limitação de
auto-referência já aceita e documentada desde as rodadas anteriores).
Após adicionar esta seção, o arquivo é regravado no Windows uma última
vez para que a cópia final no computador do usuário seja a versão
completa e definitiva deste relatório.
