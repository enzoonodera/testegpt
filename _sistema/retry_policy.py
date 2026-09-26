# -*- coding: utf-8 -*-
"""Retry inteligente por Job (PROMPT 21 -- RETRY INTELIGENTE).

ESCOPO DESTE PROMPT: exclusivamente Geração 2 (``job_engine.py``,
``batch_engine.py``, ``domain/models.py`` -- não alterado --, camada
SQLite). NÃO altera Geração 1 (``agendar_youtube.py``/``agendar_tiktok.py``)
nem ``domain/job_state_machine.py``/os invariantes já aprovados de
idempotência/pause/resume/cancel/circuit breaker. ``RetryPolicy`` é uma
camada NOVA que decide QUAL transição pedir e QUANDO -- ela nunca substitui
a State Machine central (só usa transições já existentes:
``FAILED -> {RETRY, CANCELLED}``, ``AUTH_REQUIRED -> {RETRY, RECOVERING,
CANCELLED}``, etc., todas já aprovadas antes deste Prompt) nem o
``CircuitBreaker`` (PROMPT 20, ``circuit_breaker.py``), que continua com
autoridade EXCLUSIVA sobre admissão por connector.

RELAÇÃO COM O CIRCUIT BREAKER (autoridades ortogonais, nunca cruzadas)
------------------------------------------------------------------------
``CircuitBreaker`` decide se um CONNECTOR inteiro está admitindo trabalho
novo agora (uma pergunta sobre a SAÚDE DO CONNECTOR, agregada). ``RetryPolicy``
decide o que acontece com UM Job (tentar de novo quando, quantas vezes, ou
desistir -- uma pergunta sobre O ITEM). Este módulo NUNCA chama
``CircuitBreaker.is_admitted``/``record_failure``/``record_success``, e
``circuit_breaker.py`` não foi alterado nesta rodada (nenhuma linha
tocada -- confirmado por hash no relatório de entrega). Um Connector real
futuro (fora de escopo -- Prompts 57-60) poderá consultar os dois de forma
independente para a mesma falha real: por exemplo, uma falha sistêmica de
rede tanto conta para o contador do ``CircuitBreaker`` daquele connector
(``record_failure(connector_key, code=..., systemic=True)``) quanto decide,
via ``RetryPolicy``, se ESTE Job específico ganha uma nova tentativa
agendada. As duas decisões são independentes e podem divergir sem
conflito: um connector pode estar com o circuito ainda fechado enquanto um
Job individual já esgotou suas tentativas automáticas (ou vice-versa).

INVESTIGAÇÃO PRÉVIA (seção 0) -- confirmada antes de qualquer código
------------------------------------------------------------------------
1. ``domain/job_state_machine.py`` já define exatamente os pousos que este
   Prompt precisa -- nenhum estado novo foi criado, nenhuma transição
   alterada (arquivo intocado, confirmado por hash):
   ``FAILED -> {RETRY, CANCELLED}``, ``BLOCKED -> {RETRY, CANCELLED}``,
   ``AUTH_REQUIRED -> {RETRY, RECOVERING, CANCELLED}``,
   ``USER_ACTION_REQUIRED -> {RETRY, RECOVERING, CANCELLED}``,
   ``PROCESSING -> {..., RETRY, FAILED, AUTH_REQUIRED,
   USER_ACTION_REQUIRED, ...}``. Confirmado também que ``PUBLISHING`` NÃO
   tem ``RETRY`` no seu conjunto de transições permitidas (só
   ``PUBLISHED``/``UNKNOWN``/``FAILED``/``BLOCKED``/``AUTH_REQUIRED``/
   ``USER_ACTION_REQUIRED``) -- a própria State Machine central já proíbe
   ``PUBLISHING -> RETRY`` diretamente, reforçando estruturalmente (não só
   por convenção deste módulo) que trabalho com efeito remoto incerto nunca
   pula direto para uma nova tentativa automática.
2. Confirmado em ``job_engine.py``: sair de ``FAILED`` é hoje uma decisão
   exclusivamente manual (``BatchEngine.retry_failed()`` -> ``audit_log.retry()``
   -> ``transition_job(..., JOB_RETRY, ...)``), e a docstring do módulo já
   dizia isso explicitamente. Este Prompt NÃO torna toda saída de
   ``FAILED`` automática: RetryPolicy decide o pouso *antes* de chegar em
   ``FAILED`` (na hora da falha do handler, dentro de ``advance()``) --
   uma vez que um Job efetivamente pousa em ``FAILED`` (porque a categoria
   nunca permite retry automático, ou porque o limite de tentativas se
   esgotou), a única saída continua sendo o ``retry_failed()`` manual já
   aprovado, sem nenhuma mudança de comportamento aí além de reiniciar o
   contador (ver ``RetryPolicy.reset`` e a integração em
   ``batch_engine.py``).
3. Confirmado: nem ``JobStepResult`` nem ``JobRunOutcome`` carregam hoje
   nenhum dado estruturado sobre a CAUSA de uma falha -- um handler que
   falha simplesmente levanta uma exceção qualquer, e
   ``JobEngine.advance()`` a captura cegamente
   (``_SAFE_FALLBACK_STATUS[claims_status]``, sempre ``FAILED`` para
   ``PROCESSING`` ou ``UNKNOWN`` para ``PUBLISHING``/``RECOVERING``, sem
   olhar a exceção). Por isso este módulo define ``ClassifiedJobFailure``:
   uma exceção que um handler pode levantar DELIBERADAMENTE para classificar
   a falha (mesmo princípio do ``systemic=`` explícito do CircuitBreaker --
   nunca uma heurística que tenta adivinhar a categoria a partir do texto
   de uma exceção genérica). Um handler que continua levantando qualquer
   outra exceção (o comportamento de hoje, de todo handler existente) cai
   EXATAMENTE no mesmo caminho cego de sempre -- nenhum retry automático "de
   graça" por omissão (ver ``job_engine.py``, ``advance()``).

ATENÇÃO -- COLISÃO DE NOME DELIBERADA E DOCUMENTADA: a categoria de falha
``RETRY_CATEGORY_UNKNOWN`` ("UNKNOWN", exigida com esse nome exato pelo
roadmap) tem o MESMO valor de string que o estado de Job
``JOB_UNKNOWN`` ("UNKNOWN", ``job_state_machine.py``) -- são conceitos
totalmente diferentes (uma classificação de causa de falha vs. um estado
de Job para efeito remoto incerto) que coincidem de nome só porque o
roadmap pediu exatamente esse nome de categoria. Da mesma forma,
``RETRY_CATEGORY_AUTH_REQUIRED``/``RETRY_CATEGORY_USER_ACTION_REQUIRED``
coincidem de valor com ``JOB_AUTH_REQUIRED``/``JOB_USER_ACTION_REQUIRED``
(esses dois são deliberadamente iguais: a categoria mapeia 1:1 para o
estado do mesmo nome). O código sempre usa as constantes nomeadas
(nunca strings soltas) exatamente para que essa coincidência nunca vire um
bug silencioso.

CATEGORIA -> POUSO ADOTADO (ver justificativa completa no relatório de
entrega; aqui, o resumo do que o código realmente faz)
------------------------------------------------------------------------
- ``TEMPORARY``, ``RATE_LIMIT``, ``UNKNOWN``: retry automático com backoff
  (ver ``_compute_next_retry_at``) até ``max_attempts``; ao esgotar, ``FAILED``
  definitivo (nunca ``RETRY`` de novo sozinho).
- ``PERMANENT``, ``CONTENT_BLOCKED``: NUNCA retry automático -- direto
  ``FAILED`` (0 tentativas automáticas), mesmo raciocínio já usado pelo
  Circuit Breaker para "falha de conteúdo nunca conta": tentar de novo o
  MESMO item com o MESMO problema nunca muda o resultado.
- ``AUTH_REQUIRED``, ``USER_ACTION_REQUIRED``: pousam direto no estado
  dedicado já existente (``JOB_AUTH_REQUIRED``/``JOB_USER_ACTION_REQUIRED``),
  sem agendar retry algum -- não adianta tentar de novo sozinho sem a ação
  humana que o nome do estado exige.

REFINAMENTO NECESSÁRIO SOBRE CLAIMS_STATUS (CLAUDE.md, princípio M:
"UNKNOWN nunca pode gerar repost automático") -- este módulo (``RetryPolicy``)
é deliberadamente CEGO a ``claims_status``: ele só enxerga ``category`` e o
histórico de tentativas de um ``job_id``, exatamente como o CircuitBreaker é
cego ao ``Job`` e só enxerga ``connector_key``. É ``JobEngine`` quem aplica
a salvaguarda de ``claims_status`` por cima da decisão pura deste módulo:
um pouso ``RETRY`` decidido aqui só vira ``RETRY`` de verdade quando
``claims_status == PROCESSING`` (trabalho local, sem efeito remoto
ambíguo); para ``PUBLISHING``/``RECOVERING``, mesmo com uma categoria
"retry-able", ``JobEngine`` aterrissa no MESMO fallback seguro já aprovado
para falha não classificada (``UNKNOWN``) -- nunca reposta automaticamente
algo cujo efeito remoto pode já ter começado. ``AUTH_REQUIRED``/
``USER_ACTION_REQUIRED`` são a exceção: esses pousos são seguros para
QUALQUER ``claims_status`` (a própria State Machine central já permite a
transição a partir de ``PROCESSING``/``PUBLISHING``/``RECOVERING`` -- eles
não afirmam nada sobre o efeito remoto, só que uma ação humana é
necessária, o mesmo princípio já usado para CAPTCHA/2FA em
``CLAUDE.md``, princípio F). Ver ``job_engine.py``,
``JobEngine._land_with_retry_policy``.

BACKOFF COM JITTER
--------------------
``delay = min(max_backoff_seconds, base_seconds * factor**(tentativa-1))``,
mais jitter estritamente POSITIVO (``delay_final = delay * (1 +
jitter_ratio * r)``, ``r`` uniforme em ``[0, 1)``) -- nunca reduz o delay
abaixo da curva exponencial base, só adiciona uma folga aleatória por cima
para não fazer vários Jobs do mesmo connector caírem no MESMO instante
depois de uma instabilidade compartilhada (ex.: uma queda de rede breve que
afeta vários vídeos ao mesmo tempo). ``clock``/``random_func`` são
injetáveis para determinismo em teste (sem sleep real).

DEFAULTS -- justificativa própria, NÃO copiada do Circuit Breaker (são
problemas diferentes: aqui é backoff por TENTATIVA de UM Job; lá era
limiar de falhas CONSECUTIVAS de UM connector):
- ``DEFAULT_MAX_ATTEMPTS = 5``: com a curva abaixo, 5 tentativas cobrem
  ~15,5 minutos de janela automática antes de exigir decisão humana
  (``retry_failed()``) -- suficiente para a maioria das instabilidades
  transitórias reais de um passo de trabalho LOCAL (FFmpeg/Whisper/Ollama
  travando um disco ocupado, um arquivo temporariamente lockado no
  Windows, etc.) sem deixar o usuário esperando o dia inteiro por um Job
  que nunca vai se resolver sozinho.
- ``DEFAULT_BASE_SECONDS = 30.0``: primeira nova tentativa em 30s -- rápido
  o bastante para não frustrar o usuário com uma falha momentânea óbvia
  (ex.: arquivo temporariamente em uso por outro processo), sem martelar
  imediatamente um problema que claramente não vai se resolver em
  milissegundos.
- ``DEFAULT_FACTOR = 2.0``: dobrar é o padrão mais simples e testável de
  crescimento exponencial, sem sobre-engenharia.
- ``DEFAULT_MAX_BACKOFF_SECONDS = 1800.0`` (30 min): teto que nunca é
  atingido pelos 5 attempts default (30/60/120/240/480s), mas protege
  configurações com ``max_attempts`` maior de esperas monotonicamente
  crescentes sem limite.
- ``DEFAULT_JITTER_RATIO = 0.2`` (20%): valor comum na literatura de
  backoff distribuído (AWS/GCP), suficiente para espalhar tentativas
  concorrentes sem tornar o tempo de espera imprevisível demais para o
  usuário.

Todos configuráveis por instância -- nada hardcoded na lógica de decisão.

PERSISTÊNCIA -- tabela dedicada, NÃO ``Job.extra``, NÃO coluna nova em
``jobs``/``Job`` (contraste explícito com a decisão do Circuit Breaker)
------------------------------------------------------------------------
O Circuit Breaker (Prompt 20) preferiu COLUNAS DEDICADAS (não JSON) porque
``next_retry_eligible_at`` é comparado contra o relógio a cada decisão de
admissão. O MESMO argumento de performance/comparação-contra-relógio se
aplica aqui: ``next_retry_at_epoch`` é comparado a cada
``fetch_pending_jobs()``/``_claim()``. Mas, diferente do Circuit Breaker
(que criou uma entidade nova sem relação alguma com o schema existente),
uma "coluna dedicada" LITERAL na tabela ``jobs`` exigiria alterar o
dataclass ``Job`` em ``domain/models.py`` -- uma entidade compartilhada por
TODO o Geração 2 (``job_engine.py``, ``batch_engine.py``,
``resource_manager.py``, ``control_manager.py``, toda a suíte de testes
existente que constrói ``Job(...)``) e pela camada de codificação genérica
em ``storage/database.py`` (``EntitySpec.field_names`` deriva
automaticamente de TODOS os campos do dataclass). Alterar ``Job`` é um raio
de impacto muito maior do que o necessário para um dado que só o retry
automático usa, e o Circuit Breaker também não fez isso com ``Job`` (ele
criou uma tabela nova e independente, ``circuit_breaker_state``, chaveada
por ``connector_key`` -- não por ``job_id`` numa tabela existente).

Por isso: nem (a) coluna literal em ``jobs``, nem (b) ``Job.extra`` --
uma TERCEIRA opção que preserva a propriedade que importava em (a) (coluna
REAL comparável contra o relógio, sem reencode/decode de JSON a cada
leitura) com o raio de impacto de uma tabela nova e independente, exatamente
como o Circuit Breaker já fez para o problema análogo: ``job_retry_state``
(migration 005), chaveada por ``job_id`` (texto, SEM FK para ``jobs`` --
mesma escolha do Circuit Breaker de não amarrar ``connector_key`` a nenhuma
entidade existente, o que também mantém este módulo testável de forma
isolada, sem precisar de um ``Job`` real persistido para os testes de
unidade). ``Job.extra`` (opção b) foi descartada pela MESMA razão que valeu
para o Circuit Breaker: um valor comparado contra o relógio a cada decisão
de admissão merece uma coluna real, não um campo dentro de um JSON
reencodado.
"""
from __future__ import annotations

