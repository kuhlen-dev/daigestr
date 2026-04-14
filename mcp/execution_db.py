"""
Execution DB — kanonische Laufentitäten für Direct, Async und spätere Batch-Items.

Dieses Modul definiert die persistierten Kernobjekte:
- execution
- execution_attempt
- execution_result

Wave E16/W16.1/T16.1.1 führt zunächst nur das kanonische Datenmodell und
minimale CRUD-Helfer ein. Bestehende Laufpfade werden in nachfolgenden Tasks
an diese Entitäten angeschlossen.
"""

from __future__ import annotations

from typing import Any, Optional

import psycopg2
import psycopg2.extras
import structlog

from templates_db import get_db_connection, _return_conn

log = structlog.get_logger()

_EXECUTION_KINDS = ("direct", "async", "batch_item", "replay", "system")


def init_execution_db() -> None:
    """Creates execution persistence tables and indices if they do not exist."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS execution (
                id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL UNIQUE,
                idempotency_key TEXT UNIQUE,
                execution_kind TEXT NOT NULL
                    CHECK (execution_kind IN ('direct', 'async', 'batch_item', 'replay', 'system')),
                source_type TEXT,
                source_ref TEXT,
                job_id TEXT,
                batch_id TEXT,
                batch_item_id TEXT,
                status TEXT NOT NULL DEFAULT 'queued',
                current_stage TEXT,
                progress_json JSONB,
                document_identity JSONB,
                policy_context JSONB,
                warning_summary JSONB,
                error_summary JSONB,
                created_at TIMESTAMPTZ DEFAULT now(),
                updated_at TIMESTAMPTZ DEFAULT now(),
                started_at TIMESTAMPTZ,
                finished_at TIMESTAMPTZ
            )
            """
        )
        cur.execute(
            "ALTER TABLE execution "
            "ADD COLUMN IF NOT EXISTS idempotency_key TEXT"
        )
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_execution_idempotency_key "
            "ON execution(idempotency_key) WHERE idempotency_key IS NOT NULL"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_execution_status_created_at "
            "ON execution(status, created_at DESC)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_execution_job_id "
            "ON execution(job_id)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_execution_batch_id "
            "ON execution(batch_id)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_execution_batch_item_id "
            "ON execution(batch_item_id)"
        )

        cur.execute(
            "ALTER TABLE execution "
            "ADD COLUMN IF NOT EXISTS progress_json JSONB"
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS execution_attempt (
                id TEXT PRIMARY KEY,
                execution_id TEXT NOT NULL REFERENCES execution(id) ON DELETE CASCADE,
                attempt_number INTEGER NOT NULL,
                attempt_mode TEXT,
                attempt_reason TEXT,
                status TEXT NOT NULL DEFAULT 'queued',
                quality_score REAL,
                retry_trigger TEXT,
                error JSONB,
                created_at TIMESTAMPTZ DEFAULT now(),
                updated_at TIMESTAMPTZ DEFAULT now(),
                started_at TIMESTAMPTZ,
                finished_at TIMESTAMPTZ,
                UNIQUE (execution_id, attempt_number)
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_execution_attempt_execution "
            "ON execution_attempt(execution_id, attempt_number)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_execution_attempt_status "
            "ON execution_attempt(status, created_at DESC)"
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS execution_result (
                id TEXT PRIMARY KEY,
                execution_id TEXT NOT NULL REFERENCES execution(id) ON DELETE CASCADE,
                attempt_id TEXT REFERENCES execution_attempt(id) ON DELETE SET NULL,
                is_final BOOLEAN NOT NULL DEFAULT false,
                result_status TEXT NOT NULL,
                success BOOLEAN,
                response_json JSONB,
                meta JSONB,
                extracted JSONB,
                normalized JSONB,
                artifact_refs JSONB,
                warnings JSONB,
                error JSONB,
                created_at TIMESTAMPTZ DEFAULT now(),
                updated_at TIMESTAMPTZ DEFAULT now()
            )
            """
        )
        cur.execute(
            "ALTER TABLE execution_result "
            "ADD COLUMN IF NOT EXISTS response_json JSONB"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_execution_result_execution "
            "ON execution_result(execution_id, created_at DESC)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_execution_result_attempt "
            "ON execution_result(attempt_id)"
        )
        cur.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_execution_result_one_final "
            "ON execution_result(execution_id) WHERE is_final = true"
        )

        conn.commit()
        log.info("execution_db_initialized")
    finally:
        _return_conn(conn)


def execution_create(
    execution_id: str,
    request_id: str,
    execution_kind: str,
    *,
    idempotency_key: Optional[str] = None,
    source_type: Optional[str] = None,
    source_ref: Optional[str] = None,
    job_id: Optional[str] = None,
    batch_id: Optional[str] = None,
    batch_item_id: Optional[str] = None,
    document_identity: Optional[dict[str, Any]] = None,
    policy_context: Optional[dict[str, Any]] = None,
    status: str = "queued",
    current_stage: Optional[str] = None,
    progress_json: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Create a new canonical execution row and return it."""
    if execution_kind not in _EXECUTION_KINDS:
        raise ValueError(f"Unsupported execution_kind: {execution_kind}")

    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO execution (
                id, request_id, idempotency_key, execution_kind, source_type, source_ref,
                job_id, batch_id, batch_item_id, status, current_stage,
                progress_json, document_identity, policy_context
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                execution_id,
                request_id,
                idempotency_key,
                execution_kind,
                source_type,
                source_ref,
                job_id,
                batch_id,
                batch_item_id,
                status,
                current_stage,
                psycopg2.extras.Json(progress_json) if progress_json is not None else None,
                psycopg2.extras.Json(document_identity) if document_identity is not None else None,
                psycopg2.extras.Json(policy_context) if policy_context is not None else None,
            ),
        )
        row = dict(cur.fetchone())
        conn.commit()
        return row
    finally:
        _return_conn(conn)


def execution_update(
    execution_id: str,
    *,
    status: Optional[str] = None,
    current_stage: Optional[str] = None,
    progress_json: Optional[dict[str, Any]] = None,
    idempotency_key: Optional[str] = None,
    document_identity: Optional[dict[str, Any]] = None,
    warning_summary: Optional[dict[str, Any]] = None,
    error_summary: Optional[dict[str, Any]] = None,
    policy_context: Optional[dict[str, Any]] = None,
    started_at_now: bool = False,
    finished_at_now: bool = False,
) -> Optional[dict[str, Any]]:
    """Update mutable execution state and return the updated row."""
    assignments: list[str] = ["updated_at = now()"]
    params: list[Any] = []

    if status is not None:
        assignments.append("status = %s")
        params.append(status)
    if current_stage is not None:
        assignments.append("current_stage = %s")
        params.append(current_stage)
    if progress_json is not None:
        assignments.append("progress_json = %s")
        params.append(psycopg2.extras.Json(progress_json))
    if idempotency_key is not None:
        assignments.append("idempotency_key = %s")
        params.append(idempotency_key)
    if document_identity is not None:
        assignments.append("document_identity = %s")
        params.append(psycopg2.extras.Json(document_identity))
    if warning_summary is not None:
        assignments.append("warning_summary = %s")
        params.append(psycopg2.extras.Json(warning_summary))
    if error_summary is not None:
        assignments.append("error_summary = %s")
        params.append(psycopg2.extras.Json(error_summary))
    if policy_context is not None:
        assignments.append("policy_context = %s")
        params.append(psycopg2.extras.Json(policy_context))
    if started_at_now:
        assignments.append("started_at = COALESCE(started_at, now())")
    if finished_at_now:
        assignments.append("finished_at = now()")

    params.append(execution_id)
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE execution SET {', '.join(assignments)} WHERE id = %s RETURNING *",
            params,
        )
        row = cur.fetchone()
        conn.commit()
        return dict(row) if row else None
    finally:
        _return_conn(conn)


