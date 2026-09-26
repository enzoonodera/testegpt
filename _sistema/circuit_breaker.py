# -*- coding: utf-8 -*-
"""Circuit Breaker por connector (PROMPT 20 -- FILA RESILIENTE E CIRCUIT BREAKER).

ESCOPO DESTE PROMPT: exclusivamente Geração 2 (``job_engine.py``,
``batch_engine.py``, ``resource_manager.py``, ``control_manager.py``,
``domain/models.py``, camada SQLite). NÃO altera Geração 1
(``agendar_youtube.py``/``agendar_tiktok.py``, scripts reais de Playwright)
nem ``domain/job_state_machine.py``/os invariantes já validados de
idempotência/pause/resume/cancel. O Circuit Breaker é uma camada NOVA que
CONSULTA os mecanismos já existentes de admissão de Job
(``JobEngine._claim``/``fetch_pending_jobs``/``admit_heavy_work`` -- ver
``job_engine.py``), nunca os substitui: ele só decide "admitir ou bloquear",
nunca ele mesmo muda ``Job.status`` (isso continua sendo decisão exclusiva
do ``JobEngine``/``OperationalAuditLog``/State Machine central -- ver
CLAUDE.md, princípio "AUTORIDADE ÚNICA").

INVESTIGAÇÃO PRÉVIA (seção 0 do Prompt) -- resumo do que foi confirmado
antes de qualquer código deste módulo ser escrito (evidência completa no
relatório de entrega desta rodada):

1. ``JobEngine.run_pending()`` JÁ isola falha por Job (``try/except
   JobEngineError`` dentro do laço, ``job_engine.py`` linha ~1231) e isso
   JÁ é provado por teste existente e inequívoco:
   ``tests/test_job_engine.py::test_run_pending_isola_falha_de_um_job_e_continua_os_demais``.
   Não foi reimplementado nem duplicado aqui.

2. ``Job`` (``domain/models.py``) NÃO tem ``account_id`` nem ``platform``, e
   NÃO existe FK/coluna alguma ligando ``jobs`` a ``publications``/
   ``accounts`` no schema SQLite hoje (``storage/migrations/m001_initial.py``
   -- tabela ``jobs`` não tem ``account_id``/``platform``/``publication_id``).
   ``ControlManager`` já documentou exatamente essa mesma lacuna para os
   escopos ``ACCOUNT``/``PLATFORM`` (persistidos, mas NUNCA aplicados
   automaticamente por ``JobEngine`` -- ver ``control_manager.py``,
   ``MANUALLY_ENFORCED_SCOPES``) e ``JobEngine.admit_heavy_work`` já
   documenta a mesma limitação. Este módulo segue a MESMA decisão já
   aprovada: não inventa uma FK nova em ``Job``/``jobs`` (fora de escopo --
   isso pertence ao futuro Connector Contract, Prompts 57-60), e não tenta
   adivinhar identidade de connector a partir de heurística nenhuma.

   CONECTOR ADOTADO NESTE PROMPT: uma string canônica fornecida
   explicitamente por quem chama (o futuro Connector/handler concreto, ou o
   teste que o simula) -- ``"youtube"`` (só platform) ou
   ``"youtube:canal-principal"`` (platform+conta), sem formato imposto pelo
   código além de "não vazia". A ÚNICA integração automática com
   ``JobEngine`` é por CONVENÇÃO, não por schema novo: quando presente,
   ``job.extra["connector_key"]`` (campo genérico que já existe em todo
   ``Entity`` via ``extra_json``, sem migration) é lido por
   ``JobEngine._resolve_connector_key`` e usado para consultar este Circuit
   Breaker antes de reivindicar o Job. Um Job sem essa chave em ``extra``
   NUNCA é bloqueado por este caminho (não há identidade para consultar) --
   exatamente a mesma semântica já aprovada para ACCOUNT/PLATFORM em
   ``ControlManager``.

3. ``ErrorRecord`` (``domain/models.py``) já existe e já é persistido em
   SQLite (tabela ``errors``, ``storage/database.py`` ``ENTITY_SPECS``),
   mas o caminho de falha real de ``JobEngine`` nunca escreve nele hoje --
   só grava em ``audit_events`` via ``OperationalAuditLog``. Reaproveitar
   ``ErrorRecord`` para o contador de falhas do Circuit Breaker exigiria
   uma tabela genérica (um ``ErrorRecord`` por Job, não por connector) sem
   índice por connector, forçando ou uma varredura O(n) a cada decisão de
   admissão ou uma coluna nova em ``errors`` só para isto -- nenhuma das
   duas reaproveita a tabela sem alterá-la ou duplicar esforço. Por isso
   este módulo usa uma tabela dedicada, pequena e indexada por
   ``connector_key`` (``circuit_breaker_state``, migration 004 --
   ver decisão completa abaixo), e reaproveita ``audit_events`` (via
   ``LocalDatabase.append_audit_event``, SEM tabela nova) só para os
   eventos observáveis de abertura/fechamento -- o mesmo padrão que
   ``BatchEngine``/``ControlManager`` já usam para eventos operacionais.

4. ``BatchEngine.pause_batch``/``resume_batch``/``is_batch_paused``/
   ``is_job_admission_blocked`` pausam exclusivamente por ``batch_id``
   (chave fixa ``f"batch:{batch_id}:paused"`` em ``settings``) -- não há
   uma chave genérica reaproveitável para "connector" sem forçar essa
   abstração para um conceito (conta/plataforma) que ``BatchEngine`` nunca
   modelou. Reaproveitar exigiria ou (a) tratar cada connector como um
   "batch" fictício -- confundindo duas autoridades completamente
   diferentes (agrupamento de Jobs relacionados vs. saúde de um connector
   remoto), ou (b) generalizar a chave de pausa de ``BatchEngine`` para
   aceitar qualquer string -- uma mudança em um componente já validado e
   fora do escopo autorizado deste Prompt ("NÃO mover/duplicar a lógica de
   pause/resume/cancel do Batch já validada"). Por isso este módulo NÃO
   reaproveita ``BatchEngine``: implementa sua própria autoridade de
   admissão (só leitura de admissão, nunca dono de ``Job.status``),
   integrada a ``JobEngine`` exatamente pelo mesmo padrão de colaborador
   opcional já usado por ``control_manager``/``shutdown_coordinator``/
   ``batch_engine`` (ver ``JobEngine.__init__``/``_claim``/
   ``fetch_pending_jobs``/``admit_heavy_work``).

5. ``ResourceManager``/``StorageManager`` (Prompts 18/19) continuam com
   responsabilidade exclusiva de orçamento de recurso (CPU/RAM/GPU/VRAM/
   disco) -- este módulo nunca consulta nem duplica nada de
   ``resource_manager.py``. Circuit Breaker é estritamente sobre falhas
   SISTÊMICAS repetidas de connector, nunca sobre capacidade.

SISTÊMICO vs CONTEÚDO (critério central deste Prompt)
------------------------------------------------------
Quem chama ``record_failure()`` classifica EXPLICITAMENTE via
``systemic=True/False`` -- este módulo nunca tenta adivinhar a partir do
``code`` sozinho, porque só quem está mais perto do efeito real (o handler/
Connector concreto) sabe se um erro é "o connector em si está com
problema" ou "este item específico tem um problema". Exemplos concretos:

SISTÊMICO (conta para o contador de falhas consecutivas do connector):
    - timeout/falha de conexão ao abrir a página ou ao service remoto;
    - sessão/login expirado ("auth_expired", "login_required");
    - elemento de navegação essencial da própria plataforma não encontrado
      (indício de site fora do ar, layout mudou, ou bloqueio anti-bot) --
      NÃO um elemento específico de UM vídeo (ex.: campo de descrição
      daquele upload em particular);
    - erro 5xx / "serviço indisponível" devolvido pela plataforma remota;
    - rede indisponível localmente (sem DNS, sem rota).

CONTEÚDO (NUNCA conta para o contador, mesmo repetido 3x com itens
diferentes por coincidência):
    - um vídeo específico com formato/codec inválido;
    - título/descrição excede o limite de caracteres da plataforma;
    - direitos autorais detectados NAQUELE vídeo específico (política de
      copyright já tratada em rodadas anteriores -- GATE 19.5 -- é
      inteiramente ortogonal a isto);
    - vídeo duplicado/já publicado antes.

Um erro de conteúdo, mesmo repetido, nunca deve impedir que o PRÓXIMO vídeo
daquele MESMO connector seja tentado -- é exatamente o oposto do que o
Circuit Breaker protege.

N (limiar de falhas) e COOLDOWN -- padrão conservador e justificativa
-----------------------------------------------------------------------
``DEFAULT_FAILURE_THRESHOLD = 5``: como este Prompt explicitamente NÃO
implementa retry automático (isso é o Prompt 21, o próximo da fila), cada
falha sistêmica registrada aqui corresponde hoje a uma tentativa real e
completa de um Job diferente daquele connector. Um limiar baixo demais
(ex.: 1-2) abriria o circuito por uma única instabilidade transitória de
rede -- exatamente o tipo de falso positivo que a distinção
sistêmico-vs-conteúdo já tenta evitar de um jeito, mas que o limiar evita
de outro (mesmo um erro genuinamente sistêmico pode ser uma reinicialização
de rede de alguns segundos). Um limiar alto demais (ex.: 20+) deixaria o
produto martelar repetidamente um connector genuinamente quebrado (ex.:
sessão expirada) por muito tempo antes de proteger a fila. 5 falhas
IDÊNTICAS consecutivas (mesmo ``code``) é o valor mínimo que ainda
distingue de forma robusta "isto está realmente quebrado" de "uma
instabilidade pontual", alinhado com o padrão comum de Circuit Breakers
(ex.: Hystrix usa uma janela de decisão similar por padrão) adaptado aqui
para o modelo mais simples "N consecutivas" deste Prompt.

``DEFAULT_COOLDOWN_SECONDS = 900`` (15 minutos): tempo suficiente para a
maioria das instabilidades transitórias reais (rate limiting temporário,
blip de rede, deploy da plataforma remota) se resolverem sozinhas, ou para
o usuário perceber e corrigir manualmente um problema que exige ação sua
(ex.: relogar numa conta cuja sessão expirou) sem deixar o connector
bloqueado por horas. Ambos os valores são configuráveis por instância
(``failure_threshold``/``cooldown_seconds`` no construtor) -- nenhum valor
está hardcoded na lógica de decisão.

HALF-OPEN -- decisão explícita de NÃO implementar nesta rodada
------------------------------------------------------------------
Este Prompt implementa apenas cooldown simples: depois de
``cooldown_seconds``, a PRÓXIMA consulta de admissão (``is_admitted``) já
fecha o circuito e admite normalmente -- sem reservar "1 Job de teste"
isolado dos demais. Um half-open real (deixar passar exatamente 1 Job como
sonda, mantendo os demais bloqueados até o resultado dessa sonda) exigiria
selecionar QUAL Job daquele connector é "o de teste" em meio a
``run_pending()`` processando sequencialmente vários Jobs do mesmo
connector -- e ``Job`` não carrega identidade de connector nativamente
(ver item 2 acima), então a única forma de saber "isto é uma sonda" seria
inventar um estado adicional em memória ou uma segunda tabela de
"sonda em voo", o que: (a) adiciona uma segunda fonte de verdade sobre o
que é ou não uma tentativa válida, tensionando com "AUTORIDADE ÚNICA"; e
(b) tem uma falha de correção real não resolvida sem o Prompt 21 (retry):
se a única sonda falhar, o próximo Job do mesmo connector só volta a ser
tentado no próximo ciclo natural de ``run_pending()`` de qualquer forma,
então o ganho prático de half-open sobre "cooldown simples + reabrir na
próxima falha" é marginal enquanto não existe retry automático. Cooldown
simples é mais fácil de raciocinar, testar deterministicamente e não
introduz um estado transitório novo sem um consumidor real ainda (retry
automático, Prompt 21) que dependa dele. Reavaliar half-open é recomendado
quando o Prompt 21 (retry) existir e puder coordenar qual tentativa é a
sonda.

PERSISTÊNCIA
-------------
Estado (aberto/fechado, contador de falhas consecutivas, timestamp da
última falha, timestamp de elegibilidade para nova tentativa) é persistido
em SQLite, tabela dedicada ``circuit_breaker_state`` (migration 004,
``storage/migrations/m004_circuit_breaker.py``) -- não um JSON solto em
arquivo separado, pelo mesmo motivo de todo o resto do produto (ver
CLAUDE.md, princípio E: "SQLite é exclusivamente LOCAL... histórico...
erros... configurações operacionais"). Sobrevive a um restart normalmente:
uma nova instância de ``CircuitBreaker`` contra o MESMO arquivo SQLite lê o
mesmo estado persistido (ver ``tests/test_circuit_breaker.py``, testes de
restart -- novas instâncias de ``LocalDatabase``/``CircuitBreaker``, nunca
reuso de objetos em memória, conforme CLAUDE.md ponto 4 "RESTART").

CONCORRÊNCIA / TOCTOU
-----------------------
Toda decisão que LÊ e potencialmente ESCREVE (``is_admitted`` fechando o
circuito automaticamente após o cooldown; ``record_failure`` decidindo se
abre o circuito) acontece dentro de uma ÚNICA transaction SQLite
``BEGIN IMMEDIATE`` (``LocalDatabase.transaction()``), a mesma técnica já
usada por ``JobEngine._claim``/``ControlManager``/``BatchEngine`` -- o
SQLite serializa escritores concorrentes, então duas instâncias de
``CircuitBreaker`` (mesmo processo ou processos diferentes) contra o mesmo
banco nunca decidem com uma leitura stale uma da outra (ver
``tests/test_circuit_breaker.py``, testes de duas instâncias concorrentes).
Quando ``JobEngine`` já está dentro de uma transaction própria (``_claim``/
``admit_heavy_work``), este módulo aceita ``connection=`` explícito e
participa da MESMA transaction -- nunca abre uma segunda, fechando a mesma
classe de corrida já documentada em ``job_engine.py`` (pause-vs-claim).
"""
from __future__ import annotations

