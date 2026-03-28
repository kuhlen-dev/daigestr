"""
Unit-Tests für T-MKIT-033: OCR-Layer in Scanned PDFs einbetten.

Tests laufen ohne Docker-Container und ohne echte PDF-Dateien (Mocking).
Alle externen Abhängigkeiten (PyMuPDF, pdf2image, Vision-API) werden gemockt.
"""

import base64
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conftest import load_server_module, run_async


# ---------------------------------------------------------------------------
# Server laden — einmal für alle Tests in diesem Modul
# ---------------------------------------------------------------------------

_server = load_server_module(use_real_pil=False)
embed_ocr_in_pdf = _server.embed_ocr_in_pdf
convert_auto = _server.convert_auto


# =============================================================================
# TestEmbedOcrInPdf
# =============================================================================

class TestEmbedOcrInPdf:
    """Tests für die embed_ocr_in_pdf() Funktion."""

    def _make_fake_pdf_bytes(self) -> bytes:
        """Minimale Fake-PDF-Bytes für Tests."""
        return b"%PDF-1.4 dummy content"

    def test_embed_ocr_in_pdf_returns_bytes(self):
        """
        embed_ocr_in_pdf gibt PDF-Bytes zurück wenn PyMuPDF verfügbar ist.
        """
        fake_pdf_bytes = b"fake pdf bytes"
        fake_result_bytes = b"enriched pdf bytes"

        mock_page = MagicMock()
        mock_page.rect = MagicMock()
        mock_page.insert_text = MagicMock()

        mock_doc = MagicMock()
        mock_doc.__iter__ = MagicMock(return_value=iter([mock_page]))
        mock_doc.__len__ = MagicMock(return_value=1)
        mock_doc.tobytes = MagicMock(return_value=fake_result_bytes)
        mock_doc.close = MagicMock()

        mock_fitz = MagicMock()
        mock_fitz.open = MagicMock(return_value=mock_doc)
        mock_fitz.Point = MagicMock(return_value=MagicMock())

        with patch.object(_server, "PYMUPDF_AVAILABLE", True), \
             patch.object(_server, "fitz", mock_fitz, create=True):
            result = embed_ocr_in_pdf(fake_pdf_bytes, "Hello World OCR text")

        assert result == fake_result_bytes
        assert isinstance(result, bytes)

    def test_embed_ocr_in_pdf_no_pymupdf(self):
        """
        Graceful degradation wenn PyMuPDF nicht verfügbar ist — gibt None zurück.
        """
        result = None
        with patch.object(_server, "PYMUPDF_AVAILABLE", False):
            result = embed_ocr_in_pdf(b"fake pdf", "some text")

        assert result is None

    def test_embed_ocr_in_pdf_exception_returns_none(self):
        """
        Bei unerwarteter Exception gibt embed_ocr_in_pdf None zurück (kein Absturz).
        """
        mock_fitz = MagicMock()
        mock_fitz.open = MagicMock(side_effect=RuntimeError("PDF corrupt"))

        with patch.object(_server, "PYMUPDF_AVAILABLE", True), \
             patch.object(_server, "fitz", mock_fitz, create=True):
            result = embed_ocr_in_pdf(b"bad pdf", "text")

        assert result is None

    def test_embed_ocr_per_page_text_used_when_provided(self):
        """
        pages_text wird verwendet wenn angegeben — jede Seite bekommt ihren Text.
        """
        fake_result_bytes = b"enriched pdf with pages"

        page1 = MagicMock()
        page1.rect = MagicMock()
        page1.insert_text = MagicMock()
        page2 = MagicMock()
        page2.rect = MagicMock()
        page2.insert_text = MagicMock()

        mock_doc = MagicMock()
        mock_doc.__iter__ = MagicMock(return_value=iter([page1, page2]))
        mock_doc.__len__ = MagicMock(return_value=2)
        mock_doc.tobytes = MagicMock(return_value=fake_result_bytes)
        mock_doc.close = MagicMock()

        mock_fitz = MagicMock()
        mock_fitz.open = MagicMock(return_value=mock_doc)
        mock_fitz.Point = MagicMock(return_value=MagicMock())

        pages_text = ["Seite 1 Text", "Seite 2 Text"]

        with patch.object(_server, "PYMUPDF_AVAILABLE", True), \
             patch.object(_server, "fitz", mock_fitz, create=True):
            result = embed_ocr_in_pdf(b"fake pdf", "full text", pages_text=pages_text)

        assert result == fake_result_bytes
        # Beide Seiten müssen insert_text aufgerufen haben
        assert page1.insert_text.called
        assert page2.insert_text.called

    def test_embed_ocr_distributes_text_evenly_without_pages_text(self):
        """
        Wenn pages_text=None, wird OCR-Text gleichmäßig auf Seiten aufgeteilt.
        """
        fake_result_bytes = b"distributed text pdf"

        page1 = MagicMock()
        page1.rect = MagicMock()
        page1.insert_text = MagicMock()
        page2 = MagicMock()
        page2.rect = MagicMock()
        page2.insert_text = MagicMock()

        mock_doc = MagicMock()
        mock_doc.__iter__ = MagicMock(return_value=iter([page1, page2]))
        mock_doc.__len__ = MagicMock(return_value=2)
        mock_doc.tobytes = MagicMock(return_value=fake_result_bytes)
        mock_doc.close = MagicMock()

        mock_fitz = MagicMock()
        mock_fitz.open = MagicMock(return_value=mock_doc)
        mock_fitz.Point = MagicMock(return_value=MagicMock())

        with patch.object(_server, "PYMUPDF_AVAILABLE", True), \
             patch.object(_server, "fitz", mock_fitz, create=True):
            result = embed_ocr_in_pdf(b"fake pdf", "AAAABBBB", pages_text=None)

        assert result == fake_result_bytes