from dataclasses import dataclass
import random as _random_module
import sqlite3
import time as _time_module
from typing import Callable

from .domain import JOB_AUTH_REQUIRED, JOB_FAILED, JOB_RETRY, JOB_USER_ACTION_REQUIRED
from .storage.database import LocalDatabase


RETRY_CATEGORY_TEMPORARY = "TEMPORARY"
RETRY_CATEGORY_PERMANENT = "PERMANENT"
RETRY_CATEGORY_RATE_LIMIT = "RATE_LIMIT"
RETRY_CATEGORY_AUTH_REQUIRED = "AUTH_REQUIRED"
RETRY_CATEGORY_CONTENT_BLOCKED = "CONTENT_BLOCKED"
RETRY_CATEGORY_USER_ACTION_REQUIRED = "USER_ACTION_REQUIRED"
RETRY_CATEGORY_UNKNOWN = "UNKNOWN"

RETRY_CATEGORIES = frozenset(
    {
        RETRY_CATEGORY_TEMPORARY,
        RETRY_CATEGORY_PERMANENT,
        RETRY_CATEGORY_RATE_LIMIT,
        RETRY_CATEGORY_AUTH_REQUIRED,
        RETRY_CATEGORY_CONTENT_BLOCKED,
        RETRY_CATEGORY_USER_ACTION_REQUIRED,
        RETRY_CATEGORY_UNKNOWN,
    }
)

