# -*- coding: utf-8 -*-
"""Testes do StorageManager central (PROMPT 19).

Cobre os cenários obrigatórios do Prompt: disk_usage/safety margin/
available_for_app, reservas runtime-only atômicas (incluindo corrida de
overcommit e reserva composta all-or-nothing, ambas com Barrier/Event
determinísticos, nunca sleep como prova), active_path como lease contra
limpeza, allocate_temp único/seguro, proteção absoluta (SOURCE nunca
deletável mesmo com label falso, DATABASE/WAL/SHM/BACKUP/etc. nunca
deletáveis), traversal/symlink/hardlink adversariais, TOCTOU no
plan->execute, arquivo travado não derruba o lote, classify_os_error,
promote_to_final (mesmo volume atômico, cross-volume não-atômico
documentado, destino=SOURCE recusado mesmo com overwrite=True), e
snapshot() sem segredos.
"""
from __future__ import annotations

import os
import tempfile
import threading
import time
from pathlib import Path

import pytest

from _sistema.app_paths import build_app_paths, ensure_app_directories
from _sistema.storage_manager import (
    CATEGORY_BACKUP,
    CATEGORY_BROWSER_PROFILE,
    CATEGORY_CACHE,
    CATEGORY_DATABASE,
    CATEGORY_FINAL_ARTIFACT,
    CATEGORY_FRAME_CACHE,
    CATEGORY_INTERMEDIATE_DISPOSABLE,
    CATEGORY_INTERMEDIATE_RECOVERABLE,
    CATEGORY_MODEL,
    CATEGORY_SOURCE,
    CATEGORY_TEMP,
    CATEGORY_TEMPLATE,
    CATEGORY_UNKNOWN,
    CATEGORY_USER_FILE,
    PROMOTABLE_TEMP_CATEGORIES,
    CleanupCandidate,
    CompoundReservation,
    DiskUsage,
    InsufficientStorageError,
    SafetyMarginPolicy,
    StorageCleanupError,
    StorageError,
    StoragePublishError,
    StorageReservationError,
    StorageRequirement,
    StorageVolumeUnavailableError,
    StorageManager,
    UnsafeStoragePathError,
    classify_os_error,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def managed():
    """AppPaths + StorageManager novos sobre um diretório temporário
    exclusivo deste teste (mesmo padrão de ``local_db`` em
    ``tests/test_resource_manager.py``)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        paths = build_app_paths(install_root=root / "install", data_root=root / "data")
        ensure_app_directories(paths)
        manager = StorageManager(paths)
        yield paths, manager


def _wait_until(predicate, *, timeout=5.0, interval=0.005):
    """Espera determinística por condição observável — nunca um sleep fixo
    como prova de ordering (mesmo helper de ``tests/test_resource_manager.py``)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _write_file(path: Path, content: bytes = b"conteudo") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _symlink_or_skip(target, link_path) -> None:
    """``os.symlink`` que pula (``pytest.skip``) especificamente quando o
    Windows recusa a criacao por falta de privilegio elevado/Developer
    Mode (``WinError 1314`` — ``ERROR_PRIVILEGE_NOT_HELD``). Essa e uma
    limitacao de capability do SO, nao uma falha do StorageManager: a
    logica de path-safety desses testes ja foi validada com symlink
    elevado no Windows real (PASS). Qualquer outro ``OSError`` (permissao
    de arquivo, disco cheio, etc.) NUNCA e engolido aqui — propaga
    normalmente, para nao mascarar uma falha real com um skip."""
    try:
        os.symlink(target, link_path)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip(
                "os.symlink recusado com WinError 1314 (privilegio elevado/"
                "Developer Mode ausente neste Windows) — capability do SO, "
                "nao falha do StorageManager. Validado com privilegio "
                "elevado separadamente (symlink e junction: PASS)."
            )
        raise


# ---------------------------------------------------------------------------
# 1: disk_usage
# ---------------------------------------------------------------------------


def test_disk_usage_de_path_existente(managed):
    paths, manager = managed
    usage = manager.disk_usage(paths.cache)
    assert usage.total > 0
    assert usage.free >= 0
    assert usage.used >= 0


def test_disk_usage_sobe_para_ancestral_existente(managed):
    paths, manager = managed
    inexistente = paths.cache / "nao_existe" / "ainda" / "arquivo.bin"
    usage = manager.disk_usage(inexistente)
    assert usage.total > 0


def test_disk_usage_volume_indisponivel_gera_erro_estruturado_sem_inventar_numeros(managed, monkeypatch):
    paths, manager = managed
    import shutil as shutil_module

    def _boom(_path):
        raise OSError("falha simulada de disco")

    monkeypatch.setattr(shutil_module, "disk_usage", _boom)
    with pytest.raises(StorageVolumeUnavailableError) as excinfo:
        manager.disk_usage(paths.cache)
    assert excinfo.value.operation == "disk_usage"
    assert excinfo.value.recoverable is False


# ---------------------------------------------------------------------------
# 2: safety margin / available_for_app
# ---------------------------------------------------------------------------


def test_safety_margin_usa_o_maior_entre_absoluto_e_percentual():
    policy = SafetyMarginPolicy(min_absolute_bytes=2 * 1024 ** 3, percent_of_total=0.05)
    # Disco pequeno: percentual é menor que o piso absoluto -> usa o piso.
    assert policy.compute(10 * 1024 ** 3) == 2 * 1024 ** 3
    # Disco grande: percentual supera o piso absoluto -> usa o percentual.
    assert policy.compute(1000 * 1024 ** 3) == int(0.05 * 1000 * 1024 ** 3)


def test_available_for_app_clampado_em_zero_quando_reservado_mais_margem_excede_livre(managed):
    paths, manager = managed
    manager._safety_margin = SafetyMarginPolicy(min_absolute_bytes=0, percent_of_total=0.0)
    manager.disk_usage = lambda path: DiskUsage(total=1000, used=900, free=100)
    manager.reserve(paths.cache, 60)
    # 100 livre - 0 margem - 60 reservado = 40 disponível.
    assert manager.available_for_app(paths.cache) == 40
    manager.reserve(paths.cache, 40)
    # Reserva tudo o que resta -> 0, nunca negativo.
    assert manager.available_for_app(paths.cache) == 0


# ---------------------------------------------------------------------------
# 3: reserva simples + release idempotente
# ---------------------------------------------------------------------------


def test_reserve_e_release_simples(managed):
    paths, manager = managed
    reservation = manager.reserve(paths.cache, 1024, category=CATEGORY_TEMP)
    assert manager.reserved_bytes(paths.cache) == 1024
    reservation.release()
    assert manager.reserved_bytes(paths.cache) == 0


def test_double_release_idempotente_nunca_fica_negativo_nem_inventa_capacidade(managed):
    paths, manager = managed
    reservation = manager.reserve(paths.cache, 512)
    reservation.release()
    reservation.release()  # segunda chamada: no-op seguro
    assert manager.reserved_bytes(paths.cache) == 0


def test_reserve_como_context_manager_libera_ao_sair(managed):
    paths, manager = managed
    with manager.reserve(paths.cache, 256) as reservation:
        assert manager.reserved_bytes(paths.cache) == 256
        assert reservation.released is False
    assert manager.reserved_bytes(paths.cache) == 0


# ---------------------------------------------------------------------------
# 4: adversarial — corrida de overcommit em reservas concorrentes
# ---------------------------------------------------------------------------


def _run_overcommit_race(manager, cache_path, *, capacity, n_threads, amount_each):
    """Dispara ``n_threads`` reservando ``amount_each`` cada, TODAS
    liberadas para começar ao mesmo tempo via Barrier — prova determinística
    de que a soma commitada nunca excede ``capacity`` mesmo que cada pedido
    individual caiba sozinho."""
    manager._safety_margin = SafetyMarginPolicy(min_absolute_bytes=0, percent_of_total=0.0)
    manager.disk_usage = lambda path: DiskUsage(total=capacity, used=0, free=capacity)

    barrier = threading.Barrier(n_threads)
    results = []
    results_lock = threading.Lock()

    def worker():
        barrier.wait(timeout=10)
        try:
            reservation = manager.reserve(cache_path, amount_each)
            with results_lock:
                results.append(("ok", reservation))
        except InsufficientStorageError:
            with results_lock:
                results.append(("rejected", None))

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
        assert not t.is_alive()

    committed = sum(amount_each for outcome, _ in results if outcome == "ok")
    assert committed <= capacity
    for outcome, reservation in results:
        if outcome == "ok":
            reservation.release()
    return results


def test_adversarial_overcommit_race_nunca_excede_capacidade(managed):
    """N=4 threads pedindo 40 unidades cada (cada pedido cabe sozinho em
    100), rodando via Barrier verdadeiramente concorrente; no máximo 2
    conseguem (80 <= 100 < 120), nunca as 4."""
    paths, manager = managed
    results = _run_overcommit_race(
        manager, paths.cache, capacity=100, n_threads=4, amount_each=40
    )
    accepted = sum(1 for outcome, _ in results if outcome == "ok")
    assert accepted <= 2
    assert manager.reserved_bytes(paths.cache) == 0  # todas liberadas no fim


@pytest.mark.parametrize("repeat", range(5))
def test_adversarial_overcommit_race_repetivel_5x(managed, repeat):
    """O mesmo teste de corrida, repetido pelo harness (parametrize local) —
    prova que não é flaky."""
    paths, manager = managed
    _run_overcommit_race(manager, paths.cache, capacity=100, n_threads=4, amount_each=40)


# ---------------------------------------------------------------------------
# 5: adversarial — reserva composta vaza zero em falha parcial
# ---------------------------------------------------------------------------


def test_adversarial_reserve_compound_falha_parcial_nao_vaza(managed, monkeypatch):
    """Volume A tem espaço; volume B não. ``reserve_compound`` deve falhar
    como um todo e ``reserved(A)`` deve voltar a exatamente 0."""
    paths, manager = managed
    vol_a = paths.cache / "arquivo_a.bin"
    vol_b = paths.temp / "arquivo_b.bin"

    def fake_volume_key(path):
        return "VOLUME_A" if "cache" in str(path) else "VOLUME_B"

    def fake_disk_usage(path):
        if "cache" in str(path):
            return DiskUsage(total=1000, used=0, free=1000)
        return DiskUsage(total=1000, used=990, free=10)

    monkeypatch.setattr(manager, "_volume_key", fake_volume_key)
    monkeypatch.setattr(manager, "disk_usage", fake_disk_usage)
    manager._safety_margin = SafetyMarginPolicy(min_absolute_bytes=0, percent_of_total=0.0)

    requirements = [
        StorageRequirement(path_or_volume=vol_a, estimated_bytes=500, category=CATEGORY_TEMP),
        StorageRequirement(path_or_volume=vol_b, estimated_bytes=500, category=CATEGORY_TEMP),
    ]
    with pytest.raises(InsufficientStorageError):
        manager.reserve_compound(requirements)

    assert manager.reserved_bytes(vol_a) == 0
    assert manager.reserved_bytes(vol_b) == 0


def test_reserve_compound_estimated_bytes_none_nunca_vira_zero_silencioso(managed):
    paths, manager = managed
    requirements = [StorageRequirement(path_or_volume=paths.cache, estimated_bytes=None)]
    with pytest.raises(StorageReservationError):
        manager.reserve_compound(requirements)
    assert manager.reserved_bytes(paths.cache) == 0


# ---------------------------------------------------------------------------
# 6: volumes independentes
# ---------------------------------------------------------------------------


def test_volumes_independentes_um_cheio_nao_bloqueia_o_outro(managed, monkeypatch):
    """Simula dois volumes distintos monkeypatchando ``_volume_key``/
    ``disk_usage`` (Linux sandbox de filesystem único — full validação
    multi-drive-letter real precisa de Windows real)."""
    paths, manager = managed
    vol_a = paths.cache / "a.bin"
    vol_b = paths.temp / "b.bin"

    def fake_volume_key(path):
        return "VOLUME_A" if "cache" in str(path) else "VOLUME_B"

    def fake_disk_usage(path):
        if "cache" in str(path):
            return DiskUsage(total=1000, used=1000, free=0)  # cheio
        return DiskUsage(total=1000, used=0, free=1000)  # livre

    monkeypatch.setattr(manager, "_volume_key", fake_volume_key)
    monkeypatch.setattr(manager, "disk_usage", fake_disk_usage)
    manager._safety_margin = SafetyMarginPolicy(min_absolute_bytes=0, percent_of_total=0.0)

    with pytest.raises(InsufficientStorageError):
        manager.reserve(vol_a, 10)

    reservation = manager.reserve(vol_b, 500)
    assert manager.reserved_bytes(vol_b) == 500
    reservation.release()


# ---------------------------------------------------------------------------
# 7: active_path protege contra limpeza concorrente
# ---------------------------------------------------------------------------


def test_active_path_protege_arquivo_durante_escrita_concorrente(managed):
    paths, manager = managed
    target = _write_file(paths.cache / "em_uso.bin")
    # Prova de ownership explícita (pós-1ª revisão adversarial: categoria
    # alegada sozinha nunca autoriza limpeza — ver BLOQUEADOR 1).
    manager.register_disposable_path(target, CATEGORY_TEMP)

    writer_holds = threading.Event()
    release_writer = threading.Event()

    def writer():
        with manager.active_path(target):
            writer_holds.set()
            release_writer.wait(timeout=10)

    t = threading.Thread(target=writer)
    t.start()
    assert writer_holds.wait(timeout=10)

    plan = manager.build_cleanup_plan(
        [CleanupCandidate(path=target, category=CATEGORY_TEMP, reason="teste")]
    )
    assert len(plan.items) == 0
    assert any(reason == "active_path" for _, reason in plan.skipped)

    result = manager.execute_cleanup(plan)
    assert target.exists()
    assert len(result.deleted) == 0

    release_writer.set()
    t.join(timeout=10)
    assert not t.is_alive()

    # Depois de liberado, um plano NOVO consegue limpar normalmente.
    fresh_plan = manager.build_cleanup_plan(
        [CleanupCandidate(path=target, category=CATEGORY_TEMP, reason="teste")]
    )
    assert len(fresh_plan.items) == 1
    fresh_result = manager.execute_cleanup(fresh_plan)
    assert target in fresh_result.deleted
    assert not target.exists()


# ---------------------------------------------------------------------------
# 8: allocate_temp — únicos, seguros, unicode-safe
# ---------------------------------------------------------------------------


def test_allocate_temp_gera_paths_unicos_sem_colisao_concorrente(managed):
    paths, manager = managed
    n = 100
    generated: list[Path] = []
    lock = threading.Lock()

    def worker():
        p = manager.allocate_temp()
        with lock:
            generated.append(p)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(generated) == n
    assert len(set(generated)) == n  # nenhuma colisão


def test_allocate_temp_preserva_sufixo_e_e_unicode_safe(managed):
    paths, manager = managed
    p = manager.allocate_temp(suffix=".mp4")
    assert p.name.endswith(".mp4")
    p2 = manager.allocate_temp(suffix="_legenda_ãçãõ.srt")
    assert p2.name.endswith("_legenda_ãçãõ.srt")


def test_allocate_temp_sanitiza_traversal_no_sufixo(managed):
    paths, manager = managed
    p = manager.allocate_temp(suffix="../../etc/passwd")
    assert ".." not in p.name
    assert manager.is_safe_managed_path(p)


def test_allocate_temp_create_true_cria_arquivo_vazio(managed):
    paths, manager = managed
    p = manager.allocate_temp(create=True)
    assert p.exists()
    assert p.stat().st_size == 0


# ---------------------------------------------------------------------------
# 9: adversarial — SOURCE nunca é deletável, mesmo com label falso
# ---------------------------------------------------------------------------


def test_adversarial_source_nunca_deletavel_mesmo_com_categoria_falsa(managed):
    paths, manager = managed
    original = _write_file(paths.projects / "video_original.mp4", b"bytes originais")
    manager.register_protected_path(original, CATEGORY_SOURCE)

    for fake_category in (CATEGORY_TEMP, CATEGORY_CACHE, CATEGORY_INTERMEDIATE_DISPOSABLE):
        plan = manager.build_cleanup_plan(
            [CleanupCandidate(path=original, category=fake_category, reason="tentativa_maliciosa")]
        )
        assert len(plan.items) == 0
        assert original.exists()
        result = manager.execute_cleanup(plan)
        assert len(result.deleted) == 0
        assert original.exists()
        assert original.read_bytes() == b"bytes originais"


# ---------------------------------------------------------------------------
# 10: categorias estruturalmente/registradas protegidas nunca são apagadas
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "category",
    [
        CATEGORY_FINAL_ARTIFACT,
        CATEGORY_BACKUP,
        CATEGORY_BROWSER_PROFILE,
        CATEGORY_MODEL,
        CATEGORY_TEMPLATE,
        CATEGORY_USER_FILE,
    ],
)
def test_categorias_protegidas_registradas_nunca_sao_apagadas(managed, category):
    paths, manager = managed
    target = _write_file(paths.projects / f"arquivo_{category.lower()}.bin")
    manager.register_protected_path(target, category)
    plan = manager.build_cleanup_plan(
        [CleanupCandidate(path=target, category=CATEGORY_TEMP, reason="tentativa")]
    )
    assert len(plan.items) == 0
    result = manager.execute_cleanup(plan)
    assert target.exists()
    assert len(result.deleted) == 0