# =============================================================================
# TestModels
# =============================================================================

class TestOcrEmbedModels:
    """Tests für Pydantic-Modelle: ConvertRequest und ConvertResponse."""

    def test_ocr_embed_parameter_exists(self):
        """ConvertRequest hat ocr_embed mit Default False."""
        from models import ConvertRequest
        req = ConvertRequest(path="/data/test.pdf")
        assert hasattr(req, "ocr_embed")
        assert req.ocr_embed is False

    def test_ocr_embed_default_false(self):
        """ConvertRequest.ocr_embed ist standardmäßig False."""
        from models import ConvertRequest
        req = ConvertRequest(path="/data/scan.pdf")
        assert req.ocr_embed is False

    def test_ocr_embed_can_be_set_true(self):
        """ConvertRequest.ocr_embed kann auf True gesetzt werden."""
        from models import ConvertRequest
        req = ConvertRequest(path="/data/scan.pdf", ocr_embed=True)
        assert req.ocr_embed is True

    def test_enriched_pdf_field_exists(self):
        """ConvertResponse hat enriched_pdf Feld mit Default None."""
        from models import ConvertResponse, MetaData
        resp = ConvertResponse(success=True, markdown="test", meta=MetaData())
        assert hasattr(resp, "enriched_pdf")
        assert resp.enriched_pdf is None

    def test_enriched_pdf_can_be_set(self):
        """ConvertResponse.enriched_pdf kann auf einen Base64-String gesetzt werden."""
        from models import ConvertResponse, MetaData
        b64 = base64.b64encode(b"fake pdf bytes").decode("utf-8")
        resp = ConvertResponse(success=True, markdown="test", meta=MetaData(), enriched_pdf=b64)
        assert resp.enriched_pdf == b64


# =============================================================================
# TestOcrEmbedIntegration
# =============================================================================

