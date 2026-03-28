"""
Regressions-Tests für die Modularisierung von server.py.

Prüft dass alle extrahierten Funktionen:
1. Im richtigen Modul vorhanden sind
2. Via server.py erreichbar sind (Backwards-Compatibility)
3. Nicht dupliziert sind (nur in EINEM Modul definiert)
4. Keine zirkulären Imports entstehen
"""

import importlib
import sys
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Erwartete Funktions-Zuordnungen: Funktion → Modul
# ---------------------------------------------------------------------------

UTILS_FUNCTIONS = [
    "resolve_path",
    "get_file_extension",
    "is_image_file",
    "is_markitdown_file",
    "is_audio_file",
    "is_video_file",
    "should_skip_file",
    "get_mimetype",
    "detect_mimetype_from_bytes",
    "strip_llm_artifacts",
    "detect_code_language",
    "detect_and_fence_code_blocks",
]

MISTRAL_CLIENT_FUNCTIONS = [
    "call_mistral_vision_api",
    "call_mistral_ocr_api",
    "analyze_with_mistral_vision",
]

IMAGES_FUNCTIONS = [
    "resize_image_if_needed",
    "extract_image_metadata",
    "extract_images_from_docx",
    "extract_images_from_pptx",
    "classify_image_type",
    "convert_diagram_to_mermaid",
    "extract_chart_data",
    "describe_embedded_images",
    "insert_image_descriptions",
    "render_first_page_as_image",
]

PDF_FUNCTIONS = [
    "extract_tables_with_pdfplumber",
    "extract_tables_with_img2table",
    "merge_cross_page_tables",
    "tables_to_markdown",
    "is_scanned_pdf",
    "convert_scanned_pdf_ocr3",
    "convert_scanned_pdf",
    "extract_pdf_metadata",
    "detect_zugferd",
    "extract_xmp_metadata",
    "list_embedded_files",
    "parse_zugferd_xml",
    "prepend_pdf_toc",
    "append_pdf_annotations",
    "append_pdf_form_fields",
    "embed_ocr_in_pdf",
]

OFFICE_FUNCTIONS = [
    "convert_excel_enhanced",
    "extract_docx_extras",
    "append_docx_extras_to_markdown",
    "extract_document_properties",
    "extract_pptx_hidden_info",
    "convert_with_markitdown",
]

ALL_EXTRACTED = {
    "utils": UTILS_FUNCTIONS,
    "mistral_client": MISTRAL_CLIENT_FUNCTIONS,
    "converters.images": IMAGES_FUNCTIONS,
    "converters.pdf": PDF_FUNCTIONS,
    "converters.office": OFFICE_FUNCTIONS,
}

ALL_FUNCTION_NAMES = (
    UTILS_FUNCTIONS + MISTRAL_CLIENT_FUNCTIONS + IMAGES_FUNCTIONS
    + PDF_FUNCTIONS + OFFICE_FUNCTIONS
)


# ---------------------------------------------------------------------------
# Setup: sicherstellen dass mcp/ im Pfad ist
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True, scope="module")
def setup_path():
    mcp_dir = str(Path(__file__).parent.parent)
    if mcp_dir not in sys.path:
        sys.path.insert(0, mcp_dir)


# ---------------------------------------------------------------------------
# Test 1: Jede Funktion ist im erwarteten Modul vorhanden
# ---------------------------------------------------------------------------

class TestFunctionsInModules:
    @pytest.mark.parametrize("module_name,functions", list(ALL_EXTRACTED.items()))
    def test_module_has_all_functions(self, module_name, functions):
        """Jedes Modul enthält alle erwarteten Funktionen."""
        # Lade Modul frisch (ohne server.py Side-Effects)
        if module_name in sys.modules:
            del sys.modules[module_name]
        try:
            mod = importlib.import_module(module_name)
        except ImportError as e:
            pytest.fail(f"Modul {module_name} nicht importierbar: {e}")

        for fn_name in functions:
            assert hasattr(mod, fn_name), (
                f"Funktion '{fn_name}' fehlt in Modul '{module_name}'"
            )
            assert callable(getattr(mod, fn_name)), (
                f"'{fn_name}' in '{module_name}' ist nicht callable"
            )