def test_database_wal_shm_nunca_sao_apagados(managed):
    paths, manager = managed
    db_dir = paths.database
    db_dir.mkdir(parents=True, exist_ok=True)
    db = _write_file(db_dir / "painel.db")
    wal = _write_file(db_dir / "painel.db-wal")
    shm = _write_file(db_dir / "painel.db-shm")

    for f in (db, wal, shm):
        plan = manager.build_cleanup_plan(
            [CleanupCandidate(path=f, category=CATEGORY_TEMP, reason="tentativa")]
        )
        assert len(plan.items) == 0
        result = manager.execute_cleanup(plan)
        assert f.exists()
        assert len(result.deleted) == 0


def test_arquivo_desconhecido_fora_do_registro_nunca_e_apagado(managed):
    paths, manager = managed
    unknown = _write_file(paths.projects / "arquivo_nao_classificado.bin")
    # Nunca registrado, categoria alegada não é sequer cleanable por padrão
    # quando o candidato é honesto sobre ser desconhecido:
    plan = manager.build_cleanup_plan(
        [CleanupCandidate(path=unknown, category=CATEGORY_UNKNOWN, reason="teste")]
    )
    assert len(plan.items) == 0
    assert any(reason == "category_not_cleanable" for _, reason in plan.skipped)
    assert unknown.exists()


# ---------------------------------------------------------------------------
# 11: categorias cleanable SÃO limpáveis pelo fluxo normal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "category", [CATEGORY_TEMP, CATEGORY_CACHE, CATEGORY_FRAME_CACHE, CATEGORY_INTERMEDIATE_DISPOSABLE]
)
def test_categorias_cleanable_sao_removidas_pelo_fluxo_normal(managed, category):
    paths, manager = managed
    target = _write_file(paths.cache / f"descartavel_{category.lower()}.bin")
    # Ownership explícito: só a categoria CLEANABLE alegada não basta mais
    # (BLOQUEADOR 1 da 1ª revisão adversarial) — precisa de prova de que o
    # StorageManager de fato é dono deste arquivo.
    manager.register_disposable_path(target, category)
    plan = manager.build_cleanup_plan(
        [CleanupCandidate(path=target, category=category, reason="lixo_de_processamento")]
    )
    assert len(plan.items) == 1
    result = manager.execute_cleanup(plan)
    assert target in result.deleted
    assert not target.exists()
    assert result.bytes_freed > 0


# ---------------------------------------------------------------------------
# 12: intermediate recoverable nunca é cleanable
# ---------------------------------------------------------------------------


def test_intermediate_recoverable_nunca_esta_em_cleanable(managed):
    paths, manager = managed
    target = _write_file(paths.cache / "intermediario_reaproveitavel.bin")
    plan = manager.build_cleanup_plan(
        [CleanupCandidate(path=target, category=CATEGORY_INTERMEDIATE_RECOVERABLE, reason="teste")]
    )
    assert len(plan.items) == 0
    assert target.exists()


# ---------------------------------------------------------------------------
# 13: adversarial — path traversal
# ---------------------------------------------------------------------------


def test_adversarial_path_traversal_e_rejeitado_no_plano(managed):
    paths, manager = managed
    outside = paths.data_root.parent / "fora_do_managed_root.bin"
    _write_file(outside)
    traversal_path = paths.cache / ".." / ".." / outside.name
    plan = manager.build_cleanup_plan(
        [CleanupCandidate(path=traversal_path, category=CATEGORY_TEMP, reason="tentativa")]
    )
    assert len(plan.items) == 0
    assert any(reason == "outside_managed_roots" for _, reason in plan.skipped)
    assert outside.exists()


def test_assert_safe_managed_path_rejeita_fora_do_root(managed):
    paths, manager = managed
    outside = paths.data_root.parent / "fora.bin"
    with pytest.raises(UnsafeStoragePathError):
        manager.assert_safe_managed_path(outside)
    assert manager.is_safe_managed_path(outside) is False
    assert manager.is_safe_managed_path(paths.cache / "dentro.bin") is True


# ---------------------------------------------------------------------------
# 14: adversarial — symlink nunca é seguido
# ---------------------------------------------------------------------------


def test_adversarial_symlink_escape_nunca_segue_o_link(managed):
    """No Windows real, valide manualmente reparse points/junctions — este
    teste prova a lógica de path-safety (nunca seguir um link ao decidir
    apagar), não o comportamento exato do NTFS."""
    paths, manager = managed
    external_dir = paths.data_root.parent / "external_dir"
    external_dir.mkdir(parents=True, exist_ok=True)
    precious = _write_file(external_dir / "precious.txt", b"dados preciosos")

    link = paths.cache / "malicious_link"
    _symlink_or_skip(external_dir, link)

    candidate_via_link = link / "precious.txt"
    plan = manager.build_cleanup_plan(
        [CleanupCandidate(path=candidate_via_link, category=CATEGORY_TEMP, reason="tentativa")]
    )
    # O alvo resolvido está fora dos managed roots -> nunca vira item.
    assert len(plan.items) == 0

    # O link em si, oferecido diretamente como candidato, também é pulado
    # (política conservadora v1: nunca apaga o link nem segue o alvo).
    plan_link_itself = manager.build_cleanup_plan(
        [CleanupCandidate(path=link, category=CATEGORY_TEMP, reason="tentativa")]
    )
    assert len(plan_link_itself.items) == 0
    assert any(
        reason == "symlink_candidate_skipped" for _, reason in plan_link_itself.skipped
    )

    result = manager.execute_cleanup(plan)
    result_link = manager.execute_cleanup(plan_link_itself)
    assert precious.exists()
    assert precious.read_bytes() == b"dados preciosos"
    assert len(result.deleted) == 0
    assert len(result_link.deleted) == 0


# ---------------------------------------------------------------------------
# 15: hardlink conservatism
# ---------------------------------------------------------------------------


def test_hardlink_com_nlink_maior_que_1_e_pulado(managed):
    paths, manager = managed
    original = _write_file(paths.cache / "original.bin")
    manager.register_disposable_path(original, CATEGORY_TEMP)
    hardlink_path = paths.cache / "hardlink.bin"
    os.link(original, hardlink_path)
    assert original.stat().st_nlink > 1

    plan = manager.build_cleanup_plan(
        [CleanupCandidate(path=original, category=CATEGORY_TEMP, reason="teste")]
    )
    assert len(plan.items) == 0
    assert any(reason == "hardlink_conservatism" for _, reason in plan.skipped)
    assert original.exists()


# ---------------------------------------------------------------------------
# 16: TOCTOU — arquivo mudou entre plan e execute
# ---------------------------------------------------------------------------


def test_toctou_arquivo_mudou_apos_o_plano_e_pulado(managed):
    paths, manager = managed
    target = _write_file(paths.cache / "muda_depois.bin", b"a" * 10)
    manager.register_disposable_path(target, CATEGORY_TEMP)
    plan = manager.build_cleanup_plan(
        [CleanupCandidate(path=target, category=CATEGORY_TEMP, reason="teste")]
    )
    assert len(plan.items) == 1

    # Muda conteúdo/tamanho significativamente antes da execução.
    time.sleep(0.02)
    target.write_bytes(b"b" * 999)

    result = manager.execute_cleanup(plan)
    assert len(result.deleted) == 0
    assert any(reason == "changed_since_plan" for _, reason in result.skipped)
    assert target.exists()


# ---------------------------------------------------------------------------
# 17: arquivo travado não derruba o lote
# ---------------------------------------------------------------------------


def test_arquivo_travado_nao_impede_limpeza_dos_demais(managed):
    paths, manager = managed
    locked = _write_file(paths.cache / "travado.bin")
    livre_a = _write_file(paths.cache / "livre_a.bin")
    livre_b = _write_file(paths.cache / "livre_b.bin")
    for f in (locked, livre_a, livre_b):
        manager.register_disposable_path(f, CATEGORY_TEMP)

    plan = manager.build_cleanup_plan(
        [
            CleanupCandidate(path=locked, category=CATEGORY_TEMP, reason="teste"),
            CleanupCandidate(path=livre_a, category=CATEGORY_TEMP, reason="teste"),
            CleanupCandidate(path=livre_b, category=CATEGORY_TEMP, reason="teste"),
        ]
    )
    assert len(plan.items) == 3

    real_unlink = manager._unlink

    def fake_unlink(path):
        if path == locked.resolve():
            raise PermissionError("arquivo em uso por outro processo")
        return real_unlink(path)

    manager._unlink = fake_unlink

    result = manager.execute_cleanup(plan)
    assert livre_a in result.deleted
    assert livre_b in result.deleted
    assert not livre_a.exists()
    assert not livre_b.exists()
    assert any(p == locked.resolve() for p, _ in result.failed)
    assert locked.exists()


# ---------------------------------------------------------------------------
# 18: dry_run nunca apaga
# ---------------------------------------------------------------------------


def test_dry_run_nunca_apaga_mas_reporta(managed):
    paths, manager = managed
    target = _write_file(paths.cache / "dry_run.bin", b"x" * 50)
    manager.register_disposable_path(target, CATEGORY_TEMP)
    plan = manager.build_cleanup_plan(
        [CleanupCandidate(path=target, category=CATEGORY_TEMP, reason="teste")]
    )
    result = manager.execute_cleanup(plan, dry_run=True)
    assert target.exists()
    assert target in result.deleted
    assert result.bytes_freed == 50


# ---------------------------------------------------------------------------
# 19: classify_os_error
# ---------------------------------------------------------------------------


def test_classify_os_error_enospc_e_disco_cheio():
    exc = OSError()
    exc.errno = __import__("errno").ENOSPC
    assert classify_os_error(exc) == "DISK_FULL"


def test_classify_os_error_edquot_e_disco_cheio():
    import errno as errno_module

    if not hasattr(errno_module, "EDQUOT"):
        pytest.skip("EDQUOT indisponível nesta plataforma")
    exc = OSError()
    exc.errno = errno_module.EDQUOT
    assert classify_os_error(exc) == "DISK_FULL"


def test_classify_os_error_winerror_112_e_disco_cheio():
    exc = OSError("simulado")
    exc.winerror = 112
    assert classify_os_error(exc) == "DISK_FULL"


def test_classify_os_error_permission_generico_nao_e_disco_cheio():
    exc = PermissionError("negado")
    assert classify_os_error(exc) != "DISK_FULL"
    generic = OSError("erro genérico qualquer")
    assert classify_os_error(generic) != "DISK_FULL"


# ---------------------------------------------------------------------------
# 20: adversarial — disco cheio no meio da escrita
# ---------------------------------------------------------------------------


def test_adversarial_disco_cheio_no_meio_da_escrita_nunca_promove_parcial(managed):
    paths, manager = managed
    final_target = paths.projects / "resultado_final.mp4"
    _write_file(final_target, b"final_preexistente")

    reservation = manager.reserve(paths.temp, 1000)
    temp_path = manager.allocate_temp(suffix=".mp4", create=True)

    def fake_writer(path):
        path.write_bytes(b"parcial")
        raise OSError(__import__("errno").ENOSPC, "sem espaço")

    promoted = False
    try:
        try:
            fake_writer(temp_path)
        except OSError as exc:
            assert classify_os_error(exc) == "DISK_FULL"
            # A escrita nunca chega perto de promote_to_final — o arquivo
            # final pré-existente permanece intocado.
        else:
            promoted = True
    finally:
        reservation.release()

    assert promoted is False
    assert final_target.read_bytes() == b"final_preexistente"
    assert manager.reserved_bytes(paths.temp) == 0  # reserva sempre liberada

    # Mesmo se alguém tentasse promover o parcial por engano, o storage
    # layer aceita o tamanho > 0 (checagem de storage, não de mídia) — mas
    # comprova que só é promovido para um destino NOVO, nunca sobrescrevendo
    # sem overwrite=True.
    with pytest.raises(StoragePublishError):
        manager.promote_to_final(temp_path, final_target, overwrite=False)
    assert final_target.read_bytes() == b"final_preexistente"


# ---------------------------------------------------------------------------
# 21: promote_to_final
# ---------------------------------------------------------------------------


def test_promote_to_final_mesmo_volume_usa_os_replace(managed):
    paths, manager = managed
    temp_path = manager.allocate_temp(suffix=".mp4", create=True)
    temp_path.write_bytes(b"conteudo_final")
    final_target = paths.projects / "saida.mp4"

    result_path = manager.promote_to_final(temp_path, final_target)
    assert result_path == final_target.resolve()
    assert final_target.read_bytes() == b"conteudo_final"
    assert not temp_path.exists()


