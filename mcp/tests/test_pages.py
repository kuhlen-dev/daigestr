"""
Tests für T-DAI-025: PDF Page Selection — parse_pages Funktion und Integration.
"""

import sys
import importlib
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

import pytest


# ---------------------------------------------------------------------------
# Ensure mcp/ is on sys.path
# ---------------------------------------------------------------------------
MCP_DIR = Path(__file__).parent.parent
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))


# ===========================================================================
# Unit tests: parse_pages
# ===========================================================================

def load_utils():
    """Load utils module freshly."""
    if "utils" in sys.modules:
        return sys.modules["utils"]
    return importlib.import_module("utils")


class TestParsePages:
    """Unit tests for parse_pages()."""

    def setup_method(self):
        # Ensure structlog is mocked if not already
        if "structlog" not in sys.modules:
            mock_sl = MagicMock()
            mock_sl.get_logger = MagicMock(return_value=MagicMock())
            sys.modules["structlog"] = mock_sl
        if "magic" not in sys.modules:
            sys.modules["magic"] = MagicMock()
        self.utils = load_utils()
        self.parse_pages = self.utils.parse_pages

    def test_single_page(self):
        result = self.parse_pages("1", total_pages=10)
        assert result == [0]

    def test_range(self):
        result = self.parse_pages("1-3", total_pages=10)
        assert result == [0, 1, 2]

    def test_comma_list(self):
        result = self.parse_pages("7,14", total_pages=20)
        assert result == [6, 13]

    def test_comma_list_three(self):
        result = self.parse_pages("7,14,22", total_pages=30)
        assert result == [6, 13, 21]

    def test_exclusion(self):
        result = self.parse_pages("1-5,!3", total_pages=10)
        assert result == [0, 1, 3, 4]

    def test_range_with_exclusion_alias(self):
        # "1-5,!3" should exclude page 3 (0-based index 2)
        result = self.parse_pages("1-5,!3", total_pages=10)
        assert 2 not in result
        assert sorted(result) == [0, 1, 3, 4]

    def test_multiple_ranges(self):
        result = self.parse_pages("10-12,14-16", total_pages=20)
        assert result == [9, 10, 11, 13, 14, 15]

    def test_out_of_range_ignored(self):
        # Pages beyond total_pages are silently ignored
        result = self.parse_pages("1,5,99", total_pages=5)
        assert result == [0, 4]

    def test_below_range_ignored(self):
        # Page 0 is invalid (1-based), should be ignored
        result = self.parse_pages("0,1,2", total_pages=10)
        assert result == [0, 1]  # page 0 (1-based) invalid → only pages 1 and 2

    def test_empty_result_raises_value_error(self):
        with pytest.raises(ValueError):
            self.parse_pages("99,100", total_pages=5)

    def test_empty_spec_raises_value_error(self):
        with pytest.raises(ValueError):
            self.parse_pages("", total_pages=10)

    def test_whitespace_spec_raises_value_error(self):
        with pytest.raises(ValueError):
            self.parse_pages("   ", total_pages=10)

    def test_sorted_output(self):
        result = self.parse_pages("5,2,8,1", total_pages=10)
        assert result == sorted(result)

    def test_deduplication(self):
        # Overlapping ranges should deduplicate
        result = self.parse_pages("1-3,2-4", total_pages=10)
        assert result == [0, 1, 2, 3]

    def test_last_page(self):
        result = self.parse_pages("10", total_pages=10)
        assert result == [9]

    def test_full_exclusion_raises_value_error(self):
        with pytest.raises(ValueError):
            self.parse_pages("1,!1", total_pages=5)

    def test_exclusion_of_nonexistent_page_ok(self):
        # Excluding a page that wasn't included is fine
        result = self.parse_pages("1-3,!99", total_pages=10)
        assert result == [0, 1, 2]


# ===========================================================================
# Integration tests: pages parameter flows through routing
# ===========================================================================

