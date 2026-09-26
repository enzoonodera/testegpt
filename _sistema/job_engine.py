# -*- coding: utf-8 -*-
"""JobEngine central — motor que não perde trabalho (FASE 3 / PROMPT 12).

Este módulo define o orquestrador central de execução de ``Job``. Ele é
deliberadamente independente de apresentação e de integrações remotas:

- não importa nem conhece UI/frontend;
- não importa nem conhece YouTube, TikTok, Instagram ou qualquer outro
  Connector (esses detalhes pertencem a etapas futuras — Connector Contract
  e adaptadores específicos);
- busca e persiste Jobs exclusivamente através de ``LocalDatabase``/
  ``OperationalAuditLog`` (SQLite local).

CONTRATO DE CRASH-SAFETY / CONCORRÊNCIA
----------------------------------------
"Persistir mudança de estado antes de avançar" (PROMPT 12) é tratado aqui
como uma garantia de segurança contra crash e execução concorrente, não
apenas como "persistir antes de retornar da função". Isso significa:

1. Nenhum handler roda sobre um Job que não esteja em um estado elegível à
   execução pretendida. Antes de qualquer efeito do handler, o Engine
   *reivindica* o Job: transiciona atomicamente seu estado persistido
   (PENDING/RETRY) para um estado de trabalho ativo (``claims_status`` —
   PROCESSING, PUBLISHING ou RECOVERING) usando a mesma State Machine
   central já existente. Se o Job estiver terminal (CANCELLED/PUBLISHED)
   ou em qualquer estado que não permita essa transição, a reivindicação
   falha e o handler NUNCA é chamado.

2. Essa reivindicação usa ``OperationalAuditLog.transition_job``, que roda
   dentro de uma transaction SQLite ``BEGIN IMMEDIATE``. O SQLite serializa
   escritores: duas instâncias de ``JobEngine`` (mesmo processo ou
   processos diferentes) que tentem reivindicar o mesmo Job concorrentemente
   têm suas transactions serializadas pelo próprio banco. A primeira lê o
   estado atual sob lock e grava a transição; a segunda só prossegue depois
   do commit da primeira, lê o novo estado já alterado e tenta a mesma
   transição de novo — que a State Machine rejeita (não existe aresta de um
   estado de trabalho ativo para ele mesmo, ex.: PROCESSING -> PROCESSING).
   Portanto, no máximo uma execução do handler acontece por Job, sem
   nenhuma primitiva de lock nova além da que o SQLite já fornece.

3. Se o handler falhar (exceção) ou devolver um resultado inválido depois
   de já ter sido reivindicado, o Job NUNCA volta silenciosamente para
   PENDING/RETRY (o que o tornaria elegível a repetição cega). Em vez
   disso, o Engine registra um evento de erro auditável e pousa o Job em um
   estado seguro que depende do que ``claims_status`` representa:

   - ``PROCESSING`` é trabalho local (sem efeito remoto observável de fora
     do processo): uma exceção ali é uma falha conhecida, então o Job vai
     para ``FAILED`` — que já exige decisão explícita (RETRY ou CANCELLED)
     antes de qualquer nova tentativa.
   - ``PUBLISHING``/``RECOVERING`` podem ter iniciado, ou estar avaliando,
     um efeito potencialmente remoto. Uma exceção nesse ponto (ex.: conexão
     caiu no meio do upload, ou a própria tentativa de reconciliar um
     resultado incerto lançou erro) **não prova** que a ação remota falhou
     nem que ela não aconteceu. Tratar isso como ``FAILED`` esconderia essa
     incerteza atrás de um rótulo de "falha conhecida" e abriria caminho
     para uma republicação duplicada via ``FAILED -> RETRY``. Por isso o
     Job pousa em ``UNKNOWN``, que a State Machine central só deixa seguir
     para ``RECOVERING`` — nunca direto para RETRY/PUBLISHING. O
     ``RecoveryManager`` (PROMPT 14, ``recovery_manager.py``) garante que um
     Job abandonado nesse ponto (ex.: crash) seja encaminhado de forma
     auditável para ``RECOVERING``; a reconciliação remota real (confirmar
     com a plataforma o que de fato aconteceu) continua dependendo de um
     Connector concreto, ainda não implementado.

   Um handler que *sabe* com certeza que nenhuma ação remota ocorreu
   continua livre para declarar isso explicitamente devolvendo
   ``JobStepResult(target_status=JOB_FAILED, ...)`` em vez de lançar uma
   exceção — esse caminho não passa pelo pouso automático, é uma decisão
   deliberada do handler. O pouso automático é só o *fallback* para quando
   o handler não conseguiu (ou não tentou) declarar o resultado.

O "trabalho" específico de cada ``operation`` (processar vídeo, publicar,
etc.) continua sendo responsabilidade de um *handler* registrado
externamente pelo chamador. O JobEngine não sabe o que um handler faz
internamente.

Checkpoints granulares (PROMPT 13, ``_sistema/domain/checkpoints.py`` +
``OperationalAuditLog``) e o RecoveryManager (PROMPT 14,
``recovery_manager.py``) já estão implementados e reaproveitam
integralmente esta camada: ``RecoveryManager.recover_at_startup()`` DEVE
rodar uma vez, antes de qualquer ``run_pending()``, para encontrar Jobs
abandonados por um encerramento não controlado e encaminhá-los para um
destino seguro — ``READY``/``FAILED`` quando o trabalho era local, ou
``RECOVERING`` quando a incerteza é potencialmente remota. Essa chamada na
inicialização ainda não está integrada a nenhum bootstrap do produto (etapa
futura de orquestração); o que já existe nesta etapa é a classe e o
contrato do RecoveryManager. pause/resume/stop_after_current/cancel
(PROMPT 15, ``control_manager.py``) também já estão implementados: um
``ControlManager`` opcional (``JobEngine(..., control_manager=...)``, ``None``
por padrão — sem ele, o comportamento é idêntico ao de antes deste Prompt)
faz o Engine recusar reivindicar Jobs bloqueados nos três escopos que ele
consegue resolver sozinho a partir do próprio ``Job``: ``GLOBAL``, ``QUEUE``
(``operation``) e ``JOB`` (ver ``JobBlockedByControlError``). Os escopos
``ACCOUNT``/``PLATFORM`` são persistidos e consultáveis pelo
``ControlManager``, mas não são aplicados automaticamente aqui — o modelo
``Job`` não referencia conta/plataforma hoje; essa integração fica pendente
para quando um Connector futuro tiver essa informação (ver
``control_manager.py``). ``cancel()`` em qualquer escopo continua sendo uma
responsabilidade do ``ControlManager``, não deste módulo — a única exceção é
o ponto seguro descrito em ``advance()``: quando um cancelamento foi pedido
enquanto um Job ``PROCESSING`` estava com um handler ativo (``cancel()`` não
aplica isso na hora nesse caso — ver ``control_manager.py``), o Engine
observa essa intenção assim que o handler termina e finaliza o Job como
``CANCELLED`` em vez do resultado do handler; isso nunca acontece para
``PUBLISHING``/``RECOVERING`` (efeito remoto incerto nunca é mascarado).
Também correção pós-revisão: a checagem de bloqueio em ``_claim()`` agora
roda dentro da mesma transaction ``BEGIN IMMEDIATE`` da própria
reivindicação (não mais numa leitura solta antes dela), fechando uma janela
de corrida real entre um ``pause``/``stop_after_current`` concorrente e uma
reivindicação (ver ``_claim`` e ``control_manager.py`` para os detalhes
transacionais).

CORREÇÕES DA TERCEIRA REVISÃO ADVERSARIAL
--------------------------------------------
1. O ponto seguro pós-``PROCESSING`` (ver ``_finalize_processing_result``)
   lia ``is_job_cancel_requested`` numa leitura solta e só depois persistia
   o resultado final numa transaction separada — a mesma classe de corrida
   do pause-vs-claim, agora do outro lado do ciclo de vida. Corrigido: a
   leitura de ``cancel_requested`` e a transição final (``CANCELLED`` ou o
   resultado do handler) acontecem dentro do MESMO ``BEGIN IMMEDIATE``; a
   limpeza de ``cancel_requested`` (quando o cancelamento é aplicado) é
   atômica com essa mesma transição — nunca existe uma janela onde
   ``CANCELLED`` já está commitado mas ``cancel_requested`` continua ativo.

2. ``_claim()`` agora também recusa a reivindicação (``JobBlockedByControlError``)
   quando ``claims_status == PROCESSING`` e existe uma intenção de
   cancelamento (``cancel_requested``) ainda pendente para o Job — ex.: um
   cancelamento pedido durante uma execução anterior que terminou em
   ``FAILED`` e foi manualmente mandado para ``RETRY`` sem que o
   cancelamento chegasse a ser aplicado. Uma intenção de cancelamento local
   ainda ativa nunca permite início de um NOVO trabalho local. Isso
   deliberadamente NÃO se aplica a ``claims_status == RECOVERING``: uma
   reconciliação de resultado remoto incerto precisa poder prosseguir
   mesmo com um cancelamento pendente, porque só ela resolve a incerteza.

3. ``is_execution_blocked`` (``control_manager.py``) agora também bloqueia
   um Job que seja candidato de um ``cancel_batch_request`` ``GLOBAL``/
   ``QUEUE`` ainda ``IN_PROGRESS`` — o que fecha a lacuna de um Job que
   ficou ``PENDING`` porque um cancelamento em massa foi interrompido por
   um crash antes de chegar a ele: mesmo depois de reabrir o banco (nova
   instância de ``LocalDatabase``/``ControlManager``/``JobEngine``), esse
   Job continua não-reivindicável até alguém reinvocar ``cancel()`` para
   aquele mesmo escopo e completar (ou explicitamente resumir) a
   solicitação original. Nenhum bootstrap novo foi criado para isso — a
   proteção vem inteiramente do registro já persistido em ``settings``.

Fechamento gracioso (PROMPT 16, ``shutdown_coordinator.py``) também já está
implementado: um ``ShutdownCoordinator`` opcional (``JobEngine(...,
shutdown_coordinator=...)``, ``None`` por padrão — sem ele, o comportamento é
idêntico ao de antes deste Prompt) faz o Engine recusar reivindicar Jobs
enquanto DRAINING estiver ativo (``JobBlockedByShutdownError``), verificado
sob a MESMA transaction ``BEGIN IMMEDIATE`` de ``_claim`` (mesma técnica já
usada para ``control_manager``). O ``ShutdownCoordinator`` nunca transiciona
nenhum Job: um Job já reivindicado antes do DRAINING commitar termina
normalmente através deste mesmo módulo, exatamente como já acontecia com
``pause``/``stop_after_current`` (ver ``shutdown_coordinator.py``).

Batch Engine (PROMPT 17, ``batch_engine.py``) já está implementado como uma
camada de ORQUESTRAÇÃO sobre este módulo, nunca uma fila paralela: um
``BatchEngine`` opcional (``JobEngine(..., batch_engine=...)``, ``None`` por
padrão — sem ele, o comportamento é idêntico ao de antes deste Prompt) faz o
Engine recusar reivindicar Jobs que pertençam a um batch pausado
(``JobBlockedByBatchError``), verificado sob a MESMA transaction
``BEGIN IMMEDIATE`` de ``_claim`` (mesma técnica já usada para
``control_manager``/``shutdown_coordinator``). O ``BatchEngine`` nunca
transiciona nenhum Job diretamente: toda execução real de um Job de um
batch continua passando por ``advance``/``_claim`` exatamente como qualquer
outro Job (ver ``batch_engine.py``). ``ResourceManager`` (PROMPT 18,
``resource_manager.py``) também já está implementado, como um colaborador
opcional ainda mais desacoplado que os três acima (``JobEngine(...,
resource_manager=...)``, ``None`` por padrão): diferente de
``control_manager``/``shutdown_coordinator``/``batch_engine``, este módulo
NUNCA consulta ``resource_manager`` em ``_claim``/``advance``/
``fetch_pending_jobs`` — ele só disponibiliza o atributo para que um handler
registrado externamente adquira capacidade pesada (FFmpeg, Whisper, Ollama,
browser, renderização, análise de frames) o mais perto possível da operação
real, dentro do próprio handler, nunca aqui (ver ``resource_manager.py``
para o contrato completo). Em particular, este módulo
não decide políticas de retry automático, circuit breaker ou reconciliação
remota — apenas garante que uma falha nunca fique disfarçada de "nunca
aconteceu". A reconciliação remota real (confirmar com a plataforma o
resultado de uma publicação incerta) continua dependendo de um Connector
concreto, ainda não implementado.

ADMISSÃO DE TRABALHO PESADO (PROMPT 18, CORREÇÃO PÓS-5ª REVISÃO ADVERSARIAL)
------------------------------------------------------------------------------
BLOQUEADOR REPRODUZIDO: entre a reivindicação (``_claim``, PENDING/RETRY ->
PROCESSING/PUBLISHING/RECOVERING) e o efeito pesado real de um handler
mediado por ``ResourceManager``, existe uma janela em que o Job já está
"em execução" segundo o SQLite mas o handler só está BLOQUEADO esperando
``ResourceManager.acquire_for(...)`` conceder capacidade — nenhum efeito
real começou ainda. A política pré-existente de pause/stop_after_current/
DRAINING/batch pause ("nunca interrompe um handler já em execução, ver
``control_manager.py``") foi escrita antes do ``ResourceManager`` existir,
quando "reivindicado" e "efeito já iniciado" eram a mesma coisa — deixando
de fora exatamente esta janela: cancel/pause/DRAINING/batch pause podiam
commitar enquanto o handler só esperava capacidade, e quando a capacidade
finalmente era concedida o efeito pesado começava mesmo assim, só para o
Job terminar marcado CANCELLED/READY *depois* — o que não desfaz o efeito
que já rodou.

CORREÇÃO: ``JobEngine.admit_heavy_work(claimed)`` é um método público que
um handler DEVE chamar como a primeira linha dentro do
``with resource_manager.acquire_for(...):`` — depois que a capacidade foi
concedida, antes de tocar o recurso real. Ele reexecuta exatamente as
mesmas quatro checagens que ``_claim`` já aplica (DRAINING, batch pausado,
pause/stop_after_current, cancel_requested só para PROCESSING) contra o
estado MAIS RECENTE do SQLite. Se qualquer uma bloquear, levanta
``HeavyWorkAdmissionDeniedError`` — que se propaga através do ``with``
(liberando o lease do ``ResourceManager`` sem vazar nada, já que o handler
nunca tocou o recurso real) e é pousada por
``JobEngine._land_after_admission_denied`` em ``CANCELLED`` (cancel de
trabalho local), ``RETRY`` (pause/stop_after_current/DRAINING/batch pause
sobre trabalho local ``PROCESSING`` — retomável AUTOMATICAMENTE assim que
a autoridade for liberada, ver "CORREÇÃO PÓS-7ª REVISÃO" abaixo) ou
``BLOCKED`` (mesmos motivos, mas ``claims_status`` PUBLISHING/RECOVERING —
retomada explícita, nunca ``FAILED``/``UNKNOWN`` fictícios).

PONTO DE LINEARIZAÇÃO (CORRIGIDO NA 6ª REVISÃO ADVERSARIAL): a versão
original desta correção (5ª revisão) descrevia o ponto de admissão como "o
retorno da chamada Python a ``admit_heavy_work``" — mas as quatro checagens
eram leituras separadas, sem transaction compartilhada, então havia um
TOCTOU real entre elas (um controle podia commitar bem no meio das quatro
leituras e não ser visto por nenhuma). A implementação atual fecha essa
janela: TODAS as quatro checagens (DRAINING, batch pausado,
pause/stop_after_current, cancel_requested) acontecem dentro da MESMA
transaction ``BEGIN IMMEDIATE`` (``self.database.transaction()``), cada uma
recebendo essa conexão via ``connection=conn`` — exatamente a mesma técnica
já usada por ``_claim`` para a corrida pause-vs-claim. Como
``ControlManager.pause``/``resume``/``request_stop_after_current``/``cancel``,
``ShutdownCoordinator`` e ``BatchEngine.pause_batch``/``cancel_batch``
também escrevem sob seu próprio ``BEGIN IMMEDIATE``, o SQLite garante que
nenhum desses escritores pode intercalar um commit no meio da decisão de
``admit_heavy_work``: quem adquirir o write lock primeiro decide vendo o
estado anterior ao outro; quem adquirir depois já enxerga o commit de quem
foi primeiro. O ponto de linearização real é, portanto, "o momento em que
esta transaction adquire o ``BEGIN IMMEDIATE``", não o retorno da função
Python. Cancel/pause/DRAINING/batch pause commitado ANTES desse lock ser
adquirido => admissão negada, efeito nunca começa. Commitado DEPOIS =>
bloqueado pelo SQLite até a transaction de admissão terminar (nunca
intercalado). Nenhum lock em memória foi adicionado — a garantia é
inteiramente do SQLite, válida entre processos diferentes (ver
``tests/test_resource_manager.py``, testes ``adversarial6_bloqueador1_*``).

BLOQUEADOR 2 DA 6ª REVISÃO (cancel encalhado entre denial e landing):
mesmo com a checagem acima linearizada, havia uma segunda janela entre
``admit_heavy_work`` levantar ``HeavyWorkAdmissionDeniedError`` (por
qualquer motivo NÃO relacionado a cancel — por exemplo DRAINING) e
``_land_after_admission_denied`` abrir sua PRÓPRIA transaction para
persistir o pouso: um ``cancel(JOB, ...)`` concorrente podia commitar
exatamente nessa janela (fica ``DEFERRED``, já que o Job ainda está
PROCESSING) e o pouso, se não revalidasse nada, commitava ``BLOCKED`` do
mesmo jeito — deixando ``cancel_requested=True`` encalhado para sempre
(nenhum caminho normal de cancelamento nunca mais o resolve, porque o Job
já não está mais PROCESSING). CORREÇÃO: ``_land_after_admission_denied``
sempre relê ``control_manager.is_job_cancel_requested(connection=conn)``
dentro da SUA PRÓPRIA transaction como a palavra final antes de decidir —
se ``True``, cancel tem precedência sobre qualquer motivo de negação não
relacionado a cancel e o Job pousa ``CANCELLED`` (nunca ``BLOCKED``); caso
contrário pousa ``BLOCKED`` com o motivo original. As duas ordens de
corrida (pouso decide primeiro vs. cancel commita primeiro) convergem para
o mesmo resultado seguro, e ``BLOCKED`` com ``cancel_requested=True``
preso torna-se estruturalmente impossível (ver testes
``adversarial6_bloqueador2_*``).

CORREÇÃO PÓS-7ª REVISÃO ADVERSARIAL (BLOCKED era o destino errado para
controle TEMPORÁRIO): as duas correções acima garantiam que a decisão de
negar admissão fosse segura e linearizada, mas o DESTINO da negação ainda
estava errado para o caso comum. Pause, stop_after_current, batch pause e
DRAINING são autoridades temporárias — nenhuma delas significa "este
conteúdo tem um problema" ou "esta operação falhou". Pousar em ``BLOCKED``
exigia uma transição manual fora do produto (chamando
``OperationalAuditLog.transition_job`` diretamente) para o Job voltar a
ser elegível, porque ``BLOCKED`` não está em ``_ACTIONABLE_STATES``
(``{PENDING, RETRY}``) — então o fluxo real de produto
pause→resume/resume_batch/DRAINING→nova sessão NUNCA continuava o Job
sozinho: ``fetch_pending_jobs()``/``advance_batch()`` simplesmente
paravam de vê-lo, mesmo depois do ``resume()``. Pior: um Job que pousasse
``BLOCKED`` durante um DRAINING gracioso não é reconhecido pelo
``RecoveryManager`` como Job abandonado (``find_abandoned_jobs()`` cobre
crash real de PROCESSING/PUBLISHING/RECOVERING, não ``BLOCKED``
intencional) — na próxima inicialização o Job ficava órfão
indefinidamente.

CORREÇÃO: para ``claims_status == PROCESSING`` (trabalho local, o único
caso onde ``ResourceManager`` é usado neste Prompt), um motivo de negação
NÃO-cancel agora pousa em ``RETRY`` em vez de ``BLOCKED``. ``RETRY`` já é
``_ACTIONABLE_STATES`` — o Job volta a ser elegível automaticamente,
sem nenhuma transição manual, assim que a autoridade correspondente for
liberada:

- pause/stop_after_current (GLOBAL/QUEUE/JOB): enquanto ativo,
  ``_claim()`` continua recusando a reivindicação (comportamento
  inalterado); depois de ``resume()``, o Job RETRY volta a ser
  encontrado por ``fetch_pending_jobs()``/reivindicado normalmente;
- batch pause: enquanto ``BatchEngine.is_job_admission_blocked()`` for
  ``True``, o Job RETRY não é selecionado por ``advance_batch()``; depois
  de ``resume_batch()``, volta a ser selecionado e processado — sem API
  paralela de retry;
- DRAINING: o efeito pesado nunca começou, então ``RETRY`` é seguro
  (não é um ``claims_status`` ativo que o ``ShutdownCoordinator`` precise
  esperar drenar); enquanto DRAINING permanecer ativo, ``_claim()``
  continua bloqueando; depois de uma nova sessão/``startup_reconcile()``,
  o Job persiste como ``RETRY`` normal — nunca órfão para o
  ``RecoveryManager``, porque nunca dependeu dele.

Cancel continua com precedência absoluta sobre este novo destino: se
``cancel_requested`` estiver ``True`` (checagem da 6ª revisão, inalterada),
o Job pousa ``CANCELLED``, nunca ``RETRY``. Isto NÃO é um mecanismo de
retry automático infinito nem antecipa reconciliação remota: ``RETRY``
aqui significa exclusivamente "o trabalho ainda não começou porque uma
autoridade temporária o impediu", nunca "o handler falhou, tentar de
novo" — não há timer, não há backoff, não há contagem de tentativas
alterada por este caminho.

Para ``claims_status`` PUBLISHING/RECOVERING, o destino permanece
``BLOCKED``, deliberadamente inalterado: nenhum handler concreto deste
Prompt usa ``ResourceManager`` para trabalho remoto, e inventar um
comportamento de retry automático para reconciliação remota interrompida
sem testes reais seria enfraquecer a semântica conservadora já aprovada
de ``UNKNOWN``/reconciliação (fora de escopo — ver docstring de
``control_manager.py`` e ``_finalize_processing_result``). Ver testes
``tests/test_resource_manager.py``, casos ``adversarial7_*``.

Não existe forma de fechar a janela original (handler já bloqueado dentro
de ``acquire_for`` sem nenhuma revalidação até obter capacidade) por
completo sem um mecanismo cooperativo de interrupção do handler em
execução (fora de escopo — handlers concretos, futuro Connector); esta
correção minimiza essa janela movendo a revalidação para o ponto mais
tarde possível antes do efeito real, sem polling, sem mutex distribuído,
sem lease persistente, sem nova tabela e sem duplicar
``ControlManager``/``ShutdownCoordinator``/``BatchEngine`` como
autoridade — ``admit_heavy_work`` só LÊ as mesmas três autoridades já
existentes, agora sob uma única transaction.

Handlers que NÃO usam ``ResourceManager`` (nenhuma espera possível entre
reivindicação e efeito) não precisam chamar ``admit_heavy_work`` — o
comportamento para eles é idêntico ao de antes desta correção.
"""
from __future__ import annotations

