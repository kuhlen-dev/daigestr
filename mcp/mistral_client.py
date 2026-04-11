"""
Daigestr — Mistral API Client

Enthält alle Funktionen für Mistral API-Calls:
- call_mistral_vision_api (async, mit Retry)
- call_mistral_ocr_api (async, mit Retry)
- analyze_with_mistral_vision (async, High-Level Wrapper)
"""

import asyncio
import base64
import time
from typing import Any, Optional

import httpx
import structlog
from tenacity import RetryCallState, retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from models import ErrorCode
from settings import (
    MISTRAL_API_KEY,
    MISTRAL_API_URL,
    MISTRAL_VISION_MODEL,
    MISTRAL_OCR_MODEL,
    MISTRAL_TIMEOUT,
    VISION_MAX_TOKENS,
    MAX_RETRIES,
    RATE_LIMIT_MAX_WAIT_SECONDS,
    AUDIT_ENABLED,
)
from utils import _get, _LOADED_BY_SERVER  # noqa: F401

log = structlog.get_logger()


def _retry_context(retry_state: RetryCallState) -> dict[str, Any]:
    kwargs = retry_state.kwargs or {}
    return {
        "request_id": kwargs.get("request_id"),
        "attempt_number": kwargs.get("attempt_number"),
        "pipeline_step": kwargs.get("pipeline_step"),
        "page": kwargs.get("page"),
        "filename": kwargs.get("filename"),
        "upstream_attempt": retry_state.attempt_number,
        "call": retry_state.fn.__name__,
    }


def _log_mistral_attempt(retry_state: RetryCallState) -> None:
    if retry_state.kwargs is not None:
        retry_state.kwargs["upstream_attempt"] = retry_state.attempt_number
    log.info("mistral_api_attempt", **_retry_context(retry_state))


def _log_mistral_before_sleep(retry_state: RetryCallState) -> None:
    ctx = _retry_context(retry_state)
    outcome = retry_state.outcome
    if outcome is not None and outcome.failed:
        exc = outcome.exception()
        ctx["error"] = str(exc) if exc is not None else None
        if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
            ctx["status_code"] = exc.response.status_code
    log.warning("mistral_api_retry_scheduled", **ctx)


def _audit(request_id: str, event_type: str, **kwargs) -> None:
    """Fire-and-forget Audit-Log-Helper. Patchable via _get für Tests."""
    if not AUDIT_ENABLED:
        return
    try:
        _fn = _get("audit_log_event", None)
        if _fn is None:
            from audit_db import audit_log_event as _audit_log_event  # noqa: PLC0415
            _fn = _audit_log_event
        _fn(request_id=request_id, event_type=event_type, **kwargs)
    except Exception:
        pass  # fire-and-forget


