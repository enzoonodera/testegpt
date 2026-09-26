# -*- coding: utf-8 -*-
"""Idempotência de publicação remota (PROMPT 22 -- IDEMPOTÊNCIA).

ESCOPO DESTE PROMPT: exclusivamente Geração 2 (``domain/models.py`` --
``Publication`` ganhou um campo novo --, camada SQLite). NÃO altera
Geração 1 (``agendar_youtube.py``/``agendar_tiktok.py``), NÃO altera
``domain/job_state_machine.py``, ``circuit_breaker.py`` nem
``retry_policy.py`` -- os três já aprovados continuam intocados (hash
confirmado no relatório de entrega). Este módulo é uma camada NOVA sobre
``Publication``, ORTOGONAL às três anteriores: Job individual = RetryPolicy
(Prompt 21); connector = CircuitBreaker (Prompt 20); publicação remota
ÚNICA (nunca duplicar o mesmo upload) = este Prompt. Nenhum dos três é
chamado por este módulo, nem o contrário.

NÃO implementa nenhum Connector real nem chamada de rede/Playwright
(Prompts 57-60 não existem ainda). A "reconciliação" pedida pelo roadmap,
nesta fase, é EXCLUSIVAMENTE sobre ESTADO LOCAL (SQLite): nenhuma chamada
real à plataforma remota existe em Geração 2 hoje, então "verificar
publicação conhecida" significa consultar o que o PRÓPRIO produto já
registrou localmente (uma ``Publication`` com ``remote_id`` preenchido é
prova de sucesso já confirmado; a ausência de qualquer ``Publication``
conhecida, ou uma com ``remote_id`` vazio, é inconclusiva por definição --
nunca resolvida por adivinhação). Um Connector futuro vai CONSUMIR esta
infraestrutura (chamar ``claim()`` antes de publicar, ``mark_published()``
depois de confirmar sucesso remoto de verdade), não o contrário.

INVESTIGAÇÃO PRÉVIA (seção 0) -- confirmada antes de qualquer código
------------------------------------------------------------------------
1. ``Publication`` (``domain/models.py``) já existia com ``video_id``,
   ``account_id``, ``artifact_id``, ``status`` (default ``"PENDING"``),
   ``remote_id``, ``title``, ``description``. A tabela ``publications``
   (``m001_initial.py``) tinha só ``id`` como chave, SEM NENHUMA restrição
   de unicidade sobre ``video_id``/``account_id``/qualquer combinação --
   confirmado lendo o SQL de ``m001_initial.py`` antes de desenhar
   qualquer coisa: nada no schema impedia duas ``Publication``s idênticas
   para o mesmo vídeo na mesma conta.
2. ``Job`` (confirmado nos Prompts 20/21) NÃO tem ``publication_id`` nem
   qualquer FK para ``Publication`` -- e ``Publication`` também não tem
   ``job_id``. As duas entidades hoje NÃO SE CONHECEM, e este Prompt NÃO
   inventa essa FK (mesma decisão já tomada para connector/Job no Prompt
   20): o contrato de idempotência funciona inteiramente em termos dos
   campos que ``Publication`` já tem, nunca de uma relação com ``Job`` que
   ainda não existe.
3. ``JOB_UNKNOWN`` (via ``_SAFE_FALLBACK_STATUS``, Prompt 20/21) é o pouso
   já aprovado para quando um handler falha depois de reivindicar
   ``PUBLISHING``/``RECOVERING`` (efeito remoto potencialmente incerto).
   Confirmado lendo ``job_engine.py``/``recovery_manager.py``: hoje NADA
   reconcilia esse ``UNKNOWN`` automaticamente -- ``RecoveryManager``
   expõe ``jobs_pending_reconciliation()`` e move ``UNKNOWN -> RECOVERING``
   de forma seguríssima, mas NUNCA decide se "esse vídeo já foi publicado
   de verdade ou não" -- essa pergunta é exatamente o que este módulo
   responde, com os dados que já existem localmente. Este módulo não
   integra com ``RecoveryManager``/``Job`` diretamente (não há FK, ver
   item 2 acima) -- um futuro Connector é quem vai ligar as duas pontas
   quando existir.
4. Circuit Breaker (Prompt 20) e RetryPolicy (Prompt 21) decidem "o
   connector está saudável?" e "esse Job ganha nova tentativa?" -- nenhum
   dos dois decide "isso já foi publicado?". Este módulo não duplica nem
   chama nenhum dos dois; ele é consultado ANTES de um handler tentar
   publicar de novo -- a decisão de idempotência vem primeiro; se
   ``NUNCA_TENTADO``, RetryPolicy/CircuitBreaker continuam decidindo o
   resto exatamente como já aprovado.

OS TRÊS RESULTADOS (nunca um booleano único -- roadmap, item 1.3)
------------------------------------------------------------------------
``OUTCOME_NUNCA_TENTADO``: nenhuma ``Publication`` conhecida para esta
chave -- pode publicar.
``OUTCOME_JA_PUBLICADO``: existe uma ``Publication`` conhecida com
``remote_id`` preenchido (prova de sucesso remoto já confirmado) -- NÃO
publicar de novo, devolver a publicação conhecida.
``OUTCOME_INCONCLUSIVO``: existe uma ``Publication`` conhecida MAS sem
``remote_id`` (uma tentativa anterior começou e não se sabe se terminou --
ex.: crash logo após o upload, antes de confirmar o ``remote_id``) -- NÃO
publicar de novo (arriscaria duplicar) E NÃO marcar como sucesso (não há
prova nenhuma). É uma pendência que EXIGE decisão externa -- este módulo
NUNCA resolve isso sozinho por adivinhação, exatamente como um Job
``UNKNOWN`` fica visível e auditável em vez de resolvido silenciosamente.
A reconciliação REAL contra a plataforma remota é trabalho de um Connector
futuro; este módulo só garante que o produto NUNCA finge saber o resultado
quando não sabe.

CHAVE DE IDEMPOTÊNCIA -- SEMPRE EXPLÍCITA, NUNCA ADIVINHADA
------------------------------------------------------------------------
Mesmo princípio já usado em ``systemic=``/``category=`` (Prompts 20/21):
``PublicationIdempotencyGuard`` nunca calcula uma chave sozinho a partir de
heurística nenhuma -- todo método toma ``idempotency_key: str`` como
parâmetro explícito. ``compute_idempotency_key()`` é um HELPER opcional que
oferece a fórmula DEFAULT sugerida pelo roadmap
(``operation + account_id + video_id``, hasheados para um valor estável e
compacto), mas nada obriga seu uso -- uma republicação de
descrição/thumbnail, por exemplo, pode legitimamente precisar de uma
fórmula diferente (ex.: incluir também um hash do conteúdo específico
sendo republicado), e quem chama é livre para compor sua própria chave e
passá-la diretamente a ``claim()``/``check()``.

PERSISTÊNCIA E PROTEÇÃO CONTRA DUPLICAÇÃO SOB CONCORRÊNCIA
------------------------------------------------------------------------
Ver ``m006_publication_idempotency.py`` para a decisão completa (coluna
dedicada em ``publications`` + índice único PARCIAL). A DEFESA REAL contra
duas ``Publication``s para a mesma chave é o índice único do SQLite, nunca
apenas a checagem em Python: ``claim()`` primeiro tenta fechar o TOCTOU por
software (SELECT+INSERT na MESMA ``BEGIN IMMEDIATE``, mesmo padrão já
aprovado em ``CircuitBreaker``/``RetryPolicy``), mas mesmo que esse
software falhasse ou fosse contornado por outro caminho de código (ex.:
alguém inserindo uma ``Publication`` diretamente via
``LocalDatabase.insert`` fora deste módulo), o índice único do banco ainda
assim rejeita a segunda linha -- nunca uma segunda linha silenciosa (ver
``create_publication()``, que expõe esse caminho de baixo nível
propositalmente para ser testado sob concorrência real, e
``PublicationIdempotencyConflictError``, que traduz o
``sqlite3.IntegrityError`` cru em um erro claro e específico deste
domínio).
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import sqlite3

from .domain import Publication
from .storage.database import LocalDatabase
from .time_utils import utc_now_iso


OUTCOME_NUNCA_TENTADO = "NUNCA_TENTADO"
OUTCOME_JA_PUBLICADO = "JA_PUBLICADO"
OUTCOME_INCONCLUSIVO = "INCONCLUSIVO"

IDEMPOTENCY_OUTCOMES = frozenset(
    {OUTCOME_NUNCA_TENTADO, OUTCOME_JA_PUBLICADO, OUTCOME_INCONCLUSIVO}
)


class PublicationIdempotencyError(RuntimeError):
    """Erro de contrato deste módulo."""


class PublicationIdempotencyConflictError(PublicationIdempotencyError):
    """O índice único do banco (``idx_publications_idempotency_key``,
    ``m006_publication_idempotency.py``) rejeitou uma segunda
    ``Publication`` para uma chave de idempotência já em uso -- a última
    linha de defesa contra duplicação sob concorrência (roadmap, item
    1.4) agiu. Nunca uma segunda linha silenciosa: quem chamou
    ``create_publication()`` diretamente (fora do caminho TOCTOU-safe de
    ``claim()``) recebe este erro explícito, nunca um ``sqlite3.
    IntegrityError`` cru nem uma escrita perdida silenciosamente."""


def _normalize_idempotency_key(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("idempotency_key deve ser str")
    normalized = value.strip()
    if not normalized:
        raise ValueError("idempotency_key não pode ser vazia")
    return normalized


def _normalize_component(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} deve ser str")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} não pode ser vazio")
    return normalized


def compute_idempotency_key(*, operation: str, account_id: str, video_id: str) -> str:
    """Fórmula DEFAULT sugerida pelo roadmap: ``video_id + account_id +
    operation``. Puramente opcional -- ver docstring do módulo. Devolve um
    SHA-256 hex (64 chars, estável e compacto) de uma codificação JSON
    canônica dos três componentes, em vez de uma simples concatenação por
    separador: evita qualquer ambiguidade de colisão entre, por exemplo,
    ``operation="A"`` + ``account_id="B:C"`` e ``operation="A:B"`` +
    ``account_id="C"`` que um separador ingênuo (``":"``, ``"|"`` etc.)
    poderia introduzir caso um dos componentes viesse a conter esse
    caractere no futuro."""
    operation = _normalize_component(operation, "operation")
    account_id = _normalize_component(account_id, "account_id")
    video_id = _normalize_component(video_id, "video_id")
    canonical = json.dumps(
        {"operation": operation, "account_id": account_id, "video_id": video_id},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class IdempotencyCheck:
    """Resultado de uma consulta/decisão de idempotência.

    ``publication`` é ``None`` somente quando ``outcome ==
    OUTCOME_NUNCA_TENTADO`` E a consulta foi feita por ``check()``
    (read-only, nunca escreve). Quando o mesmo ``OUTCOME_NUNCA_TENTADO`` é
    devolvido por ``claim()``, ``publication`` é a ``Publication``
    placeholder recém-reservada (``status="PENDING"``, ``remote_id=None``)
    que quem chamou deve usar para a tentativa de publicação real e depois
    atualizar via ``mark_published()``."""

    outcome: str
    publication: Publication | None = None


class PublicationIdempotencyGuard:
    """Autoridade de IDEMPOTÊNCIA de publicação remota (nunca de
    ``Job.status`` nem de admissão de connector -- ver docstring do
    módulo, "ORTOGONAL às três anteriores"). Não é um colaborador opcional
    de ``JobEngine`` (ao contrário de ``CircuitBreaker``/``RetryPolicy`` --
    não há FK Job<->Publication para integrar automaticamente, ver
    investigação item 2): é consultado diretamente por quem for publicar
    (hoje, só testes; no futuro, um Connector real)."""

    def __init__(self, database: LocalDatabase) -> None:
        self.database = database

    # ------------------------------------------------------------------
    # Leitura pura (nunca escreve)
    # ------------------------------------------------------------------

    def check(self, idempotency_key: str, *, connection: sqlite3.Connection | None = None) -> IdempotencyCheck:
        """Etapas (a)+(b)+(c) do roadmap, SOMENTE LEITURA -- nunca reserva
        nem cria nada. ``outcome == OUTCOME_NUNCA_TENTADO`` aqui sempre tem
        ``publication is None`` (diferente de ``claim()``, que reserva)."""
        idempotency_key = _normalize_idempotency_key(idempotency_key)
        if connection is not None:
            return self._check_locked(idempotency_key, connection)
        with self.database.connection() as conn:
            return self._check_locked(idempotency_key, conn)

    def _check_locked(self, idempotency_key: str, conn: sqlite3.Connection) -> IdempotencyCheck:
        row = conn.execute(
            "SELECT id FROM publications WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if row is None:
            return IdempotencyCheck(OUTCOME_NUNCA_TENTADO, None)
        publication = self.database.get(Publication, row["id"], connection=conn)
        if publication is None:
            # Linha existia no momento do SELECT acima mas sumiu antes da
            # decodificação (ex.: apagada por outra transaction entre as
            # duas leituras) -- trata como nunca vista, nunca inventa uma
            # Publication fantasma.
            return IdempotencyCheck(OUTCOME_NUNCA_TENTADO, None)
        if publication.remote_id:
            return IdempotencyCheck(OUTCOME_JA_PUBLICADO, publication)
        return IdempotencyCheck(OUTCOME_INCONCLUSIVO, publication)

    # ------------------------------------------------------------------
    # Decisão "posso publicar?" -- TOCTOU-safe, pode reservar
    # ------------------------------------------------------------------

    def claim(
        self,
        idempotency_key: str,
        *,
        video_id: str | None = None,
        account_id: str | None = None,
        artifact_id: str | None = None,
        title: str | None = None,
        description: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> IdempotencyCheck:
        """O gate real "posso publicar?" (roadmap, item 1.2). Quando o
        resultado é ``OUTCOME_NUNCA_TENTADO``, uma ``Publication``
        placeholder É RESERVADA atomicamente (SELECT+INSERT na mesma
        ``BEGIN IMMEDIATE`` -- mesmo padrão TOCTOU-safe já aprovado em
        ``CircuitBreaker.is_admitted``/``RetryPolicy.record_failure``),
        para que duas chamadas concorrentes com a MESMA chave nunca
        decidam as duas "pode publicar" -- a segunda, dentro da mesma
        transaction serializada pelo SQLite, já vê a linha que a primeira
        acabou de inserir e devolve ``JA_PUBLICADO``/``INCONCLUSIVO``
        (nunca reserva de novo). Quando o resultado é ``JA_PUBLICADO``/
        ``INCONCLUSIVO``, NADA é escrito -- quem chama nunca deve publicar
        de novo nesses dois casos (ver docstring do módulo)."""
        idempotency_key = _normalize_idempotency_key(idempotency_key)
        if connection is not None:
            return self._claim_locked(
                idempotency_key,
                video_id=video_id,
                account_id=account_id,
                artifact_id=artifact_id,
                title=title,
                description=description,
                conn=connection,
            )
        with self.database.transaction() as conn:
            return self._claim_locked(
                idempotency_key,
                video_id=video_id,
                account_id=account_id,
                artifact_id=artifact_id,
                title=title,
                description=description,
                conn=conn,
            )

    def _claim_locked(
        self,
        idempotency_key: str,
        *,
        video_id: str | None,
        account_id: str | None,
        artifact_id: str | None,
        title: str | None,
        description: str | None,
        conn: sqlite3.Connection,
    ) -> IdempotencyCheck:
        existing = self._check_locked(idempotency_key, conn)
        if existing.outcome != OUTCOME_NUNCA_TENTADO:
            return existing
        publication = Publication(
            video_id=video_id,
            account_id=account_id,
            artifact_id=artifact_id,
            status="PENDING",
            remote_id=None,
            title=title,
            description=description,
            idempotency_key=idempotency_key,
        )
        try:
            self.database.insert(publication, connection=conn)
        except sqlite3.IntegrityError as exc:
            if _is_idempotency_unique_violation(exc):
                # Sob BEGIN IMMEDIATE isto é praticamente inatingível (o
                # SELECT acima, na mesma transaction, já veria a linha de
                # qualquer outra chamada de claim() concorrente) -- mas
                # nunca ignorado silenciosamente: um caller externo que
                # tenha inserido uma Publication com esta chave por fora
                # deste método (ex.: create_publication() direto, ou
                # legacy_migration.py) faz este caminho ser alcançável de
                # verdade, e a resposta correta é reportar o conflito, não
                # fingir que a reserva foi bem-sucedida.
                raise PublicationIdempotencyConflictError(
                    f"conflito de idempotência ao reservar chave {idempotency_key!r}: "
                    "já existe uma Publication com esta chave"
                ) from exc
            raise
        return IdempotencyCheck(OUTCOME_NUNCA_TENTADO, publication)

    # ------------------------------------------------------------------
    # Caminho de baixo nível (usado por _claim_locked; também exposto para
    # provar sob concorrência REAL que o índice único do banco é a última
    # linha de defesa -- roadmap, item 1.4)
    # ------------------------------------------------------------------

    def create_publication(
        self,
        idempotency_key: str,
        *,
        video_id: str | None = None,
        account_id: str | None = None,
        artifact_id: str | None = None,
        title: str | None = None,
        description: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> Publication:
        """Insere uma ``Publication`` nova para ``idempotency_key`` SEM
        checar primeiro se já existe uma (ao contrário de ``claim()``).
        Deliberadamente "ingênuo": existe para que testes de concorrência
        possam provar que o ÍNDICE ÚNICO DO BANCO -- não uma checagem em
        Python -- é quem realmente impede duas ``Publication``s para a
        mesma chave quando duas chamadas correm sem a serialização de
        ``claim()`` (ex.: cada uma em sua própria ``BEGIN IMMEDIATE``
        curta, disputando o mesmo ``INSERT``). Levanta
        ``PublicationIdempotencyConflictError`` (nunca deixa o
        ``sqlite3.IntegrityError`` cru escapar, nunca cria uma segunda
        linha silenciosa) quando a chave já está em uso."""
        idempotency_key = _normalize_idempotency_key(idempotency_key)
        publication = Publication(
            video_id=video_id,
            account_id=account_id,
            artifact_id=artifact_id,
            status="PENDING",
            remote_id=None,
            title=title,
            description=description,
            idempotency_key=idempotency_key,
        )
        try:
            self.database.insert(publication, connection=connection)
        except sqlite3.IntegrityError as exc:
            if _is_idempotency_unique_violation(exc):
                raise PublicationIdempotencyConflictError(
                    f"conflito de idempotência ao criar Publication para a chave "
                    f"{idempotency_key!r}: já existe uma Publication com esta chave"
                ) from exc
            raise
        return publication

    # ------------------------------------------------------------------
    # Fechar o ciclo: confirmar sucesso remoto (contrato para um futuro
    # Connector -- nenhuma chamada real de rede acontece aqui)
    # ------------------------------------------------------------------

    def mark_published(
        self,
        publication_id: str,
        *,
        remote_id: str,
        connection: sqlite3.Connection | None = None,
    ) -> Publication:
        """Marca uma ``Publication`` placeholder (reservada por
        ``claim()``/``create_publication()``) como confirmada com sucesso
        remoto -- ``remote_id`` precisa ser a prova real (ex.: o id do
        vídeo devolvido pela plataforma), NUNCA um valor inventado. Este
        método não faz nenhuma chamada de rede: ele só persiste uma prova
        que quem chama (um Connector futuro) já obteve de verdade."""
        remote_id = _normalize_component(remote_id, "remote_id")
        if connection is not None:
            return self._mark_published_locked(publication_id, remote_id, connection)
        with self.database.transaction() as conn:
            return self._mark_published_locked(publication_id, remote_id, conn)

    def _mark_published_locked(
        self, publication_id: str, remote_id: str, conn: sqlite3.Connection
    ) -> Publication:
        publication = self.database.get(Publication, publication_id, connection=conn)
        if publication is None:
            raise PublicationIdempotencyError(
                f"Publication {publication_id} não encontrada"
            )
        publication.remote_id = remote_id
        publication.status = "PUBLISHED"
        publication.touch()
        self.database.save(publication, connection=conn)
        return publication


def _is_idempotency_unique_violation(exc: sqlite3.IntegrityError) -> bool:
    return "idempotency_key" in str(exc)


__all__ = [
    "OUTCOME_NUNCA_TENTADO",
    "OUTCOME_JA_PUBLICADO",
    "OUTCOME_INCONCLUSIVO",
    "IDEMPOTENCY_OUTCOMES",
    "PublicationIdempotencyError",
    "PublicationIdempotencyConflictError",
    "compute_idempotency_key",
    "IdempotencyCheck",
    "PublicationIdempotencyGuard",
]
