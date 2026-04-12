"""
Tests für T-MKIT-020: High-Accuracy Pipeline (OCR → Correction → Schema-Extraktion).
Enthält auch Tests für T-DAI-020: 429 Rate-Limit Handling in mistral_client.

Alle Tests laufen ohne Docker-Container und ohne echte API-Calls.
Alle externen Abhängigkeiten werden per unittest.mock gemockt.
"""

import asyncio
import importlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

# httpx VOR load_server_module importieren — danach wird httpx durch MagicMock ersetzt!
import httpx as _real_httpx

from conftest import load_server_module, run_async, PNG_100x100


# Einmal laden; alle Tests in diesem Modul teilen diese Instanz
_server = load_server_module(use_real_pil=False)
convert_auto = _server.convert_auto
dual_pass_validate = _server.dual_pass_validate


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def _make_convert_auto_kwargs(**overrides) -> dict:
    """Liefert minimale Kwargs für convert_auto."""
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
# Tests: Standard-Modus
# ---------------------------------------------------------------------------


class TestAccuracyStandard:
    """Standard-Modus: keine extra Pipeline-Steps, accuracy_mode='standard'."""

    def test_accuracy_standard_no_extra_steps(self):
        """
        Standard-Modus setzt pipeline_steps=['vision'] und kein dual_pass.
        """
        vision_resp = _make_vision_response("# Standard Result")

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "call_mistral_vision_api", new=AsyncMock(return_value=vision_resp)):
            result = run_async(convert_auto(**_make_convert_auto_kwargs(accuracy="standard")))

        assert result.success is True
        assert result.meta.accuracy_mode == "standard"
        assert result.meta.pipeline_steps == ["vision"]

    def test_accuracy_default_standard(self):
        """
        Default-Wert von accuracy ist 'standard'.
        """
        vision_resp = _make_vision_response("# Default")

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "call_mistral_vision_api", new=AsyncMock(return_value=vision_resp)):
            # accuracy wird NICHT übergeben → Default
            result = run_async(convert_auto(**_make_convert_auto_kwargs()))

        assert result.success is True
        assert result.meta.accuracy_mode == "standard"

    def test_accuracy_meta_fields_standard(self):
        """
        Meta enthält accuracy_mode und pipeline_steps im Standard-Modus.
        """
        vision_resp = _make_vision_response("Text")

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "call_mistral_vision_api", new=AsyncMock(return_value=vision_resp)):
            result = run_async(convert_auto(**_make_convert_auto_kwargs(accuracy="standard")))

        assert result.meta.accuracy_mode == "standard"
        assert isinstance(result.meta.pipeline_steps, list)
        assert len(result.meta.pipeline_steps) > 0


# ---------------------------------------------------------------------------
# Tests: High-Accuracy Modus — Bilder
# ---------------------------------------------------------------------------


class TestAccuracyHighImage:
    """High-Accuracy: Bild → Vision + Dual-Pass Validation."""

    def test_accuracy_high_image(self):
        """
        High-Accuracy bei Bild: dual_pass_validation wird angehängt.
        """
        vision_resp = _make_vision_response("# OCR Result")
        dual_pass_resp = _make_vision_response("# Corrected Result")

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "call_mistral_vision_api", new=AsyncMock(
                 side_effect=[vision_resp, dual_pass_resp]
             )):
            result = run_async(convert_auto(**_make_convert_auto_kwargs(
                accuracy="high",
            )))

        assert result.success is True
        assert result.meta.accuracy_mode == "high"
        assert "vision" in result.meta.pipeline_steps
        assert "dual_pass_validation" in result.meta.pipeline_steps

    def test_accuracy_high_image_with_schema(self):
        """
        High-Accuracy + Schema bei Bild: vision + ocr_correct (auto bei high) + dual_pass + schema_extraction.

        T-MKIT-022: accuracy='high' aktiviert correct_ocr_text() automatisch für Bilder,
        daher brauchen wir 4 mock-Antworten statt 3.
        """
        schema = {"type": "object", "properties": {"name": {"type": "string"}}}
        vision_resp = _make_vision_response("# OCR")
        ocr_correct_resp = _make_vision_response("# OCR<<<CORRECTIONS:0>>>")  # correct_ocr_text call (T-MKIT-022)
        dual_pass_resp = _make_vision_response("# Corrected")
        extract_resp = _make_vision_response('{"name": "Test"}')

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "call_mistral_vision_api", new=AsyncMock(
                 side_effect=[vision_resp, ocr_correct_resp, dual_pass_resp, extract_resp]
             )):
            result = run_async(convert_auto(**_make_convert_auto_kwargs(
                accuracy="high",
                extract_schema=schema,
            )))

        assert result.success is True
        assert "dual_pass_validation" in result.meta.pipeline_steps
        assert "schema_extraction" in result.meta.pipeline_steps


