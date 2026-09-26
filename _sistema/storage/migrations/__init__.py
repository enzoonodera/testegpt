"""Registro imutável de migrations numeradas do SQLite local."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


from .m001_initial import MIGRATION as MIGRATION_001  # noqa: E402
from .m002_audit_append_only import MIGRATION as MIGRATION_002  # noqa: E402
from .m003_batch_engine import MIGRATION as MIGRATION_003  # noqa: E402
from .m004_circuit_breaker import MIGRATION as MIGRATION_004  # noqa: E402
from .m005_retry_policy import MIGRATION as MIGRATION_005  # noqa: E402
from .m006_publication_idempotency import MIGRATION as MIGRATION_006  # noqa: E402
from .m007_source_asset_declarations import MIGRATION as MIGRATION_007  # noqa: E402
from .m008_source_asset_context import MIGRATION as MIGRATION_008  # noqa: E402
from .m009_video_declarations import MIGRATION as MIGRATION_009  # noqa: E402

MIGRATIONS: tuple[Migration, ...] = (
    MIGRATION_001, MIGRATION_002, MIGRATION_003, MIGRATION_004, MIGRATION_005,
    MIGRATION_006, MIGRATION_007, MIGRATION_008, MIGRATION_009,
)
LATEST_SCHEMA_VERSION = MIGRATIONS[-1].version if MIGRATIONS else 0

__all__ = ["Migration", "MIGRATIONS", "LATEST_SCHEMA_VERSION"]
