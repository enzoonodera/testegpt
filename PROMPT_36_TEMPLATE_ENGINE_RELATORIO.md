# PROMPT 36 — Template Engine + 2 Templates Oficiais (Prompt 37 adaptado)

## ADENDO — CORREÇÃO OBRIGATÓRIA (bug de empacotamento binário no Windows)

**Problema real, confirmado independentemente**: um ZIP encontrado na
máquina do usuário continha os 2 PNGs oficiais truncados para **5
bytes** cada (`\x89PNG\n`), ambos com o mesmo SHA-256 apesar de serem
imagens diferentes. Verificação direta (`device_list_dir` na pasta
`assets/templates_oficiais/` da máquina) confirmou que os arquivos
**no disco** sempre estiveram corretos (379250/1380317 bytes, hashes
batendo com os originais) — o problema estava exclusivamente na
**leitura feita pelo empacotador** ao gerar o ZIP.

**Causa raiz identificada e corrigida**: `_read_verified_bytes()` em
`empacotar_release.py` abria cada arquivo com
`os.open(path, os.O_RDONLY | O_NOFOLLOW)`, **sem `os.O_BINARY`**. No
Windows, sem essa flag o CRT (MSVCRT) abre o arquivo em **modo
texto**, onde (a) o byte `0x1A` (Ctrl-Z/SUB) é tratado como fim de
arquivo e (b) `\r\n` é traduzido para `\n`. A assinatura padrão de PNG
é `\x89 P N G \r \n \x1A \n` (8 bytes) — ou seja, **todo PNG contém
0x1A na própria assinatura**, no 7º byte. Em modo texto: `\r\n` vira
`\n` (perde 1 byte), depois a leitura para no `0x1A` — sobra
`\x89PNG\n`, exatamente os 5 bytes observados. `template_engine.py`
em si nunca esteve envolvido: o bug é anterior a este Prompt (o código
de empacotamento é herdado de rounds passados) e só foi exposto agora
porque este é o **primeiro Prompt a empacotar um arquivo binário** —
todo Prompt anterior só empacotava `.py`/`.md`, onde `0x1A` é
extremamente improvável de aparecer.

**Correção**: adicionada `getattr(os, "O_BINARY", 0)` à flag de
`os.open()` em `_read_verified_bytes()` — mesmo padrão já usado para
`O_NOFOLLOW` (`0` em POSIX, sem nenhuma mudança de comportamento em
Linux/macOS; a flag real só existe e importa no Windows).

**Testes de regressão adicionados** (`tests/test_release_packaging.py`):
`test_read_verified_bytes_preserva_byte_0x1a_sem_truncar` (prova
mínima e direta: um arquivo sintético com `0x1A` no meio precisa ser
lido por inteiro) e
`test_build_release_zip_preserva_png_assinatura_completa_com_0x1a`
(prova de ponta a ponta: um PNG sintético com a assinatura real de 8
bytes sobrevive `build_release_zip` → ZIP final, byte a byte).

**Teste fortalecido** (`tests/test_template_engine.py`):
`test_assets_oficiais_existem_e_tem_as_dimensoes_esperadas` antes só
checava `p.is_file()` — nunca teria pego este bug (um stub de 5 bytes
também "é um arquivo"). Agora lê a dimensão real via `ffprobe`
(`_read_image_dimensions`) e compara contra `941x1672` esperado para
cada um dos 2 templates oficiais — um stub truncado agora falha este
teste explicitamente.

**Verificação real, rodada nesta correção** (não presumida):
- `tests/test_template_engine.py` + `tests/test_release_packaging.py`:
  **113 passed** (62 + 51, incluindo os 2 testes de regressão novos).
- Suíte completa (`pytest tests/ -q`): **2420 passed, 1 skipped, 36
  subtests passed** (150.55s) — delta exato de **+2** em relação à
  baseline anterior (2418), correspondente aos 2 testes de regressão
  novos. Zero regressões.
- ZIP reconstruído do zero e o conteúdo **interno** de cada PNG foi
  extraído e comparado por SHA-256 contra o arquivo original em
  `assets/templates_oficiais/` — hashes idênticos (`3ef36420...`/
  `44bf6cc1...`), tamanhos idênticos (1380317/379250 bytes). Esta
  comparação do conteúdo *dentro* do ZIP (não só do arquivo solto) é
  exatamente a verificação que faltou na entrega anterior e permitiu o
  problema passar despercebido.