from dataclasses import dataclass
import sqlite3
from typing import Any, Callable, Mapping, TYPE_CHECKING

from .domain import (
    Job,
    InvalidJobState,
    InvalidJobTransition,
    JOB_BLOCKED,
    JOB_CANCELLED,
    JOB_FAILED,
    JOB_PENDING,
    JOB_PROCESSING,
    JOB_PUBLISHING,
    JOB_RECOVERING,
    JOB_RETRY,
    JOB_UNKNOWN,
)
from .storage import (
    AUDIT_JOB_CANCELLED,
    JobNotFoundForAuditError,
    LocalDatabase,
    OperationalAuditError,
    OperationalAuditLog,
)
from .control_manager import ControlManager
from .circuit_breaker import CircuitBreaker
from .retry_policy import ClassifiedJobFailure, RetryPolicy

if TYPE_CHECKING:  # evita import circular em runtime: ver shutdown_coordinator.py / batch_engine.py
    from .shutdown_coordinator import ShutdownCoordinator
    from .batch_engine import BatchEngine
    from .resource_manager import ResourceManager


class JobEngineError(RuntimeError):
    """Erro de contrato do JobEngine central."""


class JobNotFoundError(JobEngineError):
    """Job solicitado não existe no SQLite local."""


class UnknownJobOperationError(JobEngineError):
    """Nenhum handler foi registrado para a ``operation`` do Job."""


