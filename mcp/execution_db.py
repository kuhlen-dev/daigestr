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
                input_snapshot JSONB,
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
            "ALTER TABLE execution "
            "ADD COLUMN IF NOT EXISTS input_snapshot JSONB"
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
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS execution_batch (
                id TEXT PRIMARY KEY,
                batch_ref TEXT,
                idempotency_key TEXT UNIQUE,
                queue_name TEXT NOT NULL DEFAULT 'default',
                status TEXT NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued', 'processing', 'completed', 'failed', 'partial', 'cancelled')),
                item_count INTEGER NOT NULL DEFAULT 0,
                submitted_count INTEGER NOT NULL DEFAULT 0,
                completed_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                metadata JSONB,
                created_at TIMESTAMPTZ DEFAULT now(),
                updated_at TIMESTAMPTZ DEFAULT now(),
                started_at TIMESTAMPTZ,
                finished_at TIMESTAMPTZ
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_execution_batch_status_created_at "
            "ON execution_batch(status, created_at DESC)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_execution_batch_batch_ref "
            "ON execution_batch(batch_ref)"
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS execution_batch_item (
                id TEXT PRIMARY KEY,
                batch_id TEXT NOT NULL REFERENCES execution_batch(id) ON DELETE CASCADE,
                item_index INTEGER NOT NULL,
                execution_id TEXT UNIQUE REFERENCES execution(id) ON DELETE SET NULL,
                request_id TEXT NOT NULL UNIQUE,
                source_type TEXT,
                source_ref TEXT,
                filename TEXT,
                status TEXT NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued', 'processing', 'completed', 'failed', 'cancelled')),
                item_key TEXT,
                metadata JSONB,
                document_identity JSONB,
                input_snapshot JSONB,
                created_at TIMESTAMPTZ DEFAULT now(),
                updated_at TIMESTAMPTZ DEFAULT now(),
                UNIQUE (batch_id, item_index)
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_execution_batch_item_batch_id "
            "ON execution_batch_item(batch_id, item_index)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_execution_batch_item_execution_id "
            "ON execution_batch_item(execution_id)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_execution_batch_item_item_key "
            "ON execution_batch_item(item_key)"
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS execution_queue (
                id TEXT PRIMARY KEY,
                execution_id TEXT NOT NULL UNIQUE REFERENCES execution(id) ON DELETE CASCADE,
                job_id TEXT,
                queue_name TEXT NOT NULL DEFAULT 'default',
                status TEXT NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued', 'claimed', 'completed', 'failed', 'cancelled')),
                payload JSONB,
                available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                claimed_by TEXT,
                claimed_at TIMESTAMPTZ,
                lease_expires_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ DEFAULT now(),
                updated_at TIMESTAMPTZ DEFAULT now()
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_execution_queue_claim "
            "ON execution_queue(queue_name, status, available_at, created_at)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_execution_queue_job_id "
            "ON execution_queue(job_id)"
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
    input_snapshot: Optional[dict[str, Any]] = None,
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
                progress_json, document_identity, input_snapshot, policy_context
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                psycopg2.extras.Json(input_snapshot) if input_snapshot is not None else None,
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
    batch_id: Optional[str] = None,
    batch_item_id: Optional[str] = None,
    document_identity: Optional[dict[str, Any]] = None,
    input_snapshot: Optional[dict[str, Any]] = None,
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
    if batch_id is not None:
        assignments.append("batch_id = %s")
        params.append(batch_id)
    if batch_item_id is not None:
        assignments.append("batch_item_id = %s")
        params.append(batch_item_id)
    if document_identity is not None:
        assignments.append("document_identity = %s")
        params.append(psycopg2.extras.Json(document_identity))
    if input_snapshot is not None:
        assignments.append("input_snapshot = %s")
        params.append(psycopg2.extras.Json(input_snapshot))
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


def execution_queue_enqueue(
    *,
    queue_id: str,
    execution_id: str,
    job_id: Optional[str],
    payload: dict[str, Any],
    queue_name: str = "default",
) -> dict[str, Any]:
    """Insert or refresh one queued execution item."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO execution_queue (
                id, execution_id, job_id, queue_name, status, payload, available_at
            )
            VALUES (%s, %s, %s, %s, 'queued', %s, now())
            ON CONFLICT (execution_id) DO UPDATE SET
                job_id = EXCLUDED.job_id,
                queue_name = EXCLUDED.queue_name,
                status = 'queued',
                payload = EXCLUDED.payload,
                available_at = now(),
                claimed_by = NULL,
                claimed_at = NULL,
                lease_expires_at = NULL,
                updated_at = now()
            RETURNING *
            """,
            (
                queue_id,
                execution_id,
                job_id,
                queue_name,
                psycopg2.extras.Json(payload),
            ),
        )
        row = dict(cur.fetchone())
        conn.commit()
        return row
    finally:
        _return_conn(conn)


def execution_queue_claim_next(
    *,
    worker_id: str,
    lease_seconds: int,
    queue_name: str = "default",
) -> Optional[dict[str, Any]]:
    """Claim the next queued execution item with a lease."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            WITH next_item AS (
                SELECT id
                FROM execution_queue
                WHERE queue_name = %s
                  AND (
                        (status = 'queued' AND available_at <= now())
                     OR (status = 'claimed' AND lease_expires_at IS NOT NULL AND lease_expires_at <= now())
                  )
                ORDER BY created_at ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            UPDATE execution_queue q
            SET status = 'claimed',
                claimed_by = %s,
                claimed_at = now(),
                lease_expires_at = now() + (%s * interval '1 second'),
                updated_at = now()
            FROM next_item
            WHERE q.id = next_item.id
            RETURNING q.*
            """,
            (queue_name, worker_id, lease_seconds),
        )
        row = cur.fetchone()
        conn.commit()
        return dict(row) if row else None
    finally:
        _return_conn(conn)


def execution_queue_complete(queue_id: str) -> Optional[dict[str, Any]]:
    """Mark a claimed queue item as completed."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE execution_queue
            SET status = 'completed',
                claimed_by = NULL,
                claimed_at = NULL,
                lease_expires_at = NULL,
                updated_at = now()
            WHERE id = %s
            RETURNING *
            """,
            (queue_id,),
        )
        row = cur.fetchone()
        conn.commit()
        return dict(row) if row else None
    finally:
        _return_conn(conn)


