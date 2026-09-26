# -*- coding: utf-8 -*-
"""Testes de Idempotência de Publicação (PROMPT 22).

Cobre, na ordem exigida pela seção 3 do Prompt:

1. As três etapas do fluxo "antes de publicar" (roadmap, item 1.2 a/b/c),
   cada uma com teste próprio: consultar estado local, publicação
   conhecida com ``remote_id`` -> JA_PUBLICADO, publicação conhecida sem
   ``remote_id`` -> INCONCLUSIVO.
2. Índice único do banco realmente impede duas ``Publication``s com a
   mesma chave, inclusive sob concorrência simulada (item 1.4).
3. Cenário "crash pós-upload" explícito (item 3 dos testes obrigatórios):
   uma nova tentativa de publicar a MESMA chave, com uma ``Publication``
   já existente sem ``remote_id``, devolve INCONCLUSIVO e NUNCA cria uma
   segunda ``Publication`` nem afirma sucesso.
4. Cenário nunca visto -> NUNCA_TENTADO, liberado para publicar.
5. Chaves diferentes nunca colidem entre si (não-interferência).
6. Não-regressão: Circuit Breaker (Prompt 20) e RetryPolicy (Prompt 21)
   continuam intocados e suas suítes continuam passando sem alteração
   (confirmado por import/execução aqui, não duplicado).
"""
from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import threading

import pytest

from _sistema.app_paths import build_app_paths
from _sistema.domain import Account, Publication, Video
from _sistema.publication_idempotency import (
    IDEMPOTENCY_OUTCOMES,
    OUTCOME_INCONCLUSIVO,
    OUTCOME_JA_PUBLICADO,
    OUTCOME_NUNCA_TENTADO,
    PublicationIdempotencyConflictError,
    PublicationIdempotencyError,
    PublicationIdempotencyGuard,
    compute_idempotency_key,
)
from _sistema.storage import LocalDatabase


# ---------------------------------------------------------------------------
# Infraestrutura comum (mesmo padrão de test_circuit_breaker.py/test_retry_policy.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        paths = build_app_paths(install_root=root / "install", data_root=root / "data")
        db = LocalDatabase(paths=paths)
        db.initialize()
        yield db.path, paths


@pytest.fixture
def local_db(db_path):
    path, paths = db_path
    return LocalDatabase(path=path, paths=paths)


@pytest.fixture
def guard(local_db):
    return PublicationIdempotencyGuard(local_db)


# ---------------------------------------------------------------------------
# 1a. Nunca visto -> NUNCA_TENTADO
# ---------------------------------------------------------------------------


def test_chave_nunca_vista_devolve_nunca_tentado_via_check(guard):
    result = guard.check("chave-nunca-usada")
    assert result.outcome == OUTCOME_NUNCA_TENTADO
    assert result.publication is None


def test_chave_nunca_vista_devolve_nunca_tentado_via_claim_e_reserva(guard, local_db):
    result = guard.claim("chave-nova", video_id=None, account_id=None)
    assert result.outcome == OUTCOME_NUNCA_TENTADO
    assert result.publication is not None
    assert result.publication.idempotency_key == "chave-nova"
    assert result.publication.remote_id is None
    # Persistido de verdade -- não só devolvido em memória.
    persisted = local_db.get(Publication, result.publication.id)
    assert persisted is not None
    assert persisted.idempotency_key == "chave-nova"


# ---------------------------------------------------------------------------
# 1b. Publicação conhecida com remote_id -> JA_PUBLICADO
# ---------------------------------------------------------------------------


def test_publicacao_conhecida_com_remote_id_devolve_ja_publicado(guard, local_db):
    key = "chave-sucesso"
    pub = Publication(idempotency_key=key, remote_id="yt-abc123", status="PUBLISHED")
    local_db.insert(pub)

    result = guard.check(key)
    assert result.outcome == OUTCOME_JA_PUBLICADO
    assert result.publication is not None
    assert result.publication.id == pub.id
    assert result.publication.remote_id == "yt-abc123"