class TestPageSelectionIntegration:
    """Integration tests ensuring pages parameter flows into PDF processing."""

    def setup_method(self):
        """Set up mocked environment for routing tests."""
        # Ensure required stubs exist
        for mod in ["structlog", "magic", "markitdown", "fastmcp", "uvicorn",
                    "httpx", "tenacity", "fastapi", "fastapi.exceptions",
                    "fastapi.responses", "pdfplumber", "pdf2image", "PIL", "PIL.Image"]:
            if mod not in sys.modules:
                sys.modules[mod] = MagicMock()

        structlog_mock = MagicMock()
        structlog_mock.get_logger = MagicMock(return_value=MagicMock())
        sys.modules["structlog"] = structlog_mock

    def _load_routing(self):
        """Load routing module fresh."""
        mods_to_remove = [k for k in sys.modules if k in ("routing", "utils",
                          "converters.pdf", "converters.images", "converters.office",
                          "converters.audio", "intelligence", "templates_db",
                          "mistral_client", "settings")]
        for m in mods_to_remove:
            sys.modules.pop(m, None)
        return importlib.import_module("routing")

    @pytest.mark.asyncio
    async def test_pages_parameter_passed_to_convert_scanned(self):
        """
        Verify that pages spec is parsed and page_indices passed to convert_scanned_pdf.

        routing._convert_auto_impl uses _get() to look up functions.  _get() reads
        from the 'server' module in sys.modules (captured at import time via
        _LOADED_BY_SERVER).  The simplest way to patch is to set the names directly
        on the routing module AND on sys.modules['server'] so _get() finds them.
        """
        # Fitz mock: 5-page document
        import fitz as _real_fitz  # noqa: PLC0415
        _original_fitz = sys.modules.get("fitz", _real_fitz)
        fitz_mock = MagicMock()
        mock_doc = MagicMock()
        mock_doc.__len__ = lambda self: 5
        fitz_mock.open.return_value = mock_doc
        sys.modules["fitz"] = fitz_mock

        routing = self._load_routing()

        fake_pdf = b"%PDF-1.4 fake"

        mock_scanned_result = {
            "success": True,
            "markdown": "## Seite 1\n\nTest content",
            "scanned": True,
            "pages_processed": 1,
            "ocr_model": "mistral-ocr-latest",
        }

        call_kwargs = {}

        async def fake_convert_scanned(
            path,
            language="de",
            page_indices=None,
            request_id=None,
            attempt_number=None,
        ):
            call_kwargs["page_indices"] = page_indices
            call_kwargs["request_id"] = request_id
            call_kwargs["attempt_number"] = attempt_number
            return mock_scanned_result

        # _get() looks up names in the 'server' module that imported routing.
        # We create a minimal fake server module and register all needed names.
        server_mod = MagicMock()
        server_mod.is_scanned_pdf = lambda p: True
        server_mod.convert_scanned_pdf = fake_convert_scanned
        server_mod.MISTRAL_API_KEY = "fake-key"
        server_mod.CACHE_ENABLED = False
        server_mod.CACHE_TTL_SECONDS = 300
        server_mod.MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024
        server_mod.MAX_FILE_SIZE_MB = 50
        server_mod.CONVERT_TIMEOUT_SECONDS = 120
        server_mod.MAX_DESCRIBE_IMAGES = 10
        server_mod.extract_images_from_pdf = lambda path, page_indices=None: []
        server_mod.calculate_quality_score = lambda *a, **k: {}
        server_mod.chunk_markdown = lambda *a, **k: []
        server_mod.classify_document = AsyncMock(return_value={})
        server_mod._apply_auto_extract = AsyncMock(return_value=None)
        server_mod.extract_structured_data = AsyncMock(return_value={"success": False})
        server_mod.convert_with_markitdown = lambda *a, **k: {"success": False, "error": "skip"}
        server_mod.embed_ocr_in_pdf = lambda *a, **k: None
        server_mod.cache_get = lambda *a, **k: None
        server_mod.cache_set = lambda *a, **k: None
        server_mod.parse_pages = routing.parse_pages
        server_mod.render_first_page_as_image = lambda *a, **k: None
        server_mod.correct_ocr_text = AsyncMock(return_value={"success": False})
        server_mod.dual_pass_validate = AsyncMock(return_value="")
        server_mod.detect_mimetype_from_bytes = lambda data: "application/pdf"
        server_mod.get_mimetype = lambda path: "application/pdf"
        server_mod.get_file_extension = lambda filename: ".pdf"
        server_mod._convert_auto_impl = routing._convert_auto_impl

        # Register the fake server module and re-patch _LOADED_BY_SERVER in utils
        sys.modules["server"] = server_mod

        # Patch _LOADED_BY_SERVER in all relevant sub-modules
        import utils as utils_mod
        original_lbs = utils_mod._LOADED_BY_SERVER
        utils_mod._LOADED_BY_SERVER = server_mod

        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(fake_pdf)
            tmp_path = Path(tmp.name)

        try:
            response = await routing.convert_auto(
                file_data=fake_pdf,
                filename="test.pdf",
                source=str(tmp_path),
                source_type="file",
                input_meta={},
                pages="1",
            )
            # page_indices should be [0] for pages="1" with 5 total pages
            assert call_kwargs.get("page_indices") == [0], (
                f"Expected page_indices=[0] but got {call_kwargs.get('page_indices')}"
            )
        finally:
            tmp_path.unlink(missing_ok=True)
            utils_mod._LOADED_BY_SERVER = original_lbs
            # Restore real fitz so other tests can use it
            sys.modules["fitz"] = _original_fitz
