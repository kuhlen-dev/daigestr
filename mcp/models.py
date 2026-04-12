"""
Daigestr — Datenmodelle und Schemas
"""

from datetime import datetime, timezone
from typing import Any, Optional
from pydantic import BaseModel, Field, ConfigDict, field_validator


MINIMUM_META_FIELDS: dict[str, Any] = {
    "request_id": None,
    "job_id": None,
    "attempt_number": None,
    "attempt_count": None,
    "attempt_mode": None,
    "document_type": None,
    "document_type_confidence": None,
    "template_used": None,
    "template_version": None,
    "quality_score": None,
    "quality_grade": None,
    "accuracy_mode": None,
    "pipeline_steps": None,
    "retry_applied": None,
    "retry_reason": None,
    "initial_mode": None,
    "final_mode": None,
    "initial_quality_score": None,
    "final_quality_score": None,
    "retry_threshold_used": None,
}


SUCCESS_RESPONSE_DEFAULTS: dict[str, Any] = {
    "html": None,
    "extracted": None,
    "chunks": None,
    "enriched_pdf": None,
    "normalized": None,
    "normalized_version": None,
    "normalized_warnings": None,
    "normalized_trace": None,
    "normalized_context": None,
    "normalized_confidence": None,
}


# =============================================================================
# Basis-Schemas
# =============================================================================