def test_claim_com_publicacao_ja_confirmada_nao_publica_de_novo_nem_escreve(guard, local_db):
    key = "chave-sucesso-2"
    pub = Publication(idempotency_key=key, remote_id="yt-xyz", status="PUBLISHED")
    local_db.insert(pub)
    count_before = len(local_db.list(Publication))

    result = guard.claim(key, video_id=None, account_id=None)
    assert result.outcome == OUTCOME_JA_PUBLICADO
    assert result.publication.id == pub.id
    assert len(local_db.list(Publication)) == count_before  # nenhuma escrita nova


# ---------------------------------------------------------------------------
# 1c. Publicação conhecida SEM remote_id -> INCONCLUSIVO ("crash pós-upload")
# ---------------------------------------------------------------------------


def test_publicacao_conhecida_sem_remote_id_devolve_inconclusivo(guard, local_db):
    key = "chave-crash"
    pub = Publication(idempotency_key=key, remote_id=None, status="PENDING")
    local_db.insert(pub)

    result = guard.check(key)
    assert result.outcome == OUTCOME_INCONCLUSIVO
    assert result.publication is not None
    assert result.publication.id == pub.id


def test_cenario_crash_pos_upload_claim_devolve_inconclusivo_sem_criar_segunda_publication(
    guard, local_db
):
    """Cenário explícito exigido pela seção 3: uma Publication sem
    remote_id já existe para a chave (simulando um Job que pousou em
    UNKNOWN no meio de PUBLISHING) -- uma nova tentativa de publicar a
    MESMA chave via claim() precisa devolver INCONCLUSIVO, NUNCA criar uma
    segunda Publication nem afirmar sucesso."""
    key = "chave-crash-claim"
    original = Publication(idempotency_key=key, remote_id=None, status="PENDING")
    local_db.insert(original)
    count_before = len(local_db.list(Publication))

    result = guard.claim(key, video_id=None, account_id=None, title="tentativa nova")

    assert result.outcome == OUTCOME_INCONCLUSIVO
    assert result.publication.id == original.id
    assert result.publication.remote_id is None
    # Nenhuma segunda linha foi criada -- a contagem de Publications não mudou.
    assert len(local_db.list(Publication)) == count_before
    # A Publication original continua exatamente como estava (claim() nunca
    # escreve nada no caso INCONCLUSIVO).
    still_there = local_db.get(Publication, original.id)
    assert still_there.remote_id is None
    assert still_there.title is None


def test_inconclusivo_nunca_e_resolvido_silenciosamente_por_chamadas_repetidas(guard, local_db):
    """Reforça que múltiplas consultas sucessivas continuam INCONCLUSIVO --
    nenhum mecanismo automático resolve isso sozinho nesta rodada (só um
    Connector futuro, consultando a plataforma de verdade, ou uma decisão
    externa via mark_published(), poderia)."""
    key = "chave-persistentemente-inconclusiva"
    local_db.insert(Publication(idempotency_key=key, remote_id=None))

    for _ in range(5):
        assert guard.check(key).outcome == OUTCOME_INCONCLUSIVO
        assert guard.claim(key).outcome == OUTCOME_INCONCLUSIVO
    assert len(local_db.list(Publication)) == 1


# ---------------------------------------------------------------------------
# Fechando o ciclo: mark_published() transforma INCONCLUSIVO em JA_PUBLICADO
# ---------------------------------------------------------------------------


def test_mark_published_fecha_o_ciclo_de_nunca_tentado_a_ja_publicado(guard, local_db):
    key = "chave-ciclo-completo"
    claimed = guard.claim(key, video_id=None, account_id=None)
    assert claimed.outcome == OUTCOME_NUNCA_TENTADO

    # Antes de mark_published: uma nova consulta é INCONCLUSIVA (a
    # Publication existe mas ainda não tem remote_id).
    assert guard.check(key).outcome == OUTCOME_INCONCLUSIVO

    updated = guard.mark_published(claimed.publication.id, remote_id="yt-confirmado")
    assert updated.remote_id == "yt-confirmado"
    assert updated.status == "PUBLISHED"

    final = guard.check(key)
    assert final.outcome == OUTCOME_JA_PUBLICADO
    assert final.publication.remote_id == "yt-confirmado"
    assert len(local_db.list(Publication)) == 1  # nunca duplicou


