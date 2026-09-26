# -*- coding: utf-8 -*-
"""Trava de integridade das migrations congeladas (GATE 19.5).

O relatorio do Estagio 1 disse que nao verificou os hashes das migrations
"porque elas nao haviam sido alteradas" — isso nao e uma verificacao, e
uma suposicao. Este teste torna a verificacao automatica e parte do gate
de release: se qualquer byte de m001/m002/m003/m004 mudar, ou se um m005
aparecer sem essa lista ser atualizada conscientemente, a suite FALHA.

Alterar os hashes abaixo (ou adicionar uma nova entrada para m005+) exige
uma decisao consciente e documentada — nunca silenciosa.

PROMPT 20 (Circuit Breaker): m004_circuit_breaker.py foi adicionada de
proposito nesta rodada (nova tabela `circuit_breaker_state` -- ver
`_sistema/circuit_breaker.py`). O hash abaixo para ela foi calculado e
adicionado conscientemente nesta mesma mudanca, exatamente como esta
docstring exige; m001/m002/m003 permanecem com os hashes ja congelados
anteriormente, sem nenhuma alteracao.

PROMPT 21 (Retry Inteligente): m005_retry_policy.py foi adicionada de
proposito nesta rodada (nova tabela `job_retry_state` -- ver
`_sistema/retry_policy.py`). O hash abaixo para ela foi calculado e
adicionado conscientemente nesta mesma mudanca; m001/m002/m003/m004
permanecem com os hashes ja congelados anteriormente, sem nenhuma
alteracao (confirmado byte a byte nesta rodada -- nenhuma migration
existente foi tocada).

PROMPT 22 (Idempotencia): m006_publication_idempotency.py foi adicionada
de proposito nesta rodada (coluna `idempotency_key` + indice unico parcial
em `publications` -- ver `_sistema/publication_idempotency.py`). E' a
primeira migration deste projeto a alterar uma tabela EXISTENTE (ALTER
TABLE) em vez de criar uma nova. O hash abaixo foi calculado e adicionado
conscientemente nesta mesma mudanca; m001/m002/m003/m004/m005 permanecem
com os hashes ja congelados anteriormente, sem nenhuma alteracao
(confirmado byte a byte nesta rodada -- nenhuma migration existente foi
tocada).

PROMPT 24b (Import Options / User Assertions): m007_source_asset_declarations.py
foi adicionada de proposito nesta rodada (nova tabela append-only
`source_asset_declarations` -- ver `_sistema/source_import.py`,
`ImportOptions`/`apply_assertions`/`remove_assertion`). O hash abaixo foi
calculado e adicionado conscientemente nesta mesma mudanca;
m001/m002/m003/m004/m005/m006 permanecem com os hashes ja congelados
anteriormente, sem nenhuma alteracao (confirmado byte a byte nesta rodada
-- nenhuma migration existente foi tocada).

PROMPT 25 (Source Context Resolver): m008_source_asset_context.py foi
adicionada de proposito nesta rodada (nova tabela append-only dedicada
`source_asset_context` -- ver `_sistema/source_context.py`,
`SourceContextResolver`). Decisao de desenho: tabela NOVA em vez de
estender o CHECK constraint de `kind` em m007 -- ver a docstring da
propria migration para a justificativa completa. O hash abaixo foi
calculado e adicionado conscientemente nesta mesma mudanca;
m001/m002/m003/m004/m005/m006/m007 permanecem com os hashes ja congelados
anteriormente, sem nenhuma alteracao (confirmado byte a byte nesta rodada
-- nenhuma migration existente foi tocada).

PROMPT 27.5 (Media Catalog Backend): m009_video_declarations.py foi
adicionada de proposito nesta rodada (nova tabela append-only dedicada
`video_declarations`, keyed por `video_id` -- ver `_sistema/media_catalog.py`,
`MediaCatalogService.set_user_flag`/`add_user_label`/`set_user_assertion`
e correspondentes `remove_*`). Decisao de desenho: tabela NOVA, keyed por
`video_id`, em vez de reaproveitar `source_asset_declarations` (m007, keyed
por `source_asset_id` -- entidade diferente, FK errada) -- ver a docstring
da propria migration para a justificativa completa, incluindo por que
`origin` foi fixado em `'USER'` (diferente de m007, que aceita
`SYSTEM`/`USER`/`IMPORT_PRESET`). O hash abaixo foi calculado e adicionado
conscientemente nesta mesma mudanca; m001/m002/m003/m004/m005/m006/m007/m008
permanecem com os hashes ja congelados anteriormente, sem nenhuma alteracao
(confirmado byte a byte nesta rodada -- nenhuma migration existente foi
tocada).
"""
from __future__ import annotations

