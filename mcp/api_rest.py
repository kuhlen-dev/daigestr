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
    ConvertRequest,
    ConvertResponse,
    ConvertFolderRequest,
    AnalyzeRequest,
    ExtractRequest,
    TemplateResponse,
    HealthResponse,
    ErrorCode,
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
    MAX_FILE_SIZE_MB,
    IMAGE_MAX_WIDTH,
    MAX_RETRIES,
    MCP_PORT,
    REST_PORT,
    LOG_LEVEL,
    START_TIME,
    TEMP_DIR,
    MISTRAL_TIMEOUT,
    WEBHOOK_TIMEOUT_SECONDS,
)
from utils import _get, resolve_path, get_file_extension, get_mimetype, detect_mimetype_from_bytes
from intelligence import (
    classify_document,
    correct_ocr_text,
    extract_structured_data,
    _apply_auto_extract,
    chunk_markdown,
    get_db_connection,
    get_template_by_id,
)
from templates_db import (
    get_all_template_ids, search_templates, cache_clear,
    job_create, job_update, job_set_result, job_get, job_delete, job_list,
)
from routing import (
    convert_auto,
    convert_url,
    convert_folder_contents,
    _build_tips_dict,
)

log = structlog.get_logger()

# FastAPI Instanz
app = FastAPI(
    title="Daigestr API",
    description="Konvertiert Dokumente und Bilder zu Markdown",
    version=VERSION,
    docs_url="/docs",
    redoc_url="/redoc",
)


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
        file_path = _resolve_path(request.path)
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
            mode=request.mode,
            output_format=request.output_format,
            pages=request.pages,
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
            mode=request.mode,
            output_format=request.output_format,
            pages=request.pages,
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
                markdown_text = result["markdown"]
                # OCR-Korrektur für HTML (analog zum path/base64-Pfad)
                effective_ocr_correct = request.ocr_correct or (request.accuracy == "high")
                if effective_ocr_correct:
                    ocr_result = await _correct_ocr(markdown_text, request.language)
                    if ocr_result.get("success"):
                        markdown_text = ocr_result["corrected_text"]
                        meta["ocr_corrected"] = True
                        meta["ocr_corrections_count"] = ocr_result.get("corrections_count", 0)
                if request.classify:
                    classify_result = await _classify_doc(
                        markdown_text, request.classify_categories, request.language
                    )
                    meta.update(classify_result)
                response = create_success_response(markdown_text, meta=meta)
                # T-MKIT-036: Auto-Extract
                if request.auto_extract and not effective_schema:
                    response = await _apply_auto(response, meta, markdown_text, request.language, request.min_confidence, [])
                elif effective_schema:
                    extraction = await _extract_struct(markdown_text, effective_schema, request.language)
                    if extraction["success"]:
                        response.extracted = extraction["extracted"]
                    else:
                        log.warning("extract_structured_data_failed_url_html", error=extraction.get("error"))
                if request.chunk:
                    response.chunks = _chunk_md(markdown_text, chunk_size=request.chunk_size, source=request.url)
                return response
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
                    auto_extract=request.auto_extract,
                    min_confidence=request.min_confidence,
                    mode=request.mode,
                    output_format=request.output_format,
                    pages=request.pages,
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
    folder_path = resolve_path(request.path)
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
        language=request.language,
        meta=request.meta,
        extract_schema=effective_schema,
        accuracy=request.accuracy,
        ocr_correct=request.ocr_correct,
        describe_images=request.describe_images,
        classify=request.classify,
        auto_extract=request.auto_extract,
        min_confidence=request.min_confidence,
    )
    _api_convert_fn = _get("api_convert", api_convert)
    return await _api_convert_fn(convert_req)


