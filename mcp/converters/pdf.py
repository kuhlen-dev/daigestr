"""
PDF-spezifische Konvertierungs- und Extraktionsfunktionen.

Enthält:
- Tabellen-Extraktion (pdfplumber, img2table)
- Scan-Erkennung und OCR (Mistral OCR3, Vision-Fallback)
- PDF-Metadaten (Bookmarks, Annotationen, Formularfelder)
- ZUGFeRD/Factur-X E-Rechnungen
- XMP-Metadaten
- Eingebettete Dateien
- OCR-Text in PDF einbetten
"""

import io
import subprocess
from pathlib import Path
from typing import Any, Optional

import httpx
import structlog

try:
    import pdfplumber
    PDFPLUMBER_AVAILABLE = True
except ImportError:
    PDFPLUMBER_AVAILABLE = False

try:
    from pdf2image import convert_from_path
    PDF2IMAGE_AVAILABLE = True
except ImportError:
    PDF2IMAGE_AVAILABLE = False

try:
    import fitz  # PyMuPDF
    PYMUPDF_AVAILABLE = True
except ImportError:
    PYMUPDF_AVAILABLE = False

try:
    from img2table.document import PDF as Img2TablePDF
    from img2table.ocr import TesseractOCR
    IMG2TABLE_AVAILABLE = True
except ImportError:
    IMG2TABLE_AVAILABLE = False

from PIL import Image

from models import ErrorCode
from settings import (
    SCAN_THRESHOLD_CHARS,
    MISTRAL_API_KEY,
    MISTRAL_OCR_MODEL,
    MISTRAL_OCR_ENABLED,
    MISTRAL_TIMEOUT,
    MISTRAL_VISION_MODEL,
    PDF_RENDER_DPI,
    IMAGE_MAX_WIDTH,
    PDFTOTEXT_TIMEOUT,
    PDFINFO_TIMEOUT,
)
from mistral_client import call_mistral_ocr_api, analyze_with_mistral_vision, extract_mistral_ocr_metadata

from utils import _get, _LOADED_BY_SERVER  # noqa: F401

log = structlog.get_logger()

# Bekannte ZUGFeRD/Factur-X Dateinamen in eingebetteten PDF-Anhängen
_ZUGFERD_FILENAMES = [
    "factur-x.xml",       # Factur-X / ZUGFeRD 2.x
    "zugferd-invoice.xml",  # ZUGFeRD 1.x (lowercase variant)
    "ZUGFeRD-invoice.xml",  # ZUGFeRD 1.x (original case)
    "xrechnung.xml",      # XRechnung
]

# Namespaces für ZUGFeRD 2.x / Factur-X (CrossIndustryInvoice:100)
_ZUGFERD_NS_V2 = {
    "rsm": "urn:un:unece:uncefact:data:standard:CrossIndustryInvoice:100",
    "ram": "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:100",
    "udt": "urn:un:unece:uncefact:data:standard:UnqualifiedDataType:100",
}

# Namespaces für ZUGFeRD 1.x
_ZUGFERD_NS_V1 = {
    "rsm": "urn:ferd:CrossIndustryDocument:invoice:1p0:comfort",
    "ram": "urn:un:unece:uncefact:data:standard:ReusableAggregateBusinessInformationEntity:12",
    "udt": "urn:un:unece:uncefact:data:standard:UnqualifiedDataType:15",
}

# ZUGFeRD-Dateinamen die NICHT in embedded_files erscheinen sollen
_ZUGFERD_FILENAMES_SET = {name.lower() for name in (
    "factur-x.xml",
    "zugferd-invoice.xml",
    "zugferd-invoice.xml",
    "xrechnung.xml",
    "order-x.xml",
    "zugferd_1p0_comfort.xml",
    "zugferd-invoice.xml",
)}


def extract_tables_with_pdfplumber(file_path: Path) -> list[dict]:
    """
    Extrahiert Tabellen aus einer PDF-Datei mit pdfplumber.

    Gibt eine Liste von Seitentabellen zurück, wobei jeder Eintrag Seitennummer
    und die gefundenen Tabellen (als Liste von Zeilen-Listen) enthält.

    Args:
        file_path: Pfad zur PDF-Datei.

    Returns:
        Liste mit Dictionaries der Form:
        [{"page": int, "tables": list[list[list[str | None]]]}]
        Leere Liste wenn pdfplumber nicht verfügbar oder keine Tabellen gefunden.
    """
    if not _get("PDFPLUMBER_AVAILABLE", PDFPLUMBER_AVAILABLE):
        log.warning("pdfplumber_not_available")
        return []

    page_tables: list[dict] = []
    try:
        with pdfplumber.open(str(file_path)) as pdf:
            for page_num, page in enumerate(pdf.pages, start=1):
                tables = page.extract_tables()
                if tables:
                    page_tables.append({"page": page_num, "tables": tables})
        log.info(
            "pdfplumber_extracted",
            file=str(file_path),
            pages_with_tables=len(page_tables),
        )
    except Exception as e:
        log.warning("pdfplumber_error", file=str(file_path), error=str(e))
        return []

    return page_tables


def extract_tables_with_img2table(file_path: Path) -> list[list]:
    """
    Extrahiert Tabellen aus einer gescannten PDF-Datei mit img2table + TesseractOCR.

    Wird als Fallback verwendet wenn pdfplumber keine Tabellen in der Datei findet.
    Nutzt TesseractOCR als OCR-Backend für die Erkennung von Zellinhalten.

    Args:
        file_path: Pfad zur PDF-Datei.

    Returns:
        Flache Liste von Tabellen (jede Tabelle = Liste von Zeilen, jede Zeile = Liste
        von Strings). Leere Liste wenn img2table nicht verfügbar, keine Tabellen
        gefunden oder ein Fehler auftritt.
    """
    if not _get("IMG2TABLE_AVAILABLE", IMG2TABLE_AVAILABLE):
        log.warning("img2table_not_available")
        return []

    try:
        ocr = _get("TesseractOCR", TesseractOCR)()
        pdf_doc = _get("Img2TablePDF", Img2TablePDF)(src=str(file_path))
        extracted = pdf_doc.extract_tables(ocr=ocr)

        all_tables: list[list] = []
        # extracted ist ein Dict: {page_index: [ExtractedTable, ...]}
        for _page_idx, page_tables in extracted.items():
            for extracted_table in page_tables:
                # ExtractedTable.df ist ein pandas DataFrame
                df = extracted_table.df
                # Erste Zeile als Header, Rest als Datenzeilen
                rows: list[list[str]] = []
                header = [str(col) if col is not None else "" for col in df.columns]
                rows.append(header)
                for _, row in df.iterrows():
                    rows.append([str(v) if v is not None else "" for v in row])
                all_tables.append(rows)

        log.info(
            "img2table_extracted",
            file=str(file_path),
            table_count=len(all_tables),
        )
        return all_tables

    except Exception as e:
        log.warning("img2table_error", file=str(file_path), error=str(e))
        return []