def execution_get(execution_id: str) -> Optional[dict[str, Any]]:
    """Fetch a canonical execution row by id."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM execution WHERE id = %s", (execution_id,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        _return_conn(conn)


def execution_get_by_request_id(request_id: str) -> Optional[dict[str, Any]]:
    """Fetch a canonical execution row by request_id."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM execution WHERE request_id = %s", (request_id,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        _return_conn(conn)


def execution_get_by_idempotency_key(idempotency_key: str) -> Optional[dict[str, Any]]:
    """Fetch a canonical execution row by idempotency_key."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM execution WHERE idempotency_key = %s", (idempotency_key,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        _return_conn(conn)


def execution_get_by_job_id(job_id: str) -> Optional[dict[str, Any]]:
    """Fetch the newest canonical execution row by async job id."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM execution WHERE job_id = %s ORDER BY created_at DESC LIMIT 1",
            (job_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        _return_conn(conn)


def execution_list(limit: int = 50) -> list[dict[str, Any]]:
    """Return the newest executions, newest first."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM execution ORDER BY created_at DESC LIMIT %s",
            (limit,),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        _return_conn(conn)


def execution_list_active(limit: int = 50) -> list[dict[str, Any]]:
    """Return active executions ordered by most recently updated first."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT * FROM execution
            WHERE status IN ('queued', 'processing')
            ORDER BY updated_at DESC
            LIMIT %s
            """,
            (limit,),
        )
        return [dict(row) for row in cur.fetchall()]
    finally:
        _return_conn(conn)


def execution_list_stuck(stuck_after_seconds: int, limit: int = 50) -> list[dict[str, Any]]:
    """Return executions that look stale based on updated_at age."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT * FROM execution
            WHERE status IN ('queued', 'processing')
              AND updated_at < now() - make_interval(secs => %s)
            ORDER BY updated_at ASC
            LIMIT %s
            """,
            (int(stuck_after_seconds), limit),
        )
        return [dict(row) for row in cur.fetchall()]
    finally:
        _return_conn(conn)


def execution_get_full(execution_id: str) -> Optional[dict[str, Any]]:
    """Return one execution enriched with attempts and the final result."""
    execution = execution_get(execution_id)
    if not execution:
        return None
    execution["attempts"] = execution_attempt_list(execution_id)
    execution["final_result"] = execution_result_get_final(execution_id)
    return execution


def execution_attempt_upsert(
    attempt_id: str,
    execution_id: str,
    attempt_number: int,
    *,
    attempt_mode: Optional[str] = None,
    attempt_reason: Optional[str] = None,
    status: str = "queued",
    quality_score: Optional[float] = None,
    retry_trigger: Optional[str] = None,
    error: Optional[dict[str, Any]] = None,
    started_at_now: bool = False,
    finished_at_now: bool = False,
) -> dict[str, Any]:
    """Insert or update an execution attempt row."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO execution_attempt (
                id, execution_id, attempt_number, attempt_mode, attempt_reason,
                status, quality_score, retry_trigger, error, started_at, finished_at
            )
            VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s,
                CASE WHEN %s THEN now() ELSE NULL END,
                CASE WHEN %s THEN now() ELSE NULL END
            )
            ON CONFLICT (execution_id, attempt_number) DO UPDATE SET
                id = EXCLUDED.id,
                attempt_mode = EXCLUDED.attempt_mode,
                attempt_reason = EXCLUDED.attempt_reason,
                status = EXCLUDED.status,
                quality_score = EXCLUDED.quality_score,
                retry_trigger = EXCLUDED.retry_trigger,
                error = EXCLUDED.error,
                started_at = COALESCE(execution_attempt.started_at, EXCLUDED.started_at),
                finished_at = COALESCE(EXCLUDED.finished_at, execution_attempt.finished_at),
                updated_at = now()
            RETURNING *
            """,
            (
                attempt_id,
                execution_id,
                attempt_number,
                attempt_mode,
                attempt_reason,
                status,
                quality_score,
                retry_trigger,
                psycopg2.extras.Json(error) if error is not None else None,
                started_at_now,
                finished_at_now,
            ),
        )
        row = dict(cur.fetchone())
        conn.commit()
        return row
    finally:
        _return_conn(conn)


def execution_attempt_list(execution_id: str) -> list[dict[str, Any]]:
    """Return all attempts for one execution in attempt order."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM execution_attempt WHERE execution_id = %s ORDER BY attempt_number ASC",
            (execution_id,),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        _return_conn(conn)