# Categorias que nunca contam para o contador nem agendam retry algum --
# pousam direto num estado dedicado que exige ação humana (nenhum dos dois
# afirma nada sobre o efeito remoto ter ocorrido ou não).
_HUMAN_ACTION_CATEGORIES = frozenset(
    {RETRY_CATEGORY_AUTH_REQUIRED, RETRY_CATEGORY_USER_ACTION_REQUIRED}
)
_HUMAN_ACTION_TARGET_BY_CATEGORY = {
    RETRY_CATEGORY_AUTH_REQUIRED: JOB_AUTH_REQUIRED,
    RETRY_CATEGORY_USER_ACTION_REQUIRED: JOB_USER_ACTION_REQUIRED,
}

# Categorias que NUNCA permitem retry automático -- direto FAILED, mesmo
# raciocínio do CircuitBreaker para "falha de conteúdo nunca conta".
_NEVER_RETRY_CATEGORIES = frozenset({RETRY_CATEGORY_PERMANENT, RETRY_CATEGORY_CONTENT_BLOCKED})

# Categorias com retry automático (até max_attempts, com backoff).
_AUTO_RETRY_CATEGORIES = frozenset(
    {RETRY_CATEGORY_TEMPORARY, RETRY_CATEGORY_RATE_LIMIT, RETRY_CATEGORY_UNKNOWN}
)

DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_BASE_SECONDS = 30.0
DEFAULT_FACTOR = 2.0
DEFAULT_MAX_BACKOFF_SECONDS = 1800.0
DEFAULT_JITTER_RATIO = 0.2


class RetryPolicyError(RuntimeError):
    """Erro de contrato de ``RetryPolicy``."""


class ClassifiedJobFailure(RuntimeError):
    """Um handler levanta isto para classificar EXPLICITAMENTE (nunca
    adivinhado por ``JobEngine``) a categoria de uma falha, para que
    ``RetryPolicy`` decida o pouso/agendamento. Levantar qualquer OUTRA
    exceção continua caindo no comportamento cego de sempre
    (``_SAFE_FALLBACK_STATUS``, sem retry automático algum) -- ver
    ``job_engine.py``, ``advance()``."""

    def __init__(self, category: str, *, message: str | None = None) -> None:
        if category not in RETRY_CATEGORIES:
            raise ValueError(
                f"categoria de falha inválida: {category!r}; esperado uma de "
                f"{sorted(RETRY_CATEGORIES)}"
            )
        super().__init__(message or category)
        self.category = category


@dataclass(frozen=True)
class RetryState:
    """Snapshot de leitura do estado persistido de retry de um Job.

    Um Job nunca visto por ``record_failure``/``reset`` tem estado
    implícito (0 tentativas, sem agendamento) -- não existe linha no SQLite
    até a primeira falha classificada."""

    job_id: str
    attempt_count: int
    next_retry_at_epoch: float | None
    last_failure_category: str | None


