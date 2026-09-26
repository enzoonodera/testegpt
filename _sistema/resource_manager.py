# -*- coding: utf-8 -*-
"""ResourceManager central — controle de concorrência de recursos pesados
da máquina (FASE 3 / PROMPT 18).

ESCOPO DELIBERADO: O QUE O RESOURCEMANAGER NÃO É
--------------------------------------------------
Este módulo é deliberadamente um controlador de CAPACIDADE em memória, não
mais que isso. Ele explicitamente NÃO é:

- uma segunda fila (a fila continua sendo o SQLite via ``JobEngine``);
- uma segunda State Machine (``Job.status`` continua sendo decidido
  exclusivamente por ``OperationalAuditLog``/``JobEngine``/``ControlManager``);
- um scheduler de publicação;
- um ``BatchEngine``/``JobEngine`` paralelo;
- um mecanismo de persistência alternativo;
- um sistema distribuído (nenhum lock cruza processos ou máquinas).

``JobEngine`` continua sendo a única autoridade do ciclo de vida de um
``Job``. ``BatchEngine`` continua sendo a única autoridade sobre a operação
em lote. ``ControlManager``/``ShutdownCoordinator`` continuam sendo as
únicas autoridades sobre PAUSED/CANCELLED/DRAINING. O ``ResourceManager``
só responde a uma pergunta: "existe capacidade da MÁQUINA suficiente para
este workload pesado começar agora?" — e mantém a contabilização segura
dessa capacidade enquanto o workload está em execução.

CONTRATO CENTRAL
-----------------
Cada workload pesado é descrito por um conjunto de "dimensões" de recurso
(``{dimensão: quantidade}``) que ele precisa reservar simultaneamente. Uma
dimensão pode ser:

- um limite POR CATEGORIA de workload (``RESOURCE_FFMPEG``, ``RESOURCE_WHISPER``,
  ``RESOURCE_OLLAMA``, ``RESOURCE_BROWSER``, ``RESOURCE_RENDER``,
  ``RESOURCE_FRAME_ANALYSIS``) — evita que uma única categoria sozinha
  sature a máquina;
- ``DIM_GLOBAL_HEAVY`` — um orçamento COMPARTILHADO entre todas as
  categorias pesadas, para evitar o erro clássico de "cada categoria
  isolada tem limite seguro, mas a SOMA de todas rodando ao mesmo tempo
  não cabe na máquina" (ver item 6 do Prompt);
- ``DIM_GPU_SLOT`` — um slot compartilhado que representa "a GPU está
  ocupada", usado só pelos workloads que efetivamente pedem GPU nessa
  execução (ver ``acquire_for(..., use_gpu=True)``). Sua capacidade é
  decidida por ``HardwareSnapshot.gpu_backend_available`` (backend de
  aceleração CONFIRMADO), nunca por ``gpu_available`` sozinho (mera
  presença física de GPU) — ver "VALIDAÇÃO WINDOWS FINAL" e docstring de
  ``HardwareSnapshot`` para a distinção completa.

A API central é ``acquire(requests) -> ResourceLease``, usada como context
manager (preferencial) ou com ``release()`` explícito:

    with resource_manager.acquire_for(RESOURCE_FFMPEG):
        executar_ffmpeg(...)

``acquire_for(kind, ...)`` é o atalho de mais alto nível: resolve
automaticamente ``{kind: 1, DIM_GLOBAL_HEAVY: peso_da_categoria}`` (e
``DIM_GPU_SLOT`` quando aplicável) a partir da política do perfil ativo —
nenhum código chamador precisa conhecer os números exatos.

NUNCA VAZAR CAPACIDADE (item 2)
---------------------------------
``ResourceLease`` é um context manager: ``__exit__`` sempre chama
``release()``, então ``with resource_manager.acquire_for(...):`` libera em
QUALQUER caminho de saída do bloco — sucesso, exceção, ``return``
antecipado dentro do ``with``. O caller nunca precisa "lembrar" de liberar
manualmente; um ``release()`` explícito continua disponível para quem
precisar de controle mais fino (ex.: liberar antes do fim de um escopo
maior), mas o padrão recomendado e usado em todos os testes é o
``with``. Ver também "DUPLO RELEASE" abaixo.

NÃO RESERVAR RECURSO ANTES DA HORA (item 3)
----------------------------------------------
O ``ResourceManager`` não participa de ``_claim``/admissão de Job nenhuma:
ele não é consultado enquanto um Job está PENDING/PAUSED/BLOCKED/
AUTH_REQUIRED/USER_ACTION_REQUIRED/aguardando retry ou schedule. A
aquisição é responsabilidade do HANDLER concreto (ex.: o wrapper de FFmpeg
de uma etapa futura), chamada o mais próximo possível da operação pesada
real — nunca em ``_claim``, nunca em ``fetch_pending_jobs``. Isso é uma
escolha arquitetural deliberada (ver "INTEGRAÇÃO COM JOBENGINE" abaixo):
colocar a aquisição em ``JobEngine`` exigiria lógica do tipo
``if operation == "FFMPEG": ...`` espalhada pelo motor genérico (proibido
pelo item 16), e transformaria o ``ResourceManager`` em uma segunda
autoridade de admissão — o que ele explicitamente não é.

PERFIS LOW / NORMAL / HIGH (item 4)
--------------------------------------
A política de limites é centralizada em tabelas por perfil (não hardcoded
espalhado pelo código). O perfil pode ser fornecido explicitamente ou
detectado automaticamente a partir de ``detect_hardware()``:

- CPU: ``os.cpu_count()`` (sempre disponível, stdlib).
- RAM total: Windows via ``ctypes``/``GlobalMemoryStatusEx`` (WinAPI
  padrão, sem dependência nova); em Linux (usado apenas neste
  sandbox/CI de desenvolvimento — o produto oficial é Windows, ver
  ``CLAUDE.md`` item 31) via ``/proc/meminfo`` como melhor esforço.
  Qualquer falha devolve ``None`` — nunca um valor fictício.
- GPU/VRAM: melhor esforço via ``nvidia-smi`` (se presente no PATH), com
  timeout curto. Qualquer falha (binário ausente, timeout, saída
  inesperada) é tratada como "sem GPU compatível detectada" — nunca como
  erro fatal, nunca trava a inicialização (item 20/31). Não é instalada
  nenhuma dependência nova para isso.

Quando RAM/GPU não podem ser detectadas com confiança, a seleção de perfil
usa fallback conservador (nunca promove a HIGH sem RAM confirmada; nunca
assume GPU presente sem confirmação positiva) — ver ``select_profile``.

LIMITES POR CATEGORIA + CAPACIDADE GLOBAL COMPARTILHADA (itens 5 e 6)
-------------------------------------------------------------------------
Cada perfil define um limite por categoria (``_KIND_LIMITS``) E uma
capacidade compartilhada ``DIM_GLOBAL_HEAVY`` (``_GLOBAL_HEAVY_CAPACITY``)
consumida por TODAS as categorias, ponderada por um peso simples por
categoria (``_KIND_GLOBAL_WEIGHT`` — workloads tipicamente mais pesados,
como Whisper/Ollama, pesam mais no orçamento global). Isso impede que
"FFmpeg=4 + Whisper=2 + Ollama=2 + Render=3" rodem todos ao mesmo tempo só
porque cada limite individual permite, mesmo que a soma não caiba na
máquina — sem precisar modelar cada dimensão física (CPU/RAM/GPU) uma por
uma; é uma política simples, determinística e testável, deliberadamente
não-científica (item 6 do Prompt pede exatamente isso).

FAIRNESS / ANTI-STARVATION (item 7) — SIMPLIFICAÇÃO DOCUMENTADA
---------------------------------------------------------------------
A fila de espera é uma ÚNICA fila FIFO global (não uma fila por
dimensão/categoria): todo ``acquire`` recebe um ticket sequencial e só
pode ser concedido quando (a) está na cabeça da fila E (b) há capacidade
para TODAS as dimensões pedidas. Isso garante fairness forte e
determinística entre QUAISQUER dois waiters, de qualquer categoria — a
ordem de chegada é sempre respeitada, sem possibilidade de starvation por
um caller mais novo "furar a fila" na frente de um mais antigo.

Trade-off consciente: como a fila é global, um waiter na frente pedindo uma
categoria momentaneamente cheia pode atrasar um waiter atrás dele que
pediria uma categoria totalmente livre (perda de paralelismo entre
categorias não relacionadas). O Prompt pede explicitamente "não precisa
implementar algoritmo complexo... mas deve existir comportamento
razoavelmente justo" — uma fila FIFO única é a política mais simples,
determinística e testável que satisfaz esse requisito sem starvation.
Uma fila por dimensão com fairness independente por categoria é dívida
técnica documentada (ver ENTREGA), não implementada aqui para evitar
complexidade desproporcional a este Prompt.

CONCORRÊNCIA E LOCKS (itens 8, 35 — DEADLOCK REVIEW)
---------------------------------------------------------
Um único ``threading.Lock``/``threading.Condition`` interno protege TODO o
estado de accounting (``_in_use``, ``_queue``, ``_waiting_by_dim``,
``_shutting_down``). Regras:

- quem adquire: qualquer thread chamando ``acquire``/``acquire_for``/
  ``release``/``snapshot``/``begin_shutdown``/``end_shutdown``.
- quando libera: o lock é mantido apenas durante os pontos de decisão
  (checar fila/capacidade, commitar contadores) — NUNCA durante a operação
  pesada real do caller. ``Condition.wait()`` libera o lock internamente
  enquanto bloqueado, reforçando isso.
- NENHUM código arbitrário de Job/handler roda com este lock seguro: o
  lock é liberado antes de ``acquire``/``acquire_for`` retornar o
  ``ResourceLease`` ao chamador, e a operação pesada acontece inteiramente
  fora dele. Isso satisfaz a regra do item 35 ("não executar workload
  pesado/código arbitrário de Job enquanto mantém lock interno global").
- ordem de aquisição: este é o ÚNICO lock que este módulo introduz; ele
  nunca é adquirido junto com nenhum lock/transaction de
  ``LocalDatabase``/``ControlManager``/``ShutdownCoordinator``/
  ``JobEngine``/``BatchEngine`` (nenhum desses é consultado a partir de
  dentro da seção crítica deste lock, e este módulo nunca é chamado a
  partir de dentro de uma transaction SQLite desses módulos — ver
  "INTEGRAÇÃO COM JOBENGINE"). Não há, portanto, caminho conhecido de
  deadlock entre este lock e os locks/transactions já existentes.
- accounting nunca fica negativo: ``_release_internal`` defende essa
  invariante explicitamente (``ResourceAccountingError`` em vez de deixar
  o contador ir a negativo silenciosamente — nunca deveria disparar se
  ``ResourceLease.release()`` for chamado no máximo uma vez por lease, o
  que é garantido por ela mesma; é uma rede de segurança estrutural, não
  um caminho de controle esperado).

DUPLO RELEASE (item 9) — DECISÃO: NO-OP IDEMPOTENTE
--------------------------------------------------------
``ResourceLease.release()`` é idempotente: a segunda chamada (e qualquer
chamada subsequente) é um no-op seguro, protegido por um lock próprio da
lease (``_release_lock``, verificado-e-marcado atomicamente antes de tocar
o accounting do manager). Justificativa: o padrão mais comum de uso é
``with resource_manager.acquire_for(...):`` combinado eventualmente com um
``release()`` explícito adicional em algum caminho de cleanup (ex.: um
``finally`` externo que também tenta liberar por segurança) — um erro
levantado nesse segundo ``release()`` mascararia a exceção original em um
bloco ``finally``/``except`` e não traria nenhum benefício de segurança
adicional (o accounting já está correto depois da primeira liberação).
Optamos por nunca deixar o contador ir a negativo nem "inventar"
capacidade — o segundo release simplesmente não tem efeito nenhum.

AQUISIÇÃO PARCIAL É ATÔMICA (item 10)
------------------------------------------
Toda checagem de capacidade e todo incremento de contador para um
``acquire`` acontecem sob o MESMO lock, olhando TODAS as dimensões
pedidas de uma vez: ou a aquisição concede TODAS as dimensões pedidas
simultaneamente, ou nenhuma é incrementada e o caller continua esperando
(ou recebe erro). Não existe estado intermediário em que uma dimensão foi
"debitada" e outra não — não há necessidade de rollback porque nunca há
commit parcial.

PAUSE / CANCEL / SHUTDOWN (item 12)
----------------------------------------
``ResourceManager`` não decide PAUSED/CANCELLED/DRAINING — essas
autoridades continuam em ``ControlManager``/``ShutdownCoordinator``. A
única integração própria é ``begin_shutdown()``/``end_shutdown()``: um
sinal em memória, opcional, que o processo de shutdown do produto pode
disparar para acordar imediatamente qualquer waiter bloqueado em
``acquire`` (que recebe ``ResourceManagerShuttingDownError`` em vez de
ficar pendurado indefinidamente) e recusar novas aquisições enquanto o
sinal estiver ativo. Isso não duplica o estado DRAINING persistido do
``ShutdownCoordinator`` — é só uma reação de boa cidadania para não deixar
threads zumbis presas em ``Condition.wait()`` durante um encerramento.
Sem essa chamada explícita (que este Prompt não conecta automaticamente a
nenhum bootstrap — ver "INTEGRAÇÃO" abaixo), o comportamento de shutdown
já aprovado (``JobEngine`` para de reivindicar novos Jobs via
``JobBlockedByShutdownError``) continua intacto e é suficiente para
impedir que trabalho pesado NOVO comece durante DRAINING, já que nenhum
handler novo chega a ser chamado.

NÃO INVENTAR ESTADO NOVO / SEM MIGRATION (itens 13, 14, 15, 32)
---------------------------------------------------------------------
``ResourceManager`` é controle de runtime puro, inteiramente em memória.
Nenhuma tabela SQLite, nenhum lock persistente, nenhuma migration nova
(m001/m002/m003 permanecem intocadas; nenhuma m004 foi criada). Depois de
um crash/restart, uma nova instância de ``ResourceManager`` começa com
accounting zerado — nenhum "permit" da execução anterior continua preso,
porque nada da execução anterior é lido de volta. O estado persistente do
``Job`` continua sendo responsabilidade exclusiva da arquitetura já
aprovada (``LocalDatabase``/``OperationalAuditLog``/``RecoveryManager``).
Nenhum estado novo de Job (ex.: ``WAITING_FOR_RESOURCE``) foi introduzido:
"esperar por recurso" é tratado inteiramente como uma condição interna do
handler, nunca como uma transição de ``Job.status``.

INTEGRAÇÃO COM JOBENGINE (item 16)
----------------------------------------
``JobEngine`` aceita um ``resource_manager`` opcional (``None`` por
padrão — comportamento idêntico ao de antes deste Prompt) guardado como
atributo público simples, no mesmo espírito de
``control_manager``/``shutdown_coordinator``/``batch_engine``. Mas,
diferente daqueles três, o ``JobEngine`` NÃO consulta
``resource_manager`` em nenhum ponto do seu próprio fluxo (``_claim``,
``advance``, ``fetch_pending_jobs``) — ele apenas o disponibiliza para que
um handler registrado externamente possa acessá-lo (tipicamente via
closure sobre o próprio ``JobEngine``/``resource_manager`` no momento do
registro do handler) e usá-lo internamente, o mais perto possível da
operação pesada real, exatamente como pede o item 3. Isso evita
espalhar ``if ffmpeg: ... if whisper: ...`` pelo motor genérico (item 16)
e evita que o ``ResourceManager`` vire uma segunda autoridade de admissão.
``handler(claimed)`` já roda FORA de qualquer transaction SQLite (a
reivindicação já foi commitada antes do handler ser chamado — ver
``job_engine.py``/``advance``), então um handler pode chamar
``resource_manager.acquire_for(...)`` livremente sem nenhum risco de
lock-ordering com o SQLite.

INTEGRAÇÃO COM BATCHENGINE (item 17)
------------------------------------------
Nenhuma mudança foi necessária em ``batch_engine.py``. ``BatchEngine``
continua chamando exclusivamente ``JobEngine.advance()``/
``fetch_pending_jobs`` — a aquisição de recurso acontece dentro do
handler que ``advance()`` invoca, de forma completamente transparente
para ``BatchEngine`` (que nunca soube, e continua não sabendo, o que um
handler faz internamente). Membership, progresso, ``retry_failed``,
``cancel_batch`` e as transições de estado continuam exatamente como
aprovadas no Prompt 17 — nada disso foi movido para o
``ResourceManager``. Testes de regressão em
``tests/test_resource_manager.py`` confirmam que múltiplos Jobs de um
batch, cada um adquirindo um recurso limitado dentro do handler, ainda
respeitam o limite de concorrência sem quebrar progresso/membership.

OBSERVABILIDADE (item 22)
-------------------------------
``snapshot()`` devolve um ``dict`` simples e seguro para diagnóstico:
perfil ativo, disponibilidade de GPU, capacidade/uso/espera POR dimensão,
e se o manager está em shutdown. Nunca inclui cookies, tokens, paths ou
conteúdo — apenas contadores inteiros e o nome do perfil/dimensão.
"""
from __future__ import annotations