- Entrega à máquina Windows repetida com a mesma disciplina
  (`SendUserFile` → `device_commit_files` → `device_stage_files` →
  `cmp`/SHA-256), desta vez incluindo a extração e o hash do conteúdo
  interno do ZIP entregue, não só dos arquivos soltos.

---

## 0. Escopo executado

- **PROMPT 36 — Template Engine**: `TemplateEngine` interno, CRUD validado
  sobre a entidade de catálogo `Template` (já migrada, nunca consumida até
  agora). Zonas descritas em coordenadas normalizadas 0..1: background,
  vídeo, legenda, texto IA, logo/imagem, ordem de camadas, styling.
- **PROMPT 37 (adaptado pelo dono do produto: 2 templates, não 3)**:
  ingestão idempotente dos 2 templates oficiais fornecidos (PNGs anexados
  ao Prompt), tratados como assets built-in mas usando o **mesmo**
  Template Engine genérico que o futuro Importer (Prompt 38) usará.

Explicitamente **fora** de escopo, confirmado como rounds futuros do
roadmap: Layout Mapper visual (Prompt 39 — arrastar/redimensionar zonas
numa tela), Importador genérico de template (Prompt 38 — upload de
PNG/JPG arbitrário + configuração visual), Render Engine (aplicar de
fato um Template a um vídeo — ainda não existe no produto).

## 1. Arquivos criados

- `_sistema/template_engine.py` (~650 linhas) — módulo completo:
  `TemplateEngine`, validadores de geometria/estilo/vocabulário,
  ingestão dos built-ins, `PreviewInfo`.
- `tests/test_template_engine.py` (62 testes, todos passando).
- `assets/templates_oficiais/template_moldura_tech_azul.png` (cópia
  byte-a-byte do arquivo anexado ao Prompt — fundo escuro com moldura
  neon azul nos 4 cantos).
- `assets/templates_oficiais/template_moldura_branca_classica.png`
  (cópia byte-a-byte do arquivo anexado ao Prompt — fundo branco com
  borda fina preta inset).
- `PROMPT_36_TEMPLATE_ENGINE_RELATORIO.md` (este arquivo).

## 2. Arquivos modificados

- `empacotar_release.py`:
  - `"PROMPT_36_TEMPLATE_ENGINE_RELATORIO.md"` adicionado a
    `ALLOWED_ROOT_FILES` (ordem alfabética).
  - `ALLOWED_TREE_ROOTS` ganhou `"assets"` (antes: apenas `_sistema`,
    `tests`).
  - `ALLOWED_TREE_EXTRA_FILES` ganhou os 2 caminhos exatos dos PNGs
    oficiais — **decisão deliberada**: em vez de ampliar
    `ALLOWED_TREE_EXTENSIONS` para aceitar `.png` livremente em
    qualquer lugar das árvores permitidas (o que aceitaria qualquer
    PNG futuro sem revisão consciente), cada arquivo binário que entra
    no pacote é listado por caminho exato — mesma disciplina já usada
    para `tests/README.md`/`tests/requirements-test.txt`. Sem essa
    mudança, os 2 arquivos oficiais seriam silenciosamente **excluídos**
    do ZIP pela allowlist fail-closed existente, e a funcionalidade de
    ingestão dos built-ins quebraria na máquina do usuário final (os
    PNGs não estariam lá para `ingest_builtin_templates()` copiar).
  - `REQUIRED_TREE_FILES` **não** foi alterado — mantendo o mesmo
    precedente do Prompt 35 (`metadata_manager.py` também não entrou
    nessa lista): ela é deliberadamente um subconjunto mínimo de
    infraestrutura fundamental, não todo módulo de feature. Os 2 PNGs
    e `template_engine.py` são `OPTIONAL` do ponto de vista do
    empacotador (a ausência não impede o build), mas `REQUIRED` do
    ponto de vista funcional do produto — essa distinção já existe no
    próprio arquivo e não foi violada.

**Nenhum outro arquivo foi tocado** — confirmado via `find -newer`
comparando contra o estado anterior à sessão: `media_catalog.py`,
todos os módulos irmãos (`visual_editor.py`, `captions_style.py`,
`metadata_manager.py`, etc.) e todas as migrations permanecem
byte-a-byte inalterados.

## 3. Comportamento novo

- `TemplateEngine.create_template(name=..., layout=..., template_type=...,
  source_path=None, enabled=True, extra=None)` — cria um `Template`
  validado.
- `TemplateEngine.get_template(template_id)` — lê um `Template`, erro
  estruturado (`TemplateNaoEncontradoError`) se ausente.
