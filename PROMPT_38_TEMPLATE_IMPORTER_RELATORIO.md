# PROMPT 38 — Template Importer (upload de PNG/JPG do usuário)

## 1. Arquivos criados

- `_sistema/template_importer.py` (~330 linhas) — `TemplateImporter`
  (adaptador fino sobre `TemplateEngine`), `TemplateImportError`
  (estende `TemplateEngineError`), validadores de arquivo/conteúdo,
  layout inicial genérico.
- `tests/test_template_importer.py` (27 testes, todos passando).
- `PROMPT_38_TEMPLATE_IMPORTER_RELATORIO.md` (este arquivo).

## 2. Arquivos modificados

- `empacotar_release.py`: `"PROMPT_38_TEMPLATE_IMPORTER_RELATORIO.md"`
  adicionado a `ALLOWED_ROOT_FILES` (ordem alfabética, logo após o do
  Prompt 36). Nenhuma outra mudança — `template_importer.py` não
  precisou de nenhum ajuste de allowlist adicional (é `.py` dentro de
  `_sistema/`, já coberto pelo manifesto existente).

**Nenhum outro arquivo foi tocado** — confirmado via `find -newer`:
`template_engine.py`, `media_catalog.py`, todos os módulos irmãos e
todas as migrations permanecem intocados. `REQUIRED_TREE_FILES` não
foi alterado, seguindo o mesmo precedente do Prompt 36 (módulo de
feature novo não entra nessa lista, que é deliberadamente um
subconjunto mínimo de infraestrutura fundamental).

## 3. Comportamento novo

- `TemplateImporter(template_engine, *, storage_manager, app_paths)` —
  construtor validado (item 8 do GATE ADVERSARIAL).
- `TemplateImporter.import_template(file_path, *, name=None) ->
  Template` — único método público relevante. Recebe o caminho de um
  PNG/JPG fornecido pelo usuário, valida arquivo + conteúdo, copia
  (nunca referencia in-place) para `AppPaths.templates`, e cria um
  `Template` `CUSTOM` novo via `TemplateEngine.create_template` (que já
  valida `layout`/vocabulário — nunca reimplementado aqui).
- Erros estruturados via `TemplateImportError` (`.code` em um dos 7
  `ERROR_*`), nunca uma exceção Python crua.

## 4. Decisões arquiteturais (com justificativa)

### 4.1 — Validação de arquivo

Vocabulário fechado `IMPORT_IMAGE_EXTS = {".png", ".jpg", ".jpeg"}` —
exatamente o que o roadmap pede, com `.jpeg` como grafia alternativa
comum. `_validate_file_for_import` reimplementa (nunca importa) a
disciplina de `source_import.py::_validate_file_for_import`:
existência, regularidade, extensão, tamanho > 0, legibilidade — nessa
ordem, mesmos códigos de erro estruturados no mesmo espírito.
`TemplateImportError` **estende `TemplateEngineError`** (não uma base
nova desacoplada): mesmo domínio (`Template`), então um chamador que já
captura `TemplateEngineError` genericamente continua funcionando; o
atributo `.code` (mesmo padrão de `SourceImportError.code`) permite
discriminação fina sem parsing de mensagem.

### 4.2 — Validação de conteúdo (a defesa real)

Depois da validação de arquivo, `_read_image_dimensions` (de
`template_engine.py`, mesmo domínio — reaproveitada, nunca duplicada)
é chamada. Qualquer falha (não decodifica, timeout, dimensão <= 0) vira
`TemplateImportError(ERROR_CONTEUDO_NAO_E_IMAGEM, ...)` — nunca aceita
o arquivo mesmo assim. Testado diretamente contra os dois cenários do
incidente: um `.txt` disfarçado de `.png` (texto puro) e um arquivo com
só a assinatura PNG truncada (8 bytes, sem o resto da imagem) — ambos
rejeitados.

### 4.3 — Tamanho máximo: 20 MiB

