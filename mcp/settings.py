"""
Daigestr — Konfiguration / Settings

Alle Umgebungsvariablen und Konstanten aus server.py extrahiert.
"""

import os
import time
from pathlib import Path
from typing import Any


def _optional_positive_float_env(name: str) -> float | None:
    raw = os.getenv(name, "").strip()
    if raw == "":
        return None
    value = float(raw)
    if value <= 0:
        return None
    return value

# =============================================================================
# Verzeichnisse
# =============================================================================

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
TEMP_DIR = Path(os.getenv("TEMP_DIR", "/tmp/markitdown"))
TEMP_DIR.mkdir(parents=True, exist_ok=True)
ALLOWED_PATH_ROOTS = tuple(
    Path(raw.strip()).resolve(strict=False)
    for raw in os.getenv("ALLOWED_PATH_ROOTS", str(DATA_DIR)).split(":")
    if raw.strip()
)
ALLOW_SYMLINK_PATHS = os.getenv("ALLOW_SYMLINK_PATHS", "false").lower() == "true"

# Template Registry DB (T-MKIT-035)
TEMPLATES_DB_PATH = Path(os.getenv("TEMPLATES_DB_PATH", str(DATA_DIR / "templates.db")))

# =============================================================================
# Mistral Vision
# =============================================================================

MISTRAL_API_KEY = os.getenv("MISTRAL_API_KEY", "")
MISTRAL_API_URL = os.getenv("MISTRAL_API_URL", "https://api.mistral.ai/v1")
MISTRAL_VISION_MODEL = os.getenv("MISTRAL_VISION_MODEL", "mistral-small-2603")
MISTRAL_TIMEOUT = int(os.getenv("MISTRAL_TIMEOUT", "120"))

# Mistral OCR 3
MISTRAL_OCR_MODEL = os.getenv("MISTRAL_OCR_MODEL", "mistral-ocr-2512")
MISTRAL_OCR_ENABLED = os.getenv("MISTRAL_OCR_ENABLED", "true").lower() == "true"
_ocr_table_format = os.getenv("MISTRAL_OCR_TABLE_FORMAT", "").strip().lower()
MISTRAL_OCR_TABLE_FORMAT = _ocr_table_format or None
if MISTRAL_OCR_TABLE_FORMAT not in {None, "markdown", "html"}:
    raise ValueError("MISTRAL_OCR_TABLE_FORMAT must be empty, 'markdown', or 'html'")
MISTRAL_OCR_INCLUDE_IMAGE_BASE64 = os.getenv("MISTRAL_OCR_INCLUDE_IMAGE_BASE64", "false").lower() == "true"
_ocr_document_annotation_format = os.getenv("MISTRAL_OCR_DOCUMENT_ANNOTATION_FORMAT", "").strip().lower()
MISTRAL_OCR_DOCUMENT_ANNOTATION_FORMAT = _ocr_document_annotation_format or None
if MISTRAL_OCR_DOCUMENT_ANNOTATION_FORMAT not in {None, "text"}:
    raise ValueError("MISTRAL_OCR_DOCUMENT_ANNOTATION_FORMAT must be empty or 'text'")
_ocr_bbox_annotation_format = os.getenv("MISTRAL_OCR_BBOX_ANNOTATION_FORMAT", "").strip().lower()
MISTRAL_OCR_BBOX_ANNOTATION_FORMAT = _ocr_bbox_annotation_format or None
if MISTRAL_OCR_BBOX_ANNOTATION_FORMAT not in {None, "text"}:
    raise ValueError("MISTRAL_OCR_BBOX_ANNOTATION_FORMAT must be empty or 'text'")
MISTRAL_OCR_EXTRACT_HEADER = os.getenv("MISTRAL_OCR_EXTRACT_HEADER", "false").lower() == "true"
MISTRAL_OCR_EXTRACT_FOOTER = os.getenv("MISTRAL_OCR_EXTRACT_FOOTER", "false").lower() == "true"
MISTRAL_OCR_CONFIDENCE_GRANULARITY = os.getenv("MISTRAL_OCR_CONFIDENCE_GRANULARITY", "page").strip().lower()
if MISTRAL_OCR_CONFIDENCE_GRANULARITY not in {"none", "page", "word"}:
    raise ValueError("MISTRAL_OCR_CONFIDENCE_GRANULARITY must be one of: none, page, word")