# ---------------------------------------------------------------------------
# Tests: High-Accuracy Modus — Scanned PDF
# ---------------------------------------------------------------------------


class TestAccuracyHighScannedPdf:
    """High-Accuracy: Gescanntes PDF → OCR + Correction + Dual-Pass."""

    def _make_pdf_kwargs(self, **overrides) -> dict:
        # Minimales PDF (magic bytes) — wird als PDF-Extension erkannt
        pdf_data = b"%PDF-1.4 minimal"
        kwargs = dict(
            file_data=pdf_data,
            filename="scan.pdf",
            source="scan.pdf",
            source_type="base64",
            input_meta={},
            language="de",
            accuracy="high",
        )
        kwargs.update(overrides)
        return kwargs

    def test_accuracy_high_scanned_pdf(self):
        """
        High-Accuracy bei gescanntem PDF: ocr + ocr_correction + dual_pass_validation.
        """
        ocr_markdown = "# Gescannter Text"
        corrected_markdown = "# Korrigierter Text"

        ocr_result = {
            "success": True,
            "markdown": ocr_markdown,
            "scanned": True,
            "pages": 1,
            "ocr_model": "mistral-ocr-2512",
        }
        correction_result = {
            "success": True,
            "corrected_text": corrected_markdown,
            "corrections_count": 3,
        }

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "detect_mimetype_from_bytes", return_value="application/pdf"), \
             patch.object(_server, "is_scanned_pdf", return_value=True), \
             patch.object(_server, "convert_scanned_pdf", new=AsyncMock(return_value=ocr_result)), \
             patch.object(_server, "correct_ocr_text", new=AsyncMock(return_value=correction_result)), \
             patch.object(_server, "dual_pass_validate", new=AsyncMock(return_value="# Final Corrected")), \
             patch.object(_server, "PDF2IMAGE_AVAILABLE", True), \
             patch.object(_server, "convert_from_path", return_value=[MagicMock(
                 width=100, height=100,
                 resize=MagicMock(return_value=MagicMock(
                     save=MagicMock(), width=100, height=100
                 )),
                 save=MagicMock(),
             )]):
            result = run_async(convert_auto(**self._make_pdf_kwargs(no_cache=True)))

        assert result.success is True
        assert result.meta.accuracy_mode == "high"
        assert "ocr" in result.meta.pipeline_steps
        assert "ocr_correction" in result.meta.pipeline_steps
        assert "dual_pass_validation" in result.meta.pipeline_steps

    def test_accuracy_high_scanned_pdf_multipage_skips_single_page_dual_pass(self):
        """
        Multi-page scanned PDFs must not run single-page dual-pass validation against the whole OCR markdown.
        """
        ocr_result = {
            "success": True,
            "markdown": "# Seite 1\n\n...\n\n# Seite 2\n\n...",
            "scanned": True,
            "pages": 52,
            "ocr_model": "mistral-ocr-2512",
        }
        correction_result = {
            "success": True,
            "corrected_text": "# Vollständiger OCR-Text",
            "corrections_count": 3,
        }

        dual_pass_mock = AsyncMock(return_value="# Falsch reduziert")

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "detect_mimetype_from_bytes", return_value="application/pdf"), \
             patch.object(_server, "is_scanned_pdf", return_value=True), \
             patch.object(_server, "convert_scanned_pdf", new=AsyncMock(return_value=ocr_result)), \
             patch.object(_server, "correct_ocr_text", new=AsyncMock(return_value=correction_result)), \
             patch.object(_server, "dual_pass_validate", new=dual_pass_mock), \
             patch.object(_server, "PDF2IMAGE_AVAILABLE", True):
            result = run_async(convert_auto(**self._make_pdf_kwargs()))

        assert result.success is True
        assert "ocr" in result.meta.pipeline_steps
        assert "ocr_correction" in result.meta.pipeline_steps
        assert "dual_pass_validation" not in result.meta.pipeline_steps
        dual_pass_mock.assert_not_awaited()

    def test_accuracy_high_with_schema(self):
        """
        High-Accuracy + Schema bei gescanntem PDF: alle 4 Steps.
        """
        ocr_result = {
            "success": True,
            "markdown": "# Inhalt",
            "scanned": True,
            "pages": 1,
            "ocr_model": "mistral-ocr-2512",
        }
        correction_result = {"success": True, "corrected_text": "# Korrigiert", "corrections_count": 2}
        schema = {"type": "object", "properties": {"betrag": {"type": "number"}}}
        extract_result = {"success": True, "extracted": {"betrag": 42.0}}

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "detect_mimetype_from_bytes", return_value="application/pdf"), \
             patch.object(_server, "is_scanned_pdf", return_value=True), \
             patch.object(_server, "convert_scanned_pdf", new=AsyncMock(return_value=ocr_result)), \
             patch.object(_server, "correct_ocr_text", new=AsyncMock(return_value=correction_result)), \
             patch.object(_server, "dual_pass_validate", new=AsyncMock(return_value="# Dual-Pass Final")), \
             patch.object(_server, "extract_structured_data", new=AsyncMock(return_value=extract_result)), \
             patch.object(_server, "PDF2IMAGE_AVAILABLE", True), \
             patch.object(_server, "convert_from_path", return_value=[MagicMock(
                 width=100, height=100,
                 resize=MagicMock(return_value=MagicMock(save=MagicMock(), width=100, height=100)),
                 save=MagicMock(),
             )]):
            result = run_async(convert_auto(**self._make_pdf_kwargs(extract_schema=schema)))

        assert result.success is True
        assert "ocr" in result.meta.pipeline_steps
        assert "ocr_correction" in result.meta.pipeline_steps
        assert "dual_pass_validation" in result.meta.pipeline_steps
        assert "schema_extraction" in result.meta.pipeline_steps

    def test_accuracy_high_scanned_pdf_ocr_correction_auto(self):
        """
        High-Accuracy → OCR-Correction wird automatisch ausgeführt (auch ohne ocr_correct=True).
        """
        ocr_result = {
            "success": True,
            "markdown": "# Roher OCR-Text",
            "scanned": True,
            "pages": 1,
            "ocr_model": "mistral-ocr-2512",
        }
        correction_result = {"success": True, "corrected_text": "# Bereinigt", "corrections_count": 5}

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "detect_mimetype_from_bytes", return_value="application/pdf"), \
             patch.object(_server, "is_scanned_pdf", return_value=True), \
             patch.object(_server, "convert_scanned_pdf", new=AsyncMock(return_value=ocr_result)), \
             patch.object(_server, "correct_ocr_text", new=AsyncMock(return_value=correction_result)), \
             patch.object(_server, "dual_pass_validate", new=AsyncMock(return_value="# Bereinigt")), \
             patch.object(_server, "PDF2IMAGE_AVAILABLE", False):  # kein Dual-Pass wegen kein pdf2image
            result = run_async(convert_auto(**self._make_pdf_kwargs(ocr_correct=False)))

        assert result.success is True
        assert "ocr_correction" in result.meta.pipeline_steps