import hashlib
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "_sistema" / "storage" / "migrations"

# Nome do arquivo -> SHA-256 congelado. Confirmado identico entre a auditoria
# independente do Estagio 1 e o workspace de desenvolvimento.
FROZEN_MIGRATION_HASHES = {
    "m001_initial.py": "3c78c665c1bae7c50482d513c9e7534d5586caa23513a1e4457712502a806e70",
    "m002_audit_append_only.py": "1ba5a8422900e8ac35e25e44d2940cfb10687b508313fe0e5d158b521b6e6a36",
    "m003_batch_engine.py": "ab99085a0d77f1e260119d353b25e619769c05f5802d8190b0e88471a76b33e1",
    "m004_circuit_breaker.py": "2375542d04433650c3748f36d3926ab715b141ef283c4ac069e5ef1db005adbc",
    "m005_retry_policy.py": "33bbcb8eea9e3f69c798a19d13bd964a503305c16c719696bf0aaa1172413d75",
    "m006_publication_idempotency.py": "8b074ab59692137e5239c6fc27d9f8248b5df1e6c5bb069876a39f0a66e3da39",
    "m007_source_asset_declarations.py": "2ccbc4e252ed201f5933a2adaddb1bd93780738e531c7529b0ac47b8d8dfa144",
    "m008_source_asset_context.py": "62d677417d4fb5549cd7bd365daffa786d13200f56f5eb97d97e0e10866ff9f4",
    "m009_video_declarations.py": "708396f5f8ff1e29440327c04df55060c787b6aaa5d1d1ecffbdc70b7bc358a6",
}


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def test_migrations_congeladas_nao_foram_alteradas():
    for filename, expected_hash in FROZEN_MIGRATION_HASHES.items():
        path = MIGRATIONS_DIR / filename
        assert path.exists(), f"migration congelada ausente: {filename}"
        actual_hash = _sha256_of(path)
        assert actual_hash == expected_hash, (
            f"{filename} foi alterada (SHA-256 nao bate). "
            f"esperado={expected_hash} atual={actual_hash}. "
            "Se a alteracao foi intencional e autorizada, atualize "
            "FROZEN_MIGRATION_HASHES conscientemente nesta mesma mudanca."
        )


def test_nenhuma_migration_nova_fora_da_lista_congelada():
    """Um m004 (ou qualquer outro arquivo de migration) so pode existir se
    tiver sido adicionado conscientemente a FROZEN_MIGRATION_HASHES."""
    if not MIGRATIONS_DIR.is_dir():
        return
    actual_files = {
        p.name
        for p in MIGRATIONS_DIR.glob("m*.py")
        if p.name != "__init__.py"
    }
    known_files = set(FROZEN_MIGRATION_HASHES)
    unexpected = actual_files - known_files
    assert not unexpected, (
        f"Migration(s) nova(s) encontrada(s) sem autorizacao explicita: "
        f"{sorted(unexpected)}. Isso nao pode acontecer sem revisao — "
        "pare e relate, conforme o gate exige, antes de adicionar ao "
        "conjunto congelado."
    )
