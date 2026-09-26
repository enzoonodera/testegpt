# PROMPT 41 — Templates independentes do workflow (Template Selector)

## Texto literal do roadmap

    Garanta arquiteturalmente que Template NÃO seja uma etapa obrigatória.

    Deve ser possível:
    - vídeo pronto → template → exportar
    - Smart Clip → salvar cortes sem template
    - vídeo editado → aplicar template
    - 200 vídeos → aplicar template em lote
    - trocar template depois da edição
    - remover template

    Trocar template não pode exigir repetir Whisper/análise/cortes se nada
    disso mudou.

## 1. Arquivos criados

- `_sistema/template_selector.py` — novo módulo (`TemplateSelector`), engine fino sobre `EditProjectManager` que persiste qual `Template` do catálogo está associado a um `Project`, exclusivamente na categoria `TEMPLATE` já reservada desde o Prompt 27.
- `tests/test_template_selector.py` — 35 testes novos.
- `PROMPT_41_TEMPLATE_SELECTOR_RELATORIO.md` (este relatório).

## 2. Arquivos modificados

- `empacotar_release.py` — `PROMPT_41_TEMPLATE_SELECTOR_RELATORIO.md` adicionado a `ALLOWED_ROOT_FILES` (ordem alfabética).

Nenhum outro arquivo foi tocado. Em particular, `edit_project.py` (a constante `TEMPLATE` já existia, reaproveitada sem alteração), `template_engine.py` e `media_catalog.py` permanecem intocados — confirmado por leitura e por teste AST dedicado (seção 6).

## 3. Comportamento novo

`TemplateSelector(manager, template_engine)` expõe:

- `get_template_selection(project_id) -> str | None` — `template_id` associado hoje, ou `None`.
- `set_template(project_id, template_id) -> str` — associa/troca, validando o `template_id` contra o catálogo (`TemplateEngine.get_template`) ANTES de qualquer escrita.
- `clear_template(project_id) -> bool` — remove a associação; `True` se havia algo para remover, `False` (no-op seguro, nunca erro) se já não havia.
- `bulk_set_template(project_ids, template_id) -> BulkTemplateSelectionResult` — aplica o mesmo template a vários Projects; `template_id` validado uma única vez; cada `project_id` isolado (`requested`/`updated`/`unchanged`/`failed`).
- `bulk_clear_template(project_ids) -> BulkTemplateSelectionResult` — simétrico, para remoção em lote.

Nenhum FFmpeg, `Job`, `Artifact` ou render real é executado — puramente decisão/configuração, como o Prompt exige.

## 4. Decisões arquiteturais

**Representação de "nenhum template" (seção 0.2 do módulo)**: a AUSÊNCIA da categoria `TEMPLATE` no `edit_state` é a representação de "nenhum template associado" — não um sentinela `{"template_id": None}` gravado explicitamente. Essa é também a representação natural de um Project que nunca teve `set_template` chamado, evitando dois estados distintos para o mesmo significado. Consequência: `clear_template` usa `EditProjectManager.remove_category`, que já garante idempotência (devolve `False` sem lançar quando a categoria já está ausente, sem nenhuma escrita nesse caso) e isolamento de outras categorias — reaproveitada, não reimplementada.

**`set_category` vs. `update_category`**: `set_template` usa `set_category` diretamente — como `TEMPLATE` tem um único campo, o valor final já é conhecido por completo antes de qualquer escrita, e `set_category` já executa leitura+decisão de NO-OP+escrita na mesma `BEGIN IMMEDIATE`. `bulk_set_template` usa `update_category` com um mutator que captura, via flag de closure (mesmo padrão de `AudioEngine.bulk_set_audio_settings`), se o valor mudou — a classificação updated/unchanged acontece DENTRO da mesma transação atômica, nunca via uma leitura separada antes da escrita.

**FK real e propagação de erro**: `set_template`/`bulk_set_template` chamam `TemplateEngine.get_template(template_id)` antes de escrever; `TemplateNaoEncontradoError` propaga DIRETAMENTE (decisão explícita, não envolvida em um erro próprio) — ela já representa exatamente o conceito de domínio necessário, e uma segunda classe de erro para o mesmo significado seria abstração sem benefício. Nenhum tratamento especial por `template_type` (`BUILT_IN` vs. `CUSTOM`) — testado explicitamente.