MISTRAL_BATCH_ENABLED = os.getenv("MISTRAL_BATCH_ENABLED", "false").lower() == "true"
MISTRAL_BATCH_MIN_ITEMS = int(os.getenv("MISTRAL_BATCH_MIN_ITEMS", "10"))
if MISTRAL_BATCH_MIN_ITEMS < 1:
    raise ValueError("MISTRAL_BATCH_MIN_ITEMS must be >= 1")
MISTRAL_BATCH_POLL_INTERVAL_SECONDS = float(os.getenv("MISTRAL_BATCH_POLL_INTERVAL_SECONDS", "5"))
if MISTRAL_BATCH_POLL_INTERVAL_SECONDS <= 0:
    raise ValueError("MISTRAL_BATCH_POLL_INTERVAL_SECONDS must be > 0")
MISTRAL_BATCH_MAX_ACTIVE = int(os.getenv("MISTRAL_BATCH_MAX_ACTIVE", "25"))
if MISTRAL_BATCH_MAX_ACTIVE < 1:
    raise ValueError("MISTRAL_BATCH_MAX_ACTIVE must be >= 1")
MISTRAL_BATCH_TIMEOUT_HOURS = int(os.getenv("MISTRAL_BATCH_TIMEOUT_HOURS", "24"))
if MISTRAL_BATCH_TIMEOUT_HOURS < 1:
    raise ValueError("MISTRAL_BATCH_TIMEOUT_HOURS must be >= 1")
MISTRAL_BATCH_ALLOWED_SOURCE_TYPES = tuple(
    raw.strip().lower()
    for raw in os.getenv("MISTRAL_BATCH_ALLOWED_SOURCE_TYPES", "file,base64,url").split(",")
    if raw.strip()
)
if not MISTRAL_BATCH_ALLOWED_SOURCE_TYPES:
    raise ValueError("MISTRAL_BATCH_ALLOWED_SOURCE_TYPES must contain at least one source type")
# =============================================================================
# Server Ports
# =============================================================================

MCP_PORT = int(os.getenv("MCP_PORT", "8080"))
MCP_TRANSPORT = os.getenv("MCP_TRANSPORT", "sse")
REST_PORT = int(os.getenv("REST_PORT", "8081"))
BIND_HOST = os.getenv("BIND_HOST", "0.0.0.0")
MCP_HOST_BIND = os.getenv("MCP_HOST_BIND", BIND_HOST)
MCP_HOST_PORT = int(os.getenv("MCP_HOST_PORT", "18005"))
REST_HOST_BIND = os.getenv("REST_HOST_BIND", BIND_HOST)
REST_HOST_PORT = int(os.getenv("REST_HOST_PORT", "18006"))

# =============================================================================
# Limits
# =============================================================================

MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "25"))
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
IMAGE_MAX_WIDTH = int(os.getenv("IMAGE_MAX_WIDTH", "2048"))

# Max. Bilder die bei describe_images beschrieben werden (FIX 3 — T-DAI-024)
MAX_DESCRIBE_IMAGES = int(os.getenv("MAX_DESCRIBE_IMAGES", "50"))

# Max. Seiten die bei describe_pages gerendert werden (T-DAI-030)
PAGE_DESCRIBE_MAX_PAGES = int(os.getenv("PAGE_DESCRIBE_MAX_PAGES", "50"))

# Schwellwert, ab dem ein Dokument policy-seitig als Langdokument gilt.
LONG_DOCUMENT_PAGE_THRESHOLD = int(os.getenv("LONG_DOCUMENT_PAGE_THRESHOLD", "25"))

# Timeout für einzelne convert_auto Aufrufe in Sekunden.
# Standard: deaktiviert; nur per expliziter ENV-Angabe aktiv.
CONVERT_TIMEOUT_SECONDS = _optional_positive_float_env("CONVERT_TIMEOUT_SECONDS")

# Timeout für Background-Jobs in Sekunden (BUG 3 — Job-Timeout)
JOB_TIMEOUT_SECONDS = int(os.getenv("JOB_TIMEOUT_SECONDS", "900"))  # 15 Minuten
QUEUE_ENABLED = os.getenv("QUEUE_ENABLED", "true").lower() == "true"
QUEUE_WORKER_COUNT = int(os.getenv("QUEUE_WORKER_COUNT", "2"))
QUEUE_POLL_INTERVAL_SECONDS = float(os.getenv("QUEUE_POLL_INTERVAL_SECONDS", "1"))
QUEUE_LEASE_SECONDS = int(os.getenv("QUEUE_LEASE_SECONDS", "300"))
BATCH_DEFAULT_QUEUE_NAME = os.getenv("BATCH_DEFAULT_QUEUE_NAME", "default")
BATCH_STATUS_ACTIVE_ITEM_LIMIT = int(os.getenv("BATCH_STATUS_ACTIVE_ITEM_LIMIT", "10"))

