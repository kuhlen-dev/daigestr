"""
Tests for T-MKIT-032: LLM Hints — get_tips, Docstrings, Response-Hints, OpenAPI.

All tests run without Docker container and without real API calls.
All external dependencies are mocked via unittest.mock.
"""

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conftest import load_server_module, run_async


# ---------------------------------------------------------------------------
# Loader with PassthroughMCP so @mcp.tool() doesn't wrap functions in MagicMock
# ---------------------------------------------------------------------------

class _PassthroughMCP:
    """FastMCP replacement: @mcp.tool() returns the function unchanged."""

    def tool(self, name=None, **kwargs):
        def decorator(f):
            return f
        return decorator

    def run(self, *args, **kwargs):
        pass


def _load_server_with_passthrough_mcp():
    """Loads server.py with real @mcp.tool() passthrough."""
    fastmcp_mock = MagicMock()
    fastmcp_mock.FastMCP = MagicMock(return_value=_PassthroughMCP())
    sys.modules["fastmcp"] = fastmcp_mock

    if "server" in sys.modules:
        del sys.modules["server"]

    return load_server_module()


# Load once; all tests in this module share this instance
_server = _load_server_with_passthrough_mcp()
mcp_get_tips = _server.mcp_get_tips
_build_tips_dict = _server._build_tips_dict
convert_auto = _server.convert_auto

# DB in temporärem Verzeichnis initialisieren (isoliert vom Produktions-DB)
_tmp_db_dir = tempfile.mkdtemp(prefix="daigestr_test_tips_")
_tmp_db_path = Path(_tmp_db_dir) / "templates.db"
_server.TEMPLATES_DB_PATH = _tmp_db_path
_server.init_templates_db()


# ---------------------------------------------------------------------------
# Tests: get_tips MCP tool
# ---------------------------------------------------------------------------

class TestGetTipsMCPTool:
    """Tests for the mcp_get_tips MCP tool."""

    def test_get_tips_returns_json(self):
        """get_tips must return valid JSON."""
        result = run_async(mcp_get_tips())
        parsed = json.loads(result)
        assert isinstance(parsed, dict)

    def test_get_tips_has_service_field(self):
        """get_tips must include a 'service' key with version info."""
        result = json.loads(run_async(mcp_get_tips()))
        assert "service" in result
        assert "Daigestr" in result["service"]

    def test_get_tips_has_common_mistakes(self):
        """get_tips must include a 'common_mistakes' list."""
        result = json.loads(run_async(mcp_get_tips()))
        assert "common_mistakes" in result
        assert isinstance(result["common_mistakes"], list)
        assert len(result["common_mistakes"]) > 0

    def test_get_tips_common_mistakes_have_required_keys(self):
        """Each common_mistake entry must have problem, cause, and fix keys."""
        result = json.loads(run_async(mcp_get_tips()))
        for mistake in result["common_mistakes"]:
            assert "problem" in mistake
            assert "cause" in mistake
            assert "fix" in mistake

    def test_get_tips_has_templates(self):
        """get_tips must list available_templates including invoice, cv, contract."""
        result = json.loads(run_async(mcp_get_tips()))
        assert "available_templates" in result
        templates = result["available_templates"]
        assert "invoice" in templates
        assert "cv" in templates
        assert "contract" in templates

    def test_get_tips_has_optional_features(self):
        """get_tips must document all optional features."""
        result = json.loads(run_async(mcp_get_tips()))
        assert "optional_features" in result
        features = result["optional_features"]
        # All key features must be documented
        for key in ["accuracy", "classify", "extract_schema", "template",
                    "describe_images", "ocr_correct", "show_formulas", "chunk",
                    "chunk_size", "language", "mode", "prompt",
                    "classify_categories", "output_format"]:
            assert key in features, f"Missing feature documentation: {key}"

    def test_get_tips_has_quick_reference(self):
        """get_tips must include a quick_reference section."""
        result = json.loads(run_async(mcp_get_tips()))
        assert "quick_reference" in result
        qr = result["quick_reference"]
        assert "convert_file" in qr
        assert "extract_invoice" in qr

    def test_get_tips_has_response_fields(self):
        """get_tips must document response fields."""
        result = json.loads(run_async(mcp_get_tips()))
        assert "response_fields" in result
        fields = result["response_fields"]
        assert "markdown" in fields
        assert "extracted" in fields
        assert "chunks" in fields
        assert "meta" in fields

    def test_get_tips_has_note_mcp_vs_rest(self):
        """get_tips must include the base64_data vs base64 note."""
        result = json.loads(run_async(mcp_get_tips()))
        assert "note_mcp_vs_rest" in result
        assert "base64_data" in result["note_mcp_vs_rest"]

    def test_extracted_null_mistake_present(self):
        """The 'extracted is null' common mistake must be documented."""
        result = json.loads(run_async(mcp_get_tips()))
        problems = [m["problem"] for m in result["common_mistakes"]]
        assert "extracted is null" in problems

    def test_chunks_null_mistake_present(self):
        """The 'chunks is null' common mistake must be documented."""
        result = json.loads(run_async(mcp_get_tips()))
        problems = [m["problem"] for m in result["common_mistakes"]]
        assert "chunks is null" in problems

    def test_ocr_embed_mistake_present(self):
        """The 'ocr_embed has no effect' common mistake must be documented."""
        result = json.loads(run_async(mcp_get_tips()))
        problems = [m["problem"] for m in result["common_mistakes"]]
        assert "ocr_embed has no effect" in problems

    def test_classify_categories_ignored_mistake_present(self):
        """The 'classify_categories ignored' common mistake must be documented."""
        result = json.loads(run_async(mcp_get_tips()))
        problems = [m["problem"] for m in result["common_mistakes"]]
        assert "classify_categories ignored" in problems

    def test_prompt_no_effect_mistake_present(self):
        """The 'prompt has no effect on documents' common mistake must be documented."""
        result = json.loads(run_async(mcp_get_tips()))
        problems = [m["problem"] for m in result["common_mistakes"]]
        assert "prompt has no effect on documents" in problems

    def test_describe_images_mentions_pdf_and_mermaid(self):
        """describe_images description must mention PDF and Mermaid."""
        result = json.loads(run_async(mcp_get_tips()))
        desc = result["optional_features"]["describe_images"]["description"]
        assert "PDF" in desc, "describe_images description must mention PDF"
        assert "Mermaid" in desc, "describe_images description must mention Mermaid"

    def test_available_templates_not_empty(self):
        """available_templates must be a non-empty dict (dynamically loaded from DB)."""
        result = json.loads(run_async(mcp_get_tips()))
        assert "available_templates" in result
        templates = result["available_templates"]
        assert isinstance(templates, (dict, list))
        # Templates are seeded in DB — must not be empty
        assert len(templates) > 0

    def test_response_fields_has_html(self):
        """response_fields must document the html field for output_format='html'."""
        result = json.loads(run_async(mcp_get_tips()))
        assert "html" in result["response_fields"]
        assert "output_format" in result["response_fields"]["html"].lower() or "html" in result["response_fields"]["html"].lower()


