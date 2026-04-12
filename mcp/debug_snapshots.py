"""
Helpers for configurable intermediate snapshot capture.

T-DAI-083 introduces time-limited persistence of markdown/extraction/normalization
intermediates. This module keeps the capture policy deterministic and fully
driven by environment-backed settings.
"""

from __future__ import annotations

import copy
from typing import Any, Optional

from settings import (
    DEBUG_SNAPSHOTS_ENABLED,
    DEBUG_SNAPSHOTS_INCLUDE_ERRORS,
    DEBUG_SNAPSHOTS_INCLUDE_EXTRACTED,
    DEBUG_SNAPSHOTS_INCLUDE_MARKDOWN,
    DEBUG_SNAPSHOTS_INCLUDE_NORMALIZED,
    DEBUG_SNAPSHOTS_LONG_DOCUMENT_PAGE_THRESHOLD,
    DEBUG_SNAPSHOTS_LOW_QUALITY_THRESHOLD,
    DEBUG_SNAPSHOTS_POLICIES,
    DEBUG_SNAPSHOTS_RETENTION_DAYS,
)


def should_capture_debug_snapshot(
    *,
    success: Optional[bool],
    quality_score: Optional[float],
    page_count: Optional[int],
    retry_applied: Optional[bool],
    explicit: bool = False,
) -> bool:
    """Returns True when the current run should persist debug snapshots."""
    if explicit:
        return True
    if not DEBUG_SNAPSHOTS_ENABLED:
        return False

    policies = set(DEBUG_SNAPSHOTS_POLICIES)
    if "all" in policies:
        return True
    if "failures" in policies and success is False:
        return True
    if "retries" in policies and retry_applied:
        return True
    if (
        "low_quality" in policies
        and quality_score is not None
        and quality_score < DEBUG_SNAPSHOTS_LOW_QUALITY_THRESHOLD
    ):
        return True
    if (
        "long_documents" in policies
        and page_count is not None
        and page_count >= DEBUG_SNAPSHOTS_LONG_DOCUMENT_PAGE_THRESHOLD
    ):
        return True
    return False


def build_debug_snapshot_payload(
    *,
    request_id: Optional[str],
    job_id: Optional[str],
    filename: Optional[str],
    source_type: Optional[str],
    stage: str,
    attempt_number: Optional[int],
    attempt_count: Optional[int],
    attempt_mode: Optional[str],
    meta: Optional[dict[str, Any]] = None,
    markdown: Optional[str] = None,
    extracted: Optional[dict[str, Any]] = None,
    normalized: Optional[dict[str, Any]] = None,
    error: Optional[str] = None,
) -> dict[str, Any]:
    """Materializes a canonical debug snapshot payload according to env settings."""
    payload: dict[str, Any] = {
        "request_id": request_id,
        "job_id": job_id,
        "filename": filename,
        "source_type": source_type,
        "stage": stage,
        "attempt_number": attempt_number,
        "attempt_count": attempt_count,
        "attempt_mode": attempt_mode,
        "retention_days": DEBUG_SNAPSHOTS_RETENTION_DAYS,
        "meta": copy.deepcopy(meta) if isinstance(meta, dict) else {},
    }
    if DEBUG_SNAPSHOTS_INCLUDE_MARKDOWN:
        payload["markdown"] = markdown
    if DEBUG_SNAPSHOTS_INCLUDE_EXTRACTED:
        payload["extracted"] = copy.deepcopy(extracted) if isinstance(extracted, dict) else extracted
    if DEBUG_SNAPSHOTS_INCLUDE_NORMALIZED:
        payload["normalized"] = copy.deepcopy(normalized) if isinstance(normalized, dict) else normalized
    if DEBUG_SNAPSHOTS_INCLUDE_ERRORS:
        payload["error"] = error
    return payload


async def replay_normalization_from_snapshot(
    snapshot: dict[str, Any],
    *,
    template_name: Optional[str] = None,
    compact: bool = False,
) -> dict[str, Any]:
    """Re-run normalization from a stored snapshot without new OCR/LLM calls."""
    payload = snapshot.get("payload_json")
    if not isinstance(payload, dict):
        raise ValueError("Snapshot payload is missing or invalid")

    extracted = payload.get("extracted")
    if not isinstance(extracted, dict):
        raise ValueError("Snapshot has no extracted payload to replay")

    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    effective_template = template_name or meta.get("template_used") or meta.get("document_type")
    if not effective_template:
        raise ValueError("Snapshot has no template metadata for normalization replay")

    from normalizer import normalize  # noqa: PLC0415

    result = await normalize(extracted, effective_template, meta, compact=compact)
    return {
        "snapshot_id": snapshot.get("id"),
        "template_name": effective_template,
        **result,
    }