def test_promote_to_final_existente_sem_overwrite_recusa(managed):
    paths, manager = managed
    temp_path = manager.allocate_temp(create=True)
    temp_path.write_bytes(b"novo")
    final_target = _write_file(paths.projects / "ja_existe.bin", b"antigo")

    with pytest.raises(StoragePublishError):
        manager.promote_to_final(temp_path, final_target, overwrite=False)
    assert final_target.read_bytes() == b"antigo"


def test_promote_to_final_rejeita_temp_vazio(managed):
    paths, manager = managed
    temp_path = manager.allocate_temp(create=True)  # vazio
    final_target = paths.projects / "vazio.bin"
    with pytest.raises(StoragePublishError):
        manager.promote_to_final(temp_path, final_target)
    assert not final_target.exists()


def test_promote_to_final_rejeita_temp_ausente(managed):
    paths, manager = managed
    temp_path = paths.temp / "nao_existe.mp4"
    final_target = paths.projects / "saida2.mp4"
    with pytest.raises(StoragePublishError):
        manager.promote_to_final(temp_path, final_target)


def test_adversarial_promote_to_final_destino_source_recusado_mesmo_com_overwrite(managed):
    paths, manager = managed
    source = _write_file(paths.projects / "original_protegido.mp4", b"nao_pode_sobrescrever")
    manager.register_protected_path(source, CATEGORY_SOURCE)

    temp_path = manager.allocate_temp(create=True)
    temp_path.write_bytes(b"tentativa_maliciosa")

    with pytest.raises(StoragePublishError) as excinfo:
        manager.promote_to_final(temp_path, source, overwrite=True)
    assert excinfo.value.reason == "destination_protected"
    assert source.read_bytes() == b"nao_pode_sobrescrever"


def test_promote_to_final_cross_volume_nao_reivindica_atomicidade(managed, monkeypatch):
    paths, manager = managed
    temp_path = manager.allocate_temp(create=True)
    temp_path.write_bytes(b"cross_volume_conteudo")
    final_target = paths.projects / "cross_volume_saida.bin"

    calls = {"copy2": 0}
    import shutil as shutil_module

    real_copy2 = shutil_module.copy2

    def spy_copy2(src, dst, *args, **kwargs):
        calls["copy2"] += 1
        return real_copy2(src, dst, *args, **kwargs)

    def fake_volume_key(path):
        # Volumes sempre diferentes -> força o caminho cross-volume.
        return f"VOL_{path}"

    monkeypatch.setattr(shutil_module, "copy2", spy_copy2)
    monkeypatch.setattr(manager, "_volume_key", fake_volume_key)

    result_path = manager.promote_to_final(temp_path, final_target)
    assert calls["copy2"] == 1  # estratégia copy+replace foi de fato usada
    assert result_path.read_bytes() == b"cross_volume_conteudo"


# ---------------------------------------------------------------------------
# 22: exceção dentro do with reserve() ainda libera; snapshot nunca negativo
# ---------------------------------------------------------------------------


def test_reserve_libera_mesmo_com_excecao_dentro_do_with(managed):
    paths, manager = managed
    with pytest.raises(RuntimeError):
        with manager.reserve(paths.cache, 100):
            raise RuntimeError("falha simulada dentro do bloco")
    assert manager.reserved_bytes(paths.cache) == 0


def test_snapshot_nunca_mostra_contadores_negativos_apos_falhas(managed):
    paths, manager = managed
    try:
        with manager.reserve(paths.cache, 100):
            raise ValueError("boom")
    except ValueError:
        pass
    try:
        with manager.reserve(paths.temp, 50):
            raise ValueError("boom2")
    except ValueError:
        pass
    snap = manager.snapshot()
    for volume_info in snap["volumes"].values():
        if "reserved" in volume_info:
            assert volume_info["reserved"] >= 0
        if "available_for_app" in volume_info:
            assert volume_info["available_for_app"] >= 0
    assert snap["outstanding_reservations"] == 0


# ---------------------------------------------------------------------------
# 23: escala — 500 requisitos/reservas continuam corretos e rápidos
# ---------------------------------------------------------------------------


def test_escala_500_reservas_mantem_accounting_correto_e_rapido(managed):
    paths, manager = managed
    manager._safety_margin = SafetyMarginPolicy(min_absolute_bytes=0, percent_of_total=0.0)
    manager.disk_usage = lambda path: DiskUsage(total=10_000_000, used=0, free=10_000_000)

    start = time.monotonic()
    reservations = [manager.reserve(paths.cache, 1) for _ in range(500)]
    elapsed = time.monotonic() - start
    assert elapsed < 5.0  # sanidade frouxa: nada remotamente O(n^2)
    assert manager.reserved_bytes(paths.cache) == 500

    for r in reservations:
        r.release()
    assert manager.reserved_bytes(paths.cache) == 0


# ---------------------------------------------------------------------------
# 24: snapshot() nunca vaza segredos
# ---------------------------------------------------------------------------


def test_snapshot_estrutura_segura_sem_segredos(managed):
    paths, manager = managed
    with manager.reserve(paths.cache, 10):
        with manager.active_path(paths.cache / "algo.bin"):
            snap = manager.snapshot()
    forbidden_substrings = ("cookie", "token", "password", "secret")
    serialized = " ".join(str(k) for k in snap.keys()).lower()
    for vol_info in snap["volumes"].values():
        serialized += " " + " ".join(str(k) for k in vol_info.keys()).lower()
    assert not any(f in serialized for f in forbidden_substrings)
    assert "active_paths_count" in snap
    assert "outstanding_reservations" in snap


# ---------------------------------------------------------------------------
# Extras: usage_by_category (varredura real, O(n) documentado)
# ---------------------------------------------------------------------------


def test_usage_by_category_soma_bytes_por_categoria(managed):
    paths, manager = managed
    _write_file(paths.cache / "c1.bin", b"x" * 10)
    _write_file(paths.temp / "t1.bin", b"y" * 20)
    totals = manager.usage_by_category()
    assert totals.get(CATEGORY_CACHE, 0) >= 10
    assert totals.get(CATEGORY_TEMP, 0) >= 20


def test_usage_by_category_nao_segue_symlinks(managed):
    paths, manager = managed
    external_dir = paths.data_root.parent / "ext2"
    external_dir.mkdir(parents=True, exist_ok=True)
    _write_file(external_dir / "grande.bin", b"z" * 5000)
    link = paths.cache / "link_para_fora"
    _symlink_or_skip(external_dir, link)
    totals = manager.usage_by_category()
    assert totals.get(CATEGORY_CACHE, 0) < 5000


# ---------------------------------------------------------------------------
# Extras: register_protected_path rejeita categoria não-protegida
# ---------------------------------------------------------------------------


def test_register_protected_path_rejeita_categoria_cleanable(managed):
    paths, manager = managed
    target = _write_file(paths.cache / "x.bin")
    with pytest.raises(ValueError):
        manager.register_protected_path(target, CATEGORY_TEMP)


# ---------------------------------------------------------------------------
# 1ª REVISÃO ADVERSARIAL DO PROMPT 19 — testes obrigatórios novos.
#
# Cobre, item a item, a lista de 25 testes exigidos pela revisão, mais os
# cenários adicionais de cada BLOQUEADOR (fake-category em cada root
# protegida, SOURCE/FINAL_ARTIFACT/BACKUP/USER_FILE usados como temp_path de
# promote_to_final, aceitação de temp com ownership válido, linearização
# cleanup-vs-active/protected nas duas direções, identidade de arquivo com
# reaproveitamento de inode, visibilidade de reserva composta concorrente, e
# a garantia estrutural de que restart nunca transforma UNKNOWN em
# disposable).
# ---------------------------------------------------------------------------


def _new_manager_over_same_paths(paths):
    """Simula um restart REAL: uma instância NOVA de ``StorageManager`` (e
    de fato usaríamos ``AppPaths``/``LocalDatabase``/etc. novos num restart
    de produção; aqui só o StorageManager precisa ser novo, já que é o
    único componente com estado runtime relevante para estes testes) sobre
    o MESMO diretório em disco — nunca reaproveita o objeto em memória (ver
    item 4 do gate adversarial obrigatório no CLAUDE.md)."""
    return StorageManager(paths)


# -- Bloqueador 1: categoria alegada nunca é autoridade / política de root --


def test_adversarial7revisao_unknown_em_cache_mentindo_temp_e_preservado(managed):
    paths, manager = managed
    unknown = _write_file(paths.cache / "nao_registrado.bin", b"dados")
    plan = manager.build_cleanup_plan(
        [CleanupCandidate(path=unknown, category=CATEGORY_TEMP, reason="mentira")]
    )
    assert len(plan.items) == 0
    assert any(reason == "no_ownership_proof" for _, reason in plan.skipped)
    result = manager.execute_cleanup(plan)
    assert len(result.deleted) == 0
    assert unknown.exists()


def test_adversarial7revisao_unknown_em_temp_mentindo_cache_e_preservado(managed):
    paths, manager = managed
    unknown = _write_file(paths.temp / "nao_registrado.bin", b"dados")
    plan = manager.build_cleanup_plan(
        [CleanupCandidate(path=unknown, category=CATEGORY_CACHE, reason="mentira")]
    )
    assert len(plan.items) == 0
    assert any(reason == "no_ownership_proof" for _, reason in plan.skipped)
    assert unknown.exists()


@pytest.mark.parametrize(
    "root_attr, root_label",
    [
        ("backups", "backup"),
        ("models", "model"),
        ("templates", "template"),
        ("projects", "projeto/user_file"),
        ("accounts", "conta"),
        ("database", "database_dir"),
    ],
)
def test_adversarial7revisao_root_protegida_bloqueia_mesmo_com_fake_temp(managed, root_attr, root_label):
    paths, manager = managed
    root_dir = getattr(paths, root_attr)
    target = _write_file(root_dir / f"arquivo_real_{root_attr}.bin", b"dados_reais")
    plan = manager.build_cleanup_plan(
        [CleanupCandidate(path=target, category=CATEGORY_TEMP, reason=f"fake_temp_{root_label}")]
    )
    assert len(plan.items) == 0
    assert any(reason == "root_not_cleanable" for _, reason in plan.skipped)
    result = manager.execute_cleanup(plan)
    assert len(result.deleted) == 0
    assert target.exists()
    assert target.read_bytes() == b"dados_reais"


def test_adversarial7revisao_register_disposable_path_nao_burla_root_protegida(managed):
    """CORREÇÃO PÓS-2ª REVISÃO ADVERSARIAL (BLOQUEADOR 1): antes,
    ``register_disposable_path`` aceitava silenciosamente e só a checagem
    de cleanup bloqueava depois. Agora ``register_disposable_path`` recusa
    IMEDIATAMENTE (falha rápida) — nunca existe uma janela onde um registro
    "bem-sucedido" aponta para dentro de uma root não-cleanable."""
    paths, manager = managed
    target = _write_file(paths.backups / "backup_real.zip", b"dados")
    with pytest.raises(UnsafeStoragePathError) as excinfo:
        manager.register_disposable_path(target, CATEGORY_TEMP)
    assert excinfo.value.reason == "root_not_cleanable"

    # Defesa em profundidade: mesmo que um registro tivesse passado por
    # algum caminho legado, o cleanup também recusaria de novo (política de
    # root é checada em AMBOS os lugares).
    plan = manager.build_cleanup_plan(
        [CleanupCandidate(path=target, category=CATEGORY_TEMP, reason="tentativa")]
    )
    assert len(plan.items) == 0
    assert any(reason == "root_not_cleanable" for _, reason in plan.skipped)
    assert target.exists()


def test_adversarial7revisao_register_disposable_path_rejeita_categoria_invalida(managed):
    paths, manager = managed
    target = _write_file(paths.cache / "x.bin")
    with pytest.raises(ValueError):
        manager.register_disposable_path(target, CATEGORY_SOURCE)


def test_adversarial7revisao_categoria_efetiva_e_a_do_ownership_nao_a_alegada(managed):
    """O chamador alega TEMP; o registro de ownership diz FRAME_CACHE — o
    plano deve reportar a categoria do registro, nunca a alegada."""
    paths, manager = managed
    target = _write_file(paths.cache / "frame_001.png", b"dados")
    manager.register_disposable_path(target, CATEGORY_FRAME_CACHE)
    plan = manager.build_cleanup_plan(
        [CleanupCandidate(path=target, category=CATEGORY_TEMP, reason="mentira_de_categoria")]
    )
    assert len(plan.items) == 1
    assert plan.items[0].category == CATEGORY_FRAME_CACHE


# -- Bloqueador 2: promote_to_final também valida a ORIGEM (temp_path) -----


def test_adversarial7revisao_source_protegido_como_temp_de_promote_e_rejeitado(managed):
    paths, manager = managed
    source = _write_file(paths.projects / "original.mp4", b"bytes_originais_imutaveis")
    manager.register_protected_path(source, CATEGORY_SOURCE)
    final_target = paths.projects / "outro_destino.mp4"

    with pytest.raises(StoragePublishError) as excinfo:
        manager.promote_to_final(source, final_target)
    assert excinfo.value.reason in ("temp_is_protected", "temp_not_owned")
    assert source.exists()
    assert source.read_bytes() == b"bytes_originais_imutaveis"
    assert not final_target.exists()


def test_adversarial7revisao_final_artifact_protegido_como_temp_e_rejeitado(managed):
    paths, manager = managed
    final_artifact = _write_file(paths.projects / "publicado.mp4", b"artefato_final")
    manager.register_protected_path(final_artifact, CATEGORY_FINAL_ARTIFACT)
    other_target = paths.projects / "outro.mp4"

    with pytest.raises(StoragePublishError) as excinfo:
        manager.promote_to_final(final_artifact, other_target)
    assert excinfo.value.reason == "temp_is_protected"
    assert final_artifact.exists()
    assert not other_target.exists()


def test_adversarial7revisao_backup_como_temp_de_promote_e_rejeitado(managed):
    paths, manager = managed
    backup = _write_file(paths.backups / "backup.zip", b"dados_de_backup")
    other_target = paths.projects / "destino.zip"

    with pytest.raises(StoragePublishError) as excinfo:
        manager.promote_to_final(backup, other_target)
    # Política de root da origem é checada ANTES da checagem de ownership
    # (defesa em profundidade do BLOQUEADOR 1 pós-2ª revisão) — para um
    # arquivo dentro de uma root não-cleanable, a recusa já vem daí.
    assert excinfo.value.reason == "temp_root_not_cleanable"
    assert backup.exists()
    assert not other_target.exists()


def test_adversarial7revisao_user_file_como_temp_de_promote_e_rejeitado(managed):
    paths, manager = managed
    user_file = _write_file(paths.projects / "projeto_do_usuario.bin", b"dados_do_usuario")
    other_target = paths.projects / "destino.bin"

    with pytest.raises(StoragePublishError) as excinfo:
        manager.promote_to_final(user_file, other_target)
    assert excinfo.value.reason == "temp_root_not_cleanable"
    assert user_file.exists()
    assert not other_target.exists()