# ---------------------------------------------------------------------------
# Test 2: Keine Duplikate — Funktion darf nicht in server.py UND Modul sein
# ---------------------------------------------------------------------------

class TestNoDuplicates:
    def test_no_function_defined_in_server(self):
        """Extrahierte Funktionen dürfen NICHT mehr als def in server.py stehen."""
        server_path = Path(__file__).parent.parent / "server.py"
        server_code = server_path.read_text()

        duplicates = []
        for fn_name in ALL_FUNCTION_NAMES:
            # Suche nach "def fn_name(" oder "async def fn_name("
            if f"def {fn_name}(" in server_code:
                duplicates.append(fn_name)

        assert duplicates == [], (
            f"Diese Funktionen sind noch als 'def' in server.py definiert "
            f"(sollten nur noch importiert werden): {duplicates}"
        )


# ---------------------------------------------------------------------------
# Test 3: Server re-exportiert alle Funktionen (Backwards-Compatibility)
# ---------------------------------------------------------------------------

class TestServerReexports:
    def test_all_functions_accessible_via_server(self):
        """Alle extrahierten Funktionen müssen via 'import server' erreichbar sein."""
        from conftest import load_server_module
        server = load_server_module()

        missing = []
        for fn_name in ALL_FUNCTION_NAMES:
            if not hasattr(server, fn_name):
                missing.append(fn_name)

        assert missing == [], (
            f"Diese Funktionen fehlen im server-Modul Namespace "
            f"(Import vergessen?): {missing}"
        )


# ---------------------------------------------------------------------------
# Test 4: Keine zirkulären Imports
# ---------------------------------------------------------------------------

class TestNoCircularImports:
    @pytest.mark.parametrize("module_name", list(ALL_EXTRACTED.keys()))
    def test_module_importable_standalone(self, module_name):
        """Jedes Modul muss unabhängig importierbar sein (kein circular import)."""
        # Frischer Import ohne Cache
        for key in list(sys.modules.keys()):
            if key.startswith(("converters", "utils", "mistral_client", "server")):
                del sys.modules[key]

        try:
            importlib.import_module(module_name)
        except ImportError as e:
            if "circular" in str(e).lower():
                pytest.fail(f"Zirkulärer Import in {module_name}: {e}")
            # Andere ImportErrors (fehlende Deps) sind OK — werden von conftest gemockt
            pass


# ---------------------------------------------------------------------------
# Test 5: settings.py hat alle benötigten Variablen
# ---------------------------------------------------------------------------

class TestSettingsCompleteness:
    def test_settings_has_required_vars(self):
        """settings.py muss alle Konfigurations-Variablen bereitstellen."""
        if "settings" in sys.modules:
            del sys.modules["settings"]
        try:
            import settings
        except ImportError:
            pytest.skip("settings.py nicht direkt importierbar")

        required = [
            "DATA_DIR", "TEMP_DIR", "MISTRAL_API_KEY", "MISTRAL_API_URL",
            "MISTRAL_VISION_MODEL", "MISTRAL_TIMEOUT", "MISTRAL_OCR_MODEL",
            "MISTRAL_OCR_ENABLED", "MCP_PORT", "REST_PORT", "MAX_FILE_SIZE_MB",
            "MAX_FILE_SIZE_BYTES", "IMAGE_MAX_WIDTH", "MAX_RETRIES",
            "SCAN_THRESHOLD_CHARS", "LOG_LEVEL", "LOG_FORMAT",
            "IMAGE_EXTENSIONS", "MARKITDOWN_EXTENSIONS", "SKIP_FILES",
            "WHISPER_MODEL_SIZE", "AUDIO_EXTENSIONS", "VIDEO_EXTENSIONS",
            "MISTRAL_TEXT_MODEL", "VISION_MAX_TOKENS", "CLASSIFY_MAX_TOKENS",
            "EXTRACT_MAX_TOKENS", "OCR_CORRECT_MAX_TOKENS",
            "CLASSIFY_MAX_CHARS", "EXTRACT_MAX_CHARS", "DEFAULT_LANGUAGE",
            "VERSION",
        ]
        missing = [v for v in required if not hasattr(settings, v)]
        assert missing == [], f"settings.py fehlen: {missing}"
