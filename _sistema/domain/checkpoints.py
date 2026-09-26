# -*- coding: utf-8 -*-
"""Vocabulário de checkpoints de pipeline (FASE 3 / PROMPT 13).

Um checkpoint marca uma fronteira segura de progresso *dentro* da execução
de um ``Job`` — uma etapa cara (transcrição, render, etc.) que, uma vez
concluída e registrada, não deveria ser refeita desnecessariamente só
porque o processo caiu no meio do trabalho seguinte.

Isso é deliberadamente independente do ``status`` de ``Job`` (State Machine
central definida em ``job_state_machine.py``): um Job pode passar por vários
checkpoints inteiros sem que seu status mude (ex.: um Job inteiro em
``PROCESSING`` pode internamente já ter passado por ``IMPORTED``,
``TRANSCRIBED`` e ``ANALYZED``). Checkpoint é granularidade de "o que já foi
feito com segurança dentro da etapa atual"; status é o estado operacional
observável de fora.

Este módulo não conhece UI, engines, plataformas, SQLite ou pipelines
concretos de edição — apenas o vocabulário canônico e sua validação, no
mesmo espírito de ``job_state_machine.py``. Ele deliberadamente NÃO impõe
uma ordem entre checkpoints: cada operação decide, na prática (nos prompts
futuros que implementarem o pipeline real de edição), quais etapas percorre
e em que sequência. Impor aqui uma ordem fixa seria antecipar decisões de
prompts futuros (Editor, Render Engine, Content Engine) que ainda não
existem nesta etapa.
"""
from __future__ import annotations

from typing import ClassVar


CHECKPOINT_IMPORTED = "IMPORTED"
CHECKPOINT_TRANSCRIBED = "TRANSCRIBED"
CHECKPOINT_ANALYZED = "ANALYZED"
CHECKPOINT_EDIT_PLANNED = "EDIT_PLANNED"
CHECKPOINT_MEDIA_PROCESSED = "MEDIA_PROCESSED"
CHECKPOINT_COMPOSED = "COMPOSED"
CHECKPOINT_RENDERED = "RENDERED"
CHECKPOINT_VALIDATED = "VALIDATED"
CHECKPOINT_UPLOAD_STARTED = "UPLOAD_STARTED"
CHECKPOINT_REMOTE_CONFIRMED = "REMOTE_CONFIRMED"

JOB_CHECKPOINTS = frozenset(
    {
        CHECKPOINT_IMPORTED,
        CHECKPOINT_TRANSCRIBED,
        CHECKPOINT_ANALYZED,
        CHECKPOINT_EDIT_PLANNED,
        CHECKPOINT_MEDIA_PROCESSED,
        CHECKPOINT_COMPOSED,
        CHECKPOINT_RENDERED,
        CHECKPOINT_VALIDATED,
        CHECKPOINT_UPLOAD_STARTED,
        CHECKPOINT_REMOTE_CONFIRMED,
    }
)


class InvalidCheckpointError(ValueError):
    """Checkpoint fora do vocabulário canônico."""


class CheckpointVocabulary:
    """Validação central do vocabulário de checkpoints, análoga a ``JobStateMachine``."""

    checkpoints: ClassVar[frozenset[str]] = JOB_CHECKPOINTS

    @classmethod
    def validate(cls, checkpoint: str) -> str:
        if checkpoint not in cls.checkpoints:
            raise InvalidCheckpointError(f"checkpoint inválido: {checkpoint!r}")
        return checkpoint


def validate_checkpoint(checkpoint: str) -> str:
    """Atalho funcional para ``CheckpointVocabulary.validate``."""
    return CheckpointVocabulary.validate(checkpoint)


__all__ = [
    "CHECKPOINT_IMPORTED",
    "CHECKPOINT_TRANSCRIBED",
    "CHECKPOINT_ANALYZED",
    "CHECKPOINT_EDIT_PLANNED",
    "CHECKPOINT_MEDIA_PROCESSED",
    "CHECKPOINT_COMPOSED",
    "CHECKPOINT_RENDERED",
    "CHECKPOINT_VALIDATED",
    "CHECKPOINT_UPLOAD_STARTED",
    "CHECKPOINT_REMOTE_CONFIRMED",
    "JOB_CHECKPOINTS",
    "InvalidCheckpointError",
    "CheckpointVocabulary",
    "validate_checkpoint",
]