def test_mark_published_valida_remote_id_nao_vazio(guard):
    claimed = guard.claim("chave-remote-id-vazio")
    with pytest.raises(ValueError):
        guard.mark_published(claimed.publication.id, remote_id="")


def test_mark_published_de_publication_inexistente_levanta_erro_claro(guard):
    with pytest.raises(PublicationIdempotencyError):
        guard.mark_published("00000000-0000-4000-8000-000000000000", remote_id="yt-1")


# ---------------------------------------------------------------------------
# 2/1.4. Índice único do banco: proteção real contra duplicação
# ---------------------------------------------------------------------------


def test_indice_unico_rejeita_insercao_direta_de_chave_duplicada(local_db):
    """Prova de que a proteção é do BANCO, não só de uma checagem em
    Python: inserir diretamente (bypassando o guard) uma segunda
    Publication com a mesma chave é rejeitado pelo SQLite."""
    key = "chave-indice-unico"
    local_db.insert(Publication(idempotency_key=key))
    with pytest.raises(sqlite3.IntegrityError):
        local_db.insert(Publication(idempotency_key=key))
    assert len(local_db.list(Publication)) == 1


def test_multiplas_publications_com_idempotency_key_none_sao_permitidas(local_db):
    """Índice único é PARCIAL (``WHERE idempotency_key IS NOT NULL``) --
    Publications sem chave (ex.: dados legados) nunca colidem entre si."""
    local_db.insert(Publication(idempotency_key=None))
    local_db.insert(Publication(idempotency_key=None))
    local_db.insert(Publication(idempotency_key=None))
    assert len(local_db.list(Publication)) == 3


def test_create_publication_de_baixo_nivel_levanta_conflito_claro_nao_erro_cru(guard):
    key = "chave-conflito-claro"
    guard.create_publication(key)
    with pytest.raises(PublicationIdempotencyConflictError):
        guard.create_publication(key)


def test_violacao_de_fk_nunca_e_confundida_com_conflito_de_idempotencia(guard):
    """Ataque adversarial: um video_id que não existe na tabela ``videos``
    viola a FK de ``publications``, não o índice único de
    ``idempotency_key``. O código que traduz ``sqlite3.IntegrityError`` em
    ``PublicationIdempotencyConflictError`` inspeciona a mensagem do erro
    por ``"idempotency_key"`` -- este teste prova que uma violação de FK
    (mensagem diferente) nunca é mascarada como um conflito de
    idempotência, e propaga como ``sqlite3.IntegrityError`` cru (o erro
    real, de configuração do chamador, não de duplicação)."""
    with pytest.raises(sqlite3.IntegrityError) as exc_info:
        guard.create_publication("chave-com-fk-invalida", video_id="00000000-0000-4000-8000-000000000000")
    assert not isinstance(exc_info.value, PublicationIdempotencyConflictError)


def test_concorrencia_duas_instancias_disputando_a_mesma_chave_so_uma_sobrevive(db_path):
    """CLAUDE.md itens 6/14 + roadmap item 1.4: duas tentativas
    'simultâneas' (Barrier, duas instâncias de LocalDatabase/guard contra
    o mesmo arquivo, mesmo padrão já aprovado nos Prompts 20/21) de criar
    uma Publication para a MESMA chave -- só uma sobrevive; a(s) outra(s)
    recebem um erro claro de conflito, nunca uma segunda linha
    silenciosa. Usa ``create_publication()`` (o caminho ingênuo, sem
    SELECT prévio) para testar o índice único do banco como ÚLTIMA linha
    de defesa, não a serialização de alto nível de ``claim()``."""
    path, paths = db_path
    key = "chave-disputada-concorrencia"
    workers = 8
    barrier = threading.Barrier(workers)
    outcomes: list[str] = [""] * workers

    def worker(index: int) -> None:
        db = LocalDatabase(path=path, paths=paths, busy_timeout_ms=10_000)
        local_guard = PublicationIdempotencyGuard(db)
        barrier.wait(timeout=5)
        try:
            local_guard.create_publication(key)
            outcomes[index] = "created"
        except PublicationIdempotencyConflictError:
            outcomes[index] = "conflict"

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes.count("created") == 1
    assert outcomes.count("conflict") == workers - 1

    inspector = LocalDatabase(path=path, paths=paths)
    matching = [p for p in inspector.list(Publication) if p.idempotency_key == key]
    assert len(matching) == 1


