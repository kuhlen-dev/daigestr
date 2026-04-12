"""
Core Routing-Logik für Daigestr.

Enthält:
- convert_auto: Intelligente Konvertierung basierend auf Dateityp
- convert_folder_contents: Konvertiert alle Dateien in einem Ordner
- convert_url: Konvertiert eine URL zu Markdown
- _build_tips_dict: Erstellt das Tips-Dictionary für /v1/tips und get_tips

Alle patchbaren Symbole werden über _get() aus dem server-Namespace gelesen,
damit Test-Patches auf _server.X korrekt funktionieren (gleiche Semantik wie
alle anderen extrahierten Module).
"""

import asyncio
import base64
import hashlib
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Optional, Any

import httpx
import structlog
from markitdown import MarkItDown as _MarkItDown

from models import (
    ConvertResponse,
    MetaData,
    ProgressState,
    ErrorCode,
    create_error_response,
    create_success_response,
)
from progress_tracking import build_progress_payload
from settings import (
    MAX_FILE_SIZE_BYTES,
    MAX_FILE_SIZE_MB,
    IMAGE_EXTENSIONS,
    MARKITDOWN_EXTENSIONS,
    AUDIO_EXTENSIONS,
    VIDEO_EXTENSIONS,
    MISTRAL_API_KEY,
    MISTRAL_TIMEOUT,
    TEMP_DIR,
    DATA_DIR,
    WHISPER_MODEL_SIZE,
    VERSION,
    CACHE_ENABLED,
    CACHE_TTL_SECONDS,
    MAX_DESCRIBE_IMAGES,
    CONVERT_TIMEOUT_SECONDS,
    BRIX_URL,
    PAGE_DESCRIBE_MAX_PAGES,
    AUDIT_ENABLED,
    QUALITY_RETRY_ENABLED,
    QUALITY_RETRY_THRESHOLD,
    QUALITY_RETRY_MODE,
)
from utils import (
    _get,
    get_file_extension,
    get_mimetype,
    detect_mimetype_from_bytes,
    should_skip_file,
    parse_pages,
)
from mistral_client import analyze_with_mistral_vision
from converters.images import (
    resize_image_if_needed,
    extract_image_metadata,
    extract_images_from_docx,
    extract_images_from_pptx,
    extract_images_from_pdf,
    extract_images_from_odt,
    extract_images_from_odp,
    extract_images_from_html,
    describe_embedded_images,
    insert_image_descriptions,
    render_first_page_as_image,
    render_pdf_pages_as_images,
    describe_page_images,
    insert_page_descriptions,
)
from converters.pdf import (
    is_scanned_pdf,
    convert_scanned_pdf,
    embed_ocr_in_pdf,
)
from converters.office import convert_with_markitdown
from converters.audio import extract_audio_from_video, transcribe_audio
from intelligence import (
    classify_document,
    correct_ocr_text,
    extract_structured_data,
    calculate_quality_score,
    chunk_markdown,
    dual_pass_validate,
    _apply_auto_extract,
)
from templates_db import get_all_template_ids, get_template_by_id, cache_get, cache_set
from audit_db import audit_log_event as _audit_log_event
from debug_snapshots import should_capture_debug_snapshot, build_debug_snapshot_payload
from debug_snapshot_db import debug_snapshot_store

log = structlog.get_logger()

# MarkItDown Instanz (für URL-Konvertierung)
_md = _MarkItDown()

_EXAMPLE_URL = "https://example.com"  # Example URL for documentation purposes only

# =============================================================================
# Brix Detection — T-DAI-027
# =============================================================================

_brix_available_cache: dict = {"available": None, "checked_at": 0}


def _apply_retry_meta(
    response: ConvertResponse,
    *,
    retry_applied: bool,
    retry_reason: Optional[str],
    initial_mode: str,
    final_mode: str,
    initial_quality_score: Optional[float],
    final_quality_score: Optional[float],
    retry_threshold_used: Optional[float],
) -> ConvertResponse:
    """Annotate the canonical meta block with retry decision metadata."""
    meta = response.meta.model_dump()
    meta.update(
        {
            "retry_applied": retry_applied,
            "retry_reason": retry_reason,
            "initial_mode": initial_mode,
            "final_mode": final_mode,
            "initial_quality_score": initial_quality_score,
            "final_quality_score": final_quality_score,
            "retry_threshold_used": retry_threshold_used,
            "attempt_count": 2 if retry_applied else 1,
            "attempt_mode": final_mode,
        }
    )
    response.meta = MetaData(**meta)
    return response


def _has_meaningful_extracted_payload(extracted: Any) -> bool:
    if not isinstance(extracted, dict) or not extracted:
        return False

    def _value_present(value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, dict):
            return any(
                _value_present(child)
                for key, child in value.items()
                if not str(key).startswith("_")
            )
        if isinstance(value, list):
            return any(_value_present(item) for item in value)
        return True

    return any(
        _value_present(value)
        for key, value in extracted.items()
        if not str(key).startswith("_")
    )


def _response_has_complete_extraction_contract(
    response: ConvertResponse,
    *,
    auto_extract: bool,
    extract_schema: Optional[dict[str, Any]],
    template: Optional[str],
) -> bool:
    extraction_requested = bool(auto_extract or extract_schema or template)
    if not extraction_requested:
        return response.success
    if not response.success:
        return False
    if not _has_meaningful_extracted_payload(response.extracted):
        return False
    if (auto_extract or template) and not getattr(response.meta, "template_used", None):
        return False
    return True


def _create_extraction_error_response(
    *,
    markdown: str,
    meta: dict[str, Any],
    message: str,
    details: Optional[dict[str, Any]] = None,
) -> ConvertResponse:
    error_response = create_error_response(
        ErrorCode.CONVERSION_FAILED,
        message,
        meta=meta,
        details=details,
    )
    error_response.markdown = markdown
    return error_response


def _bind_request_log_context(
    *,
    request_id: str,
    attempt_number: int,
    attempt_mode: str,
    filename: str,
    source_type: str,
    job_id: Optional[str],
) -> structlog.stdlib.BoundLogger:
    """Bind a stable log context for one logical convert attempt."""
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        request_id=request_id,
        attempt_number=attempt_number,
        attempt_mode=attempt_mode,
        filename=filename,
        source_type=source_type,
        job_id=job_id,
    )
    return log.bind(
        request_id=request_id,
        attempt_number=attempt_number,
        attempt_mode=attempt_mode,
        filename=filename,
        source_type=source_type,
        job_id=job_id,
    )


def _apply_explicit_template_meta(meta: dict[str, Any], template: Optional[str]) -> None:
    """Materialize explicit template selection into the canonical meta block."""
    if not template:
        return
    meta["template_used"] = template
    if meta.get("template_version") is not None:
        return
    _get_template = _get("get_template_by_id", get_template_by_id)
    try:
        tmpl = _get_template(template)
    except Exception as exc:
        log.warning("template_meta_lookup_failed", template=template, error=str(exc))
        return
    if tmpl:
        meta["template_version"] = tmpl.get("version", 1)


def _is_brix_available() -> bool:
    """Lazy Brix health-check mit 5-Minuten-Cache."""
    _brix_url = _get("BRIX_URL", BRIX_URL)
    if time.time() - _brix_available_cache["checked_at"] < 300:  # 5min Cache
        return bool(_brix_available_cache["available"])
    try:
        r = httpx.get(f"{_brix_url}/health", timeout=3)
        available = r.status_code == 200
    except Exception:
        available = False
    _brix_available_cache.update({"available": available, "checked_at": time.time()})
    return available


async def convert_url(url: str) -> dict[str, Any]:
    """Konvertiert eine URL zu Markdown (non-blocking via asyncio.to_thread)."""
    _md_inst = _get("md", _md)
    try:
        log.info("url_convert", url=url)
        result = await asyncio.to_thread(_md_inst.convert_url, url)
        return {
            "success": True,
            "markdown": result.text_content,
            "title": getattr(result, "title", None),
        }
    except Exception as e:
        log.error("url_convert_error", url=url, error=str(e))
        return {
            "success": False,
            "error_code": ErrorCode.CONVERSION_FAILED,
            "error": f"URL-Konvertierung fehlgeschlagen: {str(e)}"
        }


async def _apply_normalizer(
    response: "ConvertResponse",
    meta: dict,
    template_name: Optional[str],
    compact: bool,
) -> "ConvertResponse":
    """
    Führt die Normalisierung nach der Extraktion durch (T-DAI-055).

    Wird nur aufgerufen wenn:
    - response.extracted ist nicht None
    - template_name ist nicht None (aus meta.template_used, meta.document_type oder explizitem Template)
    """
    if response.extracted is None:
        return response
    if not template_name:
        return response
    try:
        from normalizer import normalize
        norm_result = await normalize(
            extracted=response.extracted,
            template_name=template_name,
            meta=meta,
            compact=compact,
        )
        if norm_result:
            response.normalized = norm_result.get("normalized")
            response.normalized_version = norm_result.get("normalized_version")
            response.normalized_warnings = norm_result.get("normalized_warnings")
            response.normalized_trace = norm_result.get("normalized_trace")
            response.normalized_context = norm_result.get("normalized_context")
            response.normalized_confidence = norm_result.get("normalized_confidence")
            log.info("normalizer_applied", template=template_name, compact=compact)
    except Exception as exc:
        log.warning("normalizer_error", template=template_name, error=str(exc))
    return response


def _apply_output_format(response: "ConvertResponse", output_format: str) -> "ConvertResponse":
    """Wendet output_format-Rendering auf eine erfolgreiche ConvertResponse an."""
    if not response.success or not response.markdown:
        return response
    if output_format == "html":
        from renderers.html import markdown_to_html
        response.html = markdown_to_html(response.markdown)
    elif output_format == "text":
        from renderers.text import markdown_to_text
        response.markdown = markdown_to_text(response.markdown)
    return response