# Retry
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
RATE_LIMIT_MAX_WAIT_SECONDS = int(os.getenv("RATE_LIMIT_MAX_WAIT_SECONDS", "60"))
QUALITY_RETRY_ENABLED = os.getenv("QUALITY_RETRY_ENABLED", "false").lower() == "true"
QUALITY_RETRY_THRESHOLD = float(os.getenv("QUALITY_RETRY_THRESHOLD", "0.75"))
QUALITY_RETRY_MODE = os.getenv("QUALITY_RETRY_MODE", "full")
if QUALITY_RETRY_MODE != "full":
    raise ValueError("QUALITY_RETRY_MODE must be 'full'")

# Scanned PDF Detection
SCAN_THRESHOLD_CHARS = int(os.getenv("SCAN_THRESHOLD_CHARS", "50"))

# =============================================================================
# Klassifizierung
# =============================================================================

DEFAULT_CLASSIFY = os.getenv("DEFAULT_CLASSIFY", "true").lower() == "true"
DEFAULT_CLASSIFY_CATEGORIES = os.getenv(
    "CLASSIFY_CATEGORIES",
    "invoice,contract,cv,protocol,letter,technical_doc,report,presentation,spreadsheet,other"
).split(",")
CLASSIFY_CATEGORIES = DEFAULT_CLASSIFY_CATEGORIES  # alias matching env var name

# Cache für classify-Kategorien aus der Template-Registry
_classify_categories_cache: dict = {"categories": None, "timestamp": 0}
CLASSIFY_CACHE_TTL = int(os.getenv("CLASSIFY_CACHE_TTL", "300"))  # seconds
_CLASSIFY_CACHE_TTL = CLASSIFY_CACHE_TTL  # backward-compat alias

# =============================================================================
# Logging
# =============================================================================

LOG_LEVEL = os.getenv("LOG_LEVEL", "info").upper()
LOG_FORMAT = os.getenv("LOG_FORMAT", "json")
CONTRACT_VERSION = os.getenv("CONTRACT_VERSION", "1.0")
EXECUTION_DIAGNOSTICS_LIMIT = int(os.getenv("EXECUTION_DIAGNOSTICS_LIMIT", "25"))
EXECUTION_STUCK_THRESHOLD_SECONDS = int(os.getenv("EXECUTION_STUCK_THRESHOLD_SECONDS", "1800"))
NORMALIZATION_DRIFT_SAMPLE_LIMIT = int(os.getenv("NORMALIZATION_DRIFT_SAMPLE_LIMIT", "20"))

# =============================================================================
# Dateitypen
# =============================================================================

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
MARKITDOWN_EXTENSIONS = {".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls",
                          ".odt", ".ods", ".odp", ".html", ".htm", ".xml", ".json",
                          ".csv", ".txt", ".md", ".rtf", ".eml"}
SKIP_FILES = set(os.getenv("SKIP_FILES", "email.md,consolidated.md,metadata.json,.DS_Store,Thumbs.db").split(","))

# Audio/Video (FR-MKIT-006)
WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL_SIZE", "base")
AUDIO_EXTENSIONS = {".mp3", ".wav", ".ogg", ".flac", ".m4a"}
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".avi", ".mov"}

# =============================================================================
# Modelle
# =============================================================================

# Separate Modelle für verschiedene Tasks
MISTRAL_TEXT_MODEL = os.getenv("MISTRAL_TEXT_MODEL", "mistral-small-2603")

# Token-Limits — großzügig, Kosten nur pro verbrauchtem Token
VISION_MAX_TOKENS = int(os.getenv("VISION_MAX_TOKENS", "16384"))
CLASSIFY_MAX_TOKENS = int(os.getenv("CLASSIFY_MAX_TOKENS", "1024"))
EXTRACT_MAX_TOKENS = int(os.getenv("EXTRACT_MAX_TOKENS", "16384"))
OCR_CORRECT_MAX_TOKENS = int(os.getenv("OCR_CORRECT_MAX_TOKENS", "16384"))
MISTRAL_EXTRACT_RESPONSE_FORMAT = os.getenv("MISTRAL_EXTRACT_RESPONSE_FORMAT", "json_schema")
if MISTRAL_EXTRACT_RESPONSE_FORMAT not in {"legacy", "json_object", "json_schema"}:
    raise ValueError("MISTRAL_EXTRACT_RESPONSE_FORMAT must be one of: legacy, json_object, json_schema")

