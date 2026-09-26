# -*- coding: utf-8 -*-
"""Leitura segura de JSON de estado local.

Arquivo inexistente pode usar default. Arquivo existente porém inválido/ilegível
é erro explícito e nunca é interpretado como estado vazio.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class StateJsonReadError(RuntimeError):
    """JSON de estado existe, mas não pôde ser lido/decodificado com segurança."""

    def __init__(self, path: Path | str, cause: BaseException) -> None:
        self.path = Path(path)
        self.cause = cause
        super().__init__(f"JSON de estado inválido ou ilegível: {self.path}: {cause}")


def load_state_json(path: Path | str, default: Any) -> Any:
    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StateJsonReadError(path, exc) from exc


__all__ = ["StateJsonReadError", "load_state_json"]