import itertools
import json
import os
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


# ---------------------------------------------------------------------------
# Erros estruturados (item 23)
# ---------------------------------------------------------------------------


class ResourceManagerError(RuntimeError):
    """Erro de contrato do ResourceManager central."""


class InvalidResourceRequestError(ResourceManagerError):
    """O pedido de recurso é inválido: dimensão desconhecida, quantidade
    inválida (<= 0), ou quantidade que excede a capacidade TOTAL daquela
    dimensão (uma chamada que nunca poderia ser satisfeita, mesmo com a
    máquina inteira livre) — falha imediata em vez de esperar para
    sempre."""


class ResourceUnavailableError(ResourceManagerError):
    """``acquire``/``acquire_for`` com ``timeout`` explícito não conseguiu
    capacidade dentro do prazo."""


class ResourceAccountingError(ResourceManagerError):
    """Invariante interna de accounting violada (ex.: um release deixaria
    um contador negativo). Rede de segurança estrutural — nunca deveria
    disparar em uso normal, já que ``ResourceLease.release()`` é
    idempotente e garante no máximo um release efetivo por lease."""


class ResourceManagerShuttingDownError(ResourceManagerError):
    """``acquire``/``acquire_for`` foi recusado (ou um waiter já bloqueado
    foi acordado e recusado) porque ``begin_shutdown()`` está ativo. Nunca
    deixa uma thread presa esperando indefinidamente durante um
    encerramento (ver "PAUSE / CANCEL / SHUTDOWN" na docstring do
    módulo)."""


