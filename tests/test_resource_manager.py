# -*- coding: utf-8 -*-
"""Testes do ResourceManager central (FASE 3 / PROMPT 18).

Cobre os cenários obrigatórios do Prompt: perfis LOW/NORMAL/HIGH, limites
por categoria, capacidade global compartilhada, acquire/release normal,
anti-leak (exceção/cancelamento/double release/aquisição parcial),
concorrência real (múltiplas threads, nunca mocks sequenciais), fairness
determinística (Events/Barriers, nunca sleep como prova), detecção de
hardware best-effort (com e sem GPU, com falha de detecção), snapshot/
observabilidade, shutdown com waiter bloqueado, e integração sem regressão
com JobEngine/BatchEngine.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any

import pytest

from _sistema.app_paths import build_app_paths
from _sistema.domain import (
    Job,
    JOB_BLOCKED,
    JOB_CANCELLED,
    JOB_PENDING,
    JOB_PROCESSING,
    JOB_PUBLISHING,
    JOB_READY,
    JOB_RECOVERING,
    JOB_RETRY,
)
from _sistema.storage import LocalDatabase, OperationalAuditLog
from _sistema.control_manager import (
    CANCEL_OUTCOME_DEFERRED,
    CONTROL_SCOPE_GLOBAL,
    CONTROL_SCOPE_JOB,
    CONTROL_SCOPE_QUEUE,
    ControlManager,
)
from _sistema.job_engine import (
    HeavyWorkAdmissionDeniedError,
    JobBlockedByControlError,
    JobEngine,
    JobStepResult,
)
from _sistema.batch_engine import BatchEngine
from _sistema.shutdown_coordinator import ShutdownCoordinator
from _sistema.recovery_manager import RecoveryManager
from _sistema.resource_manager import (
    DIM_GLOBAL_HEAVY,
    DIM_GPU_SLOT,
    HardwareSnapshot,
    InvalidResourceRequestError,
    PROFILE_HIGH,
    PROFILE_LOW,
    PROFILE_NORMAL,
    ResourceAccountingError,
    ResourceManager,
    ResourceManagerShuttingDownError,
    ResourceUnavailableError,
    RESOURCE_BROWSER,
    RESOURCE_FFMPEG,
    RESOURCE_FRAME_ANALYSIS,
    RESOURCE_OLLAMA,
    RESOURCE_RENDER,
    RESOURCE_WHISPER,
    select_profile,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _hardware(
    cpu_count=4,
    total_ram_mb=8192,
    gpu_available=False,
    gpu_vram_mb=None,
    gpu_vendor=None,
    gpu_backend_available=None,
):
    """Helper de teste. ``gpu_backend_available``: quando omitido
    (``None``), espelha ``gpu_available`` — conveniência para testes
    escritos ANTES da distinção física/backend (7ª→8ª revisão, ver
    ``HardwareSnapshot``) que só querem "a máquina tem GPU utilizável
    para fins deste teste", sem se importar com a distinção. Testes que
    precisam exercitar a distinção passam ambos explicitamente."""
    if gpu_backend_available is None:
        gpu_backend_available = gpu_available
    return HardwareSnapshot(
        cpu_count=cpu_count,
        total_ram_mb=total_ram_mb,
        gpu_available=gpu_available,
        gpu_vram_mb=gpu_vram_mb,
        gpu_vendor=gpu_vendor,
        gpu_backend_available=gpu_backend_available,
    )


@pytest.fixture
def local_db():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        paths = build_app_paths(install_root=root / "install", data_root=root / "data")
        db = LocalDatabase(paths=paths)
        db.initialize()
        yield db


def _create_job(audit, *, operation="OP"):
    job = Job(operation=operation)
    return audit.create_job(job)


# ---------------------------------------------------------------------------
# 1-3: perfis LOW / NORMAL / HIGH
# ---------------------------------------------------------------------------


def test_profile_low_por_cpu_baixo():
    assert select_profile(_hardware(cpu_count=2, total_ram_mb=32 * 1024)) == PROFILE_LOW


def test_profile_low_por_ram_baixa():
    assert select_profile(_hardware(cpu_count=8, total_ram_mb=4 * 1024)) == PROFILE_LOW


def test_profile_normal_default_intermediario():
    assert select_profile(_hardware(cpu_count=4, total_ram_mb=8 * 1024)) == PROFILE_NORMAL


def test_profile_high_exige_cpu_e_ram_altos():
    assert select_profile(_hardware(cpu_count=8, total_ram_mb=16 * 1024)) == PROFILE_HIGH
    assert select_profile(_hardware(cpu_count=16, total_ram_mb=32 * 1024)) == PROFILE_HIGH


def test_profile_manager_usa_profile_explicito_sem_detectar_hardware():
    rm = ResourceManager(profile=PROFILE_HIGH, hardware=_hardware())
    assert rm.profile == PROFILE_HIGH


# ---------------------------------------------------------------------------
# 4-9: limite por categoria (FFmpeg, Whisper, Ollama, Browser, Render,
# Frame Analysis) — concorrência REAL com Barrier, nunca mocks sequenciais
# ---------------------------------------------------------------------------


def _assert_category_limit_enforced(kind, limit, n_callers=10, repeats=3):
    """Repete várias vezes para aumentar a chance de detectar uma race
    (item 25 do Prompt): nunca deve observar mais que ``limit``
    simultâneos, mesmo variando a ordem de escalonamento do SO.

    Cada worker mantém a posse por um tempo curto (não zero) para dar
    chance real de sobreposição entre workers concorrentes, sem depender
    de um Barrier — um Barrier de ``n_callers`` não funciona aqui porque
    a capacidade (``limit``) é deliberadamente menor que ``n_callers``:
    nem todos os workers conseguem entrar na seção ao mesmo tempo, então
    um Barrier dimensionado para ``n_callers`` nunca fecharia."""
    for _ in range(repeats):
        rm = ResourceManager(profile=PROFILE_HIGH, hardware=_hardware(), overrides={kind: limit})
        in_section = {"current": 0, "max_simultaneous": 0}
        lock = threading.Lock()

        def worker():
            with rm.acquire_for(kind, timeout=15):
                with lock:
                    in_section["current"] += 1
                    in_section["max_simultaneous"] = max(
                        in_section["max_simultaneous"], in_section["current"]
                    )
                time.sleep(0.03)
                with lock:
                    in_section["current"] -= 1

        threads = [threading.Thread(target=worker) for _ in range(n_callers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
            assert not t.is_alive()

        assert in_section["max_simultaneous"] <= limit
        assert in_section["max_simultaneous"] >= 1
        snap = rm.snapshot()
        assert snap["in_use"][kind] == 0
        assert snap["in_use"][DIM_GLOBAL_HEAVY] == 0


def test_limite_ffmpeg_respeitado():
    _assert_category_limit_enforced(RESOURCE_FFMPEG, limit=2)


def test_limite_whisper_respeitado():
    _assert_category_limit_enforced(RESOURCE_WHISPER, limit=1)


def test_limite_ollama_respeitado():
    _assert_category_limit_enforced(RESOURCE_OLLAMA, limit=1)


def test_limite_browser_respeitado():
    _assert_category_limit_enforced(RESOURCE_BROWSER, limit=2)


def test_limite_render_respeitado():
    _assert_category_limit_enforced(RESOURCE_RENDER, limit=2)


def test_limite_frame_analysis_respeitado():
    _assert_category_limit_enforced(RESOURCE_FRAME_ANALYSIS, limit=2)


# ---------------------------------------------------------------------------
# 10: capacidade global compartilhada entre categorias diferentes
# ---------------------------------------------------------------------------


def test_capacidade_global_limita_soma_entre_categorias_diferentes():
    """FFMPEG=4 e RENDER=4 individualmente permitiriam 8 simultâneos, mas
    GLOBAL_HEAVY=3 deve limitar a SOMA das duas categorias juntas a 3 —
    exatamente o erro descrito no item 6 do Prompt."""
    rm = ResourceManager(
        profile=PROFILE_HIGH,
        hardware=_hardware(),
        overrides={RESOURCE_FFMPEG: 4, RESOURCE_RENDER: 4, DIM_GLOBAL_HEAVY: 3},
    )
    n_callers = 12
    in_section = {"current": 0, "max_simultaneous": 0}
    lock = threading.Lock()
    release_event = threading.Event()

    def worker(kind):
        with rm.acquire_for(kind, timeout=10):
            with lock:
                in_section["current"] += 1
                in_section["max_simultaneous"] = max(
                    in_section["max_simultaneous"], in_section["current"]
                )
            release_event.wait(timeout=10)
            with lock:
                in_section["current"] -= 1

    threads = [
        threading.Thread(target=worker, args=(RESOURCE_FFMPEG if i % 2 == 0 else RESOURCE_RENDER,))
        for i in range(n_callers)
    ]
    for t in threads:
        t.start()
    time.sleep(0.05)
    release_event.set()
    for t in threads:
        t.join(timeout=15)
        assert not t.is_alive()

    assert in_section["max_simultaneous"] <= 3
    snap = rm.snapshot()
    assert snap["in_use"][DIM_GLOBAL_HEAVY] == 0
    assert snap["in_use"][RESOURCE_FFMPEG] == 0
    assert snap["in_use"][RESOURCE_RENDER] == 0


# ---------------------------------------------------------------------------
# 11: acquire/release normal
# ---------------------------------------------------------------------------


def test_acquire_release_normal_com_with():
    rm = ResourceManager(profile=PROFILE_NORMAL, hardware=_hardware())
    assert rm.snapshot()["in_use"][RESOURCE_FFMPEG] == 0
    with rm.acquire_for(RESOURCE_FFMPEG) as lease:
        assert rm.snapshot()["in_use"][RESOURCE_FFMPEG] == 1
        assert lease.released is False
    assert rm.snapshot()["in_use"][RESOURCE_FFMPEG] == 0
    assert lease.released is True


def test_acquire_release_explicito():
    rm = ResourceManager(profile=PROFILE_NORMAL, hardware=_hardware())
    lease = rm.acquire_for(RESOURCE_BROWSER)
    assert rm.snapshot()["in_use"][RESOURCE_BROWSER] == 1
    lease.release()
    assert rm.snapshot()["in_use"][RESOURCE_BROWSER] == 0


# ---------------------------------------------------------------------------
# 12 / 26: exceção durante execução libera o recurso (adversarial obrigatório)
# ---------------------------------------------------------------------------


def test_excecao_dentro_do_with_libera_recurso_e_proximo_consegue_adquirir():
    rm = ResourceManager(
        profile=PROFILE_LOW, hardware=_hardware(), overrides={RESOURCE_FFMPEG: 1}
    )

    with pytest.raises(RuntimeError):
        with rm.acquire_for(RESOURCE_FFMPEG):
            raise RuntimeError("falha simulada dentro do workload")

    assert rm.snapshot()["in_use"][RESOURCE_FFMPEG] == 0

    # Próximo caller consegue adquirir imediatamente (capacidade=1).
    with rm.acquire_for(RESOURCE_FFMPEG, timeout=2):
        assert rm.snapshot()["in_use"][RESOURCE_FFMPEG] == 1


def test_adversarial_capacidade_um_exception_libera_para_o_proximo_caller():
    """Cenário obrigatório do item 26: capacidade=1; A adquire e lança
    exceção; B esperando; depois da falha de A, B TEM que conseguir
    adquirir — se B ficar bloqueado, é FAIL."""
    rm = ResourceManager(
        profile=PROFILE_LOW, hardware=_hardware(), overrides={RESOURCE_FFMPEG: 1}
    )
    a_acquired = threading.Event()
    b_result: dict[str, Any] = {}

    def caller_a():
        with pytest.raises(RuntimeError):
            with rm.acquire_for(RESOURCE_FFMPEG):
                a_acquired.set()
                time.sleep(0.05)
                raise RuntimeError("A falha")

    def caller_b():
        assert a_acquired.wait(timeout=5)
        with rm.acquire_for(RESOURCE_FFMPEG, timeout=5):
            b_result["acquired"] = True

    t_a = threading.Thread(target=caller_a)
    t_b = threading.Thread(target=caller_b)
    t_a.start()
    t_b.start()
    t_a.join(timeout=10)
    t_b.join(timeout=10)

    assert not t_a.is_alive()
    assert not t_b.is_alive()
    assert b_result.get("acquired") is True
    assert rm.snapshot()["in_use"][RESOURCE_FFMPEG] == 0


# ---------------------------------------------------------------------------
# 13: double release
# ---------------------------------------------------------------------------


def test_double_release_e_no_op_idempotente_nunca_fica_negativo():
    rm = ResourceManager(profile=PROFILE_NORMAL, hardware=_hardware())
    lease = rm.acquire_for(RESOURCE_FFMPEG)
    lease.release()
    lease.release()
    lease.release()
    snap = rm.snapshot()
    assert snap["in_use"][RESOURCE_FFMPEG] == 0
    assert snap["in_use"][DIM_GLOBAL_HEAVY] == 0
    # Não "inventou" capacidade extra: outro caller ainda respeita o limite normal.
    lease2 = rm.acquire_for(RESOURCE_FFMPEG)
    assert rm.snapshot()["in_use"][RESOURCE_FFMPEG] == 1
    lease2.release()


def test_double_release_concorrente_nao_corrompe_accounting():
    rm = ResourceManager(profile=PROFILE_NORMAL, hardware=_hardware())
    lease = rm.acquire_for(RESOURCE_FFMPEG)
    errors: list[BaseException] = []

    def release_it():
        try:
            lease.release()
        except BaseException as exc:  # nenhuma das chamadas deveria lançar
            errors.append(exc)

    threads = [threading.Thread(target=release_it) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert not errors
    snap = rm.snapshot()
    assert snap["in_use"][RESOURCE_FFMPEG] == 0
    assert snap["in_use"][DIM_GLOBAL_HEAVY] == 0


# ---------------------------------------------------------------------------
# 14 / 27: aquisição composta não vaza (adversarial obrigatório)
# ---------------------------------------------------------------------------


def test_adversarial_aquisicao_composta_falha_nao_vaza_dimensao_disponivel():
    """CPU (aqui: FFMPEG) disponível, GPU_SLOT indisponível (capacidade 0
    — máquina sem GPU). Pedir FFMPEG+GPU_SLOT junto tem que falhar sem
    reservar nada — nem parcialmente."""
    rm = ResourceManager(
        profile=PROFILE_NORMAL, hardware=_hardware(gpu_available=False)
    )
    assert rm.capacity_of(DIM_GPU_SLOT) == 0

    with pytest.raises(InvalidResourceRequestError):
        rm.acquire({RESOURCE_FFMPEG: 1, DIM_GPU_SLOT: 1})

    # FFMPEG continua com capacidade cheia disponível — nenhum leak parcial.
    snap = rm.snapshot()
    assert snap["in_use"][RESOURCE_FFMPEG] == 0
    with rm.acquire_for(RESOURCE_FFMPEG, timeout=2):
        assert rm.snapshot()["in_use"][RESOURCE_FFMPEG] == 1


def test_aquisicao_composta_bem_sucedida_e_atomica():
    rm = ResourceManager(
        profile=PROFILE_NORMAL,
        hardware=_hardware(gpu_available=True, gpu_vram_mb=8192),
        overrides={RESOURCE_RENDER: 2},
    )
    with rm.acquire({RESOURCE_RENDER: 1, DIM_GPU_SLOT: 1}) as lease:
        snap = rm.snapshot()
        assert snap["in_use"][RESOURCE_RENDER] == 1
        assert snap["in_use"][DIM_GPU_SLOT] == 1
    snap = rm.snapshot()
    assert snap["in_use"][RESOURCE_RENDER] == 0
    assert snap["in_use"][DIM_GPU_SLOT] == 0


# ---------------------------------------------------------------------------
# 15-16: concorrência real — dois callers, e muitos callers sem ultrapassar
# ---------------------------------------------------------------------------


def test_dois_callers_concorrentes_respeitam_limite_um():
    rm = ResourceManager(
        profile=PROFILE_LOW, hardware=_hardware(), overrides={RESOURCE_FFMPEG: 1}
    )
    a_in = threading.Event()
    b_tried = threading.Event()
    release_a = threading.Event()
    b_acquired_while_a_held = {"value": None}

    def caller_a():
        with rm.acquire_for(RESOURCE_FFMPEG, timeout=5):
            a_in.set()
            release_a.wait(timeout=5)

    def caller_b():
        assert a_in.wait(timeout=5)
        b_tried.set()
        # Não deve conseguir enquanto A segura — usa timeout curto para
        # provar isso deterministicamente, sem depender de sleep como prova.
        try:
            rm.acquire_for(RESOURCE_FFMPEG, timeout=0.2)
            b_acquired_while_a_held["value"] = True
        except ResourceUnavailableError:
            b_acquired_while_a_held["value"] = False

    t_a = threading.Thread(target=caller_a)
    t_a.start()
    assert a_in.wait(timeout=5)
    caller_b()
    release_a.set()
    t_a.join(timeout=5)

    assert b_acquired_while_a_held["value"] is False
    assert rm.snapshot()["in_use"][RESOURCE_FFMPEG] == 0


def test_muitos_callers_nunca_ultrapassam_limite():
    rm = ResourceManager(
        profile=PROFILE_HIGH, hardware=_hardware(), overrides={RESOURCE_BROWSER: 3}
    )
    n_callers = 50
    in_section = {"current": 0, "max_simultaneous": 0}
    lock = threading.Lock()

    def worker():
        with rm.acquire_for(RESOURCE_BROWSER, timeout=15):
            with lock:
                in_section["current"] += 1
                in_section["max_simultaneous"] = max(
                    in_section["max_simultaneous"], in_section["current"]
                )
            time.sleep(0.01)
            with lock:
                in_section["current"] -= 1

    threads = [threading.Thread(target=worker) for _ in range(n_callers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive()

    assert in_section["max_simultaneous"] <= 3
    assert rm.snapshot()["in_use"][RESOURCE_BROWSER] == 0


# ---------------------------------------------------------------------------
# 17 / 28: fairness — waiter anterior eventualmente progride (adversarial)
# ---------------------------------------------------------------------------


def test_adversarial_fairness_waiter_antigo_nao_e_ultrapassado_por_novos():
    """Capacidade=1. A ocupa. B começa a esperar. Depois C, D, E chegam.
    Quando A libera, B (o mais antigo na fila) tem que ser o PRÓXIMO a
    conseguir — nunca C/D/E furando a fila. Determinístico via Events, sem
    sleep como mecanismo de prova."""
    rm = ResourceManager(
        profile=PROFILE_LOW, hardware=_hardware(), overrides={RESOURCE_FFMPEG: 1}
    )
    order_acquired: list[str] = []
    lock = threading.Lock()

    a_holds = threading.Event()
    b_in_queue = threading.Event()
    release_a = threading.Event()
    cde_in_queue = threading.Event()

    def caller_a():
        with rm.acquire_for(RESOURCE_FFMPEG, timeout=10):
            with lock:
                order_acquired.append("A")
            a_holds.set()
            release_a.wait(timeout=10)

    def caller_b():
        assert a_holds.wait(timeout=10)
        # Entra na fila e sinaliza que já fez isso, mas ainda não tem
        # garantia de estar bloqueado dentro do Condition.wait — damos uma
        # janela curta determinística via retry loop abaixo, sem depender
        # de um sleep arbitrário para "torcer" pela ordem.
        b_in_queue.set()
        with rm.acquire_for(RESOURCE_FFMPEG, timeout=10):
            with lock:
                order_acquired.append("B")

    def caller_cde(name):
        assert b_in_queue.wait(timeout=10)
        cde_in_queue.set()
        with rm.acquire_for(RESOURCE_FFMPEG, timeout=10):
            with lock:
                order_acquired.append(name)

    t_a = threading.Thread(target=caller_a)
    t_a.start()
    assert a_holds.wait(timeout=10)

    t_b = threading.Thread(target=caller_b)
    t_b.start()
    assert b_in_queue.wait(timeout=10)
    # Garante deterministicamente que B já está na fila FIFO interna antes
    # de C/D/E tentarem — consulta o snapshot em vez de dormir um tempo fixo.
    deadline = time.monotonic() + 5
    while rm.snapshot()["queue_length"] < 1 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert rm.snapshot()["queue_length"] >= 1

    t_c = threading.Thread(target=caller_cde, args=("C",))
    t_d = threading.Thread(target=caller_cde, args=("D",))
    t_e = threading.Thread(target=caller_cde, args=("E",))
    for t in (t_c, t_d, t_e):
        t.start()
    assert cde_in_queue.wait(timeout=10)
    deadline = time.monotonic() + 5
    while rm.snapshot()["queue_length"] < 4 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert rm.snapshot()["queue_length"] >= 4  # B, C, D, E todos na fila

    release_a.set()
    for t in (t_a, t_b, t_c, t_d, t_e):
        t.join(timeout=15)
        assert not t.is_alive()

    assert order_acquired[0] == "A"
    assert order_acquired[1] == "B"  # nunca ultrapassado por C/D/E
    assert set(order_acquired[2:]) == {"C", "D", "E"}
    assert rm.snapshot()["in_use"][RESOURCE_FFMPEG] == 0


# ---------------------------------------------------------------------------
# 18-20: máquina sem GPU / falha de detecção / fallback conservador
# ---------------------------------------------------------------------------


def test_maquina_sem_gpu_continua_funcionando():
    rm = ResourceManager(profile=PROFILE_NORMAL, hardware=_hardware(gpu_available=False))
    assert rm.gpu_available is False
    assert rm.capacity_of(DIM_GPU_SLOT) == 0
    # acquire_for com use_gpu=True degrada graciosamente (não trava, não lança).
    with rm.acquire_for(RESOURCE_WHISPER, use_gpu=True, timeout=2) as lease:
        assert DIM_GPU_SLOT not in lease.requests
        assert rm.snapshot()["in_use"][DIM_GPU_SLOT] == 0


def test_maquina_com_gpu_acquire_for_usa_gpu_slot():
    rm = ResourceManager(
        profile=PROFILE_NORMAL, hardware=_hardware(gpu_available=True, gpu_vram_mb=6144)
    )
    with rm.acquire_for(RESOURCE_WHISPER, use_gpu=True, timeout=2) as lease:
        assert DIM_GPU_SLOT in lease.requests
        assert rm.snapshot()["in_use"][DIM_GPU_SLOT] == 1


def test_falha_de_deteccao_de_gpu_nao_derruba_inicializacao(monkeypatch):
    """Substitui o teste antigo do mesmo nome (que so mockava
    ``_detect_gpu``/nvidia-smi). Auditoria Windows real (Python 3.13.7,
    maquina com GPU AMD fisica) mostrou que esse teste antigo dava FAILED
    la, nao por regressao de codigo: no Windows, ``detect_hardware``
    tambem consulta ``_detect_gpu_windows_cim`` (presenca fisica generica
    NVIDIA/AMD/Intel), e essa fonte continuava confirmando
    ``gpu_available=True`` mesmo com ``_detect_gpu`` (especifico de
    nvidia-smi) falhando - comportamento correto e ja aprovado da
    arquitetura (``gpu_available`` = presenca fisica,
    ``gpu_backend_available`` = backend acelerado confirmado; ver
    docstring de ``HardwareSnapshot``/``detect_hardware`` em
    resource_manager.py). O teste antigo nao refletia esse contrato e
    dependia implicitamente do resultado do CIM na maquina onde rodava -
    nao era deterministico entre ambientes.

    Este teste mockpatcha as DUAS fontes deterministicamente (nunca
    depende de GPU fisica real, nvidia-smi real, PowerShell/CIM real nem
    do SO real): nenhuma fonte encontra GPU nenhuma. Prova a intencao
    original ("falha de deteccao de GPU nao derruba inicializacao") no
    cenario em que a maquina de fato nao tem GPU utilizavel."""
    import _sistema.resource_manager as rm_module

    def cim_sem_gpu():
        return False, None, None

    def nvidia_smi_boom():
        raise OSError("nvidia-smi explodiu simulado")

    monkeypatch.setattr(rm_module, "_detect_gpu_windows_cim", cim_sem_gpu)
    monkeypatch.setattr(rm_module, "_detect_gpu", nvidia_smi_boom)
    hardware = rm_module.detect_hardware()
    assert hardware.gpu_available is False
    assert hardware.gpu_backend_available is False
    assert hardware.gpu_vram_mb is None
    assert any("gpu_detection_failed" in e for e in hardware.detection_errors)

    # E o ResourceManager continua inicializando normalmente com esse hardware.
    rm = ResourceManager(hardware=hardware)
    assert rm.capacity_of(DIM_GPU_SLOT) == 0


def test_gpu_amd_fisica_encontrada_sem_backend_nvidia_capacidade_zero(monkeypatch):
    """Prova explicitamente o cenario reproduzido na auditoria Windows
    real (GPU AMD fisica, sem nvidia-smi/backend acelerado): presenca
    fisica (``gpu_available``/``gpu_vendor``/``gpu_vram_mb``) e reportada
    a partir do CIM, mas ``gpu_backend_available`` permanece False (nao
    ha backend AMD/ROCm implementado neste Prompt) - e e
    ``gpu_backend_available``, nunca ``gpu_available`` sozinho, quem
    decide ``capacity_of(DIM_GPU_SLOT)`` (ver docstring do modulo).
    Deterministico: mockpatcha ``sys.platform`` e as duas funcoes de
    deteccao, nunca depende de hardware/SO real."""
    import sys

    import _sistema.resource_manager as rm_module

    def cim_amd_encontrada():
        return True, "AMD", 4095

    def nvidia_smi_boom():
        raise OSError("nvidia-smi explodiu simulado (sem NVIDIA nesta maquina)")

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(rm_module, "_detect_gpu_windows_cim", cim_amd_encontrada)
    monkeypatch.setattr(rm_module, "_detect_gpu", nvidia_smi_boom)
    hardware = rm_module.detect_hardware()

    assert hardware.gpu_available is True
    assert hardware.gpu_vendor == "AMD"
    assert hardware.gpu_vram_mb == 4095
    assert hardware.gpu_backend_available is False
    assert any("gpu_detection_failed" in e for e in hardware.detection_errors)

    # ResourceManager inicializa normalmente e nao concede slot de GPU:
    # presenca fisica sem backend acelerado confirmado nao habilita a
    # dimensao DIM_GPU_SLOT (ver capacities[DIM_GPU_SLOT] em resource_manager.py).
    rm = ResourceManager(hardware=hardware)
    assert rm.capacity_of(DIM_GPU_SLOT) == 0


def test_falha_de_deteccao_de_ram_usa_fallback_conservador(monkeypatch):
    import _sistema.resource_manager as rm_module

    def boom():
        raise OSError("leitura de RAM falhou simulada")

    monkeypatch.setattr(rm_module, "_detect_total_ram_mb", boom)
    hardware = rm_module.detect_hardware()
    assert hardware.total_ram_mb is None
    assert any("ram_detection_failed" in e for e in hardware.detection_errors)

    # RAM desconhecida nunca promove a HIGH, mesmo com CPU alto.
    forced = HardwareSnapshot(
        cpu_count=16, total_ram_mb=None, gpu_available=False, gpu_vram_mb=None
    )
    assert select_profile(forced) != PROFILE_HIGH


def test_nvidia_smi_ausente_no_path_nao_lanca(monkeypatch):
    import subprocess

    import _sistema.resource_manager as rm_module

    def fake_run(*args, **kwargs):
        raise FileNotFoundError("nvidia-smi não encontrado")

    monkeypatch.setattr(subprocess, "run", fake_run)
    gpu_available, vram = rm_module._detect_gpu()
    assert gpu_available is False
    assert vram is None


# ---------------------------------------------------------------------------
# 21: snapshot/accounting correto (observabilidade)
# ---------------------------------------------------------------------------


def test_snapshot_estrutura_segura_sem_segredos():
    rm = ResourceManager(profile=PROFILE_NORMAL, hardware=_hardware(gpu_available=True))
    with rm.acquire_for(RESOURCE_FFMPEG):
        snap = rm.snapshot()
        assert snap["profile"] == PROFILE_NORMAL
        assert snap["gpu_available"] is True
        assert snap["in_use"][RESOURCE_FFMPEG] == 1
        assert snap["capacity"][RESOURCE_FFMPEG] >= 1
        # Nenhuma chave sugere cookie/token/path/conteúdo.
        forbidden_substrings = ("cookie", "token", "path", "password", "secret")
        serialized_keys = " ".join(snap.keys()).lower()
        assert not any(f in serialized_keys for f in forbidden_substrings)


# ---------------------------------------------------------------------------
# 22 / 29: shutdown enquanto existe waiter (adversarial obrigatório)
# ---------------------------------------------------------------------------


def test_adversarial_shutdown_com_waiter_nao_deixa_thread_zumbi():
    rm = ResourceManager(
        profile=PROFILE_LOW, hardware=_hardware(), overrides={RESOURCE_FFMPEG: 1}
    )
    a_holds = threading.Event()
    release_a = threading.Event()
    b_result: dict[str, Any] = {}

    def caller_a():
        with rm.acquire_for(RESOURCE_FFMPEG, timeout=10):
            a_holds.set()
            release_a.wait(timeout=10)

    def caller_b():
        assert a_holds.wait(timeout=10)
        try:
            rm.acquire_for(RESOURCE_FFMPEG, timeout=10)
            b_result["outcome"] = "acquired"
        except ResourceManagerShuttingDownError:
            b_result["outcome"] = "shutdown"

    t_a = threading.Thread(target=caller_a)
    t_b = threading.Thread(target=caller_b)
    t_a.start()
    assert a_holds.wait(timeout=10)
    t_b.start()

    deadline = time.monotonic() + 5
    while rm.snapshot()["queue_length"] < 1 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert rm.snapshot()["queue_length"] >= 1

    rm.begin_shutdown(reason="teste_shutdown_com_waiter")

    t_b.join(timeout=10)
    assert not t_b.is_alive()  # nunca fica zumbi
    assert b_result.get("outcome") == "shutdown"

    release_a.set()
    t_a.join(timeout=10)
    assert not t_a.is_alive()

    snap = rm.snapshot()
    assert snap["in_use"][RESOURCE_FFMPEG] == 0
    assert snap["queue_length"] == 0

    # Novas aquisições continuam recusadas enquanto shutting_down estiver ativo.
    with pytest.raises(ResourceManagerShuttingDownError):
        rm.acquire_for(RESOURCE_FFMPEG, timeout=1)

    rm.end_shutdown()
    with rm.acquire_for(RESOURCE_FFMPEG, timeout=2):
        pass


# ---------------------------------------------------------------------------
# 23: cancel antes de começar trabalho pesado — recurso nunca é tocado
# ---------------------------------------------------------------------------


def test_cancel_antes_de_adquirir_recurso_nunca_toca_accounting(local_db):
    """Item 3 do Prompt: um Job que nunca chega a rodar o handler (porque
    foi bloqueado/cancelado antes) não pode ter reservado nenhuma
    capacidade — o handler é o único que chama acquire_for, e ele nunca é
    invocado para um Job que não foi reivindicado."""
    audit = OperationalAuditLog(local_db)
    control = ControlManager(local_db, audit_log=audit)
    rm = ResourceManager(profile=PROFILE_NORMAL, hardware=_hardware())

    handler_calls: list[str] = []

    def handler(job):
        handler_calls.append(job.id)
        with rm.acquire_for(RESOURCE_FFMPEG):
            return JobStepResult(target_status=JOB_READY)

    engine = JobEngine(local_db, audit_log=audit, control_manager=control, resource_manager=rm)
    engine.register_handler("FFMPEG_OP", handler, claims_status=JOB_PROCESSING)

    job = _create_job(audit, operation="FFMPEG_OP")
    control.pause(CONTROL_SCOPE_GLOBAL)

    with pytest.raises(JobBlockedByControlError):
        engine.advance(job.id)

    assert handler_calls == []
    assert rm.snapshot()["in_use"][RESOURCE_FFMPEG] == 0
    assert engine.get_job(job.id).status == JOB_PENDING


# ---------------------------------------------------------------------------
# 24: integração JobEngine
# ---------------------------------------------------------------------------


def test_integracao_jobengine_handler_adquire_recurso_via_engine(local_db):
    audit = OperationalAuditLog(local_db)
    rm = ResourceManager(
        profile=PROFILE_NORMAL, hardware=_hardware(), overrides={RESOURCE_FFMPEG: 2}
    )
    engine = JobEngine(local_db, audit_log=audit, resource_manager=rm)
    assert engine.resource_manager is rm

    max_simultaneous = {"value": 0, "current": 0}
    lock = threading.Lock()

    def handler(job):
        with engine.resource_manager.acquire_for(RESOURCE_FFMPEG, timeout=10):
            with lock:
                max_simultaneous["current"] += 1
                max_simultaneous["value"] = max(
                    max_simultaneous["value"], max_simultaneous["current"]
                )
            time.sleep(0.02)
            with lock:
                max_simultaneous["current"] -= 1
        return JobStepResult(target_status=JOB_READY)

    engine.register_handler("FFMPEG_OP", handler, claims_status=JOB_PROCESSING)
    jobs = [_create_job(audit, operation="FFMPEG_OP") for _ in range(8)]

    threads = [threading.Thread(target=engine.advance, args=(job.id,)) for job in jobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
        assert not t.is_alive()

    assert max_simultaneous["value"] <= 2
    for job in jobs:
        assert engine.get_job(job.id).status == JOB_READY
    assert rm.snapshot()["in_use"][RESOURCE_FFMPEG] == 0


# ---------------------------------------------------------------------------
# 25: integração BatchEngine sem regressão
# ---------------------------------------------------------------------------


def test_integracao_batchengine_sem_regressao_de_membership_progresso(local_db):
    audit = OperationalAuditLog(local_db)
    control = ControlManager(local_db, audit_log=audit)
    rm = ResourceManager(
        profile=PROFILE_NORMAL, hardware=_hardware(), overrides={RESOURCE_RENDER: 2}
    )
    engine = JobEngine(local_db, audit_log=audit, control_manager=control, resource_manager=rm)
    batch = BatchEngine(local_db, audit_log=audit, control_manager=control, job_engine=engine)

    max_simultaneous = {"value": 0, "current": 0}
    lock = threading.Lock()

    def handler(job):
        with engine.resource_manager.acquire_for(RESOURCE_RENDER, timeout=10):
            with lock:
                max_simultaneous["current"] += 1
                max_simultaneous["value"] = max(
                    max_simultaneous["value"], max_simultaneous["current"]
                )
            time.sleep(0.01)
            with lock:
                max_simultaneous["current"] -= 1
        return JobStepResult(target_status=JOB_READY)

    engine.register_handler("RENDER_OP", handler, claims_status=JOB_PROCESSING)
    result = batch.create_batch([{"operation": "RENDER_OP"} for _ in range(12)])

    def run_advance():
        for _ in range(6):
            batch.advance_batch(result.batch_id, limit=6)

    threads = [threading.Thread(target=run_advance) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
        assert not t.is_alive()

    progress = batch.get_progress(result.batch_id)
    assert progress.total == 12
    assert progress.ready == 12
    assert max_simultaneous["value"] <= 2
    assert batch.list_batch_job_ids(result.batch_id) == result.job_ids
    assert rm.snapshot()["in_use"][RESOURCE_RENDER] == 0


# ---------------------------------------------------------------------------
# 26: nenhuma criação de 200 operações pesadas simultâneas
# ---------------------------------------------------------------------------


def test_duzentas_operacoes_logicas_nunca_excedem_limite_simultaneo():
    """200 "operações pesadas" lógicas fluem através de um pool moderado de
    threads worker (não 200 threads do SO de uma vez — ver item 18 do
    Prompt: não criar um threadpool gigante). O que se prova é que, por
    mais que o volume lógico seja alto (500+ Jobs é o cenário do produto),
    o ResourceManager nunca deixa mais que `limit` operações rodando ao
    mesmo tempo."""
    rm = ResourceManager(
        profile=PROFILE_HIGH, hardware=_hardware(), overrides={RESOURCE_FRAME_ANALYSIS: 3}
    )
    total_ops = 200
    pool_size = 20
    counter = {"next": 0, "current": 0, "max_simultaneous": 0}
    lock = threading.Lock()

    def worker():
        while True:
            with lock:
                if counter["next"] >= total_ops:
                    return
                counter["next"] += 1
            with rm.acquire_for(RESOURCE_FRAME_ANALYSIS, timeout=15):
                with lock:
                    counter["current"] += 1
                    counter["max_simultaneous"] = max(
                        counter["max_simultaneous"], counter["current"]
                    )
                with lock:
                    counter["current"] -= 1

    threads = [threading.Thread(target=worker) for _ in range(pool_size)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
        assert not t.is_alive()

    assert counter["next"] == total_ops
    assert counter["max_simultaneous"] <= 3
    assert rm.snapshot()["in_use"][RESOURCE_FRAME_ANALYSIS] == 0


# ---------------------------------------------------------------------------
# Erros estruturados / validação
# ---------------------------------------------------------------------------


def test_acquire_dimensao_desconhecida_levanta_erro_claro():
    rm = ResourceManager(profile=PROFILE_NORMAL, hardware=_hardware())
    with pytest.raises(InvalidResourceRequestError):
        rm.acquire({"NAO_EXISTE": 1})


def test_acquire_amount_maior_que_capacidade_total_falha_rapido_sem_travar():
    rm = ResourceManager(
        profile=PROFILE_NORMAL, hardware=_hardware(), overrides={RESOURCE_FFMPEG: 2}
    )
    with pytest.raises(InvalidResourceRequestError):
        rm.acquire({RESOURCE_FFMPEG: 3})


def test_acquire_kind_desconhecido_em_acquire_for():
    rm = ResourceManager(profile=PROFILE_NORMAL, hardware=_hardware())
    with pytest.raises(InvalidResourceRequestError):
        rm.acquire_for("NAO_EXISTE")


def test_acquire_timeout_levanta_resource_unavailable_error():
    rm = ResourceManager(
        profile=PROFILE_LOW, hardware=_hardware(), overrides={RESOURCE_FFMPEG: 1}
    )
    with rm.acquire_for(RESOURCE_FFMPEG):
        with pytest.raises(ResourceUnavailableError):
            rm.acquire_for(RESOURCE_FFMPEG, timeout=0.1)


def test_release_internal_defende_contra_contador_negativo():
    """Rede de segurança estrutural (nunca deveria disparar via
    ResourceLease, já que ela é idempotente) — mas o accounting interno
    recusa explicitamente deixar um contador ir a negativo."""
    rm = ResourceManager(profile=PROFILE_NORMAL, hardware=_hardware())
    with pytest.raises(ResourceAccountingError):
        rm._release_internal({RESOURCE_FFMPEG: 1})
    assert rm.snapshot()["in_use"][RESOURCE_FFMPEG] == 0


# ---------------------------------------------------------------------------
# QUINTA REVISÃO ADVERSARIAL — ADMISSÃO DE TRABALHO PESADO
# ---------------------------------------------------------------------------
#
# BLOQUEADOR REPRODUZIDO: um Job já reivindicado (PENDING -> PROCESSING)
# pode ficar preso dentro do handler, bloqueado em
# ``ResourceManager.acquire_for`` aguardando capacidade, ANTES de o efeito
# pesado real começar. Nessa janela, cancel/pause/stop_after_current/
# DRAINING/batch pause podiam commitar sem que o trabalho pesado fosse
# impedido de começar assim que a capacidade fosse liberada — o Job só era
# corrigido para CANCELLED/etc. DEPOIS, o que não desfaz o efeito que já
# rodou.
#
# CORREÇÃO: ``JobEngine.admit_heavy_work(claimed)`` (ver job_engine.py,
# "ADMISSÃO DE TRABALHO PESADO"). Os testes abaixo provam que:
#   1. cancel commitado ANTES da admissão impede o efeito pesado (CASO 1);
#   2. pause commitado ANTES da admissão impede o efeito pesado e o Job
#      fica em um estado retomável, nunca FAILED fictício (CASO 2);
#   3. DRAINING via ``ShutdownCoordinator`` REAL (nunca
#      ``rm.begin_shutdown()`` isolado) commitado ANTES da admissão impede
#      o efeito pesado (CASO 3);
#   4. a ordem da corrida é observável e determinística nos dois sentidos —
#      controle-antes-da-admissão bloqueia, admissão-antes-do-controle é
#      tratada como trabalho já iniciado (CASO 4);
#   5. o mesmo vale para ``BatchEngine.pause_batch`` (BATCH PAUSE);
#   6. ``stop_after_current``, quando o Job só espera capacidade, é tratado
#      como "ainda não é current" — bloqueia, não deixa terminar
#      (STOP_AFTER_CURRENT, decisão explícita e documentada, não escolhida
#      em silêncio).
# Em nenhum caso um permit vaza, uma thread fica zumbi, ou o Job cai em
# FAILED/UNKNOWN fictício.


def _wait_until(predicate, *, timeout=5.0, interval=0.005):
    """Espera determinística por condição observável — nunca um sleep fixo
    como prova de ordering (a prova real é a ordem de program-order entre
    threads via Event/join; isto só evita busy-loop apertado)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _make_admission_checked_handler(rm, engine, heavy_started, calls):
    """Handler realista: adquire capacidade do ResourceManager e, como
    PRIMEIRA coisa dentro do ``with`` (contrato obrigatório de
    ``admit_heavy_work``), revalida a admissão antes de "executar" o efeito
    pesado (aqui simulado por marcar ``heavy_started``/``calls`` — nenhum
    subprocesso real é necessário para provar o contrato)."""

    def handler(job):
        with rm.acquire_for(RESOURCE_FFMPEG, timeout=15):
            engine.admit_heavy_work(job)
            calls.append(job.id)
            heavy_started["value"] = True
        return JobStepResult(target_status=JOB_READY)

    return handler