- `TemplateEngine.update_template(template_id, **fields)` — atualização
  parcial validada (campos aceitos: `name`, `layout`, `template_type`,
  `source_path`, `enabled`, `extra`); um `layout` inválido não altera o
  registro persistido.
- `TemplateEngine.list_templates(template_type=None, enabled_only=False)`
  — listagem filtrável.
- `TemplateEngine.get_preview_info(template_id)` — metadata de preview
  (`source_path`, `width`, `height`) sem gerar arquivo novo.
- `TemplateEngine.ingest_builtin_templates(assets_dir=None)` — ingestão
  idempotente dos 2 templates oficiais: copia (nunca referencia
  in-place) os PNGs de `assets/templates_oficiais/` para
  `AppPaths.templates` via `StorageManager` (`allocate_temp` +
  `promote_to_final`, mesmo padrão de todo Part B anterior), cria/
  garante as linhas `Template` `BUILT_IN` correspondentes.
- `validate_layout(layout)` — função pública de validação/normalização
  de `layout`, reutilizável fora do Engine (ex.: pelo futuro Layout
  Mapper/Importer sem duplicar a lógica).

## 4. Decisões arquiteturais

### 4.1 — Reuso de infraestrutura pré-existente (ponto obrigatório 1/5)

Confirmado por leitura direta, ANTES de codificar, que **nenhuma**
divergência existe entre o que o Prompt descreve e o estado real do
código — logo nada foi recriado:

| Item exigido pelo Prompt | Confirmado em | Reutilizado como |
|---|---|---|
| Entidade `Template` | `_sistema/domain/models.py:381-390` | Usada diretamente, nenhum campo novo |
| Tabela `templates` | `storage/migrations/m001_initial.py:76` | Usada diretamente via `EntitySpec` já mapeado |
| `AppPaths.templates` | `app_paths.py` (campo já existente) | Destino de todo arquivo gerenciado de template |
| `CATEGORY_TEMPLATE` | `storage_manager.py` (`_root_category_map`) | `allocate_temp`/`promote_to_final` já reconhecem a categoria |
| `BADGE_TEMPLATE_APPLIED` | `media_catalog.py:325,344,442` | Confirmado intocado (permanece hardcoded `"0"`) |

Nenhuma migration nova — `LATEST_SCHEMA_VERSION` permanece `9`
(confirmado em teste: `test_nenhuma_migration_nova_schema_version_permanece`).

### 4.2 — Esquema de `layout` (ponto obrigatório 2/5)

`Template.layout` = `{"zones": [<zone>, ...]}`, fechado a essa única
chave de topo. Cada zona: `zone_type` (vocabulário fechado
`BACKGROUND`/`VIDEO`/`CAPTION`/`AI_TEXT`/`LOGO`), geometria normalizada
`x`/`y`/`width`/`height` (mesmo estilo de validação de
`visual_editor.py::_validate_crop`/`_validate_range` — reimplementado,
nunca importado), `style` opcional (fechado a `opacity` +
`background_color`, reaproveitando o padrão de validação de cor
hexadecimal de `captions_style.py::_validate_color`, também
reimplementado, nunca importado).

**`z_index` decidido como IMPLÍCITO pela posição na lista** (não um
campo explícito): um campo redundante criaria uma segunda fonte de
verdade que poderia divergir da posição real (item 7 do GATE
ADVERSARIAL). Validado estruturalmente: a primeira zona da lista deve
ser sempre `BACKGROUND`.

**Cardinalidade por `zone_type`**, decidida por inspeção visual direta
dos 2 templates oficiais fornecidos:
- `BACKGROUND`: exatamente 1, sempre a primeira zona, geometria
  **imposta** ao frame inteiro (`x=0,y=0,width=1,height=1`) — os 2
  templates fornecidos confirmam que um "background" é sempre o quadro
  inteiro nesta versão do produto; um grau de liberdade extra (fundo
  parcial) não tem necessidade demonstrada hoje.
- `VIDEO`: exatamente 1 (um Template sem zona de vídeo não tem sentido
  neste produto).
- `CAPTION`/`AI_TEXT`/`LOGO`: 0..N cada — nenhum dos 2 templates
  oficiais define essas zonas hoje (a proposta inicial de ambos só tem
  `BACKGROUND`+`VIDEO`), mas a arquitetura já suporta múltiplas zonas de
  logo (multi-marca) ou zero zonas de legenda, sem exigir migração de
  schema no futuro.

### 4.3 — Preview/thumbnail (ponto obrigatório 3/5)