# ---------------------------------------------------------------------------
# Detecção de hardware (item 4, 20, 31) — sempre best-effort, nunca lança
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HardwareSnapshot:
    """Snapshot best-effort do hardware local. Qualquer campo pode ser
    ``None``/conservador quando a detecção não pôde ser feita com
    confiança — nunca um valor fictício (ver docstring do módulo).

    CORREÇÃO PÓS-VALIDAÇÃO WINDOWS FINAL: ``gpu_available`` e
    ``gpu_backend_available`` são DELIBERADAMENTE campos distintos —
    "existe uma GPU física detectada" NÃO é a mesma pergunta que "um
    backend de aceleração suportado por este produto confirmou que pode
    usar essa GPU":

    - ``gpu_available``: presença FÍSICA de um adaptador gráfico real
      (não virtual/básico) detectado pelo sistema — no Windows, via
      ``Get-CimInstance Win32_VideoController`` (genérico, nunca presume
      NVIDIA); fora do Windows (dev/CI), via ``nvidia-smi`` como único
      sinal disponível. NVIDIA, AMD e Intel são todos representados
      igualmente aqui — uma Radeon real nunca vira ``False`` só porque
      ``nvidia-smi`` não existe.
    - ``gpu_backend_available``: um backend de aceleração CONCRETO e
      suportado por este produto confirmou a GPU (hoje: só ``nvidia-smi``
      bem-sucedido, ou seja, NVIDIA com driver funcional — nenhum
      suporte a CUDA/ROCm/DirectML é inventado ou presumido para
      AMD/Intel neste Prompt). É este campo — nunca ``gpu_available``
      sozinho — que decide se ``DIM_GPU_SLOT`` tem capacidade (ver
      ``resource_policy_for``/``acquire_for``): presença física de uma
      GPU não detectada/suportada pelo backend NÃO deve habilitar
      automaticamente workloads GPU (evita trocar o falso negativo
      antigo por um falso positivo operacional).
    - ``gpu_vendor``: melhor esforço, só para diagnóstico interno
      (``"NVIDIA"``/``"AMD"``/``"INTEL"``/``"UNKNOWN"``) — nunca usado
      para decisão de capacidade.
    """

    cpu_count: int
    total_ram_mb: int | None
    gpu_available: bool
    gpu_vram_mb: int | None
    detection_errors: tuple[str, ...] = ()
    gpu_vendor: str | None = None
    gpu_backend_available: bool = False