def execution_queue_fail(queue_id: str) -> Optional[dict[str, Any]]:
    """Mark a claimed queue item as failed."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE execution_queue
            SET status = 'failed',
                claimed_by = NULL,
                claimed_at = NULL,
                lease_expires_at = NULL,
                updated_at = now()
            WHERE id = %s
            RETURNING *
            """,
            (queue_id,),
        )
        row = cur.fetchone()
        conn.commit()
        return dict(row) if row else None
    finally:
        _return_conn(conn)


def execution_queue_list(*, status: Optional[str] = None, limit: int = 100) -> list[dict[str, Any]]:
    """List queue items, newest first."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        if status:
            cur.execute(
                "SELECT * FROM execution_queue WHERE status = %s ORDER BY created_at DESC LIMIT %s",
                (status, limit),
            )
        else:
            cur.execute(
                "SELECT * FROM execution_queue ORDER BY created_at DESC LIMIT %s",
                (limit,),
            )
        return [dict(r) for r in cur.fetchall()]
    finally:
        _return_conn(conn)


def execution_batch_create(
    *,
    batch_id: str,
    batch_ref: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    queue_name: str = "default",
    status: str = "queued",
    item_count: int = 0,
    metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Create or reuse one persisted execution batch row."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO execution_batch (
                id, batch_ref, idempotency_key, queue_name, status, item_count, metadata
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                batch_id,
                batch_ref,
                idempotency_key,
                queue_name,
                status,
                item_count,
                psycopg2.extras.Json(metadata) if metadata is not None else None,
            ),
        )
        row = dict(cur.fetchone())
        conn.commit()
        return row
    finally:
        _return_conn(conn)


def execution_batch_get(batch_id: str) -> Optional[dict[str, Any]]:
    """Fetch one persisted execution batch row by id."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM execution_batch WHERE id = %s", (batch_id,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        _return_conn(conn)


def execution_batch_get_by_idempotency_key(idempotency_key: str) -> Optional[dict[str, Any]]:
    """Fetch one persisted execution batch row by idempotency key."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM execution_batch WHERE idempotency_key = %s", (idempotency_key,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        _return_conn(conn)


def execution_batch_item_create(
    *,
    batch_item_id: str,
    batch_id: str,
    item_index: int,
    execution_id: Optional[str],
    request_id: str,
    source_type: Optional[str],
    source_ref: Optional[str],
    filename: Optional[str],
    status: str = "queued",
    item_key: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
    document_identity: Optional[dict[str, Any]] = None,
    input_snapshot: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Create or refresh one persisted batch item row."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO execution_batch_item (
                id, batch_id, item_index, execution_id, request_id, source_type, source_ref,
                filename, status, item_key, metadata, document_identity, input_snapshot
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (batch_id, item_index) DO UPDATE SET
                execution_id = EXCLUDED.execution_id,
                request_id = EXCLUDED.request_id,
                source_type = EXCLUDED.source_type,
                source_ref = EXCLUDED.source_ref,
                filename = EXCLUDED.filename,
                status = EXCLUDED.status,
                item_key = EXCLUDED.item_key,
                metadata = EXCLUDED.metadata,
                document_identity = EXCLUDED.document_identity,
                input_snapshot = EXCLUDED.input_snapshot,
                updated_at = now()
            RETURNING *
            """,
            (
                batch_item_id,
                batch_id,
                item_index,
                execution_id,
                request_id,
                source_type,
                source_ref,
                filename,
                status,
                item_key,
                psycopg2.extras.Json(metadata) if metadata is not None else None,
                psycopg2.extras.Json(document_identity) if document_identity is not None else None,
                psycopg2.extras.Json(input_snapshot) if input_snapshot is not None else None,
            ),
        )
        row = dict(cur.fetchone())
        conn.commit()
        return row
    finally:
        _return_conn(conn)