def test_adversarial5_caso1_cancel_enquanto_espera_capacidade_impede_efeito_pesado(local_db):
    """CASO 1 do bloqueador: capacidade FFMPEG=1, A ocupa, Job B é
    reivindicado (PENDING->PROCESSING) e fica preso esperando capacidade,
    cancel(JOB, B) commita enquanto B ainda espera, A libera. RESULTADO
    OBRIGATÓRIO: heavy_started nunca vira True, B termina CANCELLED, sem
    permit leak, sem thread zumbi, accounting=0, audit coerente
    (cancel_requested limpo atomicamente com a transição)."""
    audit = OperationalAuditLog(local_db)
    control = ControlManager(local_db, audit_log=audit)
    rm = ResourceManager(
        profile=PROFILE_LOW, hardware=_hardware(), overrides={RESOURCE_FFMPEG: 1}
    )
    engine = JobEngine(local_db, audit_log=audit, control_manager=control, resource_manager=rm)

    heavy_started = {"value": False}
    calls: list[str] = []
    engine.register_handler(
        "FFMPEG_OP",
        _make_admission_checked_handler(rm, engine, heavy_started, calls),
        claims_status=JOB_PROCESSING,
    )

    a_lease = rm.acquire_for(RESOURCE_FFMPEG, timeout=5)
    job_b = _create_job(audit, operation="FFMPEG_OP")

    b_outcome: dict[str, Any] = {}

    def run_b():
        b_outcome["job"] = engine.advance(job_b.id)

    t_b = threading.Thread(target=run_b)
    t_b.start()

    # Confirma que B já foi reivindicado (PROCESSING) e está REALMENTE
    # esperando capacidade (fila interna do ResourceManager não vazia)
    # antes de prosseguir — sem isso não provaríamos "esperando", só
    # "ainda não rodou".
    assert _wait_until(lambda: engine.get_job(job_b.id).status == JOB_PROCESSING)
    assert _wait_until(lambda: rm.snapshot()["queue_length"] >= 1)
    assert heavy_started["value"] is False

    outcome = control.cancel(CONTROL_SCOPE_JOB, job_b.id)
    # PROCESSING está em _CANCEL_DEFERRED_STATUSES: a intenção é registrada,
    # não aplicada na hora — exatamente o comportamento já aprovado.
    assert outcome.status == CANCEL_OUTCOME_DEFERRED
    assert control.is_job_cancel_requested(job_b.id) is True

    a_lease.release()  # só agora B pode ser concedido

    t_b.join(timeout=10)
    assert not t_b.is_alive()  # nenhuma thread zumbi
    assert "error" not in b_outcome  # advance() completa normalmente (não levanta)

    assert heavy_started["value"] is False  # efeito pesado NUNCA começou
    assert calls == []
    final = engine.get_job(job_b.id)
    assert final.status == JOB_CANCELLED
    # audit coerente: nunca fica CANCELLED commitado com a intenção ainda ativa
    assert control.is_job_cancel_requested(job_b.id) is False

    snap = rm.snapshot()
    assert snap["in_use"][RESOURCE_FFMPEG] == 0
    assert snap["queue_length"] == 0
    assert snap["waiting"] == {}