class JobNotActionableError(JobEngineError):
    """O Job não está em um estado elegível para a execução pretendida.

    Cobre tanto estados terminais (``CANCELLED``/``PUBLISHED``) quanto
    qualquer outro estado do qual a State Machine não permita a transição
    para o ``claims_status`` do handler registrado — incluindo o caso de o
    Job já ter sido reivindicado por outra execução concorrente. Em nenhum
    desses casos o handler é chamado.
    """


class JobBlockedByControlError(JobNotActionableError):
    """O Job não foi reivindicado porque um escopo de controle (PROMPT 15,
    ``control_manager.py``) aplicável está pausado ou com
    ``stop_after_current`` solicitado.

    Cobre exatamente os três escopos que o ``JobEngine`` consegue resolver
    sozinho a partir do próprio ``Job``: ``GLOBAL`` (sempre verificado),
    ``QUEUE`` (``Job.operation``) e ``JOB`` (``Job.id``). Escopos
    ``ACCOUNT``/``PLATFORM`` não são verificados aqui — ver
    ``control_manager.py`` para por quê. O Job permanece exatamente no
    estado em que estava (``PENDING``/``RETRY``): nenhuma transição é
    tentada, então ele continua elegível assim que o bloqueio for removido.
    """


class JobBlockedByShutdownError(JobNotActionableError):
    """O Job não foi reivindicado porque um fechamento gracioso (PROMPT 16,
    ``shutdown_coordinator.py``) está em DRAINING.

    Mesma semântica de ``JobBlockedByControlError``: o Job permanece
    exatamente no estado em que estava (``PENDING``/``RETRY``), nenhuma
    transição é tentada, e ele volta a ser elegível assim que o DRAINING for
    resolvido (shutdown concluído seguido de um novo processo, ou
    ``ShutdownCoordinator.startup_reconcile()`` na próxima inicialização).
    """


class JobBlockedByBatchError(JobNotActionableError):
    """O Job não foi reivindicado porque pertence a um Batch pausado
    (PROMPT 17, ``batch_engine.py``).

    Mesma semântica de ``JobBlockedByControlError``/``JobBlockedByShutdownError``:
    o Job permanece exatamente no estado em que estava (``PENDING``/``RETRY``),
    nenhuma transição é tentada, e ele volta a ser elegível assim que o batch
    for retomado (``BatchEngine.resume_batch``). Um Job que não pertence a
    nenhum batch nunca é bloqueado por este caminho — ``BatchEngine`` só se
    manifesta sobre Jobs cuja membership ele mesmo persistiu
    (``batch_jobs``), e nunca altera ``Job.status`` diretamente: esta
    checagem é somente leitura (ver ``BatchEngine.is_job_admission_blocked``).
    """


class JobBlockedByCircuitBreakerError(JobNotActionableError):
    """O Job não foi reivindicado porque o Circuit Breaker (PROMPT 20,
    ``circuit_breaker.py``) do connector associado a ele está ``OPEN``.

    O connector é resolvido por CONVENÇÃO a partir de
    ``job.extra["connector_key"]`` (ver ``JobEngine._resolve_connector_key``)
    -- ``Job`` não tem ``account_id``/``platform`` hoje (ver
    ``domain/models.py``/``circuit_breaker.py`` para a investigação
    completa), então um Job sem essa chave em ``extra`` NUNCA é bloqueado
    por este caminho (não há identidade de connector para consultar) --
    exatamente a mesma limitação já documentada para os escopos
    ACCOUNT/PLATFORM em ``ControlManager``.

    Mesma semântica dos demais ``JobBlockedBy*Error``: o Job permanece
    exatamente no estado em que estava (``PENDING``/``RETRY``), nenhuma
    transição é tentada, e ele volta a ser elegível assim que o circuito
    fechar -- automaticamente após o cooldown configurado
    (``CircuitBreaker.is_admitted``), ou antes disso via
    ``CircuitBreaker.record_success`` chamado explicitamente por quem
    processa esse connector. Jobs de QUALQUER OUTRO connector (ou sem
    ``connector_key``) continuam sendo reivindicados/avançados normalmente
    -- este bloqueio nunca se propaga para além do connector aberto (ver
    ``tests/test_circuit_breaker.py``, cenário misto em ``run_pending()``).
    """


class JobBlockedByRetryScheduleError(JobNotActionableError):
    """O Job não foi reivindicado porque ``RetryPolicy`` (PROMPT 21,
    ``retry_policy.py``) agendou sua próxima tentativa automática para um
    horário futuro (``next_retry_at_epoch``) que ainda não chegou.

    Mesma semântica dos demais ``JobBlockedBy*Error``: o Job permanece
    exatamente no estado em que estava (``RETRY``), nenhuma transição é
    tentada, e ele volta a ser elegível automaticamente assim que o
    horário agendado chegar (``RetryPolicy.is_eligible_now``) -- sem
    nenhuma intervenção manual. Um Job ``RETRY`` sem agendamento (o
    caminho manual pré-existente, ``BatchEngine.retry_failed()``, ou
    depois de ``RetryPolicy.reset()``) NUNCA é bloqueado por este caminho
    -- continua imediatamente elegível, exatamente como sempre funcionou.
    """