def merge_cross_page_tables(page_tables: list[dict]) -> list[list]:
    """
    Führt Tabellen die über Seitengrenzen hinweg gehen zusammen.

    Algorithmus:
    - Vergleicht die letzte Tabelle auf Seite N mit der ersten Tabelle auf Seite N+1.
    - Wenn beide die gleiche Spaltenanzahl haben: Zusammenführen.
    - Header-Deduplizierung: Wenn die erste Zeile auf der Folgeseite identisch mit
      der ersten Zeile der Ausgangstabelle ist, wird sie weggelassen.
    - Unterschiedliche Spaltenanzahl → separate Tabellen.

    Args:
        page_tables: Ausgabe von extract_tables_with_pdfplumber.

    Returns:
        Flache Liste aller (ggf. zusammengeführten) Tabellen als Zeilen-Listen.
    """
    if not page_tables:
        return []

    # Alle Tabellen mit ihrer Seitenreihenfolge sammeln
    # Jede Tabelle bekommt eine (Seite, Tabellen-Index)-Referenz
    all_tables: list[list] = []

    for page_entry in page_tables:
        for table in page_entry["tables"]:
            all_tables.append(table)

    if not all_tables:
        return []

    # Tabellen iterativ zusammenführen
    merged: list[list] = [all_tables[0]]

    for current_table in all_tables[1:]:
        last_merged = merged[-1]

        if not last_merged or not current_table:
            merged.append(current_table)
            continue

        last_col_count = len(last_merged[0]) if last_merged else 0
        curr_col_count = len(current_table[0]) if current_table else 0

        if last_col_count == curr_col_count and last_col_count > 0:
            # Gleiche Spaltenanzahl → potenzieller Merge
            last_header = last_merged[0]
            curr_header = current_table[0]

            # Header-Deduplizierung: erste Zeile identisch → überspringen
            rows_to_add = current_table
            if curr_header == last_header:
                rows_to_add = current_table[1:]

            if rows_to_add:
                merged[-1] = last_merged + rows_to_add
        else:
            # Unterschiedliche Spaltenanzahl → separate Tabelle
            merged.append(current_table)

    return merged


def tables_to_markdown(tables: list[list]) -> str:
    """
    Konvertiert extrahierte Tabellen in Markdown-Format.

    Jede Tabelle wird als Markdown-Tabelle mit Header-Trennzeile formatiert.
    None-Werte in Zellen werden als leere Strings behandelt.

    Args:
        tables: Liste von Tabellen, jede Tabelle ist eine Liste von Zeilen,
                jede Zeile ist eine Liste von Zellwerten (str | None).

    Returns:
        Zusammengefügter Markdown-String aller Tabellen, getrennt durch Leerzeilen.
    """
    if not tables:
        return ""

    markdown_parts: list[str] = []

    for table in tables:
        if not table:
            continue

        # Zeilen normalisieren: None → leerer String, alle Werte zu str
        normalized_rows: list[list[str]] = []
        for row in table:
            normalized_rows.append([str(cell) if cell is not None else "" for cell in row])

        if not normalized_rows:
            continue

        lines: list[str] = []

        # Header (erste Zeile)
        header = normalized_rows[0]
        lines.append("| " + " | ".join(header) + " |")

        # Trennzeile
        lines.append("| " + " | ".join(["---"] * len(header)) + " |")

        # Datenzeilen
        for row in normalized_rows[1:]:
            # Sicherstellen dass die Zeilenlänge mit dem Header übereinstimmt
            padded_row = row + [""] * (len(header) - len(row))
            lines.append("| " + " | ".join(padded_row[: len(header)]) + " |")

        markdown_parts.append("\n".join(lines))

    return "\n\n".join(markdown_parts)


def is_scanned_pdf(file_path: Path) -> bool:
    """
    Prüft ob eine PDF-Datei ein eingescanntes Dokument ist.

    Nutzt pdftotext (poppler-utils) um Text zu extrahieren und berechnet den
    Durchschnitt der Zeichen pro Seite. Wenn dieser Durchschnitt unter dem
    konfigurierten Schwellwert (SCAN_THRESHOLD_CHARS) liegt, gilt die PDF
    als Scan.

    Args:
        file_path: Pfad zur PDF-Datei.

    Returns:
        True wenn die Datei als Scan erkannt wurde, False sonst.
    """
    try:
        result = subprocess.run(
            ["pdftotext", str(file_path), "-"],
            capture_output=True,
            text=True,
            timeout=PDFTOTEXT_TIMEOUT,
        )
        if result.returncode != 0:
            log.warning(
                "pdftotext_failed",
                file=str(file_path),
                returncode=result.returncode,
                stderr=result.stderr,
            )
            return False

        extracted_text = result.stdout

        # Seitenanzahl ermitteln
        page_count_result = subprocess.run(
            ["pdfinfo", str(file_path)],
            capture_output=True,
            text=True,
            timeout=PDFINFO_TIMEOUT,
        )
        pages = 1
        if page_count_result.returncode == 0:
            for line in page_count_result.stdout.splitlines():
                if line.lower().startswith("pages:"):
                    try:
                        pages = int(line.split(":", 1)[1].strip())
                    except ValueError:
                        pass
                    break

        total_chars = len(extracted_text.strip())
        avg_chars_per_page = total_chars / max(pages, 1)

        _threshold = _get("SCAN_THRESHOLD_CHARS", SCAN_THRESHOLD_CHARS)
        is_scan = avg_chars_per_page < _threshold
        log.info(
            "scan_detection",
            file=str(file_path),
            pages=pages,
            total_chars=total_chars,
            avg_chars_per_page=avg_chars_per_page,
            threshold=_threshold,
            is_scan=is_scan,
        )
        return is_scan

    except FileNotFoundError:
        log.warning("pdftotext_not_found", file=str(file_path))
        return False
    except subprocess.TimeoutExpired:
        log.warning("pdftotext_timeout", file=str(file_path))
        return False
    except Exception as e:
        log.warning("scan_detection_error", file=str(file_path), error=str(e))
        return False


