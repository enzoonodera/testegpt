# -*- coding: utf-8 -*-
"""``robust_temporary_directory`` -- substituto de ``tempfile.TemporaryDirectory``
usado SOMENTE pelos testes que abrem mais de uma ligação (``LocalDatabase``/
``sqlite3.connect`` cru) contra o MESMO arquivo `.db` dentro do mesmo
diretório temporário -- o padrão que expôs, no Windows real, um
``PermissionError`` (``WinError 32``) no teardown do próprio
``tempfile.TemporaryDirectory`` (``shutil.rmtree`` sem retry).

===========================================================================
CAUSA RAIZ -- EVIDÊNCIA E DECISÃO (ver CORRECAO_WINDOWS_SQLITE_WAL_TEARDOWN_
RELATORIO.md para o relato completo)
===========================================================================

``LocalDatabase`` (``_sistema/storage/database.py``) nunca mantém uma
``sqlite3.Connection`` como atributo persistente -- toda conexão é aberta e
fechada via ``finally: conn.close()`` (confirmado por leitura direta do
código antes desta correção). O banco roda em ``PRAGMA journal_mode=WAL``,
o que é uma exigência de produto (multi-processo/multi-thread) e NUNCA foi
desabilitado por esta correção.

Em WAL, cada conexão SQLite mantém um mapeamento de memória compartilhada
(``-shm``) e um arquivo de log (``-wal``) ao lado do `.db` principal. No
Windows, ``sqlite3_close()`` já devolveu o controle ao processo Python antes
que o SO termine de liberar o mapeamento de memória subjacente desses dois
arquivos auxiliares -- uma janela transitória e CURTA (não um vazamento de
handle real: nenhum objeto Python permanece com um handle aberto depois do
``close()``). Quando duas instâncias de ``LocalDatabase``/duas conexões
`sqlite3` distintas se revezam sobre o MESMO arquivo dentro do mesmo teste
(padrão intencional de teste de concorrência/restart -- não um uso
incorreto), essa janela fica mais provável de ainda estar aberta no instante
exato em que ``tempfile.TemporaryDirectory.__exit__`` chama
``shutil.rmtree`` (sem nenhum retry) logo em seguida.

Confirmado como problema de TESTE, não de produção: nenhum código de
``_sistema/`` apaga o diretório que contém um `.db` ativo durante a operação
normal do produto -- esse padrão nunca ocorre fora de teardown de teste.
Por isso a correção vive aqui, em ``tests/``, nunca em
``_sistema/storage/database.py`` nem em nenhum outro módulo de produção.

===========================================================================
DESENHO -- MESMO PADRÃO JÁ APROVADO DE ``_replace_with_bounded_retry``/
``_delete_with_bounded_retry`` (``_sistema/storage_manager.py``)
===========================================================================

Retry curto e limitado (poucas tentativas, backoff total de poucas dezenas
de milissegundos) em torno de ``shutil.rmtree`` -- NUNCA um framework de
retry genérico. Corre em toda plataforma, sem checagem de
``sys.platform == "win32"``, pelo mesmo raciocínio já documentado em
``_sistema/storage_manager.py``: em Linux/mac a falha nunca ocorre (o
kernel libera o mapeamento antes do ``close()`` retornar), então o laço
sempre termina na 1ª tentativa -- overhead prático zero, um único caminho de
código para as duas plataformas.

Esgotado o orçamento de tentativas, a falha NUNCA é engolida -- propaga o
``OSError``/``PermissionError`` original, indistinguível de uma falha
persistente real de permissão (que tem que continuar sendo reportada como
erro de teste).
"""
from __future__ import annotations

import shutil
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

_RMTREE_RETRY_ATTEMPTS = 5
_RMTREE_RETRY_BACKOFF_SECONDS = 0.05


def rmtree_with_bounded_retry(path: "Path | str", *, attempts: int = _RMTREE_RETRY_ATTEMPTS) -> None:
    """``shutil.rmtree`` com retry curto e limitado -- ver docstring do
    módulo. Usado tanto pelo gerenciador de contexto abaixo quanto
    diretamente por quem já tem um diretório temporário próprio e só
    precisa apagá-lo com a mesma tolerância."""
    last_exc: OSError | None = None
    for attempt in range(attempts):
        try:
            shutil.rmtree(str(path))
            return
        except FileNotFoundError:
            return  # já não existe -- nada a fazer, não é uma falha
        except OSError as exc:
            last_exc = exc
            if attempt + 1 < attempts:
                time.sleep(_RMTREE_RETRY_BACKOFF_SECONDS * (attempt + 1))
    assert last_exc is not None
    raise last_exc


@contextmanager
def robust_temporary_directory() -> Iterator[str]:
    """Substituto de ``tempfile.TemporaryDirectory`` como gerenciador de
    contexto -- mesma interface (``yield`` do caminho como ``str``), única
    diferença é a limpeza no ``__exit__`` usar
    ``rmtree_with_bounded_retry`` em vez do ``shutil.rmtree`` sem retry
    embutido em ``TemporaryDirectory.__exit__``.

    Usar SOMENTE nos testes que abrem mais de uma ligação/instância de
    ``LocalDatabase``/``sqlite3.connect`` contra o mesmo `.db` dentro do
    mesmo diretório -- nunca uma substituição indiscriminada de todo uso de
    ``tempfile.TemporaryDirectory`` no restante da suíte (ver docstring do
    módulo)."""
    name = tempfile.mkdtemp()
    try:
        yield name
    finally:
        rmtree_with_bounded_retry(name)