class MetaData(BaseModel):
    """
    Metadaten-Container.

    Kombiniert:
    - Pass-through Daten vom Client (beliebige Key-Value-Paare)
    - Angereicherte Daten aus der Verarbeitung
    """
    model_config = ConfigDict(extra="allow")  # Erlaubt beliebige zusätzliche Felder

    # Automatisch hinzugefügte Felder
    source: Optional[str] = Field(None, description="Quelle (Dateipfad, URL, 'base64')")
    source_type: Optional[str] = Field(None, description="Typ: 'file', 'base64', 'url'")
    format: Optional[str] = Field(None, description="Dateiformat (pdf, jpg, docx, ...)")
    size_bytes: Optional[int] = Field(None, description="Dateigröße in Bytes")
    processed_at: Optional[str] = Field(None, description="Verarbeitungszeitpunkt (ISO 8601)")
    duration_ms: Optional[int] = Field(None, description="Verarbeitungsdauer in Millisekunden")
    request_id: Optional[str] = Field(None, description="Stabile ID für den gesamten Convert-Lauf über alle internen Versuche hinweg")
    job_id: Optional[str] = Field(None, description="Job-ID bei asynchronen Läufen")
    attempt_number: Optional[int] = Field(None, description="Nummer des internen Verarbeitungsversuchs innerhalb desselben request_id")
    attempt_count: Optional[int] = Field(None, description="Gesamtzahl der internen Versuche, die für die finale Antwort verwendet wurden")
    attempt_mode: Optional[str] = Field(None, description="Mode des Versuchs, der die finale Antwort erzeugt hat")

    # Bild-spezifisch
    width: Optional[int] = Field(None, description="Bildbreite in Pixel")
    height: Optional[int] = Field(None, description="Bildhöhe in Pixel")
    resized: Optional[bool] = Field(None, description="Wurde das Bild verkleinert?")
    original_width: Optional[int] = Field(None, description="Ursprüngliche Breite vor Resize")
    original_height: Optional[int] = Field(None, description="Ursprüngliche Höhe vor Resize")

    # Vision-spezifisch
    vision_used: Optional[bool] = Field(None, description="Wurde Mistral Vision verwendet?")
    vision_model: Optional[str] = Field(None, description="Verwendetes Vision-Modell")
    ocr_model: Optional[str] = Field(None, description="Verwendetes OCR-Modell (Mistral OCR 3)")
    ocr_table_format: Optional[str] = Field(None, description="OCR table extraction mode used for Mistral OCR ('markdown' or 'html')")
    ocr_table_count: Optional[int] = Field(None, description="Number of OCR tables returned by Mistral OCR")
    ocr_headers: Optional[list[str]] = Field(None, description="Distinct OCR headers returned when Mistral OCR header extraction is enabled")
    ocr_footers: Optional[list[str]] = Field(None, description="Distinct OCR footers returned when Mistral OCR footer extraction is enabled")
    ocr_confidence_granularity: Optional[str] = Field(None, description="Confidence granularity requested from Mistral OCR ('page' or 'word')")
    ocr_pages_with_confidence: Optional[int] = Field(None, description="Number of OCR pages that returned confidence information")
    ocr_average_page_confidence: Optional[float] = Field(None, description="Average OCR confidence across pages when confidence scores are enabled")
    ocr_minimum_page_confidence: Optional[float] = Field(None, description="Lowest OCR page confidence observed when confidence scores are enabled")
    tokens_prompt: Optional[int] = Field(None, description="Input-Tokens")
    tokens_completion: Optional[int] = Field(None, description="Output-Tokens")
    tokens_total: Optional[int] = Field(None, description="Gesamt-Tokens")

    # URL-spezifisch
    url: Optional[str] = Field(None, description="Quell-URL")
    content_type: Optional[str] = Field(None, description="Content-Type der URL")

    # PDF-spezifisch
    pages: Optional[int] = Field(None, description="Anzahl Seiten (PDF)")
    author: Optional[str] = Field(None, description="Autor (PDF)")
    title: Optional[str] = Field(None, description="Titel (PDF)")
    pages_requested: Optional[str] = Field(None, description="Angeforderter Seitenbereich (z.B. '1-3,!2')")

    # Embedded-Images-spezifisch
    images_described: Optional[int] = Field(None, description="Anzahl beschriebener eingebetteter Bilder")

    # Klassifizierungs-spezifisch
    document_type: Optional[str] = Field(None, description="Erkannter Dokumenttyp")
    document_type_confidence: Optional[float] = Field(None, description="Konfidenz der Klassifizierung (0.0-1.0)")

    # OCR-Korrektur-spezifisch
    ocr_corrected: Optional[bool] = Field(None, description="Wurde OCR-Nachkorrektur via LLM durchgeführt?")
    ocr_corrections_count: Optional[int] = Field(None, description="Anzahl der durch OCR-Korrektur behobenen Fehler")

    # Quality-Scoring-spezifisch (FR-MKIT-010)
    quality_score: Optional[float] = Field(None, description="Qualitäts-Score des konvertierten Textes (0.0-1.0)")
    quality_grade: Optional[str] = Field(None, description="Qualitäts-Bewertung: 'poor', 'fair', 'good', 'excellent'")

    # LLM-Hints (T-MKIT-032)
    hints: Optional[list[str]] = Field(None, description="Context-sensitive usage hints for LLM consumers")

    # High-Accuracy-Pipeline-spezifisch (T-MKIT-020)
    accuracy_mode: Optional[str] = Field(None, description="Verwendeter Accuracy-Modus: 'standard' oder 'high'")
    pipeline_steps: Optional[list[str]] = Field(None, description="Liste der durchgeführten Pipeline-Schritte (z.B. ['ocr', 'ocr_correction', 'dual_pass_validation', 'schema_extraction'])")
    retry_applied: Optional[bool] = Field(None, description="True when a low-quality retry escalation was executed.")
    retry_reason: Optional[str] = Field(None, description="Reason for the retry decision, e.g. 'low_quality' or 'missing_quality_score'.")
    initial_mode: Optional[str] = Field(None, description="Initial processing mode before retry evaluation.")
    final_mode: Optional[str] = Field(None, description="Final processing mode that produced the returned result.")
    initial_quality_score: Optional[float] = Field(None, description="Quality score from the initial pass before retry evaluation.")
    final_quality_score: Optional[float] = Field(None, description="Quality score from the final returned pass.")
    retry_threshold_used: Optional[float] = Field(None, description="Effective low-quality retry threshold used for the decision.")

    # Audio/Video-spezifisch (FR-MKIT-006)
    language: Optional[str] = Field(None, description="Erkannte Sprache (z.B. 'de', 'en') bei Audio/Video-Transkription")
    duration_seconds: Optional[float] = Field(None, description="Dauer der Audio/Video-Datei in Sekunden")
    whisper_model: Optional[str] = Field(None, description="Verwendete Whisper-Modell-Größe (z.B. 'base', 'small')")

    # ZUGFeRD/Factur-X E-Rechnung (T-MKIT-024)
    zugferd: Optional[dict] = Field(None, description="Structured ZUGFeRD/Factur-X e-invoice data extracted from embedded XML. Present when a ZUGFeRD-compliant PDF is detected.")

    # PDF XMP Metadata + Embedded Files (T-MKIT-025)
    xmp_metadata: Optional[dict] = Field(None, description="XMP metadata extracted from PDF (creator, title, subject, PDF/A level, document ID)")
    embedded_files: Optional[list[dict]] = Field(None, description="List of files embedded in the PDF (name, size, description)")

    # Excel Hidden Sheets (T-MKIT-026)
    hidden_sheets: Optional[list[str]] = Field(None, description="Names of hidden or very-hidden sheets found in Excel files")

    # Image EXIF/GPS/IPTC Metadata (T-MKIT-027)
    exif: Optional[dict] = Field(None, description="EXIF metadata from images (camera, date, GPS coordinates)")
    iptc: Optional[dict] = Field(None, description="IPTC metadata from images (caption, keywords, copyright)")

    # Office Document Properties (T-MKIT-028)
    document_properties: Optional[dict] = Field(None, description="Document properties extracted from Office files (core: author/dates, app: company/stats, custom: DMS metadata)")

    # Email Metadata (T-MKIT-029)
    email_routing: Optional[dict] = Field(None, description="Email routing metadata: Received chain, SPF/DKIM/DMARC results, originating IP")
    email_thread: Optional[dict] = Field(None, description="Email threading: Message-ID, In-Reply-To, References chain")
    calendar_events: Optional[list[dict]] = Field(None, description="Calendar events (ICS) found in email attachments")

    # PPTX Hidden Slides (T-MKIT-030)
    hidden_slides: Optional[int] = Field(None, description="Number of hidden slides found in PPTX")

    # Auto-Extract (T-MKIT-036)
    template_used: Optional[str] = Field(None, description="Template ID used for extraction (set by auto_extract or explicit template)")
    template_version: Optional[int] = Field(None, description="Version of the template used")

    # Request-Level-Cache (T-DAI-019)
    cached: Optional[bool] = Field(None, description="True wenn die Antwort aus dem Cache zurückgegeben wurde")