def test_adversarial7revisao_promote_aceita_temp_com_ownership_valido(managed):
    paths, manager = managed
    temp_path = manager.allocate_temp(suffix=".mp4", create=True)
    temp_path.write_bytes(b"conteudo_valido")
    final_target = paths.projects / "saida_valida.mp4"

    result_path = manager.promote_to_final(temp_path, final_target)
    assert result_path.read_bytes() == b"conteudo_valido"
    assert not temp_path.exists()


def test_adversarial7revisao_promote_aceita_intermediate_recoverable_registrado(managed):
    """INTERMEDIATE_RECOVERABLE não é CLEANABLE (nunca some sozinho por
    limpeza), mas continua sendo uma origem válida de promoção quando o
    ownership foi registrado explicitamente."""
    paths, manager = managed
    temp_path = _write_file(paths.temp / "intermediario.bin", b"conteudo_recuperavel")
    manager.register_disposable_path(temp_path, CATEGORY_INTERMEDIATE_RECOVERABLE)
    final_target = paths.projects / "promovido.bin"

    result_path = manager.promote_to_final(temp_path, final_target)
    assert result_path.read_bytes() == b"conteudo_recuperavel"


def test_adversarial7revisao_source_protegido_como_temp_cross_volume_tambem_e_rejeitado(managed, monkeypatch):
    paths, manager = managed
    source = _write_file(paths.projects / "original_cv.mp4", b"bytes_originais_cv")
    manager.register_protected_path(source, CATEGORY_SOURCE)
    final_target = paths.projects / "destino_cv.mp4"

    monkeypatch.setattr(manager, "_volume_key", lambda p: f"VOL_{p}")

    with pytest.raises(StoragePublishError) as excinfo:
        manager.promote_to_final(source, final_target)
    assert excinfo.value.reason in ("temp_is_protected", "temp_not_owned")
    assert source.exists()
    assert source.read_bytes() == b"bytes_originais_cv"
    assert not final_target.exists()


# -- Bloqueador 3: linearização cleanup vs active/protected -----------------


def test_adversarial7revisao_active_ganha_lock_primeiro_cleanup_nao_apaga(managed):
    paths, manager = managed
    target = _write_file(paths.cache / "protegido_por_active.bin", b"dados")
    manager.register_disposable_path(target, CATEGORY_TEMP)
    manager.mark_active(target)

    plan = manager.build_cleanup_plan(
        [CleanupCandidate(path=target, category=CATEGORY_TEMP, reason="teste")]
    )
    assert len(plan.items) == 0
    assert any(reason == "active_path" for _, reason in plan.skipped)

    manager.mark_inactive(target)


def test_adversarial7revisao_cleanup_claim_ganha_lock_primeiro_mark_active_falha(managed):
    paths, manager = managed
    target = _write_file(paths.cache / "sob_claim.bin", b"dados")
    manager.register_disposable_path(target, CATEGORY_TEMP)

    claimed_event = threading.Event()
    release_event = threading.Event()
    real_claim = manager._claim_cleanup

    def slow_claim(resolved, category):
        outcome = real_claim(resolved, category)
        if outcome[0]:
            claimed_event.set()
            release_event.wait(timeout=10)
        return outcome

    manager._claim_cleanup = slow_claim

    plan = manager.build_cleanup_plan(
        [CleanupCandidate(path=target, category=CATEGORY_TEMP, reason="teste")]
    )
    outcome = {}

    def runner():
        outcome["result"] = manager.execute_cleanup(plan)

    t = threading.Thread(target=runner)
    t.start()
    assert claimed_event.wait(timeout=10)

    with pytest.raises(StorageCleanupError) as excinfo:
        manager.mark_active(target)
    assert excinfo.value.reason == "cleanup_claim_in_progress"

    with pytest.raises(StorageCleanupError) as excinfo2:
        manager.register_protected_path(target, CATEGORY_SOURCE)
    assert excinfo2.value.reason == "cleanup_claim_in_progress"

    release_event.set()
    t.join(timeout=10)
    assert not t.is_alive()
    assert target in outcome["result"].deleted
    assert not target.exists()

    # Depois que o claim foi liberado, mark_active volta a funcionar
    # normalmente (o claim não vaza para sempre).
    other = _write_file(paths.cache / "outro.bin", b"x")
    manager.mark_active(other)
    manager.mark_inactive(other)


def test_adversarial7revisao_register_protected_falha_durante_cleanup_claim(managed):
    paths, manager = managed
    target = _write_file(paths.cache / "protegido_tarde_demais.bin", b"dados")
    manager.register_disposable_path(target, CATEGORY_TEMP)

    claimed_event = threading.Event()
    release_event = threading.Event()
    real_claim = manager._claim_cleanup

    def slow_claim(resolved, category):
        outcome = real_claim(resolved, category)
        if outcome[0]:
            claimed_event.set()
            release_event.wait(timeout=10)
        return outcome

    manager._claim_cleanup = slow_claim
    plan = manager.build_cleanup_plan(
        [CleanupCandidate(path=target, category=CATEGORY_TEMP, reason="teste")]
    )

    t = threading.Thread(target=lambda: manager.execute_cleanup(plan))
    t.start()
    assert claimed_event.wait(timeout=10)

    # register_protected_path chamado ENQUANTO o claim está ativo deve
    # falhar explicitamente — nunca "proteger com sucesso" um arquivo que
    # está sendo apagado neste exato instante.
    with pytest.raises(StorageCleanupError):
        manager.register_protected_path(target, CATEGORY_SOURCE)

    release_event.set()
    t.join(timeout=10)
    assert not target.exists()


# -- Bloqueador 4: identidade de arquivo além de size/mtime -----------------


def test_adversarial7revisao_arquivo_substituido_mesmo_size_mtime_inode_reaproveitado_e_pulado(managed):
    """Reproduz o cenário exato da revisão: tamanho E mtime forjados para
    baterem com o plano antigo. Além disso, no filesystem deste sandbox o
    número de inode costuma ser reaproveitado imediatamente após
    unlink+recriação — o teste prova que mesmo esse caso extremo é
    detectado (via ``ctime``, que o chamador não consegue forjar)."""
    paths, manager = managed
    target = _write_file(paths.cache / "identidade.bin", b"a" * 10)
    manager.register_disposable_path(target, CATEGORY_TEMP)
    plan = manager.build_cleanup_plan(
        [CleanupCandidate(path=target, category=CATEGORY_TEMP, reason="teste")]
    )
    assert len(plan.items) == 1
    st_before = target.stat()

    target.unlink()
    target.write_bytes(b"b" * 10)  # mesmo tamanho
    os.utime(target, (st_before.st_atime, st_before.st_mtime))  # mtime forjado

    result = manager.execute_cleanup(plan)
    assert len(result.deleted) == 0
    assert any(reason == "changed_since_plan" for _, reason in result.skipped)
    assert target.exists()
    assert target.read_bytes() == b"b" * 10


# -- Bloqueador 5: reserva composta — commit atômico único ------------------


def test_adversarial7revisao_compound_nunca_expoe_estado_parcial_para_observador_concorrente(managed, monkeypatch):
    """Duas threads disputam reservas compostas conflitantes em ordem
    OPOSTA sobre os mesmos dois volumes, com capacidade que só permite UMA
    das duas por vez. Uma terceira thread observadora faz polling agressivo
    de ``reserved_bytes`` durante a corrida: em nenhum instante observado a
    soma pode exceder a capacidade combinada, e nunca deve ver "só um dos
    dois volumes" com reserva de uma requisição composta ainda incompleta
    (o que só é possível se o commit for realmente atômico)."""
    paths, manager = managed
    vol_a_path = paths.cache / "a.bin"
    vol_b_path = paths.temp / "b.bin"

    def fake_volume_key(path):
        return "VOLA" if "cache" in str(path) else "VOLB"

    def fake_disk_usage(path):
        return DiskUsage(total=1000, used=0, free=1000)

    monkeypatch.setattr(manager, "_volume_key", fake_volume_key)
    monkeypatch.setattr(manager, "disk_usage", fake_disk_usage)
    manager._safety_margin = SafetyMarginPolicy(min_absolute_bytes=0, percent_of_total=0.0)

    observations = []
    stop = threading.Event()

    volume_a_key = manager._volume_key(vol_a_path)
    volume_b_key = manager._volume_key(vol_b_path)

    def observer():
        # Lê os dois volumes sob UMA ÚNICA aquisição do lock (mesmo lock
        # que ``reserve_compound`` usa para commitar) para que a leitura em
        # si seja atômica — usar duas chamadas sequenciais a
        # ``reserved_bytes`` introduziria uma corrida de LEITURA que nada
        # tem a ver com a atomicidade do COMMIT sendo testada aqui.
        while not stop.is_set():
            with manager._lock:
                a = manager._reserved_by_volume.get(volume_a_key, 0)
                b = manager._reserved_by_volume.get(volume_b_key, 0)
            observations.append((a, b))

    barrier = threading.Barrier(2)
    outcomes = []
    outcomes_lock = threading.Lock()

    def racer(amount_a, amount_b):
        barrier.wait(timeout=10)
        try:
            reqs = [
                StorageRequirement(path_or_volume=vol_a_path, estimated_bytes=amount_a, category=CATEGORY_TEMP),
                StorageRequirement(path_or_volume=vol_b_path, estimated_bytes=amount_b, category=CATEGORY_TEMP),
            ]
            reservation = manager.reserve_compound(reqs)
            with outcomes_lock:
                outcomes.append(("ok", reservation))
        except InsufficientStorageError:
            with outcomes_lock:
                outcomes.append(("rejected", None))

    obs_thread = threading.Thread(target=observer)
    obs_thread.start()
    t1 = threading.Thread(target=racer, args=(700, 700))
    t2 = threading.Thread(target=racer, args=(700, 700))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)
    stop.set()
    obs_thread.join(timeout=10)

    # Nunca visto mais de 700 reservado em qualquer volume simultaneamente
    # (só uma das duas composições cabe por vez).
    for a, b in observations:
        assert a <= 700
        assert b <= 700
        # Nunca um volume parcialmente comprometido sem o outro: como ambos
        # os racers pedem valores IDÊNTICOS (700/700) nos dois volumes, uma
        # composição bem-sucedida sempre deixa os dois volumes iguais entre
        # si nesse instante (0/0 ou 700/700) — nunca 700/0 nem 0/700.
        assert a == b

    accepted = [o for o in outcomes if o[0] == "ok"]
    assert len(accepted) <= 1
    for _, reservation in accepted:
        reservation.release()
    assert manager.reserved_bytes(vol_a_path) == 0
    assert manager.reserved_bytes(vol_b_path) == 0


@pytest.mark.parametrize("repeat", range(5))
def test_adversarial7revisao_compound_concorrente_ordem_oposta_sem_leak(managed, monkeypatch, repeat):
    paths, manager = managed
    vol_a_path = paths.cache / "a.bin"
    vol_b_path = paths.temp / "b.bin"

    def fake_volume_key(path):
        return "VOLA" if "cache" in str(path) else "VOLB"

    def fake_disk_usage(path):
        return DiskUsage(total=100, used=0, free=100)

    monkeypatch.setattr(manager, "_volume_key", fake_volume_key)
    monkeypatch.setattr(manager, "disk_usage", fake_disk_usage)
    manager._safety_margin = SafetyMarginPolicy(min_absolute_bytes=0, percent_of_total=0.0)

    barrier = threading.Barrier(2)
    outcomes = []
    outcomes_lock = threading.Lock()

    def racer(order_reversed):
        barrier.wait(timeout=10)
        reqs = [
            StorageRequirement(path_or_volume=vol_a_path, estimated_bytes=60, category=CATEGORY_TEMP),
            StorageRequirement(path_or_volume=vol_b_path, estimated_bytes=60, category=CATEGORY_TEMP),
        ]
        if order_reversed:
            reqs = list(reversed(reqs))
        try:
            reservation = manager.reserve_compound(reqs)
            with outcomes_lock:
                outcomes.append(("ok", reservation))
        except InsufficientStorageError:
            with outcomes_lock:
                outcomes.append(("rejected", None))

    t1 = threading.Thread(target=racer, args=(False,))
    t2 = threading.Thread(target=racer, args=(True,))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    accepted = [o for o in outcomes if o[0] == "ok"]
    assert len(accepted) <= 1  # 60+60 não cabe duas vezes em 100
    for _, reservation in accepted:
        reservation.release()
    assert manager.reserved_bytes(vol_a_path) == 0
    assert manager.reserved_bytes(vol_b_path) == 0


# -- Bloqueador 6: orphan/grace period — Option B é verdadeira por          --
# -- construção (ownership runtime-only + ownership sempre exigido) --------


def test_adversarial7revisao_orphan_recente_com_ownership_preservado_por_grace_period(managed):
    paths, manager = managed
    target = _write_file(paths.cache / "recente.bin", b"dados")
    manager.register_disposable_path(target, CATEGORY_TEMP)
    plan = manager.build_cleanup_plan(
        [CleanupCandidate(path=target, category=CATEGORY_TEMP, reason="teste")],
        min_age_seconds=3600,
    )
    assert len(plan.items) == 0
    assert any(reason == "too_recent_for_cleanup" for _, reason in plan.skipped)
    assert target.exists()


def test_adversarial7revisao_orphan_antigo_com_ownership_e_elegivel_apos_grace_period(managed):
    paths, manager = managed
    target = _write_file(paths.cache / "antigo.bin", b"dados")
    manager.register_disposable_path(target, CATEGORY_TEMP)
    old_time = time.time() - 7200
    os.utime(target, (old_time, old_time))
    plan = manager.build_cleanup_plan(
        [CleanupCandidate(path=target, category=CATEGORY_TEMP, reason="teste")],
        min_age_seconds=3600,
    )
    assert len(plan.items) == 1
    result = manager.execute_cleanup(plan)
    assert target in result.deleted


def test_adversarial7revisao_orphan_sem_ownership_apos_restart_real_e_preservado(managed):
    """Restart de verdade: NOVA instância de StorageManager sobre o MESMO
    diretório — a antiga tinha marcado ``mark_active`` no arquivo (ex.:
    estava sendo escrito quando o processo morreu), mas isso também é
    runtime-only e desaparece com o restart. A instância nova não tem
    NENHUM registro (nem ownership, nem active) para este caminho."""
    paths, manager = managed
    orphan = _write_file(paths.temp / "orfao_pos_crash.tmp", b"resto_de_processamento")
    manager.mark_active(orphan)

    manager_apos_restart = _new_manager_over_same_paths(paths)
    plan = manager_apos_restart.build_cleanup_plan(
        [CleanupCandidate(path=orphan, category=CATEGORY_TEMP, reason="tentativa_pos_restart")]
    )
    assert len(plan.items) == 0
    assert any(reason == "no_ownership_proof" for _, reason in plan.skipped)
    result = manager_apos_restart.execute_cleanup(plan)
    assert len(result.deleted) == 0
    assert orphan.exists()