def execution_batch_item_list(batch_id: str) -> list[dict[str, Any]]:
    """List all persisted batch items for one batch in item order."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM execution_batch_item WHERE batch_id = %s ORDER BY item_index ASC",
            (batch_id,),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        _return_conn(conn)


def execution_batch_list(limit: int = 50) -> list[dict[str, Any]]:
    """List persisted execution batches, newest first."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM execution_batch ORDER BY created_at DESC LIMIT %s", (limit,))
        return [dict(r) for r in cur.fetchall()]
    finally:
        _return_conn(conn)


def execution_batch_status_summary(
    batch_id: str,
    *,
    active_item_limit: int = 10,
) -> Optional[dict[str, Any]]:
    """Return one lightweight batch summary with derived status counts and active items."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM execution_batch WHERE id = %s", (batch_id,))
        batch = cur.fetchone()
        if not batch:
            return None
        batch_row = dict(batch)
        cur.execute(
            """
            SELECT
                COUNT(*) AS item_count,
                COUNT(*) FILTER (
                    WHERE COALESCE(e.status, i.status) = 'queued'
                ) AS queued_count,
                COUNT(*) FILTER (
                    WHERE COALESCE(e.status, i.status) = 'processing'
                ) AS processing_count,
                COUNT(*) FILTER (
                    WHERE COALESCE(e.status, i.status) = 'completed'
                ) AS completed_count,
                COUNT(*) FILTER (
                    WHERE COALESCE(e.status, i.status) = 'failed'
                ) AS failed_count,
                COUNT(*) FILTER (
                    WHERE COALESCE(e.status, i.status) = 'cancelled'
                ) AS cancelled_count,
                MIN(e.started_at) FILTER (WHERE e.started_at IS NOT NULL) AS started_at,
                MAX(e.finished_at) FILTER (WHERE e.finished_at IS NOT NULL) AS finished_at
            FROM execution_batch_item i
            LEFT JOIN execution e ON e.id = i.execution_id
            WHERE i.batch_id = %s
            """,
            (batch_id,),
        )
        counts = dict(cur.fetchone())
        item_count = int(counts.get("item_count") or 0)
        queued_count = int(counts.get("queued_count") or 0)
        processing_count = int(counts.get("processing_count") or 0)
        completed_count = int(counts.get("completed_count") or 0)
        failed_count = int(counts.get("failed_count") or 0)
        cancelled_count = int(counts.get("cancelled_count") or 0)

        if item_count == 0:
            derived_status = "queued"
        elif completed_count == item_count:
            derived_status = "completed"
        elif failed_count == item_count:
            derived_status = "failed"
        elif cancelled_count == item_count:
            derived_status = "cancelled"
        elif processing_count > 0:
            derived_status = "processing"
        elif completed_count > 0 and failed_count > 0 and completed_count + failed_count == item_count:
            derived_status = "partial"
        elif completed_count > 0 and completed_count < item_count:
            derived_status = "processing"
        else:
            derived_status = "queued"

        cur.execute(
            """
            SELECT
                i.id AS batch_item_id,
                i.item_index,
                i.execution_id,
                i.filename,
                COALESCE(e.status, i.status) AS status,
                e.current_stage
            FROM execution_batch_item i
            LEFT JOIN execution e ON e.id = i.execution_id
            WHERE i.batch_id = %s
              AND COALESCE(e.status, i.status) IN ('queued', 'processing')
            ORDER BY i.item_index ASC
            LIMIT %s
            """,
            (batch_id, active_item_limit),
        )
        active_items = [dict(r) for r in cur.fetchall()]

        started_at = counts.get("started_at") or batch_row.get("started_at")
        finished_at = counts.get("finished_at") if derived_status in {"completed", "failed", "partial", "cancelled"} else None
        cur.execute(
            """
            UPDATE execution_batch
            SET
                status = %s,
                item_count = %s,
                completed_count = %s,
                failed_count = %s,
                submitted_count = %s,
                started_at = COALESCE(started_at, %s),
                finished_at = %s,
                updated_at = now()
            WHERE id = %s
            RETURNING *
            """,
            (
                derived_status,
                item_count,
                completed_count,
                failed_count,
                item_count,
                started_at,
                finished_at,
                batch_id,
            ),
        )
        updated_batch = dict(cur.fetchone())
        conn.commit()
        return {
            **updated_batch,
            "queued_count": queued_count,
            "processing_count": processing_count,
            "completed_count": completed_count,
            "failed_count": failed_count,
            "cancelled_count": cancelled_count,
            "active_items": active_items,
        }
    finally:
        _return_conn(conn)
