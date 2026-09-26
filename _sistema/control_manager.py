# -*- coding: utf-8 -*-
"""ControlManager central — pause, resume, stop_after_current e cancel
(FASE 3 / PROMPT 15, revisado após revisão adversarial pós-implementação).

Este módulo é deliberadamente independente de apresentação, do JobEngine e
de integrações remotas, no mesmo espírito de ``job_engine.py`` e
``recovery_manager.py``:

- não importa nem conhece UI/frontend;
- não importa nem conhece YouTube, TikTok, Instagram ou qualquer outro
  Connector — nenhum acoplamento a uma plataforma específica existe aqui;
  ``ACCOUNT``/``PLATFORM`` são tratados como identificadores livres (string),
  nunca como um vocabulário fechado de plataformas conhecidas;
- não importa ``job_engine.py``: o ``JobEngine`` pode opcionalmente usar um
  ``ControlManager`` (ver ``job_engine.py``), mas o inverso nunca acontece —
  o ControlManager funciona sozinho, sem nenhum Job Engine instanciado. Os
  estados de ``Job`` considerados "execução ativa/incerta" (ver mais abaixo)
  são definidos aqui a partir do vocabulário de ``_sistema.domain``
  (``JOB_PROCESSING``/``JOB_PUBLISHING``/``JOB_RECOVERING``/``JOB_UNKNOWN``),
  deliberadamente reescritos aqui em vez de importados de
  ``job_engine.CLAIMABLE_STATES`` — uma pequena duplicação intencional, para
  não criar a dependência proibida;
- lê e persiste todo o seu estado exclusivamente através de
  ``LocalDatabase``/``OperationalAuditLog`` (SQLite local), reaproveitando a
  tabela ``settings`` já existente (migrations 001/002) para os flags de
  controle e ``audit_events`` para o histórico — nenhuma migration nova foi
  necessária, inclusive após esta revisão.

CINCO NÍVEIS, DOIS GRUPOS DE APLICABILIDADE
----------------------------------------------
O roadmap do PROMPT 15 pede pause/resume/stop_after_current/cancel em cinco
níveis: ``GLOBAL``, ``ACCOUNT``, ``PLATFORM``, ``QUEUE`` (fila) e ``JOB``.
Este módulo persiste e expõe consulta para os cinco, mas a capacidade de o
**JobEngine aplicar automaticamente** um bloqueio antes de reivindicar um
Job depende de o Engine conseguir, a partir do próprio ``Job`` (aprovado nos
Prompts 1–11, sem alteração de schema aqui), determinar a que escopo esse
Job pertence:

- ``GLOBAL``: sempre aplicável — não depende de nenhum dado do Job.
- ``QUEUE``: mapeado para ``Job.operation`` (o mais próximo de uma "fila"
  que já existe no modelo aprovado) — sempre aplicável.
- ``JOB``: mapeado para ``Job.id`` — sempre aplicável.
- ``ACCOUNT`` e ``PLATFORM``: o modelo ``Job`` atual **não** possui
  ``account_id`` nem ``platform`` (só ``Account``/``Publication`` têm esses
  dados, e nenhuma FK liga um ``Job`` a eles hoje). Por isso estes dois
  níveis são implementados de forma completa e persistente **como controle
  consultável**, mas o ``JobEngine`` não pode e não tenta aplicá-los
  automaticamente nesta etapa — fazer isso exigiria ou adivinhar a relação
  (arriscado) ou alterar o modelo ``Job`` aprovado (fora do escopo deste
  Prompt). A integração pendente: quando um Connector futuro souber a conta
  e a plataforma de um Job antes de executar um efeito remoto, ele deve
  consultar ``ControlManager.is_execution_blocked(account_id=..., platform=...)``
  (para pause/stop) e ``ControlManager.is_cancel_requested(CONTROL_SCOPE_ACCOUNT, ...)``/
  ``is_cancel_requested(CONTROL_SCOPE_PLATFORM, ...)`` (para cancel — ver
  seção "CANCEL POR ACCOUNT/PLATFORM" abaixo) antes de agir. Essas chamadas
  ainda não existem em nenhum lugar do código — é a pendência explícita
  desta etapa, não uma funcionalidade ativa hoje.

PERSISTÊNCIA
-------------
Cada flag (``paused``, ``stop_after_current`` e ``cancel_requested``) de
cada ``(scope, scope_id)`` é uma chave independente na tabela ``settings``
(``control:<scope>:...``), sempre sobrescrita por inteiro a cada chamada —
nunca um read-modify-write "solto". Isso é o que torna a operação segura sob
concorrência (ver abaixo) e o que garante sobrevivência a fechar/reabrir o
processo ou reiniciar o Windows: a mesma tabela ``settings`` já usada por
outras partes do sistema, sem tabela nova. Cada chamada também registra, na
mesma transaction SQLite que grava o flag, um evento em ``audit_events``
(``entity_type="ControlScope"``) para observabilidade.

CONCORRÊNCIA E ATOMICIDADE CONTRA CLAIM (CORREÇÃO PÓS-REVISÃO)
--------------------------------------------------------------------
A implementação original deste módulo verificava ``is_execution_blocked``
numa leitura solta (própria conexão, fora de qualquer transaction) e só
*depois* o ``JobEngine`` reivindicava o Job numa transaction separada. Isso
deixava uma janela real: um ``pause``/``stop_after_current`` podia commitar
*entre* a leitura e a reivindicação, e o Engine reivindicava o Job mesmo
assim — violando o contrato "nenhuma nova reivindicação depois que o pause
foi persistido".

A correção: ``is_execution_blocked`` (e as leituras de flag que ele usa)
agora aceitam um parâmetro opcional ``connection``. Quando o ``JobEngine``
chama esse método, ele passa a MESMA conexão/transaction ``BEGIN IMMEDIATE``
que em seguida tentará a reivindicação (``OperationalAuditLog.transition_job``
também ganhou esse mesmo parâmetro — ver ``storage/audit.py``). Como
``BEGIN IMMEDIATE`` adquire o write lock do SQLite de forma exclusiva e
imediata, e todo ``pause``/``resume``/``request_stop_after_current`` também
grava sob seu próprio ``BEGIN IMMEDIATE`` (via ``self.database.transaction()``),
as duas operações — "checar flags" + "reivindicar" de um lado, "gravar flag"
do outro — nunca podem ser intercaladas por um terceiro escritor: o SQLite
serializa qual das duas transactions adquire o lock primeiro, e a que
adquire depois só prossegue já enxergando o commit da primeira. Isso dá
exatamente a propriedade exigida:

- se o claim adquirir a exclusão primeiro, ele completa vendo o estado
  anterior ao pause (o pause passa a valer só a partir da próxima tentativa
  de reivindicação, depois que ele commitar);
- se o pause/stop commitar primeiro, o claim que tentar adquirir o lock
  depois necessariamente lê o flag já ativo e nunca chega a reivindicar.

Nenhum lock em memória foi introduzido: a garantia vem inteiramente do
SQLite, então vale entre processos diferentes, não só entre threads do
mesmo processo (ver ``tests/test_control_manager.py``, testes de corrida com
duas instâncias reais de ``LocalDatabase``/``ControlManager``/``JobEngine``
contra o mesmo arquivo).

O mesmo padrão (``connection`` compartilhada sob um único ``BEGIN IMMEDIATE``)
é reaproveitado para o cancelamento seguro de Jobs (ver abaixo): decidir se
um Job pode ser cancelado imediatamente, ou se deve apenas registrar a
intenção, acontece atomicamente com a própria tentativa de transição —
nunca como uma decisão tomada sobre uma leitura que pode já estar
desatualizada quando a escrita realmente acontece.

Fora dessa composição com o claim/cancel, cada flag continua sendo gravado
como um valor autocontido (nunca mesclado com o valor anterior): duas
instâncias/processos que chamem ``pause``/``resume`` concorrentemente para o
mesmo ``(scope, scope_id)`` nunca corrompem o estado — a última escrita
commitada vence, e o resultado é sempre um dos dois valores pretendidos
(nunca uma mistura).

"PAUSE ESPERA UM PONTO SEGURO QUANDO POSSÍVEL"
-------------------------------------------------
Nem ``ControlManager`` nem ``JobEngine`` têm hoje um mecanismo para
interromper um handler já em execução dentro de ``JobEngine.advance()``
(essa chamada é síncrona e não cooperativa). "Esperar um ponto seguro"
significa, nesta etapa: um pause/stop_after_current bloqueia toda **nova**
reivindicação de Job a partir do momento em que é persistido (agora com a
garantia transacional descrita acima) — tanto em
``JobEngine.fetch_pending_jobs()`` quanto em ``JobEngine._claim()`` — mas
nunca interrompe um handler que já estava em execução quando o pause foi
solicitado. Esse handler termina normalmente, e o próximo
``advance()``/``run_pending()`` já encontrará o bloqueio.

CANCELAMENTO SEGURO DE JOB EM EXECUÇÃO ATIVA (CORREÇÃO PÓS-REVISÃO)
--------------------------------------------------------------------
A implementação original transicionava qualquer Job elegível direto para
``CANCELLED`` via ``OperationalAuditLog.cancel``, inclusive um Job
``PROCESSING`` — que a State Machine central permite (``PROCESSING ->
CANCELLED`` é uma aresta válida). Isso é inseguro: nada garante que o
handler que reivindicou esse Job já terminou. Foi reproduzido: handler ainda
rodando, ``cancel(JOB)`` marca ``CANCELLED``, o handler termina depois e
tenta persistir seu resultado (``CANCELLED -> READY`` ou qualquer outro
alvo) — transição inválida, ``JobEngine`` levanta ``JobHandlerError``
artificial mesmo o handler tendo funcionado corretamente.

A correção: ``cancel()`` nunca mais transiciona diretamente um Job cujo
estado atual represente execução ativa ou resultado remoto ainda incerto —
``_CANCEL_DEFERRED_STATUSES = {PROCESSING, PUBLISHING, RECOVERING, UNKNOWN}``.
Para esses Jobs, a *intenção* de cancelamento é persistida (mesmo mecanismo
de flag usado por pause/stop, escopo ``JOB``, flag ``cancel_requested``) e o
resultado reportado é "deferred" — nunca "cancelled". Essa decisão (ler o
estado atual do Job e decidir cancelar-agora vs. registrar-intenção) é feita
atomicamente dentro de um único ``BEGIN IMMEDIATE`` (``_cancel_or_defer_job``),
pelo mesmo motivo do parágrafo anterior sobre concorrência: sem isso, um Job
lido como ``PENDING`` poderia ser reivindicado por um ``JobEngine`` (virando
``PROCESSING``) bem no meio da decisão de cancelamento, recriando o mesmo bug.

O ``JobEngine`` (ver ``job_engine.py``) observa essa intenção em exatamente
um ponto seguro, e só para ``claims_status == PROCESSING`` (trabalho local,
sem efeito remoto): depois que o handler já terminou com sucesso e antes de
persistir o resultado dele, se um cancelamento foi solicitado nesse meio
tempo, o Job é finalizado como ``CANCELLED`` em vez do alvo que o handler
declarou — isso nunca acontece para ``PUBLISHING``/``RECOVERING``/``UNKNOWN``
(a intenção fica registrada e consultável, mas o Engine jamais mascara um
resultado remoto incerto ou já em reconciliação com ``CANCELLED``; alguém
handling isso mais tarde, quando o Job pousar num estado não-ativo, pode
chamar ``cancel()`` de novo e ela será aplicada normalmente nesse momento).

CANCEL NÃO APAGA O ORIGINAL
------------------------------
``cancel()`` em qualquer escopo apenas transiciona o(s) ``Job(s)`` alvo para
``CANCELLED`` através do mesmo caminho append-only e auditável usado para
qualquer outra transição de Job (``OperationalAuditLog.transition_job``).
Nunca apaga ``SourceAsset``, ``Video``, ``Artifact`` nem qualquer arquivo.

CANCEL POR ACCOUNT/PLATFORM: PERSISTIDO COMO INTENÇÃO, NUNCA FINGIDO
--------------------------------------------------------------------
Pelo mesmo motivo estrutural já descrito (``Job`` não referencia
``account_id``/``platform``), ``cancel(CONTROL_SCOPE_ACCOUNT, ...)`` e
``cancel(CONTROL_SCOPE_PLATFORM, ...)`` **não podem** resolver quais Jobs
pertencem à conta/plataforma indicada. A implementação original levantava
``UnsupportedBulkCancelScopeError`` para esses dois escopos, deixando o
contrato dos cinco níveis incompleto (nenhuma persistência acontecia).

A correção: esses dois escopos agora persistem a intenção de cancelamento
como um flag consultável (``cancel_requested`` no escopo ``ACCOUNT``/
``PLATFORM``, mesmo mecanismo de ``pause``/``stop_after_current``) e
``cancel()`` devolve um ``ScopeCancelOutcome`` com
``status=CANCEL_OUTCOME_DEFERRED`` e nenhum id de Job em nenhuma das
tuplas — a API deixa explícito que **nada foi cancelado agora**, apenas que
a intenção foi registrada de forma durável e auditável para um Connector
futuro consultar (``is_cancel_requested(CONTROL_SCOPE_ACCOUNT, ...)``/
``is_cancel_requested(CONTROL_SCOPE_PLATFORM, ...)``) antes de agir.
``UnsupportedBulkCancelScopeError`` permanece definida/exportada (ainda é um
``ControlManagerError`` válido de se levantar em cenários futuros), mas
``cancel()`` não a levanta mais para ``ACCOUNT``/``PLATFORM``.

RESULTADO DE CANCEL NUNCA INVENTA SUCESSO
--------------------------------------------
``ScopeCancelOutcome.status`` é sempre um de ``CANCEL_OUTCOME_FULLY_APPLIED``,
``CANCEL_OUTCOME_PARTIALLY_APPLIED``, ``CANCEL_OUTCOME_NOT_APPLIED``,
``CANCEL_OUTCOME_DEFERRED`` ou ``CANCEL_OUTCOME_ERROR`` — nunca um booleano
solto que uma UI futura possa mal interpretar como "cancelamento efetuado"
quando zero alvos foram efetivamente cancelados agora. ``.ok`` continua
existindo como atalho, mas significa estritamente
``status == CANCEL_OUTCOME_FULLY_APPLIED``.

CANCELAMENTO EM MASSA (QUEUE/GLOBAL) É CRASH-SAFE (CORREÇÃO PÓS-REVISÃO)
--------------------------------------------------------------------------
A implementação original só registrava o evento de auditoria
``CONTROL_CANCEL_SCOPE_REQUESTED`` **depois** que o laço de cancelamento
terminava. Um crash no meio do laço (ex.: processo morto depois de cancelar
o primeiro de três Jobs) deixava o sistema sem nenhum registro durável de
que o operador pediu um cancelamento em massa — na reinicialização, dois
Jobs continuavam ``PENDING`` como se nada tivesse sido pedido.

A correção: antes de tentar cancelar qualquer Job candidato, ``cancel()``
para ``QUEUE``/``GLOBAL`` persiste um registro de lote
(``control:<scope>:<scope_id>:cancel_batch_request`` na tabela ``settings``,
mesmo padrão de sobrescrita autocontida dos demais flags) com
``status="IN_PROGRESS"`` e a lista de ids candidatos, mais um evento de
auditoria — TUDO isso commitado antes de qualquer efeito em qualquer Job.
Só depois o laço roda. Ao final (mesmo que parcialmente, por erro isolado em
algum Job), o mesmo registro é atualizado para ``status="COMPLETED"`` com os
totais. Se o processo morrer no meio do laço, o registro fica
``IN_PROGRESS`` — um crash nunca apaga o fato de que o cancelamento em
massa foi pedido, ele apenas descreve honestamente que ficou incompleto.

Não foi criado um Batch Engine (fora de escopo, PROMPT 17): a retomada é
manual — reinvocar ``cancel()`` para o mesmo escopo. Isso é seguro porque
cada Job candidato é reavaliado individualmente contra seu estado *atual*
(um Job já ``CANCELLED`` é idempotentemente tratado como sucesso, nunca
reprocessado como erro/skip — ver ``_cancel_or_defer_job``), então repetir a
mesma solicitação de cancelamento em massa depois de um crash (ou só por
segurança) é sempre seguro e nunca duplica efeito.

CORREÇÕES DA TERCEIRA REVISÃO ADVERSARIAL
--------------------------------------------
Três janelas determinísticas adicionais foram fechadas depois que as
correções acima já estavam em produção:

1. **Corrida entre ``cancel_requested`` e a transição final pós-PROCESSING.**
   O ``JobEngine`` lia ``is_job_cancel_requested`` numa conexão solta e só
   depois persistia o resultado final numa transaction separada — a mesma
   classe de bug do pause-vs-claim, só que no outro lado do ciclo de vida do
   Job. Corrigido: ``JobEngine._finalize_processing_result`` agora faz a
   leitura de ``cancel_requested`` E a transição final dentro do MESMO
   ``BEGIN IMMEDIATE``; quando o cancelamento é aplicado, a transição para
   ``CANCELLED`` e a limpeza de ``cancel_requested`` (``mark_job_cancel_applied``,
   que agora aceita ``connection``) acontecem na mesma transaction — nunca
   existe uma janela de commit onde ``CANCELLED`` já está persistido mas
   ``cancel_requested`` ainda aparece ativo.

2. **Cancelamento em massa ``IN_PROGRESS`` não impedia claim depois de um
   restart.** Um Job que fazia parte de uma solicitação ``GLOBAL``/``QUEUE``
   ainda incompleta (crash no meio do laço) continuava ``PENDING`` sem
   nenhum flag de ``JOB`` individual — porque a decisão "cancelar este Job"
   nunca tinha chegado a rodar para ele. ``is_execution_blocked`` agora
   também verifica, para qualquer ``job_id`` informado, se ele está listado
   nos candidatos de um ``cancel_batch_request`` ``IN_PROGRESS`` em
   ``GLOBAL`` ou na sua ``QUEUE`` — lendo o registro persistido, nunca
   estado em memória, então a proteção sobrevive a fechar/reabrir o
   processo sem precisar de nenhum bootstrap novo (nada do PROMPT 16 foi
   antecipado).

3. **``cancel_requested`` de um Job era ignorado num novo claim local
   (ex.: depois de ``FAILED -> RETRY``).** Um cancelamento pedido durante
   ``PROCESSING`` que não chegou a ser aplicado (porque o handler falhou e o
   Job pousou em ``FAILED``, e alguém decidiu ``RETRY`` manualmente) não
   impedia uma nova execução local do zero — o handler rodava de novo, e só
   ao final o cancelamento seria aplicado, desperdiçando trabalho e
   contrariando a intenção já registrada. Corrigido em ``JobEngine._claim``:
   quando ``claims_status == PROCESSING`` e existe ``cancel_requested``
   pendente para o Job, a reivindicação é recusada (mesma exceção
   ``JobBlockedByControlError``) antes de chamar o handler — verificado sob
   o mesmo ``BEGIN IMMEDIATE`` da própria reivindicação. Deliberadamente
   **não** se aplica a ``claims_status == RECOVERING``: uma reconciliação
   de resultado remoto incerto precisa poder continuar mesmo com um cancel
   pendente, porque só ela pode resolver a incerteza — bloquear isso
   deixaria o Job preso em ``UNKNOWN``/``RECOVERING`` para sempre.

4. **Retomada de lote usava um snapshot recalculado, não o original.** Ao
   reinvocar ``cancel()`` para um escopo com ``cancel_batch_request``
   ``IN_PROGRESS``, a implementação recalculava a lista de candidatos a
   partir do estado atual do banco — incluindo Jobs criados DEPOIS da
   solicitação original, que nunca deveriam fazer parte dessa retomada.
   Corrigido: quando existe um registro ``IN_PROGRESS`` para o escopo,
   ``cancel()`` reutiliza exatamente o ``candidate_job_ids`` já persistido
   (nunca recalcula), e registra um evento de auditoria
   (``CONTROL_CANCEL_SCOPE_RESUMED``) distinto de uma solicitação nova. Só
   depois que esse lote chega a ``COMPLETED`` é que uma próxima chamada de
   ``cancel()`` para o mesmo escopo calcula um snapshot novo (e aí sim
   Jobs criados nesse meio tempo entram).

Nenhuma migration nova foi necessária para nenhuma dessas correções.

CORREÇÕES DA QUARTA REVISÃO ADVERSARIAL
----------------------------------------

1. **``cancel(JOB)`` ``IN_PROGRESS`` não bloqueava depois de um crash.**
   ``GLOBAL``/``QUEUE`` já tinham essa proteção (ver correção 2 acima), mas
   ``cancel(JOB, job_id)`` também persiste seu próprio
   ``cancel_batch_request`` (``scope=JOB``, ``status=IN_PROGRESS``) ANTES de
   chamar ``_cancel_or_defer_job`` — e se um crash acontecesse exatamente
   nessa janela, nada impedia o Job de ser reivindicado como trabalho novo
   com ``cancel_requested=False``. Corrigido generalizando a mesma checagem
   já usada para ``GLOBAL``/``QUEUE`` em ``is_execution_blocked``: agora
   também verifica, para qualquer ``job_id`` informado, se existe um
   ``cancel_batch_request`` de escopo ``JOB`` ``IN_PROGRESS`` para esse
   Job — lido do registro persistido, dentro da mesma ``BEGIN IMMEDIATE``
   do ``_claim``, sobrevivendo a fechar/reabrir o processo sem nenhum
   mecanismo novo.

2. **Snapshot novo de cancelamento em massa incluía Jobs em estado
   terminal.** ``cancel(GLOBAL)``/``cancel(QUEUE, ...)`` calculavam um
   snapshot novo a partir de TODOS os Jobs do banco, incluindo
   ``PUBLISHED``/``CANCELLED`` históricos — que só podiam terminar como
   ``"skipped"`` (a State Machine não tem aresta de saída para nenhum dos
   dois), fazendo qualquer cancelamento em massa de uma instalação antiga
   parecer ``PARTIALLY_APPLIED`` por causa de histórico irrelevante.
   Corrigido: ao calcular um snapshot NOVO (não numa retomada) para
   ``GLOBAL``/``QUEUE``, Jobs com ``status`` em ``JOB_TERMINAL_STATES``
   (``PUBLISHED``, ``CANCELLED``) são excluídos do candidato. Isso não muda
   ``cancel(JOB, x)`` explícito (que continua podendo mirar um Job já
   terminal e corretamente receber ``NOT_APPLIED``/"skipped" para ele), nem
   uma retomada de lote já ``IN_PROGRESS`` (que sempre usa o
   ``candidate_job_ids`` original tal como persistido, sem reinterpretar
   essa regra), nem apaga nenhum histórico, nem altera a State Machine.

Nenhuma migration nova foi necessária para nenhuma dessas correções.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from .domain import (
    Job,
    InvalidJobState,
    InvalidJobTransition,
    JOB_CANCELLED,
    JOB_PROCESSING,
    JOB_PUBLISHING,
    JOB_RECOVERING,
    JOB_TERMINAL_STATES,
    JOB_UNKNOWN,
)
from .storage import AUDIT_JOB_CANCELLED, JobNotFoundForAuditError, LocalDatabase, OperationalAuditLog
from .time_utils import utc_now_iso


CONTROL_SCOPE_GLOBAL = "GLOBAL"
CONTROL_SCOPE_ACCOUNT = "ACCOUNT"
CONTROL_SCOPE_PLATFORM = "PLATFORM"
CONTROL_SCOPE_QUEUE = "QUEUE"
CONTROL_SCOPE_JOB = "JOB"

CONTROL_SCOPES = frozenset(
    {
        CONTROL_SCOPE_GLOBAL,
        CONTROL_SCOPE_ACCOUNT,
        CONTROL_SCOPE_PLATFORM,
        CONTROL_SCOPE_QUEUE,
        CONTROL_SCOPE_JOB,
    }
)

# Escopos que o JobEngine consegue resolver e aplicar automaticamente hoje,
# só com dados que o Job já carrega (ver docstring do módulo).
AUTO_ENFORCED_SCOPES = frozenset({CONTROL_SCOPE_GLOBAL, CONTROL_SCOPE_QUEUE, CONTROL_SCOPE_JOB})

# Escopos persistidos/consultáveis, mas sem aplicação automática pelo
# JobEngine nesta etapa — pendentes de um Connector futuro que saiba a
# conta/plataforma de cada Job.
MANUALLY_ENFORCED_SCOPES = frozenset({CONTROL_SCOPE_ACCOUNT, CONTROL_SCOPE_PLATFORM})

assert AUTO_ENFORCED_SCOPES | MANUALLY_ENFORCED_SCOPES == CONTROL_SCOPES
assert AUTO_ENFORCED_SCOPES.isdisjoint(MANUALLY_ENFORCED_SCOPES)

# Estados de Job em que um cancelamento direto/imediato NUNCA é seguro:
# PROCESSING/RECOVERING permitem tecnicamente a aresta -> CANCELLED na State
# Machine central, mas um handler pode estar ativamente rodando; PUBLISHING
# e UNKNOWN representam um efeito remoto ainda incerto, que jamais pode ser
# mascarado por um CANCELLED. Deliberadamente reescrito aqui (não importado
# de job_engine.CLAIMABLE_STATES) para preservar a independência entre os
# dois módulos — ver docstring do módulo.
_CANCEL_DEFERRED_STATUSES = frozenset({JOB_PROCESSING, JOB_PUBLISHING, JOB_RECOVERING, JOB_UNKNOWN})

_FLAG_PAUSED = "paused"
_FLAG_STOP_AFTER_CURRENT = "stop_after_current"
_FLAG_CANCEL_REQUESTED = "cancel_requested"

_EVENT_PAUSED = "CONTROL_PAUSED"
_EVENT_RESUMED = "CONTROL_RESUMED"
_EVENT_STOP_REQUESTED = "CONTROL_STOP_AFTER_CURRENT_REQUESTED"
_EVENT_CANCEL_INTENT_RECORDED = "CONTROL_CANCEL_INTENT_RECORDED"
_EVENT_CANCEL_INTENT_CLEARED = "CONTROL_CANCEL_INTENT_CLEARED"
_EVENT_CANCEL_SCOPE_REQUESTED = "CONTROL_CANCEL_SCOPE_REQUESTED"
_EVENT_CANCEL_SCOPE_RESUMED = "CONTROL_CANCEL_SCOPE_RESUMED"
_EVENT_CANCEL_SCOPE_COMPLETED = "CONTROL_CANCEL_SCOPE_COMPLETED"

_CONTROL_ENTITY_TYPE = "ControlScope"

CANCEL_OUTCOME_FULLY_APPLIED = "FULLY_APPLIED"
CANCEL_OUTCOME_PARTIALLY_APPLIED = "PARTIALLY_APPLIED"
CANCEL_OUTCOME_NOT_APPLIED = "NOT_APPLIED"
CANCEL_OUTCOME_DEFERRED = "DEFERRED"
CANCEL_OUTCOME_ERROR = "ERROR"

CANCEL_OUTCOME_STATUSES = frozenset(
    {
        CANCEL_OUTCOME_FULLY_APPLIED,
        CANCEL_OUTCOME_PARTIALLY_APPLIED,
        CANCEL_OUTCOME_NOT_APPLIED,
        CANCEL_OUTCOME_DEFERRED,
        CANCEL_OUTCOME_ERROR,
    }
)


class ControlManagerError(RuntimeError):
    """Erro de contrato do ControlManager central."""


class InvalidControlScopeError(ControlManagerError, ValueError):
    """``scope`` fora do vocabulário canônico (``CONTROL_SCOPES``)."""


class ControlScopeIdError(ControlManagerError, ValueError):
    """``scope_id`` ausente quando exigido, informado quando proibido, ou
    com formato inválido para o escopo (ex.: ``JOB`` exige um UUID)."""


class UnsupportedBulkCancelScopeError(ControlManagerError):
    """Reservada para uso futuro.

    Até a revisão pós-implementação, ``cancel(ACCOUNT/PLATFORM, ...)``
    levantava este erro. Isso deixava o contrato dos cinco níveis
    incompleto (nada era persistido). Agora esses escopos persistem a
    intenção de cancelamento (ver docstring do módulo) em vez de levantar
    este erro; a classe permanece definida/exportada para eventuais usos
    futuros que precisem recusar explicitamente um cancelamento em massa.
    """


def _compute_cancel_status(
    *,
    cancelled: list[str],
    deferred: list[str],
    skipped: list[str],
    errors: list[tuple[str, str]],
) -> str:
    """Classifica o resultado agregado de ``cancel()`` sem nunca inventar
    sucesso: ``ok``/``status`` só relatam ``FULLY_APPLIED`` quando todo alvo
    elegível foi de fato cancelado agora (ou já estava cancelado)."""
    if errors:
        return CANCEL_OUTCOME_ERROR if not cancelled and not deferred else CANCEL_OUTCOME_PARTIALLY_APPLIED
    if deferred or skipped:
        if cancelled:
            return CANCEL_OUTCOME_PARTIALLY_APPLIED
        if deferred and not skipped:
            return CANCEL_OUTCOME_DEFERRED
        return CANCEL_OUTCOME_NOT_APPLIED
    return CANCEL_OUTCOME_FULLY_APPLIED


@dataclass(frozen=True)
class ScopeCancelOutcome:
    """Resultado de ``ControlManager.cancel()`` para qualquer escopo.

    Uniforme entre ``JOB`` (no máximo um id candidato), ``QUEUE``/``GLOBAL``
    (potencialmente vários) e ``ACCOUNT``/``PLATFORM`` (nenhum id resolvível
    — ver ``status``/docstring do módulo): cada Job candidato é isolado
    individualmente, então a falha ao cancelar um nunca impede a tentativa
    nos demais.

    ``status`` é a fonte de verdade sobre o que realmente aconteceu — nunca
    interprete ausência de ``errors`` como sucesso; use ``status`` (ou o
    atalho ``.ok``, que só é ``True`` para ``CANCEL_OUTCOME_FULLY_APPLIED``).
    """

    scope: str
    scope_id: str | None
    status: str
    cancelled_job_ids: tuple[str, ...]
    deferred_job_ids: tuple[str, ...] = field(default=())
    skipped_job_ids: tuple[str, ...] = field(default=())
    errors: tuple[tuple[str, str], ...] = field(default=())

    def __post_init__(self) -> None:
        if self.status not in CANCEL_OUTCOME_STATUSES:
            raise ValueError(f"status de cancelamento inválido: {self.status!r}")

    @property
    def ok(self) -> bool:
        """``True`` somente quando ``status == CANCEL_OUTCOME_FULLY_APPLIED``.

        Não confundir com "nenhum erro": um resultado ``DEFERRED`` ou
        ``NOT_APPLIED`` não tem erro nenhum, mas também não cancelou nada —
        ``ok`` é estritamente sobre sucesso completo, para uma UI futura
        nunca interpretar "sem erro" como "cancelado".
        """
        return self.status == CANCEL_OUTCOME_FULLY_APPLIED


class ControlManager:
    """Pause, resume, stop_after_current e cancel em cinco níveis.

    Ver a docstring do módulo para o que é aplicado automaticamente pelo
    ``JobEngine`` (``GLOBAL``/``QUEUE``/``JOB``) e o que é apenas persistido
    e consultável, pendente de integração futura (``ACCOUNT``/``PLATFORM``),
    e para a garantia transacional contra corrida com reivindicações de Job.
    """

    def __init__(
        self,
        database: LocalDatabase,
        *,
        audit_log: OperationalAuditLog | None = None,
    ) -> None:
        self.database = database
        self.audit_log = audit_log if audit_log is not None else OperationalAuditLog(database)

    # -- Validação/normalização de escopo ------------------------------------

    @staticmethod
    def _validate_scope(scope: str) -> str:
        if scope not in CONTROL_SCOPES:
            raise InvalidControlScopeError(
                f"escopo de controle inválido: {scope!r}; use um de {sorted(CONTROL_SCOPES)}"
            )
        return scope

    @classmethod
    def _resolve(cls, scope: str, scope_id: str | None) -> tuple[str, str | None]:
        scope = cls._validate_scope(scope)
        if scope == CONTROL_SCOPE_GLOBAL:
            if scope_id not in (None, ""):
                raise ControlScopeIdError("scope_id não deve ser informado para o escopo GLOBAL")
            return scope, None

        if scope_id is None or not str(scope_id).strip():
            raise ControlScopeIdError(f"scope_id é obrigatório para o escopo {scope}")
        scope_id = str(scope_id).strip()

        if scope == CONTROL_SCOPE_JOB:
            try:
                UUID(scope_id)
            except (ValueError, AttributeError, TypeError) as exc:
                raise ControlScopeIdError(
                    f"scope_id do escopo JOB deve ser um UUID válido: {scope_id!r}"
                ) from exc

        return scope, scope_id

    @staticmethod
    def _key(scope: str, scope_id: str | None, flag: str) -> str:
        if scope_id is None:
            return f"control:{scope}:{flag}"
        return f"control:{scope}:{scope_id}:{flag}"

    # -- Leitura de flags -----------------------------------------------------

    def is_paused(self, scope: str, scope_id: str | None = None) -> bool:
        scope, scope_id = self._resolve(scope, scope_id)
        return self._flag_active(scope, scope_id, _FLAG_PAUSED)

    def is_stop_requested(self, scope: str, scope_id: str | None = None) -> bool:
        scope, scope_id = self._resolve(scope, scope_id)
        return self._flag_active(scope, scope_id, _FLAG_STOP_AFTER_CURRENT)

    def is_cancel_requested(
        self, scope: str, scope_id: str | None = None, *, connection: sqlite3.Connection | None = None
    ) -> bool:
        """``True`` se uma intenção de cancelamento está registrada para o
        escopo — para ``JOB``, significa "cancelamento pedido enquanto o Job
        estava em execução ativa/incerta, ainda não aplicado"; para
        ``ACCOUNT``/``PLATFORM``, significa "operador pediu para cancelar
        esta conta/plataforma, pendente de um Connector futuro aplicar".

        ``connection`` (correção pós-3ª revisão — corrida entre o ponto
        seguro pós-PROCESSING e uma transição final): quando informada,
        permite ao ``JobEngine`` ler este flag sob o MESMO ``BEGIN
        IMMEDIATE`` da própria transição final, fechando a mesma classe de
        janela já fechada para pause-vs-claim (ver docstring do módulo e
        ``JobEngine._finalize_processing_result``)."""
        scope, scope_id = self._resolve(scope, scope_id)
        return self._flag_active(scope, scope_id, _FLAG_CANCEL_REQUESTED, connection=connection)

    def is_job_cancel_requested(self, job_id: str, *, connection: sqlite3.Connection | None = None) -> bool:
        """Atalho para ``is_cancel_requested(CONTROL_SCOPE_JOB, job_id)``,
        usado pelo ``JobEngine`` no ponto seguro após um handler PROCESSING
        terminar (ver docstring do módulo)."""
        return self.is_cancel_requested(CONTROL_SCOPE_JOB, job_id, connection=connection)

    def _flag_active(
        self,
        scope: str,
        scope_id: str | None,
        flag: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> bool:
        value = self.database.get_setting(self._key(scope, scope_id, flag), connection=connection)
        return bool(isinstance(value, dict) and value.get("active"))

    def is_execution_blocked(
        self,
        *,
        operation: str | None = None,
        job_id: str | None = None,
        account_id: str | None = None,
        platform: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> bool:
        """Consulta genérica: ``True`` se qualquer escopo aplicável (dentre os
        parâmetros fornecidos, mais ``GLOBAL`` sempre) estiver pausado ou com
        stop_after_current solicitado.

        ``operation``/``job_id`` são o que o ``JobEngine`` já consegue
        derivar de um ``Job`` hoje (ver docstring do módulo).
        ``account_id``/``platform`` existem para um Connector futuro que
        conheça essa relação consultar antes de executar um efeito remoto —
        nenhum chamador deste código faz isso automaticamente ainda.

        ``connection`` (PROMPT 15, correção de corrida pause-vs-claim):
        quando informada, todas as leituras de flag usam essa conexão em vez
        de abrir leituras próprias — permite ao ``JobEngine`` compor esta
        checagem com a própria reivindicação sob o mesmo ``BEGIN IMMEDIATE``
        (ver docstring do módulo e ``JobEngine._claim``). Quando omitida
        (padrão), o comportamento é o de sempre: cada leitura abre sua
        própria conexão de leitura.

        Também bloqueia (correção pós-3ª revisão) quando ``job_id`` é um dos
        candidatos de um cancelamento em massa ``GLOBAL``/``QUEUE`` ainda
        ``IN_PROGRESS`` — inclusive depois de um restart, já que essa lista
        é lida diretamente do registro persistido, nunca de estado em
        memória (ver ``get_cancel_batch_request`` e docstring do módulo).
        Isso vale para qualquer ``claims_status``: enquanto a solicitação de
        cancelamento em massa não terminar, nenhum desses Jobs é trabalho
        novo elegível.

        Correção pós-4ª revisão: a mesma checagem agora também cobre um
        ``cancel_batch_request`` de escopo ``JOB`` ainda ``IN_PROGRESS`` para
        este ``job_id`` específico — ``cancel(JOB, job_id)`` persiste sua
        intenção IN_PROGRESS antes de decidir cancelar/adiar (ver
        ``cancel``); se um crash acontecer exatamente nessa janela, o Job
        não pode ser reivindicado como trabalho novo até que a solicitação
        seja retomada e concluída.
        """
        if self._scope_blocked(CONTROL_SCOPE_GLOBAL, None, connection=connection):
            return True
        if operation is not None and self._scope_blocked(CONTROL_SCOPE_QUEUE, operation, connection=connection):
            return True
        if job_id is not None and self._scope_blocked(CONTROL_SCOPE_JOB, job_id, connection=connection):
            return True
        if account_id is not None and self._scope_blocked(
            CONTROL_SCOPE_ACCOUNT, account_id, connection=connection
        ):
            return True
        if platform is not None and self._scope_blocked(CONTROL_SCOPE_PLATFORM, platform, connection=connection):
            return True
        if job_id is not None and self._within_incomplete_cancel_batch(
            CONTROL_SCOPE_GLOBAL, None, job_id, connection=connection
        ):
            return True
        if (
            job_id is not None
            and operation is not None
            and self._within_incomplete_cancel_batch(CONTROL_SCOPE_QUEUE, operation, job_id, connection=connection)
        ):
            return True
        if job_id is not None and self._within_incomplete_cancel_batch(
            CONTROL_SCOPE_JOB, job_id, job_id, connection=connection
        ):
            return True
        return False

    def _within_incomplete_cancel_batch(
        self,
        scope: str,
        scope_id: str | None,
        job_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> bool:
        batch = self.database.get_setting(self._key(scope, scope_id, "cancel_batch_request"), connection=connection)
        if not isinstance(batch, dict) or batch.get("status") != "IN_PROGRESS":
            return False
        candidates = batch.get("candidate_job_ids")
        return isinstance(candidates, list) and job_id in candidates

    def is_job_within_incomplete_cancel_batch(self, job_id: str, *, operation: str | None = None) -> bool:
        """Consulta pública equivalente à checagem que ``is_execution_blocked``
        já faz internamente — útil para diagnóstico/observabilidade (ex.: uma
        UI futura explicando por que um Job não está sendo executado)."""
        if self._within_incomplete_cancel_batch(CONTROL_SCOPE_GLOBAL, None, job_id):
            return True
        if operation is not None and self._within_incomplete_cancel_batch(CONTROL_SCOPE_QUEUE, operation, job_id):
            return True
        if self._within_incomplete_cancel_batch(CONTROL_SCOPE_JOB, job_id, job_id):
            return True
        return False

    def _scope_blocked(
        self, scope: str, scope_id: str | None, *, connection: sqlite3.Connection | None = None
    ) -> bool:
        scope, scope_id = self._resolve(scope, scope_id)
        return self._flag_active(scope, scope_id, _FLAG_PAUSED, connection=connection) or self._flag_active(
            scope, scope_id, _FLAG_STOP_AFTER_CURRENT, connection=connection
        )

    # -- Escrita de flags -------------------------------------------------------

    def pause(self, scope: str, scope_id: str | None = None, *, reason: str | None = None) -> None:
        """Ativa o flag ``paused`` do escopo. Idempotente: pausar um escopo já
        pausado apenas atualiza o motivo/timestamp, sem efeito adicional."""
        scope, scope_id = self._resolve(scope, scope_id)
        self._write_flag(scope, scope_id, _FLAG_PAUSED, active=True, reason=reason, event=_EVENT_PAUSED)

    def resume(self, scope: str, scope_id: str | None = None, *, reason: str | None = None) -> None:
        """Limpa tanto ``paused`` quanto ``stop_after_current`` do escopo.

        Do ponto de vista de bloqueio de novas reivindicações, os dois flags
        têm o mesmo efeito (ver ``is_execution_blocked``); "retomar" um
        escopo significa voltar a aceitar trabalho novo nele
        independentemente de qual dos dois motivos o bloqueava. Não afeta
        ``cancel_requested`` — resumir um escopo pausado não cancela nem
        des-cancela nada.
        """
        scope, scope_id = self._resolve(scope, scope_id)
        with self.database.transaction() as conn:
            self.database.set_setting(
                self._key(scope, scope_id, _FLAG_PAUSED),
                {"active": False, "reason": reason, "updated_at": utc_now_iso()},
                connection=conn,
            )
            self.database.set_setting(
                self._key(scope, scope_id, _FLAG_STOP_AFTER_CURRENT),
                {"active": False, "reason": reason, "updated_at": utc_now_iso()},
                connection=conn,
            )
            self.database.append_audit_event(
                _EVENT_RESUMED,
                entity_type=_CONTROL_ENTITY_TYPE,
                data={"scope": scope, "scope_id": scope_id, "reason": reason},
                connection=conn,
            )

    def request_stop_after_current(
        self, scope: str, scope_id: str | None = None, *, reason: str | None = None
    ) -> None:
        """Ativa o flag ``stop_after_current`` do escopo: nenhum trabalho novo
        é reivindicado a partir de agora, mas nada em execução é
        interrompido (ver docstring do módulo)."""
        scope, scope_id = self._resolve(scope, scope_id)
        self._write_flag(
            scope, scope_id, _FLAG_STOP_AFTER_CURRENT, active=True, reason=reason, event=_EVENT_STOP_REQUESTED
        )

    def _write_flag(
        self,
        scope: str,
        scope_id: str | None,
        flag: str,
        *,
        active: bool,
        reason: str | None,
        event: str,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        def _do(conn: sqlite3.Connection) -> None:
            self.database.set_setting(
                self._key(scope, scope_id, flag),
                {"active": active, "reason": reason, "updated_at": utc_now_iso()},
                connection=conn,
            )
            self.database.append_audit_event(
                event,
                entity_type=_CONTROL_ENTITY_TYPE,
                data={"scope": scope, "scope_id": scope_id, "reason": reason},
                connection=conn,
            )

        if connection is not None:
            _do(connection)
        else:
            with self.database.transaction() as conn:
                _do(conn)

    def mark_job_cancel_applied(
        self, job_id: str, *, reason: str | None = None, connection: sqlite3.Connection | None = None
    ) -> None:
        """Limpa ``cancel_requested`` de um Job depois que o ``JobEngine`` já
        aplicou o cancelamento adiado (ver docstring do módulo). Idempotente:
        chamar isso de novo, ou sobre um Job sem intenção pendente, é
        inofensivo — o flag já ausente/inativo permanece assim.

        ``connection``: quando informada (o caso normal a partir do PROMPT 15
        pós-3ª revisão), esta limpeza roda na MESMA transaction que já
        persistiu a transição para ``CANCELLED`` — nunca existe uma janela
        entre "``CANCELLED`` commitado" e "``cancel_requested`` ainda ativo
        por acidente" (ver ``JobEngine._finalize_processing_result``).
        """
        scope, scope_id = self._resolve(CONTROL_SCOPE_JOB, job_id)
        self._write_flag(
            scope,
            scope_id,
            _FLAG_CANCEL_REQUESTED,
            active=False,
            reason=reason,
            event=_EVENT_CANCEL_INTENT_CLEARED,
            connection=connection,
        )

    # -- Cancel -----------------------------------------------------------------

    def _cancel_or_defer_job(self, job_id: str, *, reason: str | None) -> str:
        """Decide e aplica o cancelamento de um único Job atomicamente.

        Lê o estado atual do Job e decide o que fazer com ele dentro do
        MESMO ``BEGIN IMMEDIATE`` (nunca separando leitura e escrita em duas
        transactions) — a mesma técnica usada para a corrida pause-vs-claim
        (ver docstring do módulo), aqui aplicada para que um Job lido como
        elegível não possa ser reivindicado por um JobEngine concorrente
        entre a leitura e a escrita.

        Retorna um de ``"cancelled"``, ``"deferred"`` ou ``"skipped"``.
        Levanta ``JobNotFoundForAuditError`` se o Job não existir.
        """
        cancel_data = {"reason": reason} if reason is not None else None
        with self.database.transaction() as conn:
            job = self.database.get(Job, job_id, connection=conn)
            if job is None:
                raise JobNotFoundForAuditError(f"Job não encontrado: {job_id}")

            if job.status == JOB_CANCELLED:
                # Idempotente: o estado desejado já é este. Limpa qualquer
                # intenção residual (não deveria haver, mas é inofensivo).
                self._write_flag(
                    CONTROL_SCOPE_JOB,
                    job_id,
                    _FLAG_CANCEL_REQUESTED,
                    active=False,
                    reason=reason,
                    event=_EVENT_CANCEL_INTENT_CLEARED,
                    connection=conn,
                )
                return "cancelled"

            if job.status in _CANCEL_DEFERRED_STATUSES:
                self._write_flag(
                    CONTROL_SCOPE_JOB,
                    job_id,
                    _FLAG_CANCEL_REQUESTED,
                    active=True,
                    reason=reason,
                    event=_EVENT_CANCEL_INTENT_RECORDED,
                    connection=conn,
                )
                return "deferred"

            try:
                self.audit_log.transition_job(
                    job_id,
                    JOB_CANCELLED,
                    semantic_event=AUDIT_JOB_CANCELLED,
                    data=cancel_data,
                    connection=conn,
                )
            except (InvalidJobState, InvalidJobTransition):
                return "skipped"

            self._write_flag(
                CONTROL_SCOPE_JOB,
                job_id,
                _FLAG_CANCEL_REQUESTED,
                active=False,
                reason=reason,
                event=_EVENT_CANCEL_INTENT_CLEARED,
                connection=conn,
            )
            return "cancelled"

    def _record_scope_cancel_intent(self, scope: str, scope_id: str | None, *, reason: str | None) -> None:
        """Persiste a intenção de cancelamento em massa de ``ACCOUNT``/
        ``PLATFORM`` — ver docstring do módulo: nenhum Job é resolvido nem
        tocado aqui, apenas um flag consultável para um Connector futuro."""
        self._write_flag(
            scope, scope_id, _FLAG_CANCEL_REQUESTED, active=True, reason=reason, event=_EVENT_CANCEL_INTENT_RECORDED
        )

    def _persist_cancel_batch_request(
        self,
        scope: str,
        scope_id: str | None,
        *,
        reason: str | None,
        candidate_job_ids: list[str],
    ) -> None:
        """Registra, ANTES de qualquer efeito, que um cancelamento em massa
        foi pedido (Bloqueador de crash-safety pós-revisão — ver docstring
        do módulo). Sobrescrita autocontida: reinvocar ``cancel()`` para o
        mesmo escopo apenas atualiza este mesmo registro, nunca acumula
        histórico ilimitado."""
        with self.database.transaction() as conn:
            self.database.set_setting(
                self._key(scope, scope_id, "cancel_batch_request"),
                {
                    "status": "IN_PROGRESS",
                    "reason": reason,
                    "candidate_job_ids": list(candidate_job_ids),
                    "requested_at": utc_now_iso(),
                },
                connection=conn,
            )
            self.database.append_audit_event(
                _EVENT_CANCEL_SCOPE_REQUESTED,
                entity_type=_CONTROL_ENTITY_TYPE,
                data={
                    "scope": scope,
                    "scope_id": scope_id,
                    "reason": reason,
                    "candidate_count": len(candidate_job_ids),
                },
                connection=conn,
            )

    def _complete_cancel_batch_request(self, scope: str, scope_id: str | None, *, outcome: ScopeCancelOutcome) -> None:
        with self.database.transaction() as conn:
            self.database.set_setting(
                self._key(scope, scope_id, "cancel_batch_request"),
                {
                    "status": "COMPLETED",
                    "reason": None,
                    "completed_at": utc_now_iso(),
                    "result_status": outcome.status,
                    "cancelled_count": len(outcome.cancelled_job_ids),
                    "deferred_count": len(outcome.deferred_job_ids),
                    "skipped_count": len(outcome.skipped_job_ids),
                    "error_count": len(outcome.errors),
                },
                connection=conn,
            )
            self.database.append_audit_event(
                _EVENT_CANCEL_SCOPE_COMPLETED,
                entity_type=_CONTROL_ENTITY_TYPE,
                data={
                    "scope": scope,
                    "scope_id": scope_id,
                    "status": outcome.status,
                    "cancelled_count": len(outcome.cancelled_job_ids),
                    "deferred_count": len(outcome.deferred_job_ids),
                    "skipped_count": len(outcome.skipped_job_ids),
                    "error_count": len(outcome.errors),
                },
                connection=conn,
            )

    def _record_cancel_batch_resumed(
        self, scope: str, scope_id: str | None, *, candidate_job_ids: list[str]
    ) -> None:
        """Registra (auditoria apenas — não toca o registro ``settings``)
        que ``cancel()`` retomou um lote ``IN_PROGRESS`` já existente usando
        o snapshot original de candidatos, em vez de recalcular um novo a
        partir do estado atual do banco (correção pós-3ª revisão — ver
        docstring do módulo)."""
        with self.database.transaction() as conn:
            self.database.append_audit_event(
                _EVENT_CANCEL_SCOPE_RESUMED,
                entity_type=_CONTROL_ENTITY_TYPE,
                data={
                    "scope": scope,
                    "scope_id": scope_id,
                    "candidate_count": len(candidate_job_ids),
                },
                connection=conn,
            )

    def get_cancel_batch_request(self, scope: str, scope_id: str | None = None) -> dict[str, Any] | None:
        """Consulta o último registro de cancelamento em massa persistido
        para ``(scope, scope_id)`` — ``status`` é ``"IN_PROGRESS"`` (inclusive
        depois de um crash no meio do laço) ou ``"COMPLETED"``. ``None`` se
        nenhum cancelamento em massa foi pedido ainda para este escopo."""
        scope, scope_id = self._resolve(scope, scope_id)
        value = self.database.get_setting(self._key(scope, scope_id, "cancel_batch_request"))
        return value if isinstance(value, dict) else None

    def cancel(self, scope: str, scope_id: str | None = None, *, reason: str | None = None) -> ScopeCancelOutcome:
        """Cancela (ou registra a intenção de cancelar) o(s) Job(s) do escopo.

        ``JOB``: decide atomicamente cancelar agora ou adiar (ver
        ``_cancel_or_defer_job``) exatamente esse Job (ou registra erro se
        ele não existir). ``QUEUE``: mesma decisão para todos os Jobs com
        ``operation == scope_id``. ``GLOBAL``: idem, sem filtrar por
        ``operation`` — nesses dois casos a intenção do lote é persistida
        de forma crash-safe ANTES de qualquer efeito (ver docstring do
        módulo). ``ACCOUNT``/``PLATFORM``: persiste a intenção como um flag
        consultável e devolve ``status=CANCEL_OUTCOME_DEFERRED`` sem tocar
        nenhum Job — nunca finge que algo foi cancelado.

        Chamar ``cancel()`` de novo para o mesmo escopo (ex.: depois de um
        crash, ou só por segurança) é sempre seguro: cada Job candidato é
        reavaliado contra seu estado atual, e um Job já ``CANCELLED`` é
        tratado como sucesso idempotente, nunca reprocessado como erro.
        Quando já existe um registro de lote ``IN_PROGRESS`` para este
        escopo (retomada após crash), a lista de candidatos usada é
        exatamente a persistida na solicitação original (``candidate_job_ids``
        do registro), nunca recalculada — um Job criado depois da
        solicitação original nunca entra nela; só depois que o lote estiver
        ``COMPLETED`` uma nova chamada calcula um snapshot novo, que aí sim
        inclui Jobs criados nesse meio tempo (ver docstring do módulo).

        Nunca apaga ``SourceAsset``/``Video``/``Artifact`` nem qualquer
        arquivo: apenas transiciona o ``status`` do(s) Job(s), ou registra
        uma intenção, através do mesmo caminho append-only já usado para
        qualquer outra transição de Job.
        """
        scope, scope_id = self._resolve(scope, scope_id)

        if scope in MANUALLY_ENFORCED_SCOPES:
            self._record_scope_cancel_intent(scope, scope_id, reason=reason)
            return ScopeCancelOutcome(
                scope=scope,
                scope_id=scope_id,
                status=CANCEL_OUTCOME_DEFERRED,
                cancelled_job_ids=(),
                deferred_job_ids=(),
                skipped_job_ids=(),
                errors=(),
            )

        existing_batch = self.get_cancel_batch_request(scope, scope_id)
        resuming = isinstance(existing_batch, dict) and existing_batch.get("status") == "IN_PROGRESS"

        if resuming:
            # Retomada de uma solicitação incompleta: usa o snapshot
            # ORIGINAL de candidatos, não recalcula a partir do estado atual
            # do banco (correção pós-3ª revisão — ver docstring do módulo).
            candidate_ids = list(existing_batch.get("candidate_job_ids") or [])
            self._record_cancel_batch_resumed(scope, scope_id, candidate_job_ids=candidate_ids)
        else:
            if scope == CONTROL_SCOPE_JOB:
                # cancel(JOB, x) explícito continua podendo mirar um Job já
                # terminal (ex.: PUBLISHED) — o resultado correto para esse
                # caso é NOT_APPLIED/"skipped" via ``_cancel_or_defer_job``,
                # nunca um filtro silencioso (correção pós-4ª revisão, ver
                # docstring do módulo).
                candidate_ids = [scope_id]
            else:
                jobs = self.database.list(Job)
                if scope == CONTROL_SCOPE_QUEUE:
                    jobs = [job for job in jobs if job.operation == scope_id]
                # Correção pós-4ª revisão (MAJOR): um snapshot NOVO de
                # cancelamento em massa (GLOBAL/QUEUE) não deve incluir Jobs
                # já em estado terminal (PUBLISHED/CANCELLED) — eles nunca
                # fazem sentido como alvo de "cancelar o que ainda está em
                # andamento" e só produziam "skipped"/PARTIALLY_APPLIED sem
                # nenhum problema real. Isso só se aplica ao CÁLCULO de um
                # snapshot novo: uma retomada (``resuming`` acima) sempre usa
                # o ``candidate_job_ids`` original tal como persistido, sem
                # reinterpretar essa regra.
                jobs = [job for job in jobs if job.status not in JOB_TERMINAL_STATES]
                candidate_ids = [job.id for job in jobs]

            # Bloqueador de crash-safety: a intenção precisa ser durável
            # ANTES de qualquer efeito em qualquer Job (ver docstring do
            # módulo).
            self._persist_cancel_batch_request(scope, scope_id, reason=reason, candidate_job_ids=candidate_ids)

        cancelled: list[str] = []
        deferred: list[str] = []
        skipped: list[str] = []
        errors: list[tuple[str, str]] = []

        for job_id in candidate_ids:
            try:
                outcome_kind = self._cancel_or_defer_job(job_id, reason=reason)
            except JobNotFoundForAuditError as exc:
                errors.append((job_id, str(exc)))
                continue
            if outcome_kind == "cancelled":
                cancelled.append(job_id)
            elif outcome_kind == "deferred":
                deferred.append(job_id)
            else:
                skipped.append(job_id)

        result = ScopeCancelOutcome(
            scope=scope,
            scope_id=scope_id,
            status=_compute_cancel_status(cancelled=cancelled, deferred=deferred, skipped=skipped, errors=errors),
            cancelled_job_ids=tuple(cancelled),
            deferred_job_ids=tuple(deferred),
            skipped_job_ids=tuple(skipped),
            errors=tuple(errors),
        )

        self._complete_cancel_batch_request(scope, scope_id, outcome=result)
        return result


__all__ = [
    "ControlManager",
    "ControlManagerError",
    "InvalidControlScopeError",
    "ControlScopeIdError",
    "UnsupportedBulkCancelScopeError",
    "ScopeCancelOutcome",
    "CONTROL_SCOPE_GLOBAL",
    "CONTROL_SCOPE_ACCOUNT",
    "CONTROL_SCOPE_PLATFORM",
    "CONTROL_SCOPE_QUEUE",
    "CONTROL_SCOPE_JOB",
    "CONTROL_SCOPES",
    "AUTO_ENFORCED_SCOPES",
    "MANUALLY_ENFORCED_SCOPES",
    "CANCEL_OUTCOME_FULLY_APPLIED",
    "CANCEL_OUTCOME_PARTIALLY_APPLIED",
    "CANCEL_OUTCOME_NOT_APPLIED",
    "CANCEL_OUTCOME_DEFERRED",
    "CANCEL_OUTCOME_ERROR",
    "CANCEL_OUTCOME_STATUSES",
]
