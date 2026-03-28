"""
Daigestr — Mistral API Client

Enthält alle Funktionen für Mistral API-Calls:
- call_mistral_vision_api (async, mit Retry)
- call_mistral_ocr_api (async, mit Retry)
- analyze_with_mistral_vision (async, High-Level Wrapper)
"""

import asyncio
import base64
from typing import Any

import httpx
import structlog
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

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
)
from utils import _get, _LOADED_BY_SERVER  # noqa: F401

log = structlog.get_logger()


async def _handle_rate_limit(response: "httpx.Response") -> None:
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
        log.warning("rate_limited", retry_after=wait_seconds)
        await asyncio.sleep(wait_seconds)
    else:
        log.warning("rate_limited", retry_after=None)
    response.raise_for_status()


@retry(
    stop=stop_after_attempt(MAX_RETRIES),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type((httpx.TimeoutException, httpx.HTTPStatusError)),
    reraise=True,
)
async def call_mistral_vision_api(payload: dict) -> dict:
    """Ruft die Mistral Vision API mit Retry-Logik auf."""
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
            await _handle_rate_limit(response)
        response.raise_for_status()
        return response.json()


@retry(
    stop=stop_after_attempt(MAX_RETRIES),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type((httpx.TimeoutException, httpx.HTTPStatusError)),
    reraise=True,
)
async def call_mistral_ocr_api(file_data: bytes, filename: str) -> dict:
    """Ruft die Mistral OCR API (/v1/ocr) auf."""
    b64 = base64.b64encode(file_data).decode("utf-8")
    payload = {
        "model": MISTRAL_OCR_MODEL,
        "document": {
            "type": "document_url",
            "document_url": f"data:application/pdf;base64,{b64}",
        },
    }
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
            await _handle_rate_limit(response)
        response.raise_for_status()
        return response.json()


async def analyze_with_mistral_vision(
    image_data: bytes,
    mimetype: str,
    prompt: str,
    language: str = "de"
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
        log.info("vision_api_call", model=vision_model, image_size=len(image_data))
        result = await _call_fn(payload)

        content = result["choices"][0]["message"]["content"]
        content = strip_llm_artifacts(content)
        usage = result.get("usage", {})

        log.info("vision_api_success", tokens=usage.get("total_tokens", 0))
        return {
            "success": True,
            "markdown": content,
            "tokens_prompt": usage.get("prompt_tokens", 0),
            "tokens_completion": usage.get("completion_tokens", 0),
            "tokens_total": usage.get("total_tokens", 0),
            "vision_model": vision_model,
        }

    except httpx.TimeoutException:
        log.error("vision_api_timeout", timeout=MISTRAL_TIMEOUT)
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
            log.error("vision_api_rate_limit_exhausted", retries=MAX_RETRIES)
            return {
                "success": False,
                "error_code": ErrorCode.API_ERROR,
                "error": f"Mistral Vision API unavailable after {MAX_RETRIES} retries (rate limited)"
            }
        log.error("vision_api_error", error=error_detail)
        return {
            "success": False,
            "error_code": ErrorCode.API_ERROR,
            "error": f"Mistral API Fehler: {error_detail}"
        }
    except Exception as e:
        log.error("vision_api_exception", error=str(e))
        return {
            "success": False,
            "error_code": ErrorCode.VISION_FAILED,
            "error": f"Vision-Analyse fehlgeschlagen: {str(e)}"
        }
