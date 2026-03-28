#!/usr/bin/env python3
# Aktiviere uvloop für bessere Performance
try:
    import uvloop
    uvloop.install()
except ImportError:
    pass

"""
Daigestr — Document Intelligence Service v3.1.0

Bietet zwei Schnittstellen:
- MCP (Port 8080): Für Claude und andere MCP-Clients
- REST (Port 8081): Für n8n und andere HTTP-Clients

Features:
- Auto-Routing: Bilder → Vision, Dokumente → MarkItDown
- Bild-Resize vor Vision (spart Tokens)
- Folder-Konvertierung (alle Dateien in einem Ordner)
- URL-Konvertierung
- Retry-Logik für API-Calls
- Strukturiertes Logging
"""

import asyncio
import os
import io
import json
import base64
import hashlib
import mimetypes
import re
import sqlite3
import threading
import time
import logging
from datetime import datetime
from typing import Optional, Any
from pathlib import Path

import httpx
import uvicorn
import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastmcp import FastMCP
from markitdown import MarkItDown

from models import (
    ConvertRequest,
    ConvertResponse,
    ConvertFolderRequest,
    AnalyzeRequest,
    ExtractRequest,
    TemplateResponse,
    HealthResponse,
    MetaData,
    ErrorCode,
    create_error_response,
    create_success_response,
)

from settings import *  # noqa: F401, F403
from logging_setup import setup_logging

setup_logging()
log = structlog.get_logger()

# =============================================================================
# Module-Imports (ausgelagerte Funktionen)
# =============================================================================

from utils import (
    resolve_path,
    get_file_extension,
    is_image_file,
    is_markitdown_file,
    is_audio_file,
    is_video_file,
    should_skip_file,
    get_mimetype,
    detect_mimetype_from_bytes,
    strip_llm_artifacts,
    detect_code_language,
    detect_and_fence_code_blocks,
)
from mistral_client import (
    call_mistral_vision_api,
    call_mistral_ocr_api,
    analyze_with_mistral_vision,
)
from converters.images import (
    resize_image_if_needed,
    extract_image_metadata,
    _dms_to_decimal,
    extract_images_from_docx,
    extract_images_from_pptx,
    classify_image_type,
    convert_diagram_to_mermaid,
    extract_chart_data,
    describe_embedded_images,
    insert_image_descriptions,
    render_first_page_as_image,
)
from converters.pdf import (
    extract_tables_with_pdfplumber,
    extract_tables_with_img2table,
    merge_cross_page_tables,
    tables_to_markdown,
    is_scanned_pdf,
    convert_scanned_pdf_ocr3,
    convert_scanned_pdf,
    extract_pdf_metadata,
    detect_zugferd,
    extract_xmp_metadata,
    list_embedded_files,
    parse_zugferd_xml,
    prepend_pdf_toc,
    append_pdf_annotations,
    append_pdf_form_fields,
    embed_ocr_in_pdf,
)
from converters.office import (
    convert_excel_enhanced,
    extract_docx_extras,
    append_docx_extras_to_markdown,
    extract_document_properties,
    extract_pptx_hidden_info,
    convert_with_markitdown,
)
from converters.audio import (
    extract_audio_from_video,
    transcribe_audio,
    WHISPER_AVAILABLE,
)
from converters.email import extract_email_metadata, ICALENDAR_AVAILABLE
from settings import (  # noqa: F401 — re-exported for test patchability
    _classify_categories_cache,
    _CLASSIFY_CACHE_TTL,
    _whisper_model_cache,
)
from intelligence import (
    JSONSCHEMA_AVAILABLE,
    classify_document,
    correct_ocr_text,
    extract_structured_data,
    calculate_quality_score,
    chunk_markdown,
    dual_pass_validate,
    get_classify_categories_from_db,
    find_matching_template,
    _make_null_tolerant,
    _apply_auto_extract,
    get_db_connection,
    get_template_by_id,
    _META_SCHEMA,
    _STEUER_SIGNALWOERTER,
    _DATENTYP_KONVENTIONEN,
)

# Re-export availability flags and key names from sub-modules so tests can patch
# via server namespace. Sub-modules read these back via _get() for test-patchability.
from converters.pdf import (  # noqa: E402
    PDFPLUMBER_AVAILABLE,
    PDF2IMAGE_AVAILABLE,
    PYMUPDF_AVAILABLE,
)
from converters.office import OPENPYXL_AVAILABLE  # noqa: E402
# Re-export img2table availability and classes so tests can patch them.
from converters.pdf import IMG2TABLE_AVAILABLE  # noqa: E402
try:
    from img2table.document import PDF as Img2TablePDF  # noqa: E402
    from img2table.ocr import TesseractOCR  # noqa: E402
except ImportError:
    Img2TablePDF = None  # type: ignore[assignment,misc]
    TesseractOCR = None  # type: ignore[assignment]
# Re-export convert_from_path so tests can patch _server.convert_from_path.
try:
    from pdf2image import convert_from_path  # noqa: E402
except ImportError:
    convert_from_path = None  # type: ignore[assignment]


# =============================================================================
# Initialisierung
# =============================================================================

# MarkItDown Instanz
md = MarkItDown()

# FastMCP für MCP-Protokoll
mcp = FastMCP("daigestr")

# FastAPI für REST
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


async def convert_url(url: str) -> dict[str, Any]:
    """Konvertiert eine URL zu Markdown (non-blocking via asyncio.to_thread)."""
    try:
        log.info("url_convert", url=url)
        result = await asyncio.to_thread(md.convert_url, url)
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