async def _handle_rate_limit(
    response: "httpx.Response",
    *,
    request_id: Optional[str] = None,
    attempt_number: Optional[int] = None,
    pipeline_step: Optional[str] = None,
    page: Optional[int] = None,
    filename: Optional[str] = None,
    upstream_attempt: Optional[int] = None,
) -> None:
    """
    Wertet den Retry-After Header bei HTTP 429 aus und wartet entsprechend.
    Wenn kein Retry-After vorhanden, wird tenacity's exponentielles Backoff genutzt.
    Wirft immer HTTPStatusError damit tenacity den Retry auslöst.
    """
    retry_after_raw = response.headers.get("retry-after") or response.headers.get("Retry-After")
    if retry_after_raw is not None:
        try:
            wait_seconds = int(retry_after_raw)
        except ValueError:
            wait_seconds = 5
        wait_seconds = min(wait_seconds, RATE_LIMIT_MAX_WAIT_SECONDS)
        log.warning(
            "rate_limited",
            retry_after=wait_seconds,
            request_id=request_id,
            attempt_number=attempt_number,
            pipeline_step=pipeline_step,
            page=page,
            filename=filename,
            upstream_attempt=upstream_attempt,
        )
        _audit(
            request_id=request_id or "",
            event_type="warning",
            step="rate_limit",
            detail=f"retry_after={wait_seconds}",
            level="warning",
            metadata={
                "status_code": 429,
                "retry_after": wait_seconds,
                "attempt_number": attempt_number,
                "pipeline_step": pipeline_step,
                "page": page,
                "filename": filename,
                "upstream_attempt": upstream_attempt,
            },
        )
        await asyncio.sleep(wait_seconds)
    else:
        log.warning(
            "rate_limited",
            retry_after=None,
            request_id=request_id,
            attempt_number=attempt_number,
            pipeline_step=pipeline_step,
            page=page,
            filename=filename,
            upstream_attempt=upstream_attempt,
        )
        _audit(
            request_id=request_id or "",
            event_type="warning",
            step="rate_limit",
            detail="retry_after=None",
            level="warning",
            metadata={
                "status_code": 429,
                "retry_after": None,
                "attempt_number": attempt_number,
                "pipeline_step": pipeline_step,
                "page": page,
                "filename": filename,
                "upstream_attempt": upstream_attempt,
            },
        )
    response.raise_for_status()


@retry(
    stop=stop_after_attempt(MAX_RETRIES),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type((httpx.TimeoutException, httpx.HTTPStatusError)),
    before=_log_mistral_attempt,
    before_sleep=_log_mistral_before_sleep,
    reraise=True,
)
async def call_mistral_vision_api(
    payload: dict,
    *,
    request_id: Optional[str] = None,
    attempt_number: Optional[int] = None,
    pipeline_step: Optional[str] = None,
    page: Optional[int] = None,
    filename: Optional[str] = None,
    upstream_attempt: Optional[int] = None,
) -> dict:
    """Ruft die Mistral Vision API mit Retry-Logik auf."""
    t0 = time.monotonic()
    async with httpx.AsyncClient(timeout=float(MISTRAL_TIMEOUT)) as client:
        response = await client.post(
            f"{MISTRAL_API_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {MISTRAL_API_KEY}",
                "Content-Type": "application/json"
            },
            json=payload
        )
        if response.status_code == 429:
            await _handle_rate_limit(
                response,
                request_id=request_id,
                attempt_number=attempt_number,
                pipeline_step=pipeline_step,
                page=page,
                filename=filename,
                upstream_attempt=upstream_attempt,
            )
        response.raise_for_status()
        data = response.json()
        duration_ms = int((time.monotonic() - t0) * 1000)
        usage = data.get("usage", {})
        upstream_attempts_used = upstream_attempt or 1
        log.info(
            "mistral_api_completed",
            request_id=request_id,
            attempt_number=attempt_number,
            pipeline_step=pipeline_step,
            page=page,
            filename=filename,
            upstream_attempts_used=upstream_attempts_used,
            call="call_mistral_vision_api",
        )
        _audit(
            request_id=request_id or "",
            event_type="mistral_call",
            detail="vision",
            duration_ms=duration_ms,
            metadata={
                "model": MISTRAL_VISION_MODEL,
                "tokens_prompt": usage.get("prompt_tokens"),
                "tokens_completion": usage.get("completion_tokens"),
                "status_code": 200,
                "attempt_number": attempt_number,
                "pipeline_step": pipeline_step,
                "page": page,
                "filename": filename,
                "upstream_attempts_used": upstream_attempts_used,
            },
        )
        return data