async def convert_scanned_pdf_ocr3(
    file_path: Path,
    page_indices: Optional[list[int]] = None,
    request_id: Optional[str] = None,
    attempt_number: Optional[int] = None,
) -> dict[str, Any]:
    """
    Konvertiert ein gescanntes PDF via Mistral OCR 3 API (/v1/ocr).

    Args:
        file_path: Pfad zur PDF-Datei.
        page_indices: Optionale Liste von 0-basierten Seiten-Indices. Wenn None, alle Seiten.

    Returns:
        Dict mit folgenden Schlüsseln:
        - success (bool)
        - markdown (str): Zusammengeführter Markdown-Text aller Seiten
        - ocr_model (str): Verwendetes OCR-Modell
        - pages (int): Anzahl verarbeiteter Seiten
        - error_code / error: Nur bei Fehler
    """
    if not _get("MISTRAL_API_KEY", MISTRAL_API_KEY):
        return {
            "success": False,
            "error_code": ErrorCode.API_KEY_INVALID,
            "error": "MISTRAL_API_KEY nicht konfiguriert",
        }

    log.info(
        "ocr3_convert_start",
        file=str(file_path),
        model=MISTRAL_OCR_MODEL,
        request_id=request_id,
        attempt_number=attempt_number,
    )

    try:
        file_data = file_path.read_bytes()
    except Exception as e:
        log.error("ocr3_read_failed", file=str(file_path), error=str(e), request_id=request_id, attempt_number=attempt_number)
        return {
            "success": False,
            "error_code": ErrorCode.CONVERSION_FAILED,
            "error": f"Datei konnte nicht gelesen werden: {str(e)}",
        }

    try:
        result = await _get("call_mistral_ocr_api", call_mistral_ocr_api)(
            file_data,
            file_path.name,
            page_indices=page_indices,
            request_id=request_id,
            attempt_number=attempt_number,
            pipeline_step="convert_scanned_pdf_ocr3",
        )
    except httpx.TimeoutException:
        log.error("ocr3_timeout", timeout=MISTRAL_TIMEOUT, request_id=request_id, attempt_number=attempt_number)
        return {
            "success": False,
            "error_code": ErrorCode.TIMEOUT,
            "error": f"Mistral OCR API Timeout nach {MISTRAL_TIMEOUT}s",
        }
    except httpx.HTTPStatusError as e:
        error_detail = str(e)
        try:
            error_detail = e.response.json().get("error", {}).get("message", str(e))
        except Exception:
            pass
        log.error("ocr3_api_error", error=error_detail, request_id=request_id, attempt_number=attempt_number)
        return {
            "success": False,
            "error_code": ErrorCode.API_ERROR,
            "error": f"Mistral OCR API Fehler: {error_detail}",
        }
    except Exception as e:
        log.error("ocr3_exception", error=str(e), request_id=request_id, attempt_number=attempt_number)
        return {
            "success": False,
            "error_code": ErrorCode.API_ERROR,
            "error": f"OCR API Fehler: {str(e)}",
        }

    try:
        pages = result.get("pages", [])
    except (AttributeError, TypeError) as e:
        log.error("ocr3_invalid_response", error=str(e), request_id=request_id, attempt_number=attempt_number)
        return {
            "success": False,
            "error_code": ErrorCode.API_ERROR,
            "error": f"Ungültige OCR-API-Antwort: {str(e)}",
        }
    if not pages:
        log.warning("ocr3_no_pages", file=str(file_path), request_id=request_id, attempt_number=attempt_number)
        return {
            "success": False,
            "error_code": ErrorCode.CONVERSION_FAILED,
            "error": "OCR API lieferte keine Seiten zurück",
        }

    if page_indices is not None and not pages:
        return {
            "success": False,
            "error_code": ErrorCode.CONVERSION_FAILED,
            "error": "Keine Seiten nach Seitenfilterung übrig",
        }

    ocr_metadata = _get("extract_mistral_ocr_metadata", extract_mistral_ocr_metadata)(result)

    markdown_parts = []
    total_pages = len(pages)
    for i, page in enumerate(pages):
        page_index = page.get("index", 0) + 1
        page_markdown = page.get("markdown", "")
        markdown_parts.append(f"## Seite {page_index}\n\n{page_markdown}")
        log.info(
            "convert_progress",
            step="ocr",
            detail=f"page {i+1}/{total_pages}",
            progress=int(10 + 50 * i / total_pages) if total_pages else 10,
            request_id=request_id,
            attempt_number=attempt_number,
        )

    combined_markdown = "\n\n".join(markdown_parts)

    log.info(
        "ocr3_convert_complete",
        file=str(file_path),
        pages=len(pages),
        model=MISTRAL_OCR_MODEL,
        request_id=request_id,
        attempt_number=attempt_number,
    )

    return {
        "success": True,
        "markdown": combined_markdown,
        "ocr_model": MISTRAL_OCR_MODEL,
        "pages": len(pages),
        **ocr_metadata,
    }