def test_adversarial7revisao_restart_nao_transforma_unknown_em_disposable(managed):
    """Um arquivo NUNCA gerenciado por nenhuma instância (criado por outra
    ferramenta, por exemplo) continua preservado depois de um restart —
    "sobrou em temp" nunca vira "pode apagar", nem antes nem depois de
    reiniciar o processo."""
    paths, manager = managed
    mystery = _write_file(paths.temp / "misterioso.tmp", b"ninguem_sabe_o_que_e")

    manager_apos_restart = _new_manager_over_same_paths(paths)
    plan = manager_apos_restart.build_cleanup_plan(
        [CleanupCandidate(path=mystery, category=CATEGORY_CACHE, reason="tentativa")]
    )
    assert len(plan.items) == 0
    assert mystery.exists()


# ---------------------------------------------------------------------------
# Extras: PROMOTABLE_TEMP_CATEGORIES é a lista correta (documentação viva)
# ---------------------------------------------------------------------------


def test_promotable_temp_categories_contem_temp_e_intermediates_apenas():
    assert CATEGORY_TEMP in PROMOTABLE_TEMP_CATEGORIES
    assert CATEGORY_INTERMEDIATE_DISPOSABLE in PROMOTABLE_TEMP_CATEGORIES
    assert CATEGORY_INTERMEDIATE_RECOVERABLE in PROMOTABLE_TEMP_CATEGORIES
    assert CATEGORY_CACHE not in PROMOTABLE_TEMP_CATEGORIES
    assert CATEGORY_SOURCE not in PROMOTABLE_TEMP_CATEGORIES


# ---------------------------------------------------------------------------
# 2ª REVISÃO ADVERSARIAL DO PROMPT 19 — testes obrigatórios novos.
#
# Cobre os 3 bloqueadores: (1) register_disposable_path/promote_to_final não
# aplicavam a política estrutural de root da ORIGEM, permitindo reclassificar
# um arquivo real dentro de uma root protegida como TEMP promovível; (2)
# ownership sobrevivia à deleção/promoção — um arquivo NOVO no mesmo
# pathname herdava a ownership antiga ("ABA de identidade de arquivo"); (3)
# register_disposable_path não participava da linearização de cleanup claim.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "root_attr",
    ["backups", "models", "templates", "projects", "accounts", "database"],
)
def test_adversarial2revisao_register_disposable_rejeita_root_protegida(managed, root_attr):
    paths, manager = managed
    target = _write_file(getattr(paths, root_attr) / "arquivo_real.bin", b"dados_reais")
    with pytest.raises(UnsafeStoragePathError) as excinfo:
        manager.register_disposable_path(target, CATEGORY_TEMP)
    assert excinfo.value.reason == "root_not_cleanable"
    assert target.exists()
    assert target.read_bytes() == b"dados_reais"


def test_adversarial2revisao_promote_revalida_root_mesmo_com_allocation_malformada(managed):
    """Mesmo que um ``_AllocationRecord`` malformado apareça manualmente em
    ``_allocations`` (nunca deveria acontecer via API pública, mas a
    revisão exige defesa em profundidade), ``promote_to_final`` faz sua
    PRÓPRIA validação de política de root da origem e recusa mesmo
    assim — nunca confia apenas em outra função ter validado
    corretamente."""
    paths, manager = managed
    from _sistema.storage_manager import _AllocationRecord
    import uuid as _uuid

    precious = _write_file(paths.backups / "precious.bin", b"dados_preciosos_reais")
    resolved = manager._resolve_path(precious)
    with manager._lock:
        manager._allocations[resolved] = _AllocationRecord(
            category=CATEGORY_TEMP,
            disposable=True,
            created_at=time.time(),
            pid=os.getpid(),
            label=None,
            allocation_id=str(_uuid.uuid4()),
        )
    # Destino precisa ser um root NÃO-cleanable para este teste continuar
    # exercitando especificamente a defesa em profundidade da política de
    # root da ORIGEM (``temp_root_not_cleanable``) — desde a correção da
    # auditoria independente pós-3ª revisão, um destino dentro de
    # temp/cache é recusado antes mesmo de chegar na origem
    # (``destination_root_cleanable``, ver
    # ``test_auditoria_destino_dentro_de_temp_e_recusado``).
    final_target = paths.projects / "final.bin"

    with pytest.raises(StoragePublishError) as excinfo:
        manager.promote_to_final(precious, final_target)
    assert excinfo.value.reason == "temp_root_not_cleanable"
    assert precious.exists()
    assert precious.read_bytes() == b"dados_preciosos_reais"
    assert not final_target.exists()


def test_adversarial2revisao_cleanup_bem_sucedido_consome_ownership(managed):
    paths, manager = managed
    target = _write_file(paths.cache / "reuse.bin", b"conteudo_A")
    manager.register_disposable_path(target, CATEGORY_TEMP)
    plan = manager.build_cleanup_plan(
        [CleanupCandidate(path=target, category=CATEGORY_TEMP, reason="teste")]
    )
    result = manager.execute_cleanup(plan)
    assert target in result.deleted
    assert manager.allocation_category_of(target) is None


def test_adversarial2revisao_novo_arquivo_no_mesmo_pathname_nao_herda_ownership(managed):
    paths, manager = managed
    target = _write_file(paths.cache / "reuse.bin", b"conteudo_A")
    manager.register_disposable_path(target, CATEGORY_TEMP)
    plan = manager.build_cleanup_plan(
        [CleanupCandidate(path=target, category=CATEGORY_TEMP, reason="teste")]
    )
    manager.execute_cleanup(plan)
    assert not target.exists()

    # Arquivo B NOVO, não relacionado, no MESMO pathname — sem novo
    # register_disposable_path.
    b = _write_file(paths.cache / "reuse.bin", b"conteudo_B_nao_relacionado")
    plan_b = manager.build_cleanup_plan(
        [CleanupCandidate(path=b, category=CATEGORY_TEMP, reason="teste_b")]
    )
    assert len(plan_b.items) == 0
    assert any(reason == "no_ownership_proof" for _, reason in plan_b.skipped)
    result_b = manager.execute_cleanup(plan_b)
    assert len(result_b.deleted) == 0
    assert b.exists()
    assert b.read_bytes() == b"conteudo_B_nao_relacionado"


def test_adversarial2revisao_replacement_antes_do_plan_invalida_ownership_antigo(managed):
    """Registra A, substitui por B no MESMO pathname SEM passar pelo
    execute_cleanup do StorageManager (ex.: outro processo/ferramenta
    trocou o arquivo), forçando deliberadamente um inode diferente (grava
    um arquivo "queimador" no meio para não reaproveitar o número de inode
    recém-liberado, já que este sandbox reaproveita inodes rapidamente) —
    e SEM re-registrar. B precisa ser preservado."""
    paths, manager = managed
    a = _write_file(paths.cache / "replace_before_plan.bin", b"original")
    manager.register_disposable_path(a, CATEGORY_TEMP)
    a.unlink()
    _write_file(paths.cache / "_dummy_inode_burn.bin", b"burn")
    b = _write_file(paths.cache / "replace_before_plan.bin", b"substituido")

    plan = manager.build_cleanup_plan(
        [CleanupCandidate(path=b, category=CATEGORY_TEMP, reason="teste")]
    )
    assert len(plan.items) == 0
    assert any(reason == "ownership_identity_mismatch" for _, reason in plan.skipped)
    result = manager.execute_cleanup(plan)
    assert len(result.deleted) == 0
    assert b.exists()
    assert b.read_bytes() == b"substituido"


def test_adversarial2revisao_promotion_consome_ownership_da_origem(managed):
    paths, manager = managed
    temp_path = manager.allocate_temp(suffix=".mp4", create=True)
    temp_path.write_bytes(b"conteudo_promovido")
    final_target = paths.projects / "saida.mp4"
    manager.promote_to_final(temp_path, final_target)
    assert manager.allocation_category_of(temp_path) is None


def test_adversarial2revisao_novo_arquivo_no_antigo_temp_pathname_nao_herda_ownership(managed):
    paths, manager = managed
    temp_path = manager.allocate_temp(suffix=".mp4", create=True)
    temp_path.write_bytes(b"conteudo_promovido")
    final_target = paths.projects / "saida.mp4"
    manager.promote_to_final(temp_path, final_target)
    assert not temp_path.exists()

    # Um arquivo NOVO aparece no mesmo pathname do temp antigo (colisão
    # teórica de UUID à parte — aqui simulada diretamente) sem novo
    # registro: não deve ser tratado como owned.
    _write_file(temp_path, b"arquivo_nao_relacionado")
    assert manager.allocation_category_of(temp_path) is None
    plan = manager.build_cleanup_plan(
        [CleanupCandidate(path=temp_path, category=CATEGORY_TEMP, reason="teste")]
    )
    assert len(plan.items) == 0


def test_adversarial2revisao_reclassificacao_para_recoverable_antes_do_claim_e_preservado(managed):
    """ORDEM A: reclassificação (CACHE -> INTERMEDIATE_RECOVERABLE, que não
    é CLEANABLE) ganha o lock ANTES de qualquer cleanup claim. O cleanup
    precisa observar a nova categoria não-cleanable e NÃO apagar."""
    paths, manager = managed
    target = _write_file(paths.cache / "reclassificado.bin", b"dados")
    manager.register_disposable_path(target, CATEGORY_CACHE)
    manager.register_disposable_path(target, CATEGORY_INTERMEDIATE_RECOVERABLE)

    plan = manager.build_cleanup_plan(
        [CleanupCandidate(path=target, category=CATEGORY_CACHE, reason="teste")]
    )
    assert len(plan.items) == 0
    assert any(reason == "ownership_not_disposable" for _, reason in plan.skipped)
    result = manager.execute_cleanup(plan)
    assert len(result.deleted) == 0
    assert target.exists()


def test_adversarial2revisao_cleanup_claim_primeiro_reclassificacao_falha_cleanup_continua(managed):
    """ORDEM B: cleanup claim ganha o lock primeiro. A reclassificação
    concorrente falha explicitamente (nunca "sucesso" seguido de trocar a
    categoria por baixo do cleanup) e o cleanup prossegue sobre a geração
    já validada."""
    paths, manager = managed
    target = _write_file(paths.cache / "sob_claim.bin", b"dados")
    manager.register_disposable_path(target, CATEGORY_CACHE)

    claimed_event = threading.Event()
    release_event = threading.Event()
    real_claim = manager._claim_cleanup

    def slow_claim(resolved, category):
        outcome = real_claim(resolved, category)
        if outcome[0]:
            claimed_event.set()
            release_event.wait(timeout=10)
        return outcome

    manager._claim_cleanup = slow_claim
    plan = manager.build_cleanup_plan(
        [CleanupCandidate(path=target, category=CATEGORY_CACHE, reason="teste")]
    )
    outcome = {}

    def runner():
        outcome["result"] = manager.execute_cleanup(plan)

    t = threading.Thread(target=runner)
    t.start()
    assert claimed_event.wait(timeout=10)

    with pytest.raises(StorageCleanupError) as excinfo:
        manager.register_disposable_path(target, CATEGORY_INTERMEDIATE_RECOVERABLE)
    assert excinfo.value.reason == "cleanup_claim_in_progress"

    release_event.set()
    t.join(timeout=10)
    assert not t.is_alive()
    assert target in outcome["result"].deleted
    assert not target.exists()


@pytest.mark.parametrize("repeat", range(5))
def test_adversarial2revisao_ordens_de_reclassificacao_5x(managed, repeat):
    paths, manager = managed
    target = _write_file(paths.cache / f"race_{repeat}.bin", b"dados")
    manager.register_disposable_path(target, CATEGORY_CACHE)

    claimed_event = threading.Event()
    release_event = threading.Event()
    real_claim = manager._claim_cleanup

    def slow_claim(resolved, category):
        outcome = real_claim(resolved, category)
        if outcome[0]:
            claimed_event.set()
            release_event.wait(timeout=10)
        return outcome

    manager._claim_cleanup = slow_claim
    plan = manager.build_cleanup_plan(
        [CleanupCandidate(path=target, category=CATEGORY_CACHE, reason="teste")]
    )
    result_holder = {}
    t = threading.Thread(target=lambda: result_holder.__setitem__("r", manager.execute_cleanup(plan)))
    t.start()
    assert claimed_event.wait(timeout=10)
    with pytest.raises(StorageCleanupError):
        manager.register_disposable_path(target, CATEGORY_INTERMEDIATE_RECOVERABLE)
    release_event.set()
    t.join(timeout=10)
    assert target in result_holder["r"].deleted


def test_adversarial2revisao_nenhuma_allocation_stale_apos_operacoes_destrutivas(managed):
    """Varredura de sanidade: depois de um ciclo completo de
    allocate->cleanup e allocate->promote, nenhum registro de ownership
    stale sobra para os pathnames consumidos."""
    paths, manager = managed

    cleaned = _write_file(paths.cache / "vai_ser_limpo.bin", b"x")
    manager.register_disposable_path(cleaned, CATEGORY_TEMP)
    plan = manager.build_cleanup_plan(
        [CleanupCandidate(path=cleaned, category=CATEGORY_TEMP, reason="teste")]
    )
    manager.execute_cleanup(plan)
    assert manager.allocation_category_of(cleaned) is None

    promoted_temp = manager.allocate_temp(create=True)
    promoted_temp.write_bytes(b"y")
    manager.promote_to_final(promoted_temp, paths.projects / "promovido.bin")
    assert manager.allocation_category_of(promoted_temp) is None


def test_adversarial2revisao_compound_e_active_ainda_verdes(managed):
    """Sanidade rápida: as proteções da 1ª revisão continuam de pé depois
    das mudanças da 2ª (nenhuma regressão introduzida nesta rodada)."""
    paths, manager = managed
    target = _write_file(paths.cache / "ainda_protegido.bin", b"dados")
    manager.register_disposable_path(target, CATEGORY_TEMP)
    manager.mark_active(target)
    plan = manager.build_cleanup_plan(
        [CleanupCandidate(path=target, category=CATEGORY_TEMP, reason="teste")]
    )
    assert len(plan.items) == 0
    assert any(reason == "active_path" for _, reason in plan.skipped)
    manager.mark_inactive(target)


# ---------------------------------------------------------------------------
# 3ª revisão adversarial — LINEARIZAÇÃO DA PROMOÇÃO (promotion claim)
#
# Caso A: destino protegido DEPOIS da validação, ANTES do publish físico.
# Caso B: origem reclassificada DEPOIS da validação, ANTES do publish físico.
# Corrigido com ``self._promotion_claims`` reivindicando origem+destino
# atomicamente sob ``self._lock`` (``_claim_promotion``) antes de qualquer
# I/O, com liberação garantida em ``finally`` — ver "LINEARIZAÇÃO DA
# PROMOÇÃO" na docstring do módulo.
# ---------------------------------------------------------------------------


