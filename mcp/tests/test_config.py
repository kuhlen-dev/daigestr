"""
Tests für T-MKIT-017: Hardcodierte Config-Werte via ENV-Variablen konfigurierbar.

Prüft dass alle neuen Config-Variablen:
1. Via os.getenv gesetzt werden
2. Sinnvolle Defaults haben
3. Korrekt in den Funktionen verwendet werden
"""

import os
import sys
import importlib
import re
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from conftest import load_server_module, run_async


class TestConfigDefaults:
    """Prüft dass alle Config-Variablen sinnvolle Defaults haben."""

    def setup_method(self):
        self.server = load_server_module()

    def test_mistral_text_model_default(self):
        assert self.server.MISTRAL_TEXT_MODEL == "mistral-small-2603"

    def test_vision_max_tokens_default(self):
        assert self.server.VISION_MAX_TOKENS == 16384
        assert isinstance(self.server.VISION_MAX_TOKENS, int)

    def test_classify_max_tokens_default(self):
        assert self.server.CLASSIFY_MAX_TOKENS == 1024
        assert isinstance(self.server.CLASSIFY_MAX_TOKENS, int)

    def test_extract_max_tokens_default(self):
        assert self.server.EXTRACT_MAX_TOKENS == 16384
        assert isinstance(self.server.EXTRACT_MAX_TOKENS, int)

    def test_ocr_correct_max_tokens_default(self):
        assert self.server.OCR_CORRECT_MAX_TOKENS == 16384
        assert isinstance(self.server.OCR_CORRECT_MAX_TOKENS, int)

    def test_classify_max_chars_default(self):
        assert self.server.CLASSIFY_MAX_CHARS == 32000
        assert isinstance(self.server.CLASSIFY_MAX_CHARS, int)

    def test_extract_max_chars_default(self):
        assert self.server.EXTRACT_MAX_CHARS == 32000
        assert isinstance(self.server.EXTRACT_MAX_CHARS, int)

    def test_default_language(self):
        assert self.server.DEFAULT_LANGUAGE == "de"

    def test_min_image_size_px_default(self):
        assert self.server.MIN_IMAGE_SIZE_PX == 50
        assert isinstance(self.server.MIN_IMAGE_SIZE_PX, int)

    def test_pdf_render_dpi_default(self):
        assert self.server.PDF_RENDER_DPI == 200
        assert isinstance(self.server.PDF_RENDER_DPI, int)

    def test_pdftotext_timeout_default(self):
        assert self.server.PDFTOTEXT_TIMEOUT == 60
        assert isinstance(self.server.PDFTOTEXT_TIMEOUT, int)

    def test_pdfinfo_timeout_default(self):
        assert self.server.PDFINFO_TIMEOUT == 30
        assert isinstance(self.server.PDFINFO_TIMEOUT, int)

    def test_ffmpeg_timeout_default(self):
        assert self.server.FFMPEG_TIMEOUT == 600
        assert isinstance(self.server.FFMPEG_TIMEOUT, int)

    def test_skip_files_default(self):
        assert isinstance(self.server.SKIP_FILES, set)
        assert "email.md" in self.server.SKIP_FILES
        assert "consolidated.md" in self.server.SKIP_FILES
        assert "metadata.json" in self.server.SKIP_FILES
        assert ".DS_Store" in self.server.SKIP_FILES
        assert "Thumbs.db" in self.server.SKIP_FILES

    def test_whisper_device_default(self):
        assert self.server.WHISPER_DEVICE == "cpu"

    def test_whisper_compute_type_default(self):
        assert self.server.WHISPER_COMPUTE_TYPE == "int8"