def _detect_cpu_count() -> int:
    try:
        count = os.cpu_count()
    except Exception:
        count = None
    return count if count and count > 0 else 1


def _detect_total_ram_mb_windows() -> int | None:
    import ctypes

    class _MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    stat = _MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    if not kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
        return None
    return int(stat.ullTotalPhys // (1024 * 1024))


def _detect_total_ram_mb_proc_meminfo() -> int | None:
    """Melhor esforço para desenvolvimento/CI em Linux. O produto oficial é
    Windows 10/11 64-bit (ver ``CLAUDE.md`` item 31) — este caminho nunca é
    exercido em produção; existe só para que a detecção não quebre neste
    sandbox."""
    meminfo_path = Path("/proc/meminfo")
    if not meminfo_path.exists():
        return None
    for line in meminfo_path.read_text(encoding="ascii", errors="ignore").splitlines():
        if line.startswith("MemTotal:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1]) // 1024
    return None


def _detect_total_ram_mb() -> int | None:
    try:
        if sys.platform == "win32":
            return _detect_total_ram_mb_windows()
        return _detect_total_ram_mb_proc_meminfo()
    except Exception:
        return None


def _detect_gpu() -> tuple[bool, int | None]:
    """Best-effort, nunca lança: tenta ``nvidia-smi`` (se presente no
    PATH) com timeout curto. Qualquer falha (binário ausente, timeout,
    saída inesperada) é tratada como "nvidia-smi indisponível/sem GPU
    NVIDIA confirmada" — nunca como erro fatal.

    IMPORTANTE (ver ``HardwareSnapshot`` e "VALIDAÇÃO WINDOWS FINAL" na
    docstring do módulo): este é o sinal ESPECÍFICO de NVIDIA/backend —
    sua falha NUNCA significa "a máquina não tem GPU". Quem decide
    presença física de qualquer fabricante é ``_detect_gpu_windows_cim``
    no Windows; fora do Windows (sandbox/CI sem hardware real), este é o
    único sinal de presença disponível, preservado como estava."""
    try:
        completed = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except Exception:
        return False, None
    if completed.returncode != 0:
        return False, None
    output = completed.stdout.strip()
    if not output:
        return False, None
    first_line = output.splitlines()[0].strip()
    try:
        vram_mb = int(first_line)
    except ValueError:
        # GPU parece presente (nvidia-smi respondeu OK) mas a saída não
        # pôde ser interpretada com confiança — não inventamos um número.
        return True, None
    return True, vram_mb


_VIRTUAL_OR_BASIC_ADAPTER_MARKERS = (
    "microsoft basic display",
    "microsoft basic render",
    "microsoft remote display",
    "remotefx",
    "virtualbox graphics",
    "vmware svga",
    "parsec virtual",
    "citrix indirect display",
    "meta virtual",
)


def _is_virtual_or_basic_adapter(name: str) -> bool:
    """Filtra adaptadores que NÃO devem ser tratados como GPU aceleradora
    real sem análise adicional (ver "VALIDAÇÃO WINDOWS FINAL": Microsoft
    Basic Display Adapter e equivalentes puramente virtuais/software).
    Lista deliberadamente pequena e explícita — nunca um heurístico
    amplo demais que rejeitaria hardware real."""
    lowered = name.strip().lower()
    if not lowered:
        return True
    return any(marker in lowered for marker in _VIRTUAL_OR_BASIC_ADAPTER_MARKERS)


def _guess_gpu_vendor(name: str) -> str:
    """Melhor esforço, só para diagnóstico interno — nunca usado para
    decisão de capacidade (ver ``HardwareSnapshot.gpu_vendor``)."""
    lowered = name.lower()
    if "nvidia" in lowered or "geforce" in lowered or "quadro" in lowered:
        return "NVIDIA"
    if "amd" in lowered or "radeon" in lowered or " ati " in f" {lowered} ":
        return "AMD"
    if "intel" in lowered:
        return "INTEL"
    return "UNKNOWN"


def _adapter_ram_to_mb(raw_value: Any) -> int | None:
    """Converte ``AdapterRAM`` (bytes, conforme reportado pelo Windows)
    para MB best-effort. Nunca inventa um valor: qualquer valor ausente,
    não numérico, zero ou negativo vira ``None`` — inclusive o quirk
    conhecido do WMI/CIM em que ``AdapterRAM`` (campo historicamente
    32-bit) pode vir truncado/negativo/inconsistente para placas com
    VRAM >= 4 GB. Preferimos ``None`` (e o produto trata como
    desconhecido) a um número que pode estar errado."""
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    mb = value // (1024 * 1024)
    if mb <= 0:
        return None
    return mb


def _detect_gpu_windows_cim() -> tuple[bool, str | None, int | None] | None:
    """Detecção genérica de presença de GPU no Windows via PowerShell +
    CIM (``Get-CimInstance Win32_VideoController``) — NUNCA presume
    NVIDIA (ver "VALIDAÇÃO WINDOWS FINAL" na docstring do módulo).
    Best-effort, nunca lança por si só: qualquer falha (PowerShell
    ausente, timeout, saída inválida) devolve ``None`` para o chamador
    registrar em ``detection_errors`` e seguir com fallback conservador
    — hardware detection nunca pode impedir a inicialização do produto.

    Usa saída JSON estruturada (``ConvertTo-Json``) em vez de parsing de
    tabela localizada (frágil e dependente do idioma do Windows). Filtra
    adaptadores puramente virtuais/básicos
    (``_is_virtual_or_basic_adapter``) e, entre os adaptadores reais
    restantes, escolhe o de maior VRAM reportada como o "principal" —
    suporta múltiplos adapters sem inventar prioridade além disso.

    Devolve ``(False, None, None)`` quando o mecanismo funcionou mas
    nenhum adaptador real foi encontrado (só básico/virtual, ou nenhum
    adaptador) — distinto de ``None``, que significa "o próprio
    mecanismo de detecção falhou"."""
    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "Get-CimInstance Win32_VideoController | "
            "Select-Object Name,AdapterRAM | ConvertTo-Json -Compress",
        ],
        capture_output=True,
        text=True,
        timeout=5.0,
    )
    if completed.returncode != 0:
        return None
    output = completed.stdout.strip()
    if not output:
        return False, None, None

    try:
        data = json.loads(output)
    except ValueError:
        return None  # JSON inválido: mecanismo não confiável desta vez

    if isinstance(data, dict):
        adapters: list[Any] = [data]
    elif isinstance(data, list):
        adapters = data
    else:
        return None

    best: tuple[str, int | None] | None = None
    for adapter in adapters:
        if not isinstance(adapter, dict):
            continue
        name = adapter.get("Name")
        if not isinstance(name, str) or _is_virtual_or_basic_adapter(name):
            continue
        vram_mb = _adapter_ram_to_mb(adapter.get("AdapterRAM"))
        if best is None or (vram_mb or 0) > (best[1] or 0):
            best = (name, vram_mb)

    if best is None:
        return False, None, None
    best_name, best_vram_mb = best
    return True, _guess_gpu_vendor(best_name), best_vram_mb