@dataclass(frozen=True)
class RetryDecision:
    """Decisão pura de ``RetryPolicy`` para uma falha classificada.

    ``target_status`` é sempre um estado válido da State Machine central
    (``RETRY``/``FAILED``/``AUTH_REQUIRED``/``USER_ACTION_REQUIRED``) --
    mas ``JobEngine`` ainda aplica a salvaguarda de ``claims_status`` por
    cima (ver módulo, "REFINAMENTO NECESSÁRIO"): um ``target_status ==
    RETRY`` daqui só vira ``RETRY`` de verdade para trabalho local
    (``claims_status == PROCESSING``)."""

    target_status: str
    attempt_count: int
    next_retry_at_epoch: float | None
    retries_exhausted: bool


def _normalize_job_id(job_id: str) -> str:
    value = str(job_id).strip()
    if not value:
        raise ValueError("job_id não pode ser vazio")
    return value


class RetryPolicy:
    """Autoridade de DECISÃO/AGENDAMENTO por Job (nunca de admissão por
    connector -- isso é o ``CircuitBreaker``, Prompt 20, nunca chamado
    daqui). Colaborador opcional de ``JobEngine`` (mesmo padrão de
    ``circuit_breaker``/``control_manager``/``batch_engine``/
    ``shutdown_coordinator``): quando não fornecido, o comportamento é
    idêntico ao de antes deste Prompt -- toda falha de handler cai no
    caminho cego de sempre, sem retry automático."""

    def __init__(
        self,
        database: LocalDatabase,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        base_seconds: float = DEFAULT_BASE_SECONDS,
        factor: float = DEFAULT_FACTOR,
        max_backoff_seconds: float = DEFAULT_MAX_BACKOFF_SECONDS,
        jitter_ratio: float = DEFAULT_JITTER_RATIO,
        clock: Callable[[], float] | None = None,
        random_func: Callable[[], float] | None = None,
    ) -> None:
        max_attempts = int(max_attempts)
        if max_attempts < 0:
            raise ValueError("max_attempts deve ser >= 0")
        base_seconds = float(base_seconds)
        if base_seconds <= 0:
            raise ValueError("base_seconds deve ser > 0")
        factor = float(factor)
        if factor < 1.0:
            raise ValueError("factor deve ser >= 1.0 (backoff nunca pode encolher)")
        max_backoff_seconds = float(max_backoff_seconds)
        if max_backoff_seconds < base_seconds:
            raise ValueError("max_backoff_seconds deve ser >= base_seconds")
        jitter_ratio = float(jitter_ratio)
        if jitter_ratio < 0:
            raise ValueError("jitter_ratio deve ser >= 0")
        self.database = database
        self.max_attempts = max_attempts
        self.base_seconds = base_seconds
        self.factor = factor
        self.max_backoff_seconds = max_backoff_seconds
        self.jitter_ratio = jitter_ratio
        self._clock: Callable[[], float] = clock or _time_module.time
        self._random: Callable[[], float] = random_func or _random_module.random

    # ------------------------------------------------------------------
    # Backoff puro (sem I/O) -- fácil de testar isoladamente
    # ------------------------------------------------------------------

    def compute_delay_seconds(self, attempt_number: int) -> float:
        """Delay (em segundos, com jitter) para a N-ésima tentativa
        automática (``attempt_number`` 1-indexado: 1 é a primeira nova
        tentativa depois da falha original)."""
        if attempt_number < 1:
            raise ValueError("attempt_number deve ser >= 1")
        base_delay = self.base_seconds * (self.factor ** (attempt_number - 1))
        base_delay = min(base_delay, self.max_backoff_seconds)
        jitter = base_delay * self.jitter_ratio * self._random()
        return base_delay + jitter

    def _compute_next_retry_at(self, attempt_number: int) -> float:
        return self._clock() + self.compute_delay_seconds(attempt_number)

    # ------------------------------------------------------------------
    # Leitura
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_state(job_id: str, row: sqlite3.Row | None) -> RetryState:
        if row is None:
            return RetryState(
                job_id=job_id, attempt_count=0, next_retry_at_epoch=None, last_failure_category=None
            )
        return RetryState(
            job_id=job_id,
            attempt_count=int(row["attempt_count"]),
            next_retry_at_epoch=(
                float(row["next_retry_at_epoch"]) if row["next_retry_at_epoch"] is not None else None
            ),
            last_failure_category=row["last_failure_category"],
        )

    def get_state(self, job_id: str) -> RetryState:
        job_id = _normalize_job_id(job_id)
        with self.database.connection() as conn:
            row = conn.execute(
                "SELECT * FROM job_retry_state WHERE job_id = ?", (job_id,)
            ).fetchone()
        return self._row_to_state(job_id, row)

    def is_eligible_now(self, job_id: str, *, connection: sqlite3.Connection | None = None) -> bool:
        """Gate de elegibilidade (item 1.5): um Job com ``next_retry_at_epoch``
        no futuro não pode ser reivindicado ainda. Um Job sem nenhuma linha
        (nunca falhou, ou veio do caminho manual ``retry_failed()``/``reset()``,
        que remove a linha) é IMEDIATAMENTE elegível -- não regride o
        comportamento manual pré-existente."""
        job_id = _normalize_job_id(job_id)
        if connection is not None:
            return self._is_eligible_locked(job_id, connection)
        with self.database.connection() as conn:
            return self._is_eligible_locked(job_id, conn)

    def _is_eligible_locked(self, job_id: str, conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            "SELECT next_retry_at_epoch FROM job_retry_state WHERE job_id = ?", (job_id,)
        ).fetchone()
        if row is None or row["next_retry_at_epoch"] is None:
            return True
        return self._clock() >= float(row["next_retry_at_epoch"])

    # ------------------------------------------------------------------
    # Escrita
    # ------------------------------------------------------------------

    def record_failure(
        self, job_id: str, *, category: str, connection: sqlite3.Connection | None = None
    ) -> RetryDecision:
        """Classifica uma falha JÁ OCORRIDA para este Job e persiste a
        decisão resultante (contador/agendamento/categoria). Pura função de
        ``category`` + histórico de tentativas deste ``job_id`` -- nunca
        olha ``claims_status`` nem qualquer outro dado de ``Job`` (essa
        salvaguarda é aplicada por ``JobEngine``, não aqui -- ver módulo)."""
        job_id = _normalize_job_id(job_id)
        if category not in RETRY_CATEGORIES:
            raise ValueError(
                f"categoria de falha inválida: {category!r}; esperado uma de "
                f"{sorted(RETRY_CATEGORIES)}"
            )
        if connection is not None:
            return self._record_failure_locked(job_id, category, connection)
        with self.database.transaction() as conn:
            return self._record_failure_locked(job_id, category, conn)

    def _record_failure_locked(
        self, job_id: str, category: str, conn: sqlite3.Connection
    ) -> RetryDecision:
        row = conn.execute(
            "SELECT * FROM job_retry_state WHERE job_id = ?", (job_id,)
        ).fetchone()
        current_attempts = int(row["attempt_count"]) if row is not None else 0

        if category in _HUMAN_ACTION_CATEGORIES:
            target = _HUMAN_ACTION_TARGET_BY_CATEGORY[category]
            new_attempts = current_attempts
            next_retry_at_epoch = None
            retries_exhausted = False
        elif category in _NEVER_RETRY_CATEGORIES:
            target = JOB_FAILED
            new_attempts = current_attempts
            next_retry_at_epoch = None
            retries_exhausted = True
        else:
            assert category in _AUTO_RETRY_CATEGORIES
            if current_attempts >= self.max_attempts:
                target = JOB_FAILED
                new_attempts = current_attempts
                next_retry_at_epoch = None
                retries_exhausted = True
            else:
                new_attempts = current_attempts + 1
                next_retry_at_epoch = self._compute_next_retry_at(new_attempts)
                target = JOB_RETRY
                retries_exhausted = False

        self._write_row(
            conn,
            job_id=job_id,
            attempt_count=new_attempts,
            next_retry_at_epoch=next_retry_at_epoch,
            last_failure_category=category,
        )
        return RetryDecision(
            target_status=target,
            attempt_count=new_attempts,
            next_retry_at_epoch=next_retry_at_epoch,
            retries_exhausted=retries_exhausted,
        )

    def reset(self, job_id: str, *, connection: sqlite3.Connection | None = None) -> None:
        """Reinicia o ciclo de tentativas automáticas de um Job (item 1.3):
        chamado pelo caminho MANUAL já aprovado (``BatchEngine.retry_failed()``)
        ANTES de transicionar o Job para ``RETRY``, para que o próximo ciclo
        de tentativas automáticas comece do zero -- nunca herda o contador
        do ciclo anterior. Remove a linha por completo (equivalente a
        "nunca falhou"): ``attempt_count`` volta a 0, sem agendamento."""
        job_id = _normalize_job_id(job_id)
        if connection is not None:
            self._reset_locked(job_id, connection)
            return
        with self.database.transaction() as conn:
            self._reset_locked(job_id, conn)

    @staticmethod
    def _reset_locked(job_id: str, conn: sqlite3.Connection) -> None:
        conn.execute("DELETE FROM job_retry_state WHERE job_id = ?", (job_id,))

    @staticmethod
    def _write_row(
        conn: sqlite3.Connection,
        *,
        job_id: str,
        attempt_count: int,
        next_retry_at_epoch: float | None,
        last_failure_category: str,
    ) -> None:
        from .time_utils import utc_now_iso

        conn.execute(
            "INSERT INTO job_retry_state("
            "job_id, attempt_count, next_retry_at_epoch, last_failure_category, updated_at"
            ") VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(job_id) DO UPDATE SET "
            "attempt_count=excluded.attempt_count, "
            "next_retry_at_epoch=excluded.next_retry_at_epoch, "
            "last_failure_category=excluded.last_failure_category, "
            "updated_at=excluded.updated_at",
            (job_id, attempt_count, next_retry_at_epoch, last_failure_category, utc_now_iso()),
        )


__all__ = [
    "RETRY_CATEGORY_TEMPORARY",
    "RETRY_CATEGORY_PERMANENT",
    "RETRY_CATEGORY_RATE_LIMIT",
    "RETRY_CATEGORY_AUTH_REQUIRED",
    "RETRY_CATEGORY_CONTENT_BLOCKED",
    "RETRY_CATEGORY_USER_ACTION_REQUIRED",
    "RETRY_CATEGORY_UNKNOWN",
    "RETRY_CATEGORIES",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_BASE_SECONDS",
    "DEFAULT_FACTOR",
    "DEFAULT_MAX_BACKOFF_SECONDS",
    "DEFAULT_JITTER_RATIO",
    "RetryPolicyError",
    "ClassifiedJobFailure",
    "RetryState",
    "RetryDecision",
    "RetryPolicy",
]