class TestConfigEnvOverride:
    """Prüft dass ENV-Variablen die Defaults korrekt überschreiben."""

    def test_mistral_text_model_override(self):
        with patch.dict(os.environ, {"MISTRAL_TEXT_MODEL": "mistral-large-latest"}):
            server = load_server_module()
            assert server.MISTRAL_TEXT_MODEL == "mistral-large-latest"

    def test_vision_max_tokens_override(self):
        with patch.dict(os.environ, {"VISION_MAX_TOKENS": "8192"}):
            server = load_server_module()
            assert server.VISION_MAX_TOKENS == 8192

    def test_classify_max_tokens_override(self):
        with patch.dict(os.environ, {"CLASSIFY_MAX_TOKENS": "512"}):
            server = load_server_module()
            assert server.CLASSIFY_MAX_TOKENS == 512

    def test_extract_max_tokens_override(self):
        with patch.dict(os.environ, {"EXTRACT_MAX_TOKENS": "4096"}):
            server = load_server_module()
            assert server.EXTRACT_MAX_TOKENS == 4096

    def test_ocr_correct_max_tokens_override(self):
        with patch.dict(os.environ, {"OCR_CORRECT_MAX_TOKENS": "8192"}):
            server = load_server_module()
            assert server.OCR_CORRECT_MAX_TOKENS == 8192

    def test_classify_max_chars_override(self):
        with patch.dict(os.environ, {"CLASSIFY_MAX_CHARS": "4000"}):
            server = load_server_module()
            assert server.CLASSIFY_MAX_CHARS == 4000

    def test_extract_max_chars_override(self):
        with patch.dict(os.environ, {"EXTRACT_MAX_CHARS": "6000"}):
            server = load_server_module()
            assert server.EXTRACT_MAX_CHARS == 6000

    def test_default_language_override(self):
        with patch.dict(os.environ, {"DEFAULT_LANGUAGE": "en"}):
            server = load_server_module()
            assert server.DEFAULT_LANGUAGE == "en"

    def test_min_image_size_px_override(self):
        with patch.dict(os.environ, {"MIN_IMAGE_SIZE_PX": "100"}):
            server = load_server_module()
            assert server.MIN_IMAGE_SIZE_PX == 100

    def test_pdf_render_dpi_override(self):
        with patch.dict(os.environ, {"PDF_RENDER_DPI": "300"}):
            server = load_server_module()
            assert server.PDF_RENDER_DPI == 300

    def test_pdftotext_timeout_override(self):
        with patch.dict(os.environ, {"PDFTOTEXT_TIMEOUT": "120"}):
            server = load_server_module()
            assert server.PDFTOTEXT_TIMEOUT == 120

    def test_pdfinfo_timeout_override(self):
        with patch.dict(os.environ, {"PDFINFO_TIMEOUT": "15"}):
            server = load_server_module()
            assert server.PDFINFO_TIMEOUT == 15

    def test_ffmpeg_timeout_override(self):
        with patch.dict(os.environ, {"FFMPEG_TIMEOUT": "300"}):
            server = load_server_module()
            assert server.FFMPEG_TIMEOUT == 300

    def test_skip_files_override(self):
        with patch.dict(os.environ, {"SKIP_FILES": "custom.md,ignore.txt"}):
            server = load_server_module()
            assert server.SKIP_FILES == {"custom.md", "ignore.txt"}

    def test_whisper_device_override(self):
        with patch.dict(os.environ, {"WHISPER_DEVICE": "cuda"}):
            server = load_server_module()
            assert server.WHISPER_DEVICE == "cuda"

    def test_whisper_compute_type_override(self):
        with patch.dict(os.environ, {"WHISPER_COMPUTE_TYPE": "float16"}):
            server = load_server_module()
            assert server.WHISPER_COMPUTE_TYPE == "float16"


