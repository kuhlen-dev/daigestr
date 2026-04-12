"""
PostgreSQL persistence for intermediate debug snapshots.

T-DAI-083 stores markdown/extraction/normalization intermediates for a limited
retention window so regressions can be replayed without repeated upstream OCR
or extraction calls.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

import structlog

from settings import DEBUG_SNAPSHOTS_RETENTION_DAYS
from templates_db import get_db_connection, _return_conn

log = structlog.get_logger()


def init_debug_snapshot_db() -> None:
    """Create the debug_snapshot table and indexes when missing."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS debug_snapshot (
                id BIGSERIAL PRIMARY KEY,
                request_id TEXT NOT NULL,
                job_id TEXT,
                stage TEXT NOT NULL,
                attempt_number INTEGER,
                attempt_count INTEGER,
                attempt_mode TEXT,
                filename TEXT,
                source_type TEXT,
                payload_json JSONB NOT NULL,
                created_at TIMESTAMPTZ DEFAULT now(),
                expires_at TIMESTAMPTZ NOT NULL
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_debug_snapshot_request_id "
            "ON debug_snapshot (request_id)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_debug_snapshot_job_id "
            "ON debug_snapshot (job_id)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_debug_snapshot_stage "
            "ON debug_snapshot (stage)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_debug_snapshot_created_at "
            "ON debug_snapshot (created_at)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_debug_snapshot_expires_at "
            "ON debug_snapshot (expires_at)"
        )
        conn.commit()
        log.debug("debug_snapshot_db_initialized")
    finally:
        _return_conn(conn)


def debug_snapshot_store(
    *,
    request_id: str,
    stage: str,
    payload: dict,
    job_id: Optional[str] = None,
    attempt_number: Optional[int] = None,
    attempt_count: Optional[int] = None,
    attempt_mode: Optional[str] = None,
    filename: Optional[str] = None,
    source_type: Optional[str] = None,
    retention_days: Optional[int] = None,
    expires_at: Optional[datetime] = None,
) -> int:
    """Persist one snapshot payload and return its row id."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        payload_json = json.dumps(payload, ensure_ascii=False, default=str)
        if expires_at is None:
            keep_days = DEBUG_SNAPSHOTS_RETENTION_DAYS if retention_days is None else retention_days
            cur.execute(
                """
                INSERT INTO debug_snapshot (
                    request_id, job_id, stage, attempt_number, attempt_count,
                    attempt_mode, filename, source_type, payload_json, expires_at
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s::jsonb, now() + (interval '1 day' * %s)
                )
                RETURNING id
                """,
                (
                    request_id, job_id, stage, attempt_number, attempt_count,
                    attempt_mode, filename, source_type, payload_json, keep_days,
                ),
            )
        else:
            cur.execute(
                """
                INSERT INTO debug_snapshot (
                    request_id, job_id, stage, attempt_number, attempt_count,
                    attempt_mode, filename, source_type, payload_json, expires_at
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s::jsonb, %s
                )
                RETURNING id
                """,
                (
                    request_id, job_id, stage, attempt_number, attempt_count,
                    attempt_mode, filename, source_type, payload_json, expires_at,
                ),
            )
        row_id = cur.fetchone()["id"]
        conn.commit()
        return row_id
    finally:
        _return_conn(conn)


def debug_snapshot_get(snapshot_id: int) -> Optional[dict]:
    """Return one stored snapshot or None."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM debug_snapshot WHERE id = %s", (snapshot_id,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        _return_conn(conn)


def debug_snapshot_list(
    *,
    request_id: Optional[str] = None,
    job_id: Optional[str] = None,
    stage: Optional[str] = None,
    limit: int = 100,
) -> list[dict]:
    """List snapshots with optional request/job/stage filtering."""
    conditions: list[str] = []
    params: list = []
    if request_id is not None:
        conditions.append("request_id = %s")
        params.append(request_id)
    if job_id is not None:
        conditions.append("job_id = %s")
        params.append(job_id)
    if stage is not None:
        conditions.append("stage = %s")
        params.append(stage)
    where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            f"SELECT * FROM debug_snapshot {where_clause} ORDER BY created_at DESC, id DESC LIMIT %s",
            params,
        )
        rows = cur.fetchall()
        return [dict(row) for row in rows]
    finally:
        _return_conn(conn)


def debug_snapshot_cleanup(retention_days: Optional[int] = None) -> int:
    """Delete expired snapshots and return the number of deleted rows."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        if retention_days is None:
            cur.execute("DELETE FROM debug_snapshot WHERE expires_at < now()")
        else:
            cur.execute(
                "DELETE FROM debug_snapshot WHERE created_at < now() - (interval '1 day' * %s)",
                (retention_days,),
            )
        deleted = cur.rowcount
        conn.commit()
        log.info(
            "debug_snapshot_cleanup_done",
            deleted=deleted,
            retention_days=retention_days,
        )
        return deleted
    finally:
        _return_conn(conn)