# ---------------------------------------------------------------------------
# Tests: High-Accuracy Modus — Text-PDF (non-scan)
# ---------------------------------------------------------------------------


class TestAccuracyHighNonScanPdf:
    """High-Accuracy bei Text-PDFs: markitdown + optional dual_pass."""

    def _make_pdf_kwargs(self, **overrides) -> dict:
        pdf_data = b"%PDF-1.4 text"
        kwargs = dict(
            file_data=pdf_data,
            filename="textpdf.pdf",
            source="textpdf.pdf",
            source_type="base64",
            input_meta={},
            language="de",
            accuracy="high",
        )
        kwargs.update(overrides)
        return kwargs

    def test_accuracy_high_non_scan_pdf(self):
        """
        High-Accuracy bei Text-PDF: markitdown + dual_pass_validation.
        """
        markitdown_result = {"success": True, "markdown": "# Text PDF Inhalt"}

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "detect_mimetype_from_bytes", return_value="application/pdf"), \
             patch.object(_server, "is_scanned_pdf", return_value=False), \
             patch.object(_server, "convert_with_markitdown", return_value=markitdown_result), \
             patch.object(_server, "dual_pass_validate", new=AsyncMock(return_value="# Dual Corrected")), \
             patch.object(_server, "PDF2IMAGE_AVAILABLE", True), \
             patch.object(_server, "convert_from_path", return_value=[MagicMock(
                 width=100, height=100,
                 resize=MagicMock(return_value=MagicMock(save=MagicMock(), width=100, height=100)),
                 save=MagicMock(),
             )]):
            result = run_async(convert_auto(**self._make_pdf_kwargs()))

        assert result.success is True
        assert result.meta.accuracy_mode == "high"
        assert "markitdown" in result.meta.pipeline_steps
        assert "dual_pass_validation" in result.meta.pipeline_steps


