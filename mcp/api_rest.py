"""
REST API Endpoints für Daigestr.

Enthält alle @app.post/@app.get Endpoint-Handler und die FastAPI app Instanz.

Patchbare Symbole werden über _get() aus dem server-Namespace gelesen,
damit Test-Patches auf _server.X korrekt funktionieren.
"""

import asyncio
import base64
import hashlib
import json
import mimetypes
import time
import uuid
from pathlib import Path
from typing import Any, Optional

import httpx
import structlog
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from models import (
    BatchActiveItemResponse,
    BatchCreateRequest,
    BatchItemListResponse,
    BatchItemResponse,
    BatchListResponse,
    BatchStartResponse,
    BatchStatusResponse,
    ConvertRequest,
    ConvertResponse,
    ConvertFolderRequest,
    AnalyzeRequest,
    ExtractRequest,
    TemplateResponse,
    HealthResponse,
    ProgressState,
    AsyncJobStartResponse,
    ReplayStartResponse,
    JobStatusResponse,
    JobListResponse,
    ExecutionAttemptResponse,
    ExecutionStatusResponse,
    ExecutionListResponse,
    ExecutionDiagnosticsResponse,
    ErrorCode,
    ExecutionSubjobResponse,
    create_error_response,
    create_success_response,
)
from settings import (
    VERSION,
    MARKITDOWN_EXTENSIONS,
    IMAGE_EXTENSIONS,
    AUDIO_EXTENSIONS,
    VIDEO_EXTENSIONS,
    MISTRAL_API_KEY,
    MISTRAL_VISION_MODEL,
    MISTRAL_OCR_MODEL,
    MISTRAL_OCR_ENABLED,
    MISTRAL_BATCH_ALLOWED_SOURCE_TYPES,
    MISTRAL_BATCH_ENABLED,
    MISTRAL_BATCH_MIN_ITEMS,
    MISTRAL_BATCH_MAX_ACTIVE,
    MISTRAL_BATCH_POLL_INTERVAL_SECONDS,
    MISTRAL_BATCH_TIMEOUT_HOURS,
    MAX_FILE_SIZE_MB,
    IMAGE_MAX_WIDTH,
    MAX_RETRIES,
    EXECUTION_RESULT_RETENTION_DAYS,
    EXECUTION_RESULT_ARTIFACT_RETENTION_DAYS,
    DEBUG_SNAPSHOTS_RETENTION_DAYS,
    MCP_PORT,
    REST_PORT,
    LOG_LEVEL,
    START_TIME,
    TEMP_DIR,
    MISTRAL_TIMEOUT,
    WEBHOOK_TIMEOUT_SECONDS,
    JOB_TIMEOUT_SECONDS,
    QUEUE_ENABLED,
    QUEUE_WORKER_COUNT,
    QUEUE_POLL_INTERVAL_SECONDS,
    QUEUE_LEASE_SECONDS,
    BATCH_DEFAULT_QUEUE_NAME,
    BATCH_STATUS_ACTIVE_ITEM_LIMIT,
    EXECUTION_DIAGNOSTICS_LIMIT,
    EXECUTION_STUCK_THRESHOLD_SECONDS,
    NORMALIZATION_DRIFT_SAMPLE_LIMIT,
)
from progress_tracking import build_progress_payload, normalize_progress_payload
from utils import _get, resolve_path, PathPolicyError, get_file_extension, get_mimetype, detect_mimetype_from_bytes
from intelligence import (
    classify_document,
    correct_ocr_text,
    extract_structured_data,
    _apply_auto_extract,
    chunk_markdown,
    get_db_connection,
    get_template_by_id,
)
from templates_db import _return_conn as _db_return_conn
from templates_db import (
    get_all_template_ids, search_templates, cache_clear, check_persistence_health,
    job_create, job_update, job_set_result, job_set_terminal_result, job_get, job_delete, job_list,
)
from execution_db import (
    execution_batch_create,
    execution_batch_get,
    execution_batch_get_by_idempotency_key,
    execution_batch_item_get,
    execution_batch_item_create,
    execution_batch_item_list,
    execution_batch_list,
    execution_batch_item_list_paginated,
    execution_batch_status_summary,
    execution_create,
    execution_get,
    execution_get_full,
    execution_get_by_request_id,
    execution_get_by_idempotency_key,
    execution_get_by_job_id,
    execution_list,
    execution_list_active,
    execution_list_stuck,
    execution_queue_cancel,
    execution_queue_enqueue,
    execution_queue_get_by_execution_id,
    execution_queue_claim_next,
    execution_queue_complete,
    execution_queue_fail,
    execution_queue_list,
    execution_update,
    execution_result_clear_final,
    execution_result_upsert,
    execution_result_get_final,
    execution_subjob_get,
    execution_subjob_upsert,
    execution_subjob_list_by_status,
    execution_subjob_list_by_upstream_batch_id,
)
from normalizer_db import get_normalization_drift_summary
from debug_snapshot_db import debug_snapshot_list, debug_snapshot_get
from debug_snapshots import replay_normalization_from_snapshot, build_convert_response_from_snapshot
from routing import (
    _build_document_identity,
    _build_input_snapshot,
    _persist_execution_attempt_result,
    convert_auto,
    convert_url,
    convert_folder_contents,
    _build_tips_dict,
    finalize_url_markdown_response,
)
from mistral_client import (
    build_mistral_batch_ocr_request,
    cancel_mistral_batch_job,
    download_mistral_file,
    extract_mistral_ocr_metadata,
    get_mistral_batch_job,
    parse_mistral_batch_output,
    submit_mistral_batch_job,
)
from api_rest_audit import audit_router
from api_rest_normalize import normalize_router, corrections_router, batch_router

log = structlog.get_logger()
_MISTRAL_BATCH_POLL_TASKS: list[asyncio.Task] = []


def _infer_execution_source(request: ConvertRequest) -> tuple[str, Optional[str]]:
    if request.path:
        return "file", request.path
    if request.base64:
        return "base64", request.filename or "upload"
    if request.url:
        return "url", request.url
    return "unknown", None


