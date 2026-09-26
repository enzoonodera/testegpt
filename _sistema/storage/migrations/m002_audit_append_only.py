"""Migration 002: reforça audit_events como append-only no próprio SQLite."""
from __future__ import annotations

from . import Migration

SQL = r"""
CREATE TRIGGER trg_audit_events_no_update
BEFORE UPDATE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_events is append-only: UPDATE denied');
END;

CREATE TRIGGER trg_audit_events_no_delete
BEFORE DELETE ON audit_events
BEGIN
    SELECT RAISE(ABORT, 'audit_events is append-only: DELETE denied');
END;

CREATE TRIGGER trg_audit_events_no_id_reuse
BEFORE INSERT ON audit_events
WHEN EXISTS (SELECT 1 FROM audit_events WHERE id = NEW.id)
BEGIN
    SELECT RAISE(ABORT, 'audit_events is append-only: duplicate id denied');
END;
""".strip()

MIGRATION = Migration(version=2, name="audit_append_only", sql=SQL)