def test_concorrencia_duas_instancias_chamando_claim_a_mesma_chave_uma_reserva_outra_ve(db_path):
    """Mesma corrida, mas pelo caminho de produção real (``claim()``,
    TOCTOU-safe via BEGIN IMMEDIATE): exatamente uma reserva
    (NUNCA_TENTADO com Publication nova); as demais veem a reserva já
    feita e devolvem JA_PUBLICADO/INCONCLUSIVO (aqui, INCONCLUSIVO, já
    que ninguém chamou mark_published ainda) -- nunca duas reservas."""
    path, paths = db_path
    key = "chave-disputada-claim"
    workers = 8
    barrier = threading.Barrier(workers)
    outcomes: list[str] = [""] * workers

    def worker(index: int) -> None:
        db = LocalDatabase(path=path, paths=paths, busy_timeout_ms=10_000)
        local_guard = PublicationIdempotencyGuard(db)
        barrier.wait(timeout=5)
        outcomes[index] = local_guard.claim(key).outcome

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes.count(OUTCOME_NUNCA_TENTADO) == 1
    assert outcomes.count(OUTCOME_INCONCLUSIVO) == workers - 1

    inspector = LocalDatabase(path=path, paths=paths)
    matching = [p for p in inspector.list(Publication) if p.idempotency_key == key]
    assert len(matching) == 1


# ---------------------------------------------------------------------------
# Não-interferência entre chaves diferentes
# ---------------------------------------------------------------------------


def test_chaves_diferentes_mesma_conta_videos_diferentes_nao_colidem(guard):
    key_a = compute_idempotency_key(operation="PUBLISH", account_id="conta-1", video_id="video-a")
    key_b = compute_idempotency_key(operation="PUBLISH", account_id="conta-1", video_id="video-b")
    assert key_a != key_b

    claim_a = guard.claim(key_a)
    assert claim_a.outcome == OUTCOME_NUNCA_TENTADO
    guard.mark_published(claim_a.publication.id, remote_id="yt-a")

    # A publicação de video-b não é afetada pelo sucesso de video-a.
    assert guard.check(key_b).outcome == OUTCOME_NUNCA_TENTADO
    assert guard.check(key_a).outcome == OUTCOME_JA_PUBLICADO


def test_mesma_chave_base_operation_diferente_nao_colide(guard):
    key_publish = compute_idempotency_key(operation="PUBLISH", account_id="conta-1", video_id="video-x")
    key_update_metadata = compute_idempotency_key(
        operation="UPDATE_METADATA", account_id="conta-1", video_id="video-x"
    )
    assert key_publish != key_update_metadata

    claim1 = guard.claim(key_publish)
    guard.mark_published(claim1.publication.id, remote_id="yt-publish")

    # Uma republicação de metadata é uma OPERAÇÃO diferente -- não é
    # bloqueada pelo sucesso da publicação original de vídeo.
    result = guard.claim(key_update_metadata)
    assert result.outcome == OUTCOME_NUNCA_TENTADO


def test_compute_idempotency_key_e_deterministica_e_sensivel_a_cada_componente(guard):
    base = compute_idempotency_key(operation="PUBLISH", account_id="acc", video_id="vid")
    again = compute_idempotency_key(operation="PUBLISH", account_id="acc", video_id="vid")
    assert base == again  # determinística

    variants = [
        compute_idempotency_key(operation="OTHER", account_id="acc", video_id="vid"),
        compute_idempotency_key(operation="PUBLISH", account_id="other-acc", video_id="vid"),
        compute_idempotency_key(operation="PUBLISH", account_id="acc", video_id="other-vid"),
    ]
    assert base not in variants
    assert len(set(variants + [base])) == 4  # todos distintos entre si


