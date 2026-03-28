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
        assert "mode must be 'default' or 'full'" in str(exc_info.value)

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