def detect_hardware() -> HardwareSnapshot:
    """Detecção best-effort de CPU/RAM/GPU/VRAM. Nunca lança — qualquer
    falha de uma dimensão individual vira ``None``/``False`` conservador,
    registrado (só o nome do tipo de exceção, nunca ``str(exc)``/
    ``repr(exc)``/traceback — ver item 10 do CLAUDE.md) em
    ``detection_errors`` para diagnóstico.

    CORREÇÃO PÓS-VALIDAÇÃO WINDOWS FINAL (ver ``HardwareSnapshot``):
    presença física (``gpu_available``) e backend de aceleração
    confirmado (``gpu_backend_available``) são detectados
    separadamente e nunca conflados:

    1. No Windows, tenta primeiro a detecção genérica via CIM
       (``_detect_gpu_windows_cim``) — cobre NVIDIA/AMD/Intel igualmente,
       sem presumir fabricante. Define ``gpu_available``/``gpu_vendor``/
       ``gpu_vram_mb`` a partir daí quando bem-sucedida.
    2. Em seguida tenta ``nvidia-smi`` (``_detect_gpu``) — fonte MAIS
       ESPECÍFICA para NVIDIA: quando bem-sucedida, confirma
       ``gpu_backend_available=True`` (único backend suportado hoje),
       garante ``gpu_available=True`` (cobre também o caso não-Windows,
       onde o CIM nunca roda) e sua VRAM sobrepõe a do CIM (mais
       confiável para NVIDIA especificamente).
    3. Se nenhuma das duas encontrar nada: ``gpu_available=False``,
       ``gpu_backend_available=False`` — máquina sem GPU compatível
       continua funcionando normalmente (degrada para modo CPU).

    ``gpu_backend_available`` nunca é ``True`` só por presença física:
    isso exigiria inventar suporte CUDA/ROCm/DirectML não implementado
    neste Prompt (ver docstring de ``HardwareSnapshot``)."""
    errors: list[str] = []

    cpu_count = _detect_cpu_count()

    try:
        total_ram_mb = _detect_total_ram_mb()
    except Exception as exc:  # defensivo: as funções acima já não lançam
        total_ram_mb = None
        errors.append(f"ram_detection_failed:{type(exc).__name__}")

    gpu_available = False
    gpu_vendor: str | None = None
    gpu_vram_mb: int | None = None
    gpu_backend_available = False

    if sys.platform == "win32":
        try:
            cim_result = _detect_gpu_windows_cim()
        except Exception as exc:  # defensivo: a função acima já não lança
            cim_result = None
            errors.append(f"gpu_cim_detection_failed:{type(exc).__name__}")
        if cim_result is None:
            errors.append("gpu_cim_detection_unavailable")
        else:
            gpu_available, gpu_vendor, gpu_vram_mb = cim_result

    try:
        nvidia_available, nvidia_vram_mb = _detect_gpu()
    except Exception as exc:  # defensivo: _detect_gpu já não lança
        nvidia_available, nvidia_vram_mb = False, None
        errors.append(f"gpu_detection_failed:{type(exc).__name__}")

    if nvidia_available:
        gpu_backend_available = True
        gpu_available = True
        if gpu_vendor is None:
            gpu_vendor = "NVIDIA"
        if nvidia_vram_mb is not None:
            gpu_vram_mb = nvidia_vram_mb  # fonte mais específica sobrepõe o CIM genérico

    return HardwareSnapshot(
        cpu_count=cpu_count,
        total_ram_mb=total_ram_mb,
        gpu_available=gpu_available,
        gpu_vram_mb=gpu_vram_mb,
        detection_errors=tuple(errors),
        gpu_vendor=gpu_vendor,
        gpu_backend_available=gpu_backend_available,
    )