def execution_result_upsert(
    result_id: str,
    execution_id: str,
    *,
    attempt_id: Optional[str] = None,
    is_final: bool = False,
    result_status: str,
    success: Optional[bool] = None,
    response_json: Optional[dict[str, Any]] = None,
    meta: Optional[dict[str, Any]] = None,
    extracted: Optional[dict[str, Any]] = None,
    normalized: Optional[dict[str, Any]] = None,
    artifact_refs: Optional[dict[str, Any]] = None,
    warnings: Optional[list[Any]] = None,
    error: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Insert or update one execution result row."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO execution_result (
                id, execution_id, attempt_id, is_final, result_status,
                success, response_json, meta, extracted, normalized, artifact_refs, warnings, error
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                execution_id = EXCLUDED.execution_id,
                attempt_id = EXCLUDED.attempt_id,
                is_final = EXCLUDED.is_final,
                result_status = EXCLUDED.result_status,
                success = EXCLUDED.success,
                response_json = EXCLUDED.response_json,
                meta = EXCLUDED.meta,
                extracted = EXCLUDED.extracted,
                normalized = EXCLUDED.normalized,
                artifact_refs = EXCLUDED.artifact_refs,
                warnings = EXCLUDED.warnings,
                error = EXCLUDED.error,
                updated_at = now()
            RETURNING *
            """,
            (
                result_id,
                execution_id,
                attempt_id,
                is_final,
                result_status,
                success,
                psycopg2.extras.Json(response_json) if response_json is not None else None,
                psycopg2.extras.Json(meta) if meta is not None else None,
                psycopg2.extras.Json(extracted) if extracted is not None else None,
                psycopg2.extras.Json(normalized) if normalized is not None else None,
                psycopg2.extras.Json(artifact_refs) if artifact_refs is not None else None,
                psycopg2.extras.Json(warnings) if warnings is not None else None,
                psycopg2.extras.Json(error) if error is not None else None,
            ),
        )
        row = dict(cur.fetchone())
        conn.commit()
        return row
    finally:
        _return_conn(conn)


def execution_result_get_final(execution_id: str) -> Optional[dict[str, Any]]:
    """Fetch the final result row for one execution."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM execution_result WHERE execution_id = %s AND is_final = true",
            (execution_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        _return_conn(conn)


def execution_result_list(execution_id: str) -> list[dict[str, Any]]:
    """Return all result rows for one execution, newest first."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM execution_result WHERE execution_id = %s ORDER BY created_at DESC, id DESC",
            (execution_id,),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        _return_conn(conn)
