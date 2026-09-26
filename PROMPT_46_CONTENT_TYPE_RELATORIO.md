# PROMPT 46 — Tipos de conteúdo automáticos — Relatório de Entrega

## 0. Texto literal do Prompt

> Detectar automaticamente o tipo provável de conteúdo:
> - podcast/interview
> - tutorial
> - vlog
> - gameplay
> - talking-head
> - general
>
> A estratégia do Smart Clip pode mudar internamente.
> Usuário não precisa escolher obrigatoriamente.
> Permitir override avançado caso queira.

Terceiro Prompt da Fase 7. Classifica o VÍDEO INTEIRO (não segmento a
segmento) numa categoria dentro de um vocabulário fechado de seis
valores, a partir dos `SemanticSegment` já produzidos pelo Prompt 44.

## 1. Arquivos criados

- `_sistema/content_type_detector.py` (novo, ~700 linhas) — módulo
  completo: docstring de decisões (com a decisão de nomenclatura em
  destaque), vocabulário fechado, cinco funções de sub-score puras
  (`score_podcast_interview`, `score_tutorial`, `score_vlog`,
  `score_gameplay`, `score_talking_head`), `classify_segments()` (função
  pura de classificação do vídeo inteiro), `compute_cache_key()`, classe
  `ContentCategoryDetector` (Job handler `handle_detect_job` + método
  direto `register_override`).
- `tests/test_content_type_detector.py` (novo, ~470 linhas,
  **30 testes**).
- `PROMPT_46_CONTENT_TYPE_RELATORIO.md` (este arquivo).

## 2. Arquivos modificados

- `empacotar_release.py` — adicionado
  `"PROMPT_46_CONTENT_TYPE_RELATORIO.md"` a `ALLOWED_ROOT_FILES` (ordem
  alfabética preservada).