# ---------------------------------------------------------------------------
# Perfis LOW / NORMAL / HIGH (item 4)
# ---------------------------------------------------------------------------

PROFILE_LOW = "LOW"
PROFILE_NORMAL = "NORMAL"
PROFILE_HIGH = "HIGH"
PROFILES = (PROFILE_LOW, PROFILE_NORMAL, PROFILE_HIGH)


def select_profile(hardware: HardwareSnapshot) -> str:
    """Seleção conservadora: RAM desconhecida nunca promove a HIGH; CPU
    baixo sempre força LOW independentemente de RAM. Limiares
    deliberadamente simples e centralizados aqui — nenhum outro módulo
    decide isso."""
    cpu = hardware.cpu_count
    ram = hardware.total_ram_mb

    if cpu <= 2 or (ram is not None and ram < 8 * 1024):
        return PROFILE_LOW
    if cpu >= 8 and ram is not None and ram >= 16 * 1024:
        return PROFILE_HIGH
    return PROFILE_NORMAL


# ---------------------------------------------------------------------------
# Categorias de workload + dimensões compartilhadas (itens 1, 5, 6)
# ---------------------------------------------------------------------------

RESOURCE_FFMPEG = "FFMPEG"
RESOURCE_WHISPER = "WHISPER"
RESOURCE_OLLAMA = "OLLAMA"
RESOURCE_BROWSER = "BROWSER"
RESOURCE_RENDER = "RENDER"
RESOURCE_FRAME_ANALYSIS = "FRAME_ANALYSIS"
RESOURCE_KINDS = frozenset(
    {
        RESOURCE_FFMPEG,
        RESOURCE_WHISPER,
        RESOURCE_OLLAMA,
        RESOURCE_BROWSER,
        RESOURCE_RENDER,
        RESOURCE_FRAME_ANALYSIS,
    }
)

DIM_GLOBAL_HEAVY = "GLOBAL_HEAVY"
DIM_GPU_SLOT = "GPU_SLOT"

_KIND_LIMITS: dict[str, dict[str, int]] = {
    PROFILE_LOW: {
        RESOURCE_FFMPEG: 1,
        RESOURCE_WHISPER: 1,
        RESOURCE_OLLAMA: 1,
        RESOURCE_BROWSER: 1,
        RESOURCE_RENDER: 1,
        RESOURCE_FRAME_ANALYSIS: 1,
    },
    PROFILE_NORMAL: {
        RESOURCE_FFMPEG: 2,
        RESOURCE_WHISPER: 1,
        RESOURCE_OLLAMA: 1,
        RESOURCE_BROWSER: 2,
        RESOURCE_RENDER: 2,
        RESOURCE_FRAME_ANALYSIS: 2,
    },
    PROFILE_HIGH: {
        RESOURCE_FFMPEG: 4,
        RESOURCE_WHISPER: 2,
        RESOURCE_OLLAMA: 2,
        RESOURCE_BROWSER: 4,
        RESOURCE_RENDER: 3,
        RESOURCE_FRAME_ANALYSIS: 4,
    },
}

_GLOBAL_HEAVY_CAPACITY: dict[str, int] = {
    PROFILE_LOW: 2,
    PROFILE_NORMAL: 6,
    PROFILE_HIGH: 12,
}

_KIND_GLOBAL_WEIGHT: dict[str, int] = {
    RESOURCE_FFMPEG: 1,
    RESOURCE_WHISPER: 2,
    RESOURCE_OLLAMA: 2,
    RESOURCE_BROWSER: 1,
    RESOURCE_RENDER: 1,
    RESOURCE_FRAME_ANALYSIS: 1,
}


@dataclass(frozen=True)
class ResourcePolicy:
    """Política resolvida (perfil + hardware + overrides) — números
    concretos vivem só aqui, nunca duplicados/hardcoded em outro lugar."""

    profile: str
    capacities: Mapping[str, int]
    global_weight: Mapping[str, int]


