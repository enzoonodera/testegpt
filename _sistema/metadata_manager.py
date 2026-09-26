# -*- coding: utf-8 -*-
"""MetadataManager -- Prompt 35 (Fase 5: Editor Modular Não Destrutivo).

TEXTO LITERAL DO ROADMAP (implementado exatamente):

    Crie MetadataManager independente.
    Modos: KEEP; CLEAN; PROFILE.
    KEEP: preservar quando possível.
    CLEAN: remover metadata desnecessária.
    PROFILE: aplicar perfil configurado.
    A ferramenta deve funcionar sozinha em lote.
    Ao preparar/renderizar um projeto que não tenha decisão de metadata,
    mostrar uma escolha simples: Manter informações do arquivo / Limpar
    informações do arquivo.
    Permitir "lembrar minha preferência".
    Perfil avançado pode ficar em Configurações.
    Nunca apresentar metadata como recurso para burlar plataforma.

    [FASE 6 -- bloco "EVIDÊNCIA PARA CATÁLOGO", texto literal]
    Quando MetadataManager concluir CLEAN com resultado validado, registrar
    evidência estruturada suficiente para derivar METADATA_CLEAN. KEEP ou
    PROFILE não podem ser confundidos com CLEAN.

=======================================================================
0. VISÃO GERAL -- DUAS PARTES, MESMA ESTRUTURA DOS PROMPTS 31/33/34
=======================================================================

--- PARTE A -- DECISÃO (sempre incluída) ---
Configuração (``mode`` ∈ {KEEP, CLEAN, PROFILE}, e ``profile_fields``
quando ``mode == PROFILE``), persistida via ``EditProjectManager`` na
categoria ``METADATA_MODE`` (``"metadata_mode"``) -- já reservada como
CONVENIÊNCIA em ``edit_project.py`` desde o Prompt 27, confirmado por
grep nesta rodada que não tinha nenhum consumidor até agora. Mesmo
mecanismo atômico (``update_category``) de sempre. Além disso: mecanismo
de "lembrar minha preferência" (reaproveitando ``LocalDatabase.
set_setting``/``get_setting``/``delete_setting``, tabela ``settings`` já
migrada -- nenhuma tabela nova) e um contrato de "decisão pendente" para
uma camada de UI futura consumir.

--- PARTE B -- PROCESSAMENTO REAL (só para ``mode == CLEAN``) ---
Diferente de TODAS as rodadas anteriores de Parte B (Prompts 31/33/34,
onde a Parte B reaproveitava um motor de análise ainda incerto --
Whisper/detecção visual/detecção de silêncio), aqui a Parte B é a
PRIMEIRA vez que um módulo de Geração 2 efetivamente produz um arquivo de
vídeo novo (uma cópia do ``SourceAsset`` com metadata removida via
``ffmpeg -map_metadata -1``). Isso é DELIBERADO e discutido em detalhe na
seção 0.3 abaixo -- não é adiado como o backend de detecção dos Prompts
33/34, porque aqui a "detecção" não existe: a operação é determinística e
já comprovadamente funcional neste ambiente (a mesma flag já é usada pelo
módulo legado ``limpar_metadados_oficial.py``, Geração 1).

=======================================================================
0.1 ATENÇÃO CRÍTICA -- DISTINÇÃO FRENTE AO MÓDULO LEGADO
    (``_sistema/limpar_metadados_oficial.py``, Geração 1)
=======================================================================

``limpar_metadados_oficial.py`` (821 linhas, CLI standalone via
``argparse``, chamado por ``painel_oficial.py:673``) foi relido por
inteiro nesta rodada. Ele faz DUAS coisas muito além de "limpar
metadados":

    (a) remove metadata do container (``-map_metadata -1
        -map_chapters -1``, título/comentário/artista/copyright/
        descrição em branco) -- isso É equivalente ao que este módulo
        faz no modo ``CLEAN``;
    (b) em seguida FABRICA metadata falsa de captura por smartphone
        (``gerar_metadados_smartphone()``: data aleatória entre
        2024-01-01 e 2026-09-12, GPS aleatório em torno de São Paulo,
        ``creation_time``/``quicktime_creationdate``, ``make: "Apple"``,
        ``model`` aleatório entre "iPhone 13/14/15") e REENCODA o vídeo
        com filtro visual + variação de velocidade (0.99x-1.01x) + CRF
        aleatório, para dificultar detecção de reupload/duplicata.

O item (b) é EXATAMENTE o "recurso para burlar plataforma" que o
roadmap deste Prompt proíbe explicitamente ("Nunca apresentar metadata
como recurso para burlar plataforma") e que o Princípio F/M do
``CLAUDE.md`` já rejeitaria por espírito (não é privacidade/segurança
legítima, é ofuscação deliberada de origem/autenticidade).

DECISÃO OBRIGATÓRIA confirmada e documentada: este módulo (Geração 2)
é INTEIRAMENTE independente -- não importa, não chama, não reaproveita
NENHUMA linha de ``limpar_metadados_oficial.py`` (confirmado por teste
AST dedicado, seção 0.9 abaixo -- mesma disciplina de zero acoplamento
Geração 1 ↔ Geração 2 de todos os módulos anteriores, ver
``edit_project.py`` linhas ~157-163). O backend de produção deste módulo
(seção 0.4):

    - remove/neutraliza metadata EXISTENTE (``-map_metadata -1
      -map_chapters -1`` + campos de texto conhecidos em branco);
    - NUNCA escreve nenhum valor de substituição -- nenhum
      ``creation_time``/GPS/``make``/``model`` fabricado, nenhuma
      variação de velocidade, nenhum CRF aleatório, nenhum filtro
      visual. O vídeo é reempacotado com ``-c copy`` (stream copy, sem
      reencode) -- o CONTEÚDO visual/sonoro nunca é alterado, só o
      metadata do container.

Testado negativamente de forma explícita: nenhum campo de metadata do
arquivo de saída contém um valor nos moldes fabricados pelo módulo
legado (``test_backend_nunca_escreve_metadata_fabricada``).

=======================================================================
0.2 CATEGORIA ``METADATA_MODE`` -- SCHEMA
=======================================================================

    {"mode": "KEEP" | "CLEAN" | "PROFILE",
     "profile_fields": ["title", "comment", ...] | null}

``mode`` é um vocabulário FECHADO de 3 valores (diferente dos campos
numéricos com range dos Prompts 33/34 -- aqui é um enum, validado contra
``METADATA_MODES``, erro estruturado ``VocabularioInvalidoError`` para
valor fora dele).

``profile_fields`` -- o roadmap diz só "aplicar perfil configurado", sem
especificar o que um "perfil" contém. Investigado: nenhum Prompt anterior
define esse schema. LACUNA documentada -- o menor schema defensável a
partir do texto disponível é uma seleção EXPLÍCITA de quais campos de
metadata de texto devem ser removidos (em vez de "tudo", que já é o
``CLEAN``). Vocabulário fechado ``PROFILE_FIELDS`` (os mesmos 5 campos de
texto que ``CLEAN``/o módulo legado tratam -- ``title``/``comment``/
``artist``/``copyright``/``description`` -- nomes padrão de metadata de
container, não lógica do módulo legado) -- extensível sem migration
(campo é só JSON opaco em ``edit_state``), mas fechado por validação para
nunca aceitar um nome arbitrário não revisado. ``profile_fields`` só é
aceito/obrigatório quando ``mode == PROFILE``; essa checagem cruzada é
feita no HANDLER do Job (seção 0.5), nunca em ``set_config`` -- mesma
filosofia de ``edit_project.py`` de que uma categoria armazena campos
individualmente validados, sem impor uma transação de "todos os campos
de uma vez" (todo módulo de Parte A anterior -- 28/29/30/32/33/34 --
segue o mesmo padrão de atualização parcial por campo).

=======================================================================
0.3 DECISÃO DE ARQUITETURA -- ESTE PROMPT TEM PARTE B REAL PARA ``CLEAN``,
    MAS ``PROFILE`` FICA DELIBERADAMENTE ADIADO
=======================================================================

Avaliadas as duas leituras propostas pelo Prompt:

    (a) Parte A + Parte B real para CLEAN (e potencialmente PROFILE);
    (b) só Parte A, com toda a Parte B deferida.

DECISÃO: um meio-termo justificado, não uma cópia de nenhuma resposta
anterior. Para ``CLEAN``: Parte B real, HOJE -- a operação
(``-map_metadata -1``) é determinística, sem ambiguidade de "qual
algoritmo", e já comprovadamente funcional neste ambiente (é a mesma
flag que o módulo legado usa em produção há tempo) -- ao contrário dos
Prompts 33/34, não há aqui nenhuma "biblioteca de detecção ainda não
disponível" a adiar; adiar processamento real para ``CLEAN`` seria adiar
sem motivo técnico, só para "ficar parecido com os Prompts anteriores"
-- overengineering pelo lado errado (CLAUDE.md: "não fazer
overengineering", incluindo evitar trabalho postergado sem necessidade
demonstrada). Para ``PROFILE``: Parte B real fica ADIADA -- o roadmap
não especifica NENHUM campo do que um "perfil configurado" contém além
da frase em si (diferente de ``CLEAN``, que o próprio roadmap já define
operacionalmente: "remover metadata desnecessária" = tudo). Implementar
processamento real para um schema que este mesmo módulo teve que
INVENTAR (seção 0.2) arriscaria consolidar uma decisão de produto que
"Perfil avançado pode ficar em Configurações" sugere ser responsabilidade
de uma camada futura dedicada -- mesmo espírito da deferência de backend
dos Prompts 33/34 (aqui a incerteza não é "qual algoritmo", é "qual
schema de produto"). O Job de ``PROFILE`` hoje sempre completa
``JOB_READY`` sem processar nada (``data={"skipped": True, "reason":
"profile_processing_deferred"}``) -- nunca falha, nunca escreve
Artifact/checkpoint algum. Isso automaticamente satisfaz a exigência
literal do roadmap ("KEEP ou PROFILE não podem ser confundidos com
CLEAN"): como nenhum dos dois grava QUALQUER evidência hoje, é
estruturalmente impossível confundi-los com ``CLEAN`` -- não é preciso
nenhuma lógica extra de distinção porque não há nada a distinguir ainda.
Uma rodada futura que implemente processamento real de ``PROFILE`` deve
usar um ``Artifact.kind`` PRÓPRIO e DIFERENTE de
``ARTIFACT_KIND_METADATA_CLEAN_OUTPUT`` (nunca reaproveitado) -- exigido
aqui como restrição de design para essa rodada futura, análogo a como
este módulo evita reaproveitar ``CHECKPOINT_MEDIA_PROCESSED``.

"A ferramenta deve funcionar sozinha em lote" É satisfeito hoje: a
decisão (Parte A) já se aplica a N vídeos de uma vez (cada Project tem
sua própria categoria ``METADATA_MODE`` independente) e o processamento
real de ``CLEAN`` (Parte B) já roda via ``JobEngine`` -- o mesmo motor
que já processa lotes de qualquer tamanho para todos os módulos
anteriores (Princípio C do CLAUDE.md, já resolvido estruturalmente pelo
``JobEngine``/``ResourceManager`` existentes, nada readicionado aqui).

=======================================================================
0.4 BACKEND DE REMOÇÃO DE METADATA -- INFRAESTRUTURA ISOLADA
=======================================================================

Mesma disciplina de "nunca chamar ``subprocess``/``ffmpeg`` direto de
dentro de um método sem isolar via backend injetável" já usada em todo
Prompt de Parte B anterior. Diferença desta rodada: pela primeira vez o
backend padrão de produção (``_default_ffmpeg_strip_backend``) REALMENTE
invoca ``ffmpeg`` (as Partes B anteriores usavam stubs puros -- nenhum
dos Prompts 31/33/34 tem hoje um backend padrão que chama ``ffmpeg``
de verdade para PRODUZIR arquivo; só ``media_probe.py``, Prompt 26, já
invocava ``ffprobe``/``ffmpeg`` -- sempre em modo LEITURA, nunca
escrita). ``MetadataStripBackend = Callable[[str, str], None]``
(``source_path``, ``dest_path``) -- escreve a cópia processada em
``dest_path``, levanta ``MetadataBackendError`` em qualquer falha.
Comando do backend padrão::

    ffmpeg -y -hide_banner -loglevel error -i <source> -c copy
           -map_metadata -1 -map_chapters -1
           -metadata title= -metadata comment= -metadata artist=
           -metadata copyright= -metadata description=
           <dest>

``-c copy`` (stream copy, sem reencode -- nunca altera pixel/áudio,
nunca introduz variação de velocidade/qualidade, ao contrário do módulo
legado). Testado com vídeo real gerado via ``ffmpeg`` (mesmo padrão de
``test_auto_reframe.py``/``test_media_probe.py``) só no teste dedicado
ao backend padrão -- todo outro teste injeta um backend FAKE
determinístico (nunca ffmpeg real em caminho de decisão de Job, mesmo
espírito dos Prompts 31/33/34).

=======================================================================
0.5 FALHA DO BACKEND SEMPRE FALHA O JOB -- DECISÃO DIFERENTE DO
    PROMPT 34, JUSTIFICADA
=======================================================================

Prompt 34 (SilenceRemoval): falha do detector NUNCA falha o Job -- vira
"nenhum silêncio encontrado", um resultado vazio SEGURO e honesto (a
ferramenta não prometeu encontrar silêncio, só prometeu tentar). Este
Prompt é estruturalmente diferente: ``CLEAN`` PROMETE remover metadata.
Se o backend falhar (``ffmpeg`` ausente, timeout, erro de processo) e o
Job ainda assim terminasse ``JOB_READY`` sem produzir nada, o resultado
seria uma MENTIRA operacional -- o badge futuro ``METADATA_CLEAN``
acenderia (ou o usuário acreditaria que acendeu) para um vídeo cujo
metadata original está intacto. Não existe aqui um "resultado vazio
seguro" análogo ao de silêncio. DECISÃO: falha do backend SEMPRE falha o
Job (``JOB_FAILED``, ``reason="metadata_backend_failed"``) -- nunca um
fallback silencioso. Nenhum texto bruto de exceção é persistido (Item 10
do GATE ADVERSARIAL) -- só o motivo estruturado fixo.

=======================================================================
0.6 CHECKPOINT REAPROVEITADO -- POR QUE ``COMPOSED`` (NÃO ``VALIDATED``)
=======================================================================

Investigado antes de decidir: ``domain/checkpoints.py`` não reserva
significado a nenhum checkpoint além do nome (deliberado, ver sua própria
docstring -- "cada operação decide, na prática, quais etapas percorre").
Confirmado por grep nesta rodada: hoje só ``CHECKPOINT_MEDIA_PROCESSED``
(``auto_reframe.py``), ``CHECKPOINT_TRANSCRIBED``
(``captions_engine.py``) e ``CHECKPOINT_EDIT_PLANNED``
(``silence_removal.py``) têm gravador real -- ``IMPORTED``, ``ANALYZED``,
``COMPOSED``, ``RENDERED``, ``VALIDATED``, ``UPLOAD_STARTED``,
``REMOTE_CONFIRMED`` estão todos livres hoje.

``CHECKPOINT_VALIDATED`` é a escolha ÓBVIA à primeira vista -- o texto do
roadmap literalmente diz "resultado validado". DELIBERADAMENTE EVITADA.
Motivo: ``media_catalog.py`` já reserva ``BADGE_VALIDATED`` (hardcoded
``"0"``) para um significado FUTURO e DIFERENTE -- uma validação de
qualidade/QA mais ampla do conteúdo, não "a limpeza de metadata
terminou". "Resultado validado" no texto do roadmap deste Prompt, lido em
contexto, significa só "a operação terminou com sucesso verificado" (um
sinônimo de ``JOB_READY``), não uma referência à futura feature de
validação. Gravar ``CHECKPOINT_VALIDATED`` aqui criaria uma colisão de
nome DIRETA e previsível (não uma coincidência hipotética como o risco já
aceito para ``MEDIA_PROCESSED``/``REFRAMED_9_16``): quando um futuro
módulo de Validação real for implementado, ele quase certamente vai
querer gravar exatamente ``CHECKPOINT_VALIDATED`` para acender
``BADGE_VALIDATED`` -- e um leitor futuro encontraria ESTE módulo já
gravando o mesmo checkpoint por um motivo completamente não relacionado,
exatamente o tipo de confusão que a regra "checkpoint sozinho nunca
basta, sempre exigir também o Artifact" (seção 0.8 de
``media_catalog.py``) já neutraliza FUNCIONALMENTE -- mas não
neutraliza a confusão para quem LÊ o código depois. Por isso: evitada por
clareza, não por necessidade funcional.

``RENDERED``/``IMPORTED``/``UPLOAD_STARTED``/``REMOTE_CONFIRMED`` também
descartados: ``RENDERED`` tem o mesmo problema de ``VALIDATED``
(``BADGE_RENDERED`` já reservado para um futuro Render Engine real, que
também vai querer esse checkpoint); ``IMPORTED``/``UPLOAD_STARTED``/
``REMOTE_CONFIRMED`` têm forte conotação de evento específico de OUTRO
pipeline (importação/publicação), semanticamente errados para "uma cópia
com metadata limpo foi produzida". Entre as duas opções restantes sem
nome de badge colidindo e sem conotação de evento específico
(``ANALYZED``/``COMPOSED``): ``ANALYZED`` sugere leitura/inspeção
(análise de conteúdo, Smart Clip), o oposto de uma operação de
ESCRITA/transformação; ``COMPOSED`` ("montar/preparar a saída final") é o
encaixe mais direto para "uma cópia processada do arquivo foi montada" --
nenhum badge chamado ``COMPOSED`` existe hoje em ``media_catalog.py``
(confirmado por leitura de ``SYSTEM_BADGES``). DECISÃO:
``CHECKPOINT_COMPOSED``. Honestamente: nenhum checkpoint do vocabulário
fechado foi desenhado pensando em "limpeza de metadata" -- esta é a
melhor correspondência disponível, escolhida por eliminação e
documentada por escrito, exatamente como este Prompt exige. Mesma regra
geral já estabelecida continua valendo aqui: uma futura rodada de Catalog
Wiring para ``METADATA_CLEAN`` deve exigir o checkpoint ``COMPOSED`` E o
``Artifact`` ``metadata_clean_output`` juntos -- nunca o checkpoint
sozinho (seção 0.8 de ``media_catalog.py``, mesmo padrão).

=======================================================================
0.7 NUNCA TOCA ``SourceAsset.local_path`` -- SEMPRE UMA CÓPIA GERENCIADA
=======================================================================

Confirmado por investigação: nenhum módulo de Geração 2 produz hoje um
Render Engine/arquivo final publicável -- o único arquivo de vídeo real
disponível é ``SourceAsset.local_path`` (o ORIGINAL importado), e a
arquitetura inteira é não-destrutiva (Princípio B do CLAUDE.md,
``edit_project.py`` nunca toca o arquivo original). Este módulo processa
SEMPRE uma CÓPIA: ``StorageManager.allocate_temp(suffix=<extensão do
original>, create=True)`` + o backend escreve nela + ``promote_to_final``
publica em::

    <paths.projects>/metadata_clean/<video_id>/<cache_key><extensão>

Mesmo padrão de ``auto_reframe.py``/``captions_engine.py``/
``silence_removal.py`` -- nenhum campo novo em ``AppPaths``, nenhuma
criação manual de diretório (``promote_to_final`` já cria o destino).
Escolhido por VIDEO_ID (não ``project_id``) -- a limpeza de metadata é
uma propriedade do CONTEÚDO do vídeo, não de um Project de edição
específico (mesmo raciocínio de cache de ``captions_engine.py`` seção
0.2). Teste dedicado prova que o arquivo ORIGINAL nunca muda (bytes E
mtime comparados antes/depois).

=======================================================================
0.8 CACHE
=======================================================================

Mesmo padrão dos Prompts 31/33/34: ``compute_cache_key(source_fingerprint,
mode)`` -- SHA-256 hex do JSON canônico (``sort_keys=True``,
reimplementado aqui, nunca importado de módulo irmão) de
``{"source_fingerprint": ..., "mode": "CLEAN"}``. Um ``Artifact``
``metadata_clean_output`` existente para o mesmo ``(video_id,
cache_key)`` é reaproveitado sem reprocessar -- útil porque ``mode ==
CLEAN`` sempre produz o MESMO resultado determinístico para a mesma
origem (não há parâmetro variável hoje, mas o cache key já inclui
``mode`` para robustez futura caso ``CLEAN`` ganhe variações).

=======================================================================
0.9 ORTOGONALIDADE E ESCOPO
=======================================================================

Este módulo nunca importa nem chama ``circuit_breaker``/``retry_policy``/
``publication_idempotency``/``secrets_manager``/``domain.job_state_machine``
diretamente, nunca importa NADA de Geração 1 (confirmado por teste AST
dedicado -- nenhum ``import`` de ``limpar_metadados_oficial``), nunca
importa nenhum módulo irmão de decisão paralela (``captions_style.py``/
``visual_editor.py``/``audio_engine.py``/``auto_reframe.py``/
``silence_removal.py``/``captions_engine.py``), nunca cria
``Video``/``Publication``/``Schedule``, nunca chama
``subprocess``/``ffmpeg`` fora do backend isolado (``_default_ffmpeg_
strip_backend``), e não toca ``_sistema/media_catalog.py`` -- o badge
``BADGE_METADATA_CLEAN`` permanece hardcoded ``"0"`` (confirmado por
leitura direta nesta rodada); destravá-lo é trabalho de uma rodada FUTURA
de Catalog Wiring dedicada, mesmo padrão já usado para
CAPTIONS/TRANSCRIBED/REFRAMED_9_16. Nenhuma migration nova -- reaproveita
a tabela ``settings`` já migrada.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, ClassVar, Mapping, Sequence

from .app_paths import AppPaths
from .control_manager import ControlManager
from .domain import (
    Artifact,
    CHECKPOINT_COMPOSED,
    Job,
    JOB_CANCELLED,
    JOB_FAILED,
    JOB_READY,
    SourceAsset,
    Video,
)
from .edit_project import EditProjectManager, METADATA_MODE, ProjectNaoEncontradoError
from .job_engine import JobStepResult
from .storage.audit import AUDIT_JOB_PROCESSING_COMPLETED, OperationalAuditLog
from .storage.database import LocalDatabase
from .storage_manager import StorageManager

__all__ = [
    "MODE_KEEP",
    "MODE_CLEAN",
    "MODE_PROFILE",
    "METADATA_MODES",
    "PROFILE_FIELDS",
    "OPERATION_APPLY_METADATA_MODE",
    "ARTIFACT_KIND_METADATA_CLEAN_OUTPUT",
    "DETECTOR_BACKEND_VERSION",
    "MetadataManagerError",
    "CampoInvalidoError",
    "VocabularioInvalidoError",
    "MetadataBackendError",
    "MetadataBackendUnavailableError",
    "MetadataModeConfigState",
    "MetadataDecisionState",
    "MetadataStripBackend",
    "compute_cache_key",
    "MetadataManager",
]

# ---------------------------------------------------------------------
# Vocabulário -- Parte A
# ---------------------------------------------------------------------

MODE_KEEP = "KEEP"
MODE_CLEAN = "CLEAN"
MODE_PROFILE = "PROFILE"
METADATA_MODES = frozenset({MODE_KEEP, MODE_CLEAN, MODE_PROFILE})

# Campos de metadata de TEXTO conhecidos (nomes padrão de container,
# reaproveitados só como VOCABULÁRIO -- nunca a lógica do módulo legado).
PROFILE_FIELDS = frozenset({"title", "comment", "artist", "copyright", "description"})

OPERATION_APPLY_METADATA_MODE = "APPLY_METADATA_MODE"

ARTIFACT_KIND_METADATA_CLEAN_OUTPUT = "metadata_clean_output"

DETECTOR_BACKEND_VERSION = "v1"

_FFMPEG_TIMEOUT_SECONDS = 120.0

_SETTINGS_KEY_REMEMBERED_PREFERENCE = "metadata_manager:remembered_preference"


# ---------------------------------------------------------------------
# Erros estruturados
# ---------------------------------------------------------------------

class MetadataManagerError(RuntimeError):
    """Classe base de todos os erros deste módulo."""

    code: "ClassVar[str]" = "METADATA_MANAGER_ERRO"


class CampoInvalidoError(MetadataManagerError):
    """Um campo de configuração está fora do formato/vocabulário aceito."""

    code: "ClassVar[str]" = "CAMPO_INVALIDO"


class VocabularioInvalidoError(MetadataManagerError):
    """Um campo de vocabulário fechado recebeu um valor fora dele."""

    code: "ClassVar[str]" = "VOCABULARIO_INVALIDO"


class MetadataBackendError(MetadataManagerError):
    """O backend de remoção de metadata falhou (processo, timeout etc.)."""

    code: "ClassVar[str]" = "BACKEND_FALHOU"


class MetadataBackendUnavailableError(MetadataBackendError):
    """``ffmpeg`` não está disponível neste ambiente."""

    code: "ClassVar[str]" = "BACKEND_INDISPONIVEL"


# ---------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------

@dataclass(frozen=True)
class MetadataModeConfigState:
    mode: "str | None" = None
    profile_fields: "tuple[str, ...] | None" = None


@dataclass(frozen=True)
class MetadataDecisionState:
    """Contrato para uma camada de UI FUTURA consumir (este Prompt não
    implementa UI) -- "ao preparar/renderizar um projeto que não tenha
    decisão de metadata, mostrar uma escolha simples" do roadmap."""

    has_decision: bool
    current_mode: "str | None"
    current_profile_fields: "tuple[str, ...] | None"
    suggested_mode: "str | None"
    suggested_profile_fields: "tuple[str, ...] | None"


