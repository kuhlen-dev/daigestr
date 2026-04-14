"""
Audit REST API — GET/DELETE Endpoints für das Audit-Log (T-DAI-073).

Router: audit_router (prefix=/v1/audit, tags=["audit"])
Endpoints:
  GET  /v1/audit/{request_id}  — Alle Events für eine request_id
  GET  /v1/audit/job/{job_id}  — Alle Events für eine job_id
  GET  /v1/audit               — Gefilterte Liste (since, until, level, event_type, limit)
  DELETE /v1/audit/cleanup     — Alte Einträge löschen (retention_days aus Settings)

Alle Endpoints liefern HTTP 404 wenn AUDIT_API_ENABLED=false.
"""

from datetime import datetime
from typing import Optional

import structlog
from fastapi import APIRouter, HTTPException, Query

from audit_db import (
    audit_get_by_request,
    audit_get_by_execution,
    audit_get_by_job,
    audit_list,
    audit_cleanup,
)
from settings import AUDIT_API_ENABLED, AUDIT_RETENTION_DAYS

log = structlog.get_logger()

audit_router = APIRouter(prefix="/v1/audit", tags=["audit"])

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _check_enabled() -> None:
    """Raise HTTP 404 when the audit API is disabled via AUDIT_API_ENABLED=false."""
    if not AUDIT_API_ENABLED:
        raise HTTPException(status_code=404, detail="Audit API is disabled (AUDIT_API_ENABLED=false)")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@audit_router.get("/{request_id}", summary="Get audit events by request ID")
async def api_audit_get_by_request(request_id: str) -> dict:
    """Return all audit log events for a given *request_id*, in chronological order.

    Each event includes: id, request_id, job_id, event_type, step, detail,
    progress, level, error, duration_ms, metadata, source_ip, user_agent, created_at.

    Returns HTTP 404 if `AUDIT_API_ENABLED` is false.
    """
    _check_enabled()
    try:
        events = audit_get_by_request(request_id)
    except Exception as exc:
        log.error("audit_api_get_by_request_failed", request_id=request_id, error=str(exc))
        raise HTTPException(status_code=500, detail=f"Failed to query audit log: {exc}") from exc
    return {"request_id": request_id, "count": len(events), "events": events}


@audit_router.get("/job/{job_id}", summary="Get audit events by job ID")
async def api_audit_get_by_job(job_id: str) -> dict:
    """Return all audit log events for a given async *job_id*, in chronological order.

    Useful for tracing the full execution history of an async conversion job,
    including every pipeline step, Mistral API call, warning and final response.

    Returns HTTP 404 if `AUDIT_API_ENABLED` is false.
    """
    _check_enabled()
    try:
        events = audit_get_by_job(job_id)
    except Exception as exc:
        log.error("audit_api_get_by_job_failed", job_id=job_id, error=str(exc))
        raise HTTPException(status_code=500, detail=f"Failed to query audit log: {exc}") from exc
    return {"job_id": job_id, "count": len(events), "events": events}


@audit_router.get("/execution/{execution_id}", summary="Get audit events by execution ID")
async def api_audit_get_by_execution(execution_id: str) -> dict:
    """Return all audit log events for a canonical execution_id, in chronological order."""
    _check_enabled()
    try:
        events = audit_get_by_execution(execution_id)
    except Exception as exc:
        log.error("audit_api_get_by_execution_failed", execution_id=execution_id, error=str(exc))
        raise HTTPException(status_code=500, detail=f"Failed to query audit log: {exc}") from exc
    return {"execution_id": execution_id, "count": len(events), "events": events}


@audit_router.get("", summary="List audit events (filtered)")
async def api_audit_list(
    since: Optional[datetime] = Query(
        default=None,
        description="Only return events at or after this timestamp (ISO-8601, e.g. 2025-01-01T00:00:00Z)",
    ),
    until: Optional[datetime] = Query(
        default=None,
        description="Only return events at or before this timestamp (ISO-8601)",
    ),
    level: Optional[str] = Query(
        default=None,
        description="Filter by log level: info, warning, error",
    ),
    event_type: Optional[str] = Query(
        default=None,
        description="Filter by event type: request, step, mistral_call, warning, response",
    ),
    limit: int = Query(
        default=100,
        ge=1,
        le=1000,
        description="Maximum number of events to return (1-1000, default 100). Results are ordered newest first.",
    ),
) -> dict:
    """Return a filtered list of audit log events (newest first).

    All query parameters are optional. When no filters are supplied, returns
    the *limit* most recent events across all requests.

    Returns HTTP 404 if `AUDIT_API_ENABLED` is false.
    """
    _check_enabled()
    try:
        events = audit_list(
            since=since,
            until=until,
            level=level,
            event_type=event_type,
            limit=limit,
        )
    except Exception as exc:
        log.error("audit_api_list_failed", error=str(exc))
        raise HTTPException(status_code=500, detail=f"Failed to query audit log: {exc}") from exc
    return {"count": len(events), "limit": limit, "events": events}


@audit_router.delete("/cleanup", summary="Delete old audit events")
async def api_audit_cleanup() -> dict:
    """Delete audit log events older than *AUDIT_RETENTION_DAYS* days.

    The retention period is configured via the `AUDIT_RETENTION_DAYS` environment
    variable (default: 30 days). This endpoint is idempotent — calling it
    repeatedly is safe.

    Returns the number of deleted rows.

    Returns HTTP 404 if `AUDIT_API_ENABLED` is false.
    """
    _check_enabled()
    try:
        deleted = audit_cleanup(AUDIT_RETENTION_DAYS)
    except Exception as exc:
        log.error("audit_api_cleanup_failed", error=str(exc))
        raise HTTPException(status_code=500, detail=f"Audit cleanup failed: {exc}") from exc
    log.info("audit_api_cleanup", deleted=deleted, retention_days=AUDIT_RETENTION_DAYS)
    return {"deleted": deleted, "retention_days": AUDIT_RETENTION_DAYS}