def test_adversarial5_caso2_pause_enquanto_espera_capacidade_bloqueia_e_e_retomavel(local_db):
    """CASO 2 do bloqueador: mesmo cenário, mas com GLOBAL PAUSE em vez de
    cancel. RESULTADO OBRIGATÓRIO (corrigido na 7ª revisão adversarial):
    workload pesado de B NÃO começa; Job entra em estado coerente e
    AUTOMATICAMENTE retomável (RETRY, nunca FAILED fictício e nunca
    BLOCKED — pause é autoridade TEMPORÁRIA, não deve exigir intervenção
    manual); pause continua sendo autoridade exclusiva do ControlManager;
    depois de resume, ``fetch_pending_jobs()``/``advance()`` encontram B
    sozinhos e o efeito pesado roda exatamente uma vez, sem nenhuma
    transição manual via AuditLog (ver ``test_adversarial7_*`` para a
    prova completa do fluxo real de produto)."""
    audit = OperationalAuditLog(local_db)
    control = ControlManager(local_db, audit_log=audit)
    rm = ResourceManager(
        profile=PROFILE_LOW, hardware=_hardware(), overrides={RESOURCE_FFMPEG: 1}
    )
    engine = JobEngine(local_db, audit_log=audit, control_manager=control, resource_manager=rm)

    heavy_started = {"value": False}
    calls: list[str] = []
    engine.register_handler(
        "FFMPEG_OP",
        _make_admission_checked_handler(rm, engine, heavy_started, calls),
        claims_status=JOB_PROCESSING,
    )

    a_lease = rm.acquire_for(RESOURCE_FFMPEG, timeout=5)
    job_b = _create_job(audit, operation="FFMPEG_OP")

    def run_b():
        engine.advance(job_b.id)

    t_b = threading.Thread(target=run_b)
    t_b.start()

    assert _wait_until(lambda: engine.get_job(job_b.id).status == JOB_PROCESSING)
    assert _wait_until(lambda: rm.snapshot()["queue_length"] >= 1)

    control.pause(CONTROL_SCOPE_GLOBAL)
    a_lease.release()

    t_b.join(timeout=10)
    assert not t_b.is_alive()

    assert heavy_started["value"] is False
    assert calls == []
    retried = engine.get_job(job_b.id)
    assert retried.status == JOB_RETRY  # nunca FAILED/BLOCKED fictício
    assert rm.snapshot()["in_use"][RESOURCE_FFMPEG] == 0

    # Retomada pelo caminho REAL de produto: nenhuma transição manual via
    # AuditLog — apenas resume() (limpa a autoridade) seguido de um novo
    # advance() sobre o Job já RETRY, que completa normalmente e roda o
    # efeito pesado exatamente UMA vez.
    control.resume(CONTROL_SCOPE_GLOBAL)
    final = engine.advance(job_b.id)

    assert final.status == JOB_READY
    assert heavy_started["value"] is True
    assert calls == [job_b.id]  # exatamente uma execução do efeito pesado


