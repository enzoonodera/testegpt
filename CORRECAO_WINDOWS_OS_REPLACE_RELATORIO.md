# CORREÇÃO — race condition real em `os.replace` no Windows (`StorageManager.promote_to_final`)

Data: 2026-09-24
Tipo: correção pós-evidência real de produção (não é um "Prompt N" novo de roadmap — encontrada via validação manual no Windows real do dono do produto).

## 0. Contexto / evidência que originou esta correção

O dono do produto reportou, com evidência REAL (reproduzida no Windows 10 19045 dele, não suposição):

- `tests/test_captions_engine.py::test_duas_transcricoes_concorrentes_do_mesmo_video_e_config_nao_quebram` (teste já aprovado do Prompt 31) falha no Windows real com `PermissionError: [WinError 5] Acesso negado`, dentro de `storage_manager.py`, na chamada `os.replace` de `promote_to_final`.
- No Linux (este sandbox), o mesmo teste passa 100% em milhares de execuções.
- Um script de diagnóstico isolado, rodado pelo dono do produto, reproduziu a falha em 3 de 4 execuções no Windows real.

**Causa raiz confirmada por leitura direta do código**: a "linearização da promoção" feita via `self._lock`/`_claim_promotion` é um `threading.Lock` **por instância** de `StorageManager`. O cenário do teste (duas instâncias de `StorageManager` genuinamente concorrentes, publicando para o **mesmo** `final_path` determinístico) usa dois locks diferentes que não se protegem mutuamente — não é um bug de `_claim()`/`BEGIN IMMEDIATE`, é a linearização em si não cobrindo múltiplas instâncias. Além disso, a docstring do módulo afirmava que `os.replace` é atômico no Windows sob concorrência real para o mesmo destino — essa afirmação está provada falsa pela evidência acima.

## 1. Arquivos criados

- `CORRECAO_WINDOWS_OS_REPLACE_RELATORIO.md` (este relatório).

## 2. Arquivos modificados

- `_sistema/storage_manager.py`
  - Novo método `_replace_with_bounded_retry(self, temp_path, final_resolved)`, inserido imediatamente antes de `promote_to_final`.
  - Novas constantes de módulo `_FINAL_REPLACE_RETRY_ATTEMPTS = 3` e `_FINAL_REPLACE_RETRY_BACKOFF_SECONDS = 0.01`.
  - `promote_to_final`: no branch mesmo-volume, a chamada direta a `os.replace(...)` foi substituída por `self._replace_with_bounded_retry(temp_path, final_resolved)`.
  - `_cross_volume_publish`: o `os.replace` final (staging → `final_resolved`, sempre mesmo-volume por construção) também passou a usar `self._replace_with_bounded_retry(staging, final_resolved)` — descoberta adicional feita durante esta correção, não pedida explicitamente, mas exigida pela mesma causa raiz (ver seção 4).
  - Docstring do módulo, seção "DIFERENÇAS DE PLATAFORMA / WINDOWS (item 11 do gate)": corrigida para remover a alegação falsa de atomicidade incondicional de `os.replace` no Windows sob concorrência real, documentando a evidência real, a causa raiz e a correção.
- `tests/test_storage_manager.py`
  - Novo teste `test_promote_to_final_recupera_de_falha_transitoria_de_os_replace` — injeta via `monkeypatch` uma falha `PermissionError` determinística nas primeiras N-1 chamadas de `os.replace` e sucesso na última, provando que `promote_to_final` se recupera sem propagar exceção.
  - Novo teste `test_promote_to_final_falha_persistente_de_os_replace_propaga_apos_esgotar_retry` — injeta falha `PermissionError` SEMPRE, provando que o orçamento de retry é esgotado (exatamente `_FINAL_REPLACE_RETRY_ATTEMPTS` tentativas, nunca mais) e a falha propaga como `StoragePublishError` estruturada, nunca é engolida.
  - `test_adversarial3revisao_falha_os_replace_libera_claims` (pré-existente): atualizado porque a mudança de comportamento é intencional — antes esperava `OSError` cru propagando; agora espera `StoragePublishError` com `reason == "final_replace_retries_exhausted"`. Todas as demais asserções (claims liberados, `temp` intacto, `final` não criado, tentativa legítima seguinte funciona) foram preservadas.
- `tests/test_captions_engine.py`
  - `test_duas_transcricoes_concorrentes_do_mesmo_video_e_config_nao_quebram`: **nenhuma mudança de lógica**. Apenas docstring explicativa adicionada, documentando que este teste passa 100% em CI Linux porque `os.replace`/`os.rename` é incondicionalmente atômico em POSIX e por isso NÃO reproduz por si só o bug do Windows — a garantia real vem dos dois testes determinísticos novos em `tests/test_storage_manager.py`.
- `empacotar_release.py`
  - `ALLOWED_ROOT_FILES`: adicionado `"CORRECAO_WINDOWS_OS_REPLACE_RELATORIO.md"` (ordem alfabética, entre `CLAUDE.md` e `EMPACOTAR_RELEASE.bat`), necessário para este relatório poder ser incluído no ZIP de release.