# ---------------------------------------------------------------------------
# Tests: dual_pass_validate()
# ---------------------------------------------------------------------------


class TestDualPassValidate:
    """Tests für die dual_pass_validate() Hilfsfunktion."""

    def test_dual_pass_validate_success(self):
        """
        dual_pass_validate gibt korrigierten Text zurück wenn Vision erfolgreich.
        """
        corrected_text = "# Korrekt formatierter Text"
        vision_resp = _make_vision_response(corrected_text)

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "call_mistral_vision_api", new=AsyncMock(return_value=vision_resp)):
            result = run_async(dual_pass_validate(
                markdown="# OCR Fehler",
                file_data=PNG_100x100,
                mimetype="image/png",
                language="de",
            ))

        assert result == corrected_text

    def test_dual_pass_validate_fallback_on_vision_failure(self):
        """
        Bei Vision-Fehler gibt dual_pass_validate den Original-Markdown zurück.
        """
        original_markdown = "# Originaler OCR-Text"

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "analyze_with_mistral_vision", new=AsyncMock(return_value={
                 "success": False,
                 "error": "Vision nicht verfügbar",
             })):
            result = run_async(dual_pass_validate(
                markdown=original_markdown,
                file_data=PNG_100x100,
                mimetype="image/png",
                language="de",
            ))

        assert result == original_markdown

    def test_dual_pass_validate_fallback_no_api_key(self):
        """
        Ohne API-Key gibt dual_pass_validate den Original-Markdown zurück (graceful).
        """
        original_markdown = "# Originaler Text"

        with patch.object(_server, "MISTRAL_API_KEY", ""):
            result = run_async(dual_pass_validate(
                markdown=original_markdown,
                file_data=PNG_100x100,
                mimetype="image/png",
                language="de",
            ))

        assert result == original_markdown

    def test_dual_pass_validate_fallback_on_exception(self):
        """
        Bei unerwarteter Exception gibt dual_pass_validate den Original zurück.
        """
        original_markdown = "# Fehlertoleranz-Test"

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "analyze_with_mistral_vision", new=AsyncMock(
                 side_effect=RuntimeError("Netzwerkfehler")
             )):
            result = run_async(dual_pass_validate(
                markdown=original_markdown,
                file_data=PNG_100x100,
                mimetype="image/png",
                language="de",
            ))

        assert result == original_markdown


