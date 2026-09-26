# CORREÇÃO — PermissionError (WinError 32) no teardown de `test_batch_engine.py` (Windows)

## 1. Arquivos criados

- `tests/windows_tempdir.py` — utilitário `robust_temporary_directory()` (substituto drop-in de `tempfile.TemporaryDirectory()`) e `rmtree_with_bounded_retry()` (retry curto e limitado em torno de `shutil.rmtree`, mesmo padrão de `_replace_with_bounded_retry`/`_delete_with_bounded_retry` em `_sistema/storage_manager.py`). Docstring completa documenta a causa raiz e a decisão de onde a correção vive.
- `tests/test_windows_tempdir.py` (7 testes) — testa a lógica de retry isoladamente via `monkeypatch` determinístico (recuperação de falha transitória, propagação de falha persistente, diretório já ausente não é erro, contrato de gerenciador de contexto igual ao de `tempfile.TemporaryDirectory`, remoção mesmo após exceção dentro do bloco).
- `CORRECAO_WINDOWS_SQLITE_WAL_TEARDOWN_RELATORIO.md` (este arquivo).

## 2. Arquivos modificados

- `tests/test_batch_engine.py` — fixture `local_db` passou a usar `robust_temporary_directory()` no lugar de `tempfile.TemporaryDirectory()`. Import de `tempfile` removido (não usado em nenhum outro lugar do arquivo).
- `tests/test_operational_audit.py` — mesma troca na fixture `local_db` (este arquivo cria uma segunda instância de `LocalDatabase` contra o mesmo `.db`, linha ~290 — mesmo padrão de risco).
- `tests/test_backup_restore.py` — todas as 27 ocorrências de `tempfile.TemporaryDirectory()` trocadas por `robust_temporary_directory()`. Decisão de converter o arquivo inteiro (não só um subconjunto), justificada na seção 4.

Nenhum módulo de produção (`_sistema/`) foi tocado — em particular, `_sistema/storage/database.py` (o WAL continua habilitado, sem nenhuma mudança) e `_sistema/batch_engine.py` (o Prompt correto confirmou explicitamente que não é uma regressão deste módulo).

## 3. Causa raiz — confirmada por evidência, não suposição

**Evidência de origem**: `RODAR_TESTES.bat` no Windows real do usuário, 2 execuções independentes, ambas terminando `2727 passed, 1 error, 36 subtests passed` com `PermissionError: [WinError 32]` no teardown de `test_duas_instancias_de_batchengine_nunca_executam_o_mesmo_job_duas_vezes` — o teste em si PASSA, o erro é só na limpeza pós-teste (`shutil.rmtree` dentro de `tempfile.TemporaryDirectory.__exit__`).

**Investigação de código (confirmada por leitura direta, não suposição)**:

1. `LocalDatabase` (`_sistema/storage/database.py`) nunca mantém uma `sqlite3.Connection` como atributo persistente — toda conexão é aberta via `self._connect()` e fechada via `finally: conn.close()` em cada método público. Não existe um "esqueceu de fechar" no código Python.
2. O banco roda em `PRAGMA journal_mode=WAL` (confirmado em `_ensure_wal_mode`) — exigência de produto (múltiplos leitores/um escritor simultâneos), **não desabilitada por esta correção**.
3. O teste que expôs o problema (`test_duas_instancias_de_batchengine_nunca_executam_o_mesmo_job_duas_vezes`) cria DE PROPÓSITO uma segunda instância de `LocalDatabase` (`db_b`) apontando para o MESMO arquivo que a fixture `local_db` já usa — esse é o próprio propósito do teste (provar que duas instâncias concorrentes nunca executam o mesmo Job duas vezes), não um uso incorreto.
4. Em modo WAL, cada conexão SQLite mantém um mapeamento de memória compartilhada (`-shm`) e um log (`-wal`) ao lado do `.db` principal. É um comportamento amplamente documentado do SQLite no Windows que, mesmo depois de `sqlite3_close()` retornar ao processo Python, o sistema operacional pode levar um instante a mais para liberar completamente o mapeamento de memória subjacente a esses dois arquivos auxiliares — uma janela transitória e curta, não um vazamento de handle real (nenhum objeto Python retém um handle após o `close()`, confirmado no item 1).
5. `tempfile.TemporaryDirectory.__exit__` chama `shutil.rmtree` **sem nenhum retry embutido** — qualquer coisa que ainda segure um handle no exato instante da chamada derruba a limpeza inteira com `PermissionError`.