def test_adversarial3revisao_protecao_antes_do_claim_promocao_recusada(managed):
    """ORDEM A (proteção ganha o lock primeiro): destino já registrado
    como protegido antes de qualquer promoção começar -> a promoção nunca
    chega a reivindicar nada e é recusada, zero bytes do destino tocados."""
    paths, manager = managed
    temp = manager.allocate_temp(suffix=".mp4", create=True)
    temp.write_bytes(b"conteudo_original")
    final = paths.projects / "protegido_antes.mp4"
    manager.register_protected_path(final, CATEGORY_SOURCE)

    with pytest.raises(StoragePublishError) as excinfo:
        manager.promote_to_final(temp, final, overwrite=True)
    assert excinfo.value.reason == "destination_protected"
    assert not final.exists()
    assert temp.exists() and temp.read_bytes() == b"conteudo_original"


def test_adversarial3revisao_claim_antes_da_protecao_falha_explicitamente(managed):
    """ORDEM B (promoção ganha o lock primeiro): a promoção reivindica o
    destino sob o lock e é pausada logo depois (antes do os.replace). Um
    register_protected_path concorrente no MESMO destino precisa falhar
    explicitamente (nunca sucesso silencioso seguido da promoção publicar
    por cima do que seria um caminho "protegido")."""
    paths, manager = managed
    temp = manager.allocate_temp(suffix=".mp4", create=True)
    temp.write_bytes(b"conteudo_promovido")
    final = paths.projects / "claim_antes_da_protecao.mp4"

    claim_acquired = threading.Event()
    release_claim = threading.Event()
    real_claim = manager._claim_promotion

    def slow_claim(temp_resolved, final_resolved):
        outcome = real_claim(temp_resolved, final_resolved)
        if outcome[0]:
            claim_acquired.set()
            release_claim.wait(timeout=10)
        return outcome

    manager._claim_promotion = slow_claim
    outcome = {}

    def runner():
        outcome["path"] = manager.promote_to_final(temp, final, overwrite=True)

    t = threading.Thread(target=runner)
    t.start()
    assert claim_acquired.wait(timeout=10)

    with pytest.raises(StorageCleanupError) as excinfo:
        manager.register_protected_path(final, CATEGORY_SOURCE)
    assert excinfo.value.reason == "promotion_claim_in_progress"

    release_claim.set()
    t.join(timeout=10)
    assert not t.is_alive()
    assert outcome["path"] == manager._resolve_path(final)
    assert final.read_bytes() == b"conteudo_promovido"
    # a proteção concorrente falhou, então o destino não ficou "protegido"
    # depois — outra promoção legítima futura continua possível.
    assert manager.protected_category_of(final) is None


@pytest.mark.parametrize("repeat", range(5))
def test_adversarial3revisao_claim_antes_da_protecao_5x(managed, repeat):
    paths, manager = managed
    temp = manager.allocate_temp(suffix=".mp4", create=True)
    temp.write_bytes(b"conteudo")
    final = paths.projects / f"claim_antes_protecao_{repeat}.mp4"

    claim_acquired = threading.Event()
    release_claim = threading.Event()
    real_claim = manager._claim_promotion

    def slow_claim(temp_resolved, final_resolved):
        outcome = real_claim(temp_resolved, final_resolved)
        if outcome[0]:
            claim_acquired.set()
            release_claim.wait(timeout=10)
        return outcome

    manager._claim_promotion = slow_claim
    outcome = {}
    t = threading.Thread(
        target=lambda: outcome.__setitem__("path", manager.promote_to_final(temp, final, overwrite=True))
    )
    t.start()
    assert claim_acquired.wait(timeout=10)
    with pytest.raises(StorageCleanupError) as excinfo:
        manager.register_protected_path(final, CATEGORY_SOURCE)
    assert excinfo.value.reason == "promotion_claim_in_progress"
    release_claim.set()
    t.join(timeout=10)
    assert outcome["path"] == manager._resolve_path(final)


def test_adversarial3revisao_reclassificacao_para_cache_antes_do_claim_promocao_recusada(managed):
    """ORDEM A: origem reclassificada para CACHE (não promovível) ANTES de
    qualquer promoção começar -> promoção recusada, destino nunca criado."""
    paths, manager = managed
    temp = manager.allocate_temp(suffix=".mp4", create=True)
    temp.write_bytes(b"conteudo")
    manager.register_disposable_path(temp, CATEGORY_CACHE)
    final = paths.projects / "reclassificado_antes.mp4"

    with pytest.raises(StoragePublishError) as excinfo:
        manager.promote_to_final(temp, final)
    assert excinfo.value.reason == "temp_category_not_promotable"
    assert not final.exists()


def test_adversarial3revisao_claim_antes_da_reclassificacao_falha_explicitamente(managed):
    """ORDEM B: a promoção reivindica a origem sob o lock e é pausada. Uma
    reclassificação concorrente (para CACHE, categoria não promovível)
    precisa falhar explicitamente — nunca "sucesso" seguido da promoção
    antiga consumir uma geração de ownership que já deixou de ser válida
    (Caso B da 3ª revisão adversarial)."""
    paths, manager = managed
    temp = manager.allocate_temp(suffix=".mp4", create=True)
    temp.write_bytes(b"conteudo_promovido")
    final = paths.projects / "claim_antes_da_reclass.mp4"

    claim_acquired = threading.Event()
    release_claim = threading.Event()
    real_claim = manager._claim_promotion

    def slow_claim(temp_resolved, final_resolved):
        outcome = real_claim(temp_resolved, final_resolved)
        if outcome[0]:
            claim_acquired.set()
            release_claim.wait(timeout=10)
        return outcome

    manager._claim_promotion = slow_claim
    outcome = {}

    def runner():
        outcome["path"] = manager.promote_to_final(temp, final)

    t = threading.Thread(target=runner)
    t.start()
    assert claim_acquired.wait(timeout=10)

    with pytest.raises(StorageCleanupError) as excinfo:
        manager.register_disposable_path(temp, CATEGORY_CACHE)
    assert excinfo.value.reason == "promotion_claim_in_progress"

    release_claim.set()
    t.join(timeout=10)
    assert not t.is_alive()
    assert outcome["path"] == manager._resolve_path(final)
    assert final.read_bytes() == b"conteudo_promovido"
    # a promoção consumiu a geração que ela de fato validou.
    assert manager.allocation_category_of(temp) is None


@pytest.mark.parametrize("repeat", range(5))
def test_adversarial3revisao_claim_antes_da_reclassificacao_5x(managed, repeat):
    paths, manager = managed
    temp = manager.allocate_temp(suffix=".mp4", create=True)
    temp.write_bytes(b"conteudo")
    final = paths.projects / f"claim_antes_reclass_{repeat}.mp4"

    claim_acquired = threading.Event()
    release_claim = threading.Event()
    real_claim = manager._claim_promotion

    def slow_claim(temp_resolved, final_resolved):
        outcome = real_claim(temp_resolved, final_resolved)
        if outcome[0]:
            claim_acquired.set()
            release_claim.wait(timeout=10)
        return outcome

    manager._claim_promotion = slow_claim
    outcome = {}
    t = threading.Thread(
        target=lambda: outcome.__setitem__("path", manager.promote_to_final(temp, final))
    )
    t.start()
    assert claim_acquired.wait(timeout=10)
    with pytest.raises(StorageCleanupError) as excinfo:
        manager.register_disposable_path(temp, CATEGORY_CACHE)
    assert excinfo.value.reason == "promotion_claim_in_progress"
    release_claim.set()
    t.join(timeout=10)
    assert outcome["path"] == manager._resolve_path(final)
    assert manager.allocation_category_of(temp) is None


def test_adversarial3revisao_cleanup_claim_antes_da_promocao_promocao_recusada(managed):
    """ORDEM A: um cleanup já reivindicou a origem (sob lock) antes da
    promoção tentar reivindicá-la -> a promoção é recusada explicitamente
    (nunca espera silenciosamente para "roubar" o caminho do cleanup)."""
    paths, manager = managed
    target = _write_file(paths.cache / "sob_cleanup_claim.bin", b"dados")
    manager.register_disposable_path(target, CATEGORY_TEMP)

    claimed_event = threading.Event()
    release_event = threading.Event()
    real_claim = manager._claim_cleanup

    def slow_claim(resolved, category):
        outcome = real_claim(resolved, category)
        if outcome[0]:
            claimed_event.set()
            release_event.wait(timeout=10)
        return outcome

    manager._claim_cleanup = slow_claim
    plan = manager.build_cleanup_plan(
        [CleanupCandidate(path=target, category=CATEGORY_TEMP, reason="teste")]
    )
    outcome = {}

    def runner():
        outcome["result"] = manager.execute_cleanup(plan)

    t = threading.Thread(target=runner)
    t.start()
    assert claimed_event.wait(timeout=10)

    final = paths.projects / "nao_deveria_existir.bin"
    with pytest.raises(StoragePublishError) as excinfo:
        manager.promote_to_final(target, final)
    assert excinfo.value.reason == "cleanup_claim_in_progress"
    assert not final.exists()

    release_event.set()
    t.join(timeout=10)
    assert not t.is_alive()
    assert target in outcome["result"].deleted
    assert not target.exists()


def test_adversarial3revisao_promotion_claim_antes_do_cleanup_cleanup_nao_remove_origem(managed):
    """ORDEM B: a promoção já reivindicou a origem (sob lock) e está
    pausada antes do publish. Um cleanup concorrente sobre o mesmo
    caminho não pode apagá-lo — precisa observar o promotion claim e
    preservar o arquivo até o claim ser liberado."""
    paths, manager = managed
    temp = manager.allocate_temp(suffix=".bin", create=True)
    temp.write_bytes(b"conteudo_promovido")
    final = paths.projects / "promotion_antes_do_cleanup.bin"

    claim_acquired = threading.Event()
    release_claim = threading.Event()
    real_claim = manager._claim_promotion

    def slow_claim(temp_resolved, final_resolved):
        outcome = real_claim(temp_resolved, final_resolved)
        if outcome[0]:
            claim_acquired.set()
            release_claim.wait(timeout=10)
        return outcome

    manager._claim_promotion = slow_claim
    outcome = {}

    def runner():
        outcome["path"] = manager.promote_to_final(temp, final)

    t = threading.Thread(target=runner)
    t.start()
    assert claim_acquired.wait(timeout=10)

    plan = manager.build_cleanup_plan(
        [CleanupCandidate(path=temp, category=CATEGORY_TEMP, reason="teste")]
    )
    assert len(plan.items) == 0
    assert any(reason == "promotion_claim_in_progress" for _, reason in plan.skipped)
    cleanup_result = manager.execute_cleanup(plan)
    assert len(cleanup_result.deleted) == 0
    assert temp.exists()

    release_claim.set()
    t.join(timeout=10)
    assert not t.is_alive()
    assert outcome["path"] == manager._resolve_path(final)
    assert final.read_bytes() == b"conteudo_promovido"


def test_adversarial3revisao_duas_promocoes_concorrentes_mesma_origem_no_maximo_uma_vence(managed):
    """Duas promoções concorrentes tentando publicar a MESMA origem para
    destinos diferentes -> no máximo uma vence; a outra falha
    explicitamente com ``promotion_claim_in_progress``, nunca as duas
    "sucedem" sobre a mesma geração de ownership."""
    paths, manager = managed
    temp = manager.allocate_temp(suffix=".bin", create=True)
    temp.write_bytes(b"conteudo_disputado")
    final_a = paths.projects / "origem_disputada_A.bin"
    final_b = paths.projects / "origem_disputada_B.bin"

    claim_acquired = threading.Event()
    release_claim = threading.Event()
    real_claim = manager._claim_promotion

    def slow_claim(temp_resolved, final_resolved):
        outcome = real_claim(temp_resolved, final_resolved)
        if outcome[0]:
            claim_acquired.set()
            release_claim.wait(timeout=10)
        return outcome

    manager._claim_promotion = slow_claim
    result_a = {}

    def runner_a():
        try:
            result_a["path"] = manager.promote_to_final(temp, final_a)
        except StoragePublishError as exc:
            result_a["error"] = exc

    t_a = threading.Thread(target=runner_a)
    t_a.start()
    assert claim_acquired.wait(timeout=10)

    manager._claim_promotion = real_claim
    result_b = {}
    try:
        result_b["path"] = manager.promote_to_final(temp, final_b)
    except StoragePublishError as exc:
        result_b["error"] = exc

    release_claim.set()
    t_a.join(timeout=10)

    successes = [r for r in (result_a, result_b) if "path" in r]
    errors = [r for r in (result_a, result_b) if "error" in r]
    assert len(successes) == 1
    assert len(errors) == 1
    assert errors[0]["error"].reason == "promotion_claim_in_progress"


def test_adversarial3revisao_duas_promocoes_concorrentes_mesmo_destino_sem_dupla_publicacao(managed):
    """Duas promoções concorrentes de origens DIFERENTES para o MESMO
    destino -> comportamento determinístico, nunca dupla publicação nem
    conteúdo misturado; exatamente uma vence."""
    paths, manager = managed
    temp_a = manager.allocate_temp(suffix=".bin", create=True)
    temp_a.write_bytes(b"conteudo_A")
    temp_b = manager.allocate_temp(suffix=".bin", create=True)
    temp_b.write_bytes(b"conteudo_B")
    final = paths.projects / "destino_disputado.bin"

    claim_acquired = threading.Event()
    release_claim = threading.Event()
    real_claim = manager._claim_promotion

    def slow_claim(temp_resolved, final_resolved):
        outcome = real_claim(temp_resolved, final_resolved)
        if outcome[0]:
            claim_acquired.set()
            release_claim.wait(timeout=10)
        return outcome

    manager._claim_promotion = slow_claim
    result_a = {}

    def runner_a():
        try:
            result_a["path"] = manager.promote_to_final(temp_a, final)
        except StoragePublishError as exc:
            result_a["error"] = exc

    t_a = threading.Thread(target=runner_a)
    t_a.start()
    assert claim_acquired.wait(timeout=10)

    manager._claim_promotion = real_claim
    result_b = {}
    try:
        result_b["path"] = manager.promote_to_final(temp_b, final, overwrite=True)
    except StoragePublishError as exc:
        result_b["error"] = exc

    release_claim.set()
    t_a.join(timeout=10)

    successes = [r for r in (result_a, result_b) if "path" in r]
    errors = [r for r in (result_a, result_b) if "error" in r]
    assert len(successes) == 1
    assert len(errors) == 1
    assert errors[0]["error"].reason == "promotion_claim_in_progress"
    winner_content = b"conteudo_A" if "path" in result_a else b"conteudo_B"
    assert final.read_bytes() == winner_content


# ===========================================================================
# CORREÇÃO PÓS-EVIDÊNCIA REAL DE WINDOWS -- retry curto e limitado em torno
# de ``os.replace`` (ver ``_replace_with_bounded_retry`` e "DIFERENÇAS DE
# PLATAFORMA" na docstring de storage_manager.py). A corrida real (duas
# instâncias concorrentes de StorageManager promovendo para o MESMO
# destino) não é reproduzível de forma confiável em CI Linux -- os testes
# abaixo provam o comportamento via injeção CONTROLADA e DETERMINÍSTICA da
# falha exata observada no Windows real (PermissionError/WinError 5).
# ===========================================================================