# (get_classify_categories_from_db moved to intelligence.py)


# (classify_document, correct_ocr_text, _META_SCHEMA, _STEUER_SIGNALWOERTER,
#  _DATENTYP_KONVENTIONEN moved to intelligence.py)


# =============================================================================
# Template Registry — SQLite (T-MKIT-035)
# =============================================================================

def init_templates_db() -> None:
    """Erstellt die templates.db beim ersten Start und migriert bestehende Templates."""
    TEMPLATES_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(TEMPLATES_DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS template (
            id TEXT PRIMARY KEY,
            category TEXT NOT NULL DEFAULT 'other',
            display_name TEXT NOT NULL,
            description TEXT,
            schema TEXT NOT NULL,
            field_descriptions TEXT,
            classify_keywords TEXT,
            typical_senders TEXT,
            steuer_relevanz TEXT,
            priority INTEGER DEFAULT 0,
            enabled BOOLEAN DEFAULT 1,
            version INTEGER DEFAULT 1,
            source TEXT DEFAULT 'manual',
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_templates_category ON template(category)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_templates_enabled ON template(enabled)")

    # Seed aus seed.sql wenn DB leer
    cursor = conn.execute("SELECT COUNT(*) FROM template")
    if cursor.fetchone()[0] == 0:
        seed_path = Path(__file__).parent / "seed.sql"
        if seed_path.exists():
            seed_sql = seed_path.read_text(encoding="utf-8")
            conn.executescript(seed_sql)
            log.info("templates_seeded", source=str(seed_path))
    conn.commit()
    conn.close()


# get_db_connection and get_template_by_id imported from intelligence


def get_all_template_ids() -> list[str]:
    """Gibt alle aktiven Template-IDs aus der DB zurück."""
    conn = get_db_connection()
    rows = conn.execute("SELECT id FROM template WHERE enabled = 1 ORDER BY category, display_name").fetchall()
    conn.close()
    return [r["id"] for r in rows]


def search_templates(query: str) -> list[dict]:
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT id, category, display_name, description, enabled FROM template "
        "WHERE id LIKE ? OR display_name LIKE ? OR description LIKE ? OR classify_keywords LIKE ?",
        (f"%{query}%", f"%{query}%", f"%{query}%", f"%{query}%")
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]




# =============================================================================
# Core Konvertierungs-Logik
# =============================================================================

async def convert_auto(
    file_data: bytes,
    filename: str,
    source: str,
    source_type: str,
    input_meta: dict[str, Any],
    prompt: Optional[str] = None,
    language: str = "de",
    describe_images: bool = False,
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
) -> ConvertResponse:
    """
    Intelligente Konvertierung basierend auf Dateityp.

    Args:
        file_data: Rohe Datei-Bytes
        filename: Dateiname (wird für Extension-Erkennung genutzt)
        source: Quell-Pfad oder -Bezeichnung (für Metadaten)
        source_type: 'file', 'base64' oder 'url'
        input_meta: Beliebige Pass-through-Metadaten
        prompt: Optionaler Custom-Prompt für Vision
        language: Antwortsprache ('de' oder 'en')
        describe_images: Eingebettete Bilder in DOCX/PPTX durch Pixtral beschreiben
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
    ext = get_file_extension(filename)
    mimetype = detect_mimetype_from_bytes(file_data) or get_mimetype(Path(filename))

    meta = {
        **input_meta,
        "source": source,
        "source_type": source_type,
        "format": ext.lstrip("."),
        "size_bytes": len(file_data),
    }

    # Größenprüfung
    if len(file_data) > MAX_FILE_SIZE_BYTES:
        meta["duration_ms"] = int((time.time() - start_time) * 1000)
        return create_error_response(
            ErrorCode.FILE_TOO_LARGE,
            f"Datei zu groß: {len(file_data) / 1024 / 1024:.1f}MB (Max: {MAX_FILE_SIZE_MB}MB)",
            meta=meta
        )

    # T-MKIT-020: Accuracy-Modus immer in Meta dokumentieren
    meta["accuracy_mode"] = accuracy

    # T-MKIT-023: High-Accuracy ohne API-Key → Warning + Degradierung auf Standard
    # Nur für Dateitypen die Mistral API nutzen (Bilder, PDFs) — nicht für Audio/Video (Whisper)
    _needs_mistral_api = ext in IMAGE_EXTENSIONS or ext in MARKITDOWN_EXTENSIONS
    if accuracy == "high" and not MISTRAL_API_KEY and _needs_mistral_api:
        log.warning("high_accuracy_degraded_no_api_key")
        meta["accuracy_warning"] = "High accuracy requested but MISTRAL_API_KEY not set — running in standard mode"
        accuracy = "standard"
        meta["accuracy_mode"] = accuracy

    # Bild → Vision
    if ext in IMAGE_EXTENSIONS or (mimetype and mimetype.startswith("image/")):
        processed_data, resize_meta = resize_image_if_needed(file_data)
        meta.update(resize_meta)
        meta["vision_used"] = True

        vision_prompt = prompt or (
            "Analysiere dieses Bild und gib den Inhalt als Markdown zurück.\n\n"
            "- Wenn Text sichtbar ist: extrahiere ihn vollständig und strukturiert "
            "(Überschriften, Listen, Tabellen in Markdown-Syntax)\n"
            "- Wenn es ein Diagramm, Chart oder Grafik ist: beschreibe die dargestellten Daten präzise\n"
            "- Wenn es ein Foto ohne Text ist: beschreibe den Bildinhalt in einem kurzen Absatz\n"
            "- Wenn die Bildqualität zu schlecht ist: schreibe [UNLESERLICH]\n\n"
            "Antworte ausschließlich mit dem Markdown-Ergebnis."
        )

        result = await analyze_with_mistral_vision(
            processed_data,
            mimetype or "image/jpeg",
            vision_prompt,
            language
        )

        meta["duration_ms"] = int((time.time() - start_time) * 1000)

        if result["success"]:
            meta["vision_model"] = result.get("vision_model")
            meta["tokens_prompt"] = result.get("tokens_prompt")
            meta["tokens_completion"] = result.get("tokens_completion")
            meta["tokens_total"] = result.get("tokens_total")

            # T-MKIT-027: EXIF/GPS/IPTC Metadaten aus Original-Bilddaten extrahieren
            try:
                img_metadata = extract_image_metadata(file_data)
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
                correction = await correct_ocr_text(markdown, language=language)
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
                markdown = await dual_pass_validate(
                    markdown=markdown,
                    file_data=processed_data,
                    mimetype=effective_mimetype,
                    language=language,
                )
                pipeline_steps.append("dual_pass_validation")

            meta["pipeline_steps"] = pipeline_steps

            if classify:
                classify_result = await classify_document(markdown, classify_categories, language)
                meta.update(classify_result)
            # AC-010: Quality Scoring
            meta.update(calculate_quality_score(markdown, meta))
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
                response = await _apply_auto_extract(response, meta, markdown, language, min_confidence, hints)
            # AC-014-2/AC-014-3: Strukturierte Extraktion falls Schema gesetzt
            elif extract_schema:
                extraction = await extract_structured_data(markdown, extract_schema, language)
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
                    log.warning("extract_structured_data_failed_vision", error=extraction.get("error"))
            # FR-MKIT-011: Smart Chunking für RAG
            if chunk:
                response.chunks = chunk_markdown(markdown, chunk_size=chunk_size, source=source)
            return response
        else:
            return create_error_response(
                result.get("error_code", ErrorCode.VISION_FAILED),
                result["error"],
                meta=meta
            )

    # Audio/Video → faster-whisper Transkription (FR-MKIT-006)
    elif ext in AUDIO_EXTENSIONS or ext in VIDEO_EXTENSIONS:
        temp_media_path = TEMP_DIR / f"{hashlib.md5(file_data).hexdigest()}_{filename}"
        extracted_wav: Optional[Path] = None
        try:
            temp_media_path.write_bytes(file_data)
            audio_path = temp_media_path

            # Video: erst Audio-Track extrahieren
            if ext in VIDEO_EXTENSIONS:
                log.info("video_audio_extract_start", file=filename)
                try:
                    extracted_wav = extract_audio_from_video(temp_media_path)
                    audio_path = extracted_wav
                except RuntimeError as exc:
                    meta["duration_ms"] = int((time.time() - start_time) * 1000)
                    return create_error_response(
                        ErrorCode.CONVERSION_FAILED,
                        f"Audio-Extraktion fehlgeschlagen: {str(exc)}",
                        meta=meta,
                    )

            # Transkribieren
            transcription = transcribe_audio(audio_path)
            meta["duration_ms"] = int((time.time() - start_time) * 1000)

            if not transcription["success"]:
                return create_error_response(
                    ErrorCode.CONVERSION_FAILED,
                    transcription.get("error", "Transkription fehlgeschlagen"),
                    meta=meta,
                )

            # Meta-Daten setzen (AC-006-6)
            meta["language"] = transcription.get("language", "unknown")
            meta["duration_seconds"] = transcription.get("duration", 0.0)
            meta["whisper_model"] = transcription.get("model_size", WHISPER_MODEL_SIZE)
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
            meta.update(calculate_quality_score(markdown, meta))
            response = create_success_response(markdown, meta=meta)
            # FR-MKIT-011: Smart Chunking für RAG
            if chunk:
                response.chunks = chunk_markdown(markdown, chunk_size=chunk_size, source=source)
            return response

        finally:
            temp_media_path.unlink(missing_ok=True)
            if extracted_wav is not None:
                extracted_wav.unlink(missing_ok=True)

    # Dokument → MarkItDown (mit optionalem Scanned-PDF-Routing)
    elif ext in MARKITDOWN_EXTENSIONS or ext:
        temp_path = TEMP_DIR / f"{hashlib.md5(file_data).hexdigest()}_{filename}"
        try:
            temp_path.write_bytes(file_data)

            # Scanned PDF Detection: VOR dem normalen markitdown-Pfad prüfen
            if ext == ".pdf" and is_scanned_pdf(temp_path):
                log.info("scanned_pdf_detected", file=filename)
                result = await convert_scanned_pdf(temp_path, language=language)
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
                        correction = await correct_ocr_text(scanned_markdown, language=language)
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
                        log.info("high_accuracy_scanned_pdf_dual_pass_start", file=filename)
                        rendered = render_first_page_as_image(temp_path)
                        if rendered is not None:
                            page_image_bytes, page_mimetype = rendered
                            scanned_markdown = await dual_pass_validate(
                                markdown=scanned_markdown,
                                file_data=page_image_bytes,
                                mimetype=page_mimetype,
                                language=language,
                            )
                            scanned_pipeline_steps.append("dual_pass_validation")
                        else:
                            log.warning("high_accuracy_scanned_pdf_dual_pass_skipped_no_pdf2image")

                    meta["pipeline_steps"] = scanned_pipeline_steps

                    if classify:
                        classify_result = await classify_document(scanned_markdown, classify_categories, language)
                        meta.update(classify_result)
                    # AC-010: Quality Scoring
                    meta.update(calculate_quality_score(scanned_markdown, meta))
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
                        embedded = embed_ocr_in_pdf(file_data, scanned_markdown, pages_text_list)
                        if embedded is not None:
                            response.enriched_pdf = base64.b64encode(embedded).decode("utf-8")
                            log.info("ocr_embed_done", size_bytes=len(embedded))
                        else:
                            log.warning("ocr_embed_failed_no_output")
                    # T-MKIT-036: Auto-Extract (classify → template lookup → extraction)
                    if auto_extract and not extract_schema:
                        response = await _apply_auto_extract(response, meta, scanned_markdown, language, min_confidence, scanned_hints)
                    # AC-014-2/AC-014-3: Strukturierte Extraktion falls Schema gesetzt
                    elif extract_schema:
                        extraction = await extract_structured_data(scanned_markdown, extract_schema, language)
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
                            log.warning("extract_structured_data_failed_scanned_pdf", error=extraction.get("error"))
                    # FR-MKIT-011: Smart Chunking für RAG
                    if chunk:
                        response.chunks = chunk_markdown(scanned_markdown, chunk_size=chunk_size, source=source)
                    return response
                else:
                    return create_error_response(
                        result.get("error_code", ErrorCode.CONVERSION_FAILED),
                        result["error"],
                        meta=meta
                    )

            result = convert_with_markitdown(temp_path, show_formulas=show_formulas)
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

                # Eingebettete Bilder beschreiben (nur für DOCX/PPTX, nur wenn aktiviert)
                if describe_images and ext in {".docx", ".doc", ".pptx", ".ppt"}:
                    log.info("embedded_images_describe_start", file=filename, ext=ext)
                    if ext in {".docx", ".doc"}:
                        images = extract_images_from_docx(temp_path)
                    else:
                        images = extract_images_from_pptx(temp_path)

                    if images:
                        descriptions = await describe_embedded_images(images, language=language)
                        markdown = insert_image_descriptions(markdown, descriptions)
                        meta["images_described"] = len(descriptions)
                        log.info(
                            "embedded_images_described",
                            file=filename,
                            count=len(descriptions),
                        )

                # T-MKIT-020: High-Accuracy → Dual-Pass Validation für PDFs und Bilder
                if accuracy == "high" and ext == ".pdf":
                    log.info("high_accuracy_pdf_dual_pass_start", file=filename)
                    rendered = render_first_page_as_image(temp_path)
                    if rendered is not None:
                        page_image_bytes, page_mimetype = rendered
                        markdown = await dual_pass_validate(
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
                    classify_result = await classify_document(markdown, classify_categories, language)
                    meta.update(classify_result)

                # AC-010: Quality Scoring
                meta.update(calculate_quality_score(markdown, meta))
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
                if not describe_images and ext in (".docx", ".pptx") and source_type == "file":
                    doc_hints.append("This document may contain embedded images. Add describe_images=true to describe them via Vision AI.")
                # T-MKIT-024: ZUGFeRD hint wenn erkannt aber kein template/extract_schema
                if meta.get("zugferd") is not None and not extract_schema and not auto_extract:
                    doc_hints.append("This PDF contains embedded ZUGFeRD/Factur-X e-invoice data. Add template='invoice' to get the structured data in the extracted field — no LLM needed, 100% accurate.")
                if doc_hints:
                    meta["hints"] = doc_hints
                response = create_success_response(markdown, meta=meta)
                # T-MKIT-036: Auto-Extract (classify → template lookup → extraction)
                if auto_extract and not extract_schema:
                    response = await _apply_auto_extract(response, meta, markdown, language, min_confidence, doc_hints)
                # T-MKIT-024: ZUGFeRD Daten direkt als extracted verwenden (kein LLM nötig)
                elif extract_schema and meta.get("zugferd") is not None:
                    response.extracted = meta["zugferd"]
                    meta["zugferd_source"] = "embedded_xml"
                    meta["extraction_method"] = "zugferd"
                    response.meta = MetaData(**{k: v for k, v in meta.items()})
                    log.info("zugferd_used_as_extracted", file=filename)
                elif extract_schema:
                    # AC-014-2/AC-014-3: Strukturierte Extraktion falls Schema gesetzt (kein ZUGFeRD)
                    extraction = await extract_structured_data(markdown, extract_schema, language)
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
                        log.warning("extract_structured_data_failed_markitdown", error=extraction.get("error"))
                # FR-MKIT-011: Smart Chunking für RAG
                if chunk:
                    response.chunks = chunk_markdown(markdown, chunk_size=chunk_size, source=source)
                return response
            else:
                return create_error_response(
                    result.get("error_code", ErrorCode.CONVERSION_FAILED),
                    result["error"],
                    meta=meta
                )
        finally:
            temp_path.unlink(missing_ok=True)

    else:
        meta["duration_ms"] = int((time.time() - start_time) * 1000)
        return create_error_response(
            ErrorCode.UNSUPPORTED_FORMAT,
            f"Nicht unterstütztes Format: {ext}",
            meta=meta
        )


async def convert_folder_contents(
    folder_path: Path,
    input_meta: dict[str, Any],
    language: str = "de",
) -> ConvertResponse:
    """
    Konvertiert alle Dateien in einem Ordner zu einem zusammengeführten Markdown.
    """
    start_time = time.time()
    log.info("folder_convert_start", folder=str(folder_path))

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
        if f.is_file() and not should_skip_file(f.name)
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
            result = await convert_auto(
                file_data=file_data,
                filename=file_path.name,
                source=str(file_path),
                source_type="file",
                input_meta={},
                language=language,  # T-MKIT-022: language durchgereicht
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


# =============================================================================
# Tips Helper (T-MKIT-032)
# =============================================================================

_EXAMPLE_URL = "https://example.com"  # Example URL for documentation purposes only

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
            {"problem": "extracted is null with auto_extract=true", "cause": "No template registered for this document type", "fix": "Register a template via POST /v1/templates or check meta.document_type and meta.template_used for details"},
            {"problem": "chunks is null", "cause": "Missing chunk parameter", "fix": "Add chunk=true to get RAG-ready chunks in the chunks field"},
            {"problem": "Poor quality on scanned PDFs", "cause": "Using standard accuracy", "fix": "Set accuracy='high' for OCR correction + dual-pass validation"},
            {"problem": "Images in DOCX/PPTX not described", "cause": "describe_images defaults to false", "fix": "Set describe_images=true (costs extra API calls)"},
            {"problem": "Document type not detected", "cause": "classify defaults to false", "fix": "Set classify=true to get document_type in meta"},
            {"problem": "OCR errors in output", "cause": "No post-correction", "fix": "Set ocr_correct=true or accuracy='high' (auto-enables correction)"},
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
            "template": {"type": "str", "default": None, "description": "Shortcut for extract_schema. Available: invoice, cv, contract"},
            "auto_extract": {"type": "bool", "default": False, "description": "Automatically classify document, find matching template, and extract structured data — all in one call. No template or extract_schema needed."},
            "min_confidence": {"type": "float", "default": 0.7, "description": "Minimum classification confidence for auto_extract to use a template (0.0-1.0)"},
            "describe_images": {"type": "bool", "default": False, "description": "Extract and describe embedded images in DOCX/PPTX via Vision AI"},
            "ocr_correct": {"type": "bool", "default": False, "description": "LLM post-correction for OCR errors"},
            "ocr_embed": {"type": "bool", "default": False, "description": "Embed OCR text layer into PDF — creates searchable PDF output"},
            "show_formulas": {"type": "bool", "default": False, "description": "Show Excel formulas in output"},
            "chunk": {"type": "bool", "default": False, "description": "Split output into RAG-ready chunks"},
            "chunk_size": {"type": "int", "default": 512, "description": "Approximate chunk size in tokens"},
            "language": {"type": "str", "default": "de", "description": "Language for Vision/OCR responses"},
            "zugferd": {"type": "bool", "default": "auto", "description": "Extract ZUGFeRD/Factur-X e-invoice data from PDF (auto-detected, 100% accuracy, no LLM required)"},
        },
        "response_fields": {
            "markdown": "Always present — the converted document as Markdown",
            "extracted": "Only present when extract_schema or template is set — structured JSON",
            "chunks": "Only present when chunk=true — list of text segments with metadata",
            "meta": "Always present — processing metadata (quality_score, duration, pipeline_steps, etc.)",
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
        "note_mcp_vs_rest": "MCP tool 'convert' uses 'base64_data' parameter (not 'base64' like REST API)",
    }


# =============================================================================
# REST API Endpoints
# =============================================================================

@app.post("/v1/convert", response_model=ConvertResponse)
async def api_convert(request: ConvertRequest) -> ConvertResponse:
    """
    Konvertiert eine Datei zu Markdown.
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

    # Pfad-basiert
    if request.path:
        file_path = resolve_path(request.path)
        if not file_path.exists():
            return create_error_response(
                ErrorCode.FILE_NOT_FOUND,
                f"Datei nicht gefunden: {file_path}",
                meta=request.meta
            )
        file_data = file_path.read_bytes()
        return await convert_auto(
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
        return await convert_auto(
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
        )

    # URL — T-MKIT-022: durch convert_auto() routen für vollständige Pipeline
    if request.url:
        import tempfile as _tempfile

        # Content-Type → Extension ermitteln; HTML-Seiten direkt mit convert_url() (markitdown-native)
        try:
            async with httpx.AsyncClient(timeout=float(MISTRAL_TIMEOUT)) as client:
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
            start_time = time.time()
            result = await convert_url(request.url)
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
                if request.classify:
                    classify_result = await classify_document(
                        result["markdown"], request.classify_categories, request.language
                    )
                    meta.update(classify_result)
                response = create_success_response(result["markdown"], meta=meta)
                # T-MKIT-036: Auto-Extract
                if request.auto_extract and not effective_schema:
                    response = await _apply_auto_extract(response, meta, result["markdown"], request.language, request.min_confidence, [])
                elif effective_schema:
                    extraction = await extract_structured_data(result["markdown"], effective_schema, request.language)
                    if extraction["success"]:
                        response.extracted = extraction["extracted"]
                    else:
                        log.warning("extract_structured_data_failed_url_html", error=extraction.get("error"))
                if request.chunk:
                    response.chunks = chunk_markdown(result["markdown"], chunk_size=request.chunk_size, source=request.url)
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
                async with httpx.AsyncClient(timeout=float(MISTRAL_TIMEOUT)) as client:
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
            temp_path = TEMP_DIR / f"url_{url_hash}{guessed_ext}"
            temp_path.write_bytes(dl_resp.content)
            try:
                return await convert_auto(
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
    return await api_convert(convert_req)


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
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(status_code=409, detail=f"Template '{request['id']}' already exists")
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


@app.get("/v1/tips")
async def api_tips() -> dict:
    """Usage tips and common patterns for Daigestr."""
    return _build_tips_dict()


# =============================================================================
# MCP Tools
# =============================================================================

@mcp.tool(name="convert")
async def mcp_convert(
    path: Optional[str] = None,
    base64_data: Optional[str] = None,
    filename: Optional[str] = None,
    url: Optional[str] = None,
    meta: Optional[dict] = None,
    accuracy: str = "standard",
    classify: bool = False,
    classify_categories: Optional[list] = None,
    describe_images: bool = False,
    ocr_correct: bool = False,
    ocr_embed: bool = False,
    show_formulas: bool = False,
    chunk: bool = False,
    chunk_size: int = 512,
    extract_schema: Optional[dict] = None,
    template: Optional[str] = None,
    language: str = "de",
    prompt: Optional[str] = None,
    auto_extract: bool = False,
    min_confidence: float = 0.7,
) -> str:
    """
    Converts a file, URL, or base64 payload to Markdown.

    Default output is always Markdown in the 'markdown' field.

    To get STRUCTURED JSON, add extract_schema or template parameter.
    To get AUTO-EXTRACTED JSON (no template needed), add auto_extract=true.
    To get RAG CHUNKS, add chunk=true.
    To CLASSIFY the document type, add classify=true.
    To DESCRIBE IMAGES in DOCX/PPTX, add describe_images=true.
    For SCANNED PDFs, use accuracy='high' for best results.
    To embed OCR text as searchable layer in scanned PDFs, add ocr_embed=true.

    Examples:
      convert(path="<data_dir>/invoice.pdf", template="invoice")  -> extracted JSON
      convert(path="<data_dir>/invoice.pdf", auto_extract=true)  -> classify + auto template lookup + extracted JSON
      convert(path="<data_dir>/scan.pdf", accuracy="high")  -> OCR + correction
      convert(path="<data_dir>/scan.pdf", ocr_embed=true)  -> searchable PDF in enriched_pdf
      convert(url="<url>", chunk=true)  -> RAG chunks
      convert(path="<data_dir>/report.docx", describe_images=true, classify=true)

    Note: MCP uses 'base64_data', REST API uses 'base64'.

    Args:
        path: Dateipfad im Container.
        base64_data: Base64-kodierte Datei (erfordert filename).
        filename: Dateiname (erforderlich bei base64_data).
        url: URL zu Datei oder Webseite.
        meta: Beliebige Metadaten (werden durchgereicht).
        accuracy: Accuracy-Modus: 'standard' (Default) oder 'high'.
        classify: Dokumenttyp via LLM klassifizieren.
        classify_categories: Erlaubte Dokumenttypen (überschreibt Default).
        describe_images: Eingebettete Bilder in DOCX/PPTX beschreiben.
        ocr_correct: OCR-Nachkorrektur via LLM aktivieren.
        ocr_embed: Wenn True, OCR-Text als unsichtbare Textschicht in gescannte PDFs einbetten.
        show_formulas: Excel-Formeln im Output annotieren.
        chunk: Smart Chunking für RAG aktivieren.
        chunk_size: Maximale Chunk-Größe in Tokens (Default: 512).
        extract_schema: JSON-Schema für strukturierte Daten-Extraktion.
        template: Vordefinierter Template-Name als Alternative zu extract_schema.
        language: Antwortsprache ('de' oder 'en').
        prompt: Optionaler Custom-Prompt für Vision.
        auto_extract: Wenn True, wird der Dokumenttyp klassifiziert, ein passendes Template gesucht und strukturierte Daten extrahiert — alles in einem Schritt.
        min_confidence: Minimale Klassifizierungs-Konfidenz für auto_extract (Default: 0.7).
    """
    # Template → Schema Auflösung
    effective_schema = extract_schema
    if template and not effective_schema:
        tmpl = get_template_by_id(template)
        if tmpl is None:
            return json.dumps({
                "success": False,
                "error": f"Unbekanntes Template: '{template}'. Verfügbar: {get_all_template_ids()}"
            })
        effective_schema = tmpl["schema"]

    if path:
        file_path = resolve_path(path)
        if not file_path.exists():
            return json.dumps({"success": False, "error": f"Datei nicht gefunden: {file_path}"})
        file_data = file_path.read_bytes()
        response = await convert_auto(
            file_data=file_data,
            filename=file_path.name,
            source=str(file_path),
            source_type="file",
            input_meta=meta or {},
            prompt=prompt,
            language=language,
            describe_images=describe_images,
            classify=classify,
            classify_categories=classify_categories,
            extract_schema=effective_schema,
            ocr_correct=ocr_correct,
            show_formulas=show_formulas,
            chunk=chunk,
            chunk_size=chunk_size,
            accuracy=accuracy,
            ocr_embed=ocr_embed,
            auto_extract=auto_extract,
            min_confidence=min_confidence,
        )
    elif base64_data and filename:
        try:
            file_data = base64.b64decode(base64_data)
        except Exception as e:
            return json.dumps({"success": False, "error": f"Ungültiges Base64: {e}"})
        response = await convert_auto(
            file_data=file_data,
            filename=filename,
            source="base64",
            source_type="base64",
            input_meta=meta or {},
            prompt=prompt,
            language=language,
            describe_images=describe_images,
            classify=classify,
            classify_categories=classify_categories,
            extract_schema=effective_schema,
            ocr_correct=ocr_correct,
            show_formulas=show_formulas,
            chunk=chunk,
            chunk_size=chunk_size,
            accuracy=accuracy,
            ocr_embed=ocr_embed,
            auto_extract=auto_extract,
            min_confidence=min_confidence,
        )
    elif url:
        result = await convert_url(url)
        if result["success"]:
            url_meta: dict[str, Any] = {"source": url, "source_type": "url"}
            if result.get("title"):
                url_meta["title"] = result["title"]
            if classify:
                classify_result = await classify_document(result["markdown"], classify_categories, language)
                url_meta.update(classify_result)
            mcp_url_meta = {**(meta or {}), **url_meta}
            response = create_success_response(result["markdown"], meta=mcp_url_meta)
            # T-MKIT-036: Auto-Extract für URL
            if auto_extract and not effective_schema:
                response = await _apply_auto_extract(response, mcp_url_meta, result["markdown"], language, min_confidence, [])
            elif effective_schema:
                extraction = await extract_structured_data(result["markdown"], effective_schema, language)
                if extraction["success"]:
                    response.extracted = extraction["extracted"]
                else:
                    log.warning("mcp_convert_extract_failed_url", error=extraction.get("error"))
            if chunk:
                response.chunks = chunk_markdown(result["markdown"], chunk_size=chunk_size, source=url)
        else:
            response = create_error_response(result.get("error_code", "ERROR"), result["error"])
    else:
        return json.dumps({"success": False, "error": "path, url oder (base64_data + filename) erforderlich"})

    return response.model_dump_json()


@mcp.tool(name="extract")
async def mcp_extract(
    extract_schema: Optional[dict] = None,
    path: Optional[str] = None,
    base64_data: Optional[str] = None,
    filename: Optional[str] = None,
    url: Optional[str] = None,
    template: Optional[str] = None,
    language: str = "de",
    meta: Optional[dict] = None,
    accuracy: str = "standard",
    ocr_correct: bool = False,
    classify: bool = False,
    auto_extract: bool = False,
    min_confidence: float = 0.7,
) -> str:
    """
    Converts a file and extracts structured data in one step.

    REQUIRES either extract_schema (JSON Schema dict), template name, or auto_extract=true.
    Available templates: 'invoice', 'cv', 'contract'.
    See /v1/templates for template schemas.

    The response contains both 'markdown' (full text) and 'extracted' (structured JSON).

    Examples:
      extract(path="<data_dir>/invoice.pdf", template="invoice")
      extract(path="<data_dir>/cv.pdf", template="cv")
      extract(path="<data_dir>/doc.pdf", auto_extract=true)  -> classify + auto template lookup + extracted JSON
      extract(path="<data_dir>/doc.pdf", extract_schema={"type": "object", "properties": {"title": {"type": "string"}}})

    Args:
        extract_schema: JSON-Schema für die gewünschten extrahierten Felder (optional wenn template oder auto_extract gesetzt).
        path: Dateipfad im Container (alternativ zu base64_data oder url).
        base64_data: Base64-kodierte Datei (erfordert filename).
        filename: Dateiname (erforderlich bei base64_data).
        url: URL zu Datei oder Webseite (alternativ zu path/base64_data).
        template: Vordefinierter Template-Name ('invoice', 'cv', 'contract') als
                  Alternative zu extract_schema.
        language: Antwortsprache ('de' oder 'en').
        meta: Beliebige Metadaten (werden durchgereicht).
        accuracy: Accuracy-Modus: 'standard' (Default) oder 'high'.
        ocr_correct: OCR-Nachkorrektur via LLM aktivieren.
        classify: Dokumenttyp via LLM klassifizieren.
        auto_extract: Wenn True, wird Dokumenttyp klassifiziert, ein passendes Template gesucht und Daten extrahiert.
        min_confidence: Minimale Klassifizierungs-Konfidenz für auto_extract (Default: 0.7).
    """
    # Template → Schema Auflösung
    effective_schema = extract_schema
    if template and not effective_schema:
        tmpl = get_template_by_id(template)
        if tmpl is None:
            return json.dumps({
                "success": False,
                "error": f"Unbekanntes Template: '{template}'. Verfügbar: {get_all_template_ids()}"
            })
        effective_schema = tmpl["schema"]

    # auto_extract=true macht extract_schema/template optional
    if not effective_schema and not auto_extract:
        return json.dumps({
            "success": False,
            "error": "Entweder 'extract_schema', 'template' oder 'auto_extract=true' muss angegeben werden"
        })

    if path:
        file_path = resolve_path(path)
        if not file_path.exists():
            return json.dumps({"success": False, "error": f"Datei nicht gefunden: {file_path}"})
        file_data = file_path.read_bytes()
        response = await convert_auto(
            file_data=file_data,
            filename=file_path.name,
            source=str(file_path),
            source_type="file",
            input_meta=meta or {},
            language=language,
            extract_schema=effective_schema,
            accuracy=accuracy,
            ocr_correct=ocr_correct,
            classify=classify,
            auto_extract=auto_extract,
            min_confidence=min_confidence,
        )
    elif base64_data and filename:
        try:
            file_data = base64.b64decode(base64_data)
        except Exception as e:
            return json.dumps({"success": False, "error": f"Ungültiges Base64: {e}"})
        response = await convert_auto(
            file_data=file_data,
            filename=filename,
            source="base64",
            source_type="base64",
            input_meta=meta or {},
            language=language,
            extract_schema=effective_schema,
            accuracy=accuracy,
            ocr_correct=ocr_correct,
            classify=classify,
            auto_extract=auto_extract,
            min_confidence=min_confidence,
        )
    elif url:
        result = await convert_url(url)
        if result["success"]:
            mcp_extract_url_meta: dict[str, Any] = {"source": url, "source_type": "url"}
            if classify:
                classify_result = await classify_document(result["markdown"], None, language)
                mcp_extract_url_meta.update(classify_result)
            combined_meta = {**(meta or {}), **mcp_extract_url_meta}
            response = create_success_response(result["markdown"], meta=combined_meta)
            # T-MKIT-036: Auto-Extract für URL
            if auto_extract and not effective_schema:
                response = await _apply_auto_extract(response, combined_meta, result["markdown"], language, min_confidence, [])
            elif effective_schema:
                extraction = await extract_structured_data(result["markdown"], effective_schema, language)
                if extraction["success"]:
                    response.extracted = extraction["extracted"]
        else:
            response = create_error_response(result.get("error_code", "ERROR"), result["error"])
    else:
        return json.dumps({"success": False, "error": "path, url oder (base64_data + filename) erforderlich"})

    return response.model_dump_json()


@mcp.tool(name="convert_folder")
async def mcp_convert_folder(
    path: str,
    meta: Optional[dict] = None,
    language: str = "de",
) -> str:
    """
    Converts all files in a directory to a single merged Markdown document.
    Each file becomes a ## filename section. Per-file metadata is tracked.

    Args:
        path: Ordnerpfad im Container.
        meta: Beliebige Metadaten (werden durchgereicht).
        language: Antwortsprache ('de' oder 'en').
    """
    folder_path = resolve_path(path)
    response = await convert_folder_contents(
        folder_path=folder_path,
        input_meta=meta or {},
        language=language,
    )
    return response.model_dump_json()


@mcp.tool(name="health")
async def mcp_health() -> str:
    """Health-Check (MCP-Version)."""
    response = await api_health()
    return response.model_dump_json()


@mcp.tool(name="list_files")
async def mcp_list_files(subdir: str = "") -> str:
    """Listet Dateien im /data Verzeichnis auf."""
    target_dir = DATA_DIR / subdir if subdir else DATA_DIR
    if not target_dir.exists():
        return json.dumps({"error": f"Verzeichnis nicht gefunden: {target_dir}"})

    files = []
    for item in sorted(target_dir.iterdir()):
        if item.is_file():
            files.append({
                "name": item.name,
                "size": item.stat().st_size,
                "type": item.suffix.lower()
            })
        elif item.is_dir():
            files.append({"name": item.name + "/", "type": "directory"})

    return json.dumps({"path": str(target_dir), "files": files}, ensure_ascii=False)


@mcp.tool(name="get_tips")
async def mcp_get_tips() -> str:
    """Returns usage tips and common patterns for Daigestr. Call this first to understand available features."""
    return json.dumps(_build_tips_dict(), indent=2, ensure_ascii=False)


# =============================================================================
# Server Start
# =============================================================================

def run_rest_server():
    """Startet den REST-Server in einem separaten Thread."""
    uvicorn.run(
        app,
        host=os.getenv("BIND_HOST", "0.0.0.0"),
        port=REST_PORT,
        log_level=LOG_LEVEL.lower(),
    )


if __name__ == "__main__":
    # Initialize Template Registry DB (T-MKIT-035)
    try:
        init_templates_db()
        log.info("template_registry_initialized", db_path=str(TEMPLATES_DB_PATH))
    except Exception as _db_init_err:
        log.warning("template_registry_init_failed", error=str(_db_init_err))

    log.info("server_starting",
             version=VERSION,
             data_dir=str(DATA_DIR),
             vision_model=MISTRAL_VISION_MODEL,
             vision_enabled=bool(MISTRAL_API_KEY),
             mcp_port=MCP_PORT,
             rest_port=REST_PORT,
             max_file_size_mb=MAX_FILE_SIZE_MB,
             image_max_width=IMAGE_MAX_WIDTH,
             max_retries=MAX_RETRIES)

    print(f"Daigestr v{VERSION}")
    print(f"Data-Verzeichnis: {DATA_DIR}")
    print(f"Vision-Modell: {MISTRAL_VISION_MODEL}")
    print(f"Vision aktiviert: {'Ja' if MISTRAL_API_KEY else 'Nein'}")
    print(f"MCP-Port: {MCP_PORT}")
    print(f"REST-Port: {REST_PORT}")
    print(f"Max Dateigröße: {MAX_FILE_SIZE_MB}MB")
    print(f"Max Bildbreite: {IMAGE_MAX_WIDTH}px")
    print(f"Max Retries: {MAX_RETRIES}")
    print("-" * 50)

    rest_thread = threading.Thread(target=run_rest_server, daemon=True)
    rest_thread.start()
    print(f"REST-API gestartet auf Port {REST_PORT}")

    transport = os.getenv("MCP_TRANSPORT", "sse")
    if transport == "stdio":
        mcp.run()
    else:
        print(f"MCP-Server startet auf Port {MCP_PORT}")
        mcp.run(transport="sse", host=os.getenv("BIND_HOST", "0.0.0.0"), port=MCP_PORT)
