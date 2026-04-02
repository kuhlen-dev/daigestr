"""
Tests für T-DAI-015: mode='full' Meta-Parameter.

Alle Tests laufen ohne Docker-Container und ohne echte API-Calls.
Alle externen Abhängigkeiten werden per unittest.mock gemockt.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conftest import load_server_module, run_async, PNG_100x100


# Einmal laden; alle Tests in diesem Modul teilen diese Instanz
_server = load_server_module(use_real_pil=False)
convert_auto = _server.convert_auto


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pdf_bytes() -> bytes:
    """Minimal-PDF-Datei als Bytes."""
    return b"%PDF-1.4 minimal"


def _make_vision_response(content: str, tokens_total: int = 50) -> dict:
    """Erzeugt eine minimale Mistral Vision API-Antwort."""
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {
            "prompt_tokens": 20,
            "completion_tokens": 30,
            "total_tokens": tokens_total,
        },
    }


def _make_classify_response(doc_type: str = "invoice", confidence: float = 0.9) -> dict:
    """Erzeugt eine Mistral-Antwort für Klassifizierung."""
    import json
    return {
        "choices": [{"message": {"content": json.dumps({"type": doc_type, "confidence": confidence})}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def _base_convert_kwargs(**overrides) -> dict:
    """Liefert minimale Kwargs für convert_auto (Bild-Datei)."""
    defaults = dict(
        file_data=PNG_100x100,
        filename="test.png",
        source="test.png",
        source_type="base64",
        input_meta={},
        language="de",
    )
    defaults.update(overrides)
    return defaults


# ---------------------------------------------------------------------------
# Tests: ConvertRequest Validation (models.py)
# ---------------------------------------------------------------------------


class TestModeFieldValidation:
    """mode-Feld in ConvertRequest: Validator prüft erlaubte Werte."""

    def test_mode_default_is_valid(self):
        """mode='default' ist gültig."""
        from models import ConvertRequest
        req = ConvertRequest(path="/data/test.pdf", mode="default")
        assert req.mode == "default"

    def test_mode_full_is_valid(self):
        """mode='full' ist gültig."""
        from models import ConvertRequest
        req = ConvertRequest(path="/data/test.pdf", mode="full")
        assert req.mode == "full"

    def test_mode_default_value(self):
        """Kein mode-Parameter → Default ist 'default'."""
        from models import ConvertRequest
        req = ConvertRequest(path="/data/test.pdf")
        assert req.mode == "default"

    def test_mode_invalid_raises_validation_error(self):
        """Ungültiger mode-Wert → ValidationError."""
        from pydantic import ValidationError
        from models import ConvertRequest
        with pytest.raises(ValidationError) as exc_info:
            ConvertRequest(path="/data/test.pdf", mode="fast")
        assert "mode must be 'default', 'full' or 'deep'" in str(exc_info.value)

    def test_mode_invalid_partial_raises(self):
        """Teilstring eines gültigen Werts → ValidationError."""
        from pydantic import ValidationError
        from models import ConvertRequest
        with pytest.raises(ValidationError):
            ConvertRequest(path="/data/test.pdf", mode="ful")

    def test_mode_empty_string_raises(self):
        """Leerer String → ValidationError."""
        from pydantic import ValidationError
        from models import ConvertRequest
        with pytest.raises(ValidationError):
            ConvertRequest(path="/data/test.pdf", mode="")


# ---------------------------------------------------------------------------
# Tests: mode='full' aktiviert describe_images
# ---------------------------------------------------------------------------


class TestModeFullDescribeImages:
    """mode='full' setzt describe_images=True in convert_auto."""

    def test_mode_full_triggers_image_description(self):
        """
        mode='full' bei PNG → Vision wird aufgerufen (describe_images=True impliziert durch mode).
        Wir prüfen, dass der Aufruf ohne Fehler erfolgreich ist und vision_used=True.
        """
        vision_resp = _make_vision_response("# Bildbeschreibung")

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "call_mistral_vision_api", new=AsyncMock(return_value=vision_resp)):
            result = run_async(convert_auto(**_base_convert_kwargs(mode="full")))

        assert result.success is True
        # Vision wurde genutzt (describe_images=True → Vision-Pfad für PNG)
        assert result.meta.vision_used is True


# ---------------------------------------------------------------------------
# Tests: mode='full' aktiviert accuracy='high'
# ---------------------------------------------------------------------------


class TestModeFullAccuracyHigh:
    """mode='full' setzt accuracy='high' in convert_auto."""

    def test_mode_full_sets_accuracy_high(self):
        """
        mode='full' → accuracy_mode im Meta muss 'high' sein.
        """
        vision_resp = _make_vision_response("# Result")
        dual_pass_resp = _make_vision_response("# Validated Result")

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "call_mistral_vision_api", new=AsyncMock(
                 side_effect=[vision_resp, dual_pass_resp, vision_resp, dual_pass_resp,
                               vision_resp, dual_pass_resp, vision_resp, dual_pass_resp]
             )):
            result = run_async(convert_auto(**_base_convert_kwargs(mode="full")))

        assert result.meta.accuracy_mode == "high"


# ---------------------------------------------------------------------------
# Tests: mode='full' aktiviert classify
# ---------------------------------------------------------------------------


class TestModeFullClassify:
    """mode='full' setzt classify=True in convert_auto."""

    def test_mode_full_triggers_classify(self):
        """
        mode='full' bei PNG → classify wird ausgeführt → document_type in meta.
        """
        vision_resp = _make_vision_response("# Rechnungsinhalt")
        classify_resp = _make_classify_response("invoice", 0.92)
        # Genug Antworten für vision + dual_pass (high) + classify + weitere Aufrufe
        side_effects = [vision_resp, vision_resp, classify_resp] + [vision_resp] * 10

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "call_mistral_vision_api", new=AsyncMock(
                 side_effect=side_effects
             )):
            result = run_async(convert_auto(**_base_convert_kwargs(mode="full")))

        assert result.success is True
        assert result.meta.document_type is not None


# ---------------------------------------------------------------------------
# Tests: mode='full' aktiviert chunk
# ---------------------------------------------------------------------------


class TestModeFullChunk:
    """mode='full' setzt chunk=True in convert_auto."""

    def test_mode_full_produces_chunks(self):
        """
        mode='full' bei PNG → chunk=True → chunks-Feld ist nicht None und nicht leer.
        """
        vision_resp = _make_vision_response("# Kapitel 1\n\nInhalt des Dokuments.\n\n## Abschnitt A\n\nWeiterer Inhalt.")
        classify_resp = _make_classify_response("other", 0.5)
        side_effects = [vision_resp, vision_resp, classify_resp] + [vision_resp] * 10

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "call_mistral_vision_api", new=AsyncMock(
                 side_effect=side_effects
             )):
            result = run_async(convert_auto(**_base_convert_kwargs(mode="full")))

        assert result.success is True
        # chunk=True → chunks ist eine Liste
        assert result.chunks is not None
        assert isinstance(result.chunks, list)


# ---------------------------------------------------------------------------
# Tests: mode='default' ändert Defaults nicht
# ---------------------------------------------------------------------------


class TestModeDefault:
    """mode='default' lässt individuelle Parameter unverändert."""

    def test_mode_default_no_chunks(self):
        """
        mode='default' ohne chunk=True → chunks ist None.
        """
        vision_resp = _make_vision_response("# Normal Result")

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "call_mistral_vision_api", new=AsyncMock(return_value=vision_resp)):
            result = run_async(convert_auto(**_base_convert_kwargs(mode="default")))

        assert result.success is True
        assert result.chunks is None

    def test_mode_default_accuracy_is_standard(self):
        """
        mode='default' → accuracy_mode bleibt 'standard'.
        """
        vision_resp = _make_vision_response("# Normal Result")

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "call_mistral_vision_api", new=AsyncMock(return_value=vision_resp)):
            result = run_async(convert_auto(**_base_convert_kwargs(mode="default")))

        assert result.meta.accuracy_mode == "standard"

    def test_mode_default_no_classify(self):
        """
        mode='default' ohne classify=True → document_type bleibt None.
        """
        vision_resp = _make_vision_response("# Normal Result")

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "call_mistral_vision_api", new=AsyncMock(return_value=vision_resp)):
            result = run_async(convert_auto(**_base_convert_kwargs(mode="default")))

        assert result.meta.document_type is None


# ---------------------------------------------------------------------------
# Tests: mode='full' überschreibt individuelle Parameter (full hat Vorrang)
# ---------------------------------------------------------------------------


class TestModeFullOverridesIndividualParams:
    """
    mode='full' setzt alle Features auf True/high, auch wenn sie explizit
    auf False/standard gesetzt wurden. Da wir keine Unterscheidung zwischen
    'nicht gesetzt' und 'False' haben, hat mode='full' immer Vorrang.
    """

    def test_mode_full_overrides_describe_images_false(self):
        """
        mode='full' + describe_images=False → mode='full' gewinnt → Vision wird genutzt.
        """
        vision_resp = _make_vision_response("# Override Test")
        side_effects = [vision_resp] * 20

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "call_mistral_vision_api", new=AsyncMock(
                 side_effect=side_effects
             )):
            result = run_async(convert_auto(**_base_convert_kwargs(
                mode="full",
                describe_images=False,  # Explizit False — wird durch full überschrieben
            )))

        assert result.success is True
        assert result.meta.vision_used is True

    def test_mode_full_overrides_accuracy_standard(self):
        """
        mode='full' + accuracy='standard' → mode='full' gewinnt → accuracy_mode='high'.
        """
        vision_resp = _make_vision_response("# Override Accuracy")
        side_effects = [vision_resp] * 20

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "call_mistral_vision_api", new=AsyncMock(
                 side_effect=side_effects
             )):
            result = run_async(convert_auto(**_base_convert_kwargs(
                mode="full",
                accuracy="standard",  # Explizit standard — wird durch full überschrieben
            )))

        assert result.meta.accuracy_mode == "high"

    def test_mode_full_overrides_chunk_false(self):
        """
        mode='full' + chunk=False → mode='full' gewinnt → chunks wird befüllt.
        """
        vision_resp = _make_vision_response("# Heading\n\nInhalt.\n\n## Abschnitt\n\nMehr Inhalt.")
        classify_resp = _make_classify_response("other", 0.4)
        side_effects = [vision_resp, vision_resp, classify_resp] + [vision_resp] * 10

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "call_mistral_vision_api", new=AsyncMock(
                 side_effect=side_effects
             )):
            result = run_async(convert_auto(**_base_convert_kwargs(
                mode="full",
                chunk=False,  # Explizit False — wird durch full überschrieben
            )))

        assert result.chunks is not None
        assert isinstance(result.chunks, list)


# ---------------------------------------------------------------------------
# Tests: mode='deep' Validation (T-DAI-030)
# ---------------------------------------------------------------------------


class TestModeDeepValidation:
    """mode='deep' ist gültig in ConvertRequest."""

    def test_mode_deep_is_valid(self):
        from models import ConvertRequest
        req = ConvertRequest(path="/data/test.pdf", mode="deep")
        assert req.mode == "deep"


# ---------------------------------------------------------------------------
# Tests: mode='deep' setzt describe_pages UND describe_images (T-DAI-030)
# ---------------------------------------------------------------------------


def _setup_pdf_server_mocks(server_mod, extra_mocks=None):
    """Gemeinsame Mock-Setup für PDF-basierte convert_auto Tests.

    Setzt alle nötigen Mocks auf dem server_mod damit _get() sie findet.
    """
    server_mod.CACHE_ENABLED = False
    server_mod.detect_mimetype_from_bytes = MagicMock(return_value="application/pdf")
    server_mod.get_mimetype = MagicMock(return_value="application/pdf")
    server_mod.convert_with_markitdown = MagicMock(return_value={"markdown": "# Test", "title": "Test", "success": True})
    server_mod.is_scanned_pdf = MagicMock(return_value=False)
    server_mod.render_pdf_pages_as_images = MagicMock(return_value=[
        {"name": "page_1.png", "data": PNG_100x100, "page_number": 1}
    ])
    server_mod.describe_page_images = AsyncMock(return_value=[
        {"name": "page_1.png", "description": "Page 1 content", "page_number": 1, "tokens": 100}
    ])
    server_mod.insert_page_descriptions = MagicMock(side_effect=lambda md, descs: md + "\n\nPage desc")
    server_mod.extract_images_from_pdf = MagicMock(return_value=[
        {"name": "img1.png", "data": PNG_100x100, "page_number": 1}
    ])
    server_mod.describe_embedded_images = AsyncMock(return_value=[
        {"name": "img1.png", "description": "An image", "tokens": 50, "image_type": "photo"}
    ])
    server_mod.insert_image_descriptions = MagicMock(side_effect=lambda md, descs: md + "\n\nImg desc")
    server_mod.classify_document = AsyncMock(return_value={"document_type": "report", "confidence": 0.95})
    server_mod.calculate_quality_score = MagicMock(return_value={"quality_grade": "B"})
    server_mod._apply_auto_extract = AsyncMock(side_effect=lambda response, *a, **kw: response)
    server_mod.chunk_markdown = MagicMock(return_value=[{"text": "chunk1"}])
    server_mod.correct_ocr_text = AsyncMock(return_value=("# Test", 0))
    server_mod.dual_pass_validate = AsyncMock(side_effect=lambda **kw: kw["markdown"])
    server_mod.render_first_page_as_image = MagicMock(return_value=None)
    server_mod.cache_get = MagicMock(return_value=None)
    server_mod.cache_set = MagicMock(return_value=None)
    if extra_mocks:
        for name, mock in extra_mocks.items():
            setattr(server_mod, name, mock)
    return server_mod


class TestModeDeep:
    """T-DAI-030: mode='deep' setzt describe_pages UND describe_images."""

    def test_mode_deep_sets_describe_pages_and_describe_images(self):
        _server = load_server_module()
        _setup_pdf_server_mocks(_server)

        result = run_async(_server.convert_auto(
            file_data=b"%PDF-fake",
            filename="test.pdf",
            source="/data/test.pdf",
            source_type="file",
            input_meta={},
            mode="deep",
        ))
        # deep mode: both page rendering AND image extraction should be called
        assert _server.render_pdf_pages_as_images.called, "Page rendering should be called in deep mode"
        assert _server.extract_images_from_pdf.called, "Image extraction should be called in deep mode"


# ---------------------------------------------------------------------------
# Tests: mode='full' nutzt Page-Rendering statt Einzelbild-Extraktion (T-DAI-030)
# ---------------------------------------------------------------------------


class TestModeFullPageRendering:
    """T-DAI-030: mode='full' nutzt Page-Rendering statt Einzelbild-Extraktion für PDFs."""

    def test_mode_full_pdf_uses_page_rendering(self):
        _server = load_server_module()
        _setup_pdf_server_mocks(_server, extra_mocks={
            "extract_images_from_pdf": MagicMock(return_value=[]),
        })

        result = run_async(_server.convert_auto(
            file_data=b"%PDF-fake",
            filename="test.pdf",
            source="/data/test.pdf",
            source_type="file",
            input_meta={},
            mode="full",
        ))
        assert _server.render_pdf_pages_as_images.called, "Page rendering should be called"
        assert not _server.extract_images_from_pdf.called, \
            "Individual image extraction should NOT be called in full mode for PDFs"


# ---------------------------------------------------------------------------
# Tests: mode='full' Non-PDF Fallback auf describe_images (T-DAI-030)
# ---------------------------------------------------------------------------


class TestModeFullNonPdfFallback:
    """T-DAI-030: mode='full' fällt bei Non-PDF auf describe_images zurück."""

    def test_mode_full_docx_uses_describe_images(self):
        _server = load_server_module()
        _server.CACHE_ENABLED = False
        _server.detect_mimetype_from_bytes = MagicMock(return_value="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        _server.get_mimetype = MagicMock(return_value="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        _server.convert_with_markitdown = MagicMock(return_value={"markdown": "# DOCX", "title": "DOCX", "success": True})
        _server.extract_images_from_docx = MagicMock(return_value=[
            {"name": "img1.png", "data": PNG_100x100}
        ])
        _server.describe_embedded_images = AsyncMock(return_value=[
            {"name": "img1.png", "description": "An image", "tokens": 50, "image_type": "photo"}
        ])
        _server.insert_image_descriptions = MagicMock(side_effect=lambda md, descs: md)
        _server.classify_document = AsyncMock(return_value={"document_type": "letter", "confidence": 0.9})
        _server.calculate_quality_score = MagicMock(return_value={"quality_grade": "B"})
        _server._apply_auto_extract = AsyncMock(side_effect=lambda response, *a, **kw: response)
        _server.chunk_markdown = MagicMock(return_value=[{"text": "chunk1"}])
        _server.correct_ocr_text = AsyncMock(return_value=("# DOCX", 0))
        _server.cache_get = MagicMock(return_value=None)
        _server.cache_set = MagicMock(return_value=None)

        result = run_async(_server.convert_auto(
            file_data=b"PK\x03\x04fake-docx",
            filename="test.docx",
            source="/data/test.docx",
            source_type="file",
            input_meta={},
            mode="full",
        ))
        # For non-PDF, full mode should fall back to describe_images
        assert _server.describe_embedded_images.called, "describe_images should be called for non-PDF in full mode"