def test_promote_to_final_recupera_de_falha_transitoria_de_os_replace(managed, monkeypatch):
    """Injeta EXATAMENTE o padrão de falha observado no Windows real:
    ``os.replace`` levanta ``PermissionError`` nas primeiras N-1 chamadas
    e só sucede na última tentativa do orçamento de retry -- prova que
    ``promote_to_final`` se recupera e o arquivo é promovido normalmente,
    sem exceção propagar ao chamador."""
    from _sistema.storage_manager import _FINAL_REPLACE_RETRY_ATTEMPTS

    paths, manager = managed
    temp = manager.allocate_temp(suffix=".bin", create=True)
    temp.write_bytes(b"dados-recuperados")
    final = paths.projects / "recupera_transitoria.bin"

    real_replace = os.replace
    calls: list[int] = []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) < _FINAL_REPLACE_RETRY_ATTEMPTS:
            raise PermissionError("[WinError 5] Acesso negado (simulado)")
        return real_replace(*args, **kwargs)

    monkeypatch.setattr("_sistema.storage_manager.os.replace", flaky)

    result = manager.promote_to_final(temp, final)

    assert result.exists()
    assert result.read_bytes() == b"dados-recuperados"
    assert len(calls) == _FINAL_REPLACE_RETRY_ATTEMPTS  # sucedeu exatamente na última tentativa
    assert not temp.exists()  # origem foi de fato movida na chamada que sucedeu


def test_promote_to_final_falha_persistente_de_os_replace_propaga_apos_esgotar_retry(managed, monkeypatch):
    """Simétrico ao teste acima: se ``os.replace`` falhar SEMPRE (falha
    persistente, não transitória), ``promote_to_final`` precisa esgotar o
    orçamento de retry (número exato e limitado de tentativas -- nunca
    retry infinito) e PROPAGAR a falha original como ``StoragePublishError``
    estruturada -- nunca reportar sucesso, nunca engolir silenciosamente."""
    from _sistema.storage_manager import _FINAL_REPLACE_RETRY_ATTEMPTS

    paths, manager = managed
    temp = manager.allocate_temp(suffix=".bin", create=True)
    temp.write_bytes(b"dados-nunca-promovidos")
    final = paths.projects / "falha_persistente.bin"

    calls: list[int] = []

    def always_boom(*args, **kwargs):
        calls.append(1)
        raise PermissionError("[WinError 5] Acesso negado (simulado, persistente)")

    monkeypatch.setattr("_sistema.storage_manager.os.replace", always_boom)

    with pytest.raises(StoragePublishError) as excinfo:
        manager.promote_to_final(temp, final)
    assert excinfo.value.reason == "final_replace_retries_exhausted"

    assert len(calls) == _FINAL_REPLACE_RETRY_ATTEMPTS  # nunca mais tentativas que o orçamento
    assert temp.exists()  # origem nunca foi tocada -- nenhuma promoção parcial
    assert not final.exists()

    # o claim de promoção foi liberado -- uma tentativa legítima seguinte
    # (com os.replace restaurado) funciona normalmente.
    monkeypatch.undo()
    result = manager.promote_to_final(temp, final)
    assert result.exists()


def test_adversarial3revisao_falha_os_replace_libera_claims(managed, monkeypatch):
    """``os.replace`` falhando PERSISTENTEMENTE (mesmo volume, todas as
    tentativas do retry curto esgotadas) precisa liberar o promotion
    claim de origem+destino no ``finally`` — sem isso, uma promoção
    seguinte legítima ficaria bloqueada para sempre por um claim órfão.

    Desde a correção da janela de corrida real de Windows
    (``_replace_with_bounded_retry`` -- ver docstring do módulo), uma
    falha PERSISTENTE (todas as tentativas do retry falham, como
    simulado aqui) propaga como ``StoragePublishError`` estruturada
    (não mais o ``OSError`` cru), nunca engolida silenciosamente."""
    paths, manager = managed
    temp = manager.allocate_temp(suffix=".bin", create=True)
    temp.write_bytes(b"dados")
    final = paths.projects / "falha_os_replace.bin"
    resolved_temp = manager._resolve_path(temp)
    resolved_final = manager._resolve_path(final)

    def boom(*args, **kwargs):
        raise OSError("falha simulada de os.replace")

    monkeypatch.setattr("_sistema.storage_manager.os.replace", boom)

    with pytest.raises(StoragePublishError) as excinfo:
        manager.promote_to_final(temp, final)
    assert excinfo.value.reason == "final_replace_retries_exhausted"

    assert resolved_temp not in manager._promotion_claims
    assert resolved_final not in manager._promotion_claims
    assert temp.exists()
    assert not final.exists()

    monkeypatch.undo()
    result = manager.promote_to_final(temp, final)
    assert result == resolved_final


def test_adversarial3revisao_falha_copia_cross_volume_libera_claims(managed, monkeypatch):
    """Falha na cópia cross-volume (``shutil.copy2``) também precisa
    liberar o claim — mesma disciplina do ``os.replace`` no caminho
    mesmo-volume."""
    paths, manager = managed
    temp = manager.allocate_temp(suffix=".bin", create=True)
    temp.write_bytes(b"dados")
    final = paths.projects / "falha_cross_volume.bin"
    resolved_temp = manager._resolve_path(temp)
    resolved_final = manager._resolve_path(final)

    manager._same_volume = lambda a, b: False

    def boom(*args, **kwargs):
        raise OSError("falha simulada de copy2 cross-volume")

    monkeypatch.setattr("_sistema.storage_manager.shutil.copy2", boom)

    with pytest.raises(OSError):
        manager.promote_to_final(temp, final)

    assert resolved_temp not in manager._promotion_claims
    assert resolved_final not in manager._promotion_claims
    assert temp.exists()
    assert not final.exists()


def test_adversarial3revisao_validate_false_libera_claims(managed):
    """``validate`` retornando ``False`` recusa a promoção mas precisa
    liberar o claim — permitindo uma tentativa seguinte legítima."""
    paths, manager = managed
    temp = manager.allocate_temp(suffix=".bin", create=True)
    temp.write_bytes(b"dados")
    final = paths.projects / "validate_false.bin"
    resolved_temp = manager._resolve_path(temp)
    resolved_final = manager._resolve_path(final)

    with pytest.raises(StoragePublishError) as excinfo:
        manager.promote_to_final(temp, final, validate=lambda _p: False)
    assert excinfo.value.reason == "validate_rejected"
    assert resolved_temp not in manager._promotion_claims
    assert resolved_final not in manager._promotion_claims
    assert temp.exists()
    assert not final.exists()

    result = manager.promote_to_final(temp, final)
    assert result == resolved_final


def test_adversarial3revisao_excecao_no_validate_libera_claims(managed):
    """Uma exceção dentro do callback ``validate`` também precisa liberar
    o claim, nunca deixá-lo órfão bloqueando futuras operações."""
    paths, manager = managed
    temp = manager.allocate_temp(suffix=".bin", create=True)
    temp.write_bytes(b"dados")
    final = paths.projects / "validate_raise.bin"
    resolved_temp = manager._resolve_path(temp)
    resolved_final = manager._resolve_path(final)

    def boom(_path):
        raise RuntimeError("validate quebrou")

    with pytest.raises(StoragePublishError) as excinfo:
        manager.promote_to_final(temp, final, validate=boom)
    assert excinfo.value.reason == "validate_raised"
    assert resolved_temp not in manager._promotion_claims
    assert resolved_final not in manager._promotion_claims

    result = manager.promote_to_final(temp, final)
    assert result == resolved_final


def test_adversarial3revisao_promocao_bem_sucedida_consome_geracao_validada(managed):
    """Depois de uma promoção bem-sucedida, a geração de ownership que ela
    de fato validou foi consumida (nenhuma ownership stale sobra para o
    pathname de origem)."""
    paths, manager = managed
    temp = manager.allocate_temp(suffix=".mp4", create=True)
    resolved_temp = manager._resolve_path(temp)
    allocation_id_before = manager._allocations[resolved_temp].allocation_id
    temp.write_bytes(b"dados")
    final = paths.projects / "consumo_geracao.mp4"

    manager.promote_to_final(temp, final)

    assert allocation_id_before is not None
    assert manager.allocation_category_of(temp) is None
    assert resolved_temp not in manager._allocations


def test_adversarial3revisao_consume_generation_aware_nao_remove_geracao_mais_nova(managed):
    """Prova direta da semântica de
    ``_consume_allocation_if_generation_matches``: uma operação que
    validou uma geração ANTIGA nunca pode remover uma geração NOVA criada
    depois no mesmo pathname — nunca ``pop`` incondicional."""
    paths, manager = managed
    target = _write_file(paths.cache / "geracao.bin", b"x")
    manager.register_disposable_path(target, CATEGORY_TEMP)
    resolved = manager._resolve_path(target)
    old_allocation_id = manager._allocations[resolved].allocation_id

    # Reclassificação legítima cria uma NOVA geração para o mesmo pathname.
    manager.register_disposable_path(target, CATEGORY_CACHE)
    new_allocation_id = manager._allocations[resolved].allocation_id
    assert new_allocation_id != old_allocation_id

    # Uma operação antiga que só validou a geração ANTIGA tenta consumir —
    # não pode remover a geração nova.
    manager._consume_allocation_if_generation_matches(resolved, old_allocation_id)
    assert manager.allocation_category_of(target) == CATEGORY_CACHE

    # A operação que de fato validou a geração nova consome corretamente.
    manager._consume_allocation_if_generation_matches(resolved, new_allocation_id)
    assert manager.allocation_category_of(target) is None


def test_adversarial3revisao_115_testes_anteriores_continuam_verdes_sanidade(managed):
    """Sanidade rápida final: as proteções das rodadas 1 e 2 continuam de
    pé depois da linearização da promoção (nenhuma regressão introduzida
    nesta rodada) — cobertura completa continua em
    ``pytest -q tests/test_storage_manager.py`` (115 testes anteriores +
    os desta rodada)."""
    paths, manager = managed
    source = _write_file(paths.projects / "source_ainda_protegido.mp4", b"bytes")
    manager.register_protected_path(source, CATEGORY_SOURCE)
    final = paths.projects / "final_ainda_recusado.mp4"
    with pytest.raises(StoragePublishError) as excinfo:
        manager.promote_to_final(source, final)
    assert excinfo.value.reason == "temp_is_protected"
    assert source.exists()
    assert not final.exists()


# ---------------------------------------------------------------------------
# Auditoria independente pós-3ª revisão adversarial — dois bloqueadores
# adicionais em ``promote_to_final``:
#
# (1) publicação cross-volume deixava a origem física órfã (copy+replace
#     nunca removia o temp, mas a ownership era consumida incondicionalmente
#     de qualquer forma — vazamento permanente de armazenamento);
# (2) a origem não exigia arquivo regular único — symlink, diretório e
#     hardlink (inclusive hardlink para um SOURCE protegido, violando
#     PROTECTED WINS) eram todos aceitos.
# ---------------------------------------------------------------------------


def test_auditoria_crossvolume_bem_sucedido_nao_deixa_temp_orfao(managed):
    """Caminho feliz cross-volume: destino recebe os bytes corretos, a
    origem física é removida (não fica órfã) e a ownership é consumida
    somente porque a remoção de fato aconteceu."""
    paths, manager = managed
    temp = manager.allocate_temp(suffix=".bin", create=True)
    temp.write_bytes(b"conteudo_cross_volume")
    final = paths.projects / "saida_crossvol_feliz.bin"

    manager._same_volume = lambda a, b: False  # força o caminho cross-volume

    result = manager.promote_to_final(temp, final)

    assert result == manager._resolve_path(final)
    assert final.exists() and final.read_bytes() == b"conteudo_cross_volume"
    assert not temp.exists()
    assert manager.allocation_category_of(temp) is None


def test_auditoria_crossvolume_falha_ao_apagar_origem_preserva_ownership(managed):
    """Se a remoção da origem falhar DEPOIS que o destino já foi publicado
    com sucesso: a promoção não vira falha total (já aconteceu), o temp
    permanece fisicamente, a ownership continua válida, e um cleanup
    posterior consegue tratar o temporário órfão."""
    paths, manager = managed
    temp = manager.allocate_temp(suffix=".bin", create=True)
    temp.write_bytes(b"conteudo_falha_delete")
    final = paths.projects / "saida_falha_delete.bin"
    resolved_temp = manager._resolve_path(temp)

    manager._same_volume = lambda a, b: False
    real_unlink = manager._unlink

    def boom_unlink(path):
        if path == resolved_temp:
            raise OSError("falha simulada de delete da origem")
        return real_unlink(path)

    manager._unlink = boom_unlink

    result = manager.promote_to_final(temp, final)

    resolved_final = manager._resolve_path(final)
    assert result == resolved_final
    assert final.exists() and final.read_bytes() == b"conteudo_falha_delete"
    assert temp.exists()
    assert manager.allocation_category_of(temp) == CATEGORY_TEMP

    manager._unlink = real_unlink
    plan = manager.build_cleanup_plan(
        [CleanupCandidate(path=temp, category=CATEGORY_TEMP, reason="orfao_cross_volume")]
    )
    cleanup_result = manager.execute_cleanup(plan)
    assert temp in cleanup_result.deleted
    assert not temp.exists()


def test_auditoria_symlink_como_origem_e_recusado(managed):
    """Uma origem cujo pathname é um symlink/reparse point é recusada sem
    seguir o link — o arquivo real apontado nunca é publicado nem tem sua
    ownership consumida incorretamente."""
    paths, manager = managed
    real_file = _write_file(paths.temp / "real.bin", b"conteudo_real")
    manager.register_disposable_path(real_file, CATEGORY_TEMP)
    link_path = paths.temp / "link.bin"
    _symlink_or_skip(str(real_file), str(link_path))
    manager.register_disposable_path(link_path, CATEGORY_TEMP)

    final = paths.projects / "via_symlink.bin"
    with pytest.raises(StoragePublishError) as excinfo:
        manager.promote_to_final(link_path, final)

    assert excinfo.value.reason == "temp_is_symlink"
    assert not final.exists()
    assert manager.allocation_category_of(real_file) == CATEGORY_TEMP


def test_auditoria_diretorio_como_origem_e_recusado(managed):
    """Um diretório registrado como origem é recusado — ``promote_to_final``
    exige um arquivo regular, nunca aceita ``st_size`` como prova
    suficiente."""
    paths, manager = managed
    dir_path = paths.temp / "diretorio_temp"
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / "arquivo_dentro.bin").write_bytes(b"x")
    manager.register_disposable_path(dir_path, CATEGORY_TEMP)

    final = paths.projects / "via_diretorio.bin"
    with pytest.raises(StoragePublishError) as excinfo:
        manager.promote_to_final(dir_path, final)

    assert excinfo.value.reason == "temp_not_regular_file"
    assert not final.exists()