async def convert_scanned_pdf(
    file_path: Path,
    language: str = "de",
    page_indices: Optional[list[int]] = None,
    request_id: Optional[str] = None,
    attempt_number: Optional[int] = None,
) -> dict[str, Any]:
    """
    Konvertiert ein eingescanntes PDF zu Markdown.

    Primär: Mistral OCR 3 API (/v1/ocr) — wenn MISTRAL_OCR_ENABLED=true.
    Fallback: Mistral Vision — Seiten werden als Bild gerendert und einzeln analysiert.

    Args:
        file_path: Pfad zur gescannten PDF-Datei.
        language: Sprache für den Vision-Prompt (Standard: "de", nur für Fallback relevant).
        page_indices: Optionale Liste von 0-basierten Seiten-Indices (z.B. [0, 2, 4]).
                      Wenn None, werden alle Seiten verarbeitet.

    Returns:
        Dict mit folgenden Schlüsseln:
        - success (bool)
        - markdown (str): Zusammengeführter Markdown-Text aller Seiten
        - scanned (bool): Immer True
        - pages_processed (int): Anzahl erfolgreich verarbeiteter Seiten (Vision-Pfad)
        - pages (int): Anzahl Seiten (OCR3-Pfad)
        - tokens_per_page (list[dict]): Token-Verbrauch pro Seite (Vision-Pfad)
        - tokens_total (int): Gesamter Token-Verbrauch (Vision-Pfad)
        - vision_model (str): Genutztes Vision-Modell (Vision-Pfad)
        - ocr_model (str): Genutztes OCR-Modell (OCR3-Pfad)
        - error_code / error: Nur bei Fehler
    """
    # Primärer Pfad: Mistral OCR 3
    if _get("MISTRAL_OCR_ENABLED", MISTRAL_OCR_ENABLED):
        log.info("scanned_pdf_using_ocr3", file=str(file_path), model=MISTRAL_OCR_MODEL, request_id=request_id, attempt_number=attempt_number)
        ocr3_result = await _get("convert_scanned_pdf_ocr3", convert_scanned_pdf_ocr3)(
            file_path,
            page_indices=page_indices,
            request_id=request_id,
            attempt_number=attempt_number,
        )
        if ocr3_result.get("success"):
            log.info("scanned_pdf_ocr3_success", file=str(file_path), pages=ocr3_result.get("pages", 0), request_id=request_id, attempt_number=attempt_number)
            return {
                "success": True,
                "markdown": ocr3_result["markdown"],
                "scanned": True,
                "pages_processed": ocr3_result.get("pages", 0),
                "ocr_model": ocr3_result.get("ocr_model", MISTRAL_OCR_MODEL),
                "ocr_table_format": ocr3_result.get("ocr_table_format"),
                "ocr_table_count": ocr3_result.get("ocr_table_count"),
                "ocr_headers": ocr3_result.get("ocr_headers"),
                "ocr_footers": ocr3_result.get("ocr_footers"),
                "ocr_confidence_granularity": ocr3_result.get("ocr_confidence_granularity"),
                "ocr_pages_with_confidence": ocr3_result.get("ocr_pages_with_confidence"),
                "ocr_average_page_confidence": ocr3_result.get("ocr_average_page_confidence"),
                "ocr_minimum_page_confidence": ocr3_result.get("ocr_minimum_page_confidence"),
            }
        else:
            log.warning(
                "scanned_pdf_ocr3_failed_fallback_to_vision",
                file=str(file_path),
                error=ocr3_result.get("error", "unknown"),
                request_id=request_id,
                attempt_number=attempt_number,
            )
            # Fallback: Vision-Pfad (weiter unten)
    else:
        log.info("scanned_pdf_ocr3_disabled_using_vision", file=str(file_path), request_id=request_id, attempt_number=attempt_number)

    # Fallback / direkter Pfad: Mistral Vision (pdf2image)
    if not _get("PDF2IMAGE_AVAILABLE", PDF2IMAGE_AVAILABLE):
        log.error("pdf2image_not_available", file=str(file_path), request_id=request_id, attempt_number=attempt_number)
        return {
            "success": False,
            "error_code": ErrorCode.CONVERSION_FAILED,
            "error": "pdf2image ist nicht installiert (pip install pdf2image)",
        }

    if not _get("MISTRAL_API_KEY", MISTRAL_API_KEY):
        return {
            "success": False,
            "error_code": ErrorCode.API_KEY_INVALID,
            "error": "MISTRAL_API_KEY nicht konfiguriert",
        }

    log.info("scanned_pdf_convert_start", file=str(file_path), request_id=request_id, attempt_number=attempt_number)

    try:
        pages = _get("convert_from_path", convert_from_path)(str(file_path), dpi=PDF_RENDER_DPI)
    except Exception as e:
        log.error("pdf2image_convert_failed", file=str(file_path), error=str(e), request_id=request_id, attempt_number=attempt_number)
        return {
            "success": False,
            "error_code": ErrorCode.CONVERSION_FAILED,
            "error": f"PDF-Rendering fehlgeschlagen: {str(e)}",
        }

    try:
        from templates_db import get_prompt as _get_prompt  # noqa: PLC0415
        from utils import _get as _utils_get  # noqa: PLC0415
        vision_prompt = _utils_get("get_prompt", _get_prompt)("vision", "scanned_pdf", language=language)
    except Exception:
        vision_prompt = (
            "Extrahiere den gesamten Text aus diesem Scan einer PDF-Seite und gib ihn als Markdown zurück.\n\n"
            "Regeln:\n"
            "- Behalte die Dokumentsprache bei — übersetze NICHT\n"
            "- Überschriften → # ## ### Markdown-Syntax\n"
            "- Tabellen → immer als Markdown-Tabelle mit | Spalte | Spalte | und Trennzeile\n"
            "- Listen → - oder 1. Markdown-Syntax\n"
            "- Fußnoten, Seitenzahlen und Kopfzeilen → kursiv in eckigen Klammern, z.B. *[Seite 3]*\n"
            "- Wenn eine Passage unleserlich ist → schreibe [UNLESERLICH]\n"
            "- Wenn die Seite keine Textinhalte enthält → antworte nur mit: [LEERE SEITE]\n\n"
            "Antworte ausschließlich mit dem Markdown-Text."
        )

    # Filter pages by page_indices if specified (0-based → enumerate starts at 1)
    if page_indices is not None:
        page_indices_set = set(page_indices)
        filtered_pages = [(idx, img) for idx, img in enumerate(pages) if idx in page_indices_set]
    else:
        filtered_pages = list(enumerate(pages))

    markdown_parts: list[str] = []
    tokens_per_page: list[dict] = []
    total_prompt_tokens = 0
    total_completion_tokens = 0
    total_tokens = 0
    pages_processed = 0
    vision_model_used = MISTRAL_VISION_MODEL

    total_filtered = len(filtered_pages)
    for i, (page_idx, page_image) in enumerate(filtered_pages):
        page_num = page_idx + 1
        log.info(
            "convert_progress",
            step="ocr",
            detail=f"page {i+1}/{total_filtered}",
            progress=int(10 + 50 * i / total_filtered) if total_filtered else 10,
            request_id=request_id,
            attempt_number=attempt_number,
        )
        # PIL Image → bytes (PNG)
        img_buffer = io.BytesIO()
        # Resize wenn nötig, um Tokens zu sparen
        if page_image.width > IMAGE_MAX_WIDTH:
            ratio = IMAGE_MAX_WIDTH / page_image.width
            new_height = int(page_image.height * ratio)
            page_image = page_image.resize((IMAGE_MAX_WIDTH, new_height), Image.Resampling.LANCZOS)

        page_image.save(img_buffer, format="PNG")
        image_bytes = img_buffer.getvalue()

        log.info(
            "scanned_pdf_page_start",
            file=str(file_path),
            page=page_num,
            total_pages=len(pages),
            image_size=len(image_bytes),
            request_id=request_id,
            attempt_number=attempt_number,
        )

        vision_result = await _get("analyze_with_mistral_vision", analyze_with_mistral_vision)(
            image_bytes,
            "image/png",
            vision_prompt,
            language,
            request_id=request_id,
            attempt_number=attempt_number,
            pipeline_step="scanned_pdf_vision_fallback",
            page=page_num,
            filename=file_path.name,
        )

        page_token_info = {
            "page": page_num,
            "tokens_prompt": vision_result.get("tokens_prompt", 0),
            "tokens_completion": vision_result.get("tokens_completion", 0),
            "tokens_total": vision_result.get("tokens_total", 0),
            "success": vision_result.get("success", False),
        }
        tokens_per_page.append(page_token_info)

        if vision_result.get("success"):
            pages_processed += 1
            total_prompt_tokens += vision_result.get("tokens_prompt", 0)
            total_completion_tokens += vision_result.get("tokens_completion", 0)
            total_tokens += vision_result.get("tokens_total", 0)
            vision_model_used = vision_result.get("vision_model", MISTRAL_VISION_MODEL)
            markdown_parts.append(f"## Seite {page_num}\n\n{vision_result['markdown']}")
            log.info(
                "scanned_pdf_page_done",
                file=str(file_path),
                page=page_num,
                tokens=vision_result.get("tokens_total", 0),
                request_id=request_id,
                attempt_number=attempt_number,
            )
        else:
            log.warning(
                "scanned_pdf_page_failed",
                file=str(file_path),
                page=page_num,
                error=vision_result.get("error", "unknown"),
                request_id=request_id,
                attempt_number=attempt_number,
            )
            markdown_parts.append(
                f"## Seite {page_num}\n\n*Seite konnte nicht verarbeitet werden: "
                f"{vision_result.get('error', 'Vision-Fehler')}*"
            )

    combined_markdown = "\n\n".join(markdown_parts)

    log.info(
        "scanned_pdf_convert_complete",
        file=str(file_path),
        pages_total=len(pages),
        pages_processed=pages_processed,
        tokens_total=total_tokens,
        request_id=request_id,
        attempt_number=attempt_number,
    )

    return {
        "success": True,
        "markdown": combined_markdown,
        "scanned": True,
        "pages_processed": pages_processed,
        "tokens_per_page": tokens_per_page,
        "tokens_prompt": total_prompt_tokens,
        "tokens_completion": total_completion_tokens,
        "tokens_total": total_tokens,
        "vision_model": vision_model_used,
    }