# Text-Limits für LLM-Input
CLASSIFY_MAX_CHARS = int(os.getenv("CLASSIFY_MAX_CHARS", "32000"))
EXTRACT_MAX_CHARS = int(os.getenv("EXTRACT_MAX_CHARS", "32000"))

# =============================================================================
# Sprache
# =============================================================================

DEFAULT_LANGUAGE = os.getenv("DEFAULT_LANGUAGE", "de")

# =============================================================================
# Bild-Verarbeitung
# =============================================================================

MIN_IMAGE_SIZE_PX = int(os.getenv("MIN_IMAGE_SIZE_PX", "50"))
PDF_RENDER_DPI = int(os.getenv("PDF_RENDER_DPI", "200"))

# =============================================================================
# Timeouts
# =============================================================================

PDFTOTEXT_TIMEOUT = int(os.getenv("PDFTOTEXT_TIMEOUT", "60"))
PDFINFO_TIMEOUT = int(os.getenv("PDFINFO_TIMEOUT", "30"))
FFMPEG_TIMEOUT = int(os.getenv("FFMPEG_TIMEOUT", "600"))

# =============================================================================
# Whisper
# =============================================================================

WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")

# Whisper-Modell-Cache (wird beim ersten Aufruf geladen)
_whisper_model_cache: dict[str, Any] = {}  # key: model_size → WhisperModel instance

# =============================================================================
# Version & Start
# =============================================================================

# =============================================================================
# Request-Level-Cache — T-DAI-019
# =============================================================================

CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "3600"))
CACHE_ENABLED = os.getenv("CACHE_ENABLED", "true").lower() == "true"

# =============================================================================
# HTML-Renderer CDN URLs — T-DAI-021
# =============================================================================

MERMAID_CDN_URL = os.getenv("MERMAID_CDN_URL", "https://cdn.jsdelivr.net/npm/mermaid/dist/mermaid.min.js")
HIGHLIGHTJS_CDN_URL = os.getenv("HIGHLIGHTJS_CDN_URL", "https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js")
HIGHLIGHTJS_CSS_URL = os.getenv("HIGHLIGHTJS_CSS_URL", "https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github.min.css")

VERSION = os.getenv("DAIGESTR_VERSION", "16.11.4")
START_TIME = time.time()

# =============================================================================
# Webhook — T-DAI-023
# =============================================================================

WEBHOOK_TIMEOUT_SECONDS = int(os.getenv("WEBHOOK_TIMEOUT_SECONDS", "30"))

# =============================================================================
# Brix Integration — T-DAI-027
# =============================================================================

BRIX_URL = os.getenv("BRIX_URL", "http://brix:8080")

# =============================================================================
# PostgreSQL — T-DAI-031
# =============================================================================

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://daigestr:daigestr@daigestr-postgres:5432/daigestr")
DB_POOL_MIN = int(os.getenv("DB_POOL_MIN", "1"))
DB_POOL_MAX = int(os.getenv("DB_POOL_MAX", "5"))
POSTGRES_USER = os.getenv("POSTGRES_USER", "daigestr")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "daigestr")
POSTGRES_DB = os.getenv("POSTGRES_DB", "daigestr")
POSTGRES_HOST_PORT = int(os.getenv("POSTGRES_HOST_PORT", "15432"))

# =============================================================================
# Audit Log — T-DAI-070
# =============================================================================

AUDIT_ENABLED = os.getenv("AUDIT_ENABLED", "true").lower() == "true"
AUDIT_RETENTION_DAYS = int(os.getenv("AUDIT_RETENTION_DAYS", "30"))
AUDIT_API_ENABLED = os.getenv("AUDIT_API_ENABLED", "true").lower() == "true"

# =============================================================================
# Normalization — T-DAI-052
# =============================================================================