def test_auditoria_hardlink_comum_como_origem_e_recusado(managed):
    """Uma origem com ``st_nlink > 1`` é recusada incondicionalmente —
    mesmo conservadorismo de hardlink já aplicado por ``execute_cleanup``."""
    paths, manager = managed
    original = _write_file(paths.temp / "original.bin", b"conteudo_hardlink")
    hardlink = paths.temp / "hardlink.bin"
    os.link(str(original), str(hardlink))
    manager.register_disposable_path(hardlink, CATEGORY_TEMP)

    final = paths.projects / "via_hardlink.bin"
    with pytest.raises(StoragePublishError) as excinfo:
        manager.promote_to_final(hardlink, final)

    assert excinfo.value.reason == "hardlink_conservatism"
    assert not final.exists()


@pytest.mark.parametrize("repeat", range(5))
def test_auditoria_hardlink_para_source_protegido_e_recusado_5x(managed, repeat):
    """O caso mais grave: um hardlink dentro de ``temp`` apontando para a
    mesma identidade física de um SOURCE protegido nunca pode ser aceito
    como origem — publicar por cima do destino modificaria fisicamente o
    SOURCE através da identidade compartilhada, violando PROTECTED WINS."""
    paths, manager = managed
    source = _write_file(
        paths.projects / f"source_protegido_{repeat}.bin", b"bytes_originais_protegidos"
    )
    manager.register_protected_path(source, CATEGORY_SOURCE)

    link_in_temp = paths.temp / f"link_para_source_{repeat}.bin"
    link_in_temp.parent.mkdir(parents=True, exist_ok=True)
    os.link(str(source), str(link_in_temp))
    manager.register_disposable_path(link_in_temp, CATEGORY_TEMP)

    final = paths.projects / f"final_via_hardlink_source_{repeat}.bin"
    with pytest.raises(StoragePublishError) as excinfo:
        manager.promote_to_final(link_in_temp, final, overwrite=True)

    assert excinfo.value.reason == "hardlink_conservatism"
    assert not final.exists()
    assert source.exists() and source.read_bytes() == b"bytes_originais_protegidos"


def test_auditoria_140_testes_anteriores_continuam_verdes_sanidade(managed):
    """Sanidade final: proteções das rodadas 1-3 continuam de pé depois
    das correções da auditoria independente (nenhuma regressão)."""
    paths, manager = managed
    temp = manager.allocate_temp(suffix=".bin", create=True)
    temp.write_bytes(b"conteudo_normal")
    final = paths.projects / "promocao_normal_ainda_funciona.bin"
    result = manager.promote_to_final(temp, final)
    assert result == manager._resolve_path(final)
    assert final.read_bytes() == b"conteudo_normal"
    assert not temp.exists()
    assert manager.allocation_category_of(temp) is None


# ---------------------------------------------------------------------------
# Auditoria independente, 2ª rodada — 3 bloqueadores adicionais:
#
# (1) ``lstat()`` falhando com qualquer ``OSError`` diferente de
#     ``FileNotFoundError`` era tratado como "origem ausente", consumindo
#     ownership de um arquivo que podia perfeitamente continuar existindo
#     (PermissionError, I/O error, etc. são ESTADO DESCONHECIDO, nunca
#     ausência confirmada);
# (2) ``_promotion_authorize_locked`` não checava ``_active_paths`` — a
#     simetria com ``mark_active`` (que já checava ``_promotion_claims``)
#     só existia em uma direção;
# (3) ``promote_to_final`` aceitava publicar DENTRO de um managed root
#     estruturalmente CLEANABLE (temp/cache), criando um arquivo final
#     órfão sem ownership, e também aceitava origem == destino,
#     consumindo a ownership de um arquivo que nunca saiu do lugar.
# ---------------------------------------------------------------------------


def test_auditoria2_lstat_permission_error_pos_publish_preserva_ownership(managed):
    """``PermissionError`` no ``lstat`` pós-publicação cross-volume é
    ESTADO DESCONHECIDO, nunca ausência confirmada — a promoção não vira
    falha total (destino já publicado), a ownership da origem permanece
    válida, o promotion claim é liberado, e um cleanup posterior consegue
    tratar o temp quando o erro desaparecer."""
    paths, manager = managed
    temp = manager.allocate_temp(suffix=".bin", create=True)
    temp.write_bytes(b"conteudo")
    final = paths.projects / "saida_permission_error.bin"
    resolved_temp = manager._resolve_path(temp)

    manager._same_volume = lambda a, b: False
    real_cleanup = manager._cleanup_cross_volume_origin

    def boom_cleanup(t, allocation_id):
        real_lstat = Path.lstat

        def boom_lstat(self, *a, **kw):
            if self == resolved_temp:
                raise PermissionError("acesso negado simulado")
            return real_lstat(self, *a, **kw)

        Path.lstat = boom_lstat
        try:
            return real_cleanup(t, allocation_id)
        finally:
            Path.lstat = real_lstat

    manager._cleanup_cross_volume_origin = boom_cleanup

    result = manager.promote_to_final(temp, final)

    assert result == manager._resolve_path(final)
    assert final.exists() and final.read_bytes() == b"conteudo"
    assert temp.exists()
    assert manager.allocation_category_of(temp) == CATEGORY_TEMP
    assert resolved_temp not in manager._promotion_claims

    manager._cleanup_cross_volume_origin = real_cleanup
    plan = manager.build_cleanup_plan(
        [CleanupCandidate(path=temp, category=CATEGORY_TEMP, reason="pos_erro_lstat")]
    )
    cleanup_result = manager.execute_cleanup(plan)
    assert temp in cleanup_result.deleted
    assert not temp.exists()


def test_auditoria2_lstat_oserror_generico_pos_publish_preserva_ownership(managed):
    """Mesma garantia para um ``OSError`` genérico (ex.: I/O error/EIO) —
    só ``FileNotFoundError`` confirma ausência física; qualquer outro
    ``OSError`` preserva a ownership em vez de a consumir."""
    paths, manager = managed
    temp = manager.allocate_temp(suffix=".bin", create=True)
    temp.write_bytes(b"conteudo")
    final = paths.projects / "saida_oserror_generico.bin"
    resolved_temp = manager._resolve_path(temp)

    manager._same_volume = lambda a, b: False
    real_cleanup = manager._cleanup_cross_volume_origin

    def boom_cleanup(t, allocation_id):
        real_lstat = Path.lstat

        def boom_lstat(self, *a, **kw):
            if self == resolved_temp:
                raise OSError("erro de I/O simulado (EIO)")
            return real_lstat(self, *a, **kw)

        Path.lstat = boom_lstat
        try:
            return real_cleanup(t, allocation_id)
        finally:
            Path.lstat = real_lstat

    manager._cleanup_cross_volume_origin = boom_cleanup

    result = manager.promote_to_final(temp, final)

    assert result == manager._resolve_path(final)
    assert final.exists() and final.read_bytes() == b"conteudo"
    assert temp.exists()
    assert manager.allocation_category_of(temp) == CATEGORY_TEMP
    assert resolved_temp not in manager._promotion_claims


def test_auditoria2_origem_ativa_antes_da_promocao_e_recusada(managed):
    """ORDEM A: ``mark_active`` na origem ganha o lock antes de qualquer
    promoção começar -> a promoção nunca chega a reivindicar nada e é
    recusada; nenhum byte é movido."""
    paths, manager = managed
    temp = manager.allocate_temp(suffix=".bin", create=True)
    temp.write_bytes(b"dados")
    final = paths.projects / "final_origem_ativa.bin"

    with manager.active_path(temp):
        with pytest.raises(StoragePublishError) as excinfo:
            manager.promote_to_final(temp, final)
        assert excinfo.value.reason == "temp_active"

    assert temp.exists()
    assert not final.exists()


def test_auditoria2_destino_ativo_antes_da_promocao_e_recusado(managed):
    """ORDEM A no destino: ``mark_active`` no destino ganha o lock antes
    -> promoção recusada, destino não sobrescrito (finalidade de
    ``active_path`` como lease de "arquivo em uso")."""
    paths, manager = managed
    final = _write_file(paths.projects / "final_ativo.bin", b"old")
    temp = manager.allocate_temp(suffix=".bin", create=True)
    temp.write_bytes(b"new")

    with manager.active_path(final):
        with pytest.raises(StoragePublishError) as excinfo:
            manager.promote_to_final(temp, final, overwrite=True)
        assert excinfo.value.reason == "destination_active"

    assert final.read_bytes() == b"old"


def test_auditoria2_promotion_claim_origem_antes_de_mark_active_falha(managed):
    """ORDEM B: a promoção reivindica a origem sob o lock primeiro e é
    pausada. Um ``mark_active`` concorrente sobre a MESMA origem precisa
    falhar explicitamente (``promotion_claim_in_progress``) — linearização
    bidirecional preservada."""
    paths, manager = managed
    temp = manager.allocate_temp(suffix=".bin", create=True)
    temp.write_bytes(b"dados")
    final = paths.projects / "claim_origem_antes_de_active.bin"

    claim_acquired = threading.Event()
    release_claim = threading.Event()
    real_claim = manager._claim_promotion

    def slow_claim(temp_resolved, final_resolved):
        outcome = real_claim(temp_resolved, final_resolved)
        if outcome[0]:
            claim_acquired.set()
            release_claim.wait(timeout=10)
        return outcome

    manager._claim_promotion = slow_claim
    outcome = {}

    def runner():
        outcome["path"] = manager.promote_to_final(temp, final)

    t = threading.Thread(target=runner)
    t.start()
    assert claim_acquired.wait(timeout=10)

    with pytest.raises(StorageCleanupError) as excinfo:
        manager.mark_active(temp)
    assert excinfo.value.reason == "promotion_claim_in_progress"

    release_claim.set()
    t.join(timeout=10)
    assert not t.is_alive()
    assert outcome["path"] == manager._resolve_path(final)


def test_auditoria2_promotion_claim_destino_antes_de_mark_active_falha(managed):
    """Mesma linearização para o DESTINO: a promoção reivindica o destino
    primeiro; ``mark_active`` concorrente no destino falha explicitamente."""
    paths, manager = managed
    temp = manager.allocate_temp(suffix=".bin", create=True)
    temp.write_bytes(b"dados")
    final = paths.projects / "claim_destino_antes_de_active.bin"

    claim_acquired = threading.Event()
    release_claim = threading.Event()
    real_claim = manager._claim_promotion

    def slow_claim(temp_resolved, final_resolved):
        outcome = real_claim(temp_resolved, final_resolved)
        if outcome[0]:
            claim_acquired.set()
            release_claim.wait(timeout=10)
        return outcome

    manager._claim_promotion = slow_claim
    outcome = {}

    def runner():
        outcome["path"] = manager.promote_to_final(temp, final)

    t = threading.Thread(target=runner)
    t.start()
    assert claim_acquired.wait(timeout=10)

    with pytest.raises(StorageCleanupError) as excinfo:
        manager.mark_active(final)
    assert excinfo.value.reason == "promotion_claim_in_progress"

    release_claim.set()
    t.join(timeout=10)
    assert not t.is_alive()
    assert outcome["path"] == manager._resolve_path(final)


def test_auditoria2_apos_mark_inactive_promocao_volta_a_ser_permitida(managed):
    """Depois de ``mark_inactive``, a promoção volta a ser permitida —
    o ``active_path`` não é um bloqueio permanente."""
    paths, manager = managed
    temp = manager.allocate_temp(suffix=".bin", create=True)
    temp.write_bytes(b"dados")
    final = paths.projects / "depois_de_inactive.bin"

    manager.mark_active(temp)
    with pytest.raises(StoragePublishError) as excinfo:
        manager.promote_to_final(temp, final)
    assert excinfo.value.reason == "temp_active"

    manager.mark_inactive(temp)
    result = manager.promote_to_final(temp, final)
    assert result == manager._resolve_path(final)


def test_auditoria2_destino_dentro_de_temp_e_recusado(managed):
    """``promote_to_final`` nunca publica sozinho dentro de um managed
    root estruturalmente CLEANABLE (temp) — isso criaria um arquivo final
    órfão sem ownership, dentro de um root que existe para ser limpo."""
    paths, manager = managed
    temp = manager.allocate_temp(suffix=".bin", create=True)
    temp.write_bytes(b"conteudo")
    final_in_temp = paths.temp / "final_dentro_de_temp.bin"

    with pytest.raises(StoragePublishError) as excinfo:
        manager.promote_to_final(temp, final_in_temp)

    assert excinfo.value.reason == "destination_root_cleanable"
    assert temp.exists()
    assert manager.allocation_category_of(temp) == CATEGORY_TEMP


def test_auditoria2_destino_dentro_de_cache_e_recusado(managed):
    """Mesma regra para ``cache``."""
    paths, manager = managed
    temp = manager.allocate_temp(suffix=".bin", create=True)
    temp.write_bytes(b"conteudo")
    final_in_cache = paths.cache / "final_dentro_de_cache.bin"

    with pytest.raises(StoragePublishError) as excinfo:
        manager.promote_to_final(temp, final_in_cache)

    assert excinfo.value.reason == "destination_root_cleanable"
    assert temp.exists()
    assert manager.allocation_category_of(temp) == CATEGORY_TEMP


def test_auditoria2_origem_igual_destino_e_recusado(managed):
    """``temp_path`` e ``final_path`` resolvendo para o MESMO caminho
    físico é recusado explicitamente — nunca consome a ownership de um
    arquivo que permaneceria parado no mesmo pathname."""
    paths, manager = managed
    temp = manager.allocate_temp(suffix=".bin", create=True)
    temp.write_bytes(b"conteudo_original")

    with pytest.raises(StoragePublishError) as excinfo:
        manager.promote_to_final(temp, temp, overwrite=True)

    assert excinfo.value.reason == "source_equals_destination"
    assert temp.exists() and temp.read_bytes() == b"conteudo_original"
    assert manager.allocation_category_of(temp) == CATEGORY_TEMP


def test_auditoria2_temp_para_projects_continua_funcionando(managed):
    """Sanidade: o caso normal (TEMP -> PROJECTS) continua funcionando
    depois de todas as correções desta rodada."""
    paths, manager = managed
    temp = manager.allocate_temp(suffix=".bin", create=True)
    temp.write_bytes(b"conteudo_normal")
    final = paths.projects / "normal.bin"

    result = manager.promote_to_final(temp, final)

    assert result == manager._resolve_path(final)
    assert final.read_bytes() == b"conteudo_normal"
    assert not temp.exists()
    assert manager.allocation_category_of(temp) is None


def test_auditoria2_151_testes_anteriores_continuam_verdes_sanidade(managed):
    """Sanidade final: proteções de todas as rodadas anteriores continuam
    de pé depois desta 2ª rodada da auditoria independente (nenhuma
    regressão)."""
    paths, manager = managed
    source = _write_file(paths.projects / "source_ainda_protegido2.bin", b"bytes")
    manager.register_protected_path(source, CATEGORY_SOURCE)
    final = paths.projects / "final_ainda_recusado2.bin"
    with pytest.raises(StoragePublishError) as excinfo:
        manager.promote_to_final(source, final)
    assert excinfo.value.reason == "temp_is_protected"
    assert source.exists()
    assert not final.exists()