def extract_pdf_metadata(file_path: Path) -> dict[str, Any]:
    """
    Extrahiert PDF-Metadaten mit PyMuPDF (fitz).

    Liefert:
    - toc: Inhaltsverzeichnis/Bookmarks als Liste von [level, title, page].
    - annotations: Annotationen/Kommentare mit type, content, author, page.
    - form_fields: Formularfelder mit field_name, field_value, field_type, page.

    Wenn PyMuPDF nicht installiert ist, werden leere Listen zurückgegeben.

    Args:
        file_path: Pfad zur PDF-Datei.

    Returns:
        Dict mit Schlüsseln "toc", "annotations", "form_fields".
    """
    if not _get("PYMUPDF_AVAILABLE", PYMUPDF_AVAILABLE):
        log.debug("pymupdf_not_available", file=str(file_path))
        return {"toc": [], "annotations": [], "form_fields": []}

    toc: list[list[Any]] = []
    annotations: list[dict[str, Any]] = []
    form_fields: list[dict[str, Any]] = []

    try:
        doc = _get("fitz", fitz).open(str(file_path))

        # Bookmarks / Table of Contents
        toc = doc.get_toc()  # Returns list of [level, title, page]

        # Annotationen und Formularfelder seitenweise
        for page_num, page in enumerate(doc, start=1):
            # Annotationen
            for annot in page.annots():
                info = annot.info
                annot_entry: dict[str, Any] = {
                    "page": page_num,
                    "type": annot.type[1] if annot.type else "Unknown",
                    "content": info.get("content", ""),
                    "author": info.get("title", ""),
                }
                annotations.append(annot_entry)

            # Formularfelder (Widgets)
            for widget in page.widgets():
                field_entry: dict[str, Any] = {
                    "page": page_num,
                    "field_name": widget.field_name or "",
                    "field_value": str(widget.field_value) if widget.field_value is not None else "",
                    "field_type": widget.field_type_string or str(widget.field_type),
                }
                form_fields.append(field_entry)

        doc.close()
        log.debug(
            "pdf_metadata_extracted",
            file=str(file_path),
            toc_entries=len(toc),
            annotations=len(annotations),
            form_fields=len(form_fields),
        )

    except Exception as exc:
        log.warning("pdf_metadata_error", file=str(file_path), error=str(exc))

    return {"toc": toc, "annotations": annotations, "form_fields": form_fields}