class ErrorDetail(BaseModel):
    """Fehlerdetails für einheitliche Fehlerantworten."""
    code: str = Field(..., description="Fehlercode (z.B. 'FILE_NOT_FOUND')")
    message: str = Field(..., description="Menschenlesbare Fehlermeldung")
    details: Optional[dict[str, Any]] = Field(None, description="Zusätzliche Fehlerdetails")


# =============================================================================
# Response-Schemas
# =============================================================================

class ConvertResponse(BaseModel):
    """
    Einheitliche Antwort für alle Konvertierungen.

    Bei Erfolg:
        success=True, markdown=<content>, meta=<enriched>

    Bei Fehler:
        success=False, error=<ErrorDetail>, meta=<pass-through>
    """
    success: bool = Field(..., description="War die Konvertierung erfolgreich?")
    markdown: Optional[str] = Field(None, description="Konvertierter Markdown-Inhalt")
    html: Optional[str] = Field(None, description="Konvertierter HTML-Inhalt (nur wenn output_format='html')")
    error: Optional[ErrorDetail] = Field(None, description="Fehlerdetails (nur bei success=False)")
    meta: MetaData = Field(default_factory=MetaData, description="Metadaten")
    extracted: Optional[dict[str, Any]] = Field(None, description="Strukturiert extrahierte Daten (nur wenn extract_schema gesetzt)")
    chunks: Optional[list[dict[str, Any]]] = Field(None, description="RAG-Chunks (nur wenn chunk=True gesetzt, FR-MKIT-011)")
    enriched_pdf: Optional[str] = Field(None, description="Base64-encoded searchable PDF with embedded OCR text layer. Only present when ocr_embed=true and the document is a scanned PDF.")

    # Normalisierungs-Felder (T-DAI-055)
    normalized: Optional[dict[str, Any]] = Field(None, description="Normalisierte Felder aus der Extraktion (nur wenn Template verwendet und Normalisierungs-Mapping vorhanden)")
    normalized_version: Optional[str] = Field(None, description="Version-Hash der Normalisierungs-Regeln")
    normalized_warnings: Optional[list[str]] = Field(None, description="Warnungen aus der Normalisierung (fehlende Felder, Validierungsfehler, etc.)")
    normalized_trace: Optional[list[dict[str, Any]]] = Field(None, description="Trace-Log der Normalisierungsschritte (Feld → Regel → Konfidenz)")
    normalized_context: Optional[dict[str, Any]] = Field(None, description="Kontext aus der Normalisierung (vendor_country, recipient_country, tax_country)")
    normalized_confidence: Optional[dict[str, float]] = Field(None, description="Confidence scores per normalized field (0.0-1.0)")