# ---------------------------------------------------------------------------
# Tests: Meta-Felder
# ---------------------------------------------------------------------------


class TestAccuracyMetaFields:
    """Prüft dass accuracy_mode und pipeline_steps immer korrekt in Meta landen."""

    def test_accuracy_meta_fields_high(self):
        """
        High-Accuracy: Meta enthält accuracy_mode='high' und pipeline_steps mit Steps.
        """
        vision_resp = _make_vision_response("# High Result")
        dual_pass_resp = _make_vision_response("# Dual Result")

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "call_mistral_vision_api", new=AsyncMock(
                 side_effect=[vision_resp, dual_pass_resp]
             )):
            result = run_async(convert_auto(**_make_convert_auto_kwargs(accuracy="high")))

        assert result.success is True
        assert result.meta.accuracy_mode == "high"
        assert isinstance(result.meta.pipeline_steps, list)
        assert len(result.meta.pipeline_steps) >= 2

    def test_accuracy_meta_fields_standard_always_set(self):
        """
        Auch im Standard-Modus sind accuracy_mode und pipeline_steps gesetzt.
        """
        vision_resp = _make_vision_response("# Standard")

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "call_mistral_vision_api", new=AsyncMock(return_value=vision_resp)):
            result = run_async(convert_auto(**_make_convert_auto_kwargs(accuracy="standard")))

        assert result.meta.accuracy_mode is not None
        assert result.meta.pipeline_steps is not None


# ---------------------------------------------------------------------------
# Tests: T-DAI-020 — 429 Rate Limit Handling in mistral_client
# ---------------------------------------------------------------------------


def _load_mistral_client_module():
    """Lädt mistral_client frisch mit gemockten Abhängigkeiten."""
    server_path = str(Path(__file__).parent.parent)
    if server_path not in sys.path:
        sys.path.insert(0, server_path)

    for mod in ("mistral_client", "settings", "models", "utils", "templates_db"):
        if mod in sys.modules:
            del sys.modules[mod]

    import mistral_client as mc
    return mc