def detect_zugferd(file_data: bytes) -> Optional[bytes]:
    """
    Prüft ob ein PDF eingebettete ZUGFeRD/Factur-X XML-Daten enthält.

    Öffnet das PDF mit PyMuPDF und prüft die eingebetteten Dateien (embfile_names)
    auf bekannte ZUGFeRD-Dateinamen. Gibt die XML-Bytes zurück wenn gefunden.

    Args:
        file_data: Rohe PDF-Bytes.

    Returns:
        XML-Bytes wenn ZUGFeRD-Anhang gefunden, sonst None.
    """
    if not _get("PYMUPDF_AVAILABLE", PYMUPDF_AVAILABLE):
        return None

    try:
        doc = _get("fitz", fitz).open(stream=file_data, filetype="pdf")
        embedded_names = doc.embfile_names()

        for name in embedded_names:
            # Case-insensitive Prüfung gegen bekannte ZUGFeRD-Dateinamen
            for zugferd_name in _ZUGFERD_FILENAMES:
                if name.lower() == zugferd_name.lower():
                    xml_info = doc.embfile_get(name)
                    doc.close()
                    # embfile_get gibt ein dict zurück mit 'content' (Bytes)
                    if isinstance(xml_info, dict):
                        return xml_info.get("content")
                    # Ältere PyMuPDF-Versionen können direkt Bytes zurückgeben
                    if isinstance(xml_info, bytes):
                        return xml_info
                    return None

        doc.close()
        return None

    except Exception as exc:
        log.warning("zugferd_detect_error", error=str(exc))
        return None


def extract_xmp_metadata(file_data: bytes) -> Optional[dict[str, Any]]:
    """
    Extrahiert XMP-Metadaten aus einem PDF.

    Kombiniert PyMuPDF doc.metadata (Basis-Infos) mit XMP-Stream-Daten:
    - pdf_a_level (aus pdfaid:part + pdfaid:conformance, z.B. "3B")
    - document_id (xmpMM:DocumentID)
    - instance_id (xmpMM:InstanceID)
    - creator_tool (xmp:CreatorTool)

    Args:
        file_data: Rohe PDF-Bytes.

    Returns:
        Dict mit Metadaten-Feldern oder None wenn PyMuPDF nicht verfügbar.
    """
    if not _get("PYMUPDF_AVAILABLE", PYMUPDF_AVAILABLE):
        return None

    try:
        doc = _get("fitz", fitz).open(stream=file_data, filetype="pdf")

        # Basis-Metadaten aus PyMuPDF
        base_meta = doc.metadata or {}
        result: dict[str, Any] = {}

        for key in ("title", "author", "subject", "creator", "producer", "creationDate", "modDate"):
            val = base_meta.get(key)
            if val:
                result[key] = val

        # XMP-Stream parsen
        xmp_xml = doc.get_xml_metadata()
        doc.close()

        if xmp_xml:
            try:
                from lxml import etree as ET  # noqa: PLC0415

                root = ET.fromstring(xmp_xml.encode("utf-8") if isinstance(xmp_xml, str) else xmp_xml)

                def _find_text(tag_local: str, ns_uri: str) -> Optional[str]:
                    """Sucht ein Element per Namespace-URI und lokalem Namen."""
                    for el in root.iter():
                        if el.tag == f"{{{ns_uri}}}{tag_local}":
                            return (el.text or "").strip() or None
                    return None

                NS_PDFAID = "http://www.aiim.org/pdfa/ns/id/"
                NS_XMPMM = "http://ns.adobe.com/xap/1.0/mm/"
                NS_XMP = "http://ns.adobe.com/xap/1.0/"

                part = _find_text("part", NS_PDFAID)
                conformance = _find_text("conformance", NS_PDFAID)
                if part:
                    result["pdf_a_level"] = part + (conformance or "")

                doc_id = _find_text("DocumentID", NS_XMPMM)
                if doc_id:
                    result["document_id"] = doc_id

                inst_id = _find_text("InstanceID", NS_XMPMM)
                if inst_id:
                    result["instance_id"] = inst_id

                creator_tool = _find_text("CreatorTool", NS_XMP)
                if creator_tool:
                    result["creator_tool"] = creator_tool

            except Exception as xmp_err:
                log.debug("xmp_parse_error", error=str(xmp_err))

        return result if result else None

    except Exception as exc:
        log.warning("xmp_metadata_extract_error", error=str(exc))
        return None


def list_embedded_files(file_data: bytes) -> list[dict[str, Any]]:
    """
    Listet alle in einem PDF eingebetteten Dateien auf.

    Gibt NICHT die ZUGFeRD-Dateien zurück (die werden von T-MKIT-024 separat
    behandelt). Verwendet PyMuPDF embfile_count(), embfile_names() und embfile_info().

    Args:
        file_data: Rohe PDF-Bytes.

    Returns:
        Liste von Dicts mit name, size, description. Leere Liste wenn keine Dateien
        eingebettet sind oder PyMuPDF nicht verfügbar ist.
    """
    if not _get("PYMUPDF_AVAILABLE", PYMUPDF_AVAILABLE):
        return []

    try:
        doc = _get("fitz", fitz).open(stream=file_data, filetype="pdf")
        names = doc.embfile_names()
        result: list[dict[str, Any]] = []

        for name in names:
            # ZUGFeRD-Dateien überspringen (werden von T-MKIT-024 separat behandelt)
            if name.lower() in _ZUGFERD_FILENAMES_SET:
                continue

            try:
                info = doc.embfile_info(name)
                entry: dict[str, Any] = {"name": name}
                if isinstance(info, dict):
                    size = info.get("size") or info.get("length")
                    if size is not None:
                        entry["size"] = size
                    desc = info.get("desc") or info.get("description")
                    if desc:
                        entry["description"] = desc
                result.append(entry)
            except Exception as info_err:
                log.debug("embfile_info_error", name=name, error=str(info_err))
                result.append({"name": name})

        doc.close()
        return result

    except Exception as exc:
        log.warning("list_embedded_files_error", error=str(exc))
        return []