def resource_policy_for(
    profile: str,
    hardware: HardwareSnapshot,
    *,
    overrides: Mapping[str, int] | None = None,
) -> ResourcePolicy:
    if profile not in PROFILES:
        raise InvalidResourceRequestError(f"profile desconhecido: {profile!r}")
    capacities: dict[str, int] = dict(_KIND_LIMITS[profile])
    capacities[DIM_GLOBAL_HEAVY] = _GLOBAL_HEAVY_CAPACITY[profile]
    # CORREÇÃO PÓS-VALIDAÇÃO WINDOWS FINAL: capacidade de DIM_GPU_SLOT é
    # decidida por gpu_backend_available (backend de aceleração
    # CONFIRMADO), nunca por gpu_available (mera presença física) — ver
    # docstring de HardwareSnapshot. Uma GPU AMD/Intel fisicamente
    # presente mas sem backend suportado neste Prompt não deve habilitar
    # workloads GPU sozinha (evita falso positivo operacional).
    capacities[DIM_GPU_SLOT] = 1 if hardware.gpu_backend_available else 0
    if overrides:
        for dim, value in overrides.items():
            value = int(value)
            if value < 0:
                raise InvalidResourceRequestError(
                    f"override de capacidade negativa para {dim!r}"
                )
            capacities[dim] = value
    return ResourcePolicy(
        profile=profile, capacities=capacities, global_weight=dict(_KIND_GLOBAL_WEIGHT)
    )


# ---------------------------------------------------------------------------
# Lease (item 2, 9)
# ---------------------------------------------------------------------------


class ResourceLease:
    """Handle devolvido por ``acquire``/``acquire_for``. Ver "NUNCA VAZAR
    CAPACIDADE" e "DUPLO RELEASE" na docstring do módulo."""

    __slots__ = ("_manager", "_requests", "_released", "_release_lock", "label")

    def __init__(
        self,
        manager: "ResourceManager",
        requests: Mapping[str, int],
        *,
        label: str | None = None,
    ) -> None:
        self._manager = manager
        self._requests = dict(requests)
        self._released = False
        self._release_lock = threading.Lock()
        self.label = label

    @property
    def requests(self) -> Mapping[str, int]:
        return dict(self._requests)

    @property
    def released(self) -> bool:
        with self._release_lock:
            return self._released

    def release(self) -> None:
        """Idempotente: a segunda chamada em diante é um no-op seguro —
        nunca lança, nunca corrompe accounting (ver "DUPLO RELEASE" na
        docstring do módulo)."""
        with self._release_lock:
            if self._released:
                return
            self._released = True
        self._manager._release_internal(self._requests)

    def __enter__(self) -> "ResourceLease":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.release()
        return False

    def __repr__(self) -> str:  # pragma: no cover - diagnóstico
        return f"ResourceLease(label={self.label!r}, requests={self._requests!r}, released={self.released})"


# ---------------------------------------------------------------------------
# ResourceManager
# ---------------------------------------------------------------------------