# Backend de remoção de metadata injetável: (source_path, dest_path) ->
# None, escreve a cópia processada em dest_path. Levanta
# MetadataBackendError em qualquer falha. Nunca chamado diretamente pelos
# testes deste módulo com o backend real, exceto no teste dedicado ao
# backend padrão -- ver seção 0.4 da docstring do módulo.
MetadataStripBackend = Callable[[str, str], None]


# ---------------------------------------------------------------------
# Helpers -- cache key (canônico, reimplementado -- nunca importado de
# módulo irmão, zero acoplamento)
# ---------------------------------------------------------------------

def _canonical_json(data: Mapping[str, Any]) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_cache_key(source_fingerprint: str, mode: str) -> str:
    payload = {"source_fingerprint": str(source_fingerprint), "mode": str(mode)}
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------
# Validação -- Parte A
# ---------------------------------------------------------------------

def _validate_mode(value: Any) -> str:
    if not isinstance(value, str) or value not in METADATA_MODES:
        raise VocabularioInvalidoError(
            f"mode inválido: {value!r}; use um de {sorted(METADATA_MODES)}"
        )
    return value


def _validate_profile_fields(value: Any) -> "tuple[str, ...]":
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple, set, frozenset)):
        raise CampoInvalidoError(
            f"profile_fields deve ser uma lista/tupla de strings (recebido: {value!r})"
        )
    if not value:
        raise CampoInvalidoError("profile_fields não pode ser vazio")
    normalized: "list[str]" = []
    for item in value:
        if not isinstance(item, str) or item not in PROFILE_FIELDS:
            raise VocabularioInvalidoError(
                f"campo de perfil inválido: {item!r}; use um de {sorted(PROFILE_FIELDS)}"
            )
        normalized.append(item)
    return tuple(sorted(set(normalized)))


