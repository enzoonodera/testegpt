# -*- coding: utf-8 -*-
"""Testes de ``tests/windows_tempdir.py`` -- mesmo padrão determinístico via
``monkeypatch`` já usado por ``test_storage_manager.py`` para
``_replace_with_bounded_retry``/``_delete_with_bounded_retry``: nunca depende
de reproduzir de verdade a janela de corrida do Windows (impossível em
CI/Linux) -- prova a LÓGICA de retry injetando falhas transitórias e
persistentes determinísticas."""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from tests.windows_tempdir import (
    _RMTREE_RETRY_ATTEMPTS,
    rmtree_with_bounded_retry,
    robust_temporary_directory,
)


def test_rmtree_com_bounded_retry_recupera_de_falha_transitoria(monkeypatch, tmp_path):
    """``shutil.rmtree`` levanta ``PermissionError`` nas primeiras N-1
    chamadas e só sucede na última tentativa do orçamento -- prova que a
    função se recupera sem propagar, mesmo padrão do teste de
    ``_replace_with_bounded_retry`` (Prompt de correção Windows anterior)."""
    target = tmp_path / "alvo"
    target.mkdir()
    (target / "arquivo.txt").write_bytes(b"x")

    real_rmtree = shutil.rmtree
    calls: list[int] = []

    def flaky(path, *args, **kwargs):
        calls.append(1)
        if len(calls) < _RMTREE_RETRY_ATTEMPTS:
            raise PermissionError("[WinError 32] Acesso negado (simulado)")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr("tests.windows_tempdir.shutil.rmtree", flaky)

    rmtree_with_bounded_retry(target)

    assert not target.exists()
    assert len(calls) == _RMTREE_RETRY_ATTEMPTS  # sucedeu exatamente na última tentativa


def test_rmtree_com_bounded_retry_propaga_falha_persistente_apos_esgotar_orcamento(monkeypatch, tmp_path):
    """Simétrico: se ``shutil.rmtree`` falhar SEMPRE (falha persistente,
    não transitória), a função precisa esgotar o orçamento (número exato e
    limitado, nunca retry infinito) e PROPAGAR o ``PermissionError``
    original -- nunca reportar sucesso, nunca engolir silenciosamente uma
    falha de permissão real."""
    target = tmp_path / "alvo"
    target.mkdir()

    calls: list[int] = []

    def always_boom(path, *args, **kwargs):
        calls.append(1)
        raise PermissionError("[WinError 32] Acesso negado (simulado, persistente)")

    monkeypatch.setattr("tests.windows_tempdir.shutil.rmtree", always_boom)

    with pytest.raises(PermissionError):
        rmtree_with_bounded_retry(target)

    assert len(calls) == _RMTREE_RETRY_ATTEMPTS  # nunca mais tentativas que o orçamento


def test_rmtree_com_bounded_retry_diretorio_ja_ausente_nao_e_falha(tmp_path):
    """Apagar um diretório que já não existe (removido por outro caminho
    entre a decisão e a chamada) não é um erro -- mesma filosofia de
    idempotência do restante do projeto."""
    ausente = tmp_path / "nunca-existiu"
    rmtree_with_bounded_retry(ausente)  # não deve levantar


def test_robust_temporary_directory_cria_e_remove_o_diretorio():
    with robust_temporary_directory() as tmp:
        path = Path(tmp)
        assert path.is_dir()
        (path / "arquivo.txt").write_bytes(b"conteudo")
    assert not path.exists()


def test_robust_temporary_directory_remove_mesmo_apos_excecao_no_bloco():
    captured_path: "Path | None" = None
    with pytest.raises(ValueError):
        with robust_temporary_directory() as tmp:
            captured_path = Path(tmp)
            raise ValueError("erro dentro do bloco")
    assert captured_path is not None
    assert not captured_path.exists()


def test_robust_temporary_directory_usa_retry_no_teardown(monkeypatch):
    """Prova que o gerenciador de contexto realmente delega ao retry
    limitado (não a um ``shutil.rmtree`` cru) -- injeta uma falha
    transitória e confirma que o diretório ainda assim é removido."""
    real_rmtree = shutil.rmtree
    calls: list[int] = []

    def flaky(path, *args, **kwargs):
        calls.append(1)
        if len(calls) < _RMTREE_RETRY_ATTEMPTS:
            raise PermissionError("[WinError 32] Acesso negado (simulado)")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr("tests.windows_tempdir.shutil.rmtree", flaky)

    with robust_temporary_directory() as tmp:
        path = Path(tmp)
        assert path.is_dir()

    assert not path.exists()
    assert len(calls) == _RMTREE_RETRY_ATTEMPTS


def test_robust_temporary_directory_mesma_interface_de_tempfile_temporarydirectory():
    """``yield`` de uma ``str`` (não ``Path``) -- mesmo contrato de
    ``tempfile.TemporaryDirectory``, para que a substituição nos testes
    afetados seja um drop-in (``Path(tmp)`` continua funcionando)."""
    with robust_temporary_directory() as tmp:
        assert isinstance(tmp, str)
        assert Path(tmp).is_dir()