async def finalize_url_markdown_response(
    markdown: str,
    *,
    meta: dict[str, Any],
    source: str,
    language: str,
    accuracy: str,
    classify: bool,
    classify_categories: Optional[list[str]],
    ocr_correct: bool,
    extract_schema: Optional[dict[str, Any]],
    template: Optional[str],
    auto_extract: bool,
    min_confidence: float,
    mode: str,
    retry_on_low_quality: Optional[bool],
    quality_retry_threshold: Optional[float],
    quality_retry_mode: Optional[str],
    chunk: bool,
    chunk_size: int,
    output_format: str,
    compact: bool,
    request_id: Optional[str] = None,
    attempt_number: int = 1,
    job_id: Optional[str] = None,
) -> ConvertResponse:
    """Finalize a URL-derived markdown response with the standard post-processing pipeline."""
    _classify_doc = _get("classify_document", classify_document)
    _correct_ocr = _get("correct_ocr_text", correct_ocr_text)
    _calc_quality = _get("calculate_quality_score", calculate_quality_score)
    _extract_struct = _get("extract_structured_data", extract_structured_data)
    _apply_auto = _get("_apply_auto_extract", _apply_auto_extract)
    _chunk_md = _get("chunk_markdown", chunk_markdown)

    request_id = request_id or meta.get("request_id") or str(uuid.uuid4())
    _attempt_log = _bind_request_log_context(
        request_id=request_id,
        attempt_number=attempt_number,
        attempt_mode=mode,
        filename=source,
        source_type="url",
        job_id=job_id,
    )

    effective_meta = dict(meta)
    effective_meta.setdefault("source", source)
    effective_meta.setdefault("source_type", "url")
    effective_meta["request_id"] = request_id
    effective_meta["job_id"] = job_id
    effective_meta["attempt_number"] = attempt_number
    effective_meta["attempt_mode"] = mode
    _apply_explicit_template_meta(effective_meta, template)

    retry_on_low_quality = QUALITY_RETRY_ENABLED if retry_on_low_quality is None else retry_on_low_quality
    quality_retry_threshold = QUALITY_RETRY_THRESHOLD if quality_retry_threshold is None else quality_retry_threshold
    quality_retry_mode = QUALITY_RETRY_MODE if quality_retry_mode is None else quality_retry_mode

    if mode == "full":
        accuracy = "high"
        classify = True
        ocr_correct = True
        auto_extract = True
        chunk = True

    effective_meta["accuracy_mode"] = accuracy

    markdown_text = markdown
    pipeline_steps: list[str] = ["url_fetch", "markitdown"]

    effective_ocr_correct = ocr_correct or (accuracy == "high")
    if effective_ocr_correct:
        ocr_result = await _correct_ocr(
            markdown_text,
            language,
            request_id=effective_meta.get("request_id"),
            attempt_number=effective_meta.get("attempt_number"),
        )
        if ocr_result.get("success"):
            markdown_text = ocr_result["corrected_text"]
            effective_meta["ocr_corrected"] = True
            effective_meta["ocr_corrections_count"] = ocr_result.get("corrections_count", 0)
            pipeline_steps.append("ocr_correction")

    if classify:
        classify_result = await _classify_doc(
            markdown_text,
            classify_categories,
            language,
            request_id=effective_meta.get("request_id"),
            attempt_number=effective_meta.get("attempt_number"),
        )
        effective_meta.update(classify_result)
        pipeline_steps.append("classify")

    effective_meta["pipeline_steps"] = pipeline_steps
    effective_meta.update(_calc_quality(markdown_text, effective_meta))

    hints: list[str] = []
    if not extract_schema and not auto_extract and effective_meta.get("document_type") == "invoice":
        hints.append("This document was classified as an invoice. Add template='invoice' to extract structured fields (invoice_number, total, line_items, etc.).")
    if not extract_schema and not auto_extract and effective_meta.get("document_type") and effective_meta.get("document_type") != "other":
        hints.append("Add auto_extract=true to automatically extract structured data based on document type.")
    if effective_meta.get("quality_grade") == "poor" and accuracy != "high":
        hints.append("Low quality score detected. Try accuracy='high' for better results on scanned or complex documents.")
    if hints:
        effective_meta["hints"] = hints

    response = create_success_response(markdown_text, meta=effective_meta)

    if auto_extract and not extract_schema:
        response = await _apply_auto(
            response,
            effective_meta,
            markdown_text,
            language,
            min_confidence,
            hints,
            request_id=effective_meta.get("request_id"),
            attempt_number=effective_meta.get("attempt_number"),
        )
        if not response.success:
            return response
    elif extract_schema:
        extraction = await _extract_struct(
            markdown_text,
            extract_schema,
            language,
            request_id=effective_meta.get("request_id"),
            attempt_number=effective_meta.get("attempt_number"),
        )
        if extraction["success"]:
            response.extracted = extraction["extracted"]
            updated_steps = list(effective_meta.get("pipeline_steps", pipeline_steps))
            if "schema_extraction" not in updated_steps:
                updated_steps.append("schema_extraction")
            effective_meta["pipeline_steps"] = updated_steps
            response.meta = MetaData(**{k: v for k, v in effective_meta.items()})
        else:
            return _create_extraction_error_response(
                markdown=markdown_text,
                meta=effective_meta,
                message="Schema-basierte Extraktion fehlgeschlagen",
                details={
                    "template_used": effective_meta.get("template_used") or template,
                    "extraction_error": extraction.get("error"),
                },
            )

    norm_template = effective_meta.get("template_used") or template or effective_meta.get("document_type")
    response = await _apply_normalizer(response, effective_meta, norm_template, compact)
    if chunk:
        response.chunks = _chunk_md(markdown_text, chunk_size=chunk_size, source=source)
    response = _apply_output_format(response, output_format)

    score = response.meta.quality_score
    response_contract_complete = _response_has_complete_extraction_contract(
        response,
        auto_extract=auto_extract,
        extract_schema=extract_schema,
        template=template,
    )
    should_retry = (
        retry_on_low_quality
        and mode == "default"
        and quality_retry_mode == "full"
        and (
            not response_contract_complete
            or (response.success and (score is None or score < quality_retry_threshold))
        )
    )
    if should_retry:
        if not response_contract_complete:
            retry_reason = "incomplete_extraction_contract"
        else:
            retry_reason = "missing_quality_score" if score is None else "low_quality"
        _attempt_log.info(
            "convert_retry_triggered",
            retry_reason=retry_reason,
            initial_quality_score=score,
            retry_threshold_used=float(quality_retry_threshold),
            next_mode=quality_retry_mode,
        )
        retried_response = await finalize_url_markdown_response(
            markdown,
            meta=meta,
            source=source,
            language=language,
            accuracy=accuracy,
            classify=classify,
            classify_categories=classify_categories,
            ocr_correct=ocr_correct,
            extract_schema=extract_schema,
            template=template,
            auto_extract=auto_extract,
            min_confidence=min_confidence,
            mode=quality_retry_mode,
            retry_on_low_quality=False,
            quality_retry_threshold=quality_retry_threshold,
            quality_retry_mode=quality_retry_mode,
            chunk=chunk,
            chunk_size=chunk_size,
            output_format=output_format,
            compact=compact,
            request_id=request_id,
            attempt_number=attempt_number + 1,
            job_id=job_id,
        )
        _attempt_log.info(
            "convert_retry_completed",
            retry_reason=retry_reason,
            initial_quality_score=score,
            final_quality_score=retried_response.meta.quality_score,
            retry_threshold_used=float(quality_retry_threshold),
            next_mode=quality_retry_mode,
        )
        retried_contract_complete = _response_has_complete_extraction_contract(
            retried_response,
            auto_extract=auto_extract,
            extract_schema=extract_schema,
            template=template,
        )
        final_response = retried_response
        final_mode = quality_retry_mode
        final_score = retried_response.meta.quality_score
        if response_contract_complete and not retried_contract_complete:
            final_response = response
            final_mode = mode
            final_score = score
        elif response_contract_complete and retried_contract_complete:
            comparable_initial = float(score) if isinstance(score, (int, float)) else float("-inf")
            comparable_retry = float(retried_response.meta.quality_score) if isinstance(retried_response.meta.quality_score, (int, float)) else float("-inf")
            if comparable_initial > comparable_retry:
                final_response = response
                final_mode = mode
                final_score = score
        elif not response_contract_complete and not retried_contract_complete:
            failure_meta = retried_response.meta.model_dump() if retried_response.meta else effective_meta
            final_response = _create_extraction_error_response(
                markdown=retried_response.markdown or response.markdown or markdown_text,
                meta=failure_meta,
                message="Strukturierte Extraktion lieferte in keinem Versuch einen vollständigen kanonischen Contract",
                details={
                    "initial_success": response.success,
                    "initial_quality_score": score,
                    "initial_template_used": getattr(response.meta, "template_used", None),
                    "retry_success": retried_response.success,
                    "retry_quality_score": getattr(retried_response.meta, "quality_score", None),
                    "retry_template_used": getattr(retried_response.meta, "template_used", None),
                },
            )
            final_mode = quality_retry_mode
            final_score = getattr(retried_response.meta, "quality_score", None)
        return _apply_retry_meta(
            final_response,
            retry_applied=True,
            retry_reason=retry_reason,
            initial_mode=mode,
            final_mode=final_mode,
            initial_quality_score=score,
            final_quality_score=final_score,
            retry_threshold_used=float(quality_retry_threshold),
        )

    return _apply_retry_meta(
        response,
        retry_applied=False,
        retry_reason=None,
        initial_mode=mode,
        final_mode=mode,
        initial_quality_score=score,
        final_quality_score=score,
        retry_threshold_used=float(quality_retry_threshold) if retry_on_low_quality else None,
    )


async def convert_auto(
    file_data: bytes,
    filename: str,
    source: str,
    source_type: str,
    input_meta: dict[str, Any],
    prompt: Optional[str] = None,
    language: str = "de",
    describe_images: bool = False,
    describe_pages: bool = False,
    classify: bool = False,
    classify_categories: list[str] | None = None,
    extract_schema: Optional[dict] = None,
    ocr_correct: bool = False,
    show_formulas: bool = False,
    chunk: bool = False,
    chunk_size: int = 512,
    accuracy: str = "standard",
    ocr_embed: bool = False,
    auto_extract: bool = False,
    min_confidence: float = 0.7,
    retry_on_low_quality: Optional[bool] = None,
    quality_retry_threshold: Optional[float] = None,
    quality_retry_mode: Optional[str] = None,
    mode: str = "default",
    output_format: str = "markdown",
    pages: Optional[str] = None,
    no_cache: bool = False,
    compact: bool = False,
    template: Optional[str] = None,
) -> ConvertResponse:
    """
    Intelligente Konvertierung basierend auf Dateityp.
    Kein interner Timeout — externe Limits (REST-Client, Brix-Pipeline) steuern.
    """
    request_id = input_meta.get("_request_id") or str(uuid.uuid4())
    effective_input_meta = {**input_meta, "_request_id": request_id}

    effective_retry_enabled = _get("QUALITY_RETRY_ENABLED", QUALITY_RETRY_ENABLED)
    if retry_on_low_quality is not None:
        effective_retry_enabled = retry_on_low_quality

    effective_retry_threshold = quality_retry_threshold
    if effective_retry_threshold is None:
        effective_retry_threshold = _get("QUALITY_RETRY_THRESHOLD", QUALITY_RETRY_THRESHOLD)

    effective_retry_mode = quality_retry_mode
    if effective_retry_mode is None:
        effective_retry_mode = _get("QUALITY_RETRY_MODE", QUALITY_RETRY_MODE)

    _server_module = sys.modules.get("server")
    _server_impl = None
    if _server_module is not None:
        _server_impl = getattr(_server_module, "__dict__", {}).get("_convert_auto_impl")
    _convert_auto_impl_fn = _server_impl or _convert_auto_impl
    _convert_timeout_seconds = _get("CONVERT_TIMEOUT_SECONDS", CONVERT_TIMEOUT_SECONDS)
    job_id = effective_input_meta.get("_job_id")

    def _ensure_correlation_meta(resp: ConvertResponse, *, current_mode: str, current_attempt: int, current_attempt_count: int) -> ConvertResponse:
        meta = resp.meta.model_dump()
        meta["request_id"] = request_id
        meta["job_id"] = job_id
        meta["attempt_number"] = current_attempt
        meta["attempt_count"] = current_attempt_count
        meta["attempt_mode"] = current_mode
        resp.meta = MetaData(**meta)
        return resp

    def _timeout_meta(*, current_mode: str, current_attempt: int, current_attempt_count: int) -> dict[str, Any]:
        return {
            "source": source,
            "source_type": source_type,
            "format": get_file_extension(filename).lstrip(".") or None,
            "size_bytes": len(file_data) if file_data is not None else None,
            "duration_ms": 0,
            "accuracy_mode": accuracy,
            "request_id": request_id,
            "job_id": job_id,
            "attempt_number": current_attempt,
            "attempt_count": current_attempt_count,
            "attempt_mode": current_mode,
        }

    async def _run_impl_with_timeout(*, current_mode: str, current_attempt: int, current_attempt_count: int, retry_flag: Optional[bool]) -> ConvertResponse:
        kwargs = dict(
            file_data=file_data,
            filename=filename,
            source=source,
            source_type=source_type,
            input_meta=effective_input_meta,
            prompt=prompt,
            language=language,
            describe_images=describe_images,
            describe_pages=describe_pages,
            classify=classify,
            classify_categories=classify_categories,
            extract_schema=extract_schema,
            ocr_correct=ocr_correct,
            show_formulas=show_formulas,
            chunk=chunk,
            chunk_size=chunk_size,
            accuracy=accuracy,
            ocr_embed=ocr_embed,
            auto_extract=auto_extract,
            min_confidence=min_confidence,
            retry_on_low_quality=retry_flag,
            quality_retry_threshold=quality_retry_threshold,
            quality_retry_mode=quality_retry_mode,
            mode=current_mode,
            output_format=output_format,
            pages=pages,
            no_cache=no_cache,
            compact=compact,
            template=template,
            request_id=request_id,
            attempt_number=current_attempt,
            attempt_count=current_attempt_count,
        )
        try:
            if _convert_timeout_seconds and float(_convert_timeout_seconds) > 0:
                resp = await asyncio.wait_for(
                    _convert_auto_impl_fn(**kwargs),
                    timeout=float(_convert_timeout_seconds),
                )
            else:
                resp = await _convert_auto_impl_fn(**kwargs)
        except asyncio.TimeoutError:
            return create_error_response(
                ErrorCode.TIMEOUT,
                f"Timeout nach {_convert_timeout_seconds} Sekunden bei convert_auto",
                meta=_timeout_meta(
                    current_mode=current_mode,
                    current_attempt=current_attempt,
                    current_attempt_count=current_attempt_count,
                ),
            )
        if resp is None:
            return create_error_response(
                ErrorCode.INTERNAL_ERROR,
                "_convert_auto_impl returned no response",
                meta=_timeout_meta(
                    current_mode=current_mode,
                    current_attempt=current_attempt,
                    current_attempt_count=current_attempt_count,
                ),
            )
        return _ensure_correlation_meta(
            resp,
            current_mode=current_mode,
            current_attempt=current_attempt,
            current_attempt_count=current_attempt_count,
        )

    response = await _run_impl_with_timeout(
        current_mode=mode,
        current_attempt=1,
        current_attempt_count=1,
        retry_flag=retry_on_low_quality,
    )

    extraction_requested = bool(auto_extract or extract_schema or template)
    initial_score = getattr(response.meta, "quality_score", None)
    initial_contract_complete = _response_has_complete_extraction_contract(
        response,
        auto_extract=auto_extract,
        extract_schema=extract_schema,
        template=template,
    )
    should_retry = (
        bool(effective_retry_enabled)
        and mode == "default"
        and extraction_requested
        and effective_retry_mode == "full"
        and (
            not initial_contract_complete
            or (response.success and (initial_score is None or initial_score < float(effective_retry_threshold)))
        )
    )

    if not should_retry:
        return _apply_retry_meta(
            response,
            retry_applied=False,
            retry_reason=None,
            initial_mode=mode,
            final_mode=mode,
            initial_quality_score=initial_score,
            final_quality_score=initial_score,
            retry_threshold_used=float(effective_retry_threshold) if effective_retry_enabled else None,
        )

    if not initial_contract_complete:
        retry_reason = "incomplete_extraction_contract"
    else:
        retry_reason = "missing_quality_score" if initial_score is None else "low_quality"
    retried_response = await _run_impl_with_timeout(
        current_mode=effective_retry_mode,
        current_attempt=2,
        current_attempt_count=2,
        retry_flag=False,
    )
    retried_contract_complete = _response_has_complete_extraction_contract(
        retried_response,
        auto_extract=auto_extract,
        extract_schema=extract_schema,
        template=template,
    )
    retried_score = getattr(retried_response.meta, "quality_score", None)

    selected_response = retried_response
    selected_mode = effective_retry_mode
    selected_score = retried_score

    if initial_contract_complete and not retried_contract_complete:
        selected_response = response
        selected_mode = mode
        selected_score = initial_score
    elif initial_contract_complete and retried_contract_complete:
        comparable_initial = float(initial_score) if isinstance(initial_score, (int, float)) else float("-inf")
        comparable_retry = float(retried_score) if isinstance(retried_score, (int, float)) else float("-inf")
        if comparable_initial > comparable_retry:
            selected_response = response
            selected_mode = mode
            selected_score = initial_score
    elif not initial_contract_complete and not retried_contract_complete:
        failure_meta = retried_response.meta.model_dump() if retried_response.meta else response.meta.model_dump()
        failure_meta.setdefault("request_id", getattr(response.meta, "request_id", None))
        failure_meta.setdefault("attempt_number", getattr(retried_response.meta, "attempt_number", None))
        failure_meta.setdefault("attempt_mode", effective_retry_mode)
        failure_meta.setdefault("template_used", getattr(retried_response.meta, "template_used", None))
        failure_meta.setdefault("template_version", getattr(retried_response.meta, "template_version", None))
        selected_response = _create_extraction_error_response(
            markdown=retried_response.markdown or response.markdown or "",
            meta=failure_meta,
            message="Strukturierte Extraktion lieferte in keinem Versuch einen vollständigen kanonischen Contract",
            details={
                "initial_success": response.success,
                "initial_quality_score": initial_score,
                "initial_template_used": getattr(response.meta, "template_used", None),
                "retry_success": retried_response.success,
                "retry_quality_score": retried_score,
                "retry_template_used": getattr(retried_response.meta, "template_used", None),
            },
        )
        selected_mode = effective_retry_mode
        selected_score = retried_score

    return _apply_retry_meta(
        selected_response,
        retry_applied=True,
        retry_reason=retry_reason,
        initial_mode=mode,
        final_mode=selected_mode,
        initial_quality_score=initial_score,
        final_quality_score=selected_score,
        retry_threshold_used=float(effective_retry_threshold),
    )


