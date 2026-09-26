# -*- coding: utf-8 -*-
"""BatchEngine central — orquestração de lotes (FASE 3 / PROMPT 17).

Este módulo é deliberadamente independente de apresentação e de integrações
remotas, no mesmo espírito de ``job_engine.py``/``control_manager.py``/
``shutdown_coordinator.py``:

- não importa nem conhece UI/frontend;
- não importa nem conhece YouTube, TikTok, Instagram ou qualquer outro
  Connector;
- não conhece FFmpeg, Whisper, Ollama nem qualquer executor de mídia;
- lê e persiste Batches exclusivamente através de ``LocalDatabase``
  (SQLite local).

ESCOPO DELIBERADO: BATCHENGINE É SÓ ORQUESTRAÇÃO
----------------------------------------------------
``BatchEngine`` NÃO implementa (etapas futuras, fora do escopo deste
Prompt): ``ResourceManager`` (PROMPT 18 — CPU/RAM/GPU/VRAM), ``StorageManager``
(PROMPT 19), ``RetryPolicy``/idempotência completa por ``operation``
(PROMPT 21/22), importação de fontes (PROMPT 24), executores de mídia,
connectors ou UI final.

``Job`` continua sendo a única unidade operacional real. Este módulo nunca
duplica o ``JobEngine``:

- toda execução real de um Job continua passando por ``JobEngine.advance``/
  ``JobEngine._claim`` — ``BatchEngine`` nunca transiciona ``Job.status``
  diretamente, em nenhum método (criação, pause, cancel, retry);
- a State Machine central (``_sistema/domain/job_state_machine.py``)
  continua sendo a fonte de verdade sobre o que cada Job pode fazer a
  seguir;
- o Audit Log (``OperationalAuditLog``) continua sendo a trilha auditável;
- ``RecoveryManager`` continua sendo a única autoridade de recuperação após
  crash — ``BatchEngine`` NUNCA cria um segundo mecanismo de recovery: um
  Job abandonado (``PROCESSING``/``INTERRUPTED``/``PUBLISHING``/``UNKNOWN``/
  ``RECOVERING``) continua sendo detectado e resolvido exclusivamente por
  ``RecoveryManager.recover_at_startup()``, batch ou não; ``BatchEngine`` só
  volta a enxergar esse Job (via ``get_progress``/``advance_batch``) depois
  que o RecoveryManager já o pousou em um destino seguro;
- ``ControlManager`` continua sendo responsável pelo pause/resume/
  stop_after_current/cancel já aprovados (PROMPT 15) — ``BatchEngine``
  reaproveita ``ControlManager.cancel(JOB, ...)`` integralmente para
  cancelamento individual e em massa, em vez de reinventar essa lógica;
- ``ShutdownCoordinator`` continua bloqueando toda admissão de trabalho
  novo durante DRAINING (PROMPT 16) — isso já vale automaticamente para
  batches, porque ``advance_batch`` delega a reivindicação real ao
  ``JobEngine`` já integrado a ele.

INTEGRAÇÃO COM O JOBENGINE: BATCH PAUSADO NUNCA É UMA SEGUNDA FILA
-----------------------------------------------------------------------
"BatchEngine não pode alterar Job.status diretamente" (regra explícita do
Prompt) significa que pausar um batch não pode fazer este módulo forçar
nenhuma transição. Em vez disso, ``BatchEngine`` expõe um predicado somente
leitura, ``is_job_admission_blocked(job_id, *, connection=None)``, e
``JobEngine`` passa a aceitar um ``batch_engine`` opcional (mesmo padrão já
usado para ``control_manager``/``shutdown_coordinator``): quando fornecido,
``JobEngine._claim()`` consulta esse predicado dentro da MESMA transaction
``BEGIN IMMEDIATE`` da própria reivindicação (ver ``job_engine.py``). Isso
fecha a corrida pause-vs-claim com a idêntica disciplina transacional já
usada para pause/stop_after_current/DRAINING: quem commitar primeiro (a
pausa do batch, ou a reivindicação do Job) decide o resultado de forma
determinística, sem nenhum lock novo em memória — a garantia é inteiramente
do SQLite, válida entre processos diferentes.

CONTROLMANAGER COMPARTILHADO: NUNCA SPLIT-BRAIN
-----------------------------------------------------
CORREÇÃO PÓS-REVISÃO ADVERSARIAL (BLOQUEADOR 2): ``BatchEngine`` e o
``JobEngine`` que ele usa para executar Jobs (fornecido pelo chamador ou
criado internamente) precisam SEMPRE concordar sobre qual ``ControlManager``
está em vigor. Antes desta correção, passar um ``JobEngine`` já construído
SEM ``control_manager`` (``JobEngine(db)``) fazia ``BatchEngine`` criar um
``ControlManager`` só para si mesmo, sem anexá-lo ao ``JobEngine`` fornecido
— resultado: ``batch.control_manager.pause(GLOBAL)`` não tinha efeito
nenhum sobre ``advance_batch``, porque o ``JobEngine`` usado para executar
continuava com ``control_manager is None`` e simplesmente nunca verificava
pause/cancel algum (não é uma discordância entre duas instâncias — era a
ausência total de verificação em um dos dois lados).

A regra agora é explícita e determinística, resolvida uma única vez no
``__init__``:

1. se o ``job_engine`` fornecido já tem um ``control_manager`` (não
   ``None``): essa é a instância que efetivamente decide admissão de Jobs,
   então ``BatchEngine`` REUTILIZA exatamente ela. Se o chamador também
   passou um ``control_manager`` explícito para ``BatchEngine`` e ele for
   uma instância DIFERENTE, isso é uma configuração ambígua — nunca
   escolhida silenciosamente — e levanta ``ControlManagerMismatchError``;
2. se o ``job_engine`` fornecido não tem ``control_manager`` (``None``):
   ``BatchEngine`` usa o ``control_manager`` explícito (se houver) ou cria
   um novo, e ANEXA essa mesma instância ao ``job_engine`` fornecido
   (``job_engine.control_manager = self.control_manager`` — atributo
   público simples, mesmo padrão de ``batch_engine`` em ``JobEngine``);
3. se nenhum ``job_engine`` foi fornecido, ``BatchEngine`` cria um
   ``JobEngine`` novo já com o ``control_manager`` resolvido.

Ao final da construção, ``self.job_engine.control_manager is self.control_manager``
é sempre verdadeiro — nunca dois ``ControlManager`` controlando o mesmo
fluxo de execução sem se conhecerem.

PERSISTÊNCIA: TABELAS DEDICADAS PARA IDENTIDADE/MEMBERSHIP, SETTINGS PARA
FLAGS OPERACIONAIS PEQUENOS
-----------------------------------------------------------------------------
Reavaliação crítica (conforme CLAUDE.md) descartou um único JSON gigante em
``settings`` para representar um batch inteiro: em 500+ itens isso exigiria
reescrever o blob inteiro a cada mudança de progresso (na prática progresso
nem é armazenado — é sempre reconstruído dos Jobs, ver abaixo), tornaria
consultas de membership O(n) sobre um blob em vez de um índice, e
dificultaria concorrência real entre duas instâncias de ``BatchEngine``
(duas escritas concorrentes no mesmo JSON colidiriam no nível do blob
inteiro, não por Job).

Por isso este Prompt introduz uma migration nova, m003 (``batches`` +
``batch_jobs``, ver ``storage/migrations/m003_batch_engine.py``): tabelas
SQL dedicadas, indexadas, meio natural para consulta/join eficiente com
``jobs`` e para 500+ itens sem O(n²). m001/m002 permanecem intocadas
(byte-idênticas).

Os dois únicos dados verdadeiramente pequenos, de leitura pontual e sem
necessidade de índice — "este batch está pausado?" e "existe um
cancelamento em massa deste batch em andamento?" — continuam usando
``settings`` (chave -> valor JSON pequeno), o mesmo padrão já aprovado e
testado em ``ControlManager``/``ShutdownCoordinator``. Nenhuma migration
nova foi necessária para isso.

MEMBERSHIP: UM JOB PERTENCE A NO MÁXIMO UM BATCH
-----------------------------------------------------
Decisão explícita (ver ``JobAlreadyInBatchError``): ``batch_jobs.job_id`` é
``UNIQUE``, reforçado pelo próprio SQLite (não apenas por lógica de
aplicação — vale mesmo entre duas instâncias de ``BatchEngine`` concorrentes
contra o mesmo arquivo). Um Job nunca "troca de batch" sozinho, mesmo depois
de um restart: a membership é a linha persistida em ``batch_jobs``, nunca
inferida de ``operation``, ``created_at`` ou posição na fila. Justificativa:
permitir múltiplos batches por Job tornaria "de qual batch este Job é"
ambíguo exatamente no cenário que o roadmap pede para nunca deixar em
aberto (progresso, pause/resume e cancelamento por batch deixariam de ter
um dono único). Se uma necessidade futura de Jobs compartilhados entre
batches surgir, ela pertence a uma decisão arquitetural nova e explícita,
não a uma extensão silenciosa deste contrato.

CRIAÇÃO ATÔMICA: TUDO OU NADA
----------------------------------
``create_batch``/``create_batch_from_existing_jobs`` gravam a linha do
batch, todos os Jobs (quando aplicável) e toda a membership em UMA ÚNICA
transaction SQLite (``BEGIN IMMEDIATE``, via ``LocalDatabase.transaction()``).
Isso satisfaz literalmente "ou o batch é criado corretamente, ou falha sem
deixar membership parcialmente mentirosa": qualquer erro no meio do laço
(ex.: um ``job_id`` inexistente, um Job que já pertence a outro batch, uma
violação de UNIQUE) reverte a transaction inteira — nenhuma linha em
``batches``/``batch_jobs``/``jobs`` sobra. Não há um estado "criação parcial"
suportado nesta etapa: criação é tudo-ou-nada, nunca um resultado
resumível/parcial explícito (o roadmap permite ambos os desenhos; tudo-ou-
nada foi escolhido por ser mais simples de raciocinar sob crash e não
sacrificar corretude por conveniência).

PROGRESSO: SEMPRE RECONSTRUÍDO DOS JOBS PERSISTIDOS, NUNCA DE UM CONTADOR
EM MEMÓRIA
-----------------------------------------------------------------------------
``get_progress`` faz sempre uma consulta SQL nova (JOIN ``batch_jobs``
com ``jobs``, ``GROUP BY jobs.status``) — nunca mantém nem confia em nenhum
contador cacheado. Isso garante que o progresso relatado logo após um
restart/crash é sempre honesto, mesmo que o processo anterior tenha
morrido no meio de uma execução.

As seis categorias mandatórias do roadmap (``pending``, ``processing``,
``ready``, ``failed``, ``attention_required``) mapeiam os 16 estados de
``Job`` desta forma (ver ``_PROGRESS_*`` abaixo, com um ``assert`` de
partição no import do módulo, no mesmo espírito do
``assert AUTO_ENFORCED_SCOPES | MANUALLY_ENFORCED_SCOPES == CONTROL_SCOPES``
já existente em ``control_manager.py``):

- ``pending``: ``PENDING``, ``RETRY`` — trabalho ainda não iniciado (ou que
  já sabe que precisa ser refeito).
- ``processing``: ``PROCESSING``, ``PUBLISHING``, ``RECOVERING`` — trabalho
  ativo (local ou remoto potencialmente incerto).
- ``ready``: ``READY``, ``PUBLISHED``, ``SCHEDULED`` — resultado alcançado
  com sucesso, em qualquer um dos três pontos finais "bons" da State
  Machine central.
- ``failed``: ``FAILED`` — falha conhecida, exige decisão explícita.
- ``attention_required``: ``BLOCKED``, ``AUTH_REQUIRED``,
  ``USER_ACTION_REQUIRED``, ``UNKNOWN``, ``INTERRUPTED``, ``PAUSED`` — todo
  estado que exige alguma decisão/ação externa antes de prosseguir
  (reconciliação remota, 2FA/CAPTCHA, decisão manual, retomada de pausa,
  etc.).

``CANCELLED`` é deliberadamente excluído das seis (não é "pendente",
"processando", "pronto", "falho" nem "precisa de atenção" — é um estado
terminal à parte, por decisão explícita do usuário/sistema). Por isso este
módulo NUNCA finge que ``sum(seis) == total``: ``BatchProgress`` expõe um
sétimo campo explícito, ``cancelled``, e um ``breakdown`` completo por
``Job.status`` real (incluindo qualquer estado que apareça), com a
invariante ``total == sum(breakdown.values())`` sempre validada. "O
relatório precisa ser honesto" (roadmap) é tratado ao pé da letra: nenhum
estado é escondido.

NUNCA INICIAR TODOS OS JOBS DE UM BATCH SIMULTANEAMENTE
------------------------------------------------------------
``advance_batch(batch_id, limit=DEFAULT_ADMISSION_LIMIT)`` processa no
máximo ``limit`` Jobs PENDING/RETRY deste batch por chamada — nunca todos de
uma vez, mesmo em lotes de 500+. Isto NÃO antecipa o ``ResourceManager``
(PROMPT 18, que vai decidir concorrência real baseada em CPU/RAM/GPU/VRAM):
é apenas uma salvaguarda de orquestração, uma janela de admissão pequena e
explícita. A reivindicação/execução real de cada Job continua inteiramente
delegada a ``JobEngine.advance`` (nunca uma fila paralela); a falha de um
Job nunca impede os demais (mesmo isolamento já usado por
``JobEngine.run_pending``).

PAUSE/RESUME DE BATCH SEM RECRIAR O PROMPT 15
---------------------------------------------------
Pausar um batch (``pause_batch``) NUNCA reescreve o estado de um Job já
``PROCESSING``/``PUBLISHING``/``RECOVERING`` — esses Jobs continuam seguindo
as regras de ponto seguro já aprovadas (``ControlManager``/``JobEngine``).
"Pausar o batch" significa exclusivamente: nenhum Job NOVO deste batch é
admitido por ``JobEngine._claim`` enquanto o flag persistido
(``settings["batch:<id>:paused"]``) estiver ativo — ver
``is_job_admission_blocked``. Não usa (e não deveria usar) o escopo
``QUEUE`` do ``ControlManager`` como substituto: ``QUEUE`` é por
``operation`` e pausaria TODOS os batches (e Jobs avulsos) que compartilham
aquela ``operation``, não só este batch — por isso este módulo tem seu
próprio flag dedicado por ``batch_id``, sem tocar em
``CONTROL_SCOPES``/``ControlManager`` (nenhuma extensão "às cegas" do
ControlManager, conforme o roadmap pede para evitar).

CANCELAMENTO
----------------
``cancel_job`` (item individual) e ``cancel_batch`` (lote inteiro) reusam
integralmente ``ControlManager.cancel(CONTROL_SCOPE_JOB, job_id)`` — o mesmo
contrato seguro já adversarialmente revisado (decide atomicamente cancelar
agora ou adiar quando o Job está com um handler ativo; nunca marca um Job
ativo como CANCELLED fora do ponto seguro correspondente; nunca apaga
``SourceAsset``/artifacts). ``cancel_batch`` persiste a intenção ANTES de
qualquer efeito, em ``settings["batch:<id>:cancel_request"]`` — mesma
disciplina de crash-safety já usada por ``ControlManager`` para GLOBAL/QUEUE:
se o processo cair no meio do laço, uma nova chamada retoma o MESMO pedido
``IN_PROGRESS`` e nunca toca Jobs de outro batch.

CORREÇÃO PÓS-REVISÃO ADVERSARIAL (BLOQUEADOR 1 — cancel_batch IN_PROGRESS não
bloqueava novos claims): antes desta correção, um Job deste batch ainda
``PENDING``/``RETRY`` podia ser reivindicado e executado por
``JobEngine._claim`` mesmo com um ``cancel_batch`` ``IN_PROGRESS`` já
persistido (ex.: processo morreu depois de cancelar o primeiro Job do lote,
mas antes de chegar aos demais) — violando a intenção já persistida do
operador. Corrigido: ``is_job_admission_blocked`` (o mesmo predicado somente-
leitura consultado por ``JobEngine._claim`` dentro do ``BEGIN IMMEDIATE`` da
reivindicação, ver "INTEGRAÇÃO COM O JOBENGINE" acima) agora também retorna
``True`` quando o batch do Job tem um ``cancel_request`` ``IN_PROGRESS``. Se
o pedido de cancelamento commitar antes do claim, o claim é recusado
(``JobBlockedByBatchError``); se o claim vencer a corrida primeiro, o Job já
ativo segue o caminho de cancelamento adiado (``deferred``) que
``ControlManager``/``JobEngine`` já garantem — nenhum lock novo em memória,
mesma disciplina transacional de pause/DRAINING.

CORREÇÃO PÓS-REVISÃO ADVERSARIAL (candidate_job_ids duplicado em settings):
a primeira versão também persistia, dentro do ``cancel_request``, uma cópia
de TODA a membership do batch (``candidate_job_ids``) — redundante e, em
500+ itens, já não é mais um "flag pequeno" (contradiz a própria justificativa
de usar ``settings`` só para dados pontuais). Como a membership de um batch é
IMUTÁVEL após a criação (ver "MEMBERSHIP" acima), a lista de candidatos de um
cancelamento em massa é SEMPRE, deterministicamente, igual a
``list_batch_job_ids(batch_id)`` — recalculá-la a partir de ``batch_jobs`` em
cada retomada produz exatamente o mesmo resultado que guardar uma cópia, sem
duplicar dado nenhum. Por isso ``cancel_request`` em ``settings`` agora
guarda somente metadata pequena (``status``, ``reason``, timestamps) — nunca
uma lista de UUIDs — e ``cancel_batch``/``is_job_admission_blocked`` sempre
consultam ``batch_jobs`` (via ``list_batch_job_ids``) como única fonte de
candidatos, em qualquer chamada (original ou retomada).

REPROCESSAR SOMENTE FALHAS
-------------------------------
``retry_failed`` seleciona estritamente Jobs com ``status == JOB_FAILED``
dentro do batch e usa a transição ``RETRY`` já aprovada
(``OperationalAuditLog.retry``) — nunca uma reescrita direta de status, nunca
um ``RetryPolicy``/retry automático ou infinito (isso é PROMPT 21). Qualquer
Job do batch que não esteja em ``FAILED`` (incluindo ``BLOCKED``/
``AUTH_REQUIRED``/``UNKNOWN``, mesmo quando a State Machine central
permitiria tecnicamente ``-> RETRY`` a partir de alguns desses) é reportado
como não elegível, nunca silenciosamente ignorado nem reprocessado às
cegas.

AUDITORIA MÍNIMA DE BATCH (CORREÇÃO PÓS-REVISÃO ADVERSARIAL)
-------------------------------------------------------------------
Antes desta correção, o único rastro auditável de um batch eram os
``JOB_CREATED`` de cada Job individual — nenhum evento próprio do batch
existia, e ``pause``/``resume``/``cancel_request`` apenas sobrescreviam a
mesma chave em ``settings``, perdendo o histórico operacional (quem pausou,
quando, quantas vezes, com que resultado agregado). Corrigido reaproveitando
integralmente ``audit_events`` (m001/m002, já append-only e protegido contra
UPDATE/DELETE) — nenhuma migration nova foi necessária: ``BatchEngine``
grava eventos pequenos (``entity_type="Batch"``, ``entity_id=batch_id``) para
``BATCH_CREATED``, ``BATCH_PAUSED``, ``BATCH_RESUMED``,
``BATCH_CANCEL_REQUESTED``, ``BATCH_CANCEL_RESUMED``,
``BATCH_CANCEL_COMPLETED`` e ``BATCH_RETRY_REQUESTED`` (ver constantes
``AUDIT_BATCH_*``). O payload é sempre pequeno e agregado — ``batch_id``
(via ``entity_id``), motivo quando aplicável, contagens/resultado agregado —
NUNCA uma lista de até 500+ ``job_id``. Onde a mudança em si precisa ser
atômica com o evento (criação do batch, pause/resume, abertura do
``cancel_request``), o evento é gravado na MESMA transaction SQLite da
escrita (``connection=conn``); onde o evento é apenas uma nota informativa
sobre uma chamada que não muda estado por si (retomar um cancelamento já
``IN_PROGRESS``, ou o resumo final de ``retry_failed``), ele é gravado em sua
própria transação curta, sem necessidade de atomicidade com outra escrita.
``list_batch_events(batch_id)`` expõe essa trilha para diagnóstico.

CONCORRÊNCIA ENTRE DUAS INSTÂNCIAS
----------------------------------------
Toda escrita relevante (criação, pause/resume, cancel) passa por
``LocalDatabase.transaction()``/``set_setting`` (``BEGIN IMMEDIATE``), então
o SQLite serializa duas instâncias de ``BatchEngine`` contra o mesmo
arquivo exatamente como já serializa duas instâncias de ``ControlManager``/
``JobEngine`` (ver testes). A reivindicação real de um Job continua sendo
autoridade exclusiva de ``JobEngine._claim`` — nenhuma segunda "corrida de
handlers" é possível: duas instâncias de ``BatchEngine`` chamando
``advance_batch`` para o mesmo batch nunca executam o mesmo Job duas vezes,
pela mesma garantia transacional já usada em todo o resto do sistema.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
import sqlite3
from uuid import UUID, uuid4

from .domain import (
    Job,
    InvalidJobState,
    InvalidJobTransition,
    JOB_AUTH_REQUIRED,
    JOB_BLOCKED,
    JOB_CANCELLED,
    JOB_FAILED,
    JOB_INTERRUPTED,
    JOB_PAUSED,
    JOB_PENDING,
    JOB_PROCESSING,
    JOB_PUBLISHED,
    JOB_PUBLISHING,
    JOB_READY,
    JOB_RECOVERING,
    JOB_RETRY,
    JOB_SCHEDULED,
    JOB_STATES,
    JOB_UNKNOWN,
    JOB_USER_ACTION_REQUIRED,
)
from .storage import (
    JobNotFoundForAuditError,
    LocalDatabase,
    OperationalAuditError,
    OperationalAuditLog,
)
from .control_manager import CONTROL_SCOPE_JOB, ControlManager, ScopeCancelOutcome
from .job_engine import JobEngine, JobEngineError, JobRunOutcome
from .time_utils import utc_now_iso


class BatchEngineError(RuntimeError):
    """Erro de contrato do BatchEngine central."""


class BatchNotFoundError(BatchEngineError):
    """O ``batch_id`` solicitado não existe no storage local."""


class EmptyBatchError(BatchEngineError):
    """Um batch precisa conter pelo menos um Job.

    DECISÃO ARQUITETURAL (roadmap: "defina explicitamente inválido vs.
    suportado"): um batch vazio não tem significado operacional — não há
    progresso, membership ou execução para orquestrar, e permiti-lo criaria
    ambiguidade em todo o resto do contrato (uma ``BatchProgress`` com
    ``total=0`` é indistinguível de "todos os Jobs já saíram do batch", algo
    que este módulo nunca permite acontecer, já que a membership é imutável
    após a criação). Por isso criar um batch sem itens é tratado como
    entrada inválida, não como um estado transitório suportado.
    """


class JobAlreadyInBatchError(BatchEngineError):
    """O Job referenciado já pertence a outro batch.

    Ver docstring do módulo ("MEMBERSHIP") para a justificativa completa de
    por que um Job pertence a NO MÁXIMO um batch.
    """


class JobNotEligibleForBatchError(BatchEngineError):
    """O ``job_id`` referenciado não existe, ou não é elegível para a
    operação de batch pedida (ex.: pertence a outro batch em
    ``cancel_job``)."""


class ControlManagerMismatchError(BatchEngineError):
    """``BatchEngine`` recebeu um ``control_manager`` explícito diferente do
    já anexado ao ``job_engine`` fornecido.

    Ver docstring do módulo ("CONTROLMANAGER COMPARTILHADO") para a
    justificativa completa: aceitar essa configuração silenciosamente
    criaria dois ``ControlManager`` controlando o mesmo fluxo de execução
    sem se conhecerem (split-brain). O chamador precisa resolver
    explicitamente — omitir ``control_manager`` (deixando ``BatchEngine``
    reutilizar o de ``job_engine``), ou passar exatamente a mesma instância.
    """


class BatchEngineDatabaseMismatchError(BatchEngineError):
    """Um colaborador fornecido a ``BatchEngine`` (``job_engine``,
    ``control_manager``, ``job_engine.control_manager`` ou ``audit_log``)
    aponta para um arquivo SQLite diferente do usado por este ``BatchEngine``
    (split-brain de STORAGE — ver "COLABORADORES E O MESMO SQLITE" na
    docstring do módulo).

    A comparação é feita pela identidade LÓGICA do banco (``LocalDatabase.path``
    já normalizado/resolvido), não pela identidade de instância Python: duas
    facades ``LocalDatabase`` distintas apontando para exatamente o mesmo
    arquivo ``.db`` são aceitas como válidas. Levantado sempre ANTES de
    qualquer mutação de atributo do ``job_engine`` fornecido (``control_manager``,
    ``batch_engine``) e antes de qualquer gravação em batch/Job, para que uma
    construção inválida nunca deixe o objeto do chamador parcialmente
    mutado.
    """


# Eventos de auditoria mínima de batch (ver "AUDITORIA MÍNIMA DE BATCH" na
# docstring do módulo). Reaproveita integralmente audit_events (m001/m002) —
# nenhuma migration nova. Payload sempre pequeno/agregado, nunca uma lista de
# job_ids.
_BATCH_AUDIT_ENTITY_TYPE = "Batch"
AUDIT_BATCH_CREATED = "BATCH_CREATED"
AUDIT_BATCH_PAUSED = "BATCH_PAUSED"
AUDIT_BATCH_RESUMED = "BATCH_RESUMED"
AUDIT_BATCH_CANCEL_REQUESTED = "BATCH_CANCEL_REQUESTED"
AUDIT_BATCH_CANCEL_RESUMED = "BATCH_CANCEL_RESUMED"
AUDIT_BATCH_CANCEL_COMPLETED = "BATCH_CANCEL_COMPLETED"
AUDIT_BATCH_RETRY_REQUESTED = "BATCH_RETRY_REQUESTED"

BATCH_AUDIT_EVENT_TYPES = frozenset(
    {
        AUDIT_BATCH_CREATED,
        AUDIT_BATCH_PAUSED,
        AUDIT_BATCH_RESUMED,
        AUDIT_BATCH_CANCEL_REQUESTED,
        AUDIT_BATCH_CANCEL_RESUMED,
        AUDIT_BATCH_CANCEL_COMPLETED,
        AUDIT_BATCH_RETRY_REQUESTED,
    }
)


DEFAULT_ADMISSION_LIMIT = 5
"""Quantos Jobs de um batch são admitidos por chamada de ``advance_batch``.

Isto NÃO é uma política de recursos real (CPU/RAM/GPU/VRAM — PROMPT 18,
ResourceManager). É só uma salvaguarda de orquestração para nunca disparar
todos os Jobs de um batch de 200/500+ itens numa única chamada. Pode ser
sobrescrito por chamada via o parâmetro ``limit`` de ``advance_batch``.
"""

# -- Categorização de progresso (ver docstring do módulo) --------------------
_PROGRESS_PENDING = frozenset({JOB_PENDING, JOB_RETRY})
_PROGRESS_PROCESSING = frozenset({JOB_PROCESSING, JOB_PUBLISHING, JOB_RECOVERING})
_PROGRESS_READY = frozenset({JOB_READY, JOB_PUBLISHED, JOB_SCHEDULED})
_PROGRESS_FAILED = frozenset({JOB_FAILED})
_PROGRESS_ATTENTION = frozenset(
    {
        JOB_BLOCKED,
        JOB_AUTH_REQUIRED,
        JOB_USER_ACTION_REQUIRED,
        JOB_UNKNOWN,
        JOB_INTERRUPTED,
        JOB_PAUSED,
    }
)
_PROGRESS_CANCELLED = frozenset({JOB_CANCELLED})

_ALL_PROGRESS_GROUPS = (
    _PROGRESS_PENDING,
    _PROGRESS_PROCESSING,
    _PROGRESS_READY,
    _PROGRESS_FAILED,
    _PROGRESS_ATTENTION,
    _PROGRESS_CANCELLED,
)

assert sum(len(group) for group in _ALL_PROGRESS_GROUPS) == len(JOB_STATES), (
    "categorias de progresso do BatchEngine devem particionar exatamente os "
    "16 estados de Job, sem sobreposição e sem omissão"
)
assert frozenset().union(*_ALL_PROGRESS_GROUPS) == JOB_STATES, (
    "categorias de progresso do BatchEngine devem cobrir exatamente JOB_STATES"
)


@dataclass(frozen=True)
class BatchCreationResult:
    """Resultado de uma criação de batch bem-sucedida (tudo-ou-nada)."""

    batch_id: str
    name: str
    job_ids: tuple[str, ...]
    created_at: str


@dataclass(frozen=True)
class BatchProgress:
    """Snapshot de progresso reconstruído 100% a partir dos Jobs persistidos.

    ``total`` é sempre igual à soma de ``breakdown.values()`` (nunca à soma
    dos seis campos mandatórios — ver docstring do módulo sobre
    ``CANCELLED``). ``breakdown`` mapeia cada ``Job.status`` real encontrado
    para sua contagem, sem esconder nenhum estado.
    """

    batch_id: str
    total: int
    pending: int
    processing: int
    ready: int
    failed: int
    attention_required: int
    cancelled: int
    breakdown: Mapping[str, int]

    def __post_init__(self) -> None:
        if sum(self.breakdown.values()) != self.total:
            raise BatchEngineError(
                "BatchProgress inconsistente: total não bate com a soma do breakdown"
            )


BATCH_CANCEL_FULLY_APPLIED = "FULLY_APPLIED"
BATCH_CANCEL_PARTIALLY_APPLIED = "PARTIALLY_APPLIED"
BATCH_CANCEL_DEFERRED = "DEFERRED"
BATCH_CANCEL_NOT_APPLIED = "NOT_APPLIED"
BATCH_CANCEL_ERROR = "ERROR"

BATCH_CANCEL_OUTCOME_STATUSES = frozenset(
    {
        BATCH_CANCEL_FULLY_APPLIED,
        BATCH_CANCEL_PARTIALLY_APPLIED,
        BATCH_CANCEL_DEFERRED,
        BATCH_CANCEL_NOT_APPLIED,
        BATCH_CANCEL_ERROR,
    }
)


def _aggregate_cancel_status(
    *, cancelled: list[str], deferred: list[str], skipped: list[str], errors: list[tuple[str, str]]
) -> str:
    """Mesma lógica de agregação honesta já usada por
    ``control_manager._compute_cancel_status`` (não reimportada por ser uma
    função privada daquele módulo — duplicação trivial e deliberada de
    poucas linhas, não um novo contrato)."""
    if errors:
        return BATCH_CANCEL_ERROR if not cancelled and not deferred else BATCH_CANCEL_PARTIALLY_APPLIED
    if deferred or skipped:
        if cancelled:
            return BATCH_CANCEL_PARTIALLY_APPLIED
        if deferred and not skipped:
            return BATCH_CANCEL_DEFERRED
        return BATCH_CANCEL_NOT_APPLIED
    return BATCH_CANCEL_FULLY_APPLIED


@dataclass(frozen=True)
class BatchCancelOutcome:
    """Resultado de ``BatchEngine.cancel_batch()``.

    ``status`` é a fonte de verdade (mesmo espírito de
    ``ScopeCancelOutcome.status``): nunca interprete ausência de ``errors``
    como sucesso completo.
    """

    batch_id: str
    status: str
    cancelled_job_ids: tuple[str, ...]
    deferred_job_ids: tuple[str, ...]
    skipped_job_ids: tuple[str, ...]
    errors: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if self.status not in BATCH_CANCEL_OUTCOME_STATUSES:
            raise ValueError(f"status de cancelamento de batch inválido: {self.status!r}")

    @property
    def ok(self) -> bool:
        return self.status == BATCH_CANCEL_FULLY_APPLIED


@dataclass(frozen=True)
class BatchRetryOutcome:
    """Resultado de ``BatchEngine.retry_failed()``."""

    batch_id: str
    retried_job_ids: tuple[str, ...]
    not_eligible_job_ids: tuple[str, ...]
    errors: tuple[tuple[str, str], ...]


class BatchEngine:
    """Orquestra Jobs em lote sobre o ``JobEngine``/``ControlManager`` já
    aprovados, sem duplicar nenhuma das duas filas.

    Ver a docstring do módulo para o contrato completo (persistência,
    membership, pause/resume, cancelamento, reprocessamento, progresso,
    concorrência).
    """

    def __init__(
        self,
        database: LocalDatabase,
        *,
        audit_log: OperationalAuditLog | None = None,
        control_manager: ControlManager | None = None,
        job_engine: JobEngine | None = None,
    ) -> None:
        self.database = database
        resolved_audit_log = audit_log if audit_log is not None else OperationalAuditLog(database)

        # Validação de STORAGE (ver "COLABORADORES E O MESMO SQLITE" na
        # docstring do módulo — correção do Bloqueador 1 da segunda revisão
        # adversarial): todo colaborador operacional (audit_log, job_engine,
        # job_engine.control_manager, control_manager explícito) precisa
        # apontar para o MESMO arquivo SQLite lógico que este BatchEngine,
        # comparado por LocalDatabase.path já normalizado/resolvido — nunca
        # por identidade de instância Python (duas facades legítimas do
        # mesmo painel.db são aceitas). Isso roda ANTES de qualquer mutação
        # de atributo do job_engine fornecido, para que uma construção
        # inválida nunca deixe o objeto do chamador parcialmente mutado.
        self._require_same_database(resolved_audit_log.database, "audit_log")
        if job_engine is not None:
            self._require_same_database(job_engine.database, "job_engine")
            # CORREÇÃO PÓS-TERCEIRA-REVISÃO ADVERSARIAL (Bloqueador 1):
            # job_engine.audit_log é o colaborador REALMENTE usado por
            # JobEngine em caminhos sem connection externa (ex.:
            # `_land_safely_after_claim_failure`, que grava a transição de
            # falha diretamente pelo audit_log interno, sem receber uma
            # connection de fora) — um split-brain aqui deixa o Job
            # abandonado em PROCESSING quando o handler falha.
            self._require_same_database(job_engine.audit_log.database, "job_engine.audit_log")
            if job_engine.control_manager is not None:
                self._require_same_database(
                    job_engine.control_manager.database, "job_engine.control_manager"
                )
                # O ControlManager herdado do job_engine também tem seu
                # próprio audit_log (pode ter sido construído com um
                # diferente do que o job_engine usa) — mesmo invariante de
                # composição precisa valer para ele.
                if job_engine.control_manager.audit_log is not None:
                    self._require_same_database(
                        job_engine.control_manager.audit_log.database,
                        "job_engine.control_manager.audit_log",
                    )
            # CORREÇÃO PÓS-QUARTA-REVISÃO ADVERSARIAL (Bloqueador 2):
            # job_engine.shutdown_coordinator, quando presente, é consultado
            # por `fetch_pending_jobs`/`_claim` (via `is_draining()`) sem
            # connection externa em pelo menos um caminho — um coordinator
            # apontando para outro arquivo faz o JobEngine silenciosamente
            # "achar" que não está DRAINING (ou o contrário), split-brain de
            # storage igual aos já corrigidos para audit_log/control_manager.
            # Opcional por design (``None`` continua permitido — não criamos
            # um ShutdownCoordinator implícito aqui); só valida quando o
            # chamador já forneceu um.
            if job_engine.shutdown_coordinator is not None:
                self._require_same_database(
                    job_engine.shutdown_coordinator.database, "job_engine.shutdown_coordinator"
                )
        if control_manager is not None:
            self._require_same_database(control_manager.database, "control_manager")
            if control_manager.audit_log is not None:
                self._require_same_database(
                    control_manager.audit_log.database, "control_manager.audit_log"
                )

        self.audit_log = resolved_audit_log

        # Resolução de ControlManager (ver "CONTROLMANAGER COMPARTILHADO" na
        # docstring do módulo — correção do Bloqueador 2 da revisão
        # adversarial): nunca dois ControlManager controlando o mesmo fluxo
        # de execução sem se conhecerem.
        if job_engine is not None and job_engine.control_manager is not None:
            if control_manager is not None and control_manager is not job_engine.control_manager:
                raise ControlManagerMismatchError(
                    "control_manager fornecido a BatchEngine é uma instância diferente do "
                    "já anexado a job_engine.control_manager; omita control_manager (para "
                    "reutilizar o de job_engine) ou passe exatamente a mesma instância"
                )
            self.control_manager = job_engine.control_manager
        elif control_manager is not None:
            self.control_manager = control_manager
        else:
            self.control_manager = ControlManager(database, audit_log=self.audit_log)

        self.job_engine = (
            job_engine
            if job_engine is not None
            else JobEngine(database, audit_log=self.audit_log, control_manager=self.control_manager)
        )
        # Garante — incondicionalmente — que o JobEngine (novo ou já
        # existente) usa a MESMA instância de ControlManager que este
        # BatchEngine usa para cancel_job/cancel_batch. Isso é o que
        # realmente fecha o Bloqueador 2: um job_engine fornecido sem
        # control_manager passa a ter exatamente o resolvido acima, nunca
        # fica órfão (control_manager=None) enquanto BatchEngine usa outro.
        self.job_engine.control_manager = self.control_manager
        # Anexa este BatchEngine ao JobEngine (novo ou já existente) para que
        # ``_claim`` recuse admissão de Jobs de um batch pausado/com
        # cancelamento em massa em andamento sob a MESMA transaction da
        # reivindicação (ver docstring do módulo). Atributo público simples
        # em JobEngine — seguro de sobrescrever mesmo se já existia (ver
        # job_engine.py).
        self.job_engine.batch_engine = self

    # -- Validação de storage compartilhado ------------------------------

    @staticmethod
    def _same_database(a: LocalDatabase, b: LocalDatabase) -> bool:
        """Identidade LÓGICA de banco: mesmo arquivo SQLite resolvido, não
        mesma instância Python. ``LocalDatabase.path`` já é normalizado/
        resolvido no ``__init__`` de ``LocalDatabase``; ``Path(...).resolve()``
        aqui é apenas defensivo."""
        return Path(a.path).resolve() == Path(b.path).resolve()

    def _require_same_database(self, other: LocalDatabase, label: str) -> None:
        if not self._same_database(self.database, other):
            raise BatchEngineDatabaseMismatchError(
                f"{label} aponta para um arquivo SQLite diferente do usado por "
                f"este BatchEngine ({other.path!s} vs {self.database.path!s}); "
                "BatchEngine, JobEngine, ControlManager e OperationalAuditLog "
                "precisam apontar para o mesmo banco lógico"
            )

    # -- Validação/leitura de identidade --------------------------------

    @staticmethod
    def _validate_uuid(value: str, field_name: str) -> str:
        value = str(value)
        try:
            UUID(value)
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValueError(f"{field_name} deve ser um UUID válido") from exc
        return value

    def _require_batch_exists(
        self, batch_id: str, *, connection: sqlite3.Connection | None = None
    ) -> None:
        batch_id = self._validate_uuid(batch_id, "batch_id")
        if connection is not None:
            row = connection.execute("SELECT 1 FROM batches WHERE id = ?", (batch_id,)).fetchone()
        else:
            with self.database.connection() as conn:
                row = conn.execute("SELECT 1 FROM batches WHERE id = ?", (batch_id,)).fetchone()
        if row is None:
            raise BatchNotFoundError(f"batch não encontrado: {batch_id}")

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        """Lê a linha de identidade do batch (nome, contagem de criação,
        timestamps). Não inclui progresso nem membership — ver
        ``get_progress``/``list_batch_job_ids``."""
        batch_id = self._validate_uuid(batch_id, "batch_id")
        with self.database.connection() as conn:
            row = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "name": row["name"],
            "job_count": int(row["job_count"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_batch_job_ids(self, batch_id: str) -> tuple[str, ...]:
        """Membership persistente, na ordem estável de criação (``position``).

        Sempre lê do SQLite: a membership nunca é mantida em memória entre
        chamadas ou reinicializações.
        """
        batch_id = self._validate_uuid(batch_id, "batch_id")
        with self.database.connection() as conn:
            rows = conn.execute(
                "SELECT job_id FROM batch_jobs WHERE batch_id = ? ORDER BY position",
                (batch_id,),
            ).fetchall()
        return tuple(row["job_id"] for row in rows)

    def list_batch_events(self, batch_id: str) -> list[dict[str, Any]]:
        """Trilha append-only mínima do batch (ver "AUDITORIA MÍNIMA DE
        BATCH" na docstring do módulo), na ordem exata em que foi gravada."""
        batch_id = self._validate_uuid(batch_id, "batch_id")
        return self.database.list_audit_events(entity_type=_BATCH_AUDIT_ENTITY_TYPE, entity_id=batch_id)

    def _append_batch_event(
        self,
        event_type: str,
        batch_id: str,
        *,
        data: Mapping[str, Any] | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        if event_type not in BATCH_AUDIT_EVENT_TYPES:
            raise ValueError(f"event_type de batch inválido: {event_type!r}")
        self.database.append_audit_event(
            event_type,
            entity_type=_BATCH_AUDIT_ENTITY_TYPE,
            entity_id=batch_id,
            data=dict(data or {}),
            connection=connection,
        )

    def get_batch_id_for_job(
        self, job_id: str, *, connection: sqlite3.Connection | None = None
    ) -> str | None:
        """Devolve o único batch ao qual ``job_id`` pertence, ou ``None``."""
        job_id = self._validate_uuid(job_id, "job_id")
        if connection is not None:
            row = connection.execute(
                "SELECT batch_id FROM batch_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        else:
            with self.database.connection() as conn:
                row = conn.execute(
                    "SELECT batch_id FROM batch_jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
        return None if row is None else row["batch_id"]

    # -- Criação atômica ---------------------------------------------------

    def create_batch(
        self, items: Sequence[Job | Mapping[str, Any]], *, name: str | None = None
    ) -> BatchCreationResult:
        """Cria um batch e todos os seus Jobs atomicamente (tudo-ou-nada).

        ``items`` aceita, misturados livremente: instâncias de ``Job`` já
        construídas (ainda não persistidas) e specs (mapping de kwargs para
        ``Job(...)``, ex.: ``{"operation": "RENDER", "video_id": ...}``).
        Isso é o que permite tanto "aplicar uma função em lote" (todos os
        itens com a mesma ``operation``) quanto "aplicar várias funções"
        (``operation`` diferente por item, ou múltiplos Jobs por item lógico
        já expandidos pelo chamador) sem que ``BatchEngine`` precise saber o
        que cada ``operation`` significa — ele só orquestra.

        A linha do batch, todos os Jobs e toda a membership são gravados em
        UMA ÚNICA transaction SQLite: ou o batch inteiro é criado
        corretamente, ou nada é persistido (ver docstring do módulo).
        """
        if not items:
            raise EmptyBatchError("create_batch requer ao menos um item (ver EmptyBatchError)")

        jobs: list[Job] = [item if isinstance(item, Job) else Job(**dict(item)) for item in items]

        batch_id = str(uuid4())
        now = utc_now_iso()
        batch_name = str(name) if name else ""

        with self.database.transaction() as conn:
            conn.execute(
                "INSERT INTO batches(id, schema_version, created_at, updated_at, name, job_count, extra_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (batch_id, 1, now, now, batch_name, len(jobs), "{}"),
            )
            for position, job in enumerate(jobs):
                self.audit_log.create_job(job, connection=conn)
                conn.execute(
                    "INSERT INTO batch_jobs(batch_id, job_id, position, created_at) VALUES (?, ?, ?, ?)",
                    (batch_id, job.id, position, now),
                )
            self._append_batch_event(
                AUDIT_BATCH_CREATED,
                batch_id,
                data={"name": batch_name, "job_count": len(jobs), "source": "specs"},
                connection=conn,
            )

        return BatchCreationResult(
            batch_id=batch_id,
            name=batch_name,
            job_ids=tuple(job.id for job in jobs),
            created_at=now,
        )

    def create_batch_from_existing_jobs(
        self, job_ids: Sequence[str], *, name: str | None = None
    ) -> BatchCreationResult:
        """Agrupa Jobs JÁ EXISTENTES em um batch novo, atomicamente.

        Cada ``job_id`` precisa existir e não pertencer a nenhum outro
        batch (ver ``JobAlreadyInBatchError``); qualquer violação reverte a
        transaction inteira — nenhuma membership parcial sobra.
        """
        if not job_ids:
            raise EmptyBatchError(
                "create_batch_from_existing_jobs requer ao menos um job_id"
            )
        normalized_ids = [self._validate_uuid(job_id, "job_id") for job_id in job_ids]

        batch_id = str(uuid4())
        now = utc_now_iso()
        batch_name = str(name) if name else ""

        with self.database.transaction() as conn:
            for job_id in normalized_ids:
                job = self.database.get(Job, job_id, connection=conn)
                if job is None:
                    raise JobNotEligibleForBatchError(f"Job não encontrado: {job_id}")
                existing = conn.execute(
                    "SELECT batch_id FROM batch_jobs WHERE job_id = ?", (job_id,)
                ).fetchone()
                if existing is not None:
                    raise JobAlreadyInBatchError(
                        f"Job {job_id} já pertence ao batch {existing['batch_id']}"
                    )

            conn.execute(
                "INSERT INTO batches(id, schema_version, created_at, updated_at, name, job_count, extra_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (batch_id, 1, now, now, batch_name, len(normalized_ids), "{}"),
            )
            for position, job_id in enumerate(normalized_ids):
                conn.execute(
                    "INSERT INTO batch_jobs(batch_id, job_id, position, created_at) VALUES (?, ?, ?, ?)",
                    (batch_id, job_id, position, now),
                )
            self._append_batch_event(
                AUDIT_BATCH_CREATED,
                batch_id,
                data={"name": batch_name, "job_count": len(normalized_ids), "source": "existing_jobs"},
                connection=conn,
            )

        return BatchCreationResult(
            batch_id=batch_id,
            name=batch_name,
            job_ids=tuple(normalized_ids),
            created_at=now,
        )

    # -- Pause/resume (flag dedicado, nunca ControlManager QUEUE) -----------

    @staticmethod
    def _pause_key(batch_id: str) -> str:
        return f"batch:{batch_id}:paused"

    def pause_batch(self, batch_id: str, *, reason: str | None = None) -> None:
        """Impede que Jobs NOVOS deste batch sejam admitidos por
        ``JobEngine._claim``. Jobs já ``PROCESSING``/``PUBLISHING``/
        ``RECOVERING`` continuam seguindo as regras de ponto seguro já
        aprovadas — pausar nunca reescreve o estado deles.

        O flag e o evento de auditoria (``BATCH_PAUSED``) commitam
        atomicamente na mesma transaction."""
        self._require_batch_exists(batch_id)
        with self.database.transaction() as conn:
            self.database.set_setting(
                self._pause_key(batch_id),
                {"paused": True, "reason": reason, "at": utc_now_iso()},
                connection=conn,
            )
            self._append_batch_event(AUDIT_BATCH_PAUSED, batch_id, data={"reason": reason}, connection=conn)

    def resume_batch(self, batch_id: str, *, reason: str | None = None) -> None:
        """O flag e o evento de auditoria (``BATCH_RESUMED``) commitam
        atomicamente na mesma transaction."""
        self._require_batch_exists(batch_id)
        with self.database.transaction() as conn:
            self.database.set_setting(
                self._pause_key(batch_id),
                {"paused": False, "reason": reason, "at": utc_now_iso()},
                connection=conn,
            )
            self._append_batch_event(AUDIT_BATCH_RESUMED, batch_id, data={"reason": reason}, connection=conn)

    def is_batch_paused(
        self, batch_id: str, *, connection: sqlite3.Connection | None = None
    ) -> bool:
        value = self.database.get_setting(
            self._pause_key(batch_id), default=None, connection=connection
        )
        return bool(isinstance(value, dict) and value.get("paused"))

    def is_job_admission_blocked(
        self, job_id: str, *, connection: sqlite3.Connection | None = None
    ) -> bool:
        """Protocolo somente-leitura consultado por ``JobEngine._claim``/
        ``fetch_pending_jobs`` (ver docstring do módulo). Nunca escreve nem
        transiciona ``Job.status``. Quando ``connection`` é fornecida, a
        leitura roda dentro da MESMA transaction ``BEGIN IMMEDIATE`` da
        própria reivindicação, fechando tanto a corrida pause-vs-claim
        quanto a corrida cancel_batch-vs-claim (ver "BLOQUEADOR 1" na
        docstring do módulo).

        Bloqueia quando o batch do Job está pausado, OU quando o batch tem
        um ``cancel_batch`` ``IN_PROGRESS`` — nesse segundo caso, mesmo um
        Job que ainda não foi alcançado pelo laço de cancelamento nunca pode
        iniciar trabalho novo enquanto a intenção de cancelar o lote inteiro
        continuar pendente.
        """
        batch_id = self.get_batch_id_for_job(job_id, connection=connection)
        if batch_id is None:
            return False
        if self.is_batch_paused(batch_id, connection=connection):
            return True
        cancel_request = self.database.get_setting(
            self._cancel_request_key(batch_id), default=None, connection=connection
        )
        if isinstance(cancel_request, dict) and cancel_request.get("status") == "IN_PROGRESS":
            return True
        return False

    # -- Admissão limitada (nunca todos os Jobs de uma vez) ------------------

    def advance_batch(
        self, batch_id: str, *, limit: int = DEFAULT_ADMISSION_LIMIT
    ) -> list[JobRunOutcome]:
        """Avança até ``limit`` Jobs PENDING/RETRY deste batch, delegando
        toda reivindicação/execução real ao ``JobEngine`` já aprovado (nunca
        duplica a fila: ``JobEngine.advance``/``_claim`` são a única
        autoridade). Nunca dispara todos os Jobs do batch de uma vez, mesmo
        em lotes de 500+."""
        self._require_batch_exists(batch_id)
        if limit <= 0:
            raise ValueError("limit deve ser > 0")

        member_ids = set(self.list_batch_job_ids(batch_id))
        actionable = [
            job for job in self.job_engine.fetch_pending_jobs(limit=None) if job.id in member_ids
        ][:limit]

        outcomes: list[JobRunOutcome] = []
        for job in actionable:
            try:
                updated = self.job_engine.advance(job.id)
                outcomes.append(JobRunOutcome(job_id=job.id, job=updated, error=None))
            except JobEngineError as exc:
                outcomes.append(JobRunOutcome(job_id=job.id, job=None, error=exc))
        return outcomes

    # -- Cancelamento (reusa integralmente ControlManager.cancel) -----------

    def cancel_job(
        self, batch_id: str, job_id: str, *, reason: str | None = None
    ) -> ScopeCancelOutcome:
        """Cancela um Job individual deste batch, reutilizando integralmente
        o contrato seguro já aprovado do ``ControlManager`` (nunca marca um
        Job ativo como CANCELLED fora do ponto seguro que ``ControlManager``/
        ``JobEngine`` já garantem)."""
        self._require_batch_exists(batch_id)
        job_id = self._validate_uuid(job_id, "job_id")
        actual_batch_id = self.get_batch_id_for_job(job_id)
        if actual_batch_id != batch_id:
            raise JobNotEligibleForBatchError(f"Job {job_id} não pertence ao batch {batch_id}")
        return self.control_manager.cancel(CONTROL_SCOPE_JOB, job_id, reason=reason)

    @staticmethod
    def _cancel_request_key(batch_id: str) -> str:
        return f"batch:{batch_id}:cancel_request"

    def get_batch_cancel_request(self, batch_id: str) -> dict[str, Any] | None:
        """Consulta o último registro de cancelamento em massa persistido
        para este batch — ``status`` é ``"IN_PROGRESS"`` (inclusive depois
        de um crash no meio do laço) ou ``"COMPLETED"``. ``None`` se nenhum
        cancelamento em massa foi pedido ainda para este batch.

        NÃO contém uma lista de candidatos: a membership do batch é imutável
        (ver docstring do módulo), então os candidatos de um cancelamento em
        massa são sempre ``list_batch_job_ids(batch_id)`` — recalculados a
        cada chamada, nunca duplicados aqui."""
        value = self.database.get_setting(self._cancel_request_key(batch_id))
        return value if isinstance(value, dict) else None

    def _open_or_resume_cancel_request(
        self, batch_id: str, *, reason: str | None, candidate_count: int
    ) -> tuple[str | None, str]:
        """Resolve a ÚNICA intenção histórica de cancelamento em massa deste
        batch — atomicamente, dentro de um único ``BEGIN IMMEDIATE`` (ver
        Bloqueador 2 da terceira revisão adversarial: leitura e escrita
        nunca separadas por fora de transaction).

        CANCELAMENTO DE BATCH É MONOTÔNICO (correção do Bloqueador 1 da
        quarta revisão adversarial): como a membership de ``batch_jobs`` é
        imutável, "cancelar este batch inteiro" é, neste Prompt, uma
        intenção histórica ÚNICA por batch — nunca uma nova geração:

        - sem ``cancel_request`` ainda: cria um NOVO ``IN_PROGRESS`` com o
          ``reason``/``requested_at`` desta chamada e grava
          ``BATCH_CANCEL_REQUESTED``;
        - ``cancel_request`` já ``IN_PROGRESS``: é a MESMA operação —
          devolve o ``reason``/``requested_at`` JÁ persistidos (nunca os
          argumentos desta chamada) e grava ``BATCH_CANCEL_RESUMED`` NESTA
          MESMA transaction (não depois do commit — ver "TOCTOU de
          auditoria" abaixo);
        - ``cancel_request`` já ``COMPLETED``: intenção histórica já
          concluída — NUNCA reaberta como uma operação nova. Devolve o
          ``reason``/``requested_at`` originais e não grava nenhum evento
          novo; um ``reason`` diferente recebido aqui é simplesmente
          ignorado. Isso fecha o padrão ABA em que um caller antigo,
          atrasado numa retomada, poderia completar uma operação NOVA
          (aberta por um terceiro caller depois que a original já havia
          concluído) usando o ``reason`` da operação antiga: como não
          existe "operação nova" possível depois de ``COMPLETED``, esse
          caller atrasado só pode encontrar a MESMA intenção histórica ao
          terminar — nunca uma diferente.

        TOCTOU DE AUDITORIA (``BATCH_CANCEL_RESUMED``): antes desta
        correção, o evento de retomada era gravado em uma transaction
        separada, DEPOIS do commit que detectou ``IN_PROGRESS`` — um
        ``_complete_cancel_request_once`` concorrente podia commitar
        ``COMPLETED`` nesse intervalo, produzindo a ordem histórica
        impossível ``REQUESTED -> COMPLETED -> RESUMED``. Gravando
        ``BATCH_CANCEL_RESUMED`` dentro do MESMO ``BEGIN IMMEDIATE`` que
        detectou a retomada, um completion concorrente precisa esperar essa
        transaction terminar (mesmo write lock do SQLite) — a ordem
        histórica é sempre ``REQUESTED -> RESUMED -> COMPLETED``.

        Devolve ``(effective_reason, requested_at)``, sempre os valores
        definitivos da intenção histórica única deste batch.
        """
        key = self._cancel_request_key(batch_id)
        with self.database.transaction() as conn:
            existing = self.database.get_setting(key, default=None, connection=conn)

            if isinstance(existing, dict) and existing.get("status") == "IN_PROGRESS":
                self._append_batch_event(
                    AUDIT_BATCH_CANCEL_RESUMED,
                    batch_id,
                    data={"job_count": candidate_count},
                    connection=conn,
                )
                return existing.get("reason"), existing.get("requested_at")

            if isinstance(existing, dict) and existing.get("status") == "COMPLETED":
                # Monotônico: nenhuma segunda geração é aberta depois de
                # COMPLETED. Nenhum evento novo, nenhum reason novo.
                return existing.get("reason"), existing.get("requested_at")

            requested_at = utc_now_iso()
            self.database.set_setting(
                key,
                {"status": "IN_PROGRESS", "reason": reason, "requested_at": requested_at},
                connection=conn,
            )
            self._append_batch_event(
                AUDIT_BATCH_CANCEL_REQUESTED,
                batch_id,
                data={"reason": reason, "job_count": candidate_count},
                connection=conn,
            )
            return reason, requested_at

    def _complete_cancel_request_once(
        self,
        batch_id: str,
        *,
        effective_reason: str | None,
        requested_at_fallback: str,
        status: str,
        cancelled: int,
        deferred: int,
        skipped: int,
        errors: int,
    ) -> None:
        """Faz a transição ``IN_PROGRESS -> COMPLETED`` e grava
        ``BATCH_CANCEL_COMPLETED`` — mas só se ESTA chamada for quem
        encontra o request ainda ``IN_PROGRESS`` ao relê-lo dentro do MESMO
        ``BEGIN IMMEDIATE``. Se outra chamada concorrente já o completou
        primeiro, não sobrescreve ``reason``/``requested_at`` nem grava um
        segundo evento — a operação por Job já é idempotente
        (``ControlManager.cancel``), então cooperar sem duplicar a
        transição histórica é suficiente; não precisa de lease nem mutex
        distribuído (ver Bloqueador 2 da terceira revisão adversarial)."""
        key = self._cancel_request_key(batch_id)
        with self.database.transaction() as conn:
            current = self.database.get_setting(key, default=None, connection=conn)
            if not (isinstance(current, dict) and current.get("status") == "IN_PROGRESS"):
                return
            self.database.set_setting(
                key,
                {
                    "status": "COMPLETED",
                    "reason": effective_reason,
                    "requested_at": current.get("requested_at", requested_at_fallback),
                    "completed_at": utc_now_iso(),
                },
                connection=conn,
            )
            self._append_batch_event(
                AUDIT_BATCH_CANCEL_COMPLETED,
                batch_id,
                data={
                    "status": status,
                    "cancelled": cancelled,
                    "deferred": deferred,
                    "skipped": skipped,
                    "errors": errors,
                },
                connection=conn,
            )

    def cancel_batch(self, batch_id: str, *, reason: str | None = None) -> BatchCancelOutcome:
        """Cancela todos os Jobs deste batch, com a mesma disciplina de
        crash-safety já usada por ``ControlManager.cancel`` para GLOBAL/
        QUEUE: a intenção é persistida ANTES de qualquer efeito. Uma chamada
        repetida (ex.: depois de um crash) retoma o mesmo pedido
        ``IN_PROGRESS`` e nunca toca Jobs de outro batch.

        Candidatos são SEMPRE ``list_batch_job_ids(batch_id)`` — a membership
        persistida e imutável do batch — tanto na primeira chamada quanto em
        qualquer retomada (ver "candidate_job_ids duplicado em settings" na
        docstring do módulo: nenhuma cópia da membership é guardada em
        ``settings``).

        Cada Job é cancelado via ``ControlManager.cancel(JOB, ...)`` — o
        mesmo caminho seguro usado por ``cancel_job``. Diferente da primeira
        versão, uma exceção INESPERADA (não um erro de negócio já isolado
        por ``ControlManager.cancel`` em ``ScopeCancelOutcome.errors``) NÃO é
        capturada aqui: ela propaga para fora de ``cancel_batch``,
        deliberadamente, para que um crash real no meio do laço deixe o
        ``cancel_request`` persistido como ``IN_PROGRESS`` (nunca
        ``COMPLETED`` por engano) — exatamente o cenário de crash-safety que
        a retomada precisa detectar. Um erro de negócio isolado (ex.: Job
        não encontrado) continua vindo de dentro de ``ScopeCancelOutcome``,
        nunca interrompendo os demais candidatos.
        """
        self._require_batch_exists(batch_id)
        candidate_ids = list(self.list_batch_job_ids(batch_id))

        # CORREÇÃO PÓS-TERCEIRA/QUARTA-REVISÃO ADVERSARIAL (Bloqueador 2 da
        # 3ª + Bloqueador 1 da 4ª): abrir/retomar/reconhecer a intenção
        # histórica ÚNICA e monotônica de cancelamento deste batch acontece
        # atomicamente dentro de UM ÚNICO BEGIN IMMEDIATE — incluindo o
        # evento BATCH_CANCEL_RESUMED quando aplicável (nunca mais gravado
        # fora dessa transaction: ver docstring de
        # _open_or_resume_cancel_request para o TOCTOU de auditoria
        # corrigido). Nenhuma chamada, concorrente ou atrasada, pode abrir
        # uma segunda geração desta operação nem alterar reason/requested_at
        # já persistidos.
        effective_reason, requested_at = self._open_or_resume_cancel_request(
            batch_id, reason=reason, candidate_count=len(candidate_ids)
        )

        cancelled: list[str] = []
        deferred: list[str] = []
        skipped: list[str] = []
        errors: list[tuple[str, str]] = []

        for job_id in candidate_ids:
            outcome = self.control_manager.cancel(CONTROL_SCOPE_JOB, job_id, reason=effective_reason)
            if outcome.cancelled_job_ids:
                cancelled.append(job_id)
            elif outcome.deferred_job_ids:
                deferred.append(job_id)
            elif outcome.errors:
                errors.extend(outcome.errors)
            else:
                skipped.append(job_id)

        status = _aggregate_cancel_status(
            cancelled=cancelled, deferred=deferred, skipped=skipped, errors=errors
        )

        # CORREÇÃO PÓS-TERCEIRA-REVISÃO ADVERSARIAL (Bloqueador 2 — parte de
        # completion): duas chamadas podem cooperar legitimamente na mesma
        # operação IN_PROGRESS (ControlManager.cancel(JOB) já é idempotente
        # por Job), mas só UMA pode fazer a transição histórica
        # IN_PROGRESS -> COMPLETED e gravar BATCH_CANCEL_COMPLETED. Isso
        # também roda atomicamente, relendo o request dentro do MESMO
        # BEGIN IMMEDIATE antes de decidir.
        self._complete_cancel_request_once(
            batch_id,
            effective_reason=effective_reason,
            requested_at_fallback=requested_at,
            status=status,
            cancelled=len(cancelled),
            deferred=len(deferred),
            skipped=len(skipped),
            errors=len(errors),
        )

        return BatchCancelOutcome(
            batch_id=batch_id,
            status=status,
            cancelled_job_ids=tuple(cancelled),
            deferred_job_ids=tuple(deferred),
            skipped_job_ids=tuple(skipped),
            errors=tuple(errors),
        )

    # -- Reprocessar somente falhas -------------------------------------

    def retry_failed(self, batch_id: str, *, reason: str | None = None) -> BatchRetryOutcome:
        """Reprocessa somente os Jobs ``FAILED`` deste batch, usando a
        transição ``RETRY`` já aprovada (``OperationalAuditLog.retry``) —
        nunca uma reescrita direta de status, nunca retry automático ou
        infinito. Elegibilidade é estritamente ``Job.status == JOB_FAILED``;
        qualquer outro Job do batch é reportado como não elegível, nunca
        silenciosamente ignorado."""
        self._require_batch_exists(batch_id)
        member_ids = self.list_batch_job_ids(batch_id)

        retried: list[str] = []
        not_eligible: list[str] = []
        errors: list[tuple[str, str]] = []

        for job_id in member_ids:
            job = self.job_engine.get_job(job_id)
            if job is None or job.status != JOB_FAILED:
                not_eligible.append(job_id)
                continue
            try:
                if self.job_engine.retry_policy is not None:
                    # PROMPT 21, item 1.3: o ciclo manual de retry_failed()
                    # SEMPRE reinicia o contador de tentativas automáticas
                    # -- sem isso, um Job que já esgotou seu limite
                    # automático (RetryPolicy.record_failure aterrissando em
                    # FAILED) reentraria em RETRY já "pré-esgotado" na
                    # primeira falha seguinte, sem nenhuma tentativa
                    # automática concedida ao novo ciclo. Ordem
                    # deliberada -- reset ANTES de audit_log.retry() -- por
                    # segurança de janela de crash: um crash entre as duas
                    # chamadas deixa attempt_count=0 com o Job ainda
                    # FAILED, o que é inofensivo e idempotente (rodar
                    # retry_failed() de novo apenas reseta para 0 outra
                    # vez); a ordem inversa arriscaria um crash deixando um
                    # attempt_count alto obsoleto em um Job já RETRY, o que
                    # esgotaria o limite automático na PRÓXIMA falha sem
                    # conceder nenhuma tentativa ao novo ciclo, quebrando a
                    # garantia deste item.
                    self.job_engine.retry_policy.reset(job_id)
                self.audit_log.retry(job_id, data={"reason": reason} if reason else None)
                retried.append(job_id)
            except (InvalidJobTransition, InvalidJobState):
                # CORREÇÃO PÓS-TERCEIRA-REVISÃO ADVERSARIAL (Bloqueador 3):
                # o Job era FAILED na leitura acima, mas deixou de ser antes
                # desta transição (outra chamada concorrente já o moveu —
                # ex.: outro retry_failed, ou um cancel_job). A State
                # Machine dentro da própria transaction de
                # ``audit_log.retry`` é a autoridade final; perder essa
                # corrida é uma condição operacional normal, não um crash da
                # operação inteira — reclassifica honestamente como não
                # elegível e segue para os demais candidatos.
                not_eligible.append(job_id)
            except (OperationalAuditError, JobNotFoundForAuditError) as exc:
                errors.append((job_id, str(exc)))

        self._append_batch_event(
            AUDIT_BATCH_RETRY_REQUESTED,
            batch_id,
            data={
                "reason": reason,
                "retried": len(retried),
                "not_eligible": len(not_eligible),
                "errors": len(errors),
            },
        )

        return BatchRetryOutcome(
            batch_id=batch_id,
            retried_job_ids=tuple(retried),
            not_eligible_job_ids=tuple(not_eligible),
            errors=tuple(errors),
        )

    # -- Progresso: sempre reconstruído dos Jobs persistidos ----------------

    def get_progress(self, batch_id: str) -> BatchProgress:
        """Progresso reconstruído 100% a partir dos Jobs persistidos — nunca
        de um contador em memória. Uma única query (JOIN + GROUP BY,
        indexada desde m001/m003), O(itens do batch), nunca O(n²)."""
        self._require_batch_exists(batch_id)
        with self.database.connection() as conn:
            rows = conn.execute(
                "SELECT jobs.status AS status, COUNT(*) AS n "
                "FROM batch_jobs JOIN jobs ON jobs.id = batch_jobs.job_id "
                "WHERE batch_jobs.batch_id = ? GROUP BY jobs.status",
                (batch_id,),
            ).fetchall()
        breakdown = {row["status"]: int(row["n"]) for row in rows}
        total = sum(breakdown.values())

        def _sum(group: frozenset[str]) -> int:
            return sum(count for status, count in breakdown.items() if status in group)

        return BatchProgress(
            batch_id=batch_id,
            total=total,
            pending=_sum(_PROGRESS_PENDING),
            processing=_sum(_PROGRESS_PROCESSING),
            ready=_sum(_PROGRESS_READY),
            failed=_sum(_PROGRESS_FAILED),
            attention_required=_sum(_PROGRESS_ATTENTION),
            cancelled=_sum(_PROGRESS_CANCELLED),
            breakdown=breakdown,
        )


__all__ = [
    "BatchEngine",
    "BatchEngineError",
    "BatchNotFoundError",
    "EmptyBatchError",
    "JobAlreadyInBatchError",
    "JobNotEligibleForBatchError",
    "ControlManagerMismatchError",
    "BatchEngineDatabaseMismatchError",
    "DEFAULT_ADMISSION_LIMIT",
    "BatchCreationResult",
    "BatchProgress",
    "BatchCancelOutcome",
    "BatchRetryOutcome",
    "BATCH_CANCEL_FULLY_APPLIED",
    "BATCH_CANCEL_PARTIALLY_APPLIED",
    "BATCH_CANCEL_DEFERRED",
    "BATCH_CANCEL_NOT_APPLIED",
    "BATCH_CANCEL_ERROR",
    "AUDIT_BATCH_CREATED",
    "AUDIT_BATCH_PAUSED",
    "AUDIT_BATCH_RESUMED",
    "AUDIT_BATCH_CANCEL_REQUESTED",
    "AUDIT_BATCH_CANCEL_RESUMED",
    "AUDIT_BATCH_CANCEL_COMPLETED",
    "AUDIT_BATCH_RETRY_REQUESTED",
    "BATCH_AUDIT_EVENT_TYPES",
]
