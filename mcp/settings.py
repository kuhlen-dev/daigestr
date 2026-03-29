"""
Daigestr — Konfiguration / Settings

Alle Umgebungsvariablen und Konstanten aus server.py extrahiert.
"""

import os
import time
from pathlib import Path
from typing import Any

# =============================================================================
# Verzeichnisse
# =============================================================================

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
TEMP_DIR = Path(os.getenv("TEMP_DIR", "/tmp/markitdown"))
TEMP_DIR.mkdir(parents=True, exist_ok=True)

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

# =============================================================================
# Server Ports
# =============================================================================

MCP_PORT = int(os.getenv("MCP_PORT", "8080"))
MCP_TRANSPORT = os.getenv("MCP_TRANSPORT", "sse")
REST_PORT = int(os.getenv("REST_PORT", "8081"))
BIND_HOST = os.getenv("BIND_HOST", "0.0.0.0")

# =============================================================================
# Limits
# =============================================================================

MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "25"))
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
IMAGE_MAX_WIDTH = int(os.getenv("IMAGE_MAX_WIDTH", "2048"))

# Max. Bilder die bei describe_images beschrieben werden (FIX 3 — T-DAI-024)
MAX_DESCRIBE_IMAGES = int(os.getenv("MAX_DESCRIBE_IMAGES", "50"))

# Timeout für einzelne convert_auto Aufrufe in Sekunden (FIX 4 — T-DAI-024)
CONVERT_TIMEOUT_SECONDS = int(os.getenv("CONVERT_TIMEOUT_SECONDS", "300"))

# Timeout für Background-Jobs in Sekunden (BUG 3 — Job-Timeout)
JOB_TIMEOUT_SECONDS = int(os.getenv("JOB_TIMEOUT_SECONDS", "900"))  # 15 Minuten

# Retry
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
RATE_LIMIT_MAX_WAIT_SECONDS = int(os.getenv("RATE_LIMIT_MAX_WAIT_SECONDS", "60"))

# Scanned PDF Detection
SCAN_THRESHOLD_CHARS = int(os.getenv("SCAN_THRESHOLD_CHARS", "50"))

# =============================================================================
# Klassifizierung
# =============================================================================

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

VERSION = os.getenv("DAIGESTR_VERSION", "dev")
START_TIME = time.time()

# =============================================================================
# Webhook — T-DAI-023
# =============================================================================

WEBHOOK_TIMEOUT_SECONDS = int(os.getenv("WEBHOOK_TIMEOUT_SECONDS", "30"))