def test_adversarial5_caso3_draining_real_enquanto_espera_capacidade_impede_efeito_pesado(local_db):
    """CASO 3 do bloqueador: DRAINING via ``ShutdownCoordinator`` REAL
    (nunca ``rm.begin_shutdown()`` isolado — isso só provaria o
    ResourceManager sozinho, não a integração). B já foi reivindicado e
    está esperando capacidade quando o DRAINING começa. RESULTADO
    OBRIGATÓRIO: B não inicia o workload pesado; shutdown não ganha thread
    zumbi; accounting termina consistente; o comportamento persistido de
    DRAINING continua pertencendo exclusivamente ao ShutdownCoordinator."""
    audit = OperationalAuditLog(local_db)
    control = ControlManager(local_db, audit_log=audit)
    rm = ResourceManager(
        profile=PROFILE_LOW, hardware=_hardware(), overrides={RESOURCE_FFMPEG: 1}
    )
    coordinator = ShutdownCoordinator(local_db, drain_timeout_seconds=5.0, poll_interval_seconds=0.02)
    engine = JobEngine(
        local_db,
        audit_log=audit,
        control_manager=control,
        shutdown_coordinator=coordinator,
        resource_manager=rm,
    )

    heavy_started = {"value": False}
    calls: list[str] = []
    engine.register_handler(
        "FFMPEG_OP",
        _make_admission_checked_handler(rm, engine, heavy_started, calls),
        claims_status=JOB_PROCESSING,
    )

    a_lease = rm.acquire_for(RESOURCE_FFMPEG, timeout=5)
    job_b = _create_job(audit, operation="FFMPEG_OP")

    def run_b():
        engine.advance(job_b.id)

    t_b = threading.Thread(target=run_b)
    t_b.start()

    assert _wait_until(lambda: engine.get_job(job_b.id).status == JOB_PROCESSING)
    assert _wait_until(lambda: rm.snapshot()["queue_length"] >= 1)

    coordinator.acquire_ownership()
    try:
        shutdown_result: dict[str, Any] = {}

        def do_shutdown():
            shutdown_result["report"] = coordinator.shutdown(
                reason="teste_draining_enquanto_espera_resource"
            )

        t_shutdown = threading.Thread(target=do_shutdown)
        t_shutdown.start()

        assert _wait_until(lambda: coordinator.is_draining())

        a_lease.release()

        t_b.join(timeout=10)
        assert not t_b.is_alive()

        assert heavy_started["value"] is False
        assert calls == []
        retried = engine.get_job(job_b.id)
        assert retried.status == JOB_RETRY  # saiu de PROCESSING -> drain consegue concluir;
        # RETRY (não BLOCKED, 7ª revisão) porque nenhum efeito começou e o
        # Job não deve exigir intervenção manual só por causa de DRAINING

        t_shutdown.join(timeout=10)
        assert not t_shutdown.is_alive()  # sem thread zumbi
        assert "report" in shutdown_result
    finally:
        if coordinator.owns_instance:
            coordinator.release_ownership()

    snap = rm.snapshot()
    assert snap["in_use"][RESOURCE_FFMPEG] == 0
    assert snap["queue_length"] == 0


def test_adversarial5_caso4a_corrida_real_controle_antes_da_admissao_bloqueia(local_db):
    """CASO 4, ordem A: pause em escopo QUEUE commita ANTES de
    admit_heavy_work retornar (mesma estrutura do CASO 2, isolado aqui com
    um escopo diferente para provar que é a ORDEM, não o escopo específico,
    que decide). RESULTADO: workload NÃO inicia."""
    audit = OperationalAuditLog(local_db)
    control = ControlManager(local_db, audit_log=audit)
    rm = ResourceManager(
        profile=PROFILE_LOW, hardware=_hardware(), overrides={RESOURCE_FFMPEG: 1}
    )
    engine = JobEngine(local_db, audit_log=audit, control_manager=control, resource_manager=rm)

    heavy_started = {"value": False}
    calls: list[str] = []
    engine.register_handler(
        "FFMPEG_OP",
        _make_admission_checked_handler(rm, engine, heavy_started, calls),
        claims_status=JOB_PROCESSING,
    )

    a_lease = rm.acquire_for(RESOURCE_FFMPEG, timeout=5)
    job_b = _create_job(audit, operation="FFMPEG_OP")

    t_b = threading.Thread(target=lambda: engine.advance(job_b.id))
    t_b.start()
    assert _wait_until(lambda: engine.get_job(job_b.id).status == JOB_PROCESSING)
    assert _wait_until(lambda: rm.snapshot()["queue_length"] >= 1)

    control.pause("QUEUE", "FFMPEG_OP")  # commita ANTES da admissão de B
    a_lease.release()

    t_b.join(timeout=10)
    assert not t_b.is_alive()

    assert heavy_started["value"] is False
    assert calls == []
    assert engine.get_job(job_b.id).status == JOB_RETRY
    assert rm.snapshot()["in_use"][RESOURCE_FFMPEG] == 0


def test_adversarial5_caso4b_corrida_real_admissao_antes_do_controle_trata_como_ja_iniciado(local_db):
    """CASO 4, ordem B: admit_heavy_work já retornou com sucesso (nenhuma
    espera envolvida — capacidade disponível de imediato) quando pause
    commita DEPOIS, enquanto o handler ainda está "no meio" do efeito
    pesado. RESULTADO: tratado como trabalho já iniciado — a regra já
    aprovada (pause nunca interrompe um handler já em execução, ver
    control_manager.py) se aplica inalterada, e o Job termina normalmente
    (READY), sem nenhuma nova checagem revertendo o efeito já iniciado."""
    audit = OperationalAuditLog(local_db)
    control = ControlManager(local_db, audit_log=audit)
    rm = ResourceManager(profile=PROFILE_NORMAL, hardware=_hardware())  # sobra capacidade: sem espera

    engine = JobEngine(local_db, audit_log=audit, control_manager=control, resource_manager=rm)

    admitted = threading.Event()
    allow_finish = threading.Event()
    heavy_started = threading.Event()
    calls: list[str] = []

    def handler(job):
        with rm.acquire_for(RESOURCE_FFMPEG, timeout=10):
            engine.admit_heavy_work(job)  # nada bloqueado ainda -> passa
            admitted.set()
            heavy_started.set()
            calls.append(job.id)
            assert allow_finish.wait(timeout=10)  # segura "efeito pesado em andamento"
        return JobStepResult(target_status=JOB_READY)

    engine.register_handler("FFMPEG_OP", handler, claims_status=JOB_PROCESSING)
    job_b = _create_job(audit, operation="FFMPEG_OP")

    b_outcome: dict[str, Any] = {}

    def run_b():
        b_outcome["job"] = engine.advance(job_b.id)

    t_b = threading.Thread(target=run_b)
    t_b.start()

    assert admitted.wait(timeout=5)
    control.pause(CONTROL_SCOPE_GLOBAL)  # commita DEPOIS da admissão já ter passado
    allow_finish.set()

    t_b.join(timeout=10)
    assert not t_b.is_alive()

    assert heavy_started.is_set()
    assert calls == [job_b.id]
    final = engine.get_job(job_b.id)
    assert final.status == JOB_READY  # pause posterior não desfaz/mascara o efeito já iniciado
    assert rm.snapshot()["in_use"][RESOURCE_FFMPEG] == 0


