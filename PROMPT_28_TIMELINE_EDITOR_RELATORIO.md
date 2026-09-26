# PROMPT 28 — Editor: corte e timeline — Relatório de entrega

## 0. Contexto e escopo (recapitulando o Prompt)

Texto literal do roadmap (Fase 5, imediatamente após o Prompt 27.5):

> PROMPT 28 — Editor: corte e timeline
> Implemente módulo de edição temporal.
> Funções: trim; split; remover trecho; juntar trechos; reorganizar quando
> suportado; alterar velocidade.
> Salvar tudo como Edit Decisions.
> Não alterar vídeo original.

O próprio Prompt já trazia, na seção 0, uma investigação prévia obrigatória
confirmando: (0.1) `EditProjectManager` (Prompt 27) é a única camada de
persistência de decisões de edição e delega a este Prompt o schema interno
das categorias `cuts`/`speed`; (0.2) o badge `EDITED` do Media Catalog
(Prompt 27.5) já dispara automaticamente ao existir qualquer categoria em
`edit_state`, sem exigir mudança em `media_catalog.py`; (0.3) `MediaProbe`
(Prompt 26) é deliberadamente stateless/sob demanda e este módulo não deve
chamá-lo automaticamente; (0.4) `Video`/`SourceAsset`/`Project` não têm
campo de duração persistida hoje.

## 1. Arquivos criados

- `_sistema/timeline_editor.py` (791 linhas) — novo módulo de edição
  temporal não destrutiva: classe `TimelineEditor`, dataclass `Segment`,
  10 classes de erro estruturado, e todas as operações do roadmap
  (`initialize_timeline`, `get_timeline`, `trim`, `split`, `remove_range`,
  `remove_segment`, `join`, `reorder`, `set_speed`).
- `tests/test_timeline_editor.py` (700+ linhas, 55 testes coletados) —
  suíte dedicada e permanente cobrindo a lista de testes obrigatórios do
  Prompt (seção 4), mais achados do GATE ADVERSARIAL (seção 6 abaixo).
- `PROMPT_28_TIMELINE_EDITOR_RELATORIO.md` — este relatório.

## 2. Arquivos modificados

- `_sistema/edit_project.py` — **mudança aditiva única**: novo método
  público `EditProjectManager.update_category(project_id, category,
  mutator)`. Nenhum método existente teve assinatura ou comportamento
  alterados; as 34 asserções pré-existentes de `tests/test_edit_project.py`
  continuam passando sem nenhuma modificação. Justificativa completa na
  seção 3 abaixo (decisão arquitetural) e na docstring do próprio método.
  Import ajustado: `from typing import Any, Callable, ClassVar` (adição de
  `Callable`).
- `tests/test_edit_project.py` — 8 novos testes cobrindo
  `update_category`: criação quando ausente, leitura do valor atual pelo
  mutator, no-op determinístico quando o resultado é idêntico (mesmo
  fingerprint), isolamento de outras categorias, projeto inexistente,
  mutator que lança exceção não escreve nada (rollback), e duas provas de
  concorrência determinística via `threading.Barrier` (2 threads e 8
  threads, incremento de contador, zero atualizações perdidas).
- `empacotar_release.py` — `PROMPT_28_TIMELINE_EDITOR_RELATORIO.md`
  adicionado a `ALLOWED_ROOT_FILES` (arquivo opcional, não bloqueia build
  na ausência).

Nenhum outro arquivo de produção foi tocado. Em particular, **não foram
alterados**: `media_catalog.py`, `video_promotion.py`, `media_probe.py`,
nenhuma migration existente (`m001`–`m009`), nem `edit_project.py` além do
único método aditivo descrito acima.

## 3. Comportamento novo e decisões arquiteturais

### 3.1 — `EditProjectManager.update_category` (extensão justificada, seção 0.1 do Prompt)

Os métodos públicos existentes de `EditProjectManager` eram `set_category`
(substitui o `data` inteiro de uma categoria, sem olhar o valor anterior),
`get_category` (leitura pura) e `remove_category`. Nenhum deles serve para
"ler o estado atual, computar um novo estado A PARTIR do atual, escrever de
volta" de forma atômica.

Um padrão ingênuo — `get_category()` seguido de `set_category()` como DUAS
chamadas separadas — reabriria exatamente o *lost update* que a seção 0.4
da docstring original de `edit_project.py` já resolveu para substituição
completa: duas chamadas concorrentes de `TimelineEditor` sobre o MESMO
projeto (ex.: uma dando `trim` num segmento, outra dando `split` em outro)
leriam o mesmo estado inicial, decidiriam cada uma com base nele, e a
escrita que chegasse por último apagaria silenciosamente a mudança da
outra.