class HealthResponse(BaseModel):
    """Antwort für Health-Check Endpoint."""
    status: str = Field(..., description="Status: 'ok' oder 'error'")
    version: str = Field(..., description="Server-Version")
    meta: dict[str, Any] = Field(default_factory=dict, description="Zusätzliche Infos")


class ProgressState(BaseModel):
    """Kanonischer Progress-Status für laufende Jobs und Sync/Async-Pfade."""
    status: str = Field("processing", description="Jobstatus für den aktuellen Progress-Snapshot")
    current_stage: str = Field(..., description="Aktueller Verarbeitungsschritt, z. B. 'ocr' oder 'extract'")
    message: Optional[str] = Field(None, description="Freitext-Detail zum aktuellen Schritt")
    percent: Optional[int] = Field(None, ge=0, le=100, description="Fortschritt in Prozent sofern bekannt")
    request_id: Optional[str] = Field(None, description="Stabile ID des Convert-Laufs")
    job_id: Optional[str] = Field(None, description="Job-ID bei asynchronen Läufen")
    attempt_number: Optional[int] = Field(None, description="Aktuelle interne Versuchszahl")
    attempt_count: Optional[int] = Field(None, description="Bekannte Gesamtzahl interner Versuche")
    attempt_mode: Optional[str] = Field(None, description="Mode des aktuellen Versuchs")
    page_current: Optional[int] = Field(None, description="Aktuelle Seite innerhalb eines längeren Dokuments")
    page_total: Optional[int] = Field(None, description="Gesamtseitenzahl sofern bekannt")
    upstream_attempt: Optional[int] = Field(None, description="Retry-Zähler des aktuellen Upstream-Calls sofern bekannt")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Zusätzlicher Progress-Kontext wie Dateiname oder Quelle")


class AsyncJobStartResponse(BaseModel):
    """Antwort nach dem Start eines asynchronen Convert-Jobs."""
    job_id: str = Field(..., description="ID des gestarteten Jobs")
    status: str = Field(..., description="Initialer Jobstatus, typischerweise 'queued'")


class JobStatusResponse(BaseModel):
    """Pollbarer Status für asynchrone Convert-Jobs."""
    job_id: str = Field(..., description="ID des Jobs")
    status: str = Field(..., description="Aktueller Jobstatus")
    created_at: Any = Field(..., description="Erstellungszeitpunkt des Jobs")
    updated_at: Any = Field(..., description="Letzte Statusänderung des Jobs")
    progress: Optional[ProgressState] = Field(None, description="Kanonischer Progress-Snapshot oder null")


class JobListResponse(BaseModel):
    """Liste pollbarer Jobstatus-Einträge."""
    jobs: list[JobStatusResponse] = Field(default_factory=list, description="Neueste Jobs zuerst")


# =============================================================================
# Request-Schemas
# =============================================================================