**Lote de remoção (`bulk_clear_template`)**: implementado por simetria com `bulk_set_template`, decisão documentada (não pedida explicitamente pelo roadmap, mas "remover template" já é uma operação de primeira classe deste módulo — negar a mesma forma de lote só para remoção criaria uma API assimétrica sem motivo).

**Badge `BADGE_TEMPLATE_APPLIED` — decisão (a)**: mantido como `"0"` hardcoded, pelo MESMO racional já documentado para `BADGE_AUDIO_PROCESSED` (Prompt 30) — este Prompt é puramente decisão/configuração, nenhum `Job`/`Artifact`/render real é criado. "Aplicado" deveria significar "processado/renderizado" (mesmo padrão semântico de `BADGE_REFRAMED_9_16`/`BADGE_TRANSCRIBED`, que exigem checkpoint + Artifact reais), não "selecionado no catálogo" — acender o badge apenas por uma seleção seria uma falsa evidência de processamento, o mesmo risco que o precedente de `AUDIO_PROCESSED` já identificou e evitou. `media_catalog.py` não foi tocado; existe um teste negativo explícito provando isso (`test_selecionar_template_nao_acende_badge_template_applied`).

**Garantia central de isolamento**: `TemplateSelector` nunca lê/escreve nenhuma categoria além de `TEMPLATE` — validado por teste AST estrutural e por um teste end-to-end que grava `CAPTIONS`/`CUTS` reais, aplica/troca/remove um template só por este módulo, e relê `CAPTIONS`/`CUTS` depois de cada uma das três operações, provando que `fingerprint`/`revision`/`updated_at` de ambas permanecem exatamente os mesmos — e o inverso (alterar `CUTS` depois de já haver `TEMPLATE` setado não muda `TEMPLATE`).

**Concorrência**: `set_category`/`update_category` já executam leitura+decisão+escrita numa única `BEGIN IMMEDIATE`, serializando duas chamadas concorrentes. Testado com `threading.Barrier`: duas instâncias aplicando templates DIFERENTES ao mesmo Project simultaneamente nunca corrompem o estado — o resultado final é deterministicamente um dos dois valores por completo, nunca uma mistura; qual dos dois vence depende da ordem real de aquisição do lock do SQLite pelas threads do SO, não definida por este módulo (mesma garantia já testada em `edit_project.py`/`audio_engine.py`).

## 5. Migrations

Nenhuma. A coluna `edit_state_json` já suporta a categoria `TEMPLATE` desde `m001_initial.py`; nenhum schema novo foi necessário.

## 6. Testes automatizados executados

```
python3 -m compileall -q _sistema tests
pytest tests/test_template_selector.py -q
→ 35 passed in 0.83s

pytest tests/ -q
→ 2591 passed, 1 skipped, 36 subtests passed in 152.53s (0:02:32)
```

Baseline anterior: 2556 passed. Delta: +35 (exatamente os testes novos deste Prompt), zero regressões em qualquer outro módulo.

Cobertura: FK real (template inexistente rejeitado; `BUILT_IN` e `CUSTOM` funcionando igualmente, provado também via teste estrutural); aplicar/trocar/remover isoladamente e em sequência; `clear_template` como no-op seguro (inclusive dupla remoção); isolamento de fingerprint nas duas direções (`CAPTIONS`/`CUTS` intocadas por operações de `TEMPLATE`, e `TEMPLATE` intocado por operações de `CUTS`); independência de ordem (`CUTS` sem nunca ter tido `TEMPLATE`; `TEMPLATE` em Project "cru"); `bulk_set_template` (todos válidos, mix válido/inexistente, reenvio idempotente cai em `unchanged`, lote vazio rejeitado, FK validada antes de qualquer escrita no lote); `bulk_clear_template` simétrico; concorrência real via `threading.Barrier`; restart com instâncias totalmente novas; prova negativa do badge; garantias estruturais via AST (nenhuma referência real de código a outra categoria, nenhum import de `media_catalog`/`job_engine`/`subprocess`/ffmpeg); confirmação de que nenhuma migration nova foi criada.

## 7. Como testar manualmente

```
.venv-test\Scripts\python.exe -m pytest tests\test_template_selector.py -v
```