**Nível de confirmação**: a causa raiz é fortemente sustentada pela combinação de (a) a evidência real e determinística do Windows do usuário, (b) a confirmação por leitura de código de que não há um bug óbvio de "conexão não fechada" no lado Python, e (c) o padrão ser um comportamento conhecido e documentado de SQLite+WAL no Windows. Não foi possível reproduzir a falha neste ambiente de desenvolvimento (Linux/sandbox), onde o kernel libera o mapeamento antes do `close()` retornar — por isso a verificação final desta correção depende das 3 execuções completas no Windows real (seção 6), que devem ser rodadas pelo usuário via `RODAR_TESTES.bat`, já que este ambiente não tem acesso a um shell Windows real (só a um dispositivo Linux em ponte, que não reproduziria o comportamento específico do Windows).

**Confirmado como problema de TESTE, não de produção**: nenhum caminho de código de `_sistema/` apaga o diretório que contém um `.db` ativo durante a operação normal do produto — esse padrão (apagar o diretório pai do banco logo após fechar conexões) só ocorre em teardown de teste. Por isso a correção vive inteiramente em `tests/`, nunca em código de produção.

## 4. Decisão de escopo — por que `test_backup_restore.py` foi convertido por inteiro

O Prompt de correção listou `test_operational_audit.py` e `test_backup_restore.py` como "candidatos à mesma classe de problema" e pediu para NÃO reescrever os ~97 usos de `tempfile.TemporaryDirectory` que não têm o padrão de risco (duas conexões/instâncias contra o mesmo arquivo dentro do mesmo diretório temporário).

Inspecionado `test_backup_restore.py` (27 ocorrências, nenhuma fixture compartilhada — cada teste abre seu próprio bloco `with tempfile.TemporaryDirectory()`): **todo** teste do arquivo, por natureza (é o arquivo de testes de backup/restore), abre mais de uma conexão/instância contra os mesmos arquivos dentro do mesmo diretório — `build_local_stack` cria uma `LocalDatabase`, `BackupManager.create_backup()` abre uma segunda conexão SQLite crua para o snapshot, `assert_integrity_ok`/`file_sha256` leem o arquivo de novo, e vários testes constroem explicitamente uma segunda/terceira `LocalDatabase` para simular restart. Ou seja: as 27 ocorrências deste arquivo TÊM o padrão de risco — não são usos "não relacionados" que o Prompt pediu para preservar. Por isso a troca cobriu o arquivo inteiro, sem violar a instrução de não tocar nos ~97 usos genuinamente não relacionados dos outros ~22 arquivos de teste.

`test_batch_engine.py` e `test_operational_audit.py` têm só 1 ocorrência cada (a fixture `local_db`, compartilhada por todos os testes do arquivo) — convertida em ambos.

Total convertido: 29 ocorrências (1 + 1 + 27) de um total de ~100 no projeto — as ~71 restantes, em arquivos que não abrem uma segunda conexão contra o mesmo `.db` dentro do mesmo diretório, permanecem intocadas.

## 5. Comportamento novo

`robust_temporary_directory()` tem exatamente a mesma interface de `tempfile.TemporaryDirectory()` como gerenciador de contexto (`yield` de uma `str`) — substituição transparente nos 3 arquivos afetados. A única diferença é que, na saída do bloco `with`, a limpeza usa `rmtree_with_bounded_retry` (até 5 tentativas, backoff curto de `0.05s * tentativa`) em vez de `shutil.rmtree` cru. Roda em toda plataforma (sem checagem de `sys.platform`), pelo mesmo raciocínio já documentado em `_sistema/storage_manager.py`: em Linux, a falha nunca ocorre, então o laço sempre termina na 1ª tentativa — overhead prático zero.

Esgotado o orçamento de tentativas, a falha original é sempre propagada — nunca escondida. Um `PermissionError` persistente real (não a janela transitória) continua sendo reportado como falha de teste, exatamente como antes.

## 6. Migrations