Confirmado por teste direto (não presumido): `requirements.txt` não tem
Pillow/PIL; `MediaProbe` **rejeita** imagem estática
(`error_code='DURACAO_INVALIDA'`); `ffprobe` **lê** dimensões de PNG
estático normalmente.

**Decisão: metadata-only** — nenhum arquivo de thumbnail físico é
gerado nesta etapa. As duas leituras foram avaliadas: (a) gerar um
arquivo redimensionado via `ffmpeg -vf scale=W:H` vs. (b) persistir só
`width`/`height` + `source_path` original. Escolhida (b) porque não
existe hoje nenhuma tela de UI consumindo um thumbnail físico — gerar
um arquivo extra sem consumidor real seria overengineering (vedado
pelo CLAUDE.md), e a resolução fornecida (941×1672px) já é adequada
para uma futura UI redimensionar via CSS/framework diretamente a
partir do `source_path`. `width`/`height` são lidos via `ffprobe` no
momento da ingestão e persistidos em `Template.extra` (nunca
recalculados a cada leitura). Se uma necessidade real de thumbnail
pré-computado surgir (ex.: performance de galeria com centenas de
templates), é uma decisão a revisitar com justificativa própria
naquele momento.

### 4.4 — Preservação dos arquivos originais (ponto obrigatório 4/5)

Os 2 PNGs em `assets/templates_oficiais/` são **fonte de leitura
apenas** — o módulo nunca escreve neles. Prova: `test_ingest_
builtin_templates_originais_nunca_alterados` compara SHA-256 + mtime
antes/depois de duas chamadas de ingestão (incluindo a segunda,
idempotente) e confirma bytes e timestamp idênticos. O destino
gerenciado (`AppPaths.templates/builtin_<slug>.png`) é uma **cópia**
(via `allocate_temp(create=True)` + escrita + `promote_to_final`,
nunca `LocalFileImporter`-style referência in-place), com nome
determinístico (não UUID) por ser um catálogo pequeno e fixo, não um
artefato por-job.

### 4.5 — Arquitetura não depende de "exatamente 2" (ponto obrigatório 5/5)

`BUILTIN_TEMPLATE_SPECS` é uma tupla de especificações — a lógica de
ingestão itera sobre ela sem nenhum número hardcoded em nenhum ponto.
Prova direta: `test_terceiro_template_custom_coexiste_sem_hardcode_
de_dois` insere um 3º `Template` `CUSTOM` via `create_template` e
confirma que ele coexiste normalmente com os 2 `BUILT_IN`, todos
listáveis/filtráveis sem exceção especial. Adicionar um 3º/4º/N-ésimo
built-in no futuro (via update do produto) é apenas adicionar uma
entrada à tupla — nenhuma mudança de lógica.

### 4.6 — Vocabulário fechado de `template_type`

`{"BUILT_IN", "CUSTOM"}` — mínimo pedido pelo roadmap. `BUILT_IN` é
escrito apenas por `ingest_builtin_templates`; `CUSTOM` é o default de
`create_template` (usado pelo futuro Importer genérico, Prompt 38,
sem nenhuma mudança nesta classe).

### 4.7 — Idempotência e concorrência da ingestão

Identidade de ingestão = `extra["builtin_slug"]` (nunca o campo mutável
`name`, que um futuro Layout Mapper pode renomear). Checagem
"já ingerido?" roda sob uma transação `BEGIN IMMEDIATE`
(`LocalDatabase.transaction()`) — leitura + decisão + escrita atômicas,
testado com duas instâncias concorrentes disputando o mesmo SQLite via
`threading.Barrier`
(`test_ingest_builtin_templates_concorrente_duas_instancias_nao_
duplica`): nunca gera 4 linhas para 2 templates. Re-ingestão sobre um
`BUILT_IN` já existente **nunca sobrescreve** `layout`/`name`
ajustados por fora (só garante que o arquivo gerenciado ainda existe,
recopiando do original se foi apagado por fora do produto).

## 5. Migrations

Nenhuma. `LATEST_SCHEMA_VERSION` permanece `9` — a tabela `templates`
já suportava tudo que este Prompt precisa (`layout_json` e
`extra_json` já são JSON livre, usados para o schema de zonas e para
`builtin_slug`/`width`/`height` respectivamente).

## 6. Testes automatizados executados