from dataclasses import dataclass
import sqlite3
import time as _time_module
from typing import Callable

from .storage.database import LocalDatabase
from .time_utils import utc_now_iso


STATE_CLOSED = "CLOSED"
STATE_OPEN = "OPEN"

DEFAULT_FAILURE_THRESHOLD = 5
DEFAULT_COOLDOWN_SECONDS = 900.0

CIRCUIT_BREAKER_OPENED_EVENT = "CIRCUIT_BREAKER_OPENED"
CIRCUIT_BREAKER_CLOSED_EVENT = "CIRCUIT_BREAKER_CLOSED"


class CircuitBreakerError(RuntimeError):
    """Erro de contrato do Circuit Breaker."""


@dataclass(frozen=True)
class CircuitBreakerState:
    """Snapshot de leitura do estado persistido de um connector.

    Um connector nunca visto (``record_failure``/``record_success`` nunca
    chamados para ele) tem estado implícito ``CLOSED``/0 falhas -- não
    existe linha no SQLite até a primeira falha sistêmica ou o primeiro
    sucesso explicitamente registrados.
    """

    connector_key: str
    state: str
    consecutive_failures: int
    last_failure_code: str | None
    last_failure_at: str | None
    opened_at: str | None
    retry_eligible_at_epoch: float | None

    @property
    def is_open(self) -> bool:
        return self.state == STATE_OPEN


