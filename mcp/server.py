#!/usr/bin/env python3
# Aktiviere uvloop für bessere Performance
try:
    import uvloop
    uvloop.install()
except ImportError:
    pass

"""
Daigestr — Document Intelligence Service v8.3.2

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

import os
import threading
import time as _time

import httpx  # noqa: F401 — re-exported for test patchability (tests patch _server.httpx)
import structlog
from markitdown import MarkItDown

from models import (
    CANONICAL_ERROR_SEMANTICS,
    CANONICAL_EXECUTION_FIELDS,
    CANONICAL_META_FIELDS,
    CANONICAL_NULL_SEMANTICS,
    CANONICAL_RESULT_FIELDS,
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
    extract_mistral_ocr_metadata,
)
from converters.images import (
    resize_image_if_needed,
    extract_image_metadata,
    _dms_to_decimal,
    extract_images_from_docx,
    extract_images_from_pptx,
    extract_images_from_pdf,
    extract_images_from_odt,
    extract_images_from_odp,
    extract_images_from_html,
    classify_image_type,
    convert_diagram_to_mermaid,
    extract_chart_data,
    describe_embedded_images,
    insert_image_descriptions,
    render_first_page_as_image,
    render_pdf_pages_as_images,
    describe_page_images,
    insert_page_descriptions,
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
    extract_odt_extras,
    extract_odp_hidden_slides,
    convert_ods_enhanced,
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
    get_db_connection,  # re-exported from templates_db via intelligence
    get_template_by_id,  # re-exported from templates_db via intelligence
    get_meta_schema,
    get_steuer_signalwoerter,
    get_datentyp_konventionen,
)
from debug_snapshots import (  # noqa: F401
    should_capture_debug_snapshot,
    build_debug_snapshot_payload,
    replay_normalization_from_snapshot,
)
from progress_tracking import build_progress_payload, normalize_progress_payload  # noqa: F401

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

# Re-export normalizer_db for backwards-compatibility (T-DAI-050)
from normalizer_db import (  # noqa: F401
    init_normalization_db,
    get_normalization_drift_summary,
)

# Re-export audit_db functions and settings for backwards-compatibility (T-DAI-070)
from audit_db import (  # noqa: F401
    init_audit_db,
    audit_log_event,
    audit_get_by_request,
    audit_get_by_execution,
    audit_get_by_job,
    audit_list,
    audit_cleanup,
)
from settings import (  # noqa: F401
    AUDIT_ENABLED,
    AUDIT_RETENTION_DAYS,
    AUDIT_API_ENABLED,
)
from api_rest_audit import audit_router  # noqa: F401 — re-exported for test patchability
from debug_snapshot_db import (  # noqa: F401
    init_debug_snapshot_db,
    debug_snapshot_store,
    debug_snapshot_get,
    debug_snapshot_list,
    debug_snapshot_cleanup,
)

# Re-export normalize routers and normalizer (T-DAI-055/T-DAI-056)
from api_rest_normalize import normalize_router, corrections_router, batch_router  # noqa: F401
from normalizer import normalize  # noqa: F401
from normalizer_cache import cache_reset as normalizer_cache_reset  # noqa: F401
from execution_db import (  # noqa: F401
    init_execution_db,
    execution_create,
    execution_update,
    execution_get,
    execution_get_full,
    execution_get_by_request_id,
    execution_get_by_job_id,
    execution_list,
    execution_list_active,
    execution_list_stuck,
    execution_queue_enqueue,
    execution_queue_claim_next,
    execution_queue_complete,
    execution_queue_fail,
    execution_queue_list,
    execution_attempt_upsert,
    execution_attempt_list,
    execution_result_upsert,
    execution_result_get_final,
    execution_result_list,
)

# Re-export templates_db functions for backwards-compatibility
from templates_db import (  # noqa: F401
    init_templates_db,
    check_persistence_health,
    get_all_template_ids,
    get_template_by_id,
    search_templates,
    get_prompt,
    get_scoring_weight,
    upsert_prompt,
    list_prompts,
    get_prompt_by_id,
    cache_get,
    cache_set,
    cache_clear,
    pool_reset,
    # Async Job API (T-DAI-023)
    job_create,
    job_update,
    job_set_result,
    job_set_terminal_result,
    job_get,
    job_delete,
    job_list,
)

# Re-export routing functions for backwards-compatibility
from routing import (  # noqa: F401
    convert_auto,
    _convert_auto_impl,
    _resolve_mode_policy,
    _resolve_retry_policy,
    _resolve_normalization_policy,
    _resolve_long_document_policy,
    _apply_contract_warnings,
    convert_folder_contents,
    convert_url,
    _build_tips_dict,
)

# Re-export REST API app and endpoints for backwards-compatibility
from api_rest import (  # noqa: F401
    app,
    _safe_encode,
    api_convert,
    api_convert_folder,
    api_extract,
    api_template_categories,
    api_search_templates,
    api_templates,
    api_get_template,
    api_bulk_templates,
    api_create_template,
    api_update_template,
    api_delete_template,
    api_analyze,
    api_health,
    api_formats,
    api_tips,
    run_rest_server,
    # Async Job API + Webhook (T-DAI-023)
    api_convert_async,
    api_list_jobs,
    api_get_job,
    api_get_job_result,
    api_delete_job,
    api_list_debug_snapshots,
    api_get_debug_snapshot,
    api_replay_debug_snapshot_normalize,
    _fire_webhook,
    _run_async_job,
    _api_convert_impl,
)

# Re-export MCP tools for backwards-compatibility
from api_mcp import (  # noqa: F401
    mcp,
    mcp_convert,
    mcp_extract,
    mcp_convert_folder,
    mcp_health,
    mcp_list_files,
    mcp_get_tips,
)

# MarkItDown Instanz (legacy — routing.py hat eigene Instanz)
md = MarkItDown()

# =============================================================================
# Persistence Bootstrap
# =============================================================================


def initialize_persistence() -> None:
    """
    Initialize all PostgreSQL-backed subsystems before the service starts.

    Daigestr is DB-first/DB-only. If one of the persistence layers cannot be
    initialized, startup must fail before either REST or MCP begin accepting
    requests.
    """
    init_steps = [
        ("template_registry", init_templates_db),
        ("execution_db", init_execution_db),
        ("audit_db", init_audit_db),
        ("debug_snapshot_db", init_debug_snapshot_db),
        ("normalization_db", init_normalization_db),
    ]

    for component, init_fn in init_steps:
        try:
            init_fn()
            log.info(f"{component}_initialized", database_url=DATABASE_URL)
        except Exception as exc:
            log.error(f"{component}_init_failed", database_url=DATABASE_URL, error=str(exc))
            raise RuntimeError(f"{component} initialization failed") from exc


# =============================================================================
# Server Start
# =============================================================================

if __name__ == "__main__":
    from settings import (
        VERSION, DATA_DIR, MISTRAL_VISION_MODEL, MISTRAL_API_KEY,
        MCP_PORT, REST_PORT, MAX_FILE_SIZE_MB, IMAGE_MAX_WIDTH, MAX_RETRIES,
    )

    initialize_persistence()

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

    def _rest_thread_with_watchdog():
        while True:
            try:
                run_rest_server()
            except Exception as _e:
                log.error("rest_server_crashed", error=str(_e))
                log.info("rest_server_restarting_in_5s")
                _time.sleep(5)

    rest_thread = threading.Thread(target=_rest_thread_with_watchdog, daemon=True)
    rest_thread.start()
    print(f"REST-API gestartet auf Port {REST_PORT}")

    transport = os.getenv("MCP_TRANSPORT", "sse")
    if transport == "stdio":
        mcp.run()
    else:
        print(f"MCP-Server startet auf Port {MCP_PORT}")
        mcp.run(transport="sse", host=os.getenv("BIND_HOST", "0.0.0.0"), port=MCP_PORT)