class TestConfigUsedInFunctions:
    """Prüft dass die Config-Werte tatsächlich in den Funktionen verwendet werden."""

    def setup_method(self):
        self.server = load_server_module()

    def test_classify_uses_text_model(self, monkeypatch):
        """classify_document soll MISTRAL_TEXT_MODEL verwenden, nicht MISTRAL_VISION_MODEL."""
        captured_payloads = []

        async def mock_call_mistral(payload, **_kwargs):
            captured_payloads.append(payload)
            return {
                "choices": [{"message": {"content": '{"type": "invoice", "confidence": 0.9}'}}],
                "usage": {"total_tokens": 10},
            }

        monkeypatch.setattr(self.server, "call_mistral_vision_api", mock_call_mistral)
        monkeypatch.setattr(self.server, "MISTRAL_API_KEY", "test-key")
        monkeypatch.setattr(self.server, "MISTRAL_TEXT_MODEL", "mistral-small-2603")

        run_async(self.server.classify_document("test document"))

        assert len(captured_payloads) == 1
        assert captured_payloads[0]["model"] == "mistral-small-2603"

    def test_classify_uses_max_tokens(self, monkeypatch):
        """classify_document soll CLASSIFY_MAX_TOKENS verwenden."""
        captured_payloads = []

        async def mock_call_mistral(payload, **_kwargs):
            captured_payloads.append(payload)
            return {
                "choices": [{"message": {"content": '{"type": "invoice", "confidence": 0.9}'}}],
                "usage": {"total_tokens": 10},
            }

        monkeypatch.setattr(self.server, "call_mistral_vision_api", mock_call_mistral)
        monkeypatch.setattr(self.server, "MISTRAL_API_KEY", "test-key")
        monkeypatch.setattr(self.server, "CLASSIFY_MAX_TOKENS", 512)

        run_async(self.server.classify_document("test document"))

        assert captured_payloads[0]["max_tokens"] == 512

    def test_classify_truncates_at_max_chars(self, monkeypatch):
        """classify_document soll Text auf CLASSIFY_MAX_CHARS kürzen."""
        captured_payloads = []

        async def mock_call_mistral(payload, **_kwargs):
            captured_payloads.append(payload)
            return {
                "choices": [{"message": {"content": '{"type": "invoice", "confidence": 0.9}'}}],
                "usage": {"total_tokens": 10},
            }

        monkeypatch.setattr(self.server, "call_mistral_vision_api", mock_call_mistral)
        monkeypatch.setattr(self.server, "MISTRAL_API_KEY", "test-key")
        monkeypatch.setattr(self.server, "CLASSIFY_MAX_CHARS", 100)

        long_text = "x" * 500
        run_async(self.server.classify_document(long_text))

        # Der User-Prompt soll den gekürzten Text enthalten
        user_content = captured_payloads[0]["messages"][1]["content"]
        # Prüfe die längste zusammenhängende Sequenz aus dem Dokumentinhalt statt Prompt-Overhead mitzuzählen.
        longest_run = max((len(m.group(0)) for m in re.finditer(r"x+", user_content)), default=0)
        assert longest_run <= 100, f"Erwarte max 100 zusammenhängende x-Zeichen im Prompt, gefunden: {longest_run}"
        assert longest_run > 0, "Erwarte mindestens 1 x-Zeichen (gekürzter Text muss im Prompt sein)"

    def test_ocr_correct_uses_text_model(self, monkeypatch):
        """correct_ocr_text soll MISTRAL_TEXT_MODEL verwenden."""
        captured_payloads = []

        async def mock_call_mistral(payload, **_kwargs):
            captured_payloads.append(payload)
            return {
                "choices": [{"message": {"content": "corrected text\n---CORRECTIONS: 2"}}],
                "usage": {"total_tokens": 20},
            }

        monkeypatch.setattr(self.server, "call_mistral_vision_api", mock_call_mistral)
        monkeypatch.setattr(self.server, "MISTRAL_API_KEY", "test-key")
        monkeypatch.setattr(self.server, "MISTRAL_TEXT_MODEL", "mistral-small-2603")

        run_async(self.server.correct_ocr_text("sample ocr text"))

        assert captured_payloads[0]["model"] == "mistral-small-2603"

    def test_ocr_correct_uses_max_tokens(self, monkeypatch):
        """correct_ocr_text soll OCR_CORRECT_MAX_TOKENS verwenden."""
        captured_payloads = []

        async def mock_call_mistral(payload, **_kwargs):
            captured_payloads.append(payload)
            return {
                "choices": [{"message": {"content": "corrected\n---CORRECTIONS: 0"}}],
                "usage": {"total_tokens": 5},
            }

        monkeypatch.setattr(self.server, "call_mistral_vision_api", mock_call_mistral)
        monkeypatch.setattr(self.server, "MISTRAL_API_KEY", "test-key")
        monkeypatch.setattr(self.server, "OCR_CORRECT_MAX_TOKENS", 8192)

        run_async(self.server.correct_ocr_text("some text"))

        assert captured_payloads[0]["max_tokens"] == 8192

    def test_extract_uses_text_model(self, monkeypatch):
        """extract_structured_data soll MISTRAL_TEXT_MODEL verwenden."""
        captured_payloads = []

        async def mock_call_mistral(payload, **_kwargs):
            captured_payloads.append(payload)
            return {
                "choices": [{"message": {"content": '{"name": "Test"}'}}],
                "usage": {"total_tokens": 15},
            }

        monkeypatch.setattr(self.server, "call_mistral_vision_api", mock_call_mistral)
        monkeypatch.setattr(self.server, "MISTRAL_API_KEY", "test-key")
        monkeypatch.setattr(self.server, "MISTRAL_TEXT_MODEL", "mistral-small-2603")

        schema = {"type": "object", "properties": {"name": {"type": "string"}}}
        run_async(self.server.extract_structured_data("sample text", schema))

        assert captured_payloads[0]["model"] == "mistral-small-2603"

    def test_extract_uses_max_tokens(self, monkeypatch):
        """extract_structured_data soll EXTRACT_MAX_TOKENS verwenden."""
        captured_payloads = []

        async def mock_call_mistral(payload, **_kwargs):
            captured_payloads.append(payload)
            return {
                "choices": [{"message": {"content": '{"name": "Test"}'}}],
                "usage": {"total_tokens": 15},
            }

        monkeypatch.setattr(self.server, "call_mistral_vision_api", mock_call_mistral)
        monkeypatch.setattr(self.server, "MISTRAL_API_KEY", "test-key")
        monkeypatch.setattr(self.server, "EXTRACT_MAX_TOKENS", 4096)

        schema = {"type": "object", "properties": {"name": {"type": "string"}}}
        run_async(self.server.extract_structured_data("sample text", schema))

        assert captured_payloads[0]["max_tokens"] == 4096

    def test_extract_uses_json_schema_response_format_by_default(self, monkeypatch):
        captured_payloads = []

        async def mock_call_mistral(payload, **_kwargs):
            captured_payloads.append(payload)
            return {
                "choices": [{"message": {"content": '{"name": "Test"}'}}],
                "usage": {"total_tokens": 15},
            }

        monkeypatch.setattr(self.server, "call_mistral_vision_api", mock_call_mistral)
        monkeypatch.setattr(self.server, "MISTRAL_API_KEY", "test-key")
        monkeypatch.setattr(self.server, "MISTRAL_EXTRACT_RESPONSE_FORMAT", "json_schema")

        schema = {"type": "object", "properties": {"name": {"type": "string"}}}
        run_async(self.server.extract_structured_data("sample text", schema))

        response_format = captured_payloads[0]["response_format"]
        assert response_format["type"] == "json_schema"
        meta_schema = response_format["json_schema"]["schema"]["properties"]["_meta"]
        assert meta_schema["type"] == "object"
        assert "properties" in meta_schema
        assert "zusammenfassung" in meta_schema["properties"]

    def test_extract_can_use_json_object_response_format(self, monkeypatch):
        captured_payloads = []

        async def mock_call_mistral(payload, **_kwargs):
            captured_payloads.append(payload)
            return {
                "choices": [{"message": {"content": '{"name": "Test"}'}}],
                "usage": {"total_tokens": 15},
            }

        monkeypatch.setattr(self.server, "call_mistral_vision_api", mock_call_mistral)
        monkeypatch.setattr(self.server, "MISTRAL_API_KEY", "test-key")
        monkeypatch.setattr(self.server, "MISTRAL_EXTRACT_RESPONSE_FORMAT", "json_object")

        schema = {"type": "object", "properties": {"name": {"type": "string"}}}
        run_async(self.server.extract_structured_data("sample text", schema))

        assert captured_payloads[0]["response_format"] == {"type": "json_object"}

    def test_extract_can_disable_structured_response_format(self, monkeypatch):
        captured_payloads = []

        async def mock_call_mistral(payload, **_kwargs):
            captured_payloads.append(payload)
            return {
                "choices": [{"message": {"content": '{"name": "Test"}'}}],
                "usage": {"total_tokens": 15},
            }

        monkeypatch.setattr(self.server, "call_mistral_vision_api", mock_call_mistral)
        monkeypatch.setattr(self.server, "MISTRAL_API_KEY", "test-key")
        monkeypatch.setattr(self.server, "MISTRAL_EXTRACT_RESPONSE_FORMAT", "legacy")

        schema = {"type": "object", "properties": {"name": {"type": "string"}}}
        run_async(self.server.extract_structured_data("sample text", schema))

        assert "response_format" not in captured_payloads[0]

    def test_vision_uses_max_tokens(self, monkeypatch):
        """analyze_with_mistral_vision soll VISION_MAX_TOKENS verwenden."""
        captured_payloads = []

        async def mock_call_mistral(payload, **_kwargs):
            captured_payloads.append(payload)
            return {
                "choices": [{"message": {"content": "image description"}}],
                "usage": {"total_tokens": 50, "prompt_tokens": 40, "completion_tokens": 10},
            }

        monkeypatch.setattr(self.server, "call_mistral_vision_api", mock_call_mistral)
        monkeypatch.setattr(self.server, "MISTRAL_API_KEY", "test-key")
        monkeypatch.setattr(self.server, "VISION_MAX_TOKENS", 8192)

        # Minimal valid PNG bytes (1x1 pixel)
        import base64
        tiny_png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
        )
        run_async(self.server.analyze_with_mistral_vision(tiny_png, "image/png", "describe"))

        assert captured_payloads[0]["max_tokens"] == 8192