def parse_zugferd_xml(xml_data: bytes) -> dict[str, Any]:
    """
    Parst ZUGFeRD/Factur-X XML und extrahiert strukturierte Rechnungsdaten.

    Unterstützt ZUGFeRD 2.x / Factur-X (Namespace CrossIndustryInvoice:100)
    und ZUGFeRD 1.x (ältere Namespaces). Alle Felder sind Optional — fehlendes
    XML liefert None-Werte statt Exceptions.

    Args:
        xml_data: ZUGFeRD/Factur-X XML als Bytes.

    Returns:
        Dict mit extrahierten Rechnungsfeldern (BT-Nummern nach EN 16931).
    """
    try:
        from lxml import etree as ET  # noqa: PLC0415
    except ImportError:
        log.warning("zugferd_lxml_not_available")
        return {"parse_error": "lxml not available"}

    result: dict[str, Any] = {
        "invoice_number": None,
        "invoice_date": None,
        "invoice_type": None,
        "seller_name": None,
        "seller_address": None,
        "seller_vat_id": None,
        "buyer_name": None,
        "buyer_address": None,
        "currency": None,
        "total_net": None,
        "total_vat": None,
        "total_gross": None,
        "due_amount": None,
        "payment_reference": None,
        "iban": None,
        "bic": None,
        "payment_terms": None,
        "due_date": None,
        "line_items": [],
    }

    try:
        root = ET.fromstring(xml_data)
    except Exception as exc:
        log.warning("zugferd_xml_parse_error", error=str(exc))
        result["parse_error"] = f"XML parse error: {str(exc)}"
        return result

    # Namespace-Erkennung: V2 oder V1?
    root_ns = root.nsmap.get("rsm") or ""
    if "CrossIndustryInvoice:100" in root_ns:
        NS = _ZUGFERD_NS_V2
    elif "ferd" in root_ns or "CrossIndustryDocument" in root_ns:
        NS = _ZUGFERD_NS_V1
    else:
        # Versuche V2 als Default
        NS = _ZUGFERD_NS_V2

    def _text(element, xpath: str) -> Optional[str]:
        """Extrahiert den Text des ersten XPath-Treffers oder None."""
        try:
            nodes = element.xpath(xpath, namespaces=NS)
            if nodes:
                node = nodes[0]
                text = node.text if hasattr(node, "text") else str(node)
                return text.strip() if text else None
        except Exception:
            pass
        return None

    def _build_address(party_element) -> Optional[str]:
        """Baut eine lesbare Adresse aus Adressfeldern zusammen."""
        try:
            addr = party_element.xpath(".//ram:PostalTradeAddress", namespaces=NS)
            if not addr:
                return None
            a = addr[0]
            parts = []
            for field in ["ram:LineOne", "ram:LineTwo", "ram:PostcodeCode", "ram:CityName", "ram:CountryID"]:
                val = _text(a, field)
                if val:
                    parts.append(val)
            return ", ".join(parts) if parts else None
        except Exception:
            return None

    try:
        # --- ExchangedDocument (Kopfdaten) ---
        doc_elem = root.find("rsm:ExchangedDocument", NS)
        if doc_elem is not None:
            result["invoice_number"] = _text(doc_elem, "ram:ID")
            result["invoice_type"] = _text(doc_elem, "ram:TypeCode")
            # Datum: BT-2 in DateTimeString Format 102 = YYYYMMDD
            raw_date = _text(doc_elem, "ram:IssueDateTime/udt:DateTimeString")
            if raw_date and len(raw_date) == 8:
                result["invoice_date"] = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
            elif raw_date:
                result["invoice_date"] = raw_date

        # --- SupplyChainTradeTransaction ---
        txn = root.find("rsm:SupplyChainTradeTransaction", NS)
        if txn is None:
            return result

        # --- Seller (BT-27) ---
        seller_party = txn.find(".//ram:SellerTradeParty", NS)
        if seller_party is not None:
            result["seller_name"] = _text(seller_party, "ram:Name")
            result["seller_address"] = _build_address(seller_party)
            # VAT-ID: BT-31 schemeID="VA"
            for tax_reg in seller_party.findall(".//ram:SpecifiedTaxRegistration", NS):
                scheme = tax_reg.find("ram:ID", NS)
                if scheme is not None and scheme.get("schemeID") == "VA":
                    result["seller_vat_id"] = scheme.text.strip() if scheme.text else None
                    break

        # --- Buyer (BT-44) ---
        buyer_party = txn.find(".//ram:BuyerTradeParty", NS)
        if buyer_party is not None:
            result["buyer_name"] = _text(buyer_party, "ram:Name")
            result["buyer_address"] = _build_address(buyer_party)

        # --- Settlement (Zahlungsinfos) ---
        settlement = txn.find(".//ram:ApplicableHeaderTradeSettlement", NS)
        if settlement is not None:
            result["currency"] = _text(settlement, "ram:InvoiceCurrencyCode")
            result["payment_reference"] = _text(settlement, "ram:PaymentReference")

            # Fälligkeitsdatum: BT-9
            raw_due = _text(settlement, ".//ram:DueDateDateTime/udt:DateTimeString")
            if raw_due and len(raw_due) == 8:
                result["due_date"] = f"{raw_due[:4]}-{raw_due[4:6]}-{raw_due[6:8]}"
            elif raw_due:
                result["due_date"] = raw_due

            # Zahlungsbedingungen: BT-20
            result["payment_terms"] = _text(settlement, ".//ram:SpecifiedTradePaymentTerms/ram:Description")

            # IBAN / BIC
            result["iban"] = _text(settlement, ".//ram:PayeePartyCreditorFinancialAccount/ram:IBANID")
            result["bic"] = _text(settlement, ".//ram:PayeeSpecifiedCreditorFinancialInstitution/ram:BICID")

            # Beträge: Monetary Summation
            summation = settlement.find(".//ram:SpecifiedTradeSettlementHeaderMonetarySummation", NS)
            if summation is not None:
                result["total_net"] = _text(summation, "ram:TaxBasisTotalAmount")
                result["total_gross"] = _text(summation, "ram:GrandTotalAmount")
                result["due_amount"] = _text(summation, "ram:DuePayableAmount")

            # Steuerbetrag: BT-110 (mit currencyID Attribut)
            tax_total_elem = settlement.find(".//ram:TaxTotalAmount", NS)
            if tax_total_elem is not None and tax_total_elem.text:
                result["total_vat"] = tax_total_elem.text.strip()

        # --- Line Items ---
        line_items = []
        for item in txn.findall(".//ram:IncludedSupplyChainTradeLineItem", NS):
            li: dict[str, Any] = {
                "description": None,
                "quantity": None,
                "unit_price": None,
                "total": None,
                "vat_rate": None,
            }
            # Beschreibung
            li["description"] = _text(item, ".//ram:SpecifiedTradeProduct/ram:Name")
            # Menge
            li["quantity"] = _text(item, ".//ram:BilledQuantity")
            # Einzelpreis
            li["unit_price"] = _text(item, ".//ram:ChargeAmount")
            # Zeilensumme
            li["total"] = _text(item, ".//ram:LineTotalAmount")
            # MwSt-Satz
            li["vat_rate"] = _text(item, ".//ram:ApplicableTradeTax/ram:RateApplicablePercent")
            line_items.append(li)

        result["line_items"] = line_items

    except Exception as exc:
        log.warning("zugferd_xml_extract_error", error=str(exc))
        result["parse_error"] = f"extraction error: {str(exc)}"

    return result