_FIELD_VALIDATORS = {
    "mode": _validate_mode,
    "profile_fields": _validate_profile_fields,
}


def _validate_config_fields(fields: Mapping[str, Any]) -> "dict[str, Any]":
    validated: "dict[str, Any]" = {}
    for key, value in fields.items():
        validator = _FIELD_VALIDATORS.get(key)
        if validator is None:
            raise CampoInvalidoError(
                f"campo de configuração desconhecido: {key!r}; use um de {sorted(_FIELD_VALIDATORS)}"
            )
        validated[key] = validator(value)
    return validated


# ---------------------------------------------------------------------
# Backend padrão de produção -- ffmpeg real, isolado
# ---------------------------------------------------------------------

def _default_ffmpeg_strip_backend(source_path: str, dest_path: str) -> None:
    """Remove metadata via ``ffmpeg -map_metadata -1`` -- stream copy
    (``-c copy``, sem reencode), NUNCA escreve nenhum valor de
    substituição (ver seção 0.1/0.4 da docstring do módulo -- o
    antipadrão deliberadamente evitado é ``limpar_metadados_oficial.py``,
    que fabrica metadata falsa de smartphone e reencoda com variações
    aleatórias)."""
    import shutil
    import subprocess

    if shutil.which("ffmpeg") is None:
        raise MetadataBackendUnavailableError("ffmpeg não encontrado no PATH")

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", source_path,
        "-c", "copy",
        "-map_metadata", "-1",
        "-map_chapters", "-1",
        "-metadata", "title=",
        "-metadata", "comment=",
        "-metadata", "artist=",
        "-metadata", "copyright=",
        "-metadata", "description=",
        dest_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=_FFMPEG_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        raise MetadataBackendError("timeout ao executar ffmpeg") from None
    except OSError:
        raise MetadataBackendError("falha ao iniciar o processo ffmpeg") from None
    if result.returncode != 0:
        raise MetadataBackendError(f"ffmpeg terminou com código {result.returncode}")