Nenhuma. Este é um utilitário de teste — nenhuma tabela, coluna ou schema tocado.

## 7. Testes automatizados executados

```
pytest tests/test_windows_tempdir.py -q
→ 7 passed (lógica de retry testada isoladamente via monkeypatch
  determinístico -- recuperação de falha transitória, propagação de falha
  persistente, diretório já ausente, contrato idêntico ao de
  tempfile.TemporaryDirectory, remoção mesmo após exceção no bloco)

pytest tests/test_batch_engine.py tests/test_operational_audit.py tests/test_backup_restore.py -q
→ 105 passed (neste ambiente Linux -- a falha original é Windows-específica
  e não reproduz aqui, como esperado; zero regressões nestes 3 arquivos)

python3 -m compileall -q tests
→ sem erros

pytest tests/ -q
→ 2733 passed, 1 skipped, 36 subtests passed em 159.47s
  (baseline anterior, Prompt 43: 2725 passed -- 7 novos testes deste
  utilitário, 1 skip pré-existente e não relacionado, ZERO regressões)
```

**PENDENTE — evidência real do Windows (exigida pelo escopo desta correção)**: este ambiente de desenvolvimento não tem acesso a um shell Windows real (a ponte de dispositivo remoto roda uma VM Linux isolada, que não reproduziria o comportamento específico do Windows que originou o problema). A confirmação final de que o `PermissionError: [WinError 32]` deixou de ocorrer depende de rodar `RODAR_TESTES.bat` (ou `pytest tests/ -q`) no Windows real do usuário **pelo menos 3 vezes consecutivas** e comparar contra a contagem esperada (2733 passed, 1 skipped, 36 subtests, zero error/failure). Os arquivos já foram entregues ao Windows (seção 9) — falta essa confirmação para fechar o ciclo de evidência real end-to-end, do mesmo jeito que a evidência original (`2727 passed, 1 error`) também veio de execução real, não de suposição.

## 8. Como testar manualmente

1. No Windows, rodar `RODAR_TESTES.bat` (ou `pytest tests/ -q` diretamente) a partir de `C:\Users\Enzo\Desktop\teste`.
2. Confirmar que a contagem final é `2733 passed, 1 skipped, 36 subtests passed` (ou o total que a árvore de testes tiver naquele momento) e **zero** `error`/`failure` — em particular, que `test_duas_instancias_de_batchengine_nunca_executam_o_mesmo_job_duas_vezes` não aparece mais na lista de erros de teardown.
3. Repetir os passos 1-2 mais duas vezes (3 execuções completas no total) para confirmar que a correção é determinística, não uma melhora de sorte estatística.

## 9. Riscos conhecidos

- O retry cobre uma janela **transitória e curta**. Se o ambiente Windows do usuário tiver antivírus/indexação de disco especialmente agressivos segurando o handle por mais tempo que o orçamento de 5 tentativas/backoff crescente (~0.75s de espera total no pior caso), a falha ainda pode ocorrer — nesse caso, o `PermissionError` original propaga normalmente (nunca escondido), e o orçamento (`_RMTREE_RETRY_ATTEMPTS`/`_RMTREE_RETRY_BACKOFF_SECONDS` em `tests/windows_tempdir.py`) pode ser ajustado com evidência adicional das 3 execuções pendentes (seção 7).
- Esta correção não elimina a causa raiz de nível SO (o comportamento do Windows em torno de WAL/mmap) — ela absorve a janela no ÚNICO lugar onde essa janela pode se manifestar como uma falha visível (teardown de teste que apaga o diretório do banco), sem desabilitar WAL (proibido pelo escopo) e sem qualquer mudança em código de produção.

## 10. Dívida técnica criada

Nenhuma nova. Os ~71 usos restantes de `tempfile.TemporaryDirectory()` nos outros ~22 arquivos de teste não têm o padrão de risco (uma única conexão/instância por diretório temporário) e permanecem como estavam, por instrução explícita do escopo desta correção.

## Pendências

- Rodar `RODAR_TESTES.bat` 3 vezes no Windows real e reportar os 3 resultados (seção 7) — não pôde ser feito a partir deste ambiente de desenvolvimento, que não tem acesso a um shell Windows real.