NORMALIZE_CACHE_TTL_SECONDS = int(os.getenv("NORMALIZE_CACHE_TTL_SECONDS", "60"))
NORMALIZE_CACHE_ENABLED = os.getenv("NORMALIZE_CACHE_ENABLED", "true").lower() == "true"
NORMALIZE_FALLBACK_COUNTRY = os.getenv("NORMALIZE_FALLBACK_COUNTRY", "DE")
NORMALIZE_PLAUSIBILITY_TOLERANCE = float(os.getenv("NORMALIZE_PLAUSIBILITY_TOLERANCE", "0.01"))

# =============================================================================
# Debug Snapshots / Zwischenstände — T-DAI-083
# =============================================================================

DEBUG_SNAPSHOTS_ENABLED = os.getenv("DEBUG_SNAPSHOTS_ENABLED", "false").lower() == "true"
DEBUG_SNAPSHOTS_RETENTION_DAYS = int(os.getenv("DEBUG_SNAPSHOTS_RETENTION_DAYS", "14"))
EXECUTION_RESULT_RETENTION_DAYS = int(os.getenv("EXECUTION_RESULT_RETENTION_DAYS", "30"))
EXECUTION_RESULT_ARTIFACT_RETENTION_DAYS = int(os.getenv("EXECUTION_RESULT_ARTIFACT_RETENTION_DAYS", "14"))
PII_STORAGE_MODE = os.getenv("PII_STORAGE_MODE", "strict").strip().lower()
if PII_STORAGE_MODE not in {"strict", "standard"}:
    raise ValueError("PII_STORAGE_MODE must be 'strict' or 'standard'")
DEBUG_SNAPSHOTS_ALLOW_PII = os.getenv("DEBUG_SNAPSHOTS_ALLOW_PII", "false").lower() == "true"
PII_SENSITIVE_FIELDS = tuple(
    part.strip()
    for part in os.getenv(
        "PII_SENSITIVE_FIELDS",
        "markdown,html,extracted,normalized,base64,url,source_ref,email,phone,address,iban,tax_id",
    ).split(",")
    if part.strip()
)
DEBUG_SNAPSHOT_API_ENABLED = os.getenv("DEBUG_SNAPSHOT_API_ENABLED", "false").lower() == "true"
REPLAY_API_ENABLED = os.getenv("REPLAY_API_ENABLED", "false").lower() == "true"
DEBUG_SNAPSHOTS_POLICIES = tuple(
    part.strip()
    for part in os.getenv(
        "DEBUG_SNAPSHOTS_POLICIES",
        "failures,retries,long_documents,low_quality",
    ).split(",")
    if part.strip()
)
_ALLOWED_DEBUG_SNAPSHOT_POLICIES = {
    "all",
    "failures",
    "retries",
    "low_quality",
    "long_documents",
}
_invalid_debug_snapshot_policies = sorted(
    set(DEBUG_SNAPSHOTS_POLICIES) - _ALLOWED_DEBUG_SNAPSHOT_POLICIES
)
if _invalid_debug_snapshot_policies:
    raise ValueError(
        "DEBUG_SNAPSHOTS_POLICIES contains unsupported values: "
        + ", ".join(_invalid_debug_snapshot_policies)
    )
DEBUG_SNAPSHOTS_LONG_DOCUMENT_PAGE_THRESHOLD = int(
    os.getenv("DEBUG_SNAPSHOTS_LONG_DOCUMENT_PAGE_THRESHOLD", "25")
)
DEBUG_SNAPSHOTS_LOW_QUALITY_THRESHOLD = float(
    os.getenv("DEBUG_SNAPSHOTS_LOW_QUALITY_THRESHOLD", str(QUALITY_RETRY_THRESHOLD))
)
DEBUG_SNAPSHOTS_INCLUDE_MARKDOWN = (
    os.getenv("DEBUG_SNAPSHOTS_INCLUDE_MARKDOWN", "true").lower() == "true"
)
DEBUG_SNAPSHOTS_INCLUDE_EXTRACTED = (
    os.getenv("DEBUG_SNAPSHOTS_INCLUDE_EXTRACTED", "true").lower() == "true"
)
DEBUG_SNAPSHOTS_INCLUDE_NORMALIZED = (
    os.getenv("DEBUG_SNAPSHOTS_INCLUDE_NORMALIZED", "true").lower() == "true"
)
DEBUG_SNAPSHOTS_INCLUDE_ERRORS = (
    os.getenv("DEBUG_SNAPSHOTS_INCLUDE_ERRORS", "true").lower() == "true"
)