# ---------------------------------------------------------------------
# MetadataManager
# ---------------------------------------------------------------------

class MetadataManager:
    """Parte A (configuração + preferência lembrada) + Parte B (remoção
    real de metadata para ``mode == CLEAN``) -- ver seção 0 da docstring
    do módulo para o contrato completo."""

    OPERATION: "ClassVar[str]" = OPERATION_APPLY_METADATA_MODE

    def __init__(
        self,
        manager: EditProjectManager,
        *,
        database: LocalDatabase,
        storage_manager: StorageManager,
        app_paths: AppPaths,
        audit_log: "OperationalAuditLog | None" = None,
        strip_backend: "MetadataStripBackend | None" = None,
        control_manager: "ControlManager | None" = None,
    ) -> None:
        if not isinstance(manager, EditProjectManager):
            raise TypeError(f"manager deve ser EditProjectManager (recebido: {type(manager)!r})")
        if not isinstance(database, LocalDatabase):
            raise TypeError(f"database deve ser LocalDatabase (recebido: {type(database)!r})")
        if not isinstance(storage_manager, StorageManager):
            raise TypeError(
                f"storage_manager deve ser StorageManager (recebido: {type(storage_manager)!r})"
            )
        if not isinstance(app_paths, AppPaths):
            raise TypeError(f"app_paths deve ser AppPaths (recebido: {type(app_paths)!r})")
        if strip_backend is not None and not callable(strip_backend):
            raise TypeError("strip_backend deve ser chamável")
        if control_manager is not None and not isinstance(control_manager, ControlManager):
            raise TypeError(
                f"control_manager deve ser ControlManager (recebido: {type(control_manager)!r})"
            )

        self._manager = manager
        self._database = database
        self._storage = storage_manager
        self._app_paths = app_paths
        self._audit_log = audit_log if audit_log is not None else OperationalAuditLog(database)
        self._backend: MetadataStripBackend = strip_backend or _default_ffmpeg_strip_backend
        self._control_manager = control_manager

    # ===================================================================
    # PARTE A -- configuração
    # ===================================================================

    def set_config(self, project_id: str, **fields: Any) -> MetadataModeConfigState:
        validated = _validate_config_fields(fields)

        def mutator(current_data: Any) -> dict:
            merged = dict(current_data) if isinstance(current_data, Mapping) else {}
            merged.update(validated)
            return merged

        try:
            self._manager.update_category(project_id, METADATA_MODE, mutator)
        except ProjectNaoEncontradoError:
            raise
        except ValueError as exc:
            raise CampoInvalidoError(f"project_id inválido: {project_id!r}") from exc
        return self.get_config(project_id)

    def get_config(self, project_id: str) -> MetadataModeConfigState:
        try:
            state = self._manager.get_category(project_id, METADATA_MODE)
        except ValueError as exc:
            raise CampoInvalidoError(f"project_id inválido: {project_id!r}") from exc
        if state is None:
            return MetadataModeConfigState()
        data = state.data if isinstance(state.data, Mapping) else {}

        def _read(name: str, validator) -> Any:
            value = data.get(name)
            if value is None:
                return None
            try:
                return validator(value)
            except MetadataManagerError as exc:
                raise CampoInvalidoError(
                    f"{name} persistido é inválido (recebido: {value!r})"
                ) from exc

        return MetadataModeConfigState(
            mode=_read("mode", _validate_mode),
            profile_fields=_read("profile_fields", _validate_profile_fields),
        )

    # -- "lembrar minha preferência" ------------------------------------
    #
    # Escopo: instalação (única) -- investigado nesta rodada: não existe
    # conceito de multi-usuário no banco local hoje (``Account`` é conta
    # de PUBLICAÇÃO/plataforma, não usuário da aplicação). Uma única
    # chave global em ``settings``, mesmo mecanismo já existente
    # (``LocalDatabase.set_setting``/``get_setting``/``delete_setting``),
    # namespaced no mesmo espírito de ``ControlManager._key`` (prefixo
    # fixo do módulo + nome do flag).

    def remember_preference(
        self, mode: str, *, profile_fields: "Sequence[str] | None" = None
    ) -> MetadataModeConfigState:
        validated_mode = _validate_mode(mode)
        validated_fields = (
            _validate_profile_fields(profile_fields) if profile_fields is not None else None
        )
        if validated_mode == MODE_PROFILE and not validated_fields:
            raise CampoInvalidoError("profile_fields é obrigatório para lembrar uma preferência PROFILE")
        if validated_mode != MODE_PROFILE and validated_fields:
            raise CampoInvalidoError("profile_fields só é aceito quando mode == PROFILE")

        payload = {
            "mode": validated_mode,
            "profile_fields": list(validated_fields) if validated_fields else None,
        }
        self._database.set_setting(_SETTINGS_KEY_REMEMBERED_PREFERENCE, payload)
        return MetadataModeConfigState(mode=validated_mode, profile_fields=validated_fields)

    def get_remembered_preference(self) -> "MetadataModeConfigState | None":
        raw = self._database.get_setting(_SETTINGS_KEY_REMEMBERED_PREFERENCE, default=None)
        if raw is None or not isinstance(raw, Mapping):
            return None
        mode_raw = raw.get("mode")
        if mode_raw is None:
            return None
        try:
            mode = _validate_mode(mode_raw)
            fields_raw = raw.get("profile_fields")
            fields = _validate_profile_fields(fields_raw) if fields_raw is not None else None
        except MetadataManagerError as exc:
            raise CampoInvalidoError("preferência lembrada persistida é inválida") from exc
        return MetadataModeConfigState(mode=mode, profile_fields=fields)

    def forget_remembered_preference(self) -> bool:
        return self._database.delete_setting(_SETTINGS_KEY_REMEMBERED_PREFERENCE)

    # -- contrato para UI futura -----------------------------------------

    def get_pending_decision(self, project_id: str) -> MetadataDecisionState:
        config = self.get_config(project_id)
        remembered = self.get_remembered_preference()
        return MetadataDecisionState(
            has_decision=config.mode is not None,
            current_mode=config.mode,
            current_profile_fields=config.profile_fields,
            suggested_mode=remembered.mode if remembered else None,
            suggested_profile_fields=remembered.profile_fields if remembered else None,
        )

    # ===================================================================
    # PARTE B -- handler de Job (remoção real de metadata, só CLEAN)
    # ===================================================================

    def handle_metadata_job(self, job: Job) -> JobStepResult:
        if job.video_id is None:
            return JobStepResult(target_status=JOB_FAILED, data={"reason": "job_missing_video_id"})
        if job.project_id is None:
            return JobStepResult(target_status=JOB_FAILED, data={"reason": "job_missing_project_id"})

        try:
            config = self.get_config(job.project_id)
        except ProjectNaoEncontradoError:
            return JobStepResult(
                target_status=JOB_FAILED,
                data={"reason": "project_not_found", "project_id": job.project_id},
            )
        if config.mode is None:
            return JobStepResult(target_status=JOB_FAILED, data={"reason": "metadata_mode_not_configured"})

        if config.mode == MODE_KEEP:
            # KEEP nunca processa -- nunca grava Artifact/checkpoint algum
            # (ver seção 0.3: estruturalmente impossível confundir com
            # CLEAN porque nenhuma evidência é produzida).
            return JobStepResult(
                target_status=JOB_READY,
                data={"mode": MODE_KEEP, "skipped": True, "reason": "keep_never_processes"},
            )
        if config.mode == MODE_PROFILE:
            # PROFILE: processamento real deliberadamente adiado (seção
            # 0.3) -- nunca grava Artifact/checkpoint algum hoje.
            return JobStepResult(
                target_status=JOB_READY,
                data={"mode": MODE_PROFILE, "skipped": True, "reason": "profile_processing_deferred"},
            )

        # mode == CLEAN -- processamento real.
        video = self._database.get(Video, job.video_id)
        if video is None or video.source_asset_id is None:
            return JobStepResult(
                target_status=JOB_FAILED,
                data={"reason": "video_or_source_asset_id_missing", "video_id": job.video_id},
            )
        source = self._database.get(SourceAsset, video.source_asset_id)
        if source is None or not source.local_path:
            return JobStepResult(
                target_status=JOB_FAILED, data={"reason": "source_asset_missing_or_no_local_path"}
            )
        if not source.fingerprint:
            return JobStepResult(
                target_status=JOB_FAILED, data={"reason": "source_asset_missing_fingerprint"}
            )

        local_path = str(source.local_path)
        try:
            valid_source = os.path.isfile(local_path) and os.path.getsize(local_path) > 0
        except OSError:
            valid_source = False
        if not valid_source:
            return JobStepResult(
                target_status=JOB_FAILED, data={"reason": "source_file_missing_or_empty"}
            )

        cache_key = compute_cache_key(source.fingerprint, MODE_CLEAN)

        if self._cancel_requested(job.id):
            return JobStepResult(
                target_status=JOB_CANCELLED, data={"reason": "cancelled_before_processing"}
            )

        cached_artifact = self._find_cached_artifact(job.video_id, cache_key)
        if cached_artifact is not None:
            self._audit_log.record_checkpoint(
                job.id, CHECKPOINT_COMPOSED, data={"cache_hit": True, "cache_key": cache_key, "mode": MODE_CLEAN}
            )
            return JobStepResult(
                target_status=JOB_READY,
                semantic_event=AUDIT_JOB_PROCESSING_COMPLETED,
                data={
                    "mode": MODE_CLEAN,
                    "cache_hit": True,
                    "cache_key": cache_key,
                    "reused_artifact_id": cached_artifact.id,
                },
            )

        suffix = Path(local_path).suffix or ".mp4"
        temp_path = self._storage.allocate_temp(suffix=suffix, create=True)
        try:
            self._backend(local_path, str(temp_path))
        except MetadataBackendError:
            temp_path.unlink(missing_ok=True)
            return JobStepResult(
                target_status=JOB_FAILED, data={"reason": "metadata_backend_failed"}
            )
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise

        if self._cancel_requested(job.id):
            temp_path.unlink(missing_ok=True)
            return JobStepResult(
                target_status=JOB_CANCELLED, data={"reason": "cancelled_after_processing"}
            )

        final_path = self._final_path_for(job.video_id, cache_key, suffix)
        try:
            promoted = self._storage.promote_to_final(temp_path, final_path, overwrite=True)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise

        artifact = Artifact(
            video_id=job.video_id,
            project_id=job.project_id,
            job_id=job.id,
            kind=ARTIFACT_KIND_METADATA_CLEAN_OUTPUT,
            path=str(promoted),
            fingerprint=cache_key,
            size_bytes=os.path.getsize(promoted),
        )
        self._database.insert(artifact)

        self._audit_log.record_checkpoint(
            job.id, CHECKPOINT_COMPOSED, data={"cache_hit": False, "cache_key": cache_key, "mode": MODE_CLEAN}
        )
        return JobStepResult(
            target_status=JOB_READY,
            semantic_event=AUDIT_JOB_PROCESSING_COMPLETED,
            data={
                "mode": MODE_CLEAN,
                "cache_hit": False,
                "cache_key": cache_key,
                "artifact_id": artifact.id,
            },
        )

    # -- helpers internos de Parte B ------------------------------------

    def _cancel_requested(self, job_id: str) -> bool:
        if self._control_manager is None:
            return False
        return self._control_manager.is_job_cancel_requested(job_id)

    def _find_cached_artifact(self, video_id: str, cache_key: str) -> "Artifact | None":
        with self._database.connection() as conn:
            row = conn.execute(
                "SELECT id FROM artifacts WHERE video_id = ? AND kind = ? AND fingerprint = ? "
                "ORDER BY created_at LIMIT 1",
                (video_id, ARTIFACT_KIND_METADATA_CLEAN_OUTPUT, cache_key),
            ).fetchone()
        if row is None:
            return None
        return self._database.get(Artifact, row[0])

    def _final_path_for(self, video_id: str, cache_key: str, suffix: str) -> Path:
        return Path(self._app_paths.projects) / "metadata_clean" / video_id / f"{cache_key}{suffix}"