`update_category(project_id, category, mutator)` executa leitura + decisão
(`mutator(current_data)`) + escrita dentro da MESMA transaction `BEGIN
IMMEDIATE`, fechando essa janela. Se `mutator` lançar exceção, a transação
inteira sofre rollback e a exceção se propaga (provado por teste
dedicado). Fingerprint/revisão são computados exatamente como em
`set_category` — nenhuma lógica de concorrência foi duplicada ou
reimplementada.

Esta é a única mudança em `edit_project.py`, permitida explicitamente pela
seção 0.1 do Prompt ("Se... concluírem que `EditProjectManager` como está
NÃO é suficiente... isso deve vir como uma DECISÃO EXPLÍCITA e
JUSTIFICADA").

### 3.2 — Schema da timeline (`data` da categoria `CUTS`)

```json
{
  "segments": [
    {
      "segment_id": "<uuid>",
      "start": 0.0,
      "end": 40.0,
      "speed": 1.0,
      "source_segment_id": null
    }
  ]
}
```

A lista é ORDENADA (a ordem dos elementos é a ordem de reprodução final).
`start`/`end` são SEMPRE relativos ao vídeo ORIGINAL, nunca ao resultado já
cortado — preserva potencial de reaproveitamento de processamento futuro
por trecho (mesmo espírito do fingerprint por categoria do Prompt 27).

### 3.3 — Velocidade vive DENTRO de `cuts`, nunca em `SPEED` separado

`EditProjectManager` expõe `SPEED = "speed"` como categoria de
CONVENIÊNCIA (nomenclatura, nunca exigência). Este módulo deliberadamente
NÃO usa a categoria `SPEED`: o campo `speed` vive dentro de cada segmento
de `cuts`. Se `speed` vivesse numa categoria separada, as duas poderiam
DESSINCRONIZAR — um `split`/`remove`/`reorder` em `cuts` mudaria o
conjunto de `segment_id` válidos sem que `speed` fosse atualizado no
mesmo commit atômico (`set_category`/`update_category` operam UMA
categoria por chamada). Manter tudo em `cuts` garante que toda operação
seja uma única escrita atômica de um documento consistente. Confirmado
por teste (`test_speed_nunca_e_usada_como_categoria_separada`).

### 3.4 — `reorder`: escopo de "quando suportado" (texto literal do roadmap)

`Project.video_id` é único por projeto — todos os segmentos de uma mesma
timeline já pertencem, por construção, ao mesmo vídeo de origem. `reorder`
é implementado para o caso que o roadmap pede e que o modelo atual
suporta: uma nova PERMUTAÇÃO da lista de segmentos já existente (mesmo
conjunto de `segment_id`, nova ordem). Multi-vídeo por timeline exigiria
estender o schema (campo de origem por segmento, hoje inexistente) — fora
de escopo sem necessidade concreta demonstrada.

### 3.5 — `join`: por que exige contiguidade no vídeo original

Além de adjacência NA ORDEM da timeline (exigida literalmente pelo
Prompt), `join` exige `segmento_a.end == segmento_b.start` (contiguidade
no vídeo original) e velocidades idênticas. A contiguidade não é uma
política de produto arbitrária — é consequência direta do schema escolhido
(um segmento é UM único intervalo `[start, end]`); unir dois trechos não
contíguos exigiria um segmento capaz de representar múltiplos intervalos
descontínuos, o que este schema não suporta e este Prompt não introduz sem
necessidade demonstrada.

### 3.6 — `trim`: só encolhe, nunca expande

O novo `start` (se informado) precisa ser `>=` o atual, e o novo `end`
(se informado) precisa ser `<=` o atual. Expandir um segmento além do que
já foi validado fabricaria a alegação de que um trecho antes excluído
passou a ser válido, sem nenhuma evidência (nem a duração real do arquivo
é verificada por este módulo — seção 0.3 do Prompt). Quem quiser recuperar
um trecho removido faz isso explicitamente via uma nova operação.

### 3.7 — `remove_range` como primitiva única + `remove_segment` como atalho

`remove_range(project_id, segment_id, start, end)` cobre os três casos do
roadmap (trecho no meio == split implícito nos dois lados; trecho tocando
uma borda == equivalente a um trim; trecho == segmento inteiro == remoção
total) através de UMA única função pura. `remove_segment` é um atalho fino
que delega para a mesma lógica interna — nunca duplica validação/remoção.

### 3.8 — Achado do GATE ADVERSARIAL: timeline esvaziada por edição válida == nunca inicializada

Ao tentar quebrar a implementação (seção 6 abaixo), constatei que remover
(via `remove_segment`/`remove_range`) o ÚLTIMO segmento restante produz
`data = {"segments": []}` — indistinguível, para este módulo, do estado
"categoria `cuts` nunca foi definida" (`get_timeline` retorna `()` nos dois
casos, e `initialize_timeline` aceita reinicializar nos dois casos sem
levantar `TimelineJaInicializadaError`). Decisão DELIBERADA, documentada na
seção 1.4B da docstring do módulo: distinguir os dois estados exigiria um
terceiro estado no schema sem benefício concreto demonstrado nesta etapa;
reinicializar uma timeline esvaziada é um caminho de recuperação legítimo,
não perda de dados silenciosa (o histórico de como ela chegou a ficar
vazia já está nas revisões/fingerprints de `EditProjectManager`). Confirmado
por teste dedicado
(`test_remover_o_ultimo_segmento_restante_esvazia_a_timeline_e_permite_reinicializar`).

### 3.9 — Achado do GATE ADVERSARIAL: `source_segment_id` não era validado (corrigido)

`Segment.from_dict` aceitava, sem validação, qualquer tipo para
`source_segment_id` (ex.: um inteiro), quebrando a garantia documentada de
que este campo é sempre `str | None`. Corrigido: valores não-`None`
precisam ser `str` não-vazia, caso contrário `SegmentoInvalidoError`.
Confirmado por teste dedicado
(`test_segment_from_dict_rejeita_source_segment_id_de_tipo_errado`).

## 4. Migrations

Nenhuma migration nova. `LATEST_SCHEMA_VERSION` permanece **9**
(confirmado por teste `test_nenhuma_migration_nova_criada_por_este_prompt`
e por `tests/test_migrations_frozen.py`, já existente, que continua
passando).

## 5. Testes automatizados executados

```
$ /root/.local/bin/pytest -q tests/test_timeline_editor.py
.......................................................  [100%]
55 passed in 1.20s

$ /root/.local/bin/pytest -q tests/test_edit_project.py
..........................................               [100%]
42 passed in 0.75s

$ /root/.local/bin/pytest -q
(suíte completa)
1656 passed, 1 skipped, 36 subtests passed in 118.56s (0:01:58)

$ python3 -m compileall -q _sistema tests empacotar_release.py
(sem saída = sucesso)
```

Baseline antes do Prompt 28: 1593 passed, 1 skipped, 36 subtests.
Delta: **+63 testes** (8 em `test_edit_project.py` + 55 em
`test_timeline_editor.py`) = 1656. Confere.

Cobertura da lista de testes obrigatórios do Prompt (seção 4 do
documento-fonte), todos como testes permanentes e nomeados honestamente
(GATE 12):

- timeline vazia / inicial (`test_timeline_vazia_para_projeto_recem_criado`,
  `test_operacao_antes_de_inicializar_levanta_erro`);
- trim simples (start/end/ambos), trim que reduziria a duração a `<=0`
  (erro), trim que tentaria expandir (erro), trim em segmento inexistente,
  trim sem nenhum argumento;
- split em ponto válido com prova de soma de durações preservada, split
  fora do segmento (erro), split exatamente na borda (erro);
- remoção de trecho no meio (split implícito duplo), trecho tocando uma
  borda, trecho == segmento inteiro, atalho `remove_segment`, intervalo
  fora do segmento (erro), intervalo invertido (erro);
- join de dois segmentos adjacentes (sucesso), join de dois segmentos
  NÃO adjacentes na ordem (erro), join de segmentos não contíguos no vídeo
  original mesmo lado a lado na ordem (erro), join com velocidades
  incompatíveis (erro), join de um segmento consigo mesmo (erro);
- reorder com permutação válida, e as TRÊS formas distintas de violação
  testadas separadamente (id inexistente, id duplicado, id faltando);
- set_speed válido com prova de persistência, set_speed não afeta outros
  segmentos, set_speed fora do intervalo `[0.25, 4.0]` documentado (5
  variantes parametrizadas), set_speed com valor não finito (inf/-inf/nan);
- sequência de operações em cadeia (split → trim → reorder → set_speed)
  provando consistência e ausência de id perdido/duplicado;
- isolamento de fingerprint entre categorias (`TEMPLATE` definida antes,
  `cuts` mutado depois, fingerprint de `TEMPLATE` inalterado) — mesma
  garantia central do Prompt 27, agora provada do ponto de vista deste
  módulo consumidor;
- confirmação de que `SPEED` nunca é gravada como categoria separada;
- integração read-only com o badge `EDITED` do `MediaCatalogService`
  (sem nenhuma alteração em `media_catalog.py`);
- testes estruturais via AST: nenhum import proibido (circuit_breaker,
  retry_policy, publication_idempotency, secrets_manager,
  job_state_machine, e módulos de Geração 1/outros Prompts), nenhuma
  chamada a `subprocess`/`ffmpeg`/`ffprobe` (via nós AST, não grep textual
  — a própria docstring do módulo cita esses termos legitimamente),
  nenhuma criação de `Job`/`Artifact`/`Publication`/`Schedule`/`Video`,
  nenhum acesso direto a `edit_state_json`/`.transaction()`/
  `database.get(Project...)`/`database.save(...)` a partir do módulo;
- confirmação de `LATEST_SCHEMA_VERSION` inalterado (9).

Testes adicionais do GATE ADVERSARIAL (seção 6 abaixo): restart com
instâncias novas, idempotência de operações repetidas, timeline esvaziada
por edição válida, e validação de tipo de `source_segment_id`.

## 6. GATE ADVERSARIAL OBRIGATÓRIO — segunda passagem, tentativa de quebrar a solução

Passagem sistemática pelos 15 pontos do CLAUDE.md, focada no novo módulo e
na extensão de `edit_project.py`:

1. **Storage/split-brain**: `TimelineEditor.__init__` só aceita
   `LocalDatabase` (`TypeError` caso contrário) e constrói
   `EditProjectManager` internamente a partir dela — um único caminho de
   persistência, nunca dois bancos diferentes coexistindo.
2. **Transações/TOCTOU**: identificado e corrigido via
   `update_category` (seção 3.1) — o próprio motivo da extensão.
3. **Crash windows**: não aplicável de forma nova — todas as escritas
   passam por uma única `BEGIN IMMEDIATE` de `update_category`/
   `EditProjectManager`, já coberta pelas garantias do Prompt 27
   (nenhum "meio de operação" persistível é introduzido por este módulo).
4. **Restart**: `test_timeline_sobrevive_reabertura_com_instancias_totalmente_novas`
   — grava com uma instância de `LocalDatabase`/`TimelineEditor`, abre
   OUTRA instância nova apontando para o mesmo arquivo, confirma que o
   estado (incluindo `speed` alterado) é lido corretamente.
5. **Idempotência**: `test_repetir_o_mesmo_trim_sequencialmente_e_deterministico_e_nao_acumula`,
   `test_repetir_o_mesmo_set_speed_sequencialmente_e_deterministico`,
   `test_initialize_timeline_repetido_nunca_apaga_a_timeline_ja_existente`
   — repetir a mesma operação não acumula efeito nem corrompe o estado.
6. **Concorrência**: duas provas com `threading.Barrier` — duas em
   `test_edit_project.py` (2 e 8 threads incrementando um contador via
   `update_category` bruto) e uma em `test_timeline_editor.py`
   (`test_duas_operacoes_concorrentes_em_segmentos_diferentes_nao_se_perdem`,
   2 threads reais dando `set_speed` em segmentos DIFERENTES do MESMO
   projeto simultaneamente — sem `update_category`, uma das duas mudanças
   seria apagada silenciosamente).
7. **Autoridade única**: não aplicável — este módulo não decide
   `Job.status`/claim/cancel/retry/lifecycle; não toca nenhum desses
   conceitos.
8. **Construção dos objetos**: `TimelineEditor.__init__` valida o tipo de
   `database` ANTES de construir `EditProjectManager` — falha de
   construção não deixa nada parcialmente mutado.
9. **Histórico**: não aplicável a este módulo — `EditProjectManager` já
   mantém revisão/fingerprint/timestamp por categoria, não substituídos
   por argumentos de uma chamada de retomada (nenhuma operação de retomada
   existe aqui).
10. **Segredos**: nenhuma mensagem de erro deste módulo inclui
    `str(exc)`/traceback/token/credencial — todas as mensagens de erro
    referenciam apenas valores estruturais (ids, floats, ranges).
11. **Windows**: este módulo nunca abre arquivo, nunca cria lock, nunca
    spawna subprocesso, nunca faz rename/delete/tempfile — confirmado por
    teste estrutural AST (seção 5). Nenhuma semântica específica de
    Windows a revisar.
12. **Teste honesto**: nomes de teste revisados para corresponder
    exatamente ao que provam (ex.: o teste de concorrência usa threads
    reais e `Barrier`, nunca uma exceção capturada chamada de "crash").
13. **Falha de handler**: não aplicável — este módulo não tem handlers de
    fila/job; toda exceção de `mutator` propaga via rollback da transação
    de `update_category` (já provado em `test_edit_project.py`).
14. **Dois chamadores**: coberto pelo item 6 (concorrência).
15. **Prova final**: testes específicos executados, suíte completa
    executada (1656 passed, 1 skipped, 36 subtests), `compileall` limpo,
    diff revisado (arquivos criados/modificados listados nas seções 1-2),
    migrations confirmadas congeladas (seção 4).

**Ataques adicionais executados manualmente (fora da suíte formal, via
scripts ad-hoc) e resultado:**

- `Segment.from_dict({"segment_id": "   "})` com espaços → rejeitado
  (`SegmentoInvalidoError`), já coberto por teste existente.
- `initialize_timeline(..., segment_id="   ")` (id customizado em branco)
  → rejeitado (`SegmentoInvalidoError`).
- `source_segment_id` com tipo não-string (ex.: `12345`) → **aceito
  silenciosamente antes da correção**; corrigido nesta rodada (seção 3.9)
  e agora rejeitado, com teste de regressão.
- Remover o último segmento restante de uma timeline → esvazia o estado
  para `{"segments": []}`, indistinguível de "nunca inicializada" —
  **decisão documentada explicitamente** (seção 3.8), não um bug, com
  teste dedicado provando o comportamento (inclusive a reinicialização
  subsequente).
- `set_speed` com `float("inf")`/`float("-inf")`/`float("nan")` → todos
  rejeitados com erro estruturado da hierarquia do módulo (via `_as_float`,
  antes mesmo de chegar à validação de faixa), nunca um `OverflowError`/
  `ValueError` cru.

Nenhum outro problema foi encontrado que exigisse correção adicional.

## 7. Como testar manualmente

```python
from _sistema.storage.database import LocalDatabase
from _sistema.timeline_editor import TimelineEditor

db = LocalDatabase("caminho/para/painel.db")
db.initialize()
editor = TimelineEditor(db)

segs = editor.initialize_timeline(project_id, duration_seconds=120.0)
left, right = editor.split(project_id, segs[0].segment_id, at=60.0)
editor.set_speed(project_id, left.segment_id, 1.5)
editor.trim(project_id, right.segment_id, start=65.0)
print(editor.get_timeline(project_id))
```

O vídeo correspondente já aparece com o badge `EDITED` em
`MediaCatalogService.get_item(video_id)` assim que `initialize_timeline`
grava a primeira decisão — sem nenhuma chamada adicional.

## 8. Riscos conhecidos

- Este módulo só valida ESTRUTURALMENTE (nunca contra o arquivo real —
  decisão deliberada da seção 0.3 do Prompt). Uma timeline estruturalmente
  válida ainda pode referenciar pontos além da duração real do arquivo se
  o chamador passar um `duration_seconds` incorreto em
  `initialize_timeline` — uma etapa de renderização futura precisará
  confrontar isso com `MediaProbe` por conta própria, fora do escopo
  aqui.
- Nenhum mecanismo de "desfazer" (`undo`) é oferecido por este módulo —
  cada operação produz um novo estado a partir do estado atual; recuperar
  um estado anterior exigiria uma funcionalidade de histórico de revisões
  de `EditProjectManager`, não implementada aqui (fora de escopo do
  Prompt 28).

## 9. Dívida técnica criada

Nenhuma dívida técnica nova deliberada. A decisão da seção 3.8 (timeline
esvaziada == nunca inicializada) é uma simplificação documentada, não uma
dívida — reavaliar apenas se uma necessidade concreta de distinguir os dois
estados surgir em um Prompt futuro.

## 10. Pendências

Nenhuma pendência dentro do escopo deste Prompt. Fora de escopo,
explicitamente adiado por decisão do próprio Prompt (seção 6, "NÃO
FAZER"): renderização real (geração de arquivo de vídeo de saída),
frontend/UI, e integração automática em qualquer fluxo de
importação/promoção existente.

## 11. Entrega / verificação (ZIP)

**Estado da ponte com a máquina Windows real neste momento**: as
ferramentas `mcp__remote-devices__*` (que dão acesso ao computador Windows
do usuário) **caíram/ficaram indisponíveis durante esta rodada** (o MCP
correspondente foi desconectado). Por isso, **esta seção reflete uma
reprodução no SANDBOX, não uma execução real via `device_bash` na máquina
Windows** — declarado aqui explícita e honestamente, conforme exigido pela
seção 5 do Prompt 28 ("se não foi possível gerar esta seção com dados
reais, o relatório deve dizer isso explicitamente").

Os 6 arquivos de lançador (`PAINEL_OFICIAL.bat`, `EMPACOTAR_RELEASE.bat`,
`INSTALAR_DEPENDENCIAS_TESTE.bat`, `RODAR_TESTES.bat`,
`LIMPAR_ANTES_DE_ZIPAR.bat`, `LEIA_ME_PRIMEIRO.txt`) foram obtidos de um
snapshot real, já enviado anteriormente pelo usuário para esta conversa
(`/mnt/user-data/uploads/teste/`, cópia read-only do estado real da
entrega anterior), não fabricados — o mesmo padrão de fallback já usado no
Prompt 27.5 quando `device_bash` estava indisponível.

Comando executado (sandbox):

```
$ cd /home/claude/project && python3 empacotar_release.py --out /tmp/zipcheck/entrega_prompt28.zip
...
[2/3] ZIP gerado atomicamente, com identidade revalidada por arquivo, e ja inspecionado.

[3/3] Calculando SHA-256 do ZIP final...
      SHA-256: d14b4bd4dddadd022c49aadcc255b16933040d72f93a8399c4f77a65e56823c4
      Tamanho: 0.95 MB

======================================================================
EMPACOTAMENTO CONCLUIDO COM SUCESSO
Arquivo: entrega_prompt28.zip
SHA-256: d14b4bd4dddadd022c49aadcc255b16933040d72f93a8399c4f77a65e56823c4
======================================================================
```

Nota: o texto do PRÓPRIO relatório dentro do ZIP contém, necessariamente,
uma versão anterior desta seção (com placeholders) — é fisicamente
impossível um arquivo conter o hash de si mesmo já incluindo esse hash.
A versão AUTORITATIVA, com os dados reais preenchidos, é esta que está
sendo entregue nesta mensagem/anexo, fora do ZIP.

### (a) Saída literal de `unzip -l`

```
Archive:  /tmp/zipcheck/entrega_prompt28.zip
  Length      Date    Time    Name
---------  ---------- -----   ----
    40632  2026-09-23 00:16   ARQUITETURA_ATUAL.md
    13132  2026-09-23 00:16   CLAUDE.md
     2687  2026-09-23 00:16   EMPACOTAR_RELEASE.bat
   169662  2026-09-23 00:16   GATE_19_5_ESTAGIO2_RELATORIO.md
    11446  2026-09-23 00:16   INSTALAR_DEPENDENCIAS_TESTE.bat
     2969  2026-09-23 00:16   LEIA_ME_PRIMEIRO.txt
      866  2026-09-23 00:16   LIMPAR_ANTES_DE_ZIPAR.bat
    24932  2026-09-23 00:16   MAPA_DE_DADOS.md
      686  2026-09-23 00:16   PAINEL_OFICIAL.bat
    29374  2026-09-23 00:16   PRODUCT_INVARIANTS.md
    23512  2026-09-23 00:16   PROMPT_20_CIRCUIT_BREAKER_RELATORIO.md
    14164  2026-09-23 00:16   PROMPT_21_RETRY_INTELIGENTE_RELATORIO.md
    13986  2026-09-23 00:16   PROMPT_22_IDEMPOTENCIA_RELATORIO.md
    17346  2026-09-23 00:16   PROMPT_23_CORRECAO_RELATORIO.md
    13751  2026-09-23 00:16   PROMPT_23_SECRETS_MANAGER_RELATORIO.md
    31899  2026-09-23 00:16   PROMPT_24B_IMPORT_OPTIONS_RELATORIO.md
    25358  2026-09-23 00:16   PROMPT_24_SOURCE_IMPORT_RELATORIO.md
    28360  2026-09-23 00:16   PROMPT_25_SOURCE_CONTEXT_RESOLVER_RELATORIO.md
    29422  2026-09-23 00:16   PROMPT_26_MEDIA_PROBE_RELATORIO.md
    23735  2026-09-23 00:16   PROMPT_27B_VIDEO_PROMOTION_RELATORIO.md
    19021  2026-09-23 00:16   PROMPT_27_5_MEDIA_CATALOG_RELATORIO.md
    28168  2026-09-23 00:16   PROMPT_27_EDIT_PROJECT_RELATORIO.md
    23160  2026-09-23 00:16   PROMPT_28_TIMELINE_EDITOR_RELATORIO.md
    24543  2026-09-23 00:16   REGRESSION_CHECKLIST.md
    26204  2026-09-23 00:16   RISCOS_ATUAIS.md
    55747  2026-09-23 00:16   ROADMAP_COMPLETO.md
     6104  2026-09-23 00:16   RODAR_TESTES.bat
    15403  2026-09-23 00:16   TESTE_MANUAL_WINDOWS_ESTAGIO2.md
    97511  2026-09-23 00:16   _sistema/agendar_tiktok.py
    91980  2026-09-23 00:16   _sistema/agendar_youtube.py
    18611  2026-09-23 00:16   _sistema/app_paths.py
    68310  2026-09-23 00:16   _sistema/batch_engine.py
    27989  2026-09-23 00:16   _sistema/circuit_breaker.py
    55733  2026-09-23 00:16   _sistema/control_manager.py
     2545  2026-09-23 00:16   _sistema/domain/__init__.py
     3307  2026-09-23 00:16   _sistema/domain/checkpoints.py
     5442  2026-09-23 00:16   _sistema/domain/job_state_machine.py
    15978  2026-09-23 00:16   _sistema/domain/models.py
    23434  2026-09-23 00:16   _sistema/edit_project.py
    17683  2026-09-23 00:16   _sistema/gerar_textos.py
    85229  2026-09-23 00:16   _sistema/job_engine.py
    27891  2026-09-23 00:16   _sistema/limpar_metadados_oficial.py
     2724  2026-09-23 00:16   _sistema/login_conta.py
    37607  2026-09-23 00:16   _sistema/media_catalog.py
    23350  2026-09-23 00:16   _sistema/media_probe.py
    44162  2026-09-23 00:16   _sistema/painel_oficial.py
    21683  2026-09-23 00:16   _sistema/publication_idempotency.py
    32885  2026-09-23 00:16   _sistema/recovery_manager.py
    48725  2026-09-23 00:16   _sistema/resource_manager.py
    27252  2026-09-23 00:16   _sistema/retry_policy.py
    17712  2026-09-23 00:16   _sistema/secrets_manager.py
    94188  2026-09-23 00:16   _sistema/shutdown_coordinator.py
    20616  2026-09-23 00:16   _sistema/source_context.py
    49320  2026-09-23 00:16   _sistema/source_import.py
     1036  2026-09-23 00:16   _sistema/state_json.py
     3365  2026-09-23 00:16   _sistema/storage/__init__.py
    19254  2026-09-23 00:16   _sistema/storage/audit.py
    51041  2026-09-23 00:16   _sistema/storage/backup.py
    27352  2026-09-23 00:16   _sistema/storage/database.py
    38955  2026-09-23 00:16   _sistema/storage/legacy_migration.py
     1359  2026-09-23 00:16   _sistema/storage/migrations/__init__.py
     7411  2026-09-23 00:16   _sistema/storage/migrations/m001_initial.py
      764  2026-09-23 00:16   _sistema/storage/migrations/m002_audit_append_only.py
     2395  2026-09-23 00:16   _sistema/storage/migrations/m003_batch_engine.py
     2538  2026-09-23 00:16   _sistema/storage/migrations/m004_circuit_breaker.py
     2135  2026-09-23 00:16   _sistema/storage/migrations/m005_retry_policy.py
     2605  2026-09-23 00:16   _sistema/storage/migrations/m006_publication_idempotency.py
     4713  2026-09-23 00:16   _sistema/storage/migrations/m007_source_asset_declarations.py
     4856  2026-09-23 00:16   _sistema/storage/migrations/m008_source_asset_context.py
     5469  2026-09-23 00:16   _sistema/storage/migrations/m009_video_declarations.py
   119736  2026-09-23 00:16   _sistema/storage_manager.py
    14323  2026-09-23 00:16   _sistema/time_utils.py
    37920  2026-09-23 00:16   _sistema/timeline_editor.py
    14013  2026-09-23 00:16   _sistema/video_promotion.py
    36025  2026-09-23 00:16   empacotar_release.py
       33  2026-09-23 00:16   requirements.txt
     6014  2026-09-23 00:16   tests/README.md
       65  2026-09-23 00:16   tests/__init__.py
    11123  2026-09-23 00:16   tests/fakes_playwright.py
       21  2026-09-23 00:16   tests/requirements-test.txt
    23822  2026-09-23 00:16   tests/test_agendar_tiktok_check_item_detection.py
    19670  2026-09-23 00:16   tests/test_agendar_tiktok_checks_card_scope.py
    36470  2026-09-23 00:16   tests/test_agendar_tiktok_copyright_policy.py
    14773  2026-09-23 00:16   tests/test_agendar_tiktok_date_before_time_order.py
    13400  2026-09-23 00:16   tests/test_agendar_tiktok_interactive_copyright_policy.py
    28453  2026-09-23 00:16   tests/test_agendar_tiktok_preflight_checks.py
     7817  2026-09-23 00:16   tests/test_agendar_tiktok_skip_checks_on_allow.py
    33286  2026-09-23 00:16   tests/test_agendar_tiktok_time_layers.py
    31582  2026-09-23 00:16   tests/test_agendar_youtube_copyright_policy.py
    23904  2026-09-23 00:16   tests/test_agendar_youtube_interactive_copyright_policy.py
    10080  2026-09-23 00:16   tests/test_agendar_youtube_time_layers.py
     7327  2026-09-23 00:16   tests/test_ai_cache_parsing.py
    20247  2026-09-23 00:16   tests/test_app_paths.py
    31931  2026-09-23 00:16   tests/test_backup_restore.py
    70112  2026-09-23 00:16   tests/test_batch_engine.py
    19226  2026-09-23 00:16   tests/test_checkpoints.py
    26822  2026-09-23 00:16   tests/test_circuit_breaker.py
     7389  2026-09-23 00:16   tests/test_config_names_paths.py
    86602  2026-09-23 00:16   tests/test_control_manager.py
    13408  2026-09-23 00:16   tests/test_domain_models.py
    27954  2026-09-23 00:16   tests/test_edit_project.py
     6240  2026-09-23 00:16   tests/test_ffmpeg_processing.py
     4450  2026-09-23 00:16   tests/test_fingerprint_state.py
     5547  2026-09-23 00:16   tests/test_gerar_textos_exception_persistence.py
     8899  2026-09-23 00:16   tests/test_gerar_textos_ollama_adversarial.py
     4796  2026-09-23 00:16   tests/test_gerar_textos_whisper_adversarial.py
    25385  2026-09-23 00:16   tests/test_job_engine.py
     6839  2026-09-23 00:16   tests/test_job_state_machine.py
    17472  2026-09-23 00:16   tests/test_legacy_json_migration.py
     3209  2026-09-23 00:16   tests/test_limpeza_permission_error.py
    34192  2026-09-23 00:16   tests/test_media_catalog.py
    23595  2026-09-23 00:16   tests/test_media_probe.py
     6950  2026-09-23 00:16   tests/test_migrations_frozen.py
    11476  2026-09-23 00:16   tests/test_operational_audit.py
    16612  2026-09-23 00:16   tests/test_painel_oficial_horarios_por_dia.py
     4723  2026-09-23 00:16   tests/test_painel_oficial_youtube_copyright_timeout_menu.py
    20700  2026-09-23 00:16   tests/test_publication_idempotency.py
    36620  2026-09-23 00:16   tests/test_recovery_manager.py
    37274  2026-09-23 00:16   tests/test_release_packaging.py
   105416  2026-09-23 00:16   tests/test_resource_manager.py
     4676  2026-09-23 00:16   tests/test_resume_detection.py
    31831  2026-09-23 00:16   tests/test_retry_policy.py
     7501  2026-09-23 00:16   tests/test_schedule_slots.py
    19241  2026-09-23 00:16   tests/test_secrets_manager.py
    89276  2026-09-23 00:16   tests/test_shutdown_coordinator.py
    21566  2026-09-23 00:16   tests/test_source_context.py
    18065  2026-09-23 00:16   tests/test_source_import.py
    29758  2026-09-23 00:16   tests/test_source_import_options.py
    25017  2026-09-23 00:16   tests/test_sqlite_storage.py
     7928  2026-09-23 00:16   tests/test_state_json_persistence.py
   116323  2026-09-23 00:16   tests/test_storage_manager.py
     5928  2026-09-23 00:16   tests/test_time_utils.py
    29296  2026-09-23 00:16   tests/test_timeline_editor.py
    16852  2026-09-23 00:16   tests/test_video_promotion.py
---------                     -------
  3450585                     134 files
```

### (b) SHA-256 do ZIP gerado

```
$ sha256sum /tmp/zipcheck/entrega_prompt28.zip
d14b4bd4dddadd022c49aadcc255b16933040d72f93a8399c4f77a65e56823c4  /tmp/zipcheck/entrega_prompt28.zip
```

### (c) Declaração honesta de origem

Esta seção foi gerada e verificada **inteiramente no sandbox** (ambiente
de desenvolvimento cloud), **não** na máquina Windows real do usuário,
porque a ponte `device_bash`/`mcp__remote-devices__*` estava indisponível
no momento desta entrega. Os arquivos de código (`_sistema/timeline_editor.py`,
`_sistema/edit_project.py`, testes) são os mesmos que serão copiados para
o Windows; os 6 arquivos de lançador vieram do snapshot real já enviado
pelo usuário, não foram reescritos. O ZIP de verificação abaixo prova a
integridade do CONTEÚDO produzido nesta rodada; a cópia byte-a-byte para
`C:\Users\Enzo\Desktop\teste` e sua verificação via hash será feita assim
que a ponte com a máquina Windows for restabelecida, ou os arquivos serão
entregues diretamente pela conversa (download) caso o usuário prefira não
esperar.