def prepend_pdf_toc(markdown: str, toc: list[list[Any]]) -> str:
    """
    Fügt ein Markdown-Inhaltsverzeichnis aus PDF-Bookmarks VOR dem Inhalt ein.

    Level-Mapping: Level 1 → ##, Level 2 → ###, Level 3 → ####, etc.

    Args:
        markdown: Bestehender Markdown-Text.
        toc: Liste von [level, title, page] aus fitz.Document.get_toc().

    Returns:
        Markdown mit vorangestelltem Inhaltsverzeichnis.
    """
    if not toc:
        return markdown

    lines = ["## Inhaltsverzeichnis", ""]
    for entry in toc:
        if len(entry) >= 3:
            level, title, page = entry[0], entry[1], entry[2]
        elif len(entry) == 2:
            level, title, page = entry[0], entry[1], None
        else:
            continue
        # Level 1 → ##, Level 2 → ###, ...
        prefix = "#" * (level + 1)
        page_str = f" *(Seite {page})*" if page and page > 0 else ""
        lines.append(f"{prefix} {title}{page_str}")

    toc_block = "\n".join(lines)
    return toc_block + "\n\n" + markdown


def append_pdf_annotations(markdown: str, annotations: list[dict[str, Any]]) -> str:
    """
    Hängt PDF-Annotationen als Blockquotes an den Markdown-Text an.

    Format:
    ## Annotationen
    > **Author** (Seite N, Typ): Content

    Args:
        markdown: Bestehender Markdown-Text.
        annotations: Liste von Dicts mit page, type, content, author.

    Returns:
        Markdown mit angehängter Annotationen-Sektion.
    """
    if not annotations:
        return markdown

    lines = ["## Annotationen", ""]
    for ann in annotations:
        author = ann.get("author", "")
        page = ann.get("page", "")
        ann_type = ann.get("type", "")
        content = ann.get("content", "")

        author_str = f"**{author}**" if author else "**Unbekannt**"
        meta_parts = []
        if page:
            meta_parts.append(f"Seite {page}")
        if ann_type:
            meta_parts.append(ann_type)
        meta_str = f" ({', '.join(meta_parts)})" if meta_parts else ""

        lines.append(f"> {author_str}{meta_str}: {content}")
        lines.append("")

    section = "\n".join(lines).rstrip()
    return markdown.rstrip() + "\n\n" + section


def append_pdf_form_fields(markdown: str, form_fields: list[dict[str, Any]]) -> str:
    """
    Hängt PDF-Formularfelder als Key-Value-Tabelle an den Markdown-Text an.

    Format:
    ## Formularfelder
    | Feld | Wert | Typ | Seite |
    |------|------|-----|-------|
    | name | wert | typ | 1     |

    Args:
        markdown: Bestehender Markdown-Text.
        form_fields: Liste von Dicts mit field_name, field_value, field_type, page.

    Returns:
        Markdown mit angehängter Formularfelder-Sektion.
    """
    if not form_fields:
        return markdown

    lines = [
        "## Formularfelder",
        "",
        "| Feld | Wert | Typ | Seite |",
        "|------|------|-----|-------|",
    ]
    for field in form_fields:
        name = field.get("field_name", "")
        value = field.get("field_value", "")
        ftype = field.get("field_type", "")
        page = field.get("page", "")
        lines.append(f"| {name} | {value} | {ftype} | {page} |")

    section = "\n".join(lines)
    return markdown.rstrip() + "\n\n" + section


def embed_ocr_in_pdf(
    file_data: bytes,
    ocr_text: str,
    pages_text: list[str] | None = None,
) -> bytes | None:
    """
    Embeds OCR text as an invisible text layer into a PDF, making it searchable.

    Args:
        file_data: Raw PDF bytes.
        ocr_text: Full OCR text to embed. Used when pages_text is None.
        pages_text: Optional per-page OCR text. If provided, each element is
                    embedded on its corresponding page. Falls back to ocr_text
                    distributed equally across pages when None.

    Returns:
        PDF bytes with embedded invisible text layer, or None on failure.
    """
    if not _get("PYMUPDF_AVAILABLE", PYMUPDF_AVAILABLE):
        log.warning("embed_ocr_in_pdf_skipped_no_pymupdf")
        return None
    try:
        doc = _get("fitz", fitz).open(stream=file_data, filetype="pdf")
        num_pages = len(doc)

        for page_num, page in enumerate(doc):
            # Determine text for this page
            if pages_text is not None and page_num < len(pages_text):
                page_text = pages_text[page_num]
            else:
                # Distribute full OCR text equally across pages
                if num_pages > 0:
                    chars_per_page = max(1, len(ocr_text) // num_pages)
                    start = page_num * chars_per_page
                    end = start + chars_per_page if page_num < num_pages - 1 else len(ocr_text)
                    page_text = ocr_text[start:end]
                else:
                    page_text = ocr_text

            if not page_text.strip():
                continue

            # Insert invisible text layer: render_mode=3 makes text invisible
            # but still selectable/searchable by PDF viewers and search engines.
            page.insert_text(
                _get("fitz", fitz).Point(0, 12),
                page_text,
                fontsize=1,
                render_mode=3,
            )

        result_bytes = doc.tobytes()
        doc.close()
        return result_bytes
    except Exception as e:
        log.warning("embed_ocr_in_pdf_failed", error=str(e))
        return None


# =============================================================================
# Hinweis: Funktionen die Mistral API aufrufen (call_mistral_ocr_api,
# call_mistral_vision_api, analyze_with_mistral_vision, strip_llm_artifacts)
# werden aus server importiert oder als Parameter übergeben.
# convert_scanned_pdf_ocr3 und convert_scanned_pdf rufen diese Funktionen auf
# und müssen daher im Kontext von server.py verwendet werden, wo diese
# Funktionen verfügbar sind.
# =============================================================================