# ---------------------------------------------------------------------------
# Tests: REST /v1/tips endpoint (via _build_tips_dict)
# ---------------------------------------------------------------------------

class TestApiTipsEndpoint:
    """Tests for the REST /v1/tips endpoint logic."""

    def test_api_tips_returns_same_content_as_mcp(self):
        """REST /v1/tips and MCP get_tips must return the same content."""
        rest_result = _build_tips_dict()
        mcp_result = json.loads(run_async(mcp_get_tips()))
        assert rest_result == mcp_result

    def test_build_tips_dict_returns_dict(self):
        """_build_tips_dict must return a plain dict."""
        result = _build_tips_dict()
        assert isinstance(result, dict)

    def test_build_tips_dict_is_json_serializable(self):
        """_build_tips_dict output must be JSON-serializable."""
        result = _build_tips_dict()
        serialized = json.dumps(result)
        assert isinstance(serialized, str)
        assert len(serialized) > 100


# ---------------------------------------------------------------------------
# Tests: Response hints in convert_auto
# ---------------------------------------------------------------------------

class TestResponseHints:
    """Tests for context-sensitive response hints in convert_auto."""

    def test_hints_invoice_without_schema(self):
        """A hint must appear when document_type=invoice but no extract_schema is set."""
        mock_result = {"success": True, "markdown": "# Invoice\nTest content"}
        quality_result = {"quality_score": 0.8, "quality_grade": "good"}

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "MAX_FILE_SIZE_BYTES", 1024 * 1024 * 100), \
             patch.object(_server, "detect_mimetype_from_bytes", return_value="application/pdf"), \
             patch.object(_server, "convert_with_markitdown", return_value=mock_result), \
             patch.object(_server, "calculate_quality_score", return_value=quality_result), \
             patch.object(_server, "classify_document", new=AsyncMock(return_value={
                 "document_type": "invoice",
                 "document_type_confidence": 0.95
             })):
            response = run_async(convert_auto(
                file_data=b"fake pdf content",
                filename="invoice.pdf",
                source="/data/invoice.pdf",
                source_type="file",
                input_meta={},
                classify=True,
                extract_schema=None,
            ))

        assert response.success is True
        assert response.meta.hints is not None
        assert any("invoice" in h.lower() for h in response.meta.hints)
        assert any("template='invoice'" in h for h in response.meta.hints)

    def test_hints_poor_quality(self):
        """A hint must appear when quality_grade is 'poor' and accuracy is not 'high'."""
        mock_result = {"success": True, "markdown": "x"}
        quality_result = {"quality_score": 0.2, "quality_grade": "poor"}

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "MAX_FILE_SIZE_BYTES", 1024 * 1024 * 100), \
             patch.object(_server, "detect_mimetype_from_bytes", return_value="application/pdf"), \
             patch.object(_server, "convert_with_markitdown", return_value=mock_result), \
             patch.object(_server, "calculate_quality_score", return_value=quality_result):
            response = run_async(convert_auto(
                file_data=b"fake content",
                filename="doc.pdf",
                source="/data/doc.pdf",
                source_type="file",
                input_meta={},
                accuracy="standard",
            ))

        assert response.success is True
        assert response.meta.hints is not None
        assert any("accuracy='high'" in h or "quality" in h.lower() for h in response.meta.hints)

    def test_hints_docx_without_describe_images(self):
        """A hint must appear for DOCX files when describe_images is not set."""
        mock_result = {"success": True, "markdown": "# Document\nContent here"}
        quality_result = {"quality_score": 0.85, "quality_grade": "good"}

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "MAX_FILE_SIZE_BYTES", 1024 * 1024 * 100), \
             patch.object(_server, "detect_mimetype_from_bytes", return_value="application/vnd.openxmlformats-officedocument.wordprocessingml.document"), \
             patch.object(_server, "convert_with_markitdown", return_value=mock_result), \
             patch.object(_server, "calculate_quality_score", return_value=quality_result):
            response = run_async(convert_auto(
                file_data=b"fake docx",
                filename="report.docx",
                source="/data/report.docx",
                source_type="file",
                input_meta={},
                describe_images=False,
            ))

        assert response.success is True
        assert response.meta.hints is not None
        assert any("describe_images" in h for h in response.meta.hints)

    def test_no_hints_when_all_set(self):
        """No hints about missing features when all relevant flags are correctly set."""
        mock_result = {"success": True, "markdown": "# Invoice\nFull content"}
        quality_result = {"quality_score": 0.9, "quality_grade": "excellent"}
        extracted_result = {"success": True, "extracted": {"invoice_number": "INV-001"}}

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "MAX_FILE_SIZE_BYTES", 1024 * 1024 * 100), \
             patch.object(_server, "detect_mimetype_from_bytes", return_value="application/pdf"), \
             patch.object(_server, "convert_with_markitdown", return_value=mock_result), \
             patch.object(_server, "calculate_quality_score", return_value=quality_result), \
             patch.object(_server, "extract_structured_data", new=AsyncMock(return_value=extracted_result)):
            response = run_async(convert_auto(
                file_data=b"fake pdf",
                filename="invoice.pdf",
                source="/data/invoice.pdf",
                source_type="file",
                input_meta={},
                extract_schema={"type": "object", "properties": {"invoice_number": {"type": "string"}}},
                accuracy="standard",
            ))

        assert response.success is True
        # No hints about missing extract_schema (it is set), no poor quality hints
        if response.meta.hints:
            assert not any("template='invoice'" in h for h in response.meta.hints)
            assert not any("quality" in h.lower() and "poor" in h.lower() for h in response.meta.hints)

    def test_hints_scanned_without_ocr(self):
        """A hint must appear for scanned PDFs when ocr_correct is not enabled."""
        mock_scanned_result = {
            "success": True,
            "markdown": "OCR text from scanned PDF",
            "vision_model": "pixtral-12b",
        }
        quality_result = {"quality_score": 0.5, "quality_grade": "fair"}

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "MAX_FILE_SIZE_BYTES", 1024 * 1024 * 100), \
             patch.object(_server, "detect_mimetype_from_bytes", return_value="application/pdf"), \
             patch.object(_server, "is_scanned_pdf", return_value=True), \
             patch.object(_server, "convert_scanned_pdf", new=AsyncMock(return_value=mock_scanned_result)), \
             patch.object(_server, "calculate_quality_score", return_value=quality_result):
            response = run_async(convert_auto(
                file_data=b"fake scanned pdf",
                filename="scan.pdf",
                source="/data/scan.pdf",
                source_type="file",
                input_meta={},
                ocr_correct=False,
                accuracy="standard",
            ))

        assert response.success is True
        assert response.meta.hints is not None
        assert any("ocr" in h.lower() or "scanned" in h.lower() for h in response.meta.hints)