- `tests/test_template_engine.py`: **62 testes, todos passando**
  (CRUD; matriz adversarial de geometria — 9 casos de geometria
  inválida parametrizados + borda exata aceita; vocabulário fechado de
  `zone_type`/`template_type`; cardinalidade de zonas — falta de
  BACKGROUND/VIDEO, duplicidade, BACKGROUND fora de ordem/geometria;
  `style` válido/inválido incluindo cor hex parametrizada; ingestão —
  criação, cópia nunca in-place, originais nunca alterados,
  idempotência sequencial, preservação de layout ajustado após
  re-ingestão, auto-cura de arquivo apagado, arquivo oficial ausente
  falha sem substituto genérico, concorrência com `threading.Barrier`;
  prova do "não depende de exatamente 2"; preview metadata-only;
  restart com novas instâncias; validação de construtor; AST
  estrutural — zero import de módulo irmão/Geração 1, confirmação de
  `media_catalog.py` intocado, confirmação de nenhuma migration nova,
  confirmação dos 2 assets oficiais presentes com as dimensões
  esperadas).
- Suíte completa: **2418 passed, 1 skipped, 36 subtests passed**
  (148.83s) — delta de **+62** em relação à baseline anterior (2356),
  exatamente o número de testes novos. Zero regressões.
- `python3 -m compileall -q _sistema tests` — OK.
- `find -newer` confirmou que **somente** os arquivos listados nas
  seções 1/2 foram tocados nesta sessão.

## 7. Como testar manualmente

1. Rodar `RODAR_TESTES.bat` (ou
   `python3 -m pytest tests/test_template_engine.py -v`) e confirmar
   62 testes verdes.
2. Em um shell Python dentro do projeto instalado:
   ```python
   from _sistema.app_paths import PATHS, ensure_app_directories
   from _sistema.storage.database import LocalDatabase
   from _sistema.storage_manager import StorageManager
   from _sistema.template_engine import TemplateEngine

   ensure_app_directories(PATHS)
   db = LocalDatabase(PATHS.database / "painel.db")
   db.initialize()
   storage = StorageManager(PATHS, database_path=PATHS.database / "painel.db")
   engine = TemplateEngine(db, storage_manager=storage, app_paths=PATHS)

   templates = engine.ingest_builtin_templates()
   for t in templates:
       print(t.name, t.template_type, t.enabled, t.source_path)
   ```
   Confirmar que 2 arquivos aparecem em `AppPaths.templates` e que
   rodar a mesma chamada de novo não duplica nem falha.
3. Confirmar que `assets/templates_oficiais/*.png` continuam com o
   mesmo tamanho/hash de antes de rodar a ingestão.

## 8. Riscos conhecidos

- O nome de arquivo determinístico (`builtin_<slug>.png`) é uma
  exceção deliberada ao padrão UUID4 usado por artefatos derivados de
  job — se um Prompt futuro precisar versionar múltiplas revisões
  físicas do mesmo built-in (não apenas o layout, mas a própria
  imagem), esse esquema de nome único por slug precisará ser
  revisitado.
- `_read_image_dimensions` depende de `ffprobe` no PATH — já uma
  dependência de produção comprovada em todo Part B anterior, mas se
  ausente na máquina do usuário a ingestão falha com erro estruturado
  (`TemplateEngineError`), não silenciosamente.
- Nenhum mecanismo de `delete_template` foi implementado (o Prompt
  pediu explicitamente apenas create/read/update/list) — se um Prompt
  futuro precisar de exclusão, é uma decisão própria daquele momento
  (ex.: o que acontece com um `BUILT_IN` "deletado" após um update do
  produto reintroduzi-lo).

## 9. Dívida técnica criada

- Nenhuma migração de schema, mas o esquema de `layout` vive apenas em
  validação Python (`validate_layout`), não em uma constraint SQL —
  consistente com o padrão já estabelecido de todo o projeto (JSON
  livre validado na camada de aplicação, nunca no SQLite).
- A cardinalidade "exatamente 1 BACKGROUND coprindo o frame inteiro"
  pode precisar ser revisitada se um template com background composto
  (múltiplas camadas de fundo) for pedido no futuro — documentado como
  decisão consciente, não uma lacuna descoberta tarde.

## 10. Pendências

- Layout Mapper visual (Prompt 39) — UI de arrastar/redimensionar
  zonas sobre a imagem do template.
- Importador genérico de template (Prompt 38) — upload de PNG/JPG
  arbitrário do usuário + configuração de zonas.
- Render Engine (ainda não existe no produto) — é o que eventualmente
  fará `BADGE_TEMPLATE_APPLIED` deixar de ser `"0"`.

## 11. Nota sobre o ZIP (política não-bloqueante já estabelecida)

Uma eventual divergência de hash/listagem do ZIP de release na Seção
11 **não é bloqueante**, conforme a política já estabelecida nas
entregas anteriores deste projeto.