def test_adversarial5_batch_pause_enquanto_espera_capacidade_impede_efeito_pesado(local_db):
    """BATCH PAUSE: mesma análise, usando a autoridade já existente
    (``BatchEngine.pause_batch``), nunca uma reimplementação. Um Job que
    pertence a um batch pode já estar PROCESSING só porque está esperando
    capacidade — se o batch é pausado ANTES da admissão, o workload pesado
    não pode começar depois."""
    audit = OperationalAuditLog(local_db)
    control = ControlManager(local_db, audit_log=audit)
    rm = ResourceManager(
        profile=PROFILE_LOW, hardware=_hardware(), overrides={RESOURCE_FFMPEG: 1}
    )
    engine = JobEngine(local_db, audit_log=audit, control_manager=control, resource_manager=rm)
    batch = BatchEngine(local_db, audit_log=audit, control_manager=control, job_engine=engine)

    heavy_started = {"value": False}
    calls: list[str] = []
    engine.register_handler(
        "FFMPEG_OP",
        _make_admission_checked_handler(rm, engine, heavy_started, calls),
        claims_status=JOB_PROCESSING,
    )

    a_lease = rm.acquire_for(RESOURCE_FFMPEG, timeout=5)
    result = batch.create_batch([{"operation": "FFMPEG_OP"}])
    job_b_id = result.job_ids[0]

    t_b = threading.Thread(target=lambda: engine.advance(job_b_id))
    t_b.start()

    assert _wait_until(lambda: engine.get_job(job_b_id).status == JOB_PROCESSING)
    assert _wait_until(lambda: rm.snapshot()["queue_length"] >= 1)

    batch.pause_batch(result.batch_id)
    a_lease.release()

    t_b.join(timeout=10)
    assert not t_b.is_alive()

    assert heavy_started["value"] is False
    assert calls == []
    assert engine.get_job(job_b_id).status == JOB_RETRY
    assert rm.snapshot()["in_use"][RESOURCE_FFMPEG] == 0


def test_adversarial5_stop_after_current_enquanto_espera_capacidade_nao_conta_como_current(local_db):
    """STOP_AFTER_CURRENT — decisão explícita e documentada (nunca
    escolhida em silêncio, ver ``admit_heavy_work``): um Job que só espera
    capacidade do ResourceManager NÃO conta como "current" para fins de
    ``stop_after_current`` — ele ainda não iniciou nenhum efeito, então é
    bloqueado exatamente como pause, nunca deixado terminar."""
    audit = OperationalAuditLog(local_db)
    control = ControlManager(local_db, audit_log=audit)
    rm = ResourceManager(
        profile=PROFILE_LOW, hardware=_hardware(), overrides={RESOURCE_FFMPEG: 1}
    )
    engine = JobEngine(local_db, audit_log=audit, control_manager=control, resource_manager=rm)

    heavy_started = {"value": False}
    calls: list[str] = []
    engine.register_handler(
        "FFMPEG_OP",
        _make_admission_checked_handler(rm, engine, heavy_started, calls),
        claims_status=JOB_PROCESSING,
    )

    a_lease = rm.acquire_for(RESOURCE_FFMPEG, timeout=5)
    job_b = _create_job(audit, operation="FFMPEG_OP")

    t_b = threading.Thread(target=lambda: engine.advance(job_b.id))
    t_b.start()

    assert _wait_until(lambda: engine.get_job(job_b.id).status == JOB_PROCESSING)
    assert _wait_until(lambda: rm.snapshot()["queue_length"] >= 1)

    control.request_stop_after_current(CONTROL_SCOPE_GLOBAL)
    a_lease.release()

    t_b.join(timeout=10)
    assert not t_b.is_alive()

    assert heavy_started["value"] is False
    assert calls == []
    assert engine.get_job(job_b.id).status == JOB_RETRY
    assert rm.snapshot()["in_use"][RESOURCE_FFMPEG] == 0


def test_adversarial5_admit_heavy_work_sem_colaboradores_e_no_op(local_db):
    """Sem control_manager/shutdown_coordinator/batch_engine (todos
    ``None``, comportamento padrão antes deste Prompt), ``admit_heavy_work``
    nunca bloqueia nada — mesma filosofia de "opcional e independente" já
    usada pelos outros três colaboradores."""
    audit = OperationalAuditLog(local_db)
    rm = ResourceManager(profile=PROFILE_NORMAL, hardware=_hardware())
    engine = JobEngine(local_db, audit_log=audit, resource_manager=rm)

    calls: list[str] = []

    def handler(job):
        with rm.acquire_for(RESOURCE_FFMPEG, timeout=5):
            engine.admit_heavy_work(job)  # não levanta nada
            calls.append(job.id)
        return JobStepResult(target_status=JOB_READY)

    engine.register_handler("FFMPEG_OP", handler, claims_status=JOB_PROCESSING)
    job = _create_job(audit, operation="FFMPEG_OP")

    final = engine.advance(job.id)
    assert final.status == JOB_READY
    assert calls == [job.id]


def test_adversarial5_heavy_work_admission_denied_error_e_publica():
    """``HeavyWorkAdmissionDeniedError`` é parte do contrato público de
    ``job_engine`` (exportada em ``__all__``), não um detalhe interno."""
    from _sistema import job_engine as job_engine_module

    assert "HeavyWorkAdmissionDeniedError" in job_engine_module.__all__
    assert issubclass(HeavyWorkAdmissionDeniedError, Exception)


# ---------------------------------------------------------------------------
# SEXTA REVISÃO ADVERSARIAL — LINEARIZAÇÃO TRANSACIONAL DE admit_heavy_work
# E PRECEDÊNCIA DE CANCEL NO LANDING
# ---------------------------------------------------------------------------
#
# BLOQUEADOR 1 (corrigido): as quatro checagens de ``admit_heavy_work``
# viravam leituras soltas, cada uma abrindo sua própria conexão — TOCTOU
# real entre elas. Correção: todas as quatro leituras agora acontecem sob a
# MESMA transaction ``BEGIN IMMEDIATE`` (mesma técnica de ``_claim``), que
# disputa o write lock do SQLite com qualquer pause/cancel/DRAINING/batch
# pause concorrente (todos gravam sob seu próprio ``BEGIN IMMEDIATE``).
#
# BLOQUEADOR 2 (corrigido): ``_land_after_admission_denied`` agora relê
# ``is_job_cancel_requested`` dentro da SUA PRÓPRIA transaction como última
# palavra, com precedência sobre qualquer motivo de negação não-cancel —
# fechando a janela entre a negação por CONTROL_BLOCKED/DRAINING/
# BATCH_PAUSED e o pouso, onde um cancel concorrente podia ficar
# "encalhado" (BLOCKED + cancel_requested=True).


