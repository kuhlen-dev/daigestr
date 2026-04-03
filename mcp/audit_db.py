"""
Audit Log — PostgreSQL DB Helpers (T-DAI-070).

Verfolgt alle Konvertierungsanfragen, Pipeline-Schritte, Mistral-Calls,
Warnungen und Antworten für Debugging, Monitoring und Compliance.

Tabelle: audit_log
Funktionen:
- init_audit_db()            — CREATE TABLE IF NOT EXISTS + Indices
- audit_log_event(...)       — INSERT, fire-and-forget-geeignet
- audit_get_by_request(...)  — Alle Events für eine request_id
- audit_get_by_job(...)      — Alle Events für eine job_id
- audit_list(...)            — Gefilterte Liste (since, until, level, event_type, limit)
- audit_cleanup(...)         — Alte Einträge löschen, gibt Anzahl zurück

Connection: Importiert get_db_connection und _return_conn aus templates_db.
"""

import json
from datetime import datetime, timezone
from typing import Optional

import structlog

from templates_db import get_db_connection, _return_conn

log = structlog.get_logger()

# Erlaubte event_type-Werte (spiegelt CHECK-Constraint in der DB)
_VALID_EVENT_TYPES = {"request", "step", "mistral_call", "warning", "response"}


def init_audit_db() -> None:
    """Erstellt die audit_log-Tabelle und Indices wenn nicht vorhanden."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id SERIAL PRIMARY KEY,
                request_id TEXT NOT NULL,
                job_id TEXT,
                event_type TEXT NOT NULL CHECK (event_type IN (
                    'request', 'step', 'mistral_call', 'warning', 'response'
                )),
                step TEXT,
                detail TEXT,
                progress INTEGER,
                level TEXT DEFAULT 'info',
                error TEXT,
                duration_ms INTEGER,
                metadata JSONB,
                source_ip TEXT,
                user_agent TEXT,
                created_at TIMESTAMPTZ DEFAULT now()
            )
        """)

        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_log_request_id "
            "ON audit_log (request_id)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_log_job_id "
            "ON audit_log (job_id)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_log_created_at "
            "ON audit_log (created_at)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_log_event_type "
            "ON audit_log (event_type)"
        )

        conn.commit()
        log.debug("audit_db_initialized")
    finally:
        _return_conn(conn)


def audit_log_event(
    request_id: str,
    event_type: str,
    *,
    job_id: Optional[str] = None,
    step: Optional[str] = None,
    detail: Optional[str] = None,
    progress: Optional[int] = None,
    level: str = "info",
    error: Optional[str] = None,
    duration_ms: Optional[int] = None,
    metadata: Optional[dict] = None,
    source_ip: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> None:
    """Schreibt ein Audit-Event in die DB.

    Fire-and-forget-geeignet: Fehler werden geloggt, aber nicht weitergeworfen.
    event_type muss einer von: 'request', 'step', 'mistral_call', 'warning', 'response'.
    """
    if event_type not in _VALID_EVENT_TYPES:
        log.warning(
            "audit_log_event_invalid_type",
            event_type=event_type,
            valid=sorted(_VALID_EVENT_TYPES),
        )
        return

    metadata_json: Optional[str] = None
    if metadata is not None:
        try:
            metadata_json = json.dumps(metadata, ensure_ascii=False, default=str)
        except (TypeError, ValueError) as exc:
            log.warning("audit_log_event_metadata_serialization_failed", error=str(exc))
            metadata_json = None

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO audit_log (
                request_id, job_id, event_type, step, detail,
                progress, level, error, duration_ms, metadata,
                source_ip, user_agent
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s::jsonb,
                %s, %s
            )
            """,
            (
                request_id, job_id, event_type, step, detail,
                progress, level, error, duration_ms, metadata_json,
                source_ip, user_agent,
            ),
        )
        conn.commit()
    except Exception as exc:
        log.warning("audit_log_event_failed", request_id=request_id, error=str(exc))
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        _return_conn(conn)


def audit_get_by_request(request_id: str) -> list[dict]:
    """Gibt alle Audit-Events für eine request_id zurück, chronologisch."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM audit_log WHERE request_id = %s ORDER BY created_at ASC, id ASC",
            (request_id,),
        )
        rows = cur.fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        _return_conn(conn)


def audit_get_by_job(job_id: str) -> list[dict]:
    """Gibt alle Audit-Events für eine job_id zurück, chronologisch."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM audit_log WHERE job_id = %s ORDER BY created_at ASC, id ASC",
            (job_id,),
        )
        rows = cur.fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        _return_conn(conn)


def audit_list(
    *,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    level: Optional[str] = None,
    event_type: Optional[str] = None,
    limit: int = 100,
) -> list[dict]:
    """Gibt eine gefilterte Liste von Audit-Events zurück.

    Args:
        since:      Nur Events ab diesem Zeitpunkt (inkl.)
        until:      Nur Events bis zu diesem Zeitpunkt (inkl.)
        level:      Filter auf Log-Level ('info', 'warning', 'error', ...)
        event_type: Filter auf Event-Typ ('request', 'step', ...)
        limit:      Maximale Anzahl Einträge (default 100, max empfohlen 1000)

    Returns:
        Liste von Dicts, neueste zuerst.
    """
    conditions: list[str] = []
    params: list = []

    if since is not None:
        conditions.append("created_at >= %s")
        params.append(since)
    if until is not None:
        conditions.append("created_at <= %s")
        params.append(until)
    if level is not None:
        conditions.append("level = %s")
        params.append(level)
    if event_type is not None:
        conditions.append("event_type = %s")
        params.append(event_type)

    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            f"SELECT * FROM audit_log {where_clause} "
            f"ORDER BY created_at DESC, id DESC LIMIT %s",
            params,
        )
        rows = cur.fetchall()
        return [_row_to_dict(r) for r in rows]
    finally:
        _return_conn(conn)


def audit_cleanup(retention_days: int) -> int:
    """Löscht Audit-Events älter als retention_days Tage.

    Args:
        retention_days: Anzahl Tage — Einträge älter als dieser Wert werden gelöscht.

    Returns:
        Anzahl der gelöschten Zeilen.
    """
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM audit_log "
            "WHERE created_at < now() - (interval '1 day' * %s)",
            (retention_days,),
        )
        deleted = cur.rowcount
        conn.commit()
        log.info("audit_cleanup_done", deleted=deleted, retention_days=retention_days)
        return deleted
    finally:
        _return_conn(conn)


# ---------------------------------------------------------------------------
# Interne Helpers
# ---------------------------------------------------------------------------

def _row_to_dict(row) -> dict:
    """Konvertiert einen DB-Row (RealDictRow oder dict) in ein sauberes dict.

    JSONB-Felder werden als Python-Objekte zurückgegeben (psycopg2 parsed diese
    automatisch). datetime-Felder werden als ISO-8601-Strings zurückgegeben.
    """
    result = dict(row)

    # created_at: datetime → ISO-String für JSON-Serialisierbarkeit
    if isinstance(result.get("created_at"), datetime):
        result["created_at"] = result["created_at"].isoformat()

    return result