Fluxo manual (Python REPL ou script):

```python
from _sistema.app_paths import build_app_paths, ensure_app_directories
from _sistema.storage.database import LocalDatabase
from _sistema.storage_manager import StorageManager
from _sistema.edit_project import EditProjectManager
from _sistema.template_engine import TemplateEngine
from _sistema.template_selector import TemplateSelector
from _sistema.domain import Project, SourceAsset, Video

paths = build_app_paths(data_root="C:/temp/painel_teste")
ensure_app_directories(paths)
db = LocalDatabase(paths.database / "painel.db"); db.initialize()
storage = StorageManager(paths, database_path=paths.database / "painel.db")
manager = EditProjectManager(db)
template_engine = TemplateEngine(db, storage_manager=storage, app_paths=paths)
selector = TemplateSelector(manager, template_engine)

src = SourceAsset(source_uri="C:/videos/x.mp4", original_name="x.mp4"); db.insert(src)
video = Video(source_asset_id=src.id, name="v"); db.insert(video)
project = Project(video_id=video.id, name="p"); db.insert(project)

tpl = template_engine.create_template(name="Meu Template", layout={"zones": [
    {"zone_type": "BACKGROUND", "x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0},
    {"zone_type": "VIDEO", "x": 0.1, "y": 0.1, "width": 0.8, "height": 0.8},
]})

selector.set_template(project.id, tpl.id)
print(selector.get_template_selection(project.id))  # tpl.id
selector.clear_template(project.id)
print(selector.get_template_selection(project.id))  # None
```

## 8. Riscos conhecidos

- Este módulo não valida se o `Template` referenciado permanece consistente ao longo do tempo (ex.: um `Template` `CUSTOM` cujo arquivo de origem em `assets/templates_customizados/` foi apagado externamente) — essa validação já é responsabilidade de `template_engine.py`/`StorageManager` (auto-cura de arquivo gerenciado, já testada naquele módulo), não duplicada aqui.
- Nenhum mecanismo de "notificação" existe hoje para avisar um Project cujo template foi removido do catálogo (não há `delete_template` implementado ainda — fora de escopo confirmado) — quando essa operação existir num Prompt futuro, será necessário decidir o que acontece com Projects que referenciam um template removido (fora do escopo desta etapa).

## 9. Dívida técnica criada

Nenhuma nova. O padrão de erro/validação/lote segue exatamente o já estabelecido por `audio_engine.py`/`visual_editor.py`.

## 10. Pendências

- Nenhuma pendência técnica nesta etapa. O Render Engine (Prompt 42) é quem efetivamente consumirá `TemplateSelector.get_template_selection` para aplicar de fato um template a um vídeo — fora de escopo aqui, como explicitamente delimitado pelo Prompt.

## Ataques adversariais adicionais executados (item 15 do GATE)

1. Confirmado, por teste AST (não apenas leitura), que nenhum identificador de código deste módulo referencia `CUTS`/`CROP`/`AUDIO_SETTINGS`/`CAPTIONS`/`REFRAME`/`SPEED`/`TEXT_LAYERS`/`METADATA_MODE` — só a prosa da docstring os menciona, o que não conta como violação da garantia de isolamento.
2. Confirmado que `template_id` malformado (string vazia) é rejeitado antes de qualquer chamada ao catálogo ou ao `EditProjectManager`.
3. Confirmado que `project_id` malformado (não-UUID) nunca deixa um `ValueError` cru escapar de nenhum método público — sempre reclassificado como `CampoInvalidoError`.
4. Confirmado que `bulk_set_template` com `template_id` inexistente rejeita ANTES de qualquer escrita em qualquer `project_id` do lote (nenhuma escrita parcial).
5. Confirmado que dupla remoção (`clear_template` chamado duas vezes seguidas) é idempotente e nunca lança na segunda chamada.
6. Confirmado que `bulk_clear_template` de um lote onde todos os projetos já não têm template cai inteiramente em `unchanged`, nunca em `updated`.
7. Confirmado, por teste com `threading.Barrier`, que duas escritas concorrentes de templates diferentes no mesmo Project nunca produzem um estado corrompido/misto.
8. Suíte completa (2591 testes) rodada do zero após todas as mudanças, sem nenhuma regressão em nenhum módulo não relacionado.