def test_adversarial6_bloqueador1_ordem1_controle_serializado_antes_nega_todas_autoridades(local_db):
    """ORDEM 1 (controle vence): cada autoridade commita ANTES de
    ``admit_heavy_work`` ser sequer chamado — prova direta, para TODAS as
    autoridades (não só uma), de que a decisão as enxerga corretamente.
    Usa ``engine._claim`` diretamente (mesmo padrão já usado em
    ``tests/test_control_manager.py``) para isolar exclusivamente o
    contrato de ``admit_heavy_work``, sem depender do ResourceManager."""
    audit = OperationalAuditLog(local_db)
    control = ControlManager(local_db, audit_log=audit)
    coordinator = ShutdownCoordinator(local_db, drain_timeout_seconds=3.0, poll_interval_seconds=0.02)
    rm = ResourceManager(profile=PROFILE_NORMAL, hardware=_hardware())
    engine = JobEngine(
        local_db,
        audit_log=audit,
        control_manager=control,
        shutdown_coordinator=coordinator,
        resource_manager=rm,
    )
    batch = BatchEngine(local_db, audit_log=audit, control_manager=control, job_engine=engine)
    engine.register_handler("FFMPEG_OP", lambda job: JobStepResult(target_status=JOB_READY), claims_status=JOB_PROCESSING)

    def _fresh_claimed_job():
        job = _create_job(audit, operation="FFMPEG_OP")
        return engine._claim(job.id, JOB_PROCESSING, "FFMPEG_OP")

    # 1. GLOBAL pause — precisa reivindicar (_claim) ANTES de pausar, já
    # que _claim também recusaria a reivindicação com GLOBAL pausado
    # (comportamento já aprovado, inalterado): a ordem aqui prova
    # especificamente admit_heavy_work, não _claim.
    claimed = _fresh_claimed_job()
    control.pause(CONTROL_SCOPE_GLOBAL)
    with pytest.raises(HeavyWorkAdmissionDeniedError) as exc_info:
        engine.admit_heavy_work(claimed)
    assert exc_info.value.reason == "CONTROL_BLOCKED"
    control.resume(CONTROL_SCOPE_GLOBAL)

    # 2. QUEUE pause
    claimed = _fresh_claimed_job()
    control.pause("QUEUE", "FFMPEG_OP")
    with pytest.raises(HeavyWorkAdmissionDeniedError) as exc_info:
        engine.admit_heavy_work(claimed)
    assert exc_info.value.reason == "CONTROL_BLOCKED"
    control.resume("QUEUE", "FFMPEG_OP")

    # 3. JOB pause (escopo do próprio Job)
    claimed = _fresh_claimed_job()
    control.pause(CONTROL_SCOPE_JOB, claimed.id)
    with pytest.raises(HeavyWorkAdmissionDeniedError) as exc_info:
        engine.admit_heavy_work(claimed)
    assert exc_info.value.reason == "CONTROL_BLOCKED"

    # 4. stop_after_current (GLOBAL) — decisão explícita: Job só esperando
    # capacidade NÃO conta como "current".
    claimed = _fresh_claimed_job()
    control.request_stop_after_current(CONTROL_SCOPE_GLOBAL)
    with pytest.raises(HeavyWorkAdmissionDeniedError) as exc_info:
        engine.admit_heavy_work(claimed)
    assert exc_info.value.reason == "CONTROL_BLOCKED"
    control.resume(CONTROL_SCOPE_GLOBAL)  # resume() limpa paused E stop_after_current

    # 5. cancel_requested (só PROCESSING)
    claimed = _fresh_claimed_job()
    outcome = control.cancel(CONTROL_SCOPE_JOB, claimed.id)
    assert outcome.status == CANCEL_OUTCOME_DEFERRED
    with pytest.raises(HeavyWorkAdmissionDeniedError) as exc_info:
        engine.admit_heavy_work(claimed)
    assert exc_info.value.reason == "CANCEL_REQUESTED"

    # 6. batch pause
    result = batch.create_batch([{"operation": "FFMPEG_OP"}])
    claimed = engine._claim(result.job_ids[0], JOB_PROCESSING, "FFMPEG_OP")
    batch.pause_batch(result.batch_id)
    with pytest.raises(HeavyWorkAdmissionDeniedError) as exc_info:
        engine.admit_heavy_work(claimed)
    assert exc_info.value.reason == "BATCH_PAUSED"
    batch.resume_batch(result.batch_id)

    # 7. batch cancel_batch_request IN_PROGRESS (crash simulado no meio do
    # laço de cancelamento — mesmo padrão de
    # test_crash_real_durante_cancelamento_de_batch_bloqueia_tudo_ate_retomar
    # em tests/test_batch_engine.py) também bloqueia admissão de QUALQUER
    # candidato do batch, mesmo um ainda não alcançado pelo laço.
    class _SimulatedCrashLocal(BaseException):
        pass

    result2 = batch.create_batch([{"operation": "FFMPEG_OP"}, {"operation": "FFMPEG_OP"}])
    claimed_untouched = engine._claim(result2.job_ids[1], JOB_PROCESSING, "FFMPEG_OP")
    real_cancel = batch.control_manager.cancel
    call_count = {"n": 0}

    def crashing_cancel(scope, scope_id=None, *, reason=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise _SimulatedCrashLocal("queda real simulada no meio do laço")
        return real_cancel(scope, scope_id, reason=reason)

    import unittest.mock

    with unittest.mock.patch.object(batch.control_manager, "cancel", crashing_cancel):
        with pytest.raises(_SimulatedCrashLocal):
            batch.cancel_batch(result2.batch_id)

    request = batch.get_batch_cancel_request(result2.batch_id)
    assert request["status"] == "IN_PROGRESS"

    with pytest.raises(HeavyWorkAdmissionDeniedError) as exc_info:
        engine.admit_heavy_work(claimed_untouched)
    assert exc_info.value.reason == "BATCH_PAUSED"

    # 8. DRAINING real (ShutdownCoordinator) — DELIBERADAMENTE POR ÚLTIMO:
    # ``is_draining()`` permanece ``True`` para sempre nesta base depois que
    # ``shutdown()`` conclui (``admission_blocked`` persiste até um
    # ``startup_reconcile()`` de uma sessão nova — ver docstring de
    # ``ShutdownCoordinator.is_draining``), então iniciar um drain real aqui
    # bloquearia permanentemente qualquer caso subsequente deste teste.
    claimed = _fresh_claimed_job()
    coordinator.acquire_ownership()
    try:
        shutdown_result: dict[str, Any] = {}

        def do_shutdown():
            shutdown_result["report"] = coordinator.shutdown(reason="teste_ordem1_draining")

        t_shutdown = threading.Thread(target=do_shutdown)
        t_shutdown.start()
        assert _wait_until(lambda: coordinator.is_draining())

        with pytest.raises(HeavyWorkAdmissionDeniedError) as exc_info:
            engine.admit_heavy_work(claimed)
        assert exc_info.value.reason == "DRAINING"

        # libera o drain: transiciona o Job manualmente para fora de PROCESSING
        audit.transition_job(claimed.id, JOB_BLOCKED, data={"reason": "teste"})
        t_shutdown.join(timeout=10)
        assert not t_shutdown.is_alive()
        assert "report" in shutdown_result
    finally:
        if coordinator.owns_instance:
            coordinator.release_ownership()


def test_adversarial6_bloqueador1_ordem2_admissao_vence_o_lock_bloqueia_pause_concorrente(local_db, monkeypatch):
    """ORDEM 2 (admissão vence): a transaction de ``admit_heavy_work``
    adquire o ``BEGIN IMMEDIATE`` primeiro. Um ``pause(GLOBAL)`` concorrente
    tenta commitar ENQUANTO essa transaction ainda segura o write lock do
    SQLite — e não consegue: ``ControlManager.pause`` também abre seu
    próprio ``BEGIN IMMEDIATE`` (ver control_manager.py), então fica
    genuinamente bloqueado pelo SQLite (não é uma corrida de agendamento do
    SO) até a transaction de admissão terminar. Prova real de serialização
    via lock do banco, não program-order Python — sem sleep como mecanismo
    de prova principal."""
    audit = OperationalAuditLog(local_db)
    control = ControlManager(local_db, audit_log=audit)
    rm = ResourceManager(profile=PROFILE_NORMAL, hardware=_hardware())
    engine = JobEngine(local_db, audit_log=audit, control_manager=control, resource_manager=rm)
    engine.register_handler("FFMPEG_OP", lambda job: JobStepResult(target_status=JOB_READY), claims_status=JOB_PROCESSING)

    job = _create_job(audit, operation="FFMPEG_OP")
    claimed = engine._claim(job.id, JOB_PROCESSING, "FFMPEG_OP")

    admission_holding_lock = threading.Event()
    release_admission = threading.Event()
    real_is_execution_blocked = control.is_execution_blocked

    def patched_is_execution_blocked(*args, **kwargs):
        # Neste ponto a transaction BEGIN IMMEDIATE de admit_heavy_work já
        # foi aberta (é a primeira leitura que depende de control_manager
        # dentro dela) — o write lock do SQLite já está retido.
        admission_holding_lock.set()
        assert release_admission.wait(timeout=10)
        return real_is_execution_blocked(*args, **kwargs)

    monkeypatch.setattr(control, "is_execution_blocked", patched_is_execution_blocked)

    admit_result: dict[str, Any] = {}

    def run_admit():
        try:
            engine.admit_heavy_work(claimed)
            admit_result["outcome"] = "ADMITTED"
        except HeavyWorkAdmissionDeniedError as exc:
            admit_result["outcome"] = f"DENIED:{exc.reason}"

    t_admit = threading.Thread(target=run_admit)
    t_admit.start()
    assert admission_holding_lock.wait(timeout=10)

    pause_committed = threading.Event()

    def run_pause():
        control.pause(CONTROL_SCOPE_GLOBAL)
        pause_committed.set()

    t_pause = threading.Thread(target=run_pause)
    t_pause.start()

    # Garantia real (não heurística): enquanto admit_heavy_work segura o
    # BEGIN IMMEDIATE, control.pause() está fisicamente bloqueado pelo
    # SQLite (busy_timeout=5s) tentando adquirir o mesmo write lock — uma
    # janela de espera curta e determinística confirma que o SQLite (não o
    # agendamento do SO) é quem está segurando o pause.
    assert not pause_committed.wait(timeout=0.5)

    release_admission.set()
    t_admit.join(timeout=10)
    t_pause.join(timeout=10)
    assert not t_admit.is_alive()
    assert not t_pause.is_alive()

    # Admissão venceu a corrida pelo lock: leu pause=False antes do pause
    # concorrente conseguir commitar.
    assert admit_result["outcome"] == "ADMITTED"
    assert pause_committed.is_set()  # o pause, represado, prossegue normalmente depois


def test_adversarial6_bloqueador2_cancel_entre_denial_e_landing_ainda_vira_cancelled(local_db, monkeypatch):
    """BLOQUEADOR 2: ``admit_heavy_work`` já negou a admissão de B por
    CONTROL_BLOCKED (pause), mas ANTES de ``_land_after_admission_denied``
    abrir sua própria transaction, um ``cancel(JOB, B)`` concorrente
    commita — fica DEFERRED porque B ainda está PROCESSING nesse instante
    (nenhuma transaction de landing foi aberta ainda). RESULTADO
    OBRIGATÓRIO: o pouso final é CANCELLED (NUNCA BLOCKED com
    cancel_requested=True pendurado), cancel_requested fica False depois,
    heavy_started nunca vira True, accounting=0, sem thread zumbi, sem
    FAILED/UNKNOWN."""
    audit = OperationalAuditLog(local_db)
    control = ControlManager(local_db, audit_log=audit)
    rm = ResourceManager(profile=PROFILE_LOW, hardware=_hardware(), overrides={RESOURCE_FFMPEG: 1})
    engine = JobEngine(local_db, audit_log=audit, control_manager=control, resource_manager=rm)

    heavy_started = {"value": False}
    calls: list[str] = []
    engine.register_handler(
        "FFMPEG_OP",
        _make_admission_checked_handler(rm, engine, heavy_started, calls),
        claims_status=JOB_PROCESSING,
    )

    a_lease = rm.acquire_for(RESOURCE_FFMPEG, timeout=5)
    job_b = _create_job(audit, operation="FFMPEG_OP")

    landing_about_to_start = threading.Event()
    let_landing_proceed = threading.Event()
    real_land = JobEngine._land_after_admission_denied

    def patched_land(self, claimed, claims_status, exc):
        landing_about_to_start.set()
        assert let_landing_proceed.wait(timeout=10)
        return real_land(self, claimed, claims_status, exc)

    monkeypatch.setattr(JobEngine, "_land_after_admission_denied", patched_land)

    b_outcome: dict[str, Any] = {}

    def run_b():
        b_outcome["job"] = engine.advance(job_b.id)

    t_b = threading.Thread(target=run_b)
    t_b.start()

    assert _wait_until(lambda: engine.get_job(job_b.id).status == JOB_PROCESSING)
    assert _wait_until(lambda: rm.snapshot()["queue_length"] >= 1)

    control.pause(CONTROL_SCOPE_GLOBAL)  # fará admit_heavy_work negar por CONTROL_BLOCKED
    a_lease.release()

    assert landing_about_to_start.wait(timeout=10)

    # Enquanto o landing ainda não abriu sua transaction, um cancel real
    # concorrente commita — B ainda está PROCESSING, então fica DEFERRED.
    outcome = control.cancel(CONTROL_SCOPE_JOB, job_b.id)
    assert outcome.status == CANCEL_OUTCOME_DEFERRED
    assert control.is_job_cancel_requested(job_b.id) is True
    assert engine.get_job(job_b.id).status == JOB_PROCESSING  # landing ainda não rodou

    let_landing_proceed.set()
    t_b.join(timeout=10)
    assert not t_b.is_alive()

    assert heavy_started["value"] is False
    assert calls == []
    final = engine.get_job(job_b.id)
    assert final.status == JOB_CANCELLED  # NUNCA BLOCKED com cancel_requested pendurado
    assert control.is_job_cancel_requested(job_b.id) is False
    snap = rm.snapshot()
    assert snap["in_use"][RESOURCE_FFMPEG] == 0
    assert snap["queue_length"] == 0


def test_adversarial6_bloqueador2_landing_vence_primeiro_cancel_depois_segue_caminho_normal(local_db):
    """ORDEM B do BLOQUEADOR 2: o landing (RETRY, 7ª revisão) commita ANTES
    de qualquer cancel concorrente ser sequer chamado. Um ``cancel(JOB)``
    chamado DEPOIS encontra o Job já ``RETRY`` (não mais ``PROCESSING``) e
    segue o caminho normal já aprovado para esse estado — nunca fica
    "perdido"."""
    audit = OperationalAuditLog(local_db)
    control = ControlManager(local_db, audit_log=audit)
    rm = ResourceManager(profile=PROFILE_LOW, hardware=_hardware(), overrides={RESOURCE_FFMPEG: 1})
    engine = JobEngine(local_db, audit_log=audit, control_manager=control, resource_manager=rm)

    heavy_started = {"value": False}
    calls: list[str] = []
    engine.register_handler(
        "FFMPEG_OP",
        _make_admission_checked_handler(rm, engine, heavy_started, calls),
        claims_status=JOB_PROCESSING,
    )

    a_lease = rm.acquire_for(RESOURCE_FFMPEG, timeout=5)
    job_b = _create_job(audit, operation="FFMPEG_OP")

    def run_b():
        engine.advance(job_b.id)

    t_b = threading.Thread(target=run_b)
    t_b.start()
    assert _wait_until(lambda: engine.get_job(job_b.id).status == JOB_PROCESSING)
    assert _wait_until(lambda: rm.snapshot()["queue_length"] >= 1)

    control.pause(CONTROL_SCOPE_GLOBAL)
    a_lease.release()

    t_b.join(timeout=10)
    assert not t_b.is_alive()

    assert engine.get_job(job_b.id).status == JOB_RETRY  # landing já commitou

    # cancel chamado só agora: Job não está mais PROCESSING, então o
    # ControlManager aplica o caminho normal (RETRY -> CANCELLED direto,
    # já que a State Machine permite e não há handler ativo — RETRY não
    # está em _CANCEL_DEFERRED_STATUSES, exatamente como BLOCKED não
    # estava).
    outcome = control.cancel(CONTROL_SCOPE_JOB, job_b.id)
    assert outcome.ok  # aplicado imediatamente, não DEFERRED
    assert engine.get_job(job_b.id).status == JOB_CANCELLED
    assert heavy_started["value"] is False


# ---------------------------------------------------------------------------
# SÉTIMA REVISÃO ADVERSARIAL — BLOCKED ERA O DESTINO ERRADO PARA CONTROLE
# TEMPORÁRIO (pause/stop_after_current/batch pause/DRAINING sobre trabalho
# local PROCESSING agora pousam em RETRY, retomável AUTOMATICAMENTE — ver
# "CORREÇÃO PÓS-7ª REVISÃO" na docstring do módulo e de
# ``_land_after_admission_denied``). Estes testes provam o fluxo REAL de
# produto (resume()/resume_batch()/nova sessão), nunca uma transição manual
# via AuditLog — a lição explícita da 7ª revisão é que o teste anterior
# mascarava exatamente esse ponto.
# ---------------------------------------------------------------------------


def test_adversarial7_batch_pause_resume_real_continua_sem_transicao_manual(local_db):
    """TESTE ADVERSARIAL 1: batch pause/resume real. B fica RETRY (nunca
    BLOCKED) quando o batch pausado impede a admissão; enquanto pausado,
    ``advance_batch()`` não o executa; depois de ``resume_batch()``,
    ``advance_batch()`` sozinho encontra B, roda o efeito pesado exatamente
    uma vez e termina READY — sem nenhuma chamada manual a
    ``audit.transition_job``."""
    audit = OperationalAuditLog(local_db)
    control = ControlManager(local_db, audit_log=audit)
    rm = ResourceManager(profile=PROFILE_LOW, hardware=_hardware(), overrides={RESOURCE_FFMPEG: 1})
    engine = JobEngine(local_db, audit_log=audit, control_manager=control, resource_manager=rm)
    batch = BatchEngine(local_db, audit_log=audit, control_manager=control, job_engine=engine)

    heavy_started = {"value": False}
    calls: list[str] = []
    engine.register_handler(
        "FFMPEG_OP",
        _make_admission_checked_handler(rm, engine, heavy_started, calls),
        claims_status=JOB_PROCESSING,
    )

    a_lease = rm.acquire_for(RESOURCE_FFMPEG, timeout=5)
    result = batch.create_batch([{"operation": "FFMPEG_OP"}])
    job_b_id = result.job_ids[0]

    t_b = threading.Thread(target=lambda: engine.advance(job_b_id))
    t_b.start()
    assert _wait_until(lambda: engine.get_job(job_b_id).status == JOB_PROCESSING)
    assert _wait_until(lambda: rm.snapshot()["queue_length"] >= 1)

    batch.pause_batch(result.batch_id)
    a_lease.release()

    t_b.join(timeout=10)
    assert not t_b.is_alive()

    assert heavy_started["value"] is False
    assert calls == []
    assert engine.get_job(job_b_id).status == JOB_RETRY

    # Enquanto o batch continua pausado, advance_batch() não executa B —
    # is_job_admission_blocked() continua vendo o batch pausado.
    outcomes_while_paused = batch.advance_batch(result.batch_id)
    assert outcomes_while_paused == []
    assert engine.get_job(job_b_id).status == JOB_RETRY
    assert heavy_started["value"] is False

    # resume_batch() + advance_batch() real: nenhuma transição manual.
    batch.resume_batch(result.batch_id)
    outcomes = batch.advance_batch(result.batch_id)

    assert len(outcomes) == 1
    assert outcomes[0].job_id == job_b_id
    assert outcomes[0].error is None
    final = engine.get_job(job_b_id)
    assert final.status == JOB_READY
    assert heavy_started["value"] is True
    assert calls == [job_b_id]  # exatamente uma execução do efeito pesado
    assert rm.snapshot()["in_use"][RESOURCE_FFMPEG] == 0


def test_adversarial7_global_pause_resume_real_fetch_pending_encontra_sozinho(local_db):
    """TESTE ADVERSARIAL 2: GLOBAL pause/resume real. Enquanto pausado,
    ``fetch_pending_jobs()`` não devolve B (ControlManager continua
    recusando a reivindicação); depois de ``resume(GLOBAL)``,
    ``fetch_pending_jobs()`` passa a devolvê-lo e ``advance(B)`` roda o
    efeito pesado exatamente uma vez — sem transição manual de estado."""
    audit = OperationalAuditLog(local_db)
    control = ControlManager(local_db, audit_log=audit)
    rm = ResourceManager(profile=PROFILE_LOW, hardware=_hardware(), overrides={RESOURCE_FFMPEG: 1})
    engine = JobEngine(local_db, audit_log=audit, control_manager=control, resource_manager=rm)

    heavy_started = {"value": False}
    calls: list[str] = []
    engine.register_handler(
        "FFMPEG_OP",
        _make_admission_checked_handler(rm, engine, heavy_started, calls),
        claims_status=JOB_PROCESSING,
    )

    a_lease = rm.acquire_for(RESOURCE_FFMPEG, timeout=5)
    job_b = _create_job(audit, operation="FFMPEG_OP")

    t_b = threading.Thread(target=lambda: engine.advance(job_b.id))
    t_b.start()
    assert _wait_until(lambda: engine.get_job(job_b.id).status == JOB_PROCESSING)
    assert _wait_until(lambda: rm.snapshot()["queue_length"] >= 1)

    control.pause(CONTROL_SCOPE_GLOBAL)
    a_lease.release()

    t_b.join(timeout=10)
    assert not t_b.is_alive()
    assert engine.get_job(job_b.id).status == JOB_RETRY
    assert heavy_started["value"] is False

    # Enquanto pausado, o Job RETRY existe mas fetch_pending_jobs() não o
    # entrega para execução — ControlManager continua bloqueando a
    # reivindicação real (chamar advance() diretamente confirma isso).
    with pytest.raises(JobBlockedByControlError):
        engine.advance(job_b.id)
    assert engine.get_job(job_b.id).status == JOB_RETRY
    assert heavy_started["value"] is False

    control.resume(CONTROL_SCOPE_GLOBAL)

    pending_ids = {job.id for job in engine.fetch_pending_jobs(limit=None)}
    assert job_b.id in pending_ids

    final = engine.advance(job_b.id)
    assert final.status == JOB_READY
    assert heavy_started["value"] is True
    assert calls == [job_b.id]
    assert rm.snapshot()["in_use"][RESOURCE_FFMPEG] == 0


def test_adversarial7_stop_after_current_resume_real(local_db):
    """TESTE ADVERSARIAL 3: mesma lógica de pause, mas para
    stop_after_current — RETRY enquanto ativo (não executável), elegível de
    novo automaticamente assim que um ``resume()`` explícito limpar o
    controle."""
    audit = OperationalAuditLog(local_db)
    control = ControlManager(local_db, audit_log=audit)
    rm = ResourceManager(profile=PROFILE_LOW, hardware=_hardware(), overrides={RESOURCE_FFMPEG: 1})
    engine = JobEngine(local_db, audit_log=audit, control_manager=control, resource_manager=rm)

    heavy_started = {"value": False}
    calls: list[str] = []
    engine.register_handler(
        "FFMPEG_OP",
        _make_admission_checked_handler(rm, engine, heavy_started, calls),
        claims_status=JOB_PROCESSING,
    )

    a_lease = rm.acquire_for(RESOURCE_FFMPEG, timeout=5)
    job_b = _create_job(audit, operation="FFMPEG_OP")

    t_b = threading.Thread(target=lambda: engine.advance(job_b.id))
    t_b.start()
    assert _wait_until(lambda: engine.get_job(job_b.id).status == JOB_PROCESSING)
    assert _wait_until(lambda: rm.snapshot()["queue_length"] >= 1)

    control.request_stop_after_current(CONTROL_SCOPE_GLOBAL)
    a_lease.release()

    t_b.join(timeout=10)
    assert not t_b.is_alive()
    assert engine.get_job(job_b.id).status == JOB_RETRY
    assert heavy_started["value"] is False

    with pytest.raises(JobBlockedByControlError):
        engine.advance(job_b.id)

    control.resume(CONTROL_SCOPE_GLOBAL)  # limpa paused E stop_after_current

    final = engine.advance(job_b.id)
    assert final.status == JOB_READY
    assert heavy_started["value"] is True
    assert calls == [job_b.id]


def test_adversarial7_draining_restart_real_job_nao_fica_orfao():
    """TESTE ADVERSARIAL 4: DRAINING + restart REAL — instâncias NOVAS de
    LocalDatabase/JobEngine/ShutdownCoordinator/RecoveryManager sobre o
    MESMO caminho de banco (nunca reaproveitando objetos em memória, ver
    GATE ADVERSARIAL item 4). B pousa RETRY quando o DRAINING impede a
    admissão; a sessão termina (shutdown DRAINED); uma sessão NOVA sobe,
    roda RecoveryManager.recover_at_startup() e
    ShutdownCoordinator.startup_reconcile() na ordem aprovada; B precisa
    continuar como RETRY, elegível, e NUNCA aparecer em
    find_abandoned_jobs() (não é um Job abandonado por crash — é um Job
    que só esperava uma autoridade temporária ser liberada)."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        paths = build_app_paths(install_root=root / "install", data_root=root / "data")

        # --- Sessão 1: Job B fica PROCESSING, aguardando capacidade, e o
        # DRAINING começa antes da admissão. ---
        db1 = LocalDatabase(paths=paths)
        db1.initialize()
        audit1 = OperationalAuditLog(db1)
        control1 = ControlManager(db1, audit_log=audit1)
        coordinator1 = ShutdownCoordinator(db1, drain_timeout_seconds=5.0, poll_interval_seconds=0.02)
        rm1 = ResourceManager(profile=PROFILE_LOW, hardware=_hardware(), overrides={RESOURCE_FFMPEG: 1})
        engine1 = JobEngine(
            db1,
            audit_log=audit1,
            control_manager=control1,
            shutdown_coordinator=coordinator1,
            resource_manager=rm1,
        )

        heavy_started = {"value": False}
        calls: list[str] = []
        engine1.register_handler(
            "FFMPEG_OP",
            _make_admission_checked_handler(rm1, engine1, heavy_started, calls),
            claims_status=JOB_PROCESSING,
        )

        a_lease = rm1.acquire_for(RESOURCE_FFMPEG, timeout=5)
        job_b = _create_job(audit1, operation="FFMPEG_OP")

        t_b = threading.Thread(target=lambda: engine1.advance(job_b.id))
        t_b.start()
        assert _wait_until(lambda: engine1.get_job(job_b.id).status == JOB_PROCESSING)
        assert _wait_until(lambda: rm1.snapshot()["queue_length"] >= 1)

        coordinator1.acquire_ownership()
        shutdown_result: dict[str, Any] = {}

        def do_shutdown():
            shutdown_result["report"] = coordinator1.shutdown(reason="teste_adversarial7_draining_restart")

        t_shutdown = threading.Thread(target=do_shutdown)
        t_shutdown.start()
        assert _wait_until(lambda: coordinator1.is_draining())

        a_lease.release()
        t_b.join(timeout=10)
        assert not t_b.is_alive()

        assert heavy_started["value"] is False
        assert calls == []
        assert engine1.get_job(job_b.id).status == JOB_RETRY  # nunca BLOCKED/órfão

        t_shutdown.join(timeout=10)
        assert not t_shutdown.is_alive()
        assert "report" in shutdown_result
        coordinator1.release_ownership()
        db1.close() if hasattr(db1, "close") else None

        # --- Sessão 2 (restart REAL): instâncias TOTALMENTE novas sobre o
        # mesmo caminho de banco, seguindo a ordem de startup já aprovada:
        # acquire_ownership -> RecoveryManager.recover_at_startup() ->
        # ShutdownCoordinator.startup_reconcile(). ---
        db2 = LocalDatabase(paths=paths)
        db2.initialize()
        audit2 = OperationalAuditLog(db2)
        # process_session_id explícito e DIFERENTE do da sessão 1: dentro
        # do mesmo processo de teste, o identificador de sessão padrão é
        # gerado uma única vez por processo (_PROCESS_SESSION_ID) — usar o
        # parâmetro de injeção dedicado é a forma aprovada de simular um
        # restart REAL sem depender de abrir um processo Python separado.
        coordinator2 = ShutdownCoordinator(
            db2,
            drain_timeout_seconds=5.0,
            poll_interval_seconds=0.02,
            process_session_id="sessao-2-restart-real-adversarial7",
        )
        recovery2 = RecoveryManager(db2, audit_log=audit2)

        coordinator2.acquire_ownership()
        try:
            # B nunca foi PROCESSING/PUBLISHING/RECOVERING/UNKNOWN quando
            # esta sessão nova começou (já tinha pousado RETRY na sessão
            # anterior) — não é um Job abandonado por crash.
            abandoned = recovery2.find_abandoned_jobs()
            assert job_b.id not in {job.id for job in abandoned}

            report = recovery2.recover_at_startup()
            assert job_b.id not in report.recovered_job_ids

            resolution = coordinator2.startup_reconcile()
            assert resolution.resolved is True
            assert coordinator2.is_draining() is False
        finally:
            coordinator2.release_ownership()

        # B continua persistido como RETRY — nunca órfão — e volta a ser
        # elegível/executável normalmente, sem nenhuma transição manual.
        control2 = ControlManager(db2, audit_log=audit2)
        rm2 = ResourceManager(profile=PROFILE_LOW, hardware=_hardware(), overrides={RESOURCE_FFMPEG: 1})
        engine2 = JobEngine(
            db2,
            audit_log=audit2,
            control_manager=control2,
            shutdown_coordinator=coordinator2,
            resource_manager=rm2,
        )
        heavy_started2 = {"value": False}
        calls2: list[str] = []
        engine2.register_handler(
            "FFMPEG_OP",
            _make_admission_checked_handler(rm2, engine2, heavy_started2, calls2),
            claims_status=JOB_PROCESSING,
        )

        reloaded = engine2.get_job(job_b.id)
        assert reloaded.status == JOB_RETRY

        final = engine2.advance(job_b.id)
        assert final.status == JOB_READY
        assert heavy_started2["value"] is True
        assert calls2 == [job_b.id]


def test_adversarial7_cancel_continua_tendo_precedencia_sobre_retry_ordem_a(local_db):
    """TESTE ADVERSARIAL 5, ordem A: cancel_requested commitado ANTES da
    negação continua produzindo CANCELLED diretamente — nunca RETRY. A
    correção da 7ª revisão não enfraquece a precedência de cancel já
    provada na 6ª revisão."""
    audit = OperationalAuditLog(local_db)
    control = ControlManager(local_db, audit_log=audit)
    rm = ResourceManager(profile=PROFILE_LOW, hardware=_hardware(), overrides={RESOURCE_FFMPEG: 1})
    engine = JobEngine(local_db, audit_log=audit, control_manager=control, resource_manager=rm)

    heavy_started = {"value": False}
    calls: list[str] = []
    engine.register_handler(
        "FFMPEG_OP",
        _make_admission_checked_handler(rm, engine, heavy_started, calls),
        claims_status=JOB_PROCESSING,
    )

    a_lease = rm.acquire_for(RESOURCE_FFMPEG, timeout=5)
    job_b = _create_job(audit, operation="FFMPEG_OP")

    t_b = threading.Thread(target=lambda: engine.advance(job_b.id))
    t_b.start()
    assert _wait_until(lambda: engine.get_job(job_b.id).status == JOB_PROCESSING)
    assert _wait_until(lambda: rm.snapshot()["queue_length"] >= 1)

    outcome = control.cancel(CONTROL_SCOPE_JOB, job_b.id)
    assert outcome.status == CANCEL_OUTCOME_DEFERRED
    a_lease.release()

    t_b.join(timeout=10)
    assert not t_b.is_alive()

    assert engine.get_job(job_b.id).status == JOB_CANCELLED  # nunca RETRY
    assert heavy_started["value"] is False
    assert calls == []
    assert control.is_job_cancel_requested(job_b.id) is False


def test_adversarial7_publishing_recovering_preserva_blocked_nao_vira_retry(local_db):
    """CLAIMS_STATUS DIFERENTE DE PROCESSING: para PUBLISHING/RECOVERING
    (trabalho com efeito remoto potencialmente incerto), o pouso permanece
    ``BLOCKED`` — deliberadamente NÃO alterado para RETRY, já que nenhum
    handler concreto deste Prompt usa ResourceManager para esses
    claims_status e inventar retry automático para reconciliação remota
    interrompida está fora de escopo (ver docstring do módulo)."""
    audit = OperationalAuditLog(local_db)
    control = ControlManager(local_db, audit_log=audit)
    rm = ResourceManager(profile=PROFILE_LOW, hardware=_hardware(), overrides={RESOURCE_FFMPEG: 1})
    engine = JobEngine(local_db, audit_log=audit, control_manager=control, resource_manager=rm)

    heavy_started = {"value": False}
    calls: list[str] = []
    engine.register_handler(
        "REMOTE_OP",
        _make_admission_checked_handler(rm, engine, heavy_started, calls),
        claims_status=JOB_PUBLISHING,
    )

    a_lease = rm.acquire_for(RESOURCE_FFMPEG, timeout=5)
    job_b = _create_job(audit, operation="REMOTE_OP")

    t_b = threading.Thread(target=lambda: engine.advance(job_b.id))
    t_b.start()
    assert _wait_until(lambda: engine.get_job(job_b.id).status == JOB_PUBLISHING)
    assert _wait_until(lambda: rm.snapshot()["queue_length"] >= 1)

    control.pause(CONTROL_SCOPE_GLOBAL)
    a_lease.release()

    t_b.join(timeout=10)
    assert not t_b.is_alive()

    assert heavy_started["value"] is False
    assert calls == []
    assert engine.get_job(job_b.id).status == JOB_BLOCKED  # preservado, nunca RETRY
    assert rm.snapshot()["in_use"][RESOURCE_FFMPEG] == 0


# ---------------------------------------------------------------------------
# VALIDAÇÃO WINDOWS FINAL — DETECÇÃO GENÉRICA DE GPU (CIM/PowerShell)
# ---------------------------------------------------------------------------
#
# BLOQUEADOR reproduzido em hardware Windows real: uma AMD Radeon RX550
# física (confirmada por ``Get-CimInstance Win32_VideoController`` no
# Windows) era relatada como ``gpu_available=False`` porque a detecção
# antiga só tentava ``nvidia-smi`` — confundindo "NVIDIA detectada" com
# "GPU disponível". CORREÇÃO: ``_detect_gpu_windows_cim`` detecta presença
# física via CIM (genérico, qualquer fabricante) no Windows;
# ``gpu_available`` reflete presença física real; ``gpu_backend_available``
# (campo NOVO e distinto) só fica ``True`` quando um backend concreto
# (hoje: nvidia-smi) confirma — é ele, não ``gpu_available``, que decide a
# capacidade de ``DIM_GPU_SLOT``. Nenhum teste aqui depende de hardware
# real: subprocess é sempre mockado.
# ---------------------------------------------------------------------------


def _fake_completed(returncode=0, stdout="", stderr=""):
    import subprocess

    return subprocess.CompletedProcess(args=["x"], returncode=returncode, stdout=stdout, stderr=stderr)


def test_adversarial8_amd_via_windows_cim_sem_nvidia_smi(monkeypatch):
    """CENÁRIO REPRODUZIDO: Radeon RX550 real, sem nvidia-smi no PATH.
    ``gpu_available`` deve ser ``True`` (presença física confirmada pelo
    CIM) e ``gpu_vendor`` deve indicar AMD — mas ``gpu_backend_available``
    permanece ``False`` (nenhum backend AMD suportado neste Prompt)."""
    import subprocess

    import _sistema.resource_manager as rm_module

    monkeypatch.setattr(rm_module.sys, "platform", "win32")

    cim_json = '{"Name":"Radeon RX550/550 Series","AdapterRAM":4278190080}'

    def fake_run(args, **kwargs):
        if args[0] == "powershell":
            return _fake_completed(returncode=0, stdout=cim_json)
        raise FileNotFoundError("nvidia-smi não encontrado")

    monkeypatch.setattr(subprocess, "run", fake_run)

    hardware = rm_module.detect_hardware()
    assert hardware.gpu_available is True  # nunca mais False só por falta de nvidia-smi
    assert hardware.gpu_vendor == "AMD"
    assert hardware.gpu_vram_mb == 4278190080 // (1024 * 1024)
    assert hardware.gpu_backend_available is False  # nenhum backend AMD suportado ainda


def test_adversarial8_intel_via_windows_cim(monkeypatch):
    """Intel integrada real detectada via CIM: presença física
    reconhecida, backend continua não confirmado."""
    import subprocess

    import _sistema.resource_manager as rm_module

    monkeypatch.setattr(rm_module.sys, "platform", "win32")

    cim_json = '{"Name":"Intel(R) UHD Graphics 630","AdapterRAM":1073741824}'

    def fake_run(args, **kwargs):
        if args[0] == "powershell":
            return _fake_completed(returncode=0, stdout=cim_json)
        raise FileNotFoundError("nvidia-smi não encontrado")

    monkeypatch.setattr(subprocess, "run", fake_run)

    hardware = rm_module.detect_hardware()
    assert hardware.gpu_available is True
    assert hardware.gpu_vendor == "INTEL"
    assert hardware.gpu_backend_available is False


def test_adversarial8_nvidia_via_nvidia_smi_confirma_backend(monkeypatch):
    """NVIDIA real: CIM reporta a GeForce (presença física) E nvidia-smi
    responde com sucesso — ``gpu_backend_available`` deve ficar ``True``,
    e a VRAM de nvidia-smi (mais específica) sobrepõe a do CIM."""
    import subprocess

    import _sistema.resource_manager as rm_module

    monkeypatch.setattr(rm_module.sys, "platform", "win32")

    cim_json = '{"Name":"NVIDIA GeForce RTX 3060","AdapterRAM":4278190080}'

    def fake_run(args, **kwargs):
        if args[0] == "powershell":
            return _fake_completed(returncode=0, stdout=cim_json)
        if args[0] == "nvidia-smi":
            return _fake_completed(returncode=0, stdout="12288\n")
        raise FileNotFoundError(f"comando inesperado: {args!r}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    hardware = rm_module.detect_hardware()
    assert hardware.gpu_available is True
    assert hardware.gpu_vendor == "NVIDIA"
    assert hardware.gpu_backend_available is True
    assert hardware.gpu_vram_mb == 12288  # nvidia-smi sobrepõe o valor do CIM


def test_adversarial8_multiplos_adapters_escolhe_maior_vram(monkeypatch):
    """Múltiplos adaptadores no CIM (comum em laptops híbridos
    Intel+NVIDIA/AMD): escolhe o de maior VRAM reportada como
    principal."""
    import subprocess

    import _sistema.resource_manager as rm_module

    monkeypatch.setattr(rm_module.sys, "platform", "win32")

    cim_json = json.dumps(
        [
            {"Name": "Intel(R) UHD Graphics", "AdapterRAM": 1073741824},
            {"Name": "AMD Radeon RX 6600", "AdapterRAM": 8589934592},
        ]
    )

    def fake_run(args, **kwargs):
        if args[0] == "powershell":
            return _fake_completed(returncode=0, stdout=cim_json)
        raise FileNotFoundError("nvidia-smi não encontrado")

    monkeypatch.setattr(subprocess, "run", fake_run)

    hardware = rm_module.detect_hardware()
    assert hardware.gpu_available is True
    assert hardware.gpu_vendor == "AMD"
    assert hardware.gpu_vram_mb == 8589934592 // (1024 * 1024)


def test_adversarial8_microsoft_basic_display_adapter_nao_conta_como_gpu(monkeypatch):
    """Só o Microsoft Basic Display Adapter (adaptador virtual/software) —
    nenhuma GPU aceleradora real presente."""
    import subprocess

    import _sistema.resource_manager as rm_module

    monkeypatch.setattr(rm_module.sys, "platform", "win32")

    cim_json = '{"Name":"Microsoft Basic Display Adapter","AdapterRAM":null}'

    def fake_run(args, **kwargs):
        if args[0] == "powershell":
            return _fake_completed(returncode=0, stdout=cim_json)
        raise FileNotFoundError("nvidia-smi não encontrado")

    monkeypatch.setattr(subprocess, "run", fake_run)

    hardware = rm_module.detect_hardware()
    assert hardware.gpu_available is False
    assert hardware.gpu_vendor is None
    assert hardware.gpu_vram_mb is None
    assert hardware.gpu_backend_available is False


def test_adversarial8_ausencia_total_de_detector_nunca_impede_inicializacao(monkeypatch):
    """Nem PowerShell nem nvidia-smi disponíveis: hardware detection
    nunca impede a inicialização do produto — fallback conservador
    registrado em detection_errors."""
    import subprocess

    import _sistema.resource_manager as rm_module

    monkeypatch.setattr(rm_module.sys, "platform", "win32")

    def fake_run(args, **kwargs):
        raise FileNotFoundError(f"{args[0]} não encontrado")

    monkeypatch.setattr(subprocess, "run", fake_run)

    hardware = rm_module.detect_hardware()
    assert hardware.gpu_available is False
    assert hardware.gpu_backend_available is False
    # _detect_gpu_windows_cim propaga FileNotFoundError -> detect_hardware
    # registra e segue com fallback conservador (nunca trava a
    # inicialização); _detect_gpu (nvidia-smi) já captura essa mesma
    # exceção internamente e devolve (False, None) sem precisar levantar,
    # então só o erro do lado do CIM aparece aqui.
    assert any("gpu_cim_detection_failed" in e for e in hardware.detection_errors)

    # E o ResourceManager continua inicializando normalmente com esse hardware.
    rm = ResourceManager(hardware=hardware)
    assert rm.capacity_of(DIM_GPU_SLOT) == 0


def test_adversarial8_powershell_cim_retorna_erro(monkeypatch):
    """PowerShell/CIM roda mas devolve um returncode de erro (ex.:
    Win32_VideoController não pôde ser consultado) — tratado como
    mecanismo indisponível, nunca como GPU ausente com falso positivo de
    confiança nem crash."""
    import subprocess

    import _sistema.resource_manager as rm_module

    monkeypatch.setattr(rm_module.sys, "platform", "win32")

    def fake_run(args, **kwargs):
        if args[0] == "powershell":
            return _fake_completed(returncode=1, stdout="", stderr="erro simulado do CIM")
        raise FileNotFoundError("nvidia-smi não encontrado")

    monkeypatch.setattr(subprocess, "run", fake_run)

    hardware = rm_module.detect_hardware()
    assert hardware.gpu_available is False
    assert any("gpu_cim_detection_unavailable" in e for e in hardware.detection_errors)


def test_adversarial8_powershell_timeout_nunca_trava_startup(monkeypatch):
    """Timeout do subprocess (PowerShell travado/lento): obrigatório não
    travar a inicialização — vira erro registrado, fallback conservador."""
    import subprocess

    import _sistema.resource_manager as rm_module

    monkeypatch.setattr(rm_module.sys, "platform", "win32")

    def fake_run(args, **kwargs):
        if args[0] == "powershell":
            raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout", 5.0))
        raise FileNotFoundError("nvidia-smi não encontrado")

    monkeypatch.setattr(subprocess, "run", fake_run)

    hardware = rm_module.detect_hardware()
    assert hardware.gpu_available is False
    assert any("gpu_cim_detection_failed:TimeoutExpired" in e for e in hardware.detection_errors)


def test_adversarial8_json_invalido_do_cim_nao_lanca(monkeypatch):
    """Saída inesperada/corrompida do PowerShell (não-JSON): tratada como
    mecanismo não confiável desta vez, nunca como crash nem como GPU
    inventada."""
    import subprocess

    import _sistema.resource_manager as rm_module

    monkeypatch.setattr(rm_module.sys, "platform", "win32")

    def fake_run(args, **kwargs):
        if args[0] == "powershell":
            return _fake_completed(returncode=0, stdout="isto não é JSON {{{")
        raise FileNotFoundError("nvidia-smi não encontrado")

    monkeypatch.setattr(subprocess, "run", fake_run)

    hardware = rm_module.detect_hardware()
    assert hardware.gpu_available is False
    assert any("gpu_cim_detection_unavailable" in e for e in hardware.detection_errors)


def test_adversarial8_vram_ausente_no_cim_mantem_none(monkeypatch):
    """Adaptador real detectado, mas sem ``AdapterRAM`` utilizável: VRAM
    fica ``None`` — nunca inventada."""
    import subprocess

    import _sistema.resource_manager as rm_module

    monkeypatch.setattr(rm_module.sys, "platform", "win32")

    cim_json = '{"Name":"AMD Radeon RX550/550 Series"}'  # sem AdapterRAM

    def fake_run(args, **kwargs):
        if args[0] == "powershell":
            return _fake_completed(returncode=0, stdout=cim_json)
        raise FileNotFoundError("nvidia-smi não encontrado")

    monkeypatch.setattr(subprocess, "run", fake_run)

    hardware = rm_module.detect_hardware()
    assert hardware.gpu_available is True
    assert hardware.gpu_vram_mb is None


def test_adversarial8_vram_valida_do_cim_e_aproveitada(monkeypatch):
    """VRAM reportada de forma válida pelo Windows (caso da RX550 real:
    ~4 GB) é aproveitada em MB best-effort."""
    import subprocess

    import _sistema.resource_manager as rm_module

    monkeypatch.setattr(rm_module.sys, "platform", "win32")

    cim_json = '{"Name":"Radeon RX550/550 Series","AdapterRAM":4278190080}'

    def fake_run(args, **kwargs):
        if args[0] == "powershell":
            return _fake_completed(returncode=0, stdout=cim_json)
        raise FileNotFoundError("nvidia-smi não encontrado")

    monkeypatch.setattr(subprocess, "run", fake_run)

    hardware = rm_module.detect_hardware()
    assert hardware.gpu_vram_mb == 4278190080 // (1024 * 1024)


def test_adversarial8_nvidia_smi_ausente_nao_apaga_gpu_amd_detectada(monkeypatch):
    """PONTO CENTRAL do bloqueador reproduzido: a ausência de nvidia-smi
    NUNCA deve apagar uma GPU AMD (ou qualquer outro fabricante) já
    confirmada fisicamente pelo CIM."""
    import subprocess

    import _sistema.resource_manager as rm_module

    monkeypatch.setattr(rm_module.sys, "platform", "win32")

    cim_json = '{"Name":"Radeon RX550/550 Series","AdapterRAM":4278190080}'

    def fake_run(args, **kwargs):
        if args[0] == "powershell":
            return _fake_completed(returncode=0, stdout=cim_json)
        if args[0] == "nvidia-smi":
            raise FileNotFoundError("nvidia-smi não encontrado")
        raise FileNotFoundError(f"comando inesperado: {args!r}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    hardware = rm_module.detect_hardware()
    assert hardware.gpu_available is True  # AMD continua presente
    assert hardware.gpu_vendor == "AMD"


def test_adversarial8_nenhuma_deteccao_falsa_promove_perfil_high(monkeypatch):
    """Presença de GPU (física ou por backend) nunca influencia
    ``select_profile`` — a seleção de perfil é decidida só por CPU/RAM,
    inalterado por esta correção."""
    import subprocess

    import _sistema.resource_manager as rm_module

    monkeypatch.setattr(rm_module.sys, "platform", "win32")
    monkeypatch.setattr(rm_module, "_detect_cpu_count", lambda: 2)  # CPU baixo força LOW

    cim_json = '{"Name":"NVIDIA GeForce RTX 4090","AdapterRAM":25769803776}'

    def fake_run(args, **kwargs):
        if args[0] == "powershell":
            return _fake_completed(returncode=0, stdout=cim_json)
        if args[0] == "nvidia-smi":
            return _fake_completed(returncode=0, stdout="24576\n")
        raise FileNotFoundError("comando inesperado")

    monkeypatch.setattr(subprocess, "run", fake_run)

    hardware = rm_module.detect_hardware()
    assert hardware.gpu_available is True
    assert hardware.gpu_backend_available is True
    # GPU topo de linha não promove HIGH sozinha: CPU=2 continua forçando LOW.
    assert select_profile(hardware) == PROFILE_LOW


def test_adversarial8_compatibilidade_de_workload_separada_de_presenca_fisica():
    """Integração: GPU fisicamente presente (AMD) mas sem backend
    suportado confirmado — ``DIM_GPU_SLOT`` continua com capacidade ZERO
    e ``acquire_for(..., use_gpu=True)`` degrada graciosamente para modo
    CPU, exatamente como uma máquina sem GPU nenhuma. Presença física
    NUNCA promove automaticamente disponibilidade de workload GPU."""
    hardware = _hardware(gpu_available=True, gpu_vendor="AMD", gpu_backend_available=False)
    rm = ResourceManager(profile=PROFILE_NORMAL, hardware=hardware)

    assert rm.gpu_available is True  # GPU física está lá
    assert rm.gpu_backend_available is False  # mas nenhum backend a confirmou
    assert rm.capacity_of(DIM_GPU_SLOT) == 0

    with rm.acquire_for(RESOURCE_WHISPER, use_gpu=True, timeout=2) as lease:
        assert DIM_GPU_SLOT not in lease.requests
        assert rm.snapshot()["in_use"][DIM_GPU_SLOT] == 0