class HeavyWorkAdmissionDeniedError(JobEngineError):
    """Um handler chamou ``JobEngine.admit_heavy_work(claimed)`` (PROMPT 18,
    correção pós-5ª revisão adversarial — ver "ADMISSÃO DE TRABALHO PESADO"
    na docstring do módulo) imediatamente antes de iniciar o efeito pesado
    real, e a revalidação encontrou uma autoridade de controle já commitada
    que teria impedido a reivindicação original se estivesse ativa a tempo:
    cancel, pause/stop_after_current, DRAINING ou batch pausado.

    Isso só pode acontecer quando o Job ficou PROCESSING/PUBLISHING/
    RECOVERING mas o efeito pesado real ainda não tinha começado — tipicamente
    porque o handler estava bloqueado em ``ResourceManager.acquire_for``
    esperando capacidade. O handler NUNCA deve ter tocado o recurso real
    quando levanta isto (ver contrato de ``admit_heavy_work``).

    ``reason`` é um de ``"CANCEL_REQUESTED"``, ``"CONTROL_BLOCKED"``
    (pause/stop_after_current em qualquer escopo que o Engine resolve
    sozinho), ``"DRAINING"``, ``"BATCH_PAUSED"`` ou ``"CIRCUIT_BREAKER_OPEN"``
    (PROMPT 20 — connector com circuito aberto, só quando
    ``circuit_breaker`` foi fornecido e o Job carrega
    ``extra["connector_key"]``) — usado por
    ``JobEngine._land_after_admission_denied`` para decidir o pouso correto.
    ``job_id``/``detail`` existem só para diagnóstico/auditoria.
    """

    def __init__(self, job_id: str, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.job_id = job_id
        self.reason = reason
        self.detail = detail


class JobHandlerError(JobEngineError):
    """A execução ou persistência do resultado de um handler falhou.

    Isso cobre tanto uma exceção levantada pelo handler quanto um resultado
    inválido (tipo errado ou transição final não permitida). Em todos os
    casos o Job já foi reivindicado (seu estado mudou de PENDING/RETRY para
    um estado de trabalho ativo) antes da falha, então o Engine pousa o Job
    em um estado seguro e auditável em vez de deixá-lo como se o handler
    nunca tivesse rodado: ``FAILED`` quando o trabalho reivindicado era
    local (``PROCESSING``), ou ``UNKNOWN`` quando podia envolver um efeito
    remoto ainda incerto (``PUBLISHING``/``RECOVERING`` — ver
    ``_SAFE_FALLBACK_STATUS``).
    """


@dataclass(frozen=True)
class JobStepResult:
    """Resultado declarado por um handler de ``operation`` após executar um passo.

    O handler nunca escreve diretamente no storage: ele apenas descreve o
    resultado e o JobEngine persiste a transição atomicamente.

    ``target_status`` é validado pela State Machine central de ``Job``
    (via ``OperationalAuditLog.transition_job``) antes de qualquer escrita.
    ``semantic_event`` é opcional e deve ser um dos tipos de evento
    operacionais já definidos em ``_sistema.storage`` (ex.: ``AUDIT_JOB_PROCESSING_COMPLETED``).
    """

    target_status: str
    semantic_event: str | None = None
    data: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class JobRunOutcome:
    """Resultado isolado de uma tentativa de avanço dentro de ``run_pending``.

    Uma falha em um Job nunca deve impedir que os demais sejam processados.
    ``error`` preserva a exceção original (sempre uma ``JobEngineError``, já
    que ``advance`` traduz qualquer falha interna para esse vocabulário) para
    diagnóstico do chamador, sem interromper o laço de ``run_pending``.
    """

    job_id: str
    job: Job | None
    error: JobEngineError | None

    @property
    def ok(self) -> bool:
        return self.error is None


# Handler de operation: recebe o Job já reivindicado (persistido em
# ``claims_status``) e devolve o resultado do passo. Não recebe o JobEngine
# nem o storage: um handler não pode decidir sozinho o que persistir, apenas
# o que deveria acontecer a seguir.
JobHandler = Callable[[Job], JobStepResult]

# Pouso seguro para quando algo falha DEPOIS da reivindicação e o Engine não
# pode presumir se o efeito do handler (local ou potencialmente remoto)
# realmente ocorreu:
#
# - PROCESSING é trabalho local, sem efeito observável fora do processo.
#   Uma exceção ali é uma falha conhecida: FAILED.
# - PUBLISHING pode já ter iniciado um efeito remoto (ex.: upload em voo
#   quando a conexão cai) e RECOVERING pode estar no meio de uma tentativa
#   de reconciliar um resultado remoto incerto. Em nenhum dos dois uma
#   exceção prova que a ação remota não aconteceu (ou não vai confirmar
#   depois). Tratar isso como FAILED apagaria essa incerteza e abriria
#   caminho para repostagem duplicada via FAILED -> RETRY. O pouso correto
#   é UNKNOWN, que a State Machine central só deixa avançar para
#   RECOVERING — nunca direto para RETRY/PUBLISHING.
#
# Todos os estados aqui aceitam seu próprio fallback como destino válido na
# State Machine central (ver job_state_machine.py), o que garante que o
# pouso automático sempre tenha onde acontecer.
_SAFE_FALLBACK_STATUS: dict[str, str] = {
    JOB_PROCESSING: JOB_FAILED,
    JOB_PUBLISHING: JOB_UNKNOWN,
    JOB_RECOVERING: JOB_UNKNOWN,
}

# Estados de trabalho ativo que um handler pode reivindicar: exatamente os
# estados para os quais existe uma política de pouso seguro definida acima.
CLAIMABLE_STATES = frozenset(_SAFE_FALLBACK_STATUS)

# Jobs nesses estados ainda têm trabalho pendente de execução pelo Engine.
# RETRY é incluído porque representa um Job que já sabe que precisa ser
# re-executado; PENDING é o estado inicial de todo Job novo. Um Job que
# falhou (FAILED) deliberadamente NÃO está aqui: sair de FAILED exige uma
# decisão explícita (ex.: ``OperationalAuditLog.retry``), nunca um novo
# ``advance`` automático.
_ACTIONABLE_STATES = frozenset({JOB_PENDING, JOB_RETRY})


class JobEngine:
    """Motor central de execução de Jobs, independente de UI e de plataformas.

    O JobEngine é a única peça que decide *quando* uma transição de estado é
    persistida. Ele nunca mantém progresso importante somente em memória:
    toda leitura de Job vem do SQLite e toda transição — inclusive a
    reivindicação que precede o handler — é escrita nele antes de qualquer
    efeito ser considerado iniciado ou concluído.
    """

    def __init__(
        self,
        database: LocalDatabase,
        *,
        audit_log: OperationalAuditLog | None = None,
        control_manager: ControlManager | None = None,
        shutdown_coordinator: "ShutdownCoordinator | None" = None,
        batch_engine: "BatchEngine | None" = None,
        resource_manager: "ResourceManager | None" = None,
        circuit_breaker: CircuitBreaker | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self.database = database
        self.audit_log = audit_log if audit_log is not None else OperationalAuditLog(database)
        # Opcional e independente (PROMPT 15): quando omitido (padrão), o
        # comportamento é idêntico ao de antes deste Prompt — nenhum Job é
        # bloqueado por pause/stop_after_current. Ver control_manager.py.
        self.control_manager = control_manager
        # Opcional e independente (PROMPT 16): quando omitido (padrão), o
        # comportamento é idêntico ao de antes deste Prompt — nenhum Job é
        # bloqueado por DRAINING. Ver shutdown_coordinator.py.
        self.shutdown_coordinator = shutdown_coordinator
        # Opcional e independente (PROMPT 17): quando omitido (padrão), o
        # comportamento é idêntico ao de antes deste Prompt — nenhum Job é
        # bloqueado por batch pausado. Ver batch_engine.py. Atributo público
        # simples (como os dois acima): BatchEngine pode se anexar a um
        # JobEngine já existente atribuindo ``job_engine.batch_engine = self``
        # depois da construção, sem precisar recriar o JobEngine.
        self.batch_engine = batch_engine
        # Opcional e independente (PROMPT 18): quando omitido (padrão), o
        # comportamento é idêntico ao de antes deste Prompt. Diferente dos
        # três colaboradores acima, o JobEngine NUNCA consulta
        # ``resource_manager`` em ``_claim``/``advance``/
        # ``fetch_pending_jobs`` — ``ResourceManager`` não é uma autoridade
        # de admissão de Job, só um limitador de concorrência de operações
        # pesadas (ver ``resource_manager.py``, "NÃO RESERVAR RECURSO ANTES
        # DA HORA" / "INTEGRAÇÃO COM JOBENGINE"). Este atributo só existe
        # para que um handler registrado externamente possa acessá-lo (via
        # closure sobre o JobEngine/ResourceManager no momento do registro)
        # e adquirir capacidade o mais perto possível da operação pesada
        # real — nunca aqui.
        self.resource_manager = resource_manager
        # Opcional e independente (PROMPT 20): quando omitido (padrão), o
        # comportamento é idêntico ao de antes deste Prompt — nenhum Job é
        # bloqueado por circuito aberto. Ver circuit_breaker.py. Diferente
        # de control_manager/shutdown_coordinator/batch_engine (que operam
        # sobre GLOBAL/QUEUE/JOB, sempre resolvíveis a partir do próprio
        # Job), o connector aqui só é conhecido quando
        # ``job.extra["connector_key"]`` foi preenchido por quem criou o
        # Job (ver ``_resolve_connector_key``) — um Job sem essa chave nunca
        # é bloqueado por este caminho, mesmo com ``circuit_breaker``
        # fornecido.
        self.circuit_breaker = circuit_breaker
        # Opcional e independente (PROMPT 21): quando omitido (padrão), o
        # comportamento é idêntico ao de antes deste Prompt — toda falha de
        # handler cai no pouso cego de sempre (``_SAFE_FALLBACK_STATUS``),
        # sem retry automático. Ver retry_policy.py. Autoridade ORTOGONAL
        # ao ``circuit_breaker`` acima (decide o destino de UM Job, nunca a
        # admissão de um connector inteiro) — nunca chama
        # ``circuit_breaker.is_admitted``/``record_failure`` nem é chamado
        # por ele.
        self.retry_policy = retry_policy
        self._handlers: dict[str, tuple[JobHandler, str]] = {}

    @staticmethod
    def _resolve_connector_key(job: Job) -> str | None:
        """Resolve a identidade do connector (PROMPT 20, ``circuit_breaker.py``)
        a partir de um Job já existente.

        ``Job`` (``domain/models.py``) não referencia ``account_id`` nem
        ``platform`` hoje, e não existe FK alguma de ``jobs`` para
        ``publications``/``accounts`` no schema SQLite (ver investigação
        documentada em ``circuit_breaker.py`` e no relatório de entrega
        deste Prompt) — não há como derivar isso automaticamente a partir
        de um Job, então este método NUNCA tenta adivinhar. O valor é lido
        por CONVENÇÃO de ``job.extra["connector_key"]`` quando presente — o
        mesmo espaço genérico que ``Entity.extra``/``extra_json`` já oferece
        para metadados soltos, sem exigir migration nova em ``jobs``. Um Job
        sem essa chave (ou com um valor que não seja uma string não vazia)
        devolve ``None`` e nunca é bloqueado pelo Circuit Breaker — mesma
        semântica já aprovada para os escopos ACCOUNT/PLATFORM em
        ``ControlManager``, que também ficam pendentes de um Connector
        futuro concreto (Prompts 57-60, fora de escopo aqui)."""
        extra = job.extra
        if not isinstance(extra, dict):
            return None
        value = extra.get("connector_key")
        if not isinstance(value, str):
            return None
        value = value.strip()
        return value or None

    def _is_blocked_by_circuit_breaker(
        self, job: Job, *, connection: sqlite3.Connection | None = None
    ) -> bool:
        assert self.circuit_breaker is not None
        connector_key = self._resolve_connector_key(job)
        if connector_key is None:
            return False
        return not self.circuit_breaker.is_admitted(connector_key, connection=connection)

    def _is_blocked_by_retry_schedule(
        self, job: Job, *, connection: sqlite3.Connection | None = None
    ) -> bool:
        """PROMPT 21, item 1.5: um Job com ``next_retry_at_epoch`` agendado
        no futuro (via ``RetryPolicy.record_failure``) não pode ser
        reivindicado antes desse horário. Aplicado incondicionalmente a
        todo Job candidato (não só ``RETRY``) — um Job sem linha em
        ``job_retry_state`` (nunca falhou, ou veio do caminho manual
        ``retry_failed()``/``RetryPolicy.reset()``) é sempre elegível, então
        isto nunca regride nenhum Job ``PENDING``/``RETRY`` pré-existente."""
        assert self.retry_policy is not None
        return not self.retry_policy.is_eligible_now(job.id, connection=connection)

    def register_handler(
        self,
        operation: str,
        handler: JobHandler,
        *,
        claims_status: str,
    ) -> None:
        """Registra o handler responsável por executar uma ``operation``.

        ``claims_status`` é o estado de trabalho ativo que o Job assume
        atomicamente antes de o handler ser chamado (deve ser um dos
        ``CLAIMABLE_STATES``). O JobEngine não valida o que o handler faz
        internamente; ele apenas precisa devolver um ``JobStepResult`` para
        que o Engine persista o avanço final.
        """
        operation = str(operation).strip()
        if not operation:
            raise ValueError("operation não pode ser vazia")
        if not callable(handler):
            raise TypeError("handler deve ser chamável")
        if claims_status not in CLAIMABLE_STATES:
            raise ValueError(
                f"claims_status deve ser um de {sorted(CLAIMABLE_STATES)}; recebido {claims_status!r}"
            )
        self._handlers[operation] = (handler, claims_status)

    def unregister_handler(self, operation: str) -> None:
        """Remove o handler registrado para ``operation``, se existir."""
        self._handlers.pop(str(operation).strip(), None)

    def has_handler(self, operation: str) -> bool:
        return str(operation).strip() in self._handlers

    def get_job(self, job_id: str) -> Job | None:
        """Busca um Job diretamente no SQLite local pelo id."""
        return self.database.get(Job, job_id)

    def fetch_pending_jobs(self, *, limit: int | None = None) -> list[Job]:
        """Busca no SQLite os Jobs com trabalho pendente (``PENDING``/``RETRY``).

        A ordenação segue ``created_at, id`` de ``LocalDatabase.list`` (ordem
        aproximada de criação; ``created_at`` tem resolução de segundos, então
        Jobs criados no mesmo segundo não têm ordem relativa garantida). Este
        método sempre lê do SQLite: nenhuma fila é mantida apenas em memória
        pelo Engine.

        Quando um ``control_manager`` foi fornecido (PROMPT 15), Jobs
        bloqueados por pause/stop_after_current em ``GLOBAL``, na sua
        ``QUEUE`` (``operation``) ou nele mesmo (``JOB``) são omitidos daqui
        — eles continuam ``PENDING``/``RETRY`` no SQLite, só não são
        oferecidos para execução enquanto o bloqueio estiver ativo. Isso
        inclui (correção pós-3ª revisão) Jobs candidatos de um cancelamento
        em massa ``GLOBAL``/``QUEUE`` ainda ``IN_PROGRESS`` (ver
        ``control_manager.is_execution_blocked``) e Jobs com
        ``cancel_requested`` pendente cujo handler registrado reivindicaria
        com ``claims_status == PROCESSING`` (nunca para ``RECOVERING`` — ver
        ``_claim``). Sem ``control_manager`` (padrão), o comportamento é
        exatamente o de antes do PROMPT 15.

        Esta lista é um snapshot de leitura: a decisão realmente autoritativa
        (a única que precisa estar livre de corrida) é sempre a de
        ``_claim()``, que reavalia tudo sob o mesmo ``BEGIN IMMEDIATE`` da
        própria reivindicação.
        """
        if limit is not None and limit < 0:
            raise ValueError("limit deve ser >= 0")
        candidates = [job for job in self.database.list(Job) if job.status in _ACTIONABLE_STATES]
        if self.shutdown_coordinator is not None and self.shutdown_coordinator.is_draining():
            return []
        if self.control_manager is not None:
            candidates = [job for job in candidates if not self._is_blocked_for_claim(job)]
        if self.batch_engine is not None:
            candidates = [
                job for job in candidates if not self.batch_engine.is_job_admission_blocked(job.id)
            ]
        if self.circuit_breaker is not None:
            candidates = [
                job for job in candidates if not self._is_blocked_by_circuit_breaker(job)
            ]
        if self.retry_policy is not None:
            candidates = [
                job for job in candidates if not self._is_blocked_by_retry_schedule(job)
            ]
        if limit is not None:
            candidates = candidates[:limit]
        return candidates

    def _is_blocked_for_claim(self, job: Job) -> bool:
        """Pré-filtro de leitura usado por ``fetch_pending_jobs`` — reflete
        (sem a garantia transacional, que só ``_claim`` precisa oferecer) as
        mesmas duas checagens que ``_claim`` aplicaria para este Job."""
        assert self.control_manager is not None
        if self.control_manager.is_execution_blocked(operation=job.operation, job_id=job.id):
            return True
        handler_entry = self._handlers.get(job.operation)
        claims_status = handler_entry[1] if handler_entry is not None else None
        if claims_status == JOB_PROCESSING and self.control_manager.is_job_cancel_requested(job.id):
            return True
        return False

    def admit_heavy_work(self, job: Job) -> None:
        """Revalida, IMEDIATAMENTE ANTES do início real de um efeito pesado
        mediado por ``ResourceManager``, que nenhuma autoridade de controle
        já bloqueou este Job enquanto ele esperava capacidade (PROMPT 18,
        correção pós-5ª revisão adversarial — ver "ADMISSÃO DE TRABALHO
        PESADO" na docstring do módulo).

        CONTRATO: um handler que faz ``with resource_manager.acquire_for(...):``
        para uma operação pesada DEVE chamar
        ``job_engine.admit_heavy_work(claimed)`` como a PRIMEIRA coisa dentro
        desse ``with`` — antes de tocar o recurso real (iniciar o
        subprocesso, abrir o navegador, etc.) — passando o próprio ``claimed``
        recebido como argumento do handler. Se a admissão for negada
        (``HeavyWorkAdmissionDeniedError``), a exceção se propaga através do
        ``with`` (que libera o lease do ``ResourceManager`` via ``__exit__``,
        sem nenhum permit vazado) e ``advance()`` pousa o Job em um estado
        seguro e RETOMÁVEL — nunca ``FAILED`` fictício, porque nenhum efeito
        (local ou remoto) chegou a ser tentado.

        PONTO DE LINEARIZAÇÃO (CORREÇÃO PÓS-6ª REVISÃO ADVERSARIAL): o
        retorno da função Python NÃO é a fronteira — quatro leituras
        separadas (cada uma abrindo sua própria conexão de leitura) deixam
        um TOCTOU real entre elas, onde um ``pause``/``cancel``/DRAINING
        concorrente podia commitar exatamente entre duas checagens e ainda
        assim a admissão prosseguir. O ponto de linearização real é a
        decisão tomada dentro de uma ÚNICA transaction SQLite
        ``BEGIN IMMEDIATE`` (mesma técnica de ``_claim`` — ver
        ``control_manager.py``, "corrida pause-vs-claim"): as quatro
        checagens abaixo leem sob a MESMA ``connection``, e
        ``BEGIN IMMEDIATE`` adquire o write lock do SQLite de forma
        exclusiva e imediata, então nenhum ``pause``/``resume``/
        ``request_stop_after_current``/``cancel``/``shutdown``/
        ``pause_batch`` concorrente (todos gravam sob seu próprio
        ``BEGIN IMMEDIATE``) pode intercalar um commit no meio desta
        decisão — quem adquirir o lock primeiro decide a ordem:

        - se um commit de controle vence a corrida pelo lock primeiro,
          esta transaction só começa depois e enxerga esse commit → nega a
          admissão;
        - se esta transaction vence o lock primeiro, ela decide com uma
          visão serializada e consistente das quatro autoridades, e
          qualquer commit de controle concorrente fica bloqueado até esta
          transaction terminar (commit vazio, já que não escrevemos nada)
          → a admissão já venceu a corrida, e só then o controle prossegue.

        Se a admissão for negada, a exceção se propaga através do
        ``with resource_manager.acquire_for(...):`` do handler (que libera
        o lease via ``__exit__``, sem nenhum permit vazado) e ``advance()``
        pousa o Job em um estado seguro e RETOMÁVEL — nunca ``FAILED``
        fictício, porque nenhum efeito (local ou remoto) chegou a ser
        tentado. Se a admissão vencer, o trabalho é considerado iniciado a
        partir desse commit e as regras já aprovadas para operação em
        andamento se aplicam inalteradas (cancel observado só depois que o
        handler termina — ver ``_finalize_processing_result`` — e
        pause/DRAINING nunca interrompem um handler já em execução — ver
        ``control_manager.py``/``shutdown_coordinator.py``). Não há como
        fechar a janela ENTRE esta chamada retornar e o handler realmente
        tocar o recurso físico (ex.: ``subprocess.run``) sem um mecanismo
        cooperativo de interrupção do handler síncrono (fora de escopo
        deste Prompt); esta chamada garante que a DECISÃO em si é atômica
        e correta, minimizando a janela restante ao mínimo inevitável.

        Reutiliza exatamente as mesmas quatro checagens (mesma ordem e
        mesma semântica) que ``_claim`` já aplica antes de reivindicar um
        Job — nenhuma autoridade nova é criada, nenhum estado de
        controle/shutdown/batch é copiado para uma segunda fonte:

        1. ``shutdown_coordinator.is_draining(connection=conn)`` — DRAINING.
        2. ``batch_engine.is_job_admission_blocked(job.id, connection=conn)``
           — batch pausado OU cancel_batch IN_PROGRESS.
        3. ``control_manager.is_execution_blocked(operation=..., job_id=...,
           connection=conn)`` — pause OU stop_after_current em
           GLOBAL/QUEUE/JOB (ver "STOP_AFTER_CURRENT" abaixo).
        4. ``control_manager.is_job_cancel_requested(job.id, connection=conn)``
           — só quando ``job.status == PROCESSING`` (mesma restrição de
           ``_claim``: nunca para PUBLISHING/RECOVERING, onde um
           cancelamento pendente continua sendo apenas uma intenção
           consultável, nunca mascarando um efeito remoto incerto).

        STOP_AFTER_CURRENT (decisão explícita, não escolhida em silêncio):
        um Job que só está esperando capacidade do ``ResourceManager`` NÃO
        conta como "current" para fins de ``stop_after_current`` — ele ainda
        não iniciou nenhum efeito, então é tratado exatamente como pause:
        bloqueado aqui, nunca deixado terminar. Isso é uma extensão
        deliberada e consistente da política já documentada em
        ``control_manager.py`` ("PAUSE ESPERA UM PONTO SEGURO QUANDO
        POSSÍVEL" — escrita antes do ResourceManager existir, quando
        "reivindicado" e "efeito já iniciado" eram a mesma coisa). Um Job
        cujo handler já passou deste ponto (já admitido) continua sendo
        tratado como "current" e termina normalmente, como sempre.

        Não faz sentido chamar isto fora de um handler com um ``claimed``
        real (não valida nada sozinho: sempre lê o estado mais recente do
        SQLite/controle a cada chamada, sob uma transaction nova a cada
        vez — nunca cacheia)."""
        job_id = job.id
        with self.database.transaction() as conn:
            if self.shutdown_coordinator is not None and self.shutdown_coordinator.is_draining(
                connection=conn
            ):
                raise HeavyWorkAdmissionDeniedError(
                    job_id, "DRAINING", f"Job {job_id}: fechamento gracioso (DRAINING) em andamento"
                )
            if self.batch_engine is not None and self.batch_engine.is_job_admission_blocked(
                job_id, connection=conn
            ):
                raise HeavyWorkAdmissionDeniedError(
                    job_id, "BATCH_PAUSED", f"Job {job_id}: pertence a um Batch pausado"
                )
            if self.circuit_breaker is not None:
                connector_key = self._resolve_connector_key(job)
                if connector_key is not None and not self.circuit_breaker.is_admitted(
                    connector_key, connection=conn
                ):
                    raise HeavyWorkAdmissionDeniedError(
                        job_id,
                        "CIRCUIT_BREAKER_OPEN",
                        f"Job {job_id}: circuito aberto (CircuitBreaker) para o connector "
                        f"{connector_key!r}",
                    )
            if self.control_manager is not None:
                if self.control_manager.is_execution_blocked(
                    operation=job.operation, job_id=job_id, connection=conn
                ):
                    raise HeavyWorkAdmissionDeniedError(
                        job_id,
                        "CONTROL_BLOCKED",
                        f"Job {job_id}: pause/stop_after_current ativo em GLOBAL, na fila "
                        f"(operation={job.operation!r}) ou neste Job",
                    )
                if job.status == JOB_PROCESSING and self.control_manager.is_job_cancel_requested(
                    job_id, connection=conn
                ):
                    raise HeavyWorkAdmissionDeniedError(
                        job_id,
                        "CANCEL_REQUESTED",
                        f"Job {job_id}: cancelamento pendente para trabalho local ainda não iniciado",
                    )

    def _claim(
        self,
        job_id: str,
        claims_status: str,
        operation: str,
        *,
        connector_key: str | None = None,
    ) -> Job:
        """Reivindica atomicamente um Job antes de qualquer efeito do handler.

        Usa a mesma transaction ``BEGIN IMMEDIATE`` de
        ``OperationalAuditLog.transition_job``: o SQLite serializa
        reivindicações concorrentes do mesmo Job, e a State Machine central
        rejeita a transição quando o estado atual (terminal, já reivindicado
        por outra execução, ou qualquer outro incompatível) não permite
        avançar para ``claims_status``. Nesses casos o handler nunca chega a
        ser chamado.

        Também recusa a reivindicação (``JobBlockedByControlError``, sem
        sequer tentar a transição) quando um ``control_manager`` (PROMPT 15)
        foi fornecido e algum escopo que o Engine consegue resolver sozinho
        — ``GLOBAL``, ``QUEUE``/``operation`` ou este ``JOB`` — está
        pausado ou com ``stop_after_current`` solicitado. Isso protege tanto
        chamadas via ``run_pending()``/``fetch_pending_jobs()`` quanto uma
        chamada direta a ``advance(job_id)`` que contorne o filtro de
        ``fetch_pending_jobs``.

        Da mesma forma, recusa (``JobBlockedByCircuitBreakerError``) quando
        um ``circuit_breaker`` (PROMPT 20) foi fornecido, ``connector_key``
        foi resolvido para este Job (``JobEngine._resolve_connector_key`` —
        ``None`` quando o Job não carrega ``extra["connector_key"]``, caso
        em que este bloqueio nunca se aplica) e o circuito desse connector
        está ``OPEN``.

        CORREÇÃO PÓS-REVISÃO (corrida pause-vs-claim): a checagem de
        controle e a tentativa de transição agora acontecem dentro da MESMA
        transaction ``BEGIN IMMEDIATE`` (``self.database.transaction()``),
        em vez de a checagem ler o flag numa conexão solta e só depois abrir
        uma transaction separada para a transição. Antes dessa correção
        havia uma janela real: um ``pause``/``stop_after_current`` podia
        commitar exatamente nessa janela e a reivindicação prosseguia mesmo
        assim. Como ``BEGIN IMMEDIATE`` adquire o write lock do SQLite de
        forma exclusiva e imediata, e ``ControlManager.pause``/``resume``/
        ``request_stop_after_current`` também gravam sob seu próprio
        ``BEGIN IMMEDIATE``, as duas operações nunca mais podem ser
        intercaladas por um terceiro escritor: quem adquirir o lock primeiro
        conclui vendo o estado anterior ao outro; quem adquirir depois já
        enxerga o commit de quem foi primeiro. Nenhum lock em memória foi
        adicionado — a garantia é inteiramente do SQLite, válida entre
        processos diferentes (ver ``control_manager.py`` e
        ``tests/test_control_manager.py``, testes de corrida).

        CORREÇÃO PÓS-3ª REVISÃO (precedência de cancel_requested sobre novo
        trabalho local): quando ``claims_status == PROCESSING`` e existe uma
        intenção de cancelamento (``cancel_requested``) ainda pendente para
        este Job — por exemplo, um cancelamento pedido durante uma execução
        anterior que terminou em ``FAILED`` e foi mandado para ``RETRY``
        manualmente sem que o cancelamento chegasse a ser aplicado — a
        reivindicação também é recusada, verificado sob o mesmo
        ``BEGIN IMMEDIATE``. Deliberadamente não se aplica a
        ``claims_status == RECOVERING``/``PUBLISHING``: uma reconciliação de
        resultado remoto incerto precisa poder prosseguir mesmo com um
        cancelamento pendente, porque só ela resolve a incerteza (ver
        docstring do módulo e de ``control_manager.py``).
        """
        with self.database.transaction() as conn:
            if self.shutdown_coordinator is not None and self.shutdown_coordinator.is_draining(connection=conn):
                raise JobBlockedByShutdownError(
                    f"Job {job_id} não reivindicado: fechamento gracioso (DRAINING) em andamento"
                )
            if self.batch_engine is not None and self.batch_engine.is_job_admission_blocked(
                job_id, connection=conn
            ):
                raise JobBlockedByBatchError(
                    f"Job {job_id} não reivindicado: pertence a um Batch pausado (BatchEngine)"
                )
            if (
                self.circuit_breaker is not None
                and connector_key is not None
                and not self.circuit_breaker.is_admitted(connector_key, connection=conn)
            ):
                raise JobBlockedByCircuitBreakerError(
                    f"Job {job_id} não reivindicado: circuito aberto (CircuitBreaker) para o "
                    f"connector {connector_key!r}"
                )
            if self.retry_policy is not None and not self.retry_policy.is_eligible_now(
                job_id, connection=conn
            ):
                raise JobBlockedByRetryScheduleError(
                    f"Job {job_id} não reivindicado: agendado para uma tentativa automática "
                    "futura (RetryPolicy, PROMPT 21) que ainda não chegou"
                )
            if self.control_manager is not None:
                if self.control_manager.is_execution_blocked(
                    operation=operation, job_id=job_id, connection=conn
                ):
                    raise JobBlockedByControlError(
                        f"Job {job_id} não reivindicado: pause/stop_after_current ativo em "
                        f"GLOBAL, na fila (operation={operation!r}) ou neste Job"
                    )
                if claims_status == JOB_PROCESSING and self.control_manager.is_job_cancel_requested(
                    job_id, connection=conn
                ):
                    raise JobBlockedByControlError(
                        f"Job {job_id} não reivindicado: cancelamento pendente (cancel_requested) "
                        "para trabalho local ainda ativo"
                    )
            try:
                return self.audit_log.transition_job(
                    job_id,
                    claims_status,
                    data={"claimed_operation": operation},
                    connection=conn,
                )
            except JobNotFoundForAuditError as exc:
                raise JobNotFoundError(f"Job não encontrado no SQLite local: {job_id}") from exc
            except (InvalidJobState, InvalidJobTransition) as exc:
                raise JobNotActionableError(
                    f"Job {job_id} não está em estado elegível para operation={operation!r} "
                    f"(transição para {claims_status} não permitida no estado atual, ou o Job já "
                    "foi reivindicado por outra execução concorrente)"
                ) from exc

    def _land_safely_after_claim_failure(
        self,
        job_id: str,
        claims_status: str,
        error: BaseException,
    ) -> None:
        """Pousa o Job em um estado seguro e auditável após já reivindicado e
        ter falhado (exceção do handler, resultado inválido, ou transição
        final não permitida).

        Nunca deixa o Job voltar silenciosamente para PENDING/RETRY: isso o
        tornaria elegível a uma repetição cega do mesmo efeito. O destino
        depende de ``claims_status`` (ver ``_SAFE_FALLBACK_STATUS``): FAILED
        para trabalho local (PROCESSING), UNKNOWN para trabalho que podia
        envolver um efeito remoto ainda incerto (PUBLISHING/RECOVERING) — uma
        exceção nunca é tratada como prova de que a ação remota não
        aconteceu. A falha em registrar o erro ou aplicar a transição (ex.:
        corrida rara em que o Job mudou de estado por outro caminho) é
        isolada aqui e nunca mascara a exceção original que já está sendo
        propagada pelo chamador.
        """
        target = _SAFE_FALLBACK_STATUS[claims_status]
        try:
            self.audit_log.record_error(
                job_id,
                code="JOB_ADVANCE_FAILED",
                message=str(error),
                recoverable=(target != JOB_FAILED),
                data={"claims_status": claims_status, "landed_status": target},
            )
        except OperationalAuditError:
            pass
        try:
            self.audit_log.transition_job(
                job_id,
                target,
                data={"reason": "advance_failed_after_claim", "claims_status": claims_status},
            )
        except (InvalidJobState, InvalidJobTransition, JobNotFoundForAuditError):
            pass

    def advance(self, job_id: str) -> Job:
        """Executa um único passo do Job e persiste o resultado antes de retornar.

        Fluxo:
        1. recarrega o Job do SQLite (a cópia em memória de quem chamou é
           ignorada: só o estado persistido é confiável);
        2. resolve o handler registrado para ``job.operation``;
        3. reivindica o Job atomicamente para ``claims_status`` — se o Job
           não estiver elegível (terminal ou já reivindicado por outra
           execução), levanta ``JobNotActionableError`` e o handler NUNCA é
           chamado;
        4. executa o handler sobre o Job já reivindicado;
        5. persiste a transição final e o audit trail atomicamente através
           de ``OperationalAuditLog`` antes de devolver o Job atualizado.

        Se o handler falhar ou devolver um resultado inválido depois do
        passo 3, o Job nunca volta para PENDING/RETRY sozinho: ele pousa em
        ``FAILED`` (trabalho local, ``claims_status=PROCESSING``) ou em
        ``UNKNOWN`` (trabalho que podia ter efeito remoto ainda incerto,
        ``claims_status`` PUBLISHING/RECOVERING — ver ``_SAFE_FALLBACK_STATUS``),
        sempre de forma auditável, e ``JobHandlerError`` é levantada com a
        causa original encadeada.

        CANCELAMENTO ADIADO (PROMPT 15, correção pós-revisão): se um
        ``control_manager`` foi fornecido, ``claims_status == PROCESSING``
        (trabalho local, sem efeito remoto) e um cancelamento foi pedido
        enquanto o handler estava rodando (``ControlManager.cancel`` não
        aplica isso na hora para Jobs em execução ativa — ver
        ``control_manager.py``), este é o ponto seguro em que essa intenção
        é observada: o handler já terminou (com sucesso) e o resultado dele
        é substituído por ``CANCELLED`` em vez de persistido. Isso nunca
        acontece para ``PUBLISHING``/``RECOVERING`` — um efeito remoto
        incerto ou uma reconciliação em andamento jamais é mascarado por um
        cancelamento pedido nesse meio tempo.
        """
        job = self.get_job(job_id)
        if job is None:
            raise JobNotFoundError(f"Job não encontrado no SQLite local: {job_id}")

        handler_entry = self._handlers.get(job.operation)
        if handler_entry is None:
            raise UnknownJobOperationError(
                f"nenhum handler registrado para operation={job.operation!r}"
            )
        handler, claims_status = handler_entry

        # Reivindicação atômica: nada abaixo desta linha executa se o Job não
        # estiver elegível, incluindo a corrida entre duas instâncias de
        # JobEngine contra o mesmo SQLite.
        claimed = self._claim(
            job.id,
            claims_status,
            job.operation,
            connector_key=self._resolve_connector_key(job),
        )

        try:
            result = handler(claimed)
            if not isinstance(result, JobStepResult):
                raise JobHandlerError(
                    "handler deve retornar um JobStepResult; "
                    f"recebido {type(result).__name__}"
                )
            return self._finalize_processing_result(claimed, claims_status, result)
        except HeavyWorkAdmissionDeniedError as exc:
            # PROMPT 18, correção pós-5ª revisão adversarial: o handler
            # nunca chegou a tocar o efeito pesado real (ver contrato de
            # ``admit_heavy_work``) — isto NÃO é uma falha de handler e
            # NUNCA deve pousar em FAILED/UNKNOWN via
            # ``_land_safely_after_claim_failure``. Pousa em CANCELLED (só
            # para cancel de trabalho local) ou BLOCKED (retomável) — ver
            # ``_land_after_admission_denied``.
            return self._land_after_admission_denied(claimed, claims_status, exc)
        except Exception as exc:
            # PROMPT 21: um handler pode classificar EXPLICITAMENTE a causa
            # da falha levantando ClassifiedJobFailure (nunca adivinhado
            # aqui) para que RetryPolicy decida o pouso/agendamento. Só
            # entra em ação quando AMBOS estão presentes -- um retry_policy
            # configurado E uma falha explicitamente classificada; qualquer
            # outra exceção (o comportamento de todo handler hoje) continua
            # caindo, sem exceção, no caminho cego de sempre.
            if self.retry_policy is not None and isinstance(exc, ClassifiedJobFailure):
                self._land_with_retry_policy(claimed, claims_status, exc)
            else:
                self._land_safely_after_claim_failure(claimed.id, claims_status, exc)
            if isinstance(exc, JobEngineError):
                raise
            raise JobHandlerError(
                f"handler falhou para Job {claimed.id} (operation={claimed.operation!r})"
            ) from exc

    def _land_with_retry_policy(
        self, claimed: Job, claims_status: str, exc: ClassifiedJobFailure
    ) -> None:
        """Pousa um Job cujo handler falhou com uma causa EXPLICITAMENTE
        classificada (``ClassifiedJobFailure``, PROMPT 21), usando
        ``RetryPolicy`` para decidir contador/agendamento/destino.

        SALVAGUARDA (CLAUDE.md, princípio M -- "UNKNOWN nunca pode gerar
        repost automático"): ``RetryPolicy`` é cego a ``claims_status`` (só
        decide a partir de ``category`` + histórico do Job -- ver
        ``retry_policy.py``). É AQUI que a decisão pura é filtrada pela
        realidade do trabalho reivindicado: um pouso ``RETRY`` só vira
        ``RETRY`` de verdade quando ``claims_status == PROCESSING``
        (trabalho local, sem efeito remoto ambíguo -- e a própria State
        Machine central já proíbe estruturalmente ``PUBLISHING -> RETRY``,
        reforçando isto). Para ``PUBLISHING``/``RECOVERING``, mesmo uma
        categoria "retry-able" (TEMPORARY/RATE_LIMIT/UNKNOWN) aterrissa no
        MESMO fallback seguro já aprovado para falha não classificada
        (``UNKNOWN`` via ``_SAFE_FALLBACK_STATUS``) -- o Circuit Breaker e o
        RetryPolicy continuam intocados por essa decisão de pouso, que é
        exclusiva do ``JobEngine`` (AUTORIDADE ÚNICA). ``AUTH_REQUIRED``/
        ``USER_ACTION_REQUIRED`` são a exceção deliberada: esses pousos são
        seguros para qualquer ``claims_status`` (não afirmam nada sobre o
        efeito remoto, só que uma ação humana é necessária -- mesmo
        princípio já aprovado para CAPTCHA/2FA, CLAUDE.md princípio F), e a
        State Machine central já permite essa transição a partir de
        PROCESSING/PUBLISHING/RECOVERING.
        """
        # JANELA DE CRASH (CLAUDE.md item 3): a decisão de RetryPolicy
        # (contador/agendamento persistidos em ``job_retry_state``) e a
        # transição de status do Job precisam ser a MESMA operação
        # atômica -- caso contrário um crash entre as duas deixaria
        # ``job_retry_state`` já atualizado com um Job ainda preso no
        # status reivindicado (PROCESSING/PUBLISHING/RECOVERING), nunca
        # aterrissando em RETRY/FAILED/AUTH_REQUIRED/USER_ACTION_REQUIRED.
        # Por isso ambas correm sob o mesmo ``BEGIN IMMEDIATE``, com
        # ``connection=conn`` roteado explicitamente (mesmo padrão já
        # usado por cancel/pause em outros pontos deste arquivo).
        with self.database.transaction() as conn:
            decision = self.retry_policy.record_failure(
                claimed.id, category=exc.category, connection=conn
            )
            if decision.target_status == JOB_RETRY and claims_status != JOB_PROCESSING:
                # Nunca reposta automaticamente algo cujo efeito remoto pode
                # já ter começado -- mesmo fallback já aprovado de sempre. O
                # agendamento que RetryPolicy acabou de persistir fica
                # simplesmente sem efeito (o Job não vai passar por RETRY
                # por este caminho): nada aqui reabre nem descarta essa
                # decisão, só decide não segui-la para este claims_status.
                target = _SAFE_FALLBACK_STATUS[claims_status]
            else:
                target = decision.target_status
            try:
                self.audit_log.transition_job(
                    claimed.id,
                    target,
                    data={
                        "reason": "advance_failed_after_claim",
                        "claims_status": claims_status,
                        "category": exc.category,
                        "attempt_count": decision.attempt_count,
                        "next_retry_at_epoch": decision.next_retry_at_epoch,
                    },
                    connection=conn,
                )
            except (InvalidJobState, InvalidJobTransition, JobNotFoundForAuditError):
                pass
        # Diagnóstico best-effort, própria transação (mesmo padrão já
        # aprovado em ``_land_safely_after_claim_failure``: ``record_error``
        # não aceita ``connection=`` e não precisa ser atômico com a
        # transição -- é informativo, não autoritativo).
        try:
            self.audit_log.record_error(
                claimed.id,
                code=f"JOB_ADVANCE_FAILED_{exc.category}",
                message=str(exc),
                recoverable=(target != JOB_FAILED),
                data={
                    "claims_status": claims_status,
                    "landed_status": target,
                    "category": exc.category,
                    "attempt_count": decision.attempt_count,
                    "next_retry_at_epoch": decision.next_retry_at_epoch,
                    "retries_exhausted": decision.retries_exhausted,
                },
            )
        except OperationalAuditError:
            pass

    def _land_after_admission_denied(
        self, claimed: Job, claims_status: str, exc: HeavyWorkAdmissionDeniedError
    ) -> Job:
        """Pousa um Job cujo efeito pesado NUNCA começou (negado por
        ``admit_heavy_work`` — ver docstring do módulo, "ADMISSÃO DE
        TRABALHO PESADO") em um estado seguro, auditável e RETOMÁVEL.

        Nunca usa ``FAILED``/``UNKNOWN`` aqui: esses destinos (ver
        ``_SAFE_FALLBACK_STATUS``) significam "o handler falhou depois de
        talvez ter começado um efeito", o que é exatamente o que NÃO
        aconteceu neste caminho.

        CORREÇÃO PÓS-6ª REVISÃO ADVERSARIAL (BLOQUEADOR 2 — cancel encalhado
        entre denial e landing): ``admit_heavy_work`` pode ter negado a
        admissão por um motivo diferente de cancel (``CONTROL_BLOCKED``/
        ``DRAINING``/``BATCH_PAUSED``) e, exatamente na janela entre essa
        negação e esta transaction abrir, um ``cancel(JOB, ...)`` concorrente
        pode ter sido registrado como ``cancel_requested=True`` (adiado,
        porque o Job ainda estava PROCESSING naquele instante — ver
        ``control_manager.py``, ``_CANCEL_DEFERRED_STATUSES``). Sem
        reavaliar isso aqui, o Job pousaria em ``BLOCKED`` com
        ``cancel_requested`` ainda pendurado — a intenção de cancelamento
        do usuário ficaria "perdida" até uma retomada manual notar o flag.

        Por isso esta transaction SEMPRE relê
        ``control_manager.is_job_cancel_requested(claimed.id, connection=conn)``
        (quando ``claims_status == PROCESSING`` — mesma restrição de
        ``_claim``/``admit_heavy_work``: nunca para PUBLISHING/RECOVERING)
        como a ÚLTIMA palavra sob a MESMA transaction que decide o pouso —
        cancel tem PRECEDÊNCIA sobre qualquer motivo de bloqueio não-cancel:

        - se ``cancel_requested`` está (ou acabou de ficar) ``True`` aqui:
          pousa ``CANCELLED`` e limpa ``cancel_requested``
          (``mark_job_cancel_applied``) na MESMA transaction — igual ao
          tratamento já aprovado do cancelamento adiado
          (``_finalize_processing_result``). Isso cobre tanto o motivo
          original ``CANCEL_REQUESTED`` quanto um cancel que chegou DEPOIS
          da negação por CONTROL_BLOCKED/DRAINING/BATCH_PAUSED mas ANTES
          desta transaction — as duas ordens da corrida (cancel vence o
          lock primeiro, ou esta transaction vence o lock primeiro e só
          depois o cancel concorrente prossegue e vê o Job já não-ativo)
          convergem para o mesmo resultado: CANCELLED.
        - caso contrário: pousa ``BLOCKED`` — estado que a State Machine
          central já permite a partir de PROCESSING/PUBLISHING/RECOVERING e
          que só pode seguir para ``RETRY``/``CANCELLED`` (nunca direto
          para um estado "de sucesso"), exigindo uma nova reivindicação
          explícita quando o bloqueio for resolvido — nenhuma repetição
          cega automática. O motivo e o detalhe diagnosticável ficam
          registrados no evento de auditoria da transição, nunca
          ``str(exc)``/traceback bruto.

        NUNCA fica com ``cancel_requested=True`` pendurado: ou a intenção de
        cancelamento é observada aqui e aplicada, ou ela ainda não existia
        quando este ``BEGIN IMMEDIATE`` decidiu — nesse caso um ``cancel()``
        que chegue depois encontra o Job já fora de ``PROCESSING`` e segue o
        caminho normal já aprovado para esse novo estado.

        CORREÇÃO PÓS-7ª REVISÃO ADVERSARIAL (BLOCKED era o destino errado
        para controle temporário): para ``claims_status == PROCESSING``
        (trabalho local), um motivo de negação NÃO-cancel
        (``CONTROL_BLOCKED``/``DRAINING``/``BATCH_PAUSED``) agora pousa em
        ``RETRY``, não mais em ``BLOCKED``. Justificativa: pause,
        stop_after_current, batch pause e DRAINING são autoridades
        TEMPORÁRIAS — nenhum efeito pesado começou, não há erro de
        conteúdo, não há falha permanente. ``BLOCKED`` exigia uma
        transição manual (via ``OperationalAuditLog`` direto, fora do
        produto) para o Job voltar a ser elegível, o que quebrava o fluxo
        real de pause/resume: depois de ``resume()``/``resume_batch()``/
        conclusão do DRAINING, ``fetch_pending_jobs()``/``advance_batch()``
        simplesmente não devolviam mais o Job, porque ``BLOCKED`` não está
        em ``_ACTIONABLE_STATES``. ``RETRY`` já está: o Job volta a ser
        automaticamente elegível assim que a autoridade correspondente for
        liberada, sem qualquer intervenção manual — e enquanto a
        autoridade permanecer ativa, ``_claim()``/``admit_heavy_work``
        continuam bloqueando a próxima tentativa exatamente como antes.
        Isso não enfraquece nenhuma autoridade: RETRY não é um estado
        "ativo" para pause/DRAINING/batch pause, então nada aqui os
        contorna — apenas evita uma pausa permanente disfarçada de estado
        retomável que na prática nunca era retomado automaticamente.

        Para ``claims_status`` PUBLISHING/RECOVERING (trabalho com efeito
        remoto potencialmente incerto), o destino permanece ``BLOCKED``,
        inalterado: ``admit_heavy_work`` não verifica
        ``cancel_requested`` para esses ``claims_status`` (só DRAINING/
        batch pause/control podem negar), e nenhum handler concreto deste
        Prompt usa ``ResourceManager`` para trabalho PUBLISHING/RECOVERING
        — não há ainda um fluxo de retry automático seguro e testado para
        reconciliação remota interrompida (isso pertence a um Prompt
        futuro de reconciliação/retry remoto, fora de escopo aqui). Manter
        ``BLOCKED`` preserva a semântica conservadora já aprovada
        (retomada explícita) em vez de inventar um comportamento não
        testado para um caminho que hoje não é exercitado por nenhum
        handler real.
        """
        with self.database.transaction() as conn:
            cancel_wins = (
                claims_status == JOB_PROCESSING
                and self.control_manager is not None
                and self.control_manager.is_job_cancel_requested(claimed.id, connection=conn)
            )
            if cancel_wins:
                cancelled_job = self.audit_log.transition_job(
                    claimed.id,
                    JOB_CANCELLED,
                    semantic_event=AUDIT_JOB_CANCELLED,
                    data={
                        "reason": "cancel_requested_before_heavy_work_started",
                        "original_denial_reason": exc.reason,
                        "claims_status": claims_status,
                    },
                    connection=conn,
                )
                self.control_manager.mark_job_cancel_applied(claimed.id, connection=conn)
                return cancelled_job

            target_status = JOB_RETRY if claims_status == JOB_PROCESSING else JOB_BLOCKED
            return self.audit_log.transition_job(
                claimed.id,
                target_status,
                data={
                    "reason": exc.reason,
                    "detail": exc.detail,
                    "claims_status": claims_status,
                },
                connection=conn,
            )

    def _finalize_processing_result(self, claimed: Job, claims_status: str, result: JobStepResult) -> Job:
        """Persiste o resultado final de um handler que já terminou,
        observando atomicamente uma intenção de cancelamento pendente.

        CORREÇÃO PÓS-3ª REVISÃO: a leitura de ``is_job_cancel_requested`` e a
        transição final (``CANCELLED`` ou o resultado do handler) acontecem
        dentro do MESMO ``BEGIN IMMEDIATE`` — exatamente a mesma técnica já
        usada para a corrida pause-vs-claim (ver ``_claim``). Antes disso, a
        leitura acontecia numa conexão solta e a transição numa transaction
        separada: um ``cancel(JOB)`` concorrente podia commitar entre as
        duas, e o Engine terminava persistindo o resultado do handler mesmo
        assim (ex.: ``PROCESSING -> READY`` com ``cancel_requested=True``
        já commitado). Como ambas as operações agora disputam o mesmo write
        lock SQLite via ``BEGIN IMMEDIATE``, a ordem de quem commita primeiro
        decide o resultado de forma determinística: se o cancelamento
        commitar antes desta transaction adquirir o lock, esta transaction
        enxerga ``cancel_requested=True`` e pousa o Job em ``CANCELLED``; se
        esta transaction adquirir o lock primeiro, ela conclui normalmente e
        o cancelamento (se vier) passa a valer só depois, sobre o novo
        estado. Quando o cancelamento é aplicado aqui, a transição para
        ``CANCELLED`` e a limpeza de ``cancel_requested``
        (``mark_job_cancel_applied``) são gravadas na MESMA transaction —
        nunca existe uma janela onde ``CANCELLED`` já esteja commitado mas
        ``cancel_requested`` continue ativo por acidente (nem mesmo sob
        crash: ou as duas escritas commitam juntas, ou nenhuma commita).

        Só se aplica a ``claims_status == PROCESSING`` (trabalho local) —
        nunca a ``PUBLISHING``/``RECOVERING``, onde um efeito remoto incerto
        ou uma reconciliação em andamento jamais pode ser mascarado por um
        cancelamento pedido nesse meio tempo (ver docstring do módulo e de
        ``control_manager.py``).
        """
        with self.database.transaction() as conn:
            if (
                claims_status == JOB_PROCESSING
                and self.control_manager is not None
                and self.control_manager.is_job_cancel_requested(claimed.id, connection=conn)
            ):
                cancelled_job = self.audit_log.transition_job(
                    claimed.id,
                    JOB_CANCELLED,
                    semantic_event=AUDIT_JOB_CANCELLED,
                    data={
                        "reason": "cancel_requested_while_processing",
                        "handler_target_status": result.target_status,
                    },
                    connection=conn,
                )
                self.control_manager.mark_job_cancel_applied(claimed.id, connection=conn)
                return cancelled_job

            return self.audit_log.transition_job(
                claimed.id,
                result.target_status,
                semantic_event=result.semantic_event,
                data=result.data,
                connection=conn,
            )

    def run_pending(self, *, limit: int | None = None) -> list[JobRunOutcome]:
        """Avança sequencialmente os Jobs pendentes encontrados no SQLite.

        Cada Job é buscado, executado e persistido individualmente, em
        ordem, sem paralelismo. A falha de um Job nunca interrompe os
        demais: cada tentativa produz um ``JobRunOutcome`` isolado, e o
        chamador consegue inspecionar quais Jobs tiveram sucesso e quais
        falharam (e por quê) sem precisar adivinhar. Concorrência real entre
        múltiplos workers, priorização e orçamento de recursos ficam para o
        Resource Manager (PROMPT 18) e o Batch Engine (PROMPT 17); este
        método só garante que nenhum avanço depende de estado mantido apenas
        em memória entre um Job e o próximo, e que um Job com problema não
        derruba o lote.
        """
        outcomes: list[JobRunOutcome] = []
        for job in self.fetch_pending_jobs(limit=limit):
            try:
                updated = self.advance(job.id)
                outcomes.append(JobRunOutcome(job_id=job.id, job=updated, error=None))
            except JobEngineError as exc:
                outcomes.append(JobRunOutcome(job_id=job.id, job=None, error=exc))
        return outcomes


__all__ = [
    "JobEngine",
    "JobEngineError",
    "JobNotFoundError",
    "UnknownJobOperationError",
    "JobNotActionableError",
    "JobBlockedByControlError",
    "JobBlockedByShutdownError",
    "JobBlockedByBatchError",
    "HeavyWorkAdmissionDeniedError",
    "JobHandlerError",
    "JobStepResult",
    "JobRunOutcome",
    "JobHandler",
    "CLAIMABLE_STATES",
]
