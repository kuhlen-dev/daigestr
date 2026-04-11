"""
MCP Tool Handler für Daigestr.

Enthält alle @mcp.tool Funktionen und die FastMCP mcp Instanz.
"""

import base64
import json
from typing import Any, Optional

import structlog
from fastmcp import FastMCP

from models import (
    ErrorCode,
    create_error_response,
    create_success_response,
)
from settings import DATA_DIR
from utils import _get, resolve_path
from intelligence import (
    classify_document,
    correct_ocr_text,
    extract_structured_data,
    chunk_markdown,
    _apply_auto_extract,
    get_template_by_id,
)
from templates_db import get_all_template_ids
from routing import (
    convert_auto,
    convert_url,
    convert_folder_contents,
    _build_tips_dict,
    finalize_url_markdown_response,
)

log = structlog.get_logger()

# FastMCP Instanz
mcp = FastMCP("daigestr")


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
    retry_on_low_quality: Optional[bool] = None,
    quality_retry_threshold: Optional[float] = None,
    quality_retry_mode: Optional[str] = None,
    mode: str = "default",
    output_format: str = "markdown",
    pages: Optional[str] = None,
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
    For mode='full': page-level rendering for PDFs (faster, sees full page context).
    For mode='deep': like full, plus per-image extraction with classification (best for technical docs).
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
        retry_on_low_quality: Wenn True, darf Daigestr bei zu niedriger Qualität automatisch mit stärkerem Modus erneut laufen.
        quality_retry_threshold: Optionaler Schwellwert für die Eskalation. Null = Env-Default.
        quality_retry_mode: Modus für die Eskalation. Aktuell nur 'full'.
        mode: Processing mode. 'default': use individual parameter settings. 'full': enable all features with page-level rendering for PDFs (faster, sees full page context). Enables: describe_pages, accuracy=high, classify, ocr_correct, auto_extract, chunk. 'deep': like full, plus per-image extraction with classification (diagram->Mermaid, chart->data table, photo->description, text_scan->OCR). Use for technical documents where individual image analysis matters.
        pages: Seitenauswahl für PDFs. Syntax: '1-3', '7,14,22', '10-20,!15'. Null = alle Seiten.
    """
    # Patchable symbols via _get() for test-patchability
    _resolve_path = _get("resolve_path", resolve_path)
    _convert_auto = _get("convert_auto", convert_auto)
    _get_template = _get("get_template_by_id", get_template_by_id)

    # Template → Schema Auflösung
    effective_schema = extract_schema
    if template and not effective_schema:
        tmpl = _get_template(template)
        if tmpl is None:
            return json.dumps({
                "success": False,
                "error": f"Unbekanntes Template: '{template}'. Verfügbar: {get_all_template_ids()}"
            })
        effective_schema = tmpl["schema"]

    if path:
        file_path = _resolve_path(path)
        if not file_path.exists():
            return json.dumps({"success": False, "error": f"Datei nicht gefunden: {file_path}"})
        file_data = file_path.read_bytes()
        response = await _convert_auto(
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
            retry_on_low_quality=retry_on_low_quality,
            quality_retry_threshold=quality_retry_threshold,
            quality_retry_mode=quality_retry_mode,
            mode=mode,
            output_format=output_format,
            pages=pages,
        )
    elif base64_data and filename:
        try:
            file_data = base64.b64decode(base64_data)
        except Exception as e:
            return json.dumps({"success": False, "error": f"Ungültiges Base64: {e}"})
        response = await _convert_auto(
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
            retry_on_low_quality=retry_on_low_quality,
            quality_retry_threshold=quality_retry_threshold,
            quality_retry_mode=quality_retry_mode,
            mode=mode,
            output_format=output_format,
            pages=pages,
        )
    elif url:
        _convert_url = _get("convert_url", convert_url)
        result = await _convert_url(url)
        if result["success"]:
            url_meta: dict[str, Any] = {
                "source": url,
                "source_type": "url",
                "accuracy_mode": accuracy,
            }
            if result.get("title"):
                url_meta["title"] = result["title"]
            response = await finalize_url_markdown_response(
                result["markdown"],
                meta={**(meta or {}), **url_meta},
                source=url,
                language=language,
                accuracy=accuracy,
                classify=classify,
                classify_categories=classify_categories,
                ocr_correct=ocr_correct,
                extract_schema=effective_schema,
                template=template,
                auto_extract=auto_extract,
                min_confidence=min_confidence,
                mode=mode,
                retry_on_low_quality=retry_on_low_quality,
                quality_retry_threshold=quality_retry_threshold,
                quality_retry_mode=quality_retry_mode,
                chunk=chunk,
                chunk_size=chunk_size,
                output_format=output_format,
                compact=False,
            )
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
    prompt: Optional[str] = None,
    language: str = "de",
    meta: Optional[dict] = None,
    accuracy: str = "standard",
    ocr_correct: bool = False,
    classify: bool = False,
    classify_categories: Optional[list] = None,
    describe_images: bool = False,
    chunk: bool = False,
    chunk_size: int = 512,
    ocr_embed: bool = False,
    show_formulas: bool = False,
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
        prompt: Optionaler Custom-Prompt für Vision.
        language: Antwortsprache ('de' oder 'en').
        meta: Beliebige Metadaten (werden durchgereicht).
        accuracy: Accuracy-Modus: 'standard' (Default) oder 'high'.
        ocr_correct: OCR-Nachkorrektur via LLM aktivieren.
        classify: Dokumenttyp via LLM klassifizieren.
        classify_categories: Erlaubte Klassifizierungs-Kategorien.
        describe_images: Eingebettete Bilder via Vision beschreiben.
        chunk: Smart Chunking aktivieren.
        chunk_size: Chunk-Größe in Tokens (Default: 512).
        ocr_embed: OCR-Text als Textschicht in gescannte PDFs einbetten.
        show_formulas: Excel-Formeln im Output annotieren.
        auto_extract: Wenn True, wird Dokumenttyp klassifiziert, ein passendes Template gesucht und Daten extrahiert.
        min_confidence: Minimale Klassifizierungs-Konfidenz für auto_extract (Default: 0.7).
        retry_on_low_quality: Wenn True, darf Daigestr bei zu niedriger Qualität automatisch mit stärkerem Modus erneut laufen.
        quality_retry_threshold: Optionaler Schwellwert für die Eskalation. Null = Env-Default.
        quality_retry_mode: Modus für die Eskalation. Aktuell nur 'full'.
        mode: Processing mode. 'default', 'full', 'deep'.
        output_format: Ausgabeformat: 'markdown', 'html', 'text'.
        pages: Seitenauswahl für PDFs.
        no_cache: Cache für diesen Request umgehen.
        compact: Null-Felder aus dem normalisierten Output entfernen.
    """
    # Patchable symbols via _get() for test-patchability
    _resolve_path = _get("resolve_path", resolve_path)
    _convert_auto = _get("convert_auto", convert_auto)
    _get_template = _get("get_template_by_id", get_template_by_id)

    # Template → Schema Auflösung
    effective_schema = extract_schema
    if template and not effective_schema:
        tmpl = _get_template(template)
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
        file_path = _resolve_path(path)
        if not file_path.exists():
            return json.dumps({"success": False, "error": f"Datei nicht gefunden: {file_path}"})
        file_data = file_path.read_bytes()
        response = await _convert_auto(
            file_data=file_data,
            filename=file_path.name,
            source=str(file_path),
            source_type="file",
            input_meta=meta or {},
            prompt=prompt,
            language=language,
            describe_images=describe_images,
            extract_schema=effective_schema,
            accuracy=accuracy,
            ocr_correct=ocr_correct,
            classify=classify,
            classify_categories=classify_categories,
            show_formulas=show_formulas,
            chunk=chunk,
            chunk_size=chunk_size,
            ocr_embed=ocr_embed,
            auto_extract=auto_extract,
            min_confidence=min_confidence,
            retry_on_low_quality=retry_on_low_quality,
            quality_retry_threshold=quality_retry_threshold,
            quality_retry_mode=quality_retry_mode,
            mode=mode,
            output_format=output_format,
            pages=pages,
            no_cache=no_cache,
            compact=compact,
        )
    elif base64_data and filename:
        try:
            file_data = base64.b64decode(base64_data)
        except Exception as e:
            return json.dumps({"success": False, "error": f"Ungültiges Base64: {e}"})
        response = await _convert_auto(
            file_data=file_data,
            filename=filename,
            source="base64",
            source_type="base64",
            input_meta=meta or {},
            prompt=prompt,
            language=language,
            describe_images=describe_images,
            extract_schema=effective_schema,
            accuracy=accuracy,
            ocr_correct=ocr_correct,
            classify=classify,
            classify_categories=classify_categories,
            show_formulas=show_formulas,
            chunk=chunk,
            chunk_size=chunk_size,
            ocr_embed=ocr_embed,
            auto_extract=auto_extract,
            min_confidence=min_confidence,
            retry_on_low_quality=retry_on_low_quality,
            quality_retry_threshold=quality_retry_threshold,
            quality_retry_mode=quality_retry_mode,
            mode=mode,
            output_format=output_format,
            pages=pages,
            no_cache=no_cache,
            compact=compact,
        )
    elif url:
        _convert_url = _get("convert_url", convert_url)
        result = await _convert_url(url)
        if result["success"]:
            mcp_extract_url_meta: dict[str, Any] = {"source": url, "source_type": "url"}
            response = await finalize_url_markdown_response(
                result["markdown"],
                meta={**(meta or {}), **mcp_extract_url_meta},
                source=url,
                language=language,
                accuracy=accuracy,
                classify=classify,
                classify_categories=classify_categories,
                ocr_correct=ocr_correct,
                extract_schema=effective_schema,
                template=template,
                auto_extract=auto_extract,
                min_confidence=min_confidence,
                mode=mode,
                retry_on_low_quality=retry_on_low_quality,
                quality_retry_threshold=quality_retry_threshold,
                quality_retry_mode=quality_retry_mode,
                chunk=chunk,
                chunk_size=chunk_size,
                output_format=output_format,
                compact=compact,
            )
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
    describe_images: bool = False,
    classify: bool = False,
    classify_categories: Optional[list] = None,
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
) -> str:
    """
    Converts all files in a directory to a single merged Markdown document.
    Each file becomes a ## filename section. Per-file metadata is tracked.

    Args:
        path: Ordnerpfad im Container.
        meta: Beliebige Metadaten (werden durchgereicht).
        language: Antwortsprache ('de' oder 'en').
        describe_images: Eingebettete Bilder in DOCX/PPTX/PDF/ODT/ODP/HTML beschreiben.
        classify: Dokumenttyp via LLM klassifizieren.
        classify_categories: Erlaubte Klassifizierungs-Kategorien.
        extract_schema: JSON Schema für strukturierte Daten-Extraktion.
        auto_extract: Automatisch klassifizieren, Template suchen und Daten extrahieren.
        accuracy: Accuracy-Modus: 'standard' (Default) oder 'high'.
        chunk: Smart Chunking für RAG aktivieren.
        chunk_size: Chunk-Größe in Tokens (Default: 512).
        ocr_correct: OCR-Nachkorrektur via LLM aktivieren.
        ocr_embed: OCR-Text als Textschicht in gescannte PDFs einbetten.
        show_formulas: Excel-Formeln im Output annotieren.
        prompt: Custom Prompt für Vision-Analyse.
        template: Vordefinierter Template-Name als Alternative zu extract_schema.
        min_confidence: Minimale Klassifizierungs-Konfidenz für auto_extract (Default: 0.7).
        mode: Processing mode. 'default': use individual parameter settings. 'full': enable all features with page-level rendering for PDFs. 'deep': like full, plus per-image extraction with classification.
    """
    _resolve_path = _get("resolve_path", resolve_path)
    _convert_folder = _get("convert_folder_contents", convert_folder_contents)
    folder_path = _resolve_path(path)
    response = await _convert_folder(
        folder_path=folder_path,
        input_meta=meta or {},
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
        mode=mode,
    )
    return response.model_dump_json()


@mcp.tool(name="health")
async def mcp_health() -> str:
    """Health-Check (MCP-Version)."""
    from api_rest import api_health  # noqa: PLC0415
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