Nenhum outro arquivo foi tocado nesta correção. Confirmado por timestamp (`stat`) que `_sistema/template_engine.py`, `_sistema/template_importer.py` e os testes dos Prompts 39/40 têm mtime anterior ao início desta correção e permanecem intocados.

## 3. Comportamento novo

- `promote_to_final`, no caminho mesmo-volume, agora tolera até `_FINAL_REPLACE_RETRY_ATTEMPTS` (3) tentativas de `os.replace`, com backoff curto entre tentativas (`0.01s`, `0.02s` — total máximo ~30ms), antes de considerar a falha definitiva.
- `_cross_volume_publish` recebe a mesma proteção no seu `os.replace` final (staging → destino), pela mesma causa raiz.
- Se todas as tentativas falharem (falha persistente, não transitória), o método propaga `StoragePublishError` estruturada (com `reason="final_replace_retries_exhausted"`, `operation="promote_to_final"`, `recoverable=True`) — nunca engole a falha, nunca reporta sucesso falso, nunca faz retry infinito. A claim de promoção é sempre liberada no `finally`, preservando o invariante pré-existente de nunca deixar um claim órfão bloqueando promoções legítimas futuras.
- O retry roda em **toda** plataforma (não condicionado a `sys.platform == "win32"`) — ver decisão arquitetural na seção 4.

## 4. Decisões arquiteturais

**Por que a correção fica em `promote_to_final` (via `_replace_with_bounded_retry`), nunca em `captions_engine.py` ou em qualquer chamador isolado**: `promote_to_final` é a única autoridade de publicação de artefatos finais determinísticos usada por todo módulo que escreve com `overwrite=True` sob concorrência possível (hoje pelo menos `captions_engine.py` e `template_engine.py::ingest_builtin_templates`). Corrigir num chamador isolado criaria uma segunda fonte de verdade sobre como publicar com segurança — exatamente o padrão que o item 7 do GATE ("autoridade única") proíbe — e deixaria todo outro chamador atual e futuro vulnerável à mesma race.

**Por que também corrigir `_cross_volume_publish`**: seu `os.replace` final é mesmo-volume por construção (staging e destino sempre no volume de destino) — está sujeito à mesma janela de corrida sob concorrência real (duas publicações cross-volume concorrentes visando o mesmo `final_path`). Não corrigi-lo deixaria uma segunda implementação de `os.replace` sem a mesma proteção dentro do mesmo módulo, para o mesmo tipo de falha — inconsistente e arriscado. Reaproveitei o mesmo método (`_replace_with_bounded_retry`), não uma cópia paralela.

**Por que um retry local, pequeno e auto-contido em vez de reutilizar `retry_policy.py`**: `retry_policy.py` é infraestrutura de retry em nível de `Job` inteiro (outro domínio — reagendamento de trabalho de longa duração, com políticas configuráveis, backoff exponencial, etc.). Importar isso dentro de uma operação de baixo nível de filesystem, de poucos milissegundos, seria acoplamento de domínios sem benefício real e overengineering (violaria o princípio explícito do CLAUDE.md contra abstração desnecessária). O próprio módulo já tinha um precedente idêntico em espírito e forma — `_delete_with_bounded_retry` — que segui como padrão: mesmo estilo (loop bounded, `time.sleep` entre tentativas, não após a última, `raise` explícito ao esgotar).

**Por que `StoragePublishError` em vez de deixar o `OSError` cru propagar**: todas as demais rejeições de `promote_to_final` (claim, ownership, overwrite, validação) já propagam como `StoragePublishError` estruturada, nunca como exceção crua de baixo nível. Manter essa consistência facilita ao chamador (ex.: `captions_engine.py`) tratar toda falha de publicação de forma uniforme, e respeita o item 10 do GATE (nunca persistir `str(exc)`/`repr(exc)`/traceback de exceção capturada — a mensagem estruturada nunca inclui o texto bruto da exceção original; usei `from None` para cortar o encadeamento de exceção exposto).

**Por que o retry roda incondicionalmente em toda plataforma, sem checagem `sys.platform == "win32"`**: em POSIX, `os.replace`/`os.rename` é incondicionalmente atômico mesmo sob concorrência real — a falha nunca ocorre lá, então o laço sempre termina na 1ª tentativa. O custo extra é um único bloco `try/except` que já seria necessário de qualquer forma — overhead prático zero. Manter um único caminho de código para as duas plataformas evita introduzir uma ramificação condicional por SO só para pular uma proteção que é inofensiva onde não é necessária — mesmo raciocínio já aplicado (sem checagem de plataforma) em `_delete_with_bounded_retry`, que já existia no módulo antes desta correção.