def _build_request_idempotency_key(request: ConvertRequest, execution_kind: str, job_id: Optional[str] = None) -> Optional[str]:
    explicit = request.meta.get("idempotency_key")
    if explicit:
        return str(explicit)
    if request.path:
        basis = {
            "execution_kind": execution_kind,
            "source_type": "file",
            "source_ref": request.path,
            "template": request.template,
            "mode": request.mode,
            "auto_extract": request.auto_extract,
            "has_extract_schema": bool(request.extract_schema),
        }
    elif request.base64 and request.filename:
        basis = {
            "execution_kind": execution_kind,
            "source_type": "base64",
            "filename": request.filename,
            "content_sha256": hashlib.sha256(request.base64.encode("utf-8")).hexdigest(),
            "template": request.template,
            "mode": request.mode,
            "auto_extract": request.auto_extract,
            "has_extract_schema": bool(request.extract_schema),
        }
    elif request.url:
        basis = {
            "execution_kind": execution_kind,
            "source_type": "url",
            "source_ref": request.url,
            "template": request.template,
            "mode": request.mode,
            "auto_extract": request.auto_extract,
            "has_extract_schema": bool(request.extract_schema),
        }
    else:
        return None
    if job_id:
        basis["job_id"] = job_id
    payload = json.dumps(basis, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _build_batch_item_key(request: ConvertRequest, item_index: int) -> str:
    basis: dict[str, Any] = {
        "item_index": item_index,
        "template": request.template,
        "mode": request.mode,
        "auto_extract": request.auto_extract,
        "has_extract_schema": bool(request.extract_schema),
    }
    if request.path:
        basis.update({"source_type": "file", "source_ref": request.path})
    elif request.base64 and request.filename:
        basis.update(
            {
                "source_type": "base64",
                "filename": request.filename,
                "content_sha256": hashlib.sha256(request.base64.encode("utf-8")).hexdigest(),
            }
        )
    elif request.url:
        basis.update({"source_type": "url", "source_ref": request.url})
    payload = json.dumps(basis, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _build_batch_idempotency_key(request: BatchCreateRequest, queue_name: str) -> Optional[str]:
    if request.idempotency_key:
        return str(request.idempotency_key)
    basis = {
        "batch_ref": request.batch_ref,
        "queue_name": queue_name,
        "meta": request.meta,
        "items": [_build_batch_item_key(item, index) for index, item in enumerate(request.items)],
    }
    payload = json.dumps(basis, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _build_batch_item_artifacts(request: ConvertRequest) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    source_type, source_ref = _infer_execution_source(request)
    filename = request.filename or (Path(request.path).name if request.path else None) or source_ref or "upload"
    if request.path:
        resolved = resolve_path(request.path)
        file_data = resolved.read_bytes()
        document_identity = _build_document_identity(
            file_data=file_data,
            filename=filename,
            source=str(resolved),
            source_type=source_type,
        )
        input_snapshot = _build_input_snapshot(
            source=str(resolved),
            source_type=source_type,
            filename=filename,
            document_identity=document_identity,
            input_meta=request.meta,
        )
        return document_identity, input_snapshot
    if request.base64 and request.filename:
        file_data = base64.b64decode(request.base64)
        document_identity = _build_document_identity(
            file_data=file_data,
            filename=filename,
            source=filename,
            source_type=source_type,
        )
        input_snapshot = _build_input_snapshot(
            source=filename,
            source_type=source_type,
            filename=filename,
            document_identity=document_identity,
            input_meta=request.meta,
        )
        return document_identity, input_snapshot
    if request.url:
        input_snapshot = _build_input_snapshot(
            source=source_ref or filename,
            source_type=source_type,
            filename=filename,
            document_identity=None,
            input_meta=request.meta,
        )
        return None, input_snapshot
    return None, None


def _merge_execution_policy_context(execution_id: Optional[str], **sections: dict[str, Any]) -> None:
    if not execution_id:
        return
    existing = _get("execution_get", execution_get)(execution_id)
    current_policy = dict(existing.get("policy_context") or {}) if existing else {}
    for key, value in sections.items():
        if value is not None:
            current_policy[key] = value
    _get("execution_update", execution_update)(execution_id, policy_context=current_policy)


def _resolve_dispatch_policy(
    *,
    request: ConvertRequest,
    execution_kind: str,
    batch_item_count: Optional[int] = None,
) -> dict[str, Any]:
    source_type, source_ref = _infer_execution_source(request)
    if execution_kind == "direct":
        return {
            "preferred_dispatch_target": "direct",
            "effective_dispatch_target": "direct",
            "reason": "sync_request_path",
            "source_type": source_type,
            "source_ref": source_ref,
            "mistral_batch_enabled": bool(_get("MISTRAL_BATCH_ENABLED", MISTRAL_BATCH_ENABLED)),
        }
    if execution_kind == "async":
        return {
            "preferred_dispatch_target": "queued",
            "effective_dispatch_target": "queued",
            "reason": "async_request_path",
            "source_type": source_type,
            "source_ref": source_ref,
            "mistral_batch_enabled": bool(_get("MISTRAL_BATCH_ENABLED", MISTRAL_BATCH_ENABLED)),
        }

    item_count = int(batch_item_count or 0)
    enabled = bool(_get("MISTRAL_BATCH_ENABLED", MISTRAL_BATCH_ENABLED))
    min_items = int(_get("MISTRAL_BATCH_MIN_ITEMS", MISTRAL_BATCH_MIN_ITEMS))
    poll_interval_seconds = float(_get("MISTRAL_BATCH_POLL_INTERVAL_SECONDS", MISTRAL_BATCH_POLL_INTERVAL_SECONDS))
    max_active = int(_get("MISTRAL_BATCH_MAX_ACTIVE", MISTRAL_BATCH_MAX_ACTIVE))
    timeout_hours = int(_get("MISTRAL_BATCH_TIMEOUT_HOURS", MISTRAL_BATCH_TIMEOUT_HOURS))
    allowed_source_types = tuple(_get("MISTRAL_BATCH_ALLOWED_SOURCE_TYPES", MISTRAL_BATCH_ALLOWED_SOURCE_TYPES))
    eligible = enabled and item_count >= min_items and (source_type or "") in allowed_source_types
    preferred_target = "mistral_batch" if eligible else "queued"
    reason = "threshold_met_for_provider_batch" if eligible else "local_queue_selected"
    fallback_reason = "provider_batch_submission_lands_in_later_wave" if eligible else None
    return {
        "preferred_dispatch_target": preferred_target,
        "effective_dispatch_target": "queued",
        "reason": reason,
        "fallback_reason": fallback_reason,
        "source_type": source_type,
        "source_ref": source_ref,
        "batch_item_count": item_count,
        "mistral_batch_enabled": enabled,
        "mistral_batch_min_items": min_items,
        "mistral_batch_poll_interval_seconds": poll_interval_seconds,
        "mistral_batch_max_active": max_active,
        "mistral_batch_timeout_hours": timeout_hours,
        "mistral_batch_allowed_source_types": list(allowed_source_types),
    }


def _ensure_execution_for_request(
    request: ConvertRequest,
    *,
    execution_kind: str,
    job_id: Optional[str] = None,
) -> tuple[str, str]:
    request_id = request.meta.get("_request_id") or str(uuid.uuid4())
    idempotency_key = _build_request_idempotency_key(request, execution_kind, job_id=job_id)
    existing = _get("execution_get_by_request_id", execution_get_by_request_id)(request_id)
    if existing:
        execution_id = existing["id"]
        _get("execution_update", execution_update)(
            execution_id,
            status=existing.get("status") or "queued",
            current_stage=existing.get("current_stage") or "queued",
        )
    elif idempotency_key and _get("execution_get_by_idempotency_key", execution_get_by_idempotency_key)(idempotency_key):
        existing = _get("execution_get_by_idempotency_key", execution_get_by_idempotency_key)(idempotency_key)
        execution_id = existing["id"]
        _get("execution_update", execution_update)(
            execution_id,
            status=existing.get("status") or "queued",
            current_stage=existing.get("current_stage") or "queued",
            idempotency_key=idempotency_key,
        )
    else:
        source_type, source_ref = _infer_execution_source(request)
        created = _get("execution_create", execution_create)(
            execution_id=str(uuid.uuid4()),
            request_id=request_id,
            idempotency_key=idempotency_key,
            execution_kind=execution_kind,
            source_type=source_type,
            source_ref=source_ref,
            job_id=job_id,
            status="queued",
            current_stage="queued",
            policy_context={
                "mode": request.mode,
                "classify": request.classify,
                "auto_extract": request.auto_extract,
                "template": request.template,
                "has_extract_schema": bool(request.extract_schema),
            },
        )
        execution_id = created["id"]
    request.meta["_request_id"] = request_id
    request.meta["execution_id"] = execution_id
    if idempotency_key:
        request.meta["idempotency_key"] = idempotency_key
    if job_id:
        request.meta["_job_id"] = job_id
    return request_id, execution_id


def _execution_result_id(execution_id: str, *, kind: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{execution_id}:result:{kind}"))


def _build_execution_status_response(row: dict[str, Any]) -> ExecutionStatusResponse:
    progress = None
    if row.get("progress_json"):
        try:
            normalized = normalize_progress_payload(row["progress_json"])
            progress = ProgressState(**normalized) if normalized else None
        except Exception:
            progress = None
    result_meta_summary = None
    final_result_summary = row.get("final_result_summary")
    final_meta = final_result_summary.get("meta") if isinstance(final_result_summary, dict) else None
    if isinstance(final_meta, dict):
        result_meta_summary = {
            "document_type": final_meta.get("document_type"),
            "document_type_confidence": final_meta.get("document_type_confidence"),
            "template_used": final_meta.get("template_used"),
            "template_version": final_meta.get("template_version"),
            "quality_score": final_meta.get("quality_score"),
            "quality_grade": final_meta.get("quality_grade"),
            "retry_applied": final_meta.get("retry_applied"),
            "retry_reason": final_meta.get("retry_reason"),
            "initial_mode": final_meta.get("initial_mode"),
            "final_mode": final_meta.get("final_mode"),
            "initial_quality_score": final_meta.get("initial_quality_score"),
            "final_quality_score": final_meta.get("final_quality_score"),
        }
    attempts = [
        ExecutionAttemptResponse(
            attempt_id=attempt["id"],
            attempt_number=attempt["attempt_number"],
            attempt_mode=attempt.get("attempt_mode"),
            attempt_reason=attempt.get("attempt_reason"),
            status=attempt["status"],
            quality_score=attempt.get("quality_score"),
            retry_trigger=attempt.get("retry_trigger"),
            error=attempt.get("error"),
            created_at=attempt["created_at"],
            updated_at=attempt["updated_at"],
            started_at=attempt.get("started_at"),
            finished_at=attempt.get("finished_at"),
        )
        for attempt in row.get("attempts", [])
    ]
    subjobs = [
        ExecutionSubjobResponse(
            subjob_id=subjob["id"],
            provider=subjob["provider"],
            subjob_type=subjob["subjob_type"],
            upstream_batch_id=subjob.get("upstream_batch_id"),
            upstream_item_id=subjob.get("upstream_item_id"),
            subjob_status=subjob["subjob_status"],
            metadata=subjob.get("metadata"),
            error=subjob.get("error"),
            created_at=subjob["created_at"],
            updated_at=subjob["updated_at"],
            started_at=subjob.get("started_at"),
            finished_at=subjob.get("finished_at"),
        )
        for subjob in row.get("subjobs", [])
    ]
    return ExecutionStatusResponse(
        execution_id=row["id"],
        request_id=row["request_id"],
        idempotency_key=row.get("idempotency_key"),
        execution_kind=row["execution_kind"],
        source_type=row.get("source_type"),
        source_ref=row.get("source_ref"),
        job_id=row.get("job_id"),
        batch_id=row.get("batch_id"),
        batch_item_id=row.get("batch_item_id"),
        status=row["status"],
        current_stage=row.get("current_stage"),
        progress=progress,
        result_meta_summary=result_meta_summary,
        result_artifact_refs=final_result_summary.get("artifact_refs") if isinstance(final_result_summary, dict) else None,
        document_identity=row.get("document_identity"),
        input_snapshot=row.get("input_snapshot"),
        policy_context=row.get("policy_context"),
        warning_summary=row.get("warning_summary"),
        error_summary=row.get("error_summary"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        started_at=row.get("started_at"),
        finished_at=row.get("finished_at"),
        attempts=attempts,
        subjobs=subjobs,
        final_result_available=bool(final_result_summary),
    )


def _get_execution_status_payload(execution_id: str) -> ExecutionStatusResponse:
    row = _get("execution_get_full", execution_get_full)(execution_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"Execution '{execution_id}' not found")
    return _build_execution_status_response(row)


def _get_execution_result_payload(execution_id: str) -> ConvertResponse:
    execution = _get("execution_get", execution_get)(execution_id)
    if not execution:
        raise HTTPException(status_code=404, detail=f"Execution '{execution_id}' not found")
    final_result = _get("execution_result_get_final", execution_result_get_final)(execution_id)
    if not final_result or not final_result.get("response_json"):
        raise HTTPException(status_code=404, detail=f"Execution '{execution_id}' has no final result")
    return ConvertResponse.model_validate(final_result["response_json"])


def _pick_replay_snapshot_for_request(request_id: str, *, snapshot_id: Optional[int] = None) -> dict[str, Any]:
    if snapshot_id is not None:
        snapshot = _get("debug_snapshot_get", debug_snapshot_get)(snapshot_id)
        if not snapshot:
            raise HTTPException(status_code=404, detail=f"Snapshot '{snapshot_id}' not found")
        if snapshot.get("request_id") != request_id:
            raise HTTPException(status_code=409, detail=f"Snapshot '{snapshot_id}' does not belong to request '{request_id}'")
        return snapshot

    snapshots = _get("debug_snapshot_list", debug_snapshot_list)(request_id=request_id, limit=20)
    if not snapshots:
        raise HTTPException(status_code=404, detail=f"Execution request '{request_id}' has no stored replay snapshots")

    preferred_stages = ("normalized_result", "extract_result", "convert_result", "error_result")
    for stage in preferred_stages:
        for snapshot in snapshots:
            if snapshot.get("stage") == stage:
                return snapshot
    return snapshots[0]


def _build_replay_start_response(
    *,
    replay_execution: dict[str, Any],
    snapshot: dict[str, Any],
    source_execution: dict[str, Any],
    source_batch_id: Optional[str] = None,
    source_batch_item_id: Optional[str] = None,
) -> ReplayStartResponse:
    replay_execution_id = replay_execution["id"]
    return ReplayStartResponse(
        execution_id=replay_execution_id,
        request_id=replay_execution["request_id"],
        execution_kind=replay_execution["execution_kind"],
        status=replay_execution["status"],
        snapshot_id=int(snapshot["id"]),
        snapshot_stage=snapshot["stage"],
        source_execution_id=source_execution["id"],
        source_batch_id=source_batch_id,
        source_batch_item_id=source_batch_item_id,
        status_path=f"/v1/executions/{replay_execution_id}",
        result_path=f"/v1/executions/{replay_execution_id}/result",
    )


def _run_execution_replay(
    source_execution: dict[str, Any],
    *,
    snapshot_id: Optional[int] = None,
    source_batch_id: Optional[str] = None,
    source_batch_item_id: Optional[str] = None,
) -> ReplayStartResponse:
    snapshot = _pick_replay_snapshot_for_request(source_execution["request_id"], snapshot_id=snapshot_id)
    replay_execution_id = str(uuid.uuid4())
    replay_request_id = f"replay-{uuid.uuid4()}"
    replay_progress = build_progress_payload(
        status="processing",
        current_stage="replay",
        message="Replaying persisted snapshot",
        percent=20,
        request_id=replay_request_id,
        attempt_number=1,
        attempt_count=1,
        attempt_mode="replay",
    )
    replay_policy_context = {
        "replay": {
            "source_execution_id": source_execution["id"],
            "source_request_id": source_execution["request_id"],
            "source_batch_id": source_batch_id,
            "source_batch_item_id": source_batch_item_id,
            "snapshot_id": snapshot["id"],
            "snapshot_stage": snapshot.get("stage"),
            "mode": "snapshot",
        }
    }
    replay_execution = _get("execution_create", execution_create)(
        execution_id=replay_execution_id,
        request_id=replay_request_id,
        execution_kind="replay",
        source_type=source_execution.get("source_type"),
        source_ref=source_execution.get("source_ref"),
        document_identity=source_execution.get("document_identity"),
        input_snapshot=source_execution.get("input_snapshot"),
        policy_context=replay_policy_context,
        status="processing",
        current_stage="replay",
        progress_json=replay_progress,
    )
    replay_meta = {
        "request_id": replay_request_id,
        "execution_id": replay_execution_id,
        "attempt_number": 1,
        "attempt_count": 1,
        "attempt_mode": "replay",
        "replayed_from_request_id": source_execution["request_id"],
        "replayed_from_execution_id": source_execution["id"],
    }
    if source_batch_id:
        replay_meta["replayed_from_batch_id"] = source_batch_id
    if source_batch_item_id:
        replay_meta["replayed_from_batch_item_id"] = source_batch_item_id

    try:
        response = _get("build_convert_response_from_snapshot", build_convert_response_from_snapshot)(
            snapshot,
            replay_meta=replay_meta,
        )
    except ValueError as exc:
        response = create_error_response(
            ErrorCode.INTERNAL_ERROR,
            str(exc),
            meta=replay_meta,
            details={"snapshot_id": snapshot["id"]},
        )

    _get("execution_update", execution_update)(
        replay_execution_id,
        started_at_now=True,
        current_stage="replay",
        progress_json=replay_progress,
    )
    _persist_execution_attempt_result(
        execution_id=replay_execution_id,
        attempt_number=1,
        response=response,
        attempt_mode="replay",
        attempt_reason=f"snapshot:{snapshot.get('stage')}",
        is_final=True,
        result_status="completed" if response.success else "failed",
    )
    refreshed = _get("execution_get", execution_get)(replay_execution_id)
    if not refreshed:
        raise HTTPException(status_code=500, detail=f"Replay execution '{replay_execution_id}' could not be reloaded")
    return _build_replay_start_response(
        replay_execution=refreshed,
        snapshot=snapshot,
        source_execution=source_execution,
        source_batch_id=source_batch_id,
        source_batch_item_id=source_batch_item_id,
    )


def _replay_execution(execution_id: str, *, snapshot_id: Optional[int] = None) -> ReplayStartResponse:
    source_execution = _get("execution_get", execution_get)(execution_id)
    if not source_execution:
        raise HTTPException(status_code=404, detail=f"Execution '{execution_id}' not found")
    return _run_execution_replay(source_execution, snapshot_id=snapshot_id)


def _replay_batch_item(batch_id: str, batch_item_id: str, *, snapshot_id: Optional[int] = None) -> ReplayStartResponse:
    batch_item = _require_batch_item_row(batch_id, batch_item_id)
    source_execution = _get("execution_get", execution_get)(batch_item["execution_id"])
    if not source_execution:
        raise HTTPException(status_code=404, detail=f"Execution '{batch_item['execution_id']}' not found")
    return _run_execution_replay(
        source_execution,
        snapshot_id=snapshot_id,
        source_batch_id=batch_id,
        source_batch_item_id=batch_item_id,
    )


def _get_job_result_payload(job_id: str) -> ConvertResponse:
    _job_get = _get("job_get", job_get)
    job = _job_get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    if job["status"] != "completed":
        if job["status"] == "failed" and job.get("result_json"):
            return ConvertResponse.model_validate_json(job["result_json"])
        raise HTTPException(
            status_code=202,
            detail=f"Job '{job_id}' is not completed yet (status: {job['status']})"
        )
    if job.get("result_json"):
        return ConvertResponse.model_validate_json(job["result_json"])
    execution = _get("execution_get_by_job_id", execution_get_by_job_id)(job_id)
    if execution:
        final_result = _get("execution_result_get_final", execution_result_get_final)(execution["id"])
    if final_result and final_result.get("response_json"):
        return ConvertResponse.model_validate(final_result["response_json"])
    raise HTTPException(status_code=500, detail=f"Job '{job_id}' has no result data")


def _build_batch_status_response(row: dict[str, Any]) -> BatchStatusResponse:
    active_items = [
        BatchActiveItemResponse(
            batch_item_id=item["batch_item_id"],
            item_index=item["item_index"],
            execution_id=item.get("execution_id"),
            filename=item.get("filename"),
            status=item["status"],
            current_stage=item.get("current_stage"),
        )
        for item in row.get("active_items", [])
    ]
    return BatchStatusResponse(
        batch_id=row["id"],
        batch_ref=row.get("batch_ref"),
        queue_name=row["queue_name"],
        status=row["status"],
        item_count=row.get("item_count") or 0,
        queued_count=row.get("queued_count") or 0,
        processing_count=row.get("processing_count") or 0,
        completed_count=row.get("completed_count") or 0,
        failed_count=row.get("failed_count") or 0,
        cancelled_count=row.get("cancelled_count") or 0,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        started_at=row.get("started_at"),
        finished_at=row.get("finished_at"),
        metadata=row.get("metadata"),
        active_items=active_items,
    )


def _get_batch_status_payload(batch_id: str, *, active_item_limit: int = BATCH_STATUS_ACTIVE_ITEM_LIMIT) -> BatchStatusResponse:
    row = _get("execution_batch_status_summary", execution_batch_status_summary)(
        batch_id,
        active_item_limit=active_item_limit,
    )
    if not row:
        raise HTTPException(status_code=404, detail=f"Batch '{batch_id}' not found")
    return _build_batch_status_response(row)


def _build_batch_item_response(row: dict[str, Any]) -> BatchItemResponse:
    return BatchItemResponse(
        batch_item_id=row["id"],
        batch_id=row["batch_id"],
        item_index=row["item_index"],
        execution_id=row.get("execution_id"),
        request_id=row["request_id"],
        filename=row.get("filename"),
        source_type=row.get("source_type"),
        source_ref=row.get("source_ref"),
        status=row.get("effective_status") or row["status"],
        current_stage=row.get("current_stage"),
        metadata=row.get("metadata"),
        result_artifact_refs=row.get("result_artifact_refs"),
        document_identity=row.get("document_identity"),
        input_snapshot=row.get("input_snapshot"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        final_result_available=bool(row.get("final_result_available")),
    )


def _build_queued_progress_payload(
    *,
    request: ConvertRequest,
    job_id: Optional[str],
    message: str,
) -> dict[str, Any]:
    return build_progress_payload(
        status="queued",
        current_stage="queued",
        message=message,
        percent=0,
        request_id=request.meta.get("_request_id"),
        job_id=job_id,
    )


def _run_coro_blocking(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _next_attempt_number(execution_id: str) -> int:
    execution = _get("execution_get_full", execution_get_full)(execution_id)
    attempts = execution.get("attempts") if execution else []
    return len(attempts or []) + 1


def _resolve_request_bytes(request: ConvertRequest) -> tuple[bytes, str]:
    if request.path:
        file_path = resolve_path(request.path)
        return file_path.read_bytes(), file_path.name
    if request.base64:
        return base64.b64decode(request.base64), request.filename or "upload"
    raise ValueError("Provider batch submission requires file or base64 inputs")


def _batch_request_is_provider_compatible(request: ConvertRequest) -> bool:
    if request.url:
        return False
    if request.output_format and request.output_format != "markdown":
        return False
    if request.classify or request.extract_schema or request.auto_extract or request.template:
        return False
    if request.chunk or request.ocr_correct or request.describe_images or request.prompt:
        return False
    return True


def _apply_dispatch_fallback(execution_id: str, reason: str) -> None:
    execution = _get("execution_get", execution_get)(execution_id)
    policy_context = dict((execution or {}).get("policy_context") or {})
    dispatch_policy = dict(policy_context.get("dispatch_policy") or {})
    dispatch_policy["effective_dispatch_target"] = "queued"
    dispatch_policy["fallback_reason"] = reason
    _merge_execution_policy_context(execution_id, dispatch_policy=dispatch_policy)


def _build_mistral_success_response(
    *,
    execution_id: str,
    request: ConvertRequest,
    ocr_result: dict[str, Any],
) -> ConvertResponse:
    pages = ocr_result.get("pages") or []
    markdown_parts: list[str] = []
    for index, page in enumerate(pages):
        page_index = int(page.get("index", index)) + 1
        markdown_parts.append(f"## Seite {page_index}\n\n{page.get('markdown', '')}")
    execution = _get("execution_get", execution_get)(execution_id) or {}
    meta = {
        "request_id": execution.get("request_id"),
        "execution_id": execution_id,
        "source": execution.get("source_ref"),
        "source_type": execution.get("source_type"),
        "ocr_model": _get("MISTRAL_OCR_MODEL", MISTRAL_OCR_MODEL),
        "pipeline_steps": ["mistral_batch_ocr"],
        "accuracy_mode": request.accuracy,
        "pages": len(pages) or None,
        **_get("extract_mistral_ocr_metadata", extract_mistral_ocr_metadata)(ocr_result),
    }
    return create_success_response("\n\n".join(markdown_parts).strip(), meta=meta)


def _build_mistral_error_response(
    *,
    execution_id: str,
    message: str,
    code: str = ErrorCode.API_ERROR,
    details: Optional[dict[str, Any]] = None,
) -> ConvertResponse:
    execution = _get("execution_get", execution_get)(execution_id) or {}
    meta = {
        "request_id": execution.get("request_id"),
        "execution_id": execution_id,
        "source": execution.get("source_ref"),
        "source_type": execution.get("source_type"),
        "pipeline_steps": ["mistral_batch_ocr"],
    }
    return create_error_response(code, message, meta=meta, details=details)


async def _submit_mistral_provider_batch(
    *,
    batch_id: str,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    requests: list[dict[str, Any]] = []
    for item in items:
        file_data, filename = _resolve_request_bytes(item["request"])
        requests.append(
            _get("build_mistral_batch_ocr_request", build_mistral_batch_ocr_request)(
                file_data,
                filename,
                custom_id=item["batch_item_id"],
            )
        )
    provider_job = await _get("submit_mistral_batch_job", submit_mistral_batch_job)(
        requests,
        endpoint="/v1/ocr",
        model=_get("MISTRAL_OCR_MODEL", MISTRAL_OCR_MODEL),
        metadata={
            "daigestr_batch_id": batch_id,
            "provider": "mistral",
            "subjob_type": "mistral_batch",
        },
        timeout_hours=int(_get("MISTRAL_BATCH_TIMEOUT_HOURS", MISTRAL_BATCH_TIMEOUT_HOURS)),
    )
    upstream_batch_id = provider_job["id"]
    for item in items:
        execution_id = item["execution_id"]
        request = item["request"]
        metadata = {
            "decision_only": False,
            "dispatch_policy": item["dispatch_policy"],
            "request_payload": request.model_dump(mode="json"),
            "provider_job": provider_job,
        }
        _get("execution_subjob_upsert", execution_subjob_upsert)(
            subjob_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{execution_id}:{upstream_batch_id}")),
            execution_id=execution_id,
            batch_id=batch_id,
            batch_item_id=item["batch_item_id"],
            provider="mistral",
            subjob_type="mistral_batch",
            upstream_batch_id=upstream_batch_id,
            upstream_item_id=item["batch_item_id"],
            subjob_status="submitted",
            metadata=metadata,
        )
        dispatch_policy = dict(item["dispatch_policy"])
        dispatch_policy["effective_dispatch_target"] = "mistral_batch"
        dispatch_policy["fallback_reason"] = None
        _merge_execution_policy_context(execution_id, dispatch_policy=dispatch_policy)
        _get("execution_update", execution_update)(
            execution_id,
            status="processing",
            current_stage="provider_batch_submitted",
            progress_json=build_progress_payload(
                status="processing",
                current_stage="provider_batch_submitted",
                message="Submitted to provider batch",
                percent=5,
                request_id=request.meta.get("_request_id"),
            ),
            error_summary=None,
        )
    return provider_job


async def _poll_mistral_batch_job(upstream_batch_id: str) -> None:
    subjobs = _get("execution_subjob_list_by_upstream_batch_id", execution_subjob_list_by_upstream_batch_id)(upstream_batch_id)
    if not subjobs:
        return
    provider_job = await _get("get_mistral_batch_job", get_mistral_batch_job)(upstream_batch_id)
    provider_status = str(provider_job.get("status") or "").upper()
    provider_meta = {"provider_job": provider_job, "provider_status": provider_status}

    if provider_status in {"QUEUED", "RUNNING", "CANCELLATION_REQUESTED"}:
        subjob_status = "processing" if provider_status == "RUNNING" else "submitted"
        for subjob in subjobs:
            metadata = dict(subjob.get("metadata") or {})
            metadata.update(provider_meta)
            _get("execution_subjob_upsert", execution_subjob_upsert)(
                subjob_id=subjob["id"],
                execution_id=subjob["execution_id"],
                batch_id=subjob.get("batch_id"),
                batch_item_id=subjob.get("batch_item_id"),
                provider=subjob["provider"],
                subjob_type=subjob["subjob_type"],
                upstream_batch_id=upstream_batch_id,
                upstream_item_id=subjob.get("upstream_item_id"),
                subjob_status=subjob_status,
                metadata=metadata,
                error=subjob.get("error"),
                started_at_now=(provider_status == "RUNNING"),
            )
        return

    output_rows: dict[str, dict[str, Any]] = {}
    error_rows: dict[str, dict[str, Any]] = {}
    if provider_job.get("output_file"):
        output_rows = _get("parse_mistral_batch_output", parse_mistral_batch_output)(
            await _get("download_mistral_file", download_mistral_file)(provider_job["output_file"])
        )
    if provider_job.get("error_file"):
        error_rows = _get("parse_mistral_batch_output", parse_mistral_batch_output)(
            await _get("download_mistral_file", download_mistral_file)(provider_job["error_file"])
        )

    for subjob in subjobs:
        execution_id = subjob["execution_id"]
        custom_id = str(subjob.get("upstream_item_id") or subjob.get("batch_item_id"))
        metadata = dict(subjob.get("metadata") or {})
        metadata.update(provider_meta)
        request = ConvertRequest.model_validate((metadata.get("request_payload") or {}))
        attempt_number = _next_attempt_number(execution_id)
        attempt_reason = "initial" if attempt_number == 1 else "retry"
        output_entry = output_rows.get(custom_id)
        error_entry = error_rows.get(custom_id)
        if output_entry:
            response = _build_mistral_success_response(
                execution_id=execution_id,
                request=request,
                ocr_result=((output_entry.get("response") or {}).get("body") or {}),
            )
            _persist_execution_attempt_result(
                execution_id=execution_id,
                attempt_number=attempt_number,
                attempt_mode=request.mode,
                attempt_reason=attempt_reason,
                response=response,
                is_final=True,
                result_status="completed",
            )
            _get("execution_subjob_upsert", execution_subjob_upsert)(
                subjob_id=subjob["id"],
                execution_id=execution_id,
                batch_id=subjob.get("batch_id"),
                batch_item_id=subjob.get("batch_item_id"),
                provider=subjob["provider"],
                subjob_type=subjob["subjob_type"],
                upstream_batch_id=upstream_batch_id,
                upstream_item_id=subjob.get("upstream_item_id"),
                subjob_status="completed",
                metadata=metadata,
                finished_at_now=True,
            )
            continue

        message = (
            ((error_entry or {}).get("error") or {}).get("message")
            or (error_entry or {}).get("message")
            or f"Provider batch ended with status {provider_status}"
        )
        error_response = _build_mistral_error_response(
            execution_id=execution_id,
            message=message,
            details={"provider_status": provider_status, "provider_error": error_entry},
        )
        _persist_execution_attempt_result(
            execution_id=execution_id,
            attempt_number=attempt_number,
            attempt_mode=request.mode,
            attempt_reason=attempt_reason,
            response=error_response,
            is_final=True,
            result_status="failed" if provider_status != "CANCELLED" else "cancelled",
        )
        _get("execution_subjob_upsert", execution_subjob_upsert)(
            subjob_id=subjob["id"],
            execution_id=execution_id,
            batch_id=subjob.get("batch_id"),
            batch_item_id=subjob.get("batch_item_id"),
            provider=subjob["provider"],
            subjob_type=subjob["subjob_type"],
            upstream_batch_id=upstream_batch_id,
            upstream_item_id=subjob.get("upstream_item_id"),
            subjob_status="failed" if provider_status != "CANCELLED" else "cancelled",
            metadata=metadata,
            error={"message": message, "provider_status": provider_status},
            finished_at_now=True,
        )


async def _mistral_batch_poll_loop(worker_id: str) -> None:
    while True:
        try:
            subjobs = _get("execution_subjob_list_by_status", execution_subjob_list_by_status)(
                statuses=["submitted", "processing"],
                provider="mistral",
                subjob_type="mistral_batch",
                limit=int(_get("MISTRAL_BATCH_MAX_ACTIVE", MISTRAL_BATCH_MAX_ACTIVE)),
            )
            upstream_batch_ids = sorted({row.get("upstream_batch_id") for row in subjobs if row.get("upstream_batch_id")})
            for upstream_batch_id in upstream_batch_ids:
                await _poll_mistral_batch_job(upstream_batch_id)
            await asyncio.sleep(float(_get("MISTRAL_BATCH_POLL_INTERVAL_SECONDS", MISTRAL_BATCH_POLL_INTERVAL_SECONDS)))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("mistral_batch_poller_error", worker_id=worker_id, error=str(exc))
            await asyncio.sleep(float(_get("MISTRAL_BATCH_POLL_INTERVAL_SECONDS", MISTRAL_BATCH_POLL_INTERVAL_SECONDS)))


def _requeue_execution(
    *,
    execution_id: str,
    request: ConvertRequest,
    queue_name: str,
    payload: dict[str, Any],
    message: str,
) -> None:
    _get("execution_result_clear_final", execution_result_clear_final)(execution_id)
    _get("execution_update", execution_update)(
        execution_id,
        status="queued",
        current_stage="queued",
        progress_json=_build_queued_progress_payload(
            request=request,
            job_id=payload.get("job_id"),
            message=message,
        ),
        error_summary={},
        warning_summary={},
    )
    _get("execution_queue_enqueue", execution_queue_enqueue)(
        queue_id=str(uuid.uuid4()),
        execution_id=execution_id,
        job_id=payload.get("job_id"),
        payload=payload,
        queue_name=queue_name,
    )


def _require_batch_item_row(batch_id: str, batch_item_id: str) -> dict[str, Any]:
    row = _get("execution_batch_item_get", execution_batch_item_get)(batch_id, batch_item_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"Batch item '{batch_item_id}' not found in batch '{batch_id}'")
    if not row.get("execution_id"):
        raise HTTPException(status_code=409, detail=f"Batch item '{batch_item_id}' has no linked execution")
    return row


def _cancel_batch_item(batch_id: str, batch_item_id: str) -> BatchItemResponse:
    batch_item = _require_batch_item_row(batch_id, batch_item_id)
    effective_status = batch_item.get("effective_status") or batch_item.get("status")
    if effective_status in {"completed", "failed"}:
        raise HTTPException(status_code=409, detail=f"Batch item '{batch_item_id}' is already terminal with status '{effective_status}'")

    execution_id = batch_item["execution_id"]
    _get("execution_queue_cancel", execution_queue_cancel)(execution_id)
    _get("execution_update", execution_update)(
        execution_id,
        status="cancelled",
        current_stage="cancelled",
        progress_json=build_progress_payload(
            status="cancelled",
            current_stage="cancelled",
            message="Batch item cancelled",
            percent=100,
            request_id=batch_item["request_id"],
        ),
        finished_at_now=True,
    )
    refreshed = _require_batch_item_row(batch_id, batch_item_id)
    return _build_batch_item_response(refreshed)


def _resume_batch_item(batch_id: str, batch_item_id: str) -> BatchItemResponse:
    batch_item = _require_batch_item_row(batch_id, batch_item_id)
    effective_status = batch_item.get("effective_status") or batch_item.get("status")
    if effective_status != "cancelled":
        raise HTTPException(status_code=409, detail=f"Batch item '{batch_item_id}' is not cancelled")

    execution_id = batch_item["execution_id"]
    queue_row = _get("execution_queue_get_by_execution_id", execution_queue_get_by_execution_id)(execution_id)
    if not queue_row or not isinstance(queue_row.get("payload"), dict):
        raise HTTPException(status_code=409, detail=f"Batch item '{batch_item_id}' has no persisted queue payload to resume")

    request_payload = queue_row["payload"].get("request")
    if not isinstance(request_payload, dict):
        raise HTTPException(status_code=409, detail=f"Batch item '{batch_item_id}' has no valid persisted request payload")

    request = ConvertRequest(**request_payload)
    _requeue_execution(
        execution_id=execution_id,
        request=request,
        queue_name=queue_row.get("queue_name") or "default",
        payload=queue_row["payload"],
        message="Batch item resumed and queued for worker execution",
    )
    refreshed = _require_batch_item_row(batch_id, batch_item_id)
    return _build_batch_item_response(refreshed)


def _retry_batch_item(batch_id: str, batch_item_id: str) -> BatchItemResponse:
    batch_item = _require_batch_item_row(batch_id, batch_item_id)
    effective_status = batch_item.get("effective_status") or batch_item.get("status")
    if effective_status != "failed":
        raise HTTPException(status_code=409, detail=f"Batch item '{batch_item_id}' is not failed")

    execution_id = batch_item["execution_id"]
    subjob = _get("execution_subjob_get", execution_subjob_get)(execution_id)
    if subjob and subjob.get("subjob_type") == "mistral_batch":
        request_payload = (subjob.get("metadata") or {}).get("request_payload")
        if not isinstance(request_payload, dict):
            raise HTTPException(status_code=409, detail=f"Batch item '{batch_item_id}' has no persisted provider request payload")
        request = ConvertRequest(**request_payload)
        _get("execution_result_clear_final", execution_result_clear_final)(execution_id)
        coro = _submit_mistral_provider_batch(
            batch_id=batch_id,
            items=[{
                "batch_item_id": batch_item_id,
                "execution_id": execution_id,
                "request": request,
                "dispatch_policy": (((_get("execution_get", execution_get)(execution_id) or {}).get("policy_context") or {}).get("dispatch_policy") or {}),
            }],
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            _run_coro_blocking(coro)
        else:
            loop.create_task(coro)
    else:
        queue_row = _get("execution_queue_get_by_execution_id", execution_queue_get_by_execution_id)(execution_id)
        if not queue_row or not isinstance(queue_row.get("payload"), dict):
            raise HTTPException(status_code=409, detail=f"Batch item '{batch_item_id}' has no persisted queue payload to retry")

        request_payload = queue_row["payload"].get("request")
        if not isinstance(request_payload, dict):
            raise HTTPException(status_code=409, detail=f"Batch item '{batch_item_id}' has no valid persisted request payload")

        request = ConvertRequest(**request_payload)
        _requeue_execution(
            execution_id=execution_id,
            request=request,
            queue_name=queue_row.get("queue_name") or "default",
            payload=queue_row["payload"],
            message="Batch item retry queued for worker execution",
        )
    refreshed = _require_batch_item_row(batch_id, batch_item_id)
    return _build_batch_item_response(refreshed)


def _cancel_batch(batch_id: str) -> BatchStatusResponse:
    batch = _get("execution_batch_get", execution_batch_get)(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail=f"Batch '{batch_id}' not found")
    for item in _get("execution_batch_item_list", execution_batch_item_list)(batch_id):
        item_row = _get("execution_batch_item_get", execution_batch_item_get)(batch_id, item["id"])
        effective_status = (item_row or item).get("effective_status") or item["status"]
        if effective_status in {"queued", "processing"}:
            _cancel_batch_item(batch_id, item["id"])
    return _get_batch_status_payload(batch_id)


def _resume_batch(batch_id: str) -> BatchStatusResponse:
    batch = _get("execution_batch_get", execution_batch_get)(batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail=f"Batch '{batch_id}' not found")
    for item in _get("execution_batch_item_list", execution_batch_item_list)(batch_id):
        item_row = _get("execution_batch_item_get", execution_batch_item_get)(batch_id, item["id"])
        effective_status = (item_row or item).get("effective_status") or item["status"]
        if effective_status == "cancelled":
            _resume_batch_item(batch_id, item["id"])
    return _get_batch_status_payload(batch_id)


def _get_batch_items_payload(batch_id: str, *, limit: int = 50, offset: int = 0) -> BatchItemListResponse:
    if limit < 1:
        raise HTTPException(status_code=400, detail="limit must be >= 1")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be >= 0")
    batch = _get("execution_batch_status_summary", execution_batch_status_summary)(batch_id, active_item_limit=0)
    if not batch:
        raise HTTPException(status_code=404, detail=f"Batch '{batch_id}' not found")
    page = _get("execution_batch_item_list_paginated", execution_batch_item_list_paginated)(
        batch_id,
        limit=limit,
        offset=offset,
    )
    return BatchItemListResponse(
        batch_id=batch_id,
        limit=page["limit"],
        offset=page["offset"],
        total_count=page["total_count"],
        items=[_build_batch_item_response(item) for item in page["items"]],
    )


def _get_batch_item_result_payload(batch_id: str, batch_item_id: str) -> ConvertResponse:
    batch_item = _get("execution_batch_item_get", execution_batch_item_get)(batch_id, batch_item_id)
    if not batch_item:
        raise HTTPException(status_code=404, detail=f"Batch item '{batch_item_id}' not found in batch '{batch_id}'")
    execution_id = batch_item.get("execution_id")
    if not execution_id:
        raise HTTPException(status_code=404, detail=f"Batch item '{batch_item_id}' has no linked execution")
    return _get_execution_result_payload(execution_id)


def _get_execution_diagnostics_payload(
    *,
    limit: int = EXECUTION_DIAGNOSTICS_LIMIT,
    stuck_after_seconds: int = EXECUTION_STUCK_THRESHOLD_SECONDS,
    drift_sample_limit: int = NORMALIZATION_DRIFT_SAMPLE_LIMIT,
) -> ExecutionDiagnosticsResponse:
    _active_rows = _get("execution_list_active", execution_list_active)(limit=limit)
    _stuck_rows = _get("execution_list_stuck", execution_list_stuck)(
        stuck_after_seconds=stuck_after_seconds,
        limit=limit,
    )
    _full_row = _get("execution_get_full", execution_get_full)
    active = [_build_execution_status_response(_full_row(row["id"]) or row) for row in _active_rows]
    stuck = [_build_execution_status_response(_full_row(row["id"]) or row) for row in _stuck_rows]
    drift = _get("get_normalization_drift_summary", get_normalization_drift_summary)(limit=drift_sample_limit)
    return ExecutionDiagnosticsResponse(
        active_count=len(active),
        stuck_count=len(stuck),
        stuck_threshold_seconds=stuck_after_seconds,
        active_executions=active,
        stuck_executions=stuck,
        normalizer_drift=drift,
    )

# FastAPI Instanz
app = FastAPI(
    title="Daigestr API",
    description="Konvertiert Dokumente und Bilder zu Markdown",
    version=VERSION,
    docs_url="/docs",
    redoc_url="/redoc",
)
_QUEUE_WORKER_TASKS: list[asyncio.Task] = []


def _safe_encode(obj: Any) -> Any:
    """Recursively converts bytes values to a safe string representation."""
    if isinstance(obj, bytes):
        try:
            return obj.decode("utf-8")
        except UnicodeDecodeError:
            return f"<binary {len(obj)} bytes>"
    if isinstance(obj, dict):
        return {k: _safe_encode(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_safe_encode(v) for v in obj]
    return obj


async def _queue_worker_loop(worker_id: str) -> None:
    """Poll and execute queued async conversion work."""
    while True:
        queue_item: Optional[dict[str, Any]] = None
        try:
            queue_item = _get("execution_queue_claim_next", execution_queue_claim_next)(
                worker_id=worker_id,
                lease_seconds=int(_get("QUEUE_LEASE_SECONDS", QUEUE_LEASE_SECONDS)),
            )
            if not queue_item:
                await asyncio.sleep(float(_get("QUEUE_POLL_INTERVAL_SECONDS", QUEUE_POLL_INTERVAL_SECONDS)))
                continue

            payload = queue_item.get("payload") or {}
            execution_id = queue_item.get("execution_id")
            if execution_id:
                execution = _get("execution_get", execution_get)(execution_id)
                if execution and execution.get("status") == "cancelled":
                    _get("execution_queue_cancel", execution_queue_cancel)(execution_id)
                    continue
            request_payload = payload.get("request") or {}
            request = ConvertRequest(**request_payload)
            await _run_async_job(queue_item.get("job_id"), request)
            job = _get("job_get", job_get)(queue_item.get("job_id"))
            if execution_id:
                execution = _get("execution_get", execution_get)(execution_id)
                if execution and execution.get("status") == "cancelled":
                    _get("execution_queue_cancel", execution_queue_cancel)(execution_id)
                    continue
            if job and job.get("status") == "completed":
                _get("execution_queue_complete", execution_queue_complete)(queue_item["id"])
            elif queue_item.get("job_id"):
                _get("execution_queue_fail", execution_queue_fail)(queue_item["id"])
            else:
                execution = _get("execution_get", execution_get)(execution_id) if execution_id else None
                if execution and execution.get("status") == "completed":
                    _get("execution_queue_complete", execution_queue_complete)(queue_item["id"])
                else:
                    _get("execution_queue_fail", execution_queue_fail)(queue_item["id"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("queue_worker_error", worker_id=worker_id, queue_item_id=queue_item["id"] if queue_item else None, error=str(exc))
            if queue_item is not None:
                _get("execution_queue_fail", execution_queue_fail)(queue_item["id"])
            await asyncio.sleep(float(_get("QUEUE_POLL_INTERVAL_SECONDS", QUEUE_POLL_INTERVAL_SECONDS)))


@app.on_event("startup")
async def startup_queue_workers() -> None:
    if not _get("QUEUE_ENABLED", QUEUE_ENABLED):
        return
    if _QUEUE_WORKER_TASKS:
        return
    worker_count = max(1, int(_get("QUEUE_WORKER_COUNT", QUEUE_WORKER_COUNT)))
    for idx in range(worker_count):
        _QUEUE_WORKER_TASKS.append(asyncio.create_task(_queue_worker_loop(f"queue-worker-{idx + 1}")))
    if _get("MISTRAL_BATCH_ENABLED", MISTRAL_BATCH_ENABLED) and not _MISTRAL_BATCH_POLL_TASKS:
        _MISTRAL_BATCH_POLL_TASKS.append(asyncio.create_task(_mistral_batch_poll_loop("mistral-batch-poller-1")))


@app.on_event("shutdown")
async def shutdown_queue_workers() -> None:
    if not _QUEUE_WORKER_TASKS:
        return
    tasks = list(_QUEUE_WORKER_TASKS)
    _QUEUE_WORKER_TASKS.clear()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    if _MISTRAL_BATCH_POLL_TASKS:
        poll_tasks = list(_MISTRAL_BATCH_POLL_TASKS)
        _MISTRAL_BATCH_POLL_TASKS.clear()
        for task in poll_tasks:
            task.cancel()
        await asyncio.gather(*poll_tasks, return_exceptions=True)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all handler für unerwartete Exceptions — verhindert Worker-Crash."""
    log.error("unhandled_exception", error=str(exc), type=type(exc).__name__, path=str(request.url))
    return JSONResponse(
        status_code=500,
        content={"success": False, "error": str(exc), "error_code": "INTERNAL_ERROR"},
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """
    Custom handler for request validation errors.

    FastAPI's default handler uses jsonable_encoder which calls bytes.decode(),
    causing UnicodeDecodeError when binary data (e.g. PDF uploads via multipart)
    appears in the error details.
    """
    safe_errors = _safe_encode(exc.errors())
    return JSONResponse(
        status_code=422,
        content={"detail": safe_errors},
    )


# Audit Router einbinden
app.include_router(audit_router)
# Normalization Admin Routers (T-DAI-056)
app.include_router(normalize_router)
app.include_router(corrections_router)
app.include_router(batch_router)


@app.post("/v1/convert", response_model=ConvertResponse)
async def api_convert(request: ConvertRequest) -> ConvertResponse:
    """
    Konvertiert eine Datei zu Markdown.
    """
    response = await _api_convert_impl(request)
    # Webhook (T-DAI-023): nach Konvertierung feuern wenn webhook_url gesetzt
    if request.webhook_url:
        await _fire_webhook(request.webhook_url, response)
    return response


async def _api_convert_impl(request: ConvertRequest) -> ConvertResponse:
    """Interne Implementierung von api_convert (ohne Webhook-Logik)."""
    # Patchable symbols via _get() for test-patchability
    _resolve_path = _get("resolve_path", resolve_path)
    _convert_auto = _get("convert_auto", convert_auto)
    _convert_url = _get("convert_url", convert_url)
    _httpx = _get("httpx", httpx)
    _classify_doc = _get("classify_document", classify_document)
    _apply_auto = _get("_apply_auto_extract", _apply_auto_extract)
    _extract_struct = _get("extract_structured_data", extract_structured_data)
    _chunk_md = _get("chunk_markdown", chunk_markdown)
    _get_template = _get("get_template_by_id", get_template_by_id)
    _temp_dir = _get("TEMP_DIR", TEMP_DIR)
    _mistral_timeout = _get("MISTRAL_TIMEOUT", MISTRAL_TIMEOUT)

    inputs = [request.path, request.base64, request.url]
    if sum(1 for x in inputs if x) != 1:
        return create_error_response(
            ErrorCode.INVALID_INPUT,
            "Genau einer von 'path', 'base64' oder 'url' muss angegeben werden",
            meta=request.meta
        )
    _ensure_execution_for_request(request, execution_kind="direct")
    _merge_execution_policy_context(
        request.meta.get("execution_id"),
        dispatch_policy=_resolve_dispatch_policy(request=request, execution_kind="direct"),
    )

    # Template → Schema Auflösung (AC-014-4)
    effective_schema = request.extract_schema
    if request.template and not effective_schema:
        tmpl = _get_template(request.template)
        if tmpl is None:
            return create_error_response(
                ErrorCode.INVALID_INPUT,
                f"Unbekanntes Template: '{request.template}'. Verfügbar: {get_all_template_ids()}",
                meta=request.meta
            )
        effective_schema = tmpl["schema"]

    # Pfad-basiert
    if request.path:
        try:
            file_path = _resolve_path(request.path)
        except PathPolicyError as exc:
            return create_error_response(
                ErrorCode.PATH_NOT_ALLOWED,
                str(exc),
                meta={**request.meta, "path_policy_reason": exc.reason},
            )
        if not file_path.exists():
            return create_error_response(
                ErrorCode.FILE_NOT_FOUND,
                f"Datei nicht gefunden: {file_path}",
                meta=request.meta
            )
        file_data = file_path.read_bytes()
        return await _convert_auto(
            file_data=file_data,
            filename=file_path.name,
            source=str(file_path),
            source_type="file",
            input_meta=request.meta,
            prompt=request.prompt,
            language=request.language,
            describe_images=request.describe_images,
            classify=request.classify,
            classify_categories=request.classify_categories,
            extract_schema=effective_schema,
            ocr_correct=request.ocr_correct,
            show_formulas=request.show_formulas,
            chunk=request.chunk,
            chunk_size=request.chunk_size,
            accuracy=request.accuracy,
            ocr_embed=request.ocr_embed,
            auto_extract=request.auto_extract,
            min_confidence=request.min_confidence,
            retry_on_low_quality=request.retry_on_low_quality,
            quality_retry_threshold=request.quality_retry_threshold,
            quality_retry_mode=request.quality_retry_mode,
            mode=request.mode,
            output_format=request.output_format,
            pages=request.pages,
            no_cache=request.no_cache,
            compact=request.compact,
            template=request.template,
        )

    # Base64
    if request.base64:
        if not request.filename:
            return create_error_response(
                ErrorCode.INVALID_INPUT,
                "'filename' ist erforderlich bei Base64-Upload",
                meta=request.meta
            )
        try:
            file_data = base64.b64decode(request.base64)
        except Exception as e:
            return create_error_response(
                ErrorCode.INVALID_BASE64,
                f"Ungültiges Base64: {str(e)}",
                meta=request.meta
            )
        return await _convert_auto(
            file_data=file_data,
            filename=request.filename,
            source="base64",
            source_type="base64",
            input_meta=request.meta,
            prompt=request.prompt,
            language=request.language,
            describe_images=request.describe_images,
            classify=request.classify,
            classify_categories=request.classify_categories,
            extract_schema=effective_schema,
            ocr_correct=request.ocr_correct,
            show_formulas=request.show_formulas,
            chunk=request.chunk,
            chunk_size=request.chunk_size,
            accuracy=request.accuracy,
            ocr_embed=request.ocr_embed,
            auto_extract=request.auto_extract,
            min_confidence=request.min_confidence,
            retry_on_low_quality=request.retry_on_low_quality,
            quality_retry_threshold=request.quality_retry_threshold,
            quality_retry_mode=request.quality_retry_mode,
            mode=request.mode,
            output_format=request.output_format,
            pages=request.pages,
            no_cache=request.no_cache,
            compact=request.compact,
            template=request.template,
        )

    # URL — T-MKIT-022: durch convert_auto() routen für vollständige Pipeline
    if request.url:
        # Content-Type → Extension ermitteln; HTML-Seiten direkt mit convert_url() (markitdown-native)
        try:
            async with _httpx.AsyncClient(timeout=float(_mistral_timeout)) as client:
                head_resp = await client.head(request.url, follow_redirects=True)
                url_content_type = head_resp.headers.get("content-type", "text/html")
        except Exception:
            url_content_type = "text/html"

        # Prüfen ob die URL eine HTML-Seite ist → alter Pfad (markitdown convert_url ist besser für HTML)
        is_html = url_content_type.split(";")[0].strip() in (
            "text/html", "application/xhtml+xml", "text/plain",
        )

        if is_html:
            # Fallback: HTML-Seiten mit markitdown convert_url (strukturierter)
            _correct_ocr = _get("correct_ocr_text", correct_ocr_text)
            start_time = time.time()
            result = await _convert_url(request.url)
            meta = {
                **request.meta,
                "source": request.url,
                "source_type": "url",
                "url": request.url,
                "content_type": url_content_type,
                "duration_ms": int((time.time() - start_time) * 1000),
                "accuracy_mode": request.accuracy,
            }
            if result["success"]:
                if result.get("title"):
                    meta["title"] = result["title"]
                return await finalize_url_markdown_response(
                    result["markdown"],
                    meta=meta,
                    source=request.url,
                    language=request.language,
                    accuracy=request.accuracy,
                    classify=request.classify,
                    classify_categories=request.classify_categories,
                    ocr_correct=request.ocr_correct,
                    extract_schema=effective_schema,
                    template=request.template,
                    auto_extract=request.auto_extract,
                    min_confidence=request.min_confidence,
                    mode=request.mode,
                    retry_on_low_quality=request.retry_on_low_quality,
                    quality_retry_threshold=request.quality_retry_threshold,
                    quality_retry_mode=request.quality_retry_mode,
                    chunk=request.chunk,
                    chunk_size=request.chunk_size,
                    output_format=request.output_format,
                    compact=getattr(request, "compact", False),
                )
            else:
                return create_error_response(
                    result.get("error_code", ErrorCode.CONVERSION_FAILED),
                    result["error"],
                    meta=meta
                )
        else:
            # Nicht-HTML (PDF, DOCX, Bilder, …) → Inhalt laden und durch convert_auto() schicken
            try:
                async with _httpx.AsyncClient(timeout=float(_mistral_timeout)) as client:
                    dl_resp = await client.get(request.url, follow_redirects=True)
                    dl_resp.raise_for_status()
            except Exception as exc:
                return create_error_response(
                    ErrorCode.CONVERSION_FAILED,
                    f"URL-Download fehlgeschlagen: {str(exc)}",
                    meta=request.meta
                )

            raw_content_type = dl_resp.headers.get("content-type", url_content_type)
            ct_base = raw_content_type.split(";")[0].strip()
            guessed_ext = mimetypes.guess_extension(ct_base) or ".bin"
            # mimetypes liefert manchmal .jpe statt .jpg — normalisieren
            _ext_map = {".jpe": ".jpg", ".jpeg": ".jpg"}
            guessed_ext = _ext_map.get(guessed_ext, guessed_ext)

            url_hash = hashlib.md5(request.url.encode()).hexdigest()
            temp_path = _temp_dir / f"url_{url_hash}{guessed_ext}"
            temp_path.write_bytes(dl_resp.content)
            try:
                return await _convert_auto(
                    file_data=dl_resp.content,
                    filename=temp_path.name,
                    source=request.url,
                    source_type="url",
                    input_meta={**request.meta, "url": request.url, "content_type": raw_content_type},
                    prompt=request.prompt,
                    language=request.language,
                    describe_images=request.describe_images,
                    classify=request.classify,
                    classify_categories=request.classify_categories,
                    ocr_correct=request.ocr_correct,
                    show_formulas=request.show_formulas,
                    chunk=request.chunk,
                    chunk_size=request.chunk_size,
                    extract_schema=effective_schema,
                    accuracy=request.accuracy,
                    ocr_embed=request.ocr_embed,
                    auto_extract=request.auto_extract,
                    min_confidence=request.min_confidence,
                    retry_on_low_quality=request.retry_on_low_quality,
                    quality_retry_threshold=request.quality_retry_threshold,
                    quality_retry_mode=request.quality_retry_mode,
                    mode=request.mode,
                    output_format=request.output_format,
                    pages=request.pages,
                    no_cache=request.no_cache,
                    compact=request.compact,
                    template=request.template,
                )
            finally:
                temp_path.unlink(missing_ok=True)

    return create_error_response(
        ErrorCode.INTERNAL_ERROR,
        "Unerwarteter Zustand",
        meta=request.meta
    )


@app.post("/v1/convert/folder", response_model=ConvertResponse)
async def api_convert_folder(request: ConvertFolderRequest) -> ConvertResponse:
    """
    Konvertiert alle Dateien in einem Ordner zu Markdown.
    """
    try:
        folder_path = resolve_path(request.path)
    except PathPolicyError as exc:
        return create_error_response(
            ErrorCode.PATH_NOT_ALLOWED,
            str(exc),
            meta={**request.meta, "path_policy_reason": exc.reason},
        )
    return await convert_folder_contents(
        folder_path=folder_path,
        input_meta=request.meta,
        language=request.language,
        describe_images=request.describe_images,
        classify=request.classify,
        classify_categories=request.classify_categories,
        extract_schema=request.extract_schema,
        auto_extract=request.auto_extract,
        accuracy=request.accuracy,
        chunk=request.chunk,
        chunk_size=request.chunk_size,
        ocr_correct=request.ocr_correct,
        ocr_embed=request.ocr_embed,
        show_formulas=request.show_formulas,
        prompt=request.prompt,
        template=request.template,
        min_confidence=request.min_confidence,
        mode=request.mode,
    )


@app.post("/v1/extract", response_model=ConvertResponse)
async def api_extract(request: ExtractRequest) -> ConvertResponse:
    """
    Konvertiert ein Dokument zu Markdown UND extrahiert strukturierte Daten in einem Schritt.

    Kombiniert convert + schema-basierte Extraktion via LLM (AC-014-5).
    """
    inputs = [request.path, request.base64, request.url]
    if sum(1 for x in inputs if x) != 1:
        return create_error_response(
            ErrorCode.INVALID_INPUT,
            "Genau einer von 'path', 'base64' oder 'url' muss angegeben werden",
            meta=request.meta
        )

    # Template → Schema Auflösung (AC-014-4)
    effective_schema = request.extract_schema
    if request.template and not effective_schema:
        tmpl = get_template_by_id(request.template)
        if tmpl is None:
            return create_error_response(
                ErrorCode.INVALID_INPUT,
                f"Unbekanntes Template: '{request.template}'. Verfügbar: {get_all_template_ids()}",
                meta=request.meta
            )
        effective_schema = tmpl["schema"]

    # Wenn auto_extract=true ist extract_schema/template optional
    if not effective_schema and not request.auto_extract:
        return create_error_response(
            ErrorCode.INVALID_INPUT,
            "Entweder 'extract_schema', 'template' oder 'auto_extract=true' muss angegeben werden",
            meta=request.meta
        )

    # Konvertierungs-Request aus ExtractRequest aufbauen (T-MKIT-022: neue Felder durchreichen)
    convert_req = ConvertRequest(
        path=request.path,
        base64=request.base64,
        filename=request.filename,
        url=request.url,
        output_format=request.output_format,
        prompt=request.prompt,
        language=request.language,
        classify_categories=request.classify_categories,
        meta=request.meta,
        extract_schema=effective_schema,
        accuracy=request.accuracy,
        ocr_correct=request.ocr_correct,
        describe_images=request.describe_images,
        classify=request.classify,
        show_formulas=request.show_formulas,
        chunk=request.chunk,
        chunk_size=request.chunk_size,
        ocr_embed=request.ocr_embed,
        auto_extract=request.auto_extract,
        min_confidence=request.min_confidence,
        retry_on_low_quality=request.retry_on_low_quality,
        quality_retry_threshold=request.quality_retry_threshold,
        quality_retry_mode=request.quality_retry_mode,
        mode=request.mode,
        pages=request.pages,
        webhook_url=request.webhook_url,
        no_cache=request.no_cache,
        compact=request.compact,
    )
    _api_convert_fn = _get("api_convert", api_convert)
    return await _api_convert_fn(convert_req)


@app.get("/v1/templates/categories")
async def api_template_categories() -> dict:
    """Gibt alle Template-Kategorien mit Anzahl zurück (T-MKIT-035)."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT category, COUNT(*) as count FROM template WHERE enabled = 1 GROUP BY category ORDER BY category"
        )
        rows = cur.fetchall()
    finally:
        _db_return_conn(conn)
    return {"categories": [{"name": r["category"], "count": r["count"]} for r in rows]}


@app.get("/v1/templates/search")
async def api_search_templates(q: str = "") -> dict:
    """Sucht Templates nach Stichwort (T-MKIT-035)."""
    if not q:
        return {"templates": []}
    results = search_templates(q)
    return {"templates": results}


@app.get("/v1/templates", response_model=TemplateResponse)
async def api_templates() -> TemplateResponse:
    """
    Gibt alle vordefinierten Extraktions-Templates zurück (AC-014-4).

    Templates können direkt als 'template' Parameter in /v1/convert oder /v1/extract
    übergeben werden.
    """
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, category, display_name, enabled FROM template ORDER BY category, display_name"
        )
        rows = cur.fetchall()
    finally:
        _db_return_conn(conn)
    # Build legacy-compatible dict with full schemas for TemplateResponse
    templates: dict = {}
    for row in rows:
        tmpl = get_template_by_id(row["id"])
        if tmpl:
            templates[row["id"]] = tmpl["schema"]
    return TemplateResponse(templates=templates)


@app.get("/v1/templates/{template_id}")
async def api_get_template(template_id: str) -> dict:
    """Gibt ein einzelnes Template mit allen Feldern zurück (T-MKIT-035)."""
    tmpl = get_template_by_id(template_id)
    if not tmpl:
        raise HTTPException(status_code=404, detail=f"Template '{template_id}' not found")
    return tmpl


@app.post("/v1/templates/bulk")
async def api_bulk_templates(request: dict) -> dict:
    """Bulk-Upsert für Templates (T-MKIT-035)."""
    templates = request.get("templates", [])
    mode = request.get("mode", "upsert")
    conn = get_db_connection()
    created = 0
    updated = 0
    try:
        cur = conn.cursor()
        for tmpl in templates:
            if "id" not in tmpl or "schema" not in tmpl:
                continue
            cur.execute("SELECT id FROM template WHERE id = %s", (tmpl["id"],))
            existing = cur.fetchone()
            if existing and mode == "upsert":
                cur.execute(
                    """UPDATE template SET category=%s, display_name=%s, description=%s, schema=%s,
                       field_descriptions=%s, classify_keywords=%s, typical_senders=%s, steuer_relevanz=%s,
                       priority=%s, enabled=%s, version=%s, source=%s, notes=%s, updated_at=now()
                       WHERE id=%s""",
                    (tmpl.get("category", "other"), tmpl.get("display_name", tmpl["id"]),
                     tmpl.get("description"), json.dumps(tmpl["schema"]),
                     json.dumps(tmpl.get("field_descriptions")) if tmpl.get("field_descriptions") else None,
                     tmpl.get("classify_keywords"), tmpl.get("typical_senders"),
                     tmpl.get("steuer_relevanz"), tmpl.get("priority", 0),
                     int(tmpl.get("enabled", 1)), tmpl.get("version", 1),
                     tmpl.get("source", "manual"), tmpl.get("notes"), tmpl["id"])
                )
                updated += 1
            elif not existing:
                cur.execute(
                    """INSERT INTO template (id, category, display_name, description, schema, field_descriptions,
                       classify_keywords, typical_senders, steuer_relevanz, priority, enabled, version, source, notes)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (tmpl["id"], tmpl.get("category", "other"), tmpl.get("display_name", tmpl["id"]),
                     tmpl.get("description"), json.dumps(tmpl["schema"]),
                     json.dumps(tmpl.get("field_descriptions")) if tmpl.get("field_descriptions") else None,
                     tmpl.get("classify_keywords"), tmpl.get("typical_senders"),
                     tmpl.get("steuer_relevanz"), tmpl.get("priority", 0),
                     int(tmpl.get("enabled", 1)), tmpl.get("version", 1),
                     tmpl.get("source", "manual"), tmpl.get("notes"))
                )
                created += 1
        conn.commit()
    finally:
        _db_return_conn(conn)
    return {"success": True, "created": created, "updated": updated, "total": len(templates)}


@app.post("/v1/templates")
async def api_create_template(request: dict) -> dict:
    """Erstellt ein neues Template (T-MKIT-035)."""
    if "id" not in request or "schema" not in request:
        raise HTTPException(status_code=400, detail="'id' and 'schema' are required")
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO template (id, category, display_name, description, schema, field_descriptions,
               classify_keywords, typical_senders, steuer_relevanz, priority, enabled, version, source, notes)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (request["id"], request.get("category", "other"), request.get("display_name", request["id"]),
             request.get("description"), json.dumps(request["schema"]),
             json.dumps(request.get("field_descriptions")) if request.get("field_descriptions") else None,
             request.get("classify_keywords"), request.get("typical_senders"),
             request.get("steuer_relevanz"), request.get("priority", 0),
             int(request.get("enabled", 1)), request.get("version", 1),
             request.get("source", "manual"), request.get("notes"))
        )
        conn.commit()
    except Exception as e:
        conn.rollback()
        _db_return_conn(conn)
        import psycopg2.errors
        if isinstance(e, psycopg2.errors.UniqueViolation) or "UniqueViolation" in type(e).__name__ or "duplicate key" in str(e).lower():
            raise HTTPException(status_code=409, detail=f"Template '{request['id']}' already exists")
        raise
    _db_return_conn(conn)
    return {"success": True, "id": request["id"]}


@app.put("/v1/templates/{template_id}")
async def api_update_template(template_id: str, request: dict) -> dict:
    """Aktualisiert ein Template (partial update, T-MKIT-035)."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM template WHERE id = %s", (template_id,))
        existing = cur.fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail=f"Template '{template_id}' not found")
        cur.execute("SELECT * FROM template WHERE id = %s", (template_id,))
        current = dict(cur.fetchone())
        cur.execute(
            """UPDATE template SET
                category = %s, display_name = %s, description = %s,
                schema = %s, field_descriptions = %s,
                classify_keywords = %s, typical_senders = %s,
                steuer_relevanz = %s, priority = %s, enabled = %s,
                version = %s, source = %s, notes = %s,
                updated_at = now()
            WHERE id = %s""",
            (
                request.get("category", current["category"]),
                request.get("display_name", current["display_name"]),
                request.get("description", current["description"]),
                json.dumps(request["schema"]) if "schema" in request else current["schema"],
                json.dumps(request["field_descriptions"]) if "field_descriptions" in request else current["field_descriptions"],
                request.get("classify_keywords", current["classify_keywords"]),
                request.get("typical_senders", current["typical_senders"]),
                request.get("steuer_relevanz", current["steuer_relevanz"]),
                request.get("priority", current["priority"]),
                int(request.get("enabled", current["enabled"])),
                request.get("version", current["version"]),
                request.get("source", current["source"]),
                request.get("notes", current["notes"]),
                template_id,
            )
        )
        conn.commit()
    finally:
        _db_return_conn(conn)
    return {"success": True, "id": template_id}


@app.delete("/v1/templates/{template_id}")
async def api_delete_template(template_id: str) -> dict:
    """Löscht ein Template (T-MKIT-035)."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM template WHERE id = %s", (template_id,))
        deleted = cur.rowcount > 0
        conn.commit()
    finally:
        _db_return_conn(conn)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Template '{template_id}' not found")
    return {"success": True, "id": template_id}


@app.post("/v1/analyze", response_model=ConvertResponse)
async def api_analyze(request: AnalyzeRequest) -> ConvertResponse:
    """Analysiert ein Bild explizit mit Mistral Vision."""
    from converters.images import resize_image_if_needed
    from mistral_client import analyze_with_mistral_vision

    if not request.path and not request.base64:
        return create_error_response(
            ErrorCode.INVALID_INPUT,
            "'path' oder 'base64' muss angegeben werden",
            meta=request.meta
        )

    if request.path:
        try:
            file_path = resolve_path(request.path)
        except PathPolicyError as exc:
            return create_error_response(
                ErrorCode.PATH_NOT_ALLOWED,
                str(exc),
                meta={**request.meta, "path_policy_reason": exc.reason},
            )
        if not file_path.exists():
            return create_error_response(
                ErrorCode.FILE_NOT_FOUND,
                f"Datei nicht gefunden: {file_path}",
                meta=request.meta
            )
        file_data = file_path.read_bytes()
        filename = file_path.name
        source = str(file_path)
        source_type = "file"
    else:
        if not request.filename:
            return create_error_response(
                ErrorCode.INVALID_INPUT,
                "'filename' ist erforderlich bei Base64",
                meta=request.meta
            )
        try:
            file_data = base64.b64decode(request.base64)
        except Exception as e:
            return create_error_response(
                ErrorCode.INVALID_BASE64,
                f"Ungültiges Base64: {str(e)}",
                meta=request.meta
            )
        filename = request.filename
        source = "base64"
        source_type = "base64"

    start_time = time.time()
    processed_data, resize_meta = resize_image_if_needed(file_data)
    mimetype = detect_mimetype_from_bytes(file_data) or get_mimetype(Path(filename))

    result = await analyze_with_mistral_vision(
        processed_data,
        mimetype,
        request.prompt,
        request.language
    )

    meta = {
        **request.meta,
        **resize_meta,
        "source": source,
        "source_type": source_type,
        "format": get_file_extension(filename).lstrip("."),
        "size_bytes": len(file_data),
        "vision_used": True,
        "duration_ms": int((time.time() - start_time) * 1000),
    }

    if result["success"]:
        meta["vision_model"] = result.get("vision_model")
        meta["tokens_prompt"] = result.get("tokens_prompt")
        meta["tokens_completion"] = result.get("tokens_completion")
        meta["tokens_total"] = result.get("tokens_total")
        return create_success_response(result["markdown"], meta=meta)
    else:
        return create_error_response(
            result.get("error_code", ErrorCode.VISION_FAILED),
            result["error"],
            meta=meta
        )


@app.get("/v1/health", response_model=HealthResponse)
async def api_health() -> HealthResponse:
    """Health-Check Endpoint."""
    uptime = int(time.time() - START_TIME)
    mistral_configured = bool(MISTRAL_API_KEY)
    _check_persistence = _get("check_persistence_health", check_persistence_health)
    persistence = _check_persistence()
    status = "ok" if persistence.get("ready") else "error"

    return HealthResponse(
        status=status,
        version=VERSION,
        meta={
            "persistence_ready": persistence.get("ready", False),
            "database_url_configured": persistence.get("database_url_configured", False),
            "database_connection_ok": persistence.get("connection_ok", False),
            "missing_tables": persistence.get("missing_tables", []),
            "persistence_error": persistence.get("error"),
            "mistral_api_configured": mistral_configured,
            "vision_model": MISTRAL_VISION_MODEL,
            "mistral_ocr_configured": mistral_configured and MISTRAL_OCR_ENABLED,
            "ocr_model": MISTRAL_OCR_MODEL if MISTRAL_OCR_ENABLED else None,
            "max_file_size_mb": MAX_FILE_SIZE_MB,
            "image_max_width": IMAGE_MAX_WIDTH,
            "max_retries": MAX_RETRIES,
            "execution_result_retention_days": EXECUTION_RESULT_RETENTION_DAYS,
            "execution_result_artifact_retention_days": EXECUTION_RESULT_ARTIFACT_RETENTION_DAYS,
            "debug_snapshot_retention_days": DEBUG_SNAPSHOTS_RETENTION_DAYS,
            "uptime_seconds": uptime,
            "mcp_port": MCP_PORT,
            "rest_port": REST_PORT,
        }
    )


@app.get("/v1/formats")
async def api_formats() -> dict:
    """Listet unterstützte Formate auf."""
    return {
        "markitdown": sorted(MARKITDOWN_EXTENSIONS),
        "vision": sorted(IMAGE_EXTENSIONS),
        "audio": sorted(AUDIO_EXTENSIONS),
        "video": sorted(VIDEO_EXTENSIONS),
        "all": sorted(MARKITDOWN_EXTENSIONS | IMAGE_EXTENSIONS | AUDIO_EXTENSIONS | VIDEO_EXTENSIONS),
    }


@app.delete("/v1/cache")
async def api_cache_clear() -> dict:
    """Löscht alle Einträge aus dem Request-Level-Cache (T-DAI-019)."""
    _cache_clear = _get("cache_clear", cache_clear)
    _cache_clear()
    return {"cleared": True}


# =============================================================================
# Async Job API — T-DAI-023
# =============================================================================

async def _fire_webhook(webhook_url: str, result: ConvertResponse) -> None:
    """Sendet das Konvertierungsergebnis an eine Webhook-URL (POST).

    Fehler werden geloggt aber nicht weitergeworfen — sie beeinflussen die Response nicht.

    Args:
        webhook_url: Ziel-URL für den POST-Request.
        result: Konvertierungsergebnis als ConvertResponse.
    """
    _httpx = _get("httpx", httpx)
    _timeout = _get("WEBHOOK_TIMEOUT_SECONDS", WEBHOOK_TIMEOUT_SECONDS)
    try:
        async with _httpx.AsyncClient(timeout=float(_timeout)) as client:
            await client.post(webhook_url, json=result.model_dump())
        log.info("webhook_sent", url=webhook_url)
    except Exception as exc:
        log.warning("webhook_failed", url=webhook_url, error=str(exc))


async def _run_async_job(job_id: str, request: "ConvertRequest") -> None:
    """Background-Worker: führt convert_auto aus und schreibt Ergebnis in die DB.

    Args:
        job_id: Job-ID in der Datenbank.
        request: Original ConvertRequest.
    """
    _convert_auto = _get("convert_auto", convert_auto)
    _job_update = _get("job_update", job_update)
    _job_set_result = _get("job_set_result", job_set_result)
    _job_set_terminal_result = _get("job_set_terminal_result", job_set_terminal_result)
    _execution_update = _get("execution_update", execution_update)
    _execution_result_upsert = _get("execution_result_upsert", execution_result_upsert)
    temp_path: Path | None = None
    execution_id = request.meta.get("execution_id")

    if execution_id:
        existing_execution = _get("execution_get", execution_get)(execution_id)
        if existing_execution and existing_execution.get("status") == "cancelled":
            return

    _job_update(
        job_id,
        "processing",
        ProgressState(
            **build_progress_payload(
                status="processing",
                current_stage="start",
                message="Starting conversion",
                percent=0,
                job_id=job_id,
            )
        ).model_dump_json(),
    )
    if execution_id:
        start_progress = build_progress_payload(
            status="processing",
            current_stage="start",
            message="Starting conversion",
            percent=0,
            request_id=request.meta.get("_request_id"),
            job_id=job_id,
        )
        _execution_update(
            execution_id,
            status="processing",
            current_stage="start",
            progress_json=start_progress,
            started_at_now=True,
        )
    try:
        # Eingabe auflösen
        _resolve_path = _get("resolve_path", resolve_path)
        _httpx_mod = _get("httpx", httpx)
        _temp_dir = _get("TEMP_DIR", TEMP_DIR)
        _mistral_timeout = _get("MISTRAL_TIMEOUT", MISTRAL_TIMEOUT)

        inputs = [request.path, request.base64, request.url]
        if sum(1 for x in inputs if x) != 1:
            raise ValueError("Genau einer von 'path', 'base64' oder 'url' muss angegeben werden")

        # Template → Schema Auflösung
        effective_schema = request.extract_schema
        if request.template and not effective_schema:
            _get_template = _get("get_template_by_id", get_template_by_id)
            tmpl = _get_template(request.template)
            if tmpl:
                effective_schema = tmpl["schema"]

        if request.path:
            try:
                file_path = _resolve_path(request.path)
            except PathPolicyError as exc:
                raise exc
            file_data = file_path.read_bytes()
            filename = file_path.name
            source = str(file_path)
            source_type = "file"
        elif request.base64:
            file_data = base64.b64decode(request.base64)
            filename = request.filename or "upload"
            source = "base64"
            source_type = "base64"
        else:
            async with _httpx_mod.AsyncClient(timeout=float(_mistral_timeout)) as client:
                dl_resp = await client.get(request.url, follow_redirects=True)
                dl_resp.raise_for_status()
            file_data = dl_resp.content
            ct_base = dl_resp.headers.get("content-type", "application/octet-stream").split(";")[0].strip()
            guessed_ext = mimetypes.guess_extension(ct_base) or ".bin"
            _ext_map = {".jpe": ".jpg", ".jpeg": ".jpg"}
            guessed_ext = _ext_map.get(guessed_ext, guessed_ext)
            url_hash = hashlib.md5(request.url.encode()).hexdigest()
            temp_path = _temp_dir / f"url_{url_hash}{guessed_ext}"
            temp_path.write_bytes(dl_resp.content)
            filename = temp_path.name
            source = request.url
            source_type = "url"

        _job_timeout = _get("JOB_TIMEOUT_SECONDS", JOB_TIMEOUT_SECONDS)
        try:
            result: ConvertResponse = await asyncio.wait_for(
                _convert_auto(
                    file_data=file_data,
                    filename=filename,
                    source=source,
                    source_type=source_type,
                    input_meta={**request.meta, "_job_id": job_id},
                    prompt=request.prompt,
                    language=request.language,
                    describe_images=request.describe_images,
                    classify=request.classify,
                    classify_categories=request.classify_categories,
                    extract_schema=effective_schema,
                    ocr_correct=request.ocr_correct,
                    show_formulas=request.show_formulas,
                    chunk=request.chunk,
                    chunk_size=request.chunk_size,
                    accuracy=request.accuracy,
                    ocr_embed=request.ocr_embed,
                    auto_extract=request.auto_extract,
                    min_confidence=request.min_confidence,
                    retry_on_low_quality=request.retry_on_low_quality,
                    quality_retry_threshold=request.quality_retry_threshold,
                    quality_retry_mode=request.quality_retry_mode,
                    mode=request.mode,
                    output_format=request.output_format,
                    pages=request.pages,
                    no_cache=request.no_cache,
                    compact=getattr(request, "compact", False),
                    template=getattr(request, "template", None),
                ),
                timeout=float(_job_timeout),
            )
        except asyncio.TimeoutError:
            log.error("async_job_timeout", job_id=job_id, timeout=_job_timeout)
            timeout_result = create_error_response(
                ErrorCode.TIMEOUT,
                f"Job timed out after {_job_timeout}s",
                meta={
                    "request_id": request.meta.get("_request_id"),
                    "execution_id": execution_id,
                    "job_id": job_id,
                },
            )
            if execution_id:
                _execution_result_upsert(
                    result_id=_execution_result_id(execution_id, kind="job-timeout"),
                    execution_id=execution_id,
                    is_final=True,
                    result_status="failed",
                    success=False,
                    response_json=timeout_result.model_dump(),
                    meta=timeout_result.meta.model_dump(),
                    extracted=timeout_result.extracted,
                    normalized=timeout_result.normalized,
                    warnings=timeout_result.normalized_warnings,
                    error=timeout_result.error.model_dump() if timeout_result.error else None,
                )
                _execution_update(
                    execution_id,
                    status="failed",
                    current_stage="failed",
                    progress_json=build_progress_payload(
                        status="failed",
                        current_stage="failed",
                        message=f"Job timed out after {_job_timeout}s",
                        percent=100,
                        request_id=request.meta.get("_request_id"),
                        job_id=job_id,
                    ),
                    error_summary=timeout_result.error.model_dump() if timeout_result.error else None,
                    finished_at_now=True,
                )
            _job_update(
                job_id,
                "failed",
                ProgressState(
                    **build_progress_payload(
                        status="failed",
                        current_stage="failed",
                        message=f"Job timed out after {_job_timeout}s",
                        percent=100,
                        job_id=job_id,
                    )
                ).model_dump_json(),
            )
            return
        if result.success:
            _job_set_result(job_id, result.model_dump_json())
        else:
            _job_set_terminal_result(job_id, result.model_dump_json(), status="failed")

        # Webhook senden wenn konfiguriert
        if request.webhook_url:
            await _fire_webhook(request.webhook_url, result)

    except PathPolicyError as exc:
        log.error("async_job_path_policy_error", job_id=job_id, error=str(exc), reason=exc.reason)
        error_result = create_error_response(
            ErrorCode.PATH_NOT_ALLOWED,
            str(exc),
            meta={
                "request_id": request.meta.get("_request_id"),
                "execution_id": execution_id,
                "job_id": job_id,
                "path_policy_reason": exc.reason,
            },
        )
        _job_set_terminal_result(job_id, error_result.model_dump_json(), status="failed")
        if execution_id:
            _execution_result_upsert(
                result_id=_execution_result_id(execution_id, kind="job-path-policy"),
                execution_id=execution_id,
                is_final=True,
                result_status="failed",
                success=False,
                response_json=error_result.model_dump(),
                meta=error_result.meta.model_dump(),
                extracted=None,
                normalized=None,
                warnings=None,
                error=error_result.error.model_dump() if error_result.error else None,
            )
            _execution_update(
                execution_id,
                status="failed",
                current_stage="failed",
                progress_json=build_progress_payload(
                    status="failed",
                    current_stage="failed",
                    message=str(exc),
                    percent=100,
                    request_id=request.meta.get("_request_id"),
                    job_id=job_id,
                ),
                error_summary=error_result.error.model_dump() if error_result.error else None,
                finished_at_now=True,
            )
        return
    except Exception as exc:
        log.error("async_job_failed", job_id=job_id, error=str(exc))
        error_result = create_error_response(
            ErrorCode.INTERNAL_ERROR,
            f"Job fehlgeschlagen: {str(exc)}",
        )
        _job_update(
            job_id,
            "failed",
            ProgressState(
                **build_progress_payload(
                    status="failed",
                    current_stage="failed",
                    message=str(exc),
                    percent=100,
                    job_id=job_id,
                )
            ).model_dump_json(),
        )
        if execution_id:
            _execution_result_upsert(
                result_id=_execution_result_id(execution_id, kind="job-exception"),
                execution_id=execution_id,
                is_final=True,
                result_status="failed",
                success=False,
                response_json=error_result.model_dump(),
                meta={
                    "request_id": request.meta.get("_request_id"),
                    "execution_id": execution_id,
                    "job_id": job_id,
                },
                extracted=None,
                normalized=None,
                warnings=None,
                error=error_result.error.model_dump() if error_result.error else None,
            )
            _execution_update(
                execution_id,
                status="failed",
                current_stage="failed",
                progress_json=build_progress_payload(
                    status="failed",
                    current_stage="failed",
                    message=str(exc),
                    percent=100,
                    request_id=request.meta.get("_request_id"),
                    job_id=job_id,
                ),
                error_summary=error_result.error.model_dump() if error_result.error else None,
                finished_at_now=True,
            )
        # Webhook auch bei Fehler senden
        if request.webhook_url:
            await _fire_webhook(request.webhook_url, error_result)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


async def _start_async_execution(request: ConvertRequest) -> AsyncJobStartResponse:
    """Start one async execution, either via persisted queue or direct task scheduling."""
    _job_create = _get("job_create", job_create)
    job_id = str(uuid.uuid4())
    _, execution_id = _ensure_execution_for_request(request, execution_kind="async", job_id=job_id)
    _merge_execution_policy_context(
        execution_id,
        dispatch_policy=_resolve_dispatch_policy(request=request, execution_kind="async"),
    )
    _job_create(job_id)
    if _get("QUEUE_ENABLED", QUEUE_ENABLED):
        _get("execution_queue_enqueue", execution_queue_enqueue)(
            queue_id=str(uuid.uuid4()),
            execution_id=execution_id,
            job_id=job_id,
            payload={"request": request.model_dump(mode="json")},
        )
        _get("execution_update", execution_update)(
            execution_id,
            status="queued",
            current_stage="queued",
            progress_json=build_progress_payload(
                status="queued",
                current_stage="queued",
                message="Queued for worker execution",
                percent=0,
                request_id=request.meta.get("_request_id"),
                job_id=job_id,
            ),
        )
    else:
        asyncio.create_task(_run_async_job(job_id, request))
    return AsyncJobStartResponse(job_id=job_id, execution_id=execution_id, status="queued")


def _start_batch_execution(body: BatchCreateRequest) -> BatchStartResponse:
    """Persist one canonical batch plus batch_items and enqueue linked batch_item executions."""
    if not _get("QUEUE_ENABLED", QUEUE_ENABLED):
        raise HTTPException(status_code=409, detail="Batch execution requires QUEUE_ENABLED=true")

    queue_name = body.queue_name or _get("BATCH_DEFAULT_QUEUE_NAME", BATCH_DEFAULT_QUEUE_NAME)
    batch_idempotency_key = _build_batch_idempotency_key(body, queue_name)
    batch_dispatch_policy = {
        "queue_name": queue_name,
        "mistral_batch_enabled": bool(_get("MISTRAL_BATCH_ENABLED", MISTRAL_BATCH_ENABLED)),
        "mistral_batch_min_items": int(_get("MISTRAL_BATCH_MIN_ITEMS", MISTRAL_BATCH_MIN_ITEMS)),
        "mistral_batch_poll_interval_seconds": float(_get("MISTRAL_BATCH_POLL_INTERVAL_SECONDS", MISTRAL_BATCH_POLL_INTERVAL_SECONDS)),
        "mistral_batch_max_active": int(_get("MISTRAL_BATCH_MAX_ACTIVE", MISTRAL_BATCH_MAX_ACTIVE)),
        "mistral_batch_timeout_hours": int(_get("MISTRAL_BATCH_TIMEOUT_HOURS", MISTRAL_BATCH_TIMEOUT_HOURS)),
        "mistral_batch_allowed_source_types": list(_get("MISTRAL_BATCH_ALLOWED_SOURCE_TYPES", MISTRAL_BATCH_ALLOWED_SOURCE_TYPES)),
        "batch_item_count": len(body.items),
    }
    if batch_idempotency_key:
        existing = _get("execution_batch_get_by_idempotency_key", execution_batch_get_by_idempotency_key)(batch_idempotency_key)
        if existing:
            return BatchStartResponse(
                batch_id=existing["id"],
                status=existing["status"],
                item_count=existing["item_count"],
                batch_ref=existing.get("batch_ref"),
                queue_name=existing["queue_name"],
            )

    batch_id = str(uuid.uuid4())
    _get("execution_batch_create", execution_batch_create)(
        batch_id=batch_id,
        batch_ref=body.batch_ref,
        idempotency_key=batch_idempotency_key,
        queue_name=queue_name,
        status="queued",
        item_count=len(body.items),
        metadata={**(body.meta or {}), "dispatch_policy": batch_dispatch_policy},
    )

    provider_candidates: list[dict[str, Any]] = []
    queued_candidates: list[dict[str, Any]] = []

    for item_index, item_request in enumerate(body.items):
        item_meta = dict(item_request.meta or {})
        item_key = _build_batch_item_key(item_request, item_index)
        item_meta.setdefault("batch_id", batch_id)
        if body.batch_ref is not None:
            item_meta.setdefault("batch_ref", body.batch_ref)
        item_meta.setdefault("item_key", item_key)
        item_meta.setdefault("idempotency_key", f"{batch_id}:{item_index}:{item_key}")
        item_request.meta = item_meta

        request_id, execution_id = _ensure_execution_for_request(item_request, execution_kind="batch_item")
        document_identity, input_snapshot = _build_batch_item_artifacts(item_request)
        batch_item_id = str(uuid.uuid4())

        _get("execution_update", execution_update)(
            execution_id,
            status="queued",
            current_stage="queued",
            batch_id=batch_id,
            batch_item_id=batch_item_id,
            document_identity=document_identity,
            input_snapshot=input_snapshot,
        )
        source_type, source_ref = _infer_execution_source(item_request)
        filename = item_request.filename or (Path(item_request.path).name if item_request.path else None) or source_ref
        _get("execution_batch_item_create", execution_batch_item_create)(
            batch_item_id=batch_item_id,
            batch_id=batch_id,
            item_index=item_index,
            execution_id=execution_id,
            request_id=request_id,
            source_type=source_type,
            source_ref=source_ref,
            filename=filename,
            status="queued",
            item_key=item_key,
            metadata=item_meta,
            document_identity=document_identity,
            input_snapshot=input_snapshot,
        )
        _get("execution_update", execution_update)(
            execution_id,
            status="queued",
            current_stage="queued",
        )
        dispatch_policy = _resolve_dispatch_policy(
            request=item_request,
            execution_kind="batch_item",
            batch_item_count=len(body.items),
        )
        _merge_execution_policy_context(execution_id, dispatch_policy=dispatch_policy)
        if dispatch_policy["preferred_dispatch_target"] == "mistral_batch":
            _get("execution_subjob_upsert", execution_subjob_upsert)(
                subjob_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{execution_id}:mistral-batch")),
                execution_id=execution_id,
                batch_id=batch_id,
                batch_item_id=batch_item_id,
                provider="mistral",
                subjob_type="mistral_batch",
                subjob_status="queued",
                metadata={
                    "decision_only": True,
                    "dispatch_policy": dispatch_policy,
                },
            )
            if _batch_request_is_provider_compatible(item_request):
                provider_candidates.append(
                    {
                        "batch_item_id": batch_item_id,
                        "execution_id": execution_id,
                        "request": item_request,
                        "dispatch_policy": dispatch_policy,
                    }
                )
            else:
                _apply_dispatch_fallback(execution_id, "provider_batch_request_not_compatible")
                queued_candidates.append(
                    {
                        "batch_item_id": batch_item_id,
                        "execution_id": execution_id,
                        "request": item_request,
                    }
                )
        else:
            queued_candidates.append(
                {
                    "batch_item_id": batch_item_id,
                    "execution_id": execution_id,
                    "request": item_request,
                }
            )

    if provider_candidates:
        submit_coro = _submit_mistral_provider_batch(batch_id=batch_id, items=provider_candidates)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                _run_coro_blocking(submit_coro)
            except Exception as exc:
                log.warning("mistral_batch_submit_failed", batch_id=batch_id, error=str(exc))
                for item in provider_candidates:
                    _apply_dispatch_fallback(item["execution_id"], "provider_batch_submission_failed")
                    queued_candidates.append(item)
        else:
            loop.create_task(submit_coro)

    for item in queued_candidates:
        _get("execution_queue_enqueue", execution_queue_enqueue)(
            queue_id=str(uuid.uuid4()),
            execution_id=item["execution_id"],
            job_id=None,
            payload={
                "request": item["request"].model_dump(mode="json"),
                "batch_id": batch_id,
                "batch_item_id": item["batch_item_id"],
            },
            queue_name=queue_name,
        )

    return BatchStartResponse(
        batch_id=batch_id,
        status="queued",
        item_count=len(body.items),
        batch_ref=body.batch_ref,
        queue_name=queue_name,
    )


@app.post("/v1/convert/async", response_model=AsyncJobStartResponse)
async def api_convert_async(request: ConvertRequest) -> AsyncJobStartResponse:
    """
    Startet eine asynchrone Konvertierung.

    Gibt sofort eine Job-ID zurück. Der Fortschritt kann über GET /v1/jobs/{id}
    abgefragt werden. Das Ergebnis ist über GET /v1/jobs/{id}/result abrufbar.
    """
    return await _start_async_execution(request)


@app.post("/v1/batches", response_model=BatchStartResponse)
async def api_create_batch(body: BatchCreateRequest) -> BatchStartResponse:
    """Persistiert einen Batch mit expliziten Items und queue't die verlinkten batch_item executions."""
    return _start_batch_execution(body)


@app.get("/v1/batches", response_model=BatchListResponse)
async def api_list_batches(
    limit: int = 50,
    active_item_limit: int = BATCH_STATUS_ACTIVE_ITEM_LIMIT,
) -> BatchListResponse:
    """Leichtgewichtige Liste persistierter Batches mit aggregierten Statuszählern."""
    rows = _get("execution_batch_list", execution_batch_list)(limit=limit)
    summary_fn = _get("execution_batch_status_summary", execution_batch_status_summary)
    batches = []
    for row in rows:
        summary = summary_fn(row["id"], active_item_limit=active_item_limit) or row
        batches.append(_build_batch_status_response(summary))
    return BatchListResponse(batches=batches)


@app.get("/v1/batches/{batch_id}", response_model=BatchStatusResponse)
async def api_get_batch(
    batch_id: str,
    active_item_limit: int = BATCH_STATUS_ACTIVE_ITEM_LIMIT,
) -> BatchStatusResponse:
    """Leichtgewichtiger pollbarer Status eines persistierten Batch-Auftrags."""
    return _get_batch_status_payload(batch_id, active_item_limit=active_item_limit)


@app.post("/v1/batches/{batch_id}/cancel", response_model=BatchStatusResponse)
async def api_cancel_batch(batch_id: str) -> BatchStatusResponse:
    """Bricht alle noch nicht terminalen Items eines Batchs ab."""
    return _cancel_batch(batch_id)


@app.post("/v1/batches/{batch_id}/resume", response_model=BatchStatusResponse)
async def api_resume_batch(batch_id: str) -> BatchStatusResponse:
    """Nimmt alle gecancelten Items eines Batchs wieder in die Queue auf."""
    return _resume_batch(batch_id)


@app.get("/v1/batches/{batch_id}/items", response_model=BatchItemListResponse)
async def api_list_batch_items(
    batch_id: str,
    limit: int = 50,
    offset: int = 0,
) -> BatchItemListResponse:
    """Paginierte Liste persistierter Batch-Items ohne Inlining voller Dokumentresultate."""
    return _get_batch_items_payload(batch_id, limit=limit, offset=offset)


@app.get("/v1/batches/{batch_id}/items/{batch_item_id}/result", response_model=ConvertResponse)
async def api_get_batch_item_result(
    batch_id: str,
    batch_item_id: str,
) -> ConvertResponse:
    """Persistiertes finales ConvertResult eines einzelnen Batch-Items."""
    return _get_batch_item_result_payload(batch_id, batch_item_id)


@app.post("/v1/batches/{batch_id}/items/{batch_item_id}/cancel", response_model=BatchItemResponse)
async def api_cancel_batch_item(
    batch_id: str,
    batch_item_id: str,
) -> BatchItemResponse:
    """Bricht ein einzelnes Batch-Item ab."""
    return _cancel_batch_item(batch_id, batch_item_id)


@app.post("/v1/batches/{batch_id}/items/{batch_item_id}/resume", response_model=BatchItemResponse)
async def api_resume_batch_item(
    batch_id: str,
    batch_item_id: str,
) -> BatchItemResponse:
    """Nimmt ein gecanceltes Batch-Item wieder in die Queue auf."""
    return _resume_batch_item(batch_id, batch_item_id)


@app.post("/v1/batches/{batch_id}/items/{batch_item_id}/retry", response_model=BatchItemResponse)
async def api_retry_batch_item(
    batch_id: str,
    batch_item_id: str,
) -> BatchItemResponse:
    """Queue't ein fehlgeschlagenes Batch-Item als Wiederholungsversuch erneut ein."""
    return _retry_batch_item(batch_id, batch_item_id)


@app.get("/v1/jobs", response_model=JobListResponse)
async def api_list_jobs() -> JobListResponse:
    """Gibt alle Jobs zurück (neueste zuerst)."""
    _job_list = _get("job_list", job_list)
    jobs = _job_list()
    result = []
    for j in jobs:
        execution = _get("execution_get_by_job_id", execution_get_by_job_id)(j["id"])
        progress = None
        if execution and execution.get("progress_json"):
            try:
                normalized = normalize_progress_payload(execution["progress_json"])
                progress = ProgressState(**normalized) if normalized else None
            except Exception:
                progress = None
        elif j.get("progress_json"):
            try:
                normalized = normalize_progress_payload(json.loads(j["progress_json"]))
                progress = ProgressState(**normalized) if normalized else None
            except Exception:
                progress = None
        result.append(
            JobStatusResponse(
                job_id=j["id"],
                execution_id=execution["id"] if execution else None,
                status=execution["status"] if execution else j["status"],
                created_at=j["created_at"],
                updated_at=j["updated_at"],
                progress=progress,
            )
        )
    return JobListResponse(jobs=result)


@app.get("/v1/jobs/{job_id}", response_model=JobStatusResponse)
async def api_get_job(job_id: str) -> JobStatusResponse:
    """Gibt den Status und Fortschritt eines Jobs zurück."""
    _job_get = _get("job_get", job_get)
    job = _job_get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    execution = _get("execution_get_by_job_id", execution_get_by_job_id)(job_id)
    progress = None
    if execution and execution.get("progress_json"):
        try:
            normalized = normalize_progress_payload(execution["progress_json"])
            progress = ProgressState(**normalized) if normalized else None
        except Exception:
            progress = None
    elif job.get("progress_json"):
        try:
            normalized = normalize_progress_payload(json.loads(job["progress_json"]))
            progress = ProgressState(**normalized) if normalized else None
        except Exception:
            progress = None
    return JobStatusResponse(
        job_id=job["id"],
        execution_id=execution["id"] if execution else None,
        status=execution["status"] if execution else job["status"],
        created_at=job["created_at"],
        updated_at=job["updated_at"],
        progress=progress,
    )


@app.get("/v1/jobs/{job_id}/result", response_model=ConvertResponse)
async def api_get_job_result(job_id: str) -> ConvertResponse:
    """Gibt das volle ConvertResponse Ergebnis zurück (nur wenn status=completed)."""
    return _get_job_result_payload(job_id)


@app.delete("/v1/jobs/{job_id}")
async def api_delete_job(job_id: str) -> dict:
    """Löscht einen Job."""
    _job_delete = _get("job_delete", job_delete)
    deleted = _job_delete(job_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return {"success": True, "job_id": job_id}


@app.get("/v1/executions", response_model=ExecutionListResponse)
async def api_list_executions(limit: int = 50) -> ExecutionListResponse:
    """Gibt die kanonische History aller Ausführungsläufe zurück."""
    rows = _get("execution_list", execution_list)(limit=limit)
    executions = []
    for row in rows:
        full_row = _get("execution_get_full", execution_get_full)(row["id"]) or row
        executions.append(_build_execution_status_response(full_row))
    return ExecutionListResponse(executions=executions)


@app.get("/v1/executions/{execution_id}", response_model=ExecutionStatusResponse)
async def api_get_execution(execution_id: str) -> ExecutionStatusResponse:
    """Gibt die kanonische Detailansicht eines Ausführungslaufs zurück."""
    return _get_execution_status_payload(execution_id)


@app.get("/v1/executions/{execution_id}/result", response_model=ConvertResponse)
async def api_get_execution_result(execution_id: str) -> ConvertResponse:
    """Gibt das persistierte finale Ergebnis eines Ausführungslaufs zurück."""
    return _get_execution_result_payload(execution_id)


@app.post("/v1/executions/{execution_id}/replay", response_model=ReplayStartResponse)
async def api_replay_execution(execution_id: str, snapshot_id: Optional[int] = None) -> ReplayStartResponse:
    """Erzeugt einen kanonischen Replay-Lauf aus einem vorhandenen Snapshot der Execution."""
    return _replay_execution(execution_id, snapshot_id=snapshot_id)


@app.post("/v1/batches/{batch_id}/items/{batch_item_id}/replay", response_model=ReplayStartResponse)
async def api_replay_batch_item(
    batch_id: str,
    batch_item_id: str,
    snapshot_id: Optional[int] = None,
) -> ReplayStartResponse:
    """Erzeugt einen kanonischen Replay-Lauf aus einem vorhandenen Snapshot des Batch-Items."""
    return _replay_batch_item(batch_id, batch_item_id, snapshot_id=snapshot_id)


@app.get("/v1/diagnostics/executions", response_model=ExecutionDiagnosticsResponse)
async def api_get_execution_diagnostics(
    limit: int = EXECUTION_DIAGNOSTICS_LIMIT,
    stuck_after_seconds: int = EXECUTION_STUCK_THRESHOLD_SECONDS,
    drift_sample_limit: int = NORMALIZATION_DRIFT_SAMPLE_LIMIT,
) -> ExecutionDiagnosticsResponse:
    """Operator-Diagnose für aktive/stuck Executions und normalize-mapping drift."""
    return _get_execution_diagnostics_payload(
        limit=limit,
        stuck_after_seconds=stuck_after_seconds,
        drift_sample_limit=drift_sample_limit,
    )


@app.get("/v1/debug/snapshots")
async def api_list_debug_snapshots(
    request_id: Optional[str] = None,
    job_id: Optional[str] = None,
    stage: Optional[str] = None,
    limit: int = 100,
) -> dict:
    """List stored debug snapshots for replay and regression analysis."""
    _snapshot_list = _get("debug_snapshot_list", debug_snapshot_list)
    rows = _snapshot_list(request_id=request_id, job_id=job_id, stage=stage, limit=limit)
    return {"snapshots": rows, "count": len(rows)}


@app.get("/v1/debug/snapshots/{snapshot_id}")
async def api_get_debug_snapshot(snapshot_id: int) -> dict:
    """Return one stored debug snapshot."""
    _snapshot_get = _get("debug_snapshot_get", debug_snapshot_get)
    row = _snapshot_get(snapshot_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"Debug snapshot '{snapshot_id}' not found")
    return row


@app.post("/v1/debug/snapshots/{snapshot_id}/replay/normalize")
async def api_replay_debug_snapshot_normalize(
    snapshot_id: int,
    template_name: Optional[str] = None,
    compact: bool = False,
) -> dict:
    """Replay normalization from a stored snapshot without new OCR/LLM calls."""
    _snapshot_get = _get("debug_snapshot_get", debug_snapshot_get)
    _replay_normalize = _get("replay_normalization_from_snapshot", replay_normalization_from_snapshot)
    row = _snapshot_get(snapshot_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"Debug snapshot '{snapshot_id}' not found")
    try:
        return await _replay_normalize(row, template_name=template_name, compact=compact)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/v1/tips")
async def api_tips() -> dict:
    """Usage tips and common patterns for Daigestr."""
    return _build_tips_dict()


# =============================================================================
# T-DAI-027: Mistral Batch Integration
# =============================================================================

async def _resolve_file_input(path: Optional[str], b64: Optional[str], filename: Optional[str], url: Optional[str]) -> tuple[bytes, str] | dict:
    """
    Löst path/base64/url zu (file_data, filename) auf.

    Returns:
        Tuple (bytes, filename) bei Erfolg,
        oder dict mit 'error' und 'error_code' bei Fehler.
    """
    _resolve_path = _get("resolve_path", resolve_path)
    _httpx = _get("httpx", httpx)
    _mistral_timeout = _get("MISTRAL_TIMEOUT", MISTRAL_TIMEOUT)
    _temp_dir = _get("TEMP_DIR", TEMP_DIR)

    if path:
        try:
            file_path = _resolve_path(path)
        except PathPolicyError as exc:
            return {
                "error": str(exc),
                "error_code": ErrorCode.PATH_NOT_ALLOWED,
                "details": {"path_policy_reason": exc.reason},
            }
        if not file_path.exists():
            return {"error": f"Datei nicht gefunden: {file_path}", "error_code": ErrorCode.FILE_NOT_FOUND}
        return file_path.read_bytes(), file_path.name

    if b64:
        if not filename:
            return {"error": "'filename' ist erforderlich bei Base64-Upload", "error_code": ErrorCode.INVALID_INPUT}
        try:
            file_data = base64.b64decode(b64)
        except Exception as e:
            return {"error": f"Ungültiges Base64: {str(e)}", "error_code": ErrorCode.INVALID_BASE64}
        return file_data, filename

    if url:
        try:
            async with _httpx.AsyncClient(timeout=float(_mistral_timeout)) as client:
                resp = await client.get(url, follow_redirects=True)
                resp.raise_for_status()
            guessed_filename = url.split("/")[-1].split("?")[0] or "document.pdf"
            return resp.content, guessed_filename
        except Exception as exc:
            return {"error": f"URL-Download fehlgeschlagen: {str(exc)}", "error_code": ErrorCode.CONVERSION_FAILED}

    return {"error": "Genau einer von 'path', 'base64' oder 'url' muss angegeben werden", "error_code": ErrorCode.INVALID_INPUT}


@app.post("/v1/prepare-batch")
async def api_prepare_batch(request: ConvertRequest) -> dict:
    """
    Bereitet einen Mistral Batch-Job vor.

    Extrahiert Bilder aus dem Dokument und baut Classify-Prompts zusammen,
    OHNE Vision-API-Calls zu machen. Gibt batch_jobs Array zurück das mit
    Mistral Batch API oder 'brix run convert-pdf-batch' verarbeitet werden kann.

    Returns:
        {
            "batch_jobs": [{"id": "page3_img0", "image_base64": "...", "prompt": "...", "mimetype": "..."}],
            "markdown": "...",
            "meta": {...}
        }
    """
    from converters.images import (
        extract_images_from_pdf,
        extract_images_from_docx,
        extract_images_from_pptx,
        extract_images_from_odt,
        extract_images_from_odp,
        extract_images_from_html,
        resize_image_if_needed,
    )
    from converters.office import convert_with_markitdown
    from utils import get_file_extension, detect_mimetype_from_bytes

    _get_file_extension = _get("get_file_extension", get_file_extension)
    _detect_mimetype = _get("detect_mimetype_from_bytes", detect_mimetype_from_bytes)
    _convert_markitdown = _get("convert_with_markitdown", convert_with_markitdown)
    _extract_imgs_pdf = _get("extract_images_from_pdf", extract_images_from_pdf)
    _extract_imgs_docx = _get("extract_images_from_docx", extract_images_from_docx)
    _extract_imgs_pptx = _get("extract_images_from_pptx", extract_images_from_pptx)
    _extract_imgs_odt = _get("extract_images_from_odt", extract_images_from_odt)
    _extract_imgs_odp = _get("extract_images_from_odp", extract_images_from_odp)
    _extract_imgs_html = _get("extract_images_from_html", extract_images_from_html)
    _resize_image = _get("resize_image_if_needed", resize_image_if_needed)
    _temp_dir = _get("TEMP_DIR", TEMP_DIR)

    # Eingabe auflösen
    file_result = await _resolve_file_input(request.path, request.base64, request.filename, request.url)
    if isinstance(file_result, dict):
        raise HTTPException(status_code=400, detail=file_result["error"])
    file_data, filename = file_result

    ext = _get_file_extension(filename)
    start_time = time.time()

    # Dokument zu Markdown konvertieren (ohne Vision)
    import hashlib as _hashlib
    temp_path = _temp_dir / f"{_hashlib.md5(file_data).hexdigest()}_{filename}"
    markdown = ""
    meta: dict = {
        "source": request.path or "base64" if request.path or request.base64 else (request.url or ""),
        "source_type": "file" if request.path else ("base64" if request.base64 else "url"),
        "format": ext.lstrip("."),
        "size_bytes": len(file_data),
    }

    try:
        temp_path.write_bytes(file_data)

        _MARKITDOWN_EXTS = {".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls",
                            ".odt", ".ods", ".odp", ".html", ".htm"}
        if ext in _MARKITDOWN_EXTS:
            result = _convert_markitdown(temp_path)
            if result["success"]:
                markdown = result["markdown"]
                if result.get("title"):
                    meta["title"] = result["title"]

        # Bilder extrahieren
        images: list[dict] = []
        _DESCRIBE_EXTS = {".docx", ".doc", ".pptx", ".ppt", ".pdf", ".odt", ".odp", ".html", ".htm"}
        if ext in _DESCRIBE_EXTS:
            if ext in {".docx", ".doc"}:
                images = _extract_imgs_docx(temp_path)
            elif ext in {".pptx", ".ppt"}:
                images = _extract_imgs_pptx(temp_path)
            elif ext == ".pdf":
                images = _extract_imgs_pdf(temp_path)
            elif ext == ".odt":
                images = _extract_imgs_odt(temp_path)
            elif ext == ".odp":
                images = _extract_imgs_odp(temp_path)
            elif ext in {".html", ".htm"}:
                images = _extract_imgs_html(temp_path)
    finally:
        temp_path.unlink(missing_ok=True)

    # Batch-Jobs aufbauen (ein Job pro Bild, kein Vision-API-Call)
    classify_prompt = (
        "Classify this image into EXACTLY one category. Reply with ONE word only:\n\n"
        "photo      = photograph of real-world objects, people, places\n"
        "chart      = bar chart, line chart, pie chart, data visualization with axes/values\n"
        "diagram    = flowchart, org chart, mind map, network diagram, UML, architecture diagram\n"
        "text_scan  = image of a document, form, invoice, letter, or any image where text is the primary content\n"
        "decorative = logo, icon, background image, decorative graphic with no information value\n\n"
        "Reply with exactly one of: photo, chart, diagram, text_scan, decorative"
    )

    batch_jobs: list[dict] = []
    for img in images:
        img_data = img["data"]
        # Resize vor Batch-Upload
        img_data_resized, _ = _resize_image(img_data)
        mimetype = _detect_mimetype(img_data_resized) or "image/png"
        batch_jobs.append({
            "id": img["name"],
            "image_base64": base64.b64encode(img_data_resized).decode("utf-8"),
            "prompt": classify_prompt,
            "mimetype": mimetype,
        })

    meta["duration_ms"] = int((time.time() - start_time) * 1000)
    meta["images_found"] = len(batch_jobs)

    log.info("prepare_batch_done", filename=filename, jobs=len(batch_jobs))
    return {
        "batch_jobs": batch_jobs,
        "markdown": markdown,
        "meta": meta,
    }


@app.post("/v1/apply-batch-results")
async def api_apply_batch_results(body: dict) -> dict:
    """
    Fügt Batch-Ergebnisse in ein Markdown-Dokument ein.

    Nimmt das Markdown aus prepare-batch und die Ergebnisse des Batch-Runs
    und ersetzt Bild-Platzhalter durch die Beschreibungen.

    Request body:
        {
            "markdown": "...",
            "batch_results": [{"id": "page3_img0", "description": "..."}]
        }

    Returns:
        {"markdown": "...", "images_inserted": N}
    """
    from converters.images import insert_image_descriptions

    _insert_img_desc = _get("insert_image_descriptions", insert_image_descriptions)

    markdown = body.get("markdown", "")
    batch_results = body.get("batch_results", [])

    if not isinstance(markdown, str):
        raise HTTPException(status_code=400, detail="'markdown' muss ein String sein")
    if not isinstance(batch_results, list):
        raise HTTPException(status_code=400, detail="'batch_results' muss ein Array sein")

    # batch_results in das Format konvertieren das insert_image_descriptions erwartet
    descriptions: list[dict] = []
    for item in batch_results:
        if isinstance(item, dict) and "id" in item and "description" in item:
            descriptions.append({"name": item["id"], "description": item["description"]})

    enriched_markdown = _insert_img_desc(markdown, descriptions)

    log.info("apply_batch_results_done", images_inserted=len(descriptions))
    return {
        "markdown": enriched_markdown,
        "images_inserted": len(descriptions),
    }


def run_rest_server():
    """Startet den REST-Server in einem separaten Thread."""
    import os
    uvicorn.run(
        app,
        host=os.getenv("BIND_HOST", "0.0.0.0"),
        port=REST_PORT,
        log_level=LOG_LEVEL.lower(),
    )