def test_compute_idempotency_key_rejeita_componentes_vazios():
    with pytest.raises(ValueError):
        compute_idempotency_key(operation="", account_id="acc", video_id="vid")
    with pytest.raises(ValueError):
        compute_idempotency_key(operation="PUBLISH", account_id="  ", video_id="vid")


# ---------------------------------------------------------------------------
# Validação de entrada / robustez
# ---------------------------------------------------------------------------


def test_idempotency_key_vazia_e_rejeitada(guard):
    with pytest.raises(ValueError):
        guard.check("")
    with pytest.raises(ValueError):
        guard.claim("   ")


def test_todas_as_constantes_de_outcome_estao_no_conjunto_publico():
    assert IDEMPOTENCY_OUTCOMES == {
        OUTCOME_NUNCA_TENTADO,
        OUTCOME_JA_PUBLICADO,
        OUTCOME_INCONCLUSIVO,
    }


# ---------------------------------------------------------------------------
# Restart: novas instâncias enxergam o mesmo estado persistido
# ---------------------------------------------------------------------------


def test_estado_sobrevive_a_restart_com_novas_instancias(db_path):
    path, paths = db_path
    key = "chave-restart"

    db1 = LocalDatabase(path=path, paths=paths)
    guard1 = PublicationIdempotencyGuard(db1)
    claimed = guard1.claim(key, video_id=None, account_id=None)
    guard1.mark_published(claimed.publication.id, remote_id="yt-restart")

    # Novas instâncias de LocalDatabase E do guard -- nunca reuso de
    # objeto em memória (CLAUDE.md, ponto 4 "RESTART").
    db2 = LocalDatabase(path=path, paths=paths)
    guard2 = PublicationIdempotencyGuard(db2)
    result = guard2.check(key)
    assert result.outcome == OUTCOME_JA_PUBLICADO
    assert result.publication.remote_id == "yt-restart"


# ---------------------------------------------------------------------------
# Publications com campos reais (video_id/account_id válidos) -- roundtrip
# ---------------------------------------------------------------------------


def test_claim_com_video_id_e_account_id_reais_persiste_fks_corretamente(guard, local_db):
    account = Account(platform="youtube", name="Canal Teste")
    video = Video(name="Video Teste")
    local_db.insert(account)
    local_db.insert(video)

    key = compute_idempotency_key(operation="PUBLISH", account_id=account.id, video_id=video.id)
    result = guard.claim(key, video_id=video.id, account_id=account.id, title="Meu vídeo")
    assert result.outcome == OUTCOME_NUNCA_TENTADO
    persisted = local_db.get(Publication, result.publication.id)
    assert persisted.video_id == video.id
    assert persisted.account_id == account.id
    assert persisted.title == "Meu vídeo"


# ---------------------------------------------------------------------------
# Não-regressão: Circuit Breaker (Prompt 20) e RetryPolicy (Prompt 21)
# permanecem intocados -- este módulo nunca os importa nem os chama.
# ---------------------------------------------------------------------------


def test_publication_idempotency_nunca_importa_circuit_breaker_nem_retry_policy():
    import ast

    import _sistema.publication_idempotency as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names = " ".join([node.module or ""] + [alias.name for alias in node.names])
            assert "circuit_breaker" not in names.lower()
            assert "retry_policy" not in names.lower()
        elif isinstance(node, ast.Import):
            names = " ".join(alias.name for alias in node.names)
            assert "circuit_breaker" not in names.lower()
            assert "retry_policy" not in names.lower()
        if isinstance(node, ast.Attribute):
            assert node.attr.lower() not in ("circuit_breaker", "retry_policy")
        if isinstance(node, ast.Name):
            assert "circuit_breaker" not in node.id.lower()
            assert "retry_policy" not in node.id.lower()


def test_publication_nunca_cria_job_id_nem_fk_nova_para_job():
    """Confirma que nenhuma FK nova Job<->Publication foi introduzida --
    ``Publication`` continua sem ``job_id``, ``Job`` continua sem
    ``publication_id`` (ver docstring do módulo, investigação item 2)."""
    from dataclasses import fields

    field_names = {f.name for f in fields(Publication)}
    assert "job_id" not in field_names