class ConvertRequest(BaseModel):
    """
    Einheitlicher Request für Konvertierungen.

    Unterstützt drei Eingabe-Modi (genau einer muss gesetzt sein):
    - path: Dateipfad im Container
    - base64 + filename: Base64-kodierte Datei
    - url: URL zu Datei/Webseite
    """
    model_config = ConfigDict(str_strip_whitespace=True)

    # Eingabe-Modi (einer von dreien)
    path: Optional[str] = Field(None, description="Dateipfad (relativ zu /data oder absolut)")
    base64: Optional[str] = Field(None, description="Base64-kodierte Datei")
    filename: Optional[str] = Field(None, description="Dateiname (erforderlich bei base64)")
    url: Optional[str] = Field(None, description="URL zu Datei oder Webseite")

    # Optionen
    output_format: str = Field("markdown", description="Ausgabeformat: 'markdown', 'text', 'html'")

    @field_validator("output_format")
    @classmethod
    def validate_output_format(cls, v: str) -> str:
        if v not in ("markdown", "html", "text"):
            raise ValueError(f"output_format must be 'markdown', 'html', or 'text', got '{v}'")
        return v

    # Vision-spezifisch
    prompt: Optional[str] = Field(None, description="Custom Prompt für Vision-Analyse")
    language: str = Field("de", description="Sprache für Vision-Antwort: 'de', 'en'")

    # Embedded-Images-Option
    describe_images: bool = Field(False, description="When true, extracts embedded images from DOCX/PPTX and describes them via Mistral Vision. Replaces [image] placeholders with actual descriptions. Costs additional API calls.")

    # Klassifizierungs-Option
    classify: bool = Field(False, description="When true, detects the document type (invoice, contract, cv, etc.) and returns it in meta.document_type with a confidence score.")
    classify_categories: Optional[list[str]] = Field(None, description="Custom Klassifizierungs-Kategorien")

    # Extraktion
    extract_schema: Optional[dict[str, Any]] = Field(None, description="JSON Schema for structured data extraction. Populates the 'extracted' field when provided. Alternatives: use the 'template' parameter or auto_extract=true.")
    template: Optional[str] = Field(None, description="Predefined extraction template name from the live template registry. See GET /v1/templates for available templates and schemas. Alternative to extract_schema.")

    # OCR-Korrektur
    ocr_correct: bool = Field(False, description="When true, runs an LLM post-correction pass on OCR output to fix common recognition errors. Automatically enabled when accuracy='high'.")

    # Accuracy-Modus (T-MKIT-020)
    accuracy: str = Field("standard", description="Processing accuracy mode. 'standard' (default): single-pass conversion. 'high': multi-stage pipeline with OCR correction and dual-pass vision validation — recommended for scanned documents.")

    @field_validator("accuracy")
    @classmethod
    def validate_accuracy(cls, v: str) -> str:
        if v not in ("standard", "high"):
            raise ValueError(f"accuracy must be 'standard' or 'high', got '{v}'")
        return v

    # Excel-spezifisch (FR-MKIT-007)
    show_formulas: bool = Field(False, description="When true, Excel formula cells display as '42 [=SUM(A1:A10)]' instead of just the computed value.")

    # Smart Chunking (FR-MKIT-011)
    chunk: bool = Field(False, description="When true, splits the Markdown output into RAG-ready chunks returned in the 'chunks' field. Without this, chunks is always null.")
    chunk_size: int = Field(512, ge=1, description="Approximate chunk size in tokens (heuristic: characters / 4). Only used when chunk=true.")

    # OCR Embed (T-MKIT-033)
    ocr_embed: bool = Field(False, description="When true and the document is a scanned PDF, embeds the OCR text as an invisible text layer into the PDF, making it searchable. The enriched PDF is returned as base64 in the enriched_pdf field.")

    # Auto-Extract (T-MKIT-036)
    auto_extract: bool = Field(False, description="When true, automatically classifies the document, looks up a matching template from the registry, and extracts structured data — all in one call. No need to specify template or extract_schema.")
    min_confidence: float = Field(0.7, ge=0.0, le=1.0, description="Minimum classification confidence for auto_extract to use a template. Below this threshold, only markdown is returned without extraction.")

    # Low-Quality Escalation (E10/W10.3)
    retry_on_low_quality: Optional[bool] = Field(None, description="Override the server-side low-quality retry setting. true enables a deterministic second pass when the first quality score is too low or unavailable. null = use env default.")
    quality_retry_threshold: Optional[float] = Field(None, ge=0.0, le=1.0, description="Optional quality-score threshold for low-quality retry escalation. Null = use env default.")
    quality_retry_mode: Optional[str] = Field(None, description="Override the retry mode used for low-quality retry escalation. Null = use env default. Currently only 'full' is supported.")

    @field_validator("quality_retry_mode")
    @classmethod
    def validate_quality_retry_mode(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v != "full":
            raise ValueError(f"quality_retry_mode must be 'full', got '{v}'")
        return v

    # Processing Mode (T-DAI-015)
    mode: str = Field("default", description="Processing mode. 'default': use individual parameter settings. 'full': enable all features with page-rendering for PDFs (describe_pages, accuracy=high, classify, ocr_correct, auto_extract, chunk). 'deep': like full, plus per-image extraction with classification (diagram→Mermaid, chart→table, photo→description).")

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        if v not in ("default", "full", "deep"):
            raise ValueError(f"mode must be 'default', 'full' or 'deep', got '{v}'")
        return v

    # PDF Page Selection (T-DAI-025)
    pages: Optional[str] = Field(None, description="Page selection for PDFs. Syntax: '1-3', '7,14,22', '10-20,!15'. Null = all pages.")

    # Meta Pass-through
    meta: dict[str, Any] = Field(default_factory=dict, description="Beliebige Metadaten (werden durchgereicht)")

    # Webhook (T-DAI-023)
    webhook_url: Optional[str] = Field(None, description="URL to POST the result to when conversion completes")

    # Cache control
    no_cache: bool = Field(False, description="When true, bypass cache and force fresh conversion. Result is still cached for future requests.")

    # Normalisierung (T-DAI-055)
    compact: bool = Field(False, description="When true, removes null fields from the normalized output to reduce response size.")


class ExtractRequest(BaseModel):
    """
    Request für Konvertierung + strukturierte Extraktion in einem Schritt.

    Unterstützt drei Eingabe-Modi (genau einer muss gesetzt sein):
    - path: Dateipfad im Container
    - base64 + filename: Base64-kodierte Datei
    - url: URL zu Datei/Webseite

    extract_schema ist required; alternativ kann ein vordefinierter template-Name
    angegeben werden.
    """
    model_config = ConfigDict(str_strip_whitespace=True)

    # Eingabe-Modi (einer von dreien)
    path: Optional[str] = Field(None, description="Dateipfad (relativ zu /data oder absolut)")
    base64: Optional[str] = Field(None, description="Base64-kodierte Datei")
    filename: Optional[str] = Field(None, description="Dateiname (erforderlich bei base64)")
    url: Optional[str] = Field(None, description="URL zu Datei oder Webseite")

    # Extraktion (eines von beiden erforderlich, oder auto_extract=true)
    extract_schema: Optional[dict[str, Any]] = Field(None, description="JSON Schema for structured data extraction. Populates the 'extracted' field when provided. Alternatives: use the 'template' parameter or auto_extract=true.")
    template: Optional[str] = Field(None, description="Predefined extraction template name from the live template registry. See GET /v1/templates for available templates and schemas. Alternative to extract_schema.")

    # Auto-Extract (T-MKIT-036)
    auto_extract: bool = Field(False, description="When true, automatically classifies the document, looks up a matching template from the registry, and extracts structured data — all in one call. No need to specify template or extract_schema.")
    min_confidence: float = Field(0.7, ge=0.0, le=1.0, description="Minimum classification confidence for auto_extract to use a template. Below this threshold, only markdown is returned without extraction.")

    # Low-Quality Escalation (E10/W10.3)
    retry_on_low_quality: Optional[bool] = Field(None, description="Override the server-side low-quality retry setting. true enables a deterministic second pass when the first quality score is too low or unavailable. null = use env default.")
    quality_retry_threshold: Optional[float] = Field(None, ge=0.0, le=1.0, description="Optional quality-score threshold for low-quality retry escalation. Null = use env default.")
    quality_retry_mode: Optional[str] = Field(None, description="Override the retry mode used for low-quality retry escalation. Null = use env default. Currently only 'full' is supported.")

    @field_validator("quality_retry_mode")
    @classmethod
    def validate_extract_quality_retry_mode(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v != "full":
            raise ValueError(f"quality_retry_mode must be 'full', got '{v}'")
        return v

    # Optionen
    output_format: str = Field("markdown", description="Ausgabeformat: 'markdown', 'text', 'html'")

    @field_validator("output_format")
    @classmethod
    def validate_extract_output_format(cls, v: str) -> str:
        if v not in ("markdown", "html", "text"):
            raise ValueError(f"output_format must be 'markdown', 'html', or 'text', got '{v}'")
        return v

    # Vision-spezifisch
    prompt: Optional[str] = Field(None, description="Custom Prompt für Vision-Analyse")
    language: str = Field("de", description="Sprache für Vision-Antwort: 'de', 'en'")

    # Accuracy-Modus (T-MKIT-022)
    accuracy: str = Field("standard", description="Processing accuracy mode. 'standard' (default): single-pass conversion. 'high': multi-stage pipeline with OCR correction and dual-pass vision validation — recommended for scanned documents.")

    @field_validator("accuracy")
    @classmethod
    def validate_accuracy(cls, v: str) -> str:
        if v not in ("standard", "high"):
            raise ValueError(f"accuracy must be 'standard' or 'high', got '{v}'")
        return v

    # OCR-Korrektur (T-MKIT-022)
    ocr_correct: bool = Field(False, description="When true, runs an LLM post-correction pass on OCR output to fix common recognition errors. Automatically enabled when accuracy='high'.")

    # Vision-Optionen (T-MKIT-022)
    describe_images: bool = Field(False, description="When true, extracts embedded images from DOCX/PPTX and describes them via Mistral Vision. Replaces [image] placeholders with actual descriptions. Costs additional API calls.")

    # Klassifizierung (T-MKIT-022)
    classify: bool = Field(False, description="When true, detects the document type (invoice, contract, cv, etc.) and returns it in meta.document_type with a confidence score.")
    classify_categories: Optional[list[str]] = Field(None, description="Custom Klassifizierungs-Kategorien")

    # Smart Chunking
    chunk: bool = Field(False, description="When true, splits the Markdown output into RAG-ready chunks returned in the 'chunks' field.")
    chunk_size: int = Field(512, ge=1, description="Approximate chunk size in tokens (heuristic: characters / 4). Only used when chunk=true.")

    # OCR Embed
    ocr_embed: bool = Field(False, description="When true and the document is a scanned PDF, embeds the OCR text as an invisible text layer into the PDF, making it searchable.")

    # Excel-spezifisch
    show_formulas: bool = Field(False, description="When true, Excel formula cells display as '42 [=SUM(A1:A10)]' instead of just the computed value.")

    # Processing Mode
    mode: str = Field("default", description="Processing mode. 'default': use individual parameter settings. 'full': enable all features with page-rendering for PDFs. 'deep': like full, plus per-image extraction.")

    @field_validator("mode")
    @classmethod
    def validate_extract_mode(cls, v: str) -> str:
        if v not in ("default", "full", "deep"):
            raise ValueError(f"mode must be 'default', 'full' or 'deep', got '{v}'")
        return v

    # PDF Page Selection
    pages: Optional[str] = Field(None, description="Page selection for PDFs. Syntax: '1-3', '7,14,22', '10-20,!15'. Null = all pages.")

    # Meta Pass-through
    meta: dict[str, Any] = Field(default_factory=dict, description="Beliebige Metadaten (werden durchgereicht)")

    # Webhook
    webhook_url: Optional[str] = Field(None, description="URL to POST the result to when conversion completes")

    # Cache control
    no_cache: bool = Field(False, description="When true, bypass cache and force fresh conversion. Result is still cached for future requests.")

    # Normalisierung
    compact: bool = Field(False, description="When true, removes null fields from the normalized output to reduce response size.")


class TemplateResponse(BaseModel):
    """Antwort für /v1/templates Endpoint."""
    templates: dict[str, dict[str, Any]] = Field(..., description="Vordefinierte Extraktions-Templates")


class ConvertFolderRequest(BaseModel):
    """Request für Ordner-Konvertierung."""
    model_config = ConfigDict(str_strip_whitespace=True)

    path: str = Field(..., description="Ordnerpfad (relativ zu /data oder absolut)")
    language: str = Field(default="de", description="Sprache für Vision-Antwort: 'de', 'en'")
    meta: dict[str, Any] = Field(default_factory=dict, description="Beliebige Metadaten")

    # Optionen (analog zu ConvertRequest)
    describe_images: bool = Field(False, description="Eingebettete Bilder in DOCX/PPTX/PDF/ODT/ODP/HTML via Mistral Vision beschreiben.")
    classify: bool = Field(False, description="Dokumenttyp via LLM klassifizieren.")
    classify_categories: Optional[list[str]] = Field(None, description="Erlaubte Klassifizierungs-Kategorien.")
    extract_schema: Optional[dict[str, Any]] = Field(None, description="JSON Schema für strukturierte Daten-Extraktion.")
    auto_extract: bool = Field(False, description="Automatisch klassifizieren, Template suchen und Daten extrahieren.")
    accuracy: str = Field("standard", description="Accuracy-Modus: 'standard' oder 'high'.")
    chunk: bool = Field(False, description="Smart Chunking für RAG aktivieren.")
    chunk_size: int = Field(512, ge=1, description="Chunk-Größe in Tokens.")
    ocr_correct: bool = Field(False, description="OCR-Nachkorrektur via LLM aktivieren.")
    ocr_embed: bool = Field(False, description="OCR-Text als Textschicht in gescannte PDFs einbetten.")
    show_formulas: bool = Field(False, description="Excel-Formeln im Output annotieren.")
    prompt: Optional[str] = Field(None, description="Custom Prompt für Vision-Analyse.")
    template: Optional[str] = Field(None, description="Vordefinierter Template-Name als Alternative zu extract_schema.")
    min_confidence: float = Field(0.7, ge=0.0, le=1.0, description="Minimale Klassifizierungs-Konfidenz für auto_extract.")
    mode: Optional[str] = Field(None, description="Optionaler Modus-Hint für zukünftige Erweiterungen.")


class AnalyzeRequest(BaseModel):
    """Request für explizite Vision-Analyse (Bilder)."""
    model_config = ConfigDict(str_strip_whitespace=True)

    # Eingabe-Modi
    path: Optional[str] = Field(None, description="Bildpfad")
    base64: Optional[str] = Field(None, description="Base64-kodiertes Bild")
    filename: Optional[str] = Field(None, description="Dateiname (bei base64)")

    # Vision-Optionen
    prompt: str = Field(
        "Beschreibe dieses Bild detailliert. Erfasse alle wichtigen visuellen Elemente, Text, Diagramme oder Informationen.",
        description="Prompt für die Bildanalyse"
    )
    language: str = Field("de", description="Sprache: 'de' oder 'en'")

    # Meta
    meta: dict[str, Any] = Field(default_factory=dict, description="Beliebige Metadaten")


# =============================================================================
# Error Codes
# =============================================================================

class ErrorCode:
    """Definierte Fehlercodes für einheitliche Fehlerbehandlung."""

    # Input-Fehler
    FILE_NOT_FOUND = "FILE_NOT_FOUND"
    INVALID_INPUT = "INVALID_INPUT"
    UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    INVALID_BASE64 = "INVALID_BASE64"

    # Verarbeitungsfehler
    CONVERSION_FAILED = "CONVERSION_FAILED"
    VISION_FAILED = "VISION_FAILED"
    TIMEOUT = "TIMEOUT"

    # API-Fehler
    API_ERROR = "API_ERROR"
    API_RATE_LIMIT = "API_RATE_LIMIT"
    API_KEY_INVALID = "API_KEY_INVALID"

    # Server-Fehler
    INTERNAL_ERROR = "INTERNAL_ERROR"


# =============================================================================
# Helper Functions
# =============================================================================

def create_error_response(
    code: str,
    message: str,
    meta: Optional[dict[str, Any]] = None,
    details: Optional[dict[str, Any]] = None
) -> ConvertResponse:
    """Erstellt eine einheitliche Fehlerantwort."""
    return ConvertResponse(
        success=False,
        error=ErrorDetail(code=code, message=message, details=details),
        meta=MetaData(**(meta or {}))
    )


def create_success_response(
    markdown: str,
    meta: Optional[dict[str, Any]] = None
) -> ConvertResponse:
    """Erstellt eine einheitliche Erfolgsantwort."""
    enriched_meta = {**MINIMUM_META_FIELDS, **(meta or {})}
    enriched_meta["processed_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    return ConvertResponse(
        success=True,
        markdown=markdown,
        meta=MetaData(**enriched_meta),
        **SUCCESS_RESPONSE_DEFAULTS,
    )