class TestRateLimitHandling:
    """T-DAI-020: 429 Rate-Limit Retry-After und exponentielles Backoff."""

    def _make_429_response(self, retry_after: str | None = None) -> MagicMock:
        """Erzeugt eine Mock-Response mit Status 429."""
        response = MagicMock()
        response.status_code = 429
        if retry_after is not None:
            response.headers = {"retry-after": retry_after}
        else:
            response.headers = {}
        return response

    def test_rate_limit_with_retry_after_waits(self):
        """
        _handle_rate_limit mit Retry-After Header: asyncio.sleep wird mit dem
        angegebenen Wert aufgerufen, dann raise_for_status geworfen.
        """
        mc = _load_mistral_client_module()

        response_429 = self._make_429_response(retry_after="5")
        response_429.raise_for_status.side_effect = _real_httpx.HTTPStatusError(
            "429", request=MagicMock(), response=response_429
        )

        sleep_calls = []

        async def mock_sleep(seconds):
            sleep_calls.append(seconds)

        with patch("asyncio.sleep", new=mock_sleep):
            with pytest.raises(_real_httpx.HTTPStatusError):
                run_async(mc._handle_rate_limit(response_429))

        assert len(sleep_calls) == 1
        assert sleep_calls[0] == 5

    def test_rate_limit_without_retry_after_no_sleep(self):
        """
        Bei HTTP 429 ohne Retry-After: asyncio.sleep wird NICHT aufgerufen
        (tenacity's exponentielles Backoff übernimmt). HTTPStatusError wird geworfen.
        """
        mc = _load_mistral_client_module()

        response_429 = self._make_429_response(retry_after=None)
        response_429.raise_for_status.side_effect = _real_httpx.HTTPStatusError(
            "429", request=MagicMock(), response=response_429
        )

        sleep_calls = []

        async def mock_sleep(seconds):
            sleep_calls.append(seconds)

        with patch("asyncio.sleep", new=mock_sleep):
            with pytest.raises(_real_httpx.HTTPStatusError):
                run_async(mc._handle_rate_limit(response_429))

        # Ohne Retry-After: kein asyncio.sleep (tenacity handelt das)
        assert len(sleep_calls) == 0

    def test_rate_limit_retry_after_capped_at_max_wait(self):
        """
        Retry-After > RATE_LIMIT_MAX_WAIT_SECONDS wird auf das Maximum gecappt.
        Standard-Default: 60s.
        """
        mc = _load_mistral_client_module()

        # Retry-After mit extremem Wert
        response_429 = self._make_429_response(retry_after="9999")
        response_429.raise_for_status.side_effect = _real_httpx.HTTPStatusError(
            "429", request=MagicMock(), response=response_429
        )

        sleep_calls = []

        async def mock_sleep(seconds):
            sleep_calls.append(seconds)

        with patch("asyncio.sleep", new=mock_sleep):
            with pytest.raises(_real_httpx.HTTPStatusError):
                run_async(mc._handle_rate_limit(response_429))

        assert len(sleep_calls) == 1
        # Muss auf RATE_LIMIT_MAX_WAIT_SECONDS (60) gecappt sein
        assert sleep_calls[0] <= 60

    def test_rate_limit_logs_and_audits_request_context(self):
        """429-Handling trägt request_id, attempt und Schritt in Log und Audit."""
        mc = _load_mistral_client_module()

        response_429 = self._make_429_response(retry_after="5")
        response_429.raise_for_status.side_effect = _real_httpx.HTTPStatusError(
            "429", request=MagicMock(), response=response_429
        )

        async def mock_sleep(_seconds):
            return None

        with patch("asyncio.sleep", new=mock_sleep), \
             patch.object(mc.log, "warning") as log_warning, \
             patch.object(mc, "_audit") as audit_mock:
            with pytest.raises(_real_httpx.HTTPStatusError):
                run_async(
                    mc._handle_rate_limit(
                        response_429,
                        request_id="req-123",
                        attempt_number=2,
                        pipeline_step="extract_structured_data",
                        page=4,
                        filename="scan.pdf",
                        upstream_attempt=3,
                    )
                )

        log_warning.assert_called_once_with(
            "rate_limited",
            retry_after=5,
            request_id="req-123",
            attempt_number=2,
            pipeline_step="extract_structured_data",
            page=4,
            filename="scan.pdf",
            upstream_attempt=3,
        )
        audit_mock.assert_called_once()
        assert audit_mock.call_args.kwargs["request_id"] == "req-123"
        assert audit_mock.call_args.kwargs["metadata"]["attempt_number"] == 2
        assert audit_mock.call_args.kwargs["metadata"]["pipeline_step"] == "extract_structured_data"
        assert audit_mock.call_args.kwargs["metadata"]["page"] == 4
        assert audit_mock.call_args.kwargs["metadata"]["filename"] == "scan.pdf"
        assert audit_mock.call_args.kwargs["metadata"]["upstream_attempt"] == 3

    def test_all_retries_exhausted_returns_clear_error(self):
        """
        Wenn alle Retries fehlschlagen (429): analyze_with_mistral_vision gibt
        klaren Error zurück mit 'unavailable after' und 'retries' im Text.
        """
        request_mock = MagicMock()
        response_mock = MagicMock()
        response_mock.status_code = 429
        error = _real_httpx.HTTPStatusError(
            "429 Too Many Requests",
            request=request_mock,
            response=response_mock,
        )

        with patch.object(_server, "MISTRAL_API_KEY", "test-key"), \
             patch.object(_server, "call_mistral_vision_api", new=AsyncMock(side_effect=error)):
            result = run_async(_server.analyze_with_mistral_vision(
                image_data=PNG_100x100,
                mimetype="image/png",
                prompt="Beschreibe das Bild",
                language="de",
            ))

        assert result["success"] is False
        assert "unavailable after" in result["error"]
        assert "retries" in result["error"]
