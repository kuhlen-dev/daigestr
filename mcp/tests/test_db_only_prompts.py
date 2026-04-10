"""
Tests für E8/W8.1/T8.1.3: DB-only Prompt- und Registry-Kontrakte.
"""

from unittest.mock import patch

import pytest

from conftest import load_server_module, run_async


_server = load_server_module(use_real_pil=False)


class TestDbOnlyPromptContracts:
    """DB-gesteuerte Prompts und Meta-Konfiguration dürfen nicht auf Hardcode fallen."""

    def setup_method(self):
        _server.get_meta_schema.__globals__["_META_SCHEMA"] = None
        _server.get_steuer_signalwoerter.__globals__["_STEUER_SIGNALWOERTER"] = None
        _server.get_datentyp_konventionen.__globals__["_DATENTYP_KONVENTIONEN"] = None

    def test_get_meta_schema_raises_when_prompt_missing(self):
        with patch.object(_server, "get_prompt", side_effect=ValueError("Prompt nicht gefunden")):
            with pytest.raises(ValueError, match="Prompt nicht gefunden"):
                _server.get_meta_schema()

    def test_get_steuer_signalwoerter_raises_when_prompt_missing(self):
        with patch.object(_server, "get_prompt", side_effect=ValueError("Prompt nicht gefunden")):
            with pytest.raises(ValueError, match="Prompt nicht gefunden"):
                _server.get_steuer_signalwoerter()

    def test_get_datentyp_konventionen_raises_when_prompt_missing(self):
        with patch.object(_server, "get_prompt", side_effect=ValueError("Prompt nicht gefunden")):
            with pytest.raises(ValueError, match="Prompt nicht gefunden"):
                _server.get_datentyp_konventionen()

    def test_dual_pass_validate_raises_when_prompt_missing(self):
        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "get_prompt", side_effect=ValueError("Prompt nicht gefunden")):
            with pytest.raises(ValueError, match="Prompt nicht gefunden"):
                run_async(
                    _server.dual_pass_validate(
                        markdown="OCR text",
                        file_data=b"image-bytes",
                        mimetype="image/png",
                    )
                )

    def test_classify_document_raises_when_prompt_missing(self):
        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "get_prompt", side_effect=ValueError("Prompt nicht gefunden")):
            with pytest.raises(ValueError, match="Prompt nicht gefunden"):
                run_async(_server.classify_document("Dokument", categories=["invoice"]))

    def test_correct_ocr_text_raises_when_prompt_missing(self):
        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "get_prompt", side_effect=ValueError("Prompt nicht gefunden")):
            with pytest.raises(ValueError, match="Prompt nicht gefunden"):
                run_async(_server.correct_ocr_text("OCR Text"))

    def test_extract_structured_data_raises_when_prompt_missing(self):
        schema = {"type": "object", "properties": {"value": {"type": "string"}}}
        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "get_prompt", side_effect=ValueError("Prompt nicht gefunden")):
            with pytest.raises(ValueError, match="Prompt nicht gefunden"):
                run_async(_server.extract_structured_data("Dokument", schema))