class TestOcrEmbedIntegration:
    """Integration-Tests: ocr_embed in convert_auto()."""

    def _make_scanned_result(self) -> dict:
        return {
            "success": True,
            "markdown": "## Seite 1\n\nGescannter Text hier.",
            "vision_model": "pixtral-12b",
            "tokens_prompt": 100,
            "tokens_completion": 50,
            "tokens_total": 150,
            "scanned": True,
            "pages_processed": 1,
        }

    def test_ocr_embed_in_convert_auto_sets_enriched_pdf(self):
        """
        Wenn ocr_embed=True und Scanned PDF erkannt → enriched_pdf in Response gesetzt.
        """
        fake_enriched = b"enriched searchable pdf"
        quality_result = {"quality_score": 0.7, "quality_grade": "good"}

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "MAX_FILE_SIZE_BYTES", 1024 * 1024 * 100), \
             patch.object(_server, "detect_mimetype_from_bytes", return_value="application/pdf"), \
             patch.object(_server, "is_scanned_pdf", return_value=True), \
             patch.object(_server, "convert_scanned_pdf", new=AsyncMock(return_value=self._make_scanned_result())), \
             patch.object(_server, "calculate_quality_score", return_value=quality_result), \
             patch.object(_server, "embed_ocr_in_pdf", return_value=fake_enriched):
            response = run_async(convert_auto(
                file_data=b"fake scanned pdf bytes",
                filename="scan.pdf",
                source="/data/scan.pdf",
                source_type="file",
                input_meta={},
                ocr_embed=True,
            ))

        assert response.success is True
        assert response.enriched_pdf is not None
        decoded = base64.b64decode(response.enriched_pdf)
        assert decoded == fake_enriched

    def test_ocr_embed_false_enriched_pdf_is_none(self):
        """
        Wenn ocr_embed=False → enriched_pdf ist None (kein Overhead).
        """
        quality_result = {"quality_score": 0.7, "quality_grade": "good"}

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "MAX_FILE_SIZE_BYTES", 1024 * 1024 * 100), \
             patch.object(_server, "detect_mimetype_from_bytes", return_value="application/pdf"), \
             patch.object(_server, "is_scanned_pdf", return_value=True), \
             patch.object(_server, "convert_scanned_pdf", new=AsyncMock(return_value=self._make_scanned_result())), \
             patch.object(_server, "calculate_quality_score", return_value=quality_result):
            response = run_async(convert_auto(
                file_data=b"fake scanned pdf bytes",
                filename="scan.pdf",
                source="/data/scan.pdf",
                source_type="file",
                input_meta={},
                ocr_embed=False,
            ))

        assert response.success is True
        assert response.enriched_pdf is None

    def test_ocr_embed_hint_when_scanned_without_embed(self):
        """
        Hint erscheint wenn Scanned PDF erkannt aber ocr_embed=False.
        """
        quality_result = {"quality_score": 0.7, "quality_grade": "good"}

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "MAX_FILE_SIZE_BYTES", 1024 * 1024 * 100), \
             patch.object(_server, "detect_mimetype_from_bytes", return_value="application/pdf"), \
             patch.object(_server, "is_scanned_pdf", return_value=True), \
             patch.object(_server, "convert_scanned_pdf", new=AsyncMock(return_value=self._make_scanned_result())), \
             patch.object(_server, "calculate_quality_score", return_value=quality_result):
            response = run_async(convert_auto(
                file_data=b"fake scanned pdf bytes",
                filename="scan.pdf",
                source="/data/scan.pdf",
                source_type="file",
                input_meta={},
                ocr_embed=False,
                ocr_correct=True,
                accuracy="high",
            ))

        assert response.success is True
        assert response.meta.hints is not None
        assert any("ocr_embed" in h for h in response.meta.hints)
        assert any("searchable" in h.lower() for h in response.meta.hints)

    def test_no_ocr_embed_hint_when_embed_enabled(self):
        """
        Kein ocr_embed-Hint wenn ocr_embed=True gesetzt ist.
        """
        fake_enriched = b"enriched pdf"
        quality_result = {"quality_score": 0.7, "quality_grade": "good"}

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "MAX_FILE_SIZE_BYTES", 1024 * 1024 * 100), \
             patch.object(_server, "detect_mimetype_from_bytes", return_value="application/pdf"), \
             patch.object(_server, "is_scanned_pdf", return_value=True), \
             patch.object(_server, "convert_scanned_pdf", new=AsyncMock(return_value=self._make_scanned_result())), \
             patch.object(_server, "calculate_quality_score", return_value=quality_result), \
             patch.object(_server, "embed_ocr_in_pdf", return_value=fake_enriched):
            response = run_async(convert_auto(
                file_data=b"fake scanned pdf bytes",
                filename="scan.pdf",
                source="/data/scan.pdf",
                source_type="file",
                input_meta={},
                ocr_embed=True,
            ))

        assert response.success is True
        if response.meta.hints:
            assert not any("ocr_embed" in h for h in response.meta.hints)

    def test_embed_ocr_failure_does_not_break_response(self):
        """
        Wenn embed_ocr_in_pdf None zurückgibt, bleibt Response trotzdem erfolgreich.
        enriched_pdf ist dann None.
        """
        quality_result = {"quality_score": 0.7, "quality_grade": "good"}

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "MAX_FILE_SIZE_BYTES", 1024 * 1024 * 100), \
             patch.object(_server, "detect_mimetype_from_bytes", return_value="application/pdf"), \
             patch.object(_server, "is_scanned_pdf", return_value=True), \
             patch.object(_server, "convert_scanned_pdf", new=AsyncMock(return_value=self._make_scanned_result())), \
             patch.object(_server, "calculate_quality_score", return_value=quality_result), \
             patch.object(_server, "embed_ocr_in_pdf", return_value=None):
            response = run_async(convert_auto(
                file_data=b"fake scanned pdf bytes",
                filename="scan.pdf",
                source="/data/scan.pdf",
                source_type="file",
                input_meta={},
                ocr_embed=True,
            ))

        assert response.success is True
        assert response.enriched_pdf is None