async def _convert_auto_impl(
    file_data: bytes,
    filename: str,
    source: str,
    source_type: str,
    input_meta: dict[str, Any],
    prompt: Optional[str] = None,
    language: str = "de",
    describe_images: bool = False,
    describe_pages: bool = False,
    classify: bool = False,
    classify_categories: list[str] | None = None,
    extract_schema: Optional[dict] = None,
    ocr_correct: bool = False,
    show_formulas: bool = False,
    chunk: bool = False,
    chunk_size: int = 512,
    accuracy: str = "standard",
    ocr_embed: bool = False,
    auto_extract: bool = False,
    min_confidence: float = 0.7,
    retry_on_low_quality: Optional[bool] = None,
    quality_retry_threshold: Optional[float] = None,
    quality_retry_mode: Optional[str] = None,
    mode: str = "default",
    output_format: str = "markdown",
    pages: Optional[str] = None,
    no_cache: bool = False,
    compact: bool = False,
    template: Optional[str] = None,
    request_id: Optional[str] = None,
    attempt_number: int = 1,
    attempt_count: int = 1,
) -> ConvertResponse:
    """
    Eigentliche Konvertierungslogik.

    Args:
        file_data: Rohe Datei-Bytes
        filename: Dateiname (wird für Extension-Erkennung genutzt)
        source: Quell-Pfad oder -Bezeichnung (für Metadaten)
        source_type: 'file', 'base64' oder 'url'
        input_meta: Beliebige Pass-through-Metadaten
        prompt: Optionaler Custom-Prompt für Vision
        language: Antwortsprache ('de' oder 'en')
        describe_images: Eingebettete Bilder in DOCX/PPTX/PDF/ODT/ODP/HTML durch Pixtral beschreiben
        classify: Dokumenttyp nach Konvertierung via LLM klassifizieren
        classify_categories: Erlaubte Dokumenttypen (überschreibt DEFAULT_CLASSIFY_CATEGORIES)
        extract_schema: JSON-Schema für strukturierte Daten-Extraktion (AC-014-1)
        ocr_correct: OCR-Nachkorrektur via LLM aktivieren (AC-015-5)
        show_formulas: Excel-Formeln im Output annotieren (FR-MKIT-007)
        chunk: Smart Chunking für RAG aktivieren (FR-MKIT-011)
        chunk_size: Maximale Chunk-Größe in Tokens (Default: 512)
        accuracy: Accuracy-Modus: 'standard' (Default) oder 'high'. High aktiviert
                  automatische OCR-Correction und Dual-Pass Vision-Validierung (T-MKIT-020).
        ocr_embed: Wenn True und das Dokument ein gescanntes PDF ist, wird der OCR-Text
                   als unsichtbare Textschicht eingebettet (T-MKIT-033).
    """
    start_time = time.time()
    request_id = request_id or input_meta.get("_request_id") or str(uuid.uuid4())

    # T-DAI-071: Audit-Logging — fire-and-forget
    _audit_enabled = _get("AUDIT_ENABLED", AUDIT_ENABLED)

    def _audit(event_type: str, **kwargs) -> None:
        if not _audit_enabled:
            return
        try:
            _audit_log_event(request_id=request_id, event_type=event_type, **kwargs)
        except Exception:
            pass

    # T-DAI-023: Job-Progress-Updates — _job_id wird aus input_meta gelesen (gesetzt von _run_async_job)
    _job_id = input_meta.get("_job_id")
    _attempt_log = _bind_request_log_context(
        request_id=request_id,
        attempt_number=attempt_number,
        attempt_mode=mode,
        filename=filename,
        source_type=source_type,
        job_id=_job_id,
    )
    _job_update_fn = _get("job_update", None)
    try:
        from templates_db import job_update as _default_job_update
    except Exception:
        _default_job_update = None

    _job_completed = False  # Guard: no more status updates after job_set_result

    def _parse_page_progress(detail: str) -> tuple[Optional[int], Optional[int]]:
        if not isinstance(detail, str):
            return None, None
        match = re.match(r"^page\s+(\d+)/(\d+)", detail.strip(), flags=re.IGNORECASE)
        if not match:
            return None, None
        return int(match.group(1)), int(match.group(2))

    def _update_progress(
        step: str,
        detail: str,
        progress: int,
        *,
        page_current: Optional[int] = None,
        page_total: Optional[int] = None,
        upstream_attempt: Optional[int] = None,
        extra_metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        if page_current is None and page_total is None:
            page_current, page_total = _parse_page_progress(detail)
        progress_payload = ProgressState(
            **build_progress_payload(
                status="processing",
                current_stage=step,
                message=detail,
                percent=progress,
                request_id=request_id,
                job_id=_job_id,
                attempt_number=attempt_number,
                attempt_count=attempt_count,
                attempt_mode=mode,
                page_current=page_current,
                page_total=page_total,
                upstream_attempt=upstream_attempt,
                metadata={
                    "source": source,
                    "filename": filename,
                    **(extra_metadata or {}),
                },
            )
        )
        _attempt_log.info(
            "convert_progress",
            step=step,
            detail=detail,
            progress=progress,
            attempt_count=attempt_count,
            source=source,
        )
        _audit("step", step=step, detail=detail, progress=progress)
        if _job_id and not _job_completed:
            import json as _json
            try:
                _fn = _get("job_update", _default_job_update)
                if _fn:
                    _fn(_job_id, "processing", progress_payload.model_dump_json())
            except Exception:
                pass

    _update_progress("start", "Starting conversion", 0)

    # T-DAI-071: Audit request event
    _audit(
        "request",
        detail=filename,
        metadata={
            "request_id": request_id,
            "job_id": _job_id,
            "attempt_number": attempt_number,
            "attempt_count": attempt_count,
            "attempt_mode": mode,
            "mode": mode,
            "accuracy": accuracy,
            "describe_images": describe_images,
            "classify": classify,
            "chunk": chunk,
            "pages": pages,
            "output_format": output_format,
            "size_bytes": len(file_data),
            "source_type": source_type,
        },
    )

    retry_on_low_quality = QUALITY_RETRY_ENABLED if retry_on_low_quality is None else retry_on_low_quality
    quality_retry_threshold = QUALITY_RETRY_THRESHOLD if quality_retry_threshold is None else quality_retry_threshold
    quality_retry_mode = QUALITY_RETRY_MODE if quality_retry_mode is None else quality_retry_mode

    # T-DAI-030: Mode-Resolution — 'full' = page-rendering, 'deep' = full + Einzelbilder
    if mode == "full":
        describe_pages = True
        accuracy = "high"
        classify = True
        ocr_correct = True
        auto_extract = True
        chunk = True
    elif mode == "deep":
        describe_pages = True
        describe_images = True
        accuracy = "high"
        classify = True
        ocr_correct = True
        auto_extract = True
        chunk = True

    # T-DAI-019: Request-Level-Cache — Cache-Key aus file_data + relevanten Parametern
    _cache_enabled = _get("CACHE_ENABLED", CACHE_ENABLED)
    _cache_ttl = _get("CACHE_TTL_SECONDS", CACHE_TTL_SECONDS)
    _cache_get = _get("cache_get", cache_get)
    _cache_set = _get("cache_set", cache_set)

    if _cache_enabled and not no_cache:
        _cache_key_data = (
            hashlib.sha256(file_data).hexdigest()
            + str(sorted({
                "filename": filename,
                "language": language,
                "describe_images": describe_images,
                "describe_pages": describe_pages,
                "classify": classify,
                "classify_categories": str(classify_categories),
                "extract_schema": str(sorted(extract_schema.items()) if extract_schema else None),
                "ocr_correct": ocr_correct,
                "show_formulas": show_formulas,
                "chunk": chunk,
                "chunk_size": chunk_size,
                "accuracy": accuracy,
                "ocr_embed": ocr_embed,
                "auto_extract": auto_extract,
                "min_confidence": min_confidence,
                "retry_on_low_quality": retry_on_low_quality,
                "quality_retry_threshold": quality_retry_threshold,
                "quality_retry_mode": quality_retry_mode,
                "mode": mode,
                "output_format": output_format,
                "pages": pages,
                "prompt": prompt,
            }.items()))
        )
        _cache_key = hashlib.sha256(_cache_key_data.encode()).hexdigest()
        _cached_json = _cache_get(_cache_key, _cache_ttl)
        if _cached_json is not None:
            try:
                _cr = ConvertResponse.model_validate_json(_cached_json)
                # Preserve the canonical meta shape on cache hits as well.
                _cm = _cr.meta.model_dump()
                _cm["cached"] = True
                _cm["request_id"] = request_id
                _cm["job_id"] = _job_id
                _cm["attempt_number"] = attempt_number
                _cm["attempt_count"] = attempt_count
                _cm["attempt_mode"] = mode
                _cr.meta = MetaData(**_cm)
                _audit(
                    "response",
                    detail=filename,
                    duration_ms=int((time.time() - start_time) * 1000),
                    metadata={"success": _cr.success, "markdown_length": len(_cr.markdown or ""), "cached": True},
                )
                return _cr
            except Exception as _cache_err:
                log.warning("cache_deserialize_error", error=str(_cache_err))
    else:
        _cache_key = None

    def _cache_and_return(resp: ConvertResponse) -> ConvertResponse:
        """Finalisiert Response mit Cache, Audit und optionalem Debug-Snapshot."""
        nonlocal _job_completed
        _job_completed = True  # Guard: prevent _update_progress from resetting status after this
        if _cache_enabled and _cache_key is not None and resp.success:
            try:
                _cache_set(_cache_key, resp.model_dump_json())
            except Exception as _ce:
                log.warning("cache_set_error", error=str(_ce))
        try:
            _should_capture_snapshot = _get("should_capture_debug_snapshot", should_capture_debug_snapshot)
            _build_snapshot_payload = _get("build_debug_snapshot_payload", build_debug_snapshot_payload)
            _snapshot_store = _get("debug_snapshot_store", debug_snapshot_store)
            _resp_meta = resp.meta.model_dump() if hasattr(resp.meta, "model_dump") else dict(meta)
            _page_count = _resp_meta.get("pages_processed") or _resp_meta.get("pages")
            if _page_count is None and isinstance(resp.markdown, str):
                _page_matches = re.findall(r"^## Seite \d+\s*$", resp.markdown, flags=re.MULTILINE)
                _page_count = len(_page_matches) or None
            if _should_capture_snapshot(
                success=resp.success,
                quality_score=_resp_meta.get("quality_score"),
                page_count=_page_count,
                retry_applied=_resp_meta.get("retry_applied"),
            ):
                _stage = "normalized_result"
                if not resp.success:
                    _stage = "error_result"
                elif resp.normalized is None and resp.extracted is not None:
                    _stage = "extract_result"
                elif resp.normalized is None and resp.extracted is None:
                    _stage = "convert_result"
                _payload = _build_snapshot_payload(
                    request_id=request_id,
                    job_id=_job_id,
                    filename=filename,
                    source_type=source_type,
                    stage=_stage,
                    attempt_number=attempt_number,
                    attempt_count=_resp_meta.get("attempt_count"),
                    attempt_mode=_resp_meta.get("attempt_mode"),
                    meta=_resp_meta,
                    markdown=resp.markdown,
                    extracted=resp.extracted,
                    normalized=resp.normalized,
                    error=resp.error.message if resp.error else None,
                )
                _snapshot_id = _snapshot_store(
                    request_id=request_id,
                    job_id=_job_id,
                    stage=_stage,
                    attempt_number=attempt_number,
                    attempt_count=_resp_meta.get("attempt_count"),
                    attempt_mode=_resp_meta.get("attempt_mode"),
                    filename=filename,
                    source_type=source_type,
                    payload=_payload,
                )
                _resp_meta["debug_snapshot_id"] = _snapshot_id
                resp.meta = MetaData(**_resp_meta)
        except Exception as _snapshot_err:
            log.warning("debug_snapshot_capture_failed", request_id=request_id, error=str(_snapshot_err))
        _audit(
            "response",
            detail=filename,
            duration_ms=int((time.time() - start_time) * 1000),
            metadata={
                "success": resp.success,
                "markdown_length": len(resp.markdown or ""),
                "cached": getattr(resp.meta, "cached", None),
            },
        )
        return resp

    # All patchable symbols read via _get() for test-patchability
    _get_file_extension = _get("get_file_extension", get_file_extension)
    _detect_mimetype = _get("detect_mimetype_from_bytes", detect_mimetype_from_bytes)
    _get_mimetype = _get("get_mimetype", get_mimetype)
    _max_file_size_bytes = _get("MAX_FILE_SIZE_BYTES", MAX_FILE_SIZE_BYTES)
    _max_file_size_mb = _get("MAX_FILE_SIZE_MB", MAX_FILE_SIZE_MB)
    _mistral_api_key = _get("MISTRAL_API_KEY", MISTRAL_API_KEY)
    _image_extensions = _get("IMAGE_EXTENSIONS", IMAGE_EXTENSIONS)
    _markitdown_extensions = _get("MARKITDOWN_EXTENSIONS", MARKITDOWN_EXTENSIONS)
    _audio_extensions = _get("AUDIO_EXTENSIONS", AUDIO_EXTENSIONS)
    _video_extensions = _get("VIDEO_EXTENSIONS", VIDEO_EXTENSIONS)
    _temp_dir = _get("TEMP_DIR", TEMP_DIR)
    _whisper_model_size = _get("WHISPER_MODEL_SIZE", WHISPER_MODEL_SIZE)

    _resize_image = _get("resize_image_if_needed", resize_image_if_needed)
    _extract_img_meta = _get("extract_image_metadata", extract_image_metadata)
    _analyze_vision = _get("analyze_with_mistral_vision", analyze_with_mistral_vision)
    _correct_ocr = _get("correct_ocr_text", correct_ocr_text)
    _dual_pass = _get("dual_pass_validate", dual_pass_validate)
    _classify_doc = _get("classify_document", classify_document)
    _calc_quality = _get("calculate_quality_score", calculate_quality_score)
    _apply_auto = _get("_apply_auto_extract", _apply_auto_extract)
    _extract_struct = _get("extract_structured_data", extract_structured_data)
    _chunk_md = _get("chunk_markdown", chunk_markdown)
    _extract_imgs_docx = _get("extract_images_from_docx", extract_images_from_docx)
    _extract_imgs_pptx = _get("extract_images_from_pptx", extract_images_from_pptx)
    _extract_imgs_pdf = _get("extract_images_from_pdf", extract_images_from_pdf)
    _extract_imgs_odt = _get("extract_images_from_odt", extract_images_from_odt)
    _extract_imgs_odp = _get("extract_images_from_odp", extract_images_from_odp)
    _extract_imgs_html = _get("extract_images_from_html", extract_images_from_html)
    _describe_imgs = _get("describe_embedded_images", describe_embedded_images)
    _insert_img_desc = _get("insert_image_descriptions", insert_image_descriptions)
    _render_page = _get("render_first_page_as_image", render_first_page_as_image)
    _render_pdf_pages = _get("render_pdf_pages_as_images", render_pdf_pages_as_images)
    _describe_page_imgs = _get("describe_page_images", describe_page_images)
    _insert_page_desc = _get("insert_page_descriptions", insert_page_descriptions)
    _max_describe_pages = _get("PAGE_DESCRIBE_MAX_PAGES", PAGE_DESCRIBE_MAX_PAGES)
    _is_scanned = _get("is_scanned_pdf", is_scanned_pdf)
    _convert_scanned = _get("convert_scanned_pdf", convert_scanned_pdf)
    _embed_ocr = _get("embed_ocr_in_pdf", embed_ocr_in_pdf)
    _convert_markitdown = _get("convert_with_markitdown", convert_with_markitdown)
    _extract_audio = _get("extract_audio_from_video", extract_audio_from_video)
    _transcribe = _get("transcribe_audio", transcribe_audio)
    _max_describe_images = _get("MAX_DESCRIBE_IMAGES", MAX_DESCRIBE_IMAGES)
    _parse_pages = _get("parse_pages", parse_pages)

    ext = _get_file_extension(filename)
    mimetype = _detect_mimetype(file_data) or _get_mimetype(Path(filename))

    # T-DAI-030: Non-PDF Fallback — Page-Rendering nur für PDFs, sonst Einzelbilder
    if describe_pages and ext != ".pdf":
        describe_images = True

    meta = {
        **input_meta,
        "source": source,
        "source_type": source_type,
        "format": ext.lstrip("."),
        "size_bytes": len(file_data),
        "request_id": request_id,
        "job_id": _job_id,
        "attempt_number": attempt_number,
        "attempt_count": attempt_count,
        "attempt_mode": mode,
    }
    _apply_explicit_template_meta(meta, template)

    # Größenprüfung
    if len(file_data) > _max_file_size_bytes:
        meta["duration_ms"] = int((time.time() - start_time) * 1000)
        _err_resp = create_error_response(
            ErrorCode.FILE_TOO_LARGE,
            f"Datei zu groß: {len(file_data) / 1024 / 1024:.1f}MB (Max: {_max_file_size_mb}MB)",
            meta=meta
        )
        return _cache_and_return(_err_resp)

    # T-MKIT-020: Accuracy-Modus immer in Meta dokumentieren
    meta["accuracy_mode"] = accuracy

    # T-MKIT-023: High-Accuracy ohne API-Key → Warning + Degradierung auf Standard
    # Nur für Dateitypen die Mistral API nutzen (Bilder, PDFs) — nicht für Audio/Video (Whisper)
    _needs_mistral_api = ext in _image_extensions or ext in _markitdown_extensions
    if accuracy == "high" and not _mistral_api_key and _needs_mistral_api:
        log.warning("high_accuracy_degraded_no_api_key")
        meta["accuracy_warning"] = "High accuracy requested but MISTRAL_API_KEY not set — running in standard mode"
        accuracy = "standard"
        meta["accuracy_mode"] = accuracy

    # Bild → Vision
    if ext in _image_extensions or (mimetype and mimetype.startswith("image/")):
        processed_data, resize_meta = _resize_image(file_data)
        meta.update(resize_meta)
        meta["vision_used"] = True

        if prompt:
            vision_prompt = prompt
        else:
            from templates_db import get_prompt as _get_prompt  # noqa: PLC0415
            vision_prompt = _get("get_prompt", _get_prompt)("vision", "default", language=language)

        result = await _analyze_vision(
            processed_data,
            mimetype or "image/jpeg",
            vision_prompt,
            language,
            request_id=request_id,
            attempt_number=attempt_number,
            pipeline_step="image_vision",
            filename=filename,
        )

        meta["duration_ms"] = int((time.time() - start_time) * 1000)

        if result["success"]:
            meta["vision_model"] = result.get("vision_model")
            meta["tokens_prompt"] = result.get("tokens_prompt")
            meta["tokens_completion"] = result.get("tokens_completion")
            meta["tokens_total"] = result.get("tokens_total")

            # T-MKIT-027: EXIF/GPS/IPTC Metadaten aus Original-Bilddaten extrahieren
            try:
                img_metadata = _extract_img_meta(file_data)
                if img_metadata.get("exif"):
                    meta["exif"] = img_metadata["exif"]
                if img_metadata.get("iptc"):
                    meta["iptc"] = img_metadata["iptc"]
            except Exception as _exif_err:
                log.debug("exif_extraction_skipped", error=str(_exif_err))

            markdown = result["markdown"]
            pipeline_steps: list[str] = ["vision"]

            # AC-015: OCR-Nachkorrektur via LLM (T-MKIT-022: auch bei accuracy="high" automatisch aktiv)
            if ocr_correct or accuracy == "high":
                log.info("ocr_correct_start", path="vision", accuracy=accuracy)
                correction = await _correct_ocr(
                    markdown,
                    language=language,
                    request_id=request_id,
                    attempt_number=attempt_number,
                )
                if correction["success"]:
                    markdown = correction["corrected_text"]
                    meta["ocr_corrected"] = True
                    meta["ocr_corrections_count"] = correction["corrections_count"]
                    log.info("ocr_correct_done_vision", corrections=correction["corrections_count"])
                else:
                    log.warning("ocr_correct_failed_vision", error=correction.get("error"))
                    meta["ocr_corrected"] = False

            # T-MKIT-020: High-Accuracy-Pipeline für Bilder
            if accuracy == "high":
                log.info("high_accuracy_image_dual_pass_start", file=filename)
                effective_mimetype = mimetype or "image/jpeg"
                markdown = await _dual_pass(
                    markdown=markdown,
                    file_data=processed_data,
                    mimetype=effective_mimetype,
                    language=language,
                )
                pipeline_steps.append("dual_pass_validation")

            meta["pipeline_steps"] = pipeline_steps

            if classify:
                classify_result = await _classify_doc(
                    markdown,
                    classify_categories,
                    language,
                    request_id=request_id,
                    attempt_number=attempt_number,
                )
                meta.update(classify_result)
            # AC-010: Quality Scoring
            meta.update(_calc_quality(markdown, meta))
            # T-MKIT-032: Response hints for LLM consumers
            hints: list[str] = []
            if not extract_schema and not auto_extract and meta.get("document_type") == "invoice":
                hints.append("This document was classified as an invoice. Add template='invoice' to extract structured fields (invoice_number, total, line_items, etc.).")
            if not extract_schema and not auto_extract and meta.get("document_type") and meta.get("document_type") != "other":
                hints.append("Add auto_extract=true to automatically extract structured data based on document type.")
            if meta.get("quality_grade") == "poor" and accuracy != "high":
                hints.append("Low quality score detected. Try accuracy='high' for better results on scanned or complex documents.")
            if hints:
                meta["hints"] = hints
            response = create_success_response(markdown, meta=meta)
            # T-MKIT-036: Auto-Extract (classify → template lookup → extraction)
            if auto_extract and not extract_schema:
                response = await _apply_auto(
                    response,
                    meta,
                    markdown,
                    language,
                    min_confidence,
                    hints,
                    request_id=request_id,
                    attempt_number=attempt_number,
                )
                if not response.success:
                    return _cache_and_return(response)
            # AC-014-2/AC-014-3: Strukturierte Extraktion falls Schema gesetzt
            elif extract_schema:
                extraction = await _extract_struct(
                    markdown,
                    extract_schema,
                    language,
                    request_id=request_id,
                    attempt_number=attempt_number,
                )
                if extraction["success"]:
                    response.extracted = extraction["extracted"]
                    if accuracy == "high":
                        pipeline_steps_updated = list(meta.get("pipeline_steps", pipeline_steps))
                        if "schema_extraction" not in pipeline_steps_updated:
                            pipeline_steps_updated.append("schema_extraction")
                        meta["pipeline_steps"] = pipeline_steps_updated
                        response.meta = MetaData(**{
                            k: v for k, v in meta.items()
                        })
                else:
                    return _cache_and_return(
                        _create_extraction_error_response(
                            markdown=markdown,
                            meta=meta,
                            message="Schema-basierte Extraktion fehlgeschlagen",
                            details={"extraction_error": extraction.get("error")},
                        )
                    )
            # T-DAI-055: Normalisierung nach Extraktion
            _norm_template = meta.get("template_used") or template or meta.get("document_type")
            response = await _apply_normalizer(response, meta, _norm_template, compact)
            # FR-MKIT-011: Smart Chunking für RAG
            if chunk:
                response.chunks = _chunk_md(markdown, chunk_size=chunk_size, source=source)
            response = _apply_output_format(response, output_format)
            return _cache_and_return(response)
        else:
            _err = create_error_response(result.get("error_code", ErrorCode.VISION_FAILED), result["error"], meta=meta)
            return _cache_and_return(_err)

    # Audio/Video → faster-whisper Transkription (FR-MKIT-006)
    elif ext in _audio_extensions or ext in _video_extensions:
        temp_media_path = _temp_dir / f"{hashlib.md5(file_data).hexdigest()}_{filename}"
        extracted_wav: Optional[Path] = None
        try:
            temp_media_path.write_bytes(file_data)
            audio_path = temp_media_path

            # Video: erst Audio-Track extrahieren
            if ext in _video_extensions:
                log.info("video_audio_extract_start", file=filename)
                try:
                    extracted_wav = _extract_audio(temp_media_path)
                    audio_path = extracted_wav
                except RuntimeError as exc:
                    meta["duration_ms"] = int((time.time() - start_time) * 1000)
                    _err = create_error_response(ErrorCode.CONVERSION_FAILED, f"Audio-Extraktion fehlgeschlagen: {str(exc)}", meta=meta)
                    return _cache_and_return(_err)

            # Transkribieren
            transcription = _transcribe(audio_path)
            meta["duration_ms"] = int((time.time() - start_time) * 1000)

            if not transcription["success"]:
                _err = create_error_response(ErrorCode.CONVERSION_FAILED, transcription.get("error", "Transkription fehlgeschlagen"), meta=meta)
                return _cache_and_return(_err)

            # Meta-Daten setzen (AC-006-6)
            meta["language"] = transcription.get("language", "unknown")
            meta["duration_seconds"] = transcription.get("duration", 0.0)
            meta["whisper_model"] = transcription.get("model_size", _whisper_model_size)
            meta["accuracy_mode"] = accuracy  # T-MKIT-022: accuracy_mode auch bei Audio/Video

            transcript_text = transcription.get("text", "")
            markdown = f"# Transkription\n\n{transcript_text}"

            log.info(
                "audio_transcription_done",
                file=filename,
                language=meta["language"],
                duration=meta["duration_seconds"],
                chars=len(transcript_text),
            )

            # AC-010: Quality Scoring
            meta.update(_calc_quality(markdown, meta))

            audio_pipeline_steps: list[str] = ["transcription"]

            if classify:
                classify_result = await _classify_doc(markdown, classify_categories, language)
                meta.update(classify_result)
                audio_pipeline_steps.append("classify")

            meta["pipeline_steps"] = audio_pipeline_steps

            # T-MKIT-032: Response hints for LLM consumers
            hints: list[str] = []
            if not extract_schema and not auto_extract and meta.get("document_type") == "invoice":
                hints.append("This transcript was classified as invoice. Add template='invoice' to extract structured fields.")
            if not extract_schema and not auto_extract and meta.get("document_type") and meta.get("document_type") != "other":
                hints.append("Add auto_extract=true to automatically extract structured data based on document type.")
            if not chunk:
                hints.append("Add chunk=true to split this transcript into RAG-ready segments.")
            if meta.get("language"):
                hints.append(f"Detected language: {meta['language']}.")
            if hints:
                meta["hints"] = hints

            response = create_success_response(markdown, meta=meta)

            # T-MKIT-036: Auto-Extract
            if auto_extract and not extract_schema:
                response = await _apply_auto(
                    response,
                    meta,
                    markdown,
                    language,
                    min_confidence,
                    hints,
                    request_id=request_id,
                    attempt_number=attempt_number,
                )
                if not response.success:
                    return _cache_and_return(response)
                if "extraction" not in audio_pipeline_steps:
                    audio_pipeline_steps.append("extraction")
                meta["pipeline_steps"] = audio_pipeline_steps
            # AC-014-2/AC-014-3: Strukturierte Extraktion falls Schema gesetzt
            elif extract_schema:
                extraction = await _extract_struct(markdown, extract_schema, language)
                if extraction["success"]:
                    response.extracted = extraction["extracted"]
                    if "extraction" not in audio_pipeline_steps:
                        audio_pipeline_steps.append("extraction")
                    meta["pipeline_steps"] = audio_pipeline_steps
                else:
                    return _cache_and_return(
                        _create_extraction_error_response(
                            markdown=markdown,
                            meta=meta,
                            message="Schema-basierte Extraktion fehlgeschlagen",
                            details={"extraction_error": extraction.get("error")},
                        )
                    )

            # T-DAI-055: Normalisierung nach Extraktion
            _norm_template = meta.get("template_used") or template or meta.get("document_type")
            response = await _apply_normalizer(response, meta, _norm_template, compact)
            # FR-MKIT-011: Smart Chunking für RAG
            if chunk:
                response.chunks = _chunk_md(markdown, chunk_size=chunk_size, source=source)
            response = _apply_output_format(response, output_format)
            return _cache_and_return(response)

        finally:
            temp_media_path.unlink(missing_ok=True)
            if extracted_wav is not None:
                extracted_wav.unlink(missing_ok=True)

    # Dokument → MarkItDown (mit optionalem Scanned-PDF-Routing)
    elif ext in _markitdown_extensions or ext:
        temp_path = _temp_dir / f"{hashlib.md5(file_data).hexdigest()}_{filename}"
        original_temp_path = temp_path
        sliced_temp_path: Path | None = None
        try:
            temp_path.write_bytes(file_data)

            # T-DAI-025: PDF Page Selection — parse pages spec into 0-based indices
            _page_indices: list[int] | None = None
            if pages and ext == ".pdf":
                try:
                    import fitz as _fitz  # noqa: PLC0415
                    _doc = _fitz.open(str(temp_path))
                    _total_pages = len(_doc)
                    _doc.close()
                    _page_indices = _parse_pages(pages, _total_pages)
                    meta["pages_requested"] = pages
                    meta["pages_selected"] = [i + 1 for i in _page_indices]
                    log.info("pdf_page_selection", spec=pages, indices=_page_indices, total=_total_pages)
                except ValueError as _pe:
                    meta["duration_ms"] = int((time.time() - start_time) * 1000)
                    _err = create_error_response(ErrorCode.INVALID_INPUT, f"Ungültige Seitenauswahl: {str(_pe)}", meta=meta)
                    return _cache_and_return(_err)
                except Exception as _pe:
                    log.warning("pdf_page_selection_failed", error=str(_pe))
                    # Fallback: alle Seiten

            # Scanned PDF Detection: VOR dem normalen markitdown-Pfad prüfen
            if ext == ".pdf":
                _update_progress("scan_detection", filename, 5)
            if ext == ".pdf" and _is_scanned(temp_path):
                log.info(
                    "scanned_pdf_detected",
                    file=filename,
                    request_id=request_id,
                    job_id=_job_id,
                    attempt_number=attempt_number,
                    attempt_mode=mode,
                )
                result = await _convert_scanned(
                    temp_path,
                    language=language,
                    page_indices=_page_indices,
                    request_id=request_id,
                    attempt_number=attempt_number,
                )
                meta["duration_ms"] = int((time.time() - start_time) * 1000)

                if result["success"]:
                    meta["scanned"] = True
                    meta["vision_model"] = result.get("vision_model")
                    meta["ocr_model"] = result.get("ocr_model")
                    meta["tokens_prompt"] = result.get("tokens_prompt")
                    meta["tokens_completion"] = result.get("tokens_completion")
                    meta["tokens_total"] = result.get("tokens_total")
                    meta["tokens_per_page"] = result.get("tokens_per_page")
                    meta["pages_processed"] = result.get("pages_processed") or result.get("pages")
                    for _ocr_meta_field in (
                        "ocr_table_format",
                        "ocr_table_count",
                        "ocr_headers",
                        "ocr_footers",
                        "ocr_confidence_granularity",
                        "ocr_pages_with_confidence",
                        "ocr_average_page_confidence",
                        "ocr_minimum_page_confidence",
                    ):
                        meta[_ocr_meta_field] = result.get(_ocr_meta_field)
                    if result.get("ocr_model"):
                        # OCR3-Pfad: kein Vision
                        meta["vision_used"] = False
                    else:
                        meta["vision_used"] = True

                    scanned_markdown = result["markdown"]
                    scanned_pipeline_steps: list[str] = ["ocr"]

                    # AC-015: OCR-Nachkorrektur via LLM (nur wenn explizit aktiviert ODER accuracy=high)
                    if ocr_correct or accuracy == "high":
                        log.info("ocr_correct_start", path="scanned_pdf", accuracy=accuracy)
                        correction = await _correct_ocr(
                            scanned_markdown,
                            language=language,
                            request_id=request_id,
                            attempt_number=attempt_number,
                        )
                        if correction["success"]:
                            scanned_markdown = correction["corrected_text"]
                            meta["ocr_corrected"] = True
                            meta["ocr_corrections_count"] = correction["corrections_count"]
                            log.info("ocr_correct_done_scanned_pdf", corrections=correction["corrections_count"])
                            scanned_pipeline_steps.append("ocr_correction")
                        else:
                            log.warning("ocr_correct_failed_scanned_pdf", error=correction.get("error"))
                            meta["ocr_corrected"] = False

                    # T-MKIT-020: High-Accuracy → Dual-Pass Validation für gescannte PDFs
                    if accuracy == "high":
                        pages_processed = result.get("pages_processed") or result.get("pages")
                        single_page_scan = pages_processed == 1
                        if single_page_scan:
                            log.info("high_accuracy_scanned_pdf_dual_pass_start", file=filename)
                            rendered = _render_page(temp_path)
                            if rendered is not None:
                                page_image_bytes, page_mimetype = rendered
                                scanned_markdown = await _dual_pass(
                                    markdown=scanned_markdown,
                                    file_data=page_image_bytes,
                                    mimetype=page_mimetype,
                                    language=language,
                                )
                                scanned_pipeline_steps.append("dual_pass_validation")
                            else:
                                log.warning("high_accuracy_scanned_pdf_dual_pass_skipped_no_pdf2image")
                        else:
                            log.info(
                                "high_accuracy_scanned_pdf_dual_pass_skipped_multipage",
                                file=filename,
                                pages_processed=pages_processed,
                            )

                    meta["pipeline_steps"] = scanned_pipeline_steps

                    if classify:
                        _update_progress("classify", filename, 70)
                        classify_result = await _classify_doc(
                            scanned_markdown,
                            classify_categories,
                            language,
                            request_id=request_id,
                            attempt_number=attempt_number,
                        )
                        meta.update(classify_result)
                    # AC-010: Quality Scoring
                    meta.update(_calc_quality(scanned_markdown, meta))
                    # T-MKIT-032: Response hints for LLM consumers
                    scanned_hints: list[str] = []
                    if not extract_schema and not auto_extract and meta.get("document_type") == "invoice":
                        scanned_hints.append("This document was classified as an invoice. Add template='invoice' to extract structured fields (invoice_number, total, line_items, etc.).")
                    if not extract_schema and not auto_extract and meta.get("document_type") and meta.get("document_type") != "other":
                        scanned_hints.append("Add auto_extract=true to automatically extract structured data based on document type.")
                    if meta.get("quality_grade") == "poor" and accuracy != "high":
                        scanned_hints.append("Low quality score detected. Try accuracy='high' for better results on scanned or complex documents.")
                    if not ocr_correct and accuracy != "high":
                        scanned_hints.append("This is a scanned document. Enable ocr_correct=true or accuracy='high' to fix common OCR errors.")
                    if meta.get("scanned") and not ocr_embed:
                        scanned_hints.append("This scanned PDF has no searchable text layer. Add ocr_embed=true to create a searchable PDF with embedded OCR text.")
                    if scanned_hints:
                        meta["hints"] = scanned_hints
                    response = create_success_response(scanned_markdown, meta=meta)
                    # T-MKIT-033: OCR Embed — invisible text layer in scanned PDF
                    if ocr_embed:
                        pages_text_list: list[str] | None = None
                        if result.get("pages_text"):
                            pages_text_list = result["pages_text"]
                        embedded = _embed_ocr(file_data, scanned_markdown, pages_text_list)
                        if embedded is not None:
                            response.enriched_pdf = base64.b64encode(embedded).decode("utf-8")
                            log.info("ocr_embed_done", size_bytes=len(embedded))
                        else:
                            log.warning("ocr_embed_failed_no_output")
                    # T-MKIT-036: Auto-Extract (classify → template lookup → extraction)
                    if auto_extract and not extract_schema:
                        _update_progress("extract", filename, 80)
                        response = await _apply_auto(
                            response,
                            meta,
                            scanned_markdown,
                            language,
                            min_confidence,
                            scanned_hints,
                            request_id=request_id,
                            attempt_number=attempt_number,
                        )
                        if not response.success:
                            return _cache_and_return(response)
                    # AC-014-2/AC-014-3: Strukturierte Extraktion falls Schema gesetzt
                    elif extract_schema:
                        _update_progress("extract", filename, 80)
                        extraction = await _extract_struct(
                            scanned_markdown,
                            extract_schema,
                            language,
                            request_id=request_id,
                            attempt_number=attempt_number,
                        )
                        if extraction["success"]:
                            response.extracted = extraction["extracted"]
                            if accuracy == "high":
                                updated_steps = list(meta.get("pipeline_steps", scanned_pipeline_steps))
                                if "schema_extraction" not in updated_steps:
                                    updated_steps.append("schema_extraction")
                                meta["pipeline_steps"] = updated_steps
                                response.meta = MetaData(**{
                                    k: v for k, v in meta.items()
                                })
                        else:
                            return _cache_and_return(
                                _create_extraction_error_response(
                                    markdown=scanned_markdown,
                                    meta=meta,
                                    message="Schema-basierte Extraktion fehlgeschlagen",
                                    details={"extraction_error": extraction.get("error")},
                                )
                            )
                    # T-DAI-055: Normalisierung nach Extraktion
                    _norm_template = meta.get("template_used") or template or meta.get("document_type")
                    response = await _apply_normalizer(response, meta, _norm_template, compact)
                    # FR-MKIT-011: Smart Chunking für RAG
                    if chunk:
                        _update_progress("chunk", filename, 90)
                        response.chunks = _chunk_md(scanned_markdown, chunk_size=chunk_size, source=source)
                    _update_progress("render", output_format, 95)
                    _update_progress("done", filename, 100)
                    response = _apply_output_format(response, output_format)
                    return _cache_and_return(response)
                else:
                    _err = create_error_response(result.get("error_code", ErrorCode.CONVERSION_FAILED), result["error"], meta=meta)
                    return _cache_and_return(_err)

            # T-DAI-025: Non-scanned PDF page selection — create temp PDF with only requested pages
            if ext == ".pdf" and _page_indices is not None:
                try:
                    import fitz as _fitz_mkit  # noqa: PLC0415
                    _mkit_doc = _fitz_mkit.open(str(temp_path))
                    _mkit_doc.select(_page_indices)
                    _sliced_path = _temp_dir / f"sliced_{temp_path.name}"
                    _mkit_doc.save(str(_sliced_path))
                    _mkit_doc.close()
                    sliced_temp_path = _sliced_path
                    temp_path = _sliced_path
                    log.info("pdf_page_slice_done", indices=_page_indices, sliced_path=str(_sliced_path))
                except Exception as _slice_err:
                    log.warning("pdf_page_slice_failed", error=str(_slice_err))
                    # Fallback: use full PDF

            _update_progress("markitdown", filename, 10)
            result = _convert_markitdown(temp_path, show_formulas=show_formulas)
            meta["duration_ms"] = int((time.time() - start_time) * 1000)

            if result["success"]:
                if result.get("title"):
                    meta["title"] = result["title"]

                # FR-MKIT-007: Excel-spezifische Metadaten
                if ext in {".xlsx", ".xls"}:
                    if result.get("sheets_count") is not None:
                        meta["sheets_count"] = result["sheets_count"]
                    if result.get("charts_count") is not None:
                        meta["charts_count"] = result["charts_count"]
                    # T-MKIT-026: Hidden sheets
                    if result.get("hidden_sheets"):
                        meta["hidden_sheets"] = result["hidden_sheets"]

                # T-MKIT-024: ZUGFeRD/Factur-X Daten aus convert_with_markitdown übernehmen
                zugferd_extracted = result.get("zugferd")
                if zugferd_extracted is not None:
                    meta["zugferd"] = zugferd_extracted

                # T-MKIT-025: XMP Metadata + Embedded Files übernehmen
                xmp_extracted = result.get("xmp_metadata")
                if xmp_extracted is not None:
                    meta["xmp_metadata"] = xmp_extracted

                embedded_extracted = result.get("embedded_files")
                if embedded_extracted is not None:
                    meta["embedded_files"] = embedded_extracted

                # T-MKIT-028: Office Document Properties übernehmen
                doc_props = result.get("document_properties")
                if doc_props is not None:
                    meta["document_properties"] = doc_props

                # T-MKIT-029: Email Metadata übernehmen
                email_meta = result.get("email_metadata")
                if email_meta is not None:
                    meta["email_routing"] = email_meta.get("routing")
                    meta["email_thread"] = email_meta.get("thread")
                    meta["calendar_events"] = email_meta.get("calendar_events")

                # T-MKIT-030: PPTX Hidden Slides übernehmen
                pptx_hidden = result.get("pptx_hidden_info")
                if pptx_hidden is not None and pptx_hidden.get("hidden_slide_count", 0) > 0:
                    meta["hidden_slides"] = pptx_hidden["hidden_slide_count"]

                markdown = result["markdown"]
                markitdown_pipeline_steps: list[str] = ["markitdown"]

                # T-DAI-030: Page-Level Vision-Beschreibung für PDFs
                if describe_pages and ext == ".pdf":
                    log.info("page_rendering_start", file=filename)
                    _page_images = _render_pdf_pages(temp_path, page_indices=_page_indices)
                    if _page_images:
                        if len(_page_images) > _max_describe_pages:
                            log.warning("pages_truncated", file=filename, total=len(_page_images), limit=_max_describe_pages)
                            _page_images = _page_images[:_max_describe_pages]
                            meta["pages_truncated"] = True
                        _update_progress(
                            "describe_pages",
                            f"{len(_page_images)} pages to describe",
                            25,
                            page_current=0,
                            page_total=len(_page_images),
                        )
                        import inspect as _insp_pages
                        _dp_kwargs: dict = {"language": language}
                        _dp_sig = _insp_pages.signature(_describe_page_imgs)
                        if "progress_callback" in _dp_sig.parameters:
                            _dp_kwargs["progress_callback"] = lambda detail, pct: _update_progress("describe_page", detail, pct)
                        if "request_id" in _dp_sig.parameters:
                            _dp_kwargs["request_id"] = request_id
                        if "attempt_number" in _dp_sig.parameters:
                            _dp_kwargs["attempt_number"] = attempt_number
                        if "filename" in _dp_sig.parameters:
                            _dp_kwargs["filename"] = filename
                        _page_descs = await _describe_page_imgs(_page_images, **_dp_kwargs)
                        markdown = _insert_page_desc(markdown, _page_descs)
                        meta["pages_described"] = len(_page_descs)
                        markitdown_pipeline_steps.append("page_descriptions")
                        log.info("page_rendering_done", file=filename, count=len(_page_descs))

                # Eingebettete Bilder beschreiben (DOCX, PPTX, PDF, ODT, ODP, HTML)
                _DESCRIBE_IMGS_EXTS = {
                    ".docx", ".doc", ".pptx", ".ppt",
                    ".pdf", ".odt", ".odp", ".html", ".htm",
                }
                if describe_images and ext in _DESCRIBE_IMGS_EXTS:
                    log.info("embedded_images_describe_start", file=filename, ext=ext)
                    if ext in {".docx", ".doc"}:
                        images = _extract_imgs_docx(temp_path)
                    elif ext in {".pptx", ".ppt"}:
                        images = _extract_imgs_pptx(temp_path)
                    elif ext == ".pdf":
                        images = _extract_imgs_pdf(temp_path, page_indices=_page_indices)
                    elif ext == ".odt":
                        images = _extract_imgs_odt(temp_path)
                    elif ext == ".odp":
                        images = _extract_imgs_odp(temp_path)
                    else:  # .html / .htm
                        images = _extract_imgs_html(temp_path)

                    if images:
                        # T-DAI-027: Brix batch hint für Dokumente mit vielen Bildern
                        _brix_hint_images = images  # snapshot for hint check
                        if len(_brix_hint_images) > 10 and _is_brix_available():
                            existing_hints = meta.get("hints", [])
                            meta["hints"] = existing_hints + [
                                f"This document has {len(_brix_hint_images)} images. For faster processing, "
                                f"use Brix batch: POST /v1/prepare-batch then 'brix run convert-pdf-batch'"
                            ]
                        # FIX 3 (T-DAI-024): Limit auf MAX_DESCRIBE_IMAGES — verhindert OOM/Timeout bei großen PDFs
                        total_images = len(images)
                        if total_images > _max_describe_images:
                            log.warning(
                                "images_truncated",
                                file=filename,
                                total=total_images,
                                limit=_max_describe_images,
                            )
                            images = images[:_max_describe_images]
                            meta["images_truncated"] = True
                        _update_progress("describe_images", f"{len(images)} images found", 30)
                        import inspect as _inspect
                        _describe_kwargs: dict = {"language": language}
                        _describe_sig = _inspect.signature(_describe_imgs)
                        if "progress_callback" in _describe_sig.parameters:
                            _describe_kwargs["progress_callback"] = lambda detail, pct: _update_progress("describe_image", detail, pct)
                        descriptions = await _describe_imgs(images, **_describe_kwargs)
                        markdown = _insert_img_desc(markdown, descriptions)
                        meta["images_described"] = len(descriptions)
                        if meta.get("images_truncated"):
                            hint_msg = (
                                f"Document has {total_images} images, only first {_max_describe_images} were described. "
                                f"Increase MAX_DESCRIBE_IMAGES env to process more."
                            )
                            existing_hints = meta.get("hints", [])
                            meta["hints"] = existing_hints + [hint_msg]
                        log.info(
                            "embedded_images_described",
                            file=filename,
                            count=len(descriptions),
                        )

                # T-MKIT-020: High-Accuracy → Dual-Pass Validation für PDFs und Bilder
                if accuracy == "high" and ext == ".pdf":
                    log.info("high_accuracy_pdf_dual_pass_start", file=filename)
                    rendered = _render_page(temp_path)
                    if rendered is not None:
                        page_image_bytes, page_mimetype = rendered
                        markdown = await _dual_pass(
                            markdown=markdown,
                            file_data=page_image_bytes,
                            mimetype=page_mimetype,
                            language=language,
                        )
                        markitdown_pipeline_steps.append("dual_pass_validation")
                    else:
                        log.warning("high_accuracy_pdf_dual_pass_skipped_no_pdf2image")

                meta["pipeline_steps"] = markitdown_pipeline_steps

                if classify:
                    _update_progress("classify", filename, 70)
                    classify_result = await _classify_doc(
                        markdown,
                        classify_categories,
                        language,
                        request_id=request_id,
                        attempt_number=attempt_number,
                    )
                    meta.update(classify_result)

                # AC-010: Quality Scoring
                meta.update(_calc_quality(markdown, meta))
                # T-MKIT-032: Response hints for LLM consumers
                doc_hints: list[str] = []
                if not extract_schema and not auto_extract and meta.get("document_type") == "invoice":
                    doc_hints.append("This document was classified as an invoice. Add template='invoice' to extract structured fields (invoice_number, total, line_items, etc.).")
                if not extract_schema and not auto_extract and meta.get("document_type") and meta.get("document_type") != "other":
                    doc_hints.append("Add auto_extract=true to automatically extract structured data based on document type.")
                if meta.get("quality_grade") == "poor" and accuracy != "high":
                    doc_hints.append("Low quality score detected. Try accuracy='high' for better results on scanned or complex documents.")
                if meta.get("scanned") and not ocr_correct and accuracy != "high":
                    doc_hints.append("This is a scanned document. Enable ocr_correct=true or accuracy='high' to fix common OCR errors.")
                _HINT_EXTS = (".docx", ".pptx", ".pdf", ".odt", ".odp", ".html", ".htm")
                if not describe_images and ext in _HINT_EXTS and source_type == "file":
                    doc_hints.append("This document may contain embedded images. Add describe_images=true to describe them via Vision AI.")
                # T-MKIT-024: ZUGFeRD hint wenn erkannt aber kein template/extract_schema
                if meta.get("zugferd") is not None and not extract_schema and not auto_extract:
                    doc_hints.append("This PDF contains embedded ZUGFeRD/Factur-X e-invoice data. Add template='invoice' to get the structured data in the extracted field — no LLM needed, 100% accurate.")
                if doc_hints:
                    meta["hints"] = doc_hints
                response = create_success_response(markdown, meta=meta)
                # T-MKIT-036: Auto-Extract (classify → template lookup → extraction)
                if auto_extract and not extract_schema:
                    _update_progress("extract", filename, 80)
                    response = await _apply_auto(
                        response,
                        meta,
                        markdown,
                        language,
                        min_confidence,
                        doc_hints,
                        request_id=request_id,
                        attempt_number=attempt_number,
                    )
                    if not response.success:
                        return _cache_and_return(response)
                # T-MKIT-024: ZUGFeRD Daten direkt als extracted verwenden (kein LLM nötig)
                elif extract_schema and meta.get("zugferd") is not None:
                    _update_progress("extract", filename, 80)
                    response.extracted = meta["zugferd"]
                    meta["zugferd_source"] = "embedded_xml"
                    meta["extraction_method"] = "zugferd"
                    response.meta = MetaData(**{k: v for k, v in meta.items()})
                    log.info("zugferd_used_as_extracted", file=filename)
                elif extract_schema:
                    _update_progress("extract", filename, 80)
                    # AC-014-2/AC-014-3: Strukturierte Extraktion falls Schema gesetzt (kein ZUGFeRD)
                    extraction = await _extract_struct(
                        markdown,
                        extract_schema,
                        language,
                        request_id=request_id,
                        attempt_number=attempt_number,
                    )
                    if extraction["success"]:
                        response.extracted = extraction["extracted"]
                        if accuracy == "high":
                            updated_steps = list(meta.get("pipeline_steps", markitdown_pipeline_steps))
                            if "schema_extraction" not in updated_steps:
                                updated_steps.append("schema_extraction")
                            meta["pipeline_steps"] = updated_steps
                            response.meta = MetaData(**{
                                k: v for k, v in meta.items()
                            })
                    else:
                        return _cache_and_return(
                            _create_extraction_error_response(
                                markdown=markdown,
                                meta=meta,
                                message="Schema-basierte Extraktion fehlgeschlagen",
                                details={"extraction_error": extraction.get("error")},
                            )
                        )
                # T-DAI-055: Normalisierung nach Extraktion
                _norm_template = meta.get("template_used") or template or meta.get("document_type")
                response = await _apply_normalizer(response, meta, _norm_template, compact)
                # FR-MKIT-011: Smart Chunking für RAG
                if chunk:
                    _update_progress("chunk", filename, 90)
                    response.chunks = _chunk_md(markdown, chunk_size=chunk_size, source=source)
                _update_progress("render", output_format, 95)
                _update_progress("done", filename, 100)
                response = _apply_output_format(response, output_format)
                return _cache_and_return(response)
            else:
                _err = create_error_response(result.get("error_code", ErrorCode.CONVERSION_FAILED), result["error"], meta=meta)
                return _cache_and_return(_err)
        finally:
            temp_path.unlink(missing_ok=True)
            if sliced_temp_path is not None and original_temp_path != sliced_temp_path:
                original_temp_path.unlink(missing_ok=True)

    else:
        meta["duration_ms"] = int((time.time() - start_time) * 1000)
        _err = create_error_response(ErrorCode.UNSUPPORTED_FORMAT, f"Nicht unterstütztes Format: {ext}", meta=meta)
        return _cache_and_return(_err)


async def convert_folder_contents(
    folder_path: Path,
    input_meta: dict[str, Any],
    language: str = "de",
    describe_images: bool = False,
    classify: bool = False,
    classify_categories: list[str] | None = None,
    extract_schema: Optional[dict] = None,
    auto_extract: bool = False,
    accuracy: str = "standard",
    chunk: bool = False,
    chunk_size: int = 512,
    ocr_correct: bool = False,
    ocr_embed: bool = False,
    show_formulas: bool = False,
    prompt: Optional[str] = None,
    template: Optional[str] = None,
    min_confidence: float = 0.7,
    mode: Optional[str] = None,
) -> ConvertResponse:
    """
    Konvertiert alle Dateien in einem Ordner zu einem zusammengeführten Markdown.
    """
    start_time = time.time()
    log.info("folder_convert_start", folder=str(folder_path))

    _should_skip = _get("should_skip_file", should_skip_file)
    _convert_auto = _get("convert_auto", convert_auto)

    if not folder_path.exists():
        return create_error_response(
            ErrorCode.FILE_NOT_FOUND,
            f"Ordner nicht gefunden: {folder_path}",
            meta=input_meta
        )

    if not folder_path.is_dir():
        return create_error_response(
            ErrorCode.INVALID_INPUT,
            f"Kein Ordner: {folder_path}",
            meta=input_meta
        )

    # Dateien sammeln
    files = sorted([
        f for f in folder_path.iterdir()
        if f.is_file() and not _should_skip(f.name)
    ], key=lambda f: f.name.lower())

    if not files:
        return create_error_response(
            ErrorCode.INVALID_INPUT,
            f"Keine Dateien im Ordner: {folder_path}",
            meta=input_meta
        )

    # Ergebnisse sammeln
    markdown_parts = []
    file_results = []
    total_tokens = 0
    files_processed = 0
    files_failed = 0

    for file_path in files:
        file_meta = {"filename": file_path.name}

        try:
            file_data = file_path.read_bytes()
            result = await _convert_auto(
                file_data=file_data,
                filename=file_path.name,
                source=str(file_path),
                source_type="file",
                input_meta=input_meta,
                language=language,
                describe_images=describe_images,
                classify=classify,
                classify_categories=classify_categories,
                extract_schema=extract_schema,
                auto_extract=auto_extract,
                accuracy=accuracy,
                chunk=chunk,
                chunk_size=chunk_size,
                ocr_correct=ocr_correct,
                ocr_embed=ocr_embed,
                show_formulas=show_formulas,
                prompt=prompt,
                template=template,
                min_confidence=min_confidence,
                mode=mode or "default",
            )

            if result.success:
                files_processed += 1
                markdown_parts.append(f"\n\n## {file_path.name}\n\n{result.markdown}")
                file_meta["success"] = True
                file_meta["size_bytes"] = len(file_data)

                if result.meta:
                    if hasattr(result.meta, 'tokens_total') and result.meta.tokens_total:
                        total_tokens += result.meta.tokens_total
                        file_meta["tokens"] = result.meta.tokens_total
                    if hasattr(result.meta, 'vision_used') and result.meta.vision_used:
                        file_meta["vision_used"] = True
            else:
                files_failed += 1
                file_meta["success"] = False
                file_meta["error"] = result.error.message if result.error else "Unknown error"
                markdown_parts.append(f"\n\n## {file_path.name}\n\n*Konvertierung fehlgeschlagen*")

        except Exception as e:
            files_failed += 1
            file_meta["success"] = False
            file_meta["error"] = str(e)
            log.error("folder_file_error", file=file_path.name, error=str(e))

        file_results.append(file_meta)

    # Zusammenführen
    combined_markdown = f"# {folder_path.name}\n" + "".join(markdown_parts)

    meta = {
        **input_meta,
        "source": str(folder_path),
        "source_type": "folder",
        "files_processed": files_processed,
        "files_failed": files_failed,
        "files_total": len(files),
        "tokens_total": total_tokens,
        "files": file_results,
        "duration_ms": int((time.time() - start_time) * 1000),
    }

    log.info("folder_convert_complete",
             folder=str(folder_path),
             processed=files_processed,
             failed=files_failed,
             duration_ms=meta["duration_ms"])

    return create_success_response(combined_markdown, meta=meta)


def _build_tips_dict() -> dict:
    """Builds the tips dictionary used by both MCP get_tips tool and REST /v1/tips endpoint."""
    data_dir = str(DATA_DIR)
    return {
        "service": f"Daigestr — Document Intelligence Service v{VERSION}",
        "quick_reference": {
            "convert_file": {"endpoint": "POST /v1/convert", "mcp_tool": "convert", "params": {"path": f"{data_dir}/file.pdf"}},
            "convert_url": {"endpoint": "POST /v1/convert", "mcp_tool": "convert", "params": {"url": _EXAMPLE_URL}},
            "convert_base64": {"endpoint": "POST /v1/convert", "mcp_tool": "convert", "params": {"base64_data": "...", "filename": "file.pdf"}},
            "extract_invoice": {"endpoint": "POST /v1/extract", "mcp_tool": "extract", "params": {"path": f"{data_dir}/invoice.pdf", "template": "invoice"}},
            "extract_custom": {"endpoint": "POST /v1/convert", "mcp_tool": "convert", "params": {"path": f"{data_dir}/doc.pdf", "extract_schema": {"type": "object", "properties": {"title": {"type": "string"}}}}},
        },
        "common_mistakes": [
            {"problem": "extracted is null", "cause": "Missing extract_schema or template parameter", "fix": "Add template='invoice' or extract_schema={...} to get structured JSON in the extracted field"},
            {"problem": "auto_extract returns an error", "cause": "No matching template, insufficient classification confidence, or extraction contract incomplete", "fix": "Check meta.document_type, meta.document_type_confidence, meta.template_used, and error.details; register a template or raise confidence/quality before retrying"},
            {"problem": "chunks is null", "cause": "Missing chunk parameter", "fix": "Add chunk=true to get RAG-ready chunks in the chunks field"},
            {"problem": "Poor quality on scanned PDFs", "cause": "Using standard accuracy", "fix": "Set accuracy='high' for OCR correction + dual-pass validation"},
            {"problem": "Images in DOCX/PPTX/PDF/ODT/ODP/HTML not described", "cause": "describe_images defaults to false", "fix": "Set describe_images=true (costs extra API calls)"},
            {"problem": "Document type not detected", "cause": "classify defaults to false", "fix": "Set classify=true to get document_type in meta"},
            {"problem": "OCR errors in output", "cause": "No post-correction", "fix": "Set ocr_correct=true or accuracy='high' (auto-enables correction)"},
            {"problem": "ocr_embed has no effect", "cause": "Only works on scanned PDFs", "fix": "ocr_embed only embeds OCR text layer in scanned PDFs — has no effect on text PDFs or other formats"},
            {"problem": "classify_categories ignored", "cause": "classify defaults to false", "fix": "Set classify=true to use custom categories"},
            {"problem": "prompt has no effect on documents", "cause": "prompt only affects Vision analysis", "fix": "prompt is only used for image files processed via Vision API, not for documents"},
            {"problem": "Audit log entries missing or incorrect", "cause": "Direct DB writes bypass validation and indexing", "fix": "Do NOT modify audit_log directly in DB — use audit_log_event() from audit_db.py for all writes"},
        ],
        "available_templates": get_all_template_ids(),
        "available_formats": {
            "documents": ["pdf", "docx", "doc", "pptx", "ppt", "xlsx", "xls", "odt", "ods", "odp", "html", "csv", "txt", "md", "rtf", "json", "xml"],
            "images": ["jpg", "jpeg", "png", "gif", "webp", "bmp"],
            "audio": ["mp3", "wav", "ogg", "flac", "m4a"],
            "video": ["mp4", "mkv", "webm", "avi", "mov"],
        },
        "optional_features": {
            "accuracy": {"values": ["standard", "high"], "default": "standard", "description": "high activates OCR correction + dual-pass vision validation for scanned documents"},
            "classify": {"type": "bool", "default": False, "description": "Detect document type (invoice, contract, cv, etc.) with confidence score in meta"},
            "extract_schema": {"type": "dict", "default": None, "description": "JSON Schema for structured extraction — result in 'extracted' field. Without this, extracted is always null."},
            "template": {"type": "str", "default": None, "description": "Shortcut for extract_schema. Use GET /v1/templates or /v1/tips for the live template registry."},
            "auto_extract": {"type": "bool", "default": False, "description": "Automatically classify document, find matching template, and extract structured data — all in one call. No template or extract_schema needed."},
            "min_confidence": {"type": "float", "default": 0.7, "description": "Minimum classification confidence for auto_extract to use a template (0.0-1.0)"},
            "retry_on_low_quality": {"type": "bool|null", "default": None, "description": "Allow Daigestr to retry once with a stronger mode when the initial quality score is too low. Null = env default."},
            "quality_retry_threshold": {"type": "float|null", "default": None, "description": "Quality threshold for the retry decision. Null = env default."},
            "quality_retry_mode": {"values": ["full"], "default": None, "description": "Escalation mode for low-quality retry. Currently only 'full' is supported."},
            "describe_images": {"type": "bool", "default": False, "description": "Extract embedded images from PDF, DOCX, PPTX, ODT, ODP, HTML and auto-classify each one: diagrams → Mermaid syntax, charts → data tables, photos → descriptions, text scans → OCR, decorative → skipped"},
            "ocr_correct": {"type": "bool", "default": False, "description": "LLM post-correction for OCR errors"},
            "ocr_embed": {"type": "bool", "default": False, "description": "Embed OCR text layer into PDF — creates searchable PDF output"},
            "show_formulas": {"type": "bool", "default": False, "description": "Show Excel formulas in output"},
            "chunk": {"type": "bool", "default": False, "description": "Split output into RAG-ready chunks"},
            "chunk_size": {"type": "int", "default": 512, "description": "Approximate chunk size in tokens"},
            "language": {"type": "str", "default": "de", "description": "Language for Vision/OCR responses"},
            "zugferd": {"type": "bool", "default": "auto", "description": "Extract ZUGFeRD/Factur-X e-invoice data from PDF (auto-detected, 100% accuracy, no LLM required)"},
            "mode": {"values": ["default", "full", "deep"], "default": "default", "description": "full enables page-level rendering for PDFs + all features (accuracy=high, classify, ocr_correct, auto_extract, chunk). deep adds per-image extraction with classification (diagram→Mermaid, chart→table, photo→description)."},
            "prompt": {"type": "str", "default": None, "description": "Custom prompt for Vision analysis (images only)"},
            "classify_categories": {"type": "list", "default": None, "description": "Custom classification categories (requires classify=true)"},
            "output_format": {"values": ["markdown", "html", "text"], "default": "markdown", "description": "Output format. html includes Mermaid rendering, CSS, syntax highlighting. text strips all Markdown syntax."},
            "pages": {"type": "str", "default": None, "description": "PDF page selection. Syntax: '1-3', '7,14,22', '10-20,!15'. Null = all pages."},
            "webhook_url": {"type": "str", "default": None, "description": "URL to POST the result to when conversion completes. Errors logged silently."},
            "no_cache": {"type": "bool", "default": False, "description": "Bypass cache and force fresh conversion. Result is still cached for future requests."},
        },
        "response_fields": {
            "markdown": "Always present — the converted document as Markdown",
            "extracted": "Present when extract_schema, template, or auto_extract produced structured JSON. Otherwise null.",
            "chunks": "Only present when chunk=true — list of text segments with metadata",
            "meta": "Always present — processing metadata (quality_score, duration, pipeline_steps, etc.)",
            "html": "Only present when output_format='html' — standalone HTML page with CSS and Mermaid rendering",
        },
        "response_contract": {
            "success_response_fields": {
                "success": "Always present.",
                "markdown": "Always present on successful convert/extract responses. May be empty string only for truly empty source content.",
                "meta": "Always present. Canonical location for request, template, quality, retry, and pipeline metadata.",
                "extracted": "Always present in the response shape. Null when no structured extraction was requested or produced.",
                "chunks": "Always present in the response shape. Null when chunking was not requested.",
                "html": "Always present in the response shape. Null unless output_format='html'.",
                "enriched_pdf": "Always present in the response shape. Null unless ocr_embed=true generated a searchable PDF.",
                "normalized": "Always present in the response shape. Null when no normalization mapping applied.",
                "normalized_version": "Always present in the response shape. Null when normalized is null.",
                "normalized_warnings": "Always present in the response shape. Null when normalization did not run; otherwise list of warnings or empty list.",
                "normalized_trace": "Always present in the response shape. Null when normalization did not run.",
                "normalized_context": "Always present in the response shape. Null when normalization did not run.",
                "normalized_confidence": "Always present in the response shape. Null when normalization did not run.",
            },
            "null_semantics": {
                "null": "Field belongs to the contract but currently has no value or the corresponding pipeline branch did not run.",
                "missing": "Should only happen for unknown extra fields or legacy payloads, not for documented contract fields.",
            },
            "job_progress_endpoints": {
                "start": "POST /v1/convert/async returns {job_id, status}.",
                "status": "GET /v1/jobs/{id} returns canonical progress under progress.",
                "list": "GET /v1/jobs returns the same canonical progress shape per entry.",
                "result": "GET /v1/jobs/{id}/result returns the final ConvertResponse after completion.",
            },
            "job_progress_fields": {
                "progress.status": "Job status snapshot, e.g. queued, processing, failed.",
                "progress.current_stage": "Current pipeline stage such as start, markitdown, describe_page, classify, extract, chunk, render, done, failed.",
                "progress.message": "Human-readable detail for the current stage.",
                "progress.percent": "Best-effort progress percentage when known.",
                "progress.request_id": "Stable request correlation id shared with the final response meta.",
                "progress.job_id": "Job id for async processing.",
                "progress.attempt_number": "Current internal attempt number.",
                "progress.attempt_count": "Known number of internal attempts for the current response path.",
                "progress.attempt_mode": "Mode of the current attempt, e.g. default or full.",
                "progress.page_current": "Current page for long-document progress when known.",
                "progress.page_total": "Total page count for the current long-document progress window when known.",
                "progress.upstream_attempt": "Current retry count of an upstream API call when known.",
                "progress.metadata": "Additional context such as filename or source.",
            },
        },
        "canonical_meta_fields": {
            "meta.document_type": "Canonical document classification output.",
            "meta.document_type_confidence": "Confidence for meta.document_type.",
            "meta.template_used": "Canonical template identifier used or selected for extraction.",
            "meta.template_version": "Version of the template recorded in meta.template_used.",
            "meta.quality_score": "Canonical document/conversion quality score.",
            "meta.quality_grade": "Grade derived from meta.quality_score.",
            "meta.retry_applied": "True when low-quality escalation was executed.",
            "meta.retry_reason": "Reason for retry decision: low_quality or missing_quality_score.",
        },
        "brix_integration_contract": {
            "read_from_raw_meta": [
                "meta.document_type",
                "meta.document_type_confidence",
                "meta.template_used",
                "meta.template_version",
                "meta.quality_score",
                "meta.quality_grade",
                "meta.retry_applied",
                "meta.retry_reason",
                "meta.initial_mode",
                "meta.final_mode",
                "meta.initial_quality_score",
                "meta.final_quality_score",
                "meta.retry_threshold_used",
                "meta.request_id",
                "meta.attempt_number",
                "meta.attempt_count",
                "meta.attempt_mode",
                "meta.pipeline_steps",
            ],
            "read_business_data_from": {
                "extracted": "Template-specific extracted payload.",
                "normalized": "Normalized downstream payload when a mapping exists.",
            },
            "do_not_use_as_canonical": [
                "logs",
                "raw._meta.template",
                "top-level quality_score",
                "normalized._quality_score as document quality",
                "normalized.quality_score as document quality",
            ],
            "notes": [
                "Use raw.meta as the only canonical source for template, quality, retry, and attempt metadata.",
                "Use raw.extracted for template-specific fields and raw.normalized for normalized downstream fields.",
                "Treat normalized._quality_score and normalized.quality_score as normalizer-specific scores only.",
            ],
        },
        "v3_meta_fields": {
            "zugferd": "ZUGFeRD/Factur-X structured invoice data (auto-extracted from PDF when present)",
            "xmp_metadata": "PDF XMP metadata (title, creator, keywords, custom properties)",
            "embedded_files": "Files embedded inside the PDF (e.g. original XML in ZUGFeRD)",
            "hidden_sheets": "XLSX sheets with visibility=hidden or xlSheetVeryHidden — extracted regardless",
            "exif": "Image EXIF data (camera, GPS coordinates, exposure, focal length)",
            "iptc": "Image IPTC metadata (creator, copyright, keywords, caption)",
            "document_properties": "Office document core/app/custom properties (author, company, revision, etc.)",
            "email_routing": "Email routing headers (Received chain, SPF, DKIM, DMARC results)",
            "email_thread": "Email thread/conversation metadata (In-Reply-To, References)",
            "calendar_events": "Calendar events extracted from .ics attachments",
            "hidden_slides": "PPTX hidden slides — extracted with hidden=true marker in meta",
        },
        "normalization": {
            "description": "Automatische Normalisierung extrahierter Daten in einheitliche Feldnamen",
            "how_it_works": "Bei convert/extract: Wenn ein Mapping für den Template-Typ existiert, werden extrahierte Felder automatisch normalisiert",
            "parameters": {
                "compact": "true/false — remove null fields from the normalized output to reduce payload size",
            },
            "response_fields": {
                "normalized": "Normalisierte Daten mit einheitlichen Feldnamen, Typen und normalizer-spezifischen Scores",
            },
            "admin_endpoints": [
                "GET/POST /v1/normalized/fields — Felder verwalten",
                "GET/POST /v1/normalized/values/{field} — Erlaubte Werte verwalten",
                "GET/POST /v1/normalized/categories — Kategorien verwalten",
                "GET/PUT /v1/normalized/mappings/{template} — Template-Mappings",
                "GET /v1/normalized/schema — JSON Schema",
                "GET /v1/normalized/coverage — Coverage Report",
                "POST /v1/normalized/batch-validate — Batch-Validierung",
                "POST/GET /v1/normalized/corrections — Korrektur-Feedback",
            ],
            "response_structure": {
                "normalized": "Flat dict with field values. Contains normalizer-specific _quality_score and quality_score. Null fields included unless compact=true.",
                "normalized_confidence": "Flat dict parallel to normalized. Keys = field names, Values = confidence float (0.0-1.0).",
                "normalized_version": "String — version hash of the normalization rules.",
                "normalized_warnings": "Array of strings — warnings (missing fields, validation errors).",
                "normalized_trace": "Array of objects — step-by-step audit trail. Each entry: {field, source_field, raw, rule, confidence}.",
                "normalized_context": "Dict with context: vendor_country, recipient_country, tax_country, quality_score, validation_errors.",
            },
            "score_semantics": {
                "meta.quality_score": "Primary document/conversion quality score used for retry decisions and contract-level quality reporting.",
                "normalized._quality_score": "Normalizer-internal quality score for the normalized payload only.",
                "normalized.quality_score": "Alias of normalized._quality_score inside normalized output.",
            },
        },
        "note_mcp_vs_rest": "MCP tool 'convert' uses 'base64_data' parameter (not 'base64' like REST API)",
    }