@retry(
    stop=stop_after_attempt(MAX_RETRIES),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type((httpx.TimeoutException, httpx.HTTPStatusError)),
    before=_log_mistral_attempt,
    before_sleep=_log_mistral_before_sleep,
    reraise=True,
)
async def call_mistral_ocr_api(
    file_data: bytes,
    filename: str,
    *,
    request_id: Optional[str] = None,
    attempt_number: Optional[int] = None,
    pipeline_step: Optional[str] = None,
    upstream_attempt: Optional[int] = None,
) -> dict:
    """Ruft die Mistral OCR API (/v1/ocr) auf."""
    b64 = base64.b64encode(file_data).decode("utf-8")
    payload = {
        "model": MISTRAL_OCR_MODEL,
        "document": {
            "type": "document_url",
            "document_url": f"data:application/pdf;base64,{b64}",
        },
    }
    t0 = time.monotonic()
    async with httpx.AsyncClient(timeout=float(MISTRAL_TIMEOUT)) as client:
        response = await client.post(
            f"{MISTRAL_API_URL}/ocr",
            headers={
                "Authorization": f"Bearer {MISTRAL_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        if response.status_code == 429:
            await _handle_rate_limit(
                response,
                request_id=request_id,
                attempt_number=attempt_number,
                pipeline_step=pipeline_step,
                filename=filename,
                upstream_attempt=upstream_attempt,
            )
        response.raise_for_status()
        data = response.json()
        duration_ms = int((time.monotonic() - t0) * 1000)
        usage = data.get("usage", {})
        upstream_attempts_used = upstream_attempt or 1
        log.info(
            "mistral_api_completed",
            request_id=request_id,
            attempt_number=attempt_number,
            pipeline_step=pipeline_step,
            filename=filename,
            upstream_attempts_used=upstream_attempts_used,
            call="call_mistral_ocr_api",
        )
        _audit(
            request_id=request_id or "",
            event_type="mistral_call",
            detail="ocr",
            duration_ms=duration_ms,
            metadata={
                "model": MISTRAL_OCR_MODEL,
                "tokens_prompt": usage.get("prompt_tokens"),
                "tokens_completion": usage.get("completion_tokens"),
                "status_code": 200,
                "attempt_number": attempt_number,
                "pipeline_step": pipeline_step,
                "filename": filename,
                "upstream_attempts_used": upstream_attempts_used,
            },
        )
        return data


async def analyze_with_mistral_vision(
    image_data: bytes,
    mimetype: str,
    prompt: str,
    language: str = "de",
    request_id: Optional[str] = None,
    attempt_number: Optional[int] = None,
    pipeline_step: Optional[str] = None,
    page: Optional[int] = None,
    filename: Optional[str] = None,
) -> dict[str, Any]:
    """Analysiert ein Bild mit Mistral Pixtral Vision API."""
    from utils import strip_llm_artifacts  # noqa: PLC0415 — avoid circular at module level

    api_key = _get("MISTRAL_API_KEY", MISTRAL_API_KEY)
    vision_model = _get("MISTRAL_VISION_MODEL", MISTRAL_VISION_MODEL)
    max_tokens = _get("VISION_MAX_TOKENS", VISION_MAX_TOKENS)
    _call_fn = _get("call_mistral_vision_api", call_mistral_vision_api)

    if not api_key:
        return {
            "success": False,
            "error_code": ErrorCode.API_KEY_INVALID,
            "error": "MISTRAL_API_KEY nicht konfiguriert"
        }

    b64_image = base64.b64encode(image_data).decode("utf-8")
    data_url = f"data:{mimetype};base64,{b64_image}"

    try:
        from templates_db import get_prompt as _get_prompt  # noqa: PLC0415
        system_prompt = _get("get_prompt", _get_prompt)("vision", "system", language=language)
    except Exception:
        system_prompt = (
            "Du bist ein präziser Assistent für Bild- und Dokumentenanalyse. "
            "Befolge ausschließlich die Anweisungen im User-Prompt. "
            "Antworte NICHT mit Einleitungssätzen, Erklärungen oder Code-Block-Wrapping — nur mit dem angeforderten Ergebnis."
            if language == "de"
            else "You are a precise assistant for image and document analysis. "
            "Follow only the instructions in the user prompt. "
            "Do NOT reply with introductions, explanations, or code-block wrapping — only the requested result."
        )

    payload = {
        "model": vision_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}}
                ]
            }
        ],
        "max_tokens": max_tokens
    }

    try:
        log.info(
            "vision_api_call",
            model=vision_model,
            image_size=len(image_data),
            request_id=request_id,
            attempt_number=attempt_number,
            pipeline_step=pipeline_step,
            page=page,
            filename=filename,
        )
        result = await _call_fn(
            payload,
            request_id=request_id,
            attempt_number=attempt_number,
            pipeline_step=pipeline_step,
            page=page,
            filename=filename,
        )

        content = result["choices"][0]["message"]["content"]
        content = strip_llm_artifacts(content)
        usage = result.get("usage", {})

        log.info(
            "vision_api_success",
            tokens=usage.get("total_tokens", 0),
            request_id=request_id,
            attempt_number=attempt_number,
            pipeline_step=pipeline_step,
            page=page,
            filename=filename,
        )
        return {
            "success": True,
            "markdown": content,
            "tokens_prompt": usage.get("prompt_tokens", 0),
            "tokens_completion": usage.get("completion_tokens", 0),
            "tokens_total": usage.get("total_tokens", 0),
            "vision_model": vision_model,
        }

    except httpx.TimeoutException as e:
        log.error("vision_api_timeout", timeout=MISTRAL_TIMEOUT)
        _audit(
            request_id=request_id or "",
            event_type="warning",
            step="vision_timeout",
            level="warning",
            error=str(e),
            metadata={"model": vision_model, "timeout": MISTRAL_TIMEOUT},
        )
        return {
            "success": False,
            "error_code": ErrorCode.TIMEOUT,
            "error": f"Mistral API Timeout nach {MISTRAL_TIMEOUT}s"
        }
    except httpx.HTTPStatusError as e:
        error_detail = str(e)
        status_code = getattr(getattr(e, "response", None), "status_code", None)
        try:
            error_detail = e.response.json().get("error", {}).get("message", str(e))
        except Exception:
            pass
        if status_code == 429:
            log.error(
                "vision_api_rate_limit_exhausted",
                retries=MAX_RETRIES,
                request_id=request_id,
                attempt_number=attempt_number,
                pipeline_step=pipeline_step,
                page=page,
                filename=filename,
            )
            _audit(
                request_id=request_id or "",
                event_type="warning",
                step="vision_rate_limit_exhausted",
                level="warning",
                error=error_detail,
                metadata={
                    "model": vision_model,
                    "retries": MAX_RETRIES,
                    "status_code": 429,
                    "attempt_number": attempt_number,
                    "pipeline_step": pipeline_step,
                    "page": page,
                    "filename": filename,
                },
            )
            return {
                "success": False,
                "error_code": ErrorCode.API_ERROR,
                "error": f"Mistral Vision API unavailable after {MAX_RETRIES} retries (rate limited)"
            }
        log.error("vision_api_error", error=error_detail, request_id=request_id, attempt_number=attempt_number, pipeline_step=pipeline_step, page=page, filename=filename)
        _audit(
            request_id=request_id or "",
            event_type="warning",
            step="vision_api_error",
            level="warning",
            error=error_detail,
            metadata={"model": vision_model, "status_code": status_code},
        )
        return {
            "success": False,
            "error_code": ErrorCode.API_ERROR,
            "error": f"Mistral API Fehler: {error_detail}"
        }
    except Exception as e:
        log.error("vision_api_exception", error=str(e), request_id=request_id, attempt_number=attempt_number, pipeline_step=pipeline_step, page=page, filename=filename)
        _audit(
            request_id=request_id or "",
            event_type="warning",
            step="vision_exception",
            level="warning",
            error=str(e),
            metadata={"model": vision_model},
        )
        return {
            "success": False,
            "error_code": ErrorCode.VISION_FAILED,
            "error": f"Vision-Analyse fehlgeschlagen: {str(e)}"
        }
