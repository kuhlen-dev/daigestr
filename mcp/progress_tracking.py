"""
Canonical progress payload helpers for Daigestr jobs.
"""

from __future__ import annotations

from typing import Any, Optional


def build_progress_payload(
    *,
    status: str = "processing",
    current_stage: str,
    message: Optional[str] = None,
    percent: Optional[int] = None,
    request_id: Optional[str] = None,
    job_id: Optional[str] = None,
    attempt_number: Optional[int] = None,
    attempt_count: Optional[int] = None,
    attempt_mode: Optional[str] = None,
    page_current: Optional[int] = None,
    page_total: Optional[int] = None,
    upstream_attempt: Optional[int] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Materialize a canonical, pollable progress payload."""
    return {
        "status": status,
        "current_stage": current_stage,
        "message": message,
        "percent": percent,
        "request_id": request_id,
        "job_id": job_id,
        "attempt_number": attempt_number,
        "attempt_count": attempt_count,
        "attempt_mode": attempt_mode,
        "page_current": page_current,
        "page_total": page_total,
        "upstream_attempt": upstream_attempt,
        "metadata": metadata or {},
    }


def normalize_progress_payload(progress: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Normalize legacy and partial progress payloads into the canonical shape."""
    if progress is None or not isinstance(progress, dict):
        return None
    return build_progress_payload(
        status=progress.get("status", "processing"),
        current_stage=progress.get("current_stage") or progress.get("stage") or progress.get("step") or "processing",
        message=progress.get("message") or progress.get("detail"),
        percent=progress.get("percent"),
        request_id=progress.get("request_id"),
        job_id=progress.get("job_id"),
        attempt_number=progress.get("attempt_number"),
        attempt_count=progress.get("attempt_count"),
        attempt_mode=progress.get("attempt_mode"),
        page_current=progress.get("page_current"),
        page_total=progress.get("page_total"),
        upstream_attempt=progress.get("upstream_attempt"),
        metadata=progress.get("metadata"),
    )