**Nenhum outro arquivo foi tocado.** Confirmado por hash
(`sha256sum`) antes/depois: `smart_clip_engine.py`,
`smart_clip_scoring.py`, `domain/checkpoints.py`, `template_engine.py`,
`captions_engine.py`, `auto_reframe.py` e `silence_removal.py`
permanecem byte-idênticos — exatamente como o Prompt exigiu ("nenhuma
mudança esperada... se alguma for genuinamente necessária, justificar
com evidência concreta antes de fazer" — nenhuma foi necessária).

## 3. Decisão de nomes — por que não colide com `template_engine.py`

Achado confirmado por leitura direta: `_sistema/template_engine.py` já
usa o identificador `content_type` e a constante `CONTENT_TYPES`
(`FIXED_TEXT`, `AI_HOOK`, `AI_TITLE`, `AI_SUMMARY`, `AI_QUESTION`,
`AI_CTA`, `CUSTOM_AI`) para o tipo de ZONA de conteúdo de um template
visual — um conceito completamente diferente. Confundir os dois seria
uma violação de autoridade única (GATE item 7).

Decisão adotada: o módulo novo **nunca usa o identificador `content_type`
em nada que expõe**:

| Onde | Nome escolhido | Por que |
|---|---|---|
| Classe | `ContentCategoryDetector` | "Category", nunca "Type" |
| Vocabulário fechado | `SMART_CLIP_CONTENT_CATEGORIES` | nunca `CONTENT_TYPES` |
| Campo persistido no Artifact | `category` | nunca `content_type` |
| `kind` do Artifact | `"content_category_classification"` | nunca contém `"content_type"` |
| Nome do arquivo | `content_type_detector.py` | nome de ARQUIVO nunca colide em Python (não é identificador importável); mantém o nome sugerido no Prompt e a convenção de nomenclatura do projeto |

Testado estruturalmente (seção 6): os dois vocabulários são conjuntos
disjuntos (`SMART_CLIP_CONTENT_CATEGORIES.isdisjoint(template_engine.CONTENT_TYPES)`),
e o código real (fora da docstring de módulo, que precisa poder CITAR
`template_engine.py` para explicar a decisão) nunca importa
`template_engine` nem define nenhum símbolo chamado `content_type`/
`CONTENT_TYPES`/`ContentTypeDetector`.

## 4. Vocabulário do roadmap → constantes Python

| Termo do roadmap | Constante |
|---|---|
| podcast/interview | `CATEGORY_PODCAST_INTERVIEW = "PODCAST_INTERVIEW"` |
| tutorial | `CATEGORY_TUTORIAL = "TUTORIAL"` |
| vlog | `CATEGORY_VLOG = "VLOG"` |
| gameplay | `CATEGORY_GAMEPLAY = "GAMEPLAY"` |
| talking-head | `CATEGORY_TALKING_HEAD = "TALKING_HEAD"` |
| general | `CATEGORY_GENERAL = "GENERAL"` |

Fechado do mesmo jeito que `domain/checkpoints.py` — estender exige um
Prompt novo, nunca decisão unilateral do código (`validate_content_category`
rejeita qualquer valor fora dessas seis constantes).

## 5. Léxicos/heurísticas — decisão e justificativa com texto real

- **PODCAST_INTERVIEW** (`score_podcast_interview`): reaproveita
  `smart_clip_scoring.DEFAULT_QUESTION_MARKERS`/`DEFAULT_ANSWER_CUE_LEXICON`
  via import direto (nunca duplicados). Combina densidade de pergunta +
  densidade de resposta + bônus de ALTERNÂNCIA real de padrão textual
  entre segmentos consecutivos (nunca assume diarização — sem "quem
  fala", só "o texto muda de pergunta pra não-pergunta"). Testado com
  texto real: `"Você já parou pra pensar sobre isso?"` / `"A resposta é
  que sim, com certeza."` / `"E por que você acha isso?"` / `"Bom, no
  final eu percebi que sim."` → score `0.70`.
- **TUTORIAL** (`score_tutorial`): léxico próprio
  `DEFAULT_TUTORIAL_LEXICON` — marcadores sequenciais/imperativos
  ("primeiro,", "depois,", "em seguida", "clique em", "abra o",
  "configure", "selecione"). Testado com `"Primeiro, abra o programa no
  seu computador."` / `"Depois, clique em novo projeto."` / `"Em
  seguida, configure as opções principais."` → score `1.0`.
- **VLOG** (`score_vlog`): léxico próprio `DEFAULT_VLOG_LEXICON` —
  narrativa em primeira pessoa/rotina ("hoje eu", "meu dia", "minha
  rotina", "cheguei em casa"). Testado com `"Hoje eu vou te mostrar como
  é meu dia."` / `"Cheguei em casa e minha rotina começou."` → score
  `1.0`.
- **GAMEPLAY** (`score_gameplay`): léxico próprio
  `DEFAULT_GAMEPLAY_LEXICON` ("gameplay", "esse jogo", "boss", "loot",
  "fase", "combo") tratado como **sinal fraco deliberado**, limitado por
  `GAMEPLAY_WEAK_SIGNAL_CAP_PADRAO = 0.5` quando nenhum backend
  visual/sonoro real está disponível — texto sobre jogos não é prova de
  que o vídeo É gameplay (alguém pode estar CONVERSANDO sobre um jogo
  num talking-head). Testado: `"Nesse jogo o boss é bem difícil."` /
  `"Consegui um loot incrível."` → score `0.5` (teto), nunca mais alto
  sem sinal real. Quando um backend real é injetado e reporta
  `available=True`, o teto desaparece e o sinal léxico se combina com o
  real (testado: com um sample `activity=0.9`, o score sobe pra `0.7`).
- **TALKING_HEAD** (`score_talking_head`): léxico próprio
  `DEFAULT_TALKING_HEAD_LEXICON` — endereçamento direto de opinião ("eu
  acho que", "na minha opinião", "eu acredito"). Testado com `"Eu acho
  que isso é importante."` / `"Na minha opinião, esse é o ponto
  principal."` → score `1.0`.
- **GENERAL**: nunca tem léxico próprio — seu score é
  `1.0 - max(scores das outras cinco)`. Testado com texto genérico sem
  nenhum sinal (`"Isso é só uma frase qualquer sem sinal nenhum."` /
  `"Outra frase comum, nada especial por aqui."`) → todas as cinco
  categorias dão `0.0`, `GENERAL` vence com `1.0`. Também testado o caso
  intermediário: 1 de 5 segmentos com uma única menção fraca de jogo →
  `GAMEPLAY=0.1`, `GENERAL=0.9` — `GENERAL` vence com folga, provando que
  o design nunca força uma categoria só pra evitar `GENERAL`.

## 6. Testes automatizados executados

`tests/test_content_type_detector.py` — **30 testes**, cobrindo:

1. Vocabulário de categorias fechado (exatamente as 6 do roadmap,
   `validate_content_category` rejeita qualquer valor fora, incluindo
   nomes de OUTRO vocabulário como `"FIXED_TEXT"`).
2. **Nenhuma colisão com `template_engine.CONTENT_TYPES`** — conjuntos
   disjuntos, o código real nunca importa `template_engine` nem define
   `content_type`/`CONTENT_TYPES`/`ContentTypeDetector`.
3. Cada heurística de categoria isolada, com texto real curado.
4. Caso honesto de `GENERAL` (sem sinal nenhum) e caso de sinal fraco
   isolado que não deve dominar.
5. **Sinal sem backend real nunca fabrica valor** (gameplay): teto
   respeitado sem backend, e teto removido quando um backend real
   injetado reporta `available=True` — incluindo teste do handler
   completo confirmando que o backend padrão (stub honesto) nunca
   fabrica sinal.
6. Nenhum checkpoint novo gravado/referenciado (runtime + estrutural).
7. **Override nunca apaga a detecção automática original** — testado
   com um override único e com múltiplos overrides em sequência: a auto
   original permanece idêntica e recuperável via
   `get_latest_auto_classification` em todos os casos; o valor EFETIVO
   (`get_latest_classification`) sempre reflete o mais recente; nenhum
   Artifact é apagado (1 auto + N overrides = N+1 linhas sempre).
8. Rejeição de categoria fora do vocabulário em `register_override`.
9. Cache reaproveitado quando nada muda (nunca duplica); recalculado
   quando um léxico muda (dois `cache_key` diferentes → dois Artifacts);
   `compute_cache_key` determinístico e sensível à segmentação de
   origem.
10. Encadeamento honesto até segmentação/transcrição quando nada existe
    ainda (mesmo padrão do Prompt 45); falha honesta quando a
    segmentação subjacente falha.
11. Falha honesta sem `video_id`; construção — validação de tipos/
    valores.
12. Contrato Job/Artifact — restart com instâncias TOTALMENTE novas.

Resultado: **30 passed** (isolado) e **suíte completa: 2830 passed,
1 skipped, 36 subtests passed** — baseline anterior era 2800 (Prompt
45), crescimento de exatamente 30, **zero regressões**.

```
2830 passed, 1 skipped, 36 subtests passed in 166.44s (0:02:46)
```

`python3 -m compileall -q _sistema tests` — sem erros.

## 7. Como testar manualmente

```python
from _sistema.content_type_detector import ContentCategoryDetector, OPERATION_DETECT_CONTENT_CATEGORY
# construir com manager/database/storage_manager/app_paths reais
# criar um Job(video_id=..., operation=OPERATION_DETECT_CONTENT_CATEGORY)
# chamar engine.handle_detect_job(job)
# engine.get_latest_classification(video_id) -- valor efetivo atual
# engine.get_latest_auto_classification(video_id) -- sempre a auto original
# engine.register_override(video_id, "VLOG", reason="...") -- nunca apaga a auto
```

## 8. Riscos conhecidos

- Todos os léxicos são heurísticas modestas de v1 em português coloquial
  — parâmetros do construtor, ajustáveis sem mudança de código.
- `score_gameplay` continua um sinal fraco por padrão (sem backend
  visual/sonoro real integrado) — comportamento conhecido e honesto, não
  um bug.
- A alternância de pergunta/resposta em `score_podcast_interview` é uma
  aproximação textual, não diarização real — dois locutores que nunca
  fazem perguntas explícitas não seriam capturados por este sinal
  isoladamente (mitigado pela combinação com densidade de resposta).

## 9. Dívida técnica criada

Nenhuma dívida nova deliberada. Segue os padrões já estabelecidos
(Job/Artifact/cache_key determinístico, backend injetável honesto,
JobEngine interno para encadeamento, vocabulário fechado validado).

## 10. Pendências

- Prompt 47 (expansão de bordas + deduplicação de candidatos) e Prompt
  48 (tela de seleção/override do usuário) constroem sobre esta base —
  nenhum foi antecipado. `register_override` já está pronto para ser
  chamado por uma UI futura sem nenhuma mudança estrutural.
- Ajustar pesos de `smart_clip_scoring.py` por tipo de conteúdo
  permanece uma possibilidade futura descrita no roadmap, não
  implementada aqui (fora de escopo).
- **Pendência não relacionada a este Prompt, ainda em aberto**: a
  correção Windows do teardown SQLite/WAL segue aguardando os 3
  resultados de `RODAR_TESTES.bat` na máquina Windows real.

## Ataques adversariais adicionais executados (GATE item 15)

- Confirmado que um override NUNCA sobrescreve nem apaga a linha
  automática original — testado com 1 e com 2 overrides consecutivos,
  sempre preservando o histórico completo (contagem exata de Artifacts).
- Confirmado que `GENERAL` vence honestamente tanto no caso de zero
  sinal quanto no caso de sinal fraco isolado insuficiente — nunca uma
  categoria é forçada artificialmente.
- Confirmado que o teste estrutural de "não colide com
  `template_engine`" distingue corretamente entre citar o módulo na
  docstring (legítimo, para explicar a decisão) e importá-lo/reusar seu
  identificador no código real (proibido).
- Confirmado que uma falha real na transcrição subjacente propaga
  corretamente como `JOB_FAILED` honesto em toda a cadeia (Classificação
  → Segmentação → Transcrição).
- Confirmado por hash que `smart_clip_engine.py`, `smart_clip_scoring.py`,
  `domain/checkpoints.py`, `template_engine.py`, `captions_engine.py`,
  `auto_reframe.py` e `silence_removal.py` permanecem byte-idênticos
  antes/depois desta entrega.
- Confirmado por teste que dois `ContentCategoryDetector` com léxicos
  diferentes sobre o MESMO vídeo produzem dois Artifacts distintos
  (`cache_key` diferentes), nunca um sobrescrevendo o outro
  silenciosamente.