**Orçamento de retry**: 3 tentativas, backoff linear curto (`0.01s`, `0.02s`), cobrindo uma janela de corrida tipicamente muito curta (a evidência do dono do produto mostrou reprodução em 3 de 4 execuções sem qualquer retry — um orçamento de poucas tentativas e dezenas de milissegundos é suficiente para a janela real, sem se tornar uma operação de longa duração).

## 5. Migrations

Nenhuma. Esta é uma correção pura de lógica/retry em código Python — nenhuma mudança de schema SQLite, nenhuma migration nova, nenhuma migration existente foi tocada.

## 6. Testes automatizados executados

```
python3 -m compileall -q _sistema tests
pytest tests/test_storage_manager.py tests/test_captions_engine.py -q
→ 250 passed in 2.64s

pytest tests/ -q
→ 2556 passed, 1 skipped, 36 subtests passed in 149.77s (0:02:29)
```

Baseline anterior (Prompt 40): 2554 passed. Delta: +2 (os dois novos testes determinísticos), zero regressões — o único teste pré-existente afetado (`test_adversarial3revisao_falha_os_replace_libera_claims`) foi atualizado para refletir a mudança de comportamento intencional, não removido nem contornado.

## 7. Como testar manualmente

**Esta correção não pode ser validada de forma conclusiva neste sandbox Linux** — o bug só se manifesta no Windows real (POSIX nunca aciona o caminho de retry além da 1ª tentativa). A validação manual abaixo, no Windows real do dono do produto, é o **critério de aceite final** desta correção:

1. Rodar `RODAR_TESTES.bat` e confirmar 100% verde (2556 passed, 1 skipped, 36 subtests passed — mesmo número visto neste sandbox).
2. Repetir o script de diagnóstico já usado para confirmar este bug originalmente, de 20 a 30 vezes seguidas, e confirmar **ZERO reproduções** de `PermissionError`/`WinError 5`.

Somente após esses dois passos manuais no Windows real esta correção deve ser considerada validada e aceita — nenhuma execução de teste neste sandbox Linux substitui essa validação, porque o bug em si nunca ocorre aqui.

## 8. Riscos conhecidos

- O retry cobre uma janela de corrida curta e transitória; se a causa da `PermissionError` no Windows real for persistente (ex.: antivírus bloqueando o arquivo por um período mais longo, ou outro processo com handle aberto por mais tempo que o orçamento de retry), a falha ainda propagará como `StoragePublishError` — isso é o comportamento correto e intencional (nunca mascarar uma falha real), mas significa que o orçamento de 3 tentativas/~30ms pode não ser suficiente para TODO cenário adverso de Windows. Se a validação manual mostrar reproduções residuais, o orçamento pode precisar ser ajustado (mais tentativas ou backoff maior) — decisão a ser tomada com base em evidência real, não especulação.
- Esta correção não elimina a causa raiz mais profunda (lock por instância não protege múltiplas instâncias de `StorageManager`) — ela mitiga o sintoma físico observável (falha transitória de `os.replace`) de forma robusta, sem alterar a arquitetura maior de locking. Uma correção arquitetural mais profunda (ex.: lock cross-processo/cross-instância real, como um lock de arquivo do SO) seria uma mudança muito maior, fora do escopo desta correção pontual, e não foi pedida.

## 9. Dívida técnica criada

Nenhuma nova. O padrão de retry seguido é o mesmo já estabelecido por `_delete_with_bounded_retry`, mantendo consistência interna do módulo.

## 10. Pendências

- Validação manual no Windows real (seção 7) — pendente, a ser feita pelo dono do produto. Esta é a pendência crítica e o critério de aceite final desta correção.
- Nenhuma outra pendência técnica identificada nesta correção.

## Ataques adversariais adicionais executados (item 15 do GATE)

1. Confirmado que o retry nunca disfarça uma falha persistente como sucesso (teste `test_promote_to_final_falha_persistente_de_os_replace_propaga_apos_esgotar_retry`).
2. Confirmado que o número exato de tentativas nunca excede o orçamento configurado, em nenhum dos dois cenários (transitório e persistente).
3. Confirmado que o claim de promoção é sempre liberado mesmo após falha persistente esgotando o retry (via `finally` pré-existente, exercitado pelo teste atualizado `test_adversarial3revisao_falha_os_replace_libera_claims`), permitindo uma tentativa legítima subsequente funcionar normalmente.
4. Confirmado que `temp_path` nunca é tocado/movido em caso de falha persistente (arquivo de origem permanece intacto — nenhuma promoção parcial).
5. Verificado que `_cross_volume_publish` tem a mesma proteção aplicada e seu `finally` de limpeza do staging continua funcionando independente do resultado do retry.
6. Confirmado, por leitura, que nenhum outro ponto do módulo chama `os.replace` diretamente fora de `_replace_with_bounded_retry` (grep em `storage_manager.py`).
7. Suíte completa (2556 testes) rodada do zero após todas as mudanças, sem nenhuma regressão em módulos não relacionados (ex.: `template_engine.py`, `media_catalog.py`, `captions_style.py`, etc.).