Sem precedente no produto. Decisão: `MAX_IMPORT_FILE_SIZE_BYTES = 20 *
1024 * 1024`. Os 2 templates oficiais do Prompt 36 pesam 379KB e
1.3MB — 20 MiB dá margem de 15-50x sobre o maior asset real conhecido,
cobrindo uma imagem de alta resolução com textura pesada sem deixar o
upload irrestrito (que permitiria, por engano ou abuso, ler dezenas/
centenas de MB inteiros em memória via `read_bytes()`). Rejeitado
**antes** da leitura de conteúdo (`path.stat().st_size`), barato e
cedo. Testado no limite exato (`MAX` bytes, não `MAX+1`) para provar
que o corte é `> MAX`, não `>= MAX`.

### 4.4 — Cópia binária-segura (imunidade ao bug do Prompt 36)

`allocate_temp(suffix=<extensão original>, create=True)` (nome UUID4
do próprio `StorageManager`) + `Path.write_bytes(Path.read_bytes())` +
`promote_to_final(temp_path, AppPaths.templates/temp_path.name,
overwrite=False)`. **Confirmado**: `Path.read_bytes()`/`write_bytes()`
delegam a `io.open(path, "rb"/"wb")` — o modo `"b"` explícito garante
binário em qualquer SO, ao contrário do `os.open()` cru (sem
`O_BINARY`) que causou o bug do Prompt 36. Prova por teste, não
inferência: `test_copia_preserva_byte_0x1a_no_meio_do_arquivo` e
`test_copia_e_binaria_segura_para_arquivo_sintetico_com_0x1a_no_meio`
comparam bytes/tamanho idênticos com `0x1A` no conteúdo. AST
(`test_copia_binaria_nunca_usa_os_open_cru`) confirma que o módulo nem
importa `os`.

`overwrite=False` (diferente do Prompt 36, que usa `overwrite=True` de
propósito para idempotência por slug fixo): aqui o nome final é sempre
um UUID4 novo, então uma colisão de destino preexistente seria uma
anomalia grave nunca esperada — `overwrite=False` garante falha
ruidosa em vez de sobrescrever silenciosamente o arquivo gerenciado de
outro template.

### 4.5 — Layout inicial genérico

Sem inspeção visual possível de uma imagem arbitrária do usuário
(diferente dos 2 built-ins, cuja zona `VIDEO` veio de inspeção manual),
a zona `VIDEO` nasce em `x=0.1, y=0.1, width=0.8, height=0.8` — 10% de
margem uniforme, cobrindo 64% do frame. Proposta inicial deliberadamente
conservadora, editável depois pelo futuro Layout Mapper (Prompt 39).
`BACKGROUND` segue a mesma regra imposta de `validate_layout` (frame
inteiro) — validado (nunca reimplementado) por
`TemplateEngine.create_template`.

### 4.6 — Sem deduplicação (decisão consciente)

Ao contrário da ingestão built-in (idempotente por slug fixo — catálogo
conhecido e fixo), cada `import_template` cria um `Template` `CUSTOM`
novo e distinto. Dois uploads do mesmo arquivo são dois templates
customizados legítimos e independentes — o usuário pode querer
variações. Nenhuma transação `BEGIN IMMEDIATE` de deduplicação é
necessária: não há decisão atômica "já existe?" a proteger aqui.
Testado: duas importações do mesmo arquivo produzem ids e
`source_path` distintos, ambos coexistindo.

### 4.7 — Arquivo original nunca modificado

O caminho fornecido só é acessado via
`exists`/`is_file`/`stat`/`open("rb")`/`read_bytes()` — nenhuma
escrita, rename ou delete. Provado por teste comparando hash/mtime
antes e depois.

## 5. Migrations

Nenhuma. Nenhum campo novo em `Template`/`templates` foi necessário —
`extra_json`/`layout_json` (já JSON livre) cobrem tudo.

## 6. Testes automatizados executados (saída real, não presumida)

```
$ pytest tests/test_template_importer.py -v
...
27 passed in 1.44s
```

Suíte completa:

```
$ pytest tests/ -q
...
2447 passed, 1 skipped, 36 subtests passed in 155.36s (0:02:35)
```

Delta exato de **+27** em relação à baseline anterior (2420, pós-
correção do Prompt 36) — o número exato de testes novos deste módulo.
Zero regressões em `test_template_engine.py` ou em qualquer outro
arquivo de teste. `python3 -m compileall -q _sistema tests` — OK.
`find -newer` confirmou que somente os arquivos das seções 1/2 foram
tocados.

Cobertura da matriz obrigatória do Prompt: importação bem-sucedida com
`Template` `CUSTOM`/`source_path` copiado/`layout` válido/`extra` com
dimensão correta; rejeição de extensão inválida, arquivo inexistente,
vazio, não-regular, muito grande; rejeição por conteúdo falso (texto
puro e assinatura PNG truncada); prova de bytes idênticos byte a byte
incluindo `0x1A` no meio; arquivo original nunca modificado; duas
importações produzem dois Templates distintos; AST estrutural.

## 7. Como testar manualmente

```python
from _sistema.app_paths import PATHS, ensure_app_directories
from _sistema.storage.database import LocalDatabase
from _sistema.storage_manager import StorageManager
from _sistema.template_engine import TemplateEngine
from _sistema.template_importer import TemplateImporter

ensure_app_directories(PATHS)
db = LocalDatabase(PATHS.database / "painel.db")
db.initialize()
storage = StorageManager(PATHS, database_path=PATHS.database / "painel.db")
engine = TemplateEngine(db, storage_manager=storage, app_paths=PATHS)
importer = TemplateImporter(engine, storage_manager=storage, app_paths=PATHS)

tpl = importer.import_template("/caminho/para/minha_imagem.png")
print(tpl.name, tpl.template_type, tpl.source_path, tpl.extra)
```

Confirmar que o arquivo original em `/caminho/para/minha_imagem.png`
continua intacto e que um novo arquivo aparece em
`AppPaths.templates`. Testar também com um `.txt` renomeado para
`.png` e confirmar a rejeição estruturada (`ERROR_CONTEUDO_NAO_E_
IMAGEM`).

## 8. Riscos conhecidos

- `MAX_IMPORT_FILE_SIZE_BYTES` (20 MiB) é uma estimativa razoável sem
  dado real de uso — se usuários reais tentarem importar imagens
  legitimamente maiores (ex.: fotografias profissionais não otimizadas),
  o limite precisará ser revisitado com dado real.
- Nenhuma verificação de que o `codec_name` reportado pelo `ffprobe`
  é especificamente PNG/JPEG (aceita qualquer formato de imagem que o
  `ffprobe` decodifique, incluindo formatos exóticos que ele reconheça
  mas que não sejam PNG/JPG "de verdade") — decisão deliberada de não
  travar nisso, já que a extensão já filtra a intenção do usuário e o
  objetivo desta camada é impedir CONTEÚDO NÃO-IMAGEM, não policiar o
  codec exato.
- Nenhum limite de dimensão (largura×altura) — só de bytes. Uma imagem
  minúscula em bytes mas com dimensões absurdas (ex.: PNG altamente
  comprimido de 50000x50000px) passaria pela validação de tamanho de
  arquivo. Não é o cenário do incidente que este Prompt endereça, e
  não foi pedido — documentado como possível trabalho futuro.

## 9. Dívida técnica criada

- Nenhuma migração de schema.
- `Template.extra["imported_original_filename"]` guarda o nome
  original do arquivo do usuário para referência futura de UI (ex.:
  "importado de foto_natal.png") — campo livre em `extra`, não
  validado por `TemplateEngine` (é responsabilidade deste módulo,
  documentado aqui).

## 10. Pendências

- Prompt 39 — Layout Mapper visual (ajuste fino da zona `VIDEO`
  proposta genericamente por este Importer).
- Render Engine (ainda não existe).
- UI de upload (drag-and-drop, preview) — este Prompt é só o motor.