def _normalize_connector_key(connector_key: str) -> str:
    key = str(connector_key).strip()
    if not key:
        raise ValueError("connector_key não pode ser vazio")
    return key


class CircuitBreaker:
    """Autoridade de ADMISSÃO (nunca de ``Job.status``) por connector.

    Colaborador opcional de ``JobEngine`` (mesmo padrão de
    ``control_manager``/``shutdown_coordinator``/``batch_engine`` -- ver
    ``job_engine.py``): quando não fornecido a nenhum ``JobEngine``, ou
    quando um Job não carrega ``extra["connector_key"]``, o comportamento é
    idêntico ao de antes deste Prompt -- nenhum Job é bloqueado por este
    caminho.
    """

    def __init__(
        self,
        database: LocalDatabase,
        *,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        failure_threshold = int(failure_threshold)
        if failure_threshold < 1:
            raise ValueError("failure_threshold deve ser >= 1")
        cooldown_seconds = float(cooldown_seconds)
        if cooldown_seconds <= 0:
            raise ValueError("cooldown_seconds deve ser > 0")
        self.database = database
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        # Injeção de clock só para determinismo em teste (simular cooldown
        # decorrido sem sleep real). Padrão: time.time() de verdade.
        self._clock: Callable[[], float] = clock or _time_module.time

    # ------------------------------------------------------------------
    # Leitura
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_state(connector_key: str, row: sqlite3.Row | None) -> CircuitBreakerState:
        if row is None:
            return CircuitBreakerState(
                connector_key=connector_key,
                state=STATE_CLOSED,
                consecutive_failures=0,
                last_failure_code=None,
                last_failure_at=None,
                opened_at=None,
                retry_eligible_at_epoch=None,
            )
        return CircuitBreakerState(
            connector_key=connector_key,
            state=row["state"],
            consecutive_failures=int(row["consecutive_failures"]),
            last_failure_code=row["last_failure_code"],
            last_failure_at=row["last_failure_at"],
            opened_at=row["opened_at"],
            retry_eligible_at_epoch=(
                float(row["retry_eligible_at_epoch"])
                if row["retry_eligible_at_epoch"] is not None
                else None
            ),
        )

    def get_state(self, connector_key: str) -> CircuitBreakerState:
        """Leitura pura (não fecha o circuito mesmo que o cooldown já tenha
        decorrido -- só ``is_admitted()``/``record_success()`` fazem essa
        transição, porque só elas representam uma decisão de admissão real
        sendo tomada). Útil para diagnóstico/observabilidade sem efeito
        colateral."""
        connector_key = _normalize_connector_key(connector_key)
        with self.database.connection() as conn:
            row = conn.execute(
                "SELECT * FROM circuit_breaker_state WHERE connector_key = ?",
                (connector_key,),
            ).fetchone()
        return self._row_to_state(connector_key, row)

    def is_admitted(
        self, connector_key: str, *, connection: sqlite3.Connection | None = None
    ) -> bool:
        """Decide se um novo Job deste connector pode ser admitido AGORA.

        Se o circuito estiver ``OPEN`` e o cooldown já tiver decorrido, esta
        chamada fecha o circuito (ver módulo, "HALF-OPEN") e devolve
        ``True`` -- a mesma leitura que decide já é a escrita que fecha,
        dentro da mesma transaction, fechando o TOCTOU entre "ver que já
        pode fechar" e "fechar de fato" (ver ``JobEngine._claim`` para o
        mesmo padrão já aprovado)."""
        connector_key = _normalize_connector_key(connector_key)
        if connection is not None:
            return self._is_admitted_locked(connector_key, connection)
        with self.database.transaction() as conn:
            return self._is_admitted_locked(connector_key, conn)

    def _is_admitted_locked(self, connector_key: str, conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            "SELECT * FROM circuit_breaker_state WHERE connector_key = ?",
            (connector_key,),
        ).fetchone()
        if row is None or row["state"] == STATE_CLOSED:
            return True
        retry_eligible_at_epoch = row["retry_eligible_at_epoch"]
        now = self._clock()
        if retry_eligible_at_epoch is not None and now >= float(retry_eligible_at_epoch):
            self._close_locked(
                connector_key,
                conn,
                opened_at_epoch=row["opened_at_epoch"],
                now=now,
            )
            return True
        return False

    # ------------------------------------------------------------------
    # Escrita
    # ------------------------------------------------------------------

    def record_success(
        self, connector_key: str, *, connection: sqlite3.Connection | None = None
    ) -> None:
        """Reseta o contador de falhas consecutivas do connector.

        Se o circuito estiver ``OPEN`` no momento do sucesso (só possível se
        alguém chamar isto fora do fluxo normal de admissão -- ``is_admitted``
        já bloquearia um Job desse connector enquanto ``OPEN`` -- por
        exemplo um teste/handler chamando diretamente), fecha o circuito
        também: um sucesso real é o sinal mais forte possível de que o
        connector voltou a funcionar."""
        connector_key = _normalize_connector_key(connector_key)
        if connection is not None:
            self._record_success_locked(connector_key, connection)
            return
        with self.database.transaction() as conn:
            self._record_success_locked(connector_key, conn)

    def _record_success_locked(self, connector_key: str, conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT * FROM circuit_breaker_state WHERE connector_key = ?",
            (connector_key,),
        ).fetchone()
        if row is not None and row["state"] == STATE_OPEN:
            self._close_locked(
                connector_key, conn, opened_at_epoch=row["opened_at_epoch"], now=self._clock()
            )
            return
        conn.execute(
            "INSERT INTO circuit_breaker_state("
            "connector_key, state, consecutive_failures, last_failure_code, "
            "last_failure_at, opened_at, opened_at_epoch, retry_eligible_at_epoch, updated_at"
            ") VALUES (?, ?, 0, NULL, NULL, NULL, NULL, NULL, ?) "
            "ON CONFLICT(connector_key) DO UPDATE SET "
            "state=excluded.state, consecutive_failures=0, updated_at=excluded.updated_at",
            (connector_key, STATE_CLOSED, utc_now_iso()),
        )

    def record_failure(
        self,
        connector_key: str,
        *,
        code: str,
        systemic: bool,
        connection: sqlite3.Connection | None = None,
    ) -> bool:
        """Registra uma falha do connector. Devolve ``True`` somente quando
        o circuito ABRIU agora nesta chamada (``False`` em todos os outros
        casos: falha de conteúdo ignorada, falha sistêmica que ainda não
        atingiu ``failure_threshold``, ou connector já estava ``OPEN``).

        ``systemic=False`` (falha de CONTEÚDO -- ver docstring do módulo)
        NUNCA conta para o contador, mesmo repetida com itens diferentes: é
        um no-op para fins de estado (o circuito não muda por causa dela).

        ``code`` identifica a RAZÃO da falha (ex.: ``"connection_timeout"``,
        ``"auth_expired"``). O contador de consecutivas só acumula quando o
        ``code`` é IDÊNTICO ao da falha sistêmica anterior -- uma falha
        sistêmica com ``code`` diferente da anterior reinicia a contagem em
        1 (ainda sistêmica, mas não é a MESMA razão repetida -- ver Prompt:
        "N consecutive IDENTICAL systemic failures (same code/reason)")."""
        connector_key = _normalize_connector_key(connector_key)
        code = str(code).strip()
        if not code:
            raise ValueError("code não pode ser vazio")
        if connection is not None:
            return self._record_failure_locked(connector_key, code, bool(systemic), connection)
        with self.database.transaction() as conn:
            return self._record_failure_locked(connector_key, code, bool(systemic), conn)

    def _record_failure_locked(
        self, connector_key: str, code: str, systemic: bool, conn: sqlite3.Connection
    ) -> bool:
        if not systemic:
            return False
        row = conn.execute(
            "SELECT * FROM circuit_breaker_state WHERE connector_key = ?",
            (connector_key,),
        ).fetchone()
        now_iso = utc_now_iso()
        if row is not None and row["state"] == STATE_OPEN:
            # Já aberto: só atualiza metadados de diagnóstico (última falha
            # observada), nunca reabre de novo nem estende o cooldown já em
            # curso -- ver módulo, "decisão de NÃO estender cooldown".
            conn.execute(
                "UPDATE circuit_breaker_state SET last_failure_code=?, last_failure_at=?, "
                "updated_at=? WHERE connector_key=?",
                (code, now_iso, now_iso, connector_key),
            )
            return False

        previous_count = int(row["consecutive_failures"]) if row is not None else 0
        previous_code = row["last_failure_code"] if row is not None else None
        new_count = previous_count + 1 if previous_code == code else 1
        opens_now = new_count >= self.failure_threshold

        if opens_now:
            now = self._clock()
            retry_eligible_at_epoch = now + self.cooldown_seconds
            conn.execute(
                "INSERT INTO circuit_breaker_state("
                "connector_key, state, consecutive_failures, last_failure_code, "
                "last_failure_at, opened_at, opened_at_epoch, retry_eligible_at_epoch, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(connector_key) DO UPDATE SET "
                "state=excluded.state, consecutive_failures=excluded.consecutive_failures, "
                "last_failure_code=excluded.last_failure_code, "
                "last_failure_at=excluded.last_failure_at, opened_at=excluded.opened_at, "
                "opened_at_epoch=excluded.opened_at_epoch, "
                "retry_eligible_at_epoch=excluded.retry_eligible_at_epoch, "
                "updated_at=excluded.updated_at",
                (
                    connector_key,
                    STATE_OPEN,
                    new_count,
                    code,
                    now_iso,
                    now_iso,
                    now,
                    retry_eligible_at_epoch,
                    now_iso,
                ),
            )
            self.database.append_audit_event(
                CIRCUIT_BREAKER_OPENED_EVENT,
                entity_type="circuit_breaker",
                data={
                    "connector_key": connector_key,
                    "consecutive_failures": new_count,
                    "failure_threshold": self.failure_threshold,
                    "last_failure_code": code,
                    "cooldown_seconds": self.cooldown_seconds,
                },
                connection=conn,
            )
        else:
            conn.execute(
                "INSERT INTO circuit_breaker_state("
                "connector_key, state, consecutive_failures, last_failure_code, "
                "last_failure_at, opened_at, opened_at_epoch, retry_eligible_at_epoch, updated_at"
                ") VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, ?) "
                "ON CONFLICT(connector_key) DO UPDATE SET "
                "state=excluded.state, consecutive_failures=excluded.consecutive_failures, "
                "last_failure_code=excluded.last_failure_code, "
                "last_failure_at=excluded.last_failure_at, updated_at=excluded.updated_at",
                (connector_key, STATE_CLOSED, new_count, code, now_iso, now_iso),
            )
        return opens_now

    def _close_locked(
        self,
        connector_key: str,
        conn: sqlite3.Connection,
        *,
        opened_at_epoch: float | None,
        now: float,
    ) -> None:
        open_duration_seconds = None
        if opened_at_epoch is not None:
            open_duration_seconds = max(0.0, float(now) - float(opened_at_epoch))
        conn.execute(
            "UPDATE circuit_breaker_state SET state=?, consecutive_failures=0, "
            "last_failure_code=NULL, opened_at=NULL, opened_at_epoch=NULL, "
            "retry_eligible_at_epoch=NULL, updated_at=? WHERE connector_key=?",
            (STATE_CLOSED, utc_now_iso(), connector_key),
        )
        self.database.append_audit_event(
            CIRCUIT_BREAKER_CLOSED_EVENT,
            entity_type="circuit_breaker",
            data={
                "connector_key": connector_key,
                "open_duration_seconds": open_duration_seconds,
            },
            connection=conn,
        )


__all__ = [
    "STATE_CLOSED",
    "STATE_OPEN",
    "DEFAULT_FAILURE_THRESHOLD",
    "DEFAULT_COOLDOWN_SECONDS",
    "CIRCUIT_BREAKER_OPENED_EVENT",
    "CIRCUIT_BREAKER_CLOSED_EVENT",
    "CircuitBreakerError",
    "CircuitBreakerState",
    "CircuitBreaker",
]