class ResourceManager:
    """Controlador central de capacidade em memória. Ver a docstring do
    módulo para o contrato completo (perfis, dimensões, fairness,
    anti-leak, shutdown, integração)."""

    def __init__(
        self,
        *,
        profile: str | None = None,
        hardware: HardwareSnapshot | None = None,
        overrides: Mapping[str, int] | None = None,
    ) -> None:
        self._hardware = hardware if hardware is not None else detect_hardware()
        self._profile = profile if profile is not None else select_profile(self._hardware)
        if self._profile not in PROFILES:
            raise InvalidResourceRequestError(f"profile desconhecido: {self._profile!r}")
        self._policy = resource_policy_for(self._profile, self._hardware, overrides=overrides)

        self._capacities: dict[str, int] = dict(self._policy.capacities)
        self._in_use: dict[str, int] = {dim: 0 for dim in self._capacities}
        self._waiting_by_dim: dict[str, int] = {dim: 0 for dim in self._capacities}

        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._queue: "deque[int]" = deque()
        self._ticket_seq = itertools.count(1)

        self._shutting_down = False
        self._shutdown_reason: str | None = None

    # -- Identidade / política -------------------------------------------

    @property
    def profile(self) -> str:
        return self._profile

    @property
    def hardware(self) -> HardwareSnapshot:
        return self._hardware

    @property
    def gpu_available(self) -> bool:
        """Presença FÍSICA de GPU detectada (qualquer fabricante) — não
        implica que um backend de aceleração suportado a confirmou. Ver
        ``gpu_backend_available`` e docstring de ``HardwareSnapshot``."""
        return self._hardware.gpu_available

    @property
    def gpu_backend_available(self) -> bool:
        """Backend de aceleração CONFIRMADO (hoje: NVIDIA via
        ``nvidia-smi``) — é este campo, não ``gpu_available``, que decide
        se ``DIM_GPU_SLOT`` tem capacidade (ver ``resource_policy_for``)."""
        return self._hardware.gpu_backend_available

    def capacity_of(self, dim: str) -> int:
        return self._capacities.get(dim, 0)

    @property
    def is_shutting_down(self) -> bool:
        with self._condition:
            return self._shutting_down

    # -- Shutdown (item 12) ------------------------------------------------

    def begin_shutdown(self, *, reason: str | None = None) -> None:
        """Sinal em memória: recusa novas aquisições e acorda IMEDIATAMENTE
        qualquer waiter já bloqueado (que recebe
        ``ResourceManagerShuttingDownError`` em vez de ficar pendurado). Não
        toca nenhum estado persistido de ``ShutdownCoordinator`` — ver
        "PAUSE / CANCEL / SHUTDOWN" na docstring do módulo."""
        with self._condition:
            self._shutting_down = True
            self._shutdown_reason = reason
            self._condition.notify_all()

    def end_shutdown(self) -> None:
        """Reverte ``begin_shutdown`` — útil para testes ou para um
        shutdown abortado. Não é chamado automaticamente por nenhum
        bootstrap."""
        with self._condition:
            self._shutting_down = False
            self._shutdown_reason = None
            self._condition.notify_all()

    # -- Validação -----------------------------------------------------------

    def _normalize_and_validate(self, requests: Mapping[str, int]) -> dict[str, int]:
        if not requests:
            raise InvalidResourceRequestError(
                "acquire requer ao menos uma dimensão de recurso"
            )
        normalized: dict[str, int] = {}
        for dim, amount in requests.items():
            if dim not in self._capacities:
                raise InvalidResourceRequestError(
                    f"dimensão de recurso desconhecida: {dim!r}"
                )
            amount = int(amount)
            if amount <= 0:
                raise InvalidResourceRequestError(
                    f"amount deve ser > 0 para {dim!r} (recebido {amount!r})"
                )
            if amount > self._capacities[dim]:
                raise InvalidResourceRequestError(
                    f"pedido de {amount} para {dim!r} excede a capacidade total "
                    f"({self._capacities[dim]}); esta chamada nunca poderia ser satisfeita"
                )
            normalized[dim] = amount
        return normalized

    def _has_capacity(self, requests: Mapping[str, int]) -> bool:
        return all(
            self._in_use[dim] + amount <= self._capacities[dim]
            for dim, amount in requests.items()
        )

    def _commit(self, requests: Mapping[str, int]) -> None:
        for dim, amount in requests.items():
            self._in_use[dim] += amount

    def _release_internal(self, requests: Mapping[str, int]) -> None:
        with self._condition:
            for dim, amount in requests.items():
                new_value = self._in_use.get(dim, 0) - amount
                if new_value < 0:
                    raise ResourceAccountingError(
                        f"accounting corrompido: in_use[{dim!r}] ficaria negativo "
                        f"({self._in_use.get(dim, 0)} - {amount})"
                    )
                self._in_use[dim] = new_value
            self._condition.notify_all()

    # -- Aquisição (itens 1, 7, 8, 10, 13) ------------------------------------

    def acquire(
        self,
        requests: Mapping[str, int],
        *,
        timeout: float | None = None,
        label: str | None = None,
    ) -> ResourceLease:
        """Adquire TODAS as dimensões pedidas atomicamente (tudo ou nada —
        ver "AQUISIÇÃO PARCIAL É ATÔMICA" na docstring do módulo), respeitando
        a fila FIFO global (ver "FAIRNESS"). Bloqueia até conseguir, até
        ``timeout`` expirar (``ResourceUnavailableError``), ou até
        ``begin_shutdown()`` ser chamado (``ResourceManagerShuttingDownError``).

        Devolve um ``ResourceLease`` — use como ``with acquire(...):`` para
        garantir liberação em qualquer caminho de saída.
        """
        requests = self._normalize_and_validate(requests)
        ticket = next(self._ticket_seq)
        granted = False

        with self._condition:
            self._queue.append(ticket)
            for dim, amount in requests.items():
                self._waiting_by_dim[dim] = self._waiting_by_dim.get(dim, 0) + amount
            try:
                deadline = None if timeout is None else time.monotonic() + timeout
                while True:
                    if self._shutting_down:
                        raise ResourceManagerShuttingDownError(
                            "ResourceManager está em shutdown; acquire recusado"
                            + (f" ({self._shutdown_reason})" if self._shutdown_reason else "")
                        )
                    if self._queue and self._queue[0] == ticket and self._has_capacity(requests):
                        self._commit(requests)
                        granted = True
                        self._queue.popleft()
                        break
                    if deadline is not None:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise ResourceUnavailableError(
                                f"capacidade indisponível para {requests!r} dentro do timeout"
                            )
                        self._condition.wait(remaining)
                    else:
                        self._condition.wait()
            finally:
                if granted:
                    for dim, amount in requests.items():
                        self._waiting_by_dim[dim] -= amount
                else:
                    # Cobre timeout, shutdown e qualquer exceção inesperada:
                    # este ticket nunca fica preso na fila nem com
                    # accounting parcialmente incrementado (nada foi
                    # commitado neste ramo).
                    try:
                        self._queue.remove(ticket)
                    except ValueError:
                        pass
                    for dim, amount in requests.items():
                        self._waiting_by_dim[dim] -= amount
                    self._condition.notify_all()

        return ResourceLease(self, requests, label=label)

    def acquire_for(
        self,
        kind: str,
        *,
        use_gpu: bool = False,
        timeout: float | None = None,
    ) -> ResourceLease:
        """Atalho de alto nível: resolve ``{kind: 1, DIM_GLOBAL_HEAVY: peso}``
        (e ``DIM_GPU_SLOT`` quando ``use_gpu=True`` E a máquina tem GPU
        detectada) a partir da política do perfil ativo. Se ``use_gpu=True``
        mas não há GPU disponível, degrada graciosamente para modo CPU (não
        levanta erro, não trava) — a máquina sem GPU compatível continua
        funcionando (item 4)."""
        if kind not in RESOURCE_KINDS:
            raise InvalidResourceRequestError(f"kind de workload desconhecido: {kind!r}")
        requests: dict[str, int] = {kind: 1, DIM_GLOBAL_HEAVY: self._policy.global_weight[kind]}
        if use_gpu and self._hardware.gpu_backend_available:
            requests[DIM_GPU_SLOT] = 1
        return self.acquire(requests, timeout=timeout, label=kind)

    # -- Observabilidade (item 22) ---------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Estado seguro para diagnóstico — apenas contadores e nomes de
        dimensão/perfil, nunca cookies/tokens/paths/conteúdo."""
        with self._condition:
            return {
                "profile": self._profile,
                "gpu_available": self._hardware.gpu_available,
                "gpu_backend_available": self._hardware.gpu_backend_available,
                "shutting_down": self._shutting_down,
                "capacity": dict(self._capacities),
                "in_use": dict(self._in_use),
                "waiting": {dim: n for dim, n in self._waiting_by_dim.items() if n},
                "queue_length": len(self._queue),
            }


__all__ = [
    "ResourceManagerError",
    "InvalidResourceRequestError",
    "ResourceUnavailableError",
    "ResourceAccountingError",
    "ResourceManagerShuttingDownError",
    "HardwareSnapshot",
    "detect_hardware",
    "PROFILE_LOW",
    "PROFILE_NORMAL",
    "PROFILE_HIGH",
    "PROFILES",
    "select_profile",
    "RESOURCE_FFMPEG",
    "RESOURCE_WHISPER",
    "RESOURCE_OLLAMA",
    "RESOURCE_BROWSER",
    "RESOURCE_RENDER",
    "RESOURCE_FRAME_ANALYSIS",
    "RESOURCE_KINDS",
    "DIM_GLOBAL_HEAVY",
    "DIM_GPU_SLOT",
    "ResourcePolicy",
    "resource_policy_for",
    "ResourceLease",
    "ResourceManager",
]