@app.get("/v1/templates/categories")
async def api_template_categories() -> dict:
    """Gibt alle Template-Kategorien mit Anzahl zurück (T-MKIT-035)."""
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT category, COUNT(*) as count FROM template WHERE enabled = 1 GROUP BY category ORDER BY category"
    ).fetchall()
    conn.close()
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
    rows = conn.execute(
        "SELECT id, category, display_name, enabled FROM template ORDER BY category, display_name"
    ).fetchall()
    conn.close()
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
    for tmpl in templates:
        if "id" not in tmpl or "schema" not in tmpl:
            continue
        existing = conn.execute("SELECT id FROM template WHERE id = ?", (tmpl["id"],)).fetchone()
        if existing and mode == "upsert":
            conn.execute(
                """UPDATE template SET category=?, display_name=?, description=?, schema=?,
                   field_descriptions=?, classify_keywords=?, typical_senders=?, steuer_relevanz=?,
                   priority=?, enabled=?, version=?, source=?, notes=?, updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (tmpl.get("category", "other"), tmpl.get("display_name", tmpl["id"]),
                 tmpl.get("description"), json.dumps(tmpl["schema"]),
                 json.dumps(tmpl.get("field_descriptions")) if tmpl.get("field_descriptions") else None,
                 tmpl.get("classify_keywords"), tmpl.get("typical_senders"),
                 tmpl.get("steuer_relevanz"), tmpl.get("priority", 0),
                 tmpl.get("enabled", True), tmpl.get("version", 1),
                 tmpl.get("source", "manual"), tmpl.get("notes"), tmpl["id"])
            )
            updated += 1
        elif not existing:
            conn.execute(
                """INSERT INTO template (id, category, display_name, description, schema, field_descriptions,
                   classify_keywords, typical_senders, steuer_relevanz, priority, enabled, version, source, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (tmpl["id"], tmpl.get("category", "other"), tmpl.get("display_name", tmpl["id"]),
                 tmpl.get("description"), json.dumps(tmpl["schema"]),
                 json.dumps(tmpl.get("field_descriptions")) if tmpl.get("field_descriptions") else None,
                 tmpl.get("classify_keywords"), tmpl.get("typical_senders"),
                 tmpl.get("steuer_relevanz"), tmpl.get("priority", 0),
                 tmpl.get("enabled", True), tmpl.get("version", 1),
                 tmpl.get("source", "manual"), tmpl.get("notes"))
            )
            created += 1
    conn.commit()
    conn.close()
    return {"success": True, "created": created, "updated": updated, "total": len(templates)}


@app.post("/v1/templates")
async def api_create_template(request: dict) -> dict:
    """Erstellt ein neues Template (T-MKIT-035)."""
    import sqlite3 as _sqlite3  # noqa: F401
    if "id" not in request or "schema" not in request:
        raise HTTPException(status_code=400, detail="'id' and 'schema' are required")
    conn = get_db_connection()
    try:
        conn.execute(
            """INSERT INTO template (id, category, display_name, description, schema, field_descriptions,
               classify_keywords, typical_senders, steuer_relevanz, priority, enabled, version, source, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (request["id"], request.get("category", "other"), request.get("display_name", request["id"]),
             request.get("description"), json.dumps(request["schema"]),
             json.dumps(request.get("field_descriptions")) if request.get("field_descriptions") else None,
             request.get("classify_keywords"), request.get("typical_senders"),
             request.get("steuer_relevanz"), request.get("priority", 0),
             request.get("enabled", True), request.get("version", 1),
             request.get("source", "manual"), request.get("notes"))
        )
        conn.commit()
    except Exception as e:
        conn.close()
        if "UNIQUE constraint failed" in str(e) or "IntegrityError" in type(e).__name__:
            raise HTTPException(status_code=409, detail=f"Template '{request['id']}' already exists")
        raise
    conn.close()
    return {"success": True, "id": request["id"]}


@app.put("/v1/templates/{template_id}")
async def api_update_template(template_id: str, request: dict) -> dict:
    """Aktualisiert ein Template (partial update, T-MKIT-035)."""
    conn = get_db_connection()
    existing = conn.execute("SELECT id FROM template WHERE id = ?", (template_id,)).fetchone()
    if not existing:
        conn.close()
        raise HTTPException(status_code=404, detail=f"Template '{template_id}' not found")
    # Fixed UPDATE with COALESCE — no dynamic SQL construction
    row = conn.execute("SELECT * FROM template WHERE id = ?", (template_id,)).fetchone()
    current = dict(row)
    conn.execute(
        """UPDATE template SET
            category = ?, display_name = ?, description = ?,
            schema = ?, field_descriptions = ?,
            classify_keywords = ?, typical_senders = ?,
            steuer_relevanz = ?, priority = ?, enabled = ?,
            version = ?, source = ?, notes = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?""",
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
            request.get("enabled", current["enabled"]),
            request.get("version", current["version"]),
            request.get("source", current["source"]),
            request.get("notes", current["notes"]),
            template_id,
        )
    )
    conn.commit()
    conn.close()
    return {"success": True, "id": template_id}


@app.delete("/v1/templates/{template_id}")
async def api_delete_template(template_id: str) -> dict:
    """Löscht ein Template (T-MKIT-035)."""
    conn = get_db_connection()
    cursor = conn.execute("DELETE FROM template WHERE id = ?", (template_id,))
    conn.commit()
    conn.close()
    if cursor.rowcount == 0:
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
        file_path = resolve_path(request.path)
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

    return HealthResponse(
        status="ok",
        version=VERSION,
        meta={
            "mistral_api_configured": mistral_configured,
            "vision_model": MISTRAL_VISION_MODEL,
            "mistral_ocr_configured": mistral_configured and MISTRAL_OCR_ENABLED,
            "ocr_model": MISTRAL_OCR_MODEL if MISTRAL_OCR_ENABLED else None,
            "max_file_size_mb": MAX_FILE_SIZE_MB,
            "image_max_width": IMAGE_MAX_WIDTH,
            "max_retries": MAX_RETRIES,
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

    _job_update(job_id, "processing", json.dumps({"message": "Conversion started"}))
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
            file_path = _resolve_path(request.path)
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

        result: ConvertResponse = await _convert_auto(
            file_data=file_data,
            filename=filename,
            source=source,
            source_type=source_type,
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
            mode=request.mode,
            output_format=request.output_format,
            pages=request.pages,
        )
        _job_set_result(job_id, result.model_dump_json())

        # Webhook senden wenn konfiguriert
        if request.webhook_url:
            await _fire_webhook(request.webhook_url, result)

    except Exception as exc:
        log.error("async_job_failed", job_id=job_id, error=str(exc))
        error_result = create_error_response(
            ErrorCode.INTERNAL_ERROR,
            f"Job fehlgeschlagen: {str(exc)}",
        )
        _job_update(job_id, "failed", json.dumps({"error": str(exc)}))
        # Webhook auch bei Fehler senden
        if request.webhook_url:
            await _fire_webhook(request.webhook_url, error_result)


@app.post("/v1/convert/async")
async def api_convert_async(request: ConvertRequest) -> dict:
    """
    Startet eine asynchrone Konvertierung.

    Gibt sofort eine Job-ID zurück. Der Fortschritt kann über GET /v1/jobs/{id}
    abgefragt werden. Das Ergebnis ist über GET /v1/jobs/{id}/result abrufbar.
    """
    _job_create = _get("job_create", job_create)
    job_id = str(uuid.uuid4())
    _job_create(job_id)
    asyncio.create_task(_run_async_job(job_id, request))
    return {"job_id": job_id, "status": "queued"}


@app.get("/v1/jobs")
async def api_list_jobs() -> dict:
    """Gibt alle Jobs zurück (neueste zuerst)."""
    _job_list = _get("job_list", job_list)
    jobs = _job_list()
    result = []
    for j in jobs:
        entry = {
            "job_id": j["id"],
            "status": j["status"],
            "created_at": j["created_at"],
            "updated_at": j["updated_at"],
        }
        if j.get("progress_json"):
            try:
                entry["progress"] = json.loads(j["progress_json"])
            except Exception:
                entry["progress"] = None
        result.append(entry)
    return {"jobs": result}


@app.get("/v1/jobs/{job_id}")
async def api_get_job(job_id: str) -> dict:
    """Gibt den Status und Fortschritt eines Jobs zurück."""
    _job_get = _get("job_get", job_get)
    job = _job_get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    result = {
        "job_id": job["id"],
        "status": job["status"],
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
    }
    if job.get("progress_json"):
        try:
            result["progress"] = json.loads(job["progress_json"])
        except Exception:
            result["progress"] = None
    return result


@app.get("/v1/jobs/{job_id}/result", response_model=ConvertResponse)
async def api_get_job_result(job_id: str) -> ConvertResponse:
    """Gibt das volle ConvertResponse Ergebnis zurück (nur wenn status=completed)."""
    _job_get = _get("job_get", job_get)
    job = _job_get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    if job["status"] != "completed":
        raise HTTPException(
            status_code=202,
            detail=f"Job '{job_id}' is not completed yet (status: {job['status']})"
        )
    if not job.get("result_json"):
        raise HTTPException(status_code=500, detail=f"Job '{job_id}' has no result data")
    return ConvertResponse.model_validate_json(job["result_json"])


@app.delete("/v1/jobs/{job_id}")
async def api_delete_job(job_id: str) -> dict:
    """Löscht einen Job."""
    _job_delete = _get("job_delete", job_delete)
    deleted = _job_delete(job_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return {"success": True, "job_id": job_id}


@app.get("/v1/tips")
async def api_tips() -> dict:
    """Usage tips and common patterns for Daigestr."""
    return _build_tips_dict()


def run_rest_server():
    """Startet den REST-Server in einem separaten Thread."""
    import os
    uvicorn.run(
        app,
        host=os.getenv("BIND_HOST", "0.0.0.0"),
        port=REST_PORT,
        log_level=LOG_LEVEL.lower(),
    )
